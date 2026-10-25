## Phase 11 — Batches, Campaign Scheduling, Retries, DNC, and Limit Completion

### Goal
Run outbound campaigns at scale safely: `/v2/batches/*` with validation (E.164 normalization, dedupe, DNC scrub),
`BatchWorkflow` with pacing, concurrency, calling windows in callee local time, retries by outcome, continue-as-new for
large batches, status webhooks per phase, stop; scheduled single calls and cancellation; Send Call `retry`; the DNC
management API; and completion of all remaining limit rules.

### Read first
- `specs/17-batches-and-scheduling.md` — entire file (§3 BatchSchedule).
- `specs/23-guardrails-and-compliance.md` — DNC API, platform blacklist, calling hours, TCPA quiet hours.
- `specs/24-billing-and-limits.md` — caps and their interplay with batch concurrency.
- `specs/19-events-and-webhooks.md` §6 — batch status webhook body.

### Phase exit gate
`make check && make e2e` pass including flows batch_100_calls, scheduled_call, retry_on_voicemail, and a time-skipping
workflow test of a 5 000-item batch across two calling-window days with continue-as-new.
