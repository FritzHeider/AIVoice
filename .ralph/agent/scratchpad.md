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

## Plan P07-T11d — Simulations (LLM-as-caller)

Spec: specs/12 §9 (simulations paragraph), §2 (`POST /v1/pathway/{pathway_id}/simulations`), §10 permissions
(analysts allowed: specs/04 §3.2 "Pathway tests / simulations"), Decisions P07-T11a/b/c under §8. No migration
(`pathway_test_runs.kind` already allows `simulation`). Reuses `PathwayTestWorkflow` (same id scheme, same queue,
same starter) with a `kind` switch.

### Decisions (new block under specs/12 §9: `> Decision (2026-11-04): simulations (P07-T11d).`)
1. Body `{"version": "draft"|"production"|"staging"|int ≥ 1 (default draft), "scenarios": [1..20 items],
   "runs_per_scenario": 1..10 (default 1)}`, other keys rejected. Scenario `{name 1-200, persona 1-4000,
   goal 1-2000, max_turns 1..50 (default 20), request_data object (default {})}`, extra forbidden. Version resolves
   at request time (`load_version`, as unit runs). Permission `pathways:write`, no editor guard, audit-exempt
   (the run row is the record), `Idempotency-Key` replays. Inserts `pathway_test_runs(kind='simulation',
   status='queued')`, commits, starts `PathwayTestWorkflow`; start failure → run `error` + 503 (same as unit runs).
   Answer 200 `{"status":"success","run":<run detail>}`.
2. Jobs: scenario i (1-based) × run r (1-based) → `simulation_id` `"<i>-<r>"`, scenario order then run order;
   at most 8 in flight (RUN_CONCURRENCY). Results ordered by job order at the finish.
3. One run: fresh state (`new_pathway_state`) at the start node, request data = scenario `request_data`,
   simulated caller/number like the test chat (`TextSetup` defaults; `conversation_id` = new uuid per run, also the
   `call_id` of every LLM call of the run incl. the caller's, so FakeLLM `call_index` counts per run). Agent
   opens (`engine.start()`), then loop: caller LLM (call type `analysis`, temperature 0.7, max_tokens 256, system =
   `persona + "\n\n" + CALLER_SUFFIX` where CALLER_SUFFIX = `Reply only with what you would say on the phone. Say
   "[HANGUP]" when you would hang up.` verbatim; conversation from the caller's view: agent lines `user`, caller
   lines `assistant`; an agent turn that said nothing is shown as `(silence)`). Reply containing `[HANGUP]`
   (case-sensitive token): the text before it, stripped, is kept as the last caller line when non-blank and is
   NOT sent to the engine; the run ends `ended_by: "hangup"`. Blank caller reply (no token) → sent as nothing?
   No: a blank reply ends the run with `error: "caller_invalid"`. Otherwise the line is a `handle_user_turn`
   (UserTurn index = state.turn_index + 1, start/end_ms = elapsed ms of the clock). Each engine run capped 60 s
   (`CASE_TIMEOUT_S`). Stop after the agent's answer to caller turn `max_turns` (`ended_by: "max_turns"`) or when
   the engine state ended (End Call / transfer terminal → `ended_by: "agent_ended"`).
4. Grading (only when the conversation ran without error): one forced `grade` call (analysis, existing
   `GradeVerdict`), messages = transcript (Caller/Agent lines) + `Final variables: <json sorted keys>` then
   `SIMULATION_GRADE_PROMPT` = `Given the full conversation and the final variables, did the conversation achieve
   this goal? Goal: "{goal}". Be strict.` (goal substituted literally). Status: verdict pass → `passed`, verdict
   fail → `failed`; errors (`engine_error`, `turn_timeout`, `caller_unavailable`, `caller_invalid`,
   `grading_unavailable`, `version_unavailable`, `runner_error`) → `error`.
5. Result item: `{"simulation_id", "scenario", "run", "goal", "passed", "status", "ended_by":
   "hangup"|"max_turns"|"agent_ended"|"error", "turns" (caller lines sent to the engine), "transcript": [{"role":
   "assistant"|"user", "content"}] (agent = assistant, caller = user, in order, blank agent turns omitted),
   "variables", "grading": {pass, rationale}|null, "actions" (simulated actions, chat shape), "duration_ms",
   "error"?}`. Error items for runs that could not start: same keys with `transcript: []`, `turns: 0`,
   `ended_by: "error"`, `variables: {}`, `grading: null`, `actions: []`, `duration_ms: 0`, `error`.
   `run_summary` counts `status` as for unit runs.
6. Workflow: `PathwayTestWorkflowInput` gains `kind: Literal["unit","simulation"] = "unit"` and
   `simulations: tuple[SimulationJob, ...] = ()`; a model validator requires 1..500 `test_ids` and no simulations
   for `unit`, 1..200 simulations and no test_ids for `simulation` (`test_ids` default `()`, drop field-level
   min_length). Activity `run_pathway_simulation` (timeout 60 min start-to-close: 50 turns × (60 s + caller), retry
   = TEST_RETRY) stores its item itself and returns the status; failure after retries →
   `record_pathway_simulation_error` (replace=False, `runner_error`). `FinishInput` gains `simulation_ids:
   tuple[str, ...] = ()`; order = test ids then simulation ids. Repo results are keyed by `test_id` or
   `simulation_id` (`_item_key`).

### Files
- `packages/va_domain/src/va_domain/pathway_tests.py`: `MAX_SCENARIOS = 20`, `MAX_RUNS_PER_SCENARIO = 10`,
  `MAX_SIMULATION_TURNS = 50`, `DEFAULT_SIMULATION_TURNS = 20`, `MAX_SIMULATIONS = 200`,
  `RunKind = Literal["unit","simulation"]`, `class Scenario(BaseModel)` frozen extra=forbid (`name`, `persona`,
  `goal`, `max_turns`, `request_data: dict[str, JsonValue]`; constraints as Decision 1), `class SimulationJob`
  (`simulation_id: str`, `scenario_index: int ≥1`, `run_index: int ≥1`, `scenario: Scenario`),
  `simulation_jobs(scenarios, runs_per_scenario) -> tuple[SimulationJob, ...]`, input changes (Decision 6).
- `packages/va_db/src/va_db/repositories/pathway_tests.py`: `_item_key(item) -> str | None` (`test_id` else
  `simulation_id`, as str); `put_result`/`finish` use it (behaviour for unit items unchanged).
- `packages/va_pathways/src/va_pathways/grading.py`: `SIMULATION_GRADE_PROMPT`, `grade_conversation_messages(
  transcript, variables, goal)`, `grade_conversation(port, *, org_id, transcript, variables, goal) ->
  GradeVerdict | None` (share the try/except with `grade` via a private `_verdict(port, org_id, messages)`).
- `packages/va_pathways/src/va_pathways/simulations.py` (new): `CALLER_SUFFIX`, `HANGUP_TOKEN = "[HANGUP]"`,
  `SILENCE = "(silence)"`, `CALLER_TEMPERATURE = 0.7`, `CALLER_MAX_TOKENS = 256`, `EndedBy`, `SimulationError`
  Literal, `caller_messages(persona, transcript) -> list[ChatMessage]`, `parse_caller_reply(content) ->
  CallerReply(text: str, hangup: bool)` (pure), `@dataclass SimulationResult` with `.item()`,
  `simulation_error_item(job, error) -> Item`, `async def run_simulation(deps, job: SimulationJob, *, org_id,
  active, org, loader, counter) -> SimulationResult` (one `TextConversation` per engine run over the carried
  state, collecting `recorder.actions` across runs; never raises for engine/model failures; catches `Exception`
  from engine runs only, like `_converse`; `PathwayUnavailableError` from `new_pathway_state` (no start node) →
  `version_unavailable`). Caller via `deps.gateway.complete(..., call_id=conversation_id)` catching
  `LlmUnavailableError` → `caller_unavailable`. Keep functions small (radon < 10).
- `packages/va_pathways/src/va_pathways/runs.py`: `StoredSimulation(org_id, pathway_id, version_number, job)`,
  `run_stored_simulation(deps, database, org_settings, sim, *, counter) -> Item` (loader + org settings like
  `run_stored_test`; extract a shared `_load_version(...)`/org helper to avoid duplication).
- `packages/va_workflows/src/va_workflows/activities/pathway_tests_inputs.py`: `SIMULATION_TIMEOUT =
  timedelta(minutes=60)`, `SimulationInput(RunRef)` (`pathway_id`, `version_number`, `job: SimulationJob`),
  `SimulationErrorInput(RunRef)` (`job: SimulationJob`, `error: Literal["runner_error"]`), `FinishInput.
  simulation_ids`.
- `packages/va_workflows/src/va_workflows/activities/pathway_tests.py`: activities `run_pathway_simulation`,
  `record_pathway_simulation_error`; `_store` keyed generically (log `item_key`).
- `packages/va_workflows/src/va_workflows/pathway_tests/pathway_test_workflow.py`: branch on `start.kind`;
  `_run_simulation(start, job, slots)` mirroring `_run_test`; outcomes keyed by simulation_id; finish passes
  `simulation_ids`. Update module docstring.
- `apps/worker/src/va_worker/activities.py`: `postcall_queue()` adds the two new activities.
- API: `apps/api/src/va_api/routers/pathway_tests_schemas.py`: `ScenarioBody` (= domain `Scenario` with
  OpenAPI examples/descriptions; may subclass it) and `SimulationRunRequest` (first example = the spec §9 body
  with `"runs_per_scenario": 3`); `apps/api/src/va_api/services/pathway_test_runs.py`: refactor insert/start/fail
  into `_launch(database, starter, clock, ctx, run, start_factory)` used by `start_unit_run` and new
  `start_simulation_run(database, starter, clock, ctx, pathway_id, *, version, scenarios, runs_per_scenario)`;
  route `POST /{pathway_id}/simulations` operation_id `pathways_simulations_create` in `routers/pathway_tests.py`
  (`_WRITE` + `audit_exempt("simulation runs are recorded as pathway_test_runs rows")`). `make openapi` + both
  `gen:api`.
- Spec Decision block (specs/12 §9); AGENTS.md dated line.

### Tests
- `packages/va_domain/tests/test_pathway_tests.py`: `simulation_jobs` ids/order; input validator (unit needs
  test_ids and no simulations; simulation needs jobs, no test_ids; bounds).
- `packages/va_db/tests/test_pathway_test_repos.py`: put_result replaces by `simulation_id`; finish orders
  simulation items and counts missing ones as error.
- `packages/va_pathways/tests/test_simulations.py` (pure): `parse_caller_reply` (token mid/end/only/absent,
  blank), `caller_messages` roles flip + silence + system suffix verbatim, `simulation_error_item` shape.
- `packages/va_pathways/tests/test_simulations_sim.py` (FakeLLM via `text_rig()`, bella_pasta or a small graph):
  caller hangs up after N turns → `hangup`, transcript order, goal passed → `passed`; grader says fail →
  `failed`; caller never hangs up → stops at `max_turns` exactly (count caller turns sent / FakeLLM requests);
  End Call node → `agent_ended`; caller LLM 400 error rule → `error` `caller_unavailable` (no grading request);
  grader 400 → `grading_unavailable`; blank caller → `caller_invalid`; final variables passed to the grader
  message (assert request body contains `Final variables:`). FakeLLM rules: caller rules `system_contains:
  ["[HANGUP]"]` with `call_index`, grader `tool_name: grade`.
- `packages/va_pathways/tests/test_runs.py`: `run_stored_simulation` on DB (`pathways_db_url`): item stored
  shape; deleted pathway → `version_unavailable` error item.
- `packages/va_workflows/tests/test_pathway_test_workflow.py`: simulation kind runs only simulation
  activities, finish gets `simulation_ids` in job order, failed sim → failed, activity failure →
  `record_pathway_simulation_error`, ≤ 8 concurrent.
- `packages/va_workflows/tests/test_pathway_test_activities.py`: `run_pathway_simulation` on real rows stores an
  item; retry replaces not duplicates.
- `apps/worker/tests`: postcall_queue lists the 2 new activity names.
- `apps/api/tests/test_pathway_simulations.py`: starts workflow with kind simulation + jobs `1-1..2-3` for 2
  scenarios × 3 runs, version resolved; defaults (runs 1, max_turns 20, request_data {}); validation errors (0 or
  21 scenarios, runs 11, max_turns 0/51, unknown key, blank persona) → 400 with field path; unset production pin →
  PATHWAY_VERSION_NOT_FOUND; starter failure → 503 + run `error`; run readable via `GET /runs/{id}` with kind
  simulation; `GET /runs?kind=simulation` lists it; idempotent replay same run; analyst 200 / viewer 403 (extend
  `test_pathways_permissions.py`). Cross-tenant suite covers the route via the example body (owner 200).

### Gates
`make fmt lint typecheck placeholders`; targeted pytest `packages/va_domain packages/va_db packages/va_pathways
packages/va_workflows apps/worker apps/api -n 8 -k "pathway or simulation"`; `make test` in background + Monitor
heartbeat; `make test-ts`; openapi drift; spec refs; jscpd; radon. No e2e (no call/SMS flow).

### Risks
- jscpd: `run_pathway_simulation` vs `run_pathway_unit_test` and the workflow `_run_simulation` vs `_run_test`
  look alike → factor a shared `_run_item(activity_name, input, timeout, on_error)` helper in the workflow.
- FakeLLM dialogue rules for the agent must not match the caller (caller system prompt contains `[HANGUP]`; give
  caller rules higher priority).
- `TextConversation.run` acloses the engine every run: carry `conversation.state` into the next conversation
  (same as the chat). `recorder.terminal` marks state ended.
- Temporal payload: 200 jobs × persona (4000 chars) ≈ 1 MB worst case < 2 MB Temporal limit; OK, note in
  Decision.

## Build P07-T11d (2026-11-04)
Implemented per plan. Deviations: `HANGUP_TOKEN` named `HANGUP_MARKER` (ruff S105); `run_simulation` always
returns a `SimulationResult` (`error_result(job, error)` for no start node); `grading_unavailable` keeps the
real `ended_by` (conversation errors use `ended_by: "error"`), recorded in the Decision; grading call carries
the conversation id as `call_id`. Shared helpers to keep jscpd at 0: `pathways_helpers.route_rule/size_rule/
grade_rule/broken_user_turns`, api `pathway_helpers.run_of`. Workflow item dispatch generalized via `_Item`
(activity + error recorder per test/simulation). Spec Decision P07-T11d under specs/12 §9; AGENTS.md line.
Gates: make fmt/lint/typecheck/placeholders pass; make test-ts pass (widget 22, dashboard 52, sdk 48); openapi
drift current (openapi.json + both schema.d.ts regenerated); spec refs 0; jscpd 0 clones; radon avg A (2.92);
pip-audit clean, pnpm audit no high (3 moderate). make test run 1: overloaded by concurrent runs (Docker read
timeouts; all 118 affected tests passed alone). Run 2: 12430 passed, 4 failed, coverage 96.65%; the 4 failures
(sim shared redis, media runtime_sim press_button + interruptibility[4-2], dispatcher two pods) are timing
flakes in untouched modules, 6/6 passed alone. Targeted: va_pathways + api simulations/runs + workflow
activities 97 passed. No e2e: no call/SMS flow touched.

## Review P07-T11d (2026-11-04)
Approved. Re-ran: make lint/typecheck/placeholders pass; make test-ts pass; openapi drift current; spec refs 0;
jscpd 0 clones. make test: 12369 passed, 5 failed + 60 errors, all pytest-timeout/startup timeouts in untouched
modules (edge DB fixture, sim shared redis, media runtime/transfer, api transcript ws) under load; all 95 pass
alone; coverage 96.11%. Targeted pathway/simulation/workflow/api/cross-tenant tests 195 passed. Diff matches
specs/12 §9 + Decision P07-T11d (body bounds, ids `<i>-<r>`, caller prompt suffix verbatim, grading prompt,
item shape); all DB access in tenant_session; items stored by activities.

## Plan P07-T11e1 — Regression settings API and shared publish core

P07-T11e split into e1 (settings + publish core), e2 (async regression run via PathwayTestWorkflow, worker
publishes on pass), e3 (synchronous ≤ 20 tests gate in the api + 409 + e2e). Rationale: each slice is complete
on its own (e2 gates every regression publish asynchronously; e3 adds the synchronous fast path), no interim stub.
e2 note: pathway_test_runs has no run-level metadata column; e2 needs a migration (e.g. `publish jsonb`) for the
intent/outcome, and the intent carries the version's graph_hash + an AuditActor-shaped actor for the worker.

### Decisions (new block under specs/12 §9: `> Decision (2026-11-04): regression settings (P07-T11e1).`)
1. Settings model `{run_on_publish: bool = false, block_publish_on_failure: bool = true, include_simulations: bool
   = false, scenarios: [Scenario] (0-20, the simulation scenario shape of Decision P07-T11d item 1; one
   conversation each, used by P07-T11e2 when include_simulations)}`, other keys rejected. Defaults chosen so an
   existing pathway's publish runs nothing until the suite opts in (spec example shows the opted-in shape).
2. Stored under the draft's `global_config.regression` (save/fork/duplicate/export copy it with the graph; a draft
   update that sends `global_config` replaces it, so clients keep the key; it changes `graph_hash`). Reads are
   lenient: a missing or invalid stored value reads as the defaults (imported graphs may carry anything).
3. `GET /v1/pathway/{id}/regression` (`pathways:read`) → `{"status":"success","regression":{...all four
   keys...}}`. `PUT` (`pathways:write` + editing role, it edits the draft) body = the full settings (omitted keys
   take the defaults) → same body plus `draft_version`, `graph_hash`. Pathway row lock first; storing the same
   settings writes nothing (no audit). Audit `pathway.regression_update` (new two-part action in specs/04 §13 +
   `AUDIT_ACTIONS`), before/after = `{run_on_publish, block_publish_on_failure, include_simulations, scenarios:
   <count>, graph_hash}`.
4. Publish core shared: `va_db.pathway_publish.apply_publish` performs §4 step 3 + audit (freeze `published`,
   pin, fork a draft when the version was the draft, `pathway.publish` audit row) for the api now and the worker
   in P07-T11e2. Validation and cache invalidation stay with the caller.

### Files
- `packages/va_domain/src/va_domain/pathway_tests.py`: `MAX_REGRESSION_SCENARIOS: Final = 20`;
  `class RegressionSettings(BaseModel)` frozen, extra="forbid": fields above (`scenarios: tuple[Scenario, ...] =
  Field(default=(), max_length=MAX_REGRESSION_SCENARIOS)`); `REGRESSION_KEY: Final = "regression"`;
  `load_regression(global_config: Mapping[str, object]) -> RegressionSettings` (value under the key validated,
  `ValidationError`/non-mapping → `RegressionSettings()`); `with_regression(global_config, settings) -> dict[str,
  Any]` (copy + `settings.model_dump(mode="json")` under the key); `regression_summary(settings) -> dict` (audit
  shape without graph_hash). Add to `__all__`.
- `packages/va_db/src/va_db/pathway_publish.py` (new): `@dataclass(frozen=True, slots=True) PublishResult
  (published_version: int, environment: PinEnvironment, draft_version: int, previous: int | None)`;
  `async def apply_publish(session, repo: PathwayRepo, pathway: Pathway, version: PathwayVersion, *, environment,
  published_by: uuid.UUID | None, actor: AuditActor) -> PublishResult` — body moved from
  `va_api.services.pathway_versions.publish_version` (previous pin computed inside; `pinned_number` moves here too
  and the api re-imports it), audit via `va_db.audit.record(session, actor, "pathway.publish", ...)` same
  before/after. Pathway must already be locked by the caller (docstring).
- `apps/api/src/va_api/services/pathway_versions.py`: `publish_version` = load/lock/validate then
  `apply_publish(..., published_by=await creating_user_id(ctx, session), actor=actor_of(ctx))` (import
  `actor_of` from `va_api.audit`); outcome built from `PublishResult` (keep `PublishOutcome` or alias it —
  keep `PublishOutcome` for router stability).
- `apps/api/src/va_api/services/pathway_regression.py` (new): `async def get_regression(session, ctx,
  pathway_id) -> tuple[Pathway, PathwayVersion, RegressionSettings]` (draft, no lock);
  `async def put_regression(session, ctx, pathway_id, settings) -> RegressionOutcome(draft, settings, changed)`
  (lock, draft, `before = load_regression(draft.global_config)`; if the stored raw value already equals
  `settings.model_dump(mode="json")` → unchanged; else `update_draft(pathway, draft, values={},
  graph=PathwayGraphJson(nodes, edges, with_regression(...), graph_hash(load_graph(...)) )` — compute the hash
  the same way the draft update path does (look at `services.pathways` draft update for the helper that builds
  `PathwayGraphJson` from a `GraphInput`; reuse it, do not re-implement hashing) + audit
  `pathway.regression_update`).
- `apps/api/src/va_api/routers/pathway_regression_schemas.py` (new): `RegressionBody` (subclass or mirror of
  `RegressionSettings` with OpenAPI example = spec §9 object + `"scenarios": []`; if subclassing a frozen domain
  model is awkward use `RegressionSettings` directly as the body with `json_schema_extra` example via a thin
  subclass), `RegressionView` (`status`, `regression`), `RegressionSaved` (adds `draft_version`, `graph_hash`),
  both with examples.
- `apps/api/src/va_api/routers/pathway_regression.py` (new, routes on `pathways_router`, side-effect import in
  `va_api/main.py` like `pathway_versions`): `GET /{pathway_id}/regression` operation_id
  `pathways_regression_get` (`x-scope pathways:read`); `PUT /{pathway_id}/regression` operation_id
  `pathways_regression_put`, `dependencies=EDITOR`, `openapi_extra={"x-scope": "pathways:write",
  **audited("pathway.regression_update")}`.
- `packages/va_db/src/va_db/audit.py` `AUDIT_ACTIONS` + specs/04 §13 list: `pathway.regression_update`.
- `make openapi` + `corepack pnpm --filter dashboard gen:api` + `corepack pnpm --filter @voiceagents/sdk gen:api`.
- specs/12 §9 Decision block (above); AGENTS.md dated line.

### Tests
- `packages/va_domain/tests/test_pathway_tests.py`: defaults; unknown key rejected; 21 scenarios rejected;
  `load_regression` on missing / non-dict / invalid / valid; `with_regression` does not mutate input.
- `packages/va_db/tests/test_pathway_publish_db.py` (or extend the existing pathway repo DB test module): publish
  draft forks a new draft and writes one `pathway.publish` audit row with before/after; publish saved version
  does not fork; `published_at/by` only first time (existing behaviour, assert via the core).
- `apps/api/tests/test_pathway_regression.py`: GET on a new pathway → defaults; PUT round trip (GET returns it,
  draft `global_config.regression` holds it, graph_hash changed, response draft_version/graph_hash); PUT same
  again → no second audit row, same graph_hash; PUT validation (unknown key, scenarios 21, scenario
  `max_turns` 0 → 400 field `scenarios[0].max_turns`); omitted keys default; save version carries the setting
  into the new draft (GET after `POST /version`); invalid stored value (seed draft global_config regression =
  "x" via repo) reads as defaults; audit row `pathway.regression_update` before/after shape; other org → 404.
- `apps/api/tests/test_pathways_permissions.py`: GET for viewer/analyst 200, billing 403; PUT analyst/viewer 403,
  developer 200.
- Existing `test_pathway_versions*.py` stay green (publish refactor).
- Cross-tenant suite covers the two routes automatically (PUT body from the first example).

### Gates
`make fmt lint typecheck placeholders`; targeted `uv run pytest packages/va_domain packages/va_db apps/api -n 8
-k "pathway or regression or audit or openapi or cross_tenant"`; `make test` in background with Monitor
heartbeat; `make test-ts`; openapi drift; spec refs; jscpd; radon. No e2e (no call/SMS flow; e2e comes in e3).

### Risks
- `test_audit_unit` compares specs/04 §13 list with `AUDIT_ACTIONS` — edit both.
- graph_hash recomputation must use the existing helper (draft update path) so hashes match export/import.
- Moving `pinned_number`: grep importers (`routers`, `services.pathways` version selector) and keep a re-export
  or update imports.

## Build P07-T11e1 (2026-11-04)
Implemented per plan. Deviations: `put_regression` hashes the stored draft JSON with
`va_engine.models.graph.graph_hash` directly (no re-validation: a regression PUT must not fail on unrelated save-mode
graph errors); test asserts it equals the parse+dump hash. `pinned_number` moved to `va_db.pathway_publish` (api
re-imports). `get_regression` returns the settings only. Shared `pathway_rows.seed_orders_pathway` (va_db tests
support) replaced a duplicate in va_pathways `test_loader.py` (jscpd 0). Spec: Decision P07-T11e1 under specs/12 §9,
§11 audit list, specs/04 §13 list. Gates: fmt/lint/typecheck/placeholders pass; test-ts pass (22/52/48); openapi drift
current; spec refs 0; jscpd 0; radon A (1.5); pip-audit clean; pnpm audit 3 moderate. make test run 1: gw0 OOM-killed
(dmesg; e2e docker stack shares the 15 GB host) at 94% and syrupy's xdist merge warning then crashed session finish
(exit 2, summary lost). Rerun with junitxml.
Rerun (2026-11-04, after inactivity-timeout rejection with no findings): make test finished: 12379 passed, 8 failed,
84 errors, coverage TOTAL 99% (combined), 3.5 h wall time because another session's pytest shared the host; every
failure/error was a TimeoutError in untouched modules (edge/dispatcher DB fixtures, media runtime_sim, api
org_members/transcript_ws, live gate). Re-ran all 18 affected modules + the T11e1 test modules alone: 360 passed,
exit 0. make lint/typecheck/placeholders pass this iteration; radon A (1.375).
Rerun 2 (2026-11-04, second empty review.rejected = inactivity timeout, no findings): lint/typecheck/placeholders
exit 0; T11e1 modules (regression API, publish core DB, domain settings, permissions) 89 passed. Full make test
evidence from the previous rerun stands (all failures load timeouts in untouched modules, 360/360 pass alone).

## Review P07-T11e1 (2026-11-04)
Approved. make lint/typecheck/placeholders pass; spec refs 0; openapi drift current; jscpd 0. First make test
(-n auto) hung after a host OOM kill of an xdist worker (dmesg); killed it and re-ran the same command with -n 6:
12462 passed, 7 skipped, coverage 99.08%, exit 0. Diff matches specs/12 §9 Decision P07-T11e1 (defaults, extra
forbidden, lenient reads, lock + no-op, audit shape, editor guard) and §11/specs/04 §13 audit lists; publish
refactor keeps behaviour (version tests green).
