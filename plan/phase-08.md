# Phase 08 — Tools, Dynamic Data, Custom Code, Knowledge Bases

- [x] P08-FIX-22 "The run goes on after a cut round" is a property of the system tool (`SystemTool.detaches`), not of being a system tool — see .ralph/review-inbox/2026-11-05-p08-fix-21-wip-review.md (no blockers; item 1 fixed here, items 2 and 3 closed with a written reason below) — `va_tools.executor.SystemTool.detaches` (protocol member, no default), `CallToolExecutor._run_system` runs a non-detaching tool in the waiting task (never pending, never detached, reads `interrupted`), `ToolSms`/`CallbackScheduler` `detaches: Final = True`, 6 new test cases (3 parametrized twins, cancelled-with-its-wait, TaskBrain barge-in `interrupted`, media class check), Decision P08-FIX-22 (specs/13 §6, pointers in Decisions P08-FIX-13 item 2 and P08-FIX-21 item 1); reviewer re-run: lint/typecheck/placeholders pass, check_spec_refs 0, jscpd 0 clones, radon avg 2.93, pip-audit clean, pnpm audit high exit 0, make test 13092 passed 7 skipped (cov 99.08%, va_tools/executor 99%), make e2e 20 passed (capacity flow 0 underruns; the builder's first run on the cold stack had 11 underruns from one ~65 ms media loop stall, runtime task fix:capacity-start-burst-stall, the flow runs no system tool); reviewer mutation checks on a shadow copy: always shield → the 2 non-detaching tests fail, never shield → 6 fail, `ToolSms.detaches = False` → the media test fails
  - Spec refs: specs/13 §6 (Decisions P08-FIX-13 item 2, P08-FIX-21), specs/13 §4 (Decision P08-FIX-20)
  - Depends on: P08-FIX-21
  - Deliverables: `va_tools.executor.SystemTool.detaches: bool` (protocol member, no default: every tool says
    it); `CallToolExecutor` owns, shields and notes as detached only the runs of tools with `detaches`; a tool
    without it runs in the waiting task, is cancelled with its wait, is never `detached` (a cut round answers
    it `interrupted`) and leaves nothing for `aclose`; `ToolSms.detaches` / `CallbackScheduler.detaches` true;
    specs/13 §6 Decision `P08-FIX-22` (+ pointer in Decision P08-FIX-13 item 2 and Decision P08-FIX-21 item 1);
    AGENTS.md.
  - Acceptance: va_tools tests: a system tool with `detaches = False` whose wait is cancelled mid-run is
    cancelled, not detached, and `aclose` has nothing to wait for; it answers, reports a failure
    (`call.error` `TOOL_FAILED`) and propagates a crash like the detaching ones; TaskBrain barge-in test with
    the real executor: a held non-detaching tool reads `interrupted` and never completes, the detaching twin
    still reads `in_progress` and completes once; media test: `send_sms` and `schedule_callback` detach;
    `make check` and `make e2e` pass; append the note name to `.ralph/review-inbox/DONE` on approval.
  - Closed without a change (Planner, 2026-11-05, DEC-015): item 2 (an `in_progress` answer is never replaced
    by the run's outcome): the case needs a barge-in during the few hundred ms of a system tool run AND a
    failure of that run; the failure is recorded (`call.error` `TOOL_FAILED`) and the caps of Decision
    P08-FIX-13 item 3 bound a model that sends again; replacing the answer needs a call-to-record link that
    survives superseded turns (`keep_tool_records`, `completed_rounds`) and the persisted engine state in both
    runners. The reviewer marked it "fine to drop". Item 3 (a run that had just ended when its wait was
    cancelled also reads `in_progress`): stated in Decision P08-FIX-21 item 1 ("still running, or just
    finished"), a window of two or three loop turns that errs on the safe side; no change asked for.
- [x] P08-FIX-21 A system tool whose run goes on after the round was cut is answered `in_progress`, not `interrupted`; a test pins the cut while the tools are awaited — see .ralph/review-inbox/2026-11-05-p08-fix-20-wip-review.md (items 1, 2; neither is a blocker) — `ToolExecutor.detached(call)` (object identity), `CallToolExecutor` notes a system tool call whose wait was cancelled while its run was not, `task_tools.IN_PROGRESS`, `settle_round(..., executor=)`/`cut_contents(..., executor=)` in both runners, 4 unit + 4 engine + 4 TaskBrain + 4 executor tests and the barge-in test, Decision P08-FIX-21 (specs/13 §6, with §4/§5 pointers); reviewer re-run: lint/typecheck/placeholders pass, check_spec_refs 0, jscpd 0 clones, radon avg 2.93, pip-audit clean, pnpm audit high exit 0, make test 13086 passed 7 skipped (cov 99.08%, tool_tasks/tool_executor/task_tools/engine_node_tools 100%, va_tools/executor 99%), make e2e 20 passed (capacity flow 0 underruns; the builder's first run had 4 underruns from one media loop stall on the cold stack, tracked as runtime task fix:capacity-start-burst-stall); reviewer mutation checks on a shadow copy: every cancellation detaches → 1 test fails, `detached` by equality → 1, never detached → 2, cut never asks the executor → 5, every cancelled call `in_progress` → 19, cancelled wait turned into an answer → 5
  - Spec refs: specs/13 §4 (Decision P08-FIX-20), §6 (Decisions P08-T02b2, P08-FIX-13 item 2), §5 (Decision P08-T02b1 item 4), specs/07 §8
  - Depends on: P08-FIX-20
  - Deliverables: Planner decision (2026-11-05, DEC-014): the executor tells a cut round which runs continue.
    `va_engine.tool_executor.ToolExecutor.detached(call) -> bool` (by object identity: call ids may be empty or
    repeated); `va_tools.executor.CallToolExecutor` remembers the system tool calls whose wait was cancelled
    while the executor-owned run was not; `va_engine.task_tools.IN_PROGRESS` (`{"status":"in_progress"}`);
    `va_engine.tool_tasks.settle_round(..., executor=)` / `cut_contents(..., executor=)` answer a cancelled
    executor call `in_progress` when its run is detached, `interrupted` otherwise (custom tools: the request was
    cancelled). No cancellation is swallowed anywhere. specs/13 §6 Decision `P08-FIX-21` (+ rewording of §6
    Decision P08-FIX-13 item 2 and §4 Decision P08-FIX-20 item 1); AGENTS.md.
  - Acceptance: va_tools tests: a system tool call whose wait is cancelled mid-run is `detached` (an equal but
    different `ToolCall` object is not), a run cancelled by `aclose` and a custom tool cancelled mid-request are
    not; the TaskBrain barge-in test records `in_progress` for the held `send_sms`, the message is sent once;
    engine + TaskBrain tests: a round cut with a detached system tool records `in_progress` and a superseding
    turn shows it to the next request; engine + TaskBrain tests cancel the consuming task while the round is
    parked awaiting its tools (fillers off, one finished and one held tool, held first and held second): the
    finished tool keeps its answer and variables, the held one is cancelled and reads `interrupted`, the call
    does not end; unit tests of `cut_contents` with and without an executor; `make check` and `make e2e` pass;
    append the note name to `.ralph/review-inbox/DONE` on approval.
- [x] P08-FIX-20 A tool round cut short answers `interrupted` only for what did not happen: executor calls that finished keep their answers (and variables) — see .ralph/review-inbox/2026-11-05-p08-fix-19-wip-review.md (item 1, blocker; item 2 is carried into the P08-T02c2 acceptance) — `va_engine.tool_tasks.settle_round`/`cut_contents`/`finished_answers` settle the `finally` of `run_node_round` and `TaskBrain._run_tools` (finished tools keep answer + variables merged at the cut, planned answers stay, only cancelled tools and actions not handed over read `interrupted`), 5 unit + 4 engine + 3 TaskBrain tests, Decision P08-FIX-20 (specs/13 §4); reviewer re-run: lint/typecheck/placeholders pass, check_spec_refs 0, jscpd 0 clones, radon avg 3.56, pip-audit clean, pnpm audit high exit 0, make test 13070 passed 7 skipped (cov 99.08%, tool_tasks + engine_node_tools 100%), make e2e 20 passed; reviewer mutation checks on a shadow copy: all-`interrupted` cut → 11 tests fail, late merges dropped → 2 fail, planned answers overwritten → 4 fail; reviewer probe (outside the repo): reply cancelled while the tools are awaited keeps the finished tool's answer + variables
  - Spec refs: specs/13 §4 (Decisions P08-T02c1 item 3, P08-FIX-17 item 1, P08-FIX-19), §5 (Decision P08-T02b1 items 4, 5), specs/11 §6 (Decision P05-T09c item 4), specs/07 §8
  - Depends on: P08-FIX-19
  - Deliverables: shared helpers in `va_engine.tool_tasks` (`finished_answers`, `cut_contents`, `settle_round`)
    used by the `finally` of both `va_engine.engine_node_tools.run_node_round` and `TaskBrain._run_tools`: in a
    round cut short (barge-in during the speech/filler line or the wait, or a reply closed at a press) an
    executor call whose run finished keeps its real answer and its variables are merged at the cut; a call
    answered without running (`invalid_arguments`, `too_many_tool_calls`, `skipped`) keeps that answer; only an
    executor call whose run was cancelled and a `press_button`/`end_call`/`transfer_call` that was not handed
    over read `{"error":"interrupted"}`. Planner decision (2026-11-05, DEC-013): the rule holds wherever the cut
    lands (the note's fix covers only the cut at a press; a tool that finished before a barge-in on the filler
    has the same double-side-effect risk, and §5 Decision P08-T02b1 item 4 already says "unfinished tools").
    specs/13 §4 Decision `P08-FIX-20` (+ rewording of Decision P08-FIX-19 item 1 and notes in Decision
    P08-T02c1 item 3, §5 Decision P08-T02b1 items 4-5, specs/11 §6 Decision P05-T09c item 4); AGENTS.md.
  - Acceptance: engine and TaskBrain tests: `[press 1, lookup]` closed at the press records `sent` + the real
    lookup answer with its variables merged and the tool run once, also after a superseding turn (pathway:
    context update with the real answer ahead of the merged turn; task mode: carried round with the real answer,
    pairing valid); `[press 1, lookup, press 2, end_call]` closed at press 1 records `sent`, the real answer,
    `interrupted`, `interrupted` and does not end; a barge-in on the filler with one finished and one held tool
    records the real answer (variables merged) and `interrupted`, only the held tool is cancelled; unit tests of
    the helpers; reverting the `finally` to all-`interrupted` fails the new tests; `make check` and `make e2e`
    pass; append the note name to `.ralph/review-inbox/DONE` on approval.
- [x] P08-FIX-19 A pathway `press_button` is answered `sent` only when its DTMF was handed over (answer and yield per press); regression test: a `transfer_call` repeated in the merged turn is requested — see .ralph/review-inbox/2026-11-05-p08-fix-17-review.md (item 2; item 1 was fixed in P08-FIX-17, its owed assertion is added here) — `run_node_round` answers each call and yields a `press_button`'s `SendDtmfAction` right after its answer (`RoundOutcome.terminal` set after the round; the `finally` answers the rest `interrupted`), 6 engine tests + TaskBrain repeated-transfer regression test, Decision P08-FIX-19 (specs/13 §4); reviewer re-run: lint/typecheck/placeholders pass, check_spec_refs 0, jscpd 0 clones, pip-audit clean, pnpm audit high exit 0, make test 13058 passed 7 skipped (cov 99.08%, engine_node_tools 100%); reviewer mutation checks on a shadow copy: collect-then-yield restored → 4 tests fail, `_interrupt_answer` dropped → 2 tests fail
  - Spec refs: specs/13 §4 (Decisions P08-T02c1 item 4, P08-FIX-17 item 1), specs/11 §6 (Decision P05-T09c item 4), specs/07 §8
  - Depends on: P08-FIX-17
  - Deliverables: `va_engine.engine_node_tools.run_node_round` answers the round's calls in call order and yields
    each `press_button`'s `SendDtmfAction` right after that call's answer (as `TaskBrain._run_tools` does), so a
    reply closed at a press leaves the presses (and an `end_call`) behind it answered `{"error":"interrupted"}`
    and the conversation not ended; a TaskBrain regression test that a `transfer_call` repeated in the turn
    superseding a transfer closed on its message yields its `TransferRequest`; specs/13 §4 Decision `P08-FIX-19`
    (an answer reporting an action is recorded only for an action handed to the consumer; notes in Decision
    P08-T02c1 item 4 and Decision P08-FIX-17 item 1); AGENTS.md.
  - Acceptance: engine tests: two presses around an executor call are answered and sent in call order (the
    second press is not on record when the first `SendDtmfAction` is received); a reply closed at the first of
    two presses records `sent` for the first and `interrupted` for the second, also after a superseding turn
    (records ahead of the merged caller turn); a press followed by `end_call`, closed at the press, answers
    `end_call` `interrupted`, leaves the state not ended, and the next turn replies normally; TaskBrain test: the
    merged turn's second `transfer_call` yields a `TransferRequest`, tool answers `interrupted` then
    `transferring`, pairing valid; `make check` passes; append the note name to `.ralph/review-inbox/DONE` on
    approval.
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
- [x] P08-FIX-18 A lock lost (or a keepalive overrun) during the body teardown ends the session as worker_lost — see .ralph/review-inbox/2026-11-05-p08-fix-15-review.md (item 1, not a blocker, fixed here); also closes .ralph/review-inbox/2026-11-05-p08-fix-22-wip-review.md (no blockers; its one forward note is carried into the P08-T10 acceptance, DEC-016) — `CallSession._abandoned()` (lock lost or `_overran`): `_race` returns `OWNERSHIP_LOST` unless the decided end is `transferred`, a failed session's ender gets `OWNERSHIP_LOST`; Decision P08-FIX-18 (specs/13 §6) + pointers in Decisions P08-FIX-14/15 and specs/07 §18; reviewer re-ran: lint/typecheck/placeholders/spec refs pass, jscpd 0 clones, radon 2.93, audits pass, make test 13095 passed 7 skipped (cov 99.08%), make e2e 20 passed, 5 mutations of session.py each killed by the intended tests
  - Spec refs: specs/07 §3 (Decision P05-T07a), specs/13 §6 (Decisions P08-FIX-14, P08-FIX-15), specs/07 §18 (Decisions P05-T11b, P05-T13), specs/01 §2.1
  - Depends on: P08-FIX-15
  - Deliverables: `CallSession` no longer vouches for a call once the renewer lost the lock or the body teardown
    overran `TEARDOWN_KEEPALIVE_S` (both keepers stopped): `run()` then returns `OWNERSHIP_LOST` instead of the
    end that decided the race, and the ender is given `OWNERSHIP_LOST` (worker_lost: no record, no hangup, the
    workflow does both), so the media end routine never ends a call in parallel with `CallWorkflow`. Planner
    decisions (2026-11-05, DEC-017): (a) a `transferred` end stays (`_TRANSFERRED` neither records nor hangs
    up, and `worker_lost` would make the workflow hang up on the bridged caller and target); (b) a session
    that failed (body or control handler raised) still raises and closes 1011, but its ender is given
    `OWNERSHIP_LOST` instead of `None` when the session no longer vouches for the call. The lock is released
    as before (not after a loss; compare-and-delete after an overrun). Decision `P08-FIX-18` under specs/13 §6
    (+ rewording of Decision P08-FIX-14 item 2 and a pointer in Decision P08-FIX-15 and specs/07 §18 Decision
    P05-T11b step 1); AGENTS.md.
  - Acceptance: session tests: a renewer losing the lock while a gated body teardown runs → `run()` returns
    `OWNERSHIP_LOST`, the ender sees it, `va_media_calls_ended_total{end_reason="worker_lost"}` counts it and
    the lock is not released; same end for a teardown past `TEARDOWN_KEEPALIVE_S` (lock released); a
    `transferred` end stays in both cases; a control-handler failure with the lock lost during the teardown
    raises, closes 1011 and gives the ender `OWNERSHIP_LOST`; existing session tests pass; `make check` passes;
    append both note names to `.ralph/review-inbox/DONE` on approval.
  - Closed without a change now (Planner, 2026-11-05, DEC-016): note 2026-11-05-p08-fix-22-wip-review.md has
    no blockers; its item 1 (a tool with `detaches = False` must end promptly when cancelled, because
    `stop_tasks` waits for it without a bound) cannot happen with today's tools (the only non-detaching ones
    are test fakes) and the reviewer offered "fold into P08-T10 or drop": the sentence in the `detaches`
    docstring and in Decision P08-FIX-22 item 2, and the assertion that a cut returns without waiting for a
    held lookup, are now part of the P08-T10 acceptance, where the first such tool is written.
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

- [x] P08-T02c2 Pathway call tools in the media runtime (split from P08-T02c) — `va_tools.pathway_executor.PathwayCallExecutor` (`add`/`knows`/`for_refs` per node over the shared `va_tools.executor.SystemRuns`, `tool_ref_id`, `graph_tool_refs`), media `CallToolSource.pathway_tools` → `PathwayCallTools` (`ensure(graph, timeout_s=)`, tool-loading loader bounded by `SWITCH_LOAD_TIMEOUT_S` 2 s, `read_signer` → `SigningRead`), `load_call_graph` + sync `build_pathway_brain`, `PathwayToolEvents` (`call.tool` with `turn_index`/`node_id`), `PathwayEngine._prepare` reads the call's tool speech per reply pass; Decision P08-T02c2 (specs/13 §4, addendum specs/11 §12), specs/13 §6 Decision P08-FIX-18 item 2 reworded (DEC-018, closes 2026-11-05-p08-fix-18-wip-review.md by wording); first review rejected with 4 findings (maintainer note 2026-11-05-p08-t02c2-wip-review.md: blocker item 1 + items 2-4), all fixed in code with tests (usage note quotes a later graph's speech; unwritable inline ref resolves to nothing; failed/timed-out/cancelled loads and signing reads are retried at the next load of the graph; switch deadline); reviewer re-run on the final tree: lint/typecheck/placeholders pass (mypy 1617 files), check_spec_refs 0, jscpd 0 clones, radon avg 2.93, pip-audit clean, pnpm audit high exit 0, openapi current, make test 13128 passed 7 skipped (cov 99.08%; call_tools, pathway_runtime, pathway_executor 100%), make e2e 20 passed (capacity flow included); reviewer probe (git-ignored logs/): the signing read at a switch gets only what the tool load left of the 2 s (1.5 s load → 0.5 s read), no deadline at call start
  - Spec refs: specs/13 §4-§6
  - Depends on: P08-T02c1
  - Deliverables: `CallToolSource.pathway_executor` over every tool the pinned graph references (TL ids + inline objects, keyed by ref), a `PathwayToolExecutor.for_refs(refs)` implementation whose node executors share the call's runner, events and system tool runs (Decision P08-FIX-17), runtime builds system tools + executor for pathway calls and passes facts to the engine
  - Acceptance: media unit tests + runtime sim pathway call with a node tool (call.tool event, tool answer used in the reply);
    tests of the real `for_refs` (review note 2026-11-05-p08-fix-19-wip-review.md item 2, Decision P08-FIX-17
    item 2): two nodes with different tools of one name (different URL and different `speech`) each speak their
    own speech and call their own URL, and a node executor does not `handle` a custom tool its refs do not name.
  - Also closes .ralph/review-inbox/2026-11-05-p08-fix-18-wip-review.md (no blockers; Planner, 2026-11-05,
    DEC-018): its item 1 (a `transferred` end stays only when it is the end that decided the race) is closed by
    wording, option 3 of the note: reword specs/13 §6 Decision P08-FIX-18 item 2 and the matching sentence of
    the `va_media.session` module docstring to "a `transferred` end that decided the race stays; a lock lost
    (or an overrun) before the body returned it still ends `worker_lost`, as for every call the pod still
    answers for". No code change: the window is the body's unwind after the bridge (normally well under a
    second) together with a lock lost in it (Redis unreachable for 15 s or the key gone), the same outage ends
    every other call of the pod `worker_lost`, and a pod crash in that window ends the call `worker_crash`
    anyway. Append the note name to `.ralph/review-inbox/DONE` on approval.

- [x] P08-T03a Dynamic data core and the outbound pre-call fetch (split from P08-T03 by the planner, 2026-11-05: the fetcher with the worker's pre-call fetch, the per-turn refresh inside the brains, and the inbound budget in the edge are separately testable and touch three services) — `va_tools.dynamic_data` (fetch/fetch_all, `dyn:` cache, JSONPath variables with the depth 32 / U+0000 / 16 KB rules, context lines), `PreCallDynamicData` in `prefetch_call_context` (fetch → opener → save → record), `## Context` prompt block in task and pathway calls, specs/13 §7 + specs/10 §5 Decisions; approved on the second review pass (first pass: deep nesting and U+0000 broke the stores, fixed at the source); reviewer gates: `make test` 13200 passed / 7 skipped, coverage 99.08 %, `make e2e` 20 passed, lint/typecheck/placeholders/spec refs/OpenAPI drift clean, jscpd 0 clones, radon 2.94, audits clean
  - Spec refs: specs/13-tools-and-dynamic-data.md §7; specs/01-architecture.md §2.1 step 2; specs/10-llm-gateway.md §5; specs/19-events-and-webhooks.md §2 (`call.dynamic_data`)
  - Depends on: P08-T02a
  - Deliverables: `va_tools.dynamic_data` (templated request, Redis cache `dyn:{org_id}:{sha256}` with `cache_ttl_seconds`, JSONPath `response_data` mapping, `context` lines rendered after extraction, event data without the query), the `timing: "pre_call"` fetch in `prefetch_call_context` filling `CallContext.variables`/`context_sections` (and `calls.variables`) before the opener is rendered, `call.dynamic_data` events (level `warning` on failure), the `## Context` section of the voice system prompt in task and pathway calls
  - Acceptance: tests for cache hit/miss/TTL, failures (the call proceeds, warning event, no variables), templated requests (url, query, headers, body), context only for non-null variables, the prompt section in both runners; `make e2e` still passes (the outbound call path changes).

- [ ] P08-T03b Per-turn dynamic data (`timing: "every_turn"`) in calls (split from P08-T03)
  - Spec refs: specs/13-tools-and-dynamic-data.md §7 (`every_turn`)
  - Depends on: P08-T03a
  - Deliverables: re-fetch at the start of each user turn concurrently with routing (task brain and pathway engine), 800 ms budget with the stale value on timeout, variables and context lines of a refreshed fetch replace the earlier ones, `call.dynamic_data` events from the media worker, variables in the engine state snapshot
  - Acceptance: fake-clock tests for the budget and the stale fallback, cache hit, failure tolerance (the reply goes on); a runtime sim test where the prompt of the second turn carries the refreshed context.

- [ ] P08-T03c Inbound dynamic data: 1.5 s inline budget in the edge, the rest asynchronously (split from P08-T03)
  - Spec refs: specs/13-tools-and-dynamic-data.md §7 (inbound); specs/01-architecture.md §2.2 step 3; specs/06-telephony-telnyx.md §7 step 10
  - Depends on: P08-T03a
  - Deliverables: pre-call fetches inside the edge accept's context budget; fetches not finished in time go on after the answer and their variables/context reach the running call (`inject_context`) from the next turn; `call.dynamic_data` events for both
  - Acceptance: tests: a fast fetch is in the stored context before the answer; a slow fetch does not delay the answer and appears from the next turn; a failed fetch leaves the call running; ring-to-answer stages unaffected without `dynamic_data`.

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
  - Acceptance: e2e kb_lookup passes; the `lookup_knowledge` `SystemTool` declares `detaches = False`
    (specs/13 §6 Decision P08-FIX-22): a test cuts a round during a held lookup, the lookup is cancelled and
    the call reads `interrupted`, and the cut returns while the lookup's gate is still closed (it does not
    wait for the lookup). The `SystemTool.detaches` docstring and specs/13 §6 Decision P08-FIX-22 item 2 gain
    the rule "the `run` of a tool that does not detach must not delay or swallow cancellation: a cut round
    waits for it without a bound (`va_engine.tool_tasks.stop_tasks`)" (review note
    2026-11-05-p08-fix-22-wip-review.md item 1, DEC-016).

- [ ] P08-T11 E2E: tools
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list: tool_call_with_speech)
  - Depends on: P08-T02b1
  - Deliverables: `tests_e2e/test_tool_call_with_speech.py`
  - Acceptance: filler heard (recorded agent audio contains filler phrase marker), tool result used in reply.

## Phase exit gate
`make check && make e2e` pass; KB benchmark recorded.
