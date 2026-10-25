# 05 — Calls API

Router module: `apps/api/src/va_api/routers/calls.py`. Envelope family: **legacy** (`31-error-catalog.md` §1).
Schemas: `packages/va_domain/src/va_domain/calls.py`. Service logic: `apps/api/src/va_api/services/call_dispatch.py`
(validation, runner resolution, precedence, compliance, limits, hold, insert, workflow start).

## 1. Endpoint inventory

| Method | Path | Scope required | Summary |
|---|---|---|---|
| POST | `/v1/calls` | `calls:write` | Send Call (outbound, immediate or scheduled) |
| GET | `/v1/calls` | `calls:read` | List calls with filters |
| GET | `/v1/calls/active` | `calls:read` | Currently active calls (status in `dialing,ringing,in_progress,transferring`) |
| POST | `/v1/calls/active/stop` | `calls:write` | Stop all active calls of the org |
| GET | `/v1/calls/{call_id}` | `calls:read` | Call details (Call Object, `19-events-and-webhooks.md` §4) |
| POST | `/v1/calls/{call_id}/stop` | `calls:write` | Stop an active or scheduled call |
| GET | `/v1/calls/{call_id}/recording` | `calls:read` | Recording (302 redirect to signed URL or `{"url":…}` with `?format=json`) |
| GET | `/v1/calls/{call_id}/transcript` | `calls:read` | Transcript in `json` (default), `text`, `srt`, `vtt` |
| POST | `/v1/calls/{call_id}/correct-transcript` | `calls:write` | Replace text of specific turns (human correction) |
| POST | `/v1/calls/{call_id}/analyze` | `calls:write` | Bland analyze (goal + questions) |
| POST | `/v1/calls/analyze` | `calls:write` | Batch analyze across up to 500 calls (async, §7.12) |
| GET | `/v1/calls/analyze/{analysis_id}` | `calls:read` | Batch analyze status/results (§7.12) |
| GET | `/v1/calls/export` | `calls:read` | CSV export with list filters (§7.11) |
| GET | `/v1/calls/{call_id}/events` | `calls:read` | Persisted event log (paginated by `seq`) |
| GET | `/v1/calls/{call_id}/events/stream` | `calls:read` | SSE live stream (`19` §9) |
| POST | `/v1/calls/{call_id}/transcript/token` | `calls:read` | Short-lived token for transcript WebSocket (`19` §9) |
| POST | `/v1/calls/{call_id}/control` | `calls:write` | Live control: `say`, `inject_context`, `transfer` (ours; Bland-compatible extension) |

All endpoints require API-key or dashboard-session auth (`04-auth-tenancy-rbac.md`). `call_id` path values that are
not UUIDs return `404 CALL_NOT_FOUND`.

## 2. `POST /v1/calls` — request headers

| Header | Required | Semantics |
|---|---|---|
| `authorization` | yes | `Bearer <key>` or bare `<key>`; `x-api-key` is ignored |
| `encrypted_key` | no | BYO Telnyx account handle (`telnyx_accounts.encrypted_key_handle`). When present, the call is dialed with that account's API key, Call Control App, and the `from` number must belong to that account. Unknown handle → `400 INVALID_ENCRYPTED_KEY`. Account `status != 'active'` → `502 CARRIER_CONNECTION_ERROR`. |
| `Idempotency-Key` | no | 1–255 printable chars. Stored in `idempotency_keys` for 24 h. Same key + same body hash returns the stored response with header `Idempotent-Replayed: true`. Same key + different body → `422 IDEMPOTENCY_KEY_REUSED`. In-flight duplicate → `409 IDEMPOTENCY_IN_PROGRESS`. |

## 3. `POST /v1/calls` — body parameters (complete)

Unknown top-level fields are ignored (logged at debug) for Bland forward-compatibility. All string fields are trimmed.

### 3.1 Runner selection

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `task` | string | — | 1–16 000 chars | Free-form prompt runner. Required when none of `pathway_id`, `persona_id`, `agent_id`. Ignored (with warning event) when another runner is set, except when `persona_id` is set and the persona config has no pathway (then `task` overrides the persona prompt per §4.3). |
| `pathway_id` | uuid | — | must exist in org, not deleted | Pathway runner. |
| `pathway_version` | integer \| `"production"` \| `"staging"` | `"production"` | integer ≥ 0 | `0`, omitted, or `"production"` → `pathways.production_version` (if null, the draft version is used and `call.queued` event carries warning `PATHWAY_UNPUBLISHED`). Integer that does not exist → falls back to production with warning event `PATHWAY_VERSION_FALLBACK`; if production is also null → `404 PATHWAY_VERSION_NOT_FOUND`. `"staging"` → `staging_version`, falling back to production. |
| `start_node_id` | string | pathway start node | must exist in resolved version | Begin at this node instead of the `isStart` node. |
| `persona_id` | uuid | — | must exist | Persona runner (preset). May be combined with `pathway_id`/`pathway_version` to override the persona's pathway. |
| `persona_version` | `"production"` \| `"draft"` \| integer | `"production"` | | `draft` falls back to production when the persona has no draft distinct from production. |
| `persona_settings` | object | `{}` | keys from SendCallConfig | Per-call overrides applied **after** persona config and pathway override (highest priority for versioned personas). |
| `agent_id` | uuid | — | must exist | Agent runner. Cannot be combined with `persona_id` or `pathway_id` (`400 RUNNER_CONFLICT`). `task` must be omitted (`400 RUNNER_CONFLICT`). Requires an org-scoped API key (`api_keys.scopes` contains `*` or `agents:run`), else `403 AGENT_REQUIRES_ORG_KEY`. |
| `agent_version` | string | production pin | `"production"`, `"staging"`, `"dev"`, `"latest"`, semver `^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$`, or `"branch:<name>"` with name 1–64 chars `[A-Za-z0-9._/-]` | Omitted → production pin **and eligible for experiment routing** (§4.2). Explicit values never enter experiments. `dev`/`latest` → newest `agent_versions` row on branch `main`. `branch:x` → newest on branch `x`. Missing → `404 NOT_FOUND`. |

### 3.2 Basic

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `phone_number` | string | required | parse with `phonenumbers` (default region from `country` hint `US`); must be E.164-dialable | Destination. Normalized to E.164. |
| `voice` | string | `"Karen"` mapped to org default voice (`orgs.settings.default_voice_id`, else platform voice named `Karen`) | name (case-insensitive, org scope first then platform) or voice UUID | ElevenLabs voice to use. Unknown → `404 VOICE_NOT_FOUND`. |
| `voice_settings` | object | voice's stored settings | `stability` 0–1, `similarity_boost` 0–1, `style` 0–1, `use_speaker_boost` bool, `speed` 0.7–1.2 | Per-call TTS overrides (ours). |
| `first_sentence` | string | none | ≤ 1 000 chars, supports `{{var}}` | First utterance spoken verbatim (after templating). Pre-rendered via phrase cache when possible. When absent the LLM generates the opener from the task/start node. |
| `wait_for_greeting` | boolean | `false` | | When true, agent stays silent until the callee speaks (first committed user utterance) or `greeting_timeout_ms` (default 6 000) elapses. |
| `language` | string | `"babel-en"` | one of the codes in `08-stt-elevenlabs.md` §5 | STT/TTS language selection. |
| `model` | string | `"base"` | `base`, `turbo`, `enhanced` (Bland legacy) or any model id allowed for the org | Bland legacy values map: `base`/`enhanced` → `LLM_DIALOGUE_MODEL`, `turbo` → `LLM_ROUTER_MODEL` (faster). Explicit model ids require org setting `llm.allow_model_override`. |
| `temperature` | number | `0.7` | 0–1 | Dialogue LLM temperature. |
| `max_tokens` | integer | `250` | 32–1 024 | Max tokens per agent reply (ours). |
| `pronunciation_guide` | array | `[]` | ≤ 200 items: `{word: string 1–100, pronunciation: string 1–200, case_sensitive: bool=false, spaced: bool=false}` | Applied to TTS text (`09-tts-elevenlabs.md` §6). |
| `interruption_threshold` | integer ms | `null` (adaptive) | 50–5 000 | Legacy fixed post-speech silence before responding. When set, disables adaptive endpointing and uses this value as the EOT silence (`07` §6). |
| `interruptibility` | integer | `2` | 0–4 | 0 block, 1 difficult, 2 balanced, 3 easy, 4 always (`07` §7). |
| `block_interruptions` | boolean | `false` | | Equivalent to `interruptibility=0` (wins when true). |
| `noise_cancellation` | boolean | `true` | | Enables the inbound spectral noise gate (`07` §10). |
| `background_track` | string \| null | `null` | `null`, `"office"`, `"cafe"`, `"restaurant"`, `"none"` | `null` = quiet phone static at −40 dBFS; `"none"` = digital silence between utterances. |
| `hd_audio` | boolean | `false` | | L16 16 kHz on the Telnyx leg and `pcm_16000` to ElevenLabs (`07` §15). |
| `record` | boolean | `false` | | Dual-channel recording via Telnyx; stored in S3. Forced `true` when `post_call_evals` or `citation_schema_ids` are set. |
| `max_duration` | number (minutes) | `30` | 0.5–240 | Hard limit; agent says `org.settings.max_duration_message` (default "I need to wrap up our call now. Thank you for your time. Goodbye.") 10 s before limit then hangs up. |

### 3.3 Dispatch

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `from` | string | org default number, else platform pool number | E.164; must be an `active` `phone_numbers` row of the org (or of the BYO account when `encrypted_key`) with `voice` capability | Caller ID. |
| `dialing_strategy` | object | `{"type":"default"}` | `{"type":"default"}`, `{"type":"local"}`, `{"type":"custom_pooling","pool_id":uuid}` | `local`: pick an org number whose area code (NANP NPA) equals the destination's; fall back to same state (NPA→state table in `va_common.nanp`), then default `from`. `custom_pooling`: enterprise feature (`FEATURE_NOT_IN_PLAN` otherwise); selection per pool strategy (`16-phone-numbers.md` §8). `from` given explicitly wins over strategy. |
| `timezone` | string | `"America/Los_Angeles"` | IANA tz | Used for `{{now}}`, `start_time` without offset, calling-window checks. |
| `start_time` | string | — | `YYYY-MM-DD HH:MM:SS ±HH:MM` or ISO 8601; must be in the future and ≤ 90 days ahead | Scheduled call: `status='scheduled'`; workflow durable timer. Cancel with `POST /v1/calls/{id}/stop`. |
| `transfer_phone_number` | string | — | E.164 or `sip:` URI | Default transfer target for task runner (LLM gets a `transfer_call` tool). Ignored for pathway runners (Transfer nodes define targets) — a warning event is emitted. |
| `transfer_list` | object | `{}` | ≤ 20 entries `{label: E.164}`, must include `default` if non-empty | Named transfer targets; `transfer_call` tool takes `label`. Ignored for pathways. |
| `warm_transfer` | object | `null` | `{enabled: bool, briefing_prompt: string ≤ 2000, hold_audio: "music"|"silence"|"agent", timeout_s: 10–120 (default 45)}` | Ours. When enabled, transfers are warm (`06` §10). |
| `max_attempts_per_day` | — | — | — | Not a call parameter; enforced via batches. |
| `country` | string | `"US"` | ISO 3166-1 alpha-2 | Default region for parsing `phone_number` without `+`. |

### 3.4 Knowledge and tools

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `tools` | array | `[]` | ≤ 30 items; each `"TL-<uuid>"`, `"KB-<uuid>"`, or inline tool object (schema = `13-tools-and-dynamic-data.md` §2 without `id`) | Tools available to the task runner LLM; KB ids enable retrieval augmentation each turn. For pathways, these are added as global tools available on every Default node. |
| `knowledge_base_ids` | array of `"KB-<uuid>"` | `[]` | ≤ 10 | Alias for KB entries in `tools`. |
| `memory_id` | uuid | — | must exist | Explicit memory to load/update; otherwise memory resolves from contact + persona when memory is enabled (`20`). |
| `memory_enabled` | boolean | org setting `memory.default_enabled` (false) | | Enable memory for task runner calls. |

### 3.5 Voicemail and AMD

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `ivr_mode` (alias `amd`) | boolean | `false` | | Designed for navigating IVRs: disables human-timing heuristics, forces voicemail action `ignore`, disables Telnyx AMD, raises endpointing silence floor to 900 ms, enables DTMF tool `press_button` for task runner. |
| `voicemail` | object | `{"action":"hangup"}` | see below | |
| `voicemail.message` | string | — | ≤ 1 000 chars, `{{vars}}` | Message to leave. Presence implies `leave_message` when `action` omitted. |
| `voicemail.action` | string | `"hangup"` (or `"leave_message"` when `message` set) | `hangup`, `leave_message`, `leave_message_and_sms`, `ignore` | `ignore` = continue conversation as if human. |
| `voicemail.sms` | object | — | `{message: string ≤ 1 600, from?: E.164}` | Required when `leave_message_and_sms`. `from` must have SMS capability; defaults to call `from`. |
| `voicemail.sensitive` | boolean | `false` | | Adds LLM transcript classification on top of Telnyx AMD (`10-llm-gateway.md` §9.4) for the first 12 s of callee speech. |
| `voicemail_detect_timeout_ms` | integer | `12000` | 3 000–30 000 | Ours: window during which voicemail classification can fire. |
| `answered_by_enabled` | boolean | `true` | | Bland legacy flag; when false, AMD disabled and `answered_by` = `unknown`. |

### 3.6 Analysis and post-call

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `summary_prompt` | string | platform default summary prompt (`18-post-call-analysis.md` §3) | ≤ 2 000 chars | Custom summary instructions. |
| `dispositions` | array of string | org dispositions + built-ins | ≤ 50, each `^[A-Z][A-Z0-9_]{0,63}$` | LLM picks one post-call → `disposition_tag`. Built-ins always available: `INTERESTED`, `NOT_INTERESTED`, `FOLLOW_UP_REQUIRED`, `CALL_BACK_SCHEDULED`, `TRANSFERRED`, `DO_NOT_CONTACT`. System-assigned (never LLM-chosen): `NO_ANSWER`, `BUSY`, `CANCELED`, `FAILED`, `AGENT_ENDED_CALL`, `VOICEMAIL`. |
| `analysis_schema` | object | — | JSON object mapping key → type string (`"string"`, `"number"`, `"boolean"`, `"YYYY-MM-DD HH:MM:SS"`, `"string[]"`, or a description sentence) ≤ 50 keys | Structured post-call extraction into `analysis`. |
| `analysis_prompt` | string | — | ≤ 2 000 chars | Extra instructions for `analysis_schema` extraction. |
| `citation_schema_ids` | array of uuid | `[]` | ≤ 10, must exist; enterprise feature | Post-call citation extraction; forces `record=true`. |
| `post_call_evals` | object | — | `{workbench_setup_id: uuid}` or `{workbench_setup_version_id: uuid}` (exactly one) | Post-call evaluation; forces `record=true`; results on `evals` webhook. |
| `stream_post_transfer_transcript` | boolean | `false` | | After a transfer, keep a listen-only stream and publish `call.post_transfer_transcript` events. |
| `retry` | object | — | `{wait: integer seconds 60–86 400, voicemail_action: "hangup"|"leave_message"|"ignore", voicemail_message?: string, max_attempts?: 1–5 (default 1)}` | When the call reaches voicemail (or no-answer when `retry.on_no_answer=true`), a child call is created after `wait` seconds with `parent_call_id` set and the given voicemail settings. Missing `wait` → `400 RETRY_NOT_PROVIDED`. |
| `retry.on_no_answer` | boolean | `false` | | Ours: also retry on `no_answer`/`busy`. |

### 3.7 Advanced

| Parameter | Type | Default | Validation | Semantics |
|---|---|---|---|---|
| `request_data` | object | `{}` | ≤ 64 KB serialized; keys `^[A-Za-z_][A-Za-z0-9_]{0,63}$` | Variables available as `{{key}}` in prompts, pathways, tools. **Replaces** any default `request_data` (agent/persona) entirely when present in the body, even `{}`. |
| `metadata` | object | `{}` | ≤ 64 KB | Echoed in webhooks and Call Object; never shown to the LLM. **Replaces** defaults entirely when present. |
| `external_id` | string | — | 1–256 printable ASCII (`[\x20-\x7E]`) | Customer lookup key; filterable. |
| `webhook` | string | — | https URL (http allowed in local/test or with org setting); SSRF-checked at request time (`WEBHOOK_URL_REJECTED`) | Post-call + live events target. |
| `webhook_events` | array of string | `[]` | subset of `queue`, `call`, `latency`, `webhook`, `tool`, `dynamic_data`, `citations`, `evals`, `post_transfer_transcript` | Live events to send to `webhook` (`19` §3). |
| `dynamic_data` | array | `[]` | ≤ 10 items (schema `13-tools-and-dynamic-data.md` §6) | Pre-call (and optionally per-turn) fetches merged into variables/context. |
| `keywords` | array of string | `[]` | ≤ 100; each `word` or `word:boost` with boost integer 1–10 | STT keyterm boosting (`08` §6). |
| `ignore_button_press` | boolean | `false` | | Ignore inbound DTMF entirely. |
| `precall_dtmf_sequence` | string | — | `^[0-9*#w]{1,64}$` (`w` = 0.5 s pause) | Sent immediately after answer before the agent speaks (via Telnyx `send_dtmf`), for extension/IVR navigation. |
| `test_call` | boolean | `false` | requires role developer+ (dashboard session or key with `pathways:write`) | Marks a test call (dashboard "Send test call"): allows `pathway_version: "draft"` / persona `draft`, excluded from analytics aggregates and experiments, Call Object field `test_call: true`. Billed normally. |
| `guard_rails` | array | `[]` | see below | |
| `local_dialing` | boolean | `false` | | Bland legacy alias for `dialing_strategy={"type":"local"}`. |
| `geospatial_dialing` | boolean | `false` | | Bland legacy alias for `local_dialing`. |
| `available_tags` | array of string | — | | Bland legacy alias for `dispositions`. |
| `citations` | — | — | — | (No such parameter; see `citation_schema_ids`.) |
| `test_mode` | boolean | `false` | | Ours: the call is routed to the simulator caller (`va_sim`) instead of Telnyx; only with `test` environment API keys. Billed at $0. |

`guard_rails` items:

```json
{"type": "tcpa:ai_disclosure"}
{"type": "tcpa:self_introduction"}
{"type": "tcpa:recording_disclosure", "config": {"end_seconds": 30}}
{"type": "tcpa:opt_out", "action": {"type": "end_call"}}
{"type": "custom", "name": "no_medical_advice", "description": "Agent must never give medical advice", "action": {"type": "transfer", "phone_number": "+14155550100"}}
{"type": "custom", "name": "angry_caller", "description": "Caller is very angry", "action": {"type": "move_to_node", "node_id": "escalation"}}
```

Actions: `end_call`, `transfer {phone_number}`, `move_to_node {node_id}` (pathways only), `none` (log only).
Max 5 `custom` (`400 GUARD_RAIL_LIMIT`); custom requires enterprise plan (`FEATURE_NOT_IN_PLAN`). Semantics in
`23-guardrails-and-compliance.md`.

## 4. Resolution and precedence

### 4.1 Validation order (first failure wins; each step maps to a code)

1. Auth (`AUTH_FAILURE`, `ORG_SUSPENDED`).
2. Idempotency lookup (replay / `IDEMPOTENCY_*`).
3. JSON schema/Pydantic validation (`INVALID_INPUT`).
4. Runner rule check (`RUNNER_CONFLICT`, `AGENT_REQUIRES_ORG_KEY`).
5. Resource existence: pathway/version, persona, agent/version, voice, tools, KBs, citation schemas, eval setup,
   memory, number pool (`NOT_FOUND` family codes, `PATHWAY_VERSION_NOT_FOUND`, `VOICE_NOT_FOUND`, `TOOL_NOT_FOUND`).
6. Plan features (`FEATURE_NOT_IN_PLAN`, `GUARD_RAIL_LIMIT`).
7. `encrypted_key` resolution (`INVALID_ENCRYPTED_KEY`, `CARRIER_CONNECTION_ERROR`).
8. Phone number parse and `from` ownership (`INVALID_PHONE_NUMBER`, `INVALID_INPUT` field `from`).
9. Compliance: platform blacklist + org DNC (`NUMBER_BLOCKED`), international policy (`INTERNATIONAL_NOT_ENABLED`),
   calling hours when `orgs.settings.compliance.enforce_calling_hours` (`OUTSIDE_CALLING_HOURS`; skipped for
   scheduled calls — re-checked at dispatch time).
10. Limits (skipped for scheduled calls until dispatch): `SAME_NUMBER_TOO_SOON`, `NEW_ACCOUNT_LIMIT`,
    `INTERNATIONAL_RATE_LIMITED`, `DAILY_CAP_REACHED`, `HOURLY_CAP_REACHED`, `CONCURRENCY_CAP_REACHED`.
11. Credits hold (`INSUFFICIENT_CREDITS`) — hold amount = `min(max_duration, 10) × connected_rate` + `min_attempt`.
12. Insert + workflow start (on Temporal failure the row is marked `failed` with `error_message` and
    `503 SERVICE_UNAVAILABLE` is returned; hold released).

### 4.2 Agent version resolution and experiments

1. `agent_version` omitted → look for `experiments` row with `agent_id` and `status='running'`. If found, choose an
   arm by deterministic weighted hash: `h = int(sha256(f"{experiment_id}:{phone_number}").hexdigest()[:8], 16) % 100`;
   walk arms ordered by `label` accumulating `weight`; first arm whose cumulative weight > h. Store
   `experiment_id`, `experiment_arm_id`, `agent_version_id`. Weights must sum to 100 (validated on experiment start).
2. No running experiment → `agents.env_pins.production` (semver). Missing pin → newest version on branch `main`.
3. Explicit `production`/`staging`/`dev` → `env_pins[...]`; `latest` = `dev` behaviour; semver exact; `branch:x`.

### 4.3 Config precedence (lowest → highest)

```
platform defaults (SendCallConfig field defaults)
 → org defaults (orgs.settings.call_defaults)
 → runner defaults:
      agent:   agent_versions.call_settings (+ pathway_id/pathway_version from the version)
      persona (versioned, persona_versions row exists): request fields → then persona config applied OVER them
               → then pathway_id/pathway_version override from the request → then persona_settings
      persona (legacy preset, no versions): persona config → request fields over it
      pathway: pathway_versions.global_config.call_defaults
 → explicit request fields (only fields actually present in the JSON body; tracked via model_fields_set)
```

Special rules:
- `request_data` and `metadata`: if present in the body (even `{}`), they **replace** the lower layers' objects
  entirely; otherwise the highest lower layer's object is used. No deep merge.
- `tools`: union of runner tools and request tools (deduplicated by id); inline tools from the request win on name clash.
- `dispositions`: request list replaces; otherwise org dispositions; built-ins always appended.
- `guard_rails`: union by `type`+`name`; request entries win.
- Arrays otherwise replace; objects other than `request_data`/`metadata` shallow-merge per key.

The resolution function `resolve_call_config(org, body, runner) -> SendCallConfig` is pure (no I/O; inputs loaded
beforehand) and fully unit-tested with a table of ≥ 40 cases.

### 4.4 `SendCallConfig` (resolved model persisted in `calls.config`)

```python
class PronunciationRule(BaseModel):
    word: str; pronunciation: str; case_sensitive: bool = False; spaced: bool = False

class VoicemailConfig(BaseModel):
    action: Literal["hangup","leave_message","leave_message_and_sms","ignore"] = "hangup"
    message: str | None = None
    sms: VoicemailSms | None = None          # VoicemailSms: message: str; from_: str | None (alias "from")
    sensitive: bool = False
    detect_timeout_ms: int = 12000

class RetryConfig(BaseModel):
    wait: int; voicemail_action: Literal["hangup","leave_message","ignore"] = "hangup"
    voicemail_message: str | None = None; max_attempts: int = 1; on_no_answer: bool = False

class DialingStrategy(BaseModel):
    type: Literal["default","local","custom_pooling"] = "default"; pool_id: UUID | None = None

class WarmTransferConfig(BaseModel):
    enabled: bool = False; briefing_prompt: str | None = None
    hold_audio: Literal["music","silence","agent"] = "music"; timeout_s: int = 45

class GuardRailAction(BaseModel):
    type: Literal["end_call","transfer","move_to_node","none"]; phone_number: str | None = None; node_id: str | None = None

class GuardRail(BaseModel):
    type: Literal["tcpa:ai_disclosure","tcpa:self_introduction","tcpa:recording_disclosure","tcpa:opt_out","custom"]
    name: str | None = None; description: str | None = None
    config: dict[str, JsonValue] = {}; action: GuardRailAction | None = None

class PostCallEvals(BaseModel):
    workbench_setup_id: UUID | None = None; workbench_setup_version_id: UUID | None = None

class SendCallConfig(BaseModel):
    # runner
    runner_type: Literal["task","pathway","persona","agent"]
    task: str | None = None
    pathway_id: UUID | None = None
    pathway_version: int | None = None           # resolved integer
    start_node_id: str | None = None
    persona_id: UUID | None = None
    persona_version: int | None = None
    agent_id: UUID | None = None
    agent_version_id: UUID | None = None
    experiment_id: UUID | None = None
    experiment_arm_id: UUID | None = None
    # basic
    phone_number: str
    country: str = "US"
    voice_id: UUID
    provider_voice_id: str
    voice_settings: VoiceSettings                # stability, similarity_boost, style, use_speaker_boost, speed
    first_sentence: str | None = None
    wait_for_greeting: bool = False
    greeting_timeout_ms: int = 6000
    language: str = "babel-en"
    dialogue_model: str
    router_model: str
    temperature: float = 0.7
    max_tokens: int = 250
    pronunciation_guide: list[PronunciationRule] = []
    interruption_threshold_ms: int | None = None
    interruptibility: int = 2
    block_interruptions: bool = False
    noise_cancellation: bool = True
    background_track: Literal["office","cafe","restaurant","none"] | None = None
    hd_audio: bool = False
    record: bool = False
    max_duration_min: float = 30
    # dispatch
    from_number: str
    phone_number_id: UUID | None
    telnyx_account_id: UUID | None = None
    dialing_strategy: DialingStrategy = DialingStrategy()
    timezone: str = "America/Los_Angeles"
    start_time: datetime | None = None
    transfer_phone_number: str | None = None
    transfer_list: dict[str, str] = {}
    warm_transfer: WarmTransferConfig = WarmTransferConfig()
    # knowledge
    tool_ids: list[UUID] = []
    inline_tools: list[InlineTool] = []
    knowledge_base_ids: list[UUID] = []
    memory_id: UUID | None = None
    memory_enabled: bool = False
    # voicemail
    ivr_mode: bool = False
    answered_by_enabled: bool = True
    voicemail: VoicemailConfig = VoicemailConfig()
    # analysis
    summary_prompt: str | None = None
    dispositions: list[str]
    analysis_schema: dict[str, str] | None = None
    analysis_prompt: str | None = None
    citation_schema_ids: list[UUID] = []
    post_call_evals: PostCallEvals | None = None
    stream_post_transfer_transcript: bool = False
    retry: RetryConfig | None = None
    # advanced
    request_data: dict[str, JsonValue] = {}
    metadata: dict[str, JsonValue] = {}
    external_id: str | None = None
    webhook: str | None = None
    webhook_events: list[str] = []
    dynamic_data: list[DynamicDataSpec] = []
    keywords: list[str] = []
    ignore_button_press: bool = False
    precall_dtmf_sequence: str | None = None
    guard_rails: list[GuardRail] = []
    test_mode: bool = False
    # derived
    max_reprompts: int = 2                        # from org settings (07 §12)
    no_input_timeout_ms: int = 8000
    error_phrase: str = "I'm sorry, I'm having trouble on my end. Let me get someone to help you."
```

## 5. `POST /v1/calls` — responses

Success `200`:

```json
{"status": "success", "message": "Call successfully queued.", "call_id": "0193c5d2-7b1e-7a10-9a52-1f3b0d4e6a77", "batch_id": null}
```

Scheduled success: same body with `"message": "Call successfully scheduled."` and `"scheduled_for": "2026-10-25T16:00:00Z"`.

Error example `400`:

```json
{"status": "error", "message": "Invalid phone number.", "errors": [{"error": "INVALID_PHONE_NUMBER", "message": "Invalid phone number.", "field": "phone_number"}], "request_id": "req_0193c5d2…"}
```

## 6. `GET /v1/calls` — list

Query parameters (all optional):

| Param | Type | Semantics |
|---|---|---|
| `to_number` / `to` | string | exact E.164 |
| `from_number` / `from` | string | exact E.164 |
| `limit` | int 1–1 000 (default 1 000, Bland parity) | page size |
| `ascending` | bool (default false) | order by created_at |
| `start_date` / `end_date` | `YYYY-MM-DD` | created_at range in org timezone (inclusive) |
| `created_at_gte` / `created_at_lte` | ISO 8601 | precise range |
| `batch_id` | uuid | |
| `campaign_id` | uuid | alias of `batch_id` |
| `pathway_id`, `persona_id`, `agent_id` | uuid | |
| `completed` | bool | post-call complete |
| `answered_by` | `human`,`voicemail`,`ivr`,`unknown`,`no-answer`,`fax` | |
| `inbound` | bool | |
| `status` | comma list of call statuses | |
| `disposition_tag` | string | |
| `duration_gt` / `duration_lt` | number minutes | connected length |
| `external_id` | string | |
| `cursor` | string | cursor pagination (ours); when present `total_count` is omitted |

Response `200`:

```json
{"status": "success", "total_count": 1532, "count": 2, "next_cursor": "eyJ0IjoiMjAyNi0xMC0yNFQxODoyMjozMS40MTJaIiwiaSI6IjAxOTNjNWQyIn0", "calls": [
  {"call_id": "…", "c_id": "…", "created_at": "2026-10-24T18:20:01Z", "call_length": 2.35, "to": "+14155550123", "from": "+14155550999",
   "completed": true, "queue_status": "complete", "status": "completed", "inbound": false, "answered_by": "human", "batch_id": null,
   "external_id": null, "disposition_tag": "INTERESTED", "price": 0.282, "pathway_id": null, "call_ended_by": "ASSISTANT", "error_message": null}
]}
```

List items are the Call Object minus `transcripts`, `concatenated_transcript`, `pathway_logs`, `variables`,
`citations`, `evals`, `analysis` (use detail endpoint).

## 7. Other endpoints

### 7.1 `GET /v1/calls/{call_id}`

Response body = the full Call Object (`19` §4) with no wrapper and no top-level `"status":"success"` field (Bland
returns the call object directly; the object's own `status` field is the call status). `recording_url` is a fresh 7-day presigned S3 URL.

### 7.2 `POST /v1/calls/{call_id}/stop`

- Scheduled call: cancels the workflow timer (`CallWorkflow.signal_cancel`), status `canceled`, hold released,
  disposition `CANCELED`. Response `{"status":"success","message":"Call canceled."}`.
- Queued/dialing/ringing: Telnyx `hangup` on the leg if a `call_control_id` exists; status → `canceled`.
- In progress: publishes `{"type":"stop","reason":"api"}` on `call:{id}:control`; owner worker hangs up immediately
  (no goodbye line), `ended_by='api'`, `end_reason='api_stop'`. Response `{"status":"success","message":"Call ended successfully."}`.
- Terminal statuses → `409 CALL_NOT_ACTIVE`.

### 7.3 `POST /v1/calls/active/stop`

Optional body `{"call_ids": [uuid]}` to restrict. Stops every non-terminal call (including scheduled) of the org.
Response `{"status":"success","message":"Stopped 14 calls.","stopped":14,"call_ids":[…]}`. Requires role `admin`+
for session auth.

### 7.4 `GET /v1/calls/active`

Response `{"status":"success","count":3,"calls":[{"call_id","to","from","status","started_at","direction","runner_type","pathway_id","current_node_id","duration_seconds"}]}`.
`current_node_id` read from Redis hash `call:{id}:carrier_state` field `node_id` (updated by the media worker on route).

### 7.5 `GET /v1/calls/{call_id}/recording`

`404 NOT_FOUND` if no recording (or retention purged). Default `302` to presigned URL (TTL 15 min); `?format=json` →
`{"status":"success","url":"…","expires_at":"…","duration_ms":143200,"channels":2,"format":"wav"}`.
`?channel=caller|agent|mixed` (default `mixed`): mixed/split variants are produced in post-call
(`18-post-call-analysis.md` §2) and stored as `recordings/{org}/{call}/{mixed|caller|agent}.wav`.

### 7.6 `GET /v1/calls/{call_id}/transcript`

`?format=json|text|srt|vtt`. JSON: `{"status":"success","call_id":"…","transcripts":[…same as Call Object…],"concatenated_transcript":"…"}`.
Text = `concatenated_transcript`. SRT/VTT built from `start_ms`/`end_ms` with speaker prefixes `User:`/`Agent:`.

### 7.7 `POST /v1/calls/{call_id}/correct-transcript`

Body `{"corrections":[{"id": <transcript id = turn row ordinal>, "text": "corrected"}]}` (≤ 500). Updates
`call_turns.text`, keeps original in `call_events` (category `call`, message `transcript_corrected`), writes audit log.
Response `{"status":"success","updated":n}`. Only terminal calls (`409 CALL_NOT_ACTIVE` otherwise).

### 7.8 `POST /v1/calls/{call_id}/analyze`

Body (Bland format):

```json
{"goal": "Qualify the lead for solar installation", "questions": [["Who answered the call?", "human or voicemail"], ["Is the caller a homeowner?", "boolean"], ["Best callback time", "YYYY-MM-DD HH:MM:SS"]]}
```

Validation: `goal` ≤ 2 000 chars; `questions` 1–50 pairs `[question ≤ 500, type ≤ 200]`. Runs synchronously with
`LLM_ANALYSIS_MODEL` (timeout 60 s → `504 UPSTREAM_TIMEOUT`), stores `call_analyses(kind='analysis_schema')`,
bills LLM tokens. Response:

```json
{"status": "success", "message": "Call analyzed successfully", "answers": ["human", true, "2026-10-26 15:00:00"], "credits_used": 0.003}
```

Answer types: `boolean` → JSON bool; `number`/`integer` → number; date format strings → string in that format;
anything else → string; unknowable → `null`.

### 7.9 `GET /v1/calls/{call_id}/events`

Query `after_seq` (default 0), `limit` ≤ 1 000, `category` (comma list). Response
`{"status":"success","events":[{seq, category, level, message, data, created_at}], "next_after_seq": n | null}`.

### 7.10 `POST /v1/calls/{call_id}/control`

Body one of:

```json
{"type": "say", "text": "One moment please."}
{"type": "inject_context", "text": "The customer's order shipped today."}
{"type": "transfer", "phone_number": "+14155550100", "mode": "cold"}
```

Published on `call:{id}:control`; `409 CALL_NOT_ACTIVE` if the call is not `in_progress`. Response
`{"status":"success","message":"Control message delivered."}` once the owner acknowledges via Redis reply key
`call:{id}:control_ack:{msg_id}` within 3 s, else `{"status":"success","message":"Control message queued."}`.

### 7.11 `GET /v1/calls/export`

Same filters as §6. Streams `text/csv` (UTF-8, header row) with columns `call_id, created_at, direction, to, from, status,
answered_by, call_length, end_reason, disposition_tag, pathway_id, batch_id, price, summary, external_id`. Max 100 000 rows
per request (`INVALID_INPUT` beyond, with message suggesting date filters). Scope `calls:read`.

### 7.12 `POST /v1/calls/analyze` (batch analyze)

Body `{"call_ids": [uuid, …] (1–500), "goal": string, "questions": [[question, type], …]}` — same semantics as §7.8 per
call. Runs asynchronously: response `{"status":"success","analysis_id":"…"}`; results via `GET /v1/calls/analyze/{analysis_id}`
→ `{"status":"completed"|"running","results":[{"call_id","answers":[…]}],"credits_used":…}`; each per-call result also
stored in `call_analyses`. Scope `calls:write`.

## 8. Status state machine

```
scheduled ──(timer fires)──► queued ──(dial accepted)──► dialing ──(call.initiated/ringing)──► ringing
   │                           │                           │                                    │
   └──stop──► canceled         ├──blocked at dispatch──► failed (error_message)                 ├──call.answered──► in_progress
                               └──stop──► canceled         └──Telnyx error──► failed           ├──hangup no_answer/timeout──► no_answer
                                                                                                ├──hangup busy──► busy
                                                                                                └──hangup rejected/unallocated/other──► failed
in_progress ──transfer started──► transferring ──bridged──► transferred ──hangup──► completed
in_progress ──hangup / end node / timeout / api stop / guard rail──► completed
in_progress ──fatal pipeline error after answer──► completed (end_reason='error', error_message set)
```

`completed` here means "the conversation ended"; `calls.queue_status` = `complete` and Call Object `completed=true`
only after `PostCallWorkflow` finishes. Transitions are enforced by `va_db.repos.calls.transition(call_id, from, to)`
which uses `UPDATE … WHERE status = ANY(:allowed_from)` and raises `CONFLICT` on mismatch (duplicates from carrier
retries are therefore no-ops). Bland status mapping in the Call Object: `no_answer` → `no-answer`, `in_progress` →
`in-progress`, `transferring`/`transferred` → `in-progress`/`completed`.

## 9. `end_reason` enumeration (normative; referenced by other specs)

| `end_reason` | `ended_by` | Bland `call_ended_by` | Meaning |
|---|---|---|---|
| `agent_end_call` | agent | ASSISTANT | End Call node / `end_call` tool |
| `user_hangup` | user | USER | Callee hung up (`hangup_source=callee` for outbound, `caller` for inbound) |
| `max_duration` | timeout | TIMEOUT | `max_duration` reached |
| `no_input_timeout` | system | SYSTEM | Silence policy ended the call (`07` §12) |
| `voicemail_hangup` | system | SYSTEM | Voicemail detected, action `hangup` |
| `voicemail_left` | agent | ASSISTANT | Voicemail message left, then hung up |
| `api_stop` | api | API | Stop endpoint |
| `guard_rail_end` | guard_rail | GUARD_RAIL | Guard rail action `end_call` |
| `transferred` | system | SYSTEM | Cold transfer completed; AI leg detached |
| `warm_transferred` | system | SYSTEM | Warm transfer completed |
| `transfer_failed_end` | system | SYSTEM | Transfer target failed and agent ended per fallback |
| `no_answer` | system | SYSTEM | Not answered within dial timeout |
| `busy` | system | SYSTEM | Busy |
| `rejected` | system | SYSTEM | Carrier/callee rejected |
| `invalid_number` | system | SYSTEM | Unallocated/invalid destination |
| `carrier_error` | error | ERROR | Telnyx error during dial or call |
| `canceled` | api | API | Canceled before connect |
| `dispatch_blocked` | system | SYSTEM | Blocked at dispatch (DNC, cap, window, credits) |
| `insufficient_credits` | system | SYSTEM | Credits exhausted mid-call (hard stop after grace, `24` §6) |
| `worker_lost` | error | ERROR | Media worker lost ownership |
| `worker_crash` | error | ERROR | Heartbeat lost |
| `error` | error | ERROR | Unrecoverable pipeline error |
| `ivr_dead_end` | system | SYSTEM | IVR mode: no progress for 120 s |
| `fax_detected` | system | SYSTEM | AMD fax result |
| `inbound_rejected` | system | SYSTEM | Inbound call rejected by routing |
| `loop_protection` | system | SYSTEM | Pathway node-visit or skip-user-response chain limit exceeded (`11-pathways-engine.md`); error phrase spoken first |

## 10. Tests required

- Request model: every parameter valid/invalid boundaries (parametrized), aliases (`amd`, `local_dialing`,
  `geospatial_dialing`, `available_tags`, `campaign_id`), unknown fields ignored.
- Validation order: one test per step proving earlier failures mask later ones.
- `resolve_call_config` precedence table ≥ 40 cases including `request_data` replacement with `{}`, persona versioned
  vs legacy, agent pins, experiment hashing determinism and weight distribution (10 000 numbers within ±2 %).
- Idempotency: replay, body mismatch, concurrency (two simultaneous requests → one call).
- Limits and compliance codes each produce the exact error body.
- Stop semantics for each status; stop-all; control ack/queued.
- State machine: every allowed transition and rejection of disallowed ones; duplicate carrier events are no-ops.
- Analyze endpoint type coercion; transcript formats (SRT/VTT golden files).
- Cross-tenant: every endpoint returns 404 for another org's call.
- E2E (simulator): outbound basic, scheduled call fires at `start_time` (time-skipping), stop mid-call, retry on voicemail.

## 11. Acceptance criteria

1. Every parameter in §3 is accepted, validated, persisted in `calls.config`, and honored by the pipeline (each has at
   least one test asserting downstream effect).
2. `POST /v1/calls` p95 ≤ 150 ms under 200 rps in the load test (excluding dial).
3. OpenAPI documents every field with description, type, default, and example; drift check passes.
4. Bland-compat contract test: the 12 sample requests in `apps/api/tests/fixtures/bland_send_call/*.json` (one per
   parameter group) are accepted and produce equivalent configs.
