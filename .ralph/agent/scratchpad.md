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
