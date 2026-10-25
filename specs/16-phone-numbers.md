# 16 — Phone Numbers, Inbound Configuration, Number Pools, Local Presence, Bring Your Own Telnyx

Package ownership: REST handlers in `apps/api/src/va_api/routers/numbers.py`, `inbound.py`, `number_pools.py`,
`accounts.py`; domain logic in `packages/va_domain` (schemas) and `packages/va_telnyx` (Telnyx REST calls, see
`06-telephony-telnyx.md`); workflows in `packages/va_workflows/src/va_workflows/numbers.py`
(`NumberOrderWorkflow`, `NumberReleaseWorkflow`, `TelnyxAccountBootstrapWorkflow`, `MonthlyNumberBillingWorkflow`).
Inbound call routing at runtime lives in `apps/telephony_edge/src/va_edge/inbound.py` (see `01-architecture.md` §2.2).

All endpoints in this file use the **legacy** envelope (`31-error-catalog.md` §1) unless stated otherwise.
`/v1/number_pools*` uses the **envelope** format.

## 1. Number lifecycle

```
search (GET /v1/numbers/available)  ──►  purchase (POST /numbers/purchase)  ──►  phone_numbers.status = 'pending'
     NumberOrderWorkflow: Telnyx POST /v2/number_orders → poll until success|failure (≤ 10 min)
         success → PATCH /v2/phone_numbers/{id} {connection_id} ; if sms requested → PATCH /v2/phone_numbers/{id}/messaging
                 → status='active', purchased_at=now(), usage_event phone_number_month (first month, prorated 100 %)
         failure → status='released', number_orders.status='failure', refund hold, event number.purchased not sent
release (POST /v1/inbound/{phone_number}/release) → status='releasing' → NumberReleaseWorkflow:
         DELETE /v2/phone_numbers/{telnyx_phone_number_id} → status='released', released_at=now(), deleted_at=now()
```

Numbers from a BYO Telnyx account (§7) skip purchase; they enter via `POST /v1/inbound/insert` and are never
deleted from the customer's Telnyx account by release (release only detaches them: `status='released'`, Telnyx
`connection_id` reset to the value recorded at import time in `inbound_config.byo_original_connection_id`).

## 2. Searching available numbers — `GET /v1/numbers/available`

Proxy to Telnyx `GET /v2/available_phone_numbers` (verified filter names, Telnyx API reference "List available phone numbers").

| Query param (ours) | Telnyx filter | Type / default | Validation |
|---|---|---|---|
| `country_code` | `filter[country_code]` | string, default `US` | ISO-3166 alpha-2, must be in `SUPPORTED_NUMBER_COUNTRIES` = `US, CA, GB, AU, IE, DE, FR, ES, NL, SE, MX, BR` |
| `area_code` | `filter[national_destination_code]` | string | 3 digits for US/CA; 1–5 digits elsewhere |
| `locality` | `filter[locality]` | string | ≤ 64 chars |
| `administrative_area` | `filter[administrative_area]` | string | 2-letter state/province for US/CA |
| `contains` | `filter[phone_number][contains]` | string | digits only, ≤ 10 |
| `starts_with` | `filter[phone_number][starts_with]` | string | digits only |
| `ends_with` | `filter[phone_number][ends_with]` | string | digits only |
| `number_type` | `filter[phone_number_type]` | `local` (default) \| `toll_free` \| `mobile` \| `national` | |
| `features` | `filter[features][]` | csv of `voice,sms,mms`; default `voice` | always includes `voice` |
| `limit` | `filter[limit]` | int 1–50, default 10 | |
| `best_effort` | `filter[best_effort]` | bool default false | US/CA only |
| `exclude_held` | `filter[exclude_held_numbers]` | always `true` (not exposed) | |

Response (legacy family):

```json
{
  "status": "success",
  "numbers": [
    {
      "phone_number": "+14155550123",
      "country_code": "US",
      "number_type": "local",
      "locality": "San Francisco",
      "administrative_area": "CA",
      "rate_center": "SNFC CNTL",
      "features": ["voice", "sms", "mms"],
      "best_effort": false,
      "monthly_price": 1.5,
      "upfront_price": 0.0,
      "currency": "USD"
    }
  ]
}
```

Mapping: `locality`/`administrative_area`/`rate_center` from `region_information[]` entries whose `region_type` is
`location`, `state`, `rate_center`; `features` from `features[].name` filtered to `voice|sms|mms`.
**Customer price** = `plans`-independent platform number price (§8), not Telnyx's `cost_information` (which is
stored in `number_orders` for COGS only). Empty result → 404 `NO_NUMBERS_FOUND`. Telnyx error → 502 `CARRIER_ERROR`.
Results cached in Redis `numsearch:{sha256(normalized query)}` for 60 s (key added to the Redis keyspace, see
Shared-contract additions).

## 3. Purchasing — `POST /numbers/purchase`

Bland-compatible body:

| Field | Type | Required | Default | Semantics |
|---|---|---|---|---|
| `area_code` | string | no | — | Used when `phone_number` absent: search `limit=5` with this NDC and buy the first result. |
| `country_code` | string | no | `"US"` | |
| `phone_number` | string E.164 | no | — | Exact number; must have appeared in a search within the last 24 h (Telnyx requirement) — if not in our search cache we perform a `contains` search for its national number before ordering. |
| `number_type` | string | no | `"local"` | ours |
| `sms` | boolean | no | `false` | ours; also enable SMS (adds messaging profile, `capabilities` += `sms`,`mms`) |
| `label` | string | no | null | ours |
| `prompt`, `pathway_id`, `voice`, … | — | no | — | Any InboundConfig field (§4) may be passed to configure at purchase |
| `encrypted_key` | string | no | — | BYO Telnyx: purchase into the customer's account (§7.5) |

If neither `phone_number` nor `area_code` is given, buy any `local` number in `country_code` (search `limit=1`).

Synchronous behaviour (the endpoint waits up to 8 s for the order; Telnyx US local orders usually complete in
1–3 s):

1. Authorize: role `owner|admin|developer` or API key scope `numbers:write` (see `04-auth-tenancy-rbac.md`).
2. Check plan: number count limit `plans.features` contains `numbers_unlimited` or org has < 25 active numbers
   (`start` plan cap: 5, `build`: 50, `scale`: 200, `enterprise`: unlimited — stored in `PLAN_NUMBER_CAPS` constant in
   `va_billing.plans`; see `24-billing-and-limits.md` §2).
3. Create credit hold = first-month price (`24-billing-and-limits.md` §5); `INSUFFICIENT_CREDITS` if it fails.
4. Insert `number_orders(status='pending', requested_numbers=[e164])` and `phone_numbers(status='pending')`.
5. Start `NumberOrderWorkflow(order_id)`; wait on a workflow query `get_status` up to 8 s.
6. Success → 200:

```json
{"status":"success","phone_number":"+14155550123","inbound":{…InboundConfig…},"order_id":"…"}
```

   Still pending after 8 s → 202 `{"status":"success","phone_number":"+1…","order_status":"pending","order_id":"…"}`
   (the number becomes usable when `number.purchased` fires). Failure → 409 `NUMBER_UNAVAILABLE` or 502 `CARRIER_ERROR`.

### 3.1 `NumberOrderWorkflow(order_id)` activities

1. `telnyx_create_number_order`: `POST /v2/number_orders` body
   `{"phone_numbers":[{"phone_number":e164}],"connection_id":<call control app id>,"messaging_profile_id":<id if sms>,"customer_reference":"org:{org_id}:order:{order_id}"}`.
   Idempotency: `customer_reference` is unique per order; before creating, `GET /v2/number_orders?filter[customer_reference]=…`
   and reuse an existing order (activity retry safety).
2. `telnyx_poll_number_order`: `GET /v2/number_orders/{id}` every 2 s (workflow timer) until `status ∈ {success, failure, cancelled, deleted}`;
   timeout 10 min ⇒ treated as failure with reason `order_timeout`.
3. On `success`: `GET /v2/phone_numbers?filter[phone_number]=e164` to obtain `telnyx_phone_number_id`;
   `PATCH /v2/phone_numbers/{id}` `{"connection_id": app_id, "tags":["va","org:{org_id}"]}`; if SMS:
   `PATCH /v2/phone_numbers/{id}/messaging` `{"messaging_profile_id": profile_id}`; if US long code and SMS:
   assign to the org's active 10DLC campaign when present (`21-messaging-sms.md` §6).
4. DB: `phone_numbers.status='active'`, `telnyx_phone_number_id`, `purchased_at`, `monthly_cost_micros` (our price),
   capture hold as ledger `usage` with usage_event `phone_number_month` (`reference_type='phone_number'`,
   `idempotency_key='num:{id}:{yyyymm}'`), `number_orders.status='success'`.
5. Emit `number.purchased` (data `{phone_number, phone_number_id, order_id}`).
6. On failure: release hold, `phone_numbers.status='released'`, `number_orders.status='failure'`, `error`, emit the
   system event `number.order_failed` (see Shared-contract additions).

## 4. InboundConfig (normative model, stored in `phone_numbers.inbound_config`)

`va_domain.numbers.InboundConfig` (Pydantic, `extra="forbid"`). Every field optional; the inbound call's resolved
config is built by the same precedence engine as Send Call (`05-calls-api.md`) with the inbound config as the
request body and `phone_number` = caller.

| Field | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string ≤ 20 000 | null | Same as Send Call `task` (Bland inbound uses `prompt`). |
| `pathway_id` | uuid | null | |
| `pathway_version` | int \| `"production"` \| `"staging"` | `"production"` | |
| `persona_id` | uuid | null | |
| `persona_version` | `"production"` \| `"draft"` | `"production"` | |
| `agent_id` | uuid | null | |
| `agent_version` | string | null | same selector grammar as Send Call |
| `voice` | string (name or uuid) | org default voice | |
| `first_sentence` | string ≤ 1 000 | null | |
| `wait_for_greeting` | bool | false | |
| `background_track` | `null`\|`"office"`\|`"cafe"`\|`"restaurant"`\|`"none"` | null (phone static) | |
| `noise_cancellation` | bool | true | |
| `block_interruptions` | bool | false | |
| `interruption_threshold` | int ms 50–5000 | null (adaptive) | legacy |
| `interruptibility` | int 0–4 | 2 | |
| `model` | string | org `llm.dialogue_model` | LLM model id override (must be allowed by org provider config, `10-llm-gateway.md`) |
| `temperature` | float 0–1 | 0.7 | |
| `language` | string | `"en-US"` | language code (see `08-stt-elevenlabs.md`) |
| `timezone` | IANA tz | `"America/Los_Angeles"` | |
| `request_data` | object | {} | |
| `metadata` | object | {} | |
| `tools` | array of `TL-…`/`KB-…` ids or inline tool objects | [] | |
| `dynamic_data` | array DynamicDataItem | [] | `13-tools-and-dynamic-data.md` |
| `transfer_phone_number` | E.164 | null | |
| `transfer_list` | object `{label: E.164}` incl. `default` | {} | |
| `warm_transfer` | object | null | same as Send Call |
| `fallback_number` | E.164 | null | Used for maintenance window, suspended org, out of credits, pipeline failure. |
| `maintenance` | `{enabled: bool, starts_at?: ISO, ends_at?: ISO, message?: string}` | `{enabled:false}` | Active when `enabled` and now ∈ [starts_at, ends_at] (open-ended if a bound is missing). If `message` is set and no `fallback_number`: answer, play `message` rendered by ElevenLabs TTS (cached), then hang up. |
| `memory_id` | uuid | null | Force a specific memory record; otherwise memory resolves by caller contact. |
| `memory_enabled` | bool | false | |
| `pronunciation_guide` | array `{word, pronunciation, case_sensitive?, spaced?}` | [] | |
| `keywords` | array of `"term"` or `"term:boost"` | [] | |
| `summary_prompt` | string ≤ 2 000 | null | |
| `analysis_schema` | object | null | `18-post-call-analysis.md` |
| `dispositions` | string[] | null | |
| `citation_schema_ids` | uuid[] | [] | |
| `post_call_evals` | object | null | |
| `webhook` | URL | null | |
| `webhook_events` | string[] | [] | |
| `record` | bool | false | |
| `max_duration` | number minutes 1–240 | 30 | |
| `guard_rails` | array GuardRail | [] | `23-guardrails-and-compliance.md` |
| `voicemail` | object | null | rarely used inbound, accepted for parity |
| `ignore_button_press` | bool | false | |
| `hd_audio` | bool | false | |
| `allowed_callers` | E.164[] | [] | ours: when non-empty only these callers are answered; others → `reject` |
| `blocked_callers` | E.164[] | [] | ours: rejected with `CALL_REJECTED` |
| `byo_original_connection_id` | string | null | internal; not settable via API |

Runner rule: at most one of `prompt` / `pathway_id` / `agent_id`; `persona_id` may combine with
`pathway_id` (same precedence as Send Call). A number with no runner answers with the org's
`settings.default_inbound_prompt` (default "You are a helpful receptionist for {{org_name}}. Take a message.").

## 5. Inbound endpoints

### 5.1 `GET /v1/inbound`

Query: `limit` (1–1000, default 100), `label?`, `capability?` (`voice|sms`). Response:

```json
{"status":"success","inbound_numbers":[{"phone_number":"+1…","label":"Support","capabilities":["voice","sms"],
 "number_type":"local","created_at":"…","purchased_at":"…","monthly_price":1.5,"byo":false,
 "prompt":"…","pathway_id":null, "…": "all InboundConfig fields flattened"}]}
```

### 5.2 `GET /v1/inbound/{phone_number}`

`phone_number` URL-encoded E.164 (`%2B14155550123`) or bare digits (normalized). Response:
`{"status":"success", "phone_number":…, "label":…, "capabilities":…, …InboundConfig flattened}`; 404 `NOT_FOUND`.

### 5.3 `POST /v1/inbound/{phone_number}` — update configuration

Body: any subset of InboundConfig. Semantics: **merge** at top level (fields present replace, absent keep);
`null` clears a field to default. Validation as Send Call (runner conflicts → `RUNNER_CONFLICT`, unknown voice →
`VOICE_NOT_FOUND`, unknown tool → `TOOL_NOT_FOUND`, pathway version absent → `PATHWAY_VERSION_NOT_FOUND`,
webhook SSRF → `WEBHOOK_URL_REJECTED`). Writes `audit_logs` (`inbound.update`, before/after). Response:
`{"status":"success","message":"Inbound number updated.","inbound":{…}}`.

### 5.4 `POST /inbound/update_label`

Body `{"phone_number": "+1…", "label": "Support line" | null}` (label ≤ 64 chars). Response
`{"status":"success","phone_number":"+1…","label":"Support line"}`. Also exposed as `POST /v1/inbound/update_label`.

### 5.5 `POST /v1/inbound/{phone_number}/release`

Role `owner|admin`. Fails `CONFLICT` if the number is a member of a pool with `pool.strategy` and it is the last
member referenced by a running batch (`17-batches-and-scheduling.md`) — message "Number is used by running batch {id}".
Starts `NumberReleaseWorkflow`. Response `{"status":"success","message":"Number release started."}`. No refund of
the current month.

### 5.6 `POST /v1/inbound/insert` (BYO Telnyx import) and `POST /v1/inbound/delete`

`insert` body: `{"encrypted_key":"…","numbers":["+1…", …]}` (≤ 100). For each: verify in the customer's account
(`GET /v2/phone_numbers?filter[phone_number]=…` with their key), record original `connection_id`, PATCH it to their
bootstrapped Call Control App (§7.3), create `phone_numbers(telnyx_account_id=…, status='active', monthly_cost_micros=0)`.
Response `{"status":"success","inserted":["+1…"],"failed":[{"phone_number":"+1…","error":"NOT_FOUND_IN_ACCOUNT"}]}`.
`delete` body: `{"encrypted_key":"…","phone_number":"+1…"}` → detach as in §1. Response `{"status":"success"}`.

### 5.7 SMS capability — `POST /v1/inbound/{phone_number}/sms`

Body `{"enabled": true, "sms_config": {…SmsNumberConfig…}}` (see `21-messaging-sms.md` §3). Enabling: Telnyx
`PATCH /v2/phone_numbers/{id}/messaging {"messaging_profile_id": …}`; US long code requires an active 10DLC campaign
(else 400 `SMS_NOT_ENABLED` with message "Register a 10DLC campaign before enabling SMS on US local numbers.");
toll-free requires verified toll-free status. Disabling: `messaging_profile_id: null`, remove `sms`,`mms` from capabilities.

## 6. Number pools and dialing strategy

### 6.1 Pool API (`/v1/number_pools`, envelope)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/v1/number_pools` | — | list with `member_count` |
| POST | `/v1/number_pools` | `{name, strategy?, max_calls_per_number_per_day?, phone_numbers?: string[]}` | Enterprise feature (`FEATURE_NOT_IN_PLAN` otherwise) |
| GET | `/v1/number_pools/{id}` | — | includes members with `last_used_at`, `calls_today` |
| PATCH | `/v1/number_pools/{id}` | `{name?, strategy?, max_calls_per_number_per_day?}` | |
| DELETE | `/v1/number_pools/{id}` | — | 409 if referenced by a non-terminal batch |
| POST | `/v1/number_pools/{id}/members` | `{phone_numbers: string[]}` | numbers must be active, voice-capable, same org |
| DELETE | `/v1/number_pools/{id}/members/{phone_number}` | — | |

### 6.2 `dialing_strategy` on Send Call

- Absent and `from` absent → platform default caller ID: org setting `default_from_number` if set, else a number from
  the platform shared pool (`PLATFORM_POOL_ORG_ID` system org) chosen by `local_match`.
- `from` set → must be an active voice number of the org (or the BYO account selected by `encrypted_key`); else 400
  `INVALID_INPUT` field `from`.
- `{"type":"local"}` → choose from **all** org active voice numbers using `local_match`; add-on feature flag `local_presence`.
- `{"type":"custom_pooling","pool_id":"…"}` → choose from the pool using the pool's strategy.

### 6.3 Selection algorithms (`va_telephony` code lives in `packages/va_telnyx/src/va_telnyx/caller_id.py`)

Inputs: candidate numbers `C` (with `last_used_at`, `calls_today`, `calls_today_date`), destination `d`.
Pre-filter: drop numbers whose `calls_today ≥ max_calls_per_number_per_day` when `calls_today_date == today(UTC)`.
If empty after filter → use the unfiltered set (never block a call because of rotation caps) and emit `call.error`
with level `warning` and `data.code = "POOL_EXHAUSTED_FALLBACK"`.

- `round_robin`: members sorted by `e164` ascending; index = `INCR pool:{pool_id}:rr` (Redis, no TTL) − 1 modulo
  member count; pick that member (skipping filtered-out members by advancing the index).
- `least_recently_used`: order by `last_used_at NULLS FIRST, phone_number_id`; pick first.
- `random`: uniform `secrets.choice`.
- `local_match`:
  1. Parse `d` with `phonenumbers`; if country ≠ US/CA → choose same-country numbers by LRU, else LRU of all.
  2. `npa = national_number[:3]`. Exact NPA matches → LRU among them.
  3. Else **overlay/neighbour table**: `packages/va_telnyx/src/va_telnyx/data/npa_neighbors.json` maps each NPA to
     its ordered list of NPAs in the same state sorted by geographic distance of NPA centroids (generated once by
     `scripts/generate_npa_neighbors.py` from the NANPA public area-code dataset CSV committed at
     `packages/va_telnyx/src/va_telnyx/data/nanpa_area_codes.csv` with columns `npa,state,lat,lon,overlay_of`).
     Walk the list; first NPA with candidates → LRU among them.
  4. Else any number in the same state (`state` from the table) → LRU.
  5. Else LRU of all candidates.
- Update after selection (single SQL statement, `FOR UPDATE SKIP LOCKED` on the chosen member row to avoid
  hot-spotting): `last_used_at=now()`, `calls_today = CASE WHEN calls_today_date = current_date THEN calls_today+1 ELSE 1 END`,
  `calls_today_date=current_date`. For non-pool `local` strategy a synthetic in-memory candidate set is used and
  `last_used_at` is tracked in Redis hash `localpresence:{org_id}` (field e164 → unix ts).

`calls.config.dialing_strategy` and `calls.from_number` record the outcome; `local_dialing=true` in the Call Object when
strategy `local` or `custom_pooling` with `local_match`.

## 7. Bring Your Own Telnyx

### 7.1 Purpose

Customers with their own Telnyx account route calls through it (their numbers, their carrier billing, their
STIR/SHAKEN attestation). Equivalent of Bland's Twilio BYOT. Platform charges only the platform minute rate; transfer
minutes are free (`24-billing-and-limits.md` §3).

### 7.2 `POST /v1/accounts`

Body: `{"telnyx_api_key":"KEY…","telnyx_public_key":"<base64 ed25519>","label":"Prod Telnyx"}` (label default
"Telnyx account"). Steps (synchronous, ≤ 15 s; slow paths continue in `TelnyxAccountBootstrapWorkflow`):

1. Validate key: `GET /v2/balance` with their key. 401/403 → 400 `INVALID_INPUT` with field `telnyx_api_key` and
   message "Telnyx rejected this API key." Network error → 502 `CARRIER_CONNECTION_ERROR`.
2. Validate public key decodes to 32 bytes.
3. Encrypt key with envelope encryption (`28-security.md`), insert `telnyx_accounts(status='pending')`,
   generate `encrypted_key_handle = "tlx_" + base62(24 random bytes)`.
4. Start bootstrap workflow which, in the customer's account:
   - `POST /v2/outbound_voice_profiles` `{"name":"VoiceAgents","traffic_type":"conversational","service_plan":"global"}` → id.
   - `POST /v2/call_control_applications` `{"application_name":"VoiceAgents","webhook_event_url":"https://{EDGE_PUBLIC_HOST}/telnyx/voice/byo/{telnyx_account_id}","webhook_api_version":"2","outbound":{"outbound_voice_profile_id":…},"anchorsite_override":"Latency"}` → id.
   - `POST /v2/messaging_profiles` `{"name":"VoiceAgents","webhook_url":"https://{EDGE_PUBLIC_HOST}/telnyx/messaging/byo/{telnyx_account_id}","webhook_api_version":"2","whitelisted_destinations":["US","CA"]}` → id.
   - Store ids, `status='active'`. Failure → `status='error'`, `status_detail`.
5. Respond `{"status":"success","encrypted_key":"tlx_…","account_status":"active"|"pending"}`.

Webhooks from BYO accounts hit `/telnyx/voice/byo/{telnyx_account_id}`; the edge verifies them with that
account's `public_key` (not `TELNYX_PUBLIC_KEY`).

### 7.3 Using `encrypted_key`

- `POST /v1/calls` with `encrypted_key`: `from` is **required** and must be a `phone_numbers` row with
  `telnyx_account_id` = that account; dial uses their API key and their `call_control_app_id` as `connection_id`.
  Invalid/unknown/deleted handle → 400 `INVALID_ENCRYPTED_KEY` ("Invalid `encrypted_key`."). Telnyx 401 on dial →
  502 `CARRIER_CONNECTION_ERROR` ("Error in creating Telnyx connection.").
- `call_legs.telnyx_account_id` records which account owns the leg; all subsequent call commands for the leg use that
  account's key (resolved by `va_telnyx.client_for_leg`).
- Batches: `encrypted_key` may be global or per call.

### 7.4 `POST /v1/accounts/delete`

Body `{"encrypted_key":"tlx_…"}`. Fails 409 if active calls exist on the account. Detaches all its numbers (§1),
deletes the call control app and messaging profile we created (best effort; failures logged), sets
`status='deleted'`, wipes `api_key_enc`. Response `{"status":"success","message":"Account deleted."}`.

### 7.5 Purchasing into a BYO account

`POST /numbers/purchase` with `encrypted_key`: identical workflow using their key; `monthly_cost_micros=0` (they pay
Telnyx directly).

### 7.6 `GET /v1/accounts`

Lists accounts: `{"status":"success","accounts":[{"encrypted_key":"tlx_…","label":…,"status":…,"number_count":n,"created_at":…}]}`.

## 8. Monthly number billing

- Platform price per number per month: local US/CA `$1.50`, toll-free `$2.50`, mobile `$3.00`, international local
  `$4.00` (constants `NUMBER_MONTHLY_PRICES_MICROS` in `va_billing.prices`).
- `MonthlyNumberBillingWorkflow` (Temporal schedule `0 6 1 * *` UTC): for each active non-BYO number with
  `purchased_at < first day of month`, post usage_event `phone_number_month` (idempotency `num:{id}:{yyyymm}`). If the
  org balance + credit limit cannot cover it, the number is kept for a 7-day grace period; after grace (second schedule
  run daily `0 7 * * *` checks `org.status`/balance) numbers are released and `number.released` is emitted with
  `data.reason="unpaid"`.

## 9. Carrier configuration steps (operator runbook, executed once per platform account by `scripts/telnyx_bootstrap.py`)

1. Create Outbound Voice Profile `VoiceAgents-Platform` (traffic type conversational, `max_destination_rate` US
   default, whitelisted destinations from `INTERNATIONAL_ALLOWED_COUNTRIES`).
2. Create Call Control Application `VoiceAgents-Platform`, webhook `https://{EDGE_PUBLIC_HOST}/telnyx/voice`,
   API version 2, `anchorsite_override: "Latency"` (Telnyx picks closest anchor site), DTMF type `RFC 2833`,
   `first_command_timeout: true`, `first_command_timeout_secs: 10`.
3. Create Messaging Profile `VoiceAgents-Platform` with webhook `https://{EDGE_PUBLIC_HOST}/telnyx/messaging`.
4. STIR/SHAKEN: Telnyx signs outbound calls automatically for numbers owned in the account; A-attestation requires the
   calling number to be a Telnyx number in the same account (pool and `from` numbers satisfy this). Document in
   `infra/runbooks/telephony.md`.
5. CNAM: for each purchased US number, `PATCH /v2/phone_numbers/{id}/voice` `{"cnam_listing":{"cnam_listing_enabled":true,"cnam_listing_details":"<15 chars org display name>"}}`
   when org setting `cnam_display_name` is set (activity in `NumberOrderWorkflow` step 3; also
   `POST /v1/inbound/{phone_number}/cnam` body `{"display_name": "≤15 chars, letters/digits/space"}`).
6. Script prints the ids to put in `TELNYX_CALL_CONTROL_APP_ID`, `TELNYX_MESSAGING_PROFILE_ID`,
   `TELNYX_OUTBOUND_VOICE_PROFILE_ID`; idempotent (finds existing by name).

## 10. Tests required

- Search: query→Telnyx filter mapping (parametrized over every param), response mapping from a recorded fixture,
  caching, `NO_NUMBERS_FOUND`, Telnyx error mapping.
- Purchase: happy path through fake Telnyx with order completing immediately; pending >8 s → 202; failure → 409 with hold
  released; plan number caps; idempotent order re-creation by `customer_reference`; SMS enablement path; CNAM set.
- InboundConfig: every field validated (valid + invalid case each), merge semantics with `null`, runner conflict.
- Inbound routing (edge, integration with simulator): active number → answer with stream params; unknown number →
  reject; maintenance window with fallback → transfer; maintenance with message only → play + hangup; suspended org
  → fallback; blocked/allowed callers.
- Pools: CRUD, plan gating, each strategy (deterministic with seeded random), cap filter + fallback, concurrency of two
  simultaneous selections never pick the same row when ≥ 2 candidates (SKIP LOCKED test).
- local_match: exact NPA, neighbour walk, same state, any; non-NANP destination.
- BYO: account create validates key (fake Telnyx accepts `byo-good`, rejects others), bootstrap creates three resources,
  dial uses customer key and app id, invalid handle error message exact, delete detaches numbers and restores
  connection id, BYO webhook signature verified with account public key.
- Monthly billing schedule: idempotent per month, grace and release.
- RLS: org A cannot read/update/release org B numbers (404).

## 11. Acceptance criteria

1. A developer can search, buy, configure (pathway + voice + webhook), receive a simulated inbound call that runs the
   pathway, and release the number, entirely through the API, against the simulator, in `tests_e2e/test_inbound_number.py`.
2. Outbound calls with `dialing_strategy: {"type":"local"}` select a same-NPA number when one exists (e2e assertion on
   `calls.from_number`).
3. A BYO account's calls use the customer key (asserted via the fake Telnyx request log) and are billed with zero
   transfer charges.
4. All endpoints appear in `openapi/openapi.json` with examples, and in the SDKs (`26-sdks-cli-mcp-docs.md`).
