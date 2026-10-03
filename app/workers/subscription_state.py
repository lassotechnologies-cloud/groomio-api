"""Subscription lifecycle worker (Doc 10.11, beat daily 00:05 EAT).

Moves subscriptions through their end-of-period states and keeps `businesses`
in step. This is the worker that decides a shop loses access, so it is written to
be boring and idempotent — running it twice changes nothing.

The state transitions:

    trial/active  → grace   on the day after the period ends (3-day grace)
    grace         → expired once grace is used up

Two deliberate choices:

  - The transition is driven by comparing `current_period_end` to now, never by a
    stored "has this run" flag. A missed run means the transition happens late,
    not never — a shop can always be a day late, never a day immortal.

  - `expired` is not `suspended`. A lapsed subscription is a billing state that
    payment fixes; a suspension is a support decision that payment does not.
"""

from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select

from app.core.config import settings
from app.models.billing import Subscription
from app.models.tenant import Business
from app.workers.db import run_async, session_scope


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _advance() -> dict:
    now = _now()
    grace_cutoff = now - timedelta(days=settings.grace_days)

    moved_to_grace = 0
    moved_to_expired = 0

    async with session_scope() as db:
        # Due for grace: period ended, still in a paid-or-trial state, and the
        # grace window is not already spent.
        due_for_grace = list(
            await db.scalars(
                select(Subscription).where(
                    Subscription.status.in_(("trial", "active")),
                    Subscription.current_period_end <= now,
                    Subscription.current_period_end > grace_cutoff,
                )
            )
        )
        expired_business_ids: list[str] = []

        for sub in due_for_grace:
            sub.status = "grace"
            moved_to_grace += 1
            if sub.business_id not in expired_business_ids:
                expired_business_ids.append(sub.business_id)

        # Out of grace entirely.
        out_of_grace = list(
            await db.scalars(
                select(Subscription).where(
                    Subscription.status == "grace",
                    Subscription.current_period_end <= grace_cutoff,
                )
            )
        )
        for sub in out_of_grace:
            sub.status = "expired"
            moved_to_expired += 1
            if sub.business_id not in expired_business_ids:
                expired_business_ids.append(sub.business_id)

        # Mirror onto the business. Only downgrade — a business already marked
        # `suspended` by support stays suspended even if its subscription is
        # still live, because someone made a deliberate decision about it.
        fully_expired = {s.business_id for s in out_of_grace}
        for business_id in expired_business_ids:
            biz = await db.scalar(select(Business).where(Business.id == business_id))
            if biz and biz.status in ("trialing", "active", "grace"):
                biz.status = "expired" if business_id in fully_expired else "grace"

    return {"to_grace": moved_to_grace, "to_expired": moved_to_expired}


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def expire_subscriptions(self):
    """Daily sweep. Idempotent: a subscription already in the target state is not
    selected, so a retry after a partial failure cannot double-advance anything."""
    try:
        return run_async(_advance)
    except Exception as exc:
        raise self.retry(exc=exc)
