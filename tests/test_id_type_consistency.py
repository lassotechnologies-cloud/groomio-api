"""ID type-consistency regression tests (T-008.1).

Every table in this schema uses a PostgreSQL `UUID` primary key but stores
*references* — `business_id`, `branch_id`, `staff_id`, `customer_id`, `user_id` —
as `VARCHAR`. PostgreSQL has no implicit `uuid = character varying` operator, and
asyncpg refuses to bind a `uuid.UUID` where the statement was compiled for `str`.
So any code path that hands an ORM primary key straight to a query on one of those
columns raises at runtime, and any code path that joins the two worlds without a
cast raises `operator does not exist`.

The T-008 integration tests only exercised discovery, public booking and the sale
fan-out. Every other tenant-scoped router was untested against a real database, so
the mismatch survived a fully green suite.

Each test below drives one real endpoint (or worker) against PostgreSQL and asserts
the *product* behaviour — a display name, a persisted line, a rejected
cross-tenant id — rather than the absence of an exception. A test that only proved
"the query did not blow up" would let the next regression reintroduce the same
mismatch in a different shape.

Requires TEST_DATABASE_URL — see tests/conftest.py. Without it these skip.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.appointments.router import (
    create_appointment,
    list_appointments,
    public_book,
    update_appointment_status,
)
from app.appointments.schemas import (
    AppointmentCreateRequest,
    AppointmentUpdateStatusRequest,
    PublicBookingRequest,
    QueueEntryCreateRequest,
)
from app.chairs.router import list_chairs
from app.commission.router import my_commissions, my_live_earnings, set_bonus
from app.core.tenancy import resolve_business_id
from app.customers.router import list_visits
from app.expenses.router import list_expenses
from app.models.identity import User
from app.models.messaging import Notification
from app.models.money import Commission, Expense, Sale, SaleItem
from app.models.operations import Appointment, Chair
from app.models.people import Customer, Schedule, Staff
from app.models.tenant import Branch, Business, Service
from app.queue.router import get_queue, join_queue, public_queue_position, serve_next
from app.reports.router import compare_branches, daily_report
from app.staff.router import (
    check_in,
    create_leave_request,
    decide_leave_request,
    list_leave_requests,
    list_my_leave_requests,
    list_schedules,
    list_staff,
    update_staff,
)
from app.staff.schemas import (
    BonusUpdateRequest,
    LeaveDecisionRequest,
    LeaveRequestCreateRequest,
    StaffUpdateRequest,
)

DAY = date.today() + timedelta(days=7)  # a fixed, future, conflict-free day


def _at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
async def world(db):
    """Two shops, two branches in the first one, a barber, a chair and a customer.

    Rows are committed and re-read because the routers query them back with fresh
    `db.scalar` calls; unflushed identity-map state would make a broken lookup look
    like it happened for the wrong reason.
    """
    owner_user = User(
        full_name="Wanjiru Otieno",
        phone="+254700000001",
        email="owner@kenge.test",
        password_hash="x",
        role="owner",
        status="active",
    )
    barber_user = User(
        full_name="Njoroge Kamau",
        phone="+254700000002",
        email="barber@kenge.test",
        password_hash="x",
        role="barber",
        status="active",
    )
    rival_user = User(
        full_name="Rival Owner",
        phone="+254700000003",
        email="owner@rival.test",
        password_hash="x",
        role="owner",
        status="active",
    )
    rival_barber_user = User(
        full_name="Rival Barber",
        phone="+254700000004",
        email="barber@rival.test",
        password_hash="x",
        role="barber",
        status="active",
    )
    db.add_all([owner_user, barber_user, rival_user, rival_barber_user])
    await db.flush()

    business = Business(
        owner_user_id=str(owner_user.id),
        name="Kenge Hair Studio",
        business_type="barber_shop",
        status="active",
    )
    rival = Business(
        owner_user_id=str(rival_user.id),
        name="Rival Cuts",
        business_type="barber_shop",
        status="active",
    )
    db.add_all([business, rival])
    await db.flush()

    branch = Branch(
        business_id=str(business.id),
        slug=f"kenge-central-{uuid.uuid4().hex[:8]}",
        name="Central",
        num_chairs=2,
    )
    second_branch = Branch(
        business_id=str(business.id),
        slug=f"kenge-east-{uuid.uuid4().hex[:8]}",
        name="East",
        num_chairs=1,
    )
    rival_branch = Branch(
        business_id=str(rival.id),
        slug=f"rival-{uuid.uuid4().hex[:8]}",
        name="Rival Central",
    )
    db.add_all([branch, second_branch, rival_branch])
    await db.flush()

    barber = Staff(
        business_id=str(business.id),
        branch_id=str(branch.id),
        user_id=str(barber_user.id),
        staff_role="barber",
        commission_type="percentage",
        commission_value=20,
        status="active",
    )
    rival_barber = Staff(
        business_id=str(rival.id),
        branch_id=str(rival_branch.id),
        user_id=str(rival_barber_user.id),
        staff_role="barber",
        commission_type="percentage",
        commission_value=20,
        status="active",
    )
    db.add_all([barber, rival_barber])
    await db.flush()

    service = Service(
        branch_id=str(branch.id),
        name="Cut and shave",
        price_kes=1000,
        duration_min=45,
        is_active=True,
    )
    chair = Chair(branch_id=str(branch.id), chair_number=1, status="free")
    schedule = Schedule(
        staff_id=str(barber.id),
        day_of_week=DAY.weekday(),
        start_time=time(8, 0),
        end_time=time(17, 0),
    )
    customer = Customer(
        business_id=str(business.id),
        branch_id=str(branch.id),
        full_name="Achieng Odhiambo",
        phone="+254711000111",
        loyalty_points=0,
    )
    db.add_all([service, chair, schedule, customer])
    await db.commit()

    for row in (
        owner_user,
        barber_user,
        business,
        rival,
        branch,
        second_branch,
        rival_branch,
        barber,
        rival_barber,
        service,
        chair,
        customer,
    ):
        await db.refresh(row)

    return {
        "db": db,
        "owner": {"user_id": str(owner_user.id), "role": "owner"},
        "barber": {"user_id": str(barber_user.id), "role": "barber"},
        "business": business,
        "branch": branch,
        "second_branch": second_branch,
        "barber_row": barber,
        "rival_barber": rival_barber,
        "service": service,
        "chair": chair,
        "customer": customer,
    }


async def _add_colleague(world, **overrides) -> Staff:
    """A second barber in the same business and branch, with no login row.

    `staff.user_id` is VARCHAR, but every write path stores the uuid rendered as
    text — the display-name join casts it straight back to UUID. The fixture
    therefore has to supply a syntactically valid uuid even when no `users` row
    exists for it, exactly as production does.
    """
    db = world["db"]
    fields = {
        "business_id": str(world["business"].id),
        "branch_id": str(world["branch"].id),
        "user_id": str(uuid.uuid4()),
        "staff_role": "barber",
        "commission_type": "percentage",
        "commission_value": 10,
        "status": "active",
    }
    fields.update(overrides)
    row = Staff(**fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _record_sale(world, *, staff_id: str, commission_kes: int = 200) -> Sale:
    db = world["db"]
    sale = Sale(
        business_id=str(world["business"].id),
        branch_id=str(world["branch"].id),
        customer_id=str(world["customer"].id),
        staff_id=staff_id,
        total_kes=1000,
        payment_method="cash",
        created_by=world["owner"]["user_id"],
    )
    db.add(sale)
    await db.commit()
    await db.refresh(sale)
    db.add(
        SaleItem(
            sale_id=str(sale.id),
            item_type="service",
            service_id=str(world["service"].id),
            name="Cut and shave",
            price_kes=1000,
            quantity=1,
            commission_kes=commission_kes,
        )
    )
    await db.commit()
    return sale


# ── tenancy ───────────────────────────────────────────────────────────────────
async def test_business_resolution_works_for_a_barber_token(world):
    """`resolve_business_id` is the single entry point to the caller's tenant.

    Every barbers-and-clerks endpoint goes through it, so a lookup that compares a
    UUID primary key against the VARCHAR `staff.business_id` disables the whole
    authenticated app for anyone who is not the owner.
    """
    db = world["db"]
    assert await resolve_business_id(db, world["barber"]) == str(world["business"].id)


# ── staff ─────────────────────────────────────────────────────────────────────
async def test_staff_directory_shows_the_display_name(world):
    """A staff row carries `user_id`, never a name — resolving it is the product."""
    db = world["db"]
    rows = await list_staff(db=db, user=world["owner"], branch_id=None)
    assert [r.full_name for r in rows] == ["Njoroge Kamau"]
    assert rows[0].phone == "+254700000002"


async def test_renaming_a_staff_member_updates_the_login(world):
    db = world["db"]
    out = await update_staff(
        str(world["barber_row"].id),
        StaffUpdateRequest(full_name="Njoroge K. Maina"),
        db,
        world["owner"],
    )
    assert out.full_name == "Njoroge K. Maina"
    account = await db.get(User, world["barber"]["user_id"])
    assert account.full_name == "Njoroge K. Maina"


async def test_a_barber_can_read_their_own_roster(world):
    """E5 lets a barber read their own schedule and nobody else's."""
    db = world["db"]
    rows = await list_schedules(str(world["barber_row"].id), db, world["barber"])
    assert len(rows) == 1
    assert rows[0].start_time == "08:00:00"


async def test_a_barber_cannot_read_a_colleagues_roster(world):
    db = world["db"]
    other = await _add_colleague(world)

    with pytest.raises(HTTPException) as exc:
        await list_schedules(str(other.id), db, world["barber"])
    assert exc.value.status_code == 403


async def test_a_barber_can_check_themselves_in(world):
    db = world["db"]
    out = await check_in(str(world["barber_row"].id), db, world["barber"])
    assert out.staff_id == str(world["barber_row"].id)
    assert out.check_in_at is not None


async def test_a_barber_sees_their_own_leave_requests(world):
    db = world["db"]
    created = await create_leave_request(
        LeaveRequestCreateRequest(
            leave_type="annual",
            start_date=DAY,
            end_date=DAY + timedelta(days=2),
            reason="Family wedding",
        ),
        db,
        world["barber"],
    )
    assert created.status == "pending"

    rows = await list_my_leave_requests(db, world["barber"])
    assert [r.reason for r in rows] == ["Family wedding"]


async def test_owner_sees_leave_requests_with_staff_names(world):
    db = world["db"]
    await create_leave_request(
        LeaveRequestCreateRequest(
            leave_type="sick", start_date=DAY, end_date=DAY, reason="Malaria"
        ),
        db,
        world["barber"],
    )
    rows = await list_leave_requests(db, world["owner"], status_filter=None)
    assert [r.staff_name for r in rows] == ["Njoroge Kamau"]
    assert rows[0].reason == "Malaria"


async def test_owner_can_approve_a_pending_leave_request(world):
    db = world["db"]
    created = await create_leave_request(
        LeaveRequestCreateRequest(
            leave_type="other", start_date=DAY, end_date=DAY, reason="Errand"
        ),
        db,
        world["barber"],
    )
    out = await decide_leave_request(
        created.id, LeaveDecisionRequest(status="approved"), db, world["owner"]
    )
    assert out.status == "approved"
    assert out.staff_name == "Njoroge Kamau"


# ── appointments ──────────────────────────────────────────────────────────────
async def test_booking_persists_its_priced_service_lines(world):
    """The line rows carry the appointment's price; a booking that cannot write
    them is a booking with no price."""
    db = world["db"]
    out = await create_appointment(
        AppointmentCreateRequest(
            branch_id=str(world["branch"].id),
            customer_id=str(world["customer"].id),
            staff_id=str(world["barber_row"].id),
            scheduled_at=_at(9, 0),
            service_ids=[str(world["service"].id)],
            source="staff",
        ),
        db,
        world["owner"],
    )
    assert out.total_kes == 1000
    assert [line.name for line in out.lines] == ["Cut and shave"]
    assert out.staff_name == "Njoroge Kamau"
    assert out.customer_name == "Achieng Odhiambo"


async def test_calendar_view_lists_the_days_bookings_with_names(world):
    db = world["db"]
    await create_appointment(
        AppointmentCreateRequest(
            branch_id=str(world["branch"].id),
            customer_id=str(world["customer"].id),
            staff_id=str(world["barber_row"].id),
            scheduled_at=_at(11, 0),
            service_ids=[str(world["service"].id)],
        ),
        db,
        world["owner"],
    )
    rows = await list_appointments(
        str(world["branch"].id),
        db,
        world["owner"],
        on_date=DAY.isoformat(),
        staff=None,
        status_filter=None,
    )
    assert len(rows) == 1
    assert rows[0].customer_name == "Achieng Odhiambo"
    assert rows[0].staff_name == "Njoroge Kamau"
    assert rows[0].total_kes == 1000


async def test_a_barber_can_advance_their_own_appointment(world):
    db = world["db"]
    booked = await create_appointment(
        AppointmentCreateRequest(
            branch_id=str(world["branch"].id),
            customer_id=str(world["customer"].id),
            staff_id=str(world["barber_row"].id),
            scheduled_at=_at(13, 0),
            service_ids=[str(world["service"].id)],
        ),
        db,
        world["owner"],
    )
    out = await update_appointment_status(
        booked.id,
        AppointmentUpdateStatusRequest(status="confirmed"),
        db,
        world["barber"],
    )
    assert out.status == "confirmed"


async def test_a_barber_cannot_advance_a_colleagues_appointment(world):
    db = world["db"]
    other = await _add_colleague(world)
    booked = await create_appointment(
        AppointmentCreateRequest(
            branch_id=str(world["branch"].id),
            customer_id=str(world["customer"].id),
            staff_id=str(other.id),
            scheduled_at=_at(14, 0),
            service_ids=[str(world["service"].id)],
        ),
        db,
        world["owner"],
    )
    with pytest.raises(HTTPException) as exc:
        await update_appointment_status(
            booked.id,
            AppointmentUpdateStatusRequest(status="confirmed"),
            db,
            world["barber"],
        )
    assert exc.value.status_code == 403


async def test_booking_rejects_a_barber_from_another_shop(world):
    """Doc 11.3: a clerk passing another shop's ids gets a 404, never data.

    Without the check, one shop's calendar silently absorbs another shop's barber
    and the double-booking guard starts blocking the wrong person.
    """
    db = world["db"]
    with pytest.raises(HTTPException) as exc:
        await create_appointment(
            AppointmentCreateRequest(
                branch_id=str(world["branch"].id),
                customer_id=str(world["customer"].id),
                staff_id=str(world["rival_barber"].id),
                scheduled_at=_at(15, 0),
                service_ids=[str(world["service"].id)],
            ),
            db,
            world["owner"],
        )
    assert exc.value.status_code == 404


async def test_public_booking_persists_the_appointment(world):
    db = world["db"]
    out = await public_book(
        world["branch"].slug,
        PublicBookingRequest(
            customer_name="Grace Wambui",
            customer_phone="0722000333",
            staff_id=str(world["barber_row"].id),
            scheduled_at=_at(10, 0),
            service_ids=[str(world["service"].id)],
        ),
        db,
    )
    assert out.source == "online"
    assert out.customer_name == "Grace Wambui"
    assert out.staff_name == "Njoroge Kamau"
    assert out.total_kes == 1000

    stored = await db.scalar(select(Appointment).where(Appointment.id == out.id))
    assert stored.customer_id == out.customer_id
    assert stored.staff_id == str(world["barber_row"].id)


async def test_public_booking_rejects_a_barber_from_another_shop(world):
    """The portal's slot route already refuses a foreign barber with 404; the
    booking write has to agree, or the guard exists only on the read path."""
    db = world["db"]
    with pytest.raises(HTTPException) as exc:
        await public_book(
            world["branch"].slug,
            PublicBookingRequest(
                customer_name="Grace Wambui",
                customer_phone="0722000333",
                staff_id=str(world["rival_barber"].id),
                scheduled_at=_at(16, 0),
                service_ids=[str(world["service"].id)],
            ),
            db,
        )
    assert exc.value.status_code == 404


# ── queue ─────────────────────────────────────────────────────────────────────
async def test_joining_the_queue_persists_the_entry_with_a_name(world):
    db = world["db"]
    out = await join_queue(
        str(world["branch"].id),
        QueueEntryCreateRequest(customer_id=str(world["customer"].id)),
        db,
        world["owner"],
    )
    assert out.queue_number == 1
    assert out.status == "waiting"
    assert out.customer_name == "Achieng Odhiambo"
    assert out.customer_id == str(world["customer"].id)


async def test_queue_view_lists_waiting_customers_with_names(world):
    db = world["db"]
    await join_queue(
        str(world["branch"].id),
        QueueEntryCreateRequest(customer_id=str(world["customer"].id)),
        db,
        world["owner"],
    )
    rows = await get_queue(str(world["branch"].id), db, world["owner"])
    assert [r.customer_name for r in rows] == ["Achieng Odhiambo"]


async def test_serving_the_next_entry_occupies_a_chair(world):
    db = world["db"]
    entry = await join_queue(
        str(world["branch"].id),
        QueueEntryCreateRequest(customer_id=str(world["customer"].id)),
        db,
        world["owner"],
    )
    served = await serve_next(entry.id, db, world["owner"])
    assert served.status == "serving"
    assert served.chair_id == str(world["chair"].id)

    chair = await db.get(Chair, world["chair"].id)
    assert chair.status == "occupied"


async def test_public_queue_position_is_scoped_to_the_branch(world):
    db = world["db"]
    entry = await join_queue(
        str(world["branch"].id),
        QueueEntryCreateRequest(customer_id=str(world["customer"].id)),
        db,
        world["owner"],
    )
    assert entry.queue_number == 1

    position = await public_queue_position(world["branch"].slug, "0711000111", db)
    assert position.queue_number == 1
    assert position.people_ahead == 0


# ── chairs ────────────────────────────────────────────────────────────────────
async def test_chair_listing_names_the_assigned_barber(world):
    db = world["db"]
    chair = world["chair"]
    chair.assigned_staff_id = str(world["barber_row"].id)
    await db.commit()

    rows = await list_chairs(str(world["branch"].id), db, world["owner"], True)
    assert rows[0].assigned_staff_name == "Njoroge Kamau"


# ── reports ───────────────────────────────────────────────────────────────────
async def test_daily_report_totals_revenue_and_names_the_top_barber(world):
    db = world["db"]
    await _record_sale(world, staff_id=str(world["barber_row"].id))

    report = await daily_report(
        db, world["owner"], on_date=date.today().isoformat(), branch_id=None
    )
    assert report.revenue_kes == 1000
    assert report.sales_count == 1
    assert report.top_barber.staff_name == "Njoroge Kamau"
    assert report.top_barber.commission_kes == 200


async def test_branch_comparison_reports_text_branch_ids(world):
    """`BranchComparison.branch_id` is declared `str`; a raw UUID key would fail
    response validation rather than degrade quietly."""
    db = world["db"]
    rows = await compare_branches(db, world["owner"], "monthly")
    assert {r.branch_id for r in rows} == {
        str(world["branch"].id),
        str(world["second_branch"].id),
    }
    assert all(isinstance(r.branch_id, str) for r in rows)


# ── commissions ───────────────────────────────────────────────────────────────
async def test_a_barber_reads_their_own_commission_rows(world):
    db = world["db"]
    today = date.today()
    db.add(
        Commission(
            staff_id=str(world["barber_row"].id),
            branch_id=str(world["branch"].id),
            period_start=today.replace(day=1),
            period_end=today,
            earned_kes=4200,
            bonus_kes=0,
            status="open",
        )
    )
    await db.commit()

    rows = await my_commissions(db, world["barber"], "month")
    assert [r.earned_kes for r in rows] == [4200]


async def test_a_barber_sees_live_earnings_from_their_sale_items(world):
    db = world["db"]
    await _record_sale(world, staff_id=str(world["barber_row"].id))

    live = await my_live_earnings(db, world["barber"], "month")
    assert live["earned_kes"] == 200
    assert live["staff_id"] == str(world["barber_row"].id)


async def test_owner_can_set_a_bonus_on_an_open_commission(world):
    db = world["db"]
    today = date.today()
    row = Commission(
        staff_id=str(world["barber_row"].id),
        branch_id=str(world["branch"].id),
        period_start=today.replace(day=1),
        period_end=today,
        earned_kes=4200,
        bonus_kes=0,
        status="open",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    out = await set_bonus(
        str(row.id), BonusUpdateRequest(bonus_kes=1500), db, world["owner"]
    )
    assert out.bonus_kes == 1500
    assert out.staff_name == "Njoroge Kamau"


# ── customers ─────────────────────────────────────────────────────────────────
async def test_customer_visit_history_names_the_barber(world):
    db = world["db"]
    booked = await create_appointment(
        AppointmentCreateRequest(
            branch_id=str(world["branch"].id),
            customer_id=str(world["customer"].id),
            staff_id=str(world["barber_row"].id),
            scheduled_at=_at(12, 0),
            service_ids=[str(world["service"].id)],
        ),
        db,
        world["owner"],
    )
    visits = await list_visits(str(world["customer"].id), db, world["owner"], 50)
    assert [v.id for v in visits] == [booked.id]
    assert visits[0].staff_name == "Njoroge Kamau"
    assert visits[0].service_names == ["Cut and shave"]
    assert visits[0].total_kes == 1000


# ── expenses ──────────────────────────────────────────────────────────────────
async def test_expense_listing_scopes_to_the_businesss_branches(world):
    """`expenses.branch_id` is VARCHAR; the branch-id list has to be text."""
    db = world["db"]
    db.add(
        Expense(
            branch_id=str(world["branch"].id),
            category="rent",
            amount_kes=45000,
            incurred_at=date.today(),
            recorded_by=world["owner"]["user_id"],
        )
    )
    await db.commit()

    rows = await list_expenses(
        db, world["owner"], on_date=None, branch_id=None, category=None, days=30
    )
    assert [r.amount_kes for r in rows] == [45000]
    assert rows[0].branch_id == str(world["branch"].id)


# ── workers ───────────────────────────────────────────────────────────────────
async def test_reminder_worker_resolves_the_customer_and_writes_a_notification(
    world, monkeypatch
):
    """The reminder beat selects appointments 45–60 minutes out, then looks the
    customer up by `appointment.customer_id` and writes an inbox row addressed to
    `customer.id`. Both hops cross the VARCHAR boundary."""
    import app.workers.appointment_reminders as reminders

    db = world["db"]
    fixed_now = _at(9, 0, day=date.today())
    db.add(
        Appointment(
            business_id=str(world["business"].id),
            branch_id=str(world["branch"].id),
            customer_id=str(world["customer"].id),
            staff_id=str(world["barber_row"].id),
            scheduled_at=fixed_now + timedelta(minutes=50),
            duration_min=45,
            source="online",
            status="confirmed",
        )
    )
    await db.commit()

    @asynccontextmanager
    async def _scope():
        yield db

    monkeypatch.setattr(reminders, "session_scope", _scope)
    monkeypatch.setattr(reminders, "_now", lambda: fixed_now)

    result = await reminders._remind()
    assert result["reminded"] == 1

    note = await db.scalar(
        select(Notification).where(
            Notification.recipient_customer_id == str(world["customer"].id)
        )
    )
    assert note is not None
    assert note.type == "appointment"
    assert note.body == ("Hi Achieng, your appointment is in about 50 minutes.")
