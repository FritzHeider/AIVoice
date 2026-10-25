## Phase 18 — Developer Experience: OpenAPI, Python SDK, TypeScript SDK, CLI, MCP Server, Documentation

### Goal
Make the platform developer-first: a polished OpenAPI 3.1 document (operationIds, tags, examples, envelopes), the
`voiceagents` Python SDK (sync + async), the `@voiceagents/sdk` TypeScript SDK, the `va` CLI (including live call
watching and local webhook forwarding), the MCP server (mounted at `/v1/mcp` and stdio), and the documentation site with
guides, API reference, webhook reference, SDK docs, changelog, `llms.txt` and `llms-full.txt`.

### Read first
- `specs/26-sdks-cli-mcp-docs.md` — entire file.
- `specs/19-events-and-webhooks.md` — signature verification helpers in SDKs; SSE for CLI watch.
- `specs/31-error-catalog.md` — typed errors in SDKs.

### Phase exit gate
`make check` passes; SDK coverage contract tests prove every operationId is reachable from both SDKs and from the CLI or
MCP where specified; `mkdocs build --strict` passes; `llms.txt` generated and committed.
