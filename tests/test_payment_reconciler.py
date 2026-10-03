"""Webhook reconciler decision rules (Doc 10.11).

`_reconcile` reads stored `webhook_events` that were never processed and decides,
per event, whether to apply money. Two properties matter and neither is obvious
from the code:

  1. It must not grant a subscription for a callback the provider reported as
     failed. This is the same class of bug as the one in `_apply_payment` itself.
  2. It *must* grant one for a success that arrived after a timeout. Providers do
     this — Safaricom's callback can beat our own failure handling on a slow
     connection — and refusing it means keeping a customer's money without giving
     them the month they paid for.

These tests drive the real `_reconcile` body against a fake session rather than
re-implementing its logic, because the bug class here is precisely "the guard and
the call site disagree".
"""

from datetime import datetime, timezone

import pytest

from app.models.billing import Payment, Plan, Subscription
from app.models.platform import WebhookEvent
from app.workers.payment_reconciler import _reconcile


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payment(status: str) -> Payment:
    return Payment(
        id="pay_1",
        business_id="biz_1",
        subscription_id="sub_1",
        amount_kes=1000,
        provider="daraja",
        status=status,
    )


def _subscription() -> Subscription:
    now = _now()
    return Subscription(
        id="sub_1",
        business_id="biz_1",
        plan_id="plan_monthly",
        status="active",
        current_period_start=now,
        current_period_end=now,
    )


def _daraja_payload(result_code: str, account_ref: str = "pay_1") -> dict:
    return {
        "Body": {
            "stkCallback": {
                "ResultCode": result_code,
                "AccountReference": account_ref,
                "Amount": 1000,
            }
        }
    }


def _event(payload: dict, source: str = "daraja") -> WebhookEvent:
    return WebhookEvent(source=source, event_ref="ref", payload=payload)


class _Result:
    """Stand-in for a SQLAlchemy ScalarResult: iterable, and has `.all()`."""

    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Enough of AsyncSession for `_reconcile`: one scalars(), scalar(), flush."""

    def __init__(self, events, payment, sub, plan=None):
        self._events = list(events)
        # Results queue in call order: Payment (reconcile), then Subscription and
        # Plan (inside _apply_payment). An earlier version switched on a `_next`
        # flag it never set, so every later lookup returned the Payment again and
        # blew up on `sub.plan_id`. A plain queue cannot have that failure mode.
        #
        # `payment` may be None, which is how the "no payment matches this
        # reference" branch is reached: a queue of [None, ...] simply returns
        # None from the first lookup, exactly as a real miss would.
        self._scalar_results = [
            payment,
            sub,
            plan or Plan(id="plan_monthly", interval_days=30, price_kes=1000),
        ]
        self.committed = False

    async def scalars(self, query):  # noqa: ARG002
        # SQLAlchemy's ScalarResult supports both `list(...)` and `.all()`; the
        # worker does both across its queries, so the fake offers both.
        return _Result(self._events)

    async def scalar(self, query):  # noqa: ARG002
        return self._scalar_results.pop(0)

    async def flush(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    """Route the worker's `session_scope` at a fake, per test."""

    def _install(fake):
        import app.workers.payment_reconciler as mod

        monkeypatch.setattr(mod, "session_scope", lambda: fake)

    return _install


# ── a success must grant time ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unprocessed_success_is_applied(_patch_session):
    payment = _payment("pending")
    sub = _subscription()
    end_before = sub.current_period_end
    fake = _FakeSession([_event(_daraja_payload("0"))], payment, sub)
    _patch_session(fake)

    result = await _reconcile()

    assert result["applied"] == 1
    assert payment.status == "success"
    assert sub.current_period_end > end_before


@pytest.mark.asyncio
async def test_a_late_success_after_a_timeout_is_still_applied(_patch_session):
    """The money arrived; the customer must get the month.

    `_apply_payment` is fail-closed, so the reconciler is the single place that
    re-opens a `timeout` payment. Without it, real KES 1,000 is taken and no
    service is given.
    """
    payment = _payment("timeout")
    sub = _subscription()
    end_before = sub.current_period_end
    fake = _FakeSession([_event(_daraja_payload("0"))], payment, sub)
    _patch_session(fake)

    result = await _reconcile()

    assert result["applied"] == 1
    assert payment.status == "success"
    assert sub.current_period_end > end_before


@pytest.mark.asyncio
async def test_a_late_success_after_a_failure_is_applied(_patch_session):
    payment = _payment("failed")
    sub = _subscription()
    fake = _FakeSession([_event(_daraja_payload("0"))], payment, sub)
    _patch_session(fake)

    result = await _reconcile()

    assert result["applied"] == 1
    assert payment.status == "success"


# ── a failure must never grant time ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unprocessed_failure_does_not_extend_the_subscription(_patch_session):
    """Daraja 1032 (cancelled) or 1037 (PIN timeout) replayed by the beat task."""
    payment = _payment("pending")
    sub = _subscription()
    end_before = sub.current_period_end
    fake = _FakeSession([_event(_daraja_payload("1032"))], payment, sub)
    _patch_session(fake)

    result = await _reconcile()

    assert result["applied"] == 0
    assert payment.status == "failed"
    assert sub.current_period_end == end_before


@pytest.mark.asyncio
async def test_a_duplicate_success_does_not_extend_twice(_patch_session):
    """Providers retry until they get a 200, so duplicates are routine."""
    payment = _payment("success")
    sub = _subscription()
    end_before = sub.current_period_end
    fake = _FakeSession([_event(_daraja_payload("0"))], payment, sub)
    _patch_session(fake)

    result = await _reconcile()

    assert result["applied"] == 0
    assert sub.current_period_end == end_before


@pytest.mark.asyncio
async def test_a_callback_naming_no_known_payment_is_skipped(_patch_session):
    """Garbage, or a business that has since been deleted. Never guess."""
    sub = _subscription()
    end_before = sub.current_period_end
    # The payment lookup misses: nothing in the DB has id "pay_unknown".
    fake = _FakeSession(
        [_event(_daraja_payload("0", account_ref="pay_unknown"))], None, sub
    )
    _patch_session(fake)

    result = await _reconcile()

    assert result["skipped"] == 1
    assert result["applied"] == 0
    assert sub.current_period_end == end_before
