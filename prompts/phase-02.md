## Phase 02 — Authentication, Tenancy, RBAC, and API Foundations

### Goal
Make the API multi-tenant and secure: API-key authentication (Bearer and bare), scopes, per-key rate limiting,
idempotency, dashboard user accounts with sessions/CSRF/MFA/SSO, orgs and sub-accounts, memberships and invitations,
the RBAC matrix, API key management, audit logging, org settings, the agent-onboarding key API, dev seeding, and an
automatic cross-tenant test suite that covers every route added now and in later phases.

### Read first
- `specs/04-auth-tenancy-rbac.md` — entire file.
- `specs/31-error-catalog.md` — envelopes and codes used by auth (`AUTH_FAILURE`, `MFA_REQUIRED`, `FORBIDDEN`,
  `ORG_SUSPENDED`, `RATE_LIMITED`, `IDEMPOTENCY_*`).
- `specs/03-data-model.md` §3 (tables) and §11 (Redis keys `rl:*`, `sess:*`).
- `specs/28-security.md` — password hashing, session cookies, CSRF, secret handling.
- `specs/02-repo-layout-and-conventions.md` §10 (Bland auth compatibility).

### In scope
Everything in specs/04 including: key format/hashing/pepper, auth dependency and principal model, scope checks,
token-bucket rate limiting with headers, `Idempotency-Key` middleware with request hashing and locking, dashboard
signup/verify/login/logout, sessions with sliding expiry, CSRF double-submit, password reset, SMTP email sender with
templates, TOTP MFA + recovery codes, OIDC SSO (authlib) with JIT provisioning and enforcement, `/v1/me`, `/v1/org`,
members, invitations, role changes, sub-accounts (`POST /v1/orgs/create` Bland-compatible), org switching,
`/v1/api_keys` CRUD, audit log writer + `/v1/audit_logs`, `OrgSettings` + `PATCH /v1/org/settings`, agent onboarding
keys, `scripts/seed_dev.py`, cross-tenant route fuzz suite.

### Phase-specific constraints
- Never store raw keys or tokens; compare hashes in constant time.
- The cross-tenant suite must enumerate FastAPI routes automatically so that every later router is covered without
  editing the suite (routes opt out only with an explicit `x-public` marker, e.g. health, widget session, Stripe webhook).
- All emails are sent through an interface with an SMTP implementation and an in-memory implementation for tests;
  Mailpit receives them in local dev.

### Phase exit gate
`make check` passes; manual smoke documented in AGENTS.md: `make seed` prints a working key; `curl -H "Authorization: <key>" localhost:8000/v1/me` returns the org.
