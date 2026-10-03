"""Customer routes — API group C (Doc 10.4: C1–C5).

Tenancy: every query is filtered by `business_id` derived from the caller's own
business, so a clerk passing another shop's customer id gets 404, not data.

Search (C1) is a prefix match on name plus an exact/partial match on phone —
that is what a clerk needs when a customer walks in and says "I was here last
week, I'm Peter" or reads out a phone number (CL5).
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.customers.schemas import (
    CustomerCreateRequest,
    CustomerListResponse,
    CustomerResponse,
    CustomerUpdateRequest,
    VisitResponse,
)
from app.models.identity import User
from app.models.money import Sale
from app.models.operations import Appointment, AppointmentService
from app.models.people import Customer, Staff
from app.models.tenant import Branch, Service

router = APIRouter(tags=["customers"])

Db = Annotated[AsyncSession, Depends(get_db)]
StaffOnly = Depends(require_role("owner", "clerk", "barber"))

MAX_PAGE_SIZE = 100


# ── helpers ──────────────────────────────────────────────────────────────────
async def _my_business_id(db: AsyncSession, user: dict) -> str:
    """The caller's business. Derived from the token's user, never from the body."""
    from app.models.tenant import Business

    biz = await db.scalar(
        select(Business).where(Business.owner_user_id == user["user_id"])
    )
    if not biz:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No business yet"
        )
    return str(biz.id)


async def _load_customer(
    db: AsyncSession, business_id: str, customer_id: str
) -> Customer:
    cust = await db.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.business_id == business_id
        )
    )
    if not cust:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return cust


async def _staff_name_map(
    db: AsyncSession, *rows: object
) -> dict[str, str]:
    """Map staff_id -> display name. Staff rows carry a user_id, not a name."""
    staff_ids = {getattr(r, "staff_id", None) for r in rows} - {None}
    if not staff_ids:
        return {}

    stmt = (
        select(Staff.id, User.full_name)
        .join(User, User.id == Staff.user_id)
        .where(Staff.id.in_(staff_ids))
    )
    return {str(sid): name for sid, name in (await db.execute(stmt)).all()}


def _out(cust: Customer) -> CustomerResponse:
    return CustomerResponse(
        id=str(cust.id),
        business_id=cust.business_id,
        branch_id=cust.branch_id,
        full_name=cust.full_name,
        phone=cust.phone,
        photo_url=cust.photo_url,
        preferences=cust.preferences,
        notes=cust.notes,
        loyalty_points=cust.loyalty_points or 0,
        birthday=cust.birthday.isoformat() if cust.birthday else None,
        created_at=cust.created_at.isoformat() if cust.created_at else None,
    )


# ── C1 — search ──────────────────────────────────────────────────────────────
@router.get("/customers", response_model=CustomerListResponse, dependencies=[StaffOnly])
async def search_customers(
    db: Db,
    user: CurrentUser,
    search: Optional[str] = Query(
        None, max_length=120, description="Name prefix or phone fragment"
    ),
    branch: Optional[str] = Query(None, description="Filter to one branch_id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=MAX_PAGE_SIZE),
) -> CustomerListResponse:
    """Paginated customer search scoped to the caller's business (CL5)."""
    business_id = await _my_business_id(db, user)

    stmt = select(Customer).where(Customer.business_id == business_id)
    count_stmt = (
        select(func.count())
        .select_from(Customer)
        .where(Customer.business_id == business_id)
    )

    if branch:
        stmt = stmt.where(Customer.branch_id == branch)
        count_stmt = count_stmt.where(Customer.branch_id == branch)

    if search:
        term = f"%{search.strip().lower()}%"
        name_match = func.lower(Customer.full_name).like(term)
        # Exact digits or a trailing fragment, so "0712…" finds "+254712…".
        digits = "".join(ch for ch in search if ch.isdigit())
        phone_match = Customer.phone.like(f"%{digits}") if digits else False
        stmt = stmt.where(or_(name_match, phone_match))
        count_stmt = count_stmt.where(or_(name_match, phone_match))

    total = await db.scalar(count_stmt) or 0
    rows = await db.scalars(
        stmt.order_by(Customer.full_name)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return CustomerListResponse(
        items=[_out(c) for c in rows], total=total, page=page, page_size=page_size
    )


# ── C2 — create ──────────────────────────────────────────────────────────────
@router.post(
    "/customers",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[StaffOnly],
)
async def create_customer(
    payload: CustomerCreateRequest, db: Db, user: CurrentUser
) -> CustomerResponse:
    """Create a profile. A returning customer must not become two rows (CL6)."""
    business_id = await _my_business_id(db, user)

    branch = await db.scalar(
        select(Branch).where(
            Branch.id == payload.branch_id, Branch.business_id == business_id
        )
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )

    existing = await db.scalar(
        select(Customer).where(
            Customer.business_id == business_id, Customer.phone == payload.phone
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Customer with that phone already exists: {existing.full_name}",
        )

    cust = Customer(
        business_id=business_id,
        branch_id=payload.branch_id,
        full_name=payload.full_name,
        phone=payload.phone,
        photo_url=payload.photo_url,
        preferences=payload.preferences,
        notes=payload.notes,
        birthday=payload.birthday,
        loyalty_points=0,
    )
    db.add(cust)
    await db.flush()
    return _out(cust)


# ── C3 — full profile ────────────────────────────────────────────────────────
@router.get(
    "/customers/{customer_id}",
    response_model=CustomerResponse,
    dependencies=[StaffOnly],
)
async def get_customer(customer_id: str, db: Db, user: CurrentUser) -> CustomerResponse:
    """Profile with loyalty points. Visit history is a separate call (C5)."""
    business_id = await _my_business_id(db, user)
    return _out(await _load_customer(db, business_id, customer_id))


# ── C4 — update ──────────────────────────────────────────────────────────────
@router.patch(
    "/customers/{customer_id}",
    response_model=CustomerResponse,
    dependencies=[StaffOnly],
)
async def update_customer(
    customer_id: str, payload: CustomerUpdateRequest, db: Db, user: CurrentUser
) -> CustomerResponse:
    """Preferences, notes, birthday. Loyalty points are earned, never hand-edited."""
    business_id = await _my_business_id(db, user)
    cust = await _load_customer(db, business_id, customer_id)

    if payload.branch_id and payload.branch_id != cust.branch_id:
        branch = await db.scalar(
            select(Branch).where(
                Branch.id == payload.branch_id, Branch.business_id == business_id
            )
        )
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
            )
        cust.branch_id = payload.branch_id

    for field in ("full_name", "photo_url", "preferences", "notes", "birthday"):
        value = getattr(payload, field)
        if value is not None:
            setattr(cust, field, value)

    return _out(cust)


# ── C5 — visit history ───────────────────────────────────────────────────────
@router.get(
    "/customers/{customer_id}/visits",
    response_model=list[VisitResponse],
    dependencies=[StaffOnly],
)
async def list_visits(
    customer_id: str,
    db: Db,
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
) -> list[VisitResponse]:
    """Bookings and recorded sales for one customer, newest first."""
    business_id = await _my_business_id(db, user)
    await _load_customer(db, business_id, customer_id)

    appts = await db.scalars(
        select(Appointment)
        .where(
            Appointment.customer_id == customer_id,
            Appointment.business_id == business_id,
        )
        .order_by(Appointment.scheduled_at.desc())
        .limit(limit)
    )

    appt_rows = list(appts)
    visits: list[VisitResponse] = []
    # Resolve staff display names up front so both visit kinds can use them.
    staff_names = await _staff_name_map(db, *appt_rows)

    if appt_rows:
        appt_ids = [str(a.id) for a in appt_rows]

        # Appointment total = sum of its priced service lines.
        totals: dict[str, int] = {appt_id: 0 for appt_id in appt_ids}
        service_ids: dict[str, list[str]] = {appt_id: [] for appt_id in appt_ids}
        for line in await db.scalars(
            select(AppointmentService).where(
                AppointmentService.appointment_id.in_(appt_ids)
            )
        ):
            totals[line.appointment_id] = (
                totals.get(line.appointment_id, 0) + line.price_kes
            )
            service_ids[line.appointment_id].append(line.service_id)

        # Resolve service ids to names in one query rather than one per line.
        name_by_service: dict[str, str] = {}
        wanted = {sid for ids in service_ids.values() for sid in ids}
        if wanted:
            for svc in await db.scalars(select(Service).where(Service.id.in_(wanted))):
                name_by_service[str(svc.id)] = svc.name
        service_names = {
            appt_id: [name_by_service.get(sid, "Service") for sid in ids]
            for appt_id, ids in service_ids.items()
        }

        for a in appt_rows:
            key = str(a.id)
            visits.append(
                VisitResponse(
                    id=key,
                    kind="appointment",
                    scheduled_at=a.scheduled_at,
                    staff_name=staff_names.get(a.staff_id),
                    status=a.status,
                    total_kes=totals.get(key, 0),
                    service_names=service_names.get(key, []),
                )
            )

    sales_rows = list(
        await db.scalars(
            select(Sale)
            .where(Sale.customer_id == customer_id, Sale.business_id == business_id)
            .order_by(Sale.created_at.desc())
            .limit(limit)
        )
    )

    # Sales may involve staff the appointments did not cover.
    for sale in sales_rows:
        if sale.staff_id not in staff_names:
            staff_names.update(await _staff_name_map(db, sale))
        visits.append(
            VisitResponse(
                id=str(sale.id),
                kind="sale",
                occurred_at=sale.created_at,
                staff_name=staff_names.get(sale.staff_id),
                status="completed",
                total_kes=sale.total_kes,
                service_names=[],
            )
        )

    # Mixed kinds: sort on whichever timestamp is set, newest first.
    def _when(v: VisitResponse) -> datetime:
        return (
            v.occurred_at or v.scheduled_at or datetime.min.replace(tzinfo=timezone.utc)
        )

    visits.sort(key=_when, reverse=True)
    return visits[:limit]
