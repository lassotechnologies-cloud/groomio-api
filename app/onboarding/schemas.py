"""Response shapes for the onboarding status endpoint."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.business.schemas import BranchResponse, BusinessResponse
from app.onboarding.state import STEP_BUSINESS, STEP_DONE, STEP_SUBSCRIPTION

OnboardingStep = Literal["business", "subscription", "done"]


class OnboardingStatusResponse(BaseModel):
    """Where the caller is in onboarding — the resume contract.

    The PWA calls this on every onboarding screen it opens. That is what makes a
    refresh, a lost response and a half-finished form all resolve the same way:
    the server states what exists, and the client routes on that rather than on
    a flag it wrote to `localStorage` before it knew whether the write landed.
    """

    user_id: str
    #: Always `active` on a successful call — reaching this endpoint at all
    #: requires an active account. Present so the client can assert the
    #: lifecycle it believes it is in without a second round-trip.
    account_status: str
    has_business: bool
    #: A business with no branch is not a usable shop: no chairs, no catalogue,
    #: no booking link. Reported separately from `has_business` because the two
    #: mean different things to the form.
    has_branch: bool
    #: Business *and* first branch exist. See `OnboardingState.completed` for
    #: why this is not the same as "may open the dashboard".
    completed: bool
    next: OnboardingStep
    business: Optional[BusinessResponse] = None
    #: The business's first branch, in creation order — the one onboarding
    #: created. `None` until one exists.
    branch: Optional[BranchResponse] = None
    branch_count: int = Field(0, ge=0, description="Total branches the owner has")


__all__ = [
    "OnboardingStatusResponse",
    "OnboardingStep",
    "STEP_BUSINESS",
    "STEP_DONE",
    "STEP_SUBSCRIPTION",
]
