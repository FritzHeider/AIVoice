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

## Plan P05-T18b — PostCallWorkflow v1 (steps 1, 2, 3, 11, 13) + worker registration

Spec: specs/18 §1 (steps 1/2/3/11/13, ordering, `postcall_steps`, failure handling), §10, §11; specs/06 §11 Decision
P05-T14 item 3; specs/19 §2/§2.1 (`call.completed`, `RECORDING_MISSING`), §4 (Call Object, Decision P05-T16a).
Steps 4-10 and 12 are later phases; the plan list is built so they slot in by number.

### Design (record as `> Decision (2026-11-02): PostCallWorkflow v1 (P05-T18b)` under specs/18 §1)
1. Step names (`va_domain.post_call.PostCallStep` Literal + `POSTCALL_STEPS` tuple in spec order of the 13 names, only
   five implemented now): `finalize_call_record`, `await_recording`, `store_recording`, `finalize_billing`,
   `emit_completed`. `calls.config.postcall_steps` = JSON array of completed names, appended by the activity
   `mark_postcall_step` after each successful step (a crash between step and mark re-runs an idempotent step).
2. `finalize_call_record` ALWAYS runs (it returns the facts the workflow plans with; its writes are guarded): call must be
   terminal (`is_terminal`), else retryable `CallNotEnded`; missing call → non-retryable `CallNotFound` (workflow returns
   `call_not_found`, no other step). Backfills only missing values: `ended_at` NULL → primary leg `ended_at` or now;
   `ended_by` NULL with a known `end_reason` → `END_REASONS[reason]`; answered call with `ended_at` and
   `connected_seconds == 0 and transfer_seconds == 0` → `call_durations(answered_at, ended_at, transfer_started_at=` the
   `transfer_target` leg's `started_at`)`. Turns are not flushed here: the media end routine flushes its TurnWriter
   before the session closes (specs/07 §18 Decision P05-T11b). Returns `CallRecordFacts(connected=answered_at is not
   None, recording_wanted, recording_storage, done_steps)`. `recording_wanted` = `config.record is True` or
   `config.post_call_evals` truthy or `config.retranscribe is True`; `recording_storage` = the activity instance was
   built with storage. Wanted but no storage → log `postcall.recording_storage_missing`, skip 2-3.
3. await_recording (workflow logic, not one activity): activity `find_saved_recording` reads the stored
   `call.recording.saved` (`telnyx_events`, system scope via new `TelnyxEventRepo.recording_saved(call_id,
   call_control_ids)`: event_type match AND (`call_id = :id` OR `call_control_id IN` the call's leg ccids, which the
   activity reads in a tenant session first), newest `occurred_at` first, first row) → `CarrierEvent(type, leg="primary",
   call_id, org_id, telnyx_event_id, occurred_at, payload=row.payload["data"]["payload"])` or None. None → wait for
   signal `recording_saved` (handler stores the first `CarrierEvent`, ignores others) via
   `workflow.wait_condition(..., timeout=10 min)`. Timeout → activity `report_recording_missing` (`call.error`,
   `data={"code": "RECORDING_MISSING"}`, level `warning`, message "The call recording did not arrive.") — idempotent:
   skips when a `call.error` row with that code exists for the call. Signal arriving before the lookup returns is fine
   (check the signal slot first). On re-execution with `await_recording` done and `store_recording` not done → look up
   again (the edge stores every event before signalling); None → skip step 3.
4. Not connected (`answered_at` NULL): steps 1, 11, 13 only (specs/18 §1; step 6 is later). Connected: 1, (2, 3 when
   wanted+storage), 11, 13.
5. Failure handling: every step after 1 is wrapped; an `ActivityError` after retries is logged
   (`workflow.logger.warning("postcall.step_failed", step)`) and appended to `failed`; the workflow continues. Step 11
   failure: log only — the phase-15 reconcile scans ended calls (`BillingReconcileRepo.calls_ended_between`) so no
   separate list. `emit_completed` always runs last.
6. 30 min budget: the parent abandons the child, so the child itself races the pre-completion steps
   (`asyncio.wait({task}, timeout=1800)` on the workflow timer; cancel the task on timeout, `timed_out=True`) and then
   runs `emit_completed`. No workflow execution timeout.
7. Metrics: the catalog has `va_postcall_duration_ms{step}` (WORKER), not §11's names; activities observe it (ms) for
   finalize_call_record / find_saved_recording / finalize_billing / emit_completed. No failures counter (Temporal SDK
   metrics count activity failures). Record that.
8. `emit_completed`: in one tenant transaction, reload the call (terminal required; else non-retryable `CallNotEnded`
   result `not_ended`, logged error), skip when `queue_status == 'complete'` (`already_completed`, no event), set
   `queue_status='complete'`, build the Call Object with `load_call_objects(session, [call], recording_urls=...)` (7-day
   URL via new `va_common.storage.signed_recording_url`), persist `call.completed` (`data = object.model_dump(mode="json")`
   — use the same dump the api uses for GET /v1/calls/{id}; check `routers/calls.py`), commit, broadcast. Webhook
   delivery is phase 06 (the dispatcher reads the event).
9. `finalize_billing`: `va_billing.finalize_call` in `tenant_session`; `CallNotEndedError` → retryable `CallNotEnded`;
   `AppError CALL_NOT_FOUND/NOT_FOUND` → non-retryable `CallNotFound`; other `AppError` (SERVICE_UNAVAILABLE) → retryable
   `BillingUnavailable`. Returns `BillingResult(price_micros, hold, posted)`.

### Files
1. `packages/va_domain/src/va_domain/post_call.py`: add `PostCallStep`, `POSTCALL_STEPS`, `POSTCALL_STEPS_KEY =
   "postcall_steps"`, `RECORDING_WAIT_S = 600`, `POSTCALL_BUDGET_S = 1800`.
2. NEW `packages/va_workflows/src/va_workflows/activities/post_call_inputs.py` (import-free like recording_inputs):
   `PostCallStepInput(call_id, org_id, step)`, `CallRecordFacts`, `SavedRecording(event: CarrierEvent | None)`,
   `BillingResult`, `CompletedResult(status: Literal["emitted","already_completed","not_ended"])`, timeouts/retry
   policies: record 30 s/5 attempts; billing 30 s/10; emit 30 s/10; find/report/mark 10 s/5 (2 s doubling to 30 s),
   non-retryable error types listed (`CALL_NOT_FOUND`).
3. `activities/errors.py`: add `CALL_NOT_ENDED = "CallNotEnded"`, `BILLING_UNAVAILABLE = "BillingUnavailable"`.
4. NEW `activities/post_call.py`: `PostCallActivities(*, database, publisher, clock, storage: RecordingStorage-like
   (store, bucket) | None, step_ms: Histogram | None)` with `@activity.defn` methods `finalize_call_record`,
   `find_saved_recording`, `report_recording_missing`, `mark_postcall_step`, `finalize_billing`, `emit_completed`
   (all keyword `CallActivityInput`/`PostCallStepInput`). `mark_postcall_step`: one UPDATE
   `config = jsonb_set(config, '{postcall_steps}', coalesce(config->'postcall_steps','[]') || to_jsonb(:step))
   WHERE id/org AND NOT coalesce(config->'postcall_steps','[]') ? :step` (put the SQL builder in
   `va_db.post_call_steps` with `done_steps(config) -> tuple[str,...]` and `mark_step(session, org_id, call_id, step)`).
   Move `RecordingStorage` dataclass from `va_worker.activities` to `va_common.storage` (worker re-imports it).
5. `packages/va_common/src/va_common/storage.py`: `RecordingStorage`, `signed_recording_url(store, bucket, key, *, now)
   -> SignedUrl | None` (ClientError/BotoCoreError/ValueError → warning `storage.recording_url_failed`, None);
   `va_api.services.call_read.recording_urls` loops over it (behaviour unchanged, keep its log event name via the
   helper or adjust the api test that asserts `calls.recording_url_failed`).
6. `packages/va_db/src/va_db/repositories/telnyx_events.py`: `recording_saved(call_id, call_control_ids) ->
   TelnyxEvent | None` (add to the specs/03 §2 Decision text for TelnyxEventRepo methods).
7. NEW `packages/va_workflows/src/va_workflows/post_call/` (`__init__` exports `PostCallWorkflow`):
   `plan.py` (pure: `planned_steps(facts) -> tuple[PostCallStep,...]`, `remaining(planned, done)`),
   `results.py` (`PostCallResult(status: "completed"|"call_not_found", ran, skipped, failed, recording:
   "stored"|"already_stored"|"failed"|"missing"|"not_wanted"|"no_storage"|"skipped", price_micros|None,
   timed_out, completed: CompletedResult status|None)`), `post_call_workflow.py`
   (`@workflow.defn(name=POSTCALL_WORKFLOW_NAME)`, `@workflow.signal(name=RECORDING_SAVED_SIGNAL)`, a
   `@workflow.query` `postcall_phase`). Activities scheduled by name (pattern of CallWorkflow).
8. `va_workflows/registration.py`: `POSTCALL_WORKFLOWS = (PostCallWorkflow,)`.
9. `apps/worker/src/va_worker/activities.py`: `WorkerActivities.post_call: PostCallActivities` (always built;
   storage passed through), `postcall_queue()` = post-call methods + `store_recording` when storage;
   `main_registrations` always returns both registrations, post-call with `POSTCALL_WORKFLOWS`. Docstrings updated.
   Metrics: `create_app` builds `metrics = build_metrics(Service.WORKER)` once (ProbeState registry) and the container
   factory gets it: `ContainerFactory = Callable[[WorkerSettings, ServiceMetrics], ...]`, lifespan passes
   `lambda s: container_factory(s, metrics)` to `service_scope`; `build_container(settings, metrics)` →
   `WorkerActivities.build(..., metrics=metrics)` → `metrics.histogram("va_postcall_duration_ms")`. Update worker test
   fakes accordingly.
10. specs/18 Decision (above), specs/03 §2 TelnyxEventRepo method note, specs/27 note only if catalog text needs it
    (no catalog change). AGENTS.md dated line (P05-T18b).

### Tests
- NEW `packages/va_workflows/tests/support/postcall_rig.py`: `FakePostCallActivities` (production names, scripted
  results/exceptions per activity incl. `store_recording`, records calls in order, `send={name: [signals]}` like
  FakeCallActivities), `run_post_call(env, fakes, facts=..., signals=...) -> (PostCallResult, history)`.
- NEW `tests/test_post_call_workflow.py` (time-skipping): connected+record order = record → find → store → billing →
  emit with marks after each; not connected = record, billing, emit only; record=false skips 2-3; no storage skips 2-3;
  stored event found → no wait; signal delivered (from inside `find_saved_recording` fake returning None) → store gets
  the signalled event; no signal → 10 min timer then `report_recording_missing`, no store, emit still runs (assert
  TIMER_STARTED 600 s from history); `done_steps` from facts skip completed steps (billing+emit already done → only
  record runs; await done but store not → find again then store); store/billing failures (non-retryable fakes) →
  `failed` lists them, emit still runs; emit failure → result shows failed; `CallNotFound` from step 1 → only step 1;
  budget: a fake billing that hangs past 30 min (use a long `asyncio.sleep` inside a fake activity is NOT skippable —
  instead have the wait for the recording signal + a fake whose scripted timer pushes past the budget: set the budget
  shorter via module constant monkeypatch is not possible in sandbox — so test the race with a workflow-level
  parameter `PostCallWorkflowInput` stays unchanged; put the budget in `post_call.plan.POSTCALL_BUDGET` and test
  `timed_out` by a fake `find_saved_recording` None + no signal where RECORDING_WAIT > budget is impossible) → resolve:
  implement the race against `POSTCALL_BUDGET_S` but pass the budget and recording wait through
  `PostCallTimings` read from `PostCallWorkflowInput.timings: PostCallTimings | None = None` (defaults = spec values;
  `CallWorkflow` never sets it) so the test can set budget 60 s < wait 600 s and assert `timed_out` + emit ran.
  Add `timings` to the domain input model (extra field, default None).
- Pure `tests/test_post_call_plan.py` for `planned_steps`/`remaining`.
- NEW `tests/test_post_call_activities.py` (DB, `telephony_rig` style or `call_rows.seed_call`, SystemClock):
  finalize_call_record backfills (ended_at/ended_by/durations from legs incl. transfer_target) and leaves set values;
  non-terminal → `CallNotEnded` ApplicationError retryable; unknown → `CallNotFound` non-retryable; recording_wanted
  combos; find_saved_recording by call_id and by ccid, None when absent, payload = data.payload; report_recording_missing
  writes one `call.error` (twice → still one); mark_postcall_step appends once (twice → once, order kept);
  finalize_billing + emit_completed: `call.completed` data `price` == `price_micros / 1e6` for connected, transferred,
  failed (no_answer with leg) calls; emit twice → one event, `already_completed`; queue_status 'complete'; recording
  URL present with a `FakePresigner`-like store (reuse `recording_rig.MemoryS3` + real ObjectStore) and absent without
  storage; histogram observed per step.
- `packages/va_db/tests`: `TelnyxEventRepo.recording_saved` test (system session) in the existing telnyx_events test
  module; `va_db.post_call_steps` tests.
- `packages/va_common/tests`: `signed_recording_url` success + failure → None.
- Worker: `apps/worker/tests/test_container.py` registration test — post-call queue has `PostCallWorkflow` and the six
  activities (+ `store_recording` with storage); main queue unchanged. Keep `test_worker_call_flow.py` green (default
  record=false → post-call runs 1, 11, 13 against the real DB; adjust its assertions if it inspects post-call state).

### Gates
make fmt, lint, typecheck, placeholders, spec refs; `uv run pytest packages/va_workflows packages/va_db
packages/va_common apps/worker apps/api -q -k "post_call or postcall or recording or container or telnyx_event or
call_read"` while iterating; full `make test` in background with a Monitor heartbeat; jscpd, radon, pip-audit, pnpm audit.
No e2e harness yet (P05-T17).

### Risks
- Sandbox: workflow module may import only import-free modules inside `imports_passed_through` (post_call_inputs,
  va_domain.post_call, errors).
- `asyncio.wait` with timeout in Temporal Python workflows is deterministic (workflow timer); cancel the inner task and
  await it (catch `CancelledError`) before `emit_completed`.
- `test_worker_call_flow.py` now runs a real PostCallWorkflow; make sure the worker test waits for/terminates it or the
  env teardown hangs.
- Time-skipping: a running activity blocks skipping; the 10 min recording wait happens with no activity running.
- `CallObject` dump must match the API's JSON exactly (reuse the router's serialization call).

## Build P05-T18b (2026-11-02)
Built as planned. Where it differs from the plan:
- `emit_completed` and `finalize_call_record` are both in `plan.ALWAYS_RUN`, so step 13 runs on every execution and is never listed as skipped.
- When the budget runs out the body is cancelled. The step wrappers catch `ActivityError`, so `_begin`/`_fail` re-raise `CancelledError` once `_out_of_time` is set; otherwise a cancelled activity would let billing still run.
- If a `recording_saved` signal arrives while `find_saved_recording` is running, the signal wins over the lookup result.
- `RecordingStorage` moved to `va_common.storage`, which also gains `signed_recording_url`. `va_api.call_read.recording_urls` now uses it and keeps its log event name.
- `build_container(settings, metrics=None)`: `create_app` passes the probe registry through `partial`, so the container-factory signature for test fakes is unchanged.
- Worker e2e: the canceled dialed call now runs a real post-call child to completion (steps 1, 11, 13), and is charged the attempt fee.
Gates:
- fmt/lint/typecheck/placeholders/spec refs: pass.
- make test: 10673 passed, 7 skipped, 99.14% coverage. post_call activities at 98%, workflow at 93%.
- After `_backfill` was split, the targeted tests were re-run: 80 passed.
- radon average 2.90, no C grade. jscpd 0 clones. pip-audit clean. pnpm audit: 3 moderate, none high.
- No `tests_e2e` yet (P05-T17), so no e2e run.

## Review P05-T18b (2026-11-02)
Lint, typecheck, and placeholders were re-run and pass; make test was re-run as well (result below). The diff matches specs/18 §1 and the Decision for steps 1/2/3/11/13, the registrations, mark_step, the recording lookup, and the emit_completed Call Object dump. One defect remains in the budget compensation, which this task's Decision (item 5) promises:

1. `packages/va_workflows/src/va_workflows/post_call/post_call_workflow.py:227` (`_find_saved_recording`), `:243`
   (`_report_recording_missing`), and `:326` (`_mark`) catch `ActivityError` and return without checking `_out_of_time`.
   Temporal reports a cancelled activity as `ActivityError(cause=CancelledError)`, as the builder's own `_begin` docstring says.
   - If the 30 min budget runs out while `find_saved_recording` is running, `body.cancel()` is used up by that activity.
   - `_find_saved_recording` then returns None, and `_await_recording` goes into `wait_condition` for the full `recording_wait_s`.
   - `run()` awaits `body` with no timeout, so `emit_completed` happens up to 10 min after the budget instead of at it.
   - The same happens when the budget runs out during `report_recording_missing`: that activity's swallowed cancel lets the code go on and call `_mark`.
   - Required: in every `except ActivityError` branch, re-raise `asyncio.CancelledError` when `self._out_of_time` is set (the same guard as `_fail`), and check it again before `wait_condition`.
2. No test covers the budget expiring while an activity is running. That is the path the `_begin`/`_fail` CancelledError re-raise was written for. The only budget test (`test_the_budget_cancels_the_wait_and_still_completes`) cancels during the signal wait, with no activity running.
   - Required: add time-skipping tests with a short `PostCallTimings.budget_s` and a fake activity that blocks until cancelled (heartbeating, so the cancel is delivered). Cover one case for `finalize_billing` and one for `find_saved_recording`.
   - Each test asserts that `timed_out` is True, that `emit_completed` ran right after the budget, and that no later step ran (for example, no `finalize_billing` after the cancelled find). Assert on history timers, not on wall time.
   - Keep the AGENTS.md P05-T13 gotcha in mind (the server wedges when a failing or completing dispatched activity is cancelled): the fake must block and react to the cancel, not fail.
make test (reviewer re-run): 10673 passed, 7 skipped, 99% total coverage. post_call_workflow.py is at 93%; the uncovered lines are 227-229, 243-245, 306, 312. These are exactly the budget-cancel and swallow branches in findings 1-2.

## Build P05-T18b fix (2026-11-02): review findings 1-2
1. `PostCallWorkflow._raise_if_out_of_time()` (shared by `_begin`/`_fail`) is now the first statement of every
   `except ActivityError` branch that swallows (`_find_saved_recording`, `_report_recording_missing`, `_mark`) and runs
   before the recording `wait_condition`, so a budget cancel delivered as `ActivityError(cause=CancelledError)` ends the
   body at once.
2. `postcall_rig.FakePostCallActivities(block=frozenset({...}))`: the named activity heartbeats until cancelled
   (`cancelled` lists it); the rig worker uses the 100 ms heartbeat throttle. New tests with `budget_s=2` (a running
   activity keeps the time-skipping server on wall time): budget during `finalize_billing` and during
   `find_saved_recording` (timers == [budget] only, no recording wait/report/billing; fails without fix 1). Plus plain
   lookup failure → wait → missing, and report failure → no `await_recording` mark. post_call_workflow.py 98 %.

## Review P05-T18b fix (2026-11-02): approved
Findings 1-2 resolved: `_raise_if_out_of_time` guards every swallowing `except ActivityError` and the recording wait;
blocking-activity budget tests for finalize_billing and find_saved_recording assert timers == [budget], cancelled
activity, no later steps. Reviewer re-run: lint/typecheck/placeholders pass; make test 10677 passed, 7 skipped, 99%;
jscpd 0 clones; radon A (3.0).

## Planner 2026-11-02: P05-T17 split
P05-T17 is too big for one Builder pass. Missing prerequisites:
- No application services in compose.
- The migrations are not in any wheel.
- The accelerated sim clock starts at a fixed virtual date (`AcceleratedClock` DEFAULT_START), so webhook timestamps would fail the edge's wall-clock tolerance.
- No `/internal/test/orgs`.
- No webhook dispatcher until phase 06.

Split into P05-T17a–e in plan/phase-05.md:
- a: compose app services
- b: harness plus outbound_basic, inbound_basic, stop_via_api, max_duration
- c: voicemail, IVR, and cold transfer flows
- d: barge_in, interruptibility_levels, worker_crash_recovery
- e: the 50-concurrent-call check

## Plan P05-T17a — Compose application services (`apps`/`e2e` profiles)

### Design decisions
Record all of these as `> Decision (2026-11-02): application services (P05-T17a)` under specs/29 §2, plus a short note under specs/30 §2/§5 for the clock.

1. **Migrations are packaged.**
   - Move `packages/va_db/migrations/` with `git mv` to `packages/va_db/src/va_db/migrations/` (`env.py`, `script.py.mako`, `versions/`). uv_build ships non-`.py` files under the module directory.
   - `alembic.ini` gets `script_location = %(here)s/src/va_db/migrations`, so `make migrate` is unchanged.
   - Update `pyproject.toml`:
     - ruff per-file-ignores paths: `packages/va_db/src/va_db/migrations/**` and `.../versions/0001_initial.py`.
     - mypy `files`: drop `packages/va_db/migrations`, since `src` covers it now.
     - Check for a coverage omit on migrations and keep it equivalent.
   - Update `packages/va_db/tests/support/migration_helpers.py` (`VERSIONS` path) and any other path reference: `grep -rn "va_db/migrations\|\"migrations\"" --include=*.py` and the Makefile.
   - `migrations/__init__.py`: Alembic does not need it. If ruff INP001 complains under `src`, keep the per-file ignore. Do not add an `__init__.py` to `versions` (revision modules are loaded by path).
2. **NEW `packages/va_db/src/va_db/migrate.py`.**
   - `main(argv=None) -> int`: argparse `--months-ahead` (default `partitions.DEFAULT_MONTHS_AHEAD`) and `--sql`? No `--sql`; keep it minimal: `upgrade` only.
   - It builds `alembic.config.Config()` programmatically with `script_location = "va_db:migrations"` (package resource notation) or `str(importlib.resources.files("va_db") / "migrations")`. Then it calls `alembic.command.upgrade(cfg, "head")`, which goes through env.py, reads `DATABASE_URL`, and runs as the login role.
   - It then runs `ensure_call_event_partitions(conn, now=SystemClock().now(), months_ahead=...)` in one login-role transaction (`create_engine(settings, None)`, as P01-T13 requires).
   - It logs the created partition names (configure_logging with `service_name` "migrate").
   - Exit code: 0 on success, 1 on failure, with the error logged (no traceback swallowing beyond what Alembic prints).
   - Do not call `asyncio.run` inside env.py's loop: `command.upgrade` is sync and env.py runs its own `asyncio.run`. Run partitions afterwards with a separate `asyncio.run`.
   - `python -m va_db.migrate` needs `if __name__ == "__main__": raise SystemExit(main())`.
   - Tests in `packages/va_db/tests/test_migrate.py`, sync, on `fresh_database(pg_url)`:
     - `main([])` against a fresh DB migrates to head (check `alembic_version` = current head through `migration_helpers.query`).
     - It creates `call_events_pYYYYMM` for the current month + 3 ahead.
     - A second run is a no-op that returns 0.
     - A bad `DATABASE_URL` (UNREACHABLE) returns 1.
   - Set `DATABASE_URL` with monkeypatch. Settings come from env; check how env.py reads them.
3. **docker-compose.yml.** Add the spec §2 `x-py-env` anchor and the services `migrate`, `api`, `edge`, `media`, `worker`, `webhooks`, `events-sink`, exactly as in the spec block, with these deviations (all in the Decision):
   - a. `x-py-env.environment` also sets:
     - `TELNYX_PUBLIC_KEY: ""`. `.env` holds the key of the previous sim start, because `make up` rewrites it after the wait.
     - `PUBLIC_API_BASE_URL: http://api:8000`? NO: keep the host values for anything clients see. Override only the service-to-service URLs listed in the spec, plus `SMTP_URL` (already listed).
     - Audit `.env.example` for any other `localhost` URL that a service dials (`grep localhost .env.example`). Leave browser- or client-facing values alone.
   - b. Named volume `simkeys`:
     - `sim` mounts `simkeys:/app/.sim`.
     - edge, media (and api/worker if their settings use the key; check `TelnyxPublicKeySettings` users) mount `simkeys:/app/.sim:ro`.
     - In `local`, `webhook_public_key()` falls back to `.sim/telnyx_public_key` relative to cwd `/app`.
     - edge and media gain `depends_on: {sim: {condition: service_healthy}}`, because the key file must exist before their lifespan reads it.
     - Check that the Dockerfile's `install -d -o 10001 /app/.sim` gives the volume the right ownership: a fresh named volume copies the image directory's ownership, so the sim (uid 10001) can write.
   - c. Healthchecks, same python-urllib form as api/sim, each with `timeout: 3s`, `restart: unless-stopped`, and `OTEL_EXPORTER_OTLP_ENDPOINT` from the anchor:
     - edge `http://localhost:8100/readyz`
     - media `:8200/readyz`
     - worker `:9300/readyz`
     - webhooks `:9400/readyz`
     - events-sink `:9500/readyz`
     - Verify the actual probe ports in each app's settings/main. The worker, dispatcher, and sink publish no host ports, so the fake LLM and fake Stripe ports cannot collide.
     - `migrate` gets `restart: "no"`.
   - d. `media` `stop_grace_period: 60s` as in the spec. The `webhooks` `environment: {WEBHOOK_ALLOW_PRIVATE: "true"}` must MERGE with the anchor's environment. YAML `<<` does not deep-merge, so write the full environment mapping for webhooks: the anchor's keys + `WEBHOOK_ALLOW_PRIVATE`. Use a second anchor `&py-env-vars` for the mapping and `<<: *py-env-vars` inside it.
   - e. `migrate` runs `python -m va_db.migrate` from the api image (`SERVICE: api, MODULE: va_db.migrate`, as in the spec; va_db is an api dependency, so the module is in the venv).
4. **docker-compose.e2e.yml** sets `SIM_CLOCK: realtime`. Rewrite its comment to explain why (this is the Decision under specs/29 §2 and the specs/30 §2 note):
   - `AcceleratedClock` virtual time starts at a fixed date and jumps whenever the sim's loop idles.
   - Real services stamp and verify against wall time: Telnyx webhook signature tolerance at the edge, media WS signature timestamps, Temporal timers, and 20 ms media pacing.
   - So an accelerated sim cannot drive a real stack.
   - Keep the file so later e2e-only sim settings have a home. Its content becomes `services.sim.environment.SIM_CLOCK: realtime`.
   - Alternative: delete the override and its dev_up special case. Do NOT do that; the spec names the file.
   - Also update the AGENTS.md P03-T15b line that says the e2e profile adds SIM_CLOCK accelerated.
5. **scripts/dev_up.sh.**
   - When the selected profiles include a service named `migrate` (apps/e2e), run `docker compose ... run --rm --build migrate` after postgres is healthy and before `up --wait` of the long-running services.
   - Add `migrate` to `ONE_SHOT_SERVICES`. The existing one-shot handling runs those AFTER the wait, so introduce a separate `PRE_WAIT_ONE_SHOT_SERVICES=(migrate)`. Sequence:
     - `up -d --wait --build postgres`
     - `run --rm migrate`
     - `up -d --wait --build <long-running services>`
   - `docker compose up --wait` exits 1 when a dependency one-shot exits (AGENTS P00-T09). If `depends_on: migrate: service_completed_successfully` still makes `up --wait` fail, or re-run migrate, remove `migrate` from the app services' `depends_on` and keep the ordering in dev_up (deviation in the Decision). Verify for real with Docker; it is available on this host (Docker 29.9).
   - The sim key step stays as is.
6. **Tests.**
   - `scripts/tests/test_compose.py`:
     - `SERVICES` list + new services.
     - `test_build_services_are_sim_only` → the build services equal the spec's build services, with matching build args, MODULE and SERVICE.
     - Each app service matches the spec (profiles, ports, depends_on modulo the documented deviations, stop_grace_period).
     - The anchor environment equals the spec's + the `TELNYX_PUBLIC_KEY: ""` addition.
     - The webhooks env includes `WEBHOOK_ALLOW_PRIVATE`.
     - `simkeys` is mounted rw on sim and ro on edge/media.
     - Every app healthcheck targets that app's probe port.
     - The e2e override test now asserts realtime; rename `test_e2e_override_only_accelerates_the_sim_clock` → `test_e2e_override_pins_the_realtime_sim_clock`, and update `test_docker_compose_e2e_override_renders_*`.
     - The dev_up fake-tool tests: an apps/e2e run logs `run --rm` migrate before the final `up --wait`. The infra run has no migrate step.
     - The ports test: the AGENTS.md Ports line already lists api 8000, edge 8100, media 8200; check it.
   - `scripts/tests/test_dockerfile.py` stays unchanged unless the Dockerfile changes. It should not need to: models/assets are copied and the migrations now ship inside the va_db wheel.
   - Build the api image and check `docker run --rm --entrypoint python va-api:dev -c "import importlib.resources as r; print((r.files('va_db')/'migrations'/'env.py').is_file())"` prints True. That is evidence only, not a pytest.
7. **specs/02 §6 table / `.env.example`:** no new variables, so no change, unless a new var is needed. Do not add any.

### Manual verification (evidence required in build.done)
- `make down && docker volume rm voiceagents_pgdata` is NOT needed. Use `scripts/dev_down.sh -v` only if the main stack has nothing to keep: dev data is reseedable, and `make seed` rebuilds it.
- Then `make up PROFILE=e2e`, then `docker compose -f docker-compose.yml -f docker-compose.e2e.yml --profile e2e ps`: all long-running services healthy, and migrate exited 0.
- `curl -fsS localhost:8000/readyz`, `localhost:8100/readyz`, `localhost:8200/readyz`.
- `docker compose logs edge media | grep -i "public key"`: no refusal.
- `make seed && curl -H "Authorization: <test key>" localhost:8000/v1/me` returns 200.
- Optional smoke: `POST /v1/calls` to a FakeTelnyx default number reaches `in_progress`/`completed`. Not required; P05-T17b owns flows.
- Then `make up` (infra) to restore the normal dev stack, or leave it running for T17b and say which.
- Image builds are slow. Run `make up` with Bash `run_in_background` + Monitor heartbeats (AGENTS P05-T10d inactivity rule).

### Gates
make fmt, lint, typecheck, placeholders, spec refs; `uv run pytest scripts/tests packages/va_db -q` while iterating; full `make test` in the background with a heartbeat; jscpd, radon, pip-audit, pnpm audit.

### Risks
- Moving migrations changes `__file__`-relative paths in env.py and in the tests: grep thoroughly. `test_schema_matches_spec` / `load_revision` use `VERSIONS`.
- ruff may newly flag migration files under `src` with rules applied only to src (D, ANN?). Keep the per-file ignores equivalent.
- Coverage: migrations under `src` now count toward va_db coverage. env.py's offline branch is exercised by the `--sql` tests. If coverage drops below the threshold, add a coverage omit for `*/va_db/migrations/*` (they are exercised by the migration tests in subprocess-less runs, so they are probably covered anyway).
- Container user 10001 writing to the `simkeys` volume.
- The media container's real Silero model: `models/` is copied and `VA_MODELS_DIR=/app/models`. `make setup` must have fetched `silero_vad.onnx` before the build; `.dockerignore` keeps `models/`.
- The worker needs `TELNYX_CALL_CONTROL_APP_ID`. In local the sim has no app until `scripts/telnyx_bootstrap.py --write-env` runs. Check what the worker does when it is empty: if `build_container` raises, readiness fails. Either run the bootstrap in dev_up after the sim is up (writing `.env` before the app services start: reorder to sim → key → bootstrap → apps), or document it. Prefer automating it in dev_up when `worker` is selected: `uv run python scripts/telnyx_bootstrap.py --env-file .env --write-env`, which is idempotent? Verify. Because `.env` is read by `env_file` at container create, the bootstrap must happen before `up` of the apps. FakeTelnyx resources reset on every sim restart, so it runs each time.

## Build P05-T17a (2026-11-02)
- Migrations moved (git mv) to `packages/va_db/src/va_db/migrations/`; alembic.ini/pyproject ruff+mypy paths/
  migration_helpers.VERSIONS/specs/03 path updated. NEW `va_db.migrate` (`python -m va_db.migrate`, `make migrate`):
  packaged alembic upgrade head + `ensure_call_event_partitions` (current month + 3); exit 0/1; tests test_migrate.py.
- docker-compose.yml: `x-py-env` anchor (+ `&py-env-vars`, `TELNYX_PUBLIC_KEY: ""`), services migrate/api/edge/media/
  worker/webhooks/events-sink (healthchecks on 8000/8100/8200/9300/9400/9500), `simkeys` volume (sim rw, edge/media
  ro, both depend on sim healthy). Dashboard deferred (no dashboard.Dockerfile).
- Found during real bring-up: (a) Compose dotenv reads `KEY=  # comment` as `# comment` → every blank `.env` key
  (SENTRY_DSN, *_PREVIOUS peppers/secrets) broke all apps. Fix: `scripts/compose_env.py` renders `.env` via
  python-dotenv to single-quoted `.env.compose`; anchor `env_file: [{path: .env.compose, required: false}]`
  (required false also keeps CI `docker compose config` working without .env). (b) `va_telnyx.webhook_urls` hardcoded
  https → sim could never reach the compose edge; added `webhook_scheme(va_env)` (http in local/test, like ws media)
  used by telnyx_bootstrap.py and VoicemailSms.
- dev_up.sh stages: deps up --wait → minio-init → sim key → (apps) telnyx_bootstrap --write-env with
  EDGE_PUBLIC_HOST=edge:8100 → compose_env.py → run --rm --build migrate → up --wait --no-deps apps
  (--force-recreate when the sim key changed). e2e override = SIM_CLOCK realtime. Decisions: specs/29 §2, specs/30 §3.2.
- Evidence: clean volumes (`dev_down.sh -v`) + `make up PROFILE=e2e` exit 0, every service healthy (migrate ran
  0001→0005, partitions p202611..p202702); readyz 8000/8100/8200 = 200; edge container key file == host key, no
  `#`-valued env; sim SIM_CLOCK=realtime; `make seed` + `/v1/me` with test key = 200; `make down` afterwards.
- Gates: make test 10717 passed / 7 skipped, 99.17 %; lint/typecheck/placeholders/spec refs 0; jscpd 0 clones; radon A (2.08); pip-audit clean; pnpm audit 3 moderate (no high). make e2e: no tests_e2e harness until P05-T17b. Infra stack restored with make up.

## Review P05-T17a (2026-11-02): approved
Reviewer re-ran: lint/typecheck/placeholders/spec refs 0; jscpd 0 clones; compose e2e config lists all 7 app services.
make test: 10716 passed, 7 skipped, 99.16 %, 1 failure = test_runtime_sim.py::test_the_background_bed_fills_the_agent_track_between_utterances[None-True--40.0]
(P05-T15, untouched here; wall-clock window 2000-2800 ms still caught the opener under full -n auto load: -17 dBFS).
Passes 3/3 alone and the module passes with -n 8, so it is a pre-existing load flake; follow-up task fix:bed-window-flake.
Notes for later: test_migrate asserts head == "0005" (update it with each new migration); APP_SERVICES lists dashboard ahead of its Dockerfile (harmless).
