# Scratchpad — phase 07

## Plan P07-T11b — Unit test CRUD, from_call, runs read API

Spec: specs/12 §2 (endpoint table), §8 (create body, from_call), §10, §11; specs/03 `pathway_unit_tests`,
`pathway_test_runs` (models already exist: `va_db.models.PathwayUnitTest`, `PathwayTestRun`; no migration needed).
`POST /tests/run` is NOT part of this task (T11c), but route ordering must leave room for it.

### Decisions to record (specs/12 §8, new block `> Decision (2026-11-04): unit test CRUD and runs read API (P07-T11b).`)
1. Permissions: reads (`GET tests`, `GET tests/{id}`, `GET runs`, `GET runs/{id}`) need `pathways:read` (viewer,
   analyst, developer, admin, owner). Writes (create/patch/delete/from_call) need `pathways:write` WITHOUT
   `require_pathway_editor`: specs/04 §3.2 row "Pathway tests / simulations" gives analysts `X` (and
   `rbac_matrix.MATRIX` is drift-checked against it); this supersedes the analyst "–" for "tests" in §10. (Chat keeps
   its editor guard from P07-T10.) Billing: no access (no pathways scope).
2. Audit actions are `pathway_test.create`, `pathway_test.update`, `pathway_test.delete` (two-part names, like
   `pathway_folder.*`; §11 `pathway.test.*`). from_call writes `pathway_test.create` with `after.source_call_id`.
   Before/after = test view without `history` (store `history_messages` count) to keep rows small.
3. Every route first loads the live pathway (`PathwayRepo(session, org).get(pathway_id)`, 404 "Pathway not found.");
   a test/run of another pathway (same org) is 404 like another org's.
4. Body limits: `name` = `pathways_schemas.Name` (1-200 trimmed); `node_id` 1-200 chars; `history` ≤ 200 messages,
   role `user`|`assistant` (Literal; anything else 400 INVALID_INPUT field `history[i].role`), content ≤ 4 000;
   `user_message` 1-4 000 after strip; `expected` parsed with `va_pathways.unit_tests.Expected` but extra keys
   forbidden (subclass in api schemas `ExpectedBody(Expected)` with `extra="forbid"`, `grading_goal` ≤ 2 000,
   `route_to_node_id` ≤ 200); stored as `model_dump(mode="json", exclude_unset=True)`; `simulated_variations`
   0-20 default 5 (DB default). `node_id` is NOT checked against any version (runs report `unknown_node`).
5. PATCH: only sent fields; a `null` value leaves the field unchanged; `expected` replaces the whole object. No-op
   patch (nothing changes) writes no audit row (AGENTS: "Write no row for a no-op").
6. DELETE is a hard delete (table has no `deleted_at`), answers `{"status":"success","message":"Test deleted."}`.
7. List tests / list runs: cursor pagination `created_at DESC, id DESC`, `limit` 1-100 default 50, `cursor`;
   runs also `kind` filter (`unit|simulation|regression`, else 400). Run list items omit `results` and carry
   `summary {total, passed, failed, error}` counted from `results[*].status` (items without a known status count
   only in total); run detail carries `results` + `summary`.
8. from_call body `{call_id, turn_index (≥0), name, expected?, simulated_variations?}`:
   - call loaded with `call_read.org_call(session, org_id, call_id)` (CALL_NOT_FOUND 404);
   - `call.pathway_id != pathway_id` → 400 INVALID_INPUT field `call_id` "The call did not run this pathway.";
   - turns = `call_turns` of the call ordered by `turn_index` (indices are unique across roles: the pipeline shares
     one counter); the target is the `role='user'` row with `turn_index`; missing → 400 INVALID_INPUT field
     `turn_index` "The call has no caller turn with this index.";
   - history = every earlier turn as `{role, content}` where assistant content = `text_spoken` when not null (what
     the caller actually heard), else `text`;
   - node_id = target `node_id`, else the latest earlier turn with a `node_id`; none → 400 INVALID_INPUT field
     `turn_index` "The turn has no pathway node.";
   - expected = body `expected` when sent, else `{"route_to_node_id": <node_id of the first later assistant turn
     with one>}` when that exists, else `{}`; `source_call_id` = call id; user_message = target text.

### Files
- `packages/va_db/src/va_db/repositories/pathway_tests.py` (new), exported from `repositories/__init__.py`:
  ```python
  TEST_RESOURCE: Final = "Pathway test"; RUN_RESOURCE: Final = "Pathway test run"
  class PathwayTestRepo:
      def __init__(self, session, org_id, *, clock=None)
      async def page(self, pathway_id, *, limit, cursor) -> Page[PathwayUnitTest]
      async def get(self, pathway_id, test_id) -> PathwayUnitTest          # NotFoundError(TEST_RESOURCE)
      async def add(self, pathway_id, values: Mapping[str, Any]) -> PathwayUnitTest
      async def change(self, test: PathwayUnitTest, values) -> PathwayUnitTest  # setattr + flush + refresh
      async def delete(self, test: PathwayUnitTest) -> None
  class PathwayRunRepo:
      async def page(self, pathway_id, *, kind: PathwayTestRunKind | None, limit, cursor) -> Page[PathwayTestRun]
      async def get(self, pathway_id, run_id) -> PathwayTestRun            # NotFoundError(RUN_RESOURCE)
  ```
  Pagination: reuse `TenantRepository(Model, ...).list(where=[Model.pathway_id == pid, ...])` internally (it
  already does cursor/limit validation and ordering); get via `TenantRepository.get` then compare `pathway_id`.
  T11c will add run creation/status updates to `PathwayRunRepo`.
- `packages/va_db/src/va_db/audit.py`: add the three actions to `AUDIT_ACTIONS`; `specs/04` §13 list (line ~1263)
  gets `pathway_test.create`, `pathway_test.update`, `pathway_test.delete`.
- `apps/api/src/va_api/routers/pathway_tests_schemas.py` (new): `HistoryItem`, `ExpectedBody`, `TestCreate`,
  `TestUpdate`, `TestFromCall`, `TestView` (`test_id, pathway_id, name, node_id, history, user_message, expected,
  simulated_variations, source_call_id, created_at, updated_at`), `TestOne {status, test}`, `TestList {status,
  tests, next_cursor}`, `TestDeleted {status, message}`, `RunSummary`, `RunListItem` (`run_id, pathway_id,
  version_number, kind, status, summary, started_at, finished_at, created_at`), `RunDetail(RunListItem)+results`,
  `RunOne {status, run}`, `RunList {status, runs, next_cursor}`; `test_view(row)`, `run_item(row)`,
  `run_detail(row)`, `run_summary(results)`. Every model needs an `examples` entry (test_operations_contract);
  request bodies' first example must be valid for the cross-tenant suite.
- `apps/api/src/va_api/services/pathway_tests.py` (new): `create_test(session, ctx, pathway_id, body)`,
  `list_tests(...)`, `get_test`, `update_test(session, ctx, pathway_id, test_id, values)`, `delete_test`,
  `test_from_call(session, ctx, pathway_id, body)`, `list_runs`, `get_run`; pure `history_from_turns(turns,
  target_index) -> list[dict]`, `node_for_turn(...)`, `observed_route(...)` (unit-testable). Audit via
  `va_api.audit.audit(session, ctx, "pathway_test.create", "pathway_test", str(test.id), after=...)`.
  NOTE: never name a src function `test_*` at module level if a test imports it (pytest collects imported
  `test_*` callables!) → name it `capture_from_call` instead.
- `apps/api/src/va_api/routers/pathway_tests.py` (new): routes on `pathways_router` (side-effect import in
  `va_api/main.py` next to `pathway_versions`). Declare literal `/tests/from_call` BEFORE `/tests/{test_id}`.
  Operation ids: `pathways_tests_list`, `pathways_tests_create` (201? keep 200 like folders create),
  `pathways_tests_get`, `pathways_tests_update` (PATCH), `pathways_tests_delete`, `pathways_tests_from_call`,
  `pathways_runs_list`, `pathways_runs_get`. openapi_extra `{"x-scope": ..., **audited(...)}`.
- `make openapi`, then `corepack pnpm --filter dashboard gen:api` and `corepack pnpm --filter @voiceagents/sdk
  gen:api` (schema.d.ts drift tests).
- `apps/api/tests/support/cross_tenant_factories.py`: factories `test_id` (pathway + unit test row of the tenant;
  check how `version_number` chains on the pathway factory — the test must belong to the pathway in the URL, so
  the factory probably has to create its own pathway; look at FactoryContext for already-created ids) and
  `run_id` (a `PathwayTestRun` row, kind unit, status passed); `register_request("pathways_tests_from_call", ...)`
  + `register_owner_status(..., 404)` if the owner control cannot use a real call (or seed a call in a factory).
- AGENTS.md: one dated line for P07-T11b.

### Tests
- `packages/va_db/tests/test_pathway_test_repos.py` (DB, `migrated_url`/scratch fixtures as other repo tests):
  page ordering + cursor + kind filter, get of other pathway → NotFound "Pathway test not found.", hard delete,
  RLS other org.
- `apps/api/tests/test_pathway_tests_api.py` (module Postgres via `session_helpers.migrated_postgres`, `star_api`):
  create → get → list (pagination with limit=1 + cursor) → patch (name, expected replace, null ignored) → delete →
  404; validation 400s (bad role field `history[0].role`, blank user_message, variations 21, unknown expected key);
  deleted pathway → 404; test of pathway A requested under pathway B → 404; audit rows (`audit_rows(identity, org,
  action="pathway_test.create")`, none for no-op patch); runs list/detail with seeded `PathwayTestRun` rows
  (summary counts, kind filter, bad kind 400, other pathway's run 404).
- `apps/api/tests/test_pathway_tests_from_call.py`: seed call (`call_rows.seed_call(..., pathway_id=...)`) + turns
  (`CallTurn` rows via `pathway_helpers.add`): opener assistant 0 (node start, text_spoken differs from text),
  user 1, assistant 2 (node order), user 3 → from_call turn 3 → history 3 msgs with spoken text, node from
  fallback, route expected from following assistant; turn_index of an assistant turn → 400 field turn_index;
  other pathway's call → 400 field call_id; other org's call → 404 CALL_NOT_FOUND; no node anywhere → 400;
  explicit `expected` wins.
- Pure unit tests of `history_from_turns`/`node_for_turn`/`observed_route`/`run_summary`.
- `apps/api/tests/test_pathways_permissions.py`: `test_unit_test_role_matrix` parametrized owner/admin/developer/
  analyst (write ✓), viewer (read ✓, write 403), billing (read 403).
- Cross-tenant suite + audit markers + openapi contract tests pass automatically once factories exist.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest apps/api packages/va_db -q -n 8` while iterating, then
`make test` (background + Monitor heartbeat, ~25 min), `make test-ts` (schema drift), `make openapi` drift via
`make check`'s spec refs/openapi gate, jscpd, radon.

### Risks
- Cross-tenant factory chaining for `test_id`/`run_id` under `/v1/pathway/{pathway_id}/…`: inspect
  `cross_tenant.py` `_create(chain)` to see whether a later factory receives the pathway id; if not, factory must
  create pathway+row and the registry must allow the shared pathway (look at `version_number` precedent).
- `audit` redact replaces keys containing `key`/`token`: test view keys are safe; `expected.mock_tools` bodies may
  hold secrets → leave `expected` out of audit before/after (record `expected_keys` list instead).
- openapi `pathways_tests_update` PATCH on `pathways_router` — audit marker test requires `audited`.

## Build P07-T11b (2026-11-04)
Implemented per plan. Deviations: schema classes named `UnitTest*` (not `Test*`, pytest collection); service
function `capture_from_call` + pure `capture_turn(TranscriptTurn...)`; `expected` rejects unknown keys via
`ExpectedBody(Expected)` extra=forbid; audit `update` after-dict adds `fields`. Cross-tenant: `test_id`/`run_id`
factories, `pathways_tests_from_call` owner status 404 (example call id unknown). Spec Decision added under specs/12 §8;
audit actions added to specs/04 §13 + `AUDIT_ACTIONS`. jscpd 0 clones (extracted `assert_reads` in permissions test),
radon avg A (1.98).

## Review P07-T11b (2026-11-04)
Approved. Re-ran: make lint/typecheck/placeholders pass; make test 12353 passed, 7 skipped, 99% coverage (exit 0);
make test-ts pass; openapi drift current; spec refs 0 problems; jscpd 0 clones. Diff matches specs/12 §2/§8 +
Decision P07-T11b; analyst write access matches specs/04 §3.2 "Pathway tests / simulations"; audit actions added to
specs/04 §13 and AUDIT_ACTIONS; every route loads the live pathway under tenant_session. Minor nit (not blocking):
schemas docstring cites Decision items "4, 7, and 8" while the Decision has 7 items (body rules = 4, from_call = 7).

## Plan P07-T11c — `POST /tests/run` and `PathwayTestWorkflow`

Spec: specs/12 §2 (`POST /v1/pathway/{pathway_id}/tests/run`), §8 run paragraph + Decision P07-T11a (runner) +
Decision P07-T11b (CRUD/read); specs/01 §1 (worker queues). Runner already exists: `va_pathways.unit_tests.run_unit_test`.
No migration (`pathway_test_runs` exists).

### Decisions (new block under specs/12 §8: `> Decision (2026-11-04): unit test runs (P07-T11c).`)
1. Body `{"version": "draft"|"production"|"staging"|int ≥ 1 (default "draft"), "test_ids": [uuid] | null}`. The
   version resolves at request time to a number with `services.pathways.load_version` (unset pin / missing number
   → `PATHWAY_VERSION_NOT_FOUND`, as everywhere); the run stores `version_number` and every case loads that
   number (a draft edited mid-run is read as stored when each test starts).
2. `test_ids` null → every test of the pathway, oldest first (`created_at, id`); a list → those, deduplicated in
   order, at most 500 items (pydantic `max_length=500`); an id that is not a test of this pathway → 404
   "Pathway test not found." (Decision P07-T11b item 3). No tests (empty list or a pathway without tests) → 400
   `INVALID_INPUT` field `test_ids` "There are no unit tests to run."; more than 500 tests without ids → 400 field
   `test_ids` "Select at most 500 tests." Permission: `pathways:write`, no editor guard (analysts run tests,
   specs/04 §3.2). Mutation marker: `audit_exempt("test runs are recorded as pathway_test_runs rows")`.
3. The request inserts `pathway_test_runs(kind='unit', status='queued', results=[])`, commits, then starts
   `PathwayTestWorkflow` id `pathway-test-run-<run_id>` on the post-call queue (`postcall_task_queue(
   TEMPORAL_TASK_QUEUE)`, `REJECT_DUPLICATE`; an existing workflow counts as started). Start failure → the run is
   set `error` with `finished_at` and the request answers 503 `SERVICE_UNAVAILABLE` "Test runs are unavailable;
   try again." Success answers 200 `{"status":"success","run":<run detail>}` (the existing `RunOne` shape, status
   `queued`). `Idempotency-Key` works through `require_scope` like every POST.
4. Workflow: activity `begin_pathway_test_run` (queued → running + `started_at`; returns False when the run row is
   gone or no longer queued/running → workflow ends), then one `run_pathway_unit_test` activity per test with at
   most 8 in flight (`asyncio.Semaphore(8)` inside the workflow), then `finish_pathway_test_run`. Each test
   activity writes its result item into `results` itself (row locked `FOR UPDATE`, an item with the same `test_id`
   replaced, so a retry never duplicates) and returns only the item's status (keeps big results out of Temporal
   history). Finish orders `results` by the run's test order, sets `status` (`failed` when any item failed, else
   `error` when any errored, else `passed`) and `finished_at`.
5. Per-test errors become items, never workflow failures: a test deleted since the run started → `{"test_id",
   "name": null, "passed": false, "status": "error", "cases": [], "duration_ms": 0, "error": "test_not_found"}`; a
   version that no longer loads (pathway deleted, graph no longer parses) → same shape with the test's name and
   `error: "version_unavailable"`; an activity that exhausted its retries (infrastructure) → the workflow
   records `error: "runner_error"` for that test through `record_pathway_test_error` (same upsert). Timeouts:
   test activity start-to-close 30 min (21 cases × 60 s cap + grading), retry 3 attempts (initial 5 s, ×2,
   non-retryable none); begin/finish/record 30 s, 5 attempts.

### Files
- `packages/va_domain/src/va_domain/pathway_tests.py` (new, import-free besides pydantic, workflow-safe):
  `PATHWAY_TEST_WORKFLOW_NAME: Final = "PathwayTestWorkflow"`, `RUN_CONCURRENCY: Final = 8`,
  `class PathwayTestWorkflowInput(BaseModel)` (frozen: `run_id: uuid.UUID, org_id: uuid.UUID, pathway_id: uuid.UUID,
  version_number: int, test_ids: list[uuid.UUID]`, `workflow_id()` → `f"pathway-test-run-{run_id}"`),
  `run_status(statuses: Iterable[str]) -> Literal["passed","failed","error"]` (shared by finish + tests).
- `packages/va_db/src/va_db/repositories/pathway_tests.py` add:
  `PathwayTestRepo.ordered_ids(pathway_id, *, limit) -> list[uuid.UUID]` (oldest first, `limit+1` rows so the
  caller can detect > 500), `PathwayTestRepo.find(pathway_id, ids) -> dict[uuid, PathwayUnitTest]`;
  `PathwayRunRepo.add(pathway_id, *, version_number, kind) -> PathwayTestRun` (status queued),
  `PathwayRunRepo.lock(run_id) -> PathwayTestRun | None` (`SELECT … FOR UPDATE`, org-scoped),
  `PathwayRunRepo.begin(run_id, now) -> bool`, `put_result(run_id, item) -> bool` (replace same `test_id`, assign a
  NEW list so SQLAlchemy sees the JSONB change), `finish(run_id, order: Sequence[str], now) -> PathwayTestRunStatus | None`,
  `fail(run_id, now)` (status error + finished_at). Use `run_status` from va_domain.
- `packages/va_pathways/src/va_pathways/runs.py` (new): `insecure_urls_allowed(va_env)` MOVED here from
  `va_api.services.pathways` (api re-exports/imports it from here) and
  `async def run_stored_test(deps: TextDeps, database: Database, org_settings: OrgSettingsReader, *, org_id,
  pathway_id, version_number, test_id, counter) -> Item` = load the test row (tenant session) → `test_not_found`
  item; load graph via `DbPathwayLoader(database, org_id).load(str(pathway_id), version_number)` →
  `PathwayUnavailableError` → `version_unavailable` item; else `UnitTestSpec.model_validate` of the row (history,
  user_message, expected, simulated_variations) and `run_unit_test(...).item()`; plus `error_item(test_id, name,
  error)`. Keep the DB session closed while the test runs (pool size!).
- `packages/va_workflows/src/va_workflows/pathway_tests/` (new package like `post_call/`): `__init__.py` exports
  `PathwayTestWorkflow`; `workflow.py` (`@workflow.defn(name=PATHWAY_TEST_WORKFLOW_NAME)`, `run(input) ->
  PathwayTestRunResult` (pydantic: `run_id`, `status | None`, `tests: int`); activities scheduled by name;
  `ActivityError` from a test → `record_pathway_test_error`).
  `va_workflows/activities/pathway_tests_inputs.py` (workflow-safe: `RunRef(run_id, org_id)`, `TestRunInput(run_id,
  org_id, pathway_id, version_number, test_id)`, `FinishInput(run_id, org_id, test_ids)`, `TestErrorInput(...,
  error)`, timeouts/retry policies). `va_workflows/activities/pathway_tests.py`: `class PathwayTestActivities(*,
  database, org_settings: OrgSettingsReader, text: TextDeps, counter_factory=default_counter)` with
  `@activity.defn` methods `begin_pathway_test_run`, `run_pathway_unit_test`, `record_pathway_test_error`,
  `finish_pathway_test_run` (token counter loaded once via `asyncio.to_thread(default_counter)` and cached).
  `registration.py`: `POSTCALL_WORKFLOWS = (PostCallWorkflow, PathwayTestWorkflow)`.
  `packages/va_workflows/pyproject.toml`: add `va-pathways`, `va-llm` deps (+ `[tool.uv.sources]`), `uv lock`.
- Worker: `WorkerSettings` mixes in `LlmSettings` (va_llm.settings) and `WebhookSafetySettings` (specs/02 §6
  already scopes LLM_*/WEBHOOK_ALLOW_PRIVATE to worker). `build_container` builds an `Envelope(kms_provider,
  OrgDataKeyStore(database))`, an `LlmGateway` exactly like `va_api.container` (ProviderRegistry(platform_provider,
  OrgProviderStore), `http_client(settings, timeout=...)`, `CostTable(load_costs(...))`, `LlmMetrics` on a private
  `CollectorRegistry()` unless the worker catalog has the llm metrics), `tools_client(settings,
  resolver=system_resolver)` (closed on exit), `TextDeps(..., allow_http=insecure_urls_allowed(settings.va_env))`.
  `WorkerActivities` gains `pathway_tests: PathwayTestActivities | None = None` (build kwarg `text: TextDeps |
  None`) and `postcall_queue()` appends its 4 activities when set. If extracting the gateway build into a shared
  helper avoids a jscpd clone with the api container, put `llm_gateway_for(settings, database, envelope, http,
  clock, metrics)` in `va_llm` (or va_pathways) and use it from both.
- API: `apps/api/src/va_api/services/pathway_test_runs.py` (new): `PathwayTestStarter` protocol +
  `TemporalPathwayTestStarter(connector, task_queue)` (va_common.checks.start_workflow; `RunStartError`) and
  `async def start_unit_run(deps, ctx, pathway_id, body) -> PathwayTestRun`. Container field
  `pathway_test_starter` (postcall queue from `settings.temporal_task_queue`). Router route in
  `routers/pathway_tests.py`: `POST /{pathway_id}/tests/run`, operation id `pathways_tests_run`, declared BEFORE
  `/{pathway_id}/tests/{test_id}` routes (a POST on `/tests/{test_id}` does not exist, but keep literal first);
  schema `UnitTestRunRequest` in `pathway_tests_schemas.py` (first example `{"version":"draft","test_ids":null}`).
  `make openapi` + both `gen:api`.
- Tests support: `apps/api/tests/support/call_fakes.py` (or new `pathway_fakes.py`) `FakePathwayTestStarter`
  (`.started: list[PathwayTestWorkflowInput]`, `.fail`), field on `SessionContainer`. Cross-tenant: the route
  probes with the example body; owner control on a pathway without tests answers 400 → `register_owner_status(
  "pathways_tests_run", 400)` (or register a request with the factory's test id if the factory creates one).
- AGENTS.md dated line; specs/12 §8 Decision.

### Tests
- `packages/va_domain/tests/test_pathway_tests.py`: `run_status` matrix, workflow id.
- `packages/va_db/tests/test_pathway_test_repos.py` (extend): add/begin (only from queued)/put_result replace
  same test_id/finish ordering + status/fail; `ordered_ids` order + limit; `find` ignores other pathway's tests.
- `packages/va_pathways/tests/test_runs.py` (DB `pathways_db_url` + `text_rig()` FakeLLM): stored passing test
  item; deleted test → `test_not_found`; deleted pathway → `version_unavailable`; failing route → status failed.
- `packages/va_workflows/tests/test_pathway_test_workflow.py` (time-skipping `workflow_env()`, fake activities
  with production names in `tests/support/pathway_test_rig.py`): all pass → passed and finish called with order;
  one failed → failed; begin False → no tests run; a test activity raising non-retryable `ApplicationError` →
  `record_pathway_test_error(runner_error)`; 20 tests never exceed 8 concurrent (fake counts max in flight with an
  event gate).
- `packages/va_workflows/tests/test_pathway_test_activities.py` (Postgres + FakeLLM): begin/run/finish on real
  rows via `ActivityEnvironment`, results persisted, retry of the same test does not duplicate.
- `apps/worker/tests`: `postcall_queue()` lists the 4 activities and POSTCALL registration includes the
  workflow; container settings tests still green (new settings mixins have defaults).
- `apps/api/tests/test_pathway_tests_run.py`: starts workflow with resolved version + all ids oldest first;
  explicit ids dedup + order; other pathway's test id → 404; no tests → 400 field test_ids; unset production
  pin → PATHWAY_VERSION_NOT_FOUND; starter failure → 503 and run row `error`; returned run is queued and readable
  via `GET /runs/{id}`; idempotent replay returns same run; analyst may run, viewer 403 (extend
  `test_pathways_permissions.py`).

### Gates
`make fmt lint typecheck placeholders`, targeted pytest (`apps/api packages/va_workflows packages/va_pathways
packages/va_db apps/worker -n 8`), `make test` in background with Monitor heartbeat, `make test-ts`, openapi drift,
spec refs, jscpd, radon.

### Risks
- Temporal sandbox: workflow module must import only va_domain/inputs modules inside
  `imports_passed_through()`; `asyncio.Semaphore` in a workflow is fine (deterministic loop).
- Activity DB tests need real time for nothing here (no call_events), FrozenClock ok.
- Worker container now needs LLM env; defaults point at the sim, so compose works unchanged.
- jscpd: gateway construction duplicated between api and worker containers → share a helper.

## Build P07-T11c (2026-11-04)
Implemented per plan. Deviations: activity input classes named `UnitTestInput`/`UnitTestErrorInput` (no src
`Test*` classes); `PathwayTestWorkflowInput.test_ids` is a tuple (1-500); per-test error `invalid_test` added
(stored row no longer validates); `record_pathway_test_error` keeps an item an earlier attempt stored
(`put_result(replace=False)`) and a failing record yields outcome `unrecorded` (the finish counts a test without
an item as an error). Shared `va_llm.platform.platform_gateway` now builds the api, media, and worker gateways.
The worker exposes `va_llm_*` on its probe registry (specs/27 §3.6 "any process that builds a client").
`insecure_urls_allowed` moved to `va_pathways.runs`. Cross-tenant: `pathways_tests_run` owner status 400 (factory
pathway has no tests). Spec Decision P07-T11c under specs/12 §8; AGENTS.md line.
Gates: fmt/lint/typecheck/placeholders pass; make test run 1 aborted (INTERNALERROR: media
test_wait_for_greeting_holds_the_opener timed out 60 s under load; module alone 31 passed); run 2: 12381 passed,
1 failed (worker test_worker_call_flow: Temporal ephemeral test server did not start within 5 s; alone passed),
coverage 99.07%; make test-ts pass; openapi drift current; spec refs 0; jscpd 0 clones; radon avg A (2.31);
pip-audit clean, pnpm audit no high. No e2e: the run path has no call/SMS flow (no e2e flow for test runs yet).

## Review P07-T11c (2026-11-04)
Approved. Re-ran: make lint/typecheck/placeholders pass; make test 12382 passed, 7 skipped, 99% cov (exit 0, no
flakes this run); make test-ts pass; openapi drift current; spec refs 0; jscpd 0 clones. Diff matches specs/12 §8
(concurrency 8, post-call queue, queued→running→passed/failed/error) and Decision P07-T11c; every DB access in
tenant_session; result items stored by activities with row lock, workflow history holds only statuses.
