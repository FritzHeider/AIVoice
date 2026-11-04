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
