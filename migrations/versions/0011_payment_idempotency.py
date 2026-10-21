"""Payment idempotency and schema fixes

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-04

This migration adds database-enforced idempotency for payment callbacks
and fixes schema issues identified in T-004 audit:

1. Unique index on payments(provider, provider_ref) for idempotency
2. Unique index on webhook_events(source, event_ref) for idempotency
3. Add currency column to payments
4. Change payments.subscription_id from VARCHAR to UUID with FK

Notes:
- The unique index on payments(provider, provider_ref) is partial
  (WHERE provider_ref IS NOT NULL) because provider_ref is nullable at
  initiation and only set after the provider responds.
- The Payment.method enum is already named "payment_method_sub" in the
  migration-built schema (migration 0005, line 250) and the model now
  declares the same name, so no enum rename is needed here. The old
  model declared "payment_method", which collided with the Sale enum of
  the same name — that was a model-only defect, fixed in the model.
- Indexes are created without CONCURRENTLY: Alembic runs migrations
  inside a transaction and PostgreSQL forbids CONCURRENTLY there. The
  payments and webhook_events tables are small (per-business SaaS), so
  the brief lock is acceptable.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Unique index on payments(provider, provider_ref) for idempotency.
    # Partial because provider_ref is NULL at payment initiation.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_payments_provider_ref ON public.payments (provider, provider_ref) "
        "WHERE provider_ref IS NOT NULL"
    )

    # 2. Unique index on webhook_events(source, event_ref) for idempotency
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_webhook_events_source_ref ON public.webhook_events (source, event_ref)"
    )

    # 3. Add currency column to payments (default 'kes')
    op.add_column(
        "payments",
        sa.Column("currency", sa.String(3), nullable=False, server_default="kes"),
    )

    # 4. Change payments.subscription_id from VARCHAR to UUID with FK.
    # Fails loudly if any existing row holds a non-UUID value, which is
    # the correct behaviour for a data-integrity migration.
    op.execute(
        "ALTER TABLE payments ALTER COLUMN subscription_id TYPE UUID "
        "USING subscription_id::uuid"
    )
    op.create_foreign_key(
        "fk_payments_subscription_id",
        "payments",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )


def downgrade() -> None:
    # Drop FK first
    op.drop_constraint("fk_payments_subscription_id", "payments", type_="foreignkey")

    # Revert subscription_id to VARCHAR
    op.execute(
        "ALTER TABLE payments ALTER COLUMN subscription_id TYPE VARCHAR "
        "USING subscription_id::text"
    )

    # Drop currency column
    op.drop_column("payments", "currency")

    # Drop unique indexes
    op.execute("DROP INDEX IF EXISTS uq_webhook_events_source_ref")
    op.execute("DROP INDEX IF EXISTS uq_payments_provider_ref")
