# T-004 — Subscription & Billing Integration Audit

**Status:** Audit complete. No fixes applied. No migrations written or applied. No production or remote database touched.
**Date (UTC):** 2026-10-04
**Auditor:** Kilo Code (read-only technical audit)
**Workspace:** `/Users/macbook/Documents/Internal-Projects/groomie`

---

## 1. Executive summary

Groomio's subscription and billing system is **partially implemented and not production-ready**. The backend contains a well-designed, defensively-written billing state machine (`app/subscriptions/router.py`, `app/workers/subscription_state.py`, `app/workers/payment_reconciler.py`) with strong intent around idempotency, fail-closed payment application, and tenant isolation. However, **three defects are confirmed by reproduction against the isolated test database**, and together they mean **no subscription can be activated end-to-end today**:

1. **P0 — Payment forgery (unauthenticated).** The M-Pesa Daraja callback endpoint accepts a fabricated callback and activates a subscription without any money moving, because the `CheckoutRequestID` issued by Safaricom is never matched against the `provider_ref` stored at initiation. (Reproduced.)
2. **P1 — Payment initiation is broken (500 on every attempt).** `POST /v1/subscription/pay` passes a raw `uuid.UUID` into the `payments.subscription_id` VARCHAR column, which asyncpg rejects. Every payment initiation raises `DBAPIError` → HTTP 500. (Reproduced.)
3. **P1 — Pesapal IPN signature verification is wrong.** `verify_pesapal_ipn_signature` hashes the fields with **no separators**, but Pesapal's documented algorithm joins them with single spaces. Every genuine Pesapal IPN is therefore rejected and left unprocessed, so Pesapal payments never activate. (Reproduced.)

On top of these, the **frontend has no integration with the billing API at all**: the subscription screen posts to a route that does not exist, sends a plan *code* instead of a plan *UUID*, and the dashboard's "Renew" button points at a route that does not exist. The onboarding flow therefore cannot complete the subscription step.

**Bottom line:** the subscription onboarding workflow is blocked at the payment step. The defects below are ordered by severity; the implementation sequence in §13 is ordered by dependency, not severity.

---

## 2. Scope and safety confirmation

**Safety rules observed (all of them):**

| Rule | Status |
|---|---|
| Do not connect to Supabase | ✅ Not present in this codebase; no Supabase client imported. |
| Do not connect to any production or remote database | ✅ Only `localhost:5433/groomio_test` was used. |
| Do not use production payment credentials | ✅ No credentials used; provider calls were pointed at `127.0.0.1:9` (unroutable) or bypassed entirely. |
| Do not initiate real M-Pesa or Pesapal transactions | ✅ No HTTP request left the host. The Daraja callback was delivered to the local ASGI app over loopback with a fabricated body. |
| Do not send SMS/email/payment callbacks to external providers | ✅ None sent. |
| Do not apply or create database migrations | ✅ No migrations applied. `0010` remains written-but-unapplied. |
| Do not modify database schemas | ✅ No DDL issued against any database. |
| Do not change application source code | ✅ No source file modified. Verified by mtime scan (§12). |
| Do not modify existing tests | ✅ No test file modified. |
| Do not delete/reset/truncate/seed existing development data | ✅ The dev DB on port 5432 was never contacted. The test DB was used only inside a transaction that was **rolled back**. |
| Do not weaken auth/authz/tenant isolation/payment verification | ✅ None weakened. |
| Do not install new dependencies | ✅ None installed. |
| Do not claim a payment integration works because an endpoint exists | ✅ Each provider is classified below with evidence. |
| Do not claim production readiness | ✅ Explicit assessment in §20. |

**Isolated test database used:** `postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test` (container `groomio-test-postgres`, published `127.0.0.1:5433:5432`). The development database on `127.0.0.1:5432` (`groomie-postgres`) was confirmed present and **left untouched**.

**Reproduction harness:** a throwaway script at `/tmp/t004_audit_repro.py` (outside the repository) drove the real FastAPI ASGI app over loopback against the isolated test DB inside a single transaction that was rolled back. It is referenced as evidence, not committed.

---

## 3. Actual architecture and endpoint inventory

### Backend (FastAPI, `groomio-api/app`)

| Group | Route | Method | Auth | Purpose |
|---|---|---|---|---|
| H2 | `/v1/plans` | GET | public | Plan catalogue (price from DB). |
| H1 | `/v1/subscription` | GET | owner | Live subscription + last 20 payments. |
| H3 | `/v1/subscription/pay` | POST | owner | Initiate a payment (writes `payments` row, then calls provider). |
| H6 | `/v1/payments/{payment_id}` | GET | owner | Poll a payment (scoped by business). |
| H4 | `/v1/payments/webhooks/daraja` | POST | **public** | M-Pesa STK callback. |
| H5 | `/v1/payments/webhooks/pesapal` | POST | **public** | Pesapal IPN. |
| B1 | `/v1/business` | POST | owner (active) | Create business + first branch + trial subscription. |
| B2 | `/v1/business` | GET | owner | Business summary + active plan code. |
| B3 | `/v1/business` | PATCH | owner | Owner-editable fields only. |
| H7 | `/v1/admin/trial-extend` | POST | super_admin | Add N days to a trial. |
| H8 | `/v1/admin/tenants` | GET | super_admin | Tenant list. |
| H9 | `/v1/admin/tenants/{id}` | PATCH | super_admin | Suspend/reactivate. |
| H10 | `/v1/admin/dashboard` | GET | super_admin | Platform metrics. |
| Onboarding | `/v1/onboarding/status` | GET | onboarding\|access | Derived onboarding step. |

**Key modules:**
- `app/models/billing.py` — `Plan`, `Subscription`, `Payment`.
- `app/models/tenant.py` — `Business`, `Branch`, `Service`.
- `app/models/money.py` — `Sale` (note: `Sale.payment_method` enum, see §11).
- `app/core/payments.py` — Daraja STK push + Pesapal order + callback verification (no DB access).
- `app/core/deps.py` — `get_current_user`, `get_active_user`, `get_onboarding_user`, `require_role`.
- `app/core/tenancy.py` — `TenantIsolationMiddleware`, `resolve_business_id`, `require_role`.
- `app/subscriptions/router.py` — the entire billing state machine.
- `app/workers/subscription_state.py` — daily trial→grace→expired sweep.
- `app/workers/payment_reconciler.py` — 5-minute webhook drain.
- `app/workers/subscription_reminders.py` — daily trial/renewal notices.
- `app/workers/__init__.py` — Celery app + beat schedule.

### Frontend (`groomio-web`)

| Route | File | Notes |
|---|---|---|
| `/onboarding/subscription` | `app/onboarding/subscription/page.tsx` | **Posts to `/onboarding/subscription` (no such route).** Sends `plan_id` = plan *code* (`"monthly"`), `payment_method` (not `method`). Redirects to `/dashboard` on any 2xx. |
| `/onboarding/subscription` (legacy) | `app/onboarding/subscription.tsx` | **Dead/shadowed file.** Same URL as the page above; Next.js resolves the `page.tsx`. This file is unreachable and should be deleted. |
| `/dashboard` | `app/dashboard/page.tsx` | "Renew" button → `router.push("/subscription")` → **404** (no such route). All dashboard data is hardcoded mock data; no live subscription query. |
| `/more/payments` | `app/more/payments/page.tsx` | Stub: "Payment settings coming soon." |
| API client | `lib/api-client.ts` | Bearer-token `fetch` wrapper; 401 → clear session + redirect to `/login`. |
| Auth | `lib/auth.tsx` | Token in `localStorage.access_token`; role from token. |
| Onboarding client | `lib/onboarding.ts` | `destinationForStep("subscription") === "/dashboard"` — **deliberately skips the billing screen** (see §9). |

**No frontend code calls `GET /v1/plans`, `POST /v1/subscription/pay`, or `GET /v1/payments/{id}`.** The billing API is entirely unwired on the client.

### Infrastructure

- **Celery** (`app/workers/__init__.py`): broker + backend = `redis://localhost:6379/0`. Beat schedule registers `subscription-state` (daily 00:05 EAT), `subscription-reminders` (daily 08:00 EAT), `payment-reconciler` (every 5 min), plus campaign/appointment/daily-report/chat-retention jobs.
- **Docker** (`docker-compose.test.yml`): isolated Postgres 16 on `127.0.0.1:5433` for tests.
- **Railway** (`railway.toml`): three services from one repo — API (`alembic upgrade head && uvicorn`), worker (`celery -A app.workers.celery_app worker`), beat (`celery -A app.workers.celery_app beat`). `numReplicas=1` on the API to avoid migration races.
- **CI** (`.github/workflows/ci.yml`): runs `pytest` against a throwaway Postgres on 5432 inside the job; fails if DB tests skip. Lints `import-error` only.
- **Env** (`.env.example`): all M-Pesa/Pesapal keys are empty by default; `MPESA_STK_PUSH_URL` defaults to the **STK Query** endpoint (see §5).

---

## 4. Subscription state machine

### Current states (DB enums)

`subscriptions.status` ∈ {`trial`, `active`, `grace`, `expired`}
`businesses.status` ∈ {`trialing`, `active`, `grace`, `expired`, `suspended`}

These are **two independent state machines** that are only partially kept in sync (see §8).

### Transitions

```
trial ──payment ok──▶ active ──period ends, unpaid──▶ grace ──▶ expired
  ▲                                                              │
  └──────────── payment ok (renewal) ────────────────────────────┘
```

| Transition | Driver | Enforced server-side? |
|---|---|---|
| trial → active | `_apply_payment` (on a success callback) | ✅ (only `pending` payments apply) |
| active → grace | `workers/subscription_state.py` (`current_period_end` passed + within grace window) | ✅ |
| grace → expired | `workers/subscription_state.py` (grace window elapsed) | ✅ |
| expired → active | **None.** `POST /v1/subscription/pay` 404s for an expired business (no live subscription). | ❌ No recovery path. |
| any → suspended | `admin/trial-extend` / `admin/tenants/{id}` (super_admin only) | ✅ |

### Findings on the state machine

- **No `cancelled` or `pending_payment` state** exists (matches the model). The task said not to introduce new states; none were.
- **Transitions are server-side** — the frontend cannot set `subscriptions.status` directly. ✅
- **Invalid transitions are rejected** by the whitelist in `_apply_payment` (`status != "pending"` → return). ✅
- **State changes are NOT atomic across the two machines.** `_apply_payment` updates `Subscription` + `Payment` in one flush, but **never updates `Business.status`**. So a paying customer's `businesses.status` stays `trialing` forever (see §8).
- **The frontend cannot modify subscription status.** ✅ (No endpoint accepts it.)
- **Race condition (check-then-act):** `_apply_payment` reads `payment.status` then writes it with no row lock (`SELECT ... FOR UPDATE`) and no DB-level guard. Two concurrent success callbacks for the same payment can both read `pending` and both extend the subscription. The application-level `status != "pending"` check is **not** a database-enforced guarantee. (See §7.)

---

## 5. Plan and pricing verification

### Expected vs actual

| Plan | Expected (KES) | Seeded (migration 0003) | Match |
|---|---|---|---|
| Daily | 40 | 40 | ✅ |
| Bi-Weekly | 500 | 500 | ✅ |
| Monthly | 1,000 | 1,000 | ✅ |
| Half-Yearly | 5,400 | 5,400 | ✅ |
| Yearly | 9,780 | 9,780 | ✅ |

Prices are formula-driven from `settings.daily_price_kes` (40) and the discount ladder, seeded once by migration 0003. ✅

### Trial / grace rules

| Rule | Expected | Actual | Match |
|---|---|---|---|
| Trial duration | 10 days | `settings.trial_days = 10`; `Business.trial_ends_at = now + 10d`; `Subscription.current_period_end = now + 10d` | ✅ |
| Grace period | 3 days | `settings.grace_days = 3`; worker moves `active/trial → grace` when `current_period_end <= now` and `> now - 3d` | ✅ |
| After grace expiry | restrict access, retain data | **No access restriction exists** (see §8 / T004-D). Data retained. | ❌ |
| Reactivation must not duplicate | no dup business/branch/trial | `POST /v1/business` is idempotent per owner (T-003 fix). But **reactivation from `expired` is impossible** (see §4). | ❌ |

### Pricing defects

- **P1 — M-Pesa STK push URL is the Query endpoint.** `settings.mpesa_stk_push_url` defaults to `https://sandbox.safaricom.co.ke/mpesa/stkpush/v2/query` (the **STK Query** endpoint, which takes a `CheckoutRequestID` and returns a status) in both [`app/core/config.py:31`](groomio-api/app/core/config.py:31) and `.env.example:48`. `daraja_stk_push` POSTs a full STK-push body to it. Safaricom's STK Push endpoint is `/mpesa/stkpush/v1/processrequest`. **As configured, no STK push can ever succeed.**
- **P2 — `PartyB` is the payment UUID, not the paybill/till.** In `daraja_stk_push`, `PartyB = str(account_ref)` (the payment UUID). For `CustomerPayBillOnline`, `PartyB` must be the paybill/till number (`settings.mpesa_shortcode`). Safaricom would reject this.
- **P3 — Frontend hardcodes prices.** `app/onboarding/subscription/page.tsx` ships a `PLANS` array with prices duplicated from the backend. Drift risk; the backend `GET /v1/plans` exists but is unused by the client.
- **P3 — Trial banner says 14 days.** The screen reads "14-Day Free Trial Active" while `settings.trial_days = 10`.

---

## 6. Payment provider integration matrix

| Aspect | M-Pesa Daraja | Pesapal | Manual bank transfer |
|---|---|---|---|
| Initiation endpoint | `POST /v1/subscription/pay` (method=`mpesa`) | `POST /v1/subscription/pay` (method=`pesapal`) | **Not implemented** (enum value exists but no route) |
| Required request fields | `plan_id` (UUID), `method` | same | n/a |
| Authentication | OAuth2 client-credentials (`mpesa_consumer_key/secret`) | HTTP basic (`pesapal_consumer_key/secret`) | n/a |
| Amount/currency validation | Amount from `Plan.price_kes`; callback amount re-checked | Amount from `Plan.price_kes`; callback amount re-checked | n/a |
| Provider reference handling | `AccountReference` = payment UUID (minted by us); `CheckoutRequestID` returned and stored in `provider_ref` | `merchant_reference` = payment UUID; redirect URL returned (not stored) | n/a |
| Callback endpoint | `POST /v1/payments/webhooks/daraja` (public) | `POST /v1/payments/webhooks/pesapal` (public) | n/a |
| Callback authenticity | **None.** Safaricom does not sign callbacks; verification is "does the `AccountReference` match a payment we issued". **The `CheckoutRequestID` is never matched.** | SHA-512 IPN signature, recomputed in constant time. **But the algorithm is wrong (see below).** | n/a |
| Payment status mapping | `ResultCode == 0` → success; else `failed` | `status == "completed"` → success; else `failed` | n/a |
| Subscription activation | `_apply_payment` (only `pending` payments) | `_apply_payment` (only `pending` payments) | n/a |
| Retry handling | Provider retries until 200; `payment_reconciler` drains unprocessed events | Same | n/a |
| Timeout handling | `timeout` status; reconciler re-opens after timeout | n/a | n/a |
| Duplicate callback handling | `payment.status != "pending"` guard (app-level only) | Same | n/a |
| Refund/reversal | **Not handled.** A late failure callback flips `success → failed` without reversing granted time. No `refunded` status. | n/a | n/a |
| Audit trail | `raw_callback` JSONB + `webhook_events` row | Same | n/a |
| Error handling | `PaymentProviderError` → payment marked `failed`, 502 to client | Same | n/a |

### Provider classification

- **M-Pesa Daraja:** Implemented, but **not correctly configured** (STK push URL is the Query endpoint; `PartyB` is wrong) and **insecure** (unsigned callback, `CheckoutRequestID` not verified — see §7).
- **Pesapal:** Implemented, but **signature verification is broken** (wrong field-joining algorithm), so real IPNs are rejected.
- **Manual bank transfer:** **Missing.** The `payment_method`/`payment_provider` enums include `bank_transfer`/`manual`, but no initiation or verification path exists.
- **Airtel Money:** **Missing.** Listed in the frontend as "Coming soon"; the backend `StartPaymentRequest.method` is `Literal["mpesa","pesapal"]` only.

---

## 7. Payment security and idempotency findings

### P0 — Forged Daraja callback activates a subscription (REPRODUCED)

**Evidence:** `/tmp/t004_audit_repro.py` (T004-A). A POST to `/v1/payments/webhooks/daraja` with a fabricated body — `CheckoutRequestID = "ATTACKER-INVENTED-CHECKOUT-ID"`, `AccountReference = <victim's pending payment UUID>`, `Amount = 1000`, `ResultCode = 0` — returned **HTTP 200** and moved the victim's subscription `trial → active`, advancing `current_period_end` by 30 days. No money moved.

**Root cause:** `daraja_webhook` matches the payment on `Payment.id == account_ref` and `Payment.provider == "daraja"` only. It **never compares** `event_ref` (the `CheckoutRequestID`) to `payment.provider_ref` (the `CheckoutRequestID` we stored at initiation). The docstring of `daraja_callback_is_ours` claims "the real authentication is the row: we only mark a payment successful when the CheckoutRequestID matches one we issued ourselves" — **the code does not do this.** Safaricom callbacks are unsigned, so the `CheckoutRequestID` match is the only available authentication, and it is missing.

**Impact:** Any party who knows (or guesses) a pending payment UUID — which is returned to the owner in `StartPaymentResponse.payment_id` — can activate a subscription for free. This is a direct financial-loss / unauthorized-activation defect.

**Fix:** In `daraja_webhook`, require `event_ref == payment.provider_ref` (the stored `CheckoutRequestID`) before applying. Additionally, query Safaricom's STK Query API to confirm the transaction before applying (defence in depth).

### P1 — Payment initiation raises 500 (REPRODUCED)

**Evidence:** `/tmp/t004_audit_repro.py` (T004-E). `POST /v1/subscription/pay` raised `sqlalchemy.exc.DBAPIError: invalid input for query argument $3: UUID(...) (expected str, got UUID)`.

**Root cause:** In `start_payment`, the `Payment` row is built with `subscription_id=sub.id` — a raw `uuid.UUID` — but `payments.subscription_id` is a `VARCHAR` column. asyncpg rejects the type mismatch. The same bug was already found and fixed for `plan_id` in `_open_trial_subscription` (which uses `str(trial_plan.id)`), but the identical fix was **not** applied in `start_payment`.

**Impact:** **Every** payment initiation fails with HTTP 500. No subscription can be activated. This is the single highest-blocker for the onboarding workflow.

**Fix:** `subscription_id=str(sub.id)` in `start_payment`.

### P1 — Pesapal IPN signature verification rejects genuine callbacks (REPRODUCED)

**Evidence:** In-memory check (T004-F). `verify_pesapal_ipn_signature` computes `SHA-512(notification_type + notification_id + notification_details + merchant_id)` — **no separators**. Pesapal's documented algorithm joins the four fields with single spaces. The implementation **accepts its own wrong signature and rejects the correct one**.

**Impact:** Every genuine Pesapal IPN is stored in `webhook_events` and left unprocessed. Pesapal payments never activate. The `payment_reconciler` would also reject them (it re-derives the same wrong signature).

**Fix:** Join the fields with single spaces: `f"{notification_type} {notification_id} {notification_details} {merchant_id}"`.

### P1 — No DB-enforced idempotency; check-then-act race

**Evidence (code):** `_apply_payment` (`app/subscriptions/router.py:150-176`) reads `payment.status` then writes it with no `SELECT ... FOR UPDATE` and no unique constraint on `provider_ref`. `payments` has **no unique constraint** on `provider_ref` or on `(provider, provider_ref)` (verified via `pg_indexes` on the test DB). `webhook_events` has **no unique constraint** on `(source, event_ref)` — replayed callbacks insert duplicate rows.

**Impact:** Two concurrent success callbacks for the same payment can both pass the `status == "pending"` check and both extend the subscription (double extension). The existing tests (`test_applying_twice_does_not_extend_twice`) only cover **sequential** re-application, not concurrency.

**Fix:** Add a unique constraint on `payments(provider, provider_ref)` (or a dedicated `idempotency_key`), and use `SELECT ... FOR UPDATE` (or an atomic `UPDATE ... WHERE status='pending'`) in `_apply_payment`.

### P1 — No recovery path from `expired`

**Evidence (code):** `start_payment` calls `_current_subscription`, which filters `status IN ('trial','active','grace')`. An expired business has no live subscription → `POST /v1/subscription/pay` returns **404 "No subscription yet"**. The `_apply_payment` docstring claims "A customer who pays after the grace period is a paying customer, not a lost one" — but the code makes that impossible.

**Impact:** A lapsed business cannot re-subscribe. The onboarding/renewal workflow is unrecoverable after expiry.

**Fix:** Allow `start_payment` to open a new subscription for an expired business (reusing the expired row's `plan_id`, or inserting a new row), and let `_apply_payment` transition `expired → active`.

### P1 — Reversal/refund not handled

**Evidence (code):** `daraja_webhook` sets `payment.status = "failed"` for any non-zero `ResultCode`, **even if the payment was already `success`**. It does not reverse the subscription time already granted. There is no `refunded`/`reversed` status in the `payment_status` enum.

**Impact:** A reversal after a success flips the payment to `failed` while the subscription keeps its granted time — a dispute vector and an accounting mismatch.

**Fix:** Distinguish "user cancelled before completion" (no time granted) from "reversal after success" (time must be clawed back or flagged). Add a `refunded` status and a reversal path.

### P2 — Pesapal `provider_ref` is the payment's own UUID, not a provider transaction reference

**Evidence (code):** On the Pesapal IPN, `payment.provider_ref = details.get("merchant_reference")` — which is the payment UUID we sent, not a Pesapal transaction id. Pesapal's `payment_reference`/`tracking_account_reference` is ignored.

**Impact:** Weak audit trail; cannot reconcile against Pesapal's records.

### P2 — `start_payment` has no duplicate-initiation guard

**Evidence (code):** `start_payment` writes a new `Payment` row and calls the provider on every request. No check for an existing `pending` payment for the same `(business_id, plan_id)`.

**Impact:** A double-tapped button creates two payment rows and two STK pushes.

**Fix:** Before inserting, look for an existing `pending` payment for the business and return it (idempotent initiation).

### P2 — Reconciler does not re-verify amount or `CheckoutRequestID`

**Evidence (code):** `payment_reconciler._reconcile` matches on `Payment.id == reference` only; it does not re-check the amount or the `CheckoutRequestID`. It trusts the stored payload.

**Impact:** A stored-but-unprocessed forged callback (P0) would be "reconciled" into a success by the background job. Defence in depth is missing.

### P3 — `webhook_events` grows unboundedly

**Evidence:** No unique constraint on `(source, event_ref)`; no TTL/cleanup job. Replayed callbacks insert duplicate rows forever.

### P3 — No currency column

**Evidence:** `payments` has `amount_kes` only; no `currency`. Pesapal order hardcodes `"currency": "KES"`. Acceptable for a KES-only product, but the absence of a currency field means a future multi-currency expansion would be unsafe.

---

## 8. Tenant authorization findings

### P1 — Expired/suspended businesses are NOT access-restricted (REPRODUCED)

**Evidence:** `/tmp/t004_audit_repro.py` (T004-D). With `businesses.status = 'expired'`, the owner still received **HTTP 200** on `GET /v1/business` and `GET /v1/customers`.

**Root cause:** `businesses.status` is only consulted by `app/booking/discovery.py` (public discovery, to hide expired shops from customers). **No authenticated endpoint enforces it.** There is no dependency, middleware, or decorator that blocks an expired/suspended tenant from reading or writing their data.

**Impact:** After grace expiry, a business retains full access to appointments, customers, sales, etc. — contradicting the product rule "business access should be restricted according to the existing product policy."

**Fix:** Add a tenant-status dependency (e.g. `require_active_business`) applied to business-scoped routers, returning 402/403 for `expired`/`suspended`.

### P2 — `businesses.status` and `subscriptions.status` drift

**Evidence (code):** `_apply_payment` updates `Subscription` but never `Business.status`. `subscription_state` only **downgrades** `Business.status` (trial/active/grace → grace/expired) and never **upgrades** it. So:
- A paying customer's `businesses.status` stays `trialing` forever.
- After a late payment, `businesses.status` stays `grace`/`expired` while `subscriptions.status` becomes `active`.

**Impact:** Admin metrics (`active_businesses`, `trialing_businesses`) are wrong; any future access-control logic built on `businesses.status` would lock out paying customers.

**Fix:** Have `_apply_payment` set `businesses.status = 'active'` on success, and keep the two machines in sync.

### P2 — Admin trial extension does not affect the billing clock

**Evidence (code):** `admin/trial-extend` (`app/admin/router.py:89`) sets `biz.trial_ends_at` only. The lifecycle worker keys off `Subscription.current_period_end`, which is untouched.

**Impact:** Support's "extend trial" button has no effect on when the shop actually loses access.

**Fix:** Extend `Subscription.current_period_end` in the same operation.

### ✅ Verified correct

- **Only owners manage billing:** `POST /v1/subscription/pay`, `GET /v1/subscription`, `GET /v1/payments/{id}` all use `dependencies=[OwnerOnly]` (`require_role("owner")`). ✅
- **Staff cannot access billing:** T004-C — a `clerk` got 403 on `/v1/subscription`. ✅ (Note: `/v1/plans` is intentionally public.)
- **Cross-tenant payment access blocked:** T004-B — owner B got 404 on owner A's payment. ✅
- **Business IDs derived server-side:** `resolve_business_id` resolves from the token's `user_id`, never from the request body. ✅
- **Plan IDs validated:** `start_payment` looks up `Plan.id == payload.plan_id AND is_active`. ✅
- **Prices from server:** `amount_kes = plan.price_kes`. ✅
- **Payment status not mutable via business update:** `PATCH /v1/business` only writes `name`, `logo_url`, `chat_enabled`. ✅
- **T-003 one-owner/one-business invariant:** `uq_businesses_owner_user_id` unique index exists (migration 0002 + model). ✅

---

## 9. Frontend subscription onboarding assessment

### The journey is broken at the payment step.

| Step | Status |
|---|---|
| Registration → OTP verification | OTP is **deferred** (`groomio-web/TODO.md`): signup creates `status="active"` directly, no OTP. Backend OTP endpoints remain intact. |
| Business creation | ✅ Wired correctly via `lib/onboarding.ts` → `POST /v1/business` (idempotent, server-derived). |
| Subscription selection | ❌ Hardcoded `PLANS` array; never calls `GET /v1/plans`. |
| Payment | ❌ `app/onboarding/subscription/page.tsx:69` posts to `/onboarding/subscription` — **no such route**. Sends `plan_id: "monthly"` (a code, not a UUID) and `payment_method` (not `method`). |
| Subscription status | ❌ No polling of `GET /v1/payments/{id}` or `GET /v1/subscription`. |
| Dashboard redirect | ❌ On any 2xx it `router.replace("/dashboard")` — i.e. it would redirect to the dashboard **without confirming payment**. A pending payment would unlock the business. |

### The T-002 mismatch is still present

The previous T-002 audit found `/onboarding/subscription` calling a non-existent route while the backend exposed `POST /v1/subscription/pay`. **This is still the case.** The screen's own comment (lines 26–42) documents the mismatch and explicitly defers fixing it.

### Additional frontend defects

- **P1 — Dashboard "Renew" → 404.** `app/dashboard/page.tsx:78` → `router.push("/subscription")`. No `/subscription` route exists (confirmed by `next build` route table).
- **P2 — `destinationForStep("subscription") === "/dashboard"`.** `lib/onboarding.ts:131-139` deliberately routes past the billing screen to the dashboard, so the onboarding flow **skips subscription entirely** and lands the owner on a dashboard with a trial banner. This is a conscious workaround for the broken payment screen, not a complete flow.
- **P2 — No double-click protection / loading state on the real payment path.** The screen has a `loading` flag, but since the request 404s, it is moot.
- **P2 — No payment pending/failure/success handling.** No polling, no timeout, no retry, no recovery after refresh.
- **P3 — Dead/shadowed file.** `app/onboarding/subscription.tsx` (77 lines) coexists with `app/onboarding/subscription/page.tsx`. `next build` succeeds and resolves the `page.tsx`; the `.tsx` file is unreachable dead code.
- **P3 — Dashboard is fully mocked.** `app/dashboard/page.tsx` shows hardcoded stats ("Customers Today: 42", "Revenue: KES 18,500") and a static "Trial ends in 3 days" banner. No live data.

### What works

- `RequireAuth` + `RequireOnboardingComplete` gate the dashboard on a real session and a real business (server-derived via `GET /onboarding/status`). ✅
- `api-client.ts` carries the Bearer token and clears the session on 401. ✅

---

## 10. Background job assessment

| Job | Registered? | Scheduled? | Idempotent? | Tenant-scoped? | Overlap-safe? |
|---|---|---|---|---|---|
| `subscription_state.expire_subscriptions` | ✅ (`__init__.py:20`) | ✅ daily 00:05 EAT | ✅ (selects only rows in the source state) | ✅ (per-subscription) | ⚠️ No distributed lock; relies on beat not double-firing. |
| `subscription_reminders.send_subscription_reminders` | ✅ | ✅ daily 08:00 EAT | ✅ (`notify_once`) | ✅ | ⚠️ Same. |
| `payment_reconciler.reconcile_webhooks` | ✅ | ✅ every 5 min | ✅ (guards on `payment.status`) | ✅ | ⚠️ Same. |

### Findings

- **P2 — `notify_once` rolls back the whole worker transaction on a concurrent insert.** `notifications.py:85` does `await db.rollback()` on `IntegrityError`. In a worker that processes many notifications per run, one collision discards all earlier notifications in that batch.
- **P3 — Timezone mixing.** `subscription_state._now()` and `subscription_reminders._now()` use `datetime.now(timezone.utc)`, while Celery beat is configured `timezone="Africa/Nairobi"` + `enable_utc=True`. Comparisons are UTC-correct, but `subscription_reminders._start_of_day` buckets by **UTC midnight**, so a shop whose trial ends at 02:00 EAT (23:00 UTC) is bucketed a day early.
- **P3 — No distributed lock / advisory lock.** The beat schedule is the only guard against overlapping execution. A beat failover or a long-running task could double-fire.
- **P2 — No "pending payment cleanup" job.** Payments stuck in `pending` (e.g. STK push sent but no callback ever arrives) are never timed out or marked `timeout`. The `timeout` status exists but is never written by any code path.
- **P2 — No failed-payment retry job.** A failed payment is terminal; there is no retry-to-charge flow.

---

## 11. Database and migration consistency

### Model vs migration parity

`python -m migrations.check_parity` reports **PARITY OK** (38 tables, 340 columns agree). However, the checker only compares **tables, columns, and index names** — it does **not** compare enum type names, enum labels, or index predicates.

### P1 — Enum type-name collision: `payment_method`

- `app/models/money.py:19` — `Sale.payment_method` → `Enum("cash","mpesa","pesapal", name="payment_method")`.
- `app/models/billing.py:104` — `Payment.method` → `Enum("mpesa","airtel_money","bank_transfer","pesapal", name="payment_method")`.

Two **different** enums share the **same** PostgreSQL type name `payment_method`. Under `Base.metadata.create_all` (used by every integration test), one type wins and the other reuses it. **Verified on the live test DB:** the `payment_method` type contains `(cash, mpesa, pesapal)` — the `Sale` definition. Inserting a `Payment` with `method='airtel_money'` or `'bank_transfer'` would raise `invalid input for enum payment_method`.

- Migration 0005 avoids this by naming the payments enum `payment_method_sub` (line 250). So a **migration-built** DB is fine, but a **model-built** DB (all tests, and any `create_all` deployment) is broken.
- **Not currently reachable** via the API (`StartPaymentRequest.method` is `Literal["mpesa","pesapal"]`, both valid in both label sets), but it is a latent integrity defect that breaks the moment `airtel_money`/`bank_transfer` are wired up.

**Fix:** Rename one enum type in the model (e.g. `name="payment_method"` on `Sale` and `name="payment_method"` → `name="payment_method"` on `Payment` is the collision; give `Payment.method` a distinct name like `payment_method` → `payment_method` is shared; rename to `payment_method` on Sale and `payment_method` on Payment — i.e. disambiguate).

### P2 — Model declares an index no applied migration creates

- `app/models/billing.py:69-79` declares `uq_subscriptions_live_business` (partial unique index on `business_id WHERE status IN ('trial','active','grace')`).
- This index is created by **migration 0010**, which is **written but unapplied** (per the T-003 constraint).
- `check_parity` reports it as present (it dry-runs 0010's `upgrade()`), but **no production database has it yet** — `railway.toml` runs `alembic upgrade head` on boot, so it will be applied on first deploy, but it is not currently enforced.
- **Verified on the test DB:** the index exists there (because the test DB is built from `create_all`, not migrations).

**Impact:** Until 0010 is applied, two live subscriptions per business are possible (the exact T-003 duplicate-trial bug).

### P1 — `payments` has no uniqueness on `provider_ref`

Verified via `pg_indexes`: `payments` has only `business_id`, `subscription_id`, and `(business_id, paid_at)` indexes. No unique constraint on `provider_ref` or `(provider, provider_ref)`. This is the database-level gap behind the P1 idempotency finding in §7.

### P1 — `webhook_events` has no uniqueness on `(source, event_ref)`

Verified via `pg_indexes`: only `event_ref` and `(source, processed_at)` indexes. Replayed callbacks insert duplicate rows.

### P0/P1 — `payments.subscription_id` is a VARCHAR, not a UUID FK

- `payments.subscription_id` is `Column(String, nullable=False, index=True)` — a plain string, **no foreign key** to `subscriptions.id`.
- This is the structural reason the P1 UUID-type bug (§7) is possible: the column accepts anything, so the type mismatch only surfaces at the asyncpg codec layer, not at the SQLAlchemy level.
- No FK means a payment can reference a non-existent subscription silently.

**Fix proposal (proposal only):** add `ForeignKey("subscriptions.id")` and a `UUID` column type; add `UNIQUE (provider, provider_ref)`; add `UNIQUE (source, event_ref)` on `webhook_events`.

### Other schema observations

- `subscriptions` has no `cancelled_at`, `current_period_end` is `NOT NULL` ✅, `plan_id` is `String` (not UUID) ✅.
- `payments.amount_kes` is `Integer` (whole shillings) ✅; no `currency` column (see §7 P3).
- `businesses` has both `uq_businesses_owner_user_id` (unique) and `ix_businesses_owner_user_id` (non-unique) — the latter is redundant (T-003 notes this).

---

## 12. Test commands and exact results

All run against the isolated test DB `postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test`.

### 12.1 Existing test suite

```
SECRET_KEY=ci-throwaway-not-a-real-secret \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test \
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test \
.venv/bin/python -m pytest --tb=short -rs -q
```
**Result:** `437 passed, 33 warnings in 597.13s` (full suite, green).

### 12.2 Billing-specific subset

```
.venv/bin/python -m pytest tests/test_payment_application.py \
  tests/test_payment_reconciler.py tests/test_workers.py -v --tb=short -rs
```
**Result:** `29 passed`.

### 12.3 Parity + contract + guards

```
.venv/bin/python -m migrations.check_parity
.venv/bin/python -m pytest tests/test_pwa_onboarding_contract.py \
  tests/test_integration_guards.py tests/test_env_template.py -q --tb=short -rs
```
**Result:** `check_parity` → `PARITY OK` (with 6 informational index drifts); `46 passed`.

### 12.4 Frontend contract

```
cd groomio-web && npm test   # -> node scripts/check-onboarding-contract.mjs
```
**Result:** `✓ onboarding contract: 11 checks passed`. (This pins the *customer* onboarding funnel, not billing.)

### 12.5 Defect reproduction (throwaway, outside repo)

```
PYTHONPATH=groomio-api .venv/bin/python /tmp/t004_audit_repro.py
```
**Result (verbatim):**
```
[T004-A] REPRODUCED — subscription activated by a fabricated callback
      HTTP 200; checkout id sent = 'ATTACKER-INVENTED-CHECKOUT-ID' vs stored
      provider_ref 'REAL-CHECKOUT-ID-777'; subscription.status=active;
      payment.status=success; period_end advanced=True
      (2026-10-14T03:19:41 -> 2026-11-13T03:19:41)

[T004-B] PROTECTED
      GET /v1/payments/<A's payment> as owner B -> HTTP 404;
      POST /v1/subscription/pay as owner B -> HTTP 404

[T004-C] PROTECTED
      clerk GET /v1/subscription -> HTTP 403; clerk GET /v1/plans -> HTTP 200

[T004-D] NOT RESTRICTED
      business.status='expired'; owner GET /v1/business -> HTTP 200;
      owner GET /v1/customers -> HTTP 200

[T004-E] REPRODUCED — payment initiation is broken
      two identical POST /v1/subscription/pay -> HTTP raised / 404;
      payment rows created = 0; raised = DBAPIError: ... invalid input for
      query argument $3: UUID(...) (expected str, got UUID)

[T004-F] REPRODUCED — implementation rejects the correct signature
      Pesapal-documented algorithm (space-joined) accepted=False;
      implementation's own concatenation accepted=True
```

The reproduction script wraps all writes in a single transaction that is **rolled back**; the test DB was left empty (0 rows in `payments`/`subscriptions`/`plans`/`webhook_events`) after the run.

### 12.6 What was NOT verified (requires live provider)

- End-to-end M-Pesa STK push → callback → subscription activation (would require live Safaricom credentials and a real phone).
- End-to-end Pesapal redirect → IPN → activation (would require live Pesapal credentials).
- Webhook signature verification against a real provider-signed payload.

These are marked **NOT VERIFIED** and are covered by the in-memory / forged-callback reproductions above, which exercise the same code paths.

---

## 13. Prioritized defect register

| ID | Severity | File / function | Evidence | Repro | Impact | Fix (proposal) | Migration? | Regression test | Blocks prod? |
|---|---|---|---|---|---|---|---|---|---|
| T004-01 | **P0** | `app/subscriptions/router.py:387-434` `daraja_webhook` | Forged callback activated subscription; `CheckoutRequestID` never matched to `provider_ref` | Reproduced (T004-A) | Free subscription activation; financial loss | Match `event_ref == payment.provider_ref` before applying; add STK Query verification | No | Forged-callback test: mismatched `CheckoutRequestID` must NOT activate | **Yes** |
| T004-02 | **P1** | `app/subscriptions/router.py:262-269` `start_payment` | `subscription_id=sub.id` (UUID) into VARCHAR → 500 | Reproduced (T004-E) | No payment can ever be initiated | `subscription_id=str(sub.id)` | No | `POST /v1/subscription/pay` returns 202, not 500 | **Yes** |
| T004-03 | **P1** | `app/core/payments.py:195` `verify_pesapal_ipn_signature` | Space-joined signature rejected; concat accepted | Reproduced (T004-F) | Pesapal payments never activate | Join fields with single spaces | No | Signature of a space-joined payload verifies | **Yes** |
| T004-04 | **P1** | `app/subscriptions/router.py:238-243` `start_payment` | `_current_subscription` returns None for `expired` → 404 | Code reading | No recovery path from expiry | Allow re-subscription from `expired` | No | Expired business can start a payment | **Yes** |
| T004-05 | **P1** | `app/models/billing.py:104` vs `app/models/money.py:20` | Two enums named `payment_method` with different labels; test DB type = `(cash,mpesa,pesapal)` | Verified on test DB | `airtel_money`/`bank_transfer` payments 500; latent | Rename one enum type in the model | Yes (type rename) | Insert `Payment(method="bank_transfer")` succeeds | **Yes** (once those methods are wired) |
| T004-06 | **P1** | `app/subscriptions/router.py:129-176` `_apply_payment` | No row lock; no DB uniqueness on `provider_ref` | Code reading (concurrency not testable on `db` fixture) | Double subscription extension under concurrent callbacks | `SELECT ... FOR UPDATE` or atomic `UPDATE ... WHERE status='pending'`; add `UNIQUE(provider, provider_ref)` | Yes | Concurrent identical callbacks extend once | **Yes** |
| T004-07 | **P1** | `app/subscriptions/router.py:419-428` `daraja_webhook` | `not succeeded` flips `success → failed` without reversing time | Code reading | Reversal after success leaves service granted | Distinguish cancel-before-completion from reversal-after-success; add `refunded` status | Yes | Late failure on a `success` payment does not silently downgrade | **Yes** |
| T004-08 | **P1** | `app/core/payments.py:88` `daraja_stk_push` | `PartyB = str(account_ref)` (payment UUID) | Code reading | STK push rejected by Safaricom | `PartyB = settings.mpesa_shortcode` | No | STK push request body has correct PartyB | No (config) |
| T004-09 | **P1** | `app/core/config.py:31` | `mpesa_stk_push_url` = STK Query endpoint | Code reading | STK push can never succeed | Point at `/mpesa/stkpush/v1/processrequest` | No | Config value is the push endpoint | **Yes** |
| T004-10 | **P1** | `app/subscriptions/router.py:262-269` `start_payment` | No check for existing `pending` payment | Code reading | Double-tap → two STK pushes | Look up existing `pending` payment first | No | Second identical request returns the same payment | No |
| T004-11 | **P1** | `app/workers/payment_reconciler.py:77-112` `_reconcile` | Matches on `Payment.id` only; no amount/`CheckoutRequestID` re-check | Code reading | Forged callback reconciled into success | Re-verify amount + `provider_ref` | No | Reconciler rejects amount-mismatched stored events | No |
| T004-12 | **P1** | `app/subscriptions/router.py` (whole module) | No access-control dependency on `businesses.status` | Reproduced (T004-D) | Expired/suspended tenants keep full access | Add `require_active_business` dependency | No | Expired business gets 402/403 on a business-scoped endpoint | **Yes** |
| T004-13 | **P2** | `app/admin/router.py:89` `trial_extend` | Extends `biz.trial_ends_at` only, not `Subscription.current_period_end` | Code reading | Support's trial-extend button is a no-op | Extend the subscription period too | No | Trial extension advances `current_period_end` | No |
| T004-14 | **P2** | `app/subscriptions/router.py:129-176` `_apply_payment` | Never sets `businesses.status` | Code reading | Paying customers stay `trialing`; metrics wrong | Set `businesses.status='active'` on success | No | Successful payment sets business status to active | No |
| T004-15 | **P2** | `app/workers/notifications.py:85` `notify_once` | `db.rollback()` on IntegrityError discards the whole batch | Code reading | Retried reminders drop earlier notifications | Use savepoint or per-row handling | No | Concurrent identical notifications do not drop others | No |
| T004-16 | **P2** | `app/workers/` (none) | No job marks long-pending payments `timeout` | Code reading | Payments stuck `pending` forever | Add a pending-cleanup job | No | Stale pending payments are timed out | No |
| T004-17 | **P2** | `app/subscriptions/router.py:487-489` `pesapal_webhook` | `provider_ref` set to the payment's own UUID, not a Pesapal txn id | Code reading | Weak audit trail | Store Pesapal's `payment_reference` | No | `provider_ref` differs from the payment id after IPN | No |
| T004-18 | **P2** | `app/onboarding/subscription/page.tsx:69` | Posts to `/onboarding/subscription` (404); sends plan code not UUID | Code reading | Onboarding payment step cannot succeed | Call `GET /v1/plans` then `POST /v1/subscription/pay` with UUID + `method`; poll `GET /v1/payments/{id}` | No | Subscription screen reaches `state="awaiting_mpesa"`/`redirect` | **Yes** |
| T004-19 | **P2** | `app/dashboard/page.tsx:78` | "Renew" → `/subscription` (404) | `next build` route table | Renewal entry point broken | Point at the real subscription screen | No | Renew button navigates to a real route | No |
| T004-20 | **P2** | `app/onboarding/subscription/page.tsx:102` | Banner says "14-Day Free Trial" | Code reading | Misleading trial length | Read trial length from the API | No | Banner matches `settings.trial_days` | No |
| T004-21 | **P3** | `app/onboarding/subscription.tsx` | Dead file shadowed by `app/onboarding/subscription/page.tsx` | `next build` resolves the page | Dead code / confusion | Delete the legacy file | No | n/a | No |
| T004-22 | **P3** | `app/models/billing.py:112` `Payment.provider_ref` | No unique constraint | `pg_indexes` | Replayed callbacks insert duplicate `webhook_events` rows | Add `UNIQUE(source, event_ref)` | Yes | Duplicate callback does not create a second event row | No |
| T004-23 | **P3** | `app/models/billing.py` | No `currency` column | Code reading | Multi-currency expansion unsafe | Add `currency` (default `kes`) | Yes | n/a | No |
| T004-24 | **P3** | `app/workers/subscription_reminders.py:34` | Buckets by UTC midnight, not EAT | Code reading | Reminders fire a day early for late-EAT shops | Use EAT day boundaries | No | n/a | No |
| T004-25 | **P3** | `app/workers/__init__.py` | No advisory lock on beat tasks | Code reading | Double-fire on beat failover | Add `SELECT pg_advisory_lock` per task | No | n/a | No |

**Severity legend:** P0 = financial loss / unauthorized activation / cross-tenant exposure. P1 = core billing workflow broken or duplicate-payment risk. P2 = incomplete integration / missing safeguards. P3 = observability / maintainability / minor UX.

---

## 14. Recommended implementation sequence

Ordered by dependency and blast radius. **None of these are implemented in this audit.**

1. **T-004.2 — Payment initiation & provider integration (unblock the workflow).**
   - Fix T004-02 (UUID→str) — the single blocker for any payment.
   - Fix T004-09 (STK push URL) and T004-08 (PartyB).
   - Fix T004-03 (Pesapal signature).
   - Add duplicate-initiation guard (T004-10).
   - *Why first:* nothing downstream can be tested until a payment can be initiated.

2. **T-004.3 — Callback verification & idempotency (secure the money path).**
   - Fix T004-01 (match `CheckoutRequestID` to `provider_ref`; add STK Query).
   - Fix T004-06 (row lock / atomic update + `UNIQUE(provider, provider_ref)`).
   - Fix T004-07 (reversal handling) and T004-11 (reconciler re-verification).
   - Fix T004-05 (enum type-name collision) and T004-22 (event uniqueness).
   - *Why second:* the payment path must be secure before it is exposed.

3. **T-004.1 — Subscription lifecycle & access enforcement.**
   - Fix T004-04 (re-subscribe from `expired`).
   - Fix T004-12 (access restriction on `expired`/`suspended`).
   - Fix T004-13/T004-14 (sync `businesses.status` with `subscriptions.status`).
   - *Why third:* the state machine must be coherent before the UI drives it.

4. **T-004.4 — Frontend subscription onboarding integration.**
   - Rewrite `app/onboarding/subscription/page.tsx` to call `GET /v1/plans`, then `POST /v1/subscription/pay` with a plan UUID and `method`, then poll `GET /v1/payments/{id}` until `success`/`failed`/`timeout`.
   - Fix the dashboard "Renew" link (T004-19) and the trial banner (T004-20).
   - Delete the dead `app/onboarding/subscription.tsx` (T004-21).
   - *Why fourth:* needs the backend payment path to exist and be secure.

5. **T-004.5 — Renewal, expiry, grace, reconciliation.**
   - Add a pending-payment timeout job (T004-16).
   - Fix `notify_once` rollback (T004-15).
   - Fix timezone bucketing (T004-24) and add advisory locks (T004-25).
   - *Why fifth:* operational hardening after the core flow works.

6. **T-004.6 — End-to-end billing regression tests.**
   - Add DB-backed tests for: forged callback rejection, concurrent callback idempotency, re-subscription from expired, cross-tenant payment access, Pesapal signature acceptance, STK push request shape.
   - *Why last:* tests pin the behaviour the above workstreams establish.

---

## 15. Proposed files to change in the next milestone

**Backend:**
- `app/subscriptions/router.py` — fix `subscription_id=str(sub.id)`; add `CheckoutRequestID` match; add re-subscribe-from-expired; sync `businesses.status`; add duplicate-initiation guard; fix Pesapal `provider_ref`.
- `app/core/payments.py` — fix `verify_pesapal_ipn_signature` (space-joined); fix `PartyB`; (config URL is in `config.py`).
- `app/core/config.py` — fix `mpesa_stk_push_url` default.
- `app/models/billing.py` — rename the `payment_method` enum type to avoid collision with `money.py`; add `currency` column (proposal).
- `app/models/money.py` — (no change needed if billing.py is renamed).
- `app/workers/payment_reconciler.py` — re-verify amount + `provider_ref`.
- `app/workers/notifications.py` — fix `notify_once` rollback.
- `app/workers/subscription_state.py` — (timezone hardening, optional).
- `app/admin/router.py` — extend `Subscription.current_period_end` in `trial_extend`.
- `app/core/deps.py` / `app/core/tenancy.py` — add `require_active_business` dependency.
- `app/main.py` — apply the new dependency to business-scoped routers.

**Frontend:**
- `groomio-web/app/onboarding/subscription/page.tsx` — real billing integration.
- `groomio-web/app/dashboard/page.tsx` — live trial banner + working Renew link.
- `groomio-web/lib/onboarding.ts` — point `destinationForStep("subscription")` at the real screen.
- `groomio-web/app/onboarding/subscription.tsx` — delete (dead file).

**Migrations (proposals only — not applied in this audit):**
- Rename the `payment_method` enum type used by `payments.method` (or `sales.payment_method`) to disambiguate.
- Add `UNIQUE (provider, provider_ref)` on `payments`.
- Add `UNIQUE (source, event_ref)` on `webhook_events`.
- Add `currency` column to `payments` (default `kes`).
- Add `ForeignKey("subscriptions.id")` on `payments.subscription_id` and change its type to `UUID`.

---

## 16. Migration proposals

| # | Change | Type | Risk | Required by |
|---|---|---|---|---|
| M1 | Rename `payments.method` enum type from `payment_method` to `payment_method` (disambiguate from `sales.payment_method`) | ALTER TYPE (rename + recreate) | Medium (locks `payments` briefly; requires downtime window) | T004-05 |
| M2 | `ALTER TABLE payments ADD CONSTRAINT uq_payments_provider_ref UNIQUE (provider, provider_ref)` | ADD CONSTRAINT | Low (no existing duplicates expected) | T004-06 |
| M3 | `ALTER TABLE webhook_events ADD CONSTRAINT uq_webhook_events_source_ref UNIQUE (source, event_ref)` | ADD CONSTRAINT | Low | T004-22 |
| M4 | `ALTER TABLE payments ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'kes'` | ADD COLUMN | Low | T004-23 |
| M5 | `ALTER TABLE payments ALTER COLUMN subscription_id TYPE UUID USING subscription_id::uuid; ALTER TABLE payments ADD FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)` | TYPE + FK | Medium (requires all `subscription_id` values to be valid UUIDs) | T004-06 (FK) |

**Note:** Migration 0010 (`uq_subscriptions_live_business`, `uq_businesses_owner_user_id`) is written but unapplied. It is **not** modified by this audit. It should be applied as part of T-004.2 (it is a prerequisite for the "one live subscription per business" guarantee).

---

## 17. External provider configuration requirements

These are **not** set in the repository and must be provisioned in the deployment environment before any payment can work:

- **M-Pesa Daraja (production):** `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE` (the paybill/till), `MPESA_PASSKEY`, `MPESA_BASE_URL=https://api.safaricom.co.ke`, `MPESA_STK_PUSH_URL=https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest`, `MPESA_CALLBACK_URL=https://api.groomio.app/v1/payments/webhooks/daraja`.
- **Pesapal (production):** `PESAPAL_CONSUMER_KEY`, `PESAPAL_CONSUMER_SECRET`, `PESAPAL_MERCHANT_ID`, `PESAPAL_BASE_URL=https://www.pesapal.com`, `PESAPAL_IPN_URL`, `PESAPAL_RETURN_URL`, `PESAPAL_CANCEL_URL`.
- **Webhook exposure:** `/v1/payments/webhooks/daraja` and `/v1/payments/webhooks/pesapal` must be reachable from the public internet (Safaricom/Pesapal call back to them). The current `MPESA_CALLBACK_URL` points at `api.groomio.app` — confirm DNS + TLS + the route is registered (it is, via `main.py`).
- **Redis:** `REDIS_URL` for Celery broker/backend (workers + beat).
- **STK Query (recommended for T004-01 fix):** a second Daraja call to `/mpesa/stkpush/v1/query` to confirm a callback before applying — requires the same Daraja credentials.

**NOT VERIFIED:** no live provider credentials were available or used. The provider integrations are classified as "implemented but not correctly configured / not securely verifiable" until a live end-to-end test is run with real credentials in a sandbox.

---

## 18. Acceptance criteria for each proposed fix

- **T004-02:** `POST /v1/subscription/pay` with a valid plan UUID and `method=mpesa` returns `202 Accepted` with `state="awaiting_mpesa"` and a `payment_id`; no 500.
- **T004-01:** A Daraja callback whose `CheckoutRequestID` does not match the stored `provider_ref` is stored but does **not** change `payment.status` or `subscription.status`.
- **T004-03:** `verify_pesapal_ipn_signature` returns `True` for a signature computed with the space-joined algorithm and the configured `pesapal_merchant_id`.
- **T004-04:** An `expired` business can call `POST /v1/subscription/pay` and receive `202`.
- **T004-06:** Two concurrent identical Daraja success callbacks for the same payment extend the subscription exactly once.
- **T004-09:** `settings.mpesa_stk_push_url` ends with `/mpesa/stkpush/v1/processrequest`.
- **T004-12:** An `expired` or `suspended` business receives `402`/`403` on a business-scoped endpoint (e.g. `GET /v1/customers`).
- **T004-18:** The subscription screen fetches `GET /v1/plans`, submits `POST /v1/subscription/pay` with a UUID, and polls `GET /v1/payments/{id}` until a terminal state.
- **T004-19:** The dashboard "Renew" button navigates to a route that builds.
- **T004-05:** `Payment(method="bank_transfer")` inserts without error on a `create_all`-built database.

---

## 19. Remaining risks

- **Unsigned Daraja callbacks.** Even with the `CheckoutRequestID` match (T004-01), Safaricom callbacks are not signed. The only complete mitigation is STK Query verification, which requires the Daraja credentials and a second provider call. Until then, the `CheckoutRequestID` match is the best available control.
- **No live provider testing.** All provider-facing code is classified NOT VERIFIED against a real Safaricom/Pesapal sandbox. The reproductions exercise the local code paths only.
- **Concurrent-callback tests are not possible on the `db` fixture.** `tests/conftest.py` documents that the `db` fixture cannot express concurrency (single connection, single transaction). A true concurrency test for T004-06 requires the `concurrency_client` fixture and a real Postgres — it is proposed for T-004.6.
- **Enum type-name collision (T004-05) is latent.** It does not break the current API (only `mpesa`/`pesapal` are reachable), but it will break the moment `airtel_money`/`bank_transfer` are enabled.
- **Migration 0010 is unapplied.** The `uq_subscriptions_live_business` constraint is not enforced in production until `alembic upgrade head` runs. The T-003 constraint forbids applying it during this audit.
- **`businesses.status` is not the source of truth.** The lifecycle worker keys off `subscriptions.current_period_end`; `businesses.status` is a derived, often-stale mirror. Any access-control logic built on it must be kept in sync.

---

## 20. Production-readiness assessment

**NOT PRODUCTION-READY.**

The subscription and billing system cannot currently complete a single paid subscription end-to-end:

- Payment initiation returns HTTP 500 for every attempt (T004-02).
- Even if it did not, the Daraja callback can be forged to activate a subscription without payment (T004-01, P0).
- Pesapal IPNs are rejected by a broken signature check (T004-03).
- The STK push is sent to the wrong endpoint with the wrong `PartyB` (T004-08, T004-09).
- The frontend does not call the billing API at all (T004-18).
- Expired businesses cannot re-subscribe (T004-04) and are not access-restricted (T004-12).

The **design intent** is sound and well-documented (fail-closed `_apply_payment`, idempotent business creation, tenant-scoped payment reads, server-side pricing). The defects are implementation gaps, not architectural flaws. The system becomes production-ready after the workstreams in §13 are completed and accepted against the criteria in §18.

---

## Appendix A — Repository structure differences from the task brief

The task brief listed expected paths. The actual repository differs as follows:

- `app/payments/` — **does not exist.** Payment logic lives in `app/core/payments.py` and `app/subscriptions/router.py`.
- `app/subscription/` — **does not exist.** It is `app/subscriptions/` (plural).
- `groomio-web/app/onboarding/subscription/` — exists (plural `subscription` directory) **and** `groomio-web/app/onboarding/subscription.tsx` exists (legacy dead file).
- `groomio-web/lib/auth.tsx` — exists.
- `groomio-web/lib/api-client.ts` — exists.
- `app/core/deps.py`, `app/core/database.py`, `app/workers/`, `app/models/billing.py`, `app/models/tenant.py` — all present as expected.

## Appendix B — Reproduction artifact

`/tmp/t004_audit_repro.py` — throwaway, outside the repository. Re-runnable with:
```
PYTHONPATH=groomio-api \
SECRET_KEY=audit-only-not-a-real-secret \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test \
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test \
.venv/bin/python /tmp/t004_audit_repro.py
```

---

## 21. Implementation record (2026-10-21)

The workstreams in §14 have been implemented. This section records what was
built, what was verified against, and what is still open — including the parts
that are better than the audit asked for and the part that is worse.

### 21.1 Backend

| Defect | Status | Where |
|---|---|---|
| T004-01 | fixed | `app/subscriptions/router.py` — `daraja_webhook` matches the callback's `CheckoutRequestID` against `payment.provider_ref` before it touches the payment; `daraja_callback_is_ours` checks the account reference and the quoted amount |
| T004-02 | fixed | `subscription_id=str(sub.id)` in `start_payment` |
| T004-03 | fixed | `app/core/payments.py::verify_pesapal_ipn_signature` signs the space-joined payload in Pesapal's field order |
| T004-04 | fixed | `_current_or_expired_subscription` — an expired business can start a payment again |
| T004-05 | fixed | the model declares `payment_method_sub`; migration 0005 already created that name, so the collision was model-only |
| T004-06 | fixed | `_apply_payment` takes `SELECT … FOR UPDATE` on the payment row; `uq_payments_provider_ref` (migration 0011) makes a replayed callback a constraint violation rather than a second extension |
| T004-07 | **half** | a late failure callback no longer downgrades a `success` payment — but there is still no `refunded` status and no reversal path (§21.5) |
| T004-08 | fixed | `PartyB = settings.mpesa_shortcode` |
| T004-09 | fixed | `mpesa_stk_push_url` points at `/mpesa/stkpush/v1/processrequest` |
| T004-10 | fixed | `start_payment` returns the existing `pending` payment instead of pushing a second STK prompt |
| T004-11 | fixed | `app/workers/payment_reconciler.py` re-checks `provider_ref` and the amount before applying |
| T004-12 | fixed | `require_active_business` in `app/core/deps.py`, applied per-router in `app/main.py`; the subscription router is deliberately left ungated so an expired business can still pay |
| T004-13 | fixed | `app/admin/router.py::trial_extend` advances `subscriptions.current_period_end` as well as `businesses.trial_ends_at` |
| T004-14 | fixed | `_apply_payment` sets `businesses.status = 'active'`, unless the business was deliberately suspended |
| T004-15 | fixed | `notify_once` inserts inside `db.begin_nested()`, so a duplicate no longer discards the batch |
| T004-16 | fixed | `app/workers/payment_reconciler.py::_timeout_pending` |
| T004-17 | fixed | the Pesapal IPN stores Pesapal's `payment_reference` in `provider_ref` |
| T004-22 | fixed | `uq_webhook_events_source_ref` (migration 0011) |
| T004-23 | fixed | `payments.currency` (migration 0011) |
| T004-24 | fixed | `app/workers/subscription_reminders.py` buckets on the shop's own local day (`settings.business_timezone`, EAT by default) |
| T004-25 | fixed | `app/workers/db.py::beat_lock` — `pg_try_advisory_lock` per beat task |

### 21.2 Frontend (`groomio-web`)

| Defect | Status | Where |
|---|---|---|
| T004-18 | fixed | `app/onboarding/subscription/page.tsx` reads `GET /v1/plans`, initiates with `POST /v1/subscription/pay` (plan UUID + method) and polls `GET /v1/payments/{id}` until `success`, `failed` or `timeout` |
| T004-19 | fixed | the dashboard's "Renew" button pushes `/onboarding/subscription` — the route that exists — instead of the 404 `/subscription` |
| T004-20 | fixed | the trial banner reads `GET /v1/subscription` instead of asserting "10-Day Free Trial" in the screen; the server rounds the countdown **up**, so a ten-day trial reads as ten days and not as nine |
| T004-21 | fixed | `app/onboarding/subscription.tsx` and `app/onboarding/business.tsx` deleted; the directory pages are the live ones |

`lib/onboarding.ts::destinationForStep` now routes the `subscription` step to
`/onboarding/subscription` instead of `/dashboard`. It used to route to the
dashboard because the billing screen was not wired to the payment API; once it
was, keeping the old mapping would have left the only screen that can take the
owner's money unreachable from the flow that asks for it.

### 21.3 What was verified, and how

```
groomio-api:  480 passed          (pytest, real PostgreSQL on 5433)
              python -m migrations.check_parity → PARITY OK
groomio-web:  npm test            → 13 contract checks passed
              npx tsc --noEmit     → clean
              npm run build        → compiled, 23 routes, /onboarding/subscription present
```

The billing contract is pinned from both ends, because neither project can see
the other at compile time:

- `groomio-api/tests/test_pwa_billing_contract.py` — reads the OpenAPI schema
  and asserts the paths, request bodies, response fields and enums the PWA's
  billing screen depends on. A renamed path or a dropped field fails here.
- `groomio-web/scripts/check-onboarding-contract.mjs` — reads the PWA's own
  source and asserts it calls the real endpoints, that no screen posts to an
  `/onboarding/*` write path (neither exists on this API), that the redirect map
  is total and points only at routes the app actually has, and that the
  dashboard's Renew button points at the billing screen.

### 21.4 Migrations

`0010_businesses_unique_owner` and `0011_payment_idempotency` are written and
the model/migration parity check passes. **Neither has been applied anywhere.**
The development database on 5432 is empty and its `alembic_version` table holds
no rows; the test database is built from `Base.metadata.create_all`, not from
the migration chain. The constraints the audit asked for therefore exist as
migrations and as model declarations, but no environment has ever run
`alembic upgrade head`. That is the next operator step, and it is a step with
a lock window — see `migrations/preflight_0010_readonly.sql` and the T-003
runbook (§11) before running it.

### 21.5 What is still not production-ready

- **No live provider testing.** Nothing here has been exercised against a real
  Safaricom or Pesapal sandbox. The provider credentials in §17 still have to
  be provisioned, and a first end-to-end payment has to be run in a sandbox
  before this is trusted with an owner's money.
- **Refunds and reversals are not implemented.** T004-07 is half-done by
  design: a late failure callback will not silently claw back granted
  subscription time, but there is no `refunded` payment status, no reversal
  handler and no manual-review path. A customer who reverses an M-Pesa payment
  keeps their month until a human intervenes.
- **Daraja callbacks are still unsigned.** Matching `CheckoutRequestID` to
  `provider_ref` is the strongest control available without the STK Query
  call, and it is not authentication. STK Query verification is still
  unimplemented.
- **`businesses.status` remains a derived mirror.** It is written by the
  lifecycle worker and by `_apply_payment`, and it is not the source of truth
  — `subscriptions.current_period_end` is.
