"""Onboarding routes — the read side of a resumable signup.

One endpoint: `GET /v1/onboarding/status`. It exists because the write side
(`POST /v1/business`) is idempotent but the *client* still has to know which
screen to show. Without a read, a PWA has to answer that from `localStorage` —
which is a guess written before the request completed, and a guess that is wrong
in every case that matters: a response lost after commit, a refresh mid-form, a
second tab finishing the flow while the first still shows the form.

`POST /onboarding/business` never existed on this API. The PWA called it and got
a 404, which is not a contract mismatch to be papered over by editing a string:
the write endpoint is `POST /v1/business`, it takes a different field set than
the PWA was sending, and it already had a documented response. What was missing
was the read that lets a client resume — that is what this router adds.

On credentials: this uses `get_active_user`, not `get_onboarding_user`. The
restricted onboarding token exists to name a *pending* registrant during OTP
verification, and onboarding state here is business state — it only exists after
the account is verified. Accepting the weaker credential would buy nothing and
would widen what an unverified account can read. `get_current_user` is untouched.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.router import branch_out, business_out
from app.core.database import get_db
from app.core.deps import ActiveUser
from app.onboarding.schemas import OnboardingStatusResponse
from app.onboarding.state import load_onboarding_state

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

Db = Annotated[AsyncSession, Depends(get_db)]


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(db: Db, user: ActiveUser) -> OnboardingStatusResponse:
    """Where the caller is in onboarding.

    Scoped entirely to the caller's own id, taken from their token. There is no
    query parameter, so there is nothing for a caller to vary and nothing to
    probe — asking about another owner's progress is not expressible in this
    API's surface.
    """
    state = await load_onboarding_state(db, user["user_id"])
    return OnboardingStatusResponse(
        user_id=state.user_id,
        account_status=state.account_status,
        has_business=state.has_business,
        has_branch=state.first_branch is not None,
        completed=state.completed,
        next=state.next_step,
        business=business_out(state.business) if state.business else None,
        branch=branch_out(state.first_branch) if state.first_branch else None,
        branch_count=state.branch_count,
    )
