# Phase 08 scratchpad

## Planner 2026-11-04 — inbox triage
- Inbox notes not in DONE: `2026-11-04-p08-t00-accept-stages.md` (review of the uncommitted P08-T00 tree; folded into
  P08-T00 deliverables) and `2026-11-04-recording-copy-lock-busy-budget.md` (its finding was fixed by P08-FIX-1: busy
  delay = PTTL + 1 s; attached patch = P08-FIX-1 patch + `test_get_recording.py`, both on main) → new P08-FIX-2 after
  P08-T00 (regression test only). P08-T00 goes first because its work sits uncommitted in the tree; a FIX built on top
  would have to commit around it. DEC: ordering deviation from "insert at top", conf 80.
- The previous scratchpad was deleted in the working tree (restore not needed; HEAD has it). This file restarts it.

## Plan P08-T00 (finish + inbox finding)
State of the working tree (already written, keep it): `call.interrupt` data adds `speech_ms`/`clear_delay_ms`/`level`
(turns.py + test_turns.py, specs/07 §17, specs/19 §2); e2e `interruptibility_levels` asserts the policy's
`speech_ms` ≥ min and sim clear − onset ≥ min − `ONSET_TOLERANCE_MS` (32 ms = `va_voice.vad.WINDOW_MS`)
(`tests_e2e/support/e2e_timing.py`, unit tests in `tests_e2e/unit/test_e2e_timing.py`); edge accept `stages_ms` in
`telnyx.inbound_answered` (accept.py, test_accept_db.py); specs/06 §7, specs/30 §5 two Decisions; AGENTS.md lines.
The inbox note's "check before commit" (specs/06 + specs/30 text) is satisfied by the current diff.

### Remaining work: Finding 1 (stages lost on accept timeout/failure)
`apps/telephony_edge/src/va_edge/accept.py` `InboundAccept.accept`:
- The router (`routing.py` ~line 371) already logs `telnyx.inbound_accept_failed` (error_type, code, field) for every
  accept exception, so do NOT emit a second event with that name. Instead, in `accept()` on both failure branches
  (exception and `not finished`), before `_cleanup`, log
  `_log.warning("telnyx.inbound_accept_stages", call_id=, org_id=, reason="timeout" | type(exc).__name__,
  stages_ms=dict(progress.stages_ms), last_stage=<last key or None>, elapsed_ms=round((monotonic -
  call.received_monotonic)*1000, 1))` via a helper `_log_unfinished(call, progress, reason)`.
  (The note suggested the name `telnyx.inbound_accept_failed`; record in specs/06 §7 Decision text that the stage log
  is `telnyx.inbound_accept_stages` because the router owns `inbound_accept_failed`.)
- `_Progress.stages_ms` is a dict filled in order, so `last_stage = next(reversed(stages_ms), None)`.
- Note: on timeout `within` cancels `_steps`; stages finished before the cancel are already in the dict. Answer stage is
  stamped only on success; a failing answer (`answer_failing_factory`) → last stage `start`.

### Tests (apps/telephony_edge/tests/test_accept_db.py)
1. `test_a_timed_out_accept_logs_the_stages_it_finished`: GatedClock, `r.starter.hang = True` (as in
   `test_the_accept_budget_cancels_a_hanging_start_and_refuses`), capture_logs around route, fire ACCEPT_BUDGET_S →
   one `telnyx.inbound_accept_stages` with reason `timeout`, `list(stages_ms) == ["route","persist","context"]`,
   `last_stage == "context"`, `elapsed_ms >= 0`; logged before cleanup's call failure (order: stages log index <
   any cleanup log, or just check presence + router's `inbound_accept_failed` follows it).
2. `test_a_failed_answer_logs_the_stages_it_finished`: `accept_rig(database, telnyx=answer_failing_factory)` →
   reason = the Telnyx error type name (assert equals the router log's `error_type`), stages route..start,
   last_stage `start`.
3. Success path unchanged: existing `test_the_answer_log_names_the_time_of_every_step` also asserts no
   `telnyx.inbound_accept_stages` log.

### Specs/docs
- specs/06 §7 Decision text (the stages sentence added this task): add "a timed-out or failed accept logs the stages
  it finished in `telnyx.inbound_accept_stages` (`reason`, `stages_ms`, `last_stage`, `elapsed_ms`) before its cleanup".
- AGENTS.md P08-T00 line about `stages_ms`: mention `telnyx.inbound_accept_stages` for failures.

### Gates
1. `uv run pytest apps/telephony_edge apps/media_worker/tests/test_turns.py tests_e2e/unit -q`
2. `make check` (background + heartbeat Monitor; PATH must include /home/ralph/.local/bin; ~25 min).
3. e2e acceptance: `make up PROFILE=e2e` (or `scripts/dev_up.sh e2e`) then 10 standalone runs
   `for i in $(seq 10); do uv run pytest tests_e2e -m e2e -k interruptibility_levels -q; done` — record pass counts
   (all 4 levels, focus [1]); then `make e2e` (rebuilds images). Check `amd_ivr_mode` in that run; if its DTMF-digit
   flake recurs, capture the timeline/logs in the scratchpad (do not hide it with retries); if it does not recur in
   `make e2e` + one cold rebuild, record that in the task evidence.
4. Commit: `fix(e2e): interruptibility onset reference and accept stage logs` + `Task: P08-T00`; reviewer appends the
   accept-stages note to `.ralph/review-inbox/DONE`.

### Risks
- `make test` right before `make e2e` causes whole-stack contention (AGENTS.md P08-T00): run e2e after make check
  finishes, not concurrently.
- capture_logs misses events in some packages once logging is configured; edge tests already use capture_logs
  successfully for `telnyx.inbound_answered`, so keep that.

## Build P08-T00 (2026-11-04)
- accept.py: `InboundAccept._log_unfinished` logs `telnyx.inbound_accept_stages` (reason timeout | exception type,
  stages_ms, last_stage, elapsed_ms) on both failure branches before `_cleanup`; router keeps `inbound_accept_failed`.
- Tests (test_accept_db.py): timed-out accept → stages route/persist/context, last `context`; failed answer → reason
  == router error_type, stages ..start; success path asserts no stages log. specs/06 §7 Decision + AGENTS.md updated.
- Gates: make check exit 0 (12646 passed, 7 skipped, coverage 99 %, TS 22+52+48 passed); e2e stack rebuilt
  (`scripts/dev_up.sh e2e`), 10/10 standalone `-k interruptibility_levels` runs passed (5 passed each);
  `make e2e` 20 passed exit 0 (amd_ivr_mode passed: its DTMF flake did not recur).

## Review P08-T00 (2026-11-04)
Re-ran: `make lint typecheck placeholders` exit 0; `make test` exit 0 (12646 passed, 7 skipped, coverage 99.07 %);
focused tests (test_accept_db, test_turns, tests_e2e/unit/test_e2e_timing) 61 passed. Diff vs specs/06 §7, specs/07
§17, specs/19 §2, specs/30 §5 is consistent; `interruptibility_levels` (all 4 levels) passed inside my `make e2e`.
Rejected because the claimed `make e2e` gate does not pass (acceptance: "`make e2e` passes"):
1. `make e2e` (logs/review_e2e.log) exit 2: `tests_e2e/test_concurrent_calls.py::test_concurrent_calls_on_one_media_worker`
   failed `assert 151 <= 150` (overhead p50 151 ms vs `OVERHEAD_P50_MS` 150). Standalone `uv run pytest tests_e2e -m
   capacity -s` then failed 4 of 8 runs: underruns 1, 18, 1 (`assert underruns == 0`) and one more fail; passing runs
   had p50 142-150 ms (right at the bound). Load was low (load avg ~3.5 on 8 CPUs, nothing else running). The
   P08-T00 diff does not touch the media hot path (3 extra keys on `call.interrupt` data), so this is probably
   pre-existing, but it must be shown, not assumed: rebuild images at HEAD (`git stash`, `scripts/dev_up.sh e2e`, run
   `-m capacity` ~8 times, `git stash pop`, rebuild). If HEAD fails the same way, do not hide it in P08-T00: emit
   `build.blocked` naming the capacity flake so the Planner inserts a P08-FIX task (root cause: underruns + p50
   margin under 50 calls), and P08-T00 can be approved on its own acceptance. If only the working tree fails, fix it.
2. Inbox note `.ralph/review-inbox/2026-11-04-p08-t00-plan-wording.md` (non-blocker): `plan/phase-08.md` P08-T00
   says the acceptor "must log `telnyx.inbound_accept_failed`"; the code/spec log `telnyx.inbound_accept_stages` and
   the router owns `inbound_accept_failed`. Reword the plan paragraph to match specs/06 §7 (no code change); append
   that note's name to `.ralph/review-inbox/DONE` together with `2026-11-04-p08-t00-accept-stages.md` on approval.

## Blocker P08-T00 (2026-11-04) — capacity flake is pre-existing at HEAD
- Review finding 2 is done: `plan/phase-08.md` P08-T00 now says the acceptor logs `telnyx.inbound_accept_stages` and
  the router owns `telnyx.inbound_accept_failed` (matches specs/06 §7). No code change.
- Review finding 1, the HEAD check: the 4 app files were reverted to HEAD (patch kept in `logs/p08t00_apps.patch`,
  backup in `/var/tmp/p08t00-backup`), `scripts/dev_up.sh e2e` exit 0, then 8 runs of
  `uv run pytest tests_e2e -m capacity -s -q` (logs `logs/head_cap_N.log`, summary `logs/head_capacity.log`).
  5 passed with overhead p50 139/141/146/131/142 ms, all within 4-19 ms of the 150 ms bound. 3 failed on
  `assert underruns == 0`, with 14, 3 and 1 underruns (runs 4, 6, 7).
  So HEAD fails the same way, and P08-T00 (3 extra `call.interrupt` data keys, edge accept logs) does not cause it.
- Working tree restored (cmp against the backup: identical), stack rebuilt from the working tree (dev_up exit 0),
  focused tests 61 passed.
- Proposal for the Planner: insert P08-FIX-3 "50-call capacity: platform underruns + overhead p50 margin on one media
  worker" (tests_e2e/test_concurrent_calls.py, MASTER_PROMPT §7 target "50 concurrent calls without underruns").
  Root-cause the underruns (outbound pacer lateness vs FakeTelnyx drain under load; py-spy --nonblocking on media
  and sim per AGENTS.md P05-T17e2/P07-FIX-1). Do not loosen the asserts. After that, approve P08-T00 on its own
  acceptance: 10/10 interruptibility_levels runs, and every other `make e2e` flow passed in the review run.

## Planner 2026-11-04 — P08-T00 blocker resolved
- Inserted P08-FIX-3 (50-call capacity: underruns + overhead p50 margin, acceptance 10/10 capacity runs, 0 underruns,
  p50 ≤ 140 ms) right after P08-T00, before P08-FIX-2. P08-T00 acceptance now excludes `-m capacity` from its
  `make e2e` requirement (pre-existing at HEAD, DEC-010).

## Plan P08-T00 (finalize)
- No new code: the working tree already holds the full P08-T00 diff + the plan-wording fix (review finding 2).
- Builder: verify the tree is intact (`git status` shows the P08-T00 files incl. plan/phase-08.md), run
  `uv run pytest apps/telephony_edge/tests/test_accept_db.py apps/media_worker/tests/test_turns.py tests_e2e/unit -q`
  and `make lint typecheck placeholders` (PATH += /home/ralph/.local/bin). Earlier full-gate evidence from this tree:
  make check exit 0 (12646 passed, 7 skipped, cov 99 %), 10/10 interruptibility_levels runs, review `make e2e`: all
  flows passed except concurrent_calls (pre-existing, P08-FIX-3). Re-run `make check` (background + heartbeat) because
  the plan file changed only — acceptable to rely on it if lint/typecheck/placeholders/focused tests pass; Reviewer
  re-runs `make check`.
- Then emit build.done with the evidence strings (tests/lint/typecheck/audit/coverage/duplication/complexity).
- Reviewer on approval: tick P08-T00 with evidence, append `2026-11-04-p08-t00-accept-stages.md` and
  `2026-11-04-p08-t00-plan-wording.md` to `.ralph/review-inbox/DONE`, commit
  `fix(e2e): interruptibility onset reference and accept stage logs` with `Task: P08-T00`.

## Build P08-T00 finalize (2026-11-04)
- Tree intact (16 modified files incl. plan/phase-08.md wording fix). Focused tests (test_accept_db, test_turns,
  tests_e2e/unit) 159 passed; `make lint typecheck placeholders` exit 0 (0 findings); radon avg 2.24 on touched src;
  jscpd 0 clones. Full `make check` evidence from this same code: exit 0 (12646 passed, cov 99 %); reviewer re-runs.
  `make e2e`: all flows except pre-existing capacity flake (P08-FIX-3) passed in the review run.

## Review P08-T00 finalize (2026-11-04) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12646 passed, 7 skipped).
- Diff matches specs/06 §7, specs/07 §17, specs/19 §2, specs/30 §5 Decisions; plan wording fixed. Capacity flake is
  pre-existing at HEAD → P08-FIX-3 (next). Inbox notes accept-stages + plan-wording appended to DONE.
