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

## Plan P03-T02b — FakeTelnyx call control and scenario-driven call progress (Planner, 2026-10-29)

Scope: Telnyx call REST (dial + every action in specs/06 §2.1), conferences, scenario-driven progress (ring →
answered | busy | no_answer | rejected | failed), AMD events, transfer/bridge legs, recordings (`call.recording.saved`
+ WAV), streaming/playback state + events, inbound calls, call control API. **Out of scope (P03-T03):** the media WS
client (connecting to `stream_url`, frames, marks, caller `steps`, `greeting_audio`, DTMF steps, agent_audio.wav). T02b
keeps per-call stream params and empty PCM16 track buffers (`CallAudio.caller` / `.agent`) and an empty
`timeline_utterances` list so T03 only fills them.

### Decisions (Builder writes ONE `> Decision (2026-10-29): FakeTelnyx call control (P03-T02b).` block at the end of specs/30 §3.2)
1. Ids: `call_control_id` = `"v3:" + state.ids.token(24)`; `call_leg_id`, `call_session_id`, conference id, recording
   id, stream id = `state.ids.uuid()`. A transfer leg or a dial with `link_to` shares the linked call's `call_session_id`.
2. Command responses: every call action answers 200 `{"data": {"result": "ok"}}`; conference create 200 `{"data":
   conference}`. State changes happen synchronously in the handler; the resulting webhooks are emitted by a supervised
   task after the response, in per-call order (one `asyncio.Lock` per call, FIFO). A command's `client_state`
   (when sent) replaces the leg's client_state for later events.
3. `command_id` dedup: per call (dial: per account). A repeated `command_id` answers the same 200 without
   re-executing and is logged with `duplicate: true`.
4. Errors: unknown ccid or another account's ccid → 404 `10005`; call already hung up → 422 `90018` "Call has
   already ended"; command invalid for the leg's state (answer/reject on an outgoing or already answered leg,
   record/stream/playback/transfer/bridge before answer, bridge with itself) → 422 `10015` with the detail "Call is
   not in a state that accepts <action>." (real Telnyx code for this not verified; va_telnyx maps any other 4xx to
   CARRIER_ERROR anyway); bad `connection_id` / `link_to` / bridge `call_control_id` → 422 `10015` with pointer.
   `from` is not checked against owned numbers (verified caller ids / BYO). `to` accepts E.164 or `sip:` URIs.
5. Webhook target: the leg's `webhook_url` (dial/answer/transfer body; a transfer leg inherits the A leg's when the
   body has none), else the app (`connection_id`) `webhook_event_url`, else `SIM_EDGE_WEBHOOK_URL`.
6. Outcome: the scenario bound to the dialled `to` (§3.5 `answer`); a dial with `link_to` and no scenario for `to`
   uses the linked call's scenario `transfer_target`; a transfer leg uses the A leg's scenario `transfer_target`;
   with no scenario: answered after 1 000 ms, `answered_by: human_residence`. Timings on the sim clock from the dial:
   `call.initiated` (state `bridging`) at once; answered → `call.answered` at `after_ms`; busy → `call.hangup`
   `user_busy`/sip `486`/source `callee` at 500 ms; rejected → `call_rejected`/`603`/`callee` at 500 ms; failed →
   `unspecified`/`503`/`unknown` at 500 ms; no_answer → `timeout`/`487`/`unknown` after the dial `timeout_secs`
   (default 30). An answered leg is hung up `time_limit`/source `unknown` after `time_limit_secs` (default 14 400).
7. Hangup sides: a platform `hangup` on a leg the platform dialled (outgoing) has `hangup_source: caller`, on an
   incoming leg `callee`; cause `normal_clearing` (sip `200`) once answered, `originator_cancel` (sip `487`) before.
   `POST /sim/telnyx/calls/{ccid}/hangup {"cause"?: <hangup cause>, default normal_clearing}` is the remote party:
   the opposite source. Hangup order: `streaming.stopped` (if streaming), `call.playback.ended` (`status:
   "call_hangup"`, if playing), `conference.participant.left`, `call.hangup`, `call.recording.saved` (if
   recording). When a bridged leg hangs up its peer is hung up too (`normal_clearing`, the peer's opposite side)
   unless the bridge command had `park_after_unbridge: "self"` (the peer goes back to `parked`, no event).
8. AMD (from answer): `premium` → `call.machine.premium.detection.ended` `{result: answered_by}` at +1 800 ms and, for
   `machine` with `beep_at_ms`, `call.machine.premium.greeting.ended` `{result: "beep_detected"}` at
   +`beep_at_ms` (events in time order). `detect`, `detect_beep`, `detect_words`, `greeting_end` →
   `call.machine.detection.ended` `{result: human|machine|not_sure}` (human_* → human, machine → machine,
   silence/fax_detected → not_sure) at +1 800 ms, and for `detect_beep`/`greeting_end` with a beep
   `call.machine.greeting.ended` `{result: "beep_detected"}`. `disabled` → none.
9. Transfer: needs an answered A leg; creates leg B (`direction: outgoing`, `from`/`to` of the body, client_state =
   `target_leg_client_state`) → B `call.initiated`; answered → B `call.answered` then `call.bridged` on A then B;
   not answered → B `call.hangup` per rule 6 and A stays answered. `bridge` → `call.bridged` on both legs (A
   first); `bridge_on_answer: true` with `link_to` bridges on answer.
10. Recordings: `record_start` (or dial/answer `record: "record-from-answer"`) needs an answered leg; `record_stop`
   or hangup emits `call.recording.saved` `{recording_id, channels ("dual" default | "single"), format,
   recording_started_at, recording_ended_at, recording_urls: {"wav": "<base>/sim/recordings/<id>.wav", "mp3": null},
   public_recording_urls: same}`. Recordings are always WAV (no MP3 encoder, as specs/06 §10.2 Decision): `mp3` is
   null even when `format: "mp3"` was requested. `<base>` is the base URL of the request that created the call
   (dial or `/sim/telnyx/inbound`) so URLs resolve wherever the sim runs. The WAV is 8 kHz PCM16, channel 0 the caller
   track and channel 1 the agent track (single: mixed sum, clipped) from `CallAudio`, padded with silence to the
   recording's sim-clock duration; built with `asyncio.to_thread(pcm16_wav, ...)`.
11. Streaming/playback: `streaming_start` (answered leg) stores the stream (stream_id uuid) and emits
   `streaming.started` `{stream_url}`; dial/answer with `stream_url` start the stream on answer; `streaming_stop`
   emits `streaming.stopped`. The WS connection itself arrives in P03-T03. `playback_start` → `call.playback.started`
   `{media_url, overlay}`; `playback_stop` → `call.playback.ended` `{media_url, overlay, status: "cancelled"}`;
   playbacks end only by stop or hangup. `send_dtmf` records `{digits, duration_millis, at}` (digits `^[0-9A-Da-d*#wW]+$`,
   duration 100–500, default 250).
12. Conferences: `POST /v2/conferences` needs an answered leg → `{id, name, record_type: "conference", status:
   "in_progress", connection_id, created_at, expires_at (+24 h), region: "sim"}`; `conference.created` then
   `conference.participant.joined` for the creating leg. `join` (answered leg, not already in it) →
   `conference.participant.joined`; `leave` or participant hangup → `conference.participant.left`; when the last
   participant leaves, or one that joined with `end_conference_on_exit: true` leaves, `status: "completed"` and
   `conference.ended` (`reason: "all_left"` | `"host_left"`). Conference events carry `{conference_id,
   call_control_id, call_leg_id, call_session_id, client_state, connection_id}` and go to the participant leg's
   webhook URL. Unknown conference → 404 `10005`; ended conference → 422 `10015`.
13. Inbound: `POST /sim/telnyx/inbound {to, from, scenario?}`: `to` must be a number owned by an account with a
   `connection_id` (else 422 `invalid_request`); `scenario` (registered id, else 404 `not_found`) or the scenario bound
   by `inbound_from`. Creates an incoming leg (state `parked`) on the owning account, emits `call.initiated`
   (`direction: incoming`) to the app's `webhook_event_url` (rule 5) and answers 202 `{"call_control_id",
   "call_leg_id", "call_session_id"}`. When the app has `first_command_timeout` and no command arrives within
   `first_command_timeout_secs`, the leg is hung up `timeout`/source `unknown`.
14. Control API: `GET /sim/telnyx/calls` → `{"calls": [summary...]}` (every account, creation order) and `GET
   /sim/telnyx/calls/{ccid}` → summary `{call_control_id, call_leg_id, call_session_id, connection_id, direction,
   from, to, state, is_alive, scenario_id, client_state, created_at, answered_at, ended_at, hangup_cause,
   hangup_source, commands: [{name, command_id, at, duplicate}], events: [{event_type, event_id, at}], dtmf_sent,
   transfers: [{call_control_id, to, from, at}], recordings: [ids], stream: {...}|null, conference_id}` (`at` ISO Z);
   `GET /sim/telnyx/calls/{ccid}/timeline` → `{call_control_id, initiated_ms, answered_ms, ended_ms, utterances: []}`
   (sim `virtual_ms`; utterances filled by the P03-T03 caller); `POST /sim/telnyx/calls/{ccid}/hangup` → 202
   summary; unknown → 404 `not_found`; already ended → 409 `call_ended` (new control error code).
   Payload states: `bridging` (outgoing initiated), `parked` (incoming initiated / unbridged), `answered`,
   `bridged`, `hangup`. `is_alive` is false until answered and after hangup.

### Files
- `packages/va_sim/src/va_sim/telnyx/call_models.py` — request models (all `_Body`-style `extra="forbid"`; reuse
  `E164`, `WebhookUrl`, `Name` from `telnyx.models` — import them, no copies): `CallTarget` type (`E164 | sip:\S+`),
  `StreamFields` mixin (stream_url, stream_track `inbound_track|outbound_track|both_tracks` = inbound_track,
  stream_codec `PCMU|PCMA|G722|OPUS|AMR-WB|L16|default` = default, stream_bidirectional_mode `mp3|rtp`|None,
  stream_bidirectional_codec, stream_bidirectional_sampling_rate `8000|16000|22050|24000|48000`,
  stream_bidirectional_target_legs `both|self|opposite`, stream_auth_token, send_silence_when_idle=False),
  `RecordFields` (record `record-from-answer`|None, record_channels `single|dual`=dual, record_format `wav|mp3`=wav,
  record_track `both|inbound|outbound`=both, record_max_length 0–14400), `CommandFields` (command_id, client_state ≤
  4096 chars), `CustomHeader{name,value}`, `AmdConfig` (the 10 Telnyx config ints, all optional),
  `DialRequest` (connection_id, to, from, from_display_name, timeout_secs 5–600=30, time_limit_secs 30–14400=14400,
  answering_machine_detection `premium|detect|detect_beep|detect_words|greeting_end|disabled`=disabled,
  answering_machine_detection_config, webhook_url, webhook_url_method `POST|GET`=POST, custom_headers, link_to,
  bridge_intent=False, bridge_on_answer=False + stream/record/command fields), `AnswerRequest`, `RejectRequest`
  (cause `CALL_REJECTED|USER_BUSY`), `HangupRequest`, `TransferRequest` (to, from, from_display_name, timeout_secs,
  time_limit_secs, early_media=True, target_leg_client_state, webhook_url, answering_machine_detection,
  custom_headers, record), `BridgeRequest` (call_control_id, park_after_unbridge `self`|None),
  `SendDtmfRequest`, `RecordStartRequest` (format, channels, play_beep, max_length, timeout_secs, trim?, command
  fields), `RecordStopRequest`, `StreamingStartRequest`, `StreamingStopRequest` (stream_id), `PlaybackStartRequest`
  (audio_url, loop `int ≥1 | "infinity"`="infinity", overlay, target_legs `self|opposite|both`), `PlaybackStopRequest`
  (stop `all|current`=all, overlay), `ConferenceCreate` (call_control_id, name, beep_enabled
  `always|never|on_enter|on_exit`=never, client_state, command_id, start_conference_on_create=True, comfort_noise?),
  `ConferenceJoin` (call_control_id, start_conference_on_enter, end_conference_on_exit=False, hold, mute,
  client_state, command_id), `ConferenceLeave` (call_control_id, command_id), control bodies `InboundCall` (to, from
  alias, scenario | None), `RemoteHangup` (cause literal of the §3.2 hangup causes = normal_clearing).
- `packages/va_sim/src/va_sim/telnyx/calls.py` — dataclasses (slots, mutable): `CallAudio(caller: bytearray, agent:
  bytearray)`, `StreamState(stream_id, params: dict)`, `ActiveRecording(recording_id, channels, format,
  started_at, caller_offset, agent_offset)`, `Playback(media_url, overlay)`, `CommandEntry`, `EventEntry`, `Call`
  (fields of decision 14 + account api_key, webhook_url, base_url, amd, outcome, bridged_with, park_after_unbridge,
  timers: list[asyncio.Task[None]], lock: asyncio.Lock, command_ids set, audio, timeline_utterances list),
  `Conference` (record dict + participants dict ccid→end_on_exit). `Call.summary() -> Record`, `Call.payload(**extra)`
  (decision 14 payload base: call_control_id, call_leg_id, call_session_id, connection_id, client_state, from, to,
  direction, state, start_time + extras). Account gains `calls`, `conferences`, `dial_commands: dict[str, str]`;
  TelnyxStore gains `call(ccid) -> tuple[Account, Call] | None` and `all_calls()` (store.py edits).
- `packages/va_sim/src/va_sim/telnyx/call_engine.py` — `class CallEngine(state)`: the only place that mutates call
  state and schedules progress. `outcome_for(...) -> LegOutcome` (frozen: result, ring_s, answered_by, beep_at_s);
  `NON_ANSWER: Final[Mapping[result, HangupSpec(cause, sip, source, delay_s|None)]]`; `start_outgoing(call, outcome,
  timeout_s)` (spawns progress), `answer(call)` (state, stream/record-from-answer, AMD timers, time limit,
  bridge_on_answer), `hang_up(call, cause, source, sip)` (decision 7 order, cancels timers except the current task,
  conference leave, peer handling, recording save), `bridge(a, b, park)`, `transfer(a, body) -> Call`,
  `start_recording/stop_recording`, `start_stream/stop_stream`, `start_playback/stop_playback`, `emit(call,
  event_type, **extra)` (per-call lock, appends EventEntry, `state.webhooks.emit(call.webhook_url, ...)`),
  `later(call, delay_s, coro_factory)` (supervised timer registered on the call). Conference join/leave helpers live in
  `conferences.py` but leave-on-hangup is invoked from `hang_up` through a small callback to avoid a cycle (or put the
  participant-left logic in the engine; Builder's choice, no duplication).
- `packages/va_sim/src/va_sim/telnyx/call_routes.py` — `call_routes(state) -> list[BaseRoute]`: `POST /v2/calls`,
  `POST /v2/calls/{call_control_id}/actions/{action}` dispatch table (answer, reject, hangup, transfer, bridge,
  send_dtmf, record_start, record_stop, streaming_start, streaming_stop, playback_start, playback_stop); shared helper
  `_command(request, account, model, handler)` doing lookup → 404/90018 → parse → dedup → client_state update →
  handler. Unknown action → 404 `10005`.
- `packages/va_sim/src/va_sim/telnyx/conferences.py` — `conference_routes(state)`: `POST /v2/conferences`, `GET
  /v2/conferences/{id}` (record), `POST /v2/conferences/{id}/actions/join|leave`.
- `packages/va_sim/src/va_sim/telnyx/call_control.py` — `call_control_routes(state)`: `POST /sim/telnyx/inbound`,
  `GET /sim/telnyx/calls`, `GET /sim/telnyx/calls/{ccid}`, `GET .../timeline`, `POST .../hangup`.
- `telnyx/errors.py` — `CALL_ENDED = "90018"` ("Call has already ended") + `call_ended()`; `invalid_state(action)`.
- `telnyx/routes.py` — add call, conference, call control routes. `state.py` snapshot `telnyx` adds `calls`,
  `conferences` counts (reset already clears accounts). `store.py` per above.
- `specs/30-testing-and-evals.md` §3.2 Decision block; AGENTS.md P03-T02b note (call control API, engine as the single
  mutation point, T03 hooks: `Call.audio`, `Call.stream`, `Call.timeline_utterances`).

### Tests (`packages/va_sim/tests/telnyx/`, existing `telnyx_helpers` client/RecordingTransport + AcceleratedClock;
add `dial(client, **overrides)` / `command(client, ccid, action, **body)` / `register(state, yaml)` helpers to
telnyx_helpers.py; add Telnyx shapes to telnyx_shapes.py: `DialResponse`, `CommandResult`, `Conference`,
`CallEventPayload`, `HangupPayload`, `RecordingSavedPayload`, `AmdPayload`, `ConferenceEventPayload`)
- `test_calls_dial.py`: dial requires connection_id/to/from (pointers), unknown connection → 10015 `/connection_id`,
  unknown field → 10015; response shape (`v3:`, is_alive false, client_state echoed); per-outcome event sequences with
  `virtual_ms` (answered at after_ms; busy/rejected/failed at 500 ms with causes/sip/source; no_answer at
  timeout_secs; default outcome without scenario); webhook URL precedence (decision 5); signatures verify
  (`verify_signature`); time_limit hangup; dial command_id dedup returns same ccid; same seed → same ids.
- `test_calls_actions.py`: answer/reject of inbound legs (reject USER_BUSY → user_busy); hangup before answer →
  originator_cancel, after → normal_clearing with decision-7 source; 90018 after hangup; 404 unknown/other account;
  invalid-state 10015; command_id dedup (one event, `duplicate: true` in log); client_state replacement echoed;
  send_dtmf log + bad digits/duration; streaming start/stop events and stream on answer via dial `stream_url`;
  playback start/stop/hangup status; hangup event order with streaming+playback+recording.
- `test_calls_amd.py`: premium human/machine+beep (both orders: beep before/after 1 800 ms)/silence/fax; standard
  detect mapping; disabled → none.
- `test_calls_transfer.py`: transfer answered → B initiated/answered, bridged A then B, B client_state =
  target_leg_client_state, same session id; transfer_target busy/no_answer → B hangup and A alive; transfer before
  answer → 10015; bridge both + 90018 for ended peer + pointer for unknown; bridged peer hangup cascades;
  `park_after_unbridge: self` keeps peer; dial `link_to` + `bridge_on_answer` bridges, link_to uses transfer_target.
- `test_calls_recording.py`: record_start/stop → `call.recording.saved` shape, URL fetch via `/sim/recordings/<id>.wav`
  returns 2-channel 8 kHz WAV whose frame count = sim duration × 8000 (wave module); `record-from-answer` on dial;
  saved on hangup; single channel; mp3 null; record_start before answer → 10015.
- `test_conferences.py`: create → shape, events created + joined; join/leave events; host_left with
  end_conference_on_exit; all_left; participant hangup → left; unknown 404; join ended conference 10015.
- `test_call_control.py`: inbound initiated payload (incoming, parked) to app URL; inbound needs owned number with
  connection (422), unknown scenario 404, scenario by inbound_from bound; first_command_timeout hangup; calls listing
  + detail summary (commands, events, dtmf, transfers); timeline ms values; remote hangup source opposite + 409 when
  ended + 404; `/sim/reset` cancels pending progress (supervisor pending 0) and drops calls; snapshot counts.

### Gates
`make fmt lint typecheck placeholders test`; `make check` (jscpd 0 clones, va_sim coverage ≥ 85 %); radon on
`packages/va_sim/src` (keep handler functions at A/B: dispatch tables, not if-chains); pip-audit + pnpm audit as in
T02a. No e2e (no platform flow yet).

### Risks
- Ordering: spawn one emission task per state change; all emits go through `CallEngine.emit` (per-call lock) or
  events of one leg can interleave. Hang-up cancels the leg's timers — never cancel the task currently running
  `hang_up` (`asyncio.current_task()` check).
- Accelerated clock: tests advance via `clock.sleep`/`advance` then `await state.tasks.drain()`; assert `virtual_ms`
  of `RecordingTransport.received`, never wall time. Default `time_limit_secs` 14 400 s timers must not make `drain()`
  wait forever in accelerated mode — tests that drain must hang up calls first or cancel; document in AGENTS.md (or
  give `drain` no part in it: prefer helper `settle(state)` = advance to a given virtual time + drain).
- jscpd: the 12 action handlers must share the `_command` wrapper; the hangup-cause tables live once in call_engine.
- Do not import `va_telnyx` / `va_domain`; the simulator builds payloads itself.
- Keep files < ~350 lines and functions < radon C; split engine helpers if needed.

## Build P03-T02b (Builder, 2026-10-29)

Implemented per plan; deviations (all in the specs/30 §3.2 Decision (2026-10-29) P03-T02b block or AGENTS.md):
- Files: `telnyx/call_models.py`, `calls.py` (Call/Conference dataclasses, summary/timeline/payloads, outcome_of),
  `call_events.py` (CallEvents: ordered delivery chains + timers), `call_media.py` (CallMedia: stream, playback,
  recording WAV, AMD), `call_engine.py` (CallEngine(CallMedia): progress, answer, hang_up, bridge, transfer,
  conferences), `call_actions.py` (action table), `call_routes.py` (dial + dispatcher), `conferences.py`,
  `call_control.py`; store/errors/routes/state edits. Engine lives on `SimState.call_engine`.
- Ordering: deliveries chained per call session AND per conference (conference events of different legs would
  otherwise race). Conference participants are keyed by Call object.
- `park_after_unbridge: "self"` applies to the commanding leg: it parks when its peer hangs up.
- Clock change (decision 15): AcceleratedClock pump advances only when `loop._ready` is empty (idle), else
  mid-call commands from in-process tests raced the 14 400 s time-limit timer. uvloop (no `_ready`) keeps the old
  fixed settle turns. Existing clock/sim tests unchanged and passing.
- Test helpers `at()` / `settle()` also await `call_engine.idle()`: the recording WAV is encoded in a worker
  thread (`asyncio.to_thread`), which leaves the loop idle.
- SIP table and `end_time` on call.hangup; ring longer than timeout_secs → no-answer; first-command timeout sip 408
  not used (timeout → 487 from the table).
Tests: tests/telnyx/test_calls_dial|actions|amd|transfer|recording.py, test_conferences.py, test_call_control.py
(+ snapshot count updates in test_state/test_control_telnyx); helpers/shapes in tests/support.
Gates (real runs): make fmt 0; make lint 0; make typecheck 0 (mypy 467 files + tsc); make placeholders 0 findings;
make check 0: 3687 pytest passed, total coverage 97.91 % (new call modules 97–100 %), TS vitest passed, openapi
drift + spec refs clean. radon average A (2.11; only C is pre-existing scenario._locate). jscpd 0 clones.
pip-audit + pnpm audit high: no known vulnerabilities. va_sim suite stable over repeated -n 4 runs with coverage.

## Review P03-T02b (Reviewer, 2026-10-29)

Gates re-run: make lint 0, make typecheck 0, make placeholders 0 findings, make test 0 (3687 passed).
Spec decision block, store/errors/state edits, ordering design (per-session/per-conference chains), and the
action dispatch table are good. Rejected for:

1. `packages/va_sim/src/va_sim/telnyx/call_engine.py` `hang_up` / `_unbridge` (and `transfer`): when leg A
   hangs up while its transfer leg B is still ringing (`b.bridge_on_answer is a`, not yet bridged), B is left
   running. Probe (dial scenario callee, answer at 1 s, transfer with `transfer_target: {answer_after_ms: 4000}`,
   `hangup` A at 2 s, advance to 10 s): B summary `state: answered, is_alive: true`, events
   `[call.initiated, call.answered]`, with no bridge and nothing else until the 14 400 s time limit. Real Telnyx
   cancels the transfer leg when the originating leg goes away, and the platform never gets a hang-up for a leg it
   cannot control. Required: on hang-up of a leg, every unanswered leg whose `bridge_on_answer` is that leg is hung up
   `originator_cancel`, source `caller`, with SIP 487 and `call.hangup`. That covers transfer legs and `link_to` +
   `bridge_on_answer` dials. A leg that answers after its peer ended is also hung up rather than left orphaned.
   Write this into Decision (9) and test it in test_calls_transfer.py (A hangs up during B ring → B `call.hangup`
   originator_cancel; same for link_to + bridge_on_answer).
2. `packages/va_sim/src/va_sim/telnyx/call_media.py` `stop_recording.store`: `mix_tracks(caller, agent)` (a pure-
   Python per-sample generator over the whole recording) is evaluated on the event loop, because the list is built
   before `asyncio.to_thread` runs. A 10-minute single-channel recording is 4.8 M samples and blocks the loop for
   seconds, which breaks MASTER_PROMPT rule 7 (CPU work via `asyncio.to_thread`). Required: do the mix inside the
   worker thread (e.g. one helper `_encode(channels, caller, agent)` passed to `to_thread`).

## Fix P03-T02b review findings (Builder, 2026-10-29)

1. Orphaned ringing legs: `Call.waiting_legs` (registered in `CallEngine.new_call` when `bridge_on_answer` is set,
   so transfer legs and `link_to` + `bridge_on_answer` dials both count). `hang_up` → `_cancel_waiting_legs` after
   `_unbridge`: each still-ringing leg whose `bridge_on_answer` is the ended leg gets `call.hangup`
   originator_cancel / caller / 487. `answer` checks the peer first: peer ended → `call.answered` then
   `call.hangup` normal_clearing / caller (platform_side), no media/AMD/limit. Written into specs/30 Decision (9).
   Tests (test_calls_transfer.py): transfer ring cancelled, link_to ring cancelled, link_to an ended call hung up on
   answer — all 3 fail with the fix disabled.
2. Recording mix: `call_media._encode(channels, caller, agent)` (mix + WAV) runs inside `asyncio.to_thread`.
   Test test_single_channel_mix_runs_off_the_event_loop asserts `mix_tracks` runs off the main thread (fails when
   the mix is done on the loop).
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0 findings, make check 0 (3691 passed, coverage 97.93 %, TS
vitest passed).

## Plan P03-T02b (re-plan after build.blocked: missing backpressure evidence, Planner, 2026-10-29)

Blocker is not a code problem: the Builder's last `build.done` (after fixing review findings 1–2) lacked the required
evidence keys, so the loop rejected it. Code for P03-T02b, including both review fixes, is in the working tree.
No split is needed; the task stays as planned above.

Builder actions (no new design):
1. Re-run all gates on the current tree and capture real output: `make fmt lint typecheck placeholders test`,
   `make check` (pytest + coverage ≥ 85 % per package, jscpd, openapi drift, TS vitest), `uv run radon cc -a -s
   packages/va_sim/src` (record the average grade/score), `uv run pip-audit` + `corepack pnpm audit --audit-level high`
   as in T02a.
2. If anything fails, fix it inside P03-T02b scope only.
3. Emit build.done with EVERY required key, exactly in this form (values from the real runs):
   `ralph emit "build.done" "tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass, complexity: <radon avg e.g. A (2.11)>, duplication: pass, specs: pass — P03-T02b review fixes (waiting-leg cancel, off-loop mix)"`
   Do not drop any key; `performance` is optional and may be omitted.

## Build P03-T02b gate re-run for evidence (Builder, 2026-10-29)

Nothing changed in the code. Gates re-run on the current tree: make fmt 0, lint 0, typecheck 0, placeholders 0
findings. make check 0: 3691 pytest passed, coverage 97.93 %, TS vitest passed (22+52+48), openapi drift current,
check_spec_refs 0 problems. `uvx radon cc -s -a packages/va_sim/src`: average A (2.13). `npx jscpd@4` va_sim src+tests:
0 clones. `uvx pip-audit --strict` on `uv export` and `corepack pnpm audit --audit-level high`: no known vulnerabilities.

## Review P03-T02b re-check (Reviewer, 2026-10-29)

Gates re-run: make lint 0, make typecheck 0, make placeholders 0, make test 0 (3691 passed, coverage 97.93 %).
Finding 1 fixed: `Call.waiting_legs` + `CallEngine._cancel_waiting_legs` (originator_cancel / caller / 487) after
`_unbridge`; `answer` hangs up a leg whose peer already ended; Decision (9) in specs/30 updated; 3 transfer tests.
Finding 2 fixed: `call_media._encode` (mix + WAV) runs inside `asyncio.to_thread`; off-loop test present.
Approved; task ticked and committed.

## Plan P03-T03a — Synthetic caller audio `va_sim.audio` (Planner, 2026-10-29)

Why the split: P03-T03 as written needs the §3.1 synthetic caller speech (marker preamble + formant body +
`decode_marker`), which no task owned; P03-T05 (STT marker scheme) needs `decode_marker` too. plan/phase-03.md now
has P03-T03a (this, audio only) and P03-T03b (media WS client + caller steps, depends on T02b + T03a); P03-T05
depends on T03a. The §3.1 Silero requirement cannot run yet (no onnxruntime / `SileroVad` until P04-T03), so it
is moved into the P04-T03 acceptance (plan/phase-04.md edited). Builder commits these plan edits with the task.

### Decisions (Builder writes ONE `> Decision (2026-10-29): synthetic caller audio (P03-T03a).` block at the end of specs/30 §3.1)
1. The simulator has its own G.711 μ-law codec (`va_sim.audio.codec`), table-based exactly as specs/07 describes
   (`ULAW_TO_PCM16` (256,) int16 with bias 0x84 / inverted bits; `PCM16_TO_ULAW` (65536,) uint8 indexed by
   `int16 + 32768`, clipping at 32635). `va_voice.codec` arrives in P04-T01 and must be bit-identical (P04 adds a
   cross-check test); the sim does not import `va_voice` (phase rule: no shared message/audio construction).
   Do NOT copy `scripts/generate_audio_assets.py` ulaw code (jscpd) — build the tables independently.
2. Randomness only from `numpy.random.PCG64(seed).random_raw()` (AGENTS.md rule), converted to uniform floats as
   `(raw >> 11) * 2**-53`. Utterance seed: `SimState.utterance_seed(utterance_id) = seed * 65_536 + utterance_id`
   (non-negative; `seed` is `SimState.seed`).
3. Utterance ids are 16-bit (the marker carries 16 bits): `synthesize_*` require `0 <= utterance_id <= 65_535`;
   `SimState.register_utterance` raises `ValueError` once 65 535 ids are allocated (ids start at 1, so 0 is never
   allocated but is synthesizable for tests).
4. Marker preamble (180 ms, peak −12 dBFS = round(32767·10^(−12/20)) = 8231): 20 ms sync sine 1 700 Hz, then 16
   bits × 10 ms FSK MSB first (0 → 1 200 Hz, 1 → 2 200 Hz), phase-continuous across segments (no clicks), 0 ms gap.
5. Voiced body: duration `len(text) * rate_ms_per_char` ms; syllables of 180 ms (last one shorter); syllable k's
   vowel = `"aeiou"[sum(map(ord, chunk_k)) % 5]` where `chunk_k` is the slice of `text` covering that syllable's
   time span (`chars_per_syllable = 180 / rate_ms_per_char`, slice `[floor(k·c), max(floor((k+1)·c), floor(k·c)+1))`
   clamped to the text). Source: Rosenberg glottal pulse train (open phase 40 % of the period rising
   `0.5·(1 − cos(π t/Tp))`, closing 16 % `cos(π (t−Tp) / (2 Tn))`, closed rest), each period `T0·(1 + 0.03·(2u−1))`
   from the RNG, first difference for lip radiation. Filter: three formant resonators in parallel (gains 1.0, 0.5,
   0.25; bandwidths 80/120/160 Hz) implemented as `np.convolve` with the truncated impulse response of a two-pole
   resonator `h[n] = r^n·sin(ω(n+1))` (`r = exp(−π B / sr)`, truncated when `r^n < 1e-4`), each normalised to unit
   peak. Each syllable is filtered with its vowel's formants and shaped by a raised-cosine (Hann) envelope over its
   own length; the body is normalised to peak −6 dBFS (16 423). Then μ-law (8 kHz) or kept PCM16 (16 kHz).
6. `text` must contain a non-whitespace character; `rate_ms_per_char` ≥ 1; `f0_hz` in [50, 400]; `sample_rate`
   is 8 000 or 16 000; otherwise `ValueError`.
7. `decode_marker(pcm16, sample_rate)`: scans the whole array in 1 ms steps for the first 20 ms window whose
   Goertzel 1 700 Hz power is ≥ 50 % of the window energy (and window RMS ≥ −30 dBFS), refines the offset to the
   local maximum within the next 5 ms, then reads 16 bits from 10 ms windows trimmed 1 ms each side (8 ms):
   bit = power(2 200) > power(1 200), and requires `(p1200 + p2200) ≥ 0.5 · energy` for every bit, else `None`.
   Works for any `sample_rate` ≥ 8 000. Returns the first valid id or None (silence, body-only audio,
   truncated preamble).
8. `synthesize_dtmf(digit, ms=120, *, sample_rate=8000) -> bytes` returns wire audio: μ-law bytes for
   `sample_rate=8000`, L16 big-endian bytes for 16 000 (the hd_audio wire format, specs/30 §3.2). Same convention for a `synthesize_utterance_wire(..., sample_rate)` helper
   used by T03b (see signatures). Tones: standard DTMF row/column frequencies, each component −10 dBFS; digits
   `0-9*#A-D` else `ValueError`.

### Files
- `packages/va_sim/pyproject.toml`: add `"numpy>=2.3"` to dependencies; `uv lock`.
- `packages/va_sim/src/va_sim/audio/__init__.py` — re-exports the public names below, docstring citing §3.1.
- `packages/va_sim/src/va_sim/audio/codec.py` — `ULAW_TO_PCM16: Final[NDArray[np.int16]]`,
  `PCM16_TO_ULAW: Final[NDArray[np.uint8]]`, `ulaw_encode(x: NDArray[np.int16]) -> bytes`,
  `ulaw_decode(b: bytes) -> NDArray[np.int16]`, `pcm_to_l16be(x) -> bytes`, `l16be_to_pcm(b) -> NDArray[np.int16]`,
  `ULAW_SILENCE: Final = 0xFF`, `dbfs_peak(dbfs: float) -> int` (int16 amplitude).
- `packages/va_sim/src/va_sim/audio/rng.py` — `class AudioRng(seed)`: `uniform(n) -> NDArray[np.float64]`
  (raw PCG64 → [0,1)).
- `packages/va_sim/src/va_sim/audio/marker.py` — constants `SYNC_HZ=1700`, `BIT0_HZ=1200`, `BIT1_HZ=2200`,
  `SYNC_MS=20`, `BIT_MS=10`, `MARKER_BITS=16`, `MARKER_MS=180`, `MAX_UTTERANCE_ID=65_535`;
  `marker_preamble(utterance_id, sample_rate) -> NDArray[np.float64]` (unit-peak float, scaled by synth);
  `goertzel_power(x: NDArray[np.float64], freq_hz: float, sample_rate: int) -> float` (public; DTMF tests and
  FakeElevenLabs STT reuse it); `decode_marker(pcm16: NDArray[np.int16], sample_rate: int) -> int | None`.
- `packages/va_sim/src/va_sim/audio/synth.py` — `VOWEL_FORMANTS: Final[Mapping[str, tuple[float,float,float]]]`
  (§3.1 table), `vowel_sequence(text, rate_ms_per_char) -> tuple[str, ...]`,
  `synthesize_pcm16(utterance_id: int, text: str, *, sample_rate: int = 8000, f0_hz: float = 140.0,
  rate_ms_per_char: int = 70, seed: int) -> NDArray[np.int16]`,
  `synthesize_utterance(utterance_id, text, *, f0_hz=140.0, rate_ms_per_char=70, seed) -> bytes` (μ-law 8 kHz,
  the §3.1 signature), `synthesize_wire(utterance_id, text, *, sample_rate, seed, f0_hz=140.0,
  rate_ms_per_char=70) -> bytes` (8 000 → μ-law, 16 000 → L16 BE), `utterance_ms(text, rate_ms_per_char) -> int`
  (= 180 + len(text)·rate).
- `packages/va_sim/src/va_sim/audio/dtmf.py` — `DTMF_FREQS: Final[Mapping[str, tuple[int, int]]]`,
  `synthesize_dtmf(digit: str, ms: int = 120, *, sample_rate: int = 8000) -> bytes`.
- `packages/va_sim/src/va_sim/state.py` — `utterance_seed(utterance_id) -> int`; 16-bit cap in
  `register_utterance` (import `MAX_UTTERANCE_ID`; watch import cycles: audio must not import state).
- specs/30 §3.1 Decision block (above, plus: synthesis is CPU-bound — async callers (T03b) run it in
  `asyncio.to_thread`); AGENTS.md P03-T03a note (va_sim.audio API, PCG64 raw rule applies, Silero check lives in
  P04-T03, codec must stay bit-identical with va_voice).
- mypy: numpy typing works with `NDArray`; `files` already include packages/va_sim/src+tests.

### Tests (`packages/va_sim/tests/audio/`, plain pytest, no clock needed)
- `test_codec.py`: known vectors (0 → 0xFF, 32 767 → 0x80, −32 768 → 0x00, 0xFF → 0, 0x7F → 0); decode table equals
  an independent formula written in the test (sign/exponent/mantissa per G.711); full-range round trip
  |decode(encode(x)) − clip(x)| ≤ one quantization step of x's segment for all 65 536 inputs; encode clips at
  ±32 635; L16 BE round trip and byte order (`0x0102` → `b"\x01\x02"`).
- `test_marker.py`: 20 ids from `random.Random(7)` over 0..65 535 (+ 0 and 65 535 explicitly) decode exactly after
  μ-law round trip at 8 kHz; after resampling the 8 kHz decode to 16 kHz in the test (`np.interp` linear) ; from
  native 16 kHz `synthesize_pcm16`; with 0–500 ms of leading silence plus −40 dBFS white noise (test-side
  `np.random.PCG64` raw) over the whole signal; `None` for silence, for noise only, for the voiced body alone
  (slice off the preamble), for a preamble truncated mid-bits; preamble length = 180 ms; sync window spectrum
  peak at 1 700 Hz (via `goertzel_power` vs 1 200/2 200).
- `test_synth.py`: length = (180 + len·rate) ms × sr/1000 samples for 8 k and 16 k; same seed → identical bytes,
  other seed → different body and identical preamble; different ids → different preamble, identical body;
  preamble peak 8 231 ± 2 % and body peak 16 423 ± 2 % (PCM16 path), μ-law decode within one step; voiced body has
  F0 periodicity (autocorrelation peak lag within ±5 % of sr/f0 on a 100 ms window) for f0 120 and 200;
  ≥ 90 % of 32 ms body windows have RMS above −40 dBFS (energy stand-in until P04-T03's Silero test);
  `vowel_sequence` deterministic and matches the documented rule on a hand-worked example; formant check: the
  spectrum of a single-vowel text ("aaaa…" chosen so all syllables map to /a/, or monkeypatch-free by picking a text
  whose vowel_sequence is all one vowel) has energy peaks within ±15 % of F1/F2; ValueError cases (id −1 and
  65 536, blank text, rate 0, f0 30, sample_rate 11 025).
- `test_dtmf.py`: all 16 digits: row and column frequency powers each ≥ 10× any other DTMF frequency;
  length 120 ms → 960 μ-law bytes (8 kHz) / 3 840 L16 bytes (16 kHz); peak ≈ −4 dBFS; bad digit / ms ≤ 0 →
  ValueError.
- `packages/va_sim/tests/test_state.py` additions: `utterance_seed` = seed·65 536 + id and changes with reset seed;
  registry cap raises after 65 535 (monkeypatch/pre-fill the dict rather than synthesizing).

### Gates
`make fmt lint typecheck placeholders test`; `make check` (va_sim coverage ≥ 85 %, jscpd 0 clones — no copy of the
scripts/ codec); `uvx radon cc -s -a packages/va_sim/src` (keep new functions ≤ B); pip-audit (numpy already in the
lock). No e2e.

### Risks
- Speed: keep synthesis vectorised (per-period pulse placement may loop over periods — ~140/s, fine; never loop per
  sample in Python). 10 s of text at 16 kHz should synthesize in well under 100 ms.
- FSK decode robustness after linear-interpolation resampling and μ-law: 8 ms trimmed windows give ~1 kHz bin width
  at 8 kHz — 1 200 vs 2 200 Hz separate cleanly; do not shrink the windows.
- Phase-continuous FSK: accumulate phase across segments, or bit boundaries splatter energy and break the 50 %
  dominance check.
- Floating-point determinism: same numpy ops on the same platform → identical output; tests compare bytes only
  within one run (do not commit golden audio files).

## Build P03-T03a (Builder, 2026-10-29)

`va_sim.audio` (codec, rng, marker, synth, dtmf, wire) + tests in `packages/va_sim/tests/audio/` (111 tests);
`SimState.utterance_seed` + 16-bit registry cap; numpy dependency + uv.lock; specs/30 §3.1 Decision; AGENTS.md note.
Plan deviations (all written into the Decision):
- Envelope: raised cosine from a 0.25 (-12 dB) floor to 1 instead of a full Hann — a full Hann left only 87 % of
  32 ms body windows above -40 dBFS (boundary windows go silent), failing the planned energy test and risking the
  P04-T03 Silero ≥ 90 % requirement. With the floor: 100 %, min window ~-33 dBFS.
- Decoder refines to the sync power peak within the next 12 ms (plan said 5 ms): the first passing 1 ms window can
  start up to 10 ms early because the dominance ratio equals the overlap fraction. Failed candidates are skipped and
  the scan continues (test with a false 1 700 Hz burst before a real marker).
- Body peak is 16 422 (round(32767·10^(-6/20)) = round(16422.5)), not 16 423.
- `POST /sim/reset` now rejects a negative seed (422; `NonNegativeInt`) because PCG64 seeds must be non-negative.
- Formant test is discriminative (band energy within ±10 % of F1/F2 larger than for vowels with no formant within
  25 %); smoothed-peak picking and LPC were biased by glottal tilt / parallel resonators.
- `goertzel_power` is the single-bin DFT (equal to Goertzel output), normalised so a tone's power equals its energy.
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0 findings; make check 0 (3880 passed, coverage 97.96 %, TS
vitest passed, openapi drift current, spec refs 0 problems); radon va_sim/src avg A (2.11), audio A (1.91);
jscpd va_sim src+tests+scripts 0 clones; pip-audit + pnpm audit no known vulnerabilities.

## Review P03-T03a (Reviewer, 2026-10-29)

Gates re-run: make lint 0, typecheck 0, placeholders 0 findings, make test 0 (3880 passed, coverage 97.96 %);
jscpd va_sim src+tests+scripts 0 clones; radon va_sim.audio avg A (1.91).
Checked against specs/30 §3.1 and specs/07 codec: decode table = G.711 (bias 0x84, inverted, verified vs an
independent formula in tests), encode clips at 32 635, -1 → 0x7F as in reference g711.c; preamble 20 ms 1 700 Hz +
16×10 ms FSK MSB first, phase-continuous, -12 dBFS; body Rosenberg + ±3 % jitter from raw PCG64, formant table
matches, -6 dBFS; decode_marker Goertzel sync scan + bits, skips false syncs. state imports audio.marker only
(no cycle); negative reset seed → 422. Plan deviations are all recorded in the §3.1 Decision. Silero requirement
moved to P04-T03 acceptance. Approved; task ticked and committed.

## Plan P03-T03b — FakeTelnyx media WebSocket client: transport, playout clock, agent audio (Planner, 2026-10-29)

Why the split: P03-T03b as written also needed the whole scenario caller (greeting, 6 step types, energy detector,
barge-in, transfer_target steps) on top of the WS transport — too big for one activation. plan/phase-03.md now has
P03-T03b (this: transport + playout + control-API caller audio) and P03-T03c (scenario step runner, depends on
T03b). Builder commits the plan edit with this task. Nothing outside phase 03 depended on "P03-T03b".

### Decisions (Builder writes ONE `> Decision (2026-10-29): FakeTelnyx media WebSocket client (P03-T03b).` block at
the end of specs/30 §3.2, and updates rule (11) of the T02b block: "the WS connection arrives with the media client
(P03-T03b)")
1. Library: `websockets` (already in uv.lock 17.3 via uvicorn[standard]) — add `"websockets>=17.0"` to va_sim deps;
   client `websockets.asyncio.client.connect(url, additional_headers=..., open_timeout=5, max_size=2**22,
   compression=None, ping_interval=None)`.
2. Handshake headers: `telnyx-signature-ed25519` = `signing_key.sign(f"{ts}|{stream_url}".encode())`,
   `telnyx-timestamp` = sim clock unix seconds (int), and `Authorization: Bearer <stream_auth_token>` when set.
   `stream_url` signed exactly as given in the command (query included).
3. Codecs: the sim streams PCMU 8 kHz or L16. `stream_codec` `PCMU`/`default` → PCMU 8 000; `L16` → L16 at
   `stream_bidirectional_sampling_rate` when it is 8 000 or 16 000, else 16 000. Agent (bidirectional) audio is parsed
   with `stream_bidirectional_codec` (default: the stream encoding) at the same rate. Any other `stream_codec` or
   `stream_bidirectional_codec` (PCMA, G722, OPUS, AMR-WB), or a bidirectional rate other than 8 000/16 000 → the
   command answers 422 `10015` with pointer `/stream_codec`, `/stream_bidirectional_codec`, or
   `/stream_bidirectional_sampling_rate` (validated in call_actions for dial, answer, streaming_start; check runs before
   any state change). `stream_bidirectional_mode` `mp3` → 422 `/stream_bidirectional_mode` (only `rtp` emulated).
   Frame size: 20 ms = 160 B PCMU, 320 B L16 8 kHz, 640 B L16 16 kHz (big-endian).
4. Frames sent (orjson, text frames):
   - `{"event":"connected","version":"1.0.0"}`
   - `{"event":"start","sequence_number":"1","start":{"user_id":<seeded uuid per stream>,"call_control_id",
     "call_session_id","from","to","tags":[],"client_state","media_format":{"encoding","sample_rate","channels":1}},
     "stream_id"}`
   - `{"event":"media","sequence_number","media":{"track":"inbound","chunk":"<n from 1>","timestamp":"<ms since
     start, 20·(n−1)>","payload":"<b64>"},"stream_id"}`
   - `{"event":"dtmf","stream_id","occurred_at":iso_z(now),"sequence_number","dtmf":{"digit"}}`
   - `{"event":"mark","stream_id","sequence_number","mark":{"name"}}`
   - `{"event":"error","payload":{"code","title","detail"},"stream_id"}` (no sequence number)
   - `{"event":"stop","sequence_number","stop":{"user_id","call_control_id"},"stream_id"}`, then close 1000.
   `sequence_number` is a per-stream counter as a string: `start` = "1", each later sequenced event +1.
   Telnyx `track` for caller audio is `"inbound"`.
5. Tick loop (one supervised task per stream; `clock.sleep` against an absolute schedule
   `next = started + n·0.02` so realtime does not drift; accelerated mode then runs as fast as the receiver reads
   because sockets leave the loop idle). Each tick, in this order: (a) play out one 20 ms frame of queued agent audio
   (partial last frame padded with silence) → append decoded PCM16 8 kHz to `call.audio.agent` (silence 320 B when the
   queue is empty, so both tracks stay aligned); (b) echo every mark whose position (queued agent bytes ahead of it)
   has been played; (c) take the next caller frame (queue, else silence: 0xFF PCMU / zeros L16), append its PCM16
   8 kHz decode to `call.audio.caller`, send it. L16 16 kHz is decimated to 8 kHz for the tracks by averaging sample
   pairs (`(a+b)//2`, numpy). The tracks start at `streaming.started` time; recordings keep padding to duration
   (existing `stop_recording`).
6. Receive loop (second task in the same TaskGroup): text frames only (binary → `error 100003`). JSON not an object
   or invalid → `100003 malformed_frame`; unknown/missing `event` → `100003`; `media` without a string
   `media.payload`, invalid base64, empty, odd length for L16, or > 30 s of audio in one message → `100004
   invalid_media`; `mark` without a non-empty string `mark.name` → `100003`. `media` appends to the playout queue;
   `mark` is echoed at once when nothing is queued, else queued at the current tail position; `clear` drops all queued
   agent audio and echoes every pending mark immediately (queue order). Errors keep the connection open (Telnyx
   behaviour). Every received frame is counted in `stream.received` (by event) for the call summary.
7. Lifecycle: `CallMedia.start_stream` creates `MediaSession` (new module) and spawns it on `state.tasks`; store it on
   `StreamState.session`. `stop_stream` calls `session.stop()` (sync: sets a stop flag + wakes the tick loop); the
   session sends `stop` and closes 1000 (sending `stop` is skipped if never connected). Connect failure (OSError,
   timeout, `InvalidHandshake`/`InvalidStatus` incl. 4401-style rejects) → `streaming.failed`
   `{stream_id, stream_url, failure_reason: "connection_failed"}` and `call.stream = None`; the peer closing first →
   `streaming.failed` `failure_reason: "connection_closed"` and `call.stream = None` (so a later `streaming_stop`
   is the existing 422). A hang-up/stop while connecting cancels the attempt without `streaming.failed`.
   `/sim/reset` cancels sessions via `tasks.cancel_all` (cancellation closes the socket: use `async with connect(...)`).
8. Agent-audio inspection: `GET /sim/telnyx/calls/{ccid}/agent_audio.wav` → mono 8 kHz PCM16 WAV of
   `call.audio.agent` (`va_sim.wav.pcm16_wav`; 404 `not_found` for an unknown call) — `audio/wav`.
9. Control API caller audio (the hook T03c's runner reuses through the same `MediaSession` methods):
   - `POST /sim/telnyx/calls/{ccid}/say {text, language="en"}` → registers the utterance
     (`state.register_utterance(text, language, tuple(text.split()))`), synthesizes with
     `synthesize_wire(id, text, sample_rate=<stream rate>, seed=state.utterance_seed(id))` in `asyncio.to_thread`,
     queues it; 202 `{utterance_id, duration_ms}`. 409 `not_streaming` when the call has no connected session,
     409 `call_ended` when ended; 404 unknown call; 422 body errors (existing `json_body`).
   - `POST .../dtmf {digits}` (`^[0-9A-D*#]+$`) → one `dtmf` event per digit at successive ticks (sent before that
     tick's media frame) and logged in the summary `dtmf_received_by_platform`? no — name it `caller_dtmf:
     [{digit, at}]`; 202. Same 409s.
   - `POST .../media/inject {frame: <any JSON object>}` → sent verbatim before the next tick's media frame (tests
     for malformed-frame handling in the media worker); 202. Same 409s.
   - Telnyx config gains `media_jitter: bool = False` (`TelnyxSimConfig` + patch): when true, media frames are sent
     in swapped adjacent pairs (chunk 2 before 1, 4 before 3, …; a dangling last frame waits at most one tick) with
     their original `chunk`/`timestamp`/`sequence_number`.
10. Timeline: each queued utterance records `{utterance_id, text, start_ms, end_ms, first_agent_audio_ms}` in
    `call.timeline_utterances` (sim `virtual_ms`): `start_ms` when its first frame is sent, `end_ms` when its last
    frame is sent, `first_agent_audio_ms` the arrival time of the first agent `media` message after `end_ms` whose
    decoded audio has a sample with |x| > 256 (null until then). `latency_ms = first_agent_audio_ms − end_ms`
    is included (null too).
11. Summary `stream` gains `{connected: bool, frames_sent, received: {event: count}, marks_echoed}`; summary gains
    `caller_dtmf`.

### Files
- `packages/va_sim/pyproject.toml` (+ `uv lock`): `websockets>=17.0`.
- `packages/va_sim/src/va_sim/telnyx/media_format.py` — `MediaFormat` frozen dataclass `(encoding: Literal["PCMU",
  "L16"], sample_rate: Literal[8000, 16000])` with `frame_bytes`, `silence_frame()`, `decode_pcm8k(wire) ->
  bytes` (PCM16 LE 8 kHz for tracks, via `va_sim.audio.codec`), `parse(wire) -> NDArray[np.int16]`;
  `stream_format(params) -> MediaFormat`, `agent_format(params) -> MediaFormat`; `check_stream_params(body) ->
  JSONResponse | None` (decision 3; reuse `errors.py` 10015 helper).
- `packages/va_sim/src/va_sim/telnyx/media_frames.py` — pure builders for every outbound frame (decision 4) and
  `parse_agent_frame(raw: str | bytes, fmt) -> AgentFrame | MediaFault` (decision 6) with `AgentMedia(audio: bytes)`,
  `AgentMark(name)`, `AgentClear()`, `MediaFault(code, title, detail)`; constants `MALFORMED_FRAME=100003`,
  `INVALID_MEDIA=100004`, `MAX_AGENT_MESSAGE_MS=30_000`.
- `packages/va_sim/src/va_sim/telnyx/playout.py` — `Playout(fmt)`: `push(audio)`, `mark(name) -> bool` (True = echo
  now), `clear() -> list[str]`, `tick() -> tuple[bytes, list[str]]` (wire bytes played this tick incl. padding,
  marks now due), `queued_ms`. Pure, unit-tested without sockets.
- `packages/va_sim/src/va_sim/telnyx/media_session.py` — `MediaSession(state, call, stream_id, params, events:
  CallMedia)`: `async run()`, `stop()`, `queue_caller(wire: bytes, utterance: dict | None)`, `queue_dtmf(digits)`,
  `inject(frame)`, `connected` property, counters. Connect + `asyncio.TaskGroup` with tick and receive loops.
- `packages/va_sim/src/va_sim/telnyx/calls.py` — `StreamState` gains `session: MediaSession | None` (TYPE_CHECKING
  import) excluded from the summary; summary fields of decision 11; `caller_dtmf` list.
- `packages/va_sim/src/va_sim/telnyx/call_media.py` — start/stop wiring (decision 7), `stream_failed(call, reason)`.
- `packages/va_sim/src/va_sim/telnyx/call_actions.py` — codec checks for dial (in the dial route — find where dial
  validates), answer, streaming_start.
- `packages/va_sim/src/va_sim/telnyx/call_control.py` (or a new `media_control.py` if call_control passes ~150
  lines) — say / dtmf / media/inject / agent_audio.wav routes; register in `routes.py`/`server.py` like the others.
- `packages/va_sim/src/va_sim/telnyx/models.py` — `media_jitter` in config + patch; bodies `CallerSay`,
  `CallerDtmf`, `InjectFrame` (in call_models.py next to `RemoteHangup`).
- specs/30 §3.2 Decision; AGENTS.md P03-T03b note (media session API, test WS agent fixture, codec limits,
  accelerated clock + sockets); a Ralph memory.

### Tests (`packages/va_sim/tests/telnyx/`)
- Support: `packages/va_sim/tests/support/media_agent.py` — `MediaAgent`, an async context manager running
  `websockets.asyncio.server.serve(handler, "127.0.0.1", 0)`; records request path+query and headers, every received
  frame (parsed) with `state.clock.virtual_ms()`, exposes `url`, `send(obj)`, `send_raw(text|bytes)`,
  `wait_for(predicate, *, frames=...)` (awaits an asyncio.Condition, never sleeps/polls), `close(code)`; option to
  reject the handshake (`process_request` returning 403). Fixture `media_agent` in tests/telnyx conftest (or
  sim_helpers if apps/sim needs it).
- Existing `test_calls_actions.py` STREAM_URL tests: switch to `media_agent.url` (else the sim tries `media.sim`
  and adds `streaming.failed`); keep their webhook assertions.
- `test_media_format.py`: format selection matrix (default/PCMU/L16 × rates), 422 pointers for PCMA/G722/OPUS/
  AMR-WB/`mp3`/22 050 on dial, answer, streaming_start (no state change after a 422), decode to 8 kHz tracks (μ-law
  decode equals codec; L16 16 k pair-average).
- `test_media_frames.py`: builders produce exactly the decision-4 shapes; parser fault table (each decision-6 case →
  code), valid media/mark/clear.
- `test_playout.py`: 3 frames + mark → mark due on the 3rd tick; mark on empty queue → immediate; partial frame padded;
  clear drops audio, returns pending marks in order, next tick silence; marks after clear behave normally.
- `test_media_session.py` (accelerated clock, `telnyx_helpers` + `media_agent`):
  signature headers verify with nacl over `f"{ts}|{url}"` incl. query, timestamp = sim unix time, Bearer header iff
  `stream_auth_token`; first frames `connected`, `start` (fields, media_format, seq "1"), then `media` with chunk 1..n,
  timestamps 0,20,…, contiguous sequence numbers, 160-byte silence payloads 0xFF; L16 16 kHz → 640-byte zero payloads
  and `media_format {L16,16000,1}`; `say` → frames decode (μ-law) to audio whose `decode_marker` = returned id, 202
  body, duration_ms = 180 + len·70; L16 say decodes via `decode_marker(..., 16000)`; agent media 3 frames + mark →
  mark echoed only after ≥ 2 further caller media frames and `agent_audio.wav` contains the decoded audio at the right
  offset; mark with empty queue echoed before the next media frame; clear with queued 1 s audio + 2 marks → both
  marks echoed at once, agent track afterwards silent; malformed frames → error codes 100003/100004 and the
  connection stays open; dtmf control → `dtmf` events per digit with occurred_at; inject → verbatim frame; jitter →
  received chunk order 2,1,4,3 with original timestamps; streaming_stop → `stop` frame then close 1000, then
  `streaming.stopped` webhook; hang-up → `stop` + `streaming.stopped` before `call.hangup` (existing order);
  handshake rejected / nothing listening (closed port) → `streaming.failed` `connection_failed` and summary
  `stream: null`; agent closes → `streaming.failed` `connection_closed`; recording WAV channel 0 contains the caller
  utterance, channel 1 the agent audio (dual); timeline utterance start/end/first_agent_audio_ms/latency_ms
  (agent replies after end → positive latency; silent agent media ignored); 409 `not_streaming` / `call_ended`, 404;
  `/sim/reset` mid-stream closes the socket (agent sees close) and leaves no pending tasks (`state.tasks.pending`
  == 0 after drain).
- One realtime-clock test (`RealtimeClock`): 10 media frames take ≥ 0.16 s wall and timestamps advance by 20.

### Gates
`make fmt lint typecheck placeholders test`; `make check` (va_sim coverage ≥ 85 %, jscpd 0 — no copied frame
builders between tests and src: tests build expected dicts inline or parse; openapi drift unaffected);
`uvx radon cc -s -a packages/va_sim/src` (new functions ≤ B); pip-audit + pnpm audit. No e2e (no platform flow yet).

### Risks
- Accelerated clock + real sockets: the tick loop is a permanent sleeper; time advances whenever the loop waits on
  socket I/O, so tests must assert orderings/counts, never exact virtual ms of socket-driven events (only of
  sim-generated ones like media timestamps). `telnyx_helpers.at/settle` still work; make sure `idle()`/`drain()`
  helpers do not wait forever on the perpetual tick task (stop the stream or hang up before draining).
- Existing T02b tests that pass `stream_url` now open sockets — migrate them to `media_agent` in the same commit.
- Keep the media session CPU-light per tick (base64 + small numpy); synthesis only in `to_thread`.
- Exceptions inside the TaskGroup must end the session cleanly (log via supervisor), never hang the call.

## Build P03-T03b (Builder, 2026-10-29)

Implemented per plan. New: `va_sim.telnyx.media_format` (MediaFormat, stream/agent format, `check_stream_params`
on dial/answer/streaming_start), `media_frames` (builders + `parse_agent_frame`), `playout` (Playout, CallerQueue),
`media_session` (MediaSession), `media_control` (say/dtmf/media/inject/agent_audio.wav). Wiring: `CallMedia`
start/stop/`stream_failed` (streaming.stopped waits for socket close via Outgoing.prepare), `StreamState.session`
+ `summary()`, `Call.caller_dtmf`, `TelnyxSimConfig.media_jitter`, `call_control.find_call` shared. Deviations from
the plan (all in the §3.2 Decision): `Playout.mark()`/`clear()` return nothing — immediate and cleared marks are
handed out by the next `tick()` (the tick loop is the only socket writer, so errors and immediate marks go out at
the next tick, ≤ 20 ms); invalid agent frames are counted as `received.invalid`; on the accelerated clock a holder
task freezes virtual time while connecting (otherwise the pump jumped to the next call timer, e.g. the 4 h time
limit, during the handshake); at connect both call tracks are padded with silence to the time since answer.
Tests: media_agent support + fixture; test_media_format/frames/playout/session (22 session tests incl. realtime
pacing); test_calls_actions stream tests migrated to media_agent; config test gains media_jitter.
Gates: make fmt 0, lint 0, typecheck 0 (489 files), placeholders 0 findings, make check 0 (3964 passed, coverage
97.94 %, openapi drift current, spec refs 0), radon: new code all ≤ B, pip-audit no known vulns. va_sim suite 3×
stable (527 passed).

## Plan P03-T03b (re-plan after build.blocked: missing backpressure evidence, Planner, 2026-10-29)

Blocker is not a code problem: the Builder's `build.done` for P03-T03b lacked the required evidence keys, so the
loop rejected it (3×, task abandoned). The full P03-T03b implementation (media_format, media_frames, playout,
media_session, media_control, CallMedia wiring, tests, specs/30 §3.2 Decision, AGENTS.md note) is in the working
tree. No split is needed; the task stays as planned above.

Builder actions (no new design):
1. Re-run all gates on the current tree and capture real output: `make fmt lint typecheck placeholders test`,
   `make check` (coverage ≥ 85 % per package, jscpd, openapi drift, spec refs, TS vitest),
   `uvx radon cc -s -a packages/va_sim/src` (record the average grade/score), `npx jscpd@4` over va_sim src+tests,
   `uvx pip-audit --strict` on `uv export` and `corepack pnpm audit --audit-level high`.
2. If anything fails, fix it inside P03-T03b scope only.
3. Append a short "Build P03-T03b gate re-run for evidence" note here, then emit build.done with EVERY required key,
   exactly in this form (values from the real runs):
   `ralph emit "build.done" "tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass, complexity: <radon avg e.g. A (2.1)>, duplication: pass, specs: pass - P03-T03b media WS client"`
   Do not drop or rename any key; `performance` is optional. Keep the payload a single plain string.

## Build P03-T03b gate re-run for evidence (Builder, 2026-10-29)

Re-ran every gate on the current tree. First `make check` run: 1 failed / 3963 passed — the failure was an unrelated
pre-existing flake, `apps/api/tests/test_orgs_integration.py::test_cache_invalidation_failure_is_logged` (structlog
`capture_logs` misses cached loggers under xdist in the full suite; passes alone). Fixed deterministically: the test
monkeypatches `va_api.auth.api_keys._log` with a recording logger instead of `capture_logs`.
Results after fix: make fmt 0, lint 0, typecheck 0, placeholders 0 findings, make check 0 (3964 passed, coverage
97.94 %, TS vitest widget 22 / dashboard 52 / sdk 48 passed, openapi drift current, spec refs 0 problems),
radon va_sim avg A (2.13) (only C is pre-existing `scenario._locate`, untouched), jscpd va_sim src+tests 0 clones,
pip-audit --strict no known vulns, pnpm audit high: no known vulnerabilities.

## Review P03-T03b (Reviewer, 2026-10-29)

Approved. Re-ran make lint, typecheck, placeholders (0 findings), make test (3964 passed, coverage 97.94 %).
Checked frame builders against specs/06 §8 (connected/start/media/dtmf/mark/error/stop shapes, string
sequence_number/chunk/timestamp, error codes 100003/100004), stream signature `{ts}|{url}` per §4.2, codec 10015
checks on dial/answer/streaming_start before state changes, streaming.failed reasons, supervised session on
state.tasks with cancellation closing the socket, synthesis via to_thread, and the race re-check after synthesis
in `say`. Tests use condition waits (no polling sleeps) apart from the one realtime pacing test. The unrelated
va_api capture_logs flake fix is deterministic. Non-blocking note: `say` registers the utterance id before the
post-synthesis re-check, so a call that ends mid-synthesis consumes an id without queuing audio (harmless).
