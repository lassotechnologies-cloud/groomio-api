"""Appointment reminder worker (Doc 10.11, beat every 15 minutes).

Tells customers their booking is coming up. The window is one hour, re-checked
every 15 minutes, which means the same appointment is selected about four times —
so `notify_once` is doing real work here, not decoration.

SMS is deliberately not used. Doc 2.11 restricts SMS in the MVP to OTP
verification; everything customer-facing goes in-app or push. A shop that
reminds by SMS in the MVP is a shop burning money on every message.
"""
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select

from app.models.operations import Appointment
from app.models.people import Customer
from app.workers.db import run_async, session_scope
from app.workers.notifications import notify_once

# How far ahead to remind. One hour is long enough to be useful and short enough
# that a no-show is the customer's own fault rather than a surprise.
REMINDER_WINDOW_MIN = 60
REMINDER_INTERVAL_MIN = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _remind() -> dict:
    now = _now()
    start = now + timedelta(minutes=REMINDER_WINDOW_MIN - REMINDER_INTERVAL_MIN)
    end = now + timedelta(minutes=REMINDER_WINDOW_MIN)

    sent = 0
    async with session_scope() as db:
        appointments = list(
            await db.scalars(
                select(Appointment)
                .where(
                    Appointment.status.in_(("booked", "confirmed")),
                    Appointment.scheduled_at >= start,
                    Appointment.scheduled_at < end,
                )
                .order_by(Appointment.scheduled_at)
                .limit(500)
            )
        )

        for appointment in appointments:
            customer = await db.scalar(
                select(Customer).where(Customer.id == appointment.customer_id)
            )
            if not customer:
                # A booking whose customer profile was deleted. Nothing to
                # remind, and nothing to fix automatically.
                continue

            minutes = int((appointment.scheduled_at - now).total_seconds() // 60)
            created = await notify_once(
                db,
                type="appointment",
                title="Appointment coming up",
                body=(
                    f"Hi {customer.full_name.split()[0]}, your appointment is in "
                    f"about {minutes} minutes."
                ),
                customer_id=customer.id,
            )
            if created:
                sent += 1

    return {"reminded": sent, "window_start": start.isoformat()}


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_appointment_reminders(self):
    """Every 15 minutes. Idempotent through `notify_once`."""
    try:
        return run_async(_remind)
    except Exception as exc:
        raise self.retry(exc=exc)
