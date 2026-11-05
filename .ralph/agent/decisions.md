# Decision Journal

Consequential decisions taken during the build, newest at the bottom. IDs are sequential.

Template:

```
## DEC-NNN — <short title>

- **Decision:** <the question that had to be answered>
- **Chosen Option:** <what was done>
- **Confidence:** <0-100>
- **Alternatives Considered:** <options and why they lost>
- **Reasoning:** <why the chosen option wins>
- **Reversibility:** <yes/no, and what undoing it costs>
- **Timestamp:** <UTC ISO 8601>
```

---

## DEC-001 — Where the specs/02 §6 env-table parser lives

- **Decision:** P00-T02 needs to parse the specs/02 §6 environment-variable table to prove `.env.example` mirrors
  it. Should that parser be a reusable module, inline test code, or a new `make check` gate script?
- **Chosen Option:** a library module `scripts/env_spec.py` (typed, no CLI, no import side effects), imported by
  `scripts/tests/test_env_example.py` via the existing `pythonpath = ["scripts"]` pytest setting. Parity is
  enforced through `make test` only.
- **Confidence:** 75
- **Alternatives Considered:**
  - Parser inline in the test module — less reusable, and invisible to the settings tests coming in P00-T05, which
    will want the same §6 row data to check `BaseServiceSettings` field coverage.
  - A new `scripts/check_env_example.py` gate wired into `make check` — rejected: specs/02 §3 fixes the `make check`
    recipe and §9 lists the complete gate table with no env gate in it, so adding one would contradict the spec.
    Enforcing the same property as a pytest test needs no spec change.
- **Reasoning:** the §6 table is a shared contract that at least two future tasks must read programmatically, so the
  parsing logic belongs somewhere importable; keeping it a plain library (no `main`, no argv) means it costs nothing
  if a later phase does decide to wrap it in a gate.
- **Reversibility:** yes — moving the functions into the test module or into a gate script is a module-local change
  with no callers outside `scripts/`.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-002 — Scope and allowances of the unfinished-work detector gate

- **Decision:** specs/02 §9.1 leaves open which files count as "tracked", how case-insensitive the markers are, and
  whether common legitimate uses (Tailwind `placeholder:` variant, `@overload` stubs, `.pyi` files, the gate's own
  module name) must fail.
- **Chosen Option:** scan tracked + untracked-not-ignored files; markers upper-case only with word boundaries; allow
  the Tailwind `placeholder:` variant, the identifier `check_no_placeholders`, `@overload` bodies, and `.pyi` files;
  no generic suppression comment. Recorded as the `> Decision (2026-10-25)` block under specs/02 §9.1.
- **Confidence:** 75
- **Alternatives Considered:**
  - Tracked files only — rejected: Builder output is uncommitted when gates run, so the gate would miss it.
  - Case-insensitive markers — rejected: flags ordinary English ("hack", "todo list") and forces awkward prose.
  - Failing on Tailwind `placeholder:` — rejected: shadcn/ui's Input ships it; it is the utility form of the
    allowed `::placeholder` pseudo-element. Failing on the module name would make `make placeholders` unrunnable.
- **Reasoning:** keeps the gate strict on real unfinished work while not punishing framework idioms the spec
  clearly intended to allow.
- **Reversibility:** yes — each allowance is one regex alternative or predicate in `scripts/check_no_placeholders.py`.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-003 — OpenAPI drift gate behaviour before the API exists

- **Decision:** `make check` runs `scripts/check_openapi_drift.py` from P00-T04 on, but `va_api` and
  `openapi/openapi.json` only arrive in P00-T11. Should the gate fail, be left out of `make check`, or skip?
- **Chosen Option:** skip with exit 0 and a stderr notice only while BOTH the `va_api` package and the committed
  document are absent; any half-state or import failure exits 1. Recorded in the specs/02 §9 Decision block.
- **Confidence:** 80
- **Alternatives Considered:**
  - Fail until P00-T11 — rejected: `make check` would be red for six tasks, violating "keep the build green".
  - Remove the call from the `make check` recipe — rejected: specs/02 §3 fixes the recipe.
- **Reasoning:** the skip condition cannot be hit once P00-T11 lands (both artefacts exist), so the gate becomes
  strict automatically with no later edit needed.
- **Reversibility:** yes — delete the bootstrap branch in `main()` once `va_api` exists.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-004 — Default HTTP status for the two-status `KB_ERROR` code

- **Decision:** specs/31 §3 lists `KB_ERROR` as `400/500`, but `CODES` maps each code to one status.
- **Chosen Option:** `CODES["KB_ERROR"] = 400`; `500` allowed only as an explicit `http_status` override via
  `va_common.errors.ALTERNATE_STATUSES`. Recorded as the `> Decision (2026-10-25)` block in specs/31 §4.
- **Confidence:** 75
- **Alternatives Considered:**
  - Default 500 — rejected: most KB failures (bad file content, parse failures) are caused by the client upload.
  - `CODES` values as sets — rejected: specs/31 §4 fixes `CODES: dict[str, int]`.
- **Reasoning:** keeps the spec's type, makes the first-listed status the default, and still lets server-side
  failures (embedding provider down) report 500 deliberately.
- **Reversibility:** yes — change one entry in `CODES`/`ALTERNATE_STATUSES` and the spec note.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-005 — Split P03-T02 (FakeTelnyx) into REST/webhooks and call control

- **Decision:** P03-T02 covers ~40 endpoints, the signed webhook emitter, and scenario-driven call progress; too
  large for one Builder activation with full tests.
- **Chosen Option:** P03-T02a (keys, auth/accounts, errors, provisioning/number/message REST, webhook emitter,
  supervised tasks) then P03-T02b (dial, call actions, conferences, scenario progress, AMD, inbound, call control
  API). P03-T03 and P03-T08 now depend on P03-T02b.
- **Confidence:** 85
- **Alternatives Considered:**
  - One task — rejected: P03-T01 (smaller) already filled an activation; a blocked build costs a full cycle.
  - Split by HTTP family only — rejected: call control needs the emitter and keys first, so they go in T02a.
- **Reasoning:** T02a is self-contained and testable (messages and number orders exercise the emitter end to end).
- **Reversibility:** yes — the plan ids are the only artefact.
- **Timestamp:** 2026-10-29T00:00:00Z

## DEC-006 — Split P03-T13 (`va_llm` gateway) and offline token estimation

- **Decision:** P03-T13 bundles providers/org overrides/probe, the §2.1 policy matrix, the breaker, metering, and
  streaming dialogue with speculation; too large for one activation. specs/10 §11 estimates missing usage with
  tiktoken, but tiktoken downloads its BPE file at first use (network in tests, not in CI).
- **Chosen Option:** P03-T13a (core + non-streaming `complete`) then P03-T13b (streaming dialogue). Missing usage
  is estimated offline as `ceil(utf8_bytes / 4)` per text (+4 per message) and flagged `estimated=True`; tiktoken
  arrives with `window.py` (phase 05) together with a pinned, checksum-verified encoding asset. Probe uses
  `max_tokens=16` (a forced tool call cannot fit in 1 token; FakeLLM rule 5 and real providers truncate it).
  Built-in COGS defaults in `va_llm.pricing.MODEL_COSTS` are provisional values overridable by `LLM_MODEL_COSTS_JSON`.
- **Confidence:** 70
- **Alternatives Considered:**
  - One task — rejected: P03-T12 alone needed a fix cycle.
  - tiktoken now with a fetched asset — rejected: CI does not run `fetch_models.sh`; estimation is a rare path
    (Telnyx Inference and FakeLLM both send `include_usage`).
- **Reasoning:** keeps T13a testable offline; written as a `> Decision` in specs/10 §1/§11 by the Builder.
- **Reversibility:** yes — estimator is one function; prices are data.
- **Timestamp:** 2026-11-01T00:00:00Z

## DEC-007
- Decision: media stream token expiry when specs/06 §4.2 (+1 h) and specs/28 §6.2 (+10 min) disagree
- Chosen Option: exp = mint time + ceil(max_duration) + 600 s (specs/28)
- Confidence: 75
- Alternatives Considered: + 1 h (specs/06)
- Reasoning: security spec owns the token; 10 min still covers ring time and a Telnyx WS reconnect at the end of the call; shorter replay window
- Reversibility: high (one constant)
- Timestamp (UTC ISO 8601): 2026-11-01T00:00:00Z

## DEC-009
- **Decision:** How to end phase 07 while runtime task fix:e2e-interrupt-level1 (P2 e2e flake) is open; Ralph rejects LOOP_COMPLETE with open tasks (diagnostics log: "Rejecting completion event with 1 open task(s)").
- **Chosen Option:** Carry the flake into plan/phase-08.md as P08-T00 (first task, explicit acceptance), mark the runtime task failed with that note, print LOOP_COMPLETE.
- **Confidence:** 75
- **Alternatives Considered:** Fix it inside phase 07 as P07-FIX-2 (exit gate already passed twice without it; adds iterations to a finished phase); closing the task unverified (dishonest).
- **Reasoning:** Phase 07 exit gate passed; the flake is tracked in the next phase's checklist so the Planner fixes it first.
- **Reversibility:** Yes: `ralph tools task reopen task-1793787934-2f0c` and remove P08-T00.
- **Timestamp:** 2026-11-04T15:30:00Z

## DEC-010
- Decision: How to resolve the P08-T00 build.blocked (make e2e capacity flow fails, pre-existing at HEAD)
- Chosen Option: Insert P08-FIX-3 (50-call underruns + overhead p50 margin) directly after P08-T00; scope P08-T00's `make e2e` acceptance to all flows except `-m capacity`, and send P08-T00 back to Builder/Reviewer for approval on its own acceptance
- Confidence: 85
- Alternatives Considered: split P08-T00 (nothing left to split); put P08-FIX-3 first (would have to commit around the uncommitted P08-T00 tree)
- Reasoning: Builder showed with images rebuilt at HEAD that 3/8 capacity runs fail (underruns 14/3/1) without the P08-T00 diff; the failure is unrelated to the interrupt-data/accept-log change and needs its own root-cause task (MASTER_PROMPT §7 target)
- Reversibility: High (plan text only)
- Timestamp: 2026-11-04T00:00:00Z

## DEC-011
- Decision: Approve P08-FIX-2 despite one failing test in the reviewer's `make test` run
- Chosen Option: Approve; record the flake as P08-FIX-5
- Confidence: 75
- Alternatives Considered: reject and send the builder to fix an unrelated media test
- Reasoning: the diff is test-only in va_workflows; the failure (`apps/media_worker/tests/test_runtime_sim.py::test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks`) came from a wall-clock FakeLLM timeout plus a gateway retry under xdist load. It passes 3/3 alone, and the builder's full run was green.
- Reversibility: high
- Timestamp: 2026-11-04T23:40:00Z

## DEC-012
- Decision: How to settle same-name tools on pathway nodes (review note 2026-11-05-p08-t02c1-recheck.md item 4) and where a superseded turn's tool results go in task mode (item 3)
- Chosen Option: (a) ref-based resolution: `PathwayToolExecutor.for_refs(refs) -> ToolExecutor` (a node offers and runs exactly the definitions its refs name; first ref of a name wins within the node), no new specs/11 §11 validation rule; (b) task mode re-appends the superseded turn's completed tool rounds BEHIND the merged caller message, pathways keep the `tool` records AHEAD of it
- Confidence: 75
- Alternatives Considered: a `PATHWAY_TOOL_CONFLICT` validation rule (needs stored tool names in `GraphRefs`, blocks publishes, and a stored tool renamed after publish still collides, so a runtime rule is needed anyway); letting the pipeline hold instead of supersede once a tool ran (changes specs/07 §8 turn routing for every Brain); task-mode rounds ahead of the merged message (assistant message directly after an assistant message: not accepted by every OpenAI-compatible chat template)
- Reasoning: the safety property is "the definition the model was shown is the one that runs"; only per-node resolution guarantees it for inline and stored tools alike. `user → assistant(tool_calls) → tool` is the standard provider shape and ties the calls to the caller's request, which is what stops a second booking; pathway history is a chronological record list rendered as context updates and its decision calls expect the caller's record last.
- Reversibility: High before P08-T02c2 (protocol used only by the engine and the test fake); medium afterwards
- Timestamp: 2026-11-05T11:50:00Z

## DEC-013
- Decision: Scope of the fix for review note 2026-11-05-p08-fix-19-wip-review.md item 1 (a tool that ran to completion is recorded `interrupted` when the reply is closed at an earlier press)
- Chosen Option: one rule for every cut of a tool round (pathway nodes and task mode): an executor call whose run finished keeps its answer and its variables are merged at the cut; calls answered without running keep their planned answer; only cancelled executor runs and actions not handed over read `interrupted`
- Confidence: 75
- Alternatives Considered: the note's narrower fix (real answers only once every task of the round finished, i.e. only for a cut at a press; a barge-in during the filler keeps answering everything `interrupted`)
- Reasoning: the note's own principle is "only answers that report something that did not happen may read interrupted". A tool that finished before a barge-in on the filler (while a slower tool of the same round still runs) has the same risk: side effect and `call.tool` event in place, model told `interrupted`, second booking. specs/13 §5 Decision P08-T02b1 item 4 already says the barge-in answers the *unfinished* tools `interrupted`. The general rule needs no "where did the cut land" state and passes every test the note asks for.
- Reversibility: High (one shared helper, two `finally` blocks)
- Timestamp: 2026-11-05T14:10:00Z

## DEC-014
- **Decision:** What to do with review note 2026-11-05-p08-fix-20-wip-review.md item 1 (a `send_sms`/`schedule_callback` run that continues after the round was cut is answered `interrupted`, so the model may send the text again)
- **Chosen Option:** Fix it in P08-FIX-21 with an explicit query: `ToolExecutor.detached(call) -> bool` (by object identity); a cut round answers a cancelled executor call `{"status":"in_progress"}` when the executor says its run goes on, `interrupted` otherwise. `CancelledError` always propagates.
- **Confidence:** 70
- **Alternatives Considered:** (a) close the item with a written reason (narrow window, predates FIX-20); (b) let `CallToolExecutor._run_system` catch the cancellation of its wait and return the `in_progress` answer (no protocol change). Rejected: a runner parked in `await task` that is itself cancelled forwards the cancel to the tool task; if that task then finishes with a result, the runner's own cancellation is lost and the reply keeps running. It would need shielded awaits in both runners and stays a trap for every later caller.
- **Reasoning:** Decision P08-FIX-20's rule is "interrupted only for what did not happen"; for an executor-owned run the opposite is known at the cut, and a duplicate customer SMS is a visible defect that P08-T02c2 would spread to pathway calls. The query costs one protocol method, two fakes, and one extra argument to `settle_round`.
- **Reversibility:** High before P08-T02c2 (one protocol method; implementations: `CallToolExecutor` and the test fakes)
- **Timestamp:** 2026-11-05T15:05:00Z
