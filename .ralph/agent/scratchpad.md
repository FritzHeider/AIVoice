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

## Plan P03-T03c — FakeTelnyx scenario caller runner (Planner, 2026-10-29)

Goal: when a leg's media session connects, a supervised `CallerScript` plays the scenario `greeting_audio`, then runs
`steps` (transfer legs: `transfer_target.steps`) against the stream, recording per-step outcomes in the call summary
and timeline. Spec refs: specs/30 §3.2 media WS client, §3.5 steps/greeting/transfer_target/barge-in.

### Decisions (Builder writes ONE `> Decision (2026-10-29): scenario caller runner (P03-T03c).` block at the end of
specs/30 §3.5, and a one-line pointer in the P03-T03b §3.2 Decision is NOT needed — just replace nothing)
1. Start: one script per leg, started once, on the first `MediaSession` connect of that leg (after `connected`+`start`
   are sent) when the leg has a scenario with a greeting or steps. Outgoing/inbound legs run `greeting_audio` then
   `steps` (role `caller`); transfer legs (created by `CallEngine.transfer`, new `Call.transfer_leg: bool`) run only
   `transfer_target.steps` (role `transfer_target`, no greeting). A transfer leg without its own stream runs on its
   bridged peer's media session: started in `CallEngine.bridge` when the transfer leg has no script and the peer has a
   connected session (the platform hears the transfer target on the original call's stream). No media → no script
   (summary `script: null`). A restarted stream does not restart a script.
2. Audio preparation: at script start every utterance (greeting text and each `say`, in document order) is registered
   (`state.register_utterance(text, language, tuple(text.split()))` → deterministic ids) and synthesized in the stream
   format, and in-band DTMF tones / the greeting WAV are rendered, all in ONE `asyncio.to_thread` call while the
   accelerated clock is held (generalize MediaSession's `_hold_clock` into `va_sim.tasks.hold_clock(clock, tasks, *,
   name)` async context manager, no-op for realtime; MediaSession `_connect` uses it). So step timing on the
   accelerated clock does not depend on thread speed. Utterance-limit ValueError → that step `failed`.
3. Encoding: new `MediaFormat.encode(pcm: NDArray[np.int16]) -> bytes` (PCMU μ-law, else L16 big-endian at its rate).
   Synthesis = `synthesize_pcm16(id, text, sample_rate=fmt.sample_rate, seed=state.utterance_seed(id))` → `fmt.encode`.
   This also fixes the existing `/say` route for L16 8 kHz streams (synthesize_wire(8000) produced μ-law): the route
   and the script share `va_sim.telnyx.caller_audio.render_utterance(state_seed_fn..)` (see Files) — no copies (jscpd).
4. `greeting_audio`: a value ending in `.wav` (case-insensitive) is a WAV path, else text spoken like `say` (language
   `en`). `load_scenario(..., base_dir=)` resolves a relative WAV path against the scenario file directory (rename
   `_resolve_rules` → `_resolve_paths`, also covers `llm_rules`); scenarios POSTed to `/sim/telnyx/scenarios` keep the
   path as given (relative to the sim process working directory). WAV: PCM16 (sampwidth 2) only, any channel count
   (averaged to mono), any rate (linear `np.interp` resample to the stream rate), ≤ 60 s; read with stdlib `wave`
   inside the preparation thread. Missing/invalid file → greeting `failed` with `detail`, steps still run. Timeline
   entry for a WAV greeting: `utterance_id: null`, `text: <file name>` (`MediaSession.queue_caller` accepts
   `tuple[int | None, str]`).
5. Agent speech detection (`va_sim.telnyx.agent_speech`): a 20 ms frame of *played-out* agent audio (8 kHz PCM16, the
   frame the playout tick just played) is speech when its RMS > `AGENT_SPEECH_LEVEL` (256). `SpeechDetector(gap_ms)`
   fed one bool per tick keeps `turn_ms` (speech ms of the current turn; silent gaps shorter than `gap_ms` keep the
   turn) and `silence_ms` (silence since the last speech frame); a silence of ≥ `gap_ms` before the turn qualifies
   resets `turn_ms` to 0. A fresh detector starts with each step (speech before the step is not counted).
6. Steps (each entry `{index, type, status, started_ms, ended_ms, detail}` + `utterance_id` for `say`; status
   `pending | running | ok | timeout | failed | cancelled`; times = sim `virtual_ms`). A timeout or failure is
   recorded and the script continues with the next step.
   - `wait_agent_speech {min_ms, then_silence_ms, timeout_ms}`: detector `gap_ms = then_silence_ms`; ok when
     `turn_ms ≥ max(min_ms, 20)` and `silence_ms ≥ then_silence_ms` (then_silence 0 → ok as soon as the turn
     qualifies); timeout after `timeout_ms` from step start.
   - `say {text, language, barge_in_after_agent_ms}`: null → queue now; N → detector `gap_ms = 700`, queue on the tick
     after `turn_ms ≥ N` (N = 0: first speech frame); no qualifying speech within 10 000 ms (`BARGE_IN_TIMEOUT_MS`) →
     `timeout`, nothing spoken. ok when the utterance's last byte has been sent (`ended_ms` = its timeline `end_ms`).
   - `dtmf {digits, inband}`: digits split at `w`; each run is sent then `w` waits 500 ms on the sim clock after the
     run has gone out. `inband: false` → `MediaSession.queue_dtmf` (one Telnyx `dtmf` event per tick); `inband: true`
     → tones only (no `dtmf` events): per digit 100 ms `synthesize_dtmf`-equivalent tone rendered in the stream
     format (`synthesize_dtmf(..., sample_rate)` for PCMU 8k/L16 16k; for L16 8k decode+`fmt.encode` — simplest: add
     a pcm variant or decode via `MediaFormat`) followed by 100 ms silence, queued as caller audio. ok once the last
     digit/tone is sent.
   - `silence {ms}`: the caller stays quiet: sim-clock sleep of `ms`; ok.
   - `expect_hangup {by, timeout_ms}`: waits for the leg to end; `agent` ⇔ `hangup_source == call.platform_side`,
     `caller` ⇔ `call.remote_side`, `any` always; mismatch → `failed` `detail: "hung up by <source>"`; not ended within
     `timeout_ms` → `timeout`.
   - `hangup {}`: `engine.hang_up(call, "normal_clearing", call.remote_side)`; ok.
7. Ending: when the leg ends, a running `expect_hangup` resolves (ok/failed) and every other running or pending step
   becomes `cancelled` (`detail: "call ended"`); when the media session the script uses stops while the leg is up
   (streaming_stop, peer close, failed), running/pending steps become `cancelled` (`detail: "media stream stopped"`).
   Script `status`: `running` → `done` (all steps ran) | `cancelled`. `/sim/reset` cancels scripts (state.tasks).
8. Summary and timeline add `script: null | {role, status, greeting: null | {source: "text"|"wav", status,
   utterance_id, started_ms, ended_ms, detail}, steps: [...]}`.

### Files
- `packages/va_sim/src/va_sim/tasks.py` — `hold_clock(clock: SimClock, tasks: Supervisor, *, name: str)` async CM.
- `packages/va_sim/src/va_sim/telnyx/media_session.py` — use `hold_clock`; listener hooks: `add_listener(l)`,
  `remove_listener(l)` with Protocol `MediaListener { on_tick(tick: AgentTick) -> None; on_media_end() -> None }`,
  `AgentTick(at_ms: int, speech: bool)` emitted at the end of every `_tick` (speech from `agent_speech.is_speech` on
  the played 8 kHz frame); `on_media_end` from `stop()` and when `_run` ends (connect failure / peer close);
  `queue_caller(...) -> int` returns the end byte offset; `caller_sent` property (bytes sent, via new
  `CallerQueue.sent`); `dtmf_pending` property; after `start` is sent call `self._events.media_connected(call, self)`.
- `packages/va_sim/src/va_sim/telnyx/playout.py` — `CallerQueue.sent` property.
- `packages/va_sim/src/va_sim/telnyx/media_format.py` — `MediaFormat.encode(pcm)`.
- `packages/va_sim/src/va_sim/telnyx/agent_speech.py` — `is_speech(pcm8k: bytes) -> bool`, `SpeechDetector`.
- `packages/va_sim/src/va_sim/audio/wav_input.py` — `read_wav_pcm(path: Path, sample_rate: int, *, max_ms=60_000)
  -> NDArray[np.int16]` (ValueError with a readable message on non-PCM16, empty, too long; OSError passes through).
  Export from `va_sim.audio`. (audio must not import va_sim.state.)
- `packages/va_sim/src/va_sim/telnyx/caller_audio.py` — pure CPU helpers: `render_text(utterance_id, text, fmt,
  seed) -> bytes`, `render_dtmf_tones(digits, fmt) -> bytes`, `render_wav(path, fmt) -> bytes`; used by the `/say`
  route (keep its register → to_thread → re-check flow) and by the script's preparation.
- `packages/va_sim/src/va_sim/telnyx/caller_script.py` — `CallerScript(state, call, steps, greeting, role)`:
  `start(session)` (spawn on state.tasks, name `telnyx-script-<ccid>`), `wake()` (called by the engine on hang-up),
  `MediaListener` impl, `summary() -> Record`; internal `_prepare()` (decision 2), `_run_step(index, step)` per type
  (a match on the step model classes from `va_sim.scenario`), `_wait(predicate, deadline_ms)` that waits on an
  `asyncio.Event` set by ticks/wake/media end, raced with `clock.sleep(remaining)` (cancel the loser), and a
  `_media()` accessor (the session the script was started with; ended → `None`). If it passes ~250 lines split the
  step implementations into `caller_steps.py`. Radon ≤ B per function.
- `packages/va_sim/src/va_sim/telnyx/calls.py` — `Call.script: CallerScript | None` (TYPE_CHECKING), `transfer_leg:
  bool = False`; `summary()`/`timeline()` add `script`.
- `packages/va_sim/src/va_sim/telnyx/call_media.py` — `media_connected(call, session)`: build the script from
  `state.scenarios[call.scenario_id]` per decision 1 (no-op if `call.script` exists, no scenario, or nothing to run).
- `packages/va_sim/src/va_sim/telnyx/call_engine.py` — `transfer()` sets `transfer_leg=True`; `bridge()` starts a
  transfer leg's script on the peer's session (decision 1); `hang_up()` calls `call.script.wake()` after the state
  change (and on the bridged peer it hangs up, naturally via its own hang_up).
- `packages/va_sim/src/va_sim/telnyx/media_control.py` — `/say` uses `caller_audio.render_text`.
- `packages/va_sim/src/va_sim/scenario.py` — `_resolve_paths` (decision 4).
- specs/30 §3.5 Decision; AGENTS.md P03-T03c note (script lifecycle, preparation under hold_clock, test helper);
  a Ralph memory.

### Tests
- `tests/telnyx/test_agent_speech.py` (pure): is_speech threshold (RMS 255 vs 257 sine/DC frames); detector: turn
  qualifies + silence → ready; short blip then gap ≥ gap_ms resets; gaps < gap_ms keep the turn; then_silence 0.
- `tests/audio/test_wav_input.py`: 8 kHz mono round trip (pcm16_wav → read), 16 kHz stereo averaged + resampled to
  8 kHz (tone frequency preserved via goertzel_power), 8-bit WAV / empty / > max → ValueError, missing → OSError.
- `tests/telnyx/test_media_format.py` add `encode` for PCMU, L16 8k, L16 16k (decode(encode(x)) ≈ x).
- `tests/test_scenario.py`: relative `.wav` greeting resolves against base_dir; text greeting untouched; absolute kept.
- `tests/telnyx/test_media_session.py` add: `/say` on an L16 8 kHz stream decodes via `decode_marker(l16, 8000)`.
- `tests/test_tasks.py`: `hold_clock` freezes accelerated time while a worker thread runs; no-op on realtime.
- `tests/telnyx/test_caller_script.py` (accelerated clock, `telnyx_helpers` + `media_agent`; a support helper
  `until(state, predicate, limit_s)` looping `await state.clock.sleep(0.02)` up to a virtual limit — sim-clock
  waits, no wall sleeps; and a loud-agent-audio builder (μ-law sine at −12 dBFS via `va_sim.audio.codec`)):
  greeting text first in timeline + caller media decode to its marker id + greeting ok and started on the first
  media frame after connect (no virtual time lost to preparation); greeting WAV (tmp_path file, PCMU and L16 16 kHz
  stream) → caller audio carries the tone; missing WAV → greeting failed, steps still run; wait_agent_speech ok after
  agent 600 ms speech then silence (ended_ms − end of played speech ≥ then_silence_ms, < +40); blip shorter than
  min_ms → timeout and next step runs; say ok with ended_ms == timeline end_ms; barge-in 500 → utterance start_ms −
  first played agent speech ms (from agent track offset + answered_ms) in [500, 540] while agent audio is still
  playing; barge-in with a silent agent → timeout, no utterance queued; dtmf "12w3" → dtmf frames 1,2 at successive
  ticks, 3 ≥ 500 ms later, caller_dtmf logged; inband "5" → no dtmf frames, caller audio has the 770/1336 Hz tones;
  silence 1000 → next say starts ≥ 1000 ms after the previous ended; expect_hangup by agent with platform hangup → ok;
  by caller with platform hangup → failed "hung up by caller"; timeout → timeout; hangup step → call.hangup with
  `hangup_source: callee`, cause normal_clearing, script done; inbound scenario hangup step → `hangup_source: caller`;
  transfer: transfer_target.steps say runs on A's stream after bridge, B summary `script.role: transfer_target`;
  platform hang-up mid-wait → running+pending cancelled, script cancelled; streaming_stop mid-script → cancelled
  "media stream stopped"; scenario without steps/greeting and call without scenario → `script: null`; `/sim/reset`
  mid-script → agent sees close and `state.tasks.pending == 0` after the stream's tasks finish; timeline `script`
  equals summary `script`.

### Gates
`make fmt lint typecheck placeholders test`; `make check` (va_sim coverage ≥ 85 %, jscpd 0, spec refs); `uvx radon
cc -s -a packages/va_sim/src` (new functions ≤ B); `uvx pip-audit --strict` on `uv export`; `corepack pnpm audit
--audit-level high`. No e2e (no platform flow yet). build.done payload must carry every key: tests, lint, typecheck,
audit, coverage, complexity, duplication, specs.

### Risks
- Accelerated clock + sockets: agent frames arrive at racy virtual times; assert relative to *playout* (agent track
  offsets, step started/ended ms), never absolute arrival ms. Stop the stream / hang up before any `tasks.drain()`.
- The runner must never block the tick loop: listeners only set an Event / feed a detector (O(1) per tick).
- Two scripts can share A's session (A's steps + transfer target): both queue into one CallerQueue (sequential audio)
  — offsets from `queue_caller` keep each script's completion check correct.
- `hang_up` order: the script's `wake()` runs after `call.state = "hangup"` and hangup_source are set, so an awaiting
  `expect_hangup` sees the source; session `on_media_end` fires earlier in the same hang_up but the script checks
  `call.ended` first when it resumes.
- Existing `test_call_control` INBOUND_YAML (`steps: [hangup]`, no stream) must stay unaffected (no media → no script).

## Build P03-T03c (Builder, 2026-10-29, after empty review.rejected)

The review.rejected event carried no payload and no `## Review P03-T03c` findings were written (the previous
build iteration left no Build section or build.done event either), so this activation re-verified the uncommitted
P03-T03c implementation against the plan: CallerScript (caller_script.py 192 lines) + ScriptSteps
(caller_steps.py 250 lines), agent_speech, caller_audio, wav_input, hold_clock, MediaFormat.encode, listeners on
MediaSession, `script` in summary/timeline, `_resolve_paths`, specs/30 §3.5 Decision, AGENTS.md note. No code
changes were needed.
Gates (all run this iteration): make fmt 0, lint 0, typecheck 0, placeholders 0; va_sim pytest 575 passed;
make check 0 (4012 passed, coverage 97.94 %, vitest widget 22 / dashboard 52 / sdk 48, openapi drift current,
spec refs 0 problems); new modules coverage 97-100 %; radon va_sim avg A (2.20), only C is pre-existing
`scenario._locate`; jscpd va_sim src+tests 0 clones; `uv export --no-emit-workspace` + `uvx pip-audit --strict`
no known vulns (plain `uv export` fails: pip-audit cannot install workspace editables); pnpm audit high: none.

## Plan P03-T03c (re-plan after build.blocked: missing backpressure evidence, Planner, 2026-10-29)

Blocker is not a code problem: the Builder's `build.done` for P03-T03c lacked the required evidence keys (same
pattern as P03-T02b/T03b). The full P03-T03c implementation (caller_script.py, caller_steps.py, agent_speech.py,
caller_audio.py, audio/wav_input.py, tasks.hold_clock, MediaFormat.encode, MediaSession listeners, `script` in
summary/timeline, scenario `_resolve_paths`, specs/30 §3.5 Decision, AGENTS.md note, tests) is in the working tree
and its last gate run was green. No split is needed; the task stays as planned above.

Builder actions (no new design):
1. Re-run all gates on the current tree and capture real output: `make fmt lint typecheck placeholders test`,
   `make check`, `uvx radon cc -s -a packages/va_sim/src` (record average grade/score; new functions ≤ B),
   jscpd over va_sim src+tests, `uv export --no-emit-workspace` | `uvx pip-audit --strict`, and
   `corepack pnpm audit --audit-level high`.
2. If anything fails, fix it inside P03-T03c scope only (or deterministically fix a pre-existing flake).
3. Append a short "Build P03-T03c gate re-run for evidence" note here, then emit build.done with EVERY required key,
   exactly in this form (values from the real runs), as one plain string:
   `ralph emit "build.done" "tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass, complexity: <radon avg e.g. A (2.20)>, duplication: pass, specs: pass - P03-T03c scenario caller runner"`
   Do not drop, rename, or reorder keys; `performance` is optional.

## Build P03-T03c gate re-run for evidence (Builder, 2026-10-29)

No code changes. Gates on the current tree: make fmt 0, lint 0, typecheck 0, placeholders 0 (0 findings).
First `make check` run: 1 failed / 4011 passed - `apps/api/tests/test_dev_seed.py::test_first_run_creates_org_owner_credit_and_working_keys`
hit the 60 s pytest-timeout with host load avg ~7.9 (stack ended in the coverage tracer, not in an I/O wait). The test
is outside P03-T03c scope, passed 3/3 in isolation (15 passed, ~6 s each). Second `make check`: exit 0, 4012 passed,
coverage 97.94 %, vitest widget 22 / dashboard 52 / sdk 48, openapi drift current, spec refs 0 problems.
radon va_sim avg A (2.20), only C is pre-existing `scenario._locate`; jscpd va_sim src+tests 0 clones;
`uv export --no-emit-workspace --frozen` + `uvx pip-audit --strict` no known vulns; pnpm audit high: none.

## Review P03-T03c (Reviewer, 2026-10-29)

Approved. Re-ran: make lint 0, typecheck 0 (mypy 497 files), placeholders 0. make test run 1: 1 failed / 4011
passed - `apps/api/tests/test_cross_tenant_routes.py::test_every_tenant_route_is_isolated` hit the 60 s
pytest-timeout at host load ~11 (no apps/ files in the diff; passes in 4.8 s alone). Run 2: 4012 passed, exit 0.
Same load-driven timeout pattern as the Builder's test_dev_seed failure -> logged as a separate fix task.
radon: only pre-existing C `scenario._locate`. Diff matches specs/30 §3.5 Decision P03-T03c rules 1-8; waits are
sim-clock/tick driven, listeners O(1), prep under hold_clock + to_thread, error paths (missing WAV, id exhaustion,
timeouts, interruption, reset) tested.
Notes (not blocking, consistent with the Decision text): a pending `expect_hangup` is cancelled (not ok) when the
leg ends during an earlier step, and a running `expect_hangup` is cancelled "media stream stopped" if the platform
stops streaming before hanging up; revisit if scenario assertions need it.

## Plan P03-T04 — FakeElevenLabs TTS: multi-context WS + REST (Planner, 2026-10-29)

Spec refs: specs/30 §3.3 (primary simulator contract), specs/09 §2-§3, §8, §9, §13. P03-T03c committed (e11eca6).

### Decisions (Builder writes ONE `> Decision (2026-10-29): FakeElevenLabs TTS (P03-T04).` block at the end of
specs/30 §3.3, and a one-line `> Decision (2026-10-29):` under specs/09 §13 saying specs/30 §3.3 governs the
simulator: 60 ms/char, 200 ms chunks, 300 Hz + (ord mod 12)·25 Hz tones at −10 dBFS, not 55 ms/100 ms/440 Hz)
1. Audio generator (pure, `va_sim.elevenlabs.tts_audio`): each character of the generated text is 60 ms; whitespace
   (`c.isspace()`) is silence, any other char a sine of `300 + (ord(c) % 12) * 25` Hz, peak `dbfs_peak(-10)`, phase 0
   at each char start. Samples/char = `rate * 60 // 1000`. Formats: `ulaw_8000` (va_sim.audio.codec.ulaw_encode),
   `pcm_16000|pcm_22050|pcm_24000|pcm_44100` (signed 16-bit little-endian mono), `mp3_44100_128`: valid MPEG-1
   Layer III mono 128 kbps 44.1 kHz frames with all-zero side info/main data (decode to silence; no encoder dependency;
   header `FF FB 90|92 C0`, frame bytes `floor((n+1)*144*128000/44100) - floor(n*144*128000/44100)` with the padding
   bit set when that is 418), frame count `ceil(duration_ms * 44.1 / 1152)`. Any other format → validation error.
2. Chunking: audio split into 200 ms pieces (last one shorter); each piece's alignment holds the chars whose start
   time lies in `[chunk_start, chunk_end)`: `{"chars": [...], "charStartTimesMs": [start - chunk_start],
   "charDurationsMs": [60, ...]}`; no char start in the piece → `null`. `normalizedAlignment` == `alignment`.
   mp3 pieces = the frames whose start time lies in the 200 ms window.
3. WS `GET /v1/text-to-speech/{voice_id}/multi-stream-input` (Starlette `WebSocketRoute`). Always `accept()`, then
   on error close with code 1008 and a reason. Auth: `xi-api-key` header, else the first message's `xi_api_key` or
   `authorization` (`Bearer <k>` or raw). Missing → close 1008 `"Invalid API key"`; same for a key not accepted (4).
   Query: `model_id` (default `eleven_flash_v2_5`, any non-empty string, recorded), `output_format` (default
   `mp3_44100_128`, set in 1), `inactivity_timeout` int 1..180 default 20, `sync_alignment`, `auto_mode`,
   `enable_ssml_parsing`, `enable_logging` (bool `true|false`), `apply_text_normalization` (`auto|on|off`),
   `language_code` (recorded). Invalid → close 1008 reason `"invalid <param>: <why>"`. Any voice_id accepted in this
   task (P03-T05 adds the voice registry and `voice_not_found`).
4. Accepted keys: `ElevenLabsStore.api_keys` empty (default) → any non-empty key; non-empty → only those keys.
   Control `PUT /sim/elevenlabs/api-keys {"keys": [str, ...]}` (204), `GET` → `{"keys": [...]}`; reset clears.
5. Client messages: JSON object (non-object/invalid JSON → close 1008 `"invalid message"`); unknown fields ignored.
   `close_socket: true` first; else `context_id` (missing → `"default"`). Unknown context → created (max 5 open per
   socket; a 6th → error frame `{"message": "Maximum of 5 concurrent contexts per connection exceeded", "error":
   "max_active_conversations", "code": 1008}` and the message is ignored, socket stays open). A context id that was
   closed on this socket → error frame `error: "context_closed"`, message ignored. `voice_settings` and
   `generation_config.chunk_length_schedule` (list of 1-4 ints each 50..500, else error frame `invalid_message` and
   the message ignored) are taken from the context's first message only. Default schedule `[120, 160, 250, 290]`
   (ElevenLabs default; specs/30's "[0] (default 120)" is its first element).
6. Generation per context: `text` is appended to the context buffer. A generation takes the whole buffer when
   (a) `flush: true` (with or without text), or (b) `len(buffer.strip()) >= schedule[min(k, len-1)]` where k = the
   context's generations so far. A whitespace-only buffer generates nothing (stays buffered; flush clears it). The
   generated text is the buffer verbatim (alignment chars = its chars incl. spaces). Generations of one context run
   in order on a per-context worker; the first chunk of a context's FIRST generation waits `state.latencies
   .tts_ttfa_ms` on the sim clock after its trigger; later chunks/generations are sent back to back. Contexts
   interleave freely; sends go through one per-socket lock. Messages `{"audio": b64, "contextId": id,
   "alignment": ..., "normalizedAlignment": ...}` (key order as written). Keepalive `{"text": "", "context_id":
   "ka"}` only refreshes the inactivity timer (creates context `ka` if new; empty text never generates).
7. `close_context: true`: unflushed buffer discarded, in-flight generation stops before its next chunk (unsent audio
   dropped), queued generations dropped, then `{"isFinal": true, "contextId": id}`. specs/30's "generates audio on
   close_context" is superseded: close_context only stops (specs/09 §8/§13, live test "close_context mid-stream
   stops audio"). `close_socket: true`: unflushed buffers discarded, triggered generations finish, `isFinal` for each
   open context, close 1000.
8. Inactivity: sim-clock timer from the last message carrying a `text` field; expiry → close 1008
   `"Have not received a new text input within the timeout of {N} seconds."` (N = inactivity_timeout). On an
   accelerated clock this fires as soon as the loop idles past N virtual seconds (documented).
9. Records `ElevenLabsStore.tts_requests: list[TtsRequest]` one per context (WS) or request (REST):
   `{id ("ttsreq_<n>"), transport: "ws"|"rest", voice_id, model_id, output_format, context_id (null for rest),
   text (all text received), generated_text, characters (generated chars), started_ms, ended_ms (null while open),
   interrupted (true when close_context/close_socket/disconnect discarded text or dropped audio),
   end_reason: null|"close_context"|"close_socket"|"inactivity"|"disconnect"|"fault"|"reset"|"completed"}`
   (REST → `completed`). Control `GET /sim/elevenlabs/tts/requests` → `{"requests": [...]}`.
10. Faults: `PUT /sim/elevenlabs/tts/fault {"kind": "disconnect"|"error"|"delay", "after_chunks": int>=0 = 0,
    "delay_ms": int>=0 = 0, "times": int>=1 = 1}` (204), `GET` → `{"fault": null|{...,"remaining"}}`, `DELETE` 204;
    reset clears. WS: checked before sending chunk number `after_chunks` (0-based within a context); disconnect →
    close 1011 `"simulated disconnect"`; error → frame `{"message": "Simulated TTS failure", "error":
    "internal_error", "code": 1011}` then close 1011; delay → sim sleep `delay_ms` then send. Each firing decrements
    `remaining`; 0 clears. REST: `error` → 500 `{"detail": {"status": "internal_error", "message": "Simulated TTS
    failure"}}`, `delay` → extra latency before the response; disconnect does not apply to REST.
11. REST (JSON body `{text (1..40 000 chars), model_id?, voice_settings?, language_code?, apply_text_normalization?,
    ...extra ignored}`, query `output_format` default `mp3_44100_128`, auth `xi-api-key`):
    - `POST /v1/text-to-speech/{voice_id}` → full audio; `POST .../stream` → `StreamingResponse` of the 200 ms pieces
      (first after tts_ttfa_ms); `POST .../with-timestamps` → `{"audio_base64", "alignment": {"characters",
      "character_start_times_seconds", "character_end_times_seconds"}, "normalized_alignment": same}` (absolute
      seconds, ElevenLabs REST shape). Content types: mp3 `audio/mpeg`, pcm `audio/pcm`, ulaw `audio/basic`; headers
      `x-character-count`, `request-id`. Non-stream routes also wait tts_ttfa_ms.
    - Errors: missing key 401 `{"detail": {"status": "needs_authorization", "message": "Neither authorization
      header nor xi-api-key received, please provide one."}}`; rejected key 401 `{"detail": {"status":
      "invalid_api_key", "message": "Invalid API key"}}`; invalid body/query 422 `{"detail": [{"loc", "msg",
      "type"}]}` (pydantic errors; invalid JSON → loc `["body"]`).
12. Synthesis runs `asyncio.to_thread` inside `va_sim.tasks.hold_clock` (no virtual time lost); tone tables cached
    per sample rate. `SimState.reset()` → `elevenlabs.reset()`: closes live sockets with 1012 `"simulator reset"`
    (open records end `reset`), clears records/keys/fault. Snapshot adds `"elevenlabs": {"tts_requests": n,
    "tts_sockets": open}`.

### Files
- `packages/va_sim/src/va_sim/elevenlabs/__init__.py` (docstring only), `routes.py` (`elevenlabs_routes(state)`),
  `tts_audio.py` (`OutputFormat` dataclass + `OUTPUT_FORMATS`, `parse_output_format(str) -> OutputFormat`,
  `synthesize(text, fmt) -> bytes`, `chunks(text, fmt) -> list[TtsChunk(audio: bytes, alignment: Alignment|None)]`,
  `absolute_alignment(text) -> dict`), `mp3.py` (silent frame writer), `auth.py` (header/first-message key check,
  REST error responses), `store.py` (`ElevenLabsStore`, `TtsRequest`, `TtsFault`, socket registry, `reset()`),
  `tts_ws.py` (endpoint + `TtsSocket` with reader/watchdog/context workers in one TaskGroup), `tts_context.py`
  (`TtsContext` buffer/schedule/trigger logic, pure enough to unit test), `tts_rest.py`, `control.py`
  (`/sim/elevenlabs/tts/requests`, `/tts/fault`, `/api-keys`). Keep each ≤ ~250 lines, radon ≤ B per function.
- `packages/va_sim/src/va_sim/state.py` — `self.elevenlabs = ElevenLabsStore()`, reset, snapshot.
- `packages/va_sim/src/va_sim/server.py` — `simulator_routes` adds `elevenlabs_routes` for `"elevenlabs"`.
- `specs/30` §3.3 Decision, `specs/09` §13 Decision, AGENTS.md (FakeElevenLabs TTS test notes), a Ralph memory.

### Tests (`packages/va_sim/tests/elevenlabs/`, conftest + `support/tts_helpers.py`: `tts_socket(state, url,
query, key)` websockets client helper over `running_sims(state, ports={"elevenlabs": 0})`, `recv_until_final`,
`decode_audio(fmt, b64)`; WS tests on `RealtimeClock` with `tts_ttfa_ms` 0/20 unless the test is about timing)
- `test_tts_audio.py` (pure): per-char durations for every format (bytes = 60 ms × rate × width); tone frequency
  of `"a"` = 300 + (97 % 12)·25 = 325 Hz via `goertzel_power`; space is silence; peak ≈ −10 dBFS; 200 ms chunking
  and per-chunk alignment (7 chars → 3 pieces, third alignment null; starts relative); mp3 frames sync/length/count;
  unknown format → ValueError.
- `test_tts_context.py`: schedule triggers ([50] fires at 50 non-space chars; progression uses k-th element; flush
  generates whole buffer; whitespace-only no generation; invalid schedules rejected).
- `test_tts_ws.py`: auth via header / first message / missing (close 1008 "Invalid API key") / api-keys allow list;
  invalid output_format and inactivity_timeout → 1008; flush returns audio whose total length = 60 ms·chars and
  alignment reconstructs the text; ttfa (tts_ttfa_ms 200: first chunk ≥ 200 ms after flush, wall clock) ;
  interleaved contexts A/B both complete, each context's chunks in order, both texts reconstructed; close_context
  mid-stream (long text, after first chunk) → no further audio for that context, isFinal, record interrupted;
  close_context with unflushed text → no audio, isFinal, interrupted; chunk_length_schedule auto-trigger without
  flush; keepalive produces no audio; 6th context → max_active_conversations frame, socket alive; closed id reuse →
  context_closed; invalid JSON → 1008; close_socket → in-flight finishes, isFinal per open context, close 1000;
  inactivity: AcceleratedClock state, no messages → close 1008 with the exact 20 s message (and N=5 variant);
  realtime inactivity_timeout=1 with keepalives every 0.4 s stays open 1.5 s then closes; faults: disconnect after 1
  chunk (1011), error frame + 1011, delay ≥ delay_ms, `times` consumed then cleared; `/sim/reset` with an open
  socket → close 1012 and `state.tasks.pending == 0` afterwards; tts_requests records (text, generated_text,
  characters, end_reason, transport ws).
- `test_tts_rest.py` (httpx ASGITransport over `Starlette(routes=simulator_routes("elevenlabs", state))`):
  convert ulaw/pcm_16000/mp3 lengths + content types + headers; stream yields 200 ms pieces whose concat equals
  convert; with-timestamps alignment absolute seconds and audio identical; 401 needs_authorization /
  invalid_api_key; 422 empty text / bad format / invalid JSON; fault error → 500 and delay; record transport rest.
- `test_control.py`/`test_state.py`: snapshot `elevenlabs` keys; reset clears records/keys/fault; elevenlabs control
  routes absent on the telnyx port (404).

### Gates
`make fmt lint typecheck placeholders test`; `make check` (va_sim coverage ≥ 85 %, jscpd 0, spec refs);
`uvx radon cc -s -a packages/va_sim/src` (new functions ≤ B); `uv export --no-emit-workspace --frozen | uvx
pip-audit --strict`; `corepack pnpm audit --audit-level high`. No e2e. build.done payload must carry every key:
`tests, lint, typecheck, audit, coverage, complexity, duplication, specs`.

### Risks
- Accelerated clock + real sockets: virtual time jumps while waiting on I/O, so inactivity fires instantly when
  idle; run protocol tests on RealtimeClock with small latencies; only the inactivity-close test uses accelerated.
- Never block the loop: synthesis in to_thread under hold_clock; per-socket TaskGroup cancels workers on close;
  reset must wake the handler (event) since it is sync.
- uvicorn WS close reasons must be ≤ 123 bytes (the inactivity message fits).
- Don't share message construction with the future va_elevenlabs client (phase constraint).
- `apps/sim` mounts `simulator_routes`, so its tests may need an expectation update for the new routes.

## Build P03-T04 (Builder, 2026-10-29)

Implemented FakeElevenLabs TTS per Plan P03-T04: `va_sim.elevenlabs` (`tts_audio`, `mp3`, `tts_context`,
`tts_params`, `auth`, `store`, `tts_ws`, `tts_rest`, `control`, `routes`), wired into `SimState` (store, reset,
snapshot) and `simulator_routes("elevenlabs")`. Decisions in specs/30 §3.3 (P03-T04 rules 1-11) + specs/09 §13.
Deviations from the plan text, recorded in the Decision:
- mp3 frame count is `ceil(duration·44.1/1152)` raised so the last 200 ms piece holds a frame (875 of the first
  40 000 lengths would otherwise get an empty last piece); at most one extra silent frame.
- Plan file names: params parsing split into `tts_params.py` (keeps `tts_ws.py` focused); the WS endpoint is a
  closure because Starlette treats a `functools.partial` endpoint as an ASGI app.
- REST default model_id `eleven_multilingual_v2` (ElevenLabs REST default); WS default stays `eleven_flash_v2_5`.
- A delay fault re-checks `stopped` after waiting, so close_context during a delayed chunk drops it.
Tests: tests/elevenlabs/ test_tts_audio, test_tts_context, test_tts_ws (realtime sockets; inactivity tests on
an accelerated clock), test_tts_rest, test_control_elevenlabs; test_state snapshot updated. 97 tests, 4 repeated
-n 8 runs stable; elevenlabs coverage 97 %.
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0 (0 findings); make check exit 0: 4109 passed, coverage
97.95 %, vitest widget 22 / dashboard 52 / sdk 48, openapi drift current, spec refs 0 problems. radon -nc only
pre-existing C `scenario._locate`, va_sim avg A (2.25), new code max B (10, parse_schedule); jscpd va_sim src+tests
0 clones; pip-audit --strict no known vulns; pnpm audit high: none.

## Review P03-T04 (Reviewer, 2026-10-29)

APPROVED. Re-ran: make lint 0, make typecheck 0 (mypy 514 files), make placeholders 0 findings, make test 4109
passed (coverage 97.95 %), radon -nc on va_sim/elevenlabs: no C+. Diff matches Plan P03-T04 and the specs/30 §3.3
Decision rules 1-11 (formats, mp3 header FF FB 90/92 C0, 200 ms chunking, auth, query validation, context limit,
schedule trigger, close_context/close_socket, inactivity, records, faults, REST shapes/errors, reset 1012).
Synthesis runs in to_thread under hold_clock; per-socket TaskGroup; tests cover every error path.
Non-blocking notes: a REST /stream whose client disconnects mid-body never ends its record (end_reason stays
null); `voice_settings` is accepted but not recorded; specs/30's `/sim/elevenlabs/config` stays served by the
shared control latency config (not part of this task).

## Plan P03-T05a — FakeElevenLabs STT realtime WebSocket (Planner, 2026-10-29)

P03-T05 was split (plan/phase-03.md) into T05a (this: realtime STT WS) and T05b (batch STT, voices REST, voice
registry + TTS voice_not_found, subscription). Both specs/30 §3.3 and specs/08 §10 describe the sim; specs/30 is the
sim contract. Builder writes ONE `> Decision (2026-10-29): FakeElevenLabs realtime STT (P03-T05a).` block at the end
of specs/30 §3.3 (after the P03-T04 Decision) and a one-line `> Decision (2026-10-29):` at the end of specs/08 §10
saying specs/30 §3.3 governs the simulator (fault route is `/sim/elevenlabs/stt/fault`, not `POST
/sim/elevenlabs/faults`; the Redis side channel `sim:stt:{session}` is replaced by `/sim/elevenlabs/stt/transcripts`;
partials every 300 ms of voiced audio, not 200 ms).

### Decisions (numbered as in the Decision block)
1. Route `WS /v1/speech-to-text/realtime` (Starlette `WebSocketRoute`, plain async def closure in `routes.py`).
   Always `accept()`. Auth: `xi-api-key` header only (STT messages carry no key). Missing → frame
   `{"message_type": "auth_error", "error": MISSING_KEY}` then close 1008 with the same text truncated to fit 123
   bytes (use `"Missing API key"` as the close reason); not accepted (store.accepts) → frame `{"message_type":
   "auth_error", "error": "Invalid API key"}` + close 1008 `"Invalid API key"`. Make `_MISSING_KEY` in auth.py public
   (`MISSING_KEY`) and reuse it.
2. Query (`stt_params.py`, `SttParams` frozen dataclass, `parse_stt_params(QueryParams) -> SttParams`, raises
   ValueError `"invalid <param>: <why>"`): `model_id` required non-empty (missing → `"invalid model_id: required"`);
   `audio_format` ∈ `ulaw_8000 | pcm_8000 | pcm_16000 | pcm_22050 | pcm_24000 | pcm_44100 | pcm_48000`, default
   `pcm_16000` (ElevenLabs default; pcm = signed 16-bit little-endian mono; ulaw = G.711 μ-law via
   `va_sim.audio.codec.ulaw_decode`); `commit_strategy` `manual` (default) | `vad`; bools `true|false`:
   `include_timestamps` (false), `include_language_detection` (false), `filter_background_audio` (false),
   `enable_logging` (true), `no_verbatim` (false); `language_code` optional, `^[a-z]{2,3}$`; `keyterms` repeated
   (`QueryParams.getlist`), each 1..50 chars, ≤ 100 terms; `entity_detection` repeated, each ∈ `pii|pci|phi|all`;
   `keepalive_interval_ms` int 1000..60000, `null` when absent (recorded, no behaviour);
   `vad_silence_threshold_secs` float 0.3..3.0 default 1.5. Unknown params ignored. Invalid → frame
   `{"message_type": "invalid_request", "error": "<reason>"}` then close 1008 `<reason>` (≤ 123 bytes; truncate
   defensively). Query is validated before auth (same order as TTS).
3. `session_started` (first frame after auth): `{"message_type": "session_started", "session_id": <32 lowercase hex
   from state.ids.uuid() without dashes>, "config": {"model_id", "audio_format", "sample_rate", "commit_strategy",
   "include_timestamps", "include_language_detection", "language_code" (null when absent), "keyterms": [...],
   "entity_detection": [...], "filter_background_audio", "keepalive_interval_ms" (null|int), "enable_logging",
   "no_verbatim", "vad_silence_threshold_secs"}}` (key order as written).
4. Client messages: JSON object with `message_type: "input_audio_chunk"`, `audio_base_64` (str, valid base64),
   `commit` (bool, default false), `sample_rate` (int, optional; must equal the format's rate), `previous_text`
   (str, optional, first chunk only). Unknown fields ignored. Any violation → frame `{"message_type": "input_error",
   "error": "<why>"}`, the message is dropped, socket stays open (specs/08 §4 "input_error: continue"). Cases:
   invalid JSON / non-object; message_type missing or not `input_audio_chunk`; audio_base_64 missing/not str/invalid
   base64; commit non-bool; sample_rate mismatch (`"sample_rate 16000 does not match audio_format ulaw_8000"`); odd
   byte length for pcm; `previous_text` on a non-first chunk (`"previous_text can only be sent with the first audio
   chunk"`); previous_text non-str. "First chunk" = the first valid input_audio_chunk of the session. An empty
   audio string with `commit: true` is valid (commit only). Decoded audio > 1 000 ms in one message → frame
   `{"message_type": "chunk_size_exceeded", "error": "Audio chunk exceeds 1000 ms"}`, message dropped (incl. its
   commit), socket open.
5. Audio analysis (`stt_stream.py`, pure, no asyncio, unit-testable): `SttStream(sample_rate, resolver,
   vad_silence_ms | None)` with `feed(pcm: NDArray[np.int16]) -> list[StreamEvent]` and `commit() -> Commit`.
   - 20 ms frames (`rate*20//1000` samples), remainder carried to the next feed. Voiced frame: RMS ≥ 328
     (−40 dBFS, `round(32767·10^(−40/20))`). Audio clock `audio_ms` advances 20 per frame (session-relative).
   - Segments: a voiced frame after ≥ 300 ms of unvoiced audio (or the first voiced frame) starts a segment; an
     unvoiced run ≥ 300 ms ends it (end = time of its last voiced frame + 20). Gaps < 300 ms stay in the segment.
     A segment ending with < 100 ms voiced audio is discarded (clicks); it never produced partials (see below).
   - Marker: each segment collects samples from 40 ms before its first voiced frame (keep a 2-frame pre-roll) up to
     260 ms after it, then calls `decode_marker(samples, rate)` once (also on segment end / commit when shorter,
     padded with zeros to 260 ms). Resolution via `resolver(marker_id | None) -> Heard(text, language,
     utterance_id | None, has_marker: bool)` provided by the socket layer: registered marker id →
     `state.utterances[id]` text/language; marker decoded but unregistered → `"[unintelligible]"`; no marker → pop
     the head of `store.stt_script` (FIFO texts from `/sim/elevenlabs/stt/transcripts`) else `"[unintelligible]"`.
     Language falls back to the session `language_code` or `"en"`.
   - Expected duration: marker segments `MARKER_MS + utterance_ms(text, 70)` with body start = segment start +
     MARKER_MS; others `len(text)·70` with body start = segment start (70 ms/char = the caller's synthesis default,
     import the constant from synth if public else define `CALLER_MS_PER_CHAR = 70` with a comment).
   - Words = `text.split()`. Revealed count `n = len(words)` once the segment ended, else
     `min(len, ceil(len · voiced_ms / expected_ms))` (0 before the marker decode). Each segment tracks
     `committed` (words already committed); the window text = join of every non-discarded segment's
     `words[committed:n]` with single spaces.
   - Partials: every time the commit window's voiced ms crosses a multiple of 300 (counted per window, reset by a
     commit) and the window text is non-empty, emit `partial_transcript {"message_type": "partial_transcript",
     "text": <window text>}` (repeats allowed).
   - Commit: snapshot window text, word timings, language; mark revealed words committed; drop fully-committed
     ended segments; reset the window voiced counter. Word timing: per char `70 ms` from body start: word i at
     `body_start + char_offset(i)·70` .. `+ len(word)·70`, seconds rounded to 3 decimals, session-relative audio
     time. `words` list alternates `{"text": w, "start", "end", "type": "word", "logprob": 0.0}` and
     `{"text": " ", "start": prev_end, "end": next_start, "type": "spacing", "logprob": 0.0}` (spacing between
     words of the committed text, also across segments).
   - vad strategy: after each frame, if a segment has ended, the window text is non-empty and unvoiced audio since
     the last voiced frame ≥ `vad_silence_threshold_secs`, `feed` returns an auto `Commit` event (processed like a
     manual commit).
   `feed` is CPU work (decode_marker): the socket runs it via `asyncio.to_thread` inside `hold_clock` per message;
   the stream object is touched only by the reader task (sequential). `store.stt_script` is a `collections.deque`;
   popleft from the worker thread and append from control routes are atomic under the GIL (comment it).
6. Commit output (committer worker task per socket, FIFO queue so reading continues): wait `state.latencies
   .stt_final_ms` (sim clock) after the commit message was read, then send `{"message_type": "committed_transcript",
   "text"}`; if `include_timestamps`: `{"message_type": "committed_transcript_with_timestamps", "text",
   "language_code", "words": [...]}`; if `entity_detection` non-empty and the text non-empty:
   `{"message_type": "committed_transcript_entities", "text", "entities": [{"text", "entity_type", "start_char",
   "end_char"}]}` with regex entities: emails (`[\w.+-]+@[\w-]+(\.[\w-]+)+`) → `email_address` when pii|all;
   digit runs `\d(?:[ -]?\d){12,}` (≥ 13 digits) → `credit_card_number` when pci|all; sorted by start_char; empty
   list allowed (still sent). No voiced audio → `committed_transcript` with `""` (and the timestamps variant with
   `words: []` when requested). Entity names from the registry are not modelled (Utterance has no entities).
7. Faults (`PUT /sim/elevenlabs/stt/fault {"kind", "after_ms": int ≥ 0 = 0, "times": int ≥ 1 = 1}` 204; `GET` →
   `{"fault": null|{kind, after_ms, remaining}}`; `DELETE` 204; reset clears; separate from the TTS fault).
   `kind` ∈ the specs/08 §4 error types `error | auth_error | quota_exceeded | commit_throttled | unaccepted_terms |
   rate_limited | queue_overflow | resource_exhausted | session_time_limit_exceeded | input_error |
   invalid_request | chunk_size_exceeded | insufficient_audio_activity | transcriber_error` plus `disconnect`.
   Firing point: when a session's received audio reaches `after_ms` (checked after each chunk's audio is fed), except
   `commit_throttled`, which fires on the first commit at/after `after_ms` and swallows that commit (window kept for
   the next commit). Frame `{"message_type": kind, "error": "Simulated <kind>"}`. Fatal kinds close after the frame:
   1008 for `auth_error | quota_exceeded | unaccepted_terms | invalid_request | rate_limited |
   session_time_limit_exceeded`, 1011 for `error | transcriber_error | resource_exhausted` (reason = the error text);
   non-fatal (`commit_throttled | queue_overflow | input_error | chunk_size_exceeded | insufficient_audio_activity`)
   leave the socket open; `disconnect` closes 1011 `"simulated disconnect"` with no frame. One fault per firing,
   `times` consumed per firing across sessions. specs/30's `SimState.stt_fail_after_s` = kind `transcriber_error`
   with `after_ms`. "Scenario flags" in the plan acceptance = this control route (the e2e harness arms it).
8. Records `store.stt_sessions: list[SttSession]` (`store.py` dataclass `SttSessionRecord`), served at
   `GET /sim/elevenlabs/stt/sessions` → `{"sessions": [{id, model_id, audio_format, commit_strategy, language_code,
   keyterms, entity_detection, include_timestamps, previous_text, started_ms, ended_ms, end_reason
   (null|disconnect|fault|reset|invalid), audio_ms, voiced_ms, partials, commits, transcripts: [{text, utterance_ids:
   [...], language_code, committed_ms}]}]}`. Sessions are recorded only after auth succeeds. Client close or
   disconnect → `disconnect`. Pending (not yet sent) commits are dropped on close.
9. Control `POST /sim/elevenlabs/stt/transcripts {"texts": [non-empty str, ...]}` appends to `store.stt_script`
   (204); `GET` → `{"texts": [...]}`; `DELETE` clears (204). Reset clears.
10. Reset: `ElevenLabsStore.reset()` also closes `stt_sockets` (1012 `"simulator reset"`, open records end
    `reset`), clears `stt_sessions`, `stt_script`, `stt_fault`. Snapshot `elevenlabs` adds `"stt_sessions": n,
    "stt_sockets": open`.
11. No server inactivity timeout and no spontaneous `commit_throttled`/limits (accelerated clock would fire them
    on idle socket waits); errors only via faults.

### Files
- `packages/va_sim/src/va_sim/elevenlabs/stt_params.py` — `AUDIO_FORMATS: Mapping[str, SttFormat(name, rate,
  encoding: Literal["ulaw","pcm"])]`, `SttParams`, `parse_stt_params`, `config_json(params) -> dict`.
- `.../elevenlabs/stt_stream.py` — `SttStream`, `Heard`, `Commit` (text, words, language, utterance_ids),
  `Partial`, `decode_chunk(audio: bytes, fmt) -> NDArray[np.int16]`, constants (FRAME_MS 20, VOICED_RMS 328,
  SEGMENT_GAP_MS 300, MIN_SPEECH_MS 100, PARTIAL_EVERY_MS 300, MARKER_WINDOW_MS 260, CALLER_MS_PER_CHAR 70).
  If > ~250 lines, put word timing + entities in `stt_text.py` (`word_timings`, `find_entities`).
- `.../elevenlabs/stt_ws.py` — `stt_websocket(state, ws)` + `SttSocket` (reader + committer in one TaskGroup, send
  lock, `close_for_reset`, fault firing, message validation helper `parse_chunk(message, first) -> Chunk | str`).
  Reuse `tts_ws._close`/`_receive`-style helpers: move the generic `receive_json`/`close_quietly` into a new
  `elevenlabs/ws_io.py` used by both tts_ws and stt_ws (avoid jscpd clones; keep TTS behaviour identical).
- `.../elevenlabs/store.py` — `SttSessionRecord`, `SttFault`, `SttFaultKind` Literal, `stt_sessions`,
  `stt_sockets`, `stt_script: deque[str]`, `stt_fault`, `new_stt_session(...)`, `arm_stt_fault`,
  `take_stt_fault(at_ms: int, *, on_commit: bool) -> SttFault | None`, reset additions. If the file passes ~250
  lines, move STT records/fault to `stt_store.py` and compose (`ElevenLabsStore.stt = SttStore()`), Builder's choice;
  record it.
- `.../elevenlabs/control.py` — STT routes (sessions, fault, transcripts); split to `stt_control.py` if long.
- `.../elevenlabs/routes.py` — add `WebSocketRoute("/v1/speech-to-text/realtime", ...)` and STT control routes.
- `.../elevenlabs/auth.py` — public `MISSING_KEY`.
- `packages/va_sim/src/va_sim/state.py` — snapshot keys, reset docstring.
- `specs/30` §3.3 Decision, `specs/08` §10 Decision, `AGENTS.md` (FakeElevenLabs STT test notes), Ralph memory.

### Tests (`packages/va_sim/tests/elevenlabs/`; helpers in new `tests/support/stt_helpers.py` importing `served`,
`realtime_state`, `send`, `recv_json`, `wait_closed`, `API_KEY` from tts_helpers — no copies: `stt_socket(base,
query, key)`, `chunked(audio, fmt, ms=100)`, `send_audio(ws, audio, *, commit=False, previous_text=None)`,
`recv_type(ws, message_type)` (skips partials unless asked), `caller_audio(state, text, sample_rate)` =
register_utterance + synthesize_wire (+ L16 BE→LE conversion for pcm_16000), `silence(fmt, ms)`)
- `test_stt_stream.py` (pure, no sockets): ulaw and pcm_16000 utterance audio → after trailing 400 ms silence a
  commit returns the registered text; partial cadence (count = floor(voiced/300) with non-empty text, increasing
  word counts, first partial ≤ 2 words); unknown registered-id → `[unintelligible]`; no marker (plain 400 Hz tone)
  with script queue → scripted text, empty queue → `[unintelligible]`; segment < 100 ms discarded; two utterances in
  one window joined; commit mid-utterance returns a prefix and the next commit the rest (union == text); empty
  commit → ""; word timings monotone, first word start = body start (segment start + 0.18 s), spacing entries;
  feed with sizes not multiple of 20 ms equals single feed; vad auto-commit after threshold silence;
  entities (email, 16-digit card with spaces, category filtering); decode_chunk per format (pcm LE, ulaw).
- `test_stt_params.py`: defaults, each invalid param reason text, keyterms/entity_detection repeated, config_json
  echo key order.
- `test_stt_ws.py` (real sockets, RealtimeClock, `realtime_state()` stt_final_ms 10): session_started echo
  (incl. keyterms list); auth missing/invalid → auth_error frame + 1008; invalid query → invalid_request + 1008;
  μ-law 8 kHz end-to-end: audio in 100 ms chunks, partials arrive, commit → committed_transcript text ==
  registered text, with timestamps words reconstruct text; pcm_16000 same; commit latency (stt_final_ms 200 →
  committed ≥ 0.2 s after commit send, wall clock); include_timestamps false → no timestamps frame; entity frame
  when entity_detection=pii&entity_detection=pci; input_error cases (invalid JSON, wrong type, bad base64,
  sample_rate mismatch, odd pcm bytes, previous_text on 2nd chunk) keep the socket usable; chunk_size_exceeded;
  previous_text recorded; script queue via control route used for markerless audio; faults: parametrized over all
  14 kinds + disconnect asserting frame/close code (fatal vs non-fatal), commit_throttled swallows one commit then
  next commit carries the text, after_ms respected, times consumed; `/sim/reset` with an open socket → 1012 and
  record end_reason reset and `state.tasks.pending == 0`; session record fields after close.
- `test_control_elevenlabs.py`: stt sessions/fault/transcripts routes (validation 422 shared control format,
  GET/DELETE), reset clears them; STT routes absent on the telnyx port (404); `test_state.py` snapshot keys.

### Gates
`make fmt lint typecheck placeholders test`; `make check` (va_sim coverage ≥ 85 %, jscpd 0, spec refs);
`uvx radon cc -s -a packages/va_sim/src` (new functions ≤ B); `uv export --no-emit-workspace --frozen | uvx
pip-audit --strict`; `corepack pnpm audit --audit-level high`. No e2e. build.done payload keys: `tests, lint,
typecheck, audit, coverage, complexity, duplication, specs`.

### Risks
- RealtimeClock for socket tests (accelerated clock races on socket waits, see memories); only pure stream tests
  are clock-free.
- Marker decode needs the pre-roll: the first voiced frame can be the sync tone itself; keep 40 ms pre-roll and
  ≥ 260 ms after so decode_marker's 12 ms sync search fits. μ-law decode of synthesized audio must recover ids
  (P03-T03a guarantees it at 8 kHz and 16 kHz; other rates untested — fine).
- The −12 dB body floor keeps utterances one segment; the caller's DTMF in-band runs become `[unintelligible]`
  segments (documented, acceptable).
- tts_ws refactor into ws_io must not change TTS behaviour (its 97 tests must stay green).
- Close reasons ≤ 123 bytes.

## Build P03-T05a (Builder, 2026-10-29)

Implemented per plan with these recorded deviations (all in specs/30 §3.3 Decision P03-T05a):
- STT state lives in a composed `stt_store.SttStore` (`ElevenLabsStore.stt`) — store.py would pass 250 lines.
- Expected duration for marker segments = `utterance_ms(text, 70)` (it already includes the 180 ms preamble; the
  plan's `MARKER_MS + utterance_ms` double-counted).
- A segment resolves (decode + resolver/script pop) only once it has >= 100 ms voiced audio, so clicks never
  consume a script text. A commit does not force-resolve an open segment whose 260 ms marker window is incomplete
  (a partial preamble misdecodes and would pop the script); its words join a later commit.
- `end_reason` has no `invalid` value (invalid queries fail before auth, so they are never recorded).
- `audio_ms` counts whole 20 ms frames analysed (a partial frame waits for the next message).
- `ws_io.py` holds receive_json/close_quietly/close_reason + close codes; tts_ws uses it (TTS tests unchanged
  except snapshot assertions that now see the stt keys).
Files: elevenlabs/{ws_io,stt_params,stt_stream,stt_text,stt_store,stt_ws,stt_control}.py, routes/store/auth/tts_ws,
state.py; tests elevenlabs/test_stt_{stream,params,ws}.py, test_control_elevenlabs.py, support/stt_helpers.py.
Gate evidence: make fmt/lint/typecheck/placeholders exit 0 (mypy 525 files, 0 placeholder findings);
va_sim + apps/sim pytest 771 passed, va_sim coverage 98% (stt_* 97-100%); full `make test` (pre-dedupe run)
4182 passed, 5 failed + 11 errors all apps/api DB integration tests on pytest-timeout >60 s under load (open task
fix:api-test-timeout-load) — those 77 tests pass alone in 25 s; make test-ts exit 0; openapi drift current;
check_spec_refs 0 problems; jscpd packages/va_sim apps/sim 0 clones (STT/TTS send moved to ws_io.send_json, STT
fault route uses one method-dispatch handler); radon: no new function above B; pip-audit + pnpm audit clean.

## Plan P03-T05a (re-plan after build.blocked: missing backpressure evidence, Planner, 2026-10-29)

The blocker is about format, not substance. The build is complete (see Build P03-T05a above). The build.done payload left out
the required backpressure keys. No split or prerequisite is needed. Builder: do not change the design. Re-run the gates on
the current working tree and fix only what fails:
1. `make fmt lint typecheck placeholders` -> lint/typecheck.
2. `uv run pytest packages/va_sim apps/sim --cov=va_sim` -> tests + coverage (>= 85 %). Then run full `make test`. Known
   unrelated flake: apps/api DB integration tests hitting the 60 s pytest-timeout under host load (open task
   fix:api-test-timeout-load). If those fail, re-run just those tests (`uv run pytest apps/api -x`) and report both results.
3. `make check` (spec refs, jscpd) -> duplication/specs; `uvx radon cc -s -a packages/va_sim/src` -> complexity (average
   grade + worst new function).
4. `uv export --no-emit-workspace --frozen | uvx pip-audit --strict` and `corepack pnpm audit --audit-level high` -> audit.
Emit build.done with exactly these keys in the payload, using the real results:
`tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass (<pct>), complexity: <avg grade/score>,
duplication: pass, specs: pass`. If any gate genuinely fails after a fix, emit build.blocked naming the gate.

## Build P03-T05a gate re-run for evidence (Builder, 2026-10-29)

No code changes; I re-ran the gates on the current working tree:
- make fmt / lint / typecheck / placeholders: all exit 0 (0 placeholder findings).
- `uv run pytest packages/va_sim apps/sim --cov=va_sim`: 771 passed in 31.8 s, va_sim coverage 98 % (stt_* 97-100 %, ws_io 87 %).
- Full `make test`: 4185 passed, 13 errors. Every error is a "Timeout (>60.0s) from pytest-timeout" in apps/api DB
  fixture setup (test_ip_allowlist_integration 11, test_dev_seed 2), which is the open task fix:api-test-timeout-load.
  Re-running just those two modules gave 26 passed in 9.9 s.
- make test-ts exit 0; check_openapi_drift current; check_spec_refs 0 problems; jscpd packages/va_sim apps/sim 0 clones.
- radon cc va_sim average A (2.31); no new stt_*/ws_io function graded C or worse.
- pip-audit --strict and pnpm audit --audit-level high: both report no known vulnerabilities.

## Review P03-T05a (Reviewer, 2026-10-29)

Approved. Gates re-run: make lint/typecheck/placeholders exit 0 (0 findings); `pytest packages/va_sim apps/sim --cov=va_sim`
771 passed, 98 % coverage; full `make test` 4174 passed with 24 errors, all "Timeout (>60.0s)" in
apps/api/tests/test_org_integration.py fixture setup (open task fix:api-test-timeout-load, unrelated to va_sim);
that module re-run alone: 27 passed in 9.8 s. check_spec_refs 0 problems; jscpd packages/va_sim apps/sim 0 clones.
Diff review: query validation, session_started echo, chunk validation (input_error / chunk_size_exceeded, previous_text
first-chunk rule), segmentation/marker/partial/vad logic, commit latency on the sim clock, timestamps/entities frames,
fault kinds with 1008/1011/non-fatal split, commit_throttled swallowing, reset 1012, and records all match the Decision
P03-T05a rules. The vad minimum of 0.3 s is >= the 300 ms segment gap, so an auto-commit always sees the segment closed.
The ws_io refactor keeps TTS behaviour, except that close reasons are now truncated to 123 bytes (safe). Tests contain no sleeps.

## Plan P03-T05b — FakeElevenLabs batch STT (Planner, 2026-10-29)

Split: the old P03-T05b (batch STT + voices + subscription) is now P03-T05b (batch STT) and P03-T05c (voice registry,
voices REST, TTS `voice_not_found`, subscription). plan/phase-03.md is updated, and T11/T12 dependencies are adjusted. The
contract comes from ElevenLabs docs fetched today (speech-to-text/convert): the multipart fields and the single-channel
`{language_code, language_probability, text, words[{text, start, end, type, speaker_id, logprob, characters?,
channel_index?}], transcription_id, audio_duration_secs, entities?}`. Multichannel `separate` (the default) returns
`{transcripts: [{language_code, language_probability, text, words, channel_index, entities?}], transcription_id,
audio_duration_secs}`. `combined` returns the single-channel shape with words sorted by start, each carrying
`channel_index`; it requires timestamps and does not support entity detection.

### Decisions (Builder writes ONE `> Decision (2026-10-29): FakeElevenLabs batch STT (P03-T05b).` block at the end of specs/30 §3.3, plus a one-line pointer Decision at the end of specs/08 §8)
1. `POST /v1/speech-to-text`, multipart/form-data, header `xi-api-key` (401 `needs_authorization` /
   `invalid_api_key` via `auth.rest_auth_error`). Form fields: `model_id` (required, non-empty); exactly one of
   `file` (upload), `cloud_storage_url`, `source_url` (http/https URL); `language_code` `^[a-z]{2,3}$`;
   `timestamps_granularity` `none|word|character` (default `word`); `diarize`, `use_multi_channel`,
   `tag_audio_events` booleans (`true|false` case-insensitive; defaults false, false, true); `num_speakers` 1..32;
   `multichannel_output_style` `separate|combined` (default `separate`); `keyterms` repeated (≤ 1000, each 1..49 chars,
   ≤ 5 words, none of `<>{}[]\`); `entity_detection` repeated (`all|pii|phi|pci|other|offensive_language`);
   `file_format` `pcm_s16le_16|other` (default `other`). Query `enable_logging` `true|false` (default true). Other
   fields are ignored. Validation failures → 422 `{"detail": [{"loc": ["body", <field>], "msg", "type"}]}`
   (query: `["query", "enable_logging"]`), with the same helper as REST TTS. `combined` with
   `timestamps_granularity=none` or with `entity_detection` → 422. A body that is not multipart → 422 with loc
   `["body"]`.
2. Audio: `file_format=pcm_s16le_16` → raw signed 16-bit LE mono 16 kHz. Otherwise the bytes must be a 16-bit PCM
   WAV (1..5 channels when `use_multi_channel`; any count otherwise; any rate ≥ 8000), else 400
   `{"detail": {"status": "invalid_audio", "message": "Only 16-bit PCM WAV or pcm_s16le_16 audio is supported by the
   simulator"}}`. Duration < 100 ms → 400 `audio_too_short`. More than 5 channels with `use_multi_channel` → 400
   `too_many_channels`. Upload cap 100 MB → 400 `file_too_large`. Without `use_multi_channel`, multi-channel audio
   is down-mixed to one channel (sample-wise clipped sum, the same as FakeTelnyx `single`). Overlapping speakers then
   do not reconstruct; use `use_multi_channel`.
3. URL sources: when the URL path is `/sim/recordings/<id>.wav` and `<id>` is in `state.recordings`, the stored WAV is
   used directly, whatever the host. Otherwise the sim fetches it with `httpx.AsyncClient(timeout=10.0,
   follow_redirects=False)`, streaming with the 100 MB cap. Non-200 status, transport error, or timeout → 400
   `{"detail": {"status": "invalid_source_url", "message": "Could not fetch audio from <url> (<why>)"}}`.
4. Segmentation per channel (pure numpy, new module): 20 ms frames, voiced when RMS ≥ `VOICED_RMS`. Segments are
   separated by ≥ `SEGMENT_GAP_MS` (300) unvoiced. A segment with < `MIN_SPEECH_MS` voiced audio is dropped. A
   segment spans from its first voiced frame start to its last voiced frame end. Import the constants from
   `stt_stream`; do not copy them.
5. Agent tones first. Starting at the segment start, find the onset: the first sample with |x| ≥ 64, minus 1,
   clamped. Then read consecutive 60 ms slots (`tts_audio.CHAR_MS`) up to the segment end. A slot whose RMS is below
   `VOICED_RMS` is a space; after a space, the next onset is searched again from the slot end, so gaps that are not
   a multiple of 60 ms resync. A voiced slot takes the tone class k ∈ 0..11 (frequency `300 + 25k`, from a new public
   `tts_audio.tone_class_frequencies()` or equivalent, so the formula is not duplicated) with the largest
   `goertzel_power`. The slot counts as a tone only if purity = best power / slot energy ≥ 0.6. Any impure voiced
   slot means the segment is not agent speech. A trailing partial slot (< 60 ms) is ignored.
   Agent text resolution: decoded string = classes as `chr(ord("a") + k)`, spaces collapsed to one, stripped.
   Candidates = every `TtsRequest.generated_text` in `state.elevenlabs.tts_requests` order, mapped the same way
   (whitespace runs → one space; other chars → class of `ord(c) % 12`) with an index map back to the original. The
   first candidate (record order, then earliest offset) containing the decoded string as a substring wins. Its
   original substring gives the words, and the decoded slot start times give the times (non-space chars align one
   to one): word start = first char slot start, end = last char slot end. Tone segments with no match → one word
   `"[unintelligible]"` (`stt_stream.UNINTELLIGIBLE`) spanning the segment. Snapshot the records list before the
   worker thread starts (`tuple(r.generated_text for r in ...)`) so the thread never touches live state.
6. Otherwise, the caller path. `decode_marker` runs on the 40 ms pre-roll + 260 ms from the segment start
   (zero-padded) at the channel rate: a registered id (`state.utterances`, snapshot as `{id: (text, language)}`
   before the thread) → `timed_words(text, segment_start + MARKER_MS)`, language = the utterance language. An
   unregistered id or no marker → one `"[unintelligible]"` word spanning the segment. Batch never pops the
   realtime transcript script. DTMF and other non-tone, non-marker audio become `[unintelligible]` (documented).
7. Words: entries `{"text", "start", "end", "type": "word", "speaker_id", "logprob": 0.0}`, with seconds rounded
   to 3 decimals, relative to the file start. A `{"text": " ", "type": "spacing", "start": prev.end, "end":
   next.start, ...}` entry goes between consecutive words of one channel (start=end when they touch). With
   `character` granularity, each word gets `"characters": [{"text", "start", "end"}]` (agent: per slot; caller:
   70 ms per char from the word start, `stt_text.CALLER_MS_PER_CHAR`). `none` → `words: []` (text still filled).
   `speaker_id`: multichannel → `speaker_<channel_index>`; mono with `diarize` → caller-path segments
   `speaker_0`, agent-path `speaker_1`; mono without diarize → `speaker_0`. Words carry `channel_index` only in
   multichannel responses. `text` = word texts in order joined by single spaces. `language_code` = the first
   caller utterance language heard on that transcript, else the request `language_code`, else `"en"`;
   `language_probability` 1.0.
8. Response: `use_multi_channel` false → the single-channel shape. True + `separate` → `transcripts` (one per
   channel, a mono file gives one) with `channel_index`. True + `combined` → the single-channel shape with all
   channels' entries merged by `(start, channel_index, original order)`, `text` from the merged words, and
   `language_code` from the first transcript that heard a caller utterance. `transcription_id` = 32 hex
   (`state.ids.token(16)`); `audio_duration_secs` = file duration rounded to 3. `entities` (per transcript /
   top level) only when `entity_detection` was sent, via `stt_text.find_entities(text, categories)` (`phi`,
   `other`, `offensive_language` find nothing).
9. Records: `store.stt.batch: list[SttBatchRecord]`, served at `GET /sim/elevenlabs/stt/batch` →
   `{"requests": [{transcription_id, model_id, source ("file"|"url"), source_url, file_format, channels,
   use_multi_channel, multichannel_output_style, diarize, num_speakers, timestamps_granularity, language_code,
   keyterms, entity_detection, enable_logging, audio_duration_secs, received_ms}]}`. Records are written after a
   successful analysis; failed requests are not recorded.
10. Fault: `PUT /sim/elevenlabs/stt/batch/fault {"kind": "error"|"rate_limited"|"delay", "delay_ms" = 0,
    "times" = 1}` (204), `GET` → `{"fault": null|{kind, delay_ms, remaining}}`, `DELETE` 204. Checked after
    auth and before validation. `error` → 500 `{"detail": {"status": "internal_error", "message": "Simulated batch
    STT failure"}}`; `rate_limited` → 429 `{"detail": {"status": "too_many_concurrent_requests", ...}}`; `delay` →
    waits `delay_ms` on the sim clock, then serves normally. Each firing consumes one of `times`. Reset clears the
    batch records and the fault; the snapshot `elevenlabs` adds `"stt_batch": n`.
11. Analysis (WAV decode, segmentation, tone and marker decoding) runs in `asyncio.to_thread` inside
    `va_sim.tasks.hold_clock`, as in TTS REST synthesis.

### Files
- `packages/va_sim/pyproject.toml`: add `python-multipart` (Starlette `request.form()` needs it; `uv add
  --package va-sim python-multipart`, then `uv lock`). Use `request.form(max_files=1, max_fields=2000,
  max_part_size=100 MB)`. Check Starlette 1.x `form()` kwargs; `max_part_size` applies to non-file fields, so
  enforce the file cap on the `UploadFile` size.
- `packages/va_sim/src/va_sim/elevenlabs/stt_batch_params.py`: `BatchParams` (frozen dataclass or pydantic model)
  + `parse_batch_form(form, query) -> BatchParams | list[dict]` (422 detail items).
- `.../elevenlabs/stt_batch_audio.py`: pure. `read_batch_audio(data: bytes, file_format) -> BatchAudio(channels:
  tuple[NDArray[int16], ...], rate, duration_ms)` raising `BatchAudioError(status, message)`; `voiced_segments`;
  `decode_tones(samples, rate, start, end) -> list[TonedChar] | None`; `match_agent_text(chars, candidates) ->
  ...`; `transcribe_channel(samples, rate, *, utterances, candidates, language) -> list[BatchSegment]` (segment =
  words with times, path "caller"|"agent", language). If > ~250 lines, put tone decode + matching in
  `stt_batch_tones.py`.
- `.../elevenlabs/stt_batch.py`: route handler `batch_routes(state)`: auth, fault, form parse, source load (URL
  fetch), thread analysis, response JSON building (single/separate/combined, speaker ids, spacing, characters,
  entities), record.
- `.../elevenlabs/rest_io.py` (new, shared): move `_unprocessable` / `_validation_errors` out of `tts_rest.py`
  (public names), with both modules using them (jscpd).
- `.../elevenlabs/tts_audio.py`: export the tone class frequency helper (keep `char_frequency` behaviour).
- `.../elevenlabs/stt_store.py`: `SttBatchRecord`, `BatchFault`, `batch`, `batch_fault`, arm/take, reset.
- `.../elevenlabs/stt_control.py`: `/sim/elevenlabs/stt/batch` GET and `/sim/elevenlabs/stt/batch/fault`
  PUT/GET/DELETE (reuse the method-dispatch pattern of the realtime fault route; avoid clones).
- `.../elevenlabs/routes.py`: add the batch route; `state.py`: snapshot key.
- specs/30 §3.3 Decision, specs/08 §8 pointer Decision, AGENTS.md (batch STT test notes), Ralph memory.

### Tests (`packages/va_sim/tests/elevenlabs/`; helpers in `tests/support/batch_helpers.py`, reusing
stt_helpers/tts_helpers, no copies: `agent_track(text, offset_ms)` = μ-law round trip of
`tts_audio.pcm_samples(text, 8000)`, `caller_track(state, text)`, `place(tracks, length_ms)`,
`multipart(...)`, `post_batch(client, files/data)`)
- `test_stt_batch_audio.py` (pure): WAV mono/stereo/5-channel decode; pcm_s16le_16; invalid WAV (8-bit, garbage)
  → invalid_audio; < 100 ms → audio_too_short. `voiced_segments` boundaries (gap 280 ms joins, 320 ms splits,
  < 100 ms dropped). `decode_tones` recovers class strings for text with single and double spaces at 8 kHz after
  μ-law round trip and at 16 kHz; resync after a 90 ms gap between two generations; formant caller speech
  (synthesize_utterance) → None. Matching: substring in the second candidate; mid-text start; no match →
  `[unintelligible]`. Caller marker segment → registered text with body start + 180 ms; unregistered id and plain
  noise → `[unintelligible]`.
- `test_stt_batch.py` (ASGI httpx client over `Starlette(routes=simulator_routes("elevenlabs", ...))` like
  test_control_elevenlabs, accelerated or realtime clock as tts REST tests do):
  * Acceptance: a FakeTelnyx call with `record` (dual), `call.audio.caller` filled with two decoded caller
    utterances and `call.audio.agent` with μ-law-round-tripped TTS tones of two texts first generated through
    `POST /v1/text-to-speech/{voice}` (so `tts_requests` has them), interleaved in time; hang up; POST the
    recording WAV (file upload) with `use_multi_channel=true`, `multichannel_output_style=combined`,
    `timestamps_granularity=word`. Assert that the words in start order rebuild caller text, agent text, caller, agent;
    channel_index 0 → caller, 1 → agent; speaker ids; start times within 70 ms of the placement offsets; sorted.
  * `separate` shape (2 transcripts, channel_index, per-channel text); single-channel shape (no use_multi_channel,
    mono file); mono + diarize speaker split; `character` granularity; `none` → words [];
    entity_detection pii+pci entities on a caller utterance "my email is jo@acme.io" (separate/single only).
  * `cloud_storage_url` pointing at `http://anything/sim/recordings/<id>.wav` (in-process shortcut);
    `source_url` fetched over a real socket (`running_sims`, the telnyx port `GET` of a served WAV such as
    `/sim/recordings/<id>.wav` on another host spelling would hit the shortcut, so use the media control
    `agent_audio.wav` route or a tiny Starlette app served by uvicorn in the test); unreachable URL / 404 → 400
    `invalid_source_url`.
  * Validation 422 matrix: missing model_id, both/neither file and URL, bad granularity, bad bool, num_speakers 0,
    keyterm too long / 6 words / `<`, bad entity category, combined+none, combined+entity_detection, bad
    enable_logging; 401 missing/invalid key; 400 invalid_audio / too_many_channels / audio_too_short.
  * Faults: error 500, rate_limited 429, delay (sim clock: response after delay_ms), times consumed.
  * Records at `/sim/elevenlabs/stt/batch` (keyterms, enable_logging false, source); reset clears records and
    fault; snapshot key.
- `test_control_elevenlabs.py`: batch fault route validation (422 shared control format), GET/DELETE.

### Gates
`make fmt lint typecheck placeholders test`; `make check` (va_sim coverage ≥ 85 %, jscpd 0, spec refs);
`uvx radon cc -s -a packages/va_sim/src` (new functions ≤ B); `uv export --no-emit-workspace --frozen | uvx
pip-audit --strict` (the new python-multipart dependency must be clean); `corepack pnpm audit --audit-level high`.
No e2e. build.done payload keys: `tests, lint, typecheck, audit, coverage, complexity, duplication, specs`.
Known unrelated flake: apps/api DB tests time out under load (task fix:api-test-timeout-load). Re-run those modules
alone and report both results.

### Risks
- Tone purity on μ-law: the quantisation noise of a −10 dBFS tone is far below 40 % of the energy, but verify that
  the 0.6 threshold works at 8 kHz μ-law and at 16 kHz. The slot grid must align (onset −1 sample); a misaligned
  slot that straddles two chars lowers purity. If tests show straddling, refine the onset (search the sign change
  before the threshold crossing) rather than lowering the threshold.
- Short agent strings (≤ 3 chars) can match the wrong candidate (12 classes). First match wins, which is
  deterministic and documented.
- Starlette `request.form()` limits: the default `max_part_size` (1 MB) applies to non-file fields only. Uploads
  spool to disk; read them with `await upload.read()`, which is fine at the 100 MB cap.
- Do not mutate `state.elevenlabs`/`state.utterances` from the worker thread: snapshot first.
- Existing TTS REST 422 tests must stay green after the `rest_io` move.

## Build P03-T05b (Builder, 2026-10-29)

Implemented per plan. New: `elevenlabs/stt_batch.py` (route), `stt_batch_params.py` (pydantic BatchForm over the
multipart form), `stt_batch_audio.py` (pure: WAV/raw PCM, voiced_segments, caller marker path, transcribe),
`stt_batch_tones.py` (pure: tone decode + match vs TTS generated_text), `stt_batch_json.py` (single/separate/
combined bodies), `rest_io.py` (shared 422 helpers, tts_rest now uses them). `tts_audio.tone_class` +
`tone_class_frequencies`; `SttStore.batch` / `batch_fault`; stt_control `_fault_route` helper shared by the
realtime and batch faults; snapshot `stt_batch`. Dependency `python-multipart` added to va-sim.
Deviations / additions (recorded in the specs/30 Decision):
- Back-to-back generations in one segment: when the whole decoded string matches nothing, the longest word-aligned
  prefix is matched and the rest matched again; unmatched runs become `[unintelligible]`.
- `transcription_id` = `ids.uuid()` without dashes (32 hex; ids.token is base64).
- A truncated multipart body parses as an empty form (python-multipart), so it yields the model_id 422.
- specs/08 §8 conflict: the platform spec sends `combined` + `entity_detection`; ElevenLabs rejects that, and so does
  the sim. The pointer Decision tells the client to request `separate` and merge when entities are needed.
Gates: fmt/lint/typecheck/placeholders pass; va_sim 825 passed; `make check` test step: 4192 passed, 2 failed + 68
errors, all apps/api pytest-timeout/container timeouts under -n auto (known fix:api-test-timeout-load); those 7
modules re-run alone: 122 passed. Coverage total 97.73% (new modules 94-100%). test-ts pass; spec refs 0
problems; openapi drift current; jscpd 0 clones; radon: new functions ≤ B (avg A 2.40); pip-audit no known
vulnerabilities; pnpm audit clean.

## Plan P03-T05b (re-plan after build.blocked: missing backpressure evidence, Planner, 2026-10-29)

The implementation is complete in the working tree (see Build P03-T05b above). The block came from the build.done
payload format, not from a code defect. Do not split the task and do not change the design.
Builder: do not redo the implementation. Re-run the gates in this iteration and emit build.done with exactly these
keys, using real results:
- `make fmt lint typecheck placeholders` → `lint: pass`, `typecheck: pass`
- `make check` → `tests: pass` (apps/api pytest-timeout errors under -n auto are the known
  fix:api-test-timeout-load flake; re-run the affected modules alone and report both numbers in the scratchpad),
  `coverage: pass` (va_sim ≥ 85 %), `duplication: pass` (jscpd 0), `specs: pass` (spec refs 0 problems)
- `uvx radon cc -s -a packages/va_sim/src` → `complexity: <avg grade/score>` (new functions ≤ B)
- `uv export --no-emit-workspace --frozen | uvx pip-audit --strict` and `corepack pnpm audit --audit-level high`
  → `audit: pass`
Example: `ralph emit "build.done" "tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass,
complexity: A (2.40), duplication: pass, specs: pass"`. If a gate really fails, fix it before emitting.

## Build P03-T05b gate re-run for evidence (Builder, 2026-10-29)

No code changes. Gates re-run this iteration: `make fmt lint typecheck placeholders` exit 0 (mypy 534 files clean,
0 placeholder findings). `make check` exit 0: 4262 passed (no api timeouts this run), coverage 98.00 %, test-ts
pass (22/52/48), openapi drift current, spec refs 0 problems. jscpd 0 clones (770 files). radon avg A (2.40), no
C+ functions in va_sim.elevenlabs. pip-audit --strict: no known vulnerabilities; pnpm audit: clean.
