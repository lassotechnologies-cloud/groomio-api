"""Queue routes — API group D5–D8 (Doc 10.5).

The live walk-in queue is the feature that makes a barber shop feel calm instead
of chaotic: a clerk takes a number, works the list in order, and each waiting
customer can check their own position by phone (CU3) without an account.

Wait estimates are intentionally simple and explainable — the number of free
barbers, not a learned model. A shop owner must be able to trust the number they
show a customer.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.schemas import (
    PublicQueuePositionResponse,
    QueueEntryCreateRequest,
    QueueEntryResponse,
)
from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.security import now_utc
from app.models.operations import Chair, QueueEntry
from app.models.people import Customer, Staff
from app.models.tenant import Branch, Business

router = APIRouter(tags=["queue"])

Db = Annotated[AsyncSession, Depends(get_db)]
QueueRoles = Depends(require_role("owner", "clerk", "barber"))
ClerkRoles = Depends(require_role("owner", "clerk"))

# Fallback service length when a walk-in gives no services (a quick trim).
DEFAULT_SERVICE_MIN = 30
# Chairs not marked offline are the real capacity.
MIN_EST_WAIT_MIN = 5


# ── helpers ──────────────────────────────────────────────────────────────────
async def _branch_and_business(
    db: AsyncSession, user: dict, branch_id: str
) -> tuple[Branch, Business]:
    biz = await db.scalar(
        select(Business).where(Business.owner_user_id == user["user_id"])
    )
    if not biz:
        staff = await db.scalar(select(Staff).where(Staff.user_id == user["user_id"]))
        biz = (
            await db.scalar(select(Business).where(Business.id == staff.business_id))
            if staff
            else None
        )
    if not biz:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No business yet"
        )

    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == str(biz.id))
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )
    return branch, biz


async def _next_queue_number(db: AsyncSession, branch_id: str) -> int:
    """Numbers run 1, 2, 3… within a day, restarting when the queue empties."""
    last = await db.scalar(
        select(func.max(QueueEntry.queue_number)).where(
            QueueEntry.branch_id == branch_id,
            func.date(QueueEntry.joined_at) == func.date(now_utc()),
        )
    )
    return int(last or 0) + 1


async def _estimate_wait_min(db: AsyncSession, branch_id: str) -> int:
    """Waiting headcount ÷ serving capacity, floored so it never reads as 0."""
    waiting = await db.scalar(
        select(func.count())
        .select_from(QueueEntry)
        .where(QueueEntry.branch_id == branch_id, QueueEntry.status == "waiting")
    )
    if not waiting:
        return 0

    barbers = await db.scalar(
        select(func.count())
        .select_from(Staff)
        .where(Staff.branch_id == branch_id, Staff.status == "active")
    )
    # A branch's usable capacity is min(staff, chairs that are not offline).
    usable_chairs = await db.scalar(
        select(func.count())
        .select_from(Chair)
        .where(Chair.branch_id == branch_id, Chair.status != "offline")
    )
    capacity = min(int(barbers or 0), int(usable_chairs or 0)) or 1

    return max(MIN_EST_WAIT_MIN, round(int(waiting) * DEFAULT_SERVICE_MIN / capacity))


async def _refresh_estimates(db: AsyncSession, branch_id: str) -> None:
    """Rewrite est_wait_min for everyone still waiting after a state change."""
    estimate = await _estimate_wait_min(db, branch_id)
    await db.execute(
        QueueEntry.__table__.update()
        .where(
            QueueEntry.branch_id == branch_id,
            QueueEntry.status.in_(("waiting", "serving")),
        )
        .values(est_wait_min=estimate)
    )


async def _load_entry(db: AsyncSession, business_id: str, entry_id: str) -> QueueEntry:
    """Scope by branch's business so a queue id from another shop is a 404."""
    entry = await db.scalar(
        select(QueueEntry)
        .join(Branch, Branch.id == QueueEntry.branch_id)
        .where(QueueEntry.id == entry_id, Branch.business_id == business_id)
    )
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return entry


async def _serialize(
    db: AsyncSession, entries: list[QueueEntry]
) -> list[QueueEntryResponse]:
    if not entries:
        return []

    names: dict[str, str] = {}
    cust_ids = {e.customer_id for e in entries}
    for c in await db.scalars(select(Customer).where(Customer.id.in_(cust_ids))):
        names[c.id] = c.full_name

    return [
        QueueEntryResponse(
            id=str(e.id),
            branch_id=e.branch_id,
            customer_id=e.customer_id,
            customer_name=names.get(e.customer_id),
            staff_id=e.staff_id,
            chair_id=e.chair_id,
            queue_number=e.queue_number,
            status=e.status,
            joined_at=e.joined_at,
            served_at=e.served_at,
            est_wait_min=e.est_wait_min,
        )
        for e in entries
    ]


# ── D5 — live queue ──────────────────────────────────────────────────────────
@router.get(
    "/branches/{branch_id}/queue",
    response_model=list[QueueEntryResponse],
    dependencies=[QueueRoles],
)
async def get_queue(
    branch_id: str, db: Db, user: CurrentUser
) -> list[QueueEntryResponse]:
    """CL2 — who is waiting, who is being served, and the current estimate."""
    branch, _ = await _branch_and_business(db, user, branch_id)
    rows = list(
        await db.scalars(
            select(QueueEntry)
            .where(
                QueueEntry.branch_id == branch_id,
                QueueEntry.status.in_(("waiting", "serving")),
            )
            .order_by(QueueEntry.queue_number)
        )
    )
    return await _serialize(db, rows)


# ── D6 — add a walk-in ───────────────────────────────────────────────────────
@router.post(
    "/branches/{branch_id}/queue",
    response_model=QueueEntryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[ClerkRoles],
)
async def join_queue(
    branch_id: str, payload: QueueEntryCreateRequest, db: Db, user: CurrentUser
) -> QueueEntryResponse:
    """CL1 — take the next number for someone who walked in."""
    branch, biz = await _branch_and_business(db, user, branch_id)

    customer = await db.scalar(
        select(Customer).where(
            Customer.id == payload.customer_id, Customer.business_id == str(biz.id)
        )
    )
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )

    # One live entry per customer — joining twice would double-count the wait.
    existing = await db.scalar(
        select(QueueEntry).where(
            QueueEntry.branch_id == branch_id,
            QueueEntry.customer_id == customer.id,
            QueueEntry.status.in_(("waiting", "serving")),
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already in the queue as #{existing.queue_number}",
        )

    entry = QueueEntry(
        branch_id=branch_id,
        customer_id=customer.id,
        queue_number=await _next_queue_number(db, branch_id),
        status="waiting",
        joined_at=now_utc(),
    )
    db.add(entry)
    await db.flush()

    await _refresh_estimates(db, branch_id)
    await db.refresh(entry)
    return (await _serialize(db, [entry]))[0]


# ── D7 — call next / finish ──────────────────────────────────────────────────
@router.post(
    "/queue-entries/{entry_id}/serve",
    response_model=QueueEntryResponse,
    dependencies=[ClerkRoles],
)
async def serve_next(entry_id: str, db: Db, user: CurrentUser) -> QueueEntryResponse:
    """Call someone forward: waiting -> serving, and free up a chair."""
    entry = await _load_entry(db, await _business_id_for(db, user), entry_id)

    if entry.status != "waiting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Entry is {entry.status}"
        )

    entry.status = "serving"
    entry.served_at = now_utc()
    entry.est_wait_min = 0

    # Occupy the first free chair so the shop view stays truthful.
    chair = await db.scalar(
        select(Chair)
        .where(Chair.branch_id == entry.branch_id, Chair.status == "free")
        .order_by(Chair.chair_number)
    )
    if chair:
        chair.status = "occupied"
        entry.chair_id = chair.id

    await _refresh_estimates(db, entry.branch_id)
    return (await _serialize(db, [entry]))[0]


@router.post(
    "/queue-entries/{entry_id}/complete",
    response_model=QueueEntryResponse,
    dependencies=[ClerkRoles],
)
async def complete_entry(
    entry_id: str, db: Db, user: CurrentUser
) -> QueueEntryResponse:
    """Finish a service: release the chair, promote the next person."""
    entry = await _load_entry(db, await _business_id_for(db, user), entry_id)

    if entry.status not in ("serving", "waiting"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Entry is {entry.status}"
        )

    entry.status = "served"
    entry.served_at = entry.served_at or now_utc()

    if entry.chair_id:
        chair = await db.scalar(
            select(Chair).where(
                Chair.id == entry.chair_id, Chair.branch_id == entry.branch_id
            )
        )
        if chair:
            chair.status = "free"

    await _refresh_estimates(db, entry.branch_id)
    return (await _serialize(db, [entry]))[0]


async def _business_id_for(db: AsyncSession, user: dict) -> str:
    """Resolve the caller's business id (owner lookup, else via their staff row)."""
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No business yet"
        )
    return str(biz.id)


# ── D8 — public queue position (no auth) ─────────────────────────────────────
@router.get(
    "/public/{branch_slug}/queue/{customer_ref}",
    response_model=PublicQueuePositionResponse,
)
async def public_queue_position(
    branch_slug: str, customer_ref: str, db: Db
) -> PublicQueuePositionResponse:
    """CU3 — a waiting customer checks their own position.

    `customer_ref` is the customer's phone number. It is deliberately the only
    lookup key, so a customer never needs an account or a leaked link.
    """
    branch = await db.scalar(select(Branch).where(Branch.slug == branch_slug))
    if not branch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    digits = "".join(ch for ch in customer_ref if ch.isdigit())
    if not digits:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="phone required"
        )
    normalized = f"+{digits}" if not digits.startswith("254") else f"+{digits}"

    entry = await db.scalar(
        select(QueueEntry)
        .join(Customer, Customer.id == QueueEntry.customer_id)
        .where(
            QueueEntry.branch_id == branch.id,
            Customer.phone == normalized,
            QueueEntry.status.in_(("waiting", "serving")),
        )
        .order_by(QueueEntry.queue_number.desc())
        .limit(1)
    )
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not in the queue"
        )

    # How many still ahead of this number.
    ahead = await db.scalar(
        select(func.count())
        .select_from(QueueEntry)
        .where(
            QueueEntry.branch_id == branch.id,
            QueueEntry.status == "waiting",
            QueueEntry.queue_number < entry.queue_number,
        )
    )

    return PublicQueuePositionResponse(
        queue_number=entry.queue_number,
        status=entry.status,
        people_ahead=int(ahead or 0),
        est_wait_min=entry.est_wait_min or 0,
        joined_at=entry.joined_at or datetime.now(timezone.utc),
    )
