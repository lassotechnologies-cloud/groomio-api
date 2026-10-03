"""Commission routes — API group E2–E4 (Doc 10.6).

Earnings are *derived*, never typed in. `sale_items.commission_kes` is written
when a sale is recorded, and this router sums it per barber per period. That means
a barber's number can always be traced back to the individual sales that produced
it — an owner disputing a figure can see exactly what it is made of.

Percentages are converted to whole KES at sale time and rounded down, so the shop
never pays out a fraction of a shilling and never overpays on rounding.
"""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.tenancy import resolve_business_id, resolve_staff_for_user
from app.models.identity import User
from app.models.money import Commission, Sale, SaleItem
from app.models.people import Staff
from app.models.tenant import Branch
from app.staff.schemas import BonusUpdateRequest, CommissionResponse

router = APIRouter(prefix="/commissions", tags=["commissions"])

Db = Annotated[AsyncSession, Depends(get_db)]
OwnerOnly = Depends(require_role("owner"))


def _period(period: str, today: date) -> tuple[date, date]:
    """Resolve `?period=` into an inclusive start / exclusive end date range."""
    if period == "week":
        start = today - timedelta(days=today.weekday())  # Monday
        return start, start + timedelta(days=7)
    if period == "month":
        start = today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt
    if period == "year":
        start = today.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1)
    # "day" or anything unrecognised — never fail a dashboard over a typo.
    return today, today + timedelta(days=1)


async def _names(db: AsyncSession, staff_ids: set[str]) -> dict[str, str]:
    if not staff_ids:
        return {}
    stmt = (
        select(Staff.id, User.full_name)
        .join(User, User.id == Staff.user_id)
        .where(Staff.id.in_(staff_ids))
    )
    return {str(sid): name for sid, name in (await db.execute(stmt)).all()}


def _out(row: Commission, names: dict[str, str]) -> CommissionResponse:
    return CommissionResponse(
        id=str(row.id),
        staff_id=row.staff_id,
        staff_name=names.get(row.staff_id),
        branch_id=row.branch_id,
        period_start=row.period_start,
        period_end=row.period_end,
        earned_kes=row.earned_kes,
        bonus_kes=row.bonus_kes,
        status=row.status,
    )


# ── E2 — a barber's own earnings ─────────────────────────────────────────────
@router.get("/me", response_model=list[CommissionResponse])
async def my_commissions(
    db: Db,
    user: CurrentUser,
    period: str = Query("month", description="day | week | month | year"),
) -> list[CommissionResponse]:
    """BA2 — 'what did I earn'. Reads only the caller's own staff row."""
    me = await resolve_staff_for_user(db, user)
    today = date.today()
    start, end = _period(period, today)

    rows = list(
        await db.scalars(
            select(Commission)
            .where(Commission.staff_id == me.id)
            .order_by(Commission.period_start.desc())
        )
    )
    names = {me.id: None}
    return [_out(r, names) for r in rows]


@router.get("/me/live", response_model=dict)
async def my_live_earnings(
    db: Db,
    user: CurrentUser,
    period: str = Query("month", description="day | week | month | year"),
) -> dict:
    """Commission earned in the current period, summed straight from sale items.

    The stored `commissions` row is only written by the payroll worker, so a
    barber asking "what have I made this month" should not wait for a batch job.
    """
    me = await resolve_staff_for_user(db, user)
    start, end = _period(period, date.today())

    # sale_items carry no staff_id — the barber is on the parent sale, so join
    # through it rather than guessing at a column that is not there.
    total = await db.scalar(
        select(func.coalesce(func.sum(SaleItem.commission_kes), 0))
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(
            Sale.staff_id == me.id,
            Sale.created_at >= start,
            Sale.created_at < end,
        )
    )
    return {
        "staff_id": str(me.id),
        "commission_type": me.commission_type,
        "commission_value": me.commission_value,
        "period": period,
        "earned_kes": int(total or 0),
        "currency": "KES",
    }


# ── E3 — all barbers, owner view ─────────────────────────────────────────────
@router.get(
    "/branches/{branch_id}/commissions",
    response_model=list[CommissionResponse],
    dependencies=[OwnerOnly],
)
async def branch_commissions(
    branch_id: str, db: Db, user: CurrentUser
) -> list[CommissionResponse]:
    """OW1 — every barber at one branch, with bonuses."""
    business_id = await resolve_business_id(db, user)

    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )

    rows = list(
        await db.scalars(
            select(Commission)
            .where(Commission.branch_id == branch_id)
            .order_by(Commission.period_start.desc(), Commission.earned_kes.desc())
        )
    )
    names = await _names(db, {r.staff_id for r in rows})
    return [_out(r, names) for r in rows]


# ── E4 — set an incentive bonus ──────────────────────────────────────────────
@router.patch(
    "/{commission_id}/bonus",
    response_model=CommissionResponse,
    dependencies=[OwnerOnly],
)
async def set_bonus(
    commission_id: str, payload: BonusUpdateRequest, db: Db, user: CurrentUser
) -> CommissionResponse:
    """Owner sets a bonus. Blocked once the period is marked paid — payroll is final."""
    business_id = await resolve_business_id(db, user)

    staff_ids = {
        s.id
        for s in await db.scalars(select(Staff).where(Staff.business_id == business_id))
    }
    row = await db.scalar(select(Commission).where(Commission.id == commission_id))
    if not row or row.staff_id not in staff_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if row.status == "paid":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This commission period is already paid",
        )

    row.bonus_kes = payload.bonus_kes
    names = await _names(db, {row.staff_id})
    return _out(row, names)
