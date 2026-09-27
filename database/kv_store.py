# path: database/kv_store.py
"""Key-value store helpers for plugin data persistence"""
import json
from typing import Any, Dict

from sqlalchemy import select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from core.logging import get_logger
from database.session import get_session
from database.models import KeyValueStore

logger = get_logger(__name__)


async def kv_get(namespace: str, key: str, default: Any = None) -> Any:
    """Get a value from the key-value store"""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore)
            .where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
            # Deliberately not scalar_one_or_none(): that raises
            # MultipleResultsFound on a duplicated key, and callers swallow the
            # exception and fall back to defaults - which for media_cleanup meant
            # silently losing the exemption list. Take the newest row instead so a
            # database written before the uniqueness constraint still reads.
            .order_by(KeyValueStore.id.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return row.value


async def kv_set(namespace: str, key: str, value: Any) -> None:
    """Set a value in the key-value store.

    Uses a single INSERT ... ON CONFLICT DO UPDATE so that two concurrent writers
    cannot both observe "no existing row" and each insert one. The previous
    select-then-insert yielded duplicate rows that then broke every read.
    """
    json_value = json.dumps(value)
    statement = sqlite_insert(KeyValueStore).values(
        namespace=namespace, key=key, value=json_value
    )
    statement = statement.on_conflict_do_update(
        index_elements=[KeyValueStore.namespace, KeyValueStore.key],
        set_={"value": json_value},
    )
    try:
        async with get_session() as session:
            await session.execute(statement)
            await session.commit()
    except OperationalError:
        # ON CONFLICT needs the unique index to exist. If the startup migration
        # could not apply it, fall back to the older read-then-write rather than
        # failing the write outright.
        logger.warning(
            f"Upsert unavailable for {namespace}/{key} (missing unique index); "
            f"falling back to read-then-write"
        )
        await _kv_set_fallback(namespace, key, json_value)


async def _kv_set_fallback(namespace: str, key: str, json_value: str) -> None:
    """Non-atomic write used only when the unique index is absent."""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore)
            .where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
            .order_by(KeyValueStore.id.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row:
            row.value = json_value
        else:
            session.add(KeyValueStore(namespace=namespace, key=key, value=json_value))
        await session.commit()


async def kv_delete(namespace: str, key: str) -> bool:
    """Delete a key from the key-value store. Returns True if deleted."""
    async with get_session() as session:
        result = await session.execute(
            delete(KeyValueStore).where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
        )
        await session.commit()
        return result.rowcount > 0


async def kv_get_all(namespace: str) -> Dict[str, Any]:
    """Get all key-value pairs in a namespace"""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore).where(KeyValueStore.namespace == namespace)
        )
        rows = result.scalars().all()
        data = {}
        for row in rows:
            try:
                data[row.key] = json.loads(row.value)
            except json.JSONDecodeError:
                data[row.key] = row.value
        return data
