# Scratchpad — Phase 07 (resumed run, 2026-11-04)

The previous loop stopped at its max runtime (12 h) partway through P07-T11e2. Its implementation is still in the
working tree and has not been committed. HEAD = 83d9014 (P07-T11e1). This file was recreated: the old scratchpad
had been deleted from the working tree. The old one is in git history (`git show HEAD:.ralph/agent/scratchpad.md`).

## Plan P07-T11e2 — Async regression runs and worker-completed publish

### State found by the planner (verified this iteration)
The uncommitted work already implements specs/12 §4 Decision P07-T11e2, items 1-7:
- `apps/api/src/va_api/services/pathway_publish.py` (new): `publish_pathway(PublishDeps, ctx, pathway_id,
  PublishRequest)`, used by `routers/pathway_versions.py`. Run starts share `pathway_test_runs.launch_run`.
- `va_domain.pathway_tests`: `RegressionPublish`, `PublishActor`, and `PathwayTestWorkflowInput.publish`.
- `va_db.pathway_publish`: `settle_regression_publish`, `publish_record`. Model column
  `PathwayTestRun.publish` and migration `0008_pathway_run_publish.py`. specs/03 DDL has the column appended last.
- `va_workflows`: activity `settle_regression_publish` (with inputs/policy) and the workflow step after the finish.
  `apps/worker/src/va_worker/activities.py` wires it.
- Tests: `apps/api/tests/test_pathway_publish_regression.py` (10 tests: none/blocking pending/settings trigger/
  simulations/reporting/empty suite/both start failures/validation first/500 cap);
  `packages/va_db/tests/test_pathway_publish_settle_db.py` (passed→published, blocked, stale, unavailable,
  pending/gone, SQL NULL); `test_pathway_run_publish_migration.py`; workflow tests (passed settles, failed →
  blocked, reporting settles nothing, never finished, settle keeps failing → unsettled); activity test (cache
  invalidated only when published). The existing migrate/migration tests are updated for head 0008.
- The AGENTS.md line and the spec Decision are written. openapi.json and both schema.d.ts files were modified.

Checks run this iteration: `make lint` 0, `make typecheck` 0, `make placeholders` 0. Targeted pytest over the 11
affected modules: 133 passed in 24.6 s.

### Builder steps (finish and verify; no redesign)
1. Read the full diff (`git diff`, plus the untracked files listed in git status) against Decision P07-T11e2 and the
   plan acceptance criteria. Close any gaps you find. Specifically confirm:
   - Run reads (`GET .../runs`, `.../runs/{run_id}`) expose `publish`, with null for other runs (item 5). The schema
     is in `routers/pathway_tests_schemas.py`. If no api test asserts this, add one to
     `test_pathway_publish_regression.py`.
   - The blocking response has `published_version: null` and `run_id`, and the `pathway:{id}:v:{env}` cache is
     NOT invalidated while the publish is pending.
   - The worker deletes the cache key only on `published` (activity test exists).
   - Run starts carry `Idempotency-Key` semantics unchanged (publish is a POST with side effects).
2. Regenerate the contracts: run `make openapi`, then `corepack pnpm --filter dashboard gen:api` and
   `corepack pnpm --filter @voiceagents/sdk gen:api`. Confirm there is no further drift
   (`uv run python scripts/check_openapi_drift.py`).
3. Gates: `make fmt lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`,
   `npx --yes jscpd@4 --silent` (must be 0 clones), `uvx radon cc -s -a` on the changed src files, and `make test-ts`.
   Run `make test` in the background (`make test > logs/make_test.log 2>&1; echo exit=$?`, after truncating the log
   first) with a Monitor heartbeat every ~2 min. The host is shared and gets OOM kills: if xdist workers die, re-run
   with `-n 6` via `PYTEST_ADDOPTS`. Load timeouts in untouched modules must be re-run alone as evidence.
   Coverage ≥ 85 % per package.
4. No e2e is needed: this is not a call/SMS flow. The e2e regression gate flow is P07-T11e3.

### Risks
- Migration head is now 0008: every test that hard-codes the head or the revision list must agree. The migrate and
  migration tests are already updated; grep for `"0007"` to find any stragglers.
- JSONB `None` is stored as JSON `null`: the code already omits the key (AGENTS line).
- Time-skipping Temporal: the settle activity runs after the finish activity. Do not cancel dispatched watches
  (AGENTS P05-T13 gotcha).

## Build P07-T11e2 (2026-11-04)
- Verified diff against Decision: run reads expose `publish` (pending/published asserted in
  test_pathway_publish_regression); added assertion that an ordinary unit run reads `publish: null` (test_pathway_tests_run).
  Blocking publish leaves the cache key untouched (asserted); worker deletes it only on `published` (activity test).
- Contracts regenerated (make openapi + both gen:api), drift check clean.
- Gates: fmt/lint/typecheck/placeholders 0, spec refs 0, jscpd 0 clones, radon avg 2.04, make test-ts 0.
- make test: 12504 passed, 7 skipped, coverage 99.09 %, exit 0.

## Review P07-T11e2 (2026-11-04) — approved
Re-ran: lint/typecheck/placeholders 0, make test 12504 passed 7 skipped (99.09 %), test-ts 0, drift clean, jscpd 0,
spec refs 0. Diff matches Decision P07-T11e2 items 1-7 (settle locks run then pathway; idempotent retry tested;
cache invalidated only on success/published). Plan's `regression_run_id` is superseded by Decision item 5 (`run_id`).
Note for T11e3: a blocking run whose start fails is marked `error` while its publish.outcome stays `pending` (allowed by the spec).

## Plan P07-T11e3 — Synchronous regression gate and e2e

Spec: specs/12 §4 publish step 2 (≤ 20 tests synchronous, 120 s, 409 `details.run_id`), §12, §13; builds on
Decision P07-T11e2 (async path, `va_db.pathway_publish.settle_regression_publish`, `publish_record`).

### Decision to add (specs/12 §4, new block "> Decision (2026-11-04): synchronous regression gate (P07-T11e3)")
1. The synchronous path applies only to a BLOCKING suite (`block_publish_on_failure: true`) with no simulation
   jobs and 1-20 unit tests (`SYNC_MAX_TESTS = 20`). Blocking suites with simulations or > 20 tests keep the
   P07-T11e2 async path (`pending_regression`); non-blocking suites keep publish-now + async reporting run (running
   them in the request would only delay a publish that happens anyway).
2. Tx 1 (as today, pathway row lock + publish-mode validation first): insert the `regression` run with the
   `publish` record `{block: true, ..., outcome: "pending"}` and move it to `running` (`PathwayRunRepo.begin`) in
   the same transaction; commit. No workflow is started.
3. The api runs the tests in the request, at most 8 concurrently, oldest first, each through
   `va_pathways.runs.run_stored_test` (same items as the worker; no DB session held while a test runs) and stores
   each item with `PathwayRunRepo.put_result`. An exception from one test becomes an `error` item `runner_error`
   (logged, no retries in the request). The whole suite is capped at 120 s on the injected clock
   (`va_common.waits.within`); tests not done by then are cancelled and stored as `error` items
   `regression_timeout` (`replace=False`). Then `finish` (results ordered by test order).
4. Tx 2: `settle_regression_publish` with the same `RegressionPublish` intent the worker would get →
   `published` answers 200 `{"status":"success","published_version","environment","version_number",
   "draft_version","run_id"}` (router invalidates the cache as for every success). Any other outcome
   (`blocked`, `stale` = draft edited during the run, `version_unavailable`; a missing run row counts as
   `version_unavailable`) answers 409 `CONFLICT` with `details {"run_id", "outcome"}` and messages:
   blocked "The regression run did not pass; the version was not published.", stale "The pathway changed during
   the regression run; publish again.", version_unavailable "The pathway version is no longer available.".
   Nothing published, cache untouched.
5. A request cancelled mid-run (client disconnect) finishes the run in a shielded cleanup: remaining tests are
   cancelled and the run is closed via `finish` (missing items count as error),
   then settle → `blocked`; never left `running`/`pending`.
6. Idempotency unchanged (409 is not stored as success; a replay of a 200 replays the body).

### Files
- `packages/va_pathways/src/va_pathways/runs.py`: add `"regression_timeout"` to `RunError`.
- NEW `apps/api/src/va_api/services/pathway_regression_sync.py`:
  - `SYNC_MAX_TESTS: Final = 20`, `SYNC_TIMEOUT_S: Final = 120.0`, `SYNC_CONCURRENCY: Final = 8`, the three 409
    messages (`CONFLICT_MESSAGES: Mapping[outcome, str]`).
  - `class RegressionTestRunner(Protocol): async def run(self, test: StoredTest) -> Item`.
  - `class StoredTestRunner` (production): `__init__(*, text: TextDeps, database, org_settings)`; `run` loads the
    counter once with `await asyncio.to_thread(default_counter)` (functools.cache) and calls `run_stored_test`.
  - `async def run_sync_suite(database, runner, clock, *, org_id, pathway_id, version_number, run_id,
    test_ids, timeout_s=SYNC_TIMEOUT_S) -> None` (TaskGroup + Semaphore(8); store items; timeout items; finish;
    shielded finish on cancellation).
  - `async def settle_sync(database, clock, org_id, run_id, intent) -> SettleResult` raising the 409 AppError.
- `apps/api/src/va_api/services/pathway_publish.py`: `PublishDeps` gains `runner: RegressionTestRunner` and
  `sync_timeout_s: float = SYNC_TIMEOUT_S`; `_Suite.synchronous` property (block and no simulations and
  len ≤ 20); `_Planned` gains `intent: RegressionPublish | None` + `synchronous: bool`; `_plan` begins the run
  in-tx for the sync case; `publish_pathway` runs `run_sync_suite` + `settle_sync` and builds the success
  `PublishOutcome` from `SettleResult.published_version/draft_version`. Update module docstring.
- `apps/api/src/va_api/container.py` + `build_container`: `pathway_test_runner: RegressionTestRunner`
  (`StoredTestRunner(text=TextDeps(gateway=llm_gateway, tools_http=tools_http, clock=clock,
  allow_http=insecure_urls_allowed(va_env)), database=, org_settings=)`). Router `pathways_publish` passes
  `runner=request.app.state.container.pathway_test_runner`. Check `apps/api/tests/test_container.py` close-order
  assertions still hold (no new client).
- `apps/api/tests/support/session_helpers.py` `SessionContainer`: `pathway_test_runner` field (init=False) built
  in the same place as `llm_gateway`/`tools_http` as the real `StoredTestRunner`; tests may replace it.
  `apps/api/tests/support/call_fakes.py`: `FakePathwayTestRunner` (per-test script: status
  `passed|failed|error`, `"raise"`, or `"hang"`; optional `before` async hook for the stale test; records
  `.ran` and `.max_in_flight`).
- `apps/api/src/va_api/routers/pathway_versions.py`: docstring mention; `Published` unchanged.
- `specs/12-pathways-api.md` Decision above (+ correct P07-T11e2 item text pointer if needed); AGENTS.md line.
- `make openapi` + both `gen:api` if the publish docstring/description changes the schema.

### Tests
`apps/api/tests/test_pathway_publish_sync.py` (container clock `chat_helpers.WallClock()`, fake runner):
- `test_a_passing_suite_publishes_in_the_request`: 200 success, published_version, run_id; run detail kind
  regression, status passed, `publish.outcome == "published"`; production pin set; starter.started empty; cache
  key deleted; one `pathway.publish` audit.
- `test_a_failing_test_answers_409_and_publishes_nothing`: details run_id + outcome blocked; run failed; pin
  unchanged; cache untouched; draft_version unchanged.
- `test_a_runner_exception_is_an_error_item`: item `runner_error`, run error, 409.
- `test_the_suite_timeout_records_timeout_items` (timeout_s small via `container.regression_timeout_s` or
  monkeypatching `SYNC_TIMEOUT_S`, a hanging test): items `regression_timeout`, 409, run not left running.
- `test_a_draft_edited_during_the_run_is_stale` (runner `before` hook updates the draft over the API/DB) → 409
  outcome stale.
- `test_concurrency_is_capped_at_eight` (12 tests gated) → `max_in_flight == 8`; results in test order.
- `test_more_than_twenty_tests_go_async` (21 seeded tests, seed rows directly) → pending_regression + started.
- `test_simulations_keep_the_async_path`.
- `test_a_reporting_suite_stays_async` (non-blocking, ≤ 20) → success + started.
- service-level `test_a_cancelled_request_blocks_the_publish` (call `publish_pathway` in a task, cancel while the
  fake hangs; run status error, publish outcome blocked).
- integration `test_real_runner_against_fakellm` using `chat_helpers.chat_stack` + `restaurant`-like graph:
  unit test "I want to order food" expects `route_to_node_id: "order"`; edge to wrong node → 409; fixed → 200.
Update `test_pathway_publish_regression.py`: blocking tests that relied on the async path with ≤ 20 tests now need
a scenario + `include_simulations` (or > 20 seeded tests) — keep their assertions.
`packages/va_pathways/tests`: no change unless `RunError` is enumerated somewhere.

### e2e
- `tests_e2e/test_pathway_regression_gate.py` (`pytestmark = [e2e, timeout(600)]`): `create_org()`; create a
  pathway (greet → order/bye edges with labels "Caller wants to order food"/"Caller says goodbye"; FakeLLM routes
  by edge label words, no rules needed) whose broken draft points `e_order` at `bye`; create one unit test
  (node `greet`, user_message "I want to order food please", expected route_to_node_id `order`,
  simulated_variations 0); PUT regression `{run_on_publish: true, block_publish_on_failure: true}`; publish → 409
  `CONFLICT`, `details.run_id`; GET run → status failed, publish.outcome blocked, production version absent;
  fix the draft; publish → 200 success, published_version; GET pathway shows production pin; run passed.
- Harness helpers in `tests_e2e/support/e2e_harness.py`: `api_post(org, path, json) -> httpx.Response` and
  `api_get` (raw), or dedicated `create_pathway`, `update_draft`, `create_unit_test`, `put_regression`,
  `publish` (raw response), `pathway_run`. Node JSON shape: copy from `graph_helpers` (camel keys).
- Run on a running stack after rebuilding api (`scripts/dev_up.sh e2e` or `make e2e`); evidence = `make e2e`
  (full; or at least the new flow + `make test`).

### Gates
`make fmt lint typecheck placeholders`, `check_spec_refs.py`, jscpd 0, radon, `make openapi` drift, `make test-ts`
if schema changed, `make test` in background with heartbeat Monitor, `make e2e` (touches no call flow, but the
plan acceptance requires the flow green).

### Risks
- FrozenClock.sleep advances instantly, so `within(...)` times out at once: sync tests must use WallClock; check
  every existing api test that publishes with a blocking ≤ 20 suite (they become synchronous and would time out /
  call the real runner) → move them to the async variant.
- The real runner needs the container's LLM gateway: in api tests without FakeLLM the default `StoredTestRunner`
  would hit an unreachable URL; tests that go synchronous must set a fake runner.
- Request duration up to 120 s: no api-level request timeout exists today (verify uvicorn/idempotency claim TTL
  is longer than 120 s; if the idempotency claim TTL is shorter, note it in the Decision).
- Cancellation cleanup must be shielded and not swallow the CancelledError.

## Build P07-T11e3 (2026-11-04)
- New `va_api.services.pathway_regression_sync` (SYNC_MAX_TESTS 20, SYNC_TIMEOUT_S 120, SYNC_CONCURRENCY 8,
  `StoredTestRunner`, `run_sync_suite` with shielded close+settle, `conflict_for`); `pathway_publish` begins the run
  in the publish tx for synchronous suites and answers 200/409; `RunError` gains `regression_timeout`;
  `Container.pathway_test_runner` (+ SessionContainer); spec Decision P07-T11e3 (idempotency note: 4xx are stored,
  claim lock 60 s < 120 s cap, documented); AGENTS line.
- Tests: `test_pathway_publish_sync.py` (12: pass/fail/setting/raise/timeout/stale/concurrency/cancel/>20/sims/
  reporting/real runner vs FakeLLM); regression module forced async via SYNC_MAX_TESTS=0; `publish_helpers.Gate`.
- e2e `tests_e2e/test_pathway_regression_gate.py` passed against the rebuilt stack.
- make test run 1: 12515 passed, 1 failed in untouched `media_worker/test_turn_writer.py` (unraisable unclosed
  socket ResourceWarning GC'd during it); passes alone 3/3 and after my modules in one process. Re-running.
- make test run 2: 12516 passed, 7 skipped, 99.09 %, exit 0. make test-ts exit 0. lint/typecheck/placeholders 0,
  spec refs 0, jscpd 0 clones, radon avg 2.31, openapi drift clean (openapi.json + both schema.d.ts regenerated).
- make e2e: 17 passed, 1 failed (amd_ivr_mode, missing 2nd DTMF; passed 3/3 alone). Warm re-run of all flows:
  17 passed, 1 failed (interruptibility_levels[1], 1183 ms vs 1200 ms rule; standalone 2/4 fail) — pre-existing
  timing flakes in call flows this task does not touch; task fix:e2e-interrupt-level1 opened.
  test_pathway_regression_gate passed in every run.

## Review P07-T11e3 (2026-11-04) — approved
Re-ran: lint/typecheck/placeholders 0, spec refs 0, openapi drift clean, jscpd 0, radon avg 2.27, make test 12516
passed 7 skipped (99 %), e2e test_pathway_regression_gate passed on the running stack. Diff matches Decision
P07-T11e3 items 1-6 (sync only for blocking, no sims, ≤ 20; run begun in publish tx; shielded conclude on
cancel; 409 details run_id/outcome; cache untouched on conflict). Reviewer removed the unused
`PublishDeps.sync_timeout_s` field (dead code; tests monkeypatch SYNC_TIMEOUT_S); both publish modules re-run: 22 passed.

## Planner 2026-11-04 (after P07-T11e3 approval)
Ticked parents P07-T11e and P07-T11 (all subtasks done). P07-T12 is too big for one builder pass (adapter + media
runtime/persistence + two e2e flows), so it is split in plan/phase-07.md into T12a (PathwayBrain adapter, pure,
va_engine + small va_voice pipeline change), T12b (media runtime wiring, events, engine snapshots, variables,
prefetch opener), T12c (the two e2e flows).

## Plan P07-T12a — `PathwayBrain`: PathwayEngine as the pipeline Brain

Specs: specs/07 §16 (Brain contract + Decision P04-T12a), specs/11 §4 rows (Wait for Response, End Call, Transfer
Call, Press Button), §7 (terminal actions after the preceding line played), §10 (EngineIO). Engine contract
(engine.py docstring): resume a generator only after the yielded speech played; on barge-in close it, then
`on_interrupted(text)`. The pipeline already satisfies "resume after playback": `CallPipeline._handle_output`
awaits `Speaker.speak` to completion before pulling the next output, and closes the outputs generator
(`_close(outputs)`) when the reply is cancelled, then calls `brain.on_interrupted`.

### Spec Decision to add (specs/11 §10, "> Decision (2026-11-04): voice Brain adapter (P07-T12a)")
- `va_engine.pathway_brain.PathwayBrain(engine, *, clock, on_state=None)` adapts `PathwayEngine` to the
  `va_voice.pipeline.Brain` protocol (the narrower §16 contract): `start(greeting)` → `engine.start(greeting)`,
  `on_user_turn` → `engine.handle_user_turn`, `on_no_input` → `engine.on_wait_timeout` while a Wait for Response
  timer is armed (else an empty stream = the pipeline's own reprompt), `on_interrupted` → `engine.on_interrupted`.
- Action mapping: `Say` → `SpeakStatic(text, interruptible=action.interruptible is not False)`; `SayStream` →
  `SpeakText(tokens, interruptible=...)` (`llm_request_ms` = clock monotonic ms when the stream was yielded);
  `EndCallAction` → `EndCall(final_text=None, reason="agent_hangup")` (the final line was already yielded as
  Say/SayStream and played; `reason` stays the pipeline's `agent_hangup` so `session_end` maps it unchanged; the
  engine reason goes to the log); `TransferAction` (cold, or warm until phase 12 → logged
  `pathway_brain.warm_transfer_as_cold`) → `va_engine.models.results.TransferRequest(target=action.target)`;
  `SendDtmfAction` → `PressButton(digits)`; `Wait(seconds)` → arms the node timer (no output);
  `SwitchGraph`/`HumanTakeover` never reach a voice consumer (the engine applies the first, text channels only
  get the second) → logged `pathway_brain.action_ignored`, nothing yielded.
- Wait for Response on voice: `BrainTiming.no_input_timeout_ms: int | None` (new field, default None). While a
  `Wait` is armed `timing()` returns `seconds * 1000`; the pipeline arms its no-input timer with that timeout
  instead of `config.no_input_timeout_ms` and such expiries are Brain-owned: they do not count toward
  `max_reprompts` and never produce the pipeline's `no_input_goodbye` (the node's `maxReprompts`/`onTimeout` decide,
  inside `on_wait_timeout`). Any user turn (speech or DTMF) disarms the Brain's wait (the engine re-arms through a
  new `Wait` action); the brain also disarms when `on_wait_timeout` yields no `Wait` again.
- Timing of the current node: `interruptibility` = node `interruptibility` (None when unset), `resumption_speed` =
  node `resumption_speed`, `collecting_number` False, `no_input_prompt` None. (Read through
  `engine.navigator.node`; `block_interruptions` maps to interruptibility 0 if that is how `effective_level`
  treats "block" — check `va_voice.interrupt.effective_level` and use its `block_interruptions` semantics: the
  pipeline config carries call-level block; for a node with `block_interruptions: true` report
  `interruptibility=0`.)
- `on_transfer_failed(spoken)`: appends an assistant `TurnRecord` (node = current) via `engine.state.add_turn`
  so later decisions know (same role as TaskBrain's).
- `on_state(state)` (sync callback) runs after every completed or closed engine generator (start, user turn, wait
  timeout); T12b stores the snapshot. `aclose()` = `settle_background()` bounded by 2 s on the clock
  (`va_common.waits.within`), then `engine.aclose()`; `state`, `variables` (copy of `state.variables`) and
  `pathway_logs()` properties for T12b.

### Files
- NEW `packages/va_engine/src/va_engine/pathway_brain.py`: `PathwayBrain` as above (`__all__`). Implementation:
  one private async generator `_relay(actions: AsyncGenerator[Action]) -> AsyncGenerator[BrainOutput]` used by all
  three entry points, wrapping the engine generator in `contextlib.aclosing` so closing the brain generator closes
  the engine's (barge-in), and calling `on_state` in `finally`. `start`/`on_user_turn`/`on_no_input` return
  `AsyncGenerator[BrainOutput]`. Disarm the wait at the start of `on_user_turn` and `on_no_input`.
- NEW `packages/va_engine/src/va_engine/pathway_call.py`: `pathway_facts(context: CallContext, org: OrgSettings,
  *, now: datetime) -> ConversationFacts` (org_id, call_id, channel "voice", request_data = config.request_data,
  builtins = `call_builtins(context, now=now)` (check its signature in builtins.py:63), timezone = config timezone
  (fallback "UTC"; look how TaskBrain/voice_inputs pick it), transfer_list = {} (pathways ignore it, specs/05)).
  Also `start_state(active: ActiveGraph, start_node_id: str | None) -> EngineState` = `new_pathway_state(active)`
  then, if `start_node_id` names another node of the graph, move `current_node_id` there with visit_counts
  {start_node_id: 1} (the api already validated it exists; an unknown id falls back to the start node + log).
- `packages/va_voice/src/va_voice/pipeline/models.py`: `BrainTiming.no_input_timeout_ms: int | None = None`.
- `packages/va_voice/src/va_voice/pipeline/silence.py`: `NoInputTimer.arm(now_ms, *, timeout_ms=None)` uses the
  override; a flag `brain_owned` on the armed deadline so `expire`/the pipeline's decision skips the reprompt
  counting and the goodbye for brain-owned expiries. Keep existing behaviour identical when `timeout_ms` is None.
- `packages/va_voice/src/va_voice/pipeline/pipeline.py`: `_arm_no_input()` passes
  `self._timing.no_input_timeout_ms` (the cached Brain timing, re-read in `_apply_timing`); on a brain-owned
  expiry queue `_NoInputJob` whose outputs are `brain.on_no_input()` directly (no fallback reprompt text when the
  stream is empty, no goodbye). Update the module docstring lines 20-25.
- `packages/va_engine/src/va_engine/__init__.py`: only if it re-exports runners (check; keep import-free style
  of the package as it is).
- `specs/11-pathways-engine.md` §10 Decision; `specs/07-media-pipeline.md` §12 one-line Decision note for the
  Brain-owned no-input timeout (specs/07 §12 is the no-input section; verify the number with grep).
- AGENTS.md line (P07-T12a): where PathwayBrain lives, the action mapping, `BrainTiming.no_input_timeout_ms`, test
  helpers.

### Tests
- NEW `packages/va_engine/tests/test_pathway_brain.py` (real `PathwayEngine` from
  `engine_helpers.engine_rig(graph, ...)`, wrap `rig.engine` in `PathwayBrain(rig.engine, clock=rig.clock,
  on_state=states.append)`; graphs from `graph_helpers` `node`/`edge`/`start`/`graph`):
  - `test_static_start_node_speaks_static`: start node with `text` → one `SpeakStatic` with the rendered text,
    `on_state` called once.
  - `test_dialogue_reply_is_speak_text`: Default node without text → `SpeakText` whose tokens are the FakeSource
    reply; history records it after the tokens are read.
  - `test_end_call_node_yields_final_line_then_end_call`: route to an End Call node with `text` → `[SpeakStatic(line),
    EndCall(final_text=None, reason="agent_hangup")]`.
  - `test_transfer_node_yields_transfer_request` (target E.164) and warm → same + log event.
  - `test_press_button_yields_press_button`.
  - `test_wait_for_response_arms_the_timer`: Wait node waitSeconds 5 → `timing().no_input_timeout_ms == 5000`;
    `on_no_input()` → reprompt text as SpeakStatic; after maxReprompts the onTimeout action (end_call →
    EndCall); a user turn disarms (`timing().no_input_timeout_ms is None` once the engine moved to a non-wait node).
  - `test_no_input_without_wait_is_empty`.
  - `test_closing_mid_stream_closes_the_engine_and_truncates`: take the first output (SpeakText) of a user turn,
    read part of the tokens, `aclose()` the brain generator, `await brain.on_interrupted("Hel—")` → the engine's
    last assistant TurnRecord content `"Hel—"` and interrupted True; `on_state` called.
  - `test_node_timing_reflects_the_current_node` (interruptibility, resumption_speed, block_interruptions → 0).
  - `test_transfer_failed_note_is_recorded`.
  - `test_aclose_settles_and_closes` (FakeSource closed / speculation closed).
  - `test_pathway_facts_and_start_state` (va_domain `resolved_configs.resolved_config()` context; start_node_id
    override + unknown id fallback).
- NEW `packages/va_engine/tests/test_pathway_brain_pipeline.py`: `pipeline_harness.harness(engine=PathwayBrain(...),
  clock=RealtimeClock(), config=PipelineConfig(no_input_timeout_ms=30_000))` (see AGENTS P05-T09c note: wait on
  `h.observer.user_turns`, barge-in needs ≥ 900 ms caller speech at default level): (1) greet → caller speaks →
  router (KeyedPort route answer) → End Call line plays → pipeline result `agent_hangup`, transport saw the
  final line's audio before ending; (2) a Wait for Response node with waitSeconds 1: no caller speech → engine
  reprompt spoken (not the pipeline's default reprompt), then onTimeout end_call ends the call; (3) barge-in during
  a long SpeakText → engine history holds the truncated text. Use `FakeTts`/`FakeStt` from pipeline_fakes; the
  KeyedPort/FakeSource from engine_helpers (FakeSource replies need `hanging`-like long text for the barge-in; if
  FakeSource cannot pace tokens, use a static long `text` node — SpeakStatic barge-in truncation also goes
  through `on_interrupted`).
- `packages/va_voice/tests/pipeline/…` (find the existing no-input tests: `grep -rn no_input packages/va_voice/tests`):
  add `test_brain_owned_no_input_timeout_uses_node_seconds` (ScriptedBrain timing with no_input_timeout_ms=2000 →
  `brain.no_input` logged at ~2 s, not at config timeout), `test_brain_owned_expiry_never_says_goodbye` (more
  expiries than max_reprompts → no EndCall `no_input_timeout`), and NoInputTimer unit tests for the override. The
  ScriptedBrain may need a `timing` knob (add `timings=` to pipeline_fakes.ScriptedBrain if absent).

### Gates
`make fmt lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a` on changed src, `make test` in background with a Monitor heartbeat (~25 min;
truncate the log first; OOM → `PYTEST_ADDOPTS="-n 6"`). No openapi change. No e2e (no call flow is wired yet;
T12b/T12c bring it).

### Risks
- Brain-owned no-input must not change task-mode behaviour: every existing silence test must pass unchanged.
- `EndCall` reason: keep `agent_hangup` (check `va_media.runtime_config.session_end` mapping and what
  `END_CALL_REASON` TaskBrain uses for its end_call tool; use the same constant).
- Engine generators must always be closed (aclosing) or speculation/background tasks leak ("Task was destroyed"
  warnings are errors under filterwarnings).
- `llm_request_ms` semantics: look at how TaskBrain fills it (latency tracer mark) and copy.

## Build P07-T12a (2026-11-04)
- NEW `va_engine.pathway_brain.PathwayBrain` (relay over aclosing engine generators, action mapping, node wait →
  `BrainTiming.no_input_timeout_ms`, timing from node model_options, on_transfer_failed via new
  `PathwayEngine.note_assistant`, on_state after every generator/truncation/note/aclose, aclose settles ≤ 2 s).
  `llm_request_ms` left None (pipeline marks on first read = same instant as the yield).
- NEW `va_engine.pathway_call` (`pathway_facts(context, now=)` — dropped the unused `org` arg from the plan;
  `start_state(active, context)` seeds context.variables + start_node_id fallback).
- va_voice: `BrainTiming.no_input_timeout_ms`, `NoInputTimer.arm(..., timeout_ms=)` / `brain_owned` / expiry
  `"brain"`, pipeline `_NoInputJob(brain_owned=True)` runs `brain.on_no_input()` raw; log `pipeline_no_input_brain`.
- Spec Decisions: specs/11 §10 P07-T12a, specs/07 §12 P07-T12a. AGENTS line.
- Tests: test_pathway_brain.py (22, 98 % of pathway_brain, 100 % pathway_call), test_pathway_brain_pipeline.py (3,
  accelerated clock), va_voice silence (+2 pipeline, +2 unit). Every existing va_voice/va_engine test passes.

## Review P07-T12a (2026-11-04) — approved
Re-ran: lint/typecheck/placeholders 0, spec refs 0, jscpd 0 clones, radon avg 2.14 on changed src, make test 12545
passed 7 skipped exit 0. Diff matches Decision P07-T12a (specs/11 §10, specs/07 §12): action mapping, engine
generators closed via aclosing, timing re-read after each reply so Brain-owned wait arms only after playback,
expiry "brain" uncounted, configured policy returns when the wait clears. No findings.

## Plan P07-T12b — Media runtime runs pathway calls

Specs: specs/07 §16, specs/11 §9 (`call:{id}:engine` 2 h, final variables → `calls.variables`), §12 + Decision
P07-T08c, specs/19 §2 (`call.route`/`call.extraction`/`call.loop_condition`/`call.webhook_node`/`call.sms_sent`),
specs/01 §2.1 step 2 (prefetch), specs/07 §11 step 5 (opener pre-render).

### Key finding (drives a schema change)
`call_turns.turn_index` is the PIPELINE counter (every user AND agent turn: opener 0, user 1, agent 2, …), but engine
events carry the ENGINE `turn_index` (+1 per accepted caller turn; opener = 0; a superseding turn takes a new index).
`va_db.call_object._pathway_logs` joins them by `turn_index`, so a stored pathway call would render wrong/empty
`pathway_logs`, and one engine turn may produce several assistant rows (chained nodes) — the unique key
(call_id, turn_index, role) forbids reusing turn_index. Decision: add `call_turns.pathway_turn integer` (nullable,
engine turn index of pathway calls, NULL for other runners); the Call Object groups by `pathway_turn` when present,
else `turn_index` (unchanged for task calls / existing tests).

### Spec Decisions to add (same commit)
- specs/11 §10 "> Decision (2026-11-04): pathway calls in the media runtime (P07-T12b)": runner selection, graph
  cache (frozen versions only), ports, events, snapshots, variables, prefetch opener, load failure → `error`.
- specs/11 §12: one-paragraph addendum to P07-T08c: rows carry `pathway_turn`; grouping rule above.
- specs/03: `call_turns` DDL gains `pathway_turn integer` LISTED LAST (added by ALTER, attnum order —
  `test_schema_matches_spec`); Redis key table (§11) gains `call:{<call_id>}:engine` (string, dump_engine_state JSON,
  TTL 7200 s, written by media per turn). Migration `packages/va_db/src/va_db/migrations/versions/0009_call_turns_pathway_turn.py`
  (upgrade ADD COLUMN, downgrade DROP COLUMN; head becomes 0009; check tests asserting head `0008` and update).

### Files
1. `packages/va_db/src/va_db/models/calls.py`: `CallTurn.pathway_turn: Mapped[int | None]` (Integer, nullable, last).
   `packages/va_db/src/va_db/call_turns.py`: `CallTurnRow.pathway_turn: int | None = None`; `CallTurnRepo`
   (va_db/repositories) writes it on insert/upsert. `va_db/call_object.py::_pathway_logs`: `LogTurn(turn_index=
   turn.pathway_turn if turn.pathway_turn is not None else turn.turn_index, …)`; order turns by
   (turn_index) as today (pipeline order = time order).
2. NEW `packages/va_pathways/src/va_pathways/call_graphs.py`:
   - `async def load_call_graph(database, org_id, pathway_id: uuid.UUID, version: int) -> tuple[ActiveGraph, bool]`
     (bool = frozen, i.e. stored version status != "draft"; reuse `loader._graph_of`, raise
     `PathwayUnavailableError("not_found"|"version_not_found"|"invalid_graph")` like `DbPathwayLoader`; the graph
     parse in `asyncio.to_thread`). Factor the shared body out of `DbPathwayLoader.load` (no duplication: jscpd 0).
   - `class CallGraphCache(database, *, max_entries=128)`: `async get(org_id, pathway_id, version) -> ActiveGraph`
     (LRU `OrderedDict` keyed (org_id, pathway_id, version); caches only frozen versions — drafts are mutable;
     concurrent misses of one key may both load: fine) and `loader(org_id) -> PathwayLoader` (an object whose
     `load(pathway_id, ref)` resolves `production`/`staging` pins through `DbPathwayLoader` each time and numbers
     through `get`).
3. `packages/va_engine/src/va_engine/pathway_call.py` additions:
   - `ENGINE_STATE_TTL_S: Final = 7200`, `engine_state_key(call_id) -> str` = `f"call:{{{call_id}}}:engine"`
     (same hash-tag form as `CallContextStore`'s `call:{<id>}:ctx`; check its builder and mirror it).
   - `opener_text(active, context, *, now) -> str | None`: the start node of `start_state(active, context)`; only a
     Default node (`type == "Default"`, not global) with non-blank `text` → rendered exactly like
     `handlers.base.speak_node` (same TemplateScope the engine builds for the node: find where `TurnContext.render`
     gets its scope in engine.py and factor a pure `node_scope(state, facts, node)`-style function used by both, so
     the text is byte-identical to the engine's `Say`). `None` otherwise / blank render. `context.config.
     wait_for_greeting` → `None` (the greeting may change the first reply: same rule as `render_opener`).
   - `PathwayBrain.turn_tags(role: Literal["user","assistant"]) -> TurnTags | None` (in pathway_brain.py; dataclass
     `TurnTags(pathway_turn: int, node_id: str | None, node_name: str | None)`): `None` once the engine ended for
     `user`; `user` → `state.turn_index + 1` and the current node (the pipeline's observer sees a routed user turn
     BEFORE the brain accepts it; every routed turn — greeting, DTMF, superseding — is accepted next);
     `assistant` → `state.turn_index` and the node of the last assistant `TurnRecord` in `state.history` (fallback
     current node); node names via `engine.navigator.graph.node(id).data.name`.
4. `apps/media_worker/src/va_media/turns.py`: `TurnRecorder(..., tagger: TurnTagger | None = None)` (Protocol with
   `turn_tags(role)`); user/agent rows get `pathway_turn`/`node_id`/`node_name` from it, and the `call.turn` event
   data `node_id` (today hard-coded `None`). New method `engine_event(event_type: str, data) -> None` publishing
   through the same ordered chain (releasing a held assistant turn first, like other callbacks); unknown types (not
   an `EventType` value) are logged `media.engine_event_unknown` and dropped. Messages: NEW
   `va_media/pathway_events.py` `event_message(event_type, data) -> str` (fixed, PHI-free, e.g. `call.route` →
   "Pathway routed to <to_node>"; `call.extraction` → "Variables extracted"; `call.loop_condition` → "Loop
   condition scored"; `call.webhook_node` → "Webhook node executed"; `call.sms_sent` → "SMS sent"), node ids only.
   `CallPersistence.recorder(..., tagger=)` passes it through.
5. NEW `apps/media_worker/src/va_media/pathway_runtime.py`:
   - `EngineStateSaver(redis, call_id, *, clock)` — `on_state(state)` sync: queues one ordered background SET
     (`dump_engine_state`, EX `ENGINE_STATE_TTL_S`), latest wins; `aclose()` awaits the chain; RedisError logged
     `media.engine_state_save_failed` (never raised).
   - `RecorderEvents(recorder | None)` implementing `EngineEvents.emit` → `recorder.engine_event` (no recorder →
     dropped).
   - `CallSmsPort` implementing `va_engine.ports.SmsPort` for voice calls: refactor the send core out of
     `va_media.voicemail_sms.VoicemailSms` (sender lookup `va_db.voicemail_sms.sms_sender` for `from_` or the
     call's agent number, `queue_outbound_message`, Telnyx `send_message`, `record_sent`/`record_failed`) into a
     shared helper both use; the SMS port does NOT publish `call.sms_sent` (the engine's report does), the voicemail
     path keeps its event. Message id `uuid7` per request. Never raises → `SmsOutcome(sent=False, error=<reason>)`.
   - `async def build_pathway_brain(context, org, *, graphs, gateway, counter, tools_http, allow_http, clock,
     sms, events, on_state) -> PathwayBrain` (raises `PathwayUnavailableError`): `graphs.get(org_id, pathway_id,
     pathway_version)` (both required for runner `pathway`; missing → `PathwayUnavailableError("not_found")`),
     `start_state`, `DialogueSettings.for_call(context, org)`, `PathwayPrompt`, `EnginePorts(decider=Decider(gateway,
     clock=), dialogue=GatewayDialogue(gateway, prompt), lines=GatewayLinePort(gateway, org_id=, call_id=),
     events=events, loader=graphs.loader(org_id), http=HttpExecutor(tools_http, clock=, allow_http=,
     call_id=str(call_id)), sms=sms)`, `facts=pathway_facts(context, now=clock.now())`.
6. `apps/media_worker/src/va_media/runtime.py`:
   - `CallRuntime.__init__(…, graphs: CallGraphCache | None = None, tools_http: httpx.AsyncClient | None = None)`.
   - `__call__`: accept runner_type `task` and `pathway` (others still `media.runner_unsupported`); pathway without
     `graphs`/`tools_http` → error end logged.
   - `_converse`: brain = TaskBrain (task) or `await build_pathway_brain(...)` (pathway; `PathwayUnavailableError` →
     log `media.pathway_unavailable` reason → `ERROR_END`). `stack.push_async_callback(brain.aclose)` for the
     pathway brain and the saver's `aclose` (order: brain.aclose before saver flush, before persistence.finish_call
     — mind AsyncExitStack LIFO). `_bind_transfer` typed against a small protocol (both brains have
     `on_transfer_failed`). Recorder built with `tagger=brain` for pathway calls (recorder creation currently precedes
     brain: reorder or set the tagger after construction — prefer building the brain first).
   - Result: `session_end(result)`, then for pathway calls `dataclasses.replace(end, variables=brain.variables)`.
   - `open_runtime`: `MediaSettings` mixes in `WebhookSafetySettings` (no new env var; already in specs/02 §6 for
     api/worker — verify, and add `media` to that row's service list if the table lists services);
     `tools_http = tools_client(settings)` closed on exit; `graphs = CallGraphCache(database)`;
     `allow_http = va_pathways.runs.insecure_urls_allowed(settings.va_env)`; SMS port factory needs telnyx +
     database + `settings.telnyx_messaging_profile_id` like VoicemailSms.
   - Module docstring step list updated.
7. `apps/media_worker/src/va_media/session.py`: `SessionEnd.variables: Mapping[str, JsonValue] | None = None`.
   `apps/media_worker/src/va_media/call_end.py::_record`: when `end.variables` is non-empty, in the same transaction
   as `record_answered_end`, `UPDATE calls SET variables = variables || :vars` (jsonb concat; a CallRepo method
   `merge_variables(call_id, variables)` in va_db) — also when the stored reason wins. Thread `end` to `_record` as
   needed.
8. `apps/media_worker/pyproject.toml`: depend on `va-pathways` and `va-tools` (uv lock refresh: `uv lock`).
9. Worker prefetch (opener): `packages/va_workflows/src/va_workflows/activities/prerender.py`:
   `OpenerPrerender(…, graphs: PathwayGraphSource | None = None)` where the source is
   `va_pathways.call_graphs.load_call_graph`-backed (protocol `async get(org_id, pathway_id, version) ->
   ActiveGraph`; worker container passes a `CallGraphCache(database)`); new `async pathway_opener(context, now) ->
   str | None` (`opener_text(...)`; any `PathwayUnavailableError`/`SQLAlchemyError` → None, logged
   `prerender.pathway_opener_failed` reason). `call_context.py`: for `runner_type == "pathway"` set
   `opener = await prerender.pathway_opener(context, now)` instead of `opener_for` — the stored
   `CallContext.opener_text` then gets rendered/cached by the existing `render()`; the PathwayBrain never reads
   `opener_text`, its start `Say` hits the phrase cache because the text is identical (pronounced via
   `cache_text`). Update `va_engine.opener` docstring line 5 accordingly. Worker container: build the cache.
10. AGENTS.md line (P07-T12b).

### Tests
- `packages/va_db/tests`: migration 0009 up/down (`migration_helpers`), schema/spec parity tests pass (DDL column
  last), `CallTurnRepo` round-trips `pathway_turn`; `call_object` test: pathway turns grouped by `pathway_turn`
  (two assistant rows with pipeline indices 2 and 3 sharing pathway_turn 1 → one log entry with joined text).
- `packages/va_pathways/tests/test_call_graphs.py`: frozen version cached (second `get` issues no query — count via
  a wrapped database or by deleting the row and still getting it), draft never cached (update draft nodes → next get
  sees new graph), LRU eviction at max_entries, unknown/deleted/other-org/invalid → `PathwayUnavailableError`
  reasons; `loader(org)` resolves production pin.
- `packages/va_engine/tests/test_pathway_call.py` (or extend test_pathway_brain.py): `opener_text` equals the text
  of the engine's first `Say` for the same graph/context/now (property-ish over a few templates incl. variables and
  request_data); None for prompt-only start, non-Default start, wait_for_greeting, blank render; `engine_state_key`;
  `turn_tags` for user/assistant across a routed turn and after end.
- `apps/media_worker/tests/test_turns.py` (existing recorder tests): tagger fills rows + `call.turn` node_id;
  `engine_event` ordered after held assistant turn; unknown type dropped.
- `apps/media_worker/tests/test_pathway_runtime.py` (unit): `EngineStateSaver` writes latest state with TTL,
  survives broken redis; `CallSmsPort` success / no sender / Telnyx error → outcomes (reuse voicemail SMS test
  fakes/rows `sms_rows`); voicemail SMS tests still pass after the refactor.
- `apps/media_worker/tests/test_call_end.py`: variables merged (existing keys kept, new ones overwrite) for an
  ended pathway call; none → column untouched.
- Runtime sim (acceptance) `apps/media_worker/tests/test_runtime_pathway_sim.py` using `runtime_rig.sim_runtime`
  (+ `graphs=CallGraphCache(db)`, `tools_http`), `persistence=` + `media_db_url`, seeded org/pathway via
  `pathways_helpers.store_bella_pasta` (va_pathways tests support; add the support dir to the media pytest
  pythonpath/mypy_path if needed) or a small graph: start (static text) → caller says → FakeLLM router rule → Webhook
  node to `webhook_receiver.served_receiver` (resolver/allow_private as in va_tools tests; WEBHOOK_ALLOW_PRIVATE true
  for the rig) → End Call node with text. Assert: receiver got the request with rendered body; `call_events` has
  `call.route` (+ `call.webhook_node`, `call.extraction` if the graph extracts) with `turn_index`; `call:{id}:engine`
  in Redis with `ended` true; `calls.variables` contains extracted/webhook-set vars; `load_call_objects` →
  `pathway_logs` equal to `brain.pathway_logs()` ignoring `created_at` (capture the brain via a recording
  wrapper or compare against expected entries); end reason `agent_end_call` (check `session_end` mapping of
  `agent_hangup`). Second test: missing pathway version → session end `error`, no TTS. Third: opener pre-render
  path — phrase cached start text → `tts.started` has no context for the opener (FakeElevenLabs REST record).
- Worker: `packages/va_workflows/tests` prerender test: pathway call context gets `opener_text` = start node text;
  load failure → None + still saves context.

### Gates
`make fmt lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones — the voicemail SMS refactor and loader factoring are the risk), `uvx radon cc -s -a` on changed src,
`make openapi` drift check (Call Object unchanged → no drift expected), `make test` in background with Monitor
heartbeat (~25 min; truncate log first; OOM → `PYTEST_ADDOPTS="-n 6"`). `make e2e` NOT required here (T12c adds the
pathway flows) but this touches the call flow: run the existing `outbound_basic` flow on a rebuilt stack
(`scripts/dev_up.sh e2e` then `uv run pytest tests_e2e -m e2e -k outbound_basic -s`) to prove task calls still work.

### Risks
- Turn-tag timing (observer before brain): verify with the runtime sim comparison of stored vs engine pathway_logs;
  if a barge-in path tags the interrupted agent turn with the next index, adjust `turn_tags` (not the projection).
- AsyncExitStack ordering: brain.aclose (settles background extractions which emit events) must run BEFORE
  `persistence.finish_call` drains the recorder, and the saver flush after brain.aclose.
- Webhook node on the sim rig needs private addresses allowed (`WEBHOOK_ALLOW_PRIVATE`/resolver), never real DNS.
- Draft-version calls (test calls) must never be served stale from the per-process cache.
- Media container now needs no new secrets; confirm `test_container`/lifespan tests close the new tools client.

## Build P07-T12b (2026-11-04)

Implemented per plan, with these deliberate deviations (recorded in the specs/11 §10 Decision P07-T12b):
- Final variables do NOT travel in `SessionEnd`: a carrier hangup cancels the session body (no result), so the
  end routine (`CallEnd._merge_variables`, step 2, before record) reads the `call:{id}:engine` snapshot written by
  `EngineStateSaver` (flushed in the body's exit stack before the ender runs) and calls `CallRepo.merge_variables`.
- Agent turn tags come from the line the brain last handed to the pipeline (`PathwayBrain._mark_spoken`), not the
  last history record: a `SayStream` is recorded only after the pipeline resumes the generator.
- `TurnRecorder.tagger` is a settable attribute (recorder and brain need each other); `build_pathway_brain` sets it.
- The brain is built BEFORE vendor sockets open (unloadable version → ERROR_END, no TTS socket); the recorder's
  `finish_call` is now registered first for all runners, then saver.aclose, then brain.aclose (LIFO exit order:
  brain closes → snapshot flushed → events drained).
- `PathwayTools` is a frozen dataclass (graphs, tools_http, allow_http) passed as `CallRuntime(pathways=)`.
- Worker: `OpenerPrerender.opener_for_call` (async; pathway → `opener_text` over `CallGraphCache`), called by
  `prefetch_call_context`.

Gates so far: make fmt/lint/typecheck 0, placeholders 0, spec refs 0, openapi drift clean, jscpd 0 clones,
radon avg 2.5 (max B 9 opener_text). Targeted suites green incl. the runtime sim acceptance
(`test_runtime_pathway_sim.py`: 3 tests). Full `make test` running.

Final gates (P07-T12b): first full `make test` run had 2 failures caused by this change, both fixed: the
`call_turns` spec column list in `test_call_models.py` (+ `pathway_turn?`) and the old
`test_non_task_runner_is_refused...` in test_runtime_sim (pathway is now supported). That test was split into a
persona refusal test (no org read) and a pathway-without-PathwayTools test (`not_configured`). Rerun of
`make test`: 12612 passed, 7 skipped, coverage 99.09%. pip-audit (workspace excluded) clean; pnpm audit 3
moderate, 0 high. jscpd 0, radon avg 2.5. e2e: stack rebuilt with `scripts/dev_up.sh e2e`, then
`pytest tests_e2e -m "e2e or capacity"`: 18 passed (outbound_basic, voicemail, transfer, barge-in, capacity,
pathway_regression_gate, ...). Not committed: the Reviewer commits.

## Review P07-T12b (2026-11-04) — approved
Gates re-run: lint/typecheck/placeholders/spec refs pass, jscpd 0 clones. make test: 12611 passed, 1 failed —
`test_runtime_sim.py::test_caller_turn_is_answered_by_the_task_brain` asserted `va_audio_underruns_total == 0`
and got 1 under xdist CPU load (realtime sim). That assertion dates from P05-T17e1 and the task path did not change;
the test passes 3/3 alone. Logged as a fix task, not a blocker. e2e (`scripts/dev_up.sh e2e` + `-m "e2e or capacity"`):
18 passed. Diff checked: exit-stack order (brain.aclose → saver flush → finish_call), drafts never cached,
tenant_session for graph loads and the variables merge, PHI-free event messages, spec Decisions added.

## Planner 2026-11-04 (after P07-T12b approval)
Next unticked: P07-T12c (last task of phase 07; after it the Planner runs the phase exit gate).

## Plan P07-T12c — e2e flows pathway_restaurant_template and webhook_node

Facts verified by the planner:
- No dashboard templates exist yet (`apps/dashboard/src/templates` arrives in phase 17, specs/25 §3), and the only
  restaurant fixture (`packages/va_engine/tests/fixtures/bland/restaurant_ordering.json`) is an ordering flow with
  KB/Custom Code nodes (phase 08). So the "bundled" reservation template is NEW and lives at
  `tests_e2e/pathways/restaurant_template.json` (tests_e2e is not scanned by the placeholder gate, so `.test`
  hosts are fine). Record that in the Decision.
- Validation rejects a templated netloc in a webhook URL (`validation._webhook_url_ok`; warning on draft, error on
  publish), so the test rewrites the Webhook node's `url` to the receiver URL before creating the pathway.
- Router/extraction prompts (`va_llm/prompts/router.txt`, `extract.txt`) do NOT contain the call prompt, so a
  `{{marker}}` cannot scope decision rules. Routing uses FakeLLM's label-word heuristic (`select_path` overlap)
  or `alwaysPick`; extraction uses a FakeLLM rule `match: {tool_name: extract_variables, last_user_regex: <unique
  name>}` (rule ids carry `{{run}}`). All agent lines are static text, so no dialogue LLM rule is needed.
- The media worker runs Webhook nodes and must reach `host.docker.internal`: `docker-compose.e2e.yml` only gives
  `api`/`webhooks` `extra_hosts`. VA_ENV in compose is local → `insecure_urls_allowed` → http allowed.
- `WebhookReceiver` answers status only (`Reply(status)`); webhook_node needs a JSON body.
- Event log items (`GET /v1/calls/{id}/events`) carry `type` and `data` (CallEventItem).

### Files
1. `docker-compose.e2e.yml`: add
   ```yaml
   media:
     extra_hosts: ["host.docker.internal:host-gateway"]
     environment:
       WEBHOOK_ALLOW_PRIVATE: "true"
   ```
   and extend the header comment (media runs pathway Webhook nodes against the receiver). Update
   `scripts/tests/test_compose.py` (expected override dict ~line 239, and the extra_hosts equality ~line 679) and the
   specs/29 §2 e2e-override Decision text (one sentence, same commit).
2. `tests_e2e/support/e2e_receiver.py`: `Reply(status: int = 200, body: JsonValue | None = None)`; `handle` answers
   `JSONResponse(body, status_code=...)` when body is not None, else the plain `Response`. Starlette route stays
   POST-only (webhook nodes POST). Add `Received.json()` helper (orjson/json loads of body) if convenient.
   Unit test in `tests_e2e/unit/test_e2e_webhooks.py` (or test_e2e_support.py — wherever receiver tests live):
   scripted JSON reply body + content-type, default reply unchanged.
3. `tests_e2e/pathways/restaurant_template.json` (Bland camelCase, prettier-formatted, with `global_config`
   `{"globalPrompt": "You are Ava, the reservation assistant of Bella Pasta."}`):
   - `greet` Default, `isStart`, name "Greeting", text "Thank you for calling Bella Pasta, this is Ava. How can I
     help you today?"
   - `reserve` Default, name "Take Reservation", text "Of course. What name should I put the reservation under?",
     `extractVars` party_size (integer, "Number of guests"), time (string, "Reservation time as the caller said
     it, for example 7pm"), name (string, "Full name for the reservation"), all required, `extractVarsMode`
     `before_response`.
   - `book` Webhook, name "Book Table", `webhook`: method POST, url `https://reservations.bella-pasta.test/bookings`
     (rewritten by the test), body `{"party_size": "{{party_size}}", "time": "{{time}}", "name": "{{name}}"}`,
     `responseData` status `$.status`, ref `$.ref`, `routeOnStatus` `{"2xx": "goodbye", "4xx": "goodbye",
     "5xx": "goodbye", "timeout": "goodbye"}` (no text/prompt → routes at once).
   - `goodbye` End Call, name "Goodbye", `endCall.speech` "You are all set, {{name}}. Your table for {{party_size}}
     at {{time}} is booked. Goodbye!", speechMode static.
   - Edges: `e_reserve` greet→reserve label "Caller wants to book a table reservation"; `e_book` reserve→book with
     `alwaysPick: true` (check the exact edge field name in `va_engine.models.graph` Edge data — `alwaysPick`);
     `e_bye` greet→goodbye label "Caller says goodbye" (keeps the template realistic; unused).
   Verify it with `validate_graph(mode="publish")` via a tests_e2e unit test (`tests_e2e/unit/test_e2e_pathways.py`:
   load the JSON, apply the URL rewrite helper, `load_graph` + `validate_graph(..., mode="publish",
   refs=None, allow_http=True)` → no errors; the rewrite touches only Webhook nodes).
4. `tests_e2e/support/e2e_pathways.py` additions:
   - `PATHWAYS_DIR`, `load_template(name) -> dict` (nodes/edges/global_config), `with_webhook_url(graph, url) ->
     dict` (deep copy, replaces `data.webhook.url` of every Webhook node),
     `webhook_graph(url) -> dict` for the webhook_node flow (below).
   - `PathwayApi.create(name, graph)` must pass the graph's own `global_config` when present (today it forces `{}`).
   - `PathwayApi.published(name, graph) -> str` = create + publish (asserts 200), returns pathway id.
   - `route_targets(events) -> list[str]` = `data["to_node"]` (check the real key in `va_engine` route event
     data, `handler_route_data`/router event) of `type == "call.route"` events in seq order.
   - `pathway_call(harness/org, scenario, pathway_id, **extra) -> dict` body: `phone_number` scenario number,
     `from` org number, `pathway_id`, `wait_for_greeting: false`, `record: false`.
5. Scenarios (`tests_e2e/scenarios/`), `answer: {after_ms: 1500, result: answered, answered_by: human_residence}`,
   `profile: fast` (or whatever the default fast profile is called; check other scenarios), steps with
   `wait_agent_speech {min_ms: 300, then_silence_ms: 700, timeout_ms: 8000}` between `say`s, final
   `expect_hangup: {by: agent, timeout_ms: 20000}`:
   - `pathway_restaurant_template.yaml` + `rules/pathway_restaurant_template.yaml`: says "Hi, I would like to book
     a table for 4 at 7pm." then "It is under Marisol Quintana." Rule `restaurant-extract-{{run}}` priority 10
     `match: {tool_name: extract_variables, last_user_regex: "(?i)marisol quintana"}` → `tool_calls: [{name:
     extract_variables, arguments: {party_size: 4, time: "7pm", name: "Marisol Quintana"}}]`. Assertions block:
     call_status completed, answered_by human, end_reason agent_end_call (verify the real mapping from P07-T12b:
     runtime `agent_hangup` → `agent_end_call`).
   - `webhook_node.yaml` (no rules file): says "Yes, please confirm my booking." then "Great, thank you, goodbye."
6. `tests_e2e/test_pathway_restaurant_template.py` (`pytestmark = [e2e, timeout(600)]`, fixtures `harness`,
   `receiver`): org → `load_scenario("pathway_restaurant_template")` → template with url
   `f"{receiver.url}/bookings/{harness.run_token}"` (path unique per run) → `published(...)` →
   `send_call(pathway body)` → `wait_complete` → assert:
   - scenario assertions via the harness evaluator (as other flows do);
   - `route_targets(events) == ["reserve", "book", "goodbye"]` (adjust only if the engine also emits a route for
     the start node; then pin the exact observed list including it — never loosen to a subset);
   - Call Object `variables` ⊇ `{"party_size": 4, "time": "7pm", "name": "Marisol Quintana"}` (plus status/ref
     from the receiver's default empty reply → none; the receiver answers JSON `{"status": "booked", "ref":
     "R7"}` scripted for that path, so also `status`/`ref` present);
   - receiver got exactly one POST at that path whose JSON body is `{"party_size": 4, "time": "7pm", "name":
     "Marisol Quintana"}`;
   - one `call.extraction` event and one `call.webhook_node` event (status_code 200, outcome ok);
   - End Call speech played: `harness.tts_texts()` contains "You are all set, Marisol Quintana. Your table for 4
     at 7pm is booked. Goodbye!" and the sim summary/timeline shows hangup by agent after it (scenario
     `expect_hangup by agent` covers it).
7. `tests_e2e/test_webhook_node.py`: graph from `webhook_graph(url)`:
   - `start` Default isStart text "Hi, this is Ava from Bella Pasta. Shall I confirm your booking?"
   - `confirm` Webhook name "Confirm Booking" POST `{url}`, body `{"call": "{{call_id}}"}` (check the built-in name
     for the call id in `va_engine.builtins`; any built-in works), `responseData` status `$.status`, ref `$.ref`,
     `text` "Your booking is {{status}}, reference {{ref}}." (no routeOnStatus → waits for the user after speaking).
   - `bye` End Call speech "Goodbye!"
   - edges start→confirm label "Caller wants the booking confirmed", confirm→bye label "Caller says goodbye".
   Receiver scripted `Reply(200, {"status": "confirmed", "ref": "A1"})`. Assert: TTS texts contain "Your booking is
   confirmed, reference A1." (the agent utterance right after the webhook — also check the assistant turn in the
   Call Object transcripts follows the first user turn); `call.webhook_node` event data `status_code == 200`,
   `outcome == "ok"`, `variables_set` ⊇ {status, ref}; route targets `["confirm", "bye"]`; receiver got one POST;
   call completed, end_reason agent_end_call.
   Shared assertions (ok/route_targets/webhook event lookup) go in e2e_pathways to keep jscpd at 0.
8. specs/30 §5 Decision (2026-11-04) P07-T12c under the flows table: template location + URL rewrite, extraction
   via FakeLLM rule keyed on tool name + caller phrase (decision prompts carry no scenario marker), routing via
   label heuristic / alwaysPick, receiver JSON replies, media `extra_hosts`. AGENTS.md line (P07-T12c).

### Tests / gates
- `uv run pytest tests_e2e/unit scripts/tests/test_compose.py -q`.
- `make fmt lint typecheck placeholders`, spec refs, jscpd 0 clones (tests_e2e is scanned), prettier on the JSON.
- Rebuild stack `scripts/dev_up.sh e2e` (compose override changed → media must be recreated), then
  `uv run pytest tests_e2e -m e2e -k "pathway_restaurant_template or webhook_node" -s`, then full
  `make e2e` (this is the phase exit gate flow set anyway). `make test` in background with Monitor heartbeat.

### Risks
- Engine behaviour on entering `reserve` from greet: confirm it speaks reserve's text and waits (not alwaysPick
  chaining immediately). If alwaysPick chains on entry, instead give `e_book` a label ("Caller gave the name for
  the reservation") and add a `select_path` FakeLLM rule keyed on `last_user_regex: marisol quintana` that picks
  `book` (check the select_path argument names in `va_engine.decision_schemas`).
- Extraction runs on `reserve` with the whole history; the rule matches the latest caller line only.
- `wait_for_greeting: false` is required, else the first caller line would be treated as the greeting.
- FakeElevenLabs STT returns the scripted text; "7pm" may come back as "7 pm" or "7pm" — irrelevant because the
  extraction rule fixes the values, but the label-overlap route on "book a table" must still hold.

## Build P07-T12c (2026-11-04)
Implemented per plan. Files: docker-compose.e2e.yml (media extra_hosts + WEBHOOK_ALLOW_PRIVATE), test_compose.py,
specs/29 §2 Decision text, specs/30 §5 Decision P07-T12c, e2e_receiver `Reply(status, body)` + `Received.json()`,
e2e_pathways (load_template, with_webhook_url, webhook_graph, pathway_call, route_targets, events_of,
webhook_node_data, run_pathway_call, `ok`, PathwayApi.published; create keeps the graph's global_config),
tests_e2e/pathways/restaurant_template.json, scenarios + rule, test_pathway_restaurant_template.py,
test_webhook_node.py, unit tests test_e2e_pathways.py + JSON reply unit test. Observed route targets exactly
reserve, book, goodbye (no route event for the start node). Restaurant flow passes record=false, so its order check
drops call.recording_ready from the required set.
Gates: fmt/lint/typecheck/placeholders 0, spec refs 0, jscpd 0 clones; both flows green on a rebuilt stack (45 s).
make test: 12619 passed, 2 failed — (a) mine: test_e2e_support rule-file invariant required the scenario marker
in every rule; narrowed to allow unmarked rules only when keyed on a decision tool + last_user_regex (fixed, tests_e2e/unit
105 passed); (b) known flaky task-brain underrun test (existing task fix:runtime-sim-underrun), passes alone.
Coverage 99%. make e2e (rebuild): 18 passed incl. pathway_restaurant_template + webhook_node, 2 failed
(concurrent_calls api 500 tenant_session timeout under 50 pollers on the cold stack; inbound_basic FakeLLM default
reply right after) — both passed on immediate rerun; logged as fix:e2e-cold-concurrent.

## Review P07-T12c (2026-11-04) — approved
Re-ran: lint, typecheck, placeholders, spec refs pass; jscpd 0 clones; make test 12621 passed, 7 skipped, 99.09%.
e2e on the running stack: pathway_restaurant_template, webhook_node, pathway_regression_gate pass; outbound_basic
failed once in that batch and passed 3/3 alone afterwards (no src change in this diff) → logged as
fix:e2e-outbound-basic-flaky. Diff checked: compose override + test_compose + specs/29 text in lockstep, specs/30
Decision P07-T12c, receiver JSON replies unit-tested, rule-file invariant narrowed to decision tools only.
Next: Planner runs the phase 07 exit gate (make check && make e2e).

## Planner 2026-11-04 — phase 07 exit gate run 1: FAILED
Ticked the parent P07-T12 (a–c done). Ran `make check` then `make e2e` (logs/exit_gate.log, logs/exit_e2e.log):
- make check: 12620 passed, 1 failed, 7 skipped. The failure was `test_runtime_sim.py::test_caller_turn_is_answered_by_the_task_brain`,
  where `va_audio_underruns_total` was 4 (expected 0). This is the known wall-clock flake (task fix:runtime-sim-underrun).
- make e2e: 18 passed, 2 failed:
  - concurrent_calls: 8/50 calls ended `error`. Worker logs show `hangup reason=stream_failed`. The sim logged
    `sim.telnyx_media_connect_failed TimeoutError('timed out during opening handshake')` for exactly 8 streams at
    13:49:17.7. Root cause: the SIM container logged NOTHING from 13:49:12.4 to 13:49:17.6 (its event loop was
    blocked ~5 s while 50 calls started). In the same window the media worker's STT connects (2 s open timeout)
    and TTS connects (5 s) to the sim timed out, and one sim webhook POST hit WriteTimeout. Prime suspect:
    `CallerScript._prepare` runs `_render_all` (caller utterance synthesis) via `asyncio.to_thread` for every
    call at once. 50 synth jobs in the default thread pool hold the GIL and starve the sim loop. The jobs are
    likewise cold on a fresh container. (Not yet confirmed by profile.)
  - inbound_basic (ran right after): answered 1455 ms after initiated (> 1000). Probably the same loaded-sim
    window; re-check after the fix.

## Plan P07-FIX-1 — make the phase 07 exit gate pass

### Part A: deterministic underrun wiring test (closes fix:runtime-sim-underrun)
`apps/media_worker/tests/test_runtime_sim.py::test_caller_turn_is_answered_by_the_task_brain` asserts zero underruns on
a wall-clock realtime sim, which depends on CPU load (xdist). The test's real purpose is that the runtime wires the
pipeline's `on_underrun` hook to `va_audio_underruns_total`. Change it to read `before = registry.get_sample_value(...)
or 0.0` after the call, call `hooks[0]()`, and assert `== before + 1`. Do NOT assert an absolute zero there. Zero
underruns under load is covered by the e2e capacity flow and the accelerated-clock pipeline tests (check that a
harness test with `harness(on_underrun=...)` asserts no underruns on the AcceleratedClock; if none exists, add one in
packages/va_voice/tests (a normal reply turn on the harness → on_underrun never called)).

### Part B: sim event-loop stall when many calls start (closes fix:e2e-cold-concurrent)
1. Measure first. `scripts/dev_up.sh e2e` (fresh rebuild), then py-spy on the sim container during
   `uv run pytest tests_e2e -m capacity -s` (AGENTS.md "Profile a compose service", target `voiceagents-sim-1`,
   `--nonblocking --gil`, ~15 s starting at dispatch). Confirm which frames hold the GIL during the stall
   (expected: va_sim.audio synthesis under `caller_script._render_all`; other candidates: TTS `chunks`, STT
   `feed`, first-use imports/caches).
2. Fix in the sim (not by raising timeouts or adding retries in e2e). Preferred, if synthesis is the cause: render
   caller clips in a `concurrent.futures.ProcessPoolExecutor` owned by `SimState` (`max_workers` from a new
   `SimSettings.sim_render_processes`, default `min(4, os.cpu_count())`). Start it at app startup and shut it down
   in the lifespan / `SimState.aclose`. Submit with `loop.run_in_executor(pool, _render_all, jobs)`. Jobs must be
   picklable: `functools.partial` of module-level functions with plain args (check `_text_job` builds; replace
   lambdas/bound methods). Keep `hold_clock`. Unit tests and the accelerated clock keep working: the in-process
   fallback (`pool=None` → `asyncio.to_thread`) is the default for `build_state` in tests, and the served sim
   (`apps/sim`) passes the pool. Alternative if profiling shows Python-level overhead dominates instead (e.g.
   per-sample loops): vectorise that code with numpy, keeping the output byte-identical (the committed
   `assets/audio` + marker decode tests guard it). New env var = specs/02 §6 table row + `.env.example` + the
   `> Decision` note (specs/30 §3) in the same commit.
3. Regression test (packages/va_sim/tests or apps/sim/tests): with the pool, rendering N=20 caller scripts'
   jobs concurrently keeps the loop responsive. Measure the max gap of a 10 ms ticker task running alongside;
   assert it is < 200 ms. Use a real thread/process pool, not wall sleeps for sync. If timing-sensitive under xdist,
   assert instead that rendering happens in another process (`os.getpid()` of the job ≠ test pid) and that the
   loop task completes ticks while a gate-blocked render is pending.
4. Re-measure: rebuild the stack, run `make e2e` FIRST run (twice: rebuild → e2e, rebuild → e2e). Concurrent_calls
   0 failed scenarios, inbound_basic answer ≤ 1000 ms. If inbound_basic still fails, diagnose it separately from
   the timeline (edge `call.initiated` → answer command latency in edge logs).

### Part C: outbound_basic flaky after pathway batch (fix:e2e-outbound-basic-flaky)
It passed in this exit-gate run. If it does not reproduce in the two e2e runs above, record that in the task's
description (`ralph tools task` reopen/close with the note) and leave it out of scope.

### Gates
make fmt lint typecheck placeholders, spec refs, `make test` (background + Monitor heartbeat), jscpd 0, radon,
audits; `scripts/dev_up.sh e2e` + `make e2e` twice from a fresh rebuild. AGENTS.md learning line (P07-FIX-1).

### Risks
- ProcessPool + uvloop + fork in the sim container: use the `spawn` (or `forkserver`) context to avoid forking a
  process that runs threads. Warm the pool at startup (submit a no-op per worker) so the first calls don't pay
  the spawn/import cost.
- `SimState` shared state (`SIM_STATE_REDIS_URL`) is unaffected: rendering is pure.
- Do not change the synthesized bytes (marker decode in FakeElevenLabs STT depends on them).

## Review P07-FIX-1 (2026-11-04) — rejected
The build.done payload was empty, and the scratchpad has no `## Build P07-FIX-1` section. The diff touches only
`apps/media_worker/tests/test_runtime_sim.py` (Part A) plus the plan and scratchpad.
1. Evidence is missing. The payload has none of the required `tests:/lint:/typecheck:/audit:/coverage:/duplication:/complexity:`
   strings, and no `make check` or `make e2e` result is recorded. The plan's acceptance requires `make check` green plus
   `scripts/dev_up.sh e2e` followed by `make e2e` green on the FIRST run after a rebuild, twice.
2. Part B was not delivered: no sim change, no regression test, no specs/30 §3 Decision. The logs show partial work.
   `logs/fix1_cap1.log` is a capacity run after a rebuild (0 failed, overhead p50 93 ms). `logs/fix1_sub1.log` has 5 flows
   passing. `logs/prof/sim_gil.txt` is a py-spy profile in which synthesis frames appear (28 lines match
   `_render_all|synthes`). Pick one of two outcomes and record it with evidence:
   (a) the stall reproduces or the profile confirms synthesis holds the GIL. Then implement the planned fix (process pool,
       or byte-identical vectorisation) with the regression test.
   (b) the stall no longer reproduces. Then write the profile analysis (top GIL frames during call start) into the
       scratchpad, run the two full rebuild→`make e2e` cycles, and close or annotate fix:e2e-cold-concurrent with the result.
3. Tasks fix:runtime-sim-underrun, fix:e2e-cold-concurrent and fix:e2e-outbound-basic-flaky are still open. Part A looks
   correct. It is covered by `packages/va_voice/tests/pipeline/test_pipeline_underruns.py::test_on_time_playback_reports_no_underrun`.
   Close fix:runtime-sim-underrun once `make test` passes. Part C needs its note.
4. AGENTS.md has no P07-FIX-1 learning line. One is required if Part B finds something non-obvious, such as the
   py-spy-on-sim procedure or the result.
Tip: run `make test` / `make e2e` in the background with a Monitor heartbeat. The empty event looks like an
inactivity-timeout kill (AGENTS.md P05-T10d).

## Build P07-FIX-1 (2026-11-04, after review rejection)
Part A: `test_caller_turn_is_answered_by_the_task_brain` now asserts that the hook increments the catalog counter by one
(before + 1) and no longer asserts an absolute zero. On-time playback with zero underruns is covered by
va_voice `test_pipeline_underruns.py::test_on_time_playback_reports_no_underrun` (accelerated clock).
Part B outcome (b): the stall did not reproduce, and the profile does not support synthesis as the cause. No sim code change.
- Profile `logs/prof/sim_gil.txt`: py-spy `--nonblocking --gil`, 40 s, running on the sim during a 50-call capacity run
  after a rebuild. 1152 samples (412 errors). Main-thread idle (`Runner.run` leaf) = 228. Inclusive counts:
  `MediaSession._tick_loop` 213, `_tick` 128, websockets send 82, `_receive_loop` 43, STT `SttStream.feed` 41,
  caller synthesis `_render_all`/`synthesize_pcm16` 41 (~3.6 %), `ulaw_decode` 29. Synthesis is a minor GIL
  holder spread over the start burst, not a multi-second blocker. The cost is the per-call 20 ms tick/WS send work.
- The original "sim logged nothing for 5 s" was weak evidence. The sim logs sparsely: in the cycle-1 run, gaps between
  consecutive sim log lines reached 111 s, 62 s, 38 s, and so on, with no failure. The real signals are
  `sim.telnyx_media_connect_failed` and the media worker's connect timeouts. Each run below has 0 of them.
- Runs: `logs/fix1_cap1.log` (capacity after rebuild: 50 calls, 0 failed, overhead p50 93 / p95 126 ms, 0 underruns,
  loop lag p99 <= 25 ms); `logs/fix1_e2e1.log` (`make e2e`: 20 passed, exit 0); `logs/fix1_e2e2.log`
  (`make down` then `make e2e`, fully cold stack, sim started 14:13:48: 20 passed, exit 0, including concurrent_calls
  and inbound_basic). The exit-gate failure (8/50 media handshake timeouts) remains unexplained but did not recur in
  3 runs. With no evidence for the planned process-pool change, I left it out.
Part C: outbound_basic passed in the exit-gate run and in both cycles above (3/3 full suites), so it does not reproduce.
Gates: make fmt 0; make check exit 0 (lint, typecheck "no issues in 1563 files", placeholders 0, 12621 passed /
7 skipped, coverage 99.09 %, TS 22+52+48 passed, openapi drift current, spec refs 0); jscpd 0 clones; radon avg
2.60; pip-audit no known vulns; pnpm audit 3 moderate (none high). AGENTS.md line P07-FIX-1 added.

## Review P07-FIX-1 (2026-11-04) — approved
Re-ran `make check`: exit 0. It covers lint, typecheck, placeholders (0), 12621 passed / 7 skipped, coverage 99.09 %, the TS tests, OpenAPI drift and spec refs (0).
Re-ran a cold `make down && make e2e`: 20 passed, exit 0. That includes concurrent_calls, inbound_basic, pathway_restaurant_template and webhook_node,
which makes this the third consecutive green full suite. Diff: the Part A test now asserts hook→counter wiring (before + 1), and the absolute-zero
check is covered on the accelerated clock by va_voice test_pipeline_underruns. Part B outcome (b) is backed by the logs (fix1_e2e1/2, cap1) and the profile analysis.
Tasks fix:runtime-sim-underrun, fix:e2e-cold-concurrent and fix:e2e-outbound-basic-flaky are closed. The AGENTS.md line has been added.
Next: the Planner re-runs the phase 07 exit gate and prints LOOP_COMPLETE once it passes. Still open (P2): fix:e2e-interrupt-level1, which did not fail in these runs.
