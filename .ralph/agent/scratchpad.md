
## Plan P05-T01 — Telephony edge: webhook intake

Spec refs: specs/06 §1 (webhook URLs / key per path), §3 (client_state), §4/§4.1 (+ Decision P03-T09), §5 last row;
specs/01 §2.1 step 4, §3; specs/03 §2 (system repos) and §4 (`telnyx_events`); specs/27 §3.2 metrics.
Existing building blocks: `va_telnyx.verify_webhook/load_public_key/WebhookVerificationError`,
`va_telnyx.webhooks.parse_envelope/parse_payload/PAYLOAD_MODELS/UnknownEventPayload`, `va_telnyx.client_state.decode`,
`va_db.repositories.TelnyxEventRepo` (record/mark_processed), `va_db.models.TelnyxEvent/TelnyxAccount`,
metrics `va_telnyx_webhooks_total{event_type,result}` + `va_telnyx_webhook_processing_ms{event_type}` (catalog exists).

### Decisions (write as `> Decision (2026-11-01): webhook intake (P05-T01).` under specs/06 §4.1, after the P03-T09 block)
1. Verification key is chosen by the ROUTE, never by body content (body is untrusted until verified):
   `/telnyx/{voice|messaging}` → platform `TELNYX_PUBLIC_KEY`; `/telnyx/{voice|messaging}/{telnyx_account_id}` → that
   BYO account's `public_key` (status `active` or `pending`; unknown/`deleted`/non-uuid id → 401 `unknown_account`, same
   counter). `connection_id`/`client_state` are used only AFTER verification, for attribution: org_id = BYO account's
   org (path) else `client_state.org_id`; call_id = `client_state.call_id`; call_control_id = `payload.call_control_id`.
   A client_state of the wrong org on a BYO path is ignored (org stays the account's org, call_id dropped).
   BYO public keys cached in-process 5 min (same TTL as BYO API keys, specs/06 §1), keyed by account id.
2. Step 7 becomes asynchronous (plan deliverable "fast 200 ACK"): after the dedupe insert the edge enqueues the stored
   event to an in-process `EventProcessor` and answers 200 at once. Ordering: N shard workers (default 8), shard =
   crc32(call_control_id or telnyx_event_id) % N so one leg's events are processed in arrival order. Backpressure:
   each shard has a bounded queue (default 256); when full the event stays unprocessed in Postgres
   (`processed_at IS NULL`) and the reprocessor picks it up — never 503 after an insert (a Telnyx retry would be a
   duplicate and be dropped). Before insert, if the process is draining (lifespan shutdown) → 503 (Telnyx retries).
3. Reprocessor (`TelnyxEventReprocessor`, every 5 s): reclaims rows with `processed_at IS NULL AND created_at < now-30s`
   (lost from queue / overflow / crash) and rows with `process_error IS NOT NULL AND process_attempts < 5`; enqueues them
   (skip if shard queue full). Needs a new column `telnyx_events.process_attempts int NOT NULL DEFAULT 0`
   (migration 0005, listed LAST in the specs/03 §4 DDL, see AGENTS.md P02-T06a note on attnum).
4. Metrics: `result` label of `va_telnyx_webhooks_total` gains `unhandled` (specs/27 §3.2 table edit): event types not in
   `va_telnyx.webhooks.PAYLOAD_MODELS` are stored and counted `unhandled` (the spec §5 "telnyx_webhook_unhandled_total"
   and §4.1 "telnyx_webhook_invalid_signature_total" names map to this catalog counter; say so in the decision).
   Invalid signature / unknown account → `event_type="unverified", result="invalid_signature"`; verified but
   unparseable envelope → 400, `event_type="invalid", result="error"`, nothing stored; duplicate → `result="duplicate"`;
   processing failure → `result="error"`; success → `processed`. Histogram observed per processed event (enqueue → done).
5. Handlers: `EventProcessor` dispatches by `event_type` to a registry `dict[str, EventHandler]` (empty in T01; P05-T02
   registers voice mapping, phase 21 messaging). Known type without handler → marked processed, `processed`; unknown
   type → marked processed, `unhandled`. `parse_payload` errors → process_error recorded (`WebhookParseError: ...`).

### Files
- `packages/va_db/migrations/versions/0005_telnyx_event_attempts.py`: `ALTER TABLE telnyx_events ADD COLUMN
  process_attempts integer NOT NULL DEFAULT 0`; downgrade drops it. Same grant situation as table (already granted).
- `specs/03-data-model.md` §4 DDL: add `process_attempts  integer NOT NULL DEFAULT 0,` as the LAST column of
  telnyx_events (after updated_at) with comment `-- processing attempts (TelnyxEventReprocessor, specs/06 §4.1)`.
- `packages/va_db/src/va_db/models/telephony.py` `TelnyxEvent.process_attempts: Mapped[int] = int_column(0)` (last).
- `packages/va_db/src/va_db/repositories/telnyx_events.py`:
  - `record(event)`: org_id inserted via `select(Org.id).where(Org.id == event.org_id).scalar_subquery()` when not None
    (a deleted org must not make the insert fail on the FK; attribution then NULL).
  - `mark_processed(event_id, *, now, error=None)`: also `process_attempts = process_attempts + 1`.
  - `due_for_retry(*, now, stale_after: timedelta, max_attempts: int, limit: int = 200) -> list[TelnyxEvent]`
    (both reclaim conditions, ORDER BY created_at).
- New system repo `TelnyxAccountLookupRepo.public_key(account_id) -> str | None` (status IN active/pending) in
  `va_db/repositories/telnyx_accounts.py`, exported from `va_db.repositories`; add to specs/03 §2 `va_system` bullet +
  a `> Decision (2026-11-01)` paragraph there (webhook arrives before any org is known); update
  `packages/va_db/tests/test_repositories.py` count (11 system repos).
- `apps/telephony_edge/pyproject.toml`: deps `va-db`, `va-telnyx` (+ `[tool.uv.sources]` workspace entries); `uv lock`.
- `apps/telephony_edge/src/va_edge/settings.py`: `EdgeSettings(... )` add `telnyx_public_key: str = ""`,
  `edge_process_shards: int`? NO — no new env vars; keep shard/queue sizes as constructor args with constants
  (`SHARDS=8`, `QUEUE_SIZE=256`). Lifespan raises `RuntimeError("TELNYX_PUBLIC_KEY is not configured")` when empty
  (load via `load_public_key` → ValueError for bad key) — like api pepper check.
- `apps/telephony_edge/src/va_edge/keys.py`: `class WebhookKeys(platform: VerifyKey, lookup: Callable[[uuid.UUID],
  Awaitable[str|None]], clock: Clock, ttl_s=300)`; `async resolve(account_id: str | None) -> ResolvedKey(key, org_id?)`
  — actually lookup returns `(public_key, org_id)`; repo method `verification_key(account_id) -> tuple[str, UUID] |
  None`. Raises `UnknownAccountError`.
- `apps/telephony_edge/src/va_edge/intake.py`: `StoredEvent` (frozen dataclass: id, telnyx_event_id, event_type,
  occurred_at, payload, org_id, call_id, call_control_id, channel `voice|messaging`); `attribute(event, channel,
  account_org) -> NewTelnyxEvent`; `async receive(request, channel, account_id) -> Response` doing §4.1 steps 1–6 +
  enqueue. Raw body via `await request.body()`; `now` from injected clock; `verify_webhook(raw, request.headers, key, now)`.
- `apps/telephony_edge/src/va_edge/processor.py`: `EventHandler = Callable[[StoredEvent, EventPayload], Awaitable[None]]`;
  `class EventProcessor(database, clock, metrics, handlers: Mapping[str, EventHandler], shards=8, queue_size=256)`
  with `offer(event) -> bool` (False = queue full, logged `telnyx.event_deferred`), `run()` (TaskGroup of shard workers;
  supervised), `join()` test seam (`await queue.join()` for all shards). Each item: parse_payload → handler → mark
  processed in `system_session()`; handler exception → mark with `process_error=f"{type(exc).__name__}: {exc}"[:2000]`,
  log `telnyx.event_failed` (no payload in logs), metric `error`. A failing mark (DB down) is logged and left for the
  reprocessor (worker never dies).
- `apps/telephony_edge/src/va_edge/reprocessor.py`: `class TelnyxEventReprocessor(database, processor, clock,
  interval_s=5.0, stale_after_s=30.0, max_attempts=5)`; `run_once() -> int` (enqueued count) and `run()` loop with
  `clock.sleep`; DB errors logged and retried next tick.
- `apps/telephony_edge/src/va_edge/routes.py`: `telnyx_routes() -> list[Route]` for the 4 POST paths (Starlette routes
  or FastAPI `APIRouter(include_in_schema=False)`; app has openapi off). Response body `{"status":"ok"}` (200),
  `{"status":"duplicate"}` 200, 401 `{"error":"invalid_signature"}`, 400 `{"error":"invalid_payload"}`, 503 draining.
- `apps/telephony_edge/src/va_edge/main.py`: `build_container` yields an `EdgeContainer` (deps + `Database.from_settings`
  disposed on exit, `configure_database`? follow api), lifespan (custom, like events_sink: `service_scope` + Supervisor
  running `processor.run` and `reprocessor.run`; stop → processor drains). `create_app(..., clock=None, handlers=None,
  stop=None)` seams; `app.state.processor` for tests.
- `apps/telephony_edge/tests/support/edge_helpers.py` (add dir to pytest pythonpath + mypy mypy_path in root
  pyproject): `keypair()` (nacl SigningKey), `signed(body: bytes, key, ts) -> headers`, `envelope(event_type, *,
  event_id=None, payload=..., client_state=None) -> bytes`, `edge_app(db_url, public_key, handlers=..., clock=)`,
  `events(url)` row reader via `migration_helpers.query`.

### Tests (apps/telephony_edge/tests)
- `test_intake.py` (Postgres via `migration_helpers.migrated_postgres()` module fixture, real `Database`):
  valid signature → 200, row stored with event_type/occurred_at/payload/call_control_id, client_state attribution
  (org_id/call_id; org row created with `OrgProvisioner` or plain insert); wrong key / tampered body / stale (>300 s) /
  missing headers → 401, nothing stored, counter `invalid_signature`; duplicate delivery → 200 `duplicate`, one row,
  handler ran once; unknown event type → stored, processed_at set, counter `unhandled`; malformed envelope with valid
  signature → 400; BYO path: account key accepted, platform key rejected on BYO path, BYO key rejected on platform path,
  unknown/deleted/non-uuid account → 401; client_state of another org on BYO path ignored; client_state with a
  non-existent org stored with org_id NULL; messaging route stores `message.received`.
- `test_processor.py` (fake DB seam? prefer real DB): per-call ordering across shards (handler records order for 2
  legs interleaved), handler failure → process_error + attempts 1 + counter `error`; queue full → `offer` False, row
  stays unprocessed; draining → 503.
- `test_reprocessor.py`: failed row retried until success; stops after 5 attempts; stale unprocessed row reclaimed,
  fresh one not; DB error in tick logged, loop continues (ManualClock / RecordingClock).
- `test_sim_contract.py`: FakeTelnyx (`telnyx_helpers.telnyx_state(transport)` but with an ASGI transport to the edge
  app — or deliver captured `RecordingTransport.received` requests to the edge) dials a call → every webhook it sends
  verifies with `state.signing_key.public_key_b64` and is stored once; forced duplicates (`force_duplicate_webhooks`)
  stored once.
- `test_fuzz_signatures.py`: hypothesis random signatures/bodies → always 401, nothing stored (spec §14.4).
- va_db: `test_telnyx_events_repo` additions (attempts increment, due_for_retry conditions, deleted org → NULL org);
  `TelnyxAccountLookupRepo` test; migration 0005 up/down; schema/autogenerate parity tests must stay green.

### Gates
`make fmt lint typecheck placeholders test` (+ `make check`), `make openapi` unaffected (edge has no schema).
`make e2e` not yet meaningful (no call flow) — run it if the target exists and report.

### Risks
- `test_schema_matches_spec`/autogenerate parity: column must be last in DDL and model.
- `test_metrics` parses the specs/27 catalog — editing only a label description should not change counts; verify.
- Edge tests needing Postgres must use the shared migrated-postgres helper (container boot time under xdist).
- Hypothesis + real DB is slow: fuzz test can call the route with a DB-less fake (signature fails before DB) — assert
  no repo call via a recording `record` seam.

## Build P05-T01 (Builder, 2026-11-01)
Implemented as planned, with these refinements:
- Reclaim lease uses `updated_at` (trigger = DB clock) instead of `created_at`, so two edge pods do not double-claim
  (`claim_retries(now, stale_after=30s, retry_after=5s, max_attempts=5)`, FOR UPDATE SKIP LOCKED). No extra index
  (kept schema change to the one column).
- BYO statuses accepted: pending/active/error (error accounts keep their key); unknown cached 30 s.
- In-process dedupe: processor ignores an offer for an event already queued (reclaim racing first try).
- Edge lifespan refuses to start without TELNYX_PUBLIC_KEY (local falls back to .sim file); serve passes stop.
- Spec decisions: specs/06 §4.1 Decision P05-T01, specs/03 §2 Decision (TelnyxAccountLookupRepo/claim_retries),
  specs/03 §4 DDL `process_attempts` last, specs/27 §3.2 result label adds `unhandled`.
- Gotcha hit: `ruff --fix --select ...,RUF100` stripped unrelated noqa's again; reverted untouched files.
Gates (Builder run): make check exit 0 (7496 passed, 7 skipped, coverage 98.56%, va_edge 99%), lint/typecheck/
placeholders/spec refs/openapi drift pass, jscpd 0 clones, radon avg A (2.46), pip-audit no known vulns (editable
lines filtered), pnpm audit 3 moderate (none high). `make e2e`: tests_e2e/ does not exist yet ("no tests ran",
exit 4) — the e2e suite arrives with the call-flow tasks; this task has no call flow.

## Review P05-T01 (Reviewer, 2026-11-01)
Gates re-run: make lint 0, make typecheck 0, make placeholders 0 (0 findings), make test 0 (7496 passed, 7 skipped, coverage 98.56%). Spec mapping,
migration 0005, repo changes, attribution, dedupe, 401/400/503 paths, reprocessor lease, metrics: all OK. Rejected for
two unauthenticated-input resource issues on a public endpoint (MASTER_PROMPT §4.6 security):

1. `apps/telephony_edge/src/va_edge/keys.py` `WebhookKeys._cache` is unbounded and never evicts. Any client can POST
   `/telnyx/voice/<random uuid>` with no valid signature: each new id costs one `va_system` query and leaves a
   permanent `_Cached(None)` entry (expired entries are only replaced on re-access of the SAME id). A flood of random
   UUIDs therefore grows memory without limit and every request still reaches Postgres — the module docstring's claim
   ("a flood of forged account ids does not reach Postgres") is false for random ids. Fix: bound the cache (e.g.
   `MAX_CACHED = 10_000`, `OrderedDict` LRU, drop expired entries on insert / evict oldest when full) and add a test
   that inserting > max distinct unknown ids keeps `len(cache) <= max` and still caches known accounts; correct the
   docstring and the specs/06 §4.1 Decision P05-T01 item 1 wording (mention the bound).
2. `apps/telephony_edge/src/va_edge/intake.py` `_endpoint` reads `await request.body()` with no size cap before
   verification, so an unauthenticated client can make the edge buffer arbitrarily large bodies in memory. Telnyx
   webhooks are a few KiB. Fix: cap the raw body (e.g. `MAX_BODY_BYTES = 1 MiB`; reject early on a larger
   `Content-Length` and stop reading the stream past the cap) with `413 {"error":"payload_too_large"}`, counted like
   an unverified request (or document the label choice), nothing stored; add the rule to the specs/06 §4.1 Decision
   P05-T01 and a test (oversized declared length and oversized chunked body).

## Fix P05-T01 review (Builder, 2026-11-01)
1. `va_edge.keys`: known and unknown account keys now live in two separate LRU caches (`_Lru`, OrderedDict,
   expired entries dropped on read, oldest evicted past the limit), bounds via `KeyCachePolicy(max_known=10_000,
   max_unknown=10_000, ttl_s, unknown_ttl_s)`. A flood of distinct unknown ids costs one lookup each but can neither
   grow memory past the bound nor evict real accounts. Docstring corrected; specs/06 §4.1 Decision P05-T01 item 1
   names the bound. Tests: flood stays bounded + known key still cached, LRU eviction order, account moving between
   caches, zero-size cache refused.
2. `va_edge.intake.read_capped`: 1 MiB cap (`MAX_BODY_BYTES`) before verification: larger declared
   `Content-Length` refused unread, chunked/understated bodies stop at the first chunk past the cap → 413
   `{"error":"payload_too_large"}`, nothing stored, counted `event_type="unverified", result="error"`, log
   `telnyx.webhook_too_large`. New decision item 1a in specs/06 §4.1. Tests `test_body_limit.py` (declared, chunked
   with read-count bound, understated length, body exactly at the cap reaches verification). Shared
   `edge_helpers.probe_edge(key)` (jscpd clone with the fuzz test removed).
Gates: make check exit 0 (7505 passed, 7 skipped, coverage 99% total; keys.py 100%, intake.py 99%), ruff/mypy clean,
placeholders 0, spec refs 0, jscpd 0 clones, radon avg A (2.46), pip-audit no known vulns, pnpm audit 3 moderate
(no high). make e2e: tests_e2e/ does not exist yet (no call flow in this task).

## Review P05-T01 round 2 (Reviewer, 2026-11-01)
Both findings fixed (keys.py: separate known/unknown LRUs with bounds + tests; intake.read_capped 1 MiB cap → 413
+ test_body_limit.py; spec Decision items 1/1a updated). Gates re-run: lint 0, typecheck 0, placeholders 0,
make test 0 (7505 passed, 7 skipped, 98.57%). APPROVED.

## Plan P05-T02 — Carrier event mapping and call/leg state machine (Planner, 2026-11-01)

Specs: specs/06 §3 (client_state fallback), §5 (mapping table), §5.1 (hangup causes); specs/05 §8 (status machine),
§9 (end_reason/ended_by); specs/19 §1–§2; specs/01 §2.1 step 4, §3; specs/07 §3 step 6, §17 (carrier channel);
specs/03 §11 (carrier_state 2 h, keys hash-tag form).

### Decisions (write as `> Decision (2026-11-01): carrier event mapping (P05-T02).` under specs/06 §5, after §5.1 table)
1. Shared contract `va_domain.carrier` (pure pydantic, no va_telnyx import): `CarrierEvent(type: str, leg: LegRole,
   call_id, org_id, telnyx_event_id, occurred_at, payload: dict[str, JsonValue])` (frozen). It is both the
   `call:{<id>}:carrier` pub/sub message (spec §17 `{"type","leg","payload"}` plus the ids/occurred_at; note it in
   a specs/07 §17 Decision line) and the argument of Temporal signal `CallWorkflow.signal_carrier_event`
   (`CARRIER_SIGNAL = "signal_carrier_event"`, workflow id `call_workflow_id(call_id) = f"call-{call_id}"`).
   Key builders live here too (hash-tag form): `carrier_state_key(id)` = `call:{<id>}:carrier_state`,
   `carrier_channel(id)`, `owner_key(id)` (media worker reuses them in P05-T07). `CARRIER_STATE_TTL_S = 7200`.
2. Call resolution: `StoredEvent.call_id`/`org_id` (from client_state at intake, P05-T01) else system
   `CallLegLookupRepo.by_call_control_id(ccid) -> LegRef(org_id, call_id, role)` (new va_system repo; add to specs/03 §2
   list + Decision; `packages/va_db/tests/test_repositories.py` count 11 → 12). Leg role: client_state `leg` else
   LegRef.role else `primary`. Unresolvable non-initiated event → processed, log `telnyx.event_unattributed`
   (event_type, telnyx_event_id only). `call.initiated` with direction `incoming` and no call → optional
   `InboundRouter` protocol hook (`route(event, payload)`, `None` in T02 → log `telnyx.inbound_unrouted`, processed;
   P05-T12 plugs the router in).
3. Effects per event (pure `va_edge.mapping.plan_effects(event_type, payload, snapshot) -> CarrierEffects`); the
   handler applies them. Signals go to the workflow for: `call.answered`, `call.bridged`, `call.hangup`, every AMD
   type, `streaming.failed`, `call.recording.saved`, `call.recording.error`. The carrier channel gets every event of a
   resolved call except the "stored only" types (hold/unhold, deepfake, conversation.*). Playback events: debug log +
   carrier channel only.
4. Telnyx commands are NOT issued by the edge (it has no TelnyxClient): `fax_detected` hangup and the `streaming.failed`
   5 s no-worker hangup are performed by `CallWorkflow` from the signal (append this to P05-T06 deliverables in
   plan/phase-05.md). The edge records `answered_by='fax'` and emits `call.amd`/`call.error stream_failed`.
5. DTMF: the edge publishes `call.dtmf.received` on the carrier channel only; the worker (which also gets WS `dtmf`
   and dedupes) emits the `call.turn` `"[DTMF 5]"` with `ignore_button_press` applied — no edge `call.turn`, so turns
   are never doubled and the edge needs no config.
6. Status machine (`va_db.call_states.CALL_TRANSITIONS`: target → allowed sources), spec §8 plus carrier-reorder
   tolerance (record as a specs/05 §8 Decision): queued←{scheduled}; dialing←{queued}; ringing←{dialing};
   in_progress←{dialing, ringing}; transferring←{in_progress}; transferred←{transferring};
   completed←{in_progress, transferring, transferred}; failed←{queued, dialing, ringing}; no_answer←{dialing, ringing};
   busy←{dialing, ringing}; canceled←{scheduled, queued, dialing, ringing} (§5.1 `originator_cancel`). Terminal
   (completed, failed, canceled, no_answer, busy) have no outgoing transitions. Leg machine
   `LEG_TRANSITIONS`: ringing←{initiated}; answered←{initiated, ringing}; bridged←{answered}; hangup←{initiated,
   ringing, answered, bridged}.
7. Hangup (primary leg only drives the call; transfer_target hangup updates its leg only):
   - never answered (call in dialing/ringing): §5.1 table → status/end_reason; `ended_by` from `END_REASONS`;
     `answered_by='no_answer'` for no_answer; `error_message="Carrier hangup: <cause> (<sip>)"` for carrier_error
     (`(<sip>)` omitted when null? NO: write `(None)`? — use `"(unknown)"` when sip cause is null); `ended_at` =
     payload `end_time` or `occurred_at`; emit `call.ended {end_reason, ended_by}`.
   - answered and `calls.end_reason` already set (we initiated: worker/workflow wrote it before hanging up) → no call
     change (replaces the `command_id` prefix test: Telnyx hangup webhooks carry no command_id).
   - answered and `call:{<id>}:owner` exists → pipeline running: the worker ends the call; edge only updates the leg,
     carrier_state, channel, signal.
   - answered, no owner → completed; end_reason `max_duration` if cause `time_limit`, `transferred` if status was
     `transferred`, else `user_hangup`; `connected_seconds` = (transfer_started_at or ended) − answered_at,
     `transfer_seconds` = ended − transfer_started_at when carrier_state has it; emit `call.ended`.
   - unknown causes stored verbatim on the leg.
8. At-least-once with retries: DB effects run in one `tenant_session(org_id)` (leg + call); domain events are
   published only when this processing applied the call transition, or when `event.attempts > 0` and the call is
   already in the target status (reprocessing after a publish failure). Carrier state HSET + EXPIRE and the channel
   PUBLISH are idempotent and always run; the signal always runs (workflow dedupes by telnyx_event_id).
   Order: DB commit → carrier_state → domain events → channel publish → signal. Any Redis/Temporal failure raises →
   process_error → reprocessor retry.
9. AMD: `call.machine.detection.ended` data.result normalized (`human`→`human_residence`, `machine`→`machine`,
   `not_sure`→`unknown`, others verbatim), leg `amd_result` = raw result, no answered_by. Premium detection: leg
   `amd_result`, call `answered_by` (`human_residence`/`human_business`→`human`, `machine`→`voicemail`,
   `silence`→`unknown`, `fax_detected`→`fax`, other→`unknown`), data.result raw. Greeting ended →
   `data.greeting_ended=true`; premium greeting ended → `data.beep_detected=true`; screening → `data.screening=true`.
   carrier_state fields `amd_result`, `answered_by`, `beep_detected`, `greeting_ended`, `screening`. Metric
   `va_amd_results_total{result=<raw>}` for detection types.
10. Signal sender `TemporalCarrierSignaller(connector: TemporalConnector)`: `client.get_workflow_handle(id).signal(
   CARRIER_SIGNAL, event)`; `RPCError` with status `NOT_FOUND` → log `telnyx.workflow_missing` (debug) and succeed
   (no workflow yet before P05-T05/T06, or already finished); other errors propagate.

### Files
- `packages/va_domain/src/va_domain/carrier.py` (+ tests `packages/va_domain/tests/test_carrier.py`: round-trip JSON,
  key formats with braces, workflow id).
- `packages/va_db/src/va_db/call_states.py`: `EndReason` Literal (26 values, §9 order), `END_REASONS:
  Mapping[EndReason, EndedBy]`, `TERMINAL_STATUSES`, `CALL_TRANSITIONS`, `LEG_TRANSITIONS`, `allowed_from(to)`,
  `leg_allowed_from(to)`, `is_terminal(status)`. Pure.
- `packages/va_db/src/va_db/repositories/calls.py`: `CallRepo(session, org_id)` tenant (pattern of other tenant repos):
  `get(call_id) -> Call` (NotFoundError), `transition(call_id, to, *, from_: Collection[CallStatus] | None = None,
  **values) -> Call` — `UPDATE calls SET status=:to, <values> WHERE id AND org_id AND status = ANY(from_ or
  allowed_from(to)) RETURNING *`; `from_` must be ⊆ allowed_from(to) (ValueError); no row → `get` → NotFoundError if
  absent else `CallTransitionConflict(AppError CONFLICT)` with `.current` status; `update_fields(call_id, **values)`
  (used for answered_by without a status change). `CallLegRepo(session, org_id)`: `by_control_id(ccid) -> CallLeg |
  None`, `record_initiated(call_id, role, ccid, *, call_leg_id, call_session_id, telnyx_account_id, started_at) ->
  CallLeg` (INSERT … ON CONFLICT (call_control_id) DO UPDATE SET call_leg_id = COALESCE(call_legs.call_leg_id,
  EXCLUDED.call_leg_id), call_session_id likewise; RETURNING), `advance(ccid, to, **values) -> CallLeg | None` (guarded
  UPDATE; None = illegal/duplicate), `set_amd(ccid, result)`.
  `packages/va_db/src/va_db/repositories/call_legs_lookup.py`: `CallLegLookupRepo(SystemRepository).by_call_control_id
  (ccid) -> LegRef | None`. Export all from `va_db.repositories`.
- `apps/telephony_edge/src/va_edge/mapping.py` (pure): `CallSnapshot(call_id, org_id, status, answered_at, end_reason,
  leg_role, leg_state, owner_present, transfer_started_at)`, `CarrierEffects(leg: LegChange | None, call: CallChange
  | None, call_fields: dict, events: tuple[PlannedEvent, ...], carrier_state: dict[str, str], publish: bool, signal:
  bool, amd_result: str | None)`, `plan_effects(event_type, payload: EventPayload, snapshot, *, occurred_at)`,
  `hangup_outcome(payload, snapshot, occurred_at) -> HangupOutcome` (§5.1), `VOICE_EVENT_TYPES` (frozenset of every
  §5 type). One small function per event family (dispatch dict) to keep radon complexity low.
- `apps/telephony_edge/src/va_edge/carrier.py`: `CarrierEventHandler(database, redis, publisher, signaller, *, clock,
  metrics, inbound: InboundRouter | None = None)`; `handlers() -> dict[str, EventHandler]` (every
  `VOICE_EVENT_TYPES` entry → `self.handle`); `handle(event, payload)` = resolve → snapshot (tenant_session: call,
  leg; Redis `EXISTS owner`, `HGET transfer_started_at`) → `plan_effects` → apply (§8 order).
  `CarrierSignaller` protocol + `TemporalCarrierSignaller` in `apps/telephony_edge/src/va_edge/signals.py`.
- `apps/telephony_edge/src/va_edge/container.py`: `EdgeDependencies` gains `redis: Redis` and `temporal:
  TemporalConnector` (EdgeContainer stores the values `deps.redis()`/`deps.temporal()` return); update
  `edge_helpers.ProbeContainer` and other test containers.
- `apps/telephony_edge/src/va_edge/main.py`: when `handlers is None`, build the registry inside `_serve_webhooks` from
  the container: `EventPublisher(container.redis, call_events=DirectCallEventWriter(DbCallEventWriter(database)))`
  (check the actual writer constructor), `TemporalCarrierSignaller(container.temporal)`.
- Dependencies: va-telephony-edge gains `va-events`, `va-domain` (workspace sources) if missing; `uv lock`.
- Specs: specs/06 §5 Decision P05-T02 (items 1–10); specs/05 §8 Decision (transition table item 6, `transition`
  lives at `va_db.repositories.CallRepo.transition`); specs/03 §2 list + Decision for `CallLegLookupRepo`; specs/07
  §17 one-line Decision about the CarrierEvent fields; plan/phase-05.md P05-T06 deliverables gets "fax_detected and
  streaming.failed (5 s, no owner) hangups from carrier signals".
- AGENTS.md: P05-T02 learning line.

### Tests
- `packages/va_db/tests/test_call_states.py` (pure): `END_REASONS` equals the specs/05 §9 table parsed from the spec
  (end_reason → ended_by, order); full status×status matrix: allowed iff in table; terminals have no outgoing; every
  §8 diagram edge present (hand-listed); leg matrix likewise.
- `packages/va_db/tests/test_calls_repo_db.py` (scratch/migrated DB like other repo DB tests): transition applies and
  returns row with values; conflict raises `CallTransitionConflict` (code CONFLICT, `.current`) and changes nothing;
  duplicate transition = conflict; other org → NotFoundError (RLS); `from_` not subset → ValueError; leg
  `record_initiated` idempotent (second call keeps first ids, fills nulls); `advance` guard (answered→initiated None,
  hangup twice None); `CallLegLookupRepo` hit/miss.
- `apps/telephony_edge/tests/test_mapping.py` (pure, table-driven): parse the specs/06 §5 table event types (reuse
  the parser approach of `packages/va_telnyx/tests/test_webhook_models.py`) and assert `VOICE_EVENT_TYPES` covers each
  row (expanding `a` / `b` and `.result` / `.error` shorthand); one parametrized case per event type with expected
  leg change / call change / event types+data / carrier_state / publish / signal; §5.1: one case per table row
  (incl. sip 404/484/604, unknown cause verbatim → carrier_error message, answered+time_limit, answered+owner,
  answered+end_reason set, transferred status → `transferred` with transfer_seconds); illegal: answered when call
  completed → no call change, no events; AMD normalization tables.
- `apps/telephony_edge/tests/test_carrier_db.py` (edge_db_url + fakeredis + `RecordingSignaller` + real publisher on
  fakeredis + DB call_events writer): seed org + call(status dialing) with own ids; drive stored events through
  `EventProcessor` with the carrier handlers: initiated → ringing + leg row + `call.ringing` in `events:calls`;
  answered → in_progress/answered_at/started_at, carrier_state answered=1 (TTL ≤ 7200), channel message is a valid
  CarrierEvent, signal recorded; hangup no owner → completed/user_hangup/connected_seconds, `call.ended`; never
  answered busy → busy/BUSY end_reason; owner key present → status stays in_progress; client_state missing →
  resolved via call_legs; unattributed event → processed no error; inbound initiated with no router → processed;
  duplicate handling of the same event (re-run handler) → no second domain event; publish failure (Redis
  `connected=False` after commit) → process_error, retry with attempts>0 re-emits once; signal NOT_FOUND tolerated;
  signal other error → process_error.
- `apps/telephony_edge/tests/test_signals.py`: fake client/handle; NOT_FOUND RPCError swallowed + logged, other
  RPCError raised, handle id `call-<uuid>`, signal name + arg.
- `apps/telephony_edge/tests/test_sim_contract.py` addition: FakeTelnyx dial with client_state of a seeded call →
  webhooks delivered to the edge app with carrier handlers → call ends `completed` / leg `hangup` (or no_answer
  scenario).
- Update `test_main.py`/`test_startup.py` for the new container fields and default registry.

### Gates
`make fmt lint typecheck placeholders test` + `make check`; jscpd 0 clones; radon; `make e2e` (tests_e2e not present
yet → report).

### Risks
- Temporal client in tests: never connect; the default registry needs `container.temporal` — test containers pass a
  connector with a fake `connect`.
- Spec-table parsers: `call.deepfake_detection.result` / `.error` and `a` / `b` cells need expansion.
- RLS: handler uses tenant_session(org_id) only after resolution; lookup uses system scope (comment citing specs/03 §2).
- jscpd: reuse edge_helpers for seeding orgs/calls; add `seed_call(db_url, status=...)` helper there.
- Numeric `connected_seconds` is numeric(10,3): pass `Decimal` rounded to ms.

## Build P05-T02 (Builder, 2026-11-01)
Implemented per plan, with these deviations:
- `CallTransitionConflict` renamed to `CallTransitionError` (ruff N818); `.current` kept.
- Leg `bridged` is also allowed from `initiated`/`ringing` (call.bridged can overtake call.answered); recorded in the specs/05 §8 Decision.
- Carrier-state fields for non-primary legs are prefixed with the role (`transfer_target_answered`, `transfer_target_hangup`, `primary_bridged`); `conference.participant.joined` on the transfer_target leg behaves like `call.bridged` (call moves to transferred).
- The signal payload is `CarrierEvent.model_dump(mode="json")`; the default Temporal converter carries no pydantic models.
- §5.1 row "command_id prefix hangup-" now refers to Decision item 7 (end_reason already set).
- The `spec_voice_event_types` parser moved to `packages/va_telnyx/tests/support/telnyx_spec.py`, shared with the edge tests.
- New test support `packages/va_db/tests/support/call_rows.py`; `SimTimeClock` in test_sim_contract (a FrozenClock app clock makes the handler budget fire at once).
Gates: fmt ok; lint exit 0; typecheck exit 0 (923 files); placeholders 0; spec refs 0; make test exit 0 (7784 passed, 7 skipped, 98.57%; carrier.py 96%, mapping.py 99%, calls.py/call_states.py/signals.py/va_domain carrier 100%); jscpd 0 clones; radon avg A (2.30, max B 9); pip-audit: no known vulns; pnpm audit: 3 moderate, none high. make e2e: exit 4 "no tests ran", because tests_e2e/ does not exist yet (P05-T17).

## Review P05-T02 (Reviewer, 2026-11-01)
Gates re-run: lint 0, typecheck 0, placeholders 0, `make test` **exit 2**: 1 failed, 7783 passed, 7 skipped (98.57 %).
The diff matches specs/06 §5/§5.1, specs/05 §8/§9, and specs/03 §2. Its tests are thorough. There is one
blocking finding:

1. `apps/telephony_edge/tests/test_intake.py:131-158` `test_unverified_webhooks_are_rejected_and_not_stored[future]`
   fails under full-suite load. It got 200 `{"status":"ok"}` instead of 401. Cause: the test computes
   `now = int(time.time())` and signs with `now ± 301` *before* `edge_client(...)` starts the app (DB pool + lifespan,
   which now also builds the carrier registry). `int()` truncates up to 1 s, so the margin over `TOLERANCE_S = 300` is
   under 1 s. When startup plus the request take longer than that, the "future" timestamp falls inside the window
   and is accepted. The edge suite alone passes 3/3, so the test is load-dependent (flaky, and the master prompt
   §8 says to fix the cause). Fix: build the headers/timestamps inside the `async with edge_client(...)` block,
   right before `client.post`, and use a skew that cannot become valid through elapsed time. For example, give the
   edge app an injected clock that both the test and the verifier read, or use `now ± 360` for stale/future (still
   outside 300 s with a minute of slack). Check the other timestamp-sensitive intake tests (`stale`, and any
   `timestamp=` use in test_intake.py / test_sim_contract.py) for the same pattern.

## Fix P05-T02 review (Builder, 2026-11-01)
Finding 1 fixed: `test_unverified_webhooks_are_rejected_and_not_stored` now builds the (tampered) headers inside
`async with edge_client(...)`, right before `client.post` (helper `_tampered(body, tamper, now)`), and stale/future use
`_SKEW_S = TOLERANCE_S + 60` (imported from `va_telnyx.signatures`), so startup time or load can never move a skewed
timestamp into the 300 s window. Checked the other timestamp users: `test_fuzz_signatures` skews within ±299 with
random signatures (rejected regardless of time); `test_sim_contract` uses the sim clock. No other skew tests.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test exit 0 (7784 passed, 7 skipped).

## Review P05-T02 round 2 (Reviewer, 2026-11-01)
Finding 1 fixed (headers signed inside the client block, skew TOLERANCE_S+60). Gates re-run: lint 0, typecheck 0,
placeholders 0, make test exit 0 (7784 passed, 7 skipped, 98.57 %). Approved and committed.

## Plan P05-T03a — Send Call request/config models, E.164 normalization, shared language table (Planner, 2026-11-01)
P05-T03 was too large for one Builder activation, so plan/phase-05.md now splits it into P05-T03a (models, pure, no
I/O), P05-T03b (pure `resolve_call_config`), and P05-T03c (I/O loader in `va_api.services.call_inputs`). P05-T04
depends on P05-T03c. Tick P05-T03 itself when a–c are ticked. Persona/agent loading and experiments stay in P13-T07
(the plan says so), but the request model validates persona/agent fields and runner rules now.

### Decisions (write as `> Decision (2026-11-01): Send Call request model (P05-T03a).` under specs/05 §3, after the guard_rails paragraph)
1. Schemas live in the package `va_domain.calls` (`__init__` re-exports; modules `request.py`, `request_parts.py`,
   `config.py`, `tools.py`, `errors.py`). The import path `va_domain.calls` matches the spec header. va_domain stays
   I/O-free and does not depend on va_common, so rule failures are `CallRequestError(code, message, field)` (a
   plain `Exception` with attributes) that the API maps to `AppError(code, message, field=field)` in P05-T03c/T05.
2. `CallParameters` has every §3 parameter **except** the runner fields and `phone_number`, all optional with the §3
   defaults. Lower layers (`orgs.settings.call_defaults`, pathway `global_config.call_defaults`, agent
   `call_settings`, persona config) are parsed with `CallParameters.model_validate(...)` in P05-T03b.
   `SendCallRequest(CallParameters)` adds the §3.1 runner fields and `phone_number` (required).
   `model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, populate_by_name=True)`. Nested objects use
   `extra="forbid"` (only top-level unknown fields are ignored, per §3). Helper `unknown_fields(raw: Mapping[str,
   object]) -> list[str]` (top-level keys that are not field names/aliases) for the endpoint's debug log.
3. `explicit_fields() -> dict[str, Any]`: canonical parameter name → value for parameters present in the body
   (`model_fields_set`), with legacy aliases folded: `amd` → `ivr_mode` (`ivr_mode` wins when both are sent);
   `available_tags` → `dispositions` (`dispositions` wins); `local_dialing`/`geospatial_dialing` true →
   `dialing_strategy={"type":"local"}` unless `dialing_strategy` was sent; `knowledge_base_ids` folded into the KB
   entries of `tools` (deduplicated, order kept); the `voicemail` object reports only its sent keys plus the implied
   `action="leave_message"` when `message` is set and `action` is not; `voicemail_detect_timeout_ms` stays a
   top-level key (P05-T03b maps it into `voicemail.detect_timeout_ms`). Values are model instances/primitives, not
   JSON (P05-T03b decides the merge).
4. `check_rules() -> None` raises the first `CallRequestError` in this order (all are schema-adjacent; each maps
   to its catalogued code): `retry` present without `wait` → `RETRY_NOT_PROVIDED` (field `retry.wait`, message
   "Retry attempt not provided for retry call."); runner rules (§3.1, step 4): none of
   task/pathway_id/persona_id/agent_id → `RUNNER_CONFLICT` (field `task`); `agent_id` with `persona_id` or
   `pathway_id` → `RUNNER_CONFLICT` (field `agent_id`); `agent_id` with `task` → `RUNNER_CONFLICT` (field `task`).
   `runner_type` property → `"agent" | "persona" | "pathway" | "task"` (that precedence: agent, persona, pathway,
   task) — `task` alongside another runner is ignored (warning emitted in T05), and persona+pathway is a persona
   runner with a pathway override. `AGENT_REQUIRES_ORG_KEY`, `GUARD_RAIL_LIMIT`, `FEATURE_NOT_IN_PLAN`,
   `test_call`/`test_mode` permission and webhook scheme/SSRF policy need the principal/org/plan: P05-T03c.
5. Destination: the model keeps `phone_number` raw (trimmed, 1–64 chars). Parsing is validation step 8
   (`INVALID_PHONE_NUMBER`) with region = body `country`, else `orgs.settings.default_country`, else `US`, so it
   happens in P05-T03c via `va_domain.phone.normalize_e164(value, region) -> str` (raises `InvalidPhoneNumber(
   ValueError)`): `phonenumbers.parse`, must be `is_possible_number` and `is_valid_number`, format E164; values
   starting with `+` ignore the region; strips spaces/dashes/parentheses/dots (phonenumbers does). `from`,
   `transfer_list` values, `voicemail.sms.from` are strict E.164 in the schema (reuse `va_domain.org.E164`);
   `transfer_phone_number` is E.164 or `sip:` URI (`^sip:[^@\s]+@[^\s]+$`, ≤ 256).
6. `start_time` parses `YYYY-MM-DD HH:MM:SS ±HH:MM`, `YYYY-MM-DD HH:MM:SS` (naive), and ISO 8601 (`datetime.
   fromisoformat`, `Z` accepted) into a `datetime` that may be naive. Future/≤ 90 days and localizing a naive value
   with the resolved timezone need the clock and resolved timezone: P05-T03b.
7. `language`: validated in the schema against the specs/08 §5 table (case-insensitive; stored as sent, trimmed).
   The pure table (`DEFAULT_TTS_LANGUAGE`, `TTS_SUPPORTED`, `LanguageMapping`, `_ROWS`, `LANGUAGE_MAP`, plus a new
   `lookup_language(value) -> LanguageMapping | None`) moves from `va_elevenlabs.languages` to
   `va_domain.languages`; `va_elevenlabs.languages` imports and re-exports them and keeps `resolve_language`
   (AppError), `tts_language`, `LanguageLock` (va_elevenlabs already depends on va-domain). Existing va_elevenlabs
   tests must pass unchanged.
8. `tools` items: `"TL-<uuid>"`, `"KB-<uuid>"` (regex `^(TL|KB)-[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$`),
   or an `InlineTool` object = specs/13 §2 ToolCreate without id: `name` `^[A-Za-z][A-Za-z0-9_]{0,63}$`,
   `description` 1–1 000, `speech` ≤ 500 | None, `url` 1–2 048 (may contain `{{vars}}`; scheme check `http(s)://`
   prefix only), `method` GET/POST/PUT/PATCH/DELETE default POST, `headers`/`query` dict[str, str] (header names
   RFC 7230 tokens), `body` JsonValue | None (≤ 64 KB serialized), `input_schema` object with `type: "object"`
   default `{"type":"object","properties":{}}`, `response` dict[str, str] (JSONPath parse is P08), `timeout_ms`
   100–60 000 default 10 000, `retries` 0–3 default 0. Inline tools do **not** accept `secret_headers` (they would be
   stored in plaintext in `calls.config`; extra="forbid" rejects them → `INVALID_INPUT`). `DynamicDataSpec` per §7:
   `url`, `method` (default GET), `headers`, `query`, `body`, `cache` (default false), `cache_ttl_seconds` 0–86 400
   default 300, `timing` `pre_call|every_turn` default `pre_call`, `response_data: list[ResponseDataItem(name
   ^[A-Za-z_][A-Za-z0-9_]{0,63}$, data 1–500, context ≤ 1 000 | None)]` ≤ 20. These live in `va_domain.calls.tools`;
   P08's `va_tools.models` re-exports them instead of redefining.
9. `campaign_id` (named in §10's alias list) is the `GET /v1/calls` alias of `batch_id` (§6), so it belongs to the
   list-filter model (P05-T16), not to Send Call.

### Field list (SendCallRequest/CallParameters) — implement every §3 row; constraints per the tables
- Runner: `task` 1–16 000; `pathway_id` UUID; `pathway_version` `int ≥ 0 | "production" | "staging" | "draft"`
  (draft only usable with `test_call`; enforced in T03c) default None (= production); `start_node_id` 1–128;
  `persona_id` UUID; `persona_version` `"production" | "draft" | int ≥ 0`; `persona_settings` dict[str, JsonValue]
  whose keys must be `CallParameters` field names/aliases (else INVALID_INPUT, field `persona_settings.<key>`);
  `agent_id` UUID; `agent_version` pattern `^(production|staging|dev|latest|\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?|branch:[A-Za-z0-9._/-]{1,64})$`.
- Basic: `phone_number`; `voice` 1–100 (name or UUID string); `voice_settings: VoiceSettingsPatch` (all fields optional,
  same bounds as `va_domain.voices.VoiceSettings`, extra forbid); `first_sentence` ≤ 1 000; `wait_for_greeting` bool;
  `language` (Decision 7); `model` 1–200; `temperature` 0–1; `max_tokens` 32–1 024; `pronunciation_guide` ≤ 200
  `PronunciationRule`; `interruption_threshold` 50–5 000 | None; `interruptibility` 0–4; `block_interruptions`;
  `noise_cancellation`; `background_track` `office|cafe|restaurant|none|None`; `hd_audio`; `record`; `max_duration`
  float 0.5–240.
- Dispatch: `from_` alias `from` (E.164); `dialing_strategy` `DialingStrategy` (custom_pooling requires `pool_id`,
  others forbid it); `timezone` (`va_domain.org.Timezone`); `start_time` (Decision 6); `transfer_phone_number`;
  `transfer_list` ≤ 20, labels 1–64, values E.164, non-empty must contain `default`; `warm_transfer`
  `WarmTransferRequest` (briefing ≤ 2 000, hold_audio, timeout_s 10–120 default 45); `country` `CountryCode`.
- Knowledge: `tools` ≤ 30; `knowledge_base_ids` ≤ 10 `KB-<uuid>`; `memory_id` UUID; `memory_enabled` bool.
- Voicemail: `ivr_mode` (+ `amd`); `voicemail` `VoicemailRequest` (message ≤ 1 000, action optional, sms
  `{message ≤ 1 600, from?}` required when action `leave_message_and_sms`, sensitive); `voicemail_detect_timeout_ms`
  3 000–30 000; `answered_by_enabled`.
- Analysis: `summary_prompt` ≤ 2 000; `dispositions` ≤ 50 `^[A-Z][A-Z0-9_]{0,63}$`; `available_tags` (same items);
  `analysis_schema` ≤ 50 keys, key 1–64, value 1–500 string; `analysis_prompt` ≤ 2 000; `citation_schema_ids` ≤ 10
  UUIDs; `post_call_evals` exactly one of the two ids; `stream_post_transfer_transcript`; `retry` `RetryRequest`
  (`wait` 60–86 400 optional at schema level, voicemail_action, voicemail_message ≤ 1 000, max_attempts 1–5 default
  1, on_no_answer).
- Advanced: `request_data` (keys `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, ≤ 65 536 bytes `json.dumps(separators=(",",":"))`
  UTF-8); `metadata` ≤ 65 536 bytes; `external_id` `^[\x20-\x7E]{1,256}$` (validate before strip? strip applies; then
  1–256); `webhook` absolute http(s) URL ≤ 2 048 (scheme policy + SSRF in T03c); `webhook_events` unique subset of
  the 9 names; `dynamic_data` ≤ 10; `keywords` ≤ 100 `^[^:\s][^:]{0,99}(:(10|[1-9]))?$`; `ignore_button_press`;
  `precall_dtmf_sequence` `^[0-9*#w]{1,64}$`; `test_call`; `guard_rails` list[`GuardRail`] ≤ 20 (custom requires
  name 1–64 + description 1–1 000; action `transfer` requires E.164 phone_number; `move_to_node` requires node_id);
  `local_dialing`, `geospatial_dialing`, `test_mode` bools.
- Every field gets `Field(description=..., examples=...)` where useful (OpenAPI in T05 relies on it), and the
  request model one `json_schema_extra` example (§5 style request).

### SendCallConfig (va_domain/calls/config.py)
Exactly the §4.4 block (field names, types, defaults), `extra="forbid"`, `VoicemailSms.from_` with
`serialization_alias`/`validation_alias` `"from"` + populate_by_name (AGENTS.md P03-T08a note: use
`Field(serialization_alias="from", validation_alias="from")`), `InlineTool`/`DynamicDataSpec` from `tools.py`,
`VoiceSettings` from `va_domain.voices`, JSON round-trip `SendCallConfig.model_validate(cfg.model_dump(mode="json",
by_alias=True))` == cfg.

### Files
- create `packages/va_domain/src/va_domain/calls/{__init__,errors,tools,request_parts,request,config}.py`
  (`request_parts` = nested request models + Annotated field types; keep each module < ~400 lines, radon ≤ B).
- create `packages/va_domain/src/va_domain/phone.py`, `packages/va_domain/src/va_domain/languages.py`.
- modify `packages/va_elevenlabs/src/va_elevenlabs/languages.py` (import table from va_domain, same `__all__`).
- modify `packages/va_domain/pyproject.toml` (+ `phonenumbers>=9`; check the latest with `uv add`), `uv lock`.
  phonenumbers ships type stubs? If not, add a commented mypy override like soxr.
- create `apps/api/tests/fixtures/bland_send_call/01_task_basic.json … 12_*.json` (groups: task basic, pathway
  runner + version, persona + persona_settings, agent runner, voice/audio, dispatch/schedule, transfer, tools/KB/
  dynamic_data, voicemail/AMD, analysis/retry, advanced/request_data/metadata/guard_rails, legacy aliases) and
  `apps/api/tests/test_bland_send_call_fixtures.py` (each parses, `check_rules()` passes, `unknown_fields` empty
  except where a fixture deliberately carries one Bland-only field).
- specs/05 §3 Decision (items 1–9); AGENTS.md learning line.

### Tests (packages/va_domain/tests)
- `test_call_request_fields.py`: one parametrized table `(field, valid values, invalid values)` covering every §3
  parameter boundary (e.g. `max_tokens` 32/1024 ok, 31/1025 bad; `max_duration` 0.5/240 ok, 0.49/240.1 bad; task
  16 000 ok/16 001 bad; request_data exactly 65 536 bytes ok/65 537 bad; bad request_data key; transfer_list without
  default; 21 entries; keywords `word:11`; dtmf `12a`; webhook_events unknown; dispositions lower-case; analysis_schema
  51 keys; post_call_evals both/neither; dialing_strategy custom_pooling without pool_id; guard rail transfer
  without phone; inline tool with secret_headers rejected; tools 31 items; bad `TL-` id; voice_settings speed 1.3;
  language `xx` rejected, `EN-us` accepted…). Builds the minimal valid body `{"phone_number": "+14155550100",
  "task": "Hi"}` and patches one field per case; asserts error `loc`.
- `test_call_request_rules.py`: aliases (amd, local_dialing, geospatial_dialing, available_tags, knowledge_base_ids,
  voicemail message → leave_message, both alias+canonical → canonical wins), `explicit_fields` only sent keys,
  trimming, unknown fields ignored + `unknown_fields`, `check_rules` table (every RUNNER_CONFLICT case, retry
  missing wait precedes runner conflict, valid combos incl. persona+pathway and task+pathway), `runner_type`,
  start_time formats (offset, naive, ISO Z, bad format), persona_settings unknown key rejected.
- `test_call_config.py`: `SendCallConfig` field names and defaults equal the specs/05 §4.4 code block (parse the
  ```python block: `name: type = default` lines, `;`-separated too; compare names per class and the literal defaults
  that are plain literals), JSON round trip, `from` alias.
- `test_phone.py`: national US with/without country, `+44 20 7946 0958`, letters rejected, too short, `+1 555 0100`
  invalid, region `GB`, whitespace/dashes, `InvalidPhoneNumber` is a ValueError.
- `test_languages.py` (va_domain): every alias resolvable, case-insensitive lookup, unknown → None; va_elevenlabs
  `tests/test_languages.py` still green.

### Gates
`make fmt lint typecheck placeholders test`, spec refs, jscpd 0 clones, radon. No call flow touched → `make e2e` not
required (tests_e2e absent anyway).

### Risks
- jscpd: the field table tests and fixture JSONs can look repetitive; keep fixtures distinct, build tests from tables.
- pydantic `str_strip_whitespace` applies to all str fields incl. JSON values? It does not touch JsonValue dict
  contents; fine. `extra="ignore"` + `populate_by_name` with alias `from` — mypy without plugin: construct in tests
  via `model_validate(dict)` only.
- `VoiceSettingsPatch` must not duplicate `VoiceSettings` field blocks verbatim (jscpd): define with Annotated types
  shared from `va_domain.voices` (add `Stability`, etc. aliases there) or generate with `create_model`; prefer the
  shared Annotated types.
- Moving the language table must keep `va_elevenlabs.languages.__all__` and behaviour identical.

## Build P05-T03a (Builder, 2026-11-01)
Implemented per plan. Deviations: the nested models shared by request and config live in `va_domain/calls/parts.py`
(not `request_parts.py`); `WarmTransferConfig`/`DialingStrategy`/`GuardRail(Action)`/`PostCallEvals`/
`PronunciationRule`/`VoicemailSms` are one model each (constrained), used by both request and config; request-only
`VoicemailRequest` (`sent()`) and `RetryRequest`; `RetryConfig`/`VoicemailConfig` are plain spec copies in config.py.
`InvalidPhoneNumber` is named `InvalidPhoneNumberError` (ruff N818). `voices.py` gained `UnitInterval`/`Speed`
Annotated types plus `VoiceSettingsPatch`. Letters are rejected by `normalize_e164`, not converted as vanity numbers.
persona_settings key errors land at `persona_settings.<key>` via a dict-key AfterValidator.
config.py field blocks sit between jscpd ignore markers (they clone the spec block on purpose).
Gates: fmt ok; lint 0; mypy 937 files clean; placeholders 0; spec refs 0; jscpd 0 clones; radon avg A (2.53, worst
class C 11); make test exit 0 (8175 passed, 7 skipped, 98.46 %); va_domain coverage 99 %; pip-audit clean; pnpm audit
3 moderate (no high, unchanged). No call flow touched → no e2e.

## Review P05-T03a (Reviewer, 2026-11-01)
Approved. Re-ran make lint typecheck placeholders (0 findings) and make test (8175 passed, 7 skipped, 98.58 %), spec refs.
Checked §3.1–§3.7 rows vs CallParameters/SendCallRequest, §4.4 drift test, specs/13 §2/§7 shapes; probed aliases,
voicemail implied action, KB dedupe, start_time forms, country upper-casing, persona_settings key errors.

## Plan P05-T03b — Pure `resolve_call_config` precedence (Planner, 2026-11-01)

Spec: specs/05 §4.3 (layers + special rules), §4.4 (target model), §3 (defaults/semantics); specs/04 §7 org settings;
specs/20 §3.1/§3.3/§4.3 (persona/agent layer shapes); specs/23 §1.1/§4/§5 (forced rails); specs/11 §3.4.
Everything is pure (no I/O, no clock reads): DB loading is P05-T03c, which calls `merge_layers` first to learn which
voice/tools/from to load, then `resolve_call_config`.

### Decisions (write as `> Decision (2026-11-01): call config resolution (P05-T03b).` under specs/05 §4.3)
1. Module `va_domain.call_resolution` (package: `__init__`, `inputs.py`, `layers.py`, `merge.py`, `resolve.py`,
   `guard_rails.py`; each < ~300 lines, radon ≤ B). Signature
   `resolve_call_config(org: OrgSettings, request: SendCallRequest, runner: RunnerInputs, refs: ResolvedRefs,
   now: datetime) -> CallResolution` where `CallResolution(config: SendCallConfig, warnings: tuple[ResolutionWarning,
   ...])` (frozen dataclasses). The §4.3 sentence "`-> SendCallConfig`" is satisfied via `.config`; warnings feed the
   `call.queued` event in T05. `ResolutionWarning(code: str, message: str, field: str | None)`.
   Failures raise `CallRequestError` (code `INVALID_INPUT` + field) — no new catalog codes.
2. Org layer (lowest above platform) = the legacy `default_*` org settings **that are explicitly set**
   (`org.model_fields_set`; `load_settings(stored)` preserves it) mapped to parameters, then
   `orgs.settings.call_defaults` over them (call_defaults is the newer, more specific mechanism):
   `default_language`→`language`, `default_record`→`record`, `default_max_duration_min`→`max_duration`,
   `default_webhook`→`webhook`, `default_webhook_events`→`webhook_events`, `timezone`→`timezone`,
   `default_country`→`country`, `summary_prompt_default`→`summary_prompt`, `dispositions_default`→`dispositions`,
   `noise_cancellation_default`→`noise_cancellation`, `background_track_default` (`phone_static`→`None`, `none`→`"none"`,
   others same)→`background_track`, `amd_default="off"`→`answered_by_enabled=False`, `memory.default_enabled`→
   `memory_enabled`, `llm.temperature_default`→`temperature` (passed through, 0–2 allowed), `post_call_eval_setup_id`→
   `post_call_evals={workbench_setup_id}`, `default_guard_rails`→`guard_rails`. `default_voice`/`default_voice_id`/
   `default_from_number` are not layered here: they are the fallback of `voice`/`from` that T03c applies when
   `merge_layers(...).voice`/`.from_` is `None` (they need a DB lookup).
   Org settings dispositions are used as-is (1–100 chars, not the request pattern).
3. Lower layers are parsed with `CallParameters.model_validate(raw)` and read through `explicit_fields()` (aliases
   folded, only set keys). `call_defaults` may additionally carry `no_input_timeout_ms` (3 000–30 000),
   `max_reprompts` (0–5), `greeting_timeout_ms` (1 000–30 000) (the derived §4.4 fields; specs/07 §12) — parsed by
   `CallDefaults(CallParameters)` with those three extra optional fields and `extra="forbid"` so typos are rejected.
   Runner fields and `phone_number` in a layer are rejected (not CallParameters fields → forbid). Invalid layer →
   `CallRequestError("INVALID_INPUT", msg, field="call_defaults.<path>")` (prefix per layer: `call_defaults`,
   `pathway.call_defaults`, `persona.config`, `agent.call_settings`, `persona_settings`).
   Also add `va_domain.call_resolution.check_call_defaults(raw)` and call it from
   `va_api.routers.org_settings.apply_settings_patch` (before writing) when `call_defaults` was sent, mapping the
   error to `AppError("INVALID_INPUT", field="call_defaults.<path>")` — so bad org defaults fail at PATCH time
   (specs/04 §7 Decision item 3 said "validated against SendCallRequest when the calls API lands"; update that line).
4. Runner layer by type (`RunnerInputs` = `TaskRunner | PathwayRunner | PersonaRunner | AgentRunner`, frozen
   dataclasses in `inputs.py`):
   - `TaskRunner()` — no layer.
   - `PathwayRef(pathway_id: UUID, version: int, call_defaults: Mapping[str, JsonValue], error_phrase: str | None,
     start_node_id: str | None = None)` (version already resolved by T03c); `PathwayRunner(pathway: PathwayRef)` →
     layer = `call_defaults`; `error_phrase` → config `error_phrase` when set.
   - `PersonaRunner(persona_id, version: int | None, legacy: bool, config: Mapping[str, JsonValue], pathway:
     PathwayRef | None)`; `persona_layer(config) -> dict` maps the specs/20 §3.1 `PersonaConfig` JSON:
     `prompt`→`task`, `first_sentence`, `voice`, `voice_settings`, `language`, `tools`, `knowledge_base_ids`,
     `memory.enabled`→`memory_enabled`, every `call_settings.*` key, `guard_rails`, `dispositions`, `summary_prompt`,
     `analysis_schema`, `post_call_evals`, `webhook`, `webhook_events`, `metadata` (null/absent keys skipped; `name`,
     `role_description`, `runner`, `sms`, `widget`, `routing`, `pathway_*` are not call parameters). The persona's
     pathway (`pathway` arg, resolved by the caller) supplies pathway_id/version/start_node and its call_defaults
     below the persona layer.
     Versioned order: org → pathway.call_defaults → request explicit → persona layer over → request
     `pathway_id`/`pathway_version`/`start_node_id` (already resolved into `pathway` by T03c when sent) →
     `persona_settings` (parsed as a layer). Legacy order: org → pathway defaults → persona → request explicit.
     `task` in the request overrides the persona prompt when the persona has no pathway (§3.1) — also for versioned
     personas; otherwise `task` is ignored with warning `TASK_IGNORED`.
   - `AgentRunner(agent_id, agent_version_id, call_settings: Mapping, pathway: PathwayRef | None, experiment_id:
     UUID | None = None, experiment_arm_id: UUID | None = None)`; layer = `call_settings` (may contain `task`,
     parsed with a `RunnerLayer(CallDefaults)` that also allows `task`); order org → pathway defaults →
     call_settings → request explicit.
   - Task with pathway (`pathway_id` + `task` sent) is a pathway runner; `task` ignored → warning `TASK_IGNORED`.
   - Pathway-backed runners (pathway, persona/agent with pathway): `transfer_phone_number`/`transfer_list` from
     any layer are dropped with warning `TRANSFER_IGNORED` (field name) — §3.3 "Ignored for pathway runners".
   - `start_node_id`: request value, else `pathway.start_node_id`; existence checked in T03c.
5. Merge rules (`merge.py`, `merge_layers(org, request, runner) -> MergedParameters`, a dict-like frozen view with
   typed accessors `voice`, `from_`, `dialing_strategy`, `tools`, `country`, `timezone`, `language`): scalars and
   arrays replace (highest set layer wins); `request_data`/`metadata` replace when the higher layer set them (even
   `{}`); `voice_settings`, `voicemail`, `warm_transfer`, `retry` merge per sent key (models: `model_fields_set`;
   `VoicemailRequest.sent()`); `transfer_list` and `analysis_schema` merge per key; `dialing_strategy` and
   `post_call_evals` replace whole (coupled fields — documented exception to "objects shallow-merge");
   `tools` = union across all layers in order, dedupe by `TL-`/`KB-` id (first position kept), inline tools keyed by
   `name` with the higher layer winning in place; `dispositions` = highest layer list, then built-ins
   (`INTERESTED, NOT_INTERESTED, FOLLOW_UP_REQUIRED, CALL_BACK_SCHEDULED, TRANSFERRED, DO_NOT_CONTACT`, constant
   `BUILTIN_DISPOSITIONS` in `va_domain.calls`) appended when absent; `guard_rails` = union keyed by
   `(type, name)`, higher layer replaces in place.
6. Config derivation (`resolve.py`):
   - `ResolvedRefs(phone_number: str (E.164), from_number: str, phone_number_id: UUID | None, telnyx_account_id:
     UUID | None, voice: ResolvedVoice, models: PlatformModels, all_party_consent: bool = False,
     ai_disclosure_state: bool = False)`; `ResolvedVoice(voice_id: UUID, provider_voice_id: str, settings:
     VoiceSettings)`; `PlatformModels(dialogue: str, router: str)` (from `LLM_DIALOGUE_MODEL`/`LLM_ROUTER_MODEL`).
   - `voice_settings` = `refs.voice.settings` → `org.voice_overrides[voice_id]` (whole object, if present) → merged
     patch keys.
   - model: `base`/`enhanced` → dialogue = `org.llm.dialogue_model or models.dialogue`; `turbo` → dialogue =
     `org.llm.router_model or models.router`; `router_model` always `org.llm.router_model or models.router`. Any other
     value: allowed when it equals one of the platform/org model ids, or when `org.llm.allow_model_override`; else
     `INVALID_INPUT` field `model` ("Model ... is not allowed for this organization.").
   - renames: `interruption_threshold`→`interruption_threshold_ms`, `max_duration`→`max_duration_min`,
     `voicemail_detect_timeout_ms`→`voicemail.detect_timeout_ms`; `tools` split into `tool_ids`/`knowledge_base_ids`
     (UUID of the suffix) and `inline_tools`; `retry` → `RetryConfig` (wait guaranteed by check_rules; a lower layer
     retry without wait → INVALID_INPUT `<layer>.retry.wait`).
   - record forced true when `post_call_evals` or `citation_schema_ids` non-empty.
   - `ivr_mode` → `voicemail.action = "ignore"`, `voicemail.sensitive = False` (AMD-off and endpointing floor are
     read from `ivr_mode` by later phases).
   - guard rails (`guard_rails.py`): after merge, add `{"type":"tcpa:ai_disclosure"}` when absent and
     (`org.ai_disclosure_required` or policy `always` or (`auto` and `refs.ai_disclosure_state`)); add
     `{"type":"tcpa:recording_disclosure"}` when absent, `record` is true and (`org.recording_disclosure_required` or
     policy `always` or (`auto` and `refs.all_party_consent`)). Org `default_guard_rails` items that do not parse as
     `GuardRail` → `INVALID_INPUT` field `default_guard_rails[i]`. (T03c/T04 compute the two booleans when the
     consent tables exist; default False.)
   - `start_time`: naive → localized with the resolved `timezone` (`zoneinfo`, `fold=0`), converted to UTC;
     must be `> now` and `<= now + 90 days` else `INVALID_INPUT` field `start_time`
     ("start_time must be in the future" / "... at most 90 days ahead"). `now` must be aware (ValueError otherwise).
   - derived: `no_input_timeout_ms`, `max_reprompts`, `greeting_timeout_ms` from call_defaults extras else §4.4
     defaults; `error_phrase` from pathway ref else default.
   - runner fields: `runner_type` = request.runner_type; `task` (task runner, persona prompt runner, agent
     call_settings.task); pathway fields from the effective `PathwayRef`; persona/agent ids/versions; experiment ids.
   - `phone_number`/`from_number`/`phone_number_id`/`telnyx_account_id` from refs; `test_mode` from request.
   - build with `SendCallConfig.model_validate(dict)` so every field is checked once.

### Files
- create `packages/va_domain/src/va_domain/call_resolution/{__init__,inputs,layers,merge,resolve,guard_rails}.py`.
- modify `packages/va_domain/src/va_domain/calls/__init__.py` + `calls/config.py` (or a new `calls/dispositions.py`):
  `BUILTIN_DISPOSITIONS`, `SYSTEM_DISPOSITIONS` constants.
- modify `apps/api/src/va_api/routers/org_settings.py` (`check_call_defaults` on PATCH) + a test in the existing
  org settings API tests (bad key → 400 INVALID_INPUT field `call_defaults.foo`; good dict accepted).
- create `apps/api/tests/support/resolved_snapshots.py` (fixture → runner inputs/refs with fixed UUIDs, fixed `now`
  2026-11-15T12:00Z, `--write` regenerates) + `apps/api/tests/fixtures/bland_send_call_resolved/NN_*.json`
  + extend `apps/api/tests/test_bland_send_call_fixtures.py` with a snapshot test (config
  `model_dump(mode="json", by_alias=True)` + warnings, byte-identical to the committed file).
  Fixture 06 `start_time` must be in the future relative to the fixed `now` (adjust fixture if needed).
- specs/05 §4.3 Decision; specs/04 §7 Decision item 3 line; AGENTS.md learning line.

### Tests (packages/va_domain/tests)
- `test_call_resolution_precedence.py`: one parametrized table of ≥ 40 cases `(id, org_settings, runner,
  body_patch, expected_subset)` built from a minimal body `{"phone_number":"+14155550100","task":"Hi"}` and
  standard refs; covers: platform defaults; each mapped org default_* (set vs unset via fields_set); call_defaults
  over default_*; pathway call_defaults over org; request over pathway; versioned persona over request; legacy
  persona under request; persona_settings over persona; agent call_settings + request; request_data/metadata
  replacement incl. `{}`; voice_settings 3-level merge incl. org voice_overrides; voicemail key merge + implied
  leave_message + ivr_mode forcing ignore; warm_transfer/retry key merge; transfer_list/analysis_schema per-key;
  dialing_strategy/post_call_evals replace; tools union/dedupe/inline name clash; KB alias; dispositions replace +
  built-ins (no duplicates) + org default; guard rails union/replace + forced ai/recording rails per policy matrix;
  record forced by evals/citations/org post_call_eval_setup_id; model mapping base/turbo/enhanced/org llm models/
  allowed explicit/rejected; amd_default off; background_track_default mapping; start_time naive localization (DST
  zone), offset form, past rejected, 90 d boundary (exactly 90 d ok, +1 s rejected); derived extras; pathway
  transfer warning; task ignored warning; persona prompt vs task.
- `test_call_resolution_layers.py`: invalid layer values → `CallRequestError` field prefixes; `check_call_defaults`
  accepts extras/rejects unknown and runner keys; persona_layer mapping table; naive `now` → ValueError.
- `merge_layers` accessor tests (voice/from_/tools for T03c).
- Results compared with plain dict subsets of `config.model_dump(mode="json", by_alias=True)`.

### Gates
`make fmt lint typecheck placeholders test`, spec refs, `npx --yes jscpd@4 --silent` (0 clones), radon. Coverage
va_domain ≥ 85 %. No call flow → no e2e.

### Risks
- jscpd on the big case table / persona mapping: keep tables as data, one builder helper.
- mypy narrowing on unions of runner dataclasses: use `match`/isinstance dispatch in one function.
- `str_strip_whitespace`/alias `from` when re-validating merged dicts: merge on canonical names, convert `from` →
  `from_number` explicitly.
- Fields_set semantics: tests must build org with `load_settings({...})`, never `OrgSettings(**defaults)` with all
  keys.

## Build P05-T03b (Builder, 2026-11-01)
Implemented `va_domain.call_resolution` (inputs/layers/merge/guard_rails/resolve) per plan, with these deviations:
- persona `prompt` is not put into the layer; `persona_prompt(config)` supplies `task` (only for a persona without
  pathway; request `task` overrides). Persona config keys null or empty list/object count as unset (stored configs
  dump defaults). Versioned persona: request `request_data`/`metadata` re-applied over the persona layer (specs/20
  §3.3). `persona_settings` applied last for legacy personas too.
- request-only keys in any layer (`start_time`, `test_call`, `test_mode`) → INVALID_INPUT.
- org `default_language`/`default_webhook_events` validated against the language table / webhook event set at
  resolution (field = setting name); other default_* mapped directly.
- runner inputs must match `request.runner_type` (ValueError otherwise).
- `BUILTIN_DISPOSITIONS`/`SYSTEM_DISPOSITIONS` in `va_domain.calls.dispositions`.
- snapshot test compares parsed JSON (prettier-formatted files); `.jscpd.json` ignores the snapshot dir.
- `PATCH /v1/org/settings` + `PATCH /v1/org` validate sent `call_defaults` (`_check_call_defaults`).
Tests: 74-case precedence table, 39 layer/error/warning/merge tests, 12 snapshots + 3 snapshot tests, 3 new API
settings cases + valid-defaults test. va_domain call_resolution coverage 100 %.
Gates: fmt 0; lint 0; mypy 948 files clean; placeholders 0; spec refs 0; jscpd 0 clones; radon avg A (3.03, no C);
make test exit 0 (8307 passed, 7 skipped, 98.60 %); pip-audit clean; pnpm audit 3 moderate (no high, unchanged).
No call flow touched → no e2e.

## Review P05-T03b (Reviewer, 2026-11-01)
Re-ran make lint typecheck placeholders (exit 0) and make test (8307 passed, 7 skipped, 98.60 %). Checked the diff against
specs/05 §4.3: layer orders per runner, request_data/metadata replacement, tools union/dedupe, dispositions plus
built-ins, guard rails keyed by type+name, record forcing, ivr_mode, model mapping, start_time localization and the 90-day
window. Spec Decision and specs/04 item 3 are updated. Approved.

## Plan P05-T03c — Send Call input loading (Planner, 2026-11-01)

Goal: one async loader that, inside the caller's `tenant_session`, runs §4.1 steps 4–8 in spec order and returns
everything `resolve_call_config` needs. No endpoint yet (P05-T05 wires it); no limits/DNC/holds (P05-T04).

### Decisions (write as `> Decision (2026-11-01): Send Call input loading (P05-T03c).` under specs/05 §4.2, before §4.3)
1. Entry point `va_api.services.call_inputs.load_call_inputs(session, ctx, request, *, org, encrypted_key, models,
   va_env) -> CallInputs`, then `resolve_inputs(inputs, request, now) -> CallResolution` (calls
   `resolve_call_config` and prepends the loader warnings). The loader raises `AppError` only (a
   `CallRequestError` from va_domain is converted 1:1: code, message, field). `encrypted_key` is the **request
   header** (§2 table), not a body field. `org` is `OrgSettings` read by the caller with `OrgSettingsReader`
   *before* the tenant session opens (the reader opens its own session).
2. Step 4 (no DB), in this order: `request.check_rules()` (RETRY_NOT_PROVIDED, RUNNER_CONFLICT) →
   `AGENT_REQUIRES_ORG_KEY` (403, field `agent_id`) when `agent_id` and the principal holds neither `*` nor any
   `agents:*` scope. The §2.3 scope table has no `agents:run`; "org-scoped key" (specs/20 §3.3) = a key that is not
   restricted away from agents. Update the specs/05 §3.1 `agent_id` row to say `*` or `agents:read`/`agents:write`.
   → `test_call` needs `pathways:write` (403 `FORBIDDEN`, field `test_call`, "test_call requires the pathways:write
   scope or a developer role.") → `test_mode` needs `ctx.environment == "test"` (403 `FORBIDDEN`, field `test_mode`,
   "test_mode requires a test environment API key.") → per specs/04 §2.2 a `test` principal when `va_env` is not
   `local`/`test` gets 403 `FORBIDDEN` "Test keys cannot place live calls, send SMS, or purchase numbers." (field
   None).
3. Step 5 resource existence, in order (each a batched `select` filtered by `org_id` and `deleted_at IS NULL` where
   the table has it):
   a. Runner (`va_api/services/call_runners.py`, `load_runner(session, ctx, request) -> LoadedRunner(runner,
      warnings)`):
      - pathway: `Pathway` absent → 404 `NOT_FOUND` field `pathway_id` ("Pathway not found."). Version: omitted/0/
        `"production"` → `production_version`; null → `draft_version` + warning `PATHWAY_UNPUBLISHED`. Integer N ≥ 1
        → that `pathway_versions` row; absent → production + `PATHWAY_VERSION_FALLBACK`; production null → 404
        `PATHWAY_VERSION_NOT_FOUND` field `pathway_version`. `"staging"` → `staging_version`, else the production
        rule (incl. draft + PATHWAY_UNPUBLISHED). `"draft"` → only with `test_call` (else 400 `INVALID_INPUT` field
        `pathway_version`, "pathway_version \"draft\" requires test_call."), uses `draft_version`.
        `PathwayRef`: `call_defaults` = pure `pathway_call_defaults(global_config)` (new in
        `va_domain.call_resolution.layers`, exported): `defaultVoice`→`voice`, `defaultLanguage`→`language`,
        `defaultTemperature`→`temperature` (non-null only), then `global_config["call_defaults"]` (object) over them;
        `error_phrase` = `errorPhrase`; `start_node_id` = id of the node whose `data.isStart` is true. A request
        `start_node_id` (or persona config one) not among the version's node ids → 400 `INVALID_INPUT` field
        `start_node_id` ("start_node_id does not exist in pathway version N.").
      - persona: `Persona` absent → 404 `NOT_FOUND` field `persona_id`. Version: `"production"`/omitted →
        `production_version`; null → `draft_version` + `PERSONA_UNPUBLISHED`; integer → that row else production +
        `PERSONA_VERSION_FALLBACK` (production null → 404 `NOT_FOUND` field `persona_version`); `"draft"` → only with
        `test_call` (same INVALID_INPUT shape), uses `draft_version` (the "identical hash" fallback is a no-op here:
        the draft row *is* production when unchanged). `legacy` = `config.get("legacy") is True`. Persona pathway:
        request `pathway_id` (+`pathway_version`) when sent, else `config.pathway_id`/`pathway_version`/
        `start_node_id` when `config.runner == "pathway"`; loaded with the pathway rules above (field prefix
        `persona.config.pathway_id` for a missing config pathway). `routing` is not evaluated (P13).
      - agent: `Agent` absent → 404 `NOT_FOUND` field `agent_id`. Version (§4.2 steps 2–3; experiment routing is
        P13-T07, so omitted = production pin and `experiment_*` stay None): omitted/`production`/`staging`/`dev`
        → `env_pins[env]` semver row (missing pin for omitted/production → newest on branch `main`; for
        staging/dev → `dev`/`latest` rule = newest on `main`); `latest` → newest on `main`; semver → exact;
        `branch:x` → newest on `x`. Missing → 404 `NOT_FOUND` field `agent_version`. Its `pathway_id`/
        `pathway_version` (integer, exact; missing row → 404 `PATHWAY_VERSION_NOT_FOUND`) give the PathwayRef.
      - task → `TaskRunner()`. Runner precedence as P05-T03a: agent, persona, pathway, task.
   b. `merged = merge_layers(org, request, runner)` (pure; may raise CallRequestError → AppError).
   c. Voice (`va_api/services/call_refs.py`, `load_voice(session, org_id, merged.voice, org) -> ResolvedVoice`):
      requested = `merged.voice` else `str(org.default_voice_id)` when set else `org.default_voice` ("Karen").
      UUID-shaped → `Voice.id` (org row or platform `org_id IS NULL`; RLS `platform_read` allows it); otherwise
      `lower(name) = lower(:v)` org rows first, then platform rows. Excluded: `deleted_at` set, `clone_status` in
      (`processing`,`failed`). None → 404 `VOICE_NOT_FOUND` field `voice` ("Voice <v> not found."). Settings via
      `VoiceSettings.model_validate(row.settings)`.
   d. Tools/KBs from `merged.tools`: `TL-` → `Tool` rows (404 `TOOL_NOT_FOUND` field `tools[i]`, "Tool TL-… not
      found."), `KB-` → `KnowledgeBase` rows (404 `NOT_FOUND` field `tools[i]`, "Knowledge base KB-… not found.").
      One query per kind; first missing in list order reported.
   e. `citation_schema_ids` (merged) → `CitationSchema` (404 `NOT_FOUND` field `citation_schema_ids[i]`);
      `post_call_evals` → `EvalSetup` by `workbench_setup_id` or `EvalSetupVersion` by `workbench_setup_version_id`
      (404 `NOT_FOUND` field `post_call_evals.<key>`); `memory_id` → `Memory` (404 `NOT_FOUND` field `memory_id`).
   f. `dialing_strategy.type == "custom_pooling"` → `NumberPool` (404 `NOT_FOUND` field `dialing_strategy.pool_id`).
4. Step 6 plan features (`plans.features` for `ctx.plan_id`, read in the same session; `plans` is system scope,
   select-granted to va_app): `citation_schema_ids` non-empty needs `citations`; any `custom` guard rail needs
   `custom_guard_rails` then > 5 custom → 400 `GUARD_RAIL_LIMIT` (field `guard_rails`); `local` strategy needs
   `local_presence`; `custom_pooling` needs `number_pools`. Failure 403 `FEATURE_NOT_IN_PLAN` with the parameter as
   field. Guard rails/citations counted on the merged values (all layers).
5. Step 7 `encrypted_key`: `TelnyxAccount` by `encrypted_key_handle` (tenant session ⇒ own org only); absent or
   `status == "deleted"` → 400 `INVALID_ENCRYPTED_KEY` "Invalid `encrypted_key`."; status `pending`/`error` → 502
   `CARRIER_CONNECTION_ERROR` "Error in creating Telnyx connection.".
6. Step 8 (`va_api/services/caller_id.py`):
   - destination: `normalize_e164(request.phone_number, merged.country)` (merged country already includes org
     `default_country` when set; else "US"); `InvalidPhoneNumberError` → 400 `INVALID_PHONE_NUMBER` field
     `phone_number`.
   - candidate filter everywhere: org row, `deleted_at IS NULL`, `status='active'`, `'voice' = ANY(capabilities)`,
     `telnyx_account_id` = account id with `encrypted_key`, `IS NULL` without (a BYO number cannot be dialed with the
     platform key).
   - explicit `merged.from_` → that row else 400 `INVALID_INPUT` field `from` ("`from` must be an active
     voice-capable number of this organization."); with `encrypted_key` and no `from` → 400 `INVALID_INPUT` field
     `from` ("`from` is required with `encrypted_key`.").
   - `local` → all candidates, `local_order(candidates, destination)` = exact NPA matches, then same state
     (`va_common.nanp`), ties by e164 ascending; first wins; none → default rule.
   - `custom_pooling` → active voice members of the pool ordered `last_used_at NULLS FIRST, phone_number_id`;
     none → default rule. (Rotation strategies, daily caps and usage updates: P10.)
   - default rule: `org.default_from_number` when set → must pass the candidate filter (else 400 `INVALID_INPUT`
     field `default_from_number`, "The org default_from_number is not an active voice number."); else, until the
     platform pool exists (`PLATFORM_POOL_ORG_ID`, specs/16 phase P10), the org's own candidates by
     `local_order` + remaining by e164; none → 400 `INVALID_INPUT` field `from` ("No caller ID available: pass
     `from`, set default_from_number, or add a phone number."). Record this fallback in a specs/16 §6.2 Decision.
   - The selection is read-only (no `last_used_at`/Redis writes; P10 adds rotation bookkeeping).
7. `CallInputs` (frozen dataclass in `call_inputs.py`): `runner`, `merged`, `refs: ResolvedRefs` (`phone_number`,
   `from_number`, `phone_number_id`, `telnyx_account_id`, `voice`, `models`; consent facts default False until
   P05-T04), `telnyx_account: ByoAccount | None` (id, call_control_app_id — never the key), `warnings`
   (loader warnings), `country` (ISO region of the destination via `phonenumbers.region_code_for_number`, for
   P05-T04 international policy).
8. `va_common.nanp`: `npa(e164) -> str | None` (`+1` + 10 digits only), `state_for_npa(npa) -> str | None`
   (USPS/province two-letter code), `same_state(a_e164, b_e164) -> bool`. Data
   `packages/va_common/src/va_common/data/npa_states.json` (sorted keys, `{"201": "NJ", ...}`, trailing newline)
   generated by `scripts/generate_npa_states.py` from the `phonenumbers` geocoder: for each NPA 200–999 sample NXX
   200..999 step 37 (valid numbers only), map description suffix (full state/province/territory name or two-letter
   code after ", ") to a code, majority vote; NPAs with no description (toll-free etc.) omitted. `--check` exits 1
   on drift. Read with `importlib.resources` once (`functools.cache`). The specs/16 §6.3 neighbour table remains P10.
9. `ApiSettings` gains `llm_dialogue_model`/`llm_router_model` (same defaults as `LlmSettings`; env table already
   lists api); the caller builds `PlatformModels` from them.

### Files
- create `apps/api/src/va_api/services/__init__.py` (docstring only), `call_inputs.py`, `call_runners.py`,
  `call_refs.py`, `caller_id.py` (each < ~250 lines, radon ≤ B).
- modify `packages/va_domain/src/va_domain/call_resolution/layers.py` + `__init__.py` (`pathway_call_defaults`).
- create `packages/va_common/src/va_common/nanp.py`, `packages/va_common/src/va_common/data/npa_states.json`,
  `scripts/generate_npa_states.py`.
- modify `apps/api/src/va_api/settings.py` (two model fields).
- specs/05 §4.2 Decision + §3.1 `agent_id` scope wording; specs/16 §6.2 Decision (platform-pool fallback, local
  selection read-only, nanp module); AGENTS.md learning line (+ command table row for the generator).

### Tests
- `packages/va_common/tests/test_nanp.py`: 415→CA, 212→NY, 416→ON, 787→PR, 800→None, non-+1 → npa None,
  same_state(+1415…, +1650…) true, (+1415…, +1212…) false; JSON keys sorted, all values 2 uppercase letters.
- `scripts/tests/test_generate_npa_states.py`: regeneration byte-identical to the committed JSON; `--check` exit 0;
  tampered copy (tmp path arg) → exit 1.
- `packages/va_domain/tests/test_call_resolution_layers.py` (extend): `pathway_call_defaults` mapping, null skip,
  `call_defaults` over the mapped keys, non-object `call_defaults` → INVALID_INPUT field
  `pathway.call_defaults`.
- `apps/api/tests/test_call_inputs_unit.py`: step-4 order (rules → AGENT_REQUIRES_ORG_KEY → test_call → test_mode →
  test key outside local/test), `local_order` (exact NPA, same state, fallback order, non-NANP destination), version
  selection helpers if written pure.
- `apps/api/tests/test_call_inputs_db.py` (module Postgres via `session_helpers.migrated_postgres()`; rows seeded
  per test with unique ids/e164s; ctx built directly): one test per error code/body (field + message) for every
  step-5..8 case above; pathway version matrix (production, null production → draft + PATHWAY_UNPUBLISHED, integer
  hit, integer miss → fallback warning, miss + null production → 404, staging hit/fallback, draft with/without
  test_call, start_node_id valid/invalid); persona versions + legacy flag + persona pathway + request pathway
  override; agent version selectors (omitted, production pin missing → newest main, semver, branch:x, latest,
  missing → 404); voice: org name beats platform name (case-insensitive), uuid of platform voice, deleted/failed
  clone → 404, org default_voice_id, default "Karen" platform voice; tools/KB/citations/evals/memory/pool 404s;
  plan feature gates (start plan rejects citations/custom rails/local/pool; enterprise accepts; 6 custom rails →
  GUARD_RAIL_LIMIT); BYO: unknown/deleted → 400, pending → 502, from required, BYO from must belong to that account,
  platform from rejected with key and vice versa; from: explicit ok/inactive/released/sms-only/other org →
  INVALID_INPUT `from`; local picks exact NPA then same state then default; pool LRU; default_from_number valid/
  invalid; fallback to own number; no numbers → INVALID_INPUT `from`. **Order tests**: a request failing at several
  steps reports the earliest (e.g. missing voice + bad phone → VOICE_NOT_FOUND; bad encrypted_key + bad phone →
  INVALID_ENCRYPTED_KEY; unknown tool + plan feature → TOOL_NOT_FOUND; AGENT_REQUIRES_ORG_KEY before missing agent).
  Happy path: `resolve_inputs` returns a `SendCallConfig` with the loaded voice/from/pathway ids and loader warnings
  first. Cross-tenant: another org's pathway/voice/tool/number/account → the same 404/400 as absent.

### Gates
`make fmt lint typecheck placeholders test`, spec refs, `npx --yes jscpd@4 --silent` (0 clones), radon, coverage
(va_api, va_common, va_domain ≥ 85 %). No call flow runs yet → no e2e.

### Risks
- Many seed helpers: put them in `apps/api/tests/support/call_input_rows.py` (pathway/version, persona, agent,
  voice, tool, kb, number, account builders) to keep jscpd at 0; reuse `va_db` `call_rows`/`model_factories`
  where they fit.
- `plans` read from va_app: confirm the grant exists (0001 grants every table); if not, read through a system repo
  listed in specs/03 §2 — prefer the existing grant.
- phonenumbers geocoder output can change between versions: the drift test pins it; regenerate deliberately.
- RLS: platform voices are visible through `platform_read`; never use `system_session` here.
- Shared module DB: unique e164s per test (`+1415555` + random 4 digits from `secrets`, not parametrized).

## Build P05-T03c (Builder, 2026-11-01)
Implemented per plan. Files:
- new `apps/api/src/va_api/services/` (`call_inputs.py` orchestration + step 4 `check_call_permissions` + `CallInputs`
  (carries `org` so `resolve_inputs(inputs, request, now)` keeps the planned signature) + `ByoAccount`;
  `call_runners.py` (agent/persona/pathway/task; `load_runner(session, org_id, request)`); `call_pathways.py`
  (split out of runners: `load_pathway`, `PathwayFields` error-field sets for request / persona config / agent);
  `call_refs.py` (voice, tools/KBs, citations/evals/memory/pool, plan features, BYO account); `caller_id.py`).
- `va_common.nanp` + generated `va_common/data/npa_states.json` (421 NPAs) + `scripts/generate_npa_states.py`.
- `va_domain.call_resolution.pathway_call_defaults` (exported); `ApiSettings.llm_dialogue_model/llm_router_model`.
- specs/05 §3.1 agent_id scope wording + §4.2 Decision P05-T03c; specs/16 §6.2 Decision; AGENTS.md row + learning.
Deviations/choices: knowledge_base_ids need no separate check (explicit_fields folds them into `tools`); a persona
config `start_node_id` error is reported as `persona.config.start_node_id` unless the request sent one; the
INVALID_PHONE_NUMBER message is fixed ("phone_number is not a valid, dialable phone number.") so the number is not
echoed; FEATURE_NOT_IN_PLAN message "<parameter> is not available on your plan.".
Tests: `test_call_inputs_unit.py` (9), `test_call_inputs_runners_db.py` (19), `test_call_inputs_refs_db.py` (21),
`va_common/tests/test_nanp.py`, `scripts/tests/test_generate_npa_states.py`, layers tests (+3). Helpers in
`apps/api/tests/support/call_input_rows.py`. New modules 100 % line+branch coverage; radon all ≤ B; jscpd 0 clones;
pip-audit clean; pnpm audit 3 moderate (unchanged).

## Review P05-T03c (Reviewer, 2026-11-01)
Approved. Re-ran make lint/typecheck/placeholders (pass, 0 findings) and make test (8383 passed, 7 skipped, 98.63 %);
new modules 100 % line+branch; jscpd 0 clones; radon nothing ≥ C; generate_npa_states --check clean. Error codes and
statuses match specs/31; step order matches specs/05 §4.1 steps 4-8; Decisions recorded in specs/05 §4.2 and specs/16
§6.2. Note: specs/05 §3.1 says `dev` → newest on main while §4.2 step 3 says env_pins[dev]; the Decision picks the pin
(missing pin → newest main), which is consistent with §4.2.

## Plan P05-T04a — Dispatch compliance checks: DNC, international policy, calling hours (Planner, 2026-11-01)
P05-T03 ticked (a/b/c done). P05-T04 split into T04a (this: `va_compliance`, Send Call §4.1 step 9) and T04b
(`va_billing`: effective limits/prices, Lua limits incl. frequency caps, holds, `dispatch.admit_call` orchestration
compliance → limits → hold with compensation). T04b moves `va_api.routers.org_schemas.effective_limits` into
`va_billing.plans` and adds the frequency-cap error code; NOT in T04a.

### Decisions (write as `> Decision (2026-11-01): dispatch compliance (P05-T04a).` under specs/05 §4.1, and a short
cross-reference Decision under specs/24 §6 item 7 and specs/28 §9.1)
1. Step 9 order: (a) high-cost prefix blocklist (specs/28 §9.2) → `NUMBER_BLOCKED`; (b) DNC (platform row, own org
   row, parent row when inherited) → `NUMBER_BLOCKED`; (c) international policy → `INTERNATIONAL_NOT_ENABLED`;
   (d) calling hours when `compliance.enforce_calling_hours` → `OUTSIDE_CALLING_HOURS`. Messages/fields:
   `NUMBER_BLOCKED` "This number is blocked." field `phone_number` (same text for every scope — never reveal which
   list); `INTERNATIONAL_NOT_ENABLED` "International calls to <REGION> are not enabled for this organization."
   field `phone_number`; `OUTSIDE_CALLING_HOURS` "The destination's local time is outside the allowed calling
   hours." field `start_time` when the call is scheduled, else `phone_number`.
2. Scheduled calls (specs/05 §4.1 says hours are skipped for scheduled calls, specs/17 §7 says they apply to the
   scheduled instant): the check takes `at` = scheduled instant (or now) and runs at creation for scheduled calls
   too; dispatch (P05-T06) re-runs the whole step with `at = now`. Recorded as the reconciliation.
3. International (reconciles specs/24 §6.7, specs/28 §9.1, specs/04 §7 settings): region = destination ISO region
   (`CallInputs.country`; non-geographic `001`/None counts as a region not in the list). Rules, first failing wins:
   region ∉ `INTERNATIONAL_ALLOWED_COUNTRIES` → blocked (always, even US orgs calling a domestic region removed from
   the env list); domestic iff region ∈ {US, CA} or region == `settings.default_country` → allowed; otherwise
   allowed iff `settings.international_enabled` AND region ∈ `settings.allowed_countries` AND (plan/KYC gate:
   `orgs.limits.international_enabled is True` (bool override only) OR `kyc_level ≥ 2` OR (`kyc_level ≥ 1` AND
   `plan_id != "start"`)). The hourly counter and new-account US/CA-only rule are T04b (`NEW_ACCOUNT_LIMIT`).
4. DNC inheritance: the flag is read from the **parent's** settings (`load_settings(parent.settings).compliance.
   dnc_inherit_from_parent`, specs/23 §2.1 "parent org setting"); direct parent only; parent rows read inside the
   caller's tenant session via `va_db.session.use_org(session, parent_id)` (RLS stays on; no `va_system`). Expired
   = `expires_at <= now`. Entries match exact `phone_e164`.
5. Calling hours: zones = `callee_timezones(e164, explicit=None, fallback=settings.timezone)`; open iff, in **every**
   zone, local time is inside an org `compliance.calling_hours` window AND (when `compliance.state_quiet_hours` and
   the destination NPA maps to a state with a rule) the state rule allows it. Windows are `[start, end)` local
   minutes. State from `va_common.nanp.state_for_npa(npa(e164))` (the specs/16 §6.3 neighbour table remains P10).
   State frequency caps (`max_calls_per_24h`) are data only here; T04b enforces them with `freq:` counters.
   Non-NANP destinations skip state rules.
6. `callee_timezones` returns a tuple (specs/17 §3.1 step 3 evaluates every candidate zone); `callee_timezone`
   (single str per §3.1 signature) = first sorted zone, kept for P11-T04 which adds `next_window_start`, holidays
   and Hypothesis DST properties. Zones from `phonenumbers.timezone.time_zones_for_number`; `("Etc/Unknown",)` or
   empty → `(fallback,)`; explicit → `(explicit,)`.
7. High-cost blocklist (specs/28 §9.2) lives in `va_compliance.fraud`: `ALWAYS_BLOCKED_PREFIXES = ("+1900",
   "+1976")`, `HIGH_RISK_PREFIXES` = the listed ones (`+371 90` → `+37190`); org `blocked_prefixes` add to them;
   enterprise orgs (`plan_id == "enterprise"`) may unblock a high-risk prefix via `orgs.limits.
   allowed_high_risk_prefixes` (list of strings, exact prefix match; ignored when not a list of str). Longest
   prefix wins; always-blocked ones are never unblocked.

### Files
- create package `packages/va_compliance` (dist `va-compliance`, uv_build, `py.typed`, deps `va-common`, `va-db`,
  `va-domain`, `phonenumbers>=9`, `tzdata`): root `pyproject.toml` dev group + `[tool.uv.sources]` workspace entry,
  mypy `mypy_path` (`packages/va_compliance/src`) and `files` (src + tests), `uv lock`.
  - `src/va_compliance/__init__.py` (docstring + re-exports of the public names below).
  - `fraud.py`: `ALWAYS_BLOCKED_PREFIXES`, `HIGH_RISK_PREFIXES: tuple[str, ...]`,
    `blocked_prefix(e164: str, *, org_blocked: Sequence[str], allowed_high_risk: Sequence[str], enterprise: bool)
    -> str | None`.
  - `dnc.py`: `@dataclass(frozen=True) DncBlock(scope: Literal["platform","org","parent"], source: str)`;
    `async def find_block(session: AsyncSession, org_id: UUID, e164: str, *, now: datetime) -> DncBlock | None`
    (one query for own+platform rows — platform wins the scope label; then parent lookup: `select(Org.parent_org_id)`
    for org, parent `Org.settings`, inherit flag, `use_org`).
  - `international.py`: `DOMESTIC_NANP = frozenset({"US","CA"})`, `parse_allowed_countries(raw: str) ->
    frozenset[str]` (comma list, strip, upper, drop empties), `international_allowed(region: str | None, *, settings:
    OrgSettings, limits: Mapping[str, object], kyc_level: int, plan_id: str, allowed_countries: frozenset[str]) ->
    bool`.
  - `calling_windows.py`: `callee_timezones(e164, *, explicit: str | None, fallback: str) -> tuple[str, ...]`,
    `callee_timezone(...) -> str`, `local_minute(now_utc, tz) -> tuple[Weekday, int]` (raises `ValueError` on naive
    datetimes), `in_window(now_utc: datetime, tz: str, windows: Sequence[CallingWindow]) -> bool`.
  - `state_rules.py` + `data/state_rules.json`: pydantic `StateRule(windows: list[CallingWindow] = [],
    no_call_days: list[Weekday] = [], max_calls_per_24h: int | None (ge 1), statute: str (min 1), source_url:
    HttpUrl (https), last_verified: date)`, extra forbid; `state_rules() -> Mapping[str, StateRule]` (cached,
    importlib.resources); `STATE_CALLING_HOURS` name exported as that mapping accessor alias per spec
    (`STATE_CALLING_HOURS = state_rules()` at import is fine, it is tiny); `state_allows(rule, weekday, minute)
    -> bool`. Entries exactly specs/17 §7: FL (08–20 all days, 3/24h), OK (08–20, 3/24h), MD (3/24h, no window),
    WA (08–20), TX (mon–sat 09–21, sun 12–21), AL/LA/MS (`no_call_days: ["sun"]`), UT (08–21). Citations to verify
    (WebFetch if available; otherwise use these well-known cites and the official statute site URL):
    FL Fla. Stat. §501.059(8)(a) / (8)(b) (leg.state.fl.us); OK 15 O.S. §775C.4 (oscn.net); MD Md. Code Com. Law
    §14-4501 et seq. Stop the Spam Calls Act (mgaleg.maryland.gov); WA RCW 80.36.390 (app.leg.wa.gov); TX Tex. Bus. &
    Com. Code §301.051 (statutes.capitol.texas.gov); AL Ala. Code §8-19C-3 (alison / law.justia acceptable only if
    no official); LA La. R.S. 45:844.14 (legis.la.gov); MS Miss. Code §77-3-725 (law.justia / official);
    UT Utah Code §13-25a-103 (le.utah.gov). `last_verified` = the date actually checked.
  - `calling_hours.py`: `calling_hours_open(at_utc: datetime, e164: str, *, settings: OrgSettings) -> bool`.
  - `compliance.py`: `@dataclass(frozen=True) OrgFacts(org_id: UUID, plan_id: str, kyc_level: int, limits:
    Mapping[str, object], settings: OrgSettings)`; `async def check_call_compliance(session, facts: OrgFacts, *,
    destination: str, region: str | None, at: datetime, now: datetime, scheduled: bool, allowed_countries:
    frozenset[str]) -> None` raising `AppError` per Decision 1 order.
- modify `apps/api/src/va_api/settings.py`: `international_allowed_countries: str` (default = spec default string)
  + property/method `allowed_destination_countries() -> frozenset[str]` via `parse_allowed_countries`; add
  `va-compliance` to `apps/api/pyproject.toml` deps (the endpoint in P05-T05 calls it; adding the dep now keeps T05
  small). Do NOT wire into a route yet (no route exists).
- specs: Decisions above (specs/05 §4.1, specs/24 §6 item 7 cross-ref, specs/28 §9.1/§9.2 cross-ref, specs/17 §7
  cross-ref for scheduled instant + `callee_timezones`). AGENTS.md learning line (package, OrgFacts, data file).

### Tests (`packages/va_compliance/tests`, conftest re-exporting nothing special; DB tests via a module fixture using
`migration_helpers.migrated_postgres()` / `tenant_rows.open_database(url)` from va_db test support — add
`packages/va_db/tests/support` is already on pythonpath)
- `test_fraud.py`: `+19005551234` blocked always (also for enterprise with it in allowed list); `+88212345678`
  blocked; enterprise + `allowed_high_risk=["+882"]` → allowed; non-enterprise with same list → blocked; `+37190…`
  blocked, `+3712…` not; org `blocked_prefixes=["+4420"]` blocks `+44207…`; longest prefix returned; `+1809…` not
  blocked; every `HIGH_RISK_PREFIXES` entry matches `^\+[0-9]+$` and spec list parity (hard-coded expected tuple).
- `test_international.py`: matrix — US org → CA allowed; US org → GB with defaults blocked; GB home org → GB allowed
  (domestic) only if GB in env list; env list without GB → GB blocked even for GB home; enabled + allowed_countries
  incl GB + kyc 2 start → allowed; kyc 1 start → blocked; kyc 1 build → allowed; kyc 0 build + limits override True
  → allowed; override `"true"` string ignored; region None / "001" → blocked; `parse_allowed_countries(" us, ca ,,")`.
- `test_calling_windows.py`: single-zone NPA (`+1212…` → America/New_York), multi-zone NANP number (pick an NPA
  phonenumbers maps to 2 zones, e.g. find one in the test via the library — assert len > 1 rather than hard-code if
  unstable), unknown → fallback, explicit wins; `in_window` table: NY 08:00 open/07:59 closed/20:59 open/21:00 closed;
  DST spring-forward 2026-03-08 and fall-back 2026-11-01 in America/New_York (same UTC instant, local hour differs),
  Europe/London 2026-03-29, Australia/Lord_Howe half-hour DST (2026-10-04); weekday-limited window; naive datetime →
  ValueError.
- `test_state_rules.py`: JSON parses; exactly keys {FL, OK, MD, WA, TX, AL, LA, MS, UT}; every entry has statute,
  https source_url, last_verified ≤ today; FL max 3; TX Sunday 11:59 closed / 12:00 open; AL Sunday closed, Monday
  open; MD has no window (always allows hours).
- `test_calling_hours.py`: org default 08–21: FL number (`+1305…`) at 20:30 local closed with state_quiet_hours, open
  without; TX Sunday 10:00 closed; custom org windows; multi-zone number open only when open in all zones; non-NANP
  (`+4420…`) uses only org windows in Europe/London.
- `test_dnc_db.py` (Postgres): own entry blocks (scope org); platform entry (inserted via `system_session`) blocks
  (scope platform) for any org; expired entry ignored, future expiry blocks; other org's entry does not block
  (cross-tenant); parent entry blocks child when parent setting default true; parent with
  `dnc_inherit_from_parent=false` → child not blocked; child's own setting false but parent true → still blocked
  (flag is parent's); grandparent ignored; org without parent fine; RLS context restored after parent lookup (a
  following query in the same session still sees only the child's rows).
- `test_compliance_db.py`: order — blocked prefix + DNC → NUMBER_BLOCKED; DNC + international → NUMBER_BLOCKED;
  international + outside hours → INTERNATIONAL_NOT_ENABLED; outside hours only with enforce true →
  OUTSIDE_CALLING_HOURS (field `phone_number`), scheduled → field `start_time` and evaluated at `at` (open `at`
  passes even if `now` closed); enforce false → passes at 03:00 local; exact error bodies (code/status/message/field).
- `apps/api/tests`: settings test for `allowed_destination_countries()` default and env override.

### Gates
`make fmt lint typecheck placeholders test`, spec refs, `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a`
(≤ B), coverage va_compliance ≥ 85 % (target 100 % of new modules), `uvx pip-audit`, `corepack pnpm audit`. No call
flow yet → no e2e.

### Risks
- phonenumbers timezone data for NANP: some NPAs map to several zones; tests must derive a multi-zone sample from the
  library (search NPAs 200–999 for one with >1 zone) instead of hard-coding.
- Platform DNC rows: va_app cannot insert `org_id IS NULL` rows (RLS); seed via `system_session` / login role.
- `use_org` must restore `app.org_id`; assert with a follow-up query.
- New package wiring (root pyproject, mypy paths, uv.lock, Dockerfile unaffected) — follow the P00-T05 AGENTS line.
- Statute citations: do not invent URLs that 404; prefer official legislature domains; `last_verified` truthfully.

## Build P05-T04a (Builder, 2026-11-01)
Implemented per plan: new `packages/va_compliance` (fraud, dnc, international, calling_windows, state_rules + data
JSON, calling_hours, compliance.check_call_compliance + OrgFacts), wired into root pyproject (dev dep, uv source,
mypy paths/files, pytest pythonpath for tests/support), uv.lock; api depends on it; `ApiSettings.
international_allowed_countries` + `allowed_destination_countries()`. Specs: Decisions in 05 §4.1, 17 §7, 23 §2.1,
24 §6, 28 §9.2. AGENTS.md line.
Deviations / choices (all recorded in specs):
- Statute verification (web search) changed 4 state entries to the stricter law: MD 08–20 window, UT no Sunday,
  AL/LA Mon–Sat 08–20 + no Sunday; FL cite is §501.616(6) (not 501.059). AL cite is the PSC rule 770-X-5-.17,
  LA R.S. 45:811; MS §77-3-723(1) via law.justia (official text not reachable; Sunday ban kept as in spec).
  Holidays not modelled (P11-T04).
- `callee_timezones` falls back when phonenumbers returns > 8 zones (NANP non-geographic like +1 500 maps to 42
  zones; intersecting would close the window forever).
- `BLOCKED_PREFIXES` (spec name) exported = ALWAYS + HIGH_RISK.
- No route wired (P05-T05); no call flow touched → e2e not run.
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 8491 passed / 7 skipped; va_compliance
100 % line+branch; spec refs 0 problems; jscpd 0 clones; radon max B (8); pip-audit clean; pnpm audit 3 moderate
(unchanged); openapi current.

## Review P05-T04a (Reviewer, 2026-11-01)
Approved. Re-ran make lint/typecheck/placeholders (pass, 0 findings), make test (8491 passed, 7 skipped, 98.64 %),
jscpd 0 clones, radon nothing ≥ C, spec refs 0 problems. Error codes/statuses match specs/31; blocklist matches
specs/28 §9.2; INTERNATIONAL_ALLOWED_COUNTRIES default matches specs/02 §6 and .env.example. State table deviations
from specs/17 §7 (stricter MD/UT/AL/LA after statute verification) are recorded as a Decision. Next: P05-T04b.

## Plan P05-T04b — Dispatch limits and credit holds (Planner, 2026-11-01)
New package `packages/va_billing` (spec owner specs/24). Send Call §4.1 steps 10–11 plus the orchestration
`dispatch.admit_call` (step 9 → 10 → 11 with compensation). No route yet (P05-T05 wires it).

### Decisions (write as `> Decision (2026-11-01): dispatch limits and holds (P05-T04b).` under specs/24 §6 (after
the P05-T04a block), short cross-refs under specs/05 §4.1 (after the P05-T04a Decision), specs/24 §5, specs/17 §7,
specs/28 §9.3; add the new code row to specs/31 §3)
1. Order of step 10 follows specs/24 §6 (the owning spec), reconciling specs/05 §4.1's listing:
   (a) same-number `SET cap:dest:{org}:{e164} 1 NX PX 10000` → `SAME_NUMBER_TOO_SOON` (message exactly the catalog
   text "Please wait at least 10 seconds between calls to the same number.", field `phone_number`); never rolled
   back (a failed later step still blocks a rapid retry); (b) per-destination frequency `freq:{org}:{e164}:{yyyymmdd}`
   (TTL 172 800) → new code `DESTINATION_FREQUENCY_CAP` 429 "This number has reached its daily call limit." field
   `phone_number`; cap = min(5 (specs/28 §9.3) or `orgs.limits.per_destination_daily_calls` int override for
   enterprise only, state rule `max_calls_per_24h` when `compliance.state_quiet_hours` and
   `va_compliance.calling_hours.destination_state_rule(e164)` has one). The 24 h of the state statutes is
   approximated by the UTC calendar day of the spec key (documented). (c) new account (org `created_at` > now−24 h):
   destination region ∉ {US, CA} → `NEW_ACCOUNT_LIMIT` "New accounts can call only US and Canadian numbers during
   their first 24 hours." (specs/28 §9.3); the daily cap becomes min(cap,100) and concurrency min(cap,10) (None →
   100/10); (d) daily `cap:org:{org}:daily:{yyyymmdd}` TTL 172 800; (e) hourly `cap:org:{org}:hourly:{yyyymmddhh}`
   TTL 7 200; (f) concurrency zset `cap:org:{org}:concurrency` score `now_ms + (max_duration_min+5)·60 000`;
   (g) international hourly `cap:org:{org}:intl:{yyyymmddhh}` TTL 7 200, only when region ∉ {US, CA,
   settings.default_country} (same "domestic" as va_compliance), caps start 10 / build 100 / scale 300 /
   enterprise unlimited, `orgs.limits.international_hourly_calls` int override wins → `INTERNATIONAL_RATE_LIMITED`.
   When (d)/(f) fail only because of the new-account tightening (the plan cap alone would allow it) the code is
   `NEW_ACCOUNT_LIMIT` "New accounts are limited to 100 calls per day and 10 concurrent calls during their first
   24 hours." else `DAILY_CAP_REACHED` "Daily call limit reached for this organization." / `HOURLY_CAP_REACHED`
   "Hourly call limit reached for this organization." / `CONCURRENCY_CAP_REACHED` "Concurrent call limit reached for
   this organization." / `INTERNATIONAL_RATE_LIMITED` "International call limit per hour reached for this
   organization.". All 429 limit errors carry no `field`, except (a)/(b).
2. Unlimited caps (NULL) still count (capped_incr with cap −1 = no cap) so `GET /v1/billing/limits` can report
   `used`; concurrency with no cap still records the member (cap −1).
3. Rollback: (b),(d),(e),(f),(g) are undone in reverse acquisition order by `LimitTicket.rollback()`; counters via
   `counter_release.lua` (DECR only when the key exists and is > 0, so a rollback never creates a TTL-less key),
   concurrency via ZREM (idempotent). `rollback()` is idempotent (second call no-op) and swallows/logs Redis errors
   per key (`limits.rollback_failed`, keeps going) — counters expire anyway.
4. Redis unavailable during acquire → `AppError("SERVICE_UNAVAILABLE", "Call limits are temporarily
   unavailable.")` after rolling back what was acquired (fail closed: limits protect spend; unlike the request-rate
   bucket which fails open).
5. `batch_concurrency_share` (specs/17 §8): `acquire(..., concurrency_share=1.0)`; batch callers pass the setting;
   effective cap = max(1, floor(cap × share)).
6. Scheduled calls: `admit_call(..., scheduled=True)` runs step 9 only (at = scheduled instant); limits AND hold
   happen at dispatch (holds expire `max_duration + 2 h` after reserve, so a hold taken days ahead would lapse).
7. Holds (specs/24 §5): amount = `min(max_duration_min, 10) × connected_rate_micros_per_min + min_attempt_micros`;
   skipped (returns None) when `orgs.credit_limit_micros >= 1_000_000_000` — that is the meaning of
   `settings.billing.skip_holds` (no such settings key is added). `reserve` locks the org row FOR UPDATE, available
   = balance − Σ held + credit_limit, `INSUFFICIENT_CREDITS` "Insufficient credits to place this call." when
   available < amount; inserts `credit_holds(status='held', reference_type='call', reference_id=str(call_id),
   expires_at=now + max_duration + 2 h)`; no ledger row, balance unchanged. `capture`/`release` are conditional
   UPDATEs from `held` returning bool (idempotent).
8. Override keys: specs/24 §2 list gains `international_hourly_calls`, `per_destination_daily_calls` (ints;
   `per_destination_daily_calls` honoured only on the enterprise plan). The api `OrgLimits` view keeps its current
   fields (no OpenAPI change).

### Files
- `packages/va_billing/` (dist `va-billing`, uv_build, `py.typed`; deps `va-common`, `va-db`, `va-domain`,
  `va-compliance`, `redis>=5`, `sqlalchemy[asyncio]`, `pydantic`) — root `pyproject.toml`: dev group entry,
  `[tool.uv.sources]`, mypy `mypy_path` (`packages/va_billing/src`, `packages/va_billing/tests/support`) and `files`
  (src+tests), pytest `pythonpath` (`packages/va_billing/tests/support`); `uv lock`. `apps/api/pyproject.toml` adds
  `va-billing`.
  - `src/va_billing/__init__.py` docstring + re-exports.
  - `plans.py`: `INT_OVERRIDE_KEYS` (moved from api + the two new keys), `BOOL_OVERRIDE_KEYS`,
    `ENTERPRISE_PLAN = "enterprise"`, `INTERNATIONAL_HOURLY_CAPS = {"start":10,"build":100,"scale":300}`,
    `DEFAULT_PER_DESTINATION_DAILY = 5`, `int_override(value) -> int | None` (bool excluded),
    `@dataclass(frozen=True, slots=True) EffectiveLimits` (every int key + `international_enabled: bool | None` +
    `international_hourly_calls`, `per_destination_daily_calls`), `effective_limits(plan: Plan | None, overrides)
    -> EffectiveLimits`; `@dataclass(frozen) EffectivePricing(connected_rate_micros_per_min,
    transfer_rate_micros_per_min, min_attempt_micros, sms_rate_micros, widget_message_micros,
    tts_rate_micros_per_1k_chars)` with `snapshot() -> dict[str,int]` (for `calls.config.pricing`, P05-T05);
    `effective_pricing(plan: Plan, overrides) -> EffectivePricing` (override keys of §2 for the four rate keys;
    min_attempt and tts from plan); `PriceMeter = Literal[...]` + `effective_price(pricing, meter) -> int`.
  - `apps/api/src/va_api/routers/org_schemas.py`: delete its key tuples/`_int_override`; `effective_limits(plan,
    overrides) -> OrgLimits` becomes `OrgLimits(**{f: getattr(base, f) for f in OrgLimits.model_fields})` over
    `va_billing.plans.effective_limits` (keep the api name; existing `test_org_limits.py` must still pass; keep
    `INT_OVERRIDE_KEYS`/`BOOL_OVERRIDE_KEYS` importable from org_schemas as re-exports only if something imports
    them — grep first).
  - `redis_scripts/capped_incr.lua` (KEYS[1]; ARGV cap, ttl_s → `{ok, value}`: INCR; if value==1 EXPIRE; if cap>=0
    and value>cap then DECR, return {0, value-1}; else {1, value}), `redis_scripts/counter_release.lua` (if
    tonumber(GET)>0 then DECR), `redis_scripts/concurrency_acquire.lua` (KEYS[1]; ARGV member, cap, now_ms,
    expiry_ms: ZREMRANGEBYSCORE -inf now_ms; if ZSCORE member exists → ZADD XX new expiry, return 1 (re-acquire of
    the same call is idempotent); if cap>=0 and ZCARD>=cap → 0; ZADD; PEXPIRE key to max(existing pttl, expiry−now)
    so an abandoned set disappears; return 1). Loaded via `importlib.resources` + `redis.register_script` (EVALSHA
    with NOSCRIPT fallback) — `scripts.py`: `LimitScripts(redis)` with typed async wrappers.
  - `keys.py`: `same_number_key`, `frequency_key(org, e164, day)`, `daily_key(org, now)`, `hourly_key`,
    `concurrency_key`, `international_key` — exact specs/03 Redis-table strings; UTC formatting.
  - `limits.py`: `@dataclass(frozen) LimitInputs(org_id, plan_id, org_created_at, limits: EffectiveLimits,
    settings: OrgSettings, destination, region, call_id: UUID, max_duration_min, concurrency_share=1.0)`;
    `class CallLimiter(redis, *, clock: Clock)`: `async acquire(inputs) -> LimitTicket` (Decision 1–5);
    `async release_call(org_id, call_id) -> None` (ZREM, idempotent; used at call end by P05-T06/T11);
    `async active_calls(org_id) -> int` (ZREMRANGEBYSCORE + ZCARD) and `async usage(org_id) -> LimitUsage(daily,
    hourly, international, concurrency)` for the later limits endpoint. `LimitTicket.rollback()` per Decision 3.
    Message constants exported.
  - `holds.py`: `CALL_HOLD_CAP_MINUTES = 10`, `SKIP_HOLDS_CREDIT_LIMIT_MICROS = 1_000_000_000`, `HOLD_GRACE =
    timedelta(hours=2)`, `call_hold_amount(max_duration_min, pricing) -> int`, `holds_skipped(credit_limit) -> bool`,
    `available_balance(session, org_id) -> int` (balance − held + credit_limit), `async reserve(session, org_id,
    amount_micros, *, reference_type, reference_id, now, expires_at) -> CreditHold`, `reserve_call_hold(session,
    org_id, call_id, *, max_duration_min, pricing, now) -> CreditHold | None`, `capture(session, hold_id) -> bool`,
    `release(session, hold_id) -> bool`. Move `CreditTransferRepo._held` into a public
    `va_db.repositories.credits.held_micros(session, org_id)` used by both (no duplicate query; jscpd).
  - `dispatch.py`: `@dataclass(frozen) BillingOrg(org_id, plan_id, kyc_level, created_at, credit_limit_micros,
    limits_raw, settings: OrgSettings, limits: EffectiveLimits, pricing: EffectivePricing)`;
    `async load_billing_org(session, org_id) -> BillingOrg` (Org + Plan in the caller's tenant session; missing org
    → `NOT_FOUND`); `@dataclass(frozen) CallAdmission(call_id, destination, region, max_duration_min, at, scheduled,
    allowed_countries, concurrency_share=1.0)`; `@dataclass Admitted(pricing, hold: CreditHold | None, ticket:
    LimitTicket | None)` with `async compensate(session, limiter)` (ticket rollback + `holds.release` + limiter
    `release_call`); `async admit_call(session, limiter, org: BillingOrg, call: CallAdmission, *, now) -> Admitted`:
    `check_call_compliance(OrgFacts(...))` → if scheduled return Admitted(pricing, None, None) → `limiter.acquire`
    → `reserve_call_hold` inside `try`; on any exception roll the ticket back (and release the concurrency member) and
    re-raise.
- `packages/va_common/src/va_common/errors.py`: `"DESTINATION_FREQUENCY_CAP": 429` after `SAME_NUMBER_TOO_SOON`;
  specs/31 §3 row at the same position ("Per-destination daily call cap (5/day/number, stricter state rules)").
- Specs: Decisions above; AGENTS.md learning line (va_billing modules, Lua files, test helpers).

### Tests (`packages/va_billing/tests`, conftest with `isolated` nothing special; fakeredis[lua] runs the Lua)
- `test_plans.py`: seed-like Plan objects (build/start/enterprise) → effective_limits values; override precedence
  (int, bool, bool-as-int ignored, string ignored); intl hourly caps per plan + override; per-destination override
  ignored off enterprise; effective_pricing override > plan; `snapshot()` keys; `effective_price` for each meter.
- `test_keys.py`: exact key strings incl. UTC conversion of a non-UTC aware datetime (and naive → ValueError).
- `test_scripts.py` (fakeredis): capped_incr boundary (cap 2: ok,ok,fail with value 2), TTL set on first incr only,
  cap −1 unlimited; counter_release never below 0 and does not create the key; concurrency acquire self-heal of
  expired member, cap reached → 0, same member re-acquire idempotent, PEXPIRE set.
- `test_limits.py` (fakeredis + `FrozenClock`/`ManualClock` advance): each error code with exact message/status/field;
  order: same-number before frequency before new-account before daily…; same-number expires after 10 s (advance
  clock + fakeredis time? → use real PX: set key and assert PTTL ≤ 10 000 instead of waiting); daily key rolls at
  UTC midnight; hourly rolls; new-account tightening gives NEW_ACCOUNT_LIMIT at 101st/11th, plan cap below 100 gives
  DAILY_CAP_REACHED; new account non-US/CA → NEW_ACCOUNT_LIMIT; intl counter only for non-domestic; FL number with
  state_quiet_hours → frequency cap 3, without → 5; enterprise override; rollback when hourly fails restores daily
  and frequency (exact counter values) and not the same-number key; rollback when concurrency fails; ticket rollback
  idempotent; concurrency share 0.5; release_call idempotent; Redis down (closed fakeredis server
  `server.connected = False`) → SERVICE_UNAVAILABLE and nothing left incremented after reconnect.
- `test_limits_redis.py` (`@pytest.mark.integration`, `redis_container.running_redis_url`, one module): 100
  concurrent `capped_incr` on cap 50 → exactly 50 ok and the key value 50; 100 concurrent concurrency acquires cap 10
  → 10 members; self-heal with past expiry; release idempotent. Pool `max_connections=100`.
- `test_holds_db.py` (Postgres via the va_db support `migrated_postgres()` / `open_database`, module fixture;
  orgs via `make_org`-style helper with balance/credit_limit): amount math (max_duration 3 → 3×rate+min; 30 → 10×);
  reserve success inserts held row, balance unchanged; available math with existing holds and credit_limit;
  INSUFFICIENT_CREDITS exact; skip when credit_limit ≥ 1e9 → None and no row; capture/release idempotent (second
  call False; capture after release False); expires_at; two concurrent reserves for the last credit → exactly one
  succeeds (FOR UPDATE).
- `test_dispatch_db.py`: compliance failure → no counters touched, no hold; limit failure → no hold; hold failure →
  counters rolled back (daily/hourly/intl/freq back to 0, concurrency member removed); scheduled → only compliance
  (no Redis keys, no hold); success → ticket + hold + pricing; `compensate` undoes all; `load_billing_org` reads
  plan + overrides, missing org NOT_FOUND.
- `packages/va_common/tests/test_errors.py` keeps passing (catalog parity) with the new row; api `test_org_limits.py`
  unchanged and green.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `make openapi` (expect no
drift), `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a` (≤ B), va_billing coverage 100 % new modules
(≥ 85 % gate), `uvx pip-audit` on `uv export`, `corepack pnpm audit`. No call flow → no e2e.

### Risks
- fakeredis Lua + `register_script`: works (`fakeredis[lua]`); real-Redis tests must skip without Docker
  (`docker_probe.docker_available()`).
- Redis Lua numbers: return integers only (no floats) from scripts.
- Holds and the org-row lock: `orgs` has no RLS; select `Org ... FOR UPDATE` inside the tenant session (as
  `CreditTransferRepo._lock`); `credit_holds` insert relies on `app.org_id` == org.
- Moving `effective_limits` must not change `/v1/org` output (OpenAPI drift gate + existing tests).
- Keep `limits.acquire` ≤ radon B: one small method per step, a list of undo callables in the ticket.

## Build P05-T04b (Builder, 2026-11-01)
Implemented per plan. New `packages/va_billing` (plans, keys, scripts + 3 Lua files, limits, holds, dispatch);
`held_micros` moved into `va_db.repositories.credits` (CreditTransferRepo uses it); api `org_schemas.effective_limits`
delegates to va_billing; error code `DESTINATION_FREQUENCY_CAP` (errors.py + specs/31; test_errors count 63).
Additions vs plan: the frequency cap also honours `compliance.max_attempts_per_number_per_day` (>0); load_billing_org
raises SERVICE_UNAVAILABLE "Billing is not configured for this organization." when orgs.plan_id names no plan (no FK);
shared test helper `redis_container.flushed_redis`. Decisions: specs/24 §6 (main) + cross-refs specs/24 §2/§5,
specs/05 §4.1, specs/17 §8, specs/28 §9.3.
Gates: make lint/typecheck/placeholders pass; make test 8560 passed, 7 skipped, 98.65 %; va_billing 100 % line+branch
(68 tests incl. real-Redis + Postgres); jscpd 0 clones; radon avg A, nothing ≥ C; spec refs 0; openapi no drift;
pip-audit clean; pnpm audit 3 moderate (unchanged). No call flow → no e2e.

## Review P05-T04b (Reviewer, 2026-11-01)
Gates re-run: make lint 0, make typecheck 0, make placeholders 0, make test 8560 passed / 7 skipped, 98.65 %.
Spec check: keys match the specs/03 Redis table; order/messages match the specs/24 §6 Decision; error catalog row + count
OK; Lua scripts are atomic and covered by the real-Redis test; dispatch compensation looks right. One finding:
1. `packages/va_billing/src/va_billing/holds.py` `_settle` (used by `capture`/`release`): the conditional
   `UPDATE credit_holds ... WHERE id = :hold_id AND status = 'held'` does not filter by `org_id`. It relies only on RLS.
   That breaks guardrail 1003 / master prompt rule 5 ("every tenant query ... filters by org_id") and the repository
   convention (`CallRepo.transition`, `CallLegRepo.advance` all add `Model.org_id == self.org_id`). Fix: make it
   `capture(session, org_id, hold_id)` / `release(session, org_id, hold_id)`, add `CreditHold.org_id == org_id` to the
   WHERE, update `Admitted.compensate` (it has `self.org_id`), the specs/24 §6 Decision wording if needed, and add a test
   that capture/release with another org's id (inside the hold's own org session) returns False and leaves the row `held`.

## Fix P05-T04b review (Builder, 2026-11-01)
Finding 1 fixed: `holds.capture/release(session, org_id, hold_id)`; `_settle` filters `CreditHold.org_id == org_id`;
`Admitted.compensate` passes `self.org_id`; specs/24 §6 Decision wording updated; new test
`test_capture_and_release_filter_by_org` (fails without the filter). Gates: fmt 0, lint 0, typecheck 0, placeholders 0,
make test 8561 passed / 7 skipped, 98.65 %; va_billing 69 passed; jscpd 0; spec refs 0; radon A.

## Re-verify P05-T04b fix (Builder, 2026-11-01)
review.rejected re-delivered; finding 1 already fixed (holds `_settle` filters `CreditHold.org_id`, test
`test_capture_and_release_filter_by_org`). Gates re-run: fmt 0, lint 0, typecheck 0, placeholders 0, spec refs 0,
make test 8561 passed / 7 skipped, 98.65 %; va_billing 69 passed.

## Review P05-T04b round 2 (Reviewer, 2026-11-01)
Approved. lint 0, typecheck 0, placeholders 0, make test 8561 passed / 7 skipped, 98.65 %. Finding 1 fixed: `_settle`
filters `CreditHold.org_id`, `compensate` passes org_id, test `test_capture_and_release_filter_by_org` covers it.

## Plan P05-T05a — Shared destination URL guard and Send Call webhook policy (Planner, 2026-11-01)
Context: P05-T04 ticked (a+b done). P05-T05 split into T05a (this) and T05b (dispatch service + `POST /v1/calls` +
CallWorkflow start; planned next iteration). T05b wires `check_call_webhook` into the dispatch right after
`resolve_inputs` (it checks the *resolved* `config.webhook`, so an org `default_webhook` is checked too).

### Decisions (write as `> Decision (2026-11-01): destination URL guard (P05-T05a).` under specs/19 §8 after the SSRF
bullet; one-line cross-refs under specs/13 §9 and specs/28 §6.4 and next to the specs/05 §3.7 `webhook` row as a
Decision paragraph after the §3.7 table)
1. Module `va_common.net` (not `assert_public_destination` in specs/13 §9 wording: the name is
   `check_destination_url`; update the specs/13 §9 sentence to the real name). The pinning HTTP client
   (`safe_client`, specs/28 §6.4) is NOT part of this task; it arrives with tools/webhook delivery. Returning the vetted
   addresses lets that client pin later.
2. Blocked address rule `blocked_address(ip) -> bool`: an IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) is judged by its
   embedded IPv4; then blocked when `not ip.is_global` OR `ip.is_multicast` OR `ip.is_reserved` OR
   `ip.is_unspecified`. This is a superset of the specs/19 §8 list plus the specs/28 §6.4 extras (0/8, 192.0.0.0/24,
   198.18/15, 240/4, ff00::/8, mapped private) — tests parametrize every range named in both specs.
3. Metadata addresses `169.254.169.254` and `fd00:ec2::254` are ALWAYS blocked, even with `allow_private`
   (local/test `WEBHOOK_ALLOW_PRIVATE=true` relaxes only the private/loopback rule so the simulators on localhost work).
4. URL rules: scheme `https`, or `http` only when `allow_http`; a hostname is required; IP-literal hosts (incl.
   `[v6]`) are checked directly without DNS; otherwise DNS resolves once (`getaddrinfo`, all A/AAAA answers) with a
   timeout (`timeout_s`, default 2.0) and EVERY answer must pass (a mixed public/private answer is rejected); empty
   answer / `OSError` / timeout → reason `resolution`.
5. `UnsafeDestinationError(reason)` with `reason: Literal["scheme", "host", "resolution", "address"]`.
6. Send Call webhook policy (specs/05 §3.7): `http` allowed when `VA_ENV` is `local`/`test` or org
   `allow_insecure_webhooks` is true; private targets allowed only with `WEBHOOK_ALLOW_PRIVATE` (forced false in
   staging/production). Error `WEBHOOK_URL_REJECTED` (400), field `webhook`, messages per reason:
   scheme → "webhook must use https.", host → "webhook must be an absolute http(s) URL with a host.",
   resolution → "The webhook host could not be resolved.", address → "The webhook URL resolves to a blocked address.".
7. `WEBHOOK_ALLOW_PRIVATE` moves into a shared settings mixin `va_common.settings.WebhookSafetySettings` (field
   `webhook_allow_private: bool = True`, model validator forces False in staging/production — exactly the current
   dispatcher logic). `DispatcherSettings` and `ApiSettings` both inherit it (specs/02 §6 already lists api). No
   `.env.example` change (key exists).

### Files
- NEW `packages/va_common/src/va_common/net.py`:
  - `type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address`; `type HostResolver = Callable[[str],
    Awaitable[list[str]]]`; `METADATA_ADDRESSES: Final = frozenset({ip("169.254.169.254"), ip("fd00:ec2::254")})`;
    `DNS_TIMEOUT_S: Final = 2.0`; `type UnsafeReason = Literal["scheme","host","resolution","address"]`.
  - `class UnsafeDestinationError(Exception)` with `.reason` (message `f"destination rejected: {reason}"`).
  - `async def system_resolver(host: str) -> list[str]` (moved from `va_api.auth.oidc_discovery`: `loop.getaddrinfo(
    host, None, type=socket.SOCK_STREAM)` → unique `sockaddr[0]` strings; strip IPv6 zone `%`).
  - `def parse_ip(host: str) -> IpAddress | None` (strips `[...]`).
  - `def blocked_address(address: IpAddress, *, allow_private: bool = False) -> bool` (Decision 2/3; with
    allow_private only METADATA_ADDRESSES, multicast, unspecified, reserved stay blocked).
  - `async def check_destination_url(url: str, *, allow_http: bool, allow_private: bool, resolver: HostResolver =
    system_resolver, timeout_s: float = DNS_TIMEOUT_S) -> tuple[IpAddress, ...]` (Decision 4; `urlsplit` ValueError
    → `host`; timeout via `asyncio.timeout(timeout_s)`).
- `packages/va_common/src/va_common/settings.py`: `WebhookSafetySettings(BaseServiceSettings)` + `_PRIVATE_FORBIDDEN`;
  export in `__all__`.
- `apps/webhook_dispatcher/src/va_webhooks/settings.py`: `DispatcherSettings(WebhookSafetySettings, RedisSettings,
  DatabaseSettings)`, delete its own field/validator (existing dispatcher settings tests must still pass unchanged).
- `apps/api/src/va_api/settings.py`: add `WebhookSafetySettings` to `ApiSettings` bases.
- `apps/api/src/va_api/auth/oidc_discovery.py`: import `HostResolver`, `system_resolver`, `parse_ip` from
  `va_common.net` (delete the local copies / `_is_ip`); `_check_host` keeps its own stricter rule (`is_global` for
  every address — OIDC never allows private even in production) but uses `parse_ip`. Re-export nothing new; grep
  tests for `oidc_discovery.system_resolver` and update imports if any.
- NEW `apps/api/src/va_api/services/webhook_policy.py`: `MESSAGES: Final[dict[UnsafeReason, str]]`,
  `INSECURE_ENVIRONMENTS = frozenset({Environment.LOCAL, Environment.TEST})`,
  `async def check_call_webhook(url: str | None, *, org: OrgSettings, settings: ApiSettings, resolver: HostResolver =
  system_resolver) -> None` (None → return; `allow_http = settings.va_env in INSECURE_ENVIRONMENTS or
  org.allow_insecure_webhooks`; `allow_private = settings.webhook_allow_private`; maps `UnsafeDestinationError` to
  `AppError("WEBHOOK_URL_REJECTED", MESSAGES[reason], field="webhook")` chained).
- Specs: Decisions above (specs/19 §8 main; specs/13 §9 name fix + cross-ref; specs/28 §6.4 cross-ref; specs/05 §3.7).
- AGENTS.md learning line: `va_common.net.check_destination_url` / `blocked_address` / `WebhookSafetySettings`,
  `check_call_webhook`, inject `resolver=` in tests (never real DNS).

### Tests
- NEW `packages/va_common/tests/test_net.py` (no real DNS: a dict-backed fake resolver; `system_resolver` tested only
  with an IP literal / `localhost`-free path: call it with `"127.0.0.1"` which getaddrinfo resolves without network):
  - `blocked_address` parametrized blocked: `10.0.0.1, 172.16.0.1, 172.31.255.255, 192.168.1.1, 127.0.0.1,
    169.254.169.254, 169.254.1.1, ::1, fc00::1, fd12::1, fe80::1, 100.64.0.1, 100.127.255.255, 0.0.0.1, 192.0.0.8,
    198.18.0.1, 198.19.255.255, 240.0.0.1, 255.255.255.255, 224.0.0.1, ff02::1, ::ffff:10.0.0.1, ::ffff:127.0.0.1, ::,
    0.0.0.0`; allowed: `8.8.8.8, 1.1.1.1, 172.32.0.1, 100.128.0.1, 2606:4700:4700::1111, ::ffff:8.8.8.8`.
  - allow_private: `10.0.0.1`, `127.0.0.1`, `::1`, `fd12::1` allowed; `169.254.169.254`, `fd00:ec2::254`,
    `224.0.0.1`, `0.0.0.0` still blocked.
  - `check_destination_url`: https public → returns addresses; http with allow_http False → `scheme`; ftp → `scheme`;
    no host (`https:///x`, `https://`) → `host`; malformed (`https://[::1`) → `host`; IP literal private → `address`
    without calling the resolver (resolver raises if called); `[::1]` literal; mixed answer `[8.8.8.8, 10.0.0.1]` →
    `address`; empty answer → `resolution`; resolver `OSError` → `resolution`; hanging resolver with
    `timeout_s=0.01` → `resolution`; uppercase scheme `HTTPS://` accepted; allow_private lets `http://localhost:9100`
    (fake resolver → 127.0.0.1) through.
  - `system_resolver("127.0.0.1") == ["127.0.0.1"]`.
- `packages/va_common/tests/test_settings.py` (or new `test_settings_webhooks.py`): `WebhookSafetySettings` default True
  in local/test, forced False in staging/production even when env sets true; env var `WEBHOOK_ALLOW_PRIVATE=false`
  honored in local.
- NEW `apps/api/tests/test_webhook_policy.py`: None → no-op; https public ok; http in local/test ok; http in
  production rejected unless `OrgSettings(allow_insecure_webhooks=True)`; staging http rejected; private target with
  `webhook_allow_private` True (test env) ok, production (forced False) rejected; each reason → exact error code,
  status 400, message, field `webhook`. Build settings with `api_helpers.make_settings(...)` (production needs
  `kms_provider="aws", aws_kms_key_id=...`, see AGENTS.md P02-T04b).
- Existing: `apps/webhook_dispatcher` settings tests, `apps/api/tests/test_oidc_discovery.py` stay green.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `make openapi` (no drift
expected), `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a` (≤ B), coverage of new modules 100 %,
`uvx pip-audit` on `uv export`, `corepack pnpm audit`. No call flow → no e2e.

### Risks
- Python `ipaddress` flag semantics differ across versions (3.12.4+ changed is_private/is_global for some ranges):
  the explicit multicast/reserved/unspecified checks plus the parametrized table guard behaviour; run on the repo
  interpreter.
- `getaddrinfo` for `127.0.0.1` works offline; never resolve real hostnames in tests.
- Moving `system_resolver` must not break OIDC tests that monkeypatch it — grep `system_resolver` in apps/api/tests.

## Build P05-T05a (Builder, 2026-11-01)
Implemented per plan: `va_common.net` (check_destination_url, blocked_address, parse_ip, system_resolver,
UnsafeDestinationError, METADATA_ADDRESSES), `WebhookSafetySettings` mixin (dispatcher + api), oidc_discovery now
imports resolver/parse_ip from va_common.net (keeps its stricter is_global rule; non-IP resolver answers are refused
instead of raising ValueError), `va_api.services.webhook_policy.check_call_webhook`. Deviation found in testing:
Python flags `::1` as `is_reserved` (inside `::/8`), so loopback is exempt from the always-blocked reserved rule
(recorded in the specs/19 §8 Decision). Resolver answers that are not IP strings → `resolution`.
Decisions: specs/19 §8 (main), specs/13 §9 (name fixed + cross-ref), specs/28 §6.4, specs/05 §3.7. AGENTS.md line.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0, spec refs 0, make test 8653 passed / 7 skipped, 98.65 %;
new modules 100 %; jscpd 0 clones; radon net.py avg A, nothing ≥ C; openapi no drift; pip-audit clean;
pnpm audit 3 moderate (unchanged). No call flow → no e2e.

## Review P05-T05a (Reviewer, 2026-11-01)
Approved. lint 0, typecheck 0, placeholders 0, spec refs 0, make test 8653 passed / 7 skipped, 98.65 %. net.py rules
match specs/19 §8 + specs/28 §6.4 (mapped v6, metadata always blocked, every DNS answer checked, timeout → resolution);
WEBHOOK_URL_REJECTED in catalog; dispatcher settings behaviour unchanged via mixin; OIDC keeps stricter is_global rule.

## Plan P05-T05b — Send Call dispatch service, `POST /v1/calls`, CallWorkflow start (Planner, 2026-11-01)
Context: P05-T05a approved (f1c5de6). This task wires T03c (inputs) + T03b (resolution) + T05a (webhook) + T04a/b
(`admit_call`) into one dispatch, inserts the `calls` row + `call.queued`/`call.scheduled` event row atomically,
starts `CallWorkflow` (defined in P05-T06; here only the start contract), and exposes `POST /v1/calls`. After it is
approved, tick P05-T05 too (a+b done).

### Decisions (write as `> Decision (2026-11-01): Send Call dispatch (P05-T05b).` under specs/05 §5, cross-refs: one
line under specs/01 §2.1 step 1, specs/19 §2.1 (new code), specs/05 §4.1 step 12)
1. Order inside one `tenant_session(ctx.org_id)`: steps 4-8 `load_call_inputs` → `resolve_inputs` →
   **webhook check** `check_call_webhook(config.webhook, ...)` (validation of the resolved config; runs before step 9 so a
   bad URL never consumes limits) → `load_billing_org` → `admit_call` (9-11) → insert `calls` row → event row. Org
   settings are read with `OrgSettingsReader.get(ctx.org_id)` BEFORE the session opens (reader opens its own session).
2. `call_id = va_common.ids.new_id()` (UUIDv7) is generated before admission (limits/hold reference it).
3. `calls` row: `id`, `org_id`, `direction='outbound'`, `to_number=config.phone_number`, `from_number=config.from_number`,
   `status` `queued` | `scheduled` (scheduled ⇔ `config.start_time` is not None), `queue_status` `queued` | `new`
   (Bland values, specs/19 §4), `runner_type`, `pathway_id/pathway_version/persona_id/persona_version/agent_id/
   agent_version_id/experiment_id/experiment_arm_id/voice_id/phone_number_id/memory_id/external_id/start_time` from the
   config, `test_call=request.test_call`, `request_data=config.request_data`, `metadata_=config.metadata`,
   `config = {**config.model_dump(mode="json"), "pricing": admitted.pricing.snapshot()}` (specs/24 §4 item 1),
   `credit_hold_id = admitted.hold.id if hold else None`, `workflow_id = call_workflow_id(call_id)`.
4. Event: `call.queued` (message "Call successfully queued.") or `call.scheduled` (message "Call successfully
   scheduled.", data `scheduled_for` ISO). Both carry `data.warnings = [{code, message, field?}]` from
   `CallResolution.warnings` (the `ResolutionWarning` docstring already says the queued event carries them) and
   `data.status`. The seq is assigned (Redis `INCR`) BEFORE commit and the `call_events` row is inserted in the SAME
   tenant transaction (`insert(CallEvent)` of `row_for(event)` via the session); the Redis XADD/PUBLISH broadcast runs
   AFTER commit, best effort: `EventPublishError` there is logged `calls.event_broadcast_failed` (event type, call_id)
   and not raised (row is durable; phase 06 outbox delivers webhooks from rows/transaction). A Redis failure while
   assigning the seq (before commit) → rollback, admission compensated, `SERVICE_UNAVAILABLE` "Call events are
   temporarily unavailable.".
5. va_events gains public split methods on `EventPublisher` (publish keeps its behaviour, now composed of them):
   `async def sequence(event) -> DomainEvent` (seq for call events, unchanged for others; the old `_next_seq`) and
   `async def broadcast(event) -> None` (the old `_broadcast`), plus `va_events.call_events.insert_rows(session, rows)`
   (same `ON CONFLICT (created_at, id) DO NOTHING` statement `DbCallEventWriter._insert` uses — refactor it to call
   `insert_rows`, no duplication). Update the specs/19 §1 Decision P03-T17 with an item 10 describing these.
6. Commit failure (any exception leaving the `tenant_session`): `admitted.compensate`-equivalent for Redis only
   (`ticket.rollback()` + `limiter.release_call`); the hold row disappears with the rollback. Implement as a
   `try/except BaseException` around the session block that calls a helper `_undo_counters(admitted, limiter)` and
   re-raises.
7. Workflow start after commit: `CallStarter.start(CallWorkflowInput)` →
   `client.start_workflow(CALL_WORKFLOW_NAME, input.model_dump(mode="json"), id=call_workflow_id(id),
   task_queue=settings.temporal_task_queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
   rpc_timeout=timedelta(seconds=START_RPC_TIMEOUT_S=5))`, the whole connect+start inside
   `asyncio.timeout(start_timeout_s)` (default 10 s, constructor arg). `WorkflowAlreadyStartedError` = success (a
   retried start). Any of `RPCError`, `TimeoutError`, `OSError`, `RuntimeError` (temporalio's failed connect) →
   `CallStartError` (cause chained).
8. On `CallStartError`: new `tenant_session`: `CallRepo(session, org_id, ...).transition(call_id, "failed",
   from_={"queued","scheduled"}, error_message="Call workflow could not be started.", end_reason="error",
   ended_by="error", ended_at=now, queue_status="call_error")` (if `CallTransitionError`, log
   `calls.start_failure_unrecorded` and continue), `holds.release(session, org_id, hold.id)` when a hold exists,
   event `call.error` level `error` data `{"code": "dispatch_failed", "detail": "workflow_start"}` sequenced + row in
   the same transaction; after commit counters rolled back (`ticket.rollback()` + `release_call`), broadcast best
   effort; raise `AppError("SERVICE_UNAVAILABLE", "The call could not be started. Please retry.")` (503; idempotency
   middleware releases 5xx claims, so a retry with the same key runs again). Add `dispatch_failed` to the specs/19 §2.1
   closed set (level `error`). The P05-T06 workflow must exit without dialing when the row is not `queued`/`scheduled`
   (an ambiguous start timeout may have started it) — write that sentence into the Decision.
9. `va_domain.call_workflow` (new module) = the start contract shared by api and worker: `CALL_WORKFLOW_NAME:
   Final = "CallWorkflow"`, `class CallWorkflowInput(BaseModel)` (frozen, extra forbid: `call_id: UUID`, `org_id: UUID`,
   `scheduled_for: datetime | None = None`), re-exports `call_workflow_id` from `va_domain.carrier`.
10. Response 200 (legacy body, specs/05 §5): `SendCallResponse(status: Literal["success"], message: str, call_id: UUID,
    batch_id: UUID | None = None, scheduled_for: datetime | None)`; `scheduled_for` is omitted when None (a
    `model_serializer(mode="wrap")` dropping it), `batch_id` always present (null). Scheduled `scheduled_for` =
    `config.start_time` in UTC (pydantic dumps `...Z`).
11. Route: `POST /v1/calls` on new `calls_router = legacy_router(prefix="/v1/calls", tags=["Calls"])` in
    `va_api/routers/calls.py` (schemas `routers/calls_schemas.py`), added to `main.ROUTERS`;
    `operation_id="calls_send"`, `dependencies=[Depends(require_scope("calls:write")), Depends(rate_limit("calls_send"))]`,
    body `SendCallRequest` (its examples feed OpenAPI), header `encrypted_key: Annotated[str | None,
    Header(alias="encrypted_key", convert_underscores=False)] = None`, `openapi_extra={"x-scope": "calls:write",
    **audit_exempt("call creation is recorded in calls and call_events")}`. No audit row.

### Files
- NEW `packages/va_domain/src/va_domain/call_workflow.py` (Decision 9) + export in `va_domain/__init__` only if that
  module re-exports others (check; otherwise import by module path).
- `packages/va_events/src/va_events/publisher.py`: public `sequence`, `broadcast`; `publish` composed of them.
- `packages/va_events/src/va_events/call_events.py`: `async def insert_rows(session: AsyncSession, rows:
  Sequence[CallEventRow]) -> None`; `DbCallEventWriter._insert` uses it. Export in `__all__` (+ package `__init__` if
  it re-exports).
- NEW `apps/api/src/va_api/services/call_starter.py`: `CallStartError(Exception)`; `class CallStarter(Protocol):
  async def start(self, call: CallWorkflowInput) -> None`; `class TemporalCallStarter` (`__init__(connector:
  TemporalConnector, task_queue: str, *, start_timeout_s: float = START_TIMEOUT_S)`); constants `START_TIMEOUT_S =
  10.0`, `START_RPC_TIMEOUT_S = 5.0`.
- NEW `apps/api/src/va_api/services/call_dispatch.py`:
  - `@dataclass(frozen=True, slots=True) class DispatchDeps`: `database: Database`, `limiter: CallLimiter`,
    `publisher: EventPublisher`, `starter: CallStarter`, `org_settings: OrgSettingsReader`, `settings: ApiSettings`,
    `clock: Clock`, `resolver: HostResolver`.
  - `@dataclass(frozen=True, slots=True) class DispatchedCall`: `call_id: UUID`, `scheduled_for: datetime | None`.
  - `async def dispatch_call(deps, ctx: RequestContext, request: SendCallRequest, *, encrypted_key: str | None) ->
    DispatchedCall` — orchestrates Decisions 1-8, split into small private helpers to stay ≤ radon B:
    `_admission(...)` (CallAdmission from config/inputs: `destination=config.phone_number`, `region=inputs.country`,
    `max_duration_min=math.ceil(config.max_duration_min)` (check `CallAdmission.max_duration_min: int`),
    `at=config.start_time or now`, `scheduled=start_time is not None`,
    `allowed_countries=settings.allowed_destination_countries()`), `_call_row(...) -> Call`, `_queued_event(...)`,
    `_persist_event(session, publisher, event) -> DomainEvent` (sequence + insert_rows), `_record_start_failure(...)`,
    `_undo_counters(admitted, limiter)`. Models: `PlatformModels(settings.llm_dialogue_model, settings.llm_router_model)`.
  - `deps_of(request: Request) -> DispatchDeps` building from the container.
- `apps/api/src/va_api/container.py`: new fields `call_limiter: CallLimiter` (`CallLimiter(redis, clock=clock)`),
  `event_publisher: EventPublisher` (`EventPublisher(redis, call_events=None)`; rows are written by the caller's
  transaction), `call_starter: CallStarter` (`TemporalCallStarter(temporal, settings.temporal_task_queue)`),
  `host_resolver: HostResolver = system_resolver`; docstring updated.
- NEW `apps/api/src/va_api/routers/calls.py`, `routers/calls_schemas.py` (Decisions 10-11); `main.ROUTERS` += calls_router.
- `apps/api/pyproject.toml`: add `va-events` dependency if not already present (va_billing/va_compliance are).
- `apps/api/tests/support/session_helpers.py` `SessionContainer`: add `call_limiter`, `event_publisher`,
  `call_starter: FakeCallStarter` (records inputs; `fail: Exception | None`), `host_resolver` (fake dict resolver;
  default resolves everything to `93.184.216.34`-style public IP — use a TEST-NET-free public literal such as
  `8.8.8.8`). Put `FakeCallStarter`/`fake_resolver` in NEW `apps/api/tests/support/call_helpers.py` with
  `send_call(client, key, body, **headers)`, `seed_callable_org(...)` (org with plan, funded via `fund`, a platform
  voice "Karen" or org voice via `call_input_rows.Seed`, an owned from number, API key with `calls:write` via
  `store_api_key(..., environment="test")`).
- `openapi/openapi.json` regenerated (`make openapi`); then dashboard + sdk `gen:api` (schema.d.ts drift tests).
- Cross-tenant suite: route has no path params, only the foreign-org-header probe applies (refused before the
  handler). If the suite needs a request, `REGISTRY.register_request("calls_send", json={...})`.
- `apps/api/tests/test_openapi.py` lists if it enumerates public paths/operations (check `PUBLIC_PATHS`-like sets).
- Specs: Decisions above; AGENTS.md learning line (dispatch module, publisher split, FakeCallStarter, partition note).

### Tests
- `packages/va_domain/tests/test_call_workflow.py`: input JSON round trip (`model_dump(mode="json")` →
  `model_validate`), extra field rejected, workflow id format, name constant.
- `packages/va_events/tests/test_publisher.py` (extend): `sequence` assigns gap-free seq and leaves non-call events
  untouched; `broadcast` XADDs + PUBLISHes the exact bytes; `publish` unchanged behaviour (existing tests stay green);
  `insert_rows` idempotent on a DB (reuse an existing va_events DB fixture; real time clock — partitions).
- NEW `apps/api/tests/test_call_starter.py` (fake client like `apps/telephony_edge/tests/test_signals.py`): start args
  (name, JSON input, id `call-<id>`, task queue, REJECT_DUPLICATE, rpc_timeout); `WorkflowAlreadyStartedError` →
  success; `RPCError` / connect `RuntimeError` / `OSError` → `CallStartError` with cause; hanging start with
  `start_timeout_s=0.01` → `CallStartError` (TimeoutError cause).
- NEW `apps/api/tests/test_calls_send.py` (module Postgres via `migrated_postgres()` + `SessionContainer`; endpoint
  through `session_app(container)` + `make_client`):
  - success immediate: 200 body exactly `{"status":"success","message":"Call successfully queued.","call_id":…,
    "batch_id":null}`; DB row (status queued, queue_status queued, config.pricing equals plan snapshot, workflow_id,
    credit_hold_id = held hold row, to/from numbers); one `call_events` row (category queue, seq 1, message);
    stream `events:calls` holds the `call.queued` payload; starter got `CallWorkflowInput(call_id, org_id, None)`;
    concurrency member present.
  - scheduled (`start_time` 1 day ahead of the container clock): message "Call successfully scheduled.",
    `scheduled_for` `…Z`; row `scheduled`/`new`; no hold, no limit keys; starter input `scheduled_for` set; event
    `call.scheduled`.
  - warnings: pathway runner with `task` also set → `call.queued` data.warnings contains `TASK_IGNORED`.
  - idempotency: same key + body → identical body + `Idempotent-Replayed: true`, ONE calls row, starter called once;
    same key different body → 422 `IDEMPOTENCY_KEY_REUSED`.
  - 4xx through the endpoint, one test each (exact code/status/field): missing `phone_number` → 400 `INVALID_INPUT`
    field `phone_number`; `RUNNER_CONFLICT`; unknown `pathway_id` → 404 family code; unknown `encrypted_key` header →
    400 `INVALID_ENCRYPTED_KEY`; invalid phone → `INVALID_PHONE_NUMBER`; webhook `http://10.0.0.1/x` with
    `webhook_allow_private=False` settings or a resolver answer `10.0.0.1` → `WEBHOOK_URL_REJECTED`; DNC row →
    `NUMBER_BLOCKED`; second call to the same number within 10 s → `SAME_NUMBER_TOO_SOON`; unfunded org →
    `INSUFFICIENT_CREDITS` (and assert counters rolled back: daily key back to 0); scope `calls:read` only → 403
    `FORBIDDEN`. After every failure: no `calls` row, no `call_events` row, starter not called.
  - Temporal failure: `call_starter.fail = CallStartError(...)` → 503 `SERVICE_UNAVAILABLE`; row `failed`,
    `queue_status` `call_error`, `end_reason` `error`, error_message set; hold `released`; counters rolled back
    (daily/hourly back, concurrency member removed); events seq 1 `call.queued` + seq 2 `call.error`
    (`data.code=dispatch_failed`); retry with the same `Idempotency-Key` runs again (claim released).
  - atomicity: seq Redis failure (monkeypatch `container.event_publisher.sequence` to raise `EventPublishError`) → 503
    "Call events are temporarily unavailable.", no row, no hold, counters rolled back; broadcast failure after commit
    (monkeypatch `broadcast`) → still 200, row + event row present, log `calls.event_broadcast_failed`.
  - sub-account acting (`X-VA-Org-Id` child) places the call in the child org (row org_id = child).
- `apps/api/tests/test_call_dispatch_unit.py` only if helpers need direct coverage (e.g. `_admission` ceil of
  fractional `max_duration_min`, `SendCallResponse` serializer drops `scheduled_for`).
- Existing gates: `test_operations_contract`, `test_audit_markers`, `test_cross_tenant_routes`, `test_openapi`,
  dashboard/sdk `schema.test.ts`.

### Gates
`make fmt lint typecheck placeholders test`, `make test-ts` (schema drift after gen:api), `uv run python
scripts/check_spec_refs.py`, `make openapi` (commit), `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a`
(≤ B), new modules 100 % line+branch, `uvx pip-audit` on `uv export`, `corepack pnpm audit`. No media/carrier flow
yet (the workflow arrives in T06) → `make e2e` not required; say so in evidence.

### Risks
- `call_events` partitions exist only around the migration month; the test container clock (`FrozenClock`, default
  2026-01-01) would make the event insert fail ("no partition of relation"). In the module fixture create the
  partitions for the frozen clock's month with `va_db.partitions.ensure_call_event_partitions(conn, clock.now())` on a
  login-role connection, or give the `SessionContainer` a `FrozenClock` at the real current time (preferred if the
  field allows it). Orgs created at real time are "in the future" for a 2026-01-01 clock → new-account rules (US/CA
  only, caps 100/10) apply; use a real-time clock or `NEW_ACCOUNT`-safe US numbers.
- `start_time` validation is relative to `now` (clock) — build scheduled times from `container.clock.now()`.
- Pool size 1 per role: never open the org-settings reader session or the failure-recording session while the
  dispatch session is open (sequential only).
- Header names with underscores: `convert_underscores=False` or FastAPI looks for `encrypted-key`.
- DNS: the webhook check uses `container.host_resolver`; tests must never hit real DNS.
- `admitted.compensate` releases the hold via the session — after a rollback the hold row does not exist, so only the
  Redis part is undone in Decision 6 (do not call `compensate` with a dead session).
- The idempotency middleware stores 4xx bodies: a 4xx replay must return the same error (assert one case).

## Build P05-T05b (Builder, 2026-11-01)
- va_events: public `EventPublisher.sequence`/`broadcast` (publish composed of them), `call_events.insert_rows`
  (DbCallEventWriter reuses it); specs/19 §1 Decision item 10; `dispatch_failed` added to §2.1 (+ Decision).
- va_domain.call_workflow (`CALL_WORKFLOW_NAME`, `CallWorkflowInput` with `workflow_id()`).
- va_db.call_states: new edge `scheduled → failed` (plan's from_={"queued","scheduled"} would otherwise raise
  ValueError); test DISPATCH_EDGES; specs/05 §8 Decision.
- api: services/call_starter.py (TemporalCallStarter, CallStartError), services/call_dispatch.py, routers/calls.py +
  calls_schemas.py (`scheduled_for` omitted via `Field(exclude_if=...)`: a wrap model_serializer erased the OpenAPI
  schema/examples), container fields, va-events dependency, openapi + both schema.d.ts regenerated.
- Deviation: tests use LIVE keys (plan said environment="test"; both work under VA_ENV=test). The start-failure
  path still commits the failed status when Redis refuses the `call.error` seq (event dropped, logged by caller).
- Tests: test_calls_send.py (21), test_call_starter.py (6), va_events publisher/DB additions, test_call_workflow.py.
- Coverage config: `[tool.coverage.run] concurrency = ["greenlet", "thread"]` — without it, lines after awaited
  SQLAlchemy async calls read as missed (call_dispatch showed 89% with phantom misses; 100% with it). Total 99.10%.
- Gates: make fmt/lint/typecheck/placeholders pass; make test 8696 passed, 7 skipped (99.10%); dashboard 52 + sdk 48
  vitest pass (schema drift ok after gen:api); jscpd 0 clones; spec refs 0; pip-audit no vulns (workspace pkgs
  unauditable as usual); pnpm audit 3 moderate (no high). e2e not run: no media/carrier flow until P05-T06.

## Review P05-T05b (Reviewer, 2026-11-01)
Gates re-run: make lint / typecheck / placeholders pass (0 findings). make test was still running when I rejected,
so this rejection rests only on the findings below. Spec/contract checks (route, scopes, x-scope, audit marker,
events, Decision text, call_states edge, publisher split, insert_rows reuse) are OK. Two correctness findings:

1. `apps/api/src/va_api/services/call_dispatch.py` `_record_start_failure` (the `except CallTransitionError` branch):
   when the transition to `failed` is refused because the row has already moved on, the code still releases the hold,
   gives the Redis counters and concurrency slot back (`finally: _undo_counters`), records `call.error dispatch_failed`,
   and `dispatch_call` returns 503. The Decision itself says a timed-out start may still have reached Temporal. In that
   case the workflow has moved the row to `dialing`/`ringing`/..., so the live call runs with no hold and no
   concurrency slot, the event log shows a false `dispatch_failed`, and the 503 releases the idempotency claim, so the
   client's retry with the same key places a SECOND real call. Required: if the row is no longer `queued`/`scheduled`,
   treat the workflow as started. Do not release the hold, undo the counters, or emit `call.error`. Log
   `calls.start_failure_unrecorded` (status) and return the normal success response (`QUEUED`/`SCHEDULED` body, the
   call exists and runs). Have `_record_start_failure` return a bool. Update the Decision P05-T05b item 5 sentence
   accordingly and rewrite `test_a_start_failure_on_a_call_that_moved_on_keeps_its_status`: it currently asserts the
   wrong behaviour (hold `released`, 503). Use a starter that moves the row to `dialing` and then raises. Assert 200,
   hold still `held`, concurrency member present, no `call.error` row. A `canceled` row (stopped meanwhile) may keep
   today's 503 only if you can justify it. Simplest is the same rule: not ours to fail, so no compensation by the
   dispatcher (the stop path owns the release).
2. Redis outage masks the real error. `_undo_counters` calls `limiter.release_call(...)`, a bare `zrem` that is not
   guarded (unlike `LimitTicket.rollback`, which logs and continues). It runs in `_create`'s `except BaseException`
   and in `_record_start_failure`'s `finally`. When Redis is down (exactly the case behind `calls.event_sequence_failed`
   → 503 "Call events are temporarily unavailable."), `zrem` raises `RedisError`/`OSError`, which replaces the
   `AppError`, so the client gets 500 INTERNAL instead of 503 (on the start-failure path: 500 instead of 503 after the
   row was already failed). Required: guard `release_call` in `_undo_counters` (catch `RedisError`/`OSError`, log
   `calls.counter_release_failed`; the slot expires by its score anyway). Add a test where the limiter's Redis fails
   during the undo (for example, monkeypatch `container.call_limiter.release_call` to raise `RedisError` together with
   a `sequence` failure) and assert 503 `SERVICE_UNAVAILABLE` plus the log event.

## Fix P05-T05b review (Builder, 2026-11-01)
1. `_record_start_failure` returns bool: a refused `failed` transition (row moved on: workflow started, or a stop
   canceled it) logs `calls.start_failure_unrecorded` and returns False with NO compensation (hold stays held,
   counters/slot kept, no `call.error`); `dispatch_call` then returns the normal success body. Counter undo now via
   `finally` guarded by `moved_on` (`# pragma: no branch`: the flag is set only right before the return).
   Test rewritten as parametrized `test_a_start_failure_on_a_call_that_moved_on_is_left_to_its_owner[dialing|canceled]`
   (200, hold held, daily=1, active=1, only call.queued). Spec §5 Decision item 5 extended.
2. `_undo_counters` guards `release_call` (RedisError/OSError → `calls.counter_release_failed`). Tests: seq failure +
   release failure → 503 "Call events are temporarily unavailable." + log; start failure + release OSError → 503,
   row failed + log.
Gates: fmt/lint/typecheck/placeholders pass; make test 8699 passed, 7 skipped, 99.10%, call_dispatch 100%;
jscpd 0 clones; radon avg A; spec refs 0; pip-audit clean; pnpm audit 3 moderate (no high). No TS change.

## Review P05-T05b round 2 (Reviewer, 2026-11-01)
Approved. Re-ran make lint/typecheck/placeholders (0 findings) and make test (8699 passed, 7 skipped, 99.10 %).
Both findings fixed: moved-on start failure returns success with no compensation (parametrized dialing|canceled
test asserts hold held, slot kept, no call.error); `_undo_counters` guards release_call (two tests, 503 kept).
Ticked P05-T05b and parent P05-T05.

## Plan P05-T06a — CallContext, stream tokens, va_workflows package, prefetch_call_context (Planner, 2026-11-01)
P05-T06 was too large for one Builder run (context + tokens + 3 telephony activities + the workflow + worker wiring),
so it is split in plan/phase-05.md into T06a (this), T06b (dial/hangup/end-state activities), T06c (CallWorkflow +
registration). Facts checked: no stream token code exists; no `packages/va_workflows`; `va_worker.task_queues.
REGISTRATIONS` is `()`; temporalio 1.34, `WorkflowEnvironment.start_time_skipping()` works here (test server cached
in /var/tmp). Dynamic data fetch is P08 (plan/phase-08.md), memory P13-T01, first-sentence pre-render P05-T10, so the
prefetch here builds the context from the row and leaves typed, empty slots for them.

### Decisions
1. under specs/01 §2.1 step 2: `> Decision (2026-11-01): call context (P05-T06a).` — `va_domain.call_context.
   CallContext` is the JSON in `call:{<id>}:ctx` (TTL `ceil(max_duration_min*60) + 3600`); built by
   `va_db.call_context.build_call_context` from the `calls` row (config = `calls.config` restricted to the
   `SendCallConfig` fields, so `pricing`/`postcall_steps` are dropped) — the same function the media worker uses to
   rebuild a missing context (specs/07 §3 step 3) and the edge uses for inbound (P05-T12). `variables` /
   `context_sections` are filled by pre-call dynamic data (phase 08), `memory` by P13-T01, the first-sentence
   pre-render by P05-T10; until then they are empty/None. The activity does not store a context for a terminal call.
2. under specs/28 §6.2 (and a one-line pointer under specs/06 §4.2): `> Decision (2026-11-01): stream token expiry
   (P05-T06a).` specs/06 says exp = dial + max_duration + 1 h, specs/28 says + 10 min. Chosen: specs/28's
   `exp = mint time + ceil(max_duration_min*60) + 600 s` (security spec, tighter; still covers ring time ≤ 120 s and a
   Telnyx WS reconnect at the end of the call). Token layout exactly as specs: raw bytes `call_id.bytes (16) ||
   exp (8, big-endian unsigned) || HMAC_SHA256(secret, call_id.bytes || exp_bytes)[:16]`, base64url without padding
   (40 bytes → 54 chars). Secret = `bytes.fromhex(STREAM_TOKEN_SECRET)`. Verification accepts the current or the
   previous secret, compares with `hmac.compare_digest`, rejects exp < now. Record DEC-007 in decisions.md.

### Files
- NEW `packages/va_common/src/va_common/stream_tokens.py`:
  - `STREAM_TOKEN_GRACE_S: Final = 600`
  - `class StreamTokenError(ValueError)` with `reason: Literal["malformed","signature","expired","call_mismatch"]`.
  - `@dataclass(frozen=True, slots=True) class StreamTokenClaims: call_id: uuid.UUID; expires_at: datetime`.
  - `def stream_token_expiry(now: datetime, max_duration_min: float) -> datetime` (now + ceil(min*60) + grace,
    truncated to whole seconds).
  - `def mint_stream_token(call_id: uuid.UUID, expires_at: datetime, secret: bytes) -> str`.
  - `def verify_stream_token(token: str, *, secrets: Sequence[bytes], now: datetime, call_id: uuid.UUID | None =
    None) -> StreamTokenClaims` (wrong length / bad base64 → malformed; no secret matches → signature; exp ≤ now →
    expired (signature checked FIRST so an attacker learns nothing); call_id given and different → call_mismatch).
  - `StreamTokenSecrets` helper: `def stream_token_secrets(settings: StreamTokenSettings) -> tuple[bytes, ...]`
    (current first, previous when non-empty).
- `packages/va_common/src/va_common/settings.py`: `class StreamTokenSettings(BaseServiceSettings)`:
  `stream_token_secret: str = ""`, `stream_token_secret_previous: str = ""`; validator: each non-empty value must be
  64 hex chars (`ValueError` naming the variable, never echoing the value). Services require a non-empty current
  secret where they mint/verify (T06b/T07 raise at container build, like `API_KEY_PEPPER`). Check
  `scripts/tests/test_env_example.py`/spec table need no change (variables already listed).
- NEW `packages/va_domain/src/va_domain/call_context.py`:
  - `CALL_CONTEXT_VERSION: Final = 1`, `CALL_CONTEXT_EXTRA_TTL_S: Final = 3600`.
  - `class MemoryFact(BaseModel)`: `key: str; value: JsonValue`. `class MemoryOpenItem`: `text: str; opened_at:
    AwareDatetime`. `class MemoryMessage`: `role: Literal["user","assistant"]; channel: Literal["voice","sms",
    "widget"]; text: str; at: AwareDatetime`. `class CallMemory`: `memory_id: UUID; summary: str | None = None;
    facts: list[MemoryFact] = []; open_items: list[MemoryOpenItem] = []; recent_messages: list[MemoryMessage] =
    Field(default_factory=list, max_length=10)` (specs/20 §2.3/§2.4 shape; frozen, extra forbid on all).
  - `class CallContext(BaseModel)` (frozen, extra forbid): `version: Literal[1] = 1`, `call_id`, `org_id`,
    `direction: Literal["outbound","inbound"]`, `to_number: str`, `from_number: str`, `batch_id: UUID | None`,
    `contact_id: UUID | None`, `test_call: bool` (check the Call column name; use whatever `calls` has),
    `config: SendCallConfig`, `variables: dict[str, JsonValue] = {}`, `context_sections: list[str] = []`,
    `memory: CallMemory | None = None`, `trace_context: dict[str, str] = {}`, `created_at: AwareDatetime`.
    Method `ttl_s() -> int` = `call_context_ttl_s(self.config.max_duration_min)`.
  - `def call_context_key(call_id) -> str` → `call:{<id>}:ctx` (same hash-tag form as `va_domain.carrier`).
  - `def call_context_ttl_s(max_duration_min: float) -> int` = `math.ceil(max_duration_min * 60) + 3600`.
- NEW `packages/va_db/src/va_db/call_context.py`:
  - `def config_of(call: Call) -> SendCallConfig` (validate `{k: v for k, v in call.config.items() if k in
    SendCallConfig.model_fields}`).
  - `def build_call_context(call: Call, *, now: datetime, trace_context: Mapping[str, str] | None = None) ->
    CallContext` (pure; `to_number`/`from_number` from the row, falling back to config `phone_number`/`from_number`
    when the row columns are NULL).
  - `class CallContextStore`: `__init__(self, redis: Redis)`; `async def save(self, ctx: CallContext) -> None`
    (`SET key json EX ctx.ttl_s()`, `ctx.model_dump_json()`); `async def load(self, call_id) -> CallContext | None`
    (missing → None; invalid JSON / schema → log `call_context.invalid` and return None so the caller rebuilds);
    `async def delete(self, call_id) -> None`.
- NEW package `packages/va_workflows` (dist `va-workflows`; follow AGENTS.md P00-T05 line: uv_build, `py.typed`,
  root dev group + `[tool.uv.sources]`, mypy `mypy_path`/`files`, pytest paths; deps `temporalio`, `va-common`,
  `va-db`, `va-domain`, `redis`, `opentelemetry-api`). `apps/worker/pyproject.toml` depends on it (no registration
  yet — T06c).
  - `src/va_workflows/__init__.py` (docstring only), `activities/__init__.py` (import-free).
  - `src/va_workflows/activities/inputs.py`: `class CallActivityInput(BaseModel)` frozen/extra forbid `call_id`,
    `org_id` (Temporal default converter: pydantic → pass `model_dump(mode="json")` dicts as T05b did, OR use
    `temporalio.contrib.pydantic.pydantic_data_converter` — DECIDE: use `pydantic_data_converter` for the worker
    client in T06c; for T06a activities take/return pydantic models and tests run them through `ActivityEnvironment`
    directly. Verify `temporalio.contrib.pydantic` exists in 1.34 — it does since 1.10). Record in the Decision.
  - `src/va_workflows/activities/call_context.py`:
    `PREFETCH_TIMEOUT: Final = timedelta(seconds=10)`, `PREFETCH_RETRY: Final = RetryPolicy(initial_interval=1 s,
    backoff_coefficient=2, maximum_interval=5 s, maximum_attempts=3, non_retryable_error_types=["CallNotFound"])`.
    `class PrefetchResult(BaseModel)`: `stored: bool; status: str; ttl_s: int | None`.
    `class CallContextActivities`: `__init__(self, database: Database, store: CallContextStore, clock: Clock)`;
    `@activity.defn(name="prefetch_call_context") async def prefetch_call_context(self, request: CallActivityInput)
    -> PrefetchResult`: `tenant_session(org_id)` → `TenantRepository(Call...)`/`CallRepo` get (absent → raise
    `ApplicationError("call not found", type="CallNotFound", non_retryable=True)`); terminal status
    (`va_db.call_states.TERMINAL_STATUSES`) → `PrefetchResult(stored=False, status=…, ttl_s=None)`, log
    `call_context.skipped`; else close the session, inject the current OTel context into a dict
    (`opentelemetry.propagate.inject(carrier)`), `build_call_context`, `store.save`, log `call_context.stored` with
    `call_id`/`org_id`, return stored result. Bind `bind_call_context` logging fields if convenient.
- Specs: Decisions 1-2; AGENTS.md learning line (new package, CallContext location, token module).

### Tests
- `packages/va_common/tests/test_stream_tokens.py`: round trip (claims equal; token is 54 url-safe chars, no `=`);
  known vector (fixed call id + exp + secret bytes generated at test time is fine, but pin layout by decoding and
  checking the 16/8/16 split and recomputing the HMAC independently); every byte flipped → `signature` (parametrize
  positions 0..39 — sizes, not random values); truncated/extra bytes/bad base64 → `malformed`; expired (now = exp and
  now > exp) → `expired`; expired + bad signature → `signature`; previous secret accepted, unknown secret rejected;
  `call_id=` mismatch → `call_mismatch`; `stream_token_expiry` math for fractional minutes (0.5 → +630 s);
  settings: non-hex / wrong length rejected with the variable name and without the value in the message; empty ok;
  `stream_token_secrets` order.
- `packages/va_domain/tests/test_call_context.py`: key format; ttl (30 min → 5400; 0.5 → 3630); JSON round trip;
  extra key rejected; `recent_messages` > 10 rejected; version literal.
- `packages/va_db/tests/test_call_context.py` (unit): build from a `Call` instance constructed in memory with a config
  from every `apps/api/tests/fixtures/bland_send_call_resolved/*.json` snapshot (parametrize on file names; the
  snapshot JSON may need the `pricing` key added to prove it is dropped); NULL row numbers fall back to config;
  `trace_context` copied. Store tests on fakeredis: save sets TTL = ttl_s; load round trip; missing → None; garbage
  and schema-invalid JSON → None + log event; delete.
- `packages/va_workflows/tests/test_prefetch_activity.py` (module Postgres via `migration_helpers.migrated_postgres()`
  + `call_rows.seed_call` from va_db test support; add `packages/va_db/tests/support` to the new package's pytest
  `pythonpath` only if not already global): run through `temporalio.testing.ActivityEnvironment().run(acts.
  prefetch_call_context, CallActivityInput(...))`: stored context equals `build_call_context` of the row, TTL right,
  `trace_context` contains `traceparent` when run inside a span of an in-memory tracer provider (use
  `obs_helpers.memory_provider()` and `otel_traces_sampler_arg=1.0`; if global propagation needs a global provider,
  use `opentelemetry.trace.use_span`/context with a non-recording `SpanContext` you build — keep it deterministic);
  terminal call (`completed`) → `stored=False`, nothing in Redis; unknown call id → `ApplicationError` non-retryable
  with type `CallNotFound`; other org's call id → also `CallNotFound` (RLS); seed_call must give a valid config
  (reuse a resolved snapshot JSON as `config`).
- Coverage: new modules 100 % line + branch.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a` (≤ B), `uvx pip-audit` on `uv export`, `corepack pnpm audit`. No route change (no
openapi), no call flow yet → `make e2e` not required (state it in evidence).

### Risks
- `uv.lock` changes when adding the package (`uv lock`); commit it. Docker image `.dockerignore` already includes
  `packages/`.
- Root mypy `files`/`mypy_path` and pytest `testpaths`/`pythonpath` must list the new package (AGENTS.md P00-T05).
- `call_rows.seed_call` may build a minimal config that is not a valid `SendCallConfig`: pass an explicit config
  (snapshot JSON) for prefetch tests.
- Do not store contexts with secrets: `config` contains no keys (BYO key is only referenced by `telnyx_account_id`);
  `request_data` is customer data and already in `calls.config` — acceptable (same trust zone).
- `ActivityEnvironment` runs the activity in the test loop; `activity.info()` is available there.

## Build P05-T06a (Builder, 2026-11-01)
Implemented per plan, with these deviations/notes:
- `StreamTokenSettings` secrets are `SecretStr` (not `str`) and the class sets `hide_input_in_errors=True`: pydantic
  otherwise prints `input_value=<secret>` in ValidationErrors (both field and model validators). Message names the
  variable (`STREAM_TOKEN_SECRET must be 64 hex characters (32 bytes) when set`).
- `CallContext.direction` also allows `web` (`calls.direction` CHECK has it). `MemoryOpenItem` carries `id`
  (open items are closed by id, specs/20 §2.5).
- `PrefetchResult.status` is the call's status (stored or terminal). Log events bound with call_id/org_id.
- Found: dispatch stores `calls.config` via `model_dump(mode="json")` without `by_alias`, so `voicemail.sms` is stored
  as `from_` (snapshots use `from`). Both validate (populate_by_name). The context store writes `by_alias=True`.
  Possibly worth aligning dispatch later (not in scope).
- Test support: `packages/va_domain/tests/support/resolved_configs.py` (`resolved_config(name)`, `RESOLVED_NAMES`);
  `call_rows.seed_call` now lets `values` override defaults (`config=` etc.).
- Specs: Decisions under specs/01 §2.1 step 2, specs/28 §6.2, pointer under specs/06 §4.2. DEC-007 already present.
Gates: make fmt ok; make lint pass; make typecheck pass; make placeholders 0 findings; make test 8811 passed,
7 skipped, 99.11 % (new modules 100 % line+branch); jscpd 0 clones; radon avg A (2.04); spec refs 0; pip-audit clean;
pnpm audit 3 moderate (no high). No TS/route change; no call flow yet, so make e2e not run.

## Review P05-T06a (Reviewer, 2026-11-01)
Approved. Re-ran make lint/typecheck/placeholders (0 findings), check_spec_refs (0), make test (8811 passed,
7 skipped, 99.11 %; stream_tokens, va_db/va_domain call_context, va_workflows activities all 100 %). Token layout,
check order (signature before expiry), rotation, settings redaction match specs/28 §6.2 Decision; context builder
drops non-config keys, stores by_alias; prefetch skips terminal calls, CallNotFound non-retryable incl. RLS case.
Ticked P05-T06a. Next: P05-T06b.

## Plan P05-T06b — Telephony call activities: dial, hangup, end-state recording (Planner, 2026-11-01)
Facts checked: dispatch (P05-T05b) already took limits + concurrency slot + hold for `queued` calls; `scheduled`
calls took nothing (specs/24 §6 Decision P05-T04b). `calls.from_number` is already selected at Send Call (read-only,
specs/16 §6.2 Decision P05-T03c) → §6 step 4 "select from / mark pool usage" = use `calls.from_number`; pool usage
marking arrives with P10 (say so in the Decision). Edge maps `call.initiated` only `dialing → ringing`, and treats an
answered hangup as "unchanged" when `calls.end_reason` is already set (specs/06 §5 Decision item 7). `transfer_started_at`
lives in `call:{<id>}:carrier_state`. Durations helper `_seconds` is private to `va_edge.mapping`. `DialResult` has
`call_control_id`, `call_leg_id`, `call_session_id`. BYO connection id = `telnyx_accounts.call_control_app_id`.
`WorkerSettings` has no telephony fields yet (T06c wires the container); here activities take an injected
`DialSettings` dataclass.

### Decisions (one block `> Decision (2026-11-01): dial and end-state activities (P05-T06b).` under specs/06 §6,
### plus a one-line `dial_failed` addition under specs/19 §2.1 and a pointer line under specs/05 §8)
1. Status guard / idempotency of `dial` (activity retries reuse `command_id=dial-<call_id>`):
   terminal → `skipped`; `scheduled` → dispatch admission (item 2) then `scheduled → queued` with `credit_hold_id`
   (commit); `queued` → `queued → dialing` with `dial_started_at=now` **committed before** the Telnyx request (so a
   `call.initiated` webhook racing the response finds `dialing`, never `queued`); `dialing` without a primary leg →
   send the dial again (same command_id, a retry after a crash/timeout); `dialing` with a primary leg, or `ringing` /
   `in_progress` / `transferring` / `transferred` → `already_dialed` (returns the leg's call_control_id). After a
   successful dial: `CallLegRepo.record_initiated(role='primary', …, telnyx_account_id)` (upsert tolerates the edge
   inserting first) + `call.dialing` event (data `{call_control_id}`) in one tenant session.
2. Scheduled admission: `va_billing.dispatch.admit_call` with `CallAdmission(destination=calls.to_number,
   region=phonenumbers.region_code_for_number(parse(to_number)), max_duration_min=ceil(config.max_duration_min),
   at=now, scheduled=False, allowed_countries=DialSettings.allowed_countries)` → `credit_hold_id`. `AppError` codes:
   `CONCURRENCY_CAP_REACHED` → outcome `requeue` (row stays `scheduled`, nothing held) unless `DialInput.final_attempt`
   (the workflow's 30-min requeue budget is spent; T06c decides) → blocked; `SERVICE_UNAVAILABLE` → raise retryable
   `ApplicationError(type="DispatchUnavailable")`; every other code → blocked: `scheduled → failed`,
   `end_reason='dispatch_blocked'`, `ended_by='system'`, `error_message` = the AppError message, `queue_status=
   'call_error'`, `ended_at=now`; events `call.dispatch_blocked` (level `warning`, message = AppError message, data
   `{reason: <code>, message}` — `reason` is the specs/31 code) then `call.ended` (data `{end_reason, ended_by}`).
   Admission success but a later failure before commit → `Admitted.compensate`. Only `scheduled` rows are re-checked
   (batch items arrive in phase 17 and will be `scheduled`-like; note it).
3. `DialRequest` per specs/06 §6 step 5: `connection_id` = platform `DialSettings.call_control_app_id`, or the BYO
   account's `call_control_app_id` (read in the tenant session; missing/`None` or `UnknownTelnyxAccount` → carrier
   error path, message "Telnyx account is not available for dialing."); `to`/`from_` from the row; `timeout_secs` =
   org `dial_timeout_s` (`load_settings(org.settings)`); `time_limit_secs = min(14400, ceil(max_min*60)+30)`;
   AMD `premium` with the exact §6 config unless `ivr_mode` or `not answered_by_enabled` or (`voicemail.action ==
   "ignore"` and `not voicemail.sensitive`) → `disabled` and no config; record fields when `config.record`;
   `custom_headers=[X-VA-Call-Id]`; `client_state=encode(ClientState(call_id, "primary", org_id))`;
   `webhook_url=None` (the app's URL, platform and BYO); stream `StreamParams(stream_url=…, stream_codec /
   bidirectional codec "L16" + rate 16000 when hd_audio else "PCMU"/8000)`. Stream URL:
   `f"{scheme}://{MEDIA_PUBLIC_HOST}/ws/telnyx?call_id={id}&token={token}"`, token = `mint_stream_token(call_id,
   stream_token_expiry(now, config.max_duration_min), secret)`; scheme `wss`, except `ws` when `VA_ENV` is
   `local`/`test` (the dev media worker has no TLS; same rule as the OIDC `allow_insecure` decision).
4. Telnyx failures in `dial`: `TelnyxApiError` (incl. 4xx/5xx after the client's own retries) → carrier-error path:
   `dialing → failed` (`from_={"dialing"}`), `end_reason='carrier_error'`, `ended_by='error'`, `error_message =
   "Carrier rejected the dial: <first error title or detail>"` (truncate 500), `queue_status='call_error'`,
   `ended_at=now`; hold released (`holds.release`), slot released (`CallLimiter.release_call`, Redis errors logged
   `calls.counter_release_failed`), events `call.error` (level `error`, data `{code:"dial_failed", detail:<telnyx
   code or "unknown">}`) and `call.ended`; outcome `failed`. A `CallTransitionError` there (stopped meanwhile) →
   log `calls.dial_failure_unrecorded`, outcome `skipped`. `TelnyxRateLimited` / `TelnyxTransportError` → raise
   retryable `ApplicationError(type="TelnyxUnavailable")` (the retry resends the same command_id). Add `dial_failed`
   to the specs/19 §2.1 closed set (level `error`).
5. `end_call(EndCallInput)` records a workflow-decided end. Allowed reasons (Literal): `canceled`, `dispatch_blocked`,
   `carrier_error`, `error`, `worker_crash`, `fax_detected`, `max_duration`. Current status:
   - terminal → no change (`changed=False`).
   - `scheduled`/`queued`/`dialing`/`ringing` → `canceled` (reason canceled; `disposition_tag='CANCELED'`) or
     `failed` (`queue_status='call_error'`), `end_reason`, `ended_by=END_REASONS[reason]`, `ended_at=now`,
     `error_message` from input. Hold released only when the call never reached Telnyx (was `scheduled`/`queued`, or
     `dialing` without a primary leg); otherwise PostCallWorkflow settles it (attempt minimum, P05-T18). Slot always
     released (idempotent ZREM).
   - `in_progress`/`transferring`/`transferred` with an answered-phase reason (`error`, `worker_crash`,
     `fax_detected`, `max_duration`): `→ completed` with the input's reason unless `calls.end_reason` is already set
     (kept: the worker decided before dying), `ended_at=now`, `connected_seconds`/`transfer_seconds` via the shared
     helper (item 6) with `transfer_started_at` from carrier state. Hold untouched (PostCall captures). Slot released.
     The workflow calls `end_call` BEFORE `hangup_call`, so the carrier's hangup webhook finds `end_reason` set and
     leaves the call alone. A pre-answer-only reason (`canceled`, `dispatch_blocked`, `carrier_error`) on an answered
     call → non-retryable `ApplicationError(type="InvalidEndReason")`.
   - Events of an applied change: `call.dispatch_blocked` (reason dispatch_blocked; data `{reason: input.code or
     "dispatch_blocked", message}`) then `call.ended {end_reason, ended_by}`. No `call.error` (the edge already
     emitted `stream_failed`; worker_crash has no §2.1 code).
   - A `CallTransitionError` from a concurrent edge/worker update → `changed=False` with the new status.
6. Shared durations: move the rule to `va_db.call_states.call_durations(answered_at, ended_at, transfer_started_at)
   -> dict[str, Decimal]` (`connected_seconds` = answered → min(end, transfer start), `transfer_seconds` when a
   transfer started; 3 dp like the edge's `_CONNECTED_PRECISION`); `va_edge.mapping.hangup_outcome` uses it (no
   behaviour change; edge tests stay green).
7. `hangup_call(HangupInput{call_id, org_id, reason})`: primary leg's `call_control_id` (tenant session; none →
   `sent=False`, `reason="no_leg"`); `client.hangup(ccid, command_id=f"hangup-{call_id}", client_state=…)`;
   `False` from the client (already ended) → `sent=False`, reason `already_ended`. Rate-limited/transport →
   retryable `TelnyxUnavailable`; other `TelnyxApiError` → non-retryable `ApplicationError(type="HangupRejected")`.
   Log `calls.hangup_sent` with the reason (`fax_detected`, `stream_failed`, `worker_crash`, `max_duration`, ...).
8. Retry policies (constants): `DIAL_TIMEOUT=30 s`, `DIAL_RETRY(1 s, ×2, max 10 s, 5 attempts, non-retryable
   [CallNotFound, InvalidEndReason])`; `HANGUP_TIMEOUT=15 s`, `HANGUP_RETRY(1 s, ×2, max 10 s, 6 attempts,
   non-retryable [HangupRejected])`; `END_CALL_TIMEOUT=15 s`, `END_CALL_RETRY(1 s, ×2, max 10 s, 6 attempts,
   non-retryable [CallNotFound, InvalidEndReason])`.
9. Event persistence helper shared by the activities: sequence → `insert_rows` in the open session → commit →
   `broadcast` (best effort, `calls.event_broadcast_failed`); a sequence failure raises retryable
   `ApplicationError(type="EventsUnavailable")` and rolls the transaction back.

### Files
- `packages/va_db/src/va_db/call_states.py`: `call_durations` (item 6); export it. Edit
  `apps/telephony_edge/src/va_edge/mapping.py` to use it (drop `_seconds`/`_CONNECTED_PRECISION` if unused).
- NEW `packages/va_workflows/src/va_workflows/activities/telephony_inputs.py` (import-free like `inputs.py`;
  workflows import it): `DialInput(call_id, org_id, final_attempt: bool = False)`; `DialOutcome(result:
  Literal["dialing","already_dialed","skipped","requeue","blocked","failed"], status: str, call_control_id: str |
  None = None, code: str | None = None)`; `HangupInput(call_id, org_id, reason: str)`; `HangupResult(sent: bool,
  reason: Literal["sent","no_leg","already_ended"])`; `WorkflowEndReason` Literal (item 5); `EndCallInput(call_id,
  org_id, end_reason: WorkflowEndReason, error_message: str | None = None, code: str | None = None)`;
  `EndCallResult(changed: bool, status: str)`; retry/timeout constants (item 8) — `RetryPolicy`/`timedelta` are
  workflow-safe imports. All frozen, extra forbid.
- NEW `packages/va_workflows/src/va_workflows/activities/events.py`: `persist_events(session, publisher, events) ->
  list[DomainEvent]`, `broadcast_events(publisher, events)` (item 9).
- NEW `packages/va_workflows/src/va_workflows/activities/dial_request.py` (pure): `@dataclass(frozen=True)
  DialSettings(call_control_app_id: str, media_public_host: str, media_ws_scheme: Literal["ws","wss"],
  stream_secret: bytes, allowed_countries: frozenset[str])` + `classmethod from_env(va_env, …)` helper choosing the
  scheme; `PREMIUM_AMD_CONFIG: Final[AmdConfig]`; `amd_mode(config) -> AmdMode`; `stream_params(config, url)`;
  `time_limit_secs(max_min)`; `stream_url(settings, call_id, token)`; `build_dial_request(call: Call, config:
  SendCallConfig, *, connection_id: str, timeout_secs: int, stream_token: str, settings: DialSettings) ->
  DialRequest`.
- NEW `packages/va_workflows/src/va_workflows/activities/telephony.py`: `class TelephonyActivities.__init__(self,
  database: Database, telnyx: TelnyxClientFactory, limiter: CallLimiter, publisher: EventPublisher, redis: Redis,
  settings: DialSettings, clock: Clock)`; `@activity.defn(name="dial") async def dial(self, request: DialInput) ->
  DialOutcome`; `@activity.defn(name="hangup_call") async def hangup_call(self, request: HangupInput) ->
  HangupResult`; private helpers `_guard`, `_admit_scheduled`, `_block`, `_mark_dialing`, `_send_dial`,
  `_record_dialed`, `_carrier_failure`, `_release_slot`. Keep each function ≤ radon B (split per item).
- NEW `packages/va_workflows/src/va_workflows/activities/call_end.py`: `class CallEndActivities.__init__(self,
  database, limiter, publisher, redis, clock)`; `@activity.defn(name="end_call") async def end_call(self, request:
  EndCallInput) -> EndCallResult` (item 5); reads `transfer_started_at` with `HGET carrier_state_key(call_id)`
  (Redis error → raise retryable). Reuse `_call_not_found` from `call_context.py` (move it to a small
  `activities/errors.py` with the type constants `CALL_NOT_FOUND`, `TELNYX_UNAVAILABLE`, `EVENTS_UNAVAILABLE`,
  `DISPATCH_UNAVAILABLE`, `INVALID_END_REASON`, `HANGUP_REJECTED`; keep `call_context.CALL_NOT_FOUND` re-export).
- `packages/va_workflows/pyproject.toml`: add deps `va-telnyx`, `va-billing`, `va-compliance`, `va-events`,
  `phonenumbers` (+ workspace sources); `uv lock`. If va_workflows tests need va_telnyx / va_sim / va_billing test
  support dirs, they are already on the root pytest `pythonpath` (check; add if not).
- Specs: Decisions (above); AGENTS.md learning line (activity classes, DialSettings, end_call-before-hangup rule,
  call_durations location).

### Tests (packages/va_workflows/tests; DB via the existing `database` fixture, Redis via fakeredis[lua] shared
### between limiter/publisher/carrier state; FakeTelnyx via `telnyx_client_helpers.sim_http(state)` + a
### `TelnyxClientFactory` over that http client; provision connection + number with `provisioned(...)`; seed rows
### with `call_rows.seed_call(..., config=resolved_config(name), status=…)` and `billing_rows`/`fund` style helpers
### — reuse va_billing tests/support `billing_rows.new_billing_org` if importable, else seed org/plan via RowFactory)
- `test_dial_request.py` (pure): full body `== {...}` for a default config (every field incl. `stream_*`,
  `stream_bidirectional_target_legs`, AMD premium + exact config dict, `time_limit_secs`, `timeout_secs`,
  `custom_headers`, `client_state` decodes to primary leg, `send_silence_when_idle`); hd_audio → L16/16000;
  record → the four record fields; AMD disabled for ivr_mode / answered_by_enabled false / ignore+not sensitive, but
  premium for ignore+sensitive; time limit cap (240 min → 14400; 0.5 → 60); stream URL scheme ws/wss and the token
  verifies with `verify_stream_token(call_id=)` and expires at `now + ceil(min*60) + 600`.
- `test_dial_activity.py` (ActivityEnvironment, FakeTelnyx): queued → outcome `dialing`, row `dialing` +
  `dial_started_at`, primary leg with the sim's ids, `call.dialing` row in `call_events` + stream entry, FakeTelnyx
  received exactly one dial whose body equals `build_dial_request(...).body()` (field-by-field assert on AMD,
  record, hd_audio, time limit via the sim's call record); BYO account → that account's connection id and client;
  retry after a dial already recorded → `already_dialed`, no second Telnyx dial; `dialing` without leg → dial resent
  with the same `command_id`; terminal → `skipped`, nothing sent; scheduled + admission ok → `queued` then
  `dialing`, `credit_hold_id` set, hold `held`, concurrency slot present; scheduled + concurrency cap → `requeue`,
  row `scheduled`, no hold, daily counter rolled back; same with `final_attempt=True` → `blocked`; scheduled + DNC
  → `blocked`, row failed/dispatch_blocked/error_message, events dispatch_blocked + ended; insufficient credits →
  blocked with `INSUFFICIENT_CREDITS` reason; Redis down during admission → `ApplicationError` type
  `DispatchUnavailable` retryable; Telnyx 422 (FakeTelnyx `fail`/scenario or respx) → `failed`, row
  failed/carrier_error, hold released, slot gone, events error(dial_failed)+ended; Telnyx transport error / 429 →
  `TelnyxUnavailable` retryable and row stays `dialing`; carrier error after a concurrent cancel → `skipped`;
  unknown call → `CallNotFound` non-retryable; event sequence failure → `EventsUnavailable` and row unchanged.
- `test_hangup_activity.py`: answered sim call → `sent=True`, sim call ends, command_id `hangup-<id>`; repeated →
  `already_ended`; no leg → `no_leg`; 429/transport → `TelnyxUnavailable`; other API error → `HangupRejected`.
- `test_end_call_activity.py`: parametrize pre-answer statuses × reasons (canceled → `canceled` + CANCELED
  disposition; others → `failed` + call_error) with hold released iff never dialed; answered → `completed` with
  durations (incl. transfer_started_at from carrier state), hold still `held`, slot released, `call.ended` data;
  existing `end_reason` kept; dispatch_blocked emits both events in order; terminal → `changed=False`;
  pre-answer-only reason on answered call → `InvalidEndReason`; concurrent transition → `changed=False`.
- `packages/va_db/tests/test_call_states.py` (or a new test file): `call_durations` cases (no transfer, transfer,
  end before answer clamps to 0, precision).
- Coverage: new modules 100 % line + branch.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a`, `uvx pip-audit` on `uv export`, `corepack pnpm audit`. No route change. `make e2e`:
no runnable call flow until T06c/P05-T17 — state that in the evidence.

### Risks
- FakeTelnyx dial needs a known connection id and an owned `from` number for the sim account: use the va_telnyx
  test helper `provisioned(client, voice_url, sms_url)` + number order flow (see va_telnyx contract tests).
- `call_events` partitions exist only around the migration month: activities use a real-time clock in DB tests
  (`SystemClock`), not `FrozenClock(2026-01-01)`.
- Scheduled admission reads compliance calling windows at `now`: seed orgs/settings so the test runs at any wall
  time (disable calling hours / state quiet hours in org settings) or inject a clock inside business hours that is
  still within the partitions (`call_helpers.business_hours_today()` pattern).
- Session pool size 1 in tests: never open a second session while one is open (Redis reads before the session).
- Do not import `va_edge` from va_workflows (shared code goes to va_db/va_domain).

## Build P05-T06b (Builder, 2026-11-01)
Implemented per plan, with these deviations (all recorded in the specs/06 §6 Decision P05-T06b):
- `command_id`s: dial = `dial-{call_id}` (spec §6 step 6 verbatim); hangup = `command_id("hangup", call_id, "primary")`
  (`hangup-{id}-primary`, the specs/06 §2 example), not `hangup-{id}`.
- `hangup_call`: Telnyx 429 / 5xx after the client retries are retryable `TelnyxUnavailable` (not `HangupRejected`);
  other API rejections and an unusable BYO account are `HangupRejected`.
- `DialSettings.allowed_countries` is required (an empty default blocked US: INTERNATIONAL_NOT_ENABLED); T06c passes
  `parse_allowed_countries(INTERNATIONAL_ALLOWED_COUNTRIES)`. Constructor `DialSettings.for_env(va_env, ...)`.
- Activity classes take keyword-only collaborators. `TelephonyActivities` has no `redis` param (not needed).
- New `CallLegRepo.primary(call_id)` (va_db, tested); shared `activities/lookup.load_call` (prefetch uses it too),
  `activities/events` (`persist_events`, `broadcast_events`, `call_event`, `ended_event`), `activities/errors`
  (type names + `call_not_found`/`retryable`; `call_context.CALL_NOT_FOUND` still exported).
- Event sequence failure on `_record_dialed`: the call stays `dialing` (committed before the request), the leg and
  event roll back, the retry resends the same command (deduplicated) — tested.
- Scheduled admission success then a lost transition race → counters given back, outcome `skipped` (tested).
- `call_durations` in `va_db.call_states` (+ `DURATION_PRECISION`); edge `hangup_outcome` uses it.
Gates (this iteration): make fmt 0, make lint 0, make typecheck 0 (mypy 1036 files), placeholders 0 findings,
check_spec_refs 0, make test 8913 passed / 7 skipped, coverage 99.12% (va_workflows activities 100% line+branch),
jscpd 0 clones, radon avg A (2.89; all new functions A/B), pip-audit (uv export --no-emit-workspace) no known
vulnerabilities, pnpm audit 3 moderate (no high, pre-existing). make e2e: exit 4 "collected 0 items" — `tests_e2e/`
does not exist yet (flows arrive with T06c/P05-T17); no TS change.

## Review P05-T06b (Reviewer, 2026-11-01)
Gates re-run: make lint 0, make typecheck 0, make placeholders 0, check_spec_refs 0, make test (see below).
Findings:
1. `packages/va_workflows/src/va_workflows/activities/call_end.py` `end_call`: the concurrency slot is released only
   when this activity applies the end. If the call is already terminal (the `is_terminal` early return), or a
   concurrent update made it terminal (the `CallTransitionError` branch), the slot stays taken. That contradicts the
   module docstring ("The concurrency slot is always released"), the specs/06 §6 Decision P05-T06b item 5 ("The slot
   is always released") and specs/24 §6 ("Released by ZREM when the call ends (pipeline end, no-answer, failure)").
   The edge (`va_edge.carrier`) never calls `release_call`. So every carrier-ended call (no-answer, busy, normal
   hangup) keeps its slot until the score expires (max_duration + 5 min) unless `end_call` releases it, and
   CallWorkflow (T06c) has no idempotent cleanup activity to call. Fix: whenever the call's status after the activity
   is terminal (the early return and a `CallTransitionError` whose `.current` is terminal), call `release_slot` too
   (ZREM is idempotent). Keep the slot when the race left the call live (e.g. `ringing → in_progress`). Update
   `test_a_terminal_call_is_left_alone` (line 228 currently asserts the slot is still taken) and
   `test_a_concurrent_status_change_wins` (line 219: raced to `no_answer`, which is terminal → slot must be gone).
   Add a race-to-non-terminal case that keeps the slot.

## Fix P05-T06b review (Builder, 2026-11-01)
1. `end_call` now releases the concurrency slot whenever the call is terminal after the activity: already terminal
   (early path) or a `CallTransitionError` whose `.current` is terminal (`CallEndActivities._unchanged`). A race to a
   live status (e.g. `in_progress`) keeps the slot. Tests: `test_a_terminal_call_is_left_alone_but_frees_its_slot`
   (every terminal status, slot gone, row unchanged, no events), `test_a_concurrent_status_change_wins[terminal|live]`
   (slot gone / kept). Module docstring and specs/06 §6 Decision P05-T06b item 5 reworded (end_call = CallWorkflow's
   idempotent cleanup).
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0, check_spec_refs 0, make test 8914 passed / 7 skipped,
coverage 99.12% (call_end.py 100%), jscpd 0 clones, radon end_call B(6), others A. make e2e: tests_e2e/ not present
yet (flows arrive with T06c/P05-T17).

## Review P05-T06b round 2 (Reviewer, 2026-11-01)
Finding 1 fixed: `_unchanged` releases the slot for terminal statuses; tests cover terminal/live races. Gates re-run:
lint 0, typecheck 0, placeholders 0, make test 8914 passed / 7 skipped, coverage 99.12%. Approved.
