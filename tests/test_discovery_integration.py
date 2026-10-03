"""Integration tests for the discovery query (needs TEST_DATABASE_URL).

These cover the rules that only exist once there are rows in the table — most
importantly that a lapsed or suspended shop is *absent* rather than present and
deprioritised. A customer who walks two kilometres to a barbershop that has
stopped taking bookings has been actively harmed by the feature.
"""

import itertools
import uuid

import pytest

from app.geo import haversine_km

NAIROBI = (-1.2921, 36.8219)

# `users.phone` and `users.email` are UNIQUE, and several tests seed two shops in
# one transaction, so both come from a per-process sequence. Deriving them from the
# slug collided ("live" and "dead" are both four characters).
_USER_SEQ = itertools.count(1)


def _seed_shop(
    session,
    *,
    slug: str,
    lat: float | None,
    lng: float | None,
    status: str = "active",
    branch_active: bool = True,
    town: str = "Nairobi",
):
    """One business + one located branch, plus the owner login behind it."""
    from app.models.identity import User
    from app.models.tenant import Branch, Business

    # Every `id` is a UUID primary key (`uuid_pk()`), and every `*_id` is a
    # VARCHAR column holding that uuid rendered as text. Supplying a readable
    # label such as "owner-nearby" is therefore invalid twice over: asyncpg
    # rejects it as an unparseable UUID for the PK, and the FK columns expect
    # text. uuid5 keeps the values deterministic, so a failure is reproducible.
    owner_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/discovery/owner/{slug}")
    business_id = uuid.uuid5(uuid.NAMESPACE_URL, f"groomio-test/discovery/business/{slug}")

    session.add(
        User(
            id=owner_id,
            full_name="Owner",
            phone=f"0700{next(_USER_SEQ):06d}",
            email=f"owner-{slug}-{next(_USER_SEQ)}@example.com",
            password_hash="x",
            role="owner",
        )
    )

    biz = Business(
        id=business_id,
        owner_user_id=str(owner_id),
        name=f"Shop {slug}",
        business_type="barber_shop",
        town=town,
        status=status,
    )
    session.add(biz)

    branch = Branch(
        business_id=str(business_id),
        slug=slug,
        name=f"Branch {slug}",
        town=town,
        latitude=lat,
        longitude=lng,
        address=f"{slug} road",
        phone="0700000000",
        price_tier=2,
        is_active=branch_active,
    )
    session.add(branch)
    return {"business": biz, "branch": branch}


async def _search(db, lat=NAIROBI[0], lng=NAIROBI[1], radius_km=5.0, limit=20):
    """Run the endpoint's query the same way the router does."""
    from sqlalchemy import cast, select
    from sqlalchemy.dialects.postgresql import UUID

    from app.booking.discovery import BOOKABLE_STATUSES, MAX_RESULTS
    from app.geo import bounding_box
    from app.models.tenant import Branch, Business

    min_lat, max_lat, min_lng, max_lng = bounding_box(lat, lng, radius_km)
    stmt = (
        select(Branch, Business)
        # Same ON clause as app.booking.discovery.nearby_shops, including the
        # cast: businesses.id is UUID and branches.business_id is VARCHAR, so
        # PostgreSQL rejects the bare comparison.
        .join(Business, Business.id == cast(Branch.business_id, UUID))
        .where(
            Branch.is_active.is_(True),
            Branch.latitude.isnot(None),
            Branch.longitude.isnot(None),
            Branch.latitude.between(min_lat, max_lat),
            Branch.longitude.between(min_lng, max_lng),
            Business.status.in_(BOOKABLE_STATUSES),
        )
        .limit(MAX_RESULTS * 4)
    )

    scored = []
    for branch, business in (await db.execute(stmt)).all():
        d = haversine_km(lat, lng, branch.latitude, branch.longitude)
        if d <= radius_km:
            scored.append((d, branch, business))
    scored.sort(key=lambda x: x[0])
    return scored[:limit]


@pytest.mark.asyncio
async def test_finds_a_shop_ten_minutes_away(db):
    # ~0.05 degrees of latitude north of Nairobi, roughly 5.5 km.
    _seed_shop(db, slug="nearby", lat=NAIROBI[0] + 0.05, lng=NAIROBI[1])
    await db.flush()

    # The shop is 5.5 km out, so the search radius has to reach it: at the 5 km
    # default the endpoint is *right* to return nothing, and this test is about
    # a shop that is found, not about the default radius (which
    # `test_excludes_a_shop_outside_the_radius` and
    # `test_bounding_box_corners_are_filtered_by_the_radius_pass` cover).
    found = await _search(db, radius_km=8.0)
    assert len(found) == 1
    assert 4.0 < found[0][0] < 7.0, "distance should be about 5.5 km"


@pytest.mark.asyncio
async def test_excludes_a_shop_outside_the_radius(db):
    _seed_shop(db, slug="faraway", lat=-4.0435, lng=39.6682)  # Mombasa
    await db.flush()

    assert await _search(db, radius_km=5.0) == []


@pytest.mark.asyncio
async def test_results_are_sorted_nearest_first(db):
    _seed_shop(db, slug="mid", lat=NAIROBI[0] + 0.03, lng=NAIROBI[1])
    _seed_shop(db, slug="close", lat=NAIROBI[0] + 0.005, lng=NAIROBI[1])
    _seed_shop(db, slug="lessclose", lat=NAIROBI[0] + 0.02, lng=NAIROBI[1])
    await db.flush()

    found = await _search(db)
    distances = [d for d, _, _ in found]
    assert distances == sorted(distances)
    assert found[0][1].slug == "close"
    assert found[-1][1].slug == "mid"


@pytest.mark.asyncio
async def test_suspended_business_is_invisible(db):
    """Not deprioritised — absent.

    This is the rule that makes a public, unfiltered directory safe: a lapsed
    shop cannot be discovered at all, so the endpoint cannot be used to find
    out who is on Groomio or who stopped paying.
    """
    _seed_shop(db, slug="live", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1])
    _seed_shop(db, slug="dead", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1], status="suspended")
    await db.flush()

    slugs = [b.slug for _, b, _ in await _search(db)]
    assert slugs == ["live"]
    assert "dead" not in slugs


@pytest.mark.asyncio
async def test_expired_business_is_invisible(db):
    _seed_shop(db, slug="gone", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1], status="expired")
    await db.flush()
    assert await _search(db) == []


@pytest.mark.asyncio
async def test_trialing_business_is_visible(db):
    """A shop on a free trial is a real shop taking real bookings."""
    _seed_shop(
        db, slug="newbie", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1], status="trialing"
    )
    await db.flush()
    assert [b.slug for _, b, _ in await _search(db)] == ["newbie"]


@pytest.mark.asyncio
async def test_deactivated_branch_is_invisible(db):
    _seed_shop(
        db, slug="closed", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1], branch_active=False
    )
    await db.flush()
    assert await _search(db) == []


@pytest.mark.asyncio
async def test_unlocated_shop_is_skipped_not_defaulted(db):
    """A shop that never pinned a marker stays bookable by link but must not
    appear in "near me".

    Defaulting it to (0,0) would place it in the Atlantic; guessing from its
    town would invent a location the owner never stated.
    """
    _seed_shop(db, slug="nopin", lat=None, lng=None)
    _seed_shop(db, slug="pinned", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1])
    await db.flush()

    slugs = [b.slug for _, b, _ in await _search(db)]
    assert slugs == ["pinned"]


@pytest.mark.asyncio
async def test_bounding_box_corners_are_filtered_by_the_radius_pass(db):
    """The rectangle is wider than the circle; the haversine pass is what makes
    the answer honest. A shop placed just outside the true radius but inside
    the box must not come back.
    """
    from app.geo import bounding_box

    min_lat, max_lat, min_lng, max_lng = bounding_box(*NAIROBI, 5.0)

    # The box corner, which is ~7.1 km from the centre at this latitude.
    _seed_shop(db, slug="corner", lat=max_lat, lng=max_lng)
    _seed_shop(db, slug="centre", lat=NAIROBI[0] + 0.01, lng=NAIROBI[1])
    await db.flush()

    corner_km = haversine_km(*NAIROBI, max_lat, max_lng)
    assert corner_km > 5.0, "precondition: the corner really is outside 5 km"

    slugs = [b.slug for _, b, _ in await _search(db, radius_km=5.0)]
    assert slugs == ["centre"], "the corner must be dropped by the radius pass"


@pytest.mark.asyncio
async def test_widening_the_radius_finds_more_shops(db):
    _seed_shop(db, slug="a", lat=NAIROBI[0] + 0.03, lng=NAIROBI[1])  # ~3.3 km
    _seed_shop(db, slug="b", lat=NAIROBI[0] + 0.20, lng=NAIROBI[1])  # ~22 km
    await db.flush()

    assert len(await _search(db, radius_km=5.0)) == 1
    assert len(await _search(db, radius_km=50.0)) == 2
