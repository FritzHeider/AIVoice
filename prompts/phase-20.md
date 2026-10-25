## Phase 20 — Infrastructure, Load Testing, Live Validation, Runbooks, and Final Acceptance

### Goal
Make the platform deployable and prove it end to end: production Dockerfiles, complete docker-compose profiles, Helm
chart, Terraform for AWS, CI/CD (CI, e2e, release, nightly live smoke), the 1 000-concurrent-call load test with report,
live validation against real Telnyx and ElevenLabs, operational runbooks, and a final spec-conformance audit that turns
every gap into a fix task until none remain.

### Read first
- `specs/29-infrastructure.md` — entire file.
- `specs/30-testing-and-evals.md` — load test harness, live smoke tests, voice quality evals.
- `specs/00-overview.md` §5 — performance acceptance targets.

### Phase-specific constraints
- Live validation runs only when `LIVE_TESTS=1` and credentials are present; if they are absent the task records
  "skipped: no credentials" in the plan evidence and the README explains how to run it. All other tasks must complete.
- The final audit (P20-T09) walks every spec file's "Acceptance criteria" section and records pass/fail with evidence
  in `ACCEPTANCE.md`; any failure becomes a new `P20-FIX-n` task in this plan.

### Phase exit gate
`make check && make e2e && make e2e-ui` pass; `helm lint` and `helm template` pass; `terraform validate` passes for all
modules; load test report meets thresholds; `ACCEPTANCE.md` shows every criterion passing.
