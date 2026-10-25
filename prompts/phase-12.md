## Phase 12 — Post-Call Analysis, Citations, Evals, Guard Rails, Compliance Controls, and Warm Transfer

### Goal
Turn calls into structured outcomes and enforce compliance: `PostCallWorkflow` (recording, diarized re-transcription,
summary, dispositions, analysis schema, analyze endpoint, sentiment, citations, evals, memory hook, billing hook, final
webhook), citation schemas API, evals workbench API, guard rails at runtime (TCPA disclosures, opt-out, custom), two-party
consent recording disclosure, PII/PCI redaction, DTMF capture mode, HIPAA mode, and warm transfer with post-transfer
transcript streaming.

### Read first
- `specs/18-post-call-analysis.md` — entire file.
- `specs/23-guardrails-and-compliance.md` — entire file.
- `specs/01-architecture.md` §2 (warm transfer) and `specs/06-telephony-telnyx.md` (warm transfer sequence, conferences).
- `specs/08-stt-elevenlabs.md` — batch re-transcription and entity detection.
- `specs/10-llm-gateway.md` — analysis model role and guard-rail classifier prompt.

### Phase exit gate
`make check && make e2e` pass including recording_and_post_call, citations_and_evals, guard_rail_opt_out, warm_transfer.
