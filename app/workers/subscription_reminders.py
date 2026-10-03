"""Subscription reminder worker (Doc 10.11, beat daily 08:00 EAT).

Two reminders, both to the owner:

  - trial ending      on the days in `REMIND_TRIAL_DAYS` (7, 9, 10)
  - renewal upcoming  `REMIND_BEFORE_RENEWAL_DAYS` before the period ends

The trial ladder is the point. A shop that hears about the trial ending once, on
the day it ends, has no chance to pay; a shop told on day 3, day 1, and on the day
has three chances. Which is also why these are in-app notifications and not a
single email — an owner in a busy barbershop opens the app, they do not read mail.

The day-ladder is compared by calendar date rather than by elapsed hours, so a
task that was down for a day still fires for the days it missed that are still in
the future.
"""

from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select

from app.core.config import settings
from app.models.billing import Plan, Subscription
from app.models.tenant import Business
from app.workers.db import run_async, session_scope
from app.workers.notifications import notify_once


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def _remind() -> dict:
    now = _now()
    today = _start_of_day(now)

    renewal_notices = 0
    trial_sent = 0

    async with session_scope() as db:
        # ── trial countdown ──────────────────────────────────────────────────
        for days_left in settings.remind_trial_days_list:
            # A shop with 7 trial days left gets a notice on the day that is
            # `days_left` before the trial ends. "Days left" is inclusive of
            # today, so a trial ending in 7 days reminds when 7 remain.
            target_end = today + timedelta(days=days_left)
            trials = list(
                await db.scalars(
                    select(Subscription).where(
                        Subscription.status == "trial",
                        Subscription.current_period_end >= target_end,
                        Subscription.current_period_end
                        < target_end + timedelta(days=1),
                    )
                )
            )
            for sub in trials:
                biz = await db.scalar(
                    select(Business).where(Business.id == sub.business_id)
                )
                if not biz:
                    continue
                created = await notify_once(
                    db,
                    type="subscription",
                    title=f"Trial ending in {days_left} days",
                    body=(
                        f"Your Groomio trial ends in {days_left} "
                        f"day{'s' if days_left != 1 else ''}. Choose a plan to "
                        f"keep your shop running."
                    ),
                    user_id=biz.owner_user_id,
                )
                if created:
                    trial_sent += 1

        # ── renewal approaching ─────────────────────────────────────────────
        horizon = now + timedelta(days=settings.remind_before_renewal_days)
        renewing = list(
            await db.scalars(
                select(Subscription).where(
                    Subscription.status == "active",
                    Subscription.current_period_end > now,
                    Subscription.current_period_end <= horizon,
                )
            )
        )
        for sub in renewing:
            biz = await db.scalar(
                select(Business).where(Business.id == sub.business_id)
            )
            if not biz:
                continue
            plan = await db.scalar(select(Plan).where(Plan.id == sub.plan_id))
            plan_name = plan.code if plan else "your"
            days = max((sub.current_period_end - now).days, 0)
            created = await notify_once(
                db,
                type="subscription",
                title="Your plan renews soon",
                body=(
                    f"Your {plan_name} plan renews in {days} "
                    f"day{'s' if days != 1 else ''}. No action needed if you'd like "
                    f"to keep it."
                ),
                user_id=biz.owner_user_id,
            )
            if created:
                renewal_notices += 1

    return {"trial_reminders": trial_sent, "renewal_reminders": renewal_notices}


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def send_subscription_reminders(self):
    """Daily at 08:00 EAT. Idempotent through `notify_once`."""
    try:
        return run_async(_remind)
    except Exception as exc:
        raise self.retry(exc=exc)
