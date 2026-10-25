# Phase 13 — Memory, Personas, Agents, Experiments

- [ ] P13-T01 Contacts and memory context injection
  - Spec refs: specs/20-memory-personas-agents.md (contact resolution, memory scope, injection format)
  - Depends on: P07-T12
  - Deliverables: contact upsert/resolve by phone/email/external_id with conflict handling, memory load in prefetch, prompt injection block, `memory_id` on calls
  - Acceptance: tests for resolution precedence and conflict event.

- [ ] P13-T02 Memory update after calls and conversations
  - Spec refs: specs/20-memory-personas-agents.md (update prompt, memory_changes)
  - Depends on: P13-T01, P12-T09
  - Deliverables: updater (LLM extraction → merge facts/summary/open items/recent messages), `memory_changes` rows, embedding of summary, new `update_memory` step inserted in PostCallWorkflow before the final webhook and in the conversation close path
  - Acceptance: FakeLLM tests for merge semantics and change log.

- [ ] P13-T03 Memory API
  - Spec refs: specs/20-memory-personas-agents.md (memory API endpoints, legacy add-call)
  - Depends on: P13-T02
  - Deliverables: create/get, recent messages, update summary, update facts, context preview, changes per call, reset, enable/disable, legacy `/v1/memory/{id}/add-call`
  - Acceptance: API tests and cross-tenant registration.

- [ ] P13-T04 Personas
  - Spec refs: specs/20-memory-personas-agents.md §3 and personas API/precedence
  - Depends on: P07-T09
  - Deliverables: persona CRUD, versions (draft/production), PersonaConfig validation, precedence resolver for versioned vs legacy presets
  - Acceptance: precedence table tests exactly matching spec.

- [ ] P13-T05 Agents, versions, branches, env pins
  - Spec refs: specs/20-memory-personas-agents.md (agents, version resolution algorithm, API)
  - Depends on: P07-T09
  - Deliverables: agent CRUD, version create (semver validation), branches, env pins, resolver for production/staging/dev/latest/semver/branch:<name>, `AGENT_REQUIRES_ORG_KEY`
  - Acceptance: resolver tests for every selector form.

- [ ] P13-T06 Experiments
  - Spec refs: specs/20-memory-personas-agents.md (experiments: assignment, metrics, statistics, lifecycle)
  - Depends on: P13-T05, P06-T05
  - Deliverables: experiment CRUD, arms with weights summing to 100, deterministic assignment, metrics queries, two-proportion z-test, stop/promote winner, feature gate
  - Acceptance: assignment distribution test (10 000 ids within ±2 %); statistics against known values.

- [ ] P13-T07 Send Call integration for persona and agent runners
  - Spec refs: specs/05-calls-api.md (runner selection, precedence); specs/20-memory-personas-agents.md (precedence)
  - Depends on: P13-T04, P13-T05, P13-T06
  - Deliverables: resolution in `resolve_call_config`, experiment routing when `agent_version` omitted, call fields populated, inbound config support
  - Acceptance: e2e experiment_assignment passes.

- [ ] P13-T08 E2E: memory across calls
  - Spec refs: specs/30-testing-and-evals.md (end-to-end flow list: memory_across_calls)
  - Depends on: P13-T02, P13-T07
  - Deliverables: `tests_e2e/test_memory_across_calls.py`
  - Acceptance: second call's LLM request contains facts learned in the first.

## Phase exit gate
`make check && make e2e` pass.
