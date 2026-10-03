"""add location, contact and price tier to branches (discovery)

The customer portal could only be reached through a shared branch link. A
customer with no link — or who has moved and wants a barber near them *now* —
had no way to find a shop. This adds what discovery needs.

Design decisions worth stating, because they are the ones that bite later:

* **Coordinates are nullable, never defaulted to (0,0).** A shop that has not
  pinned its map marker stays fully bookable by link; it is simply absent from
  "near me". Defaulting to (0,0) would place it in the Gulf of Guinea and send
  a customer on a very long walk.

* **No NOT NULL and no backfill.** The alternative — geocoding every existing
  `town` string — invents a location for shops that have never stated one, and
  a confidently wrong pin is worse than an honest absence.

* **address is free text and is not geocoded.** Groomio makes no third-party
  geocoding call, so the coordinate and the written address can disagree. The
  coordinate is used for distance maths; the address is only ever shown to a
  human, who resolves any disagreement.

* **price_tier is 1-3, not a price.** The discovery list needs a signal cheap
  enough to scan at a glance; exact per-service prices live on the shop's own
  page and are already fetched there.

The composite index is on (latitude, longitude) but discovery does not rely on
it alone: PostgreSQL will not use a b-tree for a range on latitude combined
with an equality on nothing in particular. It is kept because it serves the
common "everyone in one town" query, while the general case uses the bounding
box + haversine split in app/geo.py.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("branches", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("branches", sa.Column("longitude", sa.Float(), nullable=True))
    op.add_column(
        "branches", sa.Column("address", sa.String(length=200), nullable=True)
    )
    op.add_column("branches", sa.Column("phone", sa.String(length=20), nullable=True))
    op.add_column("branches", sa.Column("price_tier", sa.Integer(), nullable=True))

    # A sanity check that can only fail on a bad deploy, not on bad data:
    # coordinates outside Kenya's bounds on a Kenyan directory are a sign the
    # owner typed degrees/minutes by mistake, and they poison every radius
    # search the shop appears in.
    op.execute(
        "ALTER TABLE branches ADD CONSTRAINT branches_latitude_range "
        "CHECK (latitude IS NULL OR (latitude BETWEEN -4.8 AND 5.1))"
    )
    op.execute(
        "ALTER TABLE branches ADD CONSTRAINT branches_longitude_range "
        "CHECK (longitude IS NULL OR (longitude BETWEEN 33.9 AND 41.9))"
    )
    op.execute(
        "ALTER TABLE branches ADD CONSTRAINT branches_price_tier_range "
        "CHECK (price_tier IS NULL OR price_tier BETWEEN 1 AND 3)"
    )

    # Both coordinates or neither: a half-located branch would otherwise sit in
    # the bounding box of every search along the prime meridian.
    op.execute(
        "ALTER TABLE branches ADD CONSTRAINT branches_coords_paired "
        "CHECK ((latitude IS NULL) = (longitude IS NULL))"
    )

    op.create_index(
        "ix_branches_latitude_longitude", "branches", ["latitude", "longitude"]
    )


def downgrade() -> None:
    op.drop_index("ix_branches_latitude_longitude", table_name="branches")

    op.drop_constraint("branches_coords_paired", "branches", type_="check")
    op.drop_constraint("branches_price_tier_range", "branches", type_="check")
    op.drop_constraint("branches_longitude_range", "branches", type_="check")
    op.drop_constraint("branches_latitude_range", "branches", type_="check")

    op.drop_column("branches", "price_tier")
    op.drop_column("branches", "phone")
    op.drop_column("branches", "address")
    op.drop_column("branches", "longitude")
    op.drop_column("branches", "latitude")
