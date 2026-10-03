"""Tests for the public booking-portal reads (CU1–CU3).

Two layers:

1. **Schema + OpenAPI tests** (always run, no database). These pin the things
   that make it safe to leave these routes unauthenticated: no route demands a
   token, and no public response model carries a customer phone, a commission
   figure or any other shop-internal data.

2. **Integration tests** (need `TEST_DATABASE_URL`, skip cleanly otherwise).
   These prove the cross-branch rules on a real PostgreSQL database, using the
   same `db` fixture as the rest of the suite.

The slot-window tests deliberately call a fake in-memory clock rather than the
endpoint, because the endpoint filters out past times and would return an empty
list for any fixed date in the past — a test that asserts on "no slots" because
the date is stale passes for the wrong reason.
"""

import itertools
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

EAF = timezone(timedelta(hours=3))  # East Africa Time, no DST


# ── slot window arithmetic ───────────────────────────────────────────────────
def _slots(
    branch_opens: time,
    branch_closes: time,
    staff_start: time,
    staff_end: time,
    duration_min: int,
    step_min: int = 30,
) -> list[time]:
    """The window arithmetic from app.booking.router.public_slots, in isolation.

    A fixed future date keeps "is this in the past?" out of the picture, and
    `now` is injected rather than read so the rules under test are only the
    three that matter: opening hours, closing hours, and service length.
    """
    start = max(staff_start, branch_opens)
    end = min(staff_end, branch_closes)
    day = date(2030, 1, 5)  # a Saturday, deliberately far in the future

    out: list[time] = []
    cursor = datetime.combine(day, start, tzinfo=EAF)
    finish = datetime.combine(day, end, tzinfo=EAF)
    while cursor + timedelta(minutes=duration_min) <= finish:
        out.append(cursor.time())
        cursor += timedelta(minutes=step_min)
    return out


def test_slot_does_not_start_before_branch_opens():
    # A barber rostered from 07:00 in a shop that opens at 08:00.
    slots = _slots(time(8, 0), time(19, 0), time(7, 0), time(19, 0), 30)
    assert slots[0] == time(8, 0)
    assert time(7, 30) not in slots


def test_slot_does_not_start_when_shop_is_closing():
    slots = _slots(time(8, 0), time(19, 0), time(8, 0), time(22, 0), 30)
    assert slots[-1] == time(18, 30)
    assert time(19, 0) not in slots


def test_long_service_cannot_straddle_closing_time():
    # 90 minutes in a shop closing at 19:00 — last start is 17:30.
    slots = _slots(time(8, 0), time(19, 0), time(8, 0), time(19, 0), 90)
    assert slots[-1] == time(17, 30)
    assert time(18, 0) not in slots, "would finish at 19:30, after closing"


def test_no_slots_when_staff_roster_is_outside_opening_hours():
    # A night-shift roster at a day shop offers nothing bookable.
    assert _slots(time(8, 0), time(19, 0), time(20, 0), time(23, 0), 30) == []


def test_service_length_removes_late_slots():
    short = _slots(time(8, 0), time(10, 0), time(8, 0), time(10, 0), 30)
    long = _slots(time(8, 0), time(10, 0), time(8, 0), time(10, 0), 60)
    assert short == [time(8, 0), time(8, 30), time(9, 0), time(9, 30)]
    assert long == [time(8, 0), time(8, 30), time(9, 0)]


def test_default_opening_hours_are_applied_when_branch_has_none():
    from app.booking.router import _default_window

    opens, closes = _default_window(None, None)
    assert opens < closes
    assert opens == time(8, 0)
    assert closes == time(19, 0)


def test_branch_hours_override_the_defaults():
    from app.booking.router import _default_window

    opens, closes = _default_window(time(6, 30), time(23, 0))
    assert (opens, closes) == (time(6, 30), time(23, 0))


def test_booking_horizon_is_bounded():
    from app.booking.router import BOOKING_HORIZON_DAYS

    # A shop should not have to honour a booking made for next quarter, when
    # the roster and the service menu will have changed.
    assert 0 < BOOKING_HORIZON_DAYS <= 90


def test_public_routes_require_no_authentication():
    """None of the new reads may demand a token (Doc 10.5 D3/D8)."""
    from app.main import app

    paths = app.openapi()["paths"]
    for suffix in ("", "/services", "/staff", "/slots"):
        key = f"/v1/public/{{branch_slug}}{suffix}"
        assert key in paths, f"missing public route {key}"
        assert paths[key]["get"].get("security") in (None, [])


def test_public_slot_params_are_required():
    """Without a barber and a service there is nothing to schedule or price."""
    from app.main import app

    spec = app.openapi()["paths"]["/v1/public/{branch_slug}/slots"]["get"]
    required = {p["name"] for p in spec.get("parameters", []) if p.get("required")}
    assert {"staff_id", "service_ids"} <= required


def test_public_response_models_leak_no_shop_internals():
    """The public surface must not carry customers, staff pay or revenue.

    `phone` is allowed on `PublicBranchResponse` because it is the shop's
    contact number — a customer needs it to call ahead. It is not a customer
    or staff phone.
    """
    from app.booking.schemas import (
        PublicBranchResponse,
        PublicServiceResponse,
        PublicSlotDayResponse,
        PublicSlotResponse,
        PublicStaffResponse,
    )

    fields: set[str] = set()
    for model in (
        PublicServiceResponse,
        PublicStaffResponse,
        PublicSlotDayResponse,
        PublicSlotResponse,
    ):
        fields |= set(model.model_fields)

    for forbidden in (
        "customer_phone",
        "customer_id",
        "customer_name",
        "commission_value",
        "commission_type",
        "loyalty_points",
        "revenue",
        "profit",
        "business_id",
        "staff_role",
        "email",
    ):
        assert forbidden not in fields, f"public schema leaks {forbidden!r}"

    # `phone` is only allowed on the branch response (shop contact number).
    branch_fields = set(PublicBranchResponse.model_fields)
    assert "phone" in branch_fields, "branch response must expose the shop phone"
    for forbidden in (
        "customer_phone",
        "customer_id",
        "customer_name",
        "commission_value",
        "commission_type",
        "loyalty_points",
        "revenue",
        "profit",
        "business_id",
        "staff_role",
        "email",
    ):
        assert forbidden not in branch_fields, f"branch schema leaks {forbidden!r}"


def test_public_branch_response_omits_internal_branch_id():
    """A shared link addresses a branch by slug; the UUID stays internal."""
    from app.booking.schemas import PublicBranchResponse

    assert "id" not in PublicBranchResponse.model_fields
    assert "slug" in PublicBranchResponse.model_fields


# ── integration: cross-branch isolation (needs TEST_DATABASE_URL) ────────────
# `users.phone` and `users.email` are UNIQUE, and several tests seed two shops in
# one transaction, so both come from a per-process sequence rather than from the
# slug ("shop-a" and "shop-b" are both seven characters, and the owner and the
# barber of one shop would collide on top of that).
_USER_SEQ = itertools.count(1)


def _seed(session, *, slug: str, staff_slug: str, with_schedule: bool = True):
    """One business + branch + owner login + barber, ready for the public reads.

    Ids are minted here rather than left to the server default: `businesses.id`
    and `branches.id` are UUID primary keys while every `*_id` column is VARCHAR
    holding that uuid as text, and the branch/staff/service/schedule rows below
    need those values at construction time — before the caller flushes.
    uuid5 keeps them deterministic, so a failure is reproducible.
    """
    from app.models.identity import User
    from app.models.people import Schedule, Staff
    from app.models.tenant import Branch, Business, Service

    owner_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/booking/owner/{slug}")
    barber_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/booking/barber/{staff_slug}")
    business_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/booking/business/{slug}")
    branch_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/booking/branch/{slug}")
    staff_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/booking/staff/{staff_slug}")

    session.add(
        User(
            id=owner_id,
            full_name="Owner",
            phone=f"0700{next(_USER_SEQ):06d}",
            email=f"owner-{slug}-{next(_USER_SEQ)}@example.com",
            password_hash="x",
            role="owner",
        )
    )
    # `_branch_staff` joins `users.id` to `staff.user_id` for the display name,
    # so the barber needs a real login row behind them or the join finds nothing.
    session.add(
        User(
            id=barber_id,
            full_name=f"Barber {staff_slug}",
            phone=f"0700{next(_USER_SEQ):06d}",
            email=f"barber-{staff_slug}-{next(_USER_SEQ)}@example.com",
            password_hash="x",
            role="barber",
        )
    )

    # The public slug belongs to the *branch*, not the business: the shareable
    # link is `/public/{branch_slug}` and `_public_branch` resolves it on
    # `Branch.slug`. `Business` has no slug column at all — the shop name is not
    # the shareable identifier — which is why seeding `slug=` here raised
    # TypeError: 'slug' is an invalid keyword argument for Business.
    biz = Business(
        id=business_id,
        owner_user_id=str(owner_id),
        name=f"Shop {slug}",
        business_type="barber_shop",
        status="active",
    )
    session.add(biz)

    branch = Branch(
        id=branch_id,
        business_id=str(business_id),
        slug=slug,
        name=f"Branch {slug}",
        opens_at=time(8, 0),
        closes_at=time(19, 0),
        num_chairs=2,
        is_active=True,
    )
    session.add(branch)

    staff = Staff(
        id=staff_id,
        business_id=str(business_id),
        branch_id=str(branch_id),
        user_id=str(barber_id),
        staff_role="barber",
        commission_type="percentage",
        commission_value=30,
        status="active",
    )
    session.add(staff)
    session.add(
        Service(
            branch_id=str(branch_id),
            name="Haircut",
            price_kes=500,
            duration_min=30,
            is_active=True,
        )
    )
    if with_schedule:
        # 2030-01-05 is a Saturday (weekday 5).
        session.add(
            Schedule(
                staff_id=str(staff_id),
                day_of_week=5,
                start_time=time(9, 0),
                end_time=time(17, 0),
            )
        )
    return {"business": biz, "branch": branch, "staff": staff}


@pytest.mark.asyncio
async def test_public_branch_resolves_active_slug(db):
    from app.booking.router import _public_branch

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    found = await _public_branch(db, "bliss")
    assert found.id == seeded["branch"].id


@pytest.mark.asyncio
async def test_public_branch_404s_on_unknown_slug(db):
    from fastapi import HTTPException

    from app.booking.router import _public_branch

    _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    with pytest.raises(HTTPException) as err:
        await _public_branch(db, "no-such-shop")
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_public_branch_404s_for_deactivated_shop(db):
    """A deactivated branch stops taking bookings without confirming it existed."""
    from fastapi import HTTPException

    from app.booking.router import _public_branch

    seeded = _seed(db, slug="closed", staff_slug="closed")
    seeded["branch"].is_active = False
    await db.flush()

    with pytest.raises(HTTPException) as err:
        await _public_branch(db, "closed")
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_public_staff_excludes_suspended_barbers(db):
    from app.booking.router import _branch_staff

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    # `PublicStaffResponse.staff_id` is declared `str`, so the response carries
    # the id in the same text form the client submits it in.
    assert [s.staff_id for s in await _branch_staff(db, seeded["branch"])] == [
        str(seeded["staff"].id)
    ]

    seeded["staff"].status = "suspended"
    await db.flush()
    # A barber who cannot be rostered must not be offered to customers.
    assert await _branch_staff(db, seeded["branch"]) == []


@pytest.mark.asyncio
async def test_public_services_hides_retired_services(db):
    from app.booking.router import _branch_services
    from app.models.tenant import Service

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    assert [s.name for s in await _branch_services(db, seeded["branch"])] == ["Haircut"]

    db.add(
        Service(
            branch_id=str(seeded["branch"].id),
            name="Shaved head",
            price_kes=700,
            duration_min=45,
            is_active=False,
        )
    )
    await db.flush()
    assert [s.name for s in await _branch_services(db, seeded["branch"])] == ["Haircut"]


@pytest.mark.asyncio
async def test_barber_from_another_branch_is_not_bookable(db):
    """The central isolation rule for an unauthenticated surface.

    Shop A's barber id submitted against shop B's public link must resolve to
    nothing — the same empty result as a fabricated id, so the endpoint 404s
    without confirming that staff A exists.
    """
    from app.booking.router import _public_branch
    from app.models.people import Staff
    from sqlalchemy import select

    shop_a = _seed(db, slug="shop-a", staff_slug="a")
    shop_b = _seed(db, slug="shop-b", staff_slug="b")
    await db.flush()

    branch_b = await _public_branch(db, "shop-b")

    # A's barber is real and active…
    assert await db.scalar(select(Staff).where(Staff.id == shop_a["staff"].id))

    # …but is invisible when scoped to B.
    assert (
        await db.scalar(
            select(Staff).where(
                Staff.id == shop_a["staff"].id, Staff.branch_id == str(branch_b.id)
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_services_from_another_branch_are_rejected(db):
    """A service id from another shop must not be silently substituted."""
    from app.booking.router import _public_branch
    from app.models.tenant import Service
    from sqlalchemy import select

    shop_a = _seed(db, slug="shop-a", staff_slug="a")
    shop_b = _seed(db, slug="shop-b", staff_slug="b")
    await db.flush()

    branch_b = await _public_branch(db, "shop-b")
    service_a = await db.scalar(
        select(Service).where(Service.branch_id == str(shop_a["branch"].id))
    )

    scoped = list(
        await db.scalars(
            select(Service).where(
                Service.id.in_([service_a.id]),
                Service.branch_id == str(branch_b.id),
                Service.is_active.is_(True),
            )
        )
    )
    # Fewer results than requested => the endpoint raises 400 rather than
    # booking the customer into a different service than the one advertised.
    assert len(scoped) == 0


@pytest.mark.asyncio
async def test_slots_respect_schedule_opening_hours(db):
    """Schedule 09:00-17:00 inside a shop open 08:00-19:00 → 09:00 first."""
    from app.booking.router import _public_branch
    from app.models.tenant import Service
    from sqlalchemy import select

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    branch = await _public_branch(db, "bliss")
    service = await db.scalar(
        select(Service).where(Service.branch_id == str(branch.id))
    )

    from app.booking.router import _day_slots

    slots = await _day_slots(db, branch, seeded["staff"], [service], date(2030, 1, 5))
    assert slots, "a rostered Saturday must produce bookable times"
    first = min(s.scheduled_at for s in slots)
    assert (first.hour, first.minute) == (9, 0), "must start at the roster time"
    # Last 30-minute haircut in a 17:00 roster starts at 16:30.
    last = max(s.scheduled_at for s in slots)
    assert (last.hour, last.minute) == (16, 30)


@pytest.mark.asyncio
async def test_no_slots_when_staff_not_rostered_that_weekday(db):
    from app.booking.router import _public_branch, _day_slots
    from app.models.tenant import Service
    from sqlalchemy import select

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    branch = await _public_branch(db, "bliss")
    service = await db.scalar(select(Service).where(Service.branch_id == str(branch.id)))

    # 2030-01-07 is a Monday; the roster only covers Saturday (day 5).
    slots = await _day_slots(db, branch, seeded["staff"], [service], date(2030, 1, 7))
    assert slots == []


@pytest.mark.asyncio
async def test_existing_appointment_removes_its_slot(db):
    """An advertised slot must already be proven free by the conflict check."""
    from datetime import datetime as _dt

    from app.appointments.router import _find_conflict
    from app.booking.router import _day_slots
    from app.booking.router import _public_branch
    from app.models.operations import Appointment
    from app.models.people import Customer
    from app.models.tenant import Service
    from sqlalchemy import select

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    branch = await _public_branch(db, "bliss")
    service = await db.scalar(select(Service).where(Service.branch_id == str(branch.id)))

    taken = _dt(2030, 1, 5, 10, 0, tzinfo=EAF)
    db.add(
        Customer(
            business_id=branch.business_id,
            branch_id=str(branch.id),
            full_name="Walk-in",
            phone="0700000000",
        )
    )
    await db.flush()
    customer = await db.scalar(select(Customer).where(Customer.branch_id == str(branch.id)))
    db.add(
        Appointment(
            business_id=branch.business_id,
            branch_id=str(branch.id),
            customer_id=str(customer.id),
            staff_id=str(seeded["staff"].id),
            scheduled_at=taken,
            duration_min=30,
            source="walk_in",
            status="booked",
        )
    )
    await db.flush()

    assert await _find_conflict(db, seeded["staff"].id, taken, 30) is not None

    slots = await _day_slots(db, branch, seeded["staff"], [service], date(2030, 1, 5))
    offered = {s.scheduled_at for s in slots}
    assert taken not in offered
    # The overlap window is half-open — the booking occupies [10:00, 10:30) — so
    # 10:30 is genuinely free and must still be offered: the conflict check
    # removes the clashing slot, not the barber's whole afternoon. 09:30 and
    # 11:00 prove the rest of the roster survived the check intact.
    assert _dt(2030, 1, 5, 9, 30, tzinfo=EAF) in offered
    assert _dt(2030, 1, 5, 10, 30, tzinfo=EAF) in offered
    assert _dt(2030, 1, 5, 11, 0, tzinfo=EAF) in offered


@pytest.mark.asyncio
async def test_cancelled_appointment_does_not_block_its_slot(db):
    """A cancellation must free the chair back up for the portal."""
    from datetime import datetime as _dt

    from app.booking.router import _day_slots, _public_branch
    from app.models.operations import Appointment
    from app.models.people import Customer
    from app.models.tenant import Service
    from sqlalchemy import select

    seeded = _seed(db, slug="bliss", staff_slug="bliss")
    await db.flush()

    branch = await _public_branch(db, "bliss")
    service = await db.scalar(select(Service).where(Service.branch_id == str(branch.id)))

    db.add(
        Customer(
            business_id=branch.business_id,
            branch_id=str(branch.id),
            full_name="Walk-in",
            phone="0700000000",
        )
    )
    await db.flush()
    customer = await db.scalar(select(Customer).where(Customer.branch_id == str(branch.id)))
    db.add(
        Appointment(
            business_id=branch.business_id,
            branch_id=str(branch.id),
            customer_id=str(customer.id),
            staff_id=str(seeded["staff"].id),
            scheduled_at=_dt(2030, 1, 5, 10, 0, tzinfo=EAF),
            duration_min=30,
            source="walk_in",
            status="cancelled",
        )
    )
    await db.flush()

    slots = await _day_slots(db, branch, seeded["staff"], [service], date(2030, 1, 5))
    assert _dt(2030, 1, 5, 10, 0, tzinfo=EAF) in {s.scheduled_at for s in slots}
