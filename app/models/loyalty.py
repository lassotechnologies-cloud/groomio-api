"""9.7 Loyalty & Memberships — loyalty_transactions, membership_plans, customer_memberships, referrals."""

from sqlalchemy import Column, String, Enum, Integer, SmallInteger, Date, Index

from app.models.base import Base, TimestampMixin, uuid_pk


class LoyaltyTransaction(Base, TimestampMixin):
    """Every points movement (the balance is just the sum)."""

    __tablename__ = "loyalty_transactions"

    id = uuid_pk()
    customer_id = Column(String, nullable=False, index=True)
    points = Column(Integer, nullable=False)
    reason = Column(
        Enum("visit", "referral", "birthday", "redemption", name="loyalty_reason"),
        nullable=False,
    )
    sale_id = Column(String, nullable=True, index=True)


class MembershipPlan(Base, TimestampMixin):
    """e.g. "10 cuts for the price of 8"."""

    __tablename__ = "membership_plans"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    price_kes = Column(Integer, nullable=False)
    sessions_included = Column(SmallInteger, nullable=False)
    validity_days = Column(SmallInteger, nullable=False)


class CustomerMembership(Base, TimestampMixin):
    """A customer's active membership."""

    __tablename__ = "customer_memberships"

    id = uuid_pk()
    customer_id = Column(String, nullable=False, index=True)
    plan_id = Column(String, nullable=False, index=True)
    started_at = Column(Date, nullable=False)
    expires_at = Column(Date, nullable=False)
    sessions_used = Column(SmallInteger, default=0, nullable=False)


class Referral(Base, TimestampMixin):
    """Invite-a-friend rewards."""

    __tablename__ = "referrals"

    id = uuid_pk()
    referrer_customer_id = Column(String, nullable=False, index=True)
    referred_phone = Column(String(15), nullable=False)
    status = Column(
        Enum("pending", "converted", "rewarded", name="referral_status"),
        default="pending",
        nullable=False,
    )
    points_awarded = Column(Integer, default=0, nullable=False)
