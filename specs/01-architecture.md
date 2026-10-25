# 01 — System Architecture

## 1. Planes and services

```
                          ┌──────────────────────────── Customers ────────────────────────────┐
                          │  REST clients / SDKs / CLI / MCP     Dashboard (Next.js)   Widget │
                          └───────────────┬──────────────────────────────┬──────────────┬─────┘
                                          │ HTTPS                        │ HTTPS        │ WSS
                                   ┌──────▼───────┐               ┌──────▼──────┐  ┌────▼────────────┐
                                   │  api         │◄──────────────│ dashboard   │  │ media-worker    │
                                   │  (FastAPI)   │  BFF calls    │ (Next.js)   │  │ /ws/widget      │
                                   └──┬───┬───┬───┘               └─────────────┘  └────┬────────────┘
             Temporal client (start)  │   │   │ Redis pub/sub (control)                  │
                    ┌─────────────────┘   │   └───────────────────────┐                  │
             ┌──────▼──────┐        ┌─────▼─────┐             ┌───────▼────────┐         │
             │ worker      │        │ Postgres  │             │ Redis          │◄────────┤
             │ (Temporal   │◄──────►│ +pgvector │             │ streams/pubsub │         │
             │ workflows + │        └───────────┘             │ rate limits    │         │
             │ activities) │                                  └───┬────────▲───┘         │
             └──┬───────┬──┘                                      │        │             │
     Telnyx REST│       │ElevenLabs REST                 ┌────────▼──┐  ┌──┴───────────┐  │
                │       │ Telnyx Inference               │ webhook-  │  │ events-sink  │  │
                │       │                                │ dispatcher│  │ (→ClickHouse)│  │
                │       │                                └───────────┘  └──────────────┘  │
      ┌─────────▼───────▼──────┐   Telnyx webhooks (HTTPS)  ┌──────────────────┐          │
      │        Telnyx          │──────────────────────────► │ telephony-edge   │          │
      │  Call Control / SMS    │                            │ (FastAPI)        │──Redis──►│
      │  Media Streaming WS    │──────── WSS media ────────────────────────────────────────┤
      └────────────────────────┘                                                        │
                                                         ElevenLabs STT/TTS WSS ◄────────┤
                                                         Telnyx Inference (HTTPS SSE) ◄──┘
```

| Service | Path in repo | Process | Port (dev) | Responsibility |
|---|---|---|---|---|
| `api` | `apps/api` | `uvicorn va_api.main:app` | 8000 | Public REST API (`/v1`, `/v2`), dashboard session auth, OpenAPI, MCP endpoint `/v1/mcp`, widget config endpoints, live event SSE/WebSocket for dashboard, Stripe webhooks. Stateless. |
| `telephony-edge` | `apps/telephony_edge` | `uvicorn va_edge.main:app` | 8100 | Receives Telnyx voice & messaging webhooks, verifies Ed25519 signatures, persists raw events, maps them to domain events, routes inbound calls, publishes to Redis channels. Stateless. |
| `media-worker` | `apps/media_worker` | `python -m va_media.main` (uvicorn ASGI with websockets) | 8200 | Accepts Telnyx media WebSockets (`/ws/telnyx`) and widget audio WebSockets (`/ws/widget`), claims call ownership, runs the real-time pipeline (STT ⇄ conversation engine ⇄ TTS), executes in-call tools, emits events. Stateful per call; drains gracefully. |
| `worker` | `apps/worker` | `python -m va_worker.main` | — (metrics 9300) | Temporal worker hosting every workflow and its activities: `CallWorkflow`, `PostCallWorkflow`, `BatchWorkflow`, `EvalRunWorkflow`, `PathwayTestWorkflow`, `KnowledgeIngestWorkflow`, `VoiceCloneWorkflow`, `VoiceCatalogSyncWorkflow`, `SmsConversationWorkflow`, `ConversationTimeoutSweepWorkflow`, `TenDlcRegistrationWorkflow`, `NumberOrderWorkflow`, `NumberReleaseWorkflow`, `MonthlyNumberBillingWorkflow`, `TelnyxAccountBootstrapWorkflow`, `AutoRechargeWorkflow`, `LowBalanceNotifyWorkflow`, `SubscriptionRenewalWorkflow`, `MonthlyInvoiceWorkflow`, `BillingReconcileWorkflow`, `RetentionWorkflow`, `ErasureWorkflow`, `DncImportWorkflow`, `ReencryptWorkflow`. Task queues: `va-main` (calls, batches), `va-postcall` (post-call, evals, pathway tests), `va-ingest` (KB, voices, DNC imports), `va-system` (billing, retention, erasure, re-encryption, catalog sync). |
| `webhook-dispatcher` | `apps/webhook_dispatcher` | `python -m va_webhooks.main` | — (metrics 9400) | Reads `webhook_deliveries` outbox, delivers signed HTTP POSTs with retry/backoff, DLQ, replay. |
| `events-sink` | `apps/events_sink` | `python -m va_events_sink.main` | — (metrics 9500) | Consumes Redis Streams (`events:*`) in a consumer group, batch-inserts into ClickHouse tables `call_events`, `call_turns`, `usage_events_raw`. |
| `dashboard` | `apps/dashboard` | `next start` | 3000 | Web UI (see `25-dashboard.md`). Talks only to `api`. |
| `widget` | `apps/widget` | static bundle built by Vite | served by `api` at `/widget/v1/widget.js` | Embeddable chat + voice widget. |
| `docs` | `apps/docs` | MkDocs Material static site | 3100 | Public documentation + API reference + `llms.txt`. |
| `sim` | `apps/sim` | `python -m va_sim.server` | 9100–9400 | Local/dev/test simulators: fake Telnyx (REST + media WS client + webhook emitter) on 9100, fake ElevenLabs (TTS WS, STT WS, REST) on 9200, fake Telnyx Inference (OpenAI-compatible) on 9300, fake Stripe on 9400. |

Infrastructure dependencies: PostgreSQL 16 with `pgvector` ≥ 0.7 and `pg_trgm`; Redis 7.2; ClickHouse 24.x;
Temporal Server 1.24+ (dev: `temporalio/auto-setup`); S3-compatible object storage (dev: MinIO); Deno 2.x
binary available to the media worker and worker (custom code nodes, `13-tools-and-dynamic-data.md` §8).

## 2. Call lifecycles

### 2.1 Outbound call (`POST /v1/calls`)

1. `api` authenticates the key, validates the body (`05-calls-api.md`), resolves the runner (agent/persona/pathway/task),
   applies precedence rules, checks compliance (DNC, calling window when `start_time` absent is not enforced unless
   `compliance.enforce_calling_hours` is set on the org, 10-second same-number rule), checks limits (daily, hourly,
   concurrency, international), reserves a credit hold (`24-billing-and-limits.md` §5), inserts a `calls` row with
   `status='queued'`, writes `call.queued` event, and starts Temporal workflow `CallWorkflow(call_id)` with
   workflow id `call-{call_id}`. If `start_time` is set, the workflow sleeps until then (durable timer) and the
   row has `status='scheduled'`. Response: `{"status":"success","call_id":"…","batch_id":null,"message":"Call successfully queued."}`.
2. `CallWorkflow` activity `prefetch_call_context`: runs pre-call `dynamic_data` fetches with `cache: true`
   entries, loads memory context for the contact, resolves the voice, pre-computes the start-node prompt, and
   stores a `call_context` JSON in Redis key `call:{id}:ctx` (TTL = max_duration + 1 h).
3. Activity `dial` calls Telnyx `POST /v2/calls` with `connection_id` = org's Call Control App connection,
   `to`, `from` (resolved via dialing strategy), `stream_url = wss://{MEDIA_PUBLIC_HOST}/ws/telnyx?call_id={id}&token={stream_token}`,
   `stream_track="inbound_track"`, `stream_bidirectional_mode="rtp"`, `stream_bidirectional_codec="PCMU"`
   (or `L16` + `stream_bidirectional_sampling_rate=16000` when `hd_audio`), `stream_codec="PCMU"`, `stream_auth_token`,
   `answering_machine_detection` per voicemail settings, `record`/`record_channels="dual"`/`record_format="wav"`
   when `record=true`, `time_limit_secs = max_duration*60 + 30`, `timeout_secs` from org setting (default 45),
   `client_state = base64(json{"call_id","leg":"primary","org_id"})`, `command_id = "dial-{call_id}"`,
   `webhook_url = https://{EDGE_PUBLIC_HOST}/telnyx/voice`. Stores `call_control_id`, `call_leg_id`,
   `call_session_id` on `call_legs`.
4. `telephony-edge` receives `call.initiated` → `call.answered` (or `call.hangup` with `no_answer`/`busy`/…).
   Each Telnyx webhook is verified, stored in `telnyx_events`, mapped to a domain event, published to Redis
   channel `call:{id}:carrier` and Redis Stream `events:calls`, and signalled to the workflow
   (`CallWorkflow.signal_carrier_event`).
5. Telnyx opens the media WebSocket to a `media-worker` pod (behind the load balancer). The pod verifies the
   `telnyx-signature-ed25519` header and the `token` query param, acquires the Redis ownership lock
   `call:{id}:owner = {pod_id}` (SET NX PX 15000, renewed every 5 s), subscribes to `call:{id}:control` and
   `call:{id}:carrier`, loads `call:{id}:ctx`, opens ElevenLabs STT and TTS sockets, and starts the pipeline.
   If `wait_for_greeting=false`, the agent speaks `first_sentence` (pre-rendered during step 2 when possible)
   as soon as `call.answered` arrives and AMD (if enabled) reports human or times out per voicemail policy.
6. During the call the conversation engine (`11-pathways-engine.md`) runs turns; every turn persists to
   `call_turns` (Postgres, write-behind batched every 2 s) and emits the per-turn events listed in `19-events-and-webhooks.md` §2 (`call.turn`, `call.latency`, `call.route`,
   `call.extraction`, `call.loop_condition`, `call.tool`, `call.interrupt`, `call.guard_rail`).
7. Call ends on: agent End Call node/tool, caller hangup (`call.hangup`), `max_duration` timer, API stop
   (`POST /v1/calls/{id}/stop` → publish `{"type":"stop"}` on `call:{id}:control`), guard rail `end_call`,
   voicemail action `hangup`, or fatal pipeline error (agent says the configured error line then hangs up).
8. On end, the worker sends Telnyx `hangup` if the leg is still up, flushes turns, writes `calls.ended_at`,
   `call_length`, `end_reason`, releases the ownership lock, and signals `CallWorkflow.signal_pipeline_finished`.
9. `CallWorkflow` starts child workflow `PostCallWorkflow(call_id)` (`18-post-call-analysis.md`): waits for
   `call.recording.saved` (timeout 10 min), copies recording to S3, optional diarized re-transcription, summary,
   disposition, analysis schema extraction, citations, evals, memory update, billing finalization, final
   `call.completed` webhook.

### 2.2 Inbound call

1. Telnyx sends `call.initiated` with `direction="incoming"` to `telephony-edge`.
2. Edge looks up `phone_numbers` by `to` (E.164). Unknown/unassigned number → `reject` with cause `CALL_REJECTED`.
   Org suspended or out of credits → if `fallback_number` set, `transfer` to it; else `reject`.
   Maintenance window active (`inbound_config.maintenance`) → transfer to `fallback_number`.
3. Edge creates the `calls` row (`direction='inbound'`, runner from `inbound_config`), stores `call_context`
   (same as outbound prefetch, run inline with a 1.5 s budget; slower dynamic data continues asynchronously and
   is merged when ready), starts `CallWorkflow` in inbound mode, and issues Telnyx `answer` with the same stream
   parameters as outbound dial plus `client_state`.
4. Remaining steps are identical to outbound from step 5.

### 2.3 Transfers

- **Cold transfer:** Telnyx `transfer` action on the primary leg with `to = transfer target`, `from` = caller-facing
  number per org setting (`transfer_caller_id_mode`: `agent_number` | `original_caller`), `target_leg_client_state`
  identifying the new leg. The AI pipeline stops after the final line finishes playing (`mark` echoed). Billing:
  connected minutes until bridge, transfer minutes after bridge (`calls.transfer_seconds`).
- **Warm transfer:** worker dials a second leg (`POST /v2/calls` with `link_to = primary call_control_id`,
  `bridge_intent=true`) to the transfer target, runs a second "proxy agent" pipeline on that leg (briefs the
  human using `warm_transfer.briefing_prompt`) while the primary leg hears hold audio or continues talking to the AI,
  then creates a conference (`POST /v2/conferences` with the primary leg) and joins the target leg
  (`/actions/join`), then removes the AI from both legs (stop streaming). `stream_post_transfer_transcript=true` keeps a
  transcription-only stream (`stream_track="both_tracks"`, no bidirectional) to publish `post_transfer_transcript`
  events.

### 2.4 SMS and widget

See `21-messaging-sms.md` and `22-web-widget.md`. Both run the same conversation engine in text mode via
`SmsConversationWorkflow` / widget WebSocket handler; voice-specific nodes degrade as documented there.

## 3. Ownership, coordination, and failure handling

| Concern | Mechanism |
|---|---|
| Call ownership | Redis `SET call:{id}:owner {pod_id} NX PX 15000`; renewal task every 5 s; lost renewal → pipeline aborts with `end_reason='worker_lost'` and the workflow issues Telnyx hangup. |
| Control messages to an in-progress call | Redis pub/sub channel `call:{id}:control`; JSON messages `{"type":"stop"|"transfer"|"say"|"inject_context"|"takeover", ...}`. |
| Carrier events to the owning worker | Redis pub/sub `call:{id}:carrier`; edge also stores the latest state in hash `call:{id}:carrier_state` so a late subscriber reads it on attach. |
| Durable lifecycle | Temporal `CallWorkflow` per call, signals from edge and worker; workflow timers enforce `max_duration + 120 s` as a backstop hangup. |
| Media worker crash | Ownership lock expires; workflow backstop detects no heartbeat (`call:{id}:hb` key, 10 s TTL) for 20 s → Telnyx hangup, `end_reason='worker_crash'`, post-call still runs on partial transcript. |
| Graceful drain | `SIGTERM` → pod marks itself `draining` in Redis set `media:pods:draining`, readiness probe fails (no new WS), existing calls continue up to `MEDIA_DRAIN_TIMEOUT_S` (default 2 700 s); K8s `terminationGracePeriodSeconds` 2 760. |
| Telnyx webhook retries/duplicates | `telnyx_events.telnyx_event_id` unique; duplicates acknowledged and ignored. |
| Out-of-order media frames | Reorder buffer keyed on `media.chunk` (window 5 frames / 100 ms). |
| Vendor outage | STT socket reconnect with exponential backoff (50 ms, 100 ms, 200 ms, max 3) while buffering ≤ 2 s of audio; TTS socket reconnect and re-send current segment; LLM fallback to the org's `llm.fallback_model`; if all fail the agent plays the cached `error_phrase` and transfers to `fallback_number` or ends. |

## 4. Configuration and environments

- All services read configuration from environment variables via `va_common.settings.Settings` (pydantic-settings),
  documented exhaustively in `02-repo-layout-and-conventions.md` §6 and `.env.example`.
- Environments: `local` (docker-compose + simulators), `test` (pytest, simulators in-process), `staging`,
  `production`. `VA_ENV` selects defaults.
- Vendor base URLs are configurable (`TELNYX_API_BASE`, `ELEVENLABS_API_BASE`, `ELEVENLABS_WS_BASE`,
  `LLM_DEFAULT_BASE_URL`, `STRIPE_API_BASE`) so the simulators substitute transparently.

## 5. Technology choices (with rationale)

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 for all backend services; TypeScript 5.6 for dashboard, widget, TS SDK | Team preference; mature async ecosystem. |
| Packaging | `uv` workspace monorepo; `pnpm` workspace for TS | Fast, reproducible lockfiles. |
| Web framework | FastAPI 0.115+, Pydantic v2, uvicorn with `uvloop` and `httptools` | OpenAPI-first, async. |
| DB access | SQLAlchemy 2.0 async + asyncpg; Alembic migrations | Typed ORM + raw SQL where needed. |
| Workflows | Temporal (`temporalio` Python SDK) | Durable timers for scheduled calls, retries, batches, post-call. |
| Real-time pipeline | In-house asyncio pipeline (`packages/va_voice`) | Full control of Bland semantics (interruptibility levels, per-node timing, playback-accurate truncation); no framework coupling. Silero VAD (MIT) run through `onnxruntime`. |
| HTTP client | `httpx` (async, HTTP/2 enabled for vendor REST) | Connection pooling, timeouts, testable with `respx`. |
| WebSocket client | `websockets` 13+ | Async, supports headers, ping/pong. |
| Object storage | S3 API via `aioboto3`; MinIO in dev | Portable. |
| Analytics | ClickHouse via `clickhouse-connect` | Per-turn and per-event analytics at scale. |
| Cache/queues | Redis 7 via `redis.asyncio` | Pub/sub, streams, token buckets, locks. |
| Frontend | Next.js 15 App Router, React 19, Tailwind 4, shadcn/ui, TanStack Query 5, `@xyflow/react` 12, wavesurfer.js 7, zod, Playwright | Mature ecosystem for node editors and dashboards. |
| Sandboxed custom code | Deno 2 subprocess with `--allow-net=<allowlist>` and no fs/env/run permissions, CPU/memory/time limits | Strong isolation without containers per execution. |
| Docs | MkDocs Material + `mkdocs-render-swagger-plugin`-free approach: OpenAPI rendered via Redoc standalone bundle in `apps/docs` | Static, versionable. |
