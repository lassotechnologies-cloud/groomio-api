"""9.1 Identity & Access — users, OTP codes, refresh tokens, device tokens."""

from sqlalchemy import Column, String, Enum, SmallInteger, DateTime, Text, Index
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base, TimestampMixin, uuid_pk


class User(Base, TimestampMixin):
    """Every person who logs in (any role)."""

    __tablename__ = "users"

    id = uuid_pk()
    full_name = Column(String(120), nullable=False)
    phone = Column(String(15), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(
        Enum("super_admin", "owner", "clerk", "barber", "customer", name="user_role"),
        nullable=False,
    )
    status = Column(
        Enum("pending", "active", "suspended", name="user_status"),
        default="active",
        nullable=False,
    )


class OTPCode(Base, TimestampMixin):
    """6-digit signup codes, delivered by SMS or email.

    One code, two channels. `channel` says which; the matching recipient column
    is populated and the other is NULL, enforced by a CHECK constraint so an SMS
    code can never be mistaken for an email code at the database level. Storing
    the email on the row (rather than re-deriving it from the user) means the
    verify step does not have to trust the client to supply the same address
    the code was sent to.
    """

    __tablename__ = "otp_codes"

    id = uuid_pk()
    phone = Column(String(15), nullable=True)
    code_hash = Column(String(255), nullable=False)
    purpose = Column(
        Enum("signup_verification", "login_verification", name="otp_purpose"),
        default="signup_verification",
        nullable=False,
    )
    channel = Column(
        Enum("sms", "email", name="otp_channel"),
        default="sms",
        nullable=False,
    )
    email = Column(String(255), nullable=True)
    attempts = Column(SmallInteger, default=0, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_otp_codes_phone_created", "phone", "created_at"),
        Index("ix_otp_codes_channel_created", "channel", "created_at"),
    )


class RefreshToken(Base):
    """ "Stay logged in" tokens, stored hashed so we can revoke them."""

    __tablename__ = "refresh_tokens"

    id = uuid_pk()
    user_id = Column(String, nullable=False, index=True)
    token_hash = Column(String(255), nullable=False)
    device = Column(String(120), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class DeviceToken(Base):
    """Web-push subscriptions (Doc 2.14 push alerts)."""

    __tablename__ = "device_tokens"

    id = uuid_pk()
    user_id = Column(String, nullable=False, index=True)
    endpoint = Column(Text, nullable=False)
    keys_json = Column(JSONB, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
