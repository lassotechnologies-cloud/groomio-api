"""Billing & payment regression tests (T-004.6).

Each test pins one acceptance criterion from the T-004 audit
(docs/T-004-subscription-billing-audit.md §18). The audit reproduced
every defect against the isolated test database; these tests make sure
the fixes stay fixed.

What is under test, by defect id:

    T004-01  a forged Daraja callback (wrong CheckoutRequestID) is
             stored but changes nothing
    T004-02  POST /v1/subscription/pay returns 202, not 500
    T004-03  Pesapal IPN signatures are space-joined
    T004-04  an expired business can re-subscribe
    T004-05  every payment method enum value inserts
    T004-06  concurrent identical callbacks extend the subscription once
    T004-09  the STK push URL is the push endpoint
    T004-10  a second identical initiation returns the same payment
    T004-12  an expired/suspended tenant is refused on business-scoped
             endpoints (402/403)
    T004-13  admin trial-extend advances the subscription period
    T004-14  a successful payment marks the business active
    T004-16  the timeout sweep closes stale pending payments

The `db`-fixture tests share one rolled-back transaction with the app
(the `client` fixture below). The one true-concurrency test uses the
`concurrency_client` fixture, because a check-then-act race cannot be
expressed on a single connection inside a single transaction.
"""

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.payments import verify_pesapal_ipn_signature
from app.core.security import (
    create_access_token,
    hash_otp,
    now_utc,
)
from app.models.billing import Payment, Plan, Subscription
from app.models.identity import OTPCode, User
from app.models.platform import WebhookEvent
from app.models.tenant import Business

_PASSWORD = "correct-horse-8"
_CHECKOUT_ID = "REAL-CHECKOUT-ID-777"


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
async def client(db):
    """An ASGI client sharing the rolled-back `db` session with the test."""
    from httpx import ASGITransport, AsyncClient

    from app.core.database import get_db
    from app.main import app

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


# ── helpers ────────────────────────────────────────────────────────────


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _verified_owner(client, db, *, phone="0712345678", name="Amina Wanjiru"):
    """Register, verify the OTP, and return `(user_id, access_token)`."""
    from datetime import timedelta as _td

    from app.core.phone import normalize_phone

    resp = await client.post(
        "/v1/auth/register",
        json={
            "full_name": name,
            "phone": phone,
            "email": f"{phone}@example.com",
            "password": _PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["user_id"]

    db.add(
        OTPCode(
            phone=normalize_phone(phone),
            code_hash=hash_otp("123456"),
            purpose="signup_verification",
            channel="sms",
            attempts=0,
            expires_at=now_utc() + _td(minutes=10),
        )
    )
    await db.flush()

    verify = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": phone, "code": "123456"},
    )
    assert verify.status_code == 200, verify.text
    return user_id, verify.json()["access_token"]


async def _seed_plan(db) -> Plan:
    """The plan the trial subscription references.

    `_open_trial_subscription` keys on `code == "daily"` (the seeded
    catalogue's entry-level plan in production), so the test seed must
    use that code. Price and interval are the values the payment tests
    quote back in callbacks.
    """
    plan = Plan(code="daily", price_kes=1000, interval_days=30, is_active=True)
    db.add(plan)
    await db.flush()
    return plan


async def _owner_with_business(client, db, **owner_kwargs):
    """A verified owner whose `POST /v1/business` succeeded.

    Returns `(user_id, token, business, subscription)`. The plan is
    seeded first because the trial subscription references the cheapest
    active plan, and the test database is built with `create_all` —
    no migrations, no seed.
    """
    await _seed_plan(db)
    user_id, token = await _verified_owner(client, db, **owner_kwargs)
    resp = await client.post(
        "/v1/business",
        json={
            "name": "Sharp Cuts Barber",
            "business_type": "barber_shop",
            "county": "Nairobi",
            "town": "Nairobi",
            "branch_name": "Westlands",
            "opens_at": "08:00:00",
            "closes_at": "20:00:00",
            "num_chairs": 6,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    biz = await db.scalar(select(Business).where(Business.owner_user_id == user_id))
    sub = await db.scalar(
        select(Subscription).where(Subscription.business_id == str(biz.id))
    )
    return user_id, token, biz, sub


def _daraja_callback(
    *,
    checkout_id: str,
    account_ref: str,
    amount: int = 1000,
    result_code: int = 0,
    receipt: str = "QJFAKE00000000",
) -> dict:
    return {
        "Body": {
            "stkCallback": {
                "ResultCode": result_code,
                "ResultDesc": "Accept: Service Submitted Successfully.",
                "CheckoutRequestID": checkout_id,
                "AccountReference": account_ref,
                "MpesaReceiptNumber": receipt,
                "Amount": amount,
            }
        }
    }


async def _pending_mpesa_payment(db, biz, sub, *, provider_ref=_CHECKOUT_ID):
    """A payment as `start_payment` leaves it: pending, with the
    CheckoutRequestID Safaricom issued stored as `provider_ref`."""
    payment = Payment(
        business_id=str(biz.id),
        subscription_id=sub.id,
        amount_kes=1000,
        method="mpesa",
        provider="daraja",
        provider_ref=provider_ref,
        status="pending",
    )
    db.add(payment)
    await db.flush()
    return payment


# ── T004-09 — configuration ────────────────────────────────────────────


def test_the_stk_push_url_is_the_push_endpoint():
    """The default used to point at the STK *Query* endpoint, so every
    push was sent to the wrong URL and could never succeed."""
    assert settings.mpesa_stk_push_url.endswith("/mpesa/stkpush/v1/processrequest")


# ── T004-02 / T004-10 — payment initiation ─────────────────────────────


async def test_payment_initiation_returns_202_and_stores_the_checkout_id(
    client, db, monkeypatch
):
    """`subscription_id` used to be written as a raw `uuid.UUID` into a
    VARCHAR column, which asyncpg rejects — every initiation was a 500."""

    async def _fake_stk_push(*, phone, amount_kes, account_ref, description):
        return "CHECKOUT-TEST-123"

    monkeypatch.setattr("app.subscriptions.router.daraja_stk_push", _fake_stk_push)

    _, token, biz, sub = await _owner_with_business(client, db)
    plan = await db.scalar(select(Plan).where(Plan.code == "daily"))

    resp = await client.post(
        "/v1/subscription/pay",
        json={"plan_id": str(plan.id), "method": "mpesa"},
        headers=_auth(token),
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["state"] == "awaiting_mpesa"
    assert body["payment_id"]

    payment = await db.scalar(select(Payment).where(Payment.id == body["payment_id"]))
    assert payment is not None
    # The CheckoutRequestID is stored at initiation — it is the only
    # thing a later callback can be authenticated against (T004-01).
    assert payment.provider_ref == "CHECKOUT-TEST-123"
    assert payment.status == "pending"
    assert str(payment.subscription_id) == str(sub.id)

    # H6 — the popup can poll what it just started.
    poll = await client.get(f"/v1/payments/{body['payment_id']}", headers=_auth(token))
    assert poll.status_code == 200, poll.text
    assert poll.json()["status"] == "pending"


async def test_a_second_identical_initiation_returns_the_same_payment(
    client, db, monkeypatch
):
    """A double-tap on "Pay" used to create two STK pushes — two
    prompts, one payment, and a customer charged twice if they
    confirmed both."""

    calls = []

    async def _fake_stk_push(*, phone, amount_kes, account_ref, description):
        calls.append(account_ref)
        return f"CHECKOUT-{len(calls)}"

    monkeypatch.setattr("app.subscriptions.router.daraja_stk_push", _fake_stk_push)

    _, token, biz, sub = await _owner_with_business(client, db)
    plan = await db.scalar(select(Plan).where(Plan.code == "daily"))
    headers = _auth(token)
    body = {"plan_id": str(plan.id), "method": "mpesa"}

    first = await client.post("/v1/subscription/pay", json=body, headers=headers)
    second = await client.post("/v1/subscription/pay", json=body, headers=headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert second.json()["payment_id"] == first.json()["payment_id"]
    # Exactly one STK push was sent.
    assert len(calls) == 1


# ── T004-01 — forged callback ──────────────────────────────────────────


async def test_a_forged_daraja_callback_does_not_activate_anything(client, db):
    """The P0. A callback whose CheckoutRequestID is not the one we
    issued is stored as evidence but must not move money or time."""
    _, token, biz, sub = await _owner_with_business(client, db)
    payment = await _pending_mpesa_payment(db, biz, sub)
    period_end_before = sub.current_period_end

    resp = await client.post(
        "/v1/payments/webhooks/daraja",
        json=_daraja_callback(
            checkout_id="ATTACKER-INVENTED-CHECKOUT-ID",
            account_ref=str(payment.id),
        ),
    )
    assert resp.status_code == 200, resp.text

    # Store-first: the event is on record even though it was rejected.
    event = await db.scalar(
        select(WebhookEvent).where(
            WebhookEvent.event_ref == "ATTACKER-INVENTED-CHECKOUT-ID"
        )
    )
    assert event is not None
    assert event.processed_at is None

    await db.refresh(payment)
    await db.refresh(sub)
    await db.refresh(biz)
    assert payment.status == "pending"
    assert sub.status == "trial"
    assert sub.current_period_end == period_end_before
    assert biz.status == "trialing"


async def test_a_genuine_daraja_callback_activates_the_subscription(client, db):
    """The positive control: a callback quoting the CheckoutRequestID we
    stored, the amount we asked for, and result code 0."""
    _, token, biz, sub = await _owner_with_business(client, db)
    payment = await _pending_mpesa_payment(db, biz, sub)

    resp = await client.post(
        "/v1/payments/webhooks/daraja",
        json=_daraja_callback(checkout_id=_CHECKOUT_ID, account_ref=str(payment.id)),
    )
    assert resp.status_code == 200, resp.text

    await db.refresh(payment)
    await db.refresh(sub)
    await db.refresh(biz)
    assert payment.status == "success"
    assert payment.paid_at is not None
    assert sub.status == "active"
    # A month, measured from the trial's end (renewal keeps remaining days).
    assert sub.current_period_end == sub.current_period_start + timedelta(days=30)
    # T004-14 — the business mirror moves with the subscription.
    assert biz.status == "active"


async def test_a_callback_quoting_the_wrong_amount_fails_the_payment(client, db):
    """A success callback for KES 1 quoting a KES 1,000 payment is not
    our success."""
    _, token, biz, sub = await _owner_with_business(client, db)
    payment = await _pending_mpesa_payment(db, biz, sub)

    resp = await client.post(
        "/v1/payments/webhooks/daraja",
        json=_daraja_callback(
            checkout_id=_CHECKOUT_ID, account_ref=str(payment.id), amount=1
        ),
    )
    assert resp.status_code == 200, resp.text

    await db.refresh(payment)
    await db.refresh(sub)
    assert payment.status == "failed"
    assert sub.status == "trial"


# ── T004-06 — concurrent callbacks (real concurrency) ──────────────────


async def test_concurrent_identical_callbacks_extend_the_subscription_once(
    concurrency_client, sessions
):
    """Two simultaneous success callbacks for the same payment — the
    at-least-once delivery every provider does — must grant exactly one
    month. This needs `concurrency_client`: the `db` fixture shares one
    connection inside one transaction, which cannot interleave."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    async with sessions() as db:
        plan = Plan(code="monthly", price_kes=1000, interval_days=30, is_active=True)
        db.add(plan)
        await db.flush()
        owner = User(
            full_name="Amina Wanjiru",
            phone="+254712345678",
            email="amina@example.test",
            password_hash="x",
            role="owner",
            status="active",
        )
        db.add(owner)
        await db.flush()
        biz = Business(
            owner_user_id=str(owner.id),
            name="Sharp Cuts",
            business_type="barber_shop",
            status="trialing",
            trial_ends_at=now + timedelta(days=10),
            chat_enabled=True,
        )
        db.add(biz)
        await db.flush()
        sub = Subscription(
            business_id=str(biz.id),
            plan_id=str(plan.id),
            status="trial",
            current_period_start=now,
            current_period_end=now + timedelta(days=10),
        )
        db.add(sub)
        await db.flush()
        payment = Payment(
            business_id=str(biz.id),
            subscription_id=sub.id,
            amount_kes=1000,
            method="mpesa",
            provider="daraja",
            provider_ref=_CHECKOUT_ID,
            status="pending",
        )
        db.add(payment)
        await db.commit()
        biz_id, sub_id, pay_id = str(biz.id), str(sub.id), str(payment.id)
        trial_end = sub.current_period_end

    payload = _daraja_callback(checkout_id=_CHECKOUT_ID, account_ref=pay_id)

    # Two deliveries at the same instant. One wins the webhook_events
    # uniqueness race; the other must not double-extend even if it
    # gets past the insert.
    responses = await asyncio.gather(
        concurrency_client.post("/v1/payments/webhooks/daraja", json=payload),
        concurrency_client.post("/v1/payments/webhooks/daraja", json=payload),
        return_exceptions=True,
    )

    async with sessions() as db:
        sub = await db.scalar(select(Subscription).where(Subscription.id == sub_id))
        payment = await db.scalar(select(Payment).where(Payment.id == pay_id))
        biz = await db.scalar(select(Business).where(Business.id == biz_id))

        assert payment.status == "success"
        assert biz.status == "active"
        # Exactly one extension: the trial end plus one 30-day period,
        # not two.
        assert sub.current_period_end == trial_end + timedelta(days=30)
        assert sub.status == "active"

    # At least one delivery was acknowledged; the duplicate may have
    # lost the insert race, which is the correct outcome.
    assert any(
        r is not None and not isinstance(r, Exception) and r.status_code == 200
        for r in responses
    )


# ── T004-03 — Pesapal signature ────────────────────────────────────────


def test_pesapal_ipn_signature_is_space_joined():
    """Pesapal joins the signed fields with single spaces. The old code
    concatenated them with no separator, so every genuine IPN failed
    verification and no Pesapal payment ever activated."""
    notification_type = "ipn"
    notification_id = "ntf_123"
    notification_details = "amount=1000"
    merchant_id = settings.pesapal_merchant_id

    payload = (
        f"{notification_type} {notification_id} {notification_details} {merchant_id}"
    )
    expected = base64.b64encode(hashlib.sha512(payload.encode()).digest()).decode()

    assert verify_pesapal_ipn_signature(
        notification_type=notification_type,
        notification_id=notification_id,
        notification_details=notification_details,
        signature=expected,
    )

    # The old, broken algorithm must not verify: a signature computed
    # over the unseparated concatenation is exactly what a forgery
    # produced by someone reading the old code would look like.
    broken = f"{notification_type}{notification_id}{notification_details}{merchant_id}"
    wrong = base64.b64encode(hashlib.sha512(broken.encode()).digest()).decode()
    assert not verify_pesapal_ipn_signature(
        notification_type=notification_type,
        notification_id=notification_id,
        notification_details=notification_details,
        signature=wrong,
    )


# ── T004-04 — re-subscription from expired ─────────────────────────────


async def test_an_expired_business_can_restart_payment(client, db, monkeypatch):
    async def _fake_stk_push(*, phone, amount_kes, account_ref, description):
        return "CHECKOUT-RENEW-1"

    monkeypatch.setattr("app.subscriptions.router.daraja_stk_push", _fake_stk_push)

    _, token, biz, sub = await _owner_with_business(client, db)
    sub.status = "expired"
    biz.status = "expired"
    await db.flush()

    plan = await db.scalar(select(Plan).where(Plan.code == "daily"))
    resp = await client.post(
        "/v1/subscription/pay",
        json={"plan_id": str(plan.id), "method": "mpesa"},
        headers=_auth(token),
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "awaiting_mpesa"


# ── T004-12 — access enforcement ───────────────────────────────────────


async def test_an_expired_business_is_refused_with_402(client, db):
    """`expired` is a billing state: 402 tells the client to send the
    owner to the payment screen."""
    _, token, biz, sub = await _owner_with_business(client, db)
    biz.status = "expired"
    await db.flush()

    resp = await client.get("/v1/customers", headers=_auth(token))
    assert resp.status_code == 402, resp.text


async def test_a_suspended_business_is_refused_with_403(client, db):
    """`suspended` is a support decision, not a bill: 403, and payment
    does not lift it."""
    _, token, biz, sub = await _owner_with_business(client, db)
    biz.status = "suspended"
    await db.flush()

    resp = await client.get("/v1/customers", headers=_auth(token))
    assert resp.status_code == 403, resp.text


async def test_a_live_business_still_reaches_business_scoped_endpoints(client, db):
    """The guard must not over-block: trialing, active and grace are all
    live states."""
    _, token, biz, sub = await _owner_with_business(client, db)

    resp = await client.get("/v1/customers", headers=_auth(token))
    assert resp.status_code == 200, resp.text


async def test_the_subscription_endpoints_stay_reachable_when_expired(
    client, db, monkeypatch
):
    """An expired business must still read its subscription and start a
    payment — that is the only way out of `expired`."""

    async def _fake_stk_push(*, phone, amount_kes, account_ref, description):
        return "CHECKOUT-RECOVER-1"

    monkeypatch.setattr("app.subscriptions.router.daraja_stk_push", _fake_stk_push)

    _, token, biz, sub = await _owner_with_business(client, db)
    sub.status = "expired"
    biz.status = "expired"
    await db.flush()

    # H1 reads the *live* subscription; an expired row is history by
    # design, so the detail endpoint reports "no subscription yet"
    # rather than a stale row. The payment endpoint — the way out of
    # `expired` — is what must stay reachable.
    detail = await client.get("/v1/subscription", headers=_auth(token))
    assert detail.status_code == 404, detail.text

    plan = await db.scalar(select(Plan).where(Plan.code == "daily"))
    pay = await client.post(
        "/v1/subscription/pay",
        json={"plan_id": str(plan.id), "method": "mpesa"},
        headers=_auth(token),
    )
    assert pay.status_code == 202, pay.text


# ── T004-05 — enum collision ───────────────────────────────────────────


async def test_every_payment_method_inserts(db):
    """`payments.method` and `sales.payment_method` used to declare the
    same enum type name, so the second table to create_all won and
    `airtel_money`/`bank_transfer` payments 500'd."""
    plan = await _seed_plan(db)
    biz = Business(
        owner_user_id="00000000-0000-0000-0000-000000000001",
        name="Enum Check",
        business_type="barber_shop",
        status="trialing",
        chat_enabled=True,
    )
    db.add(biz)
    await db.flush()
    sub = Subscription(
        business_id=str(biz.id),
        plan_id=str(plan.id),
        status="trial",
        current_period_start=now_utc(),
        current_period_end=now_utc() + timedelta(days=10),
    )
    db.add(sub)
    await db.flush()

    for method in ("mpesa", "airtel_money", "bank_transfer", "pesapal"):
        payment = Payment(
            business_id=str(biz.id),
            subscription_id=sub.id,
            amount_kes=1000,
            method=method,
            provider="manual",
            status="pending",
        )
        db.add(payment)
        await db.flush()
        assert payment.method == method


# ── T004-13 — admin trial extension ────────────────────────────────────


async def test_trial_extend_advances_the_subscription_period(client, db):
    """Support's trial-extend button used to write only
    `businesses.trial_ends_at`, which nothing reads — the lifecycle
    worker keys off `subscriptions.current_period_end`, so the
    extension was a no-op the owner never saw."""
    user_id, token, biz, sub = await _owner_with_business(client, db)
    period_end_before = sub.current_period_end
    trial_end_before = biz.trial_ends_at

    admin_token = create_access_token(
        sub=str(user_id), extra_claims={"role": "super_admin"}
    )
    resp = await client.post(
        "/v1/admin/trial-extend",
        json={"business_id": str(biz.id), "days": 5, "reason": "test"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text

    await db.refresh(sub)
    await db.refresh(biz)
    assert sub.current_period_end == period_end_before + timedelta(days=5)
    assert biz.trial_ends_at == trial_end_before + timedelta(days=5)


# ── T004-16 — pending payment timeout sweep ────────────────────────────


async def test_the_timeout_sweep_closes_stale_pending_payments(
    concurrency_engine, sessions, monkeypatch
):
    """A payment no provider answered within the window is marked
    `timeout`; a fresh one is left alone, and a real success arriving
    later can still re-open it (the reconciler's job)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    async with sessions() as db:
        plan = Plan(code="monthly", price_kes=1000, interval_days=30, is_active=True)
        db.add(plan)
        await db.flush()
        sub = Subscription(
            business_id="biz_timeout",
            plan_id=str(plan.id),
            status="trial",
            current_period_start=now,
            current_period_end=now + timedelta(days=10),
        )
        db.add(sub)
        await db.flush()
        stale = Payment(
            business_id="biz_timeout",
            subscription_id=sub.id,
            amount_kes=1000,
            method="mpesa",
            provider="daraja",
            status="pending",
            created_at=now - timedelta(hours=48),
        )
        fresh = Payment(
            business_id="biz_timeout",
            subscription_id=sub.id,
            amount_kes=1000,
            method="mpesa",
            provider="daraja",
            status="pending",
            created_at=now,
        )
        db.add_all([stale, fresh])
        await db.commit()
        stale_id, fresh_id = str(stale.id), str(fresh.id)

    @asynccontextmanager
    async def _scope():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("app.workers.payment_reconciler.session_scope", _scope)

    from app.workers.payment_reconciler import _timeout_pending

    result = await _timeout_pending()

    assert result["timed_out"] == 1
    async with sessions() as db:
        stale = await db.scalar(select(Payment).where(Payment.id == stale_id))
        fresh = await db.scalar(select(Payment).where(Payment.id == fresh_id))
        assert stale.status == "timeout"
        assert fresh.status == "pending"


# ── T004-20 — the trial countdown ──────────────────────────────────────
#
# The PWA's billing screen shows the remaining trial as a number, and it takes
# that number from this API rather than from a literal in the screen. The
# calculation used to take the *whole* days out of the remaining time, so a
# ten-day trial granted at 09:00 and read at 09:05 said "9 days left" — a day
# short of what the owner was promised, on the screen that makes the promise.


def _trial_ending_in(
    delta: timedelta, *, status: str = "trial"
) -> tuple[Subscription, datetime]:
    """A subscription row that is never persisted — only its dates are read.

    The row and the instant it is read at are returned together on purpose.
    They have to be the *same* instant: a caller that captures "now" first and
    builds the row afterwards gets a row whose end is a few microseconds in the
    future, and a countdown that rounds up reads one day more than it should.
    """
    now = now_utc()
    sub = Subscription(
        business_id="biz_countdown",
        plan_id="plan_countdown",
        status=status,
        current_period_start=now,
        current_period_end=now + delta,
    )
    return sub, now


def test_a_fresh_trial_counts_the_day_it_promised():
    from app.subscriptions.router import _subscription_out

    sub, now = _trial_ending_in(timedelta(days=10))
    out = _subscription_out(sub, "monthly", now)

    assert out.trial_days_remaining == 10
    assert out.days_until_renewal == 10


def test_a_trial_read_seconds_after_it_started_still_counts_ten():
    """The real case: the row was written a moment before it was read.

    Without the rounding up, the owner was told "9 days left" on the screen
    that promised ten.
    """
    from app.subscriptions.router import _subscription_out

    sub, _ = _trial_ending_in(timedelta(days=10))
    out = _subscription_out(sub, "monthly", now_utc())

    assert out.trial_days_remaining == 10


def test_a_trial_with_hours_left_still_counts_a_day():
    """Two hours left is "1 day left", not "0 days left".

    Zero would read as "over" on a screen whose next line says the trial ends
    today, and a floor calculation produced exactly that.
    """
    from app.subscriptions.router import _subscription_out

    sub, now = _trial_ending_in(timedelta(hours=2))
    out = _subscription_out(sub, "monthly", now)

    assert out.trial_days_remaining == 1


def test_an_ended_trial_keeps_its_sign():
    """A period that ended yesterday still reads as past, not as zero.

    The countdown is a countdown, not a clamped number: the PWA's banner
    branches on the sign to say "your trial has ended" instead of "trial
    ends today".
    """
    from app.subscriptions.router import _subscription_out

    sub, now = _trial_ending_in(timedelta(days=-1))
    out = _subscription_out(sub, "monthly", now)

    assert out.trial_days_remaining == -1


def test_a_grace_subscription_reports_no_renewal_countdown():
    """`grace` means the trial ended and the subscription was not paid for.

    The renewal countdown is zero for it — there is no renewal to count down
    to until the owner pays — and the trial number is negative, so the banner
    can say so.
    """
    from app.subscriptions.router import _subscription_out

    sub, now = _trial_ending_in(timedelta(days=-2), status="grace")
    out = _subscription_out(sub, "monthly", now)

    assert out.days_until_renewal == 0
    assert out.is_in_grace is True
    assert out.trial_days_remaining == -2
