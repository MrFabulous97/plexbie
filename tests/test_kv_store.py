# path: tests/test_kv_store.py
"""Key-value store durability: atomic upsert and the uniqueness migration.

Regression coverage for: key_value_store had no uniqueness on (namespace, key),
so kv_set's select-then-insert could write two rows for one logical key. After
that, kv_get's scalar_one_or_none() raised MultipleResultsFound on every read -
and media_cleanup swallowed it, falling back to DEFAULT_CONFIG and silently
losing its exemption list while still deleting media.
"""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import conftest  # noqa: F401

UNIQUE_INDEX = "uq_key_value_store_namespace_key"

LEGACY_SCHEMA = """
CREATE TABLE key_value_store (
    id INTEGER NOT NULL PRIMARY KEY,
    namespace VARCHAR(64) NOT NULL,
    key VARCHAR(255) NOT NULL,
    value TEXT NOT NULL
)
"""


def _tmp_db(name):
    return str(Path(tempfile.mkdtemp()) / name)


def _index_count(path):
    with sqlite3.connect(path) as con:
        return con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
            (UNIQUE_INDEX,),
        ).fetchone()[0]


def _row_count(path, namespace, key):
    with sqlite3.connect(path) as con:
        return con.execute(
            "SELECT COUNT(*) FROM key_value_store WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()[0]


async def _init(path):
    """Point the module-level engine at `path`, disposing any previous one.

    init_database() replaces the global engine without disposing it, which is
    harmless in production (called once at startup) but leaks connection pools
    across tests - and a pool still holding the old file raises from its worker
    thread during teardown.
    """
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


async def _dispose():
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()


# --- fresh database ---

def test_fresh_database_gets_the_unique_index():
    path = _tmp_db("fresh.db")

    async def scenario():
        await _init(path)
        await _dispose()

    asyncio.run(scenario())
    assert _index_count(path) == 1


def test_set_then_get_roundtrip():
    from database.kv_store import kv_get, kv_set

    path = _tmp_db("roundtrip.db")

    async def scenario():
        await _init(path)
        await kv_set("ns", "k", {"a": 1})
        return await kv_get("ns", "k")

    assert asyncio.run(scenario()) == {"a": 1}


def test_repeated_set_updates_in_place_without_duplicating():
    from database.kv_store import kv_get, kv_set

    path = _tmp_db("upsert.db")

    async def scenario():
        await _init(path)
        for value in range(5):
            await kv_set("ns", "k", {"v": value})
        return await kv_get("ns", "k")

    assert asyncio.run(scenario()) == {"v": 4}
    assert _row_count(path, "ns", "k") == 1


def test_concurrent_sets_do_not_create_duplicate_rows():
    """The original race: two coroutines both saw "no row" and both inserted."""
    from database.kv_store import kv_get, kv_set

    path = _tmp_db("concurrent.db")

    async def scenario():
        await _init(path)
        await asyncio.gather(*(kv_set("ns", "same", {"writer": i}) for i in range(8)))
        return await kv_get("ns", "same")

    result = asyncio.run(scenario())
    assert _row_count(path, "ns", "same") == 1
    assert "writer" in result


def test_missing_key_returns_default():
    from database.kv_store import kv_get

    path = _tmp_db("default.db")

    async def scenario():
        await _init(path)
        return await kv_get("ns", "absent", "fallback")

    assert asyncio.run(scenario()) == "fallback"


def test_get_all_and_delete():
    from database.kv_store import kv_delete, kv_get, kv_get_all, kv_set

    path = _tmp_db("getall.db")

    async def scenario():
        await _init(path)
        await kv_set("ns", "a", 1)
        await kv_set("ns", "b", 2)
        await kv_set("other", "c", 3)
        everything = await kv_get_all("ns")
        deleted = await kv_delete("ns", "a")
        return everything, deleted, await kv_get("ns", "a")

    everything, deleted, after = asyncio.run(scenario())
    assert set(everything) == {"a", "b"}
    assert deleted is True
    assert after is None


# --- legacy database that predates the constraint ---

def _legacy_db_with_duplicates():
    path = _tmp_db("legacy.db")
    with sqlite3.connect(path) as con:
        con.execute(LEGACY_SCHEMA)
        # Two rows for one logical key - what broke every subsequent read.
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
            ("media_cleanup", "config", json.dumps({"generation": "old"})))
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
            ("media_cleanup", "config", json.dumps({"generation": "new"})))
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
            ("media_cleanup", "tracking", json.dumps({})))
        con.commit()
    assert _row_count(path, "media_cleanup", "config") == 2
    assert _index_count(path) == 0
    return path


def test_migration_collapses_duplicates_and_keeps_newest():
    from database.kv_store import kv_get

    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        return await kv_get("media_cleanup", "config")

    value = asyncio.run(scenario())
    assert _row_count(path, "media_cleanup", "config") == 1
    assert value == {"generation": "new"}, "migration kept the wrong row"
    assert _index_count(path) == 1


def test_migration_preserves_unrelated_rows():
    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        await _dispose()

    asyncio.run(scenario())
    assert _row_count(path, "media_cleanup", "tracking") == 1


def test_migration_is_idempotent():
    """It runs on every startup, so a second pass must be a no-op."""
    from database.kv_store import kv_get

    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        await _init(path)
        await _init(path)
        return await kv_get("media_cleanup", "config")

    assert asyncio.run(scenario()) == {"generation": "new"}
    assert _index_count(path) == 1


def test_reads_survive_duplicates_even_without_the_index():
    """Defence in depth: kv_get must not raise MultipleResultsFound if the
    migration could not apply for any reason.
    """
    from database.kv_store import kv_get

    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        # Re-introduce a duplicate behind the constraint's back, via a separate
        # connection. Deliberately do NOT re-init afterwards: the migration would
        # collapse the duplicate and re-add the index, which is the exact state
        # this test needs to avoid.
        with sqlite3.connect(path) as con:
            con.execute("DROP INDEX IF EXISTS " + UNIQUE_INDEX)
            con.execute(
                "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
                ("media_cleanup", "config", json.dumps({"generation": "newest"})))
            con.commit()

        result = await kv_get("media_cleanup", "config")
        # Dispose inside the running loop; leaving it to interpreter teardown
        # makes aiosqlite's worker thread raise against the mutated file.
        await _dispose()
        return result

    assert asyncio.run(scenario()) == {"generation": "newest"}
    assert _index_count(path) == 0, "test must exercise the no-index path"
