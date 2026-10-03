"""Webhook reconciler (Doc 10.11, beat every 5 minutes).

The safety net under the payment webhooks. A callback can fail to process for
reasons that have nothing to do with whether the customer paid: a deploy lands
mid-request, the worker pool is saturated, a transient database blip. When that
happens the raw payload is already in `webhook_events` with `processed_at` null,
and this task picks it up.

That ordering is why the webhooks store before they decide. Without it there is
no record of a payment that arrived and was lost, and an owner disputing a
charge has nothing to show support.

This is deliberately narrow. It re-processes stored events that are *structurally
valid but not yet applied*. It does not invent payment successes from nothing and
does not retry events that failed signature verification — those stay unprocessed
forever as evidence, and a human decides.
"""
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select

from app.models.billing import Payment
from app.models.platform import WebhookEvent
from app.subscriptions.router import _apply_payment
from app.workers.db import run_async, session_scope

# Anything older than this is left alone. A month-old unprocessed event is a bug
# to investigate, not something to silently apply 30 days late.
RECONCILE_WINDOW_DAYS = 30

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _daraja_succeeded(payload: dict) -> bool:
    stk = (payload.get("Body") or {}).get("stkCallback") or payload
    return str(stk.get("ResultCode", "")) == "0"


def _pesapal_succeeded(payload: dict) -> bool:
    details = payload.get("notification_details") or {}
    return str(details.get("status", "")).lower() == "completed"


async def _reconcile() -> dict:
    now = _now()
    floor = now - timedelta(days=RECONCILE_WINDOW_DAYS)
    applied = 0
    skipped = 0

    async with session_scope() as db:
        pending = list(
            await db.scalars(
                select(WebhookEvent)
                .where(
                    WebhookEvent.processed_at.is_(None),
                    WebhookEvent.created_at >= floor,
                )
                .order_by(WebhookEvent.created_at)
                .limit(200)  # bounded: this runs every 5 minutes and can catch up
            )
        )

        for event in pending:
            payload = event.payload or {}

            if event.source == "daraja":
                succeeded = _daraja_succeeded(payload)
                stk = (payload.get("Body") or {}).get("stkCallback") or payload
                reference = stk.get("AccountReference")
            else:
                succeeded = _pesapal_succeeded(payload)
                details = payload.get("notification_details") or {}
                reference = details.get("merchant_reference")

            payment = None
            if reference:
                payment = await db.scalar(
                    select(Payment).where(Payment.id == str(reference))
                )

            if not payment:
                # A callback naming no payment we issued. Left unprocessed on
                # purpose: it is either garbage or a payment for a deleted
                # business, and applying it would be guessing.
                skipped += 1
                continue

            if not succeeded:
                # The provider told us it failed. Record that and stop retrying —
                # re-reading a failure every five minutes forever helps nobody.
                payment.status = "failed"
                event.processed_at = now
                continue

            if payment.status == "success":
                # Already applied; this is a duplicate delivery. Mark and move on.
                event.processed_at = now
                continue

            if payment.status in ("failed", "timeout"):
                # A real success arriving after a timeout or an earlier failure.
                # `_apply_payment` is deliberately fail-closed — it applies only a
                # `pending` payment — so without this reset a customer whose money
                # arrived late would be refused here and again by the guard. This
                # is the one place allowed to re-open a closed payment, because the
                # stored payload is the provider's own verified word that it
                # succeeded. Dropping it would be losing a real KES 1,000.
                payment.status = "pending"

            await _apply_payment(db, payment)
            event.processed_at = now
            applied += 1

    return {"applied": applied, "skipped": skipped}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def reconcile_webhooks(self):
    """Drain the unprocessed-event queue. Safe to run concurrently with itself:
    every step is a guarded state change, and `_apply_payment` applies only a
    `pending` payment exactly once."""
    try:
        return run_async(_reconcile)
    except Exception as exc:
        raise self.retry(exc=exc)
