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
OTel collector 4317 · Prometheus 9090 · Grafana 3001

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
