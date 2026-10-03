"""Shop discovery — "find a barber near me" (public, unauthenticated).

The counterpart to the booking funnel: instead of a customer arriving with a
shop's link, they arrive with a location and no idea who is nearby.

Three rules make this safe to leave open, and all three are load-bearing:

1. **Only businesses that can actually take a payment.** A shop in `suspended`
   or `expired` status is filtered out, not merely deprioritised. Showing a
   customer a barbershop that will refuse to take their money — and, worse,
   that they will then walk to — is a real cost, so those rows never enter the
   result set at all.

2. **Only branches that are bookable and located.** `is_active` false means the
   shop stopped taking bookings; a NULL coordinate means it never pinned a map
   marker. Both are excluded rather than defaulted.

3. **Nothing beyond the price list is exposed.** See discovery_schemas.

The `Business.status IN (...)` filter is the reason this is safe to leave
unauthenticated: an expired shop is invisible, so discovery cannot be used to
enumerate who is on Groomio, how many shops exist in a county, or which shops
have lapsed.
"""

from datetime import time
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.discovery_schemas import NearbyShopsResponse, NearbyShopResponse
from app.core.database import get_db
from app.core.security import now_utc
from app.geo import (
    bounding_box,
    format_distance,
    haversine_km,
    is_valid_coordinate,
)
from app.models.tenant import Branch, Business

router = APIRouter(tags=["discovery"])

Db = Annotated[AsyncSession, Depends(get_db)]

# Statuses in which a shop may still take a customer's money. `trialing` is
# included deliberately: a shop on a free trial is a real shop taking real
# bookings, and excluding it would hide every new signup from discovery.
BOOKABLE_STATUSES = ("trialing", "active", "grace")

# A customer walking to a barbershop is not walking 50 km. 50 km is the ceiling
# because it is also the radius that makes "near me" a useful *search* rather
# than a filter that silently hides every result.
MAX_RADIUS_KM = 50.0
DEFAULT_RADIUS_KM = 5.0
MAX_RESULTS = 50


def _is_open_now(opens_at: Optional[time], closes_at: Optional[time]) -> bool:
    """Whether a shop's own posted hours include the current local time.

    Computed server-side against East Africa Time, not from the device clock.
    A phone with a wrong timezone would otherwise show every shop as shut.

    A shop with no hours set is reported as open: the fallback used by the
    booking slot logic is a conventional 08:00-19:00 day, and saying "closed"
    about a shop that has simply not configured hours would hide it.
    """
    if opens_at is None or closes_at is None:
        return True

    from datetime import timedelta, timezone

    eaf = timezone(timedelta(hours=3))
    now_local = now_utc().astimezone(eaf).time()
    return opens_at <= now_local <= closes_at


# ── CU4 — find shops near me ─────────────────────────────────────────────────
@router.get("/public/shops/nearby", response_model=NearbyShopsResponse)
async def nearby_shops(
    db: Db,
    lat: Annotated[float, Query(ge=-90, le=90, description="Customer latitude")],
    lng: Annotated[float, Query(ge=-180, le=180, description="Customer longitude")],
    radius_km: Annotated[
        float, Query(gt=0, le=MAX_RADIUS_KM, description="Search radius in km")
    ] = DEFAULT_RADIUS_KM,
    limit: Annotated[int, Query(ge=1, le=MAX_RESULTS)] = 20,
) -> NearbyShopsResponse:
    """Shops within `radius_km` of the customer, nearest first.

    Note the route order: this is `/public/shops/nearby`, not
    `/public/{branch_slug}`, so it cannot be shadowed by the branch lookups and
    cannot be mistaken for a shop slug. FastAPI matches the literal segment
    first regardless, but the distinct shape makes the intent obvious.
    """
    # A phone that denied the prompt, or a broken GPS, reports (0,0) — the
    # Atlantic. Answering "no shops near you" there is technically true and
    # completely useless, so it is rejected as a bad request instead.
    if not is_valid_coordinate(lat, lng):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Location is outside Kenya or unavailable. "
            "Allow location access, or search by town instead.",
        )

    min_lat, max_lat, min_lng, max_lng = bounding_box(lat, lng, radius_km)

    # Step 1 — the cheap, indexable rectangle.
    stmt = (
        select(Branch, Business)
        # `businesses.id` is a UUID while `branches.business_id` is VARCHAR
        # (migration 0002), and PostgreSQL has no `uuid = varchar` operator, so
        # the uncast ON clause raises "operator does not exist" on every request.
        # `business_id` always holds the uuid rendered as text, so casting it back
        # is lossless and the match is exactly the same set of rows.
        .join(Business, Business.id == cast(Branch.business_id, UUID))
        .where(
            Branch.is_active.is_(True),
            # Discovery skips shops that never pinned a marker. Half-located
            # rows are impossible (migration 0007 pairs the two columns), so
            # checking latitude alone is enough and lets the index do the work.
            Branch.latitude.isnot(None),
            Branch.longitude.isnot(None),
            Branch.latitude.between(min_lat, max_lat),
            Branch.longitude.between(min_lng, max_lng),
            # The reason this endpoint is safe to leave open.
            Business.status.in_(BOOKABLE_STATUSES),
        )
        .limit(MAX_RESULTS * 4)
    )
    rows = (await db.execute(stmt)).all()

    # Step 2 — true distance, radius filter, sort. The rectangle's corners are
    # the only rows that can fail here, but they must be dropped: telling a
    # customer a shop is 5 km away when it is 7.4 km is the exact failure this
    # second pass exists to prevent.
    scored: list[tuple[float, Branch, Business]] = []
    for branch, business in rows:
        distance = haversine_km(lat, lng, branch.latitude, branch.longitude)
        if distance <= radius_km:
            scored.append((distance, branch, business))

    outside = len(rows) - len(scored)
    scored.sort(key=lambda item: item[0])

    shops = [
        NearbyShopResponse(
            slug=branch.slug,
            name=branch.name,
            business_name=business.name,
            business_type=business.business_type,
            town=branch.town or business.town,
            county=branch.county or business.county,
            address=branch.address,
            phone=branch.phone,
            price_tier=branch.price_tier,
            latitude=branch.latitude,
            longitude=branch.longitude,
            distance_km=round(distance, 3),
            distance_label=format_distance(distance),
            opens_at=branch.opens_at.strftime("%H:%M") if branch.opens_at else None,
            closes_at=branch.closes_at.strftime("%H:%M") if branch.closes_at else None,
            is_open_now=_is_open_now(branch.opens_at, branch.closes_at),
            num_chairs=branch.num_chairs,
        )
        for distance, branch, business in scored[:limit]
    ]

    return NearbyShopsResponse(
        shops=shops,
        count=len(shops),
        radius_km=radius_km,
        center_lat=lat,
        center_lng=lng,
        outside_radius=outside,
    )
