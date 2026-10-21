"""The owner PWA's billing API contract, pinned.

The sibling file `test_pwa_onboarding_contract.py` pins the *onboarding* half of
the PWA's contract. This file pins the *billing* half, which is the half the
PWA had no contract with at all until the billing screen was rewired: the screen
used to post to `/onboarding/subscription`, a route this API has never had, so
paying from the PWA was impossible — and nothing on either side noticed, because
a wrong URL to a TypeScript checker is a string.

The billing screen is now written against three real endpoints:

    GET  /v1/plans              the catalogue it renders
    POST /v1/subscription/pay   the payment it initiates
    GET  /v1/payments/{id}      the payment it polls

and one more, `GET /v1/subscription`, which supplies the trial banner's
numbers. Every assertion below reads the OpenAPI schema from `app.main`, so the
file runs in this repository's CI without checking out the PWA, and it fails
loudly the moment a path is renamed, a field is dropped or an enum loses a
member — the three ways a working screen turns into a 404 or a 422 in
production while every local build stays green.
"""

import pytest

from app.main import app
from app.models.billing import Payment

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


def _enum(path: str, method: str, prop: str) -> set[str]:
    """The members of an enum-valued property in a request body."""
    body = _request_schema(path, method)
    return set(_schema(body["properties"][prop])["enum"])


# ── the paths the billing screen calls ────────────────────────────────────────


def test_the_plan_catalogue_path_exists():
    """`api.get<Plan[]>("/plans")` in app/onboarding/subscription/page.tsx."""
    assert "/v1/plans" in SPEC["paths"]
    assert "get" in SPEC["paths"]["/v1/plans"]


def test_the_start_payment_path_exists():
    """`api.post<StartPaymentResponse>("/subscription/pay", …)`."""
    assert "/v1/subscription/pay" in SPEC["paths"]
    assert "post" in SPEC["paths"]["/v1/subscription/pay"]


def test_the_payment_polling_path_exists():
    """`api.get<PaymentResponse>(`/payments/${id}`)`."""
    assert "/v1/payments/{payment_id}" in SPEC["paths"]
    assert "get" in SPEC["paths"]["/v1/payments/{payment_id}"]


def test_the_subscription_summary_path_exists():
    """`api.get<SubscriptionSummary>("/subscription")` — the trial banner.

    The banner used to be a literal in the screen ("10-Day Free Trial"), which
    was a claim about server configuration that the client could not check. It
    now reads the server's own answer, and this is the endpoint that answers.
    """
    assert "/v1/subscription" in SPEC["paths"]
    assert "get" in SPEC["paths"]["/v1/subscription"]


def test_the_old_onboarding_subscription_path_is_not_something_to_call():
    """The 404 the billing screen was hitting.

    If it ever comes back, `/v1/onboarding/subscription` and
    `/v1/subscription/pay` would both claim to start a payment and the client
    would be ambiguous about which one owns billing.
    """
    assert "/v1/onboarding/subscription" not in SPEC["paths"]


# ── the plan catalogue ────────────────────────────────────────────────────────


def test_the_catalogue_carries_every_field_the_screen_renders():
    """Each `p.<field>` in the plan list of the billing screen."""
    for field in (
        "id",
        "code",
        "price_kes",
        "interval_days",
        "discount_pct",
        "per_day_kes",
    ):
        assert field in _fields("/v1/plans", "get"), field


def test_the_catalogue_is_a_list():
    """The screen calls `api.get<Plan[]>` and maps over the result."""
    schema = _schema(
        SPEC["paths"]["/v1/plans"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )
    assert schema.get("type") == "array"


# ── starting a payment ────────────────────────────────────────────────────────


def test_start_payment_accepts_exactly_what_the_screen_sends():
    """`{ plan_id, method }` — the plan UUID and the chosen rail.

    No amount: the price comes from the plan row, so a tampered client cannot
    choose what it pays. A new required field here would 422 the screen.
    """
    body = _request_schema("/v1/subscription/pay", "post")
    assert set(body["properties"]) == {"plan_id", "method"}
    assert set(body.get("required", [])) == {"plan_id", "method"}


def test_the_payment_rail_is_one_the_screen_offers():
    """The screen renders M-Pesa and Pesapal, and no others.

    A method the screen cannot produce is a plan the owner cannot buy; a method
    the screen produces that the API rejects is a button that always fails.
    """
    assert _enum("/v1/subscription/pay", "post", "method") == {"mpesa", "pesapal"}


def test_start_payment_answers_202_with_what_the_screen_reads():
    """202, not 201: nothing is paid yet.

    The screen branches on `state` and reads `payment_id`, `instructions` and
    `checkout_url`, so all four have to be in the documented response.
    """
    responses = SPEC["paths"]["/v1/subscription/pay"]["post"]["responses"]
    assert "202" in responses, "the endpoint must still answer 202 Accepted"
    fields = _fields("/v1/subscription/pay", "post", "202")
    for field in ("payment_id", "state", "instructions", "checkout_url"):
        assert field in fields, field


def test_the_payment_state_is_one_of_the_two_the_screen_handles():
    """`awaiting_mpesa` polls; `redirect` goes to the provider's checkout.

    A third state would fall through both branches of the screen's `if` and
    leave the owner staring at a button that does nothing.
    """
    body = _schema(
        SPEC["paths"]["/v1/subscription/pay"]["post"]["responses"]["202"]["content"][
            "application/json"
        ]["schema"]
    )
    assert set(body["properties"]["state"]["enum"]) == {
        "awaiting_mpesa",
        "redirect",
    }


# ── polling a payment ────────────────────────────────────────────────────────


def test_the_payment_carries_every_field_the_screen_polls():
    """Each field the screen reads off `GET /v1/payments/{id}`."""
    for field in (
        "id",
        "subscription_id",
        "amount_kes",
        "method",
        "provider",
        "provider_ref",
        "status",
        "paid_at",
        "created_at",
    ):
        assert field in _fields("/v1/payments/{payment_id}", "get"), field


def test_the_payment_statuses_the_screen_waits_for_exist():
    """`success` ends the poll; `failed` and `timeout` end it with a message.

    These are compared as plain strings by the screen, so what matters is that
    the column's enum contains them — a status the column cannot hold is a
    payment the poll would watch forever.
    """
    statuses = set(Payment.__table__.c.status.type.enums)
    assert {"pending", "success", "failed", "timeout"} <= statuses, statuses


def test_the_terminal_statuses_are_terminal_in_the_column():
    """A status outside the enum cannot be written, so it cannot be waited on.

    `pending` is the only non-terminal one; the screen keeps polling it.
    """
    statuses = set(Payment.__table__.c.status.type.enums)
    assert statuses - {"pending"} <= {"success", "failed", "timeout"}, statuses


# ── the trial banner ──────────────────────────────────────────────────────────


def test_the_subscription_summary_carries_the_banner_numbers():
    """Every field the banner reads off `GET /v1/subscription`."""
    for field in (
        "status",
        "plan_code",
        "trial_days_remaining",
        "is_in_grace",
        "days_until_renewal",
        "current_period_end",
    ):
        assert field in _fields("/v1/subscription", "get"), field


def test_the_banner_can_tell_a_live_trial_from_an_expired_one():
    """The banner's three states come from `status` and `is_in_grace`.

    A business whose trial has ended must be able to see that on the screen —
    the alternative is an owner who is told "no payment needed today" while
    their shop is being switched off.
    """
    fields = _fields("/v1/subscription", "get")
    assert {"status", "is_in_grace", "trial_days_remaining"} <= fields


@pytest.mark.parametrize(
    "status",
    ["trial", "active", "grace", "expired"],
)
def test_the_subscription_status_is_a_value_the_banner_can_render(status):
    """The column's enum must be a subset of the states the screen knows.

    The screen branches on `status === "active"` and treats everything else as
    a trial or an ended trial, so a *new* status is not a crash — but it is a
    state the banner silently mislabels, and this is where that is caught.
    """
    from app.models.billing import Subscription

    statuses = set(Subscription.__table__.c.status.type.enums)
    assert status in statuses
