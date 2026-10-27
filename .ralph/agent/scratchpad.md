
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
