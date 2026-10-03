"""initial identity tables — users, otp_codes, refresh_tokens, device_tokens

Revision ID: 0001
Revises:
Create Date: 2026-09-24 16:13:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users — every person who logs in (any role)
    op.create_table(
        "users",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(15), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "super_admin", "owner", "clerk", "barber", "customer", name="user_role"
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "active", "suspended", name="user_status"),
            server_default="active",
            nullable=False,
        ),
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
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    # otp_codes — 6-digit signup codes (SMS); stores a HASH, never the code
    op.create_table(
        "otp_codes",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("phone", sa.String(15), nullable=False),
        sa.Column("code_hash", sa.String(255), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum("signup_verification", name="otp_purpose"),
            server_default="signup_verification",
            nullable=False,
        ),
        sa.Column("attempts", sa.SmallInteger, server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_otp_codes_phone_created", "otp_codes", ["phone", "created_at"])

    # refresh_tokens — "stay logged in" tokens, stored hashed so we can revoke them
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("device", sa.String(120), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    # device_tokens — web-push subscriptions
    op.create_table(
        "device_tokens",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("keys_json", sa.JSON, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("device_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("otp_codes")
    op.drop_table("users")
