"""Advisory-lock guard for beat tasks (T004-25).

Beat can fire a task twice — a failed-over beat process, or a run that
lasts past its next scheduled slot. `beat_lock` is the guard: a
session-level `pg_try_advisory_lock` keyed by the task name. These
tests prove the two properties that make the guard trustworthy:

  - exclusivity: a second holder of the same name is told "no"
    immediately, rather than queueing behind the first
  - release: the lock is gone once the context exits, so the next
    scheduled run is not permanently locked out

They run against the real test database (the `real_beat_lock` marker
opts out of the conftest fixture that neutralizes the lock for tests
that call worker bodies directly), but through an engine bound to the
test database — `beat_lock` normally connects through the app engine,
which points at the development database.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.workers.db import beat_lock


@pytest.mark.real_beat_lock
async def test_beat_lock_is_exclusive(test_database_url, monkeypatch):
    import app.workers.db as worker_db

    engine = create_async_engine(test_database_url)
    monkeypatch.setattr(worker_db, "engine", engine)
    try:
        async with beat_lock("test-lock-exclusivity") as first:
            assert first is True
            # A second holder, on its own connection, is refused at
            # once — `pg_try_advisory_lock` never waits.
            async with beat_lock("test-lock-exclusivity") as second:
                assert second is False
        # Exiting the context released the lock: the next run takes it.
        async with beat_lock("test-lock-exclusivity") as third:
            assert third is True
    finally:
        await engine.dispose()


@pytest.mark.real_beat_lock
async def test_beat_lock_names_are_independent(test_database_url, monkeypatch):
    import app.workers.db as worker_db

    engine = create_async_engine(test_database_url)
    monkeypatch.setattr(worker_db, "engine", engine)
    try:
        # Two differently-named tasks hold their locks at once — the
        # reconciler running must not block the daily report.
        async with beat_lock("test-lock-a"):
            async with beat_lock("test-lock-b") as second:
                assert second is True
    finally:
        await engine.dispose()
