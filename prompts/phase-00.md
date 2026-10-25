## Phase 00 — Repository Bootstrap, Tooling, and Quality Gates

### Goal
Create a working monorepo in which every later phase can be built and verified: uv + pnpm workspaces, lint/type/test
tooling, the placeholder and spec-reference gates, the shared `va_common` library, local infrastructure via
docker-compose, the API skeleton with error envelopes and OpenAPI generation, runnable skeleton services with health
endpoints, TypeScript workspace scaffolds, and CI.

### Read first
- `specs/00-overview.md` (whole file) — scope and non-negotiables.
- `specs/01-architecture.md` §1, §4, §5 — services, ports, configuration.
- `specs/02-repo-layout-and-conventions.md` (whole file) — layout, toolchain, Makefile targets, env vars, gates.
- `specs/31-error-catalog.md` (whole file) — `AppError`, `CODES`, envelopes.
- `specs/28-security.md` — envelope encryption design for `va_common.crypto`.
- `specs/27-observability.md` — logging, tracing, metrics conventions for `va_common`.
- `specs/29-infrastructure.md` — docker-compose service definitions (infra profile only in this phase).

### In scope
- Root `pyproject.toml` (uv workspace with every package/app directory as a member, dev dependency group, ruff + mypy +
  pytest + coverage config), `Makefile` with every target in specs/02 §3, `.gitignore`, `.editorconfig`,
  `.env.example` with **every** variable from specs/02 §6, `package.json` + `pnpm-workspace.yaml`.
- `scripts/check_no_placeholders.py`, `scripts/check_spec_refs.py`, `scripts/check_openapi_drift.py` with tests.
- `packages/va_common` complete: settings, clock, ids, errors, json, logging, masking, crypto, redis factory,
  http factory, tracing/metrics setup.
- `docker-compose.yml` infra profile with healthchecks; `scripts/dev_up.sh`, `scripts/dev_down.sh`.
- `scripts/fetch_models.sh` + `models/MANIFEST.json`; `scripts/generate_audio_assets.py` + generated assets.
- `apps/api` FastAPI app factory with request-id middleware, envelope-aware error handlers, `/healthz`, `/readyz`,
  OpenAPI export; `make openapi` writes `openapi/openapi.json`.
- Every other Python app (`telephony_edge`, `media_worker`, `worker`, `webhook_dispatcher`, `events_sink`, `sim`) with
  settings, structured logging, graceful shutdown, `/healthz` + `/readyz` (worker-type services expose them on their
  metrics port), and a `main` entry point that starts and stops cleanly.
- TypeScript: `apps/dashboard` (Next.js 15 app router, Tailwind 4, shadcn/ui init, design tokens, root layout, a
  `/status` page that calls `api /healthz`), `apps/widget` (Vite library build producing `widget.js` with an exported
  `init()` that mounts a Shadow DOM root), `sdks/typescript` (package scaffold with a typed `VoiceAgentsClient`
  constructor and `health()` method) — each with ESLint, Prettier, typecheck, and Vitest tests.
- `.github/workflows/ci.yml` running `make check` on push/PR with service containers.

### Out of scope
Database models (phase 01), authentication (phase 02), vendor clients (phase 03).

### Phase-specific constraints
- The placeholder checker must itself be strict from day one; all later code is written against it.
- `va_common.errors.CODES` must be generated from and tested against the table in `specs/31-error-catalog.md` §3.
- Health endpoints must report dependency status truthfully (`/readyz` checks Postgres/Redis connectivity when the
  service uses them; returns 503 with a JSON body listing failing dependencies).

### Phase exit gate
`make setup && make up && make check` passes on a clean clone; `docker compose ps` shows all infra services healthy;
`make openapi` produces a valid OpenAPI 3.1 document.
