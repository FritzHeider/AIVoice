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
| Regenerate OpenAPI | `make openapi` (commit `openapi/openapi.json`) |
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
- 2026-10-25 (P00-T02): `.env.example` mirrors the specs/02 §6 table one-to-one in table order (104 keys);
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
