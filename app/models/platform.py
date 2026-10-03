"""9.10 Platform — audit_logs, webhook_events."""

from sqlalchemy import Column, String, Enum, Text, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base, TimestampMixin, uuid_pk


class AuditLog(Base, TimestampMixin):
    """Who changed what (Doc 3: auditability)."""

    __tablename__ = "audit_logs"

    id = uuid_pk()
    business_id = Column(String, nullable=True, index=True)  # null = platform-level
    actor_user_id = Column(String, nullable=False, index=True)
    action = Column(String(120), nullable=False)
    entity = Column(String(80), nullable=True)
    entity_id = Column(String, nullable=True, index=True)
    before_json = Column(JSONB, nullable=True)
    after_json = Column(JSONB, nullable=True)


class WebhookEvent(Base, TimestampMixin):
    """Raw inbound payment callbacks (M-Pesa Daraja, Pesapal), stored before
    processing so nothing is lost if a worker is down."""

    __tablename__ = "webhook_events"

    id = uuid_pk()
    source = Column(Enum("daraja", "pesapal", name="webhook_source"), nullable=False)
    event_ref = Column(String, nullable=False, index=True)
    payload = Column(JSONB, nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_webhook_events_unprocessed", "source", "processed_at"),)
