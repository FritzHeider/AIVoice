# Phase 07 — Conversational Pathways: Engine, API, Versions, and Testing

- [x] P07-T01 Node and edge schemas with validation — va_engine.models.nodes/graph (discriminated union, lossless Bland round trip, graph_hash) + va_engine.validation (all 18 §11 rules, spec-table drift test); make test 11386 passed 7 skipped, lint/typecheck/placeholders pass, new modules 100% cov, radon 2.78, jscpd 0
  - Spec refs: specs/11-pathways-engine.md (PathwayNode and PathwayEdge JSON schemas, validation rules)
  - Depends on: P01-T04
  - Deliverables: pydantic discriminated union of node types with every field, edge model, `validate_graph(nodes, edges) -> list[GraphIssue]` with exact messages, canonical JSON + `graph_hash`
  - Acceptance: tests per rule; round-trip of Bland-style exported JSON fixtures.

- [x] P07-T02 Templating engine — never-raise rendering (big ints, non-str keys, NaN, date overflow), render/render_url accept text via cached_template LRU, 33 golden cases incl. spec §8 examples with drift checks, grammar fuzz + hostile values; make test 11442 passed, templating 100% cov
  - Spec refs: specs/11-pathways-engine.md (templating engine, built-ins, filters)
  - Depends on: P00-T05
  - Deliverables: `va_engine.templating.render(template, scope)` with dot paths, `{{input.x}}`, `default` filter, missing-variable policy, escaping rules
  - Acceptance: golden tests from spec examples; fuzz test never raises on arbitrary input.

- [x] P07-T03 EngineState and serialization — v2 state (pending_actions, wait_armed_at_ms), dump/load_engine_state with v1 upgrader, frozen v1/v2 fixtures; make test 11489 passed, lint/typecheck/placeholders pass
  - Spec refs: specs/11-pathways-engine.md §9
  - Depends on: P07-T01
  - Deliverables: `EngineState` model (current node, return stack, variables, visit counts, history window, pending actions), versioned JSON serialization
  - Acceptance: round-trip tests; backward-compat test with a v1 fixture.

- [x] P07-T04 Decision calls: routing, extraction, loop conditions — va_engine router/loop_condition/extraction over StructuredPort, LRU dynamic schemas, va_llm router/loop_condition/extract prompts (drift-checked); make test 11654 passed, 99.06% cov; lint/typecheck/placeholders/spec refs pass
  - Spec refs: specs/11-pathways-engine.md (per-turn algorithm); specs/10-llm-gateway.md (router prompt, loop-condition prompt, extraction prompt)
  - Depends on: P03-T14, P07-T03
  - Deliverables: `Router.choose(...)` with examples and alternatives, `Extractor.extract(...)` typed coercion, `LoopCondition.score(...)` with threshold
  - Acceptance: FakeLLM-driven tests including malformed outputs and repair.

- [x] P07-T05 Global nodes and return stack — va_engine.navigation Navigator (global push/auto-return/forwarding/stack cap, never-raise problems), EngineState v3 NodeFrame.kind + v2 upgrader + frozen v3 fixture, Decision under specs/11 §4; make test 11712 passed, navigation.py 100 %
  - Spec refs: specs/11-pathways-engine.md (global nodes, forwarding)
  - Depends on: P07-T04
  - Deliverables: global check precedence, return-to-previous behaviour, forwarding to node or pathway
  - Acceptance: scenario tests with nested global triggers.

- [x] P07-T06 Node handlers: Default, Wait for Response, End Call, Transfer Call (cold), Global — va_engine.handlers + final_lines.GatewayLinePort, specs/11 §4 Decision P07-T06; make test 11821 passed, 99.07 %, new modules 100 %
  - Spec refs: specs/11-pathways-engine.md (node types: Default, Wait for Response, End Call, Transfer Call, Global)
  - Depends on: P07-T05
  - Deliverables: handlers with static text vs prompt, skipUserResponse, end-call speech modes, cold transfer action emission
  - Acceptance: unit tests per handler incl. speech_mode prompt fallback.

- [ ] P07-T07 Node handlers: Webhook, SMS, Press Button, Transfer Pathway, Scheduling
  - Spec refs: specs/11-pathways-engine.md (node types: Webhook, SMS, Press Button, Transfer Pathway, Scheduling); specs/13-tools-and-dynamic-data.md (HTTP execution, JSONPath mapping, SSRF)
  - Depends on: P07-T05
  - Deliverables: shared `va_tools.http` executor (templating, timeouts, retries, JSONPath, SSRF guard), handlers for each node type, extraction-before-webhook ordering
  - Acceptance: tests with respx and the SSRF guard; Transfer Pathway swaps graph and preserves variables.

- [ ] P07-T08 Turn orchestration, speculative generation, loop protection, pathway_logs
  - Spec refs: specs/11-pathways-engine.md (per-turn algorithm, speculative generation, max node visits, pathway_logs format)
  - Depends on: P07-T06, P07-T07
  - Deliverables: `PathwayEngine.handle_user_turn(...)` streaming reply interface for the pipeline, speculative generation cancel/commit, skip-user-response chaining, visit limits, logs
  - Acceptance: snapshot tests for pathway_logs; speculative path discarded when route changes.

- [ ] P07-T09 Pathways API: CRUD, versions, publish, promote, folders, import/export
  - Spec refs: specs/12-pathways-api.md (all endpoints)
  - Depends on: P07-T01, P02-T11
  - Deliverables: routers with legacy envelope, draft editing, version creation, publish/promote with validation, folders, duplicate, import/export, audit entries, cross-tenant factories
  - Acceptance: API tests for every endpoint and error path.

- [ ] P07-T10 Test chat (text mode)
  - Spec refs: specs/12-pathways-api.md (test chat); specs/11-pathways-engine.md (text-mode degradations)
  - Depends on: P07-T08, P07-T09
  - Deliverables: chat session create/send/reset, expanded per-turn logs in responses
  - Acceptance: multi-turn chat test reproduces expected routes.

- [ ] P07-T11 Unit tests, simulations, regression on publish
  - Spec refs: specs/12-pathways-api.md (unit tests, simulation runs, regression suite on publish)
  - Depends on: P07-T10
  - Deliverables: unit test CRUD + run (variations, grading goal), LLM-as-caller simulation runner, `PathwayTestWorkflow`, publish gate `block_publish_on_failure`
  - Acceptance: tests for pass/fail/error outcomes and publish blocking.

- [ ] P07-T12 Pathway runner integration in calls
  - Spec refs: specs/05-calls-api.md (runner selection: pathway_id/pathway_version); specs/11-pathways-engine.md (voice integration)
  - Depends on: P07-T08, P05-T09
  - Deliverables: `PathwayBrain` implementing the pipeline Brain protocol, CallContext loading of pinned version, start node prefetch, call events emission, final variables persisted to `calls.variables`
  - Acceptance: e2e pathway_restaurant_template and webhook_node pass.

## Phase exit gate
`make check && make e2e` pass.
