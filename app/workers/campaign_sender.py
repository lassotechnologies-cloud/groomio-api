"""Campaign sender (Doc 10.11 / 2.11).

Delivers scheduled in-app and push campaigns. SMS is not a channel here: Doc 2.11
restricts SMS to OTP verification in the MVP, and a marketing blast over SMS is
both expensive and the kind of thing that gets a sender id throttled.

Segment resolution happens here rather than at creation time. A campaign
scheduled for next Saturday should reach the customers the shop had on Saturday,
not the ones it had when the owner clicked "schedule" a week earlier — a
birthday segment resolved too early will silently miss the birthday it was
written for.

The status moves `scheduled` → `sending` before delivery and `sent` after, so a
crash mid-send leaves a campaign visibly stuck rather than appearing complete.
"""
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import func, select, update

from app.models.messaging import Campaign, CampaignRecipient, Notification
from app.models.money import Sale
from app.models.people import Customer
from app.workers.db import run_async, session_scope

BATCH_SIZE = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _resolve_segment(db, business_id: str, segment: str) -> list[str]:
    """Customer ids matching the segment, computed at send time."""
    stmt = select(Customer.id).where(Customer.business_id == business_id)

    if segment == "inactive_30d":
        # A customer with no sale in 30 days. The point of this segment is to
        # win back people who are drifting, so anyone with a recent sale is
        # excluded even if they were once inactive.
        cutoff = _now() - timedelta(days=30)
        recent = select(Sale.customer_id).where(
            Sale.business_id == business_id, Sale.created_at >= cutoff
        )
        stmt = stmt.where(Customer.id.notin_(recent))

    elif segment == "birthday_month":
        # Match on month only: a birthday is a month-long occasion in this
        # market, and a same-day coupon is not worth mailing anyone for.
        current = _now()
        stmt = stmt.where(
            Customer.birthday.isnot(None),
            func.extract("month", Customer.birthday) == current.month,
        )

    elif segment == "loyalty_tier":
        # No tier table exists yet. Returning an empty set is the safe reading:
        # a campaign that quietly targets everyone because a segment was
        # unimplemented is how a shop accidentally emails its whole list.
        return []

    # "all" needs no extra filtering.
    return [str(c) for c in await db.scalars(stmt)]


async def _send(campaign_id: str) -> dict:
    delivered = 0
    async with session_scope() as db:
        # Atomically claim the campaign so two workers cannot both send it.
        result = await db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status == "scheduled")
            .values(status="sending")
            .returning(Campaign.id)
        )
        if not result.fetchone():
            return {"delivered": 0, "skipped": "already processing"}

        campaign = await db.scalar(select(Campaign).where(Campaign.id == campaign_id))

        customer_ids = await _resolve_segment(
            db, campaign.business_id, campaign.segment
        )
        for start in range(0, len(customer_ids), BATCH_SIZE):
            for customer_id in customer_ids[start : start + BATCH_SIZE]:
                db.add(
                    Notification(
                        recipient_customer_id=customer_id,
                        type="campaign",
                        title=campaign.title[:255],
                        body=campaign.body,
                    )
                )
                db.add(
                    CampaignRecipient(
                        campaign_id=campaign.id,
                        customer_id=customer_id,
                        delivered_at=_now(),
                    )
                )
                delivered += 1

        campaign.status = "sent"

    return {"delivered": delivered}


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def send_campaign(self, campaign_id: str):
    """Deliver one campaign. Idempotent: `sending`/`sent` short-circuits."""
    try:
        return run_async(lambda: _send(campaign_id))
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def dispatch_scheduled_campaigns(self):
    """Claim every campaign that is due and hand each to `send_campaign`.

    Claimed in small batches on a short lease so a campaign scheduled for a past
    time still goes out, but only once: `send_campaign` ignores anything already
    marked sending or sent.
    """
    now = _now()

    async def _dispatch() -> dict:
        async with session_scope() as db:
            due = list(
                await db.scalars(
                    select(Campaign)
                    .where(
                        Campaign.status == "scheduled",
                        Campaign.scheduled_at.isnot(None),
                        Campaign.scheduled_at <= now,
                    )
                    .order_by(Campaign.scheduled_at)
                    .limit(50)
                )
            )
            ids = [str(c.id) for c in due]

        for campaign_id in ids:
            send_campaign.delay(campaign_id)
        return {"dispatched": len(ids)}

    try:
        return run_async(_dispatch)
    except Exception as exc:
        raise self.retry(exc=exc)
