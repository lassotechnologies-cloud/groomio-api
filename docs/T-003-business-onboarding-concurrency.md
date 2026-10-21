# T-003 — Business Onboarding Concurrency & Data Integrity

**Status: IMPLEMENTED AND VERIFIED LOCALLY. ACCEPTED FOR LOCAL DEVELOPMENT.
Migration 0010 is WRITTEN BUT NOT APPLIED. No production or remote database
was contacted at any point. Production deployment remains blocked pending
read-only pre-flight gate clearance and explicit authorization.**

This document was originally a design-only analysis (sections 0–9 below). It has
since been implemented and verified. The implementation record is **§10**, the
deployment runbook is **§11**, and the residual risk register is **§12**.
Sections 0–9 are retained unchanged because they are the reasoning the
implementation is shaped by.

**Safety boundary held throughout:** nothing was run against Supabase or any
remote database, migration 0010 was never applied to any database, no schema was
changed on any live database, and no business record was deleted or merged.

---

## 0. Evidence base

| Item | Value |
| --- | --- |
| Database used for all evidence | `postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test` (container `groomio-test-postgres`) |
| Server version | PostgreSQL **16.15** |
| Production / Supabase | **Never contacted.** Verified by running the harness from `/tmp` with all config passed via environment, so `groomio-api/.env` (which holds the Supabase pooler URL) is never loaded. |
| Repository files modified | **None.** The harness lived at `/tmp/t003/repro2.py` and was copied in only for the duration of each run, then deleted. |
| Model/migration parity (`python -m migrations.check_parity`) | **PARITY OK** — 38 tables, 340 columns |
| Baseline suite (`test_business_onboarding.py` + `test_integration_guards.py`) | **57 passed** |

---

## 1. Root-cause analysis

### 1.1 Current transaction boundaries

[`get_db()`](groomio-api/app/core/database.py:39) yields one `AsyncSession` per request and commits **after the endpoint returns**, rolling back on any exception. So **one HTTP request = one database transaction**. Nothing in the request path commits early.

[`create_business()`](groomio-api/app/business/router.py:179) runs entirely inside that single transaction:

| Step | Code | SQL |
| --- | --- | --- |
| 1. Read | [`load_onboarding_state()`](groomio-api/app/onboarding/state.py:92) | `SELECT … FROM businesses WHERE owner_user_id = :uid` — **no lock** |
| 2. Insert tenant | [`router.py:230`](groomio-api/app/business/router.py:230) | `INSERT INTO businesses …` (`flush`, not commit) |
| 3. Insert first branch | [`_open_first_branch()`](groomio-api/app/business/router.py:137) | `INSERT INTO branches …` |
| 4. Insert trial | [`router.py:244`](groomio-api/app/business/router.py:244) | `INSERT INTO subscriptions …` |
| 5. Commit | [`database.py:44`](groomio-api/app/core/database.py:44) | `COMMIT` — *after* the handler returned |

`flush()` issues SQL but does not commit. Under PostgreSQL's default `READ COMMITTED`
isolation, rows 2–4 stay invisible to every other transaction until step 5.

### 1.2 The exact concurrent failure scenario

This is a textbook **check-then-act / TOCTOU** defect. Two `POST /v1/business`
requests for the same owner, T1 and T2:

```
T1: SELECT businesses WHERE owner_user_id = U   -> 0 rows
T2: SELECT businesses WHERE owner_user_id = U   -> 0 rows   (T1 has not committed)
T1: INSERT businesses (owner_user_id = U)       -> OK
T2: INSERT businesses (owner_user_id = U)       -> OK  <-- nothing stops this
T1: COMMIT
T2: COMMIT                                       -> owner now owns TWO tenants
```

The read in step 1 and the write in step 2 are not atomic and nothing between
them takes a lock. Any of these produce T1 and T2: a double-tapped submit button,
a proxy or client-side retry, two browser tabs, or a lost response followed by a
retry.

### 1.3 Confirmed empirically — real concurrent database test

Real PostgreSQL, real commits, real HTTP requests through the ASGI app, two
independent connections via `asyncio.gather`. **Current, unmodified code.**

| # | Scenario | Responses | Rows created |
| --- | --- | --- | --- |
| C0 | **2 distinct owners**, 1 request each, concurrent *(control)* | `201`, `201` | 1 business each — correct |
| C1 | 1 owner, 2 concurrent, **same** branch name | `201`, **unhandled `UniqueViolation`** | 1 business, 1 branch, 1 sub |
| **C2** | **1 owner, 2 concurrent, different branch names** | **`201`, `201`** | **2 businesses, 2 branches, 2 subscriptions** |
| C3 | 1 owner, **5** concurrent, distinct branch names | `201`, `201`, `200`, `200`, `200` | **2 businesses**, 2 branches, 2 subs |
| C4 | 1 owner, **3 sequential** requests | `201`, `200`, `200` | 1 business — correct |
| C5 | 1 owner, 2 concurrent, same name, **widened window** | `201`, **unhandled `UniqueViolation`** | 1 business |
| **C6** | **1 owner, 2 concurrent, different names, widened window** | **`201`, `201`** | **2 businesses**, 2 branches, 2 subs |

Conclusions:

* **C2 / C6 are the defect.** Two businesses, two branches, **two trial
  subscriptions** for a single owner, both requests reporting success. The
  `branches.slug` unique index is what usually *masks* the bug — when both
  requests carry the same branch name the loser dies on the slug constraint
  instead (C1), so the duplicate business is prevented by accident, not by design.
  Change the branch name, or let the owner use two tabs, and the mask is gone.
* **C4 proves this is purely a concurrency defect**, not a retry defect. The
  sequential idempotency path in T-002 is sound and needs no repair.
* **C0 proves the harness is sound** and that two different owners are unaffected.
* C5/C6 additionally show the defect survives widening the read→write gap, so it
  is not an artefact of a freak interleaving.

### 1.4 Secondary defect — unexplained 500

[`_unique_slug()`](groomio-api/app/business/router.py:79) is a read-then-write loop
(`SELECT` for a free slug, then insert). Under concurrency two transactions pick
the same slug, the loser's `flush()` raises `UniqueViolationError`, which
propagates out of the handler → `get_db` rolls back → FastAPI returns **500**.

This is exactly the failure mode the T-002 design set out to eliminate: the
owner sees an opaque server error, with no explanation and no safe retry story.
There is **no `IntegrityError` handling anywhere in the business router**.

### 1.5 Tertiary defect — duplicate trials

`subscriptions.business_id` carries a plain index only. The model docstring says
*"One per business"* but nothing enforces it — confirmed by C2 producing two
`trial` subscriptions.

### 1.6 Why existing duplicates are worse than they look

Five read paths resolve a tenant with `SELECT … WHERE owner_user_id = :uid` and
**no `LIMIT 1`**: [`tenancy.py:74`](groomio-api/app/core/tenancy.py:74)
(`resolve_business_id`, used by most of the app), [`business/router.py:117`](groomio-api/app/business/router.py:117),
[`queue/router.py:49`](groomio-api/app/queue/router.py:49),
[`appointments/router.py:53`](groomio-api/app/appointments/router.py:53),
[`customers/router.py:47`](groomio-api/app/customers/router.py:47).

With a duplicate present these return an **arbitrary** row. So a duplicate tenant
does not merely sit there — it makes tenant resolution non-deterministic across
the queue, appointments and customers endpoints. That is a live correctness risk,
which is why reconciliation cannot be deferred indefinitely.

---

## 2. Existing unique constraints and indexes

From the live isolated test database:

```
businesses_pkey                  UNIQUE (id)
ix_businesses_owner_user_id      (owner_user_id)          <-- NOT unique. The gap.
branches_pkey                    UNIQUE (id)
branches_slug_key                UNIQUE (slug)
ix_branches_business_id          (business_id)
subscriptions_pkey               UNIQUE (id)
ix_subscriptions_business_id      (business_id)            <-- NOT unique
ix_subscriptions_plan_id         (plan_id)
```

There are **no foreign keys** between `businesses` / `branches` / `subscriptions`.
That is why nothing else happened to catch the duplicates.

---

## 3. Could existing data violate the proposed constraint?

| Scope | Result |
| --- | --- |
| Isolated test DB (`…:5433/groomio_test`) | **0 businesses** — cannot violate. Nothing to reconcile. |
| **Production (Supabase)** | **NOT CHECKED — deliberately.** The task forbids connecting to Supabase, so I have not verified production data and will not guess about it. |

This is the single most important open item. **The migration must not be applied
until a read-only pre-flight query has been run against production** (see §5.1).
The index build will fail loudly if duplicates exist — that is the correct
behaviour, and it is why the pre-flight query is a gate rather than a nicety.

---

## 4. Reconciliation plan for existing duplicates

Deliberately **manual and explicit**. No migration will delete, merge or rewrite
anything.

**Step 1 — detect (read-only, run against production by an operator):**

```sql
SELECT owner_user_id, count(*) AS n,
       array_agg(id ORDER BY created_at) AS business_ids
FROM businesses
GROUP BY owner_user_id
HAVING count(*) > 1
ORDER BY n DESC, owner_user_id;
```

**Step 2 — size the blast radius per duplicate owner:**

```sql
-- run per duplicate owner_id
SELECT b.id,
       (SELECT count(*) FROM branches      br WHERE br.business_id = b.id::text) AS branches,
       (SELECT count(*) FROM subscriptions  s WHERE s.business_id = b.id::text) AS subscriptions,
       (SELECT count(*) FROM appointments   a WHERE a.business_id = b.id::text) AS appointments,
       (SELECT count(*) FROM sales          sa WHERE sa.business_id = b.id::text) AS sales
FROM businesses b
WHERE b.owner_user_id = :owner
ORDER BY b.created_at;
```

**Step 3 — decide, per owner, with the product owner.** Proposed default policy
(**needs approval**): keep the **earliest** `businesses` row (the one the owner's
token already resolves to, and normally the one holding the branches), and mark
the later ones `status = 'suspended'` — *not* deleted — so nothing is lost and the
constraint can be applied after they stop being live rows. If a later row holds
real trading data (appointments/sales), merging is a **product decision**, not a
migration decision, and must happen before the index is built.

**Step 4 — re-run step 1. Only proceed when it returns zero rows.**

---

## 5. Migration as designed (§10 has the as-built version)

> As-designed text, retained for the reasoning. The migration **is now written**
> — [`0010_businesses_unique_owner.py`](groomio-api/migrations/versions/0010_businesses_unique_owner.py)
> — and was **never applied** to any database. See §10.1 and §11.

### 5.1 Pre-flight gate

Step 4's query above must return zero rows. If it does not, stop.

### 5.2 The change

```sql
-- 0010_businesses_unique_owner
CREATE UNIQUE INDEX CONCURRENTLY uq_businesses_owner_user_id
    ON public.businesses (owner_user_id);
```

**On the "partial index" the brief anticipated — a partial index is not needed
here, and should not be used.** Verified on the live schema:
`businesses.owner_user_id` is `character varying NOT NULL` (`is_nullable = NO`).
A plain unique index is exactly equivalent to a partial index restricted to
`WHERE owner_user_id IS NOT NULL`, because no row can be NULL. A partial index
would only become necessary if the product later adds soft-release semantics
(e.g. `WHERE released_at IS NULL`, allowing an owner to start a second shop after
closing the first). **That does not exist today** and inventing it now would
encode a product decision that has not been made. If "one owner, one shop,
forever" is *not* the intended product rule, say so now and the design changes
to a partial index.

Design notes:

* **`CONCURRENTLY`** — the index build takes a brief `SHARE UPDATE EXCLUSIVE`
  lock, so it does not block reads or writes on `businesses`, and a production
  table will not go read-only during deploy. `CONCURRENTLY` **cannot run inside a
  transaction block**, so the migration must use `autocommit_block()`.
* **Version compatibility** — unique indexes and `CONCURRENTLY` have been
  supported since PostgreSQL 9.2; verified working on the **16.15** server here.
  Fully compatible with Supabase PostgreSQL.
* **Existing redundant index** — `ix_businesses_owner_user_id` becomes redundant
  once the unique index exists. Dropping it saves a write amplification on a hot
  column, but it is **not required** for correctness. Proposed as a separate,
  independently reversible step (`DROP INDEX CONCURRENTLY ix_businesses_owner_user_id`)
  so it can be deferred or skipped without blocking the fix.
* **Model parity is mandatory, and the parity checker will not catch a mistake.**
  [`check_parity.py`](groomio-api/migrations/check_parity.py:57) compares
  **tables and columns only** — it does not inspect indexes or constraints. Also,
  the test suite builds its schema with `Base.metadata.create_all`
  ([`conftest.py:88`](groomio-api/tests/conftest.py:88)), **not** with Alembic, and
  the test DB has **no `alembic_version` table**. Therefore the model must
  declare the index, or the tests will run against a schema that does not match
  production:

  ```python
  # app/models/tenant.py — class Business
  __table_args__ = (
      Index("uq_businesses_owner_user_id", "owner_user_id", unique=True),
  )
  ```

  `tests/test_id_type_consistency.py` or the parity checker should ideally be
  extended to assert index parity as well — flagged as a follow-up, not part of
  this change.

> **Done.** [`check_parity.py`](groomio-api/migrations/check_parity.py) now also
  compares named indexes — both `op.create_index(...)` and raw
> `CREATE UNIQUE INDEX …` DDL — and **exits 1** on any T-003 index drift. Verified
> by negative control (§10.5). Pre-existing, unrelated drift is reported
  informationally so it does not mask a real regression.

### 5.3 Rollback

```python
def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_businesses_owner_user_id")
    # Optional, only if the redundancy drop was applied:
    # op.create_index("ix_businesses_owner_user_id", "businesses", ["owner_user_id"])
```

Rollback is safe and non-destructive: dropping a unique index discards no data.
The application-side protection in §6 is designed to be **safe to keep while
rolled back** (it is a no-op when no conflict can occur), and **safe to deploy
before** the migration. **Recommended order: application change first, migration
second** — that way the endpoint never returns a raw 500 even on the window
between deploys.

---

## 6. Application-level protection (smallest change)

Exactly **one** write path exists for `Business` ([`business/router.py:219`](groomio-api/app/business/router.py:219))
and exactly **one** for `Subscription` ([`business/router.py:245`](groomio-api/app/business/router.py:245)).
The fix is therefore contained to one function.

Three layers, in order of authority:

**Layer 1 — serialise per owner (removes the race deterministically, no schema needed).**
Inside `create_business`, before the read, take a row lock on the *owner's user
row*:

```python
await db.execute(text("SELECT 1 FROM users WHERE id = :uid FOR UPDATE"), {"uid": user_id})
```

The `users` row always exists (it was verified by `ActiveUser` before we get
here), so every concurrent onboarding request for the same owner serialises on
it. The existing `load_onboarding_state()` read then happens *inside* the lock
and sees the committed winner. `FOR UPDATE` on `users` — a row no other
onboarding path contends for — is far cheaper than `SERIALIZABLE` and avoids
aborting either transaction.

**Layer 2 — the database constraint is the guarantee.**
The unique index is what makes the invariant true regardless of application
code, future endpoints, background jobs, or manual SQL.

**Layer 3 — handle the conflict instead of returning 500.**
Belt-and-braces for the window before the migration lands, and for any residual
race: wrap the insert block in a savepoint (`db.begin_nested()`), catch
`IntegrityError`, roll back **to the savepoint only**, re-read the existing
business, and return it with **200** — the same replay semantics T-002 already
established. Bounded retry for the `_unique_slug` race.

Properties this preserves:

| Requirement | How |
| --- | --- |
| Concurrent submissions safe | Layer 1 serialises; Layer 2 forbids |
| Return existing business on valid replay | Unchanged replay branch, still `200` |
| No unexplained 500 | Layer 3 converts `IntegrityError` into a replay |
| No partial onboarding on failure | Already guaranteed by `get_db`'s rollback; the savepoint means a handled conflict does **not** discard an otherwise-good transaction |
| No duplicate branch/trial | Both are created only inside the winning insert block, which is now unique |
| Tenant isolation | The lock key is the `user_id` from the **token**, never from the payload; `BusinessCreateRequest` keeps `extra="ignore"` |

Explicitly **not** proposed: weakening any auth/authz check, changing `409`
semantics back, adding an `onboarding_step` column, or touching
[`app/onboarding/state.py`](groomio-api/app/onboarding/state.py) semantics.

---

## 7. Files that would change

> As-designed list. The as-built list is in **§10.1**.

| File | Change |
| --- | --- |
| [`groomio-api/app/models/tenant.py`](groomio-api/app/models/tenant.py) | Add `__table_args__` unique `Index` on `Business.owner_user_id` (keeps `create_all` test schema in parity) |
| `groomio-api/migrations/versions/0010_businesses_unique_owner.py` | **New.** `upgrade()` = `CREATE UNIQUE INDEX CONCURRENTLY`; `downgrade()` = `DROP INDEX CONCURRENTLY` |
| [`groomio-api/app/business/router.py`](groomio-api/app/business/router.py) | `FOR UPDATE` on the owner row; savepoint + `IntegrityError` → replay 200; bounded `_unique_slug` retry |
| `groomio-api/tests/conftest.py` | **New fixture required** — see §8.2 |
| `groomio-api/tests/test_business_onboarding_concurrency.py` | **New.** The regression suite in §8 |

Nothing else. No other module, router, model or test is touched.

---

## 8. Regression test plan (as designed)

> As-designed plan, T1–T12. The suite is now written as **14 tests** in
> [`test_business_onboarding_concurrency.py`](groomio-api/tests/test_business_onboarding_concurrency.py);
> results in §10.5.

### 8.1 Required tests

| # | Test | Asserts |
| --- | --- | --- |
| T1 | Two simultaneous creates, one owner, **same** branch name | Both 2xx; exactly **1** business, 1 branch, 1 subscription |
| T2 | Two simultaneous creates, one owner, **different** branch names | Both 2xx; exactly **1** business (this is the C2 regression) |
| T3 | 5 simultaneous creates, one owner | Exactly **1** business, 1 branch, 1 subscription |
| T4 | Sequential retries (×5) | `201` then `200`s; exactly 1 business; name not overwritten |
| T5 | Lost response after successful commit, then retry | Second call returns `200` with the **same** `business.id` |
| T6 | Business exists but first branch missing → retry | Branch created **under the same business**; still exactly 1 business |
| T7 | Duplicate trial prevention | Exactly 1 subscription row; `current_period_end` unchanged by retries |
| T8 | Two different owners, concurrent | 2 businesses, 2 branches, distinct ids **and distinct slugs** |
| T9 | Ownership not settable by payload | Posting `owner_user_id`/`business_id` does not change ownership |
| T10 | Cross-tenant access still rejected | Tenant B gets `404` on A's business and branch; no regression to isolation |
| T11 | **Database uniqueness enforcement** | Raw SQL inserting a second business for the same owner raises `IntegrityError` — proves the *database*, not just the app |
| T12 | No 500 under concurrency | Every concurrent response is `2xx`; none is `5xx` |

### 8.2 The blocking test-infrastructure problem

The existing [`db` fixture](groomio-api/tests/conftest.py:78) binds **one**
connection inside **one** transaction that is always rolled back, and the test
`client` fixture shares that same session ([`test_business_onboarding.py:33`](groomio-api/tests/test_business_onboarding.py:33)).

**That fixture structurally cannot express concurrency.** Two "simultaneous"
requests through it share one session on one connection in one transaction: they
cannot interleave, they cannot commit independently, and a rollback discards both.
A concurrency test written against it would pass while the bug is live — a
false-green, which is exactly what must be avoided here.

So T1–T3 and T11 need a **new fixture** that:
* uses `AsyncSessionLocal` (real pool, real connections, **real commits**);
* overrides `get_db` with a session-per-request so concurrent requests get
  genuinely separate transactions;
* cleans up only the rows it created (the suite may not truncate tables another
  test is using);
* runs only when `TEST_DATABASE_URL` is set and the name matches the existing
  test-DB guard ([`conftest.py:63`](groomio-api/tests/conftest.py:63)).

The suite must never be disabled or weakened to accommodate this, and T11 must
skip cleanly (not silently pass) when no database is configured.

### 8.3 Definition of done

Concurrency is only claimed as fixed when T1, T2, T3, T11 and T12 pass against
a real concurrent PostgreSQL transaction, **with the migration applied**. The
§1.3 harness is a reusable, standalone check for exactly this claim.

---

## 9. Remaining risks

1. **Production data is unverified.** I did not connect to Supabase, so I cannot
   state whether duplicates exist today. If they do, §4 must run first and the
   migration must not proceed. *This is the top risk.*
2. **`CREATE UNIQUE INDEX CONCURRENTLY` through Supabase's pgbouncer** in
   transaction mode: it is a single DDL statement and should be fine, but it
   should be confirmed against the project's actual pooler on a scratch branch
   before the real deploy.
3. **Supabase migrations vs. the app's `create_all` test schema** can silently
   diverge, because `check_parity.py` verifies columns only. A test that passes
   locally may be exercising a schema production does not have.
4. **Existing duplicates already make tenant resolution non-deterministic** (§1.6)
   in the queue, appointments and customers endpoints. The index stops new ones;
   it does not fix the ones already committed.
5. **`docker-compose.test.yml` maps `5432`, but the mandated test URL is `5433`.**
   Anyone following `setup_test_db.sh` verbatim will target the wrong port — and
   port 5432 on this machine is a *different* Postgres (`groomie-postgres`).
   Worth reconciling separately; I have not changed it.
6. **Long-held transactions** on `businesses` could stall a `CONCURRENTLY` build
   (it waits for existing transactions). Worth checking for long-running
   transactions in production before the deploy window.

> **Update after implementation:** risk 5 is now **resolved** —
> [`docker-compose.test.yml`](groomio-api/docker-compose.test.yml) and
> [`setup_test_db.sh`](groomio-api/setup_test_db.sh) both use `127.0.0.1:5433`.
> Risk 3 is **mitigated** — the parity checker now verifies indexes, not just
> columns (§10.5). Risks 1, 2, 4 and 6 remain open and are carried into the
> register in §12.

---

## 10. Implementation record (2026-10-03)

### 10.1 Files changed

| File | Change |
| --- | --- |
| [`groomio-api/app/models/tenant.py`](groomio-api/app/models/tenant.py) | `Business.__table_args__` — plain unique `Index("uq_businesses_owner_user_id", "owner_user_id")`. Required so the test schema (`create_all`) matches production. |
| [`groomio-api/app/models/billing.py`](groomio-api/app/models/billing.py) | `Subscription.__table_args__` — **partial** unique `Index("uq_subscriptions_live_business", "business_id") WHERE status IN ('trial','active','grace')`. |
| [`groomio-api/app/business/router.py`](groomio-api/app/business/router.py) | `SELECT … FOR UPDATE` on the owner row; create block wrapped in a savepoint with `IntegrityError` → replay 200; `_unique_slug` replaced by `_insert_branch_with_free_slug` with a bounded, savepoint-scoped retry; driver-agnostic `_is_unique_violation_on`. |
| [`groomio-api/migrations/versions/0010_businesses_unique_owner.py`](groomio-api/migrations/versions/0010_businesses_unique_owner.py) | **New.** `CREATE UNIQUE INDEX CONCURRENTLY` ×2. **Never executed.** |
| [`groomio-api/migrations/preflight_0010_readonly.sql`](groomio-api/migrations/preflight_0010_readonly.sql) | **New.** Read-only production pre-flight. **Never executed.** |
| [`groomio-api/migrations/check_parity.py`](groomio-api/migrations/check_parity.py) | Extended with index/constraint parity (§5.2 risk 3). |
| [`groomio-api/tests/conftest.py`](groomio-api/tests/conftest.py) | New `concurrency_engine`, `sessions`, `concurrency_client` fixtures; `lock_timeout`; 5433 URL documented. |
| [`groomio-api/tests/test_business_onboarding_concurrency.py`](groomio-api/tests/test_business_onboarding_concurrency.py) | **New.** 14 real-concurrency tests. |
| [`groomio-api/docker-compose.test.yml`](groomio-api/docker-compose.test.yml) | Port `5432` → `127.0.0.1:5433`. |
| [`groomio-api/setup_test_db.sh`](groomio-api/setup_test_db.sh) | Documented port 5433. |

Nothing else. No other router, model, test or endpoint was touched. No
authentication, authorisation or tenancy check was weakened, and no existing test
was removed, disabled or relaxed.

### 10.2 The subscription decision — a partial index, and why

The brief required inspecting the subscription relationship before constraining
it. The evidence:

* `_current_subscription()` is documented as *"The live subscription: trial,
  active, or grace. **Expired rows are history**"*, and every live read filters
  on exactly that status set.
* `_apply_payment()` — the **only** writer of subscription state — extends
  `current_period_end` **on the existing row**. A billing renewal never inserts a
  second row.
* [`app/workers/subscription_state.py`](groomio-api/app/workers/subscription_state.py)
  moves rows trial → grace → expired **in place**.

So renewals never need a second row, but expired history rows legitimately
accumulate. A blanket unique index on `subscriptions.business_id` would be safe
*today*, but would silently forbid a lapsed business from ever returning with a
fresh row — a product decision nobody has made, encoded as a constraint.

The **partial** index therefore states only what is already true and already
relied upon: *a business cannot hold two live subscriptions at once*. Expired
history remains free to exist. This is also precisely the defect §1.3 observed
(a duplicated trial).

Contrast with `businesses.owner_user_id`, where `NOT NULL` makes a plain unique
index exactly equivalent to a partial one — so that one is plain, as instructed.

### 10.3 Three bugs the concurrency tests caught

Each was invisible to the existing suite:

1. **`AsyncSessionTransaction` has no `close()`.** The async nested-transaction
   object exposes only `commit`/`rollback`. Releasing a savepoint is `commit()`,
   which emits `RELEASE SAVEPOINT` — **not** a real `COMMIT`. The data stays
   inside the request's transaction and is still discarded if that rolls back.
2. **`table_name` is not on `exc.orig`.** The real chain is
   `IntegrityError → AsyncAdapt_asyncpg_dbapi.UniqueViolationError →
   asyncpg.UniqueViolationError`; the adapter re-exports `sqlstate` but **not**
   `table_name`. The first version of the conflict check read `orig.diag.table_name`
   (a psycopg2 shape) and so *never matched* — silently restoring the original
   unexplained-500 behaviour for every duplicate. Fixed by walking the exception
   chain (up to 5 levels) and supporting both driver layouts, matching on
   SQLSTATE `23505` **plus** table name.
3. **Join type mismatch.** `businesses.id` is UUID while
   `branches.business_id` is VARCHAR; joining them directly is a PostgreSQL type
   error.

### 10.4 Why the tests are believed

`test_requests_genuinely_block_on_the_owner_lock` holds the owner's user row in a
separate session and asserts a launched request is **still unfinished**. That is
what rules out the false green: if the requests did not genuinely overlap, "one
201 and the rest 200" would also be produced by running them one after another —
which the pre-fix code also does. Scheduling uses task creation on the event loop
and a bounded `lock_timeout`, **not** arbitrary sleeps as the only mechanism.

**Negative control.** Both protective layers were temporarily disabled (unique
index removed from the model, `FOR UPDATE` replaced with `pass`) and the suite
re-run. `test_requests_genuinely_block_on_the_owner_lock` **failed**, exactly as
it should. Both files were restored from backup and the suite returned to
`14 passed`. A test that cannot fail is not evidence.

### 10.5 Verification results

| Check | Command | Result |
| --- | --- | --- |
| T-003 targeted | `pytest tests/test_business_onboarding_concurrency.py -q` | **14 passed**, 0 failed, 0 skipped, 1 warning (17.07s; also 24.82s on a prior run) |
| Full backend suite | `pytest tests/ -q` | **437 passed**, 0 failed, 0 skipped, 33 warnings (586.46s) |
| Model + column + index parity | `python -m migrations.check_parity` | **PARITY OK**, exit 0 — 38 tables, 340 columns, 36 model indexes vs 38 migration indexes |
| Index parity (negative control) | T-003 index temporarily removed from model | Checker **exited 1**: `businesses.uq_businesses_owner_user_id: created by a migration but absent from the model` |
| Frontend TS | `npx tsc --noEmit` (groomio-web) | **Clean**, exit 0 |
| Frontend contract | `node scripts/check-onboarding-contract.mjs` | **11 checks passed**, exit 0 |
| Frontend build | `npm run build` (groomio-web) | **Success**, exit 0 |

Baseline before the change (`test_business_onboarding.py` +
`test_integration_guards.py`) was **57 passed** — unchanged.

Database used for every test run:
`postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test`,
PostgreSQL 16.15, container `groomio-test-postgres`. `TEST_DATABASE_URL`,
`DATABASE_URL` and `SECRET_KEY` were exported explicitly for every run so the
Supabase URL in [`groomio-api/.env`](groomio-api/.env) was never used. Port 5432
(the `groomie-postgres` development database) was not touched.

Warnings across the full suite: Passlib `crypt` deprecation, 30× Pillow
`Image.Image.getdata` deprecation, Argon2 `__version__` access deprecation. All
pre-existing and unrelated.

### 10.6 Honest limits of these results

* **The migration was never applied.** The indexes exist in the test schema only
  because the tests build it from the SQLAlchemy models. Equivalence with
  migration 0010 is asserted by the parity checker, not by executing
  `alembic upgrade` anywhere.
* **14 tests is not a concurrency stress test.** The gate serialises requests
  deterministically, which proves correctness but does not explore randomised
  interleavings. That is the right trade for a regression suite; a soak test would
  add value under load.
* **No production data was examined**, so whether duplicates exist today is still
  unknown. This remains the top risk and blocks deployment.

---

## 11. Deployment runbook

> **Nothing in this section has been executed.** It requires an authorized
> database administrator or deployment operator. Steps 0 and 1 are hard gates.

### Step 0 — Pre-flight (read-only, run by an operator)

```bash
psql "$PRODUCTION_DATABASE_URL" -f migrations/preflight_0010_readonly.sql
```

Every statement is a `SELECT`. Record the full output, the operator's name and
the timestamp in the change ticket.

**Gate conditions — both must hold:**

* **Check 1 returns zero rows** (no duplicate `owner_user_id`).
* **Check 2 returns zero rows** (no business with two live subscriptions).

If either returns rows: **STOP.** Do not apply the migration, and do not delete or
merge anything. Reconciliation is a product decision (§4), not a migration
decision. Escalate.

Check 2 counts only `trial`/`active`/`grace`. Multiple **expired** rows for one
business are legitimate billing history, are *expected*, and must not be
"fixed".

Also review **Check 5** (long-running transactions). `CREATE INDEX CONCURRENTLY`
waits for every transaction that started before it, so a stuck session will make
the deploy appear to hang.

### Step 1 — Deploy the application code

The application change is **safe to deploy before the migration** and does not
require it. Recommended order is application first, precisely so the window
between the two deploys can never produce an unexplained 500.

After this, the owner-row lock serialises concurrent submissions, so duplicate
businesses stop occurring even without the index.

### Step 2 — Apply the migration

```bash
alembic upgrade 0010
```

Both indexes are built `CONCURRENTLY`, taking only a brief
`SHARE UPDATE EXCLUSIVE` lock — reads and writes continue. Because
`CONCURRENTLY` cannot run inside a transaction block, the migration uses
`autocommit_block()`.

**Watch for a partial failure.** A failed `CREATE INDEX CONCURRENTLY` leaves an
**invalid** index behind under the same name. An invalid index enforces nothing
while appearing present. If the migration fails:

```sql
SELECT c.relname, i.indisvalid
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname IN ('uq_businesses_owner_user_id', 'uq_subscriptions_live_business');
```

Drop the invalid index (`DROP INDEX CONCURRENTLY …`), fix the cause, re-run. Do
not assume the constraint is in place merely because the name exists.

### Step 3 — Verify

```sql
-- both rows must report is_unique = true AND is_valid = true
SELECT c.relname, i.indisunique, i.indisvalid, pg_get_indexdef(c.oid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname IN ('uq_businesses_owner_user_id', 'uq_subscriptions_live_business');
```

### Rollback

```bash
alembic downgrade 0009
```

Non-destructive — dropping an index discards no data.

**Understand the limitation before rolling back.** A unique index can be dropped
at any time, but:

* After rollback the **database no longer prevents** duplicate ownership. The
  owner-row lock still serialises traffic through `POST /v1/business`, so that
  one path stays correct — but background jobs, admin tooling, manual SQL and any
  future writer are no longer constrained.
* The asymmetry is the important part: an index can be dropped instantly, but any
  row written while it was absent persists forever. **Re-applying it will fail if
  a duplicate was created in the meantime.** Re-run the pre-flight first.
* Treat the downgrade as an incident-response measure, not a feature toggle.

### Optional follow-up (separate, separately approved)

`ix_businesses_owner_user_id` (from migration 0002) is now redundant — a unique
index on the same single column serves the same lookups. Dropping it saves write
amplification on a hot column.

```sql
DROP INDEX CONCURRENTLY ix_businesses_owner_user_id;
```

Deliberately **not** bundled into 0010 — that would enlarge the rollback surface
for no correctness benefit. Note that removing it from the model as well would
require updating the parity checker's exemption list.

---

## 12. Remaining risks

1. **Production data is unverified.** Supabase was never contacted, so whether
   duplicates exist today is unknown. *This blocks deployment until Step 0
   returns zero rows.*
2. **Pre-existing duplicates already make tenant resolution non-deterministic**
   (§1.6) in the queue, appointments and customers endpoints. The index prevents
   new ones; it does not repair committed ones. If Step 0 finds duplicates they
   need a product decision before anything else proceeds.
3. **Tests build schema from models, not migrations.** The tests prove the
   *model's* constraints hold. The parity checker is a static check and ties that
   to migration 0010, but it cannot catch a migration whose SQL diverges from the
   model's intent. Mitigated, not eliminated.
4. **Six pre-existing index drifts** are now visible and reported informationally
   by `check_parity.py`: `ix_users_phone`, `ix_users_email`, `ix_users_role`,
   `ix_businesses_status`, `ix_branches_latitude_longitude`, and
   `appointments.ix_appt_customer` (model-only — `create_all` makes it in tests,
   but no migration creates it in production). They predate this change and were
   left alone.
5. **`CREATE UNIQUE INDEX CONCURRENTLY` through Supabase's pgbouncer** in
   transaction mode should be confirmed against the real pooler on a scratch
   branch before the production deploy.
6. **Long-held transactions** can stall a `CONCURRENTLY` build indefinitely.
   Pre-flight Check 5 exists for this.
7. **`ix_businesses_owner_user_id` is left in place** by choice — safe, but
   redundant write amplification until the follow-up is done.
8. **Not production-ready.** This work is local implementation and verification
   only, and stops here pending separate deployment authorization.
9. **Six pre-existing index drifts** (`ix_users_phone`, `ix_users_email`,
   `ix_users_role`, `ix_businesses_status`, `ix_branches_latitude_longitude`,
   `appointments.ix_appt_customer`) are tracked separately in
   `check_parity.py` and must **not** be bundled into migration 0010. They
   predate this change and fixing them is a separate, independently approved
   change.

---

## 13. Deployment-readiness gate closure (2026-10-04)

### 13.1 Decision

**T-003 is accepted for local development.** Groomio may proceed with planning
the next milestone.

**Production deployment remains blocked** until the following gates are cleared
by an authorized DBA or deployment operator:

1. **Pre-flight gate:** The read-only pre-flight
   [`preflight_0010_readonly.sql`](groomio-api/migrations/preflight_0010_readonly.sql)
   must be executed against the actual production database. Checks 1 and 2 must
   both return zero rows. If either returns rows, **stop for data
   reconciliation** — do not apply the migration, and do not delete or merge
   anything. Reconciliation is a product decision, not a migration decision.

2. **Invalid-index recovery procedure:** The hardened recovery procedure
   documented in [`preflight_0010_readonly.sql`](groomio-api/migrations/preflight_0010_readonly.sql)
   (INVALID-INDEX RECOVERY PROCEDURE) and in
   [`0010_businesses_unique_owner.py`](groomio-api/migrations/versions/0010_businesses_unique_owner.py)
   must be understood and available to the operator. If a `CREATE INDEX
   CONCURRENTLY` fails during deployment, the operator must:
   - Detect the invalid index via the CHECK 4 query
   - Drop it with `DROP INDEX CONCURRENTLY`
   - Re-run `alembic upgrade 0010`
   - Verify `is_valid = true` before proceeding

3. **Explicit authorization:** Once both gates pass, the deployment plan must be
   reviewed and the migration explicitly authorized before `alembic upgrade 0010`
   is executed.

### 13.2 Index drift tracking

The six pre-existing index drifts are **not** bundled into migration 0010. They
are tracked separately by [`check_parity.py`](groomio-api/migrations/check_parity.py)
and reported informationally. Fixing them requires a separate, independently
approved change. In particular:

- `appointments.ix_appt_customer` is model-only — `create_all` makes it present
  in tests, but no migration creates it in production. This is the most
  operationally significant drift because it affects appointment lookups.
- The other five drifts (`ix_users_phone`, `ix_users_email`, `ix_users_role`,
  `ix_businesses_status`, `ix_branches_latitude_longitude`) are migration-only
  indexes that exist in production but are absent from the models.

None of these drifts block T-003's local acceptance, but they must be addressed
in a future milestone.

### 13.3 Next steps for production deployment

| Step | Owner | Action |
| --- | --- | --- |
| 1 | DBA / deployment operator | Run `preflight_0010_readonly.sql` against production |
| 2 | DBA / deployment operator | Record output in change ticket; if Checks 1 or 2 return rows, escalate for data reconciliation |
| 3 | DBA / deployment operator | Confirm no invalid indexes exist (CHECK 4) |
| 4 | Product owner | Review deployment plan and explicitly authorize migration 0010 |
| 5 | Deployment operator | Execute `alembic upgrade 0010` |
| 6 | Deployment operator | Verify both indexes report `is_valid = true` and `is_unique = true` |
| 7 | Team | Monitor for any anomalies post-deploy |

**Until all seven steps complete, production deployment of migration 0010 is
not authorized.**