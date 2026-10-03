"""tenant tables — businesses, branches, services, plans, subscriptions

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _ts() -> list:
    """created_at / updated_at — mirrors app/models/base.py TimestampMixin."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _pk() -> sa.Column:
    return sa.Column(
        "id", UUID, primary_key=True, server_default=sa.func.gen_random_uuid()
    )


def upgrade() -> None:
    # ── businesses — one row per paying shop (the tenant) ────────────────────
    op.create_table(
        "businesses",
        _pk(),
        sa.Column("owner_user_id", sa.String, nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column(
            "business_type",
            sa.Enum(
                "barber_shop",
                "salon",
                "nail_bar",
                "beauty_clinic",
                "spa",
                name="business_type",
            ),
            nullable=False,
        ),
        sa.Column("logo_url", sa.String, nullable=True),
        sa.Column("county", sa.String(80), nullable=True),
        sa.Column("town", sa.String(80), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "trialing",
                "active",
                "grace",
                "expired",
                "suspended",
                name="business_status",
            ),
            server_default="trialing",
            nullable=False,
        ),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chat_enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        *_ts(),
    )
    op.create_index("ix_businesses_owner_user_id", "businesses", ["owner_user_id"])
    op.create_index("ix_businesses_status", "businesses", ["status"])

    # ── branches — physical locations under one business ────────────────────
    op.create_table(
        "branches",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        # Public booking links (D3/D8) address a branch by slug, not UUID.
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("county", sa.String(80), nullable=True),
        sa.Column("town", sa.String(80), nullable=True),
        sa.Column("opens_at", sa.Time, nullable=True),
        sa.Column("closes_at", sa.Time, nullable=True),
        sa.Column("num_chairs", sa.Integer, server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        *_ts(),
    )
    op.create_index("ix_branches_business_id", "branches", ["business_id"])
    op.create_index("ix_branches_slug", "branches", ["slug"], unique=True)

    # ── services — what the shop sells ──────────────────────────────────────
    op.create_table(
        "services",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_kes", sa.Integer, nullable=False),
        sa.Column("duration_min", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        *_ts(),
    )
    op.create_index("ix_services_branch_id", "services", ["branch_id"])

    # ── plans — the 5 tiers, seeded by 0003 from settings.* pricing ─────────
    op.create_table(
        "plans",
        _pk(),
        sa.Column(
            "code",
            sa.Enum(
                "daily",
                "biweekly",
                "monthly",
                "half_yearly",
                "yearly",
                name="plan_code",
            ),
            nullable=False,
        ),
        sa.Column("price_kes", sa.Integer, nullable=False),
        sa.Column("interval_days", sa.SmallInteger, nullable=False),
        sa.Column("discount_pct", sa.SmallInteger, server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        *_ts(),
    )
    op.create_index("ix_plans_code", "plans", ["code"], unique=True)

    # ── subscriptions — one per business; the billing state machine ──────────
    op.create_table(
        "subscriptions",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("plan_id", sa.String, nullable=False),
        sa.Column(
            "status",
            sa.Enum("trial", "active", "grace", "expired", name="subscription_status"),
            server_default="trial",
            nullable=False,
        ),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        *_ts(),
    )
    op.create_index("ix_subscriptions_business_id", "subscriptions", ["business_id"])
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"])


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_table("plans")
    op.drop_table("services")
    op.drop_table("branches")
    op.drop_table("businesses")
