# Phase 08 scratchpad

## Plan P08-T02b1 — task-mode custom tools in calls (executor, events, variables, speech, fillers)

Planner split P08-T02b (2026-11-05) into T02b1 (this) and T02b2 (`send_sms`/`schedule_callback`: needs sms_capable
from the DB and a scheduled child call + workflow start from the media worker). T02c and T11 now depend on T02b1.
Inbox: every note is in DONE.

### 1. `va_engine.tool_executor` (protocol change)
- Add `@dataclass(frozen=True, slots=True) class ToolAnswer: content: str; variables: Mapping[str, JsonValue] =
  field(default_factory=dict)`.
- `ToolExecutor` protocol: keep `definitions()`, `tool_speech`, `handles(name)`; add `speech(name) -> str | None`
  (the tool's raw `speech` template text, `None`/blank = none); change `execute(call: ToolCall, scope: TemplateScope)
  -> ToolAnswer`. Update docstring. Update `packages/va_engine/tests/support/task_helpers.RecordingExecutor`
  (+ any test caller: grep `RecordingExecutor(`/`.execute(`) — add `speeches: dict[str,str]`, `variables: dict[str,
  dict]`, `delay_s`/clock-driven hold, record `(name, started_at)` so harness tests can time starts.

### 2. `va_voice.phrase_cache` fillers
- `TOOL_FILLERS: Final = ("One moment.", "Just a second.", "Let me check.")` (specs/13 §5 step 2 list, verbatim).
- `STANDARD_FILLERS = ("Sure.", "Got it.", "One moment.", "Let me check that for you.", "Just a second.",
  "Let me check.")` so the prefetch (`va_workflows.activities.prerender`) caches the tool fillers too. Fix counts in
  prerender/phrase cache tests (`fillers=` totals). Export `TOOL_FILLERS` in `__all__`.

### 3. `packages/va_tools/src/va_tools/executor.py` (new)
```python
class ToolEvents(Protocol):
    def tool(self, data: dict[str, JsonValue]) -> None: ...          # call.tool
    def tool_failed(self, detail: str) -> None: ...                  # call.error code TOOL_FAILED
def merged_tools(stored: Sequence[ToolSpec], inline: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]
    # stored (in tool_ids order) minus names an inline tool reuses, then inline in order; reserved names
    # (va_engine.system_tools.is_reserved) and later duplicates dropped with log `tools.tool_skipped` (reason)
class CallToolExecutor:  # implements va_engine.tool_executor.ToolExecutor
    def __init__(self, specs: Sequence[ToolSpec], *, runner: ToolRunner, events: ToolEvents | None = None)
    definitions() -> tuple(function_def(s) ...); tool_speech -> first non-blank spec.speech; handles(name); speech(name)
    async def execute(call, scope) -> ToolAnswer:
        inv = await runner.run(spec, call.arguments, scope); events.tool(inv.event_data()) (every run, incl.
        invalid_arguments); if inv.failed: events.tool_failed(failure_detail(inv)); log `tools.call_ran`
        (tool, outcome, status, attempts, duration_ms — never args/headers); return ToolAnswer(inv.content, inv.variables)
```
  Event callbacks are sync (the recorder queues publishes). No bound methods of an owner held by per-call parts beyond
  the recorder itself (AGENTS P08-FIX-3 cycle rule: pass the recorder object, not `self.something`).
- Unit tests `packages/va_tools/tests/test_executor.py`: inline overrides stored same name (definition + the inline URL
  is hit, respx); reserved-name/duplicate skipping; definitions order; `call.tool` data masked (secret header `••••`);
  failing 500 → `tool_failed("<name>: http_error (500)")`; invalid args → `call.tool` only, no failure; variables only
  from 2xx; `speech()`/`tool_speech`.

### 4. `TaskBrain` (`packages/va_engine/src/va_engine/task_runner.py`)
- New kw-only params: `tool_fillers: bool = True` (runtime passes `org.engine.tool_fillers`; settings are already
  passed — read `settings.engine.tool_fillers` directly instead, no new param), `on_state: Callable[[EngineState],
  None] | None = None` (called after every tool round that changed variables, and after each `_respond` ends).
- `_execute`: pass `self._scope()` snapshot taken at round start; merge each `answer.variables` into
  `self._state.variables` in call order after the round; exceptions still → `TOOL_FAILED`.
- Restructure `_run_tools` (constants in `task_tools`: `FILLER_DELAY_S: Final = 1.2`):
  1. start one `asyncio.Task` per executed call (semaphore `EXECUTOR_CONCURRENCY`), names `tool-<name>`;
  2. if `settings.engine.tool_fillers`: line = rendered speech of the FIRST executed call with non-blank
     `executor.speech(name)` (render via `parse_template` + `render` with `self._scope()` and `input={"input":
     args}` — args decoded leniently, `{}` on bad JSON; a template that does not parse → no line, log
     `task_tool_speech_invalid`). If line: `yield SpeakStatic(line)` right away (speech concurrent with the request).
     Else, when tasks are pending: wait until all done or `clock.sleep(FILLER_DELAY_S)` (asyncio.wait over the
     tasks + a sleeper task on the injected clock, sleeper cancelled after); still pending → `yield
     SpeakStatic(TOOL_FILLERS[self._fillers % 3])`, `self._fillers += 1` (round-robin per call, ≤ 1 per round);
  3. await all tasks; then answer every planned call in call order (unchanged pairing logic) and yield PressButton;
  4. `finally`: cancel unfinished tasks and `await asyncio.wait(...)` them; unanswered calls → `INTERRUPTED` (existing).
  Speech/filler lines are NOT inserted into `self._messages` (an assistant message may not sit between the
  tool_calls message and its tool messages); record them only as a `TurnRecord(role="assistant")` in state
  history via a helper `_note_line(text)` (bump `_records_added` so `_drop_turn` stays consistent).
  Step 9: the follow-up request starts after the tools answered and the line finished playing (the generator is
  parked during playback) — record this as a Decision (below).
- Keep the per-turn/per-call caps as they are (`plan_round`).
- Engine tests `packages/va_engine/tests/test_task_tool_speech.py` (pipeline harness, AcceleratedClock, a scripted
  `DialogueSource` from `dialogue_helpers.FakeSource` — extend `FakeStream`/`reply(...)` with `tool_calls=` if it
  cannot script tool calls yet — and a clock-driven executor from task_helpers):
  - `test_tool_speech_starts_within_300_ms_of_the_tool_call` (speech set, 2 s tool; first speech frame ≤ 300 ms
    after the round's stream ended; executor start time ≤ speech start — request concurrent with speech)
  - `test_a_filler_plays_at_1200_ms_while_a_slow_tool_runs` (no speech, 3 s tool: filler text sent to TTS/static at
    1 200–1 400 ms, exactly one)
  - `test_no_filler_when_the_tool_answers_quickly` (300 ms tool)
  - `test_fillers_rotate_round_robin_across_tool_calls` (3 slow rounds → the 3 phrases in order)
  - `test_one_filler_per_round_for_parallel_calls`
  - `test_tool_fillers_off_silences_speech_and_fillers` (`engine.tool_fillers=false`)
  - `test_a_tool_timeout_answer_reaches_the_model` (CallToolExecutor + ToolRunner + HttpExecutor over a hanging
    respx route/receiver with `timeout_ms=500`: next request's tool message `{"status":0,"error":"timeout"}`, reply spoken)
  - `test_extracted_variables_merge_into_state_and_on_state`
  - `test_barge_in_during_tool_speech_cancels_the_tool` (tool task cancelled, tool message `INTERRUPTED`, pairing holds)
  Existing task runner tests must keep passing (`assert_tool_pairing`).

### 5. Media wiring
- `apps/media_worker/src/va_media/call_tools.py` (new):
  `async def load_call_tools(database, envelope, org_id, tool_ids) -> tuple[ToolSpec, ...]` — one
  `tenant_session(org_id)` reading the non-deleted rows (add `ToolRepo.by_ids(ids) -> list[Tool]` in
  `va_db.repositories.tools`), session closed BEFORE decrypting (`decrypt_secret_headers`, AGENTS P06-T04a rule),
  returned in `tool_ids` order, missing ids logged `media.tool_missing` (count only).
  `@dataclass(frozen=True, slots=True) class CallToolSource(database, envelope, tools_http, allow_http)` with
  `async def executor(self, context, events) -> CallToolExecutor | None` (None when the call has no tool_ids and no
  inline_tools; a load failure (`SQLAlchemyError`, `OSError`, crypto errors) → `media.tools_unavailable` warning and
  only the inline tools); `HttpExecutor(tools_http, clock=, allow_http=, call_id=str(call_id))` like pathway_runtime.
- `TurnRecorder` (`va_media/turns.py`): implement `ToolEvents`: `tool(data)` publishes `EventType.CALL_TOOL`
  (message e.g. `f"Tool {name}: {outcome}"`, check va_events EVENT_MESSAGES conventions/e2e `EVENT_MESSAGES`),
  `tool_failed(detail)` publishes `EventType.CALL_ERROR` data `{"code": "TOOL_FAILED", "detail": detail}` (specs/19
  §2.1; same shape other producers use — grep `TOOL_FAILED`/`"code"` in va_events/va_api).
- `CallRuntime(..., call_tools: CallToolSource | None = None)`; `_brain` task branch: `executor = None if
  self._call_tools is None else await self._call_tools.executor(context, recorder)`; `TaskBrain(..., executor=,
  on_state=saver.on_state)` with an `EngineStateSaver` for task calls too (registered `stack.push_async_callback
  (saver.aclose)` like pathway) so `CallEnd._merge_variables` writes extracted variables into `calls.variables`
  (verify CallEnd works for runner `task`). `open_runtime` builds `CallToolSource(database, envelope, tools_http,
  insecure_urls_allowed(settings.va_env))` sharing the existing `tools_http`. Update the module docstring (new step).
- `runtime_rig.sim_runtime(..., call_tools=)`.
- Tests `apps/media_worker/tests/test_call_tools.py` (media_db_url): stored tool with an encrypted secret header
  (local envelope, `va_tools.secrets.encrypt_secret_headers`) loads with the decrypted value; deleted/other-org/missing
  ids skipped; inline override; no tools → None; broken database → inline only.
  `apps/media_worker/tests/test_runtime_tools_sim.py`: task call with an inline tool (speech "Let me look that up.")
  pointing at `webhook_receiver.served_receiver`; FakeLLM rules: after the user turn a `tool_calls` reply, then a
  content reply quoting the tool answer (match on a `tool` message / `call_index`); assert receiver got the rendered
  body, FakeElevenLabs `tts_requests[*].generated_text` contains the speech and the final reply, the second LLM
  request carries the tool message with `data`/`extracted`, `call.tool` published (recording publisher) and
  `call:{id}:engine` snapshot holds the extracted variable.

### 6. Spec Decision (same commit) under specs/13 §5: `> Decision (2026-11-05) P08-T02b1:` executor/loader names;
inline overrides stored by name (reserved/duplicate dropped); `call.tool` for every run, `call.error` TOOL_FAILED for
failures; one speech line (first executed call with speech, rendered with `input.*`) or one filler per round,
round-robin `TOOL_FILLERS` per call after 1 200 ms; `engine.tool_fillers=false` silences both (specs/04 §7 row);
lines are transcript records, not LLM messages; the follow-up request starts once tools answered and the line played;
extracted variables snapshotted to `call:{id}:engine` → `calls.variables`. Note the filler list in specs/09 §9 too.

### Gates
`make fmt lint typecheck placeholders`, `make test` (background + heartbeat monitor, ~25 min), jscpd, radon. No e2e
flow changes (T11 adds tool_call_with_speech); `make e2e` optional.

### Risks
- Async generator with live tasks across yields: always cancel+await in `finally` (aclose path).
- Harness timing on AcceleratedClock: time the tool on the injected clock; don't busy-poll (freezes time).
- Filler sleeper must use `self._clock.sleep`, never asyncio.wait timeout.
- Per-call cycles (P08-FIX-3): executor/events must not hold bound methods of the runtime; `gc_cycles` guard in
  existing runtime tests may catch it.
- STANDARD_FILLERS change shifts prerender counts and phrase cache tests.

## Build P08-T02b1 (2026-11-05)
Implemented per plan, plus request signing (spec step 4) in `CallToolSource` with the org's active webhook
secrets (unsigned + `media.tool_signing_unavailable` when unreadable). `on_state` fires only after a round
that merged variables; the saver exists only for task calls with tools, and `CallEnd` merges variables for
those (`_keeps_engine_state`). RecordingExecutor field `speech` renamed `note` (usage-note speech); the hold
test now expects the FrozenClock filler. FakeStream gained `__aenter__/__aexit__` + `error`.
Gates: make test 12897 passed / 7 skipped, coverage 99.07 %; lint/typecheck/placeholders pass; jscpd 0 clones;
radon avg A (2.41); pip-audit clean, pnpm audit 1 moderate (below high). e2e not run (no flow change; T11 adds
tool_call_with_speech).
