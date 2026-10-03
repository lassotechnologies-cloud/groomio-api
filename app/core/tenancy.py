"""Multi-tenant isolation — the technical heart of Groomio (Doc 11.3).

Every business-scoped query is auto-filtered by the tenant derived from the
caller's token. A clerk can never pass another shop's IDs, and a cross-tenant
access attempt returns 404 (never confirming existence — Doc 10.1).
"""

from fastapi import HTTPException
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.security import decode_token


class TenantIsolationMiddleware(BaseHTTPMiddleware):
    """Injects the authenticated user's tenant context onto each request."""

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer ") :]
            payload = decode_token(token)
            if payload:
                request.state.user_id = payload.get("sub")
                request.state.tenant_id = payload.get("business_id")
                request.state.role = payload.get("role")
        response = await call_next(request)
        return response


def tenant_filter(query, model, request: Request):
    """Apply the tenant filter to a SQLAlchemy query.

    Business-scoped models carry `business_id`; branch-scoped models carry
    `branch_id`. Public endpoints (booking portal) skip the filter.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        return query
    if hasattr(model, "business_id"):
        return query.filter(model.business_id == tenant_id)
    return query


def require_role(*allowed_roles: str):
    """FastAPI dependency factory: restrict an endpoint to the given roles.

    The UI hiding a button is never the only guard — this enforces server-side.
    """

    def _dep(request: Request):
        role = getattr(request.state, "role", None)
        if role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires one of {allowed_roles}",
            )
        return role

    return _dep


async def resolve_business_id(db, user: dict) -> str:
    """The caller's business id, from their token's user.

    Owners own the business directly; barbers and clerks reach it through their
    staff row. Centralised here because every business-scoped endpoint needs it,
    and a per-router reimplementation is how tenant leaks happen (Doc 11.3).
    """
    from app.models.people import Staff
    from app.models.tenant import Business

    biz = await db.scalar(
        select(Business).where(Business.owner_user_id == user["user_id"])
    )
    if not biz:
        staff = await db.scalar(select(Staff).where(Staff.user_id == user["user_id"]))
        if staff:
            biz = await db.scalar(
                select(Business).where(Business.id == staff.business_id)
            )
    if not biz:
        raise HTTPException(status_code=404, detail="No business yet")
    return str(biz.id)


async def resolve_staff_for_user(db, user: dict):
    """The caller's own staff row, or 404 if they are not an employee."""
    from app.models.people import Staff

    staff = await db.scalar(select(Staff).where(Staff.user_id == user["user_id"]))
    if not staff:
        raise HTTPException(status_code=404, detail="No staff profile")
    return staff
