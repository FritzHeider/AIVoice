# Phase 05 — Telephony Edge, Call Lifecycle, and Task-Mode Calling

- [x] P05-T01 Telephony edge: webhook intake — va_edge intake/keys/processor/reprocessor, migration 0005 process_attempts, TelnyxAccountLookupRepo, LRU-bounded BYO key caches, 1 MiB body cap (413); make lint/typecheck/placeholders 0, make test 7505 passed 7 skipped, coverage 98.57%
  - Spec refs: specs/06-telephony-telnyx.md (webhook verification, envelope, raw event storage); specs/01-architecture.md §2
  - Depends on: P03-T09, P01-T10
  - Deliverables: `POST /telnyx/voice`, `POST /telnyx/messaging` (stores raw events for later phase handling), signature verification on raw body (platform key and BYO account keys resolved via `connection_id`/`client_state`), dedupe on `telnyx_event_id`, fast 200 ACK then async processing task with backpressure
  - Acceptance: tests for valid/invalid/stale signatures, duplicates, and unknown event types (stored, ignored).

- [x] P05-T02 Carrier event mapping and call/leg state machine — plan_effects for every specs/06 §5 type + §5.1 hangup table, CallRepo.transition guarded state machine, CarrierEventHandler (DB → carrier_state → events → channel → signal), TemporalCarrierSignaller; make test 7784 passed/7 skipped (98.57 %), lint/typecheck/placeholders 0, jscpd 0 clones, radon avg A
  - Spec refs: specs/06-telephony-telnyx.md (event → domain mapping, hangup causes, AMD results); specs/05-calls-api.md (status state machine); specs/19-events-and-webhooks.md (event catalog)
  - Depends on: P05-T01
  - Deliverables: `va_edge.mapping` (every voice event type), leg/call transitions with guard checks, Redis `call:{id}:carrier` publish + `carrier_state` hash, Temporal signal to `CallWorkflow`, domain events via publisher
  - Acceptance: table-driven tests for every event type and illegal transition handling.

- [x] P05-T03 Send Call validation and resolved configuration (split into P05-T03a–c below; tick when all three are ticked) — P05-T03a/b/c all ticked (3178218)
  - Spec refs: specs/05-calls-api.md (POST /v1/calls, every parameter, SendCallConfig, runner selection and precedence, validation order)
  - Depends on: P02-T03, P02-T12
  - Deliverables: `SendCallRequest` pydantic model (every parameter, aliases, defaults), `resolve_call_config(...)` implementing precedence incl. request_data/metadata replacement semantics, runner resolution for task and pathway (+ version semantics), language/voice resolution, E.164 normalization
  - Acceptance: exhaustive parameter tests; snapshot of resolved config for representative requests.

- [x] P05-T03a Send Call request and config models, E.164 normalization, shared language table — va_domain.calls (CallParameters/SendCallRequest/SendCallConfig, explicit_fields, check_rules), va_domain.phone, va_domain.languages; 12 Bland fixtures; make lint/typecheck/placeholders/test green (8175 passed, 98.58 %)
  - Spec refs: specs/05-calls-api.md §3, §4.1 steps 3–4, §4.4, §10 (request model tests); specs/08-stt-elevenlabs.md §5; specs/13-tools-and-dynamic-data.md §2, §7
  - Depends on: P02-T12
  - Deliverables: `va_domain.calls` (`CallParameters`, `SendCallRequest`, every §3 parameter with validation/aliases, `explicit_fields()`, `check_rules()` → `CallRequestError` for `RUNNER_CONFLICT`/`RETRY_NOT_PROVIDED`; `SendCallConfig` + sub-models of §4.4; `InlineTool`, `DynamicDataSpec`), `va_domain.phone.normalize_e164`, language table moved to `va_domain.languages` (va_elevenlabs re-uses it), 12 Bland sample requests in `apps/api/tests/fixtures/bland_send_call/`
  - Acceptance: every parameter valid/invalid boundary parametrized; aliases; unknown fields ignored; §4.4 field-name drift test; all 12 fixtures parse.

- [x] P05-T03b Pure `resolve_call_config` precedence — va_domain.call_resolution (merge_layers/resolve_call_config, org/pathway/persona/agent layers, forced rails, model mapping, start_time window), call_defaults validated on PATCH; 74-case precedence table + 12 resolved Bland snapshots; make lint/typecheck/placeholders/test green (8307 passed, 98.60 %)
  - Spec refs: specs/05-calls-api.md §4.3, §4.4, §3 (defaults and semantics)
  - Depends on: P05-T03a
  - Deliverables: `va_domain.call_resolution.resolve_call_config(org, request, runner, refs, now)` (pure; layers platform → org `call_defaults` → runner defaults (task, pathway `global_config.call_defaults`; persona/agent layer shapes ready for P13-T07) → explicit fields), request_data/metadata replacement, tools union, dispositions + built-ins, guard rails union, record forcing, ivr_mode effects, model mapping, start_time localization/window
  - Acceptance: ≥ 40-case precedence table; snapshot of resolved configs for the 12 Bland fixtures.

- [x] P05-T03c Send Call input loading: runner, voice, tools, from number — va_api.services (call_inputs/call_runners/call_pathways/call_refs/caller_id), va_common.nanp + generated npa_states.json, pathway_call_defaults; 49 new api tests (unit + Postgres), new modules 100 % line+branch; make lint/typecheck/placeholders/test pass (8383 passed, 7 skipped, 98.63 %), jscpd 0 clones, radon ≤ B, npa --check clean
  - Spec refs: specs/05-calls-api.md §3.1–§3.4, §4.1 steps 4, 5, 7, 8; specs/16-phone-numbers.md §8 (local selection)
  - Depends on: P05-T03b
  - Deliverables: `apps/api/src/va_api/services/call_inputs.py` loading everything `resolve_call_config` needs inside one `tenant_session`: pathway + version semantics (production/staging/integer fallback, draft for test calls, warnings), voice by name/id (org first, then platform; org default voice), tool/KB existence, `encrypted_key` account, destination parse (`INVALID_PHONE_NUMBER`), `from` ownership + `local` dialing strategy (`va_common.nanp`), `AGENT_REQUIRES_ORG_KEY`, `test_call`/`test_mode` permission checks
  - Acceptance: DB tests per validation step proving order (earlier failures mask later ones) and each error code/body.

- [ ] P05-T04 Dispatch checks: limits, compliance, credit hold (split into P05-T04a–b below; tick when both are ticked)
  - Spec refs: specs/24-billing-and-limits.md (limit enforcement algorithms, credit holds); specs/23-guardrails-and-compliance.md (DNC, blacklist, calling hours); specs/31-error-catalog.md
  - Depends on: P05-T03c
  - Deliverables: `va_billing.limits` (daily/hourly/concurrency/same-number/new-account/international), `va_compliance.dnc.check`, calling-hours check when enforced, `va_billing.holds` (create/capture/release), error codes exactly as catalogued
  - Acceptance: Redis algorithm tests with fake clock; hold sizing tests.

- [x] P05-T04a Dispatch compliance checks: DNC, international policy, calling hours — va_compliance (fraud/dnc/international/calling_windows/state_rules/calling_hours/check_call_compliance); make test 8491 passed, va_compliance 100 %, jscpd 0, radon ≤ B
  - Spec refs: specs/23-guardrails-and-compliance.md §2.1, §3; specs/17-batches-and-scheduling.md §3.1, §3.2, §7; specs/24-billing-and-limits.md §6 item 7 (policy half); specs/05-calls-api.md §4.1 step 9; specs/31-error-catalog.md
  - Depends on: P05-T03c
  - Deliverables: new package `packages/va_compliance` (`dnc.find_block` org + platform + parent inheritance via `use_org`, `international.check_destination` (allowed list, home country, US/CA, `international_enabled`/kyc ≥ 2), `calling_windows.callee_timezones` + `in_window`, `state_rules` + `data/state_rules.json` with statute/source_url/last_verified, `calling_hours.check_calling_hours`), `compliance.check_call_compliance(...)` raising `NUMBER_BLOCKED`/`INTERNATIONAL_NOT_ENABLED`/`OUTSIDE_CALLING_HOURS` in §4.1 order; `INTERNATIONAL_ALLOWED_COUNTRIES` in api settings
  - Acceptance: DB tests for DNC scopes/expiry/inheritance/cross-tenant; international matrix; calling-window table tests across DST and multi-zone NPAs; state rule data test; order test.

- [ ] P05-T04b Dispatch limits and credit holds
  - Spec refs: specs/24-billing-and-limits.md §2 (effective limits/prices), §5, §6 items 2–6 and 7 (rate half), §13; specs/17-batches-and-scheduling.md §7 (frequency caps); specs/05-calls-api.md §4.1 steps 10–11; specs/31-error-catalog.md
  - Depends on: P05-T04a
  - Deliverables: new package `packages/va_billing` (`plans.effective_limits`/`effective_pricing` (org `limits` override > plan), `limits` with Lua `capped_incr.lua`/`concurrency_acquire.lua` (same-number, frequency, new-account, daily, hourly, concurrency, international rate, reverse-order rollback, idempotent release), `holds` (sizing, reserve/capture/release, enterprise skip), `dispatch.admit_call(...)` running compliance → limits → hold with compensation)
  - Acceptance: Redis algorithm tests with fake clock (fakeredis) plus real-Redis testcontainer tests (capped_incr never exceeds cap under 100 concurrent increments; concurrency self-heal/idempotent release); rollback-order tests; hold sizing and available-balance tests.

- [ ] P05-T05 `POST /v1/calls` endpoint and CallWorkflow start
  - Spec refs: specs/05-calls-api.md (POST /v1/calls response); specs/01-architecture.md §2 (outbound steps 1–3)
  - Depends on: P05-T04, P05-T02
  - Deliverables: endpoint inserting `calls` row + `call.queued`/`call.scheduled` events in one transaction, Temporal start with workflow id `call-{id}`, Bland-compatible response
  - Acceptance: API tests incl. idempotency replay and every 4xx path.

- [ ] P05-T06 CallWorkflow and activities
  - Spec refs: specs/01-architecture.md §2, §3; specs/06-telephony-telnyx.md (dial parameters); specs/13-tools-and-dynamic-data.md (pre-call dynamic data, cache flag)
  - Depends on: P05-T05
  - Deliverables: `CallWorkflow` (scheduled wait, `prefetch_call_context`, `dial`, carrier/pipeline signals, backstop timer, heartbeat watch, hangup on failure, child `PostCallWorkflow` start), activities with retry policies, `CallContext` model stored at `call:{id}:ctx`; Telnyx hangups decided from carrier signals (`fax_detected` premium AMD; `streaming.failed` while in progress with no owner after 5 s), per specs/06 §5 Decision P05-T02 item 4
  - Acceptance: Temporal time-skipping tests for scheduled start, no-answer, worker crash backstop, normal completion.

- [ ] P05-T07 Media worker server, ownership, control channel, drain
  - Spec refs: specs/07-media-pipeline.md (worker WebSocket endpoints and auth, ownership lock, control channel schemas, graceful drain, admission control)
  - Depends on: P03-T09, P04-T12
  - Deliverables: `/ws/telnyx` endpoint (signature + stream token verification), ownership lock with renewal, heartbeat key, subscriptions to control/carrier channels, admission control (`MEDIA_MAX_CALLS_PER_POD`), draining state, `media:pods` registry
  - Acceptance: tests for duplicate stream attach (second rejected), lock loss abort, drain refusing new calls while finishing existing.

- [ ] P05-T08 Telnyx transport adapter
  - Spec refs: specs/07-media-pipeline.md (Transport protocol); specs/06-telephony-telnyx.md (media WS protocol)
  - Depends on: P05-T07
  - Deliverables: `TelnyxMediaTransport` implementing `Transport` (inbound frames via reorder buffer, outbound media/mark/clear, DTMF events, stop handling, hd_audio L16 path)
  - Acceptance: tests against FakeTelnyx media client.

- [ ] P05-T09 Task-mode Brain and system tools
  - Spec refs: specs/11-pathways-engine.md (Task-mode single prompt runner); specs/10-llm-gateway.md (dialogue system prompt, tool calling); specs/13-tools-and-dynamic-data.md (built-in system tools end_call, transfer_call, press_button)
  - Depends on: P03-T13, P04-T12
  - Deliverables: `va_engine.task_runner.TaskBrain` (prompt assembly, streaming reply, system tools, temperature, request_data templating, first_sentence handling)
  - Acceptance: harness tests with FakeLLM rules including end_call and transfer_call tool invocations.

- [ ] P05-T10 Greeting, AMD, voicemail actions, IVR mode
  - Spec refs: specs/05-calls-api.md (wait_for_greeting, voicemail object, ivr_mode/amd, retry); specs/07-media-pipeline.md (wait_for_greeting, first_sentence pre-render); specs/06-telephony-telnyx.md (AMD config, premium results); specs/10-llm-gateway.md (voicemail/IVR classifier prompt)
  - Depends on: P05-T08, P05-T09
  - Deliverables: greeting state machine, AMD result handling, `voicemail.action` hangup/leave_message/leave_message_and_sms/ignore (beep wait via `call.machine.premium.greeting.ended`), `sensitive` LLM detection, IVR mode overrides, voicemail SMS send via va_telnyx messaging
  - Acceptance: e2e flows voicemail_leave_message, voicemail_hangup, amd_ivr_mode pass.

- [ ] P05-T11 Call ending, persistence, max duration, stop API
  - Spec refs: specs/05-calls-api.md (POST /v1/calls/{id}/stop, stop all, §9 end reasons); specs/07-media-pipeline.md (max_duration timer, error phrase); specs/03-data-model.md (call_turns, call_events)
  - Depends on: P05-T08
  - Deliverables: end paths (agent, user hangup, timeout, API, error), Telnyx hangup, turn write-behind flush, final call row update, lock release, workflow signal, stop endpoints via control channel
  - Acceptance: e2e max_duration and stop_via_api pass; end_reason/ended_by correct for each path.

- [ ] P05-T12 Inbound call routing
  - Spec refs: specs/01-architecture.md §2 (inbound); specs/06-telephony-telnyx.md (inbound routing algorithm); specs/16-phone-numbers.md §4 (InboundConfig model)
  - Depends on: P05-T02, P05-T06
  - Deliverables: number lookup (system repo), reject/transfer/maintenance logic, call row + context creation with 1.5 s inline budget, `answer` with stream params, `InboundConfig` model (full API arrives in phase 10)
  - Acceptance: e2e inbound_basic passes; unknown number rejected; suspended org with fallback transfers.

- [ ] P05-T13 Cold transfer
  - Spec refs: specs/01-architecture.md §2 (transfers); specs/06-telephony-telnyx.md (cold transfer sequence); specs/05-calls-api.md (transfer_phone_number, transfer_list)
  - Depends on: P05-T09, P05-T11
  - Deliverables: transfer tool/command flow (final line → mark echo → Telnyx transfer), `transfer_list` selection, bridged handling, transfer_seconds accounting, AI pipeline shutdown
  - Acceptance: e2e cold_transfer passes with correct billing fields.

- [ ] P05-T14 Recording
  - Spec refs: specs/05-calls-api.md (record, GET recording); specs/06-telephony-telnyx.md (recording copy flow, call.recording.saved)
  - Depends on: P05-T06
  - Deliverables: record params on dial/answer, recording saved handling activity copying to S3 (`S3_BUCKET_RECORDINGS/{org}/{yyyy}/{mm}/{call_id}.wav`), signed URL generation (7 days)
  - Acceptance: tests with FakeTelnyx recording URL served by sim; S3 via MinIO testcontainer.

- [ ] P05-T15 Call audio features and options
  - Spec refs: specs/05-calls-api.md (precall_dtmf_sequence, ignore_button_press, keywords, language, background_track, noise_cancellation, interruptibility, block_interruptions, temperature, voice); specs/08-stt-elevenlabs.md (keyterms, language mapping)
  - Depends on: P05-T08, P05-T09
  - Deliverables: wiring of every listed option into the pipeline/STT/TTS/brain configuration
  - Acceptance: e2e interruptibility_levels and barge_in pass; unit tests for each option's effect.

- [ ] P05-T16 Call read endpoints
  - Spec refs: specs/05-calls-api.md (GET /v1/calls, GET /v1/calls/{id}, GET /v1/calls/active, transcript, recording, events); specs/19-events-and-webhooks.md §4 (Call Object)
  - Depends on: P05-T11
  - Deliverables: endpoints with Bland list filters and Call Object serializer (every field)
  - Acceptance: serializer test asserts every Call Object field from specs/19 §4 is present with correct type.

- [ ] P05-T18 PostCallWorkflow v1: billing finalization and completion event
  - Spec refs: specs/18-post-call-analysis.md (PostCallWorkflow step list, billing finalization hook); specs/24-billing-and-limits.md (metering rules, credit holds)
  - Depends on: P05-T06, P05-T11
  - Deliverables: `PostCallWorkflow` with the first steps that do not need later phases — recording copy (P05-T14), billing finalization (usage events for connected seconds, transfer seconds, attempt minimum; hold capture/release; `calls.price_micros`), `call.completed` event with the Call Object; later phases insert their steps at the positions defined in specs/18
  - Acceptance: Temporal tests; price on the Call Object equals the metering rule result for connected, transferred, and failed calls.

- [ ] P05-T17 End-to-end flows and concurrency check
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list)
  - Depends on: P05-T10, P05-T11, P05-T12, P05-T13, P05-T14, P05-T15, P05-T16, P05-T18
  - Deliverables: `tests_e2e/` harness (compose profile e2e, fixtures to start services, scenario helpers) and flows outbound_basic, inbound_basic, barge_in, worker_crash_recovery; a 50-concurrent-call simulator test against one media worker process
  - Acceptance: `make e2e` green; concurrency test reports no underruns and p50 platform overhead ≤ 150 ms.

## Phase exit gate
`make check && make e2e` pass with all flows listed in prompts/phase-05.md.
