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

## Plan P08-FIX-13 — system tools hardening (review inbox: 2026-11-04-p08-t02b2-system-tools.md,
## 2026-11-05-p08-t02b2-system-tools-review.md, 2026-11-05-p08-t02b2-recheck.md)

### 1. Run-to-completion through barge-in (BLOCKER) — `packages/va_tools/src/va_tools/executor.py`
- `CallToolExecutor.__init__` adds `self._pending: set[asyncio.Task[SystemOutcome]] = set()`.
- `_run_system(tool, call)`: `task = asyncio.create_task(self._finish_system(tool, call), name=f"system-tool:{tool.name}")`,
  add to `_pending`, `task.add_done_callback(self._pending.discard)`; `outcome = await asyncio.shield(task)`; return
  `ToolAnswer(outcome.content)`. `_finish_system` = `await tool.run(call)` then the existing failure event + log
  (so a late outcome is still reported after the brain stopped waiting). An unexpected exception inside the task:
  log `tools.system_tool_crashed` (error_type) + `tool_failed(f"{name}: crashed")` and re-raise (the TaskBrain
  already maps an exception to TOOL_FAILED); when nobody awaits it any more, retrieve it in the done callback
  (`task.exception()` after `not task.cancelled()`) so asyncio never logs "exception never retrieved".
- `async def aclose(self, timeout_s: float = SYSTEM_DRAIN_S) -> None` (`SYSTEM_DRAIN_S: Final = 20.0`, > 10 s start +
  DB): wait for `_pending` with `asyncio.wait(..., timeout=timeout_s)` (wall time is fine here: call teardown, not a
  test-clock deadline — OR use `va_common.waits.within` with the executor's clock if it has one; the executor has
  no clock today → asyncio.wait timeout); cancel leftovers and log `tools.system_tools_abandoned` (count). Never raises.
- No change to `TaskBrain._stop_tasks`: cancelling the outer tool task now only ends the shield wait. The model still
  gets INTERRUPTED; the caps (§3) bound the retry it may make.
- Runtime `_task_brain` (`apps/media_worker/src/va_media/runtime.py`): `if executor is not None:
  stack.push_async_callback(executor.aclose)` right after building it (LIFO → runs after brain.aclose / saver
  pushed later, i.e. before them? push it BEFORE the saver so it runs AFTER the saver's flush? Order wanted: brain
  stopped → pending system tools drained → saver flush. Saver is pushed after executor creation and brain.aclose
  in the pathway branch; in `_task_brain` push `executor.aclose` before `saver.aclose` so it runs after it — either
  order is acceptable since system tools change no brain state; keep it simple: push right after building).
  P08-FIX-3 rule: push the bound method of the executor (an owned part), not a runtime method — fine.

### 2. No tools for anonymous/withheld callers (BLOCKER)
- `va_media.call_sms`: add `def dialable(number: str) -> bool: return number.startswith("+")` (same rule as
  `va_edge.accept`); export.
- `CallRuntime._system_tools`: `if not dialable(counterpart_number(context)): return False, []` (log
  `media.system_tools_skipped`, reason `no_callable_number`) before the SMS capability query. `sms_ok` False → facts
  stop offering `send_sms`; callback dropped by `_runnable_facts`.
- Guards in `ToolSms.run` / `CallbackScheduler.run`: not dialable → `SystemOutcome(_content({"error":
  "sms_failed"|"callback_failed", "reason": "no_callable_number"}), "no_callable_number")`.

### 3. Per-call caps (should fix)
- Callback: `va_db/callbacks.py`: `MAX_PENDING_CALLBACKS: Final = 1`; `class CallbackLimitError(Exception)`; after the
  parent `FOR UPDATE`, `count(Call.id) where parent_call_id == parent_id, org_id, status == "scheduled"`; `>= MAX`
  → raise. (During a live call the only children are callbacks: retry children are created after the call ended.)
  Remove the dead existing-child branch and `ScheduledCallback.created`; fix the module docstring (fresh id per run;
  cap). `CallbackScheduler.run` catches `CallbackLimitError` → `{"error":"limit_reached"}`, no failure, log
  `media.callback_refused reason=limit_reached`. Remove `created`/`event is None` handling in `_create`/`run`
  (`_create` always returns the event).
- SMS: `ToolSms` `MAX_TOOL_SMS_PER_CALL: Final = 3` (module constant in call_system_tools), counter `self._used`
  incremented BEFORE `deliver` (in-flight counts; concurrency safe because increment happens before the first
  await), decremented on `SmsNotSent` (Telnyx did not accept: queued-row/DB/profile/telnyx_error refusals). At cap →
  `{"error":"limit_reached"}`, no failure event.
- Export the constants; document caps in the spec Decision.

### 4. Start failure without orphans (should fix) — `call_system_tools.py`
- `CallbackStarter` protocol gains `async def abandon(self, call_id: uuid.UUID) -> None` (best effort, never raises);
  `TemporalCallbackStarter.abandon` → `terminate_workflow(connector, call_workflow_id(call_id) /
  CallWorkflowInput(...).workflow_id(), "callback start failed")` catching `RPCError`/`OSError`/`RuntimeError`
  (whatever `connector.client()` raises — check `start_workflow`'s except list and mirror it) → log
  `media.callback_abandon_failed`. Covers a timed-out start that reached Temporal (a live sleeper would otherwise
  sleep up to 90 days).
- `_start` on `CallbackStartError`: `await starter.abandon(child)` then `_fail_child`. `_fail_child` returns an enum-ish
  `Literal["failed","moved_on","unrecorded"]`: try the transition up to 2 attempts on `SQLAlchemyError/OSError`;
  `CallTransitionError` → "moved_on" (answer scheduled, as today); after 2 failures log at ERROR
  `media.callback_orphaned` with `child_call_id=str(child)` and answer `{"error":"callback_failed"}` failure
  `workflow_start` (the workflow was terminated, nothing will dial it; no recovery sweep exists in specs/17 — note
  in the Decision that a future sweep must not start children of callback start failures… simply: state the row
  stays `scheduled` without a workflow and is logged). Docstrings match code exactly.

### 5. Tests
- `packages/va_tools/tests/test_executor.py`: cancel the `execute` task while a gated system tool runs → tool keeps
  running, completes, `tool_failed` reported for a late failure outcome, `aclose()` waits for it; `aclose` with a
  never-finishing tool returns after the timeout (pass `timeout_s=0.05`... use a small value; it's wall time but
  tiny) and cancels it; crashing tool not awaited → no "never retrieved" warning (filterwarnings=error catches it via
  the loop exception handler? assert via `loop.set_exception_handler` recording nothing).
- `packages/va_db/tests/test_callbacks_db.py`: second callback while one is `scheduled` → `CallbackLimitError`; after
  the first is moved to `canceled`/`failed` a new one succeeds; drop the `created=False` test.
- `apps/media_worker/tests/test_call_system_tools.py`: anonymous inbound → `no_callable_number` for both; 4th SMS →
  limit_reached (3 Telnyx sends); SMS refused by Telnyx does not use up the cap; second callback → limit_reached,
  one row, one start; start failure → `abandon` called with the child id, child failed; first `_fail_child`
  transition raising (wrap database to fail the first tenant_session once) → still failed on retry; both attempts
  failing → `media.callback_orphaned` logged with child id, answer callback_failed.
- Barge-in at the brain level: `packages/va_engine/tests/` (task_helpers / FakeSource) OR media level: build a
  `TaskBrain` with a real `CallToolExecutor` holding a gated system tool (a fake SystemTool whose run awaits an
  Event then records), close the reply generator mid-tool (`first_stream`/`aclosing` pattern), release the gate,
  `await executor.aclose()` → the side effect ran exactly once. Plus a media test: `CallbackScheduler` with a gated
  `RecordingStarter` behind an executor; cancel the execute task; release → one child, one start, child still
  `scheduled` (not failed).
- Runtime: `_system_tools` with an anonymous inbound context (runtime sim `Rig.call(direction="inbound", from=
  "anonymous")` if supported, else a unit test of `_system_tools` via `sim_runtime(...)` checking the FakeLLM request
  `tools` lacks `send_sms`/`schedule_callback`) — reuse the existing "callbacks-not-offered" runtime sim test shape.

### 6. Spec + docs
- specs/13 §6 Decision P08-T02b2: item 1 add "both need the other party's number to be E.164 (an anonymous/withheld
  caller gets neither)"; new item: system tool bodies run to completion when a barge-in interrupts the turn (the model
  hears the interruption, the outcome is still reported, call end waits up to 20 s); caps (1 pending callback per
  call counted under the parent lock, 3 SMS per call sent or in flight, `{"error":"limit_reached"}`, no error
  event); start failure: terminate the workflow id, mark failed (one retry), else log `media.callback_orphaned`.
  Or as a separate `> Decision (2026-11-05) P08-FIX-13:` block — preferred (keeps history).
- AGENTS.md learned line (shielded system tools + `aclose`, `dialable`, caps).

### Gates
make fmt lint typecheck placeholders; make test in background + heartbeat Monitor; jscpd; radon; audits. No e2e change.

### Risks
- `asyncio.shield` + task GC: keep strong refs in `_pending` (done).
- Exception-never-retrieved warnings under filterwarnings=error → done-callback retrieval.
- Existing tests asserting `ScheduledCallback.created` / duplicate behaviour must be updated, not deleted silently.

## Build P08-FIX-13 (2026-11-05)
Implemented per plan. Executor: shielded system tool tasks (`_pending`, `_settled` retrieves exceptions,
`tools.system_tool_crashed` → TOOL_FAILED `crashed`), `aclose(timeout_s=SYSTEM_DRAIN_S=20)`; runtime pushes
`executor.aclose` after the saver (LIFO: drains before snapshot flush and before events drain). `dialable` in
call_sms; runtime skips both tools (`media.system_tools_skipped`), handlers guard too. Caps: ToolSms 3 (count
before await, refunded on SmsNotSent), `va_db.callbacks.MAX_PENDING_CALLBACKS=1`/`CallbackLimitError`
(`ScheduledCallback.created` + existing-child branch removed). Start failure: `CallbackStarter.abandon`
(TemporalCallbackStarter → terminate_workflow, never raises), `_fail_child` 2 attempts → "failed"/"moved_on"/
"unrecorded" (`media.callback_orphaned` error). Spec `> Decision (2026-11-05) P08-FIX-13` under specs/13 §6;
AGENTS line. Tests: va_tools test_executor (+5), test_executor_barge_in (TaskBrain closes reply mid-tool),
va_db callbacks (limit, freed slot), media test_call_system_tools (+8), test_callback_starter (7), runtime sim
anonymous → only end_call/press_button.
Gates: lint/typecheck/placeholders pass; make test 12964 passed 7 skipped (cov 99%); jscpd 0; radon avg 2.83; pip-audit clean; pnpm audit no high.

## Review P08-FIX-13 (2026-11-05)
Approved. Re-ran lint/typecheck/placeholders (pass) and make test (12964 passed, 7 skipped, cov 99.06%). All
three inbox notes' findings covered: shield + aclose (runtime LIFO before saver/events), dialable gate in runtime
and handlers, caps, abandon + 2-try _fail_child with media.callback_orphaned, dead created path removed, spec
Decision P08-FIX-13. Inbox notes appended to DONE.

## Plan P08-FIX-14 — review of P08-FIX-13 (.ralph/review-inbox/2026-11-05-p08-fix-13-review.md)

### 1. BLOCKER: heartbeat/renewer alive through the body teardown — `apps/media_worker/src/va_media/session.py`
- `_race`: keep the task list (priority order unchanged for the result). In `finally`, two stages:
  `keepers = (renewer, heartbeat)`; stage 1 cancel the not-done others (end_request, body, control_listener) and
  `await asyncio.gather(*others, return_exceptions=True)` — the body's exit stack (executor.aclose drain up to
  20 s, saver flush, persistence) runs while heartbeat SETs continue every 3 s and the lock is renewed; stage 2
  cancel + gather the keepers. If the renewer returns OWNERSHIP_LOST during stage 1 it already set `_lost`
  (cleanup skips release); the chosen `SessionEnd` stays the one from the first `done` set (no change). Use a
  try/finally inside stage 1 so stage 2 always runs even if `_race` is cancelled during stage 1.
- Update the module docstring: "The body is cancelled and awaited first, while the lock and heartbeat are still
  maintained (its teardown may drain system tools, specs/13 §6); then the renewer and heartbeat stop."
- Tests in `apps/media_worker/tests/test_session.py` (reuse its ManualClock/fakeredis helpers):
  - `test_the_heartbeat_and_lock_are_kept_while_the_body_tears_down`: body = await forever, in `except
    CancelledError`/finally awaits an `asyncio.Event` gate; `session.request_end(...)`; advance the clock past
    several HEARTBEAT_INTERVAL_S / RENEW_INTERVAL_S (`wait_for_sleep` + `run_for`) → heartbeat key value updated
    (newer timestamp) and renew count grows, ender not called yet; release gate → ender called, heartbeat deleted,
    lock released.
  - `test_a_lock_lost_during_the_body_teardown_is_not_released`: same gate; ownership.renew returns False during
    teardown → after release `session.ownership_lost` is True and `release` not called; end stays the requested one.

### 2. Callback start failure ordering — `apps/media_worker/src/va_media/call_system_tools.py`
Rationale (deviation from the note's literal suggestion, same goal): terminating first is what creates the
"terminated but answered scheduled" orphan. Only the child's workflow moves a child out of `scheduled` (or a stop
→ `canceled`), and the dial activity skips terminal calls (`TelephonyActivities._guard`). So record the failure
FIRST (guarded `scheduled` → `failed`), then terminate only a child we failed:
- `_fail_child` returns `Literal["failed", "advanced", "ended", "unrecorded"]`: `CallTransitionError` →
  `"ended"` if `is_terminal(exc.current)` (va_db.call_states) else `"advanced"` (log `media.callback_moved_on`).
- `_start` on `CallbackStartError`: `outcome = await self._fail_child(id)`; `"advanced"` → return True (the
  workflow exists and owns the child: no terminate); `"failed"`/`"unrecorded"` → `await starter.abandon(id)`
  (a late-landing or timed-out start that created a sleeper is terminated; a failed child would be skipped at
  dial anyway) and return False; `"ended"` → return False. The orphan log for "unrecorded" stays.
- `CallbackStarter.abandon(...) -> bool` (True = a workflow was terminated; never raises; False on error);
  `TemporalCallbackStarter.abandon` returns `terminate_workflow(...)`, timeout `_ABANDON_TIMEOUT_S: Final = 5.0`
  (start 10 s + abandon 5 s + two DB tries < SYSTEM_DRAIN_S 20 s); log `media.callback_abandoned terminated=`.
  Update fakes: `test_call_system_tools.RecordingStarter.abandon` returns bool (configurable) and records order
  into a shared list if needed; `test_runtime_tools_sim.py:269` returns False.
- Docstrings of `_start`/`_fail_child`/`CallbackStarter.abandon` match.
- Tests (`test_call_system_tools.py`, real media DB fixture like the existing start-failure tests):
  - existing `test_a_failed_start_fails_the_child`: assert abandon happened after the transition (starter's
    abandon reads the child's status through `load_call` → already `failed`), answer callback_failed.
  - `test_a_start_failure_after_the_workflow_moved_the_child_on_answers_scheduled`: starter.fail with a hook
    that moves the child to `queued` (CallRepo.transition inside the fake's start before raising) → answer
    scheduled, `abandoned == []`, child stays `queued`.
  - `test_a_start_failure_for_an_ended_child_answers_callback_failed`: hook moves child to `canceled` → answer
    `callback_failed`, no abandon, no extra call.error event.
  - existing orphan test: abandon still called, `media.callback_orphaned` logged.
  - `test_callback_starter.py`: `abandon` returns True when terminated, False when not found / on error.

### 3. Spec + docs
- specs/13 §6: new `> Decision (2026-11-05) P08-FIX-14:` — failure recorded before terminate; outcomes; abandon
  5 s; session keeps lock + heartbeat during body teardown (and a line in specs/07 §3 near Decision P05-T07a
  only if that section describes the race order — check; otherwise reference from specs/13).
- AGENTS.md: amend the P08-FIX-13 line (or add one): CallSession body teardown runs with heartbeat/lock alive.

### Gates
make fmt lint typecheck placeholders; make test in background + heartbeat Monitor; jscpd; radon; audits. No e2e
flow change.

### Risks
- Existing session tests that assume all tasks cancel together (e.g. ender ordering) — re-run test_session.
- ManualClock: stage-1 waits make renewer/heartbeat sleepers stay parked; tests must advance explicitly.
- A body that never finishes its teardown now also keeps the heartbeat forever (previously it did too, minus
  the heartbeat): acceptable, the executor drain is bounded.

## Build P08-FIX-14 (2026-11-05)
- session.py: `_race` → `_stop_tasks(tasks, keepers)`: others (end_request, body, control) cancelled+awaited first,
  renewer+heartbeat cancelled in a `finally` after. Docstring updated. Tests GatedTeardown (heartbeat stamps + lock
  renewed during teardown, ender waits; lock lost during teardown not released) — both fail on HEAD.
- call_system_tools: `_fail_child` → failed/advanced/ended/unrecorded (is_terminal); `_start` records first, abandons
  only for failed/unrecorded; advanced → scheduled answer. `abandon -> bool`, `_ABANDON_TIMEOUT_S` 5 s,
  `media.callback_abandoned terminated=`. Tests: status seen at abandon == failed; queued child → scheduled, no
  abandon; canceled child → callback_failed, no abandon; starter tests assert bool.
- Spec Decision P08-FIX-14 under specs/13 §6; AGENTS line.
Gates: fmt/lint/typecheck/placeholders pass; make test 12968 passed 7 skipped (cov 99.06%); jscpd 0 clones; radon avg 2.41; pip-audit clean; pnpm audit 1 moderate (no high). No e2e flow change.

## Review P08-FIX-14 (2026-11-05)
Approved. Re-ran lint/typecheck/placeholders (pass) and make test (12968 passed, 7 skipped, exit 0). Diff matches
plan + inbox note: two-stage `_stop_tasks` (keepers renewer/heartbeat stopped in finally), record-before-abandon
outcomes with is_terminal, abandon -> bool (5 s), tests for gated teardown / lost lock / advanced / ended child.
Note (non-blocking): after a lock loss during teardown the heartbeat keeps stamping until the body finishes
(≤ 20 s drain); cleanup still deletes it and the workflow learns via the end routine. Inbox note appended to DONE.

## Plan P08-FIX-15 — bound the teardown keepalive (.ralph/review-inbox/2026-11-05-p08-fix-14-review.md, blocker)

Problem: `CallSession._stop_tasks` (apps/media_worker/src/va_media/session.py) awaits the body's teardown with the
renewer + heartbeat alive and no bound: a wedged teardown = zombie call that looks alive forever (heartbeat is the
workflow's crash detector). Also the heartbeat keeps stamping after the renewer returned OWNERSHIP_LOST.

### 1. session.py
- `from va_tools.executor import SYSTEM_DRAIN_S` (media already depends on va-tools);
  `TEARDOWN_KEEPALIVE_S: Final = SYSTEM_DRAIN_S + 5.0` (export in `__all__`).
- `_stop_tasks` becomes an instance method (needs `self._clock`, call id for the log):
  ```python
  others = [...]
  for t in others: if not t.done(): t.cancel()
  teardown = asyncio.gather(*others, return_exceptions=True)
  try:
      deadline = asyncio.ensure_future(self._clock.sleep(TEARDOWN_KEEPALIVE_S))
      try:
          await asyncio.wait({teardown, deadline}, return_when=FIRST_COMPLETED)
      finally:
          deadline.cancel(); await suppress-cancelled(deadline)
      if not teardown.done():
          _log.error("media.body_teardown_overran", keepalive_s=TEARDOWN_KEEPALIVE_S)
  finally:
      cancel + gather keepers
  await teardown   # keep waiting for the body (unbounded, as before FIX-14); keepers are stopped meanwhile
  ```
  Do NOT use `va_common.waits.within` (it cancels the awaitable: a second cancel would abort the body's drain).
  If `_stop_tasks` itself is cancelled while waiting, the `finally` still stops keepers; the gather stays
  cancelled-through as the outer cancellation propagates (`asyncio.wait` does not cancel teardown; on outer
  cancel, also `teardown.cancel()` in an `except CancelledError: raise` path — keep simple: wrap final
  `await teardown` normally; on CancelledError during wait, cancel teardown then re-raise).
- Heartbeat stops with the renewer: in `_race`, after creating tasks:
  `renewer.add_done_callback(functools.partial(_cancel_task, heartbeat))` with a module function
  `def _cancel_task(task, _done): task.cancel()` (no bound method of self: P08-FIX-3 cycle rule). Since the
  renewer only finishes on loss (or keeper cancel), this stops stamping at once on loss in race or teardown.
  The race result is unaffected: the renewer is first in priority, and a cancelled heartbeat is never in
  `done` before the renewer (callback fires after renewer completes; `asyncio.wait` returns both — `finished`
  loop: check `task.exception()` on a cancelled heartbeat would RAISE CancelledError!). Guard: in `_race`,
  skip cancelled tasks: `finished = [t for t in tasks if t in done and not t.cancelled()]`. Verify renewer
  stays first.
- Docstring: "…for at most TEARDOWN_KEEPALIVE_S (25 s; the system-tool drain is 20 s); past it
  `media.body_teardown_overran` is logged, the renewer and the heartbeat stop (the workflow's heartbeat watch
  then ends the call) and the session keeps waiting for the body. The heartbeat also stops as soon as the
  ownership is lost."

### 2. Tests (apps/media_worker/tests/test_session.py, ManualClock rig + GatedTeardown)
- `test_a_wedged_teardown_stops_the_keepalive_after_the_bound`: request_end, wait `body.tearing`; run_for
  in steps up to TEARDOWN_KEEPALIVE_S (use `wait_for_sleep` before advancing as other tests do) → stamp ==
  latest heartbeat before the bound; then run_for(HEARTBEAT_TTL_S + 3 more) → heartbeat stamp unchanged
  (fakeredis TTL is wall-clock, so assert the stamp stopped changing + `pttl` ≤ HEARTBEAT_TTL_S*1000 rather
  than expiry), owner key not renewed (set pexpire short before and check it's not extended / pttl decreasing);
  `media.body_teardown_overran` logged (json_log_stream/log_events); `running` not done; gate.set() → run
  completes, cleanup deletes heartbeat, releases lock (not lost), ender ran once.
- `test_a_lock_lost_during_the_teardown_stops_the_heartbeat`: request_end, tearing; read stamp; set owner to
  "pod-other"; run_for(5) (renewal at 5 s fails) → `ownership_lost`; record stamp; run_for(9) → stamp unchanged
  (no beat after loss); gate.set() → never released, heartbeat deleted.
- `test_a_lock_lost_during_the_race_ends_worker_lost` existing (`test_lock_taken_by_another_pod...`) must still
  pass (heartbeat cancelled via callback must not break the result) — add assert result is OWNERSHIP_LOST there
  if not already.
- Existing GatedTeardown keepalive test: still within 12 s < 25 s bound → passes unchanged.

### 3. Spec + docs
- specs/13 §6: `> Decision (2026-11-05) P08-FIX-15:` the teardown keepalive is bounded by
  `TEARDOWN_KEEPALIVE_S` = drain 20 s + 5 s on the session clock; overrun → error log, keepers stop, the heartbeat
  key expires (10 s TTL) and the workflow's heartbeat watch ends the call `worker_crash`; the session keeps
  awaiting the body before the end routine/cleanup; heartbeat stops as soon as the renewer reports the lock lost.
- AGENTS.md: amend the P08-FIX-14 line with the bound + `media.body_teardown_overran`.

### Gates
make fmt lint typecheck placeholders; make test in background with heartbeat Monitor; jscpd; radon; audits.
No e2e flow change.

### Risks
- `task.exception()` on a cancelled task raises — the `not t.cancelled()` filter is required.
- ManualClock: the deadline sleeper is one more parked sleeper; tests using `wait_for_sleep(3.0)` may match it?
  It is 25 s, distinct; fine.

## Build P08-FIX-15 (2026-11-05)
- session.py: `TEARDOWN_KEEPALIVE_S = SYSTEM_DRAIN_S + 5` (25 s); `_stop_tasks` (instance) cancels others, waits on
  `asyncio.wait({teardown, deadline})` via `_await_bounded` (never cancels the teardown), logs
  `media.body_teardown_overran`, stops keepers in finally, then still awaits the body; cancellation of `_stop_tasks`
  cancels the teardown. Heartbeat cancelled by a module-level done callback on the renewer (no bound method);
  `_race` skips cancelled tasks. Docstring updated.
- Tests: `test_a_wedged_teardown_stops_the_keepalive_after_the_bound` (new) and the existing lock-lost-during-teardown
  test now asserts no beat after the loss (merged to keep jscpd 0). Both fail on HEAD.
- Spec `> Decision (2026-11-05) P08-FIX-15` under specs/13 §6; AGENTS line.
Gates: fmt/lint/typecheck/placeholders pass; make test 12969 passed 7 skipped (cov 99.06%, exit 0); jscpd 0 clones;
radon session.py avg A (3.0); pip-audit clean; pnpm audit 1 moderate (no high). No e2e flow change.

## Review P08-FIX-15 (2026-11-05)
Approved. Re-ran lint/typecheck/placeholders (pass) and make test (12969 passed, 7 skipped, cov 99.06%, exit 0).
Diff matches the plan and inbox note: bounded keepalive (`_await_bounded` never cancels the teardown), overrun log,
keepers stopped in finally, body still awaited; heartbeat cancelled on renewer completion; cancelled tasks filtered
in `_race`. Reviewer tightened the wedged-teardown test: `pttl(owner) <= 60_000` was vacuous (a renewal sets 15 s,
which also satisfies it) → `> OWNER_TTL_MS`. Inbox note appended to DONE.

## Plan P08-T02c1 — pathway engine tool rounds on reply nodes (split from P08-T02c; T02c2 = media wiring)

All inbox notes are in DONE. Split recorded in plan/phase-08.md (parent T02c removed, T02c1/T02c2 added).

### 0. Scope decisions (record as `> Decision (2026-11-05) P08-T02c1:` under specs/13 §4, in the same commit)
1. Reply nodes = `Default` and `Knowledge Base` (global nodes included) whose reply streams from the dialogue LLM
   (no static `text`). They offer: node `tools` ∪ `global_config.globalTools` (custom, resolved through the
   executor, node first, de-duplicated by name) + system tools allowed by `SystemToolFacts` (runner `pathway`:
   `end_call` only when `globalConfig.allowEndCallTool` (default true), never `transfer_call`, `press_button` on
   voice, `send_sms`/`schedule_callback` only when the executor runs them). Other node types offer no
   custom/system tools; Scheduling keeps exactly `list_slots`/`book_slot` (13 §6 "Scheduling node only").
2. Call-level `tools` (Send Call) apply to task mode only (13 §4 "call tools (task mode)").
3. Without an executor (text chat, unit tests, simulations — `EnginePorts.tools is None`) no custom tools; without
   `ConversationFacts.system_tools` (None) no system tools: text runs are unchanged by this task.
4. Tool loop per pass at a node: same caps as task mode (`MAX_TOOL_ROUNDS` 3 rounds, `MAX_INVALID_ROUNDS` 2, 4 calls
   per round, 100 per call via `plan_round(budget=)`), then the node's reply is requested without tools. Answers
   are `tool` history records (context updates, like Scheduling; call ids/arguments not persisted); a reply cut
   by a barge-in records unanswered calls `{"error":"interrupted"}`. Speech/filler line as task mode (first
   executed tool's rendered `speech`, else a filler after 1.2 s, `engine.tool_fillers=false` silences both) —
   spoken as a `Say` and recorded as an assistant history line. Tool variables merge into `state.variables`.
5. `end_call`: final line = `final_message`, else `"Goodbye."` unless the pass already spoke text this round;
   `state.ended=True`, `end_reason="agent_end_call"`, yields `Say(final)` then
   `EndCallAction(reason="agent_end_call", ended_by="agent", after_mark=<final's mark or None>)` (the brain maps it
   as every EndCallAction; runtime maps to `agent_end_call`). `press_button` → `SendDtmfAction(digits=...)` after
   the answers are recorded.
6. Tools of another pathway entered by Transfer Pathway resolve only if the executor knows them (T02c2 loads the
   pinned graph's tools); unknown refs are skipped (`engine.tool_unresolved` info log with the count).

### 1. Shared tool-task helpers — new `packages/va_engine/src/va_engine/tool_tasks.py`
Move out of `task_runner.py` (TaskBrain then delegates; behaviour identical, keeps jscpd 0):
- `def call_arguments(call: ToolCall) -> dict[str, object]` (was `_arguments`).
- `def runnable_facts(facts: SystemToolFacts, executor: ToolExecutor | None) -> SystemToolFacts` (was
  `_runnable_facts`).
- `async def execute_gated(executor, call, scope, gate) -> ToolAnswer` (log event name stays `task_tool_failed`?
  → rename to `tool_failed` is a behaviour change; keep `task_tool_failed` only if a test asserts it — grep; else use
  `engine.tool_failed` and update tests).
- `async def stop_tasks(tasks)`.
- `def start_tools(executor, planned: Sequence[PlannedCall], scope) -> dict[str, asyncio.Task[ToolAnswer]]`
  (semaphore EXECUTOR_CONCURRENCY; scope copied with `variables=dict(scope.variables)`).
- `class ToolLines` (per conversation; no back-reference to the owner — P08-FIX-3 cycle rule): `__init__(executor,
  *, clock, enabled: bool)`, `async def line(planned, tasks, scope) -> str | None` (speech of the first executed
  call rendered with `dataclasses.replace(scope, input={"input": call_arguments(call)})`, `TemplateSyntaxError` →
  log `tool_speech_invalid`, None; else filler `TOOL_FILLERS[n % len]` round-robin when tasks still run after
  `FILLER_DELAY_S` on the clock), `still_running` internal.
- `def merge_variables(variables: dict, planned, results) -> bool` (changed?).
TaskBrain: replace the methods with these (keep `_run_tools` flow and its logs). All existing task tests must pass
unmodified except log-name updates if any.

### 2. Executor protocol — `va_engine/tool_executor.py`
- `type ToolRef = str | Mapping[str, JsonValue]` (a node/global `tools` entry).
- `class PathwayToolExecutor(ToolExecutor, Protocol)`: `def definitions_for(self, refs: Sequence[ToolRef]) ->
  tuple[ToolDef, ...]` — the executor's tools named by `refs` in ref order (str = `TL-<uuid>`/bare uuid matched to a
  stored tool id; mapping = inline tool by its `name`), unknown refs skipped, duplicates once. (Implemented by
  `CallToolExecutor` in T02c2.)
- `GlobalConfig.allow_end_call_tool: bool = True` (`va_engine/models/graph.py`; camel `allowEndCallTool`); add
  `"allowEndCallTool": true,` to the specs/11 §3.4 JSON block (verify `test_graph.py` / validation tests still
  pass; lossless dump uses exclude_unset so Bland fixtures unaffected).

### 3. Engine — `va_engine/engine.py` (+ new `va_engine/engine_node_tools.py` to keep engine.py < ~900 lines and
radon low)
- `EnginePorts.tools: PathwayToolExecutor | None = None`; `ConversationFacts.system_tools: SystemToolFacts | None =
  None`; `ConversationFacts.tool_fillers: bool = True`.
- `engine_node_tools.py`:
  - `REPLY_NODE_TYPES = frozenset({"Default", "Knowledge Base"})`.
  - `def node_tool_refs(node, graph) -> list[ToolRef]` = `node.data.tools` + `graph.global_config.global_tools`.
  - `class NodeTools` (built once per engine: `executor`, `facts` = `runnable_facts(facts, executor)` or None):
    `offered(node, graph) -> tuple[ToolDef, ...]` (system tools first then custom, custom names that are reserved
    or already offered skipped), `handles(name)`, `facts`.
  - `async def run_node_round(...)`-style helper (or a small class `NodeToolRound`) that, given the planned round
    (`plan_round(calls, facts, handles=executor.handles, budget=)`), starts tasks, yields the line, collects
    results, merges variables, records answers through a callback, yields `SendDtmfAction`s, and returns the
    terminal planned call; `finally` stops tasks and records unanswered as INTERRUPTED. Keep it an async generator
    of `Action`s with an out-param box for the terminal (pattern of `_relayed`'s `box`).
- `_prepare(node, suffix)`: Scheduling → scheduling tools (unchanged); reply node with `NodeTools` → `offered(...)`;
  also set `self._dialogue.tool_speech = executor.tool_speech or DEFAULT_TOOL_SPEECH` (check attr name in
  dialogue.py: `tool_speech`).
- `_respond`: generalize the loop: after the reply, `if node.type == "Scheduling"`: existing `_tool_round`; elif
  custom/system tools were offered and the reply was a `SayStream` with tool calls: run the node round (yield its
  actions via `_out` so `Say`s are remembered and terminal actions set `run.done`); end_call → yields final Say +
  EndCallAction (state.ended/end_reason set) and returns; counters per pass: rounds, invalid rounds; after limits
  `self._dialogue.tools = ()`; call-wide `self._tool_calls_used` (engine attribute; 100 cap).
  "Spoke this round" for the Goodbye rule = the reply stream produced text (the `_streamed` collected parts; reuse
  the recorded assistant record of this round — simplest: track via `_remember` count in the round).
- Tool answer records: `TurnRecord(role="tool", content, node_id, tool_name, tool_call_id, at_ms)` (same as
  `_tool_round`; extract a `_record_tool(node_id, call_id, name, content)` helper used by both).
- Scope for tools: `TemplateScope(variables=state.variables, request_data=facts.request_data,
  builtins=ctx builtins, timezone=facts.timezone)` — reuse how handlers build scope (`handlers/base.py` has a scope
  builder from `TurnContext`; use it).
- After variables merge, nothing else (PathwayBrain publishes state after each relay).

### 4. Tests
- `packages/va_engine/tests/test_tool_tasks.py`: ToolLines speech render / invalid template / filler round-robin /
  disabled / fast tools no filler; start_tools concurrency ≤ 3; execute_gated exception → TOOL_FAILED; runnable_facts.
  (Move the equivalents from task tests only if they would duplicate; TaskBrain tests stay.)
- `packages/va_engine/tests/test_engine_tools.py` (engine_rig gains `tools: RecordingExecutor | None`,
  `system: SystemToolFacts | None`, `fillers: bool`; `RecordingExecutor` gains `refs: dict[str, str]` (ref → name)
  and `definitions_for(refs)`):
  - `test_a_node_tool_is_offered_and_its_answer_feeds_the_next_reply`: Default node with `tools: ["TL-…"]`; FakeSource
    reply 1 = tool call `lookup` ; reply 2 = text; asserts request 1 tools names contain `lookup`, executor called,
    a `tool` history record with the content, request 2 contains the context update, spoken text = reply 2.
  - `test_global_tools_are_offered_on_every_reply_node` (two nodes, global tool on both; not on a Webhook/End Call
    node; Scheduling node offers only list_slots/book_slot).
  - `test_end_call_tool_is_offered_only_when_allowed` (allowEndCallTool false → not in tools, a returned end_call is
    answered unknown_tool invalid; true → offered).
  - `test_end_call_speaks_the_final_message_and_ends`: final_message → Say + EndCallAction(reason agent_end_call),
    state.ended; without final_message and reply text → no Goodbye; without text → "Goodbye.".
  - `test_press_button_sends_dtmf` → SendDtmfAction digits, answer `{"status":"sent",...}`.
  - `test_tool_speech_is_spoken_while_the_tool_runs` (hold executor; Say(speech) yielded before release) and filler
    when no speech (FrozenClock → instant filler while held) and `tool_fillers=False` silences.
  - `test_tool_variables_merge_into_the_state`.
  - `test_the_fourth_request_of_a_pass_offers_no_tools` (3 tool rounds → 4th request tools == ()).
  - `test_a_barge_in_mid_tool_records_interrupted` (close the generator while held → record `{"error":"interrupted"}`,
    task cancelled).
  - `test_without_an_executor_or_facts_no_tools_are_offered` (existing rigs: request tools == ()).
  - `test_transfer_call_is_never_offered_on_pathways` (facts with transfer list → absent).
- `test_graph.py`: `allowEndCallTool` parses, default true, round-trips via dump only when set.
- PathwayBrain: one test in `test_pathway_brain.py` that an engine end_call EndCallAction and SendDtmfAction map to
  EndCall / PressButton (existing mapping; add only if not covered).

### 5. Spec + docs
- specs/11 §3.4 block `allowEndCallTool`; specs/13 §4 Decision P08-T02c1 (items of §0); update specs/11 §5.9
  Decision P07-T08b rule 7's "Other nodes offer no tools until phase 08" → add a line in the new Decision that
  it is superseded.
- AGENTS.md line: `va_engine.tool_tasks` shared helpers, `EnginePorts.tools`/`ConversationFacts.system_tools`,
  `engine_rig(tools=, system=)`.

### Gates
make fmt lint typecheck placeholders; make test (background + heartbeat Monitor; ~25 min); jscpd 0; radon;
pip-audit; pnpm audit. No e2e flow change (media unchanged until T02c2).

### Risks
- Refactor of TaskBrain: keep log event names asserted by tests (grep `task_tool_failed`, `task_tool_speech_invalid`).
- Speculative stream opened at `_prepare` time now carries tools: an adopted speculative stream may return tool
  calls → handled by the same round (dialogue.last is the adopted stream).
- FakeSource scripts consumed by speculation (see AGENTS P07-T08b note): script a throwaway reply or build the rig
  with `speculative=False` for tool tests.
- engine.py size/complexity: put the round logic in `engine_node_tools.py`.

## Build P08-T02c1 (2026-11-05)
- New `va_engine.tool_tasks` (call_arguments, runnable_facts, execute_gated, stop_tasks, start_tools, merge_variables,
  ToolLines); TaskBrain delegates to it (behaviour unchanged; log names now `tool_failed`/`tool_speech_invalid`).
- `tool_executor.ToolRef` + `PathwayToolExecutor.definitions_for(refs)`; `GlobalConfig.allow_end_call_tool` (+ specs/11
  §3.4 block line).
- New `va_engine.engine_node_tools`: REPLY_NODE_TYPES, NO_SYSTEM_TOOLS, ToolOffer(tools, facts, custom, handles),
  NodeTools.offer (system first, runner pathway, allowEndCallTool from the active graph; custom via definitions_for,
  reserved/dup dropped, `engine.tool_unresolved` count), run_node_round (line, answers, DTMF after answers,
  interrupted in finally).
- Engine: EnginePorts.tools, ConversationFacts.system_tools/tool_fillers; `_respond` generalized (`_reply_calls`,
  `_scheduling_round`, `_node_round`, `_tool_end`, `_record_tool`, `_tools_exhausted`, `_ToolBudget`); `_prepare` sets the
  offer. end_call reason `agent_end_call` (Decision notes the §6 table's `agent_end_call_tool` is not used).
- Tests: test_engine_tools.py (21), test_graph allowEndCallTool; engine_rig(tools=, system=, fillers=),
  RecordingExecutor.refs/definitions_for.
- Spec Decision P08-T02c1 under specs/13 §4; specs/11 §5.9 rule 7 marked superseded; AGENTS line.
Gates: fmt/lint/typecheck/placeholders pass; make test 12991 passed 7 skipped (cov 99.06%, exit 0); jscpd 0 clones; radon avg A (3.37); pip-audit clean; pnpm audit 1 moderate (no high). No media/e2e change (T02c2 wires media).

## Review P08-T02c1 (2026-11-05)
Rejected: 4 findings (1 blocker). Reviewer gates: `make lint`, `make typecheck`, `make placeholders` pass;
`check_openapi_drift` current; jscpd 0 clones; radon worst C (12) `PathwayEngine._respond`; `uv run pytest
packages/va_engine` 1109 passed (engine_node_tools 100 %, engine.py 98 %, tool_tasks 95 %). The reviewer's full
`make test` was stopped at 38 % (no failure up to there) once the rejection was certain, so the full suite was NOT
re-verified this round. `logs/make_test.log` now ends with `exit=2` from that killed run: truncate it before the
next run.

1. BLOCKER `packages/va_engine/src/va_engine/engine.py` `_reply_calls` (`return await last.tool_calls()`): the real
   `va_llm.dialogue.DialogueStream.tool_calls()` raises `LlmUnavailableError` after an unavailable stream
   (`_raise_failure`; proven by `packages/va_llm/tests/test_dialogue_stream.py::
   test_the_fallback_has_its_own_ttft_deadline`). Before this task a Default/KB node returned early (no tools);
   now every reply node that offers tools (every voice pathway call once T02c2 passes facts, `press_button` is
   always offered on voice) speaks the `errorPhrase` and then raises out of `start`/`handle_user_turn` (the
   pipeline logs `pipeline_failed`) instead of ending the turn as specs/11 §5.9 Decision P07-T08b requires.
   Repro (prints `RAISED LlmUnavailableError` with system facts, the normal `[stream, say errorPhrase]` without):
   `uv run python /var/tmp/review_p08t02c1/repro_unavailable.py`. The tests cannot see it because
   `dialogue_helpers.FakeStream.tool_calls` never raises. Required: (a) no tool round when
   `self._dialogue.unavailable is not None` (turn ends exactly like the no-tools case, nothing raised);
   (b) make `FakeStream.tool_calls` mirror production (raise `fail` when it is an `LlmUnavailableError`);
   (c) tests in `test_engine_tools.py`: unavailable reply on a node with tools + system facts, and on a
   Scheduling node (same function), asserting the actions and that no `tool` record exists.
2. Should fix, same function: the calls of a cut-off stream (`last.error` set; the real stream returns the named
   calls with partial `arguments_json`, `test_dialogue_stream.py` lines 350-362) are planned and executed. Task mode
   drops them (`TaskBrain._round_calls`, `task_llm_stream_failed`) and Decision P08-T02c1 item 3 says the rounds
   follow task mode. Required: ignore the calls when `last.error is not None` (warning log), give `FakeStream` a
   way to script `error` together with `calls`, add a test; or keep it and say so in the Decision with the reason.
3. Dead code `packages/va_engine/src/va_engine/engine_node_tools.py:143,166`: `RoundOutcome.variables_changed` is
   written and never read (engine and tests). Remove the field and its docstring mention (call `merge_variables`
   for its effect), or use it.
4. Missing tests: (a) the call-wide cap of 100 on pathways (`_prepare` `spent`, `_tools_exhausted` third
   condition, `plan_round(budget=)` in `_node_round`) has no behaviour test: add one (e.g. monkeypatch
   `va_engine.engine.MAX_TOOL_CALLS_PER_CALL` to 2 and assert `too_many_tool_calls` answers, then no tools
   offered on the next pass); (b) the planned `test_tool_tasks.py` was not written and `tool_tasks.py` lines 49-50
   (non-JSON arguments → `{}` for the speech scope) and 174 are uncovered: add the direct tests.

Checked and fine: offer rules (system first, `allowEndCallTool`, never `transfer_call`, reserved/duplicate custom
names dropped, Scheduling unchanged), `end_call` reason `agent_end_call` (specs/13 §6 Decision 2026-11-02),
`press_button` after answers, interrupted answers in `finally`, no engine back-reference from `NodeTools`/
`ToolLines`, TaskBrain refactor behaviour-identical, spec Decision + specs/11 §3.4/§5.9 edits, AGENTS line, plan split.

## Build P08-T02c1 fix (2026-11-05)
Review findings 1-4 addressed:
1. BLOCKER fixed: `PathwayEngine._reply_calls` returns no calls when `self._dialogue.unavailable is not None` (the turn
   ends after the `errorPhrase` exactly like a node without tools; nothing raised). `dialogue_helpers.FakeStream`
   now mirrors production: `fail=LlmUnavailableError` sets `error` and `tool_calls()` raises it. New test
   `test_an_unavailable_model_ends_the_turn_with_the_error_phrase[reply-node|scheduling]` (tools + system facts).
   Reviewer repro `/var/tmp/review_p08t02c1/repro_unavailable.py` now prints the normal actions for both cases.
2. Cut-off stream: `_reply_calls` drops the calls when `last.error is not None` (warning `engine.llm_stream_failed`);
   `FakeStream(error=)` / `FakeSource.reply(..., error=)` script it; test
   `test_the_calls_of_a_cut_off_reply_are_not_run`. Verified: with both guards removed these 3 tests fail.
3. `RoundOutcome.variables_changed` removed (merge_variables called for its effect).
4. Tests: `test_the_call_wide_cap_answers_the_excess_and_then_offers_no_tools` (monkeypatched cap 2: third call
   `too_many_tool_calls`, same pass and next pass offer no tools); new `packages/va_engine/tests/test_tool_tasks.py`
   (15 tests: call_arguments incl. non-JSON, runnable_facts, execute_gated failure, start_tools gate + frozen
   variables, merge order, stop_tasks, ToolLines speech/invalid/blank → filler, disabled, round-robin on ManualClock,
   fast tools no filler, no executor) → tool_tasks.py 100 %. Extra: `test_a_static_node_never_runs_the_calls_of_an_
   earlier_stream` covers the SayStream guard (engine.py line was uncovered).
Spec: Decision P08-T02c1 item 5 (specs/13 §4) — failed/cut-off reply runs no tool round (also Scheduling). AGENTS line.
Gates (this iteration): make fmt/lint/typecheck/placeholders exit 0; make test 13015 passed, 7 skipped, coverage
99.07 %, exit=0 (logs/make_test.log); va_engine 1131+ passed (tool_tasks 100 %, engine_node_tools 100 %); jscpd 0
clones; radon average A (2.42), worst in touched files C (12) `PathwayEngine._respond` (unchanged); pip-audit no known
vulnerabilities; pnpm audit 1 moderate, no high. No media/e2e change (T02c2 wires media), so make e2e not run.

## Review P08-T02c1 fix (2026-11-05)
Approved. All 4 findings of the previous review are fixed and verified:
1. `PathwayEngine._reply_calls` returns no calls when `dialogue.unavailable` is set (reply node and Scheduling); the
   earlier repro `/var/tmp/review_p08t02c1/repro_unavailable.py` now prints the normal `[stream, say errorPhrase]`
   for both cases. `FakeStream` mirrors `va_llm.dialogue.DialogueStream` (`error` set, `tool_calls()` raises).
2. Calls of a cut-off stream (`last.error`) are dropped with `engine.llm_stream_failed` (same rule as
   `TaskBrain._round_calls`; `error` is set only for post-token truncation or unavailability, a successful
   fallback leaves it `None`).
   Mutation check: a pytest plugin restoring the pre-fix `_reply_calls`
   (`/var/tmp/review_p08t02c1_fix/mutant_reply_calls.py`) fails exactly the 3 new tests, 23 pass.
3. `RoundOutcome.variables_changed` is gone.
4. Call-wide cap test and `test_tool_tasks.py` (15 tests) exist; `tool_tasks.py` and `engine_node_tools.py` 100 %,
   `engine.py` 98 %.
Also checked: `_speculate` calls `_prepare` first, so a speculative request carries the node's tool offer; spec
Decision item 5 matches the code; no leftover `task_tool_failed`/`task_tool_speech_invalid` references.
Gates (this iteration): make lint / typecheck / placeholders exit 0; check_spec_refs 0 problems; openapi current;
jscpd 0 clones; radon worst C (12) `PathwayEngine._respond`, C (11) `TaskBrain._run_tools`.
make test run 1: 1 failed, 13014 passed, 7 skipped — `apps/sim/tests/test_sim_shared_redis.py::
test_two_sim_processes_share_scenarios_and_reset` (`(0, 1) == (0, 0)`: a sim child exited 1). Not caused by this
diff (only va_engine + docs changed; the sim does not import va_engine), passes 6/6 alone, already seen once in
P08-FIX-7. Filed task `fix:sim-shared-redis-exit-flake` (capture the child's stderr, then fix the cause) and a
fix memory. make test run 2: 13015 passed, 7 skipped, coverage 99.07 %, exit=0 (logs/make_test.log; run 1 kept
as logs/make_test_review_run1.log). make e2e not run: no media/call flow change (T02c2 wires media).

## Planner (2026-11-05, after P08-T02c1 approval): review inbox → P08-FIX-16/17/18 before P08-T02c2
Inbox notes not in DONE: `2026-11-05-p08-t02c1-recheck.md` (replaces `2026-11-05-p08-t02c1-engine-node-tools-review.md`;
item 1 BLOCKER, must land before T02c2; its "earlier item 2" `_reply_calls` is already fixed in 01ffa44) and
`2026-11-05-p08-fix-15-review.md` (should fix). Inserted at the top of plan/phase-08.md:
- P08-FIX-16 = recheck items 1 + 2 (this plan). P08-FIX-17 = recheck items 3 + 4 (superseded turn keeps tool
  records; inline tool name rule) → both t02c1 notes go to DONE when FIX-17 is approved. P08-FIX-18 = fix-15 note
  (lock lost / overrun during the body teardown → worker_lost).

## Plan P08-FIX-16 — pathway `end_call` always ends the call; tool tasks keyed by position

### Findings (verified by reading HEAD 01ffa44)
- `engine.py` `_tool_end` (~l.621): sets `state.ended = True`, then `Say(text=final, mark=...)` with
  `interruptible=None` → `PathwayBrain._output` makes it `SpeakStatic(interruptible=True)`. Barge-in on the goodbye
  closes the generator before `EndCallAction`; later `handle_user_turn` returns at `engine.turn_after_end` → nobody
  ends the call. `_protection` (~l.723) has the same weakness with the `errorPhrase` (`_error_line`).
- Extra hole found while planning (same strand, also the End Call node and a non-interruptible line): the pipeline
  cancels a reply that has produced NO audio yet when a caller turn ends (`pipeline._route_locked`: `running` and
  not `speaker.reply_audio_started` → `cancel_generation` + `_cancel_reply`, merged turn with `supersedes`).
  `interruptible=False` does not stop that. `EndCallHandler.respond` / `_tool_end` / `_protection` set
  `state.ended` before their line is yielded, so a caller turn ending between the end decision and the goodbye's
  first audio closes the generator, and the merged turn then hits `turn_after_end`. Task mode is safe (its
  `EndCall(final_text=)` is one output; the pipeline sets `_closing` on receipt). Fix in the engine: remember the
  end that is owed and deliver it on the next turn.
- `tool_tasks.start_tools` returns `{call.id: task}`; `va_llm.stream_wire._PartialCall.id` defaults to `""`: two
  id-less calls → first task dropped from the dict (runs on, never awaited/cancelled), both answered with the
  second result; the `answered` id sets skip the second `interrupted`.

### 1. `packages/va_engine/src/va_engine/engine.py`
a. `_tool_end`: `line = Say(text=final, mark=ctx.next_mark(), interruptible=False) if final else None` (or
   `line_actions(ctx, final or None)` from `va_engine.handlers.base`, whichever keeps the code shortest).
b. `_protection`: `error = self._error_line(...)`; when not None `error = replace(error, interruptible=False)`
   (only here; `_error_line` itself stays interruptible for the unavailable-model line in `_streamed`).
c. Owed end. New attribute `self._owed_end: tuple[Action, ...] = ()` ("the final line + `EndCallAction` of an
   ended conversation that the consumer has not received yet").
   - `_out(run, actions)`: materialize `queued = tuple(actions)` (check every caller passes a finite iterable;
     they pass tuples/lists). Before the loop: `if self.state.ended and any(isinstance(a, EndCallAction) for a in
     queued): self._owed_end = tuple(a for a in queued if isinstance(a, Say | EndCallAction))`. In the loop, right
     BEFORE `yield action` of an `EndCallAction`: `self._owed_end = ()` (the consumer ends the call on receipt and
     closes the generator at that yield, so clearing after the yield would never run).
     Keep `_out` complexity low: put the two bits in tiny helpers (`_owe(queued)`, and the clear inline).
   - `handle_user_turn`: replace the `if self.state.ended:` early return by: log `engine.turn_after_end` (keep,
     add `owed=bool(self._owed_end)`); `for action in self._owed_end:` → clear `_owed_end` before yielding the
     `EndCallAction`; yield each action as is (no `_out`: the line is already in the history, nothing is
     re-recorded; the superseding turn is NOT accepted/recorded — the conversation is over, `turn_tags("user")`
     already returns None); return. A second cancellation during the re-spoken line leaves it owed again.
   - `on_wait_timeout` / `start`: unchanged (the wait is disarmed at every end; a pre-audio merge always delivers
     a turn next).
   - Docstring of the class/module: one sentence on the rule.
d. Nothing else in the engine changes; `_ToolBudget`, caps, `_reply_calls` untouched.

### 2. By-position tool rounds
`packages/va_engine/src/va_engine/tool_tasks.py`:
- `start_tools(executor, planned, scope) -> list[asyncio.Task[ToolAnswer]]`: one task per item of `planned`, same
  order (`[]` without executor or calls). Docstring: "by position, never by call id (providers omit or reuse
  ids)".
- `merge_variables(variables, answers: Iterable[ToolAnswer]) -> bool`: merges `answer.variables` in the given
  (call) order.
- `ToolLines.line(planned, tasks: Sequence[asyncio.Task[ToolAnswer]], scope)`; `_still_running(tasks)` unchanged
  in substance.
- New pure helper (both runners use it, keeps jscpd at 0):
  `def round_contents(plan: RoundPlan, answers: Sequence[ToolAnswer]) -> list[tuple[PlannedCall, str]]` — every
  call of `plan.calls` in order with its tool-message content: `kind == "execute"` items take the next answer
  (`zip`-like iterator over `answers`, `strict`: `len(answers) == len(plan.executed)` else `ValueError`), others
  their precomputed `item.answer`; blank → `TOOL_FAILED`. Export in `__all__`.
`packages/va_engine/src/va_engine/engine_node_tools.py`:
- `RoundOutcome.answered: int = 0` (number of calls of `plan.calls` answered so far, they are answered in order).
- `run_node_round`: `tasks = start_tools(...)`; `line = await tools.lines.line(plan.executed, tasks, scope)`;
  `answers = [await task for task in tasks]`; `merge_variables(variables, answers)`; `for item, content in
  round_contents(plan, answers): answer(item.call, content); outcome.answered += 1; presses…`; `finally:
  await stop_tasks(tasks)`; `for item in plan.calls[outcome.answered:]: answer(item.call, INTERRUPTED);
  outcome.answered += 1`.
`packages/va_engine/src/va_engine/task_runner.py` `_run_tools`: same shape with a local `answered = 0` counter
  (drop the `answered: set[str]` parameter of `_answer`); `_merge_variables(answers)`.
No change to `plan_round`, the executor protocol, or what is stored (`tool_call_id` stays the model's id as data).

### 3. Tests
`packages/va_engine/tests/test_engine_tools.py`:
- update `test_end_call_speaks_its_final_line_and_ends`: expected say tuple `("say", line, "t0.2", False)`.
- `test_the_end_call_line_is_not_interruptible_and_an_undelivered_end_is_delivered_next_turn`: one_node +
  `system=VOICE`, reply = tool-only `end_call`; iterate `rig.engine.start()` by hand until the `Say`
  ("Goodbye.", `interruptible is False`), `aclose()` the generator (state ended, no end yielded); then
  `handle_user_turn(user("wait", 1, supersedes=...))` (also without `supersedes`) yields exactly
  `[Say("Goodbye.", same mark, interruptible=False), EndCallAction(agent_end_call, after_mark=mark)]`, no new LLM
  request, history unchanged (no user record added); a further turn yields `[]`.
- `test_a_delivered_end_is_not_repeated`: full `rig.start()` (end yielded), next turn yields `[]`.
`packages/va_engine/tests/test_engine_turns.py` (or the file holding the End Call / loop-protection engine tests;
grep `max_total_turns` / `"End Call"`):
- `test_an_end_call_node_closed_on_its_final_line_ends_on_the_next_turn` (route to an End Call node with
  `endCall.speech`; close at the line; next turn → line + `EndCallAction(reason="agent_end_call")`).
- `test_the_loop_protection_line_is_not_interruptible` (max_total_turns → `Say(errorPhrase, interruptible=False)`
  + `EndCallAction(loop_protection)`), and that the unavailable-model `errorPhrase` line keeps
  `interruptible is None` (assert in the existing unavailable test or a new one).
`packages/va_engine/tests/test_pathway_brain_pipeline.py`:
- `test_speaking_over_the_end_call_goodbye_still_ends_the_call`: `engine_rig(one-node Default graph with a
  prompt, system=VOICE voice facts, speculative off if needed)`, `rig.source.reply("", calls=[end_call with
  final_message=LONG_LINE])`; `harness(config=CONFIG, engine=brain_of(rig))`; wait until the goodbye plays
  (`h.until(lambda: h.transport/speaker playing…)` as `test_a_barge_in_truncates_the_engine_history` does), then
  `await h.caller.say(1500)` (≥ 900 ms = barge-in at the default level); `result.end_reason ==
  PATHWAY_END_REASON`; last agent turn `(text_spoken, interrupted) == (LONG_LINE, False)`;
  `rig.state.end_reason == "agent_end_call"`; no `transport.clear` in `h.transport.calls`. Must fail on HEAD
  (verify once by reverting 1a locally).
- optional if the harness can end a caller turn before the goodbye's first audio (`hanging`/TTS first-audio
  delay): pre-audio merge still ends `agent_hangup`; the engine-level tests above are the required proof.
`packages/va_engine/tests/test_tool_tasks.py`: adapt to lists (`start_tools(...) == []`, `merge_variables(vars,
  answers)`); new `test_calls_without_ids_each_get_a_task` (two `ToolCall(id="", …)` to different tools: two
  tasks, both results in order, `stop_tasks` cancels both when held); `test_round_contents_pairs_by_position`
  (execute/press/invalid mix, two id-less executes → own contents; blank → TOOL_FAILED; wrong answers length →
  ValueError).
`test_engine_tools.py`: `test_two_calls_without_ids_get_their_own_answers` (tool records `[("lookup", A),
  ("other", B)]`, both executed) and `test_a_barge_in_cancels_and_answers_every_id_less_call` (held executor,
  close the generator: `executor.cancelled` has both, two `{"error":"interrupted"}` records).
`packages/va_engine/tests/test_task_runner.py` (or test_task_tools*.py where the tool-round brain tests live):
  the same two cases for `TaskBrain` (messages: two `tool` messages with their own content).

### 4. Spec + docs (same commit)
- specs/13 §4: new `> Decision (2026-11-05) P08-FIX-16:` after Decision P08-T02c1: (1) the `end_call` final line
  and the loop-protection `errorPhrase` are spoken non-interruptibly (like the End Call node line and task mode's
  final line); (2) an ended pathway conversation owes its `EndCallAction`: when the reply was closed before the
  consumer received it (pre-audio merge, specs/07 §8), the next caller turn is not answered and yields the
  remaining final line + `EndCallAction` (tool `end_call`, End Call node, loop protection); (3) tool rounds key
  tasks, answers, variable merges and `interrupted` answers by the call's position in the round, never by the
  model's call id (may be empty or repeated). Reference it from specs/11 §5.9 with one line (write `specs/13 §4`
  on the same line so check_spec_refs pairs it).
- AGENTS.md "Learned" bullet `2026-11-05 (P08-FIX-16)`: `start_tools` returns a list by position /
  `round_contents`; `PathwayEngine` owed end; final lines non-interruptible; tests close the generator on the
  line to simulate the pre-audio merge.
- plan/phase-08.md: tick is the Reviewer's job. Do NOT touch `.ralph/review-inbox/DONE` (FIX-17 closes the notes).

### Gates
`make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine -q` first; then `make test` in the
background (`: > logs/make_test.log` first, `make test > logs/make_test.log 2>&1; echo exit=$? >>
logs/make_test.log`, heartbeat Monitor every ~2 min; PATH needs `/home/ralph/.local/bin`); `npx --yes jscpd@4
--silent` (0 clones); `uvx radon cc -s -a` on touched files; `uvx pip-audit` on `uv export`; `corepack pnpm
audit`. No media/e2e flow change (pathway tools are not wired into the media runtime until T02c2) → no `make e2e`.

### Risks
- `_out` is on every action path: keep the owed-end bookkeeping O(1) and only active when `state.ended`; radon
  for `_out` must stay ≤ B. `tuple(actions)` must not consume a lazy generator that depends on interleaving —
  check callers (`_tool_end` tuple, handler `result.actions` tuple, `_protection` tuple, `(produced_action,)`,
  `queued`/`rest` lists).
- Wait-timeout end (`wait_for_response._end`) has no line: owed tuple = just the `EndCallAction`, cleared before
  its yield → no behaviour change.
- Known flake: `apps/sim/tests/test_sim_shared_redis.py::test_two_sim_processes_share_scenarios_and_reset`
  (task fix:sim-shared-redis-exit-flake) — unrelated; if it fails, re-run that test alone and report both runs.
- TaskBrain refactor must keep messages pairing (`assert_tool_pairing`) and the existing barge-in test green.

## Build P08-FIX-16 (2026-11-05)
Implemented the plan as written (no scope change).
Source:
- `engine.py`: `_tool_end` builds its line with `handlers.base.line_actions` (`Say(interruptible=False)`);
  `_protection` makes the `errorPhrase` line non-interruptible (`_error_line` itself unchanged, so the
  unavailable-model line stays `interruptible=None`); owed end = `self._owed_end` set by `_owe(queued)` at the top
  of `_out` (only when `state.ended` and the batch holds an `EndCallAction`), cleared right before the
  `EndCallAction` is yielded; `handle_user_turn` on an ended state logs `engine.turn_after_end` with `owed=` and
  yields the owed line + end (no `_drop_turn`, no `_accept`, no request). `_out` now returns before `_owe` when the
  run is already over, so a batch arriving after a terminal action can never leave a second end owed.
- `tool_tasks.py`: `start_tools` → list by position; `merge_variables(variables, answers)`; new
  `round_contents(plan, answers)` (ValueError on a wrong answer count; blank → `tool_failed`); `ToolLines.line`
  takes a sequence of tasks. `engine_node_tools.run_node_round` and `TaskBrain._run_tools` count answered calls
  (`plan.calls[answered:]` → `interrupted`); `RoundOutcome.answered` is an int.
Tests (all new ones fail without the change; mutation: making the `end_call` line interruptible again fails the
pipeline test + 5 engine tests):
- `test_engine_tools.py`: final line tuple `False`; closed-on-line then superseding / plain next turn → line + end,
  later turn `[]`, history and request count unchanged, log `owed` `[True, False]`; closed again stays owed;
  delivered end not repeated; two id-less calls get their own answers/variables; barge-in cancels + answers both.
- `test_engine_turns.py`: loop-protection line `False` (max_visits, max_total_turns); loop-protection end closed
  on its line is delivered next turn; End Call node closed on its line is delivered next turn (no new decision).
- `test_pathway_brain_pipeline.py::test_speaking_over_the_end_call_goodbye_still_ends_the_call` (1.5 s of caller
  speech over the long goodbye: `agent_hangup`, line fully spoken, no interrupt, no `transport.clear`).
- `test_task_tool_speech.py`: TaskBrain two id-less calls (own answers; barge-in cancels both, two `interrupted`).
- `test_tool_tasks.py`: lists; id-less calls each get a task and are both cancelled; `round_contents` by position
  and its ValueError. New helper `engine_helpers.closed_on_line`.
Spec: specs/13 §4 Decision P08-FIX-16 (3 items) + specs/11 §5.9 Decision P07-T08b item 10. AGENTS.md bullet.
Gotcha found: an `end_call` tool call without `reason` is answered `invalid_arguments` (nothing ends).
Gates (this iteration, final source state): make fmt / lint / typecheck / placeholders exit 0; check_spec_refs 0
problems; va_engine 1148 passed (tool_tasks.py 100 %, engine_node_tools.py 100 %, engine.py 98 %, same 6 uncovered
lines as before the change); make test 13030 passed, 7 skipped, coverage 99.07 %, exit=0 (logs/make_test.log; a
first run was stopped on purpose because `_out` was tightened while it ran); jscpd 0 clones; radon overall
average A (2.93), touched functions ≤ B (8), worst in touched files C (12) `PathwayEngine._respond` (unchanged);
pip-audit (CI command) no known vulnerabilities; pnpm audit --audit-level high exit 0 (1 moderate). No media/e2e
flow change (pathway tools reach the media runtime in P08-T02c2), so make e2e was not run. The sim shared-Redis
flake (task fix:sim-shared-redis-exit-flake) did not occur in this run.

## Review P08-FIX-16 (2026-11-05) — APPROVED
Re-ran (this iteration): make lint / typecheck / placeholders exit 0; check_spec_refs 0 problems; jscpd 0 clones;
radon: only `PathwayEngine._respond` C (12), unchanged; make test 13030 passed, 7 skipped, coverage 99.07 %, exit=0.
Read the whole diff against .ralph/review-inbox/2026-11-05-p08-t02c1-recheck.md items 1 + 2 and Decision P08-FIX-16:
- Item 1: `_tool_end` uses `line_actions` (non-interruptible), `_protection` makes only its own `errorPhrase`
  non-interruptible. Owed end: every place that sets `state.ended` (end_call tool, End Call node, loop protection,
  wait timeout) hands its batch to `_out` with no await in between, so `_owe` always sees it; the debt is cleared
  right before the `EndCallAction` yield. The pipeline's pre-audio merge always delivers the merged turn
  (`_route_locked`), so the owed end is always collected.
- Reviewer probe (temporary test, deleted): real pipeline harness with `ttfa_s=3.0`, caller turn ends before the
  goodbye's first audio → `pipeline_turn_merged supersedes=1` → `engine.turn_after_end owed=True` → goodbye spoken
  once in full → `agent_hangup`.
- Item 2: tasks/answers/variables/interrupted by position in both runners; `round_contents` ValueError is an
  invariant only (executed calls exist only with an executor: `ToolOffer.handles`, `TaskBrain._handles`,
  `runnable_facts`).
No findings. Inbox notes stay out of DONE until P08-FIX-17 (items 3, 4).
