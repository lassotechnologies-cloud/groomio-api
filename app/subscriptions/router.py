"""Subscription & payment routes — API group H1–H6 (Doc 10.9).

The billing state machine, in one place:

    trial  ──payment ok──▶ active ──period ends, unpaid──▶ grace ──▶ expired
      ▲                                                              │
      └──────────── payment ok (renewal) ◀── payment ok ─────────────┘

Two rules govern everything below:

  1. The price comes from the `plans` row, never from the request. A client that
     can choose its own amount will, and a subscription is exactly the kind of
     thing you do not want forgeable.

  2. Extending a subscription happens in exactly one function (`_apply_payment`),
     called from exactly one place per provider. Two implementations of "the
     customer paid" is how a shop ends up with two months on a one-month plan.

Webhooks are the hard part of payments, so they are written defensively:
store first, then decide, and never trust a callback to be well-formed,
well-signed, or even new.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.payments import (
    PaymentProviderError,
    daraja_callback_is_ours,
    daraja_stk_push,
    pesapal_order,
    verify_pesapal_ipn_signature,
)
from app.core.security import now_utc
from app.core.tenancy import resolve_business_id
from app.models.billing import Payment, Plan, Subscription
from app.models.identity import User
from app.models.platform import WebhookEvent
from app.subscriptions.schemas import (
    PaymentResponse,
    PlanResponse,
    StartPaymentRequest,
    StartPaymentResponse,
    SubscriptionDetailResponse,
    WebhookAck,
)

router = APIRouter(tags=["subscriptions"])

Db = Annotated[AsyncSession, Depends(get_db)]
OwnerOnly = Depends(require_role("owner"))

# M-Pesa result codes that mean money moved. 0 is the only one.
DARAJA_SUCCESS_CODE = 0


# ── helpers ────────────────────────────────────────────────────────────────
def _plan_out(p: Plan) -> PlanResponse:
    return PlanResponse(
        id=str(p.id),
        code=p.code,
        price_kes=p.price_kes,
        interval_days=p.interval_days,
        discount_pct=p.discount_pct,
        per_day_kes=p.price_kes // max(p.interval_days, 1),
    )


def _payment_out(p: Payment) -> PaymentResponse:
    return PaymentResponse(
        id=str(p.id),
        subscription_id=p.subscription_id,
        amount_kes=p.amount_kes,
        method=p.method,
        provider=p.provider,
        provider_ref=p.provider_ref,
        status=p.status,
        paid_at=p.paid_at,
        created_at=p.created_at,
    )


async def _current_subscription(
    db: AsyncSession, business_id: str
) -> Optional[Subscription]:
    """The live subscription: trial, active, or grace. Expired rows are history."""
    return await db.scalar(
        select(Subscription)
        .where(
            Subscription.business_id == business_id,
            Subscription.status.in_(("trial", "active", "grace")),
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )


def _subscription_out(
    sub: Subscription, plan_code: Optional[str], now: datetime
) -> SubscriptionDetailResponse:
    """Serialize with live countdowns computed against `now`, not stored.

    Storing "days remaining" would mean a background job has to be exactly on time
    every day; deriving it means the banner is right even if the worker was down.
    """
    trial_left = (sub.current_period_end - now).days
    renewal_in = trial_left if sub.status in ("trial", "active") else 0
    return SubscriptionDetailResponse(
        id=str(sub.id),
        business_id=sub.business_id,
        plan_id=sub.plan_id,
        plan_code=plan_code,
        status=sub.status,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        trial_days_remaining=trial_left,
        is_in_grace=sub.status == "grace",
        days_until_renewal=renewal_in,
    )


async def _apply_payment(db: AsyncSession, payment: Payment) -> None:
    """Turn a successful payment into subscription time. The only writer of this.

    Three behaviours worth naming:

    - A renewal is measured from the later of "now" and the current period end.
      Renewing early therefore does not lose the remaining days, and renewing late
      does not grant a period that started in the past.

    - `status` becomes `active`, never `expired`. A customer who pays after the
      grace period is a paying customer, not a lost one.

    - Nothing but a *pending* payment may be applied. The guard is a whitelist
      rather than `status != "success"`, because every payment arrives `pending`
      and marking a failure "failed" must not leave it eligible. When this was a
      blacklist, a failed Daraja callback fell through this function and extended
      the subscription anyway — a barber could cancel at the PIN prompt and still
      receive a paid month. Whitelisting `pending` means an explicit
      `_apply_payment` call is required for time to be granted, and forgetting to
      call it fails closed.
    """
    if payment.status != "pending":
        return  # already applied, or never payable

    now = now_utc()
    sub = await db.scalar(
        select(Subscription).where(Subscription.id == payment.subscription_id)
    )
    if not sub:
        # The payment is real but the subscription is gone (business deleted).
        # Record the money; there is nothing to extend.
        payment.status = "failed"
        return

    plan = await db.scalar(select(Plan).where(Plan.id == sub.plan_id))
    period_days = plan.interval_days if plan else 30

    period_start = now
    if sub.status in ("active", "trial", "grace") and sub.current_period_end > now:
        period_start = sub.current_period_end

    sub.status = "active"
    sub.current_period_start = period_start
    sub.current_period_end = period_start + timedelta(days=period_days)

    payment.status = "success"
    payment.paid_at = now
    await db.flush()


# ── H2 — public plan catalogue ─────────────────────────────────────────────
@router.get("/plans", response_model=list[PlanResponse])
async def list_plans(db: Db) -> list[PlanResponse]:
    """H2 — public on purpose: the pricing page renders before anyone logs in.

    Only active plans, cheapest first, so a new plan added to the table appears
    in the right place without a deploy.
    """
    rows = await db.scalars(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_kes)
    )
    return [_plan_out(p) for p in rows]


# ── H1 — my subscription ───────────────────────────────────────────────────
@router.get(
    "/subscription",
    response_model=SubscriptionDetailResponse,
    dependencies=[OwnerOnly],
)
async def get_subscription(db: Db, user: CurrentUser) -> SubscriptionDetailResponse:
    """H1 — plan, trial countdown, renewal date, and payment history."""
    business_id = await resolve_business_id(db, user)
    sub = await _current_subscription(db, business_id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No subscription yet"
        )

    plan = await db.scalar(select(Plan).where(Plan.id == sub.plan_id))
    out = _subscription_out(sub, plan.code if plan else None, now_utc())

    payments = await db.scalars(
        select(Payment)
        .where(Payment.business_id == business_id)
        .order_by(Payment.created_at.desc())
        .limit(20)
    )
    out.payments = [_payment_out(p) for p in payments]
    return out


# ── H3 — start a payment ───────────────────────────────────────────────────
@router.post(
    "/subscription/pay",
    response_model=StartPaymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[OwnerOnly],
)
async def start_payment(
    payload: StartPaymentRequest, db: Db, user: CurrentUser
) -> StartPaymentResponse:
    """H3 — initiate the payment. 202, not 201: nothing is paid yet.

    The payment row is written *before* the provider is called. If the push fails
    the owner sees a failed attempt in their history rather than a payment that
    vanished, which is the difference between "we lost your money" and "we logged
    that M-Pesa was down".
    """
    business_id = await resolve_business_id(db, user)
    sub = await _current_subscription(db, business_id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No subscription yet"
        )

    plan = await db.scalar(
        select(Plan).where(Plan.id == payload.plan_id, Plan.is_active.is_(True))
    )
    if not plan:
        # 404 not 400: a plan that is not for sale should look like it does not
        # exist, and the price never comes from the client.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )

    if plan.price_kes < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This plan is free; no payment needed",
        )

    provider = "daraja" if payload.method == "mpesa" else "pesapal"
    payment = Payment(
        business_id=business_id,
        subscription_id=sub.id,
        amount_kes=plan.price_kes,
        method=payload.method,
        provider=provider,
        status="pending",
    )
    db.add(payment)
    await db.flush()

    if payload.method == "mpesa":
        owner = await db.scalar(select(User).where(User.id == user["user_id"]))
        if not owner or not owner.phone:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No phone on the owner account for M-Pesa",
            )
        try:
            # AccountReference is the payment id, so the callback can be matched
            # back to exactly this row without trusting the callback's own claims.
            checkout_id = await daraja_stk_push(
                phone=owner.phone,
                amount_kes=plan.price_kes,
                account_ref=str(payment.id),
                description=f"Groomio {plan.code} plan",
            )
        except PaymentProviderError as exc:
            payment.status = "failed"
            await db.flush()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc

        payment.provider_ref = checkout_id
        await db.flush()
        return StartPaymentResponse(
            payment_id=str(payment.id),
            state="awaiting_mpesa",
            instructions="Check your phone for the M-Pesa prompt and enter your PIN.",
        )

    owner = await db.scalar(select(User).where(User.id == user["user_id"]))
    try:
        checkout_url = await pesapal_order(
            reference=str(payment.id),
            amount_kes=plan.price_kes,
            description=f"Groomio {plan.code} plan",
            email=owner.email if owner else "",
            phone=(owner.phone if owner else "").lstrip("+"),
        )
    except PaymentProviderError as exc:
        payment.status = "failed"
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return StartPaymentResponse(
        payment_id=str(payment.id),
        state="redirect",
        checkout_url=checkout_url,
        instructions="Continue to the payment page to complete payment.",
    )


# ── H6 — poll a payment ────────────────────────────────────────────────────
@router.get(
    "/payments/{payment_id}", response_model=PaymentResponse, dependencies=[OwnerOnly]
)
async def get_payment(payment_id: str, db: Db, user: CurrentUser) -> PaymentResponse:
    """H6 — what the popup polls. Scoped by business so ids are not guessable
    across tenants."""
    business_id = await resolve_business_id(db, user)
    payment = await db.scalar(
        select(Payment).where(
            Payment.id == payment_id, Payment.business_id == business_id
        )
    )
    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        )
    return _payment_out(payment)


# ── H4 — M-Pesa callback ───────────────────────────────────────────────────
@router.post("/payments/webhooks/daraja", response_model=WebhookAck)
async def daraja_webhook(request: Request, db: Db) -> WebhookAck:
    """H4 — Safaricom's result callback.

    Public and therefore untrusted, so the order is fixed and deliberate:

      1. store the raw payload in `webhook_events` — if we crash at step 3 the
         money is still on record and `payment_reconciler` can pick it up;
      2. check the shape and that we recognise the reference;
      3. only then touch the subscription.

    A callback that fails step 2 is stored and left unprocessed. It is never
    deleted, because a callback we could not verify is exactly the evidence
    needed when an owner claims a payment vanished.
    """
    payload: dict[str, Any]
    try:
        payload = await request.json()
    except Exception:
        # Not JSON. Still 200 — a non-2xx makes Safaricom retry for hours.
        return WebhookAck(received=False)

    stk = (
        payload.get("Body", {}).get("stkCallback", {})
        if isinstance(payload, dict)
        else {}
    )
    event_ref = stk.get("CheckoutRequestID") or payload.get("CheckoutRequestID")
    account_ref = stk.get("AccountReference") or payload.get("AccountReference")

    event = WebhookEvent(
        source="daraja",
        event_ref=str(event_ref or "unknown"),
        payload=payload,
    )
    db.add(event)
    await db.flush()

    if not daraja_callback_is_ours(
        event_ref=event_ref,
        account_ref=account_ref,
        amount_kes=stk.get("Amount") or payload.get("Amount"),
    ):
        return WebhookAck(received=True, event_id=str(event.id))

    result_code = stk.get("ResultCode", payload.get("ResultCode"))
    succeeded = str(result_code) == str(DARAJA_SUCCESS_CODE)
    receipt = stk.get("MpesaReceiptNumber") or payload.get("MpesaReceiptNumber")

    # Match on the account reference we minted, not on the receipt number: the
    # receipt is the thing being asserted, and this is the thing we issued.
    payment = await db.scalar(
        select(Payment).where(
            Payment.id == str(account_ref), Payment.provider == "daraja"
        )
    )
    if not payment:
        return WebhookAck(received=True, event_id=str(event.id))

    # A success callback that quotes a different amount is not our success.
    quoted = stk.get("Amount") or payload.get("Amount")
    if succeeded and quoted is not None and int(quoted) != payment.amount_kes:
        payment.status = "failed"
        event.processed_at = now_utc()
        await db.flush()
        return WebhookAck(received=True, event_id=str(event.id))

    payment.provider_ref = receipt or payment.provider_ref
    payment.raw_callback = payload

    if not succeeded:
        # 1032 = user cancelled, 1037 = timeout waiting for the PIN. Both are
        # failures the owner can retry, not silent drops.
        #
        # Stop here. This used to fall through to `_apply_payment`, which only
        # guards on `status == "success"` — so a *failed* payment sailed past the
        # guard and extended the subscription anyway, flipping the payment back
        # to success. A barber could cancel at the PIN prompt and still get a
        # month. Only a success callback may extend time.
        payment.status = "failed"
    else:
        await _apply_payment(db, payment)

    event.processed_at = now_utc()
    await db.flush()
    return WebhookAck(received=True, event_id=str(event.id))


# ── H5 — Pesapal IPN ───────────────────────────────────────────────────────
@router.post("/payments/webhooks/pesapal", response_model=WebhookAck)
async def pesapal_webhook(request: Request, db: Db) -> WebhookAck:
    """H5 — Pesapal's server-to-server notification (covers Airtel Money and cards).

    Unlike Daraja, this one is genuinely signed, so an unsigned notification is
    rejected outright — but it is still stored first, because "we got an unsigned
    IPN" is a fact support needs and a discarded request is not.
    """
    try:
        payload = await request.json()
    except Exception:
        return WebhookAck(received=False)
    if not isinstance(payload, dict):
        return WebhookAck(received=False)

    event_ref = str(
        payload.get("notification_id")
        or (payload.get("notification_details") or {}).get("notification_id")
        or "unknown"
    )
    event = WebhookEvent(source="pesapal", event_ref=event_ref, payload=payload)
    db.add(event)
    await db.flush()

    valid = verify_pesapal_ipn_signature(
        notification_type=str(payload.get("notification_type", "")),
        notification_id=event_ref,
        notification_details=str(payload.get("notification_details", "")),
        signature=str(payload.get("signature", "")),
    )
    if not valid:
        return WebhookAck(received=True, event_id=str(event.id))

    details = payload.get("notification_details") or {}
    reference = str(
        details.get("merchant_reference") or details.get("account_number") or ""
    )
    status_value = str(details.get("status", "")).lower()

    payment = await db.scalar(
        select(Payment).where(
            Payment.id == reference,
            Payment.provider == "pesapal",
        )
    )
    if not payment:
        return WebhookAck(received=True, event_id=str(event.id))

    payment.raw_callback = payload
    payment.provider_ref = str(
        details.get("merchant_reference") or payment.provider_ref or ""
    )

    if status_value == "completed":
        quoted = details.get("amount")
        # Pesapal quotes the amount as a float string. Compare on whole shillings
        # so 1000.0 does not read as a mismatch against 1000.
        if quoted is None or int(float(quoted)) != payment.amount_kes:
            payment.status = "failed"
        else:
            await _apply_payment(db, payment)
    else:
        payment.status = "failed"

    event.processed_at = now_utc()
    await db.flush()
    return WebhookAck(received=True, event_id=str(event.id))
