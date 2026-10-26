# 25 — Dashboard (Next.js)

Location: `apps/dashboard`. Next.js 15 App Router, React 19, TypeScript strict, Tailwind CSS 4, shadcn/ui
(Radix primitives), TanStack Query 5, `@xyflow/react` 12, `elkjs` 0.9, wavesurfer.js 7 (+ regions & timeline
plugins), Recharts 2 for charts, `react-hook-form` + zod, `cmdk` command palette, `sonner` toasts,
`date-fns-tz`, `papaparse` (CSV), `openapi-fetch` + generated `schema.d.ts`, `zustand` (editor-local state only),
Vitest + Testing Library, Playwright.

The dashboard calls only the public `api` (`/v1`, `/v2`, `/numbers`, `/internal/*`) with session cookie + CSRF.
No business logic lives only in the dashboard: every action is an API call that also works with an API key.

## 1. Route map (complete)

| Route | Page | Min role (from `04` §3.2) |
|---|---|---|
| `/login` | Email/password login, SSO button ("Continue with SSO" → email → `/internal/auth/sso/start`) | public |
| `/signup` | Signup form (name, email, password, org name) with password strength meter | public |
| `/verify-email/[token]` | Verifies and redirects to `/onboarding` | public |
| `/forgot-password`, `/reset-password/[token]` | Password reset | public |
| `/mfa` | TOTP / recovery code entry | session |
| `/invite/[token]` | Accept invitation | public |
| `/agent-onboarding/[token]` | Approve agent key request (`04` §10) | session |
| `/onboarding` | Wizard (§3) | owner/admin |
| `/` | Home (§4) | viewer |
| `/calls` | Call list (§5.1) | viewer |
| `/calls/[callId]` | Call detail (§5.2) | viewer |
| `/calls/live` | Live calls (§6) | viewer (actions developer+) |
| `/calls/send` | Send Call form (§7) | developer |
| `/pathways` | Pathways list with folders (§8.1) | viewer |
| `/pathways/[pathwayId]` | Pathway editor (§8.2) | viewer (edit developer+) |
| `/pathways/[pathwayId]/tests` | Unit tests + simulations tabs (§8.6) | viewer |
| `/pathways/[pathwayId]/versions` | Version history & diff (§8.5) | viewer |
| `/personas`, `/personas/[personaId]` | Personas (§9) | viewer |
| `/agents`, `/agents/[agentId]` | Agents, versions, branches, env pins (§10) | viewer |
| `/agents/[agentId]/experiments/[experimentId]` | Experiment results (§10.3) | viewer |
| `/tools`, `/tools/new`, `/tools/[toolId]` | Tools (§11) | viewer |
| `/knowledge`, `/knowledge/[kbId]` | Knowledge bases (§12) | viewer |
| `/voices`, `/voices/clone`, `/voices/[voiceId]` | Voice library & cloning (§13) | viewer |
| `/numbers`, `/numbers/buy`, `/numbers/[numberId]` | Phone numbers (§14) | viewer |
| `/numbers/pools`, `/numbers/pools/[poolId]` | Number pools | viewer |
| `/numbers/telnyx-accounts` | BYO Telnyx accounts | admin |
| `/batches`, `/batches/new`, `/batches/[batchId]` | Batches (§15) | viewer |
| `/sms`, `/sms/[conversationId]` | SMS inbox (§16) | viewer |
| `/sms/registration` | 10DLC brand & campaigns | admin |
| `/widgets`, `/widgets/[widgetId]` | Widgets (§17) | viewer |
| `/memory`, `/memory/contacts/[contactId]` | Contacts & memory | viewer |
| `/analysis/dispositions`, `/analysis/citations`, `/analysis/evals`, `/analysis/evals/[setupId]` | Analysis configuration | viewer |
| `/analytics` | Analytics (§18) | viewer |
| `/webhooks`, `/webhooks/deliveries` | Webhooks (§19) | viewer |
| `/billing`, `/billing/usage`, `/billing/invoices`, `/billing/plans` | Billing (§20) | billing |
| `/settings/general`, `/settings/members`, `/settings/api-keys`, `/settings/security` (SSO, MFA, IP allowlist), `/settings/compliance` (calling hours, guard rail defaults, AI/recording disclosure, opt-out, PII redaction, HIPAA), `/settings/dnc`, `/settings/retention`, `/settings/llm`, `/settings/sub-accounts`, `/settings/profile` | Settings (§21) | per section |
| `/audit-log` | Audit log (§22) | admin |
| `/admin/*` | Platform admin (orgs, plans, BAA flag, suspensions) | platform admin |

Unauthorized navigation renders a 403 page; menu items the role cannot use are hidden.

## 2. Layout and navigation

- Left sidebar (collapsible, 240 px / 64 px): org switcher (memberships + sub-accounts with indent), sections:
  **Build** (Pathways, Personas, Agents, Tools, Knowledge, Voices), **Deploy** (Phone numbers, Batches, Widgets, SMS),
  **Monitor** (Calls, Live, Analytics, Analysis), **Configure** (Webhooks, Billing, Settings, Audit log).
- Top bar: global search / command palette (`⌘K`: jump to any page, search calls by id/phone/external_id, pathways
  by name), "Send call" button, credit balance chip (red below `low_balance_threshold`), environment badge (Test key
  mode banner when the org has only test keys), docs link, user menu (profile, theme light/dark/system, logout).
- Breadcrumbs on every detail page. Page titles via `generateMetadata`.

## 3. Onboarding wizard

Progress is persisted in `localStorage` key `va.onboarding.{orgId}`; completion is stored server-side via
`PATCH /v1/org` setting `settings.onboarding_completed_at` (`04` §7). Steps:
1. Use case selection (inbound receptionist, outbound sales, appointment reminders, custom) → seeds a template
   pathway via `POST /v1/pathway` with template JSON bundled in `src/templates/*.json`.
2. Voice pick (5 curated voices with previews).
3. Phone number: buy (area code search) or skip.
4. Test call: enter own phone number → `POST /v1/calls` with the template pathway; live transcript panel.
5. Get API key: creates key, shows once with copy button and curl / Python / TS snippets.

## 4. Home

- KPI cards (selectable range today / 7 d / 30 d): calls, connected minutes, answer rate, avg duration,
  transfer rate, spend, p50/p95 E2E latency. Source: `GET /v1/analytics/overview?from=&to=`.
- Charts: calls per day stacked by status; minutes per day; disposition distribution (donut).
- Recent calls table (10), active calls count with link to Live, low-balance alert, failing webhook alert
  (dead deliveries in 24 h), pending 10DLC alert.

## 5. Calls

### 5.1 List

- Server-side filtering via `GET /v1/calls` (legacy params) and extended params: `status`, `direction`,
  `answered_by`, `disposition_tag`, `pathway_id`, `agent_id`, `batch_id`, `from`, `to`, `external_id`,
  `start_date`, `end_date`, `duration_gt`, `duration_lt`, `has_recording`, `q` (full-text on transcript, admin+).
- Columns (user-configurable, persisted in localStorage): created, direction icon, to, from, status badge,
  answered_by, duration, disposition, pathway, agent version, cost, latency p50. Row click → detail.
- Bulk actions: export CSV (`GET /v1/calls/export?…` streamed), analyze selected (opens analyze dialog with goal and questions → the batch analyze endpoint defined in
`05-calls-api.md`).
- URL holds filter state (`useSearchParams`) so links are shareable. Infinite scroll with cursor.

### 5.2 Detail

Header: to/from, status, direction, timestamps, duration, cost, end reason, ended by, buttons: Stop (if active),
Download recording, Copy call id, Re-run analysis, Create unit test from turn, Open in pathway editor.

Tabs:
1. **Conversation**: wavesurfer dual-channel (split channels: caller top, agent bottom) using `recording_url`
   (WAV, 2 channels). Transcript list synced: clicking a turn seeks audio to `start_ms`; playback highlights the active
   turn; interrupted assistant turns show the spoken portion bold and the unspoken remainder struck-through grey.
   Playback speed 0.75/1/1.25/1.5/2.
2. **Pathway timeline**: vertical timeline of `pathway_logs` — node chip (name/color), route decision with
   alternatives and scores, loop-condition scores, extracted variables diff, tool/webhook calls with request/response
   (collapsible JSON viewer), guard-rail evaluations.
3. **Latency**: per-turn stacked bar (eot, stt_final, llm_ttft, tts_ttfa) with p50/p95 lines; table.
4. **Events**: raw `call_events` table with category filter, level colouring, JSON expand; live via SSE when active.
5. **Analysis**: summary, disposition, analysis schema results, citations (each citation shows value and cited
   utterances with play-from buttons), evals (criteria scores, rationale, pass/fail).
6. **Variables & metadata**: request_data, metadata, final variables, dynamic data results.
7. **Webhooks**: deliveries for this call with status, attempts, replay.
8. **Legs**: call legs (primary, transfer, warm proxy) with Telnyx ids, hangup causes, AMD result.

## 6. Live calls

- `GET /v1/calls/active` polled every 3 s + SSE `/v1/events/stream?types=call.*` to update rows.
- Selecting a call opens a side panel with real-time transcript (`WS /v1/calls/{id}/transcript/ws`), current node,
  latency of last turn, and controls: **Stop** (`POST /v1/calls/{id}/stop`), **Transfer** (dialog: number, cold/warm →
  `POST /v1/calls/{id}/transfer`), **Say** (inject an utterance → `POST /v1/calls/{id}/say`), **Take over** (for widget
  and SMS conversations; for phone calls takeover = warm transfer to the user's configured phone). Actions per
  `05-calls-api.md` live-control endpoints.

## 7. Send Call form

React-hook-form + zod schema generated from OpenAPI `SendCallRequest`. Groups (accordions), every field present:
- **Basics**: phone_number, from (select owned numbers), runner selector (Task prompt | Pathway + version | Persona +
  version | Agent + version/branch), task (textarea with char counter, warns > 2 000), first_sentence, voice (picker with
  preview), language, wait_for_greeting.
- **Model & behaviour**: model, temperature (slider), interruptibility (0–4 segmented), interruption_threshold
  (legacy), block_interruptions, pronunciation_guide (table editor: word, pronunciation, case_sensitive, spaced),
  keywords (tag input `word:boost`), noise_cancellation, background_track.
- **Dispatch**: dialing_strategy (local / pool select), timezone, start_time (datetime picker in chosen tz),
  max_duration, transfer_phone_number, transfer_list (key/value editor), precall_dtmf_sequence, ignore_button_press.
- **Voicemail**: ivr_mode/amd, voicemail.message, voicemail.action, voicemail.sms {message, from}, voicemail.sensitive,
  retry {wait, voicemail_action, voicemail_message}.
- **Knowledge & tools**: tools multi-select (TL-/KB- and inline tool JSON editor), dynamic_data editor (list of request
  cards with response_data rows), memory_id.
- **Analysis**: summary_prompt, dispositions (tag input + org defaults), analysis_schema (JSON editor), citation_schema_ids,
  post_call_evals, record.
- **Compliance**: guard_rails list editor (type, config, action).
- **Advanced**: request_data (JSON editor with variable hints), metadata, external_id, webhook, webhook_events
  (checkbox group), stream_post_transfer_transcript, hd_audio.
- Right pane: live JSON preview of the request body + "Copy as curl/Python/TypeScript". Submit → toast with call id →
  navigate to call detail.

## 8. Pathways

### 8.1 List

Folder tree (drag-drop pathways into folders, `PATCH /v1/pathway/{id}` folder_id), table (name, production version,
staging version, draft edited at, calls last 7 d), actions: New (blank or from template), Duplicate, Export JSON,
Import JSON, Delete (confirm by typing name).

### 8.2 Editor canvas

- `@xyflow/react` canvas with custom node components per type, custom edge component showing the label pill
  (condition text, truncated 60 chars, tooltip full).
- **Node palette** (left, draggable): Default, Wait for Response, Knowledge Base, Webhook, Transfer Call, End Call,
  SMS, Custom Code, Press Button, Transfer Pathway, Scheduling, and "Global node" toggle on any node. Exactly the node
  types of `11-pathways-engine.md` §3.
- Node visual: header colour by type, icon, name, badges: START (green), GLOBAL (purple globe), variables count,
  condition (loop icon), KB, tool. Validation errors as red ring with tooltip.
- **Start node**: exactly one; set via node menu "Set as start".
- Edge creation by dragging handles; selecting an edge opens the edge panel (label/condition text, description,
  `conditionExamples` list, priority/order, "else" fallback flag) per `11` edge schema.
- **Side panel** (right, 420 px, resizable) for the selected node, with fields per type exactly as in
  `11-pathways-engine.md` §3: common (name, type, prompt vs static text toggle, condition, variables extraction table
  {name, type string|integer|boolean, description}, interruptibility 0–3, resumption_speed 1–3, interruption threshold,
  model options temperature, dialogue/pathway/condition examples editors, global flag + global label + "enable
  forwarding" target, pronunciation overrides, voice override), plus type-specific fields: Webhook (url, method,
  headers with secret toggle, body JSON with `{{var}}` autocomplete, response_data JSONPath mapping, speech, timeout,
  retries, route-by-status edges), Knowledge Base (KB select, top_k, prompt), Transfer Call (number, transfer type
  cold/warm, speech/speech_mode/speech_prompt, warm briefing prompt), End Call (speech/speech_mode/speech_prompt),
  SMS (to {{from}} default, message template, from number), Custom Code (Monaco editor TypeScript/JavaScript, input
  vars, output mapping, allowed hosts, timeout, "Run with sample input" button calling the sandbox test endpoint),
  Press Button (digits sequence `0-9*#w`), Transfer Pathway (pathway select + version + start node), Scheduling
  (calendar webhook config per `11`).
- **Global settings drawer**: global prompt ("Add global prompt to all nodes"), default voice, language, memory
  enabled, tools available globally, guard rails, max duration, background track.
- **Variable autocomplete**: typing `{{` in any templated field lists built-ins, request_data keys from the test panel,
  extracted variables defined upstream, webhook response vars.
- **Auto-layout** button: elkjs `layered` algorithm, direction DOWN, `elk.spacing.nodeNode=60`,
  `elk.layered.spacing.nodeNodeBetweenLayers=90`; global nodes placed in a separate lane on the right.
- Minimap, zoom controls, fit view, snap-to-grid 16 px, multi-select (shift-drag), copy/paste (`⌘C/⌘V`, pastes with new
  node ids and edges among copied nodes preserved; also pasteable across pathways via clipboard JSON
  `application/x-va-nodes`), delete, duplicate (`⌘D`), undo/redo (`⌘Z/⇧⌘Z`, zustand history stack depth 100,
  coalescing drags), search nodes (`⌘F`).
- **Autosave**: debounced 1 500 ms `PUT /v1/pathway/{id}/draft` with `If-Match: <graph_hash>`; on 409 show conflict
  dialog (reload theirs / overwrite / export mine). Save status indicator.

### 8.3 Version switcher, publish, promote

- Header dropdown: Draft (editable), Staging vN, Production vN, all published versions (read-only view).
- **Publish** dialog: version label, changelog, checkbox "Promote to production" / "Promote to staging", run
  regression tests before publishing (toggle, default on when tests exist) → `POST /v1/pathway/{id}/publish`.
- **Promote** dialog on a published version: env select → `POST /v1/pathway/{id}/promote`.
- Diff view (`/versions`): side-by-side node list diff (added/removed/changed fields) and canvas overlay highlighting.

### 8.4 Validation panel

Bottom drawer listing errors/warnings from `POST /v1/pathway/{id}/validate` (client also runs the same zod checks
instantly): no start node, unreachable nodes, dead-end non-terminal nodes, edges without labels, undefined variables
referenced, transfer without number, webhook without URL, custom code syntax errors, global node without label,
duplicate node names. Clicking an item focuses the node.

### 8.5 Test panel

Drawer (right, tabbed) on the editor:
- **Text chat**: `POST /v1/pathway/{id}/test/chat` session (version selectable, request_data JSON, start node select).
  Messages show; each assistant turn has an expandable log: current node, variable extraction result, loop condition
  score ("Likely 7/10"), chosen route with alternatives, global node checks, webhook/tool request & response, latency
  of each LLM call. "Branch from here" restarts the session from a past turn. "Save as unit test" on any user turn.
- **Voice (browser)**: uses the widget voice WebSocket transport (`22-web-widget.md`) with a dashboard-issued test
  token (`POST /v1/pathway/{id}/test/voice-token`): mic capture via AudioWorklet (16 kHz PCM), playback, live transcript,
  same per-turn logs via SSE.
- **Phone**: enter phone number → `POST /v1/calls` with `pathway_id` and the current draft version number (the API
  accepts draft versions only on calls flagged `test_call: true`, as defined in `05-calls-api.md`), shows live transcript.
- Active node highlighting on the canvas during any test session.

### 8.6 Unit tests & simulations tab

- Unit tests table (name, node, expected route/vars, last result); create/edit dialog (history editor, user message,
  expected route node, expected variables, grading goal, simulated variations 0–20); run one/all →
  `POST /v1/pathway/{id}/tests/run`; results with per-variation pass/fail + rationale.
- Simulations: define personas (caller goal, persona traits, max turns) → `POST /v1/pathway/{id}/simulations`; results
  list with transcript and grading.

## 9. Personas

List; detail with version selector, form for `PersonaConfig` fields (`20-memory-personas-agents.md` §3): name,
description, voice, language, prompt or pathway routing, tools, KBs, memory policy, guard rails, call settings;
publish draft; usage (numbers, widgets referencing it).

## 10. Agents

### 10.1 Agent detail

Versions table (semver, branch, pathway+version, created by, changelog), "New version" dialog (bump major/minor/patch
or prerelease, branch select/create 1–64 chars, pathway version, call_settings JSON form), env pins card
(production/staging/dev selects → `PUT /v1/agents/{id}/env_pins`), branch filter.

### 10.2 Experiments list

Create experiment (name, primary metric, arms: version + weight, weights must sum to 100), start/stop.

### 10.3 Experiment results

Per arm: calls, primary metric value with 95 % CI, secondary metrics (avg duration, transfer rate, avg eval score,
disposition breakdown), significance indicator (two-proportion z-test or Welch t-test results returned by
`GET /v1/experiments/{id}/results`), daily trend chart.

## 11. Tools

Form for all `tools` fields (name validation `^[A-Za-z][A-Za-z0-9_]{0,63}$`, description, speech, url, method,
headers (+ secret headers masked after save), query, body JSON with `{{input.x}}` hints, input_schema visual builder
(properties: name, type, description, required, enum) + raw JSON toggle, response map rows (var name, JSONPath),
timeout, retries). **Test run** panel: input JSON → `POST /v1/tools/{id}/test` → shows request sent, status, response
body, extracted vars, duration.

## 12. Knowledge bases

List (name, status badge, documents, chunks, updated). Detail: documents table (type, name, status, tokens, error);
add document dialog: upload (drag-drop multiple files ≤ 50 MB each; pdf, txt, md, doc, docx, html, csv), add URL
(single page) or crawl (sitemap discovery → checklist of URLs → submit), add text. Polls status every 3 s while
PROCESSING. **Chat with KB** pane: question → `POST /v1/knowledge/{id}/chat` → answer with source chunks (doc name,
page/url, score). Search pane showing raw retrieval results.

## 13. Voices

- Library grid with filters (gender, accent, age, language, category premade/cloned/shared, tags, rating) and search;
  card: name, tags, play preview (audio element, one at a time), rating stars, "Use in…" menu, favourites.
- Detail: settings sliders (stability, similarity_boost, style, speed) + speaker boost toggle with "Test" textbox →
  `POST /v1/voices/{id}/preview` playing generated audio; rename; delete (clones only); samples list.
- **Clone** flow: name, description, labels; sample input via upload (mp3/wav/m4a/webm, total 10 s–10 min) or in-browser
  recorder (MediaRecorder webm/opus, shows elapsed time and level meter, script prompt text to read); remove background
  noise toggle; consent checkbox ("I have the rights and consent to clone this voice") required; submit → clone status
  polling → ready toast. Enforces plan clone cap message (`VOICE_CLONE_LIMIT`).

## 14. Phone numbers

- List: number, label, capabilities, inbound runner summary, monthly cost, status.
- Buy: country, area code, contains-digits, features (voice/sms) → `GET /v1/numbers/available` results → select →
  purchase confirmation with monthly cost → `POST /numbers/purchase`.
- Number detail: label edit, **Inbound configuration form** with the same field groups as §7 (minus phone_number /
  dispatch scheduling) plus fallback_number, maintenance window, webhook, memory settings; SMS configuration
  (`21-messaging-sms.md` §3); release number (confirm).
- Pools: CRUD, member numbers multi-select, strategy, per-number daily cap, usage.
- BYO Telnyx accounts: add (label, API key, public key) → validation status; shows `encrypted_key` handle once with
  copy; import numbers from that account.

## 15. Batches

- New batch wizard: (1) upload CSV (papaparse, preview 20 rows, max 100 000 rows / 50 MB); (2) column mapping —
  `phone_number` required, other columns mapped to `request_data.<key>`, `metadata.<key>`, or Send Call fields
  (`first_sentence`, `voice`, `external_id`, `from`, `start_time`); (3) global properties = the §7 form; (4) schedule
  (start now / start at, calling windows per weekday, timezone source: per-row column or global, max concurrency,
  retry policy) per `17-batches-and-scheduling.md`; (5) review (estimated cost range, DNC/invalid count from
  `POST /v2/batches/validate`) → create.
- Batch detail: progress bars (dispatched/completed/successful/failed), live updates via SSE, items table with status
  and links to calls, logs tab (`GET /v2/batches/{id}/logs`), Stop button, export results CSV.

## 16. SMS inbox

Two-pane inbox: conversation list (unread dot, last message, channel number, status), thread view with messages
(delivery status ticks, errors), composer (only in `human_takeover` state, or "Send as agent"), Take over / Return to
AI buttons, conversation variables/node side card. Start conversation dialog (user number, agent number, runner,
objective).

## 17. Widgets

Form: name, runner (pathway/agent/persona/prompt), allowed domains list, messages/minute, voice enabled + voice,
appearance config (`22-web-widget.md` §3: colours, position, launcher icon, greeting, avatar, placeholder text, font),
webhook url. Live preview iframe (`/widget-preview/[widgetId]` route rendering the real widget bundle against the
test API). Embed snippet tab with copy. Threads tab (conversations with transcript, takeover).

## 18. Analytics

Range picker, filters (pathway, agent, direction, batch, phone number). Panels backed by `/v1/analytics/*`
(`27-observability.md` §7): volume over time, answer rate, avg/median duration, E2E latency p50/p95 over time,
stage latency breakdown, interruptions per call, disposition distribution, end reasons, transfer rate, cost over time,
top nodes by visits and drop-off (pathway funnel), voicemail rate, eval scores trend. Export PNG/CSV per panel.

## 19. Webhooks

Endpoints CRUD (url, event types multi-select from catalog `19` §2, enabled), "Send test event", signing secret
(last4, rotate with confirmation showing new secret once). Deliveries table (filters status/type/call), detail drawer
(request headers/body, response status/body excerpt, attempts timeline), Replay.

## 20. Billing

Balance card (credits, credit limit), top-up (preset $25/$50/$100/$500 + custom → Stripe Checkout redirect),
auto-recharge form (enabled, threshold, amount), current plan + plan comparison table (from `GET /v1/billing/plans`)
with upgrade/downgrade (Stripe), usage page (charts by meter, table by day, export CSV), invoices list (download PDF),
ledger table (entry type, amount, balance after, reference link), sub-account credit transfer dialog.

## 21. Settings

- General: org name, timezone, default language/voice/from number, default webhook & events, max duration, record default.
- Members: table, invite dialog, role change, remove, pending invitations.
- API keys: list (prefix, scopes, env, last used), create dialog (name, scopes checklist grouped by resource, env,
  expiry) → show-once modal with copy + warning, revoke, roll.
- Security: MFA setup (QR, verify, recovery codes download as .txt), org MFA required toggle, SSO config form,
  IP allowlist, active sessions list with revoke.
- Compliance: calling hours editor (time range + weekday chips), enforce toggle, AI disclosure, recording disclosure,
  honor opt-out, default guard rails editor, HIPAA mode (read-only unless BAA signed), PII redaction mode & entities.
- DNC: list with search, add number, bulk import CSV, remove, source column.
- Retention: retention_days, recording_retention_days, data region (read-only after creation).
- LLM: per-org override (base URL, API key secret, models) with "Test connection".
- Sub-accounts: list, create (Bland-compatible fields), suspend/reactivate, switch into.
- Profile: name, email (verified), password change, theme.

## 22. Audit log

Table with filters (action, actor, resource, date range), change diff viewer (before/after JSON with redactions).

## 23. Cross-cutting implementation rules

- **Data fetching**: Server Components for initial page shells only; data via TanStack Query in client components
  (`staleTime` 30 s for config resources, 5 s for calls lists, `refetchOnWindowFocus` true). Query keys:
  `[resource, orgId, params]`. Mutations invalidate affected keys; optimistic updates for toggles and renames.
- **API client**: `src/lib/api/client.ts` creates `openapi-fetch` client with `credentials: "include"`, injects
  `X-CSRF-Token` from cookie, maps envelope/legacy errors to `ApiError {code, message, field?, requestId}`; 401 →
  redirect to `/login?next=`; `MFA_REQUIRED` → `/mfa`.
- **Generation**: `pnpm --filter dashboard gen:api` runs `openapi-typescript ../../openapi/openapi.json -o src/lib/api/schema.d.ts`;
  CI fails if the generated file differs.
- **Errors/toasts**: mutation errors toast `message` + request id (copy button); form field errors map `field` to the
  input; unexpected errors render route `error.tsx` with retry; `not-found.tsx` per section.
- **Live data**: `useEventSource(url)` hook with reconnect/backoff and `Last-Event-ID`.
- **Accessibility (WCAG 2.1 AA)**: all interactive elements keyboard reachable, visible focus rings, Radix
  primitives for dialogs/menus, aria-labels on icon buttons, colour contrast ≥ 4.5:1 (tokens checked by a unit test
  computing contrast ratios), canvas editor offers a keyboard-accessible "Outline view" listing nodes/edges with
  edit actions, transcripts as semantic lists, reduced-motion support. `@axe-core/playwright` runs on every e2e page.
- **Responsive**: ≥ 1280 px full layout; 768–1279 px sidebar collapsed to icons, side panels become overlays;
  < 768 px read-only mobile views for Home, Calls list/detail, Live calls, SMS inbox, Billing; editor shows
  "Use a larger screen to edit" with read-only canvas.
- **Design tokens** (`src/styles/tokens.css`, CSS variables, light/dark): `--background`, `--foreground`, `--muted`,
  `--muted-foreground`, `--card`, `--border`, `--primary` (indigo-600 / indigo-400 dark), `--primary-foreground`,
  `--success` (emerald-600), `--warning` (amber-500), `--danger` (rose-600), `--info` (sky-600), node type colours
  `--node-default`, `--node-wait`, `--node-kb`, `--node-webhook`, `--node-transfer`, `--node-end`, `--node-sms`,
  `--node-code`, `--node-button`, `--node-transfer-pathway`, `--node-scheduling`, `--node-global`; radius 8 px;
  font Inter (body) and JetBrains Mono (code); spacing scale 4 px base.
- **Component inventory** (`src/components`): AppShell, Sidebar, OrgSwitcher, CommandPalette, PageHeader, DataTable
  (sorting, column visibility, cursor pagination), FilterBar, StatusBadge, PhoneNumberText (formatted + masked),
  JsonEditor (CodeMirror 6 JSON mode with schema validation), JsonViewer, CodeEditor (Monaco, lazy-loaded), VoicePicker,
  AudioPlayer, DualChannelWaveform, TranscriptView, LatencyChart, KpiCard, TimeSeriesChart, DonutChart, KeyValueEditor,
  TagInput, VariableAutocompleteInput, CronlessScheduleEditor (calling windows), CsvMapper, ConfirmDialog
  (type-to-confirm variant), CopyButton, SecretReveal (show-once), EmptyState, ErrorState, Skeletons, PathwayCanvas,
  NodePalette, NodePanel (per type subcomponents), EdgePanel, ValidationDrawer, TestPanel, ExperimentResultsTable,
  SendCallForm, InboundConfigForm (shares field groups with SendCallForm), GuardRailEditor, PronunciationGuideEditor,
  DynamicDataEditor, ToolSchemaBuilder, Recorder.
- **Performance budgets**: first-load JS per route ≤ 250 KB gzipped (editor route ≤ 600 KB, Monaco and elkjs
  lazy-loaded in a web worker for elk), LCP ≤ 2.5 s on simulated 4G for Home and Calls list, canvas stays ≥ 50 fps with
  300 nodes (React Flow `onlyRenderVisibleElements`), call list renders 1 000 rows virtualized (`@tanstack/react-virtual`).
  Enforced by `next build` size check script and a Playwright performance test.
- **Security**: no secrets in client bundles; CSP from `28-security.md` §5; all user HTML rendered as text (no
  `dangerouslySetInnerHTML` except sanitized markdown via `rehype-sanitize`).
- **i18n**: English only in v1; all strings in `src/i18n/en.ts` for later localisation.

> Decision (2026-10-25): phase 00 scaffold choices. (a) `/status` is a client component (TanStack Query, refetch every
> 10 s) probing the api's unlisted `/healthz` and `/readyz` (bodies validated with zod against the `27-observability.md`
> §9 Decision; a 503 readiness body is data, not an error) and the public `GET /v1/health` through the `openapi-fetch`
> client; it renders loading, operational, degraded (failing checks listed), and unreachable (retry) states. The api
> origin comes from `NEXT_PUBLIC_API_BASE_URL` (`02-repo-layout-and-conventions.md` §6). (b) Until Home ships with auth,
> `/` renders the designed `not-found.tsx`. (c) Fonts are self-hosted with `@fontsource-variable/inter` and
> `@fontsource-variable/jetbrains-mono` (no network access during `next build`). (d) Token colours are written in OKLCH:
> the Tailwind 4 palette values for the hues named above and zinc neutrals. Themed tokens are
> `light-dark(<light>, <dark>)` with `:root { color-scheme: light dark }` (system preference) and `:root.light` /
> `:root.dark` forcing one scheme, so the dark values are declared once. `tests/tokens.test.ts` converts OKLCH to sRGB
> and requires ≥ 4.5:1 for foreground, muted-foreground, and primary text pairs in both themes. Status badges keep text in
> `--foreground` and show the status hue as a dot, so badge contrast never depends on the status colour.
> (e) `next-env.d.ts` is generated by Next (it references `.next/types`) and is git-ignored; `next build` skips its own ESLint
> pass (`eslint.ignoreDuringBuilds`) because `make lint` runs the shared flat config. (f) The CSRF middleware in
> `src/lib/api/client.ts` copies cookie `va_csrf` into `X-CSRF-Token` on unsafe methods (`04-auth-tenancy-rbac.md` §4.4);
> the 401/`MFA_REQUIRED` redirects arrive with authentication (phase 02).

## 24. Playwright e2e scenarios (run against compose `apps` profile with simulators)

1. Signup → verify email (mailpit API) → onboarding wizard → test call completes with live transcript.
2. Login with MFA enable/verify/logout/login with TOTP and with recovery code.
3. Create API key, copy once, revoke.
4. Build pathway from blank: add nodes of every type, connect edges, set start, validate shows zero errors, publish to
   production, verify version switcher.
5. Undo/redo/copy-paste/auto-layout preserve graph (assert via API GET draft).
6. Test panel text chat routes through 3 nodes and shows logs; save unit test; run tests → pass.
7. Send Call form with voicemail + analysis fields → call detail shows summary, disposition, recording plays, transcript seek.
8. Live calls: start long simulated call, stop it from Live page.
9. Create tool and test-run against simulator echo endpoint.
10. Knowledge base: upload PDF fixture → COMPLETED → chat answer cites source.
11. Clone voice via upload fixture → ready → preview plays.
12. Buy number (FakeTelnyx inventory) → configure inbound pathway → simulator inbound call answered.
13. Batch CSV upload with mapping → progress to completed → export.
14. SMS conversation takeover and reply.
15. Widget config → preview chat exchange → thread appears.
16. Webhook endpoint test event → delivery succeeded; replay.
17. Billing top-up via FakeStripe checkout → balance increases; auto-recharge setting saved.
18. Viewer role cannot see edit controls; developer cannot see Members invite; PII masked for viewer.
19. Sub-account create and switch; data isolation visible.
20. Accessibility: axe scan with zero serious/critical violations on every route visited above.

## 25. Tests required

Vitest unit tests for: API client error mapping, CSRF injection, pathway graph reducers (add/remove/connect/undo/redo/paste
id remapping), client-side validators (parity fixtures shared with backend `/validate` via JSON fixtures in
`apps/dashboard/test/fixtures/pathway-validation/*.json` consumed by both test suites), CSV mapper, schedule editor,
token contrast. Component tests for SendCallForm (all groups serialize to the expected request JSON snapshot),
NodePanel per node type, TranscriptView seek sync, SecretReveal show-once.

## 26. Acceptance criteria

- Every route in §1 exists, is role-gated, and is reachable via navigation.
- All 20 Playwright scenarios pass in CI; axe reports no serious/critical violations.
- Generated API types are current; bundle budgets pass.
- Every API capability in specs 04–24 has a corresponding UI (cross-checked by `apps/dashboard/test/coverage-map.test.ts`
  that lists every OpenAPI operationId as either used by the dashboard or in an explicit allowlist of API-only ops).
