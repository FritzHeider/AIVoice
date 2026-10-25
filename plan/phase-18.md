# Phase 18 — OpenAPI, SDKs, CLI, MCP, Docs

- [ ] P18-T01 OpenAPI polish
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (OpenAPI generation rules)
  - Depends on: P15-T08
  - Deliverables: operationIds per convention, tags, request/response examples on every operation, `x-envelope`, security schemes, webhook definitions (`webhooks:` section for every event payload), drift check updated
  - Acceptance: spectral lint (ruleset in repo) passes; drift check passes.

- [ ] P18-T02 Python SDK
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (Python SDK)
  - Depends on: P18-T01
  - Deliverables: `sdks/python` (models via datamodel-code-generator, resource layer, sync/async clients, pagination iterators, retries with idempotency keys, typed errors, `verify_webhook_signature`), README, tests against the running API in the e2e stack
  - Acceptance: coverage contract test (every operationId has a method).

- [ ] P18-T03 TypeScript SDK
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (TypeScript SDK)
  - Depends on: P18-T01
  - Deliverables: `sdks/typescript` (types from openapi-typescript, fetch client, ESM+CJS builds, pagination, retries, typed errors, webhook verification helper), tests
  - Acceptance: coverage contract test; build outputs both module formats.

- [ ] P18-T04 CLI
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (CLI `va`)
  - Depends on: P18-T02
  - Deliverables: Typer CLI with every command group in the spec, config file and profiles, `calls watch` (SSE), `webhooks listen` (SSE forwarding to localhost), JSON/table output, shell completion
  - Acceptance: CliRunner tests for each command against a respx-mocked API.

- [ ] P18-T05 MCP server
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (MCP server)
  - Depends on: P18-T02
  - Deliverables: `packages/va_mcp` with every tool in the spec (input schemas, output formatting), streamable HTTP mount at `/v1/mcp` with API key auth, stdio entry point `va mcp serve`
  - Acceptance: MCP client tests (official `mcp` SDK client) listing tools and invoking send_call and get_call.

- [ ] P18-T06 Documentation site and llms.txt
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (docs site structure, llms.txt and llms-full.txt generation)
  - Depends on: P18-T01
  - Deliverables: `apps/docs` MkDocs Material with all guide pages listed in the spec, Redoc API reference, webhook reference generated from OpenAPI webhooks, SDK pages, changelog, generator for `llms.txt`/`llms-full.txt`
  - Acceptance: `mkdocs build --strict` passes; link checker passes.

- [ ] P18-T07 SDK and CLI contract tests in CI
  - Spec refs: specs/26-sdks-cli-mcp-docs.md (contract tests, release process)
  - Depends on: P18-T02, P18-T03, P18-T04, P18-T05
  - Deliverables: CI job running SDK suites against the e2e stack; release workflow steps for PyPI/npm publishing (dry-run in CI)
  - Acceptance: CI job green.

## Phase exit gate
`make check` passes; docs build strict; contract tests green.
