"""The customer portal's API contract, pinned.

These are cheap, database-free tests whose only job is to fail the moment
`groomio-customer/lib/api.ts` and the API disagree. The two are separate
repositories, built by different toolchains, and share nothing at build time, so
nothing else would catch a renamed field — a `staff_id` that became `staffId`
would typecheck perfectly on the frontend, pass every frontend test, and then
render an empty barber list in production.

Every assertion here reads the OpenAPI schema from `app.main` and compares it to
field names written out by hand. Nothing in this file touches the frontend's
files on disk, which is precisely what lets it live in the API repository and
run in its CI without checking out the portal. When the two disagree, the fix
belongs in both repositories in the same pull request.

Each test names the frontend symbol it is protecting, so a failure points at the
line to change rather than just at a field name.
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


def _params(path: str, method: str) -> dict[str, bool]:
    return {
        p["name"]: bool(p.get("required"))
        for p in SPEC["paths"][path][method].get("parameters", [])
    }


# ── the four reads the funnel needs before it can book anyone ────────────────
def test_getBranch_contract():
    """lib/api.ts -> `Branch`."""
    assert _fields("/v1/public/{branch_slug}", "get") == {
        "slug",
        "name",
        "town",
        "county",
        "address",
        "phone",
        "latitude",
        "longitude",
        "price_tier",
        "opens_at",
        "closes_at",
        "num_chairs",
        "business_name",
    }


def test_getServices_contract():
    """lib/api.ts -> `Service`."""
    assert _fields("/v1/public/{branch_slug}/services", "get") == {
        "id",
        "name",
        "price_kes",
        "duration_min",
    }


def test_getBarbers_contract():
    """lib/api.ts -> `Barber`. Note `staff_id`, not `id`.

    The barber's *staff* id is what booking submits; a shop can have several
    staff rows and the public list must not expose internal user ids.
    """
    assert _fields("/v1/public/{branch_slug}/staff", "get") == {
        "staff_id",
        "full_name",
        "photo_url",
    }


def test_getSlots_contract():
    """lib/api.ts -> `SlotDay`."""
    fields = _fields("/v1/public/{branch_slug}/slots", "get")
    assert fields == {"staff_id", "date", "duration_min", "total_kes", "slots"}

    # The nested Slot shape, which the grid of time buttons renders.
    slots = _schema(
        SPEC["paths"]["/v1/public/{branch_slug}/slots"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
    )
    nested = _schema(slots["properties"]["slots"]["items"])
    assert set(nested["properties"]) == {
        "scheduled_at",
        "duration_min",
        "total_kes",
        "is_earliest",
    }


def test_bookAppointment_contract():
    """lib/api.ts -> `Booking`, rendered on the confirmation screen."""
    fields = _fields("/v1/public/{branch_slug}/appointments", "post", "201")
    for required in (
        "id",
        "scheduled_at",
        "staff_name",
        "customer_name",
        "total_kes",
        "lines",
        "status",
    ):
        assert required in fields, f"confirmation screen needs {required}"

    lines = _schema(
        SPEC["paths"]["/v1/public/{branch_slug}/appointments"]["post"]["responses"][
            "201"
        ]["content"]["application/json"]["schema"]
    )
    line = _schema(_schema(lines["properties"]["lines"])["items"])
    assert set(line["properties"]) == {
        "service_id",
        "name",
        "price_kes",
        "duration_min",
    }


def test_nearbyShops_contract():
    """lib/api.ts -> `NearbyShops`."""
    assert _fields("/v1/public/shops/nearby", "get") == {
        "shops",
        "count",
        "radius_km",
        "center_lat",
        "center_lng",
        "outside_radius",
    }


def test_nearbyShop_contract():
    """lib/api.ts -> `NearbyShop`."""
    # Pull one item out of the shops array.
    schema = _schema(
        SPEC["paths"]["/v1/public/shops/nearby"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )
    item = _schema(schema["properties"]["shops"]["items"])
    assert set(item["properties"]) == {
        "slug",
        "name",
        "business_name",
        "business_type",
        "town",
        "county",
        "address",
        "phone",
        "price_tier",
        "latitude",
        "longitude",
        "distance_km",
        "distance_label",
        "opens_at",
        "closes_at",
        "is_open_now",
        "num_chairs",
    }


def test_nearby_endpoint_is_public():
    """The customer has no account, so discovery may not start requiring one."""
    assert SPEC["paths"]["/v1/public/shops/nearby"]["get"].get("security") in (None, [])


def test_nearby_parameters_match_the_client():
    """The client builds this URL by hand in `nearbyShops`."""
    params = _params("/v1/public/shops/nearby", "get")
    assert params["lat"] is True
    assert params["lng"] is True
    assert params["radius_km"] is False, "radius_km must stay optional"
    assert params["limit"] is False, "limit must stay optional"


# ── parameters the client sends ─────────────────────────────────────────────
def test_slot_endpoint_parameter_names_match_the_client():
    """The client builds this URL by hand in `getSlots`.

    A typo here (`service_id` vs `service_ids`) is invisible to the type
    checker and produces a 422 at runtime, so it is worth pinning.
    """
    params = _params("/v1/public/{branch_slug}/slots", "get")
    assert params["staff_id"] is True
    assert params["service_ids"] is True
    assert params.get("day", False) is False, "day must stay optional"


def test_service_ids_is_repeated_not_comma_joined():
    """`withServiceIds()` appends the key once per service.

    If this ever became a comma-joined string the backend's
    `Service.id.in_(service_ids)` would silently match nothing and the customer
    would see a menu with no free times.
    """
    param = next(
        p
        for p in SPEC["paths"]["/v1/public/{branch_slug}/slots"]["get"]["parameters"]
        if p["name"] == "service_ids"
    )
    assert param["in"] == "query"
    schema = _schema(param["schema"])
    assert schema.get("type") == "array", "must be a repeated query parameter"


# ── the 409 the client depends on ───────────────────────────────────────────
def test_booking_conflict_still_returns_next_free_slots():
    """`SlotTakenError` in lib/api.ts reads `detail.next_free_slots`.

    Losing the alternatives turns a recoverable race into a dead end: the
    customer would have to start the whole funnel again because someone else
    took the chair.
    """
    responses = SPEC["paths"]["/v1/public/{branch_slug}/appointments"]["post"][
        "responses"
    ]
    assert "409" in responses, "the booking endpoint must still document a 409"

    detail = _schema(responses["409"]["content"]["application/json"]["schema"])
    props = set(detail.get("properties", {}))
    assert "next_free_slots" in props, "the client depends on the alternatives list"


def test_booking_endpoint_accepts_the_clients_payload():
    body = _schema(
        SPEC["paths"]["/v1/public/{branch_slug}/appointments"]["post"]
        .get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    required = set(body.get("required", []))
    for field in (
        "customer_name",
        "customer_phone",
        "staff_id",
        "service_ids",
        "scheduled_at",
    ):
        assert field in required, f"the form always sends {field}"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/public/{branch_slug}",
        "/v1/public/{branch_slug}/services",
        "/v1/public/{branch_slug}/staff",
        "/v1/public/{branch_slug}/slots",
    ],
)
def test_portal_reads_are_reachable_without_a_token(path):
    """The customer has no account, so no read may start requiring one."""
    assert SPEC["paths"][path]["get"].get("security") in (None, [])
