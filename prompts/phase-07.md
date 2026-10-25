## Phase 07 — Conversational Pathways: Engine, API, Versions, and Testing

### Goal
Implement Bland-equivalent Conversational Pathways: the node/edge schema and validation, `{{var}}` templating, the
per-turn execution algorithm (global nodes with return stack, variable extraction, loop conditions, LLM routing with
fine-tuning examples, node execution, prompt assembly, speculative generation), node handlers that do not depend on
later phases (Default, Wait for Response, End Call, Transfer Call cold, Webhook, SMS, Press Button, Transfer Pathway,
Scheduling, Global), the full pathways API with drafts/versions/publish/promote/folders/import/export, text test chat,
unit tests, simulations, regression-on-publish, and integration as the media Brain for `pathway_id` calls.

### Read first
- `specs/11-pathways-engine.md` — entire file (schemas, algorithm, node semantics, templating, §9 EngineState).
- `specs/12-pathways-api.md` — entire file.
- `specs/10-llm-gateway.md` — router, loop-condition, extraction prompts and structured output contract.
- `specs/13-tools-and-dynamic-data.md` — webhook node execution shares the tool runtime HTTP layer (implement the shared
  HTTP/templating/JSONPath core here if not yet present; phase 08 adds tool CRUD and LLM function exposure).
- `specs/21-messaging-sms.md` — SMS node sending (voice-call SMS uses va_telnyx messaging; conversation threading
  arrives in phase 14).
- `specs/19-events-and-webhooks.md` — `call.route`, `call.extraction`, `call.loop_condition`, `call.webhook_node`.

### Out of scope (later phases)
Knowledge Base node and Custom Code node (phase 08), warm transfer (phase 12), persona/agent runners (phase 13),
dashboard editor UI (phase 17).

### Phase-specific constraints
- Engine logic is pure and transport-agnostic: the same `PathwayEngine` drives voice (phase 07), SMS and widget
  (phase 14). Side effects go through injected ports (`LlmPort`, `HttpPort`, `SmsPort`, `TelephonyPort`, `Clock`).
- Deterministic pathway_logs output (stable ordering, stable formatting) — snapshot tested.

### Phase exit gate
`make check && make e2e` pass, including e2e flows pathway_restaurant_template and webhook_node.
