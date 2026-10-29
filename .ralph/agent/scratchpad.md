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
