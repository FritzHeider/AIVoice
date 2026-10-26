# 29 — Infrastructure, Deployment, CI/CD, Scaling, DR

## 1. Container images (`infra/docker/`)

All Python services share one multi-stage Dockerfile template parameterized by `SERVICE` (app package directory) and
`MODULE` (entry command). Base images pinned by digest in `infra/docker/base-images.lock` (a text file mapping tag →
digest, refreshed by Renovate); Dockerfiles reference `ARG` values loaded from it by `scripts/docker_build.sh`.

### 1.1 `infra/docker/python.Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1.10
ARG PYTHON_IMAGE=python:3.12.7-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv
WORKDIR /src
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY apps/ apps/
ARG SERVICE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package "va-${SERVICE//_/-}" --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 app && useradd -u 10001 -g app -s /usr/sbin/nologin -M app
COPY --from=builder /src/.venv /app/.venv
COPY models/ /app/models/
COPY assets/ /app/assets/
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VA_MODELS_DIR=/app/models VA_ASSETS_DIR=/app/assets
WORKDIR /app
USER 10001:10001
ARG MODULE
ENV VA_MODULE=${MODULE}
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python -m ${VA_MODULE}"]
```

The builder stage uses bash (`SHELL`) so `${SERVICE//_/-}` converts `media_worker` to the distribution name
`va-media-worker`. Images:

| Image | SERVICE | MODULE | Extra |
|---|---|---|---|
| `va-api` | `api` | `va_api.serve` (wraps uvicorn with uvloop/httptools, `--proxy-headers`, workers=1) | widget bundle copied to `/app/static/widget` from the widget build stage |
| `va-edge` | `telephony_edge` | `va_edge.serve` | |
| `va-media` | `media_worker` | `va_media.main` | Deno binary copied from `denoland/deno:bin-2.0.6` stage to `/usr/local/bin/deno`; ONNX models |
| `va-worker` | `worker` | `va_worker.main` | Deno binary; `ffmpeg` installed (recording redaction/transcoding) |
| `va-webhooks` | `webhook_dispatcher` | `va_webhooks.main` | |
| `va-events-sink` | `events_sink` | `va_events_sink.main` | |
| `va-sim` | `sim` | `va_sim_server.main` | not deployed to production |
| `va-migrate` | `api` | `va_db.migrate` (runs Alembic upgrade + role/grant setup) | Helm pre-upgrade hook |

### 1.2 `infra/docker/dashboard.Dockerfile`

Stage 1 `node:22.11.0-bookworm-slim` with pnpm 9 (corepack) → `pnpm install --frozen-lockfile --filter dashboard...`
→ `pnpm --filter dashboard build` (Next.js `output: "standalone"`). Stage 2 `gcr.io/distroless/nodejs22-debian12:nonroot`
copies `.next/standalone`, `.next/static`, `public`; `CMD ["server.js"]`; port 3000; user 65532.

### 1.3 `infra/docker/docs.Dockerfile`

Build with `python:3.12.7-slim-bookworm` + `mkdocs-material==9.5.44` → static output served by
`nginxinc/nginx-unprivileged:1.27-alpine` (port 8080). Production serves docs from S3+CloudFront instead; image used for
previews.

## 2. docker-compose (`docker-compose.yml`)

Profiles: `infra` (default dependencies), `apps` (all services), `e2e` (apps + sim with accelerated clock),
`observability` (otel-collector, prometheus, grafana, jaeger).

```yaml
name: voiceagents
x-py-env: &py-env
  env_file: [.env]
  environment:
    DATABASE_URL: postgresql+asyncpg://va:va@postgres:5432/va
    REDIS_URL: redis://redis:6379/0
    CLICKHOUSE_URL: http://default:@clickhouse:8123/va
    TEMPORAL_HOST: temporal:7233
    S3_ENDPOINT_URL: http://minio:9000
    TELNYX_API_BASE: http://sim:9100/v2
    ELEVENLABS_API_BASE: http://sim:9200
    ELEVENLABS_WS_BASE: ws://sim:9200
    LLM_DEFAULT_BASE_URL: http://sim:9300/v1
    STRIPE_API_BASE: http://sim:9400
    SMTP_URL: smtp://mailpit:1025
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    EDGE_PUBLIC_HOST: edge:8100
    MEDIA_PUBLIC_HOST: media:8200
services:
  postgres:
    image: pgvector/pgvector:pg16
    profiles: [infra, apps, e2e]
    environment: {POSTGRES_USER: va, POSTGRES_PASSWORD: va, POSTGRES_DB: va}
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data, ./infra/docker/postgres-init:/docker-entrypoint-initdb.d:ro]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U va"], interval: 5s, timeout: 3s, retries: 20}
  redis:
    image: redis:7.2-alpine
    profiles: [infra, apps, e2e]
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    ports: ["6379:6379"]
    healthcheck: {test: ["CMD", "redis-cli", "ping"], interval: 5s, timeout: 3s, retries: 20}
  clickhouse:
    image: clickhouse/clickhouse-server:24.8
    profiles: [infra, apps, e2e]
    environment: {CLICKHOUSE_DB: va, CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: "1"}
    ports: ["8123:8123", "9009:9000"]
    ulimits: {nofile: {soft: 262144, hard: 262144}}
    healthcheck: {test: ["CMD", "wget", "-qO-", "http://localhost:8123/ping"], interval: 5s, timeout: 3s, retries: 30}
  temporal:
    image: temporalio/auto-setup:1.25
    profiles: [infra, apps, e2e]
    depends_on: {postgres: {condition: service_healthy}}
    environment: {DB: postgres12, DB_PORT: 5432, POSTGRES_USER: va, POSTGRES_PWD: va, POSTGRES_SEEDS: postgres, DEFAULT_NAMESPACE: default}
    ports: ["7233:7233"]
    healthcheck: {test: ["CMD", "tctl", "--address", "temporal:7233", "cluster", "health"], interval: 10s, timeout: 5s, retries: 30}
  temporal-ui:
    image: temporalio/ui:2.31.2
    profiles: [infra, apps]
    environment: {TEMPORAL_ADDRESS: temporal:7233}
    ports: ["8233:8080"]
  minio:
    image: bitnamilegacy/minio:2024.10.13
    profiles: [infra, apps, e2e]
    environment: {MINIO_ROOT_USER: minio, MINIO_ROOT_PASSWORD: minio12345}
    ports: ["9000:9000", "9001:9001"]
    volumes: [miniodata:/bitnami/minio/data]
    healthcheck: {test: ["CMD", "mc", "ready", "local"], interval: 5s, timeout: 3s, retries: 20}
  minio-init:
    image: bitnamilegacy/minio-client:2024.10.8
    profiles: [infra, apps, e2e]
    depends_on: {minio: {condition: service_healthy}}
    entrypoint: ["sh", "-c", "mc alias set local http://minio:9000 minio minio12345 && mc mb -p local/va-recordings local/va-kb local/va-voice-samples"]
  mailpit:
    image: axllent/mailpit:v1.20
    profiles: [infra, apps, e2e]
    ports: ["1025:1025", "8025:8025"]
  sim:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: sim, MODULE: va_sim_server.main}}
    profiles: [infra, apps, e2e]
    environment: {SIM_CLOCK: realtime, SIM_EDGE_WEBHOOK_URL: http://edge:8100/telnyx/voice, SIM_MESSAGING_WEBHOOK_URL: http://edge:8100/telnyx/messaging, SIM_STRIPE_WEBHOOK_URL: http://api:8000/v1/billing/stripe/webhook}
    ports: ["9100:9100", "9200:9200", "9300:9300", "9400:9400"]
    healthcheck: {test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:9100/healthz')"], interval: 5s, retries: 20}
  migrate:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: api, MODULE: va_db.migrate}}
    profiles: [apps, e2e]
    <<: *py-env
    depends_on: {postgres: {condition: service_healthy}}
  api:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: api, MODULE: va_api.serve}}
    profiles: [apps, e2e]
    <<: *py-env
    ports: ["8000:8000"]
    depends_on: {migrate: {condition: service_completed_successfully}, redis: {condition: service_healthy}, temporal: {condition: service_healthy}}
    healthcheck: {test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/readyz')"], interval: 5s, retries: 30}
  edge:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: telephony_edge, MODULE: va_edge.serve}}
    profiles: [apps, e2e]
    <<: *py-env
    ports: ["8100:8100"]
    depends_on: {migrate: {condition: service_completed_successfully}}
  media:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: media_worker, MODULE: va_media.main}}
    profiles: [apps, e2e]
    <<: *py-env
    ports: ["8200:8200"]
    stop_grace_period: 60s
    depends_on: {migrate: {condition: service_completed_successfully}}
  worker:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: worker, MODULE: va_worker.main}}
    profiles: [apps, e2e]
    <<: *py-env
    depends_on: {migrate: {condition: service_completed_successfully}, temporal: {condition: service_healthy}}
  webhooks:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: webhook_dispatcher, MODULE: va_webhooks.main}}
    profiles: [apps, e2e]
    <<: *py-env
    environment: {WEBHOOK_ALLOW_PRIVATE: "true"}
    depends_on: {migrate: {condition: service_completed_successfully}}
  events-sink:
    build: {context: ., dockerfile: infra/docker/python.Dockerfile, args: {SERVICE: events_sink, MODULE: va_events_sink.main}}
    profiles: [apps, e2e]
    <<: *py-env
    depends_on: {clickhouse: {condition: service_healthy}, redis: {condition: service_healthy}}
  dashboard:
    build: {context: ., dockerfile: infra/docker/dashboard.Dockerfile}
    profiles: [apps]
    environment: {NEXT_PUBLIC_API_BASE_URL: http://localhost:8000, NEXT_PUBLIC_WIDGET_WS_HOST: localhost:8200}
    ports: ["3000:3000"]
    depends_on: {api: {condition: service_healthy}}
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.112.0
    profiles: [observability]
    volumes: [./infra/otel/collector.yaml:/etc/otelcol-contrib/config.yaml:ro]
    ports: ["4317:4317", "4318:4318"]
  jaeger:
    image: jaegertracing/all-in-one:1.62.0
    profiles: [observability]
    ports: ["16686:16686"]
  prometheus:
    image: prom/prometheus:v2.55.0
    profiles: [observability]
    volumes: [./infra/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro, ./infra/grafana/alerts.yaml:/etc/prometheus/alerts.yaml:ro]
    ports: ["9090:9090"]
  grafana:
    image: grafana/grafana:11.3.0
    profiles: [observability]
    volumes: [./infra/grafana/provisioning:/etc/grafana/provisioning:ro, ./infra/grafana/dashboards:/var/lib/grafana/dashboards:ro]
    ports: ["3001:3000"]
volumes: {pgdata: {}, miniodata: {}}
```

The `e2e` profile overrides `sim` with `SIM_CLOCK: accelerated` via `docker-compose.e2e.yml`. `infra/docker/postgres-init/01-roles.sql`
creates roles `va_app` and `va_system` (password `va` locally) and database `temporal`.

> Decision (2026-10-25): P00-T09 implements the infra and observability services of this block; the `sim` service,
> the `x-py-env` anchor, and every `build:` service arrive with their own tasks. Deviations from the block above:
> 1. MinIO's own images are no longer published (`minio/minio` and `minio/mc` are gone from Docker Hub and Quay
>    returns 401), so MinIO runs from the frozen Bitnami builds of the same releases: `bitnamilegacy/minio:2024.10.13`
>    (reports `RELEASE.2024-10-13T13-34-11Z`, commit `d10bb7e1`) and `bitnamilegacy/minio-client:2024.10.8`. The
>    Bitnami image starts the server itself (API 9000, console 9001), so `command` is dropped and data lives in
>    `/bitnami/minio/data`. `mc mb -p` is already idempotent (`-p` is `--ignore-existing`).
> 2. `temporalio/ui:2.31` does not exist as a tag; the pin is `temporalio/ui:2.31.2`.
> 3. `otel-collector` also runs in `infra`, `apps`, and `e2e`, because every service exports OTLP to `:4317`
>    (`OTEL_EXPORTER_OTLP_ENDPOINT`). It publishes 4317/4318 (OTLP), 13133 (`health_check` extension, the readiness
>    probe `scripts/dev_up.sh` polls; the contrib image has no shell, so there is no container healthcheck), and 8889
>    (Prometheus exporter). `infra/otel/collector.yaml` sends traces to Jaeger (`otlp/jaeger`) and exposes metrics on
>    8889; without the observability profile trace export only retries and logs warnings.
> 4. Healthchecks are added to mailpit (`/mailpit readyz`), temporal-ui, jaeger (admin port 14269), prometheus
>    (`/-/ready`), and grafana (`/api/health`); temporal gets `start_period: 30s`. Long-running services use
>    `restart: unless-stopped`; `minio-init` uses `restart: "no"`.
> 5. Extra named volumes keep state across restarts: `redisdata` (AOF), `chdata`, `prometheusdata`, `grafanadata`.
>    Grafana gets local-only admin `admin`/`admin` and anonymous Viewer access, and a provisioned Prometheus
>    datasource (`infra/grafana/provisioning/datasources/prometheus.yaml`). Prometheus scrapes the collector and the
>    host-run api/edge/media services through `host.docker.internal` (`extra_hosts: host-gateway`).
> 6. The alert-rules mount (`./infra/grafana/alerts.yaml`) and the dashboards mount (`./infra/grafana/dashboards`) are
>    added with the dashboards and alert rules in phase 19; mounting a missing file makes Docker create a directory.
> 7. `make up` runs `scripts/dev_up.sh` with `PROFILE ?= infra` (comma-separated lists allowed, e.g.
>    `PROFILE=infra,observability`). `docker compose up --wait` treats an exited one-shot container as a failure, so the
>    script waits on the long-running services and then runs `minio-init` with `docker compose run --rm`. `make down`
>    runs `scripts/dev_down.sh` (`docker compose --profile '*' down --remove-orphans`; `-v` also removes volumes).
> 8. `01-roles.sql` is idempotent; `temporalio/auto-setup:1.25` accepts the pre-created `temporal` database (verified).

## 3. Kubernetes Helm chart (`infra/helm/voiceagents`)

### 3.1 values.yaml (key structure, all keys)

```yaml
global:
  imageRegistry: ""            # e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com
  imageTag: ""                 # set by release pipeline (git sha)
  env: production
  region: us-east-1
  publicApiHost: api.voiceagents.ai
  publicEdgeHost: edge.voiceagents.ai
  publicMediaHost: media.voiceagents.ai
  dashboardHost: app.voiceagents.ai
  externalSecretStore: aws-secrets-manager
  serviceAccountRoleArns: {api: "", worker: "", media: "", edge: "", webhooks: "", eventsSink: ""}
api:        {replicas: 3, resources: {requests: {cpu: "500m", memory: "768Mi"}, limits: {memory: "1536Mi"}}, hpa: {min: 3, max: 30, cpuTarget: 65}}
edge:       {replicas: 2, resources: {requests: {cpu: "250m", memory: "512Mi"}, limits: {memory: "1Gi"}}, hpa: {min: 2, max: 20, cpuTarget: 65}}
media:
  replicas: 4
  resources: {requests: {cpu: "2", memory: "4Gi"}, limits: {memory: "6Gi"}}
  maxCallsPerPod: 60
  hpa: {min: 4, max: 400, activeCallsTarget: 40}   # target average va_media_active_calls per pod
  drainTimeoutSeconds: 2700
  terminationGracePeriodSeconds: 2760
  nodeSelector: {va/pool: media}
worker:     {replicas: 3, resources: {requests: {cpu: "1", memory: "2Gi"}, limits: {memory: "4Gi"}}, hpa: {min: 3, max: 60, cpuTarget: 70}}
webhooks:   {replicas: 2, resources: {requests: {cpu: "250m", memory: "512Mi"}}, hpa: {min: 2, max: 20, backlogTarget: 500}}
eventsSink: {replicas: 2, resources: {requests: {cpu: "250m", memory: "512Mi"}}}
dashboard:  {replicas: 2, resources: {requests: {cpu: "250m", memory: "512Mi"}}}
egressProxy: {enabled: true, replicas: 2}
migrations: {enabled: true}
temporal:   {mode: cloud, address: "", namespace: "", mtlsSecret: ""}     # or mode: helm (installs temporal subchart)
clickhouse: {mode: cloud, url: ""}                                           # or mode: operator (Altinity CHI resource)
monitoring: {serviceMonitors: true, grafanaDashboards: true, prometheusRules: true}
networkPolicies: {enabled: true}
```

### 3.2 Resources rendered

- Deployments for api, edge, media, worker, webhooks, events-sink, dashboard, egress-proxy (Squid 6 with ACL file from
  `28` §6.4); each with `securityContext` (runAsNonRoot, readOnlyRootFilesystem true with `emptyDir` `/tmp`,
  drop ALL capabilities, seccomp RuntimeDefault), liveness `/healthz`, readiness `/readyz`, `topologySpreadConstraints`
  across zones, `podAntiAffinity` preferred per node.
- **media** specifics: `terminationGracePeriodSeconds: 2760`; `preStop` exec `["python","-m","va_media.drain","--wait"]`
  (marks draining, waits until active calls = 0 or drain timeout); readiness fails when draining; HPA `autoscaling/v2`
  on Pods metric `va_media_active_calls` (via prometheus-adapter rule `va_media_active_calls` averaged per pod) with
  `behavior.scaleDown.stabilizationWindowSeconds: 600`, `policies: [{type: Pods, value: 2, periodSeconds: 120}]` and
  scale up `{type: Percent, value: 100, periodSeconds: 60}`; PDB `maxUnavailable: 10%`; no session affinity needed
  (ownership via Redis, `01` §3).
- Services ClusterIP for all; **Ingress** (AWS Load Balancer Controller) ALB for api/edge/dashboard with
  `alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=3600` (SSE/WS), target type ip;
  **media** exposed via a separate `Service type: LoadBalancer` NLB (TLS listener 443 → 8200, cross-zone enabled,
  `service.beta.kubernetes.io/aws-load-balancer-type: external`, `nlb-target-type: ip`, deregistration delay 2 760 s,
  connection termination disabled, listener attribute `tcp.idle_timeout.seconds=3600` set via
  `service.beta.kubernetes.io/aws-load-balancer-listener-attributes.TLS-443: tcp.idle_timeout.seconds=3600`) so
  long-lived Telnyx WebSockets are not cut. **Rule: every hop carrying WebSockets or SSE (ALB, NLB, egress, any local
  tunnel or reverse proxy) must have an idle timeout ≥ 3 600 s.** No sticky routing is needed for `MEDIA_PUBLIC_HOST`:
  any media-worker pod can accept any call's stream because ownership is claimed via Redis (`01` §3).
- PDBs: api/edge/worker/webhooks `minAvailable: 1` (or 50 % when replicas ≥ 4).
- ExternalSecrets for each service mapping Secrets Manager keys to env vars (`02` §6 and `28` §2).
- ConfigMap `va-config` with non-secret env.
- Helm hook Job `va-migrate` (pre-install, pre-upgrade, weight -5, backoffLimit 1).
- NetworkPolicies: default deny ingress; allow ALB/NLB → api/edge/dashboard/media ports; allow api/edge/media/worker →
  Postgres/Redis/ClickHouse/Temporal endpoints (egress by CIDR of data subnets); allow all pods → egress-proxy:3128;
  allow media/worker/edge/api → vendor CIDRs via NAT (egress to 0.0.0.0/0 port 443 restricted to pods with label
  `va/vendor-egress: "true"`).
- ServiceMonitors for each service `/metrics`; PrometheusRule from `infra/grafana/alerts.yaml`; Grafana dashboard
  ConfigMaps.
- Temporal schedules registered by `worker` at startup (RetentionWorkflow daily, BillingReconcileWorkflow hourly,
  fraud_scan every 5 min, monthly number fees, partition maintenance).

## 4. Terraform (AWS) (`infra/terraform/`)

```
infra/terraform/
├── modules/
│   ├── vpc/            # 3 AZs, public/private/data subnets, NAT per AZ, VPC endpoints (S3, ECR api/dkr, Secrets Manager, KMS, STS, CloudWatch logs)
│   ├── eks/            # EKS 1.31, managed node groups: system (m7i.large ×3), api (m7i.xlarge 3–30), media (c7i.2xlarge 4–200, taint va/pool=media:NoSchedule), worker (m7i.xlarge 3–60); IRSA; add-ons: vpc-cni, coredns, kube-proxy, ebs-csi, aws-load-balancer-controller, external-secrets, cluster-autoscaler (or Karpenter flag), metrics-server, prometheus-adapter, kube-prometheus-stack
│   ├── rds/            # Aurora PostgreSQL 16 (pgvector supported) writer + 1 reader, db.r7g.xlarge, storage encrypted with KMS, IAM auth disabled, parameter group (shared_preload_libraries=pg_stat_statements, max_connections=2000), PITR 14 days, deletion protection
│   ├── elasticache/    # Redis 7.1 replication group, cache.r7g.large, 1 primary + 1 replica per AZ (cluster mode disabled), TLS + AUTH token, multi-AZ failover
│   ├── s3/             # buckets recordings, kb, voice-samples, docs, backups — SSE-KMS bucket keys, versioning, public access block, lifecycle (recordings → Glacier IR after 90 d, expire 3650 d), access logging bucket
│   ├── kms/            # keys: app-envelope (KEK), rds, s3, elasticache, secrets; rotation enabled
│   ├── ecr/            # repositories per image, immutable tags, scan on push, lifecycle keep last 100
│   ├── secrets/        # Secrets Manager entries (values set out-of-band)
│   ├── dns-acm/        # Route53 zone records (api, edge, media, app, docs), ACM certs (DNS validation)
│   ├── cloudfront-docs/# S3 origin + OAC for docs and widget CDN (widget.voiceagents.ai)
│   ├── clickhouse/     # variable clickhouse_mode = "cloud" (outputs URL from variable) | "eks" (installs Altinity operator via helm_release + CHI with 2 shards × 2 replicas on gp3)
│   ├── temporal/       # variable temporal_mode = "cloud" (namespace + mTLS cert secrets) | "eks" (helm_release temporal chart with Aurora persistence schema `temporal`)
│   ├── waf/            # WAFv2 on ALB: AWS managed common rules, known bad inputs, IP reputation, rate rule 2000 req/5 min/IP for /internal/auth/*
│   └── iam-github-oidc/# GitHub OIDC provider + deploy role scoped to repo/environment
└── envs/
    ├── staging/  (main.tf, variables.tf, terraform.tfvars, backend.tf → S3 state bucket + DynamoDB lock)
    ├── production-us/
    └── production-eu/  # eu-west-1 data residency stack (separate RDS/S3/Redis/EKS; orgs with data_region=eu are created in this stack; global DNS routes by API host `api.eu.voiceagents.ai`)
```

Required tags on all resources: `project=voiceagents`, `env`, `owner=platform`, `data_classification`.
`terraform fmt -check`, `tflint`, `checkov` in CI.

## 5. Environments

| Env | Purpose | Vendors | Data |
|---|---|---|---|
| local | dev laptop via compose | simulators (optionally real vendors with tunnel: `cloudflared tunnel --url http://localhost:8100` and `:8200`, setting `EDGE_PUBLIC_HOST`/`MEDIA_PUBLIC_HOST`) | seeded |
| test | pytest/CI | in-process simulators + testcontainers | ephemeral |
| staging | pre-prod, nightly live smoke | real Telnyx (separate account/sub-account), ElevenLabs, Telnyx Inference, Stripe test mode | synthetic |
| production-us / production-eu | customers | real | real |

## 6. CI/CD (GitHub Actions)

### 6.1 `ci.yml` (on pull_request and push to main)

Jobs (parallel where independent):
1. `python-lint-type`: setup uv, `uv sync --all-packages --frozen`, `make lint typecheck placeholders`,
   `scripts/check_openapi_drift.py`, `scripts/check_spec_refs.py`.
2. `python-test`: services via testcontainers (Docker available on ubuntu-latest), `uv run pytest -n auto --cov
   --cov-fail-under=85 --junitxml=…`, upload coverage.
3. `ts`: pnpm install, `pnpm -r lint typecheck test`, dashboard `gen:api` drift check, `next build` + bundle budget check,
   widget build size check (≤ 60 KB gzip).
4. `security`: gitleaks, pip-audit, pnpm audit, semgrep, trivy config, checkov, tflint.
5. `docs`: `mkdocs build --strict`.
6. `helm`: `helm lint`, `helm template` + `kubeconform -strict`.
7. `docker-build` (needs 1–3): build all images with buildx cache (no push on PR), trivy image scan.

> Decision (2026-10-25): P00-T14 ships the phase-00 subset of these jobs. `check` merges jobs 1-2 by running
> `make check` (lint, typecheck, placeholders, pytest with coverage, `test-ts`, OpenAPI drift, spec refs) with GitHub
> service containers for Postgres, Redis, ClickHouse, and MinIO whose images, credentials, and healthchecks equal
> docker-compose.yml (service containers cannot mount the init SQL, so a step applies
> `infra/docker/postgres-init/01-roles.sql` with psql; Docker stays available for testcontainers). `ts-build` covers
> job 3's builds (`pnpm -r run build`: `next build`, the widget 60 KB gzip gate, SDK tsup) plus the `gen:api` drift
> diff; lint/typecheck/test for TS already run in `check`. `security` runs gitleaks over the full history (checksum-verified
> release binary rather than `gitleaks-action`, which needs a licence for organisations), pip-audit on
> `uv export --frozen --all-packages --no-emit-workspace`, and `pnpm audit --audit-level high`; semgrep, trivy,
> checkov, and tflint join when Dockerfiles and Terraform exist. A fourth job runs actionlint. `docs`, `helm`, and
> `docker-build` arrive with the phases that create mkdocs, the chart, and the images. Actions are pinned to their
> current major tags. Pre-commit (`.pre-commit-config.yaml`) uses local `language: system` hooks run through `uv run` /
> `corepack pnpm exec` so hook versions are the locked versions, plus the remote gitleaks hook pinned to the same
> version as CI; `.gitleaks.toml` extends the default rules with path- and match-scoped false-positive allowlists.

### 6.2 `e2e.yml` (on pull_request labeled `e2e`, on push to main, nightly)

`docker compose --profile e2e up -d --build --wait`, `make e2e` (pytest e2e suite, `30` §5), `make e2e-ui`
(Playwright), upload artifacts (recordings of agent audio from simulator, Playwright traces), `docker compose down -v`.

### 6.3 `release.yml` (on push of tag `v*` and on main after e2e success for staging)

1. Build and push images to ECR (OIDC role), tag `${{ github.sha }}` and semver tag; cosign sign; syft SBOM attach.
2. Deploy staging: `helm upgrade --install voiceagents infra/helm/voiceagents -f infra/helm/values-staging.yaml
   --set global.imageTag=$SHA --atomic --timeout 20m`; smoke tests (`scripts/smoke.sh`: health, create pathway, simulated
   call via staging sim tenant).
3. Production (tag `v*` only, GitHub environment `production` with required reviewers): same helm upgrade for us then eu;
   media pods roll gradually (maxUnavailable 10 %, maxSurge 25 %) relying on drain; post-deploy smoke; automatic
   `helm rollback` on smoke failure.
4. SDK/CLI/docs publish jobs keyed on their own tags (`26` §7); docs sync to S3 + CloudFront invalidation.

### 6.4 `live-smoke.yml` (nightly 09:00 UTC, staging secrets)

Runs `LIVE_TESTS=1 uv run pytest tests_live -m live` against real vendors (`30` §7); posts results to Slack webhook
secret `SLACK_ALERTS_WEBHOOK`; failures open a GitHub issue via `gh issue create`.

## 7. Scaling math and regional deployment

- Media worker: target 40 active calls/pod (2 vCPU, 4 GiB) leaving headroom to 60. Per call CPU budget ≈ 25 ms/s
  (μ-law decode, VAD ONNX on 32 ms windows, JSON/base64, websockets) → 40 calls ≈ 1 core; memory ≈ 40 MiB/call. For
  10 000 concurrent calls → 250 pods → 63 c7i.2xlarge nodes (4 pods each).
- API: ~1 500 req/s per 10 pods of 0.5–1 vCPU; send-call path dominated by Postgres writes (~5 queries).
- Postgres write load: per call ≈ 40 rows (turns/events batched) → 10 000 concurrent calls with 3-minute average ≈ 55
  calls/s ending → ~2 200 row inserts/s batched — within Aurora r7g.xlarge capacity; `call_events` partitioning keeps
  indexes small.
- Redis: pub/sub and streams ~ 50 msgs/s per call during speech → 500 k msgs/s at 10 000 calls requires Redis cluster
  mode; switch `REDIS_URL` to cluster endpoint when concurrency > 3 000 (supported by `redis.asyncio.RedisCluster`;
  keys use hash tags `call:{<id>}:…` so per-call keys colocate — the keyspace in `03` §11 is rendered with braces
  around the call id for this reason).
- Regions: us-east-1 primary (closest to Telnyx US core and ElevenLabs US), us-west-2 optional media-only region
  later; eu-west-1 for EU residency with ElevenLabs EU host `wss://api.eu.residency.elevenlabs.io` and Telnyx
  `sip_region: Europe`.

## 8. Backup, restore, and disaster recovery

- Aurora: automated backups with PITR 14 days; daily snapshot copied cross-region (us-west-2 / eu-central-1);
  monthly restore drill (`infra/runbooks/db-restore.md`) restores to a scratch cluster and runs
  `scripts/verify_restore.py` (row counts, latest call timestamps, RLS policies present).
- S3: versioning + cross-region replication for recordings and voice samples.
- Redis: treated as ephemeral (rebuildable); daily snapshot retained 3 days.
- ClickHouse: rebuildable from Postgres `call_turns`/`call_events`/`usage_events` via `scripts/backfill_clickhouse.py`;
  ClickHouse Cloud backups daily.
- Temporal: Temporal Cloud multi-AZ; self-hosted uses Aurora schema covered by DB backups.
- Targets: RPO ≤ 5 min (Aurora), RTO ≤ 2 h for full region loss (`infra/runbooks/dr-region-failover.md`: promote
  cross-region snapshot, apply Terraform `envs/production-us-dr`, update Route53, update Telnyx Call Control App webhook
  URLs and messaging profile URLs via `scripts/telnyx_bootstrap.py --update-urls`).
- In-flight calls during regional failure are lost; post-call runs from partial data when the region returns.

## 9. Cost estimates (monthly, us-east-1, on-demand list prices, excluding vendors)

| Item | Baseline (≤ 500 concurrent) | 5 000 concurrent |
|---|---|---|
| EKS control plane | $73 | $73 |
| Nodes: system 3× m7i.large | $220 | $220 |
| api 3–10× m7i.xlarge | $440 | $1 470 |
| media 2–32× c7i.2xlarge | $520 | $8 300 |
| worker 3–12× m7i.xlarge | $440 | $1 760 |
| Aurora r7g.xlarge writer + reader + storage/IO | $1 100 | $2 600 |
| ElastiCache r7g.large ×2 (cluster at scale ×6) | $330 | $1 000 |
| ClickHouse Cloud (dev → production tier) | $200 | $1 500 |
| Temporal Cloud | $200 | $1 200 |
| NAT gateways ×3 + data | $250 | $1 200 |
| ALB + NLB | $60 | $250 |
| S3 + CloudFront | $50 | $600 |
| Observability (managed Prometheus/Grafana or self-hosted storage) | $150 | $700 |
| **Total infra** | **≈ $4 000** | **≈ $20 900** |

Vendor COGS per minute (Telnyx voice + streaming, ElevenLabs STT/TTS, Telnyx Inference LLM) are tracked by
`usage_events.cost_micros` (`24`), not here.

## 10. Runbooks (files under `infra/runbooks/`, each with symptoms, diagnosis commands, remediation, verification)

`incident-response.md`, `secret-rotation.md`, `db-restore.md`, `dr-region-failover.md`, `media-worker-drain.md`,
`vendor-outage-elevenlabs.md`, `vendor-outage-telnyx.md`, `llm-degradation.md`, `webhook-backlog.md`,
`redis-failover.md`, `scale-up-for-campaign.md`, `fraud-suspension.md`.

## 11. Tests required

Dockerfiles build in CI; `hadolint` passes; compose `--wait` health for all services in e2e job; `helm lint` and
`kubeconform`; a chart unit test (helm-unittest plugin) asserting media `terminationGracePeriodSeconds`, preStop hook,
HPA metric name, NLB annotations, NetworkPolicy default deny; `terraform validate` per env; restore verification script
unit tests.

## 12. Acceptance criteria

- `make up --profile apps` brings every service healthy locally; e2e workflow green.
- Staging deploy via `release.yml` succeeds with smoke tests; rolling media deploy drops zero simulated calls (load test
  during deploy, `30` §6).
- Terraform plans apply cleanly in a sandbox account; checkov/tflint clean.
- DR restore drill documented and executed once successfully.
