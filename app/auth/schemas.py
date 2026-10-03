"""Auth request/response schemas (Doc 10.2 — APIs A1–A7)."""

from pydantic import BaseModel, Field, field_validator

from app.core.phone import normalize_phone

# The two transports for signup verification. SMS is the default for a
# Kenyan phone-first app; email is the fallback for a customer whose SIM is
# elsewhere or whose phone is unreachable. Same 6-digit code, same security
# model — only the delivery channel changes.
OTP_CHANNELS = ("sms", "email")


class RegisterRequest(BaseModel):
    """A1 — POST /auth/register."""

    full_name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=10, max_length=15)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        return normalize_phone(v)


class RegisterResponse(BaseModel):
    user_id: str
    next: str = "done"


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
    verified: bool = True
    onboarding_token: str


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
    """A4 — POST /auth/login."""

    phone_or_email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: dict


class RefreshRequest(BaseModel):
    """A5 — POST /auth/refresh."""

    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str


class PasswordResetRequest(BaseModel):
    """A7 — POST /auth/password/reset-request."""

    email: str


class MessageResponse(BaseModel):
    message: str
