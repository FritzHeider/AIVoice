# Memories

## Patterns

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
