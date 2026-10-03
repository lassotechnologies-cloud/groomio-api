"""9.9 Messaging, Campaigns & AI Studio (MVP scope per Phase 1)."""

from sqlalchemy import Column, String, Enum, Text, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base, TimestampMixin, uuid_pk


class MessageThread(Base, TimestampMixin):
    """Booking-scoped chats (customer↔shop). Created automatically with each
    appointment/queue entry — no cold contact."""

    __tablename__ = "message_threads"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    customer_id = Column(String, nullable=False, index=True)
    appointment_id = Column(String, nullable=True, index=True)
    queue_entry_id = Column(String, nullable=True, index=True)
    status = Column(
        Enum("open", "archived", name="thread_status"), default="open", nullable=False
    )


class Message(Base, TimestampMixin):
    """Individual chat lines."""

    __tablename__ = "messages"

    id = uuid_pk()
    thread_id = Column(String, nullable=False, index=True)
    sender_user_id = Column(String, nullable=True, index=True)
    sender_side = Column(Enum("shop", "customer", name="sender_side"), nullable=False)
    body = Column(Text, nullable=False)
    attachment_url = Column(String, nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)


class Notification(Base, TimestampMixin):
    """The in-app inbox (push delivered separately)."""

    __tablename__ = "notifications"

    id = uuid_pk()
    recipient_user_id = Column(String, nullable=True, index=True)
    recipient_customer_id = Column(String, nullable=True, index=True)
    type = Column(
        Enum(
            "appointment",
            "queue",
            "subscription",
            "birthday",
            "campaign",
            "report",  # the daily digest — its own type, see migration 0006
            name="notification_type",
        ),
        nullable=False,
    )
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True)


class Campaign(Base, TimestampMixin):
    """In-app + push blasts (MVP channels only)."""

    __tablename__ = "campaigns"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    channel = Column(Enum("in_app", "push", name="campaign_channel"), nullable=False)
    segment = Column(
        Enum(
            "all",
            "inactive_30d",
            "birthday_month",
            "loyalty_tier",
            name="campaign_segment",
        ),
        nullable=False,
    )
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(
        Enum("draft", "scheduled", "sending", "sent", name="campaign_status"),
        default="draft",
        nullable=False,
    )


class CampaignRecipient(Base, TimestampMixin):
    """Who received what (read tracking)."""

    __tablename__ = "campaign_recipients"

    id = uuid_pk()
    campaign_id = Column(String, nullable=False, index=True)
    customer_id = Column(String, nullable=False, index=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
