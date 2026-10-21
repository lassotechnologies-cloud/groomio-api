"""Async DB session for Celery tasks (Doc 10.11).

Celery tasks are synchronous, but the app's data layer is `asyncpg` + SQLAlchemy
async. Rather than add a second synchronous engine (two pools against one
database, and a second set of credentials to leak), each task runs its
coroutine through `asyncio.run` on a fresh session.

One consequence worth knowing: a retrying task gets a brand new connection and a
brand new transaction, so a task must be idempotent. Partial work from a failed
attempt is rolled back with the session, but anything sent to a third party (an
SMS, an M-Pesa push) is not. Send those last, or make the task tolerate resending.
"""

import asyncio
import functools
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine

T = TypeVar("T")


@asynccontextmanager
async def session_scope() -> AsyncIterator:
    """Async context manager giving one session, committing on clean exit."""
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def beat_lock(name: str) -> AsyncIterator[bool]:
    """Guard a beat task with a session-level advisory lock (T004-25).

    Beat fires a task again when its process fails over, or when a run
    lasts past its next scheduled slot — the reconciler every 5 minutes
    is the obvious candidate. `pg_try_advisory_lock` returns immediately
    and the context yields False when another worker already holds the
    lock, so the task body can skip itself instead of running twice.

    The lock sits on its own connection, separate from the task's
    sessions, so the task's commits cannot release it. A crashed worker
    drops the connection, and Postgres releases the session lock with it
    — no cleanup path to get wrong.
    """
    async with engine.connect() as conn:
        acquired = await conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"),
            {"name": name},
        )
        try:
            yield bool(acquired)
        finally:
            if acquired:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:name))"),
                    {"name": name},
                )


def beat_task(name: str) -> Callable:
    """Decorator: run a beat task's body only while it holds `name`'s lock.

    A skipped run returns None — beat tasks report counts, and a run that
    never started has nothing to count.
    """

    def decorator(fn: Callable[[], Awaitable[T]]) -> Callable[[], Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper() -> T | None:
            async with beat_lock(name) as acquired:
                if not acquired:
                    return None
                return await fn()

        return wrapper

    return decorator


def run_async(fn: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine from a sync Celery task.

    Each call gets its own event loop. Reusing a loop across tasks would let one
    task's cancelled await leak into the next, which is a genuinely miserable bug
    to trace and has no upside here — these tasks are short and never overlap
    within a process.
    """
    return asyncio.run(fn())
