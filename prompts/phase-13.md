## Phase 13 — Memory, Personas, Agents with Versions/Branches, and A/B Experiments

### Goal
Make agents reusable and stateful: contact resolution and memory (facts, summary, open items, recent messages) injected
into calls and updated afterwards; personas with versions and precedence rules; agents with semver versions, branches,
environment pins and the `agent_version` resolution algorithm; experiments with deterministic weighted assignment,
metrics, significance testing, stop and promote; and Send Call integration for `persona_id` and `agent_id` runners.

### Read first
- `specs/20-memory-personas-agents.md` — entire file (§3 PersonaConfig).
- `specs/05-calls-api.md` — runner selection and precedence rules for persona/agent.
- `specs/18-post-call-analysis.md` — memory update hook.
- `specs/27-observability.md` — ClickHouse analytics queries used by experiments.

### Phase exit gate
`make check && make e2e` pass including memory_across_calls and experiment_assignment.
