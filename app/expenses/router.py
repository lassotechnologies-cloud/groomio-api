"""Expense routes — API group F3 (Doc 10.7).

Per Doc 2.8 a clerk may only record expenses if the owner has granted permission.
That grant has no column in the schema, so the rule is expressed as an explicit
role gate here: owner always, clerk never, and widening it later means adding one
permission flag rather than hunting for the check.
"""

from datetime import date, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.security import now_utc
from app.core.tenancy import resolve_business_id
from app.models.money import Expense
from app.models.tenant import Branch
from app.sales.schemas import ExpenseCreateRequest, ExpenseResponse

router = APIRouter(tags=["expenses"])

Db = Annotated[AsyncSession, Depends(get_db)]
# Doc 2.8: clerks record expenses only with owner-granted permission; the MVP
# ships owner-only so the shop's books start out trustworthy.
OwnerOnly = Depends(require_role("owner"))


def _out(row: Expense) -> ExpenseResponse:
    return ExpenseResponse(
        id=str(row.id),
        branch_id=row.branch_id,
        category=row.category,
        amount_kes=row.amount_kes,
        note=row.note,
        incurred_at=row.incurred_at,
        recorded_by=row.recorded_by,
    )


@router.post(
    "/expenses",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def create_expense(
    payload: ExpenseCreateRequest, db: Db, user: CurrentUser
) -> ExpenseResponse:
    """F3 — money out: rent, stock, utilities, salaries, other."""
    business_id = await resolve_business_id(db, user)

    branch = await db.scalar(
        select(Branch).where(
            Branch.id == payload.branch_id, Branch.business_id == business_id
        )
    )
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    # A backdated expense is legitimate, but a future-dated one is a typo waiting
    # to distort next month's report.
    incurred = payload.incurred_at or now_utc().date()
    if incurred > now_utc().date():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="incurred_at cannot be future"
        )

    row = Expense(
        branch_id=payload.branch_id,
        category=payload.category,
        amount_kes=payload.amount_kes,
        note=payload.note,
        incurred_at=incurred,
        recorded_by=user["user_id"],
    )
    db.add(row)
    await db.flush()
    return _out(row)


@router.get("/expenses", response_model=list[ExpenseResponse], dependencies=[OwnerOnly])
async def list_expenses(
    db: Db,
    user: CurrentUser,
    on_date: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    branch_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
) -> list[ExpenseResponse]:
    """Expenses over a trailing window, newest first."""
    business_id = await resolve_business_id(db, user)

    branch_ids = [
        b.id
        for b in await db.scalars(select(Branch).where(Branch.business_id == business_id))
    ]
    if not branch_ids:
        return []

    if on_date:
        try:
            day = date.fromisoformat(on_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date must be YYYY-MM-DD",
            ) from None
        start, end = day, day + timedelta(days=1)
    else:
        end = now_utc().date() + timedelta(days=1)
        start = end - timedelta(days=days)

    stmt = select(Expense).where(
        Expense.branch_id.in_(branch_ids),
        Expense.incurred_at >= start,
        Expense.incurred_at < end,
    )
    if branch_id:
        stmt = stmt.where(Expense.branch_id == branch_id)
    if category:
        stmt = stmt.where(Expense.category == category)

    rows = list(await db.scalars(stmt.order_by(Expense.incurred_at.desc())))
    return [_out(r) for r in rows]
