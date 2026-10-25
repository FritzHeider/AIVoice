# Phase 16 — Dashboard I

- [ ] P16-T01 App shell, navigation, design system
  - Spec refs: specs/25-dashboard.md (route map, layout/nav, design tokens, component inventory, responsive rules)
  - Depends on: P00-T13
  - Deliverables: authenticated layout, sidebar with RBAC-aware items, org switcher, command palette, toasts, error boundary, empty/loading states, shared components from the inventory
  - Acceptance: component tests; axe clean.

- [ ] P16-T02 Typed API client and data layer
  - Spec refs: specs/25-dashboard.md (state management, API client generation, error/toast patterns); specs/26-sdks-cli-mcp-docs.md (OpenAPI generation rules)
  - Depends on: P16-T01
  - Deliverables: `openapi-typescript` generation script, `openapi-fetch` client with CSRF header and session cookies, TanStack Query hooks per resource, error normalization to toasts, SSE hook
  - Acceptance: unit tests with MSW mocks.

- [ ] P16-T03 Auth pages
  - Spec refs: specs/25-dashboard.md (auth pages, onboarding wizard); specs/04-auth-tenancy-rbac.md (dashboard auth, MFA, OIDC)
  - Depends on: P16-T02, P02-T07
  - Deliverables: signup, verify email, login (password + SSO), MFA challenge/enrollment, password reset, invitation acceptance, onboarding wizard (first number, first call)
  - Acceptance: Playwright flows against the e2e stack with Mailpit.

- [ ] P16-T04 Home KPIs
  - Spec refs: specs/25-dashboard.md (Home); specs/27-observability.md (analytics endpoints)
  - Depends on: P16-T02, P15-T08
  - Deliverables: KPI cards, volume and latency charts, recent calls
  - Acceptance: component tests with fixture data.

- [ ] P16-T05 Calls list
  - Spec refs: specs/25-dashboard.md (Calls list with filters)
  - Depends on: P16-T02
  - Deliverables: virtualized table, filters mirroring API, saved views, CSV export
  - Acceptance: Playwright filter scenarios.

- [ ] P16-T06 Call detail
  - Spec refs: specs/25-dashboard.md (Call detail)
  - Depends on: P16-T05
  - Deliverables: wavesurfer dual-channel player synced to transcript, pathway_logs timeline, latency per turn chart, events list, analysis/citations/evals panels, recording download, re-run analysis
  - Acceptance: Playwright verifies transcript click seeks audio; axe clean.

- [ ] P16-T07 Live calls
  - Spec refs: specs/25-dashboard.md (Live calls); specs/19-events-and-webhooks.md §9
  - Depends on: P16-T06, P06-T06
  - Deliverables: active calls list, live transcript via WS, stop/transfer/takeover actions
  - Acceptance: Playwright against a simulated in-progress call.

- [ ] P16-T08 Send Call form
  - Spec refs: specs/25-dashboard.md (Send Call form); specs/05-calls-api.md (POST /v1/calls parameters)
  - Depends on: P16-T02
  - Deliverables: grouped form for every parameter with zod validation mirroring API, JSON preview, code snippet generator (curl, Python SDK, TS SDK)
  - Acceptance: form-to-request snapshot tests for representative inputs.

- [ ] P16-T09 Phone numbers pages
  - Spec refs: specs/25-dashboard.md (Phone numbers); specs/16-phone-numbers.md
  - Depends on: P16-T02, P10-T05
  - Deliverables: search/buy, inbound config form (reuses Send Call field components), labels, release, pools, BYO Telnyx account management
  - Acceptance: Playwright purchase + configure flow.

- [ ] P16-T10 Settings and audit log
  - Spec refs: specs/25-dashboard.md (Settings, Audit log); specs/04-auth-tenancy-rbac.md
  - Depends on: P16-T02
  - Deliverables: org profile, members/invitations/roles, API keys (create shows secret once), SSO, MFA, compliance settings, DNC management, retention, audit log viewer
  - Acceptance: Playwright RBAC tests (viewer cannot see API keys page actions).

- [ ] P16-T11 Billing pages
  - Spec refs: specs/25-dashboard.md (Billing); specs/24-billing-and-limits.md (usage API)
  - Depends on: P16-T02, P15-T04
  - Deliverables: balance, top-up (Checkout redirect), auto-recharge, plan change, invoices, usage charts
  - Acceptance: Playwright with FakeStripe.

- [ ] P16-T12 Webhooks pages
  - Spec refs: specs/25-dashboard.md (Webhooks); specs/19-events-and-webhooks.md (webhook endpoint management API)
  - Depends on: P16-T02, P06-T04
  - Deliverables: endpoints CRUD, test send, deliveries table with filters, replay, signing secret rotate
  - Acceptance: Playwright flow.

- [ ] P16-T13 Lighthouse CI and a11y sweep
  - Spec refs: specs/25-dashboard.md (accessibility, performance budgets)
  - Depends on: P16-T06, P16-T05
  - Deliverables: `lighthouserc.json`, CI step, axe assertions in all Playwright specs of this phase
  - Acceptance: budgets met.

## Phase exit gate
`make check && make e2e-ui` pass; Lighthouse thresholds met.
