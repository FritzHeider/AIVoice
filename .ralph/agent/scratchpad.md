
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
