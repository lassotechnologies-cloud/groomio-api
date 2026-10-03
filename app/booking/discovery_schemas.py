"""Response shapes for "find a barber near me".

A discovery listing is a different question from a booking page: the customer
has no link and no intent yet, so this returns *enough to choose* and nothing
more. Notably absent: owner names, staff counts, revenue, commission, or the
shop's customer list. A shop with a suspended subscription must also not appear.
"""

from typing import Optional

from pydantic import BaseModel


class NearbyShopResponse(BaseModel):
    """One shop in the discovery list."""

    # The public link. Booking still happens on /b/{slug}; discovery only helps
    # the customer decide which slug that should be.
    slug: str
    name: str
    business_name: str
    business_type: str
    town: Optional[str] = None
    county: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    price_tier: Optional[int] = None

    latitude: float
    longitude: float

    # Both the raw number and a pre-formatted string: the client would
    # otherwise reimplement the rounding rule, and "0.4 km" vs "400 m" is a
    # product decision, not a formatting detail.
    distance_km: float
    distance_label: str

    opens_at: Optional[str] = None
    closes_at: Optional[str] = None
    # Computed server-side from the shop's own hours, because "open now" depends
    # on a business's timezone rules and the device's clock is not trustworthy.
    is_open_now: bool
    num_chairs: int


class NearbyShopsResponse(BaseModel):
    """The whole search, so the client can explain an empty result."""

    shops: list[NearbyShopResponse]
    count: int
    # Echoed back so the client can label its own filter chips, and so a bug in
    # the radius defaulting is visible in the response rather than silent.
    radius_km: float
    center_lat: float
    center_lng: float
    # Shops inside the rectangle but outside the true radius. Almost always 0;
    # non-zero is a signal the bounding box is mis-tuned, which is why it is
    # reported instead of swallowed.
    outside_radius: int
