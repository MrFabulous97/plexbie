# path: database/session.py
"""Database session management"""
from contextlib import asynccontextmanager

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from database.models import Base
from core.logging import get_logger

logger = get_logger(__name__)

engine = None
SessionLocal = None


async def init_database(db_url: str):
    """Initialize database connection"""
    global engine, SessionLocal
    
    # Convert sqlite URL for async
    if db_url.startswith("sqlite:"):
        db_url = db_url.replace("sqlite:", "sqlite+aiosqlite:")
    
    engine = create_async_engine(db_url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_kv_unique(conn)
        await _migrate_add_columns(conn)
        await _migrate_media_requests_to_kv(conn)

    logger.info("Database initialized")


#: Columns added to tables that already existed in the wild. create_all() only
#: applies a model's columns when it creates the table, so a new field never
#: reaches an existing database - which is how watch_party_credits ended up with
#: both `last_credited_at` (the model) and `last_credit_at` (added by hand).
#: Entries are (table, column, DDL type with any default).
_ADDED_COLUMNS = (
    ("plex_users", "is_top_watcher", "BOOLEAN NOT NULL DEFAULT 0"),
    ("plex_users", "exemption_lost_at", "DATETIME"),
)


#: The JSON file media requests used to live in, before one row per request.
_LEGACY_REQUESTS_FILE = Path("config/media_requests.json")


async def _migrate_media_requests_to_kv(conn):
    """Copy config/media_requests.json into the key-value store. Idempotent.

    ON CONFLICT DO NOTHING rather than DO UPDATE, deliberately: once a request
    lives in the database it is the authority, and re-reading a stale file must
    never clobber a status the bot has since recorded.

    The file is left on disk untouched, as a backup. Nothing reads it afterwards.
    """
    try:
        if not _LEGACY_REQUESTS_FILE.exists():
            return

        raw = json.loads(_LEGACY_REQUESTS_FILE.read_text() or "{}")
        if not isinstance(raw, dict) or not raw:
            return

        existing = await conn.execute(text(
            "SELECT COUNT(*) FROM key_value_store WHERE namespace = 'media_requests'"
        ))
        already = existing.scalar() or 0

        inserted = 0
        for message_id, record in raw.items():
            if not isinstance(record, dict):
                continue
            await conn.execute(
                text(
                    "INSERT INTO key_value_store (namespace, key, value) "
                    "VALUES ('media_requests', :key, :value) "
                    "ON CONFLICT (namespace, key) DO NOTHING"
                ),
                {"key": str(message_id), "value": json.dumps(record)},
            )
            inserted += 1

        after = await conn.execute(text(
            "SELECT COUNT(*) FROM key_value_store WHERE namespace = 'media_requests'"
        ))
        total = after.scalar() or 0
        if total != already:
            logger.info(
                f"Migrated media requests into the database: {len(raw)} in the file, "
                f"{already} already present, {total} now stored "
                f"(config/media_requests.json left in place as a backup)"
            )
    except Exception as e:
        # Never block startup. The plugins read the database; a failure here means
        # older requests are missing from it, which is visible and recoverable,
        # whereas a bot that will not start is neither.
        logger.error(f"Could not migrate media requests into the database: {e}")


async def _migrate_add_columns(conn):
    """Add any model column the database is missing. Idempotent.

    Deliberately additive only: it never drops or retypes anything, so running it
    against an unexpected schema cannot lose data. Failure is logged and
    swallowed - a missing column surfaces as a clear error from the query that
    needs it, which is easier to diagnose than a bot that will not start.
    """
    for table, column, ddl in _ADDED_COLUMNS:
        try:
            result = await conn.execute(text(f"PRAGMA table_info('{table}')"))
            existing = {row[1] for row in result}
            if not existing:
                # Table absent entirely; create_all will have made it from the
                # model, columns included.
                continue
            if column in existing:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info(f"Added missing column {table}.{column}")
        except Exception as e:
            logger.error(f"Could not add column {table}.{column}: {e}")


async def _migrate_kv_unique(conn):
    """Enforce one row per (namespace, key) on an already-created table.

    create_all only applies constraints when it creates the table, so databases
    that predate the UniqueConstraint on KeyValueStore keep accepting duplicates.
    Collapse any that exist (keeping the highest id, i.e. the most recent write)
    then add the index. Idempotent - safe to run on every startup.
    """
    try:
        result = await conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='uq_key_value_store_namespace_key'"
        ))
        if result.scalar():
            return

        deleted = await conn.execute(text(
            "DELETE FROM key_value_store WHERE id NOT IN ("
            "  SELECT MAX(id) FROM key_value_store GROUP BY namespace, key"
            ")"
        ))
        if deleted.rowcount:
            logger.warning(
                f"Collapsed {deleted.rowcount} duplicate key_value_store row(s) "
                f"before adding the uniqueness constraint"
            )

        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_key_value_store_namespace_key "
            "ON key_value_store (namespace, key)"
        ))
        logger.info("Applied key_value_store (namespace, key) uniqueness constraint")
    except Exception as e:
        # Never block startup on this; kv_set's upsert and kv_get's tolerant read
        # keep working without the index.
        logger.error(f"Could not apply key_value_store uniqueness migration: {e}")


@asynccontextmanager
async def get_session():
    """Get database session"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
