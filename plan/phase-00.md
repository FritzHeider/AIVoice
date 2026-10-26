# Phase 00 — Repository Bootstrap, Tooling, and Quality Gates

Legend: `- [ ]` open · `- [x]` done (append ` — <evidence>`). Each task: Spec refs · Depends on · Deliverables · Acceptance.

- [x] P00-T01 Root workspace and toolchain configuration — lint/typecheck/fmt/test/test-ts exit 0 (22 passed, 100% cov on scripts/check_toolchain.py); uv sync --frozen + pnpm install --frozen-lockfile ok; all 16 specs/02 §3 targets present; placeholders pending P00-T03
  - Spec refs: specs/02-repo-layout-and-conventions.md (Repository root, Toolchain versions, Makefile targets, Python conventions)
  - Depends on: none
  - Deliverables: `pyproject.toml` (uv workspace members for all apps/packages/sdks, `[dependency-groups] dev`, ruff, mypy strict, pytest, coverage config), `Makefile`, `.gitignore`, `.editorconfig`, `package.json`, `pnpm-workspace.yaml`, `.nvmrc` (22), `.python-version` (3.12)
  - Acceptance: `uv sync --all-packages` and `pnpm install` succeed; `make lint` and `make typecheck` run (on an empty tree they pass); every Makefile target from specs/02 exists.

- [x] P00-T02 `.env.example` and settings contract — 104 vars mirrored in spec order; fmt/lint/typecheck exit 0; 65 passed, 99.64% cov (env_spec.py 99%); parity fails both directions (test_parity_detects_variable_added_to_{spec,env}_only); radon avg A 3.29; jscpd 0 clones; pip-audit + pnpm audit clean; placeholders gate pending P00-T03
  - Spec refs: specs/02-repo-layout-and-conventions.md (Environment variables)
  - Depends on: P00-T01
  - Deliverables: `.env.example` containing every variable in the table with its dev value and a comment; `scripts/tests/test_env_example.py` parsing the spec table and asserting parity.
  - Acceptance: test fails if a variable is added to the spec but not `.env.example` or vice versa.

- [x] P00-T03 Placeholder detector gate — make lint/typecheck/placeholders/test exit 0 (0 findings; 176 passed, 99.81% total cov, checker 100%); every rule has failing + allowed fixtures; git discovery covers tracked+untracked-not-ignored; spec §9.1 Decision (a–h), DEC-002
  - Spec refs: specs/02-repo-layout-and-conventions.md (`scripts/check_no_placeholders.py` behaviour)
  - Depends on: P00-T01
  - Deliverables: `scripts/check_no_placeholders.py`, `scripts/tests/test_check_no_placeholders.py` (positive and negative cases for every rule, including AST stub detection, Protocol/abstractmethod allowances, JSX `placeholder=` allowance, empty catch detection)
  - Acceptance: `make placeholders` exits 0 on the repo; each rule has at least one failing-fixture test.

- [x] P00-T04 Spec reference and OpenAPI drift gates — both gates exit 0 on repo (drift: bootstrap skip until P00-T11; spec refs: 0 problems), exit 1 on fixtures; 229 tests pass, 99.87 % cov, both modules 100 %; lint/typecheck/placeholders/check green
  - Spec refs: specs/02-repo-layout-and-conventions.md (Quality gates)
  - Depends on: P00-T01
  - Deliverables: `scripts/check_spec_refs.py` (every `specs/<file>.md` path mentioned in `plan/*.md` and `prompts/*.md` exists; every `§N` reference paired with a spec file resolves to a heading starting with `## N.` or `## N ` in that file), `scripts/check_openapi_drift.py` (imports `va_api.main:app`, renders OpenAPI with sorted keys, compares to `openapi/openapi.json`), tests for both.
  - Acceptance: both scripts pass on the repo and fail on crafted fixtures.

- [x] P00-T05 `va_common` core: settings, clock, ids, json, errors — lint/typecheck/placeholders 0; 383 tests pass, 99.90 % total, va_common 100 %; CODES parity with specs/31 §3 (62 rows); review rework (date/time JSON, positional details) verified
  - Spec refs: specs/02-repo-layout-and-conventions.md (Python conventions, IDs, Money, Errors); specs/31-error-catalog.md (all)
  - Depends on: P00-T01
  - Deliverables: `BaseServiceSettings` (pydantic-settings), `Clock` protocol + `SystemClock` + `FrozenClock`, `ids.new_id()` (uuid7), `ids.to_public/from_public` for `TL-`/`KB-`/`PT-`, orjson helpers, `AppError`, `CODES` table, `micros` money helpers (`dollars_to_micros`, `micros_to_dollars_str`)
  - Acceptance: test parses specs/31 §3 table and asserts exact `CODES` parity; property tests (hypothesis) for id round-trips and money conversion.

- [x] P00-T06 `va_common` logging and masking — structlog JSON/console pipeline, redaction (secrets incl. camelCase, phones, transcripts, raw media), OTel ids, component levels; 490 tests pass, logging.py/masking.py 100 % coverage
  - Spec refs: specs/27-observability.md (logging); specs/02-repo-layout-and-conventions.md (Logging rule)
  - Depends on: P00-T05
  - Deliverables: structlog JSON configuration, contextvars binding helpers (`bind_call_context`), phone masking (`+1415***1234`), secret redaction processor (keys matching `*key*`, `*secret*`, `*token*`, `authorization`, `password`), `LOG_TRANSCRIPTS` enforcement (forced false in production)
  - Acceptance: tests capture log output and assert masking/redaction and field binding.

- [x] P00-T07 `va_common.crypto` envelope encryption — reviewer re-ran lint 0, typecheck 0, placeholders 0, test 0 (614 passed, 99.94 %, crypto/* 100 %); blob layout, local/AWS KEK providers, LRU+TTL cache, Envelope + rewrap, CryptoSettings validation per specs/28 §3
  - Spec refs: specs/28-security.md (envelope encryption design)
  - Depends on: P00-T05
  - Deliverables: `KmsProvider` protocol, `LocalKmsProvider` (AES-256-GCM master key from `LOCAL_MASTER_KEY`), `AwsKmsProvider` (aioboto3 `generate_data_key`/`decrypt`), `encrypt_bytes/decrypt_bytes` producing versioned blobs (`v1|key_id|wrapped_dek|nonce|ciphertext`), key rotation re-wrap helper
  - Acceptance: round-trip tests, tamper detection test, AWS provider tested against a fake client implementing the boto3 KMS method signatures.

- [x] P00-T08 `va_common` infrastructure helpers — redis/http factories, OTel tracing (force-sample, OTLP gRPC), 56-metric CATALOG parity with specs/27 §3, observability.init + Sentry redaction; 682 passed, coverage 99.95 %, lint/mypy/placeholders clean
  - Spec refs: specs/02-repo-layout-and-conventions.md (Core Python dependencies); specs/27-observability.md (tracing, metrics)
  - Depends on: P00-T05
  - Deliverables: `redis_client(settings)`, `http_client(settings, *, timeout, http2)` with OTel httpx instrumentation, `setup_tracing(service_name)`, `metrics` registry helpers and `/metrics` ASGI app factory
  - Acceptance: unit tests with fake Redis (testcontainers Redis in integration test) and respx for httpx.

- [x] P00-T09 docker-compose infrastructure profile — infra+observability compose, dev_up/dev_down (bash 3.2 safe), live make up all healthy; 720 passed, cov 99.95 %, lint/typecheck/placeholders clean
  - Spec refs: specs/29-infrastructure.md (docker-compose); specs/02-repo-layout-and-conventions.md (Toolchain versions)
  - Depends on: P00-T01
  - Deliverables: `docker-compose.yml` (postgres pgvector, redis, clickhouse, temporal auto-setup, temporal-ui, minio + bucket-init job creating the three buckets, mailpit, otel-collector, prometheus, grafana) with healthchecks and named volumes; `infra/otel/collector.yaml`; `infra/prometheus/prometheus.yml`; `scripts/dev_up.sh`, `scripts/dev_down.sh`
  - Acceptance: `make up` brings all services to healthy; ports match AGENTS.md.

- [x] P00-T10 ML model and audio asset scripts — fetch_models.sh/.py + MANIFEST (silero v5.1.2, sha256 pinned; --check fails on corrupted byte), generate_audio_assets.py + 11 committed assets byte-identical on regeneration (--check 0); make check 0, 791 passed 1 skipped, cov 99.92 %
  - Spec refs: specs/07-media-pipeline.md (Silero VAD model, background tracks); specs/02-repo-layout-and-conventions.md (`models/`, `assets/audio/`)
  - Depends on: P00-T01
  - Deliverables: `scripts/fetch_models.sh` (downloads Silero VAD ONNX from the official snakers4/silero-vad repository, computes sha256, writes/validates `models/MANIFEST.json`), `scripts/generate_audio_assets.py` (procedurally synthesizes office, cafe, restaurant, phone-static backgrounds and hold music as 8 kHz μ-law and 16 kHz PCM files, deterministic seed), generated files committed under `assets/audio/`
  - Acceptance: re-running produces byte-identical audio; manifest check fails on a corrupted model file.

- [x] P00-T11 API skeleton with envelopes and OpenAPI export — 954 passed, 1 skipped, 99.81 % cov; lint/typecheck/placeholders green; openapi/openapi.json current and valid 3.1; spec refs 0 problems
  - Spec refs: specs/31-error-catalog.md (Envelope matrix, Implementation); specs/01-architecture.md (api service)
  - Depends on: P00-T05, P00-T06, P00-T08
  - Deliverables: `apps/api/src/va_api/main.py` (`create_app()`), request-id middleware, CORS config, `install_handlers` rendering legacy vs envelope errors via `x-envelope`, validation error re-rendering to 400, `/healthz`, `/readyz`, `scripts/export_openapi.py`, `openapi/openapi.json`
  - Acceptance: tests for both envelopes, 400 validation shape, `X-Request-Id` echo/generation; `make openapi` + drift check pass.

- [x] P00-T12 Service skeletons with health and graceful shutdown — va_common.service runtime + checks; edge/media/worker/dispatcher/sink/sim with /healthz,/readyz,/metrics and signal-owned graceful shutdown; 1090 passed, coverage 99.83%, lint/typecheck/placeholders/check green, jscpd 0
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
