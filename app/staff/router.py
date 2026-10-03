"""Staff routes — API group E (Doc 10.6: E1, E5, E6, E7).

A person is 1 users row + 1 staff row (Doc 9.3). Creating staff therefore creates
a login too, which is why it is owner-only: a barber cannot add a colleague, and
nobody can mint themselves a commission rate.

Scope rules enforced here, beyond the tenant filter:
- E1 create/edit is owner-only (Doc 2.7).
- E5/E6/E7 let a barber act on *their own* records only, via `resolve_staff_for_user`.
- Suspending a staff member blocks new logins but leaves their sales and
  commission history intact — payroll history must not disappear.
"""

from datetime import date, datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.security import hash_password, now_utc
from app.core.tenancy import resolve_business_id, resolve_staff_for_user
from app.models.identity import User
from app.models.people import Attendance, LeaveRequest, Schedule, Staff
from app.models.tenant import Branch
from app.staff.schemas import (
    AttendanceResponse,
    LeaveDecisionRequest,
    LeaveRequestCreateRequest,
    LeaveRequestResponse,
    ScheduleCreateRequest,
    ScheduleResponse,
    StaffCreateRequest,
    StaffResponse,
    StaffUpdateRequest,
)

router = APIRouter(tags=["staff"])

Db = Annotated[AsyncSession, Depends(get_db)]
OwnerOnly = Depends(require_role("owner"))
StaffOrOwner = Depends(require_role("owner", "clerk", "barber"))


# ── helpers ──────────────────────────────────────────────────────────────────
def _normalize_phone(v: str) -> str:
    digits = "".join(ch for ch in v if ch.isdigit())
    if digits.startswith("0"):
        digits = "+254" + digits[1:]
    elif digits.startswith("254"):
        digits = "+" + digits
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


async def _staff_names(db: AsyncSession, staff_ids: set[str]) -> dict[str, tuple[str, str]]:
    """staff_id -> (display name, phone), joined through users."""
    if not staff_ids:
        return {}
    stmt = (
        select(Staff.id, User.full_name, User.phone)
        .join(User, User.id == Staff.user_id)
        .where(Staff.id.in_(staff_ids))
    )
    return {
        str(sid): (name, phone) for sid, name, phone in (await db.execute(stmt)).all()
    }


def _out(staff: Staff, names: dict[str, tuple[str, str]]) -> StaffResponse:
    name, phone = names.get(staff.id, (None, None))
    return StaffResponse(
        id=str(staff.id),
        business_id=staff.business_id,
        branch_id=staff.branch_id,
        user_id=staff.user_id,
        full_name=name,
        phone=phone,
        staff_role=staff.staff_role,
        commission_type=staff.commission_type,
        commission_value=staff.commission_value,
        photo_url=staff.photo_url,
        status=staff.status,
    )


async def _load_staff(db: AsyncSession, business_id: str, staff_id: str) -> Staff:
    staff = await db.scalar(
        select(Staff).where(Staff.id == staff_id, Staff.business_id == business_id)
    )
    if not staff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return staff


# ── E1 — staff profiles ──────────────────────────────────────────────────────
@router.get("/staff", response_model=list[StaffResponse], dependencies=[StaffOrOwner])
async def list_staff(
    db: Db,
    user: CurrentUser,
    branch_id: Optional[str] = Query(None),
) -> list[StaffResponse]:
    """Everyone employed by the caller's business, optionally one branch."""
    business_id = await resolve_business_id(db, user)

    stmt = select(Staff).where(Staff.business_id == business_id)
    if branch_id:
        stmt = stmt.where(Staff.branch_id == branch_id)

    rows = list(await db.scalars(stmt.order_by(Staff.created_at)))
    names = await _staff_names(db, {s.id for s in rows})
    return [_out(s, names) for s in rows]


@router.post(
    "/staff",
    response_model=StaffResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def create_staff(
    payload: StaffCreateRequest, db: Db, user: CurrentUser
) -> StaffResponse:
    """Add an employee: creates the login and the staff row together."""
    business_id = await resolve_business_id(db, user)

    branch = await db.scalar(
        select(Branch).where(
            Branch.id == payload.branch_id, Branch.business_id == business_id
        )
    )
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    phone = _normalize_phone(payload.phone)
    if await db.scalar(select(User).where(User.phone == phone)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That phone number already has an account",
        )

    account = User(
        full_name=payload.full_name,
        phone=phone,
        email=payload.email or f"{phone.lstrip('+')}@staff.groomio.internal",
        password_hash=hash_password(payload.password),
        role=payload.staff_role,  # 'barber' | 'clerk' — matches user_role enum
        status="active",
    )
    db.add(account)
    await db.flush()

    staff = Staff(
        business_id=business_id,
        branch_id=payload.branch_id,
        user_id=str(account.id),
        staff_role=payload.staff_role,
        commission_type=payload.commission_type,
        commission_value=payload.commission_value,
        photo_url=payload.photo_url,
        status="active",
    )
    db.add(staff)
    await db.flush()

    return _out(staff, {staff.id: (account.full_name, account.phone)})


@router.patch("/staff/{staff_id}", response_model=StaffResponse, dependencies=[OwnerOnly])
async def update_staff(
    staff_id: str, payload: StaffUpdateRequest, db: Db, user: CurrentUser
) -> StaffResponse:
    """Edit a profile. Suspension also blocks the person's login (E1)."""
    business_id = await resolve_business_id(db, user)
    staff = await _load_staff(db, business_id, staff_id)

    if payload.branch_id and payload.branch_id != staff.branch_id:
        branch = await db.scalar(
            select(Branch).where(
                Branch.id == payload.branch_id, Branch.business_id == business_id
            )
        )
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
            )
        staff.branch_id = payload.branch_id

    for field in ("staff_role", "commission_type", "commission_value", "photo_url"):
        value = getattr(payload, field)
        if value is not None:
            setattr(staff, field, value)

    if payload.full_name:
        account = await db.scalar(select(User).where(User.id == staff.user_id))
        if account:
            account.full_name = payload.full_name

    if payload.status:
        staff.status = payload.status
        account = await db.scalar(select(User).where(User.id == staff.user_id))
        if account:
            # Suspending the staff row without the login would leave someone able
            # to sign in and act on data they should no longer reach.
            account.status = "suspended" if payload.status == "suspended" else "active"

    names = await _staff_names(db, {staff.id})
    return _out(staff, names)


# ── E5 — weekly schedule ─────────────────────────────────────────────────────
@router.get(
    "/staff/{staff_id}/schedules",
    response_model=list[ScheduleResponse],
    dependencies=[StaffOrOwner],
)
async def list_schedules(
    staff_id: str, db: Db, user: CurrentUser
) -> list[ScheduleResponse]:
    """BA3 — a barber's weekly pattern. Staff may read their own."""
    business_id = await resolve_business_id(db, user)
    if user["role"] == "barber":
        me = await resolve_staff_for_user(db, user)
        if me.id != staff_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Own schedule only"
            )
    await _load_staff(db, business_id, staff_id)

    rows = list(
        await db.scalars(
            select(Schedule)
            .where(Schedule.staff_id == staff_id)
            .order_by(Schedule.day_of_week, Schedule.start_time)
        )
    )
    return [
        ScheduleResponse(
            id=str(s.id),
            staff_id=s.staff_id,
            day_of_week=s.day_of_week,
            start_time=s.start_time.isoformat(),
            end_time=s.end_time.isoformat(),
        )
        for s in rows
    ]


@router.post(
    "/staff/{staff_id}/schedules",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def add_schedule(
    staff_id: str, payload: ScheduleCreateRequest, db: Db, user: CurrentUser
) -> ScheduleResponse:
    business_id = await resolve_business_id(db, user)
    await _load_staff(db, business_id, staff_id)

    row = Schedule(
        staff_id=staff_id,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
    )
    db.add(row)
    await db.flush()
    return ScheduleResponse(
        id=str(row.id),
        staff_id=row.staff_id,
        day_of_week=row.day_of_week,
        start_time=row.start_time.isoformat(),
        end_time=row.end_time.isoformat(),
    )


@router.delete(
    "/staff/{staff_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[OwnerOnly],
)
async def remove_schedule(
    staff_id: str, schedule_id: str, db: Db, user: CurrentUser
) -> None:
    business_id = await resolve_business_id(db, user)
    await _load_staff(db, business_id, staff_id)

    row = await db.scalar(
        select(Schedule).where(Schedule.id == schedule_id, Schedule.staff_id == staff_id)
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    await db.delete(row)
    return None


# ── E6 — attendance ──────────────────────────────────────────────────────────
@router.post(
    "/staff/{staff_id}/attendance/check-in",
    response_model=AttendanceResponse,
    dependencies=[StaffOrOwner],
)
async def check_in(staff_id: str, db: Db, user: CurrentUser) -> AttendanceResponse:
    """One open shift per person per day — a double check-in is a conflict."""
    business_id = await resolve_business_id(db, user)
    if user["role"] == "barber":
        me = await resolve_staff_for_user(db, user)
        if me.id != staff_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Own attendance only"
            )
    staff = await _load_staff(db, business_id, staff_id)

    today = now_utc().date()
    existing = await db.scalar(
        select(Attendance).where(
            Attendance.staff_id == staff_id, Attendance.work_date == today
        )
    )
    if existing:
        if existing.check_in_at and not existing.check_out_at:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Already checked in"
            )
        existing.check_in_at = now_utc()
        return _attendance_out(existing)

    row = Attendance(
        staff_id=staff_id,
        branch_id=staff.branch_id,
        work_date=today,
        check_in_at=now_utc(),
    )
    db.add(row)
    await db.flush()
    return _attendance_out(row)


@router.post(
    "/staff/{staff_id}/attendance/check-out",
    response_model=AttendanceResponse,
    dependencies=[StaffOrOwner],
)
async def check_out(staff_id: str, db: Db, user: CurrentUser) -> AttendanceResponse:
    business_id = await resolve_business_id(db, user)
    if user["role"] == "barber":
        me = await resolve_staff_for_user(db, user)
        if me.id != staff_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Own attendance only"
            )
    await _load_staff(db, business_id, staff_id)

    today = now_utc().date()
    row = await db.scalar(
        select(Attendance).where(
            Attendance.staff_id == staff_id, Attendance.work_date == today
        )
    )
    if not row or not row.check_in_at:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Not checked in today"
        )
    if row.check_out_at:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already checked out"
        )

    row.check_out_at = now_utc()
    return _attendance_out(row)


def _attendance_out(row: Attendance) -> AttendanceResponse:
    return AttendanceResponse(
        id=str(row.id),
        staff_id=row.staff_id,
        branch_id=row.branch_id,
        work_date=row.work_date,
        check_in_at=row.check_in_at,
        check_out_at=row.check_out_at,
    )


# ── E7 — leave requests ──────────────────────────────────────────────────────
@router.get("/leave-requests", response_model=list[LeaveRequestResponse], dependencies=[OwnerOnly])
async def list_leave_requests(
    db: Db,
    user: CurrentUser,
    status_filter: Optional[str] = Query(None, alias="status"),
) -> list[LeaveRequestResponse]:
    """Owner sees every request; barbers see their own (checked below)."""
    business_id = await resolve_business_id(db, user)

    staff_ids = {
        s.id
        for s in await db.scalars(
            select(Staff).where(Staff.business_id == business_id)
        )
    }
    if not staff_ids:
        return []

    stmt = select(LeaveRequest).where(LeaveRequest.staff_id.in_(staff_ids))
    if status_filter:
        stmt = stmt.where(LeaveRequest.status == status_filter)

    rows = list(await db.scalars(stmt.order_by(LeaveRequest.start_date.desc())))
    names = await _staff_names(db, {r.staff_id for r in rows})
    return [
        LeaveRequestResponse(
            id=str(r.id),
            staff_id=r.staff_id,
            staff_name=names.get(r.staff_id, (None, None))[0],
            leave_type=r.leave_type,
            start_date=r.start_date,
            end_date=r.end_date,
            reason=r.reason,
            status=r.status,
            decided_by=r.decided_by,
        )
        for r in rows
    ]


@router.get("/leave-requests/mine", response_model=list[LeaveRequestResponse])
async def list_my_leave_requests(db: Db, user: CurrentUser) -> list[LeaveRequestResponse]:
    """A barber's own leave history, without needing the owner role."""
    me = await resolve_staff_for_user(db, user)
    rows = list(
        await db.scalars(
            select(LeaveRequest)
            .where(LeaveRequest.staff_id == me.id)
            .order_by(LeaveRequest.start_date.desc())
        )
    )
    return [
        LeaveRequestResponse(
            id=str(r.id),
            staff_id=r.staff_id,
            staff_name=None,
            leave_type=r.leave_type,
            start_date=r.start_date,
            end_date=r.end_date,
            reason=r.reason,
            status=r.status,
            decided_by=r.decided_by,
        )
        for r in rows
    ]


@router.post(
    "/leave-requests",
    response_model=LeaveRequestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[StaffOrOwner],
)
async def create_leave_request(
    payload: LeaveRequestCreateRequest, db: Db, user: CurrentUser
) -> LeaveRequestResponse:
    """A barber asks for leave; the owner decides in the next step."""
    me = await resolve_staff_for_user(db, user)

    overlap = await db.scalar(
        select(LeaveRequest).where(
            LeaveRequest.staff_id == me.id,
            LeaveRequest.status == "pending",
            LeaveRequest.start_date <= payload.end_date,
            LeaveRequest.end_date >= payload.start_date,
        )
    )
    if overlap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Overlapping leave already pending"
        )

    row = LeaveRequest(
        staff_id=me.id,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        reason=payload.reason,
        status="pending",
    )
    db.add(row)
    await db.flush()
    return LeaveRequestResponse(
        id=str(row.id),
        staff_id=row.staff_id,
        staff_name=None,
        leave_type=row.leave_type,
        start_date=row.start_date,
        end_date=row.end_date,
        reason=row.reason,
        status=row.status,
        decided_by=row.decided_by,
    )


@router.patch(
    "/leave-requests/{leave_id}",
    response_model=LeaveRequestResponse,
    dependencies=[OwnerOnly],
)
async def decide_leave_request(
    leave_id: str, payload: LeaveDecisionRequest, db: Db, user: CurrentUser
) -> LeaveRequestResponse:
    """Owner approves or rejects. A decided request is final."""
    business_id = await resolve_business_id(db, user)

    staff_ids = {
        s.id for s in await db.scalars(select(Staff).where(Staff.business_id == business_id))
    }
    row = await db.scalar(
        select(LeaveRequest).where(LeaveRequest.id == leave_id)
    )
    if not row or row.staff_id not in staff_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if row.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already {row.status}",
        )

    row.status = payload.status
    row.decided_by = user["user_id"]

    names = await _staff_names(db, {row.staff_id})
    return LeaveRequestResponse(
        id=str(row.id),
        staff_id=row.staff_id,
        staff_name=names.get(row.staff_id, (None, None))[0],
        leave_type=row.leave_type,
        start_date=row.start_date,
        end_date=row.end_date,
        reason=row.reason,
        status=row.status,
        decided_by=row.decided_by,
    )
