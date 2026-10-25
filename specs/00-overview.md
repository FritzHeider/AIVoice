# 00 — Product Overview, Scope, and Non-Negotiables

## 1. What we are building

**VoiceAgents** is an enterprise-grade, developer-first voice AI platform functionally equivalent to Bland AI
(`docs.bland.ai`). Businesses use it to build, deploy, and scale AI phone agents that hold human-sounding,
multi-turn conversations on inbound and outbound phone calls, and extend the same agents to SMS and a web
chat/voice widget with shared conversational context.

The platform is API-first. Every capability is reachable over a versioned REST API whose paths, parameter
names, and semantics mirror Bland's documented API so existing Bland integrations can switch base URLs with
minimal change ("Bland-compatible mode"). A Next.js dashboard sits on top of the same public API.

## 2. Vendor decisions (fixed — do not substitute)

| Layer | Vendor / technology | Product / endpoint |
|---|---|---|
| PSTN telephony, numbers, call control | **Telnyx** | Call Control API v2 (`https://api.telnyx.com/v2`), Call Control Applications, Media Streaming over WebSocket (bidirectional RTP mode) |
| Answering machine detection | **Telnyx** | `answering_machine_detection: "premium"` on dial/transfer |
| Call recording | **Telnyx** | `record_start` / dial `record: "record-from-answer"`, dual channel, `call.recording.saved` webhook; copied into our S3 bucket |
| SMS / MMS | **Telnyx** | Messaging API (`POST /v2/messages`), Messaging Profiles, 10DLC |
| Number search / purchase / release | **Telnyx** | `GET /v2/available_phone_numbers`, `POST /v2/number_orders`, `PATCH/DELETE /v2/phone_numbers/{id}` |
| LLM (dialogue + router/classifier + post-call analysis) | **Telnyx Inference** (OpenAI-compatible) | Base URL `https://api.telnyx.com/v2/ai/openai`, default dialogue model `zai-org/GLM-5.3-Flash`, provider abstraction allows any OpenAI-compatible endpoint per org |
| Embeddings (knowledge bases, memory search) | **Telnyx Inference** | `Qwen/Qwen3-Embedding-8B` with `dimensions: 1024` via `/embeddings` |
| Text-to-speech (streaming) | **ElevenLabs** | `wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/multi-stream-input`, model `eleven_flash_v2_5`, `output_format=ulaw_8000`, `sync_alignment=true` |
| Speech-to-text (streaming) | **ElevenLabs** | `wss://api.elevenlabs.io/v1/speech-to-text/realtime`, `model_id=scribe_v2_realtime`, `audio_format=ulaw_8000`, `commit_strategy=manual`, `include_timestamps=true` |
| Voice library and voice cloning | **ElevenLabs** | `GET /v2/voices`, `POST /v1/voices/add` (Instant Voice Cloning), `POST /v1/voices/{voice_id}/edit`, `DELETE /v1/voices/{voice_id}`, `GET /v1/voices/{voice_id}/settings` |
| Post-call batch transcription (recording re-transcription with diarization) | **ElevenLabs** | `POST /v1/speech-to-text` with `model_id=scribe_v2`, `diarize=true`, `timestamps_granularity=word` |
| Card payments for credits/subscriptions | Stripe | Checkout Sessions, Customer Portal, webhooks — behind a `BillingProvider` interface |

Audio is **G.711 μ-law 8 kHz mono end-to-end** on PSTN calls: Telnyx sends PCMU, ElevenLabs Scribe accepts
`ulaw_8000`, and ElevenLabs TTS emits `ulaw_8000`. The media worker decodes to 16-bit PCM only for local VAD,
energy metrics, background-track mixing, and recording analysis. An optional `hd_audio` mode (L16 16 kHz on
the Telnyx leg, `pcm_16000` to Scribe and from TTS) is supported per call (see `specs/07-media-pipeline.md`).

## 3. Scope — the complete feature inventory

Every row below is in scope and has a dedicated spec. Nothing is "future work" unless explicitly listed in §6.

| # | Capability | Spec |
|---|---|---|
| 1 | Architecture, services, runtime topology | `01-architecture.md` |
| 2 | Monorepo layout, tooling, coding conventions, quality gates | `02-repo-layout-and-conventions.md` |
| 3 | Complete relational data model (DDL), ClickHouse analytics schema | `03-data-model.md` |
| 4 | Orgs, sub-accounts, users, memberships, RBAC, API keys, SSO, sessions, audit log | `04-auth-tenancy-rbac.md` |
| 5 | Calls API: Send Call (every Bland parameter), call details, list, stop, active calls, recordings, transcripts, analyze, event stream | `05-calls-api.md` |
| 6 | Telnyx integration: Call Control app, webhooks, signature verification, outbound dial, inbound answer, AMD, transfer, bridging, conferences, DTMF, recording, hangup causes | `06-telephony-telnyx.md` |
| 7 | Real-time media pipeline: framing, codecs, VAD, endpointing, adaptive timing, interruptibility, barge-in, playback tracking, background audio, latency budget | `07-media-pipeline.md` |
| 8 | Streaming STT with ElevenLabs Scribe v2 Realtime, keywords/keyterms, language handling, post-call diarized re-transcription | `08-stt-elevenlabs.md` |
| 9 | Streaming TTS with ElevenLabs multi-context WebSocket, chunking, alignment, cancellation, pronunciation guides, pre-rendered phrases cache | `09-tts-elevenlabs.md` |
| 10 | LLM gateway (Telnyx Inference, OpenAI-compatible), dialogue streaming, router/classifier calls, tool calling, prompt assembly, caching, fallbacks | `10-llm-gateway.md` |
| 11 | Conversational Pathways execution engine: every node type, edges, global nodes, loop conditions, variables, fine-tuning examples, transfers between pathways | `11-pathways-engine.md` |
| 12 | Pathways API: CRUD, versions, drafts, publish, staging/production pins, folders, test chat, unit tests, simulation | `12-pathways-api.md` |
| 13 | Custom tools, dynamic data, JSONPath mapping, speech-during-tool, custom code nodes (Deno sandbox) | `13-tools-and-dynamic-data.md` |
| 14 | Knowledge bases: upload, web scrape/crawl, text, chunking, embeddings, hybrid retrieval, KB chat | `14-knowledge-bases.md` |
| 15 | Voices: library sync, cloning, settings, ratings, samples, per-org voice catalog | `15-voices.md` |
| 16 | Phone numbers: search, purchase, release, inbound configuration, labels, BYO Telnyx account, number pools, local presence | `16-phone-numbers.md` |
| 17 | Batches/campaigns, scheduling, retries, calling windows, DNC, concurrency/rate caps | `17-batches-and-scheduling.md` |
| 18 | Post-call analysis: summaries, dispositions, structured analysis, citations, evals workbench, QA scoring | `18-post-call-analysis.md` |
| 19 | Event bus, webhooks (call + batch + SMS + widget), signing, retries, DLQ, replay, live event stream | `19-events-and-webhooks.md` |
| 20 | Memory (contacts, facts, summaries), personas, agents with versions/branches, A/B experiments | `20-memory-personas-agents.md` |
| 21 | SMS conversations on Telnyx, omnichannel shared context, 10DLC registration workflow | `21-messaging-sms.md` |
| 22 | Web widget (chat + voice over WebSocket audio), allowed domains, rate limits, human takeover | `22-web-widget.md` |
| 23 | Guard rails (TCPA disclosure, opt-out, custom), compliance controls, consent, calling hours | `23-guardrails-and-compliance.md` |
| 24 | Metering, credits ledger, plans, limits, Stripe, invoices, usage API | `24-billing-and-limits.md` |
| 25 | Dashboard (Next.js): every page and component including the React Flow pathway editor | `25-dashboard.md` |
| 26 | Python SDK, TypeScript SDK, CLI, MCP server, docs site, `llms.txt` | `26-sdks-cli-mcp-docs.md` |
| 27 | Observability: logs, traces, metrics, per-turn latency spans, dashboards, alerts | `27-observability.md` |
| 28 | Security: secrets, encryption, PII redaction, retention, audit, threat model | `28-security.md` |
| 29 | Infrastructure: Docker, docker-compose, Helm, Terraform (AWS), CI/CD, scaling, draining | `29-infrastructure.md` |
| 30 | Testing strategy: unit, contract, simulator end-to-end, load, live smoke, voice evals | `30-testing-and-evals.md` |
| 31 | Error catalog and response envelopes (native + Bland-compatible) | `31-error-catalog.md` |

## 4. Non-negotiable quality rules (apply to every line of code)

1. **No placeholders.** No `TODO`, `FIXME`, `XXX`, `pass` as a function body, `...` as a body, `raise NotImplementedError`,
   "implement later", mock data in production code paths, or commented-out code. CI enforces this with
   `scripts/check_no_placeholders.py` (see `02-repo-layout-and-conventions.md` §9).
2. **Complete files.** Every file is written in full. Never abbreviate with "rest unchanged" or similar.
3. **Spec is law.** If code and spec disagree, fix the code. If the spec is wrong or ambiguous, update the spec
   file in the same commit with a dated `> Decision (YYYY-MM-DD):` note explaining the change.
4. **Tested.** Every public function, endpoint, state transition, and node type has tests. Coverage gate:
   ≥ 85 % line coverage per Python package, ≥ 80 % for TypeScript packages.
5. **Typed.** Python: `mypy --strict` clean. TypeScript: `strict: true`, no `any` except at JSON boundaries
   wrapped in a zod parse.
6. **Simulator-first.** No test depends on real Telnyx, ElevenLabs, Stripe, or Telnyx Inference. In-repo
   simulators (`packages/va_sim`) emulate all of them. Live smoke tests exist but run only with `LIVE_TESTS=1`.
7. **Multi-tenant safe.** Every query that touches tenant data filters by `org_id`; Postgres Row-Level Security is
   enabled as defense in depth. Tests prove cross-tenant access is impossible for every resource.
8. **Idempotent and durable.** External side effects (dial, SMS send, webhook delivery, charge) are idempotent
   via keys and recorded in the database before/after execution.
9. **Observable.** Every call produces per-turn latency spans and structured events.
10. **Secure by default.** Secrets only from environment or the secrets backend; vendor credentials encrypted at
    rest with envelope encryption; webhooks signed; inbound vendor webhooks verified.

## 5. Performance targets (acceptance criteria)

| Metric | Target | Measured by |
|---|---|---|
| Voice-to-voice latency p50 (end of caller speech → first agent audio sent to Telnyx) | ≤ 800 ms with live vendors; ≤ 150 ms of platform overhead in simulator runs | `call_turns.e2e_ms`, load test report |
| Voice-to-voice latency p95 | ≤ 1 300 ms live; ≤ 250 ms platform overhead in simulator | same |
| Barge-in reaction (user speech detected → `clear` sent to Telnyx) | ≤ 120 ms | `call_events` `interrupt` timing |
| API p95 for `POST /v1/calls` (excluding dial) | ≤ 150 ms | k6 load test |
| Concurrent calls per media-worker pod (2 vCPU, 4 GiB) | ≥ 50 simulated calls with no audio underrun | load test |
| Platform concurrent calls | horizontally scalable; load test proves 1 000 concurrent simulated calls across pods | `scripts/load/` |
| Webhook delivery | first attempt ≤ 2 s after event; at-least-once; 8 retries over 24 h | dispatcher tests |
| Billing accuracy | usage events reconcile to call durations within ±0.5 % | reconciliation job test |

## 6. Explicitly out of scope for v1

- Self-hosted STT/TTS/LLM models (the provider interfaces in `08`, `09`, `10` keep this possible later).
- Our own SIP edge (Kamailio/FreeSWITCH). Telnyx is the only carrier.
- iMessage and RCS channels (the `conversations.channel` enum reserves `imessage` and `rcs`; endpoints return
  `CHANNEL_NOT_SUPPORTED`).
- Twilio BYOT. Bland's BYOT uses Twilio; our equivalent is "Bring Your Own Telnyx" (`16-phone-numbers.md` §7).

## 7. Glossary

| Term | Meaning |
|---|---|
| Call | One phone conversation handled by an agent; may have child legs (transfer target, warm-transfer proxy). |
| Leg | One Telnyx call (`call_control_id`). A call has a primary leg and optional transfer legs. |
| Runner | What drives the conversation: `task` prompt, `pathway`, `persona`, or `agent` (exactly one). |
| Pathway | Versioned directed graph of nodes and natural-language-labelled edges (Bland "Conversational Pathways"). |
| Node | A step in a pathway (Default, Webhook, Knowledge Base, Transfer Call, End Call, Wait for Response, SMS, Custom Code, Press Button, Transfer Pathway, Scheduling, Global). |
| Turn | One user utterance plus the agent's reply. |
| EOT | End of turn — the moment the platform decides the caller finished speaking. |
| Barge-in | Caller speech during agent playback that interrupts the agent. |
| Persona | Reusable call preset (voice, prompt/pathway, tools, memory policy) with versions. |
| Agent | Versioned bundle (graph + call settings) with environment pins and A/B experiments. |
| Org | Tenant. Sub-accounts are child orgs. |
| Media worker | Process that runs real-time call pipelines (one asyncio task group per call). |
| Simulator | In-repo fake of a vendor (Telnyx, ElevenLabs, Telnyx Inference, Stripe) used by tests and local dev. |
