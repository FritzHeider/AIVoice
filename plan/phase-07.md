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

- [x] P07-T07 Node handlers: Webhook, SMS, Press Button, Transfer Pathway, Scheduling — va_engine handlers + ports + Navigator.switch_graph/return_from_pathway, new va_tools (SSRF-safe HttpExecutor, JSONPath); make test 12028 passed (99.08 % cov), lint/typecheck/placeholders/spec refs clean, jscpd 0 clones
  - Spec refs: specs/11-pathways-engine.md (node types: Webhook, SMS, Press Button, Transfer Pathway, Scheduling); specs/13-tools-and-dynamic-data.md (HTTP execution, JSONPath mapping, SSRF)
  - Depends on: P07-T05
  - Deliverables: shared `va_tools.http` executor (templating, timeouts, retries, JSONPath, SSRF guard), handlers for each node type, extraction-before-webhook ordering
  - Acceptance: tests with respx and the SSRF guard; Transfer Pathway swaps graph and preserves variables.

- [x] P07-T08 Turn orchestration, speculative generation, loop protection, pathway_logs (split into P07-T08a–c below; all three ticked: 06b8143)
  - Spec refs: specs/11-pathways-engine.md (per-turn algorithm, speculative generation, max node visits, pathway_logs format)
  - Depends on: P07-T06, P07-T07
  - Deliverables: `PathwayEngine.handle_user_turn(...)` streaming reply interface for the pipeline, speculative generation cancel/commit, skip-user-response chaining, visit limits, logs
  - Acceptance: snapshot tests for pathway_logs; speculative path discarded when route changes.

- [x] P07-T08a Pathway dialogue prompt assembly and speculative generation coordinator — va_engine.prompt/dialogue/speculative + specs/11 §5.8 Decision; make test 12089 passed, 7 skipped, 99.09 %; lint/typecheck/placeholders/spec refs pass; jscpd 0 clones; radon 2.84
  - Spec refs: specs/11-pathways-engine.md §5.7, §5.8, §13; specs/10-llm-gateway.md §2, §4, §5, §6, §8
  - Depends on: P07-T06, P07-T07
  - Deliverables: `va_engine.prompt` (pathway dialogue request: instructions, examples, history window, model options precedence), `va_engine.dialogue.GatewayDialogue` (the production `DialoguePort` over `stream_dialogue`), `va_engine.speculative` (eligibility, referenced-variable scan, `Speculation` commit/discard, committed-stream port)
  - Acceptance: request snapshots per node kind; speculation committed when staying, discarded (no token reaches the consumer) when the route changes or a referenced variable changed; FakeLLM contract test.

- [x] P07-T08b `PathwayEngine` turn orchestration, chaining, loop protection, events — va_engine.engine + engine_decide/engine_events/engine_tools, specs/11 §5.9 Decision; make test 12142 passed, 7 skipped, cov 99.07 %; lint/typecheck/placeholders pass
  - Spec refs: specs/11-pathways-engine.md §4, §5.1–§5.6, §5.9, §7; specs/19-events-and-webhooks.md §2
  - Depends on: P07-T08a
  - Deliverables: `va_engine.engine.PathwayEngine` (`start`, `handle_user_turn`, `on_wait_timeout`, `on_interrupted`, `aclose`) yielding §7 actions, decision pipeline with speculation, skip-user-response chaining (depth 10), `maxVisits`/`maxTotalTurns` loop protection, background extraction, transfer-pathway return, scheduling function rounds, events through an `EngineEvents` port
  - Acceptance: multi-turn tests over the restaurant fixture with scripted decisions; speculation hit/miss; loop protection and chain depth; barge-in truncation.

- [x] P07-T08c Deterministic `pathway_logs` — shared pure builder `va_domain.call_object.build_pathway_logs` used by `va_engine.logs.PathwayLogBook` and the Call Object projection; events stamped with `turn_index`/route `tag_color`; 2 syrupy snapshots + byte-stability test; make test 12158 passed, 7 skipped, 99 % coverage; lint/typecheck/placeholders/openapi drift/jscpd 0 clones pass
  - Spec refs: specs/11-pathways-engine.md §12, §14; specs/19-events-and-webhooks.md §4 (Call Object `pathway_logs`)
  - Depends on: P07-T08b
  - Deliverables: `va_engine.logs` (per assistant turn entry, `call.route` summary data with `turn_index`/`tag_color`), Call Object `decision` projection aligned with §12
  - Acceptance: syrupy snapshot of the restaurant fixture conversation; byte-stable across runs.

- [ ] P07-T09 Pathways API: CRUD, versions, publish, promote, folders, import/export (split into P07-T09a–c below; tick when all three are ticked)
  - Spec refs: specs/12-pathways-api.md (all endpoints)
  - Depends on: P07-T01, P02-T11
  - Deliverables: routers with legacy envelope, draft editing, version creation, publish/promote with validation, folders, duplicate, import/export, audit entries, cross-tenant factories
  - Acceptance: API tests for every endpoint and error path.

- [x] P07-T09a Pathways API core: create, list, get, update draft, delete with references, validate — make test 12195 passed/7 skipped, 99.07 % cov; lint/typecheck/placeholders/spec refs/openapi drift pass; jscpd 0 clones; radon A 2.63
  - Spec refs: specs/12-pathways-api.md §1, §2, §3, §10, §11, §12; specs/11-pathways-engine.md §11; specs/31-error-catalog.md §1, §3a; specs/04-auth-tenancy-rbac.md §2.3, §3.2
  - Depends on: P07-T01, P02-T11
  - Deliverables: `va_db.repositories.PathwayRepo` (tenant), `va_api.services.pathways` (graph parse/validate with org refs, draft update with optimistic concurrency, reference scan), legacy `pathways_router` (`POST /v1/pathway/create`, `GET /v1/pathway`, `GET|POST|DELETE /v1/pathway/{pathway_id}`, `POST /v1/pathway/{pathway_id}/validate`), audit entries, cross-tenant factory, role guard for analysts
  - Acceptance: API tests for every endpoint and error path (400 validation items, 409 hash conflict with `current_graph_hash`, 409 references + force, 404 other org), permissions matrix parametrized.

- [x] P07-T09b Pathway versions: list/get, save version, publish, promote, revert — make test 12219 passed/7 skipped, 99.08 % cov; lint/typecheck/placeholders/spec refs/openapi drift pass; jscpd 0 clones; radon A 1.85
  - Spec refs: specs/12-pathways-api.md §1, §4, §10, §11, §12 (version lifecycle, draft invariants)
  - Depends on: P07-T09a
  - Deliverables: version routes, publish in publish-mode validation (regression gating arrives with P07-T11), `pathway:{id}:v:{env}` cache invalidation, audit `pathway.version.save`/`pathway.publish`/`pathway.promote`/`pathway.revert`
  - Acceptance: lifecycle test create → edit → save → publish production → publish staging → promote → revert with exactly one draft at every step; publish validation 400; promote without staging 409.

- [x] P07-T09c Pathway folders, duplicate, import/export — folder CRUD with locked-tree depth/cycle checks, delete re-parenting, move; import (ours + Bland, KB/tool warnings), export round trip by graph_hash, duplicate any version; specs/12 §6 Decision; make test 12255 passed 99.08 %, lint/typecheck/placeholders/jscpd 0/spec refs/openapi drift pass
  - Spec refs: specs/12-pathways-api.md §4 (duplicate), §5, §6, §11, §12
  - Depends on: P07-T09b
  - Deliverables: folder CRUD + move (depth ≤ 5, delete moves pathways to parent), duplicate, export (`voiceagents.pathway.v1`), import (Bland aliases, missing tool/KB warnings), audit entries
  - Acceptance: Bland fixture import with aliases; export → import round trip keeps the graph hash; folder depth/delete/move tests; cross-tenant factories.

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
