"""Chat retention cleanup (Doc 2.14 / 11.4, beat weekly Monday 02:00).

Kenya's Data Protection Act gives a data subject the right to erasure, and Doc
2.14 sets chat retention at 12 months. This archives threads past that window.

It archives rather than deletes. A chat between a shop and its customer is
evidence of what was agreed — a price quoted, a service booked, a complaint — and
a dispute over a bad haircut is exactly when someone needs to read it. Retention
here means "no longer surfaced in the product", not "destroyed".

Deleting a customer's PII on request is a separate, explicit operation, not this
job's responsibility.
"""
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select

from app.core.config import settings
from app.models.messaging import MessageThread
from app.workers.db import run_async, session_scope

# Threads are archived in batches so one run cannot hold a long transaction open
# over a large backlog and time out against the pooler.
BATCH_SIZE = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _archive() -> dict:
    cutoff = _now() - timedelta(days=settings.chat_retention_months * 30)

    archived = 0
    async with session_scope() as db:
        while True:
            stale = list(
                await db.scalars(
                    select(MessageThread)
                    .where(
                        MessageThread.status == "open",
                        MessageThread.updated_at < cutoff,
                    )
                    .order_by(MessageThread.updated_at)
                    .limit(BATCH_SIZE)
                )
            )
            if not stale:
                break

            for thread in stale:
                thread.status = "archived"
            archived += len(stale)

            if len(stale) < BATCH_SIZE:
                break

    return {"archived": archived, "cutoff": cutoff.isoformat()}


@shared_task(bind=True, max_retries=3, default_retry_delay=1800)
def archive_old_threads(self):
    """Weekly. Idempotent: archived threads are no longer selected."""
    try:
        return run_async(_archive)
    except Exception as exc:
        raise self.retry(exc=exc)
