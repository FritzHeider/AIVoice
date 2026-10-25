## Phase 19 — Observability and Security Hardening

### Goal
Make the platform operable and defensible in production: complete tracing and metrics across all services, Grafana
dashboards and alert rules, Sentry, finalized health/readiness, retention and right-to-erasure workflows, partition
lifecycle, security headers/CSP/CORS, toll-fraud and abuse controls, AWS KMS integration and key rotation, dependency and
secret scanning in CI, and an authorization fuzz suite that proves the threat-model mitigations.

### Read first
- `specs/27-observability.md` — entire file.
- `specs/28-security.md` — entire file.
- `specs/23-guardrails-and-compliance.md` — retention and erasure sections.

### Phase exit gate
`make check && make e2e` pass; `infra/grafana` dashboards and `infra/prometheus/alerts.yml` validate (promtool check
rules, dashboard JSON schema test); security test suite green; CI includes pip-audit, pnpm audit (high+ fails), gitleaks,
and Trivy image scans.
