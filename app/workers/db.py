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
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from app.core.database import AsyncSessionLocal

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


def run_async(fn: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine from a sync Celery task.

    Each call gets its own event loop. Reusing a loop across tasks would let one
    task's cancelled await leak into the next, which is a genuinely miserable bug
    to trace and has no upside here — these tasks are short and never overlap
    within a process.
    """
    return asyncio.run(fn())
