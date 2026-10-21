"""Integration-test fixtures — a real Postgres, or a clean skip.

Why these skip rather than run on SQLite: the models use PostgreSQL `JSONB`,
`UUID` and native enums. SQLite would accept a subtly different schema, so a
suite that passed there would prove nothing about production — which is exactly
the gap these tests exist to close.

Point `TEST_DATABASE_URL` at a throwaway database to enable them:

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test

Port **5433**, not 5432: `docker-compose.test.yml` deliberately publishes the
test container there, because 5432 is this project's development PostgreSQL and
these tests drop and recreate the schema on every run.

**Never point this at a real database.** The suite creates and drops schema on
every session, and `tests/conftest.py` refuses a URL whose database name does not
look like a test database. That guard is deliberate: the alternative is a
fixture that quietly truncates a shop's live data the first time someone forgets
to read this comment.

Each test runs in a transaction that is rolled back, so tests are independent and
the database is left as it was found.

**The `db` fixture cannot express concurrency.** It binds one connection inside
one transaction that is always rolled back, so two "simultaneous" requests made
through it share a single session on a single connection: they cannot interleave,
they cannot commit independently, and one rollback discards both. A concurrency
test written against it would pass while the bug it targets is still live — a
false green, which is the one outcome worse than a red test. `concurrency_client`
below exists for exactly that reason and must not be replaced by `db`.
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

from sqlalchemy import text  # noqa: E402
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


@pytest.fixture(autouse=True)
def _neutralize_beat_lock(request, monkeypatch):
    """Tests that call a worker body directly must not open the app engine.

    `beat_lock` connects through `app.core.database.engine`, which is
    built from `settings.database_url` — the *development* database,
    not this test database. A test calling `_reconcile()` or
    `_remind()` directly would otherwise take a real advisory lock on
    the dev database (and fail outright where no database is
    reachable). The lock is infrastructure; these tests exercise task
    logic, so it is replaced with a context that always reports
    "acquired".

    A test that wants the real lock — to prove exclusivity — opts out
    with `@pytest.mark.real_beat_lock`.
    """
    if request.node.get_closest_marker("real_beat_lock"):
        return

    from contextlib import asynccontextmanager

    import app.workers.db as worker_db

    @asynccontextmanager
    async def _always_acquired(name: str):
        yield True

    monkeypatch.setattr(worker_db, "beat_lock", _always_acquired)


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


# ── Real-concurrency fixtures (T-003) ────────────────────────────────────────
#
# Everything below exists to do one thing the `db` fixture cannot: give each
# simultaneous HTTP request its own database connection and its own committed
# transaction, so a check-then-act race can actually happen and be observed.


#: Tables the concurrency suite writes to. Truncated rather than dropped, so a
#: test starts and ends with a genuinely empty slate. Every table is listed —
#: derived from `Base.metadata` would silently miss one that a future test adds,
#: and a leftover row would then surface as an unexplained failure somewhere else.
_CONCURRENCY_TABLES = (
    "subscriptions",
    "payments",
    "webhook_events",
    "services",
    "branches",
    "businesses",
    "otp_codes",
    "refresh_tokens",
    "device_tokens",
    "plans",
    "users",
)


async def _reset_concurrency_schema(conn) -> None:
    """Empty the tables the concurrency suite uses. Awaits a bounded lock wait.

    `SET LOCAL lock_timeout` turns "waiting forever on a lock another connection
    holds" into an immediate, named error. Without it a leaked connection from an
    interrupted run makes every subsequent test hang silently, which is a far
    worse failure mode than a loud one.
    """
    await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    tables = ", ".join(_CONCURRENCY_TABLES)
    await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY"))


@pytest.fixture()
async def concurrency_engine(test_database_url):
    """A real connection pool over an empty schema, for one test.

    Deliberately *not* the rollback fixture: every session handed out here
    commits for real, which is what makes an interleaving meaningful. The pool
    is sized for the widest concurrency test (five simultaneous requests) plus
    room for the assertions, so no request is ever silently serialised for want
    of a connection — that would make the test pass for the wrong reason.

    `create_all` is a no-op once the tables exist, so this does not rebuild the
    38-table schema on every test the way the `db` fixture does. The empty slate
    comes from `TRUNCATE` instead. That is not just a speed trade-off: `DROP
    TABLE` needs an ACCESS EXCLUSIVE lock, so if a connection is ever left
    holding one — a killed run, a leaked session — `drop_all` waits on that lock
    for the rest of the suite's life and the run hangs with no output. Truncating
    the same tables has the same lock requirement but is a single fast statement,
    and the `_reset_concurrency_schema` timeout below turns a genuine stall into
    a loud failure instead.
    """
    engine = create_async_engine(
        test_database_url, pool_size=10, max_overflow=5, future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _reset_concurrency_schema(conn)
    try:
        yield engine
    finally:
        try:
            async with engine.begin() as conn:
                await _reset_concurrency_schema(conn)
        finally:
            await engine.dispose()


@pytest.fixture()
async def sessions(concurrency_engine):
    """Factory for independent sessions on independent connections."""
    maker = async_sessionmaker(bind=concurrency_engine, expire_on_commit=False)

    def _make():
        return maker()

    return _make


@pytest.fixture()
async def concurrency_client(concurrency_engine):
    """An ASGI client whose every request gets its own session and connection.

    `get_db` is overridden with a per-request session that commits exactly the
    way `app.core.database.get_db` does — commit after the handler returns,
    rollback on any exception. Keeping the real commit/rollback semantics is the
    point: a savepoint-only fixture would let a partially-written onboarding
    look successful, and this suite exists to catch exactly that.

    Requests must be issued with `asyncio.gather` (or an equivalent) to
    interleave. Issuing them one after another tests nothing.
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.database import get_db
    from app.main import app

    maker = async_sessionmaker(bind=concurrency_engine, expire_on_commit=False)

    async def _override_get_db():
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
