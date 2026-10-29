# Phase 02 — Authentication, Tenancy, RBAC, and API Foundations

- [x] P02-T01 API key format, hashing, and auth dependency — reviewer re-ran lint/typecheck/placeholders/test (1914 passed, 99.83% cov); Bearer/bare/x-api-key ignored, one hash per attempt, 60 s cache + touch gate, require_scope, specs 02/03/04 decisions
  - Spec refs: specs/04-auth-tenancy-rbac.md (API key format, key auth middleware, scopes); specs/02-repo-layout-and-conventions.md (Bland-compatibility conventions)
  - Depends on: P01-T10
  - Deliverables: `va_api.auth.keys` (generate, hash with pepper, parse prefix), `get_principal` dependency accepting `Authorization: Bearer <key>` and bare key, ignoring `x-api-key`, `Principal` (org_id, actor, scopes, role), `require_scopes(...)`, `last_used_at` throttled updates (at most once per minute per key via Redis)
  - Acceptance: tests for both header styles, revoked/expired keys, wrong prefix, constant-time comparison usage, scope denial → `FORBIDDEN`.

- [x] P02-T02 Request rate limiting — Lua token bucket (va_common.token_bucket) + va_api.rate_limit, X-RateLimit-* headers (spec wins over plan's RateLimit-*), 100-parallel test exact 20/80; make lint/typecheck/placeholders green, make test 1978 passed (99 % cov)
  - Spec refs: specs/04-auth-tenancy-rbac.md (request rate limits per key); specs/03-data-model.md (Redis keyspace `rl:*`)
  - Depends on: P02-T01
  - Deliverables: Redis Lua token bucket, per-plan defaults, `RateLimit-Limit/Remaining/Reset` + `Retry-After` headers, `RATE_LIMITED` errors
  - Acceptance: deterministic tests with injected clock; concurrency test (100 parallel requests) never exceeds bucket.

- [x] P02-T03 Idempotency-Key middleware — claim in require_scope + capture middleware + IdempotencyRepo; 2041 passed, 99.80 % cov, lint/mypy (246)/placeholders green; specs/04 §12 Decision
  - Spec refs: specs/03-data-model.md (`idempotency_keys`); specs/31-error-catalog.md (`IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_IN_PROGRESS`)
  - Depends on: P02-T01
  - Deliverables: dependency/middleware storing request hash, lock, response replay for 24 h, cleanup job function
  - Acceptance: replay returns identical status/body; different body → 422 code; concurrent duplicate → 409.

- [ ] P02-T04 Dashboard accounts: signup, verification, login, sessions, CSRF (split by the planner on 2026-10-29 into T04a–T04c below; tick when all three are ticked)
  - Spec refs: specs/04-auth-tenancy-rbac.md (dashboard auth, sessions, CSRF); specs/28-security.md (password hashing, cookies)
  - Depends on: P01-T10
  - Deliverables: `/internal/auth/signup`, `/verify-email`, `/login`, `/logout`, `/session`, argon2id params, session cookie, sliding expiry, CSRF double-submit for cookie-authenticated mutations, `EmailSender` protocol + SMTP + in-memory implementations, email templates
  - Acceptance: full flow tests; CSRF missing → 403; expired session → 401.

- [x] P02-T04a Password, token, and email primitives — review 2026-10-29: lint, typecheck (261 files), placeholders 0, make test 2124 passed, coverage 99.81 %; argon2id spec params, breached list (96 517 entries, pinned SecLists sha), opaque tokens, SMTP + in-memory email senders
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.2 (argon2id params, password policy, breached list); specs/02-repo-layout-and-conventions.md §6 (`SMTP_URL`, `EMAIL_FROM`); specs/28-security.md §2
  - Depends on: P01-T10
  - Deliverables: `va_common.auth` package (api key module moved, `passwords`, `tokens`, bundled `data/common_passwords.txt.gz`), `scripts/build_password_list.py`, `va_common.email` (`EmailSender` protocol, `SmtpEmailSender` on aiosmtplib with timeouts, `InMemoryEmailSender`, `EmailSettings`, `verify_email` template)
  - Acceptance: argon2id parameters exactly as specs/04 §4.2 and rehash detection; policy rejects short/long/breached/email-local-part passwords; SMTP sender delivers to an aiosmtpd test server; in-memory sender records messages.

- [ ] P02-T04b Identity repositories and org provisioning
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.1 (signup creates org/user/membership), §4.3; specs/28-security.md §3 (data key on org creation); specs/03-data-model.md §2, §3 (`users`, `sessions`, `email_tokens`, `org_data_keys`)
  - Depends on: P02-T04a
  - Deliverables: va_db access path for the global identity tables, `org_data_keys`-backed `DataKeyStore`, identity repository (users, sessions, email tokens), org provisioning service (org + active data key + encrypted webhook signing secret + owner membership in one transaction), `Envelope` in the api container
  - Acceptance: provisioning is atomic (failure leaves no rows); webhook secret decrypts with its AAD; session/email-token rows store only hashes; RLS still denies tenant tables without `app.org_id`.

- [ ] P02-T04c Dashboard auth endpoints, session principal, CSRF
  - Spec refs: specs/04-auth-tenancy-rbac.md §1, §4.1 (signup, verify-email, resend-verification, login, logout, logout-all, session), §4.2 (login throttling), §4.3, §4.4; specs/03-data-model.md §11 (`sess:*`, `rl:user:*`); specs/28-security.md §5
  - Depends on: P02-T04b
  - Deliverables: `/internal/auth/*` routers listed above, session-cookie principal in `get_principal` with sliding expiry, absolute max, `sess:` cache, CSRF double-submit + Origin check, `MFA_REQUIRED` gating, `AUTH_LOCKED` throttling
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
