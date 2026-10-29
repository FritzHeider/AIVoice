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
