# Phase 06 scratchpad

## Plan P06-T02 — Webhook router and payload formats

Spec: specs/19 §1 step 5, §2 (last column = webhook key), §3, §4 (Call Object already built by post-call
`emit_completed` into `call.completed` event.data), §5, §6, §7; specs/23 §7 (HIPAA phi_allowed); specs/03 §9 DDL.
Existing: `va_events.publisher.WebhookRouter` Protocol (`route(event, *, session)`), publisher.publish calls it;
models `WebhookEndpoint`/`WebhookDelivery` (va_db.models.billing; format CHECK only 'bland','envelope');
`Call.config["webhook"]`/`["webhook_events"]` (resolved SendCallConfig), `Call.metadata_`, `Batch.status_webhook`
+ counts, `Conversation.webhook`, `Widget.webhook_url`; `va_domain.org.load_settings` (`hipaa_mode`).
Emitter wiring (persist_events path, media buffer batching) is P06-T07 — NOT this task. Dispatcher is T03.

### Decisions (add as `> Decision (2026-11-03): webhook router (P06-T02).` under specs/19 §3)
1. `format` column stores the signing/header family: `bland` for both the bare Call Object and the `bland-live`
   body (no migration; the payload column holds the rendered body), `envelope` otherwise.
2. Targets in this order, deduplicated by URL (UNIQUE(event_id,url); insert `ON CONFLICT (event_id, url) DO NOTHING`,
   first planned target wins): per-call webhook → batch status_webhook → conversation webhook → widget webhook →
   org endpoints (enabled, ordered by created_at,id).
3. Per-call webhook (`calls.config.webhook`, any call event): `call.completed` always (body = event.data, the Call
   Object); other call events only when their §2 key is in `calls.config.webhook_events` (body §5 with
   `metadata` = `calls.metadata`); key `—` (call.analysis_ready) never live.
4. Batch: `batch.*` with `batch_id` and `batches.status_webhook` → `bland` body §6: `status` = type without
   `batch.` prefix, `phase` = type, counts from the row, `created_at`/`updated_at` from the row (ms Z);
   `batch.completed`/`batch.stopped` add `completed_at` (row value, else event.occurred_at); `batch.failed` adds
   `code`/`reason` (row failure_code/failure_reason, else event.data code/reason, else "batch_failed"/"").
5. Conversation webhooks: `sms.*`/`conversation.*` with `conversation_id` → `conversations.webhook`;
   `widget.message`/`conversation.*` with `widget_id` → `widgets.webhook_url`. Envelope.
6. Org endpoints: `fnmatchcase(type, pattern)` for any pattern in `event_types` (`*` all, `call.*`). Envelope §7;
   data = Call Object for call.completed, §5 body for other call events (call_id set), §6 body for batch.*, else
   event.data. Platform-admin side of system.fraud_suspected is out of scope (T02 = org endpoints only).
7. HIPAA: when org `hipaa_mode`, a target that is not an org endpoint with `phi_allowed=true` gets transcripts
   removed: Call Object keys `transcripts` → [], `concatenated_transcript` → "", `pathway_logs` entries lose `text`
   and `user_text`; live bodies of `call.turn` and `call.post_transfer_transcript` lose `data.text`. (Per-call webhooks
   have no phi flag → stripped.)
8. Timestamps written by the router are UTC ISO 8601 with milliseconds and `Z` (`2026-10-24T18:22:31.412Z`).
9. `route(event, session=None)` opens its own `database.tenant_session(event.org_id)`; with a session it inserts in
   the caller's transaction (outbox). `route_many(events, session=)` for batched callers (T07 media flush).
   Lookups are lazy: no call/batch/org query unless the event can reach that target; org settings read only when a
   delivery is planned, from `orgs.settings` in the same session (never `OrgSettingsReader`: it opens another
   pool session).

### Files
- `packages/va_events/pyproject.toml`: add `va-domain` dependency (+ `[tool.uv.sources] va-domain = { workspace = true }`); `uv lock`.
- `packages/va_events/src/va_events/catalog.py`: `WEBHOOK_KEY: Final[Mapping[EventType, str | None]]` = §2 last
  column for `call.*`/`citations.ready`/`evals.completed` (`None` for `—` and for every non-call type);
  `ALWAYS_CALL_WEBHOOK: Final = frozenset({EventType.CALL_COMPLETED})`. Export both.
- NEW `packages/va_events/src/va_events/webhook_payloads.py` (pure):
  `iso_ms(dt) -> str`; `live_body(event, metadata) -> dict[str, JsonValue]` (§5 keys exactly: call_id, category
  (event.category or DEFAULT_CATEGORY), level, message, data, seq, created_at=iso_ms(occurred_at), metadata);
  `batch_body(event, BatchFacts) -> dict` (§6; `BatchFacts` frozen dataclass of the row values);
  `envelope_body(event, data) -> dict` (§7 keys: id, type, created_at, org_id, call_id, batch_id, conversation_id, data;
  uuids as str or None); `strip_phi(event_type, body, *, fmt) -> dict` (decision 7, returns a new dict);
  `WebhookFormat` reuse from va_db.models.
- NEW `packages/va_events/src/va_events/webhook_routing.py` (pure planning):
  `@dataclass(frozen=True) CallTarget(url, webhook_events: frozenset[str], metadata: dict)`,
  `EndpointTarget(id, url, event_types: tuple[str,...], phi_allowed)`, `RoutingFacts(call, batch: BatchFacts|None,
  batch_url, conversation_url, widget_url, endpoints, hipaa)`, `PlannedDelivery(url, format, payload, endpoint_id)`;
  `wants(event, call: CallTarget) -> bool`, `matches(event_type, patterns) -> bool`,
  `plan_deliveries(event, facts) -> list[PlannedDelivery]` (dedupe by URL, order of decision 2, HIPAA strip).
- NEW `packages/va_events/src/va_events/webhook_router.py`: `class DbWebhookRouter` (implements the Protocol):
  `__init__(self, database: Database)`; `async route(event, *, session)`; `async route_many(events, *, session)`;
  internals `_facts(session, event) -> RoutingFacts` (lazy queries: `select(Call.config, Call.metadata_)` by org+id,
  Batch by id, Conversation.webhook, Widget.webhook_url, enabled endpoints, `Org.settings` → `load_settings().hipaa_mode`),
  `_insert(session, event, planned)` = `pg insert(WebhookDelivery).values([...]).on_conflict_do_nothing(index_elements=["event_id","url"])`
  with id=new_id(), call_id/batch_id/conversation_id from the event, status default. Returns number inserted? keep
  `-> None` per Protocol; `route_many` same.
  Export `DbWebhookRouter`, payload helpers from `va_events.__init__`.
- specs/19 §3: Decision block above (one paragraph list).
- AGENTS.md: learned-bullet for P06-T02.

### Tests (packages/va_events/tests)
- `test_catalog.py`: `test_webhook_keys_match_the_spec_column` (parse 5th column: `` `queue` `` etc.; `—`/batch/sms/
  "always sent"/"org endpoints"/"SMS conversation" → None) — keys only for call-event types; `call.completed` in
  ALWAYS_CALL_WEBHOOK; every key ∈ `va_domain.calls.WEBHOOK_EVENTS`.
- NEW `test_webhook_payloads.py`: live body exact keys/values (seq, ms Z, category default when event.category None,
  metadata), envelope exact keys for call.completed (data = Call Object) and sms event, batch body for in_progress /
  completed (completed_at) / failed (code, reason), `iso_ms` of a non-UTC aware datetime, strip_phi for Call Object and
  call.turn (input not mutated), non-transcript events unchanged.
- NEW `test_webhook_routing.py` (pure matrix, parametrized): call.completed → bland to call webhook even with empty
  webhook_events; call.latency with/without `latency` in webhook_events; call.amd key `call`; call.analysis_ready never;
  no call webhook → nothing; batch.in_progress → batch url bland §6; sms.received with conversation url → envelope;
  widget.message → widget url; org endpoint globs (`*`, `call.*`, `sms.received`, non-matching, disabled excluded by
  facts builder is DB-side so here: patterns); duplicate URL across call webhook + endpoint → one delivery (bland);
  HIPAA: endpoint phi_allowed keeps transcripts, phi false + call webhook stripped, hipaa off untouched.
- NEW `test_webhook_router_db.py` (integration, `migrated_postgres` module fixture like test_call_events_db, seed org via
  `tenant_rows.OrgFactory`, calls via `call_rows.seed_call(..., config={...webhook...})`, endpoints/batches/conversations
  via ORM inserts in tenant_session): rows created with right url/format/payload/endpoint_id/call_id/event_type/status
  pending for (a) call.completed + endpoint `call.*`; (b) disabled endpoint ignored; (c) other org's endpoint never used
  (RLS + filter); (d) routing the same event twice → one row per URL; (e) **outbox atomicity**: in
  `tenant_session` route + raise → no rows; publisher_rig-style `EventPublisher(redis, call_events=None,
  router=DbWebhookRouter(db)).publish(event, session=s)` then rollback → no rows, commit → rows; (f) session=None path
  opens its own transaction and commits; (g) `route_many` inserts for several events; (h) hipaa org (set orgs.settings
  `{"hipaa_mode": true, "baa_signed_at": ...}` via system/global session) strips per-call webhook payload.
- Gates: `make fmt lint typecheck placeholders`, `uv run pytest packages/va_events -q`, then `make test` in background
  per AGENTS (or at least va_events + va_db + api suites) and `make check`. No e2e needed (no flow wiring yet).

### Risks
- RLS: org endpoints query must also filter `org_id == event.org_id` explicitly.
- `orgs` has no RLS; reading `Org.settings` inside tenant_session is allowed (va_app SELECT grant) — verify.
- jscpd: keep the DB test helpers small/shared; mypy strict on JSON dict typing (`dict[str, JsonValue]`).
- Bland format for batch status webhook: format column `bland`; dispatcher (T03) picks the 10 s timeout from
  `event_type` starting with `batch.`.

## Build P06-T02 (builder notes)
- Implemented as planned; deviation: honored org setting `webhook_format` (specs/04 §7) for the per-call target
  (`DeliveryPolicy.call_format`), recorded in the specs/19 §3 Decision item 3. `strip_phi` applied to the content
  body before envelope wrapping (no `fmt` param needed). Shared `reaches_batch/conversation/widget` predicates keep
  the router's lazy lookups and the planner in step.

## Review P06-T02 — approved
- Diff matches specs/19 §3/§5/§6/§7, specs/03 §9 webhook_deliveries, and specs/23 §7 (phi_allowed). org_id is filtered in every query, and the rollback test proves the outbox drops rows when the transaction rolls back.
- make check: lint/typecheck/placeholders pass; tests 10866 passed, 1 failed: apps/media_worker/tests/test_runtime_sim.py::
  test_the_background_bed_fills_the_agent_track_between_utterances[none-False-None] (idle window -21.8 dBFS instead of
  -inf: agent speech leaked into the "between utterances" window under full-suite load). Not touched by this diff; it passed 3 times on its own and
  in a full run of its module under -n 8. Opened fix task fix:bed-none-flake.
