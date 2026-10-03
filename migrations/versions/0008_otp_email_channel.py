"""otp_codes: add email address + delivery channel

The signup flow previously had exactly one way to prove "this phone is real":
an SMS to the number. That is the right default for a Kenyan phone-first app,
but a customer whose SIM is in another country, or whose phone is at the
barbershop, gets stuck — and the email they already typed is sitting right
there unused.

This adds a second channel so the same 6-digit code can be delivered by email.
Two columns, both nullable with safe defaults, so the change is additive:

* `channel` — 'sms' (existing behaviour) or 'email'. Default 'sms' keeps every
  existing row and every existing query valid without a backfill.
* `email` — the address the code was sent to when channel == 'email'. Nullable
  because SMS rows have no email.

The recipient of a code is therefore `phone` for SMS and `email` for email.
Both are looked up by the same `purpose` + `channel` predicate in verify, so
the two channels cannot cross-contaminate: an SMS code never validates an email
address and vice versa.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A brand-new enum type — no existing column references it, so this is a
    # plain CREATE TYPE rather than the rename/rebuild dance migration 0006
    # needed for the notification_type extension.
    op.execute("CREATE TYPE otp_channel AS ENUM ('sms', 'email')")

    op.add_column(
        "otp_codes",
        sa.Column(
            "channel",
            sa.Enum("sms", "email", name="otp_channel"),
            nullable=True,
        ),
    )
    op.add_column("otp_codes", sa.Column("email", sa.String(length=255), nullable=True))

    # Default every existing row to the channel it already had: SMS.
    op.execute("UPDATE otp_codes SET channel = 'sms' WHERE channel IS NULL")
    op.alter_column("otp_codes", "channel", server_default="sms", nullable=False)

    # A code's recipient is phone (SMS) or email (email). Enforcing that the
    # right one is populated per channel is a CHECK, not a column constraint,
    # because "if sms then phone not null else email not null" is conditional.
    op.execute(
        "ALTER TABLE otp_codes ADD CONSTRAINT otp_codes_recipient "
        "CHECK ("
        "(channel = 'sms' AND phone IS NOT NULL AND email IS NULL) OR "
        "(channel = 'email' AND email IS NOT NULL AND phone IS NULL)"
        ")"
    )

    op.create_index(
        "ix_otp_codes_channel_created", "otp_codes", ["channel", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_otp_codes_channel_created", table_name="otp_codes")
    op.drop_constraint("otp_codes_recipient", "otp_codes", type_="check")
    op.drop_column("otp_codes", "email")
    op.drop_column("otp_codes", "channel")
    op.execute("DROP TYPE IF EXISTS otp_channel")
