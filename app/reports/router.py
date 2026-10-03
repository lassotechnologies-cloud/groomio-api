"""Report routes — API group F4, F5 (Doc 10.7).

OW2: an owner wants one screen that answers "how did today go" without a
spreadsheet. The numbers here are computed from `sales` and `expenses` directly
rather than a summary table, because a cached summary that disagrees with the
underlying sales is worse than a slightly slower query — an owner who finds one
disagrees with every number in the app afterwards.

All figures are KES integers. No floats: money in a shilling-denominated ledger
should never carry a fractional shilling.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.tenancy import resolve_business_id
from app.models.identity import User
from app.models.money import Expense, Sale, SaleItem
from app.models.people import Staff
from app.models.tenant import Branch
from app.sales.schemas import (
    BarberPerformance,
    BranchComparison,
    DailyReportResponse,
    PeriodReportResponse,
)

router = APIRouter(prefix="/reports", tags=["reports"])

Db = Annotated[AsyncSession, Depends(get_db)]
OwnerOnly = Depends(require_role("owner"))


async def _branch_ids(db: AsyncSession, business_id: str) -> list[str]:
    return [
        b.id
        for b in await db.scalars(
            select(Branch).where(Branch.business_id == business_id)
        )
    ]


def _window(period: str, today: date) -> tuple[date, date, date]:
    """(from, to_exclusive, report_label). Never raises on a bad period string."""
    if period == "weekly":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7), "week"
    if period == "monthly":
        start = today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt, "month"
    return today, today + timedelta(days=1), "day"


async def _barber_performance(
    db: AsyncSession, branch_ids: list[str], start: datetime, end: datetime
) -> list[BarberPerformance]:
    """Revenue and commission per barber, assembled from two grouped queries."""
    revenue = await db.execute(
        select(
            Sale.staff_id,
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total_kes), 0),
        )
        .where(
            Sale.branch_id.in_(branch_ids),
            Sale.created_at >= start,
            Sale.created_at < end,
        )
        .group_by(Sale.staff_id)
    )
    commission = await db.execute(
        select(
            Sale.staff_id,
            func.coalesce(func.sum(SaleItem.commission_kes), 0),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(
            Sale.branch_id.in_(branch_ids),
            Sale.created_at >= start,
            Sale.created_at < end,
        )
        .group_by(Sale.staff_id)
    )

    counts = {sid: (int(c), int(rev)) for sid, c, rev in revenue.all()}
    comm = {sid: int(v) for sid, v in commission.all()}

    staff_ids = set(counts) | set(comm)
    if not staff_ids:
        return []

    stmt = (
        select(Staff.id, User.full_name)
        .join(User, User.id == Staff.user_id)
        .where(Staff.id.in_(staff_ids))
    )
    names = {str(sid): name for sid, name in (await db.execute(stmt)).all()}

    return sorted(
        (
            BarberPerformance(
                staff_id=sid,
                staff_name=names.get(sid),
                sales_count=counts.get(sid, (0, 0))[0],
                revenue_kes=counts.get(sid, (0, 0))[1],
                commission_kes=comm.get(sid, 0),
            )
            for sid in staff_ids
        ),
        key=lambda b: b.revenue_kes,
        reverse=True,
    )


# ── F4 — daily report (OW2) ─────────────────────────────────────────────────
@router.get("/daily", response_model=DailyReportResponse, dependencies=[OwnerOnly])
async def daily_report(
    db: Db,
    user: CurrentUser,
    on_date: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    branch_id: Optional[str] = Query(None),
) -> DailyReportResponse:
    """One-tap day view: customers, revenue, expenses, profit, top barber."""
    business_id = await resolve_business_id(db, user)

    all_branches = await _branch_ids(db, business_id)
    if not all_branches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No branches yet"
        )

    if branch_id:
        if branch_id not in all_branches:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
            )
        scope = [branch_id]
    else:
        scope = all_branches

    if on_date:
        try:
            day = date.fromisoformat(on_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date must be YYYY-MM-DD",
            ) from None
    else:
        day = date.today()

    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    revenue, sales_count, customers = (
        await db.execute(
            select(
                func.coalesce(func.sum(Sale.total_kes), 0),
                func.count(Sale.id),
                func.count(func.distinct(Sale.customer_id)),
            ).where(
                Sale.branch_id.in_(scope),
                Sale.created_at >= start,
                Sale.created_at < end,
            )
        )
    ).one()

    expenses = await db.scalar(
        select(func.coalesce(func.sum(Expense.amount_kes), 0)).where(
            Expense.branch_id.in_(scope),
            Expense.incurred_at == day,
        )
    )

    barbers = await _barber_performance(db, scope, start, end)
    revenue_kes, expenses_kes = int(revenue or 0), int(expenses or 0)

    return DailyReportResponse(
        branch_id=branch_id,
        on_date=day,
        # distinct customer_id excludes walk-ins with no profile, which is the
        # right reading of "customers served" for a shop that records profiles.
        customers_served=int(customers or 0),
        sales_count=int(sales_count or 0),
        revenue_kes=revenue_kes,
        expenses_kes=expenses_kes,
        profit_kes=revenue_kes - expenses_kes,
        top_barber=barbers[0] if barbers else None,
        barbers=barbers,
    )


# ── F5 — weekly / monthly aggregates + branch comparison ─────────────────────
# Two explicit paths rather than "/{period}": a catch-all would also capture
# /reports/daily and shadow the F4 endpoint above.
@router.get("/weekly", response_model=PeriodReportResponse, dependencies=[OwnerOnly])
async def weekly_report(
    db: Db, user: CurrentUser, branch_id: Optional[str] = Query(None)
) -> PeriodReportResponse:
    """F5 — this week's aggregates, optionally for one branch."""
    return await _period_report("weekly", db, user, branch_id)


@router.get("/monthly", response_model=PeriodReportResponse, dependencies=[OwnerOnly])
async def monthly_report(
    db: Db, user: CurrentUser, branch_id: Optional[str] = Query(None)
) -> PeriodReportResponse:
    """F5 — this month's aggregates, optionally for one branch."""
    return await _period_report("monthly", db, user, branch_id)


async def _period_report(
    period: str,
    db: AsyncSession,
    user: dict,
    branch_id: Optional[str],
) -> PeriodReportResponse:
    business_id = await resolve_business_id(db, user)
    all_branches = await _branch_ids(db, business_id)
    if not all_branches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No branches yet"
        )

    if branch_id:
        if branch_id not in all_branches:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
            )
        scope = [branch_id]
    else:
        scope = all_branches

    from_date, to_date, _label = _window(period, date.today())
    start = datetime(
        from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc
    )
    end = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc)

    revenue, sales_count, customers = (
        await db.execute(
            select(
                func.coalesce(func.sum(Sale.total_kes), 0),
                func.count(Sale.id),
                func.count(func.distinct(Sale.customer_id)),
            ).where(
                Sale.branch_id.in_(scope),
                Sale.created_at >= start,
                Sale.created_at < end,
            )
        )
    ).one()

    expenses = await db.scalar(
        select(func.coalesce(func.sum(Expense.amount_kes), 0)).where(
            Expense.branch_id.in_(scope),
            Expense.incurred_at >= from_date,
            Expense.incurred_at < to_date,
        )
    )

    revenue_kes, expenses_kes = int(revenue or 0), int(expenses or 0)
    count = int(sales_count or 0)

    return PeriodReportResponse(
        period=period,
        branch_id=branch_id,
        from_date=from_date,
        to_date=to_date - timedelta(days=1),
        customers_served=int(customers or 0),
        sales_count=count,
        revenue_kes=revenue_kes,
        expenses_kes=expenses_kes,
        profit_kes=revenue_kes - expenses_kes,
        average_sale_kes=revenue_kes // count if count else 0,
        barbers=await _barber_performance(db, scope, start, end),
    )


# ── F5 — branch comparison (OW3 multi-branch) ───────────────────────────────
@router.get(
    "/branches/compare", response_model=list[BranchComparison], dependencies=[OwnerOnly]
)
async def compare_branches(
    db: Db,
    user: CurrentUser,
    period: str = Query("monthly", pattern="^(weekly|monthly)$"),
) -> list[BranchComparison]:
    """Side-by-side branch performance for an owner running more than one shop."""
    business_id = await resolve_business_id(db, user)
    branches = list(
        await db.scalars(select(Branch).where(Branch.business_id == business_id))
    )
    if not branches:
        return []

    from_date, to_date, _ = _window(period, date.today())
    start = datetime(
        from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc
    )
    end = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc)

    out: list[BranchComparison] = []
    for branch in branches:
        revenue, count = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Sale.total_kes), 0),
                    func.count(Sale.id),
                ).where(
                    Sale.branch_id == branch.id,
                    Sale.created_at >= start,
                    Sale.created_at < end,
                )
            )
        ).one()

        expenses = await db.scalar(
            select(func.coalesce(func.sum(Expense.amount_kes), 0)).where(
                Expense.branch_id == branch.id,
                Expense.incurred_at >= from_date,
                Expense.incurred_at < to_date,
            )
        )

        rev, exp = int(revenue or 0), int(expenses or 0)
        out.append(
            BranchComparison(
                branch_id=branch.id,
                branch_name=branch.name,
                sales_count=int(count or 0),
                revenue_kes=rev,
                expenses_kes=exp,
                profit_kes=rev - exp,
            )
        )

    return sorted(out, key=lambda b: b.revenue_kes, reverse=True)
