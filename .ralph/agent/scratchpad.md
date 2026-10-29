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
