# Phase 08 — Tools, Dynamic Data, Custom Code, Knowledge Bases

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

- [ ] P08-FIX-4 P08-FIX-3 review follow-ups: gc_pause/underrun log hygiene — see .ralph/review-inbox/2026-11-04-p08-fix-3-gc-pause-log.md, 2026-11-04-p08-fix-3-gc-review.md, 2026-11-04-p08-fix-3-gc-wip-review.md, 2026-11-04-p08-fix-3-underrun-log.md, 2026-11-04-p08-fix3-wip-review.md, 2026-11-04-underrun-attribution.md, 2026-11-04-underrun-log-attribution.md
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

- [ ] P08-FIX-5 Flaky `test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks` under full-suite load
  - Spec refs: specs/30 §3.4, specs/10 §2
  - Depends on: none
  - Deliverables: in `make test` (P08-FIX-2 review run) the wall-clock FakeLLM opener request timed out once
    (`llm.attempt_failed kind=timeout`), the gateway retried, and `[opener_request] = r.state.llm.requests` saw 2
    requests (passes 3/3 alone). Make the assertion express the intent without depending on wall-clock latency (e.g.
    assert on the first request of this call, or give the rig's gateway a load-independent deadline), not a retry.
  - Acceptance: test robust to an LLM retry; `make check` passes.

- [ ] P08-T01 Tools API with secret headers
  - Spec refs: specs/13-tools-and-dynamic-data.md (Tools API, secret headers)
  - Depends on: P07-T07, P02-T11
  - Deliverables: `/v1/tools` CRUD (TL- ids, legacy envelope), validation of name/url/method/input_schema/response_map, secret header encryption, audit, cross-tenant factory
  - Acceptance: API tests for every field and error path; secrets never returned.

- [ ] P08-T02 Tool runtime, system tools, LLM exposure, speech during tool
  - Spec refs: specs/13-tools-and-dynamic-data.md (LLM exposure, execution runtime, built-in system tools)
  - Depends on: P08-T01, P05-T09
  - Deliverables: function schema generation, in-call execution with timeout/retries/JSONPath extraction, `speech` filler via phrase cache, result injection as tool message, inline tool objects in Send Call, system tools (end_call, transfer_call, press_button, send_sms, schedule_callback, lookup_knowledge)
  - Acceptance: harness tests for filler timing, timeout behaviour, extraction into variables.

- [ ] P08-T03 Dynamic data
  - Spec refs: specs/13-tools-and-dynamic-data.md (dynamic_data)
  - Depends on: P08-T02
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
  - Depends on: P08-T02
  - Deliverables: `tests_e2e/test_tool_call_with_speech.py`
  - Acceptance: filler heard (recorded agent audio contains filler phrase marker), tool result used in reply.

## Phase exit gate
`make check && make e2e` pass; KB benchmark recorded.
