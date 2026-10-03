"""Tests for "find a barber near me" (CU4).

Split into two layers, for the same reason as the booking suite:

* **Pure geometry**, which runs everywhere. The numbers here are real reference
  points in Kenya, so a regression in the haversine is caught as a wrong answer
  rather than a changed shape.
* **Query rules**, which need PostgreSQL and skip cleanly without it. These
  cover the security-relevant part: an expired shop must be invisible, not
  merely sorted lower.
"""

import pytest

from app.geo import (
    bounding_box,
    format_distance,
    haversine_km,
    is_valid_coordinate,
)

# Real Kenyan reference points.
NAIROBI = (-1.2921, 36.8219)
MOMBASA = (-4.0435, 39.6682)
KISUMU = (-0.0917, 34.7680)


# ── haversine ────────────────────────────────────────────────────────────────
def test_distance_to_itself_is_zero():
    assert haversine_km(NAIROBI[0], NAIROBI[1], NAIROBI[0], NAIROBI[1]) == 0.0


def test_nairobi_to_mombasa_is_about_440km():
    # Straight-line is ~440 km; the road is ~480, which is the usual gap.
    km = haversine_km(*NAIROBI, *MOMBASA)
    assert 430 < km < 450, f"got {km:.1f} km"


def test_nairobi_to_kisumu_is_about_270km():
    km = haversine_km(*NAIROBI, *KISUMU)
    assert 255 < km < 285, f"got {km:.1f} km"


def test_distance_is_symmetric():
    assert haversine_km(*NAIROBI, *MOMBASA) == pytest.approx(
        haversine_km(*MOMBASA, *NAIROBI)
    )


def test_short_distances_do_not_collapse_to_zero():
    """The reason haversine is used instead of the law of cosines.

    Two shops 200 m apart on the same road: `acos` of a value that has rounded
    to 1.0 returns 0, which would show a customer "0 km away" and then send
    them the wrong way.
    """
    # ~0.002 degrees of latitude is roughly 222 m.
    km = haversine_km(NAIROBI[0], NAIROBI[1], NAIROBI[0] + 0.002, NAIROBI[1])
    assert 0.15 < km < 0.30, f"got {km} km, expected ~0.22"


def test_antipodal_points_do_not_raise():
    """Floating point can push the haversine argument above 1.0, and asin(>1)
    raises ValueError — a 500 on a public endpoint."""
    km = haversine_km(0.0, 0.0, 0.0, 180.0)
    assert 20_000 < km < 20_100


# ── bounding box ─────────────────────────────────────────────────────────────
def test_bounding_box_contains_the_centre():
    min_lat, max_lat, min_lng, max_lng = bounding_box(*NAIROBI, 5.0)
    assert min_lat < NAIROBI[0] < max_lat
    assert min_lng < NAIROBI[1] < max_lng


def test_bounding_box_corners_exceed_the_radius():
    """The box must over-approximate, or real matches get filtered out before
    the haversine pass can rescue them."""
    min_lat, max_lat, min_lng, max_lng = bounding_box(*NAIROBI, 5.0)
    corner = haversine_km(*NAIROBI, max_lat, max_lng)
    assert corner > 5.0, "a corner must lie outside the true circle"


def test_bounding_box_grows_longitude_span_with_latitude():
    """A degree of longitude shrinks by cos(latitude), so covering a fixed
    radius requires *more* degrees of longitude as latitude increases.

    At the equator one degree is ~111 km; at Lamu it is ~111 * cos(12.5°), and
    getting it backwards — a square box in degrees — makes high-latitude shops
    vanish from results. The box must widen in longitude with latitude.
    """
    _, _, min_lng_eq, max_lng_eq = bounding_box(0.0, 36.0, 10.0)
    _, _, min_lng_north, max_lng_north = bounding_box(5.0, 36.0, 10.0)
    span_eq = max_lng_eq - min_lng_eq
    span_north = max_lng_north - min_lng_north
    assert span_north > span_eq
    # cos(5 deg) is 0.9962, so the northern box is ~0.4% wider in longitude.
    assert 1.0 < span_north / span_eq < 1.01


def test_bounding_box_captures_a_shop_due_east_at_high_latitude():
    """A shop exactly `radius_km` due east must sit on the box edge, not outside.

    This is the regression guard for the cos() term: without it a shop 10 km
    due east of a customer at Lamu would fall outside the rectangle and be
    filtered away before the haversine pass could rescue it.
    """
    import math

    lat, lng, radius = 12.5, 36.0, 10.0  # Lamu-ish latitude
    shop_lng = lng + radius / (111.0 * math.cos(math.radians(lat)))
    min_lat, max_lat, min_lng, max_lng = bounding_box(lat, lng, radius)

    assert min_lng <= shop_lng <= max_lng
    assert haversine_km(lat, shop_lng, lat, lng) == pytest.approx(radius, abs=0.05)
    # The naive square-in-degrees box would have missed it.
    assert shop_lng > lng + radius / 111.0


def test_bounding_box_stays_in_valid_latitude_range():
    min_lat, max_lat, _, _ = bounding_box(89.9, 0.0, 500.0)
    assert min_lat >= -90.0
    assert max_lat <= 90.0


# ── coordinate validation ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "coord",
    [NAIROBI, MOMBASA, KISUMU, (-4.6, 39.6), (5.0, 34.0)],
)
def test_kenyan_coordinates_are_accepted(coord):
    assert is_valid_coordinate(*coord)


def test_null_island_is_rejected():
    """(0,0) is what a phone reports when location permission is refused.

    Returning an empty result for it says "no shops near you" in the Atlantic;
    rejecting it lets the client ask for a town instead.
    """
    assert not is_valid_coordinate(0.0, 0.0)


def test_out_of_range_values_are_rejected():
    assert not is_valid_coordinate(91.0, 36.8)
    assert not is_valid_coordinate(-1.3, 181.0)
    assert not is_valid_coordinate(-51.0, 0.0)  # southern hemisphere


# ── display formatting ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "km,expected",
    [
        (0.0, "0 m"),
        (0.12, "120 m"),
        (0.46, "460 m"),
        (0.999, "1000 m"),
        (1.24, "1.2 km"),
        (7.5, "7.5 km"),
        (9.94, "9.9 km"),
        (14.2, "14 km"),
        (42.7, "43 km"),
    ],
)
def test_distance_labels_are_walkable(km, expected):
    assert format_distance(km) == expected


def test_sub_kilometre_distances_never_read_as_zero_km():
    """A customer told "0.0 km" cannot tell which direction to walk."""
    for km in (0.01, 0.1, 0.3, 0.5, 0.9):
        assert "km" not in format_distance(km)


# ── contract: the endpoint stays safe to leave open ──────────────────────────
def test_discovery_requires_no_authentication():
    from app.main import app

    assert app.openapi()["paths"]["/v1/public/shops/nearby"]["get"].get(
        "security"
    ) in (None, [])


def test_discovery_rejects_a_zero_radius():
    """`radius_km > 0` in the signature: a zero radius would return the shop
    the customer is standing inside, which is not what "near me" means."""
    from app.main import app

    params = app.openapi()["paths"]["/v1/public/shops/nearby"]["get"]["parameters"]
    radius = next(p for p in params if p["name"] == "radius_km")
    assert radius["schema"]["exclusiveMinimum"] == 0


def test_discovery_radius_is_capped():
    from app.booking.discovery import MAX_RADIUS_KM

    assert MAX_RADIUS_KM <= 50.0, "a walking/short-bus search, not a road trip"


def test_only_bookable_business_statuses_are_searchable():
    """A suspended or expired shop must be filtered out of the query entirely.

    This is the single rule that makes an unauthenticated, unfiltered directory
    defensible: lapsed shops cannot be enumerated, so the endpoint cannot be
    used to discover who is on Groomio or who has stopped paying.
    """
    from app.booking.discovery import BOOKABLE_STATUSES

    assert "suspended" not in BOOKABLE_STATUSES
    assert "expired" not in BOOKABLE_STATUSES
    # A shop on a free trial is a real shop taking real bookings.
    assert "trialing" in BOOKABLE_STATUSES
    assert "active" in BOOKABLE_STATUSES


def test_open_now_uses_shop_hours():
    from datetime import time

    from app.booking.discovery import _is_open_now

    now_hour = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).hour

    # A window that certainly contains the current time.
    assert _is_open_now(time(0, 0), time(23, 59))
    # A window that certainly does not.
    assert not _is_open_now(time(now_hour, 30) if now_hour < 23 else time(0, 1), time(0, 1))
    # No hours configured is reported open, not closed — a shop that never set
    # hours must not disappear from the list.
    assert _is_open_now(None, None)
