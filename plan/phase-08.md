# Phase 08 — Tools, Dynamic Data, Custom Code, Knowledge Bases

- [ ] P08-T01 Tools API with secret headers
  - Spec refs: specs/13-tools-and-dynamic-data.md (Tools API, secret headers)
  - Depends on: P07-T07, P02-T11
  - Deliverables: `/v1/tools` CRUD (TL- ids, legacy envelope), validation of name/url/method/input_schema/response_map, secret header encryption, audit, cross-tenant factory
  - Acceptance: API tests for every field and error path; secrets never returned.

- [ ] P08-T02 Tool runtime, system tools, LLM exposure, speech during tool
  - Spec refs: specs/13-tools-and-dynamic-data.md (LLM exposure, execution runtime, built-in system tools)
  - Depends on: P08-T01, P05-T09
  - Deliverables: function schema generation, in-call execution with timeout/retries/JSONPath extraction, `speech` filler via phrase cache, result injection as tool message, inline tool objects in Send Call, system tools (end_call, transfer_call, press_button, send_sms, schedule_callback, lookup_knowledge)
  - Acceptance: harness tests for filler timing, timeout behaviour, extraction into variables.

- [ ] P08-T03 Dynamic data
  - Spec refs: specs/13-tools-and-dynamic-data.md (dynamic_data)
  - Depends on: P08-T02
  - Deliverables: pre-call fetch in `prefetch_call_context`, per-turn refresh rules, Redis cache keyed by request hash, JSONPath `response_data` mapping with `context` injection, `call.dynamic_data` events
  - Acceptance: tests for cache hits, failures (call proceeds with event), templated requests.

- [ ] P08-T04 Deno sandbox and Custom Code node
  - Spec refs: specs/13-tools-and-dynamic-data.md §8; specs/11-pathways-engine.md (Custom Code node)
  - Depends on: P07-T08
  - Deliverables: `va_tools.sandbox.DenoRunner` (permission flags, V8 memory flag, wall-clock kill, stdin/stdout JSON protocol, concurrency limiter), publish-time `deno check` validation, node handler
  - Acceptance: tests for success, exception, timeout, memory exhaustion, forbidden fs/env/net access, output cap.

- [ ] P08-T05 KB parsers
  - Spec refs: specs/14-knowledge-bases.md (parsers)
  - Depends on: P01-T04
  - Deliverables: pdf, docx, txt/md, html, csv parsers producing structured sections with heading paths and page numbers, run in thread pool
  - Acceptance: fixture documents committed under tests/fixtures produce expected sections.

- [ ] P08-T06 Crawler
  - Spec refs: specs/14-knowledge-bases.md (crawler)
  - Depends on: P08-T05
  - Deliverables: sitemap discovery, robots.txt compliance, same-domain BFS with depth/page limits and rate limiting, content extraction, SSRF guard
  - Acceptance: tests against a local HTTP fixture site.

- [ ] P08-T07 Chunking, embeddings, ingest workflow
  - Spec refs: specs/14-knowledge-bases.md (ingest workflow, chunking and embeddings)
  - Depends on: P08-T05, P03-T14
  - Deliverables: heading-aware token chunker, `KnowledgeIngestWorkflow` (per document activities, retries, status transitions, counts), `kb.ready`/`kb.failed` events, embedding usage metering
  - Acceptance: Temporal tests; chunk boundaries and overlaps verified.

- [ ] P08-T08 Hybrid retrieval and per-call cache
  - Spec refs: specs/14-knowledge-bases.md (hybrid retrieval SQL, latency budget, per-call cache)
  - Depends on: P08-T07
  - Deliverables: `Retriever.search(kb_ids, query, top_k)` with RRF fusion SQL exactly as specified, query embedding cache, per-call cache, benchmark test (marker `bench`)
  - Acceptance: relevance tests on a fixture corpus; benchmark meets p95 target.

- [ ] P08-T09 KB API and KB chat
  - Spec refs: specs/14-knowledge-bases.md (API, KB chat, legacy compat)
  - Depends on: P08-T08
  - Deliverables: list/create/upload/crawl/text/get/delete/documents endpoints, KB chat with sources, legacy `/v1/knowledgebases`, plan limits
  - Acceptance: API tests incl. file type/size errors (`KB_UNSUPPORTED_FILE`, `KB_FILE_TOO_LARGE`).

- [ ] P08-T10 KB node and KB tools in calls
  - Spec refs: specs/11-pathways-engine.md (Knowledge Base node); specs/14-knowledge-bases.md (injection format into the voice prompt); specs/05-calls-api.md (tools array with KB- ids)
  - Depends on: P08-T08, P07-T12
  - Deliverables: KB node handler, `KB-` ids in Send Call `tools` enable retrieval for task-mode calls, prompt injection format
  - Acceptance: e2e kb_lookup passes.

- [ ] P08-T11 E2E: tools
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list: tool_call_with_speech)
  - Depends on: P08-T02
  - Deliverables: `tests_e2e/test_tool_call_with_speech.py`
  - Acceptance: filler heard (recorded agent audio contains filler phrase marker), tool result used in reply.

## Phase exit gate
`make check && make e2e` pass; KB benchmark recorded.
