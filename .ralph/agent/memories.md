# Memories

## Patterns

### mem-1793753822-cf55
> P07-T11b: unit test CRUD/from_call/runs = routers/pathway_tests (UnitTest* schema names: src Test* classes get collected by pytest; test-file helper class named Tests with __init__ errors collection) + services/pathway_tests capture_turn + va_db PathwayTestRepo/PathwayRunRepo; analysts may write tests (no editor guard); session_helpers.error_of returns (status, code, field) not message; cross-tenant factories read context.params[pathway_id]
<!-- tags: api, pathways, testing | created: 2026-11-04 -->

### mem-1793750810-7c1c
> P07-T11a: va_pathways package = shared text runs (TextConversation/TextRecorder/DbPathwayLoader moved from api) + unit_tests.run_unit_test; GradeVerdict uses validation_alias/serialization_alias 'pass' (plain alias makes mypy demand keyword pass); FakeLLM error status 400 makes analysis calls unavailable instantly (5xx retries 2s/8s on gateway clock -> hangs on ManualClock); LlmControl mixin shared by api ChatStack and TextRig to keep jscpd 0
<!-- tags: pathways, testing | created: 2026-11-04 -->

### mem-1793741280-f92b
> P07-T10: pathway test chat = services/pathway_chat (ChatStore Redis pathway_chat:{id}+:lock, one PathwayEngine per request) + pathway_chat_run.ChatRecorder (events sink, HttpPort wrapper, simulated SmsPort); api container llm_gateway + tools_http; SessionContainer.clock is typed FrozenClock so chat tests use chat_helpers.WallClock (FrozenClock subclass over SystemClock) else gateway deadlines fire at once; +1555... numbers fail normalize_e164 (use +1415555xxxx)
<!-- tags: api, pathways, testing | created: 2026-11-03 -->

### mem-1793739301-6682
> P07-T09c: pathway folders/io routers must precede pathways_router in main.ROUTERS (literal /folders,/import vs uuid /{pathway_id}); PathwayFolderRepo.lock_tree locks all folder rows FOR UPDATE then re-reads (READ COMMITTED sees rows committed while waiting); create_checked(action=,audit_extra=) shared by create/import/duplicate; dict(await session.execute()) fails -> build from .all()
<!-- tags: api, pathways, testing | created: 2026-11-03 -->

### mem-1793737035-ef46
> P07-T09b: pathway versions = services/pathway_versions + routers/pathway_versions (side-effect import) + PathwayRepo freeze/fork_draft/set_pin + va_db.pathway_cache; test_audit_unit parses only two-part audit action names (pathway.version_save); cross-tenant factories with non-unique values need register_resource_factory(marker=False)
<!-- tags: api, pathways, testing | created: 2026-11-03 -->

### mem-1793735038-296f
> P07-T09a: pathways API = va_api.routers.pathways (legacy) + services.pathways (check_graph in to_thread, MultiAppError for many errors[] items) + va_db PathwayRepo (method page not list: a method named list breaks mypy list[...] annotations in the class); require_pathway_editor guard because analysts hold pathways:write; api tests can import graph_helpers
<!-- tags: api, pathways, testing | created: 2026-11-03 -->

### mem-1793727197-2b39
> P07-T08b: PathwayEngine (va_engine.engine) async-generator turns over Decider/GatewayDialogue/Navigator; tests KeyedPort answers decision calls per function name (concurrent); mypy narrows dataclass flag after while-not loop even when callee mutates it -> read via method; killing make test leaves a stale exit= line from the wrapper, truncate log AFTER the old wrapper exits
<!-- tags: engine, pathways, testing | created: 2026-11-03 -->

### mem-1793718805-5413
> P07-T05: va_engine.navigation Navigator (apply RouteDecision / finish_node / enter, Transition speaks/over_limit/problem, never raises for broken targets); EngineState v3 NodeFrame.kind required, v2 upgrader derives kind from pathway id; service_helpers.json_log_stream returns a stream (not a context manager), filter log_events(stream) by event
<!-- tags: engine, pathways, testing | created: 2026-11-03 -->

### mem-1793715505-4859
> P07-T04: decision calls va_engine.router/loop_condition/extraction over va_engine.decisions.StructuredPort, dynamic LRU schemas in decision_schemas (never registered), prompts via va_llm.prompts.render_prompt; tests ScriptedPort validates against real schema; jscpd ignore markers need a reason suffix or ruff ERA001 flags them
<!-- tags: engine, pathways, testing | created: 2026-11-03 -->

### mem-1793709538-f7d8
> P07-T01: pathway schemas va_engine.models.nodes/graph (camel JSON via alias_generator, extra=allow, dump exclude_unset = lossless Bland round trip; BeforeValidator normalize_node_raw outside discriminated union for Global/aliases) + va_engine.validation.validate_graph(mode, GraphRefs None=skip, code_checker); test parses specs/11 §11 table; ruff --select RUF100 --fix strips noqa of untouched files (restore with git checkout)
<!-- tags: engine, pathways, testing | created: 2026-11-03 -->

### mem-1793695766-e1de
> P06-T07: producers route webhooks atomically with call_events rows via EventPublisher.route(sequenced, session=) after insert_rows; media routes in DbCallEventWriter(database, router=) flush using va_events.event_of(row); WebhookRouter fakes need route_many; TurnRecorder.started publishes call.started first (category call) so filter turns by event_type; event log items carry type
<!-- tags: events, webhooks, testing | created: 2026-11-03 -->

### mem-1793688514-633a
> P06-T06a: call SSE = va_api.services.call_stream (open_call_stream checks+subscribes, CallStream.frames replay->tail, SeqOrder gap hold) + va_api.sse.EventStreamResponse(frames,on_close) (Starlette never closes abandoned body iterators; Response subclass needs status_code init param or FastAPI openapi crashes UnboundLocalError) + va_events.LiveHub pod-wide pubsub fan-out; SessionContainer hub on SystemClock (FrozenClock times out confirmations); ASGITransport buffers body -> live streams tested via service_helpers.serving; cross_tenant register_request(headers=)
<!-- tags: api, events, testing | created: 2026-11-03 -->

### mem-1793685779-173d
> P06-T05: events sink va_events_sink.sink (StreamSink.cycle/run, run_sink, build_sinks) + pure rows.rows_for_event + DeadLetter(events:dead) + StreamConsumer read_own_pending/dead_letter; billing.usage internal type (INTERNAL_TYPES never routed); fakeredis blocking XREADGROUP returns [] immediately -> read loops must yield; XPENDING on missing group raises NOGROUP; media TurnRecorder holds assistant call.turn to attach data.latency
<!-- tags: events, clickhouse, testing | created: 2026-11-03 -->

### mem-1793682816-3287
> P06-T04b: webhook delivery code shared in va_events.webhook_{signing,schedule,delivery}; endpoint/delivery API in va_api.routers.webhook_endpoints + services.webhook_test_send; OutboxRepo outcomes only on delivering rows (DeliveryCancelledError); helpers named test_* get collected by pytest; api tests creating many resources hit frozen-clock rate limits -> seed rows directly; test_container asserts close order of every httpx client in build_container
<!-- tags: webhooks, api, testing | created: 2026-11-03 -->

### mem-1793673488-d9e9
> P06-T02: webhook outbox va_events.DbWebhookRouter (route/route_many, caller session = outbox) + pure webhook_routing.plan_deliveries + webhook_payloads (live_body/batch_body/envelope_body/strip_phi/iso_ms); WEBHOOK_KEY in catalog; org webhook_format picks per-call format; Call(metadata=...) silently sets declarative MetaData attr -> use metadata_=
<!-- tags: events, webhooks, testing | created: 2026-11-03 -->

### mem-1793669923-12cd
> P05-T17e2: 50-call overhead was mostly a per-call constant from InboundPath origin anchored on first frame processing time during attach backlog; fixed with origin=min(arrival-20k). Also batched VAD (VadExecutor.infer/VadModel.infer_batch), GatedVad window_samples=512, uvloop for media+sim, tune_gc. py-spy without --nonblocking inflates latency ~50-100ms; split sim v2v vs call.latency e2e_ms to locate overhead
<!-- tags: media, performance, e2e | created: 2026-11-03 -->

### mem-1793656519-ccbe
> P05-T17d: e2e barge/interrupt flows read FakeTelnyx timeline clears; voice onset = utterance start_ms + va_sim.audio.MARKER_MS; pipeline played_chars is whole-word text_spoken length (compare via whole_word_prefix, not plain ±10%); Silero onset on synthetic caller voice is bimodal (~0 or ~200 ms late on first syllable); docker kill media -> heartbeat watch ends call worker_crash ~29 s later (owner TTL beats stream_failed grace); make_test.log from an earlier run keeps a stale exit= line, so truncate logs before monitoring
<!-- tags: e2e, testing, sim | created: 2026-11-02 -->

### mem-1793650295-24c7
> P05-T17b: docker e2e harness tests_e2e/ (support/e2e_harness Harness + e2e_assertions.evaluate, unit tests in tests_e2e/unit run by make test); seed POST /internal/test/orgs (va_api.routers.e2e_seed, local/test only); never /sim/reset in e2e; served sim must start from random seed (seed 0 replayed Telnyx event ids after restart -> edge dedup dropped call.initiated); tool-call-only LLM rounds mark llm_first_token at stream end; end_call final line runs on turn timer
<!-- tags: e2e, testing, sim | created: 2026-11-02 -->

### mem-1793643573-18f6
> P05-T18b: PostCallWorkflow (va_workflows.post_call) + PostCallActivities; tests postcall_rig.FakePostCallActivities/run_post_call (budget timer always first TIMER_STARTED); swallowing ActivityError in step wrappers hides budget cancels -> re-raise CancelledError after timeout; RecordingStorage + signed_recording_url in va_common.storage; dialed canceled calls are charged attempt fee
<!-- tags: workflows, temporal, testing | created: 2026-11-02 -->

### mem-1793641286-4864
> P05-T18a: va_billing.metering.finalize_call (org lock -> insert usage ON CONFLICT DO NOTHING RETURNING -> ledger only for new non-zero rows -> recompute price -> capture/release hold); call_rows.seed_call accepts id= ; billing DB tests seed start balance via grant_credits so ledger sums to balance
<!-- tags: billing, testing | created: 2026-11-02 -->

### mem-1793639516-44e5
> P05-T16b: call artifacts service va_api.services.call_artifacts (active/recording/transcript/events) reuses call_read.ReadDeps(+redis)/owned_call; transcript SRT/VTT in va_domain.call_object.transcript_formats with golden files; FastAPI query-model routes need cross_tenant REGISTRY.register_request; route returning RedirectResponse or model: set response_model= explicitly, annotate -> Model | Response
<!-- tags: api, calls, testing | created: 2026-11-02 -->

### mem-1793637094-860e
> P05-T16a: Call Object = va_domain.call_object (CallObject extends CallListItem) + va_db.call_object load_call_objects/load_call_list_items (batched, reuse for call.completed); api call_read service + CallListQuery FastAPI query model (cross-tenant walker needs REGISTRY.register_request for calls_list); SessionContainer.presigner FakePresigner; fake api container has no settings -> use request.app.state.settings; type X = Literal aliases become $defs refs in JSON schema; never parametrize with new_id()
<!-- tags: api, calls, testing | created: 2026-11-02 -->

### mem-1793635337-c714
> P05-T15: GatedVad (va_voice.pipeline) + PipelineConfig.noise_cancellation; CallRuntime(background=BackgroundLibrary) required; runtime sim calls play static bed by default -> FakeTelnyx AGENT_SPEECH_LEVEL raised to 1024 frame RMS (static ~330); office/cafe/restaurant beds read as agent speech; default resolved config waits 6 s for greeting (wait_for_greeting=False for immediate opener); barge-ins observable via json_log_stream pipeline_barge_in / media.transport_closed clears
<!-- tags: media, pipeline, testing | created: 2026-11-02 -->

### mem-1793632603-0d52
> P05-T14: va_common.storage ObjectStore (aioboto3 S3, SSE-KMS only with aws KMS) + store_recording activity on postcall queue (activities-only registration); edge signals postcall-<id> recording_saved; httpx logger default WARNING because its INFO lines leak presigned URLs; MinIO testcontainer helper packages/va_common/tests/minio_container.running_minio (HttpWaitStrategy /minio/health/ready); aioboto3 upload_file raises raw botocore ClientError (no S3UploadFailedError)
<!-- tags: storage, workflows, testing | created: 2026-11-02 -->

### mem-1793631037-dbe7
> P05-T13: cold transfer = va_media.transfer.ColdTransfer via MediaActions(transfer=).bind(on_transferred=take_over(Takeover('transferred')), on_transfer_failed=brain.on_transfer_failed+CallPipeline.say); end routine transferred -> streaming_stop only; CallWorkflow phase transferred waits primary hangup. Temporal time-skipping server wedges (ACTIVITY_UNKNOWN) when the workflow cancels a dispatched watch that is failing/completing: send decisive signals from the dial activity so the watch is never dispatched. call_events rows have no type column (assert category/data). Flow tests: EdgeForwarder.wait_for + edge_app.state.processor.join()
<!-- tags: media, workflows, testing | created: 2026-11-02 -->

### mem-1793624547-e9aa
> P05-T12c: va_edge.accept.InboundAccept (persist->context 1.5s->start->answer within 3s; cleanup failed/error + terminate then router refuses accept_failed); carrier_handlers default acceptor needs STREAM_TOKEN_SECRET, NO_ACCEPTOR seam; EventProcessor budgets per type; shared va_common.waits, va_telnyx.streams, va_common.checks.start_workflow/terminate_workflow, va_db.call_rows.call_row(NewCall), ContactRepo.upsert_phone; tests accept_helpers.accept_rig (GatedClock, answer_failing_factory; AcceptRig.org adds default voice); sim clock dates are outside call_events partitions; processor tests offering 2 events may land on one shard -> run sequentially
<!-- tags: edge, telnyx, testing | created: 2026-11-02 -->

### mem-1793620529-d99b
> P05-T12b: va_edge.routing InboundRouter + pure decide; handlers that settle with a reason raise va_edge.processor.EventSettled (mark_processed settled_attempts=max); edge container needs telnyx factory + limiter + LOCAL_MASTER_KEY; routing_helpers.routing_rig assigns random DB numbers to FakeTelnyx accounts directly (sim inventory is fixed); accelerated sim clock runs bridged calls to time limit while edge awaits Postgres -> assert event history
<!-- tags: edge, telnyx, testing | created: 2026-11-02 -->

### mem-1793618340-c1ba
> P05-T12a: InboundConfig subclasses CallParameters (extra=forbid, OUTBOUND_ONLY_FIELDS rejected) like CallDefaults; Send Call loaders moved to va_db.call_inputs (loader.load_call_inputs ctx-free, load_inbound_inputs); merge_layers never applies org default voice (load_voice does); shared loader row builders packages/va_db/tests/support/call_input_seed.Seed
<!-- tags: calls, domain, testing | created: 2026-11-02 -->

### mem-1793610108-0c52
> P05-T10c: CallPipeline.take_over(Takeover) seam (sync, first end wins, before-run OK -> opener skipped); VoicemailMonitor must bind+start(verdict) synchronously before pipeline.run (a never-suspending before_opener otherwise lets the opener start); default resolved config is premium AMD -> media tests seed answered_by=human or relay FakeTelnyx AMD via sim_media.EdgeRelay; YAML flow scenario say texts with commas need quotes
<!-- tags: media, pipeline, testing | created: 2026-11-02 -->

### mem-1793607833-39d1
> P05-T11c: stop endpoints va_api.services.call_stop (TemporalCallCanceller via signal_workflow(*args), fallback cancel for scheduled/queued without workflow); shared Temporal signal fake packages/va_common/tests/workflow_fakes.py (SignalRecorder, connector_of); log-absence asserts must compare quoted JSON values, bare digits match timestamps
<!-- tags: api, calls, testing | created: 2026-11-02 -->

### mem-1793602869-7053
> P05-T11a: media in-call persistence = va_media.turns TurnRecorder (PipelineObserver, sync turn ops, ordered publish task chain + drain) + CallPersistence + va_media.turn_writer.TurnWriter (write-behind va_db.call_turns ops via CallTurnRepo) + runtime.open_persistence; media session fixture media_db_url in conftest; runtime sim tests on Postgres via rig(persistence=) + Rig.call(seeded=seed_call(...)); mypy flags 'while not self._closed: ... if self._closed' as unreachable - move the check into a helper returning bool
<!-- tags: media, persistence, testing | created: 2026-11-02 -->

### mem-1793593160-73e2
> P05-T10a: va_media.runtime CallRuntime is the media session body (open_runtime entered by create_app only when no body= injected; tests outside media_app must pass body=read_until_stop). Runtime sim tests: one realtime_telnyx() SimState + running_sims(elevenlabs,llm) + streamed_call(state=,loader=FakeContextLoader(**cfg)); assert via state.elevenlabs.tts_requests generated_text (first record empty) and state.llm.requests since sim stream is gone after hangup
<!-- tags: media, pipeline, testing | created: 2026-11-02 -->

### mem-1793591347-ba72
> P05-T09c: TaskBrain = va_engine.task_runner (pure plan_round in task_tools, TransferRequest/PressButton BrainActions, ToolExecutor protocol); internal async generators typed AsyncGenerator + consumed via contextlib.aclosing so closed replies run finally promptly; tests task_helpers.served()/Sim.brain/drain; FakeLLM call_index rules separate tool rounds; harness(engine=brain, clock=RealtimeClock()); harness barge-in at balanced level needs >=900 ms speech (400 ms was flaky)
<!-- tags: engine, pipeline, testing | created: 2026-11-02 -->

### mem-1793588946-2976
> P05-T09b: va_llm.tokens vendors cl100k_base gzip (never tiktoken.get_encoding: network); va_llm.prompts render_voice_system/fit_voice_system; va_llm.window ConversationWindow; va_engine.system_tools availability table + parse_system_call; calendar.day_name is locale-dependent - use fixed English table
<!-- tags: engine, llm, testing | created: 2026-11-02 -->

### mem-1793587466-6df6
> P05-T09a: packages/va_engine exists (templating parse_template/render/render_url/render_json + TemplateScope layers + MissingVariables; builtins.call_builtins; models.state EngineState.add_turn 200 cap). ruff TRY003 flags string literals passed to exceptions even through helper functions returning the exception: use module-level message constants
<!-- tags: engine, templating, testing | created: 2026-11-02 -->

### mem-1793585754-b976
> P05-T08: va_media.transport.TelnyxMediaTransport is the pipeline Transport over the media WS (events() once, serialized sends dropped after stream end, leg_format); media tests: FakeSocket.sent/send_gate + media_msg/dtmf_msg/mark_msg/error_msg; sim_media.streamed_call(models_dir, body, **dial) runs FakeTelnyx (wall clock) streaming to a served media worker; CallPipeline can run in media tests with va_voice pipeline_fakes
<!-- tags: media, websocket, testing | created: 2026-11-02 -->

### mem-1793584366-6bdd
> P05-T07b: media attach after start = DbCallContextLoader (ctx or primary-leg org, status check) -> pod-wide ChannelHub.subscribe (single pubsub reader; subscribe waits for both confirmations) -> carrier_state snapshot -> CallSession(context, feed, carrier_state, control_handler); control acks are shielded tasks awaited in cleanup so a stop that ends the session still writes its ack; media tests use media_helpers.served/FakeContextLoader/call_context; redis-py health_check_interval makes concurrent pubsub reads during an unsubscribed SUBSCRIBE unsafe, so the reader only reads while pubsub.subscribed
<!-- tags: media, redis, testing | created: 2026-11-02 -->

### mem-1793582579-3e67
> P05-T07a: media WS attach = va_media.server.MediaAttach (app.state.media_attach) + auth.MediaAuth + ownership.CallOwnership + session.CallSession (SessionBody seam) + pods.PodRegistry + socket.StarletteSocket; tests media_helpers.served_media/signed_connect/close_code with ManualClock app clock; sync on registry.wait_idle after close; Starlette wraps send OSError as WebSocketDisconnect
<!-- tags: media, websocket, testing | created: 2026-11-02 -->

### mem-1793580673-6d31
> P05-T06d: worker container = va_worker.main.build_container (pydantic converter TemporalConnector, TelnyxKeyResolver in va_db.telnyx_keys, va_worker.activities.WorkerActivities/main_registrations); TemporalConnector connect seam takes (host, namespace, data_converter); worker e2e test runs real activities through TemporalWorkers on workflow_env with telephony_rig realtime and Rig.dialed event
<!-- tags: workflows, worker, temporal, testing | created: 2026-11-02 -->

### mem-1793579146-289e
> P05-T06c: CallWorkflow tests on Temporal time-skipping server: a running activity blocks time skipping AND env.sleep (use watch mode fail when timers must elapse); querying a running workflow while env.sleep wedged the test server; signals sent from inside fake activities (FakeCallActivities.send) are deterministic; Worker default/max_heartbeat_throttle_interval=100ms makes activity cancels arrive fast; execute_activity by name returns Any to mypy
<!-- tags: workflows, temporal, testing | created: 2026-11-02 -->

### mem-1793575125-f13d
> P05-T06b: va_workflows activities TelephonyActivities(dial,hangup_call)/CallEndActivities(end_call) keyword-only ctor; test rig packages/va_workflows/tests/support/telephony_rig.py (FakeTelnyx ASGI + recorded requests, fakeredis, SystemClock); FakeTelnyx AcceleratedClock jumps to time-limit while awaiting Postgres -> realtime=True for live-call tests; FakeTelnyx answers repeated command_id as duplicate ok even after hangup; DialSettings.allowed_countries must include US or admission blocks INTERNATIONAL_NOT_ENABLED
<!-- tags: workflows, telnyx, testing | created: 2026-11-01 -->

### mem-1793569614-ee33
> P05-T05b: SQLAlchemy async (greenlet) made coverage report phantom misses after awaited DB calls; fixed with [tool.coverage.run] concurrency=[greenlet,thread]. Send Call dispatch = va_api.services.call_dispatch; EventPublisher.sequence/broadcast + va_events.insert_rows for in-transaction call_events rows; api tests that dispatch need SessionContainer(clock=FrozenClock(call_helpers.business_hours_today())) for call_events partitions; pydantic Field(exclude_if=) to omit optional response fields (wrap model_serializer kills OpenAPI examples)
<!-- tags: api, calls, testing | created: 2026-11-01 -->

### mem-1793567413-771c
> P05-T05a: va_common.net.check_destination_url/blocked_address (Python marks IPv6 ::1 is_reserved -> loopback exempted from always-blocked reserved rule); WebhookSafetySettings mixin for WEBHOOK_ALLOW_PRIVATE; va_api.services.webhook_policy.check_call_webhook; api production settings in tests need kms_provider=aws + aws_kms_key_id + smtps:// smtp_url
<!-- tags: api, security, testing | created: 2026-11-01 -->

### mem-1793564621-892f
> P05-T04b: va_billing = plans/keys/scripts(Lua capped_incr, counter_release, concurrency_acquire)/limits.CallLimiter(acquire->LimitTicket.rollback)/holds/dispatch.admit_call; tests billing_helpers.limiter_rig (fakeredis+FrozenClock NOW) and billing_rows.new_billing_org (created_at defaults NOW-30d, else org counts as new account); new error code needs test_errors row-count bump; redis_container.flushed_redis shared helper
<!-- tags: billing, redis, testing | created: 2026-11-01 -->

### mem-1793563013-9d78
> P05-T04a: va_compliance.check_call_compliance(session, OrgFacts, destination=, region=, at=, now=, scheduled=, allowed_countries=) is Send Call step 9; DB tests use session fixture compliance_db_url + tests/support/compliance_rows (add_dnc via system_session for platform rows); callee_timezones falls back when phonenumbers returns >8 zones (NANP non-geographic); state_rules.json entries verified against statutes (MD/UT/AL/LA stricter than original spec table)
<!-- tags: compliance, calls, testing | created: 2026-11-01 -->

### mem-1793561163-497e
> P05-T03c: Send Call loader va_api.services.call_inputs.load_call_inputs(session, ctx, request, org=, encrypted_key=, models=, va_env=) + resolve_inputs; parts call_runners/call_pathways(PathwayFields)/call_refs/caller_id; va_common.nanp reads generated data/npa_states.json (scripts/generate_npa_states.py --check); tests use apps/api/tests/support/call_input_rows.Seed (row() via RowFactory, random_e164 since phone_numbers.e164 unique across orgs, agent versions need explicit created_at); never pkill -f pytest from the Bash tool (kills its own shell)
<!-- tags: calls, api, testing | created: 2026-11-01 -->

### mem-1793559625-a229
> P05-T03b: va_domain.call_resolution merge_layers -> MergedParameters then resolve_call_config(org, request, runner, refs, now) -> CallResolution; org must come from load_settings (fields_set decides layered default_*); test builders packages/va_domain/tests/support/resolution_helpers.py; resolved snapshots apps/api/tests/fixtures/bland_send_call_resolved regenerated via PYTHONPATH=packages/va_domain/tests/support uv run python apps/api/tests/support/resolved_snapshots.py --write; pydantic union loc adds tags like constrained-str to field paths
<!-- tags: calls, domain, testing | created: 2026-11-01 -->

### mem-1793556662-6cbd
> P05-T02: edge carrier handler = va_edge.mapping.plan_effects (pure) + va_edge.carrier.CarrierEventHandler (default registry main.carrier_handlers); CallRepo.transition guarded UPDATE raises CallTransitionError(.current); seed calls via va_db tests support call_rows.py; FrozenClock app clock makes EventProcessor 2s budget fire instantly (HandlerTimeoutError) -> use clock with real sleep; Temporal signal args sent as model_dump(mode=json) (default converter)
<!-- tags: telnyx, edge, testing | created: 2026-11-01 -->

### mem-1793554070-998d
> P05-T01: edge intake = va_edge.intake/keys/processor/reprocessor; edge tests share session fixture edge_db_url (filter by own ids; reclaims pick up other tests' failed rows); telnyx_events.updated_at is DB-clock (trigger) so reclaim tests use FrozenClock(now+60s); FakeTelnyx ids seeded -> seed=secrets.randbits(32) on shared DB; never parametrize random uuids (xdist)
<!-- tags: telnyx, edge, testing | created: 2026-11-01 -->

### mem-1793552401-ab71
> P04-T13: bench tests (pytest.mark.bench) in packages/va_voice/tests/bench are deselected by that conftest unless -m mentions bench; full non-xdist run rewrites packages/va_voice/BENCHMARKS.md; harness(clock=RealtimeClock()) runs on wall clock; outbound sends no idle frames without background
<!-- tags: voice, pipeline, testing | created: 2026-11-01 -->

### mem-1793550714-ba32
> P04-T12b fix: CallPipeline runs ActionHandler.handle in its own task awaited via asyncio.shield so reply cancels (barge-in/merge/watchdog) never cut actions; reply loop _settle_action waits for orphans; BargeInMonitor only sets _decided on honored decisions (segments re-evaluated after playback gaps); test fake SlowActions
<!-- tags: voice, pipeline, testing | created: 2026-11-01 -->

### mem-1793550102-2329
> P04-T12b: barge-in in CallPipeline (_check_barge_in after inbound batch + partial -> BargeInMonitor.evaluate -> Speaker.interrupt sync cut before first await -> clear -> tts.cancel -> cancel reply task -> brain.on_interrupted); FakeTts SpeakStatic context is final immediately so TTS-cancel tests need SpeakText(hanging()); harness ScriptedBrain.bind logs brain.closed/interrupted in transport.calls; parametrizing with module-level ScriptedBrain instances is OK (one use each)
<!-- tags: voice, pipeline, testing | created: 2026-11-01 -->

### mem-1793548904-55b3
> P04-T12a: va_voice.pipeline CallPipeline (TaskGroup of inbound/stt/tts/outbound/endpoint/replies/watchdog; reply tasks are plain tasks owned by the reply loop because creating a TaskGroup child while it aborts raises and leaks the coroutine); harness tests/support/pipeline_harness.harness on sim AcceleratedClock with pipeline_fakes (FakeCaller streams 20 ms frames forever, hanging(text) keeps a TTS context open); deadlines via pipeline.waits.within on the injected clock; ruff --unsafe-fixes rewrote zip(a[3:],a[4:]) to pairwise(a) - never apply blindly
<!-- tags: voice, pipeline, testing | created: 2026-11-01 -->

### mem-1793547327-d126
> P04-T11: va_voice.phrase_cache PhraseCache single-flight via cache-owned tasks + asyncio.shield (done callback retrieves exception); log redactor masks any field containing 'key' -> log cache entries as entry=; va_voice tests can import va_elevenlabs test support (rest_client_helpers.sim_rest)
<!-- tags: voice, tts, testing | created: 2026-11-01 -->

### mem-1793546380-d277
> P04-T10: va_voice.latency TurnTimer.intervals() computed once and shared by latency()/TurnTracer.emit so misordered warnings log once; retroactive OTel spans via start_span(start_time=ns)+end(end_time=ns), anchor ns from (clock.now()-epoch)//timedelta(microseconds=1)*1000; va_voice tests/conftest.py re-exports reset_logging; use exactly-representable floats in tests (mypy rejects pytest.approx in tuple equality: comparison-overlap)
<!-- tags: voice, latency, testing | created: 2026-11-01 -->

### mem-1793545684-8d30
> P04-T09: va_voice.outbound FramePacer anchors targets at max(0,n-lead)*20 after the anchor, re-anchors when drained (now >= anchor+sent*20), mark_gap so the wait after idle is not lateness; OutboundAudio clear() uses a generation counter for frames in flight; SpectralGate returns whole 256-sample hops (input delayed 256), Rayleigh p20->mean scale 1.876 gives -11.6 dB; ManualClock float ms noise ~1e-14, so round sink timestamps
<!-- tags: voice, outbound, testing | created: 2026-11-01 -->

### mem-1793544526-4d65
> P04-T08: va_voice.playback.PlaybackTracker owns text.alignment.AlignmentTimeline (played_text); timeline uses prefix-max end bisect + lazy trailing-gap interpolation; test Alignment dataclass shared in tests/support/text_helpers.py (jscpd flagged duplicate)
<!-- tags: voice, playback, testing | created: 2026-11-01 -->

### mem-1793543716-f7d6
> P04-T07: va_voice.text.pronunciation.PronunciationApplier compiles one alternation regex (stable longest-first, (?i:) scoped per rule); OffsetMap.to_original floor (inside replaced span -> span start), to_original_ceil; tests compare with naive reference scan; C++C is not a C++ match because of the (?!\w) lookahead
<!-- tags: voice, pronunciation, testing | created: 2026-11-01 -->

### mem-1793543142-df5b
> P04-T06: va_voice.text.chunker ClauseChunker uses MarkdownStripper.space_pending to fire end-of-buffer boundaries; hypothesis split-invariance tests (tests/support/text_helpers.split_at); Write tool may turn \uXXXX escapes in string literals into literal chars -> RUF001, re-escape
<!-- tags: voice, chunker, testing | created: 2026-11-01 -->

### mem-1793542103-8376
> P04-T05: va_voice.interrupt InterruptPolicy.should_interrupt -> truthy InterruptDecision(reason); BACKCHANNELS checked verbatim against specs/07 §7 paragraph in test_interrupt.py; level thresholds in LEVEL_RULES; effective_level block->interruptible False->node->call
<!-- tags: voice, interrupt, testing | created: 2026-11-01 -->

### mem-1793541550-b3f7
> P04-T04: va_voice.endpointing.Endpointer is pure ms-timeline (on_vad/on_partial, deadline_ms, poll -> EndOfTurn|TurnDiscarded); va_voice.text_norm shared tokenizer for T05 backchannels; mypy narrows Endpointer properties across asserts -> use snapshot(ep) tuple helper in tests; apps/sim test_sim_shared_redis can flake under full-suite load (exit -15)
<!-- tags: voice, endpointing, testing | created: 2026-11-01 -->

### mem-1793540698-998b
> P04-T03: Silero VAD = va_voice.vad_model.SileroModel (manifest-verified ORT session, onnxruntime mypy override) + va_voice.vad.SileroVad(model, VadExecutor(threads)); pool computes on state copies so cancelled feeds are safe; fixtures generated by packages/va_voice/tests/support/vad_fixtures.py --write (byte-identical test); real Silero detects va_sim formant speech 100%, ends lag +56..98 ms (hangover); CI check job fetches models
<!-- tags: voice, vad, testing | created: 2026-11-01 -->

### mem-1793539212-a7f4
> P04-T01: va_voice.audio (codec/formats/framing/resample/levels); soxr has no py.typed (mypy override, only resample.py calls it); soxr LQ ResampleStream emits nothing for first chunks - assert totals after Resampler.flush(); dBFS = RMS/32767; jscpd flags known-vector parametrize tables identical to va_sim tests - use a combined (sample, code, decoded) table
<!-- tags: audio, voice, testing | created: 2026-11-01 -->

### mem-1793538351-5d78
> P03-T17 fix: CallEventWriter must be idempotent (ON CONFLICT (created_at,id) DO NOTHING) and report partial failure via CallEventWriteError.unwritten; buffer re-queues only those. Partial-failure DB test trick: rows dated outside all call_events partitions fail one org's insert
<!-- tags: events, database, testing | created: 2026-11-01 -->

### mem-1793537886-dfee
> P03-T17: va_events bus = catalog/keys/model/publisher/call_events/buffer/consumer; tests via event_helpers.publisher_rig (fakeredis max_connections=256 for 200 concurrent publishes; fakeredis supports XAUTOCLAIM+pubsub); call_events DB tests need real time (partitions only around migration month); shared migration_helpers.migrated_postgres() for module DB fixtures
<!-- tags: events, redis, testing | created: 2026-11-01 -->

### mem-1793536549-931f
> P03-T16: live smoke suite in tests_live/ (support modules live_env/live_clients/live_wer/live_fixture on pytest pythonpath + mypy_path); module pytestmark live+skipif LIVE_TESTS!=1, per-key skip in conftest fixtures; test_live_gate runs child pytest with PYTEST_/COV_CORE_/LIVE_ env stripped and asserts '7 skipped' (update LIVE_TEST_COUNT when adding live tests)
<!-- tags: testing, live | created: 2026-11-01 -->

### mem-1793535767-6ed1
> P03-T15c: SIM_STATE_REDIS_URL shares scenarios/latencies/reset+seed/utterance ids via va_sim.shared.SharedState (Lua writes, sim:state:* keys, pubsub sim:state:events); shared writers use async SimState methods (reset_all/add_scenario/set_latencies/set_profile/allocate_utterance, SharedStateUnavailable->503 state_unavailable), readers await state.sync(); tests = two SimStates on FakeAsyncRedis(server=FakeServer()), server.connected=False for outage, wait_applied(version) for subscriber; redis_container.running_redis_url + sim_helpers.control_client shared helpers
<!-- tags: sim, redis, testing | created: 2026-11-01 -->

### mem-1793534492-10f7
> P03-T15b: infra/docker/python.Dockerfile builds app images (SERVICE=app dir, dist name sed from apps/$SERVICE/pyproject.toml, uv 0.13.6); test_dockerfile.py compares it line-for-line with specs/29 §1.1 + deviations; compose sim in infra profile, dev_up runs up --build then scripts/sim_public_key.py (TELNYX_PUBLIC_KEY in .env); shared dotenv updater scripts/env_file.py
<!-- tags: docker, sim, scripts | created: 2026-11-01 -->

### mem-1793533708-ff5c
> va_telnyx P03-T15a: bootstrap_account in va_telnyx.bootstrap (exact-name find-or-create, drift PATCH only drifted fields); scripts/telnyx_bootstrap.py tested end-to-end via asyncio.to_thread(main) + running_sims(state, ports={'telnyx': 0}); settings subclasses must declare service_name as ClassVar[str]
<!-- tags: telnyx, testing, scripts | created: 2026-11-01 -->

### mem-1793532827-54b2
> va_llm P03-T14b: Embeddings (embeddings.py) separate from LlmGateway; pure embedding_wire (parse/normalize/truncate/emb:q:{sha} key), embedding_cache.QueryCache fail-open; tests embeddings_rig/erig fixture (fakeredis + ManualClock), embedding_body, sim_embeddings(state); specs/03 kbq: row replaced by emb:q 24h
<!-- tags: llm, testing, embeddings | created: 2026-11-01 -->

### mem-1793529774-bd6f
> va_llm P03-T14a: LlmGateway.call_structured -> structured_calls.run_structured (each request = one complete call); pure parts in va_llm.structured (TOOL_NAMES, tool_schema inlines $defs keyword-aware, validate_arguments -> InvalidOutputError); registry process-global -> va_llm conftest autouse fresh_structured_registry; drift test AST-scans call sites for literal function_name; pydantic docstrings become schema description sent to the model
<!-- tags: llm, testing, structured | created: 2026-11-01 -->

### mem-1793528014-c05e
> va_llm P03-T13b: streaming dialogue in va_llm.dialogue (DialogueStream producer task; END delivered by task done-callback); httpx ASGITransport buffers whole bodies so FakeLLM streaming contract tests use llm_client_helpers.served_gateway (real uvicorn); respx streams via streamed(*parts)/StreamingBody (bytes|Exception|asyncio.Event hang); wait_for_sleep(TTFT_S) before first token, TOTAL_S after
<!-- tags: llm, testing, streaming | created: 2026-11-01 -->

### mem-1793526011-80a2
> va_llm P03-T13a: LlmGateway.complete raises only LlmUnavailableError(reason,last); openai 3.x typed for httpx2 -> build SDK clients via va_llm.clients.openai_client + sdk_timeout; respx records calls only on completion -> assert Hanging.requests/cancelled and mix via llm_client_helpers.scripted(); tests gateway_rig (ManualClock wait_for_sleep+run_for), sim_gateway(state)->(rig, control)
<!-- tags: llm, testing, openai | created: 2026-11-01 -->

### mem-1793523727-35ff
> va_elevenlabs P03-T12: ElevenLabsRest REST ops go through ElevenLabsTransport.call(op, path, parse) (parse inside span so bad bodies count as failures) or observed(op, work)+Exchange.send for paged ops; per-op RestRetry in rest_retry (add/edit never retried); tests: conftest rig/api fixtures (respx on rest_client_helpers.BASE_URL, RecordingClock), sim_rest() over FakeElevenLabs ASGI, form_parts() decodes multipart via stdlib email; specs/27 §3.6 rows parsed by obs_helpers.vendor_metric_rows(prefix)
<!-- tags: elevenlabs, testing, rest | created: 2026-11-01 -->

### mem-1793522341-0949
> va_elevenlabs P03-T11: ElevenLabsRealtimeStt (stt_realtime) keeps all sent audio in ReplayBuffer until a later commit is answered, re-sends the collapsed outstanding commit at its mark after reconnect; tests use stt_client_helpers RecordingSttServer (greetings deque) + stt_rig on ManualClock (wait_for_sleep before run_for for reconnect delays), contract tests via contract_helpers.sim_served; mypy narrows properties across asserts -> compare tuples from a helper fn
<!-- tags: elevenlabs, testing, stt | created: 2026-11-01 -->

### mem-1793520581-4eea
> va_elevenlabs P03-T10: TtsSocket (tts_ws) state-first-then-write under one lock so reconnect replay repairs failed writes; flush(id) = end of utterance (close_context after tail grace), send(flush=True) only forces generation; tests use tts_client_helpers RecordingTtsServer + rig(url) on ManualClock; ManualClock.wait_for_sleep(s) before run_for(s) when the sleeper starts after real socket I/O
<!-- tags: elevenlabs, testing, clock | created: 2026-11-01 -->

### mem-1793518547-0b04
> va_telnyx P03-T09: inbound Telnyx verification in va_telnyx.signatures (verify_webhook/verify_media_ws, WebhookVerificationError.reason), webhooks.parse_envelope/parse_payload (UnknownEventPayload for unmodelled types e.g. number_order.complete), voice_events one model per specs/06 §5 row (test parses the §5 table), media.parse_media_message fast path; contract tests verify FakeTelnyx webhooks with state.signing_key.public_key_b64 + state.clock.now(); helpers telnyx_client_helpers.provisioned/sms_number
<!-- tags: telnyx, testing | created: 2026-11-01 -->

### mem-1793517443-86b9
> va_telnyx P03-T08b: TelnyxClient = client.py call commands + mixins ProvisioningCommands/NumberCommands/MessagingCommands; transport.request_pages (one metric obs per list), transport.download (streamed, file removed on failure); TelnyxClientFactory resolve_key async fn + 5 min cache on clock.monotonic; tests use telnyx_client_helpers.sim_http(state) for factory/BYO (POST /sim/telnyx/accounts registers BYO key)
<!-- tags: telnyx, testing | created: 2026-11-01 -->

### mem-1793516463-a13e
> va_telnyx: TelnyxClient wraps transport.TelnyxTransport.request(Operation(...)); request models use Field(serialization_alias='from') since mypy has no pydantic plugin (alias= breaks from_=); tests use telnyx_client_helpers.rig(http)/sim_clients(state)/leg_state + telnyx_api respx fixture; RecordingClock shared in packages/va_common/tests/clock_helpers.py
<!-- tags: telnyx, testing | created: 2026-11-01 -->

### mem-1793513723-04da
> va_sim FakeStripe: va_sim.stripe (http.stripe_endpoint pipeline, handlers raise StripeError, routes._API table + catch-all 404); webhook post/retry shared via va_sim.delivery.SignedDelivery (telnyx too); tests use stripe_helpers stripe_state(RecordingTransport)/stripe_client/encode/signature_ok; stripe SDK HTTPXClient triggers httpx data= DeprecationWarning -> module pytestmark filter + await close_async()
<!-- tags: sim, stripe, testing | created: 2026-11-01 -->

### mem-1793445974-9a2b
> va_sim FakeLLM structured calls: heuristics.structured_arguments -> heuristics_post/heuristics_base, always via schema_fill.SchemaFill.conform (validates against tool parameters, tests cross-check jsonschema); embeddings in llm/embeddings.py + embed_route.py, shared auth/fault answers in llm/gate.py; faults have endpoint chat|embeddings; /sim/state llm snapshot now has embeddings count (test_state, test_llm_control, test_llm_faults assert it)
<!-- tags: sim, llm, testing | created: 2026-10-31 -->

### mem-1793444234-6f82
> va_sim FakeLLM: va_sim.llm (params/rules/reply/wire pure; chat/store/control); tests use llm_helpers llm_state (accelerated, virtual-ms pacing) / realtime_llm_state+served_llm (sockets, openai SDK) / wait_held(state,n) for timeout faults; fault_route + yaml_or_json_body live in va_sim.control; never run ruff --fix with a narrowed --select incl RUF100 (strips other noqa)
<!-- tags: sim, testing, llm | created: 2026-10-31 -->

### mem-1793442554-94a4
> va_sim FakeElevenLabs voices: TTS WS/REST now 404/1008 voice_not_found for unknown voice ids - tests must use seeded ids (tts_helpers.VOICE_ID = voice_catalog.KAREN_VOICE_ID); multipart parsing via rest_io.multipart_form (InvalidBodyError.response), JSON bodies via rest_io.parse_json_body; voice tests use voice_helpers.add_voice/wav_sample/mp3_sample
<!-- tags: sim, testing, elevenlabs | created: 2026-10-31 -->

### mem-1793337252-fee0
> va_sim FakeElevenLabs STT: va_sim.elevenlabs stt_ws/stt_stream(pure)/stt_store (ElevenLabsStore.stt); tests use stt_helpers.caller_audio(state,text,fmt)+chunked+send_audio+recv_type on realtime_state; segment resolves after 100 ms voiced + 260 ms marker window, so mid-utterance commits need >=300 ms of speech; markerless test speech = b'\x10\x90'*n μ-law
<!-- tags: sim, testing, elevenlabs | created: 2026-10-30 -->

### mem-1793334623-f891
> va_sim FakeElevenLabs TTS: va_sim.elevenlabs (tts_ws TtsSocket, tts_rest, store on SimState.elevenlabs); WS tests use tts_helpers.realtime_state + served/tts_socket/recv_spoken; audio is unpaced so close_context right after flush yields no audio - read audio first or arm a delay fault; Starlette WebSocketRoute needs a plain async def endpoint (partial = ASGI app)
<!-- tags: sim, testing, elevenlabs | created: 2026-10-30 -->

### mem-1793330327-17d9
> va_sim caller runner: CallerScript (telnyx/caller_script.py + caller_steps.py) starts via CallMedia.media_connected after the media start frame; MediaSession.add_listener(on_tick(AgentTick)/on_media_end) must stay O(1); completion via queue_caller/queue_dtmf offsets vs caller_sent/dtmf_sent; CPU prep under va_sim.tasks.hold_clock + to_thread costs no virtual time
<!-- tags: sim, testing, clock | created: 2026-10-30 -->

### mem-1793327751-3934
> va_sim media WS: tests giving a stream a stream_url must use the media_agent fixture (tests/telnyx/conftest.py, support/media_agent.py); MediaSession tick loop always sleeps 20 ms ahead so accelerated virtual time races during socket waits - assert orders/counts; never tasks.drain() with a live stream; the session freezes accelerated time while connecting via a sleep(0) holder task
<!-- tags: sim, testing, clock | created: 2026-10-30 -->

### mem-1793326334-2ae5
> va_sim.audio: synthetic caller audio (synthesize_utterance/pcm16/wire, decode_marker, goertzel_power, synthesize_dtmf, own G.711 codec); seed with SimState.utterance_seed(id); utterance ids 16-bit; audio must not import va_sim.state; synthesis is CPU-bound -> asyncio.to_thread; body envelope has a -12 dB floor so VAD sees continuous speech
<!-- tags: sim, audio | created: 2026-10-30 -->

### mem-1793323959-b262
> va_sim AcceleratedClock advances only when loop._ready is empty; tests drive FakeTelnyx calls with telnyx_helpers.at(state, s)/settle(state) (both also await state.call_engine.idle() since recording WAVs encode in a thread); a test busy-polling asyncio.sleep(0) freezes accelerated time; an in-process ASGI request can finish without yielding, so settle (clock.sleep(0)) before sleeping to a tie deadline
<!-- tags: sim, testing, clock | created: 2026-10-30 -->

### mem-1793318126-f3a5
> va_sim: control API /sim/* via va_sim.server.simulator_routes(sim, state); tests use running_sims(state) (real uvicorn, ephemeral ports) or httpx.ASGITransport over Starlette(routes=simulator_routes(...)); sim_helpers (packages/va_sim/tests/support) has new_state/scenario_yaml and the shared autouse isolated_env fixture that both va_sim and apps/sim conftests re-export; dist names va-sim (lib) / va-sim-server (apps/sim)
<!-- tags: sim, testing | created: 2026-10-29 -->

### mem-1793292824-0b49
> va_api: a new /v1 route family must be added to specs/31 §1 envelope matrix AND va_api.envelopes.FAMILIES (test_envelopes compares them verbatim and counts them), otherwise the path classifies as legacy; Redis CAS pattern: pipe.watch -> read -> pipe.multi() (type: ignore[no-untyped-call]) -> set(keepttl=True), WatchError = lost race
<!-- tags: api, redis, testing | created: 2026-10-29 -->

### mem-1793291061-0ce1
> va_api request gates read org settings via OrgSettingsReader.get_lenient (invalid stored keys -> defaults); get_principal enforces ip_allowlist after auth; tests vary the client address with tests/support/peer.py PeerSwitch(session_app(c)) + session_client; DB-less AuthContainer has MemoryOrgSettings; settings PATCH with non-empty ip_allowlist must include caller 127.0.0.1
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793289699-6a26
> va_api org settings: write via routers.org_settings.check_settings_permissions + apply_settings_patch (locks org row, merges, validates, audit diff keyed settings.<path>), then OrgSettingsReader.invalidate after commit; tests swap container.org_settings to inject broken Redis; INVALID_INPUT field format is field_path (items[0].name)
<!-- tags: api, settings, testing | created: 2026-10-29 -->

### mem-1793288415-ea3a
> va_domain.org.OrgSettings: orgs.settings stored sparse (load_settings/sparse_settings); PATCH body parsed with OrgSettings + exclude_unset, merge_settings (groups key-wise), needs_compliance, field_path(loc). pydantic EmailStr rejects .test domains used in fixtures - use email_validator.validate_email(check_deliverability=False, test_environment=True)
<!-- tags: api, pydantic, settings | created: 2026-10-29 -->

### mem-1793287252-a730
> va_api audit markers: every POST/PUT/PATCH/DELETE route needs openapi_extra {**audited('<action>',...)} or {**audit_exempt('<reason>')} (test_audit_markers walks app.state.routers). Dashboard auth rows use va_api.audit.audit_user(session, request, org_ids, user_id, action) - one row per membership org; signing in now writes auth.login rows, so tests listing a whole org log must filter auth.* or remove them via system_session
<!-- tags: api, audit, testing | created: 2026-10-29 -->

### mem-1793285970-ba4c
> va_api audit rows: call va_api.audit.audit(session, ctx, action, resource_type, resource_id, before=, after=) inside the route's open session; va_db.audit.record flushes under use_org(actor org) so it works on global sessions too; new actions go in specs/04 §13 AND va_db.audit.AUDIT_ACTIONS (test compares); audit_logs is append-only for va_app (0004) so seed/cleanup via system_session; paginated envelope lists use va_api.responses.PageResponse[T]
<!-- tags: api, database, audit | created: 2026-10-29 -->

### mem-1793282863-1d8b
> va_api sub-accounts: get_principal applies X-VA-Org-Id via auth.sub_accounts.act_on_sub_account before rate limit/scope/idempotency; use ctx.principal_org_id (not ctx.org_id) for the caller's own API key/membership rows; session child active org via session_auth.resolve_switch_target (MembershipLookupRepo.via_parent). Tests: session_helpers.sub_account / sub_account_member; structlog capture_logs misses contextvars+cached loggers in full suite - wrap structlog.contextvars.bind_contextvars instead
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793281395-6e00
> va_api: routes returning one-time secrets call va_api.idempotency.withhold_on_replay(request, 'api_key' | 'data.key'); stored idempotency body and replays get null. Session tests asserting idempotency_keys rows: monkeypatch container.idempotency = DbIdempotencyStore(identity.database, container.clock)
<!-- tags: api, testing, auth | created: 2026-10-29 -->

### mem-1793280879-041b
> va_api sub-accounts: va_db.session.use_org(session, org_id) switches RLS org inside one va_app transaction (CreditTransferRepo writes parent+child ledger rows); SessionContainer.authenticator falls back to DB lookup so route-minted keys authenticate; session_helpers.fund/hold/set_org/stored_org
<!-- tags: api, database, testing | created: 2026-10-29 -->

### mem-1793279423-eb1e
> va_api tests: idp (FakeOidcProvider on container clock) and sso (OIDC_ENABLED client) fixtures live in apps/api/tests/conftest.py; invitation helpers session_helpers.invite(client, container, email, role)->token, INVITATIONS, ACCEPT_INVITATION; session_auth.optional_session for routes that work with or without a session
<!-- tags: api, testing, auth | created: 2026-10-29 -->

### mem-1793275386-bcdb
> va_api tests: session_helpers.signed_in(client, identity, container, role) + add_member(identity, hasher, org_id, role) for extra members; DataResponse[list[Model]] now wraps model examples (test_operations_contract requires examples on every 2xx)
<!-- tags: api, testing, openapi | created: 2026-10-29 -->

### mem-1793272997-3347
> SSO tests: tests/support/fake_oidc.py FakeOidcProvider(now=container.clock.now) + mocked_idp(provider) + sso_app(container) + configure_sso(client, identity, container) + sso_login(client, provider, email); redirect-only routes listed in test_openapi.REDIRECT_OPERATIONS
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793271457-e7c7
> va_api: outbound fetches to admin URLs use va_api.auth.oidc_discovery.OidcDiscovery (no redirects, 5s, 256KiB, is_global check unless VA_ENV local/test); tests stub IdPs with respx.mock (patches httpcore only, ASGI client untouched); after make openapi also run 'corepack pnpm gen:api' in sdks/typescript and apps/dashboard (schema.test.ts compares)
<!-- tags: api, testing, openapi | created: 2026-10-29 -->

### mem-1793269535-3669
> MFA test helpers: session_helpers.enrol_mfa(client, container) + mfa_code(container, secret); MfaGuard replay guard accepts each TOTP step once per user, so advance FrozenClock 30 s before reusing the current code in another session; gate is session_auth.gated_membership (org OR user MFA), counters with lock reuse auth.failure_counter.FailureCounters
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793268366-7e50
> SQLAlchemy model column added by a later ALTER TABLE ADD COLUMN: give it mapped_column(..., sort_order=1) so it sorts after TimestampMixin created_at/updated_at like the DB attnum (test_tenancy_models column-order lists); Mapped[list[str]] .any(x) fails mypy, use literal(x, Text) == any_(col)
<!-- tags: database, sqlalchemy | created: 2026-10-29 -->

### mem-1793267153-d5f5
> va_api: routes added to an existing router from a second module (routers/auth_password.py on internal_auth_router) need an import-for-side-effect in va_api/main.py; shared test helper session_helpers.error_of(response) -> (status, code, field) - don't redefine _error per module (jscpd)
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793265656-71f0
> va_api session integration fixtures (migrated_url module-scope, identity, container=SessionContainer(identity=...), client) live in apps/api/tests/conftest.py; modules only add extras (member). Copying them per module trips jscpd. FastAPI drops None values from OpenAPI examples; MFA_REQUIRED is HTTP 401
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793264176-ecbf
> fakeredis connection-failure stub: server=fakeredis.FakeServer(); server.connected=False; FakeAsyncRedis(server=server) raises ConnectionError for commands AND pipelines (overriding execute_command does not cover pipeline.execute)
<!-- tags: testing, redis | created: 2026-10-29 -->

### mem-1793264176-b270
> va_api session tests: serve the app over https (make_client(app, base_url='https://...')) - cookies are Secure outside VA_ENV=local and httpx drops Secure cookies on http; httpx ASGITransport peer is 127.0.0.1 but Starlette TestClient's is 'testclient' (not an IP; IdentityRepo.create_session raises ValueError on it, so use session_auth.client_ip)
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793262412-7bed
> va_db identity tests: identity_db fixture (conftest, support/identity_db.py) gives Database + local-KMS Envelope + OrgProvisioner; row_counts(db) asserts provisioning atomicity. Pool size is 1 per role, so never nest two app sessions (tenant/global) in one test (it deadlocks on pool checkout)
<!-- tags: database, testing | created: 2026-10-29 -->

### mem-1793261328-920d
> va_common.email.EmailSettings lives in the email package, not va_common.settings: va_common.logging imports va_common.settings, so settings -> email.sender -> logging is a cycle. check_no_placeholders flags example.com in production docstrings
<!-- tags: email, settings | created: 2026-10-29 -->

### mem-1793260371-4c41
> Idempotency decision logic is va_db.repositories.resolve_existing (pure); test MemoryIdempotencyStore reuses it - never re-copy the decision chain (jscpd)
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793260371-1459
> va_api pure-ASGI middleware: subclass va_api.middleware.HttpMiddleware (http-only passthrough) and override handle(); jscpd flags a copied 'if scope[type] != http' preamble
<!-- tags: api, tooling | created: 2026-10-29 -->

### mem-1793258976-9d0b
> va_api.rate_limit must not import va_api.auth at runtime (va_api.auth.dependencies imports it): RequestContext is TYPE_CHECKING-only; rate_limit("calls_send") dependency lives in va_api.auth.dependencies
<!-- tags: api | created: 2026-10-29 -->

### mem-1793257682-de8e
> Adding an env var to specs/02 §6: update scripts/tests/test_env_example.py EXPECTED_SPEC_NAMES, EXPECTED_LITERAL_KEYS and the DevValueKind count dict, plus the '106 names in 105 rows' counts in the §6 Decision text
<!-- tags: tooling, testing | created: 2026-10-29 -->

### mem-1793257682-8f0e
> FastAPI in this repo nests included routers in app.routes as fastapi.routing._IncludedRouter (no .path); enumerate APIRoutes via app.state.routers (route.dependant for require_scope.required_scope)
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793135968-8be6
> clickhouse-connect 1.11 async: create_async_client does a network round-trip (server version/settings) so open it in a supervised task; network errors and retried server errors raise OperationalError (subclass of DatabaseError); AsyncClient satisfies va_events ClickHouseCommands Protocol; server-side {name:Type} params work in INSERT ... SELECT
<!-- tags: clickhouse, testing | created: 2026-10-27 -->

### mem-1793130055-a7eb
> va_db schema checks: schema_catalog.snapshot() compares spec-applied vs migrated DB; autogenerate_diff (alembic 1.20.1) has zero noise on expression/partial/hnsw indexes, vector(1024), server defaults - any new exclusion must be a named commented exception in schema_catalog.include_object; Postgres "char" columns (contype, attgenerated) come back as bytes via asyncpg - cast ::text
<!-- tags: database, alembic, testing | created: 2026-10-27 -->

### mem-1793125348-d9c3
> va_db model tests: shared helpers in model_introspection (assert_org_indexed, org_index_ddl, enum_check_names, expected_foreign_keys, assert_enum_checks_mirror) - use them instead of per-file parametrized copies, jscpd flags the 10-line windows
<!-- tags: database, testing | created: 2026-10-27 -->

### mem-1793122444-3076
> va_db partitioned tables (call_events): PrimaryKeyConstraint must include partition key; __table_args__ trailing dict {'postgresql_partition_by': 'RANGE (created_at)'}; server-default-only PK part (created_at) is filled by asyncpg RETURNING at flush; create_all makes only the parent so tests CREATE TABLE ... PARTITION OF ... FOR VALUES FROM (date_trunc('month', now())) TO (...) themselves
<!-- tags: database, sqlalchemy, testing | created: 2026-10-27 -->

### mem-1793120870-f54b
> va_db models: use _columns builders (org_id_column, fk_column, int_column, bool_column, text_column, text_array_column, jsonb_column) - jscpd flags repeated mapped_column blocks; overlapping import headers can be wrapped in '# jscpd:ignore-start' / '# jscpd:ignore-end  (reason)' (bare end marker trips ruff ERA001)
<!-- tags: database, testing, tooling | created: 2026-10-27 -->

### mem-1793119288-abc7
> va_db model tests: reuse tests/support/model_introspection (get_table etc.) + model_factories (make_org, assert_enum_check_enforced) and conftest scratch_session; copying tenancy test bodies trips jscpd clone gate
<!-- tags: database, testing | created: 2026-10-27 -->

## Decisions

### mem-1793604160-bb3f
> P05-T11b plan: media end routine runs in CallSession between _race and _cleanup (lock still held); stream_closed re-reads carrier_state primary_hangup then waits 3 s on feed.next_carrier; worker_lost → no hangup/no row, CallWorkflow hangs up; shared va_db.call_end.record_answered_end with end_call activity
<!-- tags: media, calls, workflows | created: 2026-11-02 -->

### mem-1793562105-042f
> P05-T04 split: T04a va_compliance (fraud prefixes, DNC with parent inheritance via use_org, international policy reconciling specs/24§6.7+28§9.1, callee_timezones/in_window, state_rules.json, check_call_compliance order prefix>DNC>intl>hours); T04b va_billing limits/holds/dispatch.admit_call + move org_schemas.effective_limits to va_billing.plans
<!-- tags: calls, compliance, billing | created: 2026-11-01 -->

### mem-1793128976-28d0
> Every new migration must GRANT SELECT, INSERT, UPDATE, DELETE on new tables to va_app, va_system and add §10 trigger/RLS; RLS = NOT NULL org_id + voices/dnc_entries; call_events partitions call_events_pYYYYMM (UTC months)
<!-- tags: database, alembic | created: 2026-10-27 -->

## Fixes

### mem-1793787934-5c75
> e2e flakes 2026-11-04: interruptibility_levels[1] fails ~40% (barge clear 1183ms vs 1200 rule: assertion measures from utterance start+MARKER_MS but pipeline counts VAD speech_ms); amd_ivr_mode lost 2nd DTMF once on cold stack. Re-run alone as evidence; task fix:e2e-interrupt-level1
<!-- tags: e2e, testing | created: 2026-11-04 -->

### mem-1793756551-21d0
> failure: cmd=make test, exit=2, error=INTERNALERROR worker crash: apps/media_worker/tests/test_runtime_sim.py::test_wait_for_greeting_holds_the_opener_until_the_callee_speaks hit pytest-timeout 60s and the hypothesis makereport hook raised PluggyTeardownRaisedWarning (aborts the xdist session); module passes alone (31 passed). next=re-run make test; load-related
<!-- tags: testing, media | created: 2026-11-04 -->

### mem-1793747646-e8bf
> fix: xdist 'worker crashed ... Not properly terminated' in make test = kernel OOM kill (read via: docker run --rm --privileged --net=none redis:7-alpine dmesg | grep -i 'killed process'). Cause: FakeTelnyx recorded leg on accelerated clock jumps to 4 h time limit and encodes ~460 MB WAV (test_accept_db recording test). Fix: routing rig ring/place(time_limit_s=30). Find RSS hogs with a -p plugin logging /proc/self/statm per test
<!-- tags: testing, sim, edge | created: 2026-11-03 -->

### mem-1793715612-b9ec
> Python int(str) raises ValueError beyond 4300 digits: any never-raise coercion of model/caller text (int(digits)) must cap length or catch ValueError
<!-- tags: engine, testing | created: 2026-11-03 -->

### mem-1793702021-fdbe
> Runtime sim agent-track window tests must anchor on Call.mark_echoes[-1].track_ms (FakeTelnyx mark echo = where that agent audio ended in the aligned track), not fixed ms after answer: under load the opener starts late
<!-- tags: media, sim, testing | created: 2026-11-03 -->

### mem-1793675044-c0b4
> flake: apps/media_worker/tests/test_runtime_sim.py::test_the_background_bed_fills_the_agent_track_between_utterances[none-False-None] failed once under full make check (idle -21.8 dBFS vs -inf); passes in isolation
<!-- tags: media, testing, flaky | created: 2026-11-03 -->

### mem-1793660249-6e32
> P05-T17e: e2e load copies of one scenario need Harness.load_scenario(instance=n) (same run token = same scenario id, later registrations replace earlier ones -> calls get scenario_id null); 50 pollers at 0.25 s hit 429 on scale plan; compose Postgres max_connections default 100 runs out (temporal ~32). py-spy into containers: docker cp static binary from uvx cache, docker exec --privileged -u 0 /tmp/py-spy record --pid <python pid> --idle --threads [--native needs blocking mode]; --gil view undercounts main thread because numpy/soxr release the GIL
<!-- tags: e2e, media, performance | created: 2026-11-02 -->

### mem-1793647998-0ae8
> Flake: test_runtime_sim background-bed test (fixed 2000-2800 ms window on wall clock) can overlap the opener under full make test load; measure relative to opener end
<!-- tags: media, testing, flaky | created: 2026-11-02 -->

### mem-1793646897-5199
> P05-T17a: docker compose env_file reads 'KEY=  # comment' as the comment text (python-dotenv reads empty) -> apps get junk secrets; dev_up renders .env to single-quoted .env.compose via scripts/compose_env.py. va_telnyx.webhook_urls(scheme=webhook_scheme(va_env)) is http in local/test so the compose sim reaches http edge. Migrations packaged in va_db/migrations; python -m va_db.migrate = head + call_events partitions
<!-- tags: docker, compose, e2e | created: 2026-11-02 -->

### mem-1793645038-024d
> Monitor on a log that make test truncates later reads the previous run's exit= line: delete the log (or sleep) before arming the monitor. Temporal workflow handlers that swallow ActivityError must re-raise CancelledError after a self-initiated cancel; a heartbeating blocking fake activity keeps time-skipping on wall time, so pair with a budget of ~2 s
<!-- tags: testing, temporal, workflows | created: 2026-11-02 -->

### mem-1793640192-b97e
> failure: make test (review P05-T16b) exit=2, packages/va_workflows/tests/test_end_call_activity.py::test_a_transferred_call_splits_connected_and_transfer_seconds connected_seconds 61.072 vs 60 (+-1 s wall-clock tolerance) under xdist load; passes alone 3/3; next=derive expected durations from the rig clock instead of wall time
<!-- tags: workflows, testing | created: 2026-11-02 -->

### mem-1793633185-349b
> build.done backpressure: 'complexity: 2.45.' (trailing period) is not parsed -> rejected; write 'complexity: 2.45,' and keep it < 10
<!-- tags: tooling, ralph | created: 2026-11-02 -->

### mem-1793615226-47e9
> P05-T10f: Temporal time-skipping test server does not skip time for an abandoned child workflow awaited from the client (result()/env.sleep stall ~20s, 'No completion event found'); assert child start input + TIMER_STARTED duration from history and terminate it. Voicemail retry = va_domain.call_retry + schedule_retry_call activity (child = scheduled call admitted at dial)
<!-- tags: workflows, temporal, testing | created: 2026-11-02 -->

### mem-1793600670-6fa9
> Empty review.rejected = Reviewer killed by Ralph 300 s inactivity timeout (diagnostics log 'Execution inactivity timeout'); run make test in background + Monitor heartbeat every 2 min
<!-- tags: tooling, testing | created: 2026-11-02 -->

### mem-1793554070-c45d
> failure: ruff check --fix --select RUF022,RUF100,SIM300 on packages/va_db stripped unrelated noqa comments in 16 files; fix=git checkout untouched files; never include RUF100 in a --select --fix
<!-- tags: tooling, lint | created: 2026-11-01 -->

### mem-1793532827-46fa
> va_llm tests: openai SDK resolves platform via asyncify(thread) before a new client's first request; under xdist+cov this outlasts ManualClock deadlines so the hanging first request is cancelled before respx and scripted answers shift (60 s timeout hangs). Fix: va_llm conftest autouse inline_sdk_platform_lookup patches openai._base_client.asyncify inline
<!-- tags: llm, testing, clock | created: 2026-11-01 -->

### mem-1793518609-68fb
> va_telnyx signature review: int(timestamp) on attacker headers can raise OverflowError/ValueError (huge digit strings); cap header length before int conversion so every failure is WebhookVerificationError
<!-- tags: telnyx, security | created: 2026-11-01 -->

### mem-1793333516-86db
> make test flake: apps/api integration tests (test_cross_tenant_routes, test_dev_seed) can exceed the 60 s pytest-timeout at host load avg ~8-11 with coverage on; they pass alone in ~5 s - re-run before rejecting unrelated work, tracked as task fix:api-test-timeout-load
<!-- tags: testing, api, tooling | created: 2026-10-30 -->

### mem-1793332692-ff3b
> flake: apps/api/tests/test_dev_seed.py::test_first_run_creates_org_owner_credit_and_working_keys can hit the 60 s pytest-timeout in make check when host load avg is ~8 (stack ends in coverage tracer, not an I/O wait); it passes in ~6 s alone - re-run before treating it as a regression
<!-- tags: testing, api | created: 2026-10-30 -->

### mem-1793331520-1dbe
> pip-audit on this repo: uv export --no-hashes --no-emit-workspace --format requirements-txt > /var/tmp/req.txt; uvx pip-audit --strict -r /var/tmp/req.txt (without --no-emit-workspace pip-audit fails installing workspace editables)
<!-- tags: tooling, audit | created: 2026-10-30 -->

### mem-1793330327-5c3f
> va_sim tests: two telnyx_client blocks streaming to MediaAgents inside ONE test hang until pytest-timeout (pre-existing, reproduced on old code); split into separate tests
<!-- tags: sim, testing | created: 2026-10-30 -->

### mem-1793328571-1e58
> va_api tests: structlog capture_logs misses cached module loggers in the full xdist suite (test passes alone, fails in make check); monkeypatch the module's _log with a small recording object instead
<!-- tags: api, testing | created: 2026-10-30 -->

### mem-1793321388-bb1d
> pip-audit gate: run 'uv export --no-hashes --no-emit-workspace > /var/tmp/req.txt && uvx --python 3.12 pip-audit --strict -r /var/tmp/req.txt'; without --python 3.12 uvx uses 3.13 and fails on workspace packages (requires-python <3.13)
<!-- tags: tooling, audit | created: 2026-10-30 -->

### mem-1793319405-8587
> Python 3.12 asyncio.gather on already-done tasks completes synchronously (no loop yield): a 'while tasks: await gather(*tasks)' drain loop spins forever before done callbacks run - add await asyncio.sleep(0) per iteration (va_sim.tasks.Supervisor.drain)
<!-- tags: async, testing, sim | created: 2026-10-30 -->

### mem-1793294019-7281
> email_validator.validate_email(check_deliverability=False, test_environment=True) accepts the reserved .test domain but REJECTS special-use .local (and dotless hosts); dev seed owner is dev@voiceagents.test. make recipes echo their command to stdout - prefix with @ when stdout must be machine-readable (make seed ARGS=--json)
<!-- tags: api, tooling | created: 2026-10-29 -->

### mem-1793288415-240d
> pip-audit on uv export fails installing workspace packages; use 'uv export --no-hashes --no-emit-workspace --format requirements-txt' then 'uvx pip-audit --no-deps --disable-pip -r file'
<!-- tags: tooling | created: 2026-10-29 -->

### mem-1793280879-cb08
> Never run 'ruff check --fix --select RUF100' (or any --select incl. RUF100) on the repo: noqa for unselected rules count as unused and get stripped repo-wide (P02-T09a). Run plain 'make lint' / ruff --fix without --select
<!-- tags: tooling, ruff | created: 2026-10-29 -->

### mem-1793279423-b07e
> pydantic: Field(repr=False) inside an Annotated alias used as 'Alias | None' raises UnsupportedFieldAttributeWarning (error under filterwarnings) at app build; put Field(default=None, min_length=..., repr=False) on the attribute instead (P02-T08c AcceptInvitationRequest.password)
<!-- tags: api, pydantic, testing | created: 2026-10-29 -->

### mem-1793273041-6642
> Ralph build.done backpressure: every gate must be 'key: pass' with a colon (tests:, lint:, typecheck:, audit:, coverage:, complexity: <number>, duplication:, specs:); 'tests pass' / 'cov 99%' / 'jscpd 0 clones' converts to build.blocked (P02-T07b)
<!-- tags: tooling, ralph | created: 2026-10-29 -->

### mem-1793272997-f20d
> authlib 1.9: importing authlib.integrations.httpx_client (AsyncOAuth2Client) warns AuthlibDeprecationWarning (wants httpx2) -> error under filterwarnings; use authlib.oauth2 rfc6749/rfc7636 primitives via va_api.auth.oidc_client + OidcDiscovery.post_form, joserfc for JWTs; mypy override authlib.* ignore_missing_imports
<!-- tags: api, auth, tooling | created: 2026-10-29 -->

### mem-1793269824-fe85
> Ralph build.done backpressure needs a NUMERIC complexity value: 'complexity: 3.17' (radon cc -s -a average). 'complexity: A' or 'complexity: pass (...)' converts to build.blocked even in a plain-string payload (P00-FIX-1, P02-T06b)
<!-- tags: tooling, ralph | created: 2026-10-29 -->

### mem-1793267153-9b68
> pip-audit on this uv workspace: uv export --no-hashes --no-emit-workspace --format requirements-txt > logs/req.txt && uvx pip-audit --strict --no-deps --disable-pip -r logs/req.txt (plain export includes -e workspace pkgs and pip install --dry-run fails)
<!-- tags: tooling, audit | created: 2026-10-29 -->

### mem-1793262412-b2f5
> ApiSettings includes CryptoSettings: settings tests with va_env=production must also pass kms_provider=aws + aws_kms_key_id, otherwise 'KMS_PROVIDER=local is refused' fires before the validator under test
<!-- tags: api, settings, testing | created: 2026-10-29 -->

### mem-1793258976-544c
> Ruff noqa for a multi-line triple-quoted string (e.g. S105 on TOKEN_BUCKET_LUA) goes after the closing quotes; text after the opening quotes is part of the string (here, the Lua source)
<!-- tags: tooling, ruff | created: 2026-10-29 -->

### mem-1793257682-c402
> SQLAlchemy 2.1: Row.tuple() and Row._tuple() both raise SADeprecationWarning (error under filterwarnings); unpack the Row directly: a, b, c = row
<!-- tags: database, sqlalchemy | created: 2026-10-29 -->

### mem-1793139282-8c73
> failure: make test during OrbStack Sparkle auto-update -> 120 errors 'failed to connect to the docker API ... orbstack/run/docker.sock'; the updater stops the VM again after 'orb start'. Finish the OrbStack update first (check 'pgrep -lf Autoupdate', 'orb status'), then rerun
<!-- tags: testing, docker | created: 2026-10-27 -->

### mem-1793138363-ca2f
> make test flake (P01-T13 build): 3 of 5 full runs hit pytest-timeout 60s in va_db setup (migrated_url upgrade, scratch_session) + Docker read timeouts; Docker VM is 3.9 GiB running the full compose stack, host load ~8. Isolated rerun green; partition code never failed
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793135968-c35b
> make test flake variant (P01-T12 build): ClickHouseContainer docker create exceeded the 60s pytest-timeout while 8 xdist Postgres containers booted; the va_events integration test has pytest.mark.timeout(180) and is a single test per module (one container per xdist worker)
<!-- tags: testing, docker, clickhouse | created: 2026-10-27 -->

### mem-1793134471-2c7d
> make test flake variant (P01-T11 build): 138 errors = pytest-timeout 60s in conftest pg_url -> pg_container.running_postgres (testcontainers boot) right after compose make migrate + alembic round-trip; isolated rerun 1756 passed. Docker daemon latency, same family as scratch_session timeout
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793133155-6419
> SQLAlchemy AsyncSession.begin_nested() autoflushes pending changes into the OUTER transaction before the savepoint; make add()/setattr inside the begin_nested block or an IntegrityError aborts the caller's tenant transaction. Failed savepoint expires touched objects (attribute access -> MissingGreenlet)
<!-- tags: database, sqlalchemy | created: 2026-10-27 -->

### mem-1793131606-2dff
> va_db scratch_session timeout recurred in P01-T09 build (make test run alone, gw4/gw6): asyncpg TimeoutError at conftest CREATE EXTENSION IF NOT EXISTS citext and at create_all - looks like a lock wait/server stall on pg_url DB, not CPU; rerun green. RLS harness (tenant_rows) uses migrated_url DBs only
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793129328-3035
> make test flake: pytest-timeout 60s in va_db scratch_session setup (2 errors) when host is busy (jscpd/compose concurrently); isolated rerun green. Seen in P01-T06 and P01-T07 reviews - don't run heavy jobs alongside make test; root-cause pending
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793128976-0fb7
> va_db test Postgres runs with fsync=off: DROP DATABASE forces a checkpoint and timed out (asyncpg 30s) with 8 xdist containers; also never boot an extra container in a test - Docker daemon create timed out (60s) under full make test; rename roles aside instead
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793128976-c741
> Alembic+asyncpg: op.execute(str) wraps in text() (':0' in JSON literal = bind param) and asyncpg prepares each statement (no multi-statement strings); va_db migrations use execute_script(sql) from 0001_initial (splits on top-level ';', escapes colons)
<!-- tags: database, alembic | created: 2026-10-27 -->

### mem-1793125348-9962
> va_db tests: 'ruff check --fix --select RUF100' strips '# noqa: F401  (reason)' comments whole from import-for-side-effect lines (conftest, model_introspection) - never run RUF100 autofix on va_db; restore with git checkout
<!-- tags: tooling, ruff | created: 2026-10-27 -->

### mem-1793120870-bd75
> SQLAlchemy text() treats ':0'/':true' inside JSON literals as bind params; JSONB server defaults must go through va_db.models._columns.jsonb_default (escapes colons). Also Mapped[Any] makes a column NOT NULL - use Mapped[Any | None] + nullable=True
<!-- tags: database, sqlalchemy | created: 2026-10-27 -->

### mem-1793118560-6911
> SQLAlchemy 2.1 + asyncpg returns inet columns as ipaddress.IPv4Address/IPv6Address (not str); annotate Mapped[IpAddress | None] from va_db.models
<!-- tags: database, sqlalchemy | created: 2026-10-27 -->

### mem-1793117779-05cf
> va_db assumes va_app/va_system via asyncpg connect_args server_settings={'role': ...} (startup parameter = session default). Never SET ROLE in a pool connect listener: it runs in an implicit txn and a rolled-back first txn (exception or engine.connect() reset-on-return) reverts to the login role => RLS bypass for superuser login
<!-- tags: database, testing | created: 2026-10-27 -->

### mem-1793117599-1425
> Integration tests silently skipped on OrbStack (no DOCKER_HOST, socket at ~/.orbstack/run/docker.sock): docker_probe.docker_available() now uses testcontainers.core.docker_client.get_docker_host() (docker context aware). Use testcontainers.community.<name> imports; legacy testcontainers.redis/postgres raise DeprecationWarning (error under filterwarnings)
<!-- tags: testing, docker | created: 2026-10-27 -->

### mem-1792988928-d413
> Exit-gate clean clones must live under /Users (Docker Desktop cannot bind-mount from /var/tmp: collector.yaml 'not a directory'); compose project name is fixed 'voiceagents' so a clone's make up replaces the main stack - make down/up from main repo after. uv run --no-project --python 3.12 prepends /opt/homebrew/bin to PATH, shadowing nvm node 22 with brew node 26
<!-- tags: tooling, docker, ci | created: 2026-10-26 -->

### mem-1792988571-0143
> Never importlib.reload an app module in tests: it rebinds module globals (app) in place and breaks identity asserts in tests that imported the old object (xdist order-dependent). Use monkeypatch.delitem(sys.modules, mod) + setattr(pkg, name, orig) then import_module for a fresh copy
<!-- tags: testing, pytest | created: 2026-10-26 -->

### mem-1792988312-0c44
> gitleaks full-history scan: plan/phase-20.md:48 'key rotation, Telnyx/ElevenLabs' is a generic-api-key false positive, allowlisted in .gitleaks.toml (path+match AND); the word 'placeholders' in .py trips check_no_placeholders filler-word rule unless written as check_no_placeholders
<!-- tags: tooling, ci | created: 2026-10-26 -->

### mem-1792987695-026b
> pnpm audit in TS workspace: vitest 3.x pulls critical tinypool advisories (use vitest ^4.1.12); next 15.5 pins postcss 8.4.31 (override postcss@<8.5.23 in root package.json pnpm.overrides); Next 15.5 next-env.d.ts references .next/types so git-ignore it
<!-- tags: tooling, typescript, pnpm | created: 2026-10-26 -->

### mem-1792986234-2b28
> uv_build workspace app whose import package differs from the dist name needs [tool.uv.build-backend] module-name = "va_x" in its pyproject, else uv sync fails 'Expected a Python module at src/va_<dist>'
<!-- tags: tooling, uv | created: 2026-10-26 -->

### mem-1792986234-b9d2
> uvicorn Server.serve calls sys.exit() on startup failure; inside an asyncio task SystemExit escapes the loop - catch it in the coroutine (va_common.service._serve -> ServerStartupError). Cancelling a running uvicorn task without shutdown leaks its listening socket (pytest unraisable warning in a later test)
<!-- tags: testing, uvicorn, service | created: 2026-10-26 -->

### mem-1792982690-b4e4
> bash 3.2 (stock macOS /bin/bash) under set -u errors on "${arr[@]}" when arr is empty; guard with ${#arr[@]} checks; test scripts with /bin/bash too
<!-- tags: tooling, bash | created: 2026-10-26 -->

### mem-1792981166-10bf
> aioboto3 session.client(config=...) needs aiobotocore.config.AioConfig; mypy rejects botocore.config.Config
<!-- tags: aws, mypy | created: 2026-10-26 -->

### mem-1792981166-ce01
> pytest-xdist: never parametrize with random values (os.urandom) - workers collect different test IDs and the run errors; parametrize sizes and generate inside the test
<!-- tags: testing, pytest | created: 2026-10-26 -->

### mem-1792920384-602e
> build.done backpressure parser substring-matches plain text tokens 'tests: pass', 'lint: pass', 'typecheck: pass', 'audit: pass', 'coverage: pass', 'duplication: pass', 'complexity: <number>'; emit build.done as a plain string payload, NOT --json (JSON keys like "tests":"pass" never match)
<!-- tags: tooling, ralph | created: 2026-10-25 -->

### mem-1792920274-acc2
> build.done payload must include backpressure keys tests, lint, typecheck, audit, coverage, complexity, duplication (performance/specs optional) or Ralph converts it to build.blocked; obtain audit via uvx pip-audit on uv export + pnpm audit, complexity via uvx radon cc, duplication via jscpd
<!-- tags: tooling, ralph | created: 2026-10-25 -->

### mem-1792920236-93ac
> ruff>=0.17 formats python code blocks in Markdown; root pyproject extend-exclude includes *.md to protect specs/
<!-- tags: tooling, ruff | created: 2026-10-25 -->

## Context

### mem-1793661625-5011
> P05-T17e1: capacity flow opt-in via pytest marker capacity (make e2e = -m 'e2e and not capacity', make e2e-capacity, E2E_CONCURRENT_CALLS 1..50). Baseline: 50 calls overhead p50 335 ms, 667 underruns, lag p99<=150; 10 calls p50 92 ms but still 2 underruns at lag<=5 ms -> underruns not purely CPU (check definition/sim drain in P05-T17e2)
<!-- tags: e2e, performance, media | created: 2026-11-02 -->

### mem-1793660249-9345
> P05-T17e: media worker at 50 calls/process is main-thread bound (InboundPath ~60%: SpectralGate FFT, soxr, GIL re-take after C calls vs va-vad pool threads, asyncio overhead); SpectralGate per-hop np.percentile was 24 ms per call-second -> cadence 32 hops + np.partition = 2 ms; inline VAD (no pool) halves underruns but is not enough; next lever = batched Silero across legs
<!-- tags: media, performance, vad | created: 2026-11-02 -->
