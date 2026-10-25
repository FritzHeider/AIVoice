## Phase 14 — SMS Conversations, Omnichannel Context, 10DLC, and the Web Widget (Chat + Voice)

### Goal
Extend the same agents beyond phone calls: Telnyx SMS/MMS conversations driven by the text-mode pathway/persona/agent
engine with shared memory, opt-out handling, segment accounting, 10DLC brand/campaign registration, conversations API
with human takeover, and the embeddable web widget with chat and browser voice over our WebSocket audio transport.

### Read first
- `specs/21-messaging-sms.md` — entire file (§3 SmsNumberConfig, §6 10DLC).
- `specs/22-web-widget.md` — entire file (§3 WidgetConfig).
- `specs/11-pathways-engine.md` — text-mode degradations per node type.
- `specs/07-media-pipeline.md` — Transport protocol (widget voice reuses the pipeline).
- `specs/20-memory-personas-agents.md` — shared memory across channels.
- `specs/23-guardrails-and-compliance.md` — SMS opt-out → DNC.

### Phase exit gate
`make check && make e2e` pass including sms_conversation, widget_chat, widget_voice; `make test-ts` passes for the
widget; Playwright widget test passes.
