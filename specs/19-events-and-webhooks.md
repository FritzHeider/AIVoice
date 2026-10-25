# 19 — Event Bus, Webhooks, and Live Event Streams

## 1. Internal event model (`packages/va_events`)

```python
class DomainEvent(BaseModel):
    id: str                      # "evt_" + uuid7 hex
    type: EventType              # dotted name, see §2
    org_id: UUID
    occurred_at: datetime        # UTC
    call_id: UUID | None = None
    batch_id: UUID | None = None
    conversation_id: UUID | None = None
    widget_id: UUID | None = None
    category: CallEventCategory | None = None   # for call.* live events (matches call_events.category)
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str = ""
    data: dict[str, JsonValue] = {}
    seq: int | None = None       # per-call monotonic sequence assigned by the publisher
```

`EventPublisher.publish(event, *, persist_call_event: bool = True, deliver_webhooks: bool = True)`:

1. Assigns `seq` for call events with `INCR call:{id}:seq` (key TTL 48 h).
2. `XADD events:{stream} MAXLEN ~ 1000000 * payload <json>` where stream is `calls` for `call.*`, `batch.*`,
   `citations.*`, `evals.*`; `sms` for `sms.*`/`conversation.*`; `widget` for `widget.*`; `billing` for `billing.*`;
   `system` otherwise.
3. `PUBLISH call:{id}:live <json>` for call events (dashboard live view and `GET /v1/calls/{id}/events/stream`).
4. When `persist_call_event`, buffers the row for `call_events` (write-behind flush every 1 s or 200 rows, and on
   call end). The media worker owns the buffer for in-call events; other services write directly.
5. When `deliver_webhooks`, calls `WebhookRouter.route(event)` which inserts `webhook_deliveries` rows in the same
   database transaction as the caller's state change where one exists (transactional outbox). For in-call live
   events emitted by the media worker, the outbox insert is batched with the `call_events` flush.

Consumers use Redis consumer groups (`XREADGROUP GROUP <service> <pod>`), acknowledge after processing, and
claim stale pending entries older than 60 s with `XAUTOCLAIM`.

## 2. Event type catalog (complete)

| Type | Category | Emitted by | When | Webhook `webhook_events` key |
|---|---|---|---|---|
| `call.queued` | queue | api | call row created | `queue` |
| `call.scheduled` | queue | api | `start_time` in the future | `queue` |
| `call.dispatch_blocked` | queue | worker | blocked by cap/DNC/window/credits (data.reason) | `queue` |
| `call.dialing` | call | worker | Telnyx dial accepted | `call` |
| `call.ringing` | call | edge | `call.initiated` (outbound) / inbound received | `call` |
| `call.answered` | call | edge | `call.answered` | `call` |
| `call.amd` | voicemail | edge | AMD result (data.result: human_residence/human_business/machine/silence/fax_detected) | `call` |
| `call.voicemail` | voicemail | media | voicemail detected and action taken (data.action) | `call` |
| `call.started` | call | media | pipeline attached and first audio sent or greeting awaited | `call` |
| `call.turn` | call | media | a turn finalized (data: role, text, node_id, interrupted) | `call` |
| `call.interrupt` | interrupt | media | barge-in honored (data: played_chars, total_chars) | `call` |
| `call.latency` | latency | media | per turn; message like `"E2E: 742ms"` and data `{eot_ms, stt_final_ms, llm_ttft_ms, tts_ttfa_ms, e2e_ms}`; additionally one event per stage with Bland-style messages `"STT: 63ms"`, `"LLM: 266ms"`, `"TTS: 218ms"` | `latency` |
| `call.route` | route | media | pathway edge chosen (data: from_node, to_node, label, alternatives[{label, score}], global:bool) | `call` |
| `call.extraction` | extraction | media | variables extracted (data: variables) | `call` |
| `call.loop_condition` | route | media | condition scored (data: score 0–10, met:bool) | `call` |
| `call.tool` | tool | media | tool request/response (data: tool_name, request{url,method,body}, status_code, response_excerpt ≤ 2 KB, duration_ms, extracted) | `tool` |
| `call.dynamic_data` | dynamic_data | worker/media | dynamic data fetch result | `dynamic_data` |
| `call.webhook_node` | webhook | media | pathway webhook node executed | `webhook` |
| `call.guard_rail` | guard_rail | media | guard rail evaluated/triggered | `call` |
| `call.transfer_started` | transfer | media | transfer initiated (data: type cold/warm, target) | `call` |
| `call.transferred` | transfer | edge | `call.bridged` on target | `call` |
| `call.post_transfer_transcript` | post_transfer_transcript | media | transcript chunk after transfer (data: speaker, text) | `post_transfer_transcript` |
| `call.sms_sent` | sms | media | SMS node / voicemail SMS sent | `call` |
| `call.error` | error | any | recoverable or fatal error (data: code, detail) | `call` |
| `call.ended` | call | media/worker | pipeline finished (data: end_reason, ended_by) | `call` |
| `call.recording_ready` | call | worker | recording stored in S3 | `call` |
| `call.analysis_ready` | call | worker | summary/disposition/analysis done | — (included in completion payload) |
| `call.completed` | call | worker | post-call finished; **this triggers the primary post-call webhook** with the full Call Object | always sent to `webhook` when set |
| `citations.ready` | citations | worker | citations extracted | `citations` |
| `evals.completed` | evals | worker | post-call evals scored | `evals` |
| `batch.created`, `batch.validating`, `batch.dispatching`, `batch.in_progress`, `batch.completed`, `batch.stopped`, `batch.failed` | — | worker | batch lifecycle (one per phase) | `status_webhook` |
| `sms.received`, `sms.sent`, `sms.delivered`, `sms.failed` | sms | edge/worker | Telnyx messaging events | SMS conversation `webhook` |
| `conversation.created`, `conversation.message`, `conversation.closed`, `conversation.human_takeover` | — | worker/api | SMS + widget conversations | conversation/widget `webhook` |
| `widget.message` | — | media/api | widget message exchanged | widget `webhook_url` |
| `billing.low_balance`, `billing.recharged`, `billing.payment_failed` | — | worker | credits | org endpoints |
| `number.purchased`, `number.released`, `number.order_failed` | — | worker | number orders | org endpoints |
| `tendlc.brand_status`, `tendlc.campaign_status` | — | worker | 10DLC registration status changes | org endpoints |
| `call.retranscription_ready` | call | worker | batch re-transcription merged into turns | `call` |
| `call.transcript_corrected` | call | api | transcript corrected via API | `call` |
| `voice.clone_ready`, `voice.clone_failed` | — | worker | voice cloning | org endpoints |
| `system.fraud_suspected` | — | worker/api | toll-fraud or abuse control tripped (`28-security.md`) | org endpoints + platform admins |
| `system.retention_completed` | — | worker | daily retention run summary for the org | org endpoints |
| `kb.ready`, `kb.failed` | — | worker | knowledge base ingestion | org endpoints |

### 2.1 `call.error` data codes

`call.error` events carry `data.code` from this closed set: `stt_reconnect`, `stt_warning`, `tts_error`, `stream_failed`,
`recording_failed`, `listen_leg_misrouted`, `TEMPLATE_VAR_MISSING`, `TOOL_FAILED`, `CUSTOM_CODE_FAILED`, `KB_TIMEOUT`,
`PATHWAY_UNPUBLISHED`, `PATHWAY_VERSION_FALLBACK`, `VOICE_NOT_FOUND`, `CONTACT_IDENTIFIER_CONFLICT`, `RECORDING_MISSING`,
`llm_fallback`, `llm_failed`, `pipeline_fatal`.
Level is `warning` for recoverable codes and `error` for `pipeline_fatal`, `llm_failed`, `stream_failed`.

## 3. Webhook routing rules

1. **Per-call webhook** (`webhook` param on Send Call / inbound config / batch call object):
   - Always receives `call.completed` (format `bland`: the bare Call Object, §4).
   - Receives live events whose key is listed in the call's `webhook_events` array (format `bland-live`, §5).
2. **Batch `status_webhook`:** one POST per batch phase (§6), final with counts.
3. **Org webhook endpoints** (`webhook_endpoints` table, managed in dashboard and `/v1/webhooks/endpoints`):
   receive events whose type matches any pattern in `event_types` (glob, `*` = all), format `envelope` (§7).
4. **SMS / widget conversation webhooks**: `conversation.*`, `sms.*`, `widget.message` in `envelope` format.

## 4. Call Object (post-call webhook body and `GET /v1/calls/{call_id}` response)

Exact field set (Bland-compatible names; additional fields are ours and documented as such):

| Field | Type | Notes |
|---|---|---|
| `call_id` | string (uuid) | |
| `c_id` | string (uuid) | alias of `call_id` (Bland legacy) |
| `org_id` | string | ours |
| `external_id` | string \| null | |
| `batch_id` | string \| null | |
| `to` | string | E.164 |
| `from` | string | E.164 |
| `inbound` | boolean | |
| `direction` | `"outbound"\|"inbound"\|"web"` | ours |
| `test_call` | boolean | ours; `true` for dashboard test calls (`05-calls-api.md` `test_call`) |
| `created_at` | ISO 8601 | |
| `started_at` | ISO 8601 \| null | answered time |
| `end_at` | ISO 8601 \| null | |
| `call_length` | number | minutes, 6 decimals (connected only) |
| `corrected_duration` | string | seconds as integer string (Bland compat) |
| `completed` | boolean | true when post-call finished |
| `status` | string | `completed`, `failed`, `no-answer`, `busy`, `canceled`, `in-progress`, `queued`, `scheduled` (Bland hyphenated form) |
| `queue_status` | string | `complete`, `started`, `allocated`, `queued`, `call_error`, `new` |
| `answered_by` | `"human"\|"voicemail"\|"ivr"\|"unknown"\|"no-answer"\|"fax"` \| null | |
| `call_ended_by` | `"ASSISTANT"\|"USER"\|"SYSTEM"\|"API"\|"GUARD_RAIL"\|"TIMEOUT"\|"ERROR"` | Bland uppercase |
| `end_reason` | string | ours, §9 of `05-calls-api.md` |
| `max_duration` | number | minutes |
| `record` | boolean | |
| `recording_url` | string \| null | signed URL valid 7 days, regenerated on each GET |
| `recording_expiration` | ISO 8601 \| null | |
| `request_data` | object | |
| `metadata` | object | |
| `variables` | object | final variable bag including built-ins (`now_utc`, `from`, `to`, `call_id`, `timezone`, `language`, extracted vars, webhook response vars) |
| `pathway_id` | string \| null | |
| `pathway_version` | integer \| null | |
| `persona_id`, `persona_version`, `agent_id`, `agent_version` | string \| null | |
| `experiment_id`, `experiment_arm` | string \| null | ours |
| `pathway_logs` | array | `[{"tag":{"name":"<node name>","color":"#..."},"node_id","role","text","decision":{...},"created_at"}]` — one entry per turn with node, chosen route, extracted vars |
| `transcripts` | array | `[{"id": int, "user": "user"\|"assistant"\|"agent-action"\|"human_agent", "text": str, "created_at": ISO8601, "start_ms": int, "end_ms": int, "interrupted": bool}]` |
| `concatenated_transcript` | string | `"user: …\n assistant: …\n"` (Bland format) |
| `summary` | string \| null | |
| `disposition_tag` | string \| null | |
| `analysis_schema` | object \| null | as requested |
| `analysis` | object \| null | results of `analysis_schema` / analyze endpoint |
| `citations` | array | when citation schemas were requested (`citations.ready` also sent separately) |
| `evals` | object \| null | `{eval_run_id, overall_score, passed, results}` when computed |
| `price` | number | dollars, 6 decimals |
| `transferred_to` | string \| null | |
| `transfer_duration` | number | minutes |
| `local_dialing` | boolean | dialing strategy local |
| `error_message` | string \| null | |
| `latency` | object | ours: `{p50_e2e_ms, p95_e2e_ms, avg_llm_ttft_ms, avg_tts_ttfa_ms, turns}` |
| `voicemail` | object \| null | `{detected: bool, action_taken, message_left: bool}` |
| `warm_transfer_calls` | array | child legs summary |
| `guard_rails` | array | `[{guard_rail, triggered, action, turn_index}]` from `guard_rail_events` |

## 5. Live event webhook body (`bland-live` format)

```json
{
  "call_id": "0193c5d2-…",
  "category": "latency",
  "level": "info",
  "message": "LLM: 266ms",
  "data": {"llm_ttft_ms": 266, "turn_index": 4},
  "seq": 57,
  "created_at": "2026-10-24T18:22:31.412Z",
  "metadata": {"…": "…"}
}
```

## 6. Batch status webhook body

One POST per phase (`batch.created` → `batch.validating` → `batch.dispatching` → `batch.in_progress` → terminal):

```json
{"batch_id":"…","status":"in_progress","phase":"batch.in_progress","calls_total":1200,"calls_dispatched":340,
 "calls_completed":290,"calls_successful":251,"calls_failed":39,"created_at":"…","updated_at":"…"}
```

Terminal success body adds `"completed_at"`; terminal failure body has `"code"` and `"reason"`. The dispatcher waits
up to **10 s** per attempt for batch status webhooks (Bland parity); other webhooks wait 15 s.

## 7. Envelope format (org endpoints, SMS, widget)

```json
{
  "id": "evt_0193c5d2…",
  "type": "call.completed",
  "created_at": "2026-10-24T18:25:02.001Z",
  "org_id": "…",
  "call_id": "…",
  "batch_id": null,
  "conversation_id": null,
  "data": { }
}
```

For `call.completed` the `data` is the Call Object; for live call events `data` is the §5 body.

## 8. Delivery semantics (`apps/webhook_dispatcher`)

- Poll loop: `SELECT … FROM webhook_deliveries WHERE status IN ('pending','delivering') AND next_attempt_at <= now()
  ORDER BY next_attempt_at LIMIT 200 FOR UPDATE SKIP LOCKED` (system scope), mark `delivering`, deliver concurrently
  (semaphore 200 per pod; per-host semaphore 20 to avoid hammering one customer).
- Request: `POST url`, `Content-Type: application/json`, `User-Agent: VoiceAgents-Webhooks/1.0`, headers:
  - `X-VA-Event-Id: evt_…` (also sent as `X-Event-Id`)
  - `X-VA-Event-Type: call.completed`
  - `X-VA-Timestamp: <unix seconds>`
  - `X-VA-Signature: t=<unix>,v1=<hex HMAC_SHA256(org_secret, "<t>.<raw body>")>`
  - `X-Webhook-Signature: <hex HMAC_SHA256(org_secret, raw body)>` (Bland-compatible header name and algorithm)
- Success: any 2xx within the timeout. Redirects are not followed (treated as failure with code `redirect`).
- Retry schedule after failure (attempt n → delay): 1: 10 s, 2: 30 s, 3: 2 min, 4: 10 min, 5: 30 min, 6: 2 h, 7: 6 h,
  8: 15 h (cumulative ≈ 24 h). After attempt 8 fails → `dead`. Jitter ±10 %.
- `410 Gone` → immediately `dead` and, for org endpoints, auto-disable the endpoint and notify org admins by email.
- Each attempt is recorded in `webhook_deliveries.last_*` and as a `call_events` row (category `webhook`) for call
  webhooks so customers see delivery outcomes in the call log.
- SSRF protection: resolve DNS and refuse private, loopback, link-local, multicast, and metadata ranges
  (`10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, ::1, fc00::/7, fe80::/10, 100.64/10`), except in `local`/`test`
  env where `WEBHOOK_ALLOW_PRIVATE=true` is honored. Only `http`/`https`; `http` refused in production unless the
  org setting `allow_insecure_webhooks=true`.
- Replay: `POST /v1/webhooks/deliveries/{id}/replay` re-queues (new attempt series, same event id).
- Dead-letter view: `GET /v1/webhooks/deliveries?status=dead`.

## 9. Live streams for clients

- `GET /v1/calls/{call_id}/events/stream` — Server-Sent Events; replays persisted events (`call_events` ordered by
  `seq`) then tails `call:{id}:live`; closes 30 s after `call.completed`. Event lines: `id: <seq>`, `event: <type>`,
  `data: <json bland-live body>`. Supports `Last-Event-ID`.
- `WS /v1/calls/{call_id}/transcript/ws?token=<short-lived token>` — real-time transcript (`call.turn` partials and
  finals, `post_transfer_transcript`) for dashboards and customer apps; token from
  `POST /v1/calls/{id}/transcript/token` (60 s TTL, single call scope).
- Dashboard live call list: `GET /v1/calls/active` (poll) plus SSE `GET /v1/events/stream?types=call.*` scoped to
  org. Accepts dashboard session auth or an API key with scope `webhooks:read` (used by `va webhooks listen`, which
  re-signs forwarded events locally with the org signing secret so receivers can verify them).

## 10. Webhook endpoint management API

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/v1/webhooks/endpoints` | — | `{data:[WebhookEndpoint]}` |
| POST | `/v1/webhooks/endpoints` | `{url, description?, event_types?: string[], enabled?: bool}` | `{data: WebhookEndpoint}` |
| PATCH | `/v1/webhooks/endpoints/{id}` | any subset | `{data: WebhookEndpoint}` |
| DELETE | `/v1/webhooks/endpoints/{id}` | — | `{data:{deleted:true}}` |
| POST | `/v1/webhooks/endpoints/{id}/test` | `{event_type?: string}` | sends a synthetic signed event; returns delivery result |
| GET | `/v1/webhooks/deliveries` | `status?, event_type?, call_id?, cursor?, limit≤100` | paginated deliveries |
| POST | `/v1/webhooks/deliveries/{id}/replay` | — | `{data: WebhookDelivery}` |
| GET | `/v1/webhooks/signing-secret` | — | `{data:{secret_last4, created_at}}` (owner/admin) |
| POST | `/v1/webhooks/signing-secret/rotate` | — | `{data:{secret}}` shown once; old secret valid 24 h (dual-sign `v1=` twice) |

## 11. Tests required

- Signature generation matches a fixed vector (secret `whsec_test`, timestamp `1700000000`, body `{"a":1}`) in both
  header formats; the vector values are computed in the test and asserted against a literal produced once and
  committed (snapshot via syrupy).
- Retry schedule, jitter bounds, 410 handling, SSRF refusals (parametrized over every blocked range), per-host
  concurrency, dual-sign during rotation, outbox atomicity (state change rollback ⇒ no delivery row).
- SSE replay + tail, `Last-Event-ID` resume, close after completion.
