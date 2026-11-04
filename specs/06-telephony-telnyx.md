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
| `get_recording(recording_id)` | GET | `/recordings/{recording_id}` | — (fresh `download_urls`; §11 Decision P05-T14 rule 7) |
| `delete_recording(recording_id)` | DELETE | `/recordings/{recording_id}` | — (`404` → `False`: already gone; §11 Decision P05-T14) |

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

> Decision (2026-11-01): `va_telnyx` transport (P03-T08a).
> 1. Rate-limit categories: `call_command` (every `/calls` and `/conferences` POST, dial included) 50/s burst 100,
>    `number_search` 5/s burst 5, `number_order` 2/s burst 2, `messaging` 20/s burst 20 (burst = rate where the
>    list above names none). One bucket per account and category: `rl:telnyx:{account}:{category}` (the account is
>    `platform` or the BYO `telnyx_accounts.id`). Other operations (provisioning, phone-number management, order
>    status, balance, download) are not limited client-side. `acquire` sleeps on the injected clock until a token
>    arrives; it raises `TelnyxRateLimited` as soon as the waits of one acquire would exceed 2 s. A Redis error
>    fails open (`telnyx_rate_limiter_unavailable` warning): Telnyx's own `429` and the retry policy still apply.
> 2. "3 attempts, delays 100 ms, 400 ms, 1 200 ms" means up to three **retries** (four requests), one per delay,
>    each plus a uniform 0-100 ms jitter. `Retry-After` (delta seconds or HTTP-date) replaces the delay when it is
>    longer; a `Retry-After` above 5 s ends the retries and the response is mapped as is.
> 3. Idempotent operations: every call and conference command, `dial` included (all carry `command_id`), and every
>    GET, PATCH, and DELETE. They retry on `429`/`500`/`502`/`503`/`504` and on any transport error (connect, read,
>    write, protocol). Non-idempotent operations (`create_number_order`, `send_message`, provisioning POSTs) retry
>    only on `ConnectError`, `ConnectTimeout`, and `PoolTimeout` (no bytes sent).
> 4. A `404` counts as `CallAlreadyEnded` only for `/calls/{ccid}/actions/*`; a `404` from a conference command is a
>    `CARRIER_ERROR` (the conference may be the missing resource), while `422`/`90018` is `CallAlreadyEnded`
>    everywhere. A transport failure after the retries is `TelnyxTransportError`, also `CARRIER_ERROR` with
>    `details = {"telnyx_errors": [], "transport": "<exception class>"}`; `TelnyxRateLimited` is
>    `SERVICE_UNAVAILABLE` with `Retry-After`. An error body that is not a Telnyx error document becomes one item
>    `{"code": "http_<status>", "title": <reason phrase>, "detail": <first 200 characters>}`; a 2xx body of the wrong
>    shape is a `TelnyxApiError` with code `invalid_response`.
> 5. Request models (`DialRequest`, `AnswerRequest`, `TransferRequest`, `StreamParams`) send the defaults of the §2.1
>    rows and omit `None` fields; `stream_bidirectional_target_legs="self"` is added whenever dial/answer carry a
>    stream. `DialRequest.timeout_secs` defaults to 45 (§6 step 5), `TransferRequest.timeout_secs` to 30 (§10.1).
>    A listen-only stream (§10.1 step 4) passes `None` for the three bidirectional fields.

> Decision (2026-11-01): provisioning, numbers, messaging, download, factory (P03-T08b).
> 1. Operations and categories: `search_numbers` → `number_search`, `create_number_order` → `number_order`
>    (non-idempotent), `send_message` → `messaging` (non-idempotent); provisioning POSTs are non-idempotent; every
>    GET, PATCH, and DELETE is idempotent; provisioning, phone-number management, order status, balance, and
>    download have no client-side bucket (Decision P03-T08a rules 1 and 3).
> 2. List methods (`list_call_control_apps`, `list_outbound_voice_profiles`, `list_messaging_profiles`) return every
>    page: `page[size]=250`, `page[number]` from 1 while below `meta.total_pages` (a list without `meta` is one
>    page), at most 20 pages. A list counts as one operation (one metric observation, one span). Name filters are
>    applied by Telnyx (`contains` for apps and outbound voice profiles, exact for messaging profiles); callers that
>    need an exact name compare it themselves.
> 3. `NumberSearch` validates before any request (`country_code` two upper-case letters, default `US`; `limit`
>    1–50, default 10; features ⊆ `voice`/`sms`/`mms`; `phone_number_type` ∈ the four §2.1 values); unset filters
>    are not sent, features are repeated `filter[features][]`, and `filter[best_effort]=false` is always sent.
> 4. Create bodies carry exactly the §2.1 fields, `null` values included (`inbound.channel_limit`,
>    `concurrent_call_limit`); update methods send only the fields given and raise `ValueError` when none is
>    given. `get_phone_number_by_e164` returns `None` when Telnyx lists no number. `number_tags(org_id)` builds
>    `["va", "org:{org_id}"]`. `send_message` sends `use_profile_webhooks=false` and requires `text` or 1–10
>    `media_urls`.
> 5. `download(url, destination, max_bytes=2 GiB)` (§11 step 2) requires an absolute URL (`ValueError` otherwise),
>    sends no auth header, streams to `destination` (truncated on every attempt), and retries like a GET. Past
>    `max_bytes` it raises `TelnyxDownloadTooLarge` (`CARRIER_ERROR`, `details = {"telnyx_errors": [], "max_bytes":
>    n}`); `destination` is removed whenever the download fails. Error bodies are read up to 64 KiB.
> 6. `TelnyxClientFactory(base_url, http, limiter, platform_api_key, resolve_key, ...)`: `for_account(None)` and
>    `for_call` with no `config.telnyx_account_id` return the platform client; a BYO key comes from the injected
>    async resolver and is cached in process for 5 minutes per account (one resolve in flight per account); a
>    resolver answering `None` raises `UnknownTelnyxAccount` (a `LookupError`, not cached); `invalidate(id)` drops
>    the cached key after rotation or deletion. A BYO client's `account` (rate-limit bucket) is
>    `str(telnyx_account_id)`.

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

> Decision (2026-11-01): webhook and media verification API (P03-T09).
> 1. `va_telnyx.verify_webhook(raw_body, headers, public_key, now)` and
>    `va_telnyx.verify_media_ws(stream_url, headers, public_key, now)` return `None` or raise
>    `WebhookVerificationError` whose `reason` is `missing_headers` | `malformed_timestamp` | `stale_timestamp` |
>    `invalid_signature`. Header names match case-insensitively; an empty header counts as missing; the timestamp
>    must be 1–12 ASCII digits (unix seconds; a longer value is `malformed_timestamp`); `abs(now - timestamp) > 300` is stale (exactly 300 s is accepted, future
>    skew is rejected the same way); a signature that is not base64, not 64 bytes, or does not verify is invalid.
>    `now` must be timezone-aware. Verification runs on the raw bytes before any JSON parsing; the caller maps every
>    reason to `401` (edge) or close code `4401` (media worker) and counts `telnyx_webhook_invalid_signature_total`.
>    The stream `token` HMAC of §4.2 is the media worker's check, not part of `verify_media_ws`.
> 2. `public_key` is the base64 raw 32-byte Ed25519 key (`TELNYX_PUBLIC_KEY`) or a loaded `nacl.signing.VerifyKey`;
>    `load_public_key` raises `ValueError` on bad base64 or length (a configuration error, raised at startup).
> 3. `va_telnyx.webhooks.parse_envelope(raw_body)` parses the step 5 envelope (`meta` optional, unknown fields
>    ignored; failure → `WebhookParseError`). `parse_payload(event)` returns the model of the `event_type`
>    (`va_telnyx.voice_events` for every §5 row, `va_telnyx.message_events.MessageEventPayload` for
>    `message.received|sent|finalized`); models ignore unknown fields and default every field Telnyx may omit. An
>    unknown `event_type` parses as `UnknownEventPayload(event_type, raw)`, never an error; a known type whose
>    payload lacks a required field (`call_control_id`, `conference_id`, `digit`, message `id`/`direction`/`from`)
>    raises `WebhookParseError`.
> 4. Media parsing (§8): `va_telnyx.media.parse_media_message(raw)` dispatches by hand on `event`; `media` →
>    `MediaFrame(chunk, timestamp, payload)` (slots dataclass, base64 decoded); control frames are validated by the
>    §8 models (unknown fields ignored); unknown `event` → `UnknownMediaEvent(event, raw)`; non-JSON, non-object,
>    missing required fields, bad integers, or bad base64 → `MediaProtocolError` (a `ValueError`). Outbound
>    builders `media_message(payload)`, `mark_message(name)`, `clear_message()` return the JSON bytes;
>    `segment_mark(turn, segment)` / `parse_segment_mark(name)` handle `seg:<turn_index>:<segment_index>`.

> Decision (2026-11-01): webhook intake (P05-T01).
> 1. The route selects the verification key, never the body (it is untrusted until verified): `/telnyx/voice` and
>    `/telnyx/messaging` use `TELNYX_PUBLIC_KEY` (in `VA_ENV=local` an empty value falls back to
>    `.sim/telnyx_public_key`; the edge refuses to start without a valid key); `/telnyx/{voice|messaging}/{id}` use
>    the `public_key` of that `telnyx_accounts` row via `TelnyxAccountLookupRepo` (`pending`, `active`, or `error`;
>    an unknown, `deleted`, or non-UUID id, or a stored key that does not load, answers `401` like a bad
>    signature). Keys are cached in-process 5 min (unknown accounts 30 s) in two separate LRU caches of at most
>    10 000 entries each, so a flood of distinct forged account ids (one lookup each) neither grows memory without
>    bound nor evicts real accounts' keys. `connection_id`/`client_state` are read
>    only after verification and only for attribution: `org_id` = the BYO account's org on a BYO route, else
>    `client_state.org_id`; `call_id` = `client_state.call_id` (dropped on a BYO route when the state names another
>    org); `call_control_id` = `payload.call_control_id`; an undecodable `client_state` is ignored; an org that
>    does not exist stores NULL `org_id`. The stored `payload` is the whole verified envelope (`data` + `meta`).
> 1a. Before anything else the raw body is capped at 1 MiB (Telnyx webhooks are a few KiB): a larger
>    `Content-Length` is refused without reading, a chunked or understated body stops being read at the first chunk
>    past the cap. Both answer `413 {"error":"payload_too_large"}`, store nothing, and count
>    `event_type="unverified", result="error"` (not `invalid_signature`: no signature was checked).
> 2. Step 7 is asynchronous so Telnyx gets a fast `200`: after the insert the edge offers the stored event to an
>    in-process `EventProcessor` and answers `{"status":"ok"}` (a duplicate answers `{"status":"duplicate"}`).
>    Events are sharded by `crc32(call_control_id or telnyx_event_id) % 8`, one worker per shard, so one leg's
>    events are processed in arrival order. Each shard queue holds 256 events; when full the event stays
>    unprocessed in Postgres and the reprocessor claims it (backpressure never turns into a non-2xx after an
>    insert, which Telnyx would redeliver as a duplicate). A handler runs within the 2 s budget (then
>    `HandlerTimeoutError`); `call.initiated` has a 5 s budget (the inbound accept, §7 Decision P05-T12c:
>    `EventProcessor(budgets={...})`). Handlers are registered per `event_type` and must be idempotent; a type without a
>    handler is marked processed. Other answers: `400 {"error":"invalid_payload"}` for a verified body that is not
>    an envelope (nothing stored), `401 {"error":"invalid_signature"}`, `503 {"error":"unavailable"}` while shutting
>    down or when the database is unreachable before the insert (Telnyx retries).
> 3. `TelnyxEventReprocessor` runs every 5 s and claims (`TelnyxEventRepo.claim_retries`, specs/03 §2 Decision)
>    events never processed for 30 s and failed events (`process_error` set) with fewer than 5 attempts whose last
>    change is older than 5 s; `telnyx_events.process_attempts` (migration `0005`) counts every attempt, and the
>    claim's `updated_at` touch is the lease that keeps other edge pods off the row.
> 4. Metrics: the `telnyx_webhook_invalid_signature_total` of step 4 and the `telnyx_webhook_unhandled_total{type}` of
>    §5 are the catalog counter `va_telnyx_webhooks_total{event_type,result}` (specs/27 §3.2): rejected requests
>    count `event_type="unverified", result="invalid_signature"`, unparseable envelopes `event_type="invalid",
>    result="error"`, then per event `duplicate`, `processed`, `unhandled` (no `va_telnyx` model for the type), or
>    `error` (parse or handler failure, stored in `process_error`). `va_telnyx_webhook_processing_ms{event_type}`
>    observes handler plus mark time per processed event.

### 4.2 Media WebSocket verification (in `media-worker`)

Telnyx connects to `stream_url` with headers `telnyx-signature-ed25519` and `telnyx-timestamp`. Signed string:
`f"{timestamp}|{stream_url}"` where `stream_url` is the exact URL we supplied including query string. The worker
reconstructs it from `wss://{MEDIA_PUBLIC_HOST}{path}?{raw_query}`. Additionally the `token` query param must verify:
`token = base64url(call_id || exp_unix(8 bytes) || HMAC_SHA256(STREAM_TOKEN_SECRET, call_id || exp)[:16])`,
`exp` = dial time + max_duration + 1 h. Both checks must pass; failure → close code 4401. `stream_auth_token`
carries the same token (Telnyx forwards it as `Authorization: Bearer <token>`); if present it must equal the query
token. Live smoke test `test_media_ws_headers` records the headers Telnyx actually sends and asserts this scheme.
> Decision (2026-11-01): the token expiry is specs/28 §6.2 Decision P05-T06a (mint time + max duration + 10 min),
> not + 1 h.

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
| `call.recording.saved` | — | — | `call.recording_ready` after S3 copy | signal `PostCallWorkflow` `recording_saved`; its `store_recording` activity copies it (§11) |
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
| Answered, hangup initiated by us (`calls.end_reason` already set; Decision P05-T02 item 7) | end_reason already set by initiator; no change |

Unknown `hangup_cause` values are stored verbatim and treated as "any other cause". Live smoke test
`test_hangup_cause_values` places calls to a busy test number and an unanswered test number and asserts the causes
(`user_busy`, `timeout`) so the table is validated against production behaviour.

> Decision (2026-11-01): carrier event mapping (P05-T02).
> 1. Contract: `va_domain.carrier.CarrierEvent(type, leg, call_id, org_id, telnyx_event_id, occurred_at, payload)` is
>    both the `call:{<id>}:carrier` message (JSON) and the argument of the Temporal signal `signal_carrier_event` on
>    workflow `call-<id>` (sent as `model_dump(mode="json")` so the default data converter carries it). Key builders
>    `carrier_state_key`, `carrier_channel`, `owner_key` (hash-tag form) and `CARRIER_STATE_TTL_S = 7200` live there.
> 2. Resolution: the intake's `client_state` attribution (org and call id) when the payload's `client_state` decodes to
>    the same ids, else the system-scope `CallLegLookupRepo.by_call_control_id` (a BYO route's org must match). The
>    leg role comes from `client_state`, else the `call_legs` row. An unresolvable incoming `call.initiated` goes to
>    the `InboundRouter` hook (P05-T12; without one: log `telnyx.inbound_unrouted`); any other unresolvable event, or
>    a `client_state` naming a call that does not exist, is logged `telnyx.event_unattributed` and marked processed.
> 3. `va_edge.mapping.plan_effects(event_type, payload, snapshot, occurred_at=)` is pure and covers every row of the
>    table above. Signalled to the workflow: `call.answered`, `call.bridged`, `call.hangup`, every `call.machine.*`
>    type, `streaming.failed`, `call.recording.saved`, `call.recording.error`. Published on the carrier channel: every
>    resolved event except the "stored only" rows (`call.hold`/`call.unhold`, `call.deepfake_detection.*`,
>    `call.conversation.*`). Playback events are logged at debug and forwarded only.
> 4. The edge issues no Telnyx commands: the `fax_detected` hangup and the `streaming.failed` hangup (in progress, no
>    worker after 5 s) are performed by `CallWorkflow` from the signal (P05-T06). The edge records
>    `answered_by='fax'` and emits `call.amd` / `call.error` (`stream_failed`, level `error`; `recording_failed` is
>    level `warning`); `data.detail` carries Telnyx's `failure_reason` / `reason`.
> 5. `call.dtmf.received` is forwarded on the carrier channel only; the media worker (which also receives the WS `dtmf`
>    and dedupes) emits the `call.turn` `"[DTMF 5]"` with `ignore_button_press` applied, so turns are never doubled.
> 6. Legs: every leg-changing event first upserts the leg (`CallLegRepo.record_initiated`, which only fills missing
>    `call_leg_id`/`call_session_id` on conflict), then moves it with a guarded update; an out-of-order or repeated
>    event leaves the leg as is. Only the `primary` leg drives the call; `transfer_target` events change their own
>    leg and carrier state (`transfer_target_answered`, `transfer_target_hangup`), except that `call.bridged` or
>    `conference.participant.joined` of the `transfer_target` leg moves the call `transferring` → `transferred`
>    (`transferred_to` = payload `to`) and stores `transfer_started_at`.
> 7. Hangup of the primary leg: never answered (`answered_at` null) → the §5.1 rows in table order (`answered_by =
>    'no_answer'` for `no_answer`; `disposition_tag` `NO_ANSWER`/`BUSY`/`FAILED` for no_answer/busy/rejected;
>    `error_message = "Carrier hangup: <cause> (<sip>)"` with `unknown` for a missing value). Answered: when
>    `calls.end_reason` is already set (we ended it; Telnyx hangup webhooks carry no `command_id`, so this replaces
>    the "`hangup-` prefix" row) or `call:{<id>}:owner` exists (the pipeline ends the call), the call is unchanged;
>    otherwise `completed` with `max_duration` (cause `time_limit`), `transferred` (status `transferred`), else
>    `user_hangup`, `connected_seconds` = answered → min(end, transfer start) and `transfer_seconds` = transfer start
>    → end (milliseconds). `ended_at` = payload `end_time`, else the event's `occurred_at`; `call.ended` carries
>    `end_reason` and `ended_by`.
> 8. Ordering and retries: leg and call changes commit in one `tenant_session`; then carrier state (`HSET` +
>    `EXPIRE`), domain events, channel `PUBLISH`, signal. Domain events of a status change are published only when this
>    processing applied it, or on a retry (`process_attempts > 0`) that finds the call already in the target status;
>    events without a status change are published on every successful run (at least once). Any Redis or Temporal
>    failure raises, so the processor records `process_error` and the reprocessor retries.
> 9. AMD: `call.machine.detection.ended` stores the raw result on the leg and emits `call.amd {result}` normalized
>    (`human`→`human_residence`, `not_sure`→`unknown`, missing → `unknown`, others verbatim), without `answered_by`.
>    Premium detection stores the raw result on the leg and sets `calls.answered_by` (`human_residence`/
>    `human_business`→`human`, `machine`→`voicemail`, `fax_detected`→`fax`, anything else → `unknown`); `call.amd`
>    carries `result` (raw) and `answered_by`. Greeting ended → `data.greeting_ended=true`, premium greeting ended →
>    `data.beep_detected=true`, screening → `data.screening=true`; carrier-state fields `amd_result`, `answered_by`,
>    `greeting_ended`, `beep_detected`, `screening`. `va_amd_results_total{result=<raw>}` counts detection results.
> 10. `TemporalCarrierSignaller`: an `RPCError` `NOT_FOUND` (no workflow yet, or already finished) is logged at debug
>    (`telnyx.workflow_missing`) and tolerated; other errors propagate.

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

> Decision (2026-11-01): dial and end-state activities (P05-T06b). `va_workflows.activities.telephony`
> (`TelephonyActivities.dial`, `.hangup_call`) and `va_workflows.activities.call_end` (`CallEndActivities.end_call`);
> inputs, results, timeouts and retry policies in `activities.telephony_inputs`, error types in `activities.errors`,
> the request in `activities.dial_request` (`DialSettings`: platform app id, `MEDIA_PUBLIC_HOST`, `ws` scheme only
> for `VA_ENV` `local`/`test`, stream secret, `INTERNATIONAL_ALLOWED_COUNTRIES`).
> 1. Status guard: terminal → `skipped`; `scheduled` → admission (item 2), then `scheduled → queued` (with
>    `credit_hold_id`) and `queued → dialing` (`dial_started_at`) in one transaction committed **before** the
>    request, so a `call.initiated` that overtakes the response finds `dialing`; `dialing` without a primary leg →
>    the dial is sent again (Telnyx deduplicates `dial-{call_id}`); a primary leg recorded, or `ringing` and later →
>    `already_dialed`. After the response: `call_legs(role='primary')` upsert + `call.dialing`
>    (`data.call_control_id`) in one transaction. A stop that wins a transition race → `skipped`, the admission
>    rolled back.
> 2. Steps 2-3 for `scheduled` calls are `va_billing.dispatch.admit_call` at dispatch time (`at = now`, region of
>    `calls.to_number`). `CONCURRENCY_CAP_REACHED` → outcome `requeue` (nothing taken; the workflow waits 15 s, up
>    to 30 min) unless `final_attempt`; `SERVICE_UNAVAILABLE` → retryable `DispatchUnavailable`; any other code →
>    `failed`/`dispatch_blocked` (`ended_by='system'`, `error_message` = the error message, `queue_status=
>    'call_error'`) with `call.dispatch_blocked` (`warning`, `data = {reason: <specs/31 code>, message}`) then
>    `call.ended`. No hold or slot exists then, so nothing is released. Calls already `queued` were admitted by Send
>    Call and are not re-checked.
> 3. Step 4: `from` is `calls.from_number` (selected read-only at Send Call, specs/16 §6.2 Decision P05-T03c); pool
>    usage marking arrives with phase 10. `connection_id` is the BYO account's `call_control_app_id` when
>    `config.telnyx_account_id` is set; a missing app or key fails the call like a carrier rejection (message
>    "Telnyx account is not available for dialing.", detail `account_unavailable`). `webhook_url` is omitted (the
>    app's URL). The stream token is `mint_stream_token(call_id, stream_token_expiry(now, max_duration_min))`.
> 4. Step 7: a `TelnyxApiError` left after the client's own retries (4xx, or 5xx) → `dialing → failed`,
>    `end_reason='carrier_error'`, `ended_by='error'`, `error_message = "Carrier rejected the dial: <first error title,
>    detail or code>"` (≤ 500 chars), `queue_status='call_error'`; hold released, slot released after the commit;
>    `call.error` (`dial_failed`, specs/19 §2.1) then `call.ended`. The client rate limit or a transport failure →
>    retryable `TelnyxUnavailable` (the call stays `dialing`; the retry resends the same `command_id`).
> 5. `end_call(reason)`: terminal → unchanged. `scheduled`/`queued`/`dialing`/`ringing` → `canceled` (reason
>    `canceled`, `disposition_tag='CANCELED'`) or `failed` (`queue_status='call_error'`); the hold is released only
>    when the call never reached Telnyx (`scheduled`/`queued`, or `dialing` without a primary leg), otherwise
>    post-call billing settles it. `in_progress`/`transferring`/`transferred` → `completed` with `connected_seconds`/
>    `transfer_seconds` (`va_db.call_states.call_durations`, `transfer_started_at` from the carrier state); an
>    `end_reason` already stored by the media worker wins; `canceled`/`dispatch_blocked`/`carrier_error` there is a
>    non-retryable `InvalidEndReason`. The slot is released whenever the call is terminal afterwards, also when it
>    already was (a carrier-ended call) or a concurrent update ended it (ZREM is idempotent), so `end_call` is
>    CallWorkflow's idempotent cleanup. Events: `call.dispatch_blocked` (reason `dispatch_blocked`) then `call.ended {end_reason, ended_by}`. The workflow calls `end_call` **before**
>    `hangup_call`, so the hangup webhook finds `end_reason` set and leaves the call alone (§5 Decision P05-T02).
> 6. `hangup_call` sends `hangup` with `command_id("hangup", call_id, "primary")` to the primary leg (none →
>    `no_leg`; "already ended" → `already_ended`); rate limit, transport, 429 and 5xx → retryable
>    `TelnyxUnavailable`, any other rejection → non-retryable `HangupRejected`.
> 7. Every event row is written in the transaction of the change it describes (`seq` from Redis first; a Redis
>    failure is a retryable `EventsUnavailable` that rolls the change back) and broadcast after the commit (best
>    effort). Retry policies: `dial` 30 s timeout, 5 attempts; `hangup_call` and `end_call` 15 s, 6 attempts; all 1 s
>    doubling to 10 s.

## 7. Inbound routing algorithm (`va_edge.routing.route_inbound`)

1. Extract `to` (E.164) and `from` from payload; `call_control_id`.
2. `PhoneNumberRoutingRepo.get_active(to)` (system scope). None → `reject(cause="CALL_REJECTED")`; store
   `telnyx_events.process_error='unknown_number'`.
3. Org `status != 'active'` → if `inbound_config.fallback_number` → `transfer` to it; else reject.
4. Credits: `balance + credit_limit < min_attempt` → same as step 3.
5. Platform blacklist / org DNC for the caller (`inbound_config.block_dnc_callers`, default false) → reject.
6. Maintenance: `inbound_config.maintenance = {"enabled":bool,"windows":[{"start":"HH:MM","end":"HH:MM","days":[0-6],"timezone":tz}]}`
   active → transfer to fallback.
   > Decision (2026-11-02): the `maintenance` object is the specs/16 §4 shape (`enabled`, `starts_at`, `ends_at`,
   > `message`; `va_domain.numbers.maintenance_active`), not `windows` (specs/16 §4 Decision P05-T12a).
7. Concurrency cap reached → if fallback → transfer; else `reject(cause="USER_BUSY")`.
8. Build SendCallConfig from `inbound_config` (same resolver as outbound, runner from config) with
   `phone_number = from` (the caller), `from_number = to`.
9. Insert call (`direction='inbound'`, status `ringing`), leg (`primary`), contact upsert by caller phone.
10. Start `CallWorkflow` (inbound mode) — prefetch with 1.5 s budget runs in the edge process before answering:
    dynamic data with `cache` hits, memory, start node prompt; pending fetches continue in the workflow and are pushed
    via `{"type":"inject_context"}` when done.
11. `answer` with stream params (as §6.5), recording if configured, `command_id=f"answer-{call_id}"`.

Ring-to-answer target: ≤ 800 ms from `call.initiated` receipt to `answer` request.

> Decision (2026-11-02): inbound routing (P05-T12b). `va_edge.routing.InboundRouter` (wired into the carrier
> handler; the processor runs it for an incoming `call.initiated` without a call) implements steps 1-7; the pure
> `decide(RoutingFacts)` holds the order. (1) Order: number lookup (`to` missing or no live `active` number →
> reject `unknown_number` through the platform account); invalid stored `inbound_config` → refuse
> `invalid_inbound_config` (the stored `fallback_number` is still used when it is valid E.164); specs/16 §4
> `blocked_callers` → reject `caller_blocked` (never transferred), non-empty `allowed_callers` without the caller →
> reject `caller_not_allowed` (anonymous/unparseable callers never match a list; the caller is normalized with the
> called number's region); org `status != 'active'` → refuse `org_<status>`; credits → refuse
> `insufficient_credits` when balance − open holds + `credit_limit` < the plan's effective `min_attempt_micros`
> (holds count, unlike the step 4 shorthand; specs/24's "no attempt minimum" concerns charging, not this floor);
> `block_dnc_callers` and a DNC hit for the caller (`va_compliance.dnc.find_block`: platform, own, and inherited
> parent rows — the platform DNC list is the "platform blacklist"; the destination fraud prefixes are not applied to
> callers) → reject `caller_dnc`; active maintenance → transfer `maintenance` with a fallback, else answer with
> the message (`Accept(maintenance_message=True)`, played in P05-T12c) when `maintenance.message` is set, else
> reject `maintenance`; concurrency → a slot is taken now under a pre-allocated UUIDv7 call id
> (`CallLimiter.acquire_inbound`: plan cap with the new-account tightening, no batch share, expiry
> `ceil(max_duration)` + 5 min); full → refuse `concurrency_cap` with cause `USER_BUSY` when there is no fallback.
> "Refuse" = transfer to `fallback_number` when set, else reject `CALL_REJECTED`. (2) An accepted call is handed to
> the `InboundAcceptor` seam (P05-T12c) with the call id, number, parsed config, and normalized caller; no
> acceptor (`accept_unavailable`) or an acceptor that raises (`accept_failed`, specs/16 §4 "pipeline failure")
> releases the slot and refuses. (3) Commands go through the number's account (BYO number → BYO client):
> `reject` with `command_id=reject-{call_control_id}`; `transfer` to the fallback with `from` = the called number,
> `timeout_secs` 30, `time_limit_secs` 14 400, `command_id=fallback-{call_control_id}`, no `client_state`
> (there is no call row; `TransferRequest.client_state`/`target_leg_client_state` are optional). Telnyx allows
> transferring an unanswered incoming call: the original leg is answered when the target answers. A command on a leg
> that already ended (`CallAlreadyEnded`) is logged (`telnyx.inbound_caller_gone`) and still settles. (4) After the
> command the router raises `va_edge.processor.EventSettled(reason)`: the row stores the reason in
> `process_error` with `process_attempts` raised to the processor's `max_attempts`
> (`TelnyxEventRepo.mark_processed(..., settled_attempts=)`), counts as `processed`, and is never reclaimed.
> Failures before the command (database, Redis, Telnyx) are ordinary errors and retried; the commands are
> idempotent by `command_id`. Reasons: `unknown_number`, `invalid_inbound_config`, `caller_blocked`,
> `caller_not_allowed`, `org_<status>`, `insufficient_credits`, `caller_dnc`, `maintenance`, `concurrency_cap`,
> `accept_unavailable`, `accept_failed`. (5) `va_inbound_calls_total{result}` (specs/27 §3.2) counts `rejected`,
> `fallback`, and `answered` (an accepted call handed over without error). The edge now mixes in
> `TelnyxApiSettings` and `CryptoSettings` (`KMS_PROVIDER`/`LOCAL_MASTER_KEY` scoped to the edge too) for BYO keys.

> Decision (2026-11-02): inbound accept (P05-T12c). `va_edge.accept.InboundAccept` is the `InboundAcceptor` wired
> by default (`va_edge.main.carrier_handlers`; tests pass `NO_ACCEPTOR` to keep `accept_unavailable`). Steps 8-11
> run in this order within a 3 s accept budget on the edge clock: (a) one `tenant_session`: `inbound_request` →
> `load_inbound_inputs` → `resolve_inputs` (resolution `AppError`s commit nothing and the router refuses
> `accept_failed`), contact upsert by the caller's E.164 (`ContactRepo.upsert_phone`; anonymous or non-E.164
> callers get none), the `calls` row (`va_db.call_rows.call_row`, shared with Send Call: `direction='inbound'`,
> `status='ringing'`, `queue_status='started'`, `to_number` = the called number and `from_number` = the caller as
> Bland reports them (the config keeps `phone_number` = caller, `from_number` = called), `config.pricing` = the
> org's effective pricing snapshot, `config.maintenance_message` for a maintenance accept, no credit hold), the
> primary leg (`CallLegRepo.record_initiated` with `call_leg_id`/`call_session_id`, the number's Telnyx account,
> `started_at` = the payload's `start_time`), and a `call.ringing` event (`data.direction = "inbound"`) whose row
> is written in the same transaction and broadcast after the commit; (b) the call context
> (`build_call_context`, trace context injected) saved to Redis within 1.5 s, best effort (a timeout or Redis
> error logs `telnyx.inbound_context_skipped`; the media worker rebuilds it from Postgres, specs/07 §3). There is
> no dynamic-data/memory/start-node prefetch yet: those sources push `inject_context` when they land (phases 08 and
> 12); (c) `CallWorkflow` started in inbound mode (`CallWorkflowInput(direction="inbound", max_duration_s=…)`,
> `REJECT_DUPLICATE`, 1.0 s start deadline / 0.8 s RPC timeout); (d) `answer` through the number's account with
> `client_state` (`primary` leg), `command_id=answer-{call_id}`, the §6 stream parameters (URL and token as
> outbound, `va_telnyx.streams`), and `record-from-answer`/`dual`/`wav` when `record` is set. A
> `CallAlreadyEnded` answer (the caller hung up) is not a failure: `telnyx.inbound_caller_gone_before_answer` is
> logged and the carrier events end the call. Any other failure after (a) committed, or the budget running out
> (`InboundAcceptTimeoutError`), moves the call `ringing → failed` with `end_reason='error'`,
> `error_message` "The inbound call could not be answered.", `queue_status='call_error'`, plus a `call.ended`
> event, terminates the workflow if it started (reason `inbound_accept_failed`), then re-raises: the router
> releases the slot and refuses `accept_failed` (fallback transfer or reject). Cleanup errors are logged
> (`telnyx.inbound_cleanup_failed`) and never mask the refusal. The `call.initiated` processor budget is 5 s (3 s
> accept + cleanup + refusal), the default stays 2 s. A redelivered `call.initiated` after the accept resolves to
> the primary leg through the carrier handler (no second call or answer). Ring-to-answer is measured from the
> router's start to the `answer` response (`va_inbound_answer_ms`, specs/27 §3.2; logged as `ring_to_answer_ms`
> in `telnyx.inbound_answered`, together with `stages_ms`: the clock time of `route` (number lookup, routing
> facts, slot), `persist`, `context` (event broadcast + context save), `start` (workflow), and `answer`, which add
> up to `ring_to_answer_ms`; Decision 2026-11-04, P08-T00: a slow answer must name its step). A timed-out or
> failed accept logs the stages it finished before its cleanup as `telnyx.inbound_accept_stages` (`reason` =
> `timeout` or the exception type, `stages_ms`, `last_stage`, `elapsed_ms`); the router keeps owning
> `telnyx.inbound_accept_failed`. The edge mixes in `StreamTokenSettings` and the platform model ids
> (`LLM_DIALOGUE_MODEL`/`LLM_ROUTER_MODEL` scoped to the edge); the lifespan refuses to start without
> `STREAM_TOKEN_SECRET` when it builds the default acceptor.

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
Parsing rules and outbound builders: the specs/06 §4.1 Decision P03-T09 rule 4.

> Decision (2026-11-02): Telnyx media transport (P05-T08). `va_media.transport.TelnyxMediaTransport(socket, start,
> on_rate_limited=None)` implements the pipeline `Transport` (specs/07 §16) over the session's media socket after
> `start`. Inbound: `media` → `InboundAudio(chunk, payload)` unchanged (the pipeline's `InboundPath` reorders on the
> Telnyx `chunk`), `dtmf` → `InboundDtmf`, `mark` → `MarkEcho`; `stop` or a peer close ends the event iterator
> (`stop_received` / `closed_by_peer`); `connected`, a repeated `start`, and unknown events are ignored. Dropped and
> counted (one warning per kind per call, never the payload): a frame whose `track` is present and not `inbound`, an
> empty payload, an odd-length payload on an L16 leg (`invalid_media`). A malformed message is logged
> (`media.protocol_error`, detail only) and skipped. `error` frames never end the stream: `100005` calls
> `on_rate_limited` (the pipeline wires `OutboundAudio.rate_limited`), `100003`/`100004` log at error level, others at
> warning. Outbound `media`/`mark`/`clear` messages go out as text frames serialized by one lock; after `stop`, a peer
> close, or a send that found the socket closed, sends are dropped silently and counted (the outbound clock races the
> call end). The leg format comes from `start.media_format`: PCMU/8000/1 → `ULAW_8000`, L16/16000/1 → `L16_16000`;
> anything else is `UnsupportedMediaFormatError` (the session fails with 1011). `MediaFrame` gained
> `track: str | None`. No transport metrics: reorder/gap metrics stay with the pipeline.

## 9. DTMF

- Inbound: from media WS `dtmf` and webhook `call.dtmf.received` (deduplicated). Handled by the engine (`07` §13).
- Outbound: `send_dtmf` for `precall_dtmf_sequence` (split on `w`: each `w` → 500 ms `asyncio.sleep` between
  `send_dtmf` calls of contiguous digit groups) and for Press Button nodes / `press_button` tool.

> Decision (2026-11-02): the media worker sends both (specs/07 §11 Decision P05-T10b) through its own
> `TelnyxClientFactory` (`va_telnyx.settings.telnyx_factory`, shared with the worker; `TELNYX_API_BASE`/
> `TELNYX_API_KEY` via `TelnyxApiSettings`, BYO keys via `va_db.telnyx_keys.TelnyxKeyResolver`); `W` in a
> `press_button` sequence is a 1 s pause.

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

> Decision (2026-11-02): cold transfer (P05-T13). The media worker runs a `transfer_call` action through
> `va_media.transfer.ColdTransfer` (reached via `va_media.actions.MediaActions`); the Brain yields the transfer line
> (`SpeakStatic`, not interruptible) before the action, and the pipeline hands an action over only after the
> line's playback completed (mark echo), which satisfies step 1.
> 1. **Status and event:** `in_progress` → `transferring` first (`DbTransferStatus`, guarded transition); a call in
>    another status is `not_active` and nothing is sent; a row that cannot be written fails the transfer. Then
>    `call.transfer_started` (`data = {"type": "cold", "target": <E.164>}`, message "Transfer started."). The
>    target is never logged.
> 2. **Command:** `transfer(primary_ccid)` with `to` = target, `from` = the org's `transfer_caller_id_mode`:
>    `agent_number` = our number (outbound `from_number`, inbound/web `to_number`), `original_caller` = the party
>    being transferred (outbound `to_number`, inbound/web `from_number`); `timeout_secs = 30`; `time_limit_secs`
>    = the seconds left until answer (or now) + `max_duration`, rounded up, floored at 30 and capped at 14 400;
>    `target_leg_client_state` = our `client_state` with `leg = "transfer_target"` (the edge attributes the target's
>    webhooks with it); `client_state` = the primary leg's; `command_id = command_id("transfer", call_id,
>    "primary", n)` with `n` = the call's 0-based transfer count (a retried command dedupes, the next transfer
>    does not).
> 3. **Outcome:** the carrier watch queues every `transfer_target` carrier event; the transfer waits up to 30 s +
>    10 s for the first `call.bridged` (`bridged`) or `call.hangup` (`target_hangup`) of that leg; events of an
>    earlier attempt are drained before sending. Nothing in time → `timeout`; a Telnyx error → `command_failed`.
>    Every failure moves the call back to `in_progress` (specs/05 §8 Decision P05-T13); the pipeline then says
>    the org's `transfer_failed_message` (the `CallPipeline.say` seam, interruptible) and the Brain gets it as an
>    assistant message (`TaskBrain.on_transfer_failed`), so the conversation continues.
> 4. **Stream (deviation from steps 4 and 6):** the primary stream stays up while the target rings (the pipeline
>    waits inside the action: no replies, no no-input timer). Stopping it before the bridge and restarting it on
>    failure would be a new media WebSocket attach (owner conflict, audible gap). After `bridged` the pipeline is
>    taken over with `transferred` (no message) and the end-of-call routine sends `streaming_stop`
>    (`command_id("streaming_stop", call_id, "primary")`) instead of a hangup (specs/07 §18 Decision P05-T13). On
>    failure nothing has to restart. `stream_post_transfer_transcript=true` behaves like `false` until the listen
>    stream exists (P12-T08); it is logged `media.post_transfer_transcript_deferred`.
> 5. **Events and billing:** `call.transferred` is emitted by the edge on the target's `call.bridged` (specs/19 §2
>    table; the media worker does not repeat it), which also records `transferred`, `transferred_to` and the
>    carrier state's `transfer_started_at`. The edge records `completed`/`transferred` with connected and transfer
>    seconds on the primary leg's hangup; `CallWorkflow` waits for that hangup (specs/01 §2.1 Decision P05-T13).
>    A late or duplicate primary `call.answered` can never move a `transferring` call back to `in_progress` (the
>    edge enters `in_progress` only from `dialing`/`ringing`).
> 6. The `transfer` and `takeover` control messages (specs/07 §17) stay unacknowledged until the control endpoint
>    exists; `ColdTransfer.run(target)` is the entry point they will reuse.

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

## 11. Recording copy flow (activity `store_recording`)

1. `call.recording.saved` payload is parsed tolerant of unknown fields: `recording_urls.wav` (preferred) or
   `recording_urls.mp3`, `public_recording_urls.*`, `channels` (`single`/`dual`), `recording_started_at`,
   `recording_ended_at`, `recording_id` (optional). Missing URLs → `call.error` `recording_failed`.
2. Download immediately (URLs are short-lived presigned links) with streaming to a temp file (max 2 GB).
3. Upload to `s3://{S3_BUCKET_RECORDINGS}/{org_id}/{yyyy}/{mm}/{call_id}.{h}.wav` (key per recording: Decision
   P05-T14 rule 1) with SSE-KMS (AWS; see the Decision below for MinIO), metadata `call_id`, `org_id`.
4. Compute duration from the WAV header (stdlib `wave`); set `calls.recording_s3_key`, `recording_duration_ms`; emit
   `call.recording_ready`.
5. Post-call creates `mixed.wav`, `caller.wav`, `agent.wav` (left = caller/inbound, right = agent/outbound — verified
   by live smoke test `test_recording_channel_order`).

> Decision (2026-11-02, P05-T14): recording storage.
> 1. Keys. The raw copy is `{org_id}/{yyyy}/{mm}/{call_id}.wav` in `S3_BUCKET_RECORDINGS` (specs/18 §1 step 3; `yyyy/mm` =
>    UTC month of `calls.created_at`); every other per-call artifact lives under `{org_id}/{yyyy}/{mm}/{call_id}/`
>    (`mixed.wav`, `caller.wav`, `agent.wav`, `transcript_retranscribed.json`). Builders: `va_common.storage.recording_key`
>    and `recording_variant_key`. This replaces the earlier `recordings/{org_id}/{call_id}/raw.wav` text. Amended
>    2026-11-04 (P08-FIX-1): the copy of a carrier recording is `{org_id}/{yyyy}/{mm}/{call_id}.{h}.wav`, `h` = the
>    first 16 hex digits of the SHA-256 of the recording's identity (the rule 4 lock owner `rec:<recording_id>` /
>    `evt:<telnyx_event_id>`; builder `va_common.storage.recording_copy_key`). `calls.recording_s3_key` names it, and
>    every reader takes the key from the row, never rebuilds it.
> 2. Storage client. `va_common.storage.open_object_store(S3Settings)` (aiobotocore S3, timeouts 5 s connect / 60 s
>    read, 3 standard retries, SigV4, path-style when `S3_ENDPOINT_URL` is set) yields an `ObjectStore` (`upload_file`
>    multipart, `exists`, `delete`, `presign_get`). SSE: `KMS_PROVIDER=aws` → `aws:kms` with `AWS_KMS_KEY_ID`; the
>    local provider (MinIO in development and tests) sends no SSE header because the development MinIO has no KMS.
>    Presigned URLs live at most 7 days (SigV4); `RECORDING_URL_TTL_S` = 7 days (Call Object `recording_url`),
>    `PLAYBACK_URL_TTL_S` = 15 min (specs/05 §7.5); `signed_url(store, bucket, key, now=, ttl_s=)` returns the URL with
>    its `expires_at`.
> 3. Delivery. The edge signals `call.recording.saved` to `CallWorkflow` (unchanged) **and** to `postcall-<id>`
>    (signal `recording_saved`, the JSON `CarrierEvent`); a missing post-call workflow is logged at debug only, because
>    the recording usually arrives before `CallWorkflow` starts the child. `PostCallWorkflow` (P05-T18) must therefore
>    look for an already stored `call.recording.saved` (`telnyx_events`) when it starts, before it waits for the signal.
> 4. Activity `store_recording(StoreRecordingInput(call_id, org_id, event))` (post-call task queue; 5 min, 5 attempts,
>    2 s doubling to 30 s): a call whose `recording_s3_key` is set returns `already_stored` (nothing uploaded or
>    emitted); the URL is `recording_urls.wav`, else `public_recording_urls.wav` (an MP3 is never copied: the platform
>    always records WAV); the download streams to a private scratch directory (2 GiB cap) that is always removed;
>    the guarded `UPDATE … WHERE recording_s3_key IS NULL` and `call.recording_ready` (`data: duration_ms, channels,
>    recording_offset_ms` = recording start − `answered_at`, `null` when either is unknown) commit together. Failures:
>    `invalid_payload`, `no_wav_url`, `download_failed` (a 4xx other than 429), `too_large`, `invalid_wav` return
>    `failed` with `call.error` `{code: recording_failed, detail}` (level `warning`) and are not retried; a missing call
>    returns `failed`/`call_not_found` without an event; 429/5xx/transport download failures (`TelnyxUnavailable`) and
>    object-store failures (`StorageUnavailable`) raise retryable errors. Concurrency (amended 2026-11-02, review of
>    P05-T14): a run takes the per-call copy lock `call:{id}:reccopy` (specs/03 §11; owner = `rec:<recording_id>`,
>    else `evt:<telnyx_event_id>`; TTL = activity timeout + 1 min; `SET` if free or already held by the same owner,
>    compare-and-delete release) after the first key check. A different owner gets the retryable `RecordingBusy`
>    with `next_retry_delay` = the lock's remaining `PTTL` + 1 s (1 s without a TTL), so the 5 attempts outlast a live
>    holder or a crashed one whose lock only expires with its TTL; the same owner (a redelivery or a crashed earlier
>    attempt) proceeds, because it uploads the same bytes to the same key. Under the lock the key is read again (a
>    previous holder may have just committed; this copy would be wasted work).
>    Before recording `recording_failed` the key is read once more: when another copy stored the call's recording
>    meanwhile (its carrier delete makes this run's download 404), the run returns `already_stored` and records
>    nothing. Redis errors fail open: the copy proceeds without the lock (`recording.lock_unavailable`; an outage
>    longer than the retry budget must not cost the copy); a failed release is logged and left to the TTL. Because
>    every recording uploads to its own key (rule 1), the guarded `UPDATE` always names the bytes it describes: a run
>    whose `UPDATE` finds another key set returns `already_stored` with the row's key and deletes its own
>    just-uploaded object (a failed delete is logged as `recording.orphan_delete_failed`), unless the row's key equals
>    its own (the same recording copied twice: that object is the stored one); it never deletes its carrier copy. The
>    lock therefore only saves duplicate work and is not needed for correctness.
> Decision (2026-11-04, P08-FIX-1): per-recording object keys and the lock-aware busy retry (rules 1 and 4) close
>    the review findings that a fail-open copy of two recordings could leave the object holding the other run's bytes
>    (and the winner then deleting its carrier copy), and that the ~30 s `RecordingBusy` retry budget ended long
>    before a crashed holder's 6 min lock expired.
> 5. Carrier copy. After the commit, when the org's `delete_carrier_recordings` (default true) holds and the event
>    carries `recording_id`, the activity calls `DELETE /v2/recordings/{id}` with the call's account; a failure is logged
>    (`recording.carrier_delete_failed`) and never fails the copy. An `already_stored` run deletes nothing (it may be a
>    second recording of the call). FakeTelnyx serves `DELETE /v2/recordings/{id}` (specs/30 §3.2). A call stores one
>    recording (the platform records once, from answer, on the primary leg); a later different recording is not
>    copied, and its carrier copy is kept on purpose: a recording that was never copied is never deleted. The
>    `recording.already_stored` log line carries its `recording_id`.
> 6. Logs never contain the presigned URL (the `httpx` logger defaults to WARNING, specs/27 §1 Decision 2026-11-02).
> 7. Expired links (amended 2026-11-02, review of P05-T14). The webhook's presigned links live a few minutes, and
>    `PostCallWorkflow` may replay a stored event later (rule 3). A `403` or `404` download of an event that carries
>    `recording_id` fetches `GET /v2/recordings/{id}` with the call's account (`TelnyxClient.get_recording`) and
>    downloads its `download_urls.wav` once (`recording.url_refreshed`). 429/5xx/transport failures of the lookup are
>    retryable (`TelnyxUnavailable`); a `404`, another 4xx, an unknown BYO account, a missing `wav` link, or a second
>    `403`/`404` is `download_failed`. Without `recording_id` a `403`/`404` is `download_failed` with no lookup.

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

> Decision (2026-11-01): bootstrap library and script (P03-T15a).
> 1. The logic lives in `va_telnyx.bootstrap.bootstrap_account(client, name=, webhooks=)` (used by the script now and
>    by the `BootstrapTelnyxAccount` activity later). It only talks to a `TelnyxClient`; it never touches the
>    database or the environment.
> 2. Names: `platform_resource_name(va_env)` = `voiceagents-{va_env}`, `org_resource_name(org_id)` =
>    `voiceagents-org-{org_id}`. `webhook_urls(edge_public_host, telnyx_account_id=None)` gives
>    `https://{host}/telnyx/voice[/{id}]` and `https://{host}/telnyx/messaging[/{id}]` (always https, also for a local
>    host; the simulator only records the URL).
> 3. Names match exactly. The list filters are `contains` (apps, voice profiles) or provider-side, so results are
>    filtered to `name == wanted`. With several exact matches the first in list order is used and
>    `telnyx_bootstrap.duplicate_resources` (kind, name, count, chosen_id) is logged at warning level; nothing is
>    deleted.
> 4. Order: voice profile; app (created with that profile, or one PATCH carrying only the drifted fields among
>    `webhook_event_url` and `outbound.outbound_voice_profile_id`); messaging profile (created, or PATCHed when
>    `webhook_url` differs). Nothing is sent when nothing differs.
> 5. Result `BootstrapResult(call_control_app_id, outbound_voice_profile_id, messaging_profile_id, created, updated)`
>    with kinds `outbound_voice_profile`, `call_control_app`, `messaging_profile`; `env_values()` returns the three
>    `TELNYX_*_ID` keys in step 4 order.
> 6. Script `scripts/telnyx_bootstrap.py [--env-file PATH] [--write-env] [--json]` reads `VA_ENV`, `TELNYX_API_BASE`,
>    `TELNYX_API_KEY`, `EDGE_PUBLIC_HOST`, and `REDIS_URL` (rate limiter, fails open when Redis is down) from the
>    environment plus the env file (environment wins). Without `--env-file` it uses `.env` when present; an explicit
>    missing file or invalid settings exit 2. stdout carries only the result: three `KEY=value` lines, or one JSON
>    object with the three keys plus `created`/`updated`; logs go to stderr. A `TelnyxError` exits 1 after logging
>    `telnyx_bootstrap.failed` (error type, operation, HTTP status, Telnyx error codes; never the key).
> 7. `--write-env` updates the three keys in place: an existing `KEY=...` line keeps its trailing comment and line
>    ending, a missing key is appended, every other line stays byte for byte, the file mode is kept, and the file is
>    replaced through a temp file plus `os.replace`; an unchanged file is not written.

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
