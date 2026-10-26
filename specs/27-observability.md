# 27 — Observability

Package: `va_common.logging`, `va_common.tracing`, `va_common.metrics`. Every service calls
`va_common.observability.init(service_name, settings)` at startup.

## 1. Logging (structlog)

- Processors: `contextvars.merge_contextvars`, `add_log_level`, `TimeStamper(fmt="iso", utc=True)`,
  `StackInfoRenderer`, `format_exc_info`, custom `redact_processor`, custom `otel_trace_processor` (adds `trace_id`,
  `span_id`), `JSONRenderer(serializer=orjson.dumps)` (in `local` env `ConsoleRenderer` when `LOG_FORMAT=console`).
- Standard fields on every line: `ts`, `level`, `event` (message), `service`, `env`, `version`, `pod`, `trace_id`,
  `span_id`, `request_id` (HTTP), `org_id`, `call_id`, `turn_index`, `batch_id`, `conversation_id`, `workflow_id`,
  `activity` (Temporal), `component` (e.g. `stt`, `tts`, `llm`, `engine`, `telnyx`).
- Context binding: FastAPI middleware binds `request_id` (from `X-Request-Id` header or uuid4), `org_id` after auth;
  media worker binds `call_id`, `org_id` for the call task group; engine binds `turn_index` per turn; Temporal
  interceptor binds `workflow_id`, `run_id`, `activity`.
- Redaction processor: masks phone numbers (`+14155550123` → `+1415***0123`) in any string field unless
  `LOG_LEVEL=DEBUG` and `VA_ENV=local`; drops keys `authorization`, `api_key`, `password`, `secret`, `token`,
  `xi-api-key`, `audio`, `payload` (raw media), and drops `text`/`transcript` fields unless `LOG_TRANSCRIPTS=true`
  and env ≠ production; when the org has `hipaa_mode`, transcripts are never logged regardless.
- Levels: `INFO` default; per-component overrides via `LOG_LEVEL_<COMPONENT>` env (e.g. `LOG_LEVEL_TELNYX=DEBUG`).
- Access logs: uvicorn access log disabled; middleware logs one line per request: `method`, `path` (template, not
  raw), `status`, `duration_ms`, `principal_type`, `api_key_prefix`.

> Decision (2026-10-25): `va_common.logging` implements this section. Logging is stdlib-backed structlog
> (`ProcessorFormatter` on one root handler) so uvicorn/httpx/temporalio lines get the same fields and redaction.
> Secret keys are matched by segment, not raw substring: the key is split on `_`, `-`, `.`, whitespace and camelCase
> boundaries (`apiKey`, `APIKey` -> `api`, `key`), then lower-cased; it is secret when any segment is one of `key`,
> `secret`, `token`, `password`, `passwd`, `authorization`, `cookie`, `pepper`, `dsn`, `credential(s)`, or ends
> with `secret`, `password`, `passwd`, `credential(s)` (`dbpassword`), or is `key`/`token` glued to a known
> qualifier (`api`, `access`, `refresh`, `client`, `secret`, `private`, `session`, `auth`, `bearer`, `webhook`,
> `signing`, `id`, `x`, `xi`: `apikey`, `accesstoken`; `monkey`, `hotkey` stay visible), and the last segment is not
> `prefix`, `id`, `ids`, `count`, `version`, `type`, `name`, `length` (so `api_key_prefix`, `keyId`,
> `prompt_tokens`/`promptTokens` stay visible). Secret values are replaced by
> `[REDACTED]` rather than dropped so the key's presence stays visible; raw-media keys (`audio`, `payload`) and
> gated transcript keys (`text`, `transcript`) are dropped; bytes values render as `<N bytes>`. The rules apply
> recursively to nested mappings/lists (containers deeper than 8 levels become `[REDACTED]`) and the pure
> `redact()` function is what Sentry `before_send` reuses (§8). E.164 numbers with 7-10 digits mask as
> `+12***78` (first 2, last 2). Transcripts are allowed iff `LOG_TRANSCRIPTS=true` and `VA_ENV` ≠ production,
> and never on a line with a bound `hipaa_mode=True`. `LOG_LEVEL_<COMPONENT>` names a stdlib/structlog logger by
> the lower-cased suffix (`LOG_LEVEL_VA_MEDIA` -> logger `va_media` and its children). `version` is passed by the
> service (default `unknown`); `pod` is `POD_ID` or the hostname. In console format `format_exc_info` is omitted
> (the console renderer formats exceptions itself).

## 2. Tracing (OpenTelemetry)

- SDK: `TracerProvider` with `Resource(service.name, service.version, deployment.environment, k8s.pod.name)`,
  `BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT))`, sampler
  `ParentBased(TraceIdRatioBased(OTEL_TRACES_SAMPLER_ARG default 0.1))` for HTTP; **calls are always sampled**
  (call root span created with `sampled` forced via a custom sampler attribute `va.force_sample=true`).
- Auto-instrumentation: FastAPI, httpx, asyncpg, redis. Temporal: `temporalio.contrib.opentelemetry.TracingInterceptor`.
- Propagation: W3C `traceparent`. API → Temporal workflow headers → activities. The call trace id is stored in
  `call:{id}:ctx.trace_context` so the media worker continues the same trace.

> Decision (2026-10-25): `va_common.tracing.setup_tracing(service_name, settings, version=...)` builds the provider.
> Force-sampling is a root sampler (`ForceSampleSampler`) that returns RECORD_AND_SAMPLE when the span's start
> attributes contain `va.force_sample=True` (build them with `force_sampled_attributes(...)`) and otherwise delegates
> to `TraceIdRatioBased(OTEL_TRACES_SAMPLER_ARG)`; it sits under `ParentBased`, so children follow the parent. The
> exporter is OTLP over gRPC (collector port 4317), plaintext for `http://` endpoints, 10 s export timeout.
> `service.version` is passed by the service; `k8s.pod.name` is `POD_ID` or the host name (the same rule as the log
> `pod` field, `va_common.tracing.resolve_pod`). httpx and redis clients are instrumented per client by the
> `va_common.http.http_client` and `va_common.redis.redis_client` factories. `setup_tracing` installs the global
> tracer provider and the W3C `traceparent` + baggage propagators unless `set_global=False` (tests).

### 2.1 Span catalog

| Span name | Service | Attributes |
|---|---|---|
| `HTTP <METHOD> <route>` | api, edge | standard http.*, `va.org_id`, `va.principal_type` |
| `call` (root, one per call) | api (start) / continued everywhere | `va.call_id`, `va.org_id`, `va.direction`, `va.runner_type` |
| `call.prefetch` | worker | `va.dynamic_data.count` |
| `call.dial` | worker | `telnyx.call_control_id`, `telnyx.status_code` |
| `telnyx.webhook <event_type>` | edge | `telnyx.event_type`, `telnyx.event_id` |
| `media.session` | media | `va.pod_id`, `va.hd_audio` |
| `media.attach` | media | time from WS open to pipeline ready |
| `turn` (per turn, child of `media.session`) | media | `va.turn_index`, `va.node_id`, `va.interrupted`, `va.e2e_ms` |
| `turn.eot` | media | `va.eot_ms`, `va.silence_threshold_ms` |
| `turn.stt_final` | media | `va.stt_final_ms`, `va.chars` |
| `turn.guard_rails` | media | `va.guard_rails.count`, `va.triggered` |
| `turn.global_check` | media | `va.global.matched` |
| `turn.extraction` | media | `va.variables.count` |
| `turn.loop_condition` | media | `va.score` |
| `turn.route` | media | `va.route.to`, `va.route.alternatives` |
| `turn.kb_retrieve` | media | `va.kb_id`, `va.top_k`, `va.hits` |
| `turn.tool <name>` | media | `http.status_code`, `va.tool.duration_ms` |
| `llm.chat` | media, worker | `gen_ai.system="telnyx"`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `va.llm.ttft_ms`, `va.llm.purpose` (dialogue/router/extraction/analysis) |
| `tts.segment` | media | `va.tts.chars`, `va.tts.ttfa_ms`, `va.tts.context_id` |
| `tts.playback` | media | `va.played_ms` |
| `stt.session` | media | `va.stt.reconnects` |
| `call.transfer` | media | `va.transfer.type` |
| `postcall.<step>` (recording, retranscribe, summary, disposition, analysis, citations, evals, memory, billing, webhook) | worker | step-specific |
| `webhook.deliver` | dispatcher | `http.url` (host only), `http.status_code`, `va.attempt` |
| `kb.ingest.<stage>` | worker | `va.document_id` |
| `temporal.workflow <name>` / `temporal.activity <name>` | worker | interceptor-provided |

## 3. Metrics (Prometheus)

Each service exposes `/metrics` (api 8000, edge 8100, media 8200 on the same port; worker 9300, dispatcher 9400,
events-sink 9500 dedicated ports). Library: `prometheus-client`, multiprocess mode not used (single process per pod).
Histogram default latency buckets (ms): `[5,10,25,50,75,100,150,200,300,400,500,650,800,1000,1300,1600,2000,3000,5000,10000]`
named `LAT_MS`.

> Decision (2026-10-25): `va_common.metrics.CATALOG` holds every row of §3.1-§3.5 keyed by `Service`;
> `packages/va_common/tests/test_metrics.py` parses these tables and asserts exact parity. Combined rows (`a` / `b`)
> are two metrics; a "(X only)" label applies only to the metric whose name contains X. §3.5 is split by prefix:
> `va_webhook_*` belongs to webhook-dispatcher, `va_events_sink_*` to events-sink. Every histogram uses `LAT_MS`.
> Each process builds its own `CollectorRegistry` with `build_metrics(service)` (catalog plus the process, platform,
> and GC collectors) and never uses the prometheus_client global registry; `/metrics` is served by
> `metrics_app(registry)` (ASGI) or `render_latest(registry)`.

### 3.1 api

| Metric | Type | Labels |
|---|---|---|
| `va_http_requests_total` | counter | `route`, `method`, `status` |
| `va_http_request_duration_ms` | histogram (LAT_MS) | `route`, `method` |
| `va_calls_created_total` | counter | `direction`, `runner_type`, `env` |
| `va_calls_rejected_total` | counter | `code` (error code) |
| `va_rate_limited_total` | counter | `bucket`, `plan` |
| `va_idempotent_replays_total` | counter | — |
| `va_sse_connections` | gauge | `stream` |
| `va_mcp_tool_calls_total` | counter | `tool`, `status` |

### 3.2 telephony-edge

| Metric | Type | Labels |
|---|---|---|
| `va_telnyx_webhooks_total` | counter | `event_type`, `result` (processed/duplicate/invalid_signature/error) |
| `va_telnyx_webhook_processing_ms` | histogram | `event_type` |
| `va_inbound_calls_total` | counter | `result` (answered/rejected/fallback) |
| `va_amd_results_total` | counter | `result` |
| `va_sms_inbound_total` | counter | `result` |

### 3.3 media-worker

| Metric | Type | Labels |
|---|---|---|
| `va_media_active_calls` | gauge | `transport` (telnyx/widget) — **HPA metric** |
| `va_media_calls_started_total` / `va_media_calls_ended_total` | counter | `end_reason` (ended only) |
| `va_media_admission_rejected_total` | counter | `reason` (capacity/draining) |
| `va_turn_e2e_ms` | histogram | `org_tier` (plan id) |
| `va_turn_eot_ms` | histogram | — |
| `va_stt_final_ms` | histogram | — |
| `va_llm_ttft_ms` | histogram | `model`, `purpose` |
| `va_llm_tokens_total` | counter | `model`, `direction` (in/out) |
| `va_llm_errors_total` | counter | `model`, `kind` (timeout/http/parse) |
| `va_tts_ttfa_ms` | histogram | `model` |
| `va_tts_characters_total` | counter | `model` |
| `va_tts_cache_hits_total` / `va_tts_cache_misses_total` | counter | — |
| `va_vendor_ws_reconnects_total` | counter | `vendor` (elevenlabs_stt/elevenlabs_tts) |
| `va_vendor_errors_total` | counter | `vendor`, `code` |
| `va_barge_in_total` | counter | `honored` (true/false) |
| `va_barge_in_reaction_ms` | histogram | — |
| `va_audio_underruns_total` | counter | — |
| `va_audio_reorder_total` | counter | — |
| `va_tool_calls_total` | counter | `status_class` (2xx/4xx/5xx/timeout) |
| `va_tool_duration_ms` | histogram | — |
| `va_route_decisions_total` | counter | `kind` (edge/global/loop_stay) |
| `va_guard_rail_triggers_total` | counter | `guard_rail`, `action` |
| `va_event_loop_lag_ms` | histogram | — (measured by a 100 ms ticker) |
| `va_media_draining` | gauge | — (0/1) |

### 3.4 worker

| Metric | Type | Labels |
|---|---|---|
| `va_workflow_started_total` / `va_workflow_failed_total` | counter | `workflow` |
| `va_activity_duration_ms` | histogram | `activity` |
| `va_activity_failures_total` | counter | `activity`, `error_type` |
| `va_dial_total` | counter | `result` (accepted/carrier_error) |
| `va_postcall_duration_ms` | histogram | `step` |
| `va_batch_dispatch_total` | counter | `result` (dispatched/skipped_dnc/skipped_window/skipped_invalid/capped) |
| `va_kb_documents_processed_total` | counter | `status` |
| `va_billing_reconcile_drift_micros` | gauge | — |
| `va_credit_low_balance_orgs` | gauge | — |

### 3.5 webhook-dispatcher and events-sink

| Metric | Type | Labels |
|---|---|---|
| `va_webhook_deliveries_total` | counter | `result` (succeeded/failed/dead), `format` |
| `va_webhook_delivery_ms` | histogram | — |
| `va_webhook_backlog` | gauge | — (pending due now) |
| `va_webhook_oldest_pending_seconds` | gauge | — |
| `va_events_sink_inserted_total` | counter | `table` |
| `va_events_sink_lag_seconds` | gauge | `stream` (age of oldest unacked entry) |
| `va_events_sink_errors_total` | counter | `table` |

## 4. Per-call latency events

The media worker emits for each completed turn (see `19` §2 `call.latency`, `07-media-pipeline.md` latency tracer):
one summary event `"E2E: {e2e}ms"` with data `{eot_ms, stt_final_ms, llm_ttft_ms, tts_ttfa_ms, e2e_ms, route_ms,
tool_ms, turn_index}` and stage events `"STT: {n}ms"`, `"LLM: {n}ms"`, `"TTS: {n}ms"`. The same values are written
to `call_turns.latency` and ClickHouse `va.call_turns`, and observed into the histograms in §3.3. Definitions:
- `eot_ms`: last voiced frame end → EOT decision.
- `stt_final_ms`: EOT decision → committed transcript received.
- `llm_ttft_ms`: LLM request sent → first content token.
- `tts_ttfa_ms`: first text sent to TTS → first audio chunk received.
- `e2e_ms`: last voiced frame end → first agent audio frame written to the Telnyx WebSocket.
- `route_ms`: router decision duration (may overlap with speculative generation).
- `tool_ms`: total tool/webhook time within the turn.

## 5. SLOs

| SLO | Target | SLI |
|---|---|---|
| API availability | 99.9 % monthly | non-5xx / total for `/v1/*` |
| Call setup success | 99.5 % | calls reaching `in_progress` / calls answered by carrier |
| Voice latency | 95 % of turns `e2e_ms` ≤ 1 300 ms | `va_turn_e2e_ms` |
| Webhook timeliness | 99 % first attempt within 5 s of event | `va_webhook_oldest_pending_seconds`, delivery timestamps |
| Post-call completion | 99 % `call.completed` within 15 min of hangup | workflow timing |

Error budgets reviewed weekly; burn-rate alerts below.

## 6. Alert rules (`infra/grafana/alerts.yaml`, Prometheus rule format)

| Alert | Expression | For | Severity |
|---|---|---|---|
| `ApiHighErrorRate` | `sum(rate(va_http_requests_total{status=~"5.."}[5m])) / sum(rate(va_http_requests_total[5m])) > 0.02` | 5m | page |
| `ApiLatencyHigh` | `histogram_quantile(0.95, sum by (le) (rate(va_http_request_duration_ms_bucket[5m]))) > 500` | 10m | ticket |
| `VoiceLatencyP95High` | `histogram_quantile(0.95, sum by (le) (rate(va_turn_e2e_ms_bucket[10m]))) > 1300` | 10m | page |
| `VoiceLatencyP50High` | `histogram_quantile(0.50, sum by (le) (rate(va_turn_e2e_ms_bucket[10m]))) > 900` | 15m | ticket |
| `LlmTtftHigh` | `histogram_quantile(0.95, sum by (le, model) (rate(va_llm_ttft_ms_bucket{purpose="dialogue"}[10m]))) > 700` | 10m | ticket |
| `TtsTtfaHigh` | `histogram_quantile(0.95, sum by (le) (rate(va_tts_ttfa_ms_bucket[10m]))) > 500` | 10m | ticket |
| `VendorErrorsSpike` | `sum by (vendor) (rate(va_vendor_errors_total[5m])) > 0.5` | 5m | page |
| `LlmErrorsSpike` | `sum(rate(va_llm_errors_total[5m])) > 1` | 5m | page |
| `TelnyxSignatureFailures` | `sum(rate(va_telnyx_webhooks_total{result="invalid_signature"}[5m])) > 0.2` | 10m | ticket |
| `MediaCapacityHigh` | `sum(va_media_active_calls) / (count(va_media_active_calls) * 60) > 0.8` | 5m | ticket |
| `AdmissionRejections` | `sum(rate(va_media_admission_rejected_total{reason="capacity"}[5m])) > 0` | 5m | page |
| `EventLoopLag` | `histogram_quantile(0.99, sum by (le, pod) (rate(va_event_loop_lag_ms_bucket[5m]))) > 50` | 5m | ticket |
| `AudioUnderruns` | `sum(rate(va_audio_underruns_total[5m])) > 1` | 10m | ticket |
| `WebhookBacklog` | `va_webhook_oldest_pending_seconds > 120` | 5m | page |
| `EventsSinkLag` | `max(va_events_sink_lag_seconds) > 300` | 10m | ticket |
| `WorkflowFailures` | `sum by (workflow) (increase(va_workflow_failed_total[15m])) > 5` | 0m | ticket |
| `BillingDrift` | `abs(va_billing_reconcile_drift_micros) > 10000000` | 0m | page |
| `PostgresDown` / `RedisDown` / `TemporalDown` | `up{job=~"postgres-exporter|redis-exporter|temporal"} == 0` | 2m | page |
| `CertExpiry` | `probe_ssl_earliest_cert_expiry - time() < 14*86400` | 1h | ticket |

## 7. Analytics API (`/v1/analytics/*`, envelope, scope `analytics:read`)

Implemented in `va_api.routers.analytics` with `clickhouse-connect` async client; queries parameterized
(`{org_id:UUID}`, `{from:DateTime64(3)}`, `{to:DateTime64(3)}`), max range 400 days, results cached in Redis 60 s.
Common filters (optional, appended as `AND` clauses with parameters): `pathway_id`, `agent_id`, `direction`,
`batch_id`. Granularity `interval` ∈ `hour|day|week` → `toStartOfHour|toStartOfDay|toMonday`.

| Endpoint | SQL |
|---|---|
| `GET /v1/analytics/overview` | `SELECT count() AS calls, countIf(status='completed' AND answered_by='human') AS answered, sum(connected_seconds)/60 AS minutes, avgIf(connected_seconds, connected_seconds>0) AS avg_duration_s, countIf(transfer_seconds>0) AS transferred, sum(price_micros) AS spend_micros FROM va.calls_fact FINAL WHERE org_id={org_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)}` plus `SELECT quantile(0.5)(e2e_ms) AS p50, quantile(0.95)(e2e_ms) AS p95 FROM va.call_turns WHERE org_id={org_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} AND e2e_ms IS NOT NULL` |
| `GET /v1/analytics/volume` | `SELECT toStartOfDay(created_at) AS bucket, status, count() AS calls, sum(connected_seconds)/60 AS minutes FROM va.calls_fact FINAL WHERE org_id={org_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} GROUP BY bucket, status ORDER BY bucket` (bucket function per `interval`) |
| `GET /v1/analytics/latency` | `SELECT toStartOfHour(created_at) AS bucket, quantile(0.5)(e2e_ms) p50, quantile(0.95)(e2e_ms) p95, avg(eot_ms) eot, avg(stt_final_ms) stt, avg(llm_ttft_ms) llm, avg(tts_ttfa_ms) tts, count() turns FROM va.call_turns WHERE org_id={org_id:UUID} AND role='assistant' AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} GROUP BY bucket ORDER BY bucket` |
| `GET /v1/analytics/dispositions` | `SELECT disposition_tag, count() AS calls FROM va.calls_fact FINAL WHERE org_id={org_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} AND disposition_tag != '' GROUP BY disposition_tag ORDER BY calls DESC` |
| `GET /v1/analytics/end_reasons` | `SELECT end_reason, count() AS calls FROM va.calls_fact FINAL WHERE org_id={org_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} GROUP BY end_reason ORDER BY calls DESC` |
| `GET /v1/analytics/answered_by` | `SELECT answered_by, count() FROM va.calls_fact FINAL WHERE org_id={org_id:UUID} AND direction='outbound' AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} GROUP BY answered_by` |
| `GET /v1/analytics/interruptions` | `SELECT toStartOfDay(created_at) AS bucket, countIf(interrupted=1)/uniqExact(call_id) AS interruptions_per_call FROM va.call_turns WHERE org_id={org_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} GROUP BY bucket ORDER BY bucket` |
| `GET /v1/analytics/pathway_funnel?pathway_id=` (required) | `SELECT node_id, uniqExact(call_id) AS calls_reached, countIf(role='assistant') AS visits FROM va.call_turns WHERE org_id={org_id:UUID} AND call_id IN (SELECT call_id FROM va.calls_fact FINAL WHERE org_id={org_id:UUID} AND pathway_id={pathway_id:UUID} AND created_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)}) GROUP BY node_id ORDER BY calls_reached DESC` |
| `GET /v1/analytics/cost` | `SELECT toStartOfDay(occurred_at) AS bucket, meter, sum(amount_micros) AS amount_micros, sum(quantity) AS quantity FROM va.usage_events_raw WHERE org_id IN {org_ids:Array(UUID)} AND occurred_at BETWEEN {from:DateTime64(3)} AND {to:DateTime64(3)} GROUP BY bucket, meter ORDER BY bucket` (`org_ids` = current org plus children when `include_sub_accounts=true`) |
| `GET /v1/analytics/usage` | same as cost grouped by `meter` only |
| `GET /v1/analytics/evals` | Postgres query (eval scores live in Postgres): `SELECT date_trunc('day', created_at) AS bucket, avg(overall_score), avg(passed::int) FROM eval_runs WHERE org_id=:org_id AND status='completed' AND created_at BETWEEN :from AND :to GROUP BY 1 ORDER BY 1` |

Responses: `{data: {series: [...rows as objects...], totals?: {...}}, errors: null}`; micros converted to dollars.

## 8. Sentry

`sentry-sdk` initialised when `SENTRY_DSN` set: integrations FastAPI, asyncio, httpx; `traces_sample_rate=0`
(tracing via OTel), `send_default_pii=False`, `before_send` applies the same redaction as logs, environment and release
tags, `org_id`/`call_id` set as tags (never user email). Dashboard uses `@sentry/nextjs` with the same rules
(`NEXT_PUBLIC_SENTRY_DSN`).

> Decision (2026-10-25): `va_common.observability.init(service_name, settings, version=..., service=...)` configures
> logging, tracing, metrics (when `service` is given), and Sentry, and returns an `Observability` whose `shutdown()`
> flushes spans and Sentry. Sentry is initialised only when `SENTRY_DSN` is non-empty, with the asyncio and httpx
> integrations (the FastAPI integration enables itself when FastAPI is installed). `before_send` applies
> `va_common.logging.redact` to `extra`, `contexts`, `request`, `logentry` (`message`, `formatted`, `params`),
> breadcrumbs, and the local `vars` of every frame in `exception.values[].stacktrace` and
> `threads.values[].stacktrace`; masks phone numbers in `message` and each `exception.values[].value`; leaves
> structural fields (exception `type`/`module`, frame `filename`/`function`/`lineno`) intact; removes `user.email`
> and `user.ip_address`; and copies the structlog-bound `org_id`/`call_id` into tags.

## 9. Health and readiness

| Service | `GET /healthz` (liveness) | `GET /readyz` (readiness) |
|---|---|---|
| api | process up | Postgres `SELECT 1`, Redis `PING`, Temporal client connected (all within 1 s) |
| telephony-edge | process up | Postgres, Redis, Temporal |
| media-worker | event loop lag < 500 ms | not draining, active calls < `MEDIA_MAX_CALLS_PER_POD`, Redis reachable, VAD model loaded |
| worker | process up and Temporal worker polling | Postgres, Redis, Temporal |
| webhook-dispatcher | process up | Postgres |
| events-sink | process up | Redis, ClickHouse |
| dashboard | `/api/healthz` Next route handler | same |

Responses: `{"status":"ok","checks":{"postgres":"ok",...},"version":"…"}`; failures return 503 with failing checks.
`/v1/health` on the api is public and returns `{"status":"ok"}` only.

> Decision (2026-10-25): readiness bodies are `{"status":"ok","checks":{name:"ok"},"version":v}` (200) or
> `{"status":"unavailable","checks":{name:"ok"|"error"|"timeout"},"failing":[sorted names],"version":v}` (503).
> Each check runs concurrently with a 1 s budget; failure details (exception type) are logged as
> `health.check_failed`, never returned. `/healthz` returns `{"status":"ok","version":v}`. The shared implementation
> is `va_common.health.run_checks` (every service uses it). The request id bound by the middleware (§1) is the
> echoed `X-Request-Id` or `req_<uuidv7 hex>` (`31-error-catalog.md` §4 Decision), not uuid4. The access line event is
> `http.request` with `method`, `path` (route template, `unmatched` for 404s), `status`, `duration_ms`.

## 10. Grafana dashboards (`infra/grafana/dashboards/*.json`)

| Dashboard | Panels |
|---|---|
| Platform Overview | API RPS, API error rate, API p95, active calls (sum), calls started/min, call end reasons (stacked), webhook backlog, events sink lag, credit low-balance orgs |
| Voice Latency | E2E p50/p95/p99, EOT p50/p95, STT final p95, LLM TTFT p50/p95 by model, TTS TTFA p50/p95, barge-in reaction p95, turns/min, latency heatmap (`va_turn_e2e_ms_bucket`) |
| Media Workers | active calls per pod, admission rejections, event loop lag p99 per pod, audio underruns, reorders, vendor WS reconnects, draining pods, CPU/memory per pod |
| Vendors | ElevenLabs STT/TTS errors by code, TTS characters/min, TTS cache hit ratio, LLM tokens in/out by model, LLM errors by kind, Telnyx webhooks by type/result, dial results |
| Workflows | workflow starts/failures by type, activity duration p95 by activity, post-call step durations, batch dispatch results, KB processing |
| Webhooks | deliveries by result, delivery latency p95, backlog, oldest pending, dead count |
| Infrastructure | Postgres connections/TPS/replication lag (postgres-exporter), Redis memory/ops (redis-exporter), ClickHouse inserts, Temporal persistence latency, node resources |

Dashboards are provisioned by Helm (`grafana_dashboard` ConfigMaps labeled `grafana_dashboard: "1"`).

## 11. Tests required

- Redaction processor (phones, keys, transcripts, hipaa), trace context propagation API → workflow → media (span
  parent ids asserted with `InMemorySpanExporter`), metrics registration (every metric in §3 exists with labels; test
  parses the tables in this file), latency event emission math using fake clocks, analytics endpoints against
  testcontainers ClickHouse seeded with fixtures (exact expected numbers), health/readiness behaviours, alert rule
  file validated with `promtool check rules` in CI.

## 12. Acceptance criteria

- Every service exposes `/metrics`, `/healthz`, `/readyz`; all metrics in §3 present.
- A simulated call produces one trace containing `call`, `media.session`, ≥ 1 `turn` with stage children, `llm.chat`,
  `tts.segment`, and `postcall.*` spans.
- Analytics endpoints return correct aggregates on fixtures; dashboards and alert rules load without errors.
