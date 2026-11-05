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
