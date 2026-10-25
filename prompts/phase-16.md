## Phase 16 — Dashboard I: Shell, Auth, Calls, Live Calls, Send Call, Numbers, Settings, Billing, Webhooks

### Goal
Ship the operational half of the Next.js dashboard against the real API: app shell and navigation, auth flows (signup,
login, MFA, SSO, reset), org switching, generated typed API client, Home KPIs, call list and call detail (dual-channel
waveform synced to transcript, pathway logs timeline, latency chart, events, analysis, citations, evals, recording),
live calls (real-time transcript, stop, transfer, takeover), Send Call form with every parameter, phone numbers (search,
buy, inbound configuration, pools, BYO Telnyx), settings (org, members, roles, API keys, SSO, MFA, compliance, DNC,
retention), audit log, billing pages, and webhooks pages.

### Read first
- `specs/25-dashboard.md` — entire file (route map, page specs, component inventory, a11y, performance budgets).
- `specs/26-sdks-cli-mcp-docs.md` — OpenAPI generation rules (types consumed by the dashboard).
- `specs/04-auth-tenancy-rbac.md` — session, CSRF, MFA, SSO flows the UI drives.
- `specs/19-events-and-webhooks.md` §9 — live streams the UI consumes.

### Phase-specific constraints
- Every page enforces RBAC in the UI (hide/disable) and relies on the API for authorization.
- WCAG 2.1 AA: axe checks in Playwright for every page added.
- No hand-written API types: use `openapi-typescript` output.

### Phase exit gate
`make check && make e2e-ui` pass; Lighthouse CI (performance ≥ 85, accessibility ≥ 95) on the calls list and call detail
pages in the e2e stack.
