"""Loyalty routes — API group G3–G6 (Doc 10.7).

Three separate schemes share this router because a salon runs all three at once
and a customer should be able to hold a balance, a membership, and referrals:

  points     (G3)  earned 1 per KES 100 spent, automatic at the counter
  membership (G4/5) "10 cuts for the price of 8", sold at the counter
  referrals  (G6)  a friend joins, both sides get points

Points are never negative. A redemption that would take a customer below zero is
refused rather than allowed, because a customer who owes points to the shop is a
concept that does not exist in this market and would only ever be a bug.
"""

from datetime import date, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.phone import normalize_phone
from app.core.tenancy import resolve_business_id
from app.loyalty.schemas import (
    AttachMembershipRequest,
    LoyaltyBalanceResponse,
    LoyaltyEntryResponse,
    MembershipPlanCreateRequest,
    MembershipPlanResponse,
    MembershipPlanUpdateRequest,
    MembershipResponse,
    RedeemRequest,
    RedeemResponse,
    ReferralConvertRequest,
    ReferralCreateRequest,
    ReferralResponse,
)
from app.models.loyalty import (
    CustomerMembership,
    LoyaltyTransaction,
    MembershipPlan,
    Referral,
)
from app.models.people import Customer
from app.models.tenant import Branch

router = APIRouter(tags=["loyalty"])

Db = Annotated[AsyncSession, Depends(get_db)]
StaffOnly = Depends(require_role("owner", "clerk", "barber"))
OwnerOnly = Depends(require_role("owner"))

# Earning (POST /sales) grants 1 point per whole KES 100. Redemption pays
# 10 points = KES 1, i.e. roughly 0.1% of spend back. The gap is deliberate: a
# loyalty scheme that gives back more than it earns is not a scheme, it is a cost
# centre with a QR code.
POINTS_PER_KES = 10
# 50 points = KES 5, for a referral that converts. Small enough to be an
# impulse ask ("I'll give you fifty bob"), large enough to be worth the ask.
DEFAULT_REFERRAL_REWARD_POINTS = 50


# ── helpers ────────────────────────────────────────────────────────────────
async def _customer(db: AsyncSession, business_id: str, customer_id: str) -> Customer:
    customer = await db.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.business_id == business_id
        )
    )
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )
    return customer


async def _branch(db: AsyncSession, business_id: str, branch_id: str) -> Branch:
    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )
    return branch


def _entry(t: LoyaltyTransaction) -> LoyaltyEntryResponse:
    return LoyaltyEntryResponse(
        id=str(t.id),
        points=t.points,
        reason=t.reason,
        sale_id=t.sale_id,
        created_at=t.created_at,
    )


# ── G3 — points balance and redemption ─────────────────────────────────────
@router.get(
    "/customers/{customer_id}/loyalty",
    response_model=LoyaltyBalanceResponse,
    dependencies=[StaffOnly],
)
async def get_loyalty(
    customer_id: str, db: Db, user: CurrentUser, limit: int = Query(50, ge=1, le=200)
) -> LoyaltyBalanceResponse:
    """G3 — the balance and the movements behind it."""
    business_id = await resolve_business_id(db, user)
    customer = await _customer(db, business_id, customer_id)

    entries = list(
        await db.scalars(
            select(LoyaltyTransaction)
            .where(LoyaltyTransaction.customer_id == customer_id)
            .order_by(LoyaltyTransaction.created_at.desc())
            .limit(limit)
        )
    )
    points = int(customer.loyalty_points or 0)
    return LoyaltyBalanceResponse(
        customer_id=customer_id,
        points=points,
        redeemable_kes=points // POINTS_PER_KES,
        entries=[_entry(t) for t in entries],
    )


@router.post(
    "/customers/{customer_id}/loyalty/redeem",
    response_model=RedeemResponse,
    dependencies=[StaffOnly],
)
async def redeem_points(
    customer_id: str, payload: RedeemRequest, db: Db, user: CurrentUser
) -> RedeemResponse:
    """G3 — trade points for a discount on the current bill.

    This records the movement only; it does not apply a discount to a sale. The
    clerk reads the KES value off the response and enters it as a line, so the
    sale total and the points ledger stay separately auditable.
    """
    business_id = await resolve_business_id(db, user)
    customer = await _customer(db, business_id, customer_id)

    points_needed = payload.amount_kes * POINTS_PER_KES
    balance = int(customer.loyalty_points or 0)
    if points_needed > balance:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Needs {points_needed} points for KES {payload.amount_kes}; "
                f"balance is {balance}"
            ),
        )

    customer.loyalty_points = balance - points_needed
    entry = LoyaltyTransaction(
        customer_id=customer_id,
        # Negative: the ledger stores movements, not magnitudes.
        points=-points_needed,
        reason="redemption",
    )
    db.add(entry)
    await db.flush()

    return RedeemResponse(
        customer_id=customer_id,
        points_spent=points_needed,
        amount_kes=payload.amount_kes,
        remaining_points=customer.loyalty_points,
        entry=_entry(entry),
    )


# ── G4 — membership plans ──────────────────────────────────────────────────
def _plan_out(p: MembershipPlan) -> MembershipPlanResponse:
    return MembershipPlanResponse(
        id=str(p.id),
        branch_id=p.branch_id,
        name=p.name,
        price_kes=p.price_kes,
        sessions_included=p.sessions_included,
        validity_days=p.validity_days,
        per_session_kes=p.price_kes // p.sessions_included,
    )


@router.get(
    "/branches/{branch_id}/membership-plans",
    response_model=list[MembershipPlanResponse],
    dependencies=[StaffOnly],
)
async def list_plans(
    branch_id: str, db: Db, user: CurrentUser
) -> list[MembershipPlanResponse]:
    """G4 — what this shop sells as a membership."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)
    plans = await db.scalars(
        select(MembershipPlan)
        .where(MembershipPlan.branch_id == branch_id)
        .order_by(MembershipPlan.price_kes)
    )
    return [_plan_out(p) for p in plans]


@router.post(
    "/branches/{branch_id}/membership-plans",
    response_model=MembershipPlanResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def create_plan(
    branch_id: str, payload: MembershipPlanCreateRequest, db: Db, user: CurrentUser
) -> MembershipPlanResponse:
    """G4 — define what a membership costs. Owner-only: it sets the shop's margin."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    plan = MembershipPlan(
        branch_id=branch_id,
        name=payload.name,
        price_kes=payload.price_kes,
        sessions_included=payload.sessions_included,
        validity_days=payload.validity_days,
    )
    db.add(plan)
    await db.flush()
    return _plan_out(plan)


@router.patch(
    "/branches/{branch_id}/membership-plans/{plan_id}",
    response_model=MembershipPlanResponse,
    dependencies=[OwnerOnly],
)
async def update_plan(
    branch_id: str,
    plan_id: str,
    payload: MembershipPlanUpdateRequest,
    db: Db,
    user: CurrentUser,
) -> MembershipPlanResponse:
    """G4 — reprice or rename. Existing memberships keep the terms they bought."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    plan = await db.scalar(
        select(MembershipPlan).where(
            MembershipPlan.id == plan_id, MembershipPlan.branch_id == branch_id
        )
    )
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )

    for field in ("name", "price_kes", "sessions_included", "validity_days"):
        value = getattr(payload, field)
        if value is not None:
            setattr(plan, field, value)

    await db.flush()
    return _plan_out(plan)


# ── G5 — attach a customer to a plan ───────────────────────────────────────
def _membership_out(
    m: CustomerMembership, plans: dict[str, MembershipPlan], today: date
) -> MembershipResponse:
    """Serialize a membership. `plans` is loaded by the caller and keyed by plan id.

    A plan deleted after purchase leaves its membership without a name and without
    a session count — the membership row stays, because a customer who paid for
    ten cuts should not lose them to a catalogue cleanup.
    """
    plan = plans.get(m.plan_id)
    remaining = max(plan.sessions_included - m.sessions_used, 0) if plan else 0
    expires = m.expires_at
    return MembershipResponse(
        id=str(m.id),
        customer_id=m.customer_id,
        plan_id=m.plan_id,
        plan_name=plan.name if plan else None,
        started_at=m.started_at,
        expires_at=expires,
        sessions_used=m.sessions_used,
        sessions_remaining=remaining,
        is_active=bool(expires and expires >= today),
    )


async def _plans_by_id(
    db: AsyncSession, plan_ids: set[str]
) -> dict[str, MembershipPlan]:
    if not plan_ids:
        return {}
    return {
        str(p.id): p
        for p in await db.scalars(
            select(MembershipPlan).where(MembershipPlan.id.in_(plan_ids))
        )
    }


@router.get(
    "/customers/{customer_id}/memberships",
    response_model=list[MembershipResponse],
    dependencies=[StaffOnly],
)
async def list_memberships(
    customer_id: str, db: Db, user: CurrentUser
) -> list[MembershipResponse]:
    """G5 — what this customer holds, with remaining sessions and expiry."""
    business_id = await resolve_business_id(db, user)
    await _customer(db, business_id, customer_id)
    today = date.today()

    rows = list(
        await db.scalars(
            select(CustomerMembership)
            .where(CustomerMembership.customer_id == customer_id)
            .order_by(CustomerMembership.expires_at.desc())
        )
    )
    if not rows:
        return []

    plans = await _plans_by_id(db, {r.plan_id for r in rows})
    return [_membership_out(m, plans, today) for m in rows]


@router.post(
    "/customers/{customer_id}/memberships",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[StaffOnly],
)
async def attach_membership(
    customer_id: str, payload: AttachMembershipRequest, db: Db, user: CurrentUser
) -> MembershipResponse:
    """G5 — sell a membership. Expiry is computed from the plan's validity, not
    supplied by the client."""
    business_id = await resolve_business_id(db, user)
    customer = await _customer(db, business_id, customer_id)

    plan = await db.scalar(
        select(MembershipPlan).where(
            MembershipPlan.id == payload.plan_id,
            MembershipPlan.branch_id == customer.branch_id,
        )
    )
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan not offered at this customer's branch",
        )

    today = date.today()
    membership = CustomerMembership(
        customer_id=customer_id,
        plan_id=plan.id,
        started_at=today,
        expires_at=today + timedelta(days=plan.validity_days),
        sessions_used=0,
    )
    db.add(membership)
    await db.flush()

    return _membership_out(membership, {str(plan.id): plan}, today)


# ── G6 — referrals ─────────────────────────────────────────────────────────
def _referral_out(r: Referral) -> ReferralResponse:
    return ReferralResponse(
        id=str(r.id),
        referrer_customer_id=r.referrer_customer_id,
        referred_phone=r.referred_phone,
        status=r.status,
        points_awarded=r.points_awarded,
        created_at=r.created_at,
    )


@router.get(
    "/customers/{customer_id}/referrals",
    response_model=list[ReferralResponse],
    dependencies=[StaffOnly],
)
async def list_referrals(
    customer_id: str, db: Db, user: CurrentUser
) -> list[ReferralResponse]:
    """G6 — who this customer invited, and which invites have paid out."""
    business_id = await resolve_business_id(db, user)
    await _customer(db, business_id, customer_id)
    rows = await db.scalars(
        select(Referral)
        .where(Referral.referrer_customer_id == customer_id)
        .order_by(Referral.created_at.desc())
    )
    return [_referral_out(r) for r in rows]


@router.post(
    "/customers/{customer_id}/referrals",
    response_model=ReferralResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[StaffOnly],
)
async def create_referral(
    customer_id: str, payload: ReferralCreateRequest, db: Db, user: CurrentUser
) -> ReferralResponse:
    """G6 — record an invite. Stays `pending` until the friend actually arrives,
    so points are released on conversion, not on the promise."""
    business_id = await resolve_business_id(db, user)
    referrer = await _customer(db, business_id, customer_id)

    phone = normalize_phone(payload.referred_phone)
    if phone == referrer.phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot refer yourself",
        )

    existing = await db.scalar(
        select(Referral).where(
            Referral.referrer_customer_id == customer_id,
            Referral.referred_phone == phone,
            Referral.status != "pending",
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This number has already been referred and converted",
        )

    referral = Referral(
        referrer_customer_id=customer_id, referred_phone=phone, status="pending"
    )
    db.add(referral)
    await db.flush()
    return _referral_out(referral)


@router.post(
    "/customers/{customer_id}/referrals/{referral_id}/convert",
    response_model=LoyaltyBalanceResponse,
    dependencies=[StaffOnly],
)
async def convert_referral(
    customer_id: str,
    referral_id: str,
    payload: ReferralConvertRequest,
    db: Db,
    user: CurrentUser,
) -> LoyaltyBalanceResponse:
    """G6 — the referred friend has become a paying customer. Release the reward.

    Idempotent by design: converting twice is a no-op, because a clerk
    double-tapping a button must not pay the referral twice.
    """
    business_id = await resolve_business_id(db, user)
    customer = await _customer(db, business_id, customer_id)

    referral = await db.scalar(
        select(Referral).where(
            Referral.id == referral_id,
            Referral.referrer_customer_id == customer_id,
        )
    )
    if not referral:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Referral not found"
        )
    if referral.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Referral already {referral.status}",
        )

    points = payload.points_awarded or DEFAULT_REFERRAL_REWARD_POINTS
    referral.status = "rewarded"
    referral.points_awarded = points

    customer.loyalty_points = (customer.loyalty_points or 0) + points
    db.add(
        LoyaltyTransaction(customer_id=customer_id, points=points, reason="referral")
    )
    await db.flush()

    entries = list(
        await db.scalars(
            select(LoyaltyTransaction)
            .where(LoyaltyTransaction.customer_id == customer_id)
            .order_by(LoyaltyTransaction.created_at.desc())
            .limit(50)
        )
    )
    balance = int(customer.loyalty_points or 0)
    return LoyaltyBalanceResponse(
        customer_id=customer_id,
        points=balance,
        redeemable_kes=balance // POINTS_PER_KES,
        entries=[_entry(t) for t in entries],
    )
