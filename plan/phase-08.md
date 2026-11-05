# Phase 08 — Tools, Dynamic Data, Custom Code, Knowledge Bases

- [x] P08-FIX-16 Pathway `end_call` always ends the call (final line not interruptible, an undelivered end is delivered on the next turn); tool tasks keyed by position — see .ralph/review-inbox/2026-11-05-p08-t02c1-recheck.md (items 1, 2; replaces 2026-11-05-p08-t02c1-engine-node-tools-review.md) — `end_call`/loop-protection final lines `Say(interruptible=False)`, owed `EndCallAction` re-delivered on the next turn (`_owed_end`), tool rounds by position (`start_tools` list, `round_contents`); Decision P08-FIX-16 (specs/13 §4); reviewer re-run: lint/typecheck/placeholders pass, check_spec_refs 0, jscpd 0 clones, make test 13030 passed 7 skipped (cov 99.07%); reviewer pipeline probe of the pre-audio merge on the goodbye ended `agent_hangup`
  - Spec refs: specs/13 §4 (Decision P08-T02c1), specs/11 §5.9 (Decision P07-T08b), specs/07 §8 (pre-audio merge)
  - Depends on: P08-T02c1
  - Deliverables: `PathwayEngine._tool_end` speaks the final line as a non-interruptible `Say`; `_protection`
    speaks the `errorPhrase` non-interruptibly (the unavailable-model line stays interruptible); an ended
    conversation whose `EndCallAction` was never yielded (the reply was closed first: pre-audio merge) yields its
    remaining final line + `EndCallAction` on the next `handle_user_turn` instead of nothing (tool `end_call`, End
    Call node, loop protection); `va_engine.tool_tasks.start_tools` returns one task per executed call by position,
    answers / variable merge / `interrupted` answers are tracked by position in `plan.calls` in both
    `run_node_round` and `TaskBrain._run_tools` (never by `call.id`); specs/13 §4 Decision `P08-FIX-16`; AGENTS.md.
  - Acceptance: engine tests: the `end_call` final line and the loop-protection line are `interruptible=False`;
    closing the generator on the final line then delivering a superseding turn yields the line + end (tool
    `end_call` and End Call node), a later turn yields nothing; pipeline harness test: a caller speaking over the
    `end_call` goodbye still ends `agent_hangup` with the line not interrupted; two tool calls with id `""` each
    run, get their own answer, and are both cancelled + answered `interrupted` on a barge-in (engine and
    TaskBrain); `make check` passes. The inbox notes go to DONE with P08-FIX-17 (it closes their items 3, 4).
- [x] P08-FIX-17 A superseded turn keeps the record of tools that already ran; a pathway node runs exactly the tools its refs name (one definition per tool name per node) — see .ralph/review-inbox/2026-11-05-p08-t02c1-recheck.md (items 3, 4), 2026-11-05-p08-t02c1-engine-node-tools-review.md (item 4) — `keep_tool_records` (engine + TaskBrain `_drop_turn`), TaskBrain carries `completed_rounds` behind the merged caller message, undelivered task-mode `transfer_call` answer rewritten to `interrupted`, `PathwayToolExecutor.for_refs` + `ToolOffer.executor` (first ref per name wins); Decision P08-FIX-17 (specs/13 §4); reviewer re-run: lint/typecheck/placeholders pass, check_spec_refs 0, jscpd 0 clones, pip-audit clean, make test 13052 passed 7 skipped (cov 99.08%); 4 reviewer mutations each fail the new tests; reviewer probe: a second `transfer_call` in the merged turn yields its `TransferRequest`
  - Spec refs: specs/13 §4 (Decisions P08-T02c1, P08-FIX-16), §5 (Decision P08-T02b1), specs/11 §5.9 (Decision P07-T08b), specs/07 §8
  - Depends on: P08-FIX-16
  - Deliverables: `PathwayEngine._drop_turn` keeps the superseded turn's `tool` history records ahead of the
    merged caller turn (context updates on the next request); `TaskBrain` carries the superseded turn's
    completed tool rounds (assistant `tool_calls` message without its unheard text + its `tool` answers) behind
    the merged caller message and keeps their `tool` records, so the next request shows what ran in a
    provider-valid shape. Planner decision (2026-11-05, DEC-012): same-name tools are settled by ref-based
    resolution, not a validation rule: `PathwayToolExecutor.for_refs(refs) -> ToolExecutor` replaces
    `definitions_for`; the node's executor offers, speaks for and executes exactly the definitions its refs
    name (first ref of a name wins within the node), so two nodes with different tools of one name (inline or
    stored) each run their own; `ToolOffer.executor`, `run_node_round(executor=, lines=)`,
    `ToolLines.line(executor, …)`; specs/13 §4 Decision `P08-FIX-17` (+ notes in Decision P08-T02c1 item 1,
    §5 Decision P08-T02b1, specs/11 §5.9); AGENTS.md.
  - Acceptance: engine and TaskBrain tests: a tool-only reply superseded before any audio leaves the tool
    result visible to the next request (request messages asserted; pathway: context update ahead of the merged
    turn; task mode: `user`, assistant `tool_calls`, `tool` in that order, pairing valid), also for a round
    answered `interrupted`; resolution tests: two nodes with different inline tools of one name each offer and
    run their own definition, and an inline tool named like a stored tool referenced earlier on the same node is
    offered once (the first ref) and that definition runs; `make check` passes; append both note names to
    `.ralph/review-inbox/DONE` on approval.
- [ ] P08-FIX-18 A lock lost (or a keepalive overrun) during the body teardown ends the session as worker_lost — see .ralph/review-inbox/2026-11-05-p08-fix-15-review.md
  - Spec refs: specs/07 §3 (Decision P05-T07a), specs/13 §6 (Decisions P08-FIX-14, P08-FIX-15), specs/07 §18
  - Depends on: P08-FIX-15
  - Deliverables: `CallSession._race` returns `OWNERSHIP_LOST` when the renewer lost the lock during the body
    teardown, and resolves the end as worker_lost when the teardown overran `TEARDOWN_KEEPALIVE_S` (the
    heartbeat expired, `CallWorkflow` ends the call), so the media end routine neither records nor hangs up
    in parallel with the workflow; Decision under specs/13 §6; AGENTS.md.
  - Acceptance: session tests: a renewer losing the lock while a gated body teardown runs → `run()` returns
    `OWNERSHIP_LOST` and the ender sees worker_lost; same for a teardown past `TEARDOWN_KEEPALIVE_S`; existing
    session tests pass; `make check` passes; append the note name to `.ralph/review-inbox/DONE` on approval.
- [x] P08-FIX-15 Bound the session keepalive during the body teardown; stop the heartbeat once ownership is lost — see .ralph/review-inbox/2026-11-05-p08-fix-14-review.md — `TEARDOWN_KEEPALIVE_S` 25 s bound via asyncio.wait without cancelling the teardown, `media.body_teardown_overran`, heartbeat cancelled by a renewer done callback; Decision P08-FIX-15; lint/typecheck/placeholders pass, make test 12969 passed 7 skipped (cov 99.06%)
  - Spec refs: specs/07 §3 (Decision P05-T07a), specs/13 §6 (Decision P08-FIX-14)
  - Depends on: P08-FIX-14
  - Deliverables: `CallSession._stop_tasks` keeps the renewer and heartbeat alive during the body teardown for at
    most `TEARDOWN_KEEPALIVE_S` (= `va_tools.executor.SYSTEM_DRAIN_S` + 5 s) on the injected clock; past the
    bound it logs `media.body_teardown_overran` (error), stops the keepers, and keeps awaiting the body; the
    heartbeat stops as soon as the renewer finished (ownership lost) during the race or the teardown; module
    docstring, specs/13 §6 Decision (new `P08-FIX-15` block) and AGENTS.md updated.
  - Acceptance: a body whose teardown never finishes within the bound stops refreshing the heartbeat after
    `TEARDOWN_KEEPALIVE_S` (the key expires after its TTL) and the lock stops being renewed, while the session
    still waits for the body; a lock lost during the teardown stops the heartbeat at once (no new stamp); the
    existing gated-teardown tests still pass; `make check` passes; append the note name to
    `.ralph/review-inbox/DONE` on approval.
- [x] P08-FIX-14 Keep heartbeat and ownership through the body teardown; callback start failure never reports a terminated child as scheduled — see .ralph/review-inbox/2026-11-05-p08-fix-13-review.md — two-stage CallSession stop (body teardown with renewer+heartbeat alive), callback start failure recorded before abandon (failed/advanced/ended/unrecorded), abandon -> bool 5 s; Decision P08-FIX-14; lint/typecheck/placeholders pass, make test 12968 passed 7 skipped
  - Spec refs: specs/07 §3 (Decision P05-T07a), specs/13 §6 (Decisions P08-T02b2, P08-FIX-13)
  - Depends on: P08-FIX-13
  - Deliverables: `CallSession._race` cancels and awaits the body, end-request and control listener first while
    the ownership renewer and heartbeat keep running, then cancels those two (a lock lost during the body
    teardown still skips the release); `CallbackScheduler._start` on a start failure records the child failure
    (from `scheduled`) BEFORE abandoning: `failed` → `abandon` (terminate the possible sleeper) + `callback_failed`;
    child already moved on by its workflow (non-terminal) → no terminate, answer scheduled; child ended
    (terminal) → no terminate, `callback_failed`; unrecorded → `abandon` + `media.callback_orphaned`;
    `CallbackStarter.abandon` returns whether a workflow was terminated (logged), its timeout 5 s so a callback
    run fits in `SYSTEM_DRAIN_S`; session module docstring and specs/13 §6 Decision updated.
  - Acceptance: a body whose teardown is gated keeps refreshing the heartbeat and renewing the lock until it
    finishes, and the ender starts only after it; start failure + child advanced to `queued` answers scheduled
    with no abandon; + child `canceled` answers `callback_failed` with no abandon; + child `scheduled` fails it
    and abandons after the transition; `make check` passes; append the note name to `.ralph/review-inbox/DONE` on
    approval.

- [x] P08-FIX-13 System tools: run to completion through barge-in, no tools for anonymous callers, per-call caps, orphan-free start failures — see .ralph/review-inbox/2026-11-04-p08-t02b2-system-tools.md, 2026-11-05-p08-t02b2-system-tools-review.md, 2026-11-05-p08-t02b2-recheck.md — shielded executor-owned system tool runs + aclose drain, dialable() gate, caps (3 SMS, 1 pending callback), abandon+retried fail on start failure; Decision P08-FIX-13; lint/typecheck/placeholders pass, make test 12964 passed 7 skipped (cov 99.06%)
  - Spec refs: specs/13 §6 (Decision P08-T02b2), specs/05 §3.3, specs/07 §8
  - Depends on: P08-T02b2
  - Deliverables: `CallToolExecutor` runs each system tool in a task it owns, awaited through `asyncio.shield`
    (a barge-in cancel stops the wait, never the side effect; the outcome is still logged/reported) and
    `aclose(timeout_s)` awaits the pending ones at call end (runtime exit stack); `send_sms`/`schedule_callback`
    built only when the other party's number is E.164 (`+…`), handlers refuse `no_callable_number`; caps: one
    pending (`scheduled`) callback child per parent, counted under the parent `FOR UPDATE` lock, and 3 SMS per call
    (sent or in flight), both answer `{"error":"limit_reached"}` without an error event; a failed callback start
    terminates the workflow id (best effort) then marks the child failed with one retry, logging
    `media.callback_orphaned` (error, child id) when it still cannot be recorded, docstring and code agree; the dead
    `created`/existing-child path of `va_db.callbacks` removed and docstrings made accurate; specs/13 §6 Decision
    updated.
  - Acceptance: closing the brain generator while the starter / Telnyx is gated leaves one child with a started
    workflow / one sent SMS and a later call reaches the cap; anonymous inbound context offers neither tool; second
    pending callback and fourth SMS answer `limit_reached`; start failure + DB failure on the first transition
    attempt still marks the child failed; `make check` passes; append the three note names to
    `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-12 Precall ordering test must still catch a duplicate opener generation — see .ralph/review-inbox/2026-11-05-p08-fix-5-precall-retry-review.md — `opener_attempt` helper: 1-2 requests by received_at, a second must be on another model with identical messages; unit tests refuse same-model repeat/other messages/3 requests/none; review make test 12727 passed, 7 skipped, cov 99 %; lint/typecheck/placeholders exit 0
  - Spec refs: specs/10 §2 (Decision P03-T13b: one attempt on the platform `fallback` model after a failed first attempt), specs/30 §3.4
  - Depends on: P08-FIX-5
  - Deliverables: replace the messages-only dedupe in
    `apps/media_worker/tests/test_runtime_sim.py::test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks`
    with the gateway's bounded retry shape: requests sorted by `received_at`, 1 ≤ count ≤ 2, a second request must be
    on a different `model` with identical `messages`; the 500 ms settle stays bounded on the first request; comment says
    "one fallback-model attempt after a timed-out first attempt". Test-only.
  - Acceptance: a duplicate opener generation on the same model (2 identical requests) or a third request fails the
    test; a fallback attempt passes; the test passes alone 3/3; `make check` passes; append the note name to
    `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-1 Recording copy lock + expired-link refresh, with per-recording object keys and a lock-aware busy retry — see .ralph/review-inbox/2026-11-04-recording-copy.md and .ralph/review-inbox/2026-11-04-recording-copy-lock.md — maintainer patch landed; `recording_copy_key` per-recording S3 key (loser deletes its own object unless same key); `RecordingBusy` next_retry_delay = lock PTTL + 1 s; specs/06 §11 rules 1/4 + Decision P08-FIX-1; review make check exit=0 (12641 passed, 7 skipped, cov 99%)
  - Spec refs: specs/06-telephony-telnyx.md §11 (Decision P05-T14 rules 1, 4, 5, 7); specs/03-data-model.md §11 (`call:{id}:reccopy`); specs/30-testing-and-evals.md §3.2
  - Depends on: none
  - Deliverables: land the maintainer patch `.ralph/review-inbox/2026-11-04-recording-copy.patch` (both notes carry the
    same patch); fix finding 1 (blocker): upload to a recording-specific key, write that key in the guarded `UPDATE`,
    delete the just-uploaded object when the `UPDATE` loses to a different key; fix finding 2: `RecordingBusy` carries
    `next_retry_delay` = the lock's remaining PTTL so the retry budget covers a live or crashed holder.
  - Acceptance: tests with two concurrent different recordings and a broken (Redis-down) lock assert the stored
    object's bytes match the row and the loser's object is deleted; a foreign-owner lock test asserts the busy
    error's `next_retry_delay` follows the lock TTL; spec rule 4 amended; `make check` passes.

- [x] P08-T00 Fix flaky e2e interruptibility_levels[1] (carried over from phase 07) — onset tolerance = one Silero window + policy `speech_ms`/`clear_delay_ms`/`level` on `call.interrupt`; edge accept `stages_ms` + `telnyx.inbound_accept_stages`; review: lint/typecheck/placeholders exit 0, make test 12646 passed / 7 skipped (cov 99 %), interruptibility_levels 10/10 standalone, make e2e all flows pass except pre-existing capacity flake (P08-FIX-3)
  - Spec refs: specs/07-media-pipeline.md §7 (interruptibility levels); specs/30-testing-and-evals.md §5 (e2e flows)
  - Depends on: none
  - Deliverables: root cause of `tests_e2e/test_interruptibility_levels.py` case [1] failing 2/4 standalone runs on
    2026-11-04 (clear ~1183 ms after voice onset vs `min_speech_ms` 1200; suspect onset reference = utterance
    `start_ms` + `MARKER_MS` vs Silero onset timing). Fix the cause (measurement reference or pipeline speech_ms), no
    retries/sleeps. Also check the single `amd_ivr_mode` missing-second-DTMF-digit flake after a cold rebuild.
    Inbox finding (non-blocker) folded in, because it reviews this task's own working tree — see
    .ralph/review-inbox/2026-11-04-p08-t00-accept-stages.md: `InboundAccept.accept` logs
    `telnyx.inbound_accept_stages` (call_id, org_id, reason `timeout`|error type, `stages_ms`, `last_stage`,
    `elapsed_ms`) on the budget timeout and the exception branch before `_cleanup`; the router keeps owning
    `telnyx.inbound_accept_failed` (specs/06 §7). The note's spec check (specs/06 §7, specs/30 §5 Decisions) is
    already satisfied by the working tree.
  - Acceptance: case [1] passes 10/10 standalone runs (`uv run pytest tests_e2e -m e2e -k interruptibility_levels`);
    `make e2e` passes except `test_concurrent_calls` (`-m capacity`), whose underrun / overhead-p50 failures are
    pre-existing at HEAD (images rebuilt at HEAD 2026-11-04: 3/8 runs with 14/3/1 underruns, passing p50 131-146 ms)
    and are fixed by P08-FIX-3; an `accept_rig` + `GatedClock.fire(ACCEPT_BUDGET_S)` test asserts the failure log names the
    last finished stage; append `2026-11-04-p08-t00-accept-stages.md` to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-3 50-call capacity on one media worker: platform underruns and overhead p50 margin — root cause: finished calls were reference cycles freed in 40-75 ms gen-0 GC passes (plus default-GC sim stalls); callbacks no longer capture owners, runtime unwires, tracebacks cleared, prebuilt flush SQL, GC (10_000, 5, 50) in media + sim, `outbound.underrun`/`gc_pause` attribution logs; 10/10 capacity runs 0 underruns p50 101-137 ms, review 4/4 runs 0 underruns p50 115-128 ms; make test 12661 passed / 7 skipped (cov 99 %), lint/typecheck/placeholders exit 0, make e2e 20 passed
  - Spec refs: specs/07-media-pipeline.md §9 (Decision P05-T17e underruns), §20; specs/30-testing-and-evals.md §5
    (concurrent_calls flow); specs/00-overview.md §5; MASTER_PROMPT §7 ("50 concurrent simulated calls per media-worker
    pod without underruns", overhead ≤ 150 ms p50)
  - Depends on: P08-T00
  - Deliverables: root cause of `tests_e2e/test_concurrent_calls.py::test_concurrent_calls_on_one_media_worker` failing
    at HEAD (2026-11-04: 3/8 runs `assert underruns == 0` with 14/3/1 underruns; passing runs overhead p50 131-146 ms,
    one review run 151 ms vs `OVERHEAD_P50_MS` 150). Profile media worker and sim under the 50-call load with
    `py-spy --nonblocking --gil` (AGENTS.md P05-T17e2, P07-FIX-1), attribute each underrun (media pacer lateness vs
    FakeTelnyx drain/tick lag vs sim CPU), and fix the cause in our code (media pipeline or simulator). Never loosen
    the asserts, add retries, or lower the call count. Record findings as a specs/07 §9 (or specs/30 §5) Decision.
  - Acceptance: `uv run pytest tests_e2e -m capacity -s` passes 10/10 consecutive runs on a freshly rebuilt stack with
    0 underruns and overhead p50 ≤ 140 ms in every run (≥ 10 ms margin); `make check` and `make e2e` pass.

- [x] P08-FIX-2 RecordingBusy retry budget vs. copy lock hold — see .ralph/review-inbox/2026-11-04-recording-copy-lock-busy-budget.md — no production change needed; `test_recording_busy_retry.py`: 300 s holder → RECORDING_BUSY delay ≥ PTTL (> 30 s) then `already_stored`, worst case 4 × 361 s + 300 s < 1800 s budget, Temporal time-skipping honours a 5 min `next_retry_delay` past `maximum_interval` 30 s; lint/typecheck/placeholders pass; make test 12663 passed, 1 unrelated load flake (P08-FIX-5), cov 99 %
  - Spec refs: specs/06-telephony-telnyx.md §11 (Decision P05-T14 rule 4, Decision P08-FIX-1); specs/18 §1
  - Depends on: P08-FIX-1
  - Deliverables: the finding's fix landed with P08-FIX-1 (`RecordingBusy` `next_retry_delay` = lock PTTL + 1 s; the
    attached patch equals the P08-FIX-1 patch plus `test_get_recording.py`, already on main). Add the regression test the
    note asks for: a holder that keeps the lock beyond the old ~30 s backoff budget (PTTL ≥ 300 s) — attempt 1 raises
    busy with a delay ≥ the PTTL, the holder then commits, the next attempt returns `already_stored` for the holder's
    key; and assert the worst case (4 delays × lock TTL 6 min) fits the PostCallWorkflow 30 min budget in a unit test of
    the constants. No production change unless the test fails.
  - Acceptance: test passes; `make check` passes; append the note's file name to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-6 store_recording deadline inside the post-call budget (the P08-FIX-2 budget test certified a false bound) — see .ralph/review-inbox/2026-11-04-p08-fix-2-budget-test.md — va_workflows.post_call.budget (retry_worst_case, STORE_RESERVE 525 s, store_window) schedules store_recording with schedule_to_close = budget deadline − reserve, skipped with no window; false budget test replaced; 3 workflow tests (busy past window → 4 attempts, step failed, billing runs, no timeout; no window → skipped; window asserted); review: lint/typecheck/placeholders exit 0, make test 12676 passed, 7 skipped, cov 99 %
  - Spec refs: specs/18-post-call-analysis.md §1 (Decision P05-T18b), specs/06-telephony-telnyx.md §11 (Decision P08-FIX-1)
  - Depends on: P08-FIX-2
  - Deliverables: the budget timer starts with the workflow, so steps 1-2 (finalize ≤ 165 s, recording wait 600 s,
    lookups/marks) eat into the window `store_recording` gets; `(attempts-1) × lock TTL + timeout < budget` ignores
    them (real worst case ≈ 39 min). Fix in production: `PostCallWorkflow._store_recording` runs the activity with a
    `schedule_to_close_timeout` = time left until `start + budget − reserve` (reserve = worst case of
    `finalize_billing` + two marks, computed from the retry policies by a pure helper), and skips the step
    (`recording = "skipped"`, logged) when no window is left, so a busy/failing copy ends before the budget and
    billing still runs. Replace the false constant test with true ones (pure worst-case helper, positive store
    window after the worst-case steps 1-2, workflow test: repeated 6 min busy refusals fail the step before the
    budget, `finalize_billing` runs, `timed_out` false). Record in specs/18 §1 + amend the specs/06 §11 P08-FIX-1
    Decision.
  - Acceptance: tests pass; `make check` passes; append the note's file name to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-7 A store_recording attempt cut by its schedule_to_close must not commit after the workflow skipped it — see .ralph/review-inbox/2026-11-04-postcall-store-window.md — `StoreRecordingInput.commit_by` + `COMMIT_MARGIN` 5 s, non-retryable `RecordingOutOfTime` before the lock / after the upload (object deleted, no event, carrier copy kept), `MIN_STORE_WINDOW` 60 s; 3 activity + workflow tests; specs/18 §1 Decision P08-FIX-7; review: lint/typecheck/placeholders exit 0, make test 12681 passed, 7 skipped
  - Spec refs: specs/18-post-call-analysis.md §1 (Decision P08-FIX-6), specs/06-telephony-telnyx.md §11
  - Depends on: P08-FIX-6
  - Deliverables (non-blocker): `store_recording` never heartbeats, so a `schedule_to_close` timeout leaves the attempt
    running; it can commit `recording_s3_key` + `call.recording_ready` after `call.completed` said `recording =
    skipped`. Fix: the workflow passes the copy deadline in `StoreRecordingInput.commit_by`; the activity refuses to
    start a copy, and to commit one, once `now + COMMIT_MARGIN` passes it (non-retryable `RecordingOutOfTime`,
    uploaded object deleted, carrier copy and lock handled as on a lost run); `MIN_STORE_WINDOW` raised to 60 s so
    the workflow does not start copies it will cut. Record in the specs/18 §1 Decision.
  - Acceptance: activity tests (late before start → no download/upload; late at commit → object deleted, key unset,
    no event, carrier copy kept), workflow test (small window → skipped), `make check` passes; append the note's file
    name to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-8 Derive the store_recording reserve from the steps that follow it — see .ralph/review-inbox/2026-11-04-p08-fix-6-store-window-review.md — `plan.IMPLEMENTED` + `budget.STEP_WORST_CASE`/`steps_after_store`/`reserve_for` (ValueError for a step without a worst case), `STORE_RESERVE` still 525 s; guard/growth/refusal tests; specs/18 §1 Decision P08-FIX-8 (schedule-to-start excluded); review: lint/typecheck/placeholders 0 findings, make test 12692 passed, 7 skipped, cov 99 % (first run hit an unrelated fakeredis TTL flake in test_prefetch_activity, tracked as a task)
  - Spec refs: specs/18-post-call-analysis.md §1 (Decisions P08-FIX-6, P08-FIX-7)
  - Depends on: P08-FIX-7
  - Deliverables (non-blocker): `STORE_RESERVE` is hard-wired to "finalize_billing + two marks"; when analysis steps
    4-10 land between `store_recording` and `finalize_billing` it silently undercounts. Compute the reserve from the
    implemented steps after `store_recording` (per-step worst case + its mark) and add a test that fails when a step
    becomes plannable without a worst-case entry. State in the Decision that the reserve excludes schedule-to-start
    latency (worker backlog).
  - Acceptance: reserve still 525 s for today's steps; guard test fails for an implemented step without an entry;
    `make check` passes; append the note's file name to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-9 One source for the planned post-call steps and a guard that the copy keeps a window — see .ralph/review-inbox/2026-11-05-p08-fix-8-reserve-guard-review.md, 2026-11-05-p08-fix-8-reserve-review.md — `plan._PLAN` table (IMPLEMENTED derived), all-flags facts test, `budget.PRE_STORE_WORST_CASE` 1005 s + import-time `check_copy_window`; reserve 525 s; make test 12697 passed, 7 skipped, cov 99.07 %; lint/typecheck/placeholders 0 findings
  - Spec refs: specs/18-post-call-analysis.md §1 (Decisions P08-FIX-6, P08-FIX-7, P08-FIX-8)
  - Depends on: P08-FIX-8
  - Deliverables (non-blockers): (1) `plan.IMPLEMENTED` is a second hand-kept copy of `planned_steps`' step set, so a
    step planned only under a condition the test fixture does not set can skip the reserve; build `planned_steps`
    from one table keyed by step (`IMPLEMENTED` = its keys) and test that an all-flags-on `CallRecordFacts` plans
    exactly `IMPLEMENTED` (the builder sets every bool field, so the test grows with the model). (2) Nothing stops
    the reserve from growing until `store_window` returns `None` on every call (silent `recording = skipped`): move
    the pre-store worst case into `budget` and refuse at import when pre-store + `MIN_STORE_WINDOW` +
    `STORE_RESERVE` exceeds `POSTCALL_BUDGET_S`. (3) Decision wording: what the guard really checks, and that the
    reserve sums the post-copy steps because they run in sequence (parallel steps would use the max).
  - Acceptance: tests for the table, the all-flags test, the import-time refusal helper; reserve still 525 s;
    `make check` passes; append both note names to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-10 Flaky `test_the_context_of_a_queued_call_is_stored_with_its_ttl` (fakeredis TTL 5399 vs 5400) — `redis_ttl.assert_expires_in` (PTTL window, 7 unit tests) replaces 6 exact `ttl == N` asserts; make test 12704 passed, 7 skipped, cov 99.07 %; lint/typecheck/placeholders 0 findings
  - Spec refs: specs/03 §11 (Redis keys)
  - Depends on: none
  - Deliverables: the prefetch activity test compares a fakeredis wall-clock `TTL` with an exact value; under load a
    second passes between the write and the read. Assert the intent without wall-clock dependence (e.g. `PTTL`
    within the expected TTL and a lower bound, or read the TTL the store was asked to set).
  - Acceptance: test robust to a slow read; `make check` passes.

- [x] P08-FIX-11 `assert_expires_in` accepts missing / non-expiring keys when `ttl_s` ≤ slack — see .ralph/review-inbox/2026-11-05-redis-ttl-helper-small-ttl.md — lower bound max(0, ttl−slack): missing/no-expiry keys fail for short TTLs (ttl_s 10 cases fail on old helper); make test 12708 passed, 7 skipped, cov 99 %; lint/typecheck/placeholders 0 findings
  - Spec refs: specs/03 §11 (Redis keys)
  - Depends on: P08-FIX-10
  - Deliverables (non-blocker): the lower bound `ttl_s * 1000 - slack_ms` is ≤ 0 for short TTLs, so PTTL -1/-2 pass.
    Clamp the lower bound at 0 (a key must exist and expire), document it, and add short-TTL unit cases.
  - Acceptance: `ttl_s=10` missing key and key without expiry fail, `ttl_s=10` key set EX 10 passes; `make check`
    passes; append the note name to `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-4 P08-FIX-3 review follow-ups: gc_pause/underrun log hygiene — see .ralph/review-inbox/2026-11-04-p08-fix-3-gc-pause-log.md, 2026-11-04-p08-fix-3-gc-review.md, 2026-11-04-p08-fix-3-gc-wip-review.md, 2026-11-04-p08-fix-3-underrun-log.md, 2026-11-04-p08-fix3-wip-review.md, 2026-11-04-underrun-attribution.md, 2026-11-04-underrun-log-attribution.md — gc_pause buffered in the collector callback and flushed each second by a supervised task in an empty context (gc_pause_overflow); outbound.underrun logged after the send next to the counter, throttled per utterance/leg with suppressed + lock/socket/resume/controls attribution (TimedSink); pipeline_ended underruns_logged/max_underrun_wake_late_ms; _unwire LIFO order documented; specs/07 §9 Decision P08-FIX-4; make test 12721 passed (cov 99.07 %), lint/typecheck/placeholders clean, jscpd 0, complexity A (2.92); capacity 3 runs 0 underruns, p50 119/124/104 ms
  - Spec refs: specs/07-media-pipeline.md §9 (Decision P08-FIX-3), §21
  - Depends on: P08-FIX-3
  - Deliverables (all non-blockers, still open in 2abf225): (1) `va_common.gc_tuning.GcPauseLog._callback` must not
    log from inside the collector: record pauses allocation-light (bounded deque) and log from normal code, outside
    any bound call context (no `call_id`/`org_id` on `gc_pause`); (2) `OutboundAudio` underrun log: build the fields
    before the send, emit next to the counter after the generation check (no log without a count), add `turn`, reset
    `_due_ms` on the idle `_wake.wait()` branch, attribute `send_mark`/controls await (`prev_controls_await_ms`), and
    throttle (first underrun of a burst/utterance with fields, counts + max `wake_late_ms` in `pipeline_ended`);
    (3) separate transport backpressure (socket send time inside the transport lock) from loop lag in the
    attribution; (4) fix the `va_media.runtime._unwire` docstring (LIFO order) or register it first; (5) specs/07 §9
    Decision P08-FIX-3 carries the measured before/after numbers itself.
  - Acceptance: unit tests per item (context-free gc_pause, clear-during-held-send → no log and no count, held mark
    → named in the log, throttled burst → one line + summary counts); `make check` passes; `uv run pytest tests_e2e
    -m capacity -s` still 0 underruns, p50 ≤ 140 ms on 3 runs; append all seven note names to
    `.ralph/review-inbox/DONE` on approval.

- [x] P08-FIX-5 Flaky `test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks` under full-suite load — test-only: accepts identical gateway retries (same `messages`), bounds the 500 ms settle on the earliest `received_at`; alone 3/3 pass; lint/typecheck/placeholders pass; make test 12721 passed, 7 skipped, cov 99.07 %; jscpd 0 clones; radon avg A (2.92)
  - Spec refs: specs/30 §3.4, specs/10 §2
  - Depends on: none
  - Deliverables: in `make test` (P08-FIX-2 review run) the wall-clock FakeLLM opener request timed out once
    (`llm.attempt_failed kind=timeout`), the gateway retried, and `[opener_request] = r.state.llm.requests` saw 2
    requests (passes 3/3 alone). Make the assertion express the intent without depending on wall-clock latency (e.g.
    assert on the first request of this call, or give the rig's gateway a load-independent deadline), not a retry.
  - Acceptance: test robust to an LLM retry; `make check` passes.

- [x] P08-T01 Tools API with secret headers — /v1/tools CRUD (TL- ids, masked secret headers, AAD-bound encryption, reference-checked delete, audit, cross-tenant factory); 101 new tests; make test 12828 passed, 7 skipped, cov 99 %; lint/typecheck/placeholders pass
  - Spec refs: specs/13-tools-and-dynamic-data.md (Tools API, secret headers)
  - Depends on: P07-T07, P02-T11
  - Deliverables: `/v1/tools` CRUD (TL- ids, legacy envelope; the `/test` route is in P08-T02), validation of name/url/method/input_schema/response_map, secret header encryption, audit, cross-tenant factory
  - Acceptance: API tests for every field and error path; secrets never returned.

- [x] P08-T02a ToolRunner core and `POST /v1/tools/{tool_id}/test` — va_tools models/render/runner/signing + `/test` route (tools:write, audit-exempt); review re-run: lint/typecheck/placeholders exit 0, make test 12868 passed 7 skipped cov 99.08 %; spec specs/13 §5 Decision P08-T02a (split from P08-T02 by the planner, 2026-11-05: the original task spans the runner, two runners' in-call wiring, and the API)
  - Spec refs: specs/13-tools-and-dynamic-data.md §2 (`/test`), §3, §4 (function schema), §5 steps 1, 3-8, 10
  - Depends on: P08-T01, P05-T09
  - Deliverables: `va_tools.models` (`ToolSpec` from a stored row or an inline tool), `va_tools.render` (request rendering with `input.*`), `va_tools.runner.ToolRunner` (argument validation, rendering, signed execution over `HttpExecutor`, response map, LLM tool message, `call.tool` event data with masked headers, `TOOL_FAILED` classification), `function_def(spec)`; `POST /v1/tools/{tool_id}/test`
  - Acceptance: unit tests for every runner step (invalid arguments, rendering with type preservation and URL encoding, query merge, secret headers sent but masked, retries/timeout outcomes, JSONPath single/multi/no match, 4 000-char data cap, 2 KB excerpt); API tests for `/test` against a real-socket receiver incl. masking and errors.

- [x] P08-T02b1 Task-mode custom tools in calls: executor, events, variables, speech during tool, fillers (split from P08-T02b by the planner, 2026-11-05: the executor-backed system tools need their own DB/Temporal wiring) — CallToolExecutor + merged_tools, TaskBrain tool tasks with speech / 1 200 ms round-robin fillers, variables snapshotted to calls.variables, call.tool + call.error TOOL_FAILED, CallToolSource with signing; make test 12897 passed / 7 skipped, coverage 99.07 %, lint/typecheck/placeholders pass, jscpd 0 clones
  - Spec refs: specs/13 §4, §5 steps 2 and 9 (+ §5 caps); specs/09 §9 (fillers); specs/04 §7 (`engine.tool_fillers`); specs/19 §2 (`call.tool`, `call.error` `TOOL_FAILED`)
  - Depends on: P08-T02a
  - Deliverables: `va_tools.executor.CallToolExecutor` over `ToolRunner` (call `tool_ids` loaded with decrypted secrets + `inline_tools`, inline overrides org tool of the same name) emitting `call.tool`/`call.error TOOL_FAILED`; `ToolExecutor.execute(call, scope) -> ToolAnswer` (content + variables merged into engine state, snapshotted for `calls.variables`); `TaskBrain` speech-during-tool (speech starts with the request) and the 1 200 ms cached filler (round-robin, ≤ 1 per round, `engine.tool_fillers`); media runtime wiring
  - Acceptance: harness tests for filler timing (speech ≤ 300 ms after the tool call, filler at 1 200 ms, none when the tool is fast), timeout behaviour, extraction into variables, barge-in during a tool; runtime sim test of a tool call; loader test with a stored tool's secret header.

- [x] P08-T02b2 Executor-backed system tools `send_sms` and `schedule_callback` in task-mode calls — va_media.call_system_tools (ToolSms, CallbackScheduler, TemporalCallbackStarter, sms_capable), va_db.callbacks + call_children, va_domain.call_callback, SystemTool in CallToolExecutor, availability gating in TaskBrain; fresh ids per run; make test 12941 passed 7 skipped (cov 99.04%), lint/typecheck/placeholders pass, jscpd 0, radon 2.92
  - Spec refs: specs/13 §6 (`send_sms`, `schedule_callback`), specs/10 §7 Decision P05-T09b
  - Depends on: P08-T02b1
  - Deliverables: `sms_capable` fact from the agent number/messaging readiness (shared with `va_db.voicemail_sms.sms_sender`), `send_sms` over `OutboundSms`, `schedule_callback` creating a scheduled child call (same config, `start_time`, `parent_call_id`) and starting its `CallWorkflow`, both behind the call's executor
  - Acceptance: availability tests (number not SMS-capable, `engine.allow_callbacks` off), runtime sim test sending an SMS, DB test of the scheduled callback row and workflow start.

- [x] P08-T02c1 Pathway engine tool rounds on reply nodes (split from P08-T02c by the planner, 2026-11-05: engine and media wiring are separately testable) — va_engine.engine_node_tools (NodeTools.offer, run_node_round) + shared va_engine.tool_tasks (TaskBrain delegates), PathwayToolExecutor.definitions_for, GlobalConfig.allow_end_call_tool, EnginePorts.tools / ConversationFacts.system_tools+tool_fillers; no tool round after an unavailable or cut-off reply (mutant without the guards fails 3 tests); specs/13 §4 Decision P08-T02c1, specs/11 §3.4/§5.9; lint/typecheck/placeholders pass, make test 13015 passed 7 skipped (cov 99.07%; a first run had 1 unrelated sim flake, task fix:sim-shared-redis-exit-flake), jscpd 0, radon worst C (12)
  - Spec refs: specs/13 §4, §6; specs/11 §3.4 (`allowEndCallTool`), §5.7 tools line
  - Depends on: P08-T02b1, P07-T12
  - Deliverables: shared tool-task helpers extracted from TaskBrain (`va_engine.tool_tasks`), `GlobalConfig.allow_end_call_tool`, `PathwayToolExecutor.definitions_for`, PathwayEngine offering node ∪ `globalTools` custom tools and allowed system tools on Default / Knowledge Base nodes with speech/filler, variables, `end_call`/`press_button`, per-pass/per-call caps
  - Acceptance: engine tests (tool round on a node, global tool, `allowEndCallTool=false`, end_call, press_button, caps, barge-in mid-tool), TaskBrain tests unchanged and green.

- [ ] P08-T02c2 Pathway call tools in the media runtime (split from P08-T02c)
  - Spec refs: specs/13 §4-§6
  - Depends on: P08-T02c1
  - Deliverables: `CallToolSource.pathway_executor` over every tool the pinned graph references (TL ids + inline objects, keyed by ref), a `PathwayToolExecutor.for_refs(refs)` implementation whose node executors share the call's runner, events and system tool runs (Decision P08-FIX-17), runtime builds system tools + executor for pathway calls and passes facts to the engine
  - Acceptance: media unit tests + runtime sim pathway call with a node tool (call.tool event, tool answer used in the reply).

- [ ] P08-T03 Dynamic data
  - Spec refs: specs/13-tools-and-dynamic-data.md (dynamic_data)
  - Depends on: P08-T02a
  - Deliverables: pre-call fetch in `prefetch_call_context`, per-turn refresh rules, Redis cache keyed by request hash, JSONPath `response_data` mapping with `context` injection, `call.dynamic_data` events
  - Acceptance: tests for cache hits, failures (call proceeds with event), templated requests.

- [ ] P08-T04 Deno sandbox and Custom Code node
  - Spec refs: specs/13-tools-and-dynamic-data.md §8; specs/11-pathways-engine.md (Custom Code node)
  - Depends on: P07-T08
  - Deliverables: `va_tools.sandbox.DenoRunner` (permission flags, V8 memory flag, wall-clock kill, stdin/stdout JSON protocol, concurrency limiter), publish-time `deno check` validation, node handler
  - Acceptance: tests for success, exception, timeout, memory exhaustion, forbidden fs/env/net access, output cap.

- [ ] P08-T05 KB parsers
  - Spec refs: specs/14-knowledge-bases.md (parsers)
  - Depends on: P01-T04
  - Deliverables: pdf, docx, txt/md, html, csv parsers producing structured sections with heading paths and page numbers, run in thread pool
  - Acceptance: fixture documents committed under tests/fixtures produce expected sections.

- [ ] P08-T06 Crawler
  - Spec refs: specs/14-knowledge-bases.md (crawler)
  - Depends on: P08-T05
  - Deliverables: sitemap discovery, robots.txt compliance, same-domain BFS with depth/page limits and rate limiting, content extraction, SSRF guard
  - Acceptance: tests against a local HTTP fixture site.

- [ ] P08-T07 Chunking, embeddings, ingest workflow
  - Spec refs: specs/14-knowledge-bases.md (ingest workflow, chunking and embeddings)
  - Depends on: P08-T05, P03-T14
  - Deliverables: heading-aware token chunker, `KnowledgeIngestWorkflow` (per document activities, retries, status transitions, counts), `kb.ready`/`kb.failed` events, embedding usage metering
  - Acceptance: Temporal tests; chunk boundaries and overlaps verified.

- [ ] P08-T08 Hybrid retrieval and per-call cache
  - Spec refs: specs/14-knowledge-bases.md (hybrid retrieval SQL, latency budget, per-call cache)
  - Depends on: P08-T07
  - Deliverables: `Retriever.search(kb_ids, query, top_k)` with RRF fusion SQL exactly as specified, query embedding cache, per-call cache, benchmark test (marker `bench`)
  - Acceptance: relevance tests on a fixture corpus; benchmark meets p95 target.

- [ ] P08-T09 KB API and KB chat
  - Spec refs: specs/14-knowledge-bases.md (API, KB chat, legacy compat)
  - Depends on: P08-T08
  - Deliverables: list/create/upload/crawl/text/get/delete/documents endpoints, KB chat with sources, legacy `/v1/knowledgebases`, plan limits
  - Acceptance: API tests incl. file type/size errors (`KB_UNSUPPORTED_FILE`, `KB_FILE_TOO_LARGE`).

- [ ] P08-T10 KB node and KB tools in calls
  - Spec refs: specs/11-pathways-engine.md (Knowledge Base node); specs/14-knowledge-bases.md (injection format into the voice prompt); specs/05-calls-api.md (tools array with KB- ids)
  - Depends on: P08-T08, P07-T12
  - Deliverables: KB node handler, `KB-` ids in Send Call `tools` enable retrieval for task-mode calls, prompt injection format
  - Acceptance: e2e kb_lookup passes.

- [ ] P08-T11 E2E: tools
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list: tool_call_with_speech)
  - Depends on: P08-T02b1
  - Deliverables: `tests_e2e/test_tool_call_with_speech.py`
  - Acceptance: filler heard (recorded agent audio contains filler phrase marker), tool result used in reply.

## Phase exit gate
`make check && make e2e` pass; KB benchmark recorded.
