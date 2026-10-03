"""In-app notification helper (Doc 10.11).

Shared by every worker that needs to tell someone something. Notifications are
rows in the in-app inbox; web push is delivered separately and is not required
for a shop to function.

Idempotency is the caller's problem in general, so this module offers one
concrete guard: `notify_once` refuses to write a duplicate for the same recipient
and event key, which is what stops a retried task from showing an owner the same
"your trial ends tomorrow" banner five times.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging import Notification


async def notify(
    db: AsyncSession,
    *,
    type: str,
    title: str,
    body: str,
    user_id: str | None = None,
    customer_id: str | None = None,
) -> Notification:
    """Write one notification. Exactly one of user_id / customer_id must be set.

    Both null is a bug that would produce a row nobody can ever see, and both set
    would produce a row rendered twice; both are rejected here rather than left
    to surface as a phantom notification in a customer's inbox.
    """
    if bool(user_id) == bool(customer_id):
        raise ValueError("Exactly one of user_id or customer_id is required")

    notification = Notification(
        recipient_user_id=user_id,
        recipient_customer_id=customer_id,
        type=type,
        title=title[:255],
        body=body,
    )
    db.add(notification)
    return notification


async def notify_once(db: AsyncSession, **kwargs) -> Notification | None:
    """Notify, unless an identical notification already exists for this recipient.

    Deduplication is on the (type, title) pair. It is a heuristic, not a unique
    constraint: two genuinely distinct events sharing a title would be collapsed.
    That is an acceptable trade — a duplicate "your trial ends soon" is noise a
    shop owner cannot un-see, whereas a missed second reminder is redundant with
    the first one they already have.
    """
    recipient_user_id = kwargs.get("user_id")
    recipient_customer_id = kwargs.get("customer_id")
    type_ = kwargs.get("type")
    title = kwargs.get("title", "")[:255]

    if recipient_user_id:
        existing = await db.scalar(
            select(Notification).where(
                Notification.recipient_user_id == recipient_user_id,
                Notification.type == type_,
                Notification.title == title,
            )
        )
    else:
        existing = await db.scalar(
            select(Notification).where(
                Notification.recipient_customer_id == recipient_customer_id,
                Notification.type == type_,
                Notification.title == title,
            )
        )

    if existing:
        return None

    try:
        return await notify(db, **kwargs)
    except IntegrityError:
        # A concurrent task inserted the same row. Not an error worth retrying.
        await db.rollback()
        return None
