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
