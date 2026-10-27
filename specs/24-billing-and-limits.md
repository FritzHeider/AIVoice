# 24 — Billing, Metering, Credits, Plans, and Limits

Package ownership: `packages/va_billing` (`plans.py`, `prices.py`, `metering.py`, `ledger.py`, `holds.py`, `limits.py`,
`stripe_provider.py`, `invoices.py`, `cogs.py`, `redis_scripts/*.lua`); API routers `apps/api/src/va_api/routers/billing.py`,
`usage.py`, `stripe_webhooks.py`; workflows `packages/va_workflows/src/va_workflows/billing.py`
(`AutoRechargeWorkflow`, `MonthlyInvoiceWorkflow`, `SubscriptionRenewalWorkflow`, `BillingReconcileWorkflow`,
`LowBalanceNotifyWorkflow`). Envelope format for `/v1/billing*` and `/v1/usage*`.

All amounts are integer micro-dollars internally (`02-repo-layout-and-conventions.md` §4).

## 1. Model

Prepaid credits. Every billable event posts a `usage_events` row and a negative `credit_ledger` entry; purchases, grants,
and recharges post positive entries. `orgs.credits_balance_micros` is the materialized balance, updated in the same
transaction as each ledger insert (`SELECT … FOR UPDATE` on the org row). Balance may go negative down to
`-credit_limit_micros` (default 0; enterprise orgs get a configured credit limit and are invoiced monthly). Subscription fees
are charged by Stripe directly (card), not from credits, and recorded as `subscription_fee` ledger entries with amount 0 net
(informational: +fee purchase, −fee charge in one transaction) so statements show them.

## 2. Plans (seed data in migration `0002_seed_plans`)

| id | name | monthly fee | connected $/min | transfer $/min | min attempt | SMS $/segment | widget $/msg | daily cap | hourly cap | concurrency | voice clones | numbers | features |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `start` | Start | $0 | $0.12 | $0.05 | $0.015 | $0.02 | $0.01 | 100 | 100 | 10 | 1 | 5 | `{}` |
| `build` | Build | $249 | $0.10 | $0.04 | $0.015 | $0.02 | $0.01 | 2 000 | 1 000 | 50 | 5 | 50 | `{local_presence, analysis_schema, webhooks_org}` |
| `scale` | Scale | $449 | $0.09 | $0.03 | $0.015 | $0.02 | $0.01 | 5 000 | 1 000 | 100 | 15 | 200 | `{local_presence, analysis_schema, webhooks_org, citations, white_label_widget}` |
| `enterprise` | Enterprise | custom (per-org override) | custom | custom | $0.015 | custom | custom | unlimited | unlimited | unlimited | unlimited | unlimited | `{local_presence, analysis_schema, webhooks_org, citations, white_label_widget, custom_guard_rails, number_pools, experiments, sso, hipaa, dedicated_infra, custom_llm}` |

Micro-dollar seed values: connected 120 000 / 100 000 / 90 000 / enterprise default 80 000; transfer 50 000 / 40 000 /
30 000 / 25 000; min attempt 15 000; SMS 20 000; widget 10 000; fees 0 / 249 000 000 / 449 000 000 / 0.
Caps NULL = unlimited. Enterprise per-org overrides live in `orgs.limits` (keys `connected_rate_micros_per_min`,
`transfer_rate_micros_per_min`, `sms_rate_micros`, `widget_message_micros`, `daily_calls`, `hourly_calls`, `concurrency`,
`voice_clones`, `numbers`, `international_enabled`, `sms_per_day`) and take precedence over `plans`.
`PLAN_NUMBER_CAPS` and `NUMBER_MONTHLY_PRICES_MICROS` constants live in `va_billing.plans`/`va_billing.prices`
(`16-phone-numbers.md` §3, §8). Other prices (`va_billing.prices`): TTS standalone characters $0.00012/char (`tts_characters`,
used only by the standalone TTS API if exposed), KB embedding tokens $0.0000001/token, voice clone creation $0 (cap-limited),
analyze endpoint LLM tokens at COGS × 1.2.

`effective_price(org, meter) -> int` resolves org override → plan → default.

> Decision (2026-10-27): `0002_seed_plans` inserts the four rows above exactly. "custom" cells take the
> micro-dollar seed values (Enterprise: fee 0, connected 80 000, transfer 25 000, SMS 20 000, widget 10 000);
> "unlimited" caps are NULL; `tts_rate_micros_per_1k_chars` is 120 000 for every plan (the TTS standalone
> $0.00012/char × 1 000); `is_public` is true for all four (Enterprise is listed as contact-sales);
> `stripe_price_id` stays NULL (set per environment by deployment, never by a migration). The `numbers` column is
> not a `plans` column (`PLAN_NUMBER_CAPS`, `16-phone-numbers.md` §3). The downgrade deletes exactly these four ids
> and fails on an `org_subscriptions` reference rather than dropping data. `packages/va_db/tests/support/plan_catalog.py`
> parses this section, so the seed test fails if the table and the migration drift apart.

## 3. Metering rules

| Meter | Quantity | Unit price | When posted | Idempotency key |
|---|---|---|---|---|
| `call_connected_seconds` | connected seconds with millisecond precision (`calls.connected_seconds`), excluding transfer bridged time | connected rate / 60 per second | `finalize_call` | `call:{id}:connected` |
| `call_transfer_seconds` | seconds the primary leg was bridged to a human (cold or warm merged leg) | transfer rate / 60; **0 for BYO Telnyx calls** | `finalize_call` | `call:{id}:transfer` |
| warm-transfer proxy leg | proxy agent leg seconds are added to `call_connected_seconds` of the parent call (plan rate) | — | — | — |
| `call_attempt` | 1 | min attempt price **minus** the connected charge, floored at 0, i.e. every outbound attempt costs at least $0.015 total; inbound calls have no attempt minimum | `finalize_call` | `call:{id}:attempt` |
| `sms_segment_out` / `sms_segment_in` | segments | SMS rate | `21-messaging-sms.md` §12 | per message |
| `widget_message` | 1 | widget rate | `22-web-widget.md` §7 | per message |
| `phone_number_month` | 1 | number monthly price | `16-phone-numbers.md` §8 | `num:{id}:{yyyymm}` |
| `llm_tokens_in` / `llm_tokens_out` | tokens | COGS × 1.2 per token | analyze endpoint and KB chat endpoint only (in-call LLM cost is included in the minute rate) | `llm:{request_id}:in/out` |
| `kb_embedding_tokens` | tokens | price above | KB ingestion | `kbemb:{document_id}` |
| `stt_seconds`, `tts_characters` | quantity | 0 (COGS tracking only) | `finalize_call` | `call:{id}:stt`, `call:{id}:tts` |

Rounding: amount = `round_half_up(quantity × unit_price_per_unit)` computed in integer micro-dollars with `Decimal` and
`ROUND_HALF_UP`; per-second proration with millisecond quantity (e.g. 61.437 s × 2 000 µ$/s = 122 874 µ$).

`calls.price_micros` = sum of the call's usage amounts; Call Object `price` = dollars.

## 4. `finalize_call(call_id)` (`va_billing.metering`)

1. Load call, legs, org pricing (rates snapshot stored at dispatch in `calls.config.pricing` so mid-call plan changes don't
   alter price).
2. Compute connected seconds = Σ over primary leg (answered_at → min(ended_at, bridged_at)) + warm proxy legs; transfer
   seconds = primary leg bridged duration. Not answered → 0.
3. Build usage events (§3) with COGS (§10). Post all in one DB transaction: insert `usage_events` (ON CONFLICT
   `(org_id, idempotency_key)` DO NOTHING), ledger `usage` entries with the same idempotency keys, capture the call's hold
   (§5), update `calls.price_micros`, `orgs.credits_balance_micros`.
4. Publish `usage_events` to Redis stream `events:billing` for ClickHouse (`usage_events_raw`).
5. If balance after < auto-recharge threshold → signal `AutoRechargeWorkflow`; if balance after < `low_balance_threshold`
   (org setting, default $5) and no notification in 24 h → `billing.low_balance`.

## 5. Credit holds

- Purpose: prevent a burst of calls from overspending.
- Hold amount for a call = `min(max_duration_minutes, 10) × connected_rate + min_attempt` (cap at 10 minutes so long
  `max_duration` doesn't lock large sums); for number purchase = first month price; batches hold per dispatched item.
- `holds.reserve(org, amount, ref)`: in one transaction lock org row; `available = balance − Σ held amounts + credit_limit`;
  if `available < amount` → `INSUFFICIENT_CREDITS`; insert `credit_holds(status='held', expires_at=now()+max_duration+2h)`.
  Holds do not change `credits_balance_micros`; they reduce "available balance" shown in UI.
- Capture: in `finalize_call` set `status='captured'` (actual charge posted separately by usage). Release: on failure/cancel
  before connect, `status='released'`. Expired holds are released by `BillingReconcileWorkflow`.
- Enterprise orgs with `credit_limit_micros ≥ 1 000 000 000` skip holds (`settings.billing.skip_holds=true`).

## 6. Limits enforcement (`va_billing.limits`, Redis Lua scripts)

All checks run in `POST /v1/calls` (and batch dispatch, inbound has only concurrency) in this order; the first failure
returns its error (`31-error-catalog.md`). Scripts are loaded with `SCRIPT LOAD` at startup and called with `EVALSHA`.

1. **Request rate** (per API key): token bucket `rl:key:{api_key_id}:{window}` — 20 req/s burst 40 for start, 100/200 build,
   200/400 scale, configurable enterprise. Lua `token_bucket.lua(key, rate, burst, now_ms, cost)` returns
   `{allowed, tokens_left, retry_after_ms}`; hash fields `tokens`, `ts`; `PEXPIRE` 2× fill time.
2. **Same-number rule**: `SET cap:dest:{org_id}:{e164} 1 NX PX 10000` → fails ⇒ `SAME_NUMBER_TOO_SOON`.
3. **New account**: org `created_at` < 24 h ago ⇒ daily cap = min(plan daily, 100), concurrency = min(plan, 10), errors
   `NEW_ACCOUNT_LIMIT`.
4. **Daily**: `INCR cap:org:{org_id}:daily:{yyyymmdd}` (UTC) with `EXPIRE 172800` on first increment; if result > cap →
   `DECR` and `DAILY_CAP_REACHED`. (Atomic Lua `capped_incr.lua(key, cap, ttl)` returns `{ok, value}`.)
5. **Hourly**: same with `cap:org:{org_id}:hourly:{yyyymmddhh}`, TTL 7 200 → `HOURLY_CAP_REACHED`.
6. **Concurrency**: sorted set `cap:org:{org_id}:concurrency`, member = call_id, score = expiry unix ms
   (`now + (max_duration+5 min)`). Lua `concurrency_acquire.lua(key, member, cap, now_ms, expiry_ms)`:
   `ZREMRANGEBYSCORE key -inf now_ms` (self-healing), `if ZCARD key >= cap then return 0`, `ZADD key expiry member`, return 1.
   Released by `ZREM` when the call ends (pipeline end, no-answer, failure) — release is idempotent; scheduled calls acquire at
   dispatch time, not creation.
7. **International**: destination country ≠ org home country (`settings.default_country`, default US) and not CA for US orgs:
   plan must allow (`orgs.limits.international_enabled` or kyc_level ≥ 2) else `INTERNATIONAL_NOT_ENABLED`; per-hour counter
   `cap:org:{org_id}:intl:{yyyymmddhh}` with caps start 10, build 100, scale 300, enterprise override →
   `INTERNATIONAL_RATE_LIMITED`. Destinations outside `INTERNATIONAL_ALLOWED_COUNTRIES` (env, default
   `US,CA,GB,AU,IE,NZ,DE,FR,ES,IT,NL,BE,SE,NO,DK,FI,MX,BR,IN,PH,SG`) are always blocked (`INTERNATIONAL_NOT_ENABLED`).
8. **Credits**: hold (§5).

If any later step fails, previously incremented counters (daily, hourly, concurrency, intl) are rolled back in reverse order
(compensation in Python `finally`).

Limits introspection: `GET /v1/billing/limits` → `{data:{plan, daily:{cap, used}, hourly:{cap, used}, concurrency:{cap, active},
international:{enabled, hourly_cap, used}}}`.

## 7. Payments (Stripe, behind `BillingProvider` protocol)

```python
class BillingProvider(Protocol):
    async def ensure_customer(self, org: Org) -> str: ...
    async def create_topup_checkout(self, org: Org, amount_micros: int, success_url: str, cancel_url: str) -> str: ...  # returns URL
    async def charge_off_session(self, org: Org, amount_micros: int, idempotency_key: str) -> ChargeResult: ...
    async def create_subscription_checkout(self, org: Org, plan: Plan, success_url: str, cancel_url: str) -> str: ...
    async def change_subscription(self, org: Org, plan: Plan) -> None: ...
    async def cancel_subscription(self, org: Org, at_period_end: bool) -> None: ...
    async def customer_portal_url(self, org: Org, return_url: str) -> str: ...
    def verify_webhook(self, payload: bytes, signature_header: str) -> StripeEvent: ...
```

- Top-up: Stripe Checkout Session `mode=payment`, `line_items=[{price_data:{currency:"usd", product_data:{name:"VoiceAgents credits"}, unit_amount: cents}, quantity:1}]`,
  `payment_intent_data.setup_future_usage="off_session"`, `metadata={org_id, purpose:"topup", amount_micros}`. Min $10, max $10 000 per top-up.
- Auto-recharge: `AutoRechargeWorkflow(org_id)` (workflow id `autorecharge-{org_id}`, signal-with-start, ensures one in
  flight): charges `auto_recharge_amount_micros` off-session via PaymentIntent (`confirm=true, off_session=true`,
  default payment method from the customer), idempotency key `ar:{org_id}:{yyyymmddhh}:{n}`; success → ledger
  `auto_recharge` + `billing.recharged`; `authentication_required`/`card_declined` → disable auto-recharge, email owners,
  `billing.payment_failed`. Max 5 auto-recharges per 24 h (fraud guard).
- Subscriptions: Checkout `mode=subscription` with `plans.stripe_price_id`; plan change via `change_subscription` with proration
  `create_prorations`; downgrade effective at period end.
- Stripe webhooks `POST /v1/billing/stripe/webhook` (raw body, `Stripe-Signature` verification with `STRIPE_WEBHOOK_SECRET`,
  dedupe `stripe_events`): handled types — `checkout.session.completed` (topup → ledger `purchase`; subscription → create
  `org_subscriptions`, set `orgs.plan_id`), `payment_intent.succeeded` (auto-recharge confirmation when async),
  `payment_intent.payment_failed`, `invoice.paid` (subscription renewal → ledger `subscription_fee`, period dates update),
  `invoice.payment_failed` (status `past_due`, email; after 14 days past due → plan downgraded to `start`),
  `customer.subscription.updated`, `customer.subscription.deleted` (plan → `start`), `charge.refunded` (ledger `refund`
  negative of refunded credits, balance may go negative), `charge.dispute.created` (org `status='suspended'` pending review,
  admin email). Unknown types → stored and ignored.

## 8. Billing API (envelope)

| Method | Path | Body / query | Response `data` |
|---|---|---|---|
| GET | `/v1/billing/balance` | — | `{balance, held, available, credit_limit, currency:"USD", auto_recharge:{enabled, threshold, amount}}` (dollars, 6 decimals) |
| GET | `/v1/billing/ledger` | `cursor?, limit?, entry_type?, from?, to?` | ledger entries `{id, entry_type, amount, balance_after, reference_type, reference_id, description, created_at}` |
| POST | `/v1/billing/topup` | `{amount: number dollars 10–10000, success_url?, cancel_url?}` | `{checkout_url}` |
| PUT | `/v1/billing/auto_recharge` | `{enabled, threshold?: 1–1000, amount?: 10–10000}` | auto-recharge settings; enabling requires a saved payment method (else `INVALID_INPUT`) |
| GET | `/v1/billing/plans` | — | public plans with prices and caps |
| GET | `/v1/billing/subscription` | — | current subscription or null |
| POST | `/v1/billing/subscription` | `{plan_id, success_url?, cancel_url?}` | `{checkout_url}` (new) or updated subscription (change) |
| DELETE | `/v1/billing/subscription` | `{at_period_end?: true}` | subscription |
| GET | `/v1/billing/portal` | `return_url?` | `{url}` |
| GET | `/v1/billing/invoices` | `cursor?` | invoices `{id, period_start, period_end, total, status, pdf_url}` |
| GET | `/v1/billing/limits` | — | §6 |
| POST | `/v1/billing/transfer` | `{to_org_id, amount}` | sub-account credit transfer (§9) |
| GET | `/v1/usage` | `from, to (≤ 92 days), group_by: "day"\|"meter"\|"day,meter", meter?` | `{rows:[{day?, meter?, quantity, amount}], totals:{amount}}` from ClickHouse `usage_events_raw` (falls back to Postgres when ClickHouse unavailable) |
| GET | `/v1/usage/calls` | `from, to, group_by: "day"\|"pathway"\|"agent"\|"batch"` | `{rows:[{key, calls, connected_minutes, transfer_minutes, amount}]}` |

Role requirements: `billing`, `admin`, `owner` for write endpoints; `analyst` read-only for usage.

## 9. Sub-account credit transfer

`POST /v1/billing/transfer` from a parent org to a direct child: one transaction locking both org rows in id order; ledger
`transfer_out` (parent, negative) and `transfer_in` (child, positive) with shared idempotency key
`xfer:{request Idempotency-Key header or uuid}`; parent available balance must cover the amount. Children may have
`settings.billing.bill_to_parent=true`: then their usage posts to the parent's ledger (`reference_id` carries child org id) and
holds are reserved on the parent.

## 10. COGS estimates (`va_billing.cogs`, constants with env overrides `COGS_*`)

| Component | Default cost | Basis |
|---|---|---|
| Telnyx outbound voice US | $0.0070/min (`COGS_TELNYX_OUTBOUND_PER_MIN_MICROS=7000`) | per connected minute + ringing not billed |
| Telnyx inbound voice US local | $0.0035/min | |
| Telnyx media streaming | $0.0035/min | per streamed minute |
| Telnyx recording | $0.0020/min | when recorded |
| Telnyx premium AMD | $0.0065 per call | when `premium` AMD used |
| Telnyx SMS US | from `message.finalized` `cost.amount` (actual) | |
| ElevenLabs STT realtime | $0.0060/min of call audio | `stt_seconds` |
| ElevenLabs TTS Flash | $0.00005/char | `tts_characters` |
| Telnyx Inference LLM | per-model token prices from `LLM_MODEL_COSTS_JSON` env (default map for configured models) | tokens recorded by `va_llm` per call |
| Media compute | $0.0015/min | |

COGS are stored on usage events (`cost_micros`) and aggregated in ClickHouse for margin dashboards (`27-observability.md`).
The defaults are planning numbers; operators set actual contract rates via env.

## 11. Invoices and statements

`MonthlyInvoiceWorkflow` (schedule `0 8 1 * *` UTC): per org, aggregate the previous month's ledger into `invoices`
(`line_items`: per meter quantity/amount, number rentals, subscription fees, top-ups, refunds); for prepaid orgs status
`finalized` with total 0 due (statement); for enterprise orgs with credit limits create a Stripe invoice
(`collection_method=send_invoice`, `days_until_due=30`) for net negative usage and store `stripe_invoice_id`. The PDF is an
HTML template rendered by Jinja2 and converted to PDF by headless Chromium through Playwright (installed in the worker
image); stored at `s3://{S3_BUCKET_RECORDINGS}/invoices/{org_id}/{invoice_id}.pdf`.

## 12. BillingReconcileWorkflow (nightly `0 4 * * *` UTC)

1. For calls ended in the previous 48 h: recompute connected/transfer seconds from `call_legs` (and Telnyx `call.hangup`
   `end_time` − `start_time` stored in `telnyx_events` when available); compare with posted usage quantities.
2. Difference > 0.5 % or > 1 s → post an `adjustment` ledger entry and corrective usage event (idempotency
   `recon:{call_id}:{meter}:{yyyymmdd}`), record `call_events` row category `call` message `billing_adjusted`.
3. Calls with `finalize_billing` failures (list in `calls.config.postcall_steps` missing `finalize_billing` and ended > 1 h)
   → run `finalize_call`.
4. Release expired holds; recompute `orgs.credits_balance_micros` from the ledger sum and alert (Sentry + metric
   `va_billing_balance_drift_micros`) on any mismatch, correcting the materialized value.
5. Emit report metrics: total adjustments, max drift.

## 13. Tests required

- Plan seed values exact; effective price precedence (override > plan).
- Metering math table tests incl. attempt minimum (10 s call at $0.12/min → $0.02 total; 3 s call → $0.015 total; no-answer
  → $0.015), transfer pricing, BYO zero transfer, warm proxy leg, rounding half-up with ms precision.
- Idempotency: finalize twice → single set of rows; concurrent finalize from two workers.
- Holds: reserve/capture/release, available balance math, insufficient credits, enterprise skip, expiry release.
- Lua scripts: unit tests against real Redis (testcontainers) for token bucket accuracy (rate 10/s over 5 s ≈ 50 ± 1),
  capped_incr at boundary under 100 concurrent increments (never exceeds cap), concurrency acquire/self-heal/release idempotent.
- Limit order and rollback when a later check fails (counters restored).
- Stripe: webhook signature verification with fake Stripe, each handled event type effect, dedupe, auto-recharge success/decline,
  5-per-day guard, subscription change/downgrade/past-due path.
- Sub-account transfer atomicity and bill_to_parent.
- Reconcile: injected drift corrected, balance recomputation.
- Usage API aggregation (ClickHouse testcontainer) and Postgres fallback produce identical results.

## 14. Acceptance criteria

1. Over a 1 000-call simulated load test, Σ usage amounts equals Σ (connected seconds × rate) within ±0.5 % and every org
   balance equals its ledger sum exactly.
2. No org can exceed its concurrency cap even under 500 concurrent `POST /v1/calls` requests (load test assertion).
3. An org with $1 balance cannot start more calls than its holds allow; calls in progress are never cut off for balance reasons
   (balance may go negative by at most the in-flight usage beyond holds).
