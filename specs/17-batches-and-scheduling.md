# 17 — Batches, Campaign Scheduling, Retries, Calling Windows, Scheduled Calls

Package ownership: API `apps/api/src/va_api/routers/batches.py`; models `packages/va_domain/src/va_domain/batches.py`;
workflows `packages/va_workflows/src/va_workflows/batches.py` (`BatchWorkflow`, activities in
`batch_activities.py`); calling-window math `packages/va_compliance/src/va_compliance/calling_windows.py`;
CSV parsing `packages/va_domain/src/va_domain/batch_csv.py` (shared by API and dashboard upload endpoint).

All `/v2/batches*` endpoints use the **legacy** envelope (`31-error-catalog.md` §1).

## 1. Concepts

- A **batch** is a set of outbound calls (1–100 000) sharing optional `global` properties, an optional schedule,
  and a status webhook. Each entry is a `batch_items` row; each dispatched item creates a normal `calls` row and
  `CallWorkflow` (so all Send Call behaviour applies, `05-calls-api.md`).
- Item payload resolution: `payload = deep_merge(global, call_object)` where call-object keys win; `request_data` and
  `metadata` are merged one level deep (call-object keys win per key) — this differs from Send Call's agent-default
  replacement rule and is documented in the API reference.

## 2. `POST /v2/batches/create`

Body (`va_domain.batches.BatchCreateRequest`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `call_objects` | array of SendCallRequest-shaped objects (alias `calls`) | yes | 1–100 000 entries; each must contain `phone_number`; other fields optional, validated with the Send Call validator in "partial" mode, then validated again after merge with `global` in "full" mode |
| `global` | SendCallRequest-shaped object without `phone_number` | no | |
| `label` | string ≤ 200 | no | |
| `status_webhook` | URL | no | batch phase webhooks (`19-events-and-webhooks.md` §6) |
| `schedule` | BatchSchedule (§3) | no | |
| `encrypted_key` | string | no | BYO Telnyx (`16-phone-numbers.md` §7) applies to all items |
| `dedupe` | bool | no, default `true` | drop duplicate E.164 numbers keeping the first |
| `test_mode` | bool | no, default `false` | validate everything, create batch in status `completed` without dialing, return validation report |

Synchronous steps (target p95 ≤ 2 s for 10 000 items):

1. AuthZ: scope `batches:write` / role ≥ developer.
2. Size check → `BATCH_TOO_LARGE` (> 100 000). Body size limit for this route 64 MB.
3. Validate structurally (fast Pydantic pass). Field errors reported as `errors[]` with `field` like
   `call_objects[17].voicemail.action` (max 100 errors listed; message summarises the count).
4. Insert `batches(status='created')` and `batch_items` with `COPY` (asyncpg `copy_records_to_table`) in chunks of 5 000.
5. Emit `batch.created`; start `BatchWorkflow(batch_id)` (workflow id `batch-{batch_id}`).
6. Response 200:

```json
{"status":"success","batch_id":"0193…","message":"Batch created successfully.","calls_total":1200,
 "validation": {"skipped":0}}
```

`test_mode=true` response additionally includes `validation: {valid: n, skipped: m, skip_reasons: {"DNC": 3, "INVALID_PHONE_NUMBER": 1, "DUPLICATE": 4}}`
after running the validation phase inline.

### 2.0 `POST /v2/batches/validate`

Same body as create. Runs the validation phase synchronously without persisting (max 10 000 items; larger → validate the
first 10 000 and report `truncated: true`). Response `{"status":"success","valid": n, "skipped": [{"ordinal", "reason"}],
"normalized_preview": [first 20 resolved call objects], "estimated_cost": dollars, "truncated": bool}`. Used by the
dashboard CSV wizard before create.

### 2.1 CSV upload (dashboard and API)

`POST /v2/batches/csv` (multipart: `file` ≤ 64 MB UTF-8 CSV, `mapping` JSON, plus all non-array fields of §2 as form
fields with JSON values). `mapping`: `{"phone_number": "<column>", "request_data": {"first_name": "<column>", …},
"metadata": {"crm_id": "<column>"}, "external_id": "<column>"?, "first_sentence": "<column>"?, "voice": "<column>"?,
"start_time": "<column>"?}`. Rows convert to `call_objects` then the §2 flow runs. Header row required; delimiter
sniffed among `, ; \t`. Max 100 000 rows. Errors include `field = "row 42: phone_number"`.
`POST /v2/batches/csv/preview` returns the first 20 converted call objects and detected columns without creating anything.

## 3. BatchSchedule (normative; stored in `batches.schedule`)

```python
class CallingWindow(BaseModel):
    days: list[Literal["mon","tue","wed","thu","fri","sat","sun"]]   # at least one
    start: str   # "HH:MM" 24h local to callee
    end: str     # "HH:MM", must be > start (no overnight windows; split into two windows instead)

class RetryPolicy(BaseModel):
    max_attempts: int = 1                    # total attempts including the first, 1–5
    retry_on: list[Literal["no_answer","busy","voicemail","failed"]] = ["no_answer","busy"]
    backoff_minutes: list[int] = [60, 240, 1440]   # delay before attempt 2, 3, 4 … (last value repeats); each 5–10080
    leave_voicemail_on_last_attempt: bool = True   # earlier attempts use voicemail action "hangup" when retry_on contains "voicemail"

class BatchSchedule(BaseModel):
    start_at: datetime | None = None         # UTC; null = immediately
    end_at: datetime | None = None           # stop dispatching new attempts after this instant (batch → completed with remaining items canceled)
    timezone_mode: Literal["callee","fixed"] = "callee"
    timezone: str | None = None              # IANA; required when timezone_mode="fixed"; fallback when callee tz unknown (default org settings.timezone or "America/New_York")
    windows: list[CallingWindow] = [CallingWindow(days=["mon","tue","wed","thu","fri","sat","sun"], start="08:00", end="21:00")]
    max_concurrency: int | None = None       # per-batch concurrent calls cap; effective = min(this, org concurrency cap − 0)
    calls_per_minute: int | None = None      # pacing; 1–6000; null = no pacing beyond concurrency
    retry: RetryPolicy = RetryPolicy()
    respect_holidays: bool = False           # skip US federal holidays (fixed-date and nth-weekday rules table in va_compliance.holidays: New Year's Day, MLK Day, Presidents Day, Memorial Day, Juneteenth, Independence Day, Labor Day, Columbus Day, Veterans Day, Thanksgiving, Christmas; observed-date shifting for weekends)
```

Validation: windows non-overlapping per day, `end_at > start_at`, `timezone` valid IANA via `zoneinfo`.

### 3.1 Callee timezone resolution

`va_compliance.calling_windows.callee_timezone(e164, explicit: str | None, fallback: str) -> str`:

1. Explicit per-item `timezone` field in the call object → use it.
2. `phonenumbers.timezone.time_zones_for_number(parsed)`; if exactly one zone → use it.
3. If multiple zones (a US NPA spanning zones): choose the **most restrictive** — the zone whose current local time
   makes the window open latest and close earliest. Implementation: evaluate the window check in every candidate zone
   and treat the number as "in window" only if it is in window in all of them.
4. If none (`Etc/Unknown`) → `fallback`.

### 3.2 Window check

`in_window(now_utc, tz, windows, respect_holidays) -> bool` and `next_window_start(now_utc, tz, windows, ...) -> datetime`
(UTC) used for item rescheduling. Pure functions, property-tested with Hypothesis (DST transitions in `America/New_York`,
`Europe/London`, `Australia/Lord_Howe` half-hour DST).

## 4. Read and control endpoints

### 4.1 `GET /v2/batches/list?take=<1–500, default 50>&cursor=<opaque>`

```json
{"status":"success","batches":[{"batch_id":"…","label":"…","status":"in_progress","calls_total":1200,
 "calls_dispatched":340,"calls_completed":290,"calls_successful":251,"calls_failed":39,"created_at":"…",
 "started_at":"…","completed_at":null}], "next_cursor":"…"}
```

### 4.2 `GET /v2/batches/{batch_id}`

Batch fields above plus `global`, `schedule`, `status_webhook`, `failure_code`, `failure_reason`, and
`analysis: {answered: n, voicemail: n, no_answer: n, busy: n, failed: n, avg_call_length: minutes, total_cost: dollars,
dispositions: {"INTERESTED": n, …}}` computed from `calls` (SQL aggregate, cached 30 s in Redis `batchstats:{id}`).

### 4.3 `GET /v2/batches/{batch_id}/logs?status=&limit=&cursor=`

Per-item rows: `{ordinal, phone_number, status, skip_reason, attempts, call_id, last_call_status, answered_by,
disposition_tag, next_attempt_at}`; `status` filter over `batch_items.status`.

### 4.4 `GET /v2/batches/{batch_id}/calls`

Paginated Call Objects (summary form: without transcripts) for the batch.

### 4.5 `POST /v2/batches/{batch_id}/stop`

Signals `BatchWorkflow.stop()`. Pending/scheduled items → `canceled`; in-progress calls continue unless body
`{"hangup_active": true}` (then each active call receives `stop` on its control channel). Terminal batch →
409 `BATCH_NOT_STOPPABLE`. Response `{"status":"success","message":"Batch stopped.","batch_id":"…"}`.

### 4.6 Legacy compatibility

`POST /v1/batches` (Bland v1: `base_prompt`, `call_data[]` with `phone_number` + vars, `label`, `campaign_id`) maps to
`/v2/batches/create` (`global.task = base_prompt`, each `call_data` entry → call object with `request_data` = the entry
minus `phone_number`). `GET /v1/batches/{id}` maps to §4.2. `POST /v1/batches/{id}/stop` maps to §4.5.

## 5. BatchWorkflow (Temporal)

Task queue `va-main`. Deterministic workflow code; all I/O in activities. Signals: `stop(hangup_active: bool)`,
`item_finished(item_id, call_status, answered_by)` (sent by `CallWorkflow` on completion when `batch_item_id` set).
Queries: `progress() -> BatchProgress`.

```
state: batch_id, phase, cursor_ordinal, in_flight: set[item_id], stopped, counters, items_since_can

run(batch_id):
  1. if schedule.start_at > now: phase='scheduled'; activity set_status('scheduled'); emit batch status webhook; await timer until start_at (or stop signal)
  2. phase='validating'; activity validate_items(batch_id) in pages of 5 000:
       for each pending item: normalize phone (E.164 via phonenumbers, default region from org settings.default_country or "US")
         → invalid: status='skipped', skip_reason='INVALID_PHONE_NUMBER'
       dedupe (when enabled): keep lowest ordinal per E.164 → others skipped 'DUPLICATE'
       DNC scrub (org + platform dnc_entries, unexpired) → 'DNC'
       international check vs org plan → 'INTERNATIONAL_NOT_ENABLED'
       re-validate merged payload (full Send Call validator) → 'INVALID_INPUT:<field>'
     update counters; if valid == 0 → phase='failed', failure_code='NO_VALID_CALLS', reason → terminal
  3. phase='dispatching' → emit; then phase='in_progress' after first dispatch → emit
  4. dispatch loop:
       while not stopped:
         if schedule.end_at and now >= end_at: cancel remaining pending items (activity) → break
         capacity = activity get_capacity(batch_id)   # see §5.1
         if capacity == 0: await timer(2 s) or signal; continue
         items = activity claim_due_items(batch_id, limit=min(capacity, 200))   # status pending AND (next_attempt_at IS NULL OR <= now)
                                                                                 # and window-open for callee tz; items outside window get next_attempt_at = next_window_start
         for item in items: start child CallWorkflow via activity dispatch_item(item) (creates calls row via the same service used by POST /v1/calls with source='batch'),
                            in_flight.add(item); counters.dispatched += 1
         if no items claimable and in_flight empty and no pending items with future next_attempt_at: break
         if no items claimable: await timer(min(next due next_attempt_at − now, 60 s)) or signal
         items_since_can += len(items); if items_since_can >= 1000: continue_as_new(state)
  5. drain: await until in_flight empty (signals) or stopped with hangup
  6. phase = 'stopped' if stopped else 'completed'; activity finalize(batch_id) (counters, completed_at); emit terminal webhook

on item_finished(item_id, call_status, answered_by):
  in_flight.discard(item_id)
  outcome = map(call_status, answered_by) → one of success | no_answer | busy | voicemail | failed
  success  ⇔ call status completed and answered_by in (human, unknown, ivr)
  if outcome in retry.retry_on and attempts < retry.max_attempts and not stopped:
       activity schedule_retry(item_id, delay = backoff_minutes[min(attempts-1, len-1)])  → item.status='pending', next_attempt_at
  else: item.status = 'completed' if success else 'failed'; counters.completed += 1; successful/failed += 1
```

### 5.1 Capacity and pacing (`get_capacity` activity)

```
org_free   = org concurrency cap − ZCARD(cap:org:{org_id}:concurrency after pruning expired)   (24-billing-and-limits §6)
batch_free = (schedule.max_concurrency or ∞) − len(in_flight)
pace_free  = token bucket "batchpace:{batch_id}" with rate calls_per_minute/60 per second, burst = max(1, calls_per_minute/6)
             (Redis Lua, same script as API rate limiting, returns tokens available without consuming; claim consumes)
daily_free = org daily cap − daily counter; hourly_free likewise
capacity   = max(0, min(org_free, batch_free, pace_free, daily_free, hourly_free))
```

If `daily_free == 0` the workflow sleeps until the next UTC midnight + 1 min; `hourly_free == 0` until next hour. Each such
pause emits `call.dispatch_blocked` with `data.reason` (`DAILY_CAP_REACHED`, `HOURLY_CAP_REACHED`) once per pause and a
batch status webhook is **not** sent (phase unchanged).

### 5.2 Credits

Before dispatching each claim, `dispatch_item` performs the normal credit hold. `INSUFFICIENT_CREDITS` → the batch
pauses (status stays `in_progress`), emits `billing.low_balance`, and retries every 5 min; after 24 h without credit the
batch fails with `failure_code='INSUFFICIENT_CREDITS'`.

### 5.3 Voicemail on retries

When `retry_on` contains `voicemail` and the attempt is not the last, `dispatch_item` overrides the item's
`voicemail.action` to `hangup`; on the final attempt the original action applies (or `leave_message` when
`leave_voicemail_on_last_attempt` and a message is configured).

### 5.4 Retries of individual calls (non-batch)

Send Call `retry: {wait, voicemail_action, voicemail_message}` (Bland): `CallWorkflow` on voicemail outcome starts a new
call (`parent_call_id` = original) after `wait` seconds (10–86 400) with `voicemail.action = voicemail_action` and
`voicemail.message = voicemail_message`. Missing `wait` → 400 `RETRY_NOT_PROVIDED`. Max one automatic retry per Send Call.

## 6. Scheduled single calls

- `start_time` on Send Call (`"YYYY-MM-DD HH:MM:SS -HH:MM"`, Bland format; ISO 8601 also accepted) → `calls.status='scheduled'`,
  `calls.start_time`; `CallWorkflow` sleeps until then. Max 90 days ahead (`INVALID_INPUT` otherwise). Past times
  (≤ now + 5 s) dispatch immediately.
- Cancel: `POST /v1/calls/{call_id}/stop` on a scheduled call → workflow signal `cancel`; status `canceled`;
  `call_ended_by='API'`; hold released; `call.completed` webhook sent with `status: "canceled"`.
- `GET /v1/calls?status=scheduled` lists them.

## 7. Calling-hours enforcement

- Batches: always enforce their windows (default 08:00–21:00 callee local, every day), which match TCPA quiet hours.
- Single calls: enforced only when org setting `compliance.enforce_calling_hours=true` (window from
  `compliance.calling_hours`, default 08:00–21:00 all days, callee local) → 400 `OUTSIDE_CALLING_HOURS` when called
  outside; with `start_time` the check applies to the scheduled instant.
- State-specific stricter hours (applied on top when `compliance.state_quiet_hours=true`, default true): a table in
  `va_compliance.state_rules.STATE_CALLING_HOURS` keyed by US state. The initial table contains exactly these entries:
  FL 08:00–20:00 all days, max 3 calls per 24 h per number; OK 08:00–20:00 all days, max 3 per 24 h; MD max 3 per 24 h;
  WA 08:00–20:00 all days; TX Mon–Sat 09:00–21:00 and Sun 12:00–21:00; AL, LA, MS no calls on Sunday; UT 08:00–21:00.
  The table is data (`state_rules.json`) and each entry carries `statute`, `source_url`, and `last_verified`; the implementer
  verifies each entry against the cited statute when creating the file, and a unit test asserts every entry has all three
  fields. Per-day call frequency caps use Redis `freq:{org_id}:{e164}:{yyyymmdd}`.
- Inbound calls are never blocked by calling hours.

## 8. Interplay with org caps

Batch dispatch respects exactly the same caps as API calls (daily, hourly, concurrency, international, same-number
10 s rule, new-account limits). A cap hit inside a batch never fails items; it delays them. API single calls and
batches compete for the same concurrency pool; org setting `batch_concurrency_share` (0.1–1.0, default 1.0) limits
batches to that fraction of the org concurrency cap so API traffic keeps headroom.

## 9. Tests required

- Request validation: merge semantics (deep, request_data per-key), field error paths with indexes, size limits,
  `test_mode` report counts, CSV mapping (delimiters, BOM, quoted fields, missing column, 100 001 rows rejected).
- Calling windows: Hypothesis property tests (`in_window` ⇔ `next_window_start(now) == now`), DST cases, multi-zone NPA
  most-restrictive rule, holidays.
- BatchWorkflow with `WorkflowEnvironment.start_time_skipping()` and mocked activities: scheduled start, validation
  skip reasons, pacing (calls_per_minute=60 over 5 simulated minutes dispatches 300 ± 1), concurrency cap respected at
  all times, retry policy (no_answer twice then success → attempts=3, completed), voicemail override on non-final
  attempts, `end_at` cancels pending, stop with and without `hangup_active`, continue-as-new at 1 000 preserves counters
  and in-flight set, daily cap pause to next midnight, credit pause and 24 h failure.
- Status webhook sequence: exactly one POST per phase in order; terminal counts equal DB aggregates.
- Scheduled single call: dispatches at time (time-skipping), cancel before start, past start dispatches now, >90 days rejected.
- Calling hours: org enforcement on/off, state rules, frequency cap.
- E2E (simulator): 50-item batch with mixed outcomes (fake Telnyx scripts: 30 answer, 10 no_answer, 5 busy, 5 voicemail)
  and retry policy completes with expected counters; `tests_e2e/test_batch_campaign.py`.

## 10. Acceptance criteria

1. A 1 000-call batch with `calls_per_minute=120` and `max_concurrency=50` never exceeds 50 concurrent calls and finishes
   dispatch in 8–9 simulated minutes (time-skipping test).
2. No call is ever placed outside the configured window in the callee's timezone (property test over 10 000 random
   numbers/times).
3. Batch counters, status webhook terminal body, `GET /v2/batches/{id}`, and `calls` table aggregates agree exactly.
4. Stopping a batch prevents any new dial within 2 s (measured in e2e via fake Telnyx request log).
