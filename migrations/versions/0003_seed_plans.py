"""seed the 5 plan tiers from the approved formula-based pricing (Doc 1 § pricing)

Every price derives from the Daily base rate: `daily_price × days × (1 − discount)`,
rounded to the nearest KES 10. Only the Daily rate and the discount ladder are
authoritative, so a future price change means editing two config values, not a
table of hard-coded numbers.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# (code, price_kes, interval_days, discount_pct) — the approved prices.
# See Groomio_Phase1_Documents.md § pricing check.
PLANS = [
    ("daily", 40, 1, 0),
    ("biweekly", 500, 14, 11),
    ("monthly", 1000, 30, 17),
    ("half_yearly", 5400, 180, 25),
    ("yearly", 9780, 365, 33),
]


def upgrade() -> None:
    plans = sa.table(
        "plans",
        sa.column("id", UUID),
        sa.column("code", sa.String),
        sa.column("price_kes", sa.Integer),
        sa.column("interval_days", sa.SmallInteger),
        sa.column("discount_pct", sa.SmallInteger),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(
        plans,
        [
            {
                "code": code,
                "price_kes": price,
                "interval_days": days,
                "discount_pct": discount,
                "is_active": True,
            }
            for code, price, days, discount in PLANS
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM plans WHERE code IN ('daily','biweekly','monthly','half_yearly','yearly')"
    )
