# Phase 19 — Observability and Security Hardening

- [ ] P19-T01 Tracing and metrics completion
  - Spec refs: specs/27-observability.md (OpenTelemetry span names, Prometheus metrics list)
  - Depends on: P15-T08
  - Deliverables: every span and metric in the spec implemented in its service, metric registry test comparing to the spec table
  - Acceptance: test parses the spec metrics table and asserts each metric is registered with the right type and labels.

- [ ] P19-T02 Dashboards and alerts
  - Spec refs: specs/27-observability.md (Grafana dashboards, alert rules, SLOs)
  - Depends on: P19-T01
  - Deliverables: `infra/grafana/dashboards/*.json`, provisioning config in compose, `infra/prometheus/alerts.yml` with exact PromQL, promtool validation in CI
  - Acceptance: promtool passes; dashboard JSON validated.

- [ ] P19-T03 Sentry and health finalization
  - Spec refs: specs/27-observability.md (Sentry config, health/readiness endpoints)
  - Depends on: P19-T01
  - Deliverables: Sentry init with PII scrubbing in all services, readiness semantics per service exactly as specified (drain-aware for media worker)
  - Acceptance: tests for scrubbing and readiness transitions.

- [ ] P19-T04 Retention, partitions, erasure
  - Spec refs: specs/28-security.md (retention jobs, right-to-erasure endpoint); specs/23-guardrails-and-compliance.md (retention and erasure)
  - Depends on: P01-T13
  - Deliverables: `RetentionWorkflow` (daily: recordings by `recording_retention_days`, transcripts/events by `retention_days`, partition create/drop), erasure endpoint + workflow (contact-scoped deletion across tables, S3, ClickHouse mutations, vendor deletion requests where supported), `erasure_requests` tracking
  - Acceptance: time-skipping tests; erasure leaves no rows for the contact (verified by scanning tables).

- [ ] P19-T05 HTTP security, CORS, CSP
  - Spec refs: specs/28-security.md (TLS, CORS, CSP headers for dashboard and widget, input validation)
  - Depends on: P16-T01
  - Deliverables: security headers middleware for api, Next.js headers config, widget CSP guidance, strict CORS per environment
  - Acceptance: header snapshot tests.

- [ ] P19-T06 Toll-fraud and abuse controls
  - Spec refs: specs/28-security.md (rate limiting/abuse, toll fraud prevention)
  - Depends on: P11-T08
  - Deliverables: high-cost destination blocklist, velocity checks per org/destination prefix, anomaly auto-suspension with admin notification, signup abuse limits
  - Acceptance: tests for each control.

- [ ] P19-T07 KMS integration and key rotation
  - Spec refs: specs/28-security.md (envelope encryption design, key rotation, encrypted columns list)
  - Depends on: P00-T07
  - Deliverables: AWS KMS provider wiring via settings, rotation job re-wrapping DEKs for every encrypted column listed, rotation runbook
  - Acceptance: rotation test re-encrypts fixtures and old key decrypt still works during grace period.

- [ ] P19-T08 Supply-chain and secret scanning in CI
  - Spec refs: specs/28-security.md (dependency scanning); specs/29-infrastructure.md (CI/CD)
  - Depends on: P00-T14
  - Deliverables: pip-audit, pnpm audit, gitleaks, Trivy steps with failure thresholds, SBOM generation (syft) on release
  - Acceptance: CI configuration validated; local script `scripts/security_scan.sh` runs the same tools.

- [ ] P19-T09 Authorization fuzz and threat-model verification
  - Spec refs: specs/28-security.md (threat model, pen-test checklist)
  - Depends on: P02-T15
  - Deliverables: extended route fuzzing (role × route matrix from RBAC table, malformed ids, oversized payloads, header smuggling cases), SSRF regression suite across webhooks/tools/crawler/dynamic data, test mapping each STRIDE row to at least one automated test
  - Acceptance: suite green; mapping file `specs/28-security.md` table cross-referenced by test ids.

## Phase exit gate
`make check && make e2e` pass; observability and security validations pass.
