## Phase 08 — Tools, Dynamic Data, Custom Code Sandbox, and Knowledge Bases

### Goal
Give agents the ability to act and to know: custom tools (CRUD, secret headers, LLM function exposure, execution with
speech-during-tool fillers), built-in system tools, dynamic data (pre-call and in-call), the Deno custom-code sandbox and
Custom Code node, and knowledge bases end to end (upload, crawl, text, parsing, chunking, embeddings, hybrid retrieval,
KB chat, KB node, KB ids in the `tools` array).

### Read first
- `specs/13-tools-and-dynamic-data.md` — entire file (incl. §8 Deno sandbox).
- `specs/14-knowledge-bases.md` — entire file.
- `specs/11-pathways-engine.md` — Knowledge Base node and Custom Code node semantics, prompt assembly KB section.
- `specs/10-llm-gateway.md` — function definitions from `input_schema`, embeddings client.
- `specs/19-events-and-webhooks.md` — `call.tool`, `call.dynamic_data`, `kb.ready`, `kb.failed`.

### Phase-specific constraints
- Tool execution never blocks the audio loop: tools run as tasks; filler speech starts within 300 ms of the tool call
  when `speech` is configured, and the LLM resumes when the tool returns or times out.
- The Deno sandbox has no filesystem, environment, subprocess, or FFI permissions; network only to the declared
  allowlist; hard wall-clock kill; output size cap 64 KB.
- Retrieval budget: p95 ≤ 120 ms for top-5 hybrid retrieval on a 100k-chunk KB in the integration benchmark.

### Phase exit gate
`make check && make e2e` pass including flows tool_call_with_speech and kb_lookup; retrieval benchmark recorded in
`packages/va_kb/BENCHMARKS.md`.
