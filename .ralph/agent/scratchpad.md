# Scratchpad — Phase 03

## Plan P03-T01 — Simulator framework and scenario DSL

Spec: specs/30-testing-and-evals.md §2 (`sims` fixture), §3 (SimState, SimSettings table, fast/realistic profiles,
control API under `/sim/*`), §3.2 (Clock bullet, control API list), §3.5 (Scenario DSL).
Depends on P00-T12 (ticked). Existing: `apps/sim` (distribution **`va-sim`**, module `va_sim_server`) serves only
probes on 9100/9200/9300/9400; `va_sim_server.settings.SimSettings` has `sim_clock`, `sim_state_redis_url`, `sim_workers`.

### Decisions (Builder records these in the same commit)

1. **Control API prefix**: plan text says `/__sim/scenarios`, `/__sim/calls/{id}/recording`, `/__sim/reset`; specs/30 §3
   says `/sim/*` (`/sim/reset`, `/sim/telnyx/scenarios`, `/sim/recordings/{id}.wav`). Specs beat plan → use `/sim/*`.
   Edit the P03-T01 Deliverables line in `plan/phase-03.md` to the spec paths and add
   `> Decision (2026-10-29): control API uses the specs/30 §3 /sim/* paths ...` under §3 of specs/30.
2. **Distribution names**: `packages/va_sim` needs distribution `va-sim`, which `apps/sim` currently owns. Rename
   `apps/sim` distribution to `va-sim-server` (module stays `va_sim_server`): `apps/sim/pyproject.toml` name,
   `va_sim_server/__init__.py` → `version("va-sim-server")`, root `pyproject.toml` dev group + `[tool.uv.sources]`
   (add both `va-sim` and `va-sim-server`), then `uv lock`. Grep for other `"va-sim"` uses (Dockerfiles under
   `infra/docker`, compose) and update. Add to specs/02 §1 layout comment if it names the distribution.
3. **Seeding without a new env var**: `SimState(seed=0)`; `POST /sim/reset` accepts optional `{"seed": int}`. No
   `SIM_SEED` env var (avoids touching the specs/02 §6 env-table test counts). Record as Decision in specs/30 §3.
4. **Redis-backed SimState** (`SIM_STATE_REDIS_URL`) is out of T01 scope: T01 ships the in-memory `SimState` with a
   JSON `snapshot()`; append "Redis-backed SimState sharing when SIM_STATE_REDIS_URL is set" to the P03-T15
   Deliverables line in `plan/phase-03.md` (and its Acceptance: "two sim processes see the same registered scenario").
5. **DSL defaults not in the spec** (record as Decision in specs/30 §3.5): `wait_agent_speech` defaults
   `min_ms=0`, `then_silence_ms=700`, `timeout_ms=10000`; `expect_hangup.by` ∈ `agent|caller|any` default `any`,
   `timeout_ms` default 10000; `silence.ms` required; `dtmf.digits` regex `^[0-9A-D*#w]+$`, `inband` default false;
   `say.language` default `en`; `profile` default `fast`.

### Files

New package `packages/va_sim/` (copy layout of packages/va_events):
- `pyproject.toml`: name `va-sim`, deps `pydantic>=2.9`, `pydantic-settings` (whatever va_common uses), `pyyaml>=6.0`,
  `starlette>=1.0`, `uvicorn` (same spec as va_common), `va-common`; uv_build backend, module-name `va_sim`.
  Add `types-pyyaml` already in root dev group. Add `packages/va_sim/src` + `packages/va_sim/tests` to mypy `files`
  and `mypy_path`; testpaths already cover `packages`.
- `src/va_sim/__init__.py` (docstring + `__version__ = version("va-sim")`), `py.typed`.
- `src/va_sim/settings.py` — moved `SimSettings(RedisSettings)` (service_name `sim`) with **every** §3 table var:
  `sim_clock: Literal["realtime","accelerated"]="realtime"`, `sim_stt_commit_latency_ms: int=60 (ge 0)`,
  `sim_tts_ttfa_ms=80`, `sim_llm_ttft_ms=150`, `sim_llm_tokens_per_s: float=80 (gt 0)`, `sim_llm_rules: str=""`
  (+ property `llm_rule_paths -> tuple[Path,...]` splitting on commas, stripping empties),
  `sim_ed25519_private_key: SecretStr | None = None`, `sim_edge_webhook_url`, `sim_messaging_webhook_url`,
  `sim_stripe_webhook_url` (defaults exactly as specs/02 §6 lines 312–314: `http://localhost:8100/...`,
  `http://localhost:8000/v1/billing/stripe/webhook` — specs/02 is the shared contract and beats specs/30's `edge:`
  host; note it in the Decision), `sim_state_redis_url=""`, `sim_workers ≥1`.
  Plus `Latencies` frozen dataclass (`stt_final_ms, tts_ttfa_ms, ttft_ms, tokens_per_s`), `PROFILES: Final[Mapping[str,
  Latencies]]` = fast (10, 20, 20, 1000) / realistic (40, 250, 250, 80), and `SimSettings.latencies() -> Latencies`.
  Delete `apps/sim/src/va_sim_server/settings.py`; `va_sim_server.main` and apps/sim tests import from
  `va_sim.settings`. Check `.env.example` / `scripts/tests/test_env_example.py` still pass (field names unchanged except
  additions which are already in specs/02 §6; if the env-example test maps vars to settings classes, update it).
- `src/va_sim/clock.py`:
  - `class SimClock(Clock, Protocol)`: adds `mode: ClockMode` and `virtual_ms() -> int` (ms since clock start).
  - `RealtimeClock(start_monotonic via time.monotonic)`: now=utcnow, sleep=asyncio.sleep.
  - `AcceleratedClock(start: datetime = DEFAULT_START)`: virtual time; `sleep(s)` advances virtual time by `s` then
    `await asyncio.sleep(0)`; rejects negative; `advance(s)`; `now()` = start + elapsed; `monotonic()` = elapsed.
    Concurrent sleepers: keep a heap of wake-up deadlines so `sleep` from N tasks wakes in deadline order
    (implementation: `sleep` registers (deadline, seq, future) and a single `_pump` resolves the earliest pending
    future after `await asyncio.sleep(0)` rounds until no runnable work; simplest correct version: each sleeper sets
    `virtual = max(virtual, deadline)` in deadline order by awaiting a Condition — Builder must include a test with
    two tasks sleeping 0.3 s and 0.1 s that observe wake order 0.1 then 0.3 and `virtual_ms()` 100 then 300).
  - `clock_for(mode: ClockMode) -> SimClock`.
- `src/va_sim/scenario.py`:
  - Pydantic v2 models, all `ConfigDict(extra="forbid", frozen=True)`:
    `Match(to: E164 | None, inbound_from: E164 | None)` — exactly one set (model_validator).
    `Answer(after_ms: int ≥0 | None, result: Literal["answered","busy","no_answer","rejected","failed"]="answered",
    answered_by: Literal["human_residence","human_business","machine","silence","fax_detected"]|None,
    beep_at_ms: int≥0|None)` — `after_ms` required iff result == answered; `answered_by`/`beep_at_ms` forbidden unless
    answered; `beep_at_ms` only with `answered_by == "machine"`.
    Steps: `WaitAgentSpeech`, `Say(text min 1, language="en", barge_in_after_agent_ms ≥0|None)`, `Dtmf(digits, inband)`,
    `Silence(ms ≥0)`, `ExpectHangup(by, timeout_ms)`, `Hangup()`; wrapper models with one required key each
    (`WaitAgentSpeechStep(wait_agent_speech: WaitAgentSpeech)` etc.) and `Step = Annotated[Union[...],
    Field(union_mode="left_to_right")]`; a `model_validator(mode="before")` on the step list item (or a
    `BeforeValidator`) rejects mappings with ≠1 key with message `"a step must have exactly one key, one of: say,
    wait_agent_speech, dtmf, silence, expect_hangup, hangup"` so errors are precise instead of a union dump.
    `TransferTarget(answer_after_ms ≥0 | None, result (same literal) = "answered", steps: tuple[Step,...] = ())`.
    `Assertions(call_status, answered_by: Literal["human","machine"]|None, end_reason: str|None, min_turns ≥0|None,
    transcript_contains: tuple[str,...]=(), variables: dict[str, JsonValue]={}, max_e2e_ms_p95 >0|None,
    webhook_events: tuple[str,...]=())` — all optional.
    `Scenario(id: pattern ^[a-z0-9][a-z0-9_-]{0,63}$, description="", match, answer: Answer|None=None,
    greeting_audio: str|None, llm_rules: tuple[str,...]=(), steps: tuple[Step,...]=(), transfer_target|None,
    profile: Literal["fast","realistic"]="fast", assertions: Assertions = Assertions())`. Inbound scenarios
    (match.inbound_from) must not set `answer` (platform answers) — validator.
  - `@dataclass(frozen=True) ScenarioIssue(path: str, line: int | None, column: int | None, message: str)`;
    path format `steps[2].say.text` (same style as va_domain `field_path` — reuse it if importable without a new dep;
    otherwise local helper). `class ScenarioError(ValueError)` with `source: str`, `issues: tuple[ScenarioIssue,...]`,
    `__str__` = one line per issue `"{source}:{line}:{column}: {path}: {message}"` (1-based line/col).
  - `load_scenario(text: str, *, source: str = "<string>", base_dir: Path | None = None) -> Scenario`:
    `yaml.compose` with `SafeLoader` to keep the node tree (YAMLError → issue with `problem_mark` line/col, path
    `""`); reject non-mapping root; reject duplicate keys (walk MappingNodes) with the duplicate's line; then
    `yaml.safe_load` → `Scenario.model_validate`; on `ValidationError` map each error `loc` to the node via
    the composed tree (descend MappingNode by key, SequenceNode by index; fall back to the deepest existing node,
    and for "missing" errors use the parent mapping's start mark). `llm_rules` entries resolved against `base_dir`
    when given (stored as absolute POSIX strings).
  - `load_scenario_file(path: Path) -> Scenario` (source = path str, base_dir = path.parent; reads via
    `Path.read_text`; callers in async code wrap with `asyncio.to_thread`), `load_scenarios(directory) ->
    dict[str, Scenario]` rejecting duplicate ids (ScenarioError).
  - `scenario_json_schema() -> dict` (`Scenario.model_json_schema()`) for docs/tests.
- `src/va_sim/state.py`: `SimState` dataclass-ish class:
  `seed`, `rng: random.Random`, `clock: SimClock`, `latencies: Latencies` (mutable copy of settings/profile),
  `scenarios: dict[str, Scenario]`, `register_scenario(s)` (replaces same id, indexes by `match.to` /
  `match.inbound_from`), `scenario_for_destination(to) -> Scenario|None`, `scenario_for_inbound(from_)`,
  `recordings: dict[str, bytes]` (`store_recording(id, wav)`), `utterances: dict[int, Utterance]` with
  `register_utterance(text, language, words) -> int` (sequential from 1), `reset(seed: int | None = None)` clears
  everything and reseeds, `snapshot() -> dict[str, JsonValue]` (counts + scenario ids + seed + clock mode +
  latencies) used by `GET /sim/state`.
- `src/va_sim/wav.py`: `pcm16_wav(channels: Sequence[bytes], sample_rate: int) -> bytes` (interleaves equal-length
  PCM16 LE channels, pads shorter with zeros; stdlib `wave` + `io.BytesIO`) — used by recordings now and FakeTelnyx later.
- `src/va_sim/control.py`: Starlette routes (JSON errors `{"error": {"code", "message", "issues"?}}`):
  - all sims: `POST /sim/reset` (optional JSON `{"seed": int}`; 204), `GET /sim/clock` →
    `{"mode","now" (ISO-8601 Z),"virtual_ms"}`, `GET /sim/state` → snapshot.
  - per-sim config: `GET/PATCH /sim/{sim}/config` for `elevenlabs` (`tts_ttfa_ms`, `stt_final_ms`) and `llm`
    (`ttft_ms`, `tokens_per_s`), plus `POST /sim/{sim}/config/profile` `{"profile":"fast"|"realistic"}` — pydantic
    patch model with extra=forbid → 422 with issues. Only mounted on the matching sim's app.
  - telnyx app only: `POST /sim/telnyx/scenarios` (body `application/yaml`/`text/yaml` → `load_scenario`, or JSON →
    `Scenario.model_validate` with issues mapped to paths, line None) → 201 `{"id", "match"}`; invalid → 422 with
    `issues: [{path,line,column,message}]`; `GET /sim/telnyx/scenarios` (list), `GET /sim/telnyx/scenarios/{id}`
    (model_dump mode=json, 404 if unknown); `GET /sim/recordings/{recording_id}.wav` → `audio/wav` from
    `state.recordings`, 404 `{"error":{"code":"not_found",...}}`.
  - `control_routes(sim: SimName, state: SimState) -> list[BaseRoute]`.
- `src/va_sim/server.py`:
  - `SimName = Literal["telnyx","elevenlabs","llm","stripe"]`, `SIMULATORS`, `BASE_PORT=9100`, `PORT_STEP=100`,
    `ports_from(base)` — move these from `va_sim_server.main` (main re-imports them; keep its `__all__`).
  - `simulator_routes(sim, state) -> list[BaseRoute]` (control routes now; later tasks add vendor routes here).
  - `build_state(settings) -> SimState` (clock from `settings.sim_clock`, latencies from settings).
  - `@asynccontextmanager running_sims(state, *, host="127.0.0.1", ports: Mapping[SimName,int] | None = None)
    -> AsyncIterator[dict[SimName, str]]`: starts one `uvicorn.Server` per sim (port 0 → ephemeral) inside a TaskGroup
    using `va_common.service.run_server`/StopController if it supports port 0 and reports bound port (check
    `RecordingServer` in service_helpers for how tests read `server.url`); yields base URLs; stops servers on exit.
    This is the §2 `sims` fixture core.
- `apps/sim/src/va_sim_server/main.py`: `create_app(source, sim, state)` = `probe_app()` + `app.router.routes.extend(
  simulator_routes(sim, state))`; `run()` builds one `SimState` and passes it to all four apps. Update docstrings.
- `apps/sim/pyproject.toml`: rename (decision 2), add dep `va-sim` + `[tool.uv.sources] va-sim = { workspace = true }`.
- `packages/va_sim/tests/fixtures/outbound_basic.yaml` — the specs/30 §3.5 example verbatim;
  `fixtures/inbound_ivr.yaml` (inbound_from, transfer_target, dtmf inband, hangup step).
- AGENTS.md: add a "Simulators" note: `/sim/*` control API, `load_scenario` errors are `source:line:col: path:
  message`, `running_sims` for in-process sims, accelerated clock semantics, dist names va-sim / va-sim-server.

### Tests (`packages/va_sim/tests/`, conftest isolating SimSettings env via `service_helpers.isolate_env`)

- `test_settings.py`: defaults equal specs/02 §6 values; `SIM_LLM_RULES="a.yaml, ,b.yaml"` → two paths; profiles
  fast/realistic values; invalid `SIM_CLOCK=warp` rejected; `sim_workers=0` rejected.
- `test_clock.py`: RealtimeClock now is aware UTC and virtual_ms grows (no sleeping: monkeypatch time.monotonic);
  AcceleratedClock sleep(1.5) → virtual_ms 1500, now = start+1.5 s, returns without wall delay (assert loop time
  delta < 0.5 s); two concurrent sleepers wake in deadline order; negative sleep/advance raise; `clock_for` modes.
- `test_scenario.py`:
  - `test_spec_example_loads`: extract the first ```yaml block after `### 3.5` from `specs/30-testing-and-evals.md`
    (repo root via `Path(__file__).parents[3]`) and load it → id `outbound_basic`, 7 steps of the right types,
    `answer.answered_by == "human_residence"`, `assertions.webhook_events == ("call.completed",)`.
  - fixture files load; `llm_rules` resolved relative to the file dir.
  - precise errors (assert exact `path`, `line`, `column`, message substring):
    unknown top-level key (`colour:` at line N); bad enum `answer.result: hung`; `answer` answered without `after_ms`
    (line of `answer:`); `beep_at_ms` with human answer; both `match.to` and `match.inbound_from`; non-E.164 `to`;
    step with two keys (`steps[1]`); unknown step key `sing:`; negative `silence.ms`; wrong type
    `min_turns: many`; YAML syntax error (unclosed quote) with line/col from the marker; duplicate key `id:` twice;
    root is a list; multiple issues reported together (≥2 issues in one error) and `str(error)` format.
  - `load_scenarios` duplicate id across two files → ScenarioError naming both sources; JSON schema has `steps`.
- `test_state.py`: register/replace by id updates destination index; inbound lookup; utterance ids sequential from 1;
  `reset(seed=7)` clears everything and two states with the same seed produce the same `rng.random()` sequence;
  snapshot shape.
- `test_wav.py`: 2-channel output readable by `wave`, frames interleaved, shorter channel zero-padded.
- `test_control.py` (httpx ASGITransport on a Starlette app of `simulator_routes`): reset 204 + optional seed;
  clock endpoint in accelerated mode reflects `advance`; register YAML scenario 201 then GET returns it; invalid YAML
  → 422 with issue lines; JSON body path; unknown scenario 404; recording 404 then 200 `audio/wav` after
  `store_recording`; config PATCH on llm/elevenlabs updates `state.latencies`, unknown key 422, profile switch;
  telnyx-only routes absent on the llm app (404).
- `test_server.py`: `running_sims(state, ports=dict.fromkeys(SIMULATORS, 0))` yields four distinct URLs; a scenario
  registered on telnyx is visible via `GET /sim/state` on the llm port (shared state); context exit stops servers.
- `apps/sim/tests/test_sim.py`: update imports; add a test that each app from `create_app(..., sim, state)` serves
  `/sim/clock` alongside `/readyz`, and that only the telnyx app serves `/sim/telnyx/scenarios`.

### Gates
`uv lock && uv sync`, `make fmt lint typecheck placeholders test` (coverage of va_sim ≥ 85 %), jscpd (`make check`
runs it) — reuse `service_helpers` for env isolation rather than copying.

### Risks
- uv workspace rename: lockfile churn; make sure `uv sync` installs both `va-sim` and `va-sim-server`.
- yaml line mapping for pydantic `loc` with union steps: the left_to_right union yields locs like
  `('steps', 1, 'SayStep', 'say', 'text')` — strip model-class segments (non-int entries not present as keys in the
  node) when walking; test it explicitly.
- `check_no_placeholders` flags `example.com`; scenario fixtures use `+1415555xxxx` numbers only.
- Accelerated clock concurrency is the subtle part; keep it small and fully tested.

## Build P03-T01 (Builder, 2026-10-29)

Implemented per plan, with these deviations (all recorded in specs/30 Decisions or AGENTS.md):
- `SimName`/`SIMULATORS` live in `va_sim.settings` (re-exported by `va_sim.server`) so control.py can use them
  without an import cycle.
- Step union uses a callable `Discriminator` with tags `step:<key>` plus a `BeforeValidator` (instead of a
  left_to_right union): gives one precise error per bad step; tags are stripped from paths.
- Issues are sorted by (line, column) so errors read top to bottom (pydantic reports in field order).
- `va_common.service.ReadyServer` added (ready event + bound url); `service_helpers.RecordingServer` is now an alias
  (avoids a copied class for `running_sims`).
- Shared `isolated_env` fixture moved into `packages/va_sim/tests/support/sim_helpers.py`; both conftests re-export
  it (the copies were a jscpd clone).
- Outbound fixture is `outbound_voicemail.yaml` (machine + beep) instead of a verbatim copy of the spec example;
  the spec example itself is extracted from specs/30 in `test_spec_example_loads` (the verbatim copy was a jscpd clone).
- specs/30 §3 table webhook URL defaults aligned to specs/02 §6 (localhost); compose sets edge:/api: hosts.
- `RealtimeClock(monotonic=...)` is injectable (tests don't patch `time.monotonic` globally).
Gates: make fmt/lint/typecheck/placeholders/test all exit 0; 3519 passed, total cov 97.72 %, va_sim modules 94-100 %;
openapi drift + spec refs clean; jscpd 0 clones; radon avg A (2.35); pip-audit + pnpm audit: no vulnerabilities.

## Review P03-T01 (Reviewer, 2026-10-29)

Approved. Re-ran make lint/typecheck/placeholders (exit 0) and make test (3519 passed, cov 97.72 %, va_sim 94-100 %).
Diff checked against specs/30 §3 table (all SIM_* vars, defaults per specs/02 §6), §3.5 DSL (every field present,
spec example loads), and the control API paths in §3/§3.2. No placeholders, swallowed exceptions, or blocking I/O
(YAML parse goes through asyncio.to_thread). Error-path tests cover each validation rule with exact line/column.

## Plan P03-T02a — FakeTelnyx REST resources, auth, and signed webhook emitter (Planner, 2026-10-29)

P03-T02 was split (DEC-005, plan/phase-03.md): T02a = keys + auth/accounts + errors + provisioning/number/message
REST + webhook emitter + supervised tasks; T02b = dial/call actions/conferences/scenario progress/AMD/inbound/call
control API. T02a must leave the emitter, id generation, task supervisor, and error helpers ready for T02b.

### Decisions (Builder writes them as one `> Decision (2026-10-29):` block at the end of specs/30 §3.2)
1. Error codes (Telnyx-shaped `{"errors":[{"code","title","detail","source"?:{"pointer"}}]}`): `10009`
   "Authentication failed" (401), `10005` "Resource not found" (404), `10015` "Invalid value" (422, missing/invalid/
   unknown field; pointer = JSON pointer e.g. `/phone_numbers/0/phone_number`), `10007` "Unexpected error" not used,
   `40305` "Invalid 'from' address" (422, `/from`), `85001` "Number not available" (422) for ordering a number
   that is not in the inventory. `90018` "Call has already ended" belongs to T02b. Unknown body fields are
   rejected with `10015` (strict, so client typos fail contract tests).
2. Resources are per account: each API key (`sim-telnyx-key` built in, plus keys added with
   `POST /sim/telnyx/accounts {"api_key"}` → 201 `{"api_key"}`) owns its apps/profiles/numbers/orders/messages;
   another account's id → 404. `/sim/reset` drops registered accounts and all resources.
3. Webhook targets: messages → request `webhook_url` (when `use_profile_webhooks` is false or the profile has no URL),
   else the messaging profile `webhook_url`, else `SIM_MESSAGING_WEBHOOK_URL`; `number_order.complete` → the order's
   call control application `webhook_event_url` when `connection_id` names one, else `SIM_EDGE_WEBHOOK_URL`.
   Inbound `message.received` → owning number's messaging profile `webhook_url`, else `SIM_MESSAGING_WEBHOOK_URL`.
4. Retries: first attempt inline (keeps per-resource event order); on non-2xx or transport error the emitter
   re-sends after 1 s and then 3 s on the sim clock (attempts 2, 3; `meta.attempt` increments; re-signed with a
   fresh timestamp; same event `id`), then gives up. `force_duplicate_webhooks` sends every successful first attempt
   a second time (same id, `meta.attempt` 1).
5. Signing key: `SIM_ED25519_PRIVATE_KEY` is base64 of the 32-byte Ed25519 seed (64-byte seed+public also accepted
   when the public half matches); otherwise generated with `os.urandom` at state creation and kept across
   `/sim/reset`. Public key = base64 raw 32 bytes, `GET /sim/telnyx/public-key` → `{"public_key"}`; `apps/sim`
   writes it (+ `\n`) to `.sim/telnyx_public_key` relative to the working directory at startup.
6. `filter[limit]` default 10, max 50 (>50 → 10015). `filter[name]` on messaging profiles is exact; the other
   `[contains]` filters are case-insensitive substring. Lists use `page[number]` (default 1) / `page[size]`
   (default 20, max 250) with `meta {"page_number","page_size","total_pages","total_results"}`.
7. Message `parts`: GSM-7 (basic + extension table, extension chars count 2) 160/153 septets, else UCS-2 70/67
   UTF-16 code units; `encoding` `GSM-7`|`UCS-2`. Computed locally in the sim (not imported from va_domain).
8. Number orders finish 1 s (sim clock) after creation: `success` (numbers become owned `phone_number`s with the
   order's `connection_id`/`messaging_profile_id`/`customer_reference`) or `failure` when the telnyx config
   `fail_number_orders` is true (numbers return to inventory).

### Files
- `packages/va_sim/pyproject.toml`: add deps `cryptography>=50.0.2`, `httpx[http2]>=0.28.1`.
  Root `pyproject.toml` dev group: add `pynacl>=1.5` (test-only independent verifier; listed in specs/02 §5 as
  allowed). `uv lock && uv sync`.
- `src/va_sim/tasks.py` — `class Supervisor`: `spawn(coro, *, name) -> asyncio.Task[None]` (keeps a set, done
  callback removes and logs non-cancel exceptions via structlog `sim.task_failed` with name; never re-raises),
  `cancel_all() -> None` (sync, for reset), `async aclose()` (cancel + `asyncio.gather(..., return_exceptions=True)`),
  `pending -> int`, `async drain()` (await until no tasks remain, for tests; loops because tasks spawn tasks).
- `src/va_sim/ids.py` — `SimIds(rng)`: `uuid() -> str` (`uuid.UUID(int=rng.getrandbits(128), version=4)`),
  `token(n) -> str` (url-safe base64 of rng bytes; T02b uses it for `v3:` ccids). All ids come from the seeded rng.
- `src/va_sim/telnyx/__init__.py` — docstring, `telnyx_routes(state) -> list[BaseRoute]` (REST under `/v2` + the
  telnyx control routes below; `control._telnyx_routes` stays for scenarios/recordings).
- `src/va_sim/telnyx/keys.py` — `PUBLIC_KEY_FILE: Final = Path(".sim/telnyx_public_key")`;
  `class SigningKey` (wraps `cryptography` `Ed25519PrivateKey`): `generate()`, `from_base64(secret: str)`
  (ValueError on bad length/base64/mismatch), `public_key_b64 -> str`, `sign(message: bytes) -> str` (base64),
  `signature_headers(timestamp: int, body: bytes) -> dict[str, str]` (`telnyx-signature-ed25519` over
  `f"{timestamp}|".encode() + body`, `telnyx-timestamp`), `write_public_key(path) -> None` (mkdir parents).
- `src/va_sim/telnyx/errors.py` — `TelnyxIssue(code, title, detail, pointer|None)`, `telnyx_error(status, *issues)
  -> JSONResponse`, helpers `not_found(kind, id)`, `unauthorized()`, `invalid(detail, pointer)`, and
  `issues_from_validation(ValidationError) -> list[TelnyxIssue]` (loc → `/a/0/b`, missing → "is required",
  extra_forbidden → "is not a recognised parameter").
- `src/va_sim/telnyx/http.py` — `data(obj, status=200)`, `page(items, request)` (pagination per decision 6),
  `parse_body(request, model) -> M | JSONResponse` (empty/invalid JSON → 10015 pointer ""), `authenticated(store)`
  decorator or `account_for(request, store) -> Account | JSONResponse` (Bearer header, case-insensitive scheme).
- `src/va_sim/telnyx/models.py` — request pydantic models (`extra="forbid"`): `CallControlAppCreate/Update`
  (fields of specs/06 §2.1 row incl. nested `inbound {channel_limit, sip_subdomain_receive_settings, ...}` and
  `outbound {outbound_voice_profile_id, channel_limit}`; Literal sets: `webhook_api_version` "1"|"2",
  `dtmf_type` "RFC 2833"|"Inband"|"SIP INFO", `anchorsite_override` "Latency"|"Chicago, IL"|"Ashburn, VA"|
  "San Jose, CA"|"Amsterdam, Netherlands"|"London, UK"|"Toronto, Canada"|"Sydney, Australia",
  `webhook_timeout_secs` 0–30, `first_command_timeout_secs` 0–300), `OutboundVoiceProfileCreate/Update`
  (`traffic_type` conversational|short_duration, `service_plan` global|us|international, `concurrent_call_limit`
  ≥1|None, `whitelisted_destinations` ISO-3166 alpha-2 list, `enabled`, `tags`), `MessagingProfileCreate/Update`
  (`name`, `webhook_url`, `webhook_failover_url`, `webhook_api_version` "1"|"2"|"2010-04-01",
  `whitelisted_destinations` required on create, `enabled`), `NumberOrderCreate` (`phone_numbers` 1..20 of
  `{phone_number: E164}`, `connection_id`, `messaging_profile_id`, `customer_reference`), `PhoneNumberUpdate`
  (`connection_id`, `tags`, `customer_reference`, `external_pin`), `PhoneNumberMessagingUpdate`
  (`messaging_profile_id`), `MessageCreate` (`from_` alias from, `to`, `text`, `media_urls`, `subject`, `type`
  SMS|MMS, `messaging_profile_id`, `webhook_url`, `webhook_failover_url`, `use_profile_webhooks`=True, `auto_detect`;
  model_validator: text or media_urls required), sim control models `AccountCreate`, `TelnyxSimConfig`
  (`force_duplicate_webhooks: bool`, `fail_number_orders: bool`, `failing_message_destinations: tuple[E164,...]`),
  `InboundMessage` (`from_`, `to`, `text`, `media_urls`). E164 = `^\+[1-9]\d{6,14}$` (define locally; do not import
  va_domain).
- `src/va_sim/telnyx/store.py` — `@dataclass Account(api_key, apps, voice_profiles, messaging_profiles,
  numbers (by id), orders, messages: dict[str, dict]` storing response-shaped JSON dicts) and `TelnyxStore`:
  `accounts: dict[str, Account]` (seeded with `DEFAULT_API_KEY = "sim-telnyx-key"`), `config: TelnyxSimConfig`,
  `inventory: dict[str, AvailableNumber]` built by `inventory.seed_inventory()`, `reserved: set[str]`,
  `owner_of(e164) -> tuple[Account, dict] | None`, `reset()`.
- `src/va_sim/telnyx/inventory.py` — `AvailableNumber` frozen dataclass (phone_number, country_code, ndc,
  phone_number_type, locality, administrative_area, features, monthly_cost, upfront_cost) and
  `seed_inventory() -> tuple[AvailableNumber, ...]`: 50 each of `+1415555` (San Francisco, CA, local, voice/sms/mms),
  `+1212555` (New York, NY, local, voice/sms/mms), `+1888555` (toll_free, voice/sms; locality None), `+4420` 7946
  `0xxx` (London, GB, national, voice); suffixes 2000–2049 for US (`+14155552000`…), `+442079460000`…; deterministic
  (no rng). `to_json(n) -> dict` per specs/30 §3.2 (`record_type: available_phone_number`, `features` list of
  `{"name"}`, `cost_information {monthly_cost, upfront_cost, currency:"USD"}`, `region_information`
  `[{region_type: country_code|state|location|rate_center, region_name}]`, `best_effort:false`, `quickship:true`,
  `reservable:true`, `vanity_format:""`). `search(numbers, filters) -> list`.
- `src/va_sim/telnyx/resources.py` — route handlers for: `GET /v2/balance` (`record_type: balance`, `balance
  "100.00"`, `credit_limit "0.00"`, `available_credit "100.00"`, `pending "0.00"`, `currency "USD"`);
  `/v2/call_control_applications` POST/GET + `/{id}` GET/PATCH (defaults from specs/06 §2.1 when omitted;
  outbound_voice_profile_id must exist in the account → 10015 `/outbound/outbound_voice_profile_id`; response adds
  `id` (numeric string, Telnyx style: 19 digits from rng), `record_type: call_control_application`, `active: true`,
  `created_at`/`updated_at` ISO Z from `state.clock.now()`; nested PATCH merges);
  `/v2/outbound_voice_profiles` POST/GET + `/{id}` GET/PATCH (`record_type: outbound_voice_profile`,
  `connections_count` = apps referencing it, `usage_payment_method: "rate-deck"`, `daily_spend_limit_enabled:false`);
  `/v2/messaging_profiles` POST/GET + `/{id}` GET/PATCH (`record_type: messaging_profile`, uuid id, `v1_secret`
  omitted); `GET /v2/available_phone_numbers` (filters of specs/06 §2.1 search_numbers row; `filter[features][]`
  repeated → all must be present; unknown `filter[phone_number_type]` → 10015; owned/reserved excluded;
  `meta {"total_results","best_effort_results":0}`); `POST /v2/number_orders` (validate numbers available, not
  duplicated; reserve; 200 `{"data": order}`, order `status: pending`, each `phone_numbers[i] {id, record_type:
  number_order_phone_number, phone_number, status: pending, regulatory_requirements: []}`; spawn completion task:
  `await clock.sleep(1.0)` → success/failure → emit `number_order.complete` with payload = order);
  `GET /v2/number_orders/{id}`; `GET /v2/phone_numbers` (`filter[phone_number]` exact, `filter[tag]`,
  pagination), `GET/PATCH/DELETE /v2/phone_numbers/{id}` (connection_id must be an app of the account; DELETE → 200
  data with `status: "deleted"`, number released to inventory), `PATCH /v2/phone_numbers/{id}/messaging`
  (profile must exist → 10015; response `record_type: messaging_phone_number`, `id`, `phone_number`,
  `messaging_profile_id`, `updated_at`; also updates the number).
- `src/va_sim/telnyx/messages.py` — `segment_count(text) -> tuple[encoding, parts]` (decision 7);
  `POST /v2/messages`: `from` must be owned by the account (else 40305 `/from`); profile = request
  `messaging_profile_id` or the number's (missing both → 10015 `/messaging_profile_id`); response message per
  specs/30 §3.2 (`record_type: message`, `direction: outbound`, `id` uuid, `type` (MMS when media_urls),
  `from {phone_number, carrier: "Telnyx", line_type: "Wireless"}`, `to: [{phone_number, status: "queued", carrier,
  line_type}]`, `text`, `subject`, `media: [{url, content_type: null, sha256: null, size: null}]`, `messaging_profile_id`,
  `webhook_url`, `webhook_failover_url`, `encoding`, `parts`, `tags: []`, `cost: null`, `received_at`, `sent_at: null`,
  `completed_at: null`, `valid_until` (+24 h), `errors: []`); spawn lifecycle: +200 ms `message.sent`
  (status `sent`, `sent_at`) then +400 ms `message.finalized` (`delivered`, `completed_at`) or, when `to` is in
  `failing_message_destinations`, `delivery_failed` with `errors: [{"code": "40008", "title": "Undeliverable",
  "detail": "The destination carrier rejected the message."}]`; message stored and updated in place.
  `GET /v2/messages/{id}`. Control: `POST /sim/telnyx/messages/inbound` → emits `message.received` (direction
  inbound, `to[0].status: "webhook_delivered"`, `from.phone_number`), 202 `{"id"}`; 422 when `to` is not owned.
- `src/va_sim/telnyx/webhooks.py` — `WebhookDelivery` frozen dataclass (event_id, event_type, url, attempt,
  status_code|None, error|None, delivered_at ISO); `class WebhookEmitter(state-free: signing_key, clock, ids,
  supervisor, config getter, transport: httpx.AsyncBaseTransport | None)`: `async emit(url, event_type, payload)
  -> str` builds envelope `{"data":{"record_type":"event","id","event_type","occurred_at","payload"},
  "meta":{"attempt","delivered_to"}}` (orjson bytes, signed per attempt with `int(clock.now().timestamp())`),
  POSTs with `httpx.AsyncClient(timeout=httpx.Timeout(5.0), transport=transport)` (lazily created, `aclose()`),
  records each attempt in `deliveries` (bounded deque 10 000), schedules retries (decision 4) via supervisor.
- `src/va_sim/telnyx/control.py` — telnyx-only control routes: `GET /sim/telnyx/public-key`,
  `POST /sim/telnyx/accounts`, `GET/PATCH /sim/telnyx/config` (TelnyxSimConfig, partial patch, 422
  `invalid_request` with issues via the existing `{"error":...}` sim format), `GET /sim/telnyx/webhooks`
  (`{"deliveries":[...]}`), `GET /sim/telnyx/messages` (`{"messages":[...]}` all accounts),
  `POST /sim/telnyx/messages/inbound`. Reuse control.py `_error`/`_json_body` (make them public:
  `sim_error`, `json_body` in `va_sim.control` — no copies, jscpd).
- `src/va_sim/state.py`: constructor gains `signing_key: SigningKey | None = None` (generate when None),
  `webhook_defaults: WebhookDefaults = WebhookDefaults()` (frozen dataclass in `va_sim.settings`: `voice_url`,
  `messaging_url` defaults = specs/02 §6 values), `webhook_transport: httpx.AsyncBaseTransport | None = None`;
  attributes `tasks: Supervisor`, `ids: SimIds`, `telnyx: TelnyxStore`, `webhooks: WebhookEmitter`.
  `reset()` also `tasks.cancel_all()`, `telnyx.reset()`, `webhooks.clear()` (key kept). New `async aclose()`
  (supervisor + emitter client). `snapshot()` adds `"telnyx": {"accounts", "messages", "phone_numbers",
  "webhook_deliveries"}` counts.
- `src/va_sim/server.py`: `simulator_routes("telnyx", ...)` = control routes + `telnyx_routes(state)`;
  `build_state(settings)` passes `SigningKey.from_base64(secret)` when `sim_ed25519_private_key` is set and
  `WebhookDefaults` from settings; `running_sims` awaits `state.aclose()` in `finally` after servers stop.
- `apps/sim/src/va_sim_server/main.py`: `run()` writes the public key (`await asyncio.to_thread(
  state.signing_key.write_public_key, public_key_file)`; new keyword `public_key_file: Path = PUBLIC_KEY_FILE`)
  before serving, logs `sim.public_key_written`, and `await state.aclose()` on exit (try/finally).
- AGENTS.md: FakeTelnyx notes (auth key, accounts, `/sim/telnyx/config` knobs, `webhook_transport` injection for
  tests, `state.tasks.drain()`, public key file).

### Tests (`packages/va_sim/tests/telnyx/`, httpx `ASGITransport` over `Starlette(routes=simulator_routes(
"telnyx", state))` with `AcceleratedClock`; helper `telnyx_client(state)` + `RecordingTransport` (httpx
`MockTransport` with a programmable status list, records `(url, headers, body)`) in `tests/support/telnyx_helpers.py`;
test-local response shapes in `tests/support/telnyx_shapes.py` (pydantic `extra="allow"` models of every resource
listed above, used by `assert_shape(model, response)`)
- `test_keys.py`: generated key signs; pynacl `VerifyKey(b64decode(pub)).verify(f"{ts}|".encode()+body, sig)`
  passes; `from_base64` 32 and 64 byte forms, mismatched 64-byte → ValueError, bad base64 → ValueError;
  `write_public_key` creates parent dir; same seed b64 → same public key.
- `test_auth.py`: missing header / wrong key → 401 `10009` shape; registered account works; resources isolated
  across accounts (404 for other account's app id); reset drops accounts.
- `test_provisioning.py`: create app with only required fields → defaults of specs/06 §2.1; unknown field → 422
  10015 with pointer; bad `dtmf_type` → pointer `/dtmf_type`; outbound profile id unknown → 10015; list filter
  `[application_name][contains]` case-insensitive; PATCH merges nested `outbound`; OVP create/list/patch,
  `connections_count`; messaging profile create requires `whitelisted_destinations`, `filter[name]` exact; 404s.
- `test_numbers.py`: search by country/ndc/contains/features/type/locality/limit; limit 51 → 10015; order →
  pending, then after `clock` advance + `state.tasks.drain()` success, number owned, gone from search,
  `number_order.complete` delivered and signed; `fail_number_orders` → failure + numbers back in inventory; ordering
  an unavailable/duplicate number → 85001/10015; phone_numbers filter/patch (connection_id must exist, tags)/
  messaging patch/delete (released to search).
- `test_messages.py`: segment counts (160/161 GSM, extension char, 70/71 UCS-2, emoji); send → queued response
  shape; `message.sent` then `message.finalized delivered` in order with timestamps 200/600 ms after send (accelerated
  clock `virtual_ms`); failing destination → `delivery_failed` + errors; webhook URL precedence (decision 3);
  from not owned → 40305; neither text nor media → 10015; inbound control route emits `message.received` to the
  profile URL; `GET /v2/messages/{id}` reflects final status.
- `test_webhooks.py`: envelope shape; headers verify with pynacl; tampered body fails pynacl; receiver 500 then
  200 → attempts 1 and 2 at +1 s, same event id, `meta.attempt` 2; always 500 → 3 attempts (+1 s, +3 s) then stop;
  transport error retried; `force_duplicate_webhooks` → two identical-id deliveries; delivery log endpoint; reset
  cancels pending retries (supervisor pending == 0).
- `test_tasks.py`: failing task logged not raised; `cancel_all`/`aclose`/`drain`.
- `test_control_telnyx.py`: public-key endpoint matches state key; config GET/PATCH + unknown key 422; accounts 201;
  telnyx control routes absent on the llm app.
- Update `test_state.py` (snapshot shape, reset keeps key), `test_server.py` (running_sims closes state;
  `build_state` with `SIM_ED25519_PRIVATE_KEY` uses that key), `apps/sim/tests/test_sim.py` (run writes the public
  key file to `tmp_path`, `/v2/balance` served by the telnyx app with the sim key).

### Gates
`uv lock && uv sync`; `make fmt lint typecheck placeholders test`; `make check` (jscpd clean, coverage va_sim
≥ 85 %). No e2e needed (no call/SMS platform flow yet).

### Risks
- Determinism: never use `uuid4()`/`secrets` for ids — only `state.ids` (rng); key generation is the one exception.
- Accelerated clock + background tasks: tests must `await state.tasks.drain()` after advancing; retries sleep on
  `state.clock`, so `AcceleratedClock` makes 1 s/3 s instant — assert `virtual_ms` deltas, never wall time.
- jscpd: resource CRUD handlers for three profile types are near-identical — write one generic
  `ResourceCollection` (create/list/get/patch parameterised by models, record_type, id factory, filters, defaults)
  rather than three copies.
- `check_no_placeholders` flags `example.com` — use `+1415555xxxx` numbers and `http://localhost`/`edge` hosts.
- Keep the simulator independent of va_telnyx/va_domain (no shared message construction).

## Build P03-T02a (Builder, 2026-10-29)

Implemented per plan; deviations (all recorded in the specs/30 §3.2 Decision block or AGENTS.md):
- `telnyx_routes` lives in `va_sim/telnyx/routes.py` (package `__init__` stays import-free: `va_sim.state` imports
  `telnyx.store/webhooks/keys`, and handler modules import `SimState` only under TYPE_CHECKING → no cycle).
  `simulator_routes` appends it for `telnyx`; `va_sim.control` exports `sim_error`, `invalid_request`, `json_body`.
- REST split into `resources.py` (balance + generic `Collection[M]` for apps/OVPs/messaging profiles: PATCH =
  deep-merge into stored fields + re-validate with the create model, so no Update models), `numbers.py` (search,
  orders, phone numbers), `messages.py`. Query-parameter errors use `source.parameter` (body errors `pointer`).
- `httpx` dep without the `[http2]` extra (the emitter needs plain HTTP/1.1). `pynacl>=1.5` in the root dev group.
- Retry timing: attempt 2 is 1 s after attempt 1, attempt 3 is 3 s after attempt 2 (virtual 0/1000/4000 ms);
  payloads are deep-copied at emit so a retry carries the payload as emitted.
- `SimSettings.webhook_defaults()` + `WebhookDefaults` (settings); blank `SIM_ED25519_PRIVATE_KEY` (the .env.example
  value) means generate. `iso_z` moved to `va_sim.clock` (shared by control + telnyx).
- `Supervisor.drain` yields a loop turn per iteration: Python 3.12 `gather` over done tasks does not yield (busy loop).
- Inbound `message.received` is emitted inline before the 202 (first attempt), retries via the supervisor.
Tests: packages/va_sim/tests/telnyx/ (keys, auth, provisioning, numbers, messages, webhooks, control), test_tasks,
test_ids, updated test_state/test_server/apps/sim test_sim; support telnyx_helpers.py + telnyx_shapes.py.

## Review P03-T02a (Reviewer, 2026-10-29)

Gates re-run: `make lint` clean, `make typecheck` (mypy 451 files, tsc x3) clean, `make placeholders` 0 findings;
`make test` 3615 passed, coverage 97.87 %. Design, supervision, signing, retries, determinism and tests otherwise look good.

Rejected — findings:
1. `packages/va_sim/src/va_sim/telnyx/numbers.py` `complete_order`: a failed order sets each
   `phone_numbers[i].status` to `"failed"`. Telnyx `NumberOrderPhoneNumber.status` is
   `pending | success | failure` (same set as the order status; verified on developers.telnyx.com "Retrieve a number
   order"; specs/06 §2.1 `get_number_order` lists `pending`/`success`/`failure`). Use `"failure"`. The wrong value is
   also in the test contract: `tests/support/telnyx_shapes.py:129` (`Literal["pending","success","failed"]`) and
   `tests/telnyx/test_numbers.py:172`. Fix both, otherwise `va_telnyx` (P03-T08) gets contract-tested against a
   status Telnyx never sends.
2. `specs/30-testing-and-evals.md` §3.2 Decision (2026-10-29), item (6): it says "Application and outbound voice
   profile ids are 19-digit numeric strings, other ids UUIDs", but `phone_number` record ids come from
   `state.ids.digits()` (numeric, which matches real Telnyx). Make the Decision text match: phone number ids are
   numeric too; message, messaging profile, number order, and order-entry ids are UUIDs.

## Fix P03-T02a review findings (Builder, 2026-10-29)

1. Failed number order entries now carry `status: "failure"` (numbers.py `complete_order`), shape Literal and
   test_numbers assertion updated.
2. specs/30 §3.2 Decision (6): application, OVP and phone number ids are 19-digit numeric; message, messaging
   profile, number order, order-entry and webhook event ids are UUIDs.
Gates: fmt/lint/typecheck clean, placeholders 0, make test 3615 passed, coverage 97.87 %.

## Plan P03-T02a (re-plan after build.blocked, Planner, 2026-10-29)

The block is not a design blocker. The loop's backpressure check rejected the build.done that followed the review
fixes because its payload had no evidence keys (AGENTS.md P00-T01 note). The code fixes for both review findings are
already in the working tree. The task does not need splitting.

Builder, do only this:
1. Confirm the two review fixes are present: `status: "failure"` in numbers.py `complete_order` plus the shape Literal
   and test assertion, and the specs/30 §3.2 Decision (6) id wording.
2. Re-run the gates for real: `make fmt lint typecheck placeholders test` and `make check`. Audit: `uv export
   --no-hashes > /var/tmp/req.txt && uvx pip-audit --strict -r /var/tmp/req.txt` plus `corepack pnpm audit
   --audit-level high`. Complexity: `uvx radon cc -s -a packages/va_sim/src`. Duplication: `npx --yes jscpd@4 --silent`
   (0 clones). Coverage: va_sim ≥ 85 %.
3. Emit a PLAIN string (no --json) that contains: `tests: pass, lint: pass, typecheck: pass, audit: pass, coverage:
   pass, complexity: <radon average>, duplication: pass, specs: pass`, plus the test count and coverage %.
If a gate actually fails, fix the cause inside P03-T02a scope before emitting.

## Build P03-T02a gate re-run (Builder, 2026-10-29)

Both review fixes are confirmed present. Gates were re-run for real: fmt/lint/typecheck/placeholders all exited 0
(placeholders found 0). `make check` exited 0: 3615 pytest passed, total coverage 97.87 % (va_sim 99.3 %), TS vitest
122 passed. pip-audit (`--no-emit-workspace`, `uvx --python 3.12`) found no known vulnerabilities. pnpm audit
high found no known vulnerabilities. radon average is A (2.17). jscpd found 0 clones.

## Review P03-T02a re-check (Reviewer, 2026-10-29)

Approved. Both earlier findings are fixed (`"failure"` entry status in numbers.py, shapes and tests; specs/30 §3.2
Decision (6) id wording). Gates: lint, typecheck and placeholders (0 findings) all passed. The first `make test` run
had 1 failure and 11 errors in apps/api testcontainers fixtures, all Docker daemon read timeouts with nothing to do
with va_sim. Those 4 files passed on their own (77 passed). A second full `make test` passed: 3615 tests, coverage 97.87 %.
