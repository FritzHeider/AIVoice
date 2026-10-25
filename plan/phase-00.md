# Phase 00 — Repository Bootstrap, Tooling, and Quality Gates

Legend: `- [ ]` open · `- [x]` done (append ` — <evidence>`). Each task: Spec refs · Depends on · Deliverables · Acceptance.

- [ ] P00-T01 Root workspace and toolchain configuration
  - Spec refs: specs/02-repo-layout-and-conventions.md (Repository root, Toolchain versions, Makefile targets, Python conventions)
  - Depends on: none
  - Deliverables: `pyproject.toml` (uv workspace members for all apps/packages/sdks, `[dependency-groups] dev`, ruff, mypy strict, pytest, coverage config), `Makefile`, `.gitignore`, `.editorconfig`, `package.json`, `pnpm-workspace.yaml`, `.nvmrc` (22), `.python-version` (3.12)
  - Acceptance: `uv sync --all-packages` and `pnpm install` succeed; `make lint` and `make typecheck` run (on an empty tree they pass); every Makefile target from specs/02 exists.

- [ ] P00-T02 `.env.example` and settings contract
  - Spec refs: specs/02-repo-layout-and-conventions.md (Environment variables)
  - Depends on: P00-T01
  - Deliverables: `.env.example` containing every variable in the table with its dev value and a comment; `scripts/tests/test_env_example.py` parsing the spec table and asserting parity.
  - Acceptance: test fails if a variable is added to the spec but not `.env.example` or vice versa.

- [ ] P00-T03 Placeholder detector gate
  - Spec refs: specs/02-repo-layout-and-conventions.md (`scripts/check_no_placeholders.py` behaviour)
  - Depends on: P00-T01
  - Deliverables: `scripts/check_no_placeholders.py`, `scripts/tests/test_check_no_placeholders.py` (positive and negative cases for every rule, including AST stub detection, Protocol/abstractmethod allowances, JSX `placeholder=` allowance, empty catch detection)
  - Acceptance: `make placeholders` exits 0 on the repo; each rule has at least one failing-fixture test.

- [ ] P00-T04 Spec reference and OpenAPI drift gates
  - Spec refs: specs/02-repo-layout-and-conventions.md (Quality gates)
  - Depends on: P00-T01
  - Deliverables: `scripts/check_spec_refs.py` (every `specs/<file>.md` path mentioned in `plan/*.md` and `prompts/*.md` exists; every `§N` reference paired with a spec file resolves to a heading starting with `## N.` or `## N ` in that file), `scripts/check_openapi_drift.py` (imports `va_api.main:app`, renders OpenAPI with sorted keys, compares to `openapi/openapi.json`), tests for both.
  - Acceptance: both scripts pass on the repo and fail on crafted fixtures.

- [ ] P00-T05 `va_common` core: settings, clock, ids, json, errors
  - Spec refs: specs/02-repo-layout-and-conventions.md (Python conventions, IDs, Money, Errors); specs/31-error-catalog.md (all)
  - Depends on: P00-T01
  - Deliverables: `BaseServiceSettings` (pydantic-settings), `Clock` protocol + `SystemClock` + `FrozenClock`, `ids.new_id()` (uuid7), `ids.to_public/from_public` for `TL-`/`KB-`/`PT-`, orjson helpers, `AppError`, `CODES` table, `micros` money helpers (`dollars_to_micros`, `micros_to_dollars_str`)
  - Acceptance: test parses specs/31 §3 table and asserts exact `CODES` parity; property tests (hypothesis) for id round-trips and money conversion.

- [ ] P00-T06 `va_common` logging and masking
  - Spec refs: specs/27-observability.md (logging); specs/02-repo-layout-and-conventions.md (Logging rule)
  - Depends on: P00-T05
  - Deliverables: structlog JSON configuration, contextvars binding helpers (`bind_call_context`), phone masking (`+1415***1234`), secret redaction processor (keys matching `*key*`, `*secret*`, `*token*`, `authorization`, `password`), `LOG_TRANSCRIPTS` enforcement (forced false in production)
  - Acceptance: tests capture log output and assert masking/redaction and field binding.

- [ ] P00-T07 `va_common.crypto` envelope encryption
  - Spec refs: specs/28-security.md (envelope encryption design)
  - Depends on: P00-T05
  - Deliverables: `KmsProvider` protocol, `LocalKmsProvider` (AES-256-GCM master key from `LOCAL_MASTER_KEY`), `AwsKmsProvider` (aioboto3 `generate_data_key`/`decrypt`), `encrypt_bytes/decrypt_bytes` producing versioned blobs (`v1|key_id|wrapped_dek|nonce|ciphertext`), key rotation re-wrap helper
  - Acceptance: round-trip tests, tamper detection test, AWS provider tested against a fake client implementing the boto3 KMS method signatures.

- [ ] P00-T08 `va_common` infrastructure helpers
  - Spec refs: specs/02-repo-layout-and-conventions.md (Core Python dependencies); specs/27-observability.md (tracing, metrics)
  - Depends on: P00-T05
  - Deliverables: `redis_client(settings)`, `http_client(settings, *, timeout, http2)` with OTel httpx instrumentation, `setup_tracing(service_name)`, `metrics` registry helpers and `/metrics` ASGI app factory
  - Acceptance: unit tests with fake Redis (testcontainers Redis in integration test) and respx for httpx.

- [ ] P00-T09 docker-compose infrastructure profile
  - Spec refs: specs/29-infrastructure.md (docker-compose); specs/02-repo-layout-and-conventions.md (Toolchain versions)
  - Depends on: P00-T01
  - Deliverables: `docker-compose.yml` (postgres pgvector, redis, clickhouse, temporal auto-setup, temporal-ui, minio + bucket-init job creating the three buckets, mailpit, otel-collector, prometheus, grafana) with healthchecks and named volumes; `infra/otel/collector.yaml`; `infra/prometheus/prometheus.yml`; `scripts/dev_up.sh`, `scripts/dev_down.sh`
  - Acceptance: `make up` brings all services to healthy; ports match AGENTS.md.

- [ ] P00-T10 ML model and audio asset scripts
  - Spec refs: specs/07-media-pipeline.md (Silero VAD model, background tracks); specs/02-repo-layout-and-conventions.md (`models/`, `assets/audio/`)
  - Depends on: P00-T01
  - Deliverables: `scripts/fetch_models.sh` (downloads Silero VAD ONNX from the official snakers4/silero-vad repository, computes sha256, writes/validates `models/MANIFEST.json`), `scripts/generate_audio_assets.py` (procedurally synthesizes office, cafe, restaurant, phone-static backgrounds and hold music as 8 kHz μ-law and 16 kHz PCM files, deterministic seed), generated files committed under `assets/audio/`
  - Acceptance: re-running produces byte-identical audio; manifest check fails on a corrupted model file.

- [ ] P00-T11 API skeleton with envelopes and OpenAPI export
  - Spec refs: specs/31-error-catalog.md (Envelope matrix, Implementation); specs/01-architecture.md (api service)
  - Depends on: P00-T05, P00-T06, P00-T08
  - Deliverables: `apps/api/src/va_api/main.py` (`create_app()`), request-id middleware, CORS config, `install_handlers` rendering legacy vs envelope errors via `x-envelope`, validation error re-rendering to 400, `/healthz`, `/readyz`, `scripts/export_openapi.py`, `openapi/openapi.json`
  - Acceptance: tests for both envelopes, 400 validation shape, `X-Request-Id` echo/generation; `make openapi` + drift check pass.

- [ ] P00-T12 Service skeletons with health and graceful shutdown
  - Spec refs: specs/01-architecture.md (Planes and services); specs/27-observability.md (health/readiness)
  - Depends on: P00-T08, P00-T11
  - Deliverables: `apps/telephony_edge`, `apps/media_worker`, `apps/worker`, `apps/webhook_dispatcher`, `apps/events_sink`, `apps/sim` packages each with `Settings`, `main` entry, lifespan management, `/healthz`, `/readyz`, `/metrics`, SIGTERM handling
  - Acceptance: tests start each app in-process (asgi-lifespan) and assert health/readiness behaviour including 503 when Redis is unreachable.

- [ ] P00-T13 TypeScript workspace scaffolds
  - Spec refs: specs/02-repo-layout-and-conventions.md (TypeScript conventions); specs/25-dashboard.md (design tokens, layout); specs/22-web-widget.md (embed script); specs/26-sdks-cli-mcp-docs.md (TypeScript SDK)
  - Depends on: P00-T01
  - Deliverables: `apps/dashboard` (Next.js 15, Tailwind 4, shadcn/ui, tokens, root layout, `/status` page), `apps/widget` (Vite lib build, `init()` mounting Shadow DOM), `sdks/typescript` (client constructor + `health()`), ESLint/Prettier/tsconfig strict for each, Vitest tests
  - Acceptance: `pnpm -r lint typecheck test build` passes.

- [ ] P00-T14 CI workflow and pre-commit
  - Spec refs: specs/29-infrastructure.md (CI/CD); specs/02-repo-layout-and-conventions.md (Quality gates)
  - Depends on: P00-T03, P00-T04, P00-T11, P00-T13
  - Deliverables: `.github/workflows/ci.yml` (uv + pnpm caching, service containers for Postgres/Redis/ClickHouse/MinIO, `make check`), `.pre-commit-config.yaml` (ruff, ruff-format, placeholder check, prettier)
  - Acceptance: workflow YAML validates with `actionlint` (run via `uvx`/binary in a test or documented script) and the same commands pass locally.

## Phase exit gate
`make setup && make up && make check` passes; `docker compose ps` all healthy; `make openapi` clean.
