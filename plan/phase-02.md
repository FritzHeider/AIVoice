# Phase 02 — Authentication, Tenancy, RBAC, and API Foundations

- [ ] P02-T01 API key format, hashing, and auth dependency
  - Spec refs: specs/04-auth-tenancy-rbac.md (API key format, key auth middleware, scopes); specs/02-repo-layout-and-conventions.md (Bland-compatibility conventions)
  - Depends on: P01-T10
  - Deliverables: `va_api.auth.keys` (generate, hash with pepper, parse prefix), `get_principal` dependency accepting `Authorization: Bearer <key>` and bare key, ignoring `x-api-key`, `Principal` (org_id, actor, scopes, role), `require_scopes(...)`, `last_used_at` throttled updates (at most once per minute per key via Redis)
  - Acceptance: tests for both header styles, revoked/expired keys, wrong prefix, constant-time comparison usage, scope denial → `FORBIDDEN`.

- [ ] P02-T02 Request rate limiting
  - Spec refs: specs/04-auth-tenancy-rbac.md (request rate limits per key); specs/03-data-model.md (Redis keyspace `rl:*`)
  - Depends on: P02-T01
  - Deliverables: Redis Lua token bucket, per-plan defaults, `RateLimit-Limit/Remaining/Reset` + `Retry-After` headers, `RATE_LIMITED` errors
  - Acceptance: deterministic tests with injected clock; concurrency test (100 parallel requests) never exceeds bucket.

- [ ] P02-T03 Idempotency-Key middleware
  - Spec refs: specs/03-data-model.md (`idempotency_keys`); specs/31-error-catalog.md (`IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_IN_PROGRESS`)
  - Depends on: P02-T01
  - Deliverables: dependency/middleware storing request hash, lock, response replay for 24 h, cleanup job function
  - Acceptance: replay returns identical status/body; different body → 422 code; concurrent duplicate → 409.

- [ ] P02-T04 Dashboard accounts: signup, verification, login, sessions, CSRF
  - Spec refs: specs/04-auth-tenancy-rbac.md (dashboard auth, sessions, CSRF); specs/28-security.md (password hashing, cookies)
  - Depends on: P01-T10
  - Deliverables: `/internal/auth/signup`, `/verify-email`, `/login`, `/logout`, `/session`, argon2id params, session cookie, sliding expiry, CSRF double-submit for cookie-authenticated mutations, `EmailSender` protocol + SMTP + in-memory implementations, email templates
  - Acceptance: full flow tests; CSRF missing → 403; expired session → 401.

- [ ] P02-T05 Password reset and email change
  - Spec refs: specs/04-auth-tenancy-rbac.md (password reset)
  - Depends on: P02-T04
  - Deliverables: request/confirm endpoints, single-use hashed tokens with expiry, session revocation on reset
  - Acceptance: token reuse rejected; all sessions revoked after reset.

- [ ] P02-T06 TOTP MFA and recovery codes
  - Spec refs: specs/04-auth-tenancy-rbac.md (MFA)
  - Depends on: P02-T04
  - Deliverables: enroll (secret encrypted with va_common.crypto, otpauth URI), verify, disable, recovery codes (10, hashed), `MFA_REQUIRED` enforcement when org `mfa_required`
  - Acceptance: pyotp-based tests with frozen clock; recovery code single-use.

- [ ] P02-T07 OIDC SSO
  - Spec refs: specs/04-auth-tenancy-rbac.md (OIDC SSO)
  - Depends on: P02-T04
  - Deliverables: SSO connection CRUD (owner/admin), login start/callback with authlib, state/nonce validation, JIT provisioning with `default_role`, domain restriction, enforce mode
  - Acceptance: tests against an in-process fake OIDC provider (JWKS, token, userinfo endpoints) including bad nonce/issuer cases.

- [ ] P02-T08 Orgs, members, invitations, RBAC matrix
  - Spec refs: specs/04-auth-tenancy-rbac.md (RBAC matrix, endpoints, invitations, org switching)
  - Depends on: P02-T01, P02-T04
  - Deliverables: `/v1/me`, `/v1/org` (GET/PATCH), `/v1/org/members` (list, change role, remove), invitations (create, list, revoke, accept), `require_permission(resource, action)` driven by a single matrix table in code that is tested against the spec matrix
  - Acceptance: test parses the RBAC table from specs/04 and asserts the code matrix matches cell-for-cell; last-owner protection.

- [ ] P02-T09 Sub-accounts and org switching
  - Spec refs: specs/04-auth-tenancy-rbac.md (sub-accounts)
  - Depends on: P02-T08
  - Deliverables: `POST /v1/orgs/create` (Bland-compatible response with `org_plan`, `kyc_level`), list children, parent-admin access to child via `X-VA-Org-Id` header with authorization check, session active org switching
  - Acceptance: parent can act on child; child cannot act on parent or sibling; RLS still enforced (session uses child org id).

- [ ] P02-T10 API key management endpoints
  - Spec refs: specs/04-auth-tenancy-rbac.md (endpoints: /v1/api_keys)
  - Depends on: P02-T08
  - Deliverables: create (secret shown once), list (prefix only), update name/scopes, revoke, rotate
  - Acceptance: secret never returned after creation; audit entries written.

- [ ] P02-T11 Audit logging
  - Spec refs: specs/04-auth-tenancy-rbac.md (audit logging rules); specs/28-security.md (audit)
  - Depends on: P02-T08
  - Deliverables: `AuditWriter` with redaction of secret fields, decorator/helper for mutating endpoints, `/v1/audit_logs` with filters and pagination
  - Acceptance: every mutating endpoint added in this phase produces an audit row (test enumerates them).

- [ ] P02-T12 Org settings
  - Spec refs: specs/04-auth-tenancy-rbac.md (OrgSettings model)
  - Depends on: P02-T08
  - Deliverables: `OrgSettings` pydantic model (every field), `GET/PATCH /v1/org/settings` with validation and role checks, accessor used by other services with Redis caching (60 s) and invalidation on update
  - Acceptance: defaults match spec; invalid values rejected with field paths.

- [ ] P02-T13 Agent onboarding API
  - Spec refs: specs/04-auth-tenancy-rbac.md (agent onboarding API)
  - Depends on: P02-T10
  - Deliverables: `POST /v1/agent-onboarding/keys` flow (email verification, scoped test keys, limits)
  - Acceptance: tests for the full flow and abuse limits.

- [ ] P02-T14 Dev seed script
  - Spec refs: specs/02-repo-layout-and-conventions.md (`scripts/seed_dev.py`)
  - Depends on: P02-T10, P01-T11
  - Deliverables: idempotent seeding of a dev org (build plan, $50 credits), owner user (`dev@voiceagents.local` / generated password printed once), live+test API keys printed, default org settings
  - Acceptance: running twice does not duplicate; output parsable (JSON with `--json`).

- [ ] P02-T15 Cross-tenant route fuzz suite
  - Spec refs: specs/00-overview.md (Non-negotiable rule 7); specs/04-auth-tenancy-rbac.md (tenant isolation tests)
  - Depends on: P02-T09
  - Deliverables: `apps/api/tests/test_cross_tenant_routes.py` enumerating routes, creating resources as org A via registered factories, calling every route with org B credentials, asserting 404/403; registry API `register_resource_factory(path_param, factory)` for later phases
  - Acceptance: suite passes and fails if any route lacks org scoping (negative test with a deliberately unscoped test-only route).

## Phase exit gate
`make check` passes; seeded key authenticates against `/v1/me`.
