# Phase 17 — Dashboard II

- [ ] P17-T01 Pathways list and folders
  - Spec refs: specs/25-dashboard.md (Pathways list); specs/12-pathways-api.md (folders, duplicate, import/export)
  - Depends on: P16-T02
  - Deliverables: list with folders tree, search, duplicate, import/export JSON, delete
  - Acceptance: Playwright flow.

- [ ] P17-T02 Editor canvas
  - Spec refs: specs/25-dashboard.md (pathway editor: canvas, palette, edges, auto-layout, minimap, undo/redo, copy/paste)
  - Depends on: P17-T01
  - Deliverables: `@xyflow/react` canvas, node components per type with badges (start/global), edge label editor, elkjs auto-layout, minimap, history stack, clipboard, keyboard shortcuts, autosave of draft with conflict detection (graph_hash)
  - Acceptance: component tests for history and clipboard; Playwright drag/connect.

- [ ] P17-T03 Node configuration panels
  - Spec refs: specs/25-dashboard.md (node config side panels); specs/11-pathways-engine.md §3
  - Depends on: P17-T02
  - Deliverables: a panel per node type editing every field (prompt/static text, condition, examples, model options, extract vars, webhook/tool config, transfer settings incl. warm, SMS, press button, transfer pathway, scheduling, custom code editor with Monaco, end call speech modes, global label/forwarding), zod schemas generated from OpenAPI component schemas
  - Acceptance: test asserts every field in specs/11 §3 has a control (schema-driven coverage test).

- [ ] P17-T04 Versions, publish, promote, validation panel
  - Spec refs: specs/25-dashboard.md (version switcher, publish/promote dialogs, validation panel); specs/12-pathways-api.md
  - Depends on: P17-T02
  - Deliverables: version switcher, diff summary, publish with regression results, promote staging→production, validation issues list linking to nodes
  - Acceptance: Playwright publish flow incl. blocked publish on failing regression.

- [ ] P17-T05 Test panel
  - Spec refs: specs/25-dashboard.md (test panel); specs/12-pathways-api.md (test chat); specs/22-web-widget.md (voice mode)
  - Depends on: P17-T04, P14-T09
  - Deliverables: text chat with branching, browser voice test (AudioWorklet), per-turn expanded logs (extraction, loop condition, route alternatives, webhook payloads), highlight active node on canvas
  - Acceptance: Playwright text chat test.

- [ ] P17-T06 Unit tests and simulations tabs
  - Spec refs: specs/25-dashboard.md (unit tests tab, simulations tab); specs/12-pathways-api.md (unit tests, simulations)
  - Depends on: P17-T05
  - Deliverables: create test from a call turn, run, results with rationale, simulation personas and runs
  - Acceptance: Playwright run of a unit test.

- [ ] P17-T07 Tools, knowledge bases, voices
  - Spec refs: specs/25-dashboard.md (Tools editor, Knowledge bases, Voices)
  - Depends on: P16-T02, P08-T09, P09-T03
  - Deliverables: tool editor with schema builder and test-run, KB upload/crawl/text with status, KB chat tester, voice library with filters and previews, clone flow with in-browser recorder and uploads
  - Acceptance: Playwright flows for each.

- [ ] P17-T08 Personas, agents, experiments
  - Spec refs: specs/25-dashboard.md (Personas, Agents, experiments UI)
  - Depends on: P16-T02, P13-T06
  - Deliverables: persona editor with versions, agent versions/branches/env pins, experiment setup and results with significance display
  - Acceptance: Playwright flows.

- [ ] P17-T09 Batches, SMS inbox, widgets
  - Spec refs: specs/25-dashboard.md (Batches, SMS conversations inbox, Widgets)
  - Depends on: P16-T02, P11-T06, P14-T04, P14-T07
  - Deliverables: CSV upload with column mapping and validation preview, scheduling form, progress view; SMS inbox with takeover; widget config with embed snippet and live preview iframe
  - Acceptance: Playwright flows.

- [ ] P17-T10 Analytics pages
  - Spec refs: specs/25-dashboard.md (Analytics); specs/27-observability.md (analytics endpoints)
  - Depends on: P15-T08, P16-T02
  - Deliverables: latency p50/p95 by stage, dispositions, volumes, pathway node funnels, experiment comparisons, date range filters
  - Acceptance: component tests with fixtures.

- [ ] P17-T11 E2E: build restaurant pathway in the editor
  - Spec refs: specs/25-dashboard.md (Playwright e2e scenarios list)
  - Depends on: P17-T03, P17-T04, P17-T05
  - Deliverables: Playwright scenario building, publishing, and testing the restaurant pathway
  - Acceptance: scenario green.

## Phase exit gate
`make check && make e2e-ui` pass.
