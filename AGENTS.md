# AGENTS.md — Operational Guide for Coding Agents

Read this file at the start of every iteration. Keep it short, current, and factual. Add a line whenever you discover a
command, convention, or gotcha that a future iteration would otherwise rediscover. Remove lines that become false.

## Commands

| Purpose | Command |
|---|---|
| Install everything | `make setup` |
| Toolchain preflight | `uv run --no-project --python 3.12 python scripts/check_toolchain.py --search-path "$PATH"` |
| Start infra + simulators | `make up` (apps too: `make up PROFILE=apps`) |
| Build one Python image | `docker build -f infra/docker/python.Dockerfile --build-arg SERVICE=sim --build-arg MODULE=va_sim_server.main -t va-sim:dev .` |
| Copy sim key into `.env` | `uv run python scripts/sim_public_key.py` (run by `make up` when `sim` starts) |
| Stop | `make down` |
| Migrate DB | `make migrate` (= `uv run python -m va_db.migrate`: head + `call_events` partitions ahead; SQL only: `uv run alembic -c packages/va_db/alembic.ini upgrade head --sql`; back out: `... downgrade base`) |
| Seed dev data | `make seed` (prints dev keys `va_live_…`/`va_test_…`, dashboard login, a curl line; `make seed ARGS="--json"`, `--email`, `--reset-password`; needs `make up && make migrate`) |
| Format | `make fmt` |
| All gates | `make check` |
| Unit + integration tests | `make test` (single package: `uv run pytest packages/va_voice -q`) |
| Pipeline benchmark | `uv run pytest -m bench packages/va_voice` (rewrites `packages/va_voice/BENCHMARKS.md`; commit it) |
| Simulator end-to-end | `make e2e` (runs `scripts/dev_up.sh e2e`, rebuilding images, then `pytest tests_e2e -m e2e`; one flow on a running stack: `uv run pytest tests_e2e -m e2e -k outbound_basic -s`) |
| Single-pod capacity check | `make e2e-capacity` (50 concurrent calls on one media worker, marker `capacity`, also part of `make e2e`; `E2E_CONCURRENT_CALLS=10` for fewer; on a running stack: `uv run pytest tests_e2e -m capacity -s`) |
| Profile a compose service | `docker run --rm --pid=container:voiceagents-media-1 --cap-add SYS_PTRACE <image with py-spy + procps> py-spy record --nonblocking --gil -p <pid> -d 15 -f raw -o out.txt` (pid via `ps -eo pid,args` in the same namespace, usually 7) |
| Dashboard e2e | `make e2e-ui` |
| Live vendor smoke | `LIVE_TESTS=1 LIVE_TELNYX_API_KEY=… LIVE_ELEVENLABS_API_KEY=… LIVE_LLM_API_KEY=… uv run pytest tests_live -m live -rs` (optional `LIVE_FROM_NUMBER`, `VA_ENV`; commit the generated `tests_live/fixtures/stt_phrase.*` after the first run) |
| Dashboard dev server | `corepack pnpm --filter dashboard dev` (port 3000; set `NEXT_TELEMETRY_DISABLED=1`) |
| One TS package's gates | `corepack pnpm --filter <dashboard\|@voiceagents/widget\|@voiceagents/sdk> run <lint\|typecheck\|test\|build>` |
| Regenerate TS API types | `make openapi` then `corepack pnpm --filter dashboard gen:api` and `corepack pnpm --filter @voiceagents/sdk gen:api` |
| Regenerate OpenAPI | `make openapi` (commit `openapi/openapi.json` and `openapi/internal.json`) |
| Run a service on the host | `uv run python -m va_edge.serve` · `-m va_api.serve` · `-m va_media.main` · `-m va_worker.main` · `-m va_webhooks.main` · `-m va_events_sink.main` · `-m va_sim_server.main` (all take `--host`/`--port`) |
| Pre-commit hooks | `uv run pre-commit install` once; `uv run pre-commit run --all-files` (ruff, placeholders, prettier, actionlint, gitleaks) |
| Lint CI workflows | `uv run actionlint` |
| Telnyx bootstrap | `uv run python scripts/telnyx_bootstrap.py --env-file .env [--write-env] [--json]` (sim by default; ids on stdout) |
| Regenerate NPA → state table | `uv run python scripts/generate_npa_states.py` (`--check` for drift; commit `packages/va_common/src/va_common/data/npa_states.json`) |
| Phase progress | `scripts/ralph/status.sh` |
| Run a script on the EC2 Ralph runner | `echo 'uptime' \| scripts/ralph/ssm.sh "<comment>"` (SSM; profile `aivoice`, finds the instance by tag; env overrides in the script header) |
| Run a phase | `scripts/ralph/run_phase.sh NN` |

## Conventions (quick reference)

- Specs live in `specs/`; shared contracts: `02` (layout/env), `03` (DDL/Redis), `19` (events/webhooks), `31` (errors).
- Python packages: `packages/va_<name>/src/va_<name>/`; apps: `apps/<app>/src/va_<short>/`.
- Tenant DB access only through `va_db.session.tenant_session(org_id)`; system-scope repos are explicitly named in specs/03 §2.
- Public ID prefixes: tools `TL-`, knowledge bases `KB-`, prompts `PT-` (`va_common.ids`).
- Money in micro-dollars (`int`). Time via injectable `va_common.time.Clock`.
- Plan ticks: `- [x] P05-T03 <title> — <evidence>`; commits: Conventional Commits + `Task: P05-T03` footer.
- Cross-tenant suite (`apps/api/tests/test_cross_tenant_routes.py`, specs/04 §14): every route is probed unless it
  carries `openapi_extra={"x-public": "<reason>"}` (also required on `x-scope: public` routes). A new path parameter
  needs a factory in `apps/api/tests/support/cross_tenant_factories.py`; request bodies come from the body model's
  first `examples` entry or `REGISTRY.register_request(op, json=..., params=...)`; required query params need a
  registered request. A non-2xx owner control is declared with `REGISTRY.register_owner_status(op, status)`.

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
  Write `complexity: 2.45,` (comma or space after the number): `complexity: 2.45.` at the end of a sentence is not
  parsed and the event is rejected; complexity must also stay below 10.
- 2026-10-25 (P00-T02): `.env.example` mirrors the specs/02 §6 table one-to-one in table order (107 keys since P04-T03);
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
- 2026-10-25 (P00-T13): `.jscpd.json` (repo root) excludes the generated `schema.d.ts` files, the resolved Send Call snapshots, and the frozen engine-state fixtures; run duplication with
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
- 2026-10-27 (P01-T07): Alembic lives in `packages/va_db` (`alembic.ini`; since P05-T17a the scripts are packaged in
  `src/va_db/migrations/env.py` and `src/va_db/migrations/versions/`).
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
  timed out (30 s) under xdist load. `ruff` per-file ignores for `packages/va_db/src/va_db/migrations/**`: `INP001`,
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
- 2026-10-29 (P02-T09b): parent access to sub-accounts. `get_principal` calls
  `va_api.auth.sub_accounts.act_on_sub_account(request, ctx, rate_per_sec)` right after authentication, so the rate
  limit, scope check, and idempotency claim already see the child context; never read `ctx.org_id` as "the org owning
  the principal" — use `ctx.principal_org_id` (parent while acting) for the API key / membership rows (`/v1/me`,
  `api_keys.creating_user_id`, which switches RLS with `use_org` for the key lookup). Routes that must refuse acting
  principals check `ctx.acting_parent_org_id` (transfer-ownership). Sessions: `session_auth.resolve_switch_target`
  (own membership, else `MembershipLookupRepo.via_parent`; roles `PARENT_ACCESS_ROLES`) backs both
  `active_membership` and `POST /internal/auth/switch-org` (`routers/auth_switch_org.py`, side-effect import in
  `main.py`); `routers.auth.session_info(request, response, active)` builds the `GET /session` body. Org status gate:
  `session_auth.check_org_status(status, method)`. Tests: `session_helpers.sub_account(client, name, **headers) ->
  (child_id, key)`; contextvars-bound log fields need `capture_logs(processors=[merge_contextvars])`.
- 2026-10-29 (P02-T10): API key management. Envelope `api_keys_router` (`routers/api_keys.py`, schemas in
  `routers/api_keys_schemas.py`, in `main.ROUTERS`): `GET|POST /v1/api_keys`, `PATCH /v1/api_keys/{key_id}`,
  `POST .../revoke|roll`, all `require_scope("api_keys:write")` (create answers 201). Rules live in
  `va_api.auth.api_keys`: `grantable_scopes(ctx)`, `normalize_scopes`, `check_grantable`, `check_environment`
  (test principals manage only test keys), `check_expires_at`, `check_manages` (developer = own keys), and
  `invalidate_cached_keys(request, prefixes)` (log-and-swallow `apikey:{prefix}` delete; also used by `orgs.py` —
  monkeypatch `va_api.auth.api_keys.invalidate_api_key_cache` to simulate Redis failures). Redis cache keys use the
  hash tag form `apikey:{<prefix>}`: build them with `api_key_auth.cache_key(prefix)` in tests. `ApiKeyRepo` gained
  `list_keys`, `rename`, `revoke` (keeps the first `revoked_at`), `expire`, and `add(..., expires_at=)`. Tests:
  `store_api_key(container, org_id, scopes, environment="test")` issues test keys; `/v1/me` needs `org:read`, so a
  narrow key used to probe authentication must hold it.
- 2026-10-29 (P02-T11a): Audit log. Write rows with `va_api.audit.audit(session, ctx, "<action>", resource_type,
  resource_id, before=..., after=...)` inside the route's open session (tenant or global) before it closes; it calls
  `va_db.audit.record`, which flushes the row under `use_org(actor org)`, so a parent's row can be written in a
  sub-account's transaction and a failed request leaves nothing. New actions: add them to the specs/04 §13 list
  **and** `va_db.audit.AUDIT_ACTIONS` (`test_audit_unit` compares the two); an unknown action is `ValueError`.
  Pass views/dicts, never ORM rows; `redact` replaces secret-named keys (`password`, `token`, `key`, `*_enc`, ...)
  and API-key strings, and raises `TypeError` on non-JSON values (bytes). Write no row for a no-op. Migration
  `0004` revokes `UPDATE, DELETE` on `audit_logs` from `va_app` (append-only): tests must not modify audit rows as
  `va_app` (seed or clean up through `system_session`); `test_rls_isolation` expects "permission denied" there.
  `GET /v1/audit_logs` (`routers/audit_logs.py`) returns `va_api.responses.PageResponse[T]` (`data`, `errors`,
  `pagination`) — reuse it for later cursor-paginated envelope lists. Test helper:
  `session_helpers.audit_rows(identity, org_id, action=None)`.
- 2026-10-29 (P02-T11b): Every route with a mutating method (POST/PUT/PATCH/DELETE) needs `**audited("<action>",
  ...)` or `**audit_exempt("<reason>")` (from `va_api.audit`) merged into its `openapi_extra`;
  `apps/api/tests/test_audit_markers.py` walks every router and fails on an unmarked route (a GET that mutates may
  carry `audited` too). Dashboard auth routes have no `RequestContext`: use `va_api.audit.audit_user(session,
  request, org_ids, user_id, action, after=...)` (one row per org of the user). Fetch `memberships_of_user`
  (system pool) **before** opening the va_app session that writes the row. Logout-style writes use
  `SessionManager.revoke_in` / `revoke_all_in` in the caller's transaction, then `forget(hashes)` after commit.
- 2026-10-29 (P02-T12a): `packages/va_domain` (dist `va-domain`, deps pydantic, email-validator, tzdata) holds shared
  Pydantic domain models: `va_domain.org.OrgSettings` (every org setting, specs/04 §7), `va_domain.voices.VoiceSettings`,
  `va_domain.scheduling.CallingWindow` + `windows_overlap`. `orgs.settings` is stored **sparse** (only explicitly set
  keys): read with `load_settings(stored)`, write `sparse_settings(model)`. Parse PATCH bodies with `OrgSettings` itself
  and read `model_dump(mode="json", exclude_unset=True)` (exactly what was sent), then `merge_settings(stored, sent)`
  (groups key-wise, everything else replaced), `needs_compliance(sent)` for the `org.compliance` permission, and
  `field_path(error["loc"])` for `INVALID_INPUT` fields. Never add cross-group model validators to `OrgSettings`.
  `pydantic.EmailStr` rejects `.test` domains (used by every fixture and the dev seed); validate emails with
  `email_validator.validate_email(..., check_deliverability=False, test_environment=True)` as `va_domain.org` does.
- 2026-10-29 (P02-T12b): read org settings outside the API through `va_db.org_settings.OrgSettingsReader(database,
  redis).get(org_id)` (Redis `orgsettings:{org_id}`, 60 s, DB fallback; it opens its own global session, so never call
  it while another session of the pool is open). Any code that writes `orgs.settings` must take `OrgRepo.lock(org_id)`
  in its transaction and call `reader.invalidate(org_id)` after the commit. API writes go through
  `va_api.routers.org_settings.check_settings_permissions` + `apply_settings_patch(session, ctx, sent, prefix)` (both
  `PATCH /v1/org/settings` and `PATCH /v1/org` use them). The container exposes the reader as `org_settings`
  (`session_auth.org_settings_of(request)`; `SessionContainer.org_settings` in tests: replace it to inject a broken
  Redis). `INVALID_INPUT` `field` everywhere is `va_domain.org.field_path` format: `items[0].name`, not `items.0.name`.
- 2026-10-29 (P02-T12c): `get_principal` enforces org `ip_allowlist` (`va_api.auth.ip_allowlist`) right after
  authentication; the client address everywhere is `session_auth.client_ip(request)` (the ASGI peer; behind a proxy
  uvicorn rewrites it from `X-Forwarded-For` for `FORWARDED_ALLOW_IPS` only). Tests that need another peer wrap the app
  in `tests/support/peer.py` `PeerSwitch(session_app(container), host="203.0.113.7")` and pass it to `session_client`
  (set `peer.host` between requests; `"testclient"` imitates a non-IP peer). A settings PATCH sending a non-empty
  `ip_allowlist` must include the caller (httpx ASGI peer `127.0.0.1`). `SessionManager(..., org_settings=reader)`
  takes the sliding window from the active org's `session_max_age_days`; build the reader first and share it.
- 2026-10-29 (P02-T13): agent onboarding (specs/04 §10) lives in Redis only: `va_api.auth.agent_onboarding`
  (`AgentRequestStore` with compare-and-set `transition(...)` and atomic `take_key`, `OnboardingLimiter`),
  public routes in `routers/agent_onboarding.py`, approval routes in `routers/auth_agent_onboarding.py` (on
  `internal_auth_router`, imported for side effect in `main.py`). Fixed-window counters go through
  `va_api.auth.window_counter.hit_window(..., unavailable_event=<event> | None)` (`None` = fail closed with
  `SERVICE_UNAVAILABLE`). Redis compare-and-set: `pipe.watch(key)` → read → `pipe.multi()  # type: ignore[no-untyped-call]`
  → write with `keepttl=True`; `WatchError` means another change won. Tests: `link_token(outbox, email,
  "agent_key_request")`; `make_member` users are **unverified** (set `users.email_verified_at` when a flow needs it);
  an unhandled exception inside a route answers 500 through the app handler (assert the status, not a raise).
- 2026-10-29 (P02-T14): re-running `make seed` rolls the two `dev-seed` keys (old ones revoked) because raw keys are never
  stored; the owner password is printed only on first creation or with `ARGS=--reset-password`. Credit is granted once
  (`va_db.repositories.grant_credits`, idempotency key `seed:dev-credits`). The seed refuses any `VA_ENV` other than
  local/test. Manual smoke: `make seed` then `curl -H "Authorization: <test key>" localhost:8000/v1/me`.
- 2026-10-29 (P02-T15): `tests/support/cross_tenant.app_routes(app)` is the shared route enumerator (test_audit_markers
  uses it); `session_app(container, extra_routers=(...))` mounts test-only routers. The suite clears `rl:*` before each
  call instead of advancing the clock. FastAPI 0.13x has no `get_flat_dependant`; walk `dependant.dependencies`.
- 2026-10-29 (P03-T01): simulators: library `packages/va_sim` is distribution `va-sim`; the runner `apps/sim` is
  `va-sim-server` (module `va_sim_server`). Every sim port serves the control API under `/sim/*` (`POST /sim/reset`
  with optional `{"seed"}`, `GET /sim/clock`, `GET /sim/state`; FakeTelnyx adds `POST/GET /sim/telnyx/scenarios` and
  `/sim/recordings/{id}.wav`; `/sim/{elevenlabs|llm}/config` PATCH + `/config/profile`). Add vendor routes in
  `va_sim.server.simulator_routes(sim, state)`; all four apps share one `SimState`. In tests use
  `async with running_sims(state) as urls:` (real uvicorn on ephemeral ports, name → base URL) or an
  `httpx.ASGITransport` over `Starlette(routes=simulator_routes(sim, state))`; builders in
  `packages/va_sim/tests/support/sim_helpers.py`.
- 2026-10-29 (P03-T01): `va_sim.clock.AcceleratedClock.sleep` returns without wall delay: virtual time jumps to the
  earliest pending deadline after a few event-loop turns, so concurrent sleepers wake in deadline order. Work that
  waits on real I/O does not hold virtual time back; drive accelerated flows through the clock, and use
  `advance(s)` to move time from a test. `RealtimeClock` is the wall clock.
- 2026-10-29 (P03-T01): `va_sim.scenario.load_scenario(text, source=..., base_dir=...)` raises `ScenarioError` whose
  `issues` carry `path` (`steps[2].say.text`), 1-based `line`/`column`, `message`; `str(error)` is one
  `source:line:col: path: message` line per issue. Step union discriminator tags (`step:<key>`) appear in pydantic
  `loc` and are stripped from paths. `va_common.service.ReadyServer` (a `ManagedServer` with `ready` and `url`) is
  the production twin of the test helper `RecordingServer` (now an alias).
- 2026-10-29 (P03-T02a): FakeTelnyx lives in `va_sim.telnyx` (`routes.telnyx_routes(state)` is added by
  `simulator_routes("telnyx", ...)`; keep `va_sim.telnyx/__init__.py` import-free — `va_sim.state` imports
  `telnyx.store`/`webhooks`/`keys`, so handler modules import `SimState` only under `TYPE_CHECKING`). REST auth is
  `Authorization: Bearer sim-telnyx-key` (more keys: `POST /sim/telnyx/accounts {"api_key"}`), resources are per
  key. Knobs: `PATCH /sim/telnyx/config` (`force_duplicate_webhooks`, `fail_number_orders`,
  `failing_message_destinations`); delivery log `GET /sim/telnyx/webhooks`; public key `GET /sim/telnyx/public-key`
  and `.sim/telnyx_public_key` (written by `apps/sim` at startup). Tests: `telnyx_helpers.telnyx_state(transport)`
  passes `webhook_transport=RecordingTransport([...status or None...])` so webhooks arrive in-process with the
  sim clock's `virtual_ms`; after triggering delayed work (orders, message lifecycle, retries) `await
  state.tasks.drain()` — the accelerated clock advances on its own. Verify signatures with
  `telnyx_helpers.verify_signature` (pynacl, independent of the sim). All sim ids come from `state.ids`
  (seeded); background work goes through `state.tasks.spawn` (never bare `create_task`). Python 3.12
  `asyncio.gather` over already-done tasks does not yield — loops over it need an `await asyncio.sleep(0)`.
- 2026-10-29 (P03-T02b): FakeTelnyx call control: `va_sim.telnyx.call_engine.CallEngine` (on `SimState.call_engine`)
  is the only code that mutates calls/conferences (`calls.Call`, per account in `Account.calls`/`conferences`);
  handlers in `call_routes`/`call_actions` (dispatch table of `Action(model, needs, run, check)`), `conferences`,
  and `call_control` validate and call one engine method. Webhooks go through `CallEvents.send` (chained per call
  session and per conference, so order is deterministic); timers through `CallEvents.later` (cancelled on hang-up).
  Hooks for the P03-T03 media client: `Call.audio` (PCM16 8 kHz caller/agent tracks, recorded into WAVs),
  `Call.stream` (stream params), `Call.timeline_utterances`. The accelerated clock now advances only when the event
  loop is idle, so tests drive calls with `telnyx_helpers.at(state, seconds)` (absolute virtual time; it also waits
  for queued webhooks) and `settle(state)`; `state.tasks.drain()` runs every call to its end (time limit
  14 400 s virtual — instant). A test that busy-polls with `asyncio.sleep(0)` keeps the loop non-idle and freezes
  accelerated time. Builders: `dial`, `command`, `ok`, `summary`, `events`, `payloads`, `register`,
  `inbound_call`, `answered_call`, `ringing_call`, `ringing_inbound` in `tests/support/telnyx_helpers.py`.
- 2026-10-29 (P03-T03a): `va_sim.audio` is the synthetic caller audio (specs/30 §3.1 Decision): `synthesize_utterance`
  (μ-law 8 kHz), `synthesize_pcm16`/`synthesize_wire(..., sample_rate=8000|16000)` (16 kHz synthesized natively,
  wire = L16 BE), `decode_marker(pcm16, sample_rate)`, `goertzel_power`, `synthesize_dtmf`, plus the sim's own G.711
  codec (`va_sim.audio.codec`, must stay bit-identical with `va_voice.audio.codec`; never import `va_voice` from the sim).
  Seed synthesis with `SimState.utterance_seed(id)`; utterance ids are 16-bit (registry caps at 65 535). Randomness
  only via `va_sim.audio.AudioRng` (raw PCG64). Synthesis is CPU-bound: call it through `asyncio.to_thread` from
  async code. `va_sim.audio` must not import `va_sim.state` (the state imports `MAX_UTTERANCE_ID`). The Silero
  check of §3.1 lives in P04-T03.
- 2026-10-29 (P03-T03b): FakeTelnyx media WS client = `va_sim.telnyx.media_session.MediaSession` (one per stream,
  `StreamState.session`; spec: specs/30 §3.2 Decision P03-T03b). API the caller runner reuses: `queue_caller(wire,
  (utterance_id, text))`, `queue_dtmf(digits)`, `inject(frame)`, `connected`, `fmt` (stream `MediaFormat`). Only the
  tick loop writes to the socket. Any test that gives a stream a `stream_url` must point it at the `media_agent`
  fixture (`tests/telnyx/conftest.py`, `tests/support/media_agent.py`: `wait_for(predicate)`, `wait_media(n)`,
  `send`, `send_raw`, `close`, `reject=True`), otherwise the sim connects for real and adds `streaming.failed`.
  Accelerated clock + sockets: while a stream runs the tick loop always sleeps 20 ms ahead, so virtual time races
  whenever the loop waits on socket I/O — assert orders and counts, not virtual ms of socket-driven events; never
  `state.tasks.drain()` with a live stream (stop it or hang up first). Unsupported stream codecs (PCMA, G722, OPUS,
  AMR-WB, other rates, `mp3` mode) are 422 `10015` on dial/answer/streaming_start.
- 2026-10-29 (P03-T03c): scenario caller runner = `va_sim.telnyx.caller_script.CallerScript` (steps in
  `caller_steps.py`, spec: specs/30 §3.5 Decision P03-T03c); `Call.script` / summary+timeline `script`. It starts
  from `CallMedia.media_connected` (called by `MediaSession` after `start`) and listens to the session
  (`add_listener`: `on_tick(AgentTick)`, `on_media_end`); listeners must stay O(1). Waits race the session ticks
  against a sim-clock deadline; offsets from `queue_caller`/`queue_dtmf` vs `caller_sent`/`dtmf_sent` tell when
  audio has gone out. CPU work that must not cost virtual time runs inside `va_sim.tasks.hold_clock(clock, tasks,
  name=...)` + `asyncio.to_thread`. Tests: `tests/telnyx/test_caller_script.py` (`until(state, predicate)` sleeps
  on the sim clock; assert step times relative to played agent frames, never socket arrival times). Two
  `telnyx_client(...)` blocks streaming to media agents in ONE test hang (pre-existing); use separate tests.
- 2026-10-29 (P03-T04): FakeElevenLabs TTS = `va_sim.elevenlabs` (spec: specs/30 §3.3 Decision P03-T04):
  `tts_ws.TtsSocket` (multi-context WS, one worker task per context in a per-socket TaskGroup, one send lock),
  `tts_rest` (convert/stream/with-timestamps), pure `tts_audio`/`mp3`/`tts_context`, `store.ElevenLabsStore` on
  `SimState.elevenlabs` (records, allow-list keys, armed fault, live sockets closed 1012 by reset). Control:
  `/sim/elevenlabs/tts/requests`, `/sim/elevenlabs/tts/fault`, `/sim/elevenlabs/api-keys`. Tests
  (`tests/elevenlabs/`, helpers `tests/support/tts_helpers.py`: `served`, `tts_socket`, `recv_spoken`,
  `recv_until_final`, `wait_closed`, `rest_client`): WS protocol tests run on `realtime_state()` (wall clock,
  ttfa 0) because audio is sent with no pacing — a `close_context` sent right after a flush stops it before the
  first chunk, so read the audio first (`recv_spoken`) or hold the stream with a `delay` fault. Only inactivity
  tests use an accelerated clock (it fires as soon as the loop idles). Starlette `WebSocketRoute` needs a plain
  `async def` endpoint (a `functools.partial` is treated as an ASGI app).
- 2026-10-29 (P03-T05a): FakeElevenLabs realtime STT (spec: specs/30 §3.3 Decision P03-T05a): `stt_ws.SttSocket`
  (reader + committer tasks, one send lock), pure `stt_stream.SttStream` (20 ms frames, segments, marker decode,
  partial cadence, vad) and `stt_text` (word timings, regex entities), `stt_store.SttStore` on
  `ElevenLabsStore.stt` (sessions, transcript script deque, fault, sockets). `ws_io` has the JSON receive / quiet
  close helpers both sockets use. Control: `/sim/elevenlabs/stt/sessions`, `/stt/fault`, `/stt/transcripts`.
  Tests: `tests/elevenlabs/test_stt_*.py`, helpers `tests/support/stt_helpers.py` (`stt_socket`, `caller_audio`
  registers + synthesizes an utterance in any audio format, `chunked`, `send_audio`, `recv_type`). A segment
  resolves only after 100 ms voiced audio AND 260 ms of marker window, so commit-mid-utterance tests must commit
  at least ~300 ms into the utterance; markerless test speech = loud μ-law codes (`b"\x10\x90" * n`).
- 2026-10-29 (P03-T05b): FakeElevenLabs batch STT `POST /v1/speech-to-text` (spec: specs/30 §3.3 Decision
  P03-T05b): `stt_batch` (route: auth → batch fault → multipart form → source → thread analysis → record),
  `stt_batch_params` (pydantic `BatchForm` over the form fields, `parse_enable_logging`), pure `stt_batch_audio`
  (WAV/raw PCM reading, `voiced_segments`, caller marker path, `transcribe`), pure `stt_batch_tones` (TTS tone
  decoding, matching against `tts_requests[*].generated_text`), `stt_batch_json` (single/separate/combined bodies).
  `rest_io` holds the ElevenLabs 422 helpers shared with REST TTS. Starlette form parsing needs `python-multipart`
  (a va-sim dependency). Tests: `tests/elevenlabs/test_stt_batch*.py`, helpers `tests/support/batch_helpers.py`
  (`agent_track`, `caller_track`, `place`, `wav_file`, `multipart`/`post_batch`, `served_app` for a real HTTP
  source). httpx sends `data=` alone as urlencoded: send URL-only batch requests as `files=[(name, (None, v))]`.
  REST TTS calls cost `tts_ttfa_ms` of sim time, so a test that synthesizes before dialing a FakeTelnyx call sees
  the answer later than 1 s; extend `call.audio` only after the answer or the recording offset skips it.
- 2026-10-31 (P03-T05c): FakeElevenLabs voices (spec: specs/30 §3.3 Decision P03-T05c): `voice_catalog` (11 premade,
  `KAREN_VOICE_ID`), `voice_store.VoiceStore` on `ElevenLabsStore.voices` (reseeded by reset), pure `voice_samples`,
  `voice_params`, `voice_json`, `voice_rest`, `voice_control`. TTS (WS and REST) now rejects unknown voices with
  `voice_not_found`: tests must use a seeded id (`tts_helpers.VOICE_ID` = `KAREN_VOICE_ID`). Control:
  `/sim/elevenlabs/voices`, `/voices/requests`, `/voices/fault` (same body as the batch fault),
  `/sim/elevenlabs/subscription` (GET/PATCH), `/previews/{id}.mp3`. Shared REST helpers in `rest_io`:
  `parse_json_body(raw, Model)` and `multipart_form(request, max_files=)` (raises `InvalidBodyError` with a 422
  `["body"]` response); fault routes via `va_sim.control.fault_route`; `BatchFault.consume()`; `SimIds.alnum(n)`.
  Tests: `tests/support/voice_helpers.py` (`wav_sample`, `mp3_sample`, `id3_tag`, `add_voice(client, name|None,
  files=..., path=..., **fields)`: text fields go as name-less parts so the body is multipart even with no files).
- 2026-10-31 (P03-T06a): FakeLLM = `va_sim.llm` (spec: specs/30 §3.4 Decision P03-T06a): pure `params` (OpenAI
  request model, `parse_chat` → 400 with `param` path), `rules` (`RuleBook`, `load_rules` raising `RulesError`, a
  `ScenarioError` subclass), `reply` (tokens, default reply, truncation, usage), `wire` (chunk/completion builders,
  `Emission.paced`); `chat` (endpoint, faults, pacing), `store.LlmStore` on `SimState.llm`, `control`. Shared
  control helpers now live in `va_sim.control`: `fault_route` and `yaml_or_json_body(request, Model, load_yaml,
  what=...)`; `va_sim.scenario.compose_document(text, source=, what=)` parses any YAML mapping document with
  positioned issues. `build_state` loads `SIM_LLM_RULES` files and the model ids (`SimSettings.llm_model_ids()`,
  shared `LLM_*_MODEL`/`EMBEDDINGS_MODEL` variables). Tests: `tests/support/llm_helpers.py` (`llm_state` accelerated
  for virtual-ms pacing asserts, `realtime_llm_state` + `served_llm` for sockets/SDK, `llm_client` in-process,
  `chat_body`, `sse_events`, `deltas`, `add_rules`, `wait_held(state, n)` for `timeout` faults — never poll). A
  ``timeout`` hold races the store's release event against `http.disconnect` from `request.receive`; the
  `disconnect` fault raises `SimulatedDisconnect` inside the response body (uvicorn logs it and drops the socket;
  httpx sees `RemoteProtocolError`). The `openai` SDK (root dev dependency) is the acceptance client
  (`AsyncOpenAI(base_url=f"{url}/v1", api_key="sim-llm-key", max_retries=0)`). `ruff check --select X --fix` with
  `RUF100` in the selection strips every `noqa` for unselected rules — never combine them.
- 2026-10-31 (P03-T06b): FakeLLM forced tool calls without a rule use `va_sim.llm.heuristics.structured_arguments`
  (in-call classifiers there, post-call ones in `heuristics_post`, shared `heuristics_base.Heuristic`, text helpers
  in `llm.text`); every result goes through `schema_fill.SchemaFill.conform`, so it validates against the tool's
  `parameters` (tests cross-check with `jsonschema.Draft202012Validator`, a root dev dependency). Spec decision:
  specs/30 §3.4 Decision P03-T06b. `STRUCTURED_TOOL_NAMES` is the sim's own copy of specs/10 §3 (never import
  `va_llm` from the sim). `POST /v1/embeddings` lives in `llm/embeddings.py` (pure) + `llm/embed_route.py`; shared
  auth/model/fault answers are in `llm/gate.py`. The `openai` SDK requests `encoding_format=base64` by default.
  Faults take `endpoint: chat|embeddings` (default chat). Representative prompts/schemas for all 14 names:
  `tests/support/llm_schemas.py` (`SPEC_CALLS`, `forced_body`).
- 2026-11-01 (P03-T07a): FakeStripe = `va_sim.stripe` (spec: specs/30 §4.1 Decision P03-T07a): pure `form` (bracket
  decoding), `params` (typed readers raising `StripeError`, `check_known`, `MetadataPatch`), `errors`, `objects`
  (JSON builders, `TEST_CARDS` magic ids → `CardSpec`), `signing`; `http.stripe_endpoint(state, handler)` is the
  pipeline (auth → form → idempotency → handler returning the 200 body or raising `StripeError`); handlers in
  `customers`, `payment_methods`, `payment_intents`, `portal`, `events` (`emit_event` logs + delivers); table of
  routes `routes._API` (catch-all 404 last); `store.StripeStore` on `SimState.stripe`, emitter
  `SimState.stripe_webhooks`. Webhook post/retry/log is shared by FakeTelnyx and FakeStripe in `va_sim.delivery`
  (`SignedDelivery`, render callback per attempt). Tests: `tests/support/stripe_helpers.py` (`stripe_state(hooks)`
  with a `RecordingTransport`, `stripe_client`, independent `encode`/`signature_ok`, `events_of`,
  `realtime_stripe_state` + `served_stripe` for the SDK). stripe-python 16.1 (`StripeClient(key,
  base_addresses={"api": url}, http_client=stripe.HTTPXClient(), max_network_retries=0)`, async methods
  `*_async(params, options)`) posts with httpx `data=<bytes>`, a DeprecationWarning (error here): SDK tests ignore
  exactly that message with a module `pytestmark` filter, and must `await http_client.close_async()` (untyped:
  `# type: ignore[no-untyped-call]`) or the socket leaks into a later test.
- 2026-11-01 (P03-T07b): FakeStripe billing (spec: specs/30 §4.1 Decision P03-T07b): `catalog` (products/prices;
  `parse_price` → `PriceDraft`, `store_price` only after the whole request validated), `checkout` (sessions,
  line items, expire), `checkout_complete` + `checkout_page` (hosted page `GET /sim/stripe/checkout/{id}?outcome=
  success|cancel[&card=pm_card_…]`), `subscriptions` (`start_subscription(state, PlannedSubscription, call=)` shared
  with checkout), `subscription_update` (prorations), `invoicing` (lines as plain dicts, `open_invoice` applies the
  customer balance, `apply_attempt` + `announce`), `invoices`, `renewal` (control `POST /sim/stripe/subscriptions/
  {id}/renew {"fail"}`), pure `periods`/`billing_objects`, and `charging` (`decide`, `ChargeDecision.error()`,
  `new_id`; payment intents use it too). DELETE handlers read query + form params (the SDK sends DELETE params as a
  query). Tests: `stripe_helpers` `ok`, `monthly_price`, `checkout`, `visit`, `renew`, `subscribed_customer`,
  `event_types`; proration tests move time with `state.clock.advance(s)` on the accelerated clock.
- 2026-11-01 (P03-T08a): `packages/va_telnyx` (dist `va-telnyx`) is the only code that builds Telnyx URLs:
  `TelnyxClient(api_key, base_url, http, limiter, *, account, metrics, clock, rng, retry, tracer_provider)` wraps
  `transport.TelnyxTransport.request(Operation(name, method, category, idempotent, call_command), path, json=...)`
  (rate-limit token → attempts → error mapping; absolute paths go out without the auth header). Retry/limit/mapping
  rules: specs/06 §2.2 Decision P03-T08a; metrics/spans: specs/27 §3.6. Request models use
  `Field(serialization_alias="from")` (plain `alias=` makes mypy, which runs without the pydantic plugin, reject
  `from_=`); bodies come from `model.body()` (None omitted, `StreamParams` flattened). `va_telnyx` must never
  import `va_sim`; its tests may. Test helpers: `tests/support/telnyx_client_helpers.py` (`rig(http)` = client with
  fakeredis limiter, separate `RecordingClock`s for retries/limiter waits, `MidJitter`; `sim_clients(state)` =
  raw FakeTelnyx client + `TelnyxClient` on one app; `leg_state(leg)`), the `telnyx_api` respx fixture, and a
  `media_agent` fixture in `tests/conftest.py`. `RecordingClock` now lives in `packages/va_common/tests/clock_helpers.py`.
  `BucketDecision.retry_after_ms` gives the exact wait for in-process waiters. A new `### 3.N` heading in specs/27
  is skipped by `test_metrics.parse_spec_catalog` (it only reads §3.1-§3.5).
- 2026-11-01 (P03-T08b): `TelnyxClient` = call commands in `client.py` + mixins `provisioning.ProvisioningCommands`,
  `numbers.NumberCommands`, `messaging.MessagingCommands` (each declares `_transport: TelnyxTransport`). Shared
  transport pieces: `request_pages(op, path, Model, params=)` (all pages, one metric observation),
  `download(op, url, path, max_bytes=)` (streamed via `anyio.open_file`, file removed on failure), `parse_items`,
  `segment(id)`. Rules: specs/06 §2.2 Decision P03-T08b. `TelnyxClientFactory(..., resolve_key=async fn)` caches BYO
  clients 5 min on the injected clock's `monotonic()`. Tests: `telnyx_client_helpers.sim_http(state)` yields
  (raw sim client, bare client on the same ASGI app) for factories/BYO clients; a BYO key is registered with
  `POST /sim/telnyx/accounts`. respx streaming bodies: `httpx.Response(200, stream=<AsyncByteStream>)` (raise
  `httpx.ReadError` inside `__aiter__` for a mid-body failure). Response models use `coerce_numbers_to_str`
  (Telnyx numeric ids) and `validation_alias="from"` + `populate_by_name` for the `from` field.
- 2026-11-01 (P03-T09): Telnyx inbound verification/parsing lives in `va_telnyx.signatures` (`verify_webhook`,
  `verify_media_ws`, `load_public_key`; `WebhookVerificationError.reason`), `va_telnyx.webhooks`
  (`parse_envelope`, `parse_payload`, `PAYLOAD_MODELS`, `UnknownEventPayload`), `voice_events` (one model per
  specs/06 §5 row; `test_webhook_models` parses the §5 table and requires a model for every row, so a new row needs
  a model), `message_events`, and `media` (`parse_media_message` fast path, `media_message`/`mark_message`/
  `clear_message`, `segment_mark`). Rules: specs/06 §4.1 Decision P03-T09. Contract tests verify every FakeTelnyx
  webhook with `state.signing_key.public_key_b64` and `now=state.clock.now()`; `number_order.complete` is not a
  modelled event (clear `transport.received` after ordering numbers). The fixed signature vector in
  `test_signatures.py` uses the seed `bytes(range(32))`. Provision app + messaging profile in va_telnyx tests with
  `telnyx_client_helpers.provisioned(client, voice_url, sms_url)`.
- 2026-11-01 (P03-T10): `packages/va_elevenlabs` (dist `va-elevenlabs`) holds the TTS socket
  `va_elevenlabs.tts_ws.TtsSocket(TtsConfig(ws_base, api_key, ...), clock=, metrics=CatalogTtsMetrics(...),
  tracer_provider=)` (rules: specs/09 §11 Decision P03-T10). `flush(id)` ends an utterance (`close_context` follows
  after the tail grace); `send(..., flush=True)` only forces generation. Client state changes before every write,
  so a write to a dead socket is repaired by the reconnect replay. `va_tts_late_audio_dropped_total` is in the
  media-worker catalog (`test_metrics` counts 58 spec rows). Never import `va_sim` from `va_elevenlabs` src.
  Tests: `tests/support/tts_client_helpers.py` (`RecordingTtsServer` with `reject_status`, `auto_final`,
  `auto_close`; `Connection.next/take/send/close`; `audio_frame`, `final_frame`, `opened`, `collect`, `rig(url)`
  = socket on a `ManualClock` + media-worker registry + memory spans). `ManualClock`
  (`packages/va_common/tests/clock_helpers.py`) parks sleepers until `run_for(s)`; before advancing past a delay
  that a task starts after real socket I/O, `await clock.wait_for_sleep(s)` (event-driven, no polling). Prove
  "nothing was sent before X" by issuing a marker message and asserting it is the next one received. Contract
  tests run the socket on the wall clock against `tts_helpers.served(realtime_state())`.
- 2026-11-01 (P03-T11): realtime STT client = `va_elevenlabs.stt_realtime.ElevenLabsRealtimeStt(SttConnection(...),
  clock=, metrics=CatalogSttMetrics(...), tracer_provider=)` (rules: specs/08 §7 Decision P03-T11); pure parts in
  `stt_wire`, `stt_errors` (`ACTIONS` = §4 error rows), `stt_buffer`, `stt_commits`, plus `languages` (§5) and
  `keyterms` (§6). Shared socket-client plumbing: `ws_tasks` (`run_timer`, `nap`, `crash_of`, `stop_tasks`) and
  `ws_metrics.VendorCounters`. Test support: `ws_server_helpers.RecordingWsServer` (override `on_connect` /
  `on_message`; `RecordingTtsServer` and `stt_client_helpers.RecordingSttServer` subclass it; the STT one greets with
  `session_started` unless `greetings` holds a frame or `None`), `rig_helpers.Observed` (ManualClock + registry +
  spans; `Rig`/`SttRig` extend it), `contract_helpers.SimClient` + `sim_served()` for FakeElevenLabs contract tests.
  mypy narrows properties across asserts (`assert x.p is None` makes a later `assert x.p == "a"` unreachable):
  compare tuples returned by a helper function instead. Contract tests that need "no audio lost" must keep the
  whole utterance within the 2 s replay cap (the client sends unpaced audio before it sees the fault).
- 2026-11-01 (P03-T12): ElevenLabs REST = `va_elevenlabs.ElevenLabsRest(api_key, base_url, http, *, clock, ...)`
  (rules: specs/15 §7 Decision P03-T12). Mixins `tts_rest`/`voices_rest`/`stt_batch` call
  `ElevenLabsTransport.call(op, path, parse, ...)`: parse the body INSIDE the call so a malformed body counts as
  the operation's failure; multi-request operations (paged `list_voices`) use `transport.observed(op, work)` with
  `Exchange.send` (one metric/span, attempts summed). Retry schedules are per `RestOperation` (`rest_retry`);
  add/edit are never retried. Multipart parts are httpx `files=` tuples (`(None, text)` fields) so URL-only batch
  requests stay multipart. `retry_after_seconds` now lives in `va_common.http` (`va_telnyx.retry` re-exports).
  Tests: `tests/support/rest_client_helpers.py` (`rest_rig()` = client on `BASE_URL` for respx with a
  `RecordingClock`; `sim_rest()` = client over FakeElevenLabs via ASGI; `form_parts(request)` decodes multipart
  with the stdlib `email` parser). The sim batch record list is `GET /sim/elevenlabs/stt/batch` key `requests`.
- 2026-11-01 (P03-T13a): `packages/va_llm` (dist `va-llm`) = the LLM gateway core (rules: specs/10 §1 Decision
  P03-T13a, metering §11 Decision, metrics specs/27 §3.6 Decision). `LlmGateway(ProviderRegistry(platform,
  OrgProviderStore|None), http, clock=, metrics=LlmMetrics(registry, catalog), recorder=, costs=CostTable(...))`;
  `complete(call_type, org_id=, messages=[ChatMessage], ...)` raises only `LlmUnavailableError` (`.reason`,
  `.last`). openai ≥ 3 is typed against `httpx2` but accepts our httpx clients at runtime: build SDK clients only
  with `va_llm.clients.openai_client` and pass timeouts through `sdk_timeout` (mypy rejects httpx objects
  otherwise; test code constructing openai exceptions passes httpx objects typed `Any`). respx records a call
  only when it completes: for hanging requests assert the `Hanging` side effect's `requests`/`cancelled`, and mix
  hangs with responses through `llm_client_helpers.scripted(...)` (a respx list side effect is typed
  `Response | Exception` only). Tests: `gateway_rig()` (ManualClock: `wait_for_sleep(s)` then `run_for(s)` for
  deadlines and retry waits), `sim_gateway(state)` → `(rig, control)` over FakeLLM ASGI, the `llm_api` respx
  fixture; `test_org_providers_db.py` boots its own module Postgres with `open_identity_db`. An `LlmProvider`
  needs a non-empty key (the SDK refuses an empty one at construction). Org-provider lookup failures surface as
  `OrgProviderError` (`.cause` = exception type name); `complete` turns them into the platform fallback
  (dialogue/analysis) or `LlmUnavailableError("provider_unavailable")`. In va_llm tests read logs with
  `service_helpers.json_log_stream`/`log_events` (structlog `capture_logs` misses events once another test has
  configured logging).
- 2026-11-01 (P03-T13b): streaming dialogue = `va_llm.dialogue` (`DialogueRequest`, `DialogueStream`; rules: specs/10
  §2 Decision P03-T13b), SSE parsing in `stream_wire.StreamAssembler`, shared field readers in `wire_fields`, shared
  `Target`/`record_failure`/`platform_fallback` in `targets`, metering via `metering.Meter`. httpx's
  `ASGITransport` buffers the whole response body, so streaming tests against FakeLLM need a real socket:
  `llm_client_helpers.served_gateway(state)` (ManualClock gateway + uvicorn FakeLLM). respx streams: `streamed(*parts)`
  over `StreamingBody` (bytes, an exception to raise, or an `asyncio.Event` to hang on; records `closed`/
  `cancelled`), chunks via `chunk`/`content_stream`/`sse`. Every line read registers a deadline sleep: before the
  first token `wait_for_sleep(TTFT_S)`, after it `wait_for_sleep(TOTAL_S - elapsed)`; `await _settle()` (a few
  `sleep(0)`) before cancelling when the body must already be waiting. Always close a `DialogueStream`
  (`async with await gateway.stream_dialogue(...) as stream`).
- 2026-11-01 (P03-T14a): structured outputs = `LlmGateway.call_structured(call_type, Schema, org_id=, messages=,
  function_name=, description=)` (rules: specs/10 §3 Decision P03-T14a); pure parts in `va_llm.structured`
  (`TOOL_NAMES`, `register_structured_function`, `tool_schema`, `validate_arguments`, `first_json_object`), the run in
  `structured_calls`. `function_name` must be a string literal from `TOOL_NAMES` at every call site
  (`test_structured_drift.py` AST-scans `apps/*/src` and `packages/*/src`); a new name goes into specs/10 §3,
  `TOOL_NAMES`, and FakeLLM's `STRUCTURED_TOOL_NAMES` together. The registry is process-global: va_llm tests reset it
  with the autouse `fresh_structured_registry` fixture. Pydantic puts a model's docstring into the schema
  `description`, which is sent to the model, so write schema docstrings as instructions (or none).
- 2026-11-01 (P03-T14b): embeddings = `va_llm.Embeddings(provider, http, *, redis, clock, metrics, model, dimensions)`
  (rules: specs/10 §10 Decision P03-T14b), separate from `LlmGateway` (no policy/breaker/org providers); pure parsing,
  normalisation, truncation and the `emb:q:{<sha256>}` key in `embedding_wire`, the fail-open Redis query cache in
  `embedding_cache.QueryCache`. Usage goes to an `EmbeddingUsageRecorder` (`EmbeddingUsageRecord`, purpose
  `ingest|query`). Tests: `llm_client_helpers.embeddings_rig()` (ManualClock + fakeredis + catalog metrics) via the
  `erig` fixture, `embedding_body(n, dims)`, `EMBEDDINGS` respx URL, `sim_embeddings(state)` for FakeLLM contracts
  (`GET /sim/llm/embeddings` is the sim's request log). Shared-server fakeredis: two `FakeAsyncRedis(server=s)`.
  The openai SDK resolves the platform in a worker thread before each new client's first request; under xdist +
  coverage that wall-clock hop outlasted ManualClock deadlines (hanging-first-request tests flaked/hung 60 s), so the
  va_llm conftest autouse `inline_sdk_platform_lookup` runs it inline. `Hanging.wait_started(n)` waits for arrivals.
- 2026-11-01 (P03-T15a): Telnyx account bootstrap = `va_telnyx.bootstrap.bootstrap_account(client, name=, webhooks=)`
  (names `platform_resource_name`/`org_resource_name`, URLs `webhook_urls(host, account_id=None)`; rules: specs/06 §12
  Decision P03-T15a); the script `scripts/telnyx_bootstrap.py` only wires settings/HTTP/Redis and `update_env_file`.
  Settings subclasses declare `service_name: ClassVar[str]` (a plain field shadows the base ClassVar and pydantic
  warns, an error here). Scripts that call `asyncio.run` are tested end to end with `await asyncio.to_thread(main,
  argv)` while `running_sims(state, ports={"telnyx": 0})` serves FakeTelnyx in the test loop.
- 2026-11-01 (P03-T15b): `infra/docker/python.Dockerfile` (specs/29 §2 Decision P03-T15b) builds every Python app
  image: SERVICE = app directory, the distribution comes from `apps/$SERVICE/pyproject.toml`, uv is pinned to the
  local `0.13.6` (lock revision 5). `scripts/tests/test_dockerfile.py` compares it line for line with the §1.1 block
  plus the documented deviations, so change the spec and the file together. `.dockerignore` limits the context;
  never exclude `uv.lock`, `packages/`, `apps/`, `models/`, `assets/`. The compose `sim` service runs in the infra
  profile; `make up` builds it (`up --wait --build`) and then `scripts/sim_public_key.py` rewrites
  `TELNYX_PUBLIC_KEY` in `.env` and `.sim/telnyx_public_key` (new key on every sim start). The e2e profile adds
  `docker-compose.e2e.yml` (`SIM_CLOCK: realtime` since P05-T17a). A host-run sim on 9100-9400 now collides with the container:
  stop it (`docker compose stop sim`) or run the host sim with `--port 19100`. `env_file.update_env_file` is the
  shared dotenv updater for scripts.
- 2026-11-01 (P03-T15c): with `SIM_STATE_REDIS_URL` the sims share scenarios, latencies, reset + seed, and utterance
  ids through `va_sim.shared.SharedState` (keys `sim:state:*`, Lua writes, channel `sim:state:events`; rules: specs/30
  §3 Decision P03-T15c). Control routes and other shared writers use the async `SimState` methods (`reset_all`,
  `add_scenario`, `set_latencies`, `set_profile`, `allocate_utterance`; `SharedStateUnavailable` → 503
  `state_unavailable`) and readers call `await state.sync()` first; the sync `register_scenario`/`register_utterance`
  stay process-local. STT resolves markers with `state.resolve_utterance(id)` (worker thread only for the Redis
  fallback). Tests: two `SimState`s on `FakeAsyncRedis(server=FakeServer())` = two processes
  (`tests/test_shared_state.py`), `state.wait_applied(version)` waits for the subscriber, `server.connected = False`
  simulates a Redis outage. Two sims by hand: `SIM_STATE_REDIS_URL=redis://localhost:6379/15 uv run python -m
  va_sim_server.main --port 19100` and the same with `--port 19500`. `SIM_WORKERS` other than 1 exits 2.
- 2026-11-01 (P03-T16): live smoke = `tests_live/` (specs/30 §7 Decision P03-T16): `test_vendor_clients.py` (module
  `pytestmark` = `live` + skipif `LIVE_TESTS != "1"`; fixtures in `tests_live/conftest.py` skip per missing key via
  `live_env.require`), helpers in `tests_live/support/` (`live_env`, `live_clients` endpoints/model defaults,
  `live_wer`, `live_fixture` cached TTS-generated STT WAV + sha256 JSON). `make test` collects it (7 skipped);
  `test_live_gate.py` runs a child pytest with `PYTEST_*`/`COV_CORE_*`/`LIVE_*` stripped from the env. Adding a live
  test changes `LIVE_TEST_COUNT` there. `tests_live/**` has the same ruff ignores as `**/tests/**`.
- 2026-11-01 (P03-T17): event bus core = `va_events` (rules: specs/19 §1 Decision P03-T17): `catalog.EventType` (every §2
  type; `test_catalog.py` parses the §2 table, so a new row needs a member and a `_CATEGORIES` entry), `stream_for`,
  `keys` (`call:{<id>}:seq|live`, `events:<stream>`), `model.DomainEvent`/`new_event(type, org_id=, clock=, **fields)`/
  `encode`/`decode`, `publisher.EventPublisher(redis, call_events=sink|None, router=None).publish(...)` (raises
  `EventPublishError`), `call_events` (`row_for`, `DbCallEventWriter`, `DirectCallEventWriter`), `buffer.CallEventBuffer`
  (run `run()` under a supervisor, `flush_call` at call end, `aclose` on shutdown), `consumer.StreamConsumer`.
  va_events depends on va_db now. Tests: `packages/va_events/tests/support/event_helpers.py` (`event`, `row`,
  `RecordingWriter.fail_next`, `RecordingRouter`, `publisher_rig(buffer=)` on fakeredis with `max_connections=256` for
  200 concurrent publishes; fakeredis supports XAUTOCLAIM/pubsub). DB tests must use real time (`SystemClock`):
  `call_events` partitions exist only around the migration month, and `FrozenClock` starts at 2026-01-01.
  `CallEventWriter`s must be idempotent (`DbCallEventWriter` inserts `ON CONFLICT DO NOTHING`) and report a partial
  failure as `CallEventWriteError(unwritten, failed_orgs=)`; the buffer re-queues only `unwritten`. Rows dated
  outside every partition make an org's insert fail (handy for partial-failure DB tests).
- 2026-11-01 (P04-T01): `packages/va_voice` (dist `va-voice`) exists; audio primitives are `va_voice.audio` (codec,
  formats, framing, resample, levels; see the specs/07 §4 Decision). soxr has no `py.typed`: a commented mypy override
  ignores it and only `va_voice.audio.resample` calls it. soxr LQ streaming emits nothing for the first few 20 ms
  chunks (filter delay) and outputs in blocks: assert totals after `Resampler.flush()`, never per-chunk sizes; chunked
  vs one-shot output differs by ≤ 2 LSB. dBFS everywhere = RMS / 32 767 (`va_voice.audio.rms_dbfs`).
- 2026-11-01 (P04-T02): inbound reordering = `va_voice.reorder.ReorderBuffer(fmt, window=5, first_chunk=1, max_gap=50,
  observer=)` (specs/07 §5.1 Decision): `push(int(media.chunk), payload) -> list[OrderedFrame(chunk, payload, filled)]`,
  `drain()` at stream stop; count-based only (no timer), Telnyx chunks start at 1. Late/repeated chunks are dropped
  (`stats.duplicates`, no metric); holes wider than `max_gap` resync without silence. Metrics through
  `va_voice.reorder_metrics.CatalogReorderMetrics(build_metrics(Service.MEDIA_WORKER, ...))`
  (`va_audio_reorder_total`, `va_audio_gap_frames_total`). va_voice must stay transport-agnostic (never import
  `va_telnyx`).
- 2026-11-01 (P04-T03): Silero VAD = `va_voice.vad_model.SileroModel.load(models_dir)` (manifest-verified, one ORT
  session per process; onnxruntime has no `py.typed`, mypy override) + `va_voice.vad.SileroVad(model,
  VadExecutor(settings.media_vad_threads))` per leg (`feed(pcm16k) -> list[VadEvent]`, `await reset()`); spec:
  specs/07 §5.3 Decision. Real-model tests need `models/silero_vad.onnx` (`make setup`; CI runs
  `scripts/fetch_models.sh` before `make check`). Fixture WAVs in `packages/va_voice/tests/fixtures/vad/` are
  generated and must stay byte-identical: `uv run python packages/va_voice/tests/support/vad_fixtures.py --write`
  (truth `SPEECH_SEGMENTS_MS` lives there; the support dir is on pytest `pythonpath` and mypy `mypy_path`). Unit
  tests drive `SileroVad` with any object that has `infer(samples, state) -> (p, state)` (`ScriptedModel` in
  `tests/test_vad.py`). The media worker's `vad_model` delegates to va_voice (va-media-worker depends on va-voice).
- 2026-11-01 (P04-T04): endpointing = `va_voice.endpointing.Endpointer` (pure, sync; spec: specs/07 §6 Decision
  P04-T04): feed `on_vad(VadEvent)` / `on_partial(text, at_ms)` on one ms timeline, re-read `deadline_ms()` after every
  input, `poll(now_ms)` → `EndOfTurn | TurnDiscarded | None`. No clock inside: tests drive time with explicit ms
  values. `va_voice.text_norm` (`tokens`, `normalized`, `ends_sentence`, `ends_with_digit`) is the shared transcript
  normalizer (P04-T05 backchannels reuse it).
- 2026-11-01 (P04-T05): interruptibility = `va_voice.interrupt` (pure; spec: specs/07 §7 Decision P04-T05):
  `InterruptPolicy().should_interrupt(speech_ms, words, text, level, agent_speaking)` → truthy `InterruptDecision`
  (`reason`), `should_interrupt_dtmf(level, agent_speaking)`, `effective_level(call_level=, node_level=,
  block_interruptions=, interruptible=)`, `BackchannelClassifier.classify(text | words)` (greedy longest phrase on
  `text_norm.tokens`). `BACKCHANNELS` is checked verbatim against the specs/07 §7 paragraph by `test_interrupt.py`;
  thresholds live in `LEVEL_RULES`. The caller measures `speech_ms` of the current continuous VAD segment.
- 2026-11-01 (P04-T06): agent-speech text utilities live in the `va_voice.text` subpackage (`__init__` imports
  nothing; P04-T07 pronunciation and the alignment timeline go there too). `va_voice.text.chunker.ClauseChunker`
  (`feed(delta)`/`flush()`/`chunk_stream(deltas)`; rules: specs/09 §5 Decision P04-T06) returns clauses with exactly
  one trailing space, and `flush()` resets the first-clause minimums. Markdown normalization is
  `va_voice.text.markdown.normalize_text` (reference) + `MarkdownStripper` (streaming twin, `space_pending` tells the
  chunker that whitespace follows the released text); hypothesis tests require identical output for every split of
  the input, so any rule change must keep both in step.
- 2026-11-01 (P04-T07): pronunciation guide = `va_voice.text.pronunciation.PronunciationApplier(rules)` (rules: specs/09
  §6 Decision P04-T07): any object with `word`/`pronunciation`/`case_sensitive`/`spaced` works (`PronunciationRuleLike`
  protocol; in-package `PronunciationRule` dataclass). `apply(clause) -> PronouncedText(text, original, offsets)`;
  `offsets.to_original(i)` (floor: inside a replaced span → span start) / `to_original_ceil(i)` map sent positions to
  the original text for the alignment timeline and barge-in truncation; `replace(text)` for static phrases. Tests
  compare against a naive reference scan in `tests/text/test_pronunciation.py`; note `C++C` is not a `C++` match
  (`(?!\w)` sees the `C`).
- 2026-11-01 (P04-T08): playback = `va_voice.playback.PlaybackTracker(turn, fmt)` (pure ms API: `frame_sent(now)`,
  `end_segment(chars) -> mark name`, `finish()`, `on_mark(name)`, `played_ms`, `is_playing`, `stop(at)`,
  `played_text(at)`) owning a `va_voice.text.alignment.AlignmentTimeline` (`add_clause(PronouncedText | str)`,
  `add_chunk(audio, alignment | None)`, `text_until(ms)`); barge-in text via `truncate_assistant_message(timeline,
  played_ms)`. Rules: specs/07 §9 and specs/09 §7 Decisions P04-T08. Alignments are structural (tests use a local
  frozen dataclass with `chars`/`char_start_ms`/`char_duration_ms`).
- 2026-11-01 (P04-T09): outbound = `va_voice.outbound` (rules: specs/07 §9/§10.2 Decisions P04-T09): pure
  `FramePacer` (ms; `next_send_ms`, `on_sent -> lateness`, `mark_gap`, `reset`, `throttle`), `FrameMixer(fmt,
  background=, gain_db=, wire_byte_order=)` (`speech(frame)`, `idle()`), `OutboundAudio(sink, mixer, clock=)` with
  `start_utterance(tracker)`/`push_audio`/`end_segment(chars)`/`finish_utterance()`/`clear()`/`rate_limited()` and
  `run()` (supervise it). Background loops: `va_voice.background.BackgroundLibrary(assets_dir).loop(track, fmt)`.
  VAD-path noise gate: `va_voice.noise.SpectralGate` (`process` returns whole 256-sample hops, output = input
  delayed 256 samples; `set_speech` from VAD events). Tests drive `OutboundAudio` on `ManualClock`
  (`wait_for_sleep(0.02)` before `run_for`); sink timestamps need rounding (float ms noise ~1e-14).
- 2026-11-01 (P04-T10): turn latency = `va_voice.latency` (rules: specs/07 §20 Decision P04-T10). The pipeline gets a
  `TurnTimer` from `LatencyTracer(clock, metrics=TurnMetrics(build_metrics(Service.MEDIA_WORKER, ...), org_tier=),
  tracer=TurnTracer(clock, call_id=, org_id=, tracer_provider=, parent=))`. It calls `.start_turn(i, node_id)`,
  then `timer.mark(TurnMark.X[, at_ms])`, `add_tool`, and `annotate(span, {...})`, and finally `finish(timer)`,
  which returns a `TurnReport(latency, events)`. Barge-ins are counted with `LatencyTracer.barge_in(honored=,
  reaction_ms=)`. Never observe `va_stt_final_ms`/`va_tts_ttfa_ms`/`va_llm_ttft_ms` from the pipeline: the vendor
  clients already do. `packages/va_voice/tests/conftest.py` re-exports `reset_logging`, so va_voice tests can use
  `service_helpers.json_log_stream`/`log_events`. Retroactive OTel spans:
  `tracer.start_span(name, context=, start_time=ns)` + `span.end(end_time=ns)`.
- 2026-11-01 (P04-T11): phrase cache = `va_voice.phrase_cache.PhraseCache(synth, redis, metrics=PhraseCacheMetrics(...))`
  (rules: specs/09 §9 Decision P04-T11): `get_or_render(PhraseVoice(voice_id, model_id, settings, lang), text, fmt,
  ttl_s=)` → `CachedPhrase` (`cached` = Redis hit; `alignment` is a `ChunkAlignmentLike` for the timeline), raises
  `PhraseUnavailableError` → speak through the live socket; `prefetch(...)`; `aclose()`. `synth` is any object with
  `ElevenLabsRest.tts_with_timestamps`'s signature (va_voice depends on va-domain, never va-elevenlabs). The log
  redactor replaces any field whose name contains `key` (`cache_key` too): log cache entries as `entry=`.
  va_voice tests can use the va_elevenlabs test support (`rest_client_helpers.sim_rest`, `tts_helpers.VOICE_ID`).
- 2026-11-01 (P04-T12a): call pipeline = `va_voice.pipeline` (rules: specs/07 §16 Decision P04-T12a): `protocols`
  (`Transport`, `SttStream`, `TtsStream`, `VoiceActivityDetector`, `Brain`, `ActionHandler`, `PipelineObserver`,
  inbound events `InboundAudio`/`InboundDtmf`/`MarkEcho`), `models` (Brain outputs, `UserTurn`, `AgentTurn`,
  `PipelineResult`), `config.PipelineConfig`, `inbound.InboundPath` (reorder → STT + VAD on the pipeline timeline),
  `speaker.Speaker` (TTS contexts, `OutboundAudio`, playback completion, `preempt()`), `pipeline.CallPipeline` (task
  group, endpoint loop, reply tasks, watchdog), `waits.within`/`wait_until_set` (deadlines on the injected clock,
  never `asyncio.wait_for`). `OutboundAudio` gained `finish_utterance(chars)` and `on_finished=`. Harness:
  `packages/va_voice/tests/support/pipeline_harness.harness(...)` on the sim `AcceleratedClock` with fakes from
  `pipeline_fakes` (`FakeTransport` echoes marks after `echo_delay_ms`, `FakeCaller` streams a 20 ms frame forever —
  `await h.caller.say(ms)`, `streaming=False` + `send(payload, chunk)` for hand-made order, `FakeStt` partial every 10
  loud frames, `FakeTts` 5 ms/char with exact alignment, `ScriptedBrain`, `tokens(text, clock, first_delay_s=)`,
  `hanging(text)` keeps a TTS context open); drive with `await h.turn(600)` / `await h.until(predicate)`. Once
  `is_final` arrived nothing is cancelled at TTS, so tests that expect `tts.cancelled` must use `hanging(...)`.
  Never run `ruff --fix --unsafe-fixes` blindly: it rewrote `zip(a[3:], a[4:])` into `pairwise(a)`.
- 2026-11-01 (P04-T12b): barge-in = `CallPipeline._check_barge_in` (after each inbound batch and STT partial) →
  `BargeInMonitor.evaluate` → `_barge_in` (`Speaker.interrupt` → `_cancel_reply` → `Brain.on_interrupted` →
  `observer.on_interrupt(InterruptRecord)`); turn routing is `_route_turn` (backchannel drop / held / pre-audio merge
  with `supersedes`) and `_false_barge_in` (rules: specs/07 §8 Decision P04-T12b). Harness: `ScriptedBrain.bind`
  (the harness does it) logs `brain.closed`/`brain.interrupted` into `transport.calls` next to `transport.clear`/
  `tts.*`; `tokens(..., per_token_delay_s=, calls=)` logs `llm.cancel`; `RecordingObserver.interrupts`;
  `harness(partial_after=)`. A `SpeakStatic` context is final at once (FakeTts), so tests that need a TTS cancel
  at barge-in must use `SpeakText(hanging(...))`. Opener outputs that need the clock: append to
  `h.brain.opener` right after entering `harness` (before the first await).
  `ActionHandler.handle` runs shielded in `CallPipeline._action` (a reply cancel never cancels an action; the
  reply loop waits for it via `_settle_action`, the pipeline end cancels it). Slow actions in tests:
  `pipeline_fakes.SlowActions(duration_s, fail=)` passed as `harness(actions=...)` (the harness binds its clock).
- 2026-11-01 (P04-T12c): silence/DTMF = `CallPipeline` tasks `silence` (`pipeline.silence.NoInputTimer`, armed only
  via `_arm_no_input()` when `_idle()`; disarmed by VAD `speech_start`, digits and every job) and `dtmf`
  (`pipeline.dtmf.DtmfBuffer`, 2 s / `#`); keypad turns go through the same `_route_turn` (now serialised by a lock,
  `timer` may be `None`). The no-input timer now fires in every harness test that idles > 8 s of virtual time after
  the last utterance: give such tests `PipelineConfig(no_input_timeout_ms=30_000)` instead of weakening asserts.
  Harness: `ScriptedBrain(no_input=[[...]])` scripts `on_no_input` (empty = the pipeline's reprompt; logs
  `brain.no_input` into `transport.calls`), `h.caller.press(digit)`. Never log DTMF digit values.
- 2026-11-01 (P04-T13): `pytest.mark.bench` tests (`packages/va_voice/tests/bench/`) are deselected by that dir's
  conftest unless `-m` mentions `bench`; a full non-xdist bench run rewrites `packages/va_voice/BENCHMARKS.md`
  (`VA_BENCH_REPORT` overrides the path). `harness(clock=RealtimeClock())` runs the pipeline harness on the wall
  clock; `pipeline_bench.run_conversation(clock=, turns=)` is the scripted zero-latency call. The outbound clock
  sends frames only while speech/background plays (no idle frames without a background track).
- 2026-11-01 (P05-T01): Telnyx webhook intake = `va_edge.intake.WebhookIntake` (routes from `telnyx_routes()`),
  `va_edge.keys.WebhookKeys` (route-selected key; BYO via system `TelnyxAccountLookupRepo`), `va_edge.processor.
  EventProcessor` (8 shards × 256 queue, handler registry `create_app(handlers={event_type: fn})`, 2 s budget,
  `join()` test seam) and `va_edge.reprocessor.TelnyxEventReprocessor` (`TelnyxEventRepo.claim_retries`; rules:
  specs/06 §4.1 Decision P05-T01). The edge lifespan needs `TELNYX_PUBLIC_KEY` (local falls back to
  `.sim/telnyx_public_key`); its container must expose `database` (`va_edge.container.EdgeDependencies`). Tests:
  `apps/telephony_edge/tests/support/edge_helpers.py` (`Keypair`, `signed_headers`, `envelope`, `event_id_of`,
  `edge_client(url, key, handlers=)`, `ProbeContainer` for probe tests, `stored_event`, `metric`), session fixture
  `edge_db_url` (one migrated Postgres per xdist worker, shared by all edge modules: filter by your own ids, and
  handlers must ignore rows of other tests a reclaim picks up). `updated_at` is always the DB clock (trigger), so
  reclaim tests pass a `FrozenClock(now + 60 s)` instead of editing timestamps. Never parametrize with random
  uuids (xdist collection mismatch); FakeTelnyx ids are seeded, so contract tests on a shared DB use
  `telnyx_state(transport, seed=secrets.randbits(32))`.
- 2026-11-01 (P05-T02): carrier events = `va_edge.mapping.plan_effects` (pure, every specs/06 §5 type; spec
  Decision under specs/06 §5.1) applied by `va_edge.carrier.CarrierEventHandler` (default edge registry via
  `va_edge.main.carrier_handlers(container, ...)`; the edge container now exposes `redis` and `temporal`). Call/leg
  state machines: `va_db.call_states` + `va_db.repositories.CallRepo.transition` (guarded UPDATE, raises
  `CallTransitionError` with `.current`), `CallLegRepo` (`record_initiated` upsert, guarded `advance`), system
  `CallLegLookupRepo` (12 system repos). Carrier keys/contract: `va_domain.carrier` (`CarrierEvent`,
  `carrier_state_key`, `carrier_channel`, `owner_key`, `call_workflow_id`, `CARRIER_SIGNAL`). Tests: seed calls with
  `packages/va_db/tests/support/call_rows.py` (`seed_call`, `seed_leg`, `load_call`, `load_leg`); edge
  `DbContainer`/`ProbeContainer` carry fakeredis + `never_temporal()` (default handlers never reach Temporal unless
  a call resolves); `edge_helpers.RecordingSignaller`; the specs/06 §5 event-type parser is
  `packages/va_telnyx/tests/support/telnyx_spec.spec_voice_event_types`. A `FrozenClock` app clock makes the
  processor's 2 s handler budget fire at once (`HandlerTimeoutError`): give apps that process real handlers a clock
  with real `sleep` (see `SimTimeClock` in `test_sim_contract.py`).
- 2026-11-01 (P05-T03a): Send Call schemas = `va_domain.calls` (rules: specs/05 §3 Decision P05-T03a): `CallParameters`
  (every §3.2-§3.7 parameter; lower config layers parse with it), `SendCallRequest` (+ runner fields, `phone_number`;
  `explicit_fields()` folds legacy aliases, `check_rules()` → `CallRequestError(code, message, field)`,
  `runner_type`), `unknown_fields(raw)`, `PARAMETER_NAMES` (Bland names: `from`, not `from_`), shared nested models in
  `calls.parts`, `InlineTool`/`DynamicDataSpec` in `calls.tools` (P08 re-exports them), `SendCallConfig` in
  `calls.config` (drift-checked against the §4.4 block by `packages/va_domain/tests/test_call_config.py`; its field
  blocks sit between `jscpd:ignore` markers because they mirror the spec). The specs/08 §5 language table is
  `va_domain.languages` (`lookup_language`); `va_elevenlabs.languages` re-exports it. Destination parsing:
  `va_domain.phone.normalize_e164(value, region)` (raises `InvalidPhoneNumberError`, a `ValueError`; phonenumbers
  ships `py.typed`). Bland sample bodies: `apps/api/tests/fixtures/bland_send_call/*.json`. A dict-key validator
  (`dict[Annotated[str, AfterValidator(f)], V]`) puts the key into the error `loc`, so `field_path` gives
  `persona_settings.<key>`.
- 2026-11-01 (P05-T03b): call config resolution = `va_domain.call_resolution` (rules: specs/05 §4.3 Decision
  P05-T03b): `merge_layers(org, request, runner)` → `MergedParameters` (`voice`, `from_`, `dialing_strategy`, `tools`,
  `country` for the loader), then `resolve_call_config(org, request, runner, refs, now)` → `CallResolution(config,
  warnings)`. Build `org` with `load_settings(stored)` (only explicitly set `default_*` keys are layered). Lower
  layers parse with `layers.parse_layer(raw, prefix)` (`CallDefaults`, extra forbidden). Test builders (fixed ids,
  `NOW`, `make_refs`, `resolve`, `rail`) live in `packages/va_domain/tests/support/resolution_helpers.py` (on
  pytest `pythonpath` and mypy `mypy_path`). The 12 Bland fixtures have resolved snapshots
  (`apps/api/tests/fixtures/bland_send_call_resolved/`); after a deliberate change run
  `PYTHONPATH=packages/va_domain/tests/support uv run python apps/api/tests/support/resolved_snapshots.py --write`
  and prettier the JSON (the test compares
  parsed JSON, so formatting does not matter).
- 2026-11-01 (P05-T03c): Send Call input loading = `va_api.services.call_inputs.load_call_inputs(session, ctx, request,
  *, org, encrypted_key, models, va_env)` (§4.1 steps 4-8 in the caller's `tenant_session`; rules: specs/05 §4.2
  Decision P05-T03c) then `resolve_inputs(inputs, request, now)`. Pieces: `call_runners.load_runner` (agent/persona/
  pathway versions), `call_pathways.load_pathway` (`PathwayFields` names the error fields per source),
  `call_refs` (voice, tools/KBs, citations/evals/memory/pool, plan features, BYO account), `caller_id` (destination,
  `local_order`, read-only caller-ID selection; specs/16 §6.2 Decision). `va_common.nanp` (`npa`, `state_for_npa`,
  `same_state`) reads the generated `va_common/data/npa_states.json`. `va_domain.call_resolution.pathway_call_defaults`
  maps a version's `global_config` to the pathway layer. Tests: `apps/api/tests/support/call_input_rows.py` (`Seed`
  = an org on the module DB with builders `pathway`/`persona`/`agent`/`voice(platform=)`/`number`/`pool`/generic
  `row(Model, **values)` via `RowFactory`, `load(body, org=, ctx=, encrypted_key=)`; `make_ctx(org_id, scopes=,
  environment=, plan_id=)`; `random_e164(npa)` because `phone_numbers.e164` is unique among active rows across
  orgs). Agent versions need explicit increasing `created_at` (rows inserted in one transaction share `now()`).
- 2026-11-01 (P05-T04a): `packages/va_compliance` (dist `va-compliance`; the api depends on it) = Send Call step 9:
  `check_call_compliance(session, OrgFacts(org_id, plan_id, kyc_level, limits, settings), destination=, region=, at=,
  now=, scheduled=, allowed_countries=)` in the caller's tenant session (rules: specs/05 §4.1 Decision P05-T04a).
  Pieces: `fraud.blocked_prefix`, `dnc.find_block` (platform/own/parent via `use_org`), `international`
  (`international_allowed`, `parse_allowed_countries`; `ApiSettings.allowed_destination_countries()`),
  `calling_windows` (`callee_timezones` returns every candidate zone, `in_window`, `local_minute`), `state_rules`
  (data `va_compliance/data/state_rules.json`, every entry cites statute + https source + `last_verified`; changing
  an entry needs the statute re-checked and the specs/17 §7 Decision updated), `calling_hours.calling_hours_open`.
  DB tests: session fixture `compliance_db_url` + `tests/support/compliance_rows.py` (`new_org(parent=, settings=)`,
  `add_dnc(org_id | None, e164)` through `system_session` because va_app cannot write platform rows, `random_e164`).
- 2026-11-01 (P05-T04b): `packages/va_billing` (dist `va-billing`; the api depends on it) = Send Call steps 10-11
  (rules: specs/24 §6 Decision P05-T04b): `plans` (`effective_limits`/`effective_pricing`/`effective_price`; the api
  `org_schemas.effective_limits` projects it onto `OrgLimits`), `keys` (specs/03 Redis key strings, UTC), `scripts`
  (`LimitScripts` over `redis_scripts/*.lua`, cap `None` → -1), `limits.CallLimiter(redis, clock=)` (`acquire` →
  `LimitTicket.rollback()`, `release_call`, `active_calls`, `usage`), `holds` (`reserve`/`reserve_call_hold`/
  `capture`/`release`, `held_micros` lives in `va_db.repositories.credits`), `dispatch.admit_call(session, limiter,
  load_billing_org(session, org_id), CallAdmission(...), now=)` → `Admitted.compensate`. Tests:
  `tests/support/billing_helpers.py` (`make_plan` with seed values, `inputs(...)`, `another_call`, `limiter_rig()` on
  fakeredis + `FrozenClock(NOW)`; conftest fixture `rig`), `billing_rows.py` (`new_billing_org` defaults to
  `created_at = NOW - 30 days`, else the real insert time makes the org a "new account" for the frozen clock;
  `holds_of`). Adding an error code also bumps the row count in `packages/va_common/tests/test_errors.py`.
- 2026-11-01 (P05-T05a): outbound URL vetting = `va_common.net.check_destination_url(url, allow_http=, allow_private=,
  resolver=)` (returns the vetted addresses, raises `UnsafeDestinationError(.reason)`), `blocked_address`, `parse_ip`,
  `system_resolver`, `HostResolver` (rules: specs/19 §8 Decision P05-T05a). `WEBHOOK_ALLOW_PRIVATE` is the shared
  `va_common.settings.WebhookSafetySettings` mixin (dispatcher + api). Send Call webhook check:
  `va_api.services.webhook_policy.check_call_webhook(url, org=, settings=, resolver=)`. Tests always inject a fake
  `resolver=` (never real DNS; `system_resolver("127.0.0.1")` works offline). Python flags IPv6 `::1` as
  `is_reserved` (inside `::/8`), so loopback is exempted from the always-blocked reserved rule.
- 2026-11-01 (P05-T05b): Send Call = `va_api.services.call_dispatch.dispatch_call(deps_of(request), ctx, body,
  encrypted_key=)` behind `POST /v1/calls` (`routers/calls.py`; rules: specs/05 §5 Decision P05-T05b). Callers that
  write a `call_events` row in their own transaction use `EventPublisher.sequence(event)` → `va_events.insert_rows(
  session, [row_for(e)])` → commit → `EventPublisher.broadcast(e)`. `CallWorkflow` start contract:
  `va_domain.call_workflow` (`CALL_WORKFLOW_NAME`, `CallWorkflowInput`); the api starter is
  `va_api.services.call_starter.TemporalCallStarter` (`CallStartError`). Container gained `call_limiter`,
  `event_publisher` (no sink), `call_starter`, `host_resolver`. Tests: `SessionContainer` has `call_starter:
  FakeCallStarter` (`.started`, `.fail`) and `host_resolver: FakeResolver` (`.answers`, `.lookups`; default public
  `8.8.8.8`) from `tests/support/call_fakes.py`; `call_helpers.callable_org(identity, container)` (plan build, created
  30 days before the clock, $50, org voice `Karen`, owned number, live `*` key), `task_body(org, **over)`,
  `send_call(client, key, body, **headers)`. Any test that dispatches needs a container clock inside the migration's
  `call_events` partitions: `SessionContainer(identity=..., clock=FrozenClock(call_helpers.business_hours_today()))`
  (the default `FrozenClock` 2026-01-01 has no partition).
- 2026-11-01 (P05-T06a): `packages/va_workflows` (dist `va-workflows`; the worker depends on it) holds Temporal
  activities (and, from P05-T06c, workflows); `va_workflows.activities/__init__.py` stays import-free and activity
  inputs (`activities.inputs.CallActivityInput`) are plain pydantic models (worker client: `pydantic_data_converter`).
  Call context = `va_domain.call_context.CallContext` (`call:{<id>}:ctx`), built only by
  `va_db.call_context.build_call_context(call, now=, trace_context=)` and stored by `CallContextStore` (JSON
  `by_alias`: `calls.config` written by dispatch holds `voicemail.sms.from_`, both forms validate). Media WS tokens =
  `va_common.stream_tokens` (`mint_stream_token`, `verify_stream_token(..., secrets=stream_token_secrets(settings))`,
  `stream_token_expiry`); `StreamTokenSettings` hides inputs in validation errors. Activity tests run through
  `temporalio.testing.ActivityEnvironment().run(...)`; resolved configs for test rows come from
  `resolved_configs.resolved_config(name)` (va_domain tests/support); `call_rows.seed_call(..., config=...)`
  overrides any default column.
- 2026-11-01 (P05-T06b): call activities are classes with keyword-only collaborators: `TelephonyActivities(database=,
  telnyx=TelnyxClientFactory, limiter=, publisher=, settings=DialSettings.for_env(va_env, ...), clock=)` (`dial`,
  `hangup_call`) and `CallEndActivities(database=, limiter=, publisher=, redis=, clock=)` (`end_call`); inputs, results,
  timeouts and retry policies in `va_workflows.activities.telephony_inputs`, error type names in `activities.errors`
  (both workflow-safe). Workflows must call `end_call` before `hangup_call`. Event rows go through
  `activities.events.persist_events` (in the change's transaction) + `broadcast_events` after the commit. Connected/
  transfer seconds: `va_db.call_states.call_durations` (the edge uses it too); `CallLegRepo.primary(call_id)` finds the
  primary leg. Tests: `packages/va_workflows/tests/support/telephony_rig.py` (`rig(database, answer=, realtime=)`:
  FakeTelnyx via ASGI with every request recorded in `Rig.sent`/`dials()`, fakeredis, `SystemClock` for the
  `call_events` partitions, `seed`, `byo_account`, `failing_factory(r, handler)` without client retries,
  `broken_redis()`). FakeTelnyx's accelerated clock jumps to the next carrier timer (time limit!) whenever the loop
  waits on Postgres: tests that need a live carrier call use `realtime=True`. FakeTelnyx answers a repeated
  `command_id` as a duplicate (`ok`), even after the call ended.
- 2026-11-02 (P05-T06c): `CallWorkflow` = `va_workflows.calls` (pure signal state `calls.state.CallSignals`, timers
  `calls.policies`, result `calls.results`; rules: specs/01 §2.1 Decision P05-T06c); liveness activities
  `va_workflows.activities.liveness.CallLivenessActivities(redis=, clock=)`; `va_workflows.registration.CALL_WORKFLOWS`.
  Workflow modules import only import-free modules inside `workflow.unsafe.imports_passed_through()` and schedule
  activities by name (`execute_activity("dial", ..., result_type=X)` returns `Any` to mypy: assign to a typed
  variable). Tests: `tests/support/workflow_rig.py` (`workflow_env()` = time-skipping server with the pydantic
  converter, ~1 s per test; `FakeCallActivities` with the production activity names, `send={activity: [signals]}`
  delivers carrier/cancel/pipeline signals from inside that activity; `run_call` → `CallRun(result, post_call,
  phase, history)`; stub child in `postcall_stub.py`). Time skips only while no activity is running (a parked
  activity blocks skipping and `env.sleep` alike), so conversations that must wait on a timer use watch mode
  `fail`; querying a running workflow while awaiting `env.sleep` wedged the test server — query only after the
  result. A watch started and cancelled in the same workflow task is never dispatched (absent from `names()`).
- 2026-11-02 (P05-T06d): worker = `va_worker.main.build_container` (pydantic-converter Temporal client, Telnyx factory
  with `va_db.telnyx_keys.TelnyxKeyResolver`, `va_worker.activities.WorkerActivities` + `main_registrations`);
  `TemporalWorkers().run(source, registrations)` takes the registrations from the container. The container needs
  `STREAM_TOKEN_SECRET`, `TELNYX_CALL_CONTROL_APP_ID`, and `LOCAL_MASTER_KEY` (tests: `worker_helpers.dialing_settings`).
  `TemporalConnector`'s `connect=` seam now takes `(host, namespace, data_converter)`. Worker end-to-end test:
  `apps/worker/tests/test_worker_call_flow.py` (real activities + `telephony_rig.rig(realtime=True)` +
  `workflow_env()`; `Rig.dialed` is set by the first `POST /calls`; a default-converter client =
  `config = env.client.config(); config["data_converter"] = DataConverter.default; Client(**config)`).
- 2026-11-02 (P05-T07a): media WS attach = `va_media.server.MediaAttach` (`/ws/telnyx`, built by the lifespan into
  `app.state.media_attach`), `va_media.auth.MediaAuth`, `va_media.ownership.CallOwnership` (Lua renew/release),
  `va_media.session.CallSession` (body seam `SessionBody`, `SessionEnd`, renewer + heartbeat), `va_media.pods`,
  `va_media.socket` (`MediaSocket` protocol; `StarletteSocket` adapter). Rules: specs/07 §3 Decision P05-T07a.
  `TELNYX_PUBLIC_KEY` loading is `va_common.settings.TelnyxPublicKeySettings` (edge + media); the ws/wss choice
  is `va_common.stream_tokens.media_ws_scheme`. `probe_app(lifespan, routes=[...])` adds service routes. Tests:
  `media_helpers.media_app(models_dir, keys=MediaKeys(), clock=ManualClock(), body=RecordingBody())` (fresh signing
  key + secret, `PUBLIC_HOST`), `served_media(app)` (real uvicorn, yields `ws://...`), `signed_connect(url, keys,
  clock.now(), call_id, leg=, token=, signed_host=)`, `close_code(ws)`, `connected_msg`/`start_msg`/`stop_msg`,
  `FakeSocket` for session unit tests, `auth_values()` for hand-built `MediaSettings`. Sync on
  `registry.wait_idle(5)` after a close (the registry releases after the WS closes). Starlette turns a send
  OSError into `WebSocketDisconnect`; a refusal closed before the client sends makes a later `ws.send` raise.
- 2026-11-02 (P05-T07b): after `start` the attach loads the call context (`va_media.context.DbCallContextLoader`,
  status check per specs/28 §6.2), subscribes the call's channels on the pod-wide `va_media.channels.ChannelHub`
  (one pub/sub connection; its `run` is the only reader; `subscribe` returns after both Redis confirmations), reads
  `carrier_state` (`va_media.carrier_state.CarrierSnapshot`), then runs `CallSession(context=, feed=,
  carrier_state=, control_handler=)`. Control messages are `va_domain.control` (`parse_control`); handlers implement
  `va_media.control.ControlHandler` (`DefaultControlHandler` = stop/credits/drain notice). `create_app` takes
  `context_loader=` (factory `(container, clock)`, default `db_context_loader` needs `container.database`) and
  `control_handler=`. Tests: `media_helpers.served(models_dir, loader=, control_handler=, **settings)` (moved from
  test_server; `Served.loader` is a `FakeContextLoader` with `.contexts`/`.refusals`/`.loads`), `call_context(
  call_id, org_id=)` for a valid `CallContext`; a `create_app` without `media_app` needs
  `context_loader=FakeContextLoader().factory` unless the container has a `database`. Drain waits on the
  injected clock (`registry.wait_idle(t, clock=)`, `LiveSessions.wait_idle`), so ManualClock drain tests
  `wait_for_sleep(MEDIA_DRAIN_TIMEOUT_S)` + `run_for`.
- 2026-11-02 (P05-T08): the Telnyx media WS as a pipeline `Transport` = `va_media.transport.TelnyxMediaTransport(
  session.socket, session.start, on_rate_limited=)` (`events()` once; `send_audio`/`send_mark`/`clear` serialized,
  dropped after the stream ended; `leg_format(media_format)`; `log_summary()`); `MediaSocket.send_text` raises
  `SocketClosedError`. Rules: specs/06 §8 Decision P05-T08. Tests: `media_helpers.FakeSocket.sent`/`send_gate`,
  `media_msg`/`dtmf_msg`/`mark_msg`/`error_msg`; FakeTelnyx-to-media-worker calls via
  `sim_media.streamed_call(models_dir, body, **dial_overrides)` (wall-clock sim; `StreamedCall.sim_call().stream.
  session.received` counts what the sim got). `CallPipeline` runs over the transport with the va_voice
  `pipeline_fakes` on an `AcceleratedClock` (`test_transport_pipeline.py`).
- 2026-11-02 (P05-T09a): `packages/va_engine` (dist `va-engine`, deps va-domain/orjson/phonenumbers) exists; only
  `templating`, `builtins`, `models.state` so far (rules: specs/11 §8/§9 Decisions P05-T09a). Templates:
  `parse_template(text)` once, then `render`/`render_url`/`render_json(value, TemplateScope(variables=, request_data=,
  builtins=call_builtins(ctx, now=), node_vars=, input=, timezone=, missing=MissingVariables(sink)))`; scope values are
  plain mappings. `EngineState.add_turn` enforces the 200-turn cap; read stored state with `load_engine_state`.
  va_engine tests use `resolved_configs.resolved_config()` (va_domain tests/support) to build a `CallContext`.
- 2026-11-02 (P05-T09b): voice prompts = `va_llm.prompts` (`VoicePromptInputs` plain values, `render_voice_system`,
  `fit_voice_system`, `tool_usage_note`, `dtmf_message`/`context_update`/`interrupted_message`/`example_messages`,
  `assemble_messages`, `load_prompt(name)` for packaged `prompts/<name>.txt`; `voice_system.txt` is drift-checked
  against the specs/10 §5 block). Tokens: `va_llm.tokens.default_counter()` over the vendored
  `data/cl100k_base.tiktoken.gz` (sha256-pinned; never call `tiktoken.get_encoding`, it downloads). Window:
  `va_llm.window.ConversationWindow(budget, counter).fit(history)`. System tools: `va_engine.system_tools`
  (`system_tools(facts)`, `facts_for_call(context, settings, sms_capable=)`, `parse_system_call` → typed args |
  `InvalidToolCall(.tool_message(id))` | `None` for custom names, `resolve_transfer`). Rules: specs/10 §5/§6/§7
  Decisions P05-T09b. va_engine depends on va-llm. `calendar.day_name` follows the process locale: use a fixed table.
- 2026-11-02 (P05-T09c): task-mode Brain = `va_engine.task_runner.TaskBrain(context=, settings=, gateway=, counter=,
  facts=, clock=, executor=None, memory_context=None)` (rules: specs/11 §6 Decision P05-T09c); round planning is
  pure in `va_engine.task_tools.plan_round`, actions are `va_engine.models.results.TransferRequest`/`PressButton`
  (BrainAction subclasses), I/O tools go through the `va_engine.tool_executor.ToolExecutor` protocol, prompt inputs
  via `va_engine.voice_inputs.voice_prompt_inputs`; `va_domain.languages.language_name(value)`. va-engine depends
  on va-voice. Tests: `packages/va_engine/tests/support/task_helpers.py` (`served()` = FakeLLM on a socket +
  ManualClock gateway → `Sim.brain(context, ...)`, `Sim.rules(yaml)`, `Sim.requests()`; `task_context(direction,
  **config)`, `user_turn`, `drain(outputs)`, `RecordingExecutor`, `assert_tool_pairing`, `tool_messages`). FakeLLM
  rules distinguish tool rounds with `call_index` (counted per `X-VA-Call-Id`, which is the context's call id).
  `pipeline_harness.harness(engine=brain, clock=RealtimeClock())` runs a real Brain (then wait on
  `h.observer.user_turns`, not `h.brain`); a harness barge-in at the default level needs ≥ 900 ms of caller speech
  (or ≥ 500 ms with two content words in the partial).
- 2026-11-02 (P05-T10a): every attached call runs `va_media.runtime.CallRuntime` (rules: specs/07 §16 Decision
  P05-T10a); the lifespan enters `create_app(runtime=open_runtime)` only when no `body=` is injected, so any test
  that builds the media app without `media_helpers.media_app` must pass `body=read_until_stop` (or a fake
  `runtime=` factory) or the lifespan needs `LOCAL_MASTER_KEY` and a real `database`. Pure context → pipeline/STT/TTS
  mapping is `va_media.runtime_config`; `MediaSettings` now mixes in `CryptoSettings`, `ElevenLabsSettings`
  (`va_elevenlabs.settings`) and `LlmSettings`. End-to-end runtime tests (`apps/media_worker/tests/test_runtime_sim.py`):
  one `sim_media.realtime_telnyx()` state serves FakeTelnyx (in-process) plus `running_sims(state, ports={"elevenlabs":
  0, "llm": 0})`, `streamed_call(..., state=, loader=FakeContextLoader(**config_overrides))`, scenarios matched on
  `to: CALLEE`; assert on `state.elevenlabs.tts_requests[*].generated_text` (the socket's own record is empty) and
  `state.llm.requests` — the sim stream object is gone once the call hung up. Real Silero model path in tests:
  `Path(__file__).resolve().parents[3] / "models"` (the `models_dir` fixture holds fake bytes).
- 2026-11-02 (P05-T10b): `CallPipeline(before_opener=)` runs before the opener; `PipelineConfig.wait_for_greeting`/
  `greeting_timeout_ms` make the reply loop hand the first user turn to `Brain.start(greeting)` (every Brain's
  `start` takes `greeting: UserTurn | None = None`); `ivr_mode` adds the `ivr` task (`IvrProgress`, `ivr_dead_end_s`).
  The no-input timer is never armed before the opener. Media: `GreetingGate` (answered wait 2 s + precall DTMF),
  `CarrierWatch` (the only reader of `CallFeed.next_carrier`; T10c adds AMD there), `MediaActions` (press_button),
  `dtmf_out` (groups/pauses). Telnyx settings/pool/factory moved to `va_telnyx.settings` (`TelnyxApiSettings`,
  `telnyx_http`, `telnyx_factory`, `SIM_TELNYX_API_KEY`), mixed into worker and media settings. Runtime sim tests:
  `streamed_call(..., answered=True)` seeds the carrier state (else every outbound call waits 2 s); the rig's
  `CallRuntime(telnyx=telnyx_factory(...))` talks to the in-process FakeTelnyx at `http://sim/v2`, and
  `streamed.sim_call().dtmf_sent` lists the groups (read it inside the `async with`).
- 2026-11-02 (P05-T10d): opener pre-render = `va_workflows.activities.prerender.OpenerPrerender` (worker container
  builds it over `ElevenLabsRest` + `PhraseCache` + `OrgSettingsReader`; `CallContextActivities(..., prerender=)`
  saves `CallContext.opener_text` first, then renders; never raises). Key helpers shared by worker and media live
  in `va_engine.opener` (`render_opener`, `phrase_voice`, `phrase_format`, `cache_text`); the Speaker looks up the
  PRONOUNCED text, so any key must go through `cache_text`. Pipeline seam: `CallPipeline(phrases=PhraseSource)`
  (`va_media.phrases.CallPhrases` over `va_voice.phrase_cache.read_phrase`); harness `harness(phrases=...)`. A
  cached `SpeakStatic` opens no TTS context (`tts.started == []`). `ElevenLabsRestSettings` (worker) and
  `ElevenLabsSettings` (media) share `ElevenLabsAccountSettings` (key + TTS model). Runtime sim test rig:
  `rig(redis=...)`, `Rig.call(opener_text=...)`; FakeElevenLabs REST records have `transport == "rest"`, and the
  sim stream object is gone after hangup — read `streamed.sim_call().audio.agent` instead.
- 2026-11-02 (P05-T10d): Ralph kills an iteration after 300 s with no agent activity (diagnostics log: `Execution
  inactivity timeout reached`) and turns the missing event into an empty `review.rejected`. `make test` (~25 min) is
  silent for longer: run it with Bash `run_in_background` (`make test > logs/make_test.log 2>&1`) and a Monitor that
  prints a heartbeat every ~2 min until `exit=` appears.
- 2026-11-02 (P05-T11a): in-call persistence = `va_media.turns` (`TurnRecorder` = the call's `PipelineObserver`,
  sync callbacks: turn ops go to the sink at once, events publish through one ordered task chain, `drain()`;
  `CallPersistence(turns=, events=, event_rows=)` builds recorders and `finish_call`s) + `va_media.turn_writer.
  TurnWriter` (write-behind ops from `va_db.call_turns`: `CallTurnRow`/`LatencyMerge`/`TurnDelete`, applied by
  `va_db.repositories.CallTurnRepo`) + `va_media.runtime.open_persistence(database, redis, clock=)` (buffer +
  writer loops; `CallRuntime(persistence=)`, default `None` = no recording). `TurnReport.turn_index` names the
  user turn the latency belongs to. Shared trigger wait of write-behind loops: `va_events.batching.wait_for_trigger`.
  Media Postgres tests use the session fixture `media_db_url` (conftest); runtime sim tests write to it with
  `rig(persistence=...)` + `Rig.call(seeded=seed_call(...))` (`streamed_call(call_id=)` fixes the call id).
- 2026-11-02 (P05-T11b): media end-of-call routine = `va_media.call_end.CallEnd` (a `va_media.session.SessionEnder`
  run by `CallSession` before its cleanup; rules: specs/07 §18 Decision P05-T11b). The lifespan wires it only from a
  body exposing `end_routine` (`CallRuntime`; test bodies without it run no routine). Shared pieces:
  `va_db.call_end.record_answered_end`/`stored_reason`/`ended_values` (also the `end_call` activity),
  `va_events.call_ended_event`/`ENDED_MESSAGE`, `va_common.checks.signal_workflow` (False = no such workflow; edge
  and media), `va_media.workflow_signal.TemporalPipelineSignaller`. `CallWorkflow` hangs up on a `worker_lost`
  pipeline summary. Tests: `media_helpers.RecordingSignaller` (`signalled` event), `apps/media_worker/tests/
  test_call_end.py` (`BrokenDatabase`, fake Telnyx factory), runtime sim `ending_rig(media_db_url)` +
  `streamed_call(container=)` to share the media container's Redis with the routine.
- 2026-11-02 (P05-T11c): stop endpoints = `va_api.services.call_stop` (`stop_call`, `stop_active_calls`,
  `TemporalCallCanceller` → `Container.call_canceller`; rules: specs/05 §7.3 Decision P05-T11c). api call events in
  the caller's transaction go through `va_api.services.call_event_rows.persist_event` / `broadcast_event`.
  `va_common.checks.signal_workflow(connector, id, signal, *args)` sends zero/one/many args. Tests:
  `SessionContainer.call_canceller` = `call_fakes.FakeCallCanceller` (`.cancelled`, `.missing`, `.fail`);
  `workflow_fakes.SignalRecorder` + `connector_of` (va_common tests) fake a Temporal client for signals;
  `session_helpers.hold(...)` returns the hold id; the cross-tenant `call_id` factory seeds an `in_progress` call.
  Resource-specific not-found codes (`CALL_NOT_FOUND`) are accepted refusals in `cross_tenant.REFUSED`.
- 2026-11-02 (P05-T10c): AMD/voicemail. `amd_mode`/`classifier_enabled`/`amd_verdict` live in `va_domain.voicemail`
  (`va_workflows.activities.dial_request` re-exports `amd_mode`). Pipeline seam: `CallPipeline.take_over(Takeover(
  reason, text=, beep=))` (sync, first end wins, may be called before `run()`; a takeover before the opener skips
  it; `pipeline.speech_cue` is the VAD state of the silence cue). `va_voice.pipeline.ObserverFanout(*observers)`
  fans pipeline callbacks out. Media: `CarrierWatch.amd`/`verdict`/`beep`/`on_verdict`; `GreetingGate` waits
  `AMD_WAIT_S` for the verdict when `amd_mode == "premium"`; `va_media.voicemail.VoicemailMonitor` (outbound only;
  `bind(pipeline.take_over)` then `start(watch.verdict)` before the pipeline runs, `run()` = classifier task,
  `stop()` awaits the background `call.voicemail`/`answered_by` recordings before the call's events flush).
  Gotcha: the default resolved config is AMD `premium`, so every outbound media test now waits up to 3 s for a
  verdict unless the carrier snapshot holds one: `gate_rig(answered_by=...)` (default `human`) in
  `test_outbound_dtmf.py`, and the runtime sim `Rig.call()` seeds `answered_by=human` unless `amd="premium"` is
  passed, which dials with AMD and relays FakeTelnyx's verdict/beep through `sim_media.EdgeRelay` (the edge's
  carrier-state hash + carrier channel). The runtime sim `rig(database=...)` also gives the runtime a database
  (`DbAnsweredBy`). Scenario `say` texts with commas need quoting in the YAML flow mapping.
- 2026-11-02 (P05-T10e): voicemail SMS = `Takeover.after_message` (async hook run after the takeover message played,
  capped by `after_message_timeout_s` = 15 s, never run without text or when the call closed first) +
  `va_media.voicemail_sms.VoicemailSms(database=, telnyx=, events=, platform_profile_id=, edge_public_host=,
  clock=).send(context)` (never raises; the runtime passes it to `VoicemailMonitor(sms=...)` only with a database)
  + `va_db.voicemail_sms` (`sms_sender` = active SMS-capable org number, shared with the Send Call
  `SMS_NOT_ENABLED` check; `queue_outbound_message` reuses the specs/21 §1 active conversation or opens a send-only
  one; `record_sent`/`record_failed`). The message id is `voicemail_message_id(call_id)` (uuid5): the duplicate
  guard, one Telnyx attempt. `MediaSettings` gained `edge_public_host` and `telnyx_messaging_profile_id`. Tests:
  `packages/va_db/tests/support/sms_rows.py` (`random_e164`, `add_sms_number`, `add_telnyx_account`); runtime sim
  `rig(messaging=True)` provisions a FakeTelnyx messaging profile + SMS number (`Rig.sms_number`, the inventory is
  deterministic: insert it into the DB in one test only) and `machine_call(r, call, voicemail)` drives a beeping
  machine to `voicemail_left`; outbound SMS records live in `state.telnyx.accounts[DEFAULT_API_KEY].messages`.
  SQLAlchemy 2.1 deprecates `ARRAY.any()` (error here): use `column.contains([value])`.
- 2026-11-02 (P05-T10f): voicemail retry = `va_domain.call_retry` (`retry_wanted`, `retry_attempt`, `child_config`,
  `RETRY_ATTEMPT_KEY = "retry_attempt"`, a non-config `calls.config` key like `pricing`) + the activity
  `va_workflows.activities.retry.RetryActivities.schedule_retry_call` (inputs/policy in `retry_inputs`, `RetryPlan`;
  on the worker main queue). The child is an ordinary scheduled call (dial-time admission records refusals on the
  child); `CallWorkflow` runs the activity after the post-call start (not for `not_found`) and starts the child
  `CallWorkflow` abandoned (`CallWorkflowResult.retry_call_id`). Rules: specs/07 §14 Decision P05-T10f. Created-event
  messages are `va_events.QUEUED_MESSAGE`/`SCHEDULED_MESSAGE`. Tests: `FakeCallActivities.retry` (default
  `no_retry`), `names()` omits `schedule_retry_call` (`retry_calls()` lists it), `telephony_rig.Rig.retry()`.
  The time-skipping test server does NOT skip time for an abandoned child awaited from the client (`result()`
  and `env.sleep` stall ~20 s then "No completion event found"): assert the child's start input and
  `TIMER_STARTED` duration from its history (find the event by type), then terminate it if still running. The
  child runs on the same worker and fakes: assert parent activities with `FakeCallActivities.calls_of(call_id)` /
  `retry_calls(call_id)` (filtered by workflow id) and script `retry=[plan, NO_RETRY]` so the child stops there.
- 2026-11-02 (P05-T12a): inbound config = `va_domain.numbers` (`InboundConfig` subclasses `CallParameters`, extra
  forbidden, `OUTBOUND_ONLY_FIELDS` rejected, `ROUTING_FIELDS`, `load_inbound_config` → `InboundConfigError(.field)`,
  `Maintenance`/`maintenance_active`); its request layer is `va_domain.call_resolution.inbound_request(config,
  caller=, called=, org=, org_name=)` (only set keys, `prompt` → `task`, no runner → `OrgSettings.default_inbound_prompt`).
  The Send Call loaders moved from `va_api.services` to `va_db.call_inputs` (`runners`, `pathways`, `refs`,
  `caller_id`, `loader`; package `__init__` import-free): `loader.load_call_inputs(session, org_id, request, plan_id=,
  ...)` (steps 5-8), `loader.load_inbound_inputs(..., number=InboundNumber(...))`, `resolve_inputs`, `domain_errors`;
  `va_api.services.call_inputs` only adds `check_call_permissions`. Loader row builders are
  `packages/va_db/tests/support/call_input_seed.Seed` (the api `call_input_rows.Seed` subclasses it with `load`/`fails`).
- 2026-11-02 (P05-T12b): inbound routing = `va_edge.routing` (pure `decide(RoutingFacts)` → `Reject`/`Fallback`/
  `Accept(config)`; `InboundRouter(database, limiter, telnyx, clock=, metrics=, acceptor=)` wired by
  `main.carrier_handlers(..., acceptor=)` / `create_app(acceptor=)`; P05-T12c implements `InboundAcceptor.accept(
  InboundCall)`). A handler that finished for good but must record a reason raises `va_edge.processor.EventSettled
  (reason)` (stored in `process_error`, attempts set to max via `mark_processed(settled_attempts=)`, never
  retried). The edge container now needs `telnyx` (factory) + `limiter` and `LOCAL_MASTER_KEY` (BYO keys):
  `edge_helpers.DbContainer`/`ProbeContainer` default to `offline_telnyx()`; real-container tests pass
  `local_master_key=secrets.token_hex(32)`. Router tests: `routing_helpers.routing_rig(database)` (FakeTelnyx
  in-process with `first_command_timeout=False`, recorded commands `rig.commands(ccid)`, `rig.number(org, config)`
  inserts a random +1628 number in the DB and assigns it to the sim account directly — the sim inventory is fixed,
  `rig.place`/`ring`/`route`). FakeTelnyx `transfer` works on a ringing incoming leg (answered when the target
  answers). `test_calls_recording.py::test_record_start_stop_saves_a_dual_channel_wav` flakes under `--cov` with
  `-n 8` on HEAD too (accelerated clock reaches the time limit).
- 2026-11-02 (P05-T12c): inbound accept = `va_edge.accept.InboundAccept(database, CallContextStore, starter, telnyx,
  *, publisher=, settings=AcceptSettings, clock=, metrics=)` (rules: specs/06 §7 Decision P05-T12c), wired by default
  in `va_edge.main.carrier_handlers(container, clock=, metrics=, settings=, acceptor=None)`; `acceptor=NO_ACCEPTOR`
  routes without one (`accept_unavailable`). The default needs `STREAM_TOKEN_SECRET` (`edge_helpers.edge_settings`
  fills a random one). Processor per-type budgets: `EventProcessor(budgets={"call.initiated": 5.0})`
  (`main.HANDLER_BUDGETS`). Shared pieces moved out: `va_common.waits` (`within`/`wait_until_set`; va_voice
  re-exports), `va_telnyx.streams` (`media_stream_url`, `stream_params`, `RECORDING_FIELDS`), `va_common.checks.
  start_workflow`/`terminate_workflow`/`WorkflowStartError` (the api starter uses them), `va_domain.call_workflow.
  max_duration_seconds`, `va_db.call_rows.call_row(call_id, org_id, config, NewCall(...), extra_config)` (the api
  dispatch uses it), `va_db.repositories.ContactRepo.upsert_phone`. `CallWorkflowInput(direction="inbound",
  max_duration_s=)` runs CallWorkflow without prefetch/dial (30 s answer backstop). `calls.config.maintenance_message`
  (`va_domain.numbers.MAINTENANCE_MESSAGE_KEY`) → `CallContext.maintenance_message`. Tests:
  `apps/telephony_edge/tests/support/accept_helpers.accept_rig(database, clock=, telnyx=, contexts_redis=)`
  (`RecordingStarter`, `GatedClock.fire(s)` for budget timeouts, `answer_failing_factory`, `broken_redis`;
  `AcceptRig.org()` adds a voice named like the default `default_voice`, which inbound resolution needs).
  FakeTelnyx only places calls from E.164 numbers: rewrite the stored envelope for anonymous callers. The sim's
  accelerated clock dates lie outside the test DB's `call_events` partitions, so edge apps on `SimTimeClock` fail
  to write their own event rows (assert the call/leg state instead); `test_sim_contract.signed_at(webhook)` gives
  a clock that verifies a later webhook's signature.
- 2026-11-02 (P05-T12d): inbound maintenance message = `va_media.maintenance.maintenance_takeover(context)` (a
  `Takeover("inbound_rejected", text, silence_ms=0, max_wait_s=0)` applied by `CallRuntime._converse` before
  `pipeline.run()`; specs/16 §4 Decision P05-T12d). The runtime sim builder moved to
  `apps/media_worker/tests/support/runtime_rig.py` (`sim_runtime(state, urls, org_settings=, phrase_redis=,
  persistence=, database=, end_redis=, messaging=)`, `RecordingRuntime`, `StaticOrgSettings`, `MODELS_DIR`, `WAIT_S`);
  `media_helpers.call_context`/`FakeContextLoader` take `maintenance_message` and `direction`, and
  `test_runtime_sim.Rig.call(maintenance_message=)` makes an inbound maintenance call. Edge + media + sims in one
  process: `support/inbound_flow.inbound_flow(media_db_url)` (FakeTelnyx webhooks → `EdgeForwarder` → running edge
  app with its default acceptor; Temporal = `workflow_fakes.SignalRecorder`; served media worker with the production
  `db_context_loader` over `MediaDbContainer`; ONE shared fakeredis), `flow.org()` (voice = FakeElevenLabs'
  seeded `VOICE_ID`), `flow.number(org, config)`, `flow.caller(*steps)` (inbound scenario bound to `CALLER`, no
  `answer`), `flow.ring(to, scenario)`. Shared edge helpers: `routing_helpers.own_number`,
  `accept_helpers.add_default_voice`. The sim drops a call's stream object after hangup and `CommandEntry` has no
  body: assert command names, not stream URLs.
- 2026-11-02 (P05-T13): cold transfer = `va_media.transfer.ColdTransfer` (status `transferring` via `DbTransferStatus`,
  `call.transfer_started`, Telnyx `transfer` with `target_leg_client_state` leg `transfer_target`, outcome from
  `CarrierWatch.transfer_events`; rules: specs/06 §10.1 Decision P05-T13) run by `MediaActions(transfer=)`; the runtime
  binds `MediaActions.bind(on_transferred=take_over(Takeover("transferred")), on_transfer_failed=brain note +
  CallPipeline.say(msg))`. `CallPipeline.say(text)` queues a static line after the current reply/action. The end
  routine's `transferred` end sends `streaming_stop` (no record/hangup/slot release); `CallWorkflow` waits in phase
  `transferred` for the primary hangup. `in_progress` is enterable from `transferring` now: carrier `call.answered`
  passes `from_=RINGING_PHASE`. Tests: `test_transfer.py` (fakes), `test_transfer_flow.py` (`inbound_flow` +
  `flow.caller(..., transfer_target="{answer_after_ms: 500}")`, `flow.forwarder.wait_for(predicate)` +
  `edge_app.state.processor.join()` to settle edge processing, `RecordingRuntime.end(timeout_s)` for long calls).
  Time-skipping gotcha: cancelling a dispatched watch that is failing (`watch="fail"`) or completing wedges the test
  server (`ACTIVITY_UNKNOWN`); deliver the decisive signals from the `dial` activity so the watch is never dispatched.
- 2026-11-02 (P05-T14): object storage = `va_common.storage` (`S3Settings` mixed into worker settings,
  `open_object_store(settings)` → `ObjectStore.upload_file/exists/delete/presign_get`, `sse_for(crypto)` = SSE-KMS
  only with `KMS_PROVIDER=aws`, `recording_key`/`recording_variant_key` = `{org}/{yyyy}/{mm}/{call}[...]`,
  `signed_url(..., now=, ttl_s=)`, `RECORDING_URL_TTL_S` 7 d / `PLAYBACK_URL_TTL_S` 15 min; rules: specs/06 §11
  Decision P05-T14). Recording copy = activity `store_recording` (`va_workflows.activities.recording.
  RecordingActivities`, inputs in `recording_inputs`), polled on the post-call queue by an activities-only
  registration (`WorkerActivities.recording`, `postcall_queue()`). The edge also signals `postcall-<id>`
  `recording_saved` for `call.recording.saved`; `TelnyxClient.delete_recording(id)` (404 → False; FakeTelnyx
  `DELETE /v2/recordings/{id}`). Tests: `packages/va_common/tests/minio_container.running_minio()` (bitnamilegacy
  MinIO, random creds, `Minio.settings()`, `make_bucket`; one container per module) and
  `packages/va_workflows/tests/support/recording_rig.py` (`MemoryS3` fake client behind a real `ObjectStore`,
  `serve_recording(r, wav)` stores a FakeTelnyx WAV, `saved_input(call, id, **payload_fields)`, `wav_bytes`).
  The `httpx` logger now defaults to WARNING (`LOG_LEVEL_HTTPX=INFO` restores request lines): its INFO lines
  carry full URLs, presigned signatures included.
- 2026-11-02 (P05-T15): call audio options. `PipelineConfig.noise_cancellation` (default false; the media runtime
  maps the call value, default true) wraps the leg's VAD in `va_voice.pipeline.GatedVad` (SpectralGate, events
  shifted back 16 ms) and `stt_config` sets Scribe `filter_background_audio`. `CallRuntime(background=
  BackgroundLibrary(...))` is required: `open_runtime` uses `BACKGROUND_ASSETS_DIR` (`assets/audio`, cwd-relative),
  tests use `runtime_rig.ASSETS_DIR`; `va_media.runtime.background_mixer` builds the leg mixer (missing asset →
  `media.background_unavailable`, no bed). Every runtime sim call now plays the `static` bed (continuous idle
  frames on the agent track): assertions on agent-track digital silence need `background_track="none"`.
  Measure agent-track windows from where speech actually ended (`streamed.sim_call().mark_echoes[-1].track_ms`,
  timeline `marks`), never fixed offsets after the answer: under load the opener starts late.
  FakeTelnyx agent speech = frame RMS > 1024 (was 256; the static bed is ≈ 330); the -22 dBFS office/cafe/
  restaurant beds read as agent speech, so `wait_agent_speech` scenarios must not use them. The default resolved
  config waits up to 6 s for a callee greeting: pass `wait_for_greeting=False` when a test needs the opener at
  once. Runtime sim tests read barge-ins from `json_log_stream()` (`pipeline_barge_in`, `media.transport_closed`
  `clears`); FakeElevenLabs STT session records carry `filter_background_audio`.
- 2026-11-02 (P05-T16a): Call Object = `va_domain.call_object` (`CallObject` extends the list item `CallListItem`;
  `mapping` has `bland_status`, `BLAND_STATUS_FILTER`, `CALL_ENDED_BY`, `minutes`/`dollars`, `latency_summary`) read by
  `va_db.call_object.load_call_objects` / `load_call_list_items(session, calls, recording_urls=)` (one query per child
  table; reuse for `call.completed`). Call reads: `va_api.services.call_read` (`get_call`, `list_calls`,
  `recording_urls`), filters `routers/calls_read_schemas.CallListQuery` (a FastAPI query model: the cross-tenant
  walker sees it as a required param, so `calls_list` has a `REGISTRY.register_request`). `ApiSettings` mixes in
  `S3Settings`; the container exposes `object_store` (presign only). Tests: `SessionContainer.presigner`
  (`call_fakes.FakePresigner`, URLs `{PRESIGN_HOST}/{bucket}/{key}?expires=<s>`, `.fail`); settings come from
  `request.app.state.settings` (the fake container has no `settings`). `json_log_stream()` is not a context manager.
  Spec Decisions: specs/19 §4 and specs/05 §6 (P05-T16a). T16b's literal `GET /v1/calls/active` must be declared
  before `GET /v1/calls/{call_id}`.
- 2026-11-02 (P05-T16b): call artifact routes on `calls_router` (`GET /v1/calls/active` declared before
  `/{call_id}`, `/{call_id}/recording|transcript|events`), work in `va_api.services.call_artifacts` (shares
  `call_read.ReadDeps` — now with `redis` — and `call_read.owned_call`), schemas in
  `routers/calls_detail_schemas.py`. `va_db.call_states.ACTIVE_STATUSES`; `va_db.call_events_read.load_call_events`
  (seq paging); `va_db.call_object.load_transcripts`. Transcript downloads are `va_domain.call_object.to_srt`/
  `to_vtt` (golden files `packages/va_domain/tests/fixtures/transcripts/*.srt|vtt`, compared byte for byte;
  edit them with `printf`, never let an editor add CRLF). Recording `channel=mixed` = `mixed` variant if
  `ObjectStore.exists`, else the raw dual-channel key; `call_fakes.FakePresigner` answers `head_object` from
  `existing` (`fail_head` → 500). Routes with a FastAPI query model need `REGISTRY.register_request` in
  `cross_tenant_factories.py`; shared call-read seeding is `tests/support/call_read_helpers.an_org`.
- 2026-11-02 (P05-T18a): call metering = `va_billing.metering.finalize_call(session, org_id, call_id, now=)` (in the
  caller's `tenant_session`; pure planning `plan_call_usage(CallUsageFacts)`, `pricing_from_snapshot`,
  `CallNotEndedError` for non-terminal calls; rules: specs/24 §4 Decision P05-T18a). Usage rows insert `ON CONFLICT DO
  NOTHING ... RETURNING`, so only new rows hit the ledger. Tests: `packages/va_billing/tests/test_metering_db.py`
  (`funded_org` grants the start balance through the ledger so `Σ ledger == balance`); `call_rows.seed_call` now
  accepts an explicit `id=` (needed when a hold must reference the call before it exists).
- 2026-11-02 (P05-T18b): `PostCallWorkflow` = `va_workflows.post_call` (pure `plan.planned_steps`/`remaining`, result
  `PostCallResult`; rules: specs/18 §1 Decision P05-T18b) + `va_workflows.activities.post_call.PostCallActivities(
  database=, publisher=, clock=, storage=RecordingStorage|None, step_ms=)` (inputs/policies in `post_call_inputs`);
  registered on the post-call queue by `va_worker.activities.main_registrations` (`POSTCALL_WORKFLOWS`). Step names are
  `va_domain.post_call.POSTCALL_STEPS`; completed steps go to `calls.config.postcall_steps` via
  `va_db.post_call_steps.mark_step`. `RecordingStorage` moved to `va_common.storage` (with
  `signed_recording_url`, which never raises). `build_container(settings, metrics=None)`: `create_app` passes the probe
  registry. Tests: `packages/va_workflows/tests/support/postcall_rig.py` (`FakePostCallActivities` scripts each
  activity, `send={activity: [recording events]}` signals from inside it, `run_post_call` → `PostCallRun.timers()`; the
  30 min budget timer is always the first TIMER_STARTED); `recording_rig.MemoryS3` presigns (`PRESIGN_HOST`,
  `fail_presign`). The worker e2e test now runs the real post-call child to completion (a dialed canceled call is charged
  the attempt fee). A step wrapper that catches `ActivityError` would swallow a budget cancel: `_begin`/`_fail` re-raise
  `CancelledError` once the budget is spent.
- 2026-11-02 (P05-T17a): `make up PROFILE=e2e` (or `apps`) builds and starts the app containers (api 8000, edge 8100,
  media 8200; worker/webhooks/events-sink probe on 9300/9400/9500 inside the network only). `dev_up.sh` stages:
  deps → minio-init → sim key → `telnyx_bootstrap.py --write-env` with `EDGE_PUBLIC_HOST=edge:8100` → `run --rm
  --build migrate` → `up --wait --no-deps` of the apps (`--force-recreate` when the sim key changed). Containers get
  service-name URLs from the `x-py-env` anchor and `.env` through `.env.compose` (`scripts/compose_env.py`: Compose
  reads `KEY=  # comment` as the comment text, which broke every blank secret/DSN) and read the sim key from the `simkeys`
  volume (`TELNYX_PUBLIC_KEY` is blanked). A host-run api/edge/media collides with the container ports: `docker
  compose stop api edge media` first. Edge webhook URLs are `http://` in `local`/`test` (`va_telnyx.webhook_scheme`).
  Migrations are packaged (`va_db/migrations`), so tests find revisions at `migration_helpers.VERSIONS`.
- 2026-11-02 (P05-T17b): docker e2e = `tests_e2e/` (specs/30 §5 Decision P05-T17b). Flows `tests_e2e/test_<flow>.py`
  (`pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]`) take the `harness` fixture (`tests_e2e/conftest.py`;
  the session `stack` fixture exits with code 3 when api/edge/media/sim do not answer). Harness =
  `tests_e2e/support/e2e_harness.Harness`: `create_org(numbers=, settings=)` (calls `POST /internal/test/orgs`,
  local/test only; org voice Karen = FakeElevenLabs `KAREN_VOICE_ID`, random +1628 caller ID), `load_scenario(name,
  number=)` (scenario `<name>-<run_token>` on a fresh number, rule files from `scenarios/rules/` with `{{run}}`/
  `{{marker}}` substituted, applies the scenario `profile`, remembers the TTS log position), `send_call`,
  `wait_complete` (polls `queue_status=complete`; no webhooks until phase 06), `events`, `only_call`, `stop_call`,
  `sim_call(number)`/`timeline(ccid)`/`wait_sim`/`wait_until_sim_ms`, `tts_texts()` (since the scenario was loaded),
  `llm_requests(call_id)`, `order_inbound_number()` (released at teardown), `inbound`, `recording_wav` (MinIO via
  `localhost` + original `Host`). Never call `/sim/reset` from e2e (drops the bootstrapped Call Control app). The pure
  evaluator `e2e_assertions.evaluate(assertions, call, events, timeline)` and the scenario/rule rewriting are
  unit-tested in `tests_e2e/unit/` (in `testpaths`, so `make test` runs them; `tests_e2e/test_*.py` only run via
  `make e2e`). The first call after a stack (re)build is ~250 ms slower (cold path); outbound_basic measures p95
  ≈ 1 030 ms and overhead ≈ 130 ms on a warm stack.
- 2026-11-02 (P05-T17c): e2e voicemail/IVR/transfer flows (specs/30 §5 Decision P05-T17c). FakeTelnyx timeline
  `first_agent_speech_ms` = first played-out agent frame above the agent-speech level (evidence for "agent spoke
  after the beep"). Harness: `remote_hangup(ccid)` (far end hangs up, needed after a cold transfer: target legs run
  no steps and the primary's script stops with the stream), `iso_ms(at)` for summary command/event/DTMF deltas
  (wall-clock ms under the realtime e2e sim). `ivr_mode=true` disables AMD, so machine answers yield no
  `call.machine.*` events. `EVENT_MESSAGES` now knows `call.voicemail`/`call.transfer_started` (fixed messages
  imported from `va_media`); `call.amd` shares the `voicemail` category, so select voicemail rows by message;
  `call.transferred` has no fixed message, check its `transfer` row directly.
- 2026-11-02 (P05-T17d): FakeTelnyx timeline `clears` (virtual ms of each agent `clear`); caller voice onset =
  utterance `start_ms` + `va_sim.audio.MARKER_MS` (180 ms FSK preamble). Pure timing helpers:
  `tests_e2e/support/e2e_timing.py` (`utterance_window`, `clears_in`, `first_clear_after`, `interrupt_events`,
  `assistant_before`); shared long opener `e2e_texts.LONG_FIRST_SENTENCE` (300 chars). Harness `kill_media()` /
  `restart_media()` (docker CLI via compose labels, project `E2E_COMPOSE_PROJECT` default `voiceagents`) and
  `wait_call(org, call_id, done, what=)`. Crash timing: a killed media worker's stream failure loses to the owner TTL
  (15 s > 5 s grace), so the heartbeat watch ends the call `worker_crash` 20-35 s after the kill.
- 2026-11-02 (P05-T17e): platform underrun = speech frame sent after Telnyx drained while the previous frame on the leg
  was speech of the same utterance (specs/07 §9 Decision P05-T17e); `OutboundStats.underruns`, hook
  `CallPipeline(on_underrun=)` (harness `harness(on_underrun=)`), the media runtime increments
  `va_audio_underruns_total`; `pipeline_ended` logs `outbound_frames`/`late_frames`/`max_lateness_ms`/`underruns`.
  `tests_e2e/test_concurrent_calls.py` = 50 simultaneous calls on the one media process (scale plan, $500 via
  `create_org(plan_id=, dollars=)`); `Harness.media_metrics()` / `watch_active_calls(stop)` and pure
  `tests_e2e/support/e2e_metrics.py` (`parse_metrics`, `sample`, `sample_sum`, `histogram_quantile(before, after,
  name, q)`). Compose Postgres runs with `max_connections=300`: Temporal holds ~32 and each app 2 pools x (10 + 10
  overflow); the default 100 ran out under 50 calls (`too many clients already`). `runtime_rig.SimRuntime.metrics`
  / runtime sim `Rig.metrics` expose the runtime's catalog.
- 2026-11-02 (P05-T17e1): the 50-call flow carries marker `capacity` (registered in pyproject; since P05-T17e2
  `make e2e` runs it too), `make e2e-capacity` runs it alone (`E2E_CONCURRENT_CALLS` 1..50, parsed by
  `tests_e2e/support/e2e_capacity.concurrent_calls`). Copies of one scenario in a test need
  `load_scenario(name, instance=n)` (same id otherwise: they replace each other), and many concurrent pollers need
  `finished(..., poll_s=1.0)` (0.25 s x 50 hits the plan's 100 req/s `default` bucket). Baseline: 50 calls overhead
  p50 335 ms / 667 underruns / lag p99 ≤ 150 ms; 10 calls p50 92 ms / 2 underruns / lag ≤ 5 ms (so underruns also
  appear without CPU pressure). The flow takes ~45 s.
- 2026-11-03 (P05-T17e2): VAD inference is batched pod-wide: `VadModel.infer_batch(samples (B, 576), state (2, B,
  128))` and `VadExecutor.infer(model, samples, state)` (coalesces the windows queued in one loop turn, ≤ 64 per
  batch, ≤ `threads` batches in flight; `.batches`/`.windows`/`.in_flight` for tests). Fakes implement
  `infer_batch`: `packages/va_voice/tests/support/vad_fakes.ScriptedModel` (probability script indexed by the row's
  state value, so it stays per stream inside shared batches; `on_batch`, `fail_next`, `batch_sizes`). `InboundPath`
  wraps the VAD in `GatedVad(vad, window_samples=512)` (gate + VAD once per VAD window) and its stream origin is
  `min(arrival - 20 x k)` (a startup backlog no longer delays a call's whole timeline: that per-call constant was
  most of the 50-call overhead). Media worker and sim run on uvloop (`serve(..., loop_factory=uvloop.new_event_loop)`
  / `run_main(..., loop_factory=)`); `va_media.main.tune_gc()` freezes startup objects (tests must restore
  `gc.unfreeze()` + thresholds). Measuring: py-spy WITHOUT `--nonblocking` pauses the target at every sample and
  inflated the 50-call overhead by ~50-100 ms; profile with `--nonblocking` and take gate numbers from unprofiled
  runs. Overhead varies run to run; compare several runs, and split sim v2v minus the turn's `e2e_ms` ("outside"
  the media pipeline) from `e2e_ms` minus vendor stages ("inside") using the `call.latency` `E2E:` summaries
  (`call_events` rows, `psql -U va -d va` in the postgres container). Restarting only the `sim` container rotates
  its key: run `scripts/dev_up.sh e2e` afterwards, not `docker compose up sim`.
- 2026-11-03 (P05 exit gate): on the EC2 Ralph runner the agent shell PATH may lack `uv` (it lives in
  `/home/ralph/.local/bin`); `make check`/`make e2e` then fail at once with `uv: command not found` (exit 127). Prepend
  that dir to PATH before running the gates.
- 2026-11-03 (P06-T02): webhook outbox = `va_events.DbWebhookRouter(database)` (`route(event, session=)` /
  `route_many(events, session=)`; pass the caller's session for the transactional outbox), pure planning in
  `va_events.webhook_routing` (`RoutingFacts`, `plan_deliveries(event, facts, DeliveryPolicy(hipaa=, call_format=))`),
  bodies in `va_events.webhook_payloads` (`live_body`, `batch_body`, `envelope_body`, `strip_phi`, `iso_ms`); the §2
  `webhook_events` keys are `va_events.WEBHOOK_KEY` (`test_catalog` parses the last spec column). Rules: specs/19 §3
  Decision P06-T02. Seeding a call's metadata in tests: `seed_call(..., metadata_={...})` (`metadata=` silently sets
  the declarative `MetaData` attribute instead of the column).
- 2026-11-03 (P06-T03): webhook dispatcher = `va_webhooks.dispatcher.Dispatcher` (`run(stop)` poll loop, `run_once()`
  test seam, `refresh_backlog()`) over `Deliverer` (`delivery.py`: `Outgoing`, `SendResult`, pure `decide`),
  `SigningSecrets`, `DestinationPolicy`, `EndpointDisabledNotifier`; `container.build_container(settings, metrics=,
  resolver=, limits=)` needs `LOCAL_MASTER_KEY` (local KMS). Rules: specs/19 §8 Decision P06-T03. Customer URLs go
  through `va_common.safe_http.safe_client(settings, timeout=, allow_private=, resolver=)` (connect-time DNS pinning;
  refusals raise `UnsafeDestinationError` out of the request, not an httpx error). New event type
  `call.webhook_delivery` (dispatcher only). Tests: `apps/webhook_dispatcher/tests/support/` (`webhook_receiver.Receiver`
  real-socket receiver with scripted `Reply(status, hold=)`, `wait_for(predicate)`, per-host `in_flight`;
  `signature_ok(received, secret)`; `dispatch_helpers.dispatch_rig(url, ...)` empties the outbox, provisions orgs with a
  local-KMS envelope, fake DNS maps `hooks.test`/`a.hooks.test`/`b.hooks.test` to 127.0.0.1; rigs sharing orgs need one
  `master_key`); session fixture `dispatcher_db_url`. Keep the rig's 5 s timeouts for normal deliveries: 0.5 s ones
  timed out under coverage + xdist load.
- 2026-11-03 (P06-T04a): webhook signing secrets = `va_db.webhook_secrets` (`active_signing_secrets(envelope, org,
  now=)` current first + previous while `now < prev_expires_at`, `secret_info`, `rotate_signing_secret(session,
  envelope, org_id, now=)` in a `global_session`: org lock, active data key read via `use_org` in the same
  transaction, `encrypt_with_key`, never `envelope.encrypt` inside an open session; `ROTATION_OVERLAP` 24 h). Routes
  `GET /v1/webhooks/signing-secret` and `POST .../rotate` live on `routers/webhooks.webhooks_router` (in `ROUTERS`;
  P06-T04b adds the endpoint routes there). The dispatcher's `SigningSecrets` caches no longer than the previous
  secret's expiry. Dispatcher tests: `dispatch_rig(...).envelope` for rotating a rig org.
- 2026-11-03 (P06-T04b): webhook delivery code shared by the dispatcher and the api lives in `va_events`
  (`webhook_signing`, `webhook_schedule`, `webhook_delivery.Deliverer/Outgoing/SendResult/decide`); the real-socket test
  receiver moved to `packages/va_events/tests/support/webhook_receiver.py`. Endpoint/delivery routes are
  `va_api/routers/webhook_endpoints.py` (side-effect import in `main.py`, on `webhooks_router`) over tenant
  `va_db.repositories.WebhookEndpointRepo`/`WebhookDeliveryRepo`; the test send is
  `va_api.services.webhook_test_send.send_test_event` with the container's `webhook_deliverer` (`SessionContainer`
  builds one over `host_resolver` when the app opens it). `OutboxRepo` records outcomes only on `delivering` rows
  (`DeliveryCancelledError` otherwise), so tests must claim (or seed `delivering`) before `mark_*`. Endpoint URL checks:
  `webhook_policy.check_webhook_url(url, org=, settings=, field=, resolver=)`. Never name a test helper `test_*`
  (pytest collects it) and never name a src class `Test*` (an imported one is collected too). A test creating more
  than a handful of resources through the api hits the plan's frozen-clock rate limit (429): seed rows directly.
- 2026-11-03 (P06-T05): events sink = `va_events_sink.sink` (`run_sink(redis, connect, schema, clock=, metrics=,
  config=SinkConfig(...), started=)`, one `StreamSink` per stream: `cycle()` test seam, `wait_flushes(n)`), pure
  mapping `va_events_sink.rows.rows_for_event` (`TABLE_COLUMNS` = specs/03 §12 order, `RowError.reason`), inserts
  `writer.insert_table` (+ `is_transient`/`is_rejected`), lag `va_events_sink.lag`; parking `va_events.DeadLetter`
  (`events:dead`), `StreamConsumer(..., dead_letter=)` + `read_own_pending()`; internal type `billing.usage`
  (`va_events.usage_event(UsageEventData, ...)`, `INTERNAL_TYPES` never routed). Rules: specs/19 §1 Decision
  P06-T05. `create_app(sink=None)` disables the consumer (tests with `FakeContainer`, which has no `redis`).
  Gotcha: fakeredis returns `[]` at once from a blocking `XREADGROUP` (it never waits), so a read loop that does not
  yield starves the event loop; the sink yields once after an empty read. `XPENDING` on a missing group raises
  `NOGROUP`. `ch_support.FakeClickHouse` now records `insert`s (`insert_failures`, `reject(table, rows)`,
  `rows_of(table)`, `inserted` event). The media `TurnRecorder` holds the assistant `call.turn` until the next
  callback so the reply's latency rides on it as `data.latency`.
- 2026-11-03 (P06-T06a): call event SSE = `va_api.services.call_stream` (`open_call_stream(StreamDeps(database,
  live_hub, clock), ctx, call_id, last_event_id)` checks + subscribes; `CallStream.frames()` replay → tail, gap
  hold via pure `call_stream_order.SeqOrder`; rules: specs/19 §9 Decision P06-T06a) behind
  `GET /v1/calls/{call_id}/events/stream`; responses `va_api.sse.EventStreamResponse(frames, on_close)` (Starlette
  never closes an abandoned body iterator, so cleanup goes in `on_close`; a `Response` subclass needs a
  `status_code` parameter in `__init__` or FastAPI's OpenAPI generator crashes). Pod-wide pub/sub fan-out =
  `va_events.LiveHub` (container `live_hub`; `SessionContainer` builds one on `SystemClock` because the frozen
  clock times out confirmations). `call_events.event_type` (migration 0007) is set by `row_for`; `row_body(row,
  metadata)` == `live_body(event, metadata)`. Stream tests drive `CallStream.frames()` on a `ManualClock` set to
  real now (partitions); an in-progress stream never ends, so httpx `ASGITransport` (which buffers the body) only
  suits ended streams — live ones go over `service_helpers.serving(app)`. Cross-tenant `register_request(op,
  headers=...)` exists (the stream owner control sends a bad `Last-Event-ID` → 400). `test_openapi.STREAM_OPERATIONS`
  exempts `text/event-stream` operations from the JSON example rule.
- 2026-11-03 (P06-T06b): the replay/tail/gap core of the call live streams is `va_api.services.call_tail`
  (`call_metadata` → `subscribe_tail(..., types=, heartbeat_s=)` → `CallTail.items()` yielding `TailItem` or `None`
  for a due heartbeat); `call_stream.CallStream` (SSE) and `transcript_ws.serve_transcript` (WebSocket) only render
  it. Transcript tokens: `va_api.services.transcript_tokens.TranscriptTokens` (`transcript:token:{sha256}`, 60 s).
  WebSocket routes (`@router.websocket`) are `APIWebSocketRoute`s: not in OpenAPI, not walked by the cross-tenant or
  audit-marker suites, and an `AppError` raised in one is NOT rendered by the exception handlers: catch it and
  refuse with `websocket.send_denial_response(error_response(websocket, ...))` (uvicorn advertises
  `websocket.http.response`; `error_response` accepts any `HTTPConnection`). WS tests: real uvicorn via
  `service_helpers.serving(app)` + `websockets.asyncio.client.connect`; a refused handshake raises
  `websockets.exceptions.InvalidStatus` (`.response.status_code`, `.response.body`). A client's close races the
  server's cleanup: sync on a wrapped module `_log` (see `_ClosedLog` in `test_transcript_ws.py`), not on the close.
- 2026-11-03 (P06-T06c): org event SSE = `GET /v1/events/stream` (`routers/events.py`, tag `Webhooks`) over
  `va_api.services.org_stream` (`parse_types`, `open_org_stream(OrgStreamDeps, ctx, types)`, `OrgStream.frames()`;
  bodies = `content_body` + `envelope_body` (+ `strip_phi` for hipaa orgs), metadata via
  `va_events.webhook_router.call_metadata`/`batch_facts`) and the pod-wide `va_events.StreamTailHub(redis)` (one
  XREAD loop over the five streams, started by the first subscribe, stopped by the last unsubscribe; container
  `stream_tail`, also on `SessionContainer`). The bounded subscriber queue is the generic
  `va_events.Subscription[T]` (`SubscriptionClosedError`; `LiveSubscriptionClosedError` is an alias). Tests:
  `live_stream_helpers.FrameReader` + `started(reader)` read raw SSE frames of any stream; to know a hub reader
  dispatched earlier entries, subscribe a probe org and wait for a marker event (the reader is sequential).
  Unbounded streams in the cross-tenant suite need an owner control that fails fast (`register_request(...,
  params={"types": "BAD!"})` + `register_owner_status(op, 400)`). OpenAPI tags must be one of `va_api.openapi.TAG_NAMES`.
- 2026-11-03 (P06-T07): every call-event producer routes webhooks atomically with its `call_events` row: callers that
  insert rows themselves call `EventPublisher.route(sequenced, session=session)` after `insert_rows` (api
  `persist_event`, worker `persist_events`, edge accept `_persist_event`); the media worker routes in the buffer
  flush (`DbCallEventWriter(database, router=DbWebhookRouter(database))`, events rebuilt by `va_events.event_of(row)`).
  Publishers of api/worker/edge (and the `SessionContainer`, `telephony_rig`, `accept_rig`, carrier test rigs) carry
  `router=DbWebhookRouter(database)`; the dispatcher and the media publisher never get one. `WebhookRouter` fakes
  must implement `route_many`. The media `TurnRecorder.started(media_format)` publishes `call.started` first, so
  call event filters by category `call` also see it: filter turns by `event_type == "call.turn"`. Event log items
  (`GET /v1/calls/{id}/events`) carry `type`; e2e `event_order_failures(event_types(events))` checks the causal order.
- 2026-11-03 (P06-T08): e2e webhook receiver = `tests_e2e/support/e2e_receiver.py` (fixture `receiver` →
  `ServedReceiver(receiver, url)`; `WebhookReceiver.script(path, Reply(500), event_type=...)`, `of(path, type)`,
  event-driven `wait_for(predicate, timeout_s=)`; pure `signature_failures(received, secret, now=)`) served in the
  pytest process on `E2E_RECEIVER_BIND` (0.0.0.0) / `E2E_RECEIVER_PORT` (0); containers reach it as
  `E2E_RECEIVER_HOST` (default `host.docker.internal`, mapped by `extra_hosts: host-gateway` on `api` and `webhooks`
  in `docker-compose.e2e.yml`; on the EC2 runner that is docker0 `172.17.0.1`, reachable from the compose network).
  After editing the override on a running stack: `docker compose -f docker-compose.yml -f docker-compose.e2e.yml
  --profile e2e up -d --no-deps --wait api webhooks`. Rotate the org secret first (only the rotate response shows the
  plaintext); harness `rotate_signing_secret`, `create_endpoint`, `send_endpoint_test` (never `test_*`: pytest
  collects it), `deliveries`/`wait_deliveries`. Pure body checks: `e2e_webhooks` (`live_body_failures`,
  `envelope_failures`, `retry_gap_ok`, `call_object_mismatches` ignoring per-read presigned `recording_url` and
  `recording_expiration`); `CALL_OBJECT_FIELDS` lives in `e2e_assertions`. The flow takes ~35 s (first retry 10 s).
- 2026-11-03 (P07-T01): pathway schemas = `va_engine.models.nodes` (`parse_node`/`dump_node`, `PathwayNode` union,
  `effective_transfer_number`) + `va_engine.models.graph` (`load_graph(nodes, edges, global_config)`, `dump_graph`,
  `graph_hash`, `schema_field(loc)` for `INVALID_INPUT` fields) + `va_engine.validation.validate_graph(graph, mode=,
  refs=GraphRefs(...), allow_http=, code_checker=)` / `validation_report` (rules: specs/11 §11 Decision P07-T01;
  `test_validation.py` parses the §11 table, so a rule/message change goes in the spec, `RULES` and `MESSAGES`
  together). Always dump with the module helpers (`exclude_unset`): that is what keeps Bland exports lossless.
  Fixtures `packages/va_engine/tests/fixtures/bland/*.json`, builders `tests/support/graph_helpers.py` (`node`,
  `edge`, `start`, `graph`, `fixture`, `FIXTURE_REFS`). Never run `ruff check --select RUF100 --fix`: it stripped the
  `# noqa: PLR0913` of untouched files (task_runner.py) again.
- 2026-11-03 (P07-T02): templating rendering never raises for any scope value (huge ints, non-str keys, NaN, date
  overflow; specs/11 §8 Decision P07-T02); only parsing raises `TemplateSyntaxError`. `render`/`render_url` take a
  `Template` or text (text goes through `va_engine.templating.cached_template`, a 1 024-entry LRU; call
  `cached_template.cache_clear()` before asserting `cache_info()`). Golden cases (incl. every §8 example; spec phone
  examples are drift-checked) live in `packages/va_engine/tests/fixtures/templating/golden.json`; the grammar fuzz
  strategies are in `tests/test_templating_props.py`.
- 2026-11-03 (P07-T03): engine state is versioned (`CURRENT_SCHEMA_VERSION` 2): write only with
  `va_engine.models.dump_engine_state` (by_alias, SMS key `from`), read with `load_engine_state(str | bytes | dict)`
  (upgrades via `state._UPGRADERS`, raises `EngineStateError(.version)` for bad JSON/version). A new field = version
  bump + one upgrader + a frozen `packages/va_engine/tests/fixtures/engine_state/vN.json` (never edit an old one; the
  canonical test compares parsed JSON, so prettier formatting is fine). `pending_actions` (discriminated by `kind`,
  `after_mark`) are drained with `EngineState.take_pending(mark)`.
