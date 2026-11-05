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

## Plan P08-T02b2 — executor-backed system tools `send_sms` and `schedule_callback` (task mode)

Inbox: every note is in DONE. Specs: specs/13 §6 table + Decision 2026-11-02, specs/10 §7 Decision P05-T09b
(availability: `send_sms` = agent number SMS-capable + `tools.allow_send_sms`; `schedule_callback` =
`tools.allow_schedule_callback or engine.allow_callbacks`; schema `{datetime_iso (req), note}`), specs/05 §3.3
`start_time` rules (naive → call `timezone`, future, ≤ 90 days; `MAX_SCHEDULE_AHEAD`, `parse_start_time`).
Engine side already works: `plan_round` sends parsed `SendSmsArgs`/`ScheduleCallbackArgs` to the executor when
`handles(name)`; TaskBrain strips sms/callback/KB facts only when `executor is None`. So this task = handlers +
media wiring + availability facts. Pathway calls get these in P08-T02c (not here).

### 1. Shared child-call builder (va_db) — reused by the retry activity
- New `packages/va_db/src/va_db/call_children.py`: move `COPIED_COLUMNS` here from
  `va_workflows.activities.retry` (retry imports + re-exports it in `__all__`; keep its test green) and add
  `def child_call(parent: Call, *, child_id: uuid.UUID, config: dict[str, JsonValue], start_time: datetime,
  outbound: bool = False) -> Call` — `status="scheduled"`, `queue_status="new"`, `parent_call_id`, `workflow_id=
  call_workflow_id(child_id)`, `credit_hold_id=None`, copied columns; `outbound=True` on an inbound parent sets
  `direction="outbound"`, `to_number=parent.from_number`, `from_number=parent.to_number` (the caller is called back
  from the agent number). Refactor `RetryActivities._create_child` to use it.
- New `packages/va_db/src/va_db/callbacks.py`:
  `@dataclass(frozen=True) class ScheduledCallback: call_id: uuid.UUID; scheduled_for: datetime; created: bool`;
  `async def schedule_callback(session, org_id, parent_id, *, child_id, scheduled_for, note: str | None) ->
  ScheduledCallback` — lock parent (`SELECT ... FOR UPDATE`, org filter; missing → `NotFoundError`), existing row
  with `id == child_id` → `created=False` (duplicate guard for a repeated tool call), else insert
  `child_call(parent, ..., config=callback_config(parent.config, scheduled_for, note), outbound=True)` and flush.
  Caller's tenant session (RLS).

### 2. Pure callback rules (va_domain)
- New `packages/va_domain/src/va_domain/call_callback.py`:
  - `CALLBACK_NOTE_KEY: Final = "callback_note"` (non-config `calls.config` key like `retry_attempt`).
  - `class CallbackTimeError(ValueError)` with `.reason` in `"invalid_datetime" | "in_the_past" | "too_far_ahead"`.
  - `def callback_time(value: str, timezone: str, now: datetime) -> datetime` (UTC): `parse_start_time` (ValueError
    → invalid_datetime), naive → `ZoneInfo(timezone)` fold=0, `<= now` → in_the_past, `> now + MAX_SCHEDULE_AHEAD`
    → too_far_ahead. Import `MAX_SCHEDULE_AHEAD` from `va_domain.call_resolution.resolve` (check import cycles;
    if cyclic, move the constant to `va_domain.calls.parts` and re-export).
  - `def callback_config(stored, scheduled_for, note) -> dict[str, JsonValue]`: copy; `start_time` = ISO UTC
    (same `_iso` form as `call_retry.child_config` — reuse/expose it), drop `RETRY_ATTEMPT_KEY` and
    `va_domain.numbers.MAINTENANCE_MESSAGE_KEY`, set `CALLBACK_NOTE_KEY` only when note is non-blank; input
    untouched.

### 3. Executor support for system tools (va_tools)
- `va_tools/executor.py`: `class SystemTool(Protocol): name: str (property); async def run(self, call: ToolCall)
  -> SystemOutcome` with `@dataclass(frozen=True) class SystemOutcome: content: str; failure: str | None = None`
  (failure = short reason, `None` = success). `CallToolExecutor(specs, *, runner, events=None, system:
  Sequence[SystemTool] = ())`: `definitions()` custom only (runner adds system defs), `handles(name)` custom or
  system name, `speech(name)` None for system tools (→ filler path), `execute`: system call → `outcome = await
  tool.run(call)`; `failure` → `events.tool_failed(f"{name}: {failure}")`; no `call.tool` for system tools (their
  domain events are their own); `ToolAnswer(outcome.content)`. Unexpected exceptions are still caught by the
  TaskBrain (`TOOL_FAILED`). Log `tools.system_tool_ran` (tool, outcome) — never message text/times.
- `ToolEvents` protocol gains `def sms_sent(self, data: dict[str, JsonValue]) -> None`; `TurnRecorder.sms_sent`
  publishes `call.sms_sent` (message `TOOL_SMS_SENT_MESSAGE = "SMS sent"` defined in turns or call_system_tools).
  Update any `ToolEvents` fakes in va_tools/va_engine tests.

### 4. Media handlers — new `apps/media_worker/src/va_media/call_system_tools.py`
- `async def sms_capable(database, context) -> bool`: `sms_sender(session, org_id, agent_number(context)) is not
  None` in one tenant session; `SQLAlchemyError`/`OSError` → False + `media.sms_capability_unavailable`.
  Decision: readiness (10DLC campaign / toll-free verification) folds into `va_db.voicemail_sms.sms_sender` with
  P14-T05, so every sender check gets it at once.
- `def tool_message_id(call_id, tool_call_id) -> uuid.UUID` = uuid5(namespace, f"{call_id}:{tool_call_id}").
- `class ToolSms` (SystemTool `send_sms`): `(sender: OutboundSms, context, events: ToolEvents | None)`; `run`:
  parse `message` (already schema-validated); `sender.deliver(context, sender_number=agent_number(context),
  to=counterpart_number(context), text=message, message_id=tool_message_id(...))`; success → `events.sms_sent(
  {"message_id","conversation_id","segments","source":"tool"})`, content `{"status":"sent","segments":n}`;
  `SmsNotSent` → log `refused.event` + reason, content `{"error":"sms_failed","reason":<reason>}`, failure=reason
  (`already_queued` = duplicate: content `{"status":"sent"}`? → no: answer `{"error":"sms_failed","reason":
  "already_queued"}` without failure event is simplest; pick: duplicate counts as sent, no event, no failure).
- `class CallbackScheduler` (SystemTool `schedule_callback`): `(database, publisher: EventPublisher, router:
  WebhookRouter, starter: CallbackStarter, context, clock)`. `run`: `callback_time(args.datetime_iso,
  context.config.timezone, clock.now())` (CallbackTimeError → content `{"error":"invalid_datetime","reason":r}`,
  no failure event — the model may correct it); child id = `tool_message_id(...)`; tenant session:
  `schedule_callback(...)`; when created: `call.scheduled` event for the CHILD (`new_event(..., call_id=child,
  message=SCHEDULED_MESSAGE, data={"status":"scheduled","scheduled_for":ISO,"warnings":[],"parent_call_id":
  str(parent),"source":"callback"})` → `publisher.sequence` → `va_events.insert_rows(session,[row_for(e)])` →
  `router.route(sequenced, session=session)` in the same transaction; after commit `publisher.broadcast`; then
  `starter.start(CallWorkflowInput(call_id=child, org_id, scheduled_for=...))`. Start failure → transition child
  `failed` from `{"scheduled"}` (`end_reason="error"`, `ended_by="error"`, `queue_status="call_error"`,
  error_message) + `call.error` `{"code":"dispatch_failed","detail":"workflow_start"}` on the child (same
  pattern as api `_record_start_failure`, no hold/limits: admission happens at dial) and content
  `{"error":"callback_failed"}`, failure="workflow_start". DB failure → `{"error":"callback_failed"}`,
  failure="database". Success content `{"status":"scheduled","scheduled_call_id":..., "scheduled_for": <local ISO
  in the call timezone>}`. Duplicate (created False) → same success content, no event, no start.
- `class TemporalCallbackStarter(connector, task_queue)` over `va_common.checks.start_workflow` (CALL_WORKFLOW_NAME,
  `model_dump(mode="json")`, `call.workflow_id()`, START timeouts like the api starter); raises `CallbackStartError`
  on `WorkflowStartError`; `False` (already exists) is fine. `CallbackStarter` Protocol `start(input) -> None`.
- Bound-method rule (P08-FIX-3): handlers hold the recorder object and collaborators, never bound methods of the
  runtime.

### 5. Runtime wiring (`va_media/runtime.py`)
- `CallRuntime(..., callback_starter: CallbackStarter | None = None)`; `open_runtime` builds ONE
  `TemporalConnector(settings.temporal_host, settings.temporal_namespace)` shared by the end routine's signaller and
  `TemporalCallbackStarter(connector, settings.temporal_task_queue)`.
- `_task_brain`: compute system tools first: `sms_ok = org.tools.allow_send_sms and database and await
  sms_capable(...)`; `callback_ok = (org.tools.allow_schedule_callback or org.engine.allow_callbacks) and database
  and callback_starter`; `system = [ToolSms(self._outbound_sms("media.tool_sms"), ...)] if sms_ok` +
  `[CallbackScheduler(...)] if callback_ok` (publisher = `persistence.events`, router =
  `DbWebhookRouter(database)`; skip callback when persistence is None). `CallToolSource.executor(context,
  recorder, system=system)` returns an executor when custom specs OR system tools exist (when `_call_tools` is None
  but system exists, build `CallToolExecutor((), runner=..., system=...)` — simplest: move that into a
  `CallToolSource`-independent helper or make `call_tools` always present in prod; tests via runtime_rig pass it).
  Facts: `dataclasses.replace(facts_for_call(context, org, sms_capable=sms_ok), allow_schedule_callback=callback_ok)`
  so a tool the runtime cannot run is never offered. Refactor `_sms_port` to share an `_outbound_sms(prefix)`
  builder. Saver (`EngineStateSaver`) stays tied to "executor is not None" (unchanged behaviour).
- `runtime_rig.sim_runtime(..., callback_starter=)`; update the runtime module docstring step list.

### 6. Tests
- `packages/va_domain/tests/test_call_callback.py`: offset kept; naive localized (America/New_York vs default
  LA); DST gap/fold uses fold=0; past/equal now → in_the_past; 90 d + 1 s → too_far_ahead; garbage →
  invalid_datetime; `callback_config` sets start_time, drops retry_attempt + maintenance key, note only when given,
  input unchanged.
- `packages/va_db/tests/test_callbacks_db.py` (migrated DB fixture, `call_rows.seed_call`): outbound parent → child
  copies columns, scheduled/new, parent_call_id, workflow id, config start_time; inbound parent → outbound child
  with swapped numbers; repeat child id → `created=False`, one row; parent of another org → NotFoundError.
  Retry activity tests unchanged and green.
- `packages/va_tools/tests/test_executor.py` additions: system tool handled + not in definitions; failure →
  `tool_failed("send_sms: sms_not_enabled")`, no `call.tool`; `speech()` None; executor with only system tools.
- `apps/media_worker/tests/test_call_system_tools.py` (media_db_url, `sms_rows.add_sms_number`, `sms_fakes.
  FakeTelnyx`, recording publisher/router fakes, `RecordingStarter`): sms_capable true / not capable / inactive /
  broken DB → False; ToolSms sent (message row, `sms_sent` data source tool, content), not enabled → failure,
  repeated tool call id → one Telnyx send; CallbackScheduler creates row + child `call_events` row (`event_type`
  `call.scheduled`, data parent_call_id) + starter input `scheduled_for`; start failure → child `failed`,
  `call.error` row, content callback_failed; invalid time → no row, no start; repeat → one row, one start.
- Availability (runtime level, `test_runtime_system_tools.py` or inside the sim test): FakeLLM request `tools`
  lacks `send_sms` when the number is not SMS-capable / `allow_send_sms=false`, lacks `schedule_callback` when both
  settings off, has it with `engine.allow_callbacks=true` (via `StaticOrgSettings`).
- Runtime sim test (`apps/media_worker/tests/test_runtime_system_tools_sim.py`): task call with `rig(messaging=True)`
  number in the DB (see AGENTS P05-T10e: the sim inventory is deterministic — the voicemail SMS test already
  inserts `Rig.sms_number`; give this test's org the number only after soft-deleting/clearing it, or put the case
  next to the voicemail test sharing its helper); FakeLLM rule 1 replies `tool_calls: send_sms {message}`, rule 2
  (call_index 1) content; assert FakeTelnyx `accounts[DEFAULT_API_KEY].messages` has the text to the callee, the
  2nd LLM request carries the tool message `{"status":"sent",...}`, a `call.sms_sent` event (source tool).

### 7. Spec + docs (same commit)
- specs/13 §6 `> Decision (2026-11-05) P08-T02b2:` handlers live in the media worker behind the call executor;
  availability = capability (number active + `sms` capability via `sms_sender`; 10DLC/toll-free readiness joins
  there in P14-T05) and settings, and only offered when the runtime can run them (database, Temporal starter);
  `send_sms` goes from the agent number to the other party, message id uuid5(call, tool call) (one send per tool
  call), `call.sms_sent` source `tool`, failures `call.error TOOL_FAILED` + `{"error":"sms_failed","reason"}`;
  `schedule_callback` time rules (specs/05 §3.3), child = scheduled outbound call to the caller from the agent
  number with the parent's config (`callback_note`, no `retry_attempt`/maintenance key), deterministic id, child
  `call.scheduled` (source callback) in the creating transaction, `CallWorkflow` started from the media worker;
  admission/compliance at dial like every scheduled call; start failure fails the child; invalid times answer
  `invalid_datetime` with no error event. Update the P08-T02b1 Decision's last sentence (send_sms/schedule_callback
  now executor tools; `lookup_knowledge` P08-T10). AGENTS.md learned line.

### Gates
`make fmt lint typecheck placeholders`, `make test` (background + heartbeat Monitor, ~25 min), jscpd, radon,
pip-audit/pnpm audit. No e2e flow change.

### Risks
- Importing `MAX_SCHEDULE_AHEAD` may cycle inside va_domain → move constant.
- Media publisher has no webhook router by design: route the child event explicitly with `DbWebhookRouter`.
- `call_events` partitions: DB tests need real-time clocks (SystemClock) for event rows.
- Shared media DB + deterministic FakeTelnyx SMS inventory (unique active e164) — see §6.
- Moving `COPIED_COLUMNS` must not break `va_workflows` import-free activity modules rules (retry module already
  imports va_db).

## Build P08-T02b2 (2026-11-05)
Implemented per plan. Deviations: the publisher for callback events is a separate
`EventPublisher(redis, call_events=None, router=DbWebhookRouter(database))` (`CallbackDeps`) instead of
passing a router; TaskBrain now strips each executor-backed system tool its executor does not `handles`
(`_runnable_facts`), so the runtime passes `facts_for_call(sms_capable=...)` unchanged. System tools need
`call_tools` (always present in prod). `BrokenDatabase` moved to media `support/media_helpers.py` (jscpd).
The sim SMS test releases FakeTelnyx's fixed SMS number before/after (voicemail sim test leaves it active).
Tests: va_domain test_call_callback, va_db test_callbacks_db, va_tools executor system tests, engine
`test_only_system_tools_the_executor_runs_are_offered`, media test_call_system_tools (16), runtime sim
send_sms / schedule_callback / callbacks-not-offered.

## Review P08-T02b2 (2026-11-05)
Gates re-run: lint pass, typecheck pass (mypy 1608 files), placeholders 0 findings. make test was started but
stopped after the findings below (rejection does not depend on it).
1. apps/media_worker/src/va_media/call_system_tools.py:100,142,261 — the dedupe id
   `uuid5(ns, f"{call_id}:{tool_call_id}")` trusts the provider's tool call id. `va_llm.stream_wire._PartialCall.id`
   defaults to `""` when a provider leaves out `id`, and some OpenAI-compatible servers reuse ids across rounds
   (`call_0`). Then a second, legitimate `send_sms` in the same call gets `already_queued` and the model is told
   `{"status":"sent"}` although nothing was sent. A second `schedule_callback` returns the FIRST child (at the old
   time) as "scheduled". The TaskBrain never re-runs a tool call, so this dedupe protects against nothing real in a
   live call, and it breaks a real case. Fix: make the key unique per invocation, e.g. a per-executor/per-call
   counter of system tool runs, `uuid5(ns, f"{call_id}:{tool_call_id}:{n}")`, or a fresh `new_id()` when the id is
   blank. Update the specs/13 §6 Decision items 2/3 ("a repeated tool call sends once") to match. Add tests:
   two `send_sms` runs with the same (and with blank) tool call id → two Telnyx sends; two callbacks with the same
   id and different times → two children.
2. apps/media_worker/src/va_media/turns.py:75-78 — `TOOL_SMS_SENT_MESSAGE` was inserted between
   `TOOL_FAILED_MESSAGE` and its docstring. `TOOL_FAILED_MESSAGE` has lost its doc, and a stray string expression
   ("The message of the call.error TOOL_FAILED event") now follows the new constant. Move the new constant and its
   docstring below `TOOL_FAILED_MESSAGE`'s docstring.
Everything else checked and fine: spec Decision, availability gating (`_runnable_facts`), child_call refactor of
retry, callback transaction and outbox routing, start-failure handling, org scoping in schedule_callback.

## Build P08-T02b2 fix (2026-11-05)
Review findings fixed: (1) system tool runs use fresh `new_id()` message/callback ids (no uuid5 over the
provider tool call id; `tool_call_uuid`/`TOOL_CALL_NAMESPACE` and the `already_queued` → "sent" branch removed);
tests: two send_sms / two callbacks with reused ("call_0") and blank tool call ids → two sends / two children at
their own times / two starts. Spec Decision items 2/3 and AGENTS line updated. (2) turns.py docstrings restored.
Gates: lint/typecheck/placeholders pass, make test 12941 passed 7 skipped (cov 99.04%), jscpd 0, radon avg 2.92,
pip-audit clean, pnpm audit no high.

## Review P08-T02b2 fix (2026-11-05)
Approved. Re-ran lint/typecheck/placeholders (pass) and make test (12941 passed, 7 skipped, cov 99.04%).
Fixes verified: new_id() per system tool run (call_system_tools.py:135,252), reused/blank tool call id tests,
turns.py docstrings in order, spec Decision updated.
