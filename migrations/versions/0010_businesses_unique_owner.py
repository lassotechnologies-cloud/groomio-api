"""One business per owner, one live subscription per business

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-03

**STATUS: WRITTEN, NOT APPLIED.** This file has never been executed against any
database — neither the isolated local test database nor production. It is gated
behind the read-only pre-flight check in
`migrations/preflight_0010_readonly.sql`; see
`docs/T-003-business-onboarding-concurrency.md` for the deployment runbook.

Why a constraint and not just application code
----------------------------------------------
`POST /v1/business` reads "does this owner already have a business?" and then
inserts one. Those two statements are not atomic, so two concurrent requests for
the same owner — a double-tapped button, two tabs, a lost response and a retry —
both read "no", both insert, and both return 201. The owner ends up owning two
tenants, each with its own branch and its own trial subscription.

That is not merely a duplicate row. Five read paths resolve a tenant with
`SELECT ... WHERE owner_user_id = :uid` and no `LIMIT 1`
(`core.tenancy.resolve_business_id`, plus the queue, appointments and customers
routers), so once a duplicate exists those endpoints return an *arbitrary* one of
the two businesses for the same token. The index is what stops that from being
reachable in the first place.

`owner_user_id` is NOT NULL, so this is a plain unique index and not a partial
one: `WHERE owner_user_id IS NOT NULL` would restrict the index to every row
while quietly encoding a product rule (an owner may release one shop and open
another) that Groomio does not have.

Why the subscriptions index is partial
--------------------------------------
`subscriptions.business_id` is NOT unique today, and the duplicates are real: the
concurrent-create race above opened two `trial` rows for one shop.

But a blanket unique index there would forbid something the codebase explicitly
anticipates. `_current_subscription` is documented as "the live subscription:
trial, active, or grace. **Expired rows are history**", every live read filters
on exactly that status set, and `_apply_payment` extends `current_period_end` on
the existing row rather than inserting a new one — so billing renewals never need
a second row, but expired history rows are expected to accumulate.

The partial index therefore encodes only the invariant that is actually true and
already relied upon: **a business cannot hold two live subscriptions at once**,
while leaving expired history rows free to exist.

Pre-flight gate
---------------
Both statements below will fail if the corresponding duplicates exist, which is
the intended behaviour. Run `migrations/preflight_0010_readonly.sql` first and
resolve any rows it reports. Do NOT reconcile by deleting: no row in any
`businesses` table is deleted or merged by this project without an explicit,
recorded decision by the product owner.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Build both unique indexes without taking a write lock on the tables.

    `CREATE UNIQUE INDEX CONCURRENTLY` takes only a brief SHARE UPDATE EXCLUSIVE
    lock, so reads and writes on `businesses` and `subscriptions` continue while
    the index is built — a non-concurrent build would take an ACCESS EXCLUSIVE
    lock and make these tables effectively read-only for the duration.

    `CONCURRENTLY` cannot run inside a transaction block, so both statements are
    issued inside `autocommit_block()`, which commits between them.

    If the build fails, PostgreSQL leaves an INVALID index behind with the name
    `uq_businesses_owner_user_id` or `uq_subscriptions_live_business`. An invalid
    index enforces nothing, so the failure is loud but not silently protective.

    **Invalid-index recovery procedure:**

    1. **Detect:** After any failed migration attempt, run the detection query
       from `preflight_0010_readonly.sql` (CHECK 4) to confirm which index(es)
       are invalid (`is_valid = false`).
    2. **Drop:** Execute `DROP INDEX CONCURRENTLY IF EXISTS` for each invalid
       index. This is safe to run even if the index is already valid — it is a
       no-op in that case.
    3. **Rebuild:** Re-run `alembic upgrade 0010`. The migration will rebuild
       both indexes from scratch.
    4. **Verify:** After the rebuild, re-run the detection query. Both indexes
       must report `is_valid = true` and `is_unique = true` before proceeding.

    **Rules:**
    - Never assume the constraint is in place merely because the index name
      exists. Always verify `is_valid = true`.
    - Do not attempt to `REINDEX` or otherwise repair an invalid index — drop
      and rebuild from scratch.
    - The recovery procedure is idempotent.
    """
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_businesses_owner_user_id ON public.businesses (owner_user_id)"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_subscriptions_live_business ON public.subscriptions (business_id) "
            "WHERE status IN ('trial', 'active', 'grace')"
        )

    # `ix_businesses_owner_user_id` (migration 0002) is now strictly redundant:
    # a unique index on the same single column serves the same lookups. It is
    # deliberately NOT dropped here. Dropping it saves write amplification on a
    # hot column, but it is an independent, independently reversible change and
    # bundling it into the correctness fix only enlarges the rollback surface.
    # Do it as a separate, separately approved step after this migration has
    # been verified in production.


def downgrade() -> None:
    """Drop both indexes. Non-destructive: dropping an index discards no data.

    Consequence to understand before rolling back: `POST /v1/business` stops
    being able to *detect* a duplicate owner at the database level. It will
    still be correct for traffic that goes through the endpoint, because the
    application serialises concurrent submissions on the owner's user row — but
    that protection is now the only one, and it applies to exactly one code
    path. Background jobs, admin tooling, manual SQL and any future writer are
    no longer constrained.

    The reverse asymmetry is the important one: an index can be dropped at any
    time, but rows written while it was absent remain forever, and re-adding it
    afterwards will fail if any duplicates were created in the meantime. Treat
    the downgrade as a temporary measure for incident response, not as a way to
    "turn the feature off".
    """
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_businesses_owner_user_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_subscriptions_live_business")