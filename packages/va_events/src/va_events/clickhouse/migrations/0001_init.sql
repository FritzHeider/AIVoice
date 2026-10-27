-- ClickHouse analytics schema, specs/03-data-model.md §12 (tables copied verbatim from the spec).
-- Applied once by va_events.clickhouse.apply_migrations; never edit after release, add 0002_*.sql instead.
CREATE DATABASE IF NOT EXISTS va;

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

CREATE TABLE IF NOT EXISTS va.eval_metrics (
  run_id String, metric LowCardinality(String), value Float64, created_at DateTime64(3, 'UTC')
) ENGINE = MergeTree PARTITION BY toYYYYMM(created_at) ORDER BY (run_id, metric);

