"""Public booking-portal read routes (CU1–CU3, Doc 10.5 D3/D8).

The customer journey is: see the shop -> pick services -> pick a barber -> pick a
free time -> give a phone number -> done. Only the last step existed.

These four GETs supply the first four. They share a single guard,
`_public_branch()`, which resolves the shared link and rejects an unknown or
deactivated branch with 404 — the same "never confirm existence" rule the
authenticated routes use (Doc 10.1), so a stranger cannot probe for valid slugs
and learn which shops are on Groomio.

Slot availability deliberately reuses `app.appointments.router._find_conflict`
rather than reimplementing the overlap test. Two implementations of "is this
time free" is exactly how a portal ends up advertising a slot that then fails
with 409 at booking time; one implementation means the two can never disagree.
"""

from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.router import SLOT_STEP_MIN, _find_conflict
from app.booking.schemas import (
    PublicBranchResponse,
    PublicServiceResponse,
    PublicSlotDayResponse,
    PublicSlotResponse,
    PublicStaffResponse,
)
from app.core.database import get_db
from app.core.security import now_utc
from app.models.identity import User
from app.models.people import Schedule, Staff
from app.models.tenant import Branch, Business, Service

router = APIRouter(tags=["public-booking"])

Db = Annotated[AsyncSession, Depends(get_db)]

# A day of slots for one barber. Bounded so a single request cannot fan out into
# an unbounded scan; the portal asks day by day.
MAX_SLOTS_PER_DAY = 40
# How far ahead customers may book. Far enough to be useful, near enough that a
# barber's schedule has not churned by the time they arrive.
BOOKING_HORIZON_DAYS = 30
# Kenya keeps a single timezone with no daylight saving, so a fixed offset is
# correct here and a named zone would only add an ambiguity.
EAF = timezone(timedelta(hours=3))


async def _public_branch(db: AsyncSession, slug: str) -> Branch:
    """Resolve a public booking link, or 404.

    404 for a missing *and* an inactive branch on purpose: a deactivated shop
    should stop taking bookings without confirming it was ever there.
    """
    branch = await db.scalar(select(Branch).where(Branch.slug == slug))
    if not branch or not branch.is_active:
        raise HTTPException(status_code=404, detail="Not found")
    return branch


async def _branch_services(db: AsyncSession, branch: Branch) -> list[Service]:
    return list(
        await db.scalars(
            select(Service)
            # `services.branch_id` is VARCHAR and `branch.id` is a UUID, so the
            # id is stringified before it is bound.
            .where(Service.branch_id == str(branch.id), Service.is_active.is_(True))
            .order_by(Service.name)
        )
    )


async def _branch_staff(db: AsyncSession, branch: Branch) -> list[Staff]:
    """Active barbers at this branch, with their display name resolved.

    Suspended staff are excluded: offering a suspended barber's chair in the
    public flow is how a shop ends up with a customer waiting for someone who
    cannot come in.
    """
    stmt = (
        select(Staff, User.full_name)
        # `staff.user_id` is VARCHAR while `users.id` is UUID and PostgreSQL has
        # no `uuid = varchar` operator; `user_id` always holds the uuid rendered
        # as text, so casting it back is lossless.
        .join(User, User.id == cast(Staff.user_id, UUID))
        .where(Staff.branch_id == str(branch.id), Staff.status == "active")
        .order_by(User.full_name)
    )
    return [
        PublicStaffResponse(
            # `PublicStaffResponse.staff_id` is a `str` and `Staff.id` is a UUID;
            # pydantic will not coerce one into the other.
            staff_id=str(staff.id),
            full_name=name or "Barber",
            photo_url=staff.photo_url,
        )
        for staff, name in (await db.execute(stmt)).all()
    ]


def _default_window(branch_opens: Optional[time], branch_closes: Optional[time]):
    """Opening hours, falling back to a conventional Kenyan barbershop day.

    A shop that never set its hours still needs bookable slots, so the fallback
    is generous (08:00–19:00) rather than empty. The owner can tighten it from
    Settings at any time and the portal picks the change up immediately.
    """
    return (
        branch_opens or time(8, 0),
        branch_closes or time(19, 0),
    )


async def _day_slots(
    db: AsyncSession,
    branch: Branch,
    staff: Staff,
    services: list[Service],
    target_day: date_cls,
) -> list[PublicSlotResponse]:
    """Every free start time for one barber on one day, already conflict-checked.

    The three rules that matter, in order:

    1. **Opening hours** — never advertise a time before the shop opens, even
       if the barber is already on the clock.
    2. **Closing hours** — a slot must *finish* before closing, not merely
       start. A 30-minute haircut offered at 18:50 in a shop that shuts at
       19:00 is a customer who waits 20 minutes for a barber who has gone
       home, so the window test is `cursor + duration <= close`.
    3. **Already taken** — `_find_conflict`, the same check the booking write
       performs, so the portal cannot advertise something that then 409s.
    """
    duration = sum(s.duration_min for s in services)
    total_kes = sum(s.price_kes for s in services)

    rows = list(
        await db.scalars(
            select(Schedule).where(
                # VARCHAR column against a UUID key — stringify before binding.
                Schedule.staff_id == str(staff.id),
                Schedule.day_of_week == target_day.weekday(),
            )
        )
    )
    if not rows:
        # Not rostered that weekday — the barber is off, so there are no slots.
        return []

    opens_at, closes_at = _default_window(branch.opens_at, branch.closes_at)
    now_local = now_utc().astimezone(EAF)

    slots: list[PublicSlotResponse] = []
    for row in rows:
        start = max(row.start_time, opens_at)
        end = min(row.end_time, closes_at)
        cursor = datetime.combine(target_day, start, tzinfo=EAF)
        finish = datetime.combine(target_day, end, tzinfo=EAF)

        while cursor + timedelta(minutes=duration) <= finish:
            if len(slots) >= MAX_SLOTS_PER_DAY:
                break
            # Never advertise a time that has already gone by.
            if cursor > now_local:
                clash = await _find_conflict(db, str(staff.id), cursor, duration)
                if clash is None:
                    slots.append(
                        PublicSlotResponse(
                            scheduled_at=cursor,
                            duration_min=duration,
                            total_kes=total_kes,
                        )
                    )
            cursor += timedelta(minutes=SLOT_STEP_MIN)
        if len(slots) >= MAX_SLOTS_PER_DAY:
            break

    slots.sort(key=lambda s: s.scheduled_at)
    if slots:
        # Mark the single earliest here rather than in the client, so every
        # client agrees on which slot to suggest.
        slots[0].is_earliest = True
    return slots


# ── CU1 — the shop behind the link ───────────────────────────────────────────
@router.get("/public/{branch_slug}", response_model=PublicBranchResponse)
async def public_branch(branch_slug: str, db: Db) -> PublicBranchResponse:
    """Shop name, town and opening hours, so the portal can render a header."""
    branch = await _public_branch(db, branch_slug)
    business = await db.scalar(
        select(Business).where(Business.id == branch.business_id)
    )
    return PublicBranchResponse(
        slug=branch.slug,
        name=branch.name,
        town=branch.town,
        county=branch.county,
        address=branch.address,
        phone=branch.phone,
        latitude=branch.latitude,
        longitude=branch.longitude,
        price_tier=branch.price_tier,
        opens_at=branch.opens_at,
        closes_at=branch.closes_at,
        num_chairs=branch.num_chairs,
        business_name=business.name if business else "Groomio",
    )


# ── CU2 — what this shop sells ───────────────────────────────────────────────
@router.get(
    "/public/{branch_slug}/services", response_model=list[PublicServiceResponse]
)
async def public_services(branch_slug: str, db: Db) -> list[PublicServiceResponse]:
    """Active services with price and duration, sorted by name."""
    branch = await _public_branch(db, branch_slug)
    return [
        PublicServiceResponse(
            id=str(s.id), name=s.name, price_kes=s.price_kes, duration_min=s.duration_min
        )
        for s in await _branch_services(db, branch)
    ]


# ── CU2 — who can take the job ───────────────────────────────────────────────
@router.get("/public/{branch_slug}/staff", response_model=list[PublicStaffResponse])
async def public_staff(branch_slug: str, db: Db) -> list[PublicStaffResponse]:
    """Active barbers at this branch.

    Returns an empty list rather than 404 when nobody is rostered — the portal
    shows "call the shop" instead of an error.
    """
    branch = await _public_branch(db, branch_slug)
    return await _branch_staff(db, branch)


# ── CU3 — free times ─────────────────────────────────────────────────────────
@router.get("/public/{branch_slug}/slots", response_model=PublicSlotDayResponse)
async def public_slots(
    branch_slug: str,
    db: Db,
    staff_id: Annotated[str, Query(min_length=1)],
    service_ids: Annotated[list[str], Query(min_length=1)],
    day: Optional[date_cls] = None,
) -> PublicSlotDayResponse:
    """Free start times for one barber on one day.

    This is the route the portal leans on hardest, so it is the strictest:

    * the barber must belong to *this* branch — otherwise any shop could
      enumerate another shop's staff and read their schedule;
    * the services must all belong to this branch, and prices come from the
      database, never from the client (Doc 10.2 — the client must not be able
      to invent a price);
    * results are clipped to the branch's opening hours and the barber's
      working pattern, and past times are dropped;
    * the same `_find_conflict` the booking write uses decides what is free.
    """
    branch = await _public_branch(db, branch_slug)
    # Compare in local shop time, not UTC: at 03:00 EAT it is still "yesterday"
    # in UTC, and a customer asking for today must not be told the day has passed.
    today_local = now_utc().astimezone(EAF).date()
    target_day = day or today_local

    if target_day > today_local + timedelta(days=BOOKING_HORIZON_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bookings open {BOOKING_HORIZON_DAYS} days ahead",
        )

    staff = await db.scalar(
        select(Staff).where(
            Staff.id == staff_id,
            Staff.branch_id == str(branch.id),
            Staff.status == "active",
        )
    )
    if not staff:
        # 404 rather than 403: a valid staff id from another branch must not be
        # distinguishable from one that does not exist.
        raise HTTPException(status_code=404, detail="Not found")

    services = list(
        await db.scalars(
            select(Service).where(
                Service.id.in_(service_ids),
                Service.branch_id == str(branch.id),
                Service.is_active.is_(True),
            )
        )
    )
    if len(services) != len(set(service_ids)):
        # A service id that is not this branch's (or is retired) is a client
        # error, not a silent substitution — silently booking a haircut at a
        # different price than quoted is the worst outcome here.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more services are unavailable",
        )

    duration = sum(s.duration_min for s in services)
    total_kes = sum(s.price_kes for s in services)

    slots = await _day_slots(db, branch, staff, services, target_day)

    return PublicSlotDayResponse(
        staff_id=str(staff.id),
        date=target_day.isoformat(),
        duration_min=duration,
        total_kes=total_kes,
        slots=slots,
    )
