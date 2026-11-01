
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
