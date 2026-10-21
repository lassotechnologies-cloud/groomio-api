"""Derived onboarding state — the single answer to "how far along is this owner?".

Nothing in this module is stored, and that is the whole design.

The obvious implementation is an `onboarding_step` column on `users`, advanced by
each screen. It is wrong for this flow for four independent reasons:

* **A lost response desynchronises it.** The browser sends `POST /business`, the
  database commits, and the response never arrives — a dropped connection, a
  proxy timeout, a phone that slept. A stored step still says "not started"
  while the business plainly exists, so the retry takes the "create" branch and
  either duplicates the shop or dead-ends on a conflict. Deriving the answer
  from the rows themselves cannot drift, because there is nothing to drift from.
* **Every write needs a compensating write.** Two tabs, two retries, a partial
  failure at step 2 of 3 — each is a place the counter can end up ahead of or
  behind reality, and each needs its own repair path.
* **It cannot be trusted across a migration.** Adding a column means every row
  backfilled to a guess.
* **It is not needed.** Everything onboarding produces is already durable: the
  `businesses` row, its first `branches` row, and the trial `subscriptions` row
  opened alongside them.

So progress is *derived*, and every caller — the status endpoint and the
idempotent replay inside `POST /v1/business` — reads it through this module so
the two can never answer differently.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import User
from app.models.people import Staff
from app.models.tenant import Branch, Business

# The steps the *backend* owns. `subscription` is returned as the hand-off point
# rather than being resolved here: plan activation lives in the subscriptions
# group (H3 `POST /subscription/pay`) and needs a payment provider, so this
# module reports "the business is set up, hand over to billing" and lets the
# client decide what to render. Resolving it here would mean the onboarding
# endpoint silently taking ownership of a feature it does not implement.
STEP_BUSINESS = "business"
STEP_SUBSCRIPTION = "subscription"
STEP_DONE = "done"


@dataclass(frozen=True)
class OnboardingState:
    """One owner's progress, resolved from the rows that already exist."""

    user_id: str
    account_status: str
    business: Optional[Business]
    first_branch: Optional[Branch]
    branch_count: int
    #: True when this user works inside somebody else's business. They have
    #: nothing to onboard — pointing them at the business-creation form would be
    #: a dead end, because `require_role("owner")` will refuse them anyway.
    is_staff: bool

    @property
    def has_business(self) -> bool:
        return self.business is not None

    @property
    def completed(self) -> bool:
        """The business-and-first-branch portion of onboarding is done.

        Deliberately not "the user may open the dashboard". The subscription
        hand-off is a separate step the backend does not own, and a client that
        treats `completed` as licence to skip it is exactly the false-success
        navigation this task set out to remove.
        """
        return self.business is not None and self.first_branch is not None

    @property
    def next_step(self) -> str:
        if not self.has_business:
            # A clerk or barber is already inside a shop; there is nothing for
            # them to create, so the step after the one they skipped is right.
            return STEP_DONE if self.is_staff else STEP_BUSINESS
        if self.first_branch is None:
            # A business with no location is unusable — no branch means no
            # chairs, no catalogue, no booking link. Treat it as unfinished
            # onboarding rather than complete-with-a-warning.
            return STEP_BUSINESS
        return STEP_SUBSCRIPTION


async def load_onboarding_state(db: AsyncSession, user_id: str) -> OnboardingState:
    """Resolve one owner's onboarding progress.

    Scoped to `user_id` at every step. There is no parameter a caller can supply
    that widens this lookup, which is why it is safe to expose verbatim from a
    status endpoint: the only id in play is the caller's own, taken from their
    token.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    account_status = user.status if user else "unknown"

    business = await db.scalar(
        select(Business)
        .where(Business.owner_user_id == user_id)
        .order_by(Business.created_at, Business.id)
        .limit(1)
    )

    first_branch: Optional[Branch] = None
    branch_count = 0
    if business is not None:
        first_branch = await db.scalar(
            select(Branch)
            .where(Branch.business_id == str(business.id))
            .order_by(Branch.created_at, Branch.id)
            .limit(1)
        )
        branch_count = await db.scalar(
            select(func.count())
            .select_from(Branch)
            .where(Branch.business_id == str(business.id))
        )

    is_staff = False
    if business is None:
        # One extra query, only on the path where it can change the answer.
        is_staff = (
            await db.scalar(select(Staff.id).where(Staff.user_id == user_id).limit(1))
        ) is not None

    return OnboardingState(
        user_id=user_id,
        account_status=account_status,
        business=business,
        first_branch=first_branch,
        branch_count=branch_count or 0,
        is_staff=is_staff,
    )