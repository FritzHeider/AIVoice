# Phase 11 — Batches, Scheduling, Retries, DNC, Limits

- [ ] P11-T01 DNC management API
  - Spec refs: specs/23-guardrails-and-compliance.md (DNC management API, platform blacklist)
  - Depends on: P05-T04
  - Deliverables: `/v1/dnc` CRUD, bulk CSV import (async for > 1 000 rows), check endpoint, platform blacklist admin endpoints
  - Acceptance: API tests; import idempotency; normalization to E.164.

- [ ] P11-T02 Batch creation and validation
  - Spec refs: specs/17-batches-and-scheduling.md (create endpoint, validation phase)
  - Depends on: P05-T03, P11-T01
  - Deliverables: `POST /v2/batches/create` (call objects follow `/v1/calls` schema merged over global props), size limits (`BATCH_TOO_LARGE`), item persistence, validation (normalize, dedupe, DNC scrub, per-item config resolution) with skip reasons
  - Acceptance: tests for merge precedence and every skip reason.

- [ ] P11-T03 BatchWorkflow dispatch
  - Spec refs: specs/17-batches-and-scheduling.md (BatchWorkflow design, pacing, concurrency, continue-as-new, progress counters)
  - Depends on: P11-T02, P05-T06
  - Deliverables: workflow with token-bucket pacing, concurrency respecting org caps, child CallWorkflows, completion tracking via signals, continue-as-new every 1 000 items, counters persisted
  - Acceptance: time-skipping tests incl. org cap saturation and continue-as-new correctness.

- [ ] P11-T04 Calling windows and timezone resolution
  - Spec refs: specs/17-batches-and-scheduling.md §3; specs/23-guardrails-and-compliance.md (calling hours, TCPA quiet hours)
  - Depends on: P11-T03
  - Deliverables: per-number timezone resolution (phonenumbers timezone lookup, explicit override), weekday windows, holiday-free rules as specified, deferral scheduling
  - Acceptance: table tests across DST transitions and multi-timezone area codes.

- [ ] P11-T05 Retry policies
  - Spec refs: specs/17-batches-and-scheduling.md (retry policy); specs/05-calls-api.md (retry parameter)
  - Depends on: P11-T03
  - Deliverables: retry by outcome (no_answer, busy, voicemail, failed) with max attempts/backoff for batches; Send Call `retry` object (wait, voicemail_action, voicemail_message) creating child calls with `parent_call_id`; `RETRY_NOT_PROVIDED`
  - Acceptance: e2e retry_on_voicemail passes.

- [ ] P11-T06 Batch read/stop endpoints and status webhooks
  - Spec refs: specs/17-batches-and-scheduling.md (list, get, logs, stop); specs/19-events-and-webhooks.md §6
  - Depends on: P11-T03, P06-T02
  - Deliverables: `/v2/batches/list?take`, `/v2/batches/{id}`, `/v2/batches/{id}/logs`, `/v2/batches/{id}/stop` (`BATCH_NOT_STOPPABLE`), one status webhook per phase with counts
  - Acceptance: e2e batch_100_calls passes and receiver sees every phase in order.

- [ ] P11-T07 Scheduled single calls and cancellation
  - Spec refs: specs/17-batches-and-scheduling.md (scheduled single calls); specs/05-calls-api.md (start_time, stop)
  - Depends on: P05-T06
  - Deliverables: `start_time` parsing with offsets, durable timer, cancel via stop endpoint before dial, events
  - Acceptance: e2e scheduled_call passes.

- [ ] P11-T08 Limit completion
  - Spec refs: specs/24-billing-and-limits.md (limit enforcement: international, new-account, concurrency self-healing)
  - Depends on: P05-T04
  - Deliverables: international destination rules per plan/kyc, international hourly limits, new-account tightening, concurrency set expiry sweeper
  - Acceptance: tests for each rule with fake clock.

## Phase exit gate
`make check && make e2e` pass.
