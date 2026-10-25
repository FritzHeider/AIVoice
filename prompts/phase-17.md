## Phase 17 — Dashboard II: Pathway Editor, Testing, Agents, Content, Campaigns, Analytics

### Goal
Ship the building half of the dashboard: the React Flow Conversational Pathways editor (palette with every node type,
per-type config panels with every field, edges with labels, global nodes, auto-layout, undo/redo, copy/paste, versions,
publish/promote, validation panel), the test panel (text chat, browser voice test via the widget WebSocket, expanded
per-turn logs), unit test and simulation tabs, personas, agents with versions/branches/pins/experiments, tools editor
with test runs, knowledge bases, voice library and clone flow, batches with CSV mapping and scheduling, SMS inbox,
widgets configuration with live preview, and analytics pages.

### Read first
- `specs/25-dashboard.md` — pathway editor and remaining pages.
- `specs/11-pathways-engine.md` §3 — every node field the panels must edit.
- `specs/12-pathways-api.md` — draft/version/publish/test endpoints used by the editor.
- `specs/22-web-widget.md` — voice test uses the widget voice protocol.

### Phase exit gate
`make check && make e2e-ui` pass, including a Playwright scenario that builds the restaurant pathway from scratch in the
editor, publishes it, and passes a text test chat.
