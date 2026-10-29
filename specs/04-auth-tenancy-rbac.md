# 04 — Authentication, Tenancy, RBAC, API Keys, Sessions, SSO, Audit

Package ownership: `va_common.auth` (hashing, token primitives), `va_db` (repositories), `va_api.auth` (middleware,
dependencies, routers). Tables: `orgs`, `users`, `memberships`, `invitations`, `sessions`, `email_tokens`, `api_keys`,
`sso_connections`, `audit_logs`, `idempotency_keys` (`03-data-model.md` §3).

## 1. Principals

| Principal | How authenticated | Scope of access |
|---|---|---|
| API key | `Authorization` header (§2) | One org (the key's `org_id`); limited by `scopes` |
| Dashboard user | Session cookie `va_session` + CSRF header (§4) | Orgs where the user has a membership; acts in `sessions.active_org_id`; limited by role (§3) |
| Widget visitor | Widget public key + Origin check (`22-web-widget.md`) | One widget; conversation endpoints only |
| Telnyx | Ed25519 signature (`06-telephony-telnyx.md`) | Edge webhook endpoints only |
| Stripe | `Stripe-Signature` (`24-billing-and-limits.md`) | `/v1/billing/stripe/webhook` only |
| Platform admin | Dashboard session where `users.is_platform_admin=true` and MFA passed | `/internal/admin/*` (system-scope repos) |
| Internal service | mTLS inside cluster is not used; services talk via Postgres/Redis/Temporal only — no service-to-service HTTP auth surface exists except `api` → none | n/a |

The resolved principal is a `RequestContext` dataclass:

```python
@dataclass(frozen=True)
class RequestContext:
    request_id: str
    org_id: UUID
    principal_type: Literal["api_key", "user", "widget", "platform_admin"]
    principal_id: UUID                 # api_keys.id | users.id | widgets.id
    role: Role | None                  # users only
    scopes: frozenset[str]             # api keys: key scopes; users: scopes derived from role (§3.3)
    environment: Literal["live", "test"]
    plan_id: str
    ip: str | None
    user_agent: str | None
    acting_parent_org_id: UUID | None  # set when a parent-org user/key acts on a sub-account (§8.3)
```

Every repository call receives `ctx.org_id`; `tenant_session(ctx.org_id)` sets `app.org_id` for RLS.

## 2. API keys

### 2.1 Format

- Plaintext: `va_live_` or `va_test_` followed by 32 characters from base62 (`[0-9A-Za-z]`) generated with
  `secrets.choice` (≈190 bits). Example shape: `va_live_3kQ9xT2mLpR7vB1nZc8dHf4sWq6yJa0e`.
- `prefix`: first 12 characters of the plaintext (`va_live_3kQ9`); unique; displayed in UI and logs.
- `secret_hash`: `sha256(API_KEY_PEPPER || plaintext)` hex, where `API_KEY_PEPPER` is a 32-byte secret env var
  (added to `02` §6, see shared-contract additions). Comparison with `hmac.compare_digest`.
- Plaintext is returned exactly once at creation. It is never logged, stored, or retrievable.
- `environment=test` keys: calls are routed to the simulator stack only when `VA_ENV in (local,test)`; in staging and
  production, test keys may create/read configuration resources but `POST /v1/calls`, `/v2/batches/create`,
  `/v1/sms/send`, `/numbers/purchase` return `FORBIDDEN` with message "Test keys cannot place live calls, send SMS, or purchase numbers."

> Decision (2026-10-28): the hashed bytes are `bytes.fromhex(API_KEY_PEPPER) + plaintext.encode()` (UTF-8, which is
> ASCII for every well-formed key), i.e. `sha256(pepper || plaintext).hexdigest()`, implemented by
> `va_common.auth.ApiKeyHasher`. Peppers are versioned with the new env var `API_KEY_PEPPER_VERSION` (int ≥ 1, default
> `1`; specs/02 §6): it is the version of `API_KEY_PEPPER`, and new keys store it in `api_keys.pepper_version`. A key
> whose `pepper_version` equals it verifies with `API_KEY_PEPPER`; one whose version is exactly one lower verifies
> with `API_KEY_PEPPER_PREVIOUS` when that is set (setting it requires `API_KEY_PEPPER_VERSION` ≥ 2); any other version
> fails with `AUTH_FAILURE`. Every verification attempt computes exactly one hash. `API_KEY_PEPPER` may be empty when
> settings load (so the app module imports without environment), but the api refuses to start without a 64-hex value.

### 2.2 Middleware `va_api.auth.api_key_auth`

1. Read `Authorization`. Accept `Bearer <key>` (case-insensitive scheme) or the bare key. Ignore `x-api-key`.
2. If the value starts with `va_live_` or `va_test_` and has length 40: look up by `prefix` (Redis cache
   `apikey:{prefix}` → JSON `{id, org_id, secret_hash, scopes, environment, revoked, expires_at}` TTL 60 s; on miss
   load from DB via system scope repo `ApiKeyLookupRepo`). Compare hash. Reject if revoked, expired, or org status
   `closed`. Org `suspended` → `ORG_SUSPENDED` for writes, reads allowed.
3. If no header but a `va_session` cookie exists → session auth (§4).
4. Update `last_used_at` at most once per 60 s per key (Redis `SET apikey:{id}:touched NX EX 60` gate, then async
   DB update).
5. Revocation invalidates the Redis cache entry immediately (`DEL apikey:{prefix}`).
6. Failures return `AUTH_FAILURE` (401) with message "Invalid API key." Timing: always perform one hash even when
   prefix not found (hash a dummy) to avoid prefix enumeration timing leaks.

> Decision (2026-10-28): the auth dependency is `va_api.auth.get_principal`, which delegates to
> `va_api.auth.api_key_auth.ApiKeyAuthenticator`. A value that is not exactly `va_(live|test)_` + 32 base62 characters
> fails with `AUTH_FAILURE` after one dummy hash, like an unknown prefix; until session auth (step 3) exists, a missing
> `Authorization` header fails the same way. The cache JSON additionally carries `pepper_version`, `org_status`, and
> `plan_id` so the `RequestContext` is built without a database hit; org status and plan changes therefore take
> effect within 60 s unless the writer deletes `apikey:{prefix}`. Unknown prefixes are not cached. Redis keys use the
> prefix / key id as the hash tag (`apikey:{va_live_3kQ9}`, `apikey:{<uuid>}:touched`; specs/03 §11). When Redis is
> unavailable the cache is skipped (the database answers) and the `last_used_at` write is skipped; neither fails the
> request. The `last_used_at` write runs as a FastAPI background task after the response; its failures are logged
> (`api_key_touch_failed`, key id only) and swallowed.

### 2.3 Scopes (complete list)

| Scope | Grants |
|---|---|
| `*` | All scopes below |
| `calls:read` | GET calls, transcripts, recordings, events, active calls, analysis results |
| `calls:write` | Send/stop/analyze calls, transcript tokens, live control (transfer, say, takeover) |
| `batches:read` | GET batches, logs |
| `batches:write` | Create/stop batches |
| `pathways:read` | GET pathways, versions, folders, unit tests, test runs |
| `pathways:write` | Create/update/delete/publish/promote pathways, folders, unit tests, run tests/simulations, test chat |
| `personas:read` / `personas:write` | Personas and versions |
| `agents:read` / `agents:write` | Agents, versions, env pins, experiments |
| `tools:read` / `tools:write` | Custom tools, tool test-run |
| `knowledge:read` / `knowledge:write` | Knowledge bases, documents, KB chat/search |
| `voices:read` / `voices:write` | Voice catalog, clone, settings, ratings, samples |
| `numbers:read` / `numbers:write` | Phone numbers, inbound config, purchase/release, pools, BYO Telnyx accounts (`/v1/accounts*`) |
| `sms:read` / `sms:write` | SMS conversations, send, webhook config, 10DLC |
| `widgets:read` / `widgets:write` | Widgets, threads, live-agent messages |
| `memory:read` / `memory:write` | Contacts, memories, memory changes, reset |
| `analysis:read` / `analysis:write` | Dispositions, citation schemas, eval setups, eval runs |
| `compliance:read` / `compliance:write` | DNC entries, guard rail templates |
| `webhooks:read` / `webhooks:write` | Webhook endpoints, deliveries, replay, signing secret (rotate requires owner/admin when via session) |
| `billing:read` | Balance, usage, invoices, plans |
| `billing:write` | Top-up, auto-recharge, plan change, credit transfer to sub-accounts |
| `org:read` | `/v1/org`, members list, audit logs |
| `org:write` | Org settings, members, invitations, sub-accounts (`/v1/orgs/create`) |
| `api_keys:write` | Create/revoke API keys (keys can never create keys with scopes they do not hold) |
| `prompts:read` / `prompts:write` | Prompt library |
| `analytics:read` | `/v1/analytics/*` |
| `mcp` | Access `/v1/mcp` (each MCP tool additionally checks the scope of the underlying operation) |

### 2.4 Endpoint → scope mapping principle

- Every router declares `dependencies=[Depends(require_scope("<resource>:<read|write>"))]`; `GET`/`HEAD` map to
  `:read`, all other methods to `:write`, unless the endpoint overrides (e.g. `POST /v1/knowledge/{id}/chat` is
  `knowledge:read`; `POST /v1/calls/{id}/analyze` is `calls:write`; test-chat endpoints are `pathways:write`).
- A test (`apps/api/tests/test_scope_coverage.py`) iterates `app.routes` and asserts every route under `/v1`, `/v2`,
  `/numbers` has exactly one `require_scope` dependency or is in the explicit public allowlist:
  `GET /v1/health`, `GET /openapi.json`, `GET /widget/v1/widget.js`, `/internal/auth/*` (session-flow endpoints),
  `POST /v1/agent-onboarding/*`, `/v1/billing/stripe/webhook`, `/v1/widget/public/*` (widget visitor auth).

## 3. RBAC

### 3.1 Roles

`owner` (full control incl. deletion of org and billing), `admin` (everything except org deletion and ownership
transfer), `developer` (build and run agents; no billing writes, no member management), `analyst` (read everything
operational, run analyses, export), `billing` (billing and usage only plus read org), `viewer` (read-only, no
recordings/transcript PII unless org setting `viewers_can_see_pii=true`).

### 3.2 Matrix (R = read, W = write/create/update/delete, X = execute/operate, — = denied)

| Resource / action | owner | admin | developer | analyst | billing | viewer |
|---|---|---|---|---|---|---|
| Calls: list/view details & transcripts | R | R | R | R | — | R* |
| Calls: recordings download | R | R | R | R | — | R* |
| Calls: send / stop / live control / takeover | X | X | X | — | — | — |
| Calls: analyze (post-hoc) | X | X | X | X | — | — |
| Batches | W | W | W | R | — | R |
| Pathways / folders | W | W | W | R | — | R |
| Pathways: publish / promote production | X | X | X | — | — | — |
| Pathway tests / simulations | X | X | X | X | — | — |
| Personas | W | W | W | R | — | R |
| Agents / versions / env pins | W | W | W | R | — | R |
| Experiments start/stop | X | X | X | — | — | — |
| Tools (incl. secret headers view = masked always) | W | W | W | R | — | R |
| Knowledge bases | W | W | W | R | — | R |
| Voices: catalog / ratings | W | W | W | R | — | R |
| Voices: clone / delete clone | W | W | W | — | — | — |
| Phone numbers: view / inbound config | W | W | W | R | — | R |
| Phone numbers: purchase / release | X | X | — | — | — | — |
| BYO Telnyx accounts | W | W | — | — | — | — |
| Number pools | W | W | W | R | — | R |
| SMS conversations: view | R | R | R | R | — | R* |
| SMS: send / takeover | X | X | X | — | — | — |
| 10DLC brand/campaign registration | W | W | — | — | — | — |
| Widgets | W | W | W | R | — | R |
| Memory / contacts | W | W | W | R | — | R* |
| Memory reset / erasure | X | X | — | — | — | — |
| Dispositions / citation schemas / eval setups | W | W | W | W | — | R |
| DNC list | W | W | W | R | — | R |
| Guard rail configuration (org defaults) | W | W | W | R | — | R |
| Webhook endpoints / deliveries / replay | W | W | W | R | — | R |
| Webhook signing secret view last4 / rotate | W | W | — | — | — | — |
| Analytics | R | R | R | R | R | R |
| Prompts library | W | W | W | R | — | R |
| Billing: balance / usage / invoices | R | R | R | R | R | — |
| Billing: top-up / auto-recharge / payment method | W | W | — | — | W | — |
| Billing: plan change | W | — | — | — | W | — |
| Credit transfer to sub-account | X | X | — | — | X | — |
| API keys: list (prefix only) | R | R | R | — | — | — |
| API keys: create / revoke | W | W | W(own) | — | — | — |
| Members: list | R | R | R | R | R | R |
| Members: invite / change role / remove | W | W | — | — | — | — |
| Assign `owner` role / transfer ownership | X | — | — | — | — | — |
| Org settings (general) | W | W | — | — | — | R |
| Org settings (compliance, retention, hipaa_mode, pii_redaction, mfa_required) | W | W | — | — | — | — |
| SSO connection | W | W | — | — | — | — |
| Sub-accounts: create / list / switch into | W | W | — | — | — | — |
| Audit logs | R | R | — | — | — | — |
| Delete org | X | — | — | — | — | — |

`R*` = transcript text, recordings and memory facts are shown with PII masked (`va_compliance.redaction.mask_text`)
unless `OrgSettings.viewers_can_see_pii` is true. `W(own)` = developers may create keys with scopes ⊆ developer
scopes and revoke keys they created.

### 3.3 Role → scope derivation (for session principals)

`va_api.auth.rbac.ROLE_SCOPES: dict[Role, frozenset[str]]` encodes the matrix as scopes so the same
`require_scope` dependency works for sessions. Actions that are finer than read/write (publish, purchase, rotate,
plan change, member management, ownership) use `require_permission("<permission>")` with permission names:
`pathways.publish`, `numbers.purchase`, `numbers.release`, `telnyx_accounts.manage`, `tendlc.manage`,
`members.manage`, `org.owner_transfer`, `org.delete`, `org.compliance`, `sso.manage`, `subaccounts.manage`,
`billing.plan_change`, `billing.payment`, `billing.credit_transfer`, `webhooks.secret`, `memory.erase`,
`audit.read`, `voices.clone`, `experiments.operate`, `calls.operate`. API keys with `*` hold all permissions
except `org.delete`, `org.owner_transfer`, `sso.manage` (session-only). A test renders this matrix from code and
compares it to the table in §3.2 (parsed from this markdown file).

> Decision (2026-10-29): **role → scope derivation**. A role holds `<resource>:read` when any §3.2 row of that
> resource family is `R`/`R*`/`W`/`X` for it, and `<resource>:write` when any row is `W`/`W(own)`/`X`; finer actions
> stay behind `require_permission` (e.g. `calls.operate` gates send/stop/live so analysts may analyze but not send).
> Sessions never hold `*` or `mcp`. The sets (exact, asserted by `apps/api/tests/test_rbac.py`): **owner, admin**:
> every `<r>:read` and `<r>:write` of the 18 read/write resources of §2.3 plus `api_keys:write` and `analytics:read`;
> **developer**: the same minus `billing:write` and `org:write`; **analyst**: every `<r>:read` plus `calls:write`,
> `pathways:write`, `analysis:write`, `analytics:read`; **billing**: `billing:read`, `billing:write`, `org:read`,
> `analytics:read`; **viewer**: every `<r>:read` except `billing:read`, plus `analytics:read`. The permission matrix
> may later derive `ROLE_SCOPES` from its table as long as these sets do not change.

## 4. Dashboard authentication

### 4.1 Endpoints (`/internal/auth/*`, envelope format)

| Method | Path | Body | Behaviour |
|---|---|---|---|
| POST | `/internal/auth/signup` | `{email, password, name, org_name}` | Creates user (unverified), org (`plan_id='start'`, slug from org_name + 4 random chars), membership `owner`, sends verification email; returns `{data:{user, org}}`, sets session (MFA not required until verified) |
| POST | `/internal/auth/verify-email` | `{token}` | Marks verified; 24 h token |
| POST | `/internal/auth/resend-verification` | `{}` | Max 3 per hour per user |
| POST | `/internal/auth/login` | `{email, password}` | Returns `{data:{mfa_required: bool, user}}`; on SSO-enforced domain returns `SSO_REQUIRED` with `details.sso_url` |
| POST | `/internal/auth/mfa/verify` | `{code}` or `{recovery_code}` | Sets `sessions.mfa_passed=true` |
| POST | `/internal/auth/logout` | `{}` | Revokes session |
| POST | `/internal/auth/logout-all` | `{}` | Revokes all sessions of user |
| POST | `/internal/auth/password/forgot` | `{email}` | Always 200; sends reset link if user exists; 1 h token |
| POST | `/internal/auth/password/reset` | `{token, password}` | Sets password, revokes all sessions |
| POST | `/internal/auth/password/change` | `{current_password, new_password}` | Requires recent auth (< 10 min since login or MFA) |
| POST | `/internal/auth/mfa/setup` | `{}` | Returns `{secret_base32, otpauth_uri, qr_svg}`; secret stored encrypted but `mfa_enabled=false` |
| POST | `/internal/auth/mfa/enable` | `{code}` | Verifies code, enables, returns 10 recovery codes (shown once) |
| POST | `/internal/auth/mfa/disable` | `{code}` | Disallowed if org `mfa_required` |
| GET | `/internal/auth/session` | — | Current user, memberships (org id/name/role/plan), active org, csrf token |
| POST | `/internal/auth/switch-org` | `{org_id}` | Requires membership (or parent-org admin for sub-accounts, §8.3) |
| GET | `/internal/auth/sso/start?email=` | — | Redirects to IdP (OIDC authorization code + PKCE) |
| GET | `/internal/auth/sso/callback` | IdP params | Completes login (§5) |
| POST | `/internal/auth/invitations/accept` | `{token, name?, password?}` | Creates user if needed, adds membership |
| GET | `/internal/auth/invitations/{token}` | — | Invitation details (org name, role, email) |

Error codes added: `SSO_REQUIRED` (403), `EMAIL_NOT_VERIFIED` (403), `INVALID_CREDENTIALS` (401),
`INVALID_MFA_CODE` (401), `PASSWORD_TOO_WEAK` (400), `TOKEN_EXPIRED` (400), `REAUTH_REQUIRED` (401)
(see shared-contract additions).

> Decision (2026-10-29): **org provisioning** (`va_db.provisioning.OrgProvisioner`, used by signup,
> `POST /v1/orgs/create`, and seeding) creates in one `tenant_session(new_org_id)` transaction: the org row, its
> `active` `org_data_keys` row, a webhook signing secret `whsec_` + 43-char opaque token encrypted with AAD
> `orgs.webhook_signing_secret_enc:<org_id>`, and (when an owner is given) the owner user (new or existing) plus an
> `owner` membership; any failure rolls everything back. An empty (after `strip()`) org name is `INVALID_INPUT` with
> `field: "org_name"`. **Slug** = org name NFKD-folded to ASCII, lower-cased, runs of non-`[a-z0-9]` replaced by `-`,
> trimmed of `-`, cut to 40 characters (re-trimmed), `org` when empty, then `-` + 4 random characters from
> `[a-z0-9]`; on a slug collision a new suffix is drawn, up to 5 attempts, then `CONFLICT`. **Duplicate user email**
> raises `CONFLICT` with `field: "email"`, message "An account with this email already exists." Email addresses are
> stored as given after `strip()` (the column is `citext`). Email-link tokens live 24 h (`verify_email`) and 1 h
> (`reset_password`); consuming one is a single conditional `UPDATE` (unused, unexpired, matching purpose), so a
> token is accepted at most once.

> Decision (2026-10-29): **session endpoint shapes** (envelope `{data, errors: null}`). `login` returns
> `{mfa_required, user}` where `mfa_required = user.mfa_enabled` (an org requiring MFA from an unenrolled user is
> handled by the §4.3 gate, the dashboard then sends the user to MFA setup); `logout` and `logout-all` return
> `{revoked_sessions: int}` and clear both cookies (`Max-Age=0`) and accept an optional `{}` body (unknown fields are
> `INVALID_INPUT`); `GET /session` returns `{user, memberships[], active_org_id, mfa_passed, mfa_required,
> csrf_token, expires_at}` with `user = {id, email, name, email_verified, mfa_enabled, is_platform_admin,
> created_at}`, each membership `{org_id, org_name, org_slug, role, plan_id, org_status, parent_org_id}` (oldest
> first), `active_org_id` the org the session acts in (`null` without memberships), and `mfa_required` true when that
> org enforces MFA and the session has not passed it. Login bodies are `{email (3-320 chars, trimmed), password
> (1-1024 chars)}` with unknown fields rejected. Login always issues a fresh token and revokes the session of any
> `va_session` cookie it received (fixation defence); its active org is the user's oldest membership. All
> `/internal/auth/*` routes carry `x-public: "session-flow"` and are excluded from `openapi.json`.

### 4.2 Passwords

- argon2id via `argon2-cffi`: `time_cost=3`, `memory_cost=65536` (64 MiB), `parallelism=4`, `hash_len=32`,
  `salt_len=16`. Rehash on login when parameters change (`check_needs_rehash`).
- Policy: length 12–128; reject if in the bundled top-100 000 breached-password list (`va_common/auth/data/common_passwords.txt.gz`,
  generated by `scripts/build_password_list.py` from the SecLists `10-million-password-list-top-100000.txt` file
  downloaded at build time and committed gzipped); reject if contains the email local-part.
- Login throttling: per email 10 failures / 15 min → 15 min lock (`AUTH_LOCKED` 429); per IP 50 failures / 15 min.
  Redis keys `auth:fail:email:{sha256(email)}`, `auth:fail:ip:{ip}`.

> Decision (2026-10-29): password primitives live in `va_common.auth.passwords` and `va_common.auth.tokens`.
> **Breached list source**: SecLists renamed the file; the source is the same xato top-100 000 list at
> `Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt`, downloaded from
> `raw.githubusercontent.com/danielmiessler/SecLists/<commit>/…` pinned to commit
> `c205c36a445bff37f8e58a9ec829105cd4975c58` and verified against a pinned sha256 in the script. **Normalisation**:
> entries are stripped, NFKC-normalised, casefolded, deduplicated, sorted, blanks dropped, and stored gzipped with
> `mtime=0` (rebuilds are byte-identical); 96 517 entries remain. The lookup is the candidate's NFKC casefold.
> **Length** is counted in Unicode code points after NFKC; 12–128 inclusive. **Email local part** = text before the
> last `@` with any `+tag` removed, casefolded; checked only when it has at least 3 characters. **Rule order and
> messages** (`PASSWORD_TOO_WEAK`, `field: "password"`, first failing rule wins): "Password must be at least 12
> characters." / "Password must be at most 128 characters." / "Password is too common; choose a less predictable
> one." / "Password must not contain your email address." **Hashing runs off the event loop** (`hash_async`,
> `verify_async` via `asyncio.to_thread`); `verify` returns false for a mismatch, a malformed hash, and a missing hash
> (SSO-only or unknown user), and the missing-hash case still verifies against a precomputed dummy hash so timing
> does not reveal it. A malformed stored hash counts as needing a rehash. **Opaque tokens** (sessions, CSRF, email
> tokens, later invitations and resets) are `secrets.token_bytes(32)` base64url without padding (43 characters),
> stored as `sha256_hex(token)` and compared with `hmac.compare_digest`.

> Decision (2026-10-29): **login throttling**. Keys `auth:fail:email:<sha256_hex(email.strip().casefold())>` and
> `auth:fail:ip:<ip>` (single-key commands, no hash tag; the IP key is skipped when the peer address is not an IP).
> Each failed attempt runs `INCR` + `EXPIRE 900 NX` per key in one transaction; the failure that brings a counter to
> its limit (10 per email, 50 per IP) runs `EXPIRE 900` again, so the lock lasts 15 minutes from the locking failure.
> Before verifying, a counter at or above its limit fails with `AUTH_LOCKED` "Too many failed login attempts; try
> again in N seconds." ("1 second" when N is 1) and `Retry-After: N`, N = the key's remaining TTL (900 when it has
> none); locked attempts are not counted. A successful login deletes the email counter only. Failures are an unknown
> email, a wrong password, a user without a password, and a disabled user, all `INVALID_CREDENTIALS` "Invalid email
> or password." after exactly one argon2 verification (the dummy hash covers the missing-hash cases). Redis errors
> never block a login (`login_throttle_unavailable` warning). A present `Origin`/`Referer` on `POST /login` must be
> the dashboard's (`FORBIDDEN` "Request origin is not allowed."); requests with neither (CLI clients) pass.
> `SSO_REQUIRED` for enforced domains arrives with SSO (§5).

### 4.3 Sessions

- Cookie `va_session`: 32 random bytes base64url; `HttpOnly; Secure` (not Secure in `local`); `SameSite=Lax`;
  `Path=/`; domain = dashboard/api parent domain from `SESSION_COOKIE_DOMAIN`.
- DB stores `sha256(token)`. Sliding expiry 14 days: each authenticated request extends `expires_at` to now + 14 d
  if more than 1 h has passed since the last extension. Absolute max lifetime 90 days.
- Session cache `sess:{token_hash}` (TTL 5 min) holds `{user_id, active_org_id, mfa_passed, expires_at}`.
- Org with `mfa_required=true`: session without `mfa_passed` gets `MFA_REQUIRED` on every endpoint except
  `/internal/auth/session`, `/internal/auth/mfa/*`, `/internal/auth/logout`.

> Decision (2026-10-29): **session mechanics**. The cookie value is a 43-character opaque token
> (`va_common.auth.issue_token`); `va_session` is `HttpOnly`, `Secure` unless `VA_ENV=local`, `SameSite=Lax`,
> `Path=/`, `Domain=SESSION_COOKIE_DOMAIN` (omitted when empty), `Max-Age` 90 days (the server enforces expiry).
> **Sliding**: a request extends `expires_at` to `min(now + 14 d, created_at + 90 d)` only when that is more than 1 h
> past the current `expires_at` (creation sets `now + 14 d` with `created_at` from the same clock, so "last
> extension" is `expires_at - 14 d`); the 90-day cap makes the absolute maximum fall out of the same rule. **Cache**
> `sess:{token_hash}` (literal braces, the hash tag) holds JSON `{session_id, user_id, active_org_id, mfa_passed,
> created_at, expires_at}` for min(300 s, seconds until `expires_at`, at least 1 s); it is written on create, extend,
> and cache miss and deleted on logout, logout-all, and revoke; Redis errors fall back to the database
> (`session_cache_unavailable` warning). A user's status is checked when the session is loaded from the database, so
> disabling a user takes effect within 5 minutes unless the writer also revokes its sessions. **Active org**:
> `sessions.active_org_id`, falling back to the user's oldest membership (persisted on the session) when it is NULL or
> the membership is gone; with no membership at all principal routes fail with `FORBIDDEN` "You are not a member of
> any organization." while `/internal/auth/*` still works. **Principal**: `principal_type "user"`,
> `environment "live"`, `role` from the membership, scopes from `ROLE_SCOPES` (§3.3), rate-limited at
> `rl:user:{user_id}:<bucket>` with the org's plan and `limits.rate_per_sec`. Org `suspended` rejects writes with
> `ORG_SUSPENDED` (the API-key message); `closed` rejects every method with `ORG_SUSPENDED` "Organization is
> closed.". A request with a non-blank `Authorization` header is always API-key authenticated (cookies ignored, no
> CSRF check); with neither header nor cookie it fails as `AUTH_FAILURE` "Invalid API key."; an invalid, expired, or
> revoked session fails as `AUTH_FAILURE` "Session is invalid or expired.". **MFA gate**: org
> `settings.mfa_required` is JSON `true` and the session has not passed MFA → `MFA_REQUIRED` "Multi-factor
> authentication is required for this organization." on every route except the three above (`logout-all` is
> gated).

### 4.4 CSRF

Double-submit: on session creation and on `GET /internal/auth/session`, the API sets cookie `va_csrf` (random 32
bytes, not HttpOnly, SameSite=Lax) and returns the same value in the body. Every non-GET request authenticated by
session must send header `X-CSRF-Token` equal to the cookie, compared constant-time; otherwise `FORBIDDEN` with message
"CSRF token missing or invalid." API-key requests are exempt. Additionally `Origin`/`Referer` must match
`DASHBOARD_BASE_URL` for session-authenticated writes.

> Decision (2026-10-29): **CSRF details**. `va_csrf` is an opaque 43-character token with the `va_session` cookie
> attributes except `HttpOnly`; it is set at login and on every `GET /internal/auth/session`, which reuses the
> presented cookie when it is a well-formed token and otherwise issues a new one (the body `csrf_token` always equals
> the cookie). Session-authenticated methods other than GET/HEAD/OPTIONS need `X-CSRF-Token` equal to the cookie
> (`tokens_equal`), then an `Origin` (else `Referer`) whose origin (scheme://host[:port], lower-cased, default port
> dropped) equals that of `DASHBOARD_BASE_URL`; `Origin: null` and missing both headers are rejected. Failures are
> `FORBIDDEN` "CSRF token missing or invalid." and "Request origin is not allowed." respectively.

### 4.5 TOTP MFA

`pyotp.TOTP(secret, digits=6, interval=30)`, valid window ±1 step; replay protection: last accepted counter stored
in Redis `mfa:last:{user_id}` (TTL 90 s). Recovery codes: 10 codes of format `xxxx-xxxx-xxxx` (base32 lowercase),
stored as argon2id hashes in `users.mfa_recovery_codes_hash`; each consumed code removed.

## 5. OIDC SSO (enterprise, `OIDC_ENABLED=true`)

1. Admin configures `sso_connections` (issuer, client_id, client_secret, allowed_domains, default_role, enforce) via
   `PUT /v1/org/sso` (envelope). We validate by fetching `{issuer}/.well-known/openid-configuration`.
2. Login: user enters email on dashboard → `GET /internal/auth/sso/start?email=` → domain lookup across
   `sso_connections.allowed_domains` (system scope) → authlib `AsyncOAuth2Client` authorization-code flow with PKCE
   (S256), `scope="openid email profile"`, `state` and `nonce` stored in Redis `sso:state:{state}` (TTL 10 min).
3. Callback validates state, exchanges code, validates ID token (signature via JWKS, `iss`, `aud`, `exp`, `nonce`),
   requires `email_verified=true` (or IdP lacking the claim treated as verified only when `allowed_domains` matches).
4. JIT provisioning: if no user with that email → create user (no password, `email_verified_at=now()`), membership
   with `default_role`. Existing user without membership → membership with `default_role`. Existing membership keeps
   its role.
5. Session created with `mfa_passed=true` (IdP is trusted for MFA).
6. `enforce=true`: password login for emails in `allowed_domains` returns `SSO_REQUIRED`; API keys still work.

## 6. Invitations and members

- `POST /v1/org/members/invitations` `{email, role}` → invitation (7-day expiry, token 32 bytes base64url hashed);
  email with link `{DASHBOARD_BASE_URL}/invite/{token}`. Only owners can invite owners.
- `GET /v1/org/members` → `{data:[{user_id, email, name, role, mfa_enabled, last_login_at, joined_at}]}`.
- `PATCH /v1/org/members/{user_id}` `{role}`; `DELETE /v1/org/members/{user_id}`. Constraint: an org always has ≥ 1
  owner (`CONFLICT` "An organization must have at least one owner.").
- `GET /v1/org/members/invitations`, `DELETE /v1/org/members/invitations/{id}`.
- `POST /v1/org/transfer-ownership` `{user_id}` (owner, requires reauth).

## 7. OrgSettings model

`orgs.settings` jsonb is validated by `va_domain.org.OrgSettings` (Pydantic, `extra="forbid"`). All fields have
defaults; `PATCH /v1/org` merges.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `timezone` | IANA tz string | `America/Los_Angeles` | Default `timezone` for calls and calling windows |
| `default_language` | string | `en` | Default call `language` (`08-stt-elevenlabs.md` language map) |
| `default_voice` | string (voice name or uuid) | `Karen` (platform catalog alias seeded) | Default `voice` |
| `default_from_number` | E.164 \| null | null | Default caller ID when `from` absent |
| `default_max_duration_min` | int 1–240 | 30 | |
| `default_record` | bool | false | |
| `default_webhook` | url \| null | null | Applied when call omits `webhook` |
| `default_webhook_events` | string[] | `[]` | |
| `default_voice_id` | uuid \| null | null | Default voice by id (takes precedence over `default_voice` when set) |
| `default_country` | ISO-3166 alpha-2 | `US` | Region used to parse non-E.164 numbers |
| `call_defaults` | object (partial SendCallRequest, `05-calls-api.md`) | `{}` | Org-wide Send Call defaults applied below agent/persona defaults and above platform defaults |
| `dial_timeout_s` | int 5–120 | 45 | Telnyx `timeout_secs` |
| `transfer_caller_id_mode` | `agent_number` \| `original_caller` | `agent_number` | Caller ID presented to transfer targets |
| `transfer_failed_message` | string ≤ 500 | `"I'm sorry, I wasn't able to connect you. Is there anything else I can help with?"` | Spoken when a transfer leg fails |
| `max_duration_message` | string ≤ 500 | `"I'm sorry, we've reached the time limit for this call. Goodbye."` | Spoken before `max_duration` hangup |
| `no_input_goodbye` | string ≤ 500 | `"I haven't heard anything, so I'll end the call now. Goodbye."` | Spoken after repeated no-input timeouts |
| `pathway_version_fallback` | bool | true | Missing numeric pathway version falls back to production instead of `PATHWAY_VERSION_NOT_FOUND` |
| `voice_overrides` | map voice_id(uuid) → VoiceSettings `{stability, similarity_boost, style, use_speaker_boost, speed}` | `{}` | Org-level per-voice setting overrides |
| `calling_hours` | `{start: "HH:MM", end: "HH:MM", days: int[] (0=Mon)}` | `{start:"08:00", end:"21:00", days:[0,1,2,3,4,5,6]}` | Evaluated in callee local timezone (`23-guardrails-and-compliance.md`) |
| `default_guard_rails` | GuardRail[] | `[]` | Applied to every call unless call overrides (`23`) |
| `ai_disclosure_required` | bool | false | Forces `tcpa:ai_disclosure` guard rail |
| `recording_disclosure_required` | bool | true | Forces `tcpa:recording_disclosure` when `record=true` |
| `honor_opt_out` | bool | true | Opt-out phrases write DNC (`23`) |
| `allow_insecure_webhooks` | bool | false | Permit `http://` webhook URLs in production |
| `webhook_format` | `bland` \| `envelope` | `bland` | Default format for per-call webhooks |
| `hipaa_mode` | bool | false | Disables vendor logging (`enable_logging=false` to ElevenLabs where permitted), forces PII redaction in logs/analytics, forces recording encryption with org data key, disables transcript inclusion in emails, requires BAA flag `baa_signed_at` |
| `baa_signed_at` | datetime \| null | null | Set by platform admin |
| `pii_redaction` | `off` \| `transcripts` \| `transcripts_and_recordings` | `off` | Post-call redaction (`28-security.md` §7) |
| `pii_redaction_entities` | string[] | `["pii","phi","pci"]` | Scribe entity categories to redact |
| `viewers_can_see_pii` | bool | false | |
| `mfa_required` | bool | false | |
| `session_max_age_days` | int 1–90 | 14 | Overrides sliding expiry window |
| `ip_allowlist` | CIDR[] | `[]` | If non-empty, API keys and sessions only accepted from these ranges |
| `llm` | `{base_url?: url, api_key_ref?: str, dialogue_model?: str, router_model?: str, analysis_model?: str, fallback_model?: str, temperature_default?: float}` | `{}` | Per-org LLM override (`10-llm-gateway.md`); `api_key_ref` names an encrypted secret stored via `PUT /v1/org/secrets/{name}` |
| `international_enabled` | bool | false | Also requires plan/kyc (`24`) |
| `allowed_countries` | ISO-3166 alpha-2[] | `["US","CA"]` | Destinations allowed when international enabled |
| `blocked_prefixes` | string[] | `[]` | Extra E.164 prefixes blocked (toll fraud, `28` §9) |
| `amd_default` | `off` \| `premium` | `premium` | Default AMD mode |
| `background_track_default` | `none` \| `office` \| `cafe` \| `restaurant` \| `phone_static` | `phone_static` | |
| `noise_cancellation_default` | bool | true | |
| `post_call_eval_setup_id` | uuid \| null | null | Run this eval on every call |
| `summary_prompt_default` | string ≤ 2000 \| null | null | |
| `dispositions_default` | string[] | `[]` | |
| `low_balance_threshold_micros` | int | 5 000 000 | Triggers `billing.low_balance` |
| `notification_emails` | email[] | `[]` | Billing/security notifications in addition to owners |
| `data_export_enabled` | bool | true | CSV exports in dashboard |
| `onboarding_completed_at` | datetime \| null | null | Set by the dashboard onboarding wizard (`25` §3) |

Nested groups (each a Pydantic sub-model with `extra="forbid"`; `PATCH /v1/org` deep-merges them):

| Path | Type | Default | Meaning |
|---|---|---|---|
| `compliance.enforce_calling_hours` | bool | false | Apply `calling_hours` to non-batch API calls (`23`) |
| `llm.allow_model_override` | bool | false | Allow per-call `model` values outside the platform model allowlist |
| `llm.context_budget_tokens` | int 1 000–64 000 | 6 000 | Max prompt tokens for dialogue context assembly (`10`) |
| `privacy.redact_transcripts` | bool | false | Shorthand that forces `pii_redaction` ≥ `transcripts` |
| `privacy.zero_retention` | bool | false | Vendor calls use zero-retention options (`enable_logging=false` for ElevenLabs); requires enterprise |
| `redaction.enabled` | bool | false | Real-time PCI redaction before text reaches the LLM (`28` §7) |
| `analysis.retranscribe` | bool | false | Post-call diarized re-transcription with ElevenLabs batch STT (`18`) |
| `memory.default_enabled` | bool | false | Memory on for calls/personas that do not specify (`20`) |
| `tools.allow_send_sms` | bool | true | Built-in SMS tool/node allowed (`13`, `21`) |
| `tools.allow_schedule_callback` | bool | false | Built-in schedule-callback tool allowed |
| `engine.speculative` | bool | true | Speculative dialogue generation in parallel with routing (`11`) |
| `engine.tool_fillers` | bool | true | Speak tool `speech`/filler phrases while tools run |
| `engine.allow_callbacks` | bool | false | Agent may schedule callback calls |
| `voices.allow_raw_provider_ids` | bool | false | `voice` may be a raw ElevenLabs voice_id not in the catalog (`15`) |
| `experiments.sticky_by_contact` | bool | false | Experiment arm assignment hashed by contact instead of call id (`20`) |

Python layout: `OrgSettings` contains fields `compliance: ComplianceSettings`, `llm: LlmSettings` (which also holds the
override fields `base_url`, `api_key_ref`, `dialogue_model`, `router_model`, `analysis_model`, `fallback_model`,
`temperature_default` listed above), `privacy: PrivacySettings`, `redaction: RedactionSettings`,
`analysis: AnalysisSettings`, `memory: MemorySettings`, `tools: ToolsSettings`, `engine: EngineSettings`,
`voices: VoicesSettings`, `experiments: ExperimentsSettings`, each defaulting to its own defaults.

Org secrets: `PUT /v1/org/secrets/{name}` `{value}` stores envelope-encrypted values in `org_secrets` (new table,
see shared-contract additions); `GET /v1/org/secrets` lists names only.

## 8. Orgs and sub-accounts

### 8.1 Org endpoints (envelope)

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/me` | `{data:{principal_type, user?{id,email,name,mfa_enabled}, api_key?{id,prefix,scopes,environment}, org:{id,name,slug,plan_id,status}, role, scopes}}` |
| GET | `/v1/org` | Org incl. `settings`, `limits` effective (plan ⊕ overrides), `data_region`, `retention_days`, `recording_retention_days`, `credits_balance` (dollars) |
| PATCH | `/v1/org` | `{name?, settings?: partial OrgSettings, retention_days?, recording_retention_days?}`; compliance fields need `org.compliance` |
| DELETE | `/v1/org` | Owner + reauth; sets `status='closed'`, revokes keys, releases numbers via `NumberOrderWorkflow`, schedules erasure after 30 days |
| GET/POST/PATCH/DELETE | `/v1/org/members`, `/v1/org/members/invitations`, `/v1/org/transfer-ownership` | §6 |
| PUT/GET/DELETE | `/v1/org/sso` | §5 |
| PUT/GET/DELETE | `/v1/org/secrets[/{name}]` | §7 |

### 8.2 Bland-compatible sub-account creation (legacy envelope)

`POST /v1/orgs/create` body `{org_display_name: string, org_plan?: "start"|"build"|"scale", initial_credits?: number (dollars)}`.
Creates a child org with `parent_org_id = ctx.org_id`, `plan_id = org_plan or parent plan`, `kyc_level = parent.kyc_level`,
optionally transfers `initial_credits` from parent (§8.4), creates an `owner`-equivalent API key for the child, and
returns:

```json
{"status":"success","org_id":"…","org_display_name":"…","org_plan":"build","kyc_level":1,
 "api_key":"va_live_…","credits":10.0,"created_at":"…"}
```

Additional: `GET /v1/orgs` (list children: `{"status":"success","orgs":[{org_id, org_display_name, org_plan, kyc_level, credits, status, created_at}]}`),
`GET /v1/orgs/{org_id}`, `POST /v1/orgs/{org_id}/suspend`, `POST /v1/orgs/{org_id}/reactivate`.
Max nesting depth: 1 (a child cannot create children) → `FORBIDDEN`.

### 8.3 Parent access rules

- Parent-org principals with role owner/admin (or API keys with `org:write`) may act on any child org by sending
  header `X-VA-Org-Id: <child_org_id>` (API) or via `switch-org` (dashboard). The API verifies
  `child.parent_org_id == ctx.org_id`, then builds a new `RequestContext` with `org_id=child`, `acting_parent_org_id=parent`,
  scopes unchanged, role preserved (admin acts as admin in child). Audit logs record both orgs.
- Child principals can never see the parent.
- Billing: children have independent balances; parent can transfer credits; usage rolls up in parent analytics
  (`GET /v1/analytics/usage?include_sub_accounts=true`).

### 8.4 Credit transfer

`POST /v1/billing/credits/transfer` `{to_org_id, amount}` (dollars, > 0, ≤ parent available balance): single DB
transaction writes `credit_ledger` `transfer_out` (parent) and `transfer_in` (child) with shared idempotency key
`xfer:{request Idempotency-Key or uuid}`; updates both balances with `SELECT … FOR UPDATE` on both org rows ordered
by id to avoid deadlocks. Also allowed child → parent by parent principals (`direction: "to_parent"`).

## 9. API key endpoints (envelope)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/v1/api_keys` | — | `{data:[{id,name,prefix,scopes,environment,created_by,last_used_at,expires_at,revoked_at,created_at}]}` |
| POST | `/v1/api_keys` | `{name (1–100), scopes?: string[] (default ["*"]), environment?: "live"|"test", expires_at?: datetime}` | `{data:{...key, key:"va_live_…"}}` (plaintext once) |
| PATCH | `/v1/api_keys/{id}` | `{name?}` | key |
| POST | `/v1/api_keys/{id}/revoke` | — | key with `revoked_at` |
| POST | `/v1/api_keys/{id}/roll` | `{expire_old_in_hours?: 0–168 (default 24)}` | new key (plaintext once); old key gets `expires_at` |

Rule: requested scopes must be a subset of the caller's scopes; otherwise `FORBIDDEN`.

## 10. Agent onboarding API (keys for AI agents)

Purpose: let an autonomous coding agent obtain a scoped **test** key for a human who confirms by email.

1. `POST /v1/agent-onboarding/keys` (public, rate limited 5/h per IP and 3/h per email)
   `{email, agent_name (1–100), requested_scopes: string[] (subset of non-billing scopes), purpose (≤ 500)}` →
   `{data:{request_id, status:"pending_verification", poll_url:"/v1/agent-onboarding/keys/{request_id}", expires_at}}`.
   Sends email to `email` with approve link `{DASHBOARD_BASE_URL}/agent-onboarding/{token}`.
2. Human opens link: if logged out → login/signup; chooses org (where they are owner/admin/developer) and confirms
   scopes (can reduce). Approval creates an API key `environment="test"`, `name="agent:<agent_name>"`,
   `expires_at = now + 30 days`, stored encrypted in Redis `agentkey:{request_id}` (TTL 15 min, retrievable once).
3. Agent polls `GET /v1/agent-onboarding/keys/{request_id}` (requires the `poll_secret` returned in step 1 as header
   `X-Poll-Secret`) → `pending_verification` | `approved` (with `key` once, then `delivered`) | `denied` | `expired`.
4. State kept in Redis `agentreq:{request_id}` (TTL 24 h): `{email_hash, agent_name, scopes, poll_secret_hash, status, approver_user_id?, org_id?}`.
5. Audit log `api_key.create` with `actor_type=user` (approver) and `changes.after.agent_name`.

## 11. Request rate limiting per API key

Token bucket in Redis (`rl:key:{api_key_id}:{bucket}`, Lua script atomic refill+take). Session principals use
`rl:user:{user_id}:{bucket}` with the same limits as the org plan.

| Plan | `default` bucket (all endpoints) | `calls_send` bucket (`POST /v1/calls`, batch create counts 1) | `burst` |
|---|---|---|---|
| start | 10 req/s | 2 req/s | 20 |
| build | 50 req/s | 10 req/s | 100 |
| scale | 100 req/s | 25 req/s | 200 |
| enterprise | 300 req/s (override via `orgs.limits.rate_per_sec`) | 100 req/s | 600 |

Responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`; on exhaustion `RATE_LIMITED`
with `Retry-After` seconds (ceil). Call caps (daily/hourly/concurrency) are separate (`24-billing-and-limits.md`).

> Decision (2026-10-29): This table is normative for request rates; the older numbers in specs/24 §6 item 1 are
> replaced by a pointer here. Implementation: `va_common.token_bucket` (Lua, registered with `register_script`, so
> EVALSHA with automatic reload on `NOSCRIPT`; no I/O at startup) and `va_api.rate_limit`.
> - **Capacity.** The `burst` column is the `default` bucket capacity (2 × rate). `calls_send` capacity is also
>   2 × its rate (4 / 20 / 50 / 200). `orgs.limits.rate_per_sec` (positive integer, any plan, admin-set; it takes
>   precedence over the plan as in specs/24 §1) replaces the `default` rate with capacity 2 × override;
>   `calls_send` has no override. An unknown `plan_id` gets the `start` limits (warning `rate_limit_unknown_plan`).
> - **Keys.** `rl:key:{api_key_id}:<bucket>` and `rl:user:{user_id}:<bucket>` (user and platform_admin principals),
>   `<bucket>` ∈ `default`, `calls_send`; the id is the hash tag. Widgets use `widget:rl:*` instead.
> - **Charging.** Every authenticated request takes 1 token from `default` right after authentication, before
>   scope checks (a request whose key fails authentication is not charged and gets no rate headers). Routes that
>   send calls (`POST /v1/calls`; batch create costs 1) also declare `Depends(rate_limit("calls_send"))`. A
>   `default` token is not refunded when `calls_send` denies; a `default` denial never reaches `calls_send`.
> - **Headers.** Every response of a charged request (errors included) carries `X-RateLimit-Limit` = capacity of
>   the last charged bucket, `X-RateLimit-Remaining` = whole tokens left, `X-RateLimit-Reset` = seconds (ceil) until
>   that bucket is full again (a delta, not an epoch). A 429 adds `Retry-After` = max(1, ceil(wait)) and the message
>   `Rate limit exceeded; retry after N second(s).` The api CORS expose list (`va_api.main.CORS_EXPOSE_HEADERS`;
>   specs/28 §5 does not enumerate it) includes the three `X-RateLimit-*` headers next to `X-Request-Id` and
>   `Retry-After`.
> - **Time and failure.** `now_ms` comes from the api's injected `Clock`, not Redis `TIME` (deterministic tests; api
>   pods are NTP-synced, and a clock behind the stored `ts` never mints tokens). Redis unavailable → fail open: the
>   request proceeds without rate headers and `rate_limit_unavailable` is logged (same policy as the key cache).

## 12. Idempotency

Header `Idempotency-Key` (1–255 chars) honored on all `POST` endpoints. Middleware: compute
`request_hash = sha256(method+path+canonical JSON body)`; insert `idempotency_keys` row with `locked_until=now()+60s`
(ON CONFLICT DO NOTHING). If existing row: same hash and response stored → replay stored status/body with header
`Idempotent-Replayed: true`; same hash and locked → `IDEMPOTENCY_IN_PROGRESS`; different hash →
`IDEMPOTENCY_KEY_REUSED`. Store response for 2xx and 4xx (not 5xx; 5xx deletes the row). Rows purged after 24 h by
`RetentionWorkflow`.

> Decision (2026-10-29): idempotency details. Implementation: `va_db.repositories.IdempotencyRepo` (tenant
> session, RLS plus explicit `org_id` filter) and `va_api.idempotency`.
> - **Where.** The org id is the key space (PK `(org_id, key)`), so the claim is taken inside the `require_scope`
>   dependency once the scope check passes, at most once per request. Requests failing authentication (401), the
>   `default` rate bucket (429), or the scope check (403) never create a row. Routes without `require_scope` (the
>   public allowlist) and non-`POST` methods ignore the header. A capture middleware (pure ASGI,
>   inside the rate-limit header middleware so replays still carry `X-RateLimit-*`) settles the claim before the
>   last response chunk is sent.
> - **Key syntax.** 1–255 printable ASCII characters (0x20–0x7E); otherwise 400 `INVALID_INPUT` with
>   `field: "Idempotency-Key"` and message `Idempotency-Key must be 1-255 printable ASCII characters.`
> - **Hash.** `sha256(method + "\n" + path + "\n" + body)` in hex, `path` being the concrete request path without
>   the query string. A body that parses as JSON is canonicalised first (sorted keys, no whitespace, UTF-8), any
>   other body is hashed as raw bytes (empty body → empty bytes). Bodies over 64 KiB are hashed in a worker thread.
> - **Stored.** 2xx and 4xx responses whose body is JSON (status plus parsed body in `response_body`, lock
>   cleared). Released (row deleted, the retry runs again): 5xx, unhandled exceptions, **429** (a throttle such as
>   `calls_send` after the claim must stay retryable per its `Retry-After`), and empty or non-JSON bodies (not
>   replayable faithfully). A failure writing the response is logged as `idempotency_store_failed` (org and
>   status only, never the key), the claim is released, and the response is still delivered. A cancelled request
>   (client disconnect) leaves its lock to expire.
> - **Replay.** Stored status and JSON body (JSON-equal, not byte-equal: `jsonb` normalises key order),
>   `Content-Type: application/json`, `Idempotent-Replayed: true`; the body keeps the original `request_id` while
>   `X-Request-Id` is the new request's. Each replay increments `va_idempotent_replays_total`.
>   `Idempotent-Replayed` is in the api CORS expose list (`va_api.main.CORS_EXPOSE_HEADERS`).
> - **Precedence and expiry.** A different hash wins over everything (`IDEMPOTENCY_KEY_REUSED` 422, message
>   `Idempotency-Key was already used with a different request.`), even while the original is in flight; same
>   hash with a stored response replays; same hash while `locked_until > now` is `IDEMPOTENCY_IN_PROGRESS` 409
>   (`A request with this Idempotency-Key is still in progress.`). A row without a response whose lock expired
>   (crashed worker) is re-claimed. A row older than 24 h that the purge has not yet deleted is treated as absent
>   and re-claimed in place. Times come from the api's injected `Clock`.

## 13. Audit logging

- Written via `va_db.audit.record(ctx, action, resource_type, resource_id, before, after)` in the same transaction.
- Actions logged (complete list): `auth.login`, `auth.login_failed`, `auth.logout`, `auth.mfa_enabled`,
  `auth.mfa_disabled`, `auth.password_changed`, `auth.password_reset`, `auth.sso_login`, `org.update`,
  `org.delete`, `org.ownership_transfer`, `org.sso_update`, `org.secret_set`, `org.secret_delete`,
  `subaccount.create`, `subaccount.suspend`, `subaccount.reactivate`, `member.invite`, `member.role_change`,
  `member.remove`, `invitation.revoke`, `api_key.create`, `api_key.revoke`, `api_key.roll`, `pathway.create`,
  `pathway.update`, `pathway.delete`, `pathway.publish`, `pathway.promote`, `persona.publish`, `agent.version_create`,
  `agent.env_pin`, `experiment.start`, `experiment.stop`, `tool.create`, `tool.update`, `tool.delete`, `kb.create`,
  `kb.delete`, `voice.clone`, `voice.delete`, `number.purchase`, `number.release`, `number.inbound_update`,
  `telnyx_account.create`, `telnyx_account.delete`, `tendlc.brand_submit`, `tendlc.campaign_submit`,
  `widget.create`, `widget.update`, `widget.delete`, `webhook_endpoint.create`, `webhook_endpoint.update`,
  `webhook_endpoint.delete`, `webhook.secret_rotate`, `dnc.add`, `dnc.remove`, `dnc.import`, `memory.reset`,
  `memory.erase`, `contact.erase`, `billing.topup`, `billing.auto_recharge_update`, `billing.plan_change`,
  `billing.credit_transfer`, `call.stop`, `call.takeover`, `call.recording_download`, `batch.create`, `batch.stop`,
  `data.export`.
- Redaction: keys named `password`, `secret`, `token`, `api_key`, `client_secret`, `authorization`,
  `secret_headers`, any value matching the API key regex, and all `*_enc` fields are replaced with `"[REDACTED]"`
  before persisting `changes`.
- `GET /v1/audit_logs?action=&actor_id=&resource_type=&resource_id=&since=&until=&cursor=&limit=` (envelope,
  `audit.read`). Retained per org `retention_days` (minimum 365 days when `hipaa_mode`).

## 14. Tests required

- Key parsing: Bearer/bare/lowercase scheme; `x-api-key` ignored; wrong length/prefix; revoked; expired; constant-time
  dummy hash path exercised (assert hash function called once on unknown prefix).
- Scope coverage test (§2.4) and role matrix test (§3.3 vs §3.2 table parse).
- Tenant isolation: for every resource router, a parametrized test creates the resource in org A and asserts org B's
  key gets `NOT_FOUND` on GET/PATCH/DELETE and the resource is absent from org B lists; RLS test runs raw SQL as
  `va_app` without `app.org_id` set and asserts zero rows.
- Sub-account: `X-VA-Org-Id` allowed for parent admin, denied for sibling/child principals, nesting depth.
- Sessions: sliding expiry, absolute max, CSRF missing/mismatch/origin, MFA gating, TOTP replay, recovery code consumption.
- SSO: state/nonce validation, JIT provisioning paths, enforce blocks password login (IdP mocked with respx + a
  generated RSA JWKS).
- Rate limiter: refill math with `freezegun`, headers, per-plan defaults.
- Idempotency: replay, in-progress, reuse with different body, 5xx not stored.
- Agent onboarding: full approve/deny/expire flows, poll secret required, single retrieval.
- Audit redaction.

## 15. Acceptance criteria

- All endpoints in this spec exist, appear in OpenAPI with correct `x-envelope`, and pass the tests above.
- No route lacks a scope or allowlist entry; matrix test passes.
- Cross-tenant access is impossible in every resource test; RLS denies without context.
