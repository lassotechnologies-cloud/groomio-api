"""Business / Branch / Service routes — API group B (Doc 10.3: B1–B6).

Onboarding step 2: an owner registers and verifies their phone (group A), then
creates their business plus a first branch here. The business is created with
`status = "trialing"` and a `trial_ends_at` window (config `trial_days`), so the
shop can start working before any money changes hands (Doc 2.4).

Multi-tenancy rule (Doc 10.1): the tenant is derived from the caller's token —
clients never send a `business_id`. A branch belonging to another tenant returns
404, never 403, so cross-tenant probing reveals nothing.
"""

import re
from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
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
from app.core.deps import CurrentUser, require_role
from app.core.security import now_utc
from app.models.billing import Plan, Subscription
from app.models.tenant import Branch, Business, Service

router = APIRouter(tags=["business"])

Db = Annotated[AsyncSession, Depends(get_db)]
OwnerOnly = Depends(require_role("owner"))


# ── helpers ──────────────────────────────────────────────────────────────────
def _business_out(biz: Business, plan_code: Optional[str] = None) -> BusinessResponse:
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


async def _unique_slug(db: AsyncSession, name: str) -> str:
    """Derive a URL-safe, unique branch slug for public booking links (D3/D8)."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "branch"
    candidate, suffix = base, 1
    while await db.scalar(select(Branch.id).where(Branch.slug == candidate)):
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _branch_out(branch: Branch) -> BranchResponse:
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


# ── B1 — create business + first branch ──────────────────────────────────────
@router.post(
    "/business",
    response_model=BusinessCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def create_business(
    payload: BusinessCreateRequest, db: Db, user: CurrentUser
) -> BusinessCreateResponse:
    """Onboarding screen 2. Idempotent per owner: a second call 409s."""
    user_id = user["user_id"]
    if await db.scalar(select(Business).where(Business.owner_user_id == user_id)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Business already exists"
        )

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

    branch = Branch(
        business_id=str(biz.id),
        slug=await _unique_slug(db, payload.branch_name),
        name=payload.branch_name,
        county=payload.county,
        town=payload.town,
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        num_chairs=payload.num_chairs,
        is_active=True,
    )
    db.add(branch)
    await db.flush()

    # Open a trial subscription now, on the cheapest seeded plan. H1 reads this
    # row to show "N days left"; creating it lazily at first billing-page view
    # would make the trial countdown start whenever the owner happened to look,
    # which is a trial that silently lengthens every time the app is opened.
    trial_plan = await db.scalar(
        select(Plan).where(Plan.code == "daily", Plan.is_active.is_(True))
    )
    if trial_plan:
        now = now_utc()
        db.add(
            Subscription(
                business_id=str(biz.id),
                plan_id=trial_plan.id,
                status="trial",
                current_period_start=now,
                current_period_end=now + timedelta(days=settings.trial_days),
            )
        )
        await db.flush()

    return BusinessCreateResponse(
        business=_business_out(biz), branch=_branch_out(branch), next="subscription"
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

    return _business_out(biz, plan_code)


# ── B3 — update business ─────────────────────────────────────────────────────
@router.patch("/business", response_model=BusinessResponse, dependencies=[OwnerOnly])
async def update_business(
    payload: BusinessUpdateRequest, db: Db, user: CurrentUser
) -> BusinessResponse:
    """Owner-editable fields only — status and trial dates move via billing."""
    biz = await _load_owned_business(db, user["user_id"])

    for field in ("name", "logo_url", "chat_enabled"):
        value = getattr(payload, field)
        if value is not None:
            setattr(biz, field, value)

    return _business_out(biz)


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
    return [_branch_out(b) for b in rows]


@router.post(
    "/branches",
    response_model=BranchResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def create_branch(
    payload: BranchCreateRequest, db: Db, user: CurrentUser
) -> BranchResponse:
    """Multi-branch support (OW3) — one owner, many locations."""
    biz = await _load_owned_business(db, user["user_id"])
    branch = Branch(
        business_id=str(biz.id),
        slug=await _unique_slug(db, payload.name),
        name=payload.name,
        county=payload.county,
        town=payload.town,
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        num_chairs=payload.num_chairs,
        is_active=True,
    )
    db.add(branch)
    await db.flush()
    return _branch_out(branch)


# ── B5 — read / update one branch ────────────────────────────────────────────
@router.get("/branches/{branch_id}", response_model=BranchResponse)
async def get_branch(branch_id: str, db: Db, user: CurrentUser) -> BranchResponse:
    biz = await _load_owned_business(db, user["user_id"])
    return _branch_out(await _load_owned_branch(db, str(biz.id), branch_id))


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

    return _branch_out(branch)


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
