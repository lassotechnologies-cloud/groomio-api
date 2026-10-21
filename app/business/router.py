"""Business / Branch / Service routes — API group B (Doc 10.3: B1–B6).

Onboarding step 2: an owner registers and verifies their phone (group A), then
creates their business plus a first branch here. The business is created with
`status = "trialing"` and a `trial_ends_at` window (config `trial_days`), so the
shop can start working before any money changes hands (Doc 2.4).

Multi-tenancy rule (Doc 10.1): the tenant is derived from the caller's token —
clients never send a `business_id`. A branch belonging to another tenant returns
404, never 403, so cross-tenant probing reveals nothing.

Onboarding is resumable. B1 is idempotent per owner — a repeat submission
returns the business that already exists instead of a conflict, so a lost
response, a double tap or a refresh mid-form all resolve on the same call. The
companion read is `GET /v1/onboarding/status`, and both derive their answer
through `app.onboarding.state` so they cannot disagree.
"""

import re
from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.schemas import (
    BranchCreateRequest,
    BranchResponse,
    BranchUpdateRequest,
    BusinessCreateRequest,
    BusinessCreateResponse,
    BusinessResponse,
    BusinessUpdateRequest,
    ServiceCreateRequest,
    ServiceResponse,
    ServiceUpdateRequest,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import ActiveUser, CurrentUser, require_role
from app.core.security import now_utc
from app.models.billing import Plan, Subscription
from app.models.tenant import Branch, Business, Service
from app.onboarding.state import OnboardingState, load_onboarding_state

router = APIRouter(tags=["business"])

Db = Annotated[AsyncSession, Depends(get_db)]
# Applied as a *parameter* default, not in the decorator's `dependencies=` list.
# Decorator dependencies are solved before the endpoint's own parameters, which
# meant the role check answered first: an anonymous caller got 403 "requires
# one of ('owner',)" instead of 401 "not authenticated", and the restricted
# pre-verification token got 403 instead of 401 — so the client could not tell
# "log in" from "you may not", and `api-client.ts`, which clears the session on
# 401 alone, left the dead session in place. As a parameter it runs after
# `get_current_user`, so the status code now matches what actually went wrong.
# The restriction itself is unchanged.
OwnerOnly = Depends(require_role("owner"))


# ── helpers ──────────────────────────────────────────────────────────────────
def business_out(biz: Business, plan_code: Optional[str] = None) -> BusinessResponse:
    return BusinessResponse(
        id=str(biz.id),
        owner_user_id=biz.owner_user_id,
        name=biz.name,
        business_type=biz.business_type,
        logo_url=biz.logo_url,
        county=biz.county,
        town=biz.town,
        status=biz.status,
        trial_ends_at=biz.trial_ends_at.isoformat() if biz.trial_ends_at else None,
        chat_enabled=biz.chat_enabled,
        plan=plan_code,
    )


#: How many slugs to try before giving up. A clash under concurrency resolves in
#: one or two extra rounds; a pathological pile of identically-named branches is
#: the only thing that gets near this, and spinning forever would be a worse
#: failure than a loud, honest error.
_SLUG_ATTEMPTS = 6

#: PostgreSQL SQLSTATE for `unique_violation`. Matching on this plus the table
#: name — rather than on the constraint's name — is what makes the check survive
#: the two schemas that exist for this table: Alembic creates the unique index as
#: `ix_branches_slug`, while `Base.metadata.create_all` (used by the test suite)
#: renders the same thing from `Column(unique=True)` as `branches_slug_key`.
#: Keying on the *relation* rather than the object's name cannot drift between
#: them.
_UNIQUE_VIOLATION = "23505"


def _exception_chain(exc: BaseException):
    """Yield `exc`'s wrapper layers, innermost driver error last.

    SQLAlchemy nests the driver's exception under its own: with asyncpg the
    shape is `IntegrityError -> AsyncAdapt_asyncpg_dbapi.UniqueViolationError ->
    asyncpg.UniqueViolationError`. The adapter layer re-exposes `sqlstate` but
    *not* `table_name`, so the interesting attributes are only reachable by
    unwrapping. psycopg2 instead nests them under `.diag`. Both are walked here
    so this helper cannot silently degrade into "matches nothing" — which is
    precisely how a handled-conflict path turns back into an unexplained 500.
    """
    layer = exc
    for _ in range(5):  # bounded: a self-referential `.orig` would loop forever
        inner = getattr(layer, "orig", None)
        if inner is None or inner is layer:
            return
        layer = inner
        yield layer


def _is_unique_violation_on(exc: IntegrityError, table: str) -> bool:
    """True when `exc` is a unique-constraint failure on exactly `table`.

    Narrow on purpose. `IntegrityError` also covers foreign keys, check
    violations and NOT NULL failures, and quietly converting one of those into a
    cheerful "here is your business" replay would report success for a write that
    never happened. Only the tables this endpoint genuinely races on are
    accepted; every other failure re-raises and surfaces as a 500, which is the
    correct answer for an unexpected error.

    Matching on the *relation* rather than the index name keeps this correct
    across both schemas this table exists in: Alembic creates the unique index as
    `ix_branches_slug`, while `Base.metadata.create_all` (what the tests use)
    renders the same thing from `Column(unique=True)` as `branches_slug_key`.
    """
    chain = list(_exception_chain(exc))
    if not any(
        getattr(layer, "sqlstate", None) == _UNIQUE_VIOLATION for layer in chain
    ):
        return False

    for layer in chain:
        diag = getattr(layer, "diag", None)
        relation = (
            getattr(layer, "table_name", None)
            or getattr(diag, "table_name", None)
            or getattr(diag, "relation_name", None)
        )
        if relation is not None:
            return relation == table

    # A unique violation we cannot attribute to a relation. Refusing to guess is
    # the safe direction: an unhandled 500 is recoverable, a fabricated replay is
    # not.
    return False


def _slug_candidates(name: str) -> list[str]:
    """The slug order a branch name maps to: `name`, `name-2`, `name-3`, …

    Shared by the "is it taken" lookup and the retry loop so the two can never
    disagree about what the next candidate is.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "branch"
    return [base] + [f"{base}-{n}" for n in range(2, _SLUG_ATTEMPTS + 2)]


def branch_out(branch: Branch) -> BranchResponse:
    return BranchResponse(
        id=str(branch.id),
        business_id=branch.business_id,
        slug=branch.slug,
        name=branch.name,
        county=branch.county,
        town=branch.town,
        opens_at=branch.opens_at.isoformat() if branch.opens_at else None,
        closes_at=branch.closes_at.isoformat() if branch.closes_at else None,
        num_chairs=branch.num_chairs,
        is_active=branch.is_active,
    )


def _service_out(svc: Service) -> ServiceResponse:
    return ServiceResponse(
        id=str(svc.id),
        branch_id=svc.branch_id,
        name=svc.name,
        price_kes=svc.price_kes,
        duration_min=svc.duration_min,
        is_active=svc.is_active,
    )


async def _load_owned_business(db: AsyncSession, user_id: str) -> Business:
    """The caller's own business, or 404 (never 403 — see module docstring)."""
    biz = await db.scalar(select(Business).where(Business.owner_user_id == user_id))
    if not biz:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No business yet"
        )
    return biz


async def _load_owned_branch(
    db: AsyncSession, business_id: str, branch_id: str
) -> Branch:
    """Scope the branch lookup by business_id — the tenancy guard."""
    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return branch


async def _insert_branch_with_free_slug(
    db: AsyncSession, *, business_id: str, slug_base: str, **fields
) -> Branch:
    """Insert a `branches` row, walking past slugs that are already taken.

    Picking a free slug by reading and then inserting is a check-then-act race:
    two transactions both read "free", both insert, and the loser's `flush()`
    raises `IntegrityError`, which used to escape the handler as an unexplained
    **500**. Two owners naming their branches the same thing is a completely
    ordinary event, not a fault, so it is handled here rather than surfaced.

    Each attempt runs inside its **own savepoint**. On a slug clash the savepoint
    is rolled back — which discards only that attempt and leaves the caller's
    outer transaction, including a business row created earlier in the same
    request, completely usable — and the next candidate is tried. Because the
    object is re-created per attempt, nothing is left in a half-inserted state in
    the identity map.

    Only a unique violation on `branches` is retried. A foreign-key or NOT NULL
    failure re-raises, so an unexpected database error is never disguised as a
    successfully created branch.
    """
    for candidate in _slug_candidates(slug_base):
        savepoint = await db.begin_nested()
        try:
            # Cheap read first: it uses the unique index and avoids provoking a
            # write error for a collision we could already see.
            taken = await db.scalar(select(Branch.id).where(Branch.slug == candidate))
            if taken is not None:
                await savepoint.rollback()
                continue

            branch = Branch(business_id=business_id, slug=candidate, **fields)
            db.add(branch)
            await db.flush()
        except IntegrityError as exc:
            await savepoint.rollback()
            if not _is_unique_violation_on(exc, "branches"):
                raise
            # Lost the race for this slug — somebody committed it between our
            # read and our insert. Move on to the next candidate.
            continue
        else:
            # `commit()` on a nested transaction issues RELEASE SAVEPOINT — it
            # does NOT commit the outer transaction. The row stays inside the
            # request's transaction and is still discarded if that rolls back.
            # (The async transaction object has no `close()`; this is the
            # async equivalent of "release without committing".)
            await savepoint.commit()
            return branch

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "Could not allocate a free branch slug for this branch name. "
            "Retry with a different branch name."
        ),
    )


async def _open_first_branch(
    db: AsyncSession, biz: Business, payload: BusinessCreateRequest
) -> Branch:
    """Create the business's first location and return it.

    Shared by the create path and the idempotent replay path so a resumed
    onboarding produces a branch identical to a first-attempt one — including
    the contact details, which the replay would otherwise silently drop.

    This only ever *adds* a branch under an existing business. It never changes
    an existing branch, so a replay can never silently rewrite a branch's slug.
    """
    return await _insert_branch_with_free_slug(
        db,
        business_id=str(biz.id),
        slug_base=payload.branch_name,
        name=payload.branch_name,
        county=payload.county,
        town=payload.town,
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        num_chairs=payload.num_chairs,
        address=payload.branch_address,
        phone=payload.branch_phone,
        is_active=True,
    )


async def _open_trial_subscription(db: AsyncSession, biz: Business) -> None:
    """Open the trial subscription that ships with a brand-new business.

    Done now, on the cheapest seeded plan, rather than lazily at first
    billing-page view: H1 reads this row to show "N days left", and a trial that
    starts whenever the owner happened to look is a trial that silently lengthens
    every time the app is opened.

    A missing plan catalogue is not an error — the business and its branch are
    already valid, and billing can be handed a plan later.
    """
    trial_plan = await db.scalar(
        select(Plan).where(Plan.code == "daily", Plan.is_active.is_(True))
    )
    if not trial_plan:
        return

    now = now_utc()
    db.add(
        Subscription(
            business_id=str(biz.id),
            # `plan_id` is a String column, not a UUID one. Passing the raw
            # UUID object hands asyncpg a UUID for a VARCHAR parameter and
            # it raises `TypeError: expected str, got UUID` — so in any
            # deployment where the seeded plan catalogue exists, business
            # creation failed outright and the trial never opened.
            plan_id=str(trial_plan.id),
            status="trial",
            current_period_start=now,
            current_period_end=now + timedelta(days=settings.trial_days),
        )
    )
    await db.flush()


async def _replay_after_conflict(
    db: AsyncSession,
    user_id: str,
    payload: BusinessCreateRequest,
    response: Response,
    exc: IntegrityError,
) -> BusinessCreateResponse:
    """Turn an expected duplicate into the same 200 replay the read path returns.

    A unique violation on `businesses` or `subscriptions` under PostgreSQL's
    READ COMMITTED isolation means a competing transaction **committed** first —
    if it had rolled back, this insert would have succeeded instead. So the
    winning row is guaranteed to be visible to a re-read on this connection, and
    one read is enough; there is no polling and no sleep.

    `create_business` calls this only *after* rolling back its savepoint, so the
    session is in a clean state and this read runs in the request's still-usable
    outer transaction.

    Anything that is not one of those two constraints is an unexpected database
    failure and is re-raised, so it surfaces as a 500 rather than being dressed
    up as a successful replay.
    """
    if not (
        _is_unique_violation_on(exc, "businesses")
        or _is_unique_violation_on(exc, "subscriptions")
    ):
        raise exc

    winner = await load_onboarding_state(db, user_id)
    if winner.business is None:
        # The index says this owner already owns a business, but no such row is
        # readable. Returning a replay here would be a fabricated success, and
        # returning 201 would be a fabricated creation. Fail loudly instead —
        # this should be unreachable, and if it ever fires the operator needs to
        # see it rather than have it papered over.
        raise exc

    branch = winner.first_branch
    if branch is None:
        # The winning transaction committed a business with no first branch. Add
        # it under that same business rather than creating a second tenant.
        branch = await _open_first_branch(db, winner.business, payload)

    response.status_code = status.HTTP_200_OK
    return BusinessCreateResponse(
        business=business_out(winner.business),
        branch=branch_out(branch),
        next=winner.next_step,
    )


# ── B1 — create business + first branch ──────────────────────────────────────
@router.post(
    "/business",
    response_model=BusinessCreateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": BusinessCreateResponse,
            "description": (
                "Idempotent replay — this owner already has a business, so the "
                "existing one is returned unchanged and nothing is written."
            ),
        }
    },
)
async def create_business(
    payload: BusinessCreateRequest,
    response: Response,
    db: Db,
    user: ActiveUser,
    _role: str = OwnerOnly,
) -> BusinessCreateResponse:
    """Onboarding screen 2. Idempotent per owner, and only for active accounts.

    **Why this is not a 409.** The obvious "already exists" conflict is wrong for
    a client that cannot tell a lost response from a failed one. A phone that
    sleeps mid-request, a proxy timeout, a double-tapped submit — all of them
    leave the owner staring at a spinner with a business that already exists and
    no way to tell. Returning 409 taught them to retry forever; returning the
    resource they already own ends the loop on the same call.

    So a repeat submission is a **read**, not an error: the existing business and
    its first branch come back, nothing is written, and the caller proceeds to
    the same next step it would have reached on the first attempt. That is what
    makes retrying safe rather than merely tolerated.

    The account check comes from `ActiveUser`, not the token alone. See
    `app.core.deps.get_active_user` for why a 15-minute access token outlives a
    suspension, and why creating the tenant row is the last thing it should be
    allowed to do.
    """
    user_id = user["user_id"]

    # ── Layer 1: serialise per owner ──────────────────────────────────────────
    # The read below and the inserts after it are a check-then-act pair, and two
    # requests for the same owner can both pass the check before either commits
    # (a double-tapped button, two tabs, a lost response and a retry). Taking a
    # row lock on the *owner's own user row* — which always exists, because
    # `ActiveUser` just verified it — makes those requests queue up instead.
    # The second one then observes the first one's committed business and takes
    # the ordinary replay path below.
    #
    # The key is the `user_id` from the **token**, never anything in the payload,
    # so this cannot be used to lock another tenant's row.
    await db.execute(
        text("SELECT 1 FROM users WHERE id = :uid FOR UPDATE"), {"uid": user_id}
    )

    existing = await load_onboarding_state(db, user_id)
    if existing.business is not None:
        branch = existing.first_branch or await _open_first_branch(
            db, existing.business, payload
        )
        response.status_code = status.HTTP_200_OK
        return BusinessCreateResponse(
            business=business_out(existing.business),
            branch=branch_out(branch),
            next=existing.next_step,
        )

    # ── Layer 3: a uniqueness clash is a replay, not a 500 ────────────────────
    # The whole create block runs inside one savepoint. If the unique index on
    # `businesses.owner_user_id` fires — from a concurrent writer this lock does
    # not cover, from a second app instance on another connection pool, or from
    # anything else — we unwind to the savepoint and fall through to the same
    # replay the sequential path returns. Crucially this rolls back *only* the
    # failed attempt: the request's outer transaction stays usable, which is
    # what lets the re-read below run in it.
    create_savepoint = await db.begin_nested()
    try:
        biz = Business(
            owner_user_id=user_id,
            name=payload.name,
            business_type=payload.business_type,
            logo_url=payload.logo_url,
            county=payload.county,
            town=payload.town,
            status="trialing",
            trial_ends_at=now_utc() + timedelta(days=settings.trial_days),
            chat_enabled=payload.chat_enabled,
        )
        db.add(biz)
        await db.flush()  # need biz.id before the branch can reference it

        branch = await _open_first_branch(db, biz, payload)
        await _open_trial_subscription(db, biz)
    except IntegrityError as exc:
        await create_savepoint.rollback()
        return await _replay_after_conflict(db, user_id, payload, response, exc)
    else:
        # RELEASE SAVEPOINT, not COMMIT. Business, branch and trial are all
        # still inside the request's transaction and are committed together by
        # `get_db` afterwards — or none of them are.
        await create_savepoint.commit()

    return BusinessCreateResponse(
        business=business_out(biz),
        branch=branch_out(branch),
        next=OnboardingState(
            user_id=user_id,
            account_status=user["account_status"],
            business=biz,
            first_branch=branch,
            branch_count=1,
            is_staff=False,
        ).next_step,
    )


# ── B2 — my business summary ────────────────────────────────────────────────
@router.get("/business", response_model=BusinessResponse)
async def get_business(db: Db, user: CurrentUser) -> BusinessResponse:
    """Status, trial window, and the active plan code if a subscription exists."""
    biz = await _load_owned_business(db, user["user_id"])

    plan_code = None
    sub = await db.scalar(
        select(Subscription)
        .where(
            Subscription.business_id == str(biz.id),
            Subscription.status.in_(("trial", "active", "grace")),
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    if sub:
        plan = await db.scalar(select(Plan).where(Plan.id == sub.plan_id))
        plan_code = plan.code if plan else None

    return business_out(biz, plan_code)


# ── B3 — update business ─────────────────────────────────────────────────────
@router.patch("/business", response_model=BusinessResponse)
async def update_business(
    payload: BusinessUpdateRequest,
    db: Db,
    user: CurrentUser,
    _role: str = OwnerOnly,
) -> BusinessResponse:
    """Owner-editable fields only — status and trial dates move via billing."""
    biz = await _load_owned_business(db, user["user_id"])

    for field in ("name", "logo_url", "chat_enabled"):
        value = getattr(payload, field)
        if value is not None:
            setattr(biz, field, value)

    return business_out(biz)


# ── B4 — list / add branches ─────────────────────────────────────────────────
@router.get("/branches", response_model=list[BranchResponse])
async def list_branches(db: Db, user: CurrentUser) -> list[BranchResponse]:
    """All branches of the caller's business. Only `business_id` is filtered."""
    biz = await _load_owned_business(db, user["user_id"])
    rows = await db.scalars(
        select(Branch)
        .where(Branch.business_id == str(biz.id))
        .order_by(Branch.created_at)
    )
    return [branch_out(b) for b in rows]


@router.post(
    "/branches",
    response_model=BranchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_branch(
    payload: BranchCreateRequest,
    db: Db,
    user: CurrentUser,
    _role: str = OwnerOnly,
) -> BranchResponse:
    """Multi-branch support (OW3) — one owner, many locations."""
    biz = await _load_owned_business(db, user["user_id"])
    branch = await _insert_branch_with_free_slug(
        db,
        business_id=str(biz.id),
        slug_base=payload.name,
        name=payload.name,
        county=payload.county,
        town=payload.town,
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        num_chairs=payload.num_chairs,
        is_active=True,
    )
    return branch_out(branch)


# ── B5 — read / update one branch ────────────────────────────────────────────
@router.get("/branches/{branch_id}", response_model=BranchResponse)
async def get_branch(branch_id: str, db: Db, user: CurrentUser) -> BranchResponse:
    biz = await _load_owned_business(db, user["user_id"])
    return branch_out(await _load_owned_branch(db, str(biz.id), branch_id))


@router.patch("/branches/{branch_id}", response_model=BranchResponse)
async def update_branch(
    branch_id: str, payload: BranchUpdateRequest, db: Db, user: CurrentUser
) -> BranchResponse:
    """Working hours + chair count (clerk-writable, owner-writable)."""
    biz = await _load_owned_business(db, user["user_id"])
    branch = await _load_owned_branch(db, str(biz.id), branch_id)

    for field in (
        "name",
        "county",
        "town",
        "opens_at",
        "closes_at",
        "num_chairs",
        "is_active",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(branch, field, value)

    return branch_out(branch)


# ── B6 — service catalogue ───────────────────────────────────────────────────
@router.get("/branches/{branch_id}/services", response_model=list[ServiceResponse])
async def list_services(
    branch_id: str, db: Db, user: CurrentUser
) -> list[ServiceResponse]:
    biz = await _load_owned_business(db, user["user_id"])
    await _load_owned_branch(db, str(biz.id), branch_id)
    rows = await db.scalars(
        select(Service).where(Service.branch_id == branch_id).order_by(Service.name)
    )
    return [_service_out(s) for s in rows]


@router.post(
    "/branches/{branch_id}/services",
    response_model=ServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_service(
    branch_id: str, payload: ServiceCreateRequest, db: Db, user: CurrentUser
) -> ServiceResponse:
    biz = await _load_owned_business(db, user["user_id"])
    await _load_owned_branch(db, str(biz.id), branch_id)
    svc = Service(
        branch_id=branch_id,
        name=payload.name,
        price_kes=payload.price_kes,
        duration_min=payload.duration_min,
        is_active=payload.is_active,
    )
    db.add(svc)
    await db.flush()
    return _service_out(svc)


@router.patch(
    "/branches/{branch_id}/services/{service_id}", response_model=ServiceResponse
)
async def update_service(
    branch_id: str,
    service_id: str,
    payload: ServiceUpdateRequest,
    db: Db,
    user: CurrentUser,
) -> ServiceResponse:
    biz = await _load_owned_business(db, user["user_id"])
    await _load_owned_branch(db, str(biz.id), branch_id)
    svc = await db.scalar(
        select(Service).where(Service.id == service_id, Service.branch_id == branch_id)
    )
    if not svc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    for field in ("name", "price_kes", "duration_min", "is_active"):
        value = getattr(payload, field)
        if value is not None:
            setattr(svc, field, value)

    return _service_out(svc)


@router.delete(
    "/branches/{branch_id}/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_service(
    branch_id: str, service_id: str, db: Db, user: CurrentUser
) -> None:
    """Soft-delete: keeps historical sales/appointments referencing the service intact."""
    biz = await _load_owned_business(db, user["user_id"])
    await _load_owned_branch(db, str(biz.id), branch_id)
    svc = await db.scalar(
        select(Service).where(Service.id == service_id, Service.branch_id == branch_id)
    )
    if not svc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    svc.is_active = False
    return None
