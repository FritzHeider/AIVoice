# AGENTS.md — Operational Guide for Coding Agents

Read this file at the start of every iteration. Keep it short, current, and factual. Add a line whenever you discover a
command, convention, or gotcha that a future iteration would otherwise rediscover. Remove lines that become false.

## Commands

| Purpose | Command |
|---|---|
| Install everything | `make setup` |
| Toolchain preflight | `uv run --no-project --python 3.12 python scripts/check_toolchain.py --search-path "$PATH"` |
| Start infra + simulators | `make up` (apps too: `make up PROFILE=apps`) |
| Stop | `make down` |
| Migrate DB | `make migrate` (SQL only: `uv run alembic -c packages/va_db/alembic.ini upgrade head --sql`; back out: `... downgrade base`) |
| Seed dev data | `make seed` (prints dev API key `va_test_…` and dashboard login) |
| Format | `make fmt` |
| All gates | `make check` |
| Unit + integration tests | `make test` (single package: `uv run pytest packages/va_voice -q`) |
| Simulator end-to-end | `make e2e` (single flow: `uv run pytest tests_e2e -m e2e -k barge_in`) |
| Dashboard e2e | `make e2e-ui` |
| Dashboard dev server | `corepack pnpm --filter dashboard dev` (port 3000; set `NEXT_TELEMETRY_DISABLED=1`) |
| One TS package's gates | `corepack pnpm --filter <dashboard\|@voiceagents/widget\|@voiceagents/sdk> run <lint\|typecheck\|test\|build>` |
| Regenerate TS API types | `make openapi` then `corepack pnpm --filter dashboard gen:api` and `corepack pnpm --filter @voiceagents/sdk gen:api` |
| Regenerate OpenAPI | `make openapi` (commit `openapi/openapi.json` and `openapi/internal.json`) |
| Run a service on the host | `uv run python -m va_edge.serve` · `-m va_api.serve` · `-m va_media.main` · `-m va_worker.main` · `-m va_webhooks.main` · `-m va_events_sink.main` · `-m va_sim_server.main` (all take `--host`/`--port`) |
| Pre-commit hooks | `uv run pre-commit install` once; `uv run pre-commit run --all-files` (ruff, placeholders, prettier, actionlint, gitleaks) |
| Lint CI workflows | `uv run actionlint` |
| Phase progress | `scripts/ralph/status.sh` |
| Run a phase | `scripts/ralph/run_phase.sh NN` |

## Conventions (quick reference)

- Specs live in `specs/`; shared contracts: `02` (layout/env), `03` (DDL/Redis), `19` (events/webhooks), `31` (errors).
- Python packages: `packages/va_<name>/src/va_<name>/`; apps: `apps/<app>/src/va_<short>/`.
- Tenant DB access only through `va_db.session.tenant_session(org_id)`; system-scope repos are explicitly named in specs/03 §2.
- Public ID prefixes: tools `TL-`, knowledge bases `KB-`, prompts `PT-` (`va_common.ids`).
- Money in micro-dollars (`int`). Time via injectable `va_common.time.Clock`.
- Plan ticks: `- [x] P05-T03 <title> — <evidence>`; commits: Conventional Commits + `Task: P05-T03` footer.

## Ports (local)

api 8000 · telephony-edge 8100 · media-worker 8200 · dashboard 3000 · docs 3100 · fake Telnyx 9100 · fake ElevenLabs 9200 ·
fake LLM 9300 · fake Stripe 9400 · Postgres 5432 · Redis 6379 · ClickHouse HTTP 8123 (native 9000 inside the container is
published as 9009) · Temporal 7233 · Temporal UI 8233 · MinIO API 9000 / console 9001 · Mailpit SMTP 1025 / UI 8025 ·
OTel collector 4317 (OTLP HTTP 4318, collector health 13133, collector metrics 8889) · Jaeger 16686 · Prometheus 9090 ·
Grafana 3001

## Gotchas

- Telnyx media WS frames may arrive out of order; always pass through `va_voice.reorder.ReorderBuffer`.
- ElevenLabs STT `previous_text` is only allowed on the first `input_audio_chunk` of a session.
- ElevenLabs TTS alignment times are relative to each audio chunk; convert to absolute with the running chunk offset.
- Telnyx webhook signature covers `"{timestamp}|{raw_body}"` — verify against the raw bytes before JSON parsing.
- Temporal workflow code must not import modules with side effects; use `workflow.unsafe.imports_passed_through()` for
  pydantic models.

## Learned during the build

(append dated bullets here: `- 2026-11-02 (P03-T05): <learning>`)
- 2026-10-25 (P00-T01): pnpm comes from corepack (`corepack enable`, or the Makefile `PNPM` fallback `corepack pnpm`).
  Without the root `package.json` `packageManager` pin corepack fetches the latest major (12.x) — never remove the pin.
- 2026-10-25 (P00-T01): mypy `files` + `mypy_path` in root `pyproject.toml` list only existing dirs (currently `scripts`);
  every new package/app must append its `src` (and `tests`) there or mypy will not check it.
- 2026-10-25 (P00-T01): pytest runs with `--import-mode=importlib`; modules in `scripts/` are importable in tests via
  `pythonpath = ["scripts"]` (e.g. `import check_toolchain`).
- 2026-10-25 (P00-T01): ruff ≥ 0.17 formats Python code blocks inside Markdown; root config excludes `*.md` so
  `make fmt` never rewrites spec samples. Do not remove that exclude.
- 2026-10-25 (P00-T01): Ralph backpressure rejects `build.done` (turns it into `build.blocked`) unless the payload TEXT contains
  `tests: pass`, `lint: pass`, `typecheck: pass`, `audit: pass`, `coverage: pass`, `duplication: pass`, `complexity: <number>`
  (`specs: pass` optional). It substring-matches a plain string: emit without `--json` (JSON keys never match). Get them
  for real: audit = `uvx pip-audit` on `uv export` + `corepack pnpm audit`; complexity = `uvx radon cc -s -a`;
  duplication = `npx --yes jscpd@4`.
- 2026-10-25 (P00-T02): `.env.example` mirrors the specs/02 §6 table one-to-one in table order (106 keys since P02-T01);
  `scripts/tests/test_env_example.py` fails when a variable exists on only one side, when a literal dev value differs,
  or when an assignment loses its trailing comment. Add new variables to the spec table and `.env.example` in the same
  commit. The mirror rules (literal / empty / free dev-value classes) are the `> Decision (2026-10-25)` block under
  that table; the parsers live in `scripts/env_spec.py`.
- 2026-10-25 (P00-T02): a stale `.DS_Store` inside `.venv/lib` makes `uv sync` fail with
  `failed to remove directory .venv/lib: Directory not empty`. Fix: `find .venv -name .DS_Store -delete && rm -rf .venv`
  then re-sync. This repo lives on an external volume where Finder writes `.DS_Store` everywhere.
- 2026-10-25 (P00-T03): `make placeholders` scans git-tracked AND untracked-not-ignored files under apps/ packages/
  sdks/ infra/ scripts/ (new files are checked before `git add`). Ad-hoc run on specific files:
  `uv run python scripts/check_no_placeholders.py path/a.py path/b.ts`. Rules and allowances: specs/02 §9.1 Decision.
- 2026-10-25 (P00-T03): the gate scans its own source and tests. Write banned tokens in checker regexes with a
  splitting character class (`T[O]DO`), and in test fixtures build them from fragments (`j("TO", "DO")` or
  `"place" + "holder"`). Never use implicit concatenation (`"TO" "DO"`): `ruff format` joins it into the literal.
- 2026-10-25 (P00-T03 review): the `empty-function` rule is a plain regex on `) {}` / `=> {}`, so empty `if (x) {}`
  blocks and TS parameter-property constructors (`constructor(private readonly x: X) {}`) are findings. Declare
  fields explicitly and assign them in a non-empty constructor body instead.
- 2026-10-25 (P00-T04): `check_spec_refs.py` pairs a `§N` only with a spec reference earlier on the SAME line, so write
  `specs/02 §9.1` (or `specs/02-repo-layout-and-conventions.md §9`) on one line; a bare `§N` is unchecked prose.
  Short `specs/NN` must match exactly one spec file. Run alone: `uv run python scripts/check_spec_refs.py`.
- 2026-10-25 (P00-T04): `check_openapi_drift.py` skips (exit 0) only while both `va_api` and `openapi/openapi.json`
  are absent; P00-T11 must add both together, and `scripts/export_openapi.py` must write the file with
  `check_openapi_drift.render` (the single canonical serializer) or the gate will report drift.
- 2026-10-25 (P00-T05): adding a workspace package = member dir under `packages/` (src layout, `uv_build` backend,
  `py.typed`) + entry in the root `[dependency-groups] dev` with `[tool.uv.sources] <name> = { workspace = true }`
  (so plain `uv sync` installs it) + append its `src` to mypy `mypy_path` and `src`/`tests` to mypy `files`.
- 2026-10-25 (P00-T05): mypy runs without the pydantic plugin, so `Settings(_env_file=None)` is a `call-arg` error.
  In tests pass it through a typed kwargs dict (`cls(**{"_env_file": None})` via a `dict[str, Any]`), and give every
  settings field a default. `packages/va_common/tests/conftest.py` clears the settings env vars and `chdir`s to
  `tmp_path` so a developer `.env` never leaks into tests.
- 2026-10-25 (P00-T05): orjson wraps exceptions raised by a `default=` handler in `JSONEncodeError` with a generic
  message; the original is on `__cause__` (`va_common.json.dumps` re-raises it as `TypeError`).
- 2026-10-25 (P00-T06): log through `va_common.logging.get_logger(__name__)` after `configure_logging(settings,
  version=...)`; bind call fields with `bind_call_context` / `bind_turn` / `bound_context`. Tests capture output with
  `configure_logging(settings, stream=io.StringIO(), environ={}, pod="p")`; the `reset_logging` autouse fixture in
  `packages/va_common/tests/conftest.py` resets structlog, contextvars, and the root handler (copy it into other
  packages' conftest when they configure logging). Console format must not include `format_exc_info` (structlog
  warns, and pytest `filterwarnings=error` turns that into a failure). structlog `get_logger` returns a lazy proxy,
  not a `BoundLogger` instance, so do not `isinstance`-check it.
- 2026-10-25 (P00-T07): services that store tenant secrets extend `va_common.settings.CryptoSettings` and build
  `Envelope(provider, store)` inside `async with open_kms_provider(settings) as provider`. Test doubles
  (`InMemoryDataKeyStore`, `FakeKmsClient`, `CountingProvider`) live in `packages/va_common/tests/crypto_fakes.py`;
  that directory is on pytest `pythonpath` and mypy `mypy_path`, so tests `from crypto_fakes import ...`. Generate
  key material at test time (`os.urandom`, `secrets.token_hex`), never as literals. Never parametrize with random
  values: pytest-xdist workers collect different test IDs and the run errors; parametrize sizes instead.
- 2026-10-25 (P00-T07): aioboto3 clients take `aiobotocore.config.AioConfig`, not `botocore.config.Config` (mypy
  rejects the latter). boto3-named keyword parameters in protocols/fakes (`KeyId`, ...) need `# noqa: N803` per line.
- 2026-10-25 (P00-T08): build Redis and HTTP clients only with `va_common.redis.redis_client(settings)` and
  `va_common.http.http_client(settings, timeout=...)` (timeout is required; never construct `httpx.AsyncClient` or
  `redis.asyncio.Redis` directly). Readiness uses `redis_ping(client, timeout_s=1.0)` (ruff ASYNC109 bans an async
  `timeout` parameter). Services start with `va_common.observability.init(...)` and call `.shutdown()` on exit.
- 2026-10-25 (P00-T08): tracing tests use `setup_tracing(..., span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
  set_global=False)` (helper: `obs_helpers.memory_provider()` in `packages/va_common/tests`); the default sampler
  ratio is 0.1, so pass `otel_traces_sampler_arg=1.0` when a test expects spans. Metrics tests use
  `build_metrics(service, new_registry(process_collectors=False))`, never the prometheus_client global `REGISTRY`.
  prometheus_client 0.26 exposes `# TYPE <name>_total counter` with the full counter name.
- 2026-10-25 (P00-T08): Docker-dependent tests are `@pytest.mark.integration` and skip when the daemon socket is
  unreachable; probe it with `docker_probe.docker_available()` (`packages/va_common/tests`): `docker.from_env()`
  leaks a socket on failure and `filterwarnings=error` turns the ResourceWarning into a session error.
- 2026-10-25 (P00-T09): `make up` runs `scripts/dev_up.sh` with `PROFILE ?= infra`; profiles are comma-separated
  (`make up PROFILE=infra,observability` adds Jaeger 16686, Prometheus, Grafana; `observability` alone does not start
  Postgres etc.). `make down` stops every profile and keeps volumes; `scripts/dev_down.sh -v` wipes them. The Docker
  daemon here is OrbStack (`orb start`); `docker compose config` works without a daemon.
- 2026-10-25 (P00-T09): `docker compose up --wait` exits 1 when a one-shot container exits (even with code 0), so
  `dev_up.sh` waits on the long-running services and runs one-shot jobs (`ONE_SHOT_SERVICES`, now `minio-init`) via
  `docker compose run --rm`. Add new one-shot services to that list.
- 2026-10-25 (P00-T09): MinIO no longer publishes images (`minio/minio`, `minio/mc` are gone from Docker Hub; Quay
  401s). Compose uses `bitnamilegacy/minio:2024.10.13` (data in `/bitnami/minio/data`, no `command`) and
  `bitnamilegacy/minio-client:2024.10.8`; use the same images for CI service containers. `mc mb -p` already means
  `--ignore-existing` (passing both errors). `temporalio/ui` tags are full semver (`2.31.2`).
- 2026-10-25 (P00-T09): `scripts/tests/test_compose.py` keeps `docker-compose.yml` in lockstep with the specs/29 §2
  YAML block (image, ports, environment, command, entrypoint, ulimits, profiles), the specs/02 §2 image pins, the
  Ports line above, and `.env.example`. Change them together.
- 2026-10-25 (P00-T10): `scripts/fetch_models.sh` downloads models and verifies them against `models/MANIFEST.json`
  (committed; the `.onnx` is git-ignored). `--check` verifies without downloading, `--update` re-pins after a
  deliberate model change. Logic and tests: `scripts/fetch_models.py`, `scripts/tests/test_fetch_models.py`.
- 2026-10-25 (P00-T10): `assets/audio/` is generated by `uv run python scripts/generate_audio_assets.py` and must be
  byte-identical to a regeneration (`--check`, enforced by `scripts/tests/test_generate_audio_assets.py`). After
  changing the generator, regenerate and commit the assets in the same commit. For anything committed, draw
  randomness only from `numpy.random.PCG64(seed).random_raw()`, never `Generator` methods (their streams may change
  between numpy versions). Do not use `audioop` (deprecated, `filterwarnings=error`); use the μ-law codec in the
  generator.
- 2026-10-25 (P00-T11): run `make openapi` after any route or response-model change and commit `openapi/openapi.json`;
  `make check` fails on drift. The drift gate imports the module-level `va_api.main:app`, so a developer `.env`
  with a non-default `OPENAPI_PUBLIC_SERVER_URL` makes it fail locally (tests build the app from explicit settings).
- 2026-10-25 (P00-T11): build API routers only with `va_api.envelopes.legacy_router(prefix=...)` /
  `envelope_router(prefix=...)` and add them to `va_api.main.ROUTERS`. Put the prefix on the router, never on
  `include_router`: FastAPI 0.162 keeps an include-time prefix out of `route.path_format`, which the metrics
  `route` label and `x-envelope` lookup read from `scope["route"]`. Every operation needs `operation_id=`, one tag,
  `openapi_extra={"x-scope": ...}`, and a response model with an `examples` entry (`test_operations_contract`).
- 2026-10-25 (P00-T11): API tests build `create_app(settings, container_factory=FakeChecks(), init_observability=False,
  routers=...)` and talk to it through `api_helpers.make_client(app)` (asgi-lifespan + `httpx.ASGITransport(
  raise_app_exceptions=False)`, so unhandled errors come back as 500s like under uvicorn). Helpers live in
  `apps/api/tests/support/` (on pytest `pythonpath` and mypy `mypy_path`); test dirs themselves must NOT be on
  `mypy_path` or every package's `conftest.py` collides as module `conftest` in the single `uv run mypy` run. New
  apps follow the same layout (`apps/<app>/tests/support/`).
- 2026-10-25 (P00-T11): the 500 handler runs in Starlette's outermost `ServerErrorMiddleware`, outside our middleware,
  so `va_api.errors.error_response` sets `X-Request-Id` and the security headers itself. The request id lives in
  `scope["va.request_id"]`, never `scope["state"]`: asgi-lifespan passes one shared lifespan-state dict to every
  request. Readiness checks use `va_common.health.run_checks` (concurrent, 1 s each, details logged only).
- 2026-10-25 (P00-T12): services share `va_common.service`: `probe_routes()` / `probe_app()` serve `/healthz`
  `/readyz` `/metrics` from `app.state.probes` (`ProbeState`: version, registry, `checks`, `liveness` returning a
  failure reason); `service_scope` / `service_lifespan` / `probe_service_app` wire observability + the container;
  `Supervisor(stop.task_failed)` runs background tasks (a crash stops the process with exit 1); `serve(app,
  parse_bind(...), stop=..., drain=...)` owns SIGTERM/SIGINT via `ManagedServer` (uvicorn never installs handlers).
  Never call `uvicorn.run` in a service. Dependencies come from `va_common.checks.open_dependencies()`
  (`deps.postgres/redis/temporal/clickhouse(...)` create the client, register cleanup, and add the readiness check in
  call order). Metrics/probe ports: worker 9300, dispatcher 9400, sink 9500; on the host they collide with the fake
  LLM/Stripe, so run the sim with `--port 19100` next to them.
- 2026-10-25 (P00-T12): service tests use `packages/va_common/tests/service_helpers.py`: `settings_of(Cls, **kw)`,
  `isolate_env(monkeypatch, tmp_path, *SettingsClasses)` in an autouse `isolated_env` fixture, `reset_logging`
  (autouse, re-export it from the app `conftest.py` with `__all__`), `FakeContainer`, `make_client`,
  `readiness_of`, `UNREACHABLE` URLs, `track_closes`, `capture_serve`, `json_log_stream`/`log_events`, and
  `RecordingServer` (a `ManagedServer` with a `ready` event and `url`, for real-uvicorn tests on port 0; never poll
  with sleeps). App-specific helpers go in `apps/<app>/tests/support/<short>_helpers.py` (unique module names; add
  the dir to pytest `pythonpath` and mypy `mypy_path`). A uv_build app whose package name differs from the
  distribution needs `[tool.uv.build-backend] module-name = "va_x"` in its pyproject.
- 2026-10-25 (P00-T12): uvicorn calls `sys.exit()` on startup failure (port in use, lifespan error); in an asyncio
  task that SystemExit escapes the event loop, so `run_server` converts it to `ServerStartupError`. Cancelling a
  running server must still let uvicorn close its sockets (`_abort`), or pytest's unraisable-socket warning fails a
  later test.
- 2026-10-25 (P00-T13): TS workspace = `apps/dashboard` (`dashboard`), `apps/widget` (`@voiceagents/widget`),
  `sdks/typescript` (`@voiceagents/sdk`). Shared root files: `tsconfig.base.json` (strict + `exactOptionalPropertyTypes`,
  `noUncheckedIndexedAccess`, `verbatimModuleSyntax`), `eslint.base.mjs` (`base({tsconfigRootDir, react})`:
  strict-type-checked + stylistic, react-hooks + jsx-a11y for React, prettier last), `.prettierrc.json`,
  `.prettierignore`. Root devDependencies hold everything `eslint.base.mjs` imports. Every package has `format`, `lint`
  (`eslint --max-warnings 0` + `prettier --check --ignore-path ../../.prettierignore`: prettier only reads the ignore
  file in cwd), `typecheck`, `test` (Vitest + v8 coverage, 80 % thresholds), `build`. Pinned majors: Next 15.5,
  React 19.2, TypeScript 5.9 (typescript-eslint does not support TS 7), ESLint 9, Vitest 4.1 (3.x pulls critical advisories), Vite 7, zod 4,
  `@testing-library/jest-dom` 7 (6.10 is a deprecated breaking release).
- 2026-10-25 (P00-T13): the committed `schema.d.ts` files (dashboard `src/lib/api/`, sdk `src/`) are drift-checked by
  each package's `tests/schema.test.ts`; after `make openapi` run both `gen:api` scripts or `make test-ts` fails.
- 2026-10-25 (P00-T13): Vitest tests that read files with `new URL(..., import.meta.url)` need
  `// @vitest-environment node` in jsdom packages (jsdom's `URL` is rejected by `node:fs`). Dashboard tests inject a
  route-table fetch (`tests/fakes.ts` `fakeFetch`) instead of msw; the SDK uses msw `setupServer` with
  `onUnhandledRequest: "error"` and the `RUNTIME` symbol seam (`src/internal.ts`) to record retry sleeps.
- 2026-10-25 (P00-T13): `.jscpd.json` (repo root) excludes the generated `schema.d.ts` files; run duplication with
  `npx --yes jscpd@4 --silent`. Dashboard themed tokens use CSS `light-dark()` (declare dark values once).
- 2026-10-25 (P00-T13): `apps/dashboard/next-env.d.ts` is generated by `next dev/build` and git-ignored (it references
  `.next/types/routes.d.ts`, absent on a clean clone). `next build` adds `esModuleInterop` to `tsconfig.json` if missing;
  keep it there. `next build` skips ESLint (`eslint.ignoreDuringBuilds`). The widget `build` fails when
  `dist/widget.js` exceeds 60 KB gzip (`apps/widget/scripts/check-size.mjs`).
- 2026-10-25 (P00-T13): the placeholder gate scans TS too: no `) {}` / `=> {}` bodies (give callbacks a statement,
  e.g. `() => undefined` for a never-resolving promise executor is fine), no `console.log` outside `scripts/` dirs.
  The dashboard reads the api origin from `NEXT_PUBLIC_API_BASE_URL` (`src/lib/env.ts`, inlined at build time).
- 2026-10-25 (P00-T14): `.github/workflows/ci.yml` jobs: `check` (runs `make check` with Postgres/Redis/ClickHouse/
  MinIO service containers that mirror docker-compose.yml, applies `infra/docker/postgres-init/01-roles.sql` with psql,
  writes `reports/pytest.xml` + `reports/coverage.xml` via `PYTEST_ADDOPTS`), `ts-build` (`pnpm -r run build` + `gen:api`
  drift diff), `security` (gitleaks full history, `uvx pip-audit --strict` on `uv export`, `pnpm audit --audit-level
  high`), `actionlint`. Changing a compose image/credential means changing the CI service too
  (`scripts/tests/test_ci_workflow.py` asserts they match). gitleaks reads `.gitleaks.toml` (default rules + scoped
  false-positive allowlists); keep `GITLEAKS_VERSION` in ci.yml equal to the pre-commit `rev`. Local hooks run tools
  through `uv run`/`corepack pnpm exec`, so they use the locked versions.
- 2026-10-25 (P00-T14 review): never `importlib.reload` an app module in tests; it rebinds module globals (`app`) in
  place and breaks identity asserts in tests that imported the old object (order-dependent under xdist). Use
  `monkeypatch.delitem(sys.modules, mod)` + `monkeypatch.setattr(pkg, name, original)` and `importlib.import_module`.
- 2026-10-25 (P00-FIX-1): `uv run --no-project --python X` prepends the interpreter's directory to PATH (e.g.
  `/opt/homebrew/bin`), which shadows nvm's node 22 with Homebrew's node 26. Tools that must match the shell's PATH get
  it passed explicitly (`check_toolchain.py --search-path "$$PATH"` in the Makefile).
- 2026-10-25 (P00-FIX-1): exit-gate clean clones go under `/Users/...`: Docker Desktop cannot bind-mount from
  `/var/tmp` clones (collector config "not a directory"). The compose project name is fixed (`voiceagents`), so a
  clone's `make up` replaces the main stack; run `make down` there and `make up` from the main repo afterwards.
- 2026-10-27 (P01-T01): `docker_probe.docker_available()` resolves the daemon like testcontainers does (properties
  file, `DOCKER_HOST`, then the active docker context, e.g. OrbStack `~/.orbstack/run/docker.sock`). Before this,
  integration tests silently skipped on OrbStack machines without `DOCKER_HOST`. Import containers from
  `testcontainers.community.<name>` (`testcontainers.redis`/`.postgres` emit a DeprecationWarning, an error here).
- 2026-10-27 (P01-T01): DB access: `va_db.Database.from_settings(settings)` builds a `va_app` and a `va_system`
  engine (one `DATABASE_URL`; the role is the asyncpg `server_settings={"role": ...}` startup parameter;
  specs/03 §2 Decision). Never assume a role with `SET ROLE` from a pool `"connect"` listener: it runs in an
  implicit transaction, and a rolled-back first transaction (exception or pool reset) silently reverts the
  connection to the login role, which bypasses RLS for a superuser/owner login. Services call
  `configure_database(db)` at startup and `await db.dispose()` on exit; code uses
  `va_db.session.tenant_session(org_id)` (transaction + `set_config('app.org_id', ..., true)`) or
  `system_session()` (specs/03 §2 repositories only). Postgres integration tests use
  `pg_container.running_postgres()` (`packages/va_db/tests/support`, `pgvector/pgvector:pg16`, spec roles + an
  `rls_probe` table).
- 2026-10-27 (P01-T02): models live in `va_db.models.<area>` (re-exported from `va_db.models`). Every `mapped_column`
  passes an explicit SQL type (`Text`, `CITEXT`, `INET`, `JSONB`, `ARRAY(Text)`, ...): an unannotated
  `Mapped[SomeLiteral]` becomes a native `Enum`. Enumerations are a module-level `Literal` alias plus
  `check_in("<column>", get_args(Alias))` in `__table_args__` (constraint name `<table>_<column>_check`), so the
  Literal is the single source of truth. Indexes are always named explicitly as in the spec (no `index=True`).
  `inet` columns come back as `ipaddress.IPv4Address`/`IPv6Address` with SQLAlchemy 2.1 + asyncpg (annotate
  `Mapped[IpAddress | None]`). `IdMixin.id` has `sort_order=-1` so `create_all` column order matches the spec.
  Model integration tests (`test_tenancy_models_integration.py`) run `CREATE EXTENSION` + `Base.metadata.create_all`
  inside one login-role transaction that is rolled back at teardown (DDL is transactional), with an
  `AsyncSession(join_transaction_mode="create_savepoint")` and `begin_nested()` around expected IntegrityErrors.
  The session-scoped `pg_url` fixture is in `packages/va_db/tests/conftest.py`.
- 2026-10-27 (P01-T03): model tests share helpers in `packages/va_db/tests/support/`: `model_introspection`
  (`get_table`, `columns`, `enum_check_values`, `check_names`, `server_defaults`, `index_ddl`, `unique_and_pk`,
  `foreign_keys`, `naive_timestamps`, `text_array_columns`; name the helper `get_table`, not `table`, because tests
  parametrize on `table`) and `model_factories` (`flushed`, `make_org`, `make_user`, `assert_enum_check_enforced`).
  Integration tests take the `scratch_session` fixture (conftest.py: every model table created in a rolled-back
  login-role transaction). New model areas reuse these instead of copying, which keeps jscpd at 0 clones. Partial
  indexes use `Index(..., postgresql_where=text("status IN ('pending', 'active')"))`; mixin columns land after the
  class's own columns (SoftDeleteMixin before TimestampMixin in the bases puts `deleted_at` before `created_at`).
- 2026-10-27 (P01-T04): `va_db.models._columns` holds the column builders every model module uses: `tz()`,
  `org_fk()`, `org_id_column(nullable=False)`, `fk_column(target, ondelete, nullable=True)`, `int_column(n)`,
  `bool_column(default=...)`, `text_column("x")`, `text_array_column("{}")`, `jsonb_column(default=None)` and
  `jsonb_default(literal)`. Use them instead of spelling out `mapped_column(...)`; jscpd (0 clones) flags the
  repeated column blocks otherwise. Import lists that still overlap may be wrapped in `# jscpd:ignore-start` /
  `# jscpd:ignore-end  (reason)` (ruff ERA001 flags a bare end marker as commented-out code). `jsonb_default` escapes `:` because `text("'{\"style\":0}'")` otherwise turns `:0` into a
  bind parameter (pin the compiled default in a unit test whenever a JSON literal has colons). `pgvector`
  (0.5.x, ships `py.typed`, no mypy override needed) provides `pgvector.sqlalchemy.Vector`; it binds lists as text and
  reads `list[float]` back, so annotate `Mapped[list[float]]`. Expression and DESC indexes are declared after the
  class (`Index("voices_name_scope_uq", func.coalesce(Voice.org_id, text("'...'::uuid")), func.lower(Voice.name),
  unique=True, postgresql_where=Voice.deleted_at.is_(None))`). Generated columns use `Computed(..., persisted=True)`;
  `model_introspection.server_defaults` skips them and `computed_columns` reports them. `Mapped[Any]` makes a column
  NOT NULL, so nullable JSONB of any shape is `Mapped[Any | None]` with `nullable=True`. `scratch_session` also runs
  `CREATE EXTENSION IF NOT EXISTS vector`. `model_factories.assert_check_enforced(session, builder, column, accepted,
  rejected)` covers range/regex CHECKs and `assert_rejected(session, model, match)` covers UNIQUE violations.
  specs/03 §1 Decision: every tenant table has an `org_id`-leading index (13 §5 tables gained `<table>_org_idx`).
- 2026-10-27 (P01-T05): column names that clash with Python/SQLAlchemy names keep the spec name with an
  attribute suffix: `Call.metadata_ = mapped_column("metadata", JSONB, ...)` (`metadata` is reserved on Declarative
  classes) and `CallTurn.text_ = mapped_column("text", Text, ...)` (it would shadow `sqlalchemy.text` in the class
  body). Platform-scoped uniques (`org_id` NULL = platform row) use `_columns.platform_scope(Model.org_id)` for the
  `coalesce(org_id, nil-uuid)` key (voices, dnc_entries). `call_events` is partitioned: no mixins, no FKs on
  `org_id`/`call_id`, `PrimaryKeyConstraint("created_at", "id")` (a partitioned PK must contain the partition key)
  and `{"postgresql_partition_by": "RANGE (created_at)"}` as the last `__table_args__` item; `created_at` has only a
  server default, and asyncpg + RETURNING fill it at flush. `create_all` makes the parent only, so tests create
  partitions themselves (`CREATE TABLE call_events_2026_10 PARTITION OF call_events FOR VALUES FROM (...) TO (...)`;
  bounds may be expressions such as `date_trunc('month', now())`); an insert with no matching partition fails with
  `no partition of relation "call_events" found for row`. Migration and P01-T13 partitions are named
  `call_events_pYYYYMM` (specs/03 §10 Decision). Model unit tests use `model_introspection.spec_column_mismatches` (spec
  column strings, `?` = nullable), `assert_enum_check_mirrors`, and `leading_columns` (the org-index rule).
  `batch_items.call_id` has no FK on purpose: `calls.batch_item_id` points the other way, and a cycle would break
  create_all/drop ordering.
- 2026-10-27 (P01-T06): model modules by specs/03 section: `tenancy` §3, `telephony` §4, `library`/`pathways`/
  `agents` §5, `calls` §6, `memory` §7, `messaging` §8, `billing` §9 (webhooks + billing), `security` §9a. Model
  modules reference other tables only through string FKs and may import only shared constants/Literals from a
  sibling (`EMBEDDING_DIMENSIONS`, `VersionStatus`), never a model class; `tests/test_model_imports.py` enforces
  an acyclic va_db import graph, `configure_mappers()`, and that the `CREATE TABLE` names of specs/03 §3–§9a equal
  `Base.metadata.tables`. System-scope tables (`plans`, `stripe_events`) skip `IdMixin` and declare
  `id: Mapped[str] = mapped_column(Text, primary_key=True)` first (no org_id, no RLS). An FK with no `ON DELETE` in
  the spec is `ForeignKey("plans.id")` (never `ondelete="NO ACTION"`, which renders explicitly). Micro-dollar
  columns use `_columns.micros_column(default=None)` (bigint NOT NULL). Org-index tests use
  `model_introspection.assert_org_indexed(table, nullable=...)` and `org_index_ddl(tables)`. Re-inserting a
  duplicate text-PK row in a test needs `session.expunge(original)` first (SQLAlchemy warns on the identity
  conflict, an error under filterwarnings).
- 2026-10-27 (P01-T07): Alembic lives in `packages/va_db` (`alembic.ini`, `migrations/env.py`, `migrations/versions/`).
  It reads `DATABASE_URL` and runs as the login role, one transaction per revision. Revisions run SQL through
  `execute_script(sql)` (see `0001_initial.py`): asyncpg prepares every statement, so a multi-statement string
  fails, and `op.execute(str)` wraps the string in `text()`, which reads `"style":0` as a bind parameter. The
  helper splits at top-level `;` and escapes such colons. Every migration that adds a table must
  `GRANT SELECT, INSERT, UPDATE, DELETE` on it to `va_app, va_system` (there are no default privileges) and add
  RLS/trigger statements per specs/03 §10. The partition naming and the RLS table rule are in the specs/03 §10
  Decision. Migration tests use `tests/support/migration_helpers` (`fresh_database(pg_url)` creates
  `mig_<hex>` on the session server; `upgrade`/`downgrade(url)`; `query(url, sql, **params)` autocommits as
  the login role and returns tuples; `alembic_config(StringIO)` for `--sql` output; `load_revision("0001_initial.py")`).
  These tests are sync because env.py calls `asyncio.run`. The roles-absent test renames the worker
  server's roles aside (`spec_roles_set_aside`) instead of booting a second container: under the full suite the
  Docker daemon timed out creating it. Test Postgres runs with `fsync=off`; before that, the forced checkpoint of `DROP DATABASE`
  timed out (30 s) under xdist load. `ruff` per-file ignores for `packages/va_db/migrations/**`: `INP001`,
  `N999` (plus `RUF001` in 0001, whose spec comments are verbatim). A compose volume created before this task
  has `va_system` without BYPASSRLS; `make migrate` fixes it.
- 2026-10-27 (P01-T08): `test_schema_matches_spec.py` applies every relational specs/03 ```sql block (§2-§10, via
  `model_introspection.spec_sql_blocks()`, split with 0001's `statements()`, run with `exec_driver_sql` so no
  `text()` bind parsing) to one fresh DB and compares `schema_catalog.snapshot()` (columns with `format_type`,
  constraints, indexdefs, triggers, policies, RLS flags, extensions, non-extension functions) with the migrated DB.
  `test_autogenerate_parity.py` runs Alembic `compare_metadata` (type + server default) of `Base.metadata` against
  the migrated DB and must return `[]`; the only exclusions are `alembic_version` and `call_events_pYYYYMM` in
  `schema_catalog.include_object`. Alembic 1.20 compares expression/partial indexes without warnings. Add any
  future autogenerate-noise filter only as a named, commented exception there. Drift negatives mutate a
  `Table.to_metadata` copy, never `Base.metadata`. `migrated_url` is a module-scoped fixture in the va_db conftest.
- 2026-10-27 (P01-T09): `test_rls_isolation.py` proves tenant isolation for every table in
  `tenant_rows.TENANT_TABLE_NAMES` (NOT NULL `org_id` plus `voices`/`dnc_entries`, derived from metadata, so a new
  tenant table is covered automatically) and checks that list equals the catalog's FORCE-RLS `tenant_isolation`
  tables. `tenant_rows.RowFactory` builds minimal rows from metadata (required columns only; required FKs get fresh
  parents in the same session; enum CHECKs take their first value). A CHECK that the generic value violates needs a
  commented `VALUE_OVERRIDES` entry; an unknown column type raises `TypeError`. Semantics: RLS `USING` hides B's
  rows (SELECT empty, UPDATE/DELETE rowcount 0); only `WITH CHECK` raises (INSERT with `org_id = B`, or moving A's
  row to B). Build a test `Database` with `tenant_rows.open_database(url)`. `migration_helpers.query` calls
  `asyncio.run`, so call it only from sync tests.
- 2026-10-27 (P01-T10): data access goes through `va_db.repositories` (not re-exported from `va_db`).
  `TenantRepository(Model, session, org_id, clock=...)` inside `tenant_session(org_id)` gives get/list/create/update/
  soft_delete; `list(limit=, cursor=, where=[...])` returns `va_db.pagination.Page` (`.pagination()` is the response
  object; cursor format in the specs/31 §2 Decision). Absent/deleted/other-org rows raise `NotFoundError` (code
  `NOT_FOUND`); unique violations raise `AppError("CONFLICT")`. Make ORM changes *inside* `session.begin_nested()`:
  entering it autoflushes pending changes into the outer transaction, where an IntegrityError aborts the whole
  transaction. A failed savepoint expires the objects it touched (reading an attribute then raises `MissingGreenlet`;
  keep ids, re-`get`). System repos subclass `SystemRepository`; the set must equal the `...Repo` names in the
  specs/03 §2 `va_system` bullet (`test_repositories.py`), so add a new one to the spec first. With SQLAlchemy 2.1,
  `select(Model)` is typed `Select[Model]` (not `Select[tuple[Model]]`) and `Result.tuples()` is deprecated (an
  error under filterwarnings); rows unpack directly.
- 2026-10-27 (P01-T11): seed-data migrations (`0002_seed_plans.py`) are hand-written `INSERT`s run with
  `op.execute(sa.text(...))` (no colons, one statement); the downgrade deletes exactly the seeded keys and
  fails on FK references rather than dropping data. The head revision is now `0002`, so a migrated test DB
  already holds the `start`/`build`/`scale`/`enterprise` plans: tests that insert plans use random ids.
  `tests/support/plan_catalog.parse_spec_plans()` parses the specs/24 §2 table and seed-value paragraph;
  `test_seed_plans*.py` compare it with the offline SQL and the migrated `plans` rows, so a spec price change
  needs a new seed migration.
- 2026-10-27 (P01-T12): `packages/va_events` (dist `va-events`) owns the ClickHouse schema: packaged
  `va_events/clickhouse/migrations/NNNN_name.sql` (uv_build ships the `.sql` files; read them with
  `importlib.resources`), `apply_migrations(client, clock=...)` (records `va.schema_migrations`, raises
  `ClickHouseSchemaDriftError` when an applied file changed: never edit an applied file, add `0002_*.sql`), and
  `open_clickhouse(CLICKHOUSE_URL)` (clickhouse-connect 1.11 `AsyncClient` on database `default`; DDL and queries
  qualify `va.`). `create_async_client` does a network round-trip (server version/settings), so open it in a
  supervised task, never in `build_container`. clickhouse-connect wraps network failures and retried server errors in
  `OperationalError` (a subclass of `DatabaseError`, which non-retried SQL errors raise). Server-side parameters
  (`{name:String}`) work in `INSERT ... SELECT`. The events sink runs `bootstrap_schema` in its lifespan
  (`create_app(schema_connect=..., clock=..., stop=...)` test seams; `app.state.schema.wait()` in tests instead of
  polling). Fakes: `packages/va_events/tests/support/ch_support.py` (`FakeClickHouse`, spec §12 parsers) and
  `apps/events_sink/tests/support/sink_helpers.py` (`FakeConnect`, `never_connects`, `RecordingClock`). ClickHouse
  integration tests use `testcontainers.community.clickhouse.ClickHouseContainer("clickhouse/clickhouse-server:24.8")`
  on the mapped 8123 port, one test per module (each xdist worker running a test boots its own container).
- 2026-10-27 (P01-T13): `va_db.partitions` maintains `call_events` partitions (`call_events_pYYYYMM`, UTC month
  `[start, next start)`, same as 0001): `ensure_call_event_partitions(conn, now, months_ahead=3)` and
  `drop_call_event_partitions_before(conn, cutoff)` run in the caller's transaction and return the names they
  created/dropped. They need a connection of the login role that owns `call_events` (`create_engine(settings, None)`):
  `va_system` fails with `permission denied for schema public` (create) or `must be owner` (drop). Other partition
  names (test-only `call_events_current`, `call_events_2026_10`) are never dropped.
- 2026-10-28 (P02-T01): api auth lives in `va_api.auth` (`get_principal`, `require_scope`, `ApiKeyAuthenticator`); key
  format/hashing primitives in `va_common.auth`. The api lifespan fails with `RuntimeError("API_KEY_PEPPER is not
  configured")` unless `API_KEY_PEPPER` is 64 hex; `api_helpers.make_settings()` supplies a random one. Auth tests use
  `auth_helpers.KeyStore` (in-memory `ApiKeyLookupRepo` stand-in) + `AuthContainer` (fakeredis + `FrozenClock`); build
  hashers directly with `ApiKeyHasher(Pepper(secrets.token_bytes(32), 1))`. FastAPI ≥ 0.16x nests included routers
  in `app.routes` (`_IncludedRouter`): enumerate `app.state.routers` to reach `APIRoute`s and `route.dependant`.
- 2026-10-29 (P02-T02): request rate limiting: `va_common.token_bucket` (`TokenBucket(redis).take(key, BucketLimit(rate,
  burst), now_ms=...)`, Lua via `register_script`) and `va_api.rate_limit` (plan table, keys, `RateLimiter`,
  `RateLimitHeadersMiddleware`). `get_principal` charges the `default` bucket after auth; routes that send calls add
  `Depends(rate_limit("calls_send"))` (from `va_api.auth`). Header semantics and fail-open: specs/04 §11 Decision.
  `va_api.rate_limit` must not import `va_api.auth` at runtime (cycle; `RequestContext` is TYPE_CHECKING-only). Root
  dev deps use `fakeredis[lua]` (lupa), so Lua scripts run in unit tests; an in-test `asyncio.gather` of 100 real-Redis
  commands needs `redis_client(..., max_connections=100)` (default pool 50 raises `MaxConnectionsError`). Auth tests
  issue keys on the `build` plan by default (`KeyStore.issue(plan_id="start")` for small buckets); the frozen clock
  means buckets never refill unless the test calls `container.clock.advance(...)`.
- 2026-10-29 (P02-T03): `Idempotency-Key` is claimed inside `require_scope` (after auth, `default` bucket, scope check);
  `va_api.idempotency.IdempotencyMiddleware` (inside `RateLimitHeadersMiddleware`) stores or releases the claim before
  the last body chunk; replays are an `IdempotentReplay` exception rendered by its own handler. `va_api.idempotency`
  must not import `va_api.auth` at runtime (cycle, same as `rate_limit`). Tests: `AuthContainer.idempotency` is a
  `MemoryIdempotencyStore` (`apps/api/tests/support/idempotency_store.py`, same rules as `IdempotencyRepo`);
  route-level tests use `idempotency_helpers.idempotency_app` (counts endpoint runs in `container.hits`, `/gate` held
  by `container.gate`). An api test needing Postgres boots its own module-scoped container with the va_db support
  helpers (`running_postgres` + `fresh_database` + `upgrade`; see `apps/api/tests/test_idempotency_db.py`).
  Routes returning a one-time secret (minted API keys) must call
  `va_api.idempotency.withhold_on_replay(request, "api_key")` (dotted paths such as `"data.key"` for envelope
  bodies): the stored body and every replay carry `null` there (specs/04 §2.1 decision). To assert on the real
  `idempotency_keys` table from a session test, `monkeypatch.setattr(container, "idempotency",
  DbIdempotencyStore(identity.database, container.clock))` (see `test_orgs_integration.py`).
- 2026-10-29 (P02-T04a): `va_common.auth` is a package (`api_keys`, `passwords`, `tokens`); `from va_common.auth import …`
  still exports everything. Passwords: one `PasswordHasher()` per process (construction runs one argon2 hash for the
  dummy), use `hash_async`/`verify_async`/`check_password_policy_async` in handlers; tests use cheap
  `Argon2Params(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)`. Rebuild the breached list with
  `uv run python scripts/build_password_list.py` (pinned SecLists commit + sha256; output is byte-identical). uv_build
  ships non-`.py` files under the module dir (the `.gz` is in the wheel).
- 2026-10-29 (P02-T04a): email lives in `va_common.email` (`EmailSettings` mixed into api/worker settings,
  `SmtpEmailSender`, `InMemoryEmailSender`, `verify_email`). `EmailSettings` sits in the email package, not in
  `va_common.settings`, because `va_common.logging` imports `va_common.settings` (cycle). Tests with a real SMTP server
  use `aiosmtpd.controller.Controller` on a free 127.0.0.1 port (`test_email.py`). `check_no_placeholders` flags
  `example.com` in production code (docstrings too); tests may use it.
- 2026-10-29 (P02-T04b): identity tables without `org_id` (`orgs`, `users`, `sessions`, `email_tokens`) go through
  `Database.global_session()` (`va_app`, no `app.org_id`: RLS tables read empty and reject writes) with
  `va_db.repositories.IdentityRepo`; a user's memberships across orgs come from the `va_system`
  `MembershipLookupRepo`. Create orgs only with `va_db.provisioning.OrgProvisioner` (org + active data key +
  encrypted `whsec_` secret + owner, one transaction); signup uses `provision_in(session, org_id, ...)` inside its
  own `tenant_session(org_id)` to add the login session/verify token atomically. Unique-violation savepoints:
  `va_db.repositories.conflict_savepoint(session, message, field=...)`.
- 2026-10-29 (P02-T04b): the api container now needs `LOCAL_MASTER_KEY` (64 hex, `openssl rand -hex 32`) as well as
  `API_KEY_PEPPER`; both may be empty at settings construction, `build_container` raises `RuntimeError` before
  opening anything. `apps/api/tests/support/api_helpers.make_settings` fills both with random values. Production
  settings tests must pass `kms_provider="aws", aws_kms_key_id=...` or the local-KMS refusal fires first.
- 2026-10-29 (P02-T04c): dashboard sessions: `va_api.auth.sessions.SessionManager` (start/resolve/revoke, sliding +
  absolute expiry, `sess:{hash}` cache), `va_api.auth.session_auth` (cookie → `ActiveSession` with CSRF/Origin for
  writes, active membership, MFA gate, `RequestContext`), `va_api.auth.rbac.ROLE_SCOPES`, `/internal/auth/*` in
  `va_api/routers/auth.py` (`include_in_schema=False`, `openapi_extra={"x-public": "session-flow"}`).
  `get_principal` uses the API key whenever `Authorization` is non-blank, else the `va_session` cookie, else fails as
  an invalid key. Envelope success bodies use `va_api.responses.DataResponse[T]`. `AppError(..., headers={...})`
  adds response headers (e.g. `Retry-After` on `AUTH_LOCKED`); they are not part of the error item. Session tests:
  `apps/api/tests/support/session_helpers.py` (`migrated_postgres()` context manager for a module fixture,
  `SessionContainer(identity=...)` with cheap argon2, `make_member`, `login`, `write_headers`, a
  `/v1/knowledge/_session/whoami` probe) served over `https://` via `make_client(app, base_url=...)` because cookies
  are `Secure` outside `VA_ENV=local` and httpx drops `Secure` cookies on http. `httpx.ASGITransport` reports peer
  `127.0.0.1` (login throttling uses it); Starlette's `TestClient` reports `testclient`, which `client_ip` maps to
  `None`. `IdentityRepo.create_session(created_at=...)` takes the caller's clock so frozen-clock expiry tests agree.
- 2026-10-29 (P02-T04d): signup / verify-email / resend-verification live in `va_api/routers/auth.py`. Signup runs
  provisioning, `SessionManager.start_in` (no cache write) and the verify token in one `tenant_session`, then
  `SessionManager.remember` after the commit (pool size 1: never open another session inside it). Verification
  emails go out as FastAPI `BackgroundTasks` (`va_api.auth.verification.send_verification`; failures logged, never
  raised); httpx's ASGI transport runs them before the call returns, so tests read `InMemoryEmailSender` directly.
  The container's `email_sender` is `InMemoryEmailSender` only under `VA_ENV=test`, else `SmtpEmailSender` (Mailpit
  in local dev). Resends are limited at `auth:verify:resend:<user_id>` (3/h). `make openapi` now writes
  `openapi/internal.json` too (`app.state.internal_openapi()`, every `/internal/*` route, `SessionCookie` scheme) and
  `check_openapi_drift.py` checks both; a fake app for the script tests needs `state.internal_openapi()`.
  `DataResponse[T]` schemas carry `T`'s examples wrapped in `{data: ...}` (FastAPI drops `None` values from
  examples). Tests: `SessionContainer(identity=identity)` (has `provisioner`, `outbox`, `resend_limiter`),
  `session_helpers.signup(client, email=...)`, `link_token(container.outbox, email)`. `MFA_REQUIRED` is 401.
- 2026-10-29 (P02-T05): password forgot / reset / change live in `va_api/routers/auth_password.py` and attach to
  `internal_auth_router`; `va_api/main.py` imports the module for that side effect (`# noqa: F401`), so a new
  module adding routes to an existing router needs the same import or its routes silently vanish
  (`test_openapi.INTERNAL_PATHS` catches it). `forgot` always returns 200; the 3/h reset limit counts
  `email_tokens` rows (`count_issued_since`), no Redis key. Reset peeks the token (`IdentityRepo.peek_email_token`)
  before the policy check so a weak password does not burn the link, then consumes it in the write transaction.
  Revoking sessions inside your own transaction: `IdentityRepo.revoke_user_sessions` + `SessionManager.forget(hashes)`
  after the commit. Recent auth for change = session `created_at` (or, since P02-T06b, `mfa_passed_at`) younger than
  10 min. Emails share one layout in
  `va_common.email.templates._Copy`; senders go through `va_api.auth.verification.deliver(..., failure_event=)`.
  Tests: `link_token(container.outbox, email, "password_reset")`.
- 2026-10-29 (P02-T06a): MFA primitives are `va_common.auth.mfa` (TOTP `match_totp(secret, code, now) -> counter |
  None`, `otpauth_uri`, `qr_svg` via `segno`, recovery codes `new_recovery_codes` / `hash_recovery_codes` /
  `match_recovery_code`); DB writes are `va_db.repositories.MfaRepo` (conditional UPDATEs returning bool). Users
  have no org, so the TOTP secret uses `Envelope.platform_encrypt_str` (a fresh nil-org KMS data key per blob,
  specs/28 §3 (j)). A column added by `ALTER TABLE ... ADD COLUMN` lands last, and `test_schema_matches_spec`
  compares attnum: list it LAST in the specs/03 DDL (as `sessions.mfa_passed_at`, migration 0003). Offline
  migration SQL in tests: `migration_helpers.offline_sql(action, revision)`.
- 2026-10-29 (P02-T06b): MFA routes live in `va_api/routers/auth_mfa.py` (imported for side effect in `main.py`),
  bodies in `routers/auth_mfa_schemas.py`. The MFA gate is `session_auth.gated_membership(request, active)` (replaces
  `enforce_mfa`): it fires when the org requires MFA **or** the user enabled it (`MembershipRecord.user_mfa_enabled`,
  or a user lookup when there is no membership); use it on every gated `/internal/auth/*` route. Code checks go
  through `va_api.auth.mfa_guard.MfaGuard` (`auth:fail:mfa:<id>` lock + `mfa:last:<id>` Lua replay guard); counters
  with a lock share `va_api.auth.failure_counter.FailureCounters` (the login throttle uses it too - do not copy
  it). Recent auth = `max(created_at, mfa_passed_at)`; `SessionManager.mark_mfa_passed(active)` writes DB + cache.
  Tests: `session_helpers.enrol_mfa(client, container)` (setup + enable, then advances the frozen clock 30 s) and
  `mfa_code(container, secret)`; a code is accepted once per step, so advance the clock 30 s before reusing "the
  current code" across sessions. A pydantic `model_validator` error renders as `INVALID_INPUT` without `field`.
- 2026-10-29 (P02-T07a): finer-than-scope checks use `va_api.auth.permissions.require_permission("<name>")` next to
  `require_scope` (route `dependencies=[...]` run before parameter dependencies, so the permission message wins);
  declare both in `openapi_extra={"x-scope": ..., "x-permission": ...}`. API keys with `*` hold every permission
  except `org.delete`, `org.owner_transfer`, `sso.manage`. `/v1/org*` routes go on `routers/org_sso.org_router`
  (already in `ROUTERS`). Outbound fetches to admin-supplied URLs (OIDC discovery) go through
  `va_api.auth.oidc_discovery.OidcDiscovery` (no redirects, 5 s, 256 KiB cap, global-address check);
  `allow_insecure` (http + private addresses) is true only for `VA_ENV` `local`/`test` (`discovery_for(settings,
  http)`). Tests: `SessionContainer` builds `oidc_discovery` from the app settings when the app opens it; stub the IdP
  with `respx.mock` (it patches httpcore, so the ASGI test client is untouched). Integration modules share one
  database: use per-test unique values (domains, issuers) for anything with cross-org uniqueness.
  `session_app(container, oidc_enabled=True)` turns SSO on.
- 2026-10-29 (P02-T07b): the OIDC login is `va_api/routers/auth_sso.py` (side-effect import in `main.py`):
  `GET /internal/auth/sso/start|callback` answer **302 only** (`RedirectResponse`, failures go to
  `{DASHBOARD_BASE_URL}/login?sso_error=<reason>`; `REASONS` lists them). Never import `authlib.jose` or
  `authlib.integrations.httpx_client` (both warn, and warnings are errors): use authlib's RFC 6749/7636 primitives
  through `va_api.auth.oidc_client` and joserfc for ID tokens (`va_api.auth.oidc_tokens.validate_id_token`).
  authlib has no type information: `[[tool.mypy.overrides]] authlib.*` ignores missing imports, so wrap its return
  values in typed functions. Provider requests (discovery, JWKS `fetch_json`, token `post_form`) all go through
  `OidcDiscovery`. Login state: `va_api.auth.sso_state.SsoStateStore` (`sso:state:{state}`, `SET NX EX 600`,
  `GETDEL`) plus the `va_sso` cookie (`Path=/internal/auth/sso`). `routers.auth.revoke_presented` and
  `set_session_cookies` are public for other login flows; `SessionManager.start_in(..., mfa_passed=True)` also
  sets `mfa_passed_at`. Tests: `tests/support/fake_oidc.py` (`FakeOidcProvider(now=container.clock.now)`
  `.install(respx_router)`, `.authorize(location)` records the authorization request, `.claims` overrides the next
  ID token, `None` removes a claim; `configure_sso(client, identity, container, enforce=..)` creates an org with a
  connection via `PUT /v1/org/sso` and clears cookies; `sso_login(client, provider, email)`). RSA test keys are
  generated once at import. OpenAPI redirect operations are listed in `test_openapi.REDIRECT_OPERATIONS`.
- 2026-10-29 (P02-T08a): `va_api.auth.rbac_matrix` is the single RBAC source: `MATRIX` (specs/04 §3.2 rows,
  labels verbatim, `—` em dash for denied) and `ROW_GRANTS` (row → scope resource + permissions); `ROLE_SCOPES`,
  `ROLE_PERMISSIONS`, `PERMISSIONS`, `SESSION_ONLY_PERMISSIONS` are derived/defined there and re-exported by
  `rbac.py`/`permissions.py`. Editing the §3.2 table means editing `MATRIX`/`ROW_GRANTS` (`test_rbac_matrix.py`
  compares cell for cell). `rbac_matrix` imports only `va_api.auth.scopes` and `va_db.models` (no cycle).
  `/v1/me` lives on `routers/org.me_router` (in `ROUTERS`); `GET/PATCH /v1/org` attach to `org_sso.org_router` from
  `routers/org.py` (imported by `main.py`). `orgs` has no RLS: use `va_db.repositories.OrgRepo` on
  `global_session()` with `ctx.org_id` only, never an org id from the request; the tenant `ApiKeyRepo(session,
  org_id).get(key_id)` reads one org's key. Tests: `session_helpers.store_api_key(container, org_id, scopes)` issues
  a key the in-memory store authenticates and inserts its `api_keys` row (for routes that read the key from the DB).
  FastAPI drops `None` members from OpenAPI examples, so response examples with nullable fields render without them.
- 2026-10-29 (P02-T08b): members routes (`GET /v1/org/members`, `PATCH`/`DELETE /v1/org/members/{user_id}`,
  `POST /v1/org/transfer-ownership`) live in `routers/org_members.py` on `org_sso.org_router` (imported by
  `main.py`). Owner safety is `va_db.repositories.MembershipRepo.change_role/remove_member/transfer_ownership`: each
  locks the org row (`lock_org()`, `SELECT … FOR UPDATE` on `orgs`) before counting owners, so concurrent demotions
  serialise (`LAST_OWNER_MESSAGE`). Invitations (P02-T08c) must use the literal `/members/invitations` paths; the
  `{user_id}` routes are `uuid.UUID`-typed and there is no `GET /members/{user_id}`, so they cannot collide. Recent
  auth for other actions: `password_reset.require_recent_auth(active, now, message=...)` with the session from
  `request.state.session`. `DataResponse[list[Model]]` wraps each model example as a one-item list in OpenAPI.
  Tests: `session_helpers.signed_in(client, identity, container, role)` and `add_member(identity, hasher, org_id,
  role)` (a second user in an existing org; log them in on another `session_client(session_app(container))`).
  Lock-wait tests: poll `SELECT count(*) FROM pg_locks WHERE NOT granted` on a third `open_database(url)` (bounded
  loop, no sleeps).
- 2026-10-29 (P02-T08c): invitations. Public routes (`POST`/`GET /v1/org/members/invitations`, `DELETE
  .../{invitation_id}`) live in `routers/org_invitations.py`; the token routes (`GET
  /internal/auth/invitations/{token}`, `POST /internal/auth/invitations/accept`) in `routers/auth_invitations.py`
  (both imported by `main.py`). Data: tenant `va_db.repositories.InvitationRepo` (`create` replaces an open invite
  of the same email, `list_open`, `get_open`, `revoke` = DELETE, `consume` = conditional UPDATE) and the system-scope
  `InvitationLookupRepo.by_token` (listed in specs/03 §2; `test_repositories.py` counts 10 system repos). Shared
  pieces: `org_members.owner_guard(ctx, role)`, `routers.auth.check_sso_enforced(request, email)`,
  `session_auth.optional_session(request)` (session or `None`; CSRF/Origin enforced only when one resolves),
  `va_api.auth.invitations.send_invitation`, template `va_common.email.invitation(...)` (`_Copy.render(values=...)`
  substitutes `$name`s into intros, escaped in HTML). Pydantic: `Field(repr=False)` inside an `Annotated` alias used
  as `Alias | None` raises `UnsupportedFieldAttributeWarning` (an error under filterwarnings); put the `Field(...)`
  on the attribute instead. Tests: `session_helpers.invite(client, container, email, role) -> token`
  (`link_token(..., "invitation")`), `INVITATIONS`, `ACCEPT_INVITATION`; switch users on one client with
  `client.cookies.clear()` + `login(...)`.
- 2026-10-29 (P02-T09a): sub-accounts. Legacy `orgs_router` (`routers/orgs.py`, in `main.ROUTERS`): `POST
  /v1/orgs/create`, `GET /v1/orgs`, `GET /v1/orgs/{org_id}` (body `{"status":"success","org":{...}}`, not flat:
  the item's own `status` field would collide), `POST .../suspend|reactivate`; all need `subaccounts.manage`.
  Create = one `tenant_session(child_id)`: `provision_in(..., kyc_level=)` + `ApiKeyRepo.add` + optional
  `va_db.repositories.CreditTransferRepo.transfer(from, to, micros, idempotency_key=)` (locks both org rows in id
  order; available = balance − `held` holds; reused by P15-T07). Cross-org rows inside one `va_app` transaction:
  `va_db.session.use_org(session, org_id)` switches `app.org_id` and restores it (RLS still checks every row; no
  `va_system`). Key minting: `va_api.auth.api_keys.mint_api_key(hasher, env)` (`MintedKey.plaintext` repr-hidden)
  and `creating_user_id(ctx, session)`; `Container.api_key_hasher` / `session_auth.api_key_hasher_of`, `redis_of`.
  Tests: `SessionContainer.authenticator` falls back to the DB lookup, so route-minted keys authenticate;
  `session_helpers.fund(container, org_id, "50")` (grant row + balance) and `hold(container, org_id, "15", status)`.
