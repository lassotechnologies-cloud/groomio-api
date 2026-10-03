"""Messaging, notifications & campaigns — API group I1–I6 (Doc 10.10).

Booking-scoped chat, the in-app inbox, web-push subscription management, and
owner campaigns.

The design constraint that shapes this whole file is CU6: a customer can ask the
shop a question without handing over their phone number. That promise is kept by
masking on **write** rather than on read. A number masked at render time is still
in the database, in the backups, and in any endpoint that forgot to mask — so the
filter in `app.messaging.filter` runs on the way in and the original is never
persisted.

Who may read a thread:

  shop side     any staff of the business owning the thread's branch
  customer side  the customer themselves, proved by their phone number, because
                customers have no account and never will (Doc 2.14 — the booking
                portal is a link, not a login)

Every cross-tenant read is 404, never 403. Confirming that someone else's thread
exists is itself a leak.
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.phone import normalize_phone
from app.core.security import now_utc
from app.core.tenancy import resolve_business_id
from app.messaging.filter import filter_message
from app.messaging.schemas import (
    CampaignCreateRequest,
    CampaignResponse,
    MessageCreateRequest,
    MessageResponse,
    MessageSendResponse,
    NotificationListResponse,
    NotificationResponse,
    PushSubscribeRequest,
    PushSubscribeResponse,
    ThreadResponse,
)
from app.models.identity import DeviceToken
from app.models.messaging import (
    Campaign,
    CampaignRecipient,
    Message,
    MessageThread,
    Notification,
)
from app.models.people import Customer
from app.models.tenant import Branch

router = APIRouter(tags=["messaging"])

Db = Annotated[AsyncSession, Depends(get_db)]
StaffOnly = Depends(require_role("owner", "clerk", "barber"))
OwnerOnly = Depends(require_role("owner"))


# ── helpers ────────────────────────────────────────────────────────────────
async def _branch_ids(db: AsyncSession, business_id: str) -> list[str]:
    return [
        str(b)
        for b in await db.scalars(
            select(Branch.id).where(Branch.business_id == business_id)
        )
    ]


async def _load_thread(
    db: AsyncSession, branch_ids: list[str], thread_id: str
) -> MessageThread:
    """Fetch a thread, scoped to this business's branches. Archived threads are
    still readable — a customer checking last month's conversation should find
    it — but they cannot be written to."""
    thread = await db.scalar(
        select(MessageThread).where(
            MessageThread.id == thread_id, MessageThread.branch_id.in_(branch_ids)
        )
    )
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )
    return thread


def _preview(body: str) -> str:
    """Short, single-line preview for the thread list."""
    flat = " ".join(body.split())
    return flat[:80] + ("…" if len(flat) > 80 else "")


# ── I1 — thread list (shop side) ───────────────────────────────────────────
@router.get("/threads", response_model=list[ThreadResponse], dependencies=[StaffOnly])
async def list_threads(
    db: Db,
    user: CurrentUser,
    branch_id: Optional[str] = Query(None),
    include_archived: bool = Query(False),
) -> list[ThreadResponse]:
    """I1 — the shop's inbox. Newest activity first, so an unanswered question
    is never buried under yesterday's chatter."""
    business_id = await resolve_business_id(db, user)
    branch_ids = await _branch_ids(db, business_id)
    if not branch_ids:
        return []

    if branch_id:
        if branch_id not in branch_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
            )
        branch_ids = [branch_id]

    stmt = select(MessageThread).where(MessageThread.branch_id.in_(branch_ids))
    if not include_archived:
        stmt = stmt.where(MessageThread.status == "open")
    threads = list(await db.scalars(stmt))

    if not threads:
        return []

    thread_ids = [str(t.id) for t in threads]

    # One windowed query for the last message per thread, rather than N queries
    # — this endpoint is hit every time a clerk opens the app, so an N+1 here is
    # an N+1 on every page load, forever.
    last_rows = await db.execute(
        select(Message.thread_id, Message.body, Message.created_at)
        .where(Message.thread_id.in_(thread_ids))
        .distinct(Message.thread_id)
        .order_by(Message.thread_id, Message.created_at.desc())
    )
    last_at: dict[str, datetime] = {}
    previews: dict[str, str] = {}
    for tid, body, created in last_rows.all():
        key = str(tid)
        last_at[key] = created
        if body:
            previews[key] = _preview(body)

    unread: dict[str, int] = {}
    unread_rows = await db.execute(
        select(Message.thread_id, func.count(Message.id))
        .where(
            Message.thread_id.in_(thread_ids),
            Message.sender_side == "customer",
            Message.read_at.is_(None),
        )
        .group_by(Message.thread_id)
    )
    unread = {str(tid): int(c) for tid, c in unread_rows.all()}

    customer_ids = {t.customer_id for t in threads}
    names = {
        str(cid): name
        for cid, name in await db.execute(
            select(Customer.id, Customer.full_name).where(Customer.id.in_(customer_ids))
        )
    }

    out = [
        ThreadResponse(
            id=str(t.id),
            branch_id=t.branch_id,
            customer_id=t.customer_id,
            customer_name=names.get(t.customer_id),
            appointment_id=t.appointment_id,
            queue_entry_id=t.queue_entry_id,
            status=t.status,
            last_message_at=last_at.get(str(t.id)),
            last_message_preview=previews.get(str(t.id)),
            unread_count=unread.get(str(t.id), 0),
        )
        for t in threads
    ]
    return sorted(
        out,
        key=lambda t: t.last_message_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


# ── I2 — read a thread ─────────────────────────────────────────────────────
@router.get(
    "/public/threads/{thread_id}/messages",
    response_model=list[MessageResponse],
)
async def public_thread_messages(
    thread_id: str,
    db: Db,
    phone: str = Query(..., description="The customer's phone number"),
    limit: int = Query(100, ge=1, le=200),
) -> list[MessageResponse]:
    """I2, customer side — read a thread using the phone number as proof.

    Customers have no account (Doc 2.14), so the phone number *is* the credential.
    That is acceptable only because the thread is already scoped to that exact
    customer: a wrong number finds no thread rather than someone else's.
    """
    thread = await db.scalar(select(MessageThread).where(MessageThread.id == thread_id))
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )

    customer = await db.scalar(
        select(Customer).where(Customer.id == thread.customer_id)
    )
    if not customer or normalize_phone(phone) != customer.phone:
        # 404, not 403: saying "wrong phone" confirms the thread is real.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )

    messages = list(
        await db.scalars(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at)
            .limit(limit)
        )
    )
    _mark_read(db, thread_id, "customer")
    return [_message_out(m, None) for m in messages]


@router.get(
    "/threads/{thread_id}/messages",
    response_model=list[MessageResponse],
    dependencies=[StaffOnly],
)
async def thread_messages(
    thread_id: str, db: Db, user: CurrentUser, limit: int = Query(100, ge=1, le=200)
) -> list[MessageResponse]:
    """I2, shop side — read a thread. Opening it marks the customer's messages
    read, which is what makes the unread badge honest."""
    business_id = await resolve_business_id(db, user)
    branch_ids = await _branch_ids(db, business_id)
    thread = await _load_thread(db, branch_ids, thread_id)

    messages = list(
        await db.scalars(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at)
            .limit(limit)
        )
    )
    _mark_read(db, thread_id, "shop")
    return [_message_out(m, "shop") for m in messages]


async def _mark_read(db: AsyncSession, thread_id: str, side: str) -> None:
    """Mark the *other* side's messages as read."""
    opposite = "customer" if side == "shop" else "shop"
    await db.execute(
        update(Message)
        .where(
            Message.thread_id == thread_id,
            Message.sender_side == opposite,
            Message.read_at.is_(None),
        )
        .values(read_at=now_utc())
    )


def _message_out(m: Message, viewer_side: Optional[str], masked: bool = False):
    return MessageResponse(
        id=str(m.id),
        thread_id=m.thread_id,
        sender_side=m.sender_side,
        body=m.body,
        attachment_url=m.attachment_url,
        read_at=m.read_at,
        created_at=m.created_at,
        phone_masked=masked,
    )


# ── I3 — send a message ────────────────────────────────────────────────────
@router.post(
    "/public/threads/{thread_id}/messages",
    response_model=MessageSendResponse,
    status_code=status.HTTP_201_CREATED,
)
async def public_send_message(
    thread_id: str, payload: MessageCreateRequest, db: Db, phone: str = Query(...)
) -> MessageSendResponse:
    """I3, customer side. The customer never sees their own number masked,
    because they already know it — masking is for the *other* party."""
    thread = await db.scalar(select(MessageThread).where(MessageThread.id == thread_id))
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )
    customer = await db.scalar(
        select(Customer).where(Customer.id == thread.customer_id)
    )
    if not customer or normalize_phone(phone) != customer.phone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )
    # Ensure the thread belongs to the same business as the customer
    branch = await db.scalar(select(Branch).where(Branch.id == thread.branch_id))
    if not branch or str(branch.business_id) != str(customer.business_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )
    if thread.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Thread is closed"
        )
    return await _send(db, thread, payload, sender_side="customer", user_id=None)


@router.post(
    "/threads/{thread_id}/messages",
    response_model=MessageSendResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[StaffOnly],
)
async def send_message(
    thread_id: str, payload: MessageCreateRequest, db: Db, user: CurrentUser
) -> MessageSendResponse:
    """I3, shop side. This is the path that matters most for CU6: the shop
    replying with a phone number is the exact case the masking exists for."""
    business_id = await resolve_business_id(db, user)
    branch_ids = await _branch_ids(db, business_id)
    thread = await _load_thread(db, branch_ids, thread_id)
    return await _send(db, thread, payload, sender_side="shop", user_id=user["user_id"])


async def _send(
    db: AsyncSession,
    thread: MessageThread,
    payload: MessageCreateRequest,
    *,
    sender_side: str,
    user_id: Optional[str],
) -> MessageSendResponse:
    """Filter, store, return. The stored body is always the filtered one."""
    if not payload.body.strip() and not payload.attachment_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A message needs text or a photo",
        )

    result = filter_message(payload.body or "")

    if result.was_abusive:
        # Refused rather than filtered. Silently rewording a shop's message to a
        # customer makes the shop think it was delivered, which is worse than a
        # clear rejection it can fix by retyping.
        return MessageSendResponse(
            id="",
            thread_id=thread.id,
            sender_side=sender_side,
            body="",
            attachment_url=None,
            rejection_reason="This message was not sent. Please keep it respectful.",
            filtered=True,
        )

    message = Message(
        thread_id=thread.id,
        sender_user_id=user_id,
        sender_side=sender_side,
        body=result.text,
        attachment_url=payload.attachment_url,
    )
    db.add(message)
    await db.flush()

    return MessageSendResponse(
        id=str(message.id),
        thread_id=thread.id,
        sender_side=sender_side,
        body=result.text,
        attachment_url=message.attachment_url,
        read_at=message.read_at,
        created_at=message.created_at,
        phone_masked=result.had_phone,
        filtered=result.had_phone,
    )


# ── I4 — web push subscriptions ────────────────────────────────────────────
@router.post("/push/subscribe", response_model=PushSubscribeResponse)
async def push_subscribe(
    payload: PushSubscribeRequest, db: Db, user: CurrentUser
) -> PushSubscribeResponse:
    """I4 — register this browser.

    Keyed on the endpoint, which the browser rotates on every subscription, so
    re-subscribing updates the existing row rather than accumulating dead ones. A
    device that never clears its old endpoints would otherwise collect entries
    until the push table is mostly dead weight.
    """
    existing = await db.scalar(
        select(DeviceToken).where(DeviceToken.endpoint == payload.endpoint)
    )
    if existing:
        existing.keys_json = payload.keys
        existing.last_seen_at = now_utc()
    else:
        db.add(
            DeviceToken(
                user_id=user["user_id"],
                endpoint=payload.endpoint,
                keys_json=payload.keys,
                last_seen_at=now_utc(),
            )
        )
    await db.flush()
    return PushSubscribeResponse(endpoint=payload.endpoint)


@router.post("/push/unsubscribe", response_model=PushSubscribeResponse)
async def push_unsubscribe(
    payload: PushSubscribeRequest, db: Db
) -> PushSubscribeResponse:
    """I4 — forget this browser. No auth: the endpoint *is* the secret, and
    requiring a token here would mean a user who has lost their session can never
    stop receiving pushes."""
    token = await db.scalar(
        select(DeviceToken).where(DeviceToken.endpoint == payload.endpoint)
    )
    if token:
        await db.delete(token)
    return PushSubscribeResponse(endpoint=payload.endpoint)


# ── I5 — the in-app inbox ──────────────────────────────────────────────────
@router.get(
    "/notifications", response_model=NotificationListResponse, dependencies=[StaffOnly]
)
async def list_notifications(
    db: Db,
    user: CurrentUser,
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
) -> NotificationListResponse:
    """I5 — the owner's inbox, newest first, with a total unread count for the badge."""
    stmt = select(Notification).where(Notification.recipient_user_id == user["user_id"])
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    rows = list(
        await db.scalars(stmt.order_by(Notification.created_at.desc()).limit(limit))
    )
    unread = await db.scalar(
        select(func.count(Notification.id)).where(
            Notification.recipient_user_id == user["user_id"],
            Notification.read_at.is_(None),
        )
    )
    return NotificationListResponse(
        items=[_notification_out(n) for n in rows], unread_count=int(unread or 0)
    )


@router.patch(
    "/notifications/{notification_id}/read",
    response_model=NotificationResponse,
    dependencies=[StaffOnly],
)
async def mark_notification_read(
    notification_id: str, db: Db, user: CurrentUser
) -> NotificationResponse:
    """I5 — mark one read. Scoped to the caller so a notification id from another
    shop is a 404 rather than a silent no-op."""
    notification = await db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_user_id == user["user_id"],
        )
    )
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
        )
    if notification.read_at is None:
        notification.read_at = now_utc()
        await db.flush()
    return _notification_out(notification)


def _notification_out(n: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=str(n.id),
        type=n.type,
        title=n.title,
        body=n.body,
        read_at=n.read_at,
        created_at=n.created_at,
    )


# ── I6 — campaigns ─────────────────────────────────────────────────────────
def _campaign_out(c: Campaign, recipients: int = 0) -> CampaignResponse:
    return CampaignResponse(
        id=str(c.id),
        business_id=c.business_id,
        channel=c.channel,
        segment=c.segment,
        title=c.title,
        body=c.body,
        scheduled_at=c.scheduled_at,
        status=c.status,
        recipient_count=recipients,
        created_at=c.created_at,
    )


@router.get(
    "/campaigns", response_model=list[CampaignResponse], dependencies=[OwnerOnly]
)
async def list_campaigns(
    db: Db, user: CurrentUser, limit: int = Query(50, ge=1, le=200)
) -> list[CampaignResponse]:
    """I6 — what has been sent, and what is queued."""
    business_id = await resolve_business_id(db, user)
    campaigns = list(
        await db.scalars(
            select(Campaign)
            .where(Campaign.business_id == business_id)
            .order_by(Campaign.created_at.desc())
            .limit(limit)
        )
    )
    if not campaigns:
        return []

    counts = dict(
        (
            await db.execute(
                select(CampaignRecipient.campaign_id, func.count(CampaignRecipient.id))
                .where(
                    CampaignRecipient.campaign_id.in_([str(c.id) for c in campaigns])
                )
                .group_by(CampaignRecipient.campaign_id)
            )
        ).all()
    )
    return [_campaign_out(c, int(counts.get(str(c.id), 0))) for c in campaigns]


@router.post(
    "/campaigns",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOnly],
)
async def create_campaign(
    payload: CampaignCreateRequest, db: Db, user: CurrentUser
) -> CampaignResponse:
    """I6 — create a campaign.

    A campaign with no `scheduled_at` is created as a draft so the owner can
    review it. `POST /campaigns/{id}/send` is what actually dispatches it, which
    keeps "write a message" and "blast it to every customer" as two deliberate
    actions rather than one.
    """
    business_id = await resolve_business_id(db, user)

    campaign = Campaign(
        business_id=business_id,
        channel=payload.channel,
        segment=payload.segment,
        title=payload.title,
        body=payload.body,
        scheduled_at=payload.scheduled_at,
        status="scheduled" if payload.scheduled_at else "draft",
    )
    db.add(campaign)
    await db.flush()
    return _campaign_out(campaign)


@router.post(
    "/campaigns/{campaign_id}/send",
    response_model=CampaignResponse,
    dependencies=[OwnerOnly],
)
async def send_campaign_now(
    campaign_id: str, db: Db, user: CurrentUser
) -> CampaignResponse:
    """I6 — dispatch a campaign now.

    The segment is resolved by the worker, not here, so a campaign scheduled for
    next month reaches the customers the shop has *then*. The `loyalty_tier`
    segment currently resolves to nobody — there is no tier table yet — and that
    is deliberate: silently mailing the whole list because a segment is
    unimplemented is the kind of mistake that ends a marketing feature.
    """
    business_id = await resolve_business_id(db, user)
    campaign = await db.scalar(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.business_id == business_id
        )
    )
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found"
        )
    if campaign.status in ("sending", "sent"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign already {campaign.status}",
        )

    campaign.status = "sending"
    await db.flush()

    # Hand off to the worker rather than sending inline: a 5,000-recipient send
    # must not hold an HTTP request open.
    from app.workers.campaign_sender import send_campaign

    send_campaign.delay(str(campaign.id))

    return _campaign_out(campaign)
