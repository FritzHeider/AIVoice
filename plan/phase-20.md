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

- [ ] P20-T04a Amazon Bedrock as the platform LLM provider
  - Spec refs: specs/10-llm-gateway.md §1 (providers), §10 (embeddings), §11 (metering); specs/02-repo-layout-and-conventions.md §6 (`LLM_*`, `EMBEDDINGS_*` rows); specs/29-infrastructure.md (secrets, IAM/IRSA); specs/30-testing-and-evals.md §7 (live smoke)
  - Depends on: P20-T04
  - Context (probe 2026-11-02, account 811106940928, us-east-2): Bedrock's OpenAI-compatible Chat Completions works on the `bedrock-mantle` endpoint `https://bedrock-mantle.us-east-2.api.aws/v1` (Bearer = Bedrock API key). Streaming with `stream_options.include_usage` and a forced `tool_choice` function call both passed for `zai.glm-4.7-flash` (TTFT ≈ 500 ms), `qwen.qwen3-next-80b-a3b-instruct` (≈ 360 ms), `moonshotai.kimi-k2.5` (≈ 520 ms), `openai.gpt-oss-120b` (≈ 790 ms), `zai.glm-5` (≈ 1.3 s). The `bedrock-runtime` endpoint (`/openai/v1` and Converse) answers `400 Operation not allowed` for every model in this account; `anthropic.*` models are not served on mantle `/v1/chat/completions`; `amazon.nova-*` rejects string message content; `openai.gpt-5.6-luna` is listed but `401 not available for this account`. Mantle `GET /v1/models` lists no embedding models. Re-verify all of this at task time (`GET /v1/models` and a stream + forced-tool request per configured model) and record the results in the spec Decision.
  - Deliverables:
    - specs/10 §1 Decision: Bedrock mantle is a supported platform provider through the existing OpenAI-compatible path (no new wire format). Recommended role mapping, overridable by env: dialogue and router `zai.glm-4.7-flash` (alternative `qwen.qwen3-next-80b-a3b-instruct`), analysis `zai.glm-5`, fallback `moonshotai.kimi-k2.5` (a different vendor from dialogue). Document the endpoint and model caveats above.
    - Short-term key auth: new setting `LLM_DEFAULT_AUTH` = `static` (default; today's behaviour, `LLM_DEFAULT_API_KEY`) | `bedrock` (the gateway mints Bedrock short-term API keys from the workload's AWS credentials with `aws-bedrock-token-generator`, region from new `LLM_BEDROCK_REGION`, refreshed well before the 12 h expiry and once on a 401, single-flight across concurrent requests). Tokens never appear in logs, spans, metrics, or error messages (log redactor test). Long-term Bedrock API keys stay possible through `static` but are not the documented production path.
    - Embeddings provider split: `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_AUTH` defaulting to the `LLM_DEFAULT_*` values, so chat can move to Bedrock while embeddings stay on a provider that serves them. Titan Embed v2 (1024 dims, Bedrock InvokeModel) is a documented option in the Decision, not built here.
    - Cost table: built-in per-1k-token COGS entries (micro-dollars) for every model in the recommended mapping, taken from the Amazon Bedrock pricing page for us-east-2 with source URL and `last_verified` date in the spec; `LLM_MODEL_COSTS_JSON` still overrides.
    - specs/02 §6 table rows and `.env.example` for the new variables in the same commit (`scripts/tests/test_env_example.py`); sim defaults unchanged, FakeLLM accepts the Bedrock model ids through the existing `LLM_*_MODEL` variables.
    - IAM: the Terraform/IRSA role for api, worker, and media-worker gets the Bedrock permissions mantle short-term keys need (verify the exact actions, e.g. `bedrock:CallWithBearerToken` plus inference actions, against current AWS docs; scope to the configured model ids and us-east-2). `LLM_DEFAULT_BAA_COVERED` stays `false` unless an AWS BAA is in place (note it in the Decision).
    - Live smoke: `tests_live` gains a Bedrock variant (stream with usage, forced tool call, structured output through `call_structured`) per configured model, gated by `LIVE_TESTS=1` plus AWS credentials or `LIVE_LLM_API_KEY`, with the base URL from `LIVE_LLM_BASE_URL`; update `LIVE_TEST_COUNT` in `test_live_gate.py`.
  - Acceptance: `make check` passes; unit tests cover token minting, proactive refresh, 401-triggered refresh, single-flight refresh under concurrency, and redaction; the live Bedrock smoke passes when credentials exist (evidence in the plan tick) or skips with the reason; specs/10 Decision lists the re-verified model results.

- [ ] P20-T05 CI/CD workflows
  - Spec refs: specs/29-infrastructure.md (CI/CD GitHub Actions)
  - Depends on: P20-T02, P20-T03
  - Deliverables: `e2e.yml`, `release.yml` (build/push to ECR, helm upgrade with approvals), `live-smoke.yml` (nightly, secrets), updated `ci.yml`
  - Acceptance: actionlint passes.

- [ ] P20-T06 Load test
  - Spec refs: specs/30-testing-and-evals.md (load test harness and pass criteria); specs/00-overview.md §5
  - Depends on: P20-T02
  - Deliverables: `scripts/load/` (k6 API scenarios; asyncio simulator swarm driving 1 000 concurrent calls through FakeTelnyx/FakeElevenLabs/FakeLLM in real-time mode across multiple media-worker containers), `LOADTEST_REPORT.md` generated with latency distributions and resource usage; sim multi-worker mode `SIM_WORKERS` (shared Telnyx resources/calls or call-affine routing; deferred from P03-T15c, specs/30 §3 Decision P03-T15c)
  - Acceptance: thresholds in specs/00 §5 met.

- [ ] P20-T07 Live validation with real vendors
  - Spec refs: specs/30-testing-and-evals.md (live smoke tests, voice quality evals); specs/06-telephony-telnyx.md (bootstrap script)
  - Depends on: P20-T02, P20-T04a
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
