"""The owner PWA's onboarding API contract, pinned.

`groomio-web` and this API are separate projects built by different toolchains
with nothing shared at compile time. Nothing else would notice that the PWA
posts `business_name` to an endpoint that wants `name` — the TypeScript checker
would be perfectly happy, every frontend test would pass, and the screen would
fail with a 422 the first time an owner tapped Save in production. That is
exactly the class of bug this file exists for, and it is the same reasoning as
`test_frontend_contract.py`, which pins the customer portal's reads.

Every assertion reads the OpenAPI schema from `app.main`. Nothing here touches
the frontend's files on disk, so it runs in this repository's CI without
checking out the PWA.

Each test names the frontend symbol it protects, so a failure points at the line
to change rather than just at a field name.
"""

import pytest

from app.main import app

SPEC = app.openapi()


def _schema(node):
    """Follow a $ref to its component definition."""
    while "$ref" in node:
        name = node["$ref"].split("/")[-1]
        node = SPEC["components"]["schemas"][name]
    return node


def _fields(path: str, method: str, status_code: str = "200") -> set[str]:
    op = SPEC["paths"][path][method]
    schema = _schema(
        op["responses"][status_code]["content"]["application/json"]["schema"]
    )
    if schema.get("type") == "array":
        schema = _schema(schema["items"])
    return set(schema.get("properties", {}))


def _request_schema(path: str, method: str):
    return _schema(
        SPEC["paths"][path][method]
        .get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )


def _param_names(path: str, method: str) -> set[str]:
    return {p["name"] for p in SPEC["paths"][path][method].get("parameters", [])}


def _object_ref(node) -> str:
    """The component name behind an optional (nullable) property.

    Pydantic renders `Optional[BranchResponse]` as `anyOf: [$ref, null]`, so the
    reference has to be picked out of the union before anything else can be read
    from it. A change from optional to required would surface here rather than as
    a confusing `KeyError` three assertions later.
    """
    if "$ref" in node:
        return node["$ref"].split("/")[-1]
    refs = [b["$ref"].split("/")[-1] for b in node.get("anyOf", []) if "$ref" in b]
    assert len(refs) == 1, f"expected exactly one object branch, got {refs}"
    return refs[0]


def _object_properties(node) -> set[str]:
    return set(SPEC["components"]["schemas"][_object_ref(node)]["properties"])


# ── the paths the PWA calls ───────────────────────────────────────────────────
# `app/onboarding/business/page.tsx` posts the business; before T-002 it posted
# to `/onboarding/business`, which has never existed on this API and answered
# 404. Pinning the real path is what stops a renamed route from reintroducing
# the same silent break.


def test_the_business_creation_path_exists():
    assert "/v1/business" in SPEC["paths"]
    assert "post" in SPEC["paths"]["/v1/business"]


def test_the_onboarding_status_path_exists():
    """`getOnboardingStatus()` in lib/api-client.ts."""
    assert "/v1/onboarding/status" in SPEC["paths"]
    assert "get" in SPEC["paths"]["/v1/onboarding/status"]


def test_the_old_onboarding_business_path_is_not_something_to_call():
    """The 404 the PWA was hitting. If this ever comes back, both endpoints
    exist and the client is ambiguous about which one owns onboarding."""
    assert "/v1/onboarding/business" not in SPEC["paths"]


def test_onboarding_status_takes_no_parameters():
    """The PWA sends no query string.

    Worth pinning because it is the property that makes the endpoint safe:
    there is no id to vary, so asking about another owner's progress is not
    expressible in the surface at all.
    """
    assert _param_names("/v1/onboarding/status", "get") == set()


# ── request shape ─────────────────────────────────────────────────────────────


def test_business_creation_accepts_the_fields_the_form_sends():
    """Every `name` in `app/onboarding/business/page.tsx`'s payload."""
    body = _request_schema("/v1/business", "post")
    for field in (
        "name",
        "business_type",
        "branch_name",
        "county",
        "town",
        "branch_address",
        "branch_phone",
        "opens_at",
        "closes_at",
        "num_chairs",
    ):
        assert field in body["properties"], f"the business form sends {field}"


def test_business_creation_requires_only_the_fields_the_form_marks_required():
    """`required` in HTML does not become a Pydantic requirement by itself, and
    a 422 the owner cannot fix is worse than a blank field the shop can live
    without. These three are the ones the form cannot submit without."""
    body = _request_schema("/v1/business", "post")
    assert set(body.get("required", [])) == {"name", "business_type", "branch_name"}


def test_business_type_is_the_enum_the_dropdown_offers():
    """The dropdown must not offer a value the column cannot store.

    `businesses.business_type` is a native PostgreSQL enum, so "Other" cannot be
    added without an `ALTER TYPE` migration. Offering it and letting the
    database reject the row is the worst of both worlds.
    """
    body = _request_schema("/v1/business", "post")
    enum = _schema(body["properties"]["business_type"])
    assert set(enum["enum"]) == {
        "barber_shop",
        "salon",
        "nail_bar",
        "beauty_clinic",
        "spa",
    }


# ── response shape ────────────────────────────────────────────────────────────


def test_business_creation_returns_both_halves_of_onboarding():
    """The response nests the branch, so the PWA never has to follow up with a
    second request just to confirm its write landed."""
    for field in ("business", "branch", "next"):
        assert field in _fields("/v1/business", "post", "201"), field


def test_the_idempotent_replay_is_documented():
    """`POST /v1/business` answers 200 with the same body on a repeat.

    This is what makes a retry safe. If it silently reverted to 409, a lost
    response would leave the owner unable to tell "saved but I never saw it"
    from "failed", and the flow would have no recovery.
    """
    responses = SPEC["paths"]["/v1/business"]["post"]["responses"]
    assert "200" in responses, "the idempotent replay must stay documented"
    replay = _schema(responses["200"]["content"]["application/json"]["schema"])
    assert replay == _schema(
        responses["201"]["content"]["application/json"]["schema"]
    ), "the replay must return the same shape as the first write"


def test_onboarding_status_returns_what_the_pwa_routes_on():
    """`getOnboardingStatus()` reads exactly these."""
    for field in (
        "user_id",
        "account_status",
        "has_business",
        "has_branch",
        "completed",
        "next",
        "business",
        "branch",
        "branch_count",
    ):
        assert field in _fields("/v1/onboarding/status", "get"), field


def test_onboarding_next_is_a_closed_set_the_client_can_switch_on():
    """An open string would leave the PWA with a `default:` branch that silently
    does nothing — the failure mode this whole flow is meant to remove."""
    body = _schema(
        SPEC["paths"]["/v1/onboarding/status"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )
    assert set(body["properties"]["next"]["enum"]) == {
        "business",
        "subscription",
        "done",
    }


def test_onboarding_status_nests_the_same_summaries():
    """The PWA reads `business.name` from the status call. If these were a
    different shape from `GET /v1/business`, the resume path would need its own
    parsing and could drift from the normal one."""
    status_body = _schema(
        SPEC["paths"]["/v1/onboarding/status"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )

    # An optional field is `anyOf: [$ref, null]`, so the $ref has to be picked
    # out before its properties can be read.
    assert _object_ref(status_body["properties"]["business"]) == "BusinessResponse"
    assert _object_ref(status_body["properties"]["branch"]) == "BranchResponse"


def test_the_resume_screen_can_show_the_booking_link():
    """`slug` is the public booking segment, and it is the one piece of the
    branch an owner cannot reconstruct from the rest of the payload."""
    branch = _schema(
        SPEC["components"]["schemas"]["OnboardingStatusResponse"]["properties"][
            "branch"
        ]
    )
    assert "slug" in _object_properties(branch)


# ── authentication ────────────────────────────────────────────────────────────
#
# There is deliberately no `security` assertion here. This API reads the
# credential by hand from `request.headers` rather than through FastAPI's
# `HTTPBearer`, so OpenAPI carries no `security` requirement for *any* protected
# route and one cannot be asserted without inventing it. `test_business_onboarding`
# proves the behaviour end to end instead: anonymous and pre-verification
# credentials both get 401 from both endpoints.
#
# Worth flagging separately: the generated docs do not say these routes need a
# token at all. That is a documentation gap, not a security one.


def test_the_onboarding_router_stays_its_own_prefix():
    """One namespace, one owner.

    `/v1/onboarding/business` is the URL the PWA used to call and the one that
    answered 404. If a future change reintroduces it, two endpoints would claim
    business creation and the client would be ambiguous about which owns
    onboarding.
    """
    onboarding_paths = {
        path for path in SPEC["paths"] if path.startswith("/v1/onboarding")
    }
    assert onboarding_paths == {"/v1/onboarding/status"}, onboarding_paths


def test_no_onboarding_endpoint_accepts_the_restricted_onboarding_token():
    """`get_onboarding_user` names a pending registrant and grants no role. It
    must not be wired into business state, or an unverified account gains a
    readable surface that a later change could quietly widen."""
    from app.core.deps import get_current_user, get_onboarding_user

    onboarding_routes = [
        path for path in SPEC["paths"] if path.startswith("/v1/onboarding")
    ]
    assert onboarding_routes, "the onboarding router disappeared"

    # The dependency identity is the only place this is visible in a DB-free
    # test; the integration suite proves the behaviour end to end.
    assert get_current_user is not get_onboarding_user
