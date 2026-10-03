"""Loyalty schemas (Doc 10.7 — G3–G6).

Points are stored on the customer as a balance, but the balance is always
derivable from `loyalty_transactions`. The column exists because every read
would otherwise need a SUM, and a wrong SUM is far more expensive than a
transaction row that can be corrected.
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoyaltyEntryResponse(BaseModel):
    """One movement in the points ledger."""

    id: str
    points: int
    reason: str
    sale_id: Optional[str] = None
    created_at: Optional[datetime] = None


class LoyaltyBalanceResponse(BaseModel):
    """G3 — balance plus its derivation, so a disputed figure can be traced."""

    customer_id: str
    points: int
    # What the balance is worth in whole KES at the shop's standard rate.
    redeemable_kes: int
    entries: list[LoyaltyEntryResponse] = []


class RedeemRequest(BaseModel):
    """G3 — spend points. The customer asks for KES; points are computed server-side
    from the rate below, so a client cannot invent its own exchange."""

    amount_kes: int = Field(..., ge=1)


class RedeemResponse(BaseModel):
    customer_id: str
    points_spent: int
    amount_kes: int
    remaining_points: int
    entry: LoyaltyEntryResponse


class MembershipPlanCreateRequest(BaseModel):
    """G4 — e.g. "10 cuts for the price of 8", validity in days."""

    name: str = Field(..., min_length=1, max_length=120)
    price_kes: int = Field(..., ge=0)
    sessions_included: int = Field(..., ge=1, le=500)
    validity_days: int = Field(..., ge=1, le=3650)


class MembershipPlanUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    price_kes: Optional[int] = Field(None, ge=0)
    sessions_included: Optional[int] = Field(None, ge=1, le=500)
    validity_days: Optional[int] = Field(None, ge=1, le=3650)


class MembershipPlanResponse(BaseModel):
    id: str
    branch_id: str
    name: str
    price_kes: int
    sessions_included: int
    validity_days: int
    # Per-session price, rounded down. A customer shopping plans compares these,
    # not the totals.
    per_session_kes: int


class MembershipResponse(BaseModel):
    id: str
    customer_id: str
    plan_id: str
    plan_name: Optional[str] = None
    started_at: date
    expires_at: date
    sessions_used: int
    sessions_remaining: int
    is_active: bool


class AttachMembershipRequest(BaseModel):
    """G5 — sell a membership to a customer."""

    plan_id: str


class ReferralCreateRequest(BaseModel):
    """G6 — a customer sends a friend's number. The friend is not a customer yet,
    so only the phone is captured; converting to a customer happens later."""

    referred_phone: str = Field(..., min_length=10, max_length=15)


class ReferralConvertRequest(BaseModel):
    """G6 — the referred friend has now become a customer. Releases the reward."""

    points_awarded: int = Field(..., ge=0)


class ReferralResponse(BaseModel):
    id: str
    referrer_customer_id: str
    referred_phone: str
    status: str
    points_awarded: int
    created_at: Optional[datetime] = None
