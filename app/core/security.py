"""Security core — Doc 11.1.

- Passwords: argon2id, never stored/logged plain. Min 8 chars.
- Tokens: short access (JWT 15 min) + long refresh (30 days, stored hashed,
  revocable). Refresh rotation on use.
- OTP: 6 random digits via `secrets` (not `random`); stored hashed; 10-min
  expiry; 5 wrong attempts burn the code; Redis rate limits.
"""

import secrets
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt
from passlib.hash import argon2

from app.core.config import settings


# ── Passwords ──────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return argon2.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return argon2.verify(plain, hashed)
    except Exception:
        return False


# ── JWT tokens ─────────────────────────────────────────────────────────────
def create_access_token(sub: str, extra_claims: dict | None = None) -> str:
    payload = {
        "sub": sub,
        "type": "access",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=settings.access_token_ttl_min),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def create_refresh_token(sub: str) -> str:
    payload = {
        "sub": sub,
        "type": "refresh",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(days=settings.refresh_token_ttl_days),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except Exception:
        return None


def hash_token(token: str) -> str:
    """Store refresh tokens hashed — never plain (Doc 9 refresh_tokens)."""
    return hashlib.sha256(token.encode()).hexdigest()


# ── OTP ────────────────────────────────────────────────────────────────────
def generate_otp() -> str:
    """6 random digits via `secrets` — not `random` (Doc 11.1)."""
    return "".join(secrets.choice("0123456789") for _ in range(6))


def hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def verify_otp_hash(provided: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_otp(provided), stored_hash)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()
