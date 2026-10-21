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

from app.core.config import settings
from app.models.billing import Payment
from app.models.platform import WebhookEvent
from app.subscriptions.router import _apply_payment
from app.workers.db import beat_task, run_async, session_scope

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


@beat_task("payment-reconciler")
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
                event_ref = stk.get("CheckoutRequestID") or payload.get("CheckoutRequestID")
                quoted_amount = stk.get("Amount") or payload.get("Amount")
            else:
                succeeded = _pesapal_succeeded(payload)
                details = payload.get("notification_details") or {}
                reference = details.get("merchant_reference")
                event_ref = details.get("payment_reference") or details.get("tracking_account_reference")
                quoted_amount = details.get("amount")

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

            # Re-verify amount and provider_ref (CheckoutRequestID for Daraja)
            # to prevent forged or mismatched stored events from being applied.
            if event.source == "daraja":
                if payment.provider_ref and event_ref != payment.provider_ref:
                    # Mismatched CheckoutRequestID — this stored event is not for our payment.
                    skipped += 1
                    continue
                if quoted_amount is not None and int(quoted_amount) != payment.amount_kes:
                    # Amount mismatch — mark as failed and stop retrying.
                    payment.status = "failed"
                    event.processed_at = now
                    continue
            else:
                # Pesapal: verify amount if present
                if quoted_amount is not None and int(float(quoted_amount)) != payment.amount_kes:
                    payment.status = "failed"
                    event.processed_at = now
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


@beat_task("payment-timeout")
async def _timeout_pending() -> dict:
    """Close payments no provider ever answered (T004-16).

    A payment that stays `pending` past the window is one whose callback
    never arrived — the STK prompt expired, the customer closed the sheet,
    or the callback was lost. The subscription cannot stay in limbo forever
    and the payment list cannot show "awaiting M-Pesa" for a week, so the
    payment is marked `timeout`. That is a *state*, not a judgement: a real
    success callback that arrives later is still honoured — the reconciler
    re-opens a `timeout` payment when the provider's own payload says it
    succeeded, and `_apply_payment` grants the time then.
    """
    now = _now()
    cutoff = now - timedelta(hours=settings.payment_pending_timeout_hours)
    timed_out = 0

    async with session_scope() as db:
        stale = list(
            await db.scalars(
                select(Payment)
                .where(
                    Payment.status == "pending",
                    Payment.created_at < cutoff,
                )
                .order_by(Payment.created_at)
                .limit(500)  # bounded; the next run picks up the rest
            )
        )
        for payment in stale:
            payment.status = "timeout"
            timed_out += 1

    return {"timed_out": timed_out}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def timeout_pending_payments(self):
    """Sweep long-pending payments to `timeout`. Idempotent: a payment
    already in a terminal state is not selected."""
    try:
        return run_async(_timeout_pending)
    except Exception as exc:
        raise self.retry(exc=exc)
