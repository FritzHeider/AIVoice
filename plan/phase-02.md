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

- [x] P02-T04 Dashboard accounts: signup, verification, login, sessions, CSRF (split by the planner on 2026-10-29 into T04a–T04d below; tick when all four are ticked) — all four subtasks approved 2026-10-29
  - Spec refs: specs/04-auth-tenancy-rbac.md (dashboard auth, sessions, CSRF); specs/28-security.md (password hashing, cookies)
  - Depends on: P01-T10
  - Deliverables: `/internal/auth/signup`, `/verify-email`, `/login`, `/logout`, `/session`, argon2id params, session cookie, sliding expiry, CSRF double-submit for cookie-authenticated mutations, `EmailSender` protocol + SMTP + in-memory implementations, email templates
  - Acceptance: full flow tests; CSRF missing → 403; expired session → 401.

- [x] P02-T04a Password, token, and email primitives — review 2026-10-29: lint, typecheck (261 files), placeholders 0, make test 2124 passed, coverage 99.81 %; argon2id spec params, breached list (96 517 entries, pinned SecLists sha), opaque tokens, SMTP + in-memory email senders
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.2 (argon2id params, password policy, breached list); specs/02-repo-layout-and-conventions.md §6 (`SMTP_URL`, `EMAIL_FROM`); specs/28-security.md §2
  - Depends on: P01-T10
  - Deliverables: `va_common.auth` package (api key module moved, `passwords`, `tokens`, bundled `data/common_passwords.txt.gz`), `scripts/build_password_list.py`, `va_common.email` (`EmailSender` protocol, `SmtpEmailSender` on aiosmtplib with timeouts, `InMemoryEmailSender`, `EmailSettings`, `verify_email` template)
  - Acceptance: argon2id parameters exactly as specs/04 §4.2 and rehash detection; policy rejects short/long/breached/email-local-part passwords; SMTP sender delivers to an aiosmtpd test server; in-memory sender records messages.

- [x] P02-T04b Identity repositories and org provisioning — global_session, IdentityRepo, MembershipLookupRepo/MembershipRepo, OrgDataKeyStore, atomic OrgProvisioner (data key + encrypted whsec_ secret + owner), Envelope+provisioner in api container; review re-run: lint/typecheck/placeholders 0, make test 2171 passed, coverage 99.82 %
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.1 (signup creates org/user/membership), §4.3; specs/28-security.md §3 (data key on org creation); specs/03-data-model.md §2, §3 (`users`, `sessions`, `email_tokens`, `org_data_keys`)
  - Depends on: P02-T04a
  - Deliverables: va_db access path for the global identity tables, `org_data_keys`-backed `DataKeyStore`, identity repository (users, sessions, email tokens), org provisioning service (org + active data key + encrypted webhook signing secret + owner membership in one transaction), `Envelope` in the api container
  - Acceptance: provisioning is atomic (failure leaves no rows); webhook secret decrypts with its AAD; session/email-token rows store only hashes; RLS still denies tenant tables without `app.org_id`.

- [x] P02-T04c Session authentication: login, logout, session endpoint, session principal, CSRF (split by the planner on 2026-10-29; signup/verification moved to T04d) — reviewer 2026-10-29: make lint/typecheck/placeholders 0, make test 2251 passed (99.71 %), openapi drift current, check_spec_refs 0, jscpd 0 clones, radon avg A (1.93)
  - Spec refs: specs/04-auth-tenancy-rbac.md §1, §3.3 (role → scope derivation), §4.1 (login, logout, logout-all, session), §4.2 (login throttling), §4.3, §4.4; specs/03-data-model.md §11 (`sess:*`, `rl:user:*`); specs/28-security.md §5
  - Depends on: P02-T04b
  - Deliverables: `va_api.auth.rbac.ROLE_SCOPES`, session manager (create, resolve with 14 d sliding expiry extended at most hourly, 90 d absolute cap, `sess:` cache, revoke), session-cookie principal in `get_principal`, `va_session`/`va_csrf` cookies, CSRF double-submit + Origin/Referer check, `MFA_REQUIRED` gating with the §4.3 exemptions, `AUTH_LOCKED` login throttling, `POST /internal/auth/login`, `/logout`, `/logout-all`, `GET /internal/auth/session`
  - Acceptance: full login → session → logout flow tests on Postgres; CSRF missing → 403; expired session → 401; sliding/absolute expiry with a frozen clock; lock after 10 failures with `Retry-After`.

- [x] P02-T04d Signup, email verification, resend, internal OpenAPI document — reviewer 2026-10-29: make lint/typecheck/placeholders 0, make test 2289 passed (99.55 %), openapi drift both current, check_spec_refs 0, jscpd 0 clones, radon nothing >= C in src
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.1 (signup, verify-email, resend-verification); specs/26-sdks-cli-mcp-docs.md §1 (`openapi/internal.json`, `SessionCookie` scheme); specs/31-error-catalog.md (`PASSWORD_TOO_WEAK`, `TOKEN_EXPIRED`, `EMAIL_NOT_VERIFIED`)
  - Depends on: P02-T04c
  - Deliverables: `POST /internal/auth/signup` (policy, provisioning + session + verify token in one transaction, verification email after commit), `/verify-email`, `/resend-verification` (3 per hour), `EmailSender` in the api container (SMTP; in-memory in tests), `openapi/internal.json` export + drift check
  - Acceptance: signup → email → verify → session flow test; weak password, duplicate email, reused/expired token, 4th resend rejected; internal document drift-checked.

- [x] P02-T05 Password reset and email change — forgot/reset/change endpoints (email change not in v1, decision in specs/04 §4.1); single-use hashed 1 h tokens peeked then consumed, reuse/expiry/purpose rejected; reset revokes all sessions + sess: keys; 2315 tests pass (99.39 %), lint/typecheck/placeholders/drift/spec-refs/jscpd 0/radon clean
  - Spec refs: specs/04-auth-tenancy-rbac.md (password reset)
  - Depends on: P02-T04
  - Deliverables: request/confirm endpoints, single-use hashed tokens with expiry, session revocation on reset
  - Acceptance: token reuse rejected; all sessions revoked after reset.

- [x] P02-T06 TOTP MFA and recovery codes (split by the planner on 2026-10-29 into T06a/T06b below; tick when both are ticked) — T06a and T06b reviewed 2026-10-29
  - Spec refs: specs/04-auth-tenancy-rbac.md (MFA)
  - Depends on: P02-T04
  - Deliverables: enroll (secret encrypted with va_common.crypto, otpauth URI), verify, disable, recovery codes (10, hashed), `MFA_REQUIRED` enforcement when org `mfa_required`
  - Acceptance: pyotp-based tests with frozen clock; recovery code single-use.

- [x] P02-T06a MFA primitives: TOTP, recovery codes, platform-key encryption, session MFA time — reviewed 2026-10-29: make lint/typecheck/placeholders 0, make test 2365 passed (99.39 %), spec refs 0, jscpd 0 clones
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.5; specs/28-security.md §3; specs/03-data-model.md §3 (`users`, `sessions`)
  - Depends on: P02-T04
  - Deliverables: `va_common.auth.mfa` (pyotp TOTP secret/URI/±1-step matching counter, segno QR SVG, `xxxx-xxxx-xxxx` recovery code generation/normalisation, argon2id hashing/matching off the event loop); `Envelope.platform_encrypt` with a fresh KMS data key per blob (the nil org has no `org_data_keys` row); migration 0003 adding `sessions.mfa_passed_at`; `IdentityRepo` MFA writes (setup, enable, disable, atomic recovery-code consumption, `set_mfa_passed(session_id, now)`)
  - Acceptance: frozen-clock TOTP tests at window edges; recovery code consumed at most once under concurrency (conditional `array_remove`); platform blob round-trips and is bound to its AAD; migrated schema equals the spec-applied schema.

- [x] P02-T06b MFA endpoints and enforcement — reviewed 2026-10-29: make lint/typecheck/placeholders 0, make test 2405 passed (99.30 %), openapi drift current, spec refs 0, jscpd 0 clones, radon -n C none
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.1 (`mfa/*` rows), §4.3, §4.5; specs/03-data-model.md §11; specs/31
  - Depends on: P02-T06a
  - Deliverables: `POST /internal/auth/mfa/setup|enable|verify|disable`, TOTP replay guard `mfa:last:<user_id>`, per-user MFA failure lock, `MFA_REQUIRED` gate for org `mfa_required` and for users with MFA enabled, MFA verification refreshing recent auth for password change, `GET /session` reporting it
  - Acceptance: pyotp-based tests with frozen clock; replayed code rejected; recovery code single-use; disable refused when any org of the user requires MFA.

- [x] P02-T07 OIDC SSO (split by the planner on 2026-10-29 into T07a/T07b below; tick when both are ticked) — T07a and T07b reviewed 2026-10-29
  - Spec refs: specs/04-auth-tenancy-rbac.md (OIDC SSO)
  - Depends on: P02-T04
  - Deliverables: SSO connection CRUD (owner/admin), login start/callback with authlib, state/nonce validation, JIT provisioning with `default_role`, domain restriction, enforce mode
  - Acceptance: tests against an in-process fake OIDC provider (JWKS, token, userinfo endpoints) including bad nonce/issuer cases.

- [x] P02-T07a SSO connection management: `/v1/org/sso`, `sso.manage` permission, OIDC discovery validation — reviewed 2026-10-29: lint/typecheck/placeholders 0, make test 2500 passed (99.31% coverage), test-ts pass, openapi drift current, spec refs 0, jscpd 0 clones, gitleaks passed
  - Spec refs: specs/04-auth-tenancy-rbac.md §3.2 (SSO connection row), §3.3 (`sso.manage`, session-only), §5 step 1, §8.1 (`/v1/org/sso`); specs/03-data-model.md §2 (`va_system` list), §3 `sso_connections`; specs/28-security.md §3 (envelope encryption), §6.4; specs/02 §6 `OIDC_ENABLED`
  - Depends on: P02-T04
  - Deliverables: `OIDC_ENABLED` setting; `va_api.auth.permissions` (`require_permission`, role/permission table, session-only permissions); `PUT/GET/DELETE /v1/org/sso` (envelope, owner/admin sessions only) with client secret envelope-encrypted and never returned; issuer discovery fetch (`{issuer}/.well-known/openid-configuration`, https, timeout, size cap, no redirects, non-global address refusal outside local/test); cross-org allowed-domain conflict check through a system-scope `SsoConnectionLookupRepo`; public OpenAPI regenerated
  - Acceptance: tests with respx-mocked discovery (good, wrong issuer, missing endpoints, non-JSON, timeout, redirect); `*` API key and non-admin roles get `FORBIDDEN`; secret encrypted with the column AAD; domain overlap with another org → `CONFLICT`; with `OIDC_ENABLED=false` every `/v1/org/sso` route fails with `FORBIDDEN` "SSO is not enabled on this deployment." (decision recorded in specs/04 §5).

- [x] P02-T07b SSO login: start, callback, JIT provisioning, enforce mode — 2579 passed (99.05% cov), lint/typecheck/placeholders clean, openapi drift current, spec refs 0, jscpd 0 clones, radon no C+; OIDC code+PKCE via authlib primitives, joserfc ID-token validation, va_sso state cookie, JIT provisioning, SSO_REQUIRED enforcement
  - Spec refs: specs/04-auth-tenancy-rbac.md §4.1 (`sso/start`, `sso/callback`, login `SSO_REQUIRED`), §4.2 login decision, §5 steps 2–6, §13 `auth.sso_login`, §14 SSO tests; specs/03-data-model.md §11 (`sso:state:{state}`); specs/31 `SSO_REQUIRED`
  - Depends on: P02-T07a
  - Deliverables: authlib dependency; `GET /internal/auth/sso/start?email=` (domain lookup, PKCE S256, state/nonce in Redis 10 min, redirect); `GET /internal/auth/sso/callback` (state single-use, code exchange, ID token signature via JWKS, `iss`/`aud`/`exp`/`nonce`, `email_verified` rule, JIT user/membership with `default_role`, session with `mfa_passed=true`, redirect to the dashboard); password login on an enforced domain → `SSO_REQUIRED` with `details.sso_url`
  - Acceptance: tests against an in-process fake OIDC provider (respx discovery/JWKS/token endpoints, generated RSA key) including bad state, reused state, bad nonce, wrong issuer, wrong audience, expired token, bad signature, unverified email; JIT paths (new user, existing user without membership, existing membership keeps role); enforce blocks password login while API keys still work.

- [x] P02-T08 Orgs, members, invitations, RBAC matrix (split by the planner on 2026-10-29 into T08a/T08b/T08c below; tick when all three are ticked)
  - Spec refs: specs/04-auth-tenancy-rbac.md (RBAC matrix, endpoints, invitations, org switching)
  - Depends on: P02-T01, P02-T04
  - Deliverables: `/v1/me`, `/v1/org` (GET/PATCH), `/v1/org/members` (list, change role, remove), invitations (create, list, revoke, accept), `require_permission(resource, action)` driven by a single matrix table in code that is tested against the spec matrix
  - Acceptance: test parses the RBAC table from specs/04 and asserts the code matrix matches cell-for-cell; last-owner protection.

- [x] P02-T08a RBAC matrix table, `/v1/me`, `GET/PATCH /v1/org` — reviewed 2026-10-29: make lint/typecheck/placeholders pass, make test 2682 passed (cov 99.00%), test-ts pass, openapi drift current, spec refs 0, jscpd 0 clones
  - Spec refs: specs/04-auth-tenancy-rbac.md §3.2 (matrix), §3.3 (role scopes/permissions decisions), §8.1 (`/v1/me`, `/v1/org`); specs/24-billing-and-limits.md §2 (plan caps, `orgs.limits` overrides)
  - Depends on: P02-T01, P02-T04
  - Deliverables: `va_api.auth.rbac_matrix` (the §3.2 table in code, cell-for-cell, plus each row's scope resource and permissions); `ROLE_SCOPES` and `ROLE_PERMISSIONS` derived from it (sets unchanged); `GET /v1/me` (user and API-key principals); `GET /v1/org` (org, effective limits, credits balance in dollars, stored settings); `PATCH /v1/org` (`name`; `retention_days`/`recording_retention_days` need `org.compliance`)
  - Acceptance: a test parses the §3.2 table from specs/04 and asserts the code matrix matches cell-for-cell; the existing exact role scope/permission tests stay green on the derived sets; `/v1/me` and `/v1/org` integration tests for sessions of every role and for API keys; scope/permission failures answer `FORBIDDEN`.

- [x] P02-T08b Members: list, change role, remove, transfer ownership — 4 routes + org-row-locked last-owner rule; reviewer re-run: lint/typecheck/placeholders pass, make test 2714 passed, coverage 98.91%, openapi drift current, spec refs 0
  - Spec refs: specs/04-auth-tenancy-rbac.md §6 (members, last-owner rule, transfer-ownership), §3.2 (`Members` rows, `Assign owner role / transfer ownership`), §4.3 (reauth)
  - Depends on: P02-T08a
  - Deliverables: `GET /v1/org/members`, `PATCH /v1/org/members/{user_id}`, `DELETE /v1/org/members/{user_id}`, `POST /v1/org/transfer-ownership` (owner session + recent auth); removed members' sessions lose the org
  - Acceptance: last-owner protection (`CONFLICT` "An organization must have at least one owner.") including concurrent demotions; only owners assign `owner`; admins cannot change owners; cross-org user ids answer `NOT_FOUND`.

- [x] P02-T08c Invitations: create, list, revoke, details, accept — 3 /v1/org/members/invitations routes + 2 internal invitation routes, invitation email, InvitationLookupRepo; reviewer re-run: lint/typecheck/placeholders pass, make test 2766 passed, coverage 98.58%, openapi drift current, spec refs 0
  - Spec refs: specs/04-auth-tenancy-rbac.md §6 (invitations), §4.1 (`/internal/auth/invitations/*`), §4.2 (token format); specs/28-security.md §2
  - Depends on: P02-T08b
  - Deliverables: `POST/GET /v1/org/members/invitations`, `DELETE /v1/org/members/invitations/{id}`, invitation email template with `{DASHBOARD_BASE_URL}/invite/{token}`, `GET /internal/auth/invitations/{token}`, `POST /internal/auth/invitations/accept` (creates the user when needed, adds the membership)
  - Acceptance: 7-day expiry, hashed single-use token, only owners invite owners, revoked/expired/accepted tokens refused, email mismatch rules, existing member → `CONFLICT`.

- [x] P02-T09 Sub-accounts and org switching (split by the planner on 2026-10-29 into T09a/T09b below; tick when both are ticked) — T09a and T09b approved
  - Spec refs: specs/04-auth-tenancy-rbac.md (sub-accounts)
  - Depends on: P02-T08
  - Deliverables: `POST /v1/orgs/create` (Bland-compatible response with `org_plan`, `kyc_level`), list children, parent-admin access to child via `X-VA-Org-Id` header with authorization check, session active org switching
  - Acceptance: parent can act on child; child cannot act on parent or sibling; RLS still enforced (session uses child org id).

- [x] P02-T09a Sub-account management: create (with child key and initial credit transfer), list, get, suspend, reactivate — 2811 tests pass, cov 98.45%, lint/typecheck/placeholders/openapi drift/spec refs/jscpd/radon/gitleaks clean; one-time api_key withheld from idempotency storage
  - Spec refs: specs/04-auth-tenancy-rbac.md §8.2, §8.4, §3.2 (`Sub-accounts` row, `subaccounts.manage`); specs/24-billing-and-limits.md §5, §9; specs/03-data-model.md §2, §3 (`orgs`, `api_keys`, `credit_ledger`)
  - Depends on: P02-T08
  - Deliverables: legacy `POST /v1/orgs/create`, `GET /v1/orgs`, `GET /v1/orgs/{org_id}`, `POST /v1/orgs/{org_id}/suspend`, `POST /v1/orgs/{org_id}/reactivate`; child `*` API key minted in the same transaction (plaintext returned once); `initial_credits` moved with `transfer_out`/`transfer_in` ledger rows and both org rows locked in id order (`va_db.repositories.credits.CreditTransferRepo`, reused by P15-T07); suspend invalidates the child's cached API keys
  - Acceptance: Bland-shaped bodies; nesting depth 1 (`FORBIDDEN`); insufficient parent balance → `INSUFFICIENT_CREDITS` and nothing created; ledger sums equal both balances; non-child ids → `NOT_FOUND`; developer/analyst/billing/viewer sessions and `org:read` keys refused; the minted key authenticates against the child.

- [x] P02-T09b Parent access to sub-accounts: `X-VA-Org-Id` and `switch-org` — 2862 tests pass, cov 98.46%, lint/typecheck/placeholders clean; parent status gates switched sessions (stricter of child/parent)
  - Spec refs: specs/04-auth-tenancy-rbac.md §1 (`acting_parent_org_id`), §4.1 (`switch-org`, session memberships), §8.3, §14 (sub-account tests)
  - Depends on: P02-T09a
  - Deliverables: `X-VA-Org-Id` handling in `get_principal` for API keys (`org:write` or `*`) and sessions (owner/admin) that rebuilds the `RequestContext` for a direct child (`acting_parent_org_id` set, scopes and role kept, child plan and status gates); `POST /internal/auth/switch-org` (membership, or owner/admin of the child's parent); the session's active org may be a child of a parent membership
  - Acceptance: parent can act on child; child cannot act on parent or sibling; a non-child id → `FORBIDDEN`; RLS still enforced (the session uses the child org id); suspended child refuses writes.

- [x] P02-T10 API key management endpoints — list/create(201)/PATCH/revoke/roll under `/v1/api_keys` (api_keys:write), grantable-scope, test-env and developer-own rules, cache drop on revoke/roll, plaintext withheld on replay; 2900 passed, coverage 98.25%
  - Spec refs: specs/04-auth-tenancy-rbac.md (endpoints: /v1/api_keys)
  - Depends on: P02-T08
  - Deliverables: create (secret shown once), list (prefix only), update name/scopes, revoke, rotate
  - Acceptance: secret never returned after creation; audit entries written (planner 2026-10-29: the `api_key.create`/`api_key.revoke`/`api_key.roll` rows are written by P02-T11, whose enumeration test covers these routes; T10 records the decision in specs/04 §9).

- [x] P02-T11 Audit logging (split by the planner on 2026-10-29 into T11a/T11b below; tick when both are ticked)
  - Spec refs: specs/04-auth-tenancy-rbac.md (audit logging rules); specs/28-security.md (audit)
  - Depends on: P02-T08
  - Deliverables: `AuditWriter` with redaction of secret fields, decorator/helper for mutating endpoints, `/v1/audit_logs` with filters and pagination
  - Acceptance: every mutating endpoint added in this phase produces an audit row (test enumerates them), including `/v1/api_keys` create/revoke/roll (deferred from P02-T10).

- [x] P02-T11a Audit writer, append-only grants, `GET /v1/audit_logs`, and audit rows for the envelope routes — reviewer 2026-10-29: make test 2968 passed (98% cov), lint/typecheck/placeholders/test-ts pass, openapi drift current, spec refs 0, jscpd 0 clones
  - Spec refs: specs/04-auth-tenancy-rbac.md §13, §8.3 decision 5; specs/28-security.md §6.3, §10; specs/03-data-model.md §3 `audit_logs`; specs/31-error-catalog.md §2
  - Depends on: P02-T10
  - Deliverables: `va_db.audit` (`AuditActor`, `redact`, `record`, `AUDIT_ACTIONS`), migration `0004_audit_logs_append_only`, `AuditLogRepo`, `va_api.audit` helpers, `PageResponse`, `GET /v1/audit_logs`; audit rows from `/v1/org`, members, invitations, SSO, sub-accounts, `/v1/api_keys`
  - Acceptance: redaction unit tests; `va_app` UPDATE/DELETE on `audit_logs` refused; each envelope mutating route writes its row in the same transaction (rolled back with it); list filters, pagination, `audit.read` gate, cross-tenant isolation.

- [x] P02-T11b Audit rows for `/internal/auth/*` routes and the mutating-route enumeration test — reviewer 2026-10-29: make test 2995 passed (97.81% cov), lint/typecheck/placeholders pass, openapi drift current, spec refs 0, jscpd 0 clones, radon no C+ (avg 2.01)
  - Spec refs: specs/04-auth-tenancy-rbac.md §13, §4.1, §4.5, §5
  - Depends on: P02-T11a
  - Deliverables: `auth.*` and `invitation.accept` rows from the dashboard auth routes; an `x-audit` / `x-audit-exempt` OpenAPI marker on every mutating route; a test enumerating every non-GET route (now and in later phases) that fails when one is unmarked or names an unknown action
  - Acceptance: every mutating endpoint added in this phase is marked and its audited action is asserted by an integration test.

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
