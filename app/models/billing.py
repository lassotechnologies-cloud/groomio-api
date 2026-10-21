"""9.8 Subscriptions & Payments — plans, subscriptions, payments."""

from sqlalchemy import (
    Column,
    String,
    Enum,
    Integer,
    SmallInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

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
    """One *live* subscription per business; the billing state machine.

    **Why this index is partial.** The class docstring used to read "one per
    business", which read like a promise that a business never has a second row.
    It does not, and cannot:

      * `_current_subscription` (subscriptions/router.py) is documented as "the
        live subscription: trial, active, or grace. **Expired rows are history**",
        and every read of a live subscription filters on exactly that status set.
      * `_apply_payment` — the single writer of subscription state — extends
        `current_period_end` **on the existing row**. A billing renewal never
        inserts a second row, so renewals place no demand on this constraint.
      * `workers/subscription_state.py` moves rows trial → grace → expired in
        place. Again, no insert.

    A blanket unique index on `business_id` would therefore be *safe today* but
    would silently decide a product question nobody has asked: whether a lapsed
    business may ever buy its way back with a fresh subscription row. The
    read paths already anticipate that history, so forbidding it would be
    encoding an unstated policy as a database constraint.

    Restricting the index to the live statuses instead states exactly the
    invariant that is actually true and already relied on — **a business cannot
    hold two live subscriptions at once** — while leaving expired history rows
    free to accumulate. That is the defect T-003 actually observed (a duplicated
    trial), and it is what this index prevents. A partial index is therefore
    used here for a concrete, documented reason, unlike the plain unique index
    on `businesses.owner_user_id`.
    """

    __tablename__ = "subscriptions"

    __table_args__ = (
        Index(
            "uq_subscriptions_live_business",
            "business_id",
            unique=True,
            # Enum-to-literal comparisons are IMMUTABLE, so this predicate is
            # legal in a partial index. Mirrors the `status.in_(...)` filter used
            # by every live-subscription read in the codebase.
            postgresql_where=text("status IN ('trial', 'active', 'grace')"),
        ),
    )

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
    subscription_id = Column(
        UUID,
        ForeignKey("subscriptions.id"),
        nullable=False,
        index=True,
    )
    amount_kes = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="kes")
    method = Column(
        Enum(
            "mpesa", "airtel_money", "bank_transfer", "pesapal", name="payment_method_sub"
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

    __table_args__ = (
        Index("ix_payments_biz_paid", "business_id", "paid_at"),
        Index(
            "uq_payments_provider_ref",
            "provider",
            "provider_ref",
            unique=True,
            postgresql_where=text("provider_ref IS NOT NULL"),
        ),
    )
