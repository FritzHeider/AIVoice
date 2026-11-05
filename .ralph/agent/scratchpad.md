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

## Planner 2026-11-05T21:05Z: after P08-T02c2 (commit b690722)

Inbox: every note is listed in `DONE` (checked with `comm`), nothing new. Next unticked task was P08-T03
(Dynamic data, depends on P08-T02a ✓). It spans three services (worker pre-call fetch, media per-turn refresh
inside both brains, edge inbound budget + `inject_context`), so I split it in `plan/phase-08.md`:
P08-T03a (core + outbound pre-call + prompt section), P08-T03b (`every_turn`), P08-T03c (inbound). This
iteration plans P08-T03a only.

Found while planning (NOT part of T03a, runtime task `fix:http-body-big-int`): `HttpExecutor.execute` and
`ToolRunner._capped`/`excerpt` raise `TypeError: Integer exceeds 64-bit range` for a rendered body holding an
int beyond 64 bits (`va_common.json.dumps` is orjson) although both promise never to raise. Probes:
`logs/probe_plan_t03.py`, `logs/probe_plan_t03b.py` (model arguments as JSON text are safe: orjson parses such
ints as floats; a stored body literal is not). T03a must not depend on that promise (rule 7 below).

## Plan P08-T03a (dynamic data core + outbound pre-call fetch + `## Context` prompt section)

Facts the plan rests on (read in code today):
- `va_domain.calls.tools.DynamicDataSpec`/`ResponseDataItem` exist (url, method GET, headers, query, body,
  cache false, cache_ttl_seconds 300 (0..86 400), timing pre_call|every_turn, response_data ≤ 20 items with
  name/data/context). `SendCallConfig.dynamic_data` carries them; Send Call caps the list at 10.
- `CallContext.variables` / `context_sections` exist and are empty today. `TaskBrain` starts its state from
  `context.variables`, `va_engine.pathway_call.start_state` does the same, `render_opener` and
  `OpenerPrerender.opener_for_call` render with `context.variables` — so variables set BEFORE the opener is
  rendered reach the opener, the templated prompts and "Known information" with no further change.
  `context_sections` has no reader yet.
- `va_tools.render.render_request(ToolSpec, scope)` renders url/query/headers/body and masks headers;
  `HttpExecutor(client, clock=, allow_http=, call_id=, signer=)` sends an `HttpCall` (extract pairs → JSONPath);
  `va_tools.jsonpath.extract_all(body, pairs)` for a cached body; `va_engine.handlers.reports.redacted_url`.
- Worker: `CallContextActivities(database, store, clock, prerender=)`; `WorkerActivities.build` owns the
  `EventPublisher(redis, call_events=None, router=DbWebhookRouter(database))`; `tools_client(settings,
  resolver=system_resolver)` is built inside `va_worker.main._text_deps`; `insecure_urls_allowed(va_env)`.
  `va_workflows.activities.events`: `call_event`, `persist_events(session, publisher, events)`,
  `broadcast_events`. `CallRepo(session, org_id).merge_variables(call_id, variables)`.
- A task-mode call without tools has no engine-state saver, so nothing would copy pre-call variables into
  `calls.variables`: the activity must write them.
- `va_common.waits.within(awaitable, clock, timeout_s) -> bool` (cancels on timeout). A `FrozenClock` sleep
  returns at once, so anything bounded by `within` needs `SystemClock` or `ManualClock` in tests.
- va_workflows does not depend on va-tools yet (va-tools depends on va-common/db/domain/engine/events/llm: no cycle).

### 1. `packages/va_tools/src/va_tools/dynamic_data.py` (new)

```python
PRE_CALL_TIMEOUT_MS: Final = 5_000     # each pre-call request (HttpCall.timeout_ms; connect ≤ 2 s), retries 0
PRE_CALL_BUDGET_S: Final = 6.0         # the whole pre-call phase, on the injected clock
CONTEXT_LIMIT: Final = 2_000           # characters of one rendered context line (va_tools.render.excerpt)
MAX_VARIABLE_BYTES: Final = 16_384     # compact JSON of one extracted value; larger → not set
MAX_CACHED_BYTES: Final = 262_144      # compact JSON of a cached response body; larger → not cached

type DynamicOutcome = HttpOutcomeKind | Literal["render_error", "error"]

def cache_key(org_id: uuid.UUID, method: str, url: str, body: JsonValue) -> str:
    """dyn:{org_id}:{sha256 hex of "<METHOD>\n<rendered url with merged query>\n<body>"}; body = stdlib
    json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False), "" for None (stdlib: no
    64-bit integer limit)."""

@dataclass(frozen=True, slots=True, kw_only=True)
class DynamicDataResult:
    index: int                         # position in config.dynamic_data
    timing: Literal["pre_call", "every_turn"]
    outcome: DynamicOutcome
    url: str                           # redacted_url(rendered url): no user info, query, fragment; "" if unknown
    method: str
    status_code: int | None
    duration_ms: int
    cached: bool
    variables: dict[str, JsonValue]    # the variables set, in response_data order (only when outcome == "ok")
    sections: dict[str, str]           # variable name -> rendered context line (only variables that were set)
    failed (property): outcome != "ok"
    level (property): "warning" if failed else "info"
    def event_data(self) -> dict[str, JsonValue]:
        # {url, method, status_code, duration_ms, variables_set: [names], cached, outcome, timing, index}
    def message(self) -> str:
        # ok: "Dynamic data: {n} variable(s) set" (+ " (cached)" when cached); else "Dynamic data failed: {outcome}"
        #     (+ " ({status_code})" for http_error)

def merge_results(results: Sequence[DynamicDataResult]) -> tuple[dict[str, JsonValue], list[str]]:
    """Variables and context lines of results in index order: a later result wins a variable name, and its
    line (or the absence of one) replaces the earlier line of that name."""

class DynamicDataFetcher:
    def __init__(self, http: HttpPort, *, redis: Redis | None, org_id: uuid.UUID, clock: Clock) -> None
    async def fetch(self, spec: DynamicDataSpec, index: int, scope: TemplateScope, *, timeout_ms: int) -> DynamicDataResult
    async def fetch_all(self, specs: Sequence[tuple[int, DynamicDataSpec]], scope: TemplateScope, *,
                        timeout_ms: int, budget_s: float) -> list[DynamicDataResult]
```

Rules of `fetch` (write them into the module docstring and the spec Decision):
1. Request = the spec as an inline `ToolSpec` (`tool_id=None`, `name=f"dynamic_data_{index}"`, description "",
   `response_map={item.name: item.data}`, `timeout_ms`, `retries=0`) rendered by `va_tools.render.render_request`
   in `scope` → same rules as tools (url percent-encoding, `query` merged, headers as text, body type-preserving).
   `ToolRenderError` → outcome `render_error`, nothing sent, `url=""`.
2. Cache is used only when `spec.cache and spec.cache_ttl_seconds > 0 and redis is not None`. Lookup `GET key`:
   a value that parses as `{"status": int, "body": JsonValue}` is a hit (`cached=True`, outcome `ok`, no
   request, extraction with `extract_all`); an unreadable value is a miss (`tools.dynamic_data_cache_invalid`);
   a Redis error is a miss (`tools.dynamic_data_cache_unavailable`, `error_type`).
3. Miss → `http.execute(call)`. Stored (`SET key value EX ttl`) only for a 2xx, not truncated, body whose
   compact JSON is ≤ `MAX_CACHED_BYTES`; a Redis error on the write is logged and ignored. Failures are never
   cached. Headers are not part of the key (spec), the org id is.
4. Variables (outcome `ok` only): for each `response_data` item in order, the JSONPath value; `None` is not
   set; a value whose compact JSON (stdlib json) exceeds `MAX_VARIABLE_BYTES` is not set
   (`tools.dynamic_data_value_too_large`, name only). Duplicate names: the last item wins.
5. Context: for each variable that was set and whose item has a non-blank `context`: rendered with
   `dataclasses.replace(scope, variables={**scope.variables, **variables_of_this_fetch})`, stripped, cut with
   `excerpt(text, CONTEXT_LIMIT)`; blank → no line; `TemplateSyntaxError` → no line
   (`tools.dynamic_data_context_invalid`, index + name).
6. `duration_ms` on `clock.monotonic()` around the whole fetch. One log line per fetch
   `tools.dynamic_data` (index, timing, outcome, status, cached, duration_ms, variables=count): never the
   URL path/query, headers, bodies or values.
7. `fetch` never raises: every exception but `CancelledError` between 1 and 5 (a port that raises, the
   big-int `TypeError` above, a Redis client bug) is outcome `error`, logged `tools.dynamic_data_failed`
   with `error_type` only.
`fetch_all`: one task per spec, all concurrent, every one rendered in the SAME scope (a pre-call fetch never
sees another fetch's variables); `within(gather(tasks), clock, budget_s)`; a task not finished at the budget
is cancelled, awaited, and reported as outcome `timeout` (`duration_ms = round(budget_s * 1000)`,
`cached=False`); results in spec order. Empty `specs` → `[]` without touching the clock.

Export from `va_tools/__init__.py` only if that file re-exports modules today (look; keep its style).

### 2. `packages/va_workflows/src/va_workflows/activities/dynamic_data.py` (new) + `call_context.py`

```python
@dataclass(frozen=True, slots=True)
class PreCallData:
    context: CallContext                      # with the merged variables / context_sections
    results: tuple[DynamicDataResult, ...]

class PreCallDynamicData:
    def __init__(self, *, database: Database, redis: Redis, http: httpx.AsyncClient,
                 publisher: EventPublisher, clock: Clock, allow_http: bool) -> None
    async def fetch(self, context: CallContext) -> PreCallData
    async def record(self, data: PreCallData) -> None
```
- `fetch`: specs = `[(i, s) for i, s in enumerate(context.config.dynamic_data) if s.timing == "pre_call"]`;
  none → `PreCallData(context, ())`. Scope = `TemplateScope(variables=context.variables,
  request_data=config.request_data, builtins=call_builtins(context, now=clock.now()), timezone=config.timezone)`.
  Executor per call: `HttpExecutor(http, clock=clock, allow_http=allow_http, call_id=str(context.call_id))`
  (no signer: dynamic data requests are not signed, see Decision). `DynamicDataFetcher(executor, redis=redis,
  org_id=context.org_id, clock=clock).fetch_all(specs, scope, timeout_ms=PRE_CALL_TIMEOUT_MS,
  budget_s=PRE_CALL_BUDGET_S)`; `merge_results` → `context.model_copy(update={"variables": {**context.variables,
  **variables}, "context_sections": [*context.context_sections, *lines]})`.
- `record`: no results → nothing. Else one `call.dynamic_data` event per result (`call_event(clock, org_id,
  call_id, EventType.CALL_DYNAMIC_DATA, message=result.message(), data=result.event_data(),
  level=result.level)`); in ONE `tenant_session(org_id)`: `persist_events` + (when any variable was set)
  `CallRepo(session, org_id).merge_variables(call_id, variables)`; after the commit `broadcast_events`.
  Never raises: any exception (also the retryable `EventsUnavailable` `ApplicationError`) is logged
  `call_context.dynamic_data_record_failed` (`error_type`) and swallowed — dynamic data never fails the call.
- `CallContextActivities.__init__(..., *, prerender=None, dynamic: PreCallDynamicData | None = None)`; in
  `prefetch_call_context` after `_build` (context not None): `data = await dynamic.fetch(context)`,
  `context = data.context`, THEN the opener (`opener_for_call`), `store.save`, THEN `dynamic.record(data)`,
  then the pre-render. Order matters: variables before the opener render; events only after the context is
  saved (an activity retry caused by a failed save re-fetches but has recorded nothing yet). `PrefetchResult`
  gains `dynamic_data: int = 0` (number of pre-call fetches run). Update the `PREFETCH_TIMEOUT` docstring
  (6 s dynamic data budget + 8 s pre-render + the build < 20 s; value unchanged).
- `packages/va_workflows/pyproject.toml`: add `httpx>=0.28` and `va-tools` (+ `[tool.uv.sources]`), then
  `uv lock`/`uv sync`. Check `tests/test_model_imports`-style import-graph tests do not forbid it.

### 3. Worker wiring
- `apps/worker/src/va_worker/main.py`: build ONE `tools_http = tools_client(settings, resolver=system_resolver)`
  in `build_container` (closed on the stack) and pass it to `_text_deps` (which stops creating its own) and to
  `WorkerActivities.build(..., tools_http=tools_http, allow_http=insecure_urls_allowed(settings.va_env))`.
- `apps/worker/src/va_worker/activities.py`: `build(..., tools_http: httpx.AsyncClient | None = None,
  allow_http: bool = False)`; `dynamic = PreCallDynamicData(database=, redis=, http=tools_http, publisher=,
  clock=, allow_http=)` when `tools_http` is given; `CallContextActivities(..., dynamic=dynamic)` with a
  read-only `dynamic` property next to `prerender`.

### 4. `## Context` in the voice system prompt
- `packages/va_llm/src/va_llm/prompts/__init__.py`: `VoicePromptInputs.context_sections: Sequence[str] = ()`;
  `render_voice_system` appends, AFTER the formatted + rstripped template, `"\n\n## Context\n" + "\n".join(lines)`
  for the stripped non-blank lines (nothing when there are none). `voice_system.txt` stays verbatim (it is
  drift-checked against specs/10 §5). `fit_voice_system` never cuts the lines (say so in its docstring; they
  are counted in `over`).
- `packages/va_engine/src/va_engine/voice_inputs.py`: `voice_prompt_inputs` passes
  `context_sections=tuple(context.context_sections)` (TaskBrain needs no change).
- `packages/va_engine/src/va_engine/prompt.py`: `DialogueSettings.context_sections: tuple[str, ...] = ()`,
  filled by `for_call`; `PathwayPrompt.system` passes it to `VoicePromptInputs`.
- `packages/va_domain/src/va_domain/call_context.py`: docstring only (who fills the two fields now).

### 5. Specs (same commit)
- specs/13 §7: `> Decision (2026-11-05): dynamic data core and the outbound pre-call fetch (P08-T03a).`
  with: rules 1-7 and `fetch_all` above; the cache value/key/limits; unsigned requests carrying
  `User-Agent` + `X-VA-Call-Id` (the customer authenticates us with its own header, as the §7 example does;
  §5 step 4 signs tools only); pre-call numbers (5 s per request, no retries, 6 s phase budget inside the 20 s
  activity); concurrency + same scope; merge order; `calls.variables` gets the variables with the events;
  events after the context is saved and recording failures swallowed; a context rebuilt from the row by the
  media worker (Redis miss, specs/07 §3) carries no dynamic data; event data shape
  (`{url, method, status_code, duration_ms, variables_set, cached, outcome, timing, index}`, level `warning`
  for every outcome but `ok`); `every_turn` and inbound are P08-T03b / P08-T03c.
- specs/10 §5: Decision: the `## Context` block is appended after the rendered template (not a placeholder),
  one line per section, never cut by the §6 fit.
- specs/01 §2.1 step 2 Decision P05-T06a: replace "`variables`/`context_sections` (pre-call dynamic data,
  phase 08) ... stay empty until their producers exist" by a pointer to the specs/13 §7 Decision (memory stays
  as written). specs/19 §2 `call.dynamic_data` row: add the data keys. Keep `§N` refs on one line with their
  spec (`check_spec_refs.py`).
- AGENTS.md: one dated bullet (module names, the FrozenClock/`within` trap for the activity tests, test helpers).

### 6. Tests (names + what they assert)
`packages/va_tools/tests/test_dynamic_data.py` (respx via `tools_helpers.plain_executor`, fakeredis, a clock
that advances like the runner tests use; pure parts without HTTP):
- `test_a_templated_request_renders_url_query_headers_and_body`: `{{to}}`, `{{request_data.crm_token}}`,
  `query`, POST body with a lone expression keeping its type; asserts the received request.
- `test_response_data_sets_variables_and_renders_context_after_extraction` (`{{customer_name}}` in the context).
- `test_a_null_or_missing_value_sets_no_variable_and_no_context`.
- `test_a_cached_fetch_sends_no_request_and_reports_cached` (second fetch: 1 request total, `cached=True`,
  same variables); `test_the_cache_entry_expires_with_cache_ttl_seconds` (`redis_ttl.assert_expires_in`);
  `test_cache_false_or_ttl_zero_never_touches_redis`; `test_the_cache_key_is_per_org_method_url_and_body`
  (parametrized over what differs; headers do not change it); `test_a_failed_fetch_is_not_cached`;
  `test_an_oversized_or_truncated_body_is_not_cached`; `test_an_unreadable_cache_value_is_a_miss`;
  `test_a_redis_outage_fetches_without_the_cache` (`server.connected = False`).
- `test_failures_set_nothing_and_are_warnings` parametrized: 500 → `http_error`, 404, timeout, connection,
  blocked scheme, invalid url; `test_a_template_that_does_not_parse_is_a_render_error`;
  `test_a_port_that_raises_is_an_error_outcome` (a body with `2**70` through the real executor, plus a fake
  port raising `RuntimeError`); `test_a_context_template_that_does_not_parse_gives_no_line`;
  `test_a_value_over_the_limit_is_not_set`; `test_a_long_context_line_is_cut`.
- `test_event_data_has_no_query_and_no_header_values` (secret in the query + Authorization header: neither in
  `event_data()`/`message()` nor in the captured logs); `test_messages_and_levels`.
- `test_fetch_all_runs_concurrently_in_one_scope_and_keeps_spec_order` (gate both responses; both requests
  arrive before either answers); `test_fetch_all_reports_what_missed_the_budget_as_timeout` (`ManualClock`:
  one held request, `wait_for_sleep(budget)`, `run_for`; the finished one keeps its result, the held one is
  cancelled → `timeout`); `test_merge_results_later_fetch_wins_names_and_lines`.
`packages/va_workflows/tests/test_prefetch_dynamic_data.py` (database fixture, fakeredis, `SystemClock` —
event rows need real time and `within` needs a real sleep; respx; `ActivityEnvironment`):
- `test_pre_call_data_lands_in_the_context_the_opener_and_calls_variables`: config with `dynamic_data` +
  `first_sentence: "Hi {{customer_name}}"`; stored context has variables + context_sections, `opener_text`
  uses the variable (needs a prerender: reuse the `sim_rest` rig of `test_prefetch_activity.py`, or assert
  through `render_opener` if the rig is heavy), `calls.variables` row, one `call.dynamic_data` row
  (level info, data keys), `PrefetchResult.dynamic_data == 1`.
- `test_a_failed_fetch_stores_the_context_and_a_warning_event` (500): activity result stored, no variables,
  event level `warning`, `calls.variables` untouched.
- `test_every_turn_specs_are_not_fetched_before_the_call`; `test_no_dynamic_data_changes_nothing` (no events,
  no Redis `dyn:*` key, result `dynamic_data == 0`).
- `test_a_second_call_uses_the_cache` (`cache: true`: two calls of one org, 1 request, second event `cached`).
- `test_recording_failures_never_fail_the_activity` (publisher over a broken Redis → context stored, log
  `call_context.dynamic_data_record_failed`).
- `test_events_are_recorded_after_the_context_is_saved` (a store whose `save` raises once: the activity raises,
  no event row; the retry stores and records once).
- `test_the_webhook_outbox_gets_the_event_for_a_call_that_asked_for_it` only if cheap (`webhook_events:
  ["dynamic_data"]` + `webhook`), else rely on the router's own tests.
`packages/va_llm/tests/test_voice_prompt.py`: context block appended after the glossary, blank lines dropped,
absent when empty, braces literal; `test_fit_voice_system.py`: sections survive the fit.
`packages/va_engine/tests`: `test_voice_inputs`-level test that `voice_prompt_inputs` carries the sections; a
TaskBrain test (`task_helpers`, `FakeSource` gateway) whose first request's system prompt ends with the
`## Context` block and lists the variable under "Known information"; a `PathwayPrompt` test through
`dialogue_rig(context_sections=(...))` + `DialogueSettings.for_call`.
`apps/worker/tests/test_container.py`: `container.activities.context.dynamic` is wired (and shares the
container's tools client: one `tools_client` call — monkeypatch-count it or assert identity through TextDeps).
Existing tests that compare whole `PrefetchResult`s gain nothing (default 0).

### 7. Gates (all, in this order; `make test` and `make e2e` one after the other, ~15 min + ~7 min)
`make fmt lint typecheck placeholders`; `uv run python scripts/check_spec_refs.py`; `make test` (background +
heartbeat Monitor, re-arm at its 10 min expiry); `npx --yes jscpd@4 --silent`; `uvx radon cc -s -a apps
packages scripts`; pip-audit on `uv export --frozen --all-packages --no-hashes --no-emit-workspace`;
`corepack pnpm audit --audit-level high`; `uv run python scripts/check_openapi_drift.py` (no route change
expected); `make e2e` (the outbound prefetch path and the worker container wiring change; PATH needs
`/home/ralph/.local/bin`).

### 8. Risks
- jscpd: the new activity test file shares fixtures with `test_prefetch_activity.py`; move shared pieces into
  `packages/va_workflows/tests/support/` instead of copying.
- `uv lock` after the pyproject change; the Docker images install from the lock (`make e2e` rebuilds).
- respx + `SystemClock`: a held response for the budget test must use `ManualClock` in the va_tools test, not
  the activity test (keep the activity tests free of budget waits).
- `CallContext` is frozen: use `model_copy(update=...)`; the stored JSON must round-trip (`by_alias`).
- The cache holds customer data in Redis for up to 24 h: the key is org-scoped and the value is never logged.
- Do not touch `HttpExecutor`/`ToolRunner` big-int behaviour here (runtime task `fix:http-body-big-int`).

## Build P08-T03a (Builder, 2026-11-05T21:10Z-22:20Z)

Implemented the plan as written; deviations and things the Reviewer should know:

- `packages/va_tools/src/va_tools/dynamic_data.py` (new): `DynamicDataFetcher.fetch/fetch_all`,
  `DynamicDataResult`, `merge_results`, `cache_key`, limits. Cache JSON goes through the stdlib `json` (no
  64-bit limit); an unreadable cached value (also a too-deep one, `RecursionError`) is a miss. A failed cache
  WRITE (Redis or serialization) is logged `tools.dynamic_data_cache_write_failed` and never loses the fetched
  data. The timed-out result of `fetch_all` re-renders the URL (redacted) for the event; `""` if it cannot.
  va-tools gained the `redis` dependency.
- `packages/va_workflows/src/va_workflows/activities/dynamic_data.py` (new): `PreCallDynamicData.fetch/record`,
  `PreCallData`. `call_context.py`: fetch → opener → save → record → pre-render; `PrefetchResult.dynamic_data`.
  va-workflows gained `httpx` + `va-tools` (uv.lock updated).
- Worker: ONE `tools_client` in `build_container`, shared by `_text_deps` (now takes `tools_http`, makes its own
  `SystemClock` to stay at 5 parameters) and `WorkerActivities.build(tools_http=, allow_http=)`.
  `test_container_closes_what_it_opened` still counts 4 http clients.
- Prompt: `VoicePromptInputs.context_sections`, `CONTEXT_HEADER`, block appended after the stripped template;
  `voice_prompt_inputs` and `DialogueSettings.for_call`/`PathwayPrompt.system` pass the lines.
- Plan fact that was wrong: `build_call_context` does NOT copy `calls.variables` into the context, so a
  context's variables at prefetch are empty and the result holds only the dynamic ones; `calls.variables` keeps
  its own keys through `merge_variables` (tests assert both). Recorded in AGENTS.md.
- Added to the spec Decision beyond the plan: events are at-least-once when an attempt fails AFTER recording
  (activity timeout during the pre-render).
- Tests: `packages/va_tools/tests/test_dynamic_data.py` (43), `packages/va_workflows/tests/
  test_prefetch_dynamic_data.py` (10, incl. the webhook outbox), shared `tests/support/prefetch_rig.py`
  (`test_prefetch_activity.py` now uses it), va_llm prompt/fit tests, va_engine `test_voice_inputs.py` (TaskBrain
  over `FakeSource`) and `test_prompt.py`, worker `test_container.py`.
- Not covered by a test: `dynamic_data.py` lines 194 (a cached value that is neither text nor bytes), the
  337->328 branch (a context that renders to nothing after the cut) and 374-375 (a timed-out fetch whose URL
  does not render); module coverage 98 %.
- I stopped my first `make test` run myself ~4 min in (jscpd had found a clone inside the new va_tools test
  file; fixed with the `_fetching_all` helper) and restarted it; the background task of the first run shows
  exit 144 for that reason.

Gates (real output):
- `make fmt lint typecheck placeholders`: exit 0 (`mypy` 1622 files clean, placeholders 0 findings).
- `check_spec_refs`: 0 problems. `check_openapi_drift`: both files current.
- `make test`: `13191 passed, 7 skipped in 844.17s`, coverage 99.08 % (gate 85 %).
- jscpd: 0 clones. radon: average complexity A (2.94). pip-audit: no known vulnerabilities. pnpm audit
  (`--audit-level high`): exit 0 (1 moderate, pre-existing).
- `make e2e`: see the line below (appended when it finished).
- `make e2e` (rebuilt images, after `make test`): `20 passed, 107 deselected in 419.44s`, exit 0; the rebuilt
  worker container logged 67 `call_context.stored` lines during the run (the new prefetch path, no dynamic data
  in the e2e scenarios yet).

## Review P08-T03a (Reviewer, 2026-11-05T22:30Z-) — REJECTED, 2 findings (one root cause)

Gates I re-ran on the work tree: `make lint typecheck placeholders` exit 0; `check_spec_refs` 0 problems;
`check_openapi_drift` both current; jscpd 0 clones; radon average A (2.94); `make test`: see the line at the
end of this section. `make e2e` NOT re-run by me (rejected before it; the next review runs it).

Read: the whole diff + the 5 new files against specs/13 §7 (+ Decision), specs/10 §5, specs/19 §2, specs/01
§2.1. Request rendering, cache key/value/limits, unsigned requests, merge order, event data/message/level,
ordering fetch → opener → save → record → pre-render, worker wiring (one guarded client, SSRF guard kept),
prompt block in both runners: all match the Decision, and the tests assert behaviour. Inbox: nothing open.

The findings are values a 2xx answer may legally carry that pass rule 4 (≤ 16 384 bytes) but that the stores
behind the fetch cannot hold. Probes (git-ignored, re-runnable):
`uv run pytest logs/probe_review_t03a_db_test.py -n 0 --no-cov -p no:cacheprovider -q -s` (real activity +
Postgres) and `logs/probe_review_t03a_test.py` (fetch → `CallContextStore.save`).

1. **A deeply nested value fails `prefetch_call_context` on every attempt.**
   `packages/va_tools/src/va_tools/dynamic_data.py:309-321` (`_variables`) sets any non-null value up to 16 KB.
   A value nested deeper than 254 levels (`{"v": [[[…300…]]]}` is 600 bytes; orjson parses up to 1 024 levels)
   is set, goes into `CallContext.variables`, and `CallContextStore.save` → `model_dump_json` raises
   `PydanticSerializationError: Circular reference detected (depth exceeded)`
   (`packages/va_workflows/src/va_workflows/activities/call_context.py:95`). Probe output: `OUTCOME RAISED
   PydanticSerializationError`, no stored context, no event rows, `calls.variables` empty. The same answer comes
   back on each of the 3 attempts (from the `dyn:` cache with `cache: true`), so the prefetch fails for good:
   the call dials without a context (no opener pre-render), the variables of the HEALTHY fetches of the call are
   lost too, and no `call.dynamic_data` event is written. specs/13 §7 requires "Failures do not fail the call;
   event level `warning`", and the module docstrings promise "`fetch` … never raises" / "A failed fetch never
   fails the activity".
2. **A string holding U+0000 loses the events and `calls.variables` of every fetch of the call.**
   `{"v": "a\u0000b"}` is set and saved to Redis, then `PreCallDynamicData._record`
   (`packages/va_workflows/src/va_workflows/activities/dynamic_data.py:130-133`) writes events and variables in
   ONE transaction and Postgres refuses the JSONB (`unsupported Unicode escape sequence`). Probe output: context
   has `['blob', 'plan']`, `EVENT ROWS []`, `calls.variables []`, log `call_context.dynamic_data_record_failed`
   `DBAPIError`. So one bad value of one fetch silently drops the `call.dynamic_data` events of all fetches and
   the healthy fetch's variable never reaches `calls.variables` (for a task call without tools nothing else
   writes it — the reason the Decision gives for the merge), while the running call does use it.

Required fix (one place): `DynamicDataFetcher._variables` sets only values every store holds, like the size
rule next to it: skip (log by name, no value) a value nested deeper than a small documented limit — keep a wide
margin below 254, the consumers wrap variables in further levels (pydantic context/engine state, orjson event
and state dumps) — and a value with U+0000 in any string or object key. The fetch stays `ok` with the other
variables; no context line for a skipped one. Record both rules in the specs/13 §7 Decision (rule 4) and the
module docstring. Tests: va_tools (deep value / NUL in a string / NUL in a nested key are not set, the sibling
variable is, log by name; the same from a cache hit) and one activity test in `test_prefetch_dynamic_data.py`
(a call with a healthy fetch and a fetch returning such a value: context stored, the healthy variable in the
context AND `calls.variables`, two `call.dynamic_data` rows). Do not widen `record`'s transaction handling
instead: with the values filtered at the source it is correct as written.

Not findings (checked, fine): big integers in answers arrive as floats (orjson), `1e400`/`NaN`/lone surrogates
make the body text (nothing set); cache hit and miss extract identically; a cancelled budget leaves no task
behind; headers never reach the cache key, events or logs.
`make test` (my run, work tree as built): `13191 passed, 7 skipped in 840.59s`, coverage 99.08 %, exit 0 — the
suite is green; the rejection is for the two probe-proven cases above, which no test covers.

## Build P08-T03a, second pass (Builder, 2026-11-05T21:55Z-): the 2 review findings (one root cause)

Fixed at the source, as the Reviewer required; `PreCallDynamicData.record` and `CallContextStore.save` are untouched.

- `packages/va_tools/src/va_tools/dynamic_data.py`: `MAX_VARIABLE_DEPTH = 32` (exported) and `_unstorable(value)`
  (`"too_deep"` | `"nul_character"` | `None`), an iterative walk (no recursion: an answer may nest as deep as orjson
  parses, 1 023 levels). `_variables` calls it BEFORE the size rule (so `_compact` never sees a deep value), logs
  `tools.dynamic_data_value_unstorable` with `index`/`name`/`reason` (never the value) and skips the variable. Depth:
  a scalar is 0, `[1]` is 1; a value nested 32 deep is set, 33 is not. U+0000 is refused in any string and any
  object key at any level. The fetch stays `ok` with its other variables; a skipped variable gets no context line
  (`_sections` only renders set variables). `_store` also swallows `RecursionError` (a cache write of a very deep
  body must not lose the fetched data; stdlib `json.dumps` managed 1 000 levels in my probe, so this is a guard, not
  an observed failure).
- specs/13 §7 Decision P08-T03a rule 4 rewritten with both rules, the depth definition, why (context JSON / engine
  state / event dumps stop at ~254 and add levels of their own; `jsonb` refuses U+0000), "fetched and cached alike".
  Module docstring rule 4 says the same. AGENTS.md P08-T03a bullet gained one clause.
- Tests (all 8 new cases FAIL with `reason = _unstorable(value)` replaced by `None`, verified, then restored):
  - `packages/va_tools/tests/test_dynamic_data.py`:
    `test_a_value_the_stores_cannot_hold_is_not_set_fetched_or_cached` (5 cases: 33 nested arrays, 33 nested
    objects, NUL in a string, NUL in a nested string, NUL in a nested key; fetch + cache hit: 1 request, both `ok`,
    sibling `plan` set with its context line, `variables_set == ["plan"]`, two warning log lines `("blob", reason)`,
    the value absent from the log stream), `test_a_value_nested_to_the_limit_is_set` (32 deep is set, with its
    line), `test_an_answer_nested_as_deep_as_the_parser_allows_keeps_its_other_variables` (1 000 levels, cache on).
  - `packages/va_workflows/tests/test_prefetch_dynamic_data.py`:
    `test_a_value_the_stores_cannot_hold_costs_only_that_variable` (300-deep / NUL; a healthy fetch + a fetch whose
    answer holds the bad value AND a good sibling): context stored with the three good variables and the healthy
    context lines, `calls.variables` equal, two `call.dynamic_data` rows, both `info`/`ok`.
- Reviewer probes re-run (`logs/probe_review_t03a_db_test.py`, `logs/probe_review_t03a_test.py`, 9 passed): both
  cases now `OUTCOME returned`, `STORED CONTEXT ['plan']`, two event rows (`1 variable set`, `0 variables set`),
  `calls.variables ['plan']`, log `tools.dynamic_data_value_unstorable`.
- Not done on purpose: no rule for non-finite floats or integers beyond 64 bits. Neither can come out of a fetch
  (orjson refuses NaN/Infinity and reads big integers as floats) and the cache holds only what a fetch produced;
  the Reviewer listed both as checked non-findings.

Gates (real output, this pass):
- `make fmt lint typecheck placeholders`: exit 0 (placeholders `0 finding(s)`).
- `check_spec_refs`: 0 problems. `check_openapi_drift`: both files current.
- jscpd: 0 clones (1904 files). radon: average A (2.94). pip-audit (CI export): no known vulnerabilities.
  `pnpm audit --audit-level high`: exit 0 (1 moderate, pre-existing).
- `make test` (work tree with the fix): `13200 passed, 7 skipped in 876.31s`, coverage 99.08 % (gate 85 %), exit 0.
  After that run I made ONE test-only edit (`test_a_value_nested_to_the_limit_is_set` now nests an object with a
  number/bool/null/string at the limit, which covers the scalar branch of `_unstorable`); re-ran `make fmt lint
  typecheck placeholders` (exit 0), jscpd (0 clones) and `packages/va_tools/tests/test_dynamic_data.py` (50 passed;
  `dynamic_data.py` 98 %, uncovered: 227 = a cached value that is neither text nor bytes, 374->365 = a context that
  renders to nothing after the cut, 411-412 = a timed-out fetch whose URL does not render; same three as before).
- `make e2e` (rebuilt images, after `make test`): `20 passed, 107 deselected in 419.55s`, exit 0.
- Left open (not part of this task): runtime task `fix:http-body-big-int`.

## Review P08-T03a, second pass (Reviewer, 2026-11-05T22:27Z-) — verdict at the end of this section

Inbox: every note in `.ralph/review-inbox/` is listed in DONE; nothing about T03a.

Gates I re-ran on the work tree (real output):
- `make lint typecheck placeholders`: exit 0 (placeholders `0 finding(s)`). `check_spec_refs`: 0 problems.
  `check_openapi_drift`: both current. jscpd: 0 clones (1904 files). radon: average A (2.94). pip-audit (CI
  export): no known vulnerabilities. `pnpm audit --audit-level high`: exit 0 (1 moderate, pre-existing).
- `make test`: `13200 passed, 7 skipped in 895.70s`, coverage 99.08 %, exit 0.
- `make e2e`: see the last line of this section.

The two first-pass findings (one root cause) are fixed at the source, as required:
- `va_tools.dynamic_data._unstorable` (iterative, no recursion) + the call in `_variables` before the size rule;
  depth semantics match the spec text (scalar 0, `[1]` 1; 32 set, 33 not); U+0000 refused in strings and keys
  at any level; log by name/reason only. specs/13 §7 Decision rule 4, the module docstring and AGENTS.md agree.
- First-pass probes re-run (`logs/probe_review_t03a_db_test.py`, `logs/probe_review_t03a_test.py`, 9 passed):
  both cases `OUTCOME returned`, context `['plan']`, two event rows, `calls.variables ['plan']`.
- The new tests guard the rule: with `_unstorable` switched off by a pytest plugin
  (`PYTHONPATH=logs/probe_plugin ... -p neuter_unstorable`) 6 va_tools tests and both activity cases fail
  (the activity ones with `call_context.dynamic_data_record_failed DBAPIError`); with it on, 50 + 12 pass.
- New probe `logs/probe_review2_t03a_db_test.py` (real activity + Postgres, 7 unusual legal answers: u64 max,
  an integer beyond u64, a 32-deep value, odd keys incl. empty/`{{y}}`/U+2028/emoji, odd text incl. `{{call_id}}`
  and control characters, 1e308/-0.0/1e-320, 1 200 objects): every case stores the context, writes both
  events and merges `calls.variables`. Its 2 "failures" are my own strict `==` between the context (floats) and
  the jsonb read-back (Postgres prints 3.4e38 / 1e308 as plain integers, -0.0 as 0.0): numerically equal, not a
  defect of this task.

Not a finding, filed instead (runtime task `fix:webhook-body-big-int`, P4): a float >= ~1.8e19 in ANY call
jsonb column reads back as a Python int beyond 64 bits, and `orjson.dumps` refuses those (`Deliverer.send`,
`va_events.model.encode`). Reachable today without dynamic data (Send Call `metadata: {"x": 1e20}`), so it is
pre-existing and belongs to the serializer, like `fix:http-body-big-int`. Verified: the read-back type and the
orjson TypeError. NOT verified: that such a value reaches a webhook payload and what the dispatcher then does.
- `make e2e` (rebuilt images, after `make test`): `20 passed, 107 deselected in 422.16s`, exit 0.

**APPROVED.** Task ticked in plan/phase-08.md; next for the Planner: P08-T03b (every_turn), then P08-T03c
(inbound budget). Open runtime tasks: `fix:http-body-big-int`, `fix:webhook-body-big-int` (both P4, same family).
