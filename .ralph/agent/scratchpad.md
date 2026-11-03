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
