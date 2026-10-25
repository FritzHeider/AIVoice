# Phase 15 — Billing, Metering, Stripe, Invoices, Usage and Analytics APIs

- [ ] P15-T01 Complete metering
  - Spec refs: specs/24-billing-and-limits.md (metering rules, COGS cost estimates)
  - Depends on: P05-T18, P14-T03, P14-T08, P09-T05, P08-T07
  - Deliverables: `va_billing.meter.record(...)` used for every meter in `usage_events` (calls, SMS in/out segments, widget messages, TTS characters, embeddings tokens, voice clones, phone numbers, LLM tokens, STT seconds) with idempotency keys and COGS estimates; ClickHouse mirror
  - Acceptance: tests per meter; idempotent re-recording.

- [ ] P15-T02 Ledger posting and balance management
  - Spec refs: specs/24-billing-and-limits.md (ledger posting, credit holds)
  - Depends on: P15-T01
  - Deliverables: atomic posting (`SELECT … FOR UPDATE` on org row), balance_after, holds capture/release/expiry sweeper, `INSUFFICIENT_CREDITS` logic with credit limit, suspension policy when balance below −credit_limit
  - Acceptance: concurrency test (200 parallel postings) keeps ledger consistent.

- [ ] P15-T03 Stripe provider
  - Spec refs: specs/24-billing-and-limits.md (auto-recharge, Stripe Checkout, subscriptions, customer portal, Stripe webhook handling)
  - Depends on: P03-T07, P15-T02
  - Deliverables: `BillingProvider` protocol + `StripeBillingProvider`, customer creation, checkout sessions, off-session auto-recharge, subscription create/change/cancel, portal sessions, `/v1/billing/stripe/webhook` with signature verification and `stripe_events` dedupe
  - Acceptance: FakeStripe tests for every webhook event type handled.

- [ ] P15-T04 Billing and usage API
  - Spec refs: specs/24-billing-and-limits.md (usage API endpoints)
  - Depends on: P15-T03
  - Deliverables: `/v1/usage`, `/v1/billing/balance`, `/v1/billing/ledger`, `/v1/billing/topup`, `/v1/billing/auto_recharge`, `/v1/billing/plans`, `/v1/billing/subscription`, invoices list/download
  - Acceptance: API tests with RBAC (billing role).

- [ ] P15-T05 Monthly invoices
  - Spec refs: specs/24-billing-and-limits.md (invoices monthly generation)
  - Depends on: P15-T04
  - Deliverables: invoice generation schedule, line items by meter, PDF rendering (reportlab) to S3, Stripe invoice sync where subscription exists
  - Acceptance: golden test for line items; PDF text extraction test.

- [ ] P15-T06 Reconciliation and low-balance notifications
  - Spec refs: specs/24-billing-and-limits.md (BillingReconcileWorkflow)
  - Depends on: P15-T02
  - Deliverables: nightly reconcile (call_legs durations vs usage within ±0.5 %, discrepancy report + adjustment entries), `billing.low_balance` events and emails
  - Acceptance: time-skipping tests with injected discrepancies.

- [ ] P15-T07 Sub-account credit transfers
  - Spec refs: specs/24-billing-and-limits.md (sub-account credit transfer); specs/04-auth-tenancy-rbac.md (sub-accounts)
  - Depends on: P15-T02, P02-T09
  - Deliverables: transfer endpoint with paired ledger entries (`transfer_out`/`transfer_in`), authorization
  - Acceptance: atomicity test.

- [ ] P15-T08 Analytics API
  - Spec refs: specs/27-observability.md (ClickHouse analytics query endpoints)
  - Depends on: P06-T05
  - Deliverables: `/v1/analytics/*` endpoints with exact SQL, org scoping enforced in every query, caching (60 s)
  - Acceptance: integration tests with seeded ClickHouse data.

- [ ] P15-T09 E2E: billing
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list)
  - Depends on: P15-T03, P15-T04
  - Deliverables: `tests_e2e/test_billing_end_to_end.py`
  - Acceptance: flow green.

## Phase exit gate
`make check && make e2e` pass.
