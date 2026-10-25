
## Plan P00-T02 — `.env.example` and settings contract (Planner, 2026-10-25)
Spec: specs/02 §6 (table at lines ~184-289, 110+ rows). Goal: `.env.example` mirrors every variable with its dev value
+ a comment; a test parses the spec table and fails on any drift in either direction (names AND literal dev values).

### Files
1. `/Users/drop/AIVOICE/scripts/env_contract.py` (pure functions, no I/O except `main`; typed for mypy --strict):
   ```python
   SPEC_PATH = Path("specs/02-repo-layout-and-conventions.md"); ENV_EXAMPLE_PATH = Path(".env.example")
   class ValueRule(StrEnum): LITERAL="literal"; EMPTY="empty"; HEX64="hex64"
   @dataclass(frozen=True) class SpecVar: name: str; rule: ValueRule; literal: str | None; template: bool = False
   @dataclass(frozen=True) class EnvEntry: name: str; value: str; line: int; commented: bool  # commented=True only for "# NAME=value" template examples
   def extract_section(spec_text: str, heading_prefix: str = "## 6.") -> str   # text from heading to next "## "; ValueError if absent
   def parse_spec_table(spec_text: str) -> list[SpecVar]
   def classify_dev_value(cell: str) -> tuple[ValueRule, str | None]
   def parse_env_example(text: str) -> list[EnvEntry]          # raises ValueError("line N: ...") on malformed assignment
   def check(spec_text: str, env_text: str) -> list[str]       # human-readable problems, [] when in parity
   def main(argv: Sequence[str] | None = None) -> int          # prints problems; exit 1 if any (argparse --spec/--env overrides)
   ```
   Table parsing: rows between `| Variable |` header and end of section; skip separator `|---|`. Split cells on `|`
   (no cell in §6 contains an escaped pipe — verify; if one does, split on ` | ` respecting backticks).
   - Name cell: all backticked tokens `` `([A-Z][A-Z0-9_<>]*)` ``. Multi-name row (`S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`)
     pairs with dev cell split on ` / ` (`minio` / `minio12345`). Names containing `<` (`LOG_LEVEL_<COMPONENT>`) → template=True.
   - classify_dev_value(cell), checked in this order:
     a. cell lower starts with "empty" → EMPTY (covers "empty", "empty (host-only cookie)")
     b. contains "hex chars" (`64 random hex chars`, `64 hex chars`) → HEX64
     c. starts with a backtick literal → LITERAL = first backticked token (`localhost:8100` (tunnel…) → `localhost:8100`;
        `true` (forced…) → `true`; `console` in local… → `console`; `http://localhost:9100/v2` (sim) → that URL)
     d. anything else ("sim public key printed by `make up`", "created by …bootstrap", "created by bootstrap", "hostname",
        "generated at startup", "unset") → EMPTY (runtime-provided; the comment in .env.example says where it comes from).
        Note (d) must be checked before (c) only in the sense that (c) requires the cell to START with a backtick —
        "sim public key printed by `make up`" does not, so it lands in (d). Unit-test every distinct phrasing.
   - parse_env_example: blank lines skipped; `# NAME=value` where NAME matches a template pattern → commented entry;
     other `#` lines are comments; `NAME=value` with NAME `[A-Z][A-Z0-9_]*`; value: strip one pair of matching
     surrounding `"` or `'`; no inline comments supported (a `#` inside an unquoted value is kept literally — never needed).
     Anything else → ValueError.
   - check() problems (each a distinct message, sorted for stable output):
     `missing in .env.example: NAME`, `not in spec: NAME`, `duplicate: NAME (lines a, b)`,
     `value mismatch: NAME expected 'x' got 'y'`, `expected empty: NAME got 'y'`, `expected 64 hex chars: NAME`,
     `no comment above: NAME` (the line directly above each assignment must be a `#` comment line),
     `duplicate hex value shared by: A, B` (HEX64 secrets must be pairwise distinct),
     template rows: `.env.example` must contain a commented example `# LOG_LEVEL_<something>=...` whose name matches
     the template with `<COMPONENT>` → `[A-Z0-9_]+` → else `missing template example: LOG_LEVEL_<COMPONENT>`.
2. `/Users/drop/AIVOICE/.env.example` — every §6 variable in spec order, grouped with section comment headers
   (Core, Database, Redis/ClickHouse, Temporal, S3, Public hosts, Secrets & KMS, Telnyx, ElevenLabs, LLM, Stripe,
   Email/SSO, Observability, Media, Platform policy, COGS, Simulators, Live smoke tests, Rotation, Dashboard, Clock, CI).
   Each assignment preceded by a one-line comment derived from the spec Meaning/Used-by columns (e.g.
   `# api, worker, media: KMS provider - local (AES-256-GCM master key from env) or aws`). Specific values:
   - `EMAIL_FROM="VoiceAgents <no-reply@voiceagents.local>"` (quoted: spaces + angle brackets; dotenv + compose both strip).
   - HEX64 (`SECRET_KEY`, `STREAM_TOKEN_SECRET`, `LOCAL_MASTER_KEY`, `API_KEY_PEPPER`): generate four distinct values with
     `python -c 'import secrets; print(secrets.token_hex(32))'`; comment says "local dev only - generate a fresh value
     for every shared environment". These are dev-only throwaways (spec mandates a dev value), never used outside local/test.
   - Runtime-provided (empty): `TELNYX_PUBLIC_KEY` (comment: read from `.sim/telnyx_public_key` written at sim startup),
     `TELNYX_CALL_CONTROL_APP_ID` / `TELNYX_MESSAGING_PROFILE_ID` / `TELNYX_OUTBOUND_VOICE_PROFILE_ID` (scripts/telnyx_bootstrap.py),
     `POD_ID` (defaults to hostname), `PLATFORM_POOL_ORG_ID` (scripts/seed_dev.py), `SIM_ED25519_PRIVATE_KEY` (generated at sim startup).
   - Template: `# LOG_LEVEL_VA_MEDIA=DEBUG` under a comment explaining `LOG_LEVEL_<COMPONENT>`.
   - Avoid the words the placeholder gate will ban (TODO/TBD/placeholder/example.com/your-api-key) in comments; the file
     is outside the gate's scan roots anyway, but keep it clean. No `$` characters (compose interpolation).
3. `/Users/drop/AIVOICE/scripts/tests/test_env_example.py`:
   - `test_env_example_matches_spec`: `check(real spec, real .env.example) == []` (paths resolved from repo root via
     `Path(__file__).resolve().parents[2]`).
   - `test_spec_table_parsed_completely`: parsed var count == number of `| \`` rows in §6 (+1 for the S3 two-name row);
     includes `VA_ENV`, `SLACK_ALERTS_WEBHOOK`, both S3 key names, template `LOG_LEVEL_<COMPONENT>`.
   - `test_classify_dev_value` parametrized over every distinct phrasing listed above.
   - `test_parse_env_example_quotes_comments_and_errors`: quoted value stripped; commented template parsed; malformed line → ValueError with line number.
   - Drift tests with crafted fixtures (small synthetic spec section + env text): `test_var_added_to_spec_only_fails`,
     `test_var_added_to_env_only_fails`, `test_value_mismatch_fails`, `test_empty_rule_fails_on_value`,
     `test_hex_rule_fails_on_bad_and_duplicate_values`, `test_missing_comment_fails`, `test_duplicate_assignment_fails`,
     `test_missing_template_example_fails`, `test_missing_section_raises`.
   - `test_main_exit_codes` with tmp_path files + capsys: 0 + no output on parity; 1 + problem lines on drift.
   Coverage of env_contract.py ≥ 95 %.

### Spec decision (same commit, append under specs/02 §6 table)
`> Decision (2026-10-25): .env.example lists every variable above in table order with a comment line before each.
Values: the literal dev value; "empty"/runtime-provided rows (bootstrap/seed/sim-generated, hostname) are left empty and
resolved at runtime; "64 (random) hex chars" rows hold distinct local-only random values; LOG_LEVEL_<COMPONENT> appears
only as a commented example. scripts/env_contract.py + scripts/tests/test_env_example.py enforce parity (run by make test).`

### AGENTS.md
Commands row: `Check .env.example parity | uv run python scripts/env_contract.py`. Learned bullet: adding an env var
means editing specs/02 §6 AND .env.example (comment line above) or `make test` fails.

### Gates
make fmt, make lint, make typecheck, make test (coverage ≥85 %), make test-ts; `uv run python scripts/env_contract.py` exit 0.
make placeholders still n/a (P00-T03). Evidence extras per AGENTS.md (pip-audit, radon on env_contract.py, jscpd);
emit build.done as plain-text tokens.

### Risks
- Spec cells with odd formatting (e.g. `COGS_TELNYX_OUTBOUND_PER_MIN_MICROS` meaning cell contains parentheses/backticks) —
  only name and dev cells are parsed; meaning cell ignored.
- Table may contain a pipe inside backticks in some cell — verify with a quick scan; parser must not mis-split.
- ruff S105/S106 may flag hard-coded "secret"-named strings in tests; use fixtures that don't trip them or `# noqa: S105` with reason.

---

## Phase 01 Planner note (2026-10-25)
Phase-01 loop started while phase 00 had only P00-T01 ticked (the P00-T02 plan above was never built). Every P01 task
depends transitively on P00-T05/P00-T08, so I inserted prerequisite tasks P01-T00a (=P00-T05), P01-T00b (=P00-T08),
P01-T00c (=P00-T03, the placeholders gate) at the top of plan/phase-01.md. Reviewer: when approving P01-T00a, tick BOTH
`P01-T00a` in plan/phase-01.md and `P00-T05` in plan/phase-00.md. The P00-T02 plan above is NOT for this loop.

## Plan P01-T00a — execute P00-T05: `va_common` core (settings, clock, ids, json, errors, money)
Spec: specs/02 §4 (Time, IDs, Money, Errors, Settings), §6 (VA_ENV, LOG_LEVEL, LOG_TRANSCRIPTS, VA_CLOCK);
specs/31 §3 (codes), §4 (AppError fields, CODES parity test); specs/01 §4 (Settings via pydantic-settings);
specs/24 (round_half_up in integer micros with Decimal).

### Package scaffold (new workspace member; `packages/*` already in uv workspace members)
- `packages/va_common/pyproject.toml`: name `va-common`, version `0.1.0`, requires-python `>=3.12,<3.13`,
  build-backend hatchling (`[tool.hatch.build.targets.wheel] packages = ["src/va_common"]`), dependencies:
  `pydantic>=2.9`, `pydantic-settings>=2.6`, `orjson>=3.10`, `uuid-utils>=0.9`. (No other deps — infra helpers come in P01-T00b.)
- `packages/va_common/src/va_common/__init__.py` (docstring only + `__all__: list[str] = []`), `py.typed` (empty).
- `packages/va_common/tests/__init__.py` must NOT exist (importlib mode); add `tests/conftest.py` only if needed.
- Root `pyproject.toml`: `[tool.mypy] files = ["scripts", "packages/va_common/src", "packages/va_common/tests"]`,
  `mypy_path = "scripts:packages/va_common/src"`. Run `uv lock` and commit uv.lock.
  Also add `"va-common"` to the root `dev` dependency group with `[tool.uv.sources] va-common = { workspace = true }`
  so plain `uv run pytest` / `uv run mypy` always have it installed; verify with `uv sync && uv run python -c "import va_common"`.

### Modules (all fully typed, mypy --strict clean)
1. `va_common/time.py`
   ```python
   def utcnow() -> datetime                       # datetime.now(UTC)
   def ensure_utc(dt: datetime) -> datetime       # ValueError on naive; converts aware to UTC
   class Clock(Protocol):
       def now(self) -> datetime: ...             # aware UTC
       def monotonic(self) -> float: ...
   class SystemClock:                             # now() -> utcnow(); monotonic() -> time.monotonic()
   class FrozenClock:
       def __init__(self, start: datetime, monotonic_start: float = 0.0) -> None   # ValueError if naive
       def now(self) -> datetime; def monotonic(self) -> float
       def advance(self, delta: timedelta | float) -> None   # float = seconds; negative -> ValueError
       def set(self, when: datetime) -> None                 # moves now(); monotonic advances by forward delta only; backwards -> ValueError
   ```
   `Clock` is `@runtime_checkable`. No clock factory yet (VA_CLOCK wiring belongs to va_voice/media later).
2. `va_common/ids.py`
   ```python
   PUBLIC_PREFIXES: Final = {"tool": "TL-", "knowledge_base": "KB-", "prompt": "PT-"}
   PublicKind = Literal["tool", "knowledge_base", "prompt"]
   def new_id() -> uuid.UUID                      # uuid.UUID(str(uuid_utils.uuid7())) -> stdlib UUID, version 7
   def to_public(kind: PublicKind, value: uuid.UUID) -> str      # "TL-<canonical lowercase uuid>"
   def from_public(kind: PublicKind, value: str) -> uuid.UUID    # strict: exact prefix (case-sensitive), canonical
       # 36-char hyphenated uuid (accept upper-case hex, normalise); raise AppError("INVALID_INPUT", f"Invalid {label} id: {value!r}", field=None)
   def parse_uuid(value: str) -> uuid.UUID        # bare uuid, same strictness, AppError INVALID_INPUT
   ```
   Import order: ids imports errors (no cycle; errors imports nothing from va_common).
3. `va_common/json.py` (always absolute imports inside the package)
   ```python
   def dumps(obj: object, *, sort_keys: bool = False, indent: bool = False) -> bytes
       # orjson.dumps with OPT_PASSTHROUGH_DATETIME (+ OPT_SORT_KEYS / OPT_INDENT_2) and a `default` hook:
       #   datetime -> ensure_utc(dt) then isoformat with "+00:00" -> "Z" (naive -> TypeError("naive datetime not allowed"))
       #   Decimal -> str; pydantic BaseModel -> model_dump(mode="json"); set/frozenset -> sorted list; else TypeError
       # UUID uses orjson native canonical string.
   def dumps_str(obj: object, *, sort_keys: bool = False, indent: bool = False) -> str
   def loads(data: bytes | bytearray | memoryview | str) -> Any   # orjson.loads; malformed raises orjson.JSONDecodeError (a ValueError)
   ```
4. `va_common/errors.py`
   ```python
   CODES: Final[Mapping[str, int]] = MappingProxyType({...})   # every row of specs/31 §3 in table order
   # KB_ERROR's HTTP column is "400/500": map to 400 (default) and document via Decision note; AppError accepts an explicit
   # http_status override so KB processing failures can raise 500.
   class AppError(Exception):
       def __init__(self, code: str, message: str, http_status: int | None = None, details: Mapping[str, object] | None = None, *, field: str | None = None) -> None
           # unknown code -> ValueError(f"unknown error code {code!r}"); http_status defaults to CODES[code];
           # explicit http_status must be 400-599 else ValueError. Stores .code .message .http_status .field .details (dict copy, {} default)
       def to_item(self) -> dict[str, object]     # {"error": code, "message": message, + "field" if set, + "details" if non-empty}
       def __str__ -> f"{code}: {message}"; __repr__ includes http_status
   def not_found(resource: str = "Resource") -> AppError    # AppError("NOT_FOUND", f"{resource} not found")
   def internal_error() -> AppError                          # ("INTERNAL_ERROR", "Internal server error")
   ```
   Signature order follows specs/02 §4 `AppError(code, message, http_status, details)` with `field` keyword-only (specs/31 §4 lists it).
5. `va_common/money.py`
   ```python
   MICROS_PER_DOLLAR: Final = 1_000_000
   def dollars_to_micros(value: Decimal | int | str) -> int   # Decimal(str) exact; round_half_up to integer micros;
       # floats REJECTED with TypeError (never floats); NaN/Inf -> ValueError; bool -> TypeError
   def micros_to_dollars_str(micros: int) -> str              # exact, 6 decimals: 1_500_000 -> "1.500000", -1 -> "-0.000001"
   def micros_to_dollars(micros: int) -> Decimal              # Decimal quantized to 6 places (API "decimal numbers rounded to 6 places")
   def round_half_up_micros(quantity: Decimal | int, unit_price_micros: Decimal | int) -> int   # specs/24 rounding: Decimal product, ROUND_HALF_UP to int
   ```
6. `va_common/settings.py`
   ```python
   Environment = Literal["local", "test", "staging", "production"]
   class BaseServiceSettings(BaseSettings):
       model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True, case_sensitive=False)
       va_env: Environment = "local"           # field names == lower-cased env names, no aliases
       log_level: Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"] = "INFO"   # validator upper-cases input
       log_transcripts: bool = False
       otel_exporter_otlp_endpoint: str = "http://localhost:4317"
       sentry_dsn: str = ""
       @model_validator(mode="before") on the raw input: if va_env (case-insensitive key) == "production", set log_transcripts False
       @property is_production -> bool
   ```
   Only the "all"-scoped vars from §6 live in the base (VA_ENV, LOG_LEVEL, LOG_TRANSCRIPTS, OTEL_EXPORTER_OTLP_ENDPOINT,
   SENTRY_DSN). Service-specific vars belong to each service's `Settings` subclass in later tasks. Defaults equal the
   §6 dev values EXCEPT log_transcripts default False (safe default; .env sets true locally) — note in Decision.
   Tests must isolate env: `monkeypatch.delenv` + `_env_file=None`.

### Tests (packages/va_common/tests/)
- `test_errors.py`: `test_codes_match_spec_table` — reads `specs/31-error-catalog.md` (path via
  `Path(__file__).resolve().parents[3] / "specs/31-error-catalog.md"`), extracts the §3 section (from `## 3. Error codes`
  to next `## `, so §3a detail codes are excluded), parses rows `| \`CODE\` | HTTP |`, HTTP "400/500" → 400 (first
  value); asserts `dict(CODES) == parsed` (both directions, exact). Plus: unknown code raises ValueError; default status;
  override status; invalid override ValueError; to_item with/without field/details; details copied (mutating input
  doesn't change error); str/repr; not_found/internal_error helpers; CODES immutable (TypeError on assignment).
- `test_ids.py`: new_id is version 7, monotonic ordering of 1000 consecutive ids (uuid7 sorts by time); hypothesis
  `@given(st.uuids(), st.sampled_from(kinds))` round-trip `from_public(kind, to_public(kind, u)) == u`; upper-case hex
  accepted; wrong prefix / missing prefix / lowercase prefix "tl-" / garbage / braces / urn form / no hyphens → AppError
  with code INVALID_INPUT and http_status 400; parse_uuid same.
- `test_money.py`: hypothesis round-trip `dollars_to_micros(micros_to_dollars_str(m)) == m` for ints in ±10^15;
  `dollars_to_micros("0.0000005") == 1` (half-up), `"0.0000004" -> 0`, `"-0.0000005" -> -1` (ROUND_HALF_UP is away from
  zero in Decimal — assert that exact behaviour and document), int input `3 -> 3_000_000`; float → TypeError; bool →
  TypeError; "NaN"/"Infinity" → ValueError; invalid string → ValueError; micros_to_dollars Decimal exponent -6;
  round_half_up_micros(Decimal("1.5"), 3) == 5 (4.5 → 5), (Decimal("2"), Decimal("0.25")) == 1? (0.5 → 1).
- `test_time.py`: utcnow tz is UTC; ensure_utc naive → ValueError, +02:00 aware → converted; FrozenClock now/advance
  (timedelta and float), negative advance ValueError, set forward/backward, monotonic advances in lockstep; naive start
  ValueError; SystemClock now is aware and within freezegun frozen time (`freeze_time("2026-01-01")`), monotonic non-decreasing;
  `isinstance(SystemClock(), Clock)` requires `@runtime_checkable` on Clock — add it.
- `test_json.py`: dumps of dict with UUID, aware datetime (Z suffix), Decimal ("1.10" kept as string), pydantic model,
  set → sorted list; naive datetime → TypeError; unsupported type (object()) → TypeError; sort_keys option; indent option;
  dumps_str returns str; loads accepts bytes/str/memoryview; malformed → ValueError (orjson.JSONDecodeError).
- `test_settings.py`: defaults with clean env (`_env_file=None`); env overrides (VA_ENV=staging, LOG_LEVEL=debug →
  "DEBUG"); invalid VA_ENV → ValidationError; LOG_TRANSCRIPTS=true honoured in local, forced False in production;
  settings frozen (assignment raises ValidationError); subclass adding a field reads its env var (proves extensibility);
  `.env` file loading via tmp_path + `_env_file=tmp/.env`.
Coverage target: 100 % lines/branches on va_common (small pure modules).

### Spec decisions (append in the same commit)
- specs/31 after §4: `> Decision (2026-10-25): KB_ERROR's "400/500" maps to 400 in CODES (the parity test reads the
  first status); KB processing failures raise AppError("KB_ERROR", ..., http_status=500) via the explicit override.`
- specs/02 §4 after Settings bullet: `> Decision (2026-10-25): BaseServiceSettings holds only the variables §6 marks
  "all" (VA_ENV, LOG_LEVEL, LOG_TRANSCRIPTS, OTEL_EXPORTER_OTLP_ENDPOINT, SENTRY_DSN); services add the rest in their own
  Settings subclass. LOG_TRANSCRIPTS defaults to false in code (the .env dev value is true) and is forced false when
  VA_ENV=production. Money: dollars_to_micros rejects floats; rounding is Decimal ROUND_HALF_UP (away from zero for
  negatives).`

### AGENTS.md
- Learned bullet (P01-T00a): how va_common gets installed for `uv run` (workspace source + dev group, or --all-packages),
  and "every new package must be added to root mypy `files` and `mypy_path`" already exists — extend with the exact pattern.
- Conventions: `va_common.ids.to_public("tool", id)`; `va_common.errors.AppError(code, message, http_status=None, details=None, field=)`.

### Gates (Builder)
`make fmt lint typecheck test` (+ `test-ts`, no TS change). `make placeholders` will fail because
scripts/check_no_placeholders.py does not exist until P01-T00c — report that honestly (same as P00-T01 evidence) and
instead grep the new files for TODO|FIXME|XXX|TBD|HACK|NotImplementedError|placeholder → must be empty.
Backpressure extras (see AGENTS.md): audit via `uv export --no-hashes --all-packages > logs/p01t00a-req.txt && uvx pip-audit -r logs/p01t00a-req.txt`
+ `corepack pnpm audit`; complexity via `uvx radon cc -s -a packages/va_common/src`; duplication via `npx --yes jscpd@4 packages/va_common`.
Commit: `feat(common): add va_common core settings, clock, ids, json, errors, money` with footer `Task: P01-T00a` (and `Task: P00-T05` line).

### Risks
- `filterwarnings = ["error"]`: pydantic-settings may warn about missing `.env` — passing `_env_file=None` in tests avoids it.
- hypothesis + pytest-xdist: fine; keep examples ≤ 200 (default) for speed.
- `va_common/json.py` shadowing: inside the package always use absolute imports; stdlib `json` is not needed.
- uuid_utils.uuid7() returns `uuid_utils.UUID`, not stdlib — convert via `uuid.UUID(bytes=u.bytes)`.
