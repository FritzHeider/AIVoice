# Memories

## Patterns

### mem-1793330327-17d9
> va_sim caller runner: CallerScript (telnyx/caller_script.py + caller_steps.py) starts via CallMedia.media_connected after the media start frame; MediaSession.add_listener(on_tick(AgentTick)/on_media_end) must stay O(1); completion via queue_caller/queue_dtmf offsets vs caller_sent/dtmf_sent; CPU prep under va_sim.tasks.hold_clock + to_thread costs no virtual time
<!-- tags: sim, testing, clock | created: 2026-10-30 -->

### mem-1793327751-3934
> va_sim media WS: tests giving a stream a stream_url must use the media_agent fixture (tests/telnyx/conftest.py, support/media_agent.py); MediaSession tick loop always sleeps 20 ms ahead so accelerated virtual time races during socket waits - assert orders/counts; never tasks.drain() with a live stream; the session freezes accelerated time while connecting via a sleep(0) holder task
<!-- tags: sim, testing, clock | created: 2026-10-30 -->

### mem-1793326334-2ae5
> va_sim.audio: synthetic caller audio (synthesize_utterance/pcm16/wire, decode_marker, goertzel_power, synthesize_dtmf, own G.711 codec); seed with SimState.utterance_seed(id); utterance ids 16-bit; audio must not import va_sim.state; synthesis is CPU-bound -> asyncio.to_thread; body envelope has a -12 dB floor so VAD sees continuous speech
<!-- tags: sim, audio | created: 2026-10-30 -->

### mem-1793323959-b262
> va_sim AcceleratedClock advances only when loop._ready is empty; tests drive FakeTelnyx calls with telnyx_helpers.at(state, s)/settle(state) (both also await state.call_engine.idle() since recording WAVs encode in a thread); a test busy-polling asyncio.sleep(0) freezes accelerated time; an in-process ASGI request can finish without yielding, so settle (clock.sleep(0)) before sleeping to a tie deadline
<!-- tags: sim, testing, clock | created: 2026-10-30 -->

### mem-1793318126-f3a5
> va_sim: control API /sim/* via va_sim.server.simulator_routes(sim, state); tests use running_sims(state) (real uvicorn, ephemeral ports) or httpx.ASGITransport over Starlette(routes=simulator_routes(...)); sim_helpers (packages/va_sim/tests/support) has new_state/scenario_yaml and the shared autouse isolated_env fixture that both va_sim and apps/sim conftests re-export; dist names va-sim (lib) / va-sim-server (apps/sim)
<!-- tags: sim, testing | created: 2026-10-29 -->

### mem-1793292824-0b49
> va_api: a new /v1 route family must be added to specs/31 §1 envelope matrix AND va_api.envelopes.FAMILIES (test_envelopes compares them verbatim and counts them), otherwise the path classifies as legacy; Redis CAS pattern: pipe.watch -> read -> pipe.multi() (type: ignore[no-untyped-call]) -> set(keepttl=True), WatchError = lost race
<!-- tags: api, redis, testing | created: 2026-10-29 -->

### mem-1793291061-0ce1
> va_api request gates read org settings via OrgSettingsReader.get_lenient (invalid stored keys -> defaults); get_principal enforces ip_allowlist after auth; tests vary the client address with tests/support/peer.py PeerSwitch(session_app(c)) + session_client; DB-less AuthContainer has MemoryOrgSettings; settings PATCH with non-empty ip_allowlist must include caller 127.0.0.1
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793289699-6a26
> va_api org settings: write via routers.org_settings.check_settings_permissions + apply_settings_patch (locks org row, merges, validates, audit diff keyed settings.<path>), then OrgSettingsReader.invalidate after commit; tests swap container.org_settings to inject broken Redis; INVALID_INPUT field format is field_path (items[0].name)
<!-- tags: api, settings, testing | created: 2026-10-29 -->

### mem-1793288415-ea3a
> va_domain.org.OrgSettings: orgs.settings stored sparse (load_settings/sparse_settings); PATCH body parsed with OrgSettings + exclude_unset, merge_settings (groups key-wise), needs_compliance, field_path(loc). pydantic EmailStr rejects .test domains used in fixtures - use email_validator.validate_email(check_deliverability=False, test_environment=True)
<!-- tags: api, pydantic, settings | created: 2026-10-29 -->

### mem-1793287252-a730
> va_api audit markers: every POST/PUT/PATCH/DELETE route needs openapi_extra {**audited('<action>',...)} or {**audit_exempt('<reason>')} (test_audit_markers walks app.state.routers). Dashboard auth rows use va_api.audit.audit_user(session, request, org_ids, user_id, action) - one row per membership org; signing in now writes auth.login rows, so tests listing a whole org log must filter auth.* or remove them via system_session
<!-- tags: api, audit, testing | created: 2026-10-29 -->

### mem-1793285970-ba4c
> va_api audit rows: call va_api.audit.audit(session, ctx, action, resource_type, resource_id, before=, after=) inside the route's open session; va_db.audit.record flushes under use_org(actor org) so it works on global sessions too; new actions go in specs/04 §13 AND va_db.audit.AUDIT_ACTIONS (test compares); audit_logs is append-only for va_app (0004) so seed/cleanup via system_session; paginated envelope lists use va_api.responses.PageResponse[T]
<!-- tags: api, database, audit | created: 2026-10-29 -->

### mem-1793282863-1d8b
> va_api sub-accounts: get_principal applies X-VA-Org-Id via auth.sub_accounts.act_on_sub_account before rate limit/scope/idempotency; use ctx.principal_org_id (not ctx.org_id) for the caller's own API key/membership rows; session child active org via session_auth.resolve_switch_target (MembershipLookupRepo.via_parent). Tests: session_helpers.sub_account / sub_account_member; structlog capture_logs misses contextvars+cached loggers in full suite - wrap structlog.contextvars.bind_contextvars instead
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793281395-6e00
> va_api: routes returning one-time secrets call va_api.idempotency.withhold_on_replay(request, 'api_key' | 'data.key'); stored idempotency body and replays get null. Session tests asserting idempotency_keys rows: monkeypatch container.idempotency = DbIdempotencyStore(identity.database, container.clock)
<!-- tags: api, testing, auth | created: 2026-10-29 -->

### mem-1793280879-041b
> va_api sub-accounts: va_db.session.use_org(session, org_id) switches RLS org inside one va_app transaction (CreditTransferRepo writes parent+child ledger rows); SessionContainer.authenticator falls back to DB lookup so route-minted keys authenticate; session_helpers.fund/hold/set_org/stored_org
<!-- tags: api, database, testing | created: 2026-10-29 -->

### mem-1793279423-eb1e
> va_api tests: idp (FakeOidcProvider on container clock) and sso (OIDC_ENABLED client) fixtures live in apps/api/tests/conftest.py; invitation helpers session_helpers.invite(client, container, email, role)->token, INVITATIONS, ACCEPT_INVITATION; session_auth.optional_session for routes that work with or without a session
<!-- tags: api, testing, auth | created: 2026-10-29 -->

### mem-1793275386-bcdb
> va_api tests: session_helpers.signed_in(client, identity, container, role) + add_member(identity, hasher, org_id, role) for extra members; DataResponse[list[Model]] now wraps model examples (test_operations_contract requires examples on every 2xx)
<!-- tags: api, testing, openapi | created: 2026-10-29 -->

### mem-1793272997-3347
> SSO tests: tests/support/fake_oidc.py FakeOidcProvider(now=container.clock.now) + mocked_idp(provider) + sso_app(container) + configure_sso(client, identity, container) + sso_login(client, provider, email); redirect-only routes listed in test_openapi.REDIRECT_OPERATIONS
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793271457-e7c7
> va_api: outbound fetches to admin URLs use va_api.auth.oidc_discovery.OidcDiscovery (no redirects, 5s, 256KiB, is_global check unless VA_ENV local/test); tests stub IdPs with respx.mock (patches httpcore only, ASGI client untouched); after make openapi also run 'corepack pnpm gen:api' in sdks/typescript and apps/dashboard (schema.test.ts compares)
<!-- tags: api, testing, openapi | created: 2026-10-29 -->

### mem-1793269535-3669
> MFA test helpers: session_helpers.enrol_mfa(client, container) + mfa_code(container, secret); MfaGuard replay guard accepts each TOTP step once per user, so advance FrozenClock 30 s before reusing the current code in another session; gate is session_auth.gated_membership (org OR user MFA), counters with lock reuse auth.failure_counter.FailureCounters
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793268366-7e50
> SQLAlchemy model column added by a later ALTER TABLE ADD COLUMN: give it mapped_column(..., sort_order=1) so it sorts after TimestampMixin created_at/updated_at like the DB attnum (test_tenancy_models column-order lists); Mapped[list[str]] .any(x) fails mypy, use literal(x, Text) == any_(col)
<!-- tags: database, sqlalchemy | created: 2026-10-29 -->

### mem-1793267153-d5f5
> va_api: routes added to an existing router from a second module (routers/auth_password.py on internal_auth_router) need an import-for-side-effect in va_api/main.py; shared test helper session_helpers.error_of(response) -> (status, code, field) - don't redefine _error per module (jscpd)
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793265656-71f0
> va_api session integration fixtures (migrated_url module-scope, identity, container=SessionContainer(identity=...), client) live in apps/api/tests/conftest.py; modules only add extras (member). Copying them per module trips jscpd. FastAPI drops None values from OpenAPI examples; MFA_REQUIRED is HTTP 401
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793264176-ecbf
> fakeredis connection-failure stub: server=fakeredis.FakeServer(); server.connected=False; FakeAsyncRedis(server=server) raises ConnectionError for commands AND pipelines (overriding execute_command does not cover pipeline.execute)
<!-- tags: testing, redis | created: 2026-10-29 -->

### mem-1793264176-b270
> va_api session tests: serve the app over https (make_client(app, base_url='https://...')) - cookies are Secure outside VA_ENV=local and httpx drops Secure cookies on http; httpx ASGITransport peer is 127.0.0.1 but Starlette TestClient's is 'testclient' (not an IP; IdentityRepo.create_session raises ValueError on it, so use session_auth.client_ip)
<!-- tags: api, auth, testing | created: 2026-10-29 -->

### mem-1793262412-7bed
> va_db identity tests: identity_db fixture (conftest, support/identity_db.py) gives Database + local-KMS Envelope + OrgProvisioner; row_counts(db) asserts provisioning atomicity. Pool size is 1 per role, so never nest two app sessions (tenant/global) in one test (it deadlocks on pool checkout)
<!-- tags: database, testing | created: 2026-10-29 -->

### mem-1793261328-920d
> va_common.email.EmailSettings lives in the email package, not va_common.settings: va_common.logging imports va_common.settings, so settings -> email.sender -> logging is a cycle. check_no_placeholders flags example.com in production docstrings
<!-- tags: email, settings | created: 2026-10-29 -->

### mem-1793260371-4c41
> Idempotency decision logic is va_db.repositories.resolve_existing (pure); test MemoryIdempotencyStore reuses it - never re-copy the decision chain (jscpd)
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793260371-1459
> va_api pure-ASGI middleware: subclass va_api.middleware.HttpMiddleware (http-only passthrough) and override handle(); jscpd flags a copied 'if scope[type] != http' preamble
<!-- tags: api, tooling | created: 2026-10-29 -->

### mem-1793258976-9d0b
> va_api.rate_limit must not import va_api.auth at runtime (va_api.auth.dependencies imports it): RequestContext is TYPE_CHECKING-only; rate_limit("calls_send") dependency lives in va_api.auth.dependencies
<!-- tags: api | created: 2026-10-29 -->

### mem-1793257682-de8e
> Adding an env var to specs/02 §6: update scripts/tests/test_env_example.py EXPECTED_SPEC_NAMES, EXPECTED_LITERAL_KEYS and the DevValueKind count dict, plus the '106 names in 105 rows' counts in the §6 Decision text
<!-- tags: tooling, testing | created: 2026-10-29 -->

### mem-1793257682-8f0e
> FastAPI in this repo nests included routers in app.routes as fastapi.routing._IncludedRouter (no .path); enumerate APIRoutes via app.state.routers (route.dependant for require_scope.required_scope)
<!-- tags: api, testing | created: 2026-10-29 -->

### mem-1793135968-8be6
> clickhouse-connect 1.11 async: create_async_client does a network round-trip (server version/settings) so open it in a supervised task; network errors and retried server errors raise OperationalError (subclass of DatabaseError); AsyncClient satisfies va_events ClickHouseCommands Protocol; server-side {name:Type} params work in INSERT ... SELECT
<!-- tags: clickhouse, testing | created: 2026-10-27 -->

### mem-1793130055-a7eb
> va_db schema checks: schema_catalog.snapshot() compares spec-applied vs migrated DB; autogenerate_diff (alembic 1.20.1) has zero noise on expression/partial/hnsw indexes, vector(1024), server defaults - any new exclusion must be a named commented exception in schema_catalog.include_object; Postgres "char" columns (contype, attgenerated) come back as bytes via asyncpg - cast ::text
<!-- tags: database, alembic, testing | created: 2026-10-27 -->

### mem-1793125348-d9c3
> va_db model tests: shared helpers in model_introspection (assert_org_indexed, org_index_ddl, enum_check_names, expected_foreign_keys, assert_enum_checks_mirror) - use them instead of per-file parametrized copies, jscpd flags the 10-line windows
<!-- tags: database, testing | created: 2026-10-27 -->

### mem-1793122444-3076
> va_db partitioned tables (call_events): PrimaryKeyConstraint must include partition key; __table_args__ trailing dict {'postgresql_partition_by': 'RANGE (created_at)'}; server-default-only PK part (created_at) is filled by asyncpg RETURNING at flush; create_all makes only the parent so tests CREATE TABLE ... PARTITION OF ... FOR VALUES FROM (date_trunc('month', now())) TO (...) themselves
<!-- tags: database, sqlalchemy, testing | created: 2026-10-27 -->

### mem-1793120870-f54b
> va_db models: use _columns builders (org_id_column, fk_column, int_column, bool_column, text_column, text_array_column, jsonb_column) - jscpd flags repeated mapped_column blocks; overlapping import headers can be wrapped in '# jscpd:ignore-start' / '# jscpd:ignore-end  (reason)' (bare end marker trips ruff ERA001)
<!-- tags: database, testing, tooling | created: 2026-10-27 -->

### mem-1793119288-abc7
> va_db model tests: reuse tests/support/model_introspection (get_table etc.) + model_factories (make_org, assert_enum_check_enforced) and conftest scratch_session; copying tenancy test bodies trips jscpd clone gate
<!-- tags: database, testing | created: 2026-10-27 -->

## Decisions

### mem-1793128976-28d0
> Every new migration must GRANT SELECT, INSERT, UPDATE, DELETE on new tables to va_app, va_system and add §10 trigger/RLS; RLS = NOT NULL org_id + voices/dnc_entries; call_events partitions call_events_pYYYYMM (UTC months)
<!-- tags: database, alembic | created: 2026-10-27 -->

## Fixes

### mem-1793333516-86db
> make test flake: apps/api integration tests (test_cross_tenant_routes, test_dev_seed) can exceed the 60 s pytest-timeout at host load avg ~8-11 with coverage on; they pass alone in ~5 s - re-run before rejecting unrelated work, tracked as task fix:api-test-timeout-load
<!-- tags: testing, api, tooling | created: 2026-10-30 -->

### mem-1793332692-ff3b
> flake: apps/api/tests/test_dev_seed.py::test_first_run_creates_org_owner_credit_and_working_keys can hit the 60 s pytest-timeout in make check when host load avg is ~8 (stack ends in coverage tracer, not an I/O wait); it passes in ~6 s alone - re-run before treating it as a regression
<!-- tags: testing, api | created: 2026-10-30 -->

### mem-1793331520-1dbe
> pip-audit on this repo: uv export --no-hashes --no-emit-workspace --format requirements-txt > /var/tmp/req.txt; uvx pip-audit --strict -r /var/tmp/req.txt (without --no-emit-workspace pip-audit fails installing workspace editables)
<!-- tags: tooling, audit | created: 2026-10-30 -->

### mem-1793330327-5c3f
> va_sim tests: two telnyx_client blocks streaming to MediaAgents inside ONE test hang until pytest-timeout (pre-existing, reproduced on old code); split into separate tests
<!-- tags: sim, testing | created: 2026-10-30 -->

### mem-1793328571-1e58
> va_api tests: structlog capture_logs misses cached module loggers in the full xdist suite (test passes alone, fails in make check); monkeypatch the module's _log with a small recording object instead
<!-- tags: api, testing | created: 2026-10-30 -->

### mem-1793321388-bb1d
> pip-audit gate: run 'uv export --no-hashes --no-emit-workspace > /var/tmp/req.txt && uvx --python 3.12 pip-audit --strict -r /var/tmp/req.txt'; without --python 3.12 uvx uses 3.13 and fails on workspace packages (requires-python <3.13)
<!-- tags: tooling, audit | created: 2026-10-30 -->

### mem-1793319405-8587
> Python 3.12 asyncio.gather on already-done tasks completes synchronously (no loop yield): a 'while tasks: await gather(*tasks)' drain loop spins forever before done callbacks run - add await asyncio.sleep(0) per iteration (va_sim.tasks.Supervisor.drain)
<!-- tags: async, testing, sim | created: 2026-10-30 -->

### mem-1793294019-7281
> email_validator.validate_email(check_deliverability=False, test_environment=True) accepts the reserved .test domain but REJECTS special-use .local (and dotless hosts); dev seed owner is dev@voiceagents.test. make recipes echo their command to stdout - prefix with @ when stdout must be machine-readable (make seed ARGS=--json)
<!-- tags: api, tooling | created: 2026-10-29 -->

### mem-1793288415-240d
> pip-audit on uv export fails installing workspace packages; use 'uv export --no-hashes --no-emit-workspace --format requirements-txt' then 'uvx pip-audit --no-deps --disable-pip -r file'
<!-- tags: tooling | created: 2026-10-29 -->

### mem-1793280879-cb08
> Never run 'ruff check --fix --select RUF100' (or any --select incl. RUF100) on the repo: noqa for unselected rules count as unused and get stripped repo-wide (P02-T09a). Run plain 'make lint' / ruff --fix without --select
<!-- tags: tooling, ruff | created: 2026-10-29 -->

### mem-1793279423-b07e
> pydantic: Field(repr=False) inside an Annotated alias used as 'Alias | None' raises UnsupportedFieldAttributeWarning (error under filterwarnings) at app build; put Field(default=None, min_length=..., repr=False) on the attribute instead (P02-T08c AcceptInvitationRequest.password)
<!-- tags: api, pydantic, testing | created: 2026-10-29 -->

### mem-1793273041-6642
> Ralph build.done backpressure: every gate must be 'key: pass' with a colon (tests:, lint:, typecheck:, audit:, coverage:, complexity: <number>, duplication:, specs:); 'tests pass' / 'cov 99%' / 'jscpd 0 clones' converts to build.blocked (P02-T07b)
<!-- tags: tooling, ralph | created: 2026-10-29 -->

### mem-1793272997-f20d
> authlib 1.9: importing authlib.integrations.httpx_client (AsyncOAuth2Client) warns AuthlibDeprecationWarning (wants httpx2) -> error under filterwarnings; use authlib.oauth2 rfc6749/rfc7636 primitives via va_api.auth.oidc_client + OidcDiscovery.post_form, joserfc for JWTs; mypy override authlib.* ignore_missing_imports
<!-- tags: api, auth, tooling | created: 2026-10-29 -->

### mem-1793269824-fe85
> Ralph build.done backpressure needs a NUMERIC complexity value: 'complexity: 3.17' (radon cc -s -a average). 'complexity: A' or 'complexity: pass (...)' converts to build.blocked even in a plain-string payload (P00-FIX-1, P02-T06b)
<!-- tags: tooling, ralph | created: 2026-10-29 -->

### mem-1793267153-9b68
> pip-audit on this uv workspace: uv export --no-hashes --no-emit-workspace --format requirements-txt > logs/req.txt && uvx pip-audit --strict --no-deps --disable-pip -r logs/req.txt (plain export includes -e workspace pkgs and pip install --dry-run fails)
<!-- tags: tooling, audit | created: 2026-10-29 -->

### mem-1793262412-b2f5
> ApiSettings includes CryptoSettings: settings tests with va_env=production must also pass kms_provider=aws + aws_kms_key_id, otherwise 'KMS_PROVIDER=local is refused' fires before the validator under test
<!-- tags: api, settings, testing | created: 2026-10-29 -->

### mem-1793258976-544c
> Ruff noqa for a multi-line triple-quoted string (e.g. S105 on TOKEN_BUCKET_LUA) goes after the closing quotes; text after the opening quotes is part of the string (here, the Lua source)
<!-- tags: tooling, ruff | created: 2026-10-29 -->

### mem-1793257682-c402
> SQLAlchemy 2.1: Row.tuple() and Row._tuple() both raise SADeprecationWarning (error under filterwarnings); unpack the Row directly: a, b, c = row
<!-- tags: database, sqlalchemy | created: 2026-10-29 -->

### mem-1793139282-8c73
> failure: make test during OrbStack Sparkle auto-update -> 120 errors 'failed to connect to the docker API ... orbstack/run/docker.sock'; the updater stops the VM again after 'orb start'. Finish the OrbStack update first (check 'pgrep -lf Autoupdate', 'orb status'), then rerun
<!-- tags: testing, docker | created: 2026-10-27 -->

### mem-1793138363-ca2f
> make test flake (P01-T13 build): 3 of 5 full runs hit pytest-timeout 60s in va_db setup (migrated_url upgrade, scratch_session) + Docker read timeouts; Docker VM is 3.9 GiB running the full compose stack, host load ~8. Isolated rerun green; partition code never failed
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793135968-c35b
> make test flake variant (P01-T12 build): ClickHouseContainer docker create exceeded the 60s pytest-timeout while 8 xdist Postgres containers booted; the va_events integration test has pytest.mark.timeout(180) and is a single test per module (one container per xdist worker)
<!-- tags: testing, docker, clickhouse | created: 2026-10-27 -->

### mem-1793134471-2c7d
> make test flake variant (P01-T11 build): 138 errors = pytest-timeout 60s in conftest pg_url -> pg_container.running_postgres (testcontainers boot) right after compose make migrate + alembic round-trip; isolated rerun 1756 passed. Docker daemon latency, same family as scratch_session timeout
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793133155-6419
> SQLAlchemy AsyncSession.begin_nested() autoflushes pending changes into the OUTER transaction before the savepoint; make add()/setattr inside the begin_nested block or an IntegrityError aborts the caller's tenant transaction. Failed savepoint expires touched objects (attribute access -> MissingGreenlet)
<!-- tags: database, sqlalchemy | created: 2026-10-27 -->

### mem-1793131606-2dff
> va_db scratch_session timeout recurred in P01-T09 build (make test run alone, gw4/gw6): asyncpg TimeoutError at conftest CREATE EXTENSION IF NOT EXISTS citext and at create_all - looks like a lock wait/server stall on pg_url DB, not CPU; rerun green. RLS harness (tenant_rows) uses migrated_url DBs only
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793129328-3035
> make test flake: pytest-timeout 60s in va_db scratch_session setup (2 errors) when host is busy (jscpd/compose concurrently); isolated rerun green. Seen in P01-T06 and P01-T07 reviews - don't run heavy jobs alongside make test; root-cause pending
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793128976-0fb7
> va_db test Postgres runs with fsync=off: DROP DATABASE forces a checkpoint and timed out (asyncpg 30s) with 8 xdist containers; also never boot an extra container in a test - Docker daemon create timed out (60s) under full make test; rename roles aside instead
<!-- tags: database, testing, docker | created: 2026-10-27 -->

### mem-1793128976-c741
> Alembic+asyncpg: op.execute(str) wraps in text() (':0' in JSON literal = bind param) and asyncpg prepares each statement (no multi-statement strings); va_db migrations use execute_script(sql) from 0001_initial (splits on top-level ';', escapes colons)
<!-- tags: database, alembic | created: 2026-10-27 -->

### mem-1793125348-9962
> va_db tests: 'ruff check --fix --select RUF100' strips '# noqa: F401  (reason)' comments whole from import-for-side-effect lines (conftest, model_introspection) - never run RUF100 autofix on va_db; restore with git checkout
<!-- tags: tooling, ruff | created: 2026-10-27 -->

### mem-1793120870-bd75
> SQLAlchemy text() treats ':0'/':true' inside JSON literals as bind params; JSONB server defaults must go through va_db.models._columns.jsonb_default (escapes colons). Also Mapped[Any] makes a column NOT NULL - use Mapped[Any | None] + nullable=True
<!-- tags: database, sqlalchemy | created: 2026-10-27 -->

### mem-1793118560-6911
> SQLAlchemy 2.1 + asyncpg returns inet columns as ipaddress.IPv4Address/IPv6Address (not str); annotate Mapped[IpAddress | None] from va_db.models
<!-- tags: database, sqlalchemy | created: 2026-10-27 -->

### mem-1793117779-05cf
> va_db assumes va_app/va_system via asyncpg connect_args server_settings={'role': ...} (startup parameter = session default). Never SET ROLE in a pool connect listener: it runs in an implicit txn and a rolled-back first txn (exception or engine.connect() reset-on-return) reverts to the login role => RLS bypass for superuser login
<!-- tags: database, testing | created: 2026-10-27 -->

### mem-1793117599-1425
> Integration tests silently skipped on OrbStack (no DOCKER_HOST, socket at ~/.orbstack/run/docker.sock): docker_probe.docker_available() now uses testcontainers.core.docker_client.get_docker_host() (docker context aware). Use testcontainers.community.<name> imports; legacy testcontainers.redis/postgres raise DeprecationWarning (error under filterwarnings)
<!-- tags: testing, docker | created: 2026-10-27 -->

### mem-1792988928-d413
> Exit-gate clean clones must live under /Users (Docker Desktop cannot bind-mount from /var/tmp: collector.yaml 'not a directory'); compose project name is fixed 'voiceagents' so a clone's make up replaces the main stack - make down/up from main repo after. uv run --no-project --python 3.12 prepends /opt/homebrew/bin to PATH, shadowing nvm node 22 with brew node 26
<!-- tags: tooling, docker, ci | created: 2026-10-26 -->

### mem-1792988571-0143
> Never importlib.reload an app module in tests: it rebinds module globals (app) in place and breaks identity asserts in tests that imported the old object (xdist order-dependent). Use monkeypatch.delitem(sys.modules, mod) + setattr(pkg, name, orig) then import_module for a fresh copy
<!-- tags: testing, pytest | created: 2026-10-26 -->

### mem-1792988312-0c44
> gitleaks full-history scan: plan/phase-20.md:48 'key rotation, Telnyx/ElevenLabs' is a generic-api-key false positive, allowlisted in .gitleaks.toml (path+match AND); the word 'placeholders' in .py trips check_no_placeholders filler-word rule unless written as check_no_placeholders
<!-- tags: tooling, ci | created: 2026-10-26 -->

### mem-1792987695-026b
> pnpm audit in TS workspace: vitest 3.x pulls critical tinypool advisories (use vitest ^4.1.12); next 15.5 pins postcss 8.4.31 (override postcss@<8.5.23 in root package.json pnpm.overrides); Next 15.5 next-env.d.ts references .next/types so git-ignore it
<!-- tags: tooling, typescript, pnpm | created: 2026-10-26 -->

### mem-1792986234-2b28
> uv_build workspace app whose import package differs from the dist name needs [tool.uv.build-backend] module-name = "va_x" in its pyproject, else uv sync fails 'Expected a Python module at src/va_<dist>'
<!-- tags: tooling, uv | created: 2026-10-26 -->

### mem-1792986234-b9d2
> uvicorn Server.serve calls sys.exit() on startup failure; inside an asyncio task SystemExit escapes the loop - catch it in the coroutine (va_common.service._serve -> ServerStartupError). Cancelling a running uvicorn task without shutdown leaks its listening socket (pytest unraisable warning in a later test)
<!-- tags: testing, uvicorn, service | created: 2026-10-26 -->

### mem-1792982690-b4e4
> bash 3.2 (stock macOS /bin/bash) under set -u errors on "${arr[@]}" when arr is empty; guard with ${#arr[@]} checks; test scripts with /bin/bash too
<!-- tags: tooling, bash | created: 2026-10-26 -->

### mem-1792981166-10bf
> aioboto3 session.client(config=...) needs aiobotocore.config.AioConfig; mypy rejects botocore.config.Config
<!-- tags: aws, mypy | created: 2026-10-26 -->

### mem-1792981166-ce01
> pytest-xdist: never parametrize with random values (os.urandom) - workers collect different test IDs and the run errors; parametrize sizes and generate inside the test
<!-- tags: testing, pytest | created: 2026-10-26 -->

### mem-1792920384-602e
> build.done backpressure parser substring-matches plain text tokens 'tests: pass', 'lint: pass', 'typecheck: pass', 'audit: pass', 'coverage: pass', 'duplication: pass', 'complexity: <number>'; emit build.done as a plain string payload, NOT --json (JSON keys like "tests":"pass" never match)
<!-- tags: tooling, ralph | created: 2026-10-25 -->

### mem-1792920274-acc2
> build.done payload must include backpressure keys tests, lint, typecheck, audit, coverage, complexity, duplication (performance/specs optional) or Ralph converts it to build.blocked; obtain audit via uvx pip-audit on uv export + pnpm audit, complexity via uvx radon cc, duplication via jscpd
<!-- tags: tooling, ralph | created: 2026-10-25 -->

### mem-1792920236-93ac
> ruff>=0.17 formats python code blocks in Markdown; root pyproject extend-exclude includes *.md to protect specs/
<!-- tags: tooling, ruff | created: 2026-10-25 -->

## Context
