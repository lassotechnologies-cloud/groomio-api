"""Add login_verification to otp_purpose enum

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires creating a new type and altering the column
    # to add a value to an existing enum.
    op.execute("ALTER TYPE otp_purpose ADD VALUE 'login_verification'")


def downgrade() -> None:
    # Removing an enum value requires recreating the type in PostgreSQL.
    # This is a best-effort downgrade; in production, keep the value.
    op.execute("ALTER TYPE otp_purpose RENAME TO otp_purpose_old")
    op.execute("CREATE TYPE otp_purpose AS ENUM ('signup_verification')")
    op.execute(
        "ALTER TABLE otp_codes ALTER COLUMN purpose TYPE otp_purpose "
        "USING purpose::text::otp_purpose"
    )
    op.execute("DROP TYPE otp_purpose_old")
