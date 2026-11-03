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

> Decision (2026-11-01): publisher core (P03-T17).
> 1. A "call event" is any event with `call_id` set (`call.*`, `citations.ready`, `evals.completed`, ...). Only call
>    events get a `seq`, a `PUBLISH call:{<id>}:live`, and a `call_events` row.
> 2. Keys use the specs/03 §11 hash-tag form `call:{<uuid>}:seq` / `call:{<uuid>}:live`; the streams are
>    `events:calls|sms|widget|billing|system` (one key each). `va_events.keys` builds them (`va_common.redis.keys`
>    does not exist; bus keys live with the bus, as `apikey:` and `emb:q:` live with their owners).
> 3. Seq = `MULTI INCR + EXPIRE 172800` (TTL refreshed on every event): strictly increasing and gap-free per call under
>    concurrency. XADD order across concurrent publishers may differ from seq order; consumers order by `seq`.
> 4. A stream entry has one field `payload` = orjson of `model_dump(mode="json")` (seq included); the live PUBLISH
>    sends the same bytes. XADD and PUBLISH go in one non-transactional pipeline after the seq is assigned.
> 5. A Redis failure in steps 1-3 raises `EventPublishError` (cause chained, `.step` = `seq` or `stream`) and steps 4-5
>    do not run. Callers decide (the media worker logs and continues).
> 6. The row's category is `event.category` or the §2 default of the type; a call event with neither (e.g. `batch.*`
>    with a call id) has no row. Row `id` = the uuid inside `evt_<hex>`, `created_at` = `occurred_at`.
> 7. `publish(..., session=)` passes the caller's open `AsyncSession` (or `None`) to `WebhookRouter.route(event,
>    session=)`. Until phase 06 provides a router, publishers are built without one and step 5 is a no-op. Router
>    exceptions propagate.
> 8. `CallEventBuffer` (media worker) flushes at 200 pending rows, every 1 s on the injected `Clock`, on
>    `flush_call(call_id)` (writes everything pending), and on `aclose()`; flushes are serialised, one
>    `tenant_session(org_id)` per org, bulk insert with `ON CONFLICT (created_at, id) DO NOTHING` (re-writing a row is
>    harmless, e.g. after a COMMIT whose reply was lost). The writer attempts every org; when some fail it raises
>    `CallEventWriteError(unwritten=...)` with exactly those orgs' rows, and the buffer re-queues only them at the
>    front (order kept) for the next trigger, so one org's failure never holds back the others. Any other writer
>    exception re-queues the whole batch. At most 10 000 rows are pending; the oldest are dropped with an `events.call_events_dropped` warning
>    (count only). Rows added after `aclose()` are dropped the same way. Other services use `DirectCallEventWriter`
>    (each row written at once).
> 9. `StreamConsumer(redis, stream, group, consumer)`: `ensure_group()` (`XGROUP CREATE ... $ MKSTREAM`, BUSYGROUP
>    ignored); `read()` returns entries claimed with `XAUTOCLAIM` (idle ≥ 60 s, up to `count`) first, otherwise
>    `XREADGROUP ... >`; `ack(ids)`. The `payload` field is looked up as bytes or str (clients built with
>    `decode_responses=True`). Entries deleted while pending are acknowledged away; a payload that is not a
>    `DomainEvent` is acknowledged, logged `events.stream_poison` (entry id and stream only), and not returned.
> 10. (P05-T05b) `publish` is composed of two public steps for callers that write the `call_events` row in their own
>     transaction: `sequence(event)` (assigns the seq of a call event, returns non-call events unchanged; step `seq`) and
>     `broadcast(event)` (XADD + live PUBLISH; step `stream`). `va_events.call_events.insert_rows(session, rows)` is the
>     same idempotent `ON CONFLICT (created_at, id) DO NOTHING` insert `DbCallEventWriter` uses, in the caller's session.
>     The Send Call dispatch sequences before its commit, inserts the row in the same transaction, and broadcasts after
>     the commit (best effort).

## 2. Event type catalog (complete)

| Type | Category | Emitted by | When | Webhook `webhook_events` key |
|---|---|---|---|---|
| `call.queued` | queue | api | call row created | `queue` |
| `call.scheduled` | queue | api, worker | `start_time` in the future (worker: voicemail retry child, specs/07 §14) | `queue` |
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
`llm_fallback`, `llm_failed`, `pipeline_fatal`, `dispatch_failed`, `dial_failed`.
Level is `warning` for recoverable codes and `error` for `pipeline_fatal`, `llm_failed`, `stream_failed`,
`dispatch_failed`, `dial_failed`.

> Decision (2026-11-01): `dispatch_failed` (P05-T05b) is recorded by the api when `CallWorkflow` could not be started
> after the `calls` row was committed (`data = {"code": "dispatch_failed", "detail": "workflow_start"}`, message "Call
> workflow could not be started."); see specs/05 §5 Decision P05-T05b.

> Decision (2026-11-01): `dial_failed` (P05-T06b) is recorded by the `dial` activity when Telnyx rejects the dial
> (`data = {"code": "dial_failed", "detail": <first Telnyx error code, or "unknown", or "account_unavailable">}`,
> message = `calls.error_message`); see specs/06 §6 Decision P05-T06b.

## 3. Webhook routing rules

1. **Per-call webhook** (`webhook` param on Send Call / inbound config / batch call object):
   - Always receives `call.completed` (format `bland`: the bare Call Object, §4).
   - Receives live events whose key is listed in the call's `webhook_events` array (format `bland-live`, §5).
2. **Batch `status_webhook`:** one POST per batch phase (§6), final with counts.
3. **Org webhook endpoints** (`webhook_endpoints` table, managed in dashboard and `/v1/webhooks/endpoints`):
   receive events whose type matches any pattern in `event_types` (glob, `*` = all), format `envelope` (§7).
4. **SMS / widget conversation webhooks**: `conversation.*`, `sms.*`, `widget.message` in `envelope` format.

> Decision (2026-11-03): webhook router (P06-T02). `va_events.DbWebhookRouter` implements `WebhookRouter`; the pure
> planner is `va_events.webhook_routing`, the bodies `va_events.webhook_payloads`.
> 1. `webhook_deliveries.format` stores the signing family: `bland` for the bare Call Object, the `bland-live` body
>    (§5), and the batch body (§6); `envelope` otherwise. The `payload` column holds the rendered body.
> 2. Targets are planned in this order and deduplicated by URL (the first planned target wins; inserts are
>    `ON CONFLICT (event_id, url) DO NOTHING`, so routing an event twice is harmless): per-call `webhook`
>    (`calls.config.webhook`) → batch `status_webhook` → conversation `webhook` → widget `webhook_url` (not soft-deleted)
>    → enabled org endpoints ordered by `(created_at, id)`.
> 3. Per-call webhook: `call.completed` always (body = the event's `data`, the Call Object); any other call event only
>    when its §2 key is in `calls.config.webhook_events` (body §5 with `metadata` = `calls.metadata`); `—`
>    (`call.analysis_ready`) is never sent live. The org setting `webhook_format` (specs/04 §7) picks the per-call
>    format: `bland` (default) as above, `envelope` wraps the same body in §7.
> 4. Batch: `batch.*` events with `batch_id` whose batch has a `status_webhook` get the §6 body (format `bland`):
>    `status` = the type without `batch.`, `phase` = the type, counts and `created_at`/`updated_at` from the row;
>    `batch.completed`/`batch.stopped` add `completed_at` (row value, else the event time); `batch.failed` adds `code`
>    and `reason` (row `failure_code`/`failure_reason`, else event `data.code`/`data.reason`, else `batch_failed`/`""`).
> 5. Conversation webhooks: `sms.*`/`conversation.*` events with `conversation_id`; widget webhooks: `widget.message`/
>    `conversation.*` events with `widget_id`. Both envelope.
> 6. Org endpoints match with case-sensitive globs (`fnmatchcase`; `*` = all, `call.*`). Envelope `data` is the Call
>    Object for `call.completed`, the §5 body for other call events, the §6 body for `batch.*` events of a known
>    batch, else the event's `data`. The platform-admin side of `system.fraud_suspected` is not routed here.
> 7. HIPAA (specs/23 §7): in a `hipaa_mode` org every target except an org endpoint with `phi_allowed = true` gets
>    transcripts removed: Call Object `transcripts` → `[]`, `concatenated_transcript` → `""`, `pathway_logs` entries
>    lose `text` and `user_text`; `call.turn` and `call.post_transfer_transcript` bodies lose `data.text`.
> 8. Timestamps written by the router (`created_at` of §5/§7, batch times) are UTC ISO 8601 with milliseconds and `Z`.
> 9. `route(event, session=)` inserts in the caller's transaction (outbox); with `session=None` it opens its own
>    `tenant_session(event.org_id)`. `route_many(events, session=)` serves batched callers (one transaction per org
>    without a session). The call, batch, conversation, and widget are read only for events that can reach them; org
>    settings are read from `orgs.settings` in the same session, only when a delivery is planned.

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

> Decision (2026-11-02): Call Object serialization (P05-T16a). The contract models are
> `va_domain.call_object` (`CallObject`, and `CallListItem` = the Call Object minus the specs/05 §6 heavy fields;
> `CallObject` extends it) and the rows are read by `va_db.call_object` (`load_call_objects` /
> `load_call_list_items`, in the caller's `tenant_session`, one query per child table for any number of calls), so
> the `call.completed` webhook (P05-T18) reuses them. Field rules: `call_ended_by` and `end_reason` are `null` until
> the call ended; `to`/`from` are `""` when the call has no number (web calls); `status` maps `no_answer` →
> `no-answer`, `dialing`/`ringing`/`in_progress`/`transferring` → `in-progress`, `transferred` → `completed`;
> `answered_by` maps `no_answer` → `no-answer`; `started_at` is `answered_at` (falls back to `calls.started_at`);
> `completed` is `queue_status = 'complete'` and a missing `queue_status` reads `new`; `max_duration`, `record`,
> `analysis_schema` and `local_dialing` (`dialing_strategy.type = 'local'`) come from `calls.config` (defaults 30,
> false, null, false); `persona_version` is the version number as a string, `agent_version` the agent version's
> semver, `experiment_arm` the arm label. `transcripts` lists every `call_turns` row except role `system`, ordered
> by `(turn_index, start_ms, id)`, with `id` = 1-based position; `concatenated_transcript` is `"{role}: {text}\n"`
> per transcript entry joined by one space. `pathway_logs` (specs/11 §12 keys) has one entry per assistant turn with
> a `node_id` (empty for task calls): `tag.name` = `node_name`, `user_text` = the preceding user turn, and `decision`
> / `tag.color` come from the `call.route` event (category `route`) whose data carries `turn_index` equal to the
> turn's (`decision` = that data without `turn_index`/`tag_color`); the pathway engine (P07) must put `turn_index`
> and `tag_color` into `call.route` data. `latency` = nearest-rank p50/p95 of the turns' `latency.e2e_ms`, rounded
> means of `llm_ttft_ms`/`tts_ttfa_ms`, `turns` = turns with an `e2e_ms` (all `null`/0 without). `voicemail` is
> `null` unless a `call.voicemail` event exists, `answered_by = 'voicemail'`, or `end_reason` is
> `voicemail_hangup`/`voicemail_left`; then `{detected: true, action_taken: <latest event's data.action, else
> hangup / leave_message from end_reason, else null>, message_left: end_reason = 'voicemail_left'}`.
> `warm_transfer_calls` = `[{role, state, started_at, answered_at, ended_at, hangup_cause}]` of the `warm_proxy`
> legs; `analysis` = the latest `analysis_schema` analysis result; `evals` = the latest eval run; `citations` =
> `[{citation_schema_id, variable_name, variable_type, value, conditions_met, cited_utterances}]`.
> `recording_url` is presigned per read (7 days) only for calls with `recording_s3_key`; a presign failure is
> logged (`calls.recording_url_failed`, no key or URL) and both recording fields are `null`.

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

> Decision (2026-11-01): destination URL guard (P05-T05a). The rules above live in `va_common.net`:
> `check_destination_url(url, *, allow_http, allow_private, resolver=, timeout_s=2.0)` returns the vetted addresses
> (for a later pinning client) or raises `UnsafeDestinationError(reason)` with `reason` one of `scheme`, `host`,
> `resolution`, `address`. Scheme `https`, or `http` only when the caller allows it; a host is required. IP-literal
> hosts (also `[v6]`) are checked without DNS; names are resolved once (`getaddrinfo`, every A/AAAA answer) within the
> timeout and **every** answer must pass, so a mixed public/private answer is refused; an empty answer, a resolver
> error, or the timeout is `resolution`. `blocked_address(ip, *, allow_private=False)` judges an IPv4-mapped IPv6
> address by its embedded IPv4 address, then blocks anything that is not `is_global` plus multicast, reserved
> (`240/4`, IPv6 reserved blocks except loopback `::1`), and unspecified addresses: a superset of the list above and
> of the specs/28 §6.4 extras. With `allow_private` (`WEBHOOK_ALLOW_PRIVATE`, honored only in local/test) private,
> loopback, link-local, and CGNAT targets pass, but the metadata addresses `169.254.169.254` and `fd00:ec2::254`,
> multicast, reserved, and unspecified addresses stay blocked. `WEBHOOK_ALLOW_PRIVATE` is the shared settings mixin
> `va_common.settings.WebhookSafetySettings` (forced false in staging/production) used by the dispatcher and the api.
> Send Call (specs/05 §3.7) maps the reasons to `WEBHOOK_URL_REJECTED` on field `webhook`.
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
