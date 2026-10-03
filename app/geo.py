"""Distance maths for "find a barber near me".

Kept separate from the router so it can be unit-tested without a database, and
so the same code can be reused if the staff app ever needs a distance sort.

The strategy is the standard two-step one:

1. **Bounding box** — ask the database only for rows inside a cheap rectangle
   around the customer. This is the query that can use an index, and it throws
   away the overwhelming majority of rows before any trigonometry happens.
2. **Haversine** — compute the true great-circle distance for the survivors,
   drop anything outside the radius, and sort by it.

Doing only step 2 would mean scanning every branch row on every search. Doing
only step 1 would return a rectangle, whose corners are up to ~41% further
away than its centre at Nairobi's latitude — a customer told "2 km away" and
sent 2.9 km is the failure mode this module exists to prevent.

Distances use the mean Earth radius (6371.0088 km, the IUGG value). The
difference from 6371 is under 0.3%, which is far below the error introduced by
a shop's hand-placed map pin, so precision beyond this is false confidence.
"""

from math import asin, cos, radians, sin, sqrt

# Mean Earth radius in kilometres (IUGG).
EARTH_RADIUS_KM = 6371.0088

# Kenya's bounding box, used to validate customer-supplied coordinates before
# they reach the database. Rejecting a phone reporting (0,0) — a very common
# "permission denied" result on some Android builds — is far better than
# returning the empty result set that search would otherwise produce.
KENYA_LAT_RANGE = (-4.8, 5.1)
KENYA_LNG_RANGE = (33.9, 41.9)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in kilometres.

    The haversine form is used rather than the spherical law of cosines
    because the latter loses precision for the very small distances this
    feature cares about — two shops 200 m apart on the same road. Its
    `acos` argument rounds to 1.0 in float at that range and the formula
    returns zero for two distinct shops.
    """
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    # `min(1.0, a)` guards against floating-point overshoot: a can exceed 1 by
    # a few ULP for antipodal points, and asin(1.1) raises ValueError.
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, a)))


def bounding_box(
    lat: float, lng: float, radius_km: float
) -> tuple[float, float, float, float]:
    """A rectangle enclosing every point within `radius_km`.

    Returns (min_lat, max_lat, min_lng, max_lng). This deliberately
    over-approximates — the corners stick out beyond the true circle — because
    the haversine pass that follows removes them. The lat/lng deltas are
    computed separately because a degree of longitude is ~111 km * cos(latitude)*,
    so a square box in degrees would be far too wide at Nairobi and far too
    narrow at Lamu.
    """
    lat_delta = radius_km / 111.0
    # The cos() can go negative past the poles, and a negative longitude span
    # would silently produce an impossible (min > max) box.
    cos_lat = max(cos(radians(lat)), 1e-6)
    lng_delta = radius_km / (111.0 * cos_lat)

    return (
        max(lat - lat_delta, -90.0),
        min(lat + lat_delta, 90.0),
        lng - lng_delta,
        lng + lng_delta,
    )


def is_valid_coordinate(lat: float, lng: float) -> bool:
    """Whether a coordinate pair is inside Kenya and numerically sane.

    A customer who denies location permission, or a phone whose GPS is broken,
    reports (0, 0) — the middle of the Atlantic. Without this check that
    becomes "there are no shops near you" with no explanation.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return False
    return (
        KENYA_LAT_RANGE[0] <= lat <= KENYA_LAT_RANGE[1]
        and KENYA_LNG_RANGE[0] <= lng <= KENYA_LNG_RANGE[1]
    )


def format_distance(km: float) -> str:
    """Human-readable distance: '450 m', '1.2 km', '14 km'.

    Shown to a customer deciding whether to walk. Sub-kilometre distances are
    given in metres because "0.4 km" invites a customer to check the odometer.
    """
    if km < 1.0:
        return f"{int(round(km * 1000 / 10.0) * 10)} m"
    if km < 10.0:
        return f"{km:.1f} km"
    return f"{int(round(km))} km"
