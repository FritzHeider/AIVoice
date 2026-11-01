
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
