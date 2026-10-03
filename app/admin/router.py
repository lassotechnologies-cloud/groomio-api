"""Admin routes — API group H7–H10 (Doc 10.9), super_admin only.

Support tooling, not a product surface. Everything here is gated on
`super_admin`, and every write is audited: the person who extended a trial or
suspended a tenant is recorded, because those are exactly the actions a
disgruntled former owner will dispute three months later.

The audit log is written in the same transaction as the change. If the audit
insert fails, so does the change — an unlogged suspension is worse than no
suspension, because it looks like it did not happen.
"""

from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    AdminDashboardResponse,
    SuspendRequest,
    TenantStatusResponse,
    TrialExtendRequest,
)
from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.security import now_utc
from app.models.billing import Payment, Plan, Subscription
from app.models.platform import AuditLog
from app.models.tenant import Business

router = APIRouter(prefix="/admin", tags=["admin"])

Db = Annotated[AsyncSession, Depends(get_db)]
SuperAdminOnly = Depends(require_role("super_admin"))


def _audit(
    db: AsyncSession,
    *,
    actor_user_id: str,
    action: str,
    entity: str,
    entity_id: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            business_id=None,  # platform-level action, not a tenant action
            actor_user_id=actor_user_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before_json=before,
            after_json=after,
        )
    )


# ── H7 — extend a trial ───────────────────────────────────────────────────
@router.post(
    "/trial-extend", response_model=TenantStatusResponse, dependencies=[SuperAdminOnly]
)
async def trial_extend(
    payload: TrialExtendRequest, db: Db, user: CurrentUser
) -> TenantStatusResponse:
    """H7 — add N days to a shop's trial. Support's most-used button in the
    first month: a shop signs up on Friday and the trial ends before they have
    served anyone."""
    biz = await db.scalar(select(Business).where(Business.id == payload.business_id))
    if not biz:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Business not found"
        )

    before = {
        "trial_ends_at": biz.trial_ends_at.isoformat() if biz.trial_ends_at else None,
        "status": biz.status,
    }

    # Extend from the existing end date, not from today. A shop whose trial ended
    # yesterday and gets three more days should reach the end of those three
    # days, not have yesterday's shortfall cancelled out.
    anchor = biz.trial_ends_at or now_utc()
    if anchor < now_utc():
        anchor = now_utc()
    biz.trial_ends_at = anchor + timedelta(days=payload.days)
    # A suspended shop stays suspended; extending a trial is not a reinstatement.
    if biz.status == "expired":
        biz.status = "trialing"

    _audit(
        db,
        actor_user_id=user["user_id"],
        action="trial_extend",
        entity="business",
        entity_id=str(biz.id),
        before=before,
        after={
            "trial_ends_at": biz.trial_ends_at.isoformat(),
            "status": biz.status,
            "days": payload.days,
            "reason": payload.reason,
        },
    )
    await db.flush()
    return await _tenant_out(db, biz)


# ── H8 — list tenants ──────────────────────────────────────────────────────
@router.get(
    "/tenants", response_model=list[TenantStatusResponse], dependencies=[SuperAdminOnly]
)
async def list_tenants(
    db: Db,
    _user: CurrentUser,
    status_filter: Optional[str] = Query(
        None, alias="status", description="trialing/active/grace/expired/suspended"
    ),
    q: Optional[str] = Query(None, description="Business or owner name"),
    limit: int = Query(50, ge=1, le=200),
) -> list[TenantStatusResponse]:
    """H8 — the support queue: who is on which plan and in what state."""
    stmt = select(Business)
    if status_filter:
        stmt = stmt.where(Business.status == status_filter)
    if q:
        stmt = stmt.where(Business.name.ilike(f"%{q}%"))
    rows = await db.scalars(stmt.order_by(Business.created_at.desc()).limit(limit))
    return [await _tenant_out(db, b) for b in rows]


# ── H9 — suspend / reactivate ──────────────────────────────────────────────
@router.post(
    "/tenants/{business_id}/suspend",
    response_model=TenantStatusResponse,
    dependencies=[SuperAdminOnly],
)
async def suspend_tenant(
    business_id: str, payload: SuspendRequest, db: Db, user: CurrentUser
) -> TenantStatusResponse:
    """H9 — suspend a tenant, or lift a suspension with `reactivate: true`.

    Suspension is a status on the business, not a deletion: the shop's data,
    appointments and customer history all stay, because a non-paying shop that
    comes back should find its customers still there.
    """
    biz = await db.scalar(select(Business).where(Business.id == business_id))
    if not biz:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Business not found"
        )
    if not payload.reactivate and biz.status == "suspended":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already suspended"
        )

    before = {"status": biz.status}
    biz.status = "trialing" if payload.reactivate else "suspended"

    _audit(
        db,
        actor_user_id=user["user_id"],
        action="suspend_reactivate" if payload.reactivate else "suspend",
        entity="business",
        entity_id=str(biz.id),
        before=before,
        after={"status": biz.status, "reason": payload.reason},
    )
    await db.flush()
    return await _tenant_out(db, biz)


# ── H10 — platform dashboard ───────────────────────────────────────────────
@router.get(
    "/dashboard", response_model=AdminDashboardResponse, dependencies=[SuperAdminOnly]
)
async def admin_dashboard(db: Db, _user: CurrentUser) -> AdminDashboardResponse:
    """H10 — signups, conversions, revenue.

    Revenue counts only `status = 'success'` payments, and only amounts actually
    received. Counting pending M-Pesa pushes as revenue is the single easiest way
    to make a dashboard that flatters you and is wrong.
    """
    total = await db.scalar(select(func.count(Business.id))) or 0

    by_status = dict(
        (
            await db.execute(
                select(Business.status, func.count(Business.id)).group_by(
                    Business.status
                )
            )
        ).all()
    )

    active_subs = (
        await db.scalar(
            select(func.count(Subscription.id)).where(Subscription.status == "active")
        )
        or 0
    )

    # Businesses that have ever paid — the denominator for conversion. Using total
    # signups would count every shop that never finished onboarding.
    ever_paid = (
        await db.scalar(
            select(func.count(func.distinct(Payment.business_id))).where(
                Payment.status == "success"
            )
        )
        or 0
    )
    conversion = int(active_subs * 100 / ever_paid) if ever_paid else 0

    month_start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_month, revenue_all, pay_count = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(Payment.amount_kes).filter(Payment.paid_at >= month_start),
                    0,
                ),
                func.coalesce(func.sum(Payment.amount_kes), 0),
                func.count(Payment.id).filter(Payment.paid_at >= month_start),
            ).where(Payment.status == "success")
        )
    ).one()

    return AdminDashboardResponse(
        total_businesses=int(total),
        active_subscriptions=int(active_subs),
        trialing_businesses=int(by_status.get("trialing", 0)),
        suspended_businesses=int(by_status.get("suspended", 0)),
        expired_businesses=int(by_status.get("expired", 0)),
        conversion_rate_pct=conversion,
        revenue_this_month_kes=int(revenue_month or 0),
        revenue_all_time_kes=int(revenue_all or 0),
        payments_this_month=int(pay_count or 0),
    )


# ── serializer ─────────────────────────────────────────────────────────────
async def _tenant_out(db: AsyncSession, biz: Business) -> TenantStatusResponse:
    sub = await db.scalar(
        select(Subscription)
        .where(
            Subscription.business_id == str(biz.id),
            Subscription.status.in_(("trial", "active", "grace")),
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    plan_code = None
    if sub:
        plan = await db.scalar(select(Plan).where(Plan.id == sub.plan_id))
        plan_code = plan.code if plan else None

    return TenantStatusResponse(
        id=str(biz.id),
        name=biz.name,
        town=biz.town,
        status=biz.status,
        plan_code=plan_code,
        trial_ends_at=biz.trial_ends_at,
        created_at=biz.created_at,
    )
