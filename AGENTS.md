# AGENTS.md — Operational Guide for Coding Agents

Read this file at the start of every iteration. Keep it short, current, and factual. Add a line whenever you discover a
command, convention, or gotcha that a future iteration would otherwise rediscover. Remove lines that become false.

## Commands

| Purpose | Command |
|---|---|
| Install everything | `make setup` |
| Toolchain preflight | `uv run --no-project --python 3.12 python scripts/check_toolchain.py` |
| Start infra + simulators | `make up` (apps too: `make up PROFILE=apps`) |
| Stop | `make down` |
| Migrate DB | `make migrate` |
| Seed dev data | `make seed` (prints dev API key `va_test_…` and dashboard login) |
| Format | `make fmt` |
| All gates | `make check` |
| Unit + integration tests | `make test` (single package: `uv run pytest packages/va_voice -q`) |
| Simulator end-to-end | `make e2e` (single flow: `uv run pytest tests_e2e -m e2e -k barge_in`) |
| Dashboard e2e | `make e2e-ui` |
| Dashboard dev server | `corepack pnpm --filter dashboard dev` (port 3000; set `NEXT_TELEMETRY_DISABLED=1`) |
| One TS package's gates | `corepack pnpm --filter <dashboard\|@voiceagents/widget\|@voiceagents/sdk> run <lint\|typecheck\|test\|build>` |
| Regenerate TS API types | `make openapi` then `corepack pnpm --filter dashboard gen:api` and `corepack pnpm --filter @voiceagents/sdk gen:api` |
| Regenerate OpenAPI | `make openapi` (commit `openapi/openapi.json`) |
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
- 2026-10-25 (P00-T02): `.env.example` mirrors the specs/02 §6 table one-to-one in table order (105 keys);
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
  unreachable; probe it with a plain socket (`test_redis_integration._docker_available`): `docker.from_env()`
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
