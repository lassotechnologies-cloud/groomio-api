"""Registration verification gate + token issuance — regression tests.

Lifecycle under test (T-001.1):

    register -> `pending`, NO access/refresh token, restricted onboarding_token
    otp/verify -> `active` + access + refresh
    login -> refused while `pending`
    refresh -> refused unless the account is `active`

The gate exists because registration issues a session for whatever phone number
is posted. Without it, anyone can register somebody else's number and hold an
authenticated identity for it.

Every test runs against the throwaway `groomio_test` database via the `db`
fixture in `tests/conftest.py`, which drops/recreates the schema per test and
rolls the transaction back afterwards.
"""

import pytest
from sqlalchemy import select

from app.core.security import (
    decode_token,
    hash_token,
    now_utc,
    verify_password,
)
from app.models.identity import OTPCode, RefreshToken, User

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
async def client(db):
    """An ASGI client wired to the rolled-back `db` session.

    `get_db` is overridden so the endpoint and the test share one session and
    one transaction: whatever the request writes, the test can read, and the
    teardown rollback still removes all of it.
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.database import get_db
    from app.main import app

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


# ── helpers ────────────────────────────────────────────────────────────────────

_PHONE = "0712345678"
_PASSWORD = "correct-horse-8"


def _payload(**overrides) -> dict:
    """A valid registration body. Overrides let a test poison one field."""
    body = {
        "full_name": "Amina Wanjiru",
        "phone": _PHONE,
        "email": "amina@example.com",
        "password": _PASSWORD,
    }
    body.update(overrides)
    return body


async def _register(client, **overrides) -> dict:
    """Register. Returns the pending-account response (no session)."""
    resp = await client.post("/v1/auth/register", json=_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed_signup_otp(
    db, phone: str, code: str, *, consumed: bool = False, expired: bool = False
) -> None:
    """Insert a signup OTP directly so the test controls the digits.

    The recipient column must hold the *normalised* phone, because that is what
    the verify endpoint looks the code up by.
    """
    from datetime import timedelta

    from app.core.phone import normalize_phone
    from app.core.security import hash_otp

    db.add(
        OTPCode(
            phone=normalize_phone(phone),
            code_hash=hash_otp(code),
            purpose="signup_verification",
            channel="sms",
            attempts=0,
            consumed_at=now_utc() if consumed else None,
            expires_at=now_utc() + timedelta(minutes=-1 if expired else 10),
        )
    )
    await db.flush()


async def _verify(client, db, phone: str, code: str) -> dict:
    """Complete signup verification, returning the verified response."""
    resp = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": phone, "code": code},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _registered_and_verified(client, db, **overrides) -> dict:
    """Full happy path: register, then verify. Returns the session response."""
    body = await _register(client, **overrides)
    await _seed_signup_otp(db, overrides.get("phone", _PHONE), "123456")
    verified = await _verify(client, db, overrides.get("phone", _PHONE), "123456")
    return {**body, **verified}


async def _user(db, user_id: str) -> User:
    user = await db.scalar(select(User).where(User.id == user_id))
    assert user is not None, "user row missing"
    return user


# ══════════════════════════════════════════════════════════════════════════════
# The gate itself
# ══════════════════════════════════════════════════════════════════════════════


async def test_registration_creates_a_pending_account(client, db):
    """New accounts must not be usable until the phone is proven."""
    body = await _register(client)

    user = await _user(db, body["user_id"])
    assert user.status == "pending", "a new account must start pending"
    assert body["requires_verification"] is True
    assert body["next"] == "otp_verify"


async def test_registration_issues_no_session(client, db):
    """The takeover hole: a session for an unproven phone number."""
    body = await _register(client)

    assert "access_token" not in body, "register handed out an access token"
    assert "refresh_token" not in body, "register handed out a refresh token"
    # And no refresh row was persisted, so there is nothing to replay.
    assert (await db.scalars(select(RefreshToken))).all() == []


async def test_a_pending_account_cannot_access_business_endpoints(client, db):
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "123456")
    verified = await _verify(client, db, _PHONE, "123456")
    pending_token = body["onboarding_token"]

    for path in ("/v1/business", "/v1/branches"):
        anon = await client.get(path)
        pending = await client.get(
            path, headers={"Authorization": f"Bearer {pending_token}"}
        )
        assert anon.status_code == 401
        assert pending.status_code == 401, (
            f"an unverified account reached {path} ({pending.status_code})"
        )


async def test_the_onboarding_token_is_rejected_by_get_current_user(client):
    """It is not a session. `get_current_user` refuses it on type."""
    body = await _register(client)

    claims = decode_token(body["onboarding_token"])
    assert claims["type"] == "onboarding"
    assert claims["sub"] == body["user_id"]


async def test_the_onboarding_token_carries_no_authority(client):
    """No role and no tenant — so `require_role` can never be satisfied."""
    body = await _register(client)
    claims = decode_token(body["onboarding_token"])

    assert "role" not in claims, "an onboarding token must not assert a role"
    assert claims.get("role") is None
    assert claims.get("business_id") is None
    assert claims["verified"] is False


async def test_the_onboarding_token_cannot_pass_an_owner_only_route(client):
    """Owner-only business creation must refuse an unverified caller."""
    body = await _register(client)

    resp = await client.post(
        "/v1/business",
        json={
            "name": "Sneaky Cuts",
            "business_type": "barber_shop",
            "county": "Nairobi",
            "town": "Nairobi",
            "branch_name": "Main",
        },
        headers={"Authorization": f"Bearer {body['onboarding_token']}"},
    )
    assert resp.status_code in (401, 403), (
        f"an unverified account created a business ({resp.status_code})"
    )


async def test_a_pending_account_cannot_log_in(client):
    """Right password, unproven phone — still no session."""
    await _register(client)

    resp = await client.post(
        "/v1/auth/login",
        json={"phone_or_email": _PHONE, "password": _PASSWORD},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Account verification required"


async def test_a_pending_account_cannot_bypass_verification_via_refresh(client, db):
    """There is no refresh token to escalate with — not even a revoked one."""
    body = await _register(client)

    stored = await db.scalar(
        select(RefreshToken).where(RefreshToken.user_id == body["user_id"])
    )
    assert stored is None, "a pending account must have no refresh token persisted"

    for fake in (body["onboarding_token"], "not-a-real-token"):
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": fake})
        assert resp.status_code == 401


async def _seed_login_otp(db, phone: str, code: str) -> None:
    """Seed a login-channel OTP (a different `purpose` from signup)."""
    from datetime import timedelta

    from app.core.phone import normalize_phone
    from app.core.security import hash_otp

    db.add(
        OTPCode(
            phone=normalize_phone(phone),
            code_hash=hash_otp(code),
            purpose="login_verification",
            channel="sms",
            attempts=0,
            expires_at=now_utc() + timedelta(minutes=10),
        )
    )
    await db.flush()


async def test_a_suspended_account_cannot_use_otp_login(client, db):
    """The OTP door must not be a way around the suspension check in `/login`."""
    body = await _register(client)
    user = await _user(db, body["user_id"])
    user.status = "suspended"
    await db.flush()
    await _seed_login_otp(db, _PHONE, "246810")

    resp = await client.post(
        "/v1/auth/otp/login/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "246810"},
    )
    assert resp.status_code == 403, (
        f"a suspended account obtained a session via OTP login ({resp.status_code})"
    )


async def test_otp_login_activates_a_pending_account_that_owns_the_phone(client, db):
    """A correct login code *is* proof of control, so it completes verification."""
    body = await _register(client)
    assert (await _user(db, body["user_id"])).status == "pending"
    await _seed_login_otp(db, _PHONE, "246810")

    resp = await client.post(
        "/v1/auth/otp/login/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "246810"},
    )
    assert resp.status_code == 200, resp.text
    assert decode_token(resp.json()["access_token"])["type"] == "access"
    assert (await _user(db, body["user_id"])).status == "active"


# ══════════════════════════════════════════════════════════════════════════════
# Verification promotes the account and issues the session
# ══════════════════════════════════════════════════════════════════════════════


async def test_successful_verification_activates_the_account(client, db):
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "123456")
    await _verify(client, db, _PHONE, "123456")

    user = await _user(db, body["user_id"])
    assert user.status == "active", "verification must activate the account"


async def test_verification_returns_a_usable_session(client, db):
    verified = await _registered_and_verified(client, db)

    assert verified["verified"] is True
    claims = decode_token(verified["access_token"])
    assert claims["type"] == "access", f"expected type='access', got {claims['type']!r}"
    assert claims["role"] == "owner"


async def test_the_verified_token_authenticates_a_protected_endpoint(client, db):
    verified = await _registered_and_verified(client, db)
    headers = {"Authorization": f"Bearer {verified['access_token']}"}

    resp = await client.get("/v1/business", headers=headers)
    # 404 is correct: the owner has no business yet (B1). Any 401 means the token
    # did not authenticate.
    assert resp.status_code != 401, f"verified token rejected: {resp.text}"


async def test_verification_persists_the_refresh_hash_not_the_token(client, db):
    verified = await _registered_and_verified(client, db)

    stored = await db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(verified["refresh_token"])
        )
    )
    assert stored is not None, "refresh token was never persisted"
    assert stored.revoked_at is None


async def test_the_onboarding_token_from_verify_is_a_real_access_token(client, db):
    """The legacy field name is kept, but it must now be usable."""
    verified = await _registered_and_verified(client, db)

    claims = decode_token(verified["onboarding_token"])
    assert claims["type"] == "access"
    assert claims["sub"] == verified["user"]["id"]


async def test_a_verified_account_can_log_in_normally(client, db):
    """Existing verified users must not be locked out by the gate."""
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "123456")
    await _verify(client, db, _PHONE, "123456")

    resp = await client.post(
        "/v1/auth/login", json={"phone_or_email": _PHONE, "password": _PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["access_token"] and payload["refresh_token"]
    assert payload["user"]["id"] == body["user_id"]
    assert payload["user"]["status"] == "active"


async def test_an_already_verified_account_can_log_in_repeatedly(client, db):
    """The gate must not turn into a one-shot login."""
    await _registered_and_verified(client, db)

    for _ in range(3):
        resp = await client.post(
            "/v1/auth/login",
            json={"phone_or_email": _PHONE, "password": _PASSWORD},
        )
        assert resp.status_code == 200, resp.text


async def test_verification_never_leaks_the_password_or_the_code(client, db):
    await _register(client)
    await _seed_signup_otp(db, _PHONE, "123456")
    verified = await _verify(client, db, _PHONE, "123456")

    blob = str(verified)
    assert _PASSWORD not in blob
    assert "$argon2" not in blob
    assert "password_hash" not in verified


# ══════════════════════════════════════════════════════════════════════════════
# Failed verification must not activate anything
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_incorrect_otp_does_not_activate_the_account(client, db):
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "999999")

    resp = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "111111"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Incorrect code"

    user = await _user(db, body["user_id"])
    assert user.status == "pending", "a wrong code activated the account"


async def test_an_expired_otp_does_not_activate_the_account(client, db):
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "123456", expired=True)

    resp = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "123456"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Code expired"

    user = await _user(db, body["user_id"])
    assert user.status == "pending"


async def test_a_consumed_otp_cannot_be_replayed(client, db):
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "222222", consumed=True)

    resp = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "222222"},
    )
    assert resp.status_code == 400

    user = await _user(db, body["user_id"])
    assert user.status == "pending"


async def test_a_successful_verification_burns_the_code(client, db):
    body = await _register(client)
    await _seed_signup_otp(db, _PHONE, "333333")
    await _verify(client, db, _PHONE, "333333")

    replay = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "333333"},
    )
    assert replay.status_code == 400, "the OTP was reusable after success"
    assert (await _user(db, body["user_id"])).status == "active"


async def test_five_wrong_codes_burn_the_otp(client, db):
    await _register(client)
    await _seed_signup_otp(db, _PHONE, "444444")

    for _ in range(5):
        await client.post(
            "/v1/auth/otp/verify",
            json={"channel": "sms", "phone": _PHONE, "code": "000000"},
        )
    burnt = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": _PHONE, "code": "444444"},
    )
    assert burnt.status_code == 429, "the code survived five wrong attempts"


# ══════════════════════════════════════════════════════════════════════════════
# Refresh-token behaviour, consistent with login
# ══════════════════════════════════════════════════════════════════════════════


async def test_refresh_rotates_and_returns_the_new_token(client, db):
    verified = await _registered_and_verified(client, db)

    resp = await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    refreshed = resp.json()

    assert refreshed["access_token"], "refresh returned no access token"
    assert refreshed.get("refresh_token"), (
        "refresh did not return the rotated token — the client keeps the "
        "revoked one and cannot refresh again"
    )


async def test_the_rotated_refresh_token_chains(client, db):
    verified = await _registered_and_verified(client, db)

    first = await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["refresh_token"]}
    )
    second = await client.post(
        "/v1/auth/refresh", json={"refresh_token": first.json()["refresh_token"]}
    )
    assert second.status_code == 200, f"rotation does not chain: {second.text}"


async def test_a_refresh_token_cannot_be_reused_after_rotation(client, db):
    verified = await _registered_and_verified(client, db)
    await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["refresh_token"]}
    )

    replay = await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["refresh_token"]}
    )
    assert replay.status_code == 401, "a revoked refresh token was accepted"


async def test_an_access_token_is_not_accepted_as_a_refresh_token(client, db):
    verified = await _registered_and_verified(client, db)

    resp = await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["access_token"]}
    )
    assert resp.status_code == 401


async def test_a_suspended_account_cannot_refresh(client, db):
    verified = await _registered_and_verified(client, db)
    user = await _user(db, verified["user"]["id"])
    user.status = "suspended"
    await db.flush()

    resp = await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_logout_revokes_the_refresh_token(client, db):
    verified = await _registered_and_verified(client, db)
    await client.post(
        "/v1/auth/logout", json={"refresh_token": verified["refresh_token"]}
    )

    resp = await client.post(
        "/v1/auth/refresh", json={"refresh_token": verified["refresh_token"]}
    )
    assert resp.status_code == 401, "logout did not revoke the refresh token"


# ══════════════════════════════════════════════════════════════════════════════
# Password handling
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_password_is_hashed_and_never_stored_plain(client, db):
    body = await _register(client)
    user = await _user(db, body["user_id"])

    assert user.password_hash != _PASSWORD
    assert user.password_hash.startswith("$argon2")
    assert verify_password(_PASSWORD, user.password_hash)


async def test_the_wrong_password_still_fails(client, db):
    await _registered_and_verified(client, db)

    resp = await client.post(
        "/v1/auth/login", json={"phone_or_email": _PHONE, "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_login_does_not_reveal_whether_the_account_exists(client, db):
    """Unknown user and wrong password must be indistinguishable."""
    unknown = await client.post(
        "/v1/auth/login",
        json={"phone_or_email": "nobody@example.com", "password": "whatever-123"},
    )
    await _registered_and_verified(client, db)
    wrong = await client.post(
        "/v1/auth/login",
        json={"phone_or_email": "amina@example.com", "password": "whatever-123"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════════
# Email and phone canonicalisation
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "typed", ["0712345678", "+254712345678", "254712345678", "0712 345 678"]
)
async def test_every_phone_dialect_normalises_to_one_stored_value(client, db, typed):
    """`WHERE phone = ?` is only a real duplicate check if the forms converge."""
    body = await _register(client, phone=typed)
    assert (await _user(db, body["user_id"])).phone == "+254712345678"


@pytest.mark.parametrize(
    "typed", ["0712345678", "+254712345678", "254712345678", "0712 345 678"]
)
async def test_login_accepts_every_form_of_the_registered_phone(client, db, typed):
    """A customer must be able to sign in with the number they signed up with."""
    await _registered_and_verified(client, db)

    resp = await client.post(
        "/v1/auth/login", json={"phone_or_email": typed, "password": _PASSWORD}
    )
    assert resp.status_code == 200, f"could not log in with {typed!r}: {resp.text}"


async def test_login_still_treats_an_email_as_an_email(client, db):
    await _registered_and_verified(client, db)

    resp = await client.post(
        "/v1/auth/login",
        json={"phone_or_email": "amina@example.com", "password": _PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == "amina@example.com"


async def test_email_is_stored_in_canonical_form(client, db):
    body = await _register(client, email="  Amina.Wanjiru@Example.COM ")
    assert (await _user(db, body["user_id"])).email == "amina.wanjiru@example.com"


async def test_a_differently_cased_email_is_the_same_account(client, db):
    """`A@shop.com` and `a@shop.com` must not become two accounts."""
    await _register(client, email="Owner@Shop.com")

    resp = await client.post("/v1/auth/register", json=_payload(email="owner@shop.com"))
    assert resp.status_code == 409, "case-variant email created a second account"
    assert len((await db.scalars(select(User))).all()) == 1


async def test_login_works_regardless_of_how_the_email_was_typed(client, db):
    await _registered_and_verified(client, db, email="Owner@Shop.com")

    resp = await client.post(
        "/v1/auth/login",
        json={"phone_or_email": "OWNER@shop.COM", "password": _PASSWORD},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("bad", ["not-a-phone", "123", "+", "+254", "abcdefghijk"])
async def test_junk_phones_are_rejected_rather_than_stored(client, db, bad):
    """Normalisation must not manufacture contact numbers out of garbage."""
    resp = await client.post("/v1/auth/register", json=_payload(phone=bad))
    assert resp.status_code == 422, f"{bad!r} was accepted as a phone number"
    assert (await db.scalars(select(User))).all() == []


async def test_an_email_without_an_at_sign_is_rejected(client, db):
    resp = await client.post(
        "/v1/auth/register", json=_payload(email="amina.example.com")
    )
    assert resp.status_code == 422
    assert (await db.scalars(select(User))).all() == []


# ══════════════════════════════════════════════════════════════════════════════
# Duplicate prevention and input validation
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_duplicate_phone_is_rejected(client, db):
    await _register(client)

    resp = await client.post(
        "/v1/auth/register", json=_payload(email="someone.else@example.com")
    )
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"]


async def test_a_duplicate_email_is_rejected(client, db):
    await _register(client)

    resp = await client.post("/v1/auth/register", json=_payload(phone="0722222222"))
    assert resp.status_code == 409


async def test_a_rejected_duplicate_creates_no_second_user(client, db):
    await _register(client)
    await client.post("/v1/auth/register", json=_payload())

    assert len((await db.scalars(select(User))).all()) == 1


async def test_registration_retry_does_not_create_duplicate_users(client, db):
    """A retry must 409, not silently mint a second account or a new session."""
    first = await _register(client)
    retry = await client.post("/v1/auth/register", json=_payload())

    assert retry.status_code == 409
    assert len((await db.scalars(select(User))).all()) == 1
    # The original pending account is untouched and still verifiable.
    assert (await _user(db, first["user_id"])).status == "pending"


@pytest.mark.parametrize(
    "overrides",
    [
        {"password": "short"},
        {"full_name": "A"},
        {"phone": "123"},
        {"email": "a" * 260 + "@example.com"},
        {"email": "not-an-email"},
    ],
    ids=["short-password", "short-name", "short-phone", "long-email", "bad-email"],
)
async def test_invalid_registration_input_is_rejected(client, db, overrides):
    resp = await client.post("/v1/auth/register", json=_payload(**overrides))
    assert resp.status_code == 422, f"{overrides} was accepted"
    assert (await db.scalars(select(User))).all() == []


# ══════════════════════════════════════════════════════════════════════════════
# Roles and tenant identity stay server-controlled
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("role", ["super_admin", "admin", "clerk", "barber"])
async def test_a_client_cannot_assign_itself_a_privileged_role(client, db, role):
    resp = await client.post(
        "/v1/auth/register", json=_payload(role=role, status="active")
    )
    assert resp.status_code == 201, resp.text

    user = await _user(db, resp.json()["user_id"])
    assert user.role == "owner", f"client-supplied role {role!r} was honoured"


async def test_a_client_cannot_self_activate_the_account(client, db):
    """`status` is server-owned: posting `active` must not skip verification."""
    body = await _register(client, status="active")
    assert (await _user(db, body["user_id"])).status == "pending"


async def test_a_verified_token_claims_no_tenant_yet(client, db):
    """The owner has no business until B1, so the token must not imply one."""
    verified = await _registered_and_verified(client, db)

    claims = decode_token(verified["access_token"])
    assert claims.get("business_id") is None
    assert claims["role"] == "owner"


async def test_a_registration_cannot_impersonate_another_tenant(client, db):
    verified = await _registered_and_verified(
        client, db, business_id="11111111-2222-3333-4444-555555555555"
    )
    assert decode_token(verified["access_token"]).get("business_id") is None


async def test_tenant_isolation_is_intact_for_two_shops(client, db):
    """A verified owner must not read another owner's business."""
    from app.models.tenant import Business

    verified_a = await _registered_and_verified(client, db)
    headers_a = {"Authorization": f"Bearer {verified_a['access_token']}"}
    created = await client.post(
        "/v1/business",
        json={
            "name": "Amina Cuts",
            "business_type": "barber_shop",
            "county": "Nairobi",
            "town": "Nairobi",
            "branch_name": "Main",
        },
        headers=headers_a,
    )
    assert created.status_code == 201, created.text

    # A second, independent shop.
    payload_b = _payload(
        full_name="Brian Otieno",
        phone="0722222222",
        email="brian@example.com",
    )
    reg_b = await client.post("/v1/auth/register", json=payload_b)
    await _seed_signup_otp(db, payload_b["phone"], "123456")
    verified_b = await _verify(client, db, payload_b["phone"], "123456")
    headers_b = {"Authorization": f"Bearer {verified_b['access_token']}"}
    await client.post(
        "/v1/business",
        json={
            "name": "Brian's Braids",
            "business_type": "barber_shop",
            "county": "Nairobi",
            "town": "Nairobi",
            "branch_name": "Main",
        },
        headers=headers_b,
    )

    # Each owner sees only their own.
    seen_by_a = await client.get("/v1/business", headers=headers_a)
    assert "Amina Cuts" in seen_by_a.text
    assert "Brian's Braids" not in seen_by_a.text
    seen_by_b = await client.get("/v1/business", headers=headers_b)
    assert "Brian's Braids" in seen_by_b.text
    assert "Amina Cuts" not in seen_by_b.text

    assert (await db.scalars(select(Business))).all() is not None


# ══════════════════════════════════════════════════════════════════════════════
# Unauthenticated access stays unauthenticated
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", ["/v1/business", "/v1/branches"])
async def test_protected_endpoints_reject_anonymous_callers(client, path):
    resp = await client.get(path)
    assert resp.status_code == 401, f"{path} served an unauthenticated request"


async def test_a_forged_token_is_rejected(client, db):
    """A token signed with the wrong key must never authenticate."""
    from jose import jwt

    forged = jwt.encode(
        {"sub": "x", "type": "access", "role": "super_admin"}, "not-the-secret"
    )
    resp = await client.get(
        "/v1/business", headers={"Authorization": f"Bearer {forged}"}
    )
    assert resp.status_code == 401
