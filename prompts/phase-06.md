## Phase 06 — Event Bus, Webhooks, Live Streams, and Analytics Sink

### Goal
Complete the event system on top of the `va_events` publisher built in phase 03: the transactional webhook outbox and router with all three payload formats, the
`webhook-dispatcher` service with signing, retries, SSRF protection, DLQ and replay, org webhook endpoint management,
SSE/WebSocket live streams for calls, and the `events-sink` service feeding ClickHouse.

### Read first
- `specs/19-events-and-webhooks.md` — entire file; primary contract.
- `specs/03-data-model.md` §9 (`webhook_endpoints`, `webhook_deliveries`), §11 (Redis keys/streams), §12 (ClickHouse).
- `specs/27-observability.md` — metrics for dispatcher and sink.
- `specs/28-security.md` — SSRF rules, signing secret encryption and rotation.

### In scope
Everything in specs/19, plus retrofitting all events emitted in phase 05 to go through the publisher and router, and
honoring `webhook` + `webhook_events` on calls.

### Phase-specific constraints
- Outbox inserts happen in the same transaction as the state change they describe; tests prove rollback suppresses
  delivery rows.
- The dispatcher is horizontally scalable (multiple pods) with no duplicate concurrent delivery of the same row.

### Phase exit gate
`make check && make e2e` pass, including a new e2e flow `webhooks_call_completed_and_live` (receiver fixture verifies
signatures, live categories filtering, retries after a 500, and final Call Object fields).
