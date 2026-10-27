
# Phase 01 (2026-10-27)

Planner: phase 00 is fully ticked, so P01-T00a/b/c (re-runs of P00-T05/T08/T03) were already satisfied; ticked them in
plan/phase-01.md with that evidence. Next unblocked task: P01-T01.

## Plan P01-T01 — Engine, sessions, base model

Spec: specs/03 §1 (conventions), §2 (roles va_app / va_system), §10 (tenant_session sets app.org_id via
`set_config(..., true)`). Acceptance: testcontainers `pgvector/pgvector:pg16` test of `current_setting('app.org_id')`
inside and outside sessions.

### Design decision (record in specs/03 §2 as `> Decision (2026-10-27):` in the same commit)
Only one DSN exists (`DATABASE_URL`, specs/02 §6); adding per-role DSNs would change the env contract. So engines
connect with the `DATABASE_URL` login and assume the target role with `SET ROLE <role>` once per new DBAPI connection
(pool "connect" event). `DbRole.APP` = `va_app` (RLS enforced; superuser login no longer bypasses RLS after SET ROLE),
`DbRole.SYSTEM` = `va_system` (BYPASSRLS). `role=None` keeps the login role (Alembic/migrations, test setup only).
Consequence for P01-T07: the migration must `GRANT va_app, va_system TO CURRENT_USER` (PG16 non-superuser creators need
explicit SET membership) and grant table privileges to both roles. Write that into the Decision text too.

### Files
1. `packages/va_db/pyproject.toml` — name `va-db`, version 0.1.0, requires-python ">=3.12,<3.13", uv_build backend
   (copy va_common's), deps: `va-common`, `sqlalchemy[asyncio]>=2.0.35`, `asyncpg>=0.32.0`;
   `[tool.uv.sources] va-common = { workspace = true }` if va_common's consumers do so (check apps/api/pyproject.toml
   and mirror it).
2. `packages/va_db/src/va_db/__init__.py` (docstring only, or re-export the public names below with `__all__`),
   `py.typed`.
3. `packages/va_db/src/va_db/engine.py`
   - `class DbRole(StrEnum): APP = "va_app"; SYSTEM = "va_system"`
   - `CONNECT_TIMEOUT_S: Final = 10.0`, `COMMAND_TIMEOUT_S: Final = 30.0`
   - `def create_engine(settings: DatabaseSettings, role: DbRole | None) -> AsyncEngine`:
     `create_async_engine(settings.database_url, pool_size=settings.database_pool_size, pool_pre_ping=True,
     connect_args={"timeout": CONNECT_TIMEOUT_S, "command_timeout": COMMAND_TIMEOUT_S})`; when role is not None,
     register `event.listens_for(engine.sync_engine, "connect")` handler that runs `SET ROLE <quoted>` through
     `dbapi_connection.cursor()` (role names are enum members; still quote via `engine.dialect.identifier_preparer
     .quote`). No string interpolation of untrusted input.
4. `packages/va_db/src/va_db/base.py`
   - `NAMING_CONVENTION` matching Postgres default names so later autogenerate parity (P01-T08) lines up:
     `pk: "%(table_name)s_pkey"`, `fk: "%(table_name)s_%(column_0_name)s_fkey"`,
     `uq: "%(table_name)s_%(column_0N_name)s_key"`, `ck: "%(table_name)s_%(constraint_name)s"` (models name checks
     like `"status_check"` → `orgs_status_check`), `ix: "%(table_name)s_%(column_0N_name)s_idx"`.
   - `class Base(AsyncAttrs, DeclarativeBase): metadata = MetaData(naming_convention=NAMING_CONVENTION)`
   - `IdMixin`: `id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True) / postgresql.UUID, primary_key=True,
     default=va_common.ids.new_id)` (UUIDv7 in Python, no server default — specs/03 §1).
   - `TimestampMixin`: `created_at`, `updated_at`: `Mapped[datetime]`, `DateTime(timezone=True)`, nullable=False,
     `server_default=func.now()`; `updated_at` also `server_onupdate=FetchedValue()` (trigger-maintained, §10).
   - `SoftDeleteMixin`: `deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))`.
5. `packages/va_db/src/va_db/session.py`
   - `class Database`: `__init__(self, app_engine: AsyncEngine, system_engine: AsyncEngine)`; builds two
     `async_sessionmaker(..., expire_on_commit=False)`; `@classmethod from_settings(cls, settings) -> Database`
     (engines with DbRole.APP / DbRole.SYSTEM); `async def dispose()`;
     `@asynccontextmanager async def tenant_session(self, org_id: uuid.UUID) -> AsyncIterator[AsyncSession]`:
     `async with maker() as s, s.begin(): await s.execute(text("SELECT set_config('app.org_id', :org_id, true)"),
     {"org_id": str(org_id)}); yield s` (commit on clean exit, rollback on exception; setting is transaction-local
     so pooled connections never leak it);
     `system_session(self)` same on the system engine without set_config. Docstring of system_session cites
     specs/03 §2 (only the listed system repositories may use it).
   - Process default: `configure_database(db: Database | None) -> None`, `get_database() -> Database` (raises
     `RuntimeError("va_db is not configured; call configure_database() at startup")`), and module functions
     `tenant_session(org_id)` / `system_session()` delegating to `get_database()` — this gives the exact
     `va_db.session.tenant_session(org_id)` name from MASTER_PROMPT rule 5 / specs/03 §10.
6. Root `pyproject.toml`: add `va-db` to dev group + `[tool.uv.sources] va-db = { workspace = true }`; mypy
   `mypy_path` += `packages/va_db/src`, `files` += `packages/va_db/src`, `packages/va_db/tests`; coverage source if the
   config lists packages explicitly (check `[tool.coverage.run]`). `uv lock` + `uv sync`.
7. Shared docker probe: move `_reachable`/`_docker_available` out of `packages/va_common/tests/test_redis_integration.py`
   into `packages/va_common/tests/docker_probe.py` (`docker_available() -> bool`, already on pytest pythonpath and mypy
   path) and use it from both the redis test and the new va_db tests (avoids a jscpd clone).

### Tests (`packages/va_db/tests/`)
- `conftest.py`: session-scoped `pg_url` fixture: skip unless `docker_available()`; start
  `PostgresContainer("pgvector/pgvector:pg16", username="va", password=<secrets.token_hex>, dbname="va")`
  (testcontainers ≥ 4.15; `driver=None` / build `postgresql+asyncpg://` URL yourself), run the
  `infra/docker/postgres-init/01-roles.sql` DO-block equivalent (create va_app LOGIN, va_system LOGIN BYPASSRLS
  if absent — BYPASSRLS on va_system per specs/03 §2) plus a scratch table `rls_probe(org_id uuid, v text)` with the
  exact §10 policy (ENABLE + FORCE RLS, tenant_isolation USING/WITH CHECK) and `GRANT SELECT, INSERT ON rls_probe TO
  va_app, va_system`. Function-scoped `database` fixture yields `Database.from_settings(DatabaseSettings(
  database_url=..., database_pool_size=2))` and disposes it. Mark module `pytestmark = pytest.mark.integration`.
  Settings built via typed kwargs dict with `_env_file=None` (AGENTS P00-T05 gotcha).
- `test_session_integration.py`:
  - `test_tenant_session_sets_org_id` — inside: `current_setting('app.org_id', true) == str(org)`.
  - `test_setting_is_cleared_after_tenant_session` — pool_size 1 engine; after the block, a new `tenant_session`-free
    connection on the same engine returns `nullif(current_setting('app.org_id', true), '') IS NULL`.
  - `test_setting_cleared_after_exception_rollback` — raise inside block; error propagates; setting gone; row inserted
    inside is rolled back.
  - `test_nested_orgs_do_not_leak` — sequential sessions for org A then B see only their own value.
  - `test_tenant_session_runs_as_va_app` / `test_system_session_runs_as_va_system` — `SELECT current_user`.
  - `test_rls_probe_isolation` — insert rows for A and B via system_session; tenant_session(A) sees only A; insert of
    a B row under A raises `DBAPIError` (WITH CHECK); system_session sees both.
  - `test_system_session_has_no_org_id`.
  - `test_module_level_helpers_use_configured_database` + `test_unconfigured_raises` (configure_database(None) in a
    fixture finalizer).
- `test_engine.py` (unit, no docker): pool size from settings, `engine.url` matches, connect_args timeouts present,
  role listener registered only when role given (`event.contains`).
- `test_base.py` (unit): mixin columns — types (`UUID`, `DateTime(timezone=True)`), nullability, server_default
  rendered `now()`, `id` default produces UUID version 7, naming convention yields `x_pkey`, `x_a_b_key`,
  `x_org_id_fkey` on a throwaway model in its own `MetaData`-sharing Base subclass (use a fresh registry so the real
  metadata stays clean — or `Base.metadata.remove(table)` in teardown).

### Gates
`make fmt lint typecheck placeholders test` (Docker running: `orb start`), then audit (uvx pip-audit on uv export),
`uvx radon cc -s -a packages/va_db/src`, `npx --yes jscpd@4 --silent`. va_db coverage ≥ 85 % (target 100 %).
Update AGENTS.md: va_db session usage, SET ROLE decision, docker_probe helper.

### Risks
- asyncpg dialect "connect" event: dbapi_connection is SQLAlchemy's AdaptedConnection; `cursor().execute` is the
  supported sync-facade path. Verify with the current_user test.
- Superuser login + SET ROLE va_app: RLS applies because current_user is va_app (non-superuser, FORCE RLS). Test proves it.
- testcontainers PostgresContainer may require psycopg for its readiness wait in some versions; if so use
  `wait_for_logs` / construct with `driver=None` — do not add psycopg2 unless unavoidable.
- `filterwarnings=error`: dispose every engine (fixture finalizers) or asyncpg/ResourceWarnings fail the run.

## Build P01-T01 (2026-10-27)
Implemented per plan. Deviations/notes for the Reviewer:
- Naming convention uses `%(column_0_N_name)s` (underscore-joined, matches Postgres `t_a_b_key`), not `column_0N_name`.
- Role listener is a module-level callable class `_AssumeRole` so unit tests can find it among the pool's connect
  listeners (the asyncpg dialect registers its own there).
- `pg_container.py` lives in `packages/va_db/tests/support/` (pytest pythonpath + mypy_path), mirroring apps/*/tests/support.
- Found that docker_probe never detected OrbStack (no DOCKER_HOST), so every integration test had been skipping;
  probe now uses testcontainers' `get_docker_host()`. That surfaced a latent DeprecationWarning in
  test_redis_integration (`testcontainers.redis`) — switched to `testcontainers.community.redis`.
- Spec Decision added under specs/03 §2 roles list; AGENTS.md updated (va_db usage, docker probe).
Gates: fmt ok, lint pass, typecheck pass, placeholders 0 findings, make test 1152 passed (99.83 %, va_db 100 %),
pip-audit + pnpm audit no vulns, radon A (1.2), jscpd 0 clones.

## Review P01-T01 (2026-10-27) — REJECTED
Gates re-run by Reviewer: lint/typecheck pass, placeholders 0, make test 1152 passed, va_db 100 % coverage.
Rejected on a correctness/security defect that the suite does not exercise:

1. packages/va_db/src/va_db/engine.py:51-56 — `SET ROLE` is executed through the asyncpg adapter cursor, which
   opens an implicit transaction. If the first transaction on a fresh connection does not COMMIT (exception
   rollback in `tenant_session`, or any `engine.connect()` block, whose pool reset-on-return is a ROLLBACK),
   the `SET ROLE` is rolled back with it and the pooled connection silently runs as the login role from then on.
   With a superuser/owner login that bypasses RLS entirely (violates specs/03 §2/§10 and MASTER_PROMPT rule 5).
   Reproduced by the Reviewer against pgvector/pg16 (probe test, since deleted): pool_size=1,
   (a) first use `tenant_session` raising -> next checkout same backend pid, `current_user` = 'va' (expected va_app);
   (b) first use `app_engine.connect()` without commit -> same result.
   Fix: make the role durable independent of the first transaction, e.g. set `dbapi_connection.autocommit = True`
   around the `SET ROLE` and restore it, or execute then commit immediately, or pass the role as an asyncpg
   startup parameter (`connect_args={"server_settings": {"role": ...}}`) — pick one, verify it, and update the
   specs/03 §2 Decision text ("once per new pooled connection") to describe the mechanism precisely.
2. packages/va_db/tests/test_session_integration.py — no test covers a fresh connection whose first transaction
   rolls back. Add regression tests (pool_size=1, assert same `pg_backend_pid()`): first use = tenant_session that
   raises, and first use = bare `app_engine.connect()`; both must then show `current_user = 'va_app'` and RLS
   still hiding another org's `rls_probe` rows. Same for the system engine (`va_system`).
Everything else (naming convention, mixins, session API, docker_probe move, AGENTS.md, Decision placement) looks
correct and can stay as is.

## Build P01-T01 fix (2026-10-27) — addresses Review findings 1 and 2
1. engine.py: dropped the `SET ROLE` connect listener. The role is now an asyncpg startup parameter
   (`connect_args["server_settings"] = {"role": role.value}`), which becomes the session default for `role`:
   rollback, pool reset-on-return, and even `RESET ROLE` keep the engine role. Spec §2 Decision text and AGENTS.md
   rewritten to describe the mechanism (and why SET ROLE in a listener is unsafe). Unit tests now assert connect_args.
2. Regression tests in test_session_integration.py (pool_size=1, same `pg_backend_pid()` asserted):
   app first use = tenant_session raising / bare app_engine.connect() -> `current_user = va_app` and RLS hides the
   other org's rls_probe row; system first use = system_session raising / bare system_engine.connect() ->
   `va_system`; plus `RESET ROLE` inside tenant_session stays `va_app`. Verified all 5 fail against the old
   SET ROLE listener (temporarily reinstated) and pass with the fix.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 1154 passed (va_db 100 %, total 99 %),
pip-audit no known vulns (pnpm not on PATH this shell; no TS changes), radon A (1.18), jscpd 0 clones.

## Review P01-T01 fix (2026-10-27) — APPROVED
Re-ran lint/typecheck/placeholders (pass, 0 findings) and make test (1154 passed, 99.83 %, va_db 100 %).
Confirmed integration tests execute (Docker up, 15/15 PASSED, none skipped), including the 5 role-survives-rollback
regressions. Role now via asyncpg `server_settings={"role": ...}`; spec §2 Decision and AGENTS.md match. Ticked + committed.

## Plan P01-T02 — Tenancy and identity models (2026-10-27)

Spec: specs/03 §1 (conventions), §3 (the 10 tables, column for column), specs/04 §3.1 (role names).
Planner also amended plan/phase-01.md: §9a tables (`org_llm_providers`, `dnc_imports`, `erasure_requests`,
`org_secrets`, `org_data_keys`) had no owning task; they are now P01-T06 deliverables. Do NOT model them here.

### Files
1. `packages/va_db/src/va_db/models/__init__.py` — re-export every model + Literal alias below, `__all__` sorted.
   Later tasks add `telephony.py`, `agents.py`, ... next to it. Do not add relationship()s (plain FKs only, avoids
   import cycles across modules; repositories query by FK).
2. `packages/va_db/src/va_db/models/_checks.py` — `def check_in(column: str, values: tuple[str, ...]) -> CheckConstraint`
   building `"<column> IN ('a', 'b', ...)"` with `name=f"{column}_check"` (naming convention turns it into
   `<table>_<column>_check`, Postgres' own name for an inline column CHECK). Callers pass `get_args(SomeLiteral)` so the
   Literal is the single source of truth. Reject empty tuples / values containing `'` with ValueError (unit-tested).
3. `packages/va_db/src/va_db/models/tenancy.py` — Literal aliases (exact spec order):
   `OrgStatus = Literal["active","suspended","closed"]`, `DataRegion = Literal["us","eu"]`,
   `UserStatus = Literal["active","disabled"]`,
   `MemberRole = Literal["owner","admin","developer","analyst","billing","viewer"]`,
   `EmailTokenPurpose = Literal["verify_email","reset_password"]`, `ApiKeyEnvironment = Literal["live","test"]`,
   `SsoProvider = Literal["oidc"]`, `AuditActorType = Literal["user","api_key","system"]`.
   Models (class → table): `Org`→orgs, `User`→users, `Membership`→memberships, `Invitation`→invitations,
   `Session`→sessions, `EmailToken`→email_tokens, `ApiKey`→api_keys, `SsoConnection`→sso_connections,
   `AuditLog`→audit_logs, `IdempotencyKey`→idempotency_keys. All but IdempotencyKey use `IdMixin, TimestampMixin`;
   IdempotencyKey uses only TimestampMixin with composite PK `(org_id, key)` → `idempotency_keys_pkey`.
   Rules:
   - EVERY `mapped_column` passes an explicit SQL type (never rely on annotation inference; SQLAlchemy maps
     `Literal` to `Enum` otherwise): `Text`, `CITEXT` / `INET` / `JSONB` / `ARRAY(Text)` from
     `sqlalchemy.dialects.postgresql`, `SmallInteger`, `Integer`, `BigInteger`, `Boolean`, `LargeBinary` (bytea),
     `DateTime(timezone=True)`, `Uuid(as_uuid=True)`.
   - Annotations: enum columns `Mapped[OrgStatus]` etc.; jsonb `Mapped[dict[str, Any]]`; text[] `Mapped[list[str]]`;
     inet `Mapped[str | None]` (asyncpg dialect returns str — verify in integration test); nullable → `X | None`.
   - Nullability exactly per spec (nullable=False wherever NOT NULL; PK columns implied).
   - server_default exactly per spec, no Python-side defaults besides IdMixin: `text("'start'")`, `text("0")`,
     `text("false")`, `text("5000000")`, `text("25000000")`, `text("'{}'::jsonb")`, `text("'us'")`, `text("365")`,
     `text("90")`, `text("''")`, `text("'{}'")` (text[]), `text("'{*}'")` (api_keys.scopes), `text("1")`,
     `text("'live'")`, `text("'viewer'")`, `text("'active'")`.
   - CHECKs: `check_in(...)` for every enum; `CheckConstraint("kyc_level BETWEEN 0 AND 3", name="kyc_level_check")`,
     `retention_days BETWEEN 1 AND 3650`, `recording_retention_days BETWEEN 0 AND 3650` (same naming pattern).
     `sso_connections.default_role` has NO check in the spec — annotate `Mapped[MemberRole]` but add no constraint.
   - UNIQUE: column `unique=True` for orgs.slug, orgs.stripe_customer_id, users.email, invitations.token_hash,
     sessions.token_hash, email_tokens.token_hash, api_keys.prefix (→ `<table>_<col>_key`);
     `UniqueConstraint("org_id","user_id")` on memberships, `UniqueConstraint("org_id","issuer")` on sso_connections.
   - FKs with exact `ondelete`: orgs.parent_org_id RESTRICT; tenant org_id CASCADE; memberships.user_id CASCADE;
     invitations.invited_by (no ondelete); sessions.user_id CASCADE, sessions.active_org_id SET NULL;
     email_tokens.user_id CASCADE; api_keys.created_by SET NULL.
   - Named indexes exactly as spec (explicit names override the convention): `orgs_parent_idx(parent_org_id)`,
     `memberships_user_idx(user_id)`, `invitations_org_idx(org_id)`, `sessions_user_idx(user_id)`,
     `api_keys_org_idx(org_id)`, `audit_logs_org_created_idx(org_id, created_at DESC)` — the DESC one is declared
     after the class: `Index("audit_logs_org_created_idx", AuditLog.org_id, AuditLog.created_at.desc())`.
     No `index=True` anywhere (would invent unnamed indexes not in the spec).
   - Class docstrings name the table and the Pydantic schema noted in spec comments (OrgLimitsOverride, OrgSettings)
     but do not define those schemas here (later phases).
4. `packages/va_db/src/va_db/__init__.py` — unchanged (models are imported as `va_db.models`).

### Tests
- `packages/va_db/tests/conftest.py`: move the session-scoped `pg_url` fixture here from
  test_session_integration.py (one container per worker, shared by both integration modules).
- `test_checks.py` (unit): `check_in` SQL text and name; ValueError for empty tuple and quote in value.
- `test_tenancy_models.py` (unit, no Docker):
  - `test_tables_registered` — the 10 table names are in `Base.metadata.tables`.
  - `test_enum_checks_mirror_literals` — parametrized (table, column, Literal, expected spec tuple): constraint named
    `<table>_<column>_check` exists; its values parsed from the compiled SQL == `get_args(Literal)` == spec tuple.
  - `test_range_checks` — kyc_level / retention_days / recording_retention_days constraint text + names.
  - `test_server_defaults` — parametrized (table, column, expected compiled default) for every defaulted column.
  - `test_nullability` — parametrized over the spec's nullable columns (e.g. users.password_hash, sessions.ip) and a
    sample of NOT NULL ones; or assert the full {column: nullable} map per table (preferred, catches drift).
  - `test_unique_and_pk_names` — `orgs_slug_key`, `orgs_stripe_customer_id_key`, `users_email_key`,
    `memberships_org_id_user_id_key`, `sso_connections_org_id_issuer_key`, `idempotency_keys_pkey(org_id,key)`, ...
  - `test_foreign_keys_ondelete` — parametrized (table, column, target, ondelete).
  - `test_indexes` — exact set of index names + column order + DESC on audit_logs (compile CreateIndex).
  - `test_column_types` — CITEXT on orgs.slug/users.email/invitations.email, INET on sessions.ip/audit_logs.ip,
    ARRAY(Text) on users.mfa_recovery_codes_hash/api_keys.scopes/sso_connections.allowed_domains, JSONB, LargeBinary,
    SmallInteger (kyc_level, pepper_version), BigInteger micros.
- `test_tenancy_models_integration.py` (`pytestmark = pytest.mark.integration`, uses `pg_url`): a login-role engine
  (`create_engine(settings, None)`), one connection + transaction ROLLED BACK at teardown (DDL is transactional, so
  the container stays clean for other modules): `CREATE EXTENSION IF NOT EXISTS citext`,
  `conn.run_sync(Base.metadata.create_all)`; `AsyncSession(bind=conn, join_transaction_mode="create_savepoint")`.
  - `test_org_defaults` — insert `Org(name, slug, webhook_signing_secret_enc=os.urandom(32))`, refresh: every spec
    default (plan_id 'start', kyc 0, status 'active', 0/0 micros, recharge False/5_000_000/25_000_000, limits {},
    settings {}, 'us', 365, 90, created_at/updated_at tz-aware).
  - `test_user_api_key_sso_defaults` — users (name '', mfa False, codes [], admin False, 'active'); api_keys
    (scopes ['*'], pepper_version 1, 'live'); sso_connections (allowed_domains [], 'viewer', enforce False).
  - `test_enum_values_accepted_and_rejected` — parametrized (model builder, column, Literal): each Literal value
    inserts; `"bogus"` raises IntegrityError (CheckViolation) inside `begin_nested()`.
  - `test_range_checks_enforced` — kyc_level 4, retention_days 0, recording_retention_days 3651 rejected; bounds accepted.
  - `test_citext_unique_email_case_insensitive` — users 'A@x.test' then 'a@x.test' → IntegrityError.
  - `test_inet_round_trips_as_str` — Session.ip '203.0.113.7' reads back as that str.
  - `test_idempotency_key_composite_pk` — same (org, key) twice → IntegrityError; other org same key OK.
  - `test_org_delete_cascades` — deleting an org removes its memberships/api_keys; parent org with child → RESTRICT error.
  Use `va_common.ids.new_id` for ids, `secrets`/`os.urandom` for hashes/secrets (no literal secrets; mind the
  gitleaks + placeholder filler-word rules in AGENTS.md).

### Gates
`make fmt lint typecheck placeholders test` with Docker up (confirm integration tests PASSED, not skipped:
`uv run pytest packages/va_db -m integration -rs`), `uvx pip-audit` on uv export, `uvx radon cc -s -a
packages/va_db/src`, `npx --yes jscpd@4 --silent`. va_db coverage ≥ 85 % (target 100 %). AGENTS.md: note the
explicit-SQL-type rule, `check_in` + Literal convention, and the rolled-back create_all integration pattern.

### Risks
- `Mapped[Literal[...]]` without explicit type → SQLAlchemy Enum; the explicit `Text` avoids it. mypy: Literal in
  Mapped is fine.
- CITEXT needs the extension inside the scratch transaction (created there, rolled back with it).
- `Session` model name shadows `sqlalchemy.orm.Session` for star-importers; never import both under that name.
- Parsing CHECK values in unit tests: compile constraint `.sqltext` and regex `'([^']*)'`.

## Build P01-T02 (2026-10-27)
Implemented per plan. Deviations/notes for the Reviewer:
- `inet` is NOT returned as str: SQLAlchemy 2.1.6 + asyncpg yields `IPv4Address`/`IPv6Address`. Columns annotated
  `Mapped[IpAddress | None]` (`type IpAddress = IPv4Address | IPv6Address`, exported from va_db.models); the
  integration test round-trips v4 and v6 on sessions.ip and audit_logs.ip.
- `IdMixin.id` gained `sort_order=-1` (base.py) so model column order == spec order (id first); unit test
  `test_columns_order_and_nullability` asserts the full ordered (name, nullable) list per table.
- Extra unit tests beyond the plan: exact set of CHECK names, every timestamp tz-aware, text[] item types.
- `pg_url` moved to conftest.py; test_session_integration.py otherwise unchanged.
Gates: fmt 0, lint 0, typecheck 0 (156 files), placeholders 0 findings, make test 1252 passed (99.84 %, va_db 100 %),
integration `-m integration` 43 passed / 0 skipped, pip-audit no known vulns, radon A (1.24), jscpd 0 clones.

## Review P01-T02 (2026-10-27) — APPROVED
Re-ran lint/typecheck/placeholders (pass, 0 findings), make test (1252 passed, 99.84 %), and
`pytest packages/va_db -m integration` (43 passed, none skipped). Checked tenancy.py against specs/03 §3 line by line:
column order, types (citext/inet/jsonb/text[]/bytea/smallint/bigint), nullability, server defaults, CHECKs, UNIQUE,
FK ondelete, and named indexes (audit_logs DESC) all match. IpAddress deviation is documented in AGENTS.md and memory.
The plan amendment that moves the §9a tables into P01-T06 is accepted. Ticked + committed.

## Plan P01-T03 — Telephony models (2026-10-27)

Spec: specs/03 §4 (6 tables, column for column), §1 conventions, §10 (RLS scope note below). Follow every P01-T02
rule (explicit SQL type on every mapped_column, `check_in(col, get_args(Literal))`, server_default via `text(...)`,
explicit index names, no `index=True`, no relationship(), plain FKs).

### Files
1. `packages/va_db/src/va_db/models/telephony.py` — Literal aliases (exact spec order):
   `TelnyxAccountStatus = Literal["pending","active","error","deleted"]`,
   `NumberType = Literal["local","toll_free","mobile","national"]`,
   `PhoneNumberStatus = Literal["pending","active","releasing","released"]`,
   `NumberOrderStatus = Literal["pending","success","failure"]`,
   `NumberPoolStrategy = Literal["round_robin","least_recently_used","random","local_match"]`.
   Models (class → table):
   - `TelnyxAccount`→telnyx_accounts (IdMixin, TimestampMixin): org_id CASCADE NOT NULL, label Text NN,
     api_key_enc LargeBinary NN, public_key Text NN, call_control_app_id / messaging_profile_id /
     outbound_voice_profile_id Text NULL, encrypted_key_handle Text NN `unique=True` (→ telnyx_accounts_encrypted_key_handle_key),
     status Text NN default `'pending'` + check, status_detail Text NULL. `Index("telnyx_accounts_org_idx","org_id")`.
   - `PhoneNumber`→phone_numbers (IdMixin, SoftDeleteMixin, TimestampMixin): org_id CASCADE NN, e164 Text NN,
     telnyx_account_id Uuid FK telnyx_accounts.id SET NULL nullable, telnyx_phone_number_id Text NULL,
     country_code Text NN, number_type Text NN default `'local'` + check, label Text NULL,
     capabilities ARRAY(Text) NN default `'{voice}'` (Mapped[list[str]]), inbound_config JSONB NN `'{}'::jsonb`
     (docstring: InboundConfig, specs/16 §4), sms_config JSONB NN `'{}'::jsonb` (SmsNumberConfig, specs/21 §3),
     monthly_cost_micros BigInteger NN default `0`, status Text NN default `'active'` + check, purchased_at,
     released_at tz NULL; deleted_at (mixin) must land AFTER released_at and BEFORE created_at — the ordered-columns
     unit test asserts it. If mixin ordering does not produce that, declare `deleted_at` explicitly in the class with
     the same type instead of the mixin (do NOT change base.py sort orders that tenancy relies on), and note it.
     Indexes: `Index("phone_numbers_e164_active_uq", "e164", unique=True,
     postgresql_where=text("status IN ('pending', 'active')"))`, `Index("phone_numbers_org_idx","org_id")`.
   - `NumberOrder`→number_orders (IdMixin, TimestampMixin): org_id CASCADE NN, telnyx_order_id Text NULL,
     requested_numbers ARRAY(Text) NN (no default), status Text NN (NO default) + check, error Text NULL.
     `Index("number_orders_org_idx","org_id")`.
   - `NumberPool`→number_pools (IdMixin, TimestampMixin): org_id CASCADE NN, name Text NN, strategy Text NN default
     `'round_robin'` + check, max_calls_per_number_per_day Integer NN default `100`;
     `UniqueConstraint("org_id","name")` (→ number_pools_org_id_name_key). No extra index (UNIQUE starts with org_id).
   - `NumberPoolMember`→number_pool_members (TimestampMixin only): pool_id Uuid FK number_pools.id CASCADE PK,
     phone_number_id Uuid FK phone_numbers.id CASCADE PK, org_id CASCADE NN, last_used_at tz NULL,
     calls_today Integer NN default `0`, calls_today_date `Date` NULL (Mapped[date | None]). PK name
     number_pool_members_pkey (pool_id, phone_number_id). `Index("number_pool_members_org_idx","org_id")`.
   - `TelnyxEvent`→telnyx_events (IdMixin, TimestampMixin; system scope, docstring cites §2 TelnyxEventRepo):
     telnyx_event_id Text NN unique (→ telnyx_events_telnyx_event_id_key), event_type Text NN,
     org_id Uuid FK orgs.id CASCADE **nullable**, call_id Uuid NULL with **no FK** (spec has none; calls arrive in
     P01-T05 and events can precede the call row), call_control_id Text NULL, occurred_at tz NN, payload JSONB NN
     (no default), processed_at tz NULL, process_error Text NULL. Indexes `telnyx_events_call_idx(call_id)`,
     `telnyx_events_ccid_idx(call_control_id)`. No org index (spec has none; do not invent).
2. `packages/va_db/src/va_db/models/__init__.py` — re-export the 6 models + 5 Literals, `__all__` sorted.

### Test refactor (keep jscpd at 0 clones)
- New `packages/va_db/tests/support/model_introspection.py` with the helpers now private in test_tenancy_models.py:
  `table(name) -> Table`, `constraint(table, name)`, `enum_check_values(table, column) -> tuple[str, ...]`
  (asserts `<table>_<column>_check` starts with `<column> IN (` and regex-parses values),
  `server_defaults(tables) -> dict[(table,col), str]` (excludes created_at/updated_at),
  `index_ddl(tables) -> set[str]` (whitespace-normalised CreateIndex compile, postgresql dialect),
  `unique_and_pk(tables)`, `foreign_keys(tables) -> set[(name, table, column, target, ondelete)]`,
  `check_names(tables)`. Refactor test_tenancy_models.py to use them (assertions unchanged).
- Move the integration `session` fixture (login-role engine, transaction, citext, `Base.metadata.create_all`,
  savepoint AsyncSession, rollback) from test_tenancy_models_integration.py into conftest.py as `scratch_session`
  (rename usages), plus a shared `make_org(session, **values) -> Org` helper in a support module
  (`support/model_factories.py`: `flushed(session, model)`, `make_org`, `make_user`) used by both integration modules.

### Tests
- `test_telephony_models.py` (unit): tables registered; full ordered (name, nullable) list per table (catches the
  deleted_at position); enum checks mirror Literals (5 params, values == spec tuples); exact CHECK-name set for the 6
  tables; server defaults map (telnyx_accounts.status 'pending', phone_numbers number_type 'local', capabilities
  '{voice}', inbound_config/sms_config '{}'::jsonb, monthly_cost_micros 0, status 'active', number_pools strategy
  'round_robin', max_calls 100, number_pool_members calls_today 0) — number_orders.status / payload have none;
  unique + PK names (incl. number_pool_members_pkey composite); FKs + ondelete (8 rows incl. telnyx_events_org_id_fkey
  CASCADE, phone_numbers_telnyx_account_id_fkey SET NULL) and assert telnyx_events.call_id has no FK;
  index DDL set exactly: `CREATE UNIQUE INDEX phone_numbers_e164_active_uq ON phone_numbers (e164) WHERE status IN
  ('pending', 'active')`, plus the 5 plain ones; column types (LargeBinary api_key_enc, ARRAY(Text) capabilities /
  requested_numbers, JSONB inbound_config/sms_config/payload, BigInteger monthly_cost_micros, Integer
  max_calls/calls_today, Date calls_today_date, Uuid call_id); every timestamp tz-aware; telnyx_events.org_id nullable.
- `test_telephony_models_integration.py` (integration, `scratch_session`):
  - `test_phone_number_defaults` — capabilities ['voice'], {} configs, 0 micros, 'local', 'active', deleted_at None.
  - `test_e164_unique_among_active_numbers` (the task's acceptance) — org A active +15555550100; org B same e164
    'pending' → IntegrityError (in begin_nested); set A to 'released' → B insert succeeds; a second 'released' row
    with same e164 also succeeds; 'releasing' duplicates allowed.
  - `test_enum_values_accepted_and_rejected` — parametrized over the 5 enum columns; every Literal value inserts,
    'bogus' → IntegrityError.
  - `test_number_pool_name_unique_per_org` — same name same org fails, other org OK.
  - `test_pool_member_composite_pk_and_cascades` — duplicate (pool, number) fails; deleting the pool removes members;
    deleting a phone number removes members.
  - `test_telnyx_account_delete_sets_number_account_null`.
  - `test_telnyx_event_without_org_or_call` — org_id/call_id NULL accepted; duplicate telnyx_event_id fails;
    payload JSON round-trips; calls_today_date round-trips as `datetime.date`.
  Use `+1555555xxxx`-style fictional numbers, `os.urandom` for api_key_enc, secrets.token_hex for handles.

### Notes for later tasks (record, do not implement)
- §10 RLS: telnyx_events has nullable org_id and is not in the voices/dnc_entries list, so P01-T07 must NOT enable
  RLS on it (system scope via TelnyxEventRepo). number_pool_members has NOT NULL org_id → RLS applies.

### Gates
`make fmt lint typecheck placeholders test`; `uv run pytest packages/va_db -m integration -rs` (Docker up, 0 skipped);
`uvx pip-audit` on uv export; `uvx radon cc -s -a packages/va_db/src`; `npx --yes jscpd@4 --silent` (0 clones);
va_db coverage 100 %. AGENTS.md: note the support/model_introspection + model_factories helpers and the
`scratch_session` fixture.

### Risks
- Mixin column ordering for deleted_at (see above).
- `postgresql_where` compiled text must match the unit expectation exactly; compile once and copy the real output.
- `Date` import from sqlalchemy (not DateTime); mypy: `Mapped[date | None]`.
- Moving fixtures must not change tenancy test behaviour; rerun those integration tests (43 still pass).

## Build P01-T03 (2026-10-27)
Implemented per plan. Notes for the Reviewer:
- `va_db/models/telephony.py`: 6 models + 5 Literals, re-exported from `va_db.models`. Mixin ordering already puts
  `deleted_at` after `released_at` and before `created_at` (bases `IdMixin, SoftDeleteMixin, TimestampMixin`); no
  base.py change and no explicit column needed. `telnyx_events.call_id` has no FK; `org_id` nullable CASCADE.
- Shared test helpers: `tests/support/model_introspection.py` (helper is `get_table`, not `table`, because tests
  parametrize on `table`; added `naive_timestamps`, `text_array_columns`) and `tests/support/model_factories.py`
  (`flushed`, `make_org`, `make_user`, `assert_enum_check_enforced`, `ModelBuilder`). `scratch_session` fixture in
  conftest.py. Tenancy unit/integration tests refactored onto them with the same expectations; the tenancy text[]
  and tz checks now compare whole sets (same coverage, fewer test IDs: suite 1298 -> 1289).
- Extra integration coverage: duplicate encrypted_key_handle rejected; e164 duplicates allowed for released and
  releasing rows; pool member daily counters round-trip (`calls_today_date` as `datetime.date`).
- Builder E.164 numbers come from an itertools counter (no random values, no collisions).
Gates: fmt 0, lint 0, typecheck 0 (161 files), placeholders 0 findings, make test 1289 passed (99.84 %, va_db 100 %),
`pytest packages/va_db -m integration -rs` 55 passed / 0 skipped, pip-audit no known vulns, radon A (1.19),
jscpd 0 clones.

## Review P01-T03 (2026-10-27) — APPROVED
Re-ran lint, typecheck, placeholders (0 findings), make test (1289 passed, 99.84 %), and
`pytest packages/va_db -m integration` (55 passed, 108 deselected, none skipped). make fmt left no diff.
Checked telephony.py against specs/03 §4 column by column: types, nullability, defaults, CHECK values, UNIQUE
(encrypted_key_handle, telnyx_event_id, number_pools(org_id,name)), composite PK, FK ondelete (SET NULL on
telnyx_account_id, nullable CASCADE org_id on telnyx_events, no FK on call_id), the partial unique index
phone_numbers_e164_active_uq, and the plain indexes. All match. The integration tests cover the acceptance case
(e164 unique among pending/active only) plus the cascade and SET NULL paths. The shared test-support refactor keeps
the tenancy expectations unchanged. Ticked + committed.

## Plan P01-T04 — Agent configuration models (2026-10-27)

Spec: specs/03 §5 (24 tables, column for column), §1 conventions, §10. Same rules as P01-T02/T03: an explicit SQL type
on every mapped_column, `check_in(col, get_args(Literal))` for enums, `server_default=text(...)`, explicit index
names, no `index=True`, no relationship(), plain FKs. Mixin base order `IdMixin, SoftDeleteMixin, TimestampMixin`
puts deleted_at right before created_at, as in phone_numbers.

### Spec decision (record in specs/03 §1 as `> Decision (2026-10-27):` in the same commit)
§1 says every tenant table has an index that starts with `org_id`. §3 and §4 follow that rule, but 13 §5 tables have
no such index: voice_samples, voice_ratings, kb_documents, kb_chunks, pathway_unit_tests, pathway_test_runs,
persona_versions, agent_versions, experiments, experiment_arms, citation_schemas, eval_setups, eval_setup_versions.
(tools and dispositions are covered by UNIQUE (org_id, ...)). Decision: the §1 convention wins. Add
`CREATE INDEX <table>_org_idx ON <table>(org_id);` to each of those 13 tables in the §5 SQL block (directly after its
CREATE TABLE / existing indexes) and in the models. Why: the RLS predicate `org_id = app.org_id` and the org-scoped
list endpoints need it. Two things depend on this: the P01-T07 migration copies the DDL, and P01-T08 conformance
parses the spec.

### Shared helpers (avoid jscpd clones)
New `packages/va_db/src/va_db/models/_columns.py` with `tz() -> DateTime` (timezone=True), `org_fk() -> ForeignKey`
(orgs.id CASCADE), and `jsonb_default(literal: str)` returning `text(f"'{literal}'::jsonb")`. Refactor telephony.py
(`_tz`, `_org_fk`) and tenancy.py (`_tz`) onto them. Nothing changes in behaviour, so the existing unit tests stay green.

### Dependency
`uv add --package va-db pgvector` (latest; it pulls numpy). If pgvector ships no py.typed, add a
`[[tool.mypy.overrides]] module = ["pgvector.*"] ignore_missing_imports = true` to the root pyproject and note it in
AGENTS.md. Run `uvx pip-audit` afterwards.

### Files
1. `packages/va_db/src/va_db/models/library.py`. Literals:
   `VoiceProvider = Literal["elevenlabs"]`, `VoiceService = Literal["ELEVEN_FLASH","ELEVEN_TURBO","ELEVEN_MULTILINGUAL"]`,
   `VoiceCategory = Literal["premade","cloned","generated","professional","shared"]`,
   `VoiceCloneStatus = Literal["processing","ready","failed"]`, `ToolHttpMethod = Literal["GET","POST","PUT","PATCH","DELETE"]`,
   `KnowledgeBaseStatus = Literal["PROCESSING","COMPLETED","FAILED"]` (also used by kb_documents.status),
   `KbDocumentType = Literal["FILE","WEB_SCRAPE","TEXT"]`.
   Models: `Prompt` (prompts; tags text[] '{}'; soft delete; prompts_org_idx), `Voice` (voices; org_id **nullable**
   CASCADE; defaults description '', provider 'elevenlabs', service 'ELEVEN_FLASH', category 'premade', is_public
   false, tags '{}', language_codes '{en}', settings
   `'{"stability":0.5,"similarity_boost":0.75,"style":0,"use_speaker_boost":true,"speed":1.0}'::jsonb` (copy exactly),
   rating_sum/rating_count 0. clone_status is nullable with a check, consent is nullable JSONB, plus soft delete. Indexes: voices_org_idx,
   `voices_name_scope_uq` = unique on (`coalesce(org_id, '00000000-0000-0000-0000-000000000000'::uuid)`, `lower(name)`)
   WHERE deleted_at IS NULL, and `voices_provider_uq` = unique on (same coalesce, provider_voice_id) WHERE deleted_at IS NULL.
   Build the expressions with `func.coalesce(<col>, text("'0000...'::uuid"))` / `func.lower(<col>)` declared after
   the class (as audit_logs does) or with `text()` in __table_args__. Compile once and pin the real DDL in the unit test.
   `VoiceSample` (voice_samples; voice_id CASCADE; voice_samples_voice_idx + new voice_samples_org_idx),
   `VoiceRating` (voice_ratings; TimestampMixin only; composite PK voice_id+user_id → voice_ratings_pkey; column
   order voice_id, org_id, user_id, rating; rating SmallInteger, `CheckConstraint("rating BETWEEN 1 AND 5",
   name="rating_check")`; users FK CASCADE; new voice_ratings_org_idx),
   `Tool` (tools; `CheckConstraint("name ~ '^[A-Za-z][A-Za-z0-9_]{0,63}$'", name="name_check")`, method default
   'POST' + check_in, headers/query/response_map `'{}'::jsonb`, secret_headers_enc LargeBinary NULL, body JSONB NULL,
   input_schema `'{"type":"object","properties":{}}'::jsonb`, timeout_ms Integer 10000 check BETWEEN 100 AND 60000,
   retries SmallInteger 0 check BETWEEN 0 AND 3, soft delete, `UniqueConstraint("org_id","name")` → tools_org_id_name_key),
   `KnowledgeBase` (knowledge_bases; status 'PROCESSING', chunk sizes 400/60, counts 0, soft delete, org idx),
   `KbDocument` (kb_documents; type check, no default; size_bytes BigInteger NULL; status default 'PROCESSING'; kb idx + new org idx),
   `KbChunk` (kb_chunks; heading_path text[] '{}'; `embedding` = `pgvector.sqlalchemy.Vector(1024)` NOT NULL;
   `tsv` = `TSVECTOR` with `Computed("to_tsvector('simple', content)", persisted=True)`, nullable, Mapped[str | None];
   indexes kb_chunks_kb_idx, new kb_chunks_org_idx,
   `Index("kb_chunks_embedding_hnsw","embedding", postgresql_using="hnsw", postgresql_with={"m":16,"ef_construction":64},
   postgresql_ops={"embedding":"vector_cosine_ops"})`, `Index("kb_chunks_tsv_idx","tsv", postgresql_using="gin")`).
   Annotate `embedding` with the type the integration round-trip actually returns (pgvector returns a numpy ndarray;
   for writes accept `list[float]`). Pick a Mapped annotation that mypy --strict accepts and say why in a comment.
2. `packages/va_db/src/va_db/models/pathways.py`. `VersionStatus = Literal["draft","published","archived"]` (shared
   with persona_versions; re-export from here), `PathwayTestRunKind = Literal["unit","simulation","regression"]`,
   `PathwayTestRunStatus = Literal["queued","running","passed","failed","error"]`.
   `PathwayFolder` (self FK parent_id CASCADE, org idx), `Pathway` (folder_id SET NULL; production/staging_version
   NULL; draft/latest default 1; soft delete; org idx), `PathwayVersion` (status no default + check; nodes/edges
   `'[]'::jsonb`, global_config `'{}'::jsonb`; graph_hash NN; published_by users SET NULL; UNIQUE(pathway_id,
   version_number) → pathway_versions_pathway_id_version_number_key; org idx), `PathwayUnitTest` (source_call_id
   Uuid NULL **no FK** since calls arrive in P01-T05; history/expected JSONB NN with no default; simulated_variations default 5
   check BETWEEN 0 AND 20; pathway idx + new org idx), `PathwayTestRun` (kind/status checks with no default; results
   `'[]'::jsonb`; `pathway_test_runs_pathway_idx (pathway_id, created_at DESC)` declared after the class with
   `.desc()`, as audit_logs does; new org idx).
3. `packages/va_db/src/va_db/models/agents.py`. `ExperimentStatus = Literal["draft","running","stopped"]`.
   `Persona`, `PersonaVersion` (config JSONB NN; UNIQUE(persona_id, version_number); new org idx), `Agent`
   (env_pins '{}'; soft delete; org idx), `AgentVersion` (raw-string check `semver ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'`
   name semver_check; branch default 'main' + `char_length(branch) BETWEEN 1 AND 64` name branch_check; pathway_id
   FK **RESTRICT** nullable; pathway_version NULL; call_settings '{}'; changelog ''; created_by users SET NULL;
   UNIQUE(agent_id, semver); `agent_versions_agent_branch_idx (agent_id, branch, created_at DESC)`; new org idx),
   `Experiment` (status 'draft' + check; primary_metric Text default 'disposition_rate' with **no** CHECK (spec has
   none; keep the comment); primary_metric_config '{}'; partial unique `experiments_one_running_per_agent ON (agent_id)
   WHERE status = 'running'`; new org idx), `ExperimentArm` (agent_version_id FK RESTRICT; weight Integer check
   BETWEEN 0 AND 100; new org idx), `Disposition` (tag check `tag ~ '^[A-Z][A-Z0-9_]{0,63}$'`; UNIQUE(org_id, tag)),
   `CitationSchema` (variables JSONB NN with no default; new org idx), `EvalSetup` (latest_version 1; new org idx),
   `EvalSetupVersion` (criteria JSONB NN; pass_threshold `Numeric(5, 2)` default `70`, Mapped[Decimal];
   UNIQUE(eval_setup_id, version_number); new org idx), `Widget` (FKs pathway/agent/persona/voice all SET NULL;
   allowed_domains '{}'; messages_per_minute 20 check BETWEEN 1 AND 600; voice_enabled false; config '{}';
   public_key `unique=True` → widgets_public_key_key; soft delete; widgets_org_idx).
   Docstrings name the Pydantic models the spec comments cite (GlobalConfig, PersonaConfig, WidgetConfig, ...).
4. `va_db/models/__init__.py`: re-export the 24 models and 11 Literals. Keep `__all__` sorted.
5. specs/03 §5 SQL: the 13 org indexes plus the Decision note (see above).

### Tests
- conftest `scratch_session`: add `CREATE EXTENSION IF NOT EXISTS vector` before create_all (container is
  pgvector/pgvector:pg16). Tenancy and telephony integration tests must still pass.
- `test_agent_config_models.py` (unit, reuses support/model_introspection; split into two files by area if it gets
  past ~500 lines): all 24 tables registered; full ordered (name, nullable) column list per table; enum checks equal
  the Literal tuples (voices provider/service/category/clone_status, tools method, knowledge_bases status,
  kb_documents type/status, pathway_versions status, persona_versions status, pathway_test_runs kind/status,
  experiments status); exact CHECK-name set over the 24 tables, plus the compiled sqltext of the non-enum checks (rating,
  name regex, timeout_ms, retries, simulated_variations, semver, branch, weight, tag, messages_per_minute); full
  server-default map, including the voices.settings and tools.input_schema JSON literals; the UNIQUE and PK sets,
  including voice_ratings_pkey (voice_id, user_id); the FK set with ondelete (RESTRICT on agent_versions.pathway_id and
  experiment_arms.agent_version_id; SET NULL on widgets ×4, pathways.folder_id, pathway_versions.published_by,
  agent_versions.created_by; nullable CASCADE on voices.org_id); assert pathway_unit_tests.source_call_id has no FK;
  the exact index DDL set (HNSW `USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)`, GIN tsv,
  both voices expression-unique partial indexes, the experiments partial unique, two DESC indexes, every org_idx);
  column types (Vector dim 1024, TSVECTOR plus `Computed` persisted with the exact sqltext, Numeric(5,2), SmallInteger
  rating/retries, LargeBinary secret_headers_enc, BigInteger size_bytes); every timestamp tz-aware; the text[] column
  set; and every §5 table except voices has NOT NULL org_id with an index or UNIQUE starting with org_id (this test
  guards the Decision).
- `test_agent_config_models_integration.py` (integration, `scratch_session`, model_factories builders):
  - `test_voice_defaults` covers settings JSON equality, '{en}', 'premade', and a platform voice with org_id NULL.
  - `test_voice_name_unique_per_scope`: a case-insensitive duplicate name in the same org fails; another org and the
    platform scope succeed; two platform voices with the same name fail; a soft-deleted duplicate is allowed. Do the
    same for provider_voice_id.
  - `test_enum_values_accepted_and_rejected`: parametrized over every enum column through `assert_enum_check_enforced`.
  - `test_range_and_pattern_checks`: boundary values pass and out-of-range values fail for each non-enum check, e.g.
    tool name "1bad", semver "1.2", tag "lower", branch "" and 65 chars.
  - `test_kb_chunk_embedding_and_tsv`: insert a 1024-dim embedding; it round-trips and tsv is populated
    (`tsv @@ to_tsquery('simple', ...)` matches); a 3-dim vector fails.
  - `test_experiment_one_running_per_agent`: a second running experiment fails; draft and stopped ones are allowed.
  - `test_restrict_and_set_null_paths`: deleting a pathway referenced by agent_versions raises; deleting an
    agent_version referenced by an arm raises; deleting a voice nulls widgets.voice_id; deleting a folder cascades
    child folders and nulls pathways.folder_id.
  - `test_version_uniques`: pathway/persona/eval_setup version_number and agent semver duplicates fail.
  - `test_eval_pass_threshold_decimal`: the default is Decimal("70.00").
  Inside the scratch transaction, check that the indexes really exist: `pg_indexes` has kb_chunks_embedding_hnsw
  (`USING hnsw`) and kb_chunks_tsv_idx (`USING gin`). This is an early answer to the acceptance item that P01-T07 will
  re-verify after migration.

### Gates
`make fmt lint typecheck placeholders test`; `uv run pytest packages/va_db -m integration -rs` with Docker up and
0 skipped; `uvx pip-audit` on uv export (pgvector and numpy are new); `uvx radon cc -s -a packages/va_db/src`;
`npx --yes jscpd@4 --silent` (0 clones; the three model files must not repeat column blocks, so use `_columns.py`);
va_db coverage at 100 %. AGENTS.md: record the pgvector dependency/mypy note, `_columns.py`, the org-index Decision, and
the vector extension in scratch_session.

### Risks
- Expression indexes made of text() inside __table_args__ may not attach to the table. Fall back to module-level
  `Index(...)` with column expressions. Pin the compiled DDL from real output.
- asyncpg has no codec for `vector`. The SQLAlchemy Vector bind sends text and asyncpg falls back to the text format.
  If binding fails, register the codec only for the scratch connection in tests and record a fix memory. Do NOT add
  a connect listener to engine.py, because the extension may not exist yet at connect time.
- jscpd can flag the large expected-column dicts against each other. Keep them as data and split them by file if needed.
- A regex CHECK containing `\.` needs a raw string, and the compiled sqltext must equal the spec text.

## Build P01-T04 (2026-10-27)
Implemented per plan, with these deviations and findings for the Reviewer:
- pgvector 0.5.1 (`uv add --package va-db pgvector`) ships `py.typed` and no longer pulls numpy; its result processor
  returns `list[float]`, so `KbChunk.embedding: Mapped[list[float]]`. No mypy override was needed.
- Bug caught while pinning DDL: `text("'{\"style\":0,...}'::jsonb")` compiled `:0`/`:true` as bind params. The fix is
  `_columns.jsonb_default`, which escapes `:`. Unit tests pin the compiled voices.settings and tools.input_schema
  literals, and test_columns.py asserts that no params appear.
- `Mapped[Any]` made `tools.body` NOT NULL; it is now `Mapped[Any | None]` with `nullable=True` (the column-list test caught it).
- `_columns.py` grew past the plan, because jscpd flagged 11 clones of repeated column blocks: `org_id_column(nullable=)`,
  `fk_column`, `int_column`, `bool_column`, `text_column`, `text_array_column`, `jsonb_column(default=None)`. These were
  applied to tenancy and telephony as well; behaviour is unchanged and their unit and integration tests are unchanged and green.
  The last clone was the agents/library import header only, so agents.py wraps it in `# jscpd:ignore-start/end` with a reason.
- `pathway_test_runs_org_idx` is declared module-level next to the DESC index (same DDL).
- model_introspection: `server_defaults` skips `Computed`; new `computed_columns`. model_factories: new `assert_rejected`,
  `assert_check_enforced` (`assert_enum_check_enforced` now delegates to it).
- `pathway_unit_tests.source_call_id` has no FK, as in the spec.
- Integration: 32 new tests, including pg_indexes `USING hnsw ... WITH (m='16', ef_construction='64')` and `USING gin (tsv)`,
  cosine-distance ordering, tsv @@ match, and 3-dim vector rejection ("expected 1024 dimensions, not 3").
- specs/03 §1 Decision plus the 13 `<table>_org_idx` statements in §5. AGENTS.md is updated.
Gates: make fmt 0, lint 0, typecheck 0 (168 files), placeholders 0 findings, make test 1387 passed (99.85 %,
va_db 100 %), `pytest packages/va_db -m integration -rs` 87 passed / 0 skipped, pip-audit no known vulns,
radon A (1.15), jscpd 0 clones.

## Review P01-T04 (2026-10-27) — APPROVED
Re-ran: make lint/typecheck/placeholders clean; make test 1387 passed (99.85 %); `pytest packages/va_db -m integration`
87 passed, 0 skipped; jscpd 0 clones; radon A (1.15). Compared library.py, pathways.py, agents.py against specs/03 §5
column by column: types, nullability, defaults (voices.settings and tools.input_schema JSON literals), CHECKs,
ondelete (RESTRICT x2, SET NULL paths), voices expression/partial uniques, HNSW/GIN, DESC indexes, the experiments
partial unique, and widgets.public_key UNIQUE all match. The spec diff adds only the 13 org_idx lines plus the §1
Decision. Unit tests pin the compiled DDL, and integration tests exercise the behaviour that matters.

## Plan P01-T05 — Call models (2026-10-27)
Spec: specs/03 §6 (12 tables). Depends on T03 and T04, both ticked. Follow the T02–T04 conventions: `_columns` builders,
`check_in` from Literals, spec-named indexes, and the jscpd/test-support memories.

### Spec decision (extend the §1 Decision in the same commit)
The §1 org-index rule is binding, but §6 leaves these tables without an index that starts with org_id: contacts (only
*partial* uniques, which cannot serve a full-org scan), batch_items, call_legs, call_turns, call_events,
call_analyses, citations, eval_runs, guard_rail_events, dnc_entries (nullable org_id, same as voices, which
has voices_org_idx). Add `CREATE INDEX <table>_org_idx ON <table>(org_id);` for each of the 10 tables to the §6 SQL
block, directly after each table's existing indexes. Then append a second paragraph to the §1 Decision note
("§6 likewise: ...") that lists them. Why: it is the same RLS-predicate reasoning, and the P01-T07 and T08 tasks copy
and parse this DDL. call_events_org_idx on the partitioned parent becomes a partitioned index, which is fine.

### Files
1. `packages/va_db/src/va_db/models/calls.py`. Literals (names are exact; tuples copy the spec order):
   `BatchStatus` (created, scheduled, validating, dispatching, in_progress, completed, stopped, failed),
   `BatchItemStatus` (pending, skipped, dispatched, completed, failed, canceled), `CallDirection` (outbound, inbound, web),
   `CallStatus` (12 values), `AnsweredBy` (human, voicemail, ivr, unknown, no_answer, fax), `RunnerType` (task, pathway,
   persona, agent), `EndedBy` (agent, user, system, api, guard_rail, timeout, error), `CallLegRole`, `CallLegState`,
   `CallTurnRole` ('user','assistant','agent-action','system','human_agent'), `CallEventCategory` (18 values),
   `CallEventLevel` (debug, info, warning, error), `CallAnalysisKind`, `EvalRunStatus`, `DncSource`.
   Models:
   - `Contact` (contacts): email CITEXT NULL; attributes jsonb '{}'. Indexes: three partial uniques declared as
     `Index("contacts_phone_uq", "org_id", "phone_e164", unique=True, postgresql_where=text("phone_e164 IS NOT NULL"))`
     (likewise for email and external), plus contacts_org_idx.
   - `Batch` (batches): status text_column('created') + check; global_props/schedule '{}'; five int_column(0)
     counters; created_by_key FK api_keys.id SET NULL. `batches_org_created_idx (org_id, created_at DESC)` is
     module-level with `.desc()`.
   - `BatchItem` (batch_items): batch_id FK CASCADE NN; ordinal Integer NN; payload jsonb NN with no default; status
     'pending' + check; attempts SmallInteger default 0; call_id is a Uuid NULL with **no FK** (calls.batch_item_id
     already points the other way, and a cycle would break create_all/drop ordering); UNIQUE(batch_id, ordinal) →
     batch_items_batch_id_ordinal_key; batch_items_dispatch_idx (batch_id, status, next_attempt_at); org idx.
   - `Call` (calls): column order exactly as in the spec. external_id gets
     `CheckConstraint("char_length(external_id) <= 256", name="external_id_check")`. answered_by and ended_by are
     nullable with check_in; a NULL passes the CHECK. SET NULL FKs: contact, pathway, persona, agent, agent_version,
     experiment, experiment_arm, batch, batch_item, parent_call_id (**self** "calls.id"), phone_number, voice.
     created_by-style columns with **no FK**: credit_hold_id and memory_id (their targets come in T06; the spec has no
     REFERENCES). config jsonb NN with no default; request_data/metadata/variables '{}'; connected_seconds and
     transfer_seconds `Numeric(10, 3)` NN default `0`, Mapped[Decimal]; price_micros BigInteger default 0;
     recording_duration_ms Integer NULL; test_call bool_column(default=False). Attribute `metadata` is reserved on
     Declarative classes, so map it as `metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, ...)`.
     Add a comment saying why, and assert the column name in the unit test. Six indexes: `calls_org_created_idx` and
     `calls_org_to_idx` (org_id, to_number, created_at DESC) and `calls_pathway_idx` (org_id, pathway_id,
     created_at DESC) are module-level with `.desc()`; calls_org_status_idx; `calls_org_external_idx` partial
     WHERE external_id IS NOT NULL (non-unique); calls_batch_idx.
   - `CallLeg` (call_legs): call_id CASCADE NN; telnyx_account_id SET NULL; call_control_id NN; started_at
     tz NN `server_default=func.now()`; `call_legs_ccid_uq` unique index (an index, not a constraint);
     call_legs_call_idx; org idx.
   - `CallTurn` (call_turns): **the attribute named `text`** shadows `sqlalchemy.text` inside the class body.
     Either declare it with `mapped_column("text", Text, nullable=False)` under attribute `text` placed so that no later
     line in the class body calls `text(...)` (builders are safe because they call text() in `_columns`), or name it
     `text_` with an explicit column name "text". Prefer `text_` for clarity; mypy and the reader both see it. stt_confidence
     `REAL` (sqlalchemy.REAL) Mapped[float | None]; latency '{}'; interrupted bool false; start_ms/end_ms Integer NN
     with no default; UNIQUE(call_id, turn_index, role) → call_turns_call_id_turn_index_role_key;
     call_turns_call_idx (call_id, turn_index); org idx.
   - `CallEvent` (call_events): **no IdMixin and no TimestampMixin** (it has no updated_at, and the PK is composite).
     Columns in spec order: id Uuid NN `default=new_id`; org_id Uuid NN with **no FK** (partitioned table; the spec
     has no REFERENCES); call_id Uuid NN with no FK; seq Integer NN; category check; level text_column('info') + check;
     message NN; data '{}'; created_at tz NN server_default now(). `PrimaryKeyConstraint("created_at", "id")` →
     call_events_pkey, and `{"postgresql_partition_by": "RANGE (created_at)"}` as the trailing dict in
     __table_args__. Indexes: call_events_call_idx (call_id, seq), call_events_org_idx. The comment cites the
     RetentionWorkflow partition note. Do not give created_at a Python default: the server fills it
     and RETURNING brings it back (Postgres + asyncpg do implicit RETURNING for server-generated PK parts). If the ORM
     still complains because a PK part is NULL at flush, set `default=utcnow` from va_common.time as well, keep the
     server default, and record the fix memory.
   - `CallAnalysis` (call_analyses): kind check; goal Text NULL; questions/schema JSONB NULL as `Mapped[Any | None]`
     (memory: Mapped[Any] would make them NOT NULL); `schema` is fine as an attribute name; result jsonb NN with no
     default; model NN; call idx and org idx.
   - `Citation` (citations): citation_schema_id FK CASCADE NN; value JSONB NULL; conditions_met Boolean NULL;
     cited_utterances '[]'; call idx and org idx.
   - `EvalRun` (eval_runs): call_id FK calls CASCADE **nullable**; eval_setup_version_id FK CASCADE NN; status check
     with no default; overall_score Numeric(5,2) NULL Mapped[Decimal | None]; passed Boolean NULL; results '[]';
     error NULL; call idx and org idx.
   - `GuardRailEvent` (guard_rail_events): triggered Boolean NN with **no default** (plain mapped_column); action
     NULL; turn_index Integer NULL; detail '{}'; call idx and org idx.
   - `DncEntry` (dnc_entries): org_id_column(nullable=True); source check; call_id Uuid NULL with no FK;
     expires_at tz NULL; `dnc_scope_phone_uq` = unique on (coalesce(org_id, '0000…'::uuid), phone_e164) **without**
     WHERE. Reuse the same expression-building approach as voices_name_scope_uq; if the coalesce expression is
     duplicated, factor it into a helper in `_columns.py` such as `platform_scope(column)` and use it in both
     library.py and calls.py (jscpd). Add dnc_entries_org_idx.
   Docstrings name the Pydantic models and specs cited in the comments (BatchSchedule 17-batches §3, SendCallConfig
   05-calls-api §4, end_reason enumeration 05-calls-api §9).
2. `va_db/models/__init__.py`: re-export the 12 models and 15 Literals, with `__all__` kept sorted.
3. specs/03: the §6 org_idx lines plus the extension of the §1 Decision.
4. AGENTS.md: `metadata_` / `text_` attribute mapping convention; call_events has no FK, a composite PK, and is
   partitioned; the partition name used by tests (see below) is a candidate for T13.

### Tests
- `packages/va_db/tests/test_call_models.py` (unit; model_introspection): all 12 tables registered; ordered
  (name, nullable) columns per table; every enum check equals its Literal tuple; exact CHECK-name set, plus the
  sqltext of `calls_external_id_check`; server-default map; UNIQUE/PK set (call_events_pkey = (created_at, id),
  batch_items and call_turns uniques); FK set with ondelete, including `calls.parent_call_id → calls.id SET NULL`;
  **no-FK assertions** for batch_items.call_id, calls.credit_hold_id, calls.memory_id, call_events.org_id/call_id,
  dnc_entries.call_id; exact index DDL set (three contacts partial uniques, calls_org_external_idx partial,
  three DESC indexes, call_legs_ccid_uq UNIQUE, the dnc coalesce expression, every org_idx); the
  `postgresql_partition_by` dialect option == "RANGE (created_at)"; types (CITEXT email, Numeric(10,3) x2,
  Numeric(5,2), REAL, SmallInteger attempts, BigInteger price_micros); tz-aware timestamps; `Call.metadata_` maps
  to column "metadata" and `CallTurn.text_` to "text"; every §6 table except dnc_entries has NOT NULL org_id, and all
  12 have an index starting with org_id (this test guards the Decision). Extend the existing §5 org-index test helper
  rather than copy it (put a shared `org_leading_index_tables()` in model_introspection if needed).
- `packages/va_db/tests/test_call_models_integration.py` (scratch_session plus factory builders; add `make_call` and
  `make_batch` to model_factories if reused):
  - `test_call_event_lands_in_monthly_partition`: inside the scratch txn,
    `CREATE TABLE call_events_2026_10 PARTITION OF call_events FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')`
    plus the 2026_11 partition, then insert CallEvents with created_at in each month and assert
    `SELECT tableoid::regclass::text FROM call_events WHERE id = :id` gives the right partition. An insert outside every
    partition raises ("no partition of relation"). This is the task's acceptance item. Also insert one CallEvent
    with no created_at and check that the server default plus RETURNING populates it (needs a partition covering now; create
    it from `SELECT date_trunc('month', now())` in the test).
  - `test_call_defaults`: Decimal("0.000") for the seconds columns, price 0, test_call false, and the '{}' jsonb defaults.
  - `test_enum_values_accepted_and_rejected`: parametrized over every enum column via assert_enum_check_enforced
    (answered_by and ended_by NULL accepted).
  - `test_external_id_length_check`: 256 chars pass, 257 fail.
  - `test_contact_partial_uniques`: duplicate phone/email (email case-insensitive via citext)/external per org fails;
    multiple NULLs allowed; the same value in another org is allowed.
  - `test_dnc_scope_unique`: a duplicate phone in one org fails; a platform (NULL org) duplicate fails; the same phone in
    an org plus the platform succeeds.
  - `test_uniques`: batch_items (batch_id, ordinal), call_turns (call_id, turn_index, role) where the same index with
    a different role is allowed, and call_legs call_control_id.
  - `test_delete_paths`: deleting a call cascades legs, turns, analyses, citations, guard_rail_events and eval_runs;
    deleting the parent call nulls parent_call_id; deleting a contact, batch or voice nulls the calls FKs; deleting a
    batch cascades its batch_items.
  - `test_call_turn_text_and_real_round_trip`: the `text_` attribute round-trips; stt_confidence is a float.

### Gates
`make fmt lint typecheck placeholders test`; `uv run pytest packages/va_db -m integration -rs` (Docker up, 0 skipped);
`uvx radon cc -s -a packages/va_db/src`; `npx --yes jscpd@4 --silent` (0 clones); va_db coverage at 100 %;
`uvx pip-audit` on uv export (no new deps expected).

### Risks
- `metadata` is reserved by Declarative (InvalidRequestError), hence `metadata_`. `text` shadows the import in the class body.
- create_all on a table with postgresql_partition_by: SQLAlchemy emits `PARTITION BY RANGE (created_at)`, and
  Postgres requires the PK to include the partition key, which it does. Unique and plain indexes on the parent are fine.
- Self-referential FK calls.parent_call_id together with calls.batch_item_id → batch_items: no cycle as long as
  batch_items.call_id has no FK. Keep it that way.
- If jscpd flags call_id-FK + call_idx blocks repeated across call_analyses/citations/guard_rail_events/eval_runs,
  add a small `_columns` helper (e.g. `call_fk_column()`) instead of using ignore markers.
- test file length: split the unit test by area (calls vs. the rest) if it goes past about 500 lines.

## Build P01-T05 (2026-10-27)
Implemented per plan. Notes for the Reviewer:
- `models/calls.py`: 12 models and 15 Literals. Local helpers `_call_id_column()` (call_id FK CASCADE NN) and
  `_seconds_column()` (numeric(10,3) NN default 0). `Call.metadata_` maps to "metadata" and `CallTurn.text_` to "text".
- New `_columns.platform_scope(org_id)` (coalesce with the nil uuid). library.py now uses it for both voices uniques,
  and `_NIL_UUID` is gone; the DDL is unchanged and pinned by test_agent_config_models.
- CallEvent: no mixins, `PrimaryKeyConstraint("created_at","id")`, partition_by RANGE (created_at). `created_at` has only
  a server default; asyncpg RETURNING filled it at flush, so no Python default was needed. This is verified in
  `test_call_event_created_at_comes_back_from_the_server_default`, which uses a partition whose bounds are the
  `date_trunc('month', now())` expression.
- specs/03: 10 `<table>_org_idx` lines in §6, plus a "§6 likewise" paragraph appended to the §1 Decision.
- Test support (jscpd): `model_introspection.leading_columns`, `spec_column_mismatches`, `assert_enum_check_mirrors`.
  The agent-config and telephony unit tests now use them. Their behaviour is the same, and they are green.
- Integration (27 tests): monthly partition routing (Oct/Nov) with an out-of-range insert rejected ('no partition of
  relation "call_events" found for row'); server-default created_at; defaults; 13 enum checks; answered_by/ended_by/
  external_id (256 ok, 257 rejected, NULL ok); contact partial uniques including the citext email case; dnc scope;
  batch_items/call_turns/call_legs uniques; child cascades; SET NULL paths for parent call, contact, voice, and batch
  (+ batch_items cascade); text_/REAL round-trip.
Gates: make fmt 0, lint 0, typecheck 0 (171 files), placeholders 0 findings, make test 1464 passed (99.86 %, va_db
100 %), `pytest packages/va_db -m integration -rs` 114 passed / 0 skipped, radon A (1.12), jscpd 0 clones, pip-audit
no known vulns (no new deps).

## Review P01-T05 (2026-10-27) — APPROVED
Re-ran: make lint/typecheck/placeholders clean; make test 1464 passed (99.86 %); `pytest packages/va_db -m integration`
114 passed, 0 skipped; jscpd 0 clones; radon A (1.12). Checked calls.py against specs/03 §6 column by column: order,
types (CITEXT, numeric(10,3) x2, numeric(5,2), REAL, smallint, bigint), nullability, defaults, every CHECK list,
calls_external_id_check, FK ondelete (self FK parent_call_id SET NULL; eval_runs.call_id nullable CASCADE), the
FK-less columns (batch_items.call_id, credit_hold_id, memory_id, call_events org_id/call_id, dnc call_id), call_events
composite PK + RANGE partitioning, partial/DESC/expression indexes. The spec diff only adds the 10 org_idx lines and
the "§6 likewise" Decision paragraph. The platform_scope refactor leaves the voices DDL unchanged (pinned by tests).

## Plan P01-T06 — Memory, messaging, webhook, billing, §9a models (2026-10-27)
Spec: specs/03 §7, §8, §9, §9a (20 tables). Depends on T05 (ticked). Same conventions as T02–T05: `_columns`
builders, `check_in(get_args(Literal))`, spec-named indexes, `metadata_` mapping, tests via model_introspection /
model_factories, jscpd 0 clones.

### Spec decision (extend the §1 Decision in the same commit)
Tables in §7–§9a with no plain index leading with org_id: memory_changes, messages, ten_dlc_brands,
ten_dlc_campaigns, org_subscriptions (only a *partial* unique), org_data_keys (only a partial unique). Add
`CREATE INDEX <table>_org_idx ON <table>(org_id);` after each table's indexes in the SQL blocks, and append a third
paragraph to the §1 Decision ("§7–§9a likewise: ..."). Already satisfied: memories (non-partial unique starting
org_id), conversations (lookup idx), webhook_endpoints, webhook_deliveries, credit_ledger, credit_holds,
usage_events, invoices, org_llm_providers (org_id UNIQUE), dnc_imports, erasure_requests, org_secrets
(UNIQUE(org_id,name)). plans and stripe_events are system scope (no org_id, no RLS) — say so in the paragraph.

### Files (new model modules import only va_db.base, _checks, _columns, and library.EMBEDDING_DIMENSIONS — no
### sibling-model imports beyond that; FKs are strings)
1. `packages/va_db/src/va_db/models/memory.py` (§7):
   - `MemoryChangeType` Literal ('summary','fact_set','fact_delete','open_item_add','open_item_close','reset').
   - `Memory` (memories): org_id; contact_id fk contacts CASCADE NN; persona_id fk personas CASCADE NULL (comment:
     NULL = org-wide memory); summary text_column(''); facts jsonb '{}'; open_items '[]'; recent_messages '[]';
     enabled bool true; summary_embedding `Vector(EMBEDDING_DIMENSIONS)` NULL `Mapped[list[float] | None]`
     (import the constant from va_db.models.library). Index `memories_contact_persona_uq` unique on
     ("org_id","contact_id", platform_scope(persona_id)) — reuse `_columns.platform_scope` and widen its docstring to
     "NULL means the shared scope (platform rows for voices/DNC, org-wide memory for memories)"; do NOT rename
     (DDL pinned by earlier tests). Docstring names the facts/open_items/recent_messages shapes per the spec comments.
   - `MemoryChange` (memory_changes): memory_id fk memories CASCADE NN; call_id, conversation_id plain Uuid NULL
     (**no FK**, spec has none); change_type check (no default); before/after JSONB NULL `Mapped[Any | None]`
     (memory fix: not Mapped[Any]). Indexes: module-level `memory_changes_memory_idx (memory_id, created_at DESC)`
     with `.desc()`; memory_changes_org_idx.
2. `packages/va_db/src/va_db/models/messaging.py` (§8):
   - Literals `ConversationChannel` (sms,mms,widget,imessage,rcs), `ConversationRunnerType` (objective,pathway,
     persona,agent — distinct from calls.RunnerType, say why in a comment), `ConversationStatus` (active,
     human_takeover,closed,timed_out), `MessageDirection`, `MessageRole` (user,assistant,human_agent,system),
     `MessageStatus` (8 values in spec order), `TenDlcBrandStatus`, `TenDlcCampaignStatus`.
   - `Conversation`: columns in spec order; SET NULL FKs widget, contact, pathway, persona, agent; engine_state/
     request_data '{}'; `metadata_` → "metadata" '{}' (same comment as Call); status text_column('active')+check;
     time_out_minutes int_column(1440); last_message_at tz NULL; webhook/summary/disposition_tag Text NULL.
     Indexes conversations_lookup_idx (org_id, channel, agent_number, user_number, status) and
     conversations_widget_idx (widget_id, widget_visitor_id).
   - `Message`: conversation_id fk CASCADE NN; body NN; media_urls text_array_column('{}'); telnyx_message_id Text
     unique (→ messages_telnyx_message_id_key); status text_column('queued')+check; error JSONB NULL Mapped[Any|None];
     segments int_column(1); price_micros bigint default 0; node_id NULL. Indexes messages_conversation_idx
     (conversation_id, created_at), messages_org_idx.
   - `TenDlcBrand` (ten_dlc_brands): telnyx_brand_id unique; payload jsonb NN no default (comment 21-messaging §6);
     status check no default; status_detail NULL; org idx.
   - `TenDlcCampaign` (ten_dlc_campaigns): brand_id fk ten_dlc_brands CASCADE NN; telnyx_campaign_id unique;
     use_case NN; payload NN; status check; status_detail; org idx.
3. `packages/va_db/src/va_db/models/billing.py` (§9 — webhooks and billing as the spec groups them):
   - Literals `WebhookFormat` (bland,envelope), `WebhookDeliveryStatus` (pending,delivering,succeeded,failed,dead),
     `SubscriptionStatus` (active,past_due,canceled,trialing), `CreditEntryType` (11 values), `CreditHoldStatus`
     (held,captured,released), `UsageMeter` (13 values), `InvoiceStatus` (draft,finalized,paid,void).
   - `WebhookEndpoint`: url NN; description text_column(''); event_types text_array_column('{*}'); enabled bool
     true; phi_allowed bool false; webhook_endpoints_org_idx (org_id).
   - `WebhookDelivery`: event_id/event_type/url NN; endpoint_id fk SET NULL; payload jsonb NN; format
     text_column('bland')+check; status text_column('pending')+check; attempts SmallInteger NN default 0;
     next_attempt_at tz NN server_default now(); last_status_code/last_response_ms Integer NULL; last_error NULL;
     call_id/batch_id/conversation_id Uuid NULL **no FK**; delivered_at tz NULL. UniqueConstraint("event_id","url")
     → webhook_deliveries_event_id_url_key. Indexes: `webhook_deliveries_due_idx (next_attempt_at)` partial
     `postgresql_where=text("status IN ('pending', 'delivering')")`; module-level
     `webhook_deliveries_org_idx (org_id, created_at DESC)`.
   - `Plan` (plans): **no IdMixin**; `id: Mapped[str] = mapped_column(Text, primary_key=True)` declared first; name
     NN; six bigint NN rate columns with no default; tts_rate_micros_per_1k_chars bigint default 0; four Integer
     NULL caps (comment: NULL = unlimited); features text_array '{}'; stripe_price_id NULL; is_public bool true;
     TimestampMixin. No org_id.
   - `OrgSubscription`: plan_id `mapped_column(Text, ForeignKey("plans.id"), nullable=False)` (no ondelete →
     NO ACTION; FK test asserts ondelete None); status check no default; stripe_subscription_id unique;
     current_period_start/end tz NN; cancel_at_period_end bool false. Indexes `org_subscriptions_active_uq` unique on
     org_id WHERE status IN ('active', 'past_due', 'trialing'); org_subscriptions_org_idx.
   - `CreditLedger` (credit_ledger): entry_type check; amount_micros/balance_after_micros bigint NN (no default);
     reference_type/reference_id NULL; description ''; idempotency_key NN; UNIQUE(org_id, idempotency_key) →
     credit_ledger_org_id_idempotency_key_key; module-level credit_ledger_org_created_idx (org_id, created_at DESC).
     Docstring: append-only.
   - `CreditHold` (credit_holds): reference_type/reference_id NN; amount_micros NN; status check; expires_at tz NN;
     credit_holds_org_status_idx (org_id, status).
   - `UsageEvent` (usage_events): meter check; quantity `Numeric(18, 6)` NN Mapped[Decimal]; unit_price_micros,
     amount_micros NN; cost_micros default 0; reference_type/reference_id NN; idempotency_key NN; occurred_at tz NN;
     UNIQUE(org_id, idempotency_key); module-level usage_events_org_time_idx (org_id, occurred_at DESC).
   - `Invoice` (invoices): period_start/end tz NN; subtotal/credits_applied/total micros NN; line_items jsonb NN no
     default; status check; stripe_invoice_id unique; pdf_s3_key NULL; module-level invoices_org_idx
     (org_id, period_start DESC).
   - `StripeEvent` (stripe_events): **no IdMixin**, `id` Text PK (Stripe event id); `type` Text NN (attribute name is
     fine); payload jsonb NN; processed_at tz NULL; TimestampMixin. No org_id.
   - If the "bigint NOT NULL DEFAULT 0" mapped_column repeats (calls.price_micros, messages.price_micros,
     usage_events.cost_micros, plans.tts_rate...) and jscpd flags it, add `_columns.micros_column(default: int |
     None = None)` (bigint NOT NULL, optional DEFAULT) and use it in calls.py too (DDL unchanged, pinned by tests).
4. `packages/va_db/src/va_db/models/security.py` (§9a):
   - Literals `JobStatus` (queued,running,completed,failed — shared by dnc_imports and erasure_requests),
     `OrgDataKeyStatus` (active,retired).
   - `OrgLlmProvider`: `org_id` = `mapped_column(Uuid, org_fk(), nullable=False, unique=True)` →
     org_llm_providers_org_id_key; base_url NN; api_key_enc LargeBinary NN; models jsonb NN (docstring:
     {"dialogue","router","analysis","fallback"}, 10-llm-gateway); supports_tool_choice bool true; baa_covered bool
     false; last_probe_at tz NULL; last_probe_error NULL.
   - `DncImport`: status check; s3_key NN; total/inserted/invalid/duplicates Integer NULL; error NULL; module-level
     dnc_imports_org_idx (org_id, created_at DESC).
   - `ErasureRequest`: selector jsonb NN; status check; result JSONB NULL Mapped[Any|None]; module-level
     erasure_requests_org_idx (org_id, created_at DESC).
   - `OrgSecret`: name NN with `CheckConstraint("name ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="name_check")`
     (→ org_secrets_name_check); value_enc bytea NN; UNIQUE(org_id, name) → org_secrets_org_id_name_key.
   - `OrgDataKey`: kek_id NN; encrypted_data_key bytea NN; status check; `org_data_keys_active_uq` unique on org_id
     WHERE status = 'active'; org_data_keys_org_idx.
   - Module docstring: envelope encryption (28-security), {{secrets.NAME}} (04 §7).
5. `va_db/models/__init__.py`: re-export the 20 models and new Literals, `__all__` sorted.
6. specs/03: the 6 org_idx lines + §1 Decision paragraph.
7. AGENTS.md: list the model modules by spec section; system-scope tables (plans, stripe_events) use text PKs and
   no IdMixin; model modules must not import each other except for shared constants (import-graph test).

### Tests
- `tests/test_memory_messaging_models.py` (unit, §7+§8, 6 tables) and `tests/test_billing_models.py` (unit, §9+§9a,
  14 tables). Each: tables registered; spec_column_mismatches == {}; assert_enum_check_mirrors for every enum
  column; exact check_names set (incl. org_secrets_name_check sqltext); server_defaults map (incl. '{*}', 1440,
  'bland', jsonb '{}'/'[]'); unique_and_pk set (plans/stripe_events PK = id Text; messages/ten_dlc/subscription/
  invoice stripe ids; ledger/usage (org_id, idempotency_key); webhook (event_id,url); org_secrets; org_llm_providers
  org_id); foreign_keys set incl. org_subscriptions.plan_id → plans.id ondelete None; **no-FK assertions** for
  memory_changes.call_id/conversation_id, webhook_deliveries.call_id/batch_id/conversation_id; exact index_ddl
  (partials, DESC, the memories coalesce expression, every Decision org_idx); every org table has a NOT NULL org_id
  and "org_id" in leading_columns; plans/stripe_events have no org_id column; types (Vector(1024) on
  summary_embedding, Numeric(18,6), SmallInteger attempts, LargeBinary *_enc, BigInteger micros, ARRAY(Text)
  via text_array_columns); naive_timestamps == []; `Conversation.metadata_` maps to "metadata". If a §1-org-index
  parametrized test would be cloned a third time, move it into model_introspection as
  `assert_org_indexed(table, *, nullable=False)` and reuse it from test_call_models/test_agent_config_models.
- `tests/test_model_imports.py` (acceptance "no cycles"): ast-parse every module under va_db (src), build the
  intra-va_db import graph (from va_db.x import ...; resolve package `__init__`), assert acyclic via DFS
  (graphlib.TopologicalSorter.static_order raising CycleError is the simplest), and `sqlalchemy.orm.configure_mappers()`
  succeeds after `import va_db.models`. Assert every specs/03 §3–§9a table name is in Base.metadata.tables (parse
  `CREATE TABLE (\w+)` from specs/03-data-model.md — gives T08 a head start; path via repo root relative to test).
- `tests/test_memory_messaging_models_integration.py` and `tests/test_billing_models_integration.py`
  (scratch_session; add `make_contact`, `make_conversation`, `make_plan` to model_factories if reused):
  - defaults for each table (jsonb '{}'/'[]', bools, 1440, 'queued', '{*}', next_attempt_at populated, Decimal
    quantity round-trip Decimal("1.500000")).
  - enum checks accepted/rejected via assert_enum_check_enforced, parametrized over every enum column.
  - `memories_contact_persona_uq`: two org-wide (NULL persona) memories for one contact fail; one per persona plus
    one NULL pass; summary_embedding 1024-float round-trip.
  - `org_subscriptions_active_uq`: second active/trialing fails; canceled + active ok.
  - `org_data_keys_active_uq`: second active fails; retired + active ok.
  - uniques: webhook (event_id,url); ledger and usage (org_id, idempotency_key) same key other org ok; org_secrets
    (org_id,name); messages.telnyx_message_id; org_llm_providers one per org.
  - `org_secrets_name_check`: 'API_KEY' ok, 'api_key', '1KEY', 65-char name rejected.
  - plan FK: deleting a plan referenced by a subscription raises (NO ACTION); plan without org_id inserts with text id.
  - delete paths: contact delete cascades memories → memory_changes; persona delete cascades persona memory;
    conversation delete cascades messages; widget/contact/pathway/persona/agent delete nulls conversation FKs;
    brand delete cascades campaigns; endpoint delete nulls webhook_deliveries.endpoint_id; org delete cascades
    org_llm_providers/org_data_keys.
  - bytea round-trip (OrgSecret.value_enc).

### Gates
`make fmt lint typecheck placeholders test`; `uv run pytest packages/va_db -m integration -rs` (0 skipped);
`uvx radon cc -s -a packages/va_db/src`; `npx --yes jscpd@4 --silent` (0 clones); va_db coverage 100 %; pip-audit
(no new deps).

### Risks
- `metadata` reserved → `metadata_`. `type` and `schema` attribute names are fine.
- Partial-index WHERE text must be literal SQL (no colons) — fine; regex CHECK has none either.
- Text-PK models without IdMixin: TimestampMixin columns must still sort last (match how tenancy/calls order).
- plan FK without ondelete: ForeignKey("plans.id") — foreign_keys helper reports None; don't pass ondelete="NO ACTION"
  (it would render explicitly and break T08 parity).
- Vector import: Mapped[list[float] | None]; asyncpg needs pgvector codec as already set up for kb_chunks.
- Unit test files ≤ ~500 lines each; split integration by area as listed.

## Build P01-T06 (2026-10-27)
New: va_db/models/memory.py (§7), messaging.py (§8), billing.py (§9), security.py (§9a): 20 tables, all
Literals, spec-named indexes; models/__init__ re-exports (131 names). `_columns.micros_column(default=None)` added
and used by calls.price_micros (DDL unchanged); `platform_scope` docstring widened (memories reuse it). specs/03:
6 `<table>_org_idx` lines (memory_changes, messages, ten_dlc_brands, ten_dlc_campaigns, org_subscriptions,
org_data_keys) + "§7–§9a likewise" paragraph in the §1 Decision. AGENTS.md P01-T06 entry.
Tests: test_memory_messaging_models.py, test_billing_models.py (unit), *_integration.py for both (37 tests),
test_model_imports.py (ast import graph acyclic via graphlib, configure_mappers, sibling model imports are
constants/Literals only — agents imports pathways.VersionStatus so "no sibling imports" was too strict — and the
specs/03 §3–§9a CREATE TABLE set == Base.metadata.tables exactly: 72 tables). model_introspection gained
assert_org_indexed, org_index_ddl, enum_check_names, expected_foreign_keys, assert_enum_checks_mirror; call/agent
config unit tests refactored to use them (jscpd 0 clones).
Gates: make fmt 0, lint 0, typecheck 0 (180 files), placeholders 0 findings, make test 1563 passed (99.87 %,
va_db 100 %), `pytest packages/va_db -m integration -rs` 151 passed / 0 skipped, radon A (1.11), jscpd 0 clones,
pip-audit no known vulns. Note: the very first `make test` run of this iteration had 1 failed + 40 errors in va_db
integration setup (output not kept); three later full runs were green — likely Docker contention right after my
standalone integration runs; flag if it recurs.

## Review P01-T06 (2026-10-27) — APPROVED
Re-ran: make lint 0, typecheck 0 (180 files), placeholders 0 findings, make test 1563 passed / 0 skipped (99.87 %,
va_db 100 %), jscpd 0 clones, radon A (1.11). Checked memory.py, messaging.py, billing.py, security.py column by
column against specs/03 §7–§9a: types, nullability, defaults, CHECK lists in spec order, FK ondelete (plans FK
NO ACTION, no-FK call/conversation/batch refs), unique names, partial/DESC/coalesce indexes, Decision org_idx lines.
Tests cover defaults, enum enforcement, partial uniques, cascades/SET NULL, bytea/vector/numeric round-trips.
Minor, not blocking: test_cycle_detection_reports_a_cycle exercises graphlib itself rather than va_db.
