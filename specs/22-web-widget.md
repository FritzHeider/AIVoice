# 22 — Web Widget (Chat + Voice over WebSocket)

Package ownership: management API `apps/api/src/va_api/routers/widgets.py`; public widget endpoints
`apps/api/src/va_api/routers/widget_public.py`; real-time socket handler `apps/media_worker/src/va_media/widget_ws.py`;
voice transport `va_voice.transports.browser_ws` (`07-media-pipeline.md`); embed script `apps/widget` (Vite library
build, vanilla TypeScript, no framework); dashboard config UI (`25-dashboard.md`).

Management endpoints use the **envelope** format. Public endpoints use plain JSON shapes defined here.

## 1. Concepts

- A **widget** (`widgets` row) binds an agent runner (pathway / persona / agent / `agent_prompt`) to a public embed key
  `public_key` (`"wgt_" + base62(20 random bytes)`), an Origin allowlist, rate limits, appearance config, and optional voice.
- A **thread** is a `conversations` row with `channel='widget'`, `widget_id`, and `widget_visitor_id` (random id
  persisted in the visitor's `localStorage` under `va_widget_visitor_{public_key}`; regenerated if storage unavailable).
- Text messages run the engine in text mode (`va_engine.text_mode`); voice sessions run the full real-time pipeline with
  the browser transport. Both append to the same thread and contact memory (when the host page identifies the visitor,
  §5.3).

## 2. Management API (`/v1/widget`)

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/v1/widget` | `WidgetCreate` | `{data: Widget}` |
| GET | `/v1/widget` | `cursor?, limit?` | `{data: [Widget], pagination}` |
| GET | `/v1/widget/{widget_id}` | — | `{data: Widget}` |
| PATCH | `/v1/widget/{widget_id}` | subset of `WidgetCreate` | `{data: Widget}` |
| DELETE | `/v1/widget/{widget_id}` | — | soft delete; script stops working within 60 s (session cache TTL) |
| POST | `/v1/widget/{widget_id}/rotate_key` | — | new `public_key`; old key valid 24 h |
| GET | `/v1/widget/{widget_id}/threads` | `status?, cursor?, limit?` | list of threads (Conversation objects, `21-messaging-sms.md` §9 shape) |
| GET | `/v1/widget/{widget_id}/threads/{thread_id}` | — | thread + messages |
| POST | `/v1/widget/{widget_id}/threads/{thread_id}/message` | `{text, takeover?: bool = true}` | live-agent (human) message; with `takeover=true` sets thread `status='human_takeover'` and suppresses AI replies until `POST …/release` |
| POST | `/v1/widget/{widget_id}/threads/{thread_id}/release` | — | ends human takeover; AI resumes |
| POST | `/v1/widget/{widget_id}/threads/{thread_id}/close` | — | close thread (analysis like SMS close) |
| GET | `/v1/widget/{widget_id}/embed_code` | — | `{data: {html: "<script …>"}}` |

`WidgetCreate`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string ≤ 120 | required | |
| `pathway_id` / `pathway_version` | uuid / int\|`"production"`\|`"staging"` | — / `"production"` | |
| `agent_id` | uuid | — | |
| `persona_id` | uuid | — | |
| `agent_prompt` | string ≤ 20 000 | — | free-form runner |
| `allowed_domains` | string[] ≤ 50 | [] | hostnames, optional leading `*.` wildcard; empty list = **deny all** in production, allow `localhost` in local env |
| `messages_per_minute` | int 1–600 | 20 | per visitor |
| `voice_enabled` | bool | false | |
| `voice_id` | uuid | org default | |
| `config` | WidgetConfig (§3) | defaults | |
| `webhook_url` | URL | — | receives `widget.message`, `conversation.*` envelope events |

Runner rule: exactly one of `pathway_id`, `agent_id`, `agent_prompt`; `persona_id` may combine with `pathway_id`.

## 3. WidgetConfig (stored in `widgets.config`)

```python
class WidgetTheme(BaseModel):
    primary_color: str = "#4F46E5"          # hex
    background_color: str = "#FFFFFF"
    text_color: str = "#111827"
    font_family: str = "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    border_radius_px: int = 12              # 0–32
    mode: Literal["light","dark","auto"] = "auto"

class CustomComponent(BaseModel):
    name: str                               # ^[a-z][a-z0-9_-]{0,39}$ referenced by the engine via a tool/node output {"component": name, "props": {...}}
    type: Literal["buttons","card","form","link","image"]
    schema: dict[str, JsonValue]            # JSON Schema of props, validated before render

class WidgetConfig(BaseModel):
    title: str = "Chat with us"
    subtitle: str = "We typically reply instantly"
    greeting: str | None = "Hi! How can I help you today?"   # shown as first assistant message without an LLM call
    placeholder_text: str = "Type your message…"
    launcher_position: Literal["bottom-right","bottom-left"] = "bottom-right"
    launcher_icon: Literal["chat","phone","sparkle"] = "chat"
    avatar_url: AnyHttpUrl | None = None
    theme: WidgetTheme = WidgetTheme()
    open_on_load: bool = False
    show_branding: bool = True              # "Powered by VoiceAgents"; false requires plan feature `white_label_widget`
    collect_contact: Literal["off","optional","required"] = "off"   # pre-chat form for name/email/phone
    suggested_replies: list[str] = []       # ≤ 6 quick-reply chips before first user message
    custom_components: list[CustomComponent] = []
    language: str = "en"
    voice_button_label: str = "Talk to us"
    max_session_minutes: int = 30           # voice session cap 1–60
    persist_threads_days: int = 30          # visitor sees previous thread for this long
    privacy_notice: str | None = None       # shown under input; required non-empty when voice_enabled (recording/AI notice)
```

## 4. Embed script (`apps/widget`)

- Build output: single IIFE `widget.js` (≤ 60 KB gzip, enforced by a size-limit test) + `audio-worklet.js`, served by `api` at
  `/widget/v1/widget.js` and `/widget/v1/audio-worklet.js` with `Cache-Control: public, max-age=300` and SRI hashes listed
  in `embed_code`.
- Embed: `<script src="https://{PUBLIC_API_BASE_URL}/widget/v1/widget.js" data-widget-key="wgt_…" async></script>`
  optionally `data-user-token="<host-signed JWT>"` (§5.3) and `data-open="true"`.
- Renders inside a Shadow DOM root attached to a `<div id="va-widget-root">` appended to `body` (style isolation); CSS
  variables from theme; respects `prefers-color-scheme` in `auto`, `prefers-reduced-motion`.
- Components: launcher button (accessible name = title), panel (header with title/subtitle/close, message list with
  `aria-live="polite"`, quick replies, input with send button, voice button when enabled), pre-chat form, custom components
  renderer (buttons → sends the button value as a user message; card; form → submits JSON as a user message with
  `metadata.form`; link; image).
- JS API on `window.VoiceAgentsWidget`: `open()`, `close()`, `toggle()`, `startVoice()`, `endVoice()`, `sendMessage(text)`,
  `identify({name?, email?, phone?, external_id?, user_token?})`, `on(event, handler)` with events `ready`, `open`, `close`,
  `message`, `voice_start`, `voice_end`, `error`.
- Accessibility (WCAG 2.2 AA): full keyboard operation (Tab order, Esc closes, Enter sends, Shift+Enter newline), focus trap
  while open with focus return to launcher, contrast ≥ 4.5:1 validated at build for default theme and at config-save time
  for custom colors (API rejects with `INVALID_INPUT` field `config.theme.text_color` when contrast < 4.5), screen-reader
  labels for all controls, visible focus rings, voice mode shows live captions of both sides.

> Decision (2026-10-25): phase 00 ships the embed foundation only: `init({widgetKey, userToken?, open?})` (validates
> the `wgt_` prefix, throws `WidgetConfigError`; idempotent per key, a different key remounts), auto-init from
> `document.currentScript` `data-*` attributes (deferred to `DOMContentLoaded` while the document is loading; failures
> dispatch a `va-widget:error` `CustomEvent` on `window` instead of logging), the Shadow DOM host, and the launcher with
> its open state (`aria-expanded`, `data-open` on the host). The IIFE global `window.VoiceAgentsWidget` exposes the
> module exports; the panel, the rest of the JS API, and `audio-worklet.js` arrive with the widget phase. The size gate
> is `apps/widget/scripts/check-size.mjs`, run by `pnpm build` (fails above 61 440 bytes gzipped).

## 5. Public protocol

### 5.1 `POST /widget/v1/session`

Request headers: `Origin` (browser-set). Body `{"public_key":"wgt_…","visitor_id":"v_…"|null,"user_token":"…"?,"page_url":"…"}`.

1. Find widget by `public_key` (cached 60 s in Redis); deleted/missing → 404.
2. Origin check: parse `Origin` hostname; allowed if it equals an `allowed_domains` entry or matches a `*.` wildcard
   (suffix match on label boundary). Fail → 403 `WIDGET_DOMAIN_NOT_ALLOWED`. CORS response headers echo the allowed Origin
   only (`Access-Control-Allow-Origin: <origin>`, `Vary: Origin`).
3. Assign `visitor_id` if null (`"v_" + base62(16)`).
4. Issue session JWT (HS256 with `SECRET_KEY`-derived key `HKDF(SECRET_KEY, "widget")`), claims
   `{sub: visitor_id, wid: widget_id, org: org_id, origin, exp: now+3600, iat, jti, cid?: contact_id}`.
5. Response `{"token":"…","visitor_id":"v_…","expires_at":"…","config":{public subset of WidgetConfig},
   "voice_enabled":bool,"ws_url":"wss://{WIDGET_PUBLIC_HOST}/ws/widget","thread":{"id":…,"messages":[last 50]}|null}`.

### 5.2 Text chat

Primary: WebSocket `wss://{WIDGET_PUBLIC_HOST}/ws/widget?token=<jwt>` (media-worker). Server re-checks `Origin` header
against the JWT `origin` claim and the widget allowlist. JSON text frames:

Client → server:
- `{"type":"message","id":"<client uuid>","text":"…","metadata":{…}?}`
- `{"type":"typing"}`
- `{"type":"voice_start","sample_rate":16000}` / `{"type":"voice_end"}`
- `{"type":"ping"}`

Server → client:
- `{"type":"ack","id":"<client uuid>","message_id":"…"}`
- `{"type":"assistant_delta","message_id":"…","delta":"…"}` (streamed tokens) then `{"type":"assistant_message","message_id":"…","text":"…","component":{…}?}`
- `{"type":"human_message","message_id":"…","text":"…","agent_name":"…"}`
- `{"type":"status","status":"human_takeover"|"active"|"closed"}`
- `{"type":"transcript","role":"user"|"assistant","text":"…","final":bool}` (voice captions)
- `{"type":"error","code":"WIDGET_RATE_LIMITED"|"…","message":"…"}`
- `{"type":"pong"}`

Fallback (when WebSocket blocked): `POST /widget/v1/messages` (Bearer session token) body `{"text","metadata"?}` →
`{"message_id","reply":{"message_id","text","component"?}}` (non-streamed) and `GET /widget/v1/messages?after=<id>` long-poll
(25 s) for human-agent messages.

Rate limit: per visitor token bucket `widget:rl:{widget_id}:{visitor}` capacity = `messages_per_minute`, refill
`messages_per_minute/60` per second; exceeded → error frame / 429 `WIDGET_RATE_LIMITED`. Per widget global limit
`messages_per_minute × 100` per minute (`widget:rl:{widget_id}:_all`) to bound abuse. Max message length 2 000 chars.

### 5.3 Visitor identification

Host pages may pass `user_token`: a JWT signed (HS256) by the customer with the widget's **identity secret** (shown once
in dashboard, stored encrypted in `widgets.config.identity_secret_enc`; rotate via `POST /v1/widget/{id}/rotate_identity_secret`),
claims `{external_id?, email?, phone?, name?, exp}`. Valid token → contact resolved/created (`contacts`), thread linked to
contact, memory enabled per runner config. Unsigned `identify()` data is stored as thread metadata only (never used to
load memory) to prevent impersonation.

### 5.4 Voice mode

1. Requires `voice_enabled`. Client calls `getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}})`.
2. `AudioContext({sampleRate:16000})` when supported; otherwise native rate with resampling inside the worklet (linear
   interpolation + low-pass FIR, 31 taps) to 16 kHz.
3. `audio-worklet.js` emits 320-sample (20 ms) Int16 little-endian frames → sent as **binary** WS frames.
4. Server sends assistant audio as binary frames of PCM16 LE 16 kHz mono, 20 ms each (ElevenLabs TTS `output_format=pcm_16000`
   for widget sessions); client plays through an `AudioWorkletNode` jitter buffer (target 60 ms, max 300 ms).
5. Barge-in: identical pipeline logic to phone (`07-media-pipeline.md`); on interrupt the server sends
   `{"type":"clear"}` and the client flushes its playback buffer immediately. Client also reports playback position every
   200 ms `{"type":"played","ms":<total ms played>}` so the server's playback tracker knows exactly what was heard.
6. STT for widget voice uses ElevenLabs Scribe with `audio_format=pcm_16000`.
7. Session ends on `voice_end`, `max_session_minutes`, WebSocket close, or End Call node; a `calls` row with
   `direction='web'` is created per voice session (linked to the thread via `calls.metadata.widget_thread_id`) so voice sessions
   appear in call logs, get post-call analysis, and are billed as connected minutes.
8. Microphone permission denied → the widget shows the inline notice "Microphone access is needed for voice chat." (no
   server round-trip) and the session stays in text mode.

## 6. Human takeover

- Dashboard operators see live threads (`25-dashboard.md`), can send messages (`…/message` endpoint) which push
  `human_message` frames to the visitor through Redis channel `widget:thread:{thread_id}` subscribed by the owning media-worker
  socket handler.
- While `human_takeover`, visitor messages are stored and forwarded to the dashboard (SSE) and `webhook_url`
  (`conversation.message`), no AI reply. Voice sessions cannot start during takeover (button disabled with status text).
- Engine-triggered takeover: a Transfer Call node in widget text mode sets `human_takeover` and posts the node's
  `widget_transfer_message` (default "Connecting you with a team member…"), and emits `conversation.human_takeover`.

## 7. Billing

- Each assistant (AI) text message: usage_event `widget_message` qty 1 at plan `widget_message_micros` ($0.01),
  idempotency `wmsg:{message_id}`. Human messages and greetings are free.
- Voice sessions: billed as calls (`call_connected_seconds`) at the plan connected rate.

## 8. Security

- JWT validated on every WS connect and HTTP call; `jti` revocation list in Redis on widget delete/key rotation.
- CSP guidance in docs: hosts must allow `script-src {api host}`, `connect-src {api host} wss://{WIDGET_PUBLIC_HOST}`,
  and nothing else (the worklet is loaded from our origin by absolute, CORS-enabled URL, so no `blob:` worker source is required).
- All text rendered with `textContent` (no HTML injection); links in assistant messages rendered via a safe linkifier
  allowing only `http(s)` schemes, `rel="noopener noreferrer"`, `target="_blank"`.

## 9. Tests required

- Management API CRUD, runner validation, contrast validation, embed code SRI correctness.
- Origin matching table tests (exact, wildcard, port ignored, scheme ignored, `evil-example.com` vs `*.example.com`).
- Session JWT claims, expiry, revocation; WS origin mismatch rejection.
- Rate limiting per visitor and per widget; message length.
- Text flow with fake LLM: streamed deltas then final message; custom component validated against its schema.
- Human takeover end-to-end through Redis channel; release resumes AI.
- Voice: WS binary framing, server-side clear on barge-in, `played` position used in truncation, call row created and billed.
- Widget bundle: Vitest unit tests for state machine and audio worklet resampler (sine test: 48 kHz 1 kHz tone → 16 kHz,
  error < 1 %); size-limit test; Playwright e2e on a fixture host page served at `http://localhost:4173` with a fake
  microphone (`--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=tests/fixtures/hello.wav`) completing a
  voice exchange against the simulator stack; axe-core accessibility scan with zero violations.

## 10. Acceptance criteria

1. Embedding the script on an allowed domain yields a working chat that runs the configured pathway; on a disallowed domain
   the widget does not render and logs one console warning in the host page (allowed in widget source only for this case,
   exempted in the placeholder checker by the marker comment `// va-allow-console`).
2. Voice mode round-trip (user speech end → first assistant audio) ≤ 1 000 ms p50 in the simulator Playwright test.
3. Zero axe-core violations; full keyboard operability verified by Playwright script.
