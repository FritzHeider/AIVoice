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
