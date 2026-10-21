"""Groomio FastAPI application entry point.

All API groups (A–I) from Doc 10 register their routers here.
"""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.deps import require_active_business
from app.core.errors import VisibleErrorMiddleware
from app.core.tenancy import TenantIsolationMiddleware

app = FastAPI(
    title="Groomio API",
    version="1.0.0",
    description="Operating system for Africa's grooming businesses",
)

# Middleware order matters, and `add_middleware` puts the most recently added
# middleware on the OUTSIDE. So the registration order below reads inside-out:
#
#   TenantIsolationMiddleware → CORSMiddleware → VisibleErrorMiddleware → router
#
# `VisibleErrorMiddleware` is registered first so that it sits directly around
# the router. That is the only position where it works: an unhandled exception
# is otherwise answered by Starlette's `ServerErrorMiddleware`, which is
# *outside* the CORS layer, so the browser receives a 500 with no
# `Access-Control-Allow-Origin` header and reports a CORS policy failure
# instead of the server error that actually happened. From in here the error
# response is an ordinary response, and `CORSMiddleware` gives it the same
# headers it gives everything else. See `app/core/errors.py`.
app.add_middleware(VisibleErrorMiddleware)
app.add_middleware(
    CORSMiddleware,
    # The list, not the raw comma-separated string: a string here makes Starlette
    # treat every "origin" as untrusted, so the web app silently loses CORS.
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TenantIsolationMiddleware)

# ── API groups (Doc 10) ────────────────────────────────────────────────────
from app.auth.router import router as auth_router  # A1–A7
from app.business.router import router as business_router  # B1–B6
from app.onboarding.router import router as onboarding_router  # onboarding status
from app.customers.router import router as customers_router  # C1–C5
from app.appointments.router import router as appointments_router  # D1–D8
from app.queue.router import router as queue_router
from app.chairs.router import router as chairs_router  # E8
from app.staff.router import router as staff_router  # E1–E8
from app.sales.router import router as sales_router  # F1–F6
from app.expenses.router import router as expenses_router
from app.reports.router import router as reports_router
from app.inventory.router import router as inventory_router  # G1–G2
from app.loyalty.router import router as loyalty_router  # G3–G6
from app.subscriptions.router import router as subscriptions_router  # H1–H10
from app.messaging.router import router as messaging_router  # I1–I9
from app.ai_studio.router import router as ai_studio_router
from app.media.router import router as media_router  # Doc 11.4 signed uploads
from app.booking.router import router as public_booking_router  # CU1-CU3 portal reads
from app.booking.discovery import router as discovery_router  # CU4 "near me" search
from app.commission.router import router as commission_router  # E2–E4
from app.admin.router import router as admin_router

# ── Subscription-gated routers (T004-12) ──────────────────────────────
# Every endpoint in these routers is authenticated and business-scoped,
# so the guard is applied once at registration: an `expired` tenant
# answers 402 (payment fixes it) and a `suspended` one 403 (it does
# not). The subscription router is deliberately excluded — an expired
# business must still reach `POST /v1/subscription/pay` to recover —
# as are auth, onboarding, business, the public booking portal and
# platform admin. Routers that mix public and private endpoints
# (queue, messaging, media) apply the guard per-endpoint instead.
_GATED_ROUTERS = (
    customers_router,
    appointments_router,
    chairs_router,
    staff_router,
    commission_router,
    sales_router,
    expenses_router,
    reports_router,
    inventory_router,
    loyalty_router,
    ai_studio_router,
)

_UNGATED_ROUTERS = (
    auth_router,
    business_router,
    onboarding_router,
    queue_router,
    subscriptions_router,
    messaging_router,
    media_router,
    public_booking_router,
    discovery_router,
    admin_router,
)

for router in _UNGATED_ROUTERS:
    app.include_router(router, prefix="/v1")

for router in _GATED_ROUTERS:
    app.include_router(
        router,
        prefix="/v1",
        dependencies=[Depends(require_active_business)],
    )


@app.get("/health")
async def health():
    """Uptime pings + readiness probe (Doc 13)."""
    return {"status": "ok", "env": settings.env}


@app.get("/")
async def root():
    return {"name": "Groomio API", "docs": "/docs", "health": "/health"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host="0.0.0.0", port=int(__import__("os").environ.get("PORT", "8000"))
    )
