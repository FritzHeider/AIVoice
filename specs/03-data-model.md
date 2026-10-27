# 03 — Data Model

This file is normative. SQLAlchemy models in `packages/va_db/src/va_db/models/` must produce exactly this schema;
the first Alembic migration (`0001_initial`) creates it, and a test (`packages/va_db/tests/test_schema_matches_spec.py`)
compares `information_schema` against the column lists parsed from the SQL blocks in this file (the test reads
this markdown file, extracts every ```sql block, and applies it to a scratch database, then diffs both schemas).

## 1. Conventions

- Primary keys: `id uuid PRIMARY KEY` generated in Python as UUIDv7. No database sequences for ids.
- Every tenant-owned table has `org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE` and an index that
  starts with `org_id`.
- Timestamps: `created_at timestamptz NOT NULL DEFAULT now()`, `updated_at timestamptz NOT NULL DEFAULT now()`
  maintained by trigger `set_updated_at()`.
- Soft delete: `deleted_at timestamptz` on user-facing configuration resources (pathways, tools, KBs, voices,
  personas, agents, widgets, phone_numbers, prompts). Queries exclude soft-deleted rows by default.
- Money: `BIGINT` micro-dollars (`*_micros`).
- Enumerations: Postgres `text` with `CHECK` constraints (easier migrations than native enums).
- JSON: `jsonb`, validated in Python by Pydantic models named in comments.
- Row-Level Security: enabled on every table with `org_id` (§6).

> Decision (2026-10-27): the `org_id` index rule above is binding for every tenant table. §5 originally left 13
> tables without one (voice_samples, voice_ratings, kb_documents, kb_chunks, pathway_unit_tests, pathway_test_runs,
> persona_versions, agent_versions, experiments, experiment_arms, citation_schemas, eval_setups,
> eval_setup_versions); each now has `CREATE INDEX <table>_org_idx ON <table>(org_id)` because the RLS predicate
> `org_id = app.org_id` and the org-scoped list endpoints filter on it. A UNIQUE constraint that starts with
> `org_id` (tools, dispositions) satisfies the rule.
>
> §6 likewise: contacts (whose only `org_id`-leading indexes are partial uniques, which cannot serve a full-org
> scan), batch_items, call_legs, call_turns, call_events, call_analyses, citations, eval_runs, guard_rail_events, and
> dnc_entries (nullable `org_id`, like voices) each get `CREATE INDEX <table>_org_idx ON <table>(org_id)`. On the
> partitioned call_events table it is a partitioned index that every monthly partition inherits.

## 2. Extensions, roles, helper functions

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
```

Roles (created by the migration when absent; passwords come from the deployment, not the migration):

- `va_app` — used by `api`, `media-worker`, request-scoped code. RLS enforced.
- `va_system` — `BYPASSRLS`; used only by repositories explicitly marked `system_scope=True`
  (`PhoneNumberRoutingRepo`, `OutboxRepo`, `SchedulerRepo`, `RetentionRepo`, `BillingReconcileRepo`,
  `TelnyxEventRepo`, admin endpoints). Code review rule: any new use requires a comment citing this spec section.

> Decision (2026-10-27): deployments provide one `DATABASE_URL` (specs/02 §6), so `va_db.engine.create_engine(settings,
> role)` logs in with it and sends `role=va_app` / `role=va_system` as a connection startup parameter (asyncpg
> `server_settings`, `DbRole.APP`, `DbRole.SYSTEM`); `role=None` keeps the login role and is used only by migrations
> and test setup. A startup parameter is the session default for `role`, so no transaction rollback, pool
> reset-on-return, or `RESET ROLE` can revert a pooled connection to the login role (a `SET ROLE` issued after
> connecting would be undone if the connection's first transaction rolled back). As `va_app`, RLS applies even when
> the login role is a superuser or the table owner, because `current_user` is `va_app` and every tenant table has
> `FORCE ROW LEVEL SECURITY`. Consequently the `0001_initial`
> migration must `GRANT va_app, va_system TO CURRENT_USER` (Postgres 16 requires explicit membership with the `SET`
> option for a non-superuser login) and grant table privileges to both roles. `va_system` is created with
> `BYPASSRLS`; neither role gets a password from the migration.

## 3. Tenancy, identity, access

```sql
CREATE TABLE orgs (
  id                      uuid PRIMARY KEY,
  parent_org_id           uuid REFERENCES orgs(id) ON DELETE RESTRICT,
  name                    text NOT NULL,
  slug                    citext NOT NULL UNIQUE,
  plan_id                 text NOT NULL DEFAULT 'start',
  kyc_level               smallint NOT NULL DEFAULT 0 CHECK (kyc_level BETWEEN 0 AND 3),
  status                  text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','closed')),
  credits_balance_micros  bigint NOT NULL DEFAULT 0,
  credit_limit_micros     bigint NOT NULL DEFAULT 0,          -- how far negative the balance may go
  auto_recharge_enabled   boolean NOT NULL DEFAULT false,
  auto_recharge_threshold_micros bigint NOT NULL DEFAULT 5000000,
  auto_recharge_amount_micros    bigint NOT NULL DEFAULT 25000000,
  stripe_customer_id      text UNIQUE,
  limits                  jsonb NOT NULL DEFAULT '{}'::jsonb,  -- OrgLimitsOverride: daily_calls, hourly_calls, concurrency, international_enabled, sms_per_day
  settings                jsonb NOT NULL DEFAULT '{}'::jsonb,  -- OrgSettings (see 04-auth §7)
  data_region             text NOT NULL DEFAULT 'us' CHECK (data_region IN ('us','eu')),
  retention_days          integer NOT NULL DEFAULT 365 CHECK (retention_days BETWEEN 1 AND 3650),
  recording_retention_days integer NOT NULL DEFAULT 90 CHECK (recording_retention_days BETWEEN 0 AND 3650),
  webhook_signing_secret_enc bytea NOT NULL,                   -- envelope-encrypted
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orgs_parent_idx ON orgs(parent_org_id);

CREATE TABLE users (
  id                uuid PRIMARY KEY,
  email             citext NOT NULL UNIQUE,
  name              text NOT NULL DEFAULT '',
  password_hash     text,                                   -- argon2id; NULL for SSO-only users
  email_verified_at timestamptz,
  mfa_totp_secret_enc bytea,
  mfa_enabled       boolean NOT NULL DEFAULT false,
  mfa_recovery_codes_hash text[] NOT NULL DEFAULT '{}',
  is_platform_admin boolean NOT NULL DEFAULT false,
  last_login_at     timestamptz,
  status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  id         uuid PRIMARY KEY,
  org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('owner','admin','developer','analyst','billing','viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, user_id)
);
CREATE INDEX memberships_user_idx ON memberships(user_id);

CREATE TABLE invitations (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  email       citext NOT NULL,
  role        text NOT NULL CHECK (role IN ('owner','admin','developer','analyst','billing','viewer')),
  token_hash  text NOT NULL UNIQUE,
  invited_by  uuid NOT NULL REFERENCES users(id),
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX invitations_org_idx ON invitations(org_id);

CREATE TABLE sessions (
  id           uuid PRIMARY KEY,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   text NOT NULL UNIQUE,                 -- sha256 of the opaque cookie value
  active_org_id uuid REFERENCES orgs(id) ON DELETE SET NULL,
  ip           inet,
  user_agent   text,
  mfa_passed   boolean NOT NULL DEFAULT false,
  expires_at   timestamptz NOT NULL,
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_idx ON sessions(user_id);

CREATE TABLE email_tokens (
  id         uuid PRIMARY KEY,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose    text NOT NULL CHECK (purpose IN ('verify_email','reset_password')),
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  used_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name         text NOT NULL,
  prefix       text NOT NULL UNIQUE,                 -- first 12 chars shown in UI, e.g. "va_live_3kQ9"
  secret_hash  text NOT NULL,                        -- sha256(pepper || secret)
  scopes       text[] NOT NULL DEFAULT '{*}',
  pepper_version smallint NOT NULL DEFAULT 1,
  environment  text NOT NULL DEFAULT 'live' CHECK (environment IN ('live','test')),
  created_by   uuid REFERENCES users(id) ON DELETE SET NULL,
  last_used_at timestamptz,
  expires_at   timestamptz,
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX api_keys_org_idx ON api_keys(org_id);

CREATE TABLE sso_connections (
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  provider       text NOT NULL CHECK (provider IN ('oidc')),
  issuer         text NOT NULL,
  client_id      text NOT NULL,
  client_secret_enc bytea NOT NULL,
  allowed_domains text[] NOT NULL DEFAULT '{}',
  default_role   text NOT NULL DEFAULT 'viewer',
  enforce        boolean NOT NULL DEFAULT false,      -- password login disabled for org members when true
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, issuer)
);

CREATE TABLE audit_logs (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  actor_type  text NOT NULL CHECK (actor_type IN ('user','api_key','system')),
  actor_id    uuid,
  action      text NOT NULL,                          -- e.g. "pathway.publish", "api_key.create"
  resource_type text NOT NULL,
  resource_id text,
  ip          inet,
  user_agent  text,
  changes     jsonb NOT NULL DEFAULT '{}'::jsonb,     -- {"before":{...},"after":{...}} with secrets redacted
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_logs_org_created_idx ON audit_logs(org_id, created_at DESC);

CREATE TABLE idempotency_keys (
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  key           text NOT NULL,
  method        text NOT NULL,
  path          text NOT NULL,
  request_hash  text NOT NULL,
  response_status integer,
  response_body jsonb,
  locked_until  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, key)
);
```

## 4. Telephony: carrier accounts, numbers, pools

```sql
CREATE TABLE telnyx_accounts (                         -- "Bring Your Own Telnyx"; platform account is not stored here
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  label              text NOT NULL,
  api_key_enc        bytea NOT NULL,
  public_key         text NOT NULL,                     -- Ed25519 public key for their webhooks
  call_control_app_id text,                             -- created in their account by our bootstrap activity
  messaging_profile_id text,
  outbound_voice_profile_id text,
  encrypted_key_handle text NOT NULL UNIQUE,            -- opaque value returned to customers as `encrypted_key`
  status             text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','error','deleted')),
  status_detail      text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX telnyx_accounts_org_idx ON telnyx_accounts(org_id);

CREATE TABLE phone_numbers (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  e164               text NOT NULL,
  telnyx_account_id  uuid REFERENCES telnyx_accounts(id) ON DELETE SET NULL,   -- NULL = platform account
  telnyx_phone_number_id text,
  country_code       text NOT NULL,
  number_type        text NOT NULL DEFAULT 'local' CHECK (number_type IN ('local','toll_free','mobile','national')),
  label              text,
  capabilities       text[] NOT NULL DEFAULT '{voice}',     -- voice, sms, mms
  inbound_config     jsonb NOT NULL DEFAULT '{}'::jsonb,    -- InboundConfig (16-phone-numbers §4) mirrors Send Call fields
  sms_config         jsonb NOT NULL DEFAULT '{}'::jsonb,    -- SmsNumberConfig (21-messaging §3)
  monthly_cost_micros bigint NOT NULL DEFAULT 0,
  status             text NOT NULL DEFAULT 'active' CHECK (status IN ('pending','active','releasing','released')),
  purchased_at       timestamptz,
  released_at        timestamptz,
  deleted_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX phone_numbers_e164_active_uq ON phone_numbers(e164) WHERE status IN ('pending','active');
CREATE INDEX phone_numbers_org_idx ON phone_numbers(org_id);

CREATE TABLE number_orders (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  telnyx_order_id  text,
  requested_numbers text[] NOT NULL,
  status           text NOT NULL CHECK (status IN ('pending','success','failure')),
  error            text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX number_orders_org_idx ON number_orders(org_id);

CREATE TABLE number_pools (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name        text NOT NULL,
  strategy    text NOT NULL DEFAULT 'round_robin' CHECK (strategy IN ('round_robin','least_recently_used','random','local_match')),
  max_calls_per_number_per_day integer NOT NULL DEFAULT 100,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);

CREATE TABLE number_pool_members (
  pool_id         uuid NOT NULL REFERENCES number_pools(id) ON DELETE CASCADE,
  phone_number_id uuid NOT NULL REFERENCES phone_numbers(id) ON DELETE CASCADE,
  org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  last_used_at    timestamptz,
  calls_today     integer NOT NULL DEFAULT 0,
  calls_today_date date,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (pool_id, phone_number_id)
);
CREATE INDEX number_pool_members_org_idx ON number_pool_members(org_id);

CREATE TABLE telnyx_events (                          -- raw inbound Telnyx webhooks (system scope)
  id               uuid PRIMARY KEY,
  telnyx_event_id  text NOT NULL UNIQUE,              -- data.id
  event_type       text NOT NULL,
  org_id           uuid REFERENCES orgs(id) ON DELETE CASCADE,
  call_id          uuid,
  call_control_id  text,
  occurred_at      timestamptz NOT NULL,
  payload          jsonb NOT NULL,
  processed_at     timestamptz,
  process_error    text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX telnyx_events_call_idx ON telnyx_events(call_id);
CREATE INDEX telnyx_events_ccid_idx ON telnyx_events(call_control_id);
```

## 5. Agent configuration resources

```sql
CREATE TABLE prompts (
  id          uuid PRIMARY KEY,                       -- public "PT-<uuid>"
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name        text NOT NULL,
  content     text NOT NULL,
  tags        text[] NOT NULL DEFAULT '{}',
  deleted_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX prompts_org_idx ON prompts(org_id);

CREATE TABLE voices (
  id               uuid PRIMARY KEY,
  org_id           uuid REFERENCES orgs(id) ON DELETE CASCADE,   -- NULL = platform/public catalog voice
  name             text NOT NULL,
  description      text NOT NULL DEFAULT '',
  provider         text NOT NULL DEFAULT 'elevenlabs' CHECK (provider IN ('elevenlabs')),
  provider_voice_id text NOT NULL,
  service          text NOT NULL DEFAULT 'ELEVEN_FLASH' CHECK (service IN ('ELEVEN_FLASH','ELEVEN_TURBO','ELEVEN_MULTILINGUAL')),
  category         text NOT NULL DEFAULT 'premade' CHECK (category IN ('premade','cloned','generated','professional','shared')),
  is_public        boolean NOT NULL DEFAULT false,
  tags             text[] NOT NULL DEFAULT '{}',
  language_codes   text[] NOT NULL DEFAULT '{en}',
  gender           text,
  accent           text,
  age              text,
  preview_url      text,
  settings         jsonb NOT NULL DEFAULT '{"stability":0.5,"similarity_boost":0.75,"style":0,"use_speaker_boost":true,"speed":1.0}'::jsonb,
  rating_sum       integer NOT NULL DEFAULT 0,
  rating_count     integer NOT NULL DEFAULT 0,
  clone_status     text CHECK (clone_status IN ('processing','ready','failed')),
  consent          jsonb,                                -- clone consent record {attested_by_user_id, attested_at, statement_version, ip}
  clone_error      text,
  deleted_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voices_org_idx ON voices(org_id);
CREATE UNIQUE INDEX voices_name_scope_uq ON voices(coalesce(org_id,'00000000-0000-0000-0000-000000000000'::uuid), lower(name)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX voices_provider_uq ON voices(coalesce(org_id,'00000000-0000-0000-0000-000000000000'::uuid), provider_voice_id) WHERE deleted_at IS NULL;

CREATE TABLE voice_samples (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  voice_id    uuid NOT NULL REFERENCES voices(id) ON DELETE CASCADE,
  s3_key      text NOT NULL,
  mime_type   text NOT NULL,
  duration_ms integer NOT NULL,
  transcript  text,
  provider_sample_id text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voice_samples_voice_idx ON voice_samples(voice_id);
CREATE INDEX voice_samples_org_idx ON voice_samples(org_id);

CREATE TABLE voice_ratings (
  voice_id   uuid NOT NULL REFERENCES voices(id) ON DELETE CASCADE,
  org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating     smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (voice_id, user_id)
);
CREATE INDEX voice_ratings_org_idx ON voice_ratings(org_id);

CREATE TABLE tools (
  id            uuid PRIMARY KEY,                     -- public "TL-<uuid>"
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name          text NOT NULL CHECK (name ~ '^[A-Za-z][A-Za-z0-9_]{0,63}$'),
  description   text NOT NULL,
  speech        text,                                  -- said while the tool runs
  url           text NOT NULL,
  method        text NOT NULL DEFAULT 'POST' CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
  headers       jsonb NOT NULL DEFAULT '{}'::jsonb,    -- values may contain {{vars}}; secret values encrypted (13-tools §3)
  secret_headers_enc bytea,
  query         jsonb NOT NULL DEFAULT '{}'::jsonb,
  body          jsonb,
  input_schema  jsonb NOT NULL DEFAULT '{"type":"object","properties":{}}'::jsonb,
  response_map  jsonb NOT NULL DEFAULT '{}'::jsonb,    -- {"var_name":"$.json.path"}
  timeout_ms    integer NOT NULL DEFAULT 10000 CHECK (timeout_ms BETWEEN 100 AND 60000),
  retries       smallint NOT NULL DEFAULT 0 CHECK (retries BETWEEN 0 AND 3),
  deleted_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);

CREATE TABLE knowledge_bases (
  id           uuid PRIMARY KEY,                      -- public "KB-<uuid>"
  org_id       uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name         text NOT NULL,
  description  text NOT NULL DEFAULT '',
  status       text NOT NULL DEFAULT 'PROCESSING' CHECK (status IN ('PROCESSING','COMPLETED','FAILED')),
  embedding_model text NOT NULL,
  embedding_dimensions integer NOT NULL,
  chunk_size_tokens integer NOT NULL DEFAULT 400,
  chunk_overlap_tokens integer NOT NULL DEFAULT 60,
  document_count integer NOT NULL DEFAULT 0,
  chunk_count  integer NOT NULL DEFAULT 0,
  deleted_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX knowledge_bases_org_idx ON knowledge_bases(org_id);

CREATE TABLE kb_documents (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  kb_id         uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  type          text NOT NULL CHECK (type IN ('FILE','WEB_SCRAPE','TEXT')),
  name          text NOT NULL,
  source_url    text,
  s3_key        text,
  mime_type     text,
  size_bytes    bigint,
  content_sha256 text,
  status        text NOT NULL DEFAULT 'PROCESSING' CHECK (status IN ('PROCESSING','COMPLETED','FAILED')),
  error         text,
  token_count   integer,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX kb_documents_kb_idx ON kb_documents(kb_id);
CREATE INDEX kb_documents_org_idx ON kb_documents(org_id);

CREATE TABLE kb_chunks (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  kb_id       uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
  ordinal     integer NOT NULL,
  content     text NOT NULL,
  token_count integer NOT NULL,
  heading_path text[] NOT NULL DEFAULT '{}',
  source_url  text,
  page_number integer,
  embedding   vector(1024) NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX kb_chunks_kb_idx ON kb_chunks(kb_id);
CREATE INDEX kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX kb_chunks_tsv_idx ON kb_chunks USING gin(tsv);
CREATE INDEX kb_chunks_org_idx ON kb_chunks(org_id);

CREATE TABLE pathway_folders (
  id         uuid PRIMARY KEY,
  org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name       text NOT NULL,
  parent_id  uuid REFERENCES pathway_folders(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pathway_folders_org_idx ON pathway_folders(org_id);

CREATE TABLE pathways (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  folder_id          uuid REFERENCES pathway_folders(id) ON DELETE SET NULL,
  name               text NOT NULL,
  description        text NOT NULL DEFAULT '',
  production_version integer,                         -- version_number pinned to production
  staging_version    integer,
  draft_version      integer NOT NULL DEFAULT 1,      -- the single editable draft
  latest_version     integer NOT NULL DEFAULT 1,
  deleted_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pathways_org_idx ON pathways(org_id);

CREATE TABLE pathway_versions (
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  pathway_id     uuid NOT NULL REFERENCES pathways(id) ON DELETE CASCADE,
  version_number integer NOT NULL,
  name           text,                                 -- optional label ("v3 – new greeting")
  status         text NOT NULL CHECK (status IN ('draft','published','archived')),
  nodes          jsonb NOT NULL DEFAULT '[]'::jsonb,   -- PathwayNode[] (11-pathways-engine §3)
  edges          jsonb NOT NULL DEFAULT '[]'::jsonb,   -- PathwayEdge[]
  global_config  jsonb NOT NULL DEFAULT '{}'::jsonb,   -- GlobalConfig (11-pathways-engine §3): global prompt, memory_enabled, default voice, model options
  graph_hash     text NOT NULL,                        -- sha256 of canonical JSON for change detection
  published_at   timestamptz,
  published_by   uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (pathway_id, version_number)
);
CREATE INDEX pathway_versions_org_idx ON pathway_versions(org_id);

CREATE TABLE pathway_unit_tests (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  pathway_id    uuid NOT NULL REFERENCES pathways(id) ON DELETE CASCADE,
  name          text NOT NULL,
  node_id       text NOT NULL,
  source_call_id uuid,
  history       jsonb NOT NULL,                       -- [{role, content}] conversation before the tested user message
  user_message  text NOT NULL,
  expected      jsonb NOT NULL,                       -- {"route_to_node_id":?, "variables":{...}?, "grading_goal":?}
  simulated_variations integer NOT NULL DEFAULT 5 CHECK (simulated_variations BETWEEN 0 AND 20),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pathway_unit_tests_pathway_idx ON pathway_unit_tests(pathway_id);
CREATE INDEX pathway_unit_tests_org_idx ON pathway_unit_tests(org_id);

CREATE TABLE pathway_test_runs (
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  pathway_id      uuid NOT NULL REFERENCES pathways(id) ON DELETE CASCADE,
  version_number  integer NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('unit','simulation','regression')),
  status          text NOT NULL CHECK (status IN ('queued','running','passed','failed','error')),
  results         jsonb NOT NULL DEFAULT '[]'::jsonb,
  started_at      timestamptz,
  finished_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pathway_test_runs_pathway_idx ON pathway_test_runs(pathway_id, created_at DESC);
CREATE INDEX pathway_test_runs_org_idx ON pathway_test_runs(org_id);

CREATE TABLE personas (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name               text NOT NULL,
  description        text NOT NULL DEFAULT '',
  production_version integer,
  draft_version      integer NOT NULL DEFAULT 1,
  latest_version     integer NOT NULL DEFAULT 1,
  deleted_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX personas_org_idx ON personas(org_id);

CREATE TABLE persona_versions (
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  persona_id     uuid NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  version_number integer NOT NULL,
  status         text NOT NULL CHECK (status IN ('draft','published','archived')),
  config         jsonb NOT NULL,                       -- PersonaConfig (20-memory-personas-agents §3)
  published_at   timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (persona_id, version_number)
);
CREATE INDEX persona_versions_org_idx ON persona_versions(org_id);

CREATE TABLE agents (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  env_pins      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"production":"1.2.0","staging":"1.3.0-rc.1","dev":"latest"}
  deleted_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agents_org_idx ON agents(org_id);

CREATE TABLE agent_versions (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  agent_id      uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  semver        text NOT NULL CHECK (semver ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'),
  branch        text NOT NULL DEFAULT 'main' CHECK (char_length(branch) BETWEEN 1 AND 64),
  pathway_id    uuid REFERENCES pathways(id) ON DELETE RESTRICT,
  pathway_version integer,
  call_settings jsonb NOT NULL DEFAULT '{}'::jsonb,   -- subset of Send Call params used as defaults
  changelog     text NOT NULL DEFAULT '',
  created_by    uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (agent_id, semver)
);
CREATE INDEX agent_versions_agent_branch_idx ON agent_versions(agent_id, branch, created_at DESC);
CREATE INDEX agent_versions_org_idx ON agent_versions(org_id);

CREATE TABLE experiments (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  agent_id     uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  name         text NOT NULL,
  status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','running','stopped')),
  primary_metric text NOT NULL DEFAULT 'disposition_rate',   -- disposition_rate | avg_eval_score | transfer_rate | avg_duration
  primary_metric_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at   timestamptz,
  stopped_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX experiments_one_running_per_agent ON experiments(agent_id) WHERE status = 'running';
CREATE INDEX experiments_org_idx ON experiments(org_id);

CREATE TABLE experiment_arms (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  experiment_id uuid NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  agent_version_id uuid NOT NULL REFERENCES agent_versions(id) ON DELETE RESTRICT,
  weight        integer NOT NULL CHECK (weight BETWEEN 0 AND 100),
  label         text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX experiment_arms_org_idx ON experiment_arms(org_id);

CREATE TABLE dispositions (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  tag         text NOT NULL CHECK (tag ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  description text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, tag)
);

CREATE TABLE citation_schemas (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name        text NOT NULL,
  description text NOT NULL DEFAULT '',
  variables   jsonb NOT NULL,                         -- [{name,type,description,required,conditions:[{operator,value}]}]
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX citation_schemas_org_idx ON citation_schemas(org_id);

CREATE TABLE eval_setups (                            -- "workbench setups"
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name        text NOT NULL,
  description text NOT NULL DEFAULT '',
  latest_version integer NOT NULL DEFAULT 1,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_setups_org_idx ON eval_setups(org_id);

CREATE TABLE eval_setup_versions (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  eval_setup_id uuid NOT NULL REFERENCES eval_setups(id) ON DELETE CASCADE,
  version_number integer NOT NULL,
  criteria      jsonb NOT NULL,                       -- [{id,name,description,type:'pass_fail'|'score_1_5'|'score_0_100',weight,instructions}]
  pass_threshold numeric(5,2) NOT NULL DEFAULT 70,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (eval_setup_id, version_number)
);
CREATE INDEX eval_setup_versions_org_idx ON eval_setup_versions(org_id);

CREATE TABLE widgets (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name               text NOT NULL,
  pathway_id         uuid REFERENCES pathways(id) ON DELETE SET NULL,
  pathway_version    integer,
  agent_id           uuid REFERENCES agents(id) ON DELETE SET NULL,
  persona_id         uuid REFERENCES personas(id) ON DELETE SET NULL,
  agent_prompt       text,
  allowed_domains    text[] NOT NULL DEFAULT '{}',
  messages_per_minute integer NOT NULL DEFAULT 20 CHECK (messages_per_minute BETWEEN 1 AND 600),
  voice_enabled      boolean NOT NULL DEFAULT false,
  voice_id           uuid REFERENCES voices(id) ON DELETE SET NULL,
  config             jsonb NOT NULL DEFAULT '{}'::jsonb,  -- WidgetConfig (22-web-widget §3)
  webhook_url        text,
  public_key         text NOT NULL UNIQUE,               -- "wgt_<random>" embedded in the script tag
  deleted_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX widgets_org_idx ON widgets(org_id);
```

## 6. Calls

```sql
CREATE TABLE contacts (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  phone_e164   text,
  email        citext,
  external_id  text,
  name         text,
  attributes   jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX contacts_phone_uq ON contacts(org_id, phone_e164) WHERE phone_e164 IS NOT NULL;
CREATE UNIQUE INDEX contacts_email_uq ON contacts(org_id, email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX contacts_external_uq ON contacts(org_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX contacts_org_idx ON contacts(org_id);

CREATE TABLE batches (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  label            text,
  status           text NOT NULL DEFAULT 'created' CHECK (status IN ('created','scheduled','validating','dispatching','in_progress','completed','stopped','failed')),
  global_props     jsonb NOT NULL DEFAULT '{}'::jsonb,
  status_webhook   text,
  schedule         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- BatchSchedule (17-batches §3)
  calls_total      integer NOT NULL DEFAULT 0,
  calls_dispatched integer NOT NULL DEFAULT 0,
  calls_completed  integer NOT NULL DEFAULT 0,
  calls_successful integer NOT NULL DEFAULT 0,
  calls_failed     integer NOT NULL DEFAULT 0,
  failure_code     text,
  failure_reason   text,
  started_at       timestamptz,
  completed_at     timestamptz,
  created_by_key   uuid REFERENCES api_keys(id) ON DELETE SET NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX batches_org_created_idx ON batches(org_id, created_at DESC);

CREATE TABLE batch_items (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  batch_id    uuid NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  ordinal     integer NOT NULL,
  payload     jsonb NOT NULL,                         -- per-call Send Call body merged over global_props
  status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','skipped','dispatched','completed','failed','canceled')),
  skip_reason text,
  call_id     uuid,
  attempts    smallint NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (batch_id, ordinal)
);
CREATE INDEX batch_items_dispatch_idx ON batch_items(batch_id, status, next_attempt_at);
CREATE INDEX batch_items_org_idx ON batch_items(org_id);

CREATE TABLE calls (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  external_id        text CHECK (char_length(external_id) <= 256),
  direction          text NOT NULL CHECK (direction IN ('outbound','inbound','web')),
  to_number          text,
  from_number        text,
  contact_id         uuid REFERENCES contacts(id) ON DELETE SET NULL,
  status             text NOT NULL CHECK (status IN ('scheduled','queued','dialing','ringing','in_progress','transferring','transferred','completed','failed','canceled','no_answer','busy')),
  queue_status       text,                               -- Bland compat ("complete", "started", ...)
  answered_by        text CHECK (answered_by IN ('human','voicemail','ivr','unknown','no_answer','fax')),
  runner_type        text NOT NULL CHECK (runner_type IN ('task','pathway','persona','agent')),
  pathway_id         uuid REFERENCES pathways(id) ON DELETE SET NULL,
  pathway_version    integer,
  persona_id         uuid REFERENCES personas(id) ON DELETE SET NULL,
  persona_version    integer,
  agent_id           uuid REFERENCES agents(id) ON DELETE SET NULL,
  agent_version_id   uuid REFERENCES agent_versions(id) ON DELETE SET NULL,
  experiment_id      uuid REFERENCES experiments(id) ON DELETE SET NULL,
  experiment_arm_id  uuid REFERENCES experiment_arms(id) ON DELETE SET NULL,
  test_call          boolean NOT NULL DEFAULT false,      -- dashboard test calls; excluded from analytics/experiments
  batch_id           uuid REFERENCES batches(id) ON DELETE SET NULL,
  batch_item_id      uuid REFERENCES batch_items(id) ON DELETE SET NULL,
  parent_call_id     uuid REFERENCES calls(id) ON DELETE SET NULL,  -- retry-of / warm-transfer parent
  phone_number_id    uuid REFERENCES phone_numbers(id) ON DELETE SET NULL,
  voice_id           uuid REFERENCES voices(id) ON DELETE SET NULL,
  config             jsonb NOT NULL,                     -- fully resolved SendCallConfig after precedence (05-calls-api §4)
  request_data       jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
  variables          jsonb NOT NULL DEFAULT '{}'::jsonb, -- final variable bag at end of call
  start_time         timestamptz,
  dial_started_at    timestamptz,
  answered_at        timestamptz,
  started_at         timestamptz,
  ended_at           timestamptz,
  connected_seconds  numeric(10,3) NOT NULL DEFAULT 0,
  transfer_seconds   numeric(10,3) NOT NULL DEFAULT 0,
  end_reason         text,                               -- see 05-calls-api §9 enumeration
  ended_by           text CHECK (ended_by IN ('agent','user','system','api','guard_rail','timeout','error')),
  transferred_to     text,
  recording_s3_key   text,
  recording_duration_ms integer,
  summary            text,
  disposition_tag    text,
  error_message      text,
  price_micros       bigint NOT NULL DEFAULT 0,
  credit_hold_id     uuid,
  workflow_id        text,
  memory_id          uuid,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX calls_org_created_idx ON calls(org_id, created_at DESC);
CREATE INDEX calls_org_status_idx ON calls(org_id, status);
CREATE INDEX calls_org_external_idx ON calls(org_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX calls_org_to_idx ON calls(org_id, to_number, created_at DESC);
CREATE INDEX calls_batch_idx ON calls(batch_id);
CREATE INDEX calls_pathway_idx ON calls(org_id, pathway_id, created_at DESC);

CREATE TABLE call_legs (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  call_id          uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  role             text NOT NULL CHECK (role IN ('primary','transfer_target','warm_proxy','monitor')),
  telnyx_account_id uuid REFERENCES telnyx_accounts(id) ON DELETE SET NULL,
  call_control_id  text NOT NULL,
  call_leg_id      text,
  call_session_id  text,
  state            text NOT NULL CHECK (state IN ('initiated','ringing','answered','bridged','hangup')),
  hangup_cause     text,
  hangup_source    text,
  sip_hangup_cause text,
  amd_result       text,
  started_at       timestamptz NOT NULL DEFAULT now(),
  answered_at      timestamptz,
  ended_at         timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX call_legs_ccid_uq ON call_legs(call_control_id);
CREATE INDEX call_legs_call_idx ON call_legs(call_id);
CREATE INDEX call_legs_org_idx ON call_legs(org_id);

CREATE TABLE call_turns (
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  call_id         uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  turn_index      integer NOT NULL,
  role            text NOT NULL CHECK (role IN ('user','assistant','agent-action','system','human_agent')),
  text            text NOT NULL,
  text_spoken     text,                                -- assistant: what was actually played before interruption
  interrupted     boolean NOT NULL DEFAULT false,
  start_ms        integer NOT NULL,                    -- relative to call answered_at
  end_ms          integer NOT NULL,
  node_id         text,
  node_name       text,
  language_code   text,
  stt_confidence  real,
  latency         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {eot_ms, stt_final_ms, llm_ttft_ms, tts_ttfa_ms, e2e_ms, route_ms, tool_ms}
  text_realtime   text,                                -- user turns: realtime STT text kept when batch re-transcription replaces `text`
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (call_id, turn_index, role)
);
CREATE INDEX call_turns_call_idx ON call_turns(call_id, turn_index);
CREATE INDEX call_turns_org_idx ON call_turns(org_id);

CREATE TABLE call_events (
  id          uuid NOT NULL,
  org_id      uuid NOT NULL,
  call_id     uuid NOT NULL,
  seq         integer NOT NULL,
  category    text NOT NULL CHECK (category IN ('queue','call','latency','webhook','tool','dynamic_data','route','extraction','guard_rail','transfer','voicemail','error','citations','evals','post_transfer_transcript','sms','memory','interrupt')),
  level       text NOT NULL DEFAULT 'info' CHECK (level IN ('debug','info','warning','error')),
  message     text NOT NULL,
  data        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (created_at, id)
) PARTITION BY RANGE (created_at);
-- Partitions are created monthly by RetentionWorkflow (next 3 months ahead) and dropped after orgs' max retention.
CREATE INDEX call_events_call_idx ON call_events(call_id, seq);
CREATE INDEX call_events_org_idx ON call_events(org_id);

CREATE TABLE call_analyses (
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  call_id        uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  kind           text NOT NULL CHECK (kind IN ('summary','disposition','analysis_schema','sentiment','qa')),
  goal           text,
  questions      jsonb,                               -- [[question, type], ...] (Bland analyze format)
  schema         jsonb,                               -- analysis_schema
  result         jsonb NOT NULL,
  model          text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX call_analyses_call_idx ON call_analyses(call_id);
CREATE INDEX call_analyses_org_idx ON call_analyses(org_id);

CREATE TABLE citations (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  call_id            uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  citation_schema_id uuid NOT NULL REFERENCES citation_schemas(id) ON DELETE CASCADE,
  variable_name      text NOT NULL,
  variable_type      text NOT NULL,
  value              jsonb,
  conditions_met     boolean,
  cited_utterances   jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{turn_index,start_time,end_time,channel,speaker_id,text,topics,utterance_type}]
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX citations_call_idx ON citations(call_id);
CREATE INDEX citations_org_idx ON citations(org_id);

CREATE TABLE eval_runs (
  id                     uuid PRIMARY KEY,
  org_id                 uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  call_id                uuid REFERENCES calls(id) ON DELETE CASCADE,
  eval_setup_version_id  uuid NOT NULL REFERENCES eval_setup_versions(id) ON DELETE CASCADE,
  status                 text NOT NULL CHECK (status IN ('queued','running','completed','failed')),
  overall_score          numeric(5,2),
  passed                 boolean,
  results                jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{criterion_id, score, passed, rationale, cited_turns}]
  error                  text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_runs_call_idx ON eval_runs(call_id);
CREATE INDEX eval_runs_org_idx ON eval_runs(org_id);

CREATE TABLE guard_rail_events (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  call_id     uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  guard_rail  text NOT NULL,                          -- "tcpa:opt_out", "custom:<name>"
  triggered   boolean NOT NULL,
  action      text,
  turn_index  integer,
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX guard_rail_events_call_idx ON guard_rail_events(call_id);
CREATE INDEX guard_rail_events_org_idx ON guard_rail_events(org_id);

CREATE TABLE dnc_entries (
  id         uuid PRIMARY KEY,
  org_id     uuid REFERENCES orgs(id) ON DELETE CASCADE,  -- NULL = platform-wide blacklist
  phone_e164 text NOT NULL,
  source     text NOT NULL CHECK (source IN ('manual','api','opt_out_voice','opt_out_sms','import','platform')),
  reason     text,
  call_id    uuid,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX dnc_scope_phone_uq ON dnc_entries(coalesce(org_id,'00000000-0000-0000-0000-000000000000'::uuid), phone_e164);
CREATE INDEX dnc_entries_org_idx ON dnc_entries(org_id);
```

## 7. Memory

```sql
CREATE TABLE memories (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  contact_id       uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  persona_id       uuid REFERENCES personas(id) ON DELETE CASCADE,  -- NULL = org-wide memory for the contact
  summary          text NOT NULL DEFAULT '',
  facts            jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"key": {"value": ..., "updated_at": ..., "source_call_id": ...}}
  open_items       jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{id, text, created_at, due_at?, status}]
  recent_messages  jsonb NOT NULL DEFAULT '[]'::jsonb,   -- last 30 messages across channels [{channel, role, text, at, call_id|conversation_id}]
  enabled          boolean NOT NULL DEFAULT true,
  summary_embedding vector(1024),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX memories_contact_persona_uq ON memories(org_id, contact_id, coalesce(persona_id,'00000000-0000-0000-0000-000000000000'::uuid));

CREATE TABLE memory_changes (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  memory_id   uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  call_id     uuid,
  conversation_id uuid,
  change_type text NOT NULL CHECK (change_type IN ('summary','fact_set','fact_delete','open_item_add','open_item_close','reset')),
  before      jsonb,
  after       jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_changes_memory_idx ON memory_changes(memory_id, created_at DESC);
```

## 8. Messaging and widget conversations

```sql
CREATE TABLE conversations (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  channel          text NOT NULL CHECK (channel IN ('sms','mms','widget','imessage','rcs')),
  agent_number     text,
  user_number      text,
  widget_id        uuid REFERENCES widgets(id) ON DELETE SET NULL,
  widget_visitor_id text,
  contact_id       uuid REFERENCES contacts(id) ON DELETE SET NULL,
  runner_type      text NOT NULL CHECK (runner_type IN ('objective','pathway','persona','agent')),
  objective        text,
  pathway_id       uuid REFERENCES pathways(id) ON DELETE SET NULL,
  pathway_version  integer,
  persona_id       uuid REFERENCES personas(id) ON DELETE SET NULL,
  agent_id         uuid REFERENCES agents(id) ON DELETE SET NULL,
  current_node_id  text,
  engine_state     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- serialized EngineState (11-pathways-engine §9)
  request_data     jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
  status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active','human_takeover','closed','timed_out')),
  time_out_minutes integer NOT NULL DEFAULT 1440,
  last_message_at  timestamptz,
  webhook          text,
  summary          text,
  disposition_tag  text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_lookup_idx ON conversations(org_id, channel, agent_number, user_number, status);
CREATE INDEX conversations_widget_idx ON conversations(widget_id, widget_visitor_id);

CREATE TABLE messages (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  direction        text NOT NULL CHECK (direction IN ('inbound','outbound')),
  role             text NOT NULL CHECK (role IN ('user','assistant','human_agent','system')),
  body             text NOT NULL,
  media_urls       text[] NOT NULL DEFAULT '{}',
  telnyx_message_id text UNIQUE,
  status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','sent','delivered','sending_failed','delivery_failed','delivery_unconfirmed','received')),
  error            jsonb,
  segments         integer NOT NULL DEFAULT 1,
  price_micros     bigint NOT NULL DEFAULT 0,
  node_id          text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON messages(conversation_id, created_at);

CREATE TABLE ten_dlc_brands (
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  telnyx_brand_id text UNIQUE,
  payload         jsonb NOT NULL,                     -- brand registration fields (21-messaging §6)
  status          text NOT NULL CHECK (status IN ('draft','submitted','verified','unverified','failed')),
  status_detail   text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ten_dlc_campaigns (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  brand_id           uuid NOT NULL REFERENCES ten_dlc_brands(id) ON DELETE CASCADE,
  telnyx_campaign_id text UNIQUE,
  use_case           text NOT NULL,
  payload            jsonb NOT NULL,
  status             text NOT NULL CHECK (status IN ('draft','submitted','active','rejected','suspended')),
  status_detail      text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
```

## 9. Webhooks and billing

```sql
CREATE TABLE webhook_endpoints (                      -- org-level subscriptions (in addition to per-call `webhook`)
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  url          text NOT NULL,
  description  text NOT NULL DEFAULT '',
  event_types  text[] NOT NULL DEFAULT '{*}',
  enabled      boolean NOT NULL DEFAULT true,
  phi_allowed  boolean NOT NULL DEFAULT false,         -- HIPAA mode: transcripts included only when true
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhook_endpoints_org_idx ON webhook_endpoints(org_id);

CREATE TABLE webhook_deliveries (                     -- transactional outbox
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  event_id        text NOT NULL,                       -- "evt_<uuid>"
  event_type      text NOT NULL,
  url             text NOT NULL,
  endpoint_id     uuid REFERENCES webhook_endpoints(id) ON DELETE SET NULL,
  payload         jsonb NOT NULL,
  format          text NOT NULL DEFAULT 'bland' CHECK (format IN ('bland','envelope')),
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivering','succeeded','failed','dead')),
  attempts        smallint NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_status_code integer,
  last_error      text,
  last_response_ms integer,
  call_id         uuid,
  batch_id        uuid,
  conversation_id uuid,
  delivered_at    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (event_id, url)
);
CREATE INDEX webhook_deliveries_due_idx ON webhook_deliveries(next_attempt_at) WHERE status IN ('pending','delivering');
CREATE INDEX webhook_deliveries_org_idx ON webhook_deliveries(org_id, created_at DESC);

CREATE TABLE plans (                                   -- platform catalog (system scope, no org_id)
  id                       text PRIMARY KEY,           -- 'start','build','scale','enterprise'
  name                     text NOT NULL,
  monthly_fee_micros       bigint NOT NULL,
  connected_rate_micros_per_min bigint NOT NULL,
  transfer_rate_micros_per_min  bigint NOT NULL,
  min_attempt_micros       bigint NOT NULL,
  sms_rate_micros          bigint NOT NULL,
  widget_message_micros    bigint NOT NULL,
  tts_rate_micros_per_1k_chars bigint NOT NULL DEFAULT 0,  -- standalone TTS product price
  daily_call_cap           integer,                    -- NULL = unlimited
  hourly_call_cap          integer,
  concurrency_cap          integer,
  voice_clone_cap          integer,
  features                 text[] NOT NULL DEFAULT '{}',
  stripe_price_id          text,
  is_public                boolean NOT NULL DEFAULT true,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE org_subscriptions (
  id                    uuid PRIMARY KEY,
  org_id                uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  plan_id               text NOT NULL REFERENCES plans(id),
  status                text NOT NULL CHECK (status IN ('active','past_due','canceled','trialing')),
  stripe_subscription_id text UNIQUE,
  current_period_start  timestamptz NOT NULL,
  current_period_end    timestamptz NOT NULL,
  cancel_at_period_end  boolean NOT NULL DEFAULT false,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX org_subscriptions_active_uq ON org_subscriptions(org_id) WHERE status IN ('active','past_due','trialing');

CREATE TABLE credit_ledger (                           -- append-only
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  entry_type     text NOT NULL CHECK (entry_type IN ('purchase','auto_recharge','grant','refund','usage','adjustment','subscription_fee','hold','hold_release','transfer_in','transfer_out')),
  amount_micros  bigint NOT NULL,                      -- positive credit, negative debit
  balance_after_micros bigint NOT NULL,
  reference_type text,                                 -- 'call','sms','widget_message','stripe_payment','subscription','manual','sub_account'
  reference_id   text,
  description    text NOT NULL DEFAULT '',
  idempotency_key text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key)
);
CREATE INDEX credit_ledger_org_created_idx ON credit_ledger(org_id, created_at DESC);

CREATE TABLE credit_holds (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  reference_type text NOT NULL,
  reference_id  text NOT NULL,
  amount_micros bigint NOT NULL,
  status        text NOT NULL CHECK (status IN ('held','captured','released')),
  expires_at    timestamptz NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX credit_holds_org_status_idx ON credit_holds(org_id, status);

CREATE TABLE usage_events (                            -- authoritative metering rows (also mirrored to ClickHouse)
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  meter         text NOT NULL CHECK (meter IN ('call_connected_seconds','call_transfer_seconds','call_attempt','sms_segment_out','sms_segment_in','widget_message','tts_characters','kb_embedding_tokens','voice_clone','phone_number_month','llm_tokens_in','llm_tokens_out','stt_seconds')),
  quantity      numeric(18,6) NOT NULL,
  unit_price_micros bigint NOT NULL,                   -- customer price per unit at time of event
  amount_micros bigint NOT NULL,
  cost_micros   bigint NOT NULL DEFAULT 0,             -- our vendor cost estimate (COGS)
  reference_type text NOT NULL,
  reference_id  text NOT NULL,
  idempotency_key text NOT NULL,
  occurred_at   timestamptz NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key)
);
CREATE INDEX usage_events_org_time_idx ON usage_events(org_id, occurred_at DESC);

CREATE TABLE invoices (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  period_start      timestamptz NOT NULL,
  period_end        timestamptz NOT NULL,
  subtotal_micros   bigint NOT NULL,
  credits_applied_micros bigint NOT NULL,
  total_micros      bigint NOT NULL,
  line_items        jsonb NOT NULL,
  status            text NOT NULL CHECK (status IN ('draft','finalized','paid','void')),
  stripe_invoice_id text UNIQUE,
  pdf_s3_key        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX invoices_org_idx ON invoices(org_id, period_start DESC);

CREATE TABLE stripe_events (                           -- system scope, dedupe
  id          text PRIMARY KEY,                        -- Stripe event id
  type        text NOT NULL,
  payload     jsonb NOT NULL,
  processed_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
```


## 9a. Additional tables (merged from module specs)

```sql
CREATE TABLE org_llm_providers (                      -- per-org OpenAI-compatible LLM override (10-llm-gateway.md)
  id                   uuid PRIMARY KEY,
  org_id               uuid NOT NULL UNIQUE REFERENCES orgs(id) ON DELETE CASCADE,
  base_url             text NOT NULL,
  api_key_enc          bytea NOT NULL,
  models               jsonb NOT NULL,                 -- {"dialogue":…, "router":…, "analysis":…, "fallback":…}
  supports_tool_choice boolean NOT NULL DEFAULT true,
  baa_covered          boolean NOT NULL DEFAULT false,
  last_probe_at        timestamptz,
  last_probe_error     text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dnc_imports (                            -- async bulk DNC CSV imports (23-guardrails-and-compliance.md)
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  status      text NOT NULL CHECK (status IN ('queued','running','completed','failed')),
  s3_key      text NOT NULL,
  total       integer,
  inserted    integer,
  invalid     integer,
  duplicates  integer,
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dnc_imports_org_idx ON dnc_imports(org_id, created_at DESC);

CREATE TABLE erasure_requests (                       -- right-to-erasure tracking (28-security.md)
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  selector    jsonb NOT NULL,                         -- {"contact_id"|"phone_e164"|"email"|"external_id": …}
  status      text NOT NULL CHECK (status IN ('queued','running','completed','failed')),
  result      jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX erasure_requests_org_idx ON erasure_requests(org_id, created_at DESC);

CREATE TABLE org_secrets (                            -- named secrets referenced by tools/dynamic data as {{secrets.NAME}} (04 §7)
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name        text NOT NULL CHECK (name ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  value_enc   bytea NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);

CREATE TABLE org_data_keys (                          -- per-org wrapped data keys (28-security.md envelope encryption)
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  kek_id             text NOT NULL,
  encrypted_data_key bytea NOT NULL,
  status             text NOT NULL CHECK (status IN ('active','retired')),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX org_data_keys_active_uq ON org_data_keys(org_id) WHERE status = 'active';
```

## 10. Triggers and RLS

For every table in §3–§9 that has an `updated_at` column, the migration creates:

```sql
-- generated per table by the migration loop; shown here for `calls`
CREATE TRIGGER calls_set_updated_at BEFORE UPDATE ON calls FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

For every table with a NOT NULL `org_id` column (and `voices`, `dnc_entries` with nullable `org_id`):

```sql
ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON calls
  USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
  WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);
-- voices and dnc_entries additionally allow reading platform rows:
CREATE POLICY platform_read ON voices FOR SELECT USING (org_id IS NULL);
CREATE POLICY platform_read ON dnc_entries FOR SELECT USING (org_id IS NULL);
```

`va_db.session.tenant_session(org_id)` opens a transaction and executes `SELECT set_config('app.org_id', :org_id, true)`.
Sub-account access (parent org admins acting on child orgs) switches `app.org_id` to the child after an authorization
check in the API layer; RLS is never relaxed for it.

## 11. Redis keyspace (normative)

Redis Cluster compatibility: in every key below, the segment written `{id}` / `{org_id}` / `{batch_id}` is a literal
Redis hash tag in the implementation — the identifier is wrapped in braces (`call:{0193c5d2-…}:ctx`) so all keys of one
call (or org, or batch) map to the same slot and multi-key Lua scripts work. `va_common.redis.keys` builds every key.

| Key / channel | Type | TTL | Purpose |
|---|---|---|---|
| `call:{id}:ctx` | string (JSON CallContext) | max_duration + 1 h | Prefetched context for media worker |
| `call:{id}:owner` | string pod_id | 15 s (renewed) | Ownership lock |
| `call:{id}:hb` | string ts | 10 s | Worker heartbeat |
| `call:{id}:carrier_state` | hash | 2 h | Latest carrier state (answered, amd_result, bridged…) |
| `call:{id}:control` | pub/sub channel | — | Control messages to owner |
| `call:{id}:carrier` | pub/sub channel | — | Carrier events to owner |
| `call:{id}:live` | pub/sub channel | — | Live events for dashboard/API SSE listeners |
| `events:calls`, `events:sms`, `events:widget`, `events:billing`, `events:system` | streams (MAXLEN ~ 1 000 000) | — | Event bus |
| `rl:key:{api_key_id}:{window}` | token bucket hash | window | Request rate limiting |
| `cap:org:{org_id}:daily:{yyyymmdd}` | counter | 48 h | Daily call cap |
| `cap:org:{org_id}:hourly:{yyyymmddhh}` | counter | 2 h | Hourly call cap |
| `cap:org:{org_id}:concurrency` | sorted set (call_id → expiry ts) | — | Concurrency cap with self-healing expiry |
| `cap:dest:{org_id}:{e164}` | string | 10 s | Same-number 10-second rule |
| `media:pods` | hash pod_id → JSON {active_calls, draining, updated_at} | — | Admission control, dashboards |
| `tts:cache:{voice_id}:{sha256(text+settings)}` | string (base64 μ-law) | 30 d | Pre-rendered phrase cache |
| `dyn:{org_id}:{sha256(request)}` | string JSON | per `cache` setting (default 300 s) | Dynamic data cache |
| `widget:rl:{widget_id}:{visitor}` | token bucket | 60 s | Widget per-visitor rate limit |
| `sess:{token_hash}` | string JSON | session TTL | Session cache (DB is source of truth) |
| `call:{id}:seq` | counter | 48 h | Per-call event sequence (`19` §1) |
| `call:{id}:engine` | string (JSON EngineState) | 2 h | Engine state snapshot for recovery and inspection |
| `call:{id}:kbcache` | hash | 2 h | Per-call KB retrieval cache |
| `call:{id}:secure:{name}` | string (encrypted) | 15 min | Secure DTMF capture values (never sent to the LLM) |
| `kbq:{sha256}` | string (vector) | 1 h | Query embedding cache |
| `pathway_chat:{chat_id}` | string JSON | 24 h | Pathway test chat sessions |
| `pathway:{id}:v:{env}` | string JSON | 10 min | Resolved pathway graph cache (invalidated on publish/promote) |
| `numsearch:{sha256}` | string JSON | 60 s | Number search result cache |
| `pool:{pool_id}:rr` | counter | — | Round-robin cursor for number pools |
| `localpresence:{org_id}` | hash | 1 h | NPA → number index for local presence |
| `batchpace:{batch_id}` | token bucket hash | batch lifetime | Batch calls-per-minute pacing |
| `batchstats:{batch_id}` | string JSON | 30 s | Cached batch counters |
| `freq:{org_id}:{e164}:{yyyymmdd}` | counter | 48 h | Per-destination daily attempt frequency (state rules) |
| `cap:org:{org_id}:intl:{yyyymmddhh}` | counter | 2 h | International hourly cap |
| `smsrl:{e164}` | token bucket hash | 60 s | Per-sender SMS throughput |
| `widget:rl:{widget_id}:_all` | token bucket hash | 60 s | Widget-wide rate limit |
| `widget:thread:{thread_id}` | pub/sub channel | — | Widget thread live messages (human takeover) |

## 12. ClickHouse schema (analytics)

```sql
CREATE TABLE IF NOT EXISTS va.call_events (
  org_id UUID, call_id UUID, seq UInt32, category LowCardinality(String), level LowCardinality(String),
  message String, data String, created_at DateTime64(3, 'UTC')
) ENGINE = MergeTree PARTITION BY toYYYYMM(created_at) ORDER BY (org_id, call_id, seq) TTL toDateTime(created_at) + INTERVAL 400 DAY;

CREATE TABLE IF NOT EXISTS va.call_turns (
  org_id UUID, call_id UUID, turn_index UInt32, role LowCardinality(String), node_id String,
  text_len UInt32, interrupted UInt8, eot_ms Nullable(UInt32), stt_final_ms Nullable(UInt32),
  llm_ttft_ms Nullable(UInt32), tts_ttfa_ms Nullable(UInt32), e2e_ms Nullable(UInt32), route_ms Nullable(UInt32),
  tool_ms Nullable(UInt32), created_at DateTime64(3, 'UTC')
) ENGINE = MergeTree PARTITION BY toYYYYMM(created_at) ORDER BY (org_id, created_at, call_id, turn_index);

CREATE TABLE IF NOT EXISTS va.calls_fact (
  org_id UUID, call_id UUID, direction LowCardinality(String), status LowCardinality(String),
  answered_by LowCardinality(String), runner_type LowCardinality(String), pathway_id Nullable(UUID),
  agent_id Nullable(UUID), experiment_arm_id Nullable(UUID), batch_id Nullable(UUID),
  disposition_tag LowCardinality(String), end_reason LowCardinality(String), connected_seconds Float64,
  transfer_seconds Float64, price_micros Int64, created_at DateTime64(3, 'UTC'), ended_at Nullable(DateTime64(3, 'UTC'))
) ENGINE = ReplacingMergeTree(created_at) PARTITION BY toYYYYMM(created_at) ORDER BY (org_id, call_id);

CREATE TABLE IF NOT EXISTS va.usage_events_raw (
  org_id UUID, meter LowCardinality(String), quantity Float64, amount_micros Int64, cost_micros Int64,
  reference_type LowCardinality(String), reference_id String, occurred_at DateTime64(3, 'UTC')
) ENGINE = MergeTree PARTITION BY toYYYYMM(occurred_at) ORDER BY (org_id, occurred_at);
```

```sql
CREATE TABLE IF NOT EXISTS va.eval_metrics (
  run_id String, metric LowCardinality(String), value Float64, created_at DateTime64(3, 'UTC')
) ENGINE = MergeTree PARTITION BY toYYYYMM(created_at) ORDER BY (run_id, metric);
```

ClickHouse DDL lives in `packages/va_events/src/va_events/clickhouse/migrations/*.sql` and is applied by
`events-sink` at startup (idempotent `CREATE ... IF NOT EXISTS`).
