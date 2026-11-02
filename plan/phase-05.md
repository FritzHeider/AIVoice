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

- [x] P05-T04 Dispatch checks: limits, compliance, credit hold (split into P05-T04a–b below; tick when both are ticked) — P05-T04a/b both ticked (0d28237)
  - Spec refs: specs/24-billing-and-limits.md (limit enforcement algorithms, credit holds); specs/23-guardrails-and-compliance.md (DNC, blacklist, calling hours); specs/31-error-catalog.md
  - Depends on: P05-T03c
  - Deliverables: `va_billing.limits` (daily/hourly/concurrency/same-number/new-account/international), `va_compliance.dnc.check`, calling-hours check when enforced, `va_billing.holds` (create/capture/release), error codes exactly as catalogued
  - Acceptance: Redis algorithm tests with fake clock; hold sizing tests.

- [x] P05-T04a Dispatch compliance checks: DNC, international policy, calling hours — va_compliance (fraud/dnc/international/calling_windows/state_rules/calling_hours/check_call_compliance); make test 8491 passed, va_compliance 100 %, jscpd 0, radon ≤ B
  - Spec refs: specs/23-guardrails-and-compliance.md §2.1, §3; specs/17-batches-and-scheduling.md §3.1, §3.2, §7; specs/24-billing-and-limits.md §6 item 7 (policy half); specs/05-calls-api.md §4.1 step 9; specs/31-error-catalog.md
  - Depends on: P05-T03c
  - Deliverables: new package `packages/va_compliance` (`dnc.find_block` org + platform + parent inheritance via `use_org`, `international.check_destination` (allowed list, home country, US/CA, `international_enabled`/kyc ≥ 2), `calling_windows.callee_timezones` + `in_window`, `state_rules` + `data/state_rules.json` with statute/source_url/last_verified, `calling_hours.check_calling_hours`), `compliance.check_call_compliance(...)` raising `NUMBER_BLOCKED`/`INTERNATIONAL_NOT_ENABLED`/`OUTSIDE_CALLING_HOURS` in §4.1 order; `INTERNATIONAL_ALLOWED_COUNTRIES` in api settings
  - Acceptance: DB tests for DNC scopes/expiry/inheritance/cross-tenant; international matrix; calling-window table tests across DST and multi-zone NPAs; state rule data test; order test.

- [x] P05-T04b Dispatch limits and credit holds — va_billing (plans, keys, Lua limit scripts, CallLimiter with rollback, org-scoped holds, admit_call); DESTINATION_FREQUENCY_CAP; make test 8561 passed / 7 skipped, 98.65 %; va_billing 100 %; lint/typecheck/placeholders pass; jscpd 0; radon A
  - Spec refs: specs/24-billing-and-limits.md §2 (effective limits/prices), §5, §6 items 2–6 and 7 (rate half), §13; specs/17-batches-and-scheduling.md §7 (frequency caps); specs/05-calls-api.md §4.1 steps 10–11; specs/31-error-catalog.md
  - Depends on: P05-T04a
  - Deliverables: new package `packages/va_billing` (`plans.effective_limits`/`effective_pricing` (org `limits` override > plan), `limits` with Lua `capped_incr.lua`/`concurrency_acquire.lua` (same-number, frequency, new-account, daily, hourly, concurrency, international rate, reverse-order rollback, idempotent release), `holds` (sizing, reserve/capture/release, enterprise skip), `dispatch.admit_call(...)` running compliance → limits → hold with compensation)
  - Acceptance: Redis algorithm tests with fake clock (fakeredis) plus real-Redis testcontainer tests (capped_incr never exceeds cap under 100 concurrent increments; concurrency self-heal/idempotent release); rollback-order tests; hold sizing and available-balance tests.

- [x] P05-T05 `POST /v1/calls` endpoint and CallWorkflow start (split into P05-T05a–b below; tick when both are ticked) — P05-T05a + P05-T05b
  - Spec refs: specs/05-calls-api.md (POST /v1/calls response); specs/01-architecture.md §2 (outbound steps 1–3)
  - Depends on: P05-T04, P05-T02
  - Deliverables: endpoint inserting `calls` row + `call.queued`/`call.scheduled` events in one transaction, Temporal start with workflow id `call-{id}`, Bland-compatible response
  - Acceptance: API tests incl. idempotency replay and every 4xx path.

- [x] P05-T05a Shared destination URL guard and Send Call webhook policy — va_common.net check_destination_url/blocked_address, WebhookSafetySettings mixin, check_call_webhook; make test 8653 passed / 7 skipped, 98.65 %; lint/typecheck/placeholders/spec refs 0
  - Spec refs: specs/19-events-and-webhooks.md §8 (SSRF, scheme rules); specs/28-security.md §6.4 (extra blocked ranges); specs/13-tools-and-dynamic-data.md §9; specs/05-calls-api.md §3.7 `webhook`; specs/02-repo-layout-and-conventions.md §6 `WEBHOOK_ALLOW_PRIVATE`; specs/31-error-catalog.md `WEBHOOK_URL_REJECTED`
  - Depends on: P05-T03c
  - Deliverables: `va_common.net` (`blocked_address`, `check_destination_url` with injectable resolver, `UnsafeDestinationError.reason`), shared `WebhookSafetySettings` (dispatcher + api; forced false in staging/production), `va_api.services.webhook_policy.check_call_webhook` raising `WEBHOOK_URL_REJECTED` field `webhook`; OIDC discovery reuses the address rule
  - Acceptance: every blocked range parametrized (v4, v6, mapped v6); scheme/env/org-setting matrix; DNS failure and mixed public/private answers rejected; settings forcing test.

- [x] P05-T05b Send Call dispatch service, `POST /v1/calls`, CallWorkflow start — va_api.services.call_dispatch + call_starter, routers/calls, EventPublisher.sequence/broadcast + insert_rows, CallWorkflowInput; start-failure compensation (moved-on rows left to owner), guarded counter release; make test 8699 passed / 7 skipped, 99.10 %; lint/typecheck/placeholders 0; openapi + schema.d.ts regenerated
  - Spec refs: specs/05-calls-api.md §1, §2, §4.1 (steps 1–12), §5; specs/01-architecture.md §2.1 step 1; specs/19-events-and-webhooks.md §1, §2 (`call.queued`, `call.scheduled`, `call.error`); specs/24-billing-and-limits.md §5, §7 (`calls.config.pricing`)
  - Depends on: P05-T04, P05-T05a, P05-T02
  - Deliverables: `va_api.services.call_dispatch` (steps 4–12 in one tenant transaction: inputs, resolution, webhook check, admit_call, `calls` row with pricing snapshot, `call.queued`/`call.scheduled` event + `call_events` row in the same transaction; after commit Temporal start `CallWorkflow` id `call-{id}` on the main queue; on start failure row → `failed`, hold released, counters rolled back, `503`), `va_domain.call_workflow` start contract, `routers/calls.py` legacy router with `calls:write` + `calls_send` rate limit + idempotency, container `call_starter`
  - Acceptance: API tests for success (queued/scheduled bodies), idempotency replay/mismatch, every 4xx path through the endpoint, Temporal failure path, event + row atomicity; cross-tenant/audit/openapi gates green.

- [x] P05-T06 CallWorkflow and activities (split into P05-T06a–d below; tick when all four are ticked) — P05-T06a/b/c/d all ticked (05574f9)
  - Spec refs: specs/01-architecture.md §2, §3; specs/06-telephony-telnyx.md (dial parameters); specs/13-tools-and-dynamic-data.md (pre-call dynamic data, cache flag)
  - Depends on: P05-T05
  - Deliverables: `CallWorkflow` (scheduled wait, `prefetch_call_context`, `dial`, carrier/pipeline signals, backstop timer, heartbeat watch, hangup on failure, child `PostCallWorkflow` start), activities with retry policies, `CallContext` model stored at `call:{id}:ctx`; Telnyx hangups decided from carrier signals (`fax_detected` premium AMD; `streaming.failed` while in progress with no owner after 5 s), per specs/06 §5 Decision P05-T02 item 4
  - Acceptance: Temporal time-skipping tests for scheduled start, no-answer, worker crash backstop, normal completion.

- [x] P05-T06a CallContext, media stream tokens, `va_workflows` package, `prefetch_call_context` activity — va_common.stream_tokens + StreamTokenSettings, va_domain/va_db call_context, va_workflows prefetch activity; make test 8811 passed, 7 skipped, 99.11 % (new modules 100 %), lint/typecheck/placeholders/spec refs pass
  - Spec refs: specs/01-architecture.md §2.1 step 2; specs/03-data-model.md §11 (`call:{id}:ctx`); specs/06-telephony-telnyx.md §4.2 (stream token); specs/28-security.md §6.2; specs/07-media-pipeline.md §3 step 3 (rebuild from `calls.config`); specs/20-memory-personas-agents.md §2.3 (memory slot); specs/13-tools-and-dynamic-data.md §7 (pre-call variables slot); specs/27-observability.md §2 (`trace_context`); specs/02-repo-layout-and-conventions.md §6 (`STREAM_TOKEN_SECRET[_PREVIOUS]`)
  - Depends on: P05-T05
  - Deliverables: `va_common.stream_tokens` (mint/verify with rotation, constant time) + `StreamTokenSettings`; `va_domain.call_context` (`CallContext`, `CallMemory`, key/TTL builders); `va_db.call_context` (`build_call_context` from a `calls` row, `CallContextStore` save/load); new package `packages/va_workflows` with `CallActivityInput` and `CallContextActivities.prefetch_call_context`; dynamic data (phase 08), memory (P13-T01), and first-sentence pre-render (P05-T10) fill their slots later
  - Acceptance: token round trip, tamper/expiry/wrong-call/previous-secret tests; context built from every resolved Bland snapshot config; prefetch activity tests (ActivityEnvironment) for stored context + TTL, terminal call not stored, unknown call non-retryable.

- [x] P05-T06b Telephony call activities: dial, hangup, end-state recording — TelephonyActivities (dial, hangup_call) + CallEndActivities (end_call, slot freed whenever terminal), call_durations shared with edge; make test 8914 passed, coverage 99.12%, lint/typecheck/placeholders 0
  - Spec refs: specs/06-telephony-telnyx.md §3, §5 Decision P05-T02 item 4, §5.1, §6; specs/05-calls-api.md §5 Decision P05-T05b items 5-6, §7.2, §8, §9; specs/24-billing-and-limits.md §6 Decision P05-T04b; specs/19-events-and-webhooks.md §2 (`call.dialing`, `call.dispatch_blocked`, `call.error`, `call.ended`)
  - Depends on: P05-T06a
  - Deliverables: `va_workflows.activities.telephony` — `dial` (status guard, scheduled-call dispatch admission with limits + hold, concurrency requeue signal, `DialRequest` per §6 step 5, leg insert, `dialing` + `call.dialing`, carrier error path), `hangup_call` (idempotent `command_id`), `fail_call` / `end_call` end-state recording (`fax_detected`, `stream_failed` → `error`, `worker_crash`, `canceled`, `dispatch_blocked`) with hold release, slot release, and events; activity retry policies as constants
  - Acceptance: activity tests against FakeTelnyx (dial body asserted field by field incl. AMD/record/hd_audio/time limit), Postgres for row/leg/hold/event effects, every failure branch.

- [x] P05-T06c `CallWorkflow` orchestration and liveness activities — CallWorkflow (scheduled wait, prefetch, requeue dial, ring/max-duration backstops, heartbeat watch, fax/stream-failure hangups, end-then-hangup, ABANDON post-call child) + watch_call_heartbeat/call_presence; 30 time-skipping workflow tests; make test 8980 passed/7 skipped, cov 99.13%, lint/typecheck/placeholders/spec refs pass, jscpd 0, radon ≤ B
  - Spec refs: specs/01-architecture.md §2.1 steps 2-9, §3; specs/05-calls-api.md §7.2 (`signal_cancel`), §5 Decision P05-T05b item 5; specs/06-telephony-telnyx.md §5 Decision P05-T02 items 3-4, §6 Decision P05-T06b; specs/07-media-pipeline.md §18 step 5, §19; specs/18-post-call-analysis.md §1 (child start contract)
  - Depends on: P05-T06b
  - Deliverables: `va_workflows.calls.CallWorkflow` (scheduled durable wait + `signal_cancel`, prefetch, dial with concurrency requeue, `signal_carrier_event` handling incl. fax and `streaming.failed` hangups, `signal_pipeline_finished`, heartbeat watch → `worker_crash`, `max_duration + 120 s` backstop, ring backstop, end-then-hangup on failure, `end_call` cleanup, child `PostCallWorkflow` start with `ABANDON`); liveness activities (`watch_call_heartbeat`, `call_presence`); shared contracts (`va_domain.call_workflow` signals + `PipelineFinished`, `va_domain.post_call`, `heartbeat_key`); import-free activity input modules usable from the workflow sandbox
  - Acceptance: Temporal time-skipping tests (fake activities) for scheduled start, cancel while scheduled, cancel while ringing, requeue then dial / requeue budget spent, no-answer, worker crash backstop, fax hangup, stream failure hangup (owner absent / present), max-duration backstop, ring backstop, normal completion (child started with id `postcall-{id}` on the post-call queue); liveness activity tests (ActivityEnvironment, fakeredis, ManualClock).

- [x] P05-T06d Worker activity wiring and main-queue registration — build_container composes pydantic-converter Temporal client, TelnyxKeyResolver (active BYO keys, AAD-bound), WorkerActivities + main_registrations on TEMPORAL_TASK_QUEUE; deployed-env settings guards; real-Worker call flow test (JSON start, dial, cancel, hangup, slot released); make test 9012 passed / 7 skipped, cov 99.13%, lint/typecheck/placeholders/spec refs 0, jscpd 0 clones
  - Spec refs: specs/02-repo-layout-and-conventions.md §6 (worker-scoped variables); specs/01-architecture.md §1; specs/06-telephony-telnyx.md §2 (client), §6 Decision P05-T06b (`DialSettings`); specs/28-security.md (BYO key decryption)
  - Depends on: P05-T06c
  - Deliverables: `WorkerSettings` gains Telnyx/media/stream-token/crypto/international settings; worker container builds `Database` (configured), Redis, `TelnyxRateLimiter` + `TelnyxClientFactory` (BYO key resolver decrypting `telnyx_accounts.api_key_enc` via `Envelope`), `CallLimiter`, `EventPublisher`, `CallContextStore`, `DialSettings`, clock; Temporal client with `pydantic_data_converter`; registrations built from the container (main queue: `CallWorkflow` + prefetch/dial/hangup/end_call/liveness activities)
  - Acceptance: settings tests (missing secrets refuse to start outside local/test), container tests, a worker test running the real `Worker` on a time-skipping environment that drives `CallWorkflow` through FakeTelnyx dial to `dialing` and through cancel to `canceled`.

- [x] P05-T07 Media worker server, ownership, control channel, drain (split into P05-T07a–b below; tick when both are ticked) — P05-T07a/b both ticked (61720f4)
  - Spec refs: specs/07-media-pipeline.md (worker WebSocket endpoints and auth, ownership lock, control channel schemas, graceful drain, admission control)
  - Depends on: P03-T09, P04-T12
  - Deliverables: `/ws/telnyx` endpoint (signature + stream token verification), ownership lock with renewal, heartbeat key, subscriptions to control/carrier channels, admission control (`MEDIA_MAX_CALLS_PER_POD`), draining state, `media:pods` registry
  - Acceptance: tests for duplicate stream attach (second rejected), lock loss abort, drain refusing new calls while finishing existing.

- [x] P05-T07a Media WS attach: auth, admission, ownership lock, heartbeat, pod registry — va_media server/auth/ownership/session/pods/socket/stream; 9074 passed / 7 skipped, cov 99.14% (va_media 100%), lint/typecheck/placeholders/spec refs clean, jscpd 0
  - Spec refs: specs/07-media-pipeline.md §1 (admission), §2, §3 steps 1-4, §19 (renewal, heartbeat); specs/06-telephony-telnyx.md §4.2; specs/28-security.md §6.2; specs/03-data-model.md §11 (`media:pods`)
  - Depends on: P03-T09, P04-T12, P05-T06a
  - Deliverables: `va_media.server` `/ws/telnyx` (Ed25519 + stream token + `stream_auth_token`, close 4401/4409/4421/4503), `va_media.ownership` (Lua acquire/renew/release), `va_media.session.CallSession` (renewer, heartbeat, body seam, `SessionEnd`), `va_media.pods.PodRegistry` (`media:pods` every 2 s), readiness at 90 % capacity, drain refuses new streams
  - Acceptance: tests for bad signature/token (4401, nothing read), duplicate stream attach (second rejected 4409), admission at capacity and while draining (4503), lock loss aborts with `worker_lost`, heartbeat written every 3 s and deleted at end, `media:pods` entry published and removed.

- [x] P05-T07b Media session control/carrier channels and drain notice/timeout — DbCallContextLoader (ctx or primary-leg org, status check 4401, rebuild), pod-wide ChannelHub (confirmed subscribe, carrier dedupe/overflow), va_domain.control models + acks EX 30, DefaultControlHandler, local drain_notice + worker_lost at timeout; 9142 passed, 99.14% cov, lint/typecheck/placeholders/spec refs pass, jscpd 0, radon no C+
  - Spec refs: specs/07-media-pipeline.md §3 steps 3 and 6, §17, §19 (drain); specs/01-architecture.md §3
  - Depends on: P05-T07a
  - Deliverables: CallContext load on attach (Redis, else rebuilt from `calls.config`), control channel subscription with typed messages (`va_domain.control`), `control_ack` writes, carrier channel subscription + `carrier_state` snapshot read with dedupe, `drain_notice` to sessions, drain timeout ends remaining sessions with `worker_lost`
  - Acceptance: tests for control dispatch + ack, carrier events before/after subscription, drain refusing new calls while finishing existing, drain timeout ending sessions.

- [x] P05-T08 Telnyx transport adapter — va_media.transport.TelnyxMediaTransport (inbound media/dtmf/mark/stop, track/empty/odd-L16 drops, error 100005 rate-limit hook, serialized sends dropped after stream end, leg_format), MediaSocket.send_text, MediaFrame.track; unit + CallPipeline + FakeTelnyx PCMU/L16 contract tests; make test 9168 passed / 7 skipped (99.14 %), lint/typecheck/placeholders/spec refs 0, jscpd 0, radon ≤ B
  - Spec refs: specs/07-media-pipeline.md (Transport protocol); specs/06-telephony-telnyx.md (media WS protocol)
  - Depends on: P05-T07
  - Deliverables: `TelnyxMediaTransport` implementing `Transport` (inbound frames via reorder buffer, outbound media/mark/clear, DTMF events, stop handling, hd_audio L16 path)
  - Acceptance: tests against FakeTelnyx media client.

- [x] P05-T09 Task-mode Brain and system tools (split into P05-T09a–c below; tick when all three are ticked) — P05-T09a, P05-T09b, P05-T09c all ticked
  - Spec refs: specs/11-pathways-engine.md (Task-mode single prompt runner); specs/10-llm-gateway.md (dialogue system prompt, tool calling); specs/13-tools-and-dynamic-data.md (built-in system tools end_call, transfer_call, press_button)
  - Depends on: P03-T13, P04-T12
  - Deliverables: `va_engine.task_runner.TaskBrain` (prompt assembly, streaming reply, system tools, temperature, request_data templating, first_sentence handling)
  - Acceptance: harness tests with FakeLLM rules including end_call and transfer_call tool invocations.

- [x] P05-T09a `va_engine` package: templating engine, EngineState, built-in variables — va_engine templating/builtins/state, 99 tests (100 % package coverage); make test 9267 passed / 7 skipped (99.16 %), lint/typecheck/placeholders/spec refs 0, jscpd 0 clones, radon no C+
  - Spec refs: specs/11-pathways-engine.md §1 (layout), §8 (templating), §9 (EngineState)
  - Depends on: P05-T03a
  - Deliverables: new workspace package `packages/va_engine` (dist `va-engine`); `va_engine.templating` (tokenizer, resolver, filters, type-preserving JSON rendering, percent-encoded URL rendering, missing-variable reporting, limits); `va_engine.models.state` (`NodeFrame`, `TurnRecord`, `EngineState` with 200-turn cap and schema check); `va_engine.builtins.call_builtins` (§8 built-in variables from a `CallContext`)
  - Acceptance: unit + hypothesis tests for every grammar rule, filter, escape, limit, resolution order, missing-var reporting once per variable; EngineState round-trip and unknown schema_version rejection; built-ins for outbound and inbound contexts.

- [x] P05-T09b Voice system prompt, conversation window, built-in tool definitions — va_llm.prompts (voice_system.txt drift-checked vs specs/10 §5), va_llm.tokens (vendored sha256-pinned cl100k), va_llm.window, va_engine.system_tools; make test 9366 passed / 7 skipped (99.16 %), lint/typecheck/placeholders/spec refs 0, jscpd 0, no C+ complexity
  - Spec refs: specs/10-llm-gateway.md §4, §5, §6, §7; specs/13-tools-and-dynamic-data.md §4, §6
  - Depends on: P05-T09a, P03-T13b
  - Deliverables: `va_llm.prompts` (`prompts/voice_system.txt` verbatim + `render_voice_system`, tool usage note); `va_llm.window` (`ConversationWindow` budget trimming); `va_engine.system_tools` (built-in tool definitions, availability rules, argument validation, reserved names)
  - Acceptance: drift test of the prompt file against the specs/10 §5 block; placeholder rendering for every rule; window trimming keeps opener, drops complete turns with their tool messages, inserts the omission note; tool schemas and availability per call config.

- [x] P05-T09c `TaskBrain`: streaming replies, tool loop, system tool actions — va_engine.task_runner.TaskBrain + task_tools.plan_round, TransferRequest/PressButton, ToolExecutor, voice_inputs, language_name; 43 new tests (brain + pipeline); make test 9513 passed, coverage 99.15 %, lint/typecheck/placeholders/jscpd/radon/spec refs pass
  - Spec refs: specs/11-pathways-engine.md §6, §5.1, §7; specs/10-llm-gateway.md §2, §4, §7 (tool loop); specs/13-tools-and-dynamic-data.md §6 (final-line semantics)
  - Depends on: P05-T09b, P04-T12
  - Deliverables: `va_engine.task_runner.TaskBrain` implementing `va_voice.pipeline.Brain` (`va_engine.models.results` BrainAction subclasses, templated `first_sentence` opener, streamed replies through `LlmGateway.stream_dialogue` with call temperature/max_tokens/model, up to 3 tool rounds, `end_call` → `EndCall`, `transfer_call` → `TransferRequest` BrainAction, `press_button` → `PressButton` BrainAction, invalid-argument round, barge-in truncation of history, `call.turn`-ready `EngineState`)
  - Acceptance: va_voice pipeline harness tests with a FakeLLM-backed gateway and rules covering a plain reply, end_call with and without final_message, transfer_call by label and default, press_button, invalid arguments, interruption truncation.

- [x] P05-T10 Greeting, AMD, voicemail actions, IVR mode (split into P05-T10a–f below; tick when all six are ticked) — P05-T10a–f all ticked (4a60f42)
  - Spec refs: specs/05-calls-api.md (wait_for_greeting, voicemail object, ivr_mode/amd, retry); specs/07-media-pipeline.md (wait_for_greeting, first_sentence pre-render); specs/06-telephony-telnyx.md (AMD config, premium results); specs/10-llm-gateway.md (voicemail/IVR classifier prompt)
  - Depends on: P05-T08, P05-T09
  - Deliverables: greeting state machine, AMD result handling, `voicemail.action` hangup/leave_message/leave_message_and_sms/ignore (beep wait via `call.machine.premium.greeting.ended`), `sensitive` LLM detection, IVR mode overrides, voicemail SMS send via va_telnyx messaging
  - Acceptance: e2e flows voicemail_leave_message, voicemail_hangup, amd_ivr_mode pass (written with the e2e harness in P05-T17; the sub-tasks prove the behaviour with in-process simulator tests).

- [x] P05-T10a Media call runtime: the phase-04 pipeline runs every attached call — va_media.runtime CallRuntime/open_runtime + pure runtime_config; 6 FakeTelnyx+FakeElevenLabs+FakeLLM flows; make test 9556 passed/7 skipped (99 %), lint/typecheck/placeholders/spec refs/jscpd 0 clones pass
  - Spec refs: specs/07-media-pipeline.md §1, §16, §18 step 3; specs/08 §1 (STT session options); specs/09 §1–§4 (TTS socket, voice); specs/10 §1 (gateway, platform + org providers); specs/11 §6 (task runner)
  - Depends on: P05-T08, P05-T09
  - Deliverables: `va_elevenlabs.settings.ElevenLabsSettings` and `LlmSettings`/`CryptoSettings` mixed into `MediaSettings`; per-process collaborators built in the media lifespan (`LlmGateway` with platform + `OrgProviderStore`, `OrgSettingsReader`, Silero model, token counter); `va_media.runtime.CallRuntime` session body (TelnyxMediaTransport + ElevenLabs realtime STT + TTS socket + SileroVad + TaskBrain + `PipelineConfig` from the `CallContext`), `PipelineResult` → `SessionEnd` mapping, vendor sockets closed on every path; `create_app` builds it unless a test `body` is injected
  - Acceptance: FakeTelnyx call streamed to a served media worker with FakeElevenLabs and FakeLLM on the same simulator state: the caller's utterance gets an LLM reply the simulator hears, `end_call` ends the session with `agent_end_call`/`agent`, a hang-up ends with `stream_closed`, an STT/TTS open failure and a non-task runner end with `error`; unit tests for config mapping and end mapping.

- [x] P05-T10b Greeting gate, outbound DTMF, IVR mode — pipeline before_opener/wait_for_greeting/IVR dead end, media GreetingGate/CarrierWatch/MediaActions/dtmf_out, shared va_telnyx.settings; make test 9616 passed (99.16 % cov, new modules 100 %), lint/typecheck/placeholders/spec refs clean, jscpd 0 clones; make e2e harness lands in P05-T17
  - Spec refs: specs/07-media-pipeline.md §11 (steps 1, 3, 4, 6), §6 (ivr floor); specs/06-telephony-telnyx.md §9 (send_dtmf, precall sequence); specs/05-calls-api.md (wait_for_greeting, greeting_timeout_ms, ivr_mode, precall_dtmf_sequence); specs/05 §9 (`ivr_dead_end`)
  - Depends on: P05-T10a
  - Deliverables: pipeline greeting gate (`before_opener` hook, `wait_for_greeting` + `greeting_timeout_ms`, `Brain.start(greeting)`), `ivr_mode` dead end (no progress for 120 s → `ivr_dead_end`); media `CarrierWatch` (answered from carrier state/channel), `GreetingGate` (outbound answered wait, `precall_dtmf_sequence` groups split on `w` with 500 ms pauses), `MediaActions` executing `PressButton` through a media-side `TelnyxClientFactory` (`command_id`); Telnyx API settings/factory helpers shared by worker and media
  - Acceptance: pipeline harness tests for the gate order, wait_for_greeting with speech / DTMF / timeout, IVR dead end and progress resets; ManualClock tests for the answered wait (snapshot, channel event, timeout) and precall groups; FakeTelnyx+FakeElevenLabs+FakeLLM runtime tests where the precall sequence and a `press_button` tool call reach FakeTelnyx `send_dtmf` and wait_for_greeting holds the opener until the callee speaks.

- [x] P05-T10d Opener pre-render and cached playback — CallContext.opener_text + va_engine.opener, OpenerPrerender (8 s budget, skips zero-retention/HIPAA) in prefetch, Speaker plays cached SpeakStatic via PhraseSource/va_media.phrases.CallPhrases; make test 9675 passed/7 skipped (99.15 %, new modules 100 %), lint/typecheck/placeholders/spec refs clean, jscpd 0 clones, radon no C+; make e2e harness lands in P05-T17
  - Spec refs: specs/07-media-pipeline.md §11 step 5; specs/09 §9 (phrase cache, prefetch); specs/01 §2.1 step 2
  - Depends on: P05-T10b
  - Deliverables: `prefetch_call_context` renders the templated `first_sentence` into the phrase cache (per-call TTL) and the standard fillers; the media `Speaker` plays a `SpeakStatic` from the phrase cache when an entry exists (no TTS context, same outbound clock and alignment timeline), else through the live socket
  - Acceptance: activity test against FakeElevenLabs stores the entry; runtime test where the opener plays with no TTS socket request for it; cache miss/Redis failure falls back to the live socket.

- [x] P05-T10c AMD results and voicemail actions — pipeline takeover seam, CarrierWatch AMD/beep, GreetingGate 3 s AMD wait, VoicemailMonitor (AMD + classify_answer), call.voicemail, answered_by, VOICEMAIL disposition; make test 9912 passed / 7 skipped, coverage 99.13 %, lint/typecheck/placeholders/spec refs pass
  - Spec refs: specs/07-media-pipeline.md §11 step 2, §14 steps 1-4; specs/06-telephony-telnyx.md §5 (AMD rows, Decision P05-T02 item 9), §6 step 5 (AMD mode); specs/10-llm-gateway.md §9.4 (voicemail classifier); specs/05-calls-api.md (voicemail object, `voicemail_detect_timeout_ms`, §9 end reasons); specs/19 §2 (`call.voicemail`)
  - Depends on: P05-T10b, P05-T11
  - Deliverables: pipeline takeover seam (`CallPipeline.take_over(Takeover)`: cut the reply, go silent, optional beep/silence wait, non-interruptible message, end with the reason); `CarrierWatch` AMD facts (premium/standard detection result, beep); `GreetingGate` 3 s AMD wait after the answer; `va_media.voicemail.VoicemailMonitor` (AMD `machine` and `voicemail.sensitive` classifier within `voicemail_detect_timeout_ms`, fax → silent end, actions `hangup`/`leave_message`/`ignore`, `leave_message_and_sms` speaks the message only until P05-T10e); `call.voicemail` events; `answered_by='voicemail'` on classifier detection; `disposition_tag='VOICEMAIL'` for voicemail ends; `amd_mode` shared from va_domain; `prompts/answer_classifier.txt` drift-checked
  - Acceptance: pipeline harness tests for the takeover (beep, silence, 20 s cap, before the opener, after a reply started); ManualClock unit tests for the monitor (premium machine before/after the 3 s wait, late result beyond the window ignored, human/silence/unknown, fax, classifier voicemail/ivr/low confidence/LLM failure, every action); FakeTelnyx+FakeElevenLabs+FakeLLM runtime tests: machine + `leave_message` with `beep_at_ms` (agent audio only after the beep, TTS text = the message, `voicemail_left`/agent, `answered_by=voicemail`, one `call.voicemail`), machine + `hangup` (no TTS dialogue, `voicemail_hangup`/system, disposition `VOICEMAIL`), `ivr_mode` with machine AMD → conversation goes on, `sensitive` classifier on a human-AMD call whose callee says a mailbox greeting.

- [x] P05-T10e Voicemail SMS — Takeover.after_message hook, va_media.voicemail_sms + va_db.voicemail_sms (uuid5 duplicate guard, send-only conversation, call.sms_sent), Send Call SMS_NOT_ENABLED; make test 9948 passed / 7 skipped, 99.12 %
  - Spec refs: specs/07-media-pipeline.md §14 step 4 (`leave_message_and_sms`); specs/05-calls-api.md (`voicemail.sms`: `from` must have SMS capability, defaults to the call `from`); specs/21-messaging-sms.md §1 (active conversation lookup), §2 steps 1/5/6 (number checks, `messages` row, Telnyx `POST /v2/messages`, `telnyx_message_id` recorded at once); specs/19 §2 (`call.sms_sent`); specs/31 (`SMS_NOT_ENABLED`)
  - Depends on: P05-T10c
  - Deliverables: pipeline `Takeover.after_message` hook (awaited, bounded, after the message's mark echo and before the end); `va_media.voicemail_sms.VoicemailSms` (templated `voicemail.sms.message` from `voicemail.sms.from` or the call `from`, sender = active org number with `sms` capability, messaging profile = platform `TELNYX_MESSAGING_PROFILE_ID` or the BYO account's, deterministic message id so a restarted media worker never sends twice, conversation found or created send-only, `messages` row queued → `telnyx_message_id`/status/parts or `sending_failed`, `call.sms_sent`; every failure logged and never blocks the hangup); Send Call refuses a `leave_message_and_sms` sender without SMS capability (400 `SMS_NOT_ENABLED`)
  - Acceptance: pipeline harness tests for the hook order (after the message, before the end; timeout; not run without a message); unit tests for the sender (every failure path); Postgres tests for the rows and the duplicate guard; runtime sim test where FakeTelnyx receives the SMS after the message and before the hangup, and a failing send still ends `voicemail_left`; api test for `SMS_NOT_ENABLED`.

- [x] P05-T10f Voicemail retry child call — va_domain.call_retry + schedule_retry_call activity (scheduled child row, call.scheduled, idempotent) + CallWorkflow abandoned child; make test 9996 passed/7 skipped 99.13%, lint/typecheck/placeholders/spec refs pass, retry workflow module 5× stable
  - Spec refs: specs/07-media-pipeline.md §14 step 5; specs/05-calls-api.md (`retry`, `retry.on_no_answer`, `parent_call_id`); specs/01 §2.1; specs/30 (`retry_on_voicemail`)
  - Depends on: P05-T10e
  - Deliverables: `CallWorkflow` schedules the retry child call after a voicemail end (`voicemail_hangup`/`voicemail_left`), or after `no_answer`/`busy` when `retry.on_no_answer`: a new `calls` row with `parent_call_id`, the parent's config with `retry.voicemail_action`/`voicemail_message` as its voicemail settings and the attempt counted against `max_attempts`, started `retry.wait` seconds later through the normal dispatch checks (limits, compliance, credit hold)
  - Acceptance: Temporal time-skipping tests for the retry child (voicemail, no-answer with and without `on_no_answer`, attempts exhausted, dispatch refusal recorded on the child).

- [x] P05-T11 Call ending, persistence, max duration, stop API (split into P05-T11a–c below; tick when all three are ticked) — P05-T11a/b/c all ticked (bb8a424)
  - Spec refs: specs/05-calls-api.md (POST /v1/calls/{id}/stop, stop all, §9 end reasons); specs/07-media-pipeline.md (max_duration timer, error phrase); specs/03-data-model.md (call_turns, call_events)
  - Depends on: P05-T08
  - Deliverables: end paths (agent, user hangup, timeout, API, error), Telnyx hangup, turn write-behind flush, final call row update, lock release, workflow signal, stop endpoints via control channel
  - Acceptance: e2e max_duration and stop_via_api pass; end_reason/ended_by correct for each path.

- [x] P05-T11a In-call persistence: turn write-behind and live call events from the media worker — CallTurnRepo upsert/merge/delete, TurnWriter write-behind, TurnRecorder + call.turn/interrupt/latency events, open_persistence in open_runtime; make test 9712 passed / 7 skipped (99.16 %), new modules 100 %, lint/typecheck/placeholders/spec refs pass, jscpd 0, radon no C+
  - Spec refs: specs/07-media-pipeline.md §8 steps 2, 6, 7, §16 Decision P04-T12a, §20 Decision; specs/03-data-model.md (call_turns); specs/19-events-and-webhooks.md §1 step 4, §2 (`call.turn`, `call.interrupt`, `call.latency`); specs/01 §2.1 step 6
  - Depends on: P05-T10a
  - Deliverables: `va_db.repositories.CallTurnRepo` (idempotent batch upsert on `(call_id, turn_index, role)`, delete of a superseded user turn); `va_media.turns.TurnRecorder` (`PipelineObserver`: user/agent turn rows relative to the answer time, latency onto the turn row, `call.turn`/`call.interrupt`/`call.latency` events through a per-process `EventPublisher` + `CallEventBuffer`), write-behind flush every 2 s and on call end; per-process event buffer and turn writer supervised by the media lifespan
  - Acceptance: unit tests for row/event mapping (supersede, interrupted agent turn, latency without e2e); Postgres tests for the upsert/flush/retry; FakeTelnyx+FakeElevenLabs+FakeLLM runtime test where a conversation leaves ordered `call_turns` rows and `call.turn`/`call.latency` `call_events` rows; `uvx radon cc -s -n C` on every touched src file reports nothing (the pre-existing `va_voice.latency.TurnTracer.emit` C (11) is refactored to ≤ B, behaviour and spans unchanged).

- [x] P05-T11b Media end-of-call routine: hangup, final call row, slot release, workflow signal — va_media.call_end.CallEnd runs before lock release (resolve/record/hangup/call.ended/flush/slot/signal), shared va_db.call_end.record_answered_end, CallWorkflow worker_lost hangup; make test 9755 passed/7 skipped, 99.17 %, lint/typecheck/placeholders/spec refs pass, jscpd 0, radon max B
  - Spec refs: specs/07-media-pipeline.md §18, §19 (worker_lost, Telnyx WS closed); specs/05 §8, §9; specs/06 §5 Decision P05-T02 item 7; specs/01 §2.1 steps 7-8, §3
  - Depends on: P05-T11a
  - Deliverables: `va_media.call_end` end routine run inside the session before the lock is released (end reason resolution incl. `stream_closed` → `user_hangup`/`max_duration` from the carrier hangup, 3 s wait, else hangup + `error`; Telnyx `hangup` of the primary leg with `command_id`; turn/event flush; `completed` with `end_reason`/`ended_by`/`ended_at`/connected + transfer seconds through logic shared with the `end_call` activity; `call.ended`; slot release; `signal_pipeline_finished` over a media Temporal client); `CallWorkflow` hangs up after a `worker_lost` pipeline end; `max_duration`/`no_input_timeout`/`agent_end_call`/`error`/`api_stop`/`insufficient_credits`/`ivr_dead_end` paths verified end to end
  - Acceptance: runtime sim tests per end path assert `calls.status/end_reason/ended_by/connected_seconds`, one FakeTelnyx hangup (none after a callee hangup or ownership loss), the slot released, the signal sent; workflow test for the `worker_lost` hangup.

- [x] P05-T11c Stop endpoints: `POST /v1/calls/{call_id}/stop` and `POST /v1/calls/active/stop` — va_api.services.call_stop (signal_cancel pre-answer with no-workflow fallback cancel, stop control in progress, 409 otherwise, stop-all with call_ids/role check), cross-tenant + OpenAPI; make test 9801 passed/7 skipped, 99.16 %, lint/typecheck/placeholders/spec refs/test-ts pass, jscpd 0, radon max B
  - Spec refs: specs/05-calls-api.md §7.2, §7.3; specs/07 §17; specs/04 §3.2 (calls.operate); specs/31 (`CALL_NOT_ACTIVE`)
  - Depends on: P05-T11b
  - Deliverables: stop service (pre-answer → `signal_cancel`; in progress → `stop` control message; terminal/transferred → 409), stop-all with optional `call_ids`, `calls.operate` permission, OpenAPI + cross-tenant registrations
  - Acceptance: API tests per status, stop-all counts and filter, 404 for another org's call, control message published; media runtime test where a published stop ends the call with `api_stop`/`api` and no goodbye line.

- [ ] P05-T12 Inbound call routing
  - Spec refs: specs/01-architecture.md §2 (inbound); specs/06-telephony-telnyx.md (inbound routing algorithm); specs/16-phone-numbers.md §4 (InboundConfig model)
  - Depends on: P05-T02, P05-T06
  - Deliverables: number lookup (system repo), reject/transfer/maintenance logic, call row + context creation with 1.5 s inline budget, `answer` with stream params, `InboundConfig` model (full API arrives in phase 10)
  - Split (2026-11-02, Planner): P05-T12a–d below; tick when all four are ticked.
  - Acceptance: e2e inbound_basic passes; unknown number rejected; suspended org with fallback transfers.

- [x] P05-T12a InboundConfig model and inbound call resolution through the shared Send Call loader — va_domain.numbers.InboundConfig + call_resolution.inbound_request, loaders moved to va_db.call_inputs (load_inbound_inputs), OrgSettings.default_inbound_prompt; make test 10162 passed 7 skipped 99.13 %, lint/typecheck/placeholders/spec refs pass, jscpd 0, radon -n C empty
  - Spec refs: specs/16-phone-numbers.md §4; specs/06-telephony-telnyx.md §7 steps 6, 8; specs/05-calls-api.md §4.1 steps 5-8, §4.3; specs/04 §7 (OrgSettings)
  - Depends on: P05-T03c
  - Deliverables: `va_domain.numbers.InboundConfig` (every §4 field, `extra="forbid"`, runner rule) + `Maintenance`/`maintenance_active`; `OrgSettings.default_inbound_prompt`; `va_domain.call_resolution.inbound.inbound_request(config, caller=, called=, org_name=)` → `SendCallRequest`; the runner/ref/caller-ID loaders moved from `va_api.services` into `va_db.call_inputs` (api behaviour unchanged) plus `load_inbound_inputs` (caller ID fixed to the called number, BYO account from the number)
  - Acceptance: model tests (valid + invalid per field, runner conflict, maintenance windows); mapping tests (prompt → task, default prompt, routing-only fields dropped, persona+pathway); Postgres tests resolving an inbound config with task/pathway/agent runners and a BYO number into a `SendCallConfig`; existing api Send Call tests unchanged and green.

- [x] P05-T12b Edge inbound routing decisions: reject and fallback transfer — va_edge.routing decide + InboundRouter (reject/fallback/accept seam, EventSettled, acquire_inbound slot), FakeTelnyx ringing transfer; make test 10216 passed, 7 skipped, 99.13 %, routing.py 100 %; lint/typecheck/placeholders/spec refs pass; jscpd 0; radon -n C empty
  - Spec refs: specs/06-telephony-telnyx.md §7 steps 1-7, §5 (`call.initiated`); specs/16 §4 (`fallback_number`, `maintenance`, `allowed_callers`, `blocked_callers`); specs/05 §9 (`inbound_rejected`); specs/24 (min attempt, concurrency); specs/23 (DNC)
  - Depends on: P05-T12a
  - Deliverables: `va_edge.routing` `InboundRouter` implementation wired into the default edge handlers: number lookup, allowed/blocked callers, org status, credits, DNC (`block_dnc_callers`), maintenance, concurrency cap; Telnyx `reject` (cause) / `transfer` to `fallback_number` with `command_id`; `telnyx_events.process_error` for each refusal; accepted calls handed to an accept seam (P05-T12c)
  - Acceptance: unit/Postgres tests for every branch; FakeTelnyx `/sim/telnyx/inbound` contract tests for unknown number (rejected), suspended org with and without fallback, maintenance with fallback (transferred), blocked caller.

- [x] P05-T12c Inbound accept at the edge and CallWorkflow inbound mode — va_edge.accept InboundAccept (persist → context → start → answer, 3 s budget, cleanup + accept_failed), CallWorkflow inbound mode, shared call_row/streams/waits/start_workflow; make test 10282 passed, 7 skipped, 99.14 %; lint/typecheck/placeholders/spec refs 0; jscpd 0; e2e harness lands in P05-T17
  - Spec refs: specs/06-telephony-telnyx.md §7 steps 8-11, §4.1 (handler budget); specs/01 §2.2; specs/07 §3 (stream token/URL); specs/19 §2 (`call.ringing`); specs/27 §3.2
  - Depends on: P05-T12b
  - Split (2026-11-02, Planner): the maintenance message-only path and the full edge → media conversation test moved to P05-T12d.
  - Deliverables: `va_edge.accept.InboundAccept` (the `InboundAcceptor`): inbound call row (`direction='inbound'`, `ringing`) + primary leg + contact upsert + `call.ringing` event in one tenant transaction, call context built inline (1.5 s budget, media worker rebuilds on a miss), `CallWorkflow` started in inbound mode, Telnyx `answer` with stream params/recording/`client_state`, `command_id=answer-{call_id}`; failure cleanup (call `failed`, workflow terminated) so the router's fallback/reject applies; `CallWorkflow` inbound mode (no prefetch/dial: answer wait → conversation → end as outbound); ring-to-answer histogram; per-event-type processor budget for `call.initiated`; `CallContext.maintenance_message` carried from the accepted call (spoken in P05-T12d)
  - Acceptance: Postgres + FakeTelnyx tests for the accept path (row/leg/contact/event/context/workflow input/answer body), each failure step (resolution error, workflow start failure, answer failure, budget timeout) refusing through the fallback/reject with the call marked failed, caller gone before answer; Temporal tests for inbound mode (answered → conversation → pipeline finished; caller hangup before answer; answer backstop); FakeTelnyx `/sim/telnyx/inbound` through the edge app shows `answer` with the stream URL and the call answered.

- [ ] P05-T12d Inbound maintenance message and end-to-end inbound conversation
  - Spec refs: specs/16 §4 (`maintenance.message`); specs/06 §7; specs/07 §3, §16; specs/30 (inbound_basic flow)
  - Depends on: P05-T12c
  - Deliverables: maintenance message-only path (answer, the media worker speaks `maintenance.message` through a pipeline takeover — phrase cache when present — and hangs up with `inbound_rejected`); an in-process end-to-end test: FakeTelnyx inbound call → edge app → served media worker with FakeElevenLabs + FakeLLM → conversation → caller hangup → call `completed`
  - Acceptance: media runtime test for the maintenance takeover (message spoken, no LLM request, end `inbound_rejected`); end-to-end inbound conversation test ends `completed` with turns recorded; ring-to-answer observed ≤ 800 ms in the simulator run.

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
  - Deliverables: `tests_e2e/` harness (compose profile e2e, fixtures to start services, scenario helpers) and flows outbound_basic, inbound_basic, barge_in, worker_crash_recovery, plus the e2e flows of tasks finished before the harness existed (voicemail_leave_message, voicemail_hangup, amd_ivr_mode from P05-T10; others named in earlier tasks' acceptance when they also predate it); a 50-concurrent-call simulator test against one media worker process
  - Acceptance: `make e2e` green; concurrency test reports no underruns and p50 platform overhead ≤ 150 ms.

## Phase exit gate
`make check && make e2e` pass with all flows listed in prompts/phase-05.md.
