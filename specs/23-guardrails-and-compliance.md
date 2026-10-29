# 23 — Guard Rails and Compliance Controls

Package ownership: guard rail configs, classifiers, and runtime `packages/va_compliance` (`guard_rails.py`,
`dnc.py`, `consent.py`, `redaction.py`, `calling_windows.py`, `state_rules.py`, `holidays.py`); in-call integration
in `va_engine` (turn hook, `11-pathways-engine.md`) and `va_voice` (DTMF capture mode, `07-media-pipeline.md`);
API routers `apps/api/src/va_api/routers/dnc.py`, `compliance.py`. Envelope format for `/v1/dnc*` and `/v1/compliance*`.

This spec describes product controls. It is not legal advice; the docs site includes a notice that customers are responsible
for their own compliance and that the controls are aids.

## 1. Guard rails on calls

### 1.1 Request shape (`guard_rails` on Send Call, inbound config, batch, persona, agent call_settings)

```json
"guard_rails": [
  {"type": "tcpa:ai_disclosure"},
  {"type": "tcpa:self_introduction", "config": {"business_name": "Acme Dental"}},
  {"type": "tcpa:recording_disclosure", "config": {"end_seconds": 30}},
  {"type": "tcpa:opt_out", "action": {"type": "end_call"}},
  {"type": "custom", "name": "no_medical_advice", "description": "The agent must never give a diagnosis or recommend medication.",
   "check": "agent", "action": {"type": "transfer", "phone_number": "+14155550100"}}
]
```

`va_compliance.guard_rails.GuardRail` (discriminated union on `type`):

| Type | Fields | Default action | Evaluation |
|---|---|---|---|
| `tcpa:ai_disclosure` | `config.end_seconds` int 5–120 (default 30) | `{"type":"end_call"}` only if `config.enforce=true`, else log only (default `enforce=false`, event `triggered=false/true`) | Agent must state that it is an AI/automated system within `end_seconds` of answer. **Proactive mode** (default `config.mode="inject"`): the engine prepends a disclosure to the first agent utterance when not already present in `first_sentence` (text `config.text`, default `"Just so you know, I'm an AI assistant."`), so the rail always passes. `mode="check"`: only verifies. |
| `tcpa:self_introduction` | `config.business_name` (default org name), `config.end_seconds` (default 30), `config.mode` inject/check | as above | Agent must state the business name it calls on behalf of. Inject text `"I'm calling on behalf of {business_name}."` |
| `tcpa:recording_disclosure` | `config.end_seconds` (default 30), `config.mode` inject/check, `config.text` (default `"This call may be recorded for quality purposes."`) | as above | Required when the call is recorded (`record=true`); ignored otherwise. Auto-added by policy §4. |
| `tcpa:opt_out` | `action` (default `end_call`), `config.confirmation_text` (default `"Understood. I've removed your number from our list. Goodbye."`) | `end_call` | Every user turn: detect a request to stop being contacted. On trigger: write DNC (`source='opt_out_voice'`), speak confirmation, perform action, disposition `DO_NOT_CONTACT`. |
| `custom` | `name` `^[a-z][a-z0-9_]{0,39}$`, `description` ≤ 1 000, `check`: `"user"` \| `"agent"` \| `"both"`, `action` | required | Every turn (user turn before reply generation for `user`/`both`; agent reply after generation, before TTS flush, for `agent`/`both`). Max 5 per call (`GUARD_RAIL_LIMIT`), enterprise plans only (`FEATURE_NOT_IN_PLAN`). |

Actions: `{"type":"end_call","speech"?: string}`, `{"type":"transfer","phone_number": E.164,"speech"?: string}`,
`{"type":"move_to_node","node_id": string}` (pathway runners only; validated at request time against the resolved pathway
version → `INVALID_INPUT` field `guard_rails[i].action.node_id`), `{"type":"log"}` (record only).

### 1.2 Evaluation timing and latency

- Disclosure rails (`ai_disclosure`, `self_introduction`, `recording_disclosure`) in `check` mode: evaluated incrementally on
  each agent turn by a deterministic matcher first (case-insensitive regexes per rail, §1.3) and, if no match, by the LLM
  classifier once at `end_seconds` (async, off the critical path). If not satisfied at `end_seconds` → triggered.
- `opt_out` and `custom` with `check` user/both: the classifier runs **in parallel** with the router/extraction step for the
  turn (`11-pathways-engine.md` per-turn algorithm); the speculative reply is held until the classifier result arrives
  (budget 250 ms; if the classifier exceeds 400 ms the reply proceeds and the rail is evaluated post hoc — a post hoc trigger
  executes its action immediately after the current utterance).
- `custom` with `check` agent/both: the generated reply text is checked sentence-by-sentence as it streams; a violating
  sentence is never sent to TTS (sentence buffered until classifier verdict, budget 200 ms per first sentence; later sentences
  pipelined). On violation the reply is discarded and the action executes; for `log` action the sentence is still dropped and
  replaced by the node's re-generation once with an added instruction "Do not <description>."

### 1.3 Deterministic matchers

- AI disclosure regex: `\b(ai|a\.i\.|artificial intelligence|virtual assistant|automated (assistant|system|agent)|digital assistant|ai assistant|bot)\b`.
- Self introduction: business name tokens (casefold, punctuation stripped) all present in an agent utterance, or the literal
  `on behalf of`.
- Recording disclosure regex: `\b(record(ed|ing)?|monitored)\b`.
- Opt-out fast path regex on user text (triggers without LLM): `\b(stop calling|don't call|do not call|remove (me|my number)|take me off|put me on (your|the) do not call|unsubscribe)\b`;
  anything else goes to the classifier.

### 1.4 Classifier prompts (verbatim; `va_compliance/prompts/`), model role `router`, temperature 0, forced tool

`opt_out_system.txt`:
```
You decide whether the person on a phone call is asking to stop being contacted.
Return opted_out = true only if the person clearly asks not to be called, texted, or contacted again, or asks to be removed from a list.
Return false for mere disinterest ("not interested", "no thanks"), for hanging-up phrases, and for requests to call back later.
Call the function judge_opt_out.
```
Tool `judge_opt_out`: `{"opted_out": boolean, "confidence": number 0..1, "quote": string ≤ 200}`. Trigger when `opted_out` and
`confidence ≥ 0.7`.

`custom_rail_system.txt`:
```
You monitor a phone conversation for a rule violation.
Rule name: {{name}}
Rule: {{description}}
You are checking the {{subject}}'s latest message (subject is "person" for the caller or "agent" for the AI agent).
Decide whether that latest message violates the rule. Consider the recent conversation only as context.
Call the function judge_rule.
```
Tool `judge_rule`: `{"violated": boolean, "confidence": number 0..1, "reason": string ≤ 300}`; trigger at `confidence ≥ 0.75`.
Context window: last 6 turns.

`disclosure_check_system.txt`:
```
You check whether an AI phone agent made a required statement during the first part of a call.
Required statement: {{requirement}}
Agent utterances so far, in order:
{{agent_utterances}}
Decide whether the requirement was satisfied in substance (exact wording is not required).
Call the function judge_disclosure.
```
Tool `judge_disclosure`: `{"satisfied": boolean, "evidence_turn_index": integer|null}`. Requirements text per rail: AI disclosure
"The agent told the person it is an AI or automated system."; self introduction "The agent named the business {business_name} it is
calling for."; recording "The agent told the person the call may be recorded."

### 1.5 Recording of outcomes

Every evaluation that triggers (and every disclosure rail's final verdict) inserts `guard_rail_events`
(`guard_rail` = `tcpa:opt_out` or `custom:<name>`, `triggered`, `action`, `turn_index`, `detail` = classifier output) and emits
`call.guard_rail`. Call Object exposes `guard_rails: [{type, name?, triggered, action_taken, turn_index}]` (Shared-contract addition
to `19` §4).

## 2. Do-Not-Call management

### 2.1 Enforcement points

- `POST /v1/calls`, batch validation, SMS send: block if `(org_id, e164)` or `(platform, e164)` exists in `dnc_entries` and
  is unexpired → `NUMBER_BLOCKED` (calls/SMS) or batch item skip `DNC`.
- Inbound calls/SMS from DNC numbers are **not** blocked (they initiated contact); SMS opt-outs suppress automated replies only.
- Sub-accounts: parent org setting `compliance.dnc_inherit_from_parent` (default true) makes child orgs also honor the parent's list.

### 2.2 API (`/v1/dnc`)

| Method | Path | Body / query | Notes |
|---|---|---|---|
| GET | `/v1/dnc` | `phone?, source?, cursor?, limit?` | list org entries |
| POST | `/v1/dnc` | `{phone_number, reason?, expires_at?}` | source `api` (or `manual` when via dashboard session) |
| DELETE | `/v1/dnc/{phone_number}` | — | removal audit-logged; `opt_out_*` entries require role owner/admin |
| POST | `/v1/dnc/check` | `{phone_numbers: E.164[] ≤ 1000}` | `{data:{results:[{phone_number, blocked: bool, scope: "org"\|"platform"\|null, source}]}}` |
| POST | `/v1/dnc/import` | multipart CSV (`phone_number` column required, optional `reason`, `expires_at`), ≤ 1 000 000 rows | async: returns `{data:{import_id}}`; `GET /v1/dnc/imports/{id}` → `{status, total, inserted, invalid, duplicates}`; processed by `DncImportWorkflow` (queue `va-ingest`, tracked in `dnc_imports`) in 10 000-row COPY chunks with `ON CONFLICT DO NOTHING` |
| GET | `/v1/dnc/export` | — | streams CSV of org entries |

Platform blacklist: `org_id IS NULL` rows managed only by platform admins (`/internal/admin/dnc`), source `platform`; includes
numbers reported for abuse and emergency numbers (911, 112, 999, 000, 110, 119 and their E.164 forms are rejected earlier by the
phone validator with `INVALID_PHONE_NUMBER`).

National DNC registry (FTC) scrubbing is a customer responsibility; the import endpoint accepts registry extracts; docs explain
the process. No automatic federal registry download is built.

## 3. Calling hours and frequency

Defined in `17-batches-and-scheduling.md` §7 (windows, state rules, frequency caps). Org settings:
`compliance.enforce_calling_hours` (bool, default false), `compliance.calling_hours` (`[CallingWindow]`, default 08:00–21:00 daily),
`compliance.state_quiet_hours` (bool, default true), `compliance.max_attempts_per_number_per_day` (int, default 0 = unlimited).

> Decision (2026-10-29): `compliance.calling_hours` is the only calling-hours setting (the former top-level
> `calling_hours` of specs/04 §7 was replaced by it); `max_attempts_per_number_per_day` accepts 0–100.

## 4. Recording consent policy

- All-party consent jurisdictions table `va_compliance.consent.ALL_PARTY_CONSENT_STATES` (US) =
  `{"CA","CT","DE","FL","IL","MD","MA","MI","MT","NV","NH","OR","PA","WA"}` — a conservative superset (some of these states
  apply all-party consent only to certain call types or have mixed case law; the platform treats them all as all-party).
  Non-US destinations (any country other than US) are treated as all-party consent.
- Policy (`compliance.recording_disclosure_policy`, default `"auto"`):
  - `"always"`: whenever `record=true`, add `tcpa:recording_disclosure` (inject mode) if absent.
  - `"auto"`: add it when `record=true` and the callee (outbound) or caller (inbound) number resolves to an all-party state
    (state from NPA via `npa_neighbors.json` table state field, `16-phone-numbers.md` §6.3) or a non-US country, or when the
    agent number's state is all-party.
  - `"never"`: do not add (org admin must acknowledge a warning in the dashboard; stored in audit log).
- Disclosure also applies when `post_call_evals` forces recording.
- Consent record: `call_events` row category `guard_rail` with message `recording_disclosure_given` and the turn index; retained
  with the call.

## 5. AI disclosure laws

`compliance.ai_disclosure_policy` (default `"always"`): `"always"` adds `tcpa:ai_disclosure` (inject mode) to every call that
doesn't specify it; `"auto"` adds it only for destinations in `va_compliance.consent.AI_DISCLOSURE_REQUIRED_STATES`
(data file with `statute`, `source_url`, `last_verified` per entry; initial entries CA (B.O.T. Act, bots used to influence a
commercial transaction), UT (AI disclosure on request in consumer interactions), CO (consumer-facing AI disclosure under the
Colorado AI Act) — implementer verifies each against the cited statute and records sources); `"never"` requires admin
acknowledgement like §4. Because the FCC treats AI-generated voices as "artificial voice" under the TCPA, docs instruct customers
that outbound calls to consumers require prior express (written, for marketing) consent; the platform records the customer's
consent attestation per batch (`batches.global_props.consent_attestation` boolean required true for batches when org setting
`compliance.require_consent_attestation=true`, default true).

## 6. Redaction (PII / PCI)

### 6.1 Real-time STT entity detection

When org setting `compliance.redaction.enabled` (default false; forced true in `hipaa_mode`), the STT session is opened with
ElevenLabs `entity_detection=["pii","pci","phi"]` (`08-stt-elevenlabs.md`). `committed_transcript_entities` messages give
character spans; the stored `call_turns.text` replaces each span with `[REDACTED:<entity_type>]`. The **unredacted** text is
used in memory only for the live LLM context of the current call (needed for the conversation) unless
`compliance.redaction.redact_llm_context=true`, which replaces spans before the LLM too.

> Decision (2026-10-29): these two settings live in the top-level `redaction` group of `OrgSettings` and are read as
> `redaction.enabled` and `redaction.redact_llm_context` (specs/04 §7, Decision "OrgSettings fields (P02-T12a)").

### 6.2 Regex fallback (always applied to stored transcripts, summaries, analysis results, webhook payloads when redaction enabled)

`va_compliance.redaction.redact(text) -> (text, spans)`:
- Card numbers: `\b(?:\d[ -]?){13,19}\b` validated by Luhn → `[REDACTED:card_number]`.
- US SSN: `\b(?!000|666|9\d\d)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b` → `[REDACTED:ssn]`.
- CVV in context: `\b(cvv|cvc|security code)\D{0,10}\d{3,4}\b` → digits replaced.
- Bank account/routing in context: `\b(account|routing)( number)?\D{0,10}\d{6,17}\b` → digits replaced.
- Spoken digit sequences: words `zero…nine/oh` sequences of ≥ 13 tokens normalized to digits, then the card rule applied.

### 6.3 Recording redaction

When redaction enabled and a recording exists, `PostCallWorkflow` (after retranscription) silences audio segments whose word
timestamps overlap redacted spans (replace samples with zeros on both channels, pad ±150 ms) before upload to S3; the
original Telnyx copy is deleted regardless of `delete_carrier_recordings`.

### 6.4 DTMF capture mode (PCI scope reduction)

- Pathway node option `secure_input` (on Default/Webhook nodes, `11-pathways-engine.md`) and tool input field flag `secure: true`
  (`13-tools-and-dynamic-data.md`): the agent asks the caller to key digits; while active, the media pipeline (a) pauses
  forwarding audio to STT (sends silence frames to keep the session alive), (b) collects DTMF digits from Telnyx `dtmf` media
  events until `#` or `max_digits` or 10 s inter-digit timeout, (c) stores digits only in the encrypted ephemeral variable store
  (`call:{id}:secure:{name}` Redis key, AES-GCM with a per-call key, TTL 15 min) and exposes them to webhooks/tools via
  `{{secure.name}}` templating, never to the LLM (the LLM sees `"[secure input received: 16 digits]"`), (d) suppresses the
  digits from transcripts, events, logs, and recordings (recording paused via Telnyx `record_pause` / `record_resume` during
  capture).
- `ignore_button_press=true` disables DTMF entirely except in secure capture.

## 7. HIPAA mode

Org setting `hipaa_mode` (enterprise; requires signed BAA flag `compliance.baa_signed=true` set by platform admin):
- ElevenLabs STT/TTS sessions use `enable_logging=false` (zero-retention mode; requires the platform ElevenLabs account to be
  on an enterprise agreement, checked by startup config `ELEVENLABS_ZERO_RETENTION_AVAILABLE=true`, else hipaa_mode cannot be
  enabled → `FEATURE_NOT_IN_PLAN`).
- LLM provider: the org's provider must be flagged `baa_covered=true` in its LLM provider config (`10-llm-gateway.md`); the default
  Telnyx Inference provider is marked per platform configuration `LLM_DEFAULT_BAA_COVERED`.
- Redaction forced on; `LOG_TRANSCRIPTS` ignored (never logged); webhook payloads include transcripts only if the endpoint is
  marked `phi_allowed=true`.
- Retention: `retention_days` ≤ org-configured value, recordings encrypted with an org-specific KMS data key.
- Audit logging of every transcript/recording access (`audit_logs` action `call.transcript.read`, `call.recording.read`).

## 8. Data retention enforcement

`RetentionWorkflow` (Temporal schedule daily `30 3 * * *` UTC):
1. For each org: delete `call_turns`, `call_events`, `call_analyses`, `citations`, `eval_runs`, `guard_rail_events`, `messages`
   older than `retention_days` (batched 5 000 rows per transaction); null out `calls.summary`, `variables`, `request_data`,
   `metadata`, keep billing-relevant columns.
2. Delete S3 recordings older than `recording_retention_days` (S3 lifecycle rules also set per prefix as a backstop:
   `infra/terraform` creates a lifecycle rule with the platform maximum 3650 days; per-org deletion is done by the workflow).
3. Drop `call_events` partitions older than the maximum retention across orgs; create the next 3 monthly partitions.
4. Delete expired `idempotency_keys` (> 24 h), `sessions` expired > 30 days, `email_tokens` expired.
5. Emit metrics `va_retention_rows_deleted_total{table}`.

Right-to-erasure: `POST /v1/compliance/erasure` `{phone_number? | email? | external_id?}` → deletes the contact, memories,
messages, and turns of calls involving that contact (calls rows kept with PII columns nulled), recordings deleted; returns
`{data:{erasure_id}}`; `GET /v1/compliance/erasure/{id}` status. Audit logged.

## 9. Tests required

- Guard rail parsing for each type and action, limits, plan gating, `move_to_node` validation.
- Inject mode: first utterance contains disclosure exactly once even when `first_sentence` already contains it.
- Deterministic matchers: positive/negative examples per regex (≥ 10 each).
- Classifier hold logic with fake LLM latency 100 ms (held) and 600 ms (post hoc path).
- Agent-side custom rail drops a violating sentence before TTS (assert fake ElevenLabs never received that text).
- Opt-out end-to-end: user says "please stop calling me" → DNC entry, confirmation spoken, call ended, disposition DO_NOT_CONTACT,
  subsequent `POST /v1/calls` to the number → 403 `NUMBER_BLOCKED`.
- DNC API, import (1 000 000-row synthetic CSV in < 60 s against testcontainers Postgres), check, export, inheritance.
- Consent policy matrix: every state in the table × record on/off × policy value.
- Redaction regexes (Luhn valid/invalid, SSN exclusions), entity span replacement, recording silencing (sample-level assertion),
  DTMF secure capture: digits never appear in turns/events/logs (log capture assertion), available to tool templating.
- HIPAA mode enabling preconditions; STT/TTS URLs include `enable_logging=false`.
- Retention workflow deletes exactly the expired rows; erasure.

## 10. Acceptance criteria

1. No configuration allows a recorded call to an all-party-consent state without an automatic disclosure unless an admin
   explicitly chose `"never"` (audit trail present).
2. Card numbers spoken or keyed during a call never appear in stored transcripts, logs, events, webhooks, or recordings when
   redaction is enabled (verified by an e2e test scanning all stores for the test card number `4242 4242 4242 4242`).
3. Opt-out requests are honored within the same call and enforced across voice and SMS for that org.
