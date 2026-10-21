"""Shared FastAPI dependencies — DB session + authenticated user."""

from typing import Annotated, Optional

from fastapi import Depends, Request, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.core.tenancy import (
    require_role as role_dependency,
    resolve_business_id,
)
from app.models.identity import User


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)):
    """Return the authenticated user's id + role from the Bearer token.

    Only a `type: "access"` token is accepted. A `"onboarding"` token — issued
    to an account that has registered but not yet verified its phone — is
    refused here on purpose, so it can never reach a business endpoint.

    This reads the token and nothing else. It deliberately does **not** consult
    the database: it answers "is this credential valid?", not "is this account
    still allowed to act?". `get_active_user` is the stricter dependency for
    endpoints that create state.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[len("Bearer ") :]
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    request.state.user_id = payload.get("sub")
    request.state.role = payload.get("role")
    request.state.tenant_id = payload.get("business_id")
    return {
        "user_id": payload.get("sub"),
        "role": payload.get("role"),
        "business_id": payload.get("business_id"),
    }


async def get_active_user(request: Request, db: AsyncSession = Depends(get_db)):
    """`get_current_user` plus a live check that the account is `active`.

    An access token is a bearer credential with a 15-minute life, and it knows
    nothing about what happened to the account behind it a minute ago. Every
    role check reads the `role` *claim*, so a suspended owner keeps asserting
    `role="owner"` until that token expires — and `/auth/login` and
    `/auth/refresh` both refuse to mint a replacement, so those 15 minutes are
    the entire window in which an administrator's suspension can be undone.

    That window is enough to create the tenant row itself, which is the worst
    possible moment to honour a revoked account: `businesses` is the root of
    every tenant-scoped query in the system. Reads are cheap enough to leave on
    `get_current_user` alone; anything that *writes* onboarding state uses this
    dependency instead.
    """
    user = await get_current_user(request, db)

    row = await db.scalar(select(User).where(User.id == user["user_id"]))
    if row is None:
        # The token verifies but names a user that no longer exists — a deleted
        # account whose token has not yet expired.
        raise HTTPException(status_code=401, detail="Account no longer exists")

    if row.status != "active":
        raise HTTPException(
            status_code=403,
            detail=(
                "Account verification required"
                if row.status == "pending"
                else "Account suspended"
            ),
        )

    return {**user, "account_status": row.status}


async def get_onboarding_user(request: Request, db: AsyncSession = Depends(get_db)):
    """Identify a user who may still be mid-signup.

    This is the *explicitly restricted* credential path. It accepts a
    `type: "onboarding"` token and returns only the user's id — deliberately no
    `role`, no `business_id`, no tenant. Anything that grants real authority
    must use `get_current_user` instead, so a pending registrant can reach the
    onboarding endpoints and nothing else.

    A verified user presenting a normal access token is also accepted, because
    an owner who is already signed in may legitimately revisit onboarding.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[len("Bearer ") :]
    payload = decode_token(token)
    if not payload or payload.get("type") not in ("onboarding", "access"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    verified = bool(payload.get("verified")) or payload.get("type") == "access"
    return {
        "user_id": payload.get("sub"),
        "verified": verified,
    }


async def require_active_business(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    """Business-scoped access guard: the caller's tenant must be live.

    An access token proves *who* is calling, not *whether their shop
    still has a subscription*. The lifecycle worker moves a lapsed
    tenant to `expired` and support can `suspend` one, but without a
    check like this those tenants keep full API access until their
    15-minute token expires — an expired shop could still create
    sales, appointments and customers.

    `expired` is a billing state that payment fixes, so it answers
    **402 Payment Required** — the client can route the owner to the
    payment screen. `suspended` is a support decision that payment
    does not fix, so it answers **403**. `trialing`, `active` and
    `grace` pass: grace is part of a live subscription by design.

    Deliberately **not** applied to the subscription router: an
    expired business must still reach `POST /v1/subscription/pay` to
    recover, and to `GET /v1/subscription` to see why they are blocked.
    """
    from app.models.tenant import Business

    business_id = await resolve_business_id(db, user)
    biz = await db.scalar(
        select(Business).where(Business.id == business_id)
    )
    if biz is None:
        raise HTTPException(status_code=404, detail="No business yet")
    if biz.status == "suspended":
        raise HTTPException(
            status_code=403, detail="Business suspended"
        )
    if biz.status == "expired":
        raise HTTPException(
            status_code=402, detail="Subscription expired"
        )
    return {**user, "business_id": business_id, "business_status": biz.status}


# Role-based access — re-exported so routers can do Depends(require_role(...))
def require_role(*roles: str):
    return role_dependency(*roles)


# Convenience type aliases
DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[dict, Depends(get_current_user)]
# For endpoints that create state. Strictly narrower than `CurrentUser`.
ActiveUser = Annotated[dict, Depends(get_active_user)]
