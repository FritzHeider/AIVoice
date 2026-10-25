## Phase 05 — Telephony Edge, Call Lifecycle, and Task-Mode Calling End-to-End

### Goal
Place and receive real phone calls (against the simulator; optionally live) with a single-prompt (`task`) agent:
Send Call API with full validation and resolved configuration, limits and credit holds, `CallWorkflow` in Temporal,
the telephony edge for Telnyx webhooks, the media worker accepting Telnyx media WebSockets and running the phase-04
pipeline with ElevenLabs STT/TTS and the LLM gateway, greetings, voicemail/AMD handling, cold transfers, DTMF,
recording, call end handling, inbound routing, and the call read endpoints. This phase makes the platform usable.

### Read first
- `specs/01-architecture.md` §2 and §3 — outbound/inbound/transfer lifecycles, ownership and failure handling.
- `specs/05-calls-api.md` — entire file (Send Call parameters, precedence, endpoints, status machine, §9 end reasons).
- `specs/06-telephony-telnyx.md` — webhooks, state mapping, dial/answer/transfer/record sequences, inbound routing.
- `specs/07-media-pipeline.md` — media worker endpoints, ownership lock, control channel, greeting/voicemail behaviour.
- `specs/11-pathways-engine.md` — Task-mode single prompt runner section (the Brain used in this phase).
- `specs/10-llm-gateway.md` — dialogue system prompt template, voicemail classifier prompt.
- `specs/24-billing-and-limits.md` — limit enforcement algorithms and credit holds (used at dispatch).
- `specs/23-guardrails-and-compliance.md` — DNC/blacklist check and calling-hours enforcement at dispatch.
- `specs/30-testing-and-evals.md` — end-to-end flow definitions listed in this phase's checklist.

### In scope
See checklist. Pathway/persona/agent *execution* arrives in phases 07 and 13, but the Send Call validator, runner
resolution for `pathway_id`/`pathway_version`, and the `CallContext` structure are completed here so later phases only
add Brains.

### Phase-specific constraints
- From this phase on, `make e2e` is part of the gate for any task touching call flow.
- The media worker must handle 50 concurrent simulated calls per process (verified in P05-T17).
- Every carrier interaction uses `command_id` for idempotency and `client_state` for correlation.

### Phase exit gate
`make check && make e2e` pass, including the e2e flows: outbound_basic, inbound_basic, voicemail_leave_message,
voicemail_hangup, amd_ivr_mode, barge_in, interruptibility_levels, cold_transfer, max_duration, stop_via_api,
worker_crash_recovery.
