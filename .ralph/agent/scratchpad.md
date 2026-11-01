# Scratchpad — Phase 03

## Plan P03-T06b (Planner, 2026-11-01)

Context: the previous loop run died ("too many consecutive failures": Claude Code lingered after each turn and Ralph
counted SIGTERMs; ralph.yml now has `max_consecutive_failures: 50`). The P03-T06b implementation is already in the
working tree, UNCOMMITTED and UNREVIEWED. Planner check (2026-11-01): `pytest packages/va_sim/tests/llm
tests/test_state.py` 504 passed; ruff clean; mypy va_sim clean (168 files); placeholders 0; radon: no C+ blocks in
va_sim/llm. Do NOT rewrite it. Builder job = verify against the acceptance criteria, close gaps, run full gates.

Existing files (keep):
- src/va_sim/llm/heuristics.py (`STRUCTURED_TOOL_NAMES`, `structured_arguments`, in-call classifiers),
  heuristics_post.py (post-call: disposition/summary/analysis/citations/grade), heuristics_base.py (`Heuristic`),
  schema_fill.py (`SchemaFill.conform` -> always schema-valid), text.py (text helpers),
  embeddings.py (pure: blake2b signed buckets, L2 norm, float|base64, dims), embed_route.py (`POST /v1/embeddings`),
  gate.py (shared auth/model/fault answers); modified chat.py/control.py/params.py/reply.py/routes.py/store.py/state.py
- tests: test_llm_heuristics.py, test_llm_schema_fill.py, test_llm_text.py, test_llm_embeddings.py,
  support/llm_schemas.py (`SPEC_CALLS` for all 14 names, `forced_body`), test_llm_openai_sdk.py
  (`test_forced_structured_calls_return_schema_valid_arguments`, `test_embeddings_through_the_sdk`)
- specs/30 §3.4 Decision P03-T06b already written; plan P03-T14 drift-test note; AGENTS.md P03-T06b bullet;
  pyproject dev deps jsonschema + types-jsonschema (uv.lock updated).

Builder checklist:
1. Confirm `STRUCTURED_TOOL_NAMES` == the specs/10 §3 registered-name list exactly (every name, character for
   character); add a test parsing specs/10 §3 if one does not exist (check test_llm_heuristics.py first).
2. Confirm SDK acceptance test covers EVERY name in `SPEC_CALLS` and validates with
   `jsonschema.Draft202012Validator`; confirm embeddings tests assert determinism, unit norm, 1024 default,
   `dimensions` override, similar > dissimilar cosine, base64 decode == float vector, usage tokens.
3. Check specs/30 §3.4 decision text matches the code (dims default, bucket hashing, encoding formats, fault
   `endpoint` field, `/sim/state` llm `embeddings` count).
4. Run gates: `make fmt lint typecheck placeholders test` (record counts + coverage), `uv run python
   scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent` (0 clones expected), `uvx radon cc -s -a
   packages/va_sim/src/va_sim/llm`, audit (`uvx pip-audit` on `uv export` — new dep jsonschema).
   Known flake: apps/api pytest-timeout setup errors under load (fix:api-test-timeout-load); rerun module alone.
5. Tick nothing (Reviewer ticks). Emit build.done with the backpressure strings.

Risks: jscpd clones between heuristics/heuristics_post; schema_fill.py at 472 lines (fine unless radon C).

## Build P03-T06b (Builder, 2026-11-01)

Verified WIP against checklist, no code changes needed: specs/10 §3 name parse test exists
(test_llm_heuristics: 14 names == STRUCTURED_TOOL_NAMES == SPEC_CALLS); SDK test validates every SPEC_CALLS entry with
Draft202012Validator; embeddings tests cover determinism/unit-norm/1024/similarity/encodings/inputs/faults.
Gates: make fmt 0 (no diff), lint 0, typecheck 0, placeholders 0 findings, check_spec_refs 0, make test 0
(4873 passed, TOTAL coverage 98%), pip-audit strict: no known vulns, pnpm audit: 3 moderate (< high), jscpd 0 clones,
radon va_sim/llm avg A (3.26), no C+ blocks.

## Review P03-T06b (Reviewer, 2026-11-01)

Approved. Re-ran: make lint/typecheck/placeholders 0; make test 4873 passed, 98.02% coverage. Diff checked against
specs/30 §3.4 + Decision P03-T06b and plan acceptance (SDK forced call per name schema-valid; embeddings
deterministic/unit/1024/similarity). Reviewer fix in place: module docstrings cited stale Decision P03-T06b rule
numbers (decision has rules 1-6); corrected in heuristics*, schema_fill, text, embeddings, embed_route, gate, control.
