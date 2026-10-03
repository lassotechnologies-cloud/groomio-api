"""Database engine + async session (Doc 9).

Every business-scoped model carries `business_id`; every branch-scoped model
carries `branch_id`. The tenancy filter enforces isolation at query time.
"""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


# Detect if using Supabase pooler (pgbouncer) which doesn't support prepared statements
def _get_connect_args(database_url: str) -> dict:
    """Get connection arguments based on database URL."""
    if "pooler.supabase.com" in database_url:
        # pgbouncer in transaction mode doesn't support prepared statements
        return {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    return {}


engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    echo=False,
    connect_args=_get_connect_args(settings.database_url),
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency: one DB session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db_readonly() -> AsyncSession:
    """Read-only session for reports/analytics (Metabase-style)."""
    async with AsyncSessionLocal() as session:
        yield session
