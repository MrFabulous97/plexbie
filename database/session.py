# path: database/session.py
"""Database session management"""
from contextlib import asynccontextmanager

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

    logger.info("Database initialized")


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
