# 20 — Memory, Personas, Agents, and Experiments

## 1. Overview
- **Memory** gives every contact persistent context (facts, summary, open items, recent messages) shared across
  voice, SMS, and widget.
- **Personas** are versioned reusable call presets (voice, prompt or pathway, tools, KBs, memory policy, call settings).
- **Agents** are semver-versioned bundles (pathway version + call settings) with environment pins, branches, and
  weighted A/B experiments.

Code: `packages/va_engine/src/va_engine/memory.py` (context formatting), `packages/va_analysis/src/va_analysis/memory_update.py`
(post-call update), `apps/api/src/va_api/routers/{memory,personas,agents,experiments,contacts}.py`. Envelope:
**envelope** format (31 §1).

## 2. Memory

### 2.1 Contact resolution (`resolve_contact(org_id, phone?, email?, external_id?)`)
1. Normalize phone to E.164 (`phonenumbers`, default region from org setting `default_country`, default `US`);
   lower-case email.
2. Look up by `external_id`, then phone, then email (first match wins).
3. If found and other identifiers are new and unclaimed by another contact, attach them (UPDATE). If an identifier
   belongs to another contact, do not merge; log `memory` event warning `CONTACT_IDENTIFIER_CONFLICT`.
4. If not found → INSERT contact with given identifiers (`ON CONFLICT DO NOTHING` + re-select for races).
Voice calls use the remote party number (`to` for outbound, `from` for inbound); SMS uses `user_number`; widget uses
`visitor_id`-derived `external_id = "widget:{widget_id}:{visitor_id}"` unless the page calls
`VoiceAgents.identify({email, phone, external_id})`.

### 2.2 Scope
`memories` row per (contact, persona) where persona is the call's persona when the runner is a persona and the
persona config `memory.scope = "persona"`; otherwise persona_id NULL (org-wide memory for the contact). Pathway
global config `memoryEnabled` and persona config `memory.enabled` turn memory on; call param `memory_id` (explicit
memory uuid) overrides resolution. Memory rows with `enabled=false` are neither injected nor updated.

### 2.3 Load at call start
`prefetch_call_context` resolves contact + memory and stores in `CallContext.memory` (summary, facts, open items,
last 10 recent messages). Budget 150 ms; failure → continue without memory (warning event).

### 2.4 Context injection format (verbatim, prompt section [5] of 11 §5.7)
```
## What you remember about this person
Summary: {summary}
Known facts:
- {fact_key}: {value}
Open items:
- {open_item text} (opened {YYYY-MM-DD})
Recent interactions:
- {YYYY-MM-DD} via {channel}: {role}: {text truncated to 160 chars}
Use this naturally; do not recite it. If something seems outdated, confirm it with the person.
```
Sections with no content are omitted; whole block ≤ 2 500 characters (facts first, then open items, then recent
interactions trimmed oldest-first, summary truncated last).

### 2.5 Update after each call/conversation (`PostCallWorkflow` / conversation close)
Activity `update_memory` with the analysis model, forced function `update_memory`:
parameters schema
```json
{"type":"object","properties":{
  "summary":{"type":"string","description":"Updated cumulative summary, max 120 words"},
  "facts_set":{"type":"object","additionalProperties":{"type":["string","number","boolean"]}},
  "facts_delete":{"type":"array","items":{"type":"string"}},
  "open_items_add":{"type":"array","items":{"type":"object","properties":{"text":{"type":"string"},"due_at":{"type":["string","null"]}},"required":["text"]}},
  "open_items_close":{"type":"array","items":{"type":"string","description":"ids of open items resolved"}}
},"required":["summary","facts_set","facts_delete","open_items_add","open_items_close"]}
```
System prompt (verbatim):
```
You maintain long-term memory about a customer for a business's AI agent. Given the existing memory and the latest
conversation transcript, produce updates. Keep facts durable and useful for future conversations (name,
preferences, account details mentioned, relationships, constraints). Do not store payment card numbers, government
ID numbers, passwords, or health details unless the business context explicitly requires them; never store full
card numbers. Close open items that were resolved. Add open items for promised follow-ups. Summary must be
cumulative and at most 120 words.
```
User message: `Existing memory:\n{json}\n\nConversation ({channel}, {date}):\n{transcript}`.
Apply in one transaction: write `memories` and one `memory_changes` row per change type; append the conversation's
turns to `recent_messages` (keep last 30); recompute `summary_embedding`. Redaction (`28-security.md`) runs on
the transcript before the LLM call when the org has `redaction.enabled`. Emits `call_events` category `memory`.

### 2.6 Memory API (paths mirror Bland's memory endpoints)

| Method | Path | Body / query | `data` |
|---|---|---|---|
| POST | `/v1/memory` | `{"phone_number?","email?","external_id?","persona_id?","name?"}` | Memory (creates contact+memory if needed) |
| GET | `/v1/memory` | `phone_number?`, `email?`, `external_id?`, `persona_id?`, `limit`, `cursor` | `[Memory]` |
| GET | `/v1/memory/{memory_id}` | — | Memory |
| GET | `/v1/memory/{memory_id}/messages` | `limit≤30` | `recent_messages` |
| POST | `/v1/memory/{memory_id}/summary` | `{"summary"}` | Memory |
| POST | `/v1/memory/{memory_id}/facts` | `{"set":{…},"delete":[…]}` | Memory |
| POST | `/v1/memory/{memory_id}/open_items` | `{"add":[{"text","due_at?"}],"close":[id]}` | Memory |
| GET | `/v1/memory/{memory_id}/context` | — | `{"context": "<formatted §2.4 text>"}` |
| GET | `/v1/memory/{memory_id}/changes` | `call_id?`, `limit`, `cursor` | `[MemoryChange]` |
| GET | `/v1/calls/{call_id}/memory_changes` | — | `[MemoryChange]` |
| POST | `/v1/memory/{memory_id}/reset` | — | Memory (cleared; `memory_changes` `reset`) |
| POST | `/v1/memory/{memory_id}/enable` / `disable` | — | Memory |
| POST | `/v1/memory/{memory_id}/add-call` | `{"call_id"}` (legacy) | Memory (runs update for that call synchronously, 30 s timeout) |
| DELETE | `/v1/memory/{memory_id}` | — | `{"deleted":true}` |

`Memory` JSON: `{"id","contact":{"id","phone_number","email","external_id","name"},"persona_id","summary","facts":{key:value},"open_items":[…],"enabled","updated_at","created_at"}`.
Contacts API: `GET/POST/PATCH/DELETE /v1/contacts[/{id}]` with `{phone_number, email, external_id, name, attributes}`;
deleting a contact deletes its memories (GDPR erasure path, `28-security.md`).

## 3. Personas

### 3.1 `PersonaConfig` (stored in `persona_versions.config`)
```python
class PersonaConfig(BaseModel):
    name: str                                   # display name the agent uses for itself
    role_description: str = ""                  # identity block injected as prompt section [2]
    runner: Literal["prompt", "pathway"]
    prompt: str | None = None                   # when runner == "prompt" (task)
    first_sentence: str | None = None
    pathway_id: UUID | None = None              # when runner == "pathway"
    pathway_version: int | Literal["production", "staging"] = "production"
    start_node_id: str | None = None
    voice: str | None = None                    # name or uuid
    voice_settings: VoiceSettings | None = None
    language: str = "babel-en"
    tools: list[str | InlineTool] = []
    knowledge_base_ids: list[str] = []
    memory: PersonaMemory = PersonaMemory()     # enabled: bool=False, scope: "persona"|"org"="persona"
    call_settings: PersonaCallSettings = PersonaCallSettings()
    sms: PersonaSmsSettings = PersonaSmsSettings()   # objective override, time_out_minutes
    widget: PersonaWidgetSettings = PersonaWidgetSettings()
    routing: list[PersonaRoute] = []            # [{"when": "<natural-language condition on request_data/channel>",
                                                #   "channel": "voice"|"sms"|"widget"|None, "pathway_id": ..., "pathway_version": ...}]
    guard_rails: list[GuardRailConfig] = []
    dispositions: list[str] = []
    summary_prompt: str | None = None
    analysis_schema: dict[str, Any] | None = None
    post_call_evals: dict[str, str] | None = None
    webhook: str | None = None
    webhook_events: list[str] = []
    metadata: dict[str, Any] = {}
```
`PersonaCallSettings` fields (all optional, same names/semantics as Send Call): `wait_for_greeting`, `temperature`,
`interruptibility`, `interruption_threshold`, `block_interruptions`, `background_track`, `noise_cancellation`,
`record`, `max_duration`, `voicemail`, `transfer_phone_number`, `transfer_list`, `timezone`, `keywords`,
`pronunciation_guide`, `dynamic_data`, `ivr_mode`, `ignore_button_press`, `model`.
`PersonaRoute.when` is evaluated deterministically when it uses the structured form
`{"field":"request_data.lang","operator":"==","value":"es"}`; free-text `when` is evaluated by the router model at call
start (≤ 300 ms budget, default route on timeout). First matching route wins.

### 3.2 Versions
Same draft/publish model as pathways but production only (`personas.production_version`, `draft_version`).
`persona_version` param: `"production"` (default) or `"draft"`; draft falls back to production if no draft changes
exist (draft identical hash) — Bland parity: "draft falls back to production".

### 3.3 Precedence with Send Call
- **Versioned personas** (`persona_id` with a version row): resolved `PersonaConfig` is applied **over** request
  fields (stored config wins) for every field the persona sets non-null; request fields fill unset ones. Exception:
  `persona_settings` request object (per-call override map) wins over everything; `pathway_id`/`pathway_version`
  request params override the persona's pathway routing only when `persona_settings` does not set them.
- **Legacy presets** (`personas` rows created through `/v1/personas/legacy` with `config.legacy=true`): request fields
  win over the preset.
- `request_data` and `metadata`: request value **replaces** persona value entirely when present (even `{}`).

### 3.4 Persona API

| Method | Path | Body | `data` |
|---|---|---|---|
| GET | `/v1/personas` | `limit`, `cursor`, `search?` | `[Persona]` |
| POST | `/v1/personas` | `{"name","description?","config": PersonaConfig}` | Persona (draft v1) |
| GET | `/v1/personas/{id}` | `version?` | Persona with config |
| PATCH | `/v1/personas/{id}` | `{"name?","description?","config?"}` (edits draft) | Persona |
| POST | `/v1/personas/{id}/publish` | `{"version_number?"}` | Persona |
| GET | `/v1/personas/{id}/versions` | — | `[PersonaVersion]` |
| POST | `/v1/personas/{id}/revert` | `{"version_number"}` | Persona |
| DELETE | `/v1/personas/{id}` | — | `{"deleted":true}` (409 if referenced unless force) |
| POST | `/v1/personas/legacy` | `{"name","config"}` | Persona with `legacy:true` |

## 4. Agents

### 4.1 Model
`agents` + `agent_versions` (03). An agent version = (`pathway_id`, `pathway_version` frozen integer) +
`call_settings` (Send Call subset as in `PersonaCallSettings` plus `voice`, `language`, `tools`, `dispositions`,
`summary_prompt`, `analysis_schema`, `guard_rails`, `webhook`, `webhook_events`, `first_sentence`, `task`).
Versions are immutable once created. `branch` defaults `main`.

### 4.2 Version resolution (`resolve_agent_version(agent, spec)`)
`spec` = Send Call `agent_version` (string) or omitted:
1. Omitted → env pin `production`; **eligible for experiment routing** (§5). If no production pin → error
   `NOT_FOUND` ("Agent has no production version").
2. `"production"` / `"staging"` → `env_pins[env]` (string semver) → version row; not eligible for experiments.
3. `"dev"` or `"latest"` → newest version on branch `main` by `created_at`.
4. Semver `"1.2.0"` → exact match.
5. `"branch:<name>"` (name 1–64 chars) → newest version on that branch.
6. Anything else → 400 `INVALID_INPUT` (field `agent_version`).
Env pins accept a semver; updating pins is `PUT /v1/agents/{id}/pins`.

### 4.3 Precedence
Agent version `call_settings` are defaults; explicit Send Call fields override them; `request_data`/`metadata`
replace. `agent_id` requires an org-scoped API key (all our keys are org-scoped; keys created with scope
`restricted:calls_only` are rejected → `AGENT_REQUIRES_ORG_KEY`). `agent_id` cannot be combined with
`persona_id`/`pathway_id`/`task` → `RUNNER_CONFLICT`.

### 4.4 Agent API

| Method | Path | Body | `data` |
|---|---|---|---|
| GET/POST | `/v1/agents` | POST `{"name","description?"}` | Agent / list |
| GET/PATCH/DELETE | `/v1/agents/{id}` | | Agent |
| GET | `/v1/agents/{id}/versions` | `branch?` | `[AgentVersion]` |
| POST | `/v1/agents/{id}/versions` | `{"semver","branch?","pathway_id?","pathway_version?","call_settings","changelog?"}` (semver must be greater than every existing semver on the same branch) | AgentVersion |
| GET | `/v1/agents/{id}/versions/{semver}` | | AgentVersion |
| PUT | `/v1/agents/{id}/pins` | `{"production?":"1.2.0","staging?":"1.3.0-rc.1"}` | Agent |
| POST | `/v1/agents/{id}/resolve` | `{"agent_version?"}` | `{"agent_version_id","semver","branch","experiment_arm?":null}` (debug) |

Audit: `agent.version.create`, `agent.pins.update`, `experiment.*`.

## 5. Experiments (A/B)

### 5.1 Config
`experiments` + `experiment_arms` (weights sum to 100; 2–5 arms; each arm references a version of the same agent).
At most one running experiment per agent (unique index).

### 5.2 Assignment
Only calls with `agent_id` and **omitted** `agent_version`. `bucket = int.from_bytes(sha256(f"{experiment_id}:{call_id}").digest()[:8], "big") % 100`;
arms ordered by `created_at`; cumulative weights select the arm. Stored on `calls.experiment_id`,
`experiment_arm_id`, `agent_version_id`. For inbound/SMS repeat contacts, org setting
`experiments.sticky_by_contact=true` uses `contact_id` instead of `call_id` in the hash.

### 5.3 Metrics (`GET /v1/experiments/{id}/results`)
Computed from ClickHouse `va.calls_fact` (+ Postgres `eval_runs` for eval scores) for calls with the experiment id:
| Metric | Definition |
|---|---|
| `disposition_rate` | share of completed calls whose `disposition_tag` ∈ `primary_metric_config.success_dispositions` |
| `transfer_rate` | share with `end_reason` in transfer reasons |
| `avg_eval_score` | mean `eval_runs.overall_score` |
| `avg_duration` | mean connected seconds |
Per arm: `n`, metric value, 95 % CI. Significance for proportions: two-proportion z-test of each arm vs arm 0
(`z = (p1-p0)/sqrt(p(1-p)(1/n1+1/n0))`, pooled p), p-value two-sided via `math.erfc(|z|/sqrt(2))`; means: Welch's
t-test using `scipy`-free implementation (Welch–Satterthwaite df, p via regularized incomplete beta implemented in
`va_analysis.stats`). Response marks `significant: p < 0.05 and min n ≥ 100 per arm`.

### 5.4 Lifecycle API
`POST /v1/experiments` (`{"agent_id","name","arms":[{"agent_version":"1.2.0","weight":50,"label":"A"},…],"primary_metric","primary_metric_config"}`),
`GET /v1/experiments`, `GET /v1/experiments/{id}`, `POST /v1/experiments/{id}/start`, `POST /v1/experiments/{id}/stop`,
`POST /v1/experiments/{id}/promote {"arm_id"}` (stops experiment and sets the agent's production pin to the arm's
version). Plan feature `experiments` required (`FEATURE_NOT_IN_PLAN` otherwise; enterprise and scale).

## 6. Tests required
- Contact resolution order, identifier attach, conflict non-merge, concurrent insert race.
- Memory scope selection, enable/disable, explicit `memory_id`.
- Context formatting snapshot and 2 500-char budgeting.
- Memory update application (each change type) with scripted fake LLM; redaction before LLM; recent_messages cap.
- Every memory/contacts endpoint incl. legacy add-call; tenant isolation.
- Persona precedence matrix (versioned vs legacy, persona_settings, request_data replacement), draft fallback, routes
  structured and free-text.
- Agent resolution for every spec form; invalid forms; pins; semver ordering per branch.
- Experiment assignment determinism and weight distribution (10 000 synthetic ids within ±2 %); sticky mode; stats
  functions against known values (z-test p for p0=0.10,n0=1000,p1=0.13,n1=1000 ≈ 0.0349; verify numerically in test).
- Promote updates pin and stops experiment.

## 7. Acceptance criteria
- A contact's second call (simulator) receives the memory block containing facts extracted from the first call.
- An agent with a running 50/50 experiment routes calls to both versions and results endpoint returns per-arm metrics.
