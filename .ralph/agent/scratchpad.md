# Scratchpad — Phase 05 (restarted loop, 2026-11-02)

The previous loop stopped on max runtime with P05-T10b implemented but uncommitted (no build.done/review). The old
scratchpad (Plans/Builds/Reviews through P05-T10a) is in git history (`git show 627a5be:.ralph/agent/scratchpad.md`).

## Plan P05-T10b — Greeting gate, outbound DTMF, IVR mode (Planner, 2026-11-02)

State: the working tree already holds a full implementation (see `git status`). The Builder's job is to verify it
against the acceptance criteria below, close any gap, run every gate, and emit build.done. Do NOT rewrite it.

Existing pieces (verify, keep):
- `packages/va_voice/src/va_voice/pipeline/pipeline.py` — `CallPipeline(before_opener=)`, greeting wait in the reply
  loop (`PipelineConfig.wait_for_greeting`, `greeting_timeout_ms` 1 000–30 000), `Brain.start(greeting)`, no-input
  timer never armed before the opener, `ivr` task when `ivr_mode`.
- `packages/va_voice/src/va_voice/pipeline/ivr.py` — `IvrProgress` (normalized-text novelty, actions = progress,
  `ivr_dead_end_s` 120 s); end reason `ivr_dead_end`, ended_by `system`.
- `protocols.py` `Brain.start(greeting: UserTurn | None = None)`; `va_engine.task_runner.TaskBrain.start` appends the
  greeting before `first_sentence` / answers it.
- Media: `va_media.carrier_watch.CarrierWatch` (answered from snapshot or primary-leg `call.answered`),
  `va_media.greeting.GreetingGate` (outbound 2 s answered wait, precall groups), `va_media.dtmf_out` (split on
  `w`=0.5 s/`W`=1 s, `send_dtmf` per group with `command_id`), `va_media.actions.MediaActions` (press_button).
- `packages/va_telnyx/src/va_telnyx/settings.py` — `TelnyxApiSettings`, `telnyx_http`, `telnyx_factory`,
  `SIM_TELNYX_API_KEY`; mixed into worker + media settings (va_telnyx pyproject gained a dep; uv.lock updated).
- Spec decisions already written: specs/07 §11 Decision P05-T10b, specs/06 §9 Decision, specs/02 env table row for
  `TELNYX_API_BASE` (media added). AGENTS.md line added. plan/phase-05.md already split T10d out.

Acceptance checklist (each must have a passing test; all exist today — confirm):
1. Pipeline harness: gate order (hook before opener) — test_pipeline_greeting.py.
2. wait_for_greeting with speech / DTMF / timeout; no reprompt while waiting — test_pipeline_greeting.py.
3. IVR dead end + progress resets (new text, button press), silence after opener, none without ivr_mode —
   test_pipeline_ivr.py.
4. ManualClock answered wait (snapshot, channel event, timeout) and precall groups — test_outbound_dtmf.py.
5. FakeTelnyx+FakeElevenLabs+FakeLLM runtime: precall reaches send_dtmf before the agent speaks; press_button tool
   call reaches send_dtmf; wait_for_greeting holds the opener — test_runtime_sim.py (3 new tests).
Planner check: these 305 targeted tests pass (`uv run pytest --no-cov` on the touched test files, 78 s).

Builder steps:
1. Review the diff for placeholders / spec mismatches (specs/07 §11 steps 1,3,4,6; specs/06 §9; specs/05 §9
   `ivr_dead_end` row exists and ended_by=system). Check `.env.example` ↔ specs/02 §6 still mirror
   (`scripts/tests/test_env_example.py`) after the TELNYX_API_BASE row edit.
2. Check per-package coverage ≥ 85 % (new modules should be ~100 %); add tests for any uncovered branch in
   greeting.py / actions.py / dtmf_out.py / ivr.py / va_telnyx settings.py.
3. Gates: `make fmt`, `make lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `make test`,
   `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a` on touched src (no C+), audit
   (`uvx pip-audit` on `uv export` + `corepack pnpm audit`). make e2e harness does not exist yet (P05-T17) — say so.
4. Do not commit the scratchpad deletion problem away: this file is the new scratchpad (tracked).

Risks: host load makes api/va_db integration tests hit the 60 s timeout (known open fix tasks in summary); rerun the
failing module alone before concluding a regression. Accelerated-clock runtime sim tests: never busy-poll.

## Review P05-T10b (Reviewer, 2026-11-02) — APPROVED

Re-ran: make lint, make typecheck (mypy 1133 files), make placeholders (0), check_spec_refs (0), make test
(9616 passed, 7 skipped, 99.16 %; new modules 100 %), jscpd 0 clones, radon no C+. Diff checked against specs/07
§11 steps 1/3/4/6 + Decision, specs/06 §9 Decision, specs/05 §9 `ivr_dead_end` (system), specs/02 §6
TELNYX_API_BASE row. Note (not blocking, pre-existing from P03-T08): `TelnyxClient.send_dtmf` sends no
`client_state`; command_id idempotency is in place. make e2e does not exist until P05-T17.

## Plan P05-T10d — Opener pre-render and cached playback (Planner, 2026-11-02)

Spec refs: specs/07 §11 step 5 (+ the P05-T10b Decision's last sentence), specs/09 §9 (+ Decision P04-T11),
specs/01 §2.1 step 2 (+ Decision P05-T06a: "first-sentence pre-render (P05-T10) stays empty" → update it).
Write one `> Decision (2026-11-02): opener pre-render (P05-T10d)` under specs/07 §11 (and a one-line pointer in
specs/09 §9 and specs/01 §2.1 step 2 Decision) covering every rule below.

### Design (decided — do not re-litigate)
1. **Rendered text lives in the context.** New optional field `CallContext.opener_text: str | None = None`
   (`packages/va_domain/src/va_domain/call_context.py`, version stays 1, old JSON still validates). It is the
   templated, stripped `first_sentence` rendered once at prefetch; the media Brain speaks it verbatim, so the
   cache key always matches (time builtins cannot drift between prefetch and call). A context rebuilt by the
   media worker (`build_call_context`) has `None` → TaskBrain renders the template as today.
2. **Shared pure helpers** `packages/va_engine/src/va_engine/opener.py` (va_engine already depends on va_voice +
   va_domain):
   - `render_opener(context: CallContext, *, now: datetime) -> str | None` — `None` when runner_type != "task",
     `first_sentence` missing/blank, or the render is blank; else `render(parse_template(first_sentence), scope)`
     stripped, scope = `TemplateScope(variables=context.variables, request_data=config.request_data,
     builtins=call_builtins(context, now=now, last_user_message=""), timezone=config.timezone,
     missing=MissingVariables(sink=<log "template_missing_variable" with name only>))`; a `TemplateSyntaxError`
     falls back to the raw text (same as `task_runner._template`). Move/reuse `_template` so TaskBrain and this
     share one implementation (no jscpd clone).
   - `phrase_voice(context, *, model_id: str) -> PhraseVoice` — `provider_voice_id`, model, `voice_settings`,
     `lookup_language(config.language).tts_code` (exactly what `va_media.runtime_config.tts_voice` sends; add a
     test asserting the two agree field by field for a fixed and a detected language).
   - `phrase_format(context) -> AudioFormat` — `PCM16LE_16000` when `config.hd_audio`, else `ULAW_8000`.
   - `cache_text(context, text) -> str` — `PronunciationApplier(config.pronunciation_guide).apply(text).text`
     (the Speaker keys lookups on the pronounced text; see 5).
   - TaskBrain.start: `text = context.opener_text or self._render(self._opener)` (opener_text used only when
     non-blank; greeting is still appended first).
3. **Read-only cache access** in `packages/va_voice/src/va_voice/phrase_cache.py`: new module function
   `async def read_phrase(redis, voice, text, fmt) -> CachedPhrase | None` (validates fmt, builds the key, GET,
   decode; Redis error → `tts.phrase_cache_unavailable` operation `get` → None; malformed → logged → None).
   `PhraseCache._read` delegates to it (no duplicated logic). Export in `__all__`.
4. **Prefetch** — new `packages/va_workflows/src/va_workflows/activities/prerender.py`:
   `class OpenerPrerender(*, cache: PhraseCache, org_settings: OrgSettingsSource, model_id: str)` (protocol
   `OrgSettingsSource.get(org_id) -> OrgSettings | None`, `OrgSettingsReader` satisfies it) with
   - `def opener_for(self, context, now) -> str | None` = `None` when `config.wait_for_greeting` (the opener may
     answer the greeting), else `render_opener`.
   - `async def render(self, context) -> PrerenderResult` (frozen dataclass: `opener: bool`, `fillers: int`,
     `skipped: str | None`): org settings missing → skipped `org_missing`; `privacy.zero_retention or hipaa_mode`
     → skipped `zero_retention` (the REST client has no `enable_logging=false`; nothing is sent); else in ONE
     `asyncio.timeout(PRERENDER_BUDGET_S = 8.0)`: opener (if `context.opener_text`) via
     `cache.get_or_render(voice, cache_text(ctx, opener), fmt, ttl_s=PER_CALL_TTL_S)` and
     `cache.prefetch(voice, fmt, ttl_s=SHARED_TTL_S)` (STANDARD_FILLERS) concurrently. `PhraseUnavailableError`,
     `TimeoutError` → logged (`call_context.prerender_failed`, entry/error type only, never text), never raised.
   - `CallContextActivities(database, store, clock, *, prerender: OpenerPrerender | None = None)`: after
     `_build`, if prerender: `context = context.model_copy(update={"opener_text": prerender.opener_for(...)})`;
     **save the context first**, then `await prerender.render(context)`; `PrefetchResult` gains
     `opener_cached: bool = False` (fill from the result). Raise `PREFETCH_TIMEOUT` in `context_inputs.py` to 20 s
     (REST op timeout is 10 s); check workflow tests that pin it.
   - va_workflows pyproject: add `va-engine`, `va-voice` deps (+ `[tool.uv.sources]`), `uv lock`.
5. **Cached playback** (`va_voice.pipeline`): protocol `PhraseSource` in `protocols.py`:
   `async def lookup(self, text: str) -> CachedPhrase | None`. `CallPipeline(..., phrases: PhraseSource | None =
   None)` → `Speaker(..., phrases=)`. In `Speaker.speak`, for a `SpeakStatic` with a phrase source: `pronounced =
   applier.apply(text.strip())`; `phrase = await lookup(pronounced.text)` (any exception → log
   `pipeline_phrase_lookup_failed` → None); hit with `phrase.fmt == self._tts_format` → `_play_cached`: `_open`
   WITHOUT `tts.start_context` (factor a flag), `timeline.add_clause(pronounced)`, `timeline.add_chunk(phrase.audio,
   phrase.alignment)`, `outbound.push_audio(audio)`, `final.set()`, timer marks TTS_FIRST_TEXT/TTS_FIRST_AUDIO when a
   timer exists, `chars = len(text)`, then the normal `finish_utterance` / `_await_playback` / `_record`; no
   `tts.flush`. Barge-in/abandon paths already skip `tts.cancel` because `final` is set — test it. Miss/mismatch →
   existing live path unchanged. `SpeakText` never looks up. Verify `AlignmentTimeline.text_until` /
   `truncate_assistant_message` on a cached utterance (clause without the trailing space).
6. **Media wiring**: `apps/media_worker/src/va_media/phrases.py` `CallPhrases(redis, voice: PhraseVoice, fmt,
   metrics: PhraseCacheMetrics | None)` implementing `PhraseSource` via `read_phrase`, counting hit/miss.
   `CallRuntime(..., redis: Redis | None = None)` (open_runtime passes `container.redis`); `_converse` passes
   `phrases=CallPhrases(redis, phrase_voice(context, model_id=settings.elevenlabs_tts_model),
   tts-format AudioFormat of config.fmt (ULAW_8000 or PCM16LE_16000), PhraseCacheMetrics(self._metrics))` when
   redis is set. Note the lookup format comes from the actual leg format, so an hd_audio call streamed as PCMU
   simply misses.
7. **Worker wiring**: new `va_elevenlabs.settings.ElevenLabsRestSettings` (`elevenlabs_api_base` default
   `http://localhost:9200`, `elevenlabs_api_key`, `elevenlabs_tts_model`; reuse the default constants so media and
   worker agree) mixed into `WorkerSettings`; `va_worker.main.build_container` builds an `http_client(settings,
   timeout=...)` (closed on exit), `ElevenLabsRest(...)`, `PhraseCache(rest, redis)` (metrics=None unless the
   worker catalog has the counters — check `test_metrics`), `OrgSettingsReader(database, redis)`, and passes
   `OpenerPrerender` through `WorkerActivities`. specs/02 §6: `ELEVENLABS_TTS_MODEL` row services → `media, worker`
   (`ELEVENLABS_API_BASE`/`ELEVENLABS_API_KEY` already list worker); `.env.example` unchanged unless the mirror test
   says otherwise. AGENTS.md: one learned line.

### Tests (all must exist and pass)
- `packages/va_voice/tests/test_phrase_cache.py`: `read_phrase` hit / absent / malformed / Redis down / bad format.
- `packages/va_voice/tests/pipeline/test_pipeline_phrases.py` (harness + a `DictPhrases` fake): opener SpeakStatic
  hit → no `tts.start` for it in `transport.calls`/FakeTts records, caller receives exactly the phrase's frames,
  mark echo completes the AgentTurn (text = original); barge-in mid cached opener → `transport.clear`, no
  `tts.cancel`, `InterruptRecord` truncated text from the cached alignment; alignment `None` still plays; miss,
  lookup raising, fmt mismatch → live TTS context with the text; SpeakText never calls lookup.
- `packages/va_engine/tests/test_opener.py`: render with variables/request_data/builtins, blank/None/non-task →
  None, syntax error → raw text, `phrase_voice`/`phrase_format`/`cache_text`; TaskBrain speaks `opener_text`
  verbatim (FakeLLM untouched) and falls back to the template when absent.
- `packages/va_workflows/tests/test_prerender.py` (FakeElevenLabs via `rest_client_helpers.sim_rest()` +
  fakeredis + `OrgSettingsSource` fake): opener entry under `phrase_cache_key(phrase_voice(ctx), cache_text(ctx,
  text), phrase_format(ctx))` with TTL in (0, 3600]; 4 filler entries with TTL > 3600; pronunciation guide applied
  to the key text; zero_retention / hipaa → no sim TTS request, skipped; org missing → skipped; wait_for_greeting →
  opener None, fillers still rendered; sim TTS fault → result opener False, no raise; budget timeout (hanging synth
  fake) → logged, no raise.
- `packages/va_workflows/tests/test_call_context_activity.py` (extend): with prerender the stored context has
  `opener_text` and `PrefetchResult.opener_cached`; context is saved even when rendering fails; terminal call
  renders nothing.
- `apps/worker/tests`: container builds the prerender with the settings' model (`worker_helpers.dialing_settings`).
- `apps/media_worker/tests/test_runtime_sim.py` (+ unit `test_phrases.py`): seed the entry with the real
  `OpenerPrerender`/`PhraseCache` over FakeElevenLabs REST on the same `SimState`, context with `opener_text`, then
  `streamed_call`: the opener reaches the sim caller and no `state.elevenlabs.tts_requests[*].generated_text`
  contains the opener (TTS socket untouched for it); no entry → the TTS socket gets the opener text; broken Redis
  (`broken_redis()`-style client) → live socket. Runtime needs a fakeredis passed as `redis=` in the rig.

### Gates
`make fmt`, `make lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `make test`
(per-package ≥ 85 %, new modules ~100 %), `npx --yes jscpd@4 --silent` (0 clones), `uvx radon cc -s -a` on touched
src (no C+), audit (`uvx pip-audit` on `uv export` + `corepack pnpm audit`). `make e2e` harness does not exist until
P05-T17 — say so.

### Risks
- va_workflows now pulls va_engine/va_voice (numpy/onnxruntime) into the worker image: acceptable; check mypy
  `files`/`mypy_path` unaffected and the Dockerfile test still passes.
- Temporal workflow sandbox: the workflow module must not import `prerender` (activities only).
- `OrgSettingsReader.get` opens its own global session: call it outside the tenant session in the activity.
- Runtime sim tests: one realtime state; never busy-poll; read sim records inside the `async with`.

## Build P05-T10d (Builder, 2026-11-02) — done

Implemented per plan. Pieces: `CallContext.opener_text`; `va_voice.phrase_cache.read_phrase` (PhraseCache reads
through the same `_read_entry`); `va_voice.pipeline.PhraseSource` + `CallPipeline(phrases=)` → `Speaker._cached` /
`_play_cached` (no `start_context`, final set at once, timeline clause+chunk, TTS marks); `va_engine.opener`
(`parse_field` now shared with TaskBrain, `render_opener`, `phrase_voice`, `phrase_format`, `cache_text`);
TaskBrain speaks `opener_text` verbatim; `va_workflows.activities.prerender.OpenerPrerender` (8 s budget, skips
zero-retention/HIPAA/missing org/unreadable settings, never raises) + `CallContextActivities(prerender=)`
(context saved first; `PrefetchResult.opener_cached`; PREFETCH_TIMEOUT 20 s); worker container builds
ElevenLabsRest + PhraseCache + OrgSettingsReader (`ElevenLabsRestSettings` mixed into WorkerSettings; shared
`ElevenLabsAccountSettings`); media `va_media.phrases.CallPhrases` + `CallRuntime(redis=)`.
Specs: specs/07 §11 Decision P05-T10d, pointer in specs/09 §9, specs/01 §2.1 decision (pre-render + 20 s),
specs/02 `ELEVENLABS_TTS_MODEL` → media, worker. AGENTS.md line added.

Evidence: make lint pass; make typecheck pass (1140 files); make placeholders 0; check_spec_refs 0;
make test 9673 passed / 7 skipped, 99.15 % (new modules 100 %, speaker 99 %); one more va_voice test added
afterwards (va_voice suite 945 passed); jscpd 0 clones; radon no C+ on touched src (TaskBrain.__init__ kept
at B via `_prerendered` helper); pip-audit clean, pnpm audit 3 moderate (pre-existing, no TS change).
`make e2e`: the tests_e2e harness does not exist until P05-T17 (not run).
Note: during the build I briefly `git stash`ed while a test run was going; that run was discarded and the
full suite was rerun from a clean tree state (the 9673 result above).

## Rework P05-T10d (Builder, 2026-11-02) — after review.rejected

The rejection carried no findings (empty payload, no `## Review P05-T10d` section), so I self-reviewed the diff
and re-ran every gate. One fix: `OpenerPrerender._skip_reason` now also treats a `ValueError` (stale stored
settings failing `OrgSettings` validation in `OrgSettingsReader.get`) as `org_settings_unavailable`, so the
prefetch activity never fails on it; `test_unreadable_org_settings_render_nothing` is parametrized over a
ConnectionError and a real ValidationError and also asserts no TTS request went out.
Evidence: make fmt/lint/typecheck pass, placeholders 0, check_spec_refs 0, make test 9675 passed / 7 skipped
(99.16 %), jscpd 0 clones, radon no C+ on touched src. make e2e harness does not exist until P05-T17.
Reviewer: if you reject again, please write numbered findings under `## Review P05-T10d`.

## Rework 2 P05-T10d (Builder, 2026-11-02) — the rejections were timeouts, not findings

Diagnosis: `.ralph/diagnostics/logs/ralph-2026-11-02T05-12-57-*.log` shows both Reviewer iterations (06:07 and
06:22) ended with `Execution inactivity timeout reached, sending SIGTERM timeout_secs=300`: the Reviewer ran a
silent long command (`make test`, ~25 min, prints nothing for > 5 min) in the foreground, was killed, and the loop
turned the missing event into an empty `review.rejected`. The tree has not changed since the 06:16 build.
Reviewer: run `make test > logs/make_test.log 2>&1` with Bash `run_in_background` and watch it with a Monitor
that prints a heartbeat line every ~2 min (otherwise the 300 s inactivity timeout kills the iteration again).
Fresh evidence this iteration: lint 0, typecheck 0, placeholders 0, check_spec_refs 0, jscpd 0 clones, radon no
C+ on touched src, pip-audit clean, pnpm audit 3 moderate (pre-existing); make test result below.
make test (this iteration): 9675 passed, 7 skipped, 99.16 %, exit 0 (logs/make_test_p05t10d.log). make e2e harness does not exist until P05-T17.

## Review P05-T10d (Reviewer, 2026-11-02) — APPROVED

Re-ran (make test in background + heartbeat Monitor): make lint pass, make typecheck (1140 files) pass,
placeholders 0, check_spec_refs 0, make test 9675 passed / 7 skipped, 99.15 % (opener/phrases/prerender/
phrase_cache 100 %, speaker 99 %), jscpd 0 clones, radon no C+ on touched src. Diff checked against specs/07 §11
Decision P05-T10d, specs/09 §9 pointer, specs/02 §6 ELEVENLABS_TTS_MODEL row, plan acceptance (activity test vs
FakeElevenLabs, runtime opener without TTS socket request, miss/Redis-failure fallback). Cache key parity: Speaker
and cache_text both strip then apply the pronunciation guide; chars accounting matches the live path.
Non-blocking note: worker/media settings do not refuse the `sim-eleven-key` default in staging/production (same
as before this task). make e2e harness does not exist until P05-T17.
