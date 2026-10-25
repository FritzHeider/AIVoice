## Phase 01 — Data Layer: Models, Migrations, RLS, Repositories, ClickHouse

### Goal
Implement the complete relational schema from `specs/03-data-model.md` as SQLAlchemy 2.0 typed models plus an Alembic
initial migration that reproduces the spec DDL exactly (extensions, roles, triggers, RLS policies, partitions), a
tenant-safe session layer, a generic repository layer with cursor pagination, plan seed data, and the ClickHouse
analytics schema applier.

### Read first
- `specs/03-data-model.md` — entire file; it is the contract for this phase.
- `specs/02-repo-layout-and-conventions.md` §4 (Python conventions), §8 (Definition of Done).
- `specs/24-billing-and-limits.md` — plan catalogue values for the seed migration.
- `specs/31-error-catalog.md` §2 (pagination).

### In scope
- `packages/va_db`: engine factory (asyncpg, pool sizing from settings), `tenant_session(org_id)`,
  `system_session()` (connects as `va_system`), declarative base with `id`/timestamps mixins, every model in §3–§9,
  `alembic.ini` + `migrations/` with `0001_initial` and `0002_seed_plans`, repository base classes
  (`TenantRepository[T]` with get/list/create/update/soft_delete, cursor pagination; `SystemRepository` marker) and
  the system repositories named in specs/03 §2.
- Monthly partition management function for `call_events` (create current + next 3 months in the migration; helper
  `ensure_call_event_partitions(now)` used later by RetentionWorkflow).
- Schema conformance test that parses the SQL blocks in `specs/03-data-model.md` and diffs against the migrated DB.
- RLS isolation test that, for every tenant table, inserts rows for two orgs and proves `tenant_session(A)` can neither
  read nor write org B rows.
- `packages/va_events/src/va_events/clickhouse/` migrations SQL + async applier used by `events-sink` at startup.

### Out of scope
API endpoints (later phases), business logic beyond generic repositories.

### Phase-specific constraints
- Models must use `Mapped[...]`/`mapped_column` with exact column types (`vector(1024)` via `pgvector.sqlalchemy.Vector`,
  `INET`, `CITEXT`, `TSVECTOR` computed column, `ARRAY(Text)`, `JSONB`, `NUMERIC(10,3)`).
- The migration is hand-written SQL (`op.execute`) mirroring the spec, not autogenerate output, so that RLS, roles,
  triggers, partitions, and partial indexes are exact. A second test runs Alembic autogenerate comparison against the
  models and asserts no differences (excluding RLS/trigger objects).
- Database roles are created only if absent and without passwords; deployment sets passwords.

### Phase exit gate
`make migrate` on a fresh database succeeds; downgrade to base and upgrade again succeeds; `make check` passes
including schema-conformance, autogenerate-parity, and RLS isolation tests.
