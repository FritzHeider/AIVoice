# Phase 01 — Data Layer: Models, Migrations, RLS, Repositories, ClickHouse

> Prerequisites inserted by the Planner (2026-10-25): phase 00 was left incomplete (only P00-T01 ticked), so P01-T01
> (depends on P00-T05, P00-T08) and P01-T12 (depends on P00-T08) are blocked, and the `placeholders` gate in
> `make check` has no script yet. The tasks below execute the named phase-00 tasks exactly as specified in
> `plan/phase-00.md` (same deliverables and acceptance). Completing one ticks it here AND ticks the phase-00 task.

- [x] P01-T00a Prerequisite: execute P00-T05 (`va_common` core: settings, clock, ids, json, errors) — satisfied by P00-T05 (ticked in plan/phase-00.md; va_common settings/time/ids/json/errors present)
  - Spec refs: specs/02-repo-layout-and-conventions.md (Python conventions); specs/31-error-catalog.md (all)
  - Depends on: P00-T01
  - Deliverables and acceptance: as P00-T05 in plan/phase-00.md

- [x] P01-T00b Prerequisite: execute P00-T08 (`va_common` infrastructure helpers) — satisfied by P00-T08 (ticked in plan/phase-00.md; va_common redis/http/tracing/metrics/observability present)
  - Spec refs: specs/02-repo-layout-and-conventions.md (Toolchain versions); specs/27-observability.md
  - Depends on: P01-T00a
  - Deliverables and acceptance: as P00-T08 in plan/phase-00.md

- [x] P01-T00c Prerequisite: execute P00-T03 (placeholder detector gate, required by `make check`) — satisfied by P00-T03 (ticked in plan/phase-00.md; scripts/check_no_placeholders.py + make placeholders)
  - Spec refs: specs/02-repo-layout-and-conventions.md (Quality gates)
  - Depends on: P00-T01
  - Deliverables and acceptance: as P00-T03 in plan/phase-00.md

- [x] P01-T01 Engine, sessions, base model — va_db engine (role via asyncpg server_settings), tenant_session/system_session, Base + mixins; 15 pgvector/pg16 integration tests incl. role-survives-rollback regressions; make test 1154 passed, va_db 100 % coverage
  - Spec refs: specs/03-data-model.md (Conventions, Extensions roles helper functions, Triggers and RLS)
  - Depends on: P00-T05, P00-T08
  - Deliverables: `va_db.engine.create_engine(settings, role)`, `tenant_session(org_id)` (sets `app.org_id` with `set_config(..., true)` inside a transaction), `system_session()`, `Base`, `IdMixin`, `TimestampMixin`, `SoftDeleteMixin`
  - Acceptance: integration test with testcontainers Postgres (`pgvector/pgvector:pg16`) verifies `current_setting('app.org_id')` inside and outside sessions.

- [x] P01-T02 Tenancy and identity models — 10 §3 tables column-for-column (types, defaults, CHECKs, FKs, named indexes); unit + 43 integration tests passed (not skipped); make test 1252 passed, va_db 100 %
  - Spec refs: specs/03-data-model.md (Tenancy, identity, access)
  - Depends on: P01-T01
  - Deliverables: models `Org`, `User`, `Membership`, `Invitation`, `Session`, `EmailToken`, `ApiKey`, `SsoConnection`, `AuditLog`, `IdempotencyKey`
  - Acceptance: model unit tests for defaults and CHECK-constrained enums (Python `Literal` types mirror constraints).

- [x] P01-T03 Telephony models — 6 models + 5 Literals match specs/03 §4; make test 1289 passed (99.84 %, va_db 100 %), va_db integration 55 passed / 0 skipped, lint/typecheck/placeholders clean
  - Spec refs: specs/03-data-model.md (Telephony: carrier accounts, numbers, pools)
  - Depends on: P01-T02
  - Deliverables: `TelnyxAccount`, `PhoneNumber`, `NumberOrder`, `NumberPool`, `NumberPoolMember`, `TelnyxEvent`
  - Acceptance: partial unique index on active E.164 verified by integration test.

- [x] P01-T04 Agent configuration models — 24 §5 tables in library/pathways/agents.py + _columns builders; org_idx Decision in specs/03 §1; make test 1387 passed (99.85 %, va_db 100 %), va_db integration 87 passed / 0 skipped, lint/typecheck/placeholders clean, jscpd 0 clones, radon A
  - Spec refs: specs/03-data-model.md (Agent configuration resources)
  - Depends on: P01-T02
  - Deliverables: `Prompt`, `Voice`, `VoiceSample`, `VoiceRating`, `Tool`, `KnowledgeBase`, `KbDocument`, `KbChunk` (Vector(1024), computed tsvector), `PathwayFolder`, `Pathway`, `PathwayVersion`, `PathwayUnitTest`, `PathwayTestRun`, `Persona`, `PersonaVersion`, `Agent`, `AgentVersion`, `Experiment`, `ExperimentArm`, `Disposition`, `CitationSchema`, `EvalSetup`, `EvalSetupVersion`, `Widget`
  - Acceptance: HNSW and GIN indexes exist after migration (verified in P01-T07 tests).

- [ ] P01-T05 Call models
  - Spec refs: specs/03-data-model.md (Calls)
  - Depends on: P01-T03, P01-T04
  - Deliverables: `Contact`, `Batch`, `BatchItem`, `Call`, `CallLeg`, `CallTurn`, `CallEvent` (partitioned; model maps to parent table), `CallAnalysis`, `Citation`, `EvalRun`, `GuardRailEvent`, `DncEntry`
  - Acceptance: inserting a `CallEvent` lands in the correct monthly partition (integration test).

- [ ] P01-T06 Memory, messaging, webhook, billing models
  - Spec refs: specs/03-data-model.md (Memory, Messaging and widget conversations, Webhooks and billing, Additional tables §9a)
  - Depends on: P01-T05
  - Deliverables: `Memory`, `MemoryChange`, `Conversation`, `Message`, `TenDlcBrand`, `TenDlcCampaign`, `WebhookEndpoint`, `WebhookDelivery`, `Plan`, `OrgSubscription`, `CreditLedger`, `CreditHold`, `UsageEvent`, `Invoice`, `StripeEvent`, plus the §9a tables (added by the Planner 2026-10-27; no other task maps them and 0001/parity need them): `OrgLlmProvider`, `DncImport`, `ErasureRequest`, `OrgSecret`, `OrgDataKey`
  - Acceptance: model import graph has no cycles; `mypy --strict` clean.

- [ ] P01-T07 Alembic initial migration (exact DDL)
  - Spec refs: specs/03-data-model.md (all SQL blocks, Triggers and RLS)
  - Depends on: P01-T06
  - Deliverables: `packages/va_db/alembic.ini`, `migrations/env.py` (async), `versions/0001_initial.py` (extensions, roles if absent, `set_updated_at`, all tables/indexes, per-table triggers via loop, RLS enable/force + policies, platform_read policies, call_events partitions for current month + 3), full `downgrade()`
  - Acceptance: upgrade → downgrade → upgrade on fresh DB passes; `make migrate` works against compose Postgres.

- [ ] P01-T08 Schema conformance and autogenerate parity tests
  - Spec refs: specs/03-data-model.md (header paragraph: conformance test)
  - Depends on: P01-T07
  - Deliverables: `packages/va_db/tests/test_schema_matches_spec.py` (extracts ```sql blocks from the spec, applies to scratch DB, diffs `information_schema.columns`, indexes, constraints against migrated DB), `test_autogenerate_parity.py`
  - Acceptance: both tests pass; deliberately altering a model column makes parity test fail (verified by a parametrized negative test using a temporary metadata copy).

- [ ] P01-T09 RLS isolation test harness
  - Spec refs: specs/03-data-model.md (Triggers and RLS); specs/00-overview.md (Non-negotiable rule 7)
  - Depends on: P01-T07
  - Deliverables: factory fixtures (`OrgFactory` and per-model minimal valid row builders), parametrized test over every table with `org_id`
  - Acceptance: for every tenant table, cross-org SELECT returns 0 rows and cross-org INSERT/UPDATE raise; platform rows of `voices`/`dnc_entries` readable by all orgs.

- [ ] P01-T10 Repository layer and pagination
  - Spec refs: specs/31-error-catalog.md (Pagination); specs/03-data-model.md (Conventions: soft delete; Extensions roles helper functions: system-scope repositories)
  - Depends on: P01-T07
  - Deliverables: `TenantRepository[T]`, cursor encode/decode (base64 of `created_at|id`), soft-delete filtering, `NotFound` → `AppError('NOT_FOUND')`, system repos: `PhoneNumberRoutingRepo`, `OutboxRepo`, `SchedulerRepo`, `RetentionRepo`, `BillingReconcileRepo`, `TelnyxEventRepo`
  - Acceptance: pagination stable across inserts (tests insert between pages); limit bounds enforced (1–100).

- [ ] P01-T11 Plans seed migration
  - Spec refs: specs/24-billing-and-limits.md (plans seed data)
  - Depends on: P01-T07
  - Deliverables: `versions/0002_seed_plans.py` inserting the plan catalogue with exact micro-dollar values; downgrade deletes them
  - Acceptance: test compares seeded rows with the spec table parsed from specs/24.

- [ ] P01-T12 ClickHouse schema applier
  - Spec refs: specs/03-data-model.md (ClickHouse schema); specs/27-observability.md (analytics)
  - Depends on: P00-T08
  - Deliverables: `packages/va_events/src/va_events/clickhouse/migrations/0001_init.sql`, `apply_migrations(client)` (idempotent, records applied versions in `va.schema_migrations`), wired into `events_sink` startup
  - Acceptance: testcontainers ClickHouse test applies twice without error and tables exist with expected columns.

- [ ] P01-T13 Partition maintenance helper
  - Spec refs: specs/03-data-model.md (Calls: call_events partition comment); specs/28-security.md (RetentionWorkflow)
  - Depends on: P01-T07
  - Deliverables: `va_db.partitions.ensure_call_event_partitions(conn, now, months_ahead=3)`, `drop_call_event_partitions_before(conn, cutoff)`
  - Acceptance: tests with frozen clock create/drop the right partitions idempotently.

## Phase exit gate
Fresh `make migrate` succeeds; downgrade/upgrade cycle succeeds; `make check` passes.
