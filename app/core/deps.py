"""Shared FastAPI dependencies — DB session + authenticated user."""

from typing import Annotated, Optional

from fastapi import Depends, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.core.tenancy import require_role as role_dependency


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)):
    """Return the authenticated user's id + role from the Bearer token."""
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


# Role-based access — re-exported so routers can do Depends(require_role(...))
def require_role(*roles: str):
    return role_dependency(*roles)


# Convenience type aliases
DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[dict, Depends(get_current_user)]
