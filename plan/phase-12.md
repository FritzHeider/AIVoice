# Phase 12 — Post-Call, Citations, Evals, Guard Rails, Compliance, Warm Transfer

- [ ] P12-T01 PostCallWorkflow skeleton steps: recording and re-transcription
  - Spec refs: specs/18-post-call-analysis.md (PostCallWorkflow step list, recording wait/copy, batch re-transcription merge rules, idempotency, timeouts)
  - Depends on: P05-T14, P03-T12
  - Deliverables: workflow with ordered idempotent steps, recording wait (10 min timeout), batch diarized transcription merge into `call_turns`
  - Acceptance: time-skipping tests for missing recording and merge correctness.

- [ ] P12-T02 Summary and dispositions
  - Spec refs: specs/18-post-call-analysis.md (summary prompt, disposition classification, built-in/system tags)
  - Depends on: P12-T01
  - Deliverables: summary step (summary_prompt), `/v1/dispositions` CRUD, classification step, system tags assignment for non-connected outcomes, `calls.summary`/`disposition_tag`
  - Acceptance: FakeLLM-driven tests; system tags without LLM calls.

- [ ] P12-T03 Analysis schema, analyze endpoint, sentiment
  - Spec refs: specs/18-post-call-analysis.md (analysis_schema extraction, POST /v1/calls/{id}/analyze, sentiment); specs/05-calls-api.md (analyze endpoint)
  - Depends on: P12-T02
  - Deliverables: extraction step, synchronous analyze endpoint (goal + questions → answers), sentiment step, `call_analyses` rows
  - Acceptance: API tests incl. typed answers per question type.

- [ ] P12-T04 Citations
  - Spec refs: specs/18-post-call-analysis.md (citations engine, citation schemas API)
  - Depends on: P12-T02
  - Deliverables: `/v1/citation_schemas` CRUD (enterprise gate), extraction with cited utterance mapping, conditions evaluation, `citations.ready` event + webhook
  - Acceptance: e2e citations_and_evals (citations half) passes.

- [ ] P12-T05 Evals workbench
  - Spec refs: specs/18-post-call-analysis.md (evals workbench API, scoring prompt, post_call_evals)
  - Depends on: P12-T02
  - Deliverables: `/v1/evals/setups` CRUD + versions, `POST /v1/evals/run`, `EvalRunWorkflow`, `post_call_evals` forcing recording, `evals.completed` event
  - Acceptance: e2e citations_and_evals (evals half) passes.

- [ ] P12-T06 Guard rails runtime
  - Spec refs: specs/23-guardrails-and-compliance.md (guard_rails param, evaluation timing, classifier prompts, actions)
  - Depends on: P07-T08, P05-T09
  - Deliverables: guard rail evaluator in both Brains, ai_disclosure/self_introduction/recording_disclosure timing checks, opt_out detection writing DNC, custom guard rails (max 5, enterprise), actions end_call/transfer/move_to_node, `guard_rail_events`, `call.guard_rail` events
  - Acceptance: e2e guard_rail_opt_out passes; unit tests per guard rail.

- [ ] P12-T07 Compliance controls: consent, redaction, DTMF capture, HIPAA mode
  - Spec refs: specs/23-guardrails-and-compliance.md (two-party consent, PII/PCI redaction, DTMF capture mode, HIPAA mode, retention)
  - Depends on: P12-T06
  - Deliverables: automatic recording disclosure by callee state, redaction pipeline (entity detection + regex fallback) applied to stored transcripts/summaries/webhooks per org setting, DTMF capture mode bypassing LLM, HIPAA mode toggles (`enable_logging=false`, transcript logging off)
  - Acceptance: tests prove card numbers never reach FakeLLM requests in DTMF capture mode.

- [ ] P12-T08 Warm transfer and post-transfer transcript
  - Spec refs: specs/01-architecture.md §2 (warm transfer); specs/06-telephony-telnyx.md (warm transfer sequence, conferences); specs/11-pathways-engine.md (Transfer Call warm settings); specs/05-calls-api.md (stream_post_transfer_transcript)
  - Depends on: P05-T13
  - Deliverables: proxy leg dial with link_to, proxy agent pipeline with briefing prompt, hold audio, conference create/join, AI removal, transcription-only stream after transfer, `call.post_transfer_transcript` events, billing split
  - Acceptance: e2e warm_transfer passes.

- [ ] P12-T09 Post-call step ordering and complete final webhook
  - Spec refs: specs/18-post-call-analysis.md (PostCallWorkflow step list ordering, final webhook)
  - Depends on: P12-T03, P12-T04, P12-T05, P06-T02
  - Deliverables: PostCallWorkflow steps ordered exactly as specified (recording → re-transcription → summary → disposition → analysis → sentiment → citations → evals → billing finalization → final webhook), per-step idempotency keys, `call.completed` carrying every analysis field; the memory update step is inserted in P13-T02
  - Acceptance: e2e recording_and_post_call passes with all analysis and price fields populated.

## Phase exit gate
`make check && make e2e` pass.
