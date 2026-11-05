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

## Planner 2026-11-04 — after P08-T00 approval
- P08-T00 is ticked and committed (6a90594). Inbox: every note except `recording-copy-lock-busy-budget.md` is in DONE.
  That note is already covered by P08-FIX-2. Next unticked task in the checklist: P08-FIX-3 (depends on P08-T00).

## Plan P08-FIX-3 (50-call capacity: underruns + overhead p50 margin)
Goal: `uv run pytest tests_e2e -m capacity -s` passes 10/10 on a freshly rebuilt stack, with 0 underruns and
overhead p50 ≤ 140 ms in every run. Do not loosen asserts, retry, lower the count, or change LEAD_FRAMES/the
underrun definition (specs/07 §9 Decision P05-T17e).

### Facts
- Underrun = `OutboundAudio._send_frame` (packages/va_voice/src/va_voice/outbound.py ~l.440) sends a speech frame
  while `FramePacer.drained(now)` holds and the previous frame was speech of the same utterance. The media send loop
  of that leg was > 60 ms (lead 3) behind schedule. Counted in the media worker (`va_audio_underruns_total`).
- At HEAD it fails 3 of 8 runs with 14/3/1 underruns. Passing runs have p50 131-146 ms (logs/head_cap_*.log,
  logs/head_capacity.log). The last decision with measured numbers, specs/07 §21 P05-T17e2, reported p50 82-87 ms and
  0 underruns. Overhead has crept up since then (phases 06-07 added webhook routing in the media event flush, the
  TurnRecorder hold of `call.turn`, `call.interrupt` data, the pathway runtime, the engine state saver).
  Bisect-style attribution is useful here.

### Step 1: instrumentation (production, keep it)
- `OutboundAudio`: on each underrun, pass details to the hook, or log `outbound.underrun` at warning with
  `behind_ms` (now − the pacer's drained-at time), `since_prev_send_ms`, `send_await_ms` (how long the previous
  `await self._sink.send_audio` took) and `utterance turn`. Keep the existing `on_underrun: Callable[[], None]`
  signature (runtime metric) unless a richer type is needed. If you change it, update Speaker/CallPipeline/runtime
  and the harness `on_underrun` tests. Bind call_id through the logging context (already bound in the runtime).
- This attributes each underrun to one of: (a) a loop stall (the sleep overslept: large gap between the wake and
  the due time), (b) a slow `send_audio` await (WS backpressure from the sim's reader, i.e. the sim did not drain),
  or (c) the queue was empty, then refilled mid-utterance without an idle frame. Case (c) would be a definition bug:
  TTS starvation must not count. Check whether a starved stretch with no background (`static` bed is on by default,
  so idle frames are sent) can still count.
- Unit tests in packages/va_voice/tests/test_outbound.py (ManualClock): a stalled loop reports behind_ms ≥ the
  stall; a slow sink send reports send_await_ms; no log without an underrun.

### Step 2: measure (stack rebuilt with the instrumentation: `scripts/dev_up.sh e2e`)
- Run `-m capacity -s` about 5 times. Collect `outbound.underrun` lines from `docker logs voiceagents-media-1`,
  and loop lag from the report table.
- Profile one run each of the media worker and the sim with py-spy `--nonblocking --gil -f raw` (AGENTS.md command
  row). Aggregate by leaf and inclusive frame. Look for synchronous work > 20 ms on the media loop. Suspects, most
  likely first:
  1. Call start or end bursts on the loop: 50 attaches/ends at once, each doing CPU work inline. Examples: context
     JSON parse, `fit_voice_system`/tiktoken counting, prompt assembly, `build_pathway_logs`, the end routine's
     event flush with `DbWebhookRouter.route_many` + payload building (orjson of Call Objects), `TurnRecorder`
     event encoding. Fix: move bulky CPU work to `asyncio.to_thread` (MASTER_PROMPT §4.7), or yield between items
     of large batches (`await asyncio.sleep(0)` inside per-row loops of the flush).
  2. WS send backpressure: the media `send_audio` awaits a drain while the sim's `MediaSession._tick_loop` is busy
     (the sim is the main CPU consumer per P07-FIX-1). If `send_await_ms` explains the underruns, fix it in the sim:
     reader not starved, receiving decoupled from the tick loop. Sending is fire-and-forget in the real Telnyx, so a
     sim that stops reading is a test-harness artefact we own.
  3. GC: check `gc.callbacks` timing or py-spy samples in `gc_collect`; tune if gen2 collections hit.
  4. Per-frame overhead: 2 500 timer wakeups/s. Only if 1-3 don't explain it, consider a pod-wide 20 ms outbound
     ticker. It is a large change and would need a specs/07 §9 Decision.
- For the overhead p50 (target ≤ 140 ms), split sim v2v minus `e2e_ms` ("outside") from `e2e_ms` minus vendor
  stages ("inside") using `call.latency` rows (AGENTS.md P05-T17e2). Compare with the P05-T17e2 numbers to find
  which stage grew.

### Step 3: fix + record
- Fix the attributed causes in code (media and/or sim) with unit tests that fail before the fix. Examples: a test
  that the flush yields between rows, a test that the CPU step runs in a thread (patch `asyncio.to_thread`), and a
  sim test that a slow tick does not block reads.
- specs/07 §9: add `> Decision (2026-11-04) P08-FIX-3:` with the root cause, the measured before/after numbers
  (underruns, p50, loop lag p99), and the new `outbound.underrun` log fields. Add an AGENTS.md dated line with the
  attribution method.

### Gates
1. Focused: `uv run pytest packages/va_voice apps/media_worker -q` (plus `packages/va_sim` if the sim changed).
2. `make check`. Run it in the background with a heartbeat Monitor, PATH += /home/ralph/.local/bin, ~25 min.
   Do not run e2e at the same time (contention).
3. `scripts/dev_up.sh e2e` (rebuild), then 10 consecutive runs of `uv run pytest tests_e2e -m capacity -s -q`,
   logs in `logs/fix3_cap_N.log`. Record underruns and p50 for each run. All runs must pass with p50 ≤ 140.
4. `make e2e` (all flows incl. capacity).

### Risks
- Variance: take numbers only from unprofiled runs (py-spy without --nonblocking inflates them).
- `make test` just before e2e causes contention. Sequence the gates.
- If the root cause turns out to be host noise not attributable to code after 3 genuine attempts, emit build.blocked
  with the data. Do not tweak thresholds.

## Build P08-FIX-3 (2026-11-04)
Attribution (instrumented, kept): `outbound.underrun` (behind_ms, since_prev_send_ms, prev_send_await_ms,
wake_late_ms, queued_frames) + `gc_pause` (va_common.gc_tuning.GcPauseLog). Every underrun = synchronized media-loop
stall (wake_late 60-80 ms, send await 0.1 ms, dozens of legs at once) coinciding with a GC pass: gen0 40-75 ms
freeing 16k-54k objects (threshold 50k from P05-T17e2).
Root cause: each finished call was a set of reference cycles (~1000 objects): bound-method callbacks between parts
(InboundPath.now_ms, Speaker.next_index, BargeInMonitor.on_unhonored -> pipeline; OutboundAudio.on_finished ->
Speaker; MissingVariables sink -> TaskBrain; transport.on_rate_limited, MediaActions closures, CarrierWatch ->
VoicemailMonitor -> pipeline.take_over), vendor sockets keeping finished tasks (CancelledError tb -> self),
_PipelineEnded traceback holding _run_reply's task, CPython 3.12.7 TaskGroup.__aexit__ `me` cycle.
Fixes: no owner-capturing callbacks (pipeline/speaker/task_runner), runtime `_unwire` on exit stack,
`stop_tasks` empties the list, `_clear_frames` in CallPipeline.run; webhook router prebuilt statements + per-batch
lookups; CallTurnRepo prebuilt upsert executemany; GC thresholds (10_000, 5, 50).
Second finding: the SIM runner had default GC -> full passes 85-105 ms (~175k objects) -> inflated overhead
(cold run p50 152-153). Moved tuning to va_common.gc_tuning.tune_gc, sim calls it with the same thresholds.
Tests: packages/va_common/tests/gc_cycles.py helper; va_voice test_pipeline_teardown (live cycles, fail pre-fix),
runtime sim no_part_on_a_cycle x2 (fail pre-fix), test_gc_tuning, test_ws_tasks, router batch test, outbound log tests.
Runs before the sim fix (fresh rebuild, 10 runs): underruns 0 x10; p50 153 (cold run 1), 101-137 others.

## Review P08-FIX-3 (2026-11-04) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12661 passed, 7 skipped, cov 99 %);
  focused new tests 51 + router/turn DB tests 26 passed. Builder `make e2e` finished 20 passed exit 0 (incl. capacity).
- My own 4 capacity runs on the running stack: 0 underruns each, overhead p50 128/124/122/115 ms (≤ 140).
- Diff matches specs/07 §9 Decision P08-FIX-3; Core inserts use column names (`text`), no owner-capturing callbacks left.

## Planner 2026-11-04 — after P08-FIX-3 approval
- Inbox: 7 notes reviewing the P08-FIX-3 WIP were not in DONE; their findings are still open at 2abf225 (gc_pause logged
  inside the gc callback with a random call context; underrun log before the generation check / unthrottled / no
  turn / stale `_due_ms` / mark await unattributed / backpressure vs loop lag; `_unwire` docstring; Decision numbers).
  All non-blockers → inserted P08-FIX-4 (after P08-FIX-2, before P08-T01). Next unticked: P08-FIX-2.

## Plan P08-FIX-2 (RecordingBusy retry budget vs copy lock hold)
Context: the fix itself landed in P08-FIX-1 (`RecordingCopyLock.acquire` → `CopyLockAttempt.remaining_ms`;
`RecordingActivities._lock` raises retryable `RecordingBusy` with `next_retry_delay = PTTL + BUSY_RETRY_MARGIN_MS`;
`COPY_LOCK_TTL_MS` = STORE_RECORDING_TIMEOUT 5 min + 1 min). Existing test
`test_a_different_recording_waits_for_the_running_copy` asserts one busy delay. Missing: the note's "holder keeps the
lock beyond the retry budget" scenario and the budget arithmetic. No production change unless a test fails.

### Tests (packages/va_workflows/tests/test_recording_activity.py, reuse `_call`, `_run`, `serve_recording`, `wav_bytes`,
`saved_input`, `MemoryS3`, `RecordingCopyLock(FakeAsyncRedis())`)
1. `test_a_long_running_holder_is_waited_out_and_its_copy_is_already_stored`:
   holder acquires the lock as owner `rec:<holder>` and `await redis.pexpire(key, 300_000)` (≥ 300 s, beyond the old
   ~30 s backoff budget of STORE_RECORDING_RETRY: 2+4+8+16 s). Attempt 1 of a different recording → ApplicationError
   type RECORDING_BUSY, retryable, `next_retry_delay >= timedelta(milliseconds=pttl)` and > 30 s. Then the holder
   "commits": `update(Call)...values(recording_s3_key=<holder key>)` in a tenant session + `lock.release(call_id,
   owner)` (use whatever release API the class has). Attempt 2 (same input) → `StoreRecordingResult(status=
   "already_stored", s3_key=<holder key>)`, `s3.uploads == 0`, no call_events rows, lock key gone afterwards.
2. `test_busy_retries_fit_the_post_call_budget` (pure, no DB): worst case of a waiting run =
   `(STORE_RECORDING_RETRY.maximum_attempts - 1)` busy delays of at most `COPY_LOCK_TTL_MS + BUSY_RETRY_MARGIN_MS`
   plus one full `STORE_RECORDING_TIMEOUT` attempt (the last one does the copy) must be < `POSTCALL_BUDGET_S`
   (va_domain.post_call). 4 × 361 s + 300 s = 1744 s < 1800 s. Also assert `COPY_LOCK_TTL_MS > STORE_RECORDING_TIMEOUT`
   (a live holder never outlives its lock). If the arithmetic fails, that is a real defect: then shorten
   COPY_LOCK_TTL / cap the busy delay, and update specs/06 §11 Decision P08-FIX-1 accordingly.
3. Temporal check that `next_retry_delay` beats the policy's `maximum_interval=30s` (otherwise the whole fix is moot):
   in `packages/va_workflows/tests/` with `workflow_rig.workflow_env()` (time skipping), a tiny test workflow +
   activity named for the test that raises `ApplicationError(type=RECORDING_BUSY, next_retry_delay=timedelta(
   minutes=5))` on attempt 1 and returns on attempt 2, executed with `retry_policy=STORE_RECORDING_RETRY`; assert
   attempt 2's `activity.info().current_attempt_scheduled_time - attempt 1's started_time >= 5 min` (record from the
   activity; time skipping makes it instant). Prefer reusing an existing workflow-level test pattern (look at
   tests that use `workflow_env()` with real activities, e.g. test_post_call_workflow*). If the SDK caps the delay at
   maximum_interval, give `store_recording` its own policy without a 30 s cap (maximum_interval ≥ COPY_LOCK_TTL) and
   record it in specs/06 §11.

### Gates
- `uv run pytest packages/va_workflows -q -k "recording or busy"`, then `make check` (background + heartbeat Monitor,
  PATH += /home/ralph/.local/bin, ~25 min). No e2e needed (no call-flow code change) unless production code changes.
- Reviewer: tick P08-FIX-2, append `2026-11-04-recording-copy-lock-busy-budget.md` to `.ralph/review-inbox/DONE`,
  commit `test(workflows): recording copy busy retry outlasts a long lock holder` + `Task: P08-FIX-2`.

### Risks
- FakeAsyncRedis PTTL after pexpire is fine; do not use real sleeps. The tests touch Postgres via the rig (SystemClock).
- A test workflow defined inside a test module must be importable by the time-skipping worker sandbox: define it at
  module level with `@workflow.defn(sandboxed=False)` or pass-through imports, like other rig workflows.

## Build P08-FIX-2 (2026-11-04)
- No production change needed. New tests `packages/va_workflows/tests/test_recording_busy_retry.py` (+ workflow stub
  `tests/support/busy_retry_stub.py`): (1) a holder that keeps the lock 300 s → RECORDING_BUSY, retryable, delay ≥ PTTL
  and > 30 s; holder commits + releases → retry `already_stored` with the holder's key, 0 uploads, no events, lock gone;
  (2) budget arithmetic 4 × 361 s + 300 s = 1744 s < 1800 s and COPY_LOCK_TTL > STORE_RECORDING_TIMEOUT;
  (3) Temporal time-skipping: next_retry_delay 5 min honoured past maximum_interval 30 s (workflow time ≥ 300 s;
  control without delay waits 2-30 s). Note: activity.info().current_attempt_scheduled_time stays wall-clock under
  time skipping, so the stub measures with workflow.now() (AGENTS.md line added).
- Gates: make check exit 0 (12664 passed, 7 skipped, cov 99.07 %); pip-audit clean, pnpm audit 1 moderate (< high); radon avg 2.92 on new files; jscpd 3 clones all in pre-existing packages/va_voice/tests/test_outbound_audio.py (P08-FIX-3), none in new files.

## Review P08-FIX-2 (2026-11-04) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0; new tests 3 passed; `make test`: 12663 passed, 7 skipped, cov 99 %,
  1 failed = media runtime sim precall test (FakeLLM timeout → retry → 2 requests under load; 3/3 pass alone; unrelated
  to this test-only diff) → logged as P08-FIX-5 (DEC-011). Inbox note appended to DONE.

## Planner 2026-11-04 — after P08-FIX-2 approval
- Inbox: new note `2026-11-04-p08-fix-2-budget-test.md` (not in DONE, no task): the P08-FIX-2 budget test ignores
  the time steps 1-2 spend inside the 30 min budget (wait up to 600 s etc.) → inserted P08-FIX-6 above P08-FIX-4.
  Other open notes are covered by P08-FIX-4. First unticked + unblocked: P08-FIX-6.

## Plan P08-FIX-6 (store_recording deadline inside the post-call budget)
Chosen fix (reviewer's option a): bound `store_recording` dynamically by the remaining budget instead of
certifying a constant bound. Worst cases (policy math, backoff ×2 from initial capped at maximum):
finalize_call_record 5×30 s + (1+2+4+8) = 165 s; bookkeeping (lookup/mark) 5×10 + (2+4+8+16) = 80 s each;
finalize_billing 10×30 + (1+2+4+8+10×5) = 365 s. Pre-store worst = 165 + 80 (mark) + 80 (lookup) + 600 (wait) +
80 (mark) = 1005 s. Reserve after store = billing 365 + 2 marks 160 = 525 s. Worst store window =
1800 − 1005 − 525 = 270 s > 0.

### Production changes
1. `packages/va_workflows/src/va_workflows/post_call/plan.py` (pure, workflow-safe; or a new `post_call/budget.py`
   if plan.py should stay step-only — keep imports to datetime + temporalio.common):
   - `def retry_worst_case(timeout: timedelta, policy: RetryPolicy) -> timedelta`: `maximum_attempts × timeout` +
     the sum of the `maximum_attempts − 1` backoff intervals (`initial × coeff^i`, each capped at
     `maximum_interval`). Raise `ValueError` for `maximum_attempts < 1` (unlimited) — never used for that.
   - `STORE_RESERVE: Final = retry_worst_case(BILLING_TIMEOUT, BILLING_RETRY) + 2 * retry_worst_case(
     BOOKKEEPING_TIMEOUT, BOOKKEEPING_RETRY)` (store mark + billing mark).
   - `def store_window(started: datetime, now: datetime, budget_s: int) -> timedelta | None`: time left until
     `started + budget − STORE_RESERVE`; `None` when ≤ 0 (or < 1 s).
2. `post_call_workflow.py` `_store_recording`: `window = store_window(workflow.info().start_time, workflow.now(),
   timings.budget_s)` (thread `timings` through `_recording_steps` → `_store_recording`). `None` → `_begin` is NOT
   called... simpler: `_begin("store_recording")`, then if `window is None`: `self._recording = "skipped"`,
   `workflow.logger.warning("postcall.store_recording_out_of_time")`, append to `_failed` (or `_skipped`; pick
   `_skipped` and do not list it in `_ran`, i.e. check before `_begin`) and return. Otherwise pass
   `schedule_to_close_timeout=window` alongside start_to_close/retry policy. Temporal stops retrying when the next
   attempt (incl. a `next_retry_delay`) would pass schedule-to-close → ActivityError → existing `_fail` path
   (`recording = "skipped"`). Verify `workflow.info().start_time` is the workflow (not run) start under
   time-skipping; for continue-as-new irrelevant (none here).
   - If the time-skipping test server does NOT honour schedule_to_close against a `next_retry_delay` (test 3
     below fails), fallback: add optional `deadline: datetime | None` to `StoreRecordingInput`, and in
     `RecordingActivities._lock` raise non-retryable `RecordingBusy`-out-of-time (new type name in
     `activities.errors`, e.g. `RECORDING_OUT_OF_TIME`) when `now + delay > deadline`. Document whichever lands.
3. Docstring of the module (budget paragraph) mentions the store deadline.

### Tests
- `packages/va_workflows/tests/test_post_call_plan.py` (or new `test_post_call_budget.py`):
  - `retry_worst_case` exact values: RECORD (165 s), BOOKKEEPING (80 s), BILLING (365 s), a 1-attempt policy =
    timeout only, cap applied.
  - `store_window`: full window at start (1800 − 525), None after deadline, None exactly at deadline.
  - True budget property: `retry_worst_case(RECORD) + 3 × retry_worst_case(BOOKKEEPING) + RECORDING_WAIT_S +
    STORE_RESERVE < POSTCALL_BUDGET_S` (the worst-case pre-store path still leaves a store window).
- `packages/va_workflows/tests/test_recording_busy_retry.py`: DELETE `test_busy_retries_fit_the_post_call_budget`
  (false claim) — keep the `COPY_LOCK_TTL_MS > STORE_RECORDING_TIMEOUT` assertion as its own small test.
- `packages/va_workflows/tests/test_post_call_workflow.py` (time skipping, `postcall_rig`):
  - `test_a_store_refused_busy_past_the_window_fails_before_the_budget`: `FakePostCallActivities(store=retryable(
    BUSY_MESSAGE, RECORDING_BUSY, next_retry_delay=timedelta(minutes=6, seconds=1)), send={"finalize_call_record":
    [recording_event(start)]})`, default timings; result: `timed_out is False`, `"store_recording" in failed`,
    `recording == "skipped"`, `finalize_billing` in `names()` and `price_micros` set, store attempts count 4
    (0, 361, 722, 1083 s < 1275 s; 1444 s would pass it) — assert ≥ 2 and that the last store call happened before
    `1800 − STORE_RESERVE` workflow seconds if measurable from history; at minimum the outcome fields.
  - `test_no_store_window_left_skips_the_copy`: recording signal arrives late so the window is gone, e.g.
    `PostCallTimings(budget_s=STORE_RESERVE_s + 5)` with the event signalled after the wait... simplest: timings
    budget_s = int(STORE_RESERVE.total_seconds()) (window ≤ 0 at once); store never called, `recording ==
    "skipped"`, billing runs, `timed_out` false. Check `PostCallTimings.budget_s` has no lower bound beyond gt=0.
  - Existing workflow tests with short budgets (budget of a few seconds + `block`) now hit the "no window" branch
    when they expected store to run/block: check every `PostCallTimings(budget_s=...)` use and adjust only where the
    test was about store (e.g. a budget-cancel-during-store test should block in `finalize_billing` instead, or use
    a budget > reserve). Do not weaken asserts.

### Specs/docs
- specs/18-post-call-analysis.md §1: `> Decision (2026-11-04) P08-FIX-6:` store_recording runs with
  schedule_to_close = budget deadline − reserve (billing + 2 marks worst case from the policies, 525 s), skipped when
  no window remains; worst-case math above.
- specs/06 §11 Decision P08-FIX-1: amend the sentence claiming the busy retries fit the budget → refer to P08-FIX-6.
- AGENTS.md dated line: `va_workflows.post_call` `retry_worst_case`/`store_window`; short-budget workflow tests skip
  the store step.

### Gates
- `uv run pytest packages/va_workflows -q`, then `make check` (background + heartbeat Monitor, PATH +=
  /home/ralph/.local/bin, ~25 min). No e2e (post-call worker path unchanged in flows; default budget leaves the
  full window).
- Reviewer: tick P08-FIX-6, append `2026-11-04-p08-fix-2-budget-test.md` to `.ralph/review-inbox/DONE`, commit
  `fix(workflows): bound store_recording by the post-call budget` + `Task: P08-FIX-6`.

### Risks
- Time-skipping server and schedule_to_close vs next_retry_delay (fallback above).
- `workflow.info().start_time` vs history start: use it, deterministic on replay.
- Workflow code change = replay compatibility of running post-call workflows: dev only, acceptable (no versioning
  needed pre-production); mention in the Decision.

## Build P08-FIX-6 (2026-11-04)
- New `va_workflows.post_call.budget` (`retry_worst_case`, `STORE_RESERVE` = 365 + 2×80 = 525 s, `MIN_STORE_WINDOW` 1 s,
  `store_window`). `_store_recording(event, timings)`: no window → skipped (`postcall.store_recording_out_of_time`,
  `recording = skipped`, not in `ran`); else `schedule_to_close_timeout=window`. The time-skipping server honours it
  against `next_retry_delay` (no fallback needed): 361 s busy delays → 4 attempts, then step failed, billing runs, no timeout.
- Tests: test_post_call_budget.py (policy worst cases, default cap, unlimited, reserve, window edges, slowest pre-store
  path leaves 270 s); 3 workflow tests (busy past window, no window, scheduled timeout), all 3 fail without the
  workflow change; replaced the false budget test with `test_a_live_holder_never_outlives_its_lock`. Rig gained
  `PostCallRun.schedule_to_close`/`history_span`. Existing short-budget tests never reach store (unchanged).
- Specs: specs/18 §1 Decision P08-FIX-6; specs/06 §11 P08-FIX-1 Decision amended. AGENTS.md line.
- Gates: make check exit 0 (12676 passed, 7 skipped); radon avg 3.04 post_call; jscpd 0 clones in va_workflows.

## Review P08-FIX-6 (2026-11-05) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12676 passed, 7 skipped, cov 99 %).
- Diff matches the plan and specs/18 §1 Decision P08-FIX-6; reserve math (365 + 2×80) and window (1800 − 1005 − 525 = 270 s)
  verified by pure tests; skip branch checks the budget first; specs/06 §11 amended. Inbox note appended to DONE.

## Planner 2026-11-05 — after P08-FIX-6 approval
- Inbox: new note `2026-11-04-postcall-store-window.md` (not in DONE, no task; non-blocker): store_recording never
  heartbeats, so a schedule_to_close timeout leaves the attempt running and it may commit the recording after
  `call.completed` reported `recording = skipped`; tiny windows (MIN_STORE_WINDOW 1 s) start doomed copies.
  Inserted P08-FIX-7 above P08-FIX-4. Other open notes stay covered by P08-FIX-4.

## Plan P08-FIX-7 (no commit after the store deadline)
Chosen fix: explicit deadline in the input (deterministic, testable with ActivityEnvironment; no reliance on the
SDK delivering a timeout to a non-heartbeating activity) + larger minimum window. Heartbeating is not needed.

### Production changes
1. `packages/va_workflows/src/va_workflows/activities/recording_inputs.py`:
   - `StoreRecordingInput.commit_by: datetime | None = None` (docstring: the copy deadline the workflow set; `None` =
     no deadline, e.g. old histories/other callers). Import `datetime`.
   - `COMMIT_MARGIN: Final = timedelta(seconds=5)` (export in `__all__`): an attempt commits only while
     `now + COMMIT_MARGIN <= commit_by` (room for the UPDATE + event insert before the server times it out).
2. `activities/errors.py`: `RECORDING_OUT_OF_TIME: Final = "RecordingOutOfTime"` with docstring ("the copy's deadline
   passed; never retried, the workflow bills without the copy") + helper or inline `ApplicationError(...,
   type=RECORDING_OUT_OF_TIME, non_retryable=True)`.
3. `activities/recording.py`:
   - `_late(inp) -> bool`: `inp.commit_by is not None and self._clock.now() + COMMIT_MARGIN > inp.commit_by`.
   - In `_store_recording`, after the `already_stored` check and before `_lock`: if `_late(inp)` → log
     `recording.out_of_time stage=start` and raise the non-retryable error (no lock, no download).
   - In `_copy`, after `_upload` and before `_record`: if `_late(inp)` → `await self._delete_object(key)`, log
     `recording.out_of_time stage=commit`, raise the error (carrier copy kept; `finally` releases the lock).
   - Module docstring: one paragraph on the deadline.
   - Note: the raise must NOT go through `_failed` (no `call.error recording_failed` event: the call simply has no
     copy, as with the workflow-side skip). ApplicationError propagates out of `store_recording` (only `_Failed`
     is caught).
4. `post_call/budget.py`: `MIN_STORE_WINDOW = timedelta(seconds=60)` (docstring: a copy that cannot realistically
   finish is not started; the activity's own deadline check covers an attempt that still overruns). Keep worst-case
   math (window after slowest steps 1-2 is 270 s ≥ 60 s; existing test asserts > MIN?) — verify
   `test_post_call_budget.py` edges still hold (they use MIN_STORE_WINDOW symbolically; line 71 asserts window ==
   MIN exactly at deadline − MIN: fine).
5. `post_call/post_call_workflow.py` `_store_recording`: `commit_by = workflow.now() + window`, pass
   `StoreRecordingInput(..., commit_by=commit_by)`. Except branch unchanged (ActivityError → skipped/failed).
   Module docstring sentence.

### Tests
- `packages/va_workflows/tests/test_recording_activity.py` (reuse rig: `_call`, `_run`, `serve_recording`,
  `saved_input`, `MemoryS3`; clock = rig's SystemClock → use commit_by relative to real now, or pass a clock):
  1. `test_a_copy_past_its_deadline_is_not_started`: `commit_by = now + 2 s` (< margin) → ApplicationError type
     RECORDING_OUT_OF_TIME, non_retryable; `s3.uploads == 0`, no download request in the sim, key NULL, no
     call_events rows, lock key absent.
  2. `test_a_copy_that_overruns_its_deadline_deletes_its_object_and_does_not_commit`: deadline passes during the
     copy — make the deadline pass between upload and commit deterministically: wrap/fake `MemoryS3.upload` hook or
     use a clock whose `now()` advances (e.g. a test clock returning real now for the first check and now+10 min
     after the upload; simplest: `commit_by = now + 6 s` and a MemoryS3 upload hook that advances a `FrozenClock`
     past it — check what MemoryS3 supports; add an `on_upload` callback to MemoryS3 in recording_rig if needed).
     Assert: error RECORDING_OUT_OF_TIME, `s3.deleted == [key]`, key NULL, no `call.recording_ready`/`call.error`
     event, carrier recording not deleted (FakeTelnyx recording still present / no DELETE in rig.sent), lock released.
  3. `test_a_deadline_with_room_stores_normally`: `commit_by = now + 10 min` → `stored` (guards against inverted
     comparison).
  Existing tests without `commit_by` keep passing (None = no deadline).
- `test_post_call_workflow.py` (postcall_rig, time skipping):
  4. `test_a_store_window_below_the_minimum_skips_the_copy`: budget such that the window is between 1 s and 59 s
     (e.g. `PostCallTimings(budget_s=int(STORE_RESERVE.total_seconds()) + 30)`), store never called, `recording ==
     "skipped"`, billing runs, `timed_out` false. (Check this branch is reachable with how the existing "no window"
     test sets budget.)
  5. Extend the existing window test (the one asserting `schedule_to_close`) to assert the store input's
     `commit_by == workflow start + budget − STORE_RESERVE` (FakePostCallActivities records inputs? if so read it;
     compare to history start time) — or ≈ within 1 s.
- `test_post_call_budget.py`: assert `MIN_STORE_WINDOW == timedelta(seconds=60)` and the slowest pre-store path still
  leaves ≥ MIN_STORE_WINDOW.

### Specs/docs
- specs/18 §1: append to Decision P08-FIX-6 (or new `> Decision (2026-11-05) P08-FIX-7:`): minimum window 60 s;
  `commit_by` deadline with 5 s margin; an attempt past it is non-retryable `RecordingOutOfTime`, deletes its upload,
  keeps the carrier copy, records no event; the `call.completed` `recording = skipped` stays truthful.
- AGENTS.md dated line: `StoreRecordingInput.commit_by` / `COMMIT_MARGIN`, `RECORDING_OUT_OF_TIME`.

### Gates
- `uv run pytest packages/va_workflows -q`, then `make check` (background + heartbeat Monitor, PATH +=
  /home/ralph/.local/bin). No e2e (default budget never approaches the deadline in flows).
- Reviewer: tick P08-FIX-7, append `2026-11-04-postcall-store-window.md` to DONE, commit
  `fix(workflows): never commit a recording copy after its deadline` + `Task: P08-FIX-7`.

### Risks
- Adding a field to a workflow input changes the payload only (pydantic default None) — old histories replay fine.
- Clock source: the activity uses the injected `Clock` vs the workflow's `workflow.now()` (server time). Both UTC wall
  clocks; the 5 s margin absorbs skew. Under the time-skipping test server workflow.now() is skipped time: workflow
  tests use FakePostCallActivities, so no real activity compares clocks there.

## Build P08-FIX-7 (2026-11-05)
- `StoreRecordingInput.commit_by` + `COMMIT_MARGIN` 5 s; `errors.RECORDING_OUT_OF_TIME` + `recording_out_of_time()`
  (non-retryable). `RecordingActivities._late`: checked after parse/url (before lock) and after upload (deletes object,
  no event, carrier copy kept, lock released by finally). `MIN_STORE_WINDOW` 60 s. Workflow passes
  `commit_by=workflow.now() + window`.
- Tests: 3 activity tests (start-stage refusal, mid-copy overrun via FrozenClock advanced in `before_upload`, room →
  stored); workflow window test asserts `commit_by` ≈ start + 1275 s; no-window test parametrized (0 s, 30 s left);
  budget test `MIN_STORE_WINDOW == 60 s` + slowest path ≥ MIN. Rig `activities(clock=)`. New tests fail without src
  (imports). specs/18 §1 Decision P08-FIX-7; AGENTS.md line.
- Gates: make check exit 2 = only `apps/sim/tests/test_sim_shared_redis.py::test_two_sim_processes_share_scenarios_and_reset`
  (assert (1,0)==(0,0); passes 3/3 alone; sim untouched) — 12680 passed, 7 skipped, cov 99.07 %. va_workflows 340 passed;
  lint/typecheck/placeholders exit 0 after the clone refactor; radon avg 2.69; jscpd 0 clones in va_workflows; pip-audit
  no known vulns; pnpm audit 1 moderate (no high). Reviewer: consider a FIX task for the sim flake if it recurs.

## Review P08-FIX-7 (2026-11-05) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12681 passed, 7 skipped; the sim
  shared-redis flake did not recur).
- Diff matches the plan and specs/18 §1 Decision P08-FIX-7: late check before lock (after parse/url) and after upload
  (delete object, no event, lock released in finally), `commit_by = workflow.now() + window`, MIN_STORE_WINDOW 60 s.
  Tests cover start/commit stages, room case, below-minimum skip, and commit_by value. Inbox note appended to DONE.

## Planner 2026-11-05 — after P08-FIX-7 approval
- Inbox: `2026-11-04-p08-fix-6-store-window-review.md` is not in DONE and had no task (non-blocker: STORE_RESERVE is
  hard-wired, undercounts once analysis steps 4-10 land; also document that schedule-to-start latency is excluded).
  Inserted P08-FIX-8 above P08-FIX-4. Other open notes (fix-3 / underrun) are covered by P08-FIX-4.

## Plan P08-FIX-8 (reserve derived from the steps after store_recording)
### Production changes
1. `packages/va_workflows/src/va_workflows/post_call/plan.py`:
   - `IMPLEMENTED: Final[frozenset[PostCallStep]] = frozenset({"finalize_call_record", "await_recording",
     "store_recording", "finalize_billing", "emit_completed"})` (docstring: steps `planned_steps` can plan; a new step
     joins here). Export in `__all__`. `planned_steps` keeps its logic; add an assertion-free consistency: nothing
     else (tests check `planned_steps` output ⊆ IMPLEMENTED).
2. `packages/va_workflows/src/va_workflows/post_call/budget.py`:
   - `STEP_WORST_CASE: Final[Mapping[PostCallStep, timedelta]] = MappingProxyType({"finalize_billing":
     retry_worst_case(BILLING_TIMEOUT, BILLING_RETRY)})` — worst case of each step that may run after the copy
     (docstring: every IMPLEMENTED step between `store_recording` and `emit_completed` needs an entry; `emit_completed`
     runs regardless of the budget so it has none).
   - `MARK_WORST_CASE: Final = retry_worst_case(BOOKKEEPING_TIMEOUT, BOOKKEEPING_RETRY)`.
   - `def steps_after_store(implemented: Iterable[PostCallStep] = IMPLEMENTED) -> tuple[PostCallStep, ...]`: the
     steps of `POSTCALL_STEPS` strictly between `store_recording` and `emit_completed` that are in `implemented`.
   - `def reserve_for(steps: Iterable[PostCallStep]) -> timedelta`: `MARK_WORST_CASE` (the copy's own mark) +
     Σ (`STEP_WORST_CASE[step]` + `MARK_WORST_CASE`); raises `KeyError`-free `ValueError(f"no worst case for {step}")`
     when a step lacks an entry (so a missing entry fails at import, not silently).
   - `STORE_RESERVE: Final = reserve_for(steps_after_store())` (still 525 s). Docstring updated: derived, and it
     excludes schedule-to-start latency (a worker backlog can still push billing past the budget; the budget timer then
     ends the run and `emit_completed` reports it as today).
   - Update `__all__`. Imports `POSTCALL_STEPS` from va_domain.post_call, `IMPLEMENTED` from plan (no cycle: plan does
     not import budget). Keep module workflow-safe (no I/O).
### Tests (`packages/va_workflows/tests/test_post_call_budget.py`, `test_post_call_plan.py`)
- `test_the_reserve_covers_billing_and_two_marks` stays (525 s).
- `test_steps_after_the_copy_are_billing_today`: `steps_after_store() == ("finalize_billing",)`.
- `test_every_step_after_the_copy_has_a_worst_case`: for each step in `steps_after_store(IMPLEMENTED)`, key in
  `STEP_WORST_CASE` (the guard).
- `test_a_new_step_without_a_worst_case_is_refused`: `reserve_for(steps_after_store(IMPLEMENTED | {"summarize"}))`
  raises ValueError naming `summarize`.
- `test_a_new_step_grows_the_reserve`: monkeypatch-free — call `reserve_for(("summarize", "finalize_billing"))` with a
  local mapping? Simpler: give `reserve_for(steps, worst_cases=STEP_WORST_CASE)` a keyword param; assert
  `reserve_for(("summarize","finalize_billing"), worst_cases={**STEP_WORST_CASE, "summarize": 40 s}) == 525 + 40 + 80`.
- `test_post_call_plan.py`: `test_planned_steps_are_implemented`: for facts combinations already used there (connected
  + recording + storage, None, not connected), `set(planned_steps(f)) <= IMPLEMENTED`; and every IMPLEMENTED step
  appears for the full-facts case.
### Specs/docs
- specs/18 §1: append `> Decision (2026-11-05) P08-FIX-8:` reserve = mark of the copy + (worst case + mark) of every
  implemented step between store_recording and emit_completed, from `STEP_WORST_CASE`; a step without an entry fails
  at import/test; reserve excludes schedule-to-start latency (worker backlog), accepted: the budget timer still ends the
  run and emit_completed reports.
- AGENTS.md: one dated line (`budget.STEP_WORST_CASE`/`reserve_for`, `plan.IMPLEMENTED`: a new post-call step needs both).
### Gates
- `uv run pytest packages/va_workflows -q`; `make lint typecheck placeholders`; `make check` (background + heartbeat
  Monitor, PATH += /home/ralph/.local/bin). No e2e.
- Reviewer: tick P08-FIX-8, append `2026-11-04-p08-fix-6-store-window-review.md` to DONE, commit
  `refactor(workflows): derive the store_recording reserve from the following steps` + `Task: P08-FIX-8`.
### Risks
- Workflow import path: budget.py is imported inside `imports_passed_through`; new imports must stay pure (plan.py and
  va_domain.post_call are). `MappingProxyType` is fine for mypy (`Mapping[PostCallStep, timedelta]`).

## Build P08-FIX-8 (2026-11-05)
- `plan.IMPLEMENTED` (5 steps); `budget.MARK_WORST_CASE` (80 s), `STEP_WORST_CASE` (finalize_billing 365 s),
  `steps_after_store(implemented)`, `reserve_for(steps, worst_cases=)` (ValueError for a missing entry);
  `STORE_RESERVE = reserve_for(steps_after_store())` = 525 s, docstring notes schedule-to-start exclusion.
- Tests: budget (steps after copy, guard, mark, missing-entry refusal, outside steps, growth 525+40+80), plan
  (planned ⊆ IMPLEMENTED, full facts == IMPLEMENTED). New tests fail without src (imports).
- specs/18 §1 Decision P08-FIX-8; AGENTS.md line.
- Gates: make check exit 0 (12692 passed, 7 skipped, cov 99.07 %); lint/typecheck/placeholders 0 findings; radon
  post_call avg 3.04; jscpd 0 clones va_workflows; pip-audit no known vulns; pnpm audit 1 moderate (no high).

## Review P08-FIX-8 (2026-11-05) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` run 1: 1 failed
  (`test_prefetch_activity.py::test_the_context_of_a_queued_call_is_stored_with_its_ttl`, fakeredis TTL 5399 vs 5400:
  wall-clock flake in an untouched file; passes alone); run 2 exit 0 (12692 passed, 7 skipped, cov 99 %).
  Flake tracked as runtime task `fix:prefetch-ttl-flake` (planner: consider a FIX task).
- Diff matches plan + specs/18 §1 Decision P08-FIX-8: reserve derived (525 s unchanged), missing worst case raises at
  import, plan ⊆ IMPLEMENTED tests. Inbox note appended to DONE.

## Planner 2026-11-05 — after P08-FIX-8 approval
- Inbox: two new notes (`2026-11-05-p08-fix-8-reserve-guard-review.md`, `2026-11-05-p08-fix-8-reserve-review.md`),
  both non-blockers about FIX-8: IMPLEMENTED is a second hand-kept list; no guard that the reserve leaves a copy
  window. Inserted P08-FIX-9 above P08-FIX-4. Also added P08-FIX-10 for the runtime task `fix:prefetch-ttl-flake`
  (fakeredis TTL 5399 vs 5400) so it gets a plan slot.

## Plan P08-FIX-9 (one source for planned steps + copy-window guard)
### Production changes
1. `packages/va_workflows/src/va_workflows/post_call/plan.py`:
   - Private predicates (pure, `CallRecordFacts | None -> bool`): `_always(_)` → True; `_with_facts(f)` → `f is not None`;
     `_recorded(f)` → `f is not None and f.connected and f.recording_wanted and f.recording_storage`.
   - `_PLAN: Final[Mapping[PostCallStep, Callable[[CallRecordFacts | None], bool]]] = MappingProxyType({
     "finalize_call_record": _with_facts, "await_recording": _recorded, "store_recording": _recorded,
     "finalize_billing": _always, "emit_completed": _always})` (docstring: the ONE table; a new step joins here with its
     condition, and if it runs between store_recording and emit_completed it needs a `budget.STEP_WORST_CASE` entry).
   - `IMPLEMENTED: Final = frozenset(_PLAN)` (docstring: derived, never edited by hand).
   - `planned_steps(facts)` = `tuple(step for step in POSTCALL_STEPS if (cond := _PLAN.get(step)) is not None and cond(facts))`
     (same results as today; docstring unchanged in substance).
   - Imports: `Callable`, `Mapping` from collections.abc, `MappingProxyType`, `Final`. Workflow-safe (no I/O).
2. `packages/va_workflows/src/va_workflows/post_call/budget.py`:
   - `PRE_STORE_WORST_CASE: Final = retry_worst_case(RECORD_TIMEOUT, RECORD_RETRY) + 3 * MARK_WORST_CASE +
     timedelta(seconds=RECORDING_WAIT_S)` (docstring: step 1, its mark, the recording lookup (bookkeeping policy — check
     what the existing test assumes: it uses 3 x bookkeeping worst case = mark 1 + lookup + wait mark), the full recording
     wait, the wait's mark; today 1005 s). Import `RECORD_RETRY`, `RECORD_TIMEOUT` from post_call_inputs and
     `POSTCALL_BUDGET_S`, `RECORDING_WAIT_S` from va_domain.post_call.
   - `def check_copy_window(*, before_store: timedelta, reserve: timedelta, budget_s: int) -> None`: raises
     `ValueError(f"the post-call budget of {budget_s} s leaves no store_recording window: {before_store} before the copy,
     {reserve} reserved after it")` when `before_store + MIN_STORE_WINDOW + reserve > timedelta(seconds=budget_s)`.
   - Module level after `STORE_RESERVE`: `check_copy_window(before_store=PRE_STORE_WORST_CASE, reserve=STORE_RESERVE,
     budget_s=POSTCALL_BUDGET_S)` (import fails loudly if a future step eats the window; tests catch it first).
   - `STORE_RESERVE` docstring: the sum assumes the post-copy steps run one after another (as `_before_completion` does);
     steps run in parallel would reserve their max instead.
   - Add `PRE_STORE_WORST_CASE`, `check_copy_window` to `__all__`. Module docstring: one sentence on the import guard.
### Tests
- `packages/va_workflows/tests/test_post_call_plan.py`:
  - `_all_flags_on() -> CallRecordFacts`: build from `CallRecordFacts.model_fields`: every field whose annotation is
    `bool` → True; assert the remaining field names == {"done_steps"} (so a new non-bool gating field forces an update).
  - `test_every_flag_on_plans_exactly_the_implemented_steps`: `set(planned_steps(_all_flags_on())) == IMPLEMENTED`.
  - `test_implemented_is_the_plan_table`: `IMPLEMENTED == frozenset(plan._PLAN)` is tautological — instead assert
    `IMPLEMENTED == frozenset({the five steps})` (pins today's set; a change is deliberate).
  - Keep the existing parametrized tests (behaviour unchanged); drop/merge `test_every_implemented_step_is_planned_for_a_recorded_call`
    into the all-flags test.
- `packages/va_workflows/tests/test_post_call_budget.py`:
  - `test_the_pre_store_worst_case`: `PRE_STORE_WORST_CASE == timedelta(seconds=1005)`; rewrite
    `test_the_slowest_path_to_the_copy_still_leaves_a_window` to use `PRE_STORE_WORST_CASE` (window == 270 s, ≥ MIN).
  - `test_a_reserve_that_eats_the_window_is_refused`: `check_copy_window(before_store=PRE_STORE_WORST_CASE,
    reserve=timedelta(seconds=POSTCALL_BUDGET_S) - PRE_STORE_WORST_CASE - MIN_STORE_WINDOW + timedelta(seconds=1),
    budget_s=POSTCALL_BUDGET_S)` raises ValueError matching "no store_recording window"; exactly at the edge (without +1 s)
    returns None.
  - `test_the_default_budget_keeps_a_copy_window`: `check_copy_window(... STORE_RESERVE ...)` returns None (explicit).
### Specs/docs
- specs/18 §1: amend the P08-FIX-8 Decision sentence ("A step implemented later ... makes reserve_for raise at import")
  to be exact, or append `> Decision (2026-11-05, P08-FIX-9):` — `planned_steps` reads one table keyed by step, whose
  keys are `IMPLEMENTED`, so a plannable step is always in the reserve's input and one without a `STEP_WORST_CASE`
  entry fails at import; an all-flags-on facts test plans exactly `IMPLEMENTED`; `budget` refuses at import when
  `PRE_STORE_WORST_CASE` (1005 s) + `MIN_STORE_WINDOW` + `STORE_RESERVE` exceeds `POSTCALL_BUDGET_S`; the reserve sums
  the post-copy steps because they run in sequence (parallel steps would reserve their max).
- AGENTS.md: amend the P08-FIX-8 line (or add a dated line): new post-call step = an entry in `plan._PLAN` (condition) +
  `budget.STEP_WORST_CASE` when after the copy; `budget` import fails when the copy window disappears.
### Gates
- `uv run pytest packages/va_workflows -q`; `make lint typecheck placeholders`; `make check` (background + heartbeat
  Monitor, PATH += /home/ralph/.local/bin). No e2e. radon/jscpd as usual.
- Reviewer: tick P08-FIX-9, append both 2026-11-05 note names to DONE, commit
  `refactor(workflows): plan post-call steps from one table and guard the copy window` + `Task: P08-FIX-9`.
### Risks
- Workflow sandbox: plan.py/budget.py are imported via `imports_passed_through`; new imports are pure. Module-level
  `check_copy_window` runs at import in the worker: it passes today (1005 + 60 + 525 = 1590 ≤ 1800).
- `CallRecordFacts.model_fields[...].annotation` with `from __future__ import annotations` in pydantic: pydantic resolves
  annotations, so `annotation is bool` works; verify.

## Build P08-FIX-9 (2026-11-05)
- `plan._PLAN` (step → condition: `_with_facts`/`_recorded`/`_always`), `IMPLEMENTED = frozenset(_PLAN)`,
  `planned_steps` reads the table (same results). `budget.PRE_STORE_WORST_CASE` (1005 s), `check_copy_window(...)`
  called at import (1005 + 60 + 525 ≤ 1800); STORE_RESERVE docstring notes sequential sum.
- Tests: plan `_all_flags_on()` from `CallRecordFacts.model_fields` (non-bool fields must be {"done_steps"}) plans
  exactly IMPLEMENTED; IMPLEMENTED pinned to the five steps. Budget: PRE_STORE value, slowest path window 270 s, default
  budget passes, edge passes / edge+1 s refused, short budget (1500 s) refused. New tests fail without src (imports).
- specs/18 §1 Decision P08-FIX-9; AGENTS.md dated line.
- Gates: make check exit 0 (12697 passed, 7 skipped, cov 99.07 %); lint/typecheck/placeholders 0 findings; va_workflows
  356 passed; radon post_call avg 2.81; jscpd 0 clones va_workflows; pip-audit no known vulns; pnpm audit 1 moderate.

## Review P08-FIX-9 (2026-11-05) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12697 passed, 7 skipped, cov 99.07 %).
- Diff matches plan + specs/18 §1 Decision P08-FIX-9: `_PLAN` table drives `planned_steps` (same results),
  IMPLEMENTED derived; all-flags test reads `CallRecordFacts.model_fields` (3 bools + done_steps verified);
  PRE_STORE_WORST_CASE 1005 s, `check_copy_window` at import (1590 ≤ 1800), edge/edge+1 s/short-budget tests.
  Both inbox notes appended to DONE. Next: P08-FIX-10 (prefetch TTL flake).

## Planner 2026-11-05 — after P08-FIX-9 approval
- Inbox: every note is in DONE or covered by P08-FIX-4. Next unticked: P08-FIX-10 (runtime task `fix:prefetch-ttl-flake`).

## Plan P08-FIX-10 (wall-clock-free TTL assertions)
### Cause
fakeredis stores expiry as wall-clock deadline; `TTL` floors the remaining seconds, so if ≥ 1 ms... (any time) passes
between SET EX and TTL under load, `TTL` reads 5399. Exact `ttl == N` asserts are flaky. Same pattern exists in
4 places: `packages/va_workflows/tests/test_prefetch_activity.py:75` (the reported flake),
`packages/va_db/tests/test_call_context.py:120`, `packages/va_events/tests/test_publisher.py:61`,
`packages/va_llm/tests/test_embedding_cache.py:34`. Fix all four (same root cause; no production change).
### Helper (new) `packages/va_common/tests/redis_ttl.py` (dir already on pytest pythonpath + mypy mypy_path)
```python
TTL_SLACK_MS: Final = 30_000  # generous: a loaded xdist worker never stalls this long between write and read
async def assert_expires_in(redis: Redis-like (Any with pttl), key: str, ttl_s: int, *, slack_ms: int = TTL_SLACK_MS) -> None:
    remaining = await redis.pttl(key)
    assert ttl_s * 1000 - slack_ms < remaining <= ttl_s * 1000, f"{key}: pttl {remaining} ms, expected ≤ {ttl_s} s"
```
Type `redis` as `redis.asyncio.Redis` (FakeAsyncRedis subclasses it). The upper bound proves the key was set with
at most `ttl_s` (a longer TTL fails), the lower bound proves it was set with more than `ttl_s - 30 s` (e.g. 5400 vs
3900 still distinguished; -1 no-expiry / -2 missing fail).
### Changes
- prefetch test: `await assert_expires_in(redis, call_context_key(call.call_id), 5400)`; keep `result.ttl_s == 5400`
  and the log `ttl_s` asserts (those are exact values the store was asked to set — the intent).
- test_call_context: `assert context.ttl_s() == 3900` + `await assert_expires_in(redis, key, context.ttl_s())`.
- test_publisher: `await assert_expires_in(rig.redis, key, CALL_SEQ_TTL_S)` (the expire(5) before still proves refresh:
  5 s < CALL_SEQ_TTL_S - 30 s — check CALL_SEQ_TTL_S value; if it is < 60 s pass a smaller `slack_ms` above 5 s).
- test_embedding_cache: `assert QUERY_CACHE_TTL_S == 86_400` + helper.
- Unit test for the helper in `packages/va_common/tests/test_redis_ttl.py` (fakeredis): key set EX 100 passes;
  key set PX 100_000 - 31_000 (i.e. 69 s) fails AssertionError; key without expiry fails; missing key fails;
  key with EX 101 fails (upper bound).
- AGENTS.md: dated line "assert Redis TTLs with `redis_ttl.assert_expires_in(redis, key, ttl_s)`; never `ttl == N`
  (fakeredis floors a wall-clock deadline)".
- grep once more `ttl(.*) ==` / `pttl(.*) ==` across packages/apps/tests_e2e tests to catch any other exact assert.
### Gates
`uv run pytest packages/va_common/tests/test_redis_ttl.py packages/va_workflows/tests/test_prefetch_activity.py
packages/va_db/tests/test_call_context.py packages/va_events/tests/test_publisher.py packages/va_llm/tests/test_embedding_cache.py -q`;
`make lint typecheck placeholders`; `make check` (background + heartbeat Monitor, PATH += /home/ralph/.local/bin).
Reviewer: tick P08-FIX-10, close runtime task `fix:prefetch-ttl-flake`, commit `test: assert redis ttls without
wall-clock races` + `Task: P08-FIX-10`.
### Risks
Helper module name must be unique on the shared pythonpath (`redis_ttl` — check no clash). Never mutate fakeredis time.

## Build P08-FIX-10 (2026-11-05)
- Helper `packages/va_common/tests/redis_ttl.py` (`assert_expires_in(redis, key, ttl_s, slack_ms=30_000)`, PTTL window)
  + `test_redis_ttl.py` (7 cases: exact, inside slack, shorter, longer, no expiry -1, missing -2, custom slack).
- Replaced exact `ttl == N` in 6 tests (the plan's 4 + two more found by grep: `apps/api/tests/test_failure_counter.py`
  restart-window test, `apps/api/tests/test_pathway_chat_store.py`). Bounded `0 < ttl <= WINDOW_S` asserts left as is.
- AGENTS.md dated line. No production change.
- Gates: make check exit 0 (12704 passed, 7 skipped, cov 99.07 %); lint/typecheck/placeholders 0 findings; radon redis_ttl avg 2.0; jscpd 0 clones.

## Review P08-FIX-10 (2026-11-05) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12704 passed, 7 skipped, cov 99.07 %).
- Diff: helper + 7 unit tests; 6 call sites switched. Every TTL used (600 s, 3900, 5400, 86 400, 172 800) is well above
  the 30 s slack, so the `expire(5)` refresh tests still prove the refresh. No remaining exact `ttl(...) ==` asserts (grep).
  Runtime task fix:prefetch-ttl-flake closed.

## Planner 2026-11-05 — after P08-FIX-10 approval
- Inbox: new note `2026-11-05-redis-ttl-helper-small-ttl.md` (non-blocker on the FIX-10 helper). Inserted P08-FIX-11
  above P08-FIX-4. Other undone notes are all P08-FIX-4's.

## Plan P08-FIX-11 (TTL helper: short TTLs must not accept -1/-2)
### Change
- `packages/va_common/tests/redis_ttl.py` `assert_expires_in`: `lower = max(0, ttl_s * 1000 - slack_ms)`;
  `assert lower < remaining <= ttl_s * 1000` (message unchanged in form: `"{key}: pttl {remaining} ms, expected within
  ({lower}, {ttl_s * 1000}] ms"` — keep the `"<key>: pttl <n>"` prefix the tests match on). Docstring: for TTLs at or
  below the slack the window is `(0, ttl_s]`: the key must exist and expire, but the length is only bounded above.
  Also raise `ValueError` when `ttl_s <= 0` (meaningless call) — optional, skip if it adds no test value; keep simple.
### Tests (`packages/va_common/tests/test_redis_ttl.py`)
- `test_a_short_ttl_passes`: set EX 10, `assert_expires_in(redis, KEY, 10)` passes.
- `test_a_missing_key_fails_for_a_short_ttl`: `pytest.raises(AssertionError, match="pttl -2")` with ttl_s=10.
- `test_a_key_without_expiry_fails_for_a_short_ttl`: plain SET, ttl_s=10 → match "pttl -1".
- `test_a_longer_ttl_fails_for_a_short_ttl`: EX 11 vs 10 → AssertionError.
- Optionally parametrize over `ttl_s` in (10, 100) to keep jscpd at 0 instead of duplicating bodies
  (parametrize with fixed ints only — never random).
- Verify the new missing/no-expiry cases fail against the old helper (revert line, run, restore).
### Docs
- AGENTS.md P08-FIX-10 line: append "short TTLs (≤ 30 s) are only bounded above; the key must still exist and expire".
### Gates
- `uv run pytest packages/va_common/tests/test_redis_ttl.py -q`; `make lint typecheck placeholders`; `make check`
  (background + heartbeat Monitor; PATH += /home/ralph/.local/bin). No e2e (test-only change).
- Reviewer: tick P08-FIX-11, append `2026-11-05-redis-ttl-helper-small-ttl.md` to DONE, commit
  `test: refuse missing keys in short redis ttl asserts` + `Task: P08-FIX-11`.
### Risks
- None for callers: all six use TTLs ≥ 600 s, window unchanged for them.

## Build P08-FIX-11 (2026-11-05)
- `assert_expires_in`: window `(max(0, ttl_s*1000 - slack_ms), ttl_s*1000]`; message `"<key>: pttl <n> ms, expected within (lo, hi] ms"`.
- Tests: missing/no-expiry parametrized over ttl_s 10 and 100; short TTL passes; EX 11 vs 10 fails. The ttl_s=10
  missing/no-expiry cases fail against the old helper (verified with git stash), pass now (11 passed).
- AGENTS.md P08-FIX-10 line amended. Test-only change.
- Gates: make fmt/lint/typecheck/placeholders exit 0 (0 findings); make test exit 0 (12708 passed, 7 skipped, cov 99 %).

## Review P08-FIX-11 (2026-11-05) — APPROVED
- Re-ran: `make lint typecheck placeholders` exit 0 (0 findings); `make test` exit 0 (12708 passed, 7 skipped, cov 99 %).
- Diff: window `(max(0, ttl-slack), ttl]`; short-TTL missing/no-expiry/longer cases tested; callers (TTLs ≥ 600 s)
  unaffected. Inbox note appended to DONE.

## Planner 2026-11-05 — after P08-FIX-11 approval
- Inbox: every note is in DONE except the seven P08-FIX-4 notes. Next unticked: P08-FIX-4.
- Item (5) of P08-FIX-4 is already satisfied: specs/07 §9 Decision P08-FIX-3 contains the measured before/after numbers
  (3 of 8 runs had 1-33 underruns, p50 125-147 ms → 0 underruns, p50 101-137 ms, lag p99 ≤ 25 ms). Record that
  in the evidence. Do not change it.

## Plan P08-FIX-4 (gc_pause / underrun log hygiene)
### 1. `packages/va_common/src/va_common/gc_tuning.py`: no logging inside the collector
- `GcPauseLog.__init__(*, threshold_ms=..., clock=time.perf_counter, capacity: int = PAUSE_BUFFER = 256)`.
  `_callback` only does this: time the pass, and when ≥ threshold, `self.pauses += 1` and append the tuple
  `(generation, round(duration_ms, 1), collected, uncollectable)` to `self._pending: deque` (maxlen=capacity). When
  the deque is full before the append, `self.dropped += 1` (the deque discards the oldest). No logger call and no
  dict building beyond the info reads.
- `flush() -> int`: `contextvars.Context().run(self._emit)`. In a fresh, empty context, pop everything and
  `_log.warning("gc_pause", generation=, duration_ms=, collected=, uncollectable=)` for each tuple. If `dropped`
  is non-zero, also log one `_log.warning("gc_pause_overflow", dropped=n)` and reset it. Returns the number of
  `gc_pause` lines.
- `async def run(self, *, interval_s: float = FLUSH_INTERVAL_S (1.0), sleep: Callable[[float], Awaitable[None]] =
  asyncio.sleep) -> None`: `try: while True: await sleep(interval_s); self.flush()` `finally: self.flush()` (on cancel).
- The module global `_installed: GcPauseLog | None`. `tune_gc` sets it, and `installed_pause_log() -> GcPauseLog |
  None` returns it. Add `installed_pause_log`, `FLUSH_INTERVAL_S` and `PAUSE_BUFFER` to `__all__`. Update the module
  docstring: the callback only records, a supervised task logs outside any call context.
- Wiring:
  - `apps/media_worker/src/va_media/main.py` lifespan, next to `supervisor.start("loop_lag", ...)`:
    `pauses = installed_pause_log()` and, when not None, `supervisor.start("gc_pauses", pauses.run)`. `tune_gc()` stays
    in `main()`.
  - `apps/sim/src/va_sim_server/main.py` `run()`: inside the outer TaskGroup, when `installed_pause_log()` is not None,
    `gc_task = group.create_task(pauses.run(), name="sim-gc-pauses")`, and cancel it next to `subscriber.cancel()`.
  - Tests that call `tune_gc` must reset `gc_tuning._installed` with monkeypatch (check `test_gc_tuning.py`,
    media `test_main.py`, sim `test_sim.py`).
- Tests in `packages/va_common/tests/test_gc_tuning.py`. Rewrite the first test as follows:
  - Call the callback start/stop with no log line yet, then `flush()` == 1 with the line and fields as before.
  - `test_gc_pause_is_logged_outside_the_call_context`: `bind_call_context(org_id=..., call_id=...)` (clear it in
    finally with `structlog.contextvars.clear_contextvars()`), trigger the callback, `flush()`, and assert there is
    no `call_id`/`org_id` key in the line.
  - `test_overflow_keeps_the_latest_and_reports_the_dropped`: capacity=2 with 3 pauses gives 2 lines plus
    `gc_pause_overflow dropped=1`.
  - `test_run_flushes_each_interval_and_on_cancel`: inject a `sleep` that waits on a queue/event gate. Record one pause,
    release one sleep, and assert the line exists. Record another, cancel the task, and assert the final flush logged it.
  - `test_tune_gc_...` also asserts `installed_pause_log() is pauses`.
### 2. `packages/va_voice/src/va_voice/outbound.py`: the underrun log
- `@dataclass(frozen=True, slots=True) class SendTiming: lock_wait_ms: float; socket_ms: float`, and
  `@runtime_checkable class TimedSink(Protocol)` with `@property def last_audio_timing(self) -> SendTiming | None`.
  Export both. In `__init__`, `self._timed = sink if isinstance(sink, TimedSink) else None` (a single check).
- Fields are computed BEFORE the send in `_underrun_fields(now, tracker) -> dict[str, float | int | None]`:
  `turn` (tracker.turn), `behind_ms`, `since_prev_send_ms`, `prev_send_await_ms`, `prev_lock_wait_ms`,
  `prev_socket_send_ms`, `prev_resume_ms` (= max(0, await − lock − socket): the event loop resuming the task after
  the send completed, i.e. loop lag; all three are None without a TimedSink), `prev_controls_await_ms` (time awaited
  in `send_mark` since the previous frame), `wake_late_ms`, and `queued_frames`. After the send, store the previous
  frame's values: `_last_send_await_ms` and `_last_timing = self._timed.last_audio_timing if self._timed else None`.
- Emit only in the `if starved:` block AFTER the generation check, next to `stats.underruns += 1` (clear during a held
  send → no log, no count).
- Controls: in `_send_controls`, time each `await self._sink.send_mark(name)` with `_now_ms()` and add it to
  `self._controls_await_ms`. `_send_frame` resets it to 0.0 right after building the fields (whether starved or not;
  that is, every frame starts a new accumulation window after its snapshot).
- `_due_ms = None` on the idle branch before `await self._wake.wait()`.
- Throttle: `UNDERRUN_LOG_INTERVAL_MS: Final = 1000.0` (exported). Log the underrun when its tracker is not the last
  logged tracker (first underrun of an utterance) OR when ≥ interval has passed since the last logged line on this
  leg. Otherwise `self._suppressed += 1`. A logged line carries `suppressed=<count since previous line>`, then resets
  it. The line keeps the `outbound.underrun` warning name. `OutboundStats` gains `underruns_logged: int = 0` and
  `max_underrun_wake_late_ms: float = 0.0`, both updated for every counted underrun (max over the non-None
  wake_late values). `clear()` does not reset throttle state (it is per leg).
- `packages/va_voice/src/va_voice/pipeline/pipeline.py` `pipeline_ended`: add `underruns_logged=` and
  `max_underrun_wake_late_ms=round(..., 1)`.
- Module docstring: rewrite the underrun paragraph (logged after the send, next to the counter, throttled, fields).
### 3. `apps/media_worker/src/va_media/transport.py`: the transport's own timing
- `TelnyxMediaTransport(..., timer: Callable[[], float] = time.perf_counter)` (seconds). `_send(message, *, audio=False)`
  records `t0` before `async with self._send_lock`, `t1` once the lock is held, and `t2` after `send_text`. When
  `audio`, `self._last_audio_timing = SendTiming(lock_wait_ms=(t1-t0)*1000, socket_ms=(t2-t1)*1000)` (also set on a
  dropped/failed send, socket_ms covering whatever ran). Property `last_audio_timing`. `send_audio` passes
  `audio=True`. Import `SendTiming` from va_voice.outbound (va_media already depends on va_voice). Note in a docstring
  that `socket_ms` can still include loop stalls that happen inside the socket await, while lock wait and resume
  separate the rest.
- `test_transport.py`: `test_audio_send_timing_separates_lock_wait_from_socket_time`. Use a stepping timer, hold the
  lock with a `send_mark` whose FakeSocket `send_gate` is closed, start `send_audio`, release, and assert
  `lock_wait_ms`/`socket_ms` from the scripted timer. Marks do not overwrite `last_audio_timing`.
  `isinstance(transport, TimedSink)` is True.
### 4. `apps/media_worker/src/va_media/runtime.py` `_unwire` docstring
- State the real order. The exit stack is LIFO, so `_unwire` runs after the carrier-watch task and the voicemail
  monitor stop, which were registered after it, and BEFORE the brain close, the vendor socket closes and
  `persistence.finish_call`, which were registered earlier. A takeover or transfer outcome that arrives from those
  later closes is dropped (`media.voicemail_unbound`), which is harmless because the call is over. No order change.
### Tests in `packages/va_voice/tests/test_outbound_audio.py` (reuse `_underrun_rig`, `RecordingSink`, `json_log_stream`)
- Update `test_an_underrun_after_a_slow_send_names_the_send_await`. It still has await 200. Add `turn == 1`,
  `suppressed == 0`, and `prev_lock_wait_ms is None` (RecordingSink is untimed).
- `test_a_clear_during_a_held_starved_send_logs_and_counts_nothing`. Hold the sink on the starved frame (as in
  the slow-send test but call `out.clear()` while it is holding), release, and expect `underruns == 0` with no
  underrun log.
- `test_a_held_mark_is_named_in_the_underrun_log`. Use two segments: push 10 frames, `end_segment`, then push 10
  more. Hold `send_mark` for 200 ms (RecordingSink: add a `mark_hold` event if it has none). The next frame
  underruns, and `prev_controls_await_ms ≈ 200` with a small `prev_send_await_ms`.
- `test_an_underrun_after_waking_from_idle_has_no_stale_wake_late`. Covers the `_due_ms` reset on the idle branch.
  Sleep with a due time, then go idle (queue empty, no bg), wake via push after a long advance, and check that a
  later underrun (via a slow send) has `wake_late_ms is None`. If a natural underrun cannot be built after idle
  (continuity breaks on idle), assert `out._due_ms is None` after idling instead. The Builder picks whichever is
  behavioural, with `_due_ms` as the fallback.
- `test_a_burst_of_underruns_logs_one_line_and_counts_all`. Use three stalled sends within 1 s of one utterance
  (hold/advance 0.2 s each), expect 3 underruns, 1 log line, `stats.underruns_logged == 1`, and
  `max_underrun_wake_late_ms` as computed. Then a stall after advancing 1.1 s logs a second line with
  `suppressed == 2`.
- `test_a_new_utterance_logs_its_first_underrun`. Two utterances within 1 s give 2 lines.
- `test_a_timed_sink_splits_the_send_await`. A RecordingSink subclass implements `last_audio_timing` (lock 5 ms,
  socket 1 ms) and advances the ManualClock 200 ms during send (the loop stall). Expect `prev_send_await_ms ≈ 200`,
  `prev_lock_wait_ms == 5`, `prev_socket_send_ms == 1` and `prev_resume_ms ≈ 194`.
- Pipeline: the `pipeline_ended` log carries `underruns_logged` and `max_underrun_wake_late_ms`. Extend an existing
  va_voice pipeline test that asserts `underruns` in pipeline_ended (grep `pipeline_ended`), or add one with
  `harness(on_underrun=)`.
### Specs/docs
- specs/07 §9: append `> Decision (2026-11-05) P08-FIX-4:` with these points. The underrun line is emitted after
  the send next to the counter, so lines never outnumber counts. A clear during the send logs nothing. It is
  throttled to the first underrun of an utterance and then at most one per second per leg, with `suppressed`. The
  new fields are `turn`, `prev_lock_wait_ms`, `prev_socket_send_ms`, `prev_resume_ms` (loop lag after the send),
  and `prev_controls_await_ms`. `pipeline_ended` adds `underruns_logged` and `max_underrun_wake_late_ms`. `gc_pause`
  is recorded in the callback into a bounded buffer (256, `gc_pause_overflow`) and logged each second by a
  supervised task in an empty context (no call fields). The `_unwire` order is as stated above. Also amend the
  P08-FIX-3 text "before the frame goes out" → "(superseded by P08-FIX-4: after the send)".
- AGENTS.md: amend the P08-FIX-3 line (underrun fields and the gc_pause flush task) or add a dated P08-FIX-4 line.
### Gates
- `uv run pytest packages/va_common/tests/test_gc_tuning.py packages/va_voice apps/media_worker/tests/test_transport.py
  apps/media_worker/tests/test_main.py apps/sim/tests/test_sim.py -q`. Then `make fmt lint typecheck placeholders`,
  then `make check` in the background with a heartbeat Monitor (PATH += /home/ralph/.local/bin).
- Capacity: `scripts/dev_up.sh e2e` (rebuild images), then `uv run pytest tests_e2e -m capacity -s` 3 times, each with
  0 underruns and p50 ≤ 140 ms. Collect `docker logs voiceagents-media-1 | grep -cE 'outbound.underrun|gc_pause'`
  and check that no gc_pause line carries `call_id`.
- Reviewer: tick P08-FIX-4, append all seven note names to `.ralph/review-inbox/DONE`, and commit
  `fix(media): log gc pauses and underruns outside the hot path` with the footer `Task: P08-FIX-4`.
### Risks
- `runtime_checkable` Protocol isinstance with a property only checks that the attribute exists. FakeTransport in
  va_voice tests lacks it, so it is untimed (fine).
- gc_cycles tests: do not store a bound method of the owner in the transport or the outbound clock. `_timed` is the
  sink itself (already held), and the timer is a plain function.
- tests that monkeypatch `tune_gc` must leave `_installed` clean (monkeypatch.setattr(gc_tuning, "_installed", None)).
- The throttle must not hide underruns from the metric. The count and the hook still fire for every underrun.

## Build P08-FIX-4 (2026-11-05)
- gc_tuning: callback only buffers (deque 256, `dropped` → `gc_pause_overflow`); `flush()` logs in an empty
  contextvars Context; `run(interval_s, sleep)` flushes each second + on cancel; `installed_pause_log()` set by
  `tune_gc`. Wired: media supervisor `gc_pauses`, sim TaskGroup `sim-gc-pauses` (cancelled next to the subscriber).
  Tests: gc_tuning (flush, context-free line verified failing without the empty Context, overflow, run/cancel),
  media test_health + sim test_sim wiring tests, test_main resets `_installed`.
- outbound: fields snapshotted before send, log after send next to the counter; clear during held starved send →
  no count/no log; throttle (first per utterance, then ≤1/s per leg, `suppressed`); `turn`, `prev_lock_wait_ms`,
  `prev_socket_send_ms`, `prev_resume_ms` (TimedSink), `prev_controls_await_ms`; `_due_ms` reset on idle;
  stats `underruns_logged`/`max_underrun_wake_late_ms` → pipeline_ended. `SendTiming`/`TimedSink` exported.
  Held-mark test: 210 ms (run_for(0.13) runs 10 ms past the 120 ms mark before the 200 ms advance).
- transport: `timer=` seam, `_send(audio=True)` records SendTiming (also on dropped sends), `last_audio_timing`.
- runtime `_unwire` docstring states the LIFO order. specs/07 §9 Decision P08-FIX-4 + P08-FIX-3 "superseded" note;
  AGENTS.md line. Item (5) of the note already satisfied by the P08-FIX-3 Decision numbers (unchanged).
- Gates: make fmt/lint/typecheck/placeholders exit 0 (0 findings); `make check` exit 0 (12721 passed, 7 skipped,
  cov 99.07 %, TS 22/52/48 passed).
- Capacity (stack rebuilt with `scripts/dev_up.sh e2e`), 3 runs: underruns 0/0/0, overhead p50 119/124/104 ms
  (p95 176/173/155), loop lag p99 ≤ 25 ms. Media+sim logs over the runs: 0 `outbound.underrun`, 0 `gc_pause`
  (no pass ≥ 10 ms happened), 0 gc lines with call_id; pipeline_ended carries the new fields in the container.

## Planner 2026-11-05 — build.blocked for P08-FIX-4 (backpressure)
- The code is done and the gates passed (Build P08-FIX-4 above). The rejection came from backpressure: the
  build.done text lacked the required tokens. No split is needed, and the code plan is unchanged.
- Builder: do not reimplement. Check that the working tree still holds the P08-FIX-4 diff (git status), then actually
  run and record the following (PATH += /home/ralph/.local/bin):
  - `make lint typecheck placeholders` and `make check` (background + heartbeat Monitor) → tests/lint/typecheck/coverage.
  - audit: `uv export --frozen --no-hashes --no-emit-workspace > /var/tmp/req.txt && uvx pip-audit -r /var/tmp/req.txt`
    + `corepack pnpm audit --audit-level high`.
  - duplication: `npx --yes jscpd@4 --silent` (must report 0 clones; fix any clone the new outbound/gc code introduced).
  - complexity: `uvx radon cc -s -a packages/va_voice/src/va_voice/outbound.py packages/va_common/src/va_common/gc_tuning.py apps/media_worker/src/va_media/transport.py`
    plus the repo-wide average; it must stay < 10. If `_send_frame`/`_underrun_fields` rank D or worse, extract helpers.
  - Emit WITHOUT --json, plain text containing: `tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass,
    duplication: pass, complexity: <n>, specs: pass` (put a comma after the number).

## Build P08-FIX-4 rerun (2026-11-05) — backpressure evidence
- jscpd had found 4 clones in the new test_outbound_audio.py tests. They now use the helpers `_speak`,
  `_wake_late_once` and `_hold_third_send`, and jscpd reports 0 clones.
- radon: `OutboundAudio._send_frame` was C (14). The `_next_wire` and `_underrun` helpers bring it down to B (9).
  The repo average is A (2.92).
- audit: pip-audit reports no known vulnerabilities. `pnpm audit --audit-level high` exits 0 (1 moderate only).
- lint, typecheck and placeholders exit 0. `make check` was rerun after the refactor (result below).
- make check exit 0: 12721 passed, 7 skipped; TS 22/52/48 passed.

## Review P08-FIX-4 (2026-11-05) — APPROVED
- Re-ran: make lint/typecheck/placeholders exit 0; make test 12721 passed, 7 skipped, cov 99.07 %; jscpd 0 clones;
  radon outbound `_send_frame` B (9). Diff matches the plan; the `_unwire` docstring was checked against the exit
  stack order in `runtime._converse` and is correct. The specs/07 Decision P08-FIX-4 is accurate. The 7 inbox notes
  were appended to DONE.

## Planner 2026-11-05 — after P08-FIX-4 approval
- Every inbox note is in DONE. The next unticked task is P08-FIX-5.

## Plan P08-FIX-5 (precall test robust to an LLM retry)
### Cause
- `apps/media_worker/tests/test_runtime_sim.py::test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks`
  unpacks `[opener_request] = r.state.llm.requests`. Under full-suite load the wall-clock gateway can time out the
  first attempt and retry it, which leaves 2 requests. The test is about ordering: the opener must not be requested
  before the last DTMF group plus the 500 ms settle. Any retry comes after the first attempt, so the earliest
  request is the bound that matters.
### Change (test only, no production change)
- Replace the unpack with:
  `requests = r.state.llm.requests; assert requests` (at least one).
- Every request is the same opener generation (a retry resends the same body). Assert this with
  `assert all(req.messages == requests[0].messages for req in requests)`, so a second, distinct generation still
  fails the test.
- Take `first = min(requests, key=lambda req: datetime.fromisoformat(req.received_at))` and check
  `(first.received_at - last_group) >= 0.5 s`, as before.
- Update the comment: "earliest attempt; a gateway retry under load resends the same request".
- Check the other single-request asserts in the same file for the same exposure (lines ~218 `rule_id == ["table"]`,
  ~347 `len == 1`, ~879 `[request] =`). Do NOT change them unless they share this exact failure mode. This task
  only covers the named test. If one does, give it the same treatment (dedupe identical retries via
  `messages`) and note it in the evidence.
### Test of the fix
- Run it 3× alone: `uv run pytest apps/media_worker/tests/test_runtime_sim.py -k precall -q --count` (if
  pytest-repeat is missing, loop 3 runs in a shell).
- Prove that a retry no longer fails it. Run once with a FakeLLM rule that makes the first attempt slow enough
  for the gateway to time out, if the rig's gateway deadlines allow it cheaply. Otherwise reason it from the code
  and record that in the evidence. Do not add sleeps.
### Gates
- `make fmt lint typecheck placeholders`, and `make check` in the background with a heartbeat Monitor
  (PATH += /home/ralph/.local/bin). For backpressure, also run audit (pip-audit + pnpm audit), `npx --yes jscpd@4
  --silent`, and the radon average. Emit with the plain-text tokens (`complexity: <n>,`).
- Reviewer: tick P08-FIX-5 and commit `test(media): tolerate an llm retry in the precall ordering test` with
  footer `Task: P08-FIX-5`.
### Risks
- `requests` ordering in the sim store follows append-at-receipt. Using `min` by `received_at` avoids relying on it.

## Build P08-FIX-5 (2026-11-05)
- `test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks` no longer unpacks exactly one request. It now
  asserts that at least one request exists, that every request has the same `messages` (so a retry of the opener
  is accepted but a second, distinct generation still fails), and it checks the 500 ms settle against the earliest
  `received_at`. The change is to the test only.
- No retry was reproduced. The argument comes from the code: a gateway retry resends the same body after the first
  attempt, so it adds a later identical request, and that keeps the bound on the earliest one intact. The other
  single-request asserts (lines 218, 337, 347) were left unchanged, as the plan scopes this task to the named test.
- Results: the test passed 3/3 when run alone. fmt, lint, typecheck and placeholders exit 0. `make check` exit 0
  (12721 passed, 7 skipped; TS 22/52/48). pip-audit is clean and pnpm audit exits 0. jscpd reports 0 clones. The
  radon average is A (2.92).

## Review P08-FIX-5 (2026-11-05) — APPROVED
- Re-ran: make lint/typecheck/placeholders exit 0; precall test alone passes; make test 12721 passed, 7 skipped,
  cov 99.07 %. Diff is test-only and matches the plan (identical-retry tolerance, earliest request bounds the settle).

## Planner 2026-11-05 — after P08-FIX-5 approval
- New inbox note `2026-11-05-p08-fix-5-precall-retry-review.md` (blocker): the messages-only dedupe accepts N
  identical opener generations. I checked the code: `va_llm/dialogue.py` `_fallback` gives a failed dialogue attempt
  exactly one more attempt on `platform_fallback` (the `fallback` model), and skips it when that target is the same as
  the primary. The defaults are LLM_DIALOGUE_MODEL GLM-5.3-Flash vs LLM_FALLBACK_MODEL Kimi-K2.6, which differ, so a
  legitimate retry is always on another model. Inserted P08-FIX-12 at the top of plan/phase-08.md.

## Plan P08-FIX-12 (bounded retry shape in the precall ordering test)
### Change (test only): `apps/media_worker/tests/test_runtime_sim.py` ~lines 311-321
```python
# The opener is generated (FakeLLM) only after the last group and the 500 ms settle. Under load the
# gateway may time out its first attempt and make one fallback-model attempt after a timed-out first
# attempt (specs/10 §2); anything else (a second generation, a third request) is a regression.
requests = sorted(r.state.llm.requests, key=lambda request: datetime.fromisoformat(request.received_at))
assert 1 <= len(requests) <= 2
if len(requests) == 2:
    assert requests[1].model != requests[0].model
    assert requests[1].messages == requests[0].messages
first = requests[0]
```
Keep the `first_group`/`last_group` asserts and `(first.received_at - last_group) >= 0.5 s`.
- If it reads more cleanly, extract `_opener_attempts(requests) -> LlmRequest` (a module-level helper that
  returns the first request after asserting the shape). That makes the shape unit-testable without a sim. Add
  `test_opener_attempts_accept_one_fallback_attempt_only` over hand-built `va_sim.llm.store.LlmRequest`
  objects. Check the constructor fields in `packages/va_sim/src/va_sim/llm/store.py:55`, and build them via
  `dataclasses.replace` on a base. The cases: 1 request passes; 2 requests on different models with the same
  messages pass and the earlier one is returned; 2 requests on the same model fail (AssertionError); 3 requests fail;
  2 requests with different messages fail. This proves the acceptance criteria ("a duplicate on the same model fails").
  Use `pytest.raises(AssertionError)`. Recommended. If LlmRequest is awkward to build, use a small local frozen
  dataclass with `received_at`/`model`/`messages` and type the helper with a Protocol.
- Lines 218 (`rule_id == ["table"]`) and 352 (`len == 1`) stay unchanged. They have not flaked, and the note asks
  for the bounded shape only if they get the retry treatment.
### Gates
- `uv run pytest apps/media_worker/tests/test_runtime_sim.py -k "precall or opener_attempts" -q`, 3 times.
- `make fmt lint typecheck placeholders`, then `make check` in the background with a heartbeat Monitor
  (PATH += /home/ralph/.local/bin). For backpressure, also run audit (`uv export --frozen --no-hashes
  --no-emit-workspace` + `uvx pip-audit -r`, `corepack pnpm audit --audit-level high`), `npx --yes jscpd@4 --silent`
  (0 clones) and the radon average. Emit plain text with `complexity: <n>,`.
- Reviewer: tick P08-FIX-12, append `2026-11-05-p08-fix-5-precall-retry-review.md` to `.ralph/review-inbox/DONE`, and
  commit `test(media): bound the precall opener retry to one fallback attempt` with footer `Task: P08-FIX-12`.
### Risks
- A rig that sets the fallback model equal to the dialogue model makes the gateway skip the fallback. Then a timeout
  ends the reply with no retry, and that only gives one request, so the test still holds.

## Build P08-FIX-12 (2026-11-05)
- Changed `test_runtime_sim.py` only. The new module helper `opener_attempt(requests)` sorts requests by
  `received_at` and accepts 1 to 2 of them. If there are 2, the second must use a different model and the same
  messages. It returns the earliest request. The precall test uses it to check the 500 ms settle.
- New unit tests with hand-built `va_sim.llm.store.LlmRequest`: one request passes; a fallback attempt passes and
  the earlier request is returned; a repeat on the same model, a retry with different messages, three requests,
  or no requests all raise AssertionError.
- Results: `-k "precall or opener_attempt"` passed 3/3 (7 tests). fmt, lint, typecheck and placeholders exit 0.
  `make check` exit 0 (12727 passed, 7 skipped; TS 22/52/48). pip-audit is clean and pnpm audit exits 0. jscpd
  reports 0 clones. The radon average is A (2.92).

## Review P08-FIX-12 (2026-11-05) — APPROVED
- Re-ran: make lint/typecheck/placeholders exit 0; `-k "precall or opener_attempt"` 7 passed; make test 12727 passed,
  7 skipped, cov 99 %. Test-only diff matches the plan and the inbox note (same-model duplicate fails). Note added to DONE.

## Planner 2026-11-05 — after P08-FIX-12 approval
- All inbox notes are in DONE (checked file by file). The next unticked task is P08-T01 (deps P07-T07, P02-T11 ticked).
- Scope: `POST /v1/tools/{id}/test` needs request rendering, secret decryption and execution, which is ToolRunner work.
  It moves to P08-T02 (plan file amended). P08-T01 = CRUD, validation, secret headers, audit, reference-checked delete.

## Plan P08-T01 (Tools API with secret headers)
### Facts
- Model `va_db.models.library.Tool` already exists (DDL specs/03: UNIQUE (org_id, name) NOT partial, so a
  soft-deleted row keeps its name). Error `TOOL_NOT_FOUND` 404 exists. Audit actions `tool.create/update/delete` are
  already in `va_db.audit.AUDIT_ACTIONS`. Scopes `tools:read`/`tools:write` exist (rbac_matrix row `_grant("tools")`).
- `va_domain.calls.tools.InlineTool` already validates name regex, description 1-1000, speech ≤ 500, templated
  http(s) URL ≤ 2048, header-name tokens, query, body ≤ 64 KB, input_schema `type: object`, response dict,
  timeout_ms 100-60000 (default 10 000), retries 0-3. Reuse it, do not copy it.
- `va_engine.system_tools.RESERVED_TOOL_NAMES`, `va_tools.jsonpath.compile_path` (raises `InvalidJsonPathError`),
  `va_common.ids` (`PublicPrefix.TOOL`, `to_public`, `from_public`), `Envelope.encrypt_str/decrypt_str`.
- Encrypt BEFORE opening the tenant session, as `org_sso.py` does (`envelope_of(request).encrypt_str(...)`).
  `decrypt` needs no DB (the wrapped key is in the blob, DataKeyCache).

### va_tools (shared with the T02 runtime)
- `packages/va_tools/pyproject.toml`: add `jsonschema>=4.26` to dependencies (mypy types are already in the root dev
  group). Run `uv lock` and `uv sync`.
- `packages/va_tools/src/va_tools/secrets.py` (layout per specs/13 §1):
  - `secret_headers_aad(tool_id: uuid.UUID) -> str` → `f"tool:{tool_id}"` (spec §3).
  - `async encrypt_secret_headers(envelope, org_id, tool_id, headers: Mapping[str, str]) -> bytes | None` (empty → None;
    canonical JSON via `va_common.json.dumps`, sorted keys) and
    `async decrypt_secret_headers(envelope, org_id, tool_id, blob: bytes | None) -> dict[str, str]`.
  - `mask_secret(value: str) -> str` → `"••••" + value[-4:]` when `len(value) >= 12`, else `"••••"` (short secrets
    would be revealed mostly by their last 4). `mask_headers(headers) -> dict[str, str]`.
- `packages/va_tools/src/va_tools/validation.py` — pure, raises `va_common.errors.AppError("INVALID_INPUT", msg, field=)`:
  - `check_tool_definition(tool: InlineTool, *, secret_headers: Mapping[str, str] = {}, allow_http: bool) -> None`.
    Checks in this order: name not in `RESERVED_TOOL_NAMES` (case-sensitive, field `name`, message
    `"'<name>' is a reserved system tool name."`); url scheme `https://` unless `allow_http` (field `url`); a url that
    renders to no host (`https://{{x}}/` is allowed: templating may fill the host) is NOT checked here because runtime
    SSRF checks the rendered URL; `Draft202012Validator.check_schema(input_schema)` → `SchemaError` field
    `input_schema` with `error.message` (≤ 300 chars); every `response` value through `compile_path` → field
    `response.<var>`; every `response` key matches the variable regex `^[A-Za-z_][A-Za-z0-9_]{0,63}$` (field
    `response`); secret header names are RFC 7230 tokens (field `secret_headers`); a name in both `headers` and
    `secret_headers` (case-insensitive) → field `secret_headers.<name>`; secret header values non-empty, ≤ 4 096 chars,
    no CR/LF (field `secret_headers.<name>`).
  - Also export `HEADER_NAME` if needed. Do not duplicate the InlineTool token regex: import it, or expose it as a
    public name in va_domain (`HEADER_NAME_PATTERN`) and use it in both places.

### va_db
- `packages/va_db/src/va_db/repositories/tools.py` → `ToolRepo(session, org_id, *, clock)` built on `TenantRepository(Tool, ...)`
  (re-export from `va_db.repositories`):
  - `get(tool_id) -> Tool` (`NotFoundError` → the router maps it to `TOOL_NOT_FOUND`; or raise
    `AppError("TOOL_NOT_FOUND", "Tool not found.")` directly, which is preferred and what `call_inputs.refs` uses).
  - `page(*, limit, cursor, search) -> Page[Tool]`: `search` = ILIKE on name OR description, `%`/`_` escaped. Name the
    method `page`, not `list` (mypy issue noted in P07-T09a memory).
  - `create(values) -> Tool` and `update(tool, values) -> Tool`. A name conflict with a LIVE row →
    `AppError("CONFLICT", "A tool named '<n>' already exists.", field="name")` (use `conflict_savepoint`). A name held
    only by a soft-deleted row: hard-DELETE that row first, in the same transaction (Decision below).
  - `soft_delete(tool)`.
  - `references(tool_id) -> list[ToolReference(type, id, version)]`. Look in published pathway versions
    (`status='published'`, pathway not deleted), node `data.tools` or `global_config.globalTools`; published persona
    versions (`config.tools`, persona not deleted); and agent versions of non-deleted agents (`call_settings.tools`).
    Match both forms `TL-<uuid>` and `<uuid>` with jsonb containment (`nodes @> '[{"data":{"tools":[id]}}]'`). First
    check the real stored node shape in `packages/va_engine/tests/fixtures/bland/restaurant_ordering.json` and adjust
    the path if `tools` is not under `data`. Bind parameters with `sa.cast(literal_json, JSONB)`; never use text
    interpolation. At most 20 references returned.

### api
- `apps/api/src/va_api/routers/tools_schemas.py`:
  - `ToolCreate(InlineTool)` adds `secret_headers: dict[str, str] = Field(default_factory=dict, repr=False)`, with
    a ToolCreate example from specs/13 §2 (secret value obviously fake, built so gitleaks/placeholders stay quiet).
  - `ToolUpdate`: every field optional (`None` default + `model_fields_set` decides what was sent). Explicit `null`
    is allowed only for `speech`, `body` and `secret_headers` (null = clear). Null for any other field →
    INVALID_INPUT.
  - `ToolView`: `tool_id` (`TL-…`), name, description, speech, url, method, headers, `secret_headers` (masked), query,
    body, input_schema, `response` (from `response_map`), timeout_ms, retries, created_at, updated_at.
  - Responses `ToolCreated {status, tool_id, tool}`, `ToolEnvelope {status, tool}`, `ToolList {status, tools,
    next_cursor}`, `ToolDeleted {status, message: "Tool deleted."}`. Each has an `examples` entry
    (`test_operations_contract`).
- `apps/api/src/va_api/services/tools.py`. Merge for update: take the stored definition (an InlineTool dump of the
  row) plus the sent fields, then re-validate with `ToolCreate.model_validate(merged)`. That re-runs the cross-field
  checks; map pydantic errors with `field_path` exactly as other services do. `allow_http = settings.va_env in
  {"local","test"}` (find the existing helper, e.g. `va_pathways.runs.insecure_urls_allowed`, and reuse it).
  `secret_headers` on update replaces the whole set (`{}` or null removes all; omitted keeps it). Encrypt before the
  session. `tool_view(row, secret_headers) -> ToolView`. Decrypt after reading, masked only.
- `apps/api/src/va_api/routers/tools.py`: `tools_router = legacy_router(prefix="/v1/tools")`, added to `main.ROUTERS`,
  tag `Tools` (check it is in `va_api.openapi.TAG_NAMES`, add it if not). Routes:
  - `POST ""` → `tools_create`, 200, `require_scope("tools:write")`, `audited("tool.create")`.
  - `GET ""` → `tools_list` (`search` ≤ 200, `limit` 1-100 default as pathways, `cursor`).
  - `GET /{tool_id}` → `tools_get`.
  - `POST /{tool_id}` → `tools_update`, `audited("tool.update")`.
  - `DELETE /{tool_id}` → `tools_delete`, `?force=false`, `audited("tool.delete")`. Without force, any references
    give 409 `CONFLICT` with message `"Tool is used by published pathways, personas, or agents; pass force=true to
    delete it."` and `details={"references":[...]}`.
  - `tool_id` path parameter: accept `TL-<uuid>` or a bare uuid. Anything else → 400 INVALID_INPUT field `tool_id`.
    Responses always use the `TL-` form.
  - Audit `before`/`after` = views with masked secret headers (never plaintext). A no-op update (nothing changed)
    writes no audit row and leaves the row untouched.
- Cross-tenant: add a `tool_id` factory in `apps/api/tests/support/cross_tenant_factories.py` (ToolRepo create, returns
  the `TL-` string), and add `(404, "TOOL_NOT_FOUND")` to `cross_tenant.REFUSED`. The create body comes from the
  ToolCreate example (`examples[0]`).
- `make openapi` → commit `openapi/openapi.json`. Then `corepack pnpm --filter dashboard gen:api` and
  `corepack pnpm --filter @voiceagents/sdk gen:api` (schema drift tests).

### Tests
- `packages/va_tools/tests/test_secrets.py`: round trip; AAD binding (a blob of tool A fails to decrypt as tool B →
  `DecryptionError`); empty → None; mask rules (long/short/exact 12). Use `crypto_fakes` + local KMS as other tests do.
- `packages/va_tools/tests/test_validation.py`: one parametrized case per rule above (reserved name each of the 8,
  http refused / allowed, bad schema e.g. `{"type":"object","properties":{"a":{"type":"nope"}}}`, bad JSONPath, bad
  variable key, secret header token/dup/CRLF/empty/too long) plus a valid spec example passing.
- `packages/va_db/tests/test_tool_repo_db.py` (module DB fixture as other repo tests): create/get/page (search,
  escaping `%`, cursor), live name conflict → CONFLICT, recreate a soft-deleted name succeeds (old row gone), rename
  onto a live name conflicts, references for each of the 3 sources × both id forms, draft pathway versions and
  deleted parents are ignored.
- `apps/api/tests/test_tools_api.py` (session helpers / `store_api_key` with `tools:*`):
  - create returns `TL-` id + masked secrets, the DB `secret_headers_enc` decrypts to the plaintext, and the response
    body / `audit_rows` never contain the plaintext secret (assert the raw response text and audit JSON).
  - every validation error path gives field + code (each pydantic field bound, reserved name, schema, JSONPath, http
    refused under a non-local `va_env`; build the app with `va_env="production"` settings only if cheap, otherwise
    unit-test the allow_http flag in the service).
  - get / list (search, limit, cursor, deleted hidden). Update: partial, explicit null clears speech/body/secrets,
    null on name → 400, secrets omitted kept, secrets replaced, conflict, no-op = no audit.
  - Delete: no refs → 200 and then GET 404 `TOOL_NOT_FOUND`; referenced → 409 with `details.references`;
    `force=true` deletes. Malformed id → 400.
  - Scopes: a `tools:read` key cannot write (403); an analyst session can manage tools only if the rbac matrix says
    so (check the MATRIX row values; `_MANAGE` for sessions).
  - Idempotency: a create replay with the same `Idempotency-Key` returns the same tool_id (one row).
- The cross-tenant suite + audit-marker suite + `test_openapi` cover the new routes automatically. Run them.

### Specs/docs
- specs/13 §2: `> Decision (2026-11-05) P08-T01:` covering:
  1. Masking: last 4 shown only for values ≥ 12 chars.
  2. Update semantics: null clears speech/body/secret_headers; secret_headers replaces the whole set.
  3. A name held by a soft-deleted tool is freed by hard-deleting that row (the specs/03 UNIQUE is not partial).
  4. Reference check sources and both id forms; 409 `CONFLICT` with `details.references`.
  5. Path id accepts `TL-` or a bare uuid.
  6. Header name collision rule.
  7. `/test` arrives with the runtime (P08-T02).
- AGENTS.md dated line: router/service/repo names, the cross-tenant factory, and the secrets helpers.
- plan/phase-08.md: already amended (`/test` in T02).

### Gates
- Targeted: `uv run pytest packages/va_tools packages/va_db/tests/test_tool_repo_db.py apps/api/tests/test_tools_api.py
  apps/api/tests/test_cross_tenant_routes.py apps/api/tests/test_audit_markers.py apps/api/tests/test_openapi.py -q`.
- `make fmt lint typecheck placeholders`, then `make check` in the background with a heartbeat Monitor
  (PATH += /home/ralph/.local/bin).
- Backpressure: audit (`uv export --frozen --no-hashes --no-emit-workspace > /var/tmp/req.txt && uvx pip-audit -r
  /var/tmp/req.txt`, `corepack pnpm audit --audit-level high`), `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc
  -s -a` (average < 10). Emit plain text with `complexity: <n>,`.
- Reviewer: tick P08-T01 and commit `feat(api): tools api with encrypted secret headers` with footer `Task: P08-T01`.

### Risks
- gitleaks: test fixtures must not look like real tokens. Build them with `secrets.token_hex` at test time, and keep
  the OpenAPI example obviously fake (`"Bearer example-secret-value"`).
- `jsonschema` `check_schema` on huge or recursive schemas is cheap, but run validation in the request thread only:
  body ≤ 64 KB already caps it.
- The cross-tenant leak detector looks for ids in responses. A masked secret header's last 4 chars must not collide
  with markers; use `marker=False` only if needed.

## Build P08-T01 (2026-11-05)
- New: va_tools.secrets (AAD tool:{id}, mask ≥12 chars → ••••last4), va_tools.validation.check_tool_definition
  (reserved names, https unless local/test, Draft202012 check_schema, response keys + JSONPath, secret header rules);
  va_tools deps + jsonschema, va-domain. va_domain HEADER_NAME/VariableName public.
- va_db.repositories.ToolRepo (get/page/create/update/soft_delete/references; frees soft-deleted namesakes by DELETE;
  references = published pathway versions (node data.tools / globalTools), published persona versions (config.tools),
  any version of live agents (call_settings.tools), both id forms, ≤20).
- api: routers/tools.py + tools_schemas.py + services/tools.py; tools_router in main.ROUTERS. Update merges over
  stored definition, pydantic errors re-raised as RequestValidationError (field paths); no-op update writes nothing.
  force delete open to tools:write (recorded in Decision). Audit redactor blanks secret_headers key.
- Cross-tenant factory tool_id (TL- form, random name), REFUSED += TOOL_NOT_FOUND. openapi + both gen:api regenerated.
- Spec: specs/13 §2 Decision P08-T01. AGENTS.md line added.
- Tests: va_tools test_secrets/test_validation (36), va_db test_tool_repo_db (8), api test_tools_api (57).
- jscpd: fixed 4 clones (ToolsApi wraps pathway Api, add_all in repo test, compact repo __init__) → 0.
- make check exit 0: 12828 passed, 7 skipped, cov 99.08%; TS 22/52/48. pip-audit clean, pnpm audit exit 0 (1 moderate), jscpd 0, radon A (2.92).

## Review P08-T01 (2026-11-05) — APPROVED
- Re-ran: make lint/typecheck/placeholders exit 0; make test 12828 passed, 7 skipped, cov 99 % (exit 0).
- Diff checked against specs/13 §2/§3 + Decision P08-T01: legacy bodies, TL- ids, masking, null semantics,
  reference sources (persona config / agent call_settings are call-parameter layers → `tools` key), TOOL_NOT_FOUND
  in cross-tenant REFUSED, secrets never in responses/audit (tests assert raw text). Ticked and committed.
