# Scratchpad — Phase 02

## 2026-10-28 Planner: phase 02 start

Phase 01 fully ticked (0 open boxes). First unticked task in plan/phase-02.md is P02-T01; its dependency
P01-T10 is ticked. The api app does not depend on `va_db` yet and its container only has a login-role engine
from `va_common.checks`, so T01 must also wire a `va_db.Database` into the api container.

Spec-vs-plan naming: the plan says `va_api.auth.keys`, `Principal`, `require_scopes`; specs/04 (which wins)
says `va_common.auth` owns hashing/token primitives, the middleware is `va_api.auth.api_key_auth`, the principal
is the `RequestContext` dataclass (§1), and the dependency is `require_scope` (§2.4). Follow the spec names.

## Plan P02-T01

Goal: API key format, peppered hashing, `Authorization` parsing (Bearer + bare, `x-api-key` ignored), the
`RequestContext` principal, `get_principal` FastAPI dependency, `require_scope(...)`, Redis-gated
`last_used_at` touch, Redis key cache with invalidation helper. Specs: 04 §1, §2.1, §2.2, §2.3, §2.4 (the
dependency only; the coverage test comes when routers exist), §14 key-parsing bullet; 02 §6 (`API_KEY_PEPPER*`),
§10; 28 §2 (pepper rotation); 31 (`AUTH_FAILURE`, `FORBIDDEN`, `ORG_SUSPENDED`).

### Decisions to record in specs (same commit, `> Decision (2026-10-28):`)
1. specs/04 §2.1 — hash bytes: `sha256(bytes.fromhex(API_KEY_PEPPER) + plaintext.encode("ascii")).hexdigest()`.
   Pepper versioning: new env var `API_KEY_PEPPER_VERSION` (int ≥ 1, default 1) is the version of
   `API_KEY_PEPPER`; new keys store it in `api_keys.pepper_version`. Verification: key.pepper_version ==
   current → current pepper; == current-1 and `API_KEY_PEPPER_PREVIOUS` set → previous pepper; anything else →
   `AUTH_FAILURE`. Exactly one hash per verification attempt (still one dummy hash on unknown prefix).
2. specs/02 §6 — add row `API_KEY_PEPPER_VERSION | api | 1 | version number of API_KEY_PEPPER stored in
   api_keys.pepper_version` directly after `API_KEY_PEPPER_PREVIOUS`; add the same line to `.env.example`
   after line 156 with a trailing comment (scripts/tests/test_env_example.py enforces order/literal/comment).
3. specs/04 §2.2 — cache JSON also carries `pepper_version`, `org_status`, `plan_id` (needed to build
   `RequestContext` without a DB hit); org status/plan changes are therefore visible within ≤ 60 s unless the
   writer `DEL`s the entry. Missing `Authorization` → `AUTH_FAILURE` "Invalid API key." until session auth
   (P02-T04) adds the `va_session` cookie branch. Non-`va_` values and wrong length → `AUTH_FAILURE` with one
   dummy hash too (uniform timing).
4. specs/03 §2 — add `ApiKeyLookupRepo` to the `va_system` bullet (cite: specs/04 §2.2 names it); update the
   decision paragraph listing repo methods: `ApiKeyLookupRepo.get_by_prefix(prefix)` (join `orgs` for
   status/plan_id), `touch(api_key_id, now)`.
5. specs/03 §11 — add rows `apikey:{prefix}` (string JSON, 60 s, API key auth cache) and
   `apikey:{id}:touched` (string, 60 s, `last_used_at` throttle gate).
6. API_KEY_PEPPER is validated as empty-or-64-hex in settings (so `va_api.main` imports without env); the
   container factory raises `RuntimeError("API_KEY_PEPPER is not configured")` at lifespan start when empty.

### Files

**packages/va_common/src/va_common/auth.py** (new; pure, no I/O)
- `API_KEY_PREFIXES: Final = ("va_live_", "va_test_")`, `API_KEY_LENGTH: Final = 40`,
  `API_KEY_PREFIX_LENGTH: Final = 12`, `BASE62: Final = string.digits + string.ascii_uppercase + string.ascii_lowercase`,
  `API_KEY_RE: Final = re.compile(r"va_(?:live|test)_[0-9A-Za-z]{32}")` (reused later by audit redaction §13).
- `type KeyEnvironment = Literal["live", "test"]`
- `def generate_api_key(environment: KeyEnvironment) -> str` — `secrets.choice` ×32.
- `def api_key_prefix(plaintext: str) -> str` — first 12 chars.
- `def looks_like_api_key(value: str) -> bool` — prefix in API_KEY_PREFIXES and len == 40 (no charset check
  beyond that is required by spec; use API_KEY_RE.fullmatch for strictness).
- `def environment_of(plaintext: str) -> KeyEnvironment`.
- `@dataclass(frozen=True, slots=True) class Pepper: value: bytes; version: int` with `@classmethod from_hex(hex, version)`
  (ValueError unless exactly 32 bytes) and `__repr__` that hides the value.
- `class ApiKeyHasher` (`__init__(current: Pepper, previous: Pepper | None = None)`):
  - `current_version -> int` property
  - `hash(plaintext: str, *, version: int | None = None) -> str` (hex sha256(pepper||plaintext); version None →
    current; unknown version → `LookupError`)
  - `verify(plaintext: str, secret_hash: str, version: int) -> bool` — picks pepper by version (unknown →
    hashes with current pepper anyway and returns False, keeping one hash), compares with `hmac.compare_digest`.
  - `dummy_verify(plaintext: str) -> None` — one hash with current pepper, compared against a constant
    dummy hex via `hmac.compare_digest`, result discarded.
- `def sha256_hex(value: str) -> str` (token primitive used later for session/email tokens).

**apps/api/src/va_api/settings.py** — add fields `api_key_pepper: SecretStr = SecretStr("")`,
`api_key_pepper_previous: SecretStr = SecretStr("")`, `api_key_pepper_version: int = Field(1, ge=1)`, validator
(empty or 64 hex, lowercase/uppercase accepted) for both peppers; `def api_key_hasher(self) -> ApiKeyHasher`
raising `RuntimeError` when the pepper is empty (previous pepper → version current-1, only when set and
current version ≥ 2; if previous set with version 1, raise ValueError in a model validator).

**apps/api/pyproject.toml** — add `va-db` dependency + `[tool.uv.sources] va-db = { workspace = true }`; run
`uv lock`/`uv sync`. Update description string if it lists capabilities (add "auth").

**packages/va_db/src/va_db/repositories/api_keys.py** (new) — `ApiKeyLookupRepo(SystemRepository)` with a
docstring citing specs/03 §2 + specs/04 §2.2:
- `@dataclass(frozen=True, slots=True) class ApiKeyRecord: id, org_id, secret_hash, scopes: tuple[str, ...],
  environment, pepper_version, revoked: bool, expires_at: datetime | None, org_status, plan_id`, with
  `to_json() -> dict[str, Any]` / `from_json(data) -> ApiKeyRecord` (ISO datetimes, str UUIDs) for the cache.
- `async def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None` — `select(ApiKey, Org.status, Org.plan_id)
  .join(Org, Org.id == ApiKey.org_id).where(ApiKey.prefix == prefix)`; `revoked = revoked_at is not None`.
- `async def touch(self, api_key_id: uuid.UUID, now: datetime) -> None` — `UPDATE api_keys SET last_used_at=:now
  WHERE id=:id`.
- Export both from `va_db.repositories.__init__` (`__all__`).
- packages/va_db/tests/test_repositories.py: `len(_spec_system_repos()) == 7`.

**apps/api/src/va_api/auth/__init__.py** (new package) — re-export `RequestContext`, `get_principal`,
`require_scope`, `ApiKeyAuthenticator`.

**apps/api/src/va_api/auth/context.py** — `Role = MemberRole` (from va_db.models.tenancy),
`PrincipalType = Literal["api_key","user","widget","platform_admin"]`, `RequestContext` exactly the §1 fields
(frozen dataclass, slots), plus `def has_scope(self, scope: str) -> bool` (`"*" in scopes or scope in scopes`).

**apps/api/src/va_api/auth/api_key_auth.py**
- `INVALID_API_KEY: Final = "Invalid API key."`, `CACHE_TTL_S: Final = 60`, `TOUCH_INTERVAL_S: Final = 60`,
  `cache_key(prefix) -> "apikey:{prefix}"`, `touched_key(id) -> "apikey:{id}:touched"`.
- `def extract_api_key(authorization: str | None) -> str | None` — strip; `Bearer <x>` case-insensitive scheme
  (split once on whitespace) else bare value; empty → None.
- `async def invalidate_api_key_cache(redis: Redis, prefix: str) -> None` — `DEL` (used by P02-T10 revoke/roll).
- `class ApiKeyAuthenticator` (`__init__(database: Database, redis: Redis, hasher: ApiKeyHasher, clock: Clock)`):
  - `async def authenticate(self, authorization: str | None, *, method: str, request_id: str, ip: str | None,
    user_agent: str | None) -> tuple[RequestContext, ApiKeyRecord]` — steps: extract; not
    `looks_like_api_key` → `hasher.dummy_verify` + raise `AppError("AUTH_FAILURE", INVALID_API_KEY)`;
    `_lookup(prefix)` (Redis GET → `ApiKeyRecord.from_json`; miss → `system_session()` +
    `ApiKeyLookupRepo.get_by_prefix`; found → `SET apikey:{prefix} <json> EX 60`; not found → dummy hash +
    AUTH_FAILURE; negative results are NOT cached); `hasher.verify(...)` false → AUTH_FAILURE; revoked,
    `expires_at <= clock.now()`, `org_status == "closed"` → AUTH_FAILURE; `org_status == "suspended"` and
    method not in {GET, HEAD, OPTIONS} → `AppError("ORG_SUSPENDED", "Organization is suspended; only read
    requests are allowed.")`. Builds `RequestContext(principal_type="api_key", principal_id=key.id, role=None,
    scopes=frozenset(key.scopes), environment=key.environment, plan_id=key.plan_id, acting_parent_org_id=None, …)`.
  - `async def touch_if_due(self, key_id: uuid.UUID) -> None` — `SET touched_key NX EX 60`; when set, open
    `system_session()` and `ApiKeyLookupRepo(...).touch(key_id, clock.now())`; any exception (`RedisError`,
    `SQLAlchemyError`, `OSError`) is logged with structlog `api_key_touch_failed` (key prefix only, never the
    secret) and swallowed.
- Redis errors during cache GET/SET: fall back to the DB lookup (log warning `api_key_cache_unavailable`);
  never fail auth because Redis is down.

**apps/api/src/va_api/auth/dependencies.py**
- `async def get_principal(request: Request, background: BackgroundTasks) -> RequestContext` — reads
  `request.app.state.container.authenticator` (typed via a small `authenticator_of(request)` helper),
  `request.headers.get("authorization")` (never `x-api-key`), `request_id_of(request)`,
  `request.client.host`, `user-agent`; schedules `authenticator.touch_if_due(key.id)` via `background.add_task`;
  stores ctx on `request.state.ctx`; binds `org_id` to structlog contextvars.
- `def require_scope(scope: str) -> Callable[..., Awaitable[RequestContext]]` — validates `scope` against
  `SCOPES` at definition time (`ValueError` on unknown scope, so routers can't typo), returns a dependency that
  depends on `get_principal` and raises `AppError("FORBIDDEN", f"Missing required scope '{scope}'.")` unless
  `ctx.has_scope(scope)`; the returned callable carries attribute `required_scope = scope` (the §2.4 coverage test
  in a later task finds it via `route.dependant`).
- `SCOPES: Final[frozenset[str]]` — every scope of §2.3 (expand `a / b` rows into both scopes), in
  `apps/api/src/va_api/auth/scopes.py` along with `READ_WRITE_RESOURCES`.

**apps/api/src/va_api/container.py** — add `database: Database` (from `Database.from_settings(settings)`,
disposed via the exit stack: `deps` has a stack-less API, so wrap with `contextlib.AsyncExitStack` in
`build_container` and `push_async_callback(database.dispose)`) and `authenticator: ApiKeyAuthenticator`
(`settings.api_key_hasher()`, `SystemClock()`). Do NOT call `configure_database` (explicit injection).
Container field additions must keep `FakeChecks`/`FakeContainer` tests working: dependencies read
`container.authenticator` only at request time.

**.env.example**, **specs/02**, **specs/03**, **specs/04** — per Decisions above.
**AGENTS.md** — note: api auth lives in `va_api.auth`; `API_KEY_PEPPER` must be 64 hex or the api lifespan
fails; tests build `ApiKeyHasher(Pepper.from_hex(...))` directly.

### Tests

packages/va_common/tests/test_auth.py (unit)
- `test_generate_api_key_shape[live|test]` — len 40, regex fullmatch, prefix, base62 only.
- `test_generated_keys_are_unique` — 1 000 keys, 1 000 distinct.
- `test_prefix_is_first_12_chars`.
- `test_looks_like_api_key_rejects` parametrized: wrong prefix `va_prod_…`, len 39/41, `sk_live_…`, non-base62 char.
- `test_hash_matches_spec_formula` — equals `hashlib.sha256(bytes.fromhex(p)+key.encode()).hexdigest()`.
- `test_verify_uses_compare_digest` — monkeypatch `va_common.auth.hmac.compare_digest` with a spy; called once.
- `test_verify_previous_pepper_for_older_version` / `test_unknown_version_fails_with_one_hash`.
- `test_dummy_verify_hashes_once` (spy on `hashlib.sha256` via module attr).
- `test_pepper_from_hex_rejects_wrong_length`, `test_pepper_repr_hides_value`.

apps/api/tests/test_settings_auth.py (unit)
- empty pepper allowed at construction; `api_key_hasher()` raises RuntimeError; non-hex/short pepper →
  ValidationError; previous pepper requires version ≥ 2; hasher versions wired correctly.

apps/api/tests/test_api_key_auth.py (unit; fakeredis.aioredis + fake lookup)
- Make `ApiKeyAuthenticator` testable without Postgres: constructor takes a `lookup: Callable[[str],
  Awaitable[ApiKeyRecord | None]]` and `toucher: Callable[[UUID, datetime], Awaitable[None]]`; the production
  wiring passes `database`-backed functions (`db_lookup(database)`, `db_touch(database)` in api_key_auth.py).
  (Builder: use this injection shape rather than a Database parameter.)
- `test_extract_api_key` parametrized: `Bearer k`, `bearer k`, `BEARER   k`, bare `k`, `  k  `, `""`, None,
  `Basic xyz` → returns `Basic xyz` as bare (then fails format check).
- `test_bearer_and_bare_headers_authenticate` — both produce identical ctx (org_id, scopes, environment, plan_id,
  principal_type api_key, role None).
- `test_x_api_key_header_is_ignored` — via the HTTP app: only `x-api-key` set → 401 AUTH_FAILURE.
- `test_wrong_prefix_or_length_fails_with_one_dummy_hash` — spy hasher counts exactly 1 hash, lookup not called.
- `test_unknown_prefix_hashes_once` (§14: assert hash called once on unknown prefix).
- `test_wrong_secret_same_prefix_fails`.
- `test_revoked_key_fails`, `test_expired_key_fails` (FrozenClock at/after expires_at; just before → ok),
  `test_closed_org_fails`.
- `test_suspended_org_allows_reads_blocks_writes` — GET ok, POST → ORG_SUSPENDED 403.
- `test_cache_hit_skips_lookup` — 2nd auth does not call lookup; cached JSON has the 60 s TTL
  (`await redis.ttl(...)` in 1..60) and contains no plaintext key.
- `test_unknown_prefix_not_cached`.
- `test_invalidate_api_key_cache_forces_reload`.
- `test_redis_down_falls_back_to_lookup` — redis client whose get/set raise `RedisConnectionError`.
- `test_touch_at_most_once_per_minute` — 3 touches → toucher called once; after FrozenClock advance + fakeredis
  key expiry (delete key / use `redis.pexpire`), called again (no sleeps).
- `test_touch_failure_is_swallowed_and_logged` (structlog capture; log has no plaintext).

apps/api/tests/test_auth_dependencies.py (HTTP, test-only router mounted via `create_app(routers=...)` with a
FakeChecks-derived container carrying an authenticator built on fakeredis + fake lookup)
- `test_require_scope_allows_wildcard_and_exact`, `test_require_scope_denies_with_forbidden` — 403, legacy and
  envelope routes render `FORBIDDEN` in their family's shape (use one legacy `/v1/calls/_auth` and one envelope
  `/v1/knowledge/_auth` route in `tests/support/sample_routes.py` or a new `auth_routes.py` helper).
- `test_missing_authorization_is_auth_failure` — 401 "Invalid API key.".
- `test_require_scope_rejects_unknown_scope_name` — ValueError at definition.
- `test_scopes_match_spec_table` — parse specs/04 §2.3 table, expand `a` / `b`, equals `SCOPES`.
- `test_principal_stored_on_request_state_and_org_bound_in_logs`.
- `test_touch_scheduled_as_background_task` — after a request, toucher called once.

packages/va_db/tests/test_api_key_lookup_integration.py (integration, migrated_url DB via existing fixtures)
- `get_by_prefix` returns record with org status/plan; unknown → None; revoked flag; `touch` updates
  `last_used_at`; runs on `system_session` (RLS bypass: no app.org_id set). Reuse `model_factories.make_org` and
  existing helpers (jscpd).
- `test_record_json_round_trip` (unit) for `ApiKeyRecord.to_json/from_json`.

### Gates
`make fmt lint typecheck placeholders test` (+ jscpd duplication, radon complexity, pip-audit per memories);
coverage ≥ 85 % for va_common, va_db, va_api. No TS changes → `test-ts` not required. No call flow → no e2e.

### Risks
- `va_api.main` builds `ApiSettings()` at import: keep pepper optional at construction (Decision 6).
- Adding `va-db` to api pulls SQLAlchemy models into api import time — fine, no I/O.
- test_repositories asserts the system-repo set equals spec bullet: update the spec bullet AND the count to 7.
- test_env_example enforces order/literal/comment for the new env var.
- Background task exceptions surface in the server; touch must swallow and log.
- fakeredis TTL/time: don't rely on wall-clock expiry; delete the gate key to simulate expiry.
- Do not log `Authorization` values anywhere; masking.py already masks `pepper`.

## 2026-10-29 Builder: P02-T01 build notes

Implemented per plan with these deviations/details:
- Redis keys use hash tags per specs/03 §11 preamble: `apikey:{<prefix>}`, `apikey:{<uuid>}:touched` (recorded in
  specs/04 §2.2 Decision and the §11 rows).
- `ApiKeyAuthenticator(redis=, hasher=, lookup=, toucher=, clock=)`; production wiring `db_lookup(database)` /
  `db_touch(database)`; malformed cache JSON is treated as a miss.
- Hash input is `plaintext.encode()` (UTF-8, ASCII for valid keys) so malformed non-ASCII headers still cost one hash.
- `api_helpers.make_settings()` now injects a random pepper; `.env.example` + specs/02 §6 gained
  `API_KEY_PEPPER_VERSION=1` (env test counts 107 names / 106 literal keys).
- FastAPI nests included routers (`_IncludedRouter`) in `app.routes`; route enumeration goes through
  `app.state.routers` (important for the §2.4 coverage test and P02-T15).

## Review P02-T01 (2026-10-29): approved
Re-ran make lint typecheck placeholders test: all green (1914 passed, 99.83 %). Checked va_common.auth, va_api.auth.*,
ApiKeyLookupRepo, settings/container, spec decisions against specs/04 §1–§2.3 and specs/03 §2/§11. Non-blocking notes:
container opens a second engine pair via Database.from_settings alongside deps.postgres (fine for now; consolidate
when tenant routes land); get_principal binds org_id via structlog directly (per-request task context, no leak).

## 2026-10-29 Planner: P02-T01 approved → next P02-T02

## Plan P02-T02 — Request rate limiting (specs/04 §11, specs/03 §11, specs/24 §6.1, specs/31 RATE_LIMITED)

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. **specs/04 §11 is normative** for request rates; specs/24 §6 item 1 (20/40, 100/200, 200/400) conflicts — replace its
   numbers with a pointer "per specs/04 §11" + Decision note in both specs. Phase plan text says `RateLimit-*` headers;
   spec says `X-RateLimit-*` → spec wins (no plan edit needed beyond tick text).
2. Burst: the `burst` column is the `default` bucket capacity (always 2 × rate). `calls_send` capacity is also 2 × its
   rate (4 / 20 / 50 / 200). `orgs.limits.rate_per_sec` (positive int, any plan; admin-set override, precedence over the
   plan per specs/24 §1) replaces the `default` rate, capacity 2 × override; `calls_send` has no override.
   Unknown `plan_id` → `start` limits + warning log `rate_limit_unknown_plan`.
3. Keys (hash tags per §11 preamble): `rl:key:{<api_key_id>}:<bucket>`, `rl:user:{<user_id>}:<bucket>` (user and
   platform_admin principals), bucket ∈ `default`, `calls_send`. specs/03 §11 row: replace `rl:key:{api_key_id}:{window}`
   by `rl:key:{api_key_id}:{bucket}` + add the `rl:user:` row; type "hash (`tokens`, `ts`)"; TTL "2 × fill time".
4. Charging: every authenticated request takes 1 token from `default` (inside `get_principal`, after auth succeeds,
   before scope checks). Routes that send calls (`POST /v1/calls`, batch create = cost 1) additionally depend on
   `rate_limit("calls_send")`. Denied → `AppError("RATE_LIMITED", "Rate limit exceeded; retry after N seconds.")`.
   No refund of the `default` token when `calls_send` denies.
5. Headers: on every response of a rate-limited request: `X-RateLimit-Limit` = bucket capacity (burst),
   `X-RateLimit-Remaining` = floor(tokens left) ≥ 0, `X-RateLimit-Reset` = delta seconds (ceil, integer) until the
   bucket is full again (not epoch). 429 additionally `Retry-After` = max(1, ceil(retry_after_ms / 1000)). When two
   buckets were charged the headers describe the last (most specific) one. Headers are added by an ASGI middleware
   reading a scope key, so they survive error responses (403/404/429) rendered by exception handlers.
6. Redis unavailable (`RedisError`, `OSError`) → fail open (request allowed, no headers), structlog warning
   `rate_limit_unavailable` (no key material). Matches T01 cache behaviour.
7. Time source: `now_ms` from the injected `Clock` (not Redis `TIME`) so tests are deterministic; the Lua script is
   registered with `redis.register_script` (EVALSHA with automatic NOSCRIPT reload — no network I/O at startup;
   satisfies specs/24 "SCRIPT LOAD + EVALSHA" in spirit; note in the Decision).

### Files

**packages/va_common/src/va_common/token_bucket.py** (new; generic, reused later by widget/sms/batch pacing)
- `TOKEN_BUCKET_LUA: Final[str]` — KEYS[1]; ARGV rate (tokens/s, float), burst (int), now_ms (int), cost (int).
  Reads `HMGET key tokens ts`; missing → tokens=burst, ts=now; `elapsed = max(0, now - ts)`;
  `tokens = min(burst, tokens + elapsed * rate / 1000)`; if tokens >= cost → tokens -= cost, allowed=1, retry=0 else
  allowed=0, `retry = ceil((cost - tokens) * 1000 / rate)`; `HSET key tokens <tostring(tokens)> ts now`;
  `PEXPIRE key ceil(2 * burst * 1000 / rate)`; returns `{allowed, tokens_x1000 (floor), retry_ms}` (return tokens×1000
  floored as integer because Lua→Redis converts numbers to integers by truncation; Python divides).
  Never let ts move backwards: `ts = max(ts, now)` stored.
- `@dataclass(frozen=True, slots=True) class BucketLimit: rate_per_s: float; burst: int` (validate > 0 in `__post_init__`,
  ValueError) + `fill_time_s` property (`burst / rate_per_s`).
- `@dataclass(frozen=True, slots=True) class BucketDecision: allowed: bool; limit: int; remaining: int;
  reset_s: int; retry_after_s: int` (remaining = floor(tokens); reset_s = ceil((burst - tokens)/rate); retry_after_s = 0
  when allowed else max(1, ceil(retry_ms/1000))).
- `class TokenBucket: __init__(self, redis: Redis)`; `self._script = redis.register_script(TOKEN_BUCKET_LUA)`;
  `async def take(self, key: str, limit: BucketLimit, *, now_ms: int, cost: int = 1) -> BucketDecision`
  (cost ≥ 1 else ValueError; Redis errors propagate — caller decides fail-open).
- Export in va_common (match existing `__init__` export style if it re-exports; otherwise import path only).

**apps/api/src/va_api/rate_limit.py** (new)
- `type Bucket = Literal["default", "calls_send"]`; `BUCKETS: Final = ("default", "calls_send")`.
- `@dataclass(frozen=True, slots=True) class PlanRateLimits: default: BucketLimit; calls_send: BucketLimit` + `def for_bucket(b)`.
- `PLAN_RATE_LIMITS: Final[Mapping[str, PlanRateLimits]]` (MappingProxyType): start 10/20 & 2/4, build 50/100 & 10/20,
  scale 100/200 & 25/50, enterprise 300/600 & 100/200.
- `def plan_rate_limits(plan_id: str, rate_per_sec: int | None = None) -> PlanRateLimits` (override → default
  `BucketLimit(override, 2*override)`; unknown plan → start + warning).
- `def rate_limit_key(ctx: RequestContext, bucket: Bucket) -> str` — `rl:key:{<id>}:<bucket>` for api_key,
  `rl:user:{<id>}:<bucket>` for user/platform_admin; widget → ValueError (widgets use `widget:rl:*`).
- `RATE_LIMIT_SCOPE_KEY: Final = "va.rate_limit"`; `def record_decision(scope, decision)`; `def rate_limit_headers(decision)
  -> tuple[tuple[str, str], ...]` (adds `retry-after` when not allowed).
- `class RateLimiter: __init__(self, redis: Redis, clock: Clock)`;
  `async def check(self, ctx: RequestContext, bucket: Bucket, limits: PlanRateLimits) -> BucketDecision | None`
  (None when Redis fails → warning `rate_limit_unavailable` with `bucket`, `principal_type`).
  `now_ms = int(clock.now().timestamp() * 1000)`.
- `async def enforce(request: Request, limiter: RateLimiter, ctx, bucket, limits) -> None` — check, store decision in
  `request.scope[RATE_LIMIT_SCOPE_KEY]`, raise `AppError("RATE_LIMITED", f"Rate limit exceeded; retry after {n} seconds.")`
  when denied (message format recorded in specs/04 §11 Decision).
- `def rate_limit(bucket: Literal["calls_send"]) -> Callable[..., Awaitable[RequestContext]]` — dependency depending
  on `get_principal`; limits = `plan_rate_limits(ctx.plan_id)`; carries attribute `rate_limit_bucket = bucket`.
- `class RateLimitHeadersMiddleware` (pure ASGI, like `RequestContextMiddleware`): on `http.response.start` merges
  `rate_limit_headers(scope[...])` via `middleware.merge_headers` when a decision is present.
- `limiter_of(request) -> RateLimiter` reading `request.app.state.container.rate_limiter`.

**apps/api/src/va_api/auth/dependencies.py** — after authenticate: `limits = plan_rate_limits(ctx.plan_id,
record.rate_per_sec)`; `await enforce(request, limiter_of(request), ctx, "default", limits)` (before touch scheduling? —
schedule touch first: an authenticated-but-throttled request still used the key). Avoid an import cycle: rate_limit.py
imports `get_principal` from dependencies; dependencies imports `enforce/plan_rate_limits/limiter_of` from rate_limit →
cycle. Resolve by putting the `rate_limit(...)` dependency factory in `va_api/auth/dependencies.py` too (and the pure
pieces in `va_api/rate_limit.py`, which must not import `va_api.auth.dependencies`). Re-export `rate_limit` from
`va_api.auth`.

**packages/va_db/src/va_db/repositories/api_keys.py** — `ApiKeyRecord.rate_per_sec: int | None` from
`Org.limits.get("rate_per_sec")` (select `Org.limits` too; accept only `int` > 0 that is not bool, else None);
`to_json`/`from_json` include it (`from_json` tolerates a missing key → None so 60 s-old cache entries still parse).
Update fake `KeyStore.issue(...)` (`apps/api/tests/support/auth_helpers.py`) with `rate_per_sec: int | None = None`.

**apps/api/src/va_api/container.py** — `rate_limiter: RateLimiter` field (`RateLimiter(redis, clock)`; share one
`SystemClock()` with the authenticator). `AuthContainer` test helper gets `rate_limiter` property (same fakeredis +
FrozenClock).

**apps/api/src/va_api/main.py** — `app.add_middleware(RateLimitHeadersMiddleware)` before `MetricsMiddleware` line
(innermost of ours is fine; it must wrap the exception middleware, which every user middleware does);
`CORS_EXPOSE_HEADERS` += `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
(update test_cors expectations if it asserts the list; specs/28 §5 CORS list if it enumerates exposed headers — check
and record).

**pyproject.toml (root dev deps)** — `fakeredis[lua]>=2.40.0` (pulls `lupa`) so Lua runs in unit tests; `uv lock`,
`uv sync --all-packages`; pip-audit must stay clean.

**specs/04 §11, specs/03 §11, specs/24 §6** — per Decisions. **AGENTS.md** — rate limiter location, fakeredis[lua],
header semantics, "routes sending calls add `Depends(rate_limit("calls_send"))`".

### Tests
packages/va_common/tests/test_token_bucket.py (fakeredis[lua], unit)
- `test_fresh_bucket_starts_full` — first take: allowed, remaining = burst-1, reset_s = ceil(1/rate).
- `test_exhaustion_denies_with_retry_after` — burst takes allowed, next denied, retry_after_s = ceil(1/rate) (≥1).
- `test_refill_math` parametrized (rate 10, burst 20): drain, advance now_ms by 100 → 1 token; by 550 → 5; by 10 000 →
  capped at 20 (remaining 19 after take).
- `test_fractional_rate` (rate 0.5) retry_after 2 s.
- `test_cost_greater_than_one`, `test_cost_above_burst_never_allowed` (retry computed, never negative).
- `test_clock_going_backwards_does_not_mint_tokens`.
- `test_ttl_is_twice_fill_time` — `pttl` ≈ 2 × burst/rate s.
- `test_bucket_limit_validation` (rate ≤ 0, burst ≤ 0 → ValueError); `test_cost_must_be_positive`.
- `test_concurrent_takes_never_exceed_burst` — `asyncio.gather` 100 takes, same now_ms, burst 20 → exactly 20 allowed.
packages/va_common/tests/test_redis_integration.py — add `test_token_bucket_against_real_redis` reusing the module
`redis_url` fixture (no extra container): 100 concurrent takes, burst 20 → 20 allowed; refill after now_ms + 1000.

apps/api/tests/test_rate_limit.py (unit + HTTP via `auth_app(AuthContainer)`; rate-limit routes in auth_helpers or a
small `calls_send` route added to `_auth_routes`)
- `test_plan_rate_limits_match_spec_table` — parse specs/04 §11 table (rate/burst per plan) and compare to
  `PLAN_RATE_LIMITS` (default + calls_send rates, burst; calls_send capacity 2×).
- `test_unknown_plan_falls_back_to_start`, `test_rate_per_sec_override`.
- `test_rate_limit_key_for_api_key_and_user` (hash tags), `test_rate_limit_key_rejects_widget`.
- `test_headers_on_success` — X-RateLimit-Limit 20 (start), Remaining 19, Reset 1.
- `test_429_after_burst_with_retry_after` — 20 ok, 21st → 429 `RATE_LIMITED` in the route family envelope (legacy and
  envelope routes), `Retry-After` = "1", Remaining "0".
- `test_refill_after_clock_advance` — FrozenClock.advance(0.1) → one more request allowed (start rate 10/s).
- `test_headers_on_error_responses` — FORBIDDEN (scope denied) response still has X-RateLimit-*.
- `test_no_headers_when_auth_fails` — 401 has no X-RateLimit-* (no bucket charged).
- `test_calls_send_bucket` — route with `rate_limit("calls_send")`: start → 4 allowed then 429; headers show Limit 4;
  default bucket was also charged (Remaining on a plain route afterward = 20 - 5 - 1).
- `test_per_key_isolation` — two keys of one org have independent buckets.
- `test_enterprise_override_from_org_limits` — record `rate_per_sec=1` → Limit 2.
- `test_redis_down_fails_open` — redis whose evalsha raises `RedisConnectionError`: 200, no headers, warning logged.
- `test_concurrent_requests_never_exceed_bucket` — 100 parallel requests (`asyncio.gather` over httpx ASGITransport
  client) with frozen clock, start plan → exactly 20 × 200 and 80 × 429.
- `test_cors_exposes_rate_limit_headers` (or extend test_cors).
packages/va_db/tests — extend `ApiKeyRecord` JSON round-trip with `rate_per_sec` and missing-key tolerance; integration
`get_by_prefix` returns `rate_per_sec` from `orgs.limits` (set `limits={"rate_per_sec": 5}` on the org) and None for
non-int (`"5"`, `true`).

### Gates
`make fmt lint typecheck placeholders test` (+ jscpd, radon, pip-audit, openapi drift if middleware touches the doc —
it should not). No TS, no call flow → no test-ts/e2e.

### Risks
- fakeredis Lua needs `lupa`; if lupa wheel/pip-audit problems → fall back to the real-Redis integration module only
  and record a fix memory.
- Lua number→integer truncation on return: return tokens × 1000 floored; never return floats.
- Import cycle auth.dependencies ↔ rate_limit (see layout above).
- Exception handlers render inside ExceptionMiddleware, so the header middleware sees 429/403 responses; unhandled
  500s bypass it (acceptable).
- `test_env_example` untouched (no new env vars). CORS expose list may be pinned by a test and by specs/28 §5.
- Concurrency test must use one event loop + fakeredis shared server; keep it deterministic (frozen clock).

## 2026-10-29 Builder: P02-T02 build notes

Implemented per plan. Details/deviations:
- `va_api.rate_limit` imports `RequestContext` only under TYPE_CHECKING (va_api.auth → dependencies → rate_limit
  cycle when rate_limit is imported first). `rate_limit("calls_send")` lives in `va_api.auth.dependencies`, re-exported
  from `va_api.auth`; it carries `rate_limit_bucket`.
- `ApiKeyRecord.rate_per_sec` defaults to None (keeps existing constructors valid); `rate_per_sec_override(limits)` in
  `va_db.repositories.api_keys` (positive int, not bool).
- Lua stores `ts` with `string.format('%d', ts)`, tokens with tostring; returns tokens×1000 floored.
- RATE_LIMITED message: "Rate limit exceeded; retry after N second(s)." (singular for 1), recorded in the specs/04 §11 Decision.
- A `default` denial never charges `calls_send` (test_default_bucket_denial_skips_calls_send).
- `S105` false positive on TOKEN_BUCKET_LUA: the noqa goes after the closing `"""` (ruff maps multi-line strings
  to their last line; a noqa after the opening quotes would end up inside the Lua source).
- test_auth_dependencies route-discovery and test_cors expose-header expectations updated for the /send route and
  the X-RateLimit-* expose headers.
Gates: make fmt/lint/typecheck (239 files)/placeholders (0) green; make test 1978 passed, 99.83 % (first full run hit
the known va_db 60 s Docker setup-timeout flake, 59 errors in va_db fixtures only; va_db alone 675 passed; rerun of
full suite green). pip-audit clean (lupa 2.8), pnpm audit clean, jscpd 0 clones, radon avg A (1.90), openapi current.

## Review P02-T02 (2026-10-29): approved
Re-ran make lint typecheck placeholders (green, mypy 239 files, 0 placeholder findings) and make test (1978 passed,
TOTAL 99 %). Checked plan table vs specs/04 §11, key names vs specs/03 §11, RATE_LIMITED vs specs/31, Lua atomicity,
header middleware ordering (merge_headers keeps handler headers), fail-open logging (no key material), per-request
dependency caching (default charged once with rate_limit("calls_send")). Reviewer fix: specs/04 §11 Decision wording
claimed specs/28 §5 enumerates the CORS expose list; reworded to point at va_api.main.CORS_EXPOSE_HEADERS.

## 2026-10-29 Planner: P02-T02 approved → next P02-T03

## Plan P02-T03 — Idempotency-Key (specs/04 §12, specs/05 §Headers row, specs/03 §3 `idempotency_keys`, specs/31)

Cleanup job function already exists: `va_db.repositories.retention.PURGE_RULES` has `idempotency_keys` 24 h
(RetentionRepo.purge_expired). This task adds a test only if no existing test asserts that rule (grep first; the
retention tests likely cover it, then nothing to add there).

### Design summary
Claim (DB row with 60 s lock) happens in a dependency after auth + `default` rate bucket + scope check; capture of
the response happens in a pure-ASGI middleware that sees the claim in the scope. Replay is raised as an exception
and rendered by a dedicated handler. Org id is the key space (PK `(org_id, key)`), so unauthenticated POSTs are
not idempotent-capable.

### Decisions to record (specs/04 §12, same commit, `> Decision (2026-10-29):`)
1. **Where**: claimed inside `require_scope`'s dependency after the scope check passes, for `POST` requests carrying
   `Idempotency-Key`, once per request (scope marker guards routes with several `require_scope` deps). Principals
   that fail auth (401), the `default` bucket (429), or the scope check (403) never create a row. Routes without
   `require_scope` (public allowlist: health, signup/login, webhooks) ignore the header. Header on non-POST: ignored.
2. **Key syntax**: 1–255 chars, printable ASCII 0x20–0x7E (specs/05 "printable"); else
   `AppError("INVALID_INPUT", "Idempotency-Key must be 1-255 printable ASCII characters.", field="Idempotency-Key")`
   (check AppError signature for `field`; if absent, build via existing item API). Never stored.
3. **Hash**: `sha256(method + "\n" + path + "\n" + canonical_body)` hex; `path` = `scope["path"]` (concrete path,
   query string excluded, per spec text "method+path+body"); canonical body = `json.dumps(obj, sort_keys=True,
   separators=(",", ":"), ensure_ascii=False).encode()` when the body parses as JSON, else the raw bytes (empty body
   → b""). Bodies > 64 KiB are canonicalised in `asyncio.to_thread` (rule 7).
4. **Stored**: 2xx and 4xx JSON responses (status + parsed body into `response_body` jsonb, lock cleared). Not stored
   (row deleted): 5xx, unhandled exceptions, **429** (a transient throttle, e.g. `calls_send` after claim, must stay
   retryable per its `Retry-After`), non-JSON or empty-bodied responses (not replayable faithfully). Cancellation /
   client disconnect leaves the lock to expire.
5. **Replay**: stored status, stored JSON body (JSON-equal, not byte-equal — jsonb normalises key order/whitespace),
   `Content-Type: application/json`, header `Idempotent-Replayed: true`; body keeps the original `request_id`, the
   `X-Request-Id` header is the new request's. Increments `va_idempotent_replays_total`.
6. **Expired rows**: a row older than 24 h (created_at < now − 24 h, not yet purged) is treated as absent and
   re-claimed in place. A row with no response whose `locked_until` ≤ now (crashed worker) is re-claimed
   (lock refreshed). Time comes from the injected `Clock` (created_at/updated_at/locked_until written explicitly).
7. Mismatched hash wins over everything (checked first): `IDEMPOTENCY_KEY_REUSED` 422 even if the original is in
   flight. Same hash + locked → `IDEMPOTENCY_IN_PROGRESS` 409. Messages:
   "Idempotency-Key was already used with a different request." / "A request with this Idempotency-Key is still in progress."
Also add `Idempotent-Replayed` to `CORS_EXPOSE_HEADERS` (update test_cors + specs/04 §11-style mention in §12
Decision pointing at `va_api.main.CORS_EXPOSE_HEADERS`).

### Files

**packages/va_db/src/va_db/repositories/idempotency.py** (new; tenant-scoped, NOT a SystemRepository)
- `@dataclass(frozen=True, slots=True) class IdempotencyClaim: key: str` (fresh/re-claimed)
- `@dataclass(frozen=True, slots=True) class IdempotencyReplay: status: int; body: Any`
- `class IdempotencyConflict(StrEnum): IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"; KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"`
- `type ClaimOutcome = IdempotencyClaim | IdempotencyReplay | IdempotencyConflict`
- `LOCK_TTL: Final = timedelta(seconds=60)`, `REPLAY_WINDOW: Final = timedelta(hours=24)` (reuse the 24 h from
  PURGE_RULES? keep one source: define REPLAY_WINDOW here and make the retention rule reference it — import
  direction retention → idempotency is fine).
- `class IdempotencyRepo: __init__(self, session: AsyncSession, org_id: uuid.UUID)`
  - `async def claim(self, *, key, method, path, request_hash, now: datetime) -> ClaimOutcome`:
    `insert(IdempotencyKey).values(org_id, key, method, path, request_hash, locked_until=now+LOCK_TTL,
    created_at=now, updated_at=now).on_conflict_do_nothing(index_elements=[org_id, key]).returning(key)` (pg dialect
    insert); inserted → Claim. Else `select ... where org_id, key .with_for_update()`; row missing (deleted between)
    → retry the insert once (loop max 2, then IN_PROGRESS). Expired (created_at < now − REPLAY_WINDOW) → UPDATE all
    fields like a fresh claim (response NULL) → Claim. hash differs → KEY_REUSED. response_status not None → Replay.
    locked_until > now → IN_PROGRESS. else UPDATE locked_until=now+LOCK_TTL → Claim.
  - `async def complete(self, key, *, status: int, body: Any, now) -> None` (UPDATE response_status/body,
    locked_until NULL, updated_at=now; filter org_id+key).
  - `async def release(self, key) -> None` (DELETE where org_id, key, response_status IS NULL — never delete a
    completed row).
  - Every statement filters `org_id` explicitly (RLS + explicit filter, rule 5).
- Export from `va_db.repositories.__init__`.

**apps/api/src/va_api/idempotency.py** (new; must NOT import `va_api.auth` at runtime — RequestContext TYPE_CHECKING
only, same as rate_limit, see memory mem-1793258976-9d0b)
- `IDEMPOTENCY_HEADER: Final = "idempotency-key"`, `REPLAYED_HEADER: Final = "Idempotent-Replayed"`,
  `IDEMPOTENCY_SCOPE_KEY: Final = "va.idempotency"`, `MAX_KEY_LENGTH: Final = 255`.
- `class IdempotencyStore(Protocol)`: `claim(org_id, *, key, method, path, request_hash) -> ClaimOutcome`,
  `complete(org_id, key, *, status, body)`, `release(org_id, key)`.
- `class DbIdempotencyStore: __init__(self, database: Database, clock: Clock)` — each method opens
  `database.tenant_session(org_id)` and runs `IdempotencyRepo` (claim commits immediately so concurrent duplicates
  see the row).
- `def validate_key(raw: str) -> str`; `def canonical_body(body: bytes) -> bytes`;
  `async def request_hash(method: str, path: str, body: bytes) -> str`.
- `class IdempotentReplay(Exception)` carrying status + body; `def render_replay(request, exc) -> Response`
  (JSONResponse with REPLAYED_HEADER "true"; metrics counter `request.app.state.metrics.counter(
  "va_idempotent_replays_total").inc()`).
- `@dataclass(slots=True) class ActiveClaim: org_id: uuid.UUID; key: str; store: IdempotencyStore`.
- `async def claim_idempotency(request: Request, ctx: RequestContext) -> None` — no-op unless POST + header +
  not already claimed for this request; validate, hash (`await request.body()`), `store.claim`; Replay → raise
  IdempotentReplay; Conflict → `AppError(code, message)`; Claim → `request.scope[IDEMPOTENCY_SCOPE_KEY] = ActiveClaim`.
- `def idempotency_of(request) -> IdempotencyStore` (container.idempotency).
- `class IdempotencyMiddleware` (pure ASGI): wraps send; at `http.response.start` reads the claim from scope (absent →
  passthrough); buffers body chunks while forwarding non-final ones; before forwarding the final chunk: status in
  2xx/4xx except 429 and body parses as JSON (via `json.loads`, to_thread if > 64 KiB) → `complete`, else `release`.
  Store failure (SQLAlchemyError/OSError) → log `idempotency_store_failed` (key not logged — log only org_id/status),
  attempt release, still forward. App raising `Exception` with a claim present and nothing finalised → `release` then
  re-raise. A claim whose response never started (e.g. exception) handled by same path.
- `def install_idempotency(app: FastAPI) -> None` — registers the IdempotentReplay handler (middleware added in
  main explicitly for ordering).

**apps/api/src/va_api/auth/dependencies.py** — in `require_scope.check_scope`, after the scope check:
`await claim_idempotency(request, ctx)` (add `request: Request` param). Update module docstring.

**apps/api/src/va_api/container.py** — `idempotency: IdempotencyStore` field = `DbIdempotencyStore(database, clock)`.

**apps/api/src/va_api/main.py** — `app.add_middleware(IdempotencyMiddleware)` immediately before
`RateLimitHeadersMiddleware` line (so it is inner to it; replays still get rate headers); `install_idempotency(app)`
after `install_handlers(app)`; `CORS_EXPOSE_HEADERS += "Idempotent-Replayed"`; docstring middleware order updated.

**apps/api/tests/support/idempotency_helpers.py** (new) — `MemoryIdempotencyStore` implementing the Protocol with
the same semantics (dict keyed (org_id, key), FrozenClock, `asyncio.Lock`), recording calls; plus fault-injection
flag `fail_complete`. `AuthContainer` gains `idempotency` field (default MemoryIdempotencyStore sharing the clock).
Add to `_auth_routes`: `POST /status/{code}` (returns JSON with a counter so replay is distinguishable from re-run:
body includes `{"n": <calls>}`, status = code via `JSONResponse`), `POST /boom` (raises RuntimeError), `POST /text`
(PlainTextResponse), and a body-model route `POST /echo` (pydantic body). Keep route-discovery tests updated
(test_auth_dependencies expects the route list — memory mem-1793257682-8f0e).

### Tests
apps/api/tests/test_idempotency.py
- `test_validate_key` param: "" / 256 chars / "\x7f" / "é" rejected; "a", 255 chars, "with space" accepted.
- `test_canonical_body_ignores_key_order_and_whitespace`; `test_non_json_body_hashed_raw`;
  `test_hash_includes_method_and_path`.
- `test_replay_returns_identical_status_and_body` — POST /status/201 twice same key → same status+body (n=1 both),
  second has `Idempotent-Replayed: true`, endpoint ran once, replay counter incremented; both families (legacy +
  envelope).
- `test_4xx_is_stored_and_replayed` (route returns 404 JSON).
- `test_different_body_returns_422_key_reused` (envelope + legacy error shape, code IDEMPOTENCY_KEY_REUSED).
- `test_same_key_different_path_is_reused`.
- `test_in_progress_returns_409` — gate endpoint on an `asyncio.Event`; fire first request, wait until claimed, fire
  second → 409 IDEMPOTENCY_IN_PROGRESS; release first → 200; third → replay.
- `test_concurrent_duplicates_run_endpoint_once` — `asyncio.gather` 10 identical requests: exactly one 2xx-run,
  others 409 or replay; endpoint counter == 1.
- `test_5xx_not_stored` (/status/503 then retry runs again), `test_unhandled_exception_releases_claim` (/boom 500 then
  retry hits /boom again, store has no row), `test_429_not_stored` (calls_send exhausted after claim → retry later
  with refilled clock succeeds), `test_non_json_response_not_stored`.
- `test_validation_error_400_is_stored` (spec: 4xx stored).
- `test_auth_failure_and_forbidden_create_no_row`; `test_invalid_key_400_invalid_input`.
- `test_get_and_no_header_ignore_idempotency`; `test_keys_are_per_org` (same key two orgs both run).
- `test_lock_expiry_allows_takeover` (FrozenClock.advance(61) with abandoned claim in store → claim succeeds).
- `test_expired_row_reclaimed_after_24h`.
- `test_store_failure_still_returns_response` (fail_complete → 200 delivered, warning logged, row released).
- `test_replay_has_rate_limit_and_request_id_headers`.
- `test_cors_exposes_idempotent_replayed` (extend test_cors).
packages/va_db/tests/test_idempotency_repo_integration.py (migrated_url + Database fixture like
test_api_key_lookup_integration; uses tenant_session)
- claim → Claim; complete → replay returns status/body; hash mismatch → KEY_REUSED; locked → IN_PROGRESS;
  lock expired → Claim; created_at older than 24 h → Claim with response cleared; release deletes only unfinished
  rows; `test_concurrent_claims_one_winner` (two tenant sessions, asyncio.gather → exactly one Claim, other
  IN_PROGRESS); `test_rows_isolated_by_org` (org B same key → Claim; org B session cannot see org A row).
- `DbIdempotencyStore` exercised through one api-level integration? Not needed if va_db covers repo and api covers
  store Protocol; but DbIdempotencyStore needs coverage: add `apps/api/tests/test_idempotency_db.py` only if apps/api
  tests already have a Postgres fixture; otherwise cover DbIdempotencyStore in the va_db integration file is wrong
  layering → instead unit-test it with a tiny fake Database whose tenant_session yields a session from the va_db
  test? Simplest: check how `db_lookup(database)` in api_key_auth is covered and mirror it.

### Gates
`make fmt lint typecheck placeholders test` (+ jscpd, radon, pip-audit; openapi drift check — handler/middleware
should not change the doc). No TS/e2e.

### Risks
- Import cycle va_api.auth.dependencies → va_api.idempotency (keep idempotency free of va_api.auth runtime imports).
- FastAPI body caching: dependency `await request.body()` must reuse the cached body (same Request object in deps);
  verify a body-model route still parses after the dependency read it.
- jsonb reorders keys → tests compare parsed JSON.
- ExceptionMiddleware renders AppError/validation inside our middleware (seen and stored); unhandled exceptions
  propagate through it (release then re-raise).
- Concurrency test on fake store must be deterministic (asyncio.Event gates, no sleeps).
- test_auth_dependencies route-discovery list must include the new test routes.

## 2026-10-29 Builder: P02-T03 build notes

Implemented per plan. Details/deviations:
- `va_db.repositories.idempotency`: `IdempotencyRepo` (tenant) + pure `resolve_existing(row, hash, now)` returning
  Replay/Conflict/`Takeover.EXPIRED|UNLOCKED`; the test `MemoryIdempotencyStore` reuses it (jscpd flagged the copied
  decision chain). Fresh claims write `response_body = null()` (SQL NULL, not JSON null). Lock boundary: held while
  `locked_until > now` (re-claimable at exactly `locked_until`). Retention rule now uses `REPLAY_WINDOW`.
- `va_api.middleware.HttpMiddleware` base (http-only passthrough) now used by `RateLimitHeadersMiddleware` and
  `IdempotencyMiddleware` (jscpd clone of the scope-type preamble).
- Test support split: `idempotency_store.py` (memory store, imported by auth_helpers for `AuthContainer.idempotency`)
  and `idempotency_helpers.py` (separate `/v1/calls/_idem` + `/v1/knowledge/_idem` routers, `IdempotencyContainer`),
  so the auth route-discovery list is unchanged.
- `DbIdempotencyStore` covered by `apps/api/tests/test_idempotency_db.py` (own module-scoped Postgres container via
  va_db support helpers, timeout 180).
- Uncovered: repo's retry when the row is deleted between the conflicting insert and the select (race only).
Gates: make fmt/lint/typecheck (246 files)/placeholders (0) green; make test 2041 passed, 99.80 %; jscpd 0 clones;
radon avg A (2.53); pip-audit + pnpm audit clean; openapi current; spec refs 0 problems.

## Review P02-T03 (2026-10-29): approved
Re-ran make lint typecheck placeholders (green, mypy 246 files, 0 placeholder findings) and make test (2041 passed,
TOTAL 99.80 %). Checked against specs/04 §12, specs/05 header row, specs/31 (IDEMPOTENCY_KEY_REUSED 422,
IDEMPOTENCY_IN_PROGRESS 409, INVALID_INPUT 400 with field), specs/27 (`va_idempotent_replays_total`), retention
rule 24 h via REPLAY_WINDOW. Explicit org_id filters + tenant_session on every repo statement; key never logged;
large bodies hashed/parsed in to_thread; tests use events/FrozenClock, no sleeps. Non-blocking note for later:
complete/release do not check that the caller still holds the lock, so a request running past the 60 s LOCK_TTL
whose claim was taken over can overwrite or release the newer claim (same hash, so the stored response stays a
valid one; the worst case is a second concurrent run). Revisit if long-running POSTs appear.

## 2026-10-29 Reviewer: P02-T03 approved → next P02-T04

## 2026-10-29 Planner: P02-T03 approved → P02-T04 split into T04a/T04b/T04c

P02-T04 as written needs: password primitives + breached list, SMTP/in-memory email, identity repos for the global
tables (users/sessions/email_tokens have no org_id and no RLS; no access path exists yet), an `org_data_keys`
`DataKeyStore` (none exists, only a test fake) plus `Envelope` in the api container (signup creates an org, whose
`webhook_signing_secret_enc` is NOT NULL and envelope-encrypted), org provisioning (shared later by T09 sub-accounts
and T14 seed), and then the endpoints, session principal, CSRF, throttling. Too much for one Builder pass, so the
plan file now has T04a (primitives), T04b (repos + provisioning + Envelope), T04c (endpoints/session/CSRF).
T04 parent is ticked once all three are ticked. Plan for T04a below.

## Plan P02-T04a — Password, token, and email primitives (specs/04 §4.2, specs/02 §6, specs/28 §2)

### Decisions to record (same commit, `> Decision (2026-10-29):`)
In specs/04 §4.2:
1. **Breached list source**: SecLists renamed the file; the source is
   `Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt` (same xato top-100 000 list) fetched from
   `https://raw.githubusercontent.com/danielmiessler/SecLists/<commit sha>/...` pinned to a commit SHA (Builder finds
   the current SHA via `curl -s https://api.github.com/repos/danielmiessler/SecLists/commits?path=Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt&per_page=1`)
   and verified against a pinned sha256 of the downloaded file in the script. (raw.githubusercontent was checked
   2026-10-29: the old name 404s, the xato name returns 200.)
2. **List normalisation**: entries are stripped, lower-cased (NFKC), deduplicated, sorted, blank lines dropped; the
   check is case-insensitive (`password.casefold()` NFKC-normalised in the set). Stored gzipped (`mtime=0` so the
   build is reproducible) as `packages/va_common/src/va_common/auth/data/common_passwords.txt.gz`, one per line.
   Loaded lazily once per process (`functools.cache`), parsing done via `asyncio.to_thread` by callers at startup is
   not required: the loader is sync and called inside `to_thread` by `check_password_policy_async` (see below).
3. **Length** counted in Unicode code points after NFKC normalisation; 12–128 inclusive.
4. **Email local-part rule**: local part = text before the last `@`, with any `+tag` suffix removed, casefolded;
   applied only when it has at least 3 characters (a 1–2 char local part would reject most passwords); a password
   whose casefold contains it is rejected.
5. **Rule order and messages** (`AppError("PASSWORD_TOO_WEAK", msg, field="password")`, first failing rule wins):
   too short "Password must be at least 12 characters." / too long "Password must be at most 128 characters." /
   breached "Password is too common; choose a less predictable one." / local part "Password must not contain
   your email address."
6. **Hashing runs off the event loop**: `PasswordHasher.hash_async/verify_async/` wrap the argon2 calls in
   `asyncio.to_thread` (64 MiB × t=3 is ~50–100 ms CPU, rule 7). `verify` returns False for mismatch, for a
   malformed hash, and for `None` hash (SSO-only user) — the `None` case still performs one dummy argon2 verify
   against a module-level precomputed hash so login timing does not reveal SSO-only/unknown users.
7. **Opaque tokens** (sessions, CSRF, email tokens, later invitations/reset): `secrets.token_bytes(32)` base64url
   without padding (43 chars); stored hash = existing `va_common.auth.sha256_hex(token)`.
In specs/02 §6 (under the table, Decision): `SMTP_URL` schemes — `smtp://` (plain, STARTTLS used when the server
offers it; allowed only when `VA_ENV` is `local`/`test`), `smtp+starttls://` (STARTTLS required), `smtps://`
(implicit TLS); userinfo (percent-decoded) are the login credentials; default ports 25 / 587 / 465. Connect and
command timeout 10 s. Production refuses `smtp://` at settings validation.

### Files

**packages/va_common/src/va_common/auth/** (convert module → package; `git mv auth.py auth/api_keys.py`)
- `auth/__init__.py`: docstring summarising the package; re-export everything that `va_common.auth` exports today
  (unchanged names: API_KEY_LENGTH … sha256_hex) plus the new public names below. All existing imports
  (`from va_common.auth import ApiKeyHasher, KeyEnvironment, Pepper, ...`) keep working; no caller edits needed.
- `auth/api_keys.py`: the current file verbatim (docstring adjusted to say it is the api-key module).
- `auth/passwords.py`:
  - `@dataclass(frozen=True, slots=True) class Argon2Params: time_cost: int; memory_cost: int; parallelism: int;
    hash_len: int; salt_len: int`
  - `SPEC_ARGON2_PARAMS: Final = Argon2Params(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)`
  - `MIN_PASSWORD_LENGTH: Final = 12`, `MAX_PASSWORD_LENGTH: Final = 128`, `MIN_LOCAL_PART_CHECK: Final = 3`
  - `class PasswordHasher:` `__init__(self, params: Argon2Params = SPEC_ARGON2_PARAMS)` wrapping
    `argon2.PasswordHasher(type=Type.ID, ...)`; `params` property; `hash(password) -> str`;
    `verify(password_hash: str | None, password: str) -> bool` (catches `VerifyMismatchError`,
    `VerificationError`, `InvalidHashError`; `None` → dummy verify then False); `needs_rehash(password_hash) -> bool`
    (`check_needs_rehash`; malformed → True); `async hash_async(...)`, `async verify_async(...)` via
    `asyncio.to_thread`. Dummy hash computed once per hasher instance lazily (`functools.cached_property` is
    incompatible with slots → plain attribute set in `__init__` from `self._hasher.hash(<random>)`; that costs one
    hash at construction — acceptable, document it).
  - `def password_policy_violation(password: str, *, email: str | None) -> str | None` → message or None (pure,
    loads list via `common_passwords()`).
  - `def check_password_policy(password: str, *, email: str | None) -> None` → raises the AppError above.
  - `async def check_password_policy_async(password, *, email)` → `to_thread(check_password_policy, ...)` (first call
    loads/decompresses the list).
  - `@functools.cache def common_passwords() -> frozenset[str]` reading the gz via
    `importlib.resources.files("va_common.auth").joinpath("data/common_passwords.txt.gz")`.
  - `def normalise_password(value: str) -> str` (NFKC) shared by script + check.
- `auth/tokens.py`: `TOKEN_BYTES: Final = 32`; `def generate_token() -> str` (base64url, no padding);
  `@dataclass(frozen=True, slots=True) class IssuedToken: plaintext: str = field(repr=False); token_hash: str`;
  `def issue_token() -> IssuedToken` (hash = `sha256_hex`); `def tokens_equal(a: str, b: str) -> bool`
  (`hmac.compare_digest` on UTF-8 bytes; used for CSRF in T04c).
- `auth/data/common_passwords.txt.gz` (generated, committed). Make sure uv_build includes non-.py package data
  (check the built wheel or `importlib.resources` in tests — uv_build includes all files under the module dir by
  default; verify with a test that loads it from the installed package).

**packages/va_common/src/va_common/email/** (new package)
- `email/__init__.py` re-exports.
- `email/message.py`: `@dataclass(frozen=True, slots=True) class EmailMessage: to: str; subject: str; text: str;
  html: str; template: str` + `def to_mime(message, *, sender: str) -> email.message.EmailMessage`
  (stdlib; multipart/alternative text+html, `Message-ID` via `email.utils.make_msgid(domain=<sender domain>)`,
  `Date` via `email.utils.format_datetime(clock.now())` — take `now: datetime` param).
- `email/sender.py`:
  - `class EmailSender(Protocol): async def send(self, message: EmailMessage) -> None`
  - `class EmailDeliveryError(Exception)` (message never includes recipient address or body).
  - `class SmtpEmailSender:` `__init__(self, config: SmtpConfig, *, sender: str, clock: Clock | None = None)`;
    `send` builds MIME and calls `aiosmtplib.send(mime, hostname=…, port=…, username=…, password=…,
    use_tls=…, start_tls=…, timeout=SMTP_TIMEOUT_S)`; maps `aiosmtplib.SMTPException`, `OSError`,
    `TimeoutError` → `EmailDeliveryError`; logs `email_sent` / `email_send_failed` with `template` and masked
    recipient (`mask_email`, add to `va_common.masking`: first char + `***@` + domain).
  - `@dataclass(frozen=True, slots=True) class SmtpConfig: hostname; port; username: str | None;
    password: SecretStr | None (repr hidden); use_tls: bool; start_tls: bool | None` +
    `def parse_smtp_url(url: str) -> SmtpConfig` (ValueError on unknown scheme / missing host).
  - `class InMemoryEmailSender:` `sent: list[EmailMessage]`; `send` appends (async); `def to(self, address) ->
    list[EmailMessage]`; `def clear()`. Lives in production code (spec: "in-memory implementation for tests";
    used by the api in `VA_ENV=test` later), so it is not test-only scaffolding.
- `email/templates.py`: no jinja2 (specs/02 §3 limits it to prompt templates). `string.Template` bodies with every
  substituted value passed through `html.escape` for the HTML part. `def verify_email(*, name: str, link: str,
  to: str) -> EmailMessage` (subject "Verify your VoiceAgents email address"; text + HTML; states the link expires
  in 24 hours; template name `"verify_email"`). A `TEMPLATES: Final[frozenset[str]]` listing names for the test.
  Real copy (no lorem), plain transactional voice.
- `va_common/settings.py`: `class EmailSettings(BaseServiceSettings)`: `smtp_url: str = "smtp://localhost:1025"`,
  `email_from: str = "VoiceAgents <no-reply@voiceagents.local>"`; validators: `parse_smtp_url` succeeds; production
  refuses `smtp://`; `email_from` parses with `email.utils.parseaddr` to an address containing `@`.
  `def smtp_config(self) -> SmtpConfig`. Export in `__all__`.
- `apps/api/src/va_api/settings.py`: `ApiSettings(RedisSettings, DatabaseSettings, TemporalSettings, EmailSettings)`
  (no container wiring yet — T04c wires the sender; keep T04a free of api behaviour changes beyond settings).
  Check `apps/worker` settings: specs/02 §6 scopes SMTP_URL to api, worker → add `EmailSettings` to the worker
  settings class too if it exists (grep `class .*Settings` in apps/worker).

**packages/va_common/pyproject.toml**: add `argon2-cffi>=25.1.0`, `aiosmtplib>=<latest>` (uv add); root dev
group: `aiosmtpd` (test SMTP server). Run `uv lock`; pip-audit must stay clean.

**scripts/build_password_list.py** (new, typer-free plain argparse like other scripts — check `scripts/fetch_models.py`
style): constants `SOURCE_URL` (pinned SHA), `SOURCE_SHA256`; downloads with httpx (timeout 30 s), verifies sha256,
normalises (decision 2), writes gz with `gzip.GzipFile(..., mtime=0)`; `--source PATH` option to build from a local
file (used by its test, no network); `main(argv) -> int`. Prints entry count. Add `make passwords` target? Not
needed; document the command in AGENTS.md.
**scripts/tests/test_build_password_list.py**: builds from a small temp source (dupes, case, blanks, NFKC
fullwidth chars) → asserts content and byte-identical output across two runs; sha mismatch → non-zero exit
(monkeypatch download fn to return bytes).

### Tests
packages/va_common/tests/test_passwords.py
- `test_spec_argon2_params` (encoded hash starts `$argon2id$v=19$m=65536,t=3,p=4$`; salt 16 bytes / hash 32 bytes
  decoded from the encoded string).
- `test_hash_verify_roundtrip`, `test_verify_wrong_password_false`, `test_verify_malformed_hash_false`,
  `test_verify_none_hash_runs_dummy_verify` (spy on inner argon2 verify called once, returns False).
- `test_needs_rehash_on_param_change` (hash with cheap params, hasher with spec params → True; same → False;
  malformed → True).
- `test_async_wrappers_run_in_thread` (patch `asyncio.to_thread` spy or assert thread id differs).
- Unit tests elsewhere should use cheap `Argon2Params(time_cost=1, memory_cost=8, parallelism=1, ...)` for speed
  except the spec-param test.
- policy param table: 11 chars / 12 ok / 128 ok / 129 / "password1234" breached (assert it is in the list) /
  case variant "PASSWORD1234" breached / contains local part "jane.doe" for jane.doe+x@ex.com / local part of 2
  chars ignored / message and field asserted; `test_common_passwords_size` (≥ 90 000 entries, all lower-case,
  sorted file) loaded via importlib.resources.
packages/va_common/tests/test_tokens.py — length 43, url-safe alphabet, uniqueness over 1 000, hash = sha256_hex,
  IssuedToken repr hides plaintext, `tokens_equal`.
packages/va_common/tests/test_email.py
- `parse_smtp_url` table (smtp/smtps/smtp+starttls, default ports, percent-decoded creds, bad scheme, no host).
- `EmailSettings` production refuses `smtp://`; bad `EMAIL_FROM` rejected.
- `verify_email` template: escapes `<script>` in name in HTML, link present in both parts, subject.
- `to_mime`: multipart/alternative, From/To/Subject/Date/Message-ID.
- `InMemoryEmailSender` records and filters by recipient.
- `test_smtp_sender_delivers` — aiosmtpd `Controller` on 127.0.0.1 random port with a handler collecting
  envelopes (run in a fixture; Controller uses its own thread — fine); asserts recipient, subject, both parts.
- `test_smtp_sender_auth_and_starttls` only if aiosmtpd TLS setup is cheap; otherwise assert the kwargs passed to
  `aiosmtplib.send` via monkeypatch for smtps/starttls/credentials.
- `test_smtp_failure_raises_delivery_error_without_pii` (connection refused port → EmailDeliveryError, message has
  no address; log line has masked recipient).
- `mask_email` tests in test_masking.
apps/api/tests/test_settings_auth.py (or new test) — ApiSettings exposes `smtp_config()`.

### Gates
`make fmt lint typecheck placeholders test` + jscpd, radon, pip-audit, check_spec_refs, openapi drift (unchanged).

### Risks
- uv_build package-data: confirm the `.gz` ships (test loads via importlib.resources; also `uv build` the
  va_common wheel and `unzip -l` once, note result in build notes).
- Placeholder checker may flag words inside the password list? It scans source files — confirm it skips `.gz`
  (binary); also gitleaks may flag the list (gz is binary, should be skipped; verify `make check` secret scan).
- The real argon2 params cost ~64 MiB per hash; with xdist 8 workers keep spec-param hashing to a handful of tests.
- `va_common.auth` module→package move: stale `__pycache__/auth.cpython-312.pyc` is harmless, but make sure no
  `auth.py` remains beside `auth/`.
- Memory mem-1793257682-de8e (env var tests) applies only if specs/02 §6 rows change — they do not (SMTP_URL and
  EMAIL_FROM already exist); the Decision note is prose only. Re-run scripts/tests/test_env_example.py anyway.

## 2026-10-29 Builder: P02-T04a build notes
Implemented per plan. Deviations (deliberate):
- `EmailSettings` lives in `va_common/email/settings.py` (not `va_common/settings.py`): settings → email → sender →
  va_common.logging → va_common.settings would be an import cycle. Exported from `va_common.email`.
- `SMTP_URL` is a `SecretStr` (specs/28 §2 lists it as a platform secret; it carries credentials).
- `SmtpConfig.password` is `str | None` with `field(repr=False)` instead of pydantic SecretStr (plain dataclass);
  SmtpConfig also carries `scheme` (used by the production `smtp://` refusal).
- SMTP parsing in `email/config.py`; `EmailDeliveryError` raised `from None` (aiosmtplib messages can contain the
  recipient) with error type + SMTP code only; log has masked recipient, error_type, smtp_code.
- `breached_list_entry()` (NFKC + casefold) is shared by the policy check and the build script.
- Breached list: SecLists commit c205c36a445bff37f8e58a9ec829105cd4975c58, source sha256 1472aafa…388b, 100 000
  lines → 96 517 normalised entries, gz 253 340 bytes; two builds byte-identical. `uv build` wheel contains
  `va_common/auth/data/common_passwords.txt.gz` (checked with unzip -l).
Decisions written: specs/04 §4.2 (`> Decision (2026-10-29)`), specs/02 §6 (SMTP_URL schemes). No env rows changed
(test_env_example 43 passed). AGENTS.md: two bullets.
Gates: make fmt 0; make lint 0; make typecheck 0 (261 files); make placeholders 0 findings; make test 2124 passed,
TOTAL 99.81 % (new modules all 100 %); jscpd 0 clones; radon avg A (2.47); pip-audit + pnpm audit clean;
check_spec_refs 0 problems; openapi current; gitleaks passed.

## Review P02-T04a (2026-10-29): approved
Re-ran: make lint 0, make typecheck 0, make placeholders 0 findings, make test 2124 passed (99.81 %).
Diff checked against specs/04 §4.2 (argon2id t=3/m=65536/p=4/hash 32/salt 16, rehash detection, 12–128, breached
list path, local-part rule), specs/31 PASSWORD_TOO_WEAK 400 with field, specs/02 §6 SMTP_URL/EMAIL_FROM defaults
unchanged, specs/28 §2 SMTP_URL as secret. No PII in EmailDeliveryError (raised from None) or logs (mask_email).
Blocking argon2/list load offloaded via to_thread. No findings. Next: P02-T04b.

## 2026-10-29 Planner: P02-T04a approved → next P02-T04b

## Plan P02-T04b — Identity repositories and org provisioning (specs/04 §4.1, §4.3; specs/28 §3; specs/03 §2, §3, §10)

### Key facts found while planning
- `orgs`, `users`, `sessions`, `email_tokens` have no `org_id` column → no RLS; `va_app` has DML grants on them.
  `memberships`, `org_data_keys`, `invitations` have NOT NULL `org_id` → RLS (`tenant_isolation`).
- `va_common.crypto.DataKeyStore` is only a Protocol; no implementation exists yet (specs/28 Decision (d) said
  "implemented over org_data_keys in phase 01" — it was not). `Envelope.encrypt` reads the active key through the
  store, which cannot see the uncommitted key row inside the provisioning transaction.
- `ApiSettings` does not yet extend `CryptoSettings`, and `CryptoSettings` rejects an empty `LOCAL_MASTER_KEY` at
  construction, while `va_api.main` builds `ApiSettings()` at import with no environment (`app = create_app()`).
- Listing a user's memberships across orgs (needed by T04c login/`/internal/auth/session`) cannot run under RLS
  before an org is chosen → needs a `va_system` lookup repo (spec list + test count change).

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. specs/03 §2 (append new Decision under the existing ApiKeyLookupRepo one, and add `MembershipLookupRepo` to the
   backticked `va_system` bullet list so `_spec_system_repos()` sees it): "**Global identity tables** (`orgs`,
   `users`, `sessions`, `email_tokens`) have no `org_id` and no RLS; code reaches them through
   `Database.global_session()` (a `va_app` transaction with `app.org_id` unset, so every RLS table returns no rows and
   rejects writes) or through a `tenant_session` when it also writes tenant rows in the same transaction.
   `MembershipLookupRepo` joins the `va_system` list because a dashboard user's memberships span orgs and are read
   before an active org exists: `for_user(user_id)` returns `MembershipRecord`s (membership id, role, org id, name,
   slug, plan_id, status, parent_org_id) ordered by membership `created_at`, id."
2. specs/04 §4.1 (Decision under the table): "**Org provisioning** (`va_db.provisioning.OrgProvisioner`, used by
   signup, `POST /v1/orgs/create`, and seeding) creates in one `tenant_session(new_org_id)` transaction: the org row,
   its `active` `org_data_keys` row, a webhook signing secret `whsec_` + 43-char opaque token encrypted with AAD
   `orgs.webhook_signing_secret_enc:<org_id>`, and (when an owner is given) the owner user (new or existing) plus an
   `owner` membership; any failure rolls everything back. **Slug** = org name NFKD-folded to ASCII, lower-cased,
   runs of non-`[a-z0-9]` replaced by `-`, trimmed of `-`, cut to 40 characters (re-trimmed), `org` when empty, then
   `-` + 4 random characters from `[a-z0-9]`; on a slug collision a new suffix is drawn, up to 5 attempts, then
   `CONFLICT`. **Duplicate user email** raises `CONFLICT` with `field: "email"`, message "An account with this email
   already exists." Email addresses are stored as given after `strip()` (the column is `citext`)."
3. specs/28 §3 (new Decision after the 2026-10-25 one): "(h) `OrgDataKeyStore` in `va_db.repositories.data_keys`
   implements `DataKeyStore` over `org_data_keys` (`status='active'`) on a `tenant_session(org_id)`;
   `Envelope.encrypt_with_key(org_id, wrapped, plaintext, aad)` seals under an explicit wrapped key so a transaction
   that has just created a data key can encrypt before committing. (i) `LOCAL_MASTER_KEY` may be empty at settings
   construction (so the api imports without environment, like `API_KEY_PEPPER`); a non-empty value must be 64 hex
   characters, production still refuses `local`, and `open_kms_provider` raises `RuntimeError("LOCAL_MASTER_KEY is
   not configured")` before opening anything when it is empty." Also adjust (g) wording only by referencing (i).
   The platform (nil org) key has no `org_data_keys` row (FK to `orgs`); `platform_encrypt` stays unused until a
   later task introduces a platform key source — state that in (h).

### Files

**packages/va_common/src/va_common/crypto/envelope.py**
- `async def encrypt_with_key(self, org_id: UUID, wrapped: WrappedDataKey, plaintext: bytes, aad: str) -> bytes`
  → `return await self._seal(org_id, wrapped, plaintext, aad)`; docstring per decision 3(h). Plus
  `encrypt_str_with_key(...)` is NOT needed (callers `.encode()`); keep API minimal.

**packages/va_common/src/va_common/settings.py** — `CryptoSettings._check_kms_provider`: local branch rejects a
non-empty key that is not 64 hex (message unchanged otherwise: "LOCAL_MASTER_KEY must be 64 hex characters (32 bytes)
when set"); empty allowed. Production refusal stays first for local.
**packages/va_common/src/va_common/crypto/kms.py** — `open_kms_provider`: local branch raises
`RuntimeError("LOCAL_MASTER_KEY is not configured")` when the secret is empty.
Update `packages/va_common/tests/test_settings.py` / `test_crypto_kms.py` cases accordingly (empty accepted at
construction; bad hex rejected; `open_kms_provider` with empty → RuntimeError).

**packages/va_db/src/va_db/session.py**
- `Database.global_session()` asynccontextmanager: `async with self._app_sessions() as session, session.begin():
  yield session` (no set_config). Module-level helper `global_session()` mirroring the existing `tenant_session`/
  `system_session` helpers (check how they are defined at the bottom of the file and follow the same pattern); add
  to `__all__`. Docstring: only for `orgs`, `users`, `sessions`, `email_tokens`.

**packages/va_db/src/va_db/repositories/data_keys.py** (new)
- `class OrgDataKeyRepo:` `__init__(self, session: AsyncSession)` (tenant session);
  `async def active(self, org_id) -> WrappedDataKey | None` (select kek_id, encrypted_data_key where org_id and
  status='active'; explicit org_id filter + RLS); `async def add_active(self, org_id, wrapped) -> OrgDataKey`
  (insert `status='active'`, id `new_id()`, flush; unique partial index violation → `AppError("CONFLICT", ...)`
  inside a savepoint).
- `class OrgDataKeyStore:` `__init__(self, database: Database)`; `async def active_data_key(self, org_id) ->
  WrappedDataKey | None` → `async with database.tenant_session(org_id) as s: return await OrgDataKeyRepo(s).active(org_id)`.
  Returns None for `NIL_ORG_ID` without a query (no row can exist). Satisfies `va_common.crypto.DataKeyStore`
  (assert structurally in a test: `store: DataKeyStore = OrgDataKeyStore(db)` typechecks).

**packages/va_db/src/va_db/repositories/identity.py** (new; no RLS tables only, works on global or tenant session)
- `@dataclass(frozen=True, slots=True) class NewUser: email: str; name: str = ""; password_hash: str | None = None;
  email_verified_at: datetime | None = None` (`password_hash` `field(repr=False)`).
- `@dataclass(frozen=True, slots=True) class IssuedSession: token: str = field(repr=False); session: Session`.
- `class IdentityRepo:` `__init__(self, session: AsyncSession)`:
  - Users: `create_user(new: NewUser) -> User` (id new_id(), email stripped; unique violation in a savepoint →
    `AppError("CONFLICT", "An account with this email already exists.", field="email")`);
    `get_user(user_id) -> User | None`; `get_user_by_email(email) -> User | None` (`email.strip()`, citext compare);
    `mark_email_verified(user_id, now) -> None` (only sets when NULL: `UPDATE ... WHERE email_verified_at IS NULL`);
    `set_password_hash(user_id, password_hash) -> None`; `record_login(user_id, now) -> None` (`last_login_at`).
  - Sessions (token never stored; hash = `va_common.auth.sha256_hex`):
    `create_session(*, user_id, active_org_id: UUID | None, expires_at: datetime, ip: str | None, user_agent:
    str | None, mfa_passed: bool = False) -> IssuedSession` (uses `va_common.auth.issue_token()`);
    `get_active_session(token: str, now) -> Session | None` (by hash; `revoked_at IS NULL AND expires_at > now`);
    `extend_session(session_id, expires_at) -> None`; `set_active_org(session_id, org_id) -> None`;
    `set_mfa_passed(session_id) -> None`; `revoke_session(session_id, now) -> bool` (only unrevoked; returns whether
    a row changed); `revoke_user_sessions(user_id, now, *, except_session_id: UUID | None = None) -> list[str]`
    returning the revoked rows' `token_hash`es (T04c deletes `sess:{hash}` cache keys from them).
    `ip` stored as given string (asyncpg casts to inet; validate with `ipaddress.ip_address` → ValueError early).
  - Email tokens: `issue_email_token(*, user_id, purpose: EmailTokenPurpose, now, ttl: timedelta) -> IssuedToken`;
    `consume_email_token(token: str, purpose, now) -> UUID | None` — single atomic
    `UPDATE email_tokens SET used_at = :now WHERE token_hash = :h AND purpose = :p AND used_at IS NULL AND
    expires_at > :now RETURNING user_id` (returns user id; None for unknown/used/expired/wrong purpose — the router
    maps None to `TOKEN_EXPIRED`); `count_issued_since(user_id, purpose, since) -> int` (resend limit 3/h in T04c).
  - Constants exported: `EMAIL_VERIFY_TTL: Final = timedelta(hours=24)`, `PASSWORD_RESET_TTL: Final = timedelta(hours=1)`
    (spec §4.1 values; session lifetimes stay in T04c).

**packages/va_db/src/va_db/repositories/memberships.py** (new)
- `@dataclass(frozen=True, slots=True) class MembershipRecord: membership_id, role: MemberRole, org_id, org_name,
  org_slug, plan_id, org_status: OrgStatus, parent_org_id: UUID | None`.
- `class MembershipLookupRepo(SystemRepository):` (`system_scope = True`, comment citing specs/03 §2)
  `async def for_user(self, user_id) -> list[MembershipRecord]` (join orgs; order by memberships.created_at, id).
- `class MembershipRepo:` tenant: `__init__(session, org_id)`; `add(user_id, role) -> Membership` (unique
  (org_id,user_id) → `CONFLICT` in savepoint, message "User is already a member of this org.");
  `get_for_user(user_id) -> Membership | None` (explicit org_id filter).
  (Do not build member list/role-change endpoints — that is T08.)

**packages/va_db/src/va_db/provisioning.py** (new)
- `SLUG_MAX_BASE: Final = 40`, `SLUG_SUFFIX_LEN: Final = 4`, `SLUG_ATTEMPTS: Final = 5`,
  `WEBHOOK_SECRET_PREFIX: Final = "whsec_"`, `WEBHOOK_SECRET_AAD_COLUMN = ("orgs", "webhook_signing_secret_enc")`.
- `def slug_base(name: str) -> str` (pure, decision 2); `def make_slug(name: str, *, suffix: str | None = None) -> str`
  (suffix from `secrets.choice` over `string.ascii_lowercase + string.digits` when None).
- `def webhook_secret_aad(org_id) -> str` → `column_aad("orgs", "webhook_signing_secret_enc", org_id)`.
- `@dataclass(frozen=True, slots=True) class ProvisionedOrg: org: Org; owner: User | None; membership: Membership |
  None; webhook_signing_secret: str = field(repr=False)` (returned so `/v1/orgs/create`/seed can show nothing or use
  it; never logged).
- `class OrgProvisioner:` `__init__(self, database: Database, envelope: Envelope)`;
  - `async def provision(self, *, name: str, owner: UUID | NewUser | None, plan_id: str = "start",
    parent_org_id: UUID | None = None) -> ProvisionedOrg`: `org_id = new_id()`; `async with
    database.tenant_session(org_id) as s: return await self.provision_in(s, org_id, ...)`.
  - `async def provision_in(self, session, org_id, *, name, owner, plan_id="start", parent_org_id=None) ->
    ProvisionedOrg` — caller guarantees `session` is `tenant_session(org_id)` (T04c signup uses this to also write the
    session row and verify token in the same transaction). Steps, flushing explicitly in this order (FKs):
    1. `wrapped = await envelope.create_data_key(org_id)`; secret = `WEBHOOK_SECRET_PREFIX + generate_token()`;
       `enc = await envelope.encrypt_with_key(org_id, wrapped, secret.encode(), webhook_secret_aad(org_id))`.
    2. Org insert in a savepoint (`session.begin_nested()`, `session.add` INSIDE the block per memory
       mem-1793133155-6419) with `make_slug(name)`; on slug unique violation retry with a new suffix up to
       `SLUG_ATTEMPTS`, then `AppError("CONFLICT", "Could not allocate a unique org slug.")`. Name is `strip()`ed;
       empty name → `AppError("INVALID_INPUT", "Org name must not be empty.", field="org_name")` (specs/31 code).
    3. `OrgDataKeyRepo(session).add_active(org_id, wrapped)`.
    4. Owner: `NewUser` → `IdentityRepo(session).create_user(...)`; `UUID` → `get_user` (missing →
       `NotFoundError("user")`); then `MembershipRepo(session, org_id).add(user.id, "owner")`.
  - `async def webhook_signing_secret(self, org: Org) -> str` → `envelope.decrypt_str(org.id,
    org.webhook_signing_secret_enc, webhook_secret_aad(org.id))` (used by the acceptance test and later webhooks).
- Export from `va_db.repositories.__init__` (new repos, `NewUser`, `IssuedSession`, `MembershipRecord`,
  `EMAIL_VERIFY_TTL`, `PASSWORD_RESET_TTL`) and update its docstring; `va_db.provisioning` is its own module.

**apps/api/src/va_api/settings.py** — `ApiSettings(RedisSettings, DatabaseSettings, TemporalSettings,
EmailSettings, CryptoSettings)`; docstring mentions `LOCAL_MASTER_KEY` like the pepper.
**apps/api/src/va_api/container.py** — inside the stack: `provider = await stack.enter_async_context(
open_kms_provider(settings))` (raises RuntimeError before opening when key empty — check ordering: call a cheap
pre-check alongside `settings.api_key_hasher()` so nothing opens; simplest: add `CryptoSettings.require_kms_configured()`
method used first, or open the provider BEFORE `open_dependencies()` in the same AsyncExitStack); `envelope =
Envelope(provider, OrgDataKeyStore(database))`; `provisioner = OrgProvisioner(database, envelope)`; Container gains
fields `envelope: Envelope`, `provisioner: OrgProvisioner`. Keep the constructor call readable (keyword args).
**apps/api/tests/support/api_helpers.py** — `make_settings` adds `local_master_key=secrets.token_hex(32)` default.
Check `apps/worker` / `apps/media_worker` settings: do NOT add CryptoSettings there in this task (no consumers yet).
**AGENTS.md** — bullets: `Database.global_session()` for identity tables; `provision_in` for same-txn signup;
`LOCAL_MASTER_KEY` required by the api container.

### Tests
packages/va_common/tests: `test_encrypt_with_key_decrypts_with_store_less_envelope` (create_data_key, encrypt_with_key,
then decrypt through an Envelope whose store returns None → still decrypts, embedded key); settings/kms updates above.

packages/va_db/tests/test_identity_repo_integration.py (migrated_url, integration mark; reuse tenant_rows.open_database,
model_factories; check model_factories for make_org/make_user helpers and reuse them):
- `test_create_user_and_lookup_case_insensitive` (create "Jane@Example.org" → get_user_by_email("jane@example.ORG"));
  use `example.org` only in tests (placeholder checker flags example.com in production docstrings only).
- `test_create_user_duplicate_email_conflict` (AppError CONFLICT, field email; session still usable after).
- `test_session_stores_only_hash` (row.token_hash == sha256_hex(issued.token), token not in any column: select all
  text columns and assert plaintext absent); `test_get_active_session_excludes_expired_and_revoked`;
  `test_extend_and_set_active_org_and_mfa`; `test_revoke_user_sessions_except_current_returns_hashes`.
- `test_email_token_hash_only_and_single_use` (consume → user id, second consume → None);
  `test_email_token_expired_or_wrong_purpose_none`; `test_count_issued_since`.
- `test_mark_email_verified_keeps_first_timestamp`.
packages/va_db/tests/test_membership_lookup_integration.py — user with memberships in 2 orgs → `for_user` returns both
  in order with org fields; MembershipRepo.add duplicate → CONFLICT.
packages/va_db/tests/test_data_keys_integration.py — `OrgDataKeyStore.active_data_key` returns the active row, None
  for an org without one and for NIL_ORG_ID; second `add_active` → CONFLICT; store under another org's id cannot read
  (RLS + filter).
packages/va_db/tests/test_provisioning.py (unit): `slug_base` table ("Acme Corp" → "acme-corp", "  Ünïcödé Café!! "
  → "unicode-cafe", "日本" → "org", 60-char name cut to 40 without trailing `-`), `make_slug` suffix format regex
  `^[a-z0-9-]+-[a-z0-9]{4}$`, `webhook_secret_aad`.
packages/va_db/tests/test_provisioning_integration.py (use a real `Envelope(LocalKmsProvider.from_hex(token_hex(32)),
OrgDataKeyStore(db))`):
- `test_provision_with_new_owner_creates_all_rows` (org plan 'start', slug pattern, one active data key, owner
  membership role owner, user exists).
- `test_webhook_secret_decrypts_with_its_aad` (`provisioner.webhook_signing_secret(org)` == returned secret, starts
  `whsec_`, len 49; decrypt with another org's AAD → DecryptionError; ciphertext does not contain the secret).
- `test_provision_is_atomic` — duplicate owner email (pre-create the user, pass NewUser with same email) → CONFLICT
  and zero new `orgs` / `org_data_keys` / `memberships` rows (count via system session before/after); also a variant
  that monkeypatches `MembershipRepo.add` to raise → no rows.
- `test_provision_existing_owner_and_parent` (owner UUID, parent_org_id set; missing user id → NOT_FOUND, no rows).
- `test_slug_collision_retries` (monkeypatch suffix generator to return a taken suffix first, then a free one).
- `test_provision_in_shares_caller_transaction` (open tenant_session(org_id), provision_in, then create a session row
  via IdentityRepo in the same txn, raise → nothing persisted).
packages/va_db/tests/test_session_integration.py — add `test_global_session_denies_tenant_tables`: in
  `global_session()` `current_setting('app.org_id')` is NULL, `SELECT count(*) FROM memberships` = 0 while rows exist
  (inserted via system session), INSERT into memberships raises (RLS WITH CHECK), users/orgs visible.
packages/va_db/tests/test_repositories.py — count 7 → 8 in `test_system_repositories_are_exactly_the_spec_list`.
apps/api/tests/test_container.py — container exposes `Envelope` and `OrgProvisioner`; `local_master_key=""` →
  RuntimeError "LOCAL_MASTER_KEY is not configured" with `closed == []`; existing close-order assertion still holds
  (local provider has no close; if order changes, update the assertion and explain in build notes).
apps/api/tests/test_settings_auth.py — `ApiSettings()` without env constructs (import path) and bad-hex
  LOCAL_MASTER_KEY rejected.

### Gates
`make fmt lint typecheck placeholders test` + jscpd (reuse `TenantRepository._savepoint`-style helper instead of
copying the unique-violation savepoint block 3×: factor a module-level `async def conflict_savepoint(session, message,
field=None)` asynccontextmanager in `repositories/base.py` and use it from identity/memberships/data_keys/provisioning),
radon, pip-audit, check_spec_refs, openapi drift (unchanged), gitleaks, scripts/tests/test_env_example.py (no env rows
change; LOCAL_MASTER_KEY default text in specs/02 §6 says "64 hex chars" for dev value — unchanged).

### Risks
- jscpd: the four integration test files will share fixture boilerplate → put a `identity_db` fixture (Database +
  Envelope + provisioner) in packages/va_db/tests/conftest.py or support module once.
- `begin_nested` autoflush: flush pending objects before entering each savepoint, `add` inside it.
- RLS: provisioning inserts `org_data_keys`/`memberships` — only valid because `tenant_session(org_id)` sets the new
  id; `orgs` insert itself is not RLS-protected. Test that `provision_in` with a mismatched session org id fails
  (RLS WITH CHECK) rather than silently writing.
- va_db now imports `va_common.crypto` (cryptography, aiobotocore) — already a va_common dependency, no new deps.
- `CryptoSettings` relaxation touches va_common tests written for P01; update, do not delete, the assertions.
- Flaky Docker timeouts (memories mem-1793129328-3035 family): rerun isolated before suspecting code.

## 2026-10-29 Builder: P02-T04b build notes
Implemented per plan. Deviations (deliberate):
- `global_session` tests live in `test_session_integration.py` on the existing `rls_probe` harness (va_app, NULL
  `app.org_id`, RLS rows invisible, INSERT rejected with "row-level security") plus the module helper and the
  unconfigured-database case in `test_session.py`, instead of memberships on a migrated DB.
- `issue_email_token` writes `created_at=now` from the caller's clock so `count_issued_since` (resend limit in T04c)
  agrees with the injectable Clock rather than DB `now()`.
- `OrgProvisioner._insert_org(session, name=, build=Callable[[slug], Org])`, so the slug retry loop does not take six
  arguments; `provisioning.random_slug_suffix` is the monkeypatch seam for the collision tests.
- `TenantRepository._savepoint` now delegates to the new `conflict_savepoint(session, message, field=)` in
  `repositories/base.py`, which identity/memberships/data_keys/provisioning share.
- `model_factories.make_org` now lets callers override `slug`/`name` (it passed duplicate kwargs before).
- `test_settings_email.test_api_settings_refuse_plain_smtp_in_production` passes `kms_provider="aws"` because ApiSettings
  now includes CryptoSettings, and local KMS is refused first in production.
- Extra tests: empty org name → `INVALID_INPUT` field `org_name`; slug attempts exhausted → CONFLICT, no rows;
  provision without owner; `provision_in` with a mismatched tenant session → RLS error, no rows.
Decisions written: specs/03 §2 (global identity tables + `MembershipLookupRepo` in the va_system bullet), specs/04 §4.1
(provisioning, slug, duplicate email, token TTL/consume), specs/28 §3 (h)/(i) and the (g) cross-reference. AGENTS.md: two bullets.
Gates: make fmt 0; make lint 0; make typecheck 0 (271 files); make placeholders 0 findings; make test 2171 passed,
TOTAL 99.82 % (every new module is 100 %); jscpd 0 clones; radon avg A (2.54); pip-audit + pnpm audit clean; check_spec_refs
0 problems; openapi current; gitleaks (pre-commit hook) passed; test_env_example 43 passed.

## Review P02-T04b (2026-10-29): approved
Re-ran make lint typecheck placeholders (exit 0, 0 findings) and make test (2171 passed, 99.82 %). Diff checked against
specs/04 §4.1, specs/03 §2/§3, specs/28 §3: provisioning is one tenant_session txn (org, active data key, whsec_ secret
with column AAD, owner + membership), conflict_savepoint only maps unique violations (FK errors propagate, so a bad
parent_org_id is not misreported as a slug conflict), tokens stored hash-only, consume is one conditional UPDATE,
container refuses an empty LOCAL_MASTER_KEY before opening anything. Decisions recorded in specs. No findings.

## 2026-10-29 Planner: P02-T04b approved → P02-T04c split into T04c/T04d
T04c as written (7 endpoints + session principal + CSRF + MFA gate + throttling + internal OpenAPI) is too big for one
Builder pass. Plan file now: T04c = session authentication (login, logout, logout-all, GET session, session principal,
CSRF, MFA gate, AUTH_LOCKED, ROLE_SCOPES); T04d = signup, verify-email, resend-verification, EmailSender in the
container, `openapi/internal.json` + drift. Tests in T04c create users through `OrgProvisioner` (NewUser with a
password hash), not signup.

## Plan P02-T04c — Session authentication (specs/04 §1, §3.3, §4.1–§4.4; specs/03 §11; specs/28 §5)

### Facts found while planning
- `get_principal` (va_api/auth/dependencies.py) is API-key only; `rate_limit_key` already handles `principal_type
  "user"` → `rl:user:{id}:<bucket>`; `plan_rate_limits(plan_id, rate_per_sec)` needs the org override.
- `MembershipLookupRepo.for_user` (system) returns MembershipRecord without `orgs.settings`/`orgs.limits`; `mfa_required`
  lives in `orgs.settings` jsonb (OrgSettings model is T12, so read the raw key here); `rate_per_sec_override(limits)` is
  in va_db/repositories/api_keys.py.
- `AppError` has no headers; `_render_app_error` (va_api/errors.py:193) calls `error_response(..., headers=None)`.
- Container builds `SystemClock()` locally and has no password hasher; `PasswordHasher()` construction computes a
  dummy argon2 hash (CPU, ~0.1 s at spec params).
- specs/26 §1: `/internal/*` routes are `include_in_schema=False` (public openapi.json unchanged by T04c; internal.json
  is T04d). test_openapi asserts `x-scope` only for schema operations.
- Pool size is 1 per role in tests (memory mem-1793262412-7bed): never nest global/tenant sessions; global (va_app) and
  system (va_system) are different engines but still keep them sequential.
- Cheap argon2 params already exist in va_common tests (`Argon2Params(time_cost=1, memory_cost=8, parallelism=1,
  hash_len=16, salt_len=8)`); reuse the same values in api test support.

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. specs/04 §3.3 — **Role → scope derivation**: a role holds `<resource>:read` when any §3.2 row of that resource family
   is R/R*/W/X for it, `<resource>:write` when any row is W/W(own)/X; finer actions stay behind `require_permission`
   (T08: e.g. `calls.operate` gates send/stop/live so analysts may analyze but not send). Sessions never get `*` or `mcp`.
   Resulting sets (exact, asserted by tests):
   - owner, admin: every `<r>:read` and `<r>:write` of the 18 READ_WRITE_RESOURCES + `api_keys:write` + `analytics:read`.
   - developer: same minus `billing:write`, `org:write`.
   - analyst: every `<r>:read` + `calls:write`, `pathways:write`, `analysis:write` + `analytics:read`.
   - billing: `billing:read`, `billing:write`, `org:read`, `analytics:read`.
   - viewer: every `<r>:read` except `billing:read` + `analytics:read`.
   T08 adds permissions and the cell-for-cell matrix parity test and may derive ROLE_SCOPES from its matrix table, as
   long as these sets do not change.
2. specs/04 §4.3 — **Session mechanics**: cookie value = 43-char opaque token (`va_common.auth.issue_token`).
   `va_session`: HttpOnly, `Secure` unless `VA_ENV=local`, SameSite=Lax, Path=/, Domain=`SESSION_COOKIE_DOMAIN` (omitted
   when empty), Max-Age = 90 d (server enforces expiry). **Sliding**: target = min(now + 14 d, created_at + 90 d); the
   row is extended to target only when target − expires_at > 1 h (creation sets now + 14 d, so "last extension" =
   expires_at − 14 d); the 90 d cap makes the absolute maximum fall out of the same rule. **Cache** `sess:{token_hash}`
   (literal braces = hash tag, like `apikey:{prefix}`) JSON `{session_id, user_id, active_org_id, mfa_passed,
   expires_at, created_at}`, TTL = min(300 s, seconds until expires_at, ≥1); written on create/extend/miss, deleted on
   revoke/logout/logout-all; Redis errors fail open to the DB (`session_cache_unavailable` warning). A user's status is
   checked when the session is loaded from the DB; disabling a user must revoke its sessions (later admin task).
   **Active org**: `sessions.active_org_id`, falling back to the user's oldest membership (persisted with
   `set_active_org`) when it is NULL or the membership is gone; no membership at all → `FORBIDDEN` "You are not a member
   of any organization." on principal routes (auth endpoints still work). Session principals: `principal_type "user"`,
   `environment "live"`, `role` from the membership, scopes from ROLE_SCOPES. Org `suspended` → writes
   `ORG_SUSPENDED` (same message as keys); `closed` → `ORG_SUSPENDED` for every method. A request with a non-blank
   `Authorization` header is always API-key authenticated (cookies ignored, CSRF exempt); with neither header nor
   cookie it stays `AUTH_FAILURE` "Invalid API key." (current behaviour). Invalid/expired/revoked session →
   `AUTH_FAILURE` "Session is invalid or expired." **MFA gate**: org `settings.mfa_required is true` and session not
   `mfa_passed` → `MFA_REQUIRED` "Multi-factor authentication is required for this organization." on every route
   except `GET /internal/auth/session`, `/internal/auth/mfa/*`, `POST /internal/auth/logout` (logout-all is gated).
   `login` returns `mfa_required = user.mfa_enabled` (an org requiring MFA from an unenrolled user is handled by the gate
   and the dashboard sending the user to setup, T06). Login always issues a fresh token and revokes the session of any
   `va_session` cookie it received (fixation).
3. specs/04 §4.4 — **CSRF**: `va_csrf` cookie (opaque token, not HttpOnly, SameSite=Lax, same Secure/Domain/Path/Max-Age
   as `va_session`) set at login and on `GET /internal/auth/session` (the existing cookie value is reused when it is a
   well-formed 43-char token, otherwise a new one; body `csrf_token` = cookie). Session-authenticated methods other
   than GET/HEAD/OPTIONS need `X-CSRF-Token` == cookie (`tokens_equal`), then an `Origin` (else `Referer`) whose origin
   (scheme://host[:port], lower-cased, default port dropped) equals `DASHBOARD_BASE_URL`'s; missing both → reject. Both
   failures are `FORBIDDEN`: "CSRF token missing or invalid." / "Request origin is not allowed.". `POST /login` (no
   session yet) rejects a present-but-foreign Origin/Referer only (CLI clients send none).
4. specs/04 §4.2 — **Login throttling**: keys `auth:fail:email:<sha256_hex(email.strip().casefold())>` and
   `auth:fail:ip:<ip>` (single-key commands, no hash tag). Each failed attempt: `INCR` + `EXPIRE 900 NX` in one
   transaction pipeline; when the count reaches the limit (10 email / 50 ip) `EXPIRE 900` again so the lock lasts 15 min
   from the locking failure. Before verifying, a key at/above its limit → `AUTH_LOCKED` "Too many failed login attempts;
   try again in N second(s)." + `Retry-After: N` (N = max(1, TTL)); locked attempts do not increment. Success deletes the
   email key only. Failures = unknown email, wrong password, user without password, disabled user (all
   `INVALID_CREDENTIALS` "Invalid email or password."; argon2 runs in every case via the dummy hash). Redis errors fail
   open (`login_throttle_unavailable` warning). SSO_REQUIRED on enforced domains arrives with T07.
5. specs/04 §4.1 — response shapes (envelope `{data, errors: null}`): login `{mfa_required, user}`; logout / logout-all
   `{revoked_sessions: int}` and both cookies cleared; session `{user, memberships[], active_org_id, mfa_passed,
   mfa_required, csrf_token, expires_at}` where `user = {id, email, name, email_verified, mfa_enabled, is_platform_admin,
   created_at}`, membership = `{org_id, org_name, org_slug, role, plan_id, org_status, parent_org_id}`, and
   `mfa_required` = active org enforces MFA and the session has not passed it. Routes carry `x-public: "session-flow"`
   (specs/04 §2.4 allowlist) and operation ids `auth_login`, `auth_logout`, `auth_logout_all`, `auth_session_get`.
6. specs/03 §11 — update the `sess:{token_hash}` row (fields, TTL rule) and add rows `auth:fail:email:{sha256(email)}`
   and `auth:fail:ip:{ip}` (counter, 900 s, login throttling, specs/04 §4.2) after the `apikey:*` rows.
7. specs/31 or errors note not needed: `AppError(headers=...)` is an implementation detail (AGENTS.md bullet).

### Files
**packages/va_common/src/va_common/errors.py** — `AppError.__init__(..., *, field=None, headers: Mapping[str, str] |
None = None)` stored as `self.headers: dict[str, str]` (empty dict default; not part of `to_error_item`).
**apps/api/src/va_api/errors.py** — `_render_app_error` passes `exc.headers or None`.

**packages/va_db/src/va_db/repositories/memberships.py**
- `MembershipRecord` gains `mfa_required: bool` (`org.settings.get("mfa_required") is True`) and `rate_per_sec: int |
  None` (`rate_per_sec_override(org.limits)`); factor `_record(membership, org)` used by both queries.
- `MembershipLookupRepo.for_user_in_org(user_id, org_id) -> MembershipRecord | None` (same join + `org_id` filter).
**packages/va_db/src/va_db/repositories/identity.py** — no new methods expected; if `get_active_session` + `get_user`
  need one round trip, add `get_active_session_with_user(token, now) -> tuple[Session, User] | None` (join) and use it.

**apps/api/src/va_api/settings.py** — `session_cookie_domain: str = ""` (spec env var exists; `.env.example` and
  test_env_example already have it — verify, no row changes).

**apps/api/src/va_api/auth/rbac.py** (new) — `ROLE_SCOPES: Final[Mapping[MemberRole, frozenset[str]]]` built from
  READ_WRITE_RESOURCES per decision 1 (MappingProxyType), `scopes_for_role(role) -> frozenset[str]`.

**apps/api/src/va_api/auth/cookies.py** (new) — `SESSION_COOKIE: Final = "va_session"`, `CSRF_COOKIE = "va_csrf"`,
  `CSRF_HEADER = "x-csrf-token"`, `COOKIE_MAX_AGE_S = 90 * 86400`; `@dataclass(frozen=True, slots=True) CookiePolicy:
  secure: bool; domain: str | None` + `CookiePolicy.from_settings(settings)` (secure = va_env != LOCAL);
  `set_session_cookie(response, policy, token)`, `set_csrf_cookie(response, policy, token)`,
  `clear_auth_cookies(response, policy)` (both, same domain/path).

**apps/api/src/va_api/auth/csrf.py** (new) — `CSRF_INVALID: Final = "CSRF token missing or invalid."`,
  `ORIGIN_NOT_ALLOWED: Final = "Request origin is not allowed."`, `origin_of(url: str) -> str | None`,
  `check_csrf(header: str | None, cookie: str | None) -> None`, `check_origin(origin: str | None, referer: str | None,
  allowed: str, *, required: bool) -> None`, `is_well_formed_token(value: str | None) -> bool` (43 chars base64url;
  put next to `generate_token` in va_common.auth.tokens instead if cleaner — `looks_like_token`), `READ_METHODS` reused
  from api_key_auth.

**apps/api/src/va_api/auth/sessions.py** (new)
- Constants `SLIDING_TTL = timedelta(days=14)`, `EXTEND_AFTER = timedelta(hours=1)`, `ABSOLUTE_TTL = timedelta(days=90)`,
  `SESSION_CACHE_TTL_S = 300`, `SESSION_INVALID: Final = "Session is invalid or expired."`.
- `session_cache_key(token_hash) -> str` (`sess:{<hash>}`); pure `next_expiry(*, created_at, expires_at, now) ->
  datetime | None`; pure `cache_ttl_s(expires_at, now) -> int`.
- `@dataclass(frozen=True, slots=True) ActiveSession: session_id, token_hash, user_id, active_org_id: UUID | None,
  mfa_passed: bool, created_at, expires_at` + `to_json()` / `from_json()` (ISO datetimes via va_common.json).
- `class SessionManager:` `__init__(*, database, redis, clock)`;
  `start(*, user_id, active_org_id, ip, user_agent) -> tuple[str, ActiveSession]` (global_session create_session,
  expires now+14 d; cache write);
  `resolve(token: str) -> ActiveSession | None` (malformed → None; cache hit → expiry check; miss → global session
  load (session + active user), cache; then sliding extension via `next_expiry` → `extend_session` + cache rewrite);
  `set_active_org(active, org_id) -> ActiveSession`; `revoke(active) -> int` (revoke_session + DEL);
  `revoke_all(user_id) -> int` (revoke_user_sessions + DEL every returned hash).
  All Redis calls wrapped: `(RedisError, OSError)` → warning, continue.

**apps/api/src/va_api/auth/login_throttle.py** (new) — `EMAIL_FAILURE_LIMIT = 10`, `IP_FAILURE_LIMIT = 50`,
  `FAILURE_WINDOW_S = 900`; `email_failure_key(email)`, `ip_failure_key(ip)`; `class LoginThrottle(redis)`:
  `async check(email, ip) -> None` (raises AUTH_LOCKED with headers), `async record_failure(email, ip) -> None`,
  `async clear(email) -> None`. `ip=None` skips the ip key.

**apps/api/src/va_api/auth/session_auth.py** (new) — request-level glue:
- `async def current_session(request) -> ActiveSession` FastAPI dependency: cookie → `SessionManager.resolve` (else
  AUTH_FAILURE SESSION_INVALID); non-read methods → `check_csrf` + `check_origin(required=True)`; stores
  `request.state.session`.
- `async def session_principal(request, active) -> tuple[RequestContext, MembershipRecord]`: membership via system
  `MembershipLookupRepo.for_user_in_org` or fallback `for_user()[0]` (+ `set_active_org`), FORBIDDEN when none; org
  status checks; MFA gate (`enforce_mfa(record, active)` helper reused by logout-all); builds RequestContext.
- `get_principal` in dependencies.py: non-blank Authorization → existing key path; elif `va_session` cookie →
  `current_session` logic + `session_principal`, bind org_id, `enforce(... "default", plan_rate_limits(record.plan_id,
  record.rate_per_sec))`, no touch task; else existing key path with None. Keep dependencies.py under radon B: move the
  key branch into `_api_key_principal(request, background)` and the session branch into session_auth.
- Accessors `sessions_of(request)`, `login_throttle_of(request)`, `password_hasher_of(request)` from the container.

**apps/api/src/va_api/routers/auth.py** (new) — `internal_auth_router = envelope_router(prefix="/internal/auth",
  tags=["Auth"], include_in_schema=False)`; routes per decision 5:
  - `POST /login` (`LoginRequest{email: str (strip, 3–320), password: str (1–1024)}`, extra forbid): origin check
    (required=False) → throttle.check → global_session `get_user_by_email` (close) → `verify_async` → failure path
    (record_failure, INVALID_CREDENTIALS) → success: throttle.clear, rehash if `needs_rehash` (`hash_async`), revoke
    the presented `va_session` if it resolves, `for_user` (system) for the first org, `SessionManager.start`,
    `record_login`, set both cookies, return LoginResult.
  - `POST /logout` (current_session) → `revoke`, clear cookies.
  - `POST /logout-all` (current_session + MFA gate on the active org when one exists) → `revoke_all`, clear cookies.
  - `GET /session` (current_session) → memberships via `for_user`, active org resolution (same fallback helper), csrf
    reuse/new, set `va_csrf`, return SessionInfo.
  Optional `{}` bodies accepted (`EmptyBody` extra forbid, `Body(default=None)`).
**apps/api/src/va_api/routers/auth_schemas.py** (new) — `DataResponse[T]` generic (`data: T`, `errors: None = None`)
  — or put `DataResponse` in a new `va_api/responses.py` for reuse by later envelope routers (preferred) — plus
  LoginRequest, SessionUser, SessionMembership, SessionInfo, LoginResult, LogoutResult with `json_schema_extra`
  examples (specs/26 §1 requires examples; internal.json comes in T04d).
**apps/api/src/va_api/main.py** — add `internal_auth_router` to ROUTERS (before probe_router).
**apps/api/src/va_api/container.py** — fields `clock: Clock`, `password_hasher: PasswordHasher` (built with
  `await asyncio.to_thread(PasswordHasher)`), `sessions: SessionManager`, `login_throttle: LoginThrottle`; reuse the
  single `clock` for authenticator/limiter/idempotency.
**apps/api/tests/support/session_helpers.py** (new) — `CHEAP_ARGON2`, `SessionTestContainer` dataclass (database,
  redis=fakeredis, FrozenClock, authenticator with KeyStore, rate_limiter, idempotency MemoryIdempotencyStore,
  password_hasher cheap, sessions, login_throttle, settings), `session_app(container, *extra_routers)` via
  `create_app(container_factory=...)`, `make_member(provisioner|database, *, password, role="owner", verified=True,
  settings=None, status=None) -> (user, org)`, `login(client, email, password) -> csrf`, a test-only envelope router
  `/v1/knowledge/_session` (GET `knowledge:read`, POST `knowledge:write`) echoing ctx fields. Reuse auth_helpers /
  api_helpers pieces; do not copy (jscpd).
**AGENTS.md** — bullets: session principal path + exemptions; `AppError(headers=)`; tests use session_helpers with
  cheap argon2 and https base_url (Secure cookies are not sent over http by httpx).

### Tests
apps/api/tests/test_rbac.py: exact sets per decision 1; every value ⊆ SCOPES; no `*`/`mcp`; owner == admin.
apps/api/tests/test_session_lifetime.py (unit): next_expiry — within 1 h → None; 1 h + 1 s → now + 14 d; near cap →
  created + 90 d; at cap → None; cache_ttl_s ≤ 300 and ≥ 1; ActiveSession JSON round trip; session_cache_key format.
apps/api/tests/test_csrf.py (unit): origin_of (case, default ports 80/443 dropped, path/query ignored, garbage → None);
  check_csrf missing header/cookie/mismatch/match; check_origin Origin match, Referer fallback, mismatch, missing with
  required True/False, `Origin: null` rejected.
apps/api/tests/test_login_throttle.py (fakeredis): 9 failures pass check, 10th locks with AUTH_LOCKED + Retry-After ==
  TTL (≤ 900, > 0) and headers rendered by the app; locked attempts do not increment; lock clears when the key is gone;
  ip limit 50 across different emails; clear() resets email only; Redis failure (stub raising ConnectionError) → check
  and record do not raise.
apps/api/tests/test_errors.py: AppError headers reach the response (existing handler test file).
packages/va_common/tests: AppError headers default empty and not in to_error_item.
packages/va_db/tests/test_membership_lookup_integration.py: `for_user_in_org` hit/miss; `mfa_required` true only for
  JSON `true` (not `"true"`); `rate_per_sec` from limits.
apps/api/tests/test_session_auth_integration.py (migrated Postgres module fixture like test_idempotency_db, fakeredis,
  FrozenClock, `TestClient(app, base_url="https://testserver")`):
- `test_login_sets_cookies_and_stores_hash` (cookie attributes: HttpOnly/SameSite=Lax/Path=/ /Secure; va_csrf not
  HttpOnly; DB token_hash == sha256_hex(cookie); last_login_at set; mfa_required false; Secure absent when va_env=local).
- `test_login_invalid_credentials_generic` (unknown email and wrong password → same 401 body; counter keys incremented).
- `test_login_locks_after_ten_failures` (11th with the right password → 429 AUTH_LOCKED + Retry-After).
- `test_login_rehashes_old_params`; `test_login_revokes_presented_session`; `test_disabled_user_cannot_login`.
- `test_login_rejects_foreign_origin` (Origin evil → 403; no Origin → ok).
- `test_session_endpoint_returns_memberships_and_csrf` (csrf body == cookie; reused on second call).
- `test_session_principal_reaches_scoped_route` (ctx user/role/scopes; `rl:user:{<id>}:default` exists; X-RateLimit headers).
- `test_csrf_missing_or_wrong_is_403`, `test_session_write_requires_dashboard_origin` (Origin mismatch, both missing →
  403; Referer from dashboard ok), `test_api_key_requests_skip_csrf` (Authorization + cookie → key principal).
- `test_expired_session_is_401` (advance 14 d + 1 s); `test_sliding_extension_hourly` (advance 2 h → DB expires_at =
  now + 14 d; advance 30 min → unchanged); `test_absolute_lifetime_90_days` (request every 13 d → expires_at capped at
  created + 90 d, then 401 after it).
- `test_session_cache_hit_and_revocation` (key JSON fields, TTL ≤ 300; second request with DB row expired-by-SQL still
  served until cache deleted? — no: assert served from cache by counting DB lookups via a spy, and revoke+DEL → 401).
- `test_logout_revokes_and_clears_cookies` (Max-Age=0 for both; cache key gone; next request 401; logout without CSRF
  → 403); `test_logout_all_revokes_every_session` (two logins; both 401; both cache keys gone).
- `test_mfa_required_gate` (org settings mfa_required true: scoped route 401 MFA_REQUIRED; session 200 with
  mfa_required true; logout 200; logout-all 401; after set_mfa_passed + cache DEL → 200).
- `test_suspended_and_closed_org` (suspended GET 200 / POST ORG_SUSPENDED; closed GET ORG_SUSPENDED).
- `test_active_org_fallback_and_no_membership` (active_org_id NULL → oldest membership persisted; all memberships
  removed via system session → principal route 403, session endpoint 200 with []).
- `test_disabled_user_session_rejected_on_cache_miss`.
apps/api/tests/test_container.py: container exposes clock, PasswordHasher (spec params), SessionManager, LoginThrottle;
  existing RuntimeError/close-order assertions still hold.
apps/api/tests/test_openapi.py: public document contains no `/internal/` path (guards include_in_schema=False).

### Gates
`make fmt lint typecheck placeholders test` + jscpd (session_helpers must reuse auth_helpers/api_helpers/idempotency
store and the va_db support `OrgFactory`/identity_db helpers — check what apps/api tests already import from
packages/va_db/tests/support), radon (keep login handler ≤ B: split into `_authenticate(...)` and `_issue(...)`),
pip-audit, check_spec_refs, `make openapi` must report unchanged (internal routes excluded), gitleaks, test_env_example.

### Risks
- httpx TestClient drops `Secure` cookies on http → use `base_url="https://testserver"`; Starlette's `delete_cookie`
  must pass the same domain/path/secure/samesite.
- FrozenClock vs DB `now()`: sessions rows use caller times (`create_session(expires_at=...)`); `get_active_session`
  takes `now` from the clock — verify `created_at` is the clock time, not DB default; if `create_session` relies on the
  server default for `created_at`, pass `created_at=now` explicitly (add a kwarg, as done for email tokens in T04b),
  otherwise the absolute-lifetime test mixes clocks.
- fakeredis `EXPIRE ... NX` support: verify; fallback is `SET key 0 EX 900 NX` then `INCR` in the same MULTI.
- Pool size 1 per role: login touches global → system → global sequentially; never hold one while opening another.
- Request-level DB cost: one system query per session request (membership); acceptable for dashboard traffic, note
  in decision.
- Flaky Docker timeouts (memories): rerun isolated before suspecting code.

## 2026-10-29 Builder: P02-T04c build notes
Implemented the plan as written. Deviations / details worth reviewing:
- `SessionManager.start` also stamps `users.last_login_at` in the same global transaction (start == login; signup/SSO
  will reuse it). `IdentityRepo.create_session(created_at=...)` added (server default otherwise) and
  `get_active_session_with_user` (join; status checked in the manager, disabled → None on cache miss).
- `MembershipRecord` gained `mfa_required` / `rate_per_sec` (defaults keep existing constructors valid);
  `MembershipLookupRepo.for_user_in_org`. `va_common.auth.looks_like_token` (43 base64url) next to `generate_token`.
- `get_principal`: non-blank Authorization → `_api_key_principal`; else `va_session` cookie → `session_auth.
  authenticate_session`; else key path (AUTH_FAILURE "Invalid API key."). Both return `(ctx, rate_per_sec)` so the
  `default` bucket charge is shared.
- `client_ip(request)` drops non-IP peers (Starlette TestClient's `testclient`); `create_session` would raise on them.
- Org `closed` message: "Organization is closed." (decision records it). AUTH_LOCKED message pluralises ("1 second").
- `GET /session` returns `active_org_id: null` when the user has no memberships (not the stale session column).
- `DataResponse[T]` lives in `va_api/responses.py`. `make_client(app, base_url=...)` gained the param (https tests).
- `migrated_postgres()` context manager in session_helpers; test_idempotency_db now uses it (jscpd).
- Decisions added: specs/04 §3.3, §4.1, §4.2, §4.3, §4.4; specs/03 §11 rows for `sess:` (fields/TTL) and
  `auth:fail:*`.
Gates (this iteration): make fmt/lint/typecheck/placeholders 0; make test 2251 passed, 99.71 %; openapi drift current;
check_spec_refs 0; jscpd 0 clones; radon avg A (2.36), nothing ≥ C; pip-audit --strict clean; pnpm audit clean;
gitleaks hook passed; test_env_example 43 passed.

## Review P02-T04c (2026-10-29): approved
Re-ran: make lint/typecheck/placeholders exit 0; make test 2251 passed, 99.71 % coverage; check_openapi_drift current;
check_spec_refs 0; jscpd 0 clones; radon avg A (1.93), worst B (get_session 10). Diff checked against specs/04 §1,
§3.3, §4.1–§4.4 decisions and specs/03 §11: cookie attributes, sliding/absolute expiry rule, `sess:` cache fields/TTL,
CSRF double submit + Origin/Referer, MFA gate exemptions (session, logout; logout-all gated), throttle keys/limits/
Retry-After, one argon2 verify for every failure path, pool-size-1 sessions never nested. No findings.

## 2026-10-29 Planner: P02-T04c approved → next P02-T04d

## Plan P02-T04d — Signup, email verification, resend, internal OpenAPI document (specs/04 §4.1; specs/26 §1; specs/31)

### Facts found while planning
- `OrgProvisioner.provision_in(session, org_id, name=, owner=NewUser(...))` runs inside a caller-owned
  `tenant_session(org_id)`; users/sessions/email_tokens are global tables with no RLS (specs/03 §2 decision), so
  the same transaction can insert the session row and the verify token. Duplicate email → `CONFLICT` field `email`;
  empty org name → `INVALID_INPUT` field `org_name` (already implemented).
- `IdentityRepo.issue_email_token(user_id, purpose, now, ttl)` / `consume_email_token(token, purpose, now)` /
  `mark_email_verified(user_id, now)` (first wins) / `count_issued_since` exist; `EMAIL_VERIFY_TTL = 24 h`.
- `va_common.email`: `EmailSender` protocol, `SmtpEmailSender(config, sender=, clock=)`, `InMemoryEmailSender`
  (`.to(addr)`, `.clear()`), `templates.verify_email(name=, link=, to=)`; `ApiSettings` already includes
  `EmailSettings` (`smtp_config()`, `email_from`).
- `check_password_policy_async(password, email=)` raises `PASSWORD_TOO_WEAK` field `password`.
- Dashboard route for the link is `/verify-email/[token]` (specs/25 §routes) → link
  `{DASHBOARD_BASE_URL rstrip /}/verify-email/{token}`.
- FastAPI `get_openapi` skips `include_in_schema=False` routes; internal doc must generate from copies.
- `SessionManager.start` opens its own global session; signup needs a variant inside the provisioning txn.

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. specs/04 §4.1 **signup**: body `{email (3-320, trimmed), password (1-1024), name (1-200, trimmed), org_name
   (1-200, trimmed)}`, unknown fields `INVALID_INPUT`. Order: Origin/Referer check as login (`required=False`) →
   `check_password_policy_async(password, email=email)` → `hash_async` → one `tenant_session(new_id())` transaction:
   `provision_in(owner=NewUser(email, name, password_hash))` (plan `start`), session row (active org = new org,
   `created_at=now`, expires now+14 d, `last_login_at=now`), `verify_email` token (24 h) → after commit: `sess:`
   cache write, revoke a presented valid `va_session` (fixation defence, same as login), set `va_session`+`va_csrf`,
   status **201**, body `{data:{user: SessionUser, org: SignupOrg}}` with `SignupOrg = {id, name, slug, plan_id,
   status, created_at}`. The verification email is sent after the response as a FastAPI background task; a delivery
   failure is logged (`verification_email_failed`, template + error type, never the address) and never fails signup
   (the user can resend). Duplicate email `CONFLICT` 409 "An account with this email already exists." (signup reveals
   registration by design, like every signup form; login stays generic). New orgs have no `mfa_required`, so the
   "MFA not required until verified" clause holds with no extra code; `EMAIL_NOT_VERIFIED` gating of specific actions
   arrives with those actions (none in phase 02 so far — note it).
2. **verify-email**: body `{token: str (1-256)}`; no session needed (the link may be opened in another browser),
   Origin check `required=False`. Malformed token (not `looks_like_token`) or `consume_email_token` → `None` ⇒
   `TOKEN_EXPIRED` 400 "Verification link is invalid or has expired." `field: "token"`. Success → `mark_email_verified`
   (same global txn) → `{data:{user: SessionUser}}` (re-read user). Already-verified user with a fresh token: still
   200 (first verification time kept).
3. **resend-verification**: session-authenticated (`current_session`: CSRF + Origin), MFA-gated like logout-all
   (`active_membership` + `enforce_mfa`), optional `{}` body. Already verified → 200 `{data:{sent:false,
   email_verified:true}}`, no counter increment. Limit: Redis fixed window `auth:resend:{<user_id>}`… no hash tag
   needed → key `auth:verify:resend:<user_id>`: `INCR` + `EXPIRE 3600 NX` in one MULTI; count > 3 ⇒ `RATE_LIMITED`
   429 "Too many verification emails; try again in N seconds." (same pluralisation helper as AUTH_LOCKED — reuse,
   extract `seconds_phrase(n)` if it is private in login_throttle) with `Retry-After: N` = key TTL (3600 when none).
   Rejected attempts still count (no extra work beyond INCR). Redis errors → `resend_limit_unavailable` warning and
   allow. Success: issue a new 24 h token (older unused ones stay valid until used/expired), email in background,
   `{data:{sent:true, email_verified:false}}`. The signup email does not count toward the 3.
   Add the key to specs/03 §11 Redis table (TTL 1 h, counter).
4. specs/26 §1 **internal document**: `openapi/internal.json`, title "VoiceAgents Internal API", same version,
   servers, contact, license; paths = every APIRoute whose path starts with `/internal/` (from `app.state.routers`,
   shallow `copy.copy` with `include_in_schema=True`); tags `[{"name":"Auth"}]` (extendable list
   `INTERNAL_TAG_NAMES`); securitySchemes `{"SessionCookie": {"type":"apiKey","in":"cookie","name":"va_session"}}`
   only; same error components + 422→400 + `default` rewrite; `x-public` kept on operations. Generated by
   `app.state.internal_openapi()` (a `partial(build_internal_openapi, app, cfg)`, cached on `app.state`).
   `make openapi` writes both files; `check_openapi_drift.py` checks both (each pair reported separately).

### Files
**apps/api/src/va_api/auth/sessions.py** — split `start` into `start_in(session: AsyncSession, *, user_id,
  active_org_id, ip, user_agent) -> tuple[str, ActiveSession]` (row + `record_login`, no cache) and public
  `remember(active) -> None` (the existing `_store`, Redis errors swallowed); `start` = global_session + start_in +
  remember. Keep existing tests green.
**apps/api/src/va_api/auth/verification.py** (new) — `RESEND_LIMIT: Final = 3`, `RESEND_WINDOW_S: Final = 3600`,
  `TOKEN_INVALID_MESSAGE`, `RESEND_LIMITED_MESSAGE` builder, `resend_key(user_id) -> str`, `class ResendLimiter(redis)`
  with `async hit(user_id) -> None` (raises RATE_LIMITED + headers), `verify_link(dashboard_base_url, token) -> str`,
  `async def send_verification(sender: EmailSender, *, user: User, token: str, dashboard_base_url: str) -> None`
  (builds `templates.verify_email`, catches `EmailDeliveryError`/`OSError` → warning). Pluralisation: reuse the
  login_throttle helper (move it to a shared private module, e.g. `va_api/auth/_wording.py`, if needed for jscpd).
**apps/api/src/va_api/auth/session_auth.py** — accessors `email_sender_of(request)`, `provisioner_of(request)`,
  `resend_limiter_of(request)`, `clock_of(request)` (or reuse sessions' clock via container) in the existing style.
**apps/api/src/va_api/routers/auth_schemas.py** — `SignupRequest`, `SignupOrg` (`.of(org)`), `SignupResult{user,
  org}`, `VerifyEmailRequest{token}`, `VerifyEmailResult{user}`, `ResendResult{sent, email_verified}`; examples via
  `json_schema_extra` (specs/26 requires them; now visible in internal.json). Reuse the email/password Annotated
  types from LoginRequest (define `EmailField`/`PasswordField` aliases once).
**apps/api/src/va_api/routers/auth.py** — routes `POST /signup` (`operation_id="auth_signup"`, status_code=201,
  BackgroundTasks), `POST /verify-email` (`auth_verify_email`), `POST /resend-verification`
  (`auth_resend_verification`), all with `openapi_extra=PUBLIC_MARKER`. Keep each handler ≤ radon B: split signup
  into `_create_account(request, body, ip) -> tuple[ProvisionedOrg, str, ActiveSession, str]` (txn) and reuse a
  shared `_set_session_cookies(response, request, token)` with `_issue` (login). If the module grows past ~350 lines,
  move signup/verify/resend into `routers/auth_signup.py` sharing `internal_auth_router`.
**apps/api/src/va_api/container.py** — fields `email_sender: EmailSender` (SmtpEmailSender(settings.smtp_config(),
  sender=settings.email_from, clock=clock)), `resend_limiter: ResendLimiter`. No network at build (aiosmtplib
  connects per send — verify).
**apps/api/src/va_api/openapi.py** — factor the post-processing (`_finalise(document, security_schemes)`);
  `INTERNAL_TITLE`, `INTERNAL_TAG_NAMES = ("Auth",)`, `INTERNAL_SECURITY_SCHEMES`, `INTERNAL_PREFIX = "/internal/"`,
  `internal_routes(routers) -> list[APIRoute]`, `build_internal_openapi(app, settings) -> dict`. Every internal route
  must have a tag in INTERNAL_TAG_NAMES (test).
**apps/api/src/va_api/main.py** — `app.state.internal_openapi = partial(build_internal_openapi, app, cfg)`.
**scripts/check_openapi_drift.py** — `DEFAULT_INTERNAL_SPEC = Path("openapi/internal.json")`, `--internal-spec`;
  `render_documents(app) -> dict[Path, str]` (public via `app.openapi()`, internal via `app.state.internal_openapi()`
  — protocol `AppLike` gains `state`); compare each, exit 1 if any drifts, message names the file.
**scripts/export_openapi.py** — writes both (`wrote`/`unchanged` per file); `--internal-out`.
**openapi/internal.json** (generated by `make openapi`, committed). `openapi/openapi.json` must stay unchanged.
**Makefile** — `openapi` help text "Regenerate openapi/openapi.json and openapi/internal.json".
**apps/api/tests/support/session_helpers.py** — `SessionContainer` gains `envelope`/`provisioner` (from IdentityDb;
  take `identity: IdentityDb` kw field or pass both), `email_sender: InMemoryEmailSender`, `resend_limiter`;
  helper `signup(client, *, email, password=PASSWORD, name="Jane Park", org_name="Acme Dental") -> httpx.Response`
  and `link_token(sender, email) -> str` (parse the token from the text body link).
**specs/04, specs/26, specs/03 §11** — decisions above. **AGENTS.md** — internal.json in `make openapi`, signup
  email background task, resend key.

### Tests
apps/api/tests/test_signup_integration.py (migrated Postgres module fixture via `migrated_postgres`, fakeredis,
FrozenClock, https client):
- `test_signup_verify_flow`: 201; body user.email_verified false, org plan_id `start`, slug `acme-dental-xxxx`;
  cookies set (attrs as login); session principal reaches the probe route with role owner; one email to the address
  with template `verify_email` whose link starts with DASHBOARD_URL + `/verify-email/`; POST verify-email with the
  token → user.email_verified true; DB `email_verified_at` == clock now; webhook secret decrypts (provisioner).
- `test_signup_rejects_weak_password` (short, breached, contains local part → 400 PASSWORD_TOO_WEAK field password;
  no rows created via row_counts).
- `test_signup_duplicate_email` (409 CONFLICT field email; case-insensitive (citext) `JANE@…`; no new org rows).
- `test_signup_validation` (missing org_name / blank org_name / unknown field → 400 INVALID_INPUT).
- `test_signup_foreign_origin_rejected` (403) and no-Origin CLI ok.
- `test_signup_revokes_presented_session`.
- `test_signup_email_failure_does_not_fail` (sender stub raising EmailDeliveryError → 201, warning logged, no address
  in log).
- `test_verify_token_reuse_and_expiry` (second use → 400 TOKEN_EXPIRED; token after clock +24 h +1 s → TOKEN_EXPIRED;
  garbage token → TOKEN_EXPIRED; reset-purpose token rejected for verify).
- `test_resend_verification_limit` (3 resends 200 sent true + 3 more emails; 4th → 429 RATE_LIMITED Retry-After ≤
  3600; clock-independent: delete key → allowed again); `test_resend_requires_session_and_csrf` (no cookie 401, no
  CSRF 403); `test_resend_when_verified` (sent false, no email, no counter key); `test_resend_redis_down_allows`
  (FakeServer connected=False on limiter only).
apps/api/tests/test_verification.py (unit): verify_link joining (trailing slash), resend_key format, limiter with
  fakeredis (TTL, message pluralisation "1 second").
apps/api/tests/test_sessions_start_in.py or extend existing session tests: `start_in` + `remember` equals `start`.
apps/api/tests/test_openapi.py: internal document — validates with openapi_spec_validator; contains
  `/internal/auth/{signup,verify-email,resend-verification,login,logout,logout-all,session}`; only `SessionCookie`
  security scheme; every operation has `x-public`, `x-envelope: envelope`, a 2xx example (request bodies too),
  operationIds unique + regex; public document still has no `/internal/`; committed `openapi/internal.json` equals
  `render(app.state.internal_openapi())`.
scripts/tests/test_check_openapi_drift.py / test_export_openapi.py: both files written/checked; drift in only the
  internal file → exit 1 naming it; fake apps updated with `state.internal_openapi`.
apps/api/tests/test_container.py: container exposes SmtpEmailSender configured from settings and ResendLimiter.

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` then `uv run python scripts/check_openapi_drift.py`
(public unchanged, internal current); check_spec_refs; jscpd (session_helpers/test helpers reuse; signup cookie code
shared with login), radon (nothing ≥ C), pip-audit, gitleaks, test_env_example (no new env vars).

### Risks
- Pool size 1 per role: signup holds a tenant session (va_app) — never open global_session/system inside it; the
  revoke of a presented session and the cache write happen after commit.
- `ActiveSession.of(row)` inside the txn before commit: build it inside, use it after (attributes loaded; no lazy
  loads → MissingGreenlet).
- BackgroundTasks run after the response body; with httpx ASGITransport the client call returns after they finish,
  so in-memory assertions work without sleeps; verify.
- `copy.copy(APIRoute)` keeps dependant/response models; ensure FastAPI does not reuse cached `openapi_schema`
  across the two documents (separate cache attribute).
- aiosmtplib: confirm `SmtpEmailSender.__init__` does no I/O (container builds without a relay).
- Docker flakes (memories): rerun isolated before suspecting code.

## 2026-10-29 Builder: P02-T04d build notes
- `SessionManager.start` = `global_session` + `start_in(session, ...)` (row + `record_login`, no cache) + public
  `remember(active)` (old `_store`). Signup: origin → policy → hash → one `tenant_session(new_id())`: `provision_in`
  + `start_in` + `issue_email_token(verify_email, 24 h)` → after commit `remember`, revoke presented session, cookies,
  201, background `send_verification`. Helpers `_check_dashboard_origin`, `_revoke_presented`, `_set_session_cookies`
  shared with login. `_owner_of` raises RuntimeError if provisioning returned no owner (unreachable).
- verify-email: `looks_like_token` → consume → get_user → mark verified → refresh → `{user}`; all failures
  TOKEN_EXPIRED field token. resend: current_session + active_membership + enforce_mfa; verified → sent false (no
  INCR); `ResendLimiter.hit` (INCR, EXPIRE NX, TTL in one MULTI) → new token → background email.
- `va_api/auth/verification.py` (limits, key, link, `send_verification(sender, *, to, name, token,
  dashboard_base_url)`); `login_throttle.seconds_phrase` shared for pluralisation.
- Container: `email_sender = email_sender_for(settings, clock)` (InMemory only when VA_ENV=test, else
  SmtpEmailSender, no I/O at build) and `resend_limiter`. Accessors `clock_of`, `provisioner_of`,
  `email_sender_of`, `resend_limiter_of` in session_auth.
- OpenAPI: `_finalise` shared; `internal_routes` (copy.copy + include_in_schema=True), `build_internal_openapi`
  cached on `app.state.internal_openapi_schema`; `app.state.internal_openapi`. `DataResponse[T]` json_schema_extra
  wraps T's examples as `{data: ...}` (public openapi.json unchanged). Scripts: `render_documents`,
  `--internal-spec`/`--internal-out`, `load_app` requires `state.internal_openapi()`; per-file reporting.
- Tests: session fixtures moved to apps/api/tests/conftest.py (jscpd); `SessionContainer(identity=...)` with
  `outbox`, `provisioner`, `resend_limiter`; `session_helpers.signup`, `link_token`. New
  test_signup_integration.py (19), test_verification.py, internal-doc tests in test_openapi.py, script tests.
- Specs: 04 §4.1 decision (signup/verify/resend + email sender choice), 26 §1 decision (internal doc), 03 §11 row
  `auth:verify:resend:{user_id}`. AGENTS.md entry + `make openapi` row; Makefile help text.
Gates: make fmt/lint/typecheck 0; placeholders 0 findings; make test 2289 passed, 99.55 %; drift: both current;
check_spec_refs 0; jscpd 0 clones; radon avg A (2.67), nothing ≥ C; pip-audit --strict clean; pnpm audit clean;
gitleaks passed; test_env_example 43 passed.

## 2026-10-29 Planner: P02-T04d build.blocked (payload format only) → re-plan same task
The blocker is not a design or code problem: the Builder's `build.done` payload lacked the required backpressure
keys, so the loop converted it to `build.blocked`. The implementation and its gate results above stand; no split or
prerequisite is needed. Plan P02-T04d is unchanged.
Builder, this activation: do not redesign. Re-run the gates on the current working tree (`make fmt lint typecheck
placeholders test`, `uv run python scripts/check_openapi_drift.py`, check_spec_refs, jscpd, radon, pip-audit
--strict, pnpm audit, gitleaks), fix anything red, then emit build.done with EXACTLY these keys in the payload
(real values from this run), e.g.:
`ralph emit "build.done" "P02-T04d: tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN %), complexity: A (x.xx), duplication: pass (0 clones), specs: pass"`

## 2026-10-29 Builder: P02-T04d gate re-run (no code changes)
Working tree unchanged from the build above. Re-ran every gate on it:
make fmt 0; make lint 0; make typecheck 0; make placeholders 0 (0 findings); make test 0 (2289 passed, 99.55 %);
check_openapi_drift both current; check_spec_refs 0 problems; jscpd@4 0 clones (523 files); radon production
src avg A (1.93), nothing >= C (only test functions rated C/D because asserts count as branches; repo-wide avg A 2.60);
uvx pip-audit --strict on uv export: no known vulnerabilities; corepack pnpm audit clean; pre-commit gitleaks passed.
Note: bare `radon`, `pnpm`, `gitleaks` are not on PATH; use `uvx radon`, `corepack pnpm`, `uv run pre-commit run gitleaks`.

## Review P02-T04d (2026-10-29): approved
Re-ran: make lint 0, make typecheck 0, make placeholders 0 (0 findings), make test 0 (2289 passed, 99.55 %),
check_openapi_drift (openapi.json and internal.json current), check_spec_refs 0, jscpd@4 0 clones (523 files),
radon cc -n C on apps/api/src: none. Diff checked against specs/04 §4.1 (signup/verify/resend decision, provisioning
decision: CONFLICT field email, 24 h verify token, single-use consume), §4.2 (policy incl. email local part via
check_password_policy_async), specs/26 §1 internal document decision, specs/03 §11 resend key row. Signup txn keeps
everything in one tenant_session; cache write + presented-session revoke after commit; email failures logged without
address; resend MFA-gated, counter before token issue, Redis down allows. P02-T04 parent ticked (T04a–d all done).

## 2026-10-29 Planner: P02-T04d approved → next P02-T05
P02-T04 parent ticked. First unticked task with ticked deps: P02-T05 Password reset and email change (depends on T04).

## Plan P02-T05 — Password forgot / reset / change (specs/04 §4.1 rows `password/forgot|reset|change`, §4.2, §13; specs/31 TOKEN_EXPIRED, REAUTH_REQUIRED; specs/25 `/forgot-password`, `/reset-password/[token]`)

### Facts found while planning
- specs/04 §4.1 has no email-change endpoint, specs/03 `email_tokens.purpose` allows only `verify_email`/`reset_password`,
  and specs/25 §21 profile lists "email (verified)" as display-only + "password change". The plan title's "email
  change" is therefore covered by `POST /internal/auth/password/change` (the only "change" row), and changing a user's
  email address is not a v1 self-service feature. Record this (below) instead of inventing a migration.
- `IdentityRepo` already has `issue_email_token`, `consume_email_token` (single conditional UPDATE), `count_issued_since`,
  `set_password_hash`, `mark_email_verified` (first wins), `revoke_user_sessions(user_id, now, except_session_id=)` →
  token hashes; `PASSWORD_RESET_TTL` (1 h) exists in va_db.repositories.identity (check it is exported in
  `va_db.repositories.__init__`; add if not).
- `SessionManager._forget(hashes)` is private; reset/change revoke inside their own transaction and need it public.
- Pool size 1 per role: never open `global_session` inside another session; `sessions_of(...).revoke_all` opens its
  own, so reset/change use `IdentityRepo.revoke_user_sessions` in their transaction + `SessionManager.forget` after.
- `routers/auth.py` is 310 lines → put the three routes in a new module `routers/auth_password.py` registering on the
  same `internal_auth_router` (import it in `main.py` for side effects? No: better, make it explicit — see Files).

### Decisions to record (specs/04 §4.1, same commit, `> Decision (2026-10-29):`)
1. **Email change**: not offered in v1. Users cannot change their own address (specs/25 shows it read-only); the
   plan's "email change" maps to `password/change`. Support/admin tooling may change it later with a new token purpose.
2. **forgot** body `{email (3-320, trimmed)}`, unknown fields INVALID_INPUT; foreign Origin/Referer rejected as login
   (neither header passes). Always **200** `{data:{accepted:true}}` whatever the outcome. A reset email (template
   `password_reset`, link `{DASHBOARD_BASE_URL}/reset-password/{token}`, 1 h token) is queued as a background task only
   when the user exists, is `active`, and has had fewer than 3 `reset_password` tokens issued in the last hour
   (`count_issued_since(user, "reset_password", now - 1 h)`, DB-backed, no Redis key). Otherwise nothing is sent and
   `password_reset_suppressed` is logged with `reason` (`unknown_user` | `disabled` | `limit`) and never the address.
   Users without a password (SSO-only) may use it to set one. Delivery failures log `password_reset_email_failed`
   (template + error type only), as verification does.
3. **reset** body `{token (1-256), password (1-1024)}`, same Origin rule, no session needed. A malformed / unknown / used
   / expired / other-purpose token, or one whose user is not `active`, is `TOKEN_EXPIRED` 400 "Reset link is invalid or
   has expired." `field: "token"`. Order: `looks_like_token` → `peek_email_token` (read-only, so a policy failure does
   not burn the link) → user active → password policy with the user's email (`PASSWORD_TOO_WEAK` field `password`) →
   argon2 hash → ONE `global_session` txn: `consume_email_token` (None → TOKEN_EXPIRED, race loser), `set_password_hash`,
   `mark_email_verified(now)` (the link proves the address), `retire_email_tokens(user, "reset_password", now)` (other
   unused reset links die), `revoke_user_sessions(user, now)` (every session, including the caller's). After commit:
   `sessions.forget(hashes)`, `login_throttle.clear(user.email)`, clear both cookies (`clear_auth_cookies`), 200
   `{data:{revoked_sessions:int}}`. No session is issued (the user logs in with the new password).
4. **change** body `{current_password (1-1024), new_password (1-1024)}`. Session-authenticated (`current_session`:
   CSRF + Origin), MFA-gated (`active_membership` + `enforce_mfa`). **Recent auth** = `now - session.created_at <
   10 min` (a session is created only by login/signup; P02-T06 decides how MFA verification refreshes recency) else
   `REAUTH_REQUIRED` 401 "Sign in again to change your password." Then `login_throttle.check(user.email, ip)`
   (AUTH_LOCKED applies) → one `verify_async(user.password_hash, current_password)` (dummy hash for SSO-only) → wrong:
   `record_failure(email, ip)` + `INVALID_CREDENTIALS` 401 "Current password is incorrect." `field: "current_password"`
   → policy on `new_password` with the user's email (field `password` per §4.2 helper — keep the helper's field; note it)
   → hash → one `global_session` txn: `set_password_hash` + `revoke_user_sessions(user, now, except_session_id=current)`
   + `retire_email_tokens(user, "reset_password", now)` → after commit `forget(hashes)`; 200
   `{data:{revoked_sessions:int}}` (other sessions only; current cookie stays valid).
5. Audit actions `auth.password_reset` / `auth.password_changed` (§13) are written when the audit writer lands
   (P02-T11); the decision says so explicitly so T11 wires them.

### Files
**packages/va_db/src/va_db/repositories/identity.py** — add
  `async def peek_email_token(self, token: str, purpose: EmailTokenPurpose, now: datetime) -> uuid.UUID | None`
  (SELECT user_id with the same predicate as consume, no update) and
  `async def retire_email_tokens(self, user_id: uuid.UUID, purpose: EmailTokenPurpose, now: datetime) -> int`
  (UPDATE unused rows of that user/purpose SET used_at=now; rowcount). Share the predicate between peek/consume via a
  private `_usable_token(token, purpose, now) -> tuple[ColumnElement[bool], ...]` (jscpd). Export `PASSWORD_RESET_TTL`
  from `va_db.repositories` if missing.
**packages/va_common/src/va_common/email/templates.py** — `PASSWORD_RESET_MINUTES: Final = 60`, `password_reset(*,
  name, link, to) -> EmailMessage` (template `"password_reset"`, subject "Reset your VoiceAgents password", text + HTML
  with the same layout as verify_email: "Someone asked to reset the password … expires in 60 minutes … if it wasn't
  you, ignore this email; your password stays the same."). Factor the shared HTML shell into a private
  `_html_shell(greeting, paragraph, button_label, link, footer)` (or a `Template` with $body slots) so the two
  templates don't trip jscpd; `TEMPLATES` gains `"password_reset"`; export from `va_common.email`.
**apps/api/src/va_api/auth/sessions.py** — rename `_forget` → public `forget(token_hashes)` (update callers).
**apps/api/src/va_api/auth/password_reset.py** (new) — `RESET_HOURLY_LIMIT: Final = 3`, `RESET_LIMIT_WINDOW: Final =
  timedelta(hours=1)`, `REAUTH_WINDOW: Final = timedelta(minutes=10)`, `RESET_TOKEN_INVALID_MESSAGE`,
  `REAUTH_MESSAGE`, `CURRENT_PASSWORD_MESSAGE`, `reset_link(dashboard_base_url, token) -> str`,
  `async def send_password_reset(sender, *, to, name, token, dashboard_base_url) -> None`, `def require_recent_auth(
  active: ActiveSession, now: datetime) -> None`. Generalise verification's sender: move the try/log body into one
  helper (e.g. `va_api.auth.verification.deliver(sender, message, event: str)`) and have both senders call it (jscpd);
  likewise `verify_link`/`reset_link` via a shared `dashboard_link(base, path, token)`.
**apps/api/src/va_api/routers/auth_schemas.py** — `ForgotPasswordRequest{email: EmailField}`, `ForgotPasswordResult
  {accepted: bool}`, `ResetPasswordRequest{token, password: PasswordField}`, `ChangePasswordRequest{current_password:
  PasswordField, new_password: PasswordField}`; responses reuse `LogoutResult` (`{revoked_sessions}`) — rename it to
  `RevokedSessionsResult` only if the OpenAPI component name stays sensible; otherwise keep `LogoutResult`. Examples
  via json_schema_extra on every new model (internal.json requires examples). Token field type shared with
  VerifyEmailRequest (`EmailLinkToken` Annotated alias).
**apps/api/src/va_api/routers/auth_password.py** (new) — routes on `internal_auth_router`:
  `POST /password/forgot` (`auth_password_forgot`), `POST /password/reset` (`auth_password_reset`),
  `POST /password/change` (`auth_password_change`), all `openapi_extra=PUBLIC_MARKER`. Reuse `_check_dashboard_origin`
  by making it public in auth.py (`check_dashboard_origin`) or moving it to `va_api/auth/csrf.py`. Keep each handler
  radon ≤ B: split reset into `_reset_target(request, token) -> User` and `_apply_reset(request, token, user, hash)
  -> list[str]`; change into `_verify_current(...)`.
  Registration: `main.py` imports `va_api.routers.auth_password` so the routes attach before `include_router` (module
  import order matters: auth_password imports internal_auth_router from auth). Prefer an explicit
  `import va_api.routers.auth_password  # noqa: F401  (registers routes on internal_auth_router)` in
  `va_api/routers/auth.py`'s bottom? No — circular. Put it in main.py next to the auth import, with the reason comment.
  Test `test_internal_routes_registered` asserts the three paths are in `app.state.internal_openapi()`.
**openapi/internal.json** — regenerate with `make openapi`; openapi.json unchanged.
**specs/04** §4.1 decision (above). **specs/31** no new codes. **AGENTS.md** — reset/change notes (forgot always 200,
  recency from session created_at, auth_password module registration import).

### Tests
apps/api/tests/test_password_integration.py (conftest session fixtures: migrated Postgres, fakeredis, FrozenClock,
https client, `session_helpers.signup/login/link_token`; generalise `link_token(sender, email, template=...)` if it
assumes verify):
- `test_forgot_then_reset_flow`: forgot → 200 accepted; one `password_reset` email whose link starts with
  DASHBOARD_URL + `/reset-password/`; reset with new password → 200 revoked_sessions ≥ 1, cookies cleared; old session
  cookie → 401 on `/session`; login with old password 401, new password 200; `email_verified_at` set.
- `test_forgot_unknown_and_disabled_users`: 200 accepted, no email, log `password_reset_suppressed` reason, no address
  in logs.
- `test_forgot_hourly_limit`: 3 emails, 4th request 200 but no email (reason limit); clock +1 h → sent again.
- `test_reset_token_reuse_rejected`: second use → 400 TOKEN_EXPIRED field token; expired (clock +1 h +1 s) → expired;
  verify_email-purpose token rejected; garbage → expired.
- `test_reset_weak_password_keeps_token`: PASSWORD_TOO_WEAK (contains local part / short) then the same token works.
- `test_reset_retires_other_links`: two forgot emails; using the second kills the first.
- `test_reset_revokes_all_sessions`: two logins (two clients) → both invalid after reset; `sess:` keys gone.
- `test_reset_clears_login_lock`: lock the email with failures → reset → login succeeds.
- `test_change_password`: fresh login → change → 200 revoked_sessions == other sessions count; current session still
  works; other client 401; login with new password ok.
- `test_change_requires_recent_auth`: clock + 10 min + 1 s → 401 REAUTH_REQUIRED (and unchanged password).
- `test_change_wrong_current_password`: 401 INVALID_CREDENTIALS field current_password, counts toward the email lock
  (after limit → 429 AUTH_LOCKED).
- `test_change_requires_session_and_csrf` (no cookie 401, no CSRF 403), `test_change_mfa_gated` (org mfa_required →
  MFA_REQUIRED), `test_change_weak_new_password` (400 PASSWORD_TOO_WEAK).
- `test_password_routes_foreign_origin` (forgot/reset 403 with foreign Origin; no Origin ok).
packages/va_db tests (identity repo module): `peek_email_token` returns user without consuming, respects
  purpose/expiry/used; `retire_email_tokens` marks only unused rows of that user+purpose and returns the count.
packages/va_common tests: `password_reset` template renders text+HTML, escapes name, link present, template name, in
  TEMPLATES; verify_email output unchanged (snapshot equality against prior strings if a test exists).
apps/api/tests/test_password_reset.py (unit): reset_link joining, `require_recent_auth` boundary (exactly 10 min →
  reject? choose `>=` 10 min rejects; test both sides), send_password_reset logs on EmailDeliveryError.
apps/api/tests/test_openapi.py: internal document lists the three new paths with examples (existing generic checks
  should cover; add paths to the expected set).

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` then `uv run python scripts/check_openapi_drift.py`;
`uv run python scripts/check_spec_refs.py` (or the make target); `npx jscpd@4` (0 clones); `uvx radon cc -n C
apps/api/src packages/va_db/src packages/va_common/src` (none); `uvx pip-audit --strict`; `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files`. build.done payload MUST use the keys: tests, lint, typecheck, audit,
coverage, complexity, duplication, specs.

### Risks
- Route registration via a second module: if main.py doesn't import auth_password the routes silently vanish — test
  asserts the paths. Check the OpenAPI drift script loads the same app (it does via main).
- Pool size 1: reset txn must not call `sessions_of(...).revoke_all` or `throttle` inside the `global_session` block.
- `peek` then `consume` race: consume returns None for the loser → TOKEN_EXPIRED; hash computed already (fine).
- Forgot timing: the existing-user path does an extra DB write; acceptable (email itself is background). Do not
  return different status/body.
- Template refactor must not change verify_email output (existing tests compare it).
- policy helper raises with `field: "password"`; for change the field name stays `password` (document in decision) —
  or pass a field override if `check_password_policy_async` supports it; do not fork the policy.

## 2026-10-29 Builder: P02-T05 build notes
Implemented per plan. Files: va_db identity repo (`peek_email_token`, `retire_email_tokens`, shared
`_usable_email_token` predicate); va_common email templates refactored to one layout (`_Copy.render`), new
`password_reset` template (verify_email output verified byte-identical against a pre-change snapshot); va_api
`auth/password_reset.py` (limits, messages, `reset_link`, `send_password_reset`, `require_recent_auth`),
`auth/verification.py` (`dashboard_link`, `deliver(..., failure_event=)`), `SessionManager.forget` public,
`session_auth.check_dashboard_origin` (moved from routers/auth.py), `routers/auth_password.py` (3 routes, imported in
main.py for registration), `auth_schemas` (`EmailLinkToken`, Forgot/Reset/Change requests, `ForgotPasswordResult`,
`LogoutResult` renamed `RevokedSessionsResult`), openapi/internal.json regenerated, specs/04 §4.1 decision, AGENTS.md.
Deviation from plan (recorded in the decision): change's policy failure reports `field: "new_password"` (the body
field) by re-raising the policy AppError, instead of the helper's `password`. Successful change also clears the
email's login failure counter (as login does). Test helper `session_helpers.error_of` replaces per-module `_error`
(jscpd), `link_token(..., template="password_reset")`.
Gates: make fmt 0; make lint 0; make typecheck 0; make placeholders 0 (0 findings); make test 0 (2315 passed, 99.39 %);
check_openapi_drift both current; check_spec_refs 0; jscpd@4 0 clones; radon -n C none (avg A 1.91);
pip-audit --strict (uv export --no-emit-workspace, --no-deps --disable-pip) no known vulnerabilities; corepack pnpm
audit clean; pre-commit gitleaks passed. Coverage report shows partial lines in routers/auth_password.py (87 %) that
the integration tests do execute (same tracing artifact as routers/auth.py at 91 %).

## Review P02-T05 (2026-10-29): approved
Re-ran: make lint/typecheck/placeholders 0; make test 0 (2315 passed, 99.39 %); check_openapi_drift both current;
check_spec_refs 0; radon -n C none; jscpd@4 0 clones. Diff checked against specs/04 §4.1 rows (forgot always 200,
1 h token; reset revokes all sessions; change < 10 min recent auth) and specs/31 statuses (TOKEN_EXPIRED 400,
REAUTH_REQUIRED 401, INVALID_CREDENTIALS 401, PASSWORD_TOO_WEAK 400). Addresses never logged; tokens hash-only;
session revocation in the write txn with cache forget after commit (pool size 1 respected). Tests cover reuse, expiry,
purpose, race-free consume, retire, lock clearing, CSRF/MFA/origin. No findings.

## 2026-10-29 Planner: P02-T05 approved → P02-T06 split into T06a/T06b
T06 needs a platform key source (users have no org; specs/28 (h) says `platform_encrypt` is unused until one
exists), a schema change (recent auth "since login or MFA" needs an MFA time on the session), new deps (pyotp in the
specs/02 list; segno for `qr_svg` is new), and four endpoints plus a gate change. Split: T06a primitives + data
layer, T06b endpoints + enforcement. Design for both is below so T06b's planner inherits it.

### T06b design carried forward (do not build in T06a)
- Routes in new `routers/auth_mfa.py` on `internal_auth_router` (import for side effect in main.py like
  auth_password), all session-authenticated (`CurrentSession`: CSRF + Origin), NOT MFA-gated (§4.3 exempts
  `/internal/auth/mfa/*`). setup `{}` → CONFLICT if `mfa_enabled`; new secret replaces any pending one; returns
  `{secret_base32, otpauth_uri, qr_svg}`. enable `{code}` → CONFLICT without a pending secret / when enabled; verify
  TOTP; 10 codes hashed; conditional enable (secret blob unchanged); marks the current session MFA-passed; returns
  `{mfa_enabled:true, recovery_codes:[10]}`. verify `{code}` xor `{recovery_code}` (both/neither INVALID_INPUT);
  CONFLICT "Multi-factor authentication is not enabled." when not enrolled; success `set_mfa_passed(now)` +
  `remember` the updated ActiveSession; returns `{mfa_passed:true, recovery_codes_remaining:int}`. disable `{code}`
  (TOTP only, per spec) → FORBIDDEN when ANY org of the user has `mfa_required`; clears secret/codes/flag.
- Wrong code → INVALID_MFA_CODE 401 "Invalid or expired code." field `code`/`recovery_code`. Brute force: separate
  per-user counter `auth:fail:mfa:<user_id>` 10 failures / 15 min → AUTH_LOCKED with Retry-After (NOT the email
  counter, because a successful password login clears that and would allow unlimited MFA guesses); cleared on MFA
  success. Generalise LoginThrottle's counter logic rather than copying it (jscpd).
- Replay: `mfa:last:<user_id>` (no braces, like auth:verify:resend) TTL 90 s, Lua compare-and-set "accept only a
  counter greater than the stored one"; Redis down → log `mfa_replay_guard_unavailable`, accept (consistent with
  the other Redis-degraded paths; documented). Add both keys to specs/03 §11.
- Gate: `enforce_mfa` fires when `(org.mfa_required OR user.mfa_enabled) AND NOT session.mfa_passed` (otherwise a
  personally enabled MFA protects nothing: login only *tells* the dashboard to prompt). Needs
  `MembershipRecord.user_mfa_enabled` (join users in `_joined`). `/session.mfa_required` uses the same rule.
- Recent auth: `now - max(created_at, mfa_passed_at) < 10 min`; ActiveSession + cache JSON gain `mfa_passed_at`
  (missing key in an old cache entry → treat as None via `.get`). Update the specs/04 §4.1 change decision text.
- Audit `auth.mfa_enabled`/`auth.mfa_disabled` wired by P02-T11 (say so in the decision).

## Plan P02-T06a — MFA primitives (specs/04 §4.5, specs/28 §3, specs/03 §3)

### Facts found while planning
- `users.mfa_totp_secret_enc bytea`, `mfa_enabled bool`, `mfa_recovery_codes_hash text[] DEFAULT '{}'` already exist
  (model `va_db.models.tenancy.User`); `sessions.mfa_passed bool` exists; `IdentityRepo.set_mfa_passed(session_id)`
  exists and is unused outside tests (grep before changing; update its tests).
- `Envelope.platform_encrypt/decrypt` use org `NIL_ORG_ID`; `OrgDataKeyStore.active_data_key(NIL)` returns None so
  `platform_encrypt` currently raises CryptoError. Decryption only needs the blob's embedded wrapped key.
- `schema_catalog` compares columns including `attnum`: a column added by `ALTER TABLE ... ADD COLUMN` lands last,
  so the specs/03 `CREATE TABLE sessions` must list the new column LAST (after `updated_at`).
- `test_migrations.py` pins 0001/0002 offline SQL; spec blocks must hold only CREATE statements (fine).
- `PasswordHasher` (va_common.auth.passwords) has sync `hash`/`verify` and `*_async` via to_thread; one argon2 call
  is ~64 MiB, so never run 10 in parallel threads.
- pyotp is in the specs/02 dependency list; segno is not.

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. specs/04 §4.5 — **TOTP**: secret = `pyotp.random_base32(32)` (160 bits); `otpauth_uri` =
   `pyotp.TOTP(secret).provisioning_uri(name=<email>, issuer_name="VoiceAgents")`; a submitted code is stripped of
   whitespace and must be exactly 6 ASCII digits (else no match); matching compares (constant time,
   `hmac.compare_digest`) against the codes of time steps `T-1, T, T+1` at the injected clock's `now` and returns
   the matched step counter (the replay guard in T06b stores it). **QR**: `qr_svg` is an inline SVG of the
   otpauth URI made with `segno` (pure Python, no native deps; error level M, scale 4, no XML declaration), added as
   a dependency because pyotp has no renderer. **Recovery codes**: 10 distinct codes, 12 characters from the
   lowercase RFC 4648 base32 alphabet (`a-z2-7`, `secrets.choice`, 60 bits) shown as `xxxx-xxxx-xxxx`; input is
   normalised by casefolding and dropping whitespace and `-`, must then be 12 base32 characters, and is re-hyphenated
   before hashing/verifying; stored as argon2id hashes (§4.2 params) of the canonical hyphenated form; hashing and
   matching run sequentially in one worker thread; consumption is one conditional
   `UPDATE users SET mfa_recovery_codes_hash = array_remove(..., :hash) WHERE id = :id AND mfa_enabled AND :hash =
   ANY(...)` so a code is accepted at most once even under concurrent requests.
2. specs/28 §3 — continues the list: **(j)** the platform (nil org) has no stored data key; `platform_encrypt`
   generates a fresh KMS data key per blob (`provider.generate_data_key(NIL_ORG_ID)`, local: AAD
   `org_id=00000000-…`, AWS: encryption context with the nil org id) and embeds it, so `platform_decrypt` works
   from the blob alone (unwrapped keys go through the same cache). `users.mfa_totp_secret_enc` uses it with AAD
   `users.mfa_totp_secret_enc:<user_id>` (users belong to no single org, and a user leaving an org must not lose
   MFA). Amend (h)'s last sentence to point at (j).
3. specs/03 §3 — `sessions.mfa_passed_at timestamptz` (NULL until MFA is passed; SSO in P02-T07 sets it at login),
   added by migration `0003_session_mfa_passed_at`; it lets "recent auth" (specs/04 §4.1 change) count from MFA
   verification.

### Files
**pyproject (packages/va_common/pyproject.toml)** — `uv add --package va-common pyotp segno` (current releases);
  lock updated. Check `mypy --strict`: pyotp ships no stubs? If not typed, add a mypy override
  `ignore_missing_imports` for `pyotp` only in the existing mypy config section (or `types-` package if one exists);
  segno is typed (`py.typed`). Wrap untyped returns with explicit `str(...)`/typed locals.
**packages/va_common/src/va_common/auth/mfa.py** (new):
  - `TOTP_DIGITS: Final = 6`, `TOTP_INTERVAL_S: Final = 30`, `TOTP_VALID_WINDOW: Final = 1`,
    `TOTP_ISSUER: Final = "VoiceAgents"`, `RECOVERY_CODE_COUNT: Final = 10`, `RECOVERY_CODE_ALPHABET: Final =
    "abcdefghijklmnopqrstuvwxyz234567"`.
  - `def new_totp_secret() -> str`
  - `def otpauth_uri(secret: str, account: str) -> str`
  - `def qr_svg(data: str) -> str`
  - `def totp_counter(now: datetime) -> int` (`int(now.timestamp()) // 30`; aware datetimes only, ValueError on naive)
  - `def totp_code(secret: str, counter: int) -> str` (pyotp `generate_otp(counter)`; used by tests and matching)
  - `def match_totp(secret: str, code: str, now: datetime) -> int | None` — the matched counter or None; checks all
    three steps (no early return on the first comparison) to keep timing flat.
  - `def new_recovery_codes() -> list[str]` (10 distinct, hyphenated)
  - `def normalise_recovery_code(raw: str) -> str | None`
  - `async def hash_recovery_codes(hasher: PasswordHasher, codes: Sequence[str]) -> list[str]` (one
    `asyncio.to_thread` running `hasher.hash` sequentially)
  - `async def match_recovery_code(hasher: PasswordHasher, hashes: Sequence[str], raw: str) -> str | None` — the
    stored hash the normalised code matches (one to_thread, sequential `hasher.verify`), None when malformed or no
    match (a malformed input still costs nothing; that is fine — the format is public).
  Export from `va_common.auth.__init__` (`__all__` sorted).
**packages/va_common/src/va_common/crypto/envelope.py** — `platform_encrypt`: `data_key = await
  self._provider.generate_data_key(NIL_ORG_ID)`; cache put; `_seal(NIL_ORG_ID, data_key.wrapped, ...)`. Add
  `platform_encrypt_str`/`platform_decrypt_str` only if T06b needs them — it does (secret is text): add both,
  sharing the UTF-8 decode with `decrypt_str` via a private `_utf8(plaintext)` helper (jscpd). Docstrings say why.
**packages/va_db/migrations/versions/0003_session_mfa_passed_at.py** — revision "0003", down "0002"; upgrade
  `op.execute(sa.text("ALTER TABLE sessions ADD COLUMN mfa_passed_at timestamptz"))`; downgrade drops it. No
  grants needed (existing table; column privileges follow the table grant).
**specs/03-data-model.md** — `sessions` DDL: add `mfa_passed_at timestamptz` as the LAST column with a short
  comment; decision note (3).
**packages/va_db/src/va_db/models/tenancy.py** — `Session.mfa_passed_at: Mapped[datetime | None] =
  mapped_column(DateTime(timezone=True))` (match existing timestamp column style; autogenerate parity must stay []).
**packages/va_db/src/va_db/repositories/identity.py** (watch file length; split MFA writes into
  `repositories/identity_mfa.py` class `MfaRepo(session)` if identity.py would pass ~330 lines — preferred):
  - `set_mfa_passed(session_id, now) -> None` sets `mfa_passed=True, mfa_passed_at=now` (move/keep in IdentityRepo).
  - `MfaRepo.begin_setup(user_id, secret_enc: bytes) -> bool` — UPDATE users SET secret=:enc WHERE id AND NOT
    mfa_enabled; rowcount==1.
  - `MfaRepo.enable(user_id, secret_enc: bytes, code_hashes: list[str]) -> bool` — UPDATE SET mfa_enabled=true,
    mfa_recovery_codes_hash=:hashes WHERE id AND NOT mfa_enabled AND mfa_totp_secret_enc = :enc.
  - `MfaRepo.disable(user_id) -> bool` — UPDATE SET mfa_enabled=false, secret NULL, hashes '{}' WHERE id AND
    mfa_enabled.
  - `MfaRepo.consume_recovery_code(user_id, code_hash: str) -> bool` — the array_remove conditional UPDATE.
  - `MfaRepo.replace_recovery_codes(user_id, code_hashes) -> bool` is NOT needed (no regenerate endpoint in spec);
    don't add it.
  Export `MfaRepo` from `va_db.repositories`. It is not a system repo (users are global, `global_session`), so the
  specs/03 §2 `va_system` list is unchanged.
**specs/04** §4.5 decision (1); **specs/28** §3 decision (2); **AGENTS.md** — mfa module, platform_encrypt per-blob
  key, migration 0003 column-last rule (attnum), segno dep.

### Tests
packages/va_common/tests/test_mfa.py:
- `test_new_totp_secret_is_32_base32_chars_and_unique`.
- `test_otpauth_uri` (parse with urllib: scheme otpauth, host totp, label `VoiceAgents:<email>` url-encoded,
  `secret`, `issuer=VoiceAgents`; pyotp.parse_uri round-trip gives same secret/digits 6/interval 30).
- `test_qr_svg_is_svg` (starts with `<svg`, no `<?xml`, contains `path`; deterministic for same input).
- `test_match_totp_window` parametrized at a fixed aware datetime: code of T-1, T, T+1 → that counter; T-2, T+2 →
  None; boundary second (timestamp exactly at a step start and step_start-1).
- `test_match_totp_rejects_malformed` ("", "12345", "1234567", "12a456", full-width digits "１２３４５６") → None;
  `" 123 456 "` whitespace stripped matches when it is the right code.
- `test_totp_counter_requires_aware_datetime`.
- `test_new_recovery_codes_format_and_distinct` (regex `^[a-z2-7]{4}-[a-z2-7]{4}-[a-z2-7]{4}$`, len 10, set len 10).
- `test_normalise_recovery_code` (upper-case, spaces, no hyphens → canonical; wrong length / `0`/`1`/`8` chars → None).
- `test_hash_and_match_recovery_codes` with a cheap `Argon2Params` hasher (existing test helper/fixture if one
  exists in test_passwords.py — reuse it): matching returns the right stored hash; wrong code None; malformed None;
  hashes are argon2id strings and never contain the code.
packages/va_common/tests/test_crypto_envelope.py: `platform_encrypt` round-trips with a store that has no keys
  (existing fake); two encryptions embed different wrapped keys; wrong AAD → DecryptionError; decrypt with an
  envelope whose cache is empty (fresh Envelope over same provider) works; `platform_*_str` round-trip and
  invalid UTF-8 → DecryptionError (reuse existing str test style).
packages/va_db tests:
- migration: offline upgrade SQL contains `ALTER TABLE sessions ADD COLUMN mfa_passed_at timestamptz` and the
  `0003` alembic_version update; downgrade 0003→0002 drops it (offline SQL); existing schema-vs-spec and autogenerate
  parity tests cover the rest (run them).
- `test_identity_mfa.py` (identity_db fixture): begin_setup on a non-enrolled user stores the blob, refuses when
  enabled; enable needs the same blob (a replaced secret → False) and sets hashes; disable clears all three and
  returns False when not enabled; consume_recovery_code true once then false; concurrent consumption of one hash
  from two sessions sequentially-committed (pool size 1 → do two transactions back to back, second False; also
  assert other hashes survive); consume when MFA disabled → False; `set_mfa_passed` sets both columns.

### Gates
`make fmt lint typecheck placeholders test`; `uv run python scripts/check_spec_refs.py`; `npx jscpd@4` (0 clones);
`uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none); pip-audit per memory
mem-1793267153-9b68 (new deps!); `corepack pnpm audit`; `uv run pre-commit run gitleaks --all-files`;
`make openapi` + drift check (should be unchanged). build.done payload keys: tests, lint, typecheck, audit,
coverage, complexity, duplication, specs.

### Risks
- pyotp typing under mypy --strict (override narrowly; don't blanket-ignore).
- segno API: verify `segno.make(data, error="m").svg_inline(scale=4)` exists in the installed version; otherwise
  `save(BytesIO, kind="svg", xmldecl=False, svgns=True, scale=4)`.
- Column order: new column must be last in the spec DDL or test_schema_matches_spec fails on attnum.
- `test_offline_upgrade_creates_every_model_table` etc. must still pass with head 0003 (only CREATE TABLEs from 0001).
- Don't change verify behaviour of `Envelope.encrypt` for real orgs; only the nil-org path.
- Pool size 1 per role: never nest sessions in the repo tests.

## 2026-10-29 Builder: P02-T06a build notes
Implemented per plan. Files: `va_common/auth/mfa.py` (new; exported from `va_common.auth`), `crypto/envelope.py`
(`platform_encrypt` = fresh nil-org data key per blob, `platform_encrypt_str`/`platform_decrypt_str`, shared `_utf8`),
pyotp 2.10.0 + segno 1.6.6 (both ship py.typed, no mypy override needed), migration `0003_session_mfa_passed_at`,
`Session.mfa_passed_at` (`sort_order=1` so the model order matches the DB/spec: column after updated_at),
`IdentityRepo.set_mfa_passed(session_id, now)` (callers updated: va_db + api session test), new
`repositories/identity_mfa.py` `MfaRepo` (begin_setup/enable/disable/consume_recovery_code; `:hash = ANY(...)` via
`literal(h, Text) == any_(col)` because `Mapped[list[str]].any()` fails mypy). Specs: 04 §4.5 Decision, 28 §3 (h)
amended + (j), 03 §3 DDL column + Decision, 02 dependency list gains `segno`. Tests: test_mfa.py, envelope platform
tests, test_identity_mfa_integration.py, test_session_mfa_migration.py, tenancy model column list, migrations head
0003. Added `migration_helpers.offline_sql` and switched test_seed_plans to it (no copied `_offline`).
Gates: make fmt 0; lint 0; typecheck 0; placeholders 0 findings; make test 0 (2365 passed, 99.39 %); spec refs 0;
openapi drift current; jscpd 0 clones; radon -n C none (avg A 1.77 on auth+repos); pip-audit + pnpm audit clean;
gitleaks passed.

## Review P02-T06a (2026-10-29): approved
Re-ran: make lint/typecheck/placeholders exit 0; make test 2365 passed, 99.39 %; check_spec_refs 0; jscpd 0 clones.
Diff checked against specs/04 §4.5, specs/28 §3 (h)/(j), specs/03 §3: TOTP ±1 step constant-time no-early-return,
recovery code format/normalisation/argon2id sequential in one thread, conditional UPDATEs (enable bound to verified
blob; array_remove consumption is safe under READ COMMITTED re-evaluation), platform_encrypt per-blob nil-org key
(DataKeyCache bounded at 10k entries, so per-blob puts cannot grow it without limit), migration 0003 column-last.
Recovery-code concurrency is tested sequentially (pool size 1). T06b design is carried forward above.

## 2026-10-29 Planner: P02-T06a approved → next P02-T06b

## Plan P02-T06b — MFA endpoints and enforcement (specs/04 §4.1 `mfa/*` rows, §4.3, §4.5; specs/03 §11; specs/31)

Builds on the "T06b design carried forward" notes above. Where this plan differs, this plan wins.

### Facts found while planning
- Primitives exist: `va_common.auth.mfa` (`new_totp_secret`, `otpauth_uri`, `qr_svg`, `totp_counter`, `totp_code`,
  `match_totp(secret, code, now) -> int|None`, `new_recovery_codes`, `hash_recovery_codes(hasher, codes)`,
  `match_recovery_code(hasher, hashes, raw) -> str|None`), `Envelope.platform_encrypt_str(plaintext, aad)` /
  `platform_decrypt_str(blob, aad)`, `va_db.repositories.MfaRepo(session)` (`begin_setup`, `enable(user_id,
  secret_enc, hashes)`, `disable`, `consume_recovery_code`), `IdentityRepo.set_mfa_passed(session_id, now)`.
  No AAD helper exists yet.
- Current gate: `session_auth.enforce_mfa(record, active)` (org rule only), called after `active_membership` in
  `session_principal`, `logout_all`, `resend_verification`, `password_change`. `GET /session` computes
  `mfa_required` inline. `require_recent_auth` (auth/password_reset.py) uses `created_at` only.
- `MembershipRecord` built only in `memberships._record` (+ expected values in
  packages/va_db/tests/test_membership_lookup_integration.py). `va_system` has SELECT on `users` (0001 GRANTS).
- Container already has `envelope`; test `SessionContainer` does not — add `envelope = identity.envelope`.
- fakeredis[lua] is installed (Lua scripts work in tests); `FakeServer(); server.connected=False` for Redis-down.
- Validation errors → `INVALID_INPUT` per pydantic error via `errors.validation_items` (a `model_validator`
  ValueError message becomes the message; check how `field` is derived for model-level errors and set the message
  without the "Value error, " prefix — `_VALUE_ERROR_PREFIX` is stripped already).

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. specs/04 §4.5 (new decision after the existing one) — **MFA endpoints** (envelope, `x-public: "session-flow"`,
   session-authenticated with CSRF + `Origin`, exempt from the MFA gate per §4.3):
   - **setup** optional `{}` body. `CONFLICT` 409 "Multi-factor authentication is already enabled." when enabled.
     Otherwise a new secret is encrypted (AAD `users.mfa_totp_secret_enc:<user_id>`) and stored as the pending
     secret, replacing any earlier one (`MfaRepo.begin_setup`; a lost race with a concurrent enable → the same
     CONFLICT). 200 `{data:{secret_base32, otpauth_uri, qr_svg}}`, account label = the user's email.
   - **enable** `{code (1-32 chars)}`, unknown fields INVALID_INPUT. CONFLICT "Multi-factor authentication is
     already enabled." / "Start multi-factor setup first." (no pending secret). Code checked as in "Code checks"
     below; then 10 recovery codes are generated and hashed, `MfaRepo.enable` applies only if the pending blob is
     unchanged (else CONFLICT "Multi-factor setup changed; start again."), the current session is marked
     MFA-passed (`mfa_passed=true`, `mfa_passed_at=now`). 200 `{data:{mfa_enabled:true, recovery_codes:[10]}}`
     (shown once; never logged).
   - **verify** `{code}` or `{recovery_code}` (each 1-64 chars) — exactly one, else INVALID_INPUT "Send either
     code or recovery_code." CONFLICT "Multi-factor authentication is not enabled." when not enrolled. A recovery
     code is matched against the stored hashes and spent with the conditional UPDATE (losing a race =
     INVALID_MFA_CODE). Success marks the session MFA-passed and clears the MFA failure counter. 200
     `{data:{mfa_passed:true, recovery_codes_remaining:int}}`.
   - **disable** `{code}` (TOTP only; a recovery code cannot disable). CONFLICT "…not enabled." when not enrolled;
     FORBIDDEN 403 "Multi-factor authentication is required by one of your organizations." when ANY org the user
     belongs to has `settings.mfa_required` true (checked before the code, so no code is spent). Then code check,
     `MfaRepo.disable` (clears secret, codes, flag; lost race → CONFLICT not enabled), `mfa:last` deleted. The
     session keeps `mfa_passed`. 200 `{data:{mfa_enabled:false}}`.
   - **Code checks**: the per-user counter `auth:fail:mfa:<user_id>` (10 failures / 15 min → `AUTH_LOCKED` 429 "Too
     many invalid codes; try again in N seconds." with `Retry-After`, same INCR/EXPIRE NX/restart-on-limit
     mechanics as the login throttle; locked attempts not counted; checked before any code comparison) is
     separate from the email login counter because a successful password login clears that one. A wrong,
     malformed, or replayed code is `INVALID_MFA_CODE` 401 "Invalid or expired code." with `field` `code` or
     `recovery_code`, and counts as a failure. **Replay**: after a TOTP match, `mfa:last:<user_id>` is advanced by a
     Lua compare-and-set that accepts only a counter greater than the stored one (`SET … EX 90`); an equal or older
     counter is a replay. Redis unavailable: counters and the replay guard log `mfa_guard_unavailable` and allow
     (consistent with the other Redis-degraded auth paths; the ±1 step window bounds the exposure to 90 s).
   - Audit actions `auth.mfa_enabled`/`auth.mfa_disabled` are written once the audit writer exists (P02-T11).
2. specs/04 §4.3 — amend the "MFA gate" sentence in the session-mechanics decision (append a new short decision
   rather than rewriting history, and reference it): the gate fires when **the active org requires MFA OR the user
   has MFA enabled** and the session has not passed MFA (a personally enabled MFA must protect the account, not
   only prompt the dashboard). A user with MFA enabled and no membership is gated on the `/internal/auth/*` routes
   that are gated (logout-all, resend-verification, password change). `GET /session.mfa_required` follows the same
   rule. Cache JSON gains `mfa_passed_at` (an older entry without it reads as null).
3. specs/04 §4.1 password change — append a decision: recent auth = less than 10 minutes since
   `max(sessions.created_at, sessions.mfa_passed_at)`; MFA verification (and enable) refresh it. (Reference the
   earlier "decided with MFA" sentence.)
4. specs/03 §11 — add rows `auth:fail:mfa:{user_id}` (counter, 900 s, MFA code failures per user, specs/04 §4.5) and
   `mfa:last:{user_id}` (string counter, 90 s, last accepted TOTP step, replay guard, specs/04 §4.5); `sess:` row
   lists `mfa_passed_at`.

### Files
**apps/api/src/va_api/auth/failure_counter.py** (new) — generalise LoginThrottle's mechanics (jscpd):
  `class FailureCounters` with `__init__(self, redis: Redis, *, window_s: int, unavailable_event: str,
  locked_message: Callable[[int], str])`; `async check(self, counters: Sequence[tuple[str, int]]) -> None`
  (AUTH_LOCKED + Retry-After); `async record(self, counters) -> None`; `async clear(self, key: str) -> None`.
  Same Redis error handling (`RedisError, OSError` → warning with `error=type(exc).__name__`).
**apps/api/src/va_api/auth/login_throttle.py** — keep its public API, key helpers, constants, and log event
  (`login_throttle_unavailable`); delegate to `FailureCounters`. Existing test_login_throttle.py must pass unchanged.
**apps/api/src/va_api/auth/mfa_guard.py** (new):
  - constants `MFA_FAILURE_LIMIT = 10`, `MFA_FAILURE_WINDOW_S = 900`, `MFA_REPLAY_TTL_S = 90`; messages
    `INVALID_CODE_MESSAGE = "Invalid or expired code."`, `mfa_locked_message(n)` ("Too many invalid codes; try again
    in {seconds_phrase(n)}."), `NOT_ENABLED_MESSAGE`, `ALREADY_ENABLED_MESSAGE`, `SETUP_FIRST_MESSAGE`,
    `SETUP_CHANGED_MESSAGE`, `ORG_REQUIRES_MFA_MESSAGE`.
  - `def mfa_failure_key(user_id) -> str` (`auth:fail:mfa:<id>`), `def mfa_last_key(user_id) -> str`
    (`mfa:last:<id>`), `def mfa_secret_aad(user_id) -> str` (`users.mfa_totp_secret_enc:<id>`).
  - `REPLAY_GUARD_LUA: Final` (GET; if stored and tonumber(stored) >= tonumber(ARGV[1]) return 0; SET KEYS[1]
    ARGV[1] EX ARGV[2]; return 1) — ruff S105 noqa after closing quotes if flagged (memory mem-1793258976-544c).
  - `class MfaGuard(redis)`: `check(user_id)`, `record_failure(user_id)`, `clear(user_id)` (via FailureCounters,
    event `mfa_guard_unavailable`), `async claim_step(user_id, counter) -> bool` (registered script; Redis error →
    warn `mfa_guard_unavailable`, return True), `async forget_steps(user_id)` (DEL mfa:last; errors logged).
  - `def invalid_code(field: str) -> AppError` (INVALID_MFA_CODE, field).
**apps/api/src/va_api/auth/sessions.py** — `ActiveSession.mfa_passed_at: datetime | None` (field after
  `mfa_passed`); `of` reads `row.mfa_passed_at`; `to_json` writes ISO or None; `from_json` uses
  `data.get("mfa_passed_at")`. `SessionManager.mark_mfa_passed(active) -> ActiveSession` (DB `set_mfa_passed(now)` in
  global_session, `replace(..., mfa_passed=True, mfa_passed_at=now)`, `remember`). Fix every `ActiveSession(...)`
  construction in tests (grep).
**apps/api/src/va_api/auth/password_reset.py** — `require_recent_auth`: reference time `max(created_at,
  mfa_passed_at)` when set; docstring/module docstring updated.
**packages/va_db/src/va_db/repositories/memberships.py** — `MembershipRecord.user_mfa_enabled: bool = False`;
  `_joined` selects `Membership, Org, User.mfa_enabled` joined on `User.id == Membership.user_id`; `_record` gets
  it. Update test_membership_lookup_integration.py expectations + a test that an enrolled user reads true.
**apps/api/src/va_api/auth/session_auth.py**:
  - `def mfa_due(*, org_requires: bool, user_enabled: bool, passed: bool) -> bool`.
  - replace `enforce_mfa(record, active)` by `async def gated_membership(request, active) ->
    tuple[ActiveSession, MembershipRecord | None]`: `active_membership`, then when `record is None` load the user's
    `mfa_enabled` via `IdentityRepo.get_user` (global_session; missing user → AUTH_FAILURE SESSION_INVALID), raise
    MFA_REQUIRED when `mfa_due(...)`. Keep the message constant. Use it in `session_principal`, `logout_all`,
    `resend_verification`, `password_change` (removes the repeated 2-line pattern). Remove `enforce_mfa` from
    `__all__` and callers (grep tests).
  - accessors `envelope_of(request) -> Envelope`, `mfa_guard_of(request) -> MfaGuard`.
**apps/api/src/va_api/routers/auth.py** — `get_session`: `mfa_required=mfa_due(org_requires=record is not None and
  record.mfa_required, user_enabled=user.mfa_enabled, passed=active.mfa_passed)`.
**apps/api/src/va_api/routers/auth_mfa_schemas.py** (new; keeps auth_schemas.py < 300 lines): `MfaCodeRequest`
  (`code: str` 1-32, extra forbid — reuse the base config used by `EmptyBody`/`LoginRequest`), `MfaVerifyRequest`
  (`code: str | None = None` 1-64, `recovery_code: str | None = None` 1-64, `model_validator(mode="after")` exactly
  one), results `MfaSetupResult`, `MfaEnableResult`, `MfaVerifyResult`, `MfaDisableResult` with
  `json_schema_extra` examples like the existing schemas (no secrets that look real: use an obviously fake base32
  example; check `check_no_placeholders`/gitleaks don't flag it).
**apps/api/src/va_api/routers/auth_mfa.py** (new) — four routes on `internal_auth_router`, operation ids
  `auth_mfa_setup|enable|verify|disable`, `openapi_extra=PUBLIC_MARKER`, `active: CurrentSession`. Private helpers:
  `_load_user(request, active) -> User` (AUTH_FAILURE when gone), `_pending_secret(request, user) -> tuple[bytes,
  str]` (decrypt), `_check_totp(request, user_id, secret, code, field="code") -> None` (guard.check → match_totp at
  clock.now → None: record_failure + invalid_code; claim_step False: record_failure + invalid_code; success:
  guard.clear), `_spend_recovery_code(request, user, raw) -> int` (guard.check → match_recovery_code(hasher,
  user.mfa_recovery_codes_hash, raw) → consume in global_session; failure paths as above; returns remaining
  count = len(hashes) - 1). Keep each function radon A/B. Mark-passed via `sessions_of(request).mark_mfa_passed`.
**apps/api/src/va_api/main.py** — `import va_api.routers.auth_mfa  # noqa: F401  (registers routes)` like
  auth_password.
**apps/api/src/va_api/container.py** — `mfa_guard: MfaGuard` field, built `MfaGuard(redis)`; docstring list.
**apps/api/tests/support/session_helpers.py** — `SessionContainer` gains `envelope: Envelope = field(init=False)`
  (= identity.envelope) and `mfa_guard: MfaGuard = field(init=False)`; add helper `async def enrol_mfa(client,
  container) -> tuple[str, list[str]]` (setup + enable with `totp_code(secret, totp_counter(clock.now()))`, returns
  secret and recovery codes) used by several tests (avoid jscpd copies).
**openapi/internal.json** — regenerate (`make openapi`); public `openapi.json` must not change.
**specs** per decisions; **AGENTS.md** — MFA routes module, MfaGuard keys, gate rule, FailureCounters reuse.

### Tests
apps/api/tests/test_failure_counter.py (or extend test_login_throttle.py only if small): lock at limit with
  Retry-After from TTL, window restart on the locking failure, clear, Redis down allows + logs event.
apps/api/tests/test_mfa_guard.py (fakeredis): `claim_step` accepts increasing counters, rejects equal and lower,
  key TTL ≤ 90; Redis down → True and `mfa_guard_unavailable` logged (structlog capture used elsewhere); failure
  lock after 10 → AUTH_LOCKED message "Too many invalid codes; try again in 900 seconds."; `clear` resets; key
  helpers exact strings; `forget_steps` deletes.
apps/api/tests/test_sessions*.py / test_password_reset.py: `ActiveSession` JSON round-trip with and without
  `mfa_passed_at` (old cache entry missing the key → None); `require_recent_auth` passes 9 min after MFA on a 1 h
  old session and fails 10 min after MFA; `mark_mfa_passed` updates DB + cache (integration via client is fine).
apps/api/tests/test_mfa_enrolment_integration.py (client fixture, FrozenClock):
  - setup returns secret (32 base32), otpauth URI with the email, `<svg`; DB secret is encrypted (not equal to the
    secret bytes) and decrypts with the AAD; `mfa_enabled` false; second setup replaces the secret.
  - setup/enable/verify/disable need CSRF + Origin (one parametrized test: missing CSRF → FORBIDDEN) and a session
    (no cookie → AUTH_FAILURE).
  - enable without setup → CONFLICT; wrong code → INVALID_MFA_CODE field code; right code → 10 codes matching the
    format, user enabled, `GET /session` shows `mfa_passed` true; enable again → CONFLICT; setup when enabled →
    CONFLICT.
  - enable with a code from a secret replaced by a later setup → INVALID_MFA_CODE (code checked against the
    current pending secret).
  - unknown body fields → INVALID_INPUT.
apps/api/tests/test_mfa_verify_integration.py:
  - after enrol: login again in a fresh client → `login.mfa_required` true; probe route (SESSION_PROBE_PATH) →
    MFA_REQUIRED 401 (user-enabled MFA, org not requiring); `GET /session` 200 with `mfa_required` true;
    `logout-all` → MFA_REQUIRED; `logout` works.
  - verify with the current code → 200, probe now 200; same code again in another fresh session within the window
    → INVALID_MFA_CODE (replay); next step's code (clock advanced 30 s) accepted.
  - verify with a recovery code (upper-case, no hyphens accepted) → remaining 9; reuse → INVALID_MFA_CODE field
    recovery_code; both/neither fields → INVALID_INPUT.
  - verify when not enrolled → CONFLICT.
  - 10 wrong codes → 11th attempt (even with the right code) AUTH_LOCKED with Retry-After; counter cleared by a
    success (9 wrong, 1 right, then 9 wrong still not locked).
  - recent auth: session created, clock +30 min, password change → REAUTH_REQUIRED; MFA verify; change → 200.
  - org `mfa_required` gate still works for an unenrolled user (existing tests cover; keep green) and setup/enable
    are reachable while gated (exempt) and enabling unlocks the probe.
  - disable: with org requiring MFA in ANY membership (add a second org membership requiring it, via make_member +
    a membership insert in system_session) → FORBIDDEN and nothing changes; otherwise wrong code →
    INVALID_MFA_CODE; right code (next step to avoid replay) → `mfa_enabled` false, secret NULL, codes `{}`;
    disable again → CONFLICT; a recovery code in the `code` field → INVALID_MFA_CODE.
  - user with MFA enabled and no membership: password change → MFA_REQUIRED before verify.
packages/va_db/tests/test_membership_lookup_integration.py: `user_mfa_enabled` reflects users.mfa_enabled.
Keep each new test file < ~350 lines; share setup through session_helpers (`enrol_mfa`) and conftest fixtures (no
copied fixtures — jscpd).

### Gates
`make fmt lint typecheck placeholders test`; `uv run python scripts/check_spec_refs.py`; `make openapi` + drift check
(internal.json changes, openapi.json unchanged); `npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src
packages/va_db/src apps/api/src` (none); pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files`. build.done payload keys: tests, lint, typecheck, audit, coverage,
complexity, duplication, specs.

### Risks
- Refactoring LoginThrottle must not change its behaviour or log event names (existing tests are the oracle).
- Removing `enforce_mfa` touches 4 call sites + tests; grep `enforce_mfa` in apps/api/tests.
- argon2 with real params runs 10× in enable; tests use CHEAP_ARGON2 via SessionContainer — fine.
- FrozenClock: TOTP codes in tests must be computed from `container.clock.now()`; advance by 30 s between uses of
  "the next code" to avoid the replay guard; the replay key TTL is real Redis time in fakeredis (TTL 90 s does not
  expire during a test, which is what we want).
- Pool size 1 per role: never hold a global_session while opening a system_session in the route helpers.
- Model-level validator error `field`: confirm the rendered envelope (likely no field) and assert what it renders.
- The example base32 secret in schema examples may trip gitleaks; use a short obviously-sample value or none.

## 2026-10-29 Builder: P02-T06b build notes
- Implemented per plan. New: `auth/failure_counter.py` (FailureCounters; LoginThrottle now delegates, public API and
  `login_throttle_unavailable` event unchanged), `auth/mfa_guard.py` (MfaGuard, REPLAY_GUARD_LUA, keys/AAD/messages),
  `routers/auth_mfa.py` + `routers/auth_mfa_schemas.py`. `session_auth.gated_membership` replaces `enforce_mfa`
  (org OR user MFA; user lookup when no membership); `mfa_due` also used by `GET /session`. `ActiveSession.mfa_passed_at`
  (+ cache JSON, old entries read None), `SessionManager.mark_mfa_passed`, `require_recent_auth` uses
  max(created_at, mfa_passed_at). `MembershipRecord.user_mfa_enabled` (join users). Container `mfa_guard`.
- Specs: specs/04 §4.1 recent-auth decision, §4.3 amended gate decision, §4.5 MFA endpoints decision; specs/03 §11
  rows `auth:fail:mfa:{user_id}`, `mfa:last:{user_id}`, `sess:` lists `mfa_passed_at`. AGENTS.md entry.
- Model-level validator error renders INVALID_INPUT without `field` (asserted).
- Gates: fmt ok; lint pass; typecheck pass (307 files); placeholders 0; make test 2405 passed, coverage 99.30%;
  check_spec_refs 0; openapi drift current (internal.json regenerated, openapi.json unchanged); jscpd 0 clones;
  radon -n C none; pip-audit + pnpm audit no vulns; gitleaks passed.

## 2026-10-29 Planner: P02-T06b build.blocked (payload format only) → re-plan same task
Same cause as P02-T04d: the Builder's `build.done` used `--json` with an object payload (all gates reported pass:
2405 passed, 99.30 % coverage, 0 clones, radon none, audits clean), and the loop's backpressure check did not accept
it and converted the event to `build.blocked`. No design or code problem; no split or prerequisite. Plan P02-T06b is
unchanged.
Builder, this activation: do not redesign. Re-run the gates on the current working tree (`make fmt lint typecheck
placeholders test`, `uv run python scripts/check_openapi_drift.py`, check_spec_refs, jscpd, radon, pip-audit
--strict per mem-1793267153-9b68, pnpm audit, gitleaks), fix anything red, then emit build.done as a PLAIN STRING
(no `--json`) with exactly these keys and real values from this run, e.g.:
`ralph emit "build.done" "P02-T06b: tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN %), complexity: pass (radon -n C none), duplication: pass (0 clones), specs: pass"`

## 2026-10-29 Builder: P02-T06b gate re-run (no code changes)
fmt/lint/typecheck (307 files)/placeholders (0) pass; make test 2405 passed, coverage 99.30 %; openapi drift current
(both docs); check_spec_refs 0; jscpd 0 clones; radon -n C none; pip-audit --strict + pnpm audit no vulns; gitleaks
passed. build.done emitted as plain string.

## 2026-10-29 Planner: P02-T06b build.blocked again (complexity token format) → re-plan same task
The plain-string build.done was still converted to build.blocked. Cause, from the events log: every accepted
build.done carried a NUMERIC complexity (`complexity: 3.4`, `complexity: 3.17`, `complexity: 2.2`). The rejected
ones used `complexity: A` (P00-FIX-1) and `complexity: pass (radon -n C none)` (this one). The backpressure parser
reads complexity as a number (average cyclomatic complexity). No code or design problem; plan P02-T06b is unchanged.
Builder, this activation: do not redesign. Re-run the gates on the current tree (same list as the last re-run), fix
anything red, compute the average with
`uvx radon cc -s -a packages/va_common/src packages/va_db/src apps/api/src | tail -1` (the "Average complexity: A (N.NN)"
line), and emit a PLAIN STRING whose complexity value is just that number, e.g.:
`ralph emit "build.done" "P02-T06b tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

## 2026-10-29 Builder: P02-T06b gate re-run #2 (no code changes)
fmt/lint/typecheck (307 files)/placeholders (0) pass; make test 2405 passed, coverage 99.30 %; openapi drift current
(both docs); check_spec_refs 0; jscpd 0 clones (541 files); radon -n C none, average 1.89; pip-audit --strict + pnpm
audit no vulns; gitleaks passed. build.done emitted as plain string with numeric complexity 1.89.

## Review P02-T06b (2026-10-29): approved
Re-ran: make lint/typecheck/placeholders (0 findings) pass; make test 2405 passed, coverage 99.30 %; openapi drift
current (internal.json regenerated, openapi.json unchanged); check_spec_refs 0; jscpd 0 clones; radon -n C none.
Diff checked against specs/04 §4.1 mfa rows, §4.3 gate, §4.5 and the new Decisions; specs/03 §11 keys; error codes
CONFLICT/FORBIDDEN/INVALID_MFA_CODE/AUTH_LOCKED/MFA_REQUIRED/REAUTH_REQUIRED per specs/31. Disable checks the org
requirement before the code; failure lock checked before any comparison; replay guard via Lua CAS; LoginThrottle
behaviour and log event unchanged (existing tests green). No blocking I/O (QR in to_thread, argon2 in worker), no
secrets logged. Ticked T06b and parent T06.

## 2026-10-29 Planner: P02-T06b approved → P02-T07 split into T07a/T07b
T07 (OIDC SSO) is too large for one Builder pass: nothing exists yet (no authlib, no `OIDC_ENABLED` setting, no
`require_permission`, no `/v1/*` envelope router, no SSO repo, no outbound-URL guard). Split in plan/phase-02.md:
- **T07a** connection management (`/v1/org/sso`, `sso.manage`, discovery validation, domain-conflict lookup repo).
- **T07b** login flow (authlib, start/callback, state/nonce/PKCE in Redis, ID token validation, JIT, enforce →
  `SSO_REQUIRED` on password login). T07b reuses T07a's `fetch_provider_metadata`, `SsoConnectionLookupRepo`, and the
  decrypt helper.

## Plan P02-T07a — SSO connection management (specs/04 §3.2, §3.3, §5 step 1, §8.1; specs/03 §2, §3; specs/28 §3, §6.4; specs/02 §6)

### Facts found while planning
- `sso_connections` model exists (`va_db.models.SsoConnection`, tenancy.py): org_id, provider ('oidc'), issuer,
  client_id, client_secret_enc (bytea NOT NULL), allowed_domains text[], default_role text (no CHECK), enforce,
  UNIQUE(org_id, issuer). Tenant table with RLS (grants exist in 0001). No migration needed.
- `OIDC_ENABLED=false` is in specs/02 §6 and `.env.example` already, but NOT in `ApiSettings` (so no env-example test
  change is needed; check `scripts/tests/test_env_example.py` still passes).
- No `require_permission` yet (rbac.py has only ROLE_SCOPES); T08 owns the full matrix-vs-markdown parse test.
- `envelope_router` builds envelope routers; `/v1/org*` classifies as envelope. No `/v1` router exists yet; `ROUTERS`
  in main.py is `(public_router, internal_auth_router, probe_router)`.
- `Envelope.encrypt_str/decrypt_str(org_id, ..., aad)` + `va_common.crypto.column_aad`.
- httpx is a va_common dependency (not declared by va-api); `respx` is a root dev dependency.
- `va_common.http.safe_client` (specs/28 §6.4) does not exist yet; it belongs to the webhooks/tools phases.
- Test infra: `SessionContainer` (extends `AuthContainer` with the fake API `KeyStore`), conftest `client`,
  `make_member(identity, role=..., ...)`, `login`, `write_headers`, `error_of`.

### Decisions to record (same commit, `> Decision (2026-10-29):`)
specs/04 §5 (after step 6):
1. **One connection per org.** `PUT /v1/org/sso` upserts the org's single row (changing `issuer` updates the row in
   place); `GET` returns it or `NOT_FOUND` "SSO connection not found."; `DELETE` removes it (hard delete, table has no
   `deleted_at`) and returns `{data:{id, deleted:true}}`, `NOT_FOUND` when absent.
2. **Body** `SsoConnectionInput` (extra forbid): `issuer` (str, 1-2048, no trailing `/` normalisation beyond
   stripping one trailing slash; must parse as an absolute URL with scheme `https` — `http` accepted only when
   `VA_ENV` is `local` or `test` — no userinfo, query, or fragment), `client_id` (1-255), `client_secret` (1-1024,
   `SecretStr`; required when no connection exists yet → `INVALID_INPUT` field `client_secret` "client_secret is
   required when creating an SSO connection."; omitted on update keeps the stored secret), `allowed_domains`
   (list, 1-50 items, each lowercased + stripped, must match `^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$`,
   duplicates removed preserving order; wildcards rejected), `default_role` (`admin|developer|analyst|billing|viewer`,
   default `viewer`; `owner` refused — JIT must never mint owners), `enforce` (bool, default false), `provider`
   (Literal["oidc"], default "oidc").
3. **Response** `SsoConnectionView`: `id, provider, issuer, client_id, allowed_domains, default_role, enforce,
   created_at, updated_at`. The client secret is never returned in any form (specs/28 T8).
4. **Secret storage**: `client_secret_enc = envelope.encrypt_str(org_id, secret, column_aad("sso_connections",
   "client_secret_enc", connection_id))`. Helper `decrypt_client_secret(envelope, row)` for T07b.
5. **Discovery validation** before writing: GET `{issuer}/.well-known/openid-configuration`, timeout 5 s, no
   redirects (3xx → invalid), body cap 256 KiB, JSON object required with `issuer` exactly equal to the configured
   issuer and string `authorization_endpoint`, `token_endpoint`, `jwks_uri`; endpoints must satisfy the same scheme
   rule as the issuer. Outside `local`/`test` the issuer host must resolve only to global addresses
   (`ipaddress.ip_address(...).is_global`), else invalid. Every failure → `INVALID_INPUT` field `issuer`
   "Could not validate the OIDC issuer: <reason>." with reasons `unreachable`, `timed out`, `redirect not allowed`,
   `HTTP <status>`, `response too large`, `not a JSON object`, `issuer mismatch`, `missing <field>`,
   `insecure endpoint <field>`, `address not allowed`. Full DNS pinning arrives with `va_common.http.safe_client`
   (specs/28 §6.4) and will replace this fetch.
6. **Allowed domains are unique across orgs**: a PUT whose domains intersect another org's connection →
   `CONFLICT` field `allowed_domains` "Domain <d> is already claimed by another organization's SSO connection."
   (checked through the system-scope `SsoConnectionLookupRepo`; specs/03 §2 list amended).
7. **Feature flag**: with `OIDC_ENABLED=false` (default) all three routes fail with `FORBIDDEN` "SSO is not enabled
   on this deployment." after authentication.
8. **Audit** `org.sso_update` is written when the audit writer lands (P02-T11); not in this task.
specs/04 §3.3 — **permissions**: `va_api.auth.permissions.PERMISSIONS` = the 20 §3.3 names; `ROLE_PERMISSIONS`
  (exact): owner = all 20; admin = all except `org.delete`, `org.owner_transfer`, `billing.plan_change`; developer =
  `pathways.publish`, `voices.clone`, `experiments.operate`, `calls.operate`; analyst = none; billing =
  `billing.plan_change`, `billing.payment`, `billing.credit_transfer`; viewer = none. `SESSION_ONLY_PERMISSIONS` =
  `org.delete`, `org.owner_transfer`, `sso.manage`. API keys hold every non-session-only permission when they hold
  `*`, and none otherwise (per §3.3 text; T08 may widen when it adds the matrix parse test). FORBIDDEN message:
  "Missing required permission '<name>'." (mirrors the scope message).
specs/03 §2 — `SsoConnectionLookupRepo` joins the `va_system` list (bullet + the repository Decision): domain lookup
  spans orgs and runs before any org context (SSO start, T07b) and for the PUT conflict check.

### Files
**apps/api/pyproject.toml** — add `httpx[http2]>=0.28.1` (direct use); `uv lock` (no new package versions).
**apps/api/src/va_api/settings.py** — `oidc_enabled: bool = False` (comment: specs/04 §5).
**apps/api/src/va_api/auth/permissions.py** (new):
  - `PERMISSIONS: Final[frozenset[str]]`, `ROLE_PERMISSIONS: Final[Mapping[MemberRole, frozenset[str]]]`
    (MappingProxyType), `SESSION_ONLY_PERMISSIONS: Final[frozenset[str]]`, `REQUIRED_PERMISSION_ATTR = "required_permission"`.
  - `def holds_permission(ctx: RequestContext, permission: str) -> bool` (user/platform_admin: role table; api_key:
    `*` and not session-only; widget: False).
  - `def require_permission(permission: str) -> Callable[..., Awaitable[RequestContext]]` — ValueError for unknown
    names at definition; dependency `Depends(get_principal)`; FORBIDDEN when not held; sets the attr.
  - Export from `va_api/auth/__init__.py` if that module re-exports (check its `__all__`).
**apps/api/src/va_api/auth/oidc_discovery.py** (new; T07b reuses):
  - `@dataclass(frozen=True, slots=True) class ProviderMetadata: issuer, authorization_endpoint, token_endpoint,
    jwks_uri: str`.
  - `class DiscoveryError(Exception)` with `reason: str`.
  - `type HostResolver = Callable[[str], Awaitable[list[str]]]`; `async def system_resolver(host) -> list[str]`
    (`asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)`, unique addresses).
  - `def normalise_issuer(raw: str, *, allow_http: bool) -> str` (ValueError messages used by the schema/route).
  - `class OidcDiscovery(http: httpx.AsyncClient, *, allow_insecure: bool, resolver: HostResolver = system_resolver)`
    with `async def fetch(issuer: str) -> ProviderMetadata` (resolve check unless allow_insecure; GET with
    `follow_redirects=False`; stream and cap at 256 KiB; parse via `json.loads` (small, capped — in-loop OK);
    validate). `DISCOVERY_TIMEOUT_S = 5.0`, `MAX_DISCOVERY_BYTES = 262_144`.
  - `allow_insecure` = `settings.va_env in {LOCAL, TEST}` (http scheme + private addresses allowed).
**apps/api/src/va_api/container.py** — `oidc_http: httpx.AsyncClient` (timeout 5 s, `follow_redirects=False`,
  closed via `stack.push_async_callback(client.aclose)`), `oidc_discovery: OidcDiscovery`; docstring list.
  `apps/api/tests/support/*` containers: add `oidc_discovery` field (SessionContainer builds one with a real
  `httpx.AsyncClient` closed in the fixture, or a field default the respx router intercepts — respx patches the
  global transport, so a plain `httpx.AsyncClient()` works; make sure it is closed: add `aclose` in the `client`
  fixture teardown or construct lazily per test).
**packages/va_db/src/va_db/repositories/sso.py** (new):
  - `SsoConnectionRepo(session, org_id)` (tenant session): `get() -> SsoConnection | None`,
    `async def upsert(*, connection_id, issuer, client_id, client_secret_enc: bytes | None, allowed_domains,
    default_role, enforce, now) -> SsoConnection` (update existing row, keeping the secret when None; create
    otherwise, provider 'oidc'; flush), `delete() -> uuid.UUID | None`. Use `conflict_savepoint` for the
    (org_id, issuer) unique path even though one row per org makes it unreachable in practice.
  - `@dataclass(frozen=True, slots=True) SsoDomainClaim(org_id, connection_id, domain)`.
  - `class SsoConnectionLookupRepo(SystemRepository)`: `async def claims(domains: Collection[str]) ->
    list[SsoDomainClaim]` (rows whose `allowed_domains && :domains`, unnest to the matching domains, ordered by
    connection `created_at`, id), `async def for_domain(domain: str) -> SsoConnection | None` (oldest match; T07b).
    Use `literal(x, Text) == any_(col)` / `col.overlap(array)` per mem-1793268366-7e50 for mypy.
  - export both + `SsoDomainClaim` in `repositories/__init__.py`; module docstring of `__init__` mentions them.
**apps/api/src/va_api/routers/org_sso_schemas.py** (new) — `SsoConnectionInput`, `SsoConnectionView`,
  `SsoConnectionDeleted` with `json_schema_extra` examples (issuer `https://login.acme-dental.test`, domains
  `["acme-dental.test"]`; no secret-looking example — check placeholders/gitleaks).
**apps/api/src/va_api/routers/org_sso.py** (new) — `org_router = envelope_router(prefix="/v1/org", tags=["Org"])`
  (T08 adds the other `/v1/org` routes to it); routes `PUT/GET/DELETE /sso`, operation ids `org_sso_put|get|delete`,
  dependencies `require_scope("org:write")` (PUT/DELETE) / `require_scope("org:read")` (GET) **and**
  `require_permission("sso.manage")`; private helpers `_ensure_enabled(request)`, `_check_domains(request, org_id,
  domains)` (system session, CONFLICT), `_view(row)`. Flow for PUT: enabled → validate secret presence vs existing
  (tenant read) → discovery fetch (outside any DB session; DiscoveryError → INVALID_INPUT field issuer) → domain
  conflict (system session, closed) → encrypt (if secret given) → tenant session upsert. Never hold two sessions at
  once (pool size 1 per role). Keep functions radon A/B.
**apps/api/src/va_api/main.py** — add `org_router` to `ROUTERS`.
**apps/api/src/va_api/auth/session_auth.py** — accessor `oidc_discovery_of(request)` (+ `__all__`).
**openapi/openapi.json** — `make openapi` (public doc gains the three operations, `x-envelope: envelope`); check
  `test_openapi.py` expectations (operation lists) and update.
**specs/04 §3.3, §5; specs/03 §2** — decisions above. **AGENTS.md** — permissions module, `/v1/org` router, discovery
  guard + `allow_insecure` rule, `oidc_discovery` test seam with respx.

### Tests
apps/api/tests/test_permissions.py: `PERMISSIONS` equals the 20 names parsed from the specs/04 §3.3 paragraph
  (regex over backticked names between "permission names:" and "API keys"); exact `ROLE_PERMISSIONS` per role;
  `require_permission("nope")` → ValueError; dependency attr set; api key with `*` holds `calls.operate` but not
  `sso.manage`/`org.delete`/`org.owner_transfer`; api key with `calls:write` holds none; widget holds none.
apps/api/tests/test_oidc_discovery.py (respx, no DB): good document → ProviderMetadata; trailing slash on issuer
  normalised (fetches `/.well-known/...` once); issuer mismatch; missing each of the 3 endpoints (parametrized);
  non-JSON; JSON array; HTTP 500; 302 redirect not followed; body > 256 KiB; httpx.ConnectTimeout → "timed out";
  ConnectError → "unreachable"; `allow_insecure=False`: http issuer refused by `normalise_issuer`, resolver returning
  `10.0.0.5`/`127.0.0.1`/`169.254.169.254` → "address not allowed", public address passes, http endpoint in a https
  document → "insecure endpoint token_endpoint"; userinfo/query/fragment refused.
apps/api/tests/test_org_sso_integration.py (client fixture, `oidc_enabled=True` via session_app overrides, respx):
  - owner PUT creates (200, view without any secret key — assert `"client_secret" not in json` recursively), DB row's
    secret decrypts with the column AAD and the ciphertext != plaintext; GET returns it; second PUT without
    client_secret updates domains/enforce and keeps the secret; PUT changing issuer updates in place (same id).
  - first PUT without client_secret → INVALID_INPUT field client_secret.
  - admin allowed; developer/analyst/billing/viewer → FORBIDDEN (parametrized, GET and PUT).
  - `*` API key → FORBIDDEN "Missing required permission 'sso.manage'." on all three.
  - CSRF missing on PUT/DELETE → FORBIDDEN; no session → AUTH_FAILURE.
  - discovery failure → INVALID_INPUT field issuer, nothing written.
  - another org already claims `acme-dental.test` → CONFLICT field allowed_domains; same org re-PUT with its own
    domains is fine.
  - domains normalised (case, whitespace, duplicates); wildcard / invalid / 51 domains → INVALID_INPUT;
    `default_role: "owner"` → INVALID_INPUT; unknown field → INVALID_INPUT.
  - DELETE → `{id, deleted: true}`, then GET → NOT_FOUND, DELETE again → NOT_FOUND.
  - `oidc_enabled=False` (default settings) → FORBIDDEN "SSO is not enabled on this deployment." for the owner.
  - tenant isolation: org B owner GET → NOT_FOUND while org A has a connection.
packages/va_db/tests/test_sso_repositories_integration.py (identity_db fixture): upsert create/update keeps secret
  when None; `delete`; `claims` returns overlapping domains with org ids ordered oldest first; `for_domain` oldest;
  RLS: tenant repo of org B sees nothing.
packages/va_db/tests/test_repositories.py: system repo name list includes `SsoConnectionLookupRepo` (update the
  expected list to match specs/03 §2).
apps/api/tests/test_settings_auth.py (or nearest): `OIDC_ENABLED` env parses, default false.
Keep new test files < ~350 lines; reuse conftest fixtures and session_helpers (no copied fixtures — jscpd).

### Gates
`make fmt lint typecheck placeholders test`; `uv run python scripts/check_spec_refs.py`; `uv run python
scripts/check_openapi_drift.py` after `make openapi` (openapi.json changes; internal.json unchanged); `npx jscpd@4`
(0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and the numeric average
`uvx radon cc -s -a ... | tail -1`; pip-audit per mem-1793267153-9b68 + `corepack pnpm audit`; `uv run pre-commit run
gitleaks --all-files`. build.done as a PLAIN STRING with numeric complexity (mem-1793269824-fe85), e.g.
`ralph emit "build.done" "P02-T07a tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`.

### Risks
- respx must intercept the container's `httpx.AsyncClient`: create the test client inside the respx context or use
  `respx.mock` as a fixture around the app; never let a test reach the network (assert all routes called).
- Pool size 1 per role: discovery fetch and the system-session conflict check happen outside the tenant session.
- `test_openapi.py` / dashboard TS type generation may snapshot the public operation list — regenerate and update
  expectations; `openapi/internal.json` must not change.
- The `/v1/org` router is shared with T08: name it `org_router` in `routers/org_sso.py` now; T08 may move it to
  `routers/org.py` and keep `org_sso` routes attached via side-effect import (mem-1793267153-d5f5 pattern).
- Example issuer/domain values: `check_no_placeholders` flags `example.com`; use `.test` TLD.
- Default role column has no CHECK: validate in the schema only.

## 2026-10-29 Builder: P02-T07a build notes
Implemented per plan, with these deviations (all recorded in the specs/04 §5 Decision):
- `normalise_issuer(raw)` takes no `allow_http`; it accepts http(s). The scheme rule lives in
  `OidcDiscovery.fetch` (reason `insecure issuer` outside local/test), so the schema needs no settings.
- Issuer equality ignores one trailing `/` on either side (`same_issuer`); `ProviderMetadata.issuer` keeps the
  provider's own spelling (Auth0-style `https://x/`), which T07b must use for the ID token `iss` check.
- `SsoConnectionRepo.upsert` has no `now` parameter: the `set_updated_at()` trigger maintains `updated_at`.
- `SessionContainer.__call__` builds `oidc_discovery` from the app settings with an `httpx.AsyncClient` closed on
  exit; production container closes `oidc_http` via the exit stack (test_container close order now starts "http").
- Route `dependencies=[require_permission]` resolve before the `require_scope` parameter dependency.
- Regenerated `openapi/openapi.json` plus both TS `schema.d.ts` files (`pnpm gen:api`); internal.json unchanged.
Gates (this iteration): make fmt ok; make lint 0; make typecheck 0; make placeholders 0 findings; make test 2500
passed, coverage 99.31%; make test-ts pass (widget 22, dashboard 52, sdk 48); openapi drift current; spec refs 0;
jscpd 0 clones; radon no C+, average 1.925; pip-audit + pnpm audit clean; gitleaks passed.

## Review P02-T07a (2026-10-29): approved
Re-ran: make lint 0, make typecheck 0, make placeholders 0 findings, make test 2500 passed (coverage 99.31%), make
test-ts pass, check_spec_refs 0, check_openapi_drift current (openapi.json + internal.json), jscpd 0 clones, gitleaks
passed. Diff checked against specs/04 §3.3/§5/§8.1, specs/03 §2/§3 `sso_connections`, specs/02 §6 `OIDC_ENABLED`:
secret envelope-encrypted with column AAD and never returned; discovery guard (no redirects, 5 s, 256 KiB, is_global,
https outside local/test); `sso.manage` session-only so API keys get FORBIDDEN; cross-org domain CONFLICT via system
repo (spec-cited); tenant repo filters org_id inside tenant_session; no nested sessions. Known, documented limits:
DNS rebinding until safe_client (specs/28 §6.4); concurrent PUTs from two orgs can race the domain check (no DB
constraint) — acceptable for admin-only config, revisit if T07b lookup needs a hard guarantee.
Next: P02-T07b.

## 2026-10-29 Planner: P02-T07a approved → next P02-T07b

## Plan P02-T07b — SSO login: start, callback, JIT provisioning, enforce mode (specs/04 §4.1, §5 steps 2–6, §13, §14; specs/03 §11; specs/31 SSO_REQUIRED)

### Facts found while planning
- `authlib==1.9.1` + `joserfc==1.7.7` resolve; `from authlib.integrations.httpx_client import AsyncOAuth2Client` imports
  cleanly under `-W error`. **Do not import `authlib.jose`** (deprecated in favour of joserfc; it warns, and warnings
  are errors). Use authlib `AsyncOAuth2Client` for the PKCE authorization URL + code exchange, joserfc for the ID token.
  `create_authorization_url(url, state=, nonce=, code_verifier=, login_hint=)` → `(url, state)`; with
  `code_challenge_method="S256"` it adds `code_challenge`. `fetch_token(url, code=, code_verifier=)` (client_secret_basic
  default) raises `authlib.integrations.base_client.OAuthError`/`authlib.oauth2.OAuth2Error` on an error body.
- `SessionManager.start_in` has no `mfa_passed`; `IdentityRepo.create_session` has `mfa_passed` but no `mfa_passed_at`.
- `_revoke_presented` / `_set_session_cookies` are private in `routers/auth.py`; SSO needs both.
- `SsoConnectionLookupRepo.for_domain(domain)` (system session) and `SsoConnectionRepo(session, org_id).get()` exist;
  `decrypt_client_secret(envelope, row)` lives in `routers/org_sso.py`.
- `sso:state:{state}` is in specs/04 §5 but missing from the specs/03 §11 table. Redis key builders live next to their
  users (`session_cache_key` in auth/sessions.py, `mfa_last_key` in auth/mfa_guard.py).
- `test_org_sso_integration.py` defines `new_domain()`, `discovery_document(issuer)` and an `idp` respx fixture: move
  the two helpers into the new `tests/support/fake_oidc.py` and import them there (jscpd).
- `AppError(code, message, details=..., field=...)` supports `details`. `generate_token()` = 43 base64url chars
  (valid PKCE verifier length 43–128).

### Decisions to record (specs/04 §5, same commit, `> Decision (2026-10-29):`)
**SSO login flow.**
1. **Browser-facing errors.** `sso/start` and `sso/callback` are top-level navigations, so every failure answers
   **302** to `{DASHBOARD_BASE_URL}/login?sso_error=<reason>` (never a JSON envelope) and logs `sso_login_failed`
   with `reason` (never the email/claims). Reasons: `disabled` (`OIDC_ENABLED=false`), `invalid_email`,
   `not_configured` (no connection for the domain, or the state's connection was deleted/replaced),
   `provider_unavailable` (discovery/JWKS fetch failed or Redis unavailable), `invalid_state` (missing, malformed,
   unknown, expired, reused, or not bound to this browser), `provider_error` (IdP `error` param, missing `code`, token
   exchange failure, no `id_token`), `invalid_token` (signature, alg, `iss`, `aud`/`azp`, `exp`, `iat`, `nonce`),
   `email_not_verified`, `email_domain_mismatch`, `account_disabled`.
2. **start** `GET /internal/auth/sso/start?email=` (3–320 chars after strip, one `@`, domain part lowercased must pass
   the `allowed_domains` DNS rule): `for_domain` lookup (system session) → discovery fetch of the connection's issuer
   → `state`, `nonce`, `code_verifier` = three `generate_token()` → Redis `SET sso:state:{state} <json> EX 600 NX`
   with `{connection_id, org_id, nonce, code_verifier, issuer (provider spelling), token_endpoint, jwks_uri}` →
   302 to the authorization URL (`response_type=code`, `scope="openid email profile"`, `state`, `nonce`,
   `code_challenge` S256, `login_hint=<email>`, `redirect_uri={PUBLIC_API_BASE_URL}/internal/auth/sso/callback`) and
   sets cookie **`va_sso`** = state (HttpOnly, SameSite=Lax, Secure as the session cookie, `Path=/internal/auth/sso`,
   `Max-Age=600`, `SESSION_COOKIE_DOMAIN`). The cookie binds the flow to the browser (login-CSRF defence).
3. **callback** `GET /internal/auth/sso/callback?state=&code=&error=`: `va_sso` cookie is cleared on every outcome;
   `state` must look like a token and equal the cookie (`tokens_equal`) → `GETDEL sso:state:{state}` (single use) →
   IdP `error`/missing `code` → `provider_error` → connection re-read in `tenant_session(org_id)`; its id must equal
   the state's `connection_id` → secret decrypted → token/JWKS endpoints re-checked by the discovery guard (scheme,
   global address outside local/test) → code exchange through authlib (5 s, no redirects, `code_verifier`) → JWKS
   fetched through the discovery guard (5 s, 256 KiB, no redirects) → ID token validated: JWS algs `RS256 RS384 RS512
   PS256 PS384 PS512 ES256 ES384 ES512` only (never `none`/`HS*`), key chosen by `kid`; `iss` exactly the provider's
   discovery spelling; `aud` equals `client_id` or is a list containing it, and a multi-audience token needs
   `azp == client_id`; `exp` and `iat` required, `exp > now − 60 s`, `iat ≤ now + 60 s`; `nonce` equal
   (constant time); `email` a string.
4. **Email rule.** The email's domain (lowercased) must be in the connection's `allowed_domains` in every case (domains
   are unique across orgs, so an IdP can never assert an address another org owns); `email_verified` false (bool or
   the string `"false"`) is refused; a missing claim counts as verified because the domain matched.
5. **JIT** in one `tenant_session(org_id)`: user by email (citext) → none: create (no password, name = `name` claim or
   `""` cut to 200, `email_verified_at=now`); existing user not `active` → `account_disabled`; existing unverified user
   is marked verified (the IdP proved the address). Membership: none → `default_role`; existing keeps its role. Then a
   session (14 d sliding, active org = the connection's org, `mfa_passed=true`, `mfa_passed_at=now`, `last_login_at`).
   After the commit: `sess:` cache, presented `va_session` revoked (fixation defence), both auth cookies set, 302 to
   `{DASHBOARD_BASE_URL}/`. Logged `sso_login` with `user_id`, `org_id`, `user_created`, `membership_created`. Audit
   `auth.sso_login` is written once the audit writer exists (P02-T11).
6. **Enforcement.** With `OIDC_ENABLED=true`, `POST /internal/auth/login` for an email whose domain's connection has
   `enforce=true` answers `SSO_REQUIRED` 403 "Your organization requires single sign-on." with
   `details.sso_url = {PUBLIC_API_BASE_URL}/internal/auth/sso/start?email=<url-encoded email>`, checked after the
   throttle check and before password verification (no failure counted). With `OIDC_ENABLED=false` nothing is enforced
   (SSO would be unusable). Existing sessions, password reset, and API keys are unaffected.
specs/03 §11 — new row: `sso:state:{state}` | string JSON (`connection_id`, `org_id`, `nonce`, `code_verifier`,
  `issuer`, `token_endpoint`, `jwks_uri`) | 600 s | OIDC login state, single use via `GETDEL` (specs/04 §5).

### Files
**apps/api/pyproject.toml** — add `authlib>=1.9.1`, `joserfc>=1.7.7`; `uv lock`. If a Redis-keys/env test compares
  the specs/03 §11 table to code, update it.
**apps/api/src/va_api/auth/oidc_discovery.py** — refactor: public `async def check_url(url: str) -> None` (scheme via
  `_secure` → `DiscoveryError(f"insecure endpoint")`-style reason `INSECURE_URL = "insecure url"`; host check unless
  `allow_insecure`), `async def fetch_json(url: str) -> dict[str, Any]` (check_url + `_download` + `_json_object`);
  `fetch(issuer)` keeps its exact reasons (issuer → `INSECURE_ISSUER`) and reuses `_download`/`_json_object`. Export the
  new names. Existing tests must still pass unchanged.
**apps/api/src/va_api/auth/oidc_tokens.py** (new):
  - `ALLOWED_ALGORITHMS: Final = ("RS256","RS384","RS512","PS256","PS384","PS512","ES256","ES384","ES512")`,
    `CLOCK_SKEW: Final = timedelta(seconds=60)`.
  - `@dataclass(frozen=True, slots=True) class IdTokenClaims: subject: str; email: str; email_verified: bool | None;
    name: str`.
  - `class IdTokenError(Exception)` with `reason: str` (internal detail for logs; route maps to `invalid_token`).
  - `def validate_id_token(id_token: str, *, jwks: Mapping[str, Any], issuer: str, client_id: str, nonce: str,
    now: datetime) -> IdTokenClaims` — `KeySet.import_key_set(jwks)` (ValueError/JoseError → IdTokenError("jwks")),
    `joserfc.jwt.decode(id_token, key_set, algorithms=list(ALLOWED_ALGORITHMS))` (any `JoseError`/ValueError →
    IdTokenError("signature")), then small private checkers `_check_issuer`, `_check_audience`, `_check_times`,
    `_check_nonce` (hmac.compare_digest), `_email_verified(value) -> bool | None` (True/"true" → True, False/"false" →
    False, absent → None, anything else → IdTokenError("email_verified")). Keep each function radon A/B.
**apps/api/src/va_api/auth/sso_state.py** (new):
  - `SSO_STATE_TTL_S: Final = 600`, `def sso_state_key(state: str) -> str` (`f"sso:state:{state}"`).
  - `@dataclass(frozen=True, slots=True) class SsoState: connection_id: uuid.UUID; org_id: uuid.UUID; nonce: str =
    field(repr=False); code_verifier: str = field(repr=False); issuer: str; token_endpoint: str; jwks_uri: str` with
    `to_json() -> str` / `from_json(raw: str | bytes) -> SsoState`.
  - `class SsoStateUnavailable(Exception)`; `class SsoStateStore(redis: Redis)`: `async def put(state: str, value:
    SsoState) -> None` (SET NX EX; Redis/OSError → SsoStateUnavailable; NX collision → SsoStateUnavailable),
    `async def take(state: str) -> SsoState | None` (`GETDEL`; RedisError/OSError → SsoStateUnavailable; malformed
    JSON → None).
**apps/api/src/va_api/auth/cookies.py** — `SSO_STATE_COOKIE: Final = "va_sso"`, `SSO_COOKIE_PATH = "/internal/auth/sso"`,
  `set_sso_state_cookie(response, policy, state)`, `clear_sso_state_cookie(response, policy)` (Max-Age 0, same path).
**apps/api/src/va_api/auth/sessions.py** — `start_in(..., mfa_passed: bool = False)`; passes `mfa_passed` and
  `mfa_passed_at=now if mfa_passed else None` to the repo. **packages/va_db/src/va_db/repositories/identity.py** —
  `create_session(..., mfa_passed_at: datetime | None = None)` sets the column.
**apps/api/src/va_api/auth/session_auth.py** — `sso_states_of(request) -> SsoStateStore` accessor (+ `__all__`).
**apps/api/src/va_api/container.py** + `apps/api/tests/support/session_helpers.py::SessionContainer` — field
  `sso_states: SsoStateStore` built from the container's redis (SessionContainer in `__post_init__`).
**apps/api/src/va_api/routers/auth.py** — rename `_revoke_presented` → `revoke_presented`, `_set_session_cookies` →
  `set_session_cookies` (public, in `__all__`), update callers. `login`: after `throttle.check`, `await
  _check_sso_enforced(request, body.email)` (system session `for_domain`, only when `oidc_enabled`), raising
  `SSO_REQUIRED` with `details={"sso_url": sso_start_url(settings, email)}`; `SSO_REQUIRED_MESSAGE` constant.
  Put `sso_start_url(settings, email) -> str` and `email_domain(email) -> str | None` in `routers/auth_sso.py`? No —
  avoid an import cycle: put both in **apps/api/src/va_api/auth/sso_urls.py** (new, tiny): `CALLBACK_PATH`,
  `START_PATH`, `email_domain(email)` (reuses the `_DOMAIN` regex — move `_DOMAIN`/a `is_dns_domain(value)` helper
  from `routers/org_sso_schemas.py` into this module and import it there), `sso_start_url(settings, email)`,
  `callback_url(settings)`, `login_error_url(settings, reason)`, `dashboard_home_url(settings)`.
**apps/api/src/va_api/routers/auth_sso.py** (new; attaches to `internal_auth_router`, side-effect import in
  `va_api/main.py` like `auth_password`/`auth_mfa`):
  - `class SsoFailure(Exception)` with `reason`; `REASONS` frozenset of the 11 reasons (tests assert each is reachable
    or at least in the decision list).
  - `GET /sso/start` (`operation_id="auth_sso_start"`, `openapi_extra=PUBLIC_MARKER`, query `email: str = ""`) →
    `RedirectResponse(status_code=302)`; `GET /sso/callback` (`auth_sso_callback`, query `state`, `code`, `error`
    optional strings). Each route: `try: ... except SsoFailure as exc: log + redirect to login_error_url`; callback
    clears `va_sso` on both paths.
  - Private helpers (radon ≤ B each): `_ensure_enabled`, `_connection_for(request, email)`, `_metadata(request, issuer)`
    (DiscoveryError → `provider_unavailable`), `_authorize_url(...)` (`async with AsyncOAuth2Client(client_id=...,
    scope="openid email profile", redirect_uri=..., code_challenge_method="S256") as oauth`), `_take_state(request,
    state)`, `_exchange(request, connection, record, code) -> str` (id_token; `check_url(token_endpoint)`; `async with
    AsyncOAuth2Client(client_id, client_secret, redirect_uri, timeout=5.0, follow_redirects=False)`; catch
    `OAuthError`, `OAuth2Error`, `httpx.HTTPError`, `ValueError` → `provider_error`), `_claims(...)` (fetch_json(jwks)
    → `provider_unavailable`; IdTokenError → `invalid_token`, log `detail=exc.reason`), `_check_email(claims,
    connection)`, `_provision(request, connection, claims, ip) -> tuple[str, ActiveSession, bool, bool]` (one
    tenant_session; AppError CONFLICT from a concurrent create → `provider_unavailable`).
  - Reuse `decrypt_client_secret` from `routers/org_sso.py`; `revoke_presented`/`set_session_cookies` from
    `routers/auth.py`.
**specs/04 §5, specs/03 §11** — decisions above. **AGENTS.md** — SSO flow, `va_sso` cookie, `fake_oidc` test helper,
  "never import authlib.jose (use joserfc)". `openapi/internal.json` regenerated (`make openapi`; the two new internal
  operations appear there; `openapi.json` unchanged) and check `test_openapi.py` internal operation lists.

### Tests
**apps/api/tests/support/fake_oidc.py** (new): module-level RSA key generated once (`joserfc.jwk.RSAKey.generate_key(2048,
  parameters={"kid": "test-key"})`, plus a second "other" key for bad-signature); `new_domain()`, `discovery_document(issuer)`
  moved here from test_org_sso_integration.py; `@dataclass class FakeOidcProvider(issuer, client_id, client_secret)`:
  `install(router: respx.MockRouter)` registers discovery, `/jwks`, `/token` routes; the token route checks Basic auth,
  `grant_type`, `code` and that `sha256(code_verifier)` matches the `code_challenge` captured from the authorization URL,
  and answers `{"access_token", "token_type": "Bearer", "id_token": provider.next_id_token}`; `id_token(*, nonce, now,
  key=None, alg="RS256", **claim_overrides) -> str` (defaults iss/aud/sub/email/email_verified/iat/exp=now+300).
  Helpers `start_sso(client, email) -> (response, query_params)` and `callback(client, state, code="code-1", **extra)`.
**apps/api/tests/test_oidc_tokens.py** (no DB): valid token → claims; wrong signature key; unknown kid; `alg` HS256 and
  `none` refused; wrong iss (incl. trailing-slash variant); wrong aud; aud list without azp; aud list with azp; expired
  (exp < now − 60); exp within skew accepted; iat in future; missing exp/iat; bad nonce; missing email;
  `email_verified` "true"/"false"/absent/invalid; malformed JWKS.
**apps/api/tests/test_sso_state.py** (fakeredis): put/take round trip; take twice → None; TTL 600; NX collision →
  SsoStateUnavailable; disconnected server (mem-1793264176-ecbf) → SsoStateUnavailable on put and take; malformed → None.
**apps/api/tests/test_oidc_discovery.py** — add `fetch_json`/`check_url` cases (insecure url, private address, non-object).
**apps/api/tests/test_sso_login_integration.py** (client fixture, `oidc_enabled=True`, respx `FakeOidcProvider`;
  connection created through `PUT /v1/org/sso` by a signed-in owner, then a fresh client for the SSO user):
  - start: 302 to the IdP authorize URL with `response_type=code`, `scope`, `state`, `nonce`, S256 `code_challenge`,
    `login_hint`, `redirect_uri`; `va_sso` cookie set with path/HttpOnly; Redis key `sso:state:{state}` TTL ≤ 600.
  - start failures → 302 `/login?sso_error=`: `disabled`, `invalid_email` (parametrize "", "nope", "a@b"),
    `not_configured`, `provider_unavailable` (discovery 500).
  - callback happy path, new user: 302 to dashboard `/`, session cookie works on `GET /internal/auth/session` with
    `mfa_passed: true`, membership role = connection `default_role`, user has no password and `email_verified`.
  - existing user without membership → membership with default_role; existing member keeps role (owner stays owner);
    existing unverified user becomes verified; disabled user → `account_disabled`.
  - user with MFA enabled lands with `mfa_passed: true` and passes the gate.
  - bad state (unknown), reused state (second callback with the same state/cookie), missing/mismatched `va_sso` cookie,
    IdP `error=access_denied`, missing code, token endpoint 400 → `provider_error`; bad nonce, wrong issuer, wrong
    audience, expired token, bad signature → `invalid_token`; `email_verified: false` → `email_not_verified`; missing
    claim with matching domain → success; email outside allowed_domains → `email_domain_mismatch`; connection deleted
    between start and callback → `not_configured`. Assert no session cookie and no user row in failures.
  - `va_sso` cleared (Max-Age=0) on success and failure; fixation: a presented `va_session` is revoked.
  - enforce: `enforce=true` → password login of a domain user → 403 `SSO_REQUIRED` with `details.sso_url` ending in
    `/internal/auth/sso/start?email=...`, failure counter untouched; `enforce=false` → normal login; `OIDC_ENABLED=false`
    with an enforced connection row → normal login; an org API key still authenticates on a scoped route
    (`probe_read`/existing helper) while enforce is on.
  Keep the file < ~400 lines; split enforce tests into `test_sso_enforce_integration.py` if needed. Reuse conftest
  fixtures (`container`, `client`, `identity`), `session_helpers.error_of`, `make_member`, `signup`/`login`.
**apps/api/tests/test_session_lifetime.py / identity repo tests** — `start_in(mfa_passed=True)` sets `mfa_passed_at`.
**apps/api/tests/test_org_sso_integration.py** — import moved helpers from `fake_oidc`.

### Gates
`make fmt lint typecheck placeholders test`; `make test-ts` (internal schema types if generated from internal.json);
`make openapi` + `uv run python scripts/check_openapi_drift.py`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) + average;
pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`; `uv run pre-commit run gitleaks --all-files`. build.done as a
PLAIN STRING with numeric complexity (mem-1793269824-fe85).

### Risks
- `authlib.jose` import anywhere (incl. transitively via `authlib.integrations.starlette_client`) warns → tests error.
  Use only `authlib.integrations.httpx_client` and joserfc.
- respx must intercept authlib's own `AsyncOAuth2Client` (it is an `httpx.AsyncClient`; respx patches httpcore, so OK)
  — create it inside the request, never at import.
- RSA key generation is slow: generate once per test session in `fake_oidc` (module global), not per test.
- RedirectResponse: set cookies on the returned response object, not an injected `Response` param.
- Pool size 1 per role: system-session lookup, tenant reads, and the JIT transaction never overlap; the code exchange
  and JWKS fetch happen with no DB session open.
- fakeredis `GETDEL` support (Redis ≥ 6.2): if missing, use a `MULTI` of GET + DEL.
- Login CSRF/fixation: callback must clear `va_sso` and revoke a presented session before setting new cookies.

## 2026-10-29 Builder: P02-T07b build notes
- **Deviation from plan (recorded in specs/04 §5 decision item 2):** `authlib.integrations.httpx_client` emits
  `AuthlibDeprecationWarning` ("httpx module is deprecated; please use httpx2") on import → error under
  `filterwarnings=error`. Used authlib's RFC 6749/7636 primitives (`prepare_grant_uri`, `create_s256_code_challenge`,
  `prepare_token_request`, `ClientAuth("client_secret_basic")`) in new `va_api/auth/oidc_client.py`; the token POST
  goes through `OidcDiscovery.post_form` (same guards as discovery). mypy override `authlib.*`
  ignore_missing_imports (authlib has no py.typed).
- `OidcDiscovery`: `_download` → `_send(method, url, body, headers)`; new `check_url`, `fetch_json`, `post_form`,
  reason `INSECURE_URL = "insecure url"`; `fetch` reasons unchanged (existing tests untouched and green).
- New: `auth/oidc_tokens.py` (joserfc), `auth/sso_state.py` (`SsoStateStore`, `SsoStateUnavailableError` - ruff N818
  wants the Error suffix), `auth/sso_urls.py` (`is_dns_domain` moved from org_sso_schemas), `routers/auth_sso.py`
  (`SsoLoginError` instead of plan's `SsoFailure`, same N818 reason; `REASONS` 10 reasons).
- `routers/auth.py`: `revoke_presented`/`set_session_cookies` public; `_check_sso_enforced` after throttle check.
- `SessionManager.start_in(mfa_passed=)`, `IdentityRepo.create_session(mfa_passed_at=)`; container +
  SessionContainer `sso_states`.
- OpenAPI: two 302-only internal operations; `test_openapi` shared check accepts `REDIRECT_OPERATIONS` (302, no
  body, no 2xx). `openapi/internal.json` regenerated; TS schema gen unchanged (built from openapi.json only).
- Tests: support `fake_oidc.py` (provider + `configure_sso` + `sso_login`), `test_oidc_tokens.py` (32),
  `test_sso_state.py` (21: store, URLs, authlib requests), discovery +2, `test_sso_login_integration.py` (20),
  `test_sso_enforce_integration.py` (4). `test_org_sso_integration.py` imports moved helpers.
- Gates: make fmt/lint/typecheck/placeholders pass; make test 2579 passed (99.05% cov) before a test-only jscpd
  refactor (fake_oidc `mocked_idp`/`sso_app`), after which ruff/mypy pass, jscpd 0 clones, and the 3 SSO
  integration modules re-run green (47 passed). radon no C+ (avg 1.95); spec refs 0; openapi drift current;
  pip-audit + pnpm audit clean; gitleaks passed. No TS changes (schema gen output unchanged).

## 2026-10-29 Planner: P02-T07b build.blocked (payload format only) → re-plan same task
Cause, from the events log: the T07b build.done payload wrote `tests pass`, `lint pass`, `audit pass`, `cov 99.05%`,
`jscpd 0 clones` (no `key: value` colons, no `coverage:`/`duplication:`/`specs:` keys). Every accepted build.done uses
the exact `key: pass` form (e.g. P02-T07a). The build notes above show all gates green; no code or design problem.
The "abandoned" event is the 3-in-a-row block counter for the format issue (T06b twice + this), not a design blocker.
Plan P02-T07b is unchanged; do not split.
Builder, this activation: do not redesign. Re-run the gates on the current tree (`make fmt lint typecheck placeholders
test`, `make openapi` + `uv run python scripts/check_openapi_drift.py`, `uv run python scripts/check_spec_refs.py`,
`npx jscpd@4`, radon, pip-audit per mem-1793267153-9b68, `corepack pnpm audit`, gitleaks), fix anything red, compute
`uvx radon cc -s -a packages/va_common/src packages/va_db/src apps/api/src | tail -1`, and emit a PLAIN STRING exactly
in this shape (no extra prose before the keys; put the authlib deviation note at the very end if at all):
`ralph emit "build.done" "P02-T07b tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

## 2026-10-29 Builder: P02-T07b gate re-run (no code changes)
make fmt/lint/typecheck/placeholders exit 0; make test 2579 passed, coverage 99.05%; openapi drift current (both
docs); spec refs 0; jscpd 0 clones; radon no C+ (avg 1.95); pip-audit + pnpm audit clean; gitleaks passed.

## Review P02-T07b (2026-10-29): approved
Re-ran make lint/typecheck/placeholders (exit 0), make test 2579 passed 99.05%, openapi drift current, spec refs 0,
jscpd 0 clones, radon no C+. Diff checked against specs/04 §5 decision (reasons, state/cookie, ID-token checks, email
rule, JIT, enforcement), specs/31 SSO_REQUIRED 403, specs/03 §11 `sso:state:{state}`. No findings.

## 2026-10-29 Planner: P02-T07b approved → P02-T07 ticked; P02-T08 split into T08a/T08b/T08c
T07a and T07b both ticked, so the parent P02-T07 is ticked. P02-T08 (me, org, members, invitations, matrix test)
is too big for one activation: T08a matrix + `/v1/me` + `GET/PATCH /v1/org`; T08b members + transfer-ownership;
T08c invitations (v1 management + internal details/accept). T09–T12 depend on the parent P02-T08 (ticked when all
three are). Org settings validation (`OrgSettings`, `settings` in `PATCH /v1/org`, `/v1/org/settings`) stays in T12.

## Plan P02-T08a — RBAC matrix table, `/v1/me`, `GET/PATCH /v1/org` (specs/04 §3.2, §3.3, §8.1; specs/24 §2)

### Facts found while planning
- `va_api.auth.rbac.ROLE_SCOPES` and `va_api.auth.permissions.ROLE_PERMISSIONS` are hand-written sets; exact-set
  tests live in `apps/api/tests/test_rbac.py` and `test_permissions.py` (the latter parses the §3.3 permission list).
  Both sets are derivable exactly from §3.2 with the rule below (checked row by row while planning).
- `org_router = envelope_router(prefix="/v1/org", tags=["Org"])` lives in `routers/org_sso.py` with the comment
  "T08 attaches the remaining /v1/org routes to this router"; routes use `ReadOrg`/`WriteOrg` Annotated deps and
  `openapi_extra={"x-scope": ..., "x-permission": ...}`; `main.py` includes `org_router`.
- `orgs` is a global identity table (no RLS, specs/03 §3 decision): read/write it through `Database.global_session()`.
  `plans` is system-scope but granted to `va_app` without RLS, so `global_session()` can read it too.
- No tenant `ApiKeyRepo` yet (only `ApiKeyLookupRepo`); test API keys come from the in-memory `KeyStore`
  (`container.store.issue(org_id=..., scopes=...)`), not the DB.
- Money: `va_common.money.micros_to_dollars(micros) -> Decimal`.
- `orgs.limits` override keys (specs/24 §2): `connected_rate_micros_per_min`, `transfer_rate_micros_per_min`,
  `sms_rate_micros`, `widget_message_micros`, `daily_calls`, `hourly_calls`, `concurrency`, `voice_clones`, `numbers`,
  `international_enabled`, `sms_per_day`. Plan caps: `daily_call_cap`, `hourly_call_cap`, `concurrency_cap`,
  `voice_clone_cap` (NULL = unlimited).

### Decisions to record (same commit, `> Decision (2026-10-29):`)
1. specs/04 §3.3 (after the permissions note): **matrix in code**. `va_api.auth.rbac_matrix.MATRIX` holds every
   §3.2 row (label text exactly as in the spec, backticks included) → cell per role, cells one of `R`, `R*`, `W`,
   `W(own)`, `X`, `—`. Each row maps to a scope resource (or none) and zero or more permissions (`ROW_GRANTS`). A role
   holds `<resource>:read` when a row of that resource has a non-`—` cell and `<resource>:write` when it has `W`,
   `W(own)` or `X` (only scopes that exist in §2.3); it holds a permission when any row mapped to it has a non-`—`
   cell. `ROLE_SCOPES` and `ROLE_PERMISSIONS` are derived from `MATRIX`; the exact sets in the two notes above are
   unchanged. `apps/api/tests/test_rbac_matrix.py` parses §3.2 and compares cell-for-cell.
2. specs/04 §8.1: **`/v1/me`** needs `org:read` (every role holds it; API keys need `org:read` or `*`). Response
   `{data:{principal_type, user, api_key, org:{id,name,slug,plan_id,status}, role, scopes}}` — `user` is null for API
   keys and `api_key` null for sessions (keys always present); `scopes` sorted. The API-key branch builds
   `{id, prefix, scopes, environment}` from the context (`principal_id`, `scopes`, `environment`) plus the
   `api_keys.prefix` from the DB (tenant read); a key row that vanished answers `AUTH_FAILURE`.
3. specs/04 §8.1: **`GET /v1/org`** (`org:read`) → `{data:{id, name, slug, parent_org_id, plan_id, kyc_level, status,
   data_region, retention_days, recording_retention_days, credits_balance, limits, settings, created_at,
   updated_at}}`; `credits_balance` is a JSON number of dollars (`micros_to_dollars`); `limits` = plan caps as
   `daily_calls`/`hourly_calls`/`concurrency`/`voice_clones` (null = unlimited) overlaid by every specs/24 §2 override
   key present in `orgs.limits` (unknown keys ignored); `settings` is the stored object until `OrgSettings`
   (P02-T12) returns it with defaults. **`PATCH /v1/org`** (`org:write`) accepts `{name? (1–200 chars, trimmed),
   retention_days? (1–3650), recording_retention_days? (0–3650)}` (`extra="forbid"`); retention fields additionally
   need `org.compliance` (`FORBIDDEN` "Missing required permission 'org.compliance'."); an empty body is a no-op
   returning the org; `settings` joins this body in P02-T12. Audit `org.update` arrives with P02-T11.

### Files
**apps/api/src/va_api/auth/rbac_matrix.py** (new):
  - `type Cell = Literal["R", "R*", "W", "W(own)", "X", "—"]`; `ROLES: Final[tuple[MemberRole, ...]] = ("owner",
    "admin", "developer", "analyst", "billing", "viewer")` (spec column order).
  - `@dataclass(frozen=True, slots=True) class RowGrant: resource: str | None; permissions: frozenset[str] =
    frozenset()`.
  - `MATRIX: Final[Mapping[str, Mapping[MemberRole, Cell]]]` — all 47 rows of §3.2 in spec order (MappingProxyType;
    build each row with a small `_row("R","R","R","R","—","R*")` helper zipping `ROLES` to keep it compact and
    jscpd-safe).
  - `ROW_GRANTS: Final[Mapping[str, RowGrant]]` — resource per row: Calls×4→`calls` (send row + `calls.operate`);
    Batches→`batches`; Pathways rows (incl. tests)→`pathways` (publish row + `pathways.publish`); Personas→`personas`;
    Agents and Experiments→`agents` (experiments + `experiments.operate`); Tools→`tools`; Knowledge→`knowledge`;
    Voices catalog→`voices`, clone→`voices` + `voices.clone`; Phone numbers view, pools→`numbers`; purchase/release→
    `numbers` + `numbers.purchase`,`numbers.release`; BYO Telnyx→`numbers` + `telnyx_accounts.manage`; SMS view/send→
    `sms`; 10DLC→`sms` + `tendlc.manage`; Widgets→`widgets`; Memory/contacts→`memory`; Memory reset/erasure→`memory` +
    `memory.erase`; Dispositions→`analysis`; DNC, Guard rail→`compliance`; Webhook endpoints→`webhooks`; signing
    secret→`webhooks` + `webhooks.secret`; Analytics→`analytics`; Prompts→`prompts`; Billing balance→`billing`;
    top-up→`billing` + `billing.payment`; plan change→`billing` + `billing.plan_change`; credit transfer→`billing` +
    `billing.credit_transfer`; API keys list→`None`; API keys create/revoke→`api_keys`; Members list→`org`; Members
    invite→`org` + `members.manage`; Assign owner→`org` + `org.owner_transfer`; Org settings general→`org`; Org settings
    compliance→`org` + `org.compliance`; SSO→`org` + `sso.manage`; Sub-accounts→`org` + `subaccounts.manage`; Audit
    logs→`org` + `audit.read`; Delete org→`org` + `org.delete`. Module asserts at import nothing; a test asserts
    `ROW_GRANTS.keys() == MATRIX.keys()` and every permission ∈ `PERMISSIONS` (import PERMISSIONS from a new
    leaf location to avoid a cycle: move the `PERMISSIONS`/`SESSION_ONLY_PERMISSIONS` constants into
    `rbac_matrix.py` and re-export them from `permissions.py`, keeping `permissions.__all__` and the
    test_permissions spec-paragraph parse unchanged).
  - `def role_scopes(role: MemberRole) -> frozenset[str]` and `def role_permissions(role: MemberRole) ->
    frozenset[str]` implementing decision 1 (`_READABLE = {"R","R*","W","W(own)","X"}`, `_WRITABLE =
    {"W","W(own)","X"}`; scope kept only if in `va_api.auth.scopes.SCOPES`).
  - `def cell(row: str, role: MemberRole) -> Cell`.
**apps/api/src/va_api/auth/rbac.py** — `ROLE_SCOPES = MappingProxyType({role: role_scopes(role) for role in ROLES})`;
  drop the hand-written sets; docstring cites the matrix. `scopes_for_role` unchanged.
**apps/api/src/va_api/auth/permissions.py** — `ROLE_PERMISSIONS` derived via `role_permissions`; constants imported
  from `rbac_matrix`; `_BILLING` removed.
**packages/va_db/src/va_db/repositories/orgs.py** (new; export in `repositories/__init__.py`):
  - `@dataclass(frozen=True, slots=True) class OrgOverview: org: Org; plan: Plan | None`.
  - `class OrgRepo` (global table; works on `global_session()`): `__init__(session)`, `async def get(org_id) -> Org |
    None`, `async def overview(org_id) -> OrgOverview | None` (outer join `plans` on `plans.id == orgs.plan_id`),
    `async def update(org_id, *, name: str | None = None, retention_days: int | None = None,
    recording_retention_days: int | None = None) -> Org | None` (UPDATE … RETURNING only the provided columns +
    `updated_at = now()`; nothing provided → `get`). Docstring: callers must pass the context's org id (global table,
    no RLS) — the route always uses `ctx.org_id`.
  - Add to `ApiKeyLookupRepo`? No: add a tenant `ApiKeyRepo(session, org_id)` in `repositories/api_keys.py` with
    `async def get(key_id) -> ApiKey | None` (filters `org_id`; T10 extends it). Export it.
**apps/api/src/va_api/routers/org_schemas.py** (new): `OrgSummary(id, name, slug, plan_id, status)`, `MeUser(id,
  email, name, mfa_enabled)`, `MeApiKey(id, prefix, scopes: list[str], environment)`, `MeView(principal_type, user:
  MeUser | None, api_key: MeApiKey | None, org: OrgSummary, role: MemberRole | None, scopes: list[str])`,
  `OrgLimits` (`daily_calls`, `hourly_calls`, `concurrency`, `voice_clones`: `int | None`; optional override fields
  `connected_rate_micros_per_min`, `transfer_rate_micros_per_min`, `sms_rate_micros`, `widget_message_micros`,
  `numbers`, `sms_per_day`: `int | None = None`, `international_enabled: bool | None = None`; serialize with
  `exclude_none`? No — keep a fixed shape, all keys present, null when not overridden), `OrgView(...)` per decision 3
  (`credits_balance: float`), `OrgPatch` (`model_config = ConfigDict(extra="forbid")`, `name: Annotated[str,
  StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None`, `retention_days:
  Annotated[int, Field(ge=1, le=3650)] | None = None`, `recording_retention_days: Annotated[int, Field(ge=0,
  le=3650)] | None = None`). Field descriptions + examples (no None examples: FastAPI drops them).
  `def effective_limits(plan: Plan | None, overrides: Mapping[str, object]) -> OrgLimits` (pure; ignores unknown
  keys and wrongly typed values — `bool` only for `international_enabled`, `int` (not bool) for the rest).
**apps/api/src/va_api/routers/org.py** (new): attaches to `org_router` (import from `routers/org_sso.py`; move
  `org_router`, `ReadOrg`, `WriteOrg` into this new module? Keep them where they are to avoid churn, and import).
  Also defines `me_router = envelope_router(prefix="/v1", tags=["Org"])` with `GET /me` (`operation_id="me_get"`,
  `x-scope: org:read`). Routes: `GET /v1/org` (`org_get`), `PATCH /v1/org` (`org_update`, `x-scope: org:write`;
  inside the handler: if `retention_days` or `recording_retention_days` is set and `not holds_permission(ctx,
  "org.compliance")` → `FORBIDDEN` with the require_permission message; declare `x-permission-conditional:
  org.compliance`? Not in specs/26 — instead document it in the operation description). Helpers `_org_view(overview)`,
  `_load(request, org_id)` (`NOT_FOUND` "Organization not found." if missing). `/v1/me`: user branch reads the user via
  `IdentityRepo.get_user` in `global_session()`; API-key branch reads the key in `tenant_session(ctx.org_id)`; org from
  `OrgRepo.get` (separate sequential sessions; pool size 1 — never nest). `principal_type` platform_admin reports as
  is. Register `me_router` in `main.py` and import `va_api.routers.org` for side effects (pattern of `auth_mfa`).
**apps/api/src/va_api/openapi.py** — only if tags need it (`Org` tag exists already; verify).
**specs/04 §3.3, §8.1** — decisions 1–3. **AGENTS.md** — `rbac_matrix` is the single RBAC source; editing §3.2
  requires editing `MATRIX`/`ROW_GRANTS`; `/v1/org` reads `orgs` via `global_session` with `ctx.org_id` only.
**openapi/openapi.json** regenerated (`make openapi`), then `corepack pnpm gen:api` in `sdks/typescript` and
  `apps/dashboard` (mem-1793271457-e7c7) and commit generated schema changes.

### Tests
**apps/api/tests/test_rbac_matrix.py** (no DB): `_spec_matrix()` reads specs/04, slices from `### 3.2` to the next
  blank line after the table, splits rows on `|`, strips cells; asserts header roles == `ROLES`; asserts
  `list(MATRIX) == spec row labels` (order and text) and every cell equal (parametrize by row label for readable
  failures); `ROW_GRANTS.keys() == MATRIX.keys()`; every grant permission ∈ PERMISSIONS and every permission is granted
  by ≥ 1 row; every grant resource yields an existing scope; `role_scopes`/`role_permissions` equal `ROLE_SCOPES`/
  `ROLE_PERMISSIONS`; mutation check: flipping one cell (build a modified matrix through the private derivation
  helper taking a matrix argument, e.g. `_derive_scopes(matrix, role)`) changes the derived set (proves derivation is
  live). Existing `test_rbac.py`/`test_permissions.py` stay unchanged and green.
**apps/api/tests/test_org_limits.py** (no DB): `effective_limits` with plan caps, None plan, overrides replacing caps,
  unknown keys ignored, bool-as-int refused, string values ignored, international flag.
**apps/api/tests/test_org_integration.py** (client fixture, real DB): `/v1/me` for owner session (user block, role,
  scopes == sorted ROLE_SCOPES["owner"], api_key null); for viewer; for a `*` API key whose DB row exists (helper
  `store_api_key(identity, container, org_id, scopes)` in `session_helpers.py`: issues via `container.store.issue(org_id=
  ..., scopes=...)` then inserts an `ApiKey` row with the same id/prefix/hash via `system_session`) → api_key block
  with prefix, user null, role null; API key with `calls:read` only → 403 `FORBIDDEN`; API key without a DB row → 401
  `AUTH_FAILURE`; no credentials → 401. `GET /v1/org` for each of the six roles → 200 with plan-derived limits (build
  plan seeded: daily 2000 etc. — read expected values from the `plans` row in the test, not literals) and an
  `orgs.limits` override (`{"concurrency": 7}`) reflected; `credits_balance` 12.5 for 12_500_000 micros. `PATCH
  /v1/org`: owner renames (trimmed) → 200 and persisted; admin sets retention → 200; developer/analyst/viewer/billing
  rename → 403 `FORBIDDEN`; `*` API key renames → 200 but retention → 200 too (keys with `*` hold org.compliance);
  an `org:write`-only key setting retention → 403; invalid values (`name: ""`, `retention_days: 0`, `4000`,
  `recording_retention_days: -1`, unknown field `settings`) → 400 `INVALID_INPUT` with field; empty body → 200
  unchanged; CSRF missing on session PATCH → 403 (existing CSRF code). Cross-org: PATCH as org A never touches org B
  (assert B's row unchanged).
**apps/api/tests/test_openapi.py** — add the new operations to whatever explicit lists exist (check `x-scope`
  assertions) and ensure `org_update`'s description mentions `org.compliance`.

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` + `uv run python scripts/check_openapi_drift.py`;
`corepack pnpm gen:api` (sdks/typescript, apps/dashboard) + `make test-ts`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and
`uvx radon cc -s -a ... | tail -1` for the numeric average; pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files`. build.done as a PLAIN STRING in the exact `key: value` form
(mem-1793273041-6642, mem-1793269824-fe85):
`ralph emit "build.done" "P02-T08a tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Import cycle: `rbac_matrix` must import only `va_db.models.MemberRole` and `va_api.auth.scopes`; `permissions.py`
  and `rbac.py` import from it, never the reverse.
- The spec table uses an em dash `—` for denied cells and a backtick in "Assign `owner` role / transfer ownership";
  compare labels verbatim.
- `orgs` has no RLS: every `OrgRepo` call must use `ctx.org_id`; never accept an org id from the request in T08a.
- Pool size 1 per role: `/v1/me` opens global and tenant sessions sequentially, never nested.
- `credits_balance` float rounding: micros are integers; `float(micros_to_dollars(m))` is exact enough for display.
- jscpd: reuse `make_member`, `error_of`, `write_headers`, conftest fixtures; parametrize role cases.

## 2026-10-29 Builder: P02-T08a build notes
- Built per plan: `auth/rbac_matrix.py` (MATRIX 47 rows, ROW_GRANTS, derivation; PERMISSIONS/SESSION_ONLY moved here,
  re-exported), `rbac.py`/`permissions.py` derived; `va_db.repositories.OrgRepo`/`OrgOverview` (global session) and tenant
  `ApiKeyRepo`; `routers/org_schemas.py` (+ `effective_limits`), `routers/org.py` (`me_router` GET /v1/me; GET/PATCH
  /v1/org on `org_router`); `session_helpers.store_api_key`; specs/04 §3.3 + §8.1 decisions; AGENTS.md entry;
  openapi.json + TS schemas regenerated.
- Deviation from plan (recorded in the §8.1 decision): `limits` takes the plan's rates as well as caps (spec says
  "effective (plan ⊕ overrides)"); `numbers`/`international_enabled`/`sms_per_day` null unless overridden.
- AUTH_FAILURE messages reuse `INVALID_API_KEY` / `SESSION_INVALID`.
- FastAPI drops None members from OpenAPI examples (MeView/OrgLimits examples render without null keys).
- Gates: make fmt/lint/typecheck/placeholders pass; make test 2682 passed, coverage 99.00%; make test-ts pass;
  openapi drift current; check_spec_refs 0; jscpd 0 clones; radon no C+, avg 1.95; pip-audit + pnpm audit clean;
  gitleaks passed.

## Review P02-T08a (2026-10-29): approved
- Re-ran: make lint typecheck placeholders (pass), make test 2682 passed cov 99.00%, make test-ts pass,
  check_openapi_drift current, check_spec_refs 0, jscpd 0 clones.
- Diff checked against specs/04 §3.2/§3.3/§8.1: matrix rows verbatim and parse-tested; derived role sets equal the
  previous exact sets (test_rbac/test_permissions unchanged); /v1/org uses ctx.org_id only on global session;
  retention fields gated by org.compliance; sessions opened sequentially. No findings.

## 2026-10-29 Planner: P02-T08a approved → next P02-T08b

## Plan P02-T08b — Members: list, change role, remove, transfer ownership (specs/04 §6, §3.2 Members rows, §4.1 recent auth)

### Facts found while planning
- `memberships` is RLS (tenant_session); `users` is a global table readable by `va_app` inside a tenant_session, so
  list = one tenant_session joining `memberships`→`users`.
- Session auth re-reads the membership on every request (`active_membership` → `MembershipLookupRepo.for_user_in_org`);
  a deleted membership makes the session fall back to the user's oldest remaining membership (persisted) or
  `FORBIDDEN` "You are not a member of any organization.". So "removed members' sessions lose the org" needs no
  session writes — only a test.
- `current_session` stores the session on `request.state.session` (set during `get_principal`); the transfer route
  reads it there for the recent-auth check. `require_recent_auth(active, now)` lives in `auth/password_reset.py` with a
  password-specific message.
- Matrix rows: `Members: list` → `org` (org:read); `Members: invite / change role / remove` → `org` +
  `members.manage` (owner, admin; API keys with `*`); `Assign owner role / transfer ownership` → `org` +
  `org.owner_transfer` (owner only, session-only → API keys never hold it).
- Audit (`member.role_change`, `member.remove`, `org.ownership_transfer`) arrives with P02-T11 (it enumerates every
  mutating endpoint) — do not write audit rows now.
- Pool size 1 per role: never nest sessions.

### Decisions to record (specs/04 §6, same commit, `> Decision (2026-10-29):`)
1. **Members list**: `GET /v1/org/members` (`org:read`, every role; API keys need `org:read`/`*`) →
   `{data:[{user_id, email, name, role, mfa_enabled, last_login_at, joined_at}]}`, `joined_at` = membership
   `created_at`, ordered `joined_at`, `user_id`; unpaginated (bounded by membership count; the spec's shape has no
   cursor).
2. **Change role**: `PATCH /v1/org/members/{user_id}` `{role}` (`extra="forbid"`, role ∈ six roles) needs `org:write` +
   `members.manage`; additionally `org.owner_transfer` when the new role is `owner` **or** the target currently is an
   owner (admins and API keys cannot touch owners) → `FORBIDDEN` "Missing required permission 'org.owner_transfer'.".
   Unknown user id or a user of another org → `NOT_FOUND` "Member not found.". Same role → 200 no-op. Response
   `{data: <member view>}`.
3. **Remove**: `DELETE /v1/org/members/{user_id}` same permissions (owner target needs `org.owner_transfer`), returns
   `{data:{user_id, removed: true}}`. Removing oneself is allowed under the same rules. The removed user's sessions are
   not revoked: the next request re-reads the membership and falls back to another org (or `FORBIDDEN`).
4. **Last-owner rule**: any demotion or removal of an owner first locks the org row (`SELECT … FROM orgs WHERE id =
   :org FOR UPDATE`, inside the same tenant_session), then counts owners; if the change would leave zero owners →
   `CONFLICT` "An organization must have at least one owner.". The org-row lock serialises concurrent demotions so
   two owners cannot demote each other at once.
5. **Transfer ownership**: `POST /v1/org/transfer-ownership` `{user_id}` needs `org:write` + `org.owner_transfer`
   (owner session) and recent auth (10 min since `max(created_at, mfa_passed_at)`, else `REAUTH_REQUIRED` "Sign in
   again to transfer ownership."). Target must be a member of the org (`NOT_FOUND` "Member not found." otherwise);
   target == caller → `INVALID_INPUT` (field `user_id`) "You already own this organization.". Effect, in one
   transaction under the org-row lock: target → `owner`, caller → `admin` (other owners unchanged). Response
   `{data:{previous_owner: <member view of caller>, owner: <member view of target>}}`. Not idempotent-key-bound
   beyond the existing `require_scope` claim.

### Files
**packages/va_db/src/va_db/repositories/memberships.py** — extend `MembershipRepo`:
  - `@dataclass(frozen=True, slots=True) class MemberRecord: user_id, email, name, role: MemberRole, mfa_enabled: bool,
    last_login_at: datetime | None, joined_at: datetime` (export).
  - `async def list_members(self) -> list[MemberRecord]` (join `User`, filter `Membership.org_id == self.org_id`,
    order `Membership.created_at, Membership.user_id`).
  - `async def member(self, user_id) -> MemberRecord | None`.
  - `async def lock_org(self) -> None` — `select(Org.id).where(Org.id == self.org_id).with_for_update()`.
  - `async def owner_count(self) -> int`.
  - `async def set_role(self, user_id, role) -> bool` (UPDATE … SET role, updated_at=now() WHERE org_id, user_id;
    returns rowcount == 1).
  - `async def remove(self, user_id) -> bool` (DELETE … WHERE org_id, user_id).
  - Module-level `LAST_OWNER_MESSAGE: Final = "An organization must have at least one owner."` and a method
    `async def change_role(self, user_id, role) -> MemberRecord | None` / `async def remove_member(self, user_id) ->
    bool` that implement decision 4 (lock → load target → if target is owner and new role ≠ owner (or removal) and
    `owner_count() <= 1` → `AppError("CONFLICT", LAST_OWNER_MESSAGE)`; returns None/False when the target is not a
    member), and `async def transfer_ownership(self, from_user_id, to_user_id) -> tuple[MemberRecord, MemberRecord] |
    None` (lock; both must be members; caller must currently be owner else return None → route answers FORBIDDEN is
    unreachable since permission gate already checked role; set target owner, caller admin). Keep each function under
    radon C. Docstrings cite specs/04 §6.
**apps/api/src/va_api/auth/password_reset.py** — `require_recent_auth(active, now, message: str = REAUTH_MESSAGE)`
  (keyword `message`); add `TRANSFER_REAUTH_MESSAGE: Final = "Sign in again to transfer ownership."` to a better home:
  put it in the new router module instead (keep password_reset unchanged except the parameter).
**apps/api/src/va_api/routers/org_members_schemas.py** (new): `MemberView(user_id, email, name, role, mfa_enabled,
  last_login_at: datetime | None, joined_at)` with `from_record(record)` classmethod or module function;
  `MemberRolePatch(role: MemberRole)` `extra="forbid"`; `MemberRemoved(user_id, removed: Literal[True])`;
  `TransferOwnershipRequest(user_id: uuid.UUID)` `extra="forbid"`; `OwnershipTransferred(previous_owner: MemberView,
  owner: MemberView)`. Field descriptions + examples (no None examples).
**apps/api/src/va_api/routers/org_members.py** (new) on `org_router` (from `routers/org_sso.py`):
  - `GET /members` `operation_id="org_members_list"`, `x-scope: org:read`, dep `ReadOrg` → `DataResponse[list[MemberView]]`.
  - `PATCH /members/{user_id}` `org_members_update`, `x-scope: org:write`, deps `WriteOrg` +
    `Depends(require_permission("members.manage"))` (use the `dependencies=[...]` list pattern of org_sso `_MANAGE`).
  - `DELETE /members/{user_id}` `org_members_remove`, same.
  - `POST /transfer-ownership` `org_transfer_ownership`, `x-scope: org:write`, deps `WriteOrg` +
    `require_permission("org.owner_transfer")`; reads `request.state.session` (an `ActiveSession`; if absent →
    `FORBIDDEN` — unreachable for sessions but typed) and calls `require_recent_auth(active, clock_of(request).now(),
    message=TRANSFER_REAUTH_MESSAGE)` BEFORE opening the DB session.
  - helper `_owner_guard(ctx, target_role: MemberRole, new_role: MemberRole | None)`: raises FORBIDDEN with the
    `require_permission` message for `org.owner_transfer` when (new_role == "owner" or target_role == "owner") and not
    `holds_permission(ctx, "org.owner_transfer")`. Order inside the tenant_session: lock, load target (NOT_FOUND),
    guard, last-owner check, write. (So the guard needs the target's current role → do it via the repo returning the
    target first; simplest: route opens `tenant_session(ctx.org_id)`, `repo.lock_org()`, `target = await
    repo.member(user_id)`, guard, then `repo.change_role/remove_member` which re-checks last owner. Keep the repo
    methods doing lock+check so they are safe on their own; calling `lock_org` twice in one tx is harmless.)
  - Import the module for side effects in `va_api/main.py` (pattern of `va_api.routers.org`).
**specs/04 §6** — decisions 1–5. **AGENTS.md** — members routes live in `routers/org_members.py`; last-owner rule uses
  the org-row lock in `MembershipRepo`; T08c must define `/members/invitations` routes so they never collide with
  `/members/{user_id}` (uuid-typed; GET has no `{user_id}` route).
**openapi/openapi.json** regenerated + `corepack pnpm gen:api` in `sdks/typescript` and `apps/dashboard`.

### Tests
**packages/va_db/tests/test_membership_repo.py** (identity_db fixture, real DB): list order and fields (two members,
  `last_login_at` null/value); `member()` none for another org's user (provision two orgs); `change_role` demoting the
  only owner → CONFLICT, demoting one of two owners → ok; `remove_member` last owner → CONFLICT; `transfer_ownership`
  swaps roles; **concurrency (deterministic)**: two `Database` instances (`open_database(url)` twice, each with its own
  pool), org with owners A and B; task 1 opens tenant_session on db1, `change_role(A, "admin")` and then awaits an
  `asyncio.Event` before committing; task 2 on db2 starts `change_role(B, "admin")`; poll (bounded loop, ≤ 200
  iterations of `pg_stat_activity` via a system session on a third pool/db1 system engine, checking a backend with
  `wait_event_type = 'Lock'`) until task 2 is blocked; set the event; assert task 1 succeeded and task 2 raised CONFLICT
  and one owner remains. Plus an `asyncio.gather` variant asserting exactly one CONFLICT. Use pytest.mark.timeout as
  other DB tests do; never sleep.
**apps/api/tests/test_org_members_integration.py** (conftest client fixtures): helper `add_member(identity, hasher,
  org_id, role, *, email=None) -> User` in `support/session_helpers.py` (create user via IdentityRepo in a
  `tenant_session(org_id)` + `MembershipRepo.add`), and move `signed_in` from test_org_integration into session_helpers
  (update that module's import; jscpd).
  - list: every role (parametrize) sees all members with the documented keys; `*` API key lists; `calls:read` key →
    403 FORBIDDEN.
  - change role: owner promotes developer→admin (200, persisted); admin changes developer→viewer (200); admin
    promoting to owner → 403 FORBIDDEN with `org.owner_transfer` message; admin demoting an owner → 403; owner
    promotes admin→owner (200, two owners); sole owner demotes self → 409 CONFLICT exact message; developer/analyst/
    billing/viewer PATCH → 403; `*` API key changes developer→analyst (200) but →owner 403; user id of another org →
    404 NOT_FOUND "Member not found."; random uuid → 404; invalid role / extra field → 400 INVALID_INPUT field `role`;
    CSRF missing → 403.
  - remove: admin removes developer (200 `{user_id, removed: true}`, row gone); removed user's session: log in as the
    developer in a second client BEFORE removal, then `/v1/me` after removal → 403 FORBIDDEN "You are not a member of
    any organization." (and with a second membership elsewhere → `/v1/me` reports that other org); admin removing an
    owner → 403; sole owner removing self → 409; owner removing a co-owner → 200; cross-org id → 404.
  - transfer: owner (fresh login) transfers to a developer → 200, target owner, caller admin; then the former owner's
    `/v1/me` role is `admin`; after `container.clock` advances 10 min → 401 REAUTH_REQUIRED "Sign in again to transfer
    ownership."; MFA verification refreshes recency (reuse `enrol_mfa` + `/internal/auth/mfa/verify` pattern only if
    cheap; otherwise covered by existing password-change recency tests — skip); admin → 403; `*` API key → 403
    (session-only permission); target not a member / other org → 404; target == self → 400 INVALID_INPUT field
    `user_id`; extra field → 400.
**apps/api/tests/test_password_reset*.py** (whichever covers `require_recent_auth`) — one unit assert that the custom
  `message` is used.
**apps/api/tests/test_openapi.py** — assert the four operations' ids and `x-scope` values (pattern of the `/v1/org`
  assertions at line ~276).

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` + `uv run python scripts/check_openapi_drift.py`;
`corepack pnpm gen:api` (sdks/typescript, apps/dashboard) + `make test-ts`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and
`uvx radon cc -s -a ... | tail -1` for the numeric average; pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files`. build.done as a PLAIN STRING (mem-1793273041-6642, mem-1793269824-fe85):
`ralph emit "build.done" "P02-T08b tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- `SELECT … FOR UPDATE` on `orgs` as `va_app` needs UPDATE privilege on `orgs` (it has it: `OrgRepo.update` runs as
  va_app) — verify in the repo test.
- Under READ COMMITTED, owner counting must happen AFTER the org-row lock is acquired (fresh snapshot per statement).
- `request.state.session` is the session as resolved before `set_active_org`; only `created_at`/`mfa_passed_at` are
  used, which do not change.
- Route ordering with T08c: keep `{user_id}` typed `uuid.UUID`; FastAPI answers 400 INVALID_INPUT for non-uuid ids
  (check the existing validation handler) — test one non-uuid id.
- jscpd: reuse `error_of`, `write_headers`, `login`, `make_member`; parametrize role cases; share the role guard.

## 2026-10-29 Builder: P02-T08b build notes
- Built per plan: `MembershipRepo` gains `MemberRecord`, `list_members`, `member`, `lock_org` (FOR UPDATE on orgs),
  `owner_count`, `set_role`/`remove` (RETURNING id), `change_role`/`remove_member`/`transfer_ownership` (lock →
  load → last-owner check), `LAST_OWNER_MESSAGE`, `NOT_OWNER_MESSAGE` (caller no longer owner under a race → FORBIDDEN).
  `routers/org_members.py` + `org_members_schemas.py` (4 operations, `x-permission` like org_sso);
  `require_recent_auth(..., message=)`; specs/04 §6 four decisions; AGENTS.md entry.
- Extra (needed by test_operations_contract, every 2xx needs examples): `DataResponse[list[M]]` now wraps each
  example of M as a one-item list (`responses._wrap_examples`), unit-tested in test_openapi.
- Tests: packages/va_db/tests/test_membership_repo.py (8, incl. deterministic lock-wait via pg_locks polling and a
  gather variant); apps/api/tests/test_org_members_integration.py (21); `signed_in` moved to session_helpers (+
  `add_member`); test_openapi operation/scope/permission asserts; test_password_reset custom message.
- Coverage of org_members.py reports 88% with alternating "missed" lines inside `async with tenant_session` bodies
  that are executed by passing tests (same greenlet tracing artifact as routers/org.py lines 75/83).
- Two `make test` runs hit pytest-timeout 60s in Docker exec during migrated_url setup (43 then 18 errors, different
  modules each run, host load 11–14); isolated reruns green; third full run green.
- Gates: fmt/lint/typecheck/placeholders pass; make test 2714 passed, coverage 98.91%; make test-ts pass; openapi
  drift current; check_spec_refs 0; jscpd 0 clones; radon no C+, avg 1.95; pip-audit + pnpm audit clean; gitleaks pass.

## Review P02-T08b (2026-10-29): approved
- Re-ran lint/typecheck/placeholders (pass), make test (2714 passed, 98.91%), openapi drift current, spec refs 0.
- Diff checked against specs/04 §3.2 matrix rows (Members list R all roles; change/remove members.manage; owner
  assignment org.owner_transfer) and §6 (exact CONFLICT message). updated_at covered by set_updated_at trigger.
  Lock-then-count under the org row; concurrency tests poll pg_locks with no sleeps. No findings.

## 2026-10-29 Planner: P02-T08b approved → next P02-T08c

## Plan P02-T08c — Invitations: create, list, revoke, details, accept (specs/04 §6, §4.1 invitation rows, §4.2 token format; specs/03 §2, §3 `invitations`; specs/31 TOKEN_EXPIRED)

### Facts found while planning
- `invitations` (specs/03 l.164, model `va_db.models.tenancy.Invitation`) is an RLS tenant table: id, org_id, email
  citext, role, token_hash UNIQUE, invited_by uuid NOT NULL → users, expires_at, accepted_at, created_at/updated_at.
  **No revoked column** → revoke = DELETE the row. Retention already purges by `expires_at`.
- The accept/details endpoints know only the token, so the lookup needs a `va_system` repo (like
  `ApiKeyLookupRepo`/`SsoConnectionLookupRepo`): add `InvitationLookupRepo` to the specs/03 §2 `va_system` bullet +
  a `> Decision` (test_repositories.py enforces the set).
- `invited_by` is NOT NULL → users. API keys with `*` hold `members.manage` (matrix row "Members: invite / change
  role / remove"), so an API-key caller invites as the key's `api_keys.created_by` (nullable FK) via
  `ApiKeyRepo(session, ctx.org_id).get(ctx.principal_id)`; null → FORBIDDEN (decision 2).
- Reuse: `va_common.auth.issue_token()` / `looks_like_token()` / `sha256` hashing as email tokens;
  `va_api.auth.verification.deliver(..., failure_event=)` + `dashboard_link(base, "invite", token)`;
  `check_password_policy_async`; `routers/auth._check_sso_enforced` (make public `check_sso_enforced`, update the
  login call); `routers/auth.set_session_cookies`; `SessionManager.start_in` / `remember` / `set_active_org`;
  `session_auth.check_dashboard_origin`, `_user_mfa_enabled`/`mfa_due`; `conflict_savepoint(session, msg, field=)`;
  `org_members._owner_guard` (permission `org.owner_transfer` for owner-role invitations); `org_sso.ReadOrg/WriteOrg`,
  `org_router`. Pool size 1 per role: system lookup session closes before the tenant_session opens.
- Templates: `_Copy` has fixed intros. The invitation intro needs the org and inviter names (HTML-escaped).

### Decisions to record (specs/04 §6 and §4.1, specs/03 §2, same commit, `> Decision (2026-10-29):`)
1. **Create** `POST /v1/org/members/invitations` `{email (3-320, trimmed, must contain exactly one "@" with
   non-empty sides; else INVALID_INPUT field email), role (six roles)}` `extra="forbid"`; needs `org:write` +
   `members.manage`; role `owner` also needs `org.owner_transfer` (same FORBIDDEN message as members). Email of a
   current member of the org → `CONFLICT` field `email` "This person is already a member of the organization.".
   An existing **open** (unaccepted) invitation for the same email in the org is replaced (deleted in the same
   transaction) so re-inviting resends with a fresh token and the new role. Token = `issue_token()` (32 bytes
   base64url, stored sha256 hex), `expires_at = now + 7 days`. Response **201** `{data: <invitation>}`, invitation =
   `{id, email, role, invited_by, status: "pending"|"expired", expires_at, created_at}` (the token is never
   returned). The email `invitation` (link `{DASHBOARD_BASE_URL}/invite/{token}`, specs/25 route) is sent as a
   background task through `deliver(..., failure_event="invitation_email_failed")`. Idempotency-Key handled by the
   existing `require_scope` claim.
2. **Inviter**: a session's user; an API key's `created_by`; a key without one → `FORBIDDEN` "This API key has no
   creating user to send invitations as.".
3. **List** `GET /v1/org/members/invitations` needs `org:read` + `members.manage` (owners/admins; inviting is their
   job and specs/25 hides invite controls from others). Returns every unaccepted invitation, expired ones with
   `status: "expired"`, ordered `created_at` desc, `id` desc, unpaginated (bounded: one open invite per email,
   expired rows purged by retention).
4. **Revoke** `DELETE /v1/org/members/invitations/{invitation_id}` (uuid) needs `org:write` + `members.manage`
   (+ `org.owner_transfer` for an owner-role invite); deletes the row; unknown, other-org, or already accepted →
   `NOT_FOUND` "Invitation not found."; returns `{data:{id, revoked: true}}`.
5. **Details** `GET /internal/auth/invitations/{token}` (public, `x-public: "session-flow"`, no session): a
   malformed (`looks_like_token` false), unknown, revoked, accepted, or expired token → `TOKEN_EXPIRED` 400
   "Invitation link is invalid or has expired." field `token`. Returns `{data:{org_name, role, email, inviter_name,
   expires_at, account_exists}}` (`account_exists` tells the dashboard whether to show sign-in or create-account;
   the token holder already knows the address).
6. **Accept** `POST /internal/auth/invitations/accept` `{token (1-256), name? (1-200 trimmed), password? (1-1024)}`
   `extra="forbid"`. Order: token check as 5 → org `suspended`/`closed` → `ORG_SUSPENDED` (existing messages) →
   then by case:
   - **valid session cookie** (resolved; CSRF + Origin required like other session writes; MFA gate: the user's
     MFA on and not passed → `MFA_REQUIRED`): the session user's email must equal the invitation email
     (casefold) else `FORBIDDEN` "This invitation was sent to a different email address."; adds the membership,
     marks the email verified (first time kept), and switches the session's active org to the invited org.
   - **no valid session, a user with the email exists** → `AUTH_FAILURE` 401 "Sign in to accept this invitation."
     (nothing consumed).
   - **no user**: dashboard Origin rule as signup; `name` and `password` required (`INVALID_INPUT` field `name` /
     `password` "Name is required to create your account." / "Password is required to create your account.");
     SSO-enforced domain (OIDC on) → `SSO_REQUIRED` with `details.sso_url` (check before the policy); §4.2 policy
     (`PASSWORD_TOO_WEAK` field `password`); creates the user with `email_verified_at = now`, the membership, a login
     session (active org = invited org, `last_login_at` = now) and sets both cookies (fixation: revoke a presented
     cookie as login does).
   In every success case one `tenant_session(invitation org)` transaction consumes the invitation with a conditional
   `UPDATE … SET accepted_at = now WHERE id AND accepted_at IS NULL AND expires_at > now RETURNING` (losing a race →
   `TOKEN_EXPIRED`), creates the user if needed, and adds the membership (already a member → `CONFLICT` "You are
   already a member of this organization."; the transaction rolls back so the invite stays usable). Response 200
   `{data:{user (§4.1 session user), org_id, org_name, role}}`.
7. `InvitationLookupRepo` joins the specs/03 §2 `va_system` list: `by_token(token) -> InvitationLookup | None`
   (invitation id, org_id, org name/status, email, role, expires_at, accepted_at, inviter name, account_exists)
   because the token arrives before any org context.

### Files
**packages/va_db/src/va_db/repositories/invitations.py** (new; export from `repositories/__init__.py`):
  - `@dataclass(frozen=True, slots=True) InvitationLookup(id, org_id, org_name, org_status, email, role,
    expires_at, accepted_at, inviter_name, account_exists: bool)`; `usable(now) -> bool`.
  - `class InvitationLookupRepo(SystemRepository)`: `by_token(token: str) -> InvitationLookup | None`
    (hash with the same helper email tokens use; join orgs, inviter user, `EXISTS(users where email = inv.email)`).
  - `class InvitationRepo` (tenant; `__init__(session, org_id, *, clock)` style of MembershipRepo):
    `create(email, role, invited_by, now) -> tuple[Invitation, str]` (deletes open invites for the email first,
    returns row + raw token), `list_open() -> list[Invitation]`, `revoke(invitation_id) -> bool` (DELETE … WHERE
    org_id, id, accepted_at IS NULL RETURNING id), `consume(invitation_id, now) -> Invitation | None`.
  - Every query also filters `org_id`. Docstrings cite specs/04 §6.
**packages/va_db/src/va_db/repositories/memberships.py**: `MembershipRepo.member_by_email(email) -> MemberRecord |
  None` (reuse `_members()` select).
**packages/va_common/src/va_common/email/templates.py**: `INVITATION_DAYS: Final = 7`; `invitation(*, org_name,
  inviter_name, role, link, to) -> EmailMessage` (greeting name = "there"); extend `_Copy` so intros may carry
  `$org`/`$inviter`/`$role` substitutions (HTML-escaped in the HTML body, raw in text) without duplicating the
  layout; add `"invitation"` to `TEMPLATES`; update the email package `__all__`.
**apps/api/src/va_api/routers/org_invitations.py** + **org_invitations_schemas.py** (new, on `org_router`, side-effect
  import in `main.py`): operations `org_invitations_create` (201, `x-scope: org:write`, `x-permission:
  members.manage`), `org_invitations_list` (`org:read`, `members.manage`), `org_invitations_revoke`. Reuse
  `_owner_guard` (move to a shared spot if needed, e.g. make it public in org_members). `inviter_of(ctx, session)`
  helper for decision 2. Schemas: `InvitationCreate`, `InvitationView` (+ `invitation_view(row, now)`),
  `InvitationRevoked`; examples on all (no None).
**apps/api/src/va_api/routers/auth_invitations.py** + schemas (new, on `internal_auth_router`, side-effect import in
  `main.py`, `x-public: "session-flow"`, `include_in_schema=False` like auth.py): `invitation_details`,
  `accept_invitation`. Keep every function < radon C: split into `_lookup_usable(request, token)`,
  `_accept_signed_in(...)`, `_accept_new_user(...)`.
**apps/api/src/va_api/auth/session_auth.py**: `optional_session(request) -> ActiveSession | None` (resolve; when
  resolved run the same CSRF/Origin checks as `current_session`; share them in a private helper, no copy).
**apps/api/src/va_api/routers/auth.py**: rename `_check_sso_enforced` → `check_sso_enforced` (public, `__all__`).
**specs/04 §6/§4.1, specs/03 §2** decisions; **AGENTS.md** entry (routes/modules, InvitationLookupRepo, test helpers).
**openapi/openapi.json** + `openapi/internal.json` regenerated; `corepack pnpm gen:api` in sdks/typescript and
  apps/dashboard.

### Tests
**packages/va_db/tests/test_invitation_repo.py** (identity_db): create stores sha256 not the token, 7-day expiry
  given `now`; re-create replaces the open invite for the email; `by_token` finds org/inviter/account_exists for both
  existing and new emails, `None` for unknown; `consume` once → row, second → None, expired → None; `revoke` other
  org's id → False (RLS + filter); list ordering. `test_repositories.py` set updated via spec bullet.
**packages/va_common/tests/test_email*.py**: invitation template text/html, org/inviter names with `<script>`
  escaped in HTML only, link present, `TEMPLATES` includes it.
**apps/api/tests/test_org_invitations_integration.py**: owner/admin create (201, outbox email link token, row
  hashed); developer/analyst/billing/viewer → 403; admin invites owner → 403 `org.owner_transfer`; owner invites
  owner → 201; existing member email (case-different) → 409 field email; re-invite replaces (old token
  TOKEN_EXPIRED on details); invalid email / role / extra field → 400; `*` API key with `created_by` → 201
  `invited_by` = creator; key without creator → 403; `org:read` key → 403 on create; list (status pending vs expired
  after clock +7 d, accepted excluded, viewer → 403); revoke (200, then details → TOKEN_EXPIRED; other org id /
  accepted → 404; admin revoking an owner invite → 403); CSRF missing → 403.
**apps/api/tests/test_auth_invitations_integration.py**: details (fields, account_exists both ways, malformed /
  unknown / expired (+7 d + 1 s) / accepted → 400 TOKEN_EXPIRED field token); accept new user (cookies set,
  `/internal/auth/session` shows the org with the invited role, email verified, password policy failure keeps the
  invite usable, missing name/password → 400 field, SSO-enforced domain → SSO_REQUIRED with sso_url using
  `configure_sso`/`session_app(oidc_enabled=True)` helpers); existing user without session → 401 AUTH_FAILURE "Sign
  in to accept this invitation."; existing user signed in → 200, active org switched, member listed; signed in as a
  different email → 403; already a member → 409 and invite still usable; reuse after accept → 400; concurrent
  double accept (gather) → exactly one success; suspended org → ORG_SUSPENDED; MFA-enabled user whose session has
  not passed MFA → 401 MFA_REQUIRED; foreign Origin → 403. Add shared helpers to `support/session_helpers.py`
  (`invite(client, email, role) -> token` using `link_token(container.outbox, email, "invitation")`).
**apps/api/tests/test_openapi.py**: three public operation ids + x-scope/x-permission; `INTERNAL_PATHS` gets the two
  internal paths.

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` + `uv run python scripts/check_openapi_drift.py`;
`corepack pnpm gen:api` (sdks/typescript, apps/dashboard) + `make test-ts`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and
`uvx radon cc -s -a ... | tail -1`; pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files`. build.done as a PLAIN STRING with numeric complexity:
`ralph emit "build.done" "P02-T08c tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1: lookup (system) → close → tenant_session; `optional_session` / MFA user lookup must finish before the
  tenant transaction opens; `remember`/`set_active_org` only after commit.
- Creating a `users` row inside `tenant_session` works (signup does it) — reuse that path.
- citext: compare emails in SQL; in Python use `casefold()` for the session-email check.
- jscpd: the two new routers resemble org_members/auth_password; share the guard, `error_of`, link helpers.
- `/members/invitations` vs `/members/{user_id}`: `{user_id}` is uuid-typed; add a test that `GET
  /v1/org/members/invitations` is not swallowed and `DELETE /v1/org/members/invitations/<uuid>` hits revoke.

## 2026-10-29 Builder: P02-T08c build notes
- Built per plan: `va_db.repositories.invitations` (`InvitationRepo` create/list_open/get_open/revoke/consume,
  system `InvitationLookupRepo.by_token` with inviter name→email fallback); `MembershipRepo.member_by_email`;
  template `invitation` (`_Copy.render(values=)` substitutes `$org/$inviter/$role` raw in text, escaped in HTML;
  fixed subject, no user data in headers); `va_api.auth.invitations` (link + send); routers `org_invitations.py`
  (+schemas) and `auth_invitations.py` (+schemas); `session_auth.optional_session` (current_session now wraps it)
  and public `user_mfa_enabled`; `auth.check_sso_enforced` public; `org_members.owner_guard` public.
- Deviation from plan: no `revoke_presented` in the new-user accept path — that path is only reached when
  `optional_session` found no valid session, so a presented cookie is already invalid (nothing to revoke).
- Inviter: `principal_type` user or platform_admin → principal; api_key → `api_keys.created_by`.
- specs/04 §4.1 and §6 decisions, specs/03 §2 bullet + decision; AGENTS.md entry; openapi + internal.json and both
  TS schema.d.ts regenerated. `idp`/`sso` fixtures moved to apps/api/tests/conftest.py (jscpd vs sso_enforce test).
- Tests: va_db test_invitation_repo (7), test_email invitation template, apps/api test_org_invitations_integration
  (24), test_auth_invitations_integration (20), test_openapi operation/internal path asserts.
- Coverage of routers/org_invitations.py reports 69% with "missed" lines inside `async with tenant_session` bodies
  that passing tests exercise (same greenlet tracing artifact as org_members.py / org.py).
- Gates: fmt/lint/typecheck/placeholders pass; make test run 1: 2 pytest-timeout errors, run 2: 114 Docker
  exec_create timeouts in va_db setup at load ~15 (none in new code; va_db alone 765 passed), run 3 green: 2766
  passed, coverage 98.58%; make test-ts pass (52 + 48); openapi drift current; check_spec_refs 0; jscpd 0 clones;
  radon no C+, avg 1.96; pip-audit + pnpm audit clean; gitleaks pass.

## Review P02-T08c (2026-10-29): approved
- Re-ran: make lint typecheck placeholders (pass, 0 placeholder findings); make test 2766 passed, coverage 98.58%;
  check_openapi_drift current (openapi.json + internal.json); check_spec_refs 0.
- Diff checked against specs/04 §6, §4.1 decisions and specs/03 §2: org_id filter + tenant_session on every
  InvitationRepo query; system lookup cites specs/03 §2; token hashed (sha256), conditional UPDATE consume; CSRF/Origin
  via optional_session on signed-in accept, check_dashboard_origin on new-user accept; owner_guard on create/revoke;
  failure paths covered by tests. P02-T08 parent ticked (T08a/b/c all done).

## 2026-10-29 Planner: P02-T08c approved → P02-T09 split into T09a/T09b
- T09 mixes two independent concerns: sub-account management endpoints (legacy `/v1/orgs*`, a cross-org credit
  move) and the parent-access path through `get_principal` + `switch-org`. Split in plan/phase-02.md.
- Credit transfer endpoint itself is P15-T07; T09a builds only the reusable `CreditTransferRepo` it needs for
  `initial_credits`, so P15-T07 adds the route on top.

### T09b design carried forward (do not build in T09a)
- `get_principal`: after the principal is built, a non-blank `X-VA-Org-Id` (uuid else `INVALID_INPUT` field
  `X-VA-Org-Id`) equal to ctx.org_id is a no-op; else the caller must be an API key with `org:write`/`*` or a
  session with role owner/admin (`FORBIDDEN` otherwise); the target must be a direct child
  (`parent_org_id == ctx.org_id`, else `FORBIDDEN` "Organization is not a sub-account of the caller."); the new ctx
  = `dataclasses.replace(ctx, org_id=child, acting_parent_org_id=parent, plan_id=child.plan_id)`; child
  status gates (closed → ORG_SUSPENDED, suspended → writes refused); rate bucket stays the principal's.
- `switch-org {org_id}`: membership, or owner/admin membership of the target's parent; session row stores the
  child as `active_org_id`; `session_principal` must then resolve a child active org via the parent membership
  (role preserved, `acting_parent_org_id` set). `GET /internal/auth/session` memberships gain nothing; children
  come from `GET /v1/orgs`.

## Plan P02-T09a — Sub-account management (specs/04 §8.2, §8.4, §3.2; specs/24 §5, §9; specs/03 §2, §3)

### Facts found while planning
- `orgs` has `parent_org_id`, `kyc_level` (0-3), `credits_balance_micros`, `credit_limit_micros`; no RLS (global).
  `credit_ledger` and `credit_holds` are RLS tenant tables (model `CreditLedger`, `CreditHold`, entry types incl.
  `transfer_in`/`transfer_out`, reference_type `sub_account`, `UNIQUE(org_id, idempotency_key)`).
- `OrgProvisioner.provision_in(session, org_id, name=, owner=, plan_id=, parent_org_id=)` already supports a parent;
  it has no `kyc_level` arg. Its docstring says the session must be `tenant_session(org_id)`.
- `tenant_session` sets `app.org_id` with `set_config(..., true)` (transaction-local), so a transaction can switch
  the RLS org mid-way by calling set_config again: this is how one transaction writes both orgs' ledger rows
  without `va_system`.
- No legacy resource router exists yet (only health). `/v1/orgs*` is classified legacy in `va_api.envelopes`.
  Legacy success body: `{"status":"success", ...fields}`; legacy errors handled by `va_api.errors`.
- `ApiKeyHasher` is held only inside the authenticator; the container has no hasher field. `generate_api_key`,
  `api_key_prefix`, `hasher.hash()`, `hasher.current_version` exist in `va_common.auth`.
- `invalidate_api_key_cache(redis, prefix)` exists in `va_api.auth.api_key_auth`.
- `org_invitations._inviter_of` resolves a user from a session or a key's `created_by`.
- `va_common.money.dollars_to_micros` / `micros_to_dollars` exist. `INSUFFICIENT_CREDITS` is 402 in specs/31.
- Tests: `AuthContainer.store` (in-memory `KeyStore`) authenticates keys; a key minted into the DB would not
  authenticate unless the test container also looks up the DB.

### Decisions to record (specs/04 §8.2, same commit, `> Decision (2026-10-29):`; specs/03 §2 note for the repo)
1. **Create** `POST /v1/orgs/create` (legacy, `org:write` + `subaccounts.manage`, operation `orgs_create`): body
   `{org_display_name (1-200, trimmed; empty → INVALID_INPUT field org_display_name), org_plan? ("start"|"build"|
   "scale"), initial_credits? (number ≥ 0, ≤ 1 000 000, at most 6 decimals)}`; unknown fields are ignored (Bland
   compatibility, unlike envelope routes). The caller's org having a `parent_org_id` → `FORBIDDEN` "Sub-accounts
   cannot create sub-accounts." (max depth 1). The caller org must exist (`NOT_FOUND` "Organization not found.").
   `plan_id = org_plan or parent.plan_id`, `kyc_level = parent.kyc_level`. One `tenant_session(child_id)`
   transaction: provisioning (no owner, no members), the child key, then the credit transfer when
   `initial_credits > 0`; any failure (e.g. `INSUFFICIENT_CREDITS`) leaves nothing. The child key: name
   "Sub-account key", scopes `["*"]`, environment = the caller's `ctx.environment`, `created_by` = session user or
   the calling key's `created_by` (null allowed), current pepper version. Response **200** `{"status":"success",
   org_id, org_display_name, org_plan, kyc_level, api_key, credits, created_at}` (`credits` = child balance in
   dollars as a JSON number). Idempotency via the existing `require_scope` claim (replay returns the same body,
   including the key — the stored response is how Idempotency-Key behaves for every create; note it in the decision).
2. **Credit transfer primitive** `CreditTransferRepo(session).transfer(from_org_id, to_org_id, amount_micros,
   idempotency_key, description) -> CreditTransfer(out_entry, in_entry)`: inside the caller's transaction (a
   `tenant_session` of either org); locks both org rows `FOR UPDATE` ordered by id; parent available =
   `credits_balance_micros − Σ credit_holds.amount_micros WHERE status='held'` (no `credit_limit`: borrowed credit
   cannot be given away); `amount > available` → `INSUFFICIENT_CREDITS` 402 "Insufficient credits for this
   transfer."; amount ≤ 0 → `ValueError` (programming error; the route validates); writes `transfer_out`
   (−amount, parent, `reference_type='sub_account'`, `reference_id=str(child)`) and `transfer_in` (+amount,
   child, `reference_id=str(parent)`), both with the same `idempotency_key` (`xfer:{uuid}` for sub-account
   creation; P15-T07 passes `xfer:{Idempotency-Key}`), and updates both `credits_balance_micros`; each ledger
   row is written with `app.org_id` switched to its org via `va_db.session.use_org(session, org_id)` (an async
   context manager that `set_config`s the org transaction-locally and restores the previous value on exit),
   so RLS still checks every row. Not a `SystemRepository` (runs as `va_app`).
3. **List** `GET /v1/orgs` (`org:read` + `subaccounts.manage`, `orgs_list`): direct children of `ctx.org_id`
   ordered `created_at`, `id`, unpaginated (Bland shape): `{"status":"success","orgs":[{org_id,
   org_display_name, org_plan, kyc_level, credits, status, created_at}]}`.
4. **Get** `GET /v1/orgs/{org_id}` (`org:read` + `subaccounts.manage`, `orgs_get`, uuid path): `{"status":
   "success", ...the list item}`; an org that is not a direct child of the caller (sibling, parent, self, other
   tenant, unknown) → `NOT_FOUND` "Sub-account not found.".
5. **Suspend / reactivate** `POST /v1/orgs/{org_id}/suspend|reactivate` (`org:write` + `subaccounts.manage`,
   `orgs_suspend`, `orgs_reactivate`): non-child → `NOT_FOUND`; a `closed` child → `CONFLICT` "A closed
   sub-account cannot be changed."; otherwise sets `status` (`suspended` / `active`; repeating is a no-op) and
   returns the item. After the commit the child's `apikey:{prefix}` cache entries are deleted (Redis errors
   logged as `api_key_cache_unavailable`, never raised) so a suspension applies to keys at once; sessions read
   the org status per request already. Audit rows (`subaccount.*`) arrive with P02-T11.

### Files
**packages/va_db/src/va_db/session.py**: `use_org(session, org_id) -> AsyncContextManager[None]` (reads
  `current_setting('app.org_id', true)`, sets the new one with `set_config(..., true)`, restores the previous
  value — `''` when unset — in `finally`); export in `__all__`.
**packages/va_db/src/va_db/repositories/credits.py** (new; export from `repositories/__init__.py`):
  `@dataclass(frozen=True, slots=True) CreditTransfer(out_entry: CreditLedger, in_entry: CreditLedger)`,
  `INSUFFICIENT_TRANSFER_MESSAGE`, `class CreditTransferRepo` per decision 2 (`_lock(ids)` ordered select
  `FOR UPDATE`, `_held(org_id)` sum under `use_org`, `_entry(...)`). Uses `new_id()` for ledger ids.
**packages/va_db/src/va_db/repositories/orgs.py**: `OrgRepo.children(parent_id) -> list[Org]`,
  `OrgRepo.child(parent_id, child_id) -> Org | None`, `OrgRepo.set_status(org_id, status) -> Org | None`.
**packages/va_db/src/va_db/repositories/api_keys.py**: `ApiKeyRepo.add(*, name, prefix, secret_hash,
  pepper_version, scopes, environment, created_by) -> ApiKey` (flush + refresh), `ApiKeyRepo.prefixes() ->
  list[str]`.
**packages/va_db/src/va_db/provisioning.py**: `kyc_level: int = 0` keyword on `provision` / `provision_in`, set
  on the `Org`.
**apps/api/src/va_api/auth/api_keys.py** (new; P02-T10 reuses): `@dataclass(frozen=True) MintedKey(plaintext
  (repr=False), prefix, secret_hash, pepper_version)`; `mint_api_key(hasher, environment) -> MintedKey`;
  `async def creating_user_id(ctx, session) -> uuid.UUID | None` (session user or the key's `created_by`) — make
  `org_invitations._inviter_of` call it (no duplicate lookup code).
**apps/api/src/va_api/container.py**: `api_key_hasher: ApiKeyHasher` field filled from `settings.api_key_hasher()`
  (the same instance the authenticator gets); **session_auth.py** `api_key_hasher_of(request)` + `redis_of(request)`
  if no accessor exists yet (check `va_api.rate_limit`/idempotency for an existing one first).
**apps/api/src/va_api/routers/orgs.py** + **orgs_schemas.py** (new): `orgs_router = legacy_router(prefix="/v1/orgs",
  tags=["Org"])`, five operations with `openapi_extra={"x-scope": ..., "x-permission": "subaccounts.manage"}`
  (match how org_members declares them), `Depends(require_permission("subaccounts.manage"))`. Schemas:
  `SubAccountCreate` (extra="ignore", `initial_credits: Decimal | None` constrained, `org_plan: Literal[...]`),
  `SubAccountItem`, `SubAccountCreated`, `SubAccountList`, `SubAccountView` — all `status: Literal["success"]`
  bodies with examples (no None values). Keep functions < radon C. Add to `ROUTERS` in main.py.
**specs/04 §8.2** decisions 1, 3-5; **specs/04 §8.4** decision 2 (or specs/24 §9 cross-ref); **specs/03 §2** one line
  noting `use_org` for cross-org transactions that stay `va_app`. **AGENTS.md** entry (routes, `use_org`,
  `CreditTransferRepo`, test helpers).
**openapi/openapi.json** regenerated; `corepack pnpm gen:api` in sdks/typescript and apps/dashboard.

### Tests
**packages/va_db/tests/test_credit_transfer.py** (identity_db): transfer writes both rows with the shared key,
  `balance_after` and both org balances correct, ledger sum == balance per org; amount > balance − held →
  `INSUFFICIENT_CREDITS` and nothing written; a `held` hold reduces available, `captured`/`released` do not;
  `credit_limit` does not count; amount 0 → ValueError; duplicate idempotency key → CONFLICT/IntegrityError surfaces
  (document which); concurrent two transfers from one parent whose sum exceeds balance → exactly one succeeds
  (gather on two sessions — pool size 1 per role: use the identity_db helpers that build a second Database, or
  skip concurrency here and cover it in the API test with sequential calls; Builder picks what the pool allows and
  notes it). `use_org` restores the previous `app.org_id` (also after an exception).
**packages/va_db/tests/test_org_repo*.py**: `children` ordering/only direct children, `child` None for non-child,
  `set_status`; `ApiKeyRepo.add/prefixes` org-filtered; provisioner `kyc_level`.
**apps/api/tests/test_orgs_integration.py**: owner/admin session and `*` key create (200 body shape, child
  `parent_org_id`, plan default = parent, `org_plan` override, kyc_level copied, `api_key` authenticates and
  `GET /v1/me` with it returns the child org, key row hashed with prefix, `created_by`); initial_credits 10.5 with
  parent balance 50 → child credits 10.5, parent 39.5, two ledger rows; insufficient → 402 INSUFFICIENT_CREDITS and
  no child org row / no key; a held hold counted; validation (empty name, bad plan, negative / too many decimals
  credits → 400 legacy error body with field), unknown field ignored; child key creating a sub-account → 403;
  developer/analyst/billing/viewer sessions → 403; `org:read` key → 403 on create; list (two children ordered,
  other parent's children absent, child's own list empty); get (child ok; sibling/parent/self/unknown → 404 legacy
  body); suspend → child key POST → ORG_SUSPENDED immediately (cache invalidated), GET still works; reactivate →
  writes work; closed child → 409; non-child suspend → 404; Idempotency-Key replay of create returns the same
  org_id and creates one org.
  Test container: give `SessionContainer` an authenticator whose lookup tries the KeyStore then
  `db_lookup(self.database)` and expose `api_key_hasher` = `store.hasher` on `AuthContainer`, so DB-minted keys
  authenticate. Helper to set a parent balance: write a `grant` ledger row + balance via a support function
  (`fund(container, org_id, dollars)`), shared in session_helpers.
**apps/api/tests/test_openapi.py**: the five operation ids with `x-envelope: legacy`, x-scope, x-permission.
**apps/api/tests/test_container.py**: container exposes the hasher used by the authenticator.

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` + `uv run python scripts/check_openapi_drift.py`;
`corepack pnpm gen:api` (sdks/typescript, apps/dashboard) + `make test-ts`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and
`uvx radon cc -s -a ... | tail -1`; pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files`. build.done as a PLAIN STRING with numeric complexity:
`ralph emit "build.done" "P02-T09a tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: the create route reads the parent org (global session) first, closes it, then opens the
  single `tenant_session(child)`; `use_org` keeps everything on one connection. Never open a system session inside.
- RLS on `credit_ledger`/`credit_holds`/`org_data_keys`/`api_keys` with `app.org_id` switching: verify with the
  RLS-on test database (no superuser shortcut); `use_org` must restore the child id before provisioning writes
  continue, so do the transfer last.
- Legacy validation errors: confirm the 400 body shape for a legacy route via `va_api.errors` (first legacy
  resource route); `Decimal` with `decimal_places=6` and `extra="ignore"` must still render examples.
- The minted plaintext must never be logged; `MintedKey.plaintext` repr=False; gitleaks may flag example keys in
  schema examples — use an obviously fake example and check gitleaks.
- Response `credits` float from `micros_to_dollars` Decimal.

## 2026-10-29 Builder: P02-T09a build notes
- Built per plan. Deviation (recorded in specs/04 §8.2 decision): `GET /v1/orgs/{org_id}`, suspend and reactivate
  return `{"status":"success","org":{...item}}` (nested) because the item's own `status` (org status) collides with
  the legacy `status: "success"`. `initial_credits` is a `float` field (OpenAPI `number`) with a ≤6-decimal validator,
  not `Decimal` (which renders as number|string).
- `use_org` restores `app.org_id` on exit; on an exception it restores best-effort (suppresses SQLAlchemyError since an
  aborted transaction rolls back anyway). `CreditTransferRepo` refreshes each ledger row inside its own `use_org`
  (refresh after leaving would SELECT under the other org and miss the row). Duplicate idempotency key → `CONFLICT`
  via `conflict_savepoint` (documented in specs/04 §8.4 decision).
- Concurrency: covered sequentially (two transfers whose sum exceeds the balance → second INSUFFICIENT_CREDITS); the
  pool has one connection per role so no gather test.
- `set_org`/`stored_org` moved from test_org_integration.py into session_helpers (jscpd).
- Incident: a `ruff check --fix --select RUF022,RUF100 .` stripped valid noqa comments repo-wide (RUF100 treats
  noqa of unselected rules as unused). Restored: unrelated files via `git checkout --`, my files by re-adding the
  HEAD noqa lines; `git diff | grep '^-.*noqa'` = 0.
- Gates: fmt/lint/typecheck pass; placeholders 0 findings; make test 2808 passed, coverage 98.45% (orgs.py 81%
  line report is the async-ASGI coverage artifact also seen on org_invitations.py 68%); make openapi + drift current;
  gen:api in sdks/typescript + apps/dashboard; make test-ts pass (22 + 52 + 48); check_spec_refs 0; jscpd 0 clones;
  radon no C+, avg 1.955; pip-audit + pnpm audit clean; gitleaks pass.

## Review P02-T09a (2026-10-29): rejected (1 finding)
Gates re-run by Reviewer: make lint pass, make typecheck pass, make placeholders 0 findings, make test 2808 passed
(coverage 98.45%). Code otherwise matches specs/04 §8.2/§8.4 (access, depth 1, non-child NOT_FOUND, lock order,
held holds, use_org restore, cache invalidation, legacy bodies). One spec violation:

1. **Plaintext sub-account API key is persisted and re-retrievable via Idempotency-Key replay.**
   `apps/api/src/va_api/routers/orgs.py` `orgs_create` returns `api_key=minted.plaintext`; with an
   `Idempotency-Key` header the capture middleware (`va_api/idempotency.py` `_settle` → `complete(..., body=...)`)
   stores the whole JSON body in `idempotency_keys.response_body` (plain `jsonb`, kept 24 h) and replays it.
   The test `apps/api/tests/test_orgs_integration.py:393` exercises exactly this path. This violates
   specs/04 §2.1 line 48 "Plaintext is returned exactly once at creation. It is never logged, stored, or
   retrievable." and the phase constraint "Never store raw keys or tokens". The new decision text in specs/04
   §8.2 ("a replay returns the stored body, plaintext key included, like every create") contradicts the normative
   line silently instead of resolving it. Required: the plaintext must never reach `idempotency_keys` (or any
   table) unencrypted. Acceptable fixes (Planner picks, records a `> Decision` reconciling §2.1 and §12, and the
   same mechanism must serve P02-T10 `/v1/api_keys` create/roll):
   a) a route-level marker (e.g. a `request.state` flag or `x-idempotency-secret-fields: ["api_key"]`) so the
      middleware stores the body with secret fields removed, and a replay returns the stored body with
      `api_key` withheld (schema/doc says so; the caller rolls the key if it lost the first response); or
   b) envelope-encrypt the stored response body for such routes (`Envelope.encrypt(org_id, ..., aad=
      "idempotency:{org_id}:{key}")`, store `{"sealed": "<b64>"}`), decrypt on replay — and amend §2.1 to say
      the plaintext is stored only envelope-encrypted for the 24 h idempotency window.
   Add tests: after an idempotent create, `SELECT response_body::text FROM idempotency_keys` does not contain
   the plaintext key; replay behaviour per the chosen option; key from the first response still authenticates.

## 2026-10-29 Builder: P02-T09a fix for review finding 1
- Chose option (a): `va_api.idempotency.withhold_on_replay(request, *dotted_paths)` records paths in the ASGI
  scope (`WITHHELD_SCOPE_KEY`); `_Capture._settle` runs `withheld_body` on the parsed copy before `complete`, so
  `idempotency_keys.response_body` holds `null` there and replays return `null`; the live response bytes are
  untouched. Dotted paths (`"data.key"`) cover envelope bodies for P02-T10 `/v1/api_keys` create/roll.
- `orgs_create` calls `withhold_on_replay(request, "api_key")`; `SubAccountCreated.api_key` is `str | None` with
  the replay behaviour documented (OpenAPI + TS schemas regenerated).
- Decisions: specs/04 §2.1 (never stored includes idempotency rows; mechanism), §8.2 create text amended, §12
  stored-bullet amended. AGENTS.md updated.
- Tests: test_idempotency `/secret` route (both families: top-level + nested withheld, absent paths ignored,
  stored row free of the secret); test_orgs_integration idempotent create now uses `DbIdempotencyStore` via
  monkeypatch, asserts `SELECT response_body::text FROM idempotency_keys` lacks the key/prefix, replay `api_key`
  null, first key authenticates `/v1/me` for the child. Verified the org test fails with the route call removed.
- Gates: fmt/lint/typecheck pass; placeholders 0; make test 2811 passed, coverage 98.45%; openapi drift current; gen:api both; test-ts 22+52+48; spec refs 0; jscpd 0; radon no C+, avg 1.96; pip-audit + pnpm audit clean; gitleaks pass.

## Review P02-T09a re-review (2026-10-29): approved
Finding 1 resolved: `withhold_on_replay(request, "api_key")` + `_Capture._settle` → `withheld_body` nulls the field
before `complete`; live response untouched; specs/04 §2.1/§8.2/§12 decisions reconcile "never stored". Tests cover
stored row free of the key (DbIdempotencyStore), replay null, first key authenticates. Reviewer gates: lint,
typecheck, placeholders 0, make test 2811 passed (98%), openapi drift current, spec refs 0, jscpd 0, radon no C+
(avg 1.96), gitleaks pass.

## 2026-10-29 Planner: P02-T09a approved → next P02-T09b
P02-T09a committed (4fe50fe) and ticked. P02-T09 parent stays unticked until T09b is approved (Reviewer ticks both).

## Plan P02-T09b — Parent access to sub-accounts: `X-VA-Org-Id` and `switch-org` (specs/04 §1, §4.1, §4.3, §8.3, §14)

### Facts found while planning
- `get_principal` (va_api/auth/dependencies.py) authenticates (API key or session), stores `request.state.ctx`, binds
  `org_id`, then `enforce(... plan_rate_limits(ctx.plan_id, rate_per_sec))`. Rate keys are per principal
  (`rl:key:{id}` / `rl:user:{id}`), so the bucket identity is unaffected by a switched org.
- `RequestContext.acting_parent_org_id` exists and is always `None` today (api_key_auth.py:166, session_auth.py:305).
- `session_auth.active_membership` resolves `sessions.active_org_id` only through the user's own memberships
  (`MembershipLookupRepo.for_user_in_org`), else falls back to the oldest membership and persists it. `GET
  /internal/auth/session` (routers/auth.py `get_session`) uses `pick_active` over own memberships — a child active org
  would be silently reset. Both must learn the parent path.
- `MembershipRecord` (va_db/repositories/memberships.py) carries org fields + role + mfa/rate; `_joined(user_id)`
  builds the select. `MFA_REQUIRED_KEY`, `rate_per_sec_override` (repositories/api_keys.py) exist.
- `OrgRepo.child(parent_id, child_id)` exists (orgs table has no RLS; use `global_session`).
- Code that assumes the principal belongs to `ctx.org_id`: `routers/org.py:_me_api_key` (ApiKeyRepo(session,
  ctx.org_id).get(ctx.principal_id)), `auth/api_keys.py:creating_user_id` (same lookup), `routers/org_members.py`
  transfer-ownership (`ctx.principal_id` as the current owner).
- `claim_idempotency` keys rows by `ctx.org_id` → acting requests' idempotency rows live in the child org (fine,
  RLS-consistent; record it).
- CORS already allows `X-VA-Org-Id` (main.py:52).

### Decisions to record (same commit, `> Decision (2026-10-29):`)
specs/04 §8.3:
1. **Header.** `X-VA-Org-Id` is read in `get_principal` after authentication, before the rate limit. Absent or blank
   → ignored. Not a UUID → `INVALID_INPUT` field `X-VA-Org-Id` "X-VA-Org-Id must be an organization id.". Equal to
   `ctx.org_id` → no-op (context unchanged, `acting_parent_org_id` stays as it was).
2. **Who may.** API key holding `org:write` (or `*`); session principal whose role (in the org the session acts in)
   is `owner` or `admin`. Otherwise `FORBIDDEN` "Acting on a sub-account requires the org:write scope or the owner or
   admin role." (checked before the org lookup, so it leaks nothing).
3. **Which org.** The target must be a direct child of `ctx.org_id` (`OrgRepo.child` in a `global_session`); anything
   else — sibling, the parent, the caller's own parent, other tenant, unknown, a grandchild — `FORBIDDEN`
   "Organization is not a sub-account of the caller.". A context that already acts in a child (session switched via
   `switch-org`) therefore cannot hop to a sibling or back to the parent through the header (depth 1).
4. **Gates on the child.** `closed` → `ORG_SUSPENDED` "Organization is closed." (every method); `suspended` → writes
   `ORG_SUSPENDED` (API-key message `ORG_SUSPENDED_MESSAGE`), reads allowed. Session principal: the child's
   `settings.mfa_required` true and the session not MFA-passed → `MFA_REQUIRED` (the session gate's message), so the
   header cannot bypass a child's MFA policy.
5. **New context.** `dataclasses.replace(ctx, org_id=child.id, plan_id=child.plan_id, acting_parent_org_id=
   ctx.org_id)`; scopes, role, principal id/type, environment unchanged. Rate limiting keeps the principal's bucket
   key; its size uses the child's plan and the child's `limits.rate_per_sec` override (every bucket follows
   `ctx.plan_id`). Logs bind `org_id` = child and `acting_parent_org_id` = parent. Idempotency rows are claimed in
   the child org. Audit (P02-T11) records both orgs.
6. **Principal's own rows.** `RequestContext.principal_org_id` (property: `acting_parent_org_id or org_id`) is the
   org owning the API key / membership; `/v1/me` and `creating_user_id` look the key up there. `GET /v1/me` gains
   `acting_parent_org_id` (uuid | null) and `org` is the child. `POST /v1/org/members/transfer-ownership` (whatever
   the route path is in org_members.py) with `acting_parent_org_id` set → `FORBIDDEN` "Ownership of a sub-account
   cannot be transferred by a parent-org principal." (the caller is not the child's owner member).
specs/04 §4.1 (`switch-org` row):
7. **`POST /internal/auth/switch-org`** `{org_id: uuid}` (unknown fields `INVALID_INPUT`, `x-public: session-flow`,
   `operation_id="auth_switch_org"`): requires a valid session (CSRF + dashboard Origin, as other session writes) and
   passes the MFA gate of the *current* active org (`gated_membership`). Allowed targets: an org the user is a member
   of, or a direct child of an org where the user is `owner`/`admin`. Otherwise `FORBIDDEN` "You do not have access
   to this organization." (unknown ids included). Org status is not checked (the dashboard shows suspended/closed
   orgs; the principal gates apply on later requests). Persists `sessions.active_org_id` via
   `SessionManager.set_active_org` (cache rewritten) and returns 200 with the same body as `GET /internal/auth/session`.
8. **Session resolution** (amends the §4.3 session-mechanics "Active org"): when `sessions.active_org_id` is not
   one of the user's memberships, it resolves through `MembershipLookupRepo.via_parent(user_id, org_id)` (the user is
   owner/admin of the org's parent): the principal acts in the child with the parent membership's role and
   `acting_parent_org_id` = parent; MFA is required when the child **or** the parent requires it (or the user enabled
   it); `rate_per_sec` is the child's override. When neither path resolves (role lowered, membership removed, child
   re-parented) it falls back to the oldest membership as today. `GET /internal/auth/session` resolves the active org
   the same way and gains `acting_parent_org_id` (uuid | null); `memberships[]` is unchanged (children come from
   `GET /v1/orgs`).

### Files
**packages/va_db/src/va_db/repositories/memberships.py**
- `MembershipRecord.acting_parent_org_id: uuid.UUID | None = None` (last field, docstring line).
- `PARENT_ACCESS_ROLES: Final = frozenset({"owner", "admin"})` (export).
- `MembershipLookupRepo.via_parent(user_id, org_id) -> MembershipRecord | None`: `child = aliased(Org)`;
  `select(Membership, child, Org, User.mfa_enabled).join(Org, Org.id == Membership.org_id).join(child,
  child.parent_org_id == Membership.org_id).join(User, ...).where(Membership.user_id == user_id, child.id == org_id,
  Membership.role.in_(PARENT_ACCESS_ROLES))`; record = `_record(membership, child, mfa)` with
  `mfa_required = child flag or parent flag`, `acting_parent_org_id = parent.id` (use `dataclasses.replace` on the
  `_record` result, or give `_record` keyword args — keep it one builder, no duplicate field list).
**apps/api/src/va_api/auth/context.py**: `principal_org_id` property.
**apps/api/src/va_api/auth/sub_accounts.py** (new): module docstring citing specs/04 §8.3;
  `ORG_HEADER: Final = "x-va-org-id"`, message constants (`ACT_FORBIDDEN_MESSAGE`, `NOT_A_CHILD_MESSAGE`,
  `INVALID_ORG_HEADER_MESSAGE`); `async def act_on_sub_account(request, ctx, rate_per_sec) -> tuple[RequestContext,
  int | None]` implementing decisions 1–5 (returns inputs unchanged when no switch); small private helpers to stay
  below radon C (`_target(request) -> uuid | None`, `_may_act(ctx) -> bool`, `_check_child(child, request)`).
  Session MFA check reads `request.state.session` (set by `current_session`) — `getattr(request.state, "session",
  None)`; only for `principal_type == "user"`. Reuse `ORG_CLOSED_MESSAGE`, `MFA_REQUIRED_MESSAGE` from
  session_auth and `ORG_SUSPENDED_MESSAGE`, `READ_METHODS` from api_key_auth — factor `_check_org_status` in
  session_auth into a public `check_org_status(status: OrgStatus, method)` used by both (no copy).
**apps/api/src/va_api/auth/dependencies.py**: `get_principal` calls `act_on_sub_account` after authentication; binds
  `acting_parent_org_id` (str or omitted) to structlog contextvars; docstring updated.
**apps/api/src/va_api/auth/session_auth.py**: `active_membership` tries `via_parent` (same system session) when
  `for_user_in_org` misses; `session_principal` sets `acting_parent_org_id=record.acting_parent_org_id`;
  `check_org_status` public; new `async def resolve_switch_target(request, user_id, org_id) -> MembershipRecord |
  None` (own membership or via_parent, one system session) used by switch-org and by active_membership (no dup).
**apps/api/src/va_api/routers/auth.py**: extract `_session_info(request, response, active) -> SessionInfo` from
  `get_session` (resolves via `active_membership`, not `pick_active` — keep the "persist fallback" behaviour) and
  add `switch_org` (`POST /switch-org`, `SwitchOrgRequest`). If auth.py nears radon/size limits put switch-org in
  `routers/auth_switch_org.py` on `internal_auth_router` with a side-effect import in main.py
  (mem-1793267153-d5f5).
**apps/api/src/va_api/routers/auth_schemas.py**: `SwitchOrgRequest(org_id: uuid.UUID, extra="forbid")`;
  `SessionInfo.acting_parent_org_id: uuid.UUID | None` (+ example value `None` is dropped by FastAPI → leave it out
  of the example or use a uuid; examples must not contain None, mem-1793265656-71f0).
**apps/api/src/va_api/routers/org.py + org_schemas.py**: `MeView.acting_parent_org_id`; `_me_api_key` uses
  `ctx.principal_org_id`. **apps/api/src/va_api/auth/api_keys.py** `creating_user_id` same.
**apps/api/src/va_api/routers/org_members.py**: transfer-ownership refusal (decision 6).
**specs/04** §8.3 decisions 1–6, §4.1 decision 7, §4.3 decision 8 (short "amends" note under the session-mechanics
  decision). **AGENTS.md**: X-VA-Org-Id, `principal_org_id`, switch-org, test helpers.
**openapi/openapi.json** regenerated (`MeView`); internal OpenAPI doc too if the repo keeps one (P02-T04d added an
  internal document — regenerate/drift-check both); `corepack pnpm gen:api` in sdks/typescript and apps/dashboard.

### Tests
**packages/va_db/tests/test_membership_lookup_integration.py**: `via_parent` returns the child record with parent
  role, child plan/status/name, `acting_parent_org_id`; `mfa_required` true when only parent or only child requires
  it; None for developer/analyst/billing/viewer parent roles, for a non-child org, for a grandchild-shaped id (child of
  a child — build via direct insert if provisioner forbids), for the user's own org, for unknown ids.
**apps/api/tests/test_sub_account_access_integration.py** (new; reuse conftest `identity/container/client`,
  session_helpers `signed_in`, `fund`, `set_org`, `add_member`, `error_of`; create children through
  `POST /v1/orgs/create` so the child key exists):
  - API key `*` of parent + header child: `GET /v1/me` → org = child, `acting_parent_org_id` = parent, `api_key.id` =
    parent key; `GET /v1/org` → child; `PATCH /v1/org` renames the child only (parent name unchanged in DB) — proves
    the tenant session uses the child id.
  - parent `org:write` key allowed; `org:read`-only key → 403 FORBIDDEN act message (no org lookup).
  - header = own org id → unchanged ctx (`acting_parent_org_id` null); blank header ignored; malformed → 400
    INVALID_INPUT field `X-VA-Org-Id`.
  - sibling child key with header = other child → 403 not-a-child; child key with header = parent → 403; other
    tenant's org / unknown uuid → 403 not-a-child.
  - suspended child (via suspend route): parent header GET works, PATCH → ORG_SUSPENDED; closed child (set_org) →
    GET ORG_SUSPENDED "Organization is closed.".
  - sessions: owner and admin with header allowed (role echoed in `/v1/me`); developer/analyst/billing/viewer →
    403; child with `settings.mfa_required` and session not MFA-passed → MFA_REQUIRED, after `enrol_mfa`/verify ok.
  - nested: child-of-child creation stays FORBIDDEN while acting (`POST /v1/orgs/create` with header → 403
    depth message); `GET /v1/orgs` with header → empty list.
  - transfer ownership while acting → 403 decision-6 message.
  - idempotent POST with header stores the row under the child org (query `idempotency_keys.org_id` via
    DbIdempotencyStore monkeypatch, mem-1793281395-6e00) — optional if cheap; otherwise skip and note.
  - switch-org: to own second membership → 200, session body `active_org_id` = it; to a child as owner → 200 with
    `acting_parent_org_id` = parent, then (no header) `GET /v1/me` org = child, role owner, `PATCH /v1/org` renames
    child; as developer → 403; sibling-of-nothing/unknown/other-tenant → 403; missing CSRF → CSRF error; unknown
    body field → 400; after switching to a child, header = sibling → 403, header = parent → 403; demote the user to
    developer in the parent (DB via `add_member`/role change route) → next `GET /internal/auth/session` falls back to
    the oldest membership (`acting_parent_org_id` null); child MFA required → gated after switching.
  - `GET /internal/auth/session` keeps a child active org (regression for `pick_active`).
**apps/api/tests/test_auth_dependencies.py** (unit, fakes): header ignored for blank; INVALID_INPUT; rate limits
  use the child plan (if the unit harness has a limiter fake; else covered in integration via `X-RateLimit-Limit`
  differing between a `start` parent and a `scale` child — Builder picks).
**apps/api/tests/test_openapi.py / internal doc test**: `auth_switch_org` present only in the internal document with
  `x-public: session-flow`; `MeView` has `acting_parent_org_id`.

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` + `uv run python scripts/check_openapi_drift.py`;
`corepack pnpm gen:api` (sdks/typescript, apps/dashboard) + `make test-ts`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and
`uvx radon cc -s -a ... | tail -1` (numeric average for build.done); pip-audit per mem-1793267153-9b68 +
`corepack pnpm audit`; `uv run pre-commit run gitleaks --all-files`. build.done as a PLAIN STRING:
`ralph emit "build.done" "P02-T09b tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: `act_on_sub_account` opens one `global_session` after authentication has closed its
  sessions; never nest it inside another session. `via_parent` runs inside the same system session as
  `for_user_in_org`.
- Do not run `ruff --select RUF100` (mem-1793280879-cb08).
- `get_principal` ordering: the switch must happen before `request.state.ctx`, the structlog bind, the rate limit,
  and `require_scope`/`claim_idempotency`, so idempotency and scope checks see the child context.
- `_session_info` refactor must keep the existing GET /session tests green (fallback persistence, mfa_required).
- `RequestContext` is `slots=True`: a `@property` is fine; `dataclasses.replace` works with slots.
- Child key cache: a parent acting on a child never touches the child's key cache; no invalidation needed.

## 2026-10-29 Builder: P02-T09b build notes
Implemented per plan. Deviations / details for the Reviewer:
- `act_on_sub_account` additionally refuses any header hop when the context already acts in a child
  (`acting_parent_org_id` set) with NOT_A_CHILD_MESSAGE (explicit depth-1 check, decision 3).
- `creating_user_id` keeps its `tenant_session(ctx.org_id)` contract and reads the key row under
  `use_org(session, ctx.principal_org_id)` (an acting key's row lives in the parent; RLS would hide it). Covered by
  `test_a_key_invites_into_the_child_as_its_creator`.
- `routers.auth.session_info(...)` replaces the inline `get_session` body and resolves via `active_membership`
  (`pick_active` no longer used by routers). switch-org lives in `routers/auth_switch_org.py` (side-effect import).
- Existing `test_org_integration.test_patch_never_touches_another_org` assumed the header was ignored; it now
  asserts 403 NOT_A_CHILD and both orgs untouched.
- Idempotency-row-in-child test skipped (plan marked optional): rows are keyed by `ctx.org_id`, which is the child
  once `get_principal` returns; recorded in specs/04 §8.3 decision 5.
- Test helpers: `session_helpers.sub_account(client, name, **headers)`, `sub_account_member(client, identity,
  container, role)` (asynccontextmanager). Log binding test wraps `structlog.contextvars.bind_contextvars`
  (capture_logs misses cached loggers in the full suite).
Gates: make fmt/lint/typecheck pass; placeholders 0; make test 2855 passed, coverage 98.45%; test-ts pass (52 + 48);
openapi + internal drift current; spec refs 0; jscpd 0 clones; radon no C+ (avg 1.9645); pip-audit + pnpm audit no
vulns; gitleaks pass.

## Review P02-T09b (2026-10-29): rejected (1 finding)
Gates re-run by Reviewer: make lint, typecheck, placeholders pass; make test 2855 passed, coverage 98% (logs/review-p02-t09b.log).
Rest of the diff matches specs/04 §8.3 decisions 1–6 and §4.1/§4.3 decisions 7–8 (header parsing, may-act before lookup,
depth 1, child status/MFA gates, principal_org_id for /v1/me and creating_user_id, transfer-ownership refusal, switch-org).

1. **A switched session ignores the parent org's status.** `packages/va_db/src/va_db/repositories/memberships.py`
   `via_parent` (and its callers `session_auth.resolve_switch_target` / `active_membership` / `session_principal`):
   the record carries only the child's `org_status`, and `session_principal` calls `check_org_status(record.org_status, …)`
   on the child alone. So when the parent is `suspended`, an owner/admin who switched into an active child keeps
   **write** access to it. When the parent is `closed` (`DELETE /v1/org`, specs/04 §8.1), they keep full access. On top
   of that, `switch-org` itself never checks the status of the org the session currently acts in (only its MFA gate),
   so a user whose parent was suspended or closed can still switch into a child.
   This contradicts the header path of the same feature. An API key or session in a suspended or closed parent is
   refused by the parent's own gate (specs/04 §1 step 2, §4.3 org-status decision) before `X-VA-Org-Id` is applied,
   so parent status already gates acting there. specs/04 §8.3 grants child access by virtue of the parent role, and
   a closed parent's role must not keep granting it.
   Fix: in `via_parent`, the record (or the caller) must also apply the parent's status. A closed parent →
   `ORG_SUSPENDED` "Organization is closed." for every method. A suspended parent → writes refused with the
   suspended message, reads allowed, the same as the parent's own gate (strictest of parent and child). One option is
   to carry `parent_status` on `MembershipRecord`, or to fold the stricter status into `org_status`. Record it as a
   bullet under specs/04 §8.3 decision 4 ("Gates on the child: … the parent's status gates too").
   Tests in `test_switch_org_integration.py`: switch into a child, then `set_org(identity, parent, status="suspended")`
   → GET ok, PATCH /v1/org → ORG_SUSPENDED; parent `closed` → GET ORG_SUSPENDED "Organization is closed.". Plus one
   `via_parent` repo test that the parent status is exposed.

## 2026-10-29 Builder: P02-T09b fix for review finding 1
- `MembershipRecord.acting_parent_status` (set by `via_parent`) + `gate_status` property (stricter of
  child/parent: closed > suspended > active). `session_principal` gates on `record.gate_status`, so a switched
  session gets the parent's gate: suspended parent → reads ok, writes ORG_SUSPENDED; closed parent → every method
  "Organization is closed.".
- `switch-org`: target reached via a parent → `check_org_status(parent_status, "GET")`, so a closed parent refuses
  the switch; a suspended parent may still switch (reads allowed). The target's own status is still unchecked
  (decision 7).
- specs/04 §8.3 decision 4 gains a "parent's status gates too" bullet.
- Tests: `test_via_parent_exposes_the_parent_status_and_gates_on_the_stricter` (5 status combos + own membership
  has no parent status); switch-org suspended-parent (GET ok, PATCH refused, child unchanged, re-switch ok) and
  closed-parent (GET + switch refused) tests.
Gates: fmt/lint/typecheck pass; placeholders 0; make test 2862 passed, coverage 98.46%; openapi drift current;
spec refs 0; jscpd 0 clones; radon no C+ (avg 1.9671); pip-audit + pnpm audit no vulns; gitleaks pass.

## Review P02-T09b re-review (2026-10-29): approved
Gates re-run by Reviewer: make lint, typecheck, placeholders pass; make test 2862 passed, coverage 98.46%
(logs/review-p02-t09b-fix.log). Finding 1 fixed: `MembershipRecord.acting_parent_status` + `gate_status` (stricter of
child/parent) used by `session_principal`; switch-org refuses a closed parent; specs/04 §8.3 decision 4 bullet added;
repo test over 5 status combos and switch-org suspended/closed parent integration tests present. P02-T09 ticked.

## 2026-10-29 Planner: P02-T09b approved → next P02-T10

## Plan P02-T10 — API key management endpoints (specs/04 §9, §2.1, §2.2 step 5, §2.3 `api_keys:write`, §3.2 "API keys" rows)

### Facts found while planning
- `va_api.auth.api_keys` already has `mint_api_key(hasher, environment)`, `MintedKey`, `creating_user_id(ctx, session)`;
  `va_db.repositories.ApiKeyRepo(session, org_id)` has `get`, `add`, `prefixes`. `invalidate_api_key_cache(redis,
  prefix)` lives in `auth/api_key_auth.py`; `routers/orgs.py` has a private `_invalidate_keys` (log-and-swallow
  RedisError/OSError) — move it to `va_api.auth.api_keys.invalidate_cached_keys(request, prefixes)` and use it from
  both (no copy; jscpd).
- Envelope family `/v1/api_keys*` = envelope (envelopes.FAMILIES); OpenAPI tag `API Keys` already in TAG_NAMES.
- §2.3 has no `api_keys:read`. §3.2 "API keys: list" = R for owner/admin/developer only, and exactly those roles hold
  `api_keys:write` (§3.3 derivation) → GET uses `api_keys:write`.
- Session principals have `environment="live"` and never hold `*`/`mcp` (§3.3). Default create scopes are `["*"]`, so
  without a rule an owner session could never create a default key.
- Audit writer does not exist yet (P02-T11). Planner moved the `api_key.*` audit rows to T11 in plan/phase-02.md.
- `ApiKeyAuthenticator._check_usable` already rejects revoked/expired keys via the injected clock.

### Decisions to record (specs/04 §9, same commit, `> Decision (2026-10-29):`)
1. **Scope.** Every `/v1/api_keys` route declares `require_scope("api_keys:write")` (GET too: §2.3 has no read
   scope and the §3.2 list row matches the `api_keys:write` holders). `x-scope: api_keys:write` on all five.
2. **Grantable scopes** (`grantable_scopes(ctx) -> frozenset[str]`): API key principal → its scopes, where `*`
   means every scope in `SCOPES` (incl. `*`); owner/admin session (incl. acting parent) → all of `SCOPES`
   (incl. `*` and `mcp`: full control per §3.1); other user roles → `ROLE_SCOPES[role]` (developer: W(own) "scopes ⊆
   developer scopes"); widget → empty. Requested scopes must be ⊆ grantable, else `FORBIDDEN`
   "Cannot grant scopes you do not hold: <sorted, comma-joined>." (`*` is grantable only when the caller holds `*`
   or is owner/admin). Validation: 1–50 items, each in `SCOPES` else 400 `INVALID_INPUT` field `scopes` "Unknown
   scope '<s>'."; duplicates removed; a list containing `*` is stored as `["*"]`; otherwise stored sorted.
3. **Environment.** Default = the caller's `ctx.environment` (sessions: live). A `test` principal may create/roll
   only `test` keys → `FORBIDDEN` "Test keys can only manage test keys." (prevents test→live escalation).
4. **expires_at.** Optional, timezone-aware (naive → 400 INVALID_INPUT field `expires_at`), strictly after
   `clock.now()` (else 400 field `expires_at` "expires_at must be in the future.").
5. **Ownership (W(own)).** A `developer` session may PATCH/revoke/roll only keys whose `created_by` is its user id;
   otherwise `FORBIDDEN` "Developers can only manage API keys they created." Owner/admin sessions and API keys
   may manage every key of the org. Another org's key / unknown id → `NOT_FOUND` "API key not found." (RLS + filter).
6. **Revoke.** Sets `revoked_at = now` (already revoked → returned unchanged, 200, `revoked_at` not moved); then
   `DEL apikey:{prefix}` after commit (Redis failure logged `api_key_cache_unavailable`, not raised). A key may
   revoke itself.
7. **Roll.** Refused for a revoked or already-expired key → `CONFLICT` "A revoked or expired API key cannot be
   rolled.". The new key copies `name`, `scopes`, `environment` from the old one, `expires_at = null`,
   `created_by = creating_user_id(ctx)`; it is subject to rules 2 and 3 (a narrow key cannot roll a broader key into
   a plaintext it could not create). Old key `expires_at = min(existing expires_at, now + expire_old_in_hours h)`
   (0 → expires now). Both in one tenant_session; old prefix cache dropped after commit. Response = the new key
   with plaintext `key`.
8. **PATCH** `{name?}` (extra="forbid"; `{}` returns the key unchanged); a revoked key may be renamed. No cache
   invalidation (name is not cached).
9. **List** returns every key of the org including revoked/expired ones, oldest first (created_at, id); not
   paginated (§9 body is `{data:[…]}`, same as members). Never includes `secret_hash`/plaintext.
10. **One-time secret.** Create and roll call `withhold_on_replay(request, "data.key")`; `key` is
   `str | None` in the OpenAPI schema with a description saying replays return null.
11. **Audit.** `api_key.create`/`api_key.revoke`/`api_key.roll` rows are written by P02-T11 (plan moved).

### Files
**packages/va_db/src/va_db/repositories/api_keys.py** (`ApiKeyRepo`):
- `add(..., expires_at: datetime | None = None)`.
- `list(self) -> list[ApiKey]` ordered `created_at, id`, org filter.
- `rename(key_id, name) -> ApiKey | None`, `revoke(key_id, now) -> ApiKey | None` (only sets when `revoked_at IS
  NULL`, returns the row either way), `expire(key_id, expires_at) -> ApiKey | None`. Use `update(...).where(org_id,
  id).returning(ApiKey)` or load+mutate+flush+refresh; keep `updated_at` set (check TimestampMixin onupdate).
  All filter `ApiKey.org_id == self.org_id`.
**apps/api/src/va_api/auth/api_keys.py**: add `grantable_scopes(ctx)`, `check_grantable(ctx, scopes)`,
  `normalize_scopes(scopes) -> list[str]` (or as a pydantic validator in schemas — validation errors must be 400
  with field `scopes`), `check_environment(ctx, environment)`, `check_manages(ctx, key)` (developer own rule),
  `invalidate_cached_keys(request, prefixes)` (moved from routers/orgs.py; orgs.py imports it). Messages as
  `Final` constants exported in `__all__`.
**apps/api/src/va_api/routers/api_keys_schemas.py** (new): `ApiKeyCreate(name 1–100 stripped, scopes:
  list[str] = ["*"], environment: Literal["live","test"] | None = None, expires_at: AwareDatetime | None)`,
  `ApiKeyPatch(name?)`, `ApiKeyRoll(expire_old_in_hours: int 0–168 = 24)`, all extra="forbid";
  `ApiKeyView` (id, name, prefix, scopes, environment, created_by, last_used_at, expires_at, revoked_at,
  created_at) + `ApiKeyCreated(ApiKeyView)` with `key: str | None`; `api_key_view(row)`; examples on every model
  (no None values in examples, mem-1793265656-71f0; use a sample key `va_live_3kQ9…` like orgs_schemas).
**apps/api/src/va_api/routers/api_keys.py** (new): `api_keys_router = envelope_router(prefix="/v1/api_keys",
  tags=["API Keys"])`; operations `api_keys_list` (GET ""), `api_keys_create` (POST ""), `api_keys_update`
  (PATCH "/{key_id}"), `api_keys_revoke` (POST "/{key_id}/revoke"), `api_keys_roll` (POST "/{key_id}/roll");
  responses `DataResponse[...]`; one `tenant_session(ctx.org_id)` per request (pool size 1: invalidate after the
  session closes). Create returns 201? — keep 200 like existing creates (orgs_create/invitations) unless the repo
  convention for envelope creates is 201: Builder checks `org_invitations.py` and matches it.
**apps/api/src/va_api/routers/__init__.py / main.py**: register `api_keys_router` in the routers tuple (look at how
  `orgs_router` is listed).
**apps/api/src/va_api/routers/orgs.py**: use `invalidate_cached_keys`.
**specs/04 §9**: decisions 1–11 (one `> Decision (2026-10-29):` block with bullets). **AGENTS.md**: api key routes,
  helpers, test helpers.
**openapi/openapi.json** regenerated; `corepack pnpm gen:api` in sdks/typescript and apps/dashboard.

### Tests
**packages/va_db/tests/** (extend the existing api-key repo integration test file, or `test_api_key_repo_integration.py`):
  list order + org isolation (other org's keys absent, RLS); rename/revoke/expire return None for another org's
  id; revoke idempotent keeps first `revoked_at`; add stores `expires_at`.
**apps/api/tests/test_api_keys_unit.py** (pure): `grantable_scopes` for `*` key, narrow key, owner/admin/developer/
  analyst sessions, widget; `normalize_scopes` (dedupe, sort, `*` collapse, unknown → AppError field scopes);
  environment rule; developer own rule.
**apps/api/tests/test_api_keys_integration.py** (conftest `identity/container/client`, `session_helpers.signed_in`,
  `add_member`, `error_of`, `sub_account`; mint the caller key via the route or existing helpers):
  - `*` key creates default key → 200, `data.key` matches `va_live_` + 32, `prefix` = first 12, `scopes == ["*"]`,
    `created_by` = key's creator; the new key authenticates `GET /v1/me`; DB row has hash only (no plaintext column
    value equal to key); list shows it without `key`/`secret_hash` fields (assert exact field set).
  - Idempotent replay of create → same id, `data.key` null; roll replay same.
  - narrow key (`api_keys:write`, `calls:read`) creating `["calls:read"]` ok, `["*"]` / `["calls:write"]` → 403;
    unknown scope → 400 field scopes; empty list → 400; name "" / 101 chars → 400; extra field → 400;
    `expires_at` past / naive → 400; future → stored, key stops authenticating after FrozenClock advance.
  - key without `api_keys:write` → 403 on all five; `GET` with `api_keys:write` only works.
  - test key: default environment test, `"environment":"live"` → 403; rolling a live key with a test key → 403.
  - sessions: owner creates default `*` key ok; developer default → 403, developer `["calls:read"]` ok;
    developer revoke/patch/roll of owner-created key → 403, own → ok; analyst/billing/viewer GET → 403.
  - revoke → `revoked_at` set, revoked key gets 401 on `/v1/me` immediately even after a successful auth warmed the
    cache (proves `DEL apikey:{prefix}`); second revoke → same `revoked_at`.
  - roll → new plaintext authenticates, scopes/name/environment copied; old key `expires_at` = now+24 h, still
    authenticates; advance clock 24 h → old 401; `expire_old_in_hours: 0` → old 401 at once; 169 → 400; roll of
    revoked → 409 CONFLICT; narrow key rolling a `*` key → 403.
  - PATCH rename; `{}` unchanged; other org's key id → 404 on PATCH/revoke/roll; unknown uuid → 404.
  - acting parent (`X-VA-Org-Id` child): list/create operate on the child's keys (created key's org = child).
**apps/api/tests/test_openapi.py**: the five operations: envelope, tag `API Keys`, `x-scope` `api_keys:write`,
  `ApiKeyCreated.key` nullable.
Existing `test_orgs_integration.py` must stay green after the `_invalidate_keys` move.

### Gates
`make fmt lint typecheck placeholders test`; `make openapi` + `uv run python scripts/check_openapi_drift.py`;
`corepack pnpm gen:api` (sdks/typescript, apps/dashboard) + `make test-ts`; `uv run python scripts/check_spec_refs.py`;
`npx jscpd@4` (0 clones); `uvx radon cc -n C packages/va_common/src packages/va_db/src apps/api/src` (none) and
`uvx radon cc -s -a ... | tail -1` (numeric); pip-audit per mem-1793267153-9b68 + `corepack pnpm audit`;
`uv run pre-commit run gitleaks --all-files` (sample keys in examples/tests may need the existing allowlist pattern
used by orgs_schemas). build.done as a PLAIN STRING:
`ralph emit "build.done" "P02-T10 tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: `creating_user_id` runs inside the same tenant_session; Redis invalidation after it closes.
- Scope coverage / matrix tests may enumerate routes: GET with `api_keys:write` must not trip a "GET must be :read"
  assertion — if one exists, the decision-1 note is the justification; adjust the test's override list.
- Do not run `ruff --select RUF100` (mem-1793280879-cb08).
- Never log plaintext keys; logs use `key_prefix` only.
- `/v1/api_keys` must not clash with `/v1/api_keys/{key_id}` validation: `key_id: uuid.UUID` → malformed id gives 400
  per existing error handler conventions (match orgs_get).

## 2026-10-29 Builder: P02-T10 build notes
- `routers/api_keys.py` + `api_keys_schemas.py` (envelope, tag API Keys, all `api_keys:write`; create 201, others 200;
  create/roll `withhold_on_replay(request, "data.key")`). Registered in `main.ROUTERS`.
- `va_api.auth.api_keys`: `grantable_scopes`, `normalize_scopes`, `check_grantable`, `check_environment`,
  `check_expires_at`, `check_manages`, `invalidate_cached_keys` (moved from orgs.py `_invalidate_keys`; orgs test now
  monkeypatches `va_api.auth.api_keys.invalidate_api_key_cache`).
- `ApiKeyRepo`: `add(expires_at=)`, `list_keys` (not `list`: shadows the builtin in annotations), `rename`, `revoke`
  (UPDATE ... WHERE revoked_at IS NULL, first time kept), `expire`.
- specs/04 §9 Decision block (all 11 plan decisions). AGENTS.md entry. openapi + TS schema regenerated.
- `session_helpers.store_api_key(..., environment=)`.
- Tests: packages/va_db/tests/test_api_key_repo_integration.py (3), apps/api/tests/test_api_keys_unit.py (15),
  test_api_keys_integration.py (19), test_openapi api-key operations test.
Gates: fmt/lint/typecheck pass; placeholders 0; make test 2900 passed, coverage 98.25% (logs/p02-t10-test.log);
make test-ts pass; openapi drift current; spec refs 0; jscpd 0 clones; radon no C+ (avg 1.9679); pip-audit + pnpm
audit no vulns; gitleaks pass.

## Review P02-T10 (2026-10-29): approved
Gates re-run by Reviewer: make lint, typecheck, placeholders pass; make test 2900 passed, coverage 98.25%
(logs/review-p02-t10-test.log). Diff matches specs/04 §9 table (PATCH `{name?}`, roll 0–168 default 24) and the
recorded decision block; repo updates filter org_id; cache invalidation after session close; error paths
(403 scopes/env/own, 400 validation, 404 cross-org, 409 roll revoked) tested. Note for later: test keys may
revoke/rename live keys (decision limits only create/roll) — acceptable per decision, revisit if specs/28 tightens.

## 2026-10-29 Planner: P02-T10 approved → P02-T11 split into T11a/T11b
T11 covers ~30 mutating routes plus a list endpoint and a grants change; split in plan/phase-02.md:
T11a = writer core + migration + `GET /v1/audit_logs` + envelope-route rows; T11b = `/internal/auth/*` rows +
`x-audit` markers + the enumeration test.

### T11b design carried forward (do not build in T11a)
- Every non-GET APIRoute declares `openapi_extra` `x-audit: "<action>"` (or a list for routes writing more than one
  action) or `x-audit-exempt: "<reason>"`; the test walks `app.state.routers` (mem-1793257682-8f0e) and fails on an
  unmarked route, both markers, or an action not in `va_db.audit.AUDIT_ACTIONS`. Later phases inherit it.
- Auth actions: login → `auth.login` (session's active org); bad password for an existing user → `auth.login_failed`;
  logout / logout-all → `auth.logout`; mfa enable/disable; password change/reset; SSO callback → `auth.sso_login`;
  invitation accept → `invitation.accept` (added to the §13 list in T11a). User-level events (login_failed,
  password_*, mfa_*) go to every org where the user has an active membership; unknown email/no membership → no row.
- Exempt (reasons in the marker): signup, verify-email, resend-verification, password/forgot, mfa/setup,
  mfa/verify, switch-org.

## Plan P02-T11a — Audit writer, append-only grants, `GET /v1/audit_logs`, envelope-route audit rows (specs/04 §13, §8.3 d5; specs/28 §6.3, §10; specs/03 §3; specs/31 §2)

### Facts found while planning
- `AuditLog` model exists (`va_db.models.tenancy`, `AuditActorType` = user|api_key|system; `ip` INET; `changes` jsonb).
- `0001_initial` GRANTS give `va_app` SELECT/INSERT/UPDATE/DELETE on every table including `audit_logs`; specs/28
  §6.3 wants INSERT, SELECT only for `va_app` (append-only, threat T15). No REVOKE anywhere yet.
- `TenantRepository[M].list(limit, cursor, where)` already does keyset pagination newest-first and returns `Page`
  (`Page.pagination()`); `validate_limit`/`decode_cursor` raise INVALID_INPUT with field. No paginated response
  model in `va_api.responses` yet (only `DataResponse`).
- `RequestContext` carries `principal_type`, `principal_id`, `ip`, `user_agent`, `acting_parent_org_id`.
- `va_common.auth.api_keys.API_KEY_RE` is the key regex for value redaction.
- RBAC: `audit.read` permission exists (rbac_matrix "Audit logs" row → `_grant("org", "audit.read")`); envelope
  family `/v1/audit_logs*` registered; §2.3 scope `org:read` covers audit logs.
- spec signature is `va_db.audit.record(ctx, ...)`, but va_db cannot import `va_api.RequestContext` (layering).

### Decisions to record (specs/04 §13, same commit, `> Decision (2026-10-29):` block)
1. **Writer.** `va_db.audit.record(session, actor: AuditActor, action, resource_type, resource_id, before, after)`;
   `AuditActor` (frozen dataclass in va_db: `org_id`, `actor_type`, `actor_id`, `ip`, `user_agent`,
   `acting_parent_org_id`) replaces `ctx` because va_db must not import va_api; va_api builds it with
   `va_api.audit.actor_of(ctx)`. The row is added to the caller's open `tenant_session`, so it commits or rolls
   back with the change. Unknown action → `ValueError` (programming error, not a 4xx).
2. **Actor mapping.** `api_key` principal → actor_type `api_key`, actor_id key id; `user` → `user`, user id;
   `platform_admin` → `user` + `changes.platform_admin = true` (specs/28 §10); `widget` never mutates audited
   resources → `ValueError`. `ip` stored only when it parses as an IP address (`ipaddress.ip_address`), else NULL.
3. **changes** = `{"before": <obj|null>, "after": <obj|null>}` redacted, plus `"acting_parent_org_id": "<uuid>"`
   when acting on a sub-account (row lives in the child org = `ctx.org_id`; "record both orgs" of §8.3 d5) and
   `"platform_admin": true` when applicable. Values are made JSON-safe (uuid/datetime/Decimal → str via
   pydantic `to_jsonable_python`).
4. **Redaction** (recursive through dicts and lists): a key named (case-insensitive) `password`, `secret`, `token`,
   `api_key`, `client_secret`, `authorization`, `secret_headers`, or ending in `_enc` → `"[REDACTED]"` (whole value,
   whatever its type); any string value containing an API-key match (`API_KEY_RE.search`) → `"[REDACTED]"`.
   Other keys like `secret_hash` are not in the list but must never be passed (callers pass views, not rows); add
   `secret_hash` and `key` (ApiKeyCreated plaintext field) to the redacted names as well — record that extension.
5. **Actions list** gains `api_key.update` (rename) and `invitation.accept` (T11b) so every mutating route of this
   phase maps to a listed action; `AUDIT_ACTIONS: frozenset[str]` in `va_db.audit` mirrors the §13 list exactly
   (a test parses §13's action bullet from the markdown and compares, like the RBAC matrix test).
6. **Append-only.** Migration `0004_audit_logs_append_only`: `REVOKE UPDATE, DELETE ON audit_logs FROM va_app`.
   `va_system` keeps DELETE for the retention purge of a later phase (retention by `retention_days`, min 365 in
   hipaa_mode, arrives with OrgSettings/RetentionWorkflow; not in T11a). Downgrade re-grants.
   `credit_ledger`/`usage_events` stay untouched here (their owning billing tasks apply the same rule).
7. **List endpoint.** `GET /v1/audit_logs` (envelope, tag `Audit Logs` — add to TAG_NAMES if missing, `x-scope`
   `org:read`, permission `audit.read` via the existing permission dependency used for e.g. `sso.manage`). Query
   `action`, `actor_id` (uuid), `resource_type`, `resource_id`, `since`, `until` (aware datetimes; naive → 400 field
   name; `since > until` → 400 field `since`), `cursor`, `limit` (1–100, default 25). Filters are exact matches;
   `since` inclusive, `until` exclusive. Response `{data:[AuditLogView], errors:null, pagination:{next_cursor,
   has_more}}`. View: `id, actor_type, actor_id, action, resource_type, resource_id, ip, user_agent, changes,
   created_at`.
8. **Resource conventions** for this task's rows (resource_type / resource_id / before → after):
   - `org_update` → `org.update`, `org`/org id, changed fields before → after (only fields in the PATCH body).
   - `org_members_update` → `member.role_change`, `membership`/user id, `{role}` → `{role}`.
   - `org_members_remove` → `member.remove`, `membership`/user id, `{user_id,email,role}` → null.
   - `org_transfer_ownership` → `org.ownership_transfer`, `org`/org id, `{owner_user_id: old}` → `{owner_user_id: new}`.
   - `org_invitations_create` → `member.invite`, `invitation`/invitation id, null → `{email, role, expires_at}`.
   - `org_invitations_revoke` → `invitation.revoke`, `invitation`/id, `{email, role}` → null.
   - `org_sso_put` → `org.sso_update`, `sso_connection`/connection id, prior view (or null) → new view
     (`client_secret` redacted by name); `org_sso_delete` → `org.sso_update`, prior view → null.
   - `orgs_create` → `subaccount.create` in the **parent** org, `org`/child id, null → `{name, plan_id,
     initial_credits}`; plus `billing.credit_transfer` in the parent when `initial_credits > 0`, `org`/child id,
     null → `{amount_micros}`. The child's API key plaintext never reaches `after`.
   - `orgs_suspend` / `orgs_reactivate` → `subaccount.suspend|reactivate` in the parent, `org`/child id,
     `{status}` → `{status}`; an idempotent no-op (already in that status) writes no row.
   - `api_keys_create` → `api_key.create`, `api_key`/key id, null → view without `key`; `api_keys_update` →
     `api_key.update` `{name}` → `{name}` (`{}` body or same name → no row); `api_keys_revoke` → `api_key.revoke`
     (already revoked → no row); `api_keys_roll` → `api_key.roll`, resource = **old** key id, before `{expires_at}`,
     after `{expires_at, new_key_id, new_prefix}`.
   - Failed requests (any AppError before commit) write nothing because the row is in the same transaction.

### Files
- **packages/va_db/src/va_db/audit.py** (new): `AUDIT_ACTIONS`, `REDACTED = "[REDACTED]"`, `REDACTED_KEYS`,
  `AuditActor`, `redact(value: object) -> object`, `async record(session, actor, action, resource_type,
  resource_id: str | uuid.UUID | None, before: Mapping|None, after: Mapping|None) -> AuditLog`. Export from
  `va_db/__init__` if that's the package convention (check).
- **packages/va_db/src/va_db/repositories/audit_logs.py** (new): `AuditLogRepo(TenantRepository[AuditLog])` with
  `page(filters: AuditLogFilter, limit, cursor) -> Page[AuditLog]` building `where` and delegating to `list`;
  `AuditLogFilter` frozen dataclass. Register in `repositories/__init__`.
- **packages/va_db/migrations/versions/0004_audit_logs_append_only.py** (new; down_revision `0003...`). Update any
  migration-head/test lists (test_migrations*, schema_catalog grant checks if present).
- **specs/03-data-model.md** §3 (or §10 grants): one-line Decision noting 0004 revokes va_app UPDATE/DELETE.
- **apps/api/src/va_api/audit.py** (new): `actor_of(ctx) -> AuditActor`, `async audit(session, ctx, action,
  resource_type, resource_id, before=None, after=None)` thin wrapper used by routers.
- **apps/api/src/va_api/responses.py**: `Pagination` model + `PageResponse[T]` (`data: list[T]`, `errors: None`,
  `pagination: Pagination`), examples wrapped like `DataResponse` (reuse `_wrap_examples` — do not copy; jscpd).
- **apps/api/src/va_api/routers/audit_logs.py** + **audit_logs_schemas.py** (new): `audit_logs_router =
  envelope_router(prefix="/v1/audit_logs", tags=["Audit Logs"])`, operation `audit_logs_list`; examples on
  `AuditLogView` (no None values; mem-1793265656-71f0). Register in `main.ROUTERS`.
- **Routers wired** (call `audit(...)` inside the existing `tenant_session` before it closes): `org.py`
  (org_update), `org_members.py` (3), `org_invitations.py` (create, revoke), `org_sso.py` (put, delete),
  `orgs.py` (create, suspend, reactivate — the create path uses the CreditTransferRepo / provisioner session; write
  the rows in the parent-org transaction; if the child is provisioned in a separate global/system session, the
  audit rows go in the parent's tenant_session that moves credits — Builder checks and keeps one transaction for
  the parent rows), `api_keys.py` (create, update, revoke, roll).
- **specs/04 §13** Decision block (1–8); **AGENTS.md** (audit helper, how to add an action, migration 0004 note).
- **openapi/openapi.json** regenerated; `corepack pnpm gen:api` in sdks/typescript and apps/dashboard.

### Tests
- **packages/va_db/tests/test_audit_unit.py**: `redact` — each named key (case-insensitive), `*_enc`, nested
  dict/list, API key embedded in a string (`"Bearer va_live_<32>"`), non-secret untouched, input not mutated;
  `AUDIT_ACTIONS` equals the action list parsed from specs/04 §13; `record` rejects unknown action.
- **packages/va_db/tests/test_audit_integration.py**: `record` row fields (actor, ip INET round trip, invalid ip →
  NULL, changes shape); rollback of the enclosing transaction leaves no row; as `va_app`, `UPDATE audit_logs` and
  `DELETE FROM audit_logs` raise insufficient privilege (InsufficientPrivilegeError via DBAPIError) while INSERT/
  SELECT work; RLS: another org's rows invisible; `AuditLogRepo.page` filters (each one), since/until bounds,
  ordering, cursor pages no dupes.
- **apps/api/tests/test_audit_logs_integration.py**: GET as owner session and `*` key; analyst/billing/viewer/
  developer session → 403 per §3.2 "Audit logs" row (check matrix for which roles hold `audit.read`); key without
  `org:read` → 403; filters + pagination over HTTP; naive `since` / bad cursor / limit 0 / 101 → 400 with field;
  org B key sees none of org A rows; acting parent (`X-VA-Org-Id`) reads the child's log.
- **apps/api/tests/test_audit_rows_integration.py**: for each wired route, perform the mutation and assert exactly
  one row with action, actor_type/actor_id, resource_type/id, before/after, ip, user_agent; api_keys create row
  has no plaintext anywhere (`API_KEY_RE` not in `json.dumps(changes)`); SSO put row has `client_secret` redacted;
  orgs_create writes `subaccount.create` + `billing.credit_transfer` in the parent; acting-parent mutation row
  in the child with `changes.acting_parent_org_id`; failed mutation (403/409) writes no row; no-op revoke/suspend
  writes no row. Use a shared helper `audit_rows(identity, org_id, action=None)` in tests/support or
  session_helpers (no per-module copies; jscpd).
- **apps/api/tests/test_openapi.py**: `audit_logs_list` envelope, tag, `x-scope org:read`, pagination in schema.
- Existing test suites must stay green (scope coverage / matrix tests enumerate the new route).

### Gates
`make fmt lint typecheck placeholders test`; `make migrate`-path tests (test_migrations*), `make openapi` +
`uv run python scripts/check_openapi_drift.py`; `corepack pnpm gen:api` (sdks/typescript, apps/dashboard) +
`make test-ts`; `uv run python scripts/check_spec_refs.py`; `npx jscpd@4` (0 clones); radon `-n C` none and
`-s -a` numeric average; pip-audit (mem-1793267153-9b68) + `corepack pnpm audit`; gitleaks. build.done as a PLAIN
STRING with numeric complexity (mem-1793273041-6642, mem-1793269824-fe85):
`ralph emit "build.done" "P02-T11a tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: write audit rows in the route's existing session; never open a second session for them.
- The schema conformance / migration tests may snapshot privileges or the head revision: update expected heads.
  Tests that UPDATE/DELETE audit_logs as va_app (fixtures/cleanup) will break — clean up as owner/superuser.
- `ip` from ASGI tests is `127.0.0.1` (httpx) — fine for INET; Starlette TestClient gives `testclient` → NULL.
- Do not run `ruff --select RUF100` (mem-1793280879-cb08). Keep each router function under radon C (extract
  small helpers for before/after dicts).

## 2026-10-29 Builder: P02-T11a build notes
- `va_db.audit` (new): `AUDIT_ACTIONS` (= specs/04 §13 list incl. new `invitation.accept`, `api_key.update`),
  `REDACTED`, `REDACTED_KEYS` (+`secret_hash`, `key`), `AuditActor` (+`platform_admin` flag), `redact`, `parse_ip`,
  type aliases `ResourceId`/`Changes`, `record(session, actor, action, resource_type, resource_id, *, before, after)`
  — flushes the row inside `use_org(session, actor.org_id)`, so it works on tenant and global sessions and can log
  into the parent while the transaction runs in a sub-account. before/after are keyword-only (ruff PLR0917).
- `AuditLogRepo` + `AuditLogFilter` (`repositories/audit_logs.py`); migration `0004_audit_logs_append_only`
  (REVOKE UPDATE, DELETE FROM va_app; downgrade re-grants). Updated test_migrations(+_integration) heads/privileges
  and test_rls_isolation (append-only tables expect "permission denied").
- `va_api.audit`: `actor_of(ctx)`, `audit(session, ctx, ...)`, `changed(before, after)`. `va_api.responses`:
  `Pagination`, `PageResponse[T]` (examples reuse `_parameter`/`_model_examples`). Router `audit_logs.py`
  (+schemas), registered in `main.ROUTERS`.
- Rows wired: org_update (global session; only changed fields; no-op → none), members role/remove/transfer,
  invitations create/revoke, sso put (client_secret → "[REDACTED]" when sent)/delete, orgs create (+credit transfer,
  `initial_credits_micros`), suspend/reactivate (no-op → none), api_keys create/update/revoke/roll.
- Specs: specs/04 §13 list + Decision block (8 items), stale "(P02-T11)" notes in §5/§8/§9 pointed at it;
  specs/03 §10 Decision for 0004. AGENTS.md entry. openapi + TS schemas regenerated.
- Tests: va_db test_audit_unit (21), test_audit_integration (8), test_audit_logs_migration (4); api
  test_audit_unit (5), test_audit_logs_integration (21), test_audit_rows_integration (8), test_openapi (+1);
  helper `session_helpers.audit_rows`.
Gates: fmt/lint/typecheck pass; placeholders 0; make test 2968 passed, coverage 97.93% (logs/p02-t11a-test.log);
make test-ts pass; openapi drift current; spec refs 0; jscpd 0 clones; radon -n C none on src (avg 1.9977);
pip-audit + pnpm audit no vulns; gitleaks (pre-commit) passed.

## Review P02-T11a (2026-10-29): approved
- Re-ran: make lint typecheck placeholders (pass), make test 2968 passed / 98% (logs/rev-t11a-test.log), make test-ts
  pass, openapi drift current, spec refs 0, jscpd 0 clones.
- Diff matches specs/04 §13 Decision 1-8: writer in caller's transaction under use_org, redaction (named keys,
  *_enc, API-key strings), 0004 REVOKE UPDATE/DELETE from va_app, list endpoint filters/pagination/audit.read,
  no-op mutations write no row, api key plaintext never in changes.
- Non-blocking notes for later: `org_sso_put` reads `existing` in a separate session before the write txn, so a
  concurrent PUT could record a stale `before`; `AUDIT_ACTIONS` comment says "in its order" on a frozenset.

## 2026-10-29 Planner: P02-T11a approved → next P02-T11b

## Plan P02-T11b — `/internal/auth/*` audit rows + mutating-route marker test (specs/04 §13, §4.1, §4.5, §5)

### Facts found while planning
- 31 mutating routes today (30 POST/PUT/PATCH/DELETE + GET `/internal/auth/sso/callback`, which logs in).
  Internal auth routes carry `openapi_extra=PUBLIC_MARKER` (`{"x-public": "session-flow"}`, routers/auth.py:85)
  and `include_in_schema=False`, so their markers only show on `route.openapi_extra`, not in openapi.json.
- `SessionStore.start` wraps `start_in(session, ...)` (auth/sessions.py:135/151); `revoke`/`revoke_all`
  (230/237) open their own `global_session` then `forget(hashes)`. `memberships_of_user(request, user_id)`
  (session_auth.py:232) runs in a **system** session (separate pool from va_app global/tenant: fetch it
  BEFORE opening the write session; never nest two va_app sessions, mem-1793262412-7bed).
- Memberships have no status column: "orgs of the user" = `MembershipLookupRepo.for_user` records.
- `va_db.audit.record` flushes under `use_org(actor.org_id)`, so one `global_session` can write rows into
  several orgs. `AuditActor(org_id, actor_type, actor_id, ip, user_agent, ...)` can be built without a ctx.
- Writes today: login → `_issue` (sessions.start); login failure → `_authenticate` raises INVALID_CREDENTIALS;
  password change/reset → one global_session each; mfa enable/disable → `MfaRepo` in a global_session;
  SSO → `_provision` tenant_session(connection.org_id); invitation accept → `_accept_signed_in` /
  `_accept_new_user` tenant_session(found.org_id).

### Decisions to record (specs/04 §13, same commit, `> Decision (2026-10-29):` block "Dashboard auth rows and
route markers (P02-T11b)"; also replace the last bullet of the T11a Decision 8 with a pointer to it)
1. **Orgs.** User-level events (`auth.login`, `auth.login_failed`, `auth.logout`, `auth.mfa_enabled`,
   `auth.mfa_disabled`, `auth.password_changed`, `auth.password_reset`) write one row in **every org where the user
   has a membership** at that moment (the user's access spans them all); a user with no membership → no row.
   `auth.sso_login` writes one row in the connection's org (the only org the IdP authenticated for; the user's
   other memberships are invisible inside that tenant transaction). `invitation.accept` → the invitation's org.
2. **Actor.** `actor_type=user`, `actor_id` = the user; for `auth.login_failed` it is the account the attempt
   targeted (an unknown email writes nothing — no org to log in, and no enumeration surface). `ip` =
   `session_auth.client_ip(request)`, `user_agent` = request header. No `acting_parent_org_id`.
3. **Rows** (resource_type / resource_id: before → after), all `resource_type="user"`, id = user id, except where
   stated:
   - `auth.login`: null → `{method: "password", session_id}`.
   - `auth.login_failed`: null → `{reason}` with reason `invalid_password` (wrong or no password hash — SSO-only
     account), `account_disabled`, or `invalid_current_password` (wrong current password on `password/change`).
     Throttle lock (`LOGIN_LOCKED`) and `SSO_REQUIRED` write nothing (no credential was checked). Written in its
     own committed transaction before the error is raised.
   - `auth.logout`: null → `{revoked_sessions: n, all: false|true}` (logout / logout-all); n = 0 → no row.
   - `auth.mfa_enabled`: `{mfa_enabled: false}` → `{mfa_enabled: true}`; `auth.mfa_disabled` reverse.
   - `auth.password_changed`: null → `{revoked_sessions: n}` (other sessions); `auth.password_reset`: null →
     `{revoked_sessions: n}`.
   - `auth.sso_login`: null → `{method: "sso", session_id, user_created, membership_created}` (connection org).
   - `invitation.accept`: `invitation`/invitation id: null → `{user_id, email, role, user_created}`.
   Each success row is written in the same transaction as the change (session row, password, MFA flag,
   membership); logout/logout-all need `SessionStore.revoke_in` / `revoke_all_in` for that (see Files).
4. **Route markers.** Every APIRoute with a method other than GET/HEAD/OPTIONS declares exactly one of
   `openapi_extra["x-audit"]` = non-empty list of §13 actions the route may write, or
   `openapi_extra["x-audit-exempt"]` = non-empty reason string. A GET that mutates may carry `x-audit`
   (`auth_sso_callback`). Built with `va_api.audit.audited(*actions)` (ValueError at import on an unknown action)
   and `va_api.audit.audit_exempt(reason)`. A test walks every router of the app, so later phases inherit it.
5. **Exempt routes** (reason strings): `auth_signup` ("creates the org and its owner; the org's log starts
   empty"), `auth_verify_email` ("email verification is not a §13 action"), `auth_resend_verification`
   ("sends an email only"), `auth_password_forgot` ("sends an email only; no account change"), `auth_mfa_setup`
   ("pending secret; MFA changes are logged by enable"), `auth_mfa_verify` ("second factor of a logged login"),
   `auth_switch_org` ("session preference only").

### Files
- **apps/api/src/va_api/audit.py**: add `AUDIT_KEY: Final = "x-audit"`, `AUDIT_EXEMPT_KEY: Final =
  "x-audit-exempt"`, `audited(*actions: str) -> dict[str, list[str]]` (non-empty, each in `AUDIT_ACTIONS`, else
  ValueError), `audit_exempt(reason: str) -> dict[str, str]` (non-blank), `user_actor(request, org_id, user_id)
  -> AuditActor` (actor_type user, client_ip, user-agent), and `async audit_user(session, request, org_ids:
  Iterable[uuid.UUID], user_id, action, *, before=None, after=None) -> list[AuditLog]` (one `record` per distinct
  org, resource `user`/user_id). Extend `__all__` and the module docstring.
- **apps/api/src/va_api/auth/sessions.py**: `revoke_in(session, active) -> int` and `revoke_all_in(session,
  user_id) -> list[str]` (DB part only); `revoke`/`revoke_all` delegate (+`forget`). Docstrings: caller forgets
  the hashes after commit.
- **routers/auth.py**: `_issue` → fetch `memberships_of_user` first, then one `global_session`: `start_in` +
  `audit_user(..., "auth.login", after={method, session_id})`, then `sessions.remember(active)`. `_authenticate`
  failure branch → `await _login_failed(request, user, reason)` when `user is not None` (helper: memberships, then
  global_session + audit_user) before raising; keep functions under radon C (extract `_failure_reason(user,
  password_hash, verified) -> str | None`). logout / logout-all → global_session: `revoke_in`/`revoke_all_in` +
  `audit_user` (memberships fetched before; skip when 0 revoked), then `forget`. Markers on all 7 routes:
  `{**PUBLIC_MARKER, **audited(...)}` / `{**PUBLIC_MARKER, **audit_exempt(...)}` (module-level constants).
- **routers/auth_password.py**: reset → `_apply_reset` takes the org ids and writes `auth.password_reset`;
  change → same session writes `auth.password_changed`; `_verify_current` mismatch → `_login_failed(...,
  "invalid_current_password")` (import the helper from routers/auth.py; export it there). Markers: forgot exempt;
  reset `audited("auth.password_reset")`; change `audited("auth.password_changed", "auth.login_failed")`.
- **routers/auth_mfa.py**: enable/disable write rows in the `MfaRepo` session (only when the repo reports the
  change); disable reuses its existing `memberships_of_user` result. Markers: setup/verify exempt, enable/disable
  audited.
- **routers/auth_sso.py**: `_provision` writes `auth.sso_login` in its tenant_session after `start_in` (actor org
  = connection.org_id); callback marker `{**PUBLIC_MARKER, **audited("auth.sso_login")}`; sso_start is GET, no
  marker.
- **routers/auth_invitations.py**: `_join` (or both callers) writes `invitation.accept` in the tenant_session;
  `user_created` False for signed-in, True for new user. Marker `audited("invitation.accept")`.
- **routers/auth_switch_org.py**: exempt marker.
- **Envelope routers** (org.py, org_members.py, org_invitations.py, org_sso.py, orgs.py, api_keys.py): merge
  `audited(...)` into each mutating route's `openapi_extra` (api_keys `_EXTRA` is shared with GET list — build
  per-route dicts `{**_EXTRA, **audited(...)}`): org_update [org.update]; org_members_update [member.role_change],
  org_members_remove [member.remove], org_transfer_ownership [org.ownership_transfer]; org_invitations_create
  [member.invite], org_invitations_revoke [invitation.revoke]; org_sso_put / org_sso_delete [org.sso_update];
  orgs_create [subaccount.create, billing.credit_transfer], orgs_suspend [subaccount.suspend], orgs_reactivate
  [subaccount.reactivate]; api_keys_create/update/revoke/roll [api_key.create|update|revoke|roll].
- **specs/04 §13** Decision block (1–5); **AGENTS.md**: "every mutating route needs `audited(...)` or
  `audit_exempt(...)` in openapi_extra; test_audit_markers enforces".
- **openapi/openapi.json** regenerated (`make openapi`; x-audit appears on public envelope routes) + `corepack pnpm
  gen:api` in sdks/typescript and apps/dashboard; update test_openapi expectations only if they compare whole
  extension dicts.

### Tests
- **apps/api/tests/test_audit_markers.py** (new): builds the app as test_envelopes does (`app.state.routers`,
  APIRoute only); for each route with a method outside GET/HEAD/OPTIONS: exactly one of the two keys; `x-audit`
  is a non-empty list ⊆ `AUDIT_ACTIONS` with no duplicates; exempt reason non-blank; any GET route carrying
  `x-audit` passes the same checks. A pinned dict `PHASE_02 = {operation_id: [actions] | "exempt"}` for all 31
  routes is compared for those operation ids (subset compare so later phases add routes freely). Unit tests for
  `audited()` (unknown action / empty → ValueError) and `audit_exempt("")` → ValueError. A synthetic router with
  one unmarked POST fed to the same checker function → it reports that route (proves the check bites).
- **apps/api/tests/test_auth_audit_integration.py** (new; reuse conftest fixtures, `session_helpers.audit_rows`,
  `signed_in`, `add_member`, `enrol_mfa`/`mfa_code`, `invite`, sso fixtures — no copied helpers, jscpd):
  - login success: user with memberships in two orgs → one `auth.login` row in each, actor user, ip `127.0.0.1`,
    user_agent, `after.method == "password"`, `session_id` matches the new session.
  - wrong password → `auth.login_failed` `{reason: invalid_password}` in each org, request still 401/400 as
    today; disabled user → `account_disabled`; unknown email → no row anywhere; locked (LOGIN_LOCKED) → no new
    row; SSO-enforced domain → no row.
  - logout → `auth.logout` `{revoked_sessions:1, all:false}`; logout-all with 2 sessions → `{revoked_sessions:2,
    all:true}`.
  - mfa enable / disable rows (before/after flags); failed enable (bad code) → no row.
  - password change → `auth.password_changed`; wrong current password → `auth.login_failed`
    `invalid_current_password` and no `password_changed`; password reset → `auth.password_reset` with
    revoked count; reset with bad token → no row.
  - SSO callback (fake IdP) → `auth.sso_login` in the connection org with `user_created`/`membership_created`
    true for a JIT user, false on second login.
  - invitation accept: new user → `invitation.accept` `user_created: true`, signed-in existing user →
    `user_created: false`; accept of a used/expired token → no row.
  - no row contains a password, token, or `va_session` value (`json.dumps(changes)` checks against the plaintext).
- Exempt routes: one test calls signup, verify-email, mfa/setup, switch-org and asserts zero `audit_logs` rows
  were added for the org (documents the exemption).
- **packages / va_api unit**: `test_audit_unit.py` add `user_actor` / `audit_user` (distinct org ids → one row
  each) if not covered by integration; `SessionStore.revoke_in`/`revoke_all_in` covered via existing session tests
  staying green.
- Existing suites green; T11a's test_audit_rows_integration unchanged.

### Gates
`make fmt lint typecheck placeholders test`; `make test-ts`; `make openapi` + `uv run python
scripts/check_openapi_drift.py`; `corepack pnpm gen:api` (sdks/typescript, apps/dashboard);
`uv run python scripts/check_spec_refs.py`; `npx jscpd@4` (0 clones); radon `-n C` none on src + numeric average;
pip-audit + `corepack pnpm audit`; gitleaks. build.done PLAIN STRING (mem-1793273041-6642):
`ralph emit "build.done" "P02-T11b tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: `memberships_of_user` (va_system) BEFORE opening the va_app global/tenant session; do not
  call it inside one. `_login_failed` must commit even though the request then fails (own session, then raise).
- Login timing: the failure branch now does extra DB work only for existing users — the argon2 verify already ran
  for all; acceptable, but do not add a user-existence signal to the response.
- `include_in_schema=False` routes: markers do not reach openapi.json; the internal OpenAPI document (T04d) may
  now show `x-audit` — regenerate whatever `make openapi` writes and run drift for both.
- api_keys `_EXTRA` is shared by GET list: do not put `x-audit` on the GET.
- radon: `_authenticate` and the logout routes gain branches — extract helpers.

## 2026-10-29 Builder: P02-T11b build notes
- `va_api.audit`: `AUDIT_KEY`/`AUDIT_EXEMPT_KEY`, `audited(*actions)` (dedupes, ValueError on unknown/empty),
  `audit_exempt(reason)`, `user_actor(request, org_id, user_id)`, `audit_user(session, request, org_ids, user_id,
  action, before=, after=)` (one row per distinct org, resource user).
- `SessionManager.revoke_in` / `revoke_all_in`; `revoke`/`revoke_all` delegate.
- routers/auth.py: `audit_login_failed(request, user_id, reason)` (exported; memberships first, own committed
  global session); `_authenticate` audits an existing account's failure (`account_disabled` only when the
  password verified, else `invalid_password`); `_issue` = start_in + `auth.login` in one global session;
  `_sign_out(request, active, everywhere=)` for logout/logout-all (no row when 0 revoked).
- password reset/change, mfa enable/disable, sso `_provision`, invitation `_join(session, request, found, user,
  now, user_created=)` write their rows in the changing transaction; `_verify_current` mismatch →
  `auth.login_failed invalid_current_password`.
- Markers on all 31 mutating routes (7 exempt); envelope routes merge `**audited(...)` into openapi_extra
  (api_keys GET list keeps plain `_EXTRA`).
- Specs/04 §13: T11a Decision 8 last bullet → pointer; new Decision block "Dashboard auth rows and route markers
  (P02-T11b)" items 1–5. AGENTS.md entry. openapi.json/internal.json regenerated (x-audit extensions); TS
  gen:api produced no diff.
- Tests: new test_audit_markers.py (16: whole-app check, PHASE_02 pinned table, exempt reasons, synthetic
  unmarked route bites, audited/audit_exempt units), test_auth_audit_integration.py (10); SSO login rows added
  to test_sso_login_integration, no-row assertion to test_sso_enforce_integration (domain_user returns Member).
  T11a tests adjusted for the new sign-in rows: test_audit_rows_integration `route_rows` drops `auth.*`;
  test_audit_logs_integration `signed_in_owner` deletes the sign-in `auth.login` row via system_session.
Gates: fmt/lint/typecheck pass; placeholders 0; make test 2995 passed, coverage 97.82% (logs/p02-t11b-test.log);
make test-ts pass; openapi drift current; spec refs 0; jscpd 0 clones; radon -n C none (avg 2.0129); pip-audit
(--no-deps on uv export) + pnpm audit no vulns; gitleaks (pre-commit) passed.

## Review P02-T11b (2026-10-29): approved
- Re-ran: lint/typecheck/placeholders pass; make test 2995 passed, coverage 97.81% (logs/review-t11b-test.log);
  openapi drift current (public + internal); spec refs 0; jscpd 0 clones; radon -n C none, avg 2.0129.
- Diff matches plan + specs/04 §13 Decision (P02-T11b): rows written in the changing transaction, memberships
  read before the va_app session, login_failed committed before raising, unknown email / AUTH_LOCKED /
  SSO_REQUIRED write nothing, markers on all 31 mutating routes, marker checker proven to bite.
- P02-T11 parent ticked (T11a + T11b done).

## 2026-10-29 Planner: P02-T11b approved → P02-T12 split into T12a/T12b/T12c
- T12 needs a new package (`va_domain` does not exist yet), a ~60-field model whose fields are spread over nine specs
  with four conflicts, two routes plus a cache, and enforcement of two security settings. Split: T12a model + package +
  spec reconciliation (no routes); T12b routes, accessor, cache, audit; T12c `ip_allowlist` / `session_max_age_days`
  enforcement (otherwise those two settings would be dead fields).

### T12b design carried forward (do not build in T12a)
- Routes: `GET /v1/org/settings` (`org:read`, `operation_id="org_settings_get"`) and `PATCH /v1/org/settings`
  (`org:write`, `org_settings_update`, `audited("org.update")`), both `DataResponse[OrgSettings]` (full object with
  defaults); `PATCH /v1/org` gains `settings: OrgSettings | None` (same merge). The phase plan names
  `/v1/org/settings`, specs/04 §8.1 only `PATCH /v1/org {settings?}`: build both, record in §8.1.
- PATCH body is parsed as `OrgSettings` itself: every field has a default, so `body.model_dump(mode="json",
  exclude_unset=True)` is exactly what the client sent (recursive for sub-models), field errors come out with real
  paths, and the OpenAPI schema is the model. Then `merge_settings(stored, sent)` → `OrgSettings.model_validate` →
  `check_cross_field(merged, plan_id)` → store `sparse(merged-input)`.
- Permissions: any sent path in `COMPLIANCE_PATHS` → `org.compliance`; `baa_signed_at` sent by a non
  `platform_admin` → `FORBIDDEN` "Only platform administrators can set 'baa_signed_at'."; `hipaa_mode` true with
  `baa_signed_at` null → `INVALID_INPUT` field `hipaa_mode`; `privacy.zero_retention` true on a plan other than
  `enterprise` → `FEATURE_NOT_IN_PLAN` field `privacy.zero_retention`. Field paths: `settings.<path>` under
  `PATCH /v1/org`, `<path>` under `/v1/org/settings`.
- `va_db.org_settings.OrgSettingsReader(database, redis)`: `get(org_id) -> OrgSettings | None`, Redis
  `orgsettings:{org_id}` string JSON (the stored sparse object) EX 60, Redis errors logged and fall back to the DB
  (same pattern as `ApiKeyAuthenticator._cached/_store`), `invalidate(org_id)` = DEL after the write commits; add the
  key to specs/03 §11. `GET /v1/org` returns `OrgSettings.model_validate(org.settings)` dumped with defaults.
- Audit: one `org.update` row, before/after keyed by dotted setting path (`settings.timezone`), only changed paths.

## Plan P02-T12a — `va_domain` package and `OrgSettings` (specs/04 §7; specs/02 layout; specs/15 §5; specs/17 §3, §7; specs/23)

### Facts found while planning
- `packages/va_domain` does not exist; specs/02 lists it ("Pydantic API/domain schemas and enums shared across
  services"); specs/04 §7 names `va_domain.org.OrgSettings` (`extra="forbid"`, all fields defaulted).
- Package scaffolding precedent: commit 879b2e0 (va_events) — `pyproject.toml` (uv_build), `src/<pkg>/__init__.py`
  with `__version__ = version("va-domain")`, `py.typed`, `tests/`; root `pyproject.toml`: dev group `"va-domain"`,
  `[tool.uv.sources] va-domain = { workspace = true }`, mypy `mypy_path` + `files` (`packages/va_domain/src`,
  `packages/va_domain/tests`); `uv lock`. Coverage source already covers `packages`.
- Stored org settings today are free JSON; `orgs.settings.mfa_required` is read as raw JSON `true` by
  `MembershipLookupRepo` (SQL) and `auth/sub_accounts.py:73`. Storage must stay sparse (only explicitly set keys)
  so those readers keep working and later default changes take effect — helpers here, storage in T12b.
- Org-setting fields referenced outside §7 (all must be in the model): specs/23 §2 `compliance.dnc_inherit_from_parent`
  (true), §3 `compliance.calling_hours` ([CallingWindow], 08:00–21:00 all days), `compliance.state_quiet_hours` (true),
  `compliance.max_attempts_per_number_per_day` (int, 0 = unlimited), §4 `compliance.recording_disclosure_policy`
  (`always|auto|never`, `auto`), §5 `compliance.ai_disclosure_policy` (`always|auto|never`, `always`),
  `compliance.require_consent_attestation` (true), §6 `compliance.redaction.enabled` / `redact_llm_context`;
  specs/18 `analysis.sentiment` (true), `delete_carrier_recordings` (true); specs/16 `cnam_display_name` (≤ 15 letters,
  digits, space); specs/17 `batch_concurrency_share` (0.1–1.0, 1.0); specs/21 `sms.smart_encoding` (true);
  specs/07 `call_defaults.no_input_timeout_ms` (inside the free-form `call_defaults`).
- Conflicts: (a) §7 top-level `calling_hours {start,end,days:int[]}` vs specs/17/23 `compliance.calling_hours:
  [CallingWindow]` with day names; (b) §7 `redaction.enabled` vs specs/23 `compliance.redaction.*`; (c) §7
  `analysis.retranscribe` default false vs specs/08 "default true when record=true"; (d) §7
  `llm.context_budget_tokens` max 64 000 vs specs/10 max 32 000.
- `GuardRail` (specs/23 §1.1, `va_compliance`) and `SendCallRequest` (specs/05, phase 03+) do not exist yet.
- `email-validator` is already an api dependency (pydantic `EmailStr`). No E.164 helper in va_common.

### Decisions to record (specs/04 §7, one `> Decision (2026-10-29):` block "OrgSettings fields (P02-T12a)";
pointer notes in specs/23 §3 and §6, specs/08 (retranscribe line), specs/10 (budget line))
1. **Conflicts**: (a) one calling-hours setting, `compliance.calling_hours: list[CallingWindow]` (specs/17 §3 shape,
   1–14 windows, default one window all seven days 08:00–21:00); the §7 top-level `calling_hours` row is replaced by it
   (more specs consume the compliance form and it shares the batch model). (b) `redaction` stays a top-level group
   (§7 Python layout) with `enabled` (false) and `redact_llm_context` (false); specs/23 §6 `compliance.redaction.*`
   reads as `redaction.*`. (c) `analysis.retranscribe: bool | None = None`, null = "on when the call is recorded"
   (specs/08), true/false overrides. (d) `llm.context_budget_tokens` 1 000–32 000 (the consumer's max), default 6 000.
2. **Additions** (from the other specs, defaults as there): `compliance.dnc_inherit_from_parent` true,
   `compliance.state_quiet_hours` true, `compliance.max_attempts_per_number_per_day` int 0–100 default 0,
   `compliance.recording_disclosure_policy` `auto`, `compliance.ai_disclosure_policy` `always`,
   `compliance.require_consent_attestation` true, `analysis.sentiment` true, top-level `delete_carrier_recordings` true,
   `cnam_display_name` `^[A-Za-z0-9 ]{1,15}$` | null, `batch_concurrency_share` 0.1–1.0 default 1.0, group
   `sms: SmsSettings` with `smart_encoding` true. `ai_disclosure_required=true` forces the rail whatever the policy.
3. **Free-form until their owners land**: `call_defaults` is `dict[str, JsonValue]` (validated against
   `SendCallRequest` when the calls API lands); `default_guard_rails` items are `GuardRailConfig` with `type` ∈ the five
   specs/23 §1.1 types and other keys kept (`extra="allow"`), ≤ 10 items, until `va_compliance.guard_rails.GuardRail`
   replaces it.
4. **Value rules**: `timezone` must load with `zoneinfo.ZoneInfo` (package depends on `tzdata`); `default_language`
   1–16 chars `^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$|^babel$`… keep it simple: 1–16 chars of letters, digits, `-`;
   `default_voice` 1–100 chars; E.164 `^\+[1-9][0-9]{1,14}$` (`default_from_number`); `blocked_prefixes` items
   `^\+[0-9]{1,15}$` (≤ 200); countries `^[A-Z]{2}$` (`allowed_countries` ≤ 250, unique); URLs http(s) ≤ 2 048 stored
   as strings (`default_webhook`; scheme policy stays at webhook-send time, specs/19); `llm.base_url` https only;
   `llm.api_key_ref` `^[a-z0-9_-]{1,64}$`; model names 1–200 chars; `llm.temperature_default` 0–2;
   `default_webhook_events` ≤ 50 unique strings 1–64 chars; `pii_redaction_entities` ≤ 20 unique `^[a-z][a-z0-9_]*$`;
   `ip_allowlist` ≤ 100 CIDRs normalized by `ipaddress.ip_network(strict=False)` to their string form;
   `notification_emails` ≤ 20 `EmailStr`; `dispositions_default` ≤ 50 strings 1–100 chars; messages ≤ 500; summary
   prompt ≤ 2 000; `low_balance_threshold_micros` ≥ 0; datetimes `AwareDatetime`; `voice_overrides`
   `dict[uuid.UUID, VoiceSettings]` ≤ 200 entries; `CallingWindow.end > start`, `HH:MM` 00:00–23:59, days unique,
   ≥ 1; windows of one day must not overlap (specs/17 §3).
5. **Merge and storage**: groups (the 12 sub-model fields) merge key by key, every other field (maps and lists
   included, e.g. `voice_overrides`, `compliance.calling_hours`, and the nested `redaction` dict) is replaced whole;
   `null` sets a nullable field to null and is invalid elsewhere. Stored `orgs.settings` holds only explicitly set
   keys. Compliance paths (need `org.compliance`, T12b): every key under `compliance`, `privacy`, `redaction`, plus
   `hipaa_mode`, `baa_signed_at`, `pii_redaction`, `pii_redaction_entities`, `viewers_can_see_pii`, `mfa_required`,
   `session_max_age_days`, `ip_allowlist`, `ai_disclosure_required`, `recording_disclosure_required`,
   `honor_opt_out`, `allow_insecure_webhooks`, `international_enabled`, `allowed_countries`, `blocked_prefixes`.
   Cross-field rules (T12b, checked on the merged object, not in model validators because a partial body is parsed
   with the same model): `hipaa_mode` needs `baa_signed_at`; `privacy.zero_retention` needs plan `enterprise`.

Builder: drop the "keep it simple" wording above from the spec text — record the final `default_language` rule as
"1–16 characters of letters, digits and `-`".

### Files
- **packages/va_domain/pyproject.toml** (`va-domain`, deps `pydantic>=2.9`, `email-validator>=2.3.0`,
  `tzdata>=2025.2`), **src/va_domain/__init__.py**, **py.typed**.
- **src/va_domain/voices.py**: `VoiceSettings` exactly as specs/15 §5 (`extra="forbid"`).
- **src/va_domain/scheduling.py**: `CallingWindow` (specs/17 §3: `days: list[Literal["mon",…,"sun"]]`, `start`,
  `end`, validators), `Weekday` alias, `windows_overlap(windows) -> bool` helper used by the settings validator (specs/17
  batches will reuse it).
- **src/va_domain/org.py**: `OrgSettings` + sub-models `ComplianceSettings`, `LlmSettings`, `PrivacySettings`,
  `RedactionSettings`, `AnalysisSettings`, `MemorySettings`, `ToolsSettings`, `EngineSettings`, `VoicesSettings`,
  `ExperimentsSettings`, `SmsSettings`, plus `GuardRailConfig`; all `extra="forbid"` (except GuardRailConfig
  `extra="allow"`), every field `Field(default=…, description=…)`. Module constants and helpers:
  - `SETTINGS_GROUPS: Final[frozenset[str]]` (the 12 group field names),
  - `COMPLIANCE_PATHS: Final[frozenset[str]]` (top-level names and group names from Decision 5),
  - `merge_settings(stored: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]` (pure; groups merged
    key-wise, rest replaced; never mutates inputs),
  - `sent_paths(patch: Mapping[str, Any]) -> list[str]` (dotted paths: `timezone`, `compliance.calling_hours`),
  - `needs_compliance(patch) -> bool` (any sent path whose first segment is in `COMPLIANCE_PATHS`),
  - `load_settings(stored: Mapping[str, Any]) -> OrgSettings` (validate stored sparse JSON),
  - `field_path(loc: tuple[int | str, ...]) -> str` (pydantic loc → `compliance.calling_hours[0].end`; used by T12b to
    build `INVALID_INPUT` fields),
  - `__all__`.
- **tests/** in packages/va_domain: `test_org_settings_defaults.py` parses specs/04 §7 (both tables, plus the
  Decision 2 additions listed in a pinned dict) and asserts each default equals `OrgSettings()` at that path (table
  `Default` column text mapped by a small parser: `true/false/null/[]/{}`, numbers incl. `5 000 000`, quoted strings,
  backticked enums); the replaced `calling_hours` row must be gone from the table; every model path appears in the
  table or the pinned additions and vice versa. `test_org_settings_validation.py`: parametrized invalid values for each
  rule of Decision 4 → `ValidationError` with the expected `field_path`; extra keys rejected at top level and in each
  group; `GuardRailConfig` unknown type rejected, extra keys kept; `ip_allowlist` normalization; timezone; overlap.
  `test_org_settings_merge.py`: merge semantics (groups key-wise, lists/maps replaced, null, inputs unmutated),
  `sent_paths`, `needs_compliance` true/false cases, `OrgSettings().model_dump(mode="json")` round-trips through
  `load_settings`, a sparse stored `{"mfa_required": true}` loads with all other defaults.
  `test_voices.py` / `test_scheduling.py` for `VoiceSettings` bounds and `CallingWindow`.
  Reuse a markdown-table parser if one exists (apps/api/tests test_rbac_matrix / test_permissions parse spec tables —
  move a shared helper to `scripts/` or va_common test support instead of copying; jscpd).
- **Root pyproject.toml**: dev group, sources, mypy paths/files; `uv lock`.
- **specs/04 §7**: replace the `calling_hours` row, add the Decision block; pointer notes in specs/23 §3/§6, specs/08,
  specs/10; specs/02 nothing (package already listed).
- **AGENTS.md**: `va_domain` exists; org settings stored sparse; parse PATCH bodies with `OrgSettings` +
  `exclude_unset`.

### Gates
`make fmt lint typecheck placeholders test`; `uv run python scripts/check_spec_refs.py`; `npx jscpd@4` (0 clones);
radon `-n C` none on `packages/va_domain/src`; `pip-audit` (tzdata/email-validator) + gitleaks. No openapi change
expected (no route touches the model yet) — run `make openapi` + drift check to confirm. build.done PLAIN STRING:
`ralph emit "build.done" "P02-T12a tests: pass (N passed), lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Coverage gate is per run (`--cov-fail-under=85` overall) but the master prompt wants ≥ 85 % per package — aim for
  ~100 % in va_domain.
- The §7 default-parser test is brittle on prose cells (`Karen` with a parenthetical, the long quoted messages, the
  `5 000 000` thin spaces): parse only the leading backticked/quoted token and keep a small explicit override map for
  the few prose cells rather than loosening the comparison.
- `tzdata` must be a runtime dependency (slim containers lack /usr/share/zoneinfo).
- Do not add model validators that compare two fields across groups — a partial PATCH body is parsed with the same
  model in T12b. `CallingWindow` (single object) and the windows-overlap check (single field) are fine.

## 2026-10-29 Builder: P02-T12a build notes
- New `packages/va_domain` (dist `va-domain`; deps pydantic, email-validator, tzdata): `voices.VoiceSettings`
  (specs/15 §5), `scheduling.CallingWindow` / `Weekday` / `ALL_DAYS` / `windows_overlap` / `unique_items`,
  `org.OrgSettings` + 11 group sub-models + `GuardRailConfig`, `SETTINGS_GROUPS`, `COMPLIANCE_PATHS`, `merge_settings`,
  `sent_paths`, `needs_compliance`, `load_settings`, `sparse_settings` (added: `model_dump(mode="json",
  exclude_unset=True)` = normalized sparse form to store), `field_path` (drops pydantic's `[key]` segment).
- Deviations from the plan, recorded in the specs/04 §7 Decision:
  - Groups are 11, not 12 (compliance, llm, privacy, redaction, analysis, memory, tools, engine, voices, experiments,
    sms); the test derives the set from the model.
  - `notification_emails` does not use `EmailStr`: it rejects `.test` domains, which every fixture and the dev seed use.
    Uses `email_validator.validate_email(check_deliverability=False, test_environment=True).normalized` instead.
  - Spoken messages and `summary_prompt_default` need ≥ 1 character; `max_attempts_per_number_per_day` capped at 100.
- specs/04 §7: top-level `calling_hours` row removed, `compliance.calling_hours` row added (JSON default),
  `analysis.retranscribe` row → `bool | null`/null, `llm.context_budget_tokens` → 1 000–32 000, `sms: SmsSettings` in
  the Python layout, Decision block "OrgSettings fields (P02-T12a)" items 1–5. Pointer Decisions in specs/08 §8,
  specs/10 §6, specs/23 §3 and §6. AGENTS.md entry.
- Tests (236, 100 % coverage of va_domain): defaults parsed from both §7 tables (+ pinned additions, each must be named
  in §7), model paths == spec paths, every field has a description; 80+ invalid cases with expected `field_path`;
  extra keys per group; merge/sparse/compliance helpers; COMPLIANCE_PATHS compared with the Decision text.
Gates: fmt/lint/typecheck pass; placeholders 0; make test 3231 passed, coverage 97.87% (logs/p02-t12a-test.log);
spec refs 0; openapi drift current (no change); jscpd 0 clones; radon -n C none in va_domain, avg 2.0037 (apps+packages
src); pip-audit (uv export --no-emit-workspace, --no-deps --disable-pip) + pnpm audit no vulns; gitleaks passed.

## Review P02-T12a (2026-10-29): approved
- Gates re-run: make lint typecheck placeholders pass (0 findings); make test 3231 passed, coverage 97.86% (va_domain
  100%) (logs/review-t12a-test.log); check_spec_refs 0; jscpd 0 clones; radon -n C none in va_domain.
- Checked OrgSettings against specs/04 §7 tables row by row (defaults test parses both tables + pinned additions),
  VoiceSettings vs specs/15 §5, CallingWindow vs specs/17 §3; Decision block covers all conflicts and additions.
- Spot checks: invalid timezone / overlapping windows / bad dict key / bad CIDR / null group produce the expected
  field paths; CIDR + email normalization in sparse_settings; full dump round-trips through load_settings.

## 2026-10-29 Planner: P02-T12a approved → next P02-T12b

## Plan P02-T12b — `GET/PATCH /v1/org/settings`, `settings` on `PATCH /v1/org`, `OrgSettingsReader` (specs/04 §3.2, §7, §8.1, §13; specs/03 §11; specs/31)

### Facts found while planning
- `apps/api/src/va_api/routers/org.py`: `org_get`/`org_update` on `org_router` (from `org_sso`), `ReadOrg`/`WriteOrg`
  aliases, `_COMPLIANCE_FIELDS` check via `holds_permission(ctx, "org.compliance")`, audit via `changed(before, after)`
  + `audit(session, ctx, "org.update", "org", ctx.org_id, before=, after=)` inside the global session.
  `OrgView.settings: dict[str, Any]` (org_schemas.py:179) returns `dict(org.settings)`; `OrgPatch` has `name`,
  `retention_days`, `recording_retention_days`, `extra="forbid"`.
- `OrgRepo.update(org_id, *, name, retention_days, recording_retention_days)` (va_db/repositories/orgs.py) — no
  settings argument, no row lock. `MembershipRepo.lock_org()` shows the `select(Org.id)...with_for_update()` pattern.
- RBAC: `org:write` = owner, admin only (developer lacks it); `org.compliance` = owner, admin; API keys hold it only
  with `*`. Viewer/analyst/billing/developer hold `org:read`.
- Session principals are always `principal_type="user"` (session_auth.py:312); `platform_admin` exists only for
  `/internal/admin/*` (specs/04 §1). So on `/v1/*` nobody can set `baa_signed_at` today; tests seed it with
  `session_helpers.set_org(identity, org_id, settings={...})`.
- `ctx.plan_id` is the child's plan when a parent acts on a sub-account (P02-T09b).
- Request-validation `field` today is `".".join(loc)` (errors.py `_field`) → `compliance.calling_hours.0.end`, while
  `va_domain.org.field_path` renders `compliance.calling_hours[0].end` and drops pydantic's `[key]` segment.
- va_db depends on `va-common` (which depends on `redis>=5.1`) but not on `redis` or `va-domain` directly.
- `test_openapi.py` requires `examples` on every request-body and response model schema (line 84, 257-263).
- Redis cache precedent: `ApiKeyAuthenticator._cached/_store` (api_key_auth.py:186-204): `(RedisError, OSError)` →
  structlog warning + DB fallback; bad cached JSON → warning + DB.

### Decisions to record (specs/04 §8.1, one `> Decision (2026-10-29):` block "Org settings endpoints (P02-T12b)";
specs/03 §11 key row; specs/31 note on field path format; §13 note on the audit shape)
1. **Routes.** `GET /v1/org/settings` (`org:read`, `operation_id="org_settings_get"`) and `PATCH /v1/org/settings`
   (`org:write`, `org_settings_update`, `audited("org.update")`) return `{data: OrgSettings}` with every field
   (defaults filled). `PATCH /v1/org` accepts `settings?: partial OrgSettings` with the same semantics; `GET /v1/org`
   and `PATCH /v1/org` return `settings` with defaults filled. Both PATCH bodies are parsed as `OrgSettings` and only
   the sent keys (`exclude_unset`) are applied; the response of an empty settings body is the current settings.
2. **Merge and storage.** As specs/04 §7 Decision 5: groups merge key by key, everything else is replaced, `null` only
   on nullable fields; `orgs.settings` stores `sparse_settings(OrgSettings.model_validate(merge_settings(stored,
   sent)))`. The org row is locked (`SELECT … FOR UPDATE`) for the read-merge-write so concurrent PATCHes never lose
   keys.
3. **Permissions.** Sending any path whose first segment is in `COMPLIANCE_PATHS` (and `retention_days` /
   `recording_retention_days` as before) needs `org.compliance` → `FORBIDDEN` "Missing required permission
   'org.compliance'." Sending `baa_signed_at` (any value) as anything but a `platform_admin` principal → `FORBIDDEN`
   "Only platform administrators can set 'baa_signed_at'." (field `baa_signed_at` / `settings.baa_signed_at`); on
   `/v1/*` that is every caller until the admin console lands. Checks run on what is sent, before any write.
4. **Cross-field rules**, checked on the merged object, and only when the body sends one of the rule's paths (so an
   unrelated PATCH never fails because of state set elsewhere): `hipaa_mode` true with `baa_signed_at` null →
   `INVALID_INPUT` "hipaa_mode requires a signed BAA ('baa_signed_at')." field `hipaa_mode`; `privacy.zero_retention`
   true on a plan other than `enterprise` (`ctx.plan_id`) → `FEATURE_NOT_IN_PLAN` (403) "Zero retention requires the
   enterprise plan." field `privacy.zero_retention`. Under `PATCH /v1/org` fields get the `settings.` prefix.
5. **Field paths.** One format for every `INVALID_INPUT` `field` in the API: `va_domain.org.field_path` (dotted keys,
   `[i]` list indices, no `[key]` segment), after dropping the location (`body`, `query`, …). specs/31 gets a note.
6. **Accessor.** `va_db.org_settings.OrgSettingsReader(database, redis)`: `get(org_id) -> OrgSettings | None` reads
   Redis `orgsettings:{org_id}` (string JSON of the stored sparse object, `EX 60`) then the DB (`OrgRepo.get` on the
   global session), stores on miss, never caches a missing org; Redis errors and invalid cached JSON are logged
   (`org_settings_cache_unavailable` / `org_settings_cache_invalid`) and fall back to the DB; `invalidate(org_id)`
   deletes the key (Redis errors logged, not raised) and is called by every settings write after its transaction
   commits. Other services use this reader; the API's `GET /v1/org/settings` reads through it.
7. **Audit.** One `org.update` row per PATCH that changes something: `before`/`after` keyed by `name`,
   `retention_days`, `recording_retention_days` and `settings.<dotted path>` for each sent settings path whose
   effective value (defaults filled, JSON mode) changed; group-level paths for whole-replaced values
   (`settings.compliance.calling_hours`, `settings.voice_overrides`). No row when nothing changed.

### Files
- **packages/va_db/pyproject.toml**: add deps `va-domain`, `redis>=5.1` (+ `[tool.uv.sources] va-domain = {workspace
  = true}`); `uv lock`.
- **packages/va_db/src/va_db/org_settings.py** (new): `CACHE_TTL_S: Final = 60`, `settings_cache_key(org_id: uuid.UUID)
  -> str` (`orgsettings:{org_id}`), `class OrgSettingsReader` with `__init__(self, database: Database, redis: Redis)`,
  `async get(org_id) -> OrgSettings | None`, `async invalidate(org_id) -> None`, private `_cached/_store` (follow the
  ApiKeyAuthenticator shape; do not copy its body verbatim — jscpd). Export from `va_db.__init__` only if the package
  re-exports similar classes; otherwise import from the module.
- **packages/va_db/src/va_db/repositories/orgs.py**: `OrgRepo.lock(org_id) -> Org | None` (`select(Org)...
  with_for_update()`, populate_existing) and `update(..., settings: Mapping[str, Any] | None = None)` (sets the jsonb
  column when not None).
- **packages/va_domain/src/va_domain/org.py**: `OrgSettings.model_config` gains `json_schema_extra={"examples":
  [{"timezone": "America/New_York", "default_record": True, "compliance": {"state_quiet_hours": True}}]}` (OpenAPI
  example requirement); keep `extra="forbid"`. Add helper `effective_values(settings: OrgSettings, paths:
  Iterable[str]) -> dict[str, Any]` (JSON-mode value at each dotted path) for the audit diff, with tests in
  `packages/va_domain/tests/test_org_settings_merge.py`.
- **apps/api/pyproject.toml**: add `va-domain` dependency + source if not already transitive-declared.
- **apps/api/src/va_api/container.py**: `org_settings: OrgSettingsReader` built from `database`, `redis`; plus
  accessor `org_settings_of(request)` next to `database_of` in `auth/session_auth.py` (or wherever the `*_of` helpers
  live). Update `test_container.py` expectations and SessionContainer in tests/support.
- **apps/api/src/va_api/routers/org_settings.py** (new, routes on `org_router`; import-for-side-effect in
  `va_api/main.py`): `SettingsPatchResult` dataclass (`settings: OrgSettings`, `before: dict`, `after: dict`);
  `check_settings_permissions(ctx, sent: Mapping[str, Any], prefix: str) -> None`;
  `async apply_settings_patch(session, ctx, sent, prefix) -> SettingsPatchResult` (lock → load → merge → validate
  (ValidationError → `AppError("INVALID_INPUT", msg, field=prefix + field_path(loc))`) → cross-field → `OrgRepo.update(
  settings=sparse)` → diff); routes `org_settings_get`, `org_settings_update` (after the session closes:
  `await reader.invalidate(ctx.org_id)`). Keep each function radon A/B.
- **apps/api/src/va_api/routers/org.py**: `org_update` passes `body.settings.model_dump(mode="json",
  exclude_unset=True)` (when `"settings" in body.model_fields_set`) through `check_settings_permissions` and
  `apply_settings_patch(prefix="settings.")` inside its existing session; merge its before/after into the one audit
  row; invalidate after commit; `_org_view` uses `load_settings(org.settings).model_dump(mode="json")`.
- **apps/api/src/va_api/routers/org_schemas.py**: `OrgView.settings: OrgSettings`; `OrgPatch.settings: OrgSettings |
  None = None` (description: "Partial OrgSettings; omitted keys stay unchanged; compliance fields need
  `org.compliance`."); update `_ORG_EXAMPLE` (settings with defaults would be huge — keep the example minimal but valid
  against the schema; the contract test only checks presence).
- **apps/api/src/va_api/errors.py**: `_field` → strip location, then `va_domain.org.field_path(tuple(parts))`. Grep
  tests for `.0.`-style expected fields and update them.
- **openapi/**: `make openapi`; then `corepack pnpm gen:api` in `sdks/typescript` and `apps/dashboard`.
- **specs/04 §8.1** Decision block (items 1–7), amend the earlier §8.1 decision sentence "`settings` is the stored
  object until OrgSettings (P02-T12) …" by a pointer to the new block (do not delete history: add "Superseded by the
  P02-T12b decision below"); **§13** one-line note on settings audit keys; **specs/03 §11** new row:
  `orgsettings:{org_id}` | string JSON (stored sparse `orgs.settings`) | 60 s | Org settings cache (specs/04 §7);
  deleted on every settings write. **specs/31** note: `field` format.
- **AGENTS.md**: `OrgSettingsReader` usage (read settings through it outside the API; call `invalidate` after any
  write commits); field path format.

### Tests
- **packages/va_db/tests/test_org_settings_reader.py** (identity_db fixture + `fakeredis.FakeAsyncRedis`): miss reads
  DB, returns defaults-filled model for `{}` and stores the sparse JSON with TTL 60 (`ttl` ≤ 60, > 0); hit returns
  cached even after a direct DB change; `invalidate` → next `get` sees the DB value; Redis down
  (`FakeServer(); server.connected=False`) → `get` reads DB, `invalidate` does not raise, warning logged; invalid cached
  JSON / invalid cached settings → DB fallback + warning; unknown org → `None` and nothing cached.
- **packages/va_db/tests/test_repositories.py** (or orgs test module): `OrgRepo.update(settings=…)` writes jsonb,
  `lock` returns the row / None.
- **apps/api/tests/test_org_settings_integration.py** (conftest `client/identity/container`, `session_helpers.
  signed_in`, `error_of`, `set_org`, `stored_org`):
  - GET on a fresh org == `OrgSettings().model_dump(mode="json")`; GET `/v1/org` `settings` same.
  - PATCH `{"timezone": "America/New_York"}` → 200 full settings; `stored_org(...).settings == {"timezone": …}`.
  - group merge key-wise (two PATCHes to `llm.*`), list replace (`default_webhook_events`), `voice_overrides` replace,
    `default_from_number: null` accepted, `timezone: null` → 400 field `timezone`.
  - invalid values → 400 `INVALID_INPUT` with fields: `timezone`, `compliance.calling_hours[0].end`, `ip_allowlist[0]`;
    same through `PATCH /v1/org` with `settings.` prefix; unknown key → field `bogus` / `llm.bogus`.
  - normalization stored (CIDR `10.0.0.1/8` → `10.0.0.0/8`).
  - permission matrix (parametrized roles): GET allowed for all six roles; PATCH general field: owner/admin 200,
    developer/analyst/billing/viewer 403; compliance field (`mfa_required`, `compliance.state_quiet_hours`): owner/admin
    200; API key with `org:write` only → 403 "Missing required permission 'org.compliance'."; key with `*` → 200.
  - `baa_signed_at` sent by owner → 403 FORBIDDEN with the message; `hipaa_mode: true` without BAA → 400 field
    `hipaa_mode`; with `baa_signed_at` seeded via `set_org` → 200; unrelated PATCH with hipaa on and BAA removed still
    200.
  - `privacy.zero_retention: true` on build plan → 403 `FEATURE_NOT_IN_PLAN` field `privacy.zero_retention`; on
    enterprise (`set_org(plan_id="enterprise")`) → 200.
  - audit: one `org.update` row with `before={"settings.timezone": "America/Los_Angeles"}`, `after={…}`; PATCH /v1/org
    with name + settings → one row with both keys; no-op PATCH → no row.
  - cache: GET (populates `orgsettings:{id}`), PATCH, GET returns new value and key was deleted/refreshed; Redis
    connection failure on the container (fakeredis disconnected server) → PATCH and GET still 200.
  - sub-account: parent acting via `X-VA-Org-Id` updates the child's settings, parent unchanged.
  - existing readers keep working: PATCH `{"mfa_required": true}` → stored raw `true` → `MembershipLookupRepo`
    reports `mfa_required` (reuse an existing MFA-gate assertion helper).
- **apps/api/tests/test_errors.py**: list index rendered `items[0].name`, dict `[key]` dropped, location stripped.
- **test_openapi / test_audit_markers / test_operations_contract** pass with the new routes (x-scope, audited marker,
  examples).

### Gates
`make fmt lint typecheck placeholders test` (+ `make test-ts` after `pnpm gen:api`); `make openapi` + drift check;
`uv run python scripts/check_spec_refs.py`; `npx jscpd@4` 0 clones; radon `-n C` none in changed src; pip-audit per
mem-1793288415-240d; gitleaks. build.done PLAIN STRING: `ralph emit "build.done" "P02-T12b tests: pass (N passed),
lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Changing `errors._field` changes every route's list-index format: grep `apps/api/tests` for `"\.[0-9]+\.` field
  expectations and update; mention in specs/31.
- Pool size 1 per role: never open the reader's global session while the route's global session is open (call
  `invalidate` only, not `get`, inside/after writes; `get` opens its own session).
- `OrgView.settings: OrgSettings` makes `GET /v1/org` fail loudly if a stored row is invalid — seed/fixtures that write
  arbitrary settings (grep `settings=` in tests) must use valid keys.
- OpenAPI schema grows a lot (OrgSettings + sub-models); regenerate TS types or `schema.test.ts` fails.
- Do not put the cross-field rules in model validators (partial bodies share the model).

## 2026-10-29 Builder: P02-T12b build notes
- Implemented per plan. New: `va_db.org_settings.OrgSettingsReader` (+ `settings_cache_key`, `CACHE_TTL_S`),
  `OrgRepo.lock` + `update(settings=)`, `va_api.routers.org_settings` (GET/PATCH `/v1/org/settings`,
  `check_settings_permissions`, `apply_settings_patch`, `SettingsPatchResult`), `session_auth.org_settings_of`,
  `Container.org_settings`, `va_domain.org.effective_values` + OrgSettings OpenAPI example, `errors._field` via
  `field_path`. `PATCH /v1/org` takes `settings` (`OrgSettings = default_factory`, so `settings: null` → 400 field
  `settings`); `OrgView.settings: OrgSettings`. `ORG_NOT_FOUND_MESSAGE` now lives in `org_settings` (re-exported by org.py).
- Deviation found by tests: stored settings that no longer validate made the audit "before" diff raise (500). Fixed with
  `_previous_values` (raw stored value / default per path); the PATCH reports the bad stored field as INVALID_INPUT unless
  it sends a valid replacement, which repairs it (test_invalid_stored_settings_are_reported_and_repaired_by_a_patch).
- An empty group object (`{"privacy": {}}`) sends nothing: no permission check, no write (recorded in Decision item 1).
- Specs: 04 §8.1 Decision "Org settings endpoints (P02-T12b)" items 1-7 + supersede pointer; §13 audit sub-bullet;
  03 §11 `orgsettings:{org_id}` row; 31 Decision "Validation field format". AGENTS.md entry.
- Coverage note: lines after `await repo.lock(...)` in apply_settings_patch report uncovered (same greenlet tracing
  artefact as org.py 178-180); they are exercised (storage + audit assertions depend on them).
Gates: fmt/lint/typecheck pass; placeholders 0; make test 3279 passed, coverage 97.69% (logs/p02-t12b-test.log);
make test-ts pass; openapi regenerated (+ gen:api in sdks/typescript, apps/dashboard), drift re-run unchanged;
check_spec_refs 0; jscpd 0 clones; radon -n C none in changed src, avg 2.0173 (apps+packages src); pip-audit + pnpm
audit no vulns; gitleaks (pre-commit) passed.

## Review P02-T12b (2026-10-29): approved
- Re-ran: lint, typecheck, placeholders (0), make test 3279 passed / 97.69%, make test-ts pass, make openapi stable
  (no drift on re-run), check_spec_refs 0.
- Diff checked against specs/04 §7 (hipaa_mode needs baa_signed_at; baa set by platform admin; zero_retention
  enterprise-only → FEATURE_NOT_IN_PLAN 403 per specs/31), §8.1 decision, §13 audit keys, specs/03 §11 key row.
  Row lock for read-merge-write, invalidate after commit, Redis failures logged with DB fallback, tests cover
  permission matrix, cross-field rules, audit, cache, sub-account, invalid stored repair.
- Note (not blocking): a stored row that fails OrgSettings validation would 500 on GET via the reader/OrgView; writes
  always validate, so only out-of-band DB edits can produce one.

## 2026-10-29 Planner: P02-T12b approved → next P02-T12c

## Plan P02-T12c — Enforce `ip_allowlist` and `session_max_age_days` (specs/04 §2.2, §4.3, §7)

### Facts found while planning
- `get_principal` (auth/dependencies.py) = API key (`_api_key_principal`, which schedules `touch_if_due` before any
  further check) or `authenticate_session` → `act_on_sub_account` → rate limit. `_api_key_principal` passes the raw
  `request.client.host` as `ctx.ip`; sessions/audit use `session_auth.client_ip` (None for non-IP peers).
- `SessionManager.resolve` applies `next_expiry` (fixed 14 d window) before the active org is known; it is built in
  `container.py:130` and `tests/support/session_helpers.py:156` (SessionContainer, before `org_settings` is set).
- `OrgSettingsReader.get(org_id)` (Redis `orgsettings:{id}` 60 s, invalidated on every settings write) opens its own
  global session on a miss; `OrgSettings.ip_allowlist` is normalized CIDR strings, `session_max_age_days` int 1–90.
- uvicorn (`va_common.service.run_server`) runs with uvicorn's default `proxy_headers=True`, trusting
  `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS` (uvicorn env, default 127.0.0.1); so `request.client.host` is the
  real client once the deployment trusts its load balancer. No new app env var.
- No IP-specific code in specs/31 → reuse `FORBIDDEN` (403).
- httpx `ASGITransport` peer is fixed ("127.0.0.1", 123); tests need a way to vary it.

### Decisions to record (specs/04 §7, one `> Decision (2026-10-29):` block "Enforcing ip_allowlist and
session_max_age_days (P02-T12c)"; a one-line pointer under §2.2 and §4.3)
1. **Client address**: the ASGI peer (`session_auth.client_ip`); uvicorn rewrites it from `X-Forwarded-For` only for
   proxies listed in uvicorn's `FORWARDED_ALLOW_IPS` (the Helm chart sets it to the load balancer ranges). An
   IPv4-mapped IPv6 peer (`::ffff:a.b.c.d`) is matched as IPv4. A peer that is not an IP address never matches a
   non-empty list (fail closed). `ctx.ip` of API-key principals uses `client_ip` too.
2. **Where**: in `get_principal`, i.e. every route authenticated by API key or session (all `/v1/*`). Checked right
   after authentication (before `X-VA-Org-Id`, rate limiting, scopes, idempotency, and before the API key's
   `last_used_at` touch is scheduled), against the allowlist of every org in `{ctx.org_id, ctx.acting_parent_org_id}`
   (a session acting in a child through a parent: both); after `X-VA-Org-Id` switches to a child, the child's list is
   checked too. Denial: `FORBIDDEN` "Requests from this IP address are not allowed for this organization."; warning log
   `ip_not_allowed` (org_id, ip). `/internal/auth/*` session routes (session, logout, switch-org, MFA, password) are not
   gated so a user can still see their memberships, switch to another org, or log out.
3. **Self-lockout guard**: a settings PATCH (`/v1/org/settings` or `settings` on `PATCH /v1/org`) that sends a
   non-empty `ip_allowlist` not containing the caller's address → `INVALID_INPUT` "ip_allowlist must include the
   address of this request (<ip>)." field `ip_allowlist` / `settings.ip_allowlist` (caller address unknown → message
   "ip_allowlist cannot be set from a request without a client address."). Applies only when acting directly in the org
   (not when a parent acts on a child via `X-VA-Org-Id` / parent session: then the parent's own access is unaffected
   unless the child's list excludes it, which is the point of a child list — guard still applies there too for
   simplicity: always checked against the caller address). Keep: always check.
4. **Session window**: the sliding window of a session is `session_max_age_days` of `sessions.active_org_id`
   (14 d when NULL, the org is gone, or no reader). On each resolve the target is `min(now + window, created_at +
   90 d)`; it is written when it is more than 1 h past `expires_at` (extend) **or** earlier than `expires_at`
   (a shorter window, e.g. a lowered setting or switching into a stricter org: the session then expires `window` after
   this request). Expiry is checked against the stored `expires_at` before the new window applies, so a lowered window
   takes effect from the session's next request (no last-activity column exists to expire idle sessions
   retroactively). Login keeps `now + 14 d` (the first request in the org applies its window). A session acting in a
   child through a parent uses the child's setting (the active org).

### Files
- **apps/api/src/va_api/auth/ip_allowlist.py** (new): `IP_NOT_ALLOWED_MESSAGE: Final`, `def ip_allowed(ip: str | None,
  networks: Sequence[str]) -> bool` (empty → True; None → False; `ipaddress.ip_address`, unwrap `ipv4_mapped`;
  `any(addr in ip_network(n) for n in networks)`), `async def enforce_ip_allowlist(request: Request, ctx:
  RequestContext) -> None` (orgs `dict.fromkeys(o for o in (ctx.org_id, ctx.acting_parent_org_id) if o)`; settings via
  `session_auth.org_settings_of(request).get(org_id)`; None settings → skip). Keep radon A/B.
- **apps/api/src/va_api/auth/dependencies.py**: `_api_key_principal` returns `(ctx, rate_per_sec, key_id)` without
  scheduling the touch and uses `client_ip(request)` for `ip`; `get_principal`: authenticate → `enforce_ip_allowlist`
  → schedule touch (API key only) → `act_on_sub_account` → if `acting.org_id != ctx.org_id`: `enforce_ip_allowlist`
  again → bind log vars → rate limit. Update the docstring. Do NOT import va_api.auth from rate_limit (mem-...a730 rule).
- **apps/api/src/va_api/auth/sessions.py**: `next_expiry(*, created_at, expires_at, now, window: timedelta =
  SLIDING_TTL)` (extend or shorten per Decision 4; update module docstring); `SessionManager.__init__(..., org_settings:
  OrgSettingsReader | None = None)`; `async def _window(self, org_id: uuid.UUID | None) -> timedelta`; `resolve`
  passes `await self._window(active.active_org_id)` (only after the expiry check; never inside an open DB session —
  pool size 1).
- **apps/api/src/va_api/container.py**: build the reader once and pass it to `SessionManager(..., org_settings=reader)`
  and `org_settings=reader`. **tests/support/session_helpers.py** SessionContainer: create `org_settings` before
  `sessions` and pass it.
- **apps/api/src/va_api/routers/org_settings.py**: in `check_settings_permissions` (or a new small
  `check_ip_allowlist_keeps_caller(request_ip, sent, prefix)` called next to it from both routes) the self-lockout guard
  (Decision 3); use the normalized value (`Cidr` normalization — validate through `OrgSettings.model_validate(
  {"ip_allowlist": sent["ip_allowlist"]})` or reuse the merged object's `ip_allowlist` after validation in
  `apply_settings_patch`; simplest: do it in `apply_settings_patch` on the validated merged settings when
  `"ip_allowlist" in sent`, with `ip` passed in). Wire through `routers/org.py` too.
- **apps/api/tests/support/peer.py** (new): `class PeerSwitch` — ASGI wrapper `(app)` with mutable `host: str |
  None`; for `http` scopes with `host` set it passes `{**scope, "client": (host, 50000)}`; lifespan passes through.
  `session_client` accepts any ASGI app (widen the type) so `session_client(PeerSwitch(session_app(container)))` works.
  Export via session_helpers if that is the pattern.
- **specs/04** §7 Decision block + pointers in §2.2/§4.3; **AGENTS.md**: PeerSwitch test helper; client IP is the
  ASGI peer (uvicorn `FORWARDED_ALLOW_IPS` behind a proxy); `SessionManager` window from OrgSettingsReader.

### Tests
- **apps/api/tests/test_ip_allowlist.py** (unit): empty list allows anything incl. None; IPv4 inside/outside `/24`;
  `/32` host entry; IPv6 inside/outside `2001:db8::/32`; `::ffff:203.0.113.7` matches `203.0.113.0/24`; v4 address vs
  v6-only list → False; None or non-IP with non-empty list → False.
- **apps/api/tests/test_sessions_window.py** or extend `test_session_lifetime.py`: `next_expiry` with window 1 d
  shortens a 14 d expiry to now+1 d; window 30 d extends past 14 d; within-1 h no write for both; 90 d cap still wins;
  default arg == old behaviour (existing tests unchanged).
- **apps/api/tests/test_ip_allowlist_integration.py** (conftest fixtures + PeerSwitch client fixture in the module):
  - API key: org list `["203.0.113.0/24"]` (via `set_org(settings=...)` + reader invalidate, or PATCH from an allowed
    peer): `GET /v1/me` from 203.0.113.7 → 200, from 198.51.100.9 → 403 FORBIDDEN with the message; empty list → 200
    from any peer; non-IP peer (`testclient`) with a list → 403; denied request does not set
    `apikey:{<id>}:touched` (touch not scheduled) and leaves no idempotency row for a POST with Idempotency-Key.
  - session: allowed 200 / denied 403 on `/v1/me`; `GET /internal/auth/session` and `POST /internal/auth/logout`
    still 200 from the denied peer.
  - IPv6 peer allowed by an IPv6 network.
  - PATCH `/v1/org/settings` changing the list takes effect on the next request (cache invalidated).
  - self-lockout: PATCH list excluding the caller → 400 INVALID_INPUT field `ip_allowlist`; same via `PATCH /v1/org`
    → field `settings.ip_allowlist`; list including caller → 200; `[]` always 200.
  - sub-accounts: parent key + `X-VA-Org-Id` child whose list excludes the peer → 403; parent list excludes peer →
    403 before the header is looked at (even for a non-child id); session acting in child via parent where parent list
    excludes → 403.
- **apps/api/tests/test_session_window_integration.py**: org `session_max_age_days=1` (set_org settings): login,
  advance FrozenClock 2 h, `GET /v1/me` → stored `sessions.expires_at == now + 1 d` and `sess:` cache entry matches;
  advance 1 d + 1 s → 401 AUTH_FAILURE; org with 30: activity after 2 h → `now + 30 d`, advance 29 d → 200; default
  org stays 14 d; lowering 14 → 1 via PATCH then next request → expires now+1 d; switch-org from a 14 d org into a 1 d
  org applies 1 d on the next request; absolute 90 d cap still enforced with 90-day window.
- Existing suites (`test_session_lifetime`, session/MFA/sub-account integration, test_container) keep passing.

### Gates
`make fmt lint typecheck placeholders test` (+ `make test-ts` only if openapi changes — it should not); `make openapi`
drift check (no change expected); `uv run python scripts/check_spec_refs.py`; `npx jscpd@4` 0 clones; radon `-n C`
none in changed src; gitleaks. build.done PLAIN STRING: `ralph emit "build.done" "P02-T12c tests: pass (N passed),
lint: pass, typecheck: pass, audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: `_window` / `enforce_ip_allowlist` call `reader.get`, which opens a global session on a miss;
  never call them while a route/session-manager DB session is open (grep callers of `sessions.resolve` /
  `current_session` inside `async with ...session()` blocks).
- An existing test fixture writing `settings` with a non-empty `ip_allowlist` or small `session_max_age_days` would now
  be enforced — grep tests for both keys.
- Rate-limit/idempotency ordering tests (`test_dependencies*`) may assert touch scheduling order; adapt, don't weaken.
- `next_expiry` shortening must not fire for the 90 d cap (target == cap ≥ expires_at always) — unit test it.

## 2026-10-29 Builder: P02-T12c build notes
- Implemented per plan: `va_api.auth.ip_allowlist` (`ip_allowed`, `enforce_ip_allowlist`, `IP_NOT_ALLOWED_MESSAGE`);
  `get_principal` checks the allowlist after authentication, before scheduling the API key touch, and again for the
  child after `X-VA-Org-Id`; API-key `ctx.ip` now `client_ip(request)`. `next_expiry(..., window=)` extends or
  shortens; `SessionManager(org_settings=)` + `_window`; container/SessionContainer share one reader. Self-lockout
  guard `_check_keeps_caller` in `apply_settings_patch(..., ip=)` (both PATCH routes), `IP_ALLOWLIST_NO_CLIENT_MESSAGE`.
- Deviation found by the full suite: stored settings that fail validation made every authenticated request of the org
  500 (the reader raises), including the repair PATCH. Added `OrgSettingsReader.get_lenient` + `va_db.org_settings.
  valid_part` (drops invalid top-level keys, warning `org_settings_invalid` with key names); both gates use it, so a
  valid allowlist stays enforced and a corrupt org can still be repaired (tests in va_db reader suite +
  test_ip_allowlist_integration). Recorded in the module docstring; spec Decision item 2 says "a stored list that no
  longer validates reads as empty" via the code docstring (spec text: orgs that no longer exist skipped).
- Test support: `tests/support/peer.py` PeerSwitch; `auth_helpers.MemoryOrgSettings` + `OrgSettingsSource` Protocol on
  the DB-less AuthContainer (orgs read as gone → gate skipped). test_org_settings_integration normalization test now
  includes 127.0.0.1 (self-lockout guard).
- OpenAPI regenerated (org_settings_update description) + gen:api in sdks/typescript and apps/dashboard.
Gates: fmt; lint pass; typecheck pass; placeholders 0; make test 3328 passed, coverage 97.75%
(logs/p02-t12c-test.log; only docstring wording changed after it, reader suite re-run 15 passed); make test-ts pass
(52 + 48); make openapi stable on re-run; check_spec_refs 0; jscpd 0 clones; radon -n C none in changed src, avg
2.0324; gitleaks not installed locally (pre-commit runs it at commit).

## Review P02-T12c (2026-10-29): approved
- Re-ran: lint, typecheck, placeholders (0), make test 3328 passed / 97.75%, make test-ts pass, make openapi no drift,
  check_spec_refs 0, jscpd 0 clones (api src/tests, va_db src).
- Diff checked against plan Decisions 1-4 and specs/04 §2.2/§4.3/§7: allowlist checked after auth and before touch,
  X-VA-Org-Id, rate limit, idempotency; child re-checked after switch; parent checked for acting sessions; FORBIDDEN
  per specs/31; self-lockout guard on both PATCH routes with correct field prefix; next_expiry shortens/extends and the
  90 d cap cannot trigger a shorten; reader shared by SessionManager and container; no nested DB sessions (reader opens
  its own global session only outside resolve's sessions).
- Note (not blocking): an org whose stored settings fail validation is re-loaded from the DB on every gated request
  (get raises before caching the lenient value); only out-of-band edits produce this, and a PATCH repairs it.
- P02-T12 parent ticked (T12a/b/c all done).

## 2026-10-29 Planner: P02-T12c approved → next P02-T13 (deps P02-T10 ticked)

## Plan P02-T13 — Agent onboarding API (specs/04 §10, §2.4 public allowlist, §13, §14 "Agent onboarding"; specs/03 §11; specs/25 `/agent-onboarding/[token]`; specs/31)

### Facts found while planning
- §10 only names `POST /v1/agent-onboarding/keys` and `GET /v1/agent-onboarding/keys/{request_id}`; the dashboard page
  `/agent-onboarding/[token]` (specs/25, session) needs server routes to read/approve/deny → add them under
  `/internal/auth/agent-onboarding/*` (envelope, `PUBLIC_MARKER` x-public session-flow like invitations), using
  `current_session` (CSRF + Origin already enforced for writes there).
- Public `/v1` routes carry `openapi_extra={"x-scope": "public"}` (health) and no `require_scope`, so no per-key rate
  limit/idempotency applies; test_openapi requires `x-scope` and examples on every 2xx and request body model.
- Fixed-window counter pattern exists in `ResendLimiter.hit` (auth/verification.py); opaque tokens via
  `va_common.auth.issue_token/looks_like_token/tokens_equal/sha256_hex`; emails via `va_common.email.templates`
  (`_Copy`, `TEMPLATES`) + `verification.deliver/dashboard_link`; key minting via `auth.api_keys.mint_api_key`,
  `ApiKeyRepo.add`; org-key encryption `Envelope.encrypt_str/decrypt_str(org_id, …, aad)`; audit via
  `va_api.audit.audit(session, ctx, …)` with a user `RequestContext`; `memberships_of_user`, `scopes_for_role`.
- Redis key style: SSO state store (auth/sso_state.py) — dataclass `to_json/from_json`, store class raising an
  `…UnavailableError` on RedisError.

### Decisions to record (specs/04 §10, one `> Decision (2026-10-29):` block "Agent onboarding flow (P02-T13)"; new
rows in specs/03 §11; `api_key.create` note in §13 if the §13 decision lists sources)
1. **Routes.** Public: `POST /v1/agent-onboarding/keys` (201) and `GET /v1/agent-onboarding/keys/{request_id}`
   (tag: same family the public doc uses for API keys, "API Keys"; `x-scope: public`, envelope). Dashboard (session):
   `GET /internal/auth/agent-onboarding/{token}`, `POST /internal/auth/agent-onboarding/{token}/approve`,
   `POST /internal/auth/agent-onboarding/{token}/deny`. Add the GET poll to the §2.4 public allowlist text.
2. **Create body** `{email (EmailStr-like validated via email_validator test_environment=True per mem-ea3a),
   agent_name 1–100 (stripped, non-empty), requested_scopes 1–N strings, purpose 0–500}`; `requested_scopes` must be
   known scopes excluding `*`, `billing:read`, `billing:write`, and `api_keys:write` (an agent key must not mint
   keys) → `INVALID_INPUT` field `requested_scopes`; normalized with `normalize_scopes` (dedupe, sort). Response
   `{data:{request_id (UUIDv7), status:"pending_verification", poll_url, poll_secret, expires_at}}`. `expires_at` =
   now + **1 h**: the approval deadline; afterwards a pending request reads `expired` and the link is refused.
   Same response whether or not an account exists for the email (no enumeration).
3. **Limits.** Fixed one-hour windows: 5 per client IP (`agentonb:ip:{ip}`; a request without a client IP counts under
   `agentonb:ip:unknown`), 3 per email (`agentonb:email:{sha256(casefolded email)}`); rejected attempts count; the
   IP is checked before the email; `RATE_LIMITED` with `Retry-After` (seconds to window end). Redis unavailable →
   `SERVICE_UNAVAILABLE` (the request state lives in Redis, so the flow cannot work anyway; fail closed).
4. **State.** `agentreq:{request_id}` JSON, TTL 24 h from creation: `{email_hash, agent_name, purpose, scopes,
   poll_secret_hash, token_hash, status, created_at, expires_at, approver_user_id, org_id, key_id}`;
   `agentreq:token:{token_hash}` → request_id, TTL 1 h (the link lifetime). Statuses: `pending_verification`,
   `approved`, `delivered`, `denied`, `expired` (`expired` is computed: pending past `expires_at`, or `approved` whose
   `agentkey:` entry is gone). Transitions are compare-and-set (WATCH/MULTI on `agentreq:{id}` or a Lua script) so two
   approvals / approve+deny race to exactly one winner; the loser gets `TOKEN_EXPIRED`.
5. **Poll.** Header `X-Poll-Secret` required (missing → `AUTH_FAILURE` "X-Poll-Secret header required."); unknown
   request id, malformed id, or wrong secret (constant-time compare of sha256) → `NOT_FOUND` "Agent key request not
   found." (no oracle). Response `{data:{request_id, status, expires_at, scopes, org_id|null, key|null}}`. When
   `approved`: `GETDEL agentkey:{id}`, decrypt, return `status:"approved"` with `key`, then set status `delivered`;
   later polls → `delivered`, `key:null`. Poll responses `Cache-Control: no-store`.
6. **Approval link** `{DASHBOARD_BASE_URL}/agent-onboarding/{token}` (opaque token, only its hash stored). Token
   unknown/used/past deadline → `TOKEN_EXPIRED` "Agent key request link is invalid or has expired.". The signed-in
   user must own the addressed mailbox: `sha256(casefold(user.email)) == email_hash` and the email verified, else
   `FORBIDDEN` "This request was sent to a different email address." (EMAIL_NOT_VERIFIED if unverified).
   Details: `{request_id, agent_name, purpose, requested_scopes, expires_at, orgs:[{org_id, name, role}]}` where
   orgs = the user's memberships (`memberships_of_user`) with role in owner/admin/developer and gate status `active`.
7. **Approve** body `{org_id, scopes?: string[]}` (default = requested; must be a non-empty subset of requested →
   `INVALID_INPUT` field `scopes`). Org must be one of the eligible orgs → else `FORBIDDEN`; MFA gate as
   `gated_membership` for that org's record (`mfa_due(...)` → `MFA_REQUIRED`); org `ip_allowlist` enforced with
   `ip_allowed(client_ip, settings.ip_allowlist)` via `org_settings_of(request).get_lenient`; developer approvers
   may grant only scopes of their role (`check_grantable` with a user ctx for that org). Effects: CAS pending →
   approving claim; key minted `environment="test"`, `name="agent:<agent_name>"`, `expires_at=now+30 d`,
   `created_by=user_id` in `tenant_session(org_id)` + audit `api_key.create` (actor user, `after` = key view +
   `agent_name`) in the same transaction; then `SET agentkey:{id}` (org-envelope-encrypted, aad
   `agentkey:{request_id}`, base64, TTL 15 min) and state → `approved` (approver_user_id, org_id, key_id); delete the
   token index. On DB failure the claim is rolled back to pending. Response `{data:{request_id, status:"approved",
   org_id, key_id, scopes, expires_at}}` (never the plaintext). Marker `audited("api_key.create")`.
8. **Deny** → state `denied`, token index deleted; `{data:{request_id, status:"denied"}}`;
   `audit_exempt("no org is chosen; the request lives only in Redis")`.
9. Email template `agent_key_request` (agent name, purpose, scope list, link, 1-hour validity) — never includes
   the poll secret.

### Files
- **packages/va_common/src/va_common/email/templates.py**: `AGENT_REQUEST_MINUTES: Final = 60`, `_AGENT_REQUEST` copy,
  `agent_key_request(*, agent_name, purpose, scopes: Sequence[str], link, to) -> EmailMessage`; add to `TEMPLATES`;
  export in `va_common.email.__init__`. Tests in packages/va_common/tests (existing template test module): text+HTML
  contain agent name, each scope, link; HTML escapes `<script>` in agent_name/purpose.
- **apps/api/src/va_api/auth/window_counter.py** (new) — extract the fixed-window logic from `ResendLimiter.hit` into
  `async def hit_window(redis, key, *, limit, window_s, message: Callable[[int], str], fail_open: bool) -> None`
  (fail_open True → log + return on RedisError; False → `AppError("SERVICE_UNAVAILABLE", …)`); `ResendLimiter.hit`
  calls it (behaviour unchanged; existing tests stay green). Avoids jscpd.
- **apps/api/src/va_api/auth/agent_onboarding.py** (new): constants (`APPROVAL_WINDOW = timedelta(hours=1)`,
  `REQUEST_TTL_S = 86400`, `KEY_TTL_S = 900`, `AGENT_KEY_LIFETIME = timedelta(days=30)`, `IP_LIMIT=5`,
  `EMAIL_LIMIT=3`, `WINDOW_S=3600`, `EXCLUDED_SCOPES = frozenset({"*", "billing:read", "billing:write",
  "api_keys:write"})`, messages), key builders `agent_request_key`, `agent_token_key`, `agent_key_key`,
  `agent_ip_key`, `agent_email_key`; `@dataclass AgentRequest` (+ `to_json/from_json`, `effective_status(now,
  key_present)`); `class AgentRequestStore(redis, clock)` with `create(...)`, `get(request_id)`, `by_token(token)`,
  `transition(request_id, from_status, update: Callable[[AgentRequest], AgentRequest]) -> bool` (WATCH/MULTI,
  keeps remaining TTL via `SET … KEEPTTL`), `put_key(request_id, blob)`, `take_key(request_id) -> bytes | None`
  (GETDEL), `drop_token(token_hash)`; RedisError → `AppError("SERVICE_UNAVAILABLE", …)`;
  `check_requested_scopes(scopes) -> list[str]`; `approval_link(base, token)`;
  `async def send_agent_request(sender, *, to, request, token, dashboard_base_url)` (via `deliver`, failure event
  `agent_request_email_failed`); `OnboardingLimiter.hit(ip, email)`.
- **apps/api/src/va_api/routers/agent_onboarding_schemas.py** + **routers/agent_onboarding.py** (public router,
  `envelope_router(prefix="/v1/agent-onboarding", tags=[<API keys tag>])`): create (email goes out after the
  response via BackgroundTasks like signup/verification), poll. Markers: create `{"x-scope": "public",
  **audit_exempt("no org exists yet; the request lives only in Redis")}`; poll `{"x-scope": "public"}`.
- **apps/api/src/va_api/routers/auth_agent_onboarding.py** (+ schemas in the same `_schemas` module): the three
  internal routes on `internal_auth_router` (import-for-side-effect in main.py per mem-6e00... see mem `routers added
  to an existing router from a second module`). Register the public router in main.py/app.state.routers.
- **apps/api/src/va_api/container.py**: `agent_requests: AgentRequestStore`, `onboarding_limiter`; accessors
  `agent_requests_of(request)`, `onboarding_limiter_of(request)` in session_auth.py; add to test containers
  (`tests/support/session_helpers.py` SessionContainer, `auth_helpers` AuthContainer if it must satisfy a Protocol).
- **openapi**: `make openapi` + `corepack pnpm gen:api` in sdks/typescript and apps/dashboard (mem-7e50); add the
  new internal paths to test_openapi `INTERNAL_PATHS` if that set is enumerated.
- **specs/04 §10** Decision block, §2.4 allowlist line (+ GET poll), **specs/03 §11** rows: `agentreq:{request_id}`
  (24 h), `agentreq:token:{token_hash}` (1 h), `agentkey:{request_id}` (15 min, encrypted), `agentonb:ip:{ip}`,
  `agentonb:email:{sha256(email)}` (3600 s). **AGENTS.md**: test helper + flow summary.

### Tests
- **packages/va_common** template test (above).
- **apps/api/tests/test_agent_onboarding_unit.py**: scope check (excluded each → INVALID_INPUT field
  requested_scopes, unknown scope, dedupe/sort, empty list rejected by schema); `AgentRequest` json round trip and
  `effective_status` (pending before/after deadline, approved with/without key); window counter: 5 IP hits ok, 6th
  RATE_LIMITED with Retry-After, fail_open vs fail-closed with `fakeredis.FakeServer(connected=False)` (mem-ea3a…264176);
  ResendLimiter behaviour unchanged.
- **apps/api/tests/test_agent_onboarding_integration.py** (conftest session fixtures; FrozenClock; InMemoryEmailSender):
  - create → 201 body shape, status pending, poll_url, expires_at = now+1 h; email sent to address with link
    containing the token, no poll secret in email; same response for unknown email.
  - full approve flow: signed-in owner (email = request email) GET details lists eligible orgs (not viewer orgs);
    approve with reduced scopes → 200; DB key row env test, name `agent:<name>`, expires now+30 d, created_by user,
    scopes reduced; audit `api_key.create` row actor user with `after.agent_name`; first poll → approved + key that
    authenticates `GET /v1/me` (scope org:read requested) ; second poll → delivered, key null.
  - deny flow → poll denied; approve after deny → TOKEN_EXPIRED.
  - expire: advance clock 1 h + 1 s → poll `expired`, details/approve → TOKEN_EXPIRED; approved but key not fetched
    within 15 min (delete/expire `agentkey:` via fakeredis TTL or clock-independent: delete key) → `expired`.
  - poll secret: missing → 401 AUTH_FAILURE; wrong → 404; unknown id → 404.
  - single retrieval under concurrency: two concurrent polls (asyncio.gather) → exactly one carries the key.
  - double approval race (gather two approves) → one 200, one TOKEN_EXPIRED, one key row.
  - approver mismatch: different email user → FORBIDDEN; logged out → AUTH_FAILURE; approve without CSRF → FORBIDDEN;
    viewer/billing-only org id → FORBIDDEN; developer approver requesting a scope outside developer scopes → FORBIDDEN;
    org with mfa_required and session without MFA → MFA_REQUIRED; org ip_allowlist excluding 127.0.0.1 (set via
    session_helpers.set_org settings) → FORBIDDEN.
  - scopes on approve not subset → INVALID_INPUT field scopes.
  - abuse limits: 6th create from one IP within the hour → 429 RATE_LIMITED + Retry-After; 4th for one email (from
    different peers via PeerSwitch) → 429; window reset after clock/TTL advance (fakeredis TTL: use real counter
    deletion or `redis.expire` check—assert `ttl` ≤ 3600 instead of sleeping).
  - Redis down on create → 503 SERVICE_UNAVAILABLE.
- test_audit_markers / test_openapi pass with the new routes (x-scope, examples, x-public).

### Gates
`make fmt lint typecheck placeholders test`, `make test-ts`, `make openapi` (+ gen:api both TS packages; re-run
shows no drift), `uv run python scripts/check_spec_refs.py`, `npx jscpd@4` 0 clones, radon `-n C` none in changed
src. build.done PLAIN STRING: `ralph emit "build.done" "P02-T13 tests: pass (N passed), lint: pass, typecheck: pass,
audit: pass, coverage: pass (NN.NN%), complexity: N.NN, duplication: pass, specs: pass"`

### Risks
- Pool size 1 per role: approval reads memberships (system_session) and settings (reader global session) BEFORE
  opening `tenant_session(org_id)`; never nest.
- WATCH/MULTI in fakeredis async works; if not, use a small Lua CAS script (fakeredis supports Lua with lupa — check
  it is installed, token_bucket already uses Lua so it is).
- The api key used in the poll test must pass ip_allowlist / org status gates; the new key's org has default
  settings.
- Background email after response: tests read InMemoryEmailSender after the response returns (httpx ASGI runs
  background tasks before returning).

## 2026-10-29 Builder: P02-T13 build notes
- New: `va_api/auth/agent_onboarding.py` (AgentRequest, AgentRequestStore with WATCH/MULTI `transition` and atomic
  `take_key` watching both `agentreq:` and `agentkey:` so concurrent polls deliver once, OnboardingLimiter fail-closed),
  `auth/window_counter.py` (`hit_window`, ResendLimiter now uses it, `resend_limit_unavailable` event kept),
  `routers/agent_onboarding.py` (+ `_schemas`), `routers/auth_agent_onboarding.py` (details/approve/deny on
  internal_auth_router). Email template `agent_key_request` in va_common. Container + SessionContainer fields
  `agent_requests`, `onboarding_limiter`; accessors in session_auth.
- Deviations from plan: (a) added stored status `approving` (claim during mint; polls report pending) and
  `key_expires_at` (clock-based 15-min expiry so FrozenClock tests work; Redis TTL is the backstop);
  (b) poll after a lost `take_key` reports `expired` if the record still says approved (key TTL ran out);
  (c) extracted `session_auth.mfa_checked_user` (shared with auth_invitations._accept_signed_in) to clear a jscpd
  clone; (d) a Redis failure after the DB commit (put_key) leaves a minted-but-undeliverable key that expires in
  30 d - accepted, documented here.
- Spec: specs/04 §10 Decision block + §2.4 allowlist line; specs/03 §11 five rows; AGENTS.md entry.
- (e) Full-suite run found test_envelopes failing: `/v1/agent-onboarding` was not in the specs/31 §1 envelope matrix
  (legacy by default). Added `/v1/agent-onboarding*` to the matrix with a `> Decision (2026-10-29)` note, to
  `va_api.envelopes.FAMILIES`, and bumped the family count to 32.

Gates: fmt; lint pass; typecheck pass; placeholders 0; make test 3397 passed, coverage 97.74%
(logs/p02-t13-test.log); make test-ts pass (22 + 52 + 48); make openapi + gen:api both TS packages, no drift on
re-run; check_spec_refs 0; jscpd 0 clones (api src/tests, va_common src); radon -n C none in changed src, average
A (2.75).

## Review P02-T13 (2026-10-29): approved
- Re-ran: make lint typecheck placeholders (pass, 0 findings), make test 3397 passed 97.74%, make test-ts (22+52+48),
  make openapi (no drift vs builder output).
- Checked against specs/04 §10 steps 1-5 + Decision block, specs/03 §11 rows, specs/31 envelope matrix: routes,
  limits (5/IP, 3/email, fail closed), poll secret hashing + constant-time compare, single delivery (WATCH on both
  keys), approve CAS + claim hand-back, test env / agent:<name> / 30 d / audit api_key.create with after.agent_name.
- Accepted residual (documented in build notes d): a Redis failure after the key commit leaves the request in
  `approving` (polls read pending until the 24 h TTL) and an undelivered key that expires in 30 d.

## 2026-10-29 Planner: P02-T13 approved → next P02-T14 (deps P02-T10, P01-T11 ticked)

## Plan P02-T14 — Dev seed script (specs/02 layout `scripts/seed_dev.py`, `make seed`; specs/04 §4.1 provisioning Decision; specs/24 §2 plans)

### Facts found while planning
- `make seed` already runs `uv run python scripts/seed_dev.py` (Makefile:31); the file does not exist. AGENTS.md:15 already
  promises "prints dev API key `va_test_…` and dashboard login".
- `va_db.provisioning.OrgProvisioner.provision(name=, owner=NewUser(...), plan_id="build")` creates the org, data key,
  webhook secret, user, and owner membership atomically. `settings` is stored sparse `{}` (= every default).
- Keys: `va_api.auth.api_keys.mint_api_key(hasher, env)` → MintedKey; `ApiKeyRepo(session, org).add/revoke/list_keys`;
  cache drop: `va_api.auth.invalidate_api_key_cache(redis, prefix)`.
- Credits: `credit_ledger` UNIQUE(org_id, idempotency_key); the grant pattern lives in tests/support/session_helpers.fund
  (lock org row, bump `credits_balance_micros`, ledger `grant` row, `reference_type="manual"`).
- Users: `IdentityRepo.get_user_by_email`, `set_password_hash`; org lookup via `MembershipLookupRepo(system).for_user`.
- Settings/infra: `ApiSettings()` (DB, Redis, pepper, KMS); `settings.api_key_hasher()`; `open_kms_provider(settings)`;
  `Envelope(kms, OrgDataKeyStore(database))`; `Database.from_settings(settings)`; `PasswordHasher()` (build via
  `asyncio.to_thread`, its ctor computes a dummy hash).
- Audit: `va_db.audit.record(session, AuditActor(org_id, "system", None), "api_key.create"|"api_key.revoke", "api_key", id,
  after=/before=)`; actor types include `system`.
- Tests: `apps/api/tests/conftest.py` `identity` fixture (fresh migrated DB per module, local-KMS Envelope); `scripts`
  is on pytest pythonpath and mypy_path, so tests can `import seed_dev`.

### Decisions to record (specs/02, next to the `seed_dev.py` layout line, one `> Decision (2026-10-29):` block "Dev seed
(P02-T14)"; AGENTS.md row 15 + gotcha)
1. **Scope now:** dev org + owner + credits + keys only. Voices and the sample pathway in the specs/02 layout comment are
   added by the phases that own those tables (the seed grows with them); `PLATFORM_POOL_ORG_ID` by specs/16's phase.
2. **Refuses outside local/test:** `VA_ENV` not in {local, test} → exit 2 with "seed_dev.py only runs with VA_ENV=local
   or test." (it prints secrets).
3. **Identity of the seed:** owner email `dev@voiceagents.local` (`--email` override), org name `VoiceAgents Dev`
   (constant `DEV_ORG_NAME`), plan `build`, kyc_level 0. Existing seed = the user with that email that holds an `owner`
   membership in an org named `DEV_ORG_NAME`. User exists but no such org → provision the org with the existing user id
   as owner. Emails validated with `email_validator.validate_email(check_deliverability=False, test_environment=True)`
   (`.local` passes; mem-ea3a).
4. **Password:** generated `secrets.token_urlsafe(18)` (24 chars), argon2id via PasswordHasher, `email_verified_at=now`;
   printed only when the user is created, or when `--reset-password` is given (re-hash + `set_password_hash`); otherwise
   `password: null` and the text output says "unchanged (use --reset-password)".
5. **Credits:** one `grant` ledger row of $50 (50_000_000 micros) with `idempotency_key="seed:dev-credits"`,
   `reference_type="manual"`, description "Dev seed credits", balance bumped under the org row lock — inserted only
   when that key is absent, so a re-run never adds credits.
6. **Keys (roll on every run):** scopes `["*"]`, names `dev-seed live` / `dev-seed test`, `created_by` = owner,
   no expiry. Each run revokes the org's non-revoked keys with exactly those names (+ audit `api_key.revoke`,
   `before`/`after` key view with revoked_at) and mints one new key per environment (+ audit `api_key.create`), all in
   one `tenant_session(org_id)` transaction; after commit, the revoked prefixes are dropped from the `apikey:` cache
   (Redis failure → warning on stderr, not fatal; the cache TTL is 60 s). Result: exactly one active key per
   environment and the printed keys always work. Raw keys are never stored, so re-printing old ones is impossible —
   rolling is the only way a re-run can print working keys.
7. **Output:** default human text (org id, slug, plan, credits, owner email, password line, both keys, curl hint
   `curl -H "Authorization: <test key>" localhost:8000/v1/me`); `--json` prints exactly one JSON object on stdout:
   `{"created": bool, "org": {"id", "name", "slug", "plan_id", "credits_balance_micros"}, "owner": {"id", "email",
   "password": str|null}, "api_keys": {"live": {"id","prefix","key"}, "test": {...}}}`. Logs go to stderr.

### Files
- **apps/api/src/va_api/dev_seed.py** (new; logic importable and testable, the script is a thin CLI):
  constants `DEV_EMAIL`, `DEV_ORG_NAME`, `DEV_PLAN_ID = "build"`, `DEV_CREDITS_MICROS = 50_000_000`,
  `CREDITS_IDEMPOTENCY_KEY = "seed:dev-credits"`, `KEY_NAMES: Mapping[KeyEnvironment, str]`, `SEED_ENVIRONMENTS`;
  `@dataclass(frozen) SeededKey(id, prefix, key=field(repr=False))`; `@dataclass(frozen) SeedResult(created, org_id,
  org_name, slug, plan_id, credits_balance_micros, owner_id, owner_email, password (repr=False) | None, keys:
  dict[env, SeededKey], revoked_prefixes: list[str])` + `to_json() -> dict`;
  `class DevSeeder(database, provisioner, api_key_hasher, password_hasher, clock)` with
  `async def seed(*, email: str = DEV_EMAIL, reset_password: bool = False) -> SeedResult` and private steps
  `_owner_and_org` (lookup/create; never nest sessions: system/global lookups first, then provision, then
  `tenant_session`), `_grant_credits(session, org_id)`, `_roll_keys(session, org_id, owner_id)`;
  `async def drop_cached_prefixes(redis, prefixes) -> list[str]` (returns failures for the warning);
  `def render_text(result) -> str`; `def check_seed_environment(env: Environment) -> None` (raises `SeedRefused`).
  Credit logic: reuse/extract nothing from tests (test helper stays); if a jscpd clone with session_helpers.fund appears,
  make session_helpers.fund call a new `va_db.repositories.credits.grant_credits(session, org_id, micros, *,
  idempotency_key, description) -> bool` and use it from both (preferred: add `grant_credits` to va_db credits repo
  up front, returning False when the key exists, and switch `fund` to it).
- **scripts/seed_dev.py** (new): argparse `--json`, `--email`, `--reset-password`; `main(argv) -> int` builds
  ApiSettings, checks env, opens KMS/Database/Redis (dispose all in an AsyncExitStack), runs DevSeeder, drops cache,
  prints; `if __name__ == "__main__": raise SystemExit(main())`. `AppError` → stderr message, exit 1.
- **packages/va_db/src/va_db/repositories/credits.py**: `grant_credits` (above), exported from repositories `__init__`.
- **apps/api/tests/support/session_helpers.py**: `fund` uses `grant_credits`.
- **specs/02** Decision block; **AGENTS.md**: row 15 text (`make seed` / `make seed ARGS=--json` if Makefile passes
  `$(ARGS)`; add `$(ARGS)` to the seed target), gotcha "re-running rolls the dev-seed keys".

### Tests
- **packages/va_db/tests/test_credits_grant.py** (or existing credits test module): grant writes ledger + balance;
  same idempotency key twice → second returns False, balance unchanged.
- **apps/api/tests/test_dev_seed.py** (`identity` fixture, FrozenClock, fakeredis):
  - first run: created True; org plan `build`, name, credits 50_000_000 with one `grant` ledger row; owner membership
    role owner; user email verified; password verifies with PasswordHasher; settings `{}`; two keys env live/test,
    scopes `["*"]`, created_by owner; both plaintexts authenticate (hasher.verify against stored hash / prefix match);
    audit rows `api_key.create` ×2 actor_type system.
  - second run: created False, password None; `row_counts` orgs/users/memberships/data_keys unchanged; ledger still one
    row, balance unchanged; old keys revoked, exactly one active key per env, new prefixes differ; audit
    `api_key.revoke` ×2; `revoked_prefixes` = the first run's prefixes.
  - `--reset-password` path: new password verifies, old does not.
  - existing user without the dev org → org provisioned with that user as owner, created True, password None.
  - `drop_cached_prefixes` deletes `apikey:{prefix}` entries; broken Redis (`FakeServer(connected=False)`) returns
    failures without raising.
  - `to_json` shape exact keys; `render_text` contains both keys and the curl line; `SeedResult` repr hides secrets.
  - `check_seed_environment(PRODUCTION/STAGING)` raises; local/test pass.
- **scripts/tests/test_seed_dev.py**: `main(["--json"])` with monkeypatched seeding entry (inject a fake runner via a
  module-level `run_seed` coroutine function patched in the test) → stdout is one parseable JSON object, exit 0;
  `VA_ENV=production` → exit 2 and message on stderr, nothing on stdout; AppError → exit 1. Keep main's I/O wiring
  covered without Postgres; the real DB path is covered by apps/api/tests/test_dev_seed.py (also add one end-to-end
  `main` run against the `identity` DB if the settings can point at `migrated_url` — optional, only if cheap).

### Gates
`make fmt lint typecheck placeholders test`, `make test-ts` (unchanged), `uv run python scripts/check_spec_refs.py`,
`npx jscpd@4` 0 clones, radon `-n C` none in changed src. Manual: `make up && make migrate && make seed` twice, then
`curl -H "Authorization: <test key>" localhost:8000/v1/me` returns the org (record the output in build notes; if the
stack cannot be started, say so explicitly instead of claiming it). build.done PLAIN STRING.

### Risks
- Pool size 1 per role: do lookups (system/global) and provisioning sequentially; never open a tenant session inside
  another session.
- `check_no_placeholders` flags `example.com` in production docstrings (mem-920d); use `voiceagents.local`.
- Printing secrets: only stdout, never through structlog; repr of dataclasses hides key/password.
- Ledger + balance must be written under `SELECT … FOR UPDATE` on the org row (tenant session can lock its own org).

## 2026-10-29 Builder: P02-T14 build notes
- New: `va_api/dev_seed.py` (DevSeeder, SeedResult/SeededKey with secrets hidden from repr, render_text,
  check_seed_environment/SeedRefusedError), `scripts/seed_dev.py` (argparse `--json/--email/--reset-password`,
  `run_seed` opens KMS/Database/Redis, logs to stderr), `va_db.repositories.grant_credits` + `MANUAL_REFERENCE`
  (session_helpers.fund now uses it), `va_api.auth.api_keys.drop_cached_keys(redis, prefixes) -> failed`
  (invalidate_cached_keys delegates to it). Makefile seed recipe `@uv run python scripts/seed_dev.py $(ARGS)`.
- Deviations from plan: (a) default owner email is `dev@voiceagents.test`, not `.local`: email_validator rejects the
  special-use `.local` even with test_environment=True (found by the manual smoke; memory mem-ea3a was wrong on this).
  Login accepts plain str, so `.test` logs in. (b) `SeedRefused` renamed `SeedRefusedError` (ruff N818). (c) main checks
  VA_ENV via BaseServiceSettings before building ApiSettings (ApiSettings refuses local KMS in production, which would
  otherwise mask the refusal); ValidationError → exit 1. (d) Seed recipe is `@`-prefixed: make echoed the command into
  stdout and broke `--json`.
- Tests: apps/api/tests/test_dev_seed.py (16, incl. /v1/me through the app with the seeded keys, revoked keys 401,
  run_seed end-to-end on its own engines with unreachable Redis reported), scripts/tests/test_seed_dev.py (8),
  packages/va_db/tests/test_credit_grant_integration.py (3).
- Manual smoke (real stack, `make up && make migrate`): `make seed` → created, $50.00, both keys; `make seed
  ARGS=--json` → created false, same org 01a188a5-…, password null, credits 50000000. api on host port 8010 (8000 is
  held by an unrelated `python3 -m http.server -d site 8000`, left alone): test key and live key `/v1/me` → 200,
  principal api_key, org VoiceAgents Dev, scopes ["*"]; first-run (revoked) test key → 401 AUTH_FAILURE.
- Gates: fmt/lint/typecheck pass, placeholders 0, check_spec_refs 0, jscpd 0 clones, radon -n C none (avg A 2.33),
  pip-audit + pnpm audit no known vulnerabilities. TS unchanged (test-ts not run). make test 3423 passed, 97.72%
  (logs/p02-t14-test.log).

## Review P02-T14 (2026-10-29): approved
- Re-ran: make lint typecheck placeholders (pass, 0 findings), make test 3423 passed 97.72% (logs/p02-t14-review-test.log).
- Checked against the specs/02 Dev seed Decision block: env refusal (exit 2), owner/org identity, password only on create/reset,
  one-time $50 grant under org row lock, key rolling with revoke/create audit rows matching the api_keys route, JSON shape,
  stderr logging, secrets hidden from repr.
- Non-blocking notes: text output says "previous dev-seed keys were revoked" even on a first run; DB connection errors in
  main surface as a traceback (still exit 1).

## 2026-10-29 Planner: P02-T14 approved → next P02-T15 (last unticked task; deps P02-T09 ticked)

## Plan P02-T15 — Cross-tenant route suite (specs/00 rule 7; specs/04 §14 tenant isolation, §2.4 public allowlist, §8.3 X-VA-Org-Id decision)

### Facts found while planning
- Routes are enumerated from `app.state.routers` (see test_audit_markers.app_routes). Today's non-public routes with path
  params: `{user_id}` (PATCH/DELETE /v1/org/members/{user_id}), `{invitation_id}` (DELETE
  /v1/org/members/invitations/{invitation_id}), `{org_id}` (GET /v1/orgs/{org_id}, POST …/suspend, …/reactivate),
  `{key_id}` (PATCH /v1/api_keys/{key_id}, POST …/revoke, …/roll). All raise `NOT_FOUND` for a foreign id.
- Opt-out markers today: `/internal/auth/*` carry `x-public: session-flow`; `GET /v1/health` and both
  `/v1/agent-onboarding/keys*` routes only carry `x-scope: public`; `/healthz`, `/readyz`, `/metrics` (probe_router,
  `include_in_schema=False`) carry nothing. The phase constraint says opt-out is ONLY via `x-public`, so these get one.
- `X-VA-Org-Id` (specs/04 §8.3 decision) applies to API keys AND session principals; another tenant's org id →
  `FORBIDDEN` "Organization is not a sub-account of the caller." before scopes/body handling.
- Request body models carry `json_schema_extra={"examples": [...]}` (ApiKeyPatch, ApiKeyRoll, MemberRolePatch,
  OrgPatch…), so a valid body can be derived automatically from `route.body_field` → model JSON schema `examples[0]`.
- Rate limiting is per principal (`rl:key:{id}:*` / `rl:user:{id}:*`, burst 2×rate) on the FrozenClock: the suite
  will make >100 calls with one B principal, so it deletes `rl:*` keys from `container.redis` before each call
  (helper `reset_rate_limits(container)`), instead of advancing the clock.
- Fixtures: conftest `identity`, `container` (SessionContainer), `client`; helpers `signed_in`, `make_member`,
  `add_member`, `store_api_key(container, org_id)`, `sub_account(client, **headers)`, `invite(...)`,
  `write_headers(client)`, `session_app(container, routers?)` (currently fixed to `(*ROUTERS, _probe)`),
  `session_client(app)`, `error_of`.

### Decisions to record (specs/04 §14, one `> Decision (2026-10-29):` block "Cross-tenant route suite (P02-T15)";
specs/04 §2.4: append the `x-public` values sentence)
1. **Opt-out** = `openapi_extra["x-public"]` with a non-empty reason string; nothing else (not `x-scope: public`, not
   path prefixes). Values used now: `session-flow` (internal auth), `health` (`GET /v1/health`), `probe`
   (`/healthz`, `/readyz`, `/metrics`), `poll-secret` (`POST /v1/agent-onboarding/keys`,
   `GET /v1/agent-onboarding/keys/{request_id}`). Every route with `x-scope: public` must also carry `x-public`
   (test-enforced). Later: Stripe webhook, widget public routes, widget.js get their own values.
2. **Org A / org B**: A = owner (`make_member`) + `*` key (`store_api_key`); B = a separate owner with its own `*`
   key and a signed-in session client. Every check runs for B as `api_key` and as `session` (writes send
   `write_headers`).
3. **Checks per tenant route** (all non-`x-public` routes):
   a. **Foreign id** (route has path params): fresh A resources from the factories (left to right, later factories see
      earlier ids) → B call → status must be 404 `NOT_FOUND` or 403 `FORBIDDEN`, and the body must not contain any A
      marker. Then the **owner control**: A's key calls the same route with the same ids → must be 2xx (or the status
      registered with `expect_owner_status`), proving the ids were real and B's call did not consume them.
   b. **Foreign org header** (every tenant route): B with `X-VA-Org-Id: <A org id>` → 403 `FORBIDDEN`. Path-param
      routes use the ids from (a); parameterless mutating routes send their derived body.
   c. **List/singleton leak** (GET routes without path params): B's 2xx response must contain no A marker. A markers =
      A org id, A owner id + email, A key prefix, every id produced by a "world" built once per test (one resource per
      registered factory, created before the checks).
   Parameterless mutating routes are only exercised by (b) (they act on the caller's own org; there is no foreign id
   to address) — stated in the decision.
4. **Request derivation**: registered `register_request(operation_id, json=..., params=...)` wins; else the body
   model's first JSON-schema example; a route with a body and neither → problem "no request body for <op>"; a route
   with a required query param and nothing registered → problem. A path param with no factory → problem
   "no resource factory for {param} (<method> <path>)". So a later router that forgets registration fails the suite.
5. **Registry**: `register_resource_factory(path_param, factory, *, path_prefix="/")`; the most specific
   (longest) matching `path_prefix` wins; registering the same (param, prefix) twice → `ValueError`. Phase-02
   factories live in `tests/support/cross_tenant_factories.py`; later phases add theirs there (or a module imported
   from it).

### Files
- **apps/api/tests/support/cross_tenant.py** (new, registry + checker; no phase-specific factories):
  - `@dataclass(frozen=True, slots=True) Tenant(org_id: uuid.UUID, owner: Member, api_key: str, session: httpx.AsyncClient)`
  - `@dataclass(frozen=True, slots=True) FactoryContext(client: httpx.AsyncClient, container: SessionContainer,
    identity: IdentityDb, tenant: Tenant, params: Mapping[str, str])` (client = an unauthenticated client to the
    same app; factories pass `Authorization: tenant.api_key` themselves or write via DB).
  - `ResourceFactory = Callable[[FactoryContext], Awaitable[str]]`
  - `register_resource_factory(path_param: str, factory: ResourceFactory, *, path_prefix: str = "/") -> None`
  - `factory_for(path: str, param: str) -> ResourceFactory | None`
  - `register_request(operation_id: str, *, json: Mapping[str, Any] | None = None, params: Mapping[str, str] | None = None) -> None`
  - `register_owner_status(operation_id: str, status: int) -> None` (only if a phase-02 owner control is not 2xx;
    otherwise keep it, it is part of the API for later phases, and unit-test it)
  - `PUBLIC_KEY: Final = "x-public"`; `is_public(route) -> bool`; `public_marker_problems(routes) -> list[str]`
    (empty/blank x-public; `x-scope: public` without `x-public`)
  - `tenant_routes(routes: Iterable[APIRoute]) -> list[APIRoute]`
  - `request_for(route) -> tuple[dict | None, dict[str, str]] | str` (body, query) or a problem string
  - `reset_rate_limits(container) -> Awaitable[None]` (scan_iter `rl:*` + delete)
  - `class CrossTenantProbe` (holds app client, container, identity, a: Tenant, b: Tenant, markers: set[str]) with
    `async def problems(routes) -> list[str]` running checks a–c for both B principals; each problem string is
    `"<check> <principal> <METHOD> <path>: <status> <body excerpt ≤200 chars>"`.
  - `async def make_tenant(app, identity, container, name) -> Tenant` (make_member with a unique org name + store_api_key
    + session client logged in; caller closes the session client via an AsyncExitStack).
- **apps/api/tests/support/cross_tenant_factories.py** (new): registrations for `user_id` (add_member developer to A
  → str(user.id)), `invitation_id` (POST /v1/org/members/invitations as A's key with a unique email, role developer →
  id), `key_id` (store_api_key for A → str(row.id)), `org_id` (sub_account(client, Authorization=A key) → str(id)).
- **apps/api/tests/support/session_helpers.py**: `session_app(container, *, extra_routers: tuple[APIRouter, ...] = (),
  **settings_overrides)` so the negative test can mount the leaky router (keep `_probe`).
- **apps/api/tests/test_cross_tenant_routes.py** (new):
  - `test_every_tenant_route_is_isolated` (DB): builds A, B, world; `problems(tenant_routes(app_routes))` == [];
    also asserts the tenant route list is non-empty and includes every phase-02 path-param operation
    (`org_members_update`, `org_members_remove`, `org_invitations_revoke`, `orgs_get`, `orgs_suspend`,
    `orgs_reactivate`, `api_keys_update`, `api_keys_revoke`, `api_keys_roll`).
  - `test_an_unscoped_route_is_caught` (DB): test-only `leaky_router` (defined in the test module, prefix
    `/v1/cross-tenant-leak`, `require_scope("api_keys:write")`): `GET /{key_id}` reads `ApiKey` by id through
    `container.database.system_session()` ignoring org and returns `{"id","org_id","prefix"}`; `GET ""` returns every
    key's prefix across orgs. Probe over only those two routes → exactly the expected problems (foreign-id 2xx for both
    principals on `/{key_id}`, list leak for both principals on `""`); the header check still passes (403) — assert
    the exact problem prefixes.
  - `test_a_missing_factory_is_reported`, `test_a_body_without_example_is_reported`,
    `test_a_required_query_without_registration_is_reported` (pure: synthetic APIRoutes like
    test_audit_markers._route; call the request/factory resolution functions, no DB).
  - `test_public_markers` (pure): real routes → `public_marker_problems == []`; the opted-out set equals exactly
    {internal auth ops, health_get, the three probes, the two agent-onboarding ops}; synthetic routes with blank
    `x-public` and with `x-scope: public` only are reported.
  - Registry unit tests: longest prefix wins, duplicate registration raises, `register_request` overrides the example,
    `register_owner_status` used by the control (synthetic).
- **apps/api/src/va_api/routers/health.py**: `openapi_extra={"x-scope": "public", "x-public": "health"}` on
  `/v1/health`; `openapi_extra={"x-public": "probe"}` on the three probe routes.
- **apps/api/src/va_api/routers/agent_onboarding.py**: add `"x-public": "poll-secret"` to `_PUBLIC` (keep `x-scope`).
- **openapi/openapi.json** via `make openapi`; then `corepack pnpm gen:api` in `sdks/typescript` and `apps/dashboard`
  (schema.test.ts compares; mem-7e50).
- **specs/04** decision block + §2.4 sentence; **AGENTS.md**: "Cross-tenant suite" convention: new routes with path
  params need a factory in `tests/support/cross_tenant_factories.py`; bodies come from model examples or
  `register_request`; public routes need `x-public: <reason>`.

### Tests (assertions summary)
Above. Expected B statuses today: all path-param routes 404 NOT_FOUND; header check 403 FORBIDDEN for both principals.
If any real route fails a check, that is a real isolation bug: fix the route (not the suite) and note it in build notes.

### Gates
`make fmt lint typecheck placeholders test`, `make openapi` + both `gen:api` (no drift on re-run), `make test-ts`,
`uv run python scripts/check_spec_refs.py`, `npx jscpd@4` 0 clones (the leaky router must not copy api_keys code),
radon `-n C` none in changed src. Then — since this is the last phase-02 task — also run `make check` once and record
its result (the Planner runs the exit gate after approval, but a red `make check` should be caught now).
build.done PLAIN STRING.

### Risks
- Pool size 1 per role: factories that write via DB must not run inside an open session; build the world sequentially.
- Session principal writes need CSRF + Origin (`write_headers`); missing them gives 403 CSRF which would falsely
  "pass" the foreign-id check → the probe must assert the error code is NOT_FOUND/FORBIDDEN with the expected message
  family, and run the owner control through the API key only. Also assert, once, that B's session can do a
  CSRF-protected write on its own org (e.g. PATCH /v1/org/settings `{}` → 200) so the session path is live.
- Owner controls mutate A (revoke, suspend, remove member): fresh resources per route keep them independent.
- `/v1/orgs/{org_id}/reactivate` on an active child may not be 2xx; check, and use `register_owner_status` or suspend
  the child in the factory chain via a prefix-specific factory (`path_prefix="/v1/orgs/{org_id}/reactivate"` won't
  match since prefixes are literal — prefer register_owner_status with the real status, documented).
- Probes `/metrics` etc. return non-JSON; they are public anyway.

## 2026-10-29 Builder: P02-T15 build notes
- New: `apps/api/tests/support/cross_tenant.py` (CrossTenantRegistry + module `REGISTRY`: register_resource_factory with
  longest-prefix factory_for, register_request, register_owner_status/owner_status_ok, request_for, resources_for;
  Tenant, FactoryContext, RouteRequest, path_params, app_routes, is_public, public_marker_problems, tenant_routes,
  reset_rate_limits, make_tenant, CrossTenantProbe with foreign-id/owner-control, foreign-org-header, list-leak checks),
  `tests/support/cross_tenant_factories.py` (user_id, invitation_id, key_id, org_id), `tests/test_cross_tenant_routes.py`
  (10 tests: full-app suite, leaky router negative test, registry/derivation unit tests, x-public marker test).
- Route markers: `GET /v1/health` `x-public: health`; `/healthz` `/readyz` `/metrics` `x-public: probe`; both agent
  onboarding routes `x-public: poll-secret`. openapi.json +3 lines (`make openapi`); both `gen:api` re-run, no TS diff.
- `session_app(container, *, extra_routers=())`; `app_routes` moved from test_audit_markers into cross_tenant (shared).
- Deviations from plan: (a) the registry is a class with a module-level `REGISTRY` instance (unit tests use fresh
  instances so the global one is never mutated); (b) the invitation factory writes via `InvitationRepo` in a tenant session
  (store_api_key keys have no `created_by`, so an API-key invite has no inviter); (c) foreign-id leak check ignores the ids
  the request itself names (a 404 echoing the requested id leaks nothing); (d) the header check also requires the exact
  "Organization is not a sub-account of the caller." message, so a CSRF 403 can never count as a pass; (e) FastAPI has
  no `get_flat_dependant` here, so query params are collected by walking `dependant.dependencies`.
- No real isolation bug found: all 9 path-param ops 404 for B (both principals), every tenant route 403 on the header,
  owner controls all 2xx (reactivate on an active child is 200, so no register_owner_status needed).
- Mutation check: temporarily replacing the child lookup in `orgs_get` with `session.get(Org, id)` made the suite report
  `foreign-id api_key GET /v1/orgs/{org_id}: 200 ...` (and session); the change was reverted.
- Gates: make check exit 0 (lint, typecheck, placeholders 0, 3433 passed 97.72%, test-ts widget 22 / dashboard 52 /
  sdk 48, openapi drift current, check_spec_refs 0) — logs/p02-t15-check.log. jscpd 0 clones; radon -n C none (avg A
  2.31); pip-audit and pnpm audit no known vulnerabilities.

## Review P02-T15 (2026-10-29): approved
- Re-ran: make lint typecheck placeholders (pass, 0 findings), make test 3433 passed 97.72% (logs/p02-t15-review-test.log),
  make test-ts pass, check_spec_refs 0, jscpd 0 clones.
- Checked against the specs/04 §2.4 and §14 Decision blocks: routes enumerated from app.state.routers; x-public is the
  only opt-out, and test_public_markers pins the opted-out set; a missing factory, body example, or query registration
  is reported; CSRF 403 can't count as a refusal; owner control runs through A's key; the negative test catches both leaks.
- Non-blocking notes: /internal/auth/* (including switch-org) are outside the suite via session-flow and are covered by
  their own tests; marker matching uses substrings, which is fine because the markers are UUIDs, unique emails, and key prefixes.
