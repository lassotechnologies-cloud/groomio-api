"""Business onboarding — integration tests for `POST /v1/business` + `GET /v1/onboarding/status`.

What is under test (T-002):

    verified owner -> POST /v1/business -> business + first branch + trial
    repeat submission            -> the same rows, returned again (200)
    GET /v1/onboarding/status    -> where this owner actually is
    anything else                -> refused

The failure this suite exists to prevent is a tenant leak or a duplicate shop
produced by onboarding itself, so the assertions are deliberately about *the
database*, not just the response body. A 201 that silently created a second
`businesses` row would pass every response-shape assertion and still be a
production incident, so most of these read the tables back.

Ownership is never a parameter. There is no `business_id` on any request
schema, and the tests below prove that posting one changes nothing — including
posting another owner's id.
"""

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token, hash_otp, now_utc
from app.models.billing import Plan, Subscription
from app.models.identity import OTPCode, User
from app.models.tenant import Branch, Business

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
async def client(db):
    """An ASGI client sharing the rolled-back `db` session with the test."""
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

_PASSWORD = "correct-horse-8"


def _payload(**overrides) -> dict:
    """A valid business-creation body. Overrides let a test poison one field."""
    body = {
        "name": "Sharp Cuts Barber",
        "business_type": "barber_shop",
        "county": "Nairobi",
        "town": "Nairobi",
        "branch_name": "Westlands",
        "opens_at": "08:00:00",
        "closes_at": "20:00:00",
        "num_chairs": 6,
    }
    body.update(overrides)
    return body


async def _verified_owner(client, db, *, phone="0712345678", name="Amina Wanjiru"):
    """Register, verify the OTP, and return `(user_id, access_token)`.

    Walks the real endpoints rather than inserting rows, so the token these
    tests present is exactly the one a real owner would hold — including the
    `role` claim that `require_role` reads.
    """
    from datetime import timedelta

    from app.core.phone import normalize_phone

    resp = await client.post(
        "/v1/auth/register",
        json={
            "full_name": name,
            "phone": phone,
            "email": f"{phone}@example.com",
            "password": _PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["user_id"]

    # Seeded directly so the test controls the digits; the recipient column must
    # hold the *normalised* phone, because that is what verify looks it up by.
    db.add(
        OTPCode(
            phone=normalize_phone(phone),
            code_hash=hash_otp("123456"),
            purpose="signup_verification",
            channel="sms",
            attempts=0,
            expires_at=now_utc() + timedelta(minutes=10),
        )
    )
    await db.flush()

    verify = await client.post(
        "/v1/auth/otp/verify",
        json={"channel": "sms", "phone": phone, "code": "123456"},
    )
    assert verify.status_code == 200, verify.text
    return user_id, verify.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create(client, token: str, **overrides):
    return await client.post(
        "/v1/business", json=_payload(**overrides), headers=_auth(token)
    )


async def _seed_plan(db) -> Plan:
    """The seeded cheapest plan. Migration 0003 does this in production."""
    plan = Plan(code="daily", price_kes=40, interval_days=1, is_active=True)
    db.add(plan)
    await db.flush()
    return plan


async def _business_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(Business))


async def _branch_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(Branch))


# ══════════════════════════════════════════════════════════════════════════════
# The happy path
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_verified_owner_creates_a_business(client, db):
    _, token = await _verified_owner(client, db)

    resp = await _create(client, token)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["business"]["name"] == "Sharp Cuts Barber"
    assert body["business"]["business_type"] == "barber_shop"
    # The trial starts now rather than at first billing-page visit, so the
    # countdown cannot silently lengthen every time the app is opened.
    assert body["business"]["status"] == "trialing"
    assert body["business"]["trial_ends_at"] is not None


async def test_the_business_is_owned_by_the_caller(client, db):
    user_id, token = await _verified_owner(client, db)

    await _create(client, token)

    biz = await db.scalar(select(Business))
    assert biz is not None
    assert biz.owner_user_id == user_id, "ownership must come from the token"


async def test_the_first_branch_belongs_to_the_new_business(client, db):
    _, token = await _verified_owner(client, db)

    body = (await _create(client, token)).json()

    branch = await db.scalar(select(Branch))
    assert branch is not None
    assert str(branch.id) == body["branch"]["id"]
    assert branch.business_id == body["business"]["id"]
    assert branch.name == "Westlands"
    assert branch.num_chairs == 6
    assert branch.is_active is True
    # The slug is the public booking link segment, so it must exist and be
    # unique without the client supplying one.
    assert branch.slug == "westlands"


async def test_creation_opens_exactly_one_trial_subscription(client, db):
    plan = await _seed_plan(db)
    _, token = await _verified_owner(client, db)

    await _create(client, token)

    subs = (await db.scalars(select(Subscription))).all()
    assert len(subs) == 1
    assert subs[0].plan_id == str(plan.id)
    assert subs[0].status == "trial"


async def test_branch_contact_details_are_persisted(client, db):
    """Migration 0007 added `address`/`phone`; onboarding can finally set them."""
    _, token = await _verified_owner(client, db)

    await _create(
        client,
        token,
        branch_address=" Moi Avenue, near Nation Centre",
        branch_phone="020 123 4567",
    )

    branch = await db.scalar(select(Branch))
    assert branch.address == "Moi Avenue, near Nation Centre"
    # Stored verbatim. `normalize_phone` is Kenyan-mobile-specific and would
    # rewrite a landline into a number that dials somewhere else entirely.
    assert branch.phone == "020 123 4567"


async def test_blank_optional_fields_are_stored_as_null(client, db):
    """An empty form field is "not supplied", not the empty string."""
    _, token = await _verified_owner(client, db)

    await _create(client, token, branch_address="   ", branch_phone="")

    branch = await db.scalar(select(Branch))
    assert branch.address is None
    assert branch.phone is None


# ══════════════════════════════════════════════════════════════════════════════
# Idempotency — the whole reason a lost response is survivable
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_repeat_submission_returns_the_same_business(client, db):
    """Not a 409: the client cannot tell a lost response from a failed one."""
    _, token = await _verified_owner(client, db)

    first = await _create(client, token)
    assert first.status_code == 201

    second = await _create(client, token)

    assert second.status_code == 200, second.text
    assert second.json()["business"]["id"] == first.json()["business"]["id"]
    assert second.json()["branch"]["id"] == first.json()["branch"]["id"]
    # Same next step as the first attempt, so a retry lands where the original
    # would have instead of stranding the owner on an error.
    assert second.json()["next"] == first.json()["next"]


async def test_a_repeat_submission_creates_no_duplicate_rows(client, db):
    _, token = await _verified_owner(client, db)

    await _create(client, token)
    before = (await _business_count(db), await _branch_count(db))

    for _ in range(3):
        await _create(client, token, name="A Different Name Entirely")

    assert (await _business_count(db), await _branch_count(db)) == before == (1, 1)
    # The replayed payload must not overwrite what is already stored — the
    # owner's real shop is not a scratchpad for whatever the form held last.
    assert (await db.scalar(select(Business))).name == "Sharp Cuts Barber"


async def test_a_repeat_submission_does_not_restart_the_trial(client, db):
    await _seed_plan(db)
    _, token = await _verified_owner(client, db)

    await _create(client, token)
    first_ends = (await db.scalar(select(Subscription))).current_period_end

    await _create(client, token)

    subs = (await db.scalars(select(Subscription))).all()
    assert len(subs) == 1, "a retry must not open a second trial"
    assert subs[0].current_period_end == first_ends


async def test_a_lost_response_recovers_on_retry(client, db):
    """The database committed but the client never saw it.

    This is the case a conflict response cannot rescue: the owner has no way to
    distinguish it from a failed write, so the retry has to be the thing that
    resolves it.
    """
    _, token = await _verified_owner(client, db)

    # The write lands...
    created = await _create(client, token)
    assert created.status_code == 201

    # ...and the response is lost. The owner taps save again.
    retried = await _create(client, token)

    assert retried.status_code == 200
    assert retried.json()["business"]["id"] == created.json()["business"]["id"]
    assert await _business_count(db) == 1
    assert await _branch_count(db) == 1


async def test_a_business_without_a_branch_is_completed_not_duplicated(client, db):
    """A shop with no location is unusable, so the replay repairs it in place.

    Creates the first branch. Writing a *second* one would give the owner two
    locations they never asked for, both with the same name.
    """
    user_id, token = await _verified_owner(client, db)

    biz = Business(
        owner_user_id=user_id,
        name="Orphan Shop",
        business_type="salon",
        status="trialing",
    )
    db.add(biz)
    await db.flush()
    assert await _branch_count(db) == 0

    resp = await _create(client, token)

    assert resp.status_code == 200
    assert resp.json()["business"]["id"] == str(biz.id)
    assert await _business_count(db) == 1
    assert await _branch_count(db) == 1
    assert (await db.scalar(select(Branch))).business_id == str(biz.id)


# ══════════════════════════════════════════════════════════════════════════════
# Reading progress back
# ══════════════════════════════════════════════════════════════════════════════


async def test_onboarding_status_for_a_fresh_owner(client, db):
    user_id, token = await _verified_owner(client, db)

    resp = await client.get("/v1/onboarding/status", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == user_id
    assert body["account_status"] == "active"
    assert body["has_business"] is False
    assert body["has_branch"] is False
    assert body["completed"] is False
    assert body["next"] == "business"
    assert body["business"] is None and body["branch"] is None


async def test_onboarding_status_after_creation_points_forward(client, db):
    _, token = await _verified_owner(client, db)
    created = (await _create(client, token)).json()

    body = (await client.get("/v1/onboarding/status", headers=_auth(token))).json()

    assert body["has_business"] is True
    assert body["has_branch"] is True
    assert body["completed"] is True
    assert body["next"] == "subscription"
    assert body["business"]["id"] == created["business"]["id"]
    assert body["branch"]["id"] == created["branch"]["id"]
    assert body["branch_count"] == 1


async def test_onboarding_status_is_scoped_to_the_caller(client, db):
    """One owner asking must never see another owner's progress."""
    _, token_a = await _verified_owner(client, db, phone="0711000001")
    _, token_b = await _verified_owner(client, db, phone="0711000002")
    await _create(client, token_a, name="Amina Cuts")

    body_a = (await client.get("/v1/onboarding/status", headers=_auth(token_a))).json()
    body_b = (await client.get("/v1/onboarding/status", headers=_auth(token_b))).json()

    assert body_a["business"]["name"] == "Amina Cuts"
    assert body_b["business"] is None, "B must not see A's shop"
    assert body_b["next"] == "business"


async def test_onboarding_status_reports_a_business_with_no_branch_as_unfinished(
    client, db
):
    """No branch means no chairs, no catalogue and no booking link."""
    user_id, token = await _verified_owner(client, db)
    db.add(
        Business(
            owner_user_id=user_id,
            name="Half Done",
            business_type="nail_bar",
            status="trialing",
        )
    )
    await db.flush()

    body = (await client.get("/v1/onboarding/status", headers=_auth(token))).json()

    assert body["has_business"] is True
    assert body["has_branch"] is False
    assert body["completed"] is False
    assert body["next"] == "business"


# ══════════════════════════════════════════════════════════════════════════════
# Account lifecycle — only an active owner may create a tenant
# ══════════════════════════════════════════════════════════════════════════════


async def _token_for_status(db, user_id: str) -> str:
    """Mint an access token the way `_issue_tokens` would.

    Models the real threat: a session handed out before an administrator
    suspended the account. `require_role` reads the `role` claim, so this token
    still says `owner` — only a live status check can stop it.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    return create_access_token(
        str(user.id), {"role": user.role, "email": user.email, "business_id": None}
    )


async def test_a_pending_user_cannot_create_a_business(client, db):
    user_id, _ = await _verified_owner(client, db)
    user = await db.scalar(select(User).where(User.id == user_id))
    user.status = "pending"
    await db.flush()
    stale = await _token_for_status(db, user_id)

    resp = await _create(client, stale)

    assert resp.status_code == 403, resp.text
    assert await _business_count(db) == 0


async def test_a_suspended_user_cannot_create_a_business(client, db):
    """The one that mattered: honouring a revoked account here would create the
    tenant row that every other tenant-scoped check hangs off."""
    user_id, token = await _verified_owner(client, db)
    user = await db.scalar(select(User).where(User.id == user_id))
    user.status = "suspended"
    await db.flush()
    # The token is the one already issued — login and refresh both refuse to
    # mint a new one for a suspended account, so this is the only credential
    # the caller has, and it still says role=owner.
    assert decode_role(token) == "owner"

    resp = await _create(client, token)

    assert resp.status_code == 403, resp.text
    assert await _business_count(db) == 0


async def test_a_suspended_user_cannot_read_their_onboarding_status(client, db):
    user_id, token = await _verified_owner(client, db)
    user = await db.scalar(select(User).where(User.id == user_id))
    user.status = "suspended"
    await db.flush()

    resp = await client.get("/v1/onboarding/status", headers=_auth(token))

    assert resp.status_code == 403, resp.text


def decode_role(token: str) -> str | None:
    from app.core.security import decode_token

    return decode_token(token).get("role")


async def test_a_deleted_account_cannot_write_with_a_live_token(client, db):
    user_id, token = await _verified_owner(client, db)
    user = await db.scalar(select(User).where(User.id == user_id))
    await db.delete(user)
    await db.flush()

    resp = await _create(client, token)

    assert resp.status_code == 401, resp.text
    assert await _business_count(db) == 0


async def test_a_customer_role_cannot_create_a_business(client, db):
    """`require_role` is still the outer gate; the status check is not a
    substitute for it."""
    user_id, _ = await _verified_owner(client, db)
    user = await db.scalar(select(User).where(User.id == user_id))
    user.role = "customer"
    await db.flush()
    token = await _token_for_status(db, user_id)

    resp = await _create(client, token)

    assert resp.status_code == 403, resp.text


# ══════════════════════════════════════════════════════════════════════════════
# Credentials
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/business"),
        ("get", "/v1/onboarding/status"),
        ("get", "/v1/business"),
    ],
)
async def test_onboarding_endpoints_reject_anonymous_callers(client, db, method, path):
    resp = await getattr(client, method)(path)
    assert resp.status_code == 401, f"{method.upper()} {path} served an anonymous call"


async def test_the_restricted_onboarding_token_opens_no_onboarding_endpoint(
    client, db
):
    """The pre-verification credential stays useless for business state.

    `get_onboarding_user` was deliberately *not* used by the status endpoint: it
    names a pending registrant for OTP verification and grants no role, so
    reaching business state with it would mean an unverified account could read
    — and a future contributor could easily widen — a surface it has no business
    in.
    """
    reg = await client.post(
        "/v1/auth/register",
        json={
            "full_name": "Pending Person",
            "phone": "0719999999",
            "email": "pending@example.com",
            "password": _PASSWORD,
        },
    )
    assert reg.status_code == 201, reg.text
    restricted = reg.json()["onboarding_token"]

    # POST carries a full valid body, so the only thing that can reject it is
    # the credential.
    resp = await client.post(
        "/v1/business", json=_payload(), headers=_auth(restricted)
    )
    assert resp.status_code == 401, f"POST accepted the restricted token: {resp.text}"

    resp = await client.get("/v1/onboarding/status", headers=_auth(restricted))
    assert resp.status_code == 401, f"status accepted it: {resp.text}"

    assert await _business_count(db) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Ownership cannot be supplied
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_client_supplied_owner_id_cannot_take_ownership(client, db):
    victim_id, _ = await _verified_owner(client, db, phone="0711000003")
    attacker_id, token = await _verified_owner(client, db, phone="0711000004")

    resp = await _create(
        client,
        token,
        owner_user_id=victim_id,
        business_id="11111111-2222-3333-4444-555555555555",
    )

    assert resp.status_code == 201, resp.text
    biz = await db.scalar(select(Business))
    assert biz.owner_user_id == attacker_id
    assert biz.owner_user_id != victim_id
    assert await _business_count(db) == 1


async def test_a_user_cannot_claim_another_users_business(client, db):
    _, token_a = await _verified_owner(client, db, phone="0711000005")
    _, token_b = await _verified_owner(client, db, phone="0711000006")
    a = (await _create(client, token_a, name="Amina Cuts")).json()

    # B asks for A's business by id.
    seen = await client.get(f"/v1/business", headers=_auth(token_b))
    assert seen.status_code == 404, seen.text

    for path in (
        f"/v1/branches/{a['branch']['id']}",
        f"/v1/branches/{a['business']['id']}",
    ):
        resp = await client.get(path, headers=_auth(token_b))
        assert resp.status_code == 404, f"B read {path}: {resp.text}"


async def test_a_branch_cannot_be_created_under_another_business(client, db):
    """`business_id` in the payload is ignored, not honoured."""
    _, token_a = await _verified_owner(client, db, phone="0711000007")
    a = (await _create(client, token_a)).json()

    _, token_b = await _verified_owner(client, db, phone="0711000008")
    await _create(client, token_b, name="Brian's Braids")

    resp = await client.post(
        "/v1/branches",
        json={"name": "Injected Branch", "business_id": a["business"]["id"]},
        headers=_auth(token_b),
    )

    assert resp.status_code == 201, resp.text
    # It landed under B's own shop, not A's.
    assert resp.json()["business_id"] != a["business"]["id"]
    branches = (await db.scalars(select(Branch).where(Branch.name == "Injected Branch"))).all()
    assert len(branches) == 1
    assert branches[0].business_id != a["business"]["id"]


async def test_cross_business_branch_reads_are_not_found(client, db):
    _, token_a = await _verified_owner(client, db, phone="0711000009")
    a = (await _create(client, token_a)).json()
    _, token_b = await _verified_owner(client, db, phone="0711000010")
    b = (await _create(client, token_b)).json()

    resp = await client.get(f"/v1/branches/{a['branch']['id']}", headers=_auth(token_b))
    assert resp.status_code == 404, resp.text

    # And B cannot mutate it either.
    resp = await client.patch(
        f"/v1/branches/{a['branch']['id']}",
        json={"name": "Renamed By Intruder"},
        headers=_auth(token_b),
    )
    assert resp.status_code == 404, resp.text
    assert (await db.scalar(select(Branch).where(Branch.id == a["branch"]["id"]))).name == (
        "Westlands"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "A"},
        {"name": "x" * 161},
        {"branch_name": ""},
        {"branch_name": "y" * 121},
        {"business_type": "Barber Shop"},
        {"business_type": "other"},
        {"num_chairs": 0},
        {"num_chairs": 201},
        {"opens_at": "25:00"},
        {"county": "c" * 81},
        {"branch_phone": "0" * 21},
    ],
    ids=[
        "empty-name",
        "one-char-name",
        "long-name",
        "empty-branch-name",
        "long-branch-name",
        "display-form-type",
        "unmapped-other-type",
        "zero-chairs",
        "too-many-chairs",
        "impossible-hour",
        "long-county",
        "long-branch-phone",
    ],
)
async def test_invalid_input_is_rejected_and_writes_nothing(
    client, db, overrides
):
    _, token = await _verified_owner(client, db)

    resp = await _create(client, token, **overrides)

    assert resp.status_code == 422, f"{overrides} was accepted: {resp.text}"
    assert await _business_count(db) == 0
    assert await _branch_count(db) == 0


async def test_a_missing_required_field_is_rejected(client, db):
    _, token = await _verified_owner(client, db)

    body = _payload()
    del body["branch_name"]
    resp = await client.post("/v1/business", json=body, headers=_auth(token))

    assert resp.status_code == 422, resp.text
    assert await _business_count(db) == 0


async def test_a_second_owner_gets_their_own_tenant_not_the_first_owners(client, db):
    """Two shops, two independent onboarding runs, in one database."""
    _, token_a = await _verified_owner(client, db, phone="0711000011")
    _, token_b = await _verified_owner(client, db, phone="0711000012")

    a = (await _create(client, token_a, name="Amina Cuts", branch_name="Main")).json()
    b = (await _create(client, token_b, name="Brian's Braids", branch_name="Main")).json()

    assert a["business"]["id"] != b["business"]["id"]
    assert a["branch"]["id"] != b["branch"]["id"]
    # Same branch name, different businesses: the booking slug must still be
    # unique or one shop's public link would serve the other shop.
    assert a["branch"]["slug"] != b["branch"]["slug"]
    assert await _business_count(db) == 2
    assert await _branch_count(db) == 2