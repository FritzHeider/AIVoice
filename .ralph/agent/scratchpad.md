# Scratchpad — Phase 05

## Planner 2026-11-02: P05-T18 split
P05-T18 (PostCallWorkflow v1) was too large for one Builder activation (metering engine + workflow + 4 activities +
worker wiring), so it is now two tasks in plan/phase-05.md, and T17 depends on both:
- **P05-T18a**: call metering (`va_billing.metering.finalize_call`). Planned below.
- **P05-T18b**: `PostCallWorkflow` steps 1, 2, 3, 11, 13 plus worker registration. It uses T18a for step 11.

Notes for T18b, so the next planner does not have to rediscover them:
- The parent `CallWorkflow` abandons the child and closes right away, so the specs/18 §11 "parent runs
  emit_completed on a 30 min timeout" cannot work. Plan: the child races its pre-completion steps against a 30 min
  workflow timer and always runs `emit_completed` afterwards. Record this as a Decision.
- `recording_saved` usually arrives before the child starts (specs/06 §11 Decision P05-T14 item 3). Add an activity that
  reads `telnyx_events` (system scope, event_type `call.recording.saved`, call_id). The stored `payload` is the full
  envelope, so CarrierEvent.payload = `payload["data"]["payload"]` and leg = primary.
- Recording URL in the Call Object: move `va_api.services.call_read.recording_urls` into a shared helper
  (va_common.storage) so the worker can presign too. The worker has storage only when it is configured.
- The metrics catalog has `va_postcall_duration_ms{step}` (WORKER), not the specs/18 §11 names. Use the catalog name
  and record a Decision.
- `call.error` code `RECORDING_MISSING` is already in the specs/19 §2.1 closed set.
- There is no webhook router yet (phase 06): publish `call.completed` with persist + broadcast only.

## Plan P05-T18a — Call metering: `va_billing.metering.finalize_call`

Spec: specs/24 §3 (call meters, rounding), §4 steps 1-3, §5 capture/release; specs/18 §10.
Scope: only the three call meters. Phase 15 (P15-T01/T02/T03) owns COGS, `stt_seconds`/`tts_characters`, the
`events:billing` ClickHouse mirror, auto-recharge, `billing.low_balance`, and `bill_to_parent`. Write that down as a
Decision.

### Files
1. **NEW `packages/va_billing/src/va_billing/metering.py`**. Module docstring cites specs/24 §3-§5 and the Decision.
   - Constants: `CALL_METERS: Final = ("call_connected_seconds", "call_transfer_seconds", "call_attempt")`,
     `CALL_REFERENCE` (reuse `holds.CALL_REFERENCE` = "call"), `SECONDS_PER_MINUTE = 60`.
   - `def round_half_up(value: Decimal) -> int`: `int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))`.
   - `def seconds_amount(seconds: Decimal, rate_micros_per_min: int) -> int`:
     `round_half_up(seconds * rate / 60)`. The test vector is 61.437 s at 120 000/min → 122 874.
   - `@dataclass(frozen=True, slots=True) class CallUsageFacts`: `call_id: uuid.UUID`, `direction: CallDirection`,
     `dialed: bool` (a primary leg exists, i.e. the call reached Telnyx), `connected_seconds: Decimal`,
     `transfer_seconds: Decimal`, `byo: bool` (the primary leg has `telnyx_account_id`), `pricing: EffectivePricing`.
   - `@dataclass(frozen=True, slots=True) class PlannedUsage`: `meter: UsageMeter`, `quantity: Decimal`,
     `unit_price_micros: int`, `amount_micros: int`, `idempotency_key: str`.
   - `def call_idempotency_key(call_id, meter) -> str`: `call:{id}:connected` / `call:{id}:transfer` /
     `call:{id}:attempt` (specs/24 §3 column). Use a mapping, meter → suffix.
   - `def plan_call_usage(facts: CallUsageFacts) -> list[PlannedUsage]` (pure):
     - connected: posted when `connected_seconds > 0`. Quantity = seconds (ms precision). unit_price =
       `round_half_up(rate / 60)` (per-second price, informational). amount = `seconds_amount(...)`.
     - transfer: posted when `transfer_seconds > 0`. Rate = `transfer_rate_micros_per_min`, or 0 when `byo`
       (quantity is still posted, so reconcile sees it).
     - attempt: only for `direction == "outbound"` and `dialed`. Quantity 1. unit_price = `min_attempt_micros`.
       amount = `max(0, min_attempt − connected_amount)`. Post it even when the amount is 0, so the attempt is recorded.
     - A call that was never dialed gets `[]`. Order: connected, transfer, attempt.
   - `def pricing_from_snapshot(snapshot: object) -> EffectivePricing | None`: returns `None` unless the value is a
     mapping with every `EffectivePricing` field as an int (`va_billing.plans.int_override`). Get the field names with
     `dataclasses.fields(EffectivePricing)`.
   - `class CallNotEndedError(Exception)`: carries `status`. Raised for a non-terminal call. T18b's activity turns it
     into a retryable error.
   - `@dataclass(frozen=True, slots=True) class FinalizedCall`: `price_micros: int`, `posted: tuple[str, ...]`
     (idempotency keys inserted by this run), `hold: Literal["captured", "released", "none", "settled"]`
     ("settled" = it was already captured or released), `balance_after_micros: int`.
   - `async def finalize_call(session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID, *, now: datetime) ->
     FinalizedCall`. Runs in the caller's `tenant_session(org_id)`:
     1. `org = await OrgRepo(session).lock(org_id)` (FOR UPDATE; this serialises concurrent finalizes). None →
        `AppError("NOT_FOUND", ...)`, the same as holds.
     2. Load the call: `select(Call).where(Call.org_id == org_id, Call.id == call_id)`. None →
        `AppError("CALL_NOT_FOUND", ...)` (the code exists). Not `is_terminal(call.status)` → `CallNotEndedError`.
     3. `leg = await CallLegRepo(session, org_id).primary(call_id)`. Pricing comes from
        `pricing_from_snapshot(call.config.get("pricing"))`. When that is None, fall back to the org's current
        effective pricing: `effective_pricing(await session.get(Plan, org.plan_id), org.limits)`. Log
        `billing.pricing_snapshot_missing`. Skip the fallback if the plan row cannot be loaded.
     4. Plan the usage. `occurred_at = call.ended_at or now`. Insert with
        `postgresql.insert(UsageEvent).values([...]).on_conflict_do_nothing(index_elements=["org_id",
        "idempotency_key"]).returning(UsageEvent.idempotency_key, UsageEvent.amount_micros)`. `id=new_id()`,
        `reference_type="call"`, `reference_id=str(call_id)`. Skip the insert when the plan is empty.
     5. For each inserted row with amount > 0: `org.credits_balance_micros -= amount`, then add a `CreditLedger`
        row: `entry_type="usage"`, `amount_micros=-amount`, `balance_after_micros` = the running balance,
        `reference_type="call"`, `reference_id=str(call_id)`, `description=meter`, the same idempotency key. Then flush.
        Zero-amount rows get no ledger row.
     6. `price = sum(amount)` over `usage_events` of this org with reference_type "call", reference_id = call id,
        meter in CALL_METERS. Recompute it every time, so a re-run gives the same result. Then
        `update(Call).where(org, id).values(price_micros=price)`.
     7. Hold: if `call.credit_hold_id` is set, call `holds.capture` when price > 0, else `holds.release`. Either one
        returning False → "settled". No hold → "none".
     8. Log `billing.call_finalized` (price_micros, posted count, hold). Return the result.
   - Typing: `Call.direction` is a `CallDirection` Literal from va_db.models. Import `UsageMeter` from va_db.models.
2. **`packages/va_billing/src/va_billing/__init__.py`**: re-export `finalize_call`, `FinalizedCall`,
   `CallNotEndedError`, `plan_call_usage`, `CallUsageFacts`, `PlannedUsage`. Keep `__all__` sorted.
   This only works if importing metering from `__init__` causes no cycle; check that holds/plans are already imported there.
3. **specs/24-billing-and-limits.md**: add `> Decision (2026-11-02): call metering (P05-T18a)` under §4. It covers:
   which meters are posted; a 0-second meter is not posted, but the attempt row is (amount may be 0); attempts only for
   outbound calls with a primary leg (a call that never reached Telnyx costs nothing); BYO transfer posts its quantity
   at unit price 0; `unit_price_micros` = per-second price rounded half-up while the amount uses the exact rate/60;
   zero amounts get no ledger row; ledger idempotency key = usage key; price recomputed from all of the call's call
   meters; capture when price > 0, else release; missing snapshot → current org pricing; deferred to phase 15: COGS
   (`cost_micros` 0), stt/tts meters, `events:billing` mirror (§4 step 4), §4 step 5 auto-recharge/low balance,
   bill_to_parent; finalize locks the org row first, so concurrent finalizes serialise.
4. **AGENTS.md**: one dated line (P05-T18a) naming `va_billing.metering` and its test helpers.

### Tests
- **NEW `packages/va_billing/tests/test_metering.py`** (pure). Build `EffectivePricing` with connected 120 000/min,
  transfer 60 000, min attempt 15 000, the other fields from make_plan values.
  - `test_ten_second_call_costs_two_cents`: total 20 000, attempt amount 0 (row present).
  - `test_three_second_call_costs_the_attempt_minimum`: 6 000 + 9 000 = 15 000.
  - `test_unanswered_dialed_outbound_costs_the_minimum`: only the attempt, 15 000.
  - `test_never_dialed_call_costs_nothing`: `[]`.
  - `test_inbound_call_has_no_attempt_minimum`: 3 s inbound → only connected, 6 000.
  - `test_transfer_seconds_use_the_transfer_rate` and `test_byo_transfer_posts_zero_price`.
  - `test_rounding_half_up_with_millisecond_quantity`: 61.437 s → 122 874; a case at exactly .5 rounds up
    (0.001 s at 90 000/min = 1.5 → 2).
  - `test_idempotency_keys` (the three key formats); `pricing_from_snapshot` round trip, plus None for a missing
    key or a non-int/bool value.
- **NEW `packages/va_billing/tests/test_metering_db.py`**. Fixture `database` (billing conftest). Orgs from
  `new_billing_org(balance=...)`. Calls from `call_rows.seed_call(database, status=..., org_id=, config={"pricing":
  pricing.snapshot()}, answered_at=, ended_at=, connected_seconds=, transfer_seconds=, direction=, credit_hold_id=)`
  and legs from `seed_leg`. Make the hold with `holds.reserve_call_hold` in a tenant session, or a small helper.
  - Connected call: usage rows + ledger rows, the balance drops by the price, `calls.price_micros` is set, the hold is captured.
  - Finalize twice: the second run posts nothing, balance/price are unchanged, the hold reads "settled".
  - Concurrent finalize: two separate `tenant_session`s via `asyncio.gather` → one set of rows, balance charged once.
  - Transferred call: connected + transfer meters. BYO leg (`seed_leg(..., telnyx_account_id=)` via
    `sms_rows.add_telnyx_account`) → transfer amount 0.
  - Failed calls (`no_answer`/`busy` with a primary leg): 15 000, hold captured.
  - Canceled before dial (no leg): price 0, no rows, a still-held hold is released.
  - Non-terminal call → `CallNotEndedError`. Unknown call → `CALL_NOT_FOUND`.
  - Missing snapshot → uses the org plan/limits pricing (pass a `limits` override, assert its rate).
  - Ledger invariant: the balance after finalize equals the start balance plus the sum of the org's ledger rows.
    Seed the start balance with `grant_credits` so the whole balance is in the ledger.
  - DB tests must use real-time timestamps (the `call_events` partition gotcha does not apply here, since no events
    are written). Use `datetime.now(UTC)` for answered/ended.

### Gates
make fmt, lint, typecheck, placeholders, spec refs, `uv run pytest packages/va_billing -q` (coverage of metering.py
100 %), then the full `make test` in the background with a Monitor heartbeat (AGENTS.md P05-T10d gotcha). Also jscpd
and radon. No e2e: no call flow change (the e2e harness comes with P05-T17).

### Risks
- `on_conflict_do_nothing(...).returning` returns only the inserted rows, which is the behaviour we want.
- Session identity: after the UPDATE of `calls.price_micros`, use `execution_options(synchronize_session=False)`,
  or update the loaded object directly (simplest: `call.price_micros = price` on the loaded ORM row, then flush).
- Mypy without the pydantic plugin is irrelevant here (dataclasses only).
- Under the org lock the concurrent test needs two separate Database sessions. One `Database` with pool >1 is fine:
  `open_database` already defaults to a larger pool. If it does not, use two `open_database` contexts.

## Build P05-T18a (2026-11-02)
Implemented per plan. Changes from the plan:
- When the pricing snapshot is missing and so is the plan row, finalize raises `SERVICE_UNAVAILABLE` (same as dispatch) instead of silently skipping.
- `call_rows.seed_call` now takes an explicit `id=`, because the hold has to reference the call id before the call exists.
- Not wired into any call flow yet (T18b does that), so no e2e was run.
Gates: fmt/lint/typecheck/placeholders/spec refs pass; `make test` 10624 passed, 7 skipped, 99.17% coverage; metering.py at 100%; radon 2.75; jscpd 0 clones; pip-audit clean; pnpm audit 3 moderate, none high.

## Review P05-T18a (2026-11-02): approved
Re-ran lint, typecheck, placeholders, and spec refs: all pass. make test: 10624 passed, 7 skipped, 99.17 %; metering.py at 100 %.
Diff matches specs/24 §3-§5 and the Decision. Concurrency is serialised by the org FOR UPDATE lock (populate_existing). Tests cover the §13 metering table, idempotency, and concurrency.
