# Phase 14 — SMS, Omnichannel, 10DLC, Web Widget

- [ ] P14-T01 SMS send API and conversation creation
  - Spec refs: specs/21-messaging-sms.md (POST /v1/sms/send, conversations, SmsNumberConfig)
  - Depends on: P07-T08, P03-T08
  - Deliverables: `/v1/sms/send` with every parameter, conversation create/reuse (`new_conversation`, `time_out`), first message send via Telnyx, `messages` rows, `SMS_NOT_ENABLED` checks, `/v1/sms/webhook/update`
  - Acceptance: API tests against FakeTelnyx messaging.

- [ ] P14-T02 Inbound SMS and SmsConversationWorkflow
  - Spec refs: specs/21-messaging-sms.md (inbound handling, SmsConversationWorkflow, delivery status)
  - Depends on: P14-T01, P05-T01
  - Deliverables: edge handling of `message.received`/`message.sent`/`message.finalized`, conversation lookup, workflow with 2 s debounce, text-mode engine turn, reply send, status updates, events
  - Acceptance: Temporal tests for debounce merge and timeouts.

- [ ] P14-T03 Opt-out, HELP/START, segments, MMS
  - Spec refs: specs/21-messaging-sms.md (opt-out keywords, HELP/INFO, START, segment counting, MMS); specs/23-guardrails-and-compliance.md (opt-out to DNC)
  - Depends on: P14-T02
  - Deliverables: keyword handling with confirmations, DNC writes, GSM-7/UCS-2 segmenter, MMS media persistence to S3 with expiry-safe copies
  - Acceptance: segmenter golden tests; opt-out blocks future sends (`NUMBER_BLOCKED`).

- [ ] P14-T04 Conversations API and human takeover
  - Spec refs: specs/21-messaging-sms.md (conversations API, takeover, human message)
  - Depends on: P14-T02
  - Deliverables: list/get/messages/close/takeover/release/human message endpoints, `conversation.*` events and webhooks
  - Acceptance: API tests; AI pauses during takeover.

- [ ] P14-T05 10DLC registration
  - Spec refs: specs/21-messaging-sms.md §6
  - Depends on: P03-T08
  - Deliverables: brand/campaign API endpoints, `TenDlcRegistrationWorkflow` with Telnyx 10DLC calls and status polling/webhooks, number-to-campaign assignment, toll-free verification flow
  - Acceptance: FakeTelnyx-backed tests for submitted → verified/active and failure paths.

- [ ] P14-T06 Voice-call SMS integration and shared memory
  - Spec refs: specs/21-messaging-sms.md (shared memory with voice); specs/11-pathways-engine.md (SMS node); specs/05-calls-api.md (voicemail leave_message_and_sms)
  - Depends on: P14-T02, P13-T02
  - Deliverables: SMS sent from calls (SMS node, voicemail SMS, send_sms tool) threads into conversations; replies continue with call context; memory updates on conversation close
  - Acceptance: e2e sms_conversation passes (call → SMS follow-up → user reply handled with call context).

- [ ] P14-T07 Widget API, config, threads
  - Spec refs: specs/22-web-widget.md (widgets API, WidgetConfig, threads, live-agent message, custom components)
  - Depends on: P07-T09
  - Deliverables: `/v1/widget` CRUD, public key generation, threads listing, live-agent message endpoint, custom component registry
  - Acceptance: API tests and cross-tenant registration.

- [ ] P14-T08 Widget public session and chat transport
  - Spec refs: specs/22-web-widget.md (public widget protocol, allowed_domains, messages_per_minute, billing)
  - Depends on: P14-T07
  - Deliverables: `POST /widget/v1/session` (origin check, JWT), `WS /ws/widget` chat protocol on media-worker with HTTPS fallback, rate limiting, per-message billing events, `widget.message` events
  - Acceptance: e2e widget_chat passes; disallowed origin → `WIDGET_DOMAIN_NOT_ALLOWED`.

- [ ] P14-T09 Widget voice transport
  - Spec refs: specs/22-web-widget.md (voice mode); specs/07-media-pipeline.md (Transport protocol)
  - Depends on: P14-T08, P04-T12
  - Deliverables: `BrowserAudioTransport` (PCM16 16 kHz binary frames, `pcm_16000` TTS/STT configuration, barge-in, control messages), session limits
  - Acceptance: e2e widget_voice passes using a Python WS client streaming fixture audio.

- [ ] P14-T10 Widget embed script
  - Spec refs: specs/22-web-widget.md (embed script, theming, accessibility)
  - Depends on: P14-T08, P00-T13
  - Deliverables: `apps/widget` launcher + chat panel + voice button (AudioWorklet capture/playback), Shadow DOM styles, theming from WidgetConfig, reconnection, a11y; served by api at `/widget/v1/widget.js`
  - Acceptance: Vitest unit tests; Playwright test on a fixture host page performing a chat exchange against the e2e stack.

## Phase exit gate
`make check && make e2e` pass; widget Playwright test passes.
