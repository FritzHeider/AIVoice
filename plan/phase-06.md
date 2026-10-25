# Phase 06 — Event Bus, Webhooks, Live Streams, and Analytics Sink

- [ ] P06-T02 Webhook router and payload formats
  - Spec refs: specs/19-events-and-webhooks.md (routing rules, Call Object, live body, batch body, envelope format)
  - Depends on: P03-T17, P05-T16
  - Deliverables: `WebhookRouter.route(event, session)` creating `webhook_deliveries` rows for per-call webhook (respecting `webhook_events`), batch status webhooks, org endpoints (glob match), conversation/widget webhooks; serializers for `bland`, `bland-live`, `envelope`
  - Acceptance: routing matrix tests; outbox atomicity test.

- [ ] P06-T03 Webhook dispatcher service
  - Spec refs: specs/19-events-and-webhooks.md (delivery semantics); specs/28-security.md (SSRF)
  - Depends on: P06-T02
  - Deliverables: `apps/webhook_dispatcher` poll loop with SKIP LOCKED, concurrency (global + per-host), signing headers (both formats), timeouts (10 s batch / 15 s others), retry schedule with jitter, 410 handling + endpoint auto-disable + admin email, DNS-resolving SSRF guard, per-attempt `call_events` rows, metrics
  - Acceptance: tests with a local receiver for success, 500 retry, timeout, redirect, 410, private-IP refusal (each blocked range), multi-pod no-duplicate test.

- [ ] P06-T04 Webhook endpoint management API
  - Spec refs: specs/19-events-and-webhooks.md (webhook endpoint management API)
  - Depends on: P06-T03, P02-T08
  - Deliverables: endpoints CRUD, test send, deliveries list/filter, replay, signing secret view/rotate with 24 h dual-signing
  - Acceptance: API tests incl. RBAC and cross-tenant registration with the P02-T15 suite.

- [ ] P06-T05 Events sink to ClickHouse
  - Spec refs: specs/19-events-and-webhooks.md (consumers); specs/03-data-model.md (ClickHouse schema)
  - Depends on: P03-T17, P01-T12
  - Deliverables: `apps/events_sink` consumer group, batching (1 000 rows / 1 s), mapping to `call_events`, `call_turns`, `calls_fact`, `usage_events_raw`, retry with poison-message parking stream `events:dead`
  - Acceptance: integration test with testcontainers Redis + ClickHouse.

- [ ] P06-T06 Live streams: SSE and transcript WebSocket
  - Spec refs: specs/19-events-and-webhooks.md (live streams for clients)
  - Depends on: P03-T17
  - Deliverables: `GET /v1/calls/{id}/events/stream` (replay + tail, Last-Event-ID, close after completion), `POST /v1/calls/{id}/transcript/token`, `WS /v1/calls/{id}/transcript/ws`, org stream `GET /v1/events/stream` (dashboard session only)
  - Acceptance: tests for replay ordering, resume, token expiry/scope.

- [ ] P06-T07 Retrofit phase-05 emitters and latency events
  - Spec refs: specs/19-events-and-webhooks.md (event type catalog: call.latency messages); specs/07-media-pipeline.md (latency tracing)
  - Depends on: P06-T02
  - Deliverables: all call lifecycle emitters use the publisher; per-turn `call.latency` events with Bland-style stage messages; `webhook_events` filtering honored
  - Acceptance: e2e outbound_basic asserts the presence and order of expected event types.

- [ ] P06-T08 E2E: webhooks
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list); specs/19-events-and-webhooks.md (tests required)
  - Depends on: P06-T03, P06-T07
  - Deliverables: `tests_e2e/test_webhooks_call_completed_and_live.py` with signature-verifying receiver fixture
  - Acceptance: flow green in `make e2e`.

## Phase exit gate
`make check && make e2e` pass.
