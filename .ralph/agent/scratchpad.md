# Scratchpad — Phase 05 (restarted loop, 2026-11-02)

The previous loop stopped on max runtime with P05-T10b implemented but uncommitted (no build.done/review). The old
scratchpad (Plans/Builds/Reviews through P05-T10a) is in git history (`git show 627a5be:.ralph/agent/scratchpad.md`).

## Plan P05-T10b — Greeting gate, outbound DTMF, IVR mode (Planner, 2026-11-02)

State: the working tree already holds a full implementation (see `git status`). The Builder's job is to verify it
against the acceptance criteria below, close any gap, run every gate, and emit build.done. Do NOT rewrite it.

Existing pieces (verify, keep):
- `packages/va_voice/src/va_voice/pipeline/pipeline.py` — `CallPipeline(before_opener=)`, greeting wait in the reply
  loop (`PipelineConfig.wait_for_greeting`, `greeting_timeout_ms` 1 000–30 000), `Brain.start(greeting)`, no-input
  timer never armed before the opener, `ivr` task when `ivr_mode`.
- `packages/va_voice/src/va_voice/pipeline/ivr.py` — `IvrProgress` (normalized-text novelty, actions = progress,
  `ivr_dead_end_s` 120 s); end reason `ivr_dead_end`, ended_by `system`.
- `protocols.py` `Brain.start(greeting: UserTurn | None = None)`; `va_engine.task_runner.TaskBrain.start` appends the
  greeting before `first_sentence` / answers it.
- Media: `va_media.carrier_watch.CarrierWatch` (answered from snapshot or primary-leg `call.answered`),
  `va_media.greeting.GreetingGate` (outbound 2 s answered wait, precall groups), `va_media.dtmf_out` (split on
  `w`=0.5 s/`W`=1 s, `send_dtmf` per group with `command_id`), `va_media.actions.MediaActions` (press_button).
- `packages/va_telnyx/src/va_telnyx/settings.py` — `TelnyxApiSettings`, `telnyx_http`, `telnyx_factory`,
  `SIM_TELNYX_API_KEY`; mixed into worker + media settings (va_telnyx pyproject gained a dep; uv.lock updated).
- Spec decisions already written: specs/07 §11 Decision P05-T10b, specs/06 §9 Decision, specs/02 env table row for
  `TELNYX_API_BASE` (media added). AGENTS.md line added. plan/phase-05.md already split T10d out.

Acceptance checklist (each must have a passing test; all exist today — confirm):
1. Pipeline harness: gate order (hook before opener) — test_pipeline_greeting.py.
2. wait_for_greeting with speech / DTMF / timeout; no reprompt while waiting — test_pipeline_greeting.py.
3. IVR dead end + progress resets (new text, button press), silence after opener, none without ivr_mode —
   test_pipeline_ivr.py.
4. ManualClock answered wait (snapshot, channel event, timeout) and precall groups — test_outbound_dtmf.py.
5. FakeTelnyx+FakeElevenLabs+FakeLLM runtime: precall reaches send_dtmf before the agent speaks; press_button tool
   call reaches send_dtmf; wait_for_greeting holds the opener — test_runtime_sim.py (3 new tests).
Planner check: these 305 targeted tests pass (`uv run pytest --no-cov` on the touched test files, 78 s).

Builder steps:
1. Review the diff for placeholders / spec mismatches (specs/07 §11 steps 1,3,4,6; specs/06 §9; specs/05 §9
   `ivr_dead_end` row exists and ended_by=system). Check `.env.example` ↔ specs/02 §6 still mirror
   (`scripts/tests/test_env_example.py`) after the TELNYX_API_BASE row edit.
2. Check per-package coverage ≥ 85 % (new modules should be ~100 %); add tests for any uncovered branch in
   greeting.py / actions.py / dtmf_out.py / ivr.py / va_telnyx settings.py.
3. Gates: `make fmt`, `make lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `make test`,
   `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a` on touched src (no C+), audit
   (`uvx pip-audit` on `uv export` + `corepack pnpm audit`). make e2e harness does not exist yet (P05-T17) — say so.
4. Do not commit the scratchpad deletion problem away: this file is the new scratchpad (tracked).

Risks: host load makes api/va_db integration tests hit the 60 s timeout (known open fix tasks in summary); rerun the
failing module alone before concluding a regression. Accelerated-clock runtime sim tests: never busy-poll.

## Review P05-T10b (Reviewer, 2026-11-02) — APPROVED

Re-ran: make lint, make typecheck (mypy 1133 files), make placeholders (0), check_spec_refs (0), make test
(9616 passed, 7 skipped, 99.16 %; new modules 100 %), jscpd 0 clones, radon no C+. Diff checked against specs/07
§11 steps 1/3/4/6 + Decision, specs/06 §9 Decision, specs/05 §9 `ivr_dead_end` (system), specs/02 §6
TELNYX_API_BASE row. Note (not blocking, pre-existing from P03-T08): `TelnyxClient.send_dtmf` sends no
`client_state`; command_id idempotency is in place. make e2e does not exist until P05-T17.
