"""Integration-test fixtures — a real Postgres, or a clean skip.

Why these skip rather than run on SQLite: the models use PostgreSQL `JSONB`,
`UUID` and native enums. SQLite would accept a subtly different schema, so a
suite that passed there would prove nothing about production — which is exactly
the gap these tests exist to close.

Point `TEST_DATABASE_URL` at a throwaway database to enable them:

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/groomio_test

**Never point this at a real database.** The suite creates and drops schema on
every session, and `tests/conftest.py` refuses a URL whose database name does not
look like a test database. That guard is deliberate: the alternative is a
fixture that quietly truncates a shop's live data the first time someone forgets
to read this comment.

Each test runs in a transaction that is rolled back, so tests are independent and
the database is left as it was found.
"""

import os
import re
from urllib.parse import urlparse

import pytest

# Collection-time guard: a missing driver must not turn into a wall of errors.
asyncpg = pytest.importorskip(
    "asyncpg", reason="asyncpg is required for integration tests"
)
pytest.importorskip("sqlalchemy", reason="SQLAlchemy is required")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

# `Base` lives in app.models.base; app.models/__init__ re-exports the table classes
# only. Importing the package registers every model against that Base, which is
# what makes `create_all` produce the full schema.
import app.models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402

# A database name that is obviously not production.
_TEST_DB_PATTERN = re.compile(r"(^test|test_|_test$|test$)", re.IGNORECASE)


def _database_name(url: str) -> str:
    path = urlparse(url).path or ""
    return path.lstrip("/").split("?")[0]


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set — skipping integration tests")

    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail(
            f"TEST_DATABASE_URL must use postgresql+asyncpg:// (got {url.split('://')[0]}://). "
            f"The app's engine is async; a sync driver fails on first connect."
        )

    name = _database_name(url)
    if not _TEST_DB_PATTERN.search(name):
        pytest.fail(
            f"Refusing to run: database {name!r} does not look like a test database. "
            f"These tests drop and recreate the schema. Set TEST_DATABASE_URL to a "
            f"throwaway database whose name contains 'test'."
        )
    return url


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture()
async def db(test_database_url):
    """A session inside a transaction that is always rolled back.

    Nested-transaction isolation is not used because the code under test relies on
    real commit/rollback semantics — `POST /sales` fans out across several tables
    and must be atomic. A savepoint would let a partial commit look like success.
    """
    engine = create_async_engine(test_database_url, future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    connection = await engine.connect()
    transaction = await connection.begin()
    maker = async_sessionmaker(bind=connection, expire_on_commit=False)

    try:
        async with maker() as session:
            yield session
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
