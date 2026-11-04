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
