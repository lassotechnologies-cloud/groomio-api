"""Payment-application rules for subscriptions (H3–H5).

`_apply_payment` is the only function in the codebase that grants a customer paid
time, so its guard is the highest-consequence line in the billing flow. The tests
below pin two properties:

  1. Only a `pending` payment may extend a subscription.
  2. Applying the same payment twice does not extend it twice.

Property 1 exists because of a real bug. The guard used to read
`if payment.status == "success": return`, which meant a payment sitting in
`failed` fell *through* the check and was applied anyway. The Daraja webhook sets
`status = "failed"` for result codes 1032 (user cancelled) and 1037 (PIN timeout)
and then called `_apply_payment` unconditionally — so a barber who cancelled at
the PIN prompt still received a paid month, and the payment was flipped back to
`success` on the way out. The test named
`test_a_failed_daraja_callback_never_extends_the_subscription` is the regression
guard for that.

The fakes below stand in for the two queries `_apply_payment` makes. They are
deliberately not a mock framework: the point is to observe what the function
does to a Subscription, and a stub session returning a known row does that
without asserting on call counts, which would break on a harmless refactor.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.billing import Payment, Plan, Subscription
from app.subscriptions.router import _apply_payment

DAY = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeSession:
    """Answers `_apply_payment`'s two `db.scalar` calls with fixed rows.

    Results are queued in call order: Subscription first, then Plan. Relying on
    order rather than trying to introspect the query is deliberate — an earlier
    version matched on the mapped class by digging through `_raw_columns`, which
    is private API and broke the moment SQLAlchemy changed. Keying on call order
    breaks only if the function's query order changes, which is visible in review.
    """

    def __init__(self, sub: Subscription | None, plan: Plan | None = None):
        self._results = [
            sub,
            plan or Plan(id="plan_monthly", interval_days=DAY, price_kes=1000),
        ]
        self.flushes = 0

    async def scalar(self, query):  # noqa: ARG002 — the query itself is not under test
        return self._results.pop(0)

    async def flush(self) -> None:
        self.flushes += 1


def _payment(status: str = "pending") -> Payment:
    return Payment(
        id="pay_1",
        business_id="biz_1",
        subscription_id="sub_1",
        amount_kes=1000,
        status=status,
    )


def _subscription(status: str = "active", days_left: int = 0) -> Subscription:
    end = _now() + timedelta(days=days_left)
    return Subscription(
        id="sub_1",
        business_id="biz_1",
        plan_id="plan_monthly",
        status=status,
        current_period_start=end - timedelta(days=DAY),
        current_period_end=end,
    )


@pytest.mark.asyncio
async def test_a_pending_success_extends_the_subscription():
    sub = _subscription(days_left=0)
    payment = _payment("pending")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    assert payment.status == "success"
    assert payment.paid_at is not None
    assert sub.status == "active"
    assert sub.current_period_end > _now()


@pytest.mark.asyncio
async def test_applying_twice_does_not_extend_twice():
    """A replayed webhook is normal — providers retry until they get a 200."""
    sub = _subscription(days_left=0)
    payment = _payment("pending")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)
    first_end = sub.current_period_end

    await _apply_payment(db, payment)

    assert sub.current_period_end == first_end


@pytest.mark.asyncio
async def test_a_failed_payment_never_extends_the_subscription():
    """The regression test for the fall-through bug.

    This is the Daraja 1032/1037 case: the callback arrived, we marked the payment
    failed, and control reached `_apply_payment` anyway. The subscription must not
    move and the payment must not be resurrected.
    """
    sub = _subscription(days_left=5)
    end_before = sub.current_period_end
    payment = _payment("failed")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    assert sub.current_period_end == end_before
    assert payment.status == "failed"
    assert payment.paid_at is None


@pytest.mark.asyncio
async def test_an_already_successful_payment_is_not_reapplied():
    sub = _subscription(days_left=5)
    end_before = sub.current_period_end
    payment = _payment("success")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    assert sub.current_period_end == end_before


@pytest.mark.asyncio
async def test_a_timed_out_payment_never_extends():
    """`timeout` is a distinct status and must fail closed like `failed`."""
    sub = _subscription(days_left=5)
    end_before = sub.current_period_end
    payment = _payment("timeout")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    assert sub.current_period_end == end_before
    assert payment.status == "timeout"


@pytest.mark.asyncio
async def test_renewing_early_keeps_the_remaining_days():
    """A customer who pays on day 25 gets 25 + 30, not 30 from today.

    Losing those five days is how a salon quietly loses a month and blames the
    payment provider.
    """
    now = _now()
    sub = Subscription(
        id="sub_1",
        business_id="biz_1",
        plan_id="plan_monthly",
        status="active",
        current_period_start=now - timedelta(days=25),
        current_period_end=now + timedelta(days=5),
    )
    payment = _payment("pending")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    # The new period starts where the old one ended, not today.
    assert sub.current_period_start == sub.current_period_end - timedelta(days=DAY)
    assert sub.current_period_start > now


@pytest.mark.asyncio
async def test_renewing_after_expiry_starts_from_now():
    """An expired subscription renews from today, not from a past date."""
    now = _now()
    sub = Subscription(
        id="sub_1",
        business_id="biz_1",
        plan_id="plan_monthly",
        status="expired",
        current_period_start=now - timedelta(days=60),
        current_period_end=now - timedelta(days=30),
    )
    payment = _payment("pending")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    assert sub.status == "active"
    assert sub.current_period_start >= now - timedelta(seconds=5)
    assert sub.current_period_end > now


@pytest.mark.asyncio
async def test_a_missing_subscription_fails_the_payment_without_raising():
    """Money without a subscription still has to be recorded, not crash the webhook.

    A 500 here would make the provider retry for hours on a payment that can
    never be applied.
    """
    payment = _payment("pending")
    db = _FakeSession(None)

    await _apply_payment(db, payment)

    assert payment.status == "failed"


@pytest.mark.asyncio
async def test_a_trial_renewal_preserves_the_remaining_trial():
    """Paying mid-trial adds time to the trial end rather than resetting it."""
    now = _now()
    sub = Subscription(
        id="sub_1",
        business_id="biz_1",
        plan_id="plan_monthly",
        status="trial",
        current_period_start=now - timedelta(days=9),
        current_period_end=now + timedelta(days=11),
    )
    payment = _payment("pending")
    db = _FakeSession(sub)

    await _apply_payment(db, payment)

    assert sub.status == "active"
    assert sub.current_period_start == now + timedelta(days=11)
    assert sub.current_period_end == now + timedelta(days=11 + DAY)
