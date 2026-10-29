# 26 — OpenAPI, SDKs, CLI, MCP Server, Documentation

## 1. OpenAPI generation rules

- Source of truth: FastAPI app `va_api.main:app`. `make openapi` writes `openapi/openapi.json` (OpenAPI 3.1,
  sorted keys, 2-space indent, trailing newline). `scripts/check_openapi_drift.py` regenerates in memory and fails if
  the committed file differs.
- `info`: title "VoiceAgents API", version = `va_api.__version__` (semver), contact, license "Proprietary".
- `servers`: `[{url: "https://api.voiceagents.ai", description:"Production"}, {url:"http://localhost:8000", description:"Local"}]`
  where the production URL is read from `PUBLIC_API_BASE_URL` at generation time in `release` CI (the committed file
  uses the value of env `OPENAPI_PUBLIC_SERVER_URL`, default `https://api.voiceagents.ai`; added to env list, see
  shared-contract additions).
- **operationId** convention: `<resource>_<action>` snake_case, unique, stable: e.g. `calls_send`, `calls_get`,
  `calls_list`, `calls_stop`, `calls_active`, `calls_analyze`, `calls_transfer`, `calls_say`, `calls_events_stream`,
  `pathways_list`, `pathways_create`, `pathways_get`, `pathways_update`, `pathways_delete`, `pathways_publish`,
  `pathways_promote`, `pathways_versions_list`, `pathways_test_chat`, `tools_create`, `knowledge_upload`,
  `voices_clone`, `numbers_purchase`, `inbound_update`, `batches_create`, `sms_send`, `widgets_create`
  Set explicitly on every route via `operation_id=`; a test asserts uniqueness and the regex `^[a-z]+(_[a-z0-9]+)+$`.
  (This list is illustrative of the convention; the full set is whatever the routers define, and the contract test
  in §8 enforces SDK coverage of all of them.)
- **tags**: one per resource family, in this order: Calls, Batches, Pathways, Personas, Agents, Experiments, Tools,
  Knowledge, Voices, Numbers, Inbound, Accounts, SMS, Widgets, Memory, Contacts, Analysis, Citations, Evals,
  Dispositions, Compliance, Webhooks, Billing, Usage, Analytics, Org, API Keys, Audit Logs, Prompts, MCP, Agent Onboarding.
  `/internal/*` routes are excluded from the public document (`include_in_schema=False`) and emitted to
  `openapi/internal.json` for the dashboard (also drift-checked).
- **x-envelope**: every operation carries `x-envelope: legacy | envelope` (`31-error-catalog.md` §1).
- **x-scope**: required scope string (`04` §2.3); **x-permission** when a finer permission applies.
- **Examples**: every request body schema and every 2xx response includes at least one realistic `example`
  (Pydantic `json_schema_extra`); error responses reference shared components `ErrorLegacy`, `ErrorEnvelope`.
- Security schemes: `ApiKeyAuth` (`type: http, scheme: bearer`) and `SessionCookie` (apiKey in cookie `va_session`,
  internal doc only).
- Webhook payloads documented under OpenAPI 3.1 `webhooks`: `callCompleted` (Call Object), `callLiveEvent`,
  `batchStatus`, `envelopeEvent`.
- Nullable via `anyOf [type, null]`; enums named components (for example `CallStatus`, `NodeType`); discriminated unions with
  `discriminator.propertyName` (pathway nodes by `type`, tools inline vs id).

> Decision (2026-10-25): `servers` is built from `OPENAPI_PUBLIC_SERVER_URL` (`Production`) followed by
> `http://localhost:8000` (`Local`), collapsed to the single Local entry when both are equal: the
> `02-repo-layout-and-conventions.md` §6 default `http://localhost:8000` wins over the `https://api.voiceagents.ai`
> text above, so the committed document has one server until release CI sets the variable. A `Health` tag is
> appended after the family tags for `GET /v1/health` (`operationId` `health_get`, `x-scope: public`). The probes
> `/healthz`, `/readyz`, `/metrics` are excluded from the document. FastAPI's generated `422` response is replaced by
> `400` and every operation gets a `default` response, both referencing `ErrorLegacy` or `ErrorEnvelope` by its
> `x-envelope`; the unused `HTTPValidationError`/`ValidationError` components are dropped. Contact: VoiceAgents
> Support, `https://voiceagents.ai/support`, `support@voiceagents.ai`.

> Decision (2026-10-29): **internal document** `openapi/internal.json`: title "VoiceAgents Internal API", the same
> version, servers, contact, and license as the public document; `paths` = every route of the app's routers whose
> path starts with `/internal/` (generated from shallow copies with `include_in_schema=True`, so the public document
> still excludes them); `tags` = `Auth` (extended as internal families arrive, every internal route must use one);
> `securitySchemes` = `SessionCookie` only; the same error components, `422`→`400` rewrite, and `default` response
> as the public document; operations keep `x-public` and `x-envelope`. Envelope success bodies
> (`DataResponse[T]`) carry `T`'s examples wrapped as `{data: <example>}`, so every 2xx response has an example.
> `make openapi` writes both files and `scripts/check_openapi_drift.py` checks both, reporting each file separately.

## 2. Python SDK `voiceagents` (`sdks/python`)

### 2.1 Layout

```
sdks/python/
├── pyproject.toml            # name "voiceagents", deps: httpx>=0.27, pydantic>=2.9, typing-extensions
├── src/voiceagents/
│   ├── __init__.py           # exports VoiceAgents, AsyncVoiceAgents, errors, webhooks
│   ├── _version.py
│   ├── _client.py            # base sync/async clients
│   ├── _transport.py         # retries, timeouts, idempotency, headers
│   ├── _pagination.py        # SyncCursorPage, AsyncCursorPage, legacy list helpers
│   ├── _errors.py
│   ├── _streaming.py         # SSE parser for events streams
│   ├── models/_generated.py  # datamodel-code-generator output (pydantic v2) from openapi.json — committed
│   ├── models/__init__.py    # re-exports with stable names
│   ├── resources/            # hand-written resource classes, one module per tag
│   └── webhooks.py           # signature verification helpers
└── tests/
```

`scripts/gen_python_models.sh`: `datamodel-codegen --input openapi/openapi.json --input-file-type openapi
--output sdks/python/src/voiceagents/models/_generated.py --output-model-type pydantic_v2.BaseModel
--use-annotated --field-constraints --use-standard-collections --target-python-version 3.9 --enum-field-as-literal one`.
Drift-checked in CI.

### 2.2 Client

```python
client = VoiceAgents(api_key=os.environ["VOICEAGENTS_API_KEY"], base_url="https://api.voiceagents.ai",
                     timeout=30.0, max_retries=2, org_id=None)   # org_id sets X-VA-Org-Id for sub-accounts
call = client.calls.send(phone_number="+14155550123", pathway_id="…", request_data={"name": "Ana"})
details = client.calls.get(call.call_id)
for c in client.calls.list(limit=100):  # auto-paginates
    ...
async with AsyncVoiceAgents() as aclient:
    await aclient.calls.stop(call_id)
```

- `api_key` defaults to env `VOICEAGENTS_API_KEY`; `base_url` defaults to env `VOICEAGENTS_BASE_URL` or production.
- Headers: `Authorization: Bearer`, `User-Agent: voiceagents-python/<ver> python/<pyver>`, `Idempotency-Key` auto-generated
  (uuid4) for POST when `idempotency_key` not passed (makes retries safe).
- Retries: on connection errors, 408, 409 `IDEMPOTENCY_IN_PROGRESS`, 429 (honour `Retry-After`), 500, 502, 503, 504;
  exponential backoff 0.5 s × 2^n with full jitter, cap 8 s, `max_retries` default 2.
- Errors: `VoiceAgentsError` base; `APIStatusError(status, code, message, request_id, body)` subclasses
  `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (404), `BadRequestError` (400),
  `ConflictError` (409), `RateLimitError` (429), `InsufficientCreditsError` (402), `InternalServerError` (≥500);
  `APIConnectionError`, `APITimeoutError`. Both envelopes parsed.
- Resources (attributes on client): `calls`, `batches`, `pathways` (with `.versions`, `.folders`, `.tests`,
  `.simulations`), `personas`, `agents` (with `.versions`, `.experiments`), `tools`, `knowledge`, `voices`, `numbers`
  (with `.inbound`, `.pools`), `accounts` (BYO Telnyx), `sms`, `widgets`, `memory`, `contacts`, `analysis`
  (`.dispositions`, `.citation_schemas`, `.evals`), `compliance` (`.dnc`), `webhooks` (`.endpoints`, `.deliveries`),
  `billing`, `usage`, `analytics`, `org` (`.members`, `.sub_accounts`), `api_keys`, `audit_logs`, `prompts`.
  Every operationId maps to exactly one method; method name = operationId minus resource prefix.
- Pagination: envelope endpoints return `SyncCursorPage[T]` iterable across pages (`.has_more`, `.next_cursor`,
  `.data`); legacy `calls.list` yields calls using `limit`/`start_date` windowing.
- Streaming: `client.calls.events(call_id)` → iterator of `CallLiveEvent` from SSE with auto-resume using
  `Last-Event-ID`.
- Webhooks helper: `voiceagents.webhooks.verify(payload: bytes, headers: Mapping[str,str], secret: str,
  tolerance_s: int = 300) -> dict` validates `X-VA-Signature` (`t=…,v1=…`, any v1 matching during rotation) or the
  Bland-compatible `X-Webhook-Signature`; raises `WebhookVerificationError`. `construct_event` returns typed model.
- Typing: `py.typed`, mypy strict clean, supports Python 3.9–3.13.

## 3. TypeScript SDK `@voiceagents/sdk` (`sdks/typescript`)

- Build with `tsup` → ESM + CJS + `.d.ts`; runtime `fetch` (Node ≥ 18, browsers, Deno, Bun, edge runtimes).
- Types generated by `openapi-typescript` into `src/schema.d.ts` (committed, drift-checked); hand-written resource
  classes mirror the Python SDK naming in camelCase (`client.calls.send`, `client.pathways.versions.list`).
- Options: `{apiKey?, baseURL?, timeout? (ms, default 30000), maxRetries? (2), orgId?, fetch? (custom), defaultHeaders?}`;
  `apiKey` from `process.env.VOICEAGENTS_API_KEY` when available.
- Same retry/idempotency/error taxonomy as Python (`VoiceAgentsError`, `APIError` subclasses).
- Async iterators for pagination (`for await (const call of client.calls.list())`), SSE via `ReadableStream` parser.
- `webhooks.verify(rawBody: string | Uint8Array, headers: Headers | Record<string,string>, secret)` using Web Crypto
  HMAC (works in edge runtimes).
- Tests: Vitest with `msw` mocking; type tests with `tsd`.

> Decision (2026-10-25): (a) Type tests use Vitest `expectTypeOf` (`vitest run --typecheck`, files `tests/*.test-d.ts`)
> instead of `tsd`, which needs built `.d.ts` files while `pnpm -r test` runs before `build`. (b) Browsers forbid setting
> `User-Agent`, so the SDK identifies itself with `X-VA-Client: voiceagents-typescript/<version>` on every request.
> (c) Schema drift is a Vitest test (`tests/schema.test.ts`) that regenerates the types from `openapi/openapi.json` with
> `openapi-typescript` and compares them with the committed `src/schema.d.ts`. (d) Retries follow §2.2 exactly
> (connection errors and timeouts, 408, 409 `IDEMPOTENCY_IN_PROGRESS`, 429, 500, 502, 503, 504); a `Retry-After` of
> delta-seconds or HTTP date up to 60 s replaces the backoff delay, longer values fall back to the backoff.

## 4. CLI `va` (`sdks/cli`, Typer + Rich, depends on `voiceagents`)

Config file `~/.config/voiceagents/config.toml`: profiles `{name: {api_key, base_url, org_id?}}`, `default_profile`.
Global options: `--profile`, `--api-key`, `--base-url`, `--org`, `--json` (raw JSON output), `--no-color`.

| Command | Behaviour |
|---|---|
| `va auth login [--profile]` | Prompts for API key (hidden), validates with `GET /v1/me`, stores profile (file mode 0600) |
| `va auth logout`, `va auth whoami`, `va auth profiles` | |
| `va calls send --to <e164> [--from] [--pathway ID[@ver]] [--persona] [--agent ID[@ver]] [--task TEXT|@file] [--voice] [--first-sentence] [--record] [--max-duration] [--webhook] [--data k=v ...] [--data-json @file] [--wait]` | Sends call; `--wait` tails events until completed and prints summary |
| `va calls list [filters] [--limit]` | Table |
| `va calls get <id> [--transcript]` | Details, optional formatted transcript |
| `va calls stop <id>` | |
| `va calls watch <id>` | Tails SSE live events with coloured categories |
| `va calls recording <id> [-o file.wav]` | Downloads recording |
| `va pathways list`, `va pathways get <id> [--version]` | |
| `va pathways pull <id> [-o pathway.json] [--version]` | Writes canonical JSON |
| `va pathways push <file> [--id ID]` | Creates or updates draft from JSON |
| `va pathways publish <id> [--promote production|staging] [--label]` | |
| `va pathways test <id> [--version] [--message ...] [--interactive]` | Text chat REPL against test chat endpoint with per-turn logs |
| `va pathways tests run <id>` | Runs unit tests, exit code 1 on failure (CI-friendly) |
| `va tools list|get|create --file|test <id> --input @json` | |
| `va kb list`, `va kb create <name>`, `va kb upload <kb_id> <files...>`, `va kb crawl <kb_id> <url>`, `va kb chat <kb_id> "<q>"` | |
| `va voices list [--filter]`, `va voices clone --name N <files...>`, `va voices preview <id> "<text>" [-o file]` | |
| `va numbers search --area-code 415 [--country US] [--sms]`, `va numbers buy <e164|--area-code>`, `va numbers list`, `va numbers configure <e164> --pathway ID [--voice] [--webhook] [--file inbound.json]`, `va numbers release <e164>` | |
| `va batches create --csv file.csv --map phone_number=Phone --map request_data.name=Name [--global @file.json] [--start-at] [--wait]`, `va batches list|get|stop|logs` | |
| `va webhooks listen --forward-to http://localhost:3000/hook [--events call.*]` | Opens SSE `GET /v1/events/stream?types=` (API-key authenticated variant for CLI with scope `webhooks:read`), re-signs each event with the org secret fetched via a short-lived `GET /v1/webhooks/cli-secret` and POSTs to the local URL; prints status lines. No tunnel required. |
| `va usage [--from] [--to] [--by meter|day]` | |
| `va mcp stdio` | Runs the MCP server in stdio mode with the current profile (§5) |
| `va completion install` | Shell completion |

Exit codes: 0 success, 1 API error (message printed with code and request id), 2 usage error.

## 5. MCP server (`packages/va_mcp`)

- Implemented with the official `mcp` Python SDK (FastMCP). Two transports:
  1. **Streamable HTTP** mounted in `api` at `/v1/mcp` (`POST`/`GET` per MCP spec); auth `Authorization: Bearer <api key>`
     requiring scope `mcp`; each tool checks the underlying operation's scope.
  2. **stdio** via `va mcp stdio` (CLI) using the configured API key; tools call the public API through the SDK.
- Server name `voiceagents`, version = API version, instructions string describing capabilities.
- Tools (name → input JSON Schema → behaviour):

| Tool | Input schema (properties; required in bold) | Calls |
|---|---|---|
| `send_call` | **phone_number** string E.164; task string; pathway_id string; pathway_version integer\|string; persona_id; agent_id; voice; first_sentence; from; request_data object; metadata object; record boolean; max_duration integer 1–240; webhook string uri; wait_for_greeting boolean | `calls_send` |
| `get_call` | **call_id** string; include_transcript boolean (default true) | `calls_get` |
| `list_calls` | limit integer 1–100 (25); status string; from string; to string; start_date string date; end_date; batch_id | `calls_list` |
| `stop_call` | **call_id** | `calls_stop` |
| `list_pathways` | folder_id; limit | `pathways_list` |
| `get_pathway` | **pathway_id**; version integer\|"production"\|"staging"\|"draft" | `pathways_get` |
| `update_pathway` | **pathway_id**; name; description; nodes array (PathwayNode); edges array (PathwayEdge); global_config object | `pathways_update` (draft) |
| `publish_pathway` | **pathway_id**; promote_to "production"\|"staging"\|null; label string | `pathways_publish` |
| `list_voices` | search string; language string; gender string; category string; limit | `voices_list` |
| `create_tool` | **name**; **description**; **url**; method enum; headers object; body object; query object; input_schema object; response_map object; speech string; timeout_ms integer | `tools_create` |
| `search_knowledge` | **kb_id**; **query**; top_k integer 1–20 (5) | `knowledge_search` |
| `list_numbers` | limit | `numbers_list` |
| `create_batch` | **calls** array of {phone_number, request_data?, metadata?}; global object (Send Call props); label; status_webhook; start_time | `batches_create` |
| `get_usage` | from date; to date; group_by "day"\|"meter" | `usage_get` |

- Resources: `voiceagents://pathways/{id}` (canonical JSON), `voiceagents://calls/{id}/transcript` (text).
- Prompts: `design_pathway` (args: use_case, constraints) returning a structured instruction to draft a pathway.
- Tool results: JSON text content plus `structuredContent` matching the operation response schema; errors returned as
  `isError: true` with the API error code and message.

## 6. Documentation site (`apps/docs`, MkDocs Material)

```
apps/docs/
├── mkdocs.yml
├── docs/
│   ├── index.md                         # Overview, quickstart (send first call in 5 minutes)
│   ├── quickstart/{api-key,first-call,inbound-number,first-pathway}.md
│   ├── guides/
│   │   ├── prompting.md                 # task prompt best practices (≤ 2,000 chars guidance)
│   │   ├── pathways/{overview,node-types,edges-and-conditions,global-nodes,variables,testing,versions}.md
│   │   ├── tools.md  dynamic-data.md  knowledge-bases.md  voices-and-cloning.md
│   │   ├── voicemail-and-amd.md  transfers.md  interruptions-and-timing.md  languages.md
│   │   ├── batches.md  scheduling-and-retries.md  inbound.md  numbers-and-pools.md  byo-telnyx.md
│   │   ├── sms.md  10dlc.md  widget.md  memory.md  personas.md  agents-and-experiments.md
│   │   ├── post-call-analysis.md  citations.md  evals.md  guard-rails.md  compliance-tcpa.md
│   │   ├── webhooks.md  security.md  sub-accounts.md  billing-and-limits.md  latency.md
│   │   └── migrating-from-bland.md      # field-by-field compatibility table and differences
│   ├── api-reference/index.md           # embeds Redoc standalone (pinned redoc@2.1.5 bundle vendored in docs/assets)
│   ├── webhooks-reference.md            # generated from 19 §2/§4 models
│   ├── errors.md                        # generated from 31 §3 table
│   ├── sdks/{python,typescript,cli,mcp}.md
│   └── changelog.md
└── scripts/gen_reference.py            # copies openapi.json, renders errors/webhooks pages, builds llms files
```

- `llms.txt` (served at `/llms.txt` on the docs host and on the API host): title, one-paragraph summary, then link
  lists per section (`- [Title](absolute url): one-line description`) per the llms.txt convention.
- `llms-full.txt`: concatenation of all guide markdown plus a condensed API reference (each operation: method, path,
  summary, parameters table, example request/response) generated from OpenAPI.
- Build: `mkdocs build --strict` in CI (broken links fail). Deployed as static files (S3 + CloudFront, `29`).

## 7. Release process

- Versioning: API semver in `va_api/__init__.py`; SDKs version independently (semver) but declare `x-api-version`
  compatibility; CLI pins SDK minor.
- `release.yml` (`29-infrastructure.md` §6) on tag `sdk-python-v*` → build wheel/sdist with `uv build`, publish to
  PyPI via trusted publishing; `sdk-ts-v*` → `pnpm publish --provenance`; `cli-v*` → PyPI.
- Changelogs maintained with Changesets (TS) and `CHANGELOG.md` per package (Keep a Changelog format).
- Breaking API changes require a new path version (`/v2/...`) — never break `/v1` fields.

## 8. Contract tests

- `sdks/python/tests/test_operation_coverage.py`: loads `openapi/openapi.json`, collects operationIds, asserts each
  has a resource method (registry built by a decorator `@operation("calls_send")` on SDK methods) and vice versa.
- Same for TypeScript (`sdks/typescript/test/coverage.test.ts`) and MCP tools (each MCP tool references an existing
  operationId).
- SDK integration tests run against the real `api` app (ASGI transport via `httpx.ASGITransport` for Python, and a
  compose-run API for TS in CI) with simulators, executing at least one call per resource family.
- Webhook verify helpers tested with the same fixed vectors as `19` §11 (shared fixture JSON
  `openapi/fixtures/webhook_signature_vectors.json`).

## 9. Tests required

Covered in §8 plus: retry/backoff behaviour (respx/msw), idempotency key auto-generation, error mapping for both
envelopes, pagination across 3 pages, SSE resume, CLI commands (Typer `CliRunner` against ASGI app), MCP tools via
the SDK's in-memory client session (`mcp.shared.memory`), `mkdocs build --strict`, llms.txt format test.

## 10. Acceptance criteria

- OpenAPI drift check passes; every operation has operationId, tag, x-envelope, x-scope, examples.
- Python and TS SDKs cover 100 % of public operationIds; published packages install and pass smoke tests.
- `va` CLI commands in §4 work end-to-end against local stack; `va webhooks listen` forwards a signed event.
- MCP server lists the 14 tools and executes `send_call` → `get_call` in an automated test over streamable HTTP.
- Docs build strictly; `llms.txt` and `llms-full.txt` are generated and served.
