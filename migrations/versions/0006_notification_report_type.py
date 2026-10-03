"""add `report` to the notification_type enum

The daily digest worker (Doc 10.11) needs a notification type of its own. Filing
it under `campaign` would have worked and been wrong: an inbox that cannot tell
"your Tuesday takings" apart from "we are running a promo" cannot be filtered,
and the shop owner is the one reading it.

Adding a value to a PostgreSQL enum requires recreating the type, so this is a
four-step migration rather than an ALTER. The old rows are copied across before
the type is dropped, so nothing is lost if it fails partway — the rename leaves
`notification_type` absent and the transaction rolls back, rather than leaving a
half-migrated enum that accepts no writes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

VALUES = ("appointment", "queue", "subscription", "birthday", "campaign", "report")


def upgrade() -> None:
    # Postgres cannot add a value to a type used by a column inside a transaction
    # before PG12, so the whole enum is rebuilt. The old type is kept under a
    # temp name and only dropped once the column points at the new one.
    op.execute("ALTER TYPE notification_type RENAME TO notification_type_old")
    op.execute(
        "CREATE TYPE notification_type AS ENUM (%s)"
        % ", ".join(f"'{v}'" for v in VALUES)
    )
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN type TYPE notification_type "
        "USING type::text::notification_type"
    )
    op.execute("DROP TYPE notification_type_old")


def downgrade() -> None:
    # Rows written as `report` have no pre-0006 equivalent. They become
    # `campaign` rather than failing the downgrade, so rolling back never
    # destroys a notification.
    op.execute("UPDATE notifications SET type = 'campaign' WHERE type = 'report'")

    op.execute("ALTER TYPE notification_type RENAME TO notification_type_old")
    op.execute(
        "CREATE TYPE notification_type AS ENUM "
        "('appointment', 'queue', 'subscription', 'birthday', 'campaign')"
    )
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN type TYPE notification_type "
        "USING type::text::notification_type"
    )
    op.execute("DROP TYPE notification_type_old")
