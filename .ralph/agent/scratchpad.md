# Scratchpad — Phase 03 (resumed run, 2026-10-31)

Earlier entries (P03-T01 to P03-T05c) are in git history: `git show HEAD:.ralph/agent/scratchpad.md`,
`git show 7afdd0c:.ralph/agent/scratchpad.md`.

## Plan P03-T06a — FakeLLM chat completions, rules engine, faults, and models (Planner, 2026-10-31, resume)

State: an earlier loop implemented the task and ran `make check` with EXIT=0 (logs/p03t06a_check.log). The loop
then stopped ("too many consecutive failures") before review or commit. The work is uncommitted in the tree:
- new: `packages/va_sim/src/va_sim/llm/{__init__,chat,control,errors,params,reply,routes,rules,store,wire}.py`,
  tests `packages/va_sim/tests/llm/test_llm_{chat,control,faults,openai_sdk,params,reply,rules}.py`,
  `packages/va_sim/tests/support/llm_helpers.py`
- modified: va_sim control.py (fault_route, yaml_or_json_body), scenario.py (compose_document), server.py,
  settings.py (llm_model_ids, SIM_LLM_RULES), state.py (LlmStore), stt_control/voice_control (shared helpers),
  tests (test_server/test_settings/test_state), pyproject.toml + uv.lock (`openai` root dev dep), specs/02 §6,
  specs/30 §3.4 `> Decision (2026-10-31) ... (P03-T06a)` at line 772, AGENTS.md P03-T06a entry, plan split
  T06 → T06a/T06b (already in plan/phase-03.md).
Planner check this iteration: `uv run pytest packages/va_sim/tests/llm tests/test_server.py tests/test_settings.py
tests/test_state.py` → 125 passed in 4.9 s.

Builder: do not redesign or reimplement.
1. Check the WIP against the plan/phase-03.md P03-T06a deliverables; each needs a test: Bearer auth + OpenAI
   error bodies; request validation (400 with `param`); stream + non-stream; `stream_options.include_usage` usage
   chunk; `data: [DONE]`; tool calls from rules incl. forced `tool_choice`; finish_reason stop/tool_calls/length
   (`max_tokens`); TTFT + tokens/s pacing on the sim clock with per-rule `ttft_ms`/`delay_ms`; default dialogue
   reply; rules: `SIM_LLM_RULES` loaded at startup, `POST/GET/DELETE /sim/llm/rules`, priority then registration
   order, matchers `system_contains`, `last_user_regex`, `tool_name`, `model`, `call_index`, `call_id`
   (`X-VA-Call-Id`); faults: rule `error`, `/sim/llm/fault` 429 + `Retry-After`, 500/502/503, timeout before
   headers, stall after n tokens, malformed tool args; `GET /v1/models` from env model ids; `/sim/llm/requests`;
   reset. Acceptance: `openai` SDK streaming text, non-streaming, streamed tool-call delta assembly
   (test_llm_openai_sdk). Add a missing test and fix only real defects. Check shapes against the specs/30 §3.4
   Decision P03-T06a text. Scope excludes T06b (structured heuristics, embeddings).
2. Gates with real results:
   - `make fmt lint typecheck placeholders`
   - `make check` (if apps/api shows pytest-timeout errors under load — known task fix:api-test-timeout-load —
     re-run those modules alone and record both results)
   - `npx --yes jscpd@4 --silent` (0 clones); `uvx radon cc -s -a packages/va_sim/src/va_sim/llm` (new ≤ B)
   - `uv export --no-emit-workspace --frozen | uvx pip-audit --strict`; `corepack pnpm audit --audit-level high`
     (new dep `openai` is dev-only; it must audit clean)
3. Emit plain text (no --json):
   `ralph emit "build.done" "tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass, complexity: <grade (score)>, duplication: pass, specs: pass"`

Risks: host load during `make check` (re-run, never skip). The `disconnect` fault makes uvicorn log an exception;
make sure no test depends on log silence there. Reviewer commits `feat(sim): FakeLLM ...` with footer
`Task: P03-T06a`.

## Build P03-T06a (Builder, 2026-10-31)

Verified the WIP against every deliverable; each has a test (tests/llm/test_llm_{chat,control,faults,openai_sdk,
params,reply,rules}.py, plus test_server/test_settings for SIM_LLM_RULES startup loading and model ids). No code
changes were needed this iteration.
Gates (real runs, logs/p03t06a_gates.log, logs/p03t06a_check2.log):
- make fmt/lint/typecheck/placeholders: EXIT 0 (mypy 563 files clean, 0 placeholder findings)
- make check: EXIT 0, 4478 passed in 109 s, coverage 98.13 %; va_sim.llm modules 99-100 %
- jscpd: 0 clones; radon va_sim.llm: avg A (2.72), max B (10, params._param_of)
- pip-audit --strict: no known vulnerabilities; pnpm audit --audit-level high: 3 moderate (pre-existing, TS untouched)

## Review P03-T06a (Reviewer, 2026-10-31) — approved

Re-ran: make lint typecheck placeholders EXIT 0 (0 findings); make test EXIT 0, 4478 passed, coverage 98.13 %.
Read chat/store/reply/wire/rules/control/errors + shared-helper refactors against specs/30 §3.4 Decision P03-T06a
rules 1-11: check order, fault semantics (early faults never advance call_index; malformed stays armed until a
tool-call reply), stream wire order + include_usage, truncation, rules ordering/counters, reset. No findings.
