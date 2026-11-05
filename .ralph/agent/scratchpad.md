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

## Planner (2026-11-05, after P08-FIX-16 approval)
Inbox: no new notes (open ones: t02c1-recheck + t02c1-engine-node-tools-review → FIX-17; fix-15-review → FIX-18).
Next unticked task: P08-FIX-17. Plan entry rewritten with the decided design (DEC-012 in decisions.md); P08-T02c2
deliverable now names `for_refs`.

## Plan P08-FIX-17 — a superseded turn keeps its tool answers; a node runs exactly the tools its refs name
Notes: .ralph/review-inbox/2026-11-05-p08-t02c1-recheck.md items 3, 4 (+ engine-node-tools-review item 4).

### Findings (read at HEAD 9ecbbdf)
- Pre-audio merge (va_voice pipeline `_route_locked`): a caller turn ending while the reply has produced no audio
  cancels the reply (generator closed → `stop_tasks` cancels unfinished custom tools, answered `interrupted`) and
  delivers the merged turn with `supersedes`. `PathwayEngine._drop_turn` (engine.py ~960) deletes every record
  since the superseded user record, `tool` records included; `TaskBrain._drop_turn` (task_runner.py ~488) deletes
  `self._messages[position:]` and the records. Side effects + merged variables stay → next request has no trace.
- Pathway history renders `tool` records as `[Context update] Tool <name> returned: <content>` system messages
  (`prompt._history_message`); `handle_user_turn` needs the caller's record LAST (`history = state.history[:-1]`).
- TaskBrain messages are provider-shaped: `assistant(tool_calls)` then one `tool` message per call
  (`_record_reply`, `_answer`); the window (`va_llm.window.split_turns`) groups at `user` messages.
- `PathwayToolExecutor.definitions_for(refs)` + `execute(call, scope)` by `call.name`: one definition per name per
  call, so node B's same-name tool would run node A's definition once P08-T02c2 builds the executor.

### 1. Pathway: keep the superseded turn's tool records — `packages/va_engine/src/va_engine/engine.py`
`_drop_turn(index)`: after `mark`/`logbook.drop_turn(turn_index)` (unchanged):
```python
history = self.state.history
dropped = min(self._records_added - added, len(history))
kept = [record for record in history[-dropped:] if record.role == "tool"] if dropped > 0 else []
if dropped > 0:
    history[-dropped:] = kept
self._records_added = added + len(kept)
self._spoken = None
```
Every `tool` record stays (results, `invalid_arguments`, `interrupted`, Scheduling `list_slots`/`book_slot`
answers), in order, with its own `node_id`/`at_ms`; user/assistant records of the turn go as before; the logbook
drop is unchanged (it holds no tool records). Docstring: say why (the tool ran; Decision P08-FIX-17). Nothing
else in the engine changes for item 3 (`_accept` then records the merged turn behind the kept records).

### 2. Task mode: carry the completed tool rounds — `task_tools.py` + `task_runner.py`
`packages/va_engine/src/va_engine/task_tools.py` (pure, export in `__all__`):
```python
def completed_rounds(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The completed tool rounds in ``messages``: each assistant message with tool calls (without its text:
    nobody heard it) followed by its ``tool`` answers; a round lacking an answer for every call, and every
    other message, is left out."""
```
(walk the list; for an assistant message with `tool_calls` collect the directly following `tool` messages; keep
`message.model_copy(update={"content": None})` + answers only when `len(answers) == len(message.tool_calls)`.)
`task_runner.py`:
- new attribute `self._carried: list[ChatMessage] = []`.
- `_drop_turn(index)`: `self._carried = completed_rounds(self._messages[position:])` before `del
  self._messages[position:]`; records: same rule as the engine (`kept = [r for r in tail if r.role == "tool"]`,
  `history[-dropped:] = kept`, `self._records_added = added + len(kept)`; keep the existing guard
  `0 < dropped <= len(history)`); `_spoken` reset unchanged. Docstring updated.
- `_accept(turn)`: after `self._append(message, at_ms=…)` add `self._messages.extend(self._carried);
  self._carried = []` (messages only: their `tool` records already sit in the history ahead of the merged user
  record; `_marks[turn.index]` is taken before the user message, so a second supersede finds user + carried
  rounds in its range and carries them again). `start(greeting)` also goes through `_accept` (nothing carried
  there).
Result for the next request: `…, user(merged), assistant(content=None, tool_calls=[…]), tool, …` → the standard
tool-round shape; `assert_tool_pairing` holds; no assistant message follows an assistant message.

### 3. Ref-based resolution — `tool_executor.py`, `tool_tasks.py`, `engine_node_tools.py`, `engine.py`, `task_runner.py`
`packages/va_engine/src/va_engine/tool_executor.py`:
```python
class PathwayToolExecutor(Protocol):   # no longer extends ToolExecutor
    """The tools of a pathway call, resolved per node (specs/13 §4, Decision P08-FIX-17)."""
    @property
    def tool_speech(self) -> str | None: ...      # the tool usage note's speech (unchanged meaning)
    def handles(self, name: str) -> bool: ...     # executor-backed SYSTEM tools the call runs (runnable_facts)
    def for_refs(self, refs: Sequence[ToolRef]) -> ToolExecutor:
        """The executor of one node: ``definitions()`` are the tools ``refs`` name, in ref order (a string =
        that stored tool, a mapping = that inline tool; unresolved refs skipped; of several refs with one tool
        name the first wins); ``handles``/``speech``/``execute`` answer for exactly those definitions plus the
        call's system tools."""
```
Module docstring updated. `ToolExecutor` unchanged (task mode).
`tool_tasks.py`:
- `runnable_facts(facts, executor: ToolExecutor | PathwayToolExecutor | None)` (only `handles` is used).
- `ToolLines.__init__(self, *, clock, enabled)`; `line(self, executor: ToolExecutor | None, planned, tasks,
  scope)`; `_speech(executor, planned, scope)`. (The node's executor differs per reply, so it is an argument.)
`task_runner.py`: `ToolLines(clock=clock, enabled=settings.engine.tool_fillers)`;
`self._lines.line(self._executor, plan.executed, tasks, scope)`.
`engine_node_tools.py`:
- `ToolOffer` gains `executor: ToolExecutor | None = None` ("runs the offer's custom and executor-backed system
  tools").
- `NodeTools.__init__`: `self.lines = ToolLines(clock=clock, enabled=fillers)`.
- `NodeTools.offer`: `scoped = None if self.executor is None else self.executor.for_refs(node_tool_refs(node,
  graph))` (also with no refs: system tools run through it); `_custom(node, scoped, refs, taken)` reads
  `scoped.definitions()`; keep the reserved/taken filter and the `engine.tool_unresolved` log (count = distinct
  ref keys − definitions: refs that produced no definition, i.e. unresolved or shadowed by an earlier ref of the
  same name; say so in the docstring); return `ToolOffer(..., executor=scoped)`.
- `run_node_round(plan, *, executor: ToolExecutor | None, lines: ToolLines, scope, variables, next_mark, answer,
  outcome)` (replaces `tools: NodeTools`): `start_tools(executor, …)`, `lines.line(executor, …)`.
- module docstring: one sentence on per-node resolution.
`engine.py` `_node_round`: `run_node_round(plan, executor=offer.executor, lines=self._tools.lines, …)`.
`engine.__init__` keeps `ports.tools.tool_speech`. No validation rule, no specs/11 §11 change.
GC rule (AGENTS P08-FIX-3): the offer/view hold the call's executor, never the engine → no cycle.

### 4. Tests
Fake — `packages/va_engine/tests/support/task_helpers.py` `RecordingExecutor`: replace `definitions_for` by
`for_refs(refs)` returning a small node view (same module, e.g. `NodeExecutor` dataclass: parent, resolved
`tools`, `labels: dict[name, label]`) with `definitions()`, `tool_speech`, `handles(name)` (`name in labels or
parent.handles(name)`), `speech(name)` (parent), `execute(call, scope)` (appends `(labels.get(call.name,
call.name), call.name)` to a new parent list `ran`, then delegates to `parent.execute`). Resolution: a string ref
through `parent.refs` → the `parent.tools` entry (label = the ref); a mapping with `description` or `url` defines
its own `ToolDef(name, description, parameters={"type": "object"})` (label = its `url`, else its name); a bare
`{"name": …}` mapping resolves by name as today; first ref per name wins. Existing constructions
(`executor(refs=…)`, TaskBrain uses) keep working.
`packages/va_engine/tests/test_engine_tools.py`:
- `test_a_superseded_turn_keeps_its_tool_records`: `started`/start with "Hello.", turn 1 `reply("",
  calls=[call("c1","lookup",city="Paris")]).reply("It opens at nine.")` driven fully, then
  `handle_user_turn(user("paris and lyon", 2, supersedes=1))` with `reply("Both open at nine.")`: history ==
  `[assistant Hello., tool lookup <result>, user "paris and lyon", assistant "Both open at nine."]` (roles +
  contents, no turn-1 user/assistant record); the last request's messages contain `[Context update] Tool lookup
  returned: <result>` positioned before the `user` message "paris and lyon"; `[c.name for c in tools.calls] ==
  ["lookup"]`; `rig.state.variables` still hold the tool's variables (`variables={"lookup": {"hours": "9-5"}}`).
- `test_a_superseded_turn_keeps_an_interrupted_answer`: `drive_held(rig, tools, close_on_line=True)` (filler
  line, tool cancelled) then a superseding turn: `tool_records(rig) == [("lookup", '{"error":"interrupted"}')]`.
- `test_a_superseded_scheduling_turn_keeps_its_booking_answer` only if cheap with the existing
  `scheduling_node()` helper + `FakeHttp` (else skip: the code path is the same `_record_tool`).
- `test_two_nodes_run_their_own_tool_of_one_name`: two Default nodes (`start("a", tools=[{name: lookup,
  description: "A", url: "https://a.hooks.test/x"}])`, node `b` with description "B"/url b, edge a→b; route via
  `KeyedPort`/rig routing helper as other two-node tool-less tests do): reply at `a` calls `lookup` →
  `tools.ran == [("https://a.hooks.test/x", "lookup")]`, offered description "A"; after the route the reply at
  `b` offers description "B" and its call appends `("https://b.hooks.test/x", "lookup")`.
- `test_the_first_ref_of_a_name_is_the_tool_that_runs`: one node `tools=["TL-1", {name: lookup, description:
  "inline", url: …}]`: offered once (LOOKUP's description), `tools.ran == [("TL-1", "lookup")]`; reversed order
  → the inline one. Update `test_custom_tools_named_like_system_tools_or_twice_are_skipped` to the new
  `NodeTools`/offer API if its construction changes.
- NodeTools-level: `offer(...).executor` is `None` without an executor; with an executor and no refs it still
  runs an executor-backed system tool (`test_executor_backed_system_tools_run_through_the_executor` stays green).
`packages/va_engine/tests/test_engine_turns.py::test_superseding_turn_replaces_the_earlier_one` stays as is.
`packages/va_engine/tests/test_task_tools.py`: `completed_rounds` cases: text + calls → content None kept with
answers; two rounds; a round missing an answer dropped; plain user/assistant messages dropped; empty input.
`packages/va_engine/tests/test_task_tool_speech.py` (FakeSource brain rig; or test_task_runner.py with FakeLLM
rules if simpler):
- `test_a_superseded_turn_carries_its_tool_round_behind_the_merged_message`: turn 1 = tool-only reply + tool
  result + second reply, then `on_user_turn(user_turn(2, "order A7 and B2", supersedes=1))`: last request's
  messages (after system/notes) end `user "order A7 and B2"`, `assistant(tool_calls=[lookup_order], content
  None)`, `tool <result>`; `assert_tool_pairing`; no "order A7" user message from turn 1; `brain.state.history`
  roles end `tool, user, assistant`; executor ran once.
- `test_a_superseded_interrupted_round_is_carried_too` (held executor, generator closed before any line with
  fillers off or at the filler; answers `interrupted`; carried round has both messages).
- `test_a_second_supersede_carries_the_round_again` (turn 3 supersedes 2: round still present exactly once).
`packages/va_engine/tests/test_tool_tasks.py`: adapt `ToolLines(clock=…, enabled=…)` / `line(executor, …)`.
Mutation check to report: restoring `del history[-dropped:]` (engine) / dropping `_carried` (TaskBrain) fails the
new tests; resolving by name across nodes (fake ignoring refs) fails the two-node test.

### 5. Spec + docs (same commit)
- specs/13 §4: new `> Decision (2026-11-05) P08-FIX-17:` after Decision P08-FIX-16:
  1. Superseded turn (specs/07 §8 pre-audio merge): tools that ran stay on record. Pathways: the turn's `tool`
     history records stay, ahead of the merged caller turn (context updates on the next request); its user and
     assistant records go as before. Task mode: its completed tool rounds (assistant `tool_calls` message
     without text + `tool` answers) are re-appended behind the merged caller message, `tool` records stay; a
     round without an answer for every call is dropped. Answers `interrupted` are kept like any other.
     Variables and `call.tool` events were never withdrawn.
  2. Per-node resolution: `PathwayToolExecutor.for_refs(refs) -> ToolExecutor` (replaces `definitions_for`);
     a node offers, speaks for and executes exactly the definitions its `tools` + `globalTools` refs name; of
     several refs with one tool name the first wins (node tools before global tools), so the definition shown
     to the model is the one that runs, and different tools of one name on different nodes (inline or stored)
     never mix; no §11 validation rule. Node executors share the call's runner, events and system tool runs
     (one drain at call end) — binding for P08-T02c2.
  In Decision P08-T02c1 item 1 mark the `definitions_for` clause "(superseded by Decision P08-FIX-17 item 2)".
- specs/13 §5 Decision P08-T02b1: add item 5 pointing to `specs/13 §4` Decision P08-FIX-17 item 1 (same line).
- specs/11 §5.9 Decision P07-T08b (line ~671 "a turn with `supersedes` removes the superseded user record and
  everything after it"): append "except its `tool` records (specs/13 §4 Decision P08-FIX-17)" on one line.
- AGENTS.md bullet `2026-11-05 (P08-FIX-17)`: `for_refs` + `ToolOffer.executor` + `ToolLines.line(executor, …)`;
  superseded turns keep tool records / `completed_rounds` + `_carried`; fake `RecordingExecutor.for_refs`/`ran`.
- Do NOT touch `.ralph/review-inbox/DONE` (Reviewer appends both note names on approval); plan tick = Reviewer.

### Gates
PATH needs `/home/ralph/.local/bin`. `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
-q`; `uv run python scripts/check_spec_refs.py`; `make test` in the background (`: > logs/make_test.log`, then
`make test > logs/make_test.log 2>&1; echo exit=$? >> logs/make_test.log`, heartbeat Monitor ~2 min);
`npx --yes jscpd@4 --silent` (0 clones: the two `_drop_turn` record blocks are near-identical — if jscpd flags
them, move the shared part into `va_engine.models.state` as `keep_tool_records(history, dropped) -> int`);
`uvx radon cc -s -a` on touched files; `uvx pip-audit` on `uv export`; `corepack pnpm audit`. No media/e2e flow
changes (pathway tools reach the media runtime in P08-T02c2; task-mode supersede is covered by va_engine tests
and nothing in apps/ implements `definitions_for`) → no `make e2e`.

### Risks
- Anything else implementing/using `definitions_for` or `ToolLines(executor, …)`: grep found only the engine,
  `task_helpers.RecordingExecutor` and specs/plan text; re-grep apps/ and packages/ before finishing.
- `RecordingExecutor` must still satisfy `ToolExecutor` (TaskBrain tests) and now `PathwayToolExecutor`
  (`engine_rig(tools=)`): mypy checks both structurally.
- TaskBrain: `_carried` must be cleared in `_accept` only; a `_drop_turn` whose mark is unknown leaves it empty.
  A carried round sits inside the new turn's `_marks` range (mark taken before the user message) — covered by
  the second-supersede test.
- Window trimming: carried rounds belong to the current (never dropped) turn; fine.
- Known flake (task fix:sim-shared-redis-exit-flake): `apps/sim/tests/test_sim_shared_redis.py::
  test_two_sim_processes_share_scenarios_and_reset`; if it fails, re-run alone and report both runs.

## Build P08-FIX-17 (2026-11-05)
Implemented the plan; two deviations, both written into specs/13 §4 Decision P08-FIX-17.

What changed
- `va_engine.tool_executor`: `PathwayToolExecutor` no longer extends `ToolExecutor`; it has `tool_speech`,
  `handles(name)` and `for_refs(refs) -> ToolExecutor` (replaces `definitions_for`).
- `va_engine.tool_tasks`: `ToolLines(clock=, enabled=)`, `line(executor, planned, tasks, scope)` (speech lookup is
  a module function); `runnable_facts` accepts either executor kind.
- `va_engine.engine_node_tools`: `ToolOffer.executor` (the node's own), `NodeTools.offer` calls
  `executor.for_refs(node refs)` also with no refs (system tools), `run_node_round(plan, executor=, lines=, ...)`.
- `va_engine.engine`: `_node_round` passes `offer.executor`; `_drop_turn` keeps the turn's `tool` records.
- `va_engine.models.state.keep_tool_records(history, count) -> kept` shared by engine and TaskBrain (the two
  `_drop_turn` blocks would otherwise be a jscpd clone).
- `va_engine.task_tools.completed_rounds(messages)`; `TaskBrain._carried` set in `_drop_turn`, appended behind the
  merged user message in `_accept`.

Deviations from the plan
1. TaskBrain `_drop_turn` uses the shared clamp (`min(count, len(history))`) instead of the old
   `0 < dropped <= len(history)` guard. They differ only if one turn added more records than the history holds
   (cap 200; a turn adds at most a few dozen), where the clamp is the engine's established behaviour.
2. Not in the plan, found while implementing: a task-mode `transfer_call` records `{"status":"transferring"}`
   before its message is spoken and the `TransferRequest` is yielded. The pipeline merges a caller turn into any
   running reply without audio (`_route_locked`: `running and not reply_audio_started`), so a reply can be closed
   on the transfer message; before this task the whole turn was dropped, with carrying the model would have been
   told "transferring" for a transfer that never happened. `TaskBrain._transfer` now rewrites that answer
   (message + record) to `interrupted` when the request was not handed over
   (`test_a_transfer_closed_on_its_message_is_carried_as_interrupted`; mutation: without the rewrite it fails).
   `end_call`/`transfer_call` cut earlier in the round were already answered `interrupted`.

Not done / residual
- Scheduling variant of the superseded-turn test skipped (plan: only if cheap; same `_record_tool` path).
- Pathway `run_node_round` records every `press_button` answer `sent` before yielding the `SendDtmfAction`s
  (order from P08-T02c1). With two presses in one round and a reply closed at the first, the second is on
  record as sent though it never went out; that record now survives a supersede. Not changed here (needs its
  own decision on answer/yield order); worth a planner look.
- `.ralph/review-inbox/DONE` and the plan tick untouched (Reviewer).

Tests added
- test_engine_tools.py: superseded turn keeps tool records (result, interrupted, second supersede once);
  two nodes run their own inline `lookup`; first ref of a name wins (stored-first / inline-first, shadowed ref
  logged); node tool beats a global tool of the same name; `ToolOffer.executor` with/without an executor.
- test_task_tool_speech.py: carried round behind the merged message (request shape + pairing + history +
  variables), second supersede, interrupted round, turn without rounds, transfer closed on its message,
  transfer handed over.
- test_task_tools.py: `completed_rounds` (text stripped, two rounds, incomplete round, plain messages, empty).
- test_state.py: `keep_tool_records` (0, negative, tail, mixed, clamp).
- Fake: `task_helpers.RecordingExecutor.for_refs` → `NodeExecutor`; `executor.ran` = (label, name).

Mutation checks (each run on packages/va_engine, then restored)
- keep no tool records → 8 failed (3 engine, 2 state, 3 TaskBrain).
- TaskBrain does not append `_carried` → 3 failed.
- NodeTools reuses the first node's executor → 2 failed (two-node test, global-tools offer test).
- transfer answer not rewritten → 1 failed.

Gates (final code)
- make fmt: exit 0. make lint: exit 0. make typecheck: exit 0 (mypy: no issues in 1614 files).
- make placeholders: 0 findings. check_spec_refs: 0 problems. check_openapi_drift: both current.
- make test: 13052 passed, 7 skipped (live suite), coverage 99.08 % (touched files 98-100 %), 830 s. The known
  sim shared-Redis flake did not fire. An earlier run was killed by me on purpose (to add deviation 2), its
  `exit=2` is not a test result.
- make test-ts: exit 0 (widget 22, dashboard 52, sdk 48 passed). No TS change.
- jscpd: 0 clones. radon: average 3.09 on touched files, nothing at C or worse.
- pip-audit --strict on `uv export` (CI form): no known vulnerabilities.
- pnpm audit: `--audit-level high` (the CI gate) exit 0; plain `pnpm audit` exit 1 with 1 moderate advisory
  (minimatch <11.0.1 via typescript-eslint 8.72.0, dev-only, lockfile untouched since 2026-10-25, unrelated to
  this task) → task fix:pnpm-audit-minimatch opened.
- make e2e not run: no media/e2e flow changed (pathway tools reach the media runtime in P08-T02c2; nothing in
  apps/ implements the changed protocol).

## Review P08-FIX-17 (2026-11-05) — APPROVED
Re-run by the reviewer on the final tree: make lint / typecheck / placeholders exit 0; check_spec_refs 0;
jscpd 0 clones; pip-audit (CI form) clean; pnpm audit --audit-level high exit 0 (1 moderate, task
fix:pnpm-audit-minimatch); make test exit 0: 13052 passed, 7 skipped, coverage 99.08 % (touched files 98-100 %).
radon: average 3.09; `PathwayEngine._respond` is C (12), unchanged from HEAD (the builder's "nothing at C" was
inaccurate, not a regression).

Read against specs/13 §4 Decision P08-FIX-17, specs/11 §5.9/§6 notes, plan deliverables and the pipeline
(`_route_locked` → `_cancel_reply` waits for the reply task, `_reply` closes the Brain generator in `finally`, so
`interrupted` answers and the transfer rewrite are in place before `_drop_turn`; `_end_call` sets `_closing` at
once, so a task-mode `EndCall` handed over can never be superseded).
- Mark arithmetic holds across a second supersede (engine: kept tool records sit before the new turn's mark;
  TaskBrain: carried rounds sit behind the merged user message, inside the new mark, and are picked up again once).
- `placed` index stays valid: nothing between `_answer` and the `_transfer` finally removes messages.
- `ToolOffer.executor` is the same node executor for the offer and the round; no engine reference (no cycle).
Mutation checks (packages/va_engine, each restored byte-identical afterwards): keep no tool records → 9 failed;
transfer answer not rewritten → 1 failed; carried rounds not appended → 4 failed; node executor built from no
refs → 20 failed.
Reviewer probe (/var/tmp/review_fix17/test_adhoc.py, not in the repo): transfer closed on its message, then a
merged turn whose reply calls `transfer_call` again → carried answer `interrupted`, the second call yields a
`TransferRequest` and is answered `transferring`, pairing valid.

Both notes of this task appended to `.ralph/review-inbox/DONE` (t02c1-recheck, t02c1-engine-node-tools-review).

For the Planner: `.ralph/review-inbox/2026-11-05-p08-fix-17-review.md` (maintainer review of this work tree,
written 12:38Z, NOT in DONE) still needs its FIX task.
- Its item 1 (blocker: carried round says "transferring" for a transfer never requested) is fixed in this commit
  (`TaskBrain._transfer`/`_interrupt_answer`, test `test_a_transfer_closed_on_its_message_is_carried_as_interrupted`,
  Decision text names the case). Still owed from that item: the regression assertion that a second
  `transfer_call` in the merged turn yields a `TransferRequest` (behaviour verified by the probe above, no repo
  test asserts it).
- Its item 2 (not a blocker) is open: pathway `run_node_round` records every `press_button` answer `sent`
  before yielding the `SendDtmfAction`s; with two presses in one round and the reply closed at the first, the
  second stays on record as sent, and that record now survives a supersede. Fix as the note says (answer and
  yield per press, like TaskBrain) and state the order in the Decision.

## Planner (2026-11-05, after P08-FIX-17 approval)
Inbox: two notes not in DONE. `2026-11-05-p08-fix-15-review.md` already has its task (P08-FIX-18, unticked).
`2026-11-05-p08-fix-17-review.md` had none → inserted **P08-FIX-19** at the top of plan/phase-08.md (inbox rule:
new FIX tasks go to the top; the note is marked blocker, although its blocker item 1 was already fixed in
75e109d). FIX-19 is planned now; FIX-18 (session worker_lost, independent, media only) is next, then P08-T02c2.

## Plan P08-FIX-19 — a pathway `press_button` is answered `sent` only when its DTMF was handed over; owed transfer regression test

### Findings (read at HEAD 75e109d)
- `packages/va_engine/src/va_engine/engine_node_tools.py` `run_node_round` (lines 186-207): answers EVERY call of
  the round first (collecting `presses`), sets `outcome.terminal`, and only then yields one `SendDtmfAction` per
  press. A reply closed at the first `SendDtmfAction` (GeneratorExit at that yield) leaves later presses recorded
  `{"status":"sent",...}` though they were never yielded; since P08-FIX-17 those records survive a supersede.
  Worse variant found while planning: `[press_button, end_call]` in one round closed at the press: `end_call` is
  already answered `ending` and `outcome.terminal` is set, but the generator never reaches `_tool_end`, so the
  record says "ending" for an end that is neither performed nor owed (`state.ended` False, `_owed_end` empty).
- `TaskBrain._run_tools` (task_runner.py:353-378) is the model: per item `answer` → `answered += 1` → `yield
  PressButton`; `finally` answers `plan.calls[answered:]` `interrupted`.
- "Yielded = handed over" holds in the pipeline: `_reply` → `_handle_output` → `_handle_action` creates the
  action task synchronously (before any suspension) and awaits it shielded (pipeline.py:783-796), so an action
  the generator yielded goes out even when the reply is cancelled at once; a cancel can only land at an await
  inside the generator chain (before the yield) or after the hand-over. `PathwayBrain` maps `SendDtmfAction` →
  `PressButton` without awaiting (pathway_brain.py:212), and `_out(run, (action,))` yields a `SendDtmfAction`
  without awaiting. So the press at which the close lands was sent: it stays `sent`.
- Item 1 of the note is fixed (`TaskBrain._transfer`/`_interrupt_answer`); the Decision text names the case.
  Owed: a repo test that a second `transfer_call` in the merged turn yields a `TransferRequest` (reviewer probe:
  /var/tmp/review_fix17/test_adhoc.py `test_second_transfer_in_merged_turn_is_requested`, not in the repo).

### 1. Code — `packages/va_engine/src/va_engine/engine_node_tools.py` only
`run_node_round` body becomes (signature unchanged, no new parameters):
```python
    tasks: list[asyncio.Task[ToolAnswer]] = []
    try:
        tasks = start_tools(executor, plan.executed, scope)
        line = await lines.line(executor, plan.executed, tasks, scope)
        if line is not None:
            yield Say(text=line, mark=next_mark())
        answers = [await task for task in tasks]
        merge_variables(variables, answers)
        for item, content in round_contents(plan, answers):
            answer(item.call, content)
            outcome.answered += 1
            if item.digits is not None:
                yield SendDtmfAction(digits=item.digits, duration_ms=DTMF_DURATION_MS)
        outcome.terminal = plan.terminal
    finally:
        await stop_tasks(tasks)
        for item in plan.calls[outcome.answered :]:
            answer(item.call, INTERRUPTED)
            outcome.answered += 1
```
- `outcome.terminal` stays AFTER the loop: nothing with digits follows a terminal call (calls behind it are
  `answer`/`skipped`), so it is set exactly when the whole round was answered and every press handed over; a
  close at a press leaves it `None` and the `finally` answers the rest (an `end_call` included) `interrupted`.
- Update the function docstring ("yields the speech/filler line, then answers the calls in call order through
  `answer`, each `press_button` followed at once by its `SendDtmfAction`; a round closed at a press answers
  the calls behind it `interrupted`"), the module docstring sentence about `press_button`, and the
  `RoundOutcome` docstring (`terminal` is set once every call was answered and every press handed over).
- No engine.py change expected: `_node_round` already closes `executed` via `aclosing` and reads
  `outcome.terminal` only after the loop. Verify `_tool_calls_used`/`budget` are unaffected (set before the
  round). Do not touch TaskBrain src.

### 2. Tests
`packages/va_engine/tests/test_engine_tools.py` (helpers there: `engine_rig`, `one_node`, `call`, `executor`,
`tool_records`, `drive`, `user`, `spoken`, `VOICE`):
- Add a small local helper, e.g. `async def closed_on_press(actions) -> list[tuple[Any, ...]]` (like
  `engine_helpers.closed_on_line` but breaks at the first `SendDtmfAction`; reads streams via `spoken`). If it
  would clone `closed_on_line` for jscpd, generalise `engine_helpers.closed_on_line` into
  `closed_on(actions, kind)` with `closed_on_line = partial`-style wrapper instead.
- Replace `test_press_button_sends_dtmf_after_the_answers` by
  `test_presses_are_answered_and_sent_in_call_order`: `engine_rig(one_node(), tools=executor(results={"lookup":
  '{"hours":"9-5"}'}), system=VOICE, fillers=False)`; reply 1 calls `[press "1", lookup, press "2"]`, reply 2
  "Pressed."; drive `rig.engine.start()` by hand and snapshot `tool_records(rig)` at each `SendDtmfAction`:
  at the first → `[("press_button", sent "1")]` (the second press and the lookup are not on record yet), at
  the second → all three in call order; rendered actions == `[("stream","", "t0.1", None), ("dtmf","1",250,None),
  ("dtmf","2",250,None), ("stream","Pressed.","t0.2",None)]` (take the exact mark strings from a run).
- `test_a_reply_closed_at_the_first_press_answers_the_later_press_interrupted`: `one_node(tools=[])`,
  `system=VOICE`, reply calls `[press "1", press "2"]`, closed at the first DTMF: seen DTMF digits == ["1"];
  `tool_records == [("press_button", '{"status":"sent","digits":"1"}'), ("press_button",
  '{"error":"interrupted"}')]`.
- `test_a_superseded_turn_keeps_the_sent_and_the_interrupted_press`: `reply("Hello.")` start, turn 1 = the
  two-press round closed at the first press (`handle_user_turn(user("press one", 1))`), then
  `drive(handle_user_turn(user("press one two", 2, supersedes=1)))` with `reply("Okay.")`: `rig.history()` ==
  `[("assistant","Hello."), ("tool", sent 1), ("tool", interrupted), ("user","press one two"),
  ("assistant","Okay.")]`.
- `test_an_end_call_behind_a_press_closed_at_the_press_does_not_end`: `one_node(tools=[])`, `system=VOICE`
  (end_call offered by default), `reply("Hello.")`, turn 1 reply calls `[press "1", end_call(reason="done",
  final_message="Bye.")]` closed at the DTMF; `tool_records` == `[press sent, ("end_call", interrupted)]`;
  `rig.state.ended is False`; then `await rig.say("hello?")` with `reply("Still here.")` returns a normal
  stream (no owed end, exactly one more request). Plus the unclosed counterpart inside the same or a sibling
  test: the same round driven fully yields dtmf, the final `Say` (not interruptible), the end action, and
  records `[press sent, ("end_call", '{"status":"ending"}')]`.
`packages/va_engine/tests/test_task_tool_speech.py`:
- `test_a_transfer_repeated_in_the_merged_turn_is_requested`: FakeSource `.reply(calls=[transfer]).reply(
  calls=[again])` (both `transfer_call {"message": "One moment."}`, ids `call_t`/`call_u`), `_brain(clock,
  source, _executor(clock), fillers=False, transfer_phone_number="+14155550133")`; turn 1 closed at the
  `SpeakStatic` (factor that loop out of `test_a_transfer_closed_on_its_message_is_carried_as_interrupted` into
  a helper `_closed_on_static(brain, turn) -> SpeakStatic` used by both tests, so jscpd stays at 0); turn 2
  `drain(brain.on_user_turn(user_turn(2, "a human, now", supersedes=1)))` == `[("static", "One moment.",
  False), ("action", <TransferRequest target "+14155550133", message "One moment.">)]` (assert `isinstance`
  + `target`); `[m.content for m in brain.messages if m.role == "tool"] == [INTERRUPTED,
  '{"status":"transferring"}']`; `_history` tool records the same pair; the turn-2 request (`_wire(source)`)
  ends `user`, `assistant` (tool_calls, no content), `tool INTERRUPTED`; `assert_tool_pairing`.
Mutation checks to report (restore byte-identical after each): (a) put back collect-then-yield in
`run_node_round` → the three close-at-press tests and the order test fail; (b) drop the
`TaskBrain._interrupt_answer` call in `_transfer` → the new transfer test fails on the first tool content.

### 3. Spec + docs (same commit)
- specs/13 §4: new block after Decision P08-FIX-17:
  `> Decision (2026-11-05) P08-FIX-19:` an answer that reports an action is on record only for an action that
  was handed to the consumer. 1. Pathway tool rounds answer their calls in call order and yield each
  `press_button`'s `SendDtmfAction` right after that call's answer (the task-mode order, specs/11 §6 Decision
  P05-T09c item 4 — keep spec ref and section on one line); a reply closed at a press leaves that press `sent`
  (the pipeline runs a received action to completion: specs/07 §8 Decision P04-T12b, the `asyncio.shield`
  sentence at specs/07 line ~400), answers every later call of
  the round `{"error":"interrupted"}` — an `end_call` included, which then neither ends the conversation nor is
  owed — and these records stay when the turn is superseded (Decision P08-FIX-17 item 1). 2. In Decision
  P08-FIX-17 item 1 "ran" means: executor tools, and actions handed to the consumer (`press_button`, a
  task-mode `transfer_call` after its message).
- In Decision P08-T02c1 item 4 (line ~128) append to the `press_button` sentence: "(order superseded by
  Decision P08-FIX-19: right after that call's answer)". In Decision P08-FIX-17 item 1 first sentence add
  "(see Decision P08-FIX-19 for answers that report an action)".
- AGENTS.md: one bullet `2026-11-05 (P08-FIX-19)`: `run_node_round` answers + yields per press; rule "an answer
  that reports an action (`sent`, `transferring`, `ending`) is recorded only when the action is yielded; the
  `finally` answers the rest `interrupted`"; test helper name(s).
- Do NOT touch `.ralph/review-inbox/DONE` or the plan tick (Reviewer: appends
  `2026-11-05-p08-fix-17-review.md`).

### Gates
PATH needs `/home/ralph/.local/bin`. `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
-q`; `uv run python scripts/check_spec_refs.py`; `make test` in the background (`: > logs/make_test.log`, then
`make test > logs/make_test.log 2>&1; echo exit=$? >> logs/make_test.log`, heartbeat Monitor ~2 min);
`npx --yes jscpd@4 --silent`; `uvx radon cc -s -a` on touched files; `uvx pip-audit --strict` on `uv export`;
`corepack pnpm audit --audit-level high` (plain `pnpm audit` has the known moderate minimatch advisory, task
fix:pnpm-audit-minimatch). No `make e2e`: pathway tools are not wired into the media runtime before P08-T02c2,
and no e2e flow issues two `press_button` calls in one pathway round; no TaskBrain src change.

### Risks
- Any test asserting the old order (all answers, then DTMF): only `test_press_button_sends_dtmf_after_the_answers`
  (single press: still passes, but rename/replace as above); grep `("dtmf"` in packages/va_engine/tests and
  `SendDtmfAction` in apps/ (media maps via PathwayBrain only) before finishing.
- `rig.say` / `rig.start` helpers drain fully; the close-at-press tests must drive the generator by hand under
  `contextlib.aclosing`.
- `end_call` arguments need `reason` (else answered `invalid_arguments`, AGENTS P08-FIX-16).
- Known flake (task fix:sim-shared-redis-exit-flake): `apps/sim/tests/test_sim_shared_redis.py::
  test_two_sim_processes_share_scenarios_and_reset`; if it fails, re-run alone and report both runs.

## Build P08-FIX-19 (2026-11-05)
Implemented exactly the plan.
- `packages/va_engine/src/va_engine/engine_node_tools.py`: `run_node_round` answers each call and yields a
  `press_button`'s `SendDtmfAction` right after that call's answer; `outcome.terminal` is set after the loop; the
  `finally` answers `plan.calls[outcome.answered:]` `interrupted`. Docstrings (module, `RoundOutcome`, function)
  updated. No engine.py / TaskBrain src change (checked: `_out` yields a `SendDtmfAction` without awaiting, and
  `_node_round` reads `outcome.terminal` only after the round).
- Tests `test_engine_tools.py`: `test_press_button_sends_dtmf_right_after_its_answer` (renamed single press),
  `test_presses_are_answered_and_sent_in_call_order` (records snapshotted at each DTMF),
  `test_a_reply_closed_at_the_first_press_answers_the_later_press_interrupted`,
  `test_a_superseded_turn_keeps_the_sent_and_the_interrupted_press`,
  `test_an_end_call_behind_a_press_closed_at_the_press_does_not_end` (not ended, nothing owed, next turn is a normal
  reply, 3 requests), `test_an_end_call_behind_a_press_handed_over_ends_the_call`. Helper
  `engine_helpers.closed_on(agen, kind)`; `closed_on_line` now wraps it.
- Tests `test_task_tool_speech.py`: `test_a_transfer_repeated_in_the_merged_turn_is_requested` (carried answer
  `interrupted`, second call yields `TransferRequest` target +14155550133, answered `transferring`, turn-2 request
  ends user / assistant tool_calls / tool interrupted, pairing valid); helper `_closed_on_static(brain, turn)` shared
  with the existing closed-transfer test. Deviation from the plan's expected history: the `transferring` tool
  record precedes the assistant "One moment." record (the answer is recorded before the message is spoken), so the
  test asserts `[tool interrupted, user, tool transferring, assistant "One moment."]`.
- Spec: specs/13 §4 new `> Decision (2026-11-05) P08-FIX-19` (2 items) + cross-references in Decision P08-T02c1
  item 4 and Decision P08-FIX-17 item 1. AGENTS.md bullet `2026-11-05 (P08-FIX-19)`.

Mutation checks (`uv run pytest packages/va_engine -n 4 --no-cov`, sources restored byte-identical, clean run
1176 passed): (a) collect-then-yield put back in `run_node_round` → 4 failed (order test + the three close-at-press
tests); (b) `_interrupt_answer` call dropped from `TaskBrain._transfer` → 2 failed (the closed-transfer test and
the new repeated-transfer test).

Gates (real results, this iteration):
- make fmt / lint / typecheck / placeholders: exit 0 each. check_spec_refs: 0 problem(s) (re-run after the last
  spec wording edit).
- make test: exit 0, 13058 passed, 7 skipped (live suite), coverage 99.08 % (`engine_node_tools.py` 100 %), 849 s.
  The known sim shared-Redis flake did not fire.
- jscpd: 0 clones. radon on touched files: average 3.46; one C (11), the pre-existing test function
  `test_an_end_call_closed_on_its_final_line_ends_on_the_next_turn` (untouched); src file all A/B.
- pip-audit --strict (CI form, `uv export --frozen --all-packages`): no known vulnerabilities.
- pnpm audit --audit-level high (CI gate): exit 0 (1 moderate, known, task fix:pnpm-audit-minimatch).
- make test-ts not run (no TS change). make e2e not run: pathway tools are not wired into the media runtime
  before P08-T02c2, no TaskBrain src change, no e2e flow issues two presses in one pathway round.

## Review P08-FIX-19 (2026-11-05) — APPROVED
Re-ran: make lint / typecheck / placeholders exit 0; check_spec_refs 0 problem(s); jscpd 0 clones; radon
`engine_node_tools.py` average A (4.4); pip-audit (CI form) no known vulnerabilities; pnpm audit --audit-level high
exit 0 (1 moderate, known task fix:pnpm-audit-minimatch); make test exit 0, 13058 passed, 7 skipped, coverage
99.08 % (`engine_node_tools.py` 100 %), 830 s. The sim shared-Redis flake did not fire.
Diff read against specs/13 §4 (Decisions P08-T02c1 item 4, P08-FIX-17 item 1, new P08-FIX-19), specs/11 §6 Decision
P05-T09c item 4 and specs/07 §8 Decision P04-T12b: `run_node_round` answers per call and yields each press right
after its answer; no await sits between `answer(...)` and the consumer's receipt (`_node_round` → `_out` →
`PathwayBrain._relay` yield synchronously; `CallPipeline._handle_action` creates the action task before its first
suspension and awaits it shielded), so "yielded = handed over" holds and a cancel can only land before the answer
or after the hand-over. `plan_round` answers everything behind a terminal call `skipped`, so no press follows an
`end_call`; `outcome.terminal` after the loop is correct.
Reviewer mutation checks (shadow copy of `va_engine` on PYTHONPATH, repo untouched): collect-then-yield restored
→ 4 failed in test_engine_tools.py (order test + three close-at-press tests); `_interrupt_answer` call dropped
from `TaskBrain._transfer` → 2 failed in test_task_tool_speech.py (closed-transfer + repeated-transfer).
Inbox note `2026-11-05-p08-fix-17-review.md`: item 1 fixed in 75e109d with its owed assertion added here, item 2
fixed here → appended to `.ralph/review-inbox/DONE`. No findings.

## Planner (2026-11-05, after P08-FIX-19 approval)
Inbox: new note `2026-11-05-p08-fix-19-wip-review.md` (not in DONE). Item 1 = blocker → P08-FIX-20 inserted at the
top of plan/phase-08.md. Item 2 (not a blocker) → added to the P08-T02c2 acceptance (real `for_refs`: two nodes with
different tools of one name speak/call their own; a node executor does not `handle` a custom tool its refs do not
name). `2026-11-05-p08-fix-15-review.md` is still open as P08-FIX-18 (next after FIX-20). DEC-013 recorded.

## Plan P08-FIX-20 — a cut tool round answers `interrupted` only for what did not happen

### Findings (read at HEAD 7704591)
- `run_node_round` (engine_node_tools.py:186-208) and `TaskBrain._run_tools` (task_runner.py:353-378): the
  `finally` answers EVERY unanswered call `INTERRUPTED`. When the reply is closed at a press, all executor tasks
  of the round already finished (`answers = [await task ...]`, variables merged) → a finished `book` behind the
  press reads `interrupted` although its side effect, `call.tool` event and variables are in place; superseded
  turns keep these records (P08-FIX-17), so the model re-calls the tool.
- Same class, not in the note (DEC-013): a barge-in on the speech/filler line (generator closed at the `Say` /
  `SpeakStatic`, or cancelled inside `lines.line` / `await task`) while tool A finished and tool B still runs →
  A is answered `interrupted` and its variables are never merged. specs/13 §5 Decision P08-T02b1 item 4 says
  the barge-in "cancels the unfinished tools and answers them `interrupted`".
- `execute_gated` catches `Exception`, so a task that is done and not cancelled has a result. System tools behind
  the executor are shielded runs (P08-FIX-13): a barge-in cancels the wait → the task is cancelled → stays
  `interrupted` (outcome unknown to the round). Unchanged.
- `plan_round` kinds: `execute`, `press`, `end`, `transfer`, `answer` (invalid / too_many / skipped, with a
  precomputed `answer`).

### Rule (one for every cut, both runners)
For each call of the round not yet answered when the round is cut:
- `execute` whose task finished (done, not cancelled, no exception) → its real content (blank → `tool_failed`);
  its variables are merged (call order) if the round had not merged yet.
- `execute` whose task was cancelled / never started → `INTERRUPTED`.
- `answer` → its planned answer (`invalid_arguments`, `too_many_tool_calls`, `skipped`).
- `press`, `end`, `transfer` → `INTERRUPTED` (not handed over). `RoundOutcome.terminal` / `placed` stay unset.

### 1. `packages/va_engine/src/va_engine/tool_tasks.py` (add to `__all__`, docstring paragraph)
```python
def finished_answers(tasks: Sequence[asyncio.Task[ToolAnswer]]) -> list[ToolAnswer | None]:
    """Per task, in call order: the answer of a run that finished, ``None`` for one that was cancelled
    (call after :func:`stop_tasks`)."""
    # done() and not cancelled() and exception() is None -> task.result(); else None

def cut_contents(plan: RoundPlan, answers: Sequence[ToolAnswer | None]) -> list[tuple[PlannedCall, str]]:
    """Every call of a round that was cut short, in call order, with the content that is true for it."""
    # execute -> next(answers, None): content or TOOL_FAILED when not None, else INTERRUPTED
    #   (fewer answers than executed calls = the tools never started: INTERRUPTED; never raises)
    # answer  -> item.answer or TOOL_FAILED
    # press / end / transfer -> INTERRUPTED

async def settle_round(
    plan: RoundPlan, tasks: Sequence[asyncio.Task[ToolAnswer]], *, answered: int, merged: bool
) -> tuple[list[ToolAnswer], list[tuple[PlannedCall, str]]]:
    """End a round: cancel the tools still running; returns the finished answers whose variables are still
    to be merged (``[]`` when ``merged``) and the contents of the calls from position ``answered`` on."""
    await stop_tasks(tasks)
    finished = finished_answers(tasks)
    late = [] if merged else [answer for answer in finished if answer is not None]
    return late, cut_contents(plan, finished)[answered:]
```
Import `INTERRUPTED` from `va_engine.task_tools`. If `round_contents` and `cut_contents` trip jscpd, build both
on one private `_contents(plan, pick)`; keep `round_contents`' ValueError for a length mismatch.

### 2. `packages/va_engine/src/va_engine/engine_node_tools.py` — `run_node_round`
```python
    tasks: list[asyncio.Task[ToolAnswer]] = []
    merged = False
    try:
        tasks = start_tools(executor, plan.executed, scope)
        line = await lines.line(executor, plan.executed, tasks, scope)
        if line is not None:
            yield Say(text=line, mark=next_mark())
        answers = [await task for task in tasks]
        merge_variables(variables, answers)
        merged = True
        for item, content in round_contents(plan, answers):
            ... (unchanged)
        outcome.terminal = plan.terminal
    finally:
        late, rest = await settle_round(plan, tasks, answered=outcome.answered, merged=merged)
        merge_variables(variables, late)
        for item, content in rest:
            answer(item.call, content)
            outcome.answered += 1
```
Drop the now unused imports (`INTERRUPTED`, `stop_tasks`) if nothing else uses them. Update the module
docstring's last sentences, the `RoundOutcome` and function docstrings (a cut round: finished tools keep
their answers and variables, cancelled tools and actions not handed over read `interrupted`).
On a completed round `rest` is `[]` and `late` is `[]`: no behaviour change.

### 3. `packages/va_engine/src/va_engine/task_runner.py` — `TaskBrain._run_tools`
Same shape: `merged = False`; after `self._merge_variables(answers)` set `merged = True`; `finally`:
```python
            late, rest = await settle_round(plan, tasks, answered=answered, merged=merged)
            self._merge_variables(late)          # calls on_state when something merged
            for item, content in rest:
                self._answer(item.call, content)
```
(`answered` need not be advanced there; `placed` stays `None` for a terminal answered in the `finally`, so
`_finish` is not entered — and after a close nothing may be yielded anyway.) Fix the imports; keep the
function under the complexity limit (it is near it: check `uvx radon cc -s` on the file).
`_merge_variables` takes `list[ToolAnswer]`: fine.

### 4. Tests
`packages/va_engine/tests/support/task_helpers.py`: `RecordingExecutor` gets `holding: frozenset[str] =
frozenset()` ("only these tool names wait on `release`"): `if self.hold or call.name in self.holding: await
self.release.wait()`; docstring updated.

`packages/va_engine/tests/test_tool_tasks.py` (unit, real asyncio tasks):
- `test_finished_answers_keep_finished_runs_and_drop_cancelled_ones` (one task done, one cancelled via
  `stop_tasks` → `[answer, None]`).
- `test_cut_contents_answer_each_kind` : plan `[execute(done), execute(cancelled), answer(invalid), press,
  end, answer(skipped)]` (build with `plan_round` + facts, or `PlannedCall`s directly) → real content,
  INTERRUPTED, planned invalid message, INTERRUPTED, INTERRUPTED, SKIPPED; blank content → TOOL_FAILED; no
  answers at all (tools never started) → executes read INTERRUPTED.
- `test_settle_round_cancels_the_running_tools_and_reports_late_answers` (`merged=False` → the finished
  answer is in `late`; `merged=True` → `late == []`; `answered=n` slices).

`packages/va_engine/tests/test_engine_tools.py` (helpers `engine_rig`, `one_node`, `executor`, `call`,
`press`, `tool_records`, `closed_on`, `drive_held`, `position`, `context_update`, `SENT_1`, `CUT`, `VOICE`):
- `test_a_tool_that_finished_keeps_its_answer_when_the_reply_is_closed_at_a_press`: `one_node()`,
  `tools=executor(results={"lookup": hours}, variables={"lookup": {"hours": "9-5"}})`, `system=VOICE`,
  `fillers=False`; reply calls `[press("c1","1"), call("c2","lookup")]`; `closed_on(rig.engine.start(),
  SendDtmfAction)` → `tool_records == [SENT_1, ("lookup", hours)]`, `rig.state.variables["hours"] == "9-5"`,
  `[c.name for c in tools.calls] == ["lookup"]`, `tools.cancelled == []`.
- `test_a_superseded_turn_shows_the_finished_tool_behind_a_cut_press`: `reply("Hello.")`, turn 1 = that round
  closed at the press, then `drive(handle_user_turn(user("press one please", 2, supersedes=1)))` with
  `reply("Okay.")`: `rig.history() == [("assistant","Hello."), ("tool", SENT_1[1]), ("tool", hours),
  ("user","press one please"), ("assistant","Okay.")]`; `position(rig, "system", context_update("lookup",
  hours)) < position(rig, "user", "press one please")`; lookup ran once.
- `test_actions_behind_a_cut_press_read_interrupted_and_the_call_does_not_end`: calls `[press 1, lookup,
  press 2, end_call(reason="done", final_message="Bye.")]` closed at press 1 → `[SENT_1, ("lookup", hours),
  ("press_button", CUT), ("end_call", CUT)]`, `rig.state.ended is False`; a following `rig.say("hello?")` with
  `reply("Still here.")` is a normal stream.
- `test_a_barge_in_on_the_filler_keeps_the_answer_of_the_tool_that_finished`: `one_node(tools=["TL-1",
  "TL-2"])`, `tools=executor(holding=frozenset({"track"}), results={"lookup": hours}, variables={"lookup":
  {"hours": "9-5"}})`; calls `[lookup, track]`; `drive_held(rig, tools, close_on_line=True)` → last action a
  filler `say`; `tool_records == [("lookup", hours), ("track", CUT)]`; `[n for n, _ in tools.cancelled] ==
  ["track"]`; `rig.state.variables["hours"] == "9-5"`.
- Update `test_a_barge_in_during_the_filler_cancels_the_tool_and_answers_interrupted`: `("nope", …)` now keeps
  its planned unknown-tool answer (take the exact content from `InvalidToolCall("nope", UNKNOWN_TOOL).
  tool_message("c2").content` or from a run); rename to `..._answers_it_interrupted`. Any other expectation
  that changes must be a call of kind `answer` or a tool that had finished: report each one.

`packages/va_engine/tests/test_task_tool_speech.py` (helpers `_brain`, `_executor`, `_call`, `_shape`,
`_history`, `_wire`, `_closed_on_static`, `SHIPPED`, `FakeSource`, `FrozenClock`; add a small
`_closed_on_press(brain, turn)` — or generalise `_closed_on_static` to `_closed_on(brain, turn, kind)` to keep
jscpd at 0; press_button must be on offer: check `_brain`'s facts, see `test_task_runner.py::
test_closing_the_reply_mid_round_answers_the_pending_calls`):
- `test_a_tool_behind_a_cut_press_keeps_its_answer_and_is_carried`: round `[press_button {"digits":"1"},
  lookup_order]`, executor `results={"lookup_order": SHIPPED}, variables={"lookup_order": {"eta": "Fri"}}`,
  `fillers=False`, `states=[]`; closed at the `PressButton` → tool contents `[sent 1, SHIPPED]`,
  `brain.state.variables["eta"] == "Fri"` and a state snapshot was taken; then
  `drain(brain.on_user_turn(user_turn(2, "…", supersedes=1)))` with `reply("Done.")`: `_shape(brain) ==
  [("user", merged, 0), ("assistant", None, 2), ("tool", sent 1, 0), ("tool", SHIPPED, 0), ("assistant",
  "Done.", 0)]`, `assert_tool_pairing([{"messages": _wire(source)}])`, `len(executor.calls) == 1`.
- `test_actions_behind_a_cut_press_read_interrupted`: `[press 1, lookup_order, press 2, end_call]` closed at
  press 1 → `[sent 1, SHIPPED, INTERRUPTED, INTERRUPTED]`, `brain.state.ended is False`.
- `test_a_barge_in_on_the_filler_keeps_the_finished_tools_answer`: two tools (`lookup_order`, `eta` as in
  `_id_less_round`), `holding=frozenset({"eta"})`, FrozenClock (the filler delay returns at once); closed at
  the filler `SpeakStatic` → tool contents `[SHIPPED, INTERRUPTED]`, cancelled == ["eta"], lookup's variables
  merged.
Mutation checks to report (restore byte-identical): (a) `cut_contents` answering every call `INTERRUPTED` →
the new engine + TaskBrain tests fail; (b) `late` merge dropped in either runner → the barge-in-on-filler
variable assertions fail.

### 5. Spec + docs (same commit)
- specs/13 §4, new block after Decision P08-FIX-19:
  `> Decision (2026-11-05) P08-FIX-20:` in a tool round cut short (pathway reply nodes and task mode; the cut
  may land on the speech/filler line, while the tools are awaited, or at a press) a call is answered
  `{"error":"interrupted"}` only when what it stands for did not happen: 1. an executor call whose run was
  still unfinished (cancelled at the cut), 2. a `press_button`, `end_call` or `transfer_call` that was not
  handed over. An executor call whose run finished keeps its answer and its variables are merged at the cut,
  in call order (its request went out and its `call.tool` event exists); a call answered without running
  (`invalid_arguments`, `too_many_tool_calls`, `skipped`) keeps that answer. Helpers `va_engine.tool_tasks.
  settle_round`/`cut_contents`/`finished_answers`. Supersedes the "every later call"/"unanswered calls"
  wording of Decisions P08-FIX-19 item 1 and P08-T02c1 item 3.
- Reword in place: Decision P08-FIX-19 item 1 "Every later call of the round is answered `interrupted`, an
  `end_call` included" → "Every later call of the round that reports an action is answered …, an `end_call`
  included; executor calls of the round have finished by then and keep their answers (Decision P08-FIX-20)".
  Notes: Decision P08-T02c1 item 3 (`unanswered calls` sentence), §5 Decision P08-T02b1 item 4 (finished tools
  keep their answers) and item 5 (variables of a cut round merge at the cut), specs/11 §6 Decision P05-T09c
  item 4 last sentence (line ~720: "calls left unanswered when the reply is cut off…") → each gets
  "(see specs/13 §4 Decision P08-FIX-20)" style pointer with spec ref and section on ONE line.
- AGENTS.md: bullet `2026-11-05 (P08-FIX-20)`: the rule, helper names, `RecordingExecutor(holding=)`; adjust
  the P08-FIX-19 bullet's "the `finally` answers the rest `interrupted`" to point at it.
- Do NOT tick the plan or touch `.ralph/review-inbox/DONE` (Reviewer: appends
  `2026-11-05-p08-fix-19-wip-review.md`).

### Gates
PATH needs `/home/ralph/.local/bin`. `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
-q`; `uv run python scripts/check_spec_refs.py`; `make test` in the background (`: > logs/make_test.log`, then
`make test > logs/make_test.log 2>&1; echo exit=$? >> logs/make_test.log`, heartbeat Monitor ~2 min);
`npx --yes jscpd@4 --silent`; `uvx radon cc -s -a` on touched files; `uvx pip-audit --strict` on `uv export`;
`corepack pnpm audit --audit-level high`. `make e2e` this time (background + heartbeat): `TaskBrain._run_tools`
is on the path of every task-mode call with tool calls (IVR `press_button` flows).

### Risks
- A cancel (not a close) landing inside the `finally`'s `await settle_round` is pre-existing behaviour of
  `stop_tasks`; do not add shielding.
- `task.result()` on a cancelled task raises: guard with `cancelled()` first; a task still pending cannot
  occur after `stop_tasks`, but treat it as `None` anyway.
- Existing expectations that change: only `("nope", interrupted)` is known; media/pipeline tests asserting
  `interrupted` for a finished tool would surface in `make test` (grep `INTERRUPTED`/`"interrupted"` in
  apps/media_worker/tests and packages/va_voice/tests first).
- Known flake (task fix:sim-shared-redis-exit-flake): `apps/sim/tests/test_sim_shared_redis.py::
  test_two_sim_processes_share_scenarios_and_reset`; if it fails, re-run alone and report both runs.

## Build P08-FIX-20 (2026-11-05)
Implemented the plan as written (no deviations in behaviour):
- `va_engine.tool_tasks`: `finished_answers(tasks)`, `cut_contents(plan, answers)` (private `_cut_content`
  per call kind), `settle_round(plan, tasks, answered=, merged=)` → `(late, rest)`; module docstring paragraph.
- `run_node_round` and `TaskBrain._run_tools`: `merged` flag set after the round's merge; `finally` =
  `settle_round` → merge `late` → answer `rest`. `INTERRUPTED`/`stop_tasks` imports dropped where unused.
- Test fake: `RecordingExecutor(holding=frozenset({...}))` holds only those tool names on `release`.
- Tests: test_tool_tasks.py (+5: finished_answers finished/cancelled/failed, a raising task → None,
  cut_contents each kind incl. transfer, tools never started, settle_round late/rest/merged); test_engine_tools.py
  (+4: finished tool behind a cut press keeps answer + variables, superseded turn shows it ahead of the merged
  user turn, press/end_call behind a cut press read interrupted and the call does not end, barge-in on the filler
  keeps the finished tool's answer + variables while the held one is cancelled); test_task_tool_speech.py (+3 for
  TaskBrain, `_closed_on(brain, turn, kind)` generalises `_closed_on_static`).
- Expectation changed (the only one): `test_a_barge_in_during_the_filler_cancels_the_tool_and_answers_it_
  interrupted` (renamed): the unknown tool `nope` (kind `answer`) now keeps
  `{"error":"invalid_arguments","detail":"unknown_tool"}` instead of `interrupted`. No test outside va_engine
  changed (va_tools barge-in test: the shielded system tool's wait is cancelled → still `interrupted`).
- Spec: specs/13 §4 new Decision P08-FIX-20; reworded Decision P08-FIX-19 item 1, P08-T02c1 item 3,
  P08-FIX-17 item 1 last sentence, §5 Decision P08-T02b1 items 4 and 5; specs/11 §6 Decision P05-T09c item 4.
  AGENTS.md bullet P08-FIX-20 (+ pointer in the P08-FIX-19 bullet).
- Mutation checks (files restored byte-identical, sha256 verified): (a) `cut_contents` answering every call
  `INTERRUPTED` → 11 failed (5 engine, 3 TaskBrain, 3 unit); (b1) `merge_variables(variables, late)` dropped in
  `run_node_round` → 1 failed (engine barge-in-on-filler variables); (b2) `self._merge_variables(late)` dropped in
  `_run_tools` → 1 failed (TaskBrain barge-in-on-filler variables).
Gates: `make fmt` 0; `make lint` 0; `make typecheck` 0 (1614 source files); `make placeholders` 0 findings;
`check_spec_refs` 0 problems; `make test` exit 0: 13070 passed, 7 skipped, coverage 99.08 % (tool_tasks.py and
engine_node_tools.py 100 %, task_runner.py 98 %); jscpd 0 clones; radon average on touched src files 3.56
(`TaskBrain._run_tools` B 10, unchanged); pip-audit (CI invocation) no known vulnerabilities; `pnpm audit
--audit-level high` exit 0 (1 moderate, the known minimatch task).
Not done on purpose: plan tick, `.ralph/review-inbox/DONE` (Reviewer).
`make e2e` exit 0: 20 passed, 107 deselected in 417.86 s (amd_ivr_mode, barge_in, cold_transfer,
concurrent_calls, inbound_basic, interruptibility_levels x5, max_duration, outbound_basic,
pathway_regression_gate, pathway_restaurant_template, stop_via_api, voicemail_hangup, voicemail_leave_message,
webhook_node, webhooks_call_completed_and_live, worker_crash_recovery). After the last prose rewrap in specs/13
(made while `make test` ran): `make lint` 0, placeholders 0, spec refs 0, va_engine + va_tools + scripts tests pass.

## Review P08-FIX-20 (2026-11-05) — APPROVED
Re-run by the Reviewer on the work tree (source files unchanged since 14:04Z): `make lint` 0, `make typecheck` 0,
`make placeholders` 0, `check_spec_refs` 0 problems, jscpd 0 clones, radon average 3.56 on the touched src files
(`_run_tools` B 10, `_cut_content` B 6), pip-audit (CI export) no known vulnerabilities, `pnpm audit --audit-level
high` exit 0 (1 known moderate), `make test` exit 0: 13070 passed, 7 skipped, coverage 99.08 % (tool_tasks.py and
engine_node_tools.py 100 %), `make e2e` exit 0: 20 passed, 107 deselected in 417.57 s.
Diff read against specs/13 §4 Decision P08-FIX-20 and the reworded items (P08-FIX-19 item 1, P08-T02c1 item 3,
P08-FIX-17 item 1, §5 P08-T02b1 items 4/5, specs/11 §6 P05-T09c item 4): code and text agree; a completed round
is unchanged (`late == []`, `rest == []`); no double merge (`merged` set with no await after the merge); a cut
round never sets `outcome.terminal` / never reaches `_finish`.
Reviewer mutation checks on a shadow copy (PYTHONPATH, work tree untouched): `cut_contents` all `INTERRUPTED` →
11 failed; both `late` merges dropped → 2 failed; planned answers overwritten by `interrupted` → 4 failed.
Reviewer probe outside the repo (removed): the reply task cancelled while `run_node_round` awaits the tools
(fillers off, `track` held first and second) keeps `lookup`'s real answer + variables, `track` and `end_call`
read `interrupted`, `nope` keeps its planned answer, the call does not end.
Appended `2026-11-05-p08-fix-19-wip-review.md` to `.ralph/review-inbox/DONE`.
For the Planner: `.ralph/review-inbox/2026-11-05-p08-fix-20-wip-review.md` (maintainer review of this same tree:
no blockers) is NOT in DONE yet. Its two non-blocking items: 1. a system tool cancelled at the cut reads
`interrupted` although its shielded run completes (possible duplicate `send_sms`; needs a spec decision);
2. no committed test pins the cut "while the tools are awaited" (works today, see the probe above).

## Planner (2026-11-05, after P08-FIX-20 approval)
Inbox: two notes not in DONE. `2026-11-05-p08-fix-15-review.md` has its task (P08-FIX-18, unticked, next after
this one). `2026-11-05-p08-fix-20-wip-review.md` (no blockers; 2 items) is new → P08-FIX-21 inserted at the top
of plan/phase-08.md and planned now. Both items are fixed, not closed: item 1 by DEC-014 (explicit
`ToolExecutor.detached(call)` query, answer `in_progress`; no cancellation is swallowed), item 2 by tests.
Order after this: P08-FIX-18, then P08-T02c2.

## Plan P08-FIX-21 — a system tool run that goes on after the cut reads `in_progress`; test the cut while the tools are awaited

### Findings (read at HEAD 5365779)
- `va_tools.executor.CallToolExecutor._run_system`: the run is an executor-owned task awaited through
  `asyncio.shield`; cancelling the round's tool task cancels only the wait (`CancelledError` propagates, the
  task of `start_tools` ends cancelled).
- `va_engine.tool_tasks._cut_content`: a cancelled execute call reads `INTERRUPTED`. So a `send_sms` in flight
  at a barge-in / superseded turn is carried as `interrupted` although the text goes out; the merged turn's
  model may send it again (cap 3). `schedule_callback` is saved by the 1-pending cap.
- Do NOT make the executor return an answer from its cancelled wait: a runner parked in `await task` that is
  cancelled forwards the cancel to that tool task (`Task.cancel` cancels `_fut_waiter`); a tool task that then
  ends with a result makes the runner lose its own cancellation (DEC-014).
- Implementations of the `ToolExecutor` protocol: `CallToolExecutor` (va_tools), test fakes `NodeExecutor` and
  `RecordingExecutor` (`packages/va_engine/tests/support/task_helpers.py`). `ToolOffer` only has `handles`
  (not an executor). Callers of `settle_round`: `run_node_round`, `TaskBrain._run_tools`.

### 1. `packages/va_engine/src/va_engine/task_tools.py`
`IN_PROGRESS: Final = tool_result({"status": "in_progress"})` next to `INTERRUPTED` (+ `__all__`, one-line
docstring: "the call's run goes on without the round (an executor-owned system tool)").

### 2. `packages/va_engine/src/va_engine/tool_executor.py` — protocol
```python
    def detached(self, call: ToolCall) -> bool:
        """Whether a run of ``call`` (this very object: call ids may be empty or repeated) goes on although
        the wait for its answer was cancelled. A cut round answers such a call ``in_progress`` instead of
        ``interrupted`` (specs/13 §6, Decision P08-FIX-21)."""
        ...
```
Module docstring: one sentence. `PathwayToolExecutor` is unchanged (a node's executor from `for_refs` answers).

### 3. `packages/va_engine/src/va_engine/tool_tasks.py`
- `_cut_content(item, finished, executor)`: execute call with no finished answer →
  `IN_PROGRESS if executor is not None and executor.detached(item.call) else INTERRUPTED`.
- `cut_contents(plan, answers, *, executor: ToolExecutor | None = None)`.
- `settle_round(plan, tasks, *, executor: ToolExecutor | None, answered: int, merged: bool)` (keyword
  required, so no caller forgets it) → `cut_contents(plan, finished, executor=executor)[answered:]`.
- Module docstring paragraph on cut rounds + docstrings: "a tool cancelled at the cut reads `interrupted`,
  unless the executor says its run goes on (`in_progress`)".
`run_node_round`: `settle_round(plan, tasks, executor=executor, ...)`; `TaskBrain._run_tools`:
`executor=self._executor`. Update both docstrings/comments. Watch radon on `_run_tools` (B 10 today: do not
add a branch there) and `_cut_content` (B 6).

### 4. `packages/va_tools/src/va_tools/executor.py`
```python
        self._detached: list[ToolCall] = []     # __init__

    def detached(self, call: ToolCall) -> bool:
        return any(item is call for item in self._detached)

    async def _run_system(self, tool: SystemTool, call: ToolCall) -> ToolAnswer:
        task = ...  (unchanged)
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():  # the wait was cancelled, the run was not: it goes on (or just ended)
                self._detached.append(call)
            raise
        return ToolAnswer(outcome.content)
```
(`task.cancelled()` is true when `aclose` cancelled the run itself: then nothing goes on → `interrupted`.)
Identity, not equality: `ToolCall` is a pydantic model, equal by value. Module docstring: the paragraph on
system tools gets "and tells the round so (`detached`), which answers the call `in_progress`".

### 5. Test fakes — `packages/va_engine/tests/support/task_helpers.py`
- `RecordingExecutor`: `detaching: frozenset[str] = frozenset()` ("runs of these tool names go on when their
  wait is cancelled") and `left_running: list[ToolCall]`; in `execute`'s `except CancelledError`: append the
  call to `left_running` when `call.name in self.detaching` (still listed in `cancelled`, still re-raised);
  `def detached(self, call) -> bool: return any(item is call for item in self.left_running)`.
- `NodeExecutor.detached` delegates to `self.parent.detached(call)`.
- `async def wait_started(self, name: str) -> None`: `async with asyncio.timeout(5), self.changed: await
  self.changed.wait_for(lambda: any(n == name for n, _ in self.started))` (`started` is appended before the
  notify in `execute`). Docstrings updated.

### 6. Tests
`packages/va_engine/tests/test_tool_tasks.py`
- `test_a_cancelled_run_that_goes_on_reads_in_progress`: plan with two execute calls, both tasks cancelled via
  `stop_tasks`, an executor fake whose `detached` is true for the first call object only →
  `cut_contents(plan, answers, executor=fake)` contents `[IN_PROGRESS, INTERRUPTED]`; without an executor
  both `INTERRUPTED`; a tool that never started (no tasks) with a detaching executor → `INTERRUPTED`.
- `settle_round` tests pass `executor=None` (existing) and one with the fake.

`packages/va_engine/tests/test_engine_tools.py`
- `test_a_reply_cancelled_while_its_tools_run_keeps_the_finished_tools_answer` (item 2), parametrized over the
  call order `("lookup", "track")` / `("track", "lookup")` (string ids): `one_node(tools=["TL-1", "TL-2"])`,
  `executor(holding=frozenset({"track"}), results={"lookup": hours}, variables={"lookup": {"hours":
  "9-5"}})`, `fillers=False`, reply `calls=[call("c1", first), call("c2", second), call("c3", "nope"),
  end_call]` (end_call needs `reason`; `system=VOICE`); `consumer = asyncio.create_task(drive(rig.engine.
  start()))`; `await tools.wait_started("track")` then, for the held-first order, also
  `wait_started("lookup")` (a tool that is not held finishes in the step that starts it); `consumer.cancel()`;
  `with pytest.raises(asyncio.CancelledError): await consumer`. Expect: `tool_records` = lookup's real answer,
  `("track", CUT)`, nope's planned `invalid_arguments`/`unknown_tool` answer, `("end_call", CUT)` in call
  order; `rig.state.variables["hours"] == "9-5"`; `[n for n, _ in tools.cancelled] == ["track"]`;
  `rig.state.ended is False`. With fillers off `ToolLines.line` returns without suspending, so the generator
  is parked in `answers = [await task ...]` when `track` has started. If the held-second sync proves racy, wait
  on a predicate over `started` and `active` instead; never add sleeps.
- `test_a_system_tool_that_runs_on_is_answered_in_progress`: executor `names=frozenset({"send_sms"})`,
  `holding=frozenset({"send_sms"})`, `detaching=frozenset({"send_sms"})`, facts `dataclasses.replace(VOICE,
  sms_capable=True)`; reply `[call("c1", "send_sms", message="Code 42")]`; `drive_held(rig, tools,
  close_on_line=True)` (closed at the filler) → `tool_records(rig) == [("send_sms", IN_PROGRESS)]`; then a
  superseding turn (`user(..., supersedes=…)` as in the FIX-20 test) with `reply("Okay.")`: the context update
  with `in_progress` sits ahead of the merged caller turn and `len(tools.calls) == 1`.
- Same executor without `detaching` → `("send_sms", CUT)` (one assertion in the same test or a twin).

`packages/va_engine/tests/test_task_tool_speech.py` (TaskBrain; helpers `_brain`, `_executor`, `_call`,
`_closed_on`, `_shape`, `_wire`, `SHIPPED`)
- `test_a_reply_cancelled_while_its_tools_run_keeps_the_finished_tools_answer`: two tools (`lookup_order`,
  `eta`), `holding=frozenset({"eta"})`, `fillers=False`; consume `brain.on_user_turn(...)` in a task with a
  small local consumer (reads `SpeakText.tokens` like `_closed_on`), `wait_started("eta")`, cancel, expect
  tool contents `[SHIPPED, INTERRUPTED]`, lookup's variables merged + a state snapshot, `eta` cancelled.
- `test_a_system_tool_that_runs_on_is_carried_as_in_progress`: `send_sms` behind the executor
  (`names=frozenset({"lookup_order", "send_sms"})`, facts with `sms_capable=True`: check how `_brain` builds
  facts, extend it with a keyword if needed), closed at the filler `SpeakStatic` (FrozenClock) → tool content
  `[IN_PROGRESS]`; superseding turn: carried round `user, assistant(tool_calls), tool(in_progress)`,
  `assert_tool_pairing`, the executor ran `send_sms` once.

`packages/va_tools/tests/test_executor.py` (helpers `_GatedSystem`, `_cancel_mid_run`, `_Events`, `SCOPE`)
- `_cancel_mid_run` also returns the `ToolCall` it used (or builds it from a parameter).
- `test_a_call_whose_wait_was_cancelled_mid_run_is_detached`: after `_cancel_mid_run`: `executor.detached(call)`
  is true before and after the gate is set + `aclose`; an equal `ToolCall(id="c1", ...)` built anew is not
  detached; a call never executed is not.
- `test_a_run_cancelled_by_aclose_is_not_detached`: never-finishing tool, the waiter is NOT cancelled by the
  test: `aclose(timeout_s=0.01)` cancels the run, the waiter raises `CancelledError`, `detached(call)` false.
- `test_a_custom_tool_cancelled_mid_request_is_not_detached`: respx route with a side effect that waits on an
  `asyncio.Event` (see `StreamingBody`/`Hanging` patterns; the `api` fixture), cancel the waiting task →
  `detached` false, no `call.tool` event.
`packages/va_tools/tests/test_executor_barge_in.py`: tool messages `== [IN_PROGRESS]` (import from
`va_engine.task_tools`), `sms.sent == ["Your code is 42."]` after `aclose`; rename/docstring accordingly.

Mutation checks to report (restore byte-identical, sha256): (a) `_cut_content` never asks the executor → the
new engine, TaskBrain, unit and barge-in tests fail; (b) `_run_system` appends on every cancellation
(`task.cancelled()` check dropped) → the aclose test fails; (c) `detached` by equality (`item == call`) → the
identity assertion fails.

### 7. Spec + docs (same commit)
- specs/13 §6, new block after Decision P08-FIX-15 (or the last §6 decision):
  `> Decision (2026-11-05) P08-FIX-21:` a system tool run that goes on after its round was cut (§6 Decision
  P08-FIX-13 item 2) is answered `{"status":"in_progress"}`, not `{"error":"interrupted"}`: the message will
  normally be sent / the callback scheduled, and `interrupted` would make the model of the superseding or next
  turn do it again (a second text). The executor reports such calls (`ToolExecutor.detached(call)`, by object
  identity; `CallToolExecutor` notes a call when the wait for its executor-owned run is cancelled while the run
  is not), and `va_engine.tool_tasks.cut_contents` asks it for every executor call cancelled at the cut. A run
  cancelled by `aclose` and a custom tool (its request is cancelled) still read `interrupted`. The run's
  outcome is reported as before (`call.sms_sent` / `call.scheduled`, a failure as `call.error` `TOOL_FAILED`);
  the model is not told it later. Applies to task mode now and to pathway calls with P08-T02c2.
- Reword in place: §6 Decision P08-FIX-13 item 2 "The model gets the usual interruption answer" → "The model
  is told the run is in progress (Decision P08-FIX-21)"; §4 Decision P08-FIX-20 item 1 parenthesis → "(a
  system tool run behind the executor continues detached and reads `in_progress`, §6 Decision P08-FIX-21)" and
  its lead sentence stays true ("interrupted only when what it stands for did not happen"); §5 Decision
  P08-T02b1 item 4 gets a pointer. Keep spec ref + section on ONE line (`check_spec_refs`).
- AGENTS.md: bullet `2026-11-05 (P08-FIX-21)`: `ToolExecutor.detached(call)` (identity), `IN_PROGRESS`,
  `settle_round(..., executor=)`, the rule "never turn a cancelled wait into an answer" with the reason,
  fakes `RecordingExecutor(detaching=)` / `wait_started(name)`, cancel-the-consumer test pattern.
- Do NOT tick the plan or touch `.ralph/review-inbox/DONE` (Reviewer appends
  `2026-11-05-p08-fix-20-wip-review.md`).

### Gates
PATH needs `/home/ralph/.local/bin`. `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
packages/va_tools -q`; `uv run python scripts/check_spec_refs.py`; `make test` in the background (`: >
logs/make_test.log`, then `make test > logs/make_test.log 2>&1; echo exit=$? >> logs/make_test.log`, heartbeat
Monitor ~2 min); `npx --yes jscpd@4 --silent`; `uvx radon cc -s -a` on touched src files; pip-audit with the
CI export (`uv export --frozen --all-packages --no-hashes --no-emit-workspace`); `corepack pnpm audit
--audit-level high`. `make e2e` (background + heartbeat): `settle_round` is on the path of every tool round.

### Risks
- mypy: every structural `ToolExecutor` needs `detached` (media/test fakes outside va_engine would surface in
  `make typecheck`; grep `def handles(` found only the three above).
- The held-first cancel test: cancelling the consumer cancels the tool task it awaits (asyncio forwards the
  cancel to the awaited future), so `track` ends cancelled before the `finally` runs; that is the path the
  note wants pinned. Do not shield the awaits.
- jscpd: the engine and TaskBrain twins share shape; factor a helper rather than duplicating blocks.
- Known flake (task fix:sim-shared-redis-exit-flake): `apps/sim/tests/test_sim_shared_redis.py::
  test_two_sim_processes_share_scenarios_and_reset`; if it fails, re-run alone and report both runs.

## Build P08-FIX-21 (2026-11-05)
Implemented the plan as written (no deviations in behaviour).
- `va_engine.task_tools.IN_PROGRESS`; `ToolExecutor.detached(call)` (protocol); `tool_tasks._cut_content` /
  `cut_contents(..., executor=None)` / `settle_round(..., executor=)` (keyword required); both runners pass their
  executor (`run_node_round`: the node's, `TaskBrain._run_tools`: `self._executor`).
- `CallToolExecutor._run_system` notes the call in `_detached` when the shielded wait is cancelled while the run
  task is not (`task.cancelled()` false), re-raises; `detached` compares by identity.
- Fakes: `RecordingExecutor(detaching=)`, `left_running`, `detached`, `wait_started(name)`; `NodeExecutor.detached`.
- Tests: test_tool_tasks.py +4 (in_progress vs interrupted, identity via an equal twin plan, finished answer
  wins, settle_round with the executor); test_engine_tools.py +2 parametrized (reply task cancelled while the
  tools are awaited, held first / held second; cut system tool in_progress vs interrupted + superseding turn);
  test_task_tool_speech.py +3 (same cancel test parametrized over which tool is held, in_progress carried by a
  superseding turn, non-detached send_sms interrupted); va_tools test_executor.py +4 (detached by identity before
  and after the run ends, answered call not detached, aclose-cancelled run not detached, custom tool cancelled
  mid-request not detached and no event); test_executor_barge_in.py now expects `[IN_PROGRESS]`, sent once.
  One extra helper beyond the plan: `_system_executor(tool, events)` in test_executor.py (jscpd found a 5-line
  clone between two new tests; 0 clones after).
- Spec: specs/13 §6 Decision P08-FIX-21 (4 items) + rewording of §6 P08-FIX-13 item 2, §4 P08-FIX-20 item 1,
  §5 P08-T02b1 item 4. AGENTS.md bullet. Plan not ticked, DONE untouched.
Gates (final tree; PATH with /home/ralph/.local/bin): `make fmt` 0, `make lint` 0, `make typecheck` 0 (mypy:
no issues in 1614 source files), `make placeholders` 0 findings, `check_spec_refs` 0 problems, jscpd 0 clones,
radon repo average 2.42 (`_cut_content` B 8, `_run_tools` B 10 unchanged), pip-audit (CI export) no known
vulnerabilities, `pnpm audit --audit-level high` exit 0 (1 known moderate), `make test` exit 0: 13086 passed,
7 skipped, coverage 99.08 % (tool_tasks.py, tool_executor.py, task_tools.py, engine_node_tools.py 100 %;
va_tools/executor.py 99 %, the one partial branch 188->190 is the pre-existing crash-without-events path),
`make e2e`: run 1 FAILED, run 2 passed (details under "e2e" below). A first `make test` run was started before the jscpd helper refactor of
test_executor.py; I killed it and re-ran the whole suite on the final tree (the numbers above are that run).
Mutation checks (file restored byte-identical each time, sha256 asserted): (a) `_cut_content` never asks the
executor → 5 tests fail (unit x2, engine, TaskBrain, barge-in); (b) `_run_system` notes every cancellation →
`test_a_run_cancelled_by_aclose_is_not_detached` fails; (c) `detached` by equality →
`test_a_call_whose_wait_was_cancelled_mid_run_is_detached` fails; (d) `settle_round` drops the finished answers →
the 4 "cancelled while its tools run" tests fail.

e2e (both runs on the final tree; logs `logs/make_e2e_run1.log`, `logs/make_e2e.log`):
- Run 1 (15:21Z, images rebuilt, started right after `make test` ended): exit 2, `1 failed, 19 passed, 107
  deselected in 421.62s`. Failed: `tests_e2e/test_concurrent_calls.py::test_concurrent_calls_on_one_media_worker`
  `assert underruns == 0` got 4 (50 calls, 0 failed scenarios, overhead p50 115 / p95 172 ms, lag p99 <= 25 ms).
  Attribution from the media log (AGENTS.md P08-FIX-3): the 4 `outbound.underrun` lines sit within 12 ms
  (15:24:26.976-.988Z), `wake_late_ms` 63.5-69.5, `prev_send_await_ms` 0.1, `prev_resume_ms` 0.0 = one ~70 ms
  media event-loop stall, 0.9 s after the 50 sessions attached (the log around it is `media.amd_verdict`
  lines); no `gc_pause` at that moment (the two passes of that minute are 17 ms at 15:24:25.49 and 15:24:36.53).
  The flow's only tool round is the closing `end_call` of each call (`settle_round` with no tasks), ~30 s
  later, so the stall is not in code this task changes.
- Run 2 (15:33Z, stack reused): exit 0, `20 passed, 107 deselected in 417.73s`.
- Capacity flow alone, 3 more runs on the same stack (`uv run pytest tests_e2e -m capacity -s`): 3/3 passed,
  0 underruns each, overhead p50 103 / 125 / 118 ms, p95 171 / 167 / 171 ms.
So 1 of 5 capacity runs failed, on the cold stack. Not fixed here (one task per activation, unrelated path);
filed as runtime task `fix:capacity-start-burst-stall` with a fix memory. For the Reviewer: this is an
intermittent failure of a gate, reported as such, not hidden; a re-run decides whether it reproduces.

## Review P08-FIX-21 (2026-11-05) — APPROVED
Re-ran on the builder's tree (PATH with /home/ralph/.local/bin): `make lint` 0, `make typecheck` 0, `make
placeholders` 0 findings, `check_spec_refs` 0 problems, jscpd 0 clones, radon average 2.93 (no C+ block in
tool_tasks.py / va_tools/executor.py), pip-audit (CI export) clean, `pnpm audit --audit-level high` exit 0 (1 known
moderate), `make test` exit 0: 13086 passed, 7 skipped, coverage 99.08 % (tool_tasks, tool_executor, task_tools,
engine_node_tools 100 %; va_tools/executor 99 %, branch 188->190 pre-existing), `make e2e` exit 0: 20 passed in
419 s, started right after `make test` ended (logs/make_e2e_review.log); the capacity flow passed, so the
builder's run-1 underruns did not reproduce (task fix:capacity-start-burst-stall stays open).
Diff read against specs/13 §4/§5/§6: identity holds from `PlannedCall.call` through `start_tools` to
`_cut_content`; `settle_round` awaits `stop_tasks` before asking the executor; `_run_system` re-raises every
cancellation; `ToolExecutor` has only `CallToolExecutor` and the two test fakes as implementations.
Mutation checks on a shadow copy (PYTHONPATH over the editable install, repo untouched), the 5 touched test
files (133 tests): every cancellation detaches → 1 fails; `detached` by equality → 1; never detached → 2; cut
never asks the executor → 5; every cancelled call `in_progress` → 19; cancelled wait returned as an answer → 5.
Not changed (older general wording, the newer Decision governs): specs/11 §6 item 4 and specs/13 §4 Decision
P08-T02c1 item 3 still say a cancelled tool reads `interrupted` and point to Decision P08-FIX-20, whose item 1
carries the `in_progress` exception.
For the Planner: `.ralph/review-inbox/2026-11-05-p08-fix-21-wip-review.md` is not in DONE yet (no blockers;
item 1 = `in_progress` should be a per-tool property before `lookup_knowledge` becomes a `SystemTool`, item 2 =
an `in_progress` answer is never resolved, item 3 = documented window). Appended
`2026-11-05-p08-fix-20-wip-review.md` to DONE.

## Planner (2026-11-05, after P08-FIX-21 approval)
Inbox: `2026-11-05-p08-fix-21-wip-review.md` is new (no blockers). Inserted P08-FIX-22 at the top of
plan/phase-08.md for item 1; items 2 and 3 are closed with a written reason in the task text (DEC-015 in
`.ralph/agent/decisions.md`). P08-T10 acceptance now requires `lookup_knowledge` to declare `detaches = False`.
P08-FIX-18 (note 2026-11-05-p08-fix-15-review.md) stays the next unticked task after this one, then P08-T02c2.

## Plan P08-FIX-22 — "the run goes on after a cut" is a property of the system tool (`SystemTool.detaches`)

### Findings (read at HEAD 5b36fe9)
- `packages/va_tools/src/va_tools/executor.py` `_run_system` (l.168-178) shields EVERY `system=` tool and notes
  it detached when its wait is cancelled. Right for `send_sms`/`schedule_callback` (a side effect the caller
  asked for). Wrong for a read-only tool: `lookup_knowledge` is reserved, so P08-T10 can only add it through
  `system=`; a cut round would then answer it `in_progress`, which is never followed by the result, so the
  model neither gets the knowledge nor looks again. `interrupted` is the true answer there.
- `SystemTool` implementations: `va_media.call_system_tools.ToolSms` / `CallbackScheduler` (`name: Final = ...`
  class attributes) and the test fakes `_System`, `_GatedSystem` (va_tools `tests/test_executor.py`), `_HeldSms`
  (`tests/test_executor_barge_in.py`). `va_engine` is not touched: it only asks `ToolExecutor.detached(call)`.

### 1. `packages/va_tools/src/va_tools/executor.py`
- `SystemTool` protocol gains a read-only member (no default; every tool states it):
  ```python
  @property
  def detaches(self) -> bool:
      """Whether a run goes on when the turn waiting for it is cut (a side effect the caller asked for:
      ``send_sms``, ``schedule_callback``). A tool without one (a lookup) is cancelled with its wait."""
      ...
  ```
- `_run_system`: first lines
  ```python
  if not tool.detaches:  # nothing to finish for nobody: the run ends with the wait for it
      outcome = await self._finish_system(tool, call)
      return ToolAnswer(outcome.content)
  ```
  then the existing executor-owned shielded task + `_detached` note, unchanged. A non-detaching run is in the
  waiting task: its cancellation propagates out of `tool.run` through `_finish_system` (which already
  re-raises `CancelledError` without reporting), it is never in `_pending` (nothing for `aclose`), never in
  `_detached` (→ `cut_contents` answers `interrupted`); failure outcome and crash are reported by
  `_finish_system` exactly as for detaching tools (a crash reaches `execute_gated` → `tool_failed`).
- Module docstring, third paragraph: "A system tool that ``detaches`` has a side effect the caller asked for,
  so its body runs to completion ... (Decision P08-FIX-13)"; add: "A system tool that does not (a lookup) runs
  in the waiting task and is cancelled with it: a cut round answers it ``interrupted`` and the model asks again
  (Decision P08-FIX-22 under specs/13 §6)." `detached` docstring: mention "a system tool that does not detach".

### 2. `apps/media_worker/src/va_media/call_system_tools.py`
- `ToolSms`: `detaches: Final = True` under `name`; `CallbackScheduler`: same. Module docstring: one sentence
  (both runs go on after a cut round, Decision P08-FIX-22).

### 3. Tests
`packages/va_tools/tests/test_executor.py`
- `_System`: field `detaches: bool = True` (after `outcome`, before `calls`); `_GatedSystem`: `detaches: bool =
  True` (after `outcome`).
- Parametrize over `detaches` (`@pytest.mark.parametrize("detaches", [True, False])`, booleans only) the three
  behaviours both kinds share: `test_a_system_tool_answers_without_a_tool_event`,
  `test_a_failed_system_tool_reports_tool_failed`, `test_an_awaited_crash_propagates_to_the_caller`.
- New `test_a_tool_that_does_not_detach_is_cancelled_with_its_wait`: `_GatedSystem("lookup_knowledge",
  SystemOutcome('{"results":[]}'), detaches=False)`, `call = _system_call(tool)`, `events = _Events()`,
  `executor, _ = await _cancel_mid_run(tool, events, call)` → `tool.cancelled`, `tool.finished == []`,
  `not executor.detached(call)`, `events.failures == []`; then `tool.gate.set()`; `stream = json_log_stream()`
  taken before; `await executor.aclose(timeout_s=0)` → still `tool.finished == []` and no
  `tools.system_tools_abandoned` / `tools.system_tool_ran` event in the log (nothing was pending).
- New `test_a_tool_that_does_not_detach_leaves_nothing_to_drain_after_it_answered` is NOT needed (covered by
  the parametrized answer test); do not add tests that restate the same branch.
`packages/va_tools/tests/test_executor_barge_in.py`
- `_HeldSms`: fields `detaches: bool = True`, `cancelled: bool = False` (set in an `except
  asyncio.CancelledError: self.cancelled = True; raise` around the gate wait).
- Factor the body into `async def _barge_in(sms) -> tuple[TaskBrain, CallToolExecutor, ToolCall]` (builds the
  brain, reads up to the filler `SpeakStatic`, `await reply.aclose()`), used by the existing test and by
  `test_a_barge_in_cancels_a_system_tool_that_does_not_detach`: `_HeldSms(detaches=False)` → `sms.cancelled`,
  `not executor.detached(call)`, tool messages `== [INTERRUPTED]` (the `va_engine.task_tools` constant for
  `{"error":"interrupted"}`; check its exported name there), then `sms.gate.set(); await executor.aclose()` →
  `sms.sent == []`. The existing test additionally asserts `not sms.cancelled`. Update the module docstring
  (Decision P08-FIX-22). Keep jscpd at 0 (the helper is the point).
`apps/media_worker/tests/test_call_system_tools.py`
- `test_both_system_tools_run_on_when_their_round_is_cut`: `assert ToolSms.detaches is True` and
  `CallbackScheduler.detaches is True` (class attributes, no rig).

Mutation checks to report (restore byte-identical, sha256): (a) `_run_system` without the `detaches` branch
(always shields) → the two new non-detaching tests fail; (b) the branch taken for every tool (never shields) →
the existing detached / barge-in `in_progress` / "lets the system tool finish" tests fail; (c)
`ToolSms.detaches = False` → the media test fails.

### 4. Spec + docs (same commit)
- specs/13 §6, new block directly after the Decision P08-FIX-21 block (before "Decision (2026-11-02): the end
  reason ..."), keep `specs/NN §N` references on ONE line:
  `> Decision (2026-11-05) P08-FIX-22:` whether a run goes on after its round was cut is a property of the
  tool, not of being a system tool: `va_tools.executor.SystemTool.detaches`.
  1. `send_sms` and `schedule_callback` declare `detaches = True` (a side effect the caller asked for):
     Decision P08-FIX-13 item 2 and Decision P08-FIX-21 apply to them unchanged.
  2. A system tool without a side effect declares `detaches = False` (the `lookup_knowledge` of P08-T10): its
     run happens in the task that waits for it and is cancelled with it; the executor neither owns nor drains
     it (`aclose`), it is never `detached`, and a cut round answers it `{"error":"interrupted"}`.
     `in_progress` would be wrong there: that answer is never followed by the result, so the model would
     neither have the knowledge nor look again. A failure outcome or a crash is reported like for every system
     tool (`call.error` `TOOL_FAILED`).
  3. Not changed, on purpose: an `in_progress` answer is not replaced by the run's outcome later (Decision
     P08-FIX-21 item 4). It would matter only when a barge-in lands inside a run that then fails; the failure
     is on the call (`call.error` `TOOL_FAILED`), and rewriting the answer needs a link from the call to a
     history record that survives superseded turns and the persisted engine state in both runners.
- Reword in place: Decision P08-FIX-13 item 2 lead "A system tool run is a side effect the caller asked for"
  → "A `send_sms` / `schedule_callback` run is a side effect the caller asked for (the tool says so:
  `SystemTool.detaches`, Decision P08-FIX-22)"; Decision P08-FIX-21 item 1 after "`CallToolExecutor` notes a
  call": "(of a tool that detaches, Decision P08-FIX-22)".
- AGENTS.md bullet `2026-11-05 (P08-FIX-22)`: every `SystemTool` declares `detaches` (protocol member, no
  default; `Final` class attribute or dataclass field); true = executor-owned shielded run, `in_progress` at a
  cut, drained by `aclose`; false = runs in the waiting task, cancelled with it, `interrupted`;
  `lookup_knowledge` (P08-T10) must be false; test fakes `_GatedSystem(..., detaches=False)`.
- Do NOT tick the plan or touch `.ralph/review-inbox/DONE` (the Reviewer appends
  `2026-11-05-p08-fix-21-wip-review.md`). The plan file and `.ralph/agent/decisions.md` (DEC-015) already
  carry the Planner's edits; commit them with the task.

### Gates
PATH needs `/home/ralph/.local/bin`. `make fmt lint typecheck placeholders`; `uv run pytest packages/va_tools
packages/va_engine apps/media_worker/tests/test_call_system_tools.py -q`; `uv run python
scripts/check_spec_refs.py`; `make test` in the background (`: > logs/make_test.log`, then `make test >
logs/make_test.log 2>&1; echo exit=$? >> logs/make_test.log`, heartbeat Monitor ~2 min); `npx --yes jscpd@4
--silent`; `uvx radon cc -s -a` on the touched src files; pip-audit with the CI export (`uv export --frozen
--all-packages --no-hashes --no-emit-workspace`); `corepack pnpm audit --audit-level high`; `make e2e`
(background + heartbeat; the executor is on the path of every in-call tool).

### Risks
- mypy: a `Final` class attribute / dataclass field must satisfy the protocol's read-only property (as `name`
  already does); any other structural `SystemTool` would surface in `make typecheck` (grep found none).
- Known flakes, report both runs if hit: `apps/sim/tests/test_sim_shared_redis.py::
  test_two_sim_processes_share_scenarios_and_reset` (task fix:sim-shared-redis-exit-flake); the capacity flow
  on a cold stack (task fix:capacity-start-burst-stall; attribute with the `outbound.underrun` lines).

## Build P08-FIX-22 (2026-11-05)
Implemented exactly the plan, no deviations.
- `packages/va_tools/src/va_tools/executor.py`: `SystemTool.detaches` (read-only protocol property, no default);
  `_run_system` runs a tool with `detaches` false in the waiting task (`_finish_system` directly: never in
  `_pending`, never in `_detached`), the shielded executor-owned path is unchanged for the others; module and
  `detached` docstrings.
- `apps/media_worker/src/va_media/call_system_tools.py`: `ToolSms.detaches: Final = True`,
  `CallbackScheduler.detaches: Final = True`, one docstring sentence.
- Tests: va_tools `test_executor.py` (`_System`/`_GatedSystem` field `detaches`, the answer / failure / awaited
  crash tests parametrized over it, new `test_a_tool_that_does_not_detach_is_cancelled_with_its_wait`);
  `test_executor_barge_in.py` (`_HeldSms.detaches`/`cancelled`, helper `_barge_in(sms)` + `_tool_messages`, new
  `test_a_barge_in_cancels_a_system_tool_that_does_not_detach`, the existing test also asserts `not
  sms.cancelled`); media `test_both_system_tools_run_on_when_their_round_is_cut`.
- Spec: specs/13 §6 Decision P08-FIX-22 (3 items) + pointers in Decision P08-FIX-13 item 2 and Decision
  P08-FIX-21 item 1. AGENTS.md bullet. Plan not ticked, DONE untouched.
Gates (final tree; PATH with /home/ralph/.local/bin): `make fmt` 0, `make lint` 0, `make typecheck` 0, `make
placeholders` 0 findings, `check_spec_refs` 0 problems, jscpd 0 clones, radon repo average 2.93 (touched src
files average 2.65, no C+ block), pip-audit (CI export) no known vulnerabilities, `pnpm audit --audit-level high`
exit 0 (1 known moderate), `make test` exit 0: 13092 passed, 7 skipped (13086 + 6 new), coverage 99.09 %
(va_tools/executor.py 99 %, the one partial branch 202->204 is the pre-existing crash-without-events path, was
188->190; call_system_tools.py 96 % unchanged lines).
Mutation checks (sources restored byte-identical, sha256 compared): (a) `_run_system` always shields → the 2
new non-detaching tests fail; (b) never shields → 7 fail (finish-and-report, aclose cancel, unawaited crash,
detached, aclose-not-detached, barge-in in_progress, media callback-still-starts); (c) `ToolSms.detaches =
False` → the media test fails.

e2e (both on the final tree; logs `logs/make_e2e_run1.log`, `logs/make_e2e.log`):
- Run 1 (16:26Z, images rebuilt, started right after `make test`): exit 2, `1 failed, 19 passed, 107
  deselected in 419.06s`. Failed: `test_concurrent_calls_on_one_media_worker` `assert underruns == 0` got 11
  (50 calls, 0 failed scenarios, overhead p50 110 / p95 155 ms). Media log (`logs/e2e_run1_underruns.log`):
  the 11 `outbound.underrun` lines sit within 62 ms (16:29:59.909-.972Z), `wake_late_ms` 60-70,
  `prev_send_await_ms` ~0, `prev_resume_ms` ~0, `turn=0` = one ~65 ms media event-loop stall inside the
  session start burst (`media.session_started` / `carrier_answered` / `amd_verdict` lines around it); no
  `gc_pause` at that moment (16 ms at 16:29:58.57, 17 ms at 16:30:09.60). The flow runs no system tool
  (`_run_system` is never entered), so the change is not on that path. Second occurrence of runtime task
  `fix:capacity-start-burst-stall` (still open; fix memory mem-1793896618-d08e added).
- Run 2 (16:35Z, stack reused): exit 0, `20 passed, 107 deselected in 415.82s`.
- Capacity flow alone, 2 more runs on the same stack: 2/2 passed, 0 underruns, overhead p50 102 / 102 ms,
  p95 164 / 168 ms.
So 1 of 4 capacity runs failed, on the cold stack, like in P08-FIX-21 (1 of 5). Reported as an intermittent
gate failure, not hidden; the Reviewer's re-run decides.

## Review P08-FIX-22 (2026-11-05) — APPROVED
Re-ran on the builder's tree (PATH with /home/ralph/.local/bin): `make lint` 0, `make typecheck` 0, `make
placeholders` 0 findings, `check_spec_refs` 0 problems, jscpd 0 clones, radon average 2.93 (no C+ block in
va_tools/executor.py / call_system_tools.py), pip-audit (CI export) clean, `pnpm audit --audit-level high` exit 0
(1 known moderate), `make test` exit 0: 13092 passed, 7 skipped, coverage 99.08 % (va_tools/executor.py 99 %,
partial branch 202->204 pre-existing; call_system_tools.py 96 %, unchanged lines), `make e2e` exit 0: 20 passed
in 417.8 s, started 17:01Z right after `make test` ended (logs/make_e2e_review.log); the capacity flow passed,
so the builder's run-1 underruns did not reproduce (task fix:capacity-start-burst-stall stays open).
Diff read against specs/13 §6 (Decisions P08-FIX-13 item 2, P08-FIX-21, P08-FIX-22): the non-detaching path
awaits `_finish_system` in the waiting task (never in `_pending`/`_detached`, cancellation re-raised without a
report, failure/crash reported like the detaching path); the detaching path is unchanged; `SystemTool`
implementers are `ToolSms`, `CallbackScheduler` and the three test fakes, all declare `detaches`.
Mutation checks on a shadow copy (PYTHONPATH over the editable install, repo untouched; va_tools
test_executor.py + test_executor_barge_in.py = 27 tests): always shield → the 2 non-detaching tests fail; never
shield → 6 fail; `ToolSms.detaches = False` → the media class test fails.
Reviewer edits: rewrapped the over-long line in Decision P08-FIX-21 item 1 (wording unchanged); ticked the plan;
appended `2026-11-05-p08-fix-21-wip-review.md` to DONE.
For the Planner: `.ralph/review-inbox/2026-11-05-p08-fix-22-wip-review.md` is new and not in DONE (no blockers;
item 1 = a non-detaching tool's `run` must not delay or swallow cancellation because `stop_tasks` waits for it
without a bound: one docstring/spec sentence and an assert in the P08-T10 cut test, or drop).
