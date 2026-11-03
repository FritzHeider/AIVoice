# Phase 06 — Event Bus, Webhooks, Live Streams, and Analytics Sink

- [x] P06-T02 Webhook router and payload formats — va_events DbWebhookRouter (transactional outbox, ON CONFLICT dedupe) + pure webhook_routing/webhook_payloads (bland, bland-live, batch, envelope, HIPAA strip), WEBHOOK_KEY drift-checked vs specs/19 §2, rollback test; make check: lint/typecheck/placeholders pass, 10866 passed + 1 unrelated media_worker flake (passes on rerun)
  - Spec refs: specs/19-events-and-webhooks.md (routing rules, Call Object, live body, batch body, envelope format)
  - Depends on: P03-T17, P05-T16
  - Deliverables: `WebhookRouter.route(event, session)` creating `webhook_deliveries` rows for per-call webhook (respecting `webhook_events`), batch status webhooks, org endpoints (glob match), conversation/widget webhooks; serializers for `bland`, `bland-live`, `envelope`
  - Acceptance: routing matrix tests; outbox atomicity test.

- [x] P06-T03 Webhook dispatcher service — va_webhooks Dispatcher (SKIP LOCKED claims, 200/pod + 20/host), signing (X-VA-Signature + X-Webhook-Signature), 10/15 s timeouts, 8 jittered retries, 410 auto-disable + admin email, va_common.safe_http pinning SSRF guard, call.webhook_delivery call log rows, metrics; review: make test 10952 passed / 7 skipped (99.06 % cov), lint/typecheck/placeholders pass
  - Spec refs: specs/19-events-and-webhooks.md (delivery semantics); specs/28-security.md (SSRF)
  - Depends on: P06-T02
  - Deliverables: `apps/webhook_dispatcher` poll loop with SKIP LOCKED, concurrency (global + per-host), signing headers (both formats), timeouts (10 s batch / 15 s others), retry schedule with jitter, 410 handling + endpoint auto-disable + admin email, DNS-resolving SSRF guard, per-attempt `call_events` rows, metrics
  - Acceptance: tests with a local receiver for success, 500 retry, timeout, redirect, 410, private-IP refusal (each blocked range), multi-pod no-duplicate test.

- [x] P06-T04a Webhook signing secret view and rotation (24 h dual-signing) — migration 0006, va_db.webhook_secrets, GET/rotate routes (withheld on replay, audited last4 only), dispatcher dual-sign until expiry; make test 10980 passed, 7 skipped; lint/typecheck/placeholders pass
  - Spec refs: specs/19-events-and-webhooks.md §8, §10 (signing-secret rows), §11 (dual-sign during rotation); specs/28-security.md §3; specs/04 §3.2 (secret row), §13 (`webhook.secret_rotate`)
  - Depends on: P06-T03, P02-T08
  - Deliverables: migration 0006 (current secret `created_at`, previous secret + expiry on `orgs`), `va_db.webhook_secrets` (read active secrets, rotate), `GET /v1/webhooks/signing-secret`, `POST /v1/webhooks/signing-secret/rotate` (shown once, withheld on idempotent replay, audited), dispatcher signs with both secrets until the previous one expires
  - Acceptance: API tests incl. RBAC (owner/admin only, API keys need `webhooks:write` + `webhooks.secret`), cross-tenant registration; dispatcher dual-sign test before and after the 24 h expiry; migration/schema parity tests green.

- [x] P06-T04b Webhook endpoint and delivery management API — endpoints CRUD (URL vetting, 20 cap, phi gate, audit), delete cancels queued deliveries (dispatcher keeps cancelled rows), signed test send via shared va_events delivery code, deliveries list/filters/cursor, replay; make test 11025 passed / 7 skipped, coverage 99.07 %, lint/typecheck/placeholders pass, TS 122 passed, openapi current, jscpd 0 clones
  - Spec refs: specs/19-events-and-webhooks.md §8 (replay, dead-letter view), §10 (endpoint and delivery rows)
  - Depends on: P06-T04a
  - Deliverables: endpoints CRUD (URL vetted, deleting an endpoint cancels its pending deliveries), test send (synthetic signed event through the shared SSRF-safe sender, returns the delivery result), deliveries list/filter (cursor, limit ≤ 100), replay (new attempt series, same event id)
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
