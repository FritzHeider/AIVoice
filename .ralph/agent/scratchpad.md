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

## Planner 2026-11-05T22:55Z: after P08-T03a (commit a7f35b8)

Inbox: every note in `.ralph/review-inbox/` is in DONE (checked with `comm`). Work tree clean. Next unticked,
unblocked task: **P08-T03b** (depends on P08-T03a, ticked). Runtime tasks `fix:http-body-big-int` and
`fix:webhook-body-big-int` (both P4, pre-existing serializer family) stay open; not part of this task.

## Plan P08-T03b (per-turn dynamic data, `timing: "every_turn"`)

Read: specs/13 §7 + Decision P08-T03a, `va_tools/dynamic_data.py`, `va_workflows/activities/dynamic_data.py`,
`va_engine/task_runner.py`, `engine.py` (`handle_user_turn`/`_converse`/`_pass`/`_prepare`), `speculative.py`,
`prompt.py`, `voice_inputs.py`, `pathway_brain.py`, media `runtime.py`/`call_tools.py`/`pathway_runtime.py`/
`turns.py`, `va_domain/call_context.py`. Design decision recorded as DEC-019 (confidence 70).

### Semantics (goes into specs/13 §7 as `> Decision (2026-11-05) P08-T03b`)

1. **First value.** The pre-call phase fetches EVERY `dynamic_data` entry, whatever its `timing` (amends the
   T03a sentence "the `timing: "pre_call"` specs"): the opener, the first prompt and the stale fallback need a
   value. Variables of all entries merge as in T03a. Lines: `context_sections` = merged lines of the `pre_call`
   entries only; the lines of each `every_turn` entry go to the new `CallContext.turn_sections[index]`.
2. **Refresh.** At the start of each user turn (speech or keypad; not the opener, not a `wait_for_greeting`
   greeting, not a no-input/wait timeout, not chained passes or tool rounds) every `every_turn` entry is fetched
   again, all concurrently in one scope (the call's current variables, `request_data`, built-ins), each request
   with `timeout_ms` 800, the whole refresh within 0.8 s of the call clock. Cache rules are T03a's (`cache:
   true` → usually a Redis hit; the media worker uses the same Redis as the worker, so entries are shared).
3. **Result.** A successful fetch: its variables are merged over the call's variables and its lines replace
   that fetch's earlier lines wholesale (a variable the answer no longer has keeps its value, rule 4 of T03a
   "null sets nothing", and loses its line). A failed or timed-out fetch changes nothing (stale value).
   Variables are never unset. `## Context` = the pre-call lines, then the turn lines in `dynamic_data` order;
   two entries of different timing setting one name both keep their line.
4. **Where the turn waits.** Pathway calls: the refresh runs concurrently with the turn's decision calls and
   speculation; the node is executed after both finished (so at most `max(decision, 0.8 s)`); the decision
   itself sees the previous values. Refreshed variables merge BEFORE the turn's extraction (extraction and tool
   variables of the turn win a shared name). A refresh that changed a variable value or a turn line discards
   the speculative reply (`prompt_changed`); an unchanged one keeps it. Task calls have no routing step: the
   turn's first request waits for the refresh (≤ 0.8 s; a cache hit costs one Redis GET).
5. **Cut.** A turn cut while the refresh runs (barge-in merge, superseded turn) cancels the fetches: nothing is
   merged, no event is written; the next turn refreshes again.
6. **Events/state.** One `call.dynamic_data` per fetch per turn from the media worker through the call's
   `TurnRecorder` (same message/data/level as T03a, `timing: "every_turn"`). Refreshed variables are in the
   engine state snapshot (`on_state`); a task call with `every_turn` entries gets the snapshot saver even
   without tools, so the end routine merges them into `calls.variables`. Turn lines are NOT in `EngineState`
   (no schema bump: a voice call never resumes from a snapshot).
7. Inbound calls have no pre-call fetch until P08-T03c; their `every_turn` entries get their first value at
   the first user turn. A context rebuilt from the `calls` row has no lines; same.

### 1. va_domain — `packages/va_domain/src/va_domain/call_context.py`
- `CallContext.turn_sections: dict[int, list[str]] = Field(default_factory=dict)` (after `context_sections`;
  docstring: lines of each `every_turn` entry by its `dynamic_data` index) and
  `def turn_lines(self) -> list[str]` (all lines, ascending index). `CALL_CONTEXT_VERSION` stays 1.
- Tests (`packages/va_domain/tests/test_call_context.py`): default empty; JSON round trip (`model_dump_json`
  → `model_validate_json`) keeps int keys; `turn_lines()` order is by index, not insertion.

### 2. va_engine — the port
New `packages/va_engine/src/va_engine/turn_data.py` (import-light: templating + pydantic only):
```python
class TurnData(Protocol):
    def sections(self) -> tuple[str, ...]: ...                                  # the turn lines now
    async def refresh(self, scope: TemplateScope) -> Mapping[str, JsonValue]: ...  # variables of the ok fetches
async def refresh_turn(port: TurnData | None, scope: TemplateScope) -> Mapping[str, JsonValue]
    # {} without a port; any Exception → log `engine.turn_data_failed` (error_type) → {}; CancelledError passes
def merge_refreshed(variables: dict[str, JsonValue], refreshed: Mapping[str, JsonValue]) -> bool
    # update in place; True when a name was new or its value differed
```
- `voice_inputs.voice_prompt_inputs(..., turn_sections: Sequence[str] | None = None)`:
  `context_sections=(*context.context_sections, *(context.turn_lines() if turn_sections is None else turn_sections))`.
- `task_runner.TaskBrain(..., turn_data: TurnData | None = None)`:
  - `on_user_turn`: after `_accept(turn)`: `refreshed = await refresh_turn(self._turn_data, self._scope())`;
    `if merge_refreshed(self._state.variables, refreshed) and self._on_state: self._on_state(self._state)`;
    then `_respond()`. State changes only after the await (a cancelled refresh leaves no trace). `start()`
    does not refresh.
  - `_request`: pass `turn_sections=None if self._turn_data is None else self._turn_data.sections()`.
- `prompt.py`: `DialogueSettings.turn_sections: tuple[str, ...] = ()` (`for_call`: `tuple(context.turn_lines())`);
  `PathwayPrompt(settings, *, counter, clock, turn_data: TurnData | None = None)`; `system()` uses
  `(*settings.context_sections, *(settings.turn_sections if port is None else port.sections()))`.
- `engine.py`: `EnginePorts.turn_data: TurnData | None = None`.
  - `handle_user_turn` → `_converse(run, latest=, history=, refresh=True)`; `_converse(..., refresh: bool =
    False)` calls `self._pass(run, latest, history, refresh=refresh and chained == 0)`; `start`/
    `on_wait_timeout`/the max_total_turns protection path never refresh.
  - `_pass(..., *, refresh: bool)`: `before = self._turn_sections()` (port's `sections()` or `()`) BEFORE the
    task group; inside the group `refreshed = group.create_task(refresh_turn(port, self._ctx(node).scope()))`
    when `refresh` and a port exists; AFTER the group (synchronously, before `_schedule_background`/`_apply`):
    `changed = merge_refreshed(self.state.variables, refreshed.result()) or self._turn_sections() != before`;
    `_resolve_speculation(stays=..., prompt_changed=suffix is not None or changed)`. Do NOT merge inside the
    task: `Speculation.start` takes its variable snapshot after an await, a merge in between would hide the
    change from `resolve`.
  - Keep `_pass` complexity down (radon): put the group in a small helper if needed.
- Module docstrings of `task_runner`, `engine` (turn flow), `prompt`, `voice_inputs` get one sentence each.

### 3. va_tools — `packages/va_tools/src/va_tools/turn_data.py` (new)
```python
TURN_TIMEOUT_MS: Final = 800
TURN_BUDGET_S: Final = 0.8
class DynamicDataEvents(Protocol):
    def dynamic_data(self, result: DynamicDataResult) -> None: ...
def turn_specs(dynamic_data: Sequence[DynamicDataSpec]) -> list[tuple[int, DynamicDataSpec]]   # every_turn only
class TurnDynamicData:      # satisfies va_engine.turn_data.TurnData
    def __init__(self, fetcher: DynamicDataFetcher, specs: Sequence[tuple[int, DynamicDataSpec]], *,
                 initial: Mapping[int, Sequence[str]] | None = None, events: DynamicDataEvents | None = None)
    def sections(self) -> tuple[str, ...]            # per spec in index order: its current lines
    async def refresh(self, scope: TemplateScope) -> dict[str, JsonValue]
```
`refresh`: `results = await fetcher.fetch_all(specs, scope, timeout_ms=TURN_TIMEOUT_MS, budget_s=TURN_BUDGET_S)`;
then, with no await in between, for each result in order: `events.dynamic_data(result)`; if not failed:
`self._lines[result.index] = tuple(result.sections.values())` and `variables.update(result.variables)`.
`initial` lines of indexes that are not `every_turn` specs are ignored. No change to `dynamic_data.py` beyond
what jscpd/typing needs (do not touch the big-int behaviour).

### 4. va_workflows — `activities/dynamic_data.py`
`PreCallDynamicData.fetch`: specs = all entries; `variables, _ = merge_results(results)`; `lines =
merge_results([r for r in results if r.timing == "pre_call"])[1]`; `turn_sections = {r.index:
list(r.sections.values()) for r in results if r.timing == "every_turn" and r.sections}`; `model_copy(update=
{variables, context_sections, turn_sections})`. `record` unchanged (events carry the spec's timing).

### 5. Media worker
- `apps/media_worker/src/va_media/turns.py`: `TurnRecorder.dynamic_data(self, result: DynamicDataResult)` →
  `_publish(EventType.CALL_DYNAMIC_DATA, result.message(), result.event_data(), level=result.level)`.
- New `apps/media_worker/src/va_media/call_dynamic_data.py`:
  ```python
  @dataclass(frozen=True, slots=True)
  class TurnDataSource:               # per process
      tools_http: httpx.AsyncClient; allow_http: bool; redis: Redis | None; clock: Clock
      def for_call(self, context: CallContext, events: DynamicDataEvents | None) -> TurnDynamicData | None
  ```
  `None` when the call has no `every_turn` entry; else `HttpExecutor(tools_http, clock=, allow_http=,
  call_id=str(context.call_id))` (unsigned, as T03a) → `DynamicDataFetcher(http, redis=, org_id=, clock=)` →
  `TurnDynamicData(fetcher, turn_specs(config.dynamic_data), initial=context.turn_sections, events=events)`.
- `runtime.py`: `CallRuntime(..., turn_data: TurnDataSource | None = None)`; `_task_brain`/`_pathway_brain`
  build `turn = None if source is None else source.for_call(context, recorder)`; TaskBrain gets `turn_data=`,
  and its `EngineStateSaver` is created when `executor is not None or turn is not None` (and Redis exists).
  `pathway_runtime.build_pathway_brain(..., turn_data: TurnData | None = None)` → `PathwayPrompt(...,
  turn_data=)` and `EnginePorts(turn_data=)`. `open_runtime`: `turn_data=TurnDataSource(tools_http=tools_http,
  allow_http=insecure_urls_allowed(settings.va_env), redis=container.redis, clock=clock)`. Module docstring
  list of `runtime.py` gets item 13. No bound method of an owner is handed to a part (GC rule, AGENTS
  P08-FIX-3): the prompt and the engine hold the `TurnDynamicData` object, which holds fetcher + recorder only.
- `tests/support/runtime_rig.sim_runtime(turn_data: TurnDataSource | None = None)`.

### 6. Specs / docs (same commit)
- specs/13 §7: Decision P08-T03b (the 7 rules above, constants, module names); in Decision P08-T03a mark the
  "`timing: "pre_call"` specs" sentence and "`every_turn` (P08-T03b)" pointer as amended by T03b rule 1.
- specs/01 §2.1 Decision (line ~105): "every `timing: "pre_call"` entry is fetched" → every entry;
  `turn_sections` named next to `variables`/`context_sections`.
- specs/10 §5 Decision P08-T03a addendum: block = `context_sections` then the turn lines.
- AGENTS.md: one dated P08-T03b bullet (port, modules, test fakes, the FrozenClock/ManualClock note for
  `TurnDynamicData`, saver rule). Run `check_spec_refs`.

### 7. Tests (names + what they assert)
- `packages/va_tools/tests/test_turn_data.py` (ManualClock, `tools_helpers.plain_executor()` + respx,
  fakeredis; reuse the `_fetching_all`-style helper pattern of `test_dynamic_data.py`, no copied setup: jscpd):
  `test_a_refresh_sets_the_variables_and_replaces_that_fetch_s_lines`,
  `test_a_fetch_over_the_budget_keeps_the_stale_lines_and_sets_nothing` (hanging respx side effect;
  `wait_for_sleep(0.8)` + `run_for(0.8)`; event `timeout`/`warning`; the request was cancelled),
  `test_a_fast_fetch_counts_when_another_runs_out_of_time`,
  `test_a_failed_refresh_keeps_the_earlier_lines` (500 → `http_error`),
  `test_a_cached_answer_is_used_without_a_request` (second refresh: no new request, event `cached`; key
  deleted → a request again),
  `test_a_variable_the_answer_no_longer_has_loses_its_line` (not in the returned variables),
  `test_one_event_per_fetch_in_dynamic_data_order`, `test_only_every_turn_entries_are_refreshed`
  (`turn_specs`, `initial` of a pre-call index ignored),
  `test_a_cancelled_refresh_changes_nothing_and_reports_nothing`.
- `packages/va_engine/tests/support/turn_data_fakes.py`: `FakeTurnData(script=[(variables, lines) | Exception],
  hold: asyncio.Event | None)` recording `scopes`, `started`, `cancelled`.
- `packages/va_engine/tests/test_turn_data.py`: `refresh_turn` (no port → {}; raising port → {} + log
  `engine.turn_data_failed`; cancellation passes), `merge_refreshed` (new / changed / equal).
- `packages/va_engine/tests/test_task_turn_data.py` (TaskBrain over `dialogue_helpers.FakeSource`):
  the turn's request carries the refreshed line under `## Context` and the variable in Known information;
  refresh happens before the request and once per turn (not per tool round, with `RecordingExecutor`);
  `start()` with and without a greeting does not refresh; without a port the lines are
  `context.turn_sections`; a raising port → the reply goes on with the earlier lines; `on_state` receives the
  refreshed variables; a turn cancelled while the port is held merges nothing and the next turn refreshes.
- `packages/va_engine/tests/test_engine_turn_data.py` (`engine_rig(turn_data=FakeTurnData(...))`, add the knob
  to `engine_helpers.engine_rig` → `EnginePorts`/`PathwayPrompt` via `dialogue_rig`):
  refresh starts while the router is still gated (`KeyedPort.gates`) and the node runs only after both;
  reply prompt has the refreshed line + variable, node `text` `{{var}}` renders the refreshed value;
  once per user turn (chained passes, `start()`, `on_wait_timeout`, max_total_turns protection: none);
  a changed refresh discards the speculation (`prompt_changed`: a second, non-speculative request), an
  unchanged one commits it; the turn's extraction wins a shared name; a raising port → the turn goes on.
- `test_prompt.py` (+2): turn lines follow the static ones; fallback to `settings.turn_sections`.
  `test_voice_inputs.py` (+1): `turn_sections` param and the context fallback.
- `packages/va_workflows/tests/test_prefetch_dynamic_data.py` (+2): an `every_turn` entry is fetched pre-call
  (variable in the context and the opener, lines under `turn_sections[index]`, not in `context_sections`,
  event `timing: every_turn`); mixed entries keep T03a behaviour for the `pre_call` ones.
- `apps/media_worker/tests/test_call_dynamic_data.py`: `for_call` → `None` without `every_turn`; initial lines
  from the context; a refresh over a respx/receiver endpoint publishes `call.dynamic_data` through a real
  `TurnRecorder` (warning for a failure). `test_pathway_runtime.py` (+1): `build_pathway_brain(gateway=
  FakeSource(), turn_data=fake)` → after a user turn `source.requests[-1]` system prompt has the line.
- `apps/media_worker/tests/test_runtime_dynamic_data_sim.py` (acceptance; `sim_backends` + `sim_runtime(
  turn_data=TurnDataSource(tools_http, allow_http=True, redis, SystemClock()))`, task call WITHOUT tools,
  `dynamic_data=[{url: receiver, timing: every_turn, response_data: [{name: plan, data: $.plan, context:
  "Their plan is {{plan}}."}]}]`, receiver scripted `gold` then `platinum`, a two-utterance scenario,
  `wait_for_greeting=False`): FakeLLM request of user turn 1 has "Their plan is gold.", of turn 2 "Their plan
  is platinum." and not gold; two `call.dynamic_data` rows (`every_turn`) in `call_events`; after the end
  routine `calls.variables["plan"] == "platinum"` (proves the saver for a tool-less task call).
- GC guard: if an existing runtime `gc_cycles` test covers a call's parts, extend it to a call with turn data
  (grep `unreachable_after` in `apps/media_worker/tests`).

### 8. Gates (all; `make test` then `make e2e`, background + heartbeat, ~15 min + ~7 min)
`make fmt lint typecheck placeholders`; `uv run python scripts/check_spec_refs.py`;
`uv run python scripts/check_openapi_drift.py`; `npx --yes jscpd@4 --silent`; `uvx radon cc -s -a`;
pip-audit on the CI export; `corepack pnpm audit --audit-level high`; `make test`; `make e2e` (the outbound
prefetch and the media runtime change). PATH needs `/home/ralph/.local/bin`.

### 9. Risks
- `_pass` must not merge inside the refresh task (speculation snapshot race, see §2); test both speculation
  outcomes.
- `fetch_all` is bounded by `within` on the clock: `FrozenClock`/`RecordingClock` time every refresh out at
  once → va_tools tests use `ManualClock`, runtime tests `SystemClock`; engine tests use the fake port.
- pydantic int keys of `turn_sections` through JSON (round-trip test); `extra="forbid"` contexts written by a
  NEW worker and read by an OLD media pod would fail, irrelevant here (one deploy), note it in the Decision.
- mypy: `TurnDynamicData` must satisfy the `TurnData` protocol structurally (return `Mapping`-compatible).
- jscpd: share the fake port and the request/assert helpers; no copied respx setup.
- Task-mode latency: only calls with `every_turn` entries wait, at most 0.8 s; documented in rule 4.
- Do not change `HttpExecutor`/`ToolRunner` big-int behaviour (`fix:http-body-big-int`).

## Build P08-T03b (Builder, 2026-11-05T23:00Z-)

Implemented the plan as written, with these additions/deviations (all in the diff, none changes the 7 rules):

1. **End routine (plan gap).** `va_media.call_end._keeps_engine_state` decided by `tool_ids`/`inline_tools`
   only, so a tool-less task call with `every_turn` entries wrote its snapshot but the end routine never
   read it. The runtime sim acceptance test caught it (`calls.variables` kept the old value). Now the
   predicate is also true for a task call with an `every_turn` entry; unit test
   `test_a_task_call_s_snapshot_is_merged_only_when_it_refreshes_per_turn_data` (every_turn / pre_call only /
   none).
2. `TurnDynamicData.refresh` updates lines + variables first, then reports the fetches; a reporter that
   raises is logged `tools.turn_data_event_failed` and costs the turn nothing (test).
3. `merge_refreshed` always assigns and reports "changed" by Python equality (same comparison as the
   speculation's own variable snapshot).
4. `PathwayEngine`: the task group moved to `_decide(node, inputs, refresh=)` (returns the decision and the
   refreshed variables, never merges); `_pass` merges after it, before `_apply`. `_converse` clears `refresh`
   after the first loop round (radon: `_converse` stays B 10).
5. Test support moved to shared modules for jscpd (0 clones): `tools_helpers.HeldAnswer`/`manual_fetcher`/`ORG`
   (test_dynamic_data uses them), `task_helpers.scripted_brain` (test_task_tool_speech uses it),
   `recorder_helpers` (media; test_turns imports it), `runtime_rig.run_human_call` (tools sim uses it).
6. `test_every_turn_specs_are_not_fetched_before_the_call` (T03a) is replaced by
   `test_an_every_turn_entry_gets_its_first_value_before_the_call` + `test_mixed_entries_keep_their_lines_apart`
   (rule 1 amends T03a).

Files (src): `va_domain/call_context.py` (`turn_sections`, `turn_lines()`), `va_engine/turn_data.py` (new),
`va_engine/{task_runner,engine,prompt,voice_inputs}.py`, `va_tools/turn_data.py` (new),
`va_workflows/activities/dynamic_data.py`, `va_media/{call_dynamic_data.py (new),runtime.py,pathway_runtime.py,
turns.py,call_end.py}`. Specs: 13 §7 Decision P08-T03b (+ T03a pointer), 01 §2.1, 10 §5 addendum. AGENTS.md
bullet.

Tests (new files): `va_tools/tests/test_turn_data.py` (11), `va_engine/tests/test_turn_data.py` (9),
`test_task_turn_data.py` (7), `test_engine_turn_data.py` (9), `media_worker/tests/test_call_dynamic_data.py`
(2), `test_runtime_dynamic_data_sim.py` (2: refreshed prompt of turn 2 + events + snapshot + `calls.variables`
+ GC guard over TaskBrain/TurnRecorder/TurnDynamicData/DynamicDataFetcher/HttpExecutor/EngineStateSaver;
failed refresh keeps the earlier context). Extended: test_call_context, test_prompt, test_voice_inputs,
test_prefetch_dynamic_data, test_pathway_runtime, test_call_end.

### Gates P08-T03b (real output, final tree; 2026-11-06T00:35Z)

- `make fmt` clean; `make lint` exit 0; `make typecheck` exit 0 (mypy: 1633 source files); `make placeholders`
  0 findings; `check_spec_refs` 0 problems; `check_openapi_drift` current; jscpd 0 clones; radon average
  2.94 (`PathwayEngine._converse` B 10 as on HEAD, `_pass` A 4, `_decide` A 4); pip-audit (CI export) no known
  vulnerabilities; `pnpm audit --audit-level high` exit 0 (1 moderate, below the level).
- `make test` (final tree, run after the last source change): **13249 passed, 7 skipped**, coverage 99.08 %
  (new modules `va_engine/turn_data.py`, `va_tools/turn_data.py`, `va_media/call_dynamic_data.py`,
  `va_workflows/activities/dynamic_data.py` 100 %). An earlier full run on the tree before the 3-line
  `TaskBrain._refresh` early return gave the same numbers.
- `make e2e`: **first run 1 failed / 19 passed** — `test_concurrent_calls_on_one_media_worker`,
  `assert 10.0 == 0` platform underruns (overhead p50/p95 107/150 ms PASS, lag p99 <= 25 ms). The media log
  attributes all 10 to ONE event-loop stall at 23:36:53 while the 50 openers played (`turn=0`,
  `wake_late_ms` 94-109, `prev_send_await_ms` ~0, no `gc_pause` at that instant); the run started ~8 min
  after `make test` and right after the image rebuild (15-min load 4.5). Three reruns of the capacity flow
  on that stack (`pytest tests_e2e -m capacity`): 3 x passed. Looking for a contribution of this task I
  found that `TaskBrain._refresh` built a template scope (`call_builtins`) at every user turn even without a
  port; it now returns at once without one (it runs at user turns, not at the opener where the stall was, so
  it does not explain the stall, but it was avoidable work in the hot path). **Second full `make e2e` on the
  final tree (cold, rebuilt stack, quiet host): 20 passed, 107 deselected, 418.72 s, exit 0** (capacity flow
  passed). For the Reviewer: the first failure is reported as seen; if the capacity flow fails again on a
  quiet host, read `outbound.underrun` / `gc_pause` in the media log before blaming this diff (no code of
  this task runs for a call without `every_turn` entries beyond `turn_specs()` at brain build and one
  `is None` check per user turn).

## Review P08-T03b (Reviewer, 2026-11-06T00:10Z-00:35Z) — REJECTED, 2 findings

Gates re-run on the tree as handed over: `make lint` exit 0, `make typecheck` exit 0, `make placeholders`
exit 0, `check_spec_refs` exit 0, `make test` **13249 passed, 7 skipped**, coverage 99.08 %, exit 0
(`logs/make_test_review_t03b.log`). Not re-run because the task goes back anyway: `make e2e`, jscpd, radon,
audits (re-run them on the next pass).

The gates are green; the rejection is about behaviour. A maintainer note about THIS task landed in the inbox
at 23:50Z, while the build was running, and is not in `DONE`: `.ralph/review-inbox/2026-11-05-p08-t03b-wip-review.md`
(one blocker, one non-blocking item). The Builder never saw it (nothing in the build notes). I reproduced
both on the final tree with `logs/probe_review_t03b_test.py` (git-ignored; run
`uv run pytest logs/probe_review_t03b_test.py -n 0 --no-cov -p no:cacheprovider -rA`): 3 failed.
Read the note first; it has the full reasoning. Do not edit or delete it.

1. **BLOCKER — a pathway turn cut after its refresh finished keeps the new lines and the events but drops
   the variables** (specs/13 §7 Decision P08-T03b items 3 and 5).
   - `packages/va_tools/src/va_tools/turn_data.py:79-90`: `TurnDynamicData.refresh` replaces `_lines` and
     reports the events itself, before it returns.
   - `packages/va_engine/src/va_engine/engine.py:458-479`: `_decide` runs the refresh as a TaskGroup child
     beside the decision; the variables are merged only at `engine.py:445`, after `_decide` returned.
   - A reply cancelled (superseded turn, barge-in merge) while the decision call is still running and the
     refresh already completed raises out of `_decide`: `refreshed.result()` is never read, nothing merges.
     Same when `_decide` fails for any other reason after the refresh completed.
   - Probe output (real `TurnDynamicData` + real `PathwayEngine`, respx answer at once, `select_path`
     gated, turn cancelled): `{'variables': {'plan': 'gold'}, 'lines': ('Their plan is platinum.',),
     'events': [('ok', ['plan'])]}`. The event log says `plan` was set, the prompt's `## Context` says
     platinum, `state.variables` (prompt variables block, `{{plan}}` in node text, the snapshot,
     `calls.variables`) says gold, until a later refresh succeeds. This is the common cut: a cache hit or a
     fast endpoint finishes in ms, the decision takes hundreds.
   - The existing test `test_a_turn_cut_during_the_refresh_merges_nothing_and_the_next_turn_refreshes`
     (`packages/va_engine/tests/test_engine_turn_data.py:201`) only cuts while the fake refresh is held, so
     it cannot see this. TaskBrain is not affected (`task_runner.py:259-260`: no await between the refresh
     returning and the merge).
   - Required fix (the note's preferred one): lines, events and variables are ONE commit.
     `TurnData.refresh` changes nothing and returns a pending result (the variables plus an `apply()` that
     replaces the lines and reports the events); the callers apply it in the same synchronous step as
     `merge_refreshed` (`engine.py:445`, `TaskBrain._refresh`). A cut then leaves no trace at all, which is
     what item 5 says. Update the `TurnData` protocol docstrings, `FakeTurnData`, `refresh_turn`, the module
     docstrings and the AGENTS.md bullet accordingly; item 5 may stay as written. (The note's smaller
     alternative, merging a completed refresh task before re-raising and rewording item 5 to "cut while its
     refresh runs", is acceptable only if the preferred shape turns out not to work; say why.)
   - Required tests: (a) engine: the refresh finishes at once, the decision is gated
     (`KeyedPort(gates={"select_path": gate})`), cancel the turn → variables, `sections()` and reported
     events all unchanged, and the next turn refreshes and merges; (b) the same invariant when the decision
     task fails instead of being cancelled, if a failing decide is reachable with the test ports (else say
     so); (c) va_tools: `refresh` alone changes no line and reports nothing until `apply()`; `apply()`
     replaces lines and reports in `dynamic_data` order; a reporter that raises still costs nothing;
     (d) TaskBrain: lines/events/variables change together. Adapt the existing tests that read
     `data.sections()` right after `refresh`.

2. **A 2xx answer with nothing usable erases the entry's context lines** (note item 2, non-blocking there;
   since the task is open anyway, settle it now).
   - `turn_data.py:86-87` replaces an entry's lines for every result that is not `failed`; `ok` is any 2xx,
     including a non-JSON body, an empty body and a body cut at the size cap
     (`packages/va_tools/src/va_tools/http.py:203-210` extracts `None` for every name). Probe: a 200 HTML
     page and a 200 empty body both give outcome `ok`, variables `{}`, `sections() == ()`, while the
     variable keeps its value. A proxy's 200 error page removes context a 500 would have kept.
   - Decide and write it into item 3 of the Decision: either (preferred, the note's rule) an `ok` result
     whose body was not parsed as JSON or was truncated leaves that entry's lines alone, like a failed
     fetch (the result needs to carry that fact: the live path knows `outcome.truncated` and whether `body`
     is JSON, the cache path whether the cached body is a string), or keep the current behaviour and state
     in item 3, with the reason, that a 2xx without usable JSON clears the entry's lines. Either way add a
     `packages/va_tools/tests/test_turn_data.py` case for a 200 non-JSON body and (if the rule changes) a
     truncated one. A JSON answer that simply no longer has the variable must keep losing its line
     (existing test `test_a_variable_the_answer_no_longer_has_loses_its_line`). Pre-call is unaffected.

Nothing else found (checked against specs/13 §7, specs/01 §2.1, specs/10 §5, specs/19 `call.dynamic_data`):
the `fetch_all`/`within` cancellation path, the TaskBrain path, the speculation handling (`changed` covers
variable and line), `CallContext.turn_sections` JSON round trip, saver + `_keeps_engine_state` pairing, the
guarded HTTP client / `allow_http` wiring of `TurnDataSource`, the recorder's publishing, the pre-call
amendment (every entry fetched, `every_turn` lines only in `turn_sections`), the GC guard in the runtime
sim test, the test quality of the new files (real assertions, ManualClock/events, no sleeps).

After the fix: re-run every gate including `make e2e` (the media runtime path changes again). On approval
the Reviewer appends `2026-11-05-p08-t03b-wip-review.md` to `.ralph/review-inbox/DONE`.

## Build P08-T03b, second pass (Builder, 2026-11-06T00:40Z-): the 2 review findings

Read the inbox note `2026-11-05-p08-t03b-wip-review.md` and the Review section first; both findings fixed
with the note's preferred shape. The note is untouched and NOT in `DONE` (Reviewer appends it on approval).

1. **Blocker (lines + events + variables are one commit).**
   - `va_engine.turn_data`: `TurnData.refresh(scope) -> TurnRefresh` (protocol: `variables`, `apply()`); a
     refresh changes nothing. `refresh_turn` returns `TurnRefresh | None`; new `commit_refresh(variables,
     refresh)` = `apply()` (lines + events) then `merge_refreshed`, one synchronous step; an `apply()` that
     raises is logged `engine.turn_data_failed` and merges nothing.
   - `va_tools.turn_data.TurnDynamicData.refresh` only fetches and returns a `FetchedTurn(variables, results,
     owner)`; `FetchedTurn.apply()` → `TurnDynamicData.commit(results)` replaces the lines and reports.
   - `PathwayEngine._decide` returns the pending refresh; `_pass` commits it right after `_decide` returned
     (no await in between), so a turn cancelled, or whose decision raised, after the refresh finished keeps
     nothing. `TaskBrain._refresh` commits right after its await.
   - Tests: engine `test_a_turn_cut_after_its_refresh_finished_keeps_nothing_of_it` (decision gated,
     `FakeTurnData.fetched` event, cancel → variables/lines/`applied` unchanged, next turn commits all three),
     `test_a_turn_whose_decision_fails_after_the_refresh_finished_keeps_nothing_of_it` (a failing decide IS
     reachable: `KeyedPort.script("select_path", RuntimeError)` leaves `_decide` as an ExceptionGroup);
     `test_turn_data.py` (engine): nothing changes until committed, a commit without a value change still
     applies the lines, an `apply()` that raises; TaskBrain tests assert `port.applied` beside the variables;
     va_tools: `test_a_refresh_changes_no_line_and_reports_nothing_until_it_is_applied`,
     `test_a_refresh_that_is_never_applied_leaves_no_trace_and_the_next_one_counts`, and the reviewer's probe
     as a committed test (real `TurnDynamicData` in a real `PathwayEngine`):
     `test_a_pathway_turn_cut_after_its_refresh_finished_keeps_lines_events_and_variables_old`; media
     `test_call_dynamic_data` asserts no event before the commit. `FakeTurnData` follows the new contract
     (`Fetched.apply`, `applied`, `fetched`).
   - Proof the tests guard it: with `refresh` committing itself again (old behaviour, temp edit, restored)
     9 tests failed (8 in va_tools `test_turn_data.py`, 1 in media `test_call_dynamic_data.py`).
   - The reviewer's probe `logs/probe_review_t03b_test.py` is written against the old API (it awaits an event
     reported during the refresh, and reads `refresh()`'s return as the variables); its three cases are the
     committed tests above.
2. **A 2xx without a usable answer keeps the entry's lines (the note's rule).** `DynamicDataResult.readable`
   = the answer was a whole JSON document (`_json_document(body, truncated=)`: not truncated, not text / a
   bare JSON string, not empty / `null`); fetched and cached answers alike. Only a readable answer has
   variables: a body cut at the size cap sets nothing even when the cut text parses (new, tiny amendment of
   P08-T03a rule 4, so "not readable ⇒ no variables, no lines" holds without exception; pre-call behaviour
   is otherwise unchanged, since a non-JSON body already extracted nothing). `TurnDynamicData` counts only
   readable results (variables and lines); an unreadable 2xx is still reported `ok` with `variables_set: []`.
   A JSON answer that no longer has the variable still loses its line (existing test kept). Tests:
   `test_turn_data.py` html / empty / JSON string / JSON null keep the lines, truncated keeps the lines and
   sets nothing; `test_dynamic_data.py` `readable` for no-JSON answers, whole documents (`{}`, `[]`, `0`,
   `false`), failures, the cut answer, and the cached text answer.

Specs: 13 §7 Decision P08-T03a rule 4 (readable answer, amended 2026-11-06), Decision P08-T03b item 3 (the
unreadable 2xx), item 5 rewritten (one commit; cut or failed decision before it leaves no trace; the `dyn:`
cache entry stays), Code paragraph. AGENTS.md bullet updated.

### Gates P08-T03b, second pass (real output, final tree; 2026-11-06T00:40Z-01:27Z)

- `make fmt` clean; `make lint` exit 0; `make typecheck` exit 0 (mypy: 1633 source files); `make placeholders`
  0 findings; `check_spec_refs` 0 problems; `check_openapi_drift` current; jscpd 0 clones (one clone between
  two new tests was found and removed: `_gold_refreshed`); radon average 2.94 (`DynamicDataFetcher._fetch`
  C 12 as on HEAD, `_pass` A 4, `_decide` A 4, `commit_refresh` A 3); pip-audit (CI export) no known
  vulnerabilities; `pnpm audit --audit-level high` exit 0 (1 moderate).
- `make test` (final tree, started after the last source change): **13272 passed, 7 skipped**, coverage
  99.08 %, exit 0 (`logs/make_test_t03b2.log`; `va_engine/turn_data.py`, `va_tools/turn_data.py`,
  `va_media/call_dynamic_data.py` 100 %). A first run was stopped by me (jscpd clone + `_fetch` at C 13
  found meanwhile, fixed, restarted): its `exit=2` in an earlier copy of the log was my kill, not a failure.
- `make e2e`, three full runs, reported as they happened:
  1. right after `make test`, apps rebuilt (`logs/make_e2e_t03b2.log`): **1 failed / 19 passed** —
     `test_concurrent_calls_on_one_media_worker`, `assert 1.0 == 0` platform underruns (overhead p50/p95
     127/185 ms PASS). Media log: ONE `outbound.underrun` at 01:00:40.97, `turn=0` (opener), `wake_late_ms`
     60.1, `prev_send_await_ms` 0.0, `queued_frames` 193, no `gc_pause` at that instant (nearest 01:00:40.02,
     18.7 ms) = one event-loop stall while the 50 openers played, the signature of the previous pass's first
     run. The capacity calls have no `dynamic_data`: the per-turn code does not run for them (one `is None`
     check per user turn; nothing at the opener). Three reruns of the capacity flow on that stack
     (`pytest tests_e2e -m capacity`): 3 x passed, 0 underruns, p50 126/108/109 ms.
  2. `make down && make e2e` (cold stack; `logs/make_e2e_t03b2_run2.log`): **1 failed / 19 passed** —
     `test_amd_ivr_mode` (the FIRST flow), `WaitTimeoutError: timed out after 30.0 s waiting for a sim call`.
     Worker log: from 01:10:23 (`worker.polling`) to ~01:10:51 Temporal answered every poll `Unavailable:
     "Not enough hosts to serve the request"`; the call's prefetch ran at 01:11:01.9 and it was dialed at
     01:11:02.07, after the test's 30 s wait. Temporal was not serving yet when the first flow started (the
     `temporal` container is `healthy` before its matching ring formed). The capacity flow PASSED in this run.
  3. plain `make e2e` on that stack, host quiet (`logs/make_e2e_t03b2_run3.log`): **20 passed, 107
     deselected, 420.18 s, exit 0**.
  For the Reviewer: neither failure touches code of this task (run 1: no dynamic data in those calls, stall
  at the opener; run 2: Temporal readiness before any call ran), but both are real gate failures on this
  runner and are recorded in task `fix:e2e-cold-start-flakes`. If the capacity flow fails again, read
  `outbound.underrun`/`gc_pause` in the media log first.

## Review P08-T03b, second pass (Reviewer, 2026-11-06T01:27Z-01:45Z) — REJECTED, 3 findings

Gates re-run on the tree as handed over: `make lint` exit 0, `make typecheck` exit 0, `make placeholders`
0 findings, `check_spec_refs` exit 0, `make test` **13272 passed, 7 skipped**, coverage 99.08 %, exit 0
(`logs/make_test_review_t03b2.log`). Not re-run because the task goes back anyway: `make e2e`, jscpd, radon,
audits (re-run all of them on the next pass).

Both findings of the first review are fixed and well tested: lines, events and variables are one synchronous
commit on both runners (`commit_refresh` after `_decide` returned / right after the await in
`TaskBrain._refresh`), the cut-after-refresh and failed-decision tests are real, `readable` keeps the lines
of an unreadable 2xx. Nothing to change there.

The rejection comes from THREE maintainer notes that landed in the inbox during the second build and are not
in `DONE` (the Builder saw only the first note): `.ralph/review-inbox/2026-11-06-p08-t03b-recheck.md`
(00:19Z), `2026-11-06-p08-t03b-third-pass.md` (00:51Z), `2026-11-06-p08-t03b-fourth-pass.md` (01:22Z, read
this one first: it restates what is still open; item 1 is "fix before merge"). Read them; do not edit or
delete them. I reproduced findings 1 and 3 on this tree with `logs/probe_review_t03b3_test.py`
(git-ignored): `uv run pytest logs/probe_review_t03b3_test.py -n 0 --no-cov -p no:cacheprovider -rA`
→ 2 failed.

1. **Must fix (fourth-pass item 1): an unreadable 2xx answer is cached, so one bad answer pins the stale
   state for the whole TTL.**
   - `packages/va_tools/src/va_tools/dynamic_data.py:342-343`: `if redis is not None and not outcome.truncated`
     stores every 2xx that was not cut, readable or not (a proxy's 200 HTML page, an empty 200/204, a bare
     JSON string). The hit path (:324-327) then serves it as `ok`, `cached`, not readable.
   - Probe (real `TurnDynamicData`, `cache: true`, endpoint answers 200 text once, then 200 JSON
     `{"plan":"platinum"}`): `route.call_count == 1` after three refreshes; results
     `('ok', False, False)`, `('ok', True, False)`, `('ok', True, False)`; the plan stays gold. The endpoint
     recovered at the second turn and is not asked again for `cache_ttl_seconds` (default 300, up to
     86 400), by any call of the org with the same rendered URL and body; every new call's pre-call fetch in
     that window sets no variable. Silent: the events read `ok`, `cached`, level `info`.
   - It contradicts what this diff itself decides (specs/13 §7 Decision P08-T03b item 3: such an answer
     "says nothing about the data" and must not do more harm than a 500; a 500 is never cached, rule 3).
     `test_a_cached_text_answer_is_as_unreadable_as_the_fetched_one`
     (`packages/va_tools/tests/test_dynamic_data.py:233`) pins the wrong behaviour.
   - Required: store only readable answers (`if redis is not None and readable:`; readable implies not
     truncated). On the hit path a stored answer that is not a JSON document is a MISS (fall through to the
     request, then overwrite/skip per the new rule), so entries written by a7f35b8 do not linger. Turn the
     test at :233 around (a text answer is not cached: the second fetch sends a request and, when the
     endpoint now answers JSON, sets the variables) and add: an empty 200/204 and a bare JSON string are not
     cached; a stored non-document value left by the old build is a miss (seed it with `redis.set`).
     Add a `test_turn_data.py` case of the probe (bad answer once, recovery at the next refresh with
     `cache=True`). Spec: Decision P08-T03a rules 2 and 3 ("only readable answers are stored; a stored value
     that is not a JSON document is a miss"). Mind the wording: rule 2 already uses "readable/unreadable
     value" for "the stored value parses as `{status, body}`"; keep the two meanings apart. Module
     docstring of `dynamic_data.py` (rule 3 text) too.

2. **Settle now (recheck item 3, repeated in the third and fourth pass): `turn_sections` is not additive
   for a reader one build behind.**
   - `packages/va_domain/src/va_domain/call_context.py:42,90`: `CallContext` is `extra="forbid"`;
     `CallContextStore.save` (`packages/va_db/src/va_db/call_context.py:69`) dumps every field, so EVERY
     context a new worker writes carries `"turn_sections": {}`. A media worker one build behind fails
     validation, `load` returns `None` and `DbCallContextLoader` rebuilds the context from the `calls` row:
     no pre-call variables, no context lines, no `opener_text`, only a `call_context.invalid` warning. A
     rolling update does not swap worker and media worker at the same instant, so Decision item 1's "the
     field is additive ... deployed together" (specs/13:629-630) does not hold as written.
   - Required: `extra="ignore"` on the top-level `CallContext` only (nested models and `SendCallConfig`
     keep `forbid`), a test that a context JSON with an unknown top-level key loads (the existing
     parametrized rejection case `extra-key` in `packages/va_domain/tests/test_call_context.py:116` changes
     accordingly; an unknown key inside a nested model must still be refused), and item 1 reworded to what
     holds (unknown top-level keys are ignored from this build on, so later fields are additive for readers
     of this build or newer; nothing older is deployed). If you find a reason `ignore` is wrong, say so in
     the spec and reword item 1 honestly instead; "additive" may not stay.

3. **Settle now (recheck item 4): two `every_turn` entries that set one name flip to the earlier entry's
   value when the later one does not count.**
   - `packages/va_tools/src/va_tools/turn_data.py:99-102` builds the turn's variables only from this turn's
     readable fetches. Probe: A (index 0) and B (index 1) both set `plan`; turn 1 → `{'plan': 'from-b'}`;
     turn 2 (B answers 500, A ok) → `{'plan': 'from-a'}` while the lines stay `('Their plan is from-a.',
     'Their plan is from-b.')`. Item 3 says a failed fetch "changes nothing: the earlier value and lines
     stay"; here B's failure changes the value, and it flips back when B answers again.
   - Required, one of: (preferred) keep per entry the names its last counted fetch set (updated in
     `commit`, never in `refresh`: a refresh still changes nothing) and leave out of `FetchedTurn.variables`
     any name that a LATER entry which did not count this turn set at its last success; or state in item 3
     that a shared name follows the latest entry that counted in the turn. Either way a
     `test_turn_data.py` case for the shared name (B fails, B unreadable 2xx, B times out is enough as one
     parametrized or three short cases) and the rule in item 3. With the fix, mind the first turn of an
     inbound/rebuilt call (no last success: nothing to protect) and that `initial` lines say nothing about
     names (only fetches applied in this call count).

Decided, NOT part of this task (do not build them here):
- Fourth-pass item 2 + third-pass items 4 and 5 (no backoff for a hung per-turn endpoint; uvloop
  `getaddrinfo` pool of 4 threads; large answers parsed on the media loop): own FIX task, runtime task
  `fix:turn-data-backoff` (task-1793928628-1935). It changes Decision item 2 and adds an event outcome,
  which deserves its own plan. Planner: insert it as a P08-FIX task at the top of the checklist.
- Fourth-pass item 5 (recorder/brain cycle of pathway calls, predates T03b): already tracked as runtime
  task `fix:pathway-call-cycles` (task-1793902623-4ed4); T03b adds the edge `TurnDynamicData._events` →
  recorder to the same loop, the task-call GC guard in `test_runtime_dynamic_data_sim.py` passes. (I created
  and closed a duplicate, task-1793928628-46bb.) Planner: insert it as a P08-FIX task.

Checked myself and clean: `_decide`/`_pass` (no await between `apply()` and the merge; `changed` covers a
variable and a line; a failed decision cancels the refresh in the TaskGroup), `TaskBrain._refresh` (early
return without a port, `on_state` only on a change), `PreCallDynamicData.fetch` (every entry, `pre_call`
lines vs `turn_sections`), `_keeps_engine_state` vs the runtime's saver, `TurnDataSource` (guarded shared
client, `allow_http` local/test only, unsigned, `X-VA-Call-Id`), `TurnRecorder.dynamic_data` (level
passed), prompt assembly in task and pathway calls, the test doubles and the new tests (events/ManualClock,
no sleeps, real assertions), AGENTS.md bullet, specs/01 + specs/10 addenda.

After the fix: every gate again including `make e2e` (run it on a quiet host, not right after `make test`;
see `fix:e2e-cold-start-flakes`). On approval the Reviewer appends to `.ralph/review-inbox/DONE`:
`2026-11-05-p08-t03b-wip-review.md`, `2026-11-06-p08-t03b-recheck.md`, `2026-11-06-p08-t03b-third-pass.md`,
`2026-11-06-p08-t03b-fourth-pass.md` — the last two only once the two deferred items above are P08-FIX
tasks in `plan/phase-08.md` (else leave them for the Planner).

## Build P08-T03b, third pass (Builder, 2026-11-06T01:42Z-): the 3 review findings

Read first: the three 2026-11-06 inbox notes (recheck, third-pass, fourth-pass); nothing newer about this
task was in `.ralph/review-inbox` at 01:42Z. The two deferred items (`fix:turn-data-backoff`,
`fix:pathway-call-cycles`) are NOT built here, as the Reviewer decided.

1. **Only readable answers are cached; a stored non-document is a miss.**
   `va_tools/dynamic_data.py`: `if redis is not None and readable:` (readable implies not truncated);
   `_cached_answer` returns `None` when the stored body is not a JSON document
   (`_json_document(body, truncated=False)`), so the hit path is always readable and an entry written by
   a7f35b8 (text / bare string / `null` body) falls through to the request (logged
   `tools.dynamic_data_cache_invalid`); a readable answer then overwrites it, an unreadable one leaves it
   to age out and it stays a miss. Module docstring rules 2 and 3 reworded.
   Tests (`packages/va_tools/tests/test_dynamic_data.py`): the old
   `test_a_cached_text_answer_is_as_unreadable_as_the_fetched_one` is gone;
   `test_a_2xx_answer_that_is_no_json_document_is_not_cached[html|empty|json-string|json-null]` (no `dyn:`
   key after the bad answer, the second fetch sends a request and sets the variables, the third is a hit),
   `test_a_no_content_answer_is_not_cached` (204),
   `test_a_stored_value_that_is_no_usable_answer_is_a_miss[...,text-body,null-body]` (seeded with
   `redis.set`; the request goes out and its readable answer replaces the entry),
   `test_a_stored_answer_that_is_no_json_document_stays_a_miss_while_the_endpoint_is_unreadable`;
   `test_turn_data.py::test_one_unreadable_answer_does_not_pin_a_cached_entry` (the probe: bad answer once,
   recovery at the next refresh with `cache=True`, then a hit; 2 requests).
2. **`CallContext` ignores unknown top-level keys.** `va_domain/call_context.py`: `_TOLERANT =
   ConfigDict(frozen=True, extra="ignore")` on `CallContext` only; nested models keep `_FROZEN` (`forbid`),
   `SendCallConfig` untouched. Tests (`packages/va_domain/tests/test_call_context.py`):
   `test_a_top_level_key_a_newer_writer_added_is_ignored` (a dumped context plus an unknown key loads equal
   to the original, variables and lines kept); the rejection parametrization lost `extra-key` and gained
   `config-extra-key`, `memory-extra-key`, `fact-extra-key` (nested models still refuse). I found no reason
   `ignore` is wrong: `version: Literal[1]` still refuses a change of meaning.
3. **A shared name no longer flips.** `va_tools/turn_data.py`: `TurnDynamicData._names[index]` = the names
   the entry's last counted fetch set, written ONLY in `commit` (a refresh still changes nothing).
   `_turn_variables(results)` walks the results in `dynamic_data` order: a counting fetch sets its
   variables, one that does not count pops the names it holds from what the EARLIER entries set this turn
   (a later counting entry can still set the name). First turn / inbound / rebuilt context: nothing held,
   so nothing protected (`initial` lines say nothing about names), stated in the spec. Tests
   (`test_turn_data.py`): `test_a_later_entry_that_does_not_count_keeps_the_name_it_shares[failed|html|
   empty]` (A's other variable still arrives, lines unchanged, B wins again when it answers),
   `test_a_later_entry_that_times_out_keeps_the_name_it_shares` (ManualClock budget),
   `test_an_entry_holds_only_the_names_its_last_applied_counted_fetch_set` (never counted → holds nothing;
   fetched but never applied → holds nothing; counted without the name → releases it),
   `test_an_earlier_entry_that_does_not_count_takes_nothing_from_a_later_one`.

Proof the tests guard the fixes: with the three source changes reverted (temp copies in `/var/tmp/t03b3`,
restored afterwards) 15 of the 111 tests in the three files failed (6 turn_data, 8 dynamic_data, 1
call_context); restored: 111 passed.

The reviewer's probe `logs/probe_review_t03b3_test.py`: the shared-name case passes. The cache case still
"fails", on its own assertion `route.call_count == 3`: with the fix the count is 2, because the third
refresh is a legitimate hit of the READABLE answer the second one stored (printed: `('ok', False, False)`,
`('ok', False, True)`, `('ok', True, True)`, plan platinum from the second refresh on). Old tree: 1.

Specs: 13 §7 Decision P08-T03a rule 2 (stored shape + body must be a JSON document, else a miss; the two
meanings of "readable" kept apart: rule 2 no longer uses the word for the stored value) and rule 3 (only
readable answers are stored, why); Decision P08-T03b item 1 ("additive" replaced by what holds:
`extra="ignore"` at the top level from this build on), item 3 (unreadable 2xx is not cached; the
shared-name rule incl. the first-turn limit), Code paragraph; specs/01 §2.1 note; AGENTS.md bullet.

### Gates P08-T03b, third pass (real output, final tree; 2026-11-06T01:46Z-02:12Z)

No source, spec or AGENTS.md file changed after `make lint` started (01:46Z); the inbox had no note newer
than the fourth-pass one (01:22Z) when the gates finished.

- `make fmt` clean; `make lint` exit 0; `make typecheck` exit 0 (`Success: no issues found in 1633 source
  files`); `make placeholders` 0 findings; `check_spec_refs` 0 problems; `check_openapi_drift` current;
  jscpd `Found 0 exact clones` (1915 files); radon average A (2.94), `DynamicDataFetcher._fetch` C 11 (was
  12), `_unstorable` C 11 (unchanged), `_turn_variables` below C; pip-audit (CI export) `No known
  vulnerabilities found`; `pnpm audit --audit-level high` exit 0 (1 moderate).
- `make test` (started after the last source change): **13289 passed, 7 skipped in 873.86s**, coverage
  99.08 %, exit 0 (`logs/make_test_t03b3.log`; `va_tools/turn_data.py` 100 %, `va_domain/call_context.py`
  100 %, `va_tools/dynamic_data.py` 98 %: the three uncovered lines are the non-text Redis value in
  `_cached_answer` and the unrenderable URL in `_timed_out`, both from a7f35b8).
- `make e2e` (ONE run, on the running stack, apps rebuilt, started 30 s after `make test` ended, load
  average 2.0): **20 passed, 107 deselected in 419.64s**, exit 0 (`logs/make_e2e_t03b3.log`), the 50-call
  capacity flow included. Neither flake of the previous pass showed (`fix:e2e-cold-start-flakes` stays open).

For the Reviewer: the three inbox notes of 2026-11-06 are answered by items 1-3 above; fourth-pass items 2
and 5 and third-pass items 4 and 5 are the deferred tasks `fix:turn-data-backoff` and
`fix:pathway-call-cycles` (the latter is not in `ralph tools task ready` output of this iteration: check it
is still open before listing the notes in `DONE`).

## Review P08-T03b, third pass of the build / fourth review (Reviewer, 2026-11-06T02:12Z-02:36Z) — APPROVED

Gates re-run on the tree as handed over (no source change since 01:46Z; inbox had no note newer than the
fourth-pass one at 02:34Z): `make lint` exit 0, `make typecheck` exit 0, `make placeholders` 0 findings,
`check_spec_refs` 0 problems, `check_openapi_drift` current, jscpd 0 clones (1915 files), radon A (2.94),
pip-audit (CI export) no known vulnerabilities, `pnpm audit --audit-level high` exit 0 (1 moderate, tracked);
`make test` **13289 passed, 7 skipped in 818.95s**, coverage 99.08 %, exit 0
(`logs/make_test_review_t03b3.log`; `turn_data.py` and `call_context.py` 100 %); `make e2e` (started ~30 s
after `make test`, warm stack) **20 passed, 107 deselected in 418.72s**, exit 0
(`logs/make_e2e_review_t03b3.log`), capacity flow included.

The three findings of the previous review, checked in the code and by probes:
1. Cache: `if redis is not None and readable`, `_cached_answer` refuses a stored body that is no JSON document
   (miss, logged `tools.dynamic_data_cache_invalid`, overwritten by the next readable answer). My old probe
   `logs/probe_review_t03b3_test.py` now reads call_count 2 with `(ok, False, False)`, `(ok, False, True)`,
   `(ok, True, True)`: its `== 3` assertion was over-strict (the third refresh is a legitimate hit of the
   readable answer), as the Builder said. New probe `logs/probe_review_t03b4_test.py`: an old
   `{"status":204,"body":null}` entry is a miss and is replaced; a stored scalar document is a hit.
2. `CallContext` top level `extra="ignore"`, nested models and `SendCallConfig` still `forbid` (tests for
   config/memory/fact extra keys); spec item 1 reworded honestly.
3. Shared name: `_names` written only in `commit`; probe with three entries sharing `plan` over four turns
   gives `[{'plan': 'c1'}, {}, {}, {'plan': 'c4'}]` (no flip to an earlier entry). The first-turn/inbound/
   rebuilt-context limit is stated in spec item 3.
Files changed since the previous review are exactly those three sources, their three test files, specs/13,
specs/01 and AGENTS.md (mtimes); everything else was reviewed in the earlier passes.

Inbox: `2026-11-05-p08-t03b-wip-review.md` and `2026-11-06-p08-t03b-recheck.md` appended to `DONE` (every
item fixed). NOT in `DONE`, on purpose: `2026-11-06-p08-t03b-third-pass.md` and
`2026-11-06-p08-t03b-fourth-pass.md`. Their items 1-3 (third) / 1, 3, 4 (fourth) are fixed by this commit;
what is still open in them and must become P08-FIX tasks at the top of `plan/phase-08.md` (Planner):
- third-pass items 4 + 5, fourth-pass item 2 + "unchanged, low confidence": no backoff for a hung per-turn
  endpoint (uvloop `getaddrinfo` pool of 4 threads, `UV_THREADPOOL_SIZE`), per-turn answer size parsed on the
  media loop → runtime task `fix:turn-data-backoff` (task-1793928628-1935). Changes Decision P08-T03b item 2
  and adds an event outcome.
- fourth-pass item 5: recorder/brain cycle of pathway calls (`recorder.tagger = brain`), confirm with
  `unreachable_after()` in `test_runtime_pathway_sim.py` first → runtime task `fix:pathway-call-cycles`
  (task-1793902623-4ed4).
Once both are FIX tasks (or approved), list the two notes in `DONE`.
