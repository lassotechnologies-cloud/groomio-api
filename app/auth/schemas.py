"""Auth request/response schemas (Doc 10.2 — APIs A1–A7)."""

from pydantic import BaseModel, Field, field_validator

from app.core.phone import normalize_phone

# The two transports for signup verification. SMS is the default for a
# Kenyan phone-first app; email is the fallback for a customer whose SIM is
# elsewhere or whose phone is unreachable. Same 6-digit code, same security
# model — only the delivery channel changes.
OTP_CHANNELS = ("sms", "email")

# E.164 allows at most 15 digits with no leading zero. Anything shorter than 9
# is not a real subscriber number on any network, and `normalize_phone` happily
# turns arbitrary junk into a bare "+", which would then be stored as if it were
# a contactable address.
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15


def normalize_email(value: str) -> str:
    """Canonical email form for storage and lookup.

    Email domains are case-insensitive (RFC 5321 §2.4) and the local part is in
    practice treated the same way. Storing the raw input meant `A@Shop.com` and
    `a@shop.com` were two different rows that both belonged to one person, and
    the duplicate check missed the second one. Normalising at the edge keeps a
    single canonical row per address and makes `WHERE email = ?` reliable —
    which is the same reasoning as `normalize_phone`.
    """
    return value.strip().lower()


def _normalize_and_validate_phone(value: str) -> str:
    """Normalise a phone number *and* refuse values that are not numbers.

    Normalisation and validation are deliberately separate concerns.
    `normalize_phone` is lenient on purpose so that a lookup for a mistyped
    number still resolves the row the customer meant. But accepting that same
    leniency on *registration* would store "+" as somebody's contact number, so
    the write path checks the normalised result actually looks like E.164.
    """
    normalized = normalize_phone(value)
    digits = normalized[1:] if normalized.startswith("+") else normalized

    if not digits.isdigit():
        raise ValueError("Phone must contain digits")
    if not _MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS:
        raise ValueError(
            f"Phone must have {_MIN_PHONE_DIGITS}-{_MAX_PHONE_DIGITS} digits"
        )
    if digits.startswith("0"):
        raise ValueError("Phone must not start with 0 after the country code")
    return normalized


class RegisterRequest(BaseModel):
    """A1 — POST /auth/register."""

    full_name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=10, max_length=15)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        return _normalize_and_validate_phone(v)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        normalized = normalize_email(v)
        if not normalized or "@" not in normalized:
            raise ValueError("A valid email address is required")
        return normalized


class RegisterResponse(BaseModel):
    """A1 — `POST /auth/register`.

    Registration creates a **pending** account and deliberately returns no
    access token and no refresh token. The account holder must prove they
    control the phone (or email) before they get a session; without that gate
    anyone can register someone else's number and receive a working session for
    it.

    `onboarding_token` is the restricted alternative: it carries **no `role` and
    no `business_id`**, so it cannot satisfy `require_role` and therefore cannot
    open a single business endpoint. It exists only to identify the pending user
    during the verification step, and `get_current_user` rejects it outright.
    """

    user_id: str
    next: str = "otp_verify"
    requires_verification: bool = True
    onboarding_token: str
    user: dict


class OTPRequest(BaseModel):
    """A2 — POST /auth/otp/request.

    `channel` selects the transport. SMS is the default and always available;
    email requires the deployment to have SMTP configured, which the endpoint
    checks rather than silently queueing a code that will never arrive.
    """

    phone: str
    channel: str = "sms"

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, v: str) -> str:
        if v not in OTP_CHANNELS:
            raise ValueError(f"channel must be one of {OTP_CHANNELS}")
        return v


class OTPRequestResponse(BaseModel):
    expires_in: int = 600  # 10 minutes
    resend_after: int = 60  # seconds before a resend is allowed


class OTPVerifyRequest(BaseModel):
    """A3 — POST /auth/otp/verify.

    The recipient is identified by `phone` for SMS and `email` for email. The
    endpoint looks the code up by the matching column, so the two channels
    cannot cross-validate — an SMS code never proves an email address.
    """

    channel: str = "sms"
    phone: str | None = None
    email: str | None = None
    code: str = Field(..., min_length=6, max_length=6)

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, v: str) -> str:
        if v not in OTP_CHANNELS:
            raise ValueError(f"channel must be one of {OTP_CHANNELS}")
        return v

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_phone(v)

    @field_validator("code")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("OTP must be digits")
        return v


class OTPVerifyResponse(BaseModel):
    """A3 — `POST /auth/otp/verify`.

    `onboarding_token` is retained because the web onboarding screens still read
    it by name. It now carries a real `type: "access"` token instead of one
    stamped `type: "onboarding"`, which no dependency would accept.

    The token trio mirrors `LoginResponse` so a verified user refreshes and
    reads their profile exactly like a password-logged-in user, rather than
    getting a token that works for nothing.
    """

    verified: bool = True
    onboarding_token: str
    access_token: str
    refresh_token: str
    user: dict


class OtpLoginRequest(BaseModel):
    """OTP login — request a code for an existing user."""

    phone: str
    channel: str = "sms"

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, v: str) -> str:
        if v not in OTP_CHANNELS:
            raise ValueError(f"channel must be one of {OTP_CHANNELS}")
        return v


class OtpLoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: dict


class LoginRequest(BaseModel):
    """A4 — POST /auth/login.

    Registration stores the phone in canonical E.164 (`+254712345678`) but this
    field was previously compared verbatim, so a customer who signed up as
    `0712345678` could never sign in as `0712345678` — only by typing the
    normalised form, which nobody does. Phone input is normalised here so it
    resolves to the same stored row; anything containing `@` is treated as an
    email and left alone, because running it through the phone normaliser would
    strip it down to a couple of stray digits.
    """

    phone_or_email: str
    password: str

    @field_validator("phone_or_email")
    @classmethod
    def _normalize_identifier(cls, v: str) -> str:
        v = v.strip()
        if "@" in v:
            # Same canonical form registration stored, so a customer can sign in
            # with the casing they typed at signup.
            return normalize_email(v)
        return normalize_phone(v)


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: dict


class RefreshRequest(BaseModel):
    """A5 — POST /auth/refresh."""

    refresh_token: str


class RefreshResponse(BaseModel):
    """A5 — `POST /auth/refresh`.

    `refresh_token` was added because rotation was silently lossy: the endpoint
    revoked the presented token and persisted a fresh one, but only returned
    the access token. The client kept the revoked copy and was locked out on
    the next refresh, so "stay logged in" worked exactly once.
    """

    access_token: str
    refresh_token: str | None = None


class PasswordResetRequest(BaseModel):
    """A7 — POST /auth/password/reset-request."""

    email: str


class MessageResponse(BaseModel):
    message: str
