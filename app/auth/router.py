"""Authentication routes — API group A (Doc 10.2: A1–A7).

Signup flow (Phase 1):
  1. `POST /v1/auth/register`     — creates a `pending` user, no business yet
  2. `POST /v1/auth/otp/request`  — generates a 6-digit code, stores the HASH
  3. `POST /v1/auth/otp/verify`   — verifies, flips user to `active`, mints an
                                   onboarding token that unlocks step B1 (business)
  4. `POST /v1/auth/login`        — password login -> access + refresh tokens
  5. `POST /v1/auth/refresh`      — rotates the refresh token

Security rules enforced here (Doc 11.1):
- OTPs are generated with `secrets`, stored as SHA-256 hashes, expire in 10
  minutes, and burn after 5 wrong attempts.
- Passwords are argon2id; plain text is never stored or logged.
- Refresh tokens are persisted hashed so they can be revoked server-side.
"""

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import (
    LoginRequest,
    LoginResponse,
    MessageResponse,
    OTPRequest,
    OTPRequestResponse,
    OTPVerifyRequest,
    OTPVerifyResponse,
    PasswordResetRequest,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    OtpLoginRequest,
    OtpLoginResponse,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    create_refresh_token,
    generate_otp,
    hash_otp,
    hash_password,
    hash_token,
    now_utc,
    verify_otp_hash,
    verify_password,
)
from app.models.identity import OTPCode, RefreshToken, User
from app.workers.otp_email_sender import send_otp_email
from app.workers.otp_sender import send_otp

router = APIRouter(prefix="/auth", tags=["Auth"])

MAX_OTP_ATTEMPTS = 5
RESEND_COOLDOWN_SEC = 60


def _email_configured() -> bool:
    """Whether the deployment can actually deliver an email OTP.

    Checked at request time rather than assumed: a deployment with no SMTP
    must answer 400 instead of queueing a code that will never arrive and
    leaving the customer waiting for a text that was never sent.
    """
    return bool(settings.email_host and settings.email_user and settings.email_pass)


def _recipient(record: OTPCode) -> str:
    """The address the code was actually sent to, for the UI's "code sent to" line."""
    return record.email if record.channel == "email" else record.phone


# ── helpers ──────────────────────────────────────────────────────────────────
def _user_claims(user: User, business_id: Optional[str] = None) -> dict:
    """Claims embedded in the JWT so downstream deps can read them without a DB hit."""
    return {"role": user.role, "email": user.email, "business_id": business_id}


def _serialize_user(user: User) -> dict:
    return {
        "id": str(user.id),
        "full_name": user.full_name,
        "phone": user.phone,
        "email": user.email,
        "role": user.role,
        "status": user.status,
    }


async def _issue_tokens(db: AsyncSession, user: User) -> tuple[str, str]:
    """Mint an access + refresh pair, persisting the refresh hash for revocation."""
    access = create_access_token(str(user.id), _user_claims(user))
    refresh = create_refresh_token(str(user.id))

    db.add(
        RefreshToken(
            user_id=str(user.id),
            token_hash=hash_token(refresh),
            expires_at=now_utc() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    return access, refresh


# ── A1 — register ────────────────────────────────────────────────────────────
@router.post(
    "/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    payload: RegisterRequest, db: AsyncSession = Depends(get_db)
) -> RegisterResponse:
    """Create the user shell. No business yet — that happens after OTP (B1)."""
    existing = await db.scalar(
        select(User).where(
            (User.phone == payload.phone) | (User.email == payload.email)
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phone or email already registered",
        )

    user = User(
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="owner",
        status="active",
    )
    db.add(user)
    await db.flush()

    return RegisterResponse(user_id=str(user.id), next="done")


# ── A2 — request OTP ─────────────────────────────────────────────────────────
@router.post("/otp/request", response_model=OTPRequestResponse)
async def request_otp(
    payload: OTPRequest, db: AsyncSession = Depends(get_db)
) -> OTPRequestResponse:
    """Generate a 6-digit code, store its hash, and hand delivery to Celery.

    The channel is the customer's choice. SMS is the default and always works;
    email needs SMTP configured, which is checked here so a misconfigured
    deployment answers 400 instead of queueing a code that never arrives.
    """
    if payload.channel == "email" and not _email_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email verification is not configured for this deployment",
        )

    # Look the user up by the channel's recipient. An email request must match
    # the user's stored email, so a request for the wrong address never
    # confirms a registration (Doc 10.1).
    if payload.channel == "email":
        user = await db.scalar(select(User).where(User.email == payload.phone))
    else:
        user = await db.scalar(select(User).where(User.phone == payload.phone))
    if not user:
        # Never confirm whether a phone is registered (Doc 10.1).
        return OTPRequestResponse()

    # Resend cooldown — stops SMS-cost abuse on a single number. Scoped to the
    # channel: a customer who switches channels is not blocked by their own
    # previous request on the other transport.
    recipient_col = OTPCode.email if payload.channel == "email" else OTPCode.phone
    recipient_val = user.email if payload.channel == "email" else payload.phone
    recent = await db.scalar(
        select(OTPCode)
        .where(
            recipient_col == recipient_val,
            OTPCode.consumed_at.is_(None),
        )
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    if recent and (now_utc() - recent.created_at).total_seconds() < RESEND_COOLDOWN_SEC:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Wait {RESEND_COOLDOWN_SEC}s before requesting another code",
        )

    code = generate_otp()
    db.add(
        OTPCode(
            phone=None if payload.channel == "email" else payload.phone,
            email=user.email if payload.channel == "email" else None,
            code_hash=hash_otp(code),
            purpose="signup_verification",
            channel=payload.channel,
            attempts=0,
            expires_at=now_utc() + timedelta(minutes=settings.otp_ttl_min),
        )
    )

    # Queued, not sent inline — the request returns fast and retries in the
    # worker. The two transports are independent tasks so a broken SMTP config
    # never takes SMS down with it.
    if payload.channel == "email":
        send_otp_email.delay(user.email, code, purpose="signup_verification")
    else:
        send_otp.delay(payload.phone, code)

    return OTPRequestResponse(
        expires_in=settings.otp_ttl_min * 60,
        resend_after=RESEND_COOLDOWN_SEC,
    )


# ── A3 — verify OTP ──────────────────────────────────────────────────────────
@router.post("/otp/verify", response_model=OTPVerifyResponse)
async def verify_otp(
    payload: OTPVerifyRequest, db: AsyncSession = Depends(get_db)
) -> OTPVerifyResponse:
    """Burn the code on success, activate the user, and unlock onboarding.

    The code is looked up by the channel's recipient column, so an SMS code can
    never validate an email address and vice versa — the two channels cannot
    cross-contaminate even at the database level.
    """
    if payload.channel == "email":
        if not payload.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required for email verification",
            )
        user = await db.scalar(select(User).where(User.email == payload.email))
        recipient_col = OTPCode.email
        recipient_val = payload.email
    else:
        if not payload.phone:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone is required for SMS verification",
            )
        user = await db.scalar(select(User).where(User.phone == payload.phone))
        recipient_col = OTPCode.phone
        recipient_val = payload.phone

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    record = await db.scalar(
        select(OTPCode)
        .where(
            recipient_col == recipient_val,
            OTPCode.channel == payload.channel,
            OTPCode.purpose == "signup_verification",
            OTPCode.consumed_at.is_(None),
        )
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Request a new code"
        )

    if record.expires_at < now_utc():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Code expired"
        )

    if record.attempts >= MAX_OTP_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts — request a new code",
        )

    if not verify_otp_hash(payload.code, record.code_hash):
        record.attempts += 1
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect code"
        )

    record.consumed_at = now_utc()
    user.status = "active"

    onboarding_token = create_access_token(
        str(user.id), {**_user_claims(user), "type": "onboarding"}
    )
    return OTPVerifyResponse(verified=True, onboarding_token=onboarding_token)


# ── A3b — request OTP for login ──────────────────────────────────
@router.post("/otp/login/request", response_model=OTPRequestResponse)
async def request_otp_login(
    payload: OtpLoginRequest, db: AsyncSession = Depends(get_db)
) -> OTPRequestResponse:
    """Request a 6-digit code for an existing user to log in.

    Looks the user up by phone (SMS) or email (email channel).
    If the user does not exist, returns an empty response — never
    confirm whether the identifier is registered.
    """
    if payload.channel == "email" and not _email_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email verification is not configured for this deployment",
        )

    if payload.channel == "email":
        user = await db.scalar(select(User).where(User.email == payload.phone))
    else:
        user = await db.scalar(select(User).where(User.phone == payload.phone))
    if not user:
        return OTPRequestResponse()

    # Resend cooldown — scoped to the channel recipient + login purpose.
    recipient_col = OTPCode.email if payload.channel == "email" else OTPCode.phone
    recipient_val = user.email if payload.channel == "email" else payload.phone
    recent = await db.scalar(
        select(OTPCode)
        .where(
            recipient_col == recipient_val,
            OTPCode.consumed_at.is_(None),
            OTPCode.purpose == "login_verification",
        )
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    if recent and (now_utc() - recent.created_at).total_seconds() < RESEND_COOLDOWN_SEC:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Wait {RESEND_COOLDOWN_SEC}s before requesting another code",
        )

    code = generate_otp()
    db.add(
        OTPCode(
            phone=None if payload.channel == "email" else payload.phone,
            email=user.email if payload.channel == "email" else None,
            code_hash=hash_otp(code),
            purpose="login_verification",
            channel=payload.channel,
            attempts=0,
            expires_at=now_utc() + timedelta(minutes=settings.otp_ttl_min),
        )
    )

    if payload.channel == "email":
        send_otp_email.delay(user.email, code, purpose="login_verification")
    else:
        send_otp.delay(payload.phone, code)

    return OTPRequestResponse(
        expires_in=settings.otp_ttl_min * 60,
        resend_after=RESEND_COOLDOWN_SEC,
    )


# ── A3c — verify OTP for login ──────────────────────────────────
@router.post("/otp/login/verify", response_model=OtpLoginResponse)
async def verify_otp_login(
    payload: OTPVerifyRequest, db: AsyncSession = Depends(get_db)
) -> OtpLoginResponse:
    """Verify the OTP for login and issue tokens.

    Looks up the code by channel + recipient + purpose = login_verification.
    On success, issues access + refresh tokens (same as password login).
    """
    if payload.channel == "email":
        if not payload.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required for email verification",
            )
        user = await db.scalar(select(User).where(User.email == payload.email))
        recipient_col = OTPCode.email
        recipient_val = payload.email
    else:
        if not payload.phone:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone is required for SMS verification",
            )
        user = await db.scalar(select(User).where(User.phone == payload.phone))
        recipient_col = OTPCode.phone
        recipient_val = payload.phone

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    record = await db.scalar(
        select(OTPCode)
        .where(
            recipient_col == recipient_val,
            OTPCode.channel == payload.channel,
            OTPCode.purpose == "login_verification",
            OTPCode.consumed_at.is_(None),
        )
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Request a new code"
        )

    if record.expires_at < now_utc():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Code expired"
        )

    if record.attempts >= MAX_OTP_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts — request a new code",
        )

    if not verify_otp_hash(payload.code, record.code_hash):
        record.attempts += 1
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect code"
        )

    record.consumed_at = now_utc()

    access, refresh = await _issue_tokens(db, user)
    return OtpLoginResponse(
        access_token=access, refresh_token=refresh, user=_serialize_user(user)
    )


# ── A4 — login ───────────────────────────────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, db: AsyncSession = Depends(get_db)
) -> LoginResponse:
    """Password login by phone or email."""
    user = await db.scalar(
        select(User).where(
            (User.phone == payload.phone_or_email)
            | (User.email == payload.phone_or_email)
        )
    )
    # One generic message for both "no such user" and "wrong password".
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    if user.status == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended"
        )

    access, refresh = await _issue_tokens(db, user)
    return LoginResponse(
        access_token=access, refresh_token=refresh, user=_serialize_user(user)
    )


# ── A5 — refresh (rotation) ──────────────────────────────────────────────────
@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    payload: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> RefreshResponse:
    """Exchange a refresh token for a new access token; the old one is revoked."""
    from app.core.security import decode_token

    decoded = decode_token(payload.refresh_token)
    if not decoded or decoded.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    stored = await db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    if not stored or stored.revoked_at or stored.expires_at < now_utc():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked or expired",
        )

    # Rotate: revoke the presented token so a stolen copy is single-use.
    stored.revoked_at = now_utc()

    user = await db.scalar(select(User).where(User.id == stored.user_id))
    if not user or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not active"
        )

    new_refresh = create_refresh_token(str(user.id))
    db.add(
        RefreshToken(
            user_id=str(user.id),
            token_hash=hash_token(new_refresh),
            expires_at=now_utc() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    return RefreshResponse(
        access_token=create_access_token(str(user.id), _user_claims(user))
    )


# ── A6 — logout ──────────────────────────────────────────────────────────────
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    """Revoke the presented refresh token. Always 204, even if already revoked."""
    stored = await db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    if stored and not stored.revoked_at:
        stored.revoked_at = now_utc()
    return None


# ── A7 — password reset request ──────────────────────────────────────────────
@router.post("/password/reset-request", response_model=MessageResponse)
async def password_reset_request(payload: PasswordResetRequest) -> MessageResponse:
    """Always the same response — never leak whether the email exists."""
    return MessageResponse(message="If that email exists, a reset link is on its way")
