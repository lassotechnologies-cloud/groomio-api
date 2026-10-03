"""Admin schemas (Doc 10.9 — H7–H10, super_admin only)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TrialExtendRequest(BaseModel):
    """H7 — give a shop more time, or discount a bill.

    `days` is additive rather than an absolute `trial_ends_at` date. An absolute
    date means the person doing the extension has to read today's date correctly
    and add to it; `days: 7` cannot be misapplied.
    """

    business_id: str
    days: int = Field(..., ge=1, le=90)
    reason: str = Field(..., min_length=3, max_length=280)


class TenantStatusResponse(BaseModel):
    id: str
    name: str
    town: Optional[str] = None
    status: str
    plan_code: Optional[str] = None
    trial_ends_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class SuspendRequest(BaseModel):
    """H9 — suspending is reversible, deleting is not. Hence a reason and a body."""

    reason: str = Field(..., min_length=3, max_length=280)
    reactivate: bool = False


class AdminDashboardResponse(BaseModel):
    """H10 — the three numbers that decide whether the business is working.

    Conversion is the one that matters: signups that never pay are a marketing
    problem, and a growing signups number hides that completely.
    """

    total_businesses: int
    active_subscriptions: int
    trialing_businesses: int
    suspended_businesses: int
    expired_businesses: int
    # Of businesses that have ever had a successful payment, how many are active.
    conversion_rate_pct: int
    revenue_this_month_kes: int
    revenue_all_time_kes: int
    payments_this_month: int
