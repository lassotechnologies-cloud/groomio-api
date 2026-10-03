"""9.8 Subscriptions & Payments — plans, subscriptions, payments."""

from sqlalchemy import (
    Column,
    String,
    Enum,
    Integer,
    SmallInteger,
    Boolean,
    DateTime,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base, TimestampMixin, uuid_pk


class Plan(Base, TimestampMixin):
    """The 5 plans, seeded once from the approved pricing (Doc 1 pricing check)."""

    __tablename__ = "plans"

    id = uuid_pk()
    code = Column(
        Enum("daily", "biweekly", "monthly", "half_yearly", "yearly", name="plan_code"),
        nullable=False,
        unique=True,
    )
    price_kes = Column(Integer, nullable=False)
    interval_days = Column(SmallInteger, nullable=False)
    discount_pct = Column(SmallInteger, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)


class Subscription(Base, TimestampMixin):
    """One per business; the billing state machine."""

    __tablename__ = "subscriptions"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    plan_id = Column(String, nullable=False, index=True)
    status = Column(
        Enum("trial", "active", "grace", "expired", name="subscription_status"),
        default="trial",
        nullable=False,
    )
    current_period_start = Column(DateTime(timezone=True), nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=False)


class Payment(Base, TimestampMixin):
    """Every payment attempt (successful or not)."""

    __tablename__ = "payments"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    subscription_id = Column(String, nullable=False, index=True)
    amount_kes = Column(Integer, nullable=False)
    method = Column(
        Enum(
            "mpesa", "airtel_money", "bank_transfer", "pesapal", name="payment_method"
        ),
        nullable=False,
    )
    provider = Column(
        Enum("daraja", "pesapal", "manual", name="payment_provider"),
        nullable=False,
    )
    provider_ref = Column(String, nullable=True)
    status = Column(
        Enum("pending", "success", "failed", "timeout", name="payment_status"),
        default="pending",
        nullable=False,
    )
    paid_at = Column(DateTime(timezone=True), nullable=True)
    raw_callback = Column(JSONB, nullable=True)

    __table_args__ = (Index("ix_payments_biz_paid", "business_id", "paid_at"),)
