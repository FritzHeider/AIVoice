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

## Plan P05-T17b — E2E harness + outbound_basic, inbound_basic, stop_via_api, max_duration

Facts gathered (2026-11-02):
- `tests_e2e/` does not exist. `make e2e` runs `uv run pytest tests_e2e -m e2e`, and `testpaths` excludes it, so `make test` never collects it.
- The compose e2e stack comes from T17a:
  - Host ports: api 8000, edge 8100, media 8200, sim 9100/9200/9300.
  - The sim uses the realtime clock. Webhooks go to `edge:8100`; media streams go to `media:8200`.
- `/sim/reset` would wipe the call control app created by `telnyx_bootstrap` (TELNYX_CALL_CONTROL_APP_ID). The harness must NEVER call `/sim/reset`. Isolation comes from run-unique ids instead.
- `POST /sim/telnyx/inbound {to, from, scenario?}` needs `to` to be owned by a sim account AND assigned to a connection. The sim inventory is fixed: 50-number blocks, reset on sim restart.
- FakeLLM rules: `POST /sim/llm/rules` (YAML/JSON `RuleFile`), `DELETE /sim/llm/rules/{id}`; match on `system_contains`, `call_index`, `call_id`.
- Sim timeline: `GET /sim/telnyx/calls/{ccid}/timeline` → `utterances[{start_ms,end_ms,first_agent_audio_ms,latency_ms}]`, `initiated_ms`, `answered_ms`, `ended_ms`, `script.steps`. `GET /sim/telnyx/calls` lists summaries (find a call by `to`/`from`).
- Phone numbers: `phone_numbers.e164` is unique among active rows across orgs. `PhoneNumberRoutingRepo.get_active(e164)` (system) finds a stale owner.
- Calling hours: `calling_hours_open` uses org `compliance.calling_hours` + `state_quiet_hours`. E2E runs at any hour, so the test org must open every window.

### 1. `POST /internal/test/orgs` (api)
- NEW `apps/api/src/va_api/routers/test_orgs.py` (+ `test_orgs_schemas.py`).
  - Router `internal_test_router = APIRouter(prefix="/internal/test", include_in_schema=False)`, built the same way as `internal_auth_router` (check how `/internal/auth` is built and how `app.state.internal_openapi()` collects it).
  - Add it to `main.ROUTERS`.
  - Operation `internal_test_orgs_create`, tag `internal`, `openapi_extra={"x-scope": "public", "x-public": "test-seed (VA_ENV local/test only)", **audit_exempt("test-only seed endpoint")}`.
  - Outside `VA_ENV` local/test it answers 404 `NOT_FOUND` (route exists, behaves absent). Use `request.app.state.settings.va_env`.
- Body `TestOrgRequest` (extra forbid):
  - `name: str = "E2E org"`
  - `plan_id: str = "build"`
  - `dollars: Decimal-string = "50"` (≥ 0)
  - `settings: dict = {}`: sparse OrgSettings. Validate with `OrgSettings.model_validate` → `sparse_settings` merge; errors → `INVALID_INPUT` with `field_path`.
  - `voices: list[{name, provider_voice_id}]` (1..10)
  - `numbers: list[{e164 (normalize_e164), telnyx_phone_number_id: str|None, capabilities: list = ["voice"], inbound_config: dict = {}}]` (0..10). Validate `inbound_config` with `va_domain.numbers.load_inbound_config` → `INVALID_INPUT` field `numbers[i].inbound_config.<f>`.
- Behaviour, in this order and never nesting sessions:
  - (a) For each number: `system_session` + `PhoneNumberRoutingRepo.get_active(e164)`. When an active row exists (from a previous run after a sim restart), set it `status='released'`, `released_at=now` in `tenant_session(old.org_id)`. This is test-only and documented in the Decision.
  - (b) Provision the org through the container's `OrgProvisioner`. The owner email is `e2e+<hex>@voiceagents.test` with a random password that is never returned (reuse the DevSeeder pieces in `va_api.dev_seed` where possible, e.g. its password/owner creation, instead of copying).
  - (c) In one `tenant_session(org)`:
    - set `orgs.plan_id`, `created_at = now − 30 days` (otherwise new-account limits apply), and settings (OrgRepo.lock + write sparse; invalidate the org_settings reader after commit);
    - `grant_credits(..., idempotency_key=f"test-org:{org_id}")`;
    - insert voices (org rows, provider elevenlabs, provider_voice_id) and numbers (status active, country from the e164, `purchased_at=now`);
    - mint one live key with scopes `["*"]` (`mint_api_key` + `ApiKeyRepo.add`).
- Response 201 `DataResponse[TestOrgResult]` `{org_id, api_key, voices:[{id,name}], numbers:[{id,e164}]}`. Give the response model an `examples` entry, since the operations contract test applies.
- `withhold_on_replay` is not needed: no idempotency claim, because the route has no `require_scope`.
- Run `make openapi` (internal.json changes) and add the path to `test_openapi.INTERNAL_PATHS`. The cross-tenant suite skips it via `x-public`.
- Tests `apps/api/tests/test_test_orgs.py` (session container + module Postgres like the other integration tests):
  - creates a usable org: the returned key authenticates `/v1/me` 200, balance = $50, voices and numbers listed;
  - a Send Call with the returned org/voice/number passes validation and dispatches (`call_fakes.FakeCallStarter.started`) at a time outside default calling hours, when settings open all windows;
  - VA_ENV production/staging → 404;
  - an invalid inbound_config → 422 `INVALID_INPUT` with field;
  - a re-used e164 releases the old org's row and the new row is active;
  - unknown body field → 422.

### 2. `tests_e2e/` harness (repo root; add `tests_e2e` to pytest `pythonpath`, mypy `files` + `mypy_path`; ruff per-file ignores same as `tests_live/**`)
- `tests_e2e/conftest.py`:
  - module `pytestmark` not needed; mark each test `@pytest.mark.e2e` and `@pytest.mark.timeout(600)` if pytest-timeout is installed (check; otherwise a harness-level deadline via `asyncio.timeout` in each wait helper).
  - Session fixture `stack`: probes `GET localhost:8000/readyz`, `8100/readyz`, `8200/readyz`, `9100/sim/state` (5 s total). If any is down, call `pytest.exit("e2e stack is not running: run make e2e (or make up PROFILE=e2e)", returncode=3)`. Never skip silently.
  - Function fixture `harness` yields a `Harness` and on teardown deletes its registered LLM rules and scenarios (`DELETE /sim/telnyx/scenarios/{id}`, `/sim/llm/rules/{id}`).
  - Event loop: follow the repo's pytest-asyncio settings (asyncio_mode). Function-scoped clients only, to avoid loop-scope trouble.
- `tests_e2e/harness.py`:
  - `Stack` URLs come from env with defaults: `E2E_API_URL` (`http://localhost:8000`), `E2E_SIM_URL` (`http://localhost:9100`; ElevenLabs 9200, LLM 9300).
  - `Harness.run_token` = `secrets.token_hex(4)`.
  - `async create_org(**settings) -> E2eOrg`: calls `POST /internal/test/orgs` with:
    - a voice named `OrgSettings().default_voice`'s default value, with `provider_voice_id=va_sim.elevenlabs.voice_catalog.KAREN_VOICE_ID`;
    - settings opening calling hours: every weekday 00:00–24:00 (check `CallingWindow` format; use the widest valid window) and `state_quiet_hours: false`;
    - a random `from` number (`+1628` + 7 random digits, valid per phonenumbers; reuse the `random_e164` approach).
  - `load_scenario(name, **overrides) -> Scenario`:
    - reads `tests_e2e/scenarios/<name>.yaml` and validates it locally with `va_sim.scenario.load_scenario`;
    - rewrites `id` to `<name>-<token>` and `match.to`/`match.inbound_from` to a fresh random E.164;
    - POSTs the YAML/JSON to `/sim/telnyx/scenarios`;
    - registers each `llm_rules` file (relative to `tests_e2e/scenarios/`) via `POST /sim/llm/rules`, after substituting `{{run}}` → token in rule ids and in the `[[SCENARIO:<name>-<token>]]` marker.
    - Rules live in `tests_e2e/scenarios/rules/*.yaml`.
    - Read the control body format in `va_sim.control.yaml_or_json_body`.
  - `scenario_marker(name)` returns `[[SCENARIO:<name>-<token>]]` (goes into `task`).
  - `async send_call(org, body) -> call_id`: `POST /v1/calls` with Bearer key; asserts 200 and the `call_id`.
  - `async wait_complete(org, call_id, timeout_s=180) -> dict`: polls `GET /v1/calls/{id}` every 0.5 s until `queue_status == "complete"`. Use `asyncio.sleep` on the wall clock; this is an external system, so the no-sleeps rule targets unit tests. Use a bounded deadline with a clear failure message that includes the last body.
  - `async sim_call(to=None, from_=None) -> dict`: summary from `GET /sim/telnyx/calls` matching the scenario number, newest; `timeline(ccid)`.
  - `async events(org, call_id) -> list`: all pages of `GET /v1/calls/{id}/events`.
  - `async order_inbound_number() -> (e164, telnyx_number_id)`:
    - sim REST with `Authorization: Bearer sim-telnyx-key`;
    - finds the platform call control app id by listing `GET /v2/call_control_applications` and matching `va_telnyx.bootstrap.platform_resource_name(...)` (check the signature; fallback is a clear failure);
    - `GET /v2/available_phone_numbers?filter[country_code]=US&filter[limit]=1`, then `POST /v2/number_orders {phone_numbers:[{phone_number}], connection_id}`, then poll `GET /v2/number_orders/{id}` until `success`;
    - reads the `phone_numbers` id via `GET /v2/phone_numbers?filter[phone_number]=`.
    - Teardown: `DELETE /v2/phone_numbers/{id}` so the inventory number returns.
  - `async inbound(to, from_, scenario_id) -> ccid` via `POST /sim/telnyx/inbound`.
  - `async recording_wav(url) -> bytes`: if the URL host is not resolvable from the host (compose `minio:9000`), connect to `localhost:<port>` and send the original `Host` header. SigV4 signs the Host header, so do NOT rewrite the URL's host. Inspect the real URL first; when presigning already uses a public endpoint setting, just GET it.
  - `evaluate(assertions: ScenarioAssertions, call_object, events, timeline)`: evaluator for the §3.5 `assertions` block. Implement every key in the schema:
    - `call_status`, `answered_by`, `end_reason`, `min_turns`, `transcript_contains` (substring over joined transcript texts), `variables` (`call.variables` subset);
    - `max_e2e_ms_p95` over timeline `latency_ms` (nearest-rank p95);
    - `webhook_events`: until the phase-06 dispatcher, check the event types appear in the `/events` stream, by category/data; see the gotcha "call_events rows have no type column", so map `call.completed` to what `emit_completed` writes. Decision.
    - Returns a list of failure strings; tests `assert not failures, failures`.
    - Unit-test the evaluator in pure tests at `tests_e2e/test_harness_unit.py`, unmarked. They are not collected by `make test` because tests_e2e is outside testpaths, so put the pure evaluator in `packages/va_sim/src/va_sim/assertions.py`? NO. Keep it in `tests_e2e/assertions.py` and make `make e2e` run those unit tests too (the `-m e2e` filter would deselect them, so mark them `e2e` as well, cheap). Builder's choice; justify.
  - Webhook receiver: NOT in this task. No dispatcher exists until phase 06; record in the Decision that `tests_e2e/receiver.py` arrives with the dispatcher task.

### 3. Scenarios, rules, flows (`tests_e2e/scenarios/*.yaml`, `tests_e2e/scenarios/rules/*.yaml`, `tests_e2e/test_<flow>.py`)
- **outbound_basic** (spec §3.5 example, profile `realistic`):
  - Call: `task` = prompt + marker, `first_sentence`, `record: true`, `voice` = the org voice name, `from` = the org number, `phone_number` = the scenario `to`. Do not pass `webhook`: the receiver comes in phase 06, so leave it out (Decision).
  - Rules make the agent ask for a time, then on the 2nd/3rd user turn call the `end_call` system tool. Check its exact name/args in `va_engine.system_tools`, and use `call_index` with `system_contains` marker.
  - Asserts:
    - the §3.5 block;
    - Call Object fields of specs/19 §4 present (compare key set against `va_domain.call_object.CallObject.model_fields` aliases);
    - transcripts alternate roles;
    - `recording_url` downloads a WAV with 2 channels (stdlib `wave`);
    - one latency event per user turn;
    - sim timeline p95 `latency_ms` ≤ 1200;
    - platform overhead p95 ≤ 250 ms.
  - Overhead per turn = sim `latency_ms` − (`eot_ms` + `stt_final_ms` + `llm_ttft_ms` + `tts_ttfa_ms`) from the platform's matching `call.latency` event. `eot_ms` counts as vendor/policy time because it is the endpointer's deliberate silence wait. Record this as the overhead definition in the Decision.
  - If the measured numbers miss the targets, do NOT loosen them silently. Report the numbers; investigate (profile values via `GET /sim/{sim}/config`).
- **inbound_basic**:
  - `order_inbound_number`, then `create_org(numbers=[{e164, telnyx_phone_number_id, inbound_config: {prompt: <task with marker>, first_sentence: "Thanks for calling Bella Pasta."}}])`.
  - Run a scenario with `match.inbound_from`, then `POST /sim/telnyx/inbound`.
  - Asserts:
    - sim `answered_ms − initiated_ms ≤ 1000`;
    - the first agent TTS text (`GET /sim/elevenlabs/tts/requests`, the first record with non-empty generated_text for that call/voice; or the transcript's first assistant turn) equals first_sentence;
    - Call Object `direction == "inbound"`, runner task (pathway runner arrives in phase 07: the existing specs/30 §5 Decision, extend it);
    - status completed.
  - Find the call id via `GET /v1/calls?...` list for the org (only one call).
- **stop_via_api**:
  - Scenario: answered, the caller says one line, then `silence: {ms: 60000}`.
  - Set `PipelineConfig`-relevant call params so the no-input reprompt does not end the call first. Check the Send Call no-input params; worst case the reprompt is harmless.
  - Once the sim summary shows answered + 10 s, `POST /v1/calls/{id}/stop`; record wall time.
  - Assert sim `ended_ms` within 1000 ms of the stop request (map wall→virtual via `GET /sim/clock` before and after, realtime clock) and Call Object `ended_by` = the stop mapping for api (check `CALL_ENDED_BY` in `va_domain.call_object.mapping`; spec says `api`).
- **max_duration**:
  - `max_duration: 1`; the scenario keeps the caller engaged (`say` every ~5 s for 90 s, or silence plus a no-input config that doesn't hang up). Verify that the default no-input handling would otherwise end the call before 60 s.
  - Assert `answered_ms` → `ended_ms` = 60 s ± 1 s and `end_reason == "max_duration"`. The wrap-up message plays 10 s before the limit; assert its TTS text appears.

### 4. Makefile / docs
- `e2e:` target becomes `scripts/dev_up.sh e2e && uv run pytest tests_e2e -m e2e -p no:randomly?`. Only add flags that exist. `dev_up.sh` is idempotent and rebuilds images, so a stale stack never runs old code.
- `specs/30 §5`: add `> Decision (2026-11-02): e2e harness (P05-T17b)` covering:
  - the `/internal/test/orgs` contract (body, response, 404 outside local/test, stale-number release);
  - no `/sim/reset` and run-unique scenario/rule ids;
  - completion = `queue_status=complete` polling and the events stream instead of webhooks until phase 06;
  - the overhead definition;
  - inbound_basic with the task runner;
  - recording fetch via Host header.
- If any spec table lists internal routes (specs/04?), add the route there.
- AGENTS.md: a `Learned during the build` bullet (harness API, run token, never `/sim/reset` in e2e, `make e2e` rebuilds the stack, running one flow). Update the Commands row "Simulator end-to-end" if the invocation changes.

### Gates
- `make fmt lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `make openapi` + drift.
- `uv run pytest apps/api -q -k "test_orgs or openapi or cross_tenant or audit_markers"` while iterating.
- Full `make test` in the background with a Monitor heartbeat (AGENTS P05-T10d).
- `make e2e` (also background + heartbeat): all four flows green. Include the per-flow durations and the outbound_basic p95/overhead numbers in the evidence.
- jscpd, radon, pip-audit, pnpm audit.
- Afterwards `make up` restores the infra stack, or leave the e2e stack up; say which.

### Risks
- Calling-hours, DNC, fraud-prefix, and international checks on random destinations: use valid US NANP numbers in an area code with a state rule, and open every window plus `state_quiet_hours: false`.
- New-account limits: backdate `created_at`.
- The presigned recording host is unreachable from the host.
- Realistic-profile latency may exceed 1200 p95. Measure and report; do not weaken without a Decision.
- No-input reprompt/hangup interfering with stop_via_api/max_duration.
- FakeLLM `call_index` counts per `X-VA-Call-Id`. Check that the media/LLM gateway sends the call id header in the containers.
- The inbound number inventory is fixed: always release in teardown.
- Docker image rebuild time (several minutes): background + Monitor.
- Pre-existing flake `fix:bed-window-flake` (runtime task) may hit `make test`. It is a separate task; rerun the single test to show it is unrelated.

## Build P05-T17b (2026-11-02)

Delivered:
- `POST /internal/test/orgs` = `apps/api/src/va_api/routers/e2e_seed.py` (+ `e2e_seed_schemas.py`; files are NOT named
  `test_*` because pytest would collect them from src). Deviation from plan: the org gets NO owner user (provision_in
  owner=None, key created_by NULL) - the harness needs only the key; simpler than reusing DevSeeder. Calling hours are
  not enforced for non-batch API calls unless `compliance.enforce_calling_hours`, so the harness passes no settings.
  INVALID_INPUT is 400 here (not 422). Internal tag "Internal" added to INTERNAL_TAG_NAMES; PUBLIC_ROUTES and
  INTERNAL_PATHS updated; openapi/internal.json regenerated. Tests apps/api/tests/test_e2e_seed.py (12, 100 % cov).
- `tests_e2e/`: conftest (stack probe → pytest.exit(3)), support/e2e_harness.py, support/e2e_assertions.py,
  scenarios/{outbound_basic,inbound_basic,stop_via_api,max_duration}.yaml + rules/, test_<flow>.py, unit/ (in
  testpaths). FakeTelnyx scenario API has no DELETE → scenarios stay (run-unique ids/numbers).
- Platform fixes found by the harness (Decisions in specs/07 §20 and specs/30 §5 (8)):
  1. end_call final line spoken on the turn timer (pipeline._end_call) → end_call turns get an E2E latency event.
  2. Speaker marks LLM_FIRST_TOKEN at stream end for text-less (tool-call-only) rounds → tool-call LLM time is vendor
     time, not overhead (overhead turn 2 went 366 → 86 ms).
  3. apps/sim runner starts from a random seed (`startup_seed()`): after a sim restart, seed 0 replayed event ids and
     the edge dropped the inbound call.initiated as a duplicate (inbound_basic failed after rebuilds).
- Measured (warm stack): outbound_basic sim latency [1031, 1029] ms (p95 1031 ≤ 1200), overhead [126, 86] ms
  (p95 126 ≤ 250); first call after a rebuild was 1279 ms once (cold). Durations: max_duration 61.6 s, outbound 21.9 s,
  inbound 13.8 s, stop 11.5 s; suite 109 s.
- Stack left running in PROFILE=e2e.

## Review P05-T17b (2026-11-02): approved
Re-ran: lint/typecheck/placeholders 0, spec refs 0, gitleaks pass, jscpd 0 clones, make test 10750 passed 7 skipped
(cov 99.16 %), make e2e 4 passed in 109 s. Diff checked: seed route gated on VA_ENV, x-public + audit_exempt, no
nested sessions; pipeline/speaker latency changes covered by tests and the specs/07 §20 Decision; sim random seed
covered. Stack left running in PROFILE=e2e.

## Plan P05-T17c — E2E voicemail_leave_message, voicemail_hangup, amd_ivr_mode, cold_transfer

Builds on the P05-T17b harness (`tests_e2e/support/e2e_harness.py`, `e2e_assertions.py`). Four new flows plus one
small simulator addition. Spec rows: specs/30 §5 lines ~1272-1277.

### 0. Facts checked while planning (do not re-derive)
- `va_domain.voicemail.amd_mode`: `ivr_mode=true` → AMD **disabled** (no premium AMD on dial). So in amd_ivr_mode the
  scenario's `answered_by: machine` produces no machine verdict at all; "no voicemail action despite machine AMD"
  is met by AMD never running. Record that in the Decision.
- Default resolved config = premium AMD, so the voicemail flows need no extra AMD parameter. Send Call
  `voicemail: {message, action}` (`VoicemailRequest`, actions hangup|leave_message|...).
- The task-mode transfer tool is `transfer_call` (args `message`), enabled by the call param
  `transfer_phone_number` (`va_engine.system_tools`). The in-process reference is
  `apps/media_worker/tests/test_transfer_flow.py` (expected primary commands `answer, transfer, streaming_stop,
  hangup` inbound; outbound starts with `dial`), events `call.transfer_started {type: cold, target}` then
  `call.transferred {}` (both category `transfer`).
- Outbound DTMF pauses: `va_media.dtmf_out.DTMF_PAUSE_S = 0.5` for `w`. FakeTelnyx `dtmf_sent` entries have
  `digits`, `duration_millis`, and an ISO `at`. Commands/events in the sim summary also have ISO `at` (the e2e sim
  runs on the realtime clock, so ISO deltas are real ms).
- FakeTelnyx Answer `beep_at_ms` → `LegOutcome.beep_s`. Check in `call_engine` whether it counts from the answer
  (expected). The in-process reference for both voicemail flows is `machine_call` / `test_a_machine_with_hangup...`
  in `apps/media_worker/tests/test_runtime_sim.py`. End reasons there: leave_message → `voicemail_left`/`agent`;
  hangup → `voicemail_hangup`/`system`.
- `e2e_assertions.event_present` can identify a type only by a fixed message (`EVENT_MESSAGES`) or a category
  used by exactly one type. `transfer` is shared, so `call.transferred` cannot be recognized by category.
- A caller script stops (`media_stopped`) when the stream stops, so after a transfer the primary leg's steps no
  longer run. A transfer-target leg has no stream, so its `steps` never run either. The harness must hang up the
  bridged primary itself.

### 1. Simulator: first agent speech on the timeline
The spec asks for "agent audio starts after beep (≥ 4000 ms after answer)". Nothing in the summary or timeline
exposes when agent speech started, and the summary's `stream` is None after hangup.
- `packages/va_sim/src/va_sim/telnyx/calls.py`: add `Call.first_agent_speech_ms: int | None = None` and expose it
  in `timeline()` as `"first_agent_speech_ms"`.
- `media_session.py`: on each agent media frame received (where the playout/agent track is fed, near the
  `first_agent_audio_ms` code ~L300-330), if the call's value is None and `agent_speech.is_speech(pcm)` holds, set
  it to `self._state.clock.virtual_ms()` (or the session's existing now helper). The check must stay O(1) per
  frame and do nothing once set. Static-bed frames (RMS ≈ 330 < 1024) are not speech.
- Test, in `packages/va_sim/tests/telnyx/` next to the existing timeline/media-session tests: a media_agent that
  sends silence frames, then loud frames → timeline value = the virtual ms of the first loud frame (assert ordering
  relative to `answered_ms`, not exact socket ms, per the AGENTS accelerated-clock note); with no speech the value
  stays None.
- Add the field to the specs/30 §3.2 timeline description if one is documented there (grep `first_agent_audio_ms`
  in specs/30), as part of the §5 Decision.

### 2. Harness additions (`tests_e2e/support/e2e_harness.py`)
- `async sim_hangup(ccid)`: `POST {telnyx}/v2/calls/{ccid}/actions/hangup` with `_telnyx()` headers and
  `{"command_id": f"e2e-hangup-{secrets.token_hex(4)}"}`; `_ok(...)`.
- `def iso_ms(at: str) -> int` (module helper; parse ISO Z with `datetime.fromisoformat`), used for
  command/event/dtmf deltas. Unit-test it in `tests_e2e/unit/`.
- `async events_of(...)` is not needed; tests use `finished.events` and `sim_summary(ccid)` (read before the
  harness ends; the summary is kept after hangup, only `stream` goes away).
- `e2e_assertions.EVENT_MESSAGES`: add `call.voicemail` / `call.transferred` **only if** their producers write a
  fixed message constant (grep `va_media.voicemail`, the edge carrier handler for `call.transferred`). Import the
  constant; never copy the string. Otherwise the tests check `category`/`data` directly and the scenario
  `webhook_events` lists only `call.completed`. Extend the evaluator unit tests for any new entry.

### 3. Scenarios, rules, tests (`tests_e2e/scenarios/*.yaml`, `rules/*.yaml`, `tests_e2e/test_<flow>.py`)
All four use `pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]` and `harness.create_org()`, and send
outbound task calls through `send_task_call`. Pass `background_track: "none"` in the voicemail flows so no bed can
reach agent-speech level (belt and braces).

**voicemail_leave_message**
- Scenario: `answer: {after_ms: 1000, answered_by: machine, beep_at_ms: 4000}`; steps
  `say: {text: "Hi, you've reached Dana. Please leave a message after the tone."}` (quoted), then
  `expect_hangup: {by: agent, timeout_ms: 30000}`. Assertions: `call_status: completed`, `answered_by: machine`,
  `end_reason: voicemail_left`, `webhook_events: [call.completed]`. No LLM rules.
- Call: `voicemail: {message: "Hi, call us back", action: leave_message}`.
- Asserts:
  - failures empty;
  - timeline `first_agent_speech_ms - answered_ms >= 4000`;
  - `tts_texts() == ["Hi, call us back"]`, so no opener and no dialogue;
  - the hangup came from the platform: the sim summary `hangup_source` is the platform side (`caller` for an
    outgoing leg);
  - an event row with category `voicemail` (check the actual category in `DEFAULT_CATEGORY[CALL_VOICEMAIL]`)
    whose `data.action == "leave_message"`;
  - Call Object `call_ended_by == "ASSISTANT"`;
  - `llm_requests(call_id) == []`.

**voicemail_hangup**
- Scenario: machine answer (no beep needed, or `beep_at_ms` 4000; it doesn't matter); steps `say` greeting, then
  `expect_hangup: {by: agent, timeout_ms: 15000}`. Assertions: completed, machine, `end_reason: voicemail_hangup`.
- Call: `voicemail: {action: hangup}`.
- Asserts:
  - failures empty;
  - in the sim summary, the AMD result event (`call.machine.premium.detection.ended`, or whatever FakeTelnyx emits
    first with the machine result; take it from `events`) → the first `hangup` command `at` delta ≤ 2000 ms;
  - `tts_texts() == []`;
  - `llm_requests(call_id) == []`;
  - Call Object `call_ended_by == "SYSTEM"`.

**amd_ivr_mode**
- Scenario: `answer: {after_ms: 1000, answered_by: machine}`; steps `say: {text: "Thank you for calling. Press 1
  for sales."}`, `silence: {ms: 3000}`, `hangup: {}`. Assertions: `call_status: completed`,
  `end_reason: user_hangup` (verify the stored reason for a far-end hangup during IVR; adjust to the real value
  only if it is the documented specs/05 §9 one).
- Call: `ivr_mode: true`, `precall_dtmf_sequence: "1w2"`, `voicemail: {action: hangup}` (proves the action is
  ignored).
- Asserts:
  - failures empty;
  - sim summary `dtmf_sent` digits `["1", "2"]` and `iso_ms(at[1]) - iso_ms(at[0]) >= 500`;
  - the first DTMF follows the answer;
  - no `amd`/`answering_machine_detection` on the dial command. The summary `commands` have no body, so instead
    assert no `call.machine.*` event in the sim summary `events`;
  - no event row of the voicemail category;
  - Call Object `answered_by != "voicemail"` and `end_reason` not starting with `voicemail`.
- Optional LLM rule (marker-scoped) giving a short reply so any IVR reply is deterministic. Only add it if the
  default FakeLLM reply makes the test flaky.

**cold_transfer**
- Scenario: `answer: {after_ms: 1000, answered_by: human_residence}`; steps `wait_agent_speech: {min_ms: 300,
  then_silence_ms: 700, timeout_ms: 8000}`, `say: {text: "Can I talk to a human please?"}`, `expect_hangup:
  {timeout_ms: 60000}`; `transfer_target: {answer_after_ms: 500}`. Assertions: `call_status: completed`,
  `end_reason: transferred`, `webhook_events: [call.completed]`.
- Rules `rules/cold_transfer.yaml`: marker + `last_user_regex: "(?i)human"` → `tool_calls: [{name: transfer_call,
  arguments: {message: "Connecting you now."}}]`.
- Call: `first_sentence`, `transfer_phone_number: target` (`random_e164(CALLER_ID_NPA)` or another valid NANP
  number; it must pass Send Call validation and the DNC/fraud checks).
- Flow:
  1. `sim_call(scenario.number)`;
  2. `wait_sim(ccid, lambda s: s["transfers"] and any(c["name"] == "streaming_stop" for c in s["commands"]))`;
  3. `wait_until_sim_ms(sim_clock_ms() + 3000)` so bridged time accrues;
  4. `sim_hangup(ccid)`;
  5. `finished(org, call_id, ccid=ccid)`.
- Asserts:
  - failures empty;
  - `transfers[0]["to"] == target`;
  - non-duplicate command names after `dial`/answer form the subsequence `transfer` → `streaming_stop` → `hangup`,
    with nothing named `hangup` before `transfer`;
  - "AI stream stopped after the final line": `"Connecting you now." in tts_texts()`, and the transfer command
    `at` ≥ the end of the caller's `say` step (`script.steps[1].ended_ms` is virtual ms; compare against
    `iso_ms` only if both are on the same clock, else drop the check). Mark-echo ordering stays covered
    in-process by `test_transfer.py`/`test_transfer_flow.py` (say so in the Decision);
  - event rows with category `transfer`: data `{type: cold, target}` then `{}`;
  - Call Object `transferred_to == target`, `transfer_duration > 0`, `call_ended_by == "SYSTEM"`;
  - "billed at the transfer rate": price ≥ `transfer_duration` × the org plan's transfer per-minute rate. Read
    the rate from the effective plan pricing (`va_billing.plans` seed values for the plan the seed route uses).
    If price rounding makes this fragile, assert instead that the call's usage includes a transfer component,
    via whatever read path exists (no new public API in this task).

### 4. Specs/docs
- specs/30 §5: `> Decision (2026-11-02): voicemail, IVR, and transfer flows (P05-T17c)` covering:
  - cold_transfer runs through the task-mode `transfer_call` tool (pathway Transfer node in phase 07) on an
    outbound call; the harness hangs up the bridged caller; mark-echo ordering is covered in-process;
  - amd_ivr_mode: ivr_mode disables AMD, so the "machine" answer yields no verdict and no voicemail action;
  - the timeline's `first_agent_speech_ms` (FakeTelnyx agent-speech threshold) is the "agent audio after beep"
    evidence;
  - the hangup-within-2-s measurement (sim AMD event `at` → hangup command `at`).
- AGENTS.md: one `Learned during the build` bullet (P05-T17c): `sim_hangup`, `iso_ms`, `first_agent_speech_ms`,
  ivr_mode disables AMD, transfer-target legs run no steps.

### Gates
- `make fmt lint typecheck placeholders`; `uv run python scripts/check_spec_refs.py`.
- Iterate with `uv run pytest packages/va_sim -q -k "timeline or media_session"` and `uv run pytest tests_e2e/unit -q`.
- The sim image changes, so `make e2e` rebuilds it (background + Monitor heartbeat). It must show all 8 flows
  green; report per-flow durations.
- Full `make test` in the background with a heartbeat; jscpd, radon, pip-audit/pnpm audit as usual.

### Risks
- The machine greeting `say` overlapping the beep: the beep only gates when the agent starts.
- Premium AMD verdict timing in FakeTelnyx under realtime: the 2 s window is measured from the AMD event, not from
  the answer.
- `expect_hangup` in cold_transfer ends as `media_stopped`/interrupted. That is fine: the evaluator ignores step
  statuses.
- If the edge's outbound transfer path differs from the inbound flow (e.g. `answer` absent), adjust the expected
  subsequence. Never weaken the transfer/streaming_stop order.
- Pre-existing flake `fix:bed-window-flake` may hit `make test`; rerun the single test to show it is unrelated.

## Build P05-T17c (2026-11-02)

Delivered:
- Sim: `Call.first_agent_speech_ms` (set in `MediaSession._tick` from the played-out agent frame's speech test, O(1),
  first only) exposed in `timeline()`; test `test_timeline_reports_when_agent_speech_first_played`; call-control
  timeline equality updated. specs/30 §3.2 (14) timeline field list updated.
- Harness: `remote_hangup(ccid)` (sim `POST /sim/telnyx/calls/{ccid}/hangup`, far end; used instead of a Call
  Control hangup so the bridged caller hangs up as the remote side), `finished_leg(org, call_id, number)` →
  (FinishedCall, summary), module `iso_ms(at)` (unit-tested). `EVENT_MESSAGES` += call.voicemail (VOICEMAIL_MESSAGE),
  call.transfer_started (TRANSFER_STARTED_MESSAGE), imported from va_media; unit tests extended.
- Flows: voicemail_leave_message, voicemail_hangup, amd_ivr_mode, cold_transfer (+ rules/cold_transfer.yaml).
  Found: `call.amd` shares the `voicemail` category -> select voicemail rows by message.
- Decision `voicemail, IVR, and transfer flows (P05-T17c)` under specs/30 §5; AGENTS bullet.
- First targeted run (rebuilt stack): 4 passed in 31.8 s (cold_transfer 13.7 s, amd_ivr_mode 7.7 s,
  voicemail_leave_message 6.8 s, voicemail_hangup 3.3 s).
- Gates: make test 10757 passed / 7 skipped, cov 99.16 %; lint/typecheck/placeholders/spec refs clean; jscpd 0
  clones (after finished_leg refactor); radon avg A 2.86; pip-audit no known vulns; pnpm audit 3 moderate (no high).
- make e2e (full, rebuilt): 8 passed in 141.3 s, exit 0. Stack left running in PROFILE=e2e.

## Review P05-T17c (2026-11-02): approved
Re-ran: lint/typecheck/placeholders/spec refs 0, jscpd 0 clones, make test 10757 passed 7 skipped (99.16 %),
make e2e 8 passed in 140.6 s. Diff matches specs/30 §5 rows (answered_by voicemail via scenario mapping, AMD→hangup
≤ 2 s, DTMF gap ≥ 500 ms, transfer ordering + transfer-rate price bound against seed build plan $0.10/$0.04/$0.015).

## Plan P05-T17d — E2E barge_in, interruptibility_levels (0–4), worker_crash_recovery

Builds on the P05-T17b/c harness. Spec rows: specs/30 §5 (`barge_in`, `interruptibility_levels`,
`worker_crash_recovery`); level rules specs/07 §7 (+ Decision P04-T05); liveness specs/01 §2.1 Decision P05-T06c,
specs/07 §19. After this task `make e2e` must show every phase-exit flow (11 flows, 15 tests with the 5 levels).

### 0. Facts checked while planning (do not re-derive)
- FakeTelnyx counts `clear` frames (`media_session._handle` → `received["clear"]`) but records no time. Timeline
  utterance entries have `start_ms`/`end_ms` (sim virtual ms = wall ms under the realtime e2e sim).
- FakeElevenLabs TTS = `va_sim.elevenlabs.tts_audio.CHAR_MS` = 60 ms/char → a 300-char first_sentence ≈ 18 s.
  `first_sentence` max_length 1000. Opener is `TaskBrain._say(...)` → `SpeakStatic(interruptible=True)` (barge-able).
- Send Call has `interruptibility` (0–4, default 2), `block_interruptions`, `wait_for_greeting` (default False).
- History after barge-in: `va_llm.prompts.interrupted_message(text_spoken)` = assistant message
  `text_spoken.rstrip() + INTERRUPTION_MARK` (import the constant; never copy "—").
- Call Object transcripts carry the FULL planned text + `interrupted`; `text_spoken` is not in the public API, so
  the spoken text is checked through the next LLM request (sim LLM log) and `call.interrupt` data
  (`played_chars`, `total_chars`, `played_ms`, `reaction_ms`; `reaction_ms` = t_clear − t_speech_start).
- Silero: `va_voice.vad.START_WINDOWS = 3`, 32 ms windows (`WINDOW_MS`) → speech_start confirmation lag ≤ ~96 ms.
- Worker crash path: docker kill media → FakeTelnyx WS closes → `streaming.failed` → edge signals workflow →
  after `STREAM_FAILURE_GRACE` (5 s) `call_presence` still sees `call:{id}:owner` (`OWNER_TTL_MS` 15 s, renewed every
  5 s) → not orphaned → heartbeat watch: `call:{id}:hb` (`HEARTBEAT_TTL_S` 10, set every 3 s) absent for
  `HEARTBEAT_MISSING_AFTER_S` 20 (poll `HEARTBEAT_POLL_INTERVAL_S`) → `_HANGUP_ENDS["heartbeat_lost"]` → end_call
  `worker_crash` then `hangup_call`; post-call child runs. Worst case kill→hangup = 10 + 20 + poll(3) + activities
  ≈ 33–35 s, so the spec's "within 30 s" cannot hold in the worst case (see Decision below).
- Compose `media` has `restart: unless-stopped`; `docker kill` counts as a manual stop (verify; if Docker restarts
  it on its own, the harness restart step must tolerate an already running container).
- TurnWriter flushes every 2 s (`TURN_FLUSH_INTERVAL_S`), event buffer every 1 s: turns written before the kill
  survive → "partial transcript".

### 1. Simulator: clear times on the timeline
- `packages/va_sim/src/va_sim/telnyx/calls.py`: `Call.clear_ms: list[int] = field(default_factory=list)`, exposed
  in `timeline()` as `"clears"` (list copy).
- `media_session.py` `_handle` `AgentClear` branch: append `self._state.clock.virtual_ms()` (same clock helper the
  session already uses for `first_agent_speech_ms`).
- Test next to `test_timeline_reports_when_agent_speech_first_played` (packages/va_sim/tests/telnyx/): media_agent
  sends media, then `clear` twice → timeline `clears` has 2 non-decreasing values ≥ answered_ms; no clear → `[]`.
  Update the call-control timeline equality test (it compares the full dict).
- specs/30 §3.2 (14) timeline field list: add `clears` (virtual ms of every agent `clear` received).

### 2. Harness (`tests_e2e/support/`)
- New pure module `tests_e2e/support/e2e_timing.py` (unit-tested in `tests_e2e/unit/`):
  - `@dataclass(frozen=True) Window(start_ms: int, end_ms: int)`; `utterance_window(entry, tail_ms) -> Window`
    (`end_ms` None → raise AssertionError with the entry).
  - `clears_in(timeline, window) -> list[int]`; `first_clear_after(timeline, at_ms) -> int | None`.
  - `interrupt_events(events) -> list[dict]` = rows whose `category == "interrupt"` (check
    `DEFAULT_CATEGORY` for `call.interrupt` — "interrupt" — import from va_events if a mapping exists; else filter
    by the message prefix used in `va_media.turns.TurnRecorder.on_interrupt` via a shared constant: if the message
    is an f-string, filter by category only, `interrupt` is used by exactly one type — verify).
  - `assistant_before(messages, user_text) -> str | None`: content of the assistant message right before the last
    user message containing `user_text` (LLM request `messages` list).
- `Harness` (e2e_harness.py):
  - `async kill_media() -> int`: find the container with `docker ps -q --filter
    label=com.docker.compose.project=voiceagents --filter label=com.docker.compose.service=media` (asyncio
    subprocess, 30 s timeout, non-zero → AssertionError with stderr), `docker kill --signal KILL <id>`; returns
    `await self.sim_clock_ms()` read right after the kill returns.
  - `async restart_media(timeout_s=120)`: `docker start <id>` (same lookup with `ps -aq`), then poll
    `{media}/readyz` (StackUrls has/gets `media = http://localhost:8200`) until 200 via `_poll`. Idempotent (a running
    container is fine).
  - `async transcripts(org, call_id) -> list[dict]` only if `call_object(...)["transcripts"]` isn't enough (prefer
    the Call Object).
- Constants come from production modules (va_sim CHAR_MS, va_voice.vad START_WINDOWS/WINDOW_MS, va_voice.interrupt
  `LEVEL_RULES`, va_media.session HEARTBEAT_TTL_S, va_workflows liveness_inputs HEARTBEAT_MISSING_AFTER_S /
  HEARTBEAT_POLL_INTERVAL_S, va_llm.prompts INTERRUPTION_MARK); never hard-code duplicates.

### 3. Flows (`tests_e2e/scenarios/*.yaml`, `rules/*.yaml`, `tests_e2e/test_<flow>.py`)
All: `pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]`, `harness.create_org()`, `send_task_call`, follow
`test_cold_transfer.py`/`test_outbound_basic.py` style. `record` stays false (no 10 min recording wait).

**barge_in** — `interruptibility: 4` (Decision: the decision point is then STT-independent; levels 1–3 timing is
proven by interruptibility_levels).
- `first_sentence` = a fixed 300-char string constant (assert `len == 300` in the test module).
- Scenario: answer human after 1000 ms; steps `say: {text: "Wait, I have a quick question about my order.",
  barge_in_after_agent_ms: 1200}`, `wait_agent_speech: {min_ms: 300, then_silence_ms: 700, timeout_ms: 15000}`,
  `hangup: {}`. Assertions: completed, answered_by human, `webhook_events: [call.completed]`.
- Asserts:
  - failures empty;
  - utterance 0 onset = `start_ms`; `clear = first_clear_after(timeline, start_ms)` not None and
    `clear − start_ms ≤ LEVEL4_MIN_SPEECH (150) + START_WINDOWS×WINDOW_MS + 120 + 40` (Decision: onset
    confirmation + one 20 ms frame each way; calibrate only within this formula);
  - exactly one `call.interrupt` row; its `reaction_ms ≤ 150 + WINDOW_MS + 120`; `played_ms ≥ 1200`;
    `abs(played_chars − played_ms / CHAR_MS) ≤ 0.1 × played_ms / CHAR_MS` (Decision: the spec's "1.2 s / 60 ms"
    is evaluated at the decision's `played_ms`, which is ≥ 1.2 s by the level rule);
  - Call Object: the first assistant transcript has `interrupted is True`;
  - LLM: the first request of the call whose messages contain the caller's text has, right before that user
    message, an assistant message `m` ending with `INTERRUPTION_MARK`; `spoken = m[:-len(mark)]`;
    `FIRST_SENTENCE.startswith(spoken)`, `len(spoken) <= played_chars`, `len(spoken) < len(FIRST_SENTENCE)`, and no
    message contains `FIRST_SENTENCE[len(spoken) + 40:]` (the unspoken remainder).

**interruptibility_levels** — `@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])`, one call each, `interruptibility=
level`, same 300-char first_sentence (share the constant via a small `tests_e2e/support/e2e_texts.py` or the test
module — jscpd!).
- Scenario `interruptibility_levels.yaml` (loaded once per param: `load_scenario` gives a fresh number per call):
  answer human 1000 ms; steps
  1. `say: {text: "uh-huh", barge_in_after_agent_ms: 1500}` (measure its timeline duration; spec says ≈ 0.4 s — if
     the synth makes it ≥ 600 ms VAD speech, pick "mm-hmm"/"uh huh" whichever is < 500 ms and record it),
  2. `silence: {ms: 2500}`,
  3. `say: {text: "Sorry, could you tell me your opening hours on the weekend?", barge_in_after_agent_ms: 800}`
     (≥ 4 non-backchannel words, > 1.2 s continuous speech; at level 4 it waits for the reply turn),
  4. `wait_agent_speech: {min_ms: 300, then_silence_ms: 700, timeout_ms: 30000}`,
  5. `hangup: {}`.
- Rules `rules/interruptibility_levels.yaml`: marker-scoped default reply = a long (≈ 200 chars) fixed sentence so
  the level-4 reply is still playing when step 3 starts.
- Windows: `uh = utterance_window(utterances[0], tail_ms=500)`, `sentence = utterance_window(utterances[1], 500)`.
- Asserts per level (failures empty for all):
  - 0: `clears_in(uh) == clears_in(sentence) == []`, zero interrupt rows, no transcript `interrupted`;
  - 1: none in uh; exactly one in sentence and `clear − sentence.start ≥ 1200`; one interrupt row;
  - 2: none in uh; one in sentence, `≥ 500`; one interrupt row;
  - 3: none in uh; one in sentence, `≥ 250`; one interrupt row;
  - 4: one in uh with `clear − uh.start ≥ 150`; one in sentence; two interrupt rows.
  Take thresholds from `LEVEL_RULES` (never literals).

**worker_crash_recovery**
- Scenario: answer human 1000 ms; steps `wait_agent_speech {min_ms 300, then_silence_ms 700, timeout 8000}`,
  `say: {text: "I'd like to book a table for two tonight."}`, `wait_agent_speech {min_ms 300, then_silence_ms 700,
  timeout 15000}`, `silence: {ms: 120000}`. Assertions: completed, answered_by human, `end_reason: worker_crash`,
  `webhook_events: [call.completed]`.
- Flow: send call; `sim_call`; `wait_sim` until script step 2 status `ok`; poll the Call Object (or
  `/v1/calls/{id}/transcript`) until ≥ 2 transcripts exist (deterministic flush evidence, no sleeps);
  `kill_ms = await harness.kill_media()`; inside `try/finally: await harness.restart_media()`:
  wait for the sim leg to end (`wait_sim(ccid, ended, timeout_s=60)`), then `finished(...)`.
- Asserts:
  - failures empty (end_reason worker_crash proves the stream_failed path did not win);
  - `timeline.ended_ms − kill_ms ≤ HEARTBEAT_TTL_S×1000 + HEARTBEAT_MISSING_AFTER_S×1000 +
    HEARTBEAT_POLL_INTERVAL_S×1000 + 2000` and `≥ HEARTBEAT_MISSING_AFTER_S×1000` (it was the watch, not a hangup);
  - sim summary: a non-duplicate `hangup` command and `hangup_source == "caller"` (platform side, outgoing leg);
  - Call Object `queue_status == "complete"` (post-call ran), `end_reason == "worker_crash"`, transcripts ≥ 2
    (partial: opener + caller turn), `call_ended_by` per `va_domain.call_object.mapping.CALL_ENDED_BY` for
    ended_by `worker_crash` (import the mapping, no literal);
  - after `restart_media()`, media `/readyz` 200 (the fixture leaves the stack usable).

### 4. Specs/docs
- specs/30 §5 `> Decision (2026-11-02): barge-in, interruptibility, and worker crash flows (P05-T17d)`:
  (1) timeline `clears`; (2) barge_in runs at level 4, onset→clear bound formula (level threshold + VAD
  confirmation + 120 ms + frame transport), spoken length checked at the decision's played_ms, text_spoken via the
  next LLM request; (3) interruptibility_levels script (uh-huh, silence, sentence with barge_in_after_agent_ms) and
  the per-level window rules; (4) worker_crash_recovery bound = hb TTL + missing window + poll + 2 s (≈ 35 s; the
  "30 s" row is the typical case — measured value reported), harness kills/restarts via docker labels.
- AGENTS.md bullet (P05-T17d): timeline `clears`, `kill_media`/`restart_media`, e2e_timing helpers, the crash
  timing chain (stream_failed grace loses to the owner TTL → heartbeat watch).

### Gates
- `make fmt lint typecheck placeholders`; `uv run python scripts/check_spec_refs.py`;
  `uv run pytest packages/va_sim -q -k "timeline or clear"`; `uv run pytest tests_e2e/unit -q`.
- `make e2e` (sim image changes → rebuild; background + Monitor heartbeat): all 15 tests green; report per-flow
  durations, barge-in onset→clear ms, reaction_ms, crash kill→hangup ms.
- Full `make test` in the background with heartbeat; jscpd 0 clones, radon, pip-audit/pnpm audit.

### Risks
- uh-huh VAD segment ≥ 600 ms (level 3 long-speech rule) or ≥ 900 ms → false interrupt at 2/3: measure the timeline
  `end_ms − start_ms`; pick a shorter backchannel if needed (Decision).
- Synthetic sentence may contain inter-word gaps that end the VAD segment before 1.2 s (level 1): if so, use a
  longer/denser sentence; never relax the ≥ 1200 ms lower bound.
- Level 0: the caller audio during playback becomes the next user turn; the call ends by the scenario `hangup`.
- `docker kill` + restart policy: verify the container stays down until `restart_media`; always restart in
  `finally` so later flows (and the next `make e2e`) keep a healthy media worker.
- Pre-existing `fix:bed-window-flake` may hit `make test`; rerun that single test to show it is unrelated.

## Build P05-T17d (2026-11-02)

Delivered:
- Sim: `Call.clear_ms` → timeline `clears` (virtual ms of each agent `clear`); test
  `test_timeline_reports_every_agent_clear`; call-control timeline equality updated; specs/30 §3.2 (14) field list.
- Harness: `tests_e2e/support/e2e_timing.py` (Window, utterance_window, clears_in, first_clear_after,
  interrupt_events, assistant_before, whole_word_prefix; unit tests `tests_e2e/unit/test_e2e_timing.py`),
  `e2e_texts.py` (300-char opener), `Harness.kill_media`/`restart_media` (docker CLI, compose labels), `wait_call`
  (wait_complete uses it), module `docker`/`compose_container`.
- Flows: barge_in (level 4), interruptibility_levels (0-4), worker_crash_recovery + scenarios/rules.
- Deviations from plan, both recorded in the Decision: (a) `played_chars` = whole-word `text_spoken` length, so
  the ±10 % check is [whole_word_prefix(0.9·played_ms/60), 1.1·played_ms/60] (a plain ±10 % failed once: 28 vs 31.2);
  (b) onset→clear bound adds the synthetic voice's first 180 ms syllable (Silero onset measured bimodal ≈ +0 /
  +200 ms; pipeline reaction_ms stays 156-174 ms, the tight check).
- Measurements: barge_in onset→clear 183-381 ms, reaction_ms 156-174; levels: L1 clear 1243 ms after onset,
  L2 544, L3 422, L4 uh-huh 164 / sentence ≈ 180; uh-huh VAD never interrupted at 1-3. Crash: kill→hangup 28.8 s
  (bound 20-35 s), Docker did not auto-restart the killed container (RestartCount 0).
- Gates: make e2e 15 passed in 296.6 s exit 0 (all 11 phase-exit flows); make test 10770 passed / 7 skipped,
  cov 99.16 %; lint/typecheck/placeholders/spec refs clean; jscpd 0 clones; radon tests_e2e avg A 3.41;
  pip-audit no known vulns; pnpm audit 3 moderate (no high). Stack left running in PROFILE=e2e.

## Review P05-T17d (2026-11-02): approved
Re-ran: lint/typecheck/placeholders/spec refs 0; jscpd 0 clones; make test 10770 passed 7 skipped (99 %);
make e2e 15 passed in 300.3 s. Diff matches specs/30 §5 rows + Decision P05-T17d (thresholds imported from
LEVEL_RULES / heartbeat constants / CALL_ENDED_BY, no literals; media restarted in finally).

## Plan P05-T17e — 50-concurrent-call media worker check

Last task of phase 05 (P05-T17 is ticked together with it). Spec refs: specs/30 §6 (pass criteria scaled to one
pod), specs/00 §5 (≥ 50 simulated calls per pod with no underrun; overhead p50 ≤ 150 ms), specs/07 §21.

### 0. Facts checked while planning (do not re-derive)
- `va_audio_underruns_total` is in the media catalog (`va_common/metrics.py:154`) but NOTHING increments it.
  `va_event_loop_lag_ms` histogram is live (`va_media.loop_lag.LoopLagMonitor`). Media serves `/metrics` on 8200
  (host-reachable); `va_media_active_calls{transport="telnyx"}` gauge counts attached calls.
- `ServiceMetrics.counter(name)` / `.histogram(name)` (`va_common/metrics.py:228`).
- `OutboundAudio` (`va_voice/outbound.py`) has `OutboundStats` (frames, late_frames > 30 ms, max_lateness_ms) that
  nobody reads; built only in `va_voice/pipeline/speaker.py:132`. `FramePacer.drained(now)` = Telnyx's playback
  buffer is empty (we are > LEAD_FRAMES×20 = 60 ms behind the schedule).
- Compose e2e stack: ONE media container = one uvicorn process (that is "one media worker process"). Sim container
  runs all four sims in one process (`SIM_WORKERS` must be 1).
- Plan limits: `build` = concurrency 50, calls_send 10/s (burst 20); `scale` = concurrency 100, calls_send 25/s
  (burst 50) (`va_api.rate_limit.PLAN_RATE_LIMITS`). The seed endpoint `POST /internal/test/orgs` already accepts
  `plan_id` (`va_api.routers.e2e_seed`, body.plan_id); `Harness.create_org` does not pass it yet.
- `MEDIA_MAX_CALLS_PER_POD` default 60 → readiness `capacity` fails from 54 attached calls; 50 is fine.
- Overhead per turn: `e2e_assertions.turn_overheads(timeline, events)` + `nearest_rank` (P05-T17b Decision).
- `make load` / `scripts/load/` (1 000-call swarm, SIM_WORKERS) is phase 20 (P20 plan line 50) — not this task.

### 1. Underrun accounting (production code, va_voice + media)
Definition (record in the Decision): a **platform underrun** = a speech frame sent after Telnyx's buffer drained
(`pacer.drained(now)` before `on_sent`) while the previous frame sent on the leg was a speech frame of the SAME
utterance (no idle/background frame, no gap wait, no clear in between). That is: our send loop fell more than the
60 ms lead behind in the middle of an utterance, so the caller heard a hole that was not caused by TTS audio being
late. Vendor starvation (queue empty mid-utterance) is excluded: with a bed it sends idle frames, without one it
waits in the gap branch — both reset the continuity.
- `packages/va_voice/src/va_voice/outbound.py`:
  - `OutboundStats.underruns: int = 0`.
  - `OutboundAudio.__init__(..., on_underrun: Callable[[], None] | None = None)`; slot `_continuing:
    PlaybackTracker | None` (+ `"_on_underrun"` in `__slots__`).
  - `_send_frame`: compute `starved = isinstance(speech, _Frame) and self._continuing is speech.tracker and
    self._pacer.drained(now)` BEFORE `on_sent`; after a successful send (generation unchanged) set
    `self._continuing = speech.tracker` for speech frames, `None` for idle frames; if `starved`:
    `stats.underruns += 1` and call `on_underrun()`.
  - set `_continuing = None` in `clear()` and in the `run()` gap branch (next to `mark_gap()`), and when a `_Finish`
    is processed in `_send_controls`.
  - module docstring sentence on underruns.
- `packages/va_voice/src/va_voice/pipeline/speaker.py`: `Speaker(..., on_underrun=None)` → `OutboundAudio(...,
  on_underrun=on_underrun)`.
- `packages/va_voice/src/va_voice/pipeline/pipeline.py`: `CallPipeline(..., on_underrun: Callable[[], None] | None =
  None)` passed to the Speaker; extend the existing `pipeline_ended` log with `outbound_frames`, `late_frames`,
  `max_lateness_ms` (round 1), `underruns` from `self._speaker.outbound.stats` (diagnostics for the load check).
- `apps/media_worker/src/va_media/runtime.py` `_converse`: `CallPipeline(..., on_underrun=self._metrics.counter(
  "va_audio_underruns_total").inc)` (look the counter up once in `CallRuntime.__init__`, attribute `_underruns`).
- specs/27 §3 media table row `va_audio_underruns_total`: no change of name/labels; add the definition sentence to
  the specs/07 §9 outbound text as `> Decision (2026-11-02): outbound underruns (P05-T17e)`.

Unit tests (`packages/va_voice/tests/test_outbound.py`, ManualClock style already used there):
- `test_on_time_speech_counts_no_underrun`: 20 frames pushed, run 0.5 s → `underruns == 0`, callback not called.
- `test_stalled_loop_mid_utterance_counts_one_underrun`: push 20 frames, send a few, then block the sink (a gate
  in a fake sink's `send_audio`) for 200 ms of clock time, release → `underruns == 1`, callback called once.
- `test_tts_starvation_is_not_an_underrun` (no background): push 5 frames, let them drain, push 5 more after 300 ms
  → 0. Same with a background mixer (idle frames fill the gap) → 0.
- `test_clear_resets_continuity`: frames, clear, wait > 60 ms, new utterance → 0.
- Speaker/pipeline: a `pipeline_fakes` harness test passing `on_underrun` (counter list) and asserting it reaches
  `OutboundAudio` (e.g. `h.pipeline` speaker outbound `_on_underrun`-free check via behaviour: run a normal turn,
  0 underruns; and the `pipeline_ended` log carries `underruns`/`outbound_frames` — `json_log_stream`).
- Media: `apps/media_worker/tests` runtime unit/sim test asserting the runtime wires the catalog counter (call
  `CallRuntime` with a registry, trigger `on_underrun` via the pipeline factory seam if one exists; otherwise assert
  in `test_runtime_sim` that `va_audio_underruns_total` stays 0 after a normal call and is registered — minimal).

### 2. Harness additions (`tests_e2e/support/`)
- New pure module `tests_e2e/support/e2e_metrics.py` (unit tests `tests_e2e/unit/test_e2e_metrics.py`):
  - `parse_metrics(text) -> dict[tuple[str, frozenset[tuple[str, str]]], float]` via
    `prometheus_client.parser.text_string_to_metric_families` (sample name incl. `_total`/`_bucket` suffixes).
  - `sample(metrics, name, **labels) -> float` (0.0 when absent).
  - `histogram_quantile(before, after, name, q) -> float`: delta of cumulative `_bucket` counts between two scrapes,
    nearest-rank upper bound of the bucket holding the q-quantile (`math.inf` → the largest finite bound + report);
    `ValueError` when the delta count is 0.
- `Harness`:
  - `create_org(..., plan_id: str | None = None)` → body `plan_id` when given.
  - `async media_metrics() -> dict[...]`: `GET {urls.media}/metrics` (StackUrls already has the media URL from
    `restart_media`; reuse it).
  - `async watch_active_calls(stop: asyncio.Event, interval_s=0.25) -> int`: polls `va_media_active_calls` (sum
    over transports) until `stop` is set; returns the peak.

### 3. The check: `tests_e2e/test_concurrent_calls.py` + scenario/rules
- `pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]`; constants `CONCURRENT_CALLS: Final = 50`,
  `OVERHEAD_P50_MS: Final = 150` (specs/00 §5), `OVERHEAD_P95_MS: Final = 250` (reported, not asserted — acceptance
  gates p50 only; print PASS/WARN), `LATE_THRESHOLD_MS` imported from `va_voice.outbound` for the report text.
- Scenario `tests_e2e/scenarios/concurrent_call.yaml`: answer human after 1000 ms; steps
  `wait_agent_speech {min_ms 300, then_silence_ms 700, timeout_ms 15000}`, `say "Hi, yes this is Ana."`,
  `wait_agent_speech {then_silence_ms 700, timeout_ms 20000}`, `say "Could we move it to Friday?"`,
  `wait_agent_speech {then_silence_ms 700, timeout_ms 20000}`, `say "Friday at noon works."`,
  `expect_hangup {by: agent, timeout_ms: 20000}`; `profile: realistic`; assertions: completed, answered_by human,
  end_reason agent_end_call, min_turns 6, `webhook_events: [call.completed]`.
  Rules `rules/concurrent_call.yaml` (marker-scoped like outbound_basic): "ana" → a 2-sentence reply asking about the
  appointment; "friday\?" → reply offering times; "noon" → `end_call` with a final message.
- Flow:
  1. `org = await harness.create_org(plan_id="scale")` (concurrency 100, burst 50; Decision); one shared caller ID.
  2. `scenarios = [await harness.load_scenario("concurrent_call") for _ in range(50)]` (sequential; each a fresh
     number + rule set; the profile POST is idempotent).
  3. `before = await harness.media_metrics()`; start `watch_active_calls` task.
  4. `call_ids = await asyncio.gather(*(harness.send_task_call(org, s, TASK, first_sentence=FIRST_SENTENCE,
     wait_for_greeting=False) for s in scenarios))` — all 50 dispatched at once (a 429 is a failure: the plan
     choice must make it impossible).
  5. `finished = await asyncio.gather(*(harness.finished(org, c, number=s.number) ...))`; stop the watcher;
     `after = await harness.media_metrics()`.
- Asserts (collect a report first, print it, then assert so the report always shows):
  - every call: `finished.failures(scenario) == []` (zero dropped calls);
  - `peak_active == CONCURRENT_CALLS` (all 50 were attached to the one media process at the same time);
  - `sample(after, "va_audio_underruns_total") - sample(before, ...) == 0`;
  - overheads = flattened `turn_overheads` over all calls; count == total caller turns (3 × 50 = 150) and
    `nearest_rank(overheads, 50) <= OVERHEAD_P50_MS`.
- Report (print, markdown table; the Builder copies it into the plan tick and scratchpad): calls, peak attached,
  dispatch spread (max−min of `created_at` or wall time of the gather), turns measured, overhead p50/p95/max,
  sim e2e p50/p95 (`TurnOverhead.sim_ms`), underruns, event-loop lag p50/p99 from `histogram_quantile` (reported;
  specs/07 §21's ≤ 10 ms p99 is reported, not gated — Decision), total test wall time. Builder also records
  `docker stats --no-stream media sim` taken mid-run (manual evidence, not in the test).
- jscpd: reuse `e2e_assertions` helpers; keep the rules/scenario text distinct from outbound_basic.

### 4. Specs/docs
- specs/30 §6 `> Decision (2026-11-02): single-pod concurrency check (P05-T17e)`: the phase-05 one-pod variant of
  the swarm lives in `tests_e2e/test_concurrent_calls.py` (runs in `make e2e` against the compose stack's single
  media process and single-process sim); 50 calls dispatched at once by an org on the `scale` plan; gated: zero
  dropped calls, all 50 attached at once, zero platform underruns (definition: specs/07 §9 Decision P05-T17e), overhead
  p50 ≤ 150 ms; reported: overhead p95, event-loop lag p99, sim e2e; the 1 000-call swarm, `scripts/load/`, and the
  §21 lag/lateness p99 asserts stay with phase 20.
- specs/07 §9 Decision (underrun definition, `on_underrun`, metric increment, `pipeline_ended` fields).
- AGENTS.md bullet (P05-T17e): underrun definition + `CallPipeline(on_underrun=)`, `e2e_metrics` helpers,
  `create_org(plan_id=)`, `media_metrics`/`watch_active_calls`, measured numbers, runtime of the check.
- Plan: tick P05-T17e AND P05-T17 (all of a–e done) with evidence.

### Gates
- `make fmt lint typecheck placeholders`; `uv run python scripts/check_spec_refs.py`;
  `uv run pytest packages/va_voice -q -k "outbound or speaker or pipeline"`; `uv run pytest tests_e2e/unit -q`;
  media runtime tests touched.
- `make e2e` (images rebuild for va_voice/media changes; background + Monitor heartbeat; truncate the log first):
  all 16 tests green; report the concurrency table.
- Full `make test` in the background with heartbeat; jscpd 0 clones, radon, pip-audit/pnpm audit.

### Risks
- The single-process sim (50 media streams + STT analysis + TTS synthesis on one loop) may lag and inflate the
  sim-measured latency. If overhead p50 fails, first check sim CPU (`docker stats`) vs media loop lag; a sim-caused
  failure must be fixed in the sim (e.g. offload CPU work it already does inline to threads), never by loosening the
  150 ms gate. Record the measurement either way.
- Rate limits: `scale` burst 50 exactly matches 50 calls; if any other bucket refuses (default bucket 100/s is
  fine; caller-ID or destination limits), fix the test setup (e.g. distinct caller IDs), not the limiter.
- Realistic profile + 3 turns ≈ 30–40 s per call; all overlap because dispatch takes < 3 s — the peak assert proves it.
- Pre-existing `fix:bed-window-flake` may hit `make test`; rerun that single test to show it is unrelated.

## Build P05-T17e (2026-11-02): partial, blocked on media-worker CPU

Done in the working tree (uncommitted, gates below green):
- Underrun accounting: `OutboundStats.underruns`, `OutboundAudio/Speaker/CallPipeline(on_underrun=)`, runtime
  increments `va_audio_underruns_total`, `pipeline_ended` logs outbound_frames/late_frames/max_lateness_ms/underruns
  (specs/07 §9 Decision P05-T17e). Tests: test_outbound_audio (5 new + 2 asserts), pipeline/test_pipeline_underruns,
  runtime sim `test_caller_turn_is_answered_by_the_task_brain` checks the hook → counter (`Rig.metrics`).
- SpectralGate profile refresh cadence 32 hops + partial-sort percentile (equal to np.percentile): 23.9 → 2.1 ms per
  call-second (specs/07 §10.1 Decision P05-T17e; tests in test_noise.py).
- Harness: `e2e_metrics` (+ unit tests), `create_org(plan_id=, dollars=)`, `media_metrics`, `watch_active_calls`,
  `load_scenario(instance=)` (copies of one scenario in a test got the SAME id and replaced each other → 49 of 50
  calls had no caller script), `finished/wait_complete/wait_call(poll_s=)` (50 pollers at 0.25 s → 429 on the scale
  plan's 100 req/s and ~100 % api CPU). Flow `tests_e2e/test_concurrent_calls.py` + scenario/rules.
- Compose Postgres `max_connections=300` (default 100 ran out: temporal ~32 + 5 apps × 2 pools; api 500 `too many
  clients already`) — specs/29 Decision P05-T17e; specs/30 §6 Decision P05-T17e for the check itself.
- Gates run: make fmt/lint/typecheck/placeholders 0; jscpd 0 clones; check_spec_refs 0; pytest packages/va_voice +
  tests_e2e/unit + scripts/tests/test_compose 1101 passed; apps/media_worker 463 passed; pip-audit clean, pnpm audit
  3 moderate. Full `make test` / `make e2e` NOT run (the new e2e flow fails by design until the CPU work lands).

## Blocker P05-T17e

What: one media-worker process cannot carry 50 concurrent simulated calls on this host (c7i.2xlarge, compose stack
sharing 8 vCPU). Latest run (all 50 scenarios pass their assertions, 145 of 150 turns measured):
overhead p50 620 ms / p95 1443 ms (gate 150 ms), underruns 1743–3496 (gate 0), event-loop lag p50 ≤ 25–50 ms,
p99 ≤ 150–200 ms (target 10), media container ~140 % CPU, sim ~70 %, host ~55 % busy.
Why: the media main thread is saturated by per-frame Python work: py-spy (--idle --native) shows
`InboundPath.push` ≈ 60 % of the main thread, of which ≈ 15 % is a futex wait to re-take the GIL after soxr/FFT
release it (two `va-vad` pool threads hold the GIL ~15 % of the time for Silero glue), plus asyncio task overhead.
Attempts: (1) gate profile cadence 8 hops → lag p50 200→75 ms; (2) cadence 32 + partition → lag ≤ 50 ms;
(3) VAD inline on the loop (MEDIA_VAD_THREADS=0 experiment, reverted) → lag ≤ 25 ms, underruns halved, still far off.
A standalone 50-leg inbound loop (decode→resample→gated VAD, pool of 2) runs at lag p50 1.2 ms, so the remaining
cost is the sum of WS JSON/base64 in and out, STT ship, outbound pacing/mixing, GIL handoffs and task switches.
Proposal (Planner): split P05-T17e:
  (a) ship what is done (underrun accounting, gate cadence, harness, Postgres connections, the concurrency flow
      parameterised by `E2E_CONCURRENT_CALLS`, default 50, and gated as specced) once (b) lands, or
  (b) new task "media worker 50-call capacity": batched Silero inference across legs (one ORT run per 32 ms tick
      for every leg with a complete window; state shape (2, B, 128) supports a batch), batching inbound frames per
      leg (process 2 frames per hop), cheaper Telnyx frame encode/decode (no per-frame dataclass replace / JSON
      round-trip where avoidable), `sys.setswitchinterval` review, and re-measure with py-spy; acceptance = the
      existing `test_concurrent_calls.py` gates. Optionally run the check with a sim on its own CPU budget.
Note: the running media image contains the reverted inline-VAD experiment code (default env, 2 threads: same
behaviour); the next `make e2e` rebuilds it.

## Planner 2026-11-02: P05-T17e split after build.blocked

Blocker accepted: the remaining gap (overhead p50 620 ms vs 150, thousands of underruns) is media-worker CPU work,
a separate engineering task. Gates must not be loosened. Split in plan/phase-05.md:
- P05-T17e1: ship the measurement work already in the working tree (green today) with the 50-call flow opt-in
  (`capacity` marker, `make e2e-capacity`), so the tree is committed and `make e2e` stays green.
- P05-T17e2: the capacity work (runtime task `perf:media-50-calls`, task-1793660249-aaad) + flow joins `make e2e`.
P05-T17e and P05-T17 tick when both are done. The phase exit gate needs `make e2e` green, which with e2 includes
the capacity flow — that is the phase constraint ("50 concurrent simulated calls per process").

## Plan P05-T17e1 — Concurrency measurement and capacity flow (opt-in)

Everything listed in "Build P05-T17e (2026-11-02): partial" above is already implemented and its gates are green;
this task finalises and ships it. No production redesign.

### 1. Opt-in marker
- `pyproject.toml` `[tool.pytest.ini_options] markers`: add `"capacity: single-pod concurrency check, run with make e2e-capacity"`.
- `tests_e2e/test_concurrent_calls.py`:
  - `pytestmark = [pytest.mark.e2e, pytest.mark.capacity, pytest.mark.timeout(900)]`.
  - `CONCURRENT_CALLS` → `concurrent_calls() -> int` reading `E2E_CONCURRENT_CALLS` (default 50; must be an int
    in 1..50 else `pytest.fail` with a clear message — 50 is the `scale` plan burst and the specced target).
    Derived expectations scale with it: peak attached == n, turns == CALLER_TURNS × n. Keep the gates identical
    (zero dropped calls, peak == n, underruns == 0, overhead p50 ≤ 150 ms); the report prints n.
  - Put the env parsing in a pure helper in `tests_e2e/support/e2e_metrics.py` (or a new tiny
    `e2e_capacity.py`) with unit tests in `tests_e2e/unit/` (default, valid value, 0/51/"x" rejected) so
    `make test` covers it.
- `Makefile`:
  - `e2e:` → `uv run pytest tests_e2e -m "e2e and not capacity"`.
  - new `e2e-capacity: ## Single-pod 50-call concurrency check (E2E_CONCURRENT_CALLS overrides the count)` →
    `scripts/dev_up.sh e2e` then `uv run pytest tests_e2e -m capacity -s`; add to `.PHONY`.
  - Check `scripts/tests/test_compose.py:433` / `test_check_toolchain.py:238` still pass (they read the Makefile).
- specs/02 §(make targets table, line ~141): `make e2e` row text → `-m "e2e and not capacity"`; new row
  `make e2e-capacity`. specs/30 §6 Decision P05-T17e: append that until P05-T17e2 the flow is opt-in
  (`capacity` marker, `make e2e-capacity`) with the measured baseline, and P05-T17e2 moves it into `make e2e`.
- AGENTS.md: Commands table row for `make e2e-capacity`; extend the existing P05-T17e bullet (already in the
  working tree) with the marker/env var and the baseline numbers; keep it factual.

### 2. Evidence runs (Builder; background + Monitor heartbeat, truncate logs first)
- `make e2e` → 15 passed, 1 deselected (capacity).
- `make e2e-capacity` → expected to FAIL the overhead/underrun gates; copy the printed report (peak, overhead
  p50/p95, underruns, loop-lag p50/p99, dropped calls) into the tick and scratchpad. It must not error out for
  another reason (all calls complete, peak == 50); if calls drop, investigate before shipping.
- `E2E_CONCURRENT_CALLS=10 make e2e-capacity` (stack already up: `uv run pytest tests_e2e -m capacity -s`
  with the env var) → report; note whether 10 calls pass the gates (useful baseline for e2).
- Full `make check` (incl. `make test` in background with heartbeat), jscpd 0 clones, radon, pip-audit,
  pnpm audit, `check_spec_refs.py`.
- If `fix:bed-window-flake` hits `make test`, rerun that single test and note it (pre-existing, separate task).

### 3. Plan tick
- Tick P05-T17e1 only (not P05-T17e / P05-T17) with the evidence + baseline numbers.

### Risks
- The capacity run takes several minutes and a failing report is expected; the build.done must state it clearly
  ("capacity gates fail as expected; baseline: …") — Reviewer approves on the opt-in + baseline evidence.
- The running media image contains the reverted inline-VAD experiment; `make e2e` rebuilds images, run it first.

## Build P05-T17e1 (2026-11-02)

- Opt-in: marker `capacity` (pyproject), `pytestmark` in `tests_e2e/test_concurrent_calls.py` (test renamed
  `test_concurrent_calls_on_one_media_worker`; count from `E2E_CONCURRENT_CALLS` via new pure
  `tests_e2e/support/e2e_capacity.concurrent_calls` + `tests_e2e/unit/test_e2e_capacity.py`), Makefile `e2e`
  → `-m "e2e and not capacity"`, new `e2e-capacity`; specs/02 make table rows; specs/30 §6 opt-in Decision with
  the baseline; AGENTS command row + P05-T17e1 bullet.
- `make e2e`: 15 passed, 51 deselected, 297 s, exit 0.
- `make e2e-capacity` (50): fails as expected on underruns. calls 50 (0 failed scenarios), peak 50, dispatch
  1.58 s, 150 turns, overhead p50/p95/max 335/895/1096 ms, sim v2v p50/p95 2046/3371 ms, underruns 667, loop lag
  p50 ≤ 10 / p99 ≤ 150 ms, wall 45.4 s.
- `E2E_CONCURRENT_CALLS=10`: 10 calls, peak 10, 30 turns, overhead p50/p95/max 92/255/284 ms, sim v2v 1020/1186,
  underruns 2, lag ≤ 5/≤ 5 ms → fails only on underruns. Lead for P05-T17e2: underruns appear without CPU
  pressure, so check the underrun definition / sim drain timing as well as CPU.

## Build P05-T17e1 resubmit (2026-11-02)

The review.rejected event had an empty payload and no `## Review P05-T17e1` section exists: the Reviewer hit the
300 s inactivity timeout (AGENTS.md P05-T10d gotcha), probably while `make test` ran in the foreground. No code
changes. Re-ran: lint 0, typecheck 0, placeholders 0, check_spec_refs 0, tests_e2e/unit + touched va_voice +
test_compose 166 passed, jscpd 0 clones, radon avg 2.32. Earlier evidence still stands (make test 10796 passed /
7 skipped, 99.16 %; make e2e 15 passed, capacity deselected). Reviewer: run `make test` and `make e2e` with Bash
`run_in_background` plus a Monitor heartbeat, never in the foreground.

## Review P05-T17e1 (2026-11-02): approved

Re-ran: make lint/typecheck/placeholders 0; make test 10796 passed / 7 skipped, 99.16 % (background + heartbeat);
make e2e 15 passed, 51 deselected (capacity opt-in), 300 s; jscpd 0 clones; check_spec_refs 0. Diff checked against
specs/07 §9/§10.1 Decisions (underrun definition matches OutboundAudio: continuity reset on gap, clear, finish, idle),
specs/30 §6 opt-in Decision, specs/02 make table, specs/29 Postgres Decision. Capacity gates themselves remain for
P05-T17e2 (task perf:media-50-calls).
