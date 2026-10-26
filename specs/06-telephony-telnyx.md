# 06 — Telephony on Telnyx

Packages: `packages/va_telnyx` (client, models, verification), `apps/telephony_edge` (webhook ingress, routing),
activities in `packages/va_workflows/activities/telephony.py`, media protocol handling in `apps/media_worker`.

## 1. Accounts and resources

| Resource | Platform account | BYO Telnyx account (`telnyx_accounts`) |
|---|---|---|
| API key | `TELNYX_API_KEY` | `api_key_enc` (envelope-decrypted per use, cached in-process 5 min) |
| Webhook public key | `TELNYX_PUBLIC_KEY` | `public_key` |
| Call Control Application (`connection_id`) | `TELNYX_CALL_CONTROL_APP_ID` | `call_control_app_id` (created by `BootstrapTelnyxAccount` activity) |
| Outbound Voice Profile | `TELNYX_OUTBOUND_VOICE_PROFILE_ID` | `outbound_voice_profile_id` |
| Messaging Profile | `TELNYX_MESSAGING_PROFILE_ID` | `messaging_profile_id` |
| Webhook URL | `https://{EDGE_PUBLIC_HOST}/telnyx/voice` and `/telnyx/messaging` | `https://{EDGE_PUBLIC_HOST}/telnyx/voice/{telnyx_account_id}` and `/telnyx/messaging/{telnyx_account_id}` (path selects the verification key) |

`TelnyxClientFactory.for_call(call) -> TelnyxClient` returns the platform client or the BYO client by
`calls.config.telnyx_account_id`.

## 2. `va_telnyx.TelnyxClient`

```python
class TelnyxClient:
    def __init__(self, api_key: str, base_url: str, http: httpx.AsyncClient, limiter: TelnyxRateLimiter): ...
```

- Base URL `TELNYX_API_BASE` (`https://api.telnyx.com/v2`). Auth header `Authorization: Bearer <api_key>`.
- `httpx.AsyncClient(http2=True, timeout=httpx.Timeout(connect=2.0, read=8.0, write=5.0, pool=2.0))`, shared pool
  `max_connections=200`, `max_keepalive_connections=50`.
- Every call-command method takes `command_id: str` (deterministic, e.g. `f"hangup-{call_id}-{leg_role}"`) so Telnyx
  deduplicates retried commands.
- Responses parsed into Pydantic models; error bodies `{"errors":[{"code","title","detail","source"?}]}` raise
  `TelnyxApiError(status, errors)`.

### 2.1 Methods (complete list used by the platform)

| Method | HTTP | Path | Body fields we send |
|---|---|---|---|
| `dial(DialRequest)` | POST | `/calls` | `connection_id, to, from, from_display_name?, timeout_secs, time_limit_secs, answering_machine_detection, answering_machine_detection_config?, client_state, command_id, webhook_url, webhook_url_method="POST", stream_url, stream_track, stream_codec, stream_bidirectional_mode, stream_bidirectional_codec, stream_bidirectional_sampling_rate, stream_bidirectional_target_legs="self", stream_auth_token, record?, record_channels?, record_format?, record_track?, record_max_length?, custom_headers?, link_to?, bridge_intent?, bridge_on_answer?, send_silence_when_idle=true` |
| `answer(ccid, AnswerRequest)` | POST | `/calls/{ccid}/actions/answer` | `client_state, command_id, webhook_url, stream_url, stream_track, stream_codec, stream_bidirectional_mode, stream_bidirectional_codec, stream_bidirectional_sampling_rate, stream_bidirectional_target_legs="self", stream_auth_token, record?, record_channels?, record_format?, send_silence_when_idle=true` |
| `reject(ccid, cause)` | POST | `/calls/{ccid}/actions/reject` | `cause` ∈ `CALL_REJECTED`, `USER_BUSY`; `command_id` |
| `hangup(ccid)` | POST | `/calls/{ccid}/actions/hangup` | `client_state?, command_id` |
| `transfer(ccid, TransferRequest)` | POST | `/calls/{ccid}/actions/transfer` | `to, from, from_display_name?, timeout_secs, time_limit_secs, early_media=true, target_leg_client_state, client_state, command_id, webhook_url, answering_machine_detection="disabled", custom_headers?, record?` |
| `bridge(ccid, other_ccid)` | POST | `/calls/{ccid}/actions/bridge` | `call_control_id=other_ccid, client_state, command_id, park_after_unbridge?` |
| `send_dtmf(ccid, digits, duration_millis=250)` | POST | `/calls/{ccid}/actions/send_dtmf` | `digits` (`0-9 A-D * # w W`), `duration_millis` 100–500, `command_id` |
| `record_start(ccid, ...)` | POST | `/calls/{ccid}/actions/record_start` | `format="wav", channels="dual", play_beep=false, max_length=0, timeout_secs=0, command_id, client_state` |
| `record_stop(ccid)` | POST | `/calls/{ccid}/actions/record_stop` | `command_id, client_state` |
| `streaming_start(ccid, ...)` | POST | `/calls/{ccid}/actions/streaming_start` | `stream_url, stream_track, stream_codec, stream_bidirectional_mode, stream_bidirectional_codec, stream_bidirectional_sampling_rate, stream_auth_token, command_id, client_state` |
| `streaming_stop(ccid, stream_id?)` | POST | `/calls/{ccid}/actions/streaming_stop` | `stream_id?, command_id, client_state` |
| `playback_start(ccid, audio_url, loop="infinity", overlay=false, target_legs="self")` | POST | `/calls/{ccid}/actions/playback_start` | `audio_url, loop, overlay, target_legs, command_id, client_state` (hold music during warm transfer) |
| `playback_stop(ccid)` | POST | `/calls/{ccid}/actions/playback_stop` | `stop="all", command_id` |
| `conference_create(ccid, name)` | POST | `/conferences` | `call_control_id, name, beep_enabled="never", client_state, command_id, start_conference_on_create=true` |
| `conference_join(conf_id, ccid, ...)` | POST | `/conferences/{conf_id}/actions/join` | `call_control_id, start_conference_on_enter=true, end_conference_on_exit, hold=false, mute=false, client_state, command_id` |
| `conference_leave(conf_id, ccid)` | POST | `/conferences/{conf_id}/actions/leave` | `call_control_id, command_id` |
| `create_call_control_app(name, webhook_url)` | POST | `/call_control_applications` | `application_name, webhook_event_url, webhook_api_version="2", webhook_event_failover_url="", webhook_timeout_secs=25, first_command_timeout=true, first_command_timeout_secs=30, anchorsite_override="Latency", dtmf_type="RFC 2833", inbound={"channel_limit":null,"sip_subdomain_receive_settings":"only_my_connections"}, outbound={"outbound_voice_profile_id": …}` |
| `list_call_control_apps(name_contains)` | GET | `/call_control_applications?filter[application_name][contains]=…` | — |
| `update_call_control_app(id, ...)` | PATCH | `/call_control_applications/{id}` | `webhook_event_url, outbound.outbound_voice_profile_id` |
| `create_outbound_voice_profile(name)` | POST | `/outbound_voice_profiles` | `name, traffic_type="conversational", service_plan="global", concurrent_call_limit=null, enabled=true, whitelisted_destinations=["US","CA"]` (org international policy extends the list for BYO) |
| `list_outbound_voice_profiles(name)` | GET | `/outbound_voice_profiles?filter[name][contains]=…` | — |
| `create_messaging_profile(name, webhook_url)` | POST | `/messaging_profiles` | `name, webhook_url, webhook_api_version="2", whitelisted_destinations=["US","CA"], enabled=true` |
| `list_messaging_profiles(name)` | GET | `/messaging_profiles?filter[name]=…` | — |
| `search_numbers(...)` | GET | `/available_phone_numbers` | query `filter[country_code]`, `filter[national_destination_code]`, `filter[locality]`, `filter[administrative_area]`, `filter[phone_number][contains]`, `filter[features][]` (`voice`,`sms`,`mms`), `filter[phone_number_type]` (`local`,`toll_free`,`mobile`,`national`), `filter[limit]` (≤ 50), `filter[best_effort]=false` |
| `create_number_order(numbers, connection_id, messaging_profile_id?)` | POST | `/number_orders` | `phone_numbers=[{"phone_number": e164}], connection_id, messaging_profile_id?, customer_reference=org_id` |
| `get_number_order(id)` | GET | `/number_orders/{id}` | — (status `pending`/`success`/`failure`) |
| `get_phone_number_by_e164(e164)` | GET | `/phone_numbers?filter[phone_number]=e164` | — |
| `update_phone_number(id, connection_id?, tags?)` | PATCH | `/phone_numbers/{id}` | `connection_id, tags=["va", f"org:{org_id}"]` |
| `update_phone_number_messaging(id, messaging_profile_id)` | PATCH | `/phone_numbers/{id}/messaging` | `messaging_profile_id` |
| `delete_phone_number(id)` | DELETE | `/phone_numbers/{id}` | — |
| `send_message(...)` | POST | `/messages` | see `21-messaging-sms.md` §4 |
| `download(url)` | GET | absolute presigned URL (no auth header) | recording download |

Telephony credentials (WebRTC) are **not** used; web voice uses our own WebSocket audio transport (`22`).

### 2.2 Rate limiting and retries

- `TelnyxRateLimiter`: Redis token bucket per Telnyx account `rl:telnyx:{account}` — call commands 50/s burst 100,
  number search 5/s, number orders 2/s, messaging 20/s. Requests wait up to 2 s for a token, then raise
  `TelnyxRateLimited` (mapped to retry in activities, `503 SERVICE_UNAVAILABLE` in sync endpoints).
- Retries: idempotent methods (all call commands carry `command_id`; GETs) retry on connect errors, `429`, `500`,
  `502`, `503`, `504` — 3 attempts, delays 100 ms, 400 ms, 1 200 ms (+ jitter 0–100 ms), honoring `Retry-After`.
  `create_number_order`, `send_message` retry only on connect errors before bytes were sent; otherwise the activity
  layer reconciles (lookup by `customer_reference` / `tags`).
- Error mapping: `422` with code `90018` ("Call has already ended") → `CallAlreadyEnded` (treated as success for
  hangup and ignored for other commands); `404` on call commands → `CallAlreadyEnded`; other 4xx → `CARRIER_ERROR`
  with `details.telnyx_errors`; 5xx after retries → `CARRIER_ERROR`.

## 3. `client_state`

Base64 of compact JSON (≤ 1 KB): `{"v":1,"call_id":"<uuid>","leg":"primary|transfer_target|warm_proxy|monitor","org_id":"<uuid>"}`.
Telnyx echoes it on subsequent webhooks for that leg. `va_telnyx.client_state.encode/decode`; decode failures fall
back to `call_legs` lookup by `call_control_id`.

## 4. Webhook ingress (`apps/telephony_edge`)

Routes: `POST /telnyx/voice`, `POST /telnyx/voice/{telnyx_account_id}`, `POST /telnyx/messaging`,
`POST /telnyx/messaging/{telnyx_account_id}`, `GET /healthz`, `GET /readyz`, `GET /metrics`.

### 4.1 Verification

1. Read raw body bytes (no JSON re-serialization).
2. Headers `telnyx-signature-ed25519` (base64) and `telnyx-timestamp` (unix seconds). Missing → `401`.
3. `abs(now - timestamp) > 300` → `401` (replay protection).
4. `nacl.signing.VerifyKey(base64decode(public_key)).verify(f"{timestamp}|".encode() + raw_body, base64decode(sig))`;
   failure → `401`, metric `telnyx_webhook_invalid_signature_total`.
5. Parse envelope `{"data":{"event_type","id","occurred_at","payload","record_type":"event"},"meta":{"attempt","delivered_to"}}`.
6. Insert `telnyx_events` (system scope) with `ON CONFLICT (telnyx_event_id) DO NOTHING`; if the row already existed →
   respond `200` immediately (duplicate).
7. Process synchronously within a 2 s budget (map → state transition → publish), then respond `200`. Processing
   exceptions are caught, stored in `telnyx_events.process_error`, and re-attempted by the `TelnyxEventReprocessor`
   loop (every 5 s, max 5 attempts) so Telnyx does not need to retry; always `200` after a successful insert.

### 4.2 Media WebSocket verification (in `media-worker`)

Telnyx connects to `stream_url` with headers `telnyx-signature-ed25519` and `telnyx-timestamp`. Signed string:
`f"{timestamp}|{stream_url}"` where `stream_url` is the exact URL we supplied including query string. The worker
reconstructs it from `wss://{MEDIA_PUBLIC_HOST}{path}?{raw_query}`. Additionally the `token` query param must verify:
`token = base64url(call_id || exp_unix(8 bytes) || HMAC_SHA256(STREAM_TOKEN_SECRET, call_id || exp)[:16])`,
`exp` = dial time + max_duration + 1 h. Both checks must pass; failure → close code 4401. `stream_auth_token`
carries the same token (Telnyx forwards it as `Authorization: Bearer <token>`); if present it must equal the query
token. Live smoke test `test_media_ws_headers` records the headers Telnyx actually sends and asserts this scheme.

## 5. Voice webhook → domain mapping (complete)

`call` resolved via `client_state.call_id` → `call_legs.call_control_id` lookup. Inbound `call.initiated` without
state goes to the inbound router (§7).

| Telnyx `event_type` | Leg transition | Call transition | Domain event(s) | Other effects |
|---|---|---|---|---|
| `call.initiated` (outgoing) | → `initiated` | `dialing` → `ringing` | `call.ringing` | store `call_leg_id`, `call_session_id` |
| `call.initiated` (incoming) | create leg `initiated` | create call `ringing` | `call.ringing` | inbound router (§7) |
| `call.answered` | → `answered`, `answered_at` | `ringing` → `in_progress`; `answered_at`, `started_at` | `call.answered` | `call:{id}:carrier_state.answered=1`; signal workflow; transfer_target leg → see §10 |
| `call.bridged` | → `bridged` | `transferring` → `transferred` (transfer_target leg) | `call.transferred` | start transfer billing clock (`transfer_started_at` in carrier_state) |
| `call.hangup` | → `hangup`, store `hangup_cause`, `hangup_source`, `sip_hangup_cause` | per §5.1 | `call.ended` (if pipeline not attached) / carrier signal | stop billing clocks; signal workflow |
| `call.machine.detection.ended` | `amd_result` | — | `call.amd` | `result` `human`→`human_residence`, `machine`→`machine`, `not_sure`→`unknown` |
| `call.machine.greeting.ended` | — | — | `call.amd` (data.greeting_ended) | voicemail beep timing signal to worker |
| `call.machine.premium.detection.ended` | `amd_result` | `answered_by` set | `call.amd` | results: `human_residence`/`human_business` → `human`; `machine` → `voicemail`; `silence` → `unknown`; `fax_detected` → `fax` (then hangup, `end_reason='fax_detected'`) |
| `call.machine.premium.greeting.ended` | — | — | `call.amd` (data.beep_detected=true) | worker starts voicemail message immediately after beep |
| `call.machine.premium.call_screening.detected` | — | — | `call.amd` (data.screening=true) | worker treats as human-like screening: speaks `first_sentence` stating name and reason |
| `call.dtmf.received` | — | — | `call.turn` (role `user`, text `"[DTMF 5]"`) unless `ignore_button_press` | forwarded to worker (worker also receives `dtmf` on media WS; dedupe by digit + occurred_at within 300 ms) |
| `call.recording.saved` | — | — | `call.recording_ready` after S3 copy | start `CopyRecording` activity (§11) |
| `call.recording.error` | — | — | `call.error` (code `recording_failed`) | `calls.recording_s3_key` stays null |
| `call.playback.started` / `call.playback.ended` | — | — | none (debug log) | warm-transfer hold music tracking |
| `streaming.started` | — | — | none | carrier_state `streaming=1` |
| `streaming.stopped` | — | — | none | carrier_state `streaming=0` |
| `streaming.failed` | — | if `in_progress` and no worker attached after 5 s → `completed`, `end_reason='error'` | `call.error` (code `stream_failed`) | Telnyx `hangup`; agent cannot speak without stream |
| `conference.created` | — | — | none | store `conference_id` in carrier_state |
| `conference.participant.joined` / `conference.participant.left` | — | — | `call.transferred` (on target join) | warm transfer progression |
| `conference.ended` | — | — | none | |
| `call.hold` / `call.unhold` | — | — | none | ignored (stored) |
| `call.deepfake_detection.result` / `.error` | — | — | none | stored only |
| `call.conversation.created` / `.start_failed` | — | — | none | stored only (feature unused) |
| any other type | — | — | none | stored, metric `telnyx_webhook_unhandled_total{type}` |

### 5.1 Hangup cause handling

| Condition | Call status / end_reason |
|---|---|
| Leg never answered and `hangup_cause` = `timeout` or `no_answer` | `no_answer` / `no_answer`; disposition `NO_ANSWER` |
| Never answered and `hangup_cause` = `user_busy` | `busy` / `busy`; disposition `BUSY` |
| Never answered and `hangup_cause` = `call_rejected` | `failed` / `rejected`; disposition `FAILED` |
| Never answered and `hangup_cause` = `not_found` or `sip_hangup_cause` ∈ {`404`,`484`,`604`} | `failed` / `invalid_number` |
| Never answered and `hangup_cause` = `originator_cancel` | `canceled` / `canceled` (we canceled) |
| Never answered, any other cause (`unspecified`, `normal_clearing`, `time_limit`, unknown) | `failed` / `carrier_error`; `error_message` = `"Carrier hangup: <cause> (<sip_hangup_cause>)"` |
| Answered, `hangup_source` = `callee` (outbound) or `caller` (inbound), pipeline running | worker ends: `user_hangup` |
| Answered, `hangup_cause` = `time_limit` | `max_duration` |
| Answered, `hangup_source` = `unknown` | `user_hangup` |
| Answered, hangup initiated by us (`command_id` prefix `hangup-`) | end_reason already set by initiator; no change |

Unknown `hangup_cause` values are stored verbatim and treated as "any other cause". Live smoke test
`test_hangup_cause_values` places calls to a busy test number and an unanswered test number and asserts the causes
(`user_busy`, `timeout`) so the table is validated against production behaviour.

## 6. Outbound dial algorithm (activity `dial`)

1. Load call + config; abort if status not `queued` (stopped meanwhile).
2. Dispatch-time re-checks for scheduled/batch calls: DNC, calling window, caps, credits (each failure →
   `status='failed'`, `end_reason='dispatch_blocked'`, `call.dispatch_blocked` event, hold released).
3. Acquire concurrency slot `ZADD cap:org:{org}:concurrency {expiry} {call_id}` (expiry = now + max_duration + 5 min)
   after `ZREMRANGEBYSCORE … -inf now`; count > cap → requeue with 15 s backoff up to 30 min, then fail with
   `CONCURRENCY_CAP_REACHED`.
4. Select `from` (dialing strategy) and mark pool usage.
5. Build `DialRequest`:
   - `stream_url = f"wss://{MEDIA_PUBLIC_HOST}/ws/telnyx?call_id={id}&token={token}"`
   - `stream_track="inbound_track"`, `stream_codec="PCMU"` (or `L16` with `hd_audio`),
     `stream_bidirectional_mode="rtp"`, `stream_bidirectional_codec="PCMU"` (or `L16`),
     `stream_bidirectional_sampling_rate=8000` (or `16000`), `stream_bidirectional_target_legs="self"`
   - AMD: `answering_machine_detection="premium"` unless `ivr_mode` or `answered_by_enabled=false` or voicemail
     action `ignore` with `sensitive=false` → `"disabled"`. Config:
     `{"total_analysis_time_millis": 5000, "after_greeting_silence_millis": 800, "greeting_duration_millis": 3500, "initial_silence_millis": 3500, "maximum_number_of_words": 5, "silence_threshold": 256, "greeting_total_analysis_time_millis": 8000, "greeting_silence_duration_millis": 1500}`
   - Recording: `record="record-from-answer"`, `record_channels="dual"`, `record_format="wav"`, `record_track="both"`
     when `config.record`.
   - `time_limit_secs = min(14400, ceil(max_duration_min*60) + 30)`, `timeout_secs = orgs.settings.dial_timeout_s` (default 45).
   - `custom_headers=[{"name":"X-VA-Call-Id","value":call_id}]`.
   - `send_silence_when_idle=true` (keeps RTP flowing so far-end NAT does not drop media).
6. `dial` with `command_id=f"dial-{call_id}"`; on success insert `call_legs(role='primary')`, status → `dialing`,
   `dial_started_at`, emit `call.dialing`.
7. On `CARRIER_ERROR` → `failed`/`carrier_error`, release slot and hold, emit `call.error`.

## 7. Inbound routing algorithm (`va_edge.routing.route_inbound`)

1. Extract `to` (E.164) and `from` from payload; `call_control_id`.
2. `PhoneNumberRoutingRepo.get_active(to)` (system scope). None → `reject(cause="CALL_REJECTED")`; store
   `telnyx_events.process_error='unknown_number'`.
3. Org `status != 'active'` → if `inbound_config.fallback_number` → `transfer` to it; else reject.
4. Credits: `balance + credit_limit < min_attempt` → same as step 3.
5. Platform blacklist / org DNC for the caller (`inbound_config.block_dnc_callers`, default false) → reject.
6. Maintenance: `inbound_config.maintenance = {"enabled":bool,"windows":[{"start":"HH:MM","end":"HH:MM","days":[0-6],"timezone":tz}]}`
   active → transfer to fallback.
7. Concurrency cap reached → if fallback → transfer; else `reject(cause="USER_BUSY")`.
8. Build SendCallConfig from `inbound_config` (same resolver as outbound, runner from config) with
   `phone_number = from` (the caller), `from_number = to`.
9. Insert call (`direction='inbound'`, status `ringing`), leg (`primary`), contact upsert by caller phone.
10. Start `CallWorkflow` (inbound mode) — prefetch with 1.5 s budget runs in the edge process before answering:
    dynamic data with `cache` hits, memory, start node prompt; pending fetches continue in the workflow and are pushed
    via `{"type":"inject_context"}` when done.
11. `answer` with stream params (as §6.5), recording if configured, `command_id=f"answer-{call_id}"`.

Ring-to-answer target: ≤ 800 ms from `call.initiated` receipt to `answer` request.

## 8. Media WebSocket protocol models (`va_telnyx.media`)

Inbound (Telnyx → us), discriminated on `event`:

```python
class MediaConnected(BaseModel): event: Literal["connected"]; version: str
class MediaFormat(BaseModel): encoding: Literal["PCMU","PCMA","G722","OPUS","AMR-WB","L16"]; sample_rate: int; channels: int
class MediaStartInfo(BaseModel):
    user_id: str | None = None; call_control_id: str; call_session_id: str | None = None
    from_: str | None = Field(None, alias="from"); to: str | None = None; tags: list[str] = []
    client_state: str | None = None; media_format: MediaFormat
class MediaStart(BaseModel): event: Literal["start"]; sequence_number: str; start: MediaStartInfo; stream_id: str
class MediaPayload(BaseModel): track: str; chunk: str; timestamp: str; payload: str   # base64 RTP payload, no header
class MediaMedia(BaseModel): event: Literal["media"]; sequence_number: str; media: MediaPayload; stream_id: str
class MediaDtmfInfo(BaseModel): digit: str
class MediaDtmf(BaseModel): event: Literal["dtmf"]; stream_id: str; occurred_at: datetime; sequence_number: str; dtmf: MediaDtmfInfo
class MediaStopInfo(BaseModel): user_id: str | None = None; call_control_id: str
class MediaStop(BaseModel): event: Literal["stop"]; sequence_number: str; stop: MediaStopInfo; stream_id: str
class MediaErrorInfo(BaseModel): code: int; title: str; detail: str | None = None
class MediaError(BaseModel): event: Literal["error"]; payload: MediaErrorInfo; stream_id: str | None = None
class MediaMarkInfo(BaseModel): name: str
class MediaMark(BaseModel): event: Literal["mark"]; stream_id: str; sequence_number: str; mark: MediaMarkInfo
```

Error codes: `100002 unknown_error`, `100003 malformed_frame`, `100004 invalid_media`, `100005 rate_limit_reached`.
On `100003`/`100004` the worker logs and continues (our bug — alert); on `100005` it slows outbound sends to 20 ms
pacing (§`07` §9) for 5 s.

Outbound (us → Telnyx):

```json
{"event": "media", "media": {"payload": "<base64 PCMU or L16 bytes, 20 ms to 30 s per message>"}}
{"event": "mark", "mark": {"name": "seg:<turn_index>:<segment_index>"}}
{"event": "clear"}
```

Parsing uses `orjson.loads` + a hand-written dispatch on `event` (faster than Pydantic for `media`; `media` frames are
parsed into a lightweight `__slots__` dataclass `MediaFrame(chunk:int, timestamp:int, payload:bytes)`).

## 9. DTMF

- Inbound: from media WS `dtmf` and webhook `call.dtmf.received` (deduplicated). Handled by the engine (`07` §13).
- Outbound: `send_dtmf` for `precall_dtmf_sequence` (split on `w`: each `w` → 500 ms `asyncio.sleep` between
  `send_dtmf` calls of contiguous digit groups) and for Press Button nodes / `press_button` tool.

## 10. Transfers (exact command sequences)

### 10.1 Cold transfer

1. Worker finishes speaking the transfer line; waits for the mark echo of its last segment (timeout = remaining audio
   duration + 500 ms).
2. Emit `call.transfer_started` (`type=cold`, target). Call status → `transferring`.
3. `transfer(primary_ccid, to=target, from=caller_id, timeout_secs=30, time_limit_secs=remaining, target_leg_client_state=b64({"call_id","leg":"transfer_target"}), command_id=f"transfer-{call_id}-{n}")`.
4. If `stream_post_transfer_transcript=false`: `streaming_stop(primary_ccid)`; worker detaches pipeline (keeps
   ownership until the `call.bridged` or failure arrives).
   If true: keep the inbound stream but stop sending audio; switch pipeline to transcription-only mode and also
   request `streaming_start` on the target leg after bridge with `stream_track="inbound_track"` and no bidirectional
   mode, to a second WS path `/ws/telnyx/listen?call_id=…&leg=transfer_target&token=…`.
5. On `call.bridged` (target leg) → `transferred`, `transferred_to`, transfer clock starts; worker emits
   `call.transferred`, ends AI participation with `end_reason='transferred'` (transcription-only continues if enabled).
6. On target `call.hangup` before bridge (failure; primary still up): status back to `in_progress`; streaming restarted
   if stopped (`streaming_start` with the original parameters); agent speaks
   `orgs.settings.transfer_failed_message` (default "I'm sorry, I wasn't able to connect you. Is there anything else I can help with?")
   and resumes the node configured as `transfer_failed_node_id` (pathways) or continues the task.

### 10.2 Warm transfer

1. Agent says the hold line ("Let me connect you with someone, one moment."). If `hold_audio="music"` →
   `playback_start(primary, audio_url=f"https://{PUBLIC_API_BASE}/static/hold/hold_music.wav", loop="infinity")` after
   stopping our outbound audio; `silence` → nothing; `agent` → the AI keeps conversing on the primary leg.

   > Decision (2026-10-25): hold music is the procedural asset `assets/audio/hold_music.wav` (16 kHz 16-bit PCM
   > mono, 60 s loop, generated by `scripts/generate_audio_assets.py`, `07` §10.2); Telnyx `playback_start` accepts
   > WAV, so no MP3 encoder is added. The API serves it under `/static/hold/`.
2. `dial(to=target, from=caller_id, link_to=primary_ccid, bridge_intent=true, stream_url=…/ws/telnyx?call_id={id}&leg=warm_proxy&token=…, client_state=leg warm_proxy, answering_machine_detection="premium")`.
   Insert `call_legs(role='warm_proxy')`.
3. On proxy answer: the proxy pipeline (same worker, second `LegSession`) speaks the briefing generated from
   `warm_transfer.briefing_prompt` + conversation summary-so-far (router model, ≤ 60 words) and asks "Are you ready
   to take the call?". Proxy waits for affirmative (router classification) up to `timeout_s`.
4. AMD machine on proxy leg or no affirmative → hangup proxy, `playback_stop(primary)`, failure path of §10.1 step 6.
5. Affirmative → `playback_stop(primary)`; `conference_create(primary_ccid, name=f"va-{call_id}")`;
   `conference_join(conf_id, proxy_ccid, end_conference_on_exit=false)`; then `streaming_stop` on both legs (unless
   post-transfer transcript is enabled, which then uses a listen-only stream per leg).
6. Billing: proxy leg minutes billed at connected rate; after conference join, merged minutes at transfer rate.
   `end_reason='warm_transferred'`.

## 11. Recording copy flow (activity `copy_recording`)

1. `call.recording.saved` payload is parsed tolerant of unknown fields: `recording_urls.wav` (preferred) or
   `recording_urls.mp3`, `public_recording_urls.*`, `channels` (`single`/`dual`), `recording_started_at`,
   `recording_ended_at`, `recording_id` (optional). Missing URLs → `call.error` `recording_failed`.
2. Download immediately (URLs are short-lived presigned links) with streaming to a temp file (max 2 GB).
3. Upload to `s3://{S3_BUCKET_RECORDINGS}/recordings/{org_id}/{call_id}/raw.wav` with SSE-KMS (AWS) or SSE-S3 (MinIO),
   metadata `call_id`, `org_id`.
4. Compute duration via `soundfile.info`; set `calls.recording_s3_key`, `recording_duration_ms`; emit
   `call.recording_ready`.
5. Post-call creates `mixed.wav`, `caller.wav`, `agent.wav` (left = caller/inbound, right = agent/outbound — verified
   by live smoke test `test_recording_channel_order`).

## 12. Bootstrap (`scripts/telnyx_bootstrap.py` and activity `BootstrapTelnyxAccount`)

Idempotent; run with `uv run python scripts/telnyx_bootstrap.py --env-file .env` for the platform account.

1. Outbound voice profile named `voiceagents-{VA_ENV}`: list by name; create if absent.
2. Call Control Application named `voiceagents-{VA_ENV}`: list; create if absent with webhook
   `https://{EDGE_PUBLIC_HOST}/telnyx/voice` and the outbound profile; if present, PATCH webhook URL + profile when they differ.
3. Messaging profile `voiceagents-{VA_ENV}`: list; create/patch webhook `https://{EDGE_PUBLIC_HOST}/telnyx/messaging`.
4. Print `TELNYX_CALL_CONTROL_APP_ID=…`, `TELNYX_OUTBOUND_VOICE_PROFILE_ID=…`, `TELNYX_MESSAGING_PROFILE_ID=…` and, with
   `--write-env`, update those keys in the env file in place.
5. For BYO accounts the activity uses names `voiceagents-org-{org_id}` and the per-account webhook paths, then sets
   `telnyx_accounts.status='active'` or `error` with `status_detail`.

BYO creation flow (`POST /v1/accounts`, details in `16-phone-numbers.md` §7): validate key by `GET /balance`
(200 required), store encrypted, generate `encrypted_key_handle = "tlx_" + base62(32 random bytes)`, run bootstrap
activity, return handle.

## 13. Tests required

- Signature verification: valid vector (keypair generated in test), wrong key, tampered body, stale timestamp
  (> 300 s), missing headers; media WS signature over URL with query; token HMAC tamper/expiry.
- Client: each method sends exactly the specified body (respx assertions), retry policy (429 with Retry-After, 503),
  `CallAlreadyEnded` mapping, command_id determinism, rate limiter waits/raises.
- Webhook mapping: table-driven test feeding a fixture payload for every row in §5 and every row in §5.1, asserting
  leg/call transitions and emitted events; duplicates ignored; reprocessor retries failed processing.
- Inbound routing: every branch of §7 (unknown number, suspended, no credits, DNC, maintenance, concurrency, success).
- Transfer sequences against the Telnyx simulator: cold success, cold failure resume, warm success, warm decline,
  warm AMD machine; post-transfer transcript stream.
- Recording copy with simulator URL; tolerant parsing of extra/missing fields.
- Bootstrap idempotency (run twice → one of each resource; webhook drift patched).
- Live smoke (`LIVE_TESTS=1`): `test_live_outbound_answer` (calls `LIVE_TEST_ANSWER_NUMBER`), `test_media_ws_headers`,
  `test_hangup_cause_values`, `test_recording_channel_order`, `test_amd_machine` (calls a known voicemail number).

## 14. Acceptance criteria

1. All Telnyx traffic flows through `va_telnyx.TelnyxClient`; no other module builds Telnyx URLs.
2. Simulator e2e: outbound and inbound calls connect, stream audio both ways, record, transfer (cold and warm), and
   map hangups correctly.
3. Webhook endpoint p99 processing ≤ 150 ms (excluding inbound prefetch) under 500 events/s.
4. Zero unverified webhooks accepted (fuzz test with random signatures).
