# 31 — Error Catalog and Response Envelopes

## 1. Envelope matrix

Two response envelopes exist for Bland compatibility. Which one an endpoint uses is fixed per endpoint family.

| Family | Success body | Error body |
|---|---|---|
| **Legacy** — `/v1/calls*`, `/v1/inbound*`, `/numbers/*`, `/v1/pathway*`, `/v1/tools*`, `/v1/voices*`, `/v1/accounts*`, `/v1/orgs*`, `/v1/prompts*`, `/v2/batches*` | `{"status":"success", ...resource fields...}` (exact shapes per endpoint spec) | `{"status":"error","message":"<human message>","errors":[{"error":"<CODE>","message":"…","field":"…"?}]}` |
| **Envelope** — `/v1/knowledge*`, `/v1/sms*`, `/v1/widget*`, `/v1/memory*`, `/v1/personas*`, `/v1/agents*`, `/v1/citation_schemas*`, `/v1/evals*`, `/v1/webhooks*`, `/v1/dispositions*`, `/v1/usage*`, `/v1/billing*`, `/v1/dnc*`, `/v1/contacts*`, `/v1/number_pools*`, `/v1/experiments*`, `/v1/me`, `/v1/org*`, `/v1/api_keys*`, `/v1/audit_logs*`, dashboard-only `/internal/*` | `{"data": <resource or list>, "errors": null, "pagination"?: {...}}` | `{"data": null, "errors":[{"error":"<CODE>","message":"…","field":"…"?,"details"?:{…}}]}` |

Every error response also carries header `X-Request-Id` and the body field `request_id` at top level.

Validation errors (422 from Pydantic) are re-rendered: one `errors[]` item per invalid field, `error="INVALID_INPUT"`,
`field` as a dotted path (`voicemail.action`), `message` from the validator. HTTP status for validation is **400**
(Bland parity), not 422.

## 2. Pagination

Envelope endpoints use cursor pagination: query `limit` (1–100, default 25) and `cursor` (opaque base64 of
`(created_at, id)`); response `pagination: {"next_cursor": "…" | null, "has_more": bool}`. Legacy list endpoints
keep Bland's parameters where documented (`/v2/batches/list?take=`, `/v1/calls?from=&to=&limit=&ascending=&start_date=&end_date=&batch_id=&completed=&answered_by=&inbound=&duration_gt=&duration_lt=&campaign_id=`)
and return `{"total_count": n, "count": m, "calls":[…]}`.

## 3. Error codes (complete)

| Code | HTTP | Meaning / when raised |
|---|---|---|
| `AUTH_FAILURE` | 401 | Missing/invalid/revoked/expired API key or session |
| `MFA_REQUIRED` | 401 | Session lacks MFA but org enforces it |
| `FORBIDDEN` | 403 | Authenticated but role/scope lacks permission |
| `ORG_SUSPENDED` | 403 | Org status `suspended`/`closed` |
| `NOT_FOUND` | 404 | Resource absent or belongs to another org (never reveal existence) |
| `INVALID_INPUT` | 400 | Validation failure |
| `CONFLICT` | 409 | Unique constraint / state conflict (e.g. publishing a non-draft) |
| `IDEMPOTENCY_KEY_REUSED` | 422 | Same `Idempotency-Key` with a different request body hash |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | Concurrent request with the same key still running |
| `RATE_LIMITED` | 429 | Request-rate token bucket exhausted (`Retry-After` header set) |
| `DAILY_CAP_REACHED` | 429 | Org daily call cap |
| `HOURLY_CAP_REACHED` | 429 | Org hourly call cap |
| `CONCURRENCY_CAP_REACHED` | 429 | Org concurrency cap |
| `SAME_NUMBER_TOO_SOON` | 429 | Message: "Please wait at least 10 seconds between calls to the same number." |
| `NEW_ACCOUNT_LIMIT` | 429 | Org younger than 24 h and > 100 calls/day or > 10 concurrent (tightened limits) |
| `INTERNATIONAL_NOT_ENABLED` | 403 | Destination country not allowed for the org's plan/kyc_level |
| `INTERNATIONAL_RATE_LIMITED` | 429 | International calls per hour exceeded (start: 10/h, build: 100/h, scale: 300/h) |
| `NUMBER_BLOCKED` | 403 | Destination on platform blacklist or org DNC ("This number is blocked.") |
| `OUTSIDE_CALLING_HOURS` | 400 | Org enforces calling hours and destination local time is outside window |
| `INSUFFICIENT_CREDITS` | 402 | Balance + credit limit below required hold |
| `INVALID_PHONE_NUMBER` | 400 | Not parseable to E.164 or not dialable |
| `RUNNER_CONFLICT` | 400 | More than one of `agent_id`/`persona_id`+`agent_id`/`pathway_id`+`agent_id`; or none of task/pathway/persona/agent |
| `AGENT_REQUIRES_ORG_KEY` | 403 | `agent_id` used with a non-org-scoped key |
| `PATHWAY_VERSION_NOT_FOUND` | 404 | Requested numeric version absent and no fallback allowed |
| `VOICE_NOT_FOUND` | 404 | |
| `TOOL_NOT_FOUND` | 404 | |
| `KB_ERROR` | 400/500 | Knowledge base processing error (Bland code) |
| `KB_UNSUPPORTED_FILE` | 400 | Not pdf/txt/md/doc/docx/html/csv |
| `KB_FILE_TOO_LARGE` | 413 | > 50 MB |
| `INVALID_ENCRYPTED_KEY` | 400 | Message: "Invalid `encrypted_key`." (BYO Telnyx) |
| `CARRIER_CONNECTION_ERROR` | 502 | Message: "Error in creating Telnyx connection." |
| `CARRIER_ERROR` | 502 | Telnyx API returned an error; `details.telnyx_errors` contains Telnyx `errors[]` (code, title, detail) |
| `NUMBER_UNAVAILABLE` | 409 | Requested number no longer available during purchase |
| `NO_NUMBERS_FOUND` | 404 | Search returned no inventory for area code/country |
| `RETRY_NOT_PROVIDED` | 400 | Message: "Retry attempt not provided for retry call." |
| `CALL_NOT_ACTIVE` | 409 | Stop/transfer on a call that already ended |
| `CALL_NOT_FOUND` | 404 | |
| `BATCH_NOT_FOUND` | 404 | |
| `BATCH_TOO_LARGE` | 400 | > 100 000 calls in one batch |
| `BATCH_NOT_STOPPABLE` | 409 | Terminal batch |
| `CHANNEL_NOT_SUPPORTED` | 400 | `imessage`/`rcs` |
| `SMS_NOT_ENABLED` | 400 | Number lacks SMS capability or 10DLC campaign not active for US long codes |
| `WIDGET_DOMAIN_NOT_ALLOWED` | 403 | Origin not in `allowed_domains` |
| `WIDGET_RATE_LIMITED` | 429 | Per-widget `messages_per_minute` exceeded |
| `VOICE_CLONE_LIMIT` | 403 | Plan voice clone cap reached |
| `VOICE_CLONE_FAILED` | 502 | ElevenLabs rejected the samples (`details.provider_message`) |
| `SAMPLE_INVALID` | 400 | Audio sample too short (< 5 s), too long (> 10 min total), or unsupported format |
| `GUARD_RAIL_LIMIT` | 400 | More than 5 custom guard rails |
| `FEATURE_NOT_IN_PLAN` | 403 | Enterprise-only feature (citations, custom guard rails, number pools, experiments) on a lower plan |
| `CUSTOM_CODE_ERROR` | 400 | Custom code node failed validation (syntax) at publish time |
| `WEBHOOK_URL_REJECTED` | 400 | SSRF-blocked or insecure URL |
| `UPSTREAM_TIMEOUT` | 504 | Vendor timeout on a synchronous endpoint |
| `SSO_REQUIRED` | 403 | Org enforces SSO; password login refused |
| `EMAIL_NOT_VERIFIED` | 403 | Action requires a verified email |
| `INVALID_CREDENTIALS` | 401 | Wrong email/password (generic message, no user enumeration) |
| `INVALID_MFA_CODE` | 401 | Wrong or reused TOTP/recovery code |
| `PASSWORD_TOO_WEAK` | 400 | Password fails policy (`04-auth-tenancy-rbac.md`) |
| `TOKEN_EXPIRED` | 400 | Email verification / reset / invitation token expired or used |
| `REAUTH_REQUIRED` | 401 | Sensitive action needs recent password/MFA re-entry |
| `AUTH_LOCKED` | 429 | Too many failed login attempts; `Retry-After` set |
| `INTERNAL_ERROR` | 500 | Unhandled; message "Internal server error"; details never leak stack traces |
| `SERVICE_UNAVAILABLE` | 503 | Dependency down / draining |

## 3a. Detail codes (not HTTP codes)

Some errors carry finer-grained codes in `errors[].details.code` while the top-level `error` stays a code from §3
(for example `INVALID_INPUT`). These detail codes are excluded from the §3 parity test and are enumerated here:

- Pathway validation (`12-pathways-api.md`, `11-pathways-engine.md` validation rules), top-level `INVALID_INPUT`:
  `PATHWAY_START_NODE`, `PATHWAY_DUPLICATE_NODE_ID`, `PATHWAY_DUPLICATE_NODE_NAME`, `PATHWAY_EDGE_DANGLING`,
  `PATHWAY_EDGE_LABEL`, `PATHWAY_EDGE_CONFLICT`, `PATHWAY_UNREACHABLE`, `PATHWAY_GLOBAL_LABEL`, `PATHWAY_NODE_EMPTY`,
  `PATHWAY_WEBHOOK_URL`, `PATHWAY_TRANSFER_TARGET`, `PATHWAY_KB_MISSING`, `PATHWAY_TOOL_MISSING`, `PATHWAY_TARGET_MISSING`,
  `PATHWAY_TEMPLATE_SYNTAX`, `PATHWAY_VAR_NAME`, `PATHWAY_TOO_LARGE`.
- In-call warnings are not HTTP errors; they are `call.error` event data codes listed in `19-events-and-webhooks.md` §2.1.

## 4. Implementation

- `va_common.errors.AppError` holds `code`, `message`, `http_status`, `field`, `details`.
- `va_common.errors.CODES: dict[str, int]` maps each code above to its HTTP status; a test asserts that every row of
  this table (parsed from this markdown file) is present with the same status, and vice versa.
- `va_api.errors.install_handlers(app)` renders according to `route.openapi_extra["x-envelope"]` (`legacy` or
  `envelope`), set by router-level dependencies per family in §1.
- Legacy error `message` for the top-level field equals the first error's message.

> Decision (2026-10-25): `KB_ERROR` is the only code whose §3 cell lists two statuses (`400/500`).
> `CODES["KB_ERROR"]` is `400` (the first listed status: client-caused processing errors are the default) and `500`
> is accepted only as an explicit `http_status` override, declared in `va_common.errors.ALTERNATE_STATUSES`.
> `AppError` rejects any status that is neither the code's default nor one of its alternates. The parity test treats
> a multi-status cell as `{default} ∪ alternates`, with the default equal to the first status in the cell; it also
> asserts `CODES` lists the codes in table order (62 rows).
