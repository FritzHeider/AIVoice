# Scratchpad (loop started 2026-11-05T19:09Z)

## Planner 2026-11-05T19:25Z: where the previous loop stopped

The previous loop hit its runtime limit at 19:09:04Z, 5 s after the Builder emitted `build.done` for
P08-T02c2. Nobody reviewed it: the task is unticked and the whole implementation sits uncommitted in the work
tree on HEAD 16a65e0 (last source write 18:42:44Z, nothing touched since). The old scratchpad (with
`## Plan P08-T02c2` and `## Build P08-T02c2`) was wiped by the loop restart; the plan below replaces it.

Inbox: one note is not in `.ralph/review-inbox/DONE`: `2026-11-05-p08-fix-18-wip-review.md` (no blockers).
The previous Planner already attached it to P08-T02c2 (plan/phase-08.md "Also closes ..." bullet, DEC-018:
closed by wording, option 3 of the note). The rewording is in the work tree (specs/13 §6 Decision P08-FIX-18
item 2 and the `va_media.session` module docstring). No new FIX task is needed; the note name goes to `DONE`
when the Reviewer approves P08-T02c2.

## Plan P08-T02c2 (verify the existing work tree, do NOT rebuild)

Task: `plan/phase-08.md` "P08-T02c2 Pathway call tools in the media runtime" (spec refs specs/13 §4-§6,
depends on P08-T02c1 which is ticked).

### What is already in the work tree (read it, keep it)

- `packages/va_tools/src/va_tools/pathway_executor.py` (new): `PathwayCallExecutor(runner=, events=, system=)`
  with `add(stored, refs)`, `knows(tool_id)`, `tool_speech`, `handles(name)`, `for_refs(refs) ->
  CallToolExecutor`, `aclose(timeout_s)`; `tool_ref_id(ref) -> uuid.UUID | None`; `graph_tool_refs(graph)`.
- `packages/va_tools/src/va_tools/executor.py`: `SystemRuns` split out of `CallToolExecutor`
  (`CallToolExecutor(system=tools | SystemRuns)`), shared by a call's node executors.
- `apps/media_worker/src/va_media/call_tools.py`: `CallToolSource.pathway_tools(context, graph, events,
  system=) -> PathwayCallTools` (`executor`, `ensure(graph)`, `loader(inner)`), public `signer(org_id)`,
  `_CallSigner`, `_ToolLoadingLoader`. (The plan file's deliverable text says `pathway_executor`; the built
  name is `pathway_tools` and Decision P08-T02c2 documents it. Fine.)
- `apps/media_worker/src/va_media/pathway_runtime.py`: `load_call_graph(context, tools)`, sync
  `build_pathway_brain(..., active=, state=, call_tools=, system_tools=)`, `PathwayToolEvents(recorder, state)`.
- `apps/media_worker/src/va_media/runtime.py`: `CallRuntime._pathway_brain` (graph → start state → system
  tools → call tools → brain; exit stack: executor drain, brain close, snapshot flush).
- `packages/va_engine/src/va_engine/pathway_call.py`: `pathway_facts(context, now=, system_tools=, tool_fillers=)`.
- `apps/api/src/va_api/services/tools.py`: `parse_tool_id` uses `va_tools.pathway_executor.tool_ref_id`.
- `apps/media_worker/src/va_media/session.py`: docstring rewording only (DEC-018).
- Specs: specs/13 §4 `Decision (2026-11-05) P08-T02c2` (6 items), §6 P08-T02b2 item 4 pointer, §6 Decision
  P08-FIX-18 item 2 rewording; specs/11 §12 addendum. AGENTS.md entry `2026-11-05 (P08-T02c2)`.
- Tests: `packages/va_tools/tests/test_pathway_executor.py` (9 tests), `test_pathway_executor_engine.py`
  (`test_each_node_speaks_and_runs_its_own_tool_of_one_name`), `test_executor.py` (SystemRuns),
  `support/tools_helpers.py` (`GatedSystem`, `RecordedEvents`); media `test_call_tools.py` (4 new pathway
  tests incl. the cancelled-load one), `test_pathway_runtime.py` (2 new), `test_runtime_pathway_sim.py`
  (`test_a_node_tool_runs_and_its_answer_is_used_in_the_reply`), `test_runtime_tools_sim.py`,
  `support/runtime_rig.py` (`sim_backends`, `local_tool_source`); `va_engine/tests/test_pathway_brain.py`.

### Planner sanity check of that tree (19:15-19:20Z, real output)

- `uv run ruff check .` → All checks passed; `ruff format --check .` → 1617 files already formatted;
  `check_no_placeholders.py` → 0 finding(s); `check_spec_refs.py` → 0 problem(s); `uv run mypy` → no issues
  in 1617 source files.
- `uv run pytest packages/va_tools <the 5 touched media test files> packages/va_engine/tests/test_pathway_brain.py
  -n 4 --no-cov` → 233 passed in 72 s.
- Not run by the Planner: `make test` (full, coverage), jscpd, radon, audits, `make openapi` drift, `make e2e`.

### Builder steps

1. Do not rewrite. Read the diff once (`git diff` + the three untracked files) against the acceptance below;
   change code only if a check fails or an acceptance point is not covered.
2. Acceptance checklist (each point must be pinned by a named test; add a test only where one is missing):
   a. media unit tests for `PathwayCallTools` (load of the pinned graph's stored tools, a later graph brings
      its tools, load failure keeps inline tools, cancelled load is retried) and `PathwayToolEvents`
      (`turn_index`/`node_id` on `call.tool`);
   b. runtime sim pathway call with a node tool: a `call.tool` event is recorded and the tool's answer is
      used in the reply (`test_a_node_tool_runs_and_its_answer_is_used_in_the_reply`);
   c. real `for_refs`: two nodes with different tools of one name (different URL, different `speech`) each
      speak their own speech and call their own URL; a node executor does not `handle` a custom tool its
      refs do not name (`test_pathway_executor.py`, `test_pathway_executor_engine.py`);
   d. specs/13 §6 Decision P08-FIX-18 item 2 and the `va_media.session` docstring carry the DEC-018 wording
      ("a `transferred` end that decided the race stays; a lock lost, or an overrun, before the body returned
      it still ends `worker_lost`");
   e. system tools on pathway reply nodes follow Decision P08-T02c2 item 3 (never `transfer_call`; without a
      call tool source only `end_call`/`press_button`): check a test asserts the offered set; if none does,
      add one to `apps/media_worker/tests/test_pathway_runtime.py`.
3. Things worth a second look while reading (fix only if actually wrong, with a test):
   - `PathwayCallTools.ensure` calls `load_call_tools(..., wanted)` even when `wanted` is empty (a graph whose
     refs are all inline or already known): confirm `load_call_tools` returns without opening a session for
     an empty id list, else guard it (a pointless tenant session per graph switch on the call path).
   - the exit-stack order in `CallRuntime._pathway_brain` (LIFO: executor drain → brain close → saver flush)
     matches Decision P08-T02c2 item 6.
   - reference cycles (specs/07 §9 Decision P08-FIX-3): `PathwayToolEvents` holds recorder + state only. The
     pre-existing loop `recorder.tagger = brain → engine → RecorderEvents → recorder` is runtime task
     `fix:pathway-call-cycles` (open, priority 3); it is NOT part of this task, do not widen the scope.
4. Gates, all run in this iteration (evidence must be from this iteration; PATH needs
   `/home/ralph/.local/bin`): `make fmt lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`,
   `make test` in the background with a heartbeat monitor (AGENTS.md P05-T10d: > 300 s of silence kills the
   iteration), `make openapi` shows no drift (no route changed, expect none), jscpd (`npx --yes jscpd@4
   --silent`, 0 clones), radon (`uvx radon cc -s -a`), audits (pip-audit on the CI export
   `uv export --frozen --all-packages --no-hashes --no-emit-workspace`; `corepack pnpm audit --audit-level high`),
   and `make e2e` (the task touches the call path; the capacity flow may hit the known cold-stack start-burst
   underrun, task `fix:capacity-start-burst-stall`: rerun once and report both runs).
5. Emit `build.done` with the plain-text evidence string (`tests: pass, lint: pass, typecheck: pass,
   audit: pass, coverage: pass, duplication: pass, complexity: <n>,`), and write `## Build P08-T02c2` here.

### For the Reviewer (on approval)

Tick P08-T02c2 with evidence, append `2026-11-05-p08-fix-18-wip-review.md` to `.ralph/review-inbox/DONE`,
commit (`feat(media): pathway calls run node tools and system tools`, footer `Task: P08-T02c2`).

### Risks

- The previous Builder's gate numbers (make test 13121 passed, e2e 20 passed) are from the dead loop and are
  not evidence for this one; they only say what to expect.
- e2e on a stack built from an older image: `make e2e` rebuilds, keep it that way (no `-k` shortcuts).

## Build P08-T02c2 (Builder, 2026-11-05T19:12Z-19:41Z)

Verified the existing work tree against the plan; did not rebuild.

Read: `pathway_executor.py`, the diffs of `call_tools.py`, `pathway_runtime.py`, `runtime.py`, `session.py`,
`pathway_call.py`, `services/tools.py`, specs/11 + specs/13, plan/phase-08.md.

Second-look items (all hold, no code change):
- `load_call_tools` returns `()` before opening a session when the id list is empty, so `ensure` with nothing
  wanted costs no tenant session.
- `CallRuntime._pathway_brain` pushes saver.aclose, brain.aclose, executor.aclose → LIFO: executor drain, brain
  close, snapshot flush (Decision P08-T02c2 item 6).
- `PathwayToolEvents` holds the recorder and the state only.

Acceptance → tests:
a. `test_call_tools.py`: `test_pathway_tools_load_the_graphs_stored_tools`,
   `test_a_graph_loaded_later_brings_its_tools`, `test_a_failed_pathway_tool_load_keeps_the_inline_tools`,
   `test_a_cancelled_tool_load_is_tried_again_by_the_next_load_of_the_graph`; `test_pathway_runtime.py`:
   `test_tool_events_carry_the_engine_turn_and_node_of_the_report`.
b. `test_runtime_pathway_sim.py::test_a_node_tool_runs_and_its_answer_is_used_in_the_reply` (call.tool row with
   turn_index 1 / node_id lookup, answer in the follow-up request, variable merged, signed request).
c. `test_pathway_executor.py` (`test_two_tools_of_one_name_on_different_nodes_run_their_own_definition`,
   `test_a_node_executor_handles_only_what_its_refs_name`, 7 more) and
   `test_pathway_executor_engine.py::test_each_node_speaks_and_runs_its_own_tool_of_one_name`.
d. DEC-018 wording present in specs/13 §6 Decision P08-FIX-18 item 2 and the `va_media.session` docstring.
e. Was only partly pinned (the sim test asserts `[end_call, press_button, lookup_order]` with a tool source).
   ADDED in this iteration, `apps/media_worker/tests/test_pathway_runtime.py`:
   `test_reply_nodes_offer_only_the_system_tools_the_call_can_run_and_never_a_transfer` (a call with a transfer
   number, SMS-capable, callbacks allowed: no call tool source → `[end_call, press_button]`; a source running
   send_sms → + `send_sms`; running both → + `schedule_callback`; never `transfer_call`), via a `_brain` helper
   and `FakeSource` as the gateway; the older build test now uses the helper
   (`test_the_brain_runs_on_the_given_graph_and_state`). Only change to the tree besides one AGENTS.md sentence.

Gates, all run in this iteration:
- `make fmt lint typecheck placeholders` → exit 0 (mypy: no issues in 1617 source files; placeholders 0 finding(s)).
- `uv run python scripts/check_spec_refs.py` → 0 problem(s) (re-run after the AGENTS.md sentence).
- `make test` → 13122 passed, 7 skipped in 887 s, total coverage 99.08 % (call_tools.py 100 %, pathway_runtime.py
  100 %, pathway_executor.py 100 %, pathway_call.py 100 %, va_tools/executor.py 99 %).
- `npx --yes jscpd@4 --silent` → 0 exact clones. `uvx radon cc -s -a apps packages scripts` → average A (2.93).
- pip-audit on `uv export --frozen --all-packages --no-hashes --no-emit-workspace` → No known vulnerabilities;
  `corepack pnpm audit --audit-level high` → exit 0 (1 moderate).
- `uv run python scripts/check_openapi_drift.py` → both files current.
- `make e2e` (rebuilt images) → 20 passed, 107 deselected in 418 s, first run (capacity flow included, no rerun).

Observation (no action taken): `voiceagents-media-1` had a second start at 19:07:59Z, before this iteration
began; it is the previous Builder's e2e worker-crash flow (`kill_media`/`restart_media`), not a fault.

For the Reviewer: tick P08-T02c2, append `2026-11-05-p08-fix-18-wip-review.md` to `.ralph/review-inbox/DONE`,
commit `feat(media): pathway calls run node tools and system tools` (footer `Task: P08-T02c2`).

## Review P08-T02c2 (Reviewer, 2026-11-05T19:27Z-) — REJECTED, 4 findings

Gates re-run on the work tree (HEAD 16a65e0): `make lint typecheck placeholders` → exit 0 (mypy: no issues in
1617 source files; placeholders 0 finding(s)). `make test`: see the last line of this section.

Read: the whole diff + the three untracked files against specs/13 §4-§6 and specs/11 §12. Nothing found in:
the `SystemRuns` extraction, per-node `for_refs` (first ref per name, reserved names dropped, shared runs, one
drain), the exit-stack order (executor drain → brain close → snapshot flush), `tool_ref_id` + the api
`parse_tool_id`, `PathwayToolEvents` (`turn_index`/`node_id` on `call.tool`, listed in
`pathway_logs[].decision.tools` by the sim test), the offered system-tool set test (acceptance e), the
DEC-018 rewording (specs/13 §6 Decision P08-FIX-18 item 2 + `va_media.session` docstring), the tests' quality.

A maintainer note about this very work tree is in the inbox and not in `DONE`:
`.ralph/review-inbox/2026-11-05-p08-t02c2-wip-review.md` (written 19:20:22Z, after the Planner looked). Its
item 1 is a BLOCKER and I found the same defect independently before reading it; items 2-4 are non-blocking.
Fix them inside P08-T02c2 (the tree is uncommitted; no separate FIX task is needed). Do not edit the note; the
Reviewer appends BOTH note names (`...p08-fix-18-wip-review.md`, `...p08-t02c2-wip-review.md`) to `DONE` on
approval.

1. BLOCKER (note item 1; reproduced by me). specs/13 §4 Decision P08-T02c2 item 5 says the call's tool speech
   "(quoted by the tool usage note)" is, "while the call has none", searched in "the refs of a graph loaded
   later". `PathwayCallExecutor.add` (packages/va_tools/src/va_tools/pathway_executor.py:96-98) does that for
   the `tool_speech` property, but its only reader is `PathwayEngine.__init__`
   (packages/va_engine/src/va_engine/engine.py:278-279), once. My probe (engine_rig + a `PathwayCallExecutor`
   whose `add([], [inline tool with speech "Checking the hours."])` runs AFTER the engine was built):
   `executor.tool_speech == "Checking the hours."` but the dialogue request's note reads `the caller will hear
   "One moment."`; with `add` before the engine is built the note quotes "Checking the hours.". So the
   Decision and the code contradict each other and the later-`add` search is dead behaviour.
   Fix, one of:
   a. make it true: refresh `self._dialogue.tool_speech` from `ports.tools.tool_speech` where the reply pass
      computes its offer (engine.py `_prepare`, next to `self._offer = self._tools.offer(...)`, ~line 908),
      plus an engine-level test: pinned graph without a speech, switch (Transfer Pathway or forwarding) to a
      graph whose tool has one, assert the next dialogue request's usage note quotes it (and that a speech
      once set stays); or
   b. make the spec true: drop the "graph loaded later" half of item 5, the `if self._tool_speech is None`
      re-search on later `add` calls and the module-docstring sentence, trim
      `test_the_tool_speech_is_the_first_speech_among_the_refs`, keep the AGENTS.md sentence.
   Either way item 5, the code, the `pathway_executor` docstring and AGENTS.md must say the same thing.

2. (note item 4; reproduced by me) `PathwayCallExecutor._inline_spec`
   (packages/va_tools/src/va_tools/pathway_executor.py:144) builds its cache key with `dumps(ref,
   sort_keys=True)` BEFORE validating. `va_common.json.dumps` raises `TypeError: Integer exceeds 64-bit range`
   for a `JsonValue` integer beyond 64 bits. Probe: an inline tool `{"name": "lookup", "description": "d",
   "url": "https://api.acme.test/x", "body": {"n": 2**70}}` passes `va_engine.models.graph.load_graph`, then
   `PathwayCallExecutor.add([], graph_tool_refs(g))` and `.for_refs(...)` both raise `TypeError`. Decision
   P08-T02c2 item 1 says an inline ref that cannot be used "resolves to nothing"; here the error escapes
   `PathwayCallTools.ensure` (call start: not a `PathwayUnavailableError`; mid-call: out of the reply through
   the loader or `NodeTools.offer`). Fix: treat a ref whose key cannot be built as invalid (same
   `tools.inline_tool_invalid` line, no ref content in the log, resolves to `None`; it cannot be cached by
   that key, so make sure it is not logged on every reply pass, or say in the Decision that it is). Test in
   `packages/va_tools/tests/test_pathway_executor.py` (`add` and `for_refs` with such a ref: no raise, the
   other refs still resolve).

3. (note item 2, non-blocking: fix it or decide it in writing) `PathwayCallTools.ensure`
   (apps/media_worker/src/va_media/call_tools.py:196-204): `self._tried.update(wanted)` runs after the
   `except`, so ids are marked tried when the lookup could not run at all (database, KMS), and
   `_signing_read = True` is set when `source.signer` returned `None` because the read failed. The method
   already treats a cancelled load as "not tried"; a failed one learned just as little, and every later load
   of the graph is a free retry point. Either (preferred) mark ids tried only when `load_call_tools`
   returned, and set `_signing_read` only when the read worked (a signer was built, or the org is really
   gone: needs `signer` to tell "failed" from "no secrets"), extend specs/13 §4 Decision P08-T02c2 item 2
   ("... and is looked up again at the next load of that graph"; same for the signing secrets) and add tests
   (failed first load + working second load brings the tool; failed signer read is retried); or keep the
   code and state in item 2 that a failed load and a failed signing-secret read also count once per call,
   with the reason. Today item 2 says "An id is looked up once per call" and "A load failure ... leaves the
   graph's inline tools", which does not say which of the two holds for a failure.

4. (note item 3, non-blocking: fix it or decide it in writing) `_ToolLoadingLoader.load`
   (call_tools.py:219-222) awaits `ensure` inside the caller's turn with no deadline of its own: a tenant
   session, one KMS decrypt per tool with secret headers, and at the first graph with refs a global session
   plus the signing-secret decrypt. Before this change a graph switch cost only the cached graph load; now a
   slow pool or KMS is dead air mid-conversation (bounded only by the pool/command/KMS timeouts). Either
   bound the load when it runs from the loader (`va_common.waits.within(clock, ...)`, about 2 s; a timeout
   handled like a failure of finding 3: `media.tools_unavailable`, not tried; test on a `ManualClock` with a
   held session, like `_HeldOnce`), and say so in item 2; or record in item 2 why the existing timeouts are
   the bound (name them and their values) and that a switch waits that long at worst.

Not findings (already tracked): this change adds engine → ports.tools → `PathwayToolEvents` → recorder to
the `recorder.tagger = brain` loop of runtime task `fix:pathway-call-cycles`; out of scope here.

For the Builder: keep everything else as it is. After the fixes re-run every gate of the plan's step 4
(`make e2e` included: the call path changes) and update the AGENTS.md entry `2026-11-05 (P08-T02c2)` where a
sentence becomes false (e.g. "The engine reads `ports.tools.tool_speech` once at construction" under fix 1a).

`make test` (Reviewer re-run, finished 19:55Z): 13122 passed, 7 skipped in 817 s, total coverage 99.08 %,
exit 0. The gates are green; the rejection rests on findings 1-4 above (1 is the maintainer's blocker). Not
re-run by the Reviewer because the review was already decided: `make e2e`, jscpd, radon, audits.

## Build P08-T02c2, second pass (Builder, 2026-11-05T19:57Z-): the 4 review findings

Read `.ralph/review-inbox/2026-11-05-p08-t02c2-wip-review.md` and `## Review P08-T02c2`. All four fixed in
code (none merely "decided in writing"); everything else in the tree untouched.

1. BLOCKER, option a (make it true). `PathwayEngine._prepare` now reads `ports.tools.tool_speech` on every
   reply pass (the one-time read in `__init__` is gone), so a speech a later graph brought reaches the usage
   note. Test `packages/va_tools/tests/test_pathway_executor_engine.py::
   test_the_usage_note_quotes_the_tool_speech_a_later_graph_brought` (real `PathwayCallExecutor`, pinned graph
   whose tool has no speech, Transfer Pathway to a graph with two speeches: notes read "One moment.", then
   "Checking the order." twice, i.e. once set it stays). Proved to fail without the engine change (reverted the
   engine patch: `['One moment.', 'One moment.', 'One moment.']`). Spec item 5, the `pathway_executor` docstring
   and AGENTS.md now say the same.
2. `PathwayCallExecutor._inline_spec` catches the `TypeError` of the cache key: the ref resolves to nothing,
   logged `tools.inline_tool_invalid` (`errors=1`) once per call for all such refs (flag, since there is no
   key to cache by). Test `test_pathway_executor.py::
   test_an_inline_tool_that_cannot_be_written_as_json_resolves_to_nothing` (`add` + two `for_refs`, other ref
   still resolves and gives the tool speech, one log line, the number not in the log). Spec item 1 extended.
3. `PathwayCallTools`: ids are marked tried only when `load_call_tools` returned; the signing read is final
   only when it worked or the org is gone (`CallToolSource.read_signer` → `SigningRead(signer, final)`;
   `signer()` kept for task mode). Tests in `apps/media_worker/tests/test_call_tools.py`:
   `test_a_failed_tool_load_is_tried_again_by_the_next_load_of_the_graph` (first tenant + first global session
   fail; second load brings the tool and signs) and
   `test_the_signing_secrets_of_an_org_that_is_gone_are_read_once`.
4. The loader's `ensure(graph, timeout_s=SWITCH_LOAD_TIMEOUT_S)` (2 s, `va_common.waits.within` on the source's
   clock, tools + signing secrets share the budget; call start stays unbounded). A tool lookup cut off logs
   `media.tools_unavailable` `TimeoutError`, is not tried, and skips the signing read; a signing read cut off
   logs `media.tool_signing_unavailable` `TimeoutError`. Tests (ManualClock + a held session):
   `test_a_tool_load_at_a_graph_switch_gives_up_after_its_deadline`,
   `test_a_signing_read_at_a_graph_switch_shares_the_deadline_and_is_tried_again`. Spec item 2 rewritten
   (retry rule, signing rule, deadline). Test-only: `_switch(..., clock=)` defaults to `SystemClock` (a
   `FrozenClock` would time every switch load out at once); the once-only database fakes are one
   `_FirstSessions` (+ `_Held`, `_fails_once`) so jscpd stays at 0.

Files touched in this pass: `packages/va_engine/src/va_engine/engine.py`,
`packages/va_tools/src/va_tools/pathway_executor.py`, `apps/media_worker/src/va_media/call_tools.py`, the three
test files named above, `specs/13-tools-and-dynamic-data.md` (Decision P08-T02c2 items 1, 2, 5), `AGENTS.md`.

Gates of the second pass, all run in this iteration on the final tree (20:00Z-20:32Z):
- `make fmt lint typecheck placeholders` → exit 0; after the last test-helper refactor again: `ruff check` All
  checks passed, `ruff format --check .` 1617 files already formatted, `uv run mypy` no issues in 1617 source
  files, `make placeholders` 0 finding(s), `check_spec_refs.py` 0 problem(s).
- `make test` → 13128 passed, 7 skipped in 901 s, total coverage 99.08 % (`va_media/call_tools.py` 100 %,
  `va_tools/pathway_executor.py` 100 %, `va_engine/engine.py` 98 %). A first run was stopped by me on purpose
  (exit 144) because jscpd flagged a 6-line clone between two test fakes and the test file had to change.
- `npx --yes jscpd@4 --silent` → 0 exact clones. `uvx radon cc -s -a apps packages scripts` → A (2.93).
- pip-audit on the CI export → No known vulnerabilities; `corepack pnpm audit --audit-level high` → exit 0
  (1 moderate). `check_openapi_drift.py` → both files current.
- `make e2e` (images rebuilt) → 20 passed, 107 deselected in 419 s, first run, capacity flow included.

For the Reviewer: on approval tick P08-T02c2, append BOTH `2026-11-05-p08-fix-18-wip-review.md` and
`2026-11-05-p08-t02c2-wip-review.md` to `.ralph/review-inbox/DONE`, commit
`feat(media): pathway calls run node tools and system tools` (footer `Task: P08-T02c2`).

## Review P08-T02c2, second pass (Reviewer, 2026-11-05T20:20Z-20:55Z) — APPROVED

Gates re-run on the final work tree (HEAD 16a65e0 + tree): `make lint typecheck placeholders` exit 0 (mypy:
no issues in 1617 source files; 0 placeholder findings); `check_spec_refs.py` 0 problems; jscpd 0 exact
clones; radon average A (2.93); `check_openapi_drift.py` both current; pip-audit (CI export) no known
vulnerabilities; `pnpm audit --audit-level high` exit 0 (1 moderate); `make test` 13128 passed, 7 skipped in
891 s, coverage 99.08 % (`call_tools.py`, `pathway_runtime.py`, `pathway_executor.py` 100 %, `engine.py`
98 %); `make e2e` 20 passed, 107 deselected in 418 s (first run).

The four findings, each read in code + spec + test:
1. (blocker) `PathwayEngine._prepare` reads `ports.tools.tool_speech` per reply pass, the `__init__` read is
   gone; engine-level test with a real `PathwayCallExecutor` and a Transfer Pathway switch; spec item 5,
   module docstring and AGENTS.md agree.
2. `_inline_spec` catches the cache-key `TypeError`: resolves to nothing, one `tools.inline_tool_invalid`
   line per call, no ref content logged; test covers `add` + repeated `for_refs`.
3. Ids are marked tried only in the `else` of the load; `read_signer` → `SigningRead(signer, final)`;
   failed load + failed signing read retried at the next load (test), a gone org read once (test).
4. `_ToolLoadingLoader` passes `SWITCH_LOAD_TIMEOUT_S` (2 s, `va_common.waits.within` on the source clock);
   timeout tests on a `ManualClock` for the tool lookup and for the signing read.

Reviewer probe (`logs/probe_review_t02c2_test.py`, git-ignored): the committed signing-deadline test spends
no clock time in the tool load, so it would also pass with a fresh 2 s for the signing read. The probe makes
the tool load take 1.5 s: the signing read then sleeps 0.5 s (shared budget holds), and `ensure` without
`timeout_s` registers no deadline. Passed; not a finding, the code is right.

Inbox: no note newer than 19:20:22Z; both open notes (`...p08-fix-18-wip-review.md`,
`...p08-t02c2-wip-review.md`) appended to DONE. Still open and out of scope: runtime task
`fix:pathway-call-cycles` (the recorder.tagger loop now also runs through ports.tools).
