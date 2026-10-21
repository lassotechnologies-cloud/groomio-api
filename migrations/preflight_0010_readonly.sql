-- ============================================================================
-- T-003 — READ-ONLY PRE-FLIGHT FOR MIGRATION 0010
-- ============================================================================
--
-- PURPOSE
--   Determine whether `alembic upgrade 0010` would succeed, before anyone runs
--   it. The migration creates two UNIQUE indexes:
--
--     uq_businesses_owner_user_id        ON businesses (owner_user_id)
--     uq_subscriptions_live_business     ON subscriptions (business_id)
--                                        WHERE status IN ('trial','active','grace')
--
--   Each will FAIL if the corresponding duplicates already exist. That failure
--   is by design — it is the database refusing to accept data it considers
--   invalid. This script finds that data first, while there is still time to
--   decide what to do about it.
--
-- SAFETY
--   Every statement below is a SELECT. This script cannot modify data, cannot
--   take a lock that blocks writes, and cannot start a transaction that
--   interferes with the application. It is safe to run against production
--   during normal traffic.
--
--   Run it with autocommit / read-only, e.g.:
--     psql "postgresql://.../postgres?options=-c%20default_transaction_read_only%3Don"
--   or simply `psql -c "<paste>"` and rely on the fact that nothing here writes.
--
-- AUTHORIZATION REQUIRED
--   This script has NOT been run by the author of T-003. Connecting to
--   production was explicitly out of scope for that work.
--
--   An authorized database administrator or deployment operator must execute
--   this script and record the output in the change ticket BEFORE migration
--   0010 is deployed.
--
-- DECISION RULE
--   Every check must return the expected "clear" shape before 0010 may be
--   scheduled. Any non-empty result from checks 1 or 2 is a STOP: it means
--   business records exist whose resolution requires a product decision.
--   Reconciliation is never automatic — no row is deleted or merged by this
--   project without an explicit, recorded decision.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- CHECK 0 — Environment sanity. Confirms which database this really is.
-- Run this FIRST. It is the cheapest possible guard against running the rest of
-- this script against the wrong server.
-- ----------------------------------------------------------------------------
SELECT current_database()                    AS database,
       current_user                           AS connected_as,
       inet_server_addr()                     AS server_addr,
       inet_server_port()                     AS server_port,
       version()                              AS postgres_version,
       current_setting('transaction_read_only') AS read_only;


-- ----------------------------------------------------------------------------
-- CHECK 1 — GATE. Duplicate owners in `businesses`.
--
-- EXPECTED RESULT: 0 rows.
-- ANY ROW HERE BLOCKS MIGRATION 0010.
--
-- This is the data the migration cannot build an index over. Note that a
-- duplicate is not merely cosmetic: `core.tenancy.resolve_business_id` and four
-- other read paths select the owner's business with no LIMIT 1, so while this
-- returns rows, tenant resolution is non-deterministic across the queue,
-- appointments and customers endpoints.
--
-- For each owner returned, the second query below sizes what is at stake
-- before anyone decides anything.
-- ----------------------------------------------------------------------------
SELECT owner_user_id,
       count(*)                                    AS business_count,
       array_agg(id::text        ORDER BY created_at) AS business_ids,
       array_agg(name            ORDER BY created_at) AS business_names,
       array_agg(status::text    ORDER BY created_at) AS statuses,
       min(created_at)                             AS first_created,
       max(created_at)                             AS last_created
FROM businesses
GROUP BY owner_user_id
HAVING count(*) > 1
ORDER BY business_count DESC, owner_user_id;

-- Run once per owner_user_id returned above. Read-only.
--   SELECT * FROM t003_duplicate_blast_radius WHERE owner = '<owner_user_id>';
WITH dup_owners AS (
    SELECT owner_user_id FROM businesses GROUP BY owner_user_id HAVING count(*) > 1
)
SELECT b.owner_user_id,
       b.id::text                                   AS business_id,
       b.name,
       b.status::text                               AS status,
       b.created_at,
       (SELECT count(*) FROM branches      br WHERE br.business_id = b.id::text) AS branches,
       (SELECT count(*) FROM services      sv WHERE sv.branch_id IN
                    (SELECT id::text FROM branches WHERE business_id = b.id::text)) AS services,
       (SELECT count(*) FROM subscriptions  s  WHERE s.business_id = b.id::text) AS subscriptions,
       (SELECT count(*) FROM payments      p  WHERE p.business_id = b.id::text) AS payments,
       (SELECT count(*) FROM appointments  a  WHERE a.business_id = b.id::text) AS appointments,
       (SELECT count(*) FROM sales         sa WHERE sa.business_id = b.id::text) AS sales
FROM businesses b
JOIN dup_owners d ON d.owner_user_id = b.owner_user_id
ORDER BY b.owner_user_id, b.created_at;


-- ----------------------------------------------------------------------------
-- CHECK 2 — GATE. Businesses with more than one LIVE subscription.
--
-- EXPECTED RESULT: 0 rows.
-- ANY ROW HERE BLOCKS MIGRATION 0010.
--
-- Scope is deliberately the live statuses only — `trial`, `active`, `grace` —
-- because that is exactly the predicate of uq_subscriptions_live_business.
-- Multiple `expired` rows for one business are legitimate billing history and do
-- NOT block the migration. Do not "fix" them.
--
-- The third query below deliberately over-reports: it counts every status
-- including `expired`. It is there to show how much history exists, so that a
-- reviewer can see that the partial index was chosen precisely so that this
-- history is preserved rather than forbidden.
-- ----------------------------------------------------------------------------
SELECT s.business_id,
       count(*)                       AS live_subscription_count,
       array_agg(s.id::text ORDER BY s.created_at) AS subscription_ids,
       array_agg(s.status::text ORDER BY s.created_at) AS statuses,
       array_agg(s.plan_id  ORDER BY s.created_at)   AS plan_ids,
       b.owner_user_id
FROM subscriptions s
LEFT JOIN businesses b ON b.id::text = s.business_id
WHERE s.status IN ('trial', 'active', 'grace')
GROUP BY s.business_id, b.owner_user_id
HAVING count(*) > 1
ORDER BY live_subscription_count DESC;

-- Informational. Expected: at most one row per business that has ever been
-- billed. More than one is FINE and must be left alone.
SELECT business_id,
       count(*)                                        AS all_status_rows,
       count(*) FILTER (WHERE status = 'expired')      AS expired_history_rows,
       count(*) FILTER (WHERE status <> 'expired')     AS live_rows
FROM subscriptions
GROUP BY business_id
HAVING count(*) > 1
ORDER BY all_status_rows DESC;


-- ----------------------------------------------------------------------------
-- CHECK 3 — Column assumptions this migration relies on.
--
-- EXPECTED: owner_user_id NOT NULL, status NOT NULL. If `owner_user_id` were
-- ever nullable, a plain unique index would still be correct (PostgreSQL treats
-- NULLs as distinct, permitting many NULL rows), but the reasoning documented in
-- the migration would be stale and the partial-index decision should be
-- revisited.
-- ----------------------------------------------------------------------------
SELECT table_name,
       column_name,
       data_type,
       is_nullable,
       character_maximum_length
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (table_name, column_name) IN (
        ('businesses',   'owner_user_id'),
        ('businesses',   'status'),
        ('subscriptions','business_id'),
        ('subscriptions','status'))
ORDER BY table_name, column_name;

-- EXPECTED: business_status = 'business_status' and
--            subscription_status = 'subscription_status'.
-- The migration's partial-index predicate compares against the enum type by
-- name; if either enum was renamed, the WHERE clause in uq_subscriptions_
-- live_business would need updating before the migration is applied.
SELECT t.typname AS enum_type, e.enumlabel
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.typname IN ('business_status', 'subscription_status')
ORDER BY t.typname, e.enumsortorder;


-- ----------------------------------------------------------------------------
-- CHECK 4 — Existing index state. Tells the operator whether this is a first
-- deploy or a retry after a partially-failed one.
--
-- EXPECTED: `valid = true` for both indexes, if present at all.
-- An index with `valid = false` is a leftover from a `CREATE INDEX CONCURRENTLY`
-- that failed: it enforces NOTHING and must be dropped and rebuilt before the
-- migration is considered applied.
-- ----------------------------------------------------------------------------
SELECT c.relname                                   AS index_name,
       i.indisunique                              AS is_unique,
       i.indisvalid                               AS is_valid,
       i.indisready                               AS is_ready,
       pg_get_indexdef(c.oid)                     AS definition,
       t.relname                                   AS table_name
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('uq_businesses_owner_user_id',
                    'uq_subscriptions_live_business',
                    'ix_businesses_owner_user_id')
ORDER BY c.relname;


-- ----------------------------------------------------------------------------
-- CHECK 5 — Operational precondition for CONCURRENTLY.
--
-- `CREATE UNIQUE INDEX CONCURRENTLY` cannot proceed until every transaction that
-- started before it has finished. A long-running transaction or an idle-in-
-- transaction session will make the migration wait indefinitely, and the
-- deploy will look hung.
--
-- EXPECTED: no rows older than a few minutes. If rows appear, find and end the
-- blocking sessions before the deploy window. Do not kill them without
-- understanding what they are doing.
-- ----------------------------------------------------------------------------
SELECT pid,
       state,
       now() - xact_start AS transaction_age,
       now() - state_change AS idle_since,
       wait_event_type,
       left(query, 120) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND xact_start IS NOT NULL
  AND now() - xact_start > interval '5 minutes'
ORDER BY xact_start;

-- Long-held transactions specifically on the two tables being indexed.
SELECT pid, state, now() - xact_start AS transaction_age, left(query, 120) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND xact_start IS NOT NULL
  AND query ILIKE ANY (ARRAY['%businesses%', '%subscriptions%'])
  AND now() - xact_start > interval '1 minute'
ORDER BY xact_start;


-- ----------------------------------------------------------------------------
-- CHECK 6 — Referential context, informational only.
--
-- Note the absence of foreign keys: `businesses`, `branches` and `subscriptions`
-- are related by convention, not by constraint. That is why nothing in the
-- schema caught the duplicate ownership while it accumulated, and it is why the
-- pre-flight above is a hard gate rather than a formality.
-- ----------------------------------------------------------------------------
SELECT b.id::text AS business_id,
       b.owner_user_id,
       b.name,
       b.status::text,
       (SELECT count(*) FROM branches WHERE business_id = b.id::text) AS branches,
       (SELECT count(*) FROM subscriptions
         WHERE business_id = b.id::text
           AND status IN ('trial','active','grace')) AS live_subscriptions
FROM businesses b
ORDER BY b.created_at
LIMIT 100;

-- Branches with no parent business, and businesses with no branch at all.
-- Neither blocks migration 0010. Both are worth knowing about: a business with
-- no branch is a half-finished onboarding, and T-003's replay path is what
-- completes it on the owner's next submission.
SELECT 'branch_without_business' AS issue, br.id::text AS id, br.slug
FROM branches br
LEFT JOIN businesses b ON b.id::text = br.business_id
WHERE b.id IS NULL
UNION ALL
SELECT 'business_without_branch', b.id::text, NULL
FROM businesses b
WHERE NOT EXISTS (SELECT 1 FROM branches br WHERE br.business_id = b.id::text);

-- ============================================================================
-- INVALID-INDEX RECOVERY PROCEDURE
-- ============================================================================
--
-- `CREATE UNIQUE INDEX CONCURRENTLY` leaves an INVALID index behind if it fails.
-- An invalid index enforces nothing while appearing present in the catalog.
-- This section documents the detection and recovery procedure. It is NOT
-- executed by this script — it is a runbook for the operator.
--
-- DETECTION (run after any failed migration attempt):
--
--   SELECT c.relname                                   AS index_name,
--          i.indisunique                              AS is_unique,
--          i.indisvalid                               AS is_valid,
--          i.indisready                               AS is_ready,
--          pg_get_indexdef(c.oid)                     AS definition,
--          t.relname                                   AS table_name
--   FROM pg_index i
--   JOIN pg_class c ON c.oid = i.indexrelid
--   JOIN pg_class t ON t.oid = i.indrelid
--   JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE n.nspname = 'public'
--     AND c.relname IN ('uq_businesses_owner_user_id',
--                       'uq_subscriptions_live_business')
--   ORDER BY c.relname;
--
-- EXPECTED: both rows show is_valid = true. If any row shows is_valid = false,
-- that index is a leftover from a failed CONCURRENTLY build and must be dropped
-- before the migration can be re-run.
--
-- RECOVERY (execute only after confirming invalid indexes exist):
--
--   -- Drop the invalid index(es). CONCURRENTLY is safe even if the index is
--   -- already valid — it is a no-op in that case.
--   DROP INDEX CONCURRENTLY IF EXISTS uq_businesses_owner_user_id;
--   DROP INDEX CONCURRENTLY IF EXISTS uq_subscriptions_live_business;
--
--   -- Re-run the migration:
--   alembic upgrade 0010
--
-- IMPORTANT RULES:
--
-- 1. Never assume the constraint is in place merely because the index name
--    exists. Always verify is_valid = true after the build completes.
-- 2. Do not attempt to REINDEX or otherwise repair an invalid index — drop it
--    and rebuild from scratch.
-- 3. If the migration fails again, repeat the detection step. Do not proceed
--    to Step 3 (Verify) until both indexes report is_valid = true.
-- 4. The recovery procedure is idempotent: running DROP INDEX CONCURRENTLY
--    against a non-existent or already-valid index is harmless.
-- ============================================================================
--
-- END OF PRE-FLIGHT
--
-- Nothing above has been executed. Record the operator's name, the timestamp and
-- the full output in the change ticket. Checks 1 and 2 must both be empty before
-- migration 0010 is approved for deployment.
-- ============================================================================