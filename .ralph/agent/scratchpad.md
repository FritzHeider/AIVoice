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

## Plan P06-T03 — Webhook dispatcher service

Spec: specs/19 §8 (+ §6 timeouts, §11 tests), specs/28 §6.4 (SSRF, pinning), specs/27 §3.5 + span `webhook.deliver`,
specs/03 §2 OutboxRepo Decision, §9 DDL. Existing: `va_db.repositories.OutboxRepo` (claim_due SKIP LOCKED + lease,
mark_succeeded/schedule_retry/mark_dead, each attempts+1), `va_common.net.check_destination_url/blocked_address`,
`va_db.provisioning.OrgProvisioner.webhook_signing_secret(org)` + `webhook_secret_aad`, metrics catalog
`va_webhook_*` (Service.WEBHOOK_DISPATCHER), `OrgSettingsReader` (`allow_insecure_webhooks`), `va_common.email`,
`va_events` publisher (`sequence` → `insert_rows` → commit → `broadcast`). App skeleton `apps/webhook_dispatcher`
(probes only). Endpoint mgmt/rotation API = T04 (design signer to take several secrets now).

### Decisions (add `> Decision (2026-11-03): webhook dispatcher (P06-T03).` under specs/19 §8)
1. Attempts: the first send plus 8 retries (MASTER "8 retries over ~24 h"). After failed attempt n (n = 1..8) the next
   attempt waits `RETRY_DELAYS_S[n-1]` = 10, 30, 120, 600, 1800, 7200, 21600, 54000 s × uniform(0.9, 1.1); failed
   attempt 9 → `dead`. (`attempts` column counts sends; table "attempt n → delay" = delay after failed attempt n.)
2. Timeout per attempt: 10 s when `event_type` starts with `batch.`, else 15 s (whole request incl. connect).
3. Outcomes / `last_error` codes: 2xx → `succeeded`; 3xx → `redirect` (never followed); other status → `http_status`
   (code in `last_status_code`); `timeout`; `connection` (connect/read errors); `gone` (410 → dead at once);
   `blocked_destination:<reason>` (SSRF guard: `scheme` → dead at once because the URL never changes; `host`,
   `address`, `resolution` → retry, DNS can change); `endpoint_disabled` (row of a disabled endpoint → dead, not sent);
   `secret_unavailable` (decrypt failed → retry). `last_response_ms` set when a response arrived/timeout elapsed.
4. http allowed only in `local`/`test` or when the org setting `allow_insecure_webhooks` is true (staging too);
   private targets only with `WEBHOOK_ALLOW_PRIVATE` (local/test). Check before sending with `check_destination_url`
   (reason classification, nothing connects) AND at connect time in the transport (DNS-rebinding pinning).
5. Request: body = `orjson.dumps(payload)` bytes (signed bytes == sent bytes); headers per §8: `Content-Type`,
   `User-Agent: VoiceAgents-Webhooks/1.0`, `X-VA-Event-Id`+`X-Event-Id` (row `event_id`), `X-VA-Event-Type`,
   `X-VA-Timestamp`, `X-VA-Signature: t=<t>,v1=<hex>[,v1=<hex>]` (one v1 per active secret, current first),
   `X-Webhook-Signature` = hex HMAC-SHA256(current secret, body). Response body read capped at 64 KiB then discarded.
6. 410 on an org endpoint (`endpoint_id` set): conditional `UPDATE webhook_endpoints SET enabled=false WHERE id=… AND
   enabled` in the same system transaction; only when it flipped, email every owner/admin of the org (one message
   each, template `webhook_endpoint_disabled`, link `{DASHBOARD_BASE_URL}/settings/webhooks`), failures logged never
   raised. Rows claimed for an already-disabled endpoint → dead `endpoint_disabled` without a request.
7. Per-attempt call log: every attempt of a delivery with `call_id` writes a `call_events` row through the new event
   type `call.webhook_delivery` (category `webhook`, emitted by dispatcher, webhook key `—`), level `info` on success,
   `warn` on retry, `error` on dead; message `Webhook delivered to <host>: 200 (123ms)` / `Webhook to <host> failed:
   <code> (attempt n), retrying in 30s` / `... failed: <code>, giving up`; data `{delivery_id, event_id, event_type,
   attempt, status_code, error, response_ms, host}` (host only, never the full URL). Written in the SAME system
   transaction as the outbox update (`EventPublisher.sequence` → `va_events.insert_rows` → commit → `broadcast`).
   The dispatcher's publisher has NO router (an org endpoint matching `*` must never get deliveries about deliveries).
8. Signing secrets cached in-process per org for 300 s (injected clock monotonic); T04 adds the previous secret.
9. Concurrency: global semaphore 200, per-host (lowercased URL host) 20, claim batch 200; poll interval 1 s when the
   last claim was empty, immediate re-poll when it was full. Backlog gauges refreshed each poll from
   `OutboxRepo.backlog(now)`.
10. SIGTERM: stop claiming, wait for in-flight attempts up to 20 s; unfinished rows recover via the 5 min lease.

### Files
- NEW `packages/va_common/src/va_common/safe_http.py` (specs/28 §6.4 `safe_client`):
  `class GuardedBackend(httpcore.AsyncNetworkBackend)` (`__init__(*, allow_private: bool, resolver: HostResolver,
  inner: httpcore.AsyncNetworkBackend | None = None)`; `connect_tcp(host, port, timeout=, local_address=,
  socket_options=)`: IP literal → `blocked_address` check; name → `resolver(host)` within `DNS_TIMEOUT_S`, EVERY answer
  must pass, then connect `inner.connect_tcp(first_vetted_ip, port, ...)`; refusal raises
  `va_common.net.UnsafeDestinationError` before any socket; `connect_unix_socket` refuses; `sleep` delegates).
  `def safe_transport(*, allow_private, resolver=system_resolver, limits=httpx.Limits(...)) -> httpx.AsyncHTTPTransport`:
  build `httpx.AsyncHTTPTransport(limits=..., trust_env=False)` then replace its `_pool` with
  `httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(), max_connections=, max_keepalive_connections=,
  keepalive_expiry=, http1=True, http2=False, network_backend=GuardedBackend(...))` (TLS SNI/Host stay the name since
  httpcore passes `server_hostname` from the origin). A unit test asserts `_pool` exists and is an AsyncConnectionPool
  (guards httpx upgrades; httpx 0.28.1 has no `network_backend` kwarg).
  `def safe_client(settings, *, timeout, allow_private, resolver=system_resolver, max_connections=200,
  max_keepalive=50, headers=None, version=) -> httpx.AsyncClient` = `http_client(settings, ..., transport=safe_transport(...))`
  (never follows redirects). Export from module only.
- `packages/va_db/src/va_db/repositories/outbox.py`: add `backlog(now) -> OutboxBacklog(due: int, oldest_due_at:
  datetime | None)` (pending/delivering with next_attempt_at <= now); `endpoint_enabled(ids) -> dict[UUID, bool]`;
  `disable_endpoint(endpoint_id) -> bool` (conditional, returns whether it flipped). Export `OutboxBacklog`.
- `packages/va_db/src/va_db/repositories/memberships.py` `MembershipLookupRepo.admin_emails(org_id) -> list[str]`
  (owners+admins, ordered by email; users not deleted if such column) — system scope, already a listed repo.
- `packages/va_events/src/va_events/catalog.py` + specs/19 §2 row: `call.webhook_delivery` | webhook | dispatcher |
  each delivery attempt of a webhook for a call | `—` (`_CATEGORIES`, WEBHOOK_KEY None; test_catalog parses §2).
- `packages/va_common/src/va_common/email/templates.py`: `webhook_endpoint_disabled(*, url: str, link: str, to: str)
  -> EmailMessage` via `_Copy` (escape url).
- NEW `apps/webhook_dispatcher/src/va_webhooks/`:
  - `signing.py`: `SIGNATURE_HEADER="X-VA-Signature"`, `BLAND_SIGNATURE_HEADER="X-Webhook-Signature"`;
    `def signature_headers(secrets: Sequence[str], body: bytes, timestamp: int) -> dict[str, str]` (ValueError on
    empty secrets); `def request_headers(delivery_event_id, event_type, body, secrets, timestamp) -> dict[str, str]`.
  - `schedule.py`: `RETRY_DELAYS_S`, `MAX_ATTEMPTS = 9`, `JITTER = 0.1`, `retry_delay_s(attempt: int, rng:
    random.Random) -> float | None` (None = dead), `BATCH_TIMEOUT_S = 10.0`, `DEFAULT_TIMEOUT_S = 15.0`,
    `timeout_for(event_type, timeouts=Timeouts())` (`Timeouts` frozen dataclass so tests shrink them).
  - `secrets.py`: `class SigningSecrets(database, envelope, *, clock, ttl_s=300.0)`, `async get(org_id) ->
    tuple[str, ...]` (reads `orgs.webhook_signing_secret_enc` via `global_session` + `webhook_secret_aad`;
    raises `SecretUnavailableError` on missing org / decrypt failure).
  - `destinations.py`: `class DestinationPolicy(org_settings: OrgSettingsReader-like, *, va_env, allow_private,
    resolver)`, `async check(org_id, url) -> None | UnsafeReason` (allow_http per decision 4; resolution via
    `check_destination_url`).
  - `delivery.py`: `AttemptResult(outcome: Literal["succeeded","retry","dead"], status_code, error, response_ms,
    next_attempt_at)`, `class Deliverer(http: httpx.AsyncClient, *, clock, timeouts)` `async send(delivery, secrets,
    now) -> SendResult(status_code|None, error|None, response_ms|None)` (POST, `timeout=timeout_for(...)`, classify
    per decision 3, span `webhook.deliver` with `http.url` = host, `http.status_code`, `va.attempt`), and pure
    `decide(delivery_attempts_so_far, send_result, now, rng) -> AttemptResult`.
  - `recording.py`: `async record_attempt(session, delivery, result, *, publisher, clock) -> DomainEvent | None`
    (OutboxRepo update + optional disable + call_events row; returns the event to broadcast after commit) and the
    message builders (pure, unit-tested).
  - `dispatcher.py`: `DispatchLimits(concurrency=200, per_host=20, claim_batch=200, poll_interval_s=1.0,
    drain_timeout_s=20.0)`; `class Dispatcher(*, database, deliverer, secrets, destinations, publisher, notifier,
    clock, rng, metrics, limits=DispatchLimits())` with `async run(stop: asyncio.Event)`, `async run_once() -> int`
    (claim + deliver all claimed, returns count; test seam), `async refresh_backlog()`; per-host semaphores in a dict
    (created lazily, dropped when idle). Metrics `va_webhook_deliveries_total{result=succeeded|failed|dead,format}`,
    `va_webhook_delivery_ms`, backlog gauges.
  - `notify.py`: `class EndpointDisabledNotifier(database, sender: EmailSender, *, dashboard_base_url)` `async
    notify(org_id, url)` (admin_emails via system session, then send; log `webhook.endpoint_disable_notify_failed`).
  - `settings.py`: add `CryptoSettings`, `EmailSettings`, `dashboard_base_url` (check how api declares it; reuse).
  - `main.py`: `build_container` opens Database (`configure_database`), redis, KMS (`open_kms_provider`) → `Envelope(
    kms, OrgDataKeyStore(database))`, `OrgSettingsReader`, email sender (InMemory only VA_ENV=test, else SMTP — same
    rule as api), `safe_client`, `EventPublisher(redis, call_events=None, router=None)`; raises RuntimeError before
    opening anything when `LOCAL_MASTER_KEY` missing with local KMS. Lifespan supervises `Dispatcher.run`; drain on
    stop. Keep readiness = Postgres only.
  - pyproject deps: httpx, httpcore (direct, since safe_http lives in va_common: add httpcore to va_common deps),
    orjson, va-db, va-events, va-domain, opentelemetry-api; `uv lock`.
- specs/02 §6 env table scope column: add `dispatcher` to KMS_PROVIDER/LOCAL_MASTER_KEY/AWS_KMS_KEY_ID ("same"),
  SMTP_URL/EMAIL_FROM, DASHBOARD_BASE_URL (keep `.env.example` mirror test green; compose `x-py-env` passes them).
- specs/19 §8 Decision block; specs/28 §6.4 short Decision: `safe_client` = guarded backend pinning (no redirects yet;
  redirect re-validation arrives with tools).
- AGENTS.md learned bullet (P06-T03).

### Tests
- `packages/va_common/tests/test_safe_http.py`: pinning — resolver maps `hooks.test` → `127.0.0.1`, allow_private=True,
  request to `http://hooks.test:<port>/x` reaches a `RecordingServer` receiver with `Host: hooks.test:<port>`;
  refusal parametrized over every specs/19 §8 range + §6.4 extras (one sample address each: 10.1.2.3, 172.16.5.4,
  192.168.1.1, 127.0.0.1, 169.254.169.254, ::1, fc00::1, fe80::1, 100.64.0.1, 0.0.0.0, 192.0.0.8, 198.18.0.1,
  240.0.0.1, ff02::1, ::ffff:10.0.0.1) with allow_private=False → `UnsafeDestinationError("address")`, a counting
  inner backend proves no connect; mixed public/private answer refused; metadata refused even with allow_private;
  `_pool` type guard; IP-literal URL checked without resolver call.
- `apps/webhook_dispatcher/tests/test_signing.py`: fixed vector (secret `whsec_test`, t `1700000000`, body `{"a":1}`)
  both headers → syrupy snapshot committed (`__snapshots__`); recomputed independently with hmac in the test; two
  secrets → two `v1=` in order; empty secrets ValueError.
- `test_schedule.py`: delays per attempt 1..8 within ±10 % (rng stubs returning 0.0/1.0 → exact bounds), attempt 9 →
  None, cumulative ≈ 24 h (sum 85,960 s), batch timeout 10 / others 15.
- `test_delivery.py` (real uvicorn receiver via `service_helpers.RecordingServer`, fake resolver, tiny `Timeouts`):
  success (headers present, signature verifies with hmac over the received raw body, `X-Event-Id` == `X-VA-Event-Id`),
  500 → `http_status`, hanging handler → `timeout`, 302 → `redirect` and redirect target never hit, 410 → dead `gone`,
  connection refused port → `connection`; `decide` table tests.
- `test_dispatcher_db.py` (module Postgres via `pg_container.running_postgres` + `migration_helpers`
  `fresh_database`/`upgrade`; org via `OrgProvisioner` with local KMS so a real encrypted secret exists; SystemClock for
  call_events partitions): (a) success → row `succeeded`, attempts 1, delivered_at, call_events row category `webhook`
  data.attempt 1, metric succeeded+1, receiver signature verifies with provisioner's plaintext secret; (b) 500 → `pending`,
  attempts 1, next_attempt_at ≈ now+10 s ±10 %, `warn` call event; (c) 9th failure → dead; (d) timeout; (e) redirect;
  (f) 410 on endpoint → dead + endpoint disabled + one email per owner/admin in `InMemoryEmailSender`, second 410 row
  → `endpoint_disabled` without request and no second email; (g) private-IP refusal (allow_private=False, resolver →
  10.0.0.5) → `blocked_destination:address`, retry, receiver untouched; http URL with VA_ENV production policy and
  `allow_insecure_webhooks` false → dead `blocked_destination:scheme`, true → delivered (policy unit with stub reader is
  fine); (h) multi-pod: two Dispatchers (separate Database objects) `run_once` loops concurrently over 300 rows →
  receiver sees each event_id exactly once, all succeeded; (i) per-host concurrency: per_host=3, gated receiver, two hosts
  (`a.hooks.test`, `b.hooks.test` both → 127.0.0.1): max in-flight for host a == 3 while host b progresses; (j) lease
  recovery: claim via OutboxRepo directly without finishing, advance (FrozenClock now+5 min+1 s for claim) → claimed again;
  (k) no call_id → no call_events row; (l) run(stop) drains: set stop during a gated delivery → finishes and returns.
- `packages/va_db/tests`: OutboxRepo `backlog`, `endpoint_enabled`, `disable_endpoint` (flip once), MembershipLookupRepo
  `admin_emails` (owner+admin, not member/viewer, other org excluded).
- `packages/va_events/tests/test_catalog.py` keeps passing with the new row; email template test.
- `apps/webhook_dispatcher/tests/test_dispatcher.py`: settings defaults (crypto/email), build_container refuses without
  LOCAL_MASTER_KEY, probes unchanged.
- Gates: `make fmt lint typecheck placeholders`, `uv run pytest apps/webhook_dispatcher packages/va_common
  packages/va_db packages/va_events -q`, then full `make test` in background (log + heartbeat Monitor) and `make check`.
  No e2e (no call flow touched; compose image builds: `docker build ... SERVICE=webhook_dispatcher` sanity).

### Risks
- httpx private `_pool` swap: guarded by a test; if httpx changes, implement a minimal transport instead.
- Real-time timeout tests: keep Timeouts at ~0.3 s, handler waits on an event released at teardown (no sleeps).
- xdist: receivers on port 0; per-module Postgres; unique org per test.
- uv not on PATH on the runner: `export PATH=/home/ralph/.local/bin:$PATH`.

## Build P06-T03 (builder notes)
- Implemented per plan. Deviations: safe client lives in `va_common.safe_http` (cycle with va_common.http; specs/28
  §6.4 Decision). Call-event level is `warning` (CallEventLevel has no `warn`). Retry sum is 85 360 s (plan said 85 960).
  Secrets module named `signing_secrets.py` (avoid stdlib name). Container = `va_webhooks.container`.
- Dispatcher loop: tasks set bounded by concurrency; claim errors logged + poll wait; `_deliver` catches everything
  (lease recovery). Call event inserted in a savepoint so a partition miss never rolls back the outbox update.
- Tests: 66 in apps/webhook_dispatcher (DB tests with real receiver), test_safe_http (pinning + every range),
  va_db repo tests (backlog/endpoint_enabled/disable_endpoint/admin_emails), email template test.
- Rig timeouts 5 s by default (0.5 s flaked under cov+xdist in the 300-row two-pod test).
- Gates: make check exit 0 (10952 passed, 7 skipped, coverage 99.07 %, TS tests pass, openapi + spec refs current).
  After that run, a jscpd refactor (4 clones from this diff → 0): shared `service_helpers.serving(app)` (media
  `served_media` uses it), `packages/va_common/tests/dns_fakes.MappedResolver` (api `call_fakes.FakeResolver`
  subclasses it), public `va_common.net.resolve_host` (the safe_http backend reuses it). Re-ran va_common, dispatcher,
  api, media_worker tests: 2866 passed; fmt/lint/typecheck/placeholders/spec refs pass. pip-audit clean; pnpm audit
  3 moderate (no high); radon avg 2.58. No e2e: no call flow touched (dispatcher container is not yet in an e2e flow).

## Review P06-T03 (reviewer notes)
- Gates re-run this iteration: lint pass, typecheck pass (1322 files), placeholders 0 findings; make test running.
- Diff checked vs specs/19 §8 + Decision P06-T03, specs/28 §6.4, specs/27 metrics (names/labels match), specs/02 §6.
- Non-blocking observations (for later tasks):
  1. Tasks waiting on a per-host semaphore occupy global concurrency slots: one slow customer host with ≥200 due
     rows can starve other hosts for up to ~timeout × 10. Consider claiming fewer rows per saturated host later.
  2. A deleted endpoint (FK SET NULL) leaves pending rows delivered to its old URL; endpoint management (P06-T05)
     should cancel pending rows on delete.
- Verdict: APPROVED (make test 10952 passed, 7 skipped, 99.06 %; lint/typecheck/placeholders pass).

## Plan P06-T04a — Webhook signing secret view and rotation
P06-T04 was split (plan file updated) into T04a (this: secret storage, rotate/view routes, dispatcher dual-sign) and
T04b (endpoints CRUD, test send, deliveries list, replay; T04b also moves Deliverer/signing/destinations into a shared
package so the api test send can reuse them, and cancels pending rows on endpoint delete — reviewer note 2 of T03).

### Decisions (add `> Decision (2026-11-03): signing secret rotation (P06-T04a).` under specs/19 §10)
1. Storage: `orgs` gains three columns, appended LAST in the specs/03 orgs DDL (ADD COLUMN attnum rule):
   `webhook_signing_secret_created_at timestamptz NOT NULL DEFAULT now()`,
   `webhook_signing_secret_prev_enc bytea` (envelope-encrypted, same AAD shape via
   `column_aad("orgs","webhook_signing_secret_prev_enc", org_id)`), `webhook_signing_secret_prev_expires_at timestamptz`.
   Migration `0006_webhook_secret_rotation.py`: three `ALTER TABLE orgs ADD COLUMN …` (one statement each through
   `op.execute(sa.text(...))`), then `UPDATE orgs SET webhook_signing_secret_created_at = created_at`; downgrade drops
   the three columns. Existing grants cover new columns. Add the prev column to specs/28 §3 encrypted-columns list.
2. Active secrets = current, plus previous while `now < prev_expires_at` (current first). Rotation window 24 h
   (`ROTATION_OVERLAP = timedelta(hours=24)`).
3. Rotate (in one `global_session`, `OrgRepo.lock(org_id)` first so concurrent rotations serialize): new secret =
   `WEBHOOK_SECRET_PREFIX + generate_token()`; previous := the CURRENT secret (re-encrypted under the prev AAD — decrypt
   current, encrypt with prev AAD), `prev_expires_at = now + 24 h`; current := new, `created_at = now`. A rotation
   during an open window drops the older previous secret (only one previous kept; documented). Response
   `{data:{secret, created_at, previous_expires_at}}`: `secret` shown once; `withhold_on_replay(request, "data.secret")`.
   Audit `webhook.secret_rotate` resource_type `org`, resource_id org id, before `{secret_last4: old4, created_at}`,
   after `{secret_last4: new4, created_at, previous_expires_at}` (last4 only; `redact` would hide a key named
   `secret`, so never pass the secret itself).
4. View `GET /v1/webhooks/signing-secret` → `{data:{secret_last4, created_at, previous_secret_last4|null,
   previous_expires_at|null}}` (previous fields only while the window is open). Spec table lists
   `{secret_last4, created_at}`; extra fields documented in the Decision.
5. Auth: both routes on a new envelope router `webhooks_router` (`envelope_router(prefix="/v1/webhooks")`, tag
   `webhooks`, in `main.ROUTERS`); GET `require_scope("webhooks:read")` + `require_permission("webhooks.secret")`,
   POST `require_scope("webhooks:write")` + `require_permission("webhooks.secret")`; `openapi_extra={"x-scope":…,
   "x-permission":"webhooks.secret"}`; rotate `**audited("webhook.secret_rotate")`. Developer/analyst/viewer/billing
   sessions → 403; a `*` API key passes; a `webhooks:write` key without `*` lacks the permission → 403 (per
   holds_permission semantics — verify, keep whatever `holds_permission` does and assert it).
   The orgs table has no RLS: use `global_session()` + `OrgRepo` with `ctx.org_id` only (as `GET /v1/org`). Acting
   parent on sub-account → operates on the child's (ctx.org_id) secret, which is correct.
6. Dispatcher: `SigningSecrets.get` returns `active_signing_secrets(...)` result; cache entry expiry =
   `min(monotonic + ttl, monotonic + (prev_expires_at - now))` so an expired previous secret stops being signed with
   promptly. Rotation visibility lag ≤ 300 s is fine (old secret valid 24 h).

### Files
- `packages/va_db/src/va_db/migrations/versions/0006_webhook_secret_rotation.py` (pattern of 0005).
- `specs/03-data-model.md` orgs DDL (3 columns last, after `updated_at` — check `test_schema_matches_spec` compares
  attnum; 0001 DDL must not change).
- `packages/va_db/src/va_db/models/tenancy.py` `Org`: `webhook_signing_secret_created_at: Mapped[datetime]` (`tz()`
  server_default now()), `webhook_signing_secret_prev_enc: Mapped[bytes | None]` (LargeBinary),
  `webhook_signing_secret_prev_expires_at: Mapped[datetime | None]`. Autogenerate parity must stay `[]`.
- NEW `packages/va_db/src/va_db/webhook_secrets.py`:
  - `ROTATION_OVERLAP: Final = timedelta(hours=24)`; `previous_secret_aad(org_id) -> str`.
  - `@dataclass(frozen=True) SecretInfo(secret_last4: str, created_at: datetime, previous_secret_last4: str | None,
    previous_expires_at: datetime | None)`.
  - `@dataclass(frozen=True) ActiveSecrets(secrets: tuple[str, ...] (repr=False), previous_expires_at: datetime | None)`.
  - `async def active_signing_secrets(envelope, org: Org, *, now) -> ActiveSecrets` (current first, previous only
    while open).
  - `async def secret_info(envelope, org, *, now) -> SecretInfo` (decrypts to compute last4; last4 = last 4 chars).
  - `@dataclass(frozen=True) RotatedSecret(secret: str (repr=False), created_at, previous_expires_at, before: SecretInfo,
    after: SecretInfo)`.
  - `async def rotate_signing_secret(session, envelope, org_id, *, now) -> RotatedSecret` (lock via
    `OrgRepo(session).lock`, NotFoundError/AppError NOT_FOUND if missing; writes via ORM attribute assignment + flush).
  - Move `webhook_secret_aad` here? NO — keep it in provisioning (imported by dispatcher); import it from there.
  - Provisioning: set nothing new (server default now()); `ProvisionedOrg` unchanged.
- `apps/webhook_dispatcher/src/va_webhooks/signing_secrets.py`: use `active_signing_secrets`, cache expiry rule (6).
- NEW `apps/api/src/va_api/routers/webhooks.py` (router + two routes) and `routers/webhooks_schemas.py`
  (`SigningSecretView`, `RotatedSigningSecret` pydantic models with `examples`; secret example must be obviously fake
  but pass gitleaks — e.g. `"whsec_" + "x"*43` literal? gitleaks generic rule may trip: use a short illustrative
  `whsec_EXAMPLE…`; if gitleaks complains add a scoped allowlist entry like earlier api_key examples — check
  `.gitleaks.toml` for the api_keys example pattern and mirror it).
- `apps/api/src/va_api/main.py` ROUTERS; `make openapi` + both `gen:api` (dashboard, sdk schema.d.ts).
- specs/19 §10 Decision; specs/28 §3 encrypted column list; AGENTS.md learned bullet (P06-T04a).

### Tests
- `packages/va_db/tests/test_webhook_secrets_db.py` (module Postgres like other va_db DB tests; local-KMS Envelope +
  OrgProvisioner): fresh org → active = (provisioned secret,), info previous None, created_at set; rotate → returns new
  `whsec_` secret ≠ old, active = (new, old), prev_expires_at = now + 24 h; at now+24h-1s still two, at now+24h → one;
  second rotate during window → (newest, second), first gone; rotate unknown org → NOT_FOUND; concurrent rotations
  (two sessions via gather) serialize: final previous == the secret produced by the first committed rotation (assert
  active secrets are two distinct and both returned secrets appear consistently) — keep it simple: assert no lost
  update, i.e. the second rotation's previous equals the first rotation's new secret.
- Migration tests: `migration_helpers` upgrade to head then downgrade to 0005 and back (existing generic up/down tests
  may cover; add an assertion that 0006 backfills `webhook_signing_secret_created_at = created_at` for a pre-existing
  org row inserted at revision 0005). Schema-vs-spec + autogenerate parity tests must pass unchanged.
- `apps/api/tests/test_webhook_signing_secret.py` (session_helpers migrated_postgres, SessionContainer with
  `FrozenClock`): owner GET → last4 matches provisioned secret's last 4, created_at; rotate → 200 with secret, GET
  shows new last4 + previous fields; audit row `webhook.secret_rotate` without the secret anywhere in `changes`
  (search the serialized JSON for both secrets); idempotent replay of rotate with same `Idempotency-Key` → `secret`
  null and no second rotation (GET last4 unchanged); roles: admin 200, developer/analyst/viewer/billing 403 on both;
  API key `*` 200; key with only `webhooks:read`/`webhooks:write` → 403 (permission) — assert against actual
  holds_permission semantics; clock advanced 24 h → previous fields null.
- Cross-tenant suite: routes have no path params → probed automatically; run `apps/api/tests/test_cross_tenant_routes.py`
  and `test_audit_markers.py`, `test_openapi.py`, `test_operations_contract`.
- Dispatcher: `apps/webhook_dispatcher/tests/test_signing_secrets*.py` / extend DB test: after rotation a delivery's
  `X-VA-Signature` has two `v1=` (new first), both verify with hmac; `X-Webhook-Signature` uses the new secret; after
  advancing the clock past the expiry (and the cache) only one `v1=`. Cache unit test: entry expires at prev expiry
  even when ttl is longer (ManualClock/RecordingClock with monotonic).
- Gates: `make fmt lint typecheck placeholders`; `uv run pytest packages/va_db apps/api apps/webhook_dispatcher -q`;
  `make openapi` + gen:api + `make test-ts`; full `make test` in background with heartbeat Monitor; `make check`.
  No e2e (no call flow touched).

### Risks
- `test_schema_matches_spec` attnum: columns must be the last three in the spec's orgs DDL.
- gitleaks on `whsec_` example strings in schemas/openapi.json — check `.gitleaks.toml` before choosing the example.
- FrozenClock vs DB `now()`: compute `created_at`/expiry from the injected clock, never DB now().
- uv PATH on the runner: `export PATH=/home/ralph/.local/bin:$PATH`.

## Build P06-T04a (builder notes)
- Implemented per plan. Migration 0006 (3 ADD COLUMN + backfill), Org model columns sort_order 1-3, specs/03 DDL +
  Decision, specs/19 §10 Decision P06-T04a, specs/28 §3 list. `va_db.webhook_secrets` (active/info/rotate).
- Deviation: rotate reads the active data key inside the same global_session via `use_org` + `OrgDataKeyRepo` and
  seals with `encrypt_with_key` (Envelope.encrypt would open a second pool session while holding the org lock).
- Routes on `routers/webhooks.webhooks_router` (in ROUTERS); openapi + both gen:api regenerated. OpenAPI secret example
  `whsec_Ex4mple...` added to the .gitleaks.toml example allowlist (generic-api-key tripped on it; gitleaks dir clean).
- Dispatcher SigningSecrets uses active_signing_secrets; cache lifetime = min(ttl, prev expiry - now).
- Tests: va_db test_webhook_secrets_db (6) + test_webhook_secret_rotation_migration (4, backfill on real PG);
  head-revision asserts bumped to 0006; tenancy model column/default tables; api test_webhook_signing_secret (14:
  view, rotate, 24 h window, audit w/o secrets, idempotent replay withholds + no 2nd rotation, admin ok, 4 roles 403,
  `*` key ok, narrow keys 403, parent acting on sub-account); dispatcher test_signing_secrets_db (4: dual v1 new first,
  cache expiry at prev expiry, full TTL without window, unknown org).
- Gates: make check exit 0 (10980 passed, 7 skipped, coverage 99.07 %, TS tests pass, openapi + spec refs current).
  Then a test-only jscpd fix (3 clones from the new api test → 0): shared `bare` fixture moved to apps/api/tests/conftest.py
  (test_api_keys_integration uses it), `bearer` imported from call_helpers, local `signed_in_owner`/`db_idempotency`.
  Re-ran: ruff check/format, mypy (1330 files), placeholders 0, apps/api tests 1437 passed. pip-audit clean; pnpm audit
  3 moderate (no high); radon avg 1.95 for new modules. No e2e (no call flow touched).

## Review P06-T04a (reviewer notes)
- Gates re-run: lint pass, typecheck pass (1330 files), placeholders 0, make test 10980 passed / 7 skipped (exit 0).
- Diff checked vs specs/19 §10 Decision, specs/03 orgs DDL (columns last), specs/28 §3, specs/04 §3.2 permission.
  Rotation decrypts via embedded key (no extra pool session); encrypt_with_key with key read in-transaction.
- Verdict: APPROVED.

## Plan P06-T04b — Webhook endpoint and delivery management API
Spec: specs/19 §8 (replay, dead-letter view), §10 table; specs/04 §3.2 row "Webhook endpoints / deliveries / replay"
(owner/admin/developer W, analyst/viewer R, billing —) → GETs `require_scope("webhooks:read")`, mutations
`require_scope("webhooks:write")`, no extra permission (except phi_allowed, below). All routes go on the existing
`routers/webhooks.webhooks_router` (envelope). Every tenant read/write in `tenant_session(ctx.org_id)`.

### Decisions (add `> Decision (2026-11-03): webhook endpoint and delivery management (P06-T04b).` under specs/19 §10)
1. Endpoint body (`WebhookEndpointCreate`, extra forbidden): `url` (absolute http(s), ≤ 2048 chars, vetted at
   create/PATCH time with the Send Call rules: https unless `VA_ENV` local/test or org `allow_insecure_webhooks`;
   private targets only with `WEBHOOK_ALLOW_PRIVATE` → `WEBHOOK_URL_REJECTED` field `url`), `description` (≤ 500,
   default ""), `event_types` (1-100 unique entries; each `*`, an exact `va_events.EventType` value, or `<prefix>.*`
   where some catalog type starts with `<prefix>.`; anything else `INVALID_INPUT` field `event_types[i]`; default
   `["*"]`), `enabled` (default true), `phi_allowed` (default false; setting it true needs the `org.compliance`
   permission (`holds_permission`), else `PERMISSION_DENIED`/403 like other permission refusals — use whatever error
   `require_permission` raises). PATCH (`WebhookEndpointPatch`) = any subset, `model_dump(exclude_unset=True)`;
   explicit `null` on a non-nullable field → INVALID_INPUT. At most 20 endpoints per org → INVALID_INPUT
   ("An org can have at most 20 webhook endpoints.", no field). PATCH re-enabling an auto-disabled endpoint is allowed.
   Changing `url` leaves already-created deliveries on the URL they were created for (the URL is part of the row's
   identity, UNIQUE(event_id,url)); new events use the new URL.
2. View `WebhookEndpointView`: `id, url, description, event_types, enabled, phi_allowed, created_at, updated_at`.
   List = `{data:[...]}` oldest first (no pagination; ≤ 20 rows). DELETE → `{data:{deleted:true}}`.
3. Delete: one tenant transaction: rows with `endpoint_id = id AND status IN ('pending','delivering')` become
   `dead` with `last_error = 'endpoint_deleted'` (new code `ENDPOINT_DELETED`), then the endpoint row is deleted
   (FK sets `endpoint_id` NULL on the rest). In-flight attempts: `OutboxRepo._finish` gains the condition
   `status = 'delivering'`; when no row matches it raises new `va_db.repositories.DeliveryCancelledError`
   (subclass of NotFoundError is NOT wanted — plain `Exception` subclass exported from repositories). The dispatcher
   `_record` catches it (around the system session block), logs `webhook.delivery_cancelled` (info, delivery_id),
   writes no call event, sends no notification, and still counts nothing in metrics. Closes T03 review note 2.
4. Test send `POST /v1/webhooks/endpoints/{id}/test` body `{event_type?}` (must be a catalog `EventType` value,
   default `call.completed`; else INVALID_INPUT field `event_type`). Works on disabled endpoints too. Builds an
   envelope body (§7) `{id: "evt_<uuid7>", type, created_at: iso_ms(now), org_id, call_id: null, batch_id: null,
   conversation_id: null, data: {"test": true, "message": "Test event from VoiceAgents."}}` via
   `va_events.webhook_payloads.envelope_body(new_event(...), data)` (check its exact signature/fields), signs it with
   the org's active secrets (`va_db.webhook_secrets.active_signing_secrets` in a `global_session`, read BEFORE any
   outbound I/O and with no session open during the send), pre-checks the URL with the same rule as (1)
   (refusal → result `blocked_destination:<reason>`, no request), then sends ONCE through the shared
   `Deliverer` (10 s timeout for test sends: `Timeouts(batch_s=10.0, default_s=10.0)`). No `webhook_deliveries`
   row, no retry, no call event. Response `{data: WebhookTestResult{event_id, event_type, succeeded, status_code,
   error, response_ms}}` (always 200; the delivery outcome is in the body). `**audit_exempt("synthetic test
   delivery; stores nothing")`.
5. Deliveries list `GET /v1/webhooks/deliveries` query: `status?` (one of the 5 statuses), `event_type?` (exact),
   `call_id?` (uuid), `endpoint_id?` (uuid, extra filter), `cursor?`, `limit` 1-100 (default `DEFAULT_PAGE_LIMIT`);
   newest first via `TenantRepository.list` (cursor format of specs/31 §2). Returns `PageResponse[WebhookDeliveryView]`.
   View: `id, event_id, event_type, url, endpoint_id, format, status, attempts, next_attempt_at (only when status
   pending, else null), last_status_code, last_error, last_response_ms, call_id, batch_id, conversation_id,
   delivered_at, created_at, updated_at`. The `payload` is never returned (size, PHI). Dead-letter view =
   `?status=dead`. Use a FastAPI query model or `Depends(_filters)` like audit_logs (if a query MODEL is used,
   register a cross-tenant request; with plain `Query` params nothing is needed).
6. Replay `POST /v1/webhooks/deliveries/{id}/replay`: conditional UPDATE in the tenant session
   `WHERE id AND org_id AND status IN ('succeeded','failed','dead')` → `status='pending', attempts=0,
   next_attempt_at=now (injected clock), last_status_code/last_error/last_response_ms/delivered_at = NULL`,
   RETURNING the row → `{data: WebhookDeliveryView}`. Same row, same `event_id`, same URL/payload (new attempt series).
   Refusals: absent/other org → `NOT_FOUND`; `pending`/`delivering` → `CONFLICT` "The delivery is already queued.";
   `last_error == 'endpoint_deleted'` → `CONFLICT` "The delivery's endpoint was deleted."; endpoint exists but
   disabled → `CONFLICT` "Enable the webhook endpoint before replaying its deliveries.". Audit
   `webhook_delivery.replay` (resource_type `webhook_delivery`, before `{status, attempts, last_error}`, after
   `{status: "pending"}`): add the action to the specs/04 §13 list AND `va_db.audit.AUDIT_ACTIONS`.
7. Audit endpoint routes: `webhook_endpoint.create` (after = view dict), `.update` (before/after views; no row when
   nothing changed), `.delete` (before view, after `{cancelled_deliveries: n}`). Pass dicts with JSON-safe values
   (`model_dump(mode="json")`).

### Shared delivery code (move, no behaviour change)
- Move `apps/webhook_dispatcher/src/va_webhooks/{signing,schedule,delivery}.py` to
  `packages/va_events/src/va_events/webhook_signing.py`, `webhook_schedule.py`, `webhook_delivery.py` (update their
  internal imports and docstrings; add `ENDPOINT_DELETED = "endpoint_deleted"` to webhook_delivery codes + `__all__`;
  it is NOT in `_FINAL_ERRORS` usage since the dispatcher never produces it). Delete the old modules; update imports
  in `va_webhooks.call_log`, `container`, `dispatcher` and dispatcher tests. Add explicit deps to
  `packages/va_events/pyproject.toml`: `httpx`, `orjson`, `opentelemetry-api` (copy version specs from va_common's
  pyproject), then `uv lock` (check `uv.lock` diff is only va-events metadata).
- Move tests: `apps/webhook_dispatcher/tests/test_signing.py` → `packages/va_events/tests/test_webhook_signing.py`,
  `test_schedule.py` → `packages/va_events/tests/test_webhook_schedule.py`;
  `apps/webhook_dispatcher/tests/support/webhook_receiver.py` → `packages/va_events/tests/support/webhook_receiver.py`
  (already on pytest pythonpath + check mypy_path lists `packages/va_events/tests/support`).
- Do NOT move `destinations.py` / `signing_secrets.py` (dispatcher-only caching); the api uses
  `active_signing_secrets` directly and its own URL check.

### Files
- `packages/va_db/src/va_db/repositories/webhooks.py` (NEW, export from `va_db.repositories`):
  - `WebhookEndpointRepo(TenantRepository[WebhookEndpoint])` `__init__(session, org_id, *, clock=None)`;
    `all() -> list[WebhookEndpoint]` (created_at, id ascending); `count() -> int`;
    `delete(endpoint_id) -> int` (get → NOT_FOUND; cancel pending/delivering rows UPDATE ... RETURNING count; DELETE;
    returns cancelled count).
  - `WebhookDeliveryFilter` frozen dataclass (`status, event_type, call_id, endpoint_id`, `.conditions()`);
    `WebhookDeliveryRepo(TenantRepository[WebhookDelivery])`: `page(filters, *, limit, cursor) -> Page`,
    `replay(delivery_id, *, now) -> WebhookDelivery` (raises NotFoundError / `AppError("CONFLICT", ...)` per (6);
    read the row first (`get`) to choose the refusal, check the endpoint's enabled flag, then the conditional UPDATE;
    a lost race (UPDATE matched nothing) → CONFLICT "already queued").
  - `ENDPOINT_DELETED_ERROR: Final = "endpoint_deleted"` here too? NO: import the constant from one place — put it in
    `va_db.repositories.webhooks` (va_db cannot import va_events) and have `va_events.webhook_delivery.ENDPOINT_DELETED`
    re-export it.
- `packages/va_db/src/va_db/repositories/outbox.py`: `_finish` condition + `DeliveryCancelledError`.
- `apps/webhook_dispatcher/src/va_webhooks/dispatcher.py`: catch `DeliveryCancelledError` in `_record`.
- `packages/va_db/src/va_db/audit.py` + specs/04 §13: `webhook_delivery.replay`.
- `apps/api/src/va_api/services/webhook_policy.py`: generalise to `check_webhook_url(url, *, org, settings, resolver,
  field)` with messages built from `field` (`"{field} must use https."` ...); `check_call_webhook` keeps its
  signature (field `webhook`, same messages as today) — existing tests must pass unchanged.
- NEW `apps/api/src/va_api/services/webhook_test_send.py`: `async def send_test_event(*, database, envelope,
  deliverer, org_settings: OrgSettings, settings, resolver, org_id, endpoint_url, event_type, clock) ->
  SendResult + event_id` (pure-ish orchestration of (4)); keep the router thin.
- `apps/api/src/va_api/container.py`: field `webhook_deliverer: Deliverer` built from
  `safe_client(settings, timeout=TEST_SEND_TIMEOUT_S, allow_private=settings.webhook_allow_private,
  resolver=system_resolver, version=...)` (aclose on exit via stack) + `Deliverer(http, clock=clock,
  timeouts=Timeouts(10.0, 10.0))`; accessor `session_auth.webhook_deliverer_of(request)` (pattern of the `_of` helpers;
  the fake container uses `getattr` the same way the others do).
- `apps/api/src/va_api/routers/webhooks.py`: 7 new routes (operation ids `webhooks_endpoints_list`,
  `webhooks_endpoints_create` (201), `webhooks_endpoints_update`, `webhooks_endpoints_delete`,
  `webhooks_endpoints_test`, `webhooks_deliveries_list`, `webhooks_deliveries_replay`); path params `endpoint_id: uuid.UUID`,
  `delivery_id: uuid.UUID`; literal `/deliveries` and `/endpoints` paths before any `{id}` route. Update the module
  docstring. If the file grows past ~250 lines, put endpoints/deliveries in `routers/webhook_endpoints.py` attaching
  to `webhooks_router` and add the side-effect import in `main.py` (AGENTS.md P02-T05 gotcha).
- `apps/api/src/va_api/routers/webhooks_schemas.py`: `WebhookEndpointCreate`, `WebhookEndpointPatch`,
  `WebhookEndpointView`, `WebhookEndpointDeleted`, `WebhookTestRequest`, `WebhookTestResult`, `WebhookDeliveryView`
  (each with an `examples` entry; endpoint example URL `https://hooks.example.com/voiceagents` is fine in tests/openapi
  but `check_no_placeholders` flags `example.com` in production code — use `https://hooks.acme.test/voiceagents`
  or similar non-example.com host) + view builders; event_types validator in the schema module (uses
  `va_events.EventType`).
- `make openapi` + `corepack pnpm --filter dashboard gen:api` + `corepack pnpm --filter @voiceagents/sdk gen:api`.
- specs/19 §10 Decision (above) and §8 code list mention `endpoint_deleted`; specs/04 §13 action; AGENTS.md bullet.

### Tests
- `packages/va_db/tests/test_webhook_repos_db.py` (migrated module DB as other repo tests): endpoint all/count;
  delete cancels pending+delivering rows of that endpoint only (succeeded/dead and other endpoints untouched; returns
  2), FK nulls the rest; delivery page filters (status/event_type/call_id/endpoint_id) + cursor paging + RLS (other
  org's rows invisible); replay from dead/succeeded/failed resets fields and keeps event_id/url/payload; replay of
  pending/delivering → CONFLICT; endpoint_deleted → CONFLICT; disabled endpoint → CONFLICT; other org → NOT_FOUND.
  Outbox: `_finish` on a non-delivering row raises `DeliveryCancelledError` (mark_succeeded/schedule_retry/mark_dead).
- `apps/webhook_dispatcher/tests/test_dispatcher_db.py`: a row cancelled (status → dead endpoint_deleted) while its
  attempt is gated in the receiver stays dead with `endpoint_deleted` after release, no call event row, log
  `webhook.delivery_cancelled`. Existing dispatcher tests pass with moved imports.
- `packages/va_events/tests/test_webhook_signing.py` / `test_webhook_schedule.py` (moved, unchanged asserts).
- `apps/api/tests/test_webhook_endpoints.py` (session_helpers.migrated_postgres, `SessionContainer` with
  `FrozenClock`, `host_resolver.answers["hooks.test"] = ["127.0.0.1"]`, real `webhook_receiver.Receiver` on port 0):
  create (201 view, defaults `["*"]`, audit row), list oldest first, PATCH subset + audit before/after, PATCH no-op →
  no audit row, delete → `{deleted:true}` + pending rows dead `endpoint_deleted` + audit; URL rejections (https
  required when `allow_insecure_webhooks` false in a non-local env — build settings with `va_env=staging`? if the
  api refuses staging settings in tests, test `check_webhook_url` directly; blocked address with
  `webhook_allow_private=False`) → `WEBHOOK_URL_REJECTED` field `url`; event_types validation (`foo.bar`,
  `call.*` ok, `nope.*` rejected, empty list, duplicates) with `field` paths; 21st endpoint → INVALID_INPUT;
  phi_allowed true by developer → 403, by owner → ok; RBAC: developer write ok, analyst/viewer GET ok + POST 403,
  billing GET 403; API key with `webhooks:read` only → POST 403.
  Test send: receiver gets one POST with envelope body, `X-VA-Signature` verifies with the org secret
  (`webhook_receiver.signature_ok`), result `succeeded: true, status_code: 200`; receiver replying 500 →
  `succeeded: false, error: "http_status", status_code: 500`; bad event_type → INVALID_INPUT; disabled endpoint still
  sends; no `webhook_deliveries` row created; blocked URL (resolver answer `10.0.0.1` with allow_private False, or
  metadata `169.254.169.254` which stays blocked even with allow_private) → `error: "blocked_destination:address"` and
  the receiver got nothing.
  Deliveries: seed rows (via `RowFactory`/direct inserts in a tenant session) → list newest first, filters, cursor,
  `limit=101` → INVALID_INPUT field `limit`, payload absent from items, `next_attempt_at` null for non-pending;
  replay dead → 200 pending attempts 0 + audit `webhook_delivery.replay`; replay pending → 409; other org → 404.
- Cross-tenant: factories `endpoint_id` (seed endpoint url `http://127.0.0.1:9/hook` — IP literal, no DNS; test env
  allows http + private, refused port → quick `connection` result for the owner control of test send) and
  `delivery_id` (seed a `dead` delivery so the replay owner control is 200) in
  `apps/api/tests/support/cross_tenant_factories.py`; run `test_cross_tenant_routes.py`, `test_audit_markers.py`,
  `test_openapi.py`, operations contract, `test_rbac_matrix.py`.
- `packages/va_db` audit unit test (spec list == AUDIT_ACTIONS) passes with the new action.

### Gates
`export PATH=/home/ssm-user/.local/bin:/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
`uv run pytest packages/va_db packages/va_events apps/api apps/webhook_dispatcher -q`; `make openapi` + gen:api +
`make test-ts`; full `make test` in background with heartbeat Monitor (truncate the log first); `make check`;
jscpd (new api tests tend to clone helpers: reuse `call_helpers`/`session_helpers`); radon. No e2e (no call flow).

### Risks
- Cross-tenant suite performs real outbound requests for test send: make sure the factory URL never leaves the host
  (IP literal 127.0.0.1 closed port) and the api test settings keep `webhook_allow_private=True` (default, local/test).
- Moving modules: keep `uv.lock` consistent (`uv lock`), mypy paths unchanged (va_events src already listed).
- `example.com` in production schemas trips the placeholder gate; gitleaks unaffected (no secrets in examples).
- DeliveryCancelledError change touches the dispatcher hot path: existing dispatcher DB tests must stay green.

## Build P06-T04b (builder notes)
- Moved signing/schedule/delivery to `va_events.webhook_{signing,schedule,delivery}` (+ tests, receiver to
  va_events tests/support); va-events deps httpx/orjson/opentelemetry-api (uv.lock: metadata only).
- `va_db.repositories.webhooks`: `WebhookEndpointRepo` (all/count/delete cancelling queued rows),
  `WebhookDeliveryFilter`, `WebhookDeliveryRepo` (page/replay); `ENDPOINT_DELETED_ERROR` re-exported as
  `va_events.webhook_delivery.ENDPOINT_DELETED`.
- `OutboxRepo._finish` only on `delivering` rows → `DeliveryCancelledError`; dispatcher `_record` catches it, logs
  `webhook.delivery_cancelled`, records nothing. Existing outbox test adjusted (mark_dead after schedule_retry now
  needs a re-claim; split into separate rows) + new parametrized test.
- API: `routers/webhook_endpoints.py` (7 routes, side-effect import in main), schemas in
  `routers/webhook_endpoints_schemas.py`, `services/webhook_test_send.py`, `webhook_policy.check_webhook_url`
  (check_call_webhook unchanged behaviour), container `webhook_deliverer`; audit action `webhook_delivery.replay`
  (specs/04 §13 + AUDIT_ACTIONS). Cross-tenant factories `endpoint_id`/`delivery_id`. openapi + gen:api regenerated.
- va_db conftest gained `tenant_db`/`fresh_org` fixtures (jscpd clones otherwise).
- Deviations from plan: phi refusal is `FORBIDDEN` (what require_permission raises); test-send returns 404 for unknown
  endpoint; list `status` query typed as the status Literal (bad value → INVALID_INPUT field status).

## Review P06-T04b — approved
- Re-ran: lint/typecheck/placeholders pass; make test 11025 passed, 7 skipped, coverage 99.07 %; make test-ts pass
  (122); openapi drift current; jscpd 0 clones.
- Note (non-blocking): the 20-endpoint cap is a count-then-insert without an org lock, so two concurrent creates
  at 19 could both pass; acceptable as a soft limit, could take `OrgRepo.lock` later if it matters.

## Plan P06-T05 — Events sink to ClickHouse

### Decisions (add `> Decision (2026-11-03): events sink (P06-T05).` under specs/19 §1 after the P03-T17 decision; cross-ref from specs/03 §12)
1. One consumer group `events-sink` over all five streams (`va_events.catalog.STREAMS`), consumer name =
   `va_common.tracing.resolve_pod()` (POD_ID or host name). One `StreamConsumer` + loop task per stream (supervised).
2. Batching per stream: read with `count = 1000 - pending`, block for the rest of the 1 s window (min 1 ms); flush when
   1 000 entries are pending or 1 s (injected `Clock`) passed since the first pending entry; ack only after every
   table insert of the batch succeeded (at-least-once; a crash between insert and ack can duplicate rows in the
   MergeTree tables — `calls_fact` is ReplacingMergeTree so it self-heals; documented, no dedup token).
3. Mapping (pure, `va_events_sink.rows`):
   - `va.call_events`: every event with `call_id`, `seq`, and a category (`event.category` or `DEFAULT_CATEGORY`)
     → `(org_id, call_id, seq, category, level, message, data=orjson text of data, created_at=occurred_at)`.
     Call events without seq or category produce no row (same rule as Postgres `row_for`; reuse `va_events.call_events.row_for`
     if it fits, else mirror it).
   - `va.call_turns`: `call.turn` events → `(org_id, call_id, turn_index, role, node_id or "", text_len=len(text),
     interrupted 0/1, eot_ms, stt_final_ms, llm_ttft_ms, tts_ttfa_ms, e2e_ms, route_ms, tool_ms, created_at)`; latency
     columns come from `data["latency"]` (object with the `METRIC_KEYS` subset) when present, else NULL.
   - `va.calls_fact`: `call.completed` (data = Call Object) → parse with `va_domain.call_object.CallObject`
     (check its model_config; if extra keys are rejected that is fine because the data IS a dumped CallObject,
     `by_alias=True` → validate with `by_alias`/`populate_by_name` as the model allows). direction, status (Bland form, as
     analytics expects `completed`), answered_by or "", runner_type = agent if agent_id, persona if persona_id,
     pathway if pathway_id, else task; pathway_id, agent_id, experiment_arm_id NULL (the Call Object carries the arm
     label only), batch_id, disposition_tag or "", end_reason or "", connected_seconds = round(call_length*60, 3),
     transfer_seconds = round(transfer_duration*60, 3), price_micros = round(price*1_000_000), created_at, ended_at=end_at.
   - `va.usage_events_raw`: NEW type `billing.usage` (stream `billing`, category —, webhook key —) whose data is
     `va_events.usage.UsageEventData` (meter, quantity: Decimal-as-string/number → float, amount_micros, cost_micros,
     reference_type, reference_id, occurred_at, idempotency_key); builder `usage_event(row_values, *, org_id, clock)`.
     Producer = the phase-15 `events:billing` mirror (specs/24 §4 step 4 stays deferred; update the P05-T18a
     decision text to name `billing.usage` as the contract). Never routed to webhooks: add `INTERNAL_TYPES =
     frozenset({EventType.BILLING_USAGE})` to `va_events.catalog` and make `webhook_routing.plan_deliveries` return
     [] for them (org endpoints with `*` must not get it). Add the §2 catalog row:
     `| billing.usage | — | worker | a usage_events row was posted (data: meter, quantity, amount_micros, cost_micros, reference_type, reference_id, occurred_at, idempotency_key; internal, ClickHouse mirror) | — (never delivered) |`
     (check `test_catalog` parsing of the last column; it must map to None). Run `make openapi` + both gen:api in case
     an EventType enum appears in openapi.
   - Malformed data of a mapped type (missing/wrong-typed keys, CallObject validation error) → `RowError` → the
     entry is parked (below), never blocks the batch.
4. Assistant turn latency: the per-reply latency report arrives at the END of the reply (`CallPipeline._reply`
   finally → `on_latency`), after the agent turn callback. `va_media.turns.TurnRecorder` holds the most recent
   assistant `call.turn` event (not yet published) and publishes it: (a) on `on_latency` with
   `data["latency"] = report.latency.as_dict()` attached when non-empty (then the latency events follow), or (b)
   unattached as soon as any other callback (user turn, next agent turn, interrupt) or `drain()` happens. Order in the
   chain stays callback order otherwise. Document under specs/19 §2 (call.turn data gains optional `latency` on
   assistant turns) — analytics `/v1/analytics/latency` reads `role='assistant'` rows (specs/27 §7), so latency lives
   on the assistant row. User rows carry NULL latency in ClickHouse (Postgres keeps merging into the user row as today).
5. Failures:
   - Insert errors that are transient (`clickhouse_connect...OperationalError`, `OSError`): keep the batch, retry after
     0.5 s doubling to 5 s on the clock, `va_events_sink_errors_total{table}` +1 per failed table insert, log
     `events_sink.insert_retry` (table, rows, attempt, error type). No ack until success.
   - Non-transient insert errors (`DatabaseError` not `OperationalError`, `ValueError`/`TypeError` from the driver):
     fall back to inserting each event's rows alone; an event whose insert still fails non-transiently is parked
     with reason `insert_failed:<table>`; then ack the batch.
   - Parking = `XADD events:dead MAXLEN ~ 100000 * stream <events:x> entry_id <id> reason <reason> payload <bytes>`
     then ack. `StreamConsumer` gets an optional `dead_letter: DeadLetter | None` (new `va_events.dead_letter.DeadLetter(redis)`
     with `park(stream_key, entry_id, payload, reason)`); with it set, undecodable payloads (today: ack + log only) are
     parked with reason `undecodable` before the ack (raw payload bytes, or empty when the field is missing).
     Without it behaviour is unchanged (existing tests). Key constant `DEAD_STREAM_KEY = "events:dead"` in
     `va_events.keys`; add the row to specs/03 §11 (`events:dead` | stream (MAXLEN ~ 100 000) | — | entries the events
     sink could not map or insert). If XADD to dead fails (Redis down) the entry is NOT acked (re-read later).
6. Lag gauge `va_events_sink_lag_seconds{stream}`: refreshed after every read/flush cycle: age (clock wall now − ms of
   the entry id) of the oldest of {group's oldest pending entry (`XPENDING` summary min id), first undelivered entry
   (`XRANGE (<last-delivered-id> + COUNT 1`, last-delivered from `XINFO GROUPS`)}; 0 when neither exists. Put the pure
   id→ms/age calculation in a function with unit tests. `va_events_sink_inserted_total{table}` += rows per successful insert.
7. Startup order: the sink task waits for `SchemaState.wait()`, then opens a client via the same `SchemaConnect`
   (retrying transient connect errors with the bootstrap backoff), `ensure_group()` per stream, then runs the stream
   loops. Shutdown (supervisor cancel): stop reading, try a final flush bounded by nothing extra (cancellation just
   drops the in-memory batch — entries remain pending and are re-read via XAUTOCLAIM / own pending; document).
   Simplest correct restart behaviour: on start each stream loop first re-reads its OWN pending entries
   (`XREADGROUP ... 0`) before `>` so a restarted pod with the same name does not wait 60 s. Add
   `StreamConsumer.read_own_pending(count)` for that.

### Files
- `packages/va_events/src/va_events/catalog.py`: `BILLING_USAGE = "billing.usage"`, `INTERNAL_TYPES`, `__all__`.
- `packages/va_events/src/va_events/usage.py` (NEW): `UsageEventData` (pydantic, frozen, extra forbid; `quantity: Decimal`),
  `usage_event(data: UsageEventData, *, org_id, clock) -> DomainEvent` (no call_id, so no seq/live/row), export in `__init__`.
- `packages/va_events/src/va_events/webhook_routing.py`: early `return []` for `INTERNAL_TYPES`.
- `packages/va_events/src/va_events/keys.py`: `DEAD_STREAM_KEY`; `dead_letter.py` (NEW) `DeadLetter`.
- `packages/va_events/src/va_events/consumer.py`: `dead_letter=` param, `read_own_pending`, parking of poison.
- `apps/events_sink/src/va_events_sink/rows.py` (NEW, pure): `TABLE_COLUMNS: Final[dict[str, tuple[str, ...]]]` (§12
  column order), `TableRows` (dict table → list[tuple]), `rows_for_event(event) -> TableRows` raising `RowError(reason)`.
- `apps/events_sink/src/va_events_sink/writer.py` (NEW): `ClickHouseInserts` protocol (`insert(table, data,
  column_names=..., database="va")` = clickhouse-connect `AsyncClient.insert` signature subset), `insert_rows(client,
  rows, metrics)`; transient/fatal classification helpers.
- `apps/events_sink/src/va_events_sink/lag.py` (NEW): `entry_ms(entry_id)`, `lag_seconds(oldest_ids, now)`, `stream_lag(redis, key, group, now)`.
- `apps/events_sink/src/va_events_sink/sink.py` (NEW): `SinkConfig(batch_rows=1000, batch_interval_s=1.0, group="events-sink",
  consumer=..., streams=STREAMS)`, `StreamSink` (one stream: read → batch → map → insert → ack/park), `run_sink(redis,
  connect, schema, *, clock, metrics, config)`.
- `apps/events_sink/src/va_events_sink/main.py`: supervisor starts `"events_sink"` task after schema; `create_app` seam
  `sink: SinkFactory | None` (default builds from `container.redis` (container must expose `redis` — make
  `build_container` keep the client: `deps.redis(settings)` returns it; check how other services expose it on the
  container, e.g. `Dependencies` attribute or a small dataclass) and the schema connect); tests that use
  `FakeContainer` pass `sink=None`. `SchemaConnect` type widened to a client protocol with both `command/query` and `insert`.
  Update module docstrings ("The stream consumer comes with the analytics work" is now false).
- `apps/media_worker/src/va_media/turns.py`: hold/attach assistant turn latency (Decision 4).
- specs: 19 §1 Decision, §2 row + call.turn note; 03 §11 row + §12 cross-ref; 24 §4 P05-T18a decision text; AGENTS.md bullet.

### Tests
- `apps/events_sink/tests/test_rows.py`: call_events row for a call event (data JSON text, seq, category default and
  override), no row without seq / without category / without call_id; call.turn user row (NULL latency, node "" for
  null, text_len), assistant row with latency subset; malformed call.turn (missing turn_index, role not str) → RowError;
  call.completed with a real dumped CallObject (build from `CallObject` example / va_domain test fixture) → calls_fact
  values incl. runner_type precedence (parametrize agent/persona/pathway/task), seconds/micros conversion;
  invalid Call Object → RowError; billing.usage row; non-mapped system event → empty. Column tuple order equals
  `ch_support.spec_columns()` for each table.
- `packages/va_events/tests/test_usage.py`, routing test: `billing.usage` with a `*` endpoint plans nothing;
  catalog test passes with the new row.
- `packages/va_events/tests/test_consumer.py`: with `dead_letter`, a bad payload lands in `events:dead` with
  stream/entry_id/reason/payload and is acked; dead-letter XADD failure leaves it pending; `read_own_pending` returns
  entries delivered but unacked to this consumer.
- `apps/events_sink/tests/test_sink.py` (fakeredis + a `FakeInserts` recording client, ManualClock): flush at 1 000
  entries without waiting; flush after 1 s with fewer; ack only after insert; transient failure → retry with backoff
  (0.5, 1.0 …) then success, errors_total incremented, nothing acked in between; non-transient batch failure →
  per-event fallback parks only the bad event (reason `insert_failed:call_turns`) and inserts the rest; RowError →
  parked `unmappable:<reason>`; inserted_total per table; restart re-reads own pending; lag gauge values.
- `apps/events_sink/tests/test_lag.py` pure id/age math.
- `apps/events_sink/tests/test_events_sink.py`: existing probe tests with `sink=None`; one lifespan test where the
  default sink factory runs over fakeredis + FakeConnect client and inserts a published event.
- `apps/events_sink/tests/test_sink_integration.py` (`@pytest.mark.integration`, docker probe skip): testcontainers
  Redis (`testcontainers.community.redis`? check availability; else reuse existing redis container helper used by
  `test_events_redis_integration.py`) + ClickHouse 24.8 container (one module, one test): apply migrations, publish
  via real `EventPublisher` a call.turn (assistant with latency), a call.latency, a call.completed CallObject, a
  billing.usage, and one poison payload; run the sink until rows appear (poll ClickHouse with a bounded loop on the
  clock-free condition — query counts, no sleeps beyond the loop's await of the sink flush event; expose a
  `flushed` asyncio.Event/counter on StreamSink for tests); assert row values in all four tables and the poison in
  `events:dead`, and XPENDING 0.
- `apps/media_worker/tests/test_turns.py` (existing module): assistant turn event published with `latency` when
  `on_latency` follows; published without latency when a user turn / drain comes first; order of events.
  Runtime sim / e2e assertions that read call.turn data must still pass.

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
`uv run pytest packages/va_events apps/events_sink apps/media_worker packages/va_db -q`; `make openapi` (+gen:api if drift);
full `make test` in background with heartbeat Monitor (truncate log first); `make check`; jscpd; radon.
No e2e needed (no call-flow behaviour change beyond event data), but the media TurnRecorder change touches call
events — run `uv run pytest apps/media_worker -q` fully.

### Risks
- clickhouse-connect `AsyncClient.insert` signature: `insert(table, data, column_names='*', database='', ...)`;
  UUID/Nullable/DateTime64 conversions — pass `uuid.UUID`, `datetime` (aware UTC), `None`; integration test proves it.
- fakeredis `XINFO GROUPS` / `XPENDING` summary support (used for lag) — verify early; fall back to `xpending_range`.
- Holding the assistant event reorders it after `call.latency` events; check e2e/runtime tests that assert event order
  (`tests_e2e` outbound_basic assertions on event sequence, `test_runtime_sim`) — grep for `call.turn` order asserts.
- Testcontainers boot time under xdist: one test per module.

## Build P06-T05 (builder notes)
- va_events: `BILLING_USAGE` + `INTERNAL_TYPES` (plan_targets returns [] for them), `usage.py` (`UsageEventData`,
  `usage_event`), `dead_letter.py` (`DeadLetter.park`), keys `DEAD_STREAM_KEY`/`DEAD_STREAM_MAXLEN`,
  `StreamConsumer(dead_letter=)` parks `undecodable` before ack + `read_own_pending()` (deleted entries acked away).
- events_sink: `rows.py` (pure mapping, `RowError`), `writer.py` (`insert_table`, `is_transient`, `is_rejected`,
  `ClickHouseInserts`), `lag.py`, `sink.py` (`StreamSink.cycle/run`, `build_sinks`, `run_sink`), container now
  `SinkContainer(dependencies, redis)`, `create_app(sink=default_sink|None)`; va-domain dependency added.
- Transient insert retries only the failing table (tables already inserted in the batch are not re-sent); rejected
  table → per-event inserts for the remaining tables only.
- Redis errors in the loop (incl. failed park) → drop batch, backoff, restart with own-pending re-read.
- fakeredis blocking XREADGROUP returns [] immediately → sink yields once after an empty read (else loop starves).
- media TurnRecorder holds the assistant call.turn; on_latency attaches `data.latency`.
- Specs: 19 §1 Decision P06-T05 + §2 rows (call.turn note, billing.usage), 03 §11 `events:dead` row + §12 pointer,
  24 P05-T18a text. AGENTS.md bullet. openapi: no drift.
- Integration test (real Redis + ClickHouse 24.8) passes locally (~7 s).

## Review P06-T05 — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11100 passed, 7 skipped (coverage gate passed); jscpd 0 clones; radon avg 2.39.
- Diff checked against specs/03 §11/§12 (column order, types: UInt32 latencies clamped ≥ 0, Nullable UUIDs), specs/27 metric names, specs/19 §1 Decision + §2 rows.
- Non-blocking notes: `_recover` stops when a whole own-pending batch is poison/deleted even if more own entries remain
  (those fall back to XAUTOCLAIM after 60 s); a dead-letter park failure midway through `_decode` can park earlier
  entries twice on re-read. Both acceptable for at-least-once.

## Plan P06-T06a — Call event SSE stream
P06-T06 was split in plan/phase-06.md into T06a (call SSE), T06b (transcript token + WS), T06c (org stream).
T06a also lays the shared pieces (live hub, row body) that T06b/T06c reuse.

### Decisions (add `> Decision (2026-11-03): call event stream (P06-T06a).` under specs/19 §9)
1. `call_events` gains a nullable `event_type text` column (migration `0007_call_event_type.py`:
   `ALTER TABLE call_events ADD COLUMN event_type text`; partitioned parent → propagates to partitions; downgrade
   drops it). specs/03 DDL lists it LAST (after `created_at`, before the PRIMARY KEY line; `test_schema_matches_spec`
   compares attnum). `CallEventRow.event_type: str | None = None` (last field, default) set by `row_for` to
   `event.type.value`; `insert_rows`/`DbCallEventWriter` write it; model `CallEvent.event_type` (`text_column`-style,
   nullable). Rows written before 0007 have NULL → the SSE frame omits the `event:` line (EventSource default
   `message`). Check `va_events_sink.rows` builds its tuple field by field (not `astuple`) so ClickHouse is unchanged.
2. Route `GET /v1/calls/{call_id}/events/stream` on `calls_router` (legacy family; declare it next to
   `/{call_id}/events`), `calls:read`, `operation_id="calls_events_stream"`, `openapi_extra={"x-scope": "calls:read"}`,
   `response_class=StreamingResponse`, `responses={200: {"description": ..., "content": {"text/event-stream": {}}}}`.
   Errors (unknown/other-org call `CALL_NOT_FOUND`, bad `Last-Event-ID` `INVALID_INPUT` field `Last-Event-ID`,
   hub unavailable `SERVICE_UNAVAILABLE`) are raised BEFORE the response starts (ownership check + subscription happen
   in the route, the generator only streams). Headers: `Content-Type: text/event-stream; charset=utf-8`,
   `Cache-Control: no-store`, `X-Accel-Buffering: no`. `test_openapi.test_operations_contract` needs a
   `STREAM_OPERATIONS = {"calls_events_stream"}` exemption (200 with `text/event-stream`, no JSON schema) like
   `REDIRECT_OPERATIONS`; extend the cross-tenant suite if it reads the body (it only needs the 404 for org B; the
   owner control must not hang: either register `REGISTRY.register_owner_status` or make the factory's call already
   completed > 30 s ago so the stream ends right after replay — prefer the latter; check what the walker does with a
   2xx streaming body over ASGITransport, which buffers until the generator ends).
3. Frame format: `id: <seq>\nevent: <type>\ndata: <orjson bland-live body>\n\n` (single-line JSON). Body =
   `va_events.webhook_payloads.live_body(event, metadata)` for live events and a new
   `row_body(row: CallEvent, metadata) -> Body` next to it (same keys/format: `call_id`, `category`, `level`,
   `message`, `data`, `seq`, `created_at` iso_ms, `metadata`) for replayed rows; `metadata` = the call's
   `metadata_` loaded once at start. First frame `retry: 3000`. Heartbeat comment `: ping\n\n` every 15 s of
   clock time without a frame.
4. Only events that have a `call_events` row are streamed (`row_for(event) is not None`), so replay and tail show the
   same set; live events without a row still count as "seen" for gap tracking.
5. Order and boundary: the route subscribes `call:{<id>}:live` FIRST (hub confirms), then replays rows with
   `seq > after` (`after` = `Last-Event-ID` int ≥ 0, else 0) in pages of 500 via `load_call_events`, then tails.
   Frames go out strictly in increasing seq. Pure helper `va_api.services.call_stream_order.SeqOrder(after)`:
   `offer(seq, item)` → items now releasable in order (contiguous from `last + 1`), duplicates/`seq <= last`
   dropped; `waiting_since`/`has_gap`. When a live event leaves a gap (missing seqs below it: published before the
   subscription but not yet flushed by the media write-behind buffer, or concurrent publishers arriving out of
   order) the stream holds it up to `GAP_HOLD_S = 2.0` (clock): live arrivals may fill the gap; at the deadline it
   re-reads `call_events` for `last < seq < min_pending`, releases what it found, then skips the remaining holes and
   releases the held events.
6. Close: after a `call.completed` frame (replayed or live) the stream ends at `completed.created_at + 30 s` on the
   clock (immediately when that is already past, e.g. an old call). Hard cap `MAX_STREAM_S = 4 h` per connection
   (clients resume with `Last-Event-ID`). Hub overflow (subscriber queue > 1 000) or hub connection loss ends the
   stream at once (client resumes). Client disconnect cancels the generator; the subscription is released in
   `finally`. Log `calls.event_stream_closed` (call_id, reason, frames).
7. Pod-wide hub `va_events.live_hub.LiveHub(redis, clock, *, confirm_timeout_s=2.0, queue_size=1000)`: ONE pub/sub
   connection per api process (a pubsub per SSE client would exhaust the 50-connection pool). `subscribe(channel)
   -> LiveSubscription` (ref-counted per channel: first subscriber SUBSCRIBEs and waits for the confirmation, later
   ones join; raises `LiveHubError` on Redis error/timeout), `LiveSubscription.next() -> bytes` (raises
   `LiveSubscriptionClosed(reason)` with reason `overflow`/`lost`/`closed`), `await hub.unsubscribe(sub)` (last one
   UNSUBSCRIBEs). The reader task starts lazily with the first subscription (owned by the hub, not a supervisor:
   the api has none) and is cancelled by `aclose()`; a reader RedisError closes every subscription with `lost`,
   resets the connection, and the next subscribe starts fresh. Container field `live_hub` built in
   `va_api.container.build_container` and `aclose`d on exit (exit stack); `SessionContainer` gets one over its
   fakeredis. Do not copy `va_media.channels.ChannelHub` text (jscpd); write it for multi-subscriber fan-out.
8. Spec text: specs/19 §9 Decision (above points condensed), specs/03 §6 DDL `event_type` + note, specs/05 table
   row unchanged. AGENTS.md bullet.

### Files
- `packages/va_db/src/va_db/migrations/versions/0007_call_event_type.py` (NEW), `va_db/models/calls.py` (`event_type`).
- `specs/03-data-model.md` (DDL line + short Decision), `specs/19-events-and-webhooks.md` §9 Decision.
- `packages/va_events/src/va_events/call_events.py` (`CallEventRow.event_type`, `row_for`), `webhook_payloads.py`
  (`row_body`), `live_hub.py` (NEW), `__init__.py` exports.
- `apps/api/src/va_api/services/call_stream.py` (NEW): `StreamDeps(database, live_hub, clock)`, `stream_deps_of(request)`,
  `open_call_stream(deps, ctx, call_id, last_event_id: str | None) -> CallStream` (ownership + parse + subscribe;
  errors raised here), `CallStream.frames() -> AsyncIterator[bytes]` (replay, tail, gap fill, heartbeat, close,
  `finally` unsubscribe), constants `HEARTBEAT_S=15`, `CLOSE_AFTER_COMPLETED_S=30`, `GAP_HOLD_S=2`, `MAX_STREAM_S=14400`,
  `REPLAY_PAGE=500`. Pure SSE formatting `sse_frame(seq, event_type, body) -> bytes` (could live in
  `va_api/sse.py` for T06c reuse) and `call_stream_order.SeqOrder`.
- `apps/api/src/va_api/routers/calls.py` route + docstring; `container.py` field; tests support `SessionContainer`.
- `apps/api/tests/test_openapi.py` exemption; `apps/api/tests/support/cross_tenant_factories.py` if needed.

### Tests
- va_db: migration up/down (column exists/nullable, offline SQL), schema-vs-spec + autogenerate parity still pass.
- va_events `test_call_events.py`: `row_for` sets `event_type`; DB insert round-trips it. `test_webhook_payloads`:
  `row_body` equals `live_body` for the same event (row built via `row_for`, inserted, read back).
- va_events `test_live_hub.py` (fakeredis): two subscribers of one channel both get a published message; channel
  subscribed once (PUBSUB NUMSUB 1) and unsubscribed after the last leaves; confirm timeout → `LiveHubError`
  (fake that never confirms, ManualClock); overflow → `overflow`; reader Redis failure → `lost` for all + next subscribe works;
  `aclose` closes subscriptions with `closed` and stops the task.
- api `test_call_stream_order.py` pure: in-order release, duplicate drop, gap hold, `after` floor.
- api `test_call_stream.py` (module Postgres via `session_helpers.migrated_postgres()` + fakeredis + ManualClock
  driving `CallStream.frames()` directly): replay in seq order with exact frame bytes (`id`, `event`, body keys,
  metadata); `Last-Event-ID: 3` replays only 4..; replay→tail boundary: an event both in DB and on live is sent once;
  a live event published before subscribe and inserted later is recovered by the gap re-read after `GAP_HOLD_S`;
  out-of-order live arrivals (seq 6 then 5) go out 5,6; rowless live event (no category) is not sent but closes the
  gap; heartbeat after 15 s idle; `call.completed` → ends 30 s later; old completed call ends right after replay;
  overflow/lost ends the stream; unknown call / other org → `CALL_NOT_FOUND`; `Last-Event-ID: abc` / `-1` →
  `INVALID_INPUT`; NULL `event_type` row → frame without `event:`.
- api HTTP test over a real socket (`service_helpers.RecordingServer` serving `session_app(container)`, httpx
  `client.stream`): status 200, content type, `Cache-Control`, `X-Request-Id`, first frames, `Last-Event-ID` header honoured,
  client disconnect releases the hub subscription (hub channel count back to 0, waited on an event, no sleeps).
  Note httpx `ASGITransport` buffers the whole body, so it can only be used for streams that end (completed call).
- cross-tenant suite passes (new route probed).

### Gates
`export PATH=/home/ssm-user/.local/bin:/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
`uv run pytest packages/va_db packages/va_events apps/api apps/events_sink -q`; `make openapi` + both `gen:api`
(new route → schema.d.ts drift; run `make test-ts`); full `make test` in background with heartbeat Monitor
(truncate log first); jscpd; radon. No call-flow behaviour change → no e2e (the media write path only adds a column value).

### Risks
- Middleware (`IdempotencyMiddleware`, `RateLimitHeadersMiddleware`, `MetricsMiddleware`) must pass streamed body
  chunks through without buffering; verify with the real-socket test (first frame arrives while the stream is open).
- StreamingResponse disconnect handling: Starlette cancels the body iterator on `http.disconnect`; make sure the
  `finally` runs (unsubscribe awaited inside it with shielding if needed).
- FrozenClock.sleep advances instantly → never use the default SessionContainer FrozenClock for streams that idle;
  use ManualClock (`wait_for_sleep` / `run_for`). Seeded `call_events` rows need dates inside the migration
  partitions (`call_helpers.business_hours_today()`), so `ManualClock().set(...)` accordingly.
- Adding a field to `CallEventRow` may break tests constructing it positionally or comparing `asdict`; grep tests.

## Build P06-T06a (builder notes)
- Migration 0007 `call_events.event_type` (nullable, last column; spec DDL + comment line), model column,
  `CallEventRow.event_type` set by `row_for`, `row_body(row, metadata)` == `live_body(event, metadata)`.
- `va_events.live_hub.LiveHub` (one pubsub per process, ref-counted channels, lazy reader that exits when nothing is
  subscribed, overflow/lost/closed reasons). Exception renamed `LiveSubscriptionClosedError` (ruff N818).
- api: `services/call_stream_order.SeqOrder` (offer/advance), `va_api/sse.py` (`sse_frame`, `retry_frame`,
  `EventStreamResponse(frames, on_close)` runs cleanup however the response ends; needs `status_code` init param for
  FastAPI OpenAPI), `services/call_stream` (open_call_stream: ownership → Last-Event-ID → subscribe; frames: retry,
  replay pages of 500, tail with 2 s gap hold + DB re-read, ping 15 s, close 30 s after call.completed, 4 h cap).
  Route `calls_events_stream`; container `live_hub` (aclose on exit); SessionContainer hub on SystemClock.
- Cross-tenant: `register_request(..., headers=)`; stream owner control uses bad Last-Event-ID → 400.
- test_openapi `STREAM_OPERATIONS`. openapi.json + both schema.d.ts regenerated.
- Replay holes (rowless seqs) are skipped silently: only live gaps are held.

## Review P06-T06a — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11146 passed, 7 skipped (99.04 %); jscpd 0 clones; radon avg 2.98.
- Checked against specs/19 §9 (frame format, Last-Event-ID, close after call.completed), specs/03 §6 DDL (event_type last),
  error codes CALL_NOT_FOUND / INVALID_INPUT / SERVICE_UNAVAILABLE raised before streaming. LiveHub reader exit relies on
  redis-py `PubSub.subscribed` = channels non-empty (set at SUBSCRIBE send time), so no restart race.

## Plan P06-T06b — Transcript token and WebSocket
Spec: specs/19 §9 (WS + token), specs/05 §2 table row (`POST /v1/calls/{call_id}/transcript/token`, `calls:read`).
Reuses the T06a machinery (LiveHub, SeqOrder, replay/tail/gap logic). No new event types.

### Decisions (add `> Decision (2026-11-03): transcript token and WebSocket (P06-T06b).` under specs/19 §9, after the T06a one)
1. Scope: `calls:read` (specs/05 table wins over the specs/04 §? `calls:write` scope description "transcript tokens";
   edit that specs/04 table cell to drop "transcript tokens" from `calls:write` and mention it under `calls:read`,
   same commit). No extra permission. Route on `calls_router` (legacy body), `operation_id="calls_transcript_token"`,
   `openapi_extra={"x-scope": "calls:read", **audit_exempt("short-lived read token, no state change")}`.
   Answers 200 `TranscriptTokenResponse {status: "success", token, expires_at, ws_url}` where
   `ws_url = "/v1/calls/{call_id}/transcript/ws?token=<token>"` (path only; the host is the api's). The route calls
   `withhold_on_replay(request, "token")` (one-time secret; same rule as minted API keys). Unknown / other-org call →
   `CALL_NOT_FOUND` (reuse `call_read.owned_call`). Any call status may get a token (ended calls replay then close).
2. Token = `vtt_` + `secrets.token_urlsafe(32)`; stored in Redis `transcript:token:{sha256hex(token)}` → JSON
   `{"org_id", "call_id", "expires_at"}` with `SET ... EX 60` (TTL 60 s, `TRANSCRIPT_TOKEN_TTL_S`). Not single use:
   any number of connections may open with it until it expires (reconnects inside the minute); expiry is checked
   both by the TTL and by `expires_at <= clock.now()` (the injected clock; tests use it). Redis failure on mint →
   `SERVICE_UNAVAILABLE`. Add the row to the specs/03 §11 Redis table.
3. `WS /v1/calls/{call_id}/transcript/ws?token=` (`@calls_router.websocket(...)` — `APIWebSocketRoute`, not in
   OpenAPI, not walked by the cross-tenant/audit suites). Authentication is ONLY the token (browsers cannot set
   headers; `Authorization`/cookies are ignored). Before `accept()` the handler validates: token present and
   well-formed, Redis record exists and not expired, record `call_id == path call_id` (else `call_mismatch`), the
   call exists in the record's org (`tenant_session(record.org_id)` + `owned_call`-equivalent by org) and the live
   subscription succeeds. Refusals use `websocket.send_denial_response(error_response(...))` (Starlette denial
   extension; uvicorn supports it) with the normal error envelope: missing/invalid/expired/mismatched token →
   401 `AUTH_FAILURE` "The transcript token is invalid or expired." (one message for all, reason only in the log
   `calls.transcript_ws_refused` with `reason` ∈ `missing|unknown|expired|call_mismatch`), call gone → 404
   `CALL_NOT_FOUND`, hub unavailable → 503 `SERVICE_UNAVAILABLE`. If the server lacks the extension Starlette falls
   back to closing (403) — fine. Builder: check `error_response` signature in `va_api.errors` (it needs a request id;
   use `request_id_of(websocket)`).
4. Messages: after accept, one JSON text message per event, `{"id": <seq>, "event": "<type>", "data": <bland-live
   body>}` (same body as the SSE frame: `live_body`/`row_body`, call metadata loaded once), only for
   `call.turn` and `call.post_transfer_transcript`, strictly increasing seq; replay of stored rows with those types
   first (all of them; no resume parameter), then the live tail. Partials: the event bus carries no partial
   transcript event (specs/19 §2 has none and adding one would reach webhooks and the sink), so the socket sends
   finalized turns only — record this in the Decision as the reading of "partials and finals". The server closes
   with code 1000 reason `completed` 30 s after `call.completed` (at once for an older call; the call.completed
   event itself is NOT sent, it only drives the close), `max_duration` (4 h, code 1000), and 1011 `overflow`/`lost`
   (client reconnects with a new token). Client messages are read and ignored; a client disconnect ends it. No app
   heartbeat (uvicorn sends protocol pings every 20 s). Log `calls.transcript_ws_closed` (call_id, reason, messages).
5. Refactor, no behaviour change for SSE: move the replay/tail/gap/close core of `CallStream` into
   `va_api/services/call_tail.py` `CallTail(deps, *, org_id, call_id, metadata, after, sub, types: frozenset[str] |
   None, heartbeat_s: float | None)` with `async def items() -> AsyncGenerator[TailItem | None]` where
   `TailItem(seq: int, event_type: str | None, body: Body)` and `None` = heartbeat due (only when `heartbeat_s` set);
   `.reason`, `.aclose()` (unsubscribe, idempotent). The filter applies at item build time: non-matching rows/events
   still advance `SeqOrder` (frame `None`), replay/gap re-reads stay unfiltered, so a missing non-transcript row
   never holds a turn longer than the existing 2 s gap rule. `call.completed` sets the close deadline even when
   filtered out. `CallStream` (SSE) becomes a thin renderer over `CallTail` (types=None, heartbeat 15 s,
   `sse_frame`/`HEARTBEAT_FRAME`); keep its public API and constants (`call_stream.py` re-exports the constants the
   tests import). Shared opener: `open_tail(deps, org_id, call_id, after, types, heartbeat_s)` does ownership check +
   subscribe (used by both `open_call_stream` and the WS opener). Keep T06a tests green unchanged (only imports may move).

### Files
- NEW `apps/api/src/va_api/services/call_tail.py` (core, from call_stream.py), modify `services/call_stream.py`.
- NEW `apps/api/src/va_api/services/transcript_tokens.py`: `TRANSCRIPT_TOKEN_TTL_S=60`, `TOKEN_PREFIX="vtt_"`,
  `token_key(token) -> str`, `TranscriptTokenRecord(org_id, call_id, expires_at)` (pydantic/dataclass + JSON),
  `TranscriptTokens(redis, clock)`: `mint(org_id, call_id) -> (token, expires_at)`, `resolve(token, call_id) ->
  TranscriptTokenRecord` raising `TranscriptTokenError(reason)`.
- NEW `apps/api/src/va_api/services/transcript_ws.py`: `TRANSCRIPT_TYPES = frozenset({"call.turn",
  "call.post_transfer_transcript"})`, `open_transcript(deps, websocket, call_id_text, token) -> CallTail` (raises
  AppError for refusals) and `serve_transcript(websocket, tail)` (accept, TaskGroup of sender + receiver, close code
  by reason, log). Deps: reuse `StreamDeps` + `redis` (add `redis` to StreamDeps or a new `TranscriptDeps`).
- `apps/api/src/va_api/routers/calls.py`: token POST route + websocket route + docstring lines;
  `routers/calls_detail_schemas.py`: `TranscriptTokenResponse` (examples entry).
- Specs: specs/19 §9 Decision; specs/03 §11 row; specs/04 scope table cell; specs/05 row unchanged.
- AGENTS.md bullet (WS route test pattern, denial response, token key).
- `make openapi` + both `gen:api` (new POST route).

### Tests
- `apps/api/tests/test_transcript_tokens.py` (fakeredis + FrozenClock/ManualClock): mint stores hashed key with TTL 60
  (`ttl` ≤ 60), token prefix/length, resolve ok; expired by clock (advance 61 s) → `expired`; unknown → `unknown`;
  other call id → `call_mismatch`; malformed (no prefix / empty) → `unknown`/`missing`; raw token never in Redis keys.
- `apps/api/tests/test_call_tail.py` or keep coverage via existing `test_call_stream.py` + new filter tests: type
  filter skips other events but they fill the seq gap; filtered `call.completed` still closes; no heartbeat when
  `heartbeat_s=None`.
- `apps/api/tests/test_transcript_ws.py` (module Postgres `session_helpers.migrated_postgres()`, real uvicorn via
  `service_helpers.serving(app)` / `RecordingServer`, `websockets.asyncio.client.connect`; container on a
  ManualClock or SystemClock set inside partitions — use `call_helpers.business_hours_today()` as in T06a tests):
  * POST token: 200 body shape, `CALL_NOT_FOUND` for other org's call, `calls:read` key works, key without
    `calls:read` → 403, Idempotency-Key replay returns `token: null`.
  * WS happy path: seeded rows (call.created, call.turn x2, call.latency) → client receives only the two turns in
    seq order with exact body keys; then a live `call.turn` published via `EventPublisher` arrives next; a live
    `call.completed` then closes 1000 `completed` after 30 s of clock (ManualClock `wait_for_sleep`/`run_for`, or an
    old completed call that closes right after replay to avoid timing).
  * replay+live order with a live event already stored (sent once).
  * refusals: no token / garbage token → handshake rejected with 401 and error envelope `AUTH_FAILURE`
    (`websockets.exceptions.InvalidStatus`, `.response.status_code == 401`, body JSON code); expired token (clock
    advanced past 60 s) → 401; token minted for call X used on call Y of the same org → 401; token of org B used on
    org A's call → 401; token valid but call deleted? (skip) ; log reasons asserted via `json_log_stream`.
  * client disconnect releases the hub subscription (hub channel count back to 0, wait on an event, no sleeps).
- Existing: `test_call_stream.py`, `test_openapi.py`, cross-tenant suite (token route probed: factory call_id exists,
  owner 200), `test_audit_markers.py` (audit_exempt present).

### Gates
`export PATH=/home/ssm-user/.local/bin:/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
`uv run pytest apps/api packages/va_events -q`; `make openapi` + both gen:api + `make test-ts`; full `make test` in
background with heartbeat Monitor (truncate log first); jscpd; radon. No call-flow change → no e2e.

### Risks
- Denial response: verify uvicorn 0.54 websockets implementation advertises `websocket.http.response` in
  `scope["extensions"]`; if a test server (websockets vs wsproto impl) lacks it Starlette raises/falls back — test
  over real uvicorn with the default `ws` impl.
- `RequestContextMiddleware` handles websocket scopes: make sure the denial `http.response.start` still gets
  `X-Request-Id` (it wraps `http.response.start` — the denial message type is `websocket.http.response.start`; it
  may not add headers; acceptable, but `error_response` sets X-Request-Id itself).
- Refactoring `CallStream` must keep T06a frame bytes identical (tests compare bytes).
- The WS sender awaiting the tail must be cancelled when the receiver sees `websocket.disconnect`; aclose in finally
  (shield the unsubscribe like T06a does if needed).

## Build P06-T06b (builder notes)
- `services/call_tail.py` = shared core (call_metadata → subscribe_tail(types, heartbeat_s) → CallTail.items yields
  TailItem | None); filtered events still fill SeqOrder slots; call.completed sets the close even when filtered.
  `call_stream.CallStream` now renders the tail (frame bytes unchanged; T06a tests untouched except helper moves).
- `call_read.org_call(session, org_id, call_id)`; `owned_call` delegates.
- `services/transcript_tokens.py` (vtt_ + token_urlsafe(32), `transcript:token:{sha256}` EX 60, reasons
  missing/unknown/expired/call_mismatch), `services/transcript_ws.py` (issue/open/serve; accept → sender+receiver
  tasks; 1000 completed/max_duration, 1011 overflow/lost/closed; logs refused/closed).
- Routes: POST `/{call_id}/transcript/token` (calls:read, audit_exempt, withhold token+ws_url on replay) and
  `@calls_router.websocket("/{call_id}/transcript/ws")` with denial response via `error_response(websocket, ...)`
  (`error_response` now takes HTTPConnection).
- Specs: 19 §9 Decision, 03 §11 row, 04 §2.3 scope list (transcript tokens under calls:read). AGENTS.md bullet.
- Tests: test_transcript_tokens.py, test_transcript_ws.py; shared `tests/support/live_stream_helpers.py`
  (SSE http test now uses it). openapi.json + both schema.d.ts regenerated.
- Gates: fmt/lint/typecheck/placeholders pass; make test-ts pass (22/52/48); jscpd 0 clones; radon avg A
  (CallTail.items C(12) = same as old CallStream.frames).

## Review P06-T06b — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11163 passed, 7 skipped (99.03 %).
- Checked against specs/19 §9 (+ Decision), specs/05 §2 row (calls:read), specs/04 scope table, specs/03 §11 key row.
  Token stored only as sha256; uvicorn access_log=False so the `?token=` query never hits logs. Refusals before
  accept via denial response; tail aclose in finally; SSE frames unchanged (T06a tests green).

## Plan P06-T06c — Org event stream
Spec: specs/19 §9 (third bullet), specs/26 §4 (`va webhooks listen`), specs/25 (dashboard live list `types=call.*`).
Reuse: `va_api.sse` (frames, `EventStreamResponse`), `webhook_routing.matches`, `webhook_payloads.content_body/
envelope_body/strip_phi`, the LiveHub subscription/overflow pattern.

### Decisions (add `> Decision (2026-11-03): org event stream (P06-T06c).` under specs/19 §9 after the T06b one)
1. `GET /v1/events/stream?types=<comma-separated globs>` on a new envelope router `events_router`
   (`routers/events.py`, prefix `/v1/events`, tag `Events`, in `main.ROUTERS`), `operation_id="events_stream"`,
   `require_scope("webhooks:read")` for both auth kinds (API key; dashboard session through the §3.2 matrix row
   "Webhook endpoints / deliveries / replay", i.e. every role except billing). Scope = `ctx.org_id` (a parent acting on
   a sub-account sees the sub-account; never descendants). GET → no audit marker needed.
2. `types`: optional, default `*`. Split on `,`, strip, drop empties; each pattern must match `^[a-z0-9_.*]{1,64}$`,
   at most 20 patterns, else `INVALID_INPUT` field `types` "types must be a comma-separated list of event type
   patterns such as call.*" (checked before streaming). Matching = `webhook_routing.matches` (`fnmatchcase`, same as
   org endpoints). `INTERNAL_TYPES` are never sent, even for `*`.
3. Source = pod-wide `va_events.StreamTailHub(redis, clock, *, block_ms=1000, queue_size=1000)` (new module
   `va_events/stream_tail.py`; container `stream_tail` built next to `live_hub`, `aclose` on exit; SessionContainer
   builds one on SystemClock like `live_hub`). ONE reader task per process, started by the first subscriber and ending
   when the last leaves: `XREAD COUNT 500 BLOCK block_ms STREAMS events:calls events:sms events:widget events:billing
   events:system <ids>` with ids starting at `$`... NOTE `$` must be resolved once at reader start (first XREAD with `$`
   for all keys, then carry the returned/last ids; use `XINFO STREAM`-free approach: on start read each key's last id
   with `XREVRANGE key + - COUNT 1` → id or `0-0`, then XREAD from those ids — avoids missing entries between loop
   iterations that `$` would cause). block_ms 1000 < socket timeout 5 s. Decoding: `va_events.model.decode`;
   undecodable payloads are skipped and logged `events.stream_tail_poison` (stream + entry id). Dispatch: subscribers
   indexed by `org_id`; a subscription has a predicate (types) evaluated in the reader, so only wanted events are
   queued. Full queue (1 000) → subscription closed `overflow`; Redis error → every subscription closed `lost`, reader
   ends, next subscribe restarts it. After an empty read the reader yields (`await asyncio.sleep(0)`; fakeredis
   returns immediately from blocking reads). `subscribe(org_id, predicate) -> TailSubscription` is synchronous-ish
   (no Redis confirmation needed; async for the lock), raises `StreamTailError` after `aclose`.
   Refactor to avoid duplication: move the queue/close logic of `LiveSubscription` into a generic
   `va_events.subscription.Subscription[T]` (`next`, `deliver`, `close`, `closed_reason`, `CloseReason`,
   `SubscriptionClosedError`); `LiveSubscription(Subscription[bytes])` keeps `channel` and `LiveSubscriptionClosedError`
   stays exported as an alias of the shared error (T06a/T06b tests must pass unchanged).
4. No replay and no resume: events published before the subscription are not sent; `Last-Event-ID` is ignored
   (document: clients re-read state with `GET /v1/calls/active`). Frames: `retry: 3000`, then per event
   `id: <evt_…>` (event id string; `sse_frame` gains an overload / new `sse_text_frame(event_id: str, ...)` — keep the
   int path byte-identical), `event: <type>`, `data: <§7 envelope JSON>`; `: ping` after 15 s without a frame.
5. Body = exactly what an org endpoint receives (§3 Decision rule 6): `envelope_body(event, content_body(event,
   metadata=, batch=))`; call metadata from `calls.metadata` cached per stream (LRU 1 024 calls; a missing call →
   `{}`), batch facts read per `batch.*` event with `batch_id` (no cache, counts change). DB reads go through
   `tenant_session(ctx.org_id)`; add public `va_events.webhook_router.call_metadata(session, org_id, call_id)` and
   `batch_facts(session, org_id, batch_id)` (thin wrappers over the existing `_call`/`_batch`). An org with
   `hipaa_mode` (read once at open via `org_settings_of(request).get(org_id)`) gets `strip_phi(event.type, content)`
   before the envelope (the stream is never `phi_allowed`). A DB failure while rendering one event logs
   `events.org_stream_render_failed` and sends the event with `data = event.data` stripped per hipaa? → simpler:
   skip that event (log type + id) and continue.
6. Ends when: client disconnects, subscription `overflow`/`lost`/`closed`, or 4 h (`MAX_STREAM_S`, reuse the
   call_tail constant). Log `events.org_stream_closed` (org_id, reason, frames). Headers/content type as the call SSE.
   Hub-subscribe failure (after aclose) → `SERVICE_UNAVAILABLE` before streaming.

### Files
- NEW `packages/va_events/src/va_events/subscription.py`; modify `live_hub.py` (use it); NEW `stream_tail.py`
  (`StreamTailHub`, `TailSubscription`, `StreamTailError`, `BLOCK_MS`, `QUEUE_SIZE`); export in `va_events/__init__.py`.
- `packages/va_events/src/va_events/webhook_router.py`: public `call_metadata`, `batch_facts`.
- NEW `apps/api/src/va_api/services/org_stream.py`: `parse_types(value) -> tuple[str, ...]`, `OrgStreamDeps(database,
  stream_tail, org_settings, clock)` + `org_stream_deps_of(request)`, `open_org_stream(deps, ctx, types) -> OrgStream`,
  `OrgStream.frames()` / `aclose()`; `MetadataCache` (OrderedDict LRU).
- NEW `apps/api/src/va_api/routers/events.py`; `main.py` ROUTERS; `va_api/container.py` `stream_tail`.
- `va_api/sse.py`: frame with a string id.
- tests support: `SessionContainer.stream_tail`; `cross_tenant_factories.py`: `REGISTRY.register_request("events_stream",
  params={"types": "BAD!"})` + `register_owner_status("events_stream", 400)` (an unbounded stream must not be
  probed through ASGITransport, which buffers); `test_openapi.STREAM_OPERATIONS` += `events_stream`.
- Specs: 19 §9 Decision; specs/03 §11 nothing new (reads existing streams). AGENTS.md bullet.
- `make openapi` + both `gen:api`.

### Tests
- `packages/va_events/tests/test_subscription.py` (moved/generic queue semantics if not already covered by
  test_live_hub — keep live hub tests green).
- `packages/va_events/tests/test_stream_tail.py` (fakeredis, publisher_rig-style XADD via `EventPublisher.broadcast`
  or raw XADD): event of org A reaches A's subscriber only; predicate filters; entries XADDed before subscribe not
  delivered, after delivered in XADD order across streams sharing one key; two subscribers same org both get it;
  overflow closes with `overflow` (queue_size=2); poison entry skipped + logged; Redis error (`server.connected =
  False`) closes all with `lost` and a later subscribe restarts the reader; last unsubscribe stops the reader
  (`reader_running` property false); aclose closes `closed` and later subscribe raises.
- `apps/api/tests/test_org_stream.py` (unit, fakes/ManualClock): `parse_types` cases (default `*`, spaces, empties,
  bad chars, >20, too long → INVALID_INPUT field `types`); frames for call.turn (envelope + §5 body with metadata,
  metadata loaded once for two events of one call), call.completed (data = Call Object), batch.* (batch body from
  row), sms.* (event data), internal `billing.usage` never sent even for `*`, hipaa org strips `data.text`
  (call.turn) / transcripts (call.completed); heartbeat after 15 s; 4 h close; overflow ends stream with reason.
- `apps/api/tests/test_org_stream_http.py` (module Postgres + real uvicorn `service_helpers.serving`, httpx stream):
  API key with `webhooks:read` gets 200 `text/event-stream`, first frame `retry`, then a published event of its org;
  other org's event not received (publish B then A, first frame is A's); `types=call.*` skips `sms.*`; key without
  `webhooks:read` → 403; dashboard session (owner) works; billing-role session → 403; bad `types` → 400 INVALID_INPUT;
  disconnect releases the subscription (hub subscriber count back to 0, waited on an event, no sleeps).
- Existing: test_live_hub, test_call_stream, test_transcript_ws, cross-tenant suite, test_openapi, test_audit_markers.

### Gates
`export PATH=/home/ssm-user/.local/bin:/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
`uv run pytest packages/va_events apps/api -q`; `make openapi` + both gen:api + `make test-ts`; full `make test` in
background with heartbeat Monitor (truncate log first); jscpd; radon. No call-flow change → no e2e.

### Risks
- `XREAD BLOCK` holds one pooled connection per process while subscribers exist; block 1 s < 5 s socket timeout.
- fakeredis blocking reads return at once → reader must yield after empty reads, or tests starve the loop.
- Rendering per event hits Postgres for uncached calls; LRU keeps it to once per call per stream.
- Cross-tenant suite must not try to stream (owner control = 400 on bad types).

## Build P06-T06c (builder notes)
- `va_events.subscription.Subscription[T]` (generic bounded queue, `SubscriptionClosedError`, `CloseReason`);
  `LiveSubscription` subclasses it, `LiveSubscriptionClosedError` = alias (T06a/b tests unchanged, green).
- `va_events.stream_tail.StreamTailHub(redis, *, block_ms, queue_size)`: start ids via XREVRANGE COUNT 1 per
  stream inside the first subscribe (so nothing after subscribe returns is missed), one XREAD loop, org+predicate
  dispatch, poison skip+log, Redis error → all `lost` + reader ends, last unsubscribe cancels the reader. Dropped
  the planned `clock` param (unused).
- `webhook_router.call_metadata` / `batch_facts` public wrappers.
- api: `services/org_stream.py` (parse_types, MetadataCache LRU, open_org_stream, OrgStream), `routers/events.py`
  (tag `Webhooks`: `va_api.openapi.TAG_NAMES` only allows specs/26 tags), container + SessionContainer
  `stream_tail`, `sse_frame` accepts str ids (int path byte-identical). Cross-tenant owner control = bad types → 400.
- Tests: test_stream_tail.py, test_org_stream.py, test_events_stream_http.py; `FrameReader`/`started`/`next_frame`
  moved into live_stream_helpers (call stream tests use them; jscpd 0 clones).
- Spec 19 §9 Decision P06-T06c; AGENTS.md bullet; openapi + both schema.d.ts regenerated.
- Full make test: 11195 passed, 1 failed (test_envelopes: `/v1/events*` missing from the family table) → added
  `/v1/events*` envelope family to `va_api.envelopes` + specs/31 list + Decision; family count 33. Re-ran
  `pytest apps/api` → 1538 passed. Coverage 99.04 %. jscpd 0 clones; radon avg A (2.32); pip-audit clean, pnpm
  audit 3 moderate (no high).

## Review P06-T06c — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11197 passed, 7 skipped (99.04 %); test-ts pass; openapi drift current.
- Checked against specs/19 §9 third bullet (+ Decision), specs/26 §4 `va webhooks listen`, specs/31 envelope family list.
  Reader holds one pooled connection (BLOCK 1 s < 5 s socket timeout); subscription released via on_close/finally;
  cross-tenant owner control fails fast with bad types.

## Plan P06-T07 — Retrofit phase-05 emitters and latency events
Spec: specs/19 §1 step 5 (+ P03-T17 Decision rule 7), §2 table, §3 (+ P06-T02 Decision); specs/07 §3 step 7
(`call.started`); specs/05 event log (line ~728). Survey findings: NO publisher is built with a router today
(api container, worker `WorkerActivities.build`, edge `carrier_handlers`, media `open_persistence` all pass none), so
no call event ever produces `webhook_deliveries` rows; per-turn `call.latency` (`E2E:` + `STT:`/`LLM:`/`TTS:` stage
events) already exist (`va_voice.latency.latency_events`, `TurnRecorder.on_latency`) — keep, only cover them in the
routing + e2e asserts; `call.started` is in the catalog but never emitted; the event log API has no type field.

### Decisions (add `> Decision (2026-11-03): emitter retrofit (P06-T07).` under specs/19 §1 after the P06-T05 one)
1. Routing per producer, always atomic with the event's `call_events` row:
   - api (`call.queued/scheduled`, stop's `call.ended`/errors…): `persist_event` routes in the caller's tenant session.
   - worker activities (`call.dialing`, `dispatch_blocked`, `ended`, `error`, `recording_ready`, `completed`,
     retry child `scheduled`): `persist_events` routes in the same session as the status change.
   - edge inbound accept (`call.ringing` inbound, failure `call.ended`): `_persist_event` routes in its session;
     edge carrier events (published after the state-change commit, row via `DirectCallEventWriter`): the publisher's
     router in its own `tenant_session` (`publish(..., session=None)`), like the row today.
   - media worker (buffered): the `call_events` flush routes the flushed rows' events in the same per-org transaction
     (§1 step 5 "batched with the call_events flush"); a routing failure fails that org's flush and the rows are
     re-queued (idempotent: rows ON CONFLICT DO NOTHING, deliveries ON CONFLICT (event_id, url) DO NOTHING).
     The event is rebuilt from the row (`event_of(row)`: id `evt_<row.id.hex>`, type `row.event_type`, org/call,
     `occurred_at = created_at`, category, level, message, data, seq); media events carry no batch/conversation/widget
     id. Rows without `event_type` are not routed (logged `events.call_event_not_routed`).
   - dispatcher: never routes (`call.webhook_delivery` would loop through `*` endpoints); stays `router=None`.
2. `call.started` (media, category `call`): emitted once per primary session after the vendors opened and before the
   pipeline runs (specs/07 §3 step 7), message `"Call started"`, data `{direction, media_format}` (`"PCMU/8000"` or
   `"L16/16000"` style from the stream's `MediaFormat`, use whatever string the transport already logs). `started_at`
   is not written by the media worker: the edge sets it on `call.answered` (outbound) and accept (inbound).
3. Event log items (`GET /v1/calls/{id}/events`) gain `type` (the event type, `call_events.event_type`; `null` for rows
   written before migration 0007). Update specs/05 line ~728 body + Decision line, openapi, gen:api.

### Files
- `packages/va_events/src/va_events/publisher.py`: `WebhookRouter` protocol gains `route_many(events, *, session)`;
  new `EventPublisher.route(events: Sequence[DomainEvent], *, session: AsyncSession | None) -> None` (no-op without a
  router; router errors propagate) — for callers that sequence + insert rows themselves.
- `packages/va_events/src/va_events/call_events.py`: `event_of(row) -> DomainEvent | None`; `DbCallEventWriter(database,
  *, router: WebhookRouter | None = None)` — `_insert` does `insert_rows` then `router.route_many([...], session=)`.
  Export `event_of` in `__init__`.
- `packages/va_workflows/src/va_workflows/activities/events.py`: `persist_events` calls
  `await publisher.route(sequenced, session=session)` after `insert_rows` (router errors propagate → activity retry;
  the DB transaction rolls back).
- `apps/worker/src/va_worker/activities.py`: `EventPublisher(redis, call_events=None, router=DbWebhookRouter(database))`.
- `apps/api/src/va_api/services/call_event_rows.py`: `persist_event` routes; `apps/api/src/va_api/container.py`:
  publisher with `router=DbWebhookRouter(database)`; `apps/api/tests/support/session_helpers.py` SessionContainer
  publisher gets a router when it has an identity database (the router never touches its own database when a
  session is passed; check `__post_init__` order).
- `apps/telephony_edge/src/va_edge/main.py`: publisher `router=DbWebhookRouter(database)`; `accept.py`
  `_persist_event` calls `self._publisher.route([sequenced], session=session)`.
- `apps/media_worker/src/va_media/runtime.py` `open_persistence`: `CallEventBuffer(DbCallEventWriter(database,
  router=DbWebhookRouter(database)), ...)`; in `_converse` call `recorder.started(transport.fmt / session.start)` right
  before `pipeline.run()` (after maintenance/monitor wiring). `apps/media_worker/src/va_media/turns.py`:
  `TurnRecorder.started(direction, media_format)` publishes `call.started` through the ordered chain (first event of
  the recorder, before any turn).
- `apps/api/src/va_api/routers/calls_detail_schemas.py` `CallEventItem.type: str | None` (+ example);
  `services/call_artifacts.py` fills it from `row.event_type`. `make openapi`, both gen:api.
- `tests_e2e/test_outbound_basic.py` + pure helper `tests_e2e/support/e2e_assertions.py`
  `event_types(events)` / `check_event_order(types)`: assert present ⊇ {call.queued, call.dialing, call.ringing,
  call.answered, call.started, call.turn, call.latency, call.ended, call.recording_ready, call.completed}; ordering only
  where causal: `call.queued` first; `call.started` before every `call.turn`/`call.latency`; every turn/latency before
  `call.ended`; `call.ended` < `call.recording_ready` < `call.completed`; `call.completed` last. (dialing/ringing/
  answered/started may race between services — not ordered.) Unit tests in `tests_e2e/unit/`.
- Specs 19 §1 Decision, 05 event log, 07 §3 step 7 note (Decision ref). AGENTS.md bullet.

### Tests
- `packages/va_events/tests/test_publisher.py`: `route` no-op without router; passes session + events to a
  `RecordingRouter` (extend it with `route_many`).
- `packages/va_events/tests/test_call_events*.py`: `event_of` round trip (`event_of(row_for(e)) == e` for a sequenced
  call event); `None` for `event_type=None`. DB test: `DbCallEventWriter(db, router=DbWebhookRouter(db))` with a call
  whose config has `webhook` + `webhook_events: ["latency"]` → `call.latency` row + one `bland-live` delivery; router
  raising → no row and no delivery for that org, `CallEventWriteError` lists the rows; other org still written.
- `packages/va_workflows/tests/`: `persist_events` routes in-session: seed call with `webhook`, run inside a session
  that raises after `persist_events` → neither `call_events` row nor delivery (rollback proof); committed → delivery
  row for `call.completed` (format `bland`, payload = Call Object data). Activity-level: post-call `call.completed`
  and telephony `call.dialing` with `webhook_events=["call"]` produce deliveries (use existing rigs; worker publisher
  needs the router — rigs build their own publisher: add `router=DbWebhookRouter(database)` there).
- `apps/api/tests`: dispatch with `webhook` + `webhook_events:["queue"]` → `call.queued` delivery row in the same
  transaction; without `queue` in webhook_events → none; `persist_event` rollback proof (raise after it inside
  `tenant_session` → no row, no delivery). Event log response includes `type`.
- `apps/telephony_edge/tests`: inbound accept with an org endpoint `call.*` → `call.ringing` delivery; carrier
  `call.answered` with per-call webhook + `webhook_events:["call"]` → delivery row.
- `apps/media_worker/tests`: `TurnRecorder.started` publishes `call.started` first (unit, fake sink); runtime sim
  (`rig(persistence=...)` + seeded call with `webhook` + `webhook_events:["call","latency"]`) → after the call,
  `webhook_deliveries` holds `call.started`, `call.turn`, `call.latency` rows (bland-live) and NO `call.interrupt`.
- `tests_e2e/unit/test_e2e_assertions.py`: `check_event_order` accepts a real order, rejects ended-before-turn,
  completed-not-last, missing types.
- Existing suites: test_catalog, test_webhook_router, dispatcher, call stream tests (SSE frames unchanged),
  `test_openapi`, cross-tenant.

### Gates
`export PATH=/home/ssm-user/.local/bin:/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
targeted pytest (va_events, va_workflows, apps/api, apps/telephony_edge, apps/media_worker, apps/worker, tests_e2e/unit);
`make openapi` + both gen:api + `make test-ts`; full `make test` in background with heartbeat Monitor (truncate log);
jscpd; radon; touches the call flow → `make e2e` (at least outbound_basic, then the full run) on the EC2 runner/stack.

### Risks
- Media flush now does extra reads per org per flush (call facts + endpoints): route_many caches policy per org but
  reads call facts per event; if it shows in the 50-call capacity e2e, cache `CallFacts` per call inside one
  `_route_all` (dict by call_id) — do it anyway, cheap.
- A deterministic router bug would re-queue the org's rows forever: log once per failure (existing warning) — acceptable.
- SessionContainer router: api tests that dispatch without DB must still work (route only runs with a session).
- e2e ordering across services: only causal constraints asserted (see above).

## Build P06-T07 (builder notes)
- va_events: `WebhookRouter.route_many` in the protocol; `EventPublisher.route(events, session=)` (no-op without
  router); `event_of(row)` (call_events.py, exported); `DbCallEventWriter(database, router=)` routes in the per-org
  insert transaction (failure → that org's rows unwritten, re-queued).
- Producers: api `persist_event`, worker `persist_events`, edge accept `_persist_event` call `publisher.route` in-session;
  api container / worker `WorkerActivities.build` / edge `carrier_handlers` publishers get `router=DbWebhookRouter(db)`;
  media `open_persistence` buffer writer gets the router (publisher none); dispatcher untouched.
- `call.started`: `TurnRecorder.started(media_format)` (once, `STARTED_MESSAGE`), called by `CallRuntime._converse`
  right before `pipeline.run()`; `va_media.transport.stream_format` → "PCMU/8000".
- Event log `type` field (CallEventItem, call_artifacts), specs/05 §7.9 Decision; openapi + both schema.d.ts regenerated.
- e2e: `event_types`, `event_order_failures`, `REQUIRED_CALL_EVENTS` in e2e_assertions; `event_present` uses `type`;
  outbound_basic asserts the order. Unit tests in tests_e2e/unit/test_e2e_support.py.
- Tests added: test_call_events.py (event_of), test_publisher route, test_call_events_db routing writer + failing org,
  api test_calls_send (queued delivery / routing failure rollback), test_calls_artifacts type, workflows post-call
  completed delivery + persist_events rollback + dial webhook_events, edge carrier answered delivery + accept
  ringing/ended deliveries, media test_turns started, test_transport stream_format, runtime sim deliveries.
- Specs: 19 §1 Decision P06-T07, 07 §3 step 7 note, 05 §7.9 Decision. AGENTS.md bullet.
- Gates: fmt/lint/typecheck/placeholders pass; make test 11224 passed, 7 skipped, 99.04 %; jscpd 0 clones; radon A
  (2.58); pip-audit clean; pnpm audit 3 moderate (no high). e2e: see below.
- make e2e: 16 passed (outbound_basic with the event order check, concurrent_calls, all flows) in 338 s, exit 0.

## Review P06-T07 — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11224 passed, 7 skipped (99 %); test-ts pass; make e2e 16 passed (342 s).
- Checked against specs/19 §1 step 5 + Decision P06-T07, §2 catalog, specs/05 §7.9, specs/07 §3 step 7. Routing in-session for api/worker/edge accept; media routes in the per-org flush; dispatcher unrouted. Rollback proofs for api dispatch, persist_events, media writer.

## Plan P06-T08 — E2E: webhooks_call_completed_and_live
Spec: specs/30 §5 (flow table; Decision P05-T17b (3) promised the receiver with phase 06), specs/19 §3 (+P06-T02
Decision), §4 Call Object, §5 bland-live body, §7 envelope, §8 headers/retry (+P06-T03 Decision), §10 signing secret
rotate / endpoints / deliveries / test send. Phase exit gate wording: "receiver fixture verifies signatures, live
categories filtering, retries after a 500, and final Call Object fields".

### Decisions (add `> Decision (2026-11-03): webhooks e2e flow (P06-T08).` under specs/30 §5 after the P05-T17d one)
1. Receiver = `tests_e2e/support/e2e_receiver.py` (not `tests_e2e/receiver.py` as the T17b Decision guessed: the
   support dir is on pytest pythonpath/mypy_path; amend T17b (3) text with "(phase 06: `e2e_receiver`, see Decision
   P06-T08)"). It runs in the pytest process (real uvicorn via `va_common.service.run_server` + `ReadyServer`, bound
   on `E2E_RECEIVER_BIND` default `0.0.0.0`, port `E2E_RECEIVER_PORT` default 0) and the containers reach it as
   `http://{E2E_RECEIVER_HOST}:{port}` (default host `host.docker.internal`, mapped to the docker host by
   `extra_hosts: ["host.docker.internal:host-gateway"]` on `api` and `webhooks` in `docker-compose.e2e.yml`; the api
   resolves it too because Send Call and endpoint create vet the URL). `docker-compose.e2e.yml` also pins
   `WEBHOOK_ALLOW_PRIVATE: "true"` on `api` (the gateway address is private; the dispatcher already has it).
2. Secret: the flow rotates the fresh org's secret (`POST /v1/webhooks/signing-secret/rotate`, the only call that
   returns plaintext) BEFORE any delivery, so the dispatcher's 300 s cache is filled with the new set. Verification:
   `X-VA-Signature` = `t=<unix>,v1=<hex>[,v1=<hex>]` (dual-sign window: two v1s, current first) — accept when ANY v1
   equals HMAC(secret, "<t>.<raw body>"); `t` == `X-VA-Timestamp`, |t − receipt wall time| ≤ 300 s;
   `X-Webhook-Signature` == HMAC(secret, raw body) (current secret); `X-VA-Event-Id` == `X-Event-Id` starts `evt_`;
   `User-Agent` `VoiceAgents-Webhooks/1.0`; `Content-Type` application/json. Every request the receiver got must
   verify (a forged/unsigned one fails the test).
3. Flow (one outbound call reusing the `outbound_basic` scenario + rules via `harness.load_scenario("outbound_basic")`):
   - Org endpoint `POST /v1/webhooks/endpoints {url: <recv>/org, event_types: ["call.completed", "call.ended"]}`;
     preflight `POST /v1/webhooks/endpoints/{id}/test` must answer `succeeded: true, status_code: 200` (proves the
     api container reaches the receiver; failure message names `E2E_RECEIVER_HOST`), and the synthetic envelope it
     sent verifies (data.test true, type call.completed).
   - Receiver script: path `/call` answers 500 to the FIRST `call.completed` request, 200 otherwise.
   - Send Call with `webhook: <recv>/call`, `webhook_events: ["queue", "latency"]`, `metadata: {"run": token}`,
     `record: true`, same task/first_sentence as outbound_basic.
   - Wait (event-driven `receiver.wait_for`, timeout ~180 s) for a 200-answered `call.completed` on `/call`.
4. Assertions:
   - Signatures (decision 2) on every received request.
   - Live filtering on `/call`: event types (header) ⊆ {call.queued, call.latency, call.completed}; exactly one
     distinct `call.queued` event id; distinct `call.latency` event ids == the event log's `call.latency` rows
     (count ≥ 1 per caller turn, match by `seq`); every live body is §5 shaped: keys exactly {call_id, category, level,
     message, data, seq, created_at, metadata}, category ∈ {queue, latency}, `metadata == {"run": token}`, `call_id`
     matches, `created_at` ISO-Z ms; at least one latency message matches `^(E2E|STT|LLM|TTS): \d+ms$`. No
     `call.turn`/`call.dialing`/`call.ended` reached `/call`.
   - Retry: exactly two `call.completed` requests on `/call`, same `X-VA-Event-Id`, second − first receipt (monotonic)
     ∈ [9, 25] s (10 s × 0.9 jitter floor; upper = 11 s + 1 s poll + slack), first answered 500, second 200. Then poll
     `GET /v1/webhooks/deliveries?call_id=<id>&event_type=call.completed` until the per-call row (`endpoint_id` null,
     url `/call`, format `bland`) is `succeeded`, `attempts == 2`, `last_status_code == 200`; the org-endpoint row
     (format `envelope`) `succeeded`, `attempts == 1`. Event log contains `call.webhook_delivery` rows for that
     delivery: one level `warning` message matching `failed: 500 (attempt 1), retrying in` and one `info`
     `Webhook delivered to <host>: 200` (host only, never the URL path).
   - Final Call Object (`/call` 200 body, bare, `bland`): keys ⊇ outbound_basic's `CALL_OBJECT_FIELDS` (import from
     a shared place, see Files), `call_id`, `status == "completed"`, `completed is True`, `metadata == {"run": token}`,
     transcripts alternate and start with the first sentence, `recording_url` set; equals `GET /v1/calls/{id}` on
     every key except `recording_url` (presigned per request; both non-empty). Both 500'd and retried bodies are
     byte-identical (same stored payload).
   - Org endpoint `/org`: envelope §7 for `call.ended` and `call.completed` (each once): keys {id, type, created_at,
     org_id, call_id, batch_id, conversation_id, data}, `id` == header event id, `type` == header type, `org_id`,
     `call_id`; `data` of call.completed == the `/call` Call Object; `data` of call.ended is the §5 body with
     `data.end_reason == "agent_end_call"`. Nothing else reached `/org` (besides the preflight test event).
5. outbound_basic is unchanged (keeps the event-log path); Decision P05-T17b (3) amended: flows may now wait on the
   receiver; event-log checks remain valid.

### Files
- `tests_e2e/support/e2e_receiver.py` (new): `Reply(status=200)`, `Received(path, headers: dict[str,str], body: bytes,
  at: float (time.monotonic), wall: float (time.time()), status: int)`, `WebhookReceiver` (scripts per path FIFO then
  200, records, `changed` event, `wait_for(predicate, timeout_s)`, `of(path, event_type=None)`, `event_type(r)`),
  `served_receiver(receiver, *, bind, port) -> AsyncIterator[int]` (asynccontextmanager; `run_server(app, host=bind,
  port=port, stop=StopController(), install_signals=False, server_factory=ReadyServer factory)` like
  `service_helpers.serving` but bind host configurable), `ReceiverSettings.from_env(environ)` (bind, port, public host),
  pure `signature_failures(received, secret, *, now, tolerance_s=300) -> list[str]` (empty = ok; reasons), `hex_hmac`,
  `parse_signature_header(value) -> (t, [v1...])`. Put `signature_failures` & parsing pure so they're unit-tested.
- `tests_e2e/support/e2e_webhooks.py` (new, pure): `LIVE_BODY_KEYS`, `ENVELOPE_KEYS`, `live_body_failures(body, *,
  call_id, categories, metadata) -> list[str]`, `envelope_failures(body, *, headers, org_id, call_id) -> list[str]`,
  `retry_gap_ok(first_at, second_at, *, base_s=10.0, jitter=0.1, slack_s=14.0)`, `LATENCY_MESSAGE` regex,
  `call_object_mismatches(webhook_body, api_body, ignore=("recording_url",)) -> list[str]`.
  Move `CALL_OBJECT_FIELDS` to `e2e_assertions.py` (outbound_basic imports it from there).
- `tests_e2e/support/e2e_harness.py`: `rotate_signing_secret(org) -> str`; `create_endpoint(org, url, event_types)
  -> dict`; `test_endpoint(org, endpoint_id) -> dict` — NAME IT `send_endpoint_test` (never `test_*`, pytest collects);
  `deliveries(org, **filters) -> list[dict]`; `wait_deliveries(org, predicate, *, call_id, timeout_s)` via `_poll`.
  `Harness.open` gains nothing else; receiver is a separate fixture.
- `tests_e2e/conftest.py`: fixture `receiver` → `async with served_receiver(...) as port:` yields
  `(WebhookReceiver, base_url)` (small dataclass `ServedReceiver(receiver, url)`).
- `tests_e2e/test_webhooks_call_completed_and_live.py` (new): `pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]`;
  one test `test_webhooks_call_completed_and_live(harness, receiver)` per Decisions 3-4, split into small check
  helpers (`check_signatures`, `check_live`, `check_retry`, `check_call_object`, `check_org_endpoint`) to keep radon low.
- `docker-compose.e2e.yml`: `api: {extra_hosts: [...], environment: {WEBHOOK_ALLOW_PRIVATE: "true"}}`,
  `webhooks: {extra_hosts: [...]}`; update header comment. `scripts/tests/test_compose.py::
  test_e2e_override_pins_the_realtime_sim_clock` → rename/extend to assert the full expected override dict.
  specs/29 §2: extend the P05-T17a decision text (or a new P06-T08 sentence) for the override contents.
- Specs: 30 §5 Decision P06-T08 + T17b (3) amendment. AGENTS.md bullet (receiver, host-gateway, env knobs, rotate
  first, `send_endpoint_test` naming).

### Tests (unit, `tests_e2e/unit/test_e2e_webhooks.py`, run by make test)
- `signature_failures`: valid single v1; valid dual v1 where only the second matches; wrong secret → failure; body
  tampered → failure; `t` ≠ X-VA-Timestamp → failure; stale t (> 300 s) → failure; missing headers → failure (no
  KeyError); `X-Webhook-Signature` mismatch → failure; event id header mismatch (X-VA-Event-Id ≠ X-Event-Id).
  Secrets generated with `secrets.token_hex` at test time (never literals that look like keys; gitleaks).
- `parse_signature_header` ordering / malformed.
- `live_body_failures`: ok body; extra key; wrong category; wrong metadata; bad created_at.
- `envelope_failures`: ok; id/type mismatch with headers; wrong org.
- `retry_gap_ok`: 9.0 ok, 8.9 not, 25 ok, 25.1 not (defaults).
- `call_object_mismatches`: ignores recording_url, reports differing key.
- `WebhookReceiver` in-process: serve on 127.0.0.1 port 0 (`served_receiver(bind="127.0.0.1")`), POST with httpx:
  scripted 500 then default 200; `received.status` recorded; `wait_for` wakes; `ReceiverSettings.from_env` defaults
  and overrides.
- `scripts/tests/test_compose.py` override assertion.

### Gates
`export PATH=/home/ssm-user/.local/bin:/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`;
`uv run pytest tests_e2e/unit scripts/tests/test_compose.py -q`; full `make test` in background + heartbeat Monitor
(truncate log first); jscpd; radon; `make e2e` (full: rebuilds stack with the new override; first run the new flow
alone on the running stack: `uv run pytest tests_e2e -m e2e -k webhooks -s`).

### Risks
- host-gateway reachability on the Linux EC2 runner (iptables INPUT from docker0); preflight test-send gives a clear
  message. If blocked, fallback: `E2E_RECEIVER_HOST=<docker0 IP>` or run the receiver bound to the docker bridge IP;
  document whatever works in AGENTS.md.
- Binding 0.0.0.0 exposes the receiver briefly on the host; acceptable for a test (ephemeral port), configurable.
- Retry timing: dispatcher polls every 1 s; jitter ×[0.9,1.1] → gap 9–11 s + ≤1 s poll + delivery time; bound 25 s.
- The dispatcher secret cache: rotate before the first delivery (the preflight test send does not go through the
  dispatcher, so the cache is empty until the call's first delivery).
- latency event count vs log: deliveries are written in the media flush transaction; by the time call.completed
  arrives all are routed; but dispatcher may still be delivering the last latency rows → wait_for until the
  receiver has every log `call.latency` seq (bounded) before asserting.

## Build P06-T08 (builder notes)
- Receiver `tests_e2e/support/e2e_receiver.py` (WebhookReceiver scripts per path + optional event_type — needed because
  call.queued/latency also hit `/call` before call.completed; ServedReceiver; ReceiverSettings; signature_failures,
  parse_signature_header); fixture `receiver` in conftest. Pure checks `e2e_webhooks.py`; CALL_OBJECT_FIELDS moved
  to e2e_assertions. Harness: rotate_signing_secret, create_endpoint, send_endpoint_test, deliveries, wait_deliveries.
- Flow `tests_e2e/test_webhooks_call_completed_and_live.py` passes on the running stack (~33 s). Finding: Call Object
  differs from GET on `recording_expiration` too (per-read presign) → PRESIGNED_FIELDS ignored.
- host-gateway verified on EC2: containers reach 172.17.0.1 (docker0) from the compose network.
- docker-compose.e2e.yml: api/webhooks extra_hosts, api WEBHOOK_ALLOW_PRIVATE; test_compose updated; specs/29 §2
  P05-T17a (5) extended; specs/30 §5 row + Decision P06-T08 + T17b (3) amendment; AGENTS.md bullet.
- Unit tests tests_e2e/unit/test_e2e_webhooks.py (signing via production request_headers + independent verification).
- served_receiver reuses `service_helpers.serving(app, port=, host=)` (host param added) — jscpd 0 clones.
- Gates: fmt/lint/typecheck/placeholders pass; make test 11267 passed, 7 skipped, 99 %; post-edit re-run of
  tests_e2e/unit + va_common + va_events + dispatcher 1295 passed; make e2e 17 passed (370 s) incl. the new flow; radon A.

## Review P06-T08 — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11267 passed, 7 skipped (99.04 %); make e2e 17 passed (374 s) incl. webhooks_call_completed_and_live.
- Checked against specs/19 §8 headers (X-VA-*, X-Webhook-Signature, X-Event-Id, UA), §5/§7 bodies, specs/30 §5 Decision P06-T08; signature verification independent of the signer; compose override test + specs/29 updated.

## Phase 06 exit gate — passed
- make check exit 0 (all Python + TS gates, coverage); make e2e 17 passed in 375 s (exit 0) incl. webhooks_call_completed_and_live. README row 06 → complete (2026-11-03, fa007d9).

## Build fix:bed-none-flake (builder notes)
- plan.ready arrived with no new `## Plan` (phase 06 complete); built the only open task, the flaky
  `test_the_background_bed_fills_the_agent_track_between_utterances` (fixed 2000-2800 ms window overlapped a late opener).
- FakeTelnyx: `Call.mark_echoes` (`MarkEcho(name, track_ms)` = aligned agent-track length when the mark echoed, i.e. where
  that audio ended), timeline `marks: [{name, track_ms}]`; specs/30 §3 (14) text updated; sim tests added/updated.
- Media test: window = [last mark echo + 200, + 1000] ms, caller silence 4000 ms. Passes under 4×`yes` CPU load with -n 8.
- AGENTS.md P05-T15 bullet: measure agent-track windows from mark echoes.
- Gates: fmt/lint/typecheck/placeholders pass; make test 11268 passed, 7 skipped, 99.03 %; jscpd 0 clones; radon A (2.11);
  pip-audit clean; pnpm audit 3 moderate (no high). No e2e: sim instrumentation + unit test only.

## Review fix:bed-none-flake — approved
- Gates re-run: lint/typecheck/placeholders pass; make test 11268 passed, 7 skipped (99.04 %); targeted bed + mark-echo tests pass.
- Diff checked: FakeTelnyx `Call.mark_echoes`/timeline `marks` documented in specs/30 §3 (14); test window anchored on last mark echo with length assert; no plan task (runtime task only).
