# path: database/session.py
"""Database session management"""
from contextlib import asynccontextmanager

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
    
    logger.info("Database initialized")


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
