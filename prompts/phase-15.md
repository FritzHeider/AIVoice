## Phase 15 — Billing, Metering, Credits, Stripe, Invoices, Usage and Analytics APIs

### Goal
Make the platform commercially operable: complete metering across every billable unit, the append-only credit ledger
with holds, Stripe integration (customers, Checkout top-ups, off-session auto-recharge, subscriptions, customer portal,
webhooks), monthly invoices with PDFs, billing/usage APIs, nightly reconciliation, low-balance notifications, sub-account
credit transfers, and the ClickHouse-backed analytics API used by the dashboard.

### Read first
- `specs/24-billing-and-limits.md` — entire file.
- `specs/27-observability.md` — analytics query endpoints (`/v1/analytics/*`) with exact SQL.
- `specs/03-data-model.md` §9 — billing tables.
- `specs/19-events-and-webhooks.md` — `billing.*` events.

### Phase exit gate
`make check && make e2e` pass including a new flow `billing_end_to_end` (top-up via FakeStripe Checkout, call, ledger
debit equals Call Object price, auto-recharge triggers below threshold) and the reconciliation workflow test.
