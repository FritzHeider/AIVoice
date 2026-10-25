# 18 — Post-Call Analysis: Recording, Re-transcription, Summary, Dispositions, Analysis, Citations, Evals

Package ownership: `packages/va_analysis` (prompt templates, LLM calls, result models, citation mapping, eval
scoring); `packages/va_workflows/src/va_workflows/post_call.py` (`PostCallWorkflow` + activities);
API routers `apps/api/src/va_api/routers/analysis.py`, `citation_schemas.py`, `evals.py`, `dispositions.py`.
LLM calls go through `va_llm` (`10-llm-gateway.md`) using role `analysis` (`LLM_ANALYSIS_MODEL`, fallback
`LLM_FALLBACK_MODEL`), temperature 0, structured output via forced tool call.

## 1. PostCallWorkflow

Started by `CallWorkflow` as a child (`workflow_id = postcall-{call_id}`, task queue `va-main-postcall`) after the
pipeline finishes or after a call ends without connecting (no_answer/busy/failed/canceled → only steps 1, 6, 11, 13
run; step 6 assigns the system tag from §4.3 without the LLM). Every step is an activity with its own retry policy and is **idempotent** keyed by `(call_id, step)`; completed
steps are recorded in `calls.config.postcall_steps` (jsonb array of step names) — re-execution skips completed steps.

| # | Step (activity) | Runs when | Timeout / retries | Output |
|---|---|---|---|---|
| 1 | `finalize_call_record` | always | 30 s / 5 | flush pending `call_turns`, compute `connected_seconds`/`transfer_seconds` from `call_legs` timestamps, set `calls.status` final, `answered_by`, `end_reason` (`05-calls-api.md` §9), `ended_by` |
| 2 | `await_recording` | `config.record = true` or `post_call_evals` set or `config.retranscribe = true` | workflow waits for signal `recording_saved(urls)` from edge (`call.recording.saved`) up to 10 min; then proceeds without recording and emits `call.error` code `RECORDING_MISSING` | Telnyx recording URLs |
| 3 | `store_recording` | recording received | 5 min / 5 | download WAV (dual channel, channel 1 = caller/inbound track, channel 2 = agent/outbound track per Telnyx `record_channels=dual`) via streaming GET, upload to `s3://{S3_BUCKET_RECORDINGS}/{org_id}/{yyyy}/{mm}/{call_id}.wav` with SSE-KMS (`28-security.md`), set `recording_s3_key`, `recording_duration_ms`; delete Telnyx copy (`DELETE /v2/recordings/{id}`) when org setting `delete_carrier_recordings=true` (default true); emit `call.recording_ready` |
| 4 | `retranscribe` | `config.retranscribe = true` (default false; forced true when `citation_schema_ids` non-empty or `post_call_evals` set) and recording stored | 10 min / 3 | §2 |
| 5 | `summarize` | call connected (answered_by human/unknown/ivr and ≥ 1 user turn) | 60 s / 3 | `calls.summary`, `call_analyses(kind='summary')` |
| 6 | `classify_disposition` | always when connected; system tags otherwise (§4.3) | 60 s / 3 | `calls.disposition_tag`, `call_analyses(kind='disposition')` |
| 7 | `extract_analysis_schema` | `config.analysis_schema` present | 90 s / 3 | `call_analyses(kind='analysis_schema')` |
| 8 | `extract_citations` | `config.citation_schema_ids` non-empty | 120 s / 3 | `citations` rows, `citations.ready` event + webhook |
| 9 | `run_post_call_evals` | `config.post_call_evals` set | 180 s / 3 | `eval_runs` row, `evals.completed` event + webhook |
| 10 | `update_memory` | memory enabled for the call (`20-memory-personas-agents.md`) and connected | 60 s / 3 | memory summary/facts/open items |
| 11 | `finalize_billing` | always | 30 s / 10 | usage events + hold capture (`24-billing-and-limits.md` §4–5), `calls.price_micros` |
| 12 | `sentiment` | org setting `analysis.sentiment=true` (default true) and connected | 60 s / 3 | `call_analyses(kind='sentiment')` |
| 13 | `emit_completed` | always, last | 30 s / 10 | `calls` `completed` flag (`queue_status='complete'`), `call.completed` event → per-call `webhook` with Call Object (`19-events-and-webhooks.md` §4) |

Ordering: 1 → 2 → 3 → 4 → (5, 6, 7, 12 in parallel) → (8, 9 in parallel, they need the final transcript) → 10 → 11 → 13.
Step 8/9 failures do not block 13 (their webhooks are separate). Steps 5–7, 10, 12 failing after retries record
`call.error` events and set the corresponding field null; 13 still runs. Step 11 failing after retries puts the call on
the `billing_reconcile` list (`24-billing-and-limits.md` §9) and 13 still runs.

Total budget target: `call.completed` webhook ≤ 20 s after hangup p95 when no recording/retranscription is needed,
≤ 90 s with recording + retranscription.

## 2. Post-call re-transcription (ElevenLabs batch STT)

1. Split the dual-channel WAV into two mono 8 kHz PCM16 WAVs in memory (parse RIFF with the Python
   `wave` module and de-interleave with `numpy`).
2. For each channel call `POST {ELEVENLABS_API_BASE}/v1/speech-to-text` multipart: `file` (wav), `model_id` =
   `ELEVENLABS_BATCH_STT_MODEL` (`scribe_v2`), `language_code` = the call language's ISO-639-1 base code unless `auto`,
   `timestamps_granularity=word`, `diarize=false` (channels already separate speakers), `tag_audio_events=false`,
   `keyterms` = call keywords (terms only), and when org `hipaa_mode` is enabled `enable_logging=false`.
3. Build word lists per channel with absolute times (seconds from recording start). Recording start offset relative to
   `answered_at` is `recording_offset_ms` = `call.recording.saved.payload.recording_started_at − answered_at` (0 when missing).
4. **Merge rule**: segment each channel's words into utterances split at gaps ≥ 700 ms; interleave by start time.
   Replace `call_turns.text` for `user` turns with the re-transcribed utterances that overlap each turn's `[start_ms, end_ms]`
   window (≥ 50 % overlap of utterance duration, concatenated with single spaces in time order).
   The original real-time user text is preserved in `call_turns` column `text_realtime`
   (Shared-contract addition) and `text` is overwritten with the re-transcribed version. Assistant turns keep the
   LLM-generated text (authoritative) but `text_spoken` is recomputed when the agent-channel transcript shows the turn
   was cut shorter than recorded (only when `interrupted=true`).
   Re-transcribed utterances not overlapping any user turn (missed by real-time STT) are inserted as new `user` turns
   with `turn_index` fractional ordering resolved by renumbering all turns of the call in start-time order inside one
   transaction.
5. `call.turn` events are **not** re-sent; the Call Object served later reflects the improved transcript.

## 3. Summary

Default system prompt (verbatim, `va_analysis/prompts/summary_system.txt`):

```
You write concise, factual summaries of phone calls between an AI agent and a person.
Rules:
- Write 2 to 5 sentences in plain prose, past tense, third person ("The caller asked...", "The agent confirmed...").
- State the purpose of the call, the key information exchanged, any commitments or next steps, and how the call ended.
- Never invent facts. If something is unclear in the transcript, say it was unclear.
- Do not include phone numbers, card numbers, or government ID numbers even if they appear in the transcript.
- Output only the summary text.
```

User message template:

```
Call direction: {{direction}}
Call started: {{started_at}} ({{timezone}})
Duration: {{duration_seconds}} seconds
Ended by: {{ended_by}}
Transferred to a human: {{transferred}}
{{#if summary_prompt}}Additional instructions from the business (follow them, they override the length rule):
{{summary_prompt}}
{{/if}}
Transcript:
{{transcript}}
```

`{{transcript}}` = lines `"<role>: <text>"` where role ∈ `user`, `assistant`, `human_agent`; interrupted assistant turns use
`text_spoken` followed by ` [interrupted]`. Transcripts longer than 60 000 tokens are truncated keeping the first 20 % and
last 80 % with a line `[… N turns omitted …]`. Output plain text (no tool call); max 600 tokens. The templating engine
here is `va_analysis.template.render` (a tiny logic-less renderer supporting `{{var}}` and `{{#if var}}…{{/if}}`).

## 4. Dispositions

### 4.1 Tag sets

- Built-in agent tags: `INTERESTED`, `NOT_INTERESTED`, `FOLLOW_UP_REQUIRED`, `CALL_BACK_SCHEDULED`, `TRANSFERRED`,
  `DO_NOT_CONTACT`.
- System tags (assigned without the LLM): `NO_ANSWER`, `BUSY`, `CANCELED`, `FAILED`, `AGENT_ENDED_CALL`.
- Org custom tags: `dispositions` table (`/v1/dispositions`, §8).
- Per call: Send Call `dispositions: string[]` restricts the candidate set (each must be built-in or an org tag; unknown
  → 400 `INVALID_INPUT` field `dispositions[i]`). When absent the candidate set is built-ins + all org tags.

### 4.2 Classification prompt (verbatim, `va_analysis/prompts/disposition_system.txt`)

```
You classify the outcome of a phone call into exactly one disposition tag.
Choose the single tag from the allowed list that best describes the outcome from the business's point of view.
Definitions of the allowed tags:
{{tag_definitions}}
Rules:
- If the person asked not to be contacted again, choose DO_NOT_CONTACT when it is allowed.
- If the call was handed to a human, choose TRANSFERRED when it is allowed.
- If a specific callback time was agreed, choose CALL_BACK_SCHEDULED when it is allowed.
- If nothing fits, choose the closest tag and lower your confidence.
Call the function select_disposition with your answer.
```

`tag_definitions` lines `- TAG: description` (built-in descriptions: INTERESTED "The person expressed interest in the offer
or agreed to the next step."; NOT_INTERESTED "The person declined or showed no interest."; FOLLOW_UP_REQUIRED "The business
must follow up; the conversation is unresolved."; CALL_BACK_SCHEDULED "A callback at a specific time was agreed.";
TRANSFERRED "The call was transferred to a human."; DO_NOT_CONTACT "The person asked not to be contacted again.").
User message: the §3 transcript block. Forced tool:

```json
{"name":"select_disposition","parameters":{"type":"object","required":["tag","confidence","reason"],
 "properties":{"tag":{"type":"string","enum":["<allowed tags>"]},"confidence":{"type":"number","minimum":0,"maximum":1},
 "reason":{"type":"string","maxLength":300}}}}
```

### 4.3 System assignment (before/without LLM)

| Condition | Tag |
|---|---|
| `status = no_answer` or `answered_by = no_answer` | `NO_ANSWER` |
| `status = busy` | `BUSY` |
| `status = canceled` | `CANCELED` |
| `status = failed` | `FAILED` |
| connected, zero user turns, `ended_by = agent` | `AGENT_ENDED_CALL` |
| `answered_by = voicemail` | no tag from LLM; `disposition_tag` = `NO_ANSWER` |
| DNC was written during the call (opt-out guard rail) | `DO_NOT_CONTACT` (overrides LLM) |
| a cold/warm transfer bridged | `TRANSFERRED` when in candidate set (overrides LLM) |

## 5. Analysis schema and the analyze endpoint

### 5.1 `analysis_schema` (Send Call / inbound / batch)

Object mapping field name → type description string or JSON-Schema fragment. Accepted forms:
`{"email_address":"email","appointment_time":"YYYY-MM-DD HH:MM:SS","wants_demo":"boolean","budget_usd":"number"}` or
`{"wants_demo":{"type":"boolean","description":"…"}}`. Normalization to JSON Schema: `"string"|"email"|"phone"|"YYYY…"` →
`{"type":["string","null"],"description":<original>}`; `"boolean"` → `["boolean","null"]`; `"number"|"integer"` likewise;
arrays of strings `["a","b"]` → enum. Max 50 fields.

Prompt (verbatim `analysis_system.txt`):

```
You extract structured data from a phone call transcript.
Fill every field of the function arguments. Use null when the transcript does not contain the information.
Never guess values that were not stated or clearly implied. Dates and times must be interpreted in the call's timezone ({{timezone}}) and the call date ({{call_date}}).
Call the function record_analysis.
```

Forced tool `record_analysis` with `parameters` = the normalized schema (`additionalProperties:false`, all fields
`required`, nullable). Result → `call_analyses(kind='analysis_schema', schema, result)` and Call Object `analysis`.

### 5.2 `POST /v1/calls/{call_id}/analyze` (Bland-compatible)

Body: `{"goal": string ≤ 2000, "questions": [[question: string, type: string], …] (1–50)}`. Types: `"string"`,
`"boolean"`, `"number"`, `"YYYY-MM-DD"`, `"YYYY-MM-DD HH:MM:SS"`, `"human or voicemail"` (enum), or any free-form type
hint string. Synchronous (≤ 60 s; 504 `UPSTREAM_TIMEOUT` otherwise). The call must have ended; otherwise 409 `CONFLICT` with
message "Call is still in progress.".

Prompt (verbatim):

```
You answer questions about a phone call transcript for a business.
The business's goal for this analysis: {{goal}}
Answer each question using only the transcript. Use null if the transcript does not answer it.
Respect the requested answer type for each question.
Call the function answer_questions.
```

Forced tool `answer_questions` with `{"answers": array of length N, items: {"type":[…per question…,"null"]}}` built
positionally. Response:

```json
{"status":"success","message":"Success","answers":["2026-10-30 14:00:00", true, null],"credits_used":0.003}
```

Stored as `call_analyses(kind='qa', goal, questions, result)`. Billed at LLM token cost × 1.2 as `usage_events`
meter `llm_tokens_in`/`llm_tokens_out` (`24-billing-and-limits.md`).

## 6. Sentiment

Forced tool `record_sentiment`: `{"overall": enum["very_negative","negative","neutral","positive","very_positive"],
"score": number −1..1, "caller_frustration_detected": boolean, "notable_moments":[{"turn_index": int, "sentiment": enum, "note": string ≤ 200}] (≤ 5)}`.
System prompt (verbatim): `"You assess how the person (not the agent) felt during this phone call. Base your judgement only on what the person said and how the conversation went. Call the function record_sentiment."`

## 7. Citations (enterprise, `FEATURE_NOT_IN_PLAN` below `scale`)

### 7.1 Citation schemas API (`/v1/citation_schemas`, envelope)

| Method | Path | Body |
|---|---|---|
| GET | `/v1/citation_schemas` | — |
| POST | `/v1/citation_schemas` | `{name, description?, variables:[CitationVariable]}` |
| GET | `/v1/citation_schemas/{id}` | — |
| PATCH | `/v1/citation_schemas/{id}` | subset |
| DELETE | `/v1/citation_schemas/{id}` | — |

`CitationVariable`: `{name: ^[a-z][a-z0-9_]{0,63}$, type: "string"|"number"|"boolean"|"date"|"enum", description: string ≤ 500,
required: bool = false, enum_values?: string[], conditions?: [{"operator": "==="|"!=="|">"|">="|"<"|"<="|"contains"|"in", "value": any}]}`
(1–30 variables).

### 7.2 Extraction

Transcript presented with turn ids: `"[T{turn_index}|{start_ms}-{end_ms}|{role}] text"`. Prompt (verbatim
`citations_system.txt`):

```
You extract specific facts from a phone call transcript and cite the exact turns that support each fact.
Each transcript line starts with an identifier in square brackets: [T<turn number>|<start ms>-<end ms>|<speaker>].
For every variable, return its value (or null if not present) and the list of turn numbers whose text directly supports the value.
Only cite turns that contain the supporting words. Never cite a turn that does not support the value.
Call the function record_citations.
```

Tool schema: `{"citations":[{"variable_name": enum[names], "value": <typed per variable, nullable>, "turn_numbers": int[], "topics": string[] ≤ 5, "utterance_type": enum["statement","question","answer","confirmation","objection"]}]}`.

Post-processing per variable:
1. Discard turn numbers not present in the transcript.
2. Map each cited turn to `cited_utterances[]` items: `{turn_index, start_time: start_ms/1000, end_time: end_ms/1000,
   channel: "caller"|"agent", speaker_id: "caller"|"agent"|"human_agent", text, topics, utterance_type}`.
3. Evaluate `conditions` (all must hold; type-aware comparison; `contains` substring case-insensitive; `in` membership)
   → `conditions_met` (null when no conditions or value null).
4. Upsert `citations` rows (unique by call_id + schema + variable_name via delete-then-insert in one transaction).
5. Emit `citations.ready` with `data = {citation_schema_ids, citations:[{variable_name, variable_type, value, conditions_met, cited_utterances}]}`.

## 8. Dispositions API (`/v1/dispositions`, envelope)

GET list (built-ins included with `built_in: true`), POST `{tag, description}`, PATCH `{description}`, DELETE (built-ins
not deletable → 409 `CONFLICT`). Max 100 custom tags per org.

## 9. Evals workbench

### 9.1 API (`/v1/evals`, envelope)

| Method | Path | Body / notes |
|---|---|---|
| GET | `/v1/evals/setups` | list setups with latest version |
| POST | `/v1/evals/setups` | `{name, description?, criteria:[Criterion], pass_threshold?}` → creates setup + version 1 |
| GET | `/v1/evals/setups/{id}` | setup + versions |
| POST | `/v1/evals/setups/{id}/versions` | `{criteria, pass_threshold?}` → new immutable version |
| DELETE | `/v1/evals/setups/{id}` | soft delete not supported; hard delete blocked if runs exist (409) |
| POST | `/v1/evals/run` | `{eval_setup_id | eval_setup_version_id, call_ids: uuid[] (1–500)}` → `{data:{eval_run_ids:[…]}}`; runs `EvalRunWorkflow` per call asynchronously |
| GET | `/v1/evals/runs/{id}` | `EvalRun` |
| GET | `/v1/evals/runs?call_id=&eval_setup_id=&cursor=` | list |

`Criterion`: `{id: ^[a-z0-9_]{1,40}$, name, description, type: "pass_fail"|"score_1_5"|"score_0_100", weight: number > 0 (default 1), instructions: string ≤ 2000}` (1–25 criteria).

`post_call_evals` on Send Call: `{"workbench_setup_id": uuid}` or `{"workbench_setup_version_id": uuid}`; setup id resolves to its
latest version at dispatch time (stored in `calls.config`). Forces `record=true` and `retranscribe=true`.

### 9.2 Scoring prompt (verbatim `eval_system.txt`)

```
You are a strict quality reviewer for phone calls handled by an AI agent.
Evaluate the call against each criterion below independently. For each criterion:
- Read its instructions carefully.
- Decide the result using only evidence in the transcript.
- Give a short rationale (at most 2 sentences) and cite the turn numbers that support your decision.
Criteria:
{{criteria_block}}
Call the function record_evaluation with one result per criterion, in the same order.
```

`criteria_block`: for each criterion `"- id: {id}\n  name: {name}\n  type: {type}\n  description: {description}\n  instructions: {instructions}"`.
Transcript format as citations (§7.2). Tool: `{"results":[{"criterion_id": enum, "passed": boolean|null, "score": number|null, "rationale": string, "turn_numbers": int[]}]}`.

Score normalization to 0–100 per criterion: pass_fail → 100/0; score_1_5 → (s−1)×25; score_0_100 → s. Overall =
weighted mean. `passed = overall ≥ pass_threshold`. Store `eval_runs` and emit `evals.completed` with
`data = {eval_run_id, eval_setup_version_id, overall_score, passed, results}`. Missing/invalid criterion results from the
model → one retry with an error message appended; then the run fails with `status='failed'`, `error`.

### 9.3 QA scoring (org default)

Org setting `analysis.default_eval_setup_id`: when set, every connected call without explicit `post_call_evals` is evaluated
with it (still billed). Dashboard surfaces QA scores in call lists.

## 10. Memory and billing hooks

- `update_memory` calls `va_engine.memory.update_from_call(call_id)` (`20-memory-personas-agents.md`), which uses the
  final transcript and summary.
- `finalize_billing` calls `va_billing.metering.finalize_call(call_id)` (`24-billing-and-limits.md` §4).

## 11. Failure handling and observability

- Every activity logs start/finish with duration and emits Prometheus histogram `va_postcall_step_seconds{step}` and
  counter `va_postcall_step_failures_total{step}`.
- LLM structured-output parse failure: retry once with `"Your previous function call was invalid: <error>. Call the function again with valid arguments."` appended; then mark step failed.
- Workflow timeout (whole workflow): 30 min; on timeout `emit_completed` is executed by the parent `CallWorkflow` as a
  compensation so customers always receive `call.completed`.

## 12. Tests required

- Workflow ordering and parallelism with time-skipping env and mocked activities; skipping completed steps on replay;
  each step's skip condition; not-connected calls run only steps 1, 6 (system tag), 11, and 13.
- Recording: missing recording timeout path; S3 key format; carrier deletion setting.
- Re-transcription merge: synthetic word lists → expected turn texts, inserted missed utterances, renumbering, offset handling.
- Prompt snapshot tests (syrupy) for every verbatim prompt and rendered user message.
- Disposition: system table rows each covered; candidate set restriction; DNC/transfer overrides; invalid custom tag.
- Analysis schema normalization for every accepted form; analyze endpoint types and positional answers; in-progress call rejection.
- Citations: invalid turn numbers dropped, conditions operators (each), upsert idempotency, plan gating.
- Evals: normalization math, weighted overall, threshold, retry on invalid output, run API async lifecycle.
- E2E (simulator, scripted fake LLM): call with `record`, `analysis_schema`, `citation_schema_ids`, `post_call_evals`
  delivers `call.completed`, `citations.ready`, `evals.completed` webhooks with expected bodies.

## 13. Acceptance criteria

1. `call.completed` arrives ≤ 20 s (p95, simulator) after hangup without recording and contains summary and disposition.
2. All analysis outputs are reproducible from stored inputs (re-running a step yields identical DB state with the
   scripted fake LLM).
3. No step failure prevents `call.completed` delivery.
