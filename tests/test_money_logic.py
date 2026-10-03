"""Unit tests for the money-critical pure logic in Groups F, G, H.

Scope, stated plainly: these test the *calculations and verification rules*, which
is where a silent error costs a barber real money — commission rounding, points
maths, phone canonicalisation, the Pesapal signature, report period boundaries.

They do NOT test the sale transaction itself. The models use PostgreSQL JSONB, so
exercising `POST /sales` end to end needs a real Postgres (or aiosqlite plus a
JSONB shim), not a unit test. The fan-out's transactionality is therefore
unverified here and needs integration tests before launch — see the note at the
bottom of this file.
"""

from datetime import date, timedelta

import pytest

from app.core.payments import daraja_callback_is_ours, verify_pesapal_ipn_signature
from app.core.phone import normalize_phone
from app.models.people import Staff
from app.reports.router import _window
from app.sales.router import POINTS_PER_100_KES, _commission_kes


# ── commission ─────────────────────────────────────────────────────────────
def _staff(kind: str, value: int) -> Staff:
    """A Staff row carrying only the fields _commission_kes reads.

    The declarative constructor sets attributes without a session, so this needs
    no database — a unit test that has to provision one to check arithmetic is a
    test that will not get run.
    """
    return Staff(commission_type=kind, commission_value=value)


def test_percentage_commission_is_a_fifth_of_the_line():
    assert _commission_kes(_staff("percentage", 20), 1_000) == 200


def test_percentage_commission_rounds_down_never_up():
    # 333 * 20% = 66.6. Paying 67 would be inventing money; 66 is the honest floor.
    assert _commission_kes(_staff("percentage", 20), 333) == 66


def test_fixed_commission_does_not_scale_with_price():
    # A flat 200-per-service barber gets 200 whether the cut took 3 or 5 minutes.
    assert _commission_kes(_staff("fixed", 200), 300) == 200
    assert _commission_kes(_staff("fixed", 200), 5_000) == 200


def test_zero_rate_pays_nothing():
    assert _commission_kes(_staff("percentage", 0), 10_000) == 0


# ── loyalty points ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "total_kes, expected",
    [
        (0, 0),
        (99, 0),  # below one full 100 — earns nothing
        (100, 1),
        (199, 1),  # still only one whole 100
        (1_000, 10),
        (1_500, 15),
    ],
)
def test_points_award_one_per_whole_hundred_spent(total_kes, expected):
    assert (total_kes // 100) * POINTS_PER_100_KES == expected


# ── phone normalisation ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    [
        "0712345678",
        "+254712345678",
        "254712345678",
        "0712 345 678",
        "+254 712 345 678",
        "254-712-345-678",
    ],
)
def test_every_way_of_writing_a_number_yields_one_canonical_form(raw):
    # If these ever diverge, dedupe and loyalty lookup silently stop matching.
    assert normalize_phone(raw) == "+254712345678"


# ── report period windows ───────────────────────────────────────────────────
def test_weekly_window_starts_on_monday_and_is_seven_days():
    # 2026-09-28 is a Monday.
    monday = date(2026, 9, 28)
    start, end, label = _window("weekly", monday)

    assert start == monday
    assert end == monday + timedelta(days=7)
    assert start.weekday() == 0
    assert label == "week"


def test_weekly_window_anchors_back_to_monday_from_a_midweek_date():
    # A shop opening its dashboard on Wednesday still gets the whole week, not
    # a Wednesday-to-Wednesday window that silently drops Mon and Tue.
    wednesday = date(2026, 9, 30)
    start, end, _ = _window("weekly", wednesday)

    assert start == date(2026, 9, 28)
    assert end == date(2026, 10, 5)


def test_monthly_window_spans_the_whole_month_including_short_february():
    start, end, label = _window("monthly", date(2026, 2, 14))

    assert start == date(2026, 2, 1)
    assert end == date(2026, 3, 1)  # 2026 is not a leap year
    assert label == "month"


def test_monthly_window_handles_a_31_day_month():
    _, end, _ = _window("monthly", date(2026, 1, 31))
    assert end == date(2026, 2, 1)


def test_unknown_period_degrades_to_a_single_day_rather_than_raising():
    # A bad query param must not 500 the owner's dashboard.
    start, end, label = _window("fortnightly", date(2026, 9, 28))

    assert start == date(2026, 9, 28)
    assert end == date(2026, 9, 29)
    assert label == "day"


# ── Daraja callback shape check ─────────────────────────────────────────────
def test_daraja_callback_without_a_checkout_id_is_rejected():
    # The reference is the only thing tying a callback to a payment we issued.
    assert not daraja_callback_is_ours(
        event_ref=None, account_ref="pay_1", amount_kes=1000
    )


def test_daraja_callback_without_an_account_reference_is_rejected():
    assert not daraja_callback_is_ours(
        event_ref="chk_1", account_ref=None, amount_kes=1000
    )


def test_daraja_callback_claiming_a_non_positive_amount_is_rejected():
    assert not daraja_callback_is_ours(
        event_ref="chk_1", account_ref="pay_1", amount_kes=0
    )


def test_well_formed_daraja_callback_passes_the_shape_check():
    assert daraja_callback_is_ours(
        event_ref="chk_1", account_ref="pay_1", amount_kes=1_000
    )


# ── Pesapal IPN signature ──────────────────────────────────────────────────
def _sign(
    notification_type: str, notification_id: str, details: str, merchant_id: str
) -> str:
    import base64
    import hashlib

    payload = f"{notification_type}{notification_id}{details}{merchant_id}"
    return base64.b64encode(hashlib.sha512(payload.encode()).digest()).decode()


def test_pesapal_ipn_with_a_correct_signature_verifies(monkeypatch):
    from app.core import payments

    monkeypatch.setattr(payments.settings, "pesapal_merchant_id", "MERCHANT-1")
    details = '{"merchant_reference":"pay_1","status":"COMPLETED"}'

    assert payments.verify_pesapal_ipn_signature(
        notification_type="Transaction Success",
        notification_id="notif-1",
        notification_details=details,
        signature=_sign("Transaction Success", "notif-1", details, "MERCHANT-1"),
    )


def test_pesapal_ipn_with_a_tampered_amount_fails_verification(monkeypatch):
    # This is the whole point of the signature: someone replaying a real
    # notification with a different amount must not validate.
    from app.core import payments

    monkeypatch.setattr(payments.settings, "pesapal_merchant_id", "MERCHANT-1")
    original = '{"merchant_reference":"pay_1","amount":"1000","status":"COMPLETED"}'
    tampered = '{"merchant_reference":"pay_1","amount":"1","status":"COMPLETED"}'

    assert not payments.verify_pesapal_ipn_signature(
        notification_type="Transaction Success",
        notification_id="notif-1",
        notification_details=tampered,
        signature=_sign("Transaction Success", "notif-1", original, "MERCHANT-1"),
    )


def test_pesapal_ipn_signed_against_another_merchant_fails(monkeypatch):
    from app.core import payments

    monkeypatch.setattr(payments.settings, "pesapal_merchant_id", "MERCHANT-2")
    details = '{"status":"COMPLETED"}'

    assert not payments.verify_pesapal_ipn_signature(
        notification_type="Transaction Success",
        notification_id="notif-1",
        notification_details=details,
        signature=_sign("Transaction Success", "notif-1", details, "MERCHANT-1"),
    )


def test_pesapal_ipn_missing_any_field_fails_closed(monkeypatch):
    from app.core import payments

    monkeypatch.setattr(payments.settings, "pesapal_merchant_id", "MERCHANT-1")

    assert not payments.verify_pesapal_ipn_signature(
        notification_type="",
        notification_id="notif-1",
        notification_details="{}",
        signature="anything",
    )


# ── the gap ────────────────────────────────────────────────────────────────
# NOT covered here, and required before launch:
#
#   - POST /sales fan-out: that commission, stock, and loyalty all land together,
#     and that an oversell aborts the whole sale.
#   - Webhook replay: that delivering the same Daraja callback twice extends the
#     subscription once, not twice. `_apply_payment` short-circuits on
#     status == "success" and this is the only thing guarding it.
#   - Cross-tenant 404: that one shop cannot read another's customers or sales.
#
# All three need a real Postgres. The models' JSONB columns rule out SQLite.
