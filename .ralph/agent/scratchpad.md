
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
