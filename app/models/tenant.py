"""9.2 Business & Branches (the tenant core) — businesses, branches, services."""

from sqlalchemy import (
    Column,
    String,
    Enum,
    Integer,
    Boolean,
    Float,
    Index,
    Time,
    DateTime,
)
from app.models.base import Base, TimestampMixin, uuid_pk


class Business(Base, TimestampMixin):
    """One row per paying shop/salon (the "tenant" = the locked private office).

    **One owner owns exactly one business.** That is the approved Groomio MVP rule
    (many branches per business, but not many businesses per owner), and it is
    enforced here rather than trusted to application code.

    Why a database index and not an application check: every read path that
    resolves a tenant — `core.tenancy.resolve_business_id`, `queue`, `appointments`,
    `customers` — does `SELECT … WHERE owner_user_id = :uid` with no `LIMIT 1`
    and simply takes the row it gets first. Two rows therefore do not just sit
    there producing a duplicate shop; they make tenant resolution return an
    *arbitrary* business on different endpoints for the same token. A unique
    index makes the second row impossible instead of merely unlikely.

    It is a **plain** unique index, not a partial one. `owner_user_id` is
    `NOT NULL`, so `WHERE owner_user_id IS NOT NULL` would restrict the index to
    every row and encode a product decision that does not exist yet — nothing in
    Groomio supports releasing a shop and opening another under the same owner.

    `ix_businesses_owner_user_id` (created by migration 0002) is now redundant
    with this one: a unique index on the same single column serves the same
    lookups. Dropping it is safe and saves write amplification on a hot column,
    but it is deliberately *not* bundled into the fix — see the deployment notes
    in `docs/T-003-business-onboarding-concurrency.md`.
    """

    __tablename__ = "businesses"

    __table_args__ = (
        Index("uq_businesses_owner_user_id", "owner_user_id", unique=True),
    )

    id = uuid_pk()
    owner_user_id = Column(String, nullable=False, index=True)
    name = Column(String(160), nullable=False)
    business_type = Column(
        Enum(
            "barber_shop",
            "salon",
            "nail_bar",
            "beauty_clinic",
            "spa",
            name="business_type",
        ),
        nullable=False,
    )
    logo_url = Column(String, nullable=True)
    county = Column(String(80), nullable=True)
    town = Column(String(80), nullable=True)
    status = Column(
        Enum(
            "trialing",
            "active",
            "grace",
            "expired",
            "suspended",
            name="business_status",
        ),
        default="trialing",
        nullable=False,
    )
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    chat_enabled = Column(Boolean, default=True, nullable=False)


class Branch(Base, TimestampMixin):
    """Physical locations; an Owner can have many."""

    __tablename__ = "branches"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    # Public booking links (Doc 10.5 D3/D8) address a branch by slug so customers
    # never see an internal UUID in a shared URL.
    slug = Column(String(80), nullable=False, unique=True)
    name = Column(String(120), nullable=False)
    county = Column(String(80), nullable=True)
    town = Column(String(80), nullable=True)
    opens_at = Column(Time, nullable=True)
    closes_at = Column(Time, nullable=True)
    num_chairs = Column(Integer, default=1, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # ── "find a barber near me" (discovery) ────────────────────────────────
    # Both coordinates are nullable: a shop that has not pinned its map marker
    # must still be bookable by link. Discovery simply skips those rows rather
    # than guessing a location and putting a customer on a bus to the wrong
    # county. There is deliberately no default of (0,0) — a shop silently
    # placed in the Gulf of Guinea is worse than one missing from the list.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    # Street-level address, free text. Not geocoded: Groomio does not call a
    # geocoding API, so a wrong pin can never be corrected automatically.
    address = Column(String(200), nullable=True)
    # Public contact. A customer who cannot find the shop needs a number, and
    # booking is frequently a question ("do you take walk-ins?").
    phone = Column(String(20), nullable=True)
    # 1-3, cheapest-to-mid. A customer scanning a list needs a price signal
    # before committing to a tap; showing exact prices would be noise here.
    price_tier = Column(Integer, nullable=True)


class Service(Base, TimestampMixin):
    """What the shop sells (haircut, beard trim…)."""

    __tablename__ = "services"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    price_kes = Column(Integer, nullable=False)
    duration_min = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
