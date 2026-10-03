"""Daily report worker (Doc 10.11, beat daily 21:30 EAT).

Computes yesterday's numbers for every business and drops a summary in the owner's
inbox. 21:30 is late enough that the day is mostly done and early enough that an
owner opening the app the next morning sees it before opening the shop.

The figures are the same ones `GET /reports/daily` returns, computed here at
scheduled time rather than on request. Two reasons: the dashboard should not
cost 40 aggregate queries per page load, and a shop owner who opens the app at
6am should not wait on a report.
"""

from datetime import date, datetime, time, timedelta, timezone

from celery import shared_task
from sqlalchemy import func, select

from app.models.identity import User
from app.models.money import Expense, Sale
from app.models.operations import Appointment
from app.models.people import Customer
from app.models.tenant import Business
from app.workers.db import run_async, session_scope
from app.workers.notifications import notify_once


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _compute() -> dict:
    # Yesterday in UTC. The shop's own timezone is a per-business field we do not
    # yet model, so UTC is used consistently rather than half-right per shop.
    now = _now()
    target = (now - timedelta(days=1)).date()
    start = datetime.combine(target, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    businesses_reported = 0
    quiet_shops = 0

    async with session_scope() as db:
        businesses = list(await db.scalars(select(Business)))

        for biz in businesses:
            branch_ids = [
                str(b)
                for b in await db.scalars(
                    select(Appointment.branch_id)
                    .where(Appointment.business_id == str(biz.id))
                    .distinct()
                )
            ]
            if not branch_ids:
                continue

            revenue, sales_count, customers = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(Sale.total_kes), 0),
                        func.count(Sale.id),
                        func.count(func.distinct(Sale.customer_id)),
                    ).where(
                        Sale.branch_id.in_(branch_ids),
                        Sale.created_at >= start,
                        Sale.created_at < end,
                    )
                )
            ).one()

            expenses = await db.scalar(
                select(func.coalesce(func.sum(Expense.amount_kes), 0)).where(
                    Expense.branch_id.in_(branch_ids),
                    Expense.incurred_at == target,
                )
            )

            revenue_kes, expenses_kes = int(revenue or 0), int(expenses or 0)
            count = int(sales_count or 0)

            # A day with no sales at all gets no notification. Telling an owner
            # "you made KES 0 yesterday" at 9pm is discouraging and, for a shop
            # that is simply closed on Sundays, wrong every single week.
            if count == 0:
                quiet_shops += 1
                continue

            average = revenue_kes // count
            body = (
                f"{target.isoformat()}: KES {revenue_kes:,} from {count} "
                f"service{'s' if count != 1 else ''}, {int(customers or 0)} "
                f"customer{'s' if int(customers or 0) != 1 else ''}, "
                f"KES {expenses_kes:,} costs, KES {revenue_kes - expenses_kes:,} "
                f"profit. Average ticket KES {average:,}."
            )

            created = await notify_once(
                db,
                # `report`, not `campaign`: a daily takings summary is
                # operational, and an inbox that files it under promotions
                # cannot be filtered by the owner reading it.
                type="report",
                title=f"Your day on {target.isoformat()}",
                body=body,
                user_id=biz.owner_user_id,
            )
            if created:
                businesses_reported += 1

    return {
        "date": target.isoformat(),
        "reported": businesses_reported,
        "skipped_no_sales": quiet_shops,
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=600)
def compute_daily_report(self):
    """Daily at 21:30 EAT for yesterday. Idempotent through `notify_once`."""
    try:
        return run_async(_compute)
    except Exception as exc:
        raise self.retry(exc=exc)
