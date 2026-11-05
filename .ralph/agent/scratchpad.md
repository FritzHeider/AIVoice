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
