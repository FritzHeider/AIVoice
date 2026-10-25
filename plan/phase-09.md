# Phase 09 — Voices, Voice Cloning, Standalone TTS

- [ ] P09-T01 Platform voice catalogue sync
  - Spec refs: specs/15-voices.md (catalog sync and curated list)
  - Depends on: P03-T12, P01-T04
  - Deliverables: `VoiceCatalogSyncWorkflow` (daily Temporal schedule), curated rules, upsert of platform voices (org_id NULL), seed default voice per `PLATFORM_DEFAULT_VOICE`
  - Acceptance: tests against FakeElevenLabs with pagination; removed provider voices soft-deleted.

- [ ] P09-T02 Voices API
  - Spec refs: specs/15-voices.md (endpoints, visibility, permissions)
  - Depends on: P09-T01
  - Deliverables: list (filters), get, rename, settings PATCH, delete, rate; Bland-compatible response fields
  - Acceptance: API tests; platform voices immutable by orgs.

- [ ] P09-T03 Voice cloning
  - Spec refs: specs/15-voices.md (clone endpoint, sample validation, storage, error mapping)
  - Depends on: P09-T02
  - Deliverables: multipart upload, `mutagen` duration validation, S3 storage, `VoiceCloneWorkflow`, caps (`VOICE_CLONE_LIMIT`), events, metering, samples CRUD
  - Acceptance: success and every failure path tested against FakeElevenLabs.

- [ ] P09-T04 Voice previews
  - Spec refs: specs/15-voices.md (preview sample)
  - Depends on: P09-T02, P04-T11
  - Deliverables: `GET /v1/voices/{id}/sample` (text param, cached render, mp3 or wav output)
  - Acceptance: cache reuse test.

- [ ] P09-T05 Standalone TTS endpoints
  - Spec refs: specs/09-tts-elevenlabs.md (standalone TTS product endpoints)
  - Depends on: P03-T10, P09-T02
  - Deliverables: `POST /v1/speak`, `WSS /v2/tts/ws` (warm session, cancel/replace), `POST /v1/audio/speech` (OpenAI-compatible), per-character metering
  - Acceptance: protocol tests; billing events emitted with correct character counts.

- [ ] P09-T06 Per-call voice resolution and overrides
  - Spec refs: specs/15-voices.md (voice settings precedence); specs/05-calls-api.md (voice parameter)
  - Depends on: P09-T02, P05-T15
  - Deliverables: resolution by name/UUID with org-first precedence, settings merge (voice defaults ← org overrides ← call overrides), `VOICE_NOT_FOUND`
  - Acceptance: e2e cloned_voice_call passes.

## Phase exit gate
`make check && make e2e` pass.
