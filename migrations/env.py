"""Alembic migration environment — uses the same models as the API."""
import asyncio
import os
from logging.config import fileConfig
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context

from app.models import *  # noqa: F401,F403 — import all models so Alembic sees them
from app.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_connect_args(database_url: str) -> dict:
    """Get connection arguments based on database URL."""
    if "pooler.supabase.com" in database_url:
        # pgbouncer in transaction mode doesn't support prepared statements
        return {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    return {}


def get_database_url() -> str:
    """Get database URL from environment variable or alembic.ini."""
    # First try to get from environment variable (loaded from .env)
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    # Fallback to alembic.ini config
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the DB)."""
    url = get_database_url()
    connect_args = _get_connect_args(url)
    connectable = create_async_engine(url, connect_args=connect_args)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
