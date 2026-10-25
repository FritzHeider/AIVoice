# Phase 20 — Infrastructure, Load, Live Validation, Runbooks, Final Acceptance

- [ ] P20-T01 Production Dockerfiles
  - Spec refs: specs/29-infrastructure.md (Dockerfiles)
  - Depends on: P19-T08
  - Deliverables: multi-stage, non-root images for every service (Deno included for media-worker/worker; model and assets baked into media-worker), `.dockerignore`, image size budgets
  - Acceptance: `docker build` for all; Trivy no critical vulns; containers start with health OK.

- [ ] P20-T02 docker-compose profiles complete
  - Spec refs: specs/29-infrastructure.md (docker-compose)
  - Depends on: P20-T01
  - Deliverables: `apps` and `e2e` profiles with every service, env wiring, healthchecks, dependency ordering
  - Acceptance: `make up PROFILE=apps` all healthy; `make e2e` uses images.

- [ ] P20-T03 Helm chart
  - Spec refs: specs/29-infrastructure.md (Helm chart)
  - Depends on: P20-T01
  - Deliverables: `infra/helm/voiceagents` with deployments, services, ingress (WebSocket), HPA (media-worker on active_calls custom metric via prometheus-adapter), PDBs, preStop drain, network policies, ServiceMonitors, secrets references, values for staging/production
  - Acceptance: `helm lint`, `helm template` + kubeconform validation in CI.

- [ ] P20-T04 Terraform for AWS
  - Spec refs: specs/29-infrastructure.md (Terraform AWS modules)
  - Depends on: P20-T03
  - Deliverables: modules (vpc, eks, rds, elasticache, s3, kms, iam/irsa, route53, acm, load balancers, clickhouse, temporal) and environment stacks
  - Acceptance: `terraform fmt -check`, `terraform validate`, tflint pass.

- [ ] P20-T05 CI/CD workflows
  - Spec refs: specs/29-infrastructure.md (CI/CD GitHub Actions)
  - Depends on: P20-T02, P20-T03
  - Deliverables: `e2e.yml`, `release.yml` (build/push to ECR, helm upgrade with approvals), `live-smoke.yml` (nightly, secrets), updated `ci.yml`
  - Acceptance: actionlint passes.

- [ ] P20-T06 Load test
  - Spec refs: specs/30-testing-and-evals.md (load test harness and pass criteria); specs/00-overview.md §5
  - Depends on: P20-T02
  - Deliverables: `scripts/load/` (k6 API scenarios; asyncio simulator swarm driving 1 000 concurrent calls through FakeTelnyx/FakeElevenLabs/FakeLLM in real-time mode across multiple media-worker containers), `LOADTEST_REPORT.md` generated with latency distributions and resource usage
  - Acceptance: thresholds in specs/00 §5 met.

- [ ] P20-T07 Live validation with real vendors
  - Spec refs: specs/30-testing-and-evals.md (live smoke tests, voice quality evals); specs/06-telephony-telnyx.md (bootstrap script)
  - Depends on: P20-T02
  - Deliverables: `tests_live/test_live_calls.py` (outbound call to a Telnyx test number answered by a second Telnyx number running a scripted echo agent, inbound call, SMS round trip, voicemail detection against a configured voicemail number, latency capture), `scripts/live_tunnel.md` instructions, WER/latency report generator
  - Acceptance: runs when credentials exist (evidence recorded); otherwise recorded as skipped with instructions verified by a dry-run mode.

- [ ] P20-T08 Runbooks and README
  - Spec refs: specs/29-infrastructure.md (backup/restore, disaster recovery runbook); specs/27-observability.md (alert rules)
  - Depends on: P20-T03
  - Deliverables: `docs/runbooks/` (deploy, rollback, incident response per alert, DR restore, key rotation, Telnyx/ElevenLabs outage playbooks, scaling media workers), README build status and operations sections updated
  - Acceptance: every alert rule links to an existing runbook section (test).

- [ ] P20-T09 Final spec-conformance audit
  - Spec refs: specs/00-overview.md (Scope — the complete feature inventory, Performance targets)
  - Depends on: P20-T06, P20-T07, P20-T08
  - Deliverables: `ACCEPTANCE.md` listing every "Acceptance criteria" item from specs 00–31 with status and evidence (test ids, report links); new fix tasks appended for any failure, then resolved
  - Acceptance: all items pass.

## Phase exit gate
All gates in prompts/phase-20.md pass and ACCEPTANCE.md is fully green.
