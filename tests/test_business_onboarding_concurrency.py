"""T-003 — real PostgreSQL concurrency tests for `POST /v1/business`.

The defect these exist to prevent
--------------------------------
`POST /v1/business` read "does this owner already have a business?" and then
inserted one. Those two statements are not atomic. Two concurrent requests for
the same owner — a double-tapped button, two tabs, a lost response and a retry —
both read "no business", both inserted, and both returned **201**. The owner ended
up owning two tenants, each with its own branch and its own trial subscription.

Two things make that worth a dedicated suite rather than another assertion in
`test_business_onboarding.py`:

* **The existing fixture cannot see the bug.** `tests/conftest.py::db` binds one
  connection inside one transaction that is always rolled back. Two "simultaneous"
  requests through it share a single session on a single connection: they cannot
  interleave, they cannot commit independently, and one rollback discards both.
  A concurrency test written against it passes while the defect is live — a false
  green, which is worse than a red test. Every test here uses `concurrency_client`
  instead: a real pool, a session per request, real commits.

* **The bug hides behind an accident.** `branches.slug` is unique, so two
  concurrent requests carrying the *same* branch name usually killed the loser with
  an `IntegrityError` that escaped as an unexplained **500** — which looks like a
  different bug and is not one. Change the branch name, or use two tabs, and the
  mask is gone. Tests 1 and 2 deliberately use *different* branch names, because
  that is the variant that produced two businesses.

How the overlap is forced (not hoped for)
------------------------------------------
`asyncio.gather` starts the requests together, but "started together" is not the
same as "overlapped" — the ASGI app runs on one event loop, so without a real
blocker the requests can complete one after another and prove nothing.

So the overlap is forced at the database, not with sleeps:

* `test_requests_genuinely_block_on_the_owner_lock` takes the owner row lock in a
  separate session and asserts that a request launched behind it is still
  unfinished. That proves the serialisation point is real and that these requests
  really do interleave in the database.
* Every other test holds that same gate and releases it with all requests
  already parked on it, so they resume together against the winner.

One deliberate `asyncio.sleep` appears in that one test, and it is used to
*observe* blocking, never to manufacture an interleaving that might not occur.

Assertions are about the **database**, not the response body. A 201 that silently
created a second `businesses` row would satisfy every response-shape assertion
and still be an incident, so each test counts rows afterwards.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.security import create_access_token
from app.models.billing import Plan, Subscription
from app.models.identity import User
from app.models.tenant import Branch, Business


def _at(days_from_now: int) -> datetime:
    """A timezone-aware timestamp. asyncpg rejects raw ISO strings for
    `TIMESTAMPTZ` columns, so every datetime written here is a real datetime."""
    return datetime.now(timezone.utc) + timedelta(days=days_from_now)


# ── helpers ────────────────────────────────────────────────────────────────────


def _payload(**overrides) -> dict:
    """A valid business-creation body. Overrides let a test vary one field."""
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


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


#: How many event-loop turns to give the requests before releasing the gate.
#: Far more than needed to reach the lock, and cheap — it exists so the number
#: never has to be tuned to machine speed, not because any specific count is
#: required. `test_requests_genuinely_block_on_the_owner_lock` is the test that
#: proves the requests really are parked when the gate opens.
_GATE_YIELDS = 50


async def _seed_plans(sessions) -> None:
    """Seed the cheapest plan `POST /v1/business` opens a trial against.

    The test schema is built with `Base.metadata.create_all`, which creates
    tables but runs none of the seed migrations — so without this the trial
    subscription is silently never created and every "exactly one subscription"
    assertion below would pass for the wrong reason.

    The prices are read from migration 0003 rather than copied here, so this
    cannot drift from the catalogue the app actually ships.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "0003_seed_plans.py"
    )
    spec = importlib.util.spec_from_file_location("_t003_seed_plans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async with sessions() as s:
        for code, price, days, discount in module.PLANS:
            s.add(
                Plan(
                    code=code,
                    price_kes=price,
                    interval_days=days,
                    discount_pct=discount,
                    is_active=True,
                )
            )
        await s.commit()


async def _make_owner(sessions, phone: str, *, name: str = "Amina Wanjiru"):
    """An active owner with a real access token. Returns `(user_id, token)`.

    Inserted directly rather than through `/auth/register` because these tests
    need their owners to exist *before* the concurrent window opens; walking
    signup would put non-concurrent traffic inside the measurement. The token is
    minted by the same helper the auth router uses, with the `role` claim that
    `require_role("owner")` reads.
    """
    async with sessions() as s:
        user = User(
            full_name=name,
            phone=phone,
            email=f"{phone}@example.com",
            password_hash="not-used-in-these-tests",
            role="owner",
            status="active",
        )
        s.add(user)
        await s.commit()
        user_id = str(user.id)
    return user_id, create_access_token(user_id, {"role": "owner"})


async def _counts(sessions, owner_user_id: str) -> tuple[int, int, int]:
    """`(businesses, branches, subscriptions)` actually committed for an owner."""
    async with sessions() as s:
        businesses = await s.scalar(
            select(func.count())
            .select_from(Business)
            .where(Business.owner_user_id == owner_user_id)
        )
        biz_ids = await s.scalars(
            select(Business.id).where(Business.owner_user_id == owner_user_id)
        )
        ids = [str(b) for b in biz_ids]
        branches = (
            await s.scalar(
                select(func.count())
                .select_from(Branch)
                .where(Branch.business_id.in_(ids))
            )
            if ids
            else 0
        )
        subs = (
            await s.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.business_id.in_(ids))
            )
            if ids
            else 0
        )
    return businesses or 0, branches or 0, subs or 0


async def _branches_for(sessions, owner_user_id: str) -> list[tuple[str, str]]:
    """`(branch_id, slug)` committed for an owner, oldest first.

    Two queries rather than a join: `businesses.id` is a UUID while
    `branches.business_id` is a VARCHAR, so joining them directly is a type
    error in PostgreSQL. The application's own code sidesteps this by casting
    the business id to `str` everywhere, and so does this.
    """
    async with sessions() as s:
        ids = [
            str(b)
            for b in await s.scalars(
                select(Business.id).where(Business.owner_user_id == owner_user_id)
            )
        ]
        if not ids:
            return []
        rows = await s.execute(
            select(Branch.id, Branch.slug)
            .where(Branch.business_id.in_(ids))
            .order_by(Branch.created_at, Branch.id)
        )
        return [(str(r[0]), r[1]) for r in rows]


async def _open_gate(sessions, owner_user_id: str):
    """Take the owner's user-row lock in a dedicated session and keep it.

    This is the lever the whole suite pulls. `POST /v1/business` serialises
    concurrent submissions on exactly this row, so holding it parks every request
    at the same point and releases them together once the transaction ends.
    Returns the open session — the caller must close it.
    """
    s = sessions()
    await s.execute(
        text("SELECT 1 FROM users WHERE id = :uid FOR UPDATE"),
        {"uid": owner_user_id},
    )
    return s


async def _concurrently_open(client, gate_session, calls: list[tuple[str, dict]]):
    """Run every request against a held gate, releasing them together.

    Requests are created first, then the event loop is given enough turns for
    all of them to reach the row lock and park there. Only then is the gate
    released. PostgreSQL grants row locks in queue order, so each released
    request resumes, commits, and hands the next one a business it can already
    see — which is exactly the sequence the fix depends on.

    The turns are `sleep(0)` yields, not wall-clock sleeps: they let the tasks
    run to their first real await (the lock) without inventing timing, so the
    test cannot pass or fail based on how fast the machine is.
    """
    tasks = [asyncio.create_task(client.post(path, **kwargs)) for path, kwargs in calls]

    for _ in range(_GATE_YIELDS):
        await asyncio.sleep(0)

    await gate_session.rollback()
    return await asyncio.gather(*tasks)


# ── the overlap is real ────────────────────────────────────────────────────────


async def test_requests_genuinely_block_on_the_owner_lock(concurrency_client, sessions):
    """Proves the fixture really expresses concurrency — and the lock is real.

    Without this, every other test in this file could pass for the wrong reason:
    if requests did not actually overlap, "one business, one 201, the rest 200"
    would be produced by them running one after another, which the pre-fix code
    also does. The defect is specifically about overlap.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000001")

    gate = await _open_gate(sessions, owner_id)

    pending = asyncio.create_task(
        concurrency_client.post("/v1/business", json=_payload(), headers=_auth(token))
    )

    # The request must still be unfinished while we hold the owner's row. This
    # sleep is an observation window, not a mechanism for producing a race: it
    # asserts that blocking happened, and nothing else depends on its length.
    await asyncio.sleep(0.5)
    assert not pending.done(), (
        "request completed while another transaction held SELECT ... FOR UPDATE on "
        "the owner's user row — so these requests are NOT overlapping and the rest "
        "of this suite would be proving nothing"
    )

    await gate.rollback()
    response = await pending
    await gate.close()

    assert response.status_code == 201, response.text
    assert await _counts(sessions, owner_id) == (1, 1, 1)


# ── the regression itself ──────────────────────────────────────────────────────


async def test_two_simultaneous_requests_different_branch_names(
    concurrency_client, sessions
):
    """T-003 C2/C6 — the regression. Different branch names unmask the bug.

    Two owners' worth of requests that each carry a *different* branch name,
    which is what stops the unique `branches.slug` index from incidentally
    killing the loser. Before the fix this produced two businesses, two
    branches, two subscriptions, and two 201s.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000002")
    gate = await _open_gate(sessions, owner_id)

    try:
        responses = await _concurrently_open(
            concurrency_client,
            gate,
            [
                (
                    "/v1/business",
                    {
                        "json": _payload(branch_name="Westlands"),
                        "headers": _auth(token),
                    },
                ),
                (
                    "/v1/business",
                    {
                        "json": _payload(branch_name="Kilimani"),
                        "headers": _auth(token),
                    },
                ),
            ],
        )
    finally:
        await gate.close()

    codes = sorted(r.status_code for r in responses)
    assert all(r.status_code < 500 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    # Exactly one creation (201) and one replay (200) — never two 201s.
    assert codes == [200, 201], [r.status_code for r in responses]

    assert await _counts(sessions, owner_id) == (1, 1, 1)

    bodies = [r.json() for r in responses]
    assert bodies[0]["business"]["id"] == bodies[1]["business"]["id"]


async def test_two_simultaneous_requests_identical_branch_names(
    concurrency_client, sessions
):
    """Same branch name — the variant that previously surfaced as a 500.

    The slug unique index used to reject the losing request with an unhandled
    `IntegrityError`, producing an opaque server error. A slug collision is an
    ordinary event and must resolve to a normal 2xx replay.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000003")
    gate = await _open_gate(sessions, owner_id)

    try:
        responses = await _concurrently_open(
            concurrency_client,
            gate,
            [
                ("/v1/business", {"json": _payload(), "headers": _auth(token)}),
                ("/v1/business", {"json": _payload(), "headers": _auth(token)}),
            ],
        )
    finally:
        await gate.close()

    assert all(r.status_code < 500 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    assert sorted(r.status_code for r in responses) == [200, 201]
    assert await _counts(sessions, owner_id) == (1, 1, 1)


async def test_five_simultaneous_requests_one_owner(concurrency_client, sessions):
    """Five at once, each with its own branch name.

    Two requests can be argued about as a narrow window; five is the realistic
    shape of a client that retries aggressively, and it is what produced two
    businesses before the fix.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000004")
    gate = await _open_gate(sessions, owner_id)

    calls = [
        (
            "/v1/business",
            {
                "json": _payload(branch_name=f"Branch {i}"),
                "headers": _auth(token),
            },
        )
        for i in range(5)
    ]
    try:
        responses = await _concurrently_open(concurrency_client, gate, calls)
    finally:
        await gate.close()

    assert all(r.status_code < 500 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    codes = [r.status_code for r in responses]
    assert codes.count(201) == 1, codes
    assert codes.count(200) == 4, codes
    assert await _counts(sessions, owner_id) == (1, 1, 1)


# ── sequential and replay behaviour (the T-002 contract must not regress) ──────


async def test_sequential_retries_after_success(concurrency_client, sessions):
    """201 then 200s, and the first branch is never rewritten.

    Retries must not silently rename or re-slug an existing branch — the
    original booking link has to keep working.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000005")

    first = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token)
    )
    assert first.status_code == 201, first.text
    original = await _branches_for(sessions, owner_id)
    assert len(original) == 1

    for _ in range(4):
        again = await concurrency_client.post(
            "/v1/business",
            json=_payload(branch_name="Somewhere Else Entirely"),
            headers=_auth(token),
        )
        assert again.status_code == 200, again.text

    assert await _counts(sessions, owner_id) == (1, 1, 1)
    # Same branch row, same slug — a replay must not change it.
    assert await _branches_for(sessions, owner_id) == original


async def test_lost_response_after_commit_then_retry(concurrency_client, sessions):
    """The client never saw the 201; the retry must return the same business.

    This is the scenario the whole idempotent-onboarding contract exists for,
    and it is indistinguishable from a slow network.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000006")

    lost = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token)
    )
    assert lost.status_code == 201
    # ...the response is dropped here; the owner taps submit again.
    retried = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token)
    )

    assert retried.status_code == 200, retried.text
    assert retried.json()["business"]["id"] == lost.json()["business"]["id"]
    assert retried.json()["branch"]["id"] == lost.json()["branch"]["id"]
    assert await _counts(sessions, owner_id) == (1, 1, 1)


async def test_business_exists_but_first_branch_missing(concurrency_client, sessions):
    """A half-finished onboarding is completed under the *same* business.

    Creating the missing branch must not be used as a reason to create a second
    tenant.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000007")

    created = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token)
    )
    assert created.status_code == 201
    business_id = created.json()["business"]["id"]

    async with sessions() as s:
        await s.execute(
            text("DELETE FROM branches WHERE business_id = :bid"), {"bid": business_id}
        )
        await s.commit()

    assert await _counts(sessions, owner_id) == (1, 0, 1)

    repaired = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token)
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["business"]["id"] == business_id
    assert await _counts(sessions, owner_id) == (1, 1, 1)


async def test_retries_never_create_a_second_trial(concurrency_client, sessions):
    """Exactly one subscription row, and its period is not extended by retries.

    The trial is opened once at onboarding. A retry must not restart the clock —
    an owner who keeps retrying must not silently gain trial time.
    """
    await _seed_plans(sessions)
    owner_id, token = await _make_owner(sessions, "0711000008")

    created = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token)
    )
    business_id = created.json()["business"]["id"]

    async def _trial_end():
        async with sessions() as s:
            return await s.scalar(
                select(Subscription.current_period_end).where(
                    Subscription.business_id == business_id
                )
            )

    original_end = await _trial_end()
    assert original_end is not None, "no trial was opened at onboarding"

    gate = await _open_gate(sessions, owner_id)
    try:
        responses = await _concurrently_open(
            concurrency_client,
            gate,
            [
                ("/v1/business", {"json": _payload(), "headers": _auth(token)}),
                ("/v1/business", {"json": _payload(), "headers": _auth(token)}),
                ("/v1/business", {"json": _payload(), "headers": _auth(token)}),
            ],
        )
    finally:
        await gate.close()

    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    assert await _counts(sessions, owner_id) == (1, 1, 1)
    assert await _trial_end() == original_end


async def test_two_different_owners_creating_simultaneously(
    concurrency_client, sessions
):
    """Control case. Different owners must both succeed, fully in parallel.

    If the serialisation were accidentally global rather than per-owner, this
    would still pass on status codes — so it also asserts that the two shops got
    genuinely separate businesses, branches and slugs.
    """
    await _seed_plans(sessions)

    async def _go(phone):
        owner, token = await _make_owner(sessions, phone)
        return owner, await concurrency_client.post(
            "/v1/business", json=_payload(), headers=_auth(token)
        )

    results = await asyncio.gather(
        _go("0711000009"),
        _go("0711000010"),
        _go("0711000011"),
    )

    assert [r.status_code for _, r in results] == [201, 201, 201]
    owners = [owner for owner, _ in results]
    assert len(set(owners)) == 3, "a request was issued for the wrong owner"

    ids = {r.json()["business"]["id"] for _, r in results}
    slugs = {r.json()["branch"]["slug"] for _, r in results}
    assert len(ids) == 3, "two owners were given the same business id"
    # All three submitted the same branch name, so the slug de-duplication is
    # what keeps the public booking links distinct.
    assert len(slugs) == 3, slugs

    for owner, response in results:
        assert await _counts(sessions, owner) == (1, 1, 1)


# ── the database is the guarantee, not just the application ────────────────────


async def test_unique_constraint_rejects_duplicate_ownership(sessions):
    """Raw SQL, no application involved. The database refuses the second row.

    This is the assertion that survives application code being rewritten,
    bypassed, or replaced by a background job. It proves `uq_businesses_
    owner_user_id` exists in the schema the tests run against.
    """
    await _seed_plans(sessions)
    owner_id, _ = await _make_owner(sessions, "0711000014")

    async with sessions() as s:
        s.add(
            Business(
                owner_user_id=owner_id,
                name="First",
                business_type="barber_shop",
                status="trialing",
            )
        )
        await s.commit()

    async with sessions() as s:
        s.add(
            Business(
                owner_user_id=owner_id,
                name="Second",
                business_type="barber_shop",
                status="trialing",
            )
        )
        with pytest.raises(IntegrityError) as caught:
            await s.commit()
        await s.rollback()

    assert getattr(caught.value.orig, "sqlstate", None) == "23505"

    async with sessions() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(Business)
            .where(Business.owner_user_id == owner_id)
        )
    assert count == 1


async def test_unique_constraint_rejects_two_live_subscriptions(sessions):
    """The subscription index is partial — and still blocks a duplicate trial.

    Also asserts the *other half* of the design: an `expired` row alongside a
    live one is legitimate history and must still be accepted, which is why this
    index is partial rather than blanket.
    """
    await _seed_plans(sessions)
    owner_id, _ = await _make_owner(sessions, "0711000015")

    async with sessions() as s:
        biz = Business(
            owner_user_id=owner_id,
            name="Second Shop",
            business_type="barber_shop",
            status="trialing",
        )
        plan_id = str(await s.scalar(select(Plan.id).where(Plan.code == "daily")))
        s.add(biz)
        await s.flush()
        business_id = str(biz.id)
        s.add(
            Subscription(
                business_id=business_id,
                plan_id=plan_id,
                status="trial",
                current_period_start=_at(-30),
                current_period_end=_at(1),
            )
        )
        await s.commit()

    # A second *live* subscription for the same business is refused.
    async with sessions() as s:
        plan_id = str(await s.scalar(select(Plan.id).where(Plan.code == "daily")))
        s.add(
            Subscription(
                business_id=business_id,
                plan_id=plan_id,
                status="active",
                current_period_start=_at(1),
                current_period_end=_at(31),
            )
        )
        with pytest.raises(IntegrityError) as caught:
            await s.commit()
        await s.rollback()
    assert getattr(caught.value.orig, "sqlstate", None) == "23505"

    # An *expired* row alongside the live one is accepted: billing history.
    async with sessions() as s:
        plan_id = str(await s.scalar(select(Plan.id).where(Plan.code == "daily")))
        s.add(
            Subscription(
                business_id=business_id,
                plan_id=plan_id,
                status="expired",
                current_period_start=_at(-400),
                current_period_end=_at(-370),
            )
        )
        await s.commit()


async def test_expected_conflict_does_not_poison_the_transaction(sessions):
    """A handled uniqueness conflict leaves the session usable.

    The savepoint exists precisely so that recovering from an expected duplicate
    does not discard the caller's outer transaction. This proves that directly:
    after a rolled-back savepoint the same session must still be able to commit
    unrelated work. If the savepoint were missing, the transaction would be
    aborted and this commit would fail.
    """
    await _seed_plans(sessions)
    owner_id, _ = await _make_owner(sessions, "0711000016")
    # Created up front, on its own connection: the point of this test is that
    # the *outer* transaction stays usable after the savepoint rollback, and
    # opening a second write connection from inside it would muddy that.
    other_owner_id, _ = await _make_owner(sessions, "0711000099")

    async with sessions() as s:
        s.add(
            Business(
                owner_user_id=owner_id,
                name="Original",
                business_type="barber_shop",
                status="trialing",
            )
        )
        await s.commit()

    async with sessions() as s:
        savepoint = await s.begin_nested()
        s.add(
            Business(
                owner_user_id=owner_id,
                name="Duplicate",
                business_type="barber_shop",
                status="trialing",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()
        await savepoint.rollback()

        # The outer transaction survived, so further writes must still work.
        # A *different* owner is essential here: writing a second business for
        # the same owner would hit the very index we just recovered from.
        plan_id = str(await s.scalar(select(Plan.id).where(Plan.code == "daily")))
        biz = Business(
            owner_user_id=other_owner_id,
            name="A different owner entirely",
            business_type="barber_shop",
            status="trialing",
        )
        s.add(biz)
        await s.flush()
        s.add(
            Subscription(
                business_id=str(biz.id),
                plan_id=plan_id,
                status="trial",
                current_period_start=_at(0),
                current_period_end=_at(30),
            )
        )
        await s.commit()

    async with sessions() as s:
        original_count = await s.scalar(
            select(func.count())
            .select_from(Business)
            .where(Business.owner_user_id == owner_id)
        )
        recovered_count = await s.scalar(
            select(func.count())
            .select_from(Business)
            .where(Business.owner_user_id == other_owner_id)
        )
    # The rejected duplicate was discarded by the savepoint, not kept; and the
    # work done after recovery landed normally.
    assert original_count == 1
    assert recovered_count == 1


# ── nothing else was weakened ──────────────────────────────────────────────────


async def test_owner_cannot_claim_another_owners_business(concurrency_client, sessions):
    """Posting someone else's ids changes nothing.

    Ownership is derived from the token, never from the payload. The uniqueness
    fix must not have introduced a way to write an `owner_user_id` of one's
    choosing.
    """
    await _seed_plans(sessions)
    owner_a, token_a = await _make_owner(sessions, "0711000017", name="Owner A")
    owner_b, token_b = await _make_owner(sessions, "0711000018", name="Owner B")

    created = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token_a)
    )
    assert created.status_code == 201
    business_id = created.json()["business"]["id"]

    # Owner B submits a payload naming A's business and A as the owner.
    hijack = await concurrency_client.post(
        "/v1/business",
        json=_payload(owner_user_id=owner_a, business_id=business_id),
        headers=_auth(token_b),
    )
    assert hijack.status_code == 201, hijack.text

    async with sessions() as s:
        owner_of_a = await s.scalar(
            select(Business.owner_user_id).where(Business.id == business_id)
        )
        b_business = await s.scalar(
            select(Business.id).where(Business.owner_user_id == owner_b)
        )

    assert owner_of_a == owner_a, "ownership was reassigned by the payload"
    assert str(b_business) != business_id


async def test_cross_tenant_isolation_survives(concurrency_client, sessions):
    """Tenant B still gets 404 on tenant A's business and branch, not 403.

    A correctness fix that quietly widened a query would pass every test above
    and leak tenants. This is the guard against that.
    """
    await _seed_plans(sessions)
    owner_a, token_a = await _make_owner(sessions, "0711000019", name="Owner A")
    owner_b, token_b = await _make_owner(sessions, "0711000020", name="Owner B")

    a_business = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token_a)
    )
    assert a_business.status_code == 201
    a_branch_id = a_business.json()["branch"]["id"]

    b_business = await concurrency_client.post(
        "/v1/business", json=_payload(), headers=_auth(token_b)
    )
    assert b_business.status_code == 201
    assert b_business.json()["business"]["id"] != a_business.json()["business"]["id"]

    # B may not read or write A's shop through the owner-scoped endpoints.
    assert (
        await concurrency_client.get("/v1/business", headers=_auth(token_b))
    ).json()["id"] == b_business.json()["business"]["id"]

    reading_a_branch = await concurrency_client.get(
        f"/v1/branches/{a_branch_id}", headers=_auth(token_b)
    )
    assert reading_a_branch.status_code == 404, reading_a_branch.text

    writing_a_branch = await concurrency_client.patch(
        f"/v1/branches/{a_branch_id}",
        json={"name": "Stolen"},
        headers=_auth(token_b),
    )
    assert writing_a_branch.status_code == 404, writing_a_branch.text

    async with sessions() as s:
        name = await s.scalar(select(Branch.name).where(Branch.id == a_branch_id))
    assert name == "Westlands", "a cross-tenant write went through"


# Explicit marker so `pytest --collect-only` on this file alone is obvious.
pytestmark = pytest.mark.asyncio
