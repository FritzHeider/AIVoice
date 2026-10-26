# 28 — Security

## 1. Threat model (STRIDE)

| # | Asset / flow | Threat (STRIDE) | Mitigation (section) |
|---|---|---|---|
| T1 | API keys | Spoofing: stolen key used by attacker | Hashed storage, prefix display, revocation/roll, IP allowlist, anomaly alerts on new ASN/country (§9.4), scopes (`04`) |
| T2 | Dashboard sessions | Spoofing: session theft / CSRF | HttpOnly Secure cookies, SameSite=Lax, CSRF double-submit + Origin check, MFA, session revocation (`04` §4) |
| T3 | Telnyx webhooks | Spoofing: forged call events | Ed25519 signature + 5-minute timestamp tolerance, event-id dedupe (`06`) |
| T4 | Media WebSocket | Spoofing: attacker injects audio into a call | Ed25519 signature on WS upgrade + HMAC stream token bound to call id and expiry (§6.2) |
| T5 | Stripe webhooks | Spoofing: fake payments | `Stripe-Signature` verification, event dedupe, amounts re-fetched from Stripe API (`24`) |
| T6 | Tenant data | Information disclosure across tenants | `org_id` filtering + Postgres RLS FORCE, tenant isolation tests (`03` §10, `04` §14) |
| T7 | Recordings | Information disclosure | Private S3 buckets, SSE-KMS, per-org data keys for `hipaa_mode`, short-lived signed URLs (7 d max, 1 h in hipaa_mode), access audit (`call.recording_download`) |
| T8 | Vendor credentials (BYO Telnyx keys, tool secret headers, org LLM keys, SSO secrets, TOTP secrets) | Disclosure | Envelope encryption (§3), never returned by API, masked in UI and logs |
| T9 | Tools / dynamic data / webhooks / KB crawler | SSRF to internal network or cloud metadata | DNS-resolved IP blocklist, no redirects to blocked IPs, egress proxy in production (§6.4) |
| T10 | Custom code nodes | Elevation / RCE on worker | Deno sandbox without fs/env/run/ffi, network allowlist, CPU/mem/time limits, separate unprivileged UID (`13` §8, §6.5) |
| T11 | Prompt injection via caller speech or KB content | Tampering: agent coerced to reveal system prompt, call arbitrary tools, transfer to attacker | Tool allowlist per node, tool argument schema validation, transfer targets limited to configured numbers (LLM cannot invent transfer numbers), system prompt hardening, guard rails (`23`), output length caps |
| T12 | Toll fraud | Abuse: premium-rate / international pumping via outbound calls | International gating, high-cost prefix blocklist, velocity limits, new-account limits, KYC levels (§9) |
| T13 | Voice cloning abuse | Abuse: cloning voices without consent | Consent attestation stored with sample, clone caps per plan, audit, takedown process (§9.5) |
| T14 | API | Denial of service | Rate limits, request size limits, WAF at ALB, admission control on media workers |
| T15 | Audit trail | Repudiation | Append-only `audit_logs` (no UPDATE/DELETE grants for `va_app`), retention ≥ 365 d in hipaa_mode |
| T16 | Supply chain | Tampering via dependencies/images | Lockfiles, pinned digests, `pip-audit`, `pnpm audit`, Trivy image scans, Dependabot, SBOM (§11) |
| T17 | Secrets in repo/CI | Disclosure | gitleaks pre-commit + CI, GitHub OIDC to AWS (no static cloud keys), environment-scoped secrets (§2) |
| T18 | Dashboard XSS | Tampering / disclosure | React escaping, sanitized markdown only, strict CSP (§5) |
| T19 | Widget embedding | Abuse of widget key from other sites | Origin allowlist, per-visitor and per-widget rate limits, conversation tokens bound to visitor (`22`) |
| T20 | Transcript PII | Disclosure to analysts/viewers/vendors | Role-based masking, redaction pipeline (§7), hipaa_mode vendor logging off, BAA tracking |

## 2. Secrets management

- Runtime secrets come only from environment variables injected by Kubernetes Secrets synced from AWS Secrets
  Manager via External Secrets Operator (`29` §4). Local dev uses `.env` (gitignored).
- Platform secrets list: `SECRET_KEY`, `STREAM_TOKEN_SECRET`, `API_KEY_PEPPER`, `LOCAL_MASTER_KEY` (local/test only),
  `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` (not secret but managed alongside), `ELEVENLABS_API_KEY`, `LLM_DEFAULT_API_KEY`,
  `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `SMTP_URL`, `SENTRY_DSN`, DB passwords for `va_app`/`va_system`.
- Rotation: every secret has a documented rotation runbook in `infra/runbooks/secret-rotation.md`; `SECRET_KEY`,
  `STREAM_TOKEN_SECRET` support dual values (`*_PREVIOUS`) during rotation windows; `API_KEY_PEPPER` cannot rotate
  without re-issuing keys (documented; pepper is versioned per key in `api_keys.pepper_version`; rotation sets the new value in
  `API_KEY_PEPPER`, moves the old value to `API_KEY_PEPPER_PREVIOUS`, and keys with the old `pepper_version` keep
  verifying until re-issued).
- Tenant secrets (BYO Telnyx API keys, tool secret headers, org secrets, SSO client secrets, TOTP secrets,
  webhook signing secrets) are stored envelope-encrypted in Postgres (§3).

## 3. Envelope encryption (`va_common.crypto`)

- Algorithm: AES-256-GCM with 96-bit random nonce; associated data (AAD) = `"{table}.{column}:{row_id}"` binding
  ciphertext to its location (prevents swapping ciphertexts between rows).
- Ciphertext blob format (bytes): `version(1)=0x01 | kek_id_len(1) | kek_id(utf8) | edk_len(2, big-endian) |
  encrypted_data_key | nonce(12) | ciphertext | tag(16)`.
- Data keys: per-org 256-bit data key, generated on org creation, cached in process memory (LRU 10 000 entries,
  TTL 10 min) after unwrapping. Stored wrapped in `org_data_keys` table (new, see shared-contract additions:
  `org_data_keys(id uuid PK, org_id uuid FK, kek_id text, encrypted_data_key bytea, status text CHECK IN ('active','retired'),
  created_at, updated_at)`); the blob embeds the wrapped key so decryption never depends on table lookup order.
- KEK providers (`KMS_PROVIDER`):
  - `local`: KEK = `LOCAL_MASTER_KEY` (32 bytes hex), wrap = AES-256-GCM(KEK, data key), `kek_id="local:v1"`.
    Refused when `VA_ENV=production`.
  - `aws`: `kms:GenerateDataKey` / `kms:Decrypt` with `AWS_KMS_KEY_ID`, encryption context `{"org_id": "<uuid>"}`;
    `kek_id = key ARN`.
- API: `encrypt(org_id, plaintext: bytes, aad: str) -> bytes`, `decrypt(org_id, blob, aad) -> bytes`,
  `encrypt_str/decrypt_str` helpers, `platform_encrypt` (platform-level secrets with org_id = nil UUID).
- Encrypted columns (complete): `orgs.webhook_signing_secret_enc`, `users.mfa_totp_secret_enc`,
  `sso_connections.client_secret_enc`, `telnyx_accounts.api_key_enc`, `tools.secret_headers_enc`,
  `org_secrets.value_enc` (new table from `04` §7). Recording objects in S3 use SSE-KMS (bucket key), and in
  `hipaa_mode` additionally client-side encryption with the org data key (stream-chunked AES-GCM, 64 KiB chunks,
  per-chunk nonce = base nonce XOR chunk index) with metadata `x-amz-meta-va-enc=v1`.
- Rotation: `POST /internal/admin/orgs/{id}/rotate-data-key` creates a new active data key; a
  `ReencryptWorkflow` re-encrypts all encrypted columns for the org in batches of 500 rows; old key `retired` after
  completion. KEK rotation in AWS uses automatic KMS key rotation (no re-encryption needed).

> Decision (2026-10-25): `va_common.crypto` implements this section as follows.
> (a) The API is async (`await envelope.encrypt(...)`) because unwrapping an AWS KMS data key is network I/O; it
> lives on `Envelope(provider, store, cache=None)`, with `create_data_key`, `encrypt`, `decrypt`,
> `encrypt_str`/`decrypt_str`, `platform_encrypt`/`platform_decrypt` (org id `UUID(int=0)`) and `rewrap`.
> (b) The binary layout above is normative and is the same field list as the plan shorthand
> `v1|key_id|wrapped_dek|nonce|ciphertext`; `ciphertext | tag` is the AES-GCM output, so the tag is its last 16 bytes.
> (c) The local wrapped key is `nonce(12) | AES-256-GCM(KEK, data key, aad="org_id=<uuid>") | tag(16)`, binding the
> org exactly as the AWS encryption context does.
> (d) The active key comes from a `DataKeyStore.active_data_key(org_id)` protocol (implemented over `org_data_keys`
> in phase 01); decryption uses only the wrapped key embedded in the blob. The unwrapped-key cache is keyed by
> `(org_id, kek_id, encrypted_data_key)`.
> (e) `rewrap(org_id, blob, aad)` is the `ReencryptWorkflow` primitive: it decrypts with the embedded key and
> re-encrypts under the active key, returning the input unchanged when it is already under the active key.
> (f) Every failure to authenticate or parse raises `DecryptionError` (a `CryptoError`), whose message never carries
> key or plaintext material; the API surfaces it as `INTERNAL_ERROR`. A missing active key raises `CryptoError`.
> (g) `CryptoSettings` (extended by the api, worker and media settings) refuses `KMS_PROVIDER=local` with
> `VA_ENV=production`, requires `LOCAL_MASTER_KEY` to be 64 hex chars for `local` and `AWS_KMS_KEY_ID` for `aws`,
> at startup. The AWS KMS client uses connect timeout 5 s, read timeout 10 s, and 3 retries in standard mode.

## 4. Transport security

- TLS 1.2+ everywhere external (ALB/NLB with ACM certs, policy `ELBSecurityPolicy-TLS13-1-2-2021-06`). HSTS
  `max-age=63072000; includeSubDomains; preload` on api/dashboard/docs.
- Media WebSocket endpoint TLS terminates at NLB (TLS listener) → media pods over the VPC.
- Internal: Postgres `sslmode=require` (RDS), Redis TLS (ElastiCache in-transit encryption), ClickHouse HTTPS,
  Temporal TLS when Temporal Cloud is used.
- Outbound vendor calls verify certificates with system CA bundle; no `verify=False` anywhere (lint rule `S501`).

## 5. HTTP security headers, CORS, CSP

- **api**: CORS allow origin = `DASHBOARD_BASE_URL` only, credentials true, methods `GET,POST,PUT,PATCH,DELETE,OPTIONS`,
  headers `Authorization, Content-Type, Idempotency-Key, X-CSRF-Token, X-VA-Org-Id, Last-Event-ID`; widget public
  endpoints (`/v1/widget/public/*`) use dynamic CORS reflecting the Origin only when it matches the widget's
  `allowed_domains` (exact host or `*.domain` wildcard). API-key requests from browsers are not CORS-enabled except
  the widget endpoints. Headers on all responses: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` for authenticated responses.
- **dashboard** (Next.js middleware sets per-request nonce):
  `Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-{nonce}' 'strict-dynamic'; style-src 'self' 'unsafe-inline';
  img-src 'self' data: blob: https://*.amazonaws.com; media-src 'self' blob: https://*.amazonaws.com {S3_ENDPOINT_URL in local};
  connect-src 'self' {PUBLIC_API_BASE_URL} wss://{WIDGET_PUBLIC_HOST} wss://{api host}; font-src 'self'; frame-src 'self';
  worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'`;
  plus `X-Frame-Options: DENY`, `Permissions-Policy: microphone=(self), camera=(), geolocation=()`.
- **widget**: script served with `Cross-Origin-Resource-Policy: cross-origin`, `Cache-Control: public, max-age=300`,
  Subresource Integrity hash published per version; the widget renders inside a Shadow DOM (no iframe) and never
  uses `eval`; documented CSP requirements for host sites: `script-src {api host}`, `connect-src https://{api host} wss://{WIDGET_PUBLIC_HOST}`.
- Request limits: JSON body ≤ 1 MiB (batch create ≤ 50 MiB, uploads via multipart ≤ 50 MiB per file), header ≤ 16 KiB,
  URL ≤ 8 KiB; enforced at uvicorn/ALB and by middleware.

## 6. Input validation and isolation

### 6.1 Validation

Pydantic models with `extra="forbid"` on all request bodies (Bland-compat legacy endpoints use `extra="ignore"` and
log unknown fields at DEBUG); phone numbers validated with `phonenumbers` (`is_possible_number` and dialable type);
URLs via `HttpUrl` plus SSRF checks; JSONPath compiled at save time; templates parsed at save time; size caps on every
free-text field (task 20 000 chars hard cap, prompts 20 000, node prompt 10 000, KB text doc 5 MB).

### 6.2 Media WebSocket authentication

- `stream_token = base64url(call_id(16 bytes) | exp(8 bytes, unix) | HMAC_SHA256(STREAM_TOKEN_SECRET, call_id|exp)[:16])`,
  exp = dial time + max_duration + 10 min. Passed as `token` query param and `stream_auth_token`.
- On upgrade the media worker verifies: token HMAC (constant time, accepting `STREAM_TOKEN_SECRET_PREVIOUS`), exp,
  `call_id` param equals token call id, call status in (`dialing`,`ringing`,`in_progress`), and the Telnyx Ed25519
  signature headers over `{timestamp}|{full stream_url}` (`06`). Failure → close 4401 without reading media.
- Widget audio WS uses a conversation token (`22`).

### 6.3 Database access

`va_app` role has `SELECT, INSERT, UPDATE, DELETE` on tenant tables, `INSERT, SELECT` only on `audit_logs`,
`credit_ledger`, `usage_events` (append-only); `va_system` usage restricted to listed repositories (`03` §2). Raw SQL
uses bound parameters only (ruff `S608` enforced).

### 6.4 SSRF and egress

Shared `va_common.http.safe_client()` used for tools, dynamic data, webhook nodes, customer webhooks, KB crawling,
voice sample URL fetches: resolves hostname, rejects blocked ranges (`19` §8 list plus `0.0.0.0/8`, `192.0.0.0/24`,
`198.18.0.0/15`, `240.0.0.0/4`, `ff00::/8`, `::ffff:0:0/96` mapped private), connects to the vetted IP (pinning to
prevent DNS rebinding) with SNI/Host of the original name, max 3 redirects each re-validated, response size caps
(tools 1 MiB, crawler 10 MiB/page), timeouts. In production, egress from api/worker/media pods routes through an egress
proxy (Squid in the cluster, `29` §4) that also enforces the blocklist; vendor domains (`api.telnyx.com`,
`api.elevenlabs.io`, `api.us.elevenlabs.io`, `api.stripe.com`) bypass via NAT directly.

### 6.5 Custom code sandbox

Deno 2 subprocess per execution: `deno run --no-prompt --no-config --no-npm --no-remote --v8-flags=--max-old-space-size=64
--allow-net=<comma list of allowed hosts or none> /sandbox/runner.js`, stdin = JSON input, stdout = JSON output (≤ 256 KiB),
wall timeout = node timeout (default 3 s, max 10 s) enforced by `asyncio.wait_for` + `SIGKILL`; process runs as UID
`65534` in the container with `prlimit` CPU 5 s and address space 256 MiB; environment cleared; working dir is an
empty tmpfs. Details in `13-tools-and-dynamic-data.md` §8.

## 7. PII handling and redaction

- PII categories: phone numbers, names, emails, addresses, DOB, SSN, card numbers (PCI), health info (PHI).
- **DTMF capture for payments**: card data should be collected via Press-Button style DTMF capture nodes
  (`11`) with `sensitive: true`, where digits bypass the LLM and transcripts store `[REDACTED-DTMF]`; recordings mute
  the DTMF tones region when `pii_redaction != off` (both channels zeroed for the DTMF interval).
- **Real-time**: Scribe `entity_detection` (`08`) enabled when `pii_redaction != off`; detected `pci` entities in user
  utterances are replaced with `[REDACTED-PCI]` before the text reaches the LLM.
- **Post-call** (`PostCallWorkflow` step `redact`, `18`): when `pii_redaction` is `transcripts` or
  `transcripts_and_recordings`, transcripts (`call_turns.text`, `text_spoken`, `calls.summary`, analysis results)
  are rewritten replacing entity spans with `[REDACTED-<TYPE>]` using batch STT entity output plus a regex pass
  (cards via Luhn-validated 13–19 digit sequences, SSN `\b\d{3}-?\d{2}-?\d{4}\b`, emails, E.164 numbers). For
  `transcripts_and_recordings`, the recording is rewritten with entity time ranges replaced by silence (both
  channels) using word timestamps; the original is deleted after the redacted copy is verified.
- Masking for viewers (`04` §3.2): `va_compliance.redaction.mask_text` masks digits sequences ≥ 4 and emails at read time.
- Logs/Sentry redaction (`27` §1, §8).

## 8. Data retention and erasure

### 8.1 RetentionWorkflow (Temporal schedule, daily 03:17 UTC)

Activities (each batched 1 000 rows, idempotent, system scope):
1. Create `call_events` partitions for the next 3 months; drop partitions older than the max `retention_days`
   across orgs + 30 days.
2. For each org: delete `call_events` rows older than `retention_days` within surviving partitions; delete
   `call_turns`, `call_analyses`, `citations`, `eval_runs`, `guard_rail_events` for calls older than `retention_days`
   and null transcript-derived columns on `calls` (`summary`, `variables` → `{}`); keep the `calls` row (billing metadata)
   for 7 years.
3. Delete S3 recordings older than `recording_retention_days` (`recording_s3_key` set NULL); S3 lifecycle rule
   backstop expires objects after 3 650 days.
4. Delete `messages` older than `retention_days`; close and anonymize conversations.
5. Purge `idempotency_keys` older than 24 h, `telnyx_events` older than 30 days, `stripe_events` older than 90 days,
   `webhook_deliveries` older than 30 days, expired `sessions`, `email_tokens`, `invitations`.
6. ClickHouse: TTL handles `call_events`; for orgs with shorter retention run `ALTER TABLE va.call_turns DELETE WHERE org_id = … AND created_at < …` weekly.
7. Emit `system.retention_completed` metric/log with counts.

### 8.2 Right to erasure

`POST /v1/contacts/erase` (envelope, permission `memory.erase`) body `{phone_e164?: string, email?: string, external_id?: string}`:
starts `ErasureWorkflow` that, for matching contacts in the org: deletes memories and memory_changes, deletes
conversations/messages, redacts transcripts of calls to/from the number (text replaced with `[ERASED]`), deletes
recordings, removes the contact row, keeps billing rows with `to_number` hashed (`sha256:<hex>`), adds a DNC entry
(source `manual`, reason `erasure`) unless `add_to_dnc=false`; returns `{data:{erasure_id, status:"running"}}`;
`GET /v1/contacts/erase/{erasure_id}` returns status and counts. Audit `contact.erase`.

## 9. Abuse and toll-fraud prevention

### 9.1 International gating

Destination country derived with `phonenumbers.region_code_for_number`. Allowed iff destination ∈ {US, CA} or
(`OrgSettings.international_enabled` AND country ∈ `allowed_countries` AND org `kyc_level ≥ 1` AND plan ≠ start
unless `kyc_level ≥ 2`). Otherwise `INTERNATIONAL_NOT_ENABLED`. Hourly international caps per plan (`31` §3).

### 9.2 High-cost destination blocklist

`va_compliance.fraud.BLOCKED_PREFIXES` (platform list, not overridable by non-enterprise orgs). It contains the NANP
non-geographic premium codes `+1900` and `+1976` (always blocked, no override), plus these high-risk destinations,
blocked unless the org is on the enterprise plan and lists the prefix in `orgs.limits.allowed_high_risk_prefixes`
(Caribbean NANP countries such as `+1809` Dominican Republic are legitimate destinations and are governed only by
§9.1 international gating): `+882`, `+883` (international networks), `+870` (Inmarsat), `+881` (satellite), `+979` (international premium
rate), `+808` (shared cost), `+247` (Ascension), `+290` (St Helena), `+500` (Falklands), `+674` (Nauru), `+675`
(Papua New Guinea), `+677` (Solomon Islands), `+678` (Vanuatu), `+680` (Palau), `+682` (Cook Islands), `+683` (Niue),
`+686` (Kiribati), `+688` (Tuvalu), `+690` (Tokelau), `+691` (Micronesia), `+692` (Marshall Islands), `+239`
(São Tomé), `+245` (Guinea-Bissau), `+252` (Somalia), `+371 90` (Latvia premium). Org `blocked_prefixes` add to it.
Matching = longest-prefix on E.164 digits. Result `NUMBER_BLOCKED`.

### 9.3 Velocity checks

- New accounts (< 24 h): ≤ 100 calls/day, ≤ 10 concurrent, US/CA only (`NEW_ACCOUNT_LIMIT`).
- Per-org rolling detector every 5 min (worker activity `fraud_scan`): if > 30 % of last 200 outbound calls are < 10 s
  connected to distinct numbers in the same non-US country, or spend in last hour > 5 × the org's trailing 7-day hourly
  p95 and > $50, the org is auto-suspended for outbound (`orgs.limits.outbound_suspended=true`), owners emailed,
  platform admins alerted (`billing.payment_failed`-style system event `system.fraud_suspected` logged and paged).
- Same-number 10 s rule and per-destination daily cap (5 calls/day/number/org, enterprise configurable).

### 9.4 Key anomaly detection

API key usage from a new country (GeoLite2 ASN/country DB bundled, refreshed monthly by CI artifact) than seen in the
past 30 days triggers an email to org admins (no blocking) and an audit entry `api_key.anomaly`.

### 9.5 Voice cloning safeguards

Clone requests require `consent_attestation: true` and store attestation text, user id, IP, timestamp on the voice
row metadata (new column `voices.consent` jsonb, see shared-contract additions); plan caps; samples retained for
audit 1 year; takedown: platform admin endpoint `POST /internal/admin/voices/{id}/takedown` deletes the voice at
ElevenLabs and blocks reuse of the sample hash.

## 10. Audit

Defined in `04` §13. Additionally platform-admin actions are logged with `actor_type=user`,
`changes.platform_admin=true` into the target org's audit log.

## 11. Dependency and supply-chain security

- `uv lock` and `pnpm-lock.yaml` committed; Renovate/Dependabot weekly PRs.
- CI: `uv run pip-audit --strict` (fail on known vulns with fix available), `pnpm audit --audit-level high`,
  `gitleaks detect`, `trivy image --severity HIGH,CRITICAL --exit-code 1` on built images, `trivy config infra/`,
  `checkov -d infra/terraform`, `semgrep --config p/python --config p/typescript --error`.
- SBOM: `syft` generates CycloneDX for each image, attached to releases; images signed with `cosign` (keyless OIDC).
- Base images pinned by digest (`29` §1).

## 12. Penetration test checklist (run before GA and annually)

Authentication bypass and key enumeration; session fixation; CSRF on every state-changing dashboard route; IDOR on every
resource id across tenants and sub-accounts; RLS bypass attempts via crafted filters; SSRF via tools, dynamic data,
webhook nodes, webhooks, KB crawl (including DNS rebinding and redirects); custom code sandbox escape (fs, env,
subprocess, network to metadata IP, resource exhaustion); media WS hijack (replayed tokens, missing signature); Telnyx
and Stripe webhook forgery and replay; prompt injection to trigger unauthorized transfers/tools/data exfiltration;
widget origin spoofing and rate-limit bypass; XSS in transcripts, node names, KB content rendered in dashboard; file
upload attacks (zip bombs in docx, malformed PDFs, polyglots); rate-limit and cap bypass via concurrency races;
billing manipulation (negative amounts, double credit via idempotency races); toll-fraud scenarios; secrets exposure in
logs/errors; TLS configuration; CORS misconfiguration; open redirects in auth flows.

## 13. Compliance control mapping

| Control area | SOC 2 (TSC) | HIPAA (164.3xx) | GDPR | Implementation |
|---|---|---|---|---|
| Access control & least privilege | CC6.1, CC6.3 | 312(a)(1) | Art. 32 | RBAC (`04`), scopes, RLS, `va_system` restrictions |
| Authentication & MFA | CC6.1, CC6.2 | 312(d) | Art. 32 | argon2id, TOTP, SSO, enforced MFA |
| Encryption at rest | CC6.1, CC6.7 | 312(a)(2)(iv) | Art. 32 | RDS/S3/ElastiCache KMS, envelope encryption (§3) |
| Encryption in transit | CC6.7 | 312(e)(1) | Art. 32 | TLS (§4) |
| Audit logging | CC7.2 | 312(b) | Art. 30 | `audit_logs`, CloudTrail, retention |
| Monitoring & incident response | CC7.3–CC7.5 | 308(a)(6) | Art. 33–34 | `27` alerts, incident runbook `infra/runbooks/incident-response.md` (72 h breach notification workflow) |
| Change management | CC8.1 | 308(a)(8) | — | PR reviews, CI gates, signed images, Helm releases |
| Vendor management | CC9.2 | 308(b)(1) BAAs | Art. 28 | Subprocessor register `docs/legal/subprocessors.md` (Telnyx, ElevenLabs, AWS, Stripe), BAA tracking via `baa_signed_at`, hipaa_mode restricts features to BAA-covered vendors |
| Data retention & disposal | CC6.5 | 310(d)(2) | Art. 5(1)(e), 17 | RetentionWorkflow, ErasureWorkflow |
| Data minimization & purpose | P-series | 502(b) minimum necessary | Art. 5(1)(c) | PII redaction, viewer masking |
| Backup & DR | A1.2, A1.3 | 308(a)(7) | Art. 32(1)(c) | `29` §8 |
| Data subject rights | P5, P8 | 524 access | Art. 15–17, 20 | `/v1/contacts/erase`, contact export `GET /v1/contacts/{id}/export` (JSON of contact, memories, conversations, call list) |
| Data residency | — | — | Ch. V | `orgs.data_region` (EU deployment stack per `29` §7) |
| Recording consent | — | — | Art. 6 | `tcpa:recording_disclosure` guard rail, two-party consent states list (`23`) |

## 14. Tests required

Crypto round trip, AAD mismatch failure, blob format parsing, local provider refused in production, AWS provider via
moto; SSRF client against every blocked range, DNS rebinding simulation (resolver returning public then private),
redirect re-validation; stream token tamper/expiry; CSP header presence; body size limits; redaction regexes
(Luhn positives/negatives); recording silence redaction on a synthetic WAV (assert samples zeroed in ranges);
retention workflow with time-skipping env (rows removed per org settings); erasure workflow; fraud prefix matching
(longest prefix, org additions); velocity detector thresholds; key anomaly email trigger; append-only grants (UPDATE on
`audit_logs` as `va_app` fails).

## 15. Acceptance criteria

- All STRIDE mitigations are implemented and covered by the listed tests.
- CI security jobs (§11) pass with zero high/critical findings.
- Pen-test checklist executed with findings tracked; no open critical/high issues at GA.
