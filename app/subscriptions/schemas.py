"""Subscription & payment schemas (Doc 10.9 — H1–H6)."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PlanResponse(BaseModel):
    """H2 — one of the seeded plans. Public: the pricing page renders before login."""

    id: str
    code: str
    price_kes: int
    interval_days: int
    discount_pct: int
    # KES per day, rounded down. This is the number that lets a daily user see,
    # at a glance, that yearly is the better deal.
    per_day_kes: int


class PaymentResponse(BaseModel):
    """H6 — what the popup polls until it sees `success`."""

    id: str
    subscription_id: str
    amount_kes: int
    method: str
    provider: str
    provider_ref: Optional[str] = None
    status: str
    paid_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class SubscriptionResponse(BaseModel):
    """H1 — the billing screen: plan, when it renews, how much trial is left."""

    id: str
    business_id: str
    plan_id: str
    plan_code: Optional[str] = None
    status: str
    current_period_start: datetime
    current_period_end: datetime
    # Negative once the period has passed — a countdown, not a clamped number, so
    # the UI can distinguish "renews in 3 days" from "expired 2 days ago".
    trial_days_remaining: int
    is_in_grace: bool
    # Days until renewal. Drives the "your plan renews in N days" banner.
    days_until_renewal: int


class SubscriptionDetailResponse(SubscriptionResponse):
    """H1 with history — the same screen plus what was paid before."""

    payments: list[PaymentResponse] = []


class StartPaymentRequest(BaseModel):
    """H3 — the owner picks a plan and a rail. No amount: the price comes from
    the plan row so a tampered client cannot choose what it pays."""

    plan_id: str
    method: Literal["mpesa", "pesapal"]


class StartPaymentResponse(BaseModel):
    """H3 — 202. `checkout_url` is present only for Pesapal (a redirect); M-Pesa
    prompts on the phone, so the client just polls the payment."""

    payment_id: str
    state: Literal["awaiting_mpesa", "redirect"]
    checkout_url: Optional[str] = None
    # M-Pesa ignores this after the first prompt on some handsets; the client
    # still sends it, because if the user cancels the push we want them to retry.
    instructions: str


class WebhookAck(BaseModel):
    """What every webhook returns. Deliberately boring: providers only care that
    we received it, and a fast 200 with no body is what stops Safaricom and
    Pesapal from retrying a callback we have already stored."""

    received: bool = True
    event_id: Optional[str] = None
