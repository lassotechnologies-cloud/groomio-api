"""Appointment routes — API group D1–D4 (Doc 10.5).

The headline behaviour here is double-booking prevention (CL3 ★). Two people
cannot be in the same barber's chair at once, so a booking is a write that can
fail. When it does, the response carries the *next free slots* rather than a bare
409 — an owner standing at the counter needs a usable answer, not an error code.

Public booking (D3) is the same logic reached by an unauthenticated customer
through a shared branch link, so it reuses the identical conflict check instead of
trusting the client.
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.schemas import (
    AppointmentCreateRequest,
    AppointmentLineResponse,
    AppointmentResponse,
    AppointmentUpdateStatusRequest,
    ConflictResponse,
    PublicBookingRequest,
)
from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.security import now_utc
from app.models.identity import User
from app.models.operations import Appointment, AppointmentService
from app.models.people import Customer, Staff
from app.models.tenant import Branch, Business, Service

router = APIRouter(tags=["appointments"])

Db = Annotated[AsyncSession, Depends(get_db)]
SchedulerRoles = Depends(require_role("owner", "clerk", "barber"))

# Statuses that still occupy the barber's time.
BLOCKING_STATUSES = ("booked", "confirmed", "in_service")
# Slot granularity offered to the user when a clash happens.
SLOT_STEP_MIN = 30
SLOT_SEARCH_HOURS = 8
MAX_SUGGESTIONS = 5


# ── helpers ──────────────────────────────────────────────────────────────────
async def _business_for(db: AsyncSession, user: dict) -> Business:
    """Owner's business for owner tokens; for staff tokens, look up via staff row."""
    biz = await db.scalar(
        select(Business).where(Business.owner_user_id == user["user_id"])
    )
    if biz:
        return biz

    staff = await db.scalar(select(Staff).where(Staff.user_id == user["user_id"]))
    if staff:
        biz = await db.scalar(select(Business).where(Business.id == staff.business_id))
        if biz:
            return biz

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No business yet")


async def _load_branch(db: AsyncSession, business_id: str, branch_id: str) -> Branch:
    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )
    return branch


async def _price_services(
    db: AsyncSession, branch_id: str, service_ids: list[str]
) -> list[Service]:
    """Resolve the catalogue rows. Prices and durations always come from here."""
    rows = list(
        await db.scalars(
            select(Service).where(
                Service.id.in_(service_ids),
                Service.branch_id == branch_id,
                Service.is_active.is_(True),
            )
        )
    )
    if len(rows) != len(set(service_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown or inactive service",
        )
    return rows


async def _find_conflict(
    db: AsyncSession, staff_id: str, start: datetime, duration_min: int
) -> Optional[Appointment]:
    """Overlap test: existing [start, end) vs the requested window."""
    end = start + timedelta(minutes=duration_min)
    return await db.scalar(
        select(Appointment).where(
            # `appointments.staff_id` is VARCHAR while `Staff.id` is a UUID, so
            # a caller that passes the ORM key straight through is normalised
            # here rather than failing to bind.
            Appointment.staff_id == str(staff_id),
            Appointment.status.in_(BLOCKING_STATUSES),
            # overlap ⇔ existing_start < requested_end AND existing_end > requested_start
            Appointment.scheduled_at < end,
            Appointment.scheduled_at + Appointment.duration_min * timedelta(minutes=1)
            > start,
        )
    )


async def _next_free_slots(
    db: AsyncSession, staff_id: str, after: datetime, duration_min: int, limit: int
) -> list[datetime]:
    """Walk forward from `after` in SLOT_STEP_MIN steps until `limit` free slots."""
    step = timedelta(minutes=SLOT_STEP_MIN)
    candidate = after.replace(second=0, microsecond=0)
    horizon = candidate + timedelta(hours=SLOT_SEARCH_HOURS)
    found: list[datetime] = []

    while candidate < horizon and len(found) < limit:
        clash = await _find_conflict(db, staff_id, candidate, duration_min)
        if clash is None:
            found.append(candidate)
        candidate += step

    return found


async def _staff_names(db: AsyncSession, staff_ids: set[str]) -> dict[str, str]:
    if not staff_ids:
        return {}
    stmt = (
        select(Staff.id, User.full_name)
        .join(User, User.id == Staff.user_id)
        .where(Staff.id.in_(staff_ids))
    )
    return {str(sid): name for sid, name in (await db.execute(stmt)).all()}


async def _serialize(
    db: AsyncSession, appts: list[Appointment]
) -> list[AppointmentResponse]:
    """Attach lines, totals and display names in batched queries."""
    if not appts:
        return []

    appt_ids = [str(a.id) for a in appts]
    customer_ids = {a.customer_id for a in appts}

    lines: dict[str, list[AppointmentLineResponse]] = {i: [] for i in appt_ids}
    for row in await db.scalars(
        select(AppointmentService).where(
            AppointmentService.appointment_id.in_(appt_ids)
        )
    ):
        svc = await db.scalar(select(Service).where(Service.id == row.service_id))
        lines[row.appointment_id].append(
            AppointmentLineResponse(
                service_id=row.service_id,
                name=svc.name if svc else "Service",
                price_kes=row.price_kes,
                duration_min=svc.duration_min if svc else 0,
            )
        )

    names: dict[str, str] = {}
    for c in await db.scalars(select(Customer).where(Customer.id.in_(customer_ids))):
        names[c.id] = c.full_name
    staff_names = await _staff_names(db, {a.staff_id for a in appts})

    out = []
    for a in appts:
        key = str(a.id)
        items = lines[key]
        out.append(
            AppointmentResponse(
                id=key,
                business_id=a.business_id,
                branch_id=a.branch_id,
                customer_id=a.customer_id,
                customer_name=names.get(a.customer_id),
                staff_id=a.staff_id,
                staff_name=staff_names.get(a.staff_id),
                chair_id=a.chair_id,
                scheduled_at=a.scheduled_at,
                duration_min=a.duration_min,
                source=a.source,
                status=a.status,
                notes=a.notes,
                total_kes=sum(i.price_kes for i in items),
                lines=items,
            )
        )
    return out


# ── D1 — calendar view ───────────────────────────────────────────────────────
@router.get(
    "/branches/{branch_id}/appointments",
    response_model=list[AppointmentResponse],
    dependencies=[SchedulerRoles],
)
async def list_appointments(
    branch_id: str,
    db: Db,
    user: CurrentUser,
    on_date: Optional[str] = Query(
        None, alias="date", description="YYYY-MM-DD; defaults to today"
    ),
    staff: Optional[str] = Query(None, description="Filter to one staff_id"),
    status_filter: Optional[str] = Query(None, alias="status"),
) -> list[AppointmentResponse]:
    """A day's bookings for the branch calendar (D1)."""
    biz = await _business_for(db, user)
    await _load_branch(db, str(biz.id), branch_id)

    if on_date:
        try:
            day = datetime.strptime(on_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date must be YYYY-MM-DD",
            ) from None
    else:
        day = now_utc()

    stmt = select(Appointment).where(
        Appointment.branch_id == branch_id,
        Appointment.business_id == str(biz.id),
        Appointment.scheduled_at >= day,
        Appointment.scheduled_at < day + timedelta(days=1),
    )
    if staff:
        stmt = stmt.where(Appointment.staff_id == staff)
    if status_filter:
        stmt = stmt.where(Appointment.status == status_filter)

    rows = list(await db.scalars(stmt.order_by(Appointment.scheduled_at)))
    return await _serialize(db, rows)


# ── D2 — staff / walk-in booking ─────────────────────────────────────────────
@router.post(
    "/appointments",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[SchedulerRoles],
)
async def create_appointment(
    payload: AppointmentCreateRequest, db: Db, user: CurrentUser
) -> AppointmentResponse:
    """Book a slot. On a clash, 409 with the next free slots (CL3 ★)."""
    biz = await _business_for(db, user)
    business_id = str(biz.id)
    await _load_branch(db, business_id, payload.branch_id)

    services = await _price_services(db, payload.branch_id, payload.service_ids)
    duration = sum(s.duration_min for s in services)

    clash = await _find_conflict(db, payload.staff_id, payload.scheduled_at, duration)
    if clash:
        free = await _next_free_slots(
            db, payload.staff_id, payload.scheduled_at, duration, MAX_SUGGESTIONS
        )
        if not free:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "slot_taken",
                    "message": "No free slots in the next 8 hours",
                    "conflicting_appointment_id": str(clash.id),
                    "next_free_slots": [],
                },
            )
        # Documented as 409 in Doc 10.1 — surfaced as a body so the client can
        # offer the alternatives inline.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ConflictResponse(
                message="That slot is already taken",
                conflicting_appointment_id=str(clash.id),
                next_free_slots=free,
            ).model_dump(mode="json"),
        )

    appt = Appointment(
        business_id=business_id,
        branch_id=payload.branch_id,
        customer_id=payload.customer_id,
        staff_id=payload.staff_id,
        chair_id=payload.chair_id,
        scheduled_at=payload.scheduled_at,
        duration_min=duration,
        source=payload.source,
        status="booked",
        notes=payload.notes,
    )
    db.add(appt)
    await db.flush()

    for svc in services:
        db.add(
            AppointmentService(
                appointment_id=str(appt.id),
                service_id=svc.id,
                price_kes=svc.price_kes,
            )
        )
    await db.flush()

    return (await _serialize(db, [appt]))[0]


# ── D3 — public online booking (no auth) ─────────────────────────────────────
@router.post(
    "/public/{branch_slug}/appointments",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        # The portal's whole race-condition recovery depends on this response
        # being part of the contract, so it is declared rather than left as
        # behaviour the OpenAPI schema does not mention. The customer client
        # reads `detail.next_free_slots` straight off this.
        409: {
            "model": ConflictResponse,
            "description": (
                "The slot was taken between being shown and being confirmed. "
                "Carries the next free slots so the customer can choose again "
                "without starting the whole booking over."
            ),
        },
        404: {"description": "Unknown or inactive branch."},
    },
)
async def public_book(
    branch_slug: str, payload: PublicBookingRequest, db: Db
) -> AppointmentResponse:
    """CU1 — a customer books themselves through a shared branch link.

    The customer is matched by phone and created on first booking, so a walk-in
    never has to make an account (the customers table is deliberately not a login).
    """
    branch = await db.scalar(select(Branch).where(Branch.slug == branch_slug))
    if not branch or not branch.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    business_id = branch.business_id
    services = await _price_services(db, branch.id, payload.service_ids)
    duration = sum(s.duration_min for s in services)

    # Only free slots are offered publicly — the same check staff booking uses.
    clash = await _find_conflict(db, payload.staff_id, payload.scheduled_at, duration)
    if clash:
        free = await _next_free_slots(
            db, payload.staff_id, payload.scheduled_at, duration, MAX_SUGGESTIONS
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ConflictResponse(
                message="That slot was just taken",
                conflicting_appointment_id=str(clash.id),
                next_free_slots=free,
            ).model_dump(mode="json"),
        )

    customer = await db.scalar(
        select(Customer).where(
            Customer.business_id == business_id,
            Customer.phone == payload.customer_phone,
        )
    )
    if not customer:
        customer = Customer(
            business_id=business_id,
            branch_id=branch.id,
            full_name=payload.customer_name,
            phone=payload.customer_phone,
            loyalty_points=0,
        )
        db.add(customer)
        await db.flush()
    elif not customer.full_name:
        customer.full_name = payload.customer_name

    appt = Appointment(
        business_id=business_id,
        branch_id=branch.id,
        customer_id=customer.id,
        staff_id=payload.staff_id,
        scheduled_at=payload.scheduled_at,
        duration_min=duration,
        source="online",
        status="booked",
        notes=payload.notes,
    )
    db.add(appt)
    await db.flush()

    for svc in services:
        db.add(
            AppointmentService(
                appointment_id=str(appt.id),
                service_id=svc.id,
                price_kes=svc.price_kes,
            )
        )
    await db.flush()

    return (await _serialize(db, [appt]))[0]


# ── D4 — move status ─────────────────────────────────────────────────────────
# Lifecycle: booked -> confirmed -> in_service -> completed, with no_show/cancelled
# reachable from any non-terminal state.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "booked": {"confirmed", "in_service", "completed", "no_show", "cancelled"},
    "confirmed": {"in_service", "completed", "no_show", "cancelled"},
    "in_service": {"completed", "cancelled"},
    "completed": set(),
    "no_show": set(),
    "cancelled": set(),
}


@router.patch(
    "/appointments/{appointment_id}/status",
    response_model=AppointmentResponse,
    dependencies=[SchedulerRoles],
)
async def update_appointment_status(
    appointment_id: str,
    payload: AppointmentUpdateStatusRequest,
    db: Db,
    user: CurrentUser,
) -> AppointmentResponse:
    """Advance an appointment. A barber may only move their own (Doc 10.5 D4)."""
    biz = await _business_for(db, user)
    business_id = str(biz.id)

    appt = await db.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id, Appointment.business_id == business_id
        )
    )
    if not appt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if user["role"] == "barber":
        staff = await db.scalar(select(Staff).where(Staff.user_id == user["user_id"]))
        if not staff or staff.id != appt.staff_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Barbers can only update their own appointments",
            )

    if payload.status not in ALLOWED_TRANSITIONS.get(appt.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot move {appt.status} -> {payload.status}",
        )

    appt.status = payload.status
    if payload.notes is not None:
        appt.notes = payload.notes

    return (await _serialize(db, [appt]))[0]
