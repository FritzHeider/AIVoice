## Phase 09 — Voices, Voice Cloning, and Standalone TTS Products

### Goal
Deliver the voice layer as a product: platform voice catalogue synced from ElevenLabs, per-org voices, settings,
ratings, rename/delete, Instant Voice Cloning with samples and plan caps, voice previews, per-call voice resolution with
overrides, and Bland-compatible standalone TTS endpoints (speak, streaming WebSocket, OpenAI-compatible) with character
metering.

### Read first
- `specs/15-voices.md` — entire file.
- `specs/09-tts-elevenlabs.md` — standalone TTS product endpoints, phrase cache, voice settings.
- `specs/24-billing-and-limits.md` — `tts_characters` and `voice_clone` meters, plan clone caps.
- `specs/19-events-and-webhooks.md` — `voice.clone_ready`, `voice.clone_failed`.

### Phase exit gate
`make check && make e2e` pass; a new e2e flow `cloned_voice_call` clones a voice in the simulator and places a call using
it (asserting the provider voice id used on the TTS socket).
