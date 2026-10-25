# 21 — SMS/MMS Messaging on Telnyx and Omnichannel Conversations

Package ownership: API `apps/api/src/va_api/routers/sms.py`, `conversations.py`, `ten_dlc.py`; inbound webhook handling
`apps/telephony_edge/src/va_edge/messaging.py`; workflows `packages/va_workflows/src/va_workflows/sms.py`
(`SmsConversationWorkflow`, `TenDlcRegistrationWorkflow`, `ConversationTimeoutSweepWorkflow`); text-mode engine
entry `va_engine.text_mode.run_text_turn` (`11-pathways-engine.md`); Telnyx messaging client
`va_telnyx.messaging` (`06-telephony-telnyx.md`); segment math `va_domain.sms_segments`.

Envelope format (`31-error-catalog.md` §1) for every endpoint in this file.

## 1. Channel model

- A **conversation** (`conversations` row) is a thread between one agent number and one user number (`channel='sms'`
  or `'mms'`), or a widget thread (`22-web-widget.md`). It carries the runner (objective/pathway/persona/agent), engine
  state (`11-pathways-engine.md` §9 `EngineState`), `request_data`, `metadata`, and status.
- **Active conversation lookup** for `(org_id, agent_number, user_number)`: the most recent row with
  `status IN ('active','human_takeover')` and `last_message_at > now() − time_out_minutes`. Otherwise a new conversation
  is created on the next inbound/outbound message.
- Voice and SMS share memory through `contacts`/`memories` (`20-memory-personas-agents.md`); every SMS message is appended
  to the contact memory's `recent_messages` (channel `sms`).
- `imessage` and `rcs` → 400 `CHANNEL_NOT_SUPPORTED`.

## 2. `POST /v1/sms/send`

Body (`va_domain.sms.SmsSendRequest`):

| Field | Type | Required | Default | Semantics |
|---|---|---|---|---|
| `user_number` | E.164 | yes | — | recipient |
| `agent_number` | E.164 | yes | — | an org phone number with `sms` capability |
| `agent_message` | string ≤ 1600 | conditional | — | first message to send verbatim. Required unless `objective`/pathway/persona/agent is given and `generate_first_message=true` |
| `generate_first_message` | bool | no | false | ours: engine generates the opener |
| `objective` | string ≤ 10 000 | no | — | free-form prompt runner (text analogue of `task`) |
| `pathway_id` | uuid | no | — | |
| `pathway_version` | int \| `"production"` \| `"staging"` | no | `"production"` | |
| `start_node_id` | string | no | pathway start node | |
| `persona_id` | uuid | no | — | |
| `agent_id` | uuid | no | — | |
| `request_data` | object | no | {} | |
| `metadata` | object | no | {} | |
| `new_conversation` | bool | no | false | close any active conversation for the pair (status `closed`) and start fresh |
| `webhook` | URL | no | number's `sms_config.webhook` | conversation webhook |
| `disposition_ids` | string[] | no | — | disposition tags candidate set for conversation close classification |
| `citation_schema_ids` | uuid[] | no | [] | run citations when conversation closes |
| `channel` | `"sms"` \| `"mms"` \| `"imessage"` \| `"rcs"` | no | `"sms"` | |
| `template_id` | string | no | — | ours, replaces Bland `content_sid`: a `prompts` id `PT-…` whose content is used as the message with `{{var}}` substitution |
| `content_sid` | string | no | — | accepted alias of `template_id` |
| `content_variables` | object | no | {} | values for template substitution |
| `media_urls` | URL[] ≤ 10 | no | [] | MMS; forces `channel='mms'` |
| `time_out` | int minutes 5–43200 | no | 1440 | conversation inactivity timeout |

Runner rule: at most one of `objective` / `pathway_id` / `agent_id`; `persona_id` may combine with `pathway_id`. With no
runner, the number's `sms_config` runner applies; with none there, the conversation is "send-only" (replies are stored and
forwarded to the webhook but not answered).

Steps:
1. AuthZ scope `sms:write`. Validate numbers; `agent_number` must belong to org, be `active`, include `sms` capability;
   US long code requires the number assigned to an active 10DLC campaign (§6), toll-free requires verification status
   `verified` (§7) → else 400 `SMS_NOT_ENABLED`.
2. Compliance: `user_number` on DNC (org or platform, including SMS opt-outs) → 403 `NUMBER_BLOCKED`.
3. Rate limits (§11) → 429.
4. Resolve/create conversation; render first message (verbatim, template, or engine-generated in text mode).
5. Insert `messages(direction='outbound', role='assistant', status='queued')`; credit hold not used — SMS is charged on
   `message.sent` (§12); balance must be ≥ 0 + credit limit for the estimated segments (`INSUFFICIENT_CREDITS`).
6. Telnyx `POST /v2/messages` `{"from": agent_number, "to": user_number, "text": body, "media_urls": [...]?,
   "messaging_profile_id": profile, "webhook_url": "https://{EDGE_PUBLIC_HOST}/telnyx/messaging",
   "use_profile_webhooks": false}`. Telnyx message creation has no idempotency key, so duplicates are prevented by sending
   only from the Temporal activity `send_message(message_id)`, which records `telnyx_message_id` immediately after Telnyx
   responds; on retry the activity first checks `telnyx_message_id` and, when set, returns without sending.
7. Response 200 `{"data":{"conversation_id":"…","message_id":"…","status":"queued","segments":1}, "errors":null}`.

## 3. SmsNumberConfig (stored in `phone_numbers.sms_config`)

```python
class SmsNumberConfig(BaseModel):
    enabled: bool = False
    objective: str | None = None
    pathway_id: UUID | None = None
    pathway_version: int | Literal["production","staging"] = "production"
    persona_id: UUID | None = None
    agent_id: UUID | None = None
    request_data: dict[str, JsonValue] = {}
    webhook: AnyHttpUrl | None = None
    time_out: int = 1440                       # minutes
    debounce_ms: int = 2000                    # 500–10000, merge rapid inbound messages
    reply_delay_ms: int = 0                    # 0–30000 artificial typing delay before reply
    max_reply_chars: int = 480                 # 160–1600; engine instructed to stay within; hard truncation at sentence boundary
    help_message: str = "{{org_name}}: reply STOP to opt out. Msg & data rates may apply."
    opt_out_message: str = "You have been unsubscribed and will receive no further messages. Reply START to resubscribe."
    opt_in_message: str = "You have been resubscribed. Reply STOP to opt out."
    business_hours: dict | None = None         # {"timezone": IANA, "windows":[CallingWindow]} (17-batches §3); outside → after_hours_message
    after_hours_message: str | None = None
    memory_enabled: bool = True
    disposition_ids: list[str] | None = None
    citation_schema_ids: list[UUID] = []
```

Updated through `POST /v1/sms/webhook/update` (§4) and `POST /v1/inbound/{phone_number}/sms` (`16-phone-numbers.md` §5.7).

## 4. `POST /v1/sms/webhook/update`

Body: `{"phone_number": "+1…", "webhook": URL|null, "objective"?: …, "pathway_id"?: …, "pathway_version"?: …,
"persona_id"?: …, "agent_id"?: …, "request_data"?: …, "time_out"?: …}` — merges into `sms_config` (Bland's endpoint sets
the inbound SMS webhook/behaviour of a number). Response `{"data":{"phone_number":…,"sms_config":{…}},"errors":null}`.

## 5. Inbound flow

1. `telephony-edge` `POST /telnyx/messaging` (and `/telnyx/messaging/byo/{account_id}`): verify Ed25519 signature
   (`telnyx-signature-ed25519`, `telnyx-timestamp`, signed string `"{timestamp}|{raw body}"`, reject if
   |now − timestamp| > 300 s); dedupe on `data.id` via `telnyx_events`; respond 200 within 100 ms (processing is async via
   Temporal signal).
2. `message.received`: `from.phone_number` = user, `to[0].phone_number` = agent. Lookup number (system scope) → org.
   Unknown number → store event, ignore. Then:
   a. Keyword handling on normalized body (trim, uppercase, strip punctuation), exact match only:
      - Opt-out: `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`, `REVOKE`, `OPTOUT` → insert `dnc_entries(source='opt_out_sms')`
        for (org, user_number), close active conversation (`closed`), reply `opt_out_message` (exactly one reply; the bootstrap
        script configures the messaging profile with Telnyx's automatic opt-out replies disabled so the platform is the single
        source of truth for opt-out handling), emit `conversation.closed` (data.reason `opt_out`).
      - Opt-in: `START`, `UNSTOP`, `YES` (only `YES` when the number is currently opted out) → delete the `opt_out_sms` DNC entry, reply `opt_in_message`.
      - Help: `HELP`, `INFO` → reply `help_message`; conversation unaffected.
   b. If user is opted out (DNC) and not an opt-in keyword → store message, do not reply.
   c. Insert `messages(direction='inbound', role='user', status='received', media_urls from payload media[].url copied to
      S3 `{org_id}/mms/{message_id}/{n}` because Telnyx media links expire after 30 days)`.
   d. Resolve conversation (§1); create if none using `sms_config` runner; `SignalWithStart` `SmsConversationWorkflow`
      (workflow id `sms-{conversation_id}`) with signal `inbound(message_id)`.
3. `message.sent` / `message.finalized`: update `messages.status` from `to[0].status`, store `errors`, `segments` (`parts`),
   cost into usage (§12); emit `sms.sent` / `sms.delivered` / `sms.failed`.

## 6. 10DLC registration (`/v1/sms/10dlc`, envelope) and TenDlcRegistrationWorkflow

Telnyx endpoints (verified: Telnyx 10DLC quickstart): `POST /v2/10dlc/brand`, `GET /v2/10dlc/brand/{brandId}`,
`POST /v2/10dlc/brand/{brandId}/vetting`, `POST /v2/10dlc/campaignBuilder`, `GET /v2/10dlc/campaign/{campaignId}`,
`POST /v2/10dlc/phoneNumberCampaign`.

| Method | Path | Body |
|---|---|---|
| POST | `/v1/sms/10dlc/brands` | `{entity_type: "PRIVATE_PROFIT"\|"PUBLIC_PROFIT"\|"NON_PROFIT"\|"GOVERNMENT"\|"SOLE_PROPRIETOR", display_name, company_name, ein?, phone, street, city, state, postal_code, country ("US"), email, website?, vertical: one of TECHNOLOGY, HEALTHCARE, RETAIL, FINANCIAL, INSURANCE, REAL_ESTATE, EDUCATION, PROFESSIONAL, HOSPITALITY, ENERGY, AGRICULTURE, CONSTRUCTION, ENTERTAINMENT, GOVERNMENT, LEGAL, MANUFACTURING, NGO, TRANSPORTATION, COMMUNICATION, POLITICAL), stock_symbol?, stock_exchange?}` |
| GET | `/v1/sms/10dlc/brands` / `/{id}` | — |
| POST | `/v1/sms/10dlc/brands/{id}/vetting` | `{vetting_class: "STANDARD"}` |
| POST | `/v1/sms/10dlc/campaigns` | `{brand_id, use_case: "MIXED"\|"MARKETING"\|"CUSTOMER_CARE"\|"ACCOUNT_NOTIFICATION"\|"2FA"\|"DELIVERY_NOTIFICATION"\|"LOW_VOLUME", description, sample_messages: [s1, s2, …≤5], message_flow, help_message, help_keywords: "HELP,INFO", opt_in_keywords: "START,YES", opt_out_keywords: "STOP,UNSUBSCRIBE,CANCEL,END,QUIT,STOPALL,REVOKE,OPTOUT", opt_in_message, opt_out_message, embedded_link: bool, embedded_phone: bool, number_pool: bool, age_gated: bool, direct_lending: bool, subscriber_optin: true, subscriber_optout: true, subscriber_help: true}` |
| GET | `/v1/sms/10dlc/campaigns` / `/{id}` | — |
| POST | `/v1/sms/10dlc/campaigns/{id}/numbers` | `{phone_numbers: E.164[]}` → assigns each via `phoneNumberCampaign` |

Field mapping to Telnyx camelCase: `entity_type→entityType`, `display_name→displayName`, `company_name→companyName`,
`postal_code→postalCode`, `use_case→usecase`, `sample_messages[i]→sample{i+1}`, `message_flow→messageFlow`,
`help_message→helpMessage`, `opt_in_keywords→optinKeywords`, `opt_out_keywords→optoutKeywords`, `help_keywords→helpKeywords`,
`embedded_link→embeddedLink`, `number_pool→numberPool`, `age_gated→ageGated`, plus the rest by the same camelCase rule. The
vertical list above is the validation enum; the implementer confirms it against the Telnyx brand schema page
(`developers.telnyx.com/api-reference/10dlc-brands`) when writing the validator and records the source in a code comment.

Workflow: submit → `ten_dlc_brands.status='submitted'`; poll brand every 10 min (max 7 days) until `identityStatus`
∈ {`VERIFIED`, `VETTED_VERIFIED`} → `verified`, or `UNVERIFIED` → `unverified` (still allows low-throughput campaigns), errors → `failed`.
Campaign submit → poll `GET /v2/10dlc/campaign/{id}` every 30 min (max 14 days) until active/rejected. Status changes email org
admins and emit system events `tendlc.brand_status` and `tendlc.campaign_status` (Shared-contract additions).

## 7. Toll-free verification

`POST /v1/sms/tollfree_verifications` with Telnyx toll-free verification fields (business name, website, contact, use case,
sample message, opt-in workflow description and image URLs, monthly volume) proxied to Telnyx's toll-free verification API
(`/v2/messaging_tollfree/verification/requests`, implementer confirms field names on the Telnyx docs page and records them in
`va_telnyx.tollfree` docstring); status polled daily; stored in `phone_numbers.sms_config.tollfree_verification`
(`{request_id, status: "pending"|"verified"|"rejected", reason}`).

## 8. SmsConversationWorkflow

```
state: conversation_id, pending_inbound: list[message_id], human_takeover: bool
signals: inbound(message_id), human_message(text, user_id), takeover(on: bool), close(reason)
loop:
  wait until pending_inbound non-empty or close or timeout(time_out_minutes since last message)
  on timeout: activity close_conversation(reason='timed_out') → status timed_out → post-conversation analysis → end
  debounce: after first pending message wait debounce_ms (sms_config) while collecting more signals (timer resets per new message, max total 10 s)
  if human_takeover: activity forward_to_webhook(messages) ; clear pending ; continue
  if outside business_hours and after_hours_message: activity send_message(after_hours_message) ; continue
  activity run_text_turn(conversation_id, pending_inbound)  →
      va_engine.text_mode.run_text_turn: loads EngineState, appends merged user text (joined by "\n"), executes the pathway /
      objective turn in text mode (voice-only nodes adapted per 11-pathways-engine.md text-mode table: Transfer Call →
      sends transfer_sms_message if configured else "A team member will contact you shortly." and sets human_takeover;
      Press Button → skipped; End Call → closes conversation after sending final text), returns reply text(s) and new state
  apply reply_delay_ms timer
  activity send_message(reply) for each reply part (split if > max_reply_chars at sentence boundary, max 3 parts)
  persist EngineState ; continue_as_new every 200 turns
```

Close processing (`close_conversation`): summary (`18-post-call-analysis.md` §3 prompt with "Call" wording replaced by
"Text conversation"), disposition with `disposition_ids`, citations when configured, memory update, `conversation.closed` webhook
with full conversation object.

## 9. Conversations API

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/v1/sms/conversations` | `channel?, agent_number?, user_number?, status?, cursor?, limit?` | list of Conversation (without messages) |
| GET | `/v1/sms/conversations/{id}` | — | Conversation + `messages[]` (chronological) |
| GET | `/v1/sms/conversations/{id}/messages` | `cursor?, limit?` | paginated messages |
| POST | `/v1/sms/conversations/{id}/close` | `{reason?: string}` | closes via workflow signal |
| POST | `/v1/sms/conversations/{id}/takeover` | `{enabled: bool}` | human takeover on/off; emits `conversation.human_takeover` |
| POST | `/v1/sms/conversations/{id}/messages` | `{text, media_urls?}` | send as human agent (`role='human_agent'`); allowed only when takeover enabled or `force=true` |
| DELETE | `/v1/sms/conversations/{id}` | — | hard delete conversation + messages (owner/admin), audit logged |

Conversation object: `{id, channel, agent_number, user_number, status, runner_type, objective, pathway_id, pathway_version,
persona_id, agent_id, current_node_id, request_data, metadata, summary, disposition_tag, last_message_at, created_at,
message_count}`. Message object: `{id, direction, role, body, media_urls (signed S3 URLs), status, segments, price, error, created_at}`.

## 10. Segment counting (`va_domain.sms_segments`)

- GSM-7 basic set + extension table (`^ { } \ [ ~ ] | €` count as 2 septets). If every char is GSM-7: single segment ≤ 160
  septets; multipart segments of 153 septets.
- Otherwise UCS-2: single ≤ 70 UTF-16 code units; multipart 67 per segment; surrogate pairs never split across segments.
- `segments(text) -> int` and `encoding(text) -> Literal["GSM-7","UCS-2"]`; property-tested; `max_reply_chars` warnings.
- Optional smart-encoding (org setting `sms.smart_encoding`, default true): replace curly quotes, en/em dashes, ellipsis,
  non-breaking spaces with GSM equivalents before sending.

## 11. Rate limits

- Per agent number: 1 message/second for US long codes (10DLC throughput is campaign-dependent; we enforce the conservative
  default `sms.number_mps = 1` configurable up to the campaign's T-Mobile/AT&T tier stored in `ten_dlc_campaigns.payload.throughput_mps`),
  toll-free 3 mps, short codes not supported. Implemented with Redis token bucket `smsrl:{e164}`; sends exceeding the bucket wait
  inside the activity (≤ 30 s) rather than failing.
- Per org per day: plan `sms_per_day` (`orgs.limits`) default start 500, build 10 000, scale 50 000, enterprise unlimited →
  429 `RATE_LIMITED` for API sends; inbound always processed.
- Per conversation: max 1 automated reply per 2 s and max 50 automated replies per hour (loop protection against
  auto-responders) → further inbound messages stored, no reply, and a `conversation.message` event is emitted with
  `data.suppressed = true`.

## 12. Billing

- Outbound: on `message.sent`, usage_event `sms_segment_out` quantity = `parts`, unit price plan `sms_rate_micros`
  ($0.02 per segment), idempotency `sms:{message_id}:out`. MMS counts as 3 segments.
- Inbound: on `message.received`, `sms_segment_in` quantity = `parts`, same unit price, idempotency `sms:{message_id}:in`.
- COGS from Telnyx `cost.amount` (USD) stored as `cost_micros`.
- BYO Telnyx numbers: platform price only (same rate; carrier charges billed by Telnyx to the customer).

## 13. Tests required

- Send validation (each field), runner conflicts, 10DLC/toll-free gating, DNC block, template rendering, MMS forcing.
- Duplicate-send protection: activity retried after Telnyx success does not send twice (fake Telnyx counts requests).
- Inbound: signature verification (valid, invalid, stale timestamp), dedupe, each keyword path, opted-out silence, MMS copy to S3.
- Workflow (time-skipping): debounce merges 3 messages in 1.5 s into one turn; timeout closes; human takeover forwards; business
  hours; reply splitting; loop protection.
- Segments: property tests and fixed vectors (`"hello"` → 1 GSM; 161 GSM chars → 2; `"😀"`×70 → 2 UCS-2 segments considering surrogates).
- 10DLC mapping snake→camel snapshot, polling transitions, number assignment.
- Billing idempotency on repeated `message.finalized`.
- E2E: voice call followed by SMS to the same contact shares memory (the SMS engine prompt contains the call summary).

## 14. Acceptance criteria

1. A pathway published once runs unchanged over voice and SMS (text-mode adaptations applied), proven by e2e test.
2. Opt-out keywords stop all further automated SMS and voice calls to that number for the org (DNC enforced on Send Call).
3. Inbound webhook ack p99 ≤ 100 ms; first reply ≤ debounce + 3 s p95 with simulator.
