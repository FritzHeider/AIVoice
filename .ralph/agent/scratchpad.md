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

## Plan P05-T11a — In-call persistence: turn write-behind and live call events (Planner, 2026-11-02)

P05-T11 was split into T11a (this), T11b (end routine: hangup, final row, slot, workflow signal, worker_lost
hangup in CallWorkflow, stream_closed → user_hangup with 3 s wait), T11c (stop endpoints). P05-T10c depends on
P05-T11 (all three). Spec refs: specs/07 §8 steps 2/6/7, §16 Decision P04-T12a ("observer → write-behind,
call.turn, call.latency"), §20 Decision (latency events), specs/03 call_turns DDL, specs/19 §1 step 4 + §2 rows
`call.turn` (data role, text, node_id, interrupted), `call.interrupt` (played_chars, total_chars), `call.latency`.
Write `> Decision (2026-11-02): in-call persistence (P05-T11a)` under specs/07 §8 covering every rule below.

### Design (decided)
1. **Repo** `va_db.repositories.CallTurnRepo(session, org_id)` (new module `repositories/call_turns.py`,
   exported): `upsert_many(rows: Sequence[CallTurnRow])` = one `INSERT ... ON CONFLICT (call_id, turn_index, role)
   DO UPDATE SET text, text_spoken, interrupted, start_ms, end_ms, node_id, node_name, language_code, latency =
   call_turns.latency || EXCLUDED.latency` (ids from `va_common.ids` uuid7); `delete_turn(call_id, turn_index,
   role)`; `list_for_call(call_id)` ordered by `(turn_index, role)` (used by tests and later P05-T16).
   `CallTurnRow` = frozen dataclass in `va_db.call_turns` (org_id, call_id, turn_index, role, text, text_spoken,
   interrupted, start_ms, end_ms, node_id, language_code, latency dict). Tenant session only (RLS).
2. **Turn writer** `va_media.turn_writer.TurnWriter(database, *, clock, interval_s=2.0)`: per-process
   write-behind like `CallEventBuffer` (reuse its structure, not copy: if the loop/lock/trim logic would clone,
   factor a small generic `va_events.batching` helper or make TurnWriter compose a shared base; jscpd must stay 0).
   API: `add(row)`, `add_delete(org_id, call_id, turn_index)`, `run()` (flush every 2 s, supervised by the media
   lifespan), `flush_call(call_id)` (writes that call's pending ops now; used at call end in T11b), `aclose()`.
   One tenant transaction per org per flush; a failed org's ops are re-queued in order (bounded pending, oldest
   dropped with `media.turns_dropped` warning); ops for one call keep their order (delete after its upsert).
3. **Recorder** `va_media.turns.TurnRecorder` implements `va_voice.pipeline.PipelineObserver` (sync callbacks →
   schedule work on supervised background tasks owned by the recorder; `aclose()`/`drain()` awaits them):
   - timeline: `TurnRecorder(context, writer, publisher, *, origin_ms, answered_offset_ms)`; `start_ms/end_ms =
     round(pipeline_ms - origin_ms + answered_offset_ms)` clamped ≥ 0; `origin_ms` = `clock.monotonic()*1000` when
     the runtime builds the pipeline, `answered_offset_ms` = `clock.now() - carrier_state.answered_at` in ms when the
     snapshot has `answered_at` (≥ 0), else 0.
   - `on_user_turn`: row role `user` (text, language_code, start/end); a `supersedes` index → `add_delete` of
     that user row (the superseding turn carries its text) and a `call.turn` for the new turn only. DTMF turns are
     stored with their `"[DTMF: …]"` text. Event `call.turn` data `{turn_index, role:"user", text, node_id: null,
     interrupted: false}`, message = text truncated to 200 chars.
   - `on_agent_turn`: role `assistant`, `text`=original, `text_spoken`, `interrupted`, start/end from
     started_ms/ended_ms; `call.turn` with role `assistant`.
   - `on_latency(report)`: add `turn_index` to `va_voice.latency.TurnReport` (from the timer) if absent; latency
     goes onto the **user** row of that index through the upsert merge (`latency || EXCLUDED.latency`, so a later
     latency-only upsert must not clobber text: implement as a separate `merge_latency(call_id, turn_index,
     role, latency)` op in the writer, or include the full row — choose the separate op, UPDATE ... SET latency =
     latency || :l; if the row is not yet flushed, the writer applies ops in order so the UPDATE follows the
     INSERT). Each `report.events` item → `call.latency` event (message, data verbatim).
   - `on_interrupt(record)`: `call.interrupt` with data `{turn_index, played_chars, total_chars, played_ms,
     reaction_ms}`; if `InterruptRecord` lacks played/total chars, add them in `va_voice.pipeline` (the pipeline has
     the truncated text: `len(text_spoken)`, `len(text)`), keep existing tests green.
   - Events go through `EventPublisher.publish(new_event(EventType.X, org_id=, call_id=, clock=, ...))` whose
     `call_events` sink is the per-process `CallEventBuffer(DbCallEventWriter(database))`; publish failures
     (`EventPublishError`) are logged `media.event_publish_failed` (type only), never raised into the pipeline.
     Never log turn text.
4. **Lifespan/runtime wiring** (`va_media.main` + `runtime.open_runtime`): build `CallEventBuffer` +
   `EventPublisher(redis, call_events=buffer)` + `TurnWriter` once; run `buffer.run()` and `writer.run()` under the
   lifespan supervisor (or the runtime's AsyncExitStack with supervised tasks — follow how `ChannelHub.run` is
   supervised); `aclose()` both on shutdown (after sessions drained). `CallRuntime(..., recorder_factory=)` or
   direct `writer=`/`publisher=` kw-only (default None → `NULL_OBSERVER`, so existing tests stay valid);
   `_converse` passes `observer=TurnRecorder(...)` to `CallPipeline` and, in a `finally`, `await
   recorder.drain()` then `await writer.flush_call(call_id)` and `await buffer.flush_call(call_id)` (T11b will move
   this into the end routine; keep it in one helper `_flush_call(context)`).
5. No migration (call_turns exists). AGENTS.md: one learned line (TurnRecorder/TurnWriter, test helpers).

### Tests
- `packages/va_db/tests/test_call_turn_repo.py` (Postgres, module DB via existing support): upsert insert+update
  idempotent, latency merge keeps text, delete, list order, RLS (other org sees nothing).
- `apps/media_worker/tests/test_turn_writer.py` (Postgres + unit with a fake writer): interval flush on
  ManualClock, flush_call only that call, failure re-queue keeps order, per-org transactions, aclose flushes,
  pending cap drops oldest.
- `apps/media_worker/tests/test_turns.py` (unit, fake writer + `publisher_rig`-style fakeredis publisher): user/
  agent rows with answer offset, supersede → delete + one event, DTMF turn, interrupted agent turn text_spoken,
  latency merge op + events (no e2e → only stage events), interrupt event data, publish failure swallowed and
  logged without text.
- `packages/va_voice` tests for any new `TurnReport.turn_index` / `InterruptRecord` chars fields.
- `apps/media_worker/tests/test_runtime_sim.py` (extend; real Postgres via the media test DB support or the
  va_db `running_postgres` helpers; SystemClock-timed rows need a current `call_events` partition — seed the call
  with `call_rows.seed_call`): a FakeTelnyx+FakeElevenLabs+FakeLLM conversation (opener + one caller utterance +
  reply) leaves `call_turns` rows `assistant 0 / user 1 / assistant 2` (indexes per the pipeline counter) and
  `call_events` rows `call.turn` x3 plus `call.latency` for the answered turn, all with the context's org_id.

### Gates
`make fmt`, `make lint typecheck placeholders`, `uv run python scripts/check_spec_refs.py`, `make test` in the
background with a heartbeat Monitor (≥ 85 %/package, new modules ~100 %), `npx --yes jscpd@4 --silent` (0),
`uvx radon cc -s -a` on touched src (no C+), audit. `make e2e` harness does not exist until P05-T17 — say so.

### Risks
- PipelineObserver callbacks are sync: never await in them; background tasks must be tracked and drained, or a
  call's last turns are lost / tasks leak across tests (filterwarnings=error).
- Ordering between the upsert and the latency merge for one turn: the writer must apply a call's ops in order
  within one transaction.
- Media tests that build `CallRuntime` without writer/publisher must keep working (defaults None).
- `call_events` partitions: use real time (`SystemClock`) for rows written to Postgres in tests.

## Build P05-T11a (Builder, 2026-11-02)

Implemented per plan. Pieces: `va_db.call_turns` (`CallTurnRow`, `LatencyMerge`, `TurnDelete`, `TurnOp`);
`va_db.repositories.CallTurnRepo` (`upsert_many` ON CONFLICT DO UPDATE with `latency || EXCLUDED.latency`, in-batch
key collapse, `merge_latency`, `delete_turn`, `list_for_call`, refuses rows of another org);
`va_voice.latency.TurnReport.turn_index` (InterruptRecord already had played/total chars);
`va_events.batching.wait_for_trigger` (shared by `CallEventBuffer` and the turn writer, keeps jscpd 0);
`va_media.turn_writer.TurnWriter` (sync `add`, 2 s loop, `flush_call`, per-org transactions, in-order requeue,
`TurnWriteError`, cap 10 000); `va_media.turns.TurnRecorder` (observer, ordered publish chain, `drain`) +
`CallPersistence` (`recorder(context, clock=, answered_at=)`, `finish_call`); `va_media.runtime.open_persistence`
entered by `open_runtime`; `CallRuntime(persistence=)` (default None → NULL_OBSERVER) with `finish_call` pushed on
the call's exit stack. va-media-worker now depends on va-events (uv.lock updated). Spec: specs/07 §8 Decision
(2026-11-02) P05-T11a. Tests: va_db `test_call_turn_repo.py`, media `test_turn_writer.py`, `test_turns.py`,
`test_runtime_sim.py::test_a_conversation_leaves_ordered_turns_and_call_events` (real Postgres; session fixture
`media_db_url` moved to the media conftest; `streamed_call(call_id=)`, `Rig.call(seeded=)`).
Note: radon reports `TurnTracer.emit` C (11) in latency.py — pre-existing, not modified (only TurnReport changed).
Evidence: make fmt ok; make lint pass; make typecheck pass (1148 files); placeholders 0; check_spec_refs 0;
make test 9712 passed / 7 skipped, 99.16 %, exit 0 (logs/make_test_p05t11a.log; new modules 100 %, runtime 99 %
with the pre-existing no-Redis line); jscpd 0 clones; radon: no C+ in new code (latency.py `TurnTracer.emit` C is
pre-existing, untouched); pip-audit clean; pnpm audit 3 moderate (pre-existing, no TS change). make e2e: the
tests_e2e harness does not exist until P05-T17 (not run).

## Plan P05-T11a — blocker resolution (Planner, 2026-11-02, after build.blocked)

Cause: backpressure turned `build.done` into `build.blocked` because the payload said `complexity: 11`
(radon `TurnTracer.emit` C (11) in `packages/va_voice/src/va_voice/latency.py`, a file this task touches). The
loop needs every touched src function at ≤ B (≤ 10), and the payload's complexity number must be the real max.
Everything else in the build stands (do NOT redo the implementation). Acceptance in plan/phase-05.md amended.

### Builder steps (only these)
1. Refactor `TurnTracer.emit` (latency.py:319) without behaviour change, e.g.:
   - `_turn_attributes(timer, latency) -> dict[str, AttributeValue]` (call/org/turn_index/interrupted, optional
     node_id, optional e2e_ms, `timer.annotations("turn")`);
   - `_emit_stage_spans(context, timer, latency, measured)` (the METRIC_SPANS loop);
   - `_emit_tool_spans(context, timer)` (the tools loop, status code optional).
   `emit` keeps: measure → latency → early return on missing started/ended → start turn span → helpers → end.
   Check `uvx radon cc -s packages/va_voice/src/va_voice/latency.py` → every block A/B.
2. Existing `packages/va_voice/tests/test_latency.py` span tests must stay green unchanged (they pin span names,
   attributes, timings); run `uv run pytest packages/va_voice -q`.
3. Re-run gates: make fmt, make lint typecheck placeholders, check_spec_refs, `make test` in background with a
   heartbeat Monitor, jscpd, `uvx radon cc -s -n C apps/media_worker/src packages/va_db/src packages/va_events/src
   packages/va_voice/src` (must print nothing), audit.
4. Emit build.done WITHOUT `--json`, plain text containing `tests: pass`, `lint: pass`, `typecheck: pass`,
   `audit: pass`, `coverage: pass`, `duplication: pass`, `complexity: <max CC number of touched src, ≤ 10>`,
   `specs: pass`. Do not append explanations after the complexity number that mention C/11.

## Build P05-T11a blocker fix (Builder, 2026-11-02)

`TurnTracer.emit` split into `_turn_attributes`, `_emit_stage_spans`, `_emit_tool_spans` (no behaviour change;
test_latency span tests unchanged and green). latency.py now all A; max CC over touched src = 9.
Evidence: make fmt 0; make lint 0; make typecheck pass (1148 files); placeholders 0; check_spec_refs 0;
radon -n C over media_worker/va_db/va_events/va_voice src prints nothing; make test 9712 passed / 7 skipped,
99.16 %, exit=0 (logs/make_test_p05t11a_fix.log); jscpd 0 clones; pip-audit clean; pnpm audit 3 moderate
(pre-existing). make e2e harness does not exist until P05-T17 (not run).

## Review P05-T11a (Reviewer, 2026-11-02) — APPROVED

Re-ran: make lint 0, make typecheck 0, placeholders 0, check_spec_refs 0, make test 9712 passed / 7 skipped,
99.16 % (turns/turn_writer/call_turns/repo/batching 100 %, runtime 99 %), jscpd 0 clones, radon -n C on touched
src prints nothing. Diff checked against specs/03 call_turns DDL (UNIQUE (call_id, turn_index, role) backs the
ON CONFLICT), specs/19 §2 call.turn/interrupt/latency data, specs/07 §8 Decision P05-T11a; pipeline timeline is
clock.monotonic()*1000, matching the recorder origin. No text in logs, tenant sessions only, publish failures
logged. make e2e harness does not exist until P05-T17.
