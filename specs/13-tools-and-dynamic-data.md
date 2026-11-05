# 13 — Custom Tools, Dynamic Data, and Custom Code (`packages/va_tools`)

## 1. Package layout

```
packages/va_tools/src/va_tools/
├── models.py         # ToolDefinition, InlineTool, DynamicDataSpec, ResponseDataItem, ToolResult
├── render.py         # applies va_engine.templating to url/headers/query/body
├── http.py           # SafeHttpClient (SSRF guard, timeouts, size caps)
├── jsonpath.py       # JSONPath extraction wrapper over jsonpath-ng (extended parser)
├── runner.py         # ToolRunner: execute tool calls inside a turn
├── system_tools.py   # built-in tools (§6)
├── dynamic_data.py   # DynamicDataFetcher (§7)
├── secrets.py        # secret header encryption helpers (§3)
└── sandbox/
    ├── deno.py       # DenoSandbox (§8)
    └── harness.ts    # runtime harness copied to a temp dir per execution
```

## 2. Tools API (legacy envelope)

| Method | Path | Body / Query | Response |
|---|---|---|---|
| POST | `/v1/tools` | ToolCreate | `{"status":"success","tool_id":"TL-…","tool":{…}}` |
| GET | `/v1/tools` | `search?`, `limit≤100`, `cursor?` | `{"status":"success","tools":[Tool],"next_cursor":null}` |
| GET | `/v1/tools/{tool_id}` | — | `{"status":"success","tool":{…}}` |
| POST | `/v1/tools/{tool_id}` | ToolUpdate (any subset) | `{"status":"success","tool":{…}}` |
| DELETE | `/v1/tools/{tool_id}` | — | `{"status":"success","message":"Tool deleted."}` (409 if referenced by published pathway/persona/agent unless `?force=true`) |
| POST | `/v1/tools/{tool_id}/test` | `{"input": {…}, "variables": {…}}` | `{"status":"success","request":{url,method,headers(masked),body},"status_code":200,"response":…,"extracted":{…},"duration_ms":182}` |

`ToolCreate`:
```json
{
  "name": "check_availability",
  "description": "Checks table availability for a date, time and party size.",
  "speech": "Let me check that for you.",
  "url": "https://api.bellapasta.com/availability",
  "method": "POST",
  "headers": {"Content-Type": "application/json"},
  "secret_headers": {"Authorization": "Bearer sk_live_…"},
  "query": {},
  "body": {"date": "{{input.date}}", "time": "{{input.time}}", "party": "{{input.party_size}}", "caller": "{{from}}"},
  "input_schema": {
    "type": "object",
    "properties": {
      "date": {"type": "string", "description": "YYYY-MM-DD"},
      "time": {"type": "string", "description": "HH:MM 24h"},
      "party_size": {"type": "integer", "minimum": 1, "maximum": 20}
    },
    "required": ["date", "time", "party_size"]
  },
  "response": {"available": "$.available", "alternatives": "$.alternatives[*].time"},
  "timeout_ms": 8000,
  "retries": 1
}
```
- `response` maps to the `response_map` column. `secret_headers` are write-only: responses show
  `"secret_headers": {"Authorization": "••••last4"}`.
- Validation: `name` regex from 03; `url` must be `https://` (http allowed in local/test); `input_schema` must be a valid
  JSON Schema draft 2020-12 object schema with `type: "object"`; JSONPath expressions must parse; header names
  RFC 7230 tokens; body ≤ 64 KB JSON.

> Decision (2026-11-05) P08-T01: implemented in `va_api/routers/tools.py` over `va_api.services.tools`,
> `va_db.repositories.ToolRepo`, `va_tools.validation`, and `va_tools.secrets`.
> 1. Masking: a secret header value shows as `"••••" + last 4` only when it has at least 12 characters, else as
>    `"••••"` (the last 4 of a short secret would give most of it away). Audit rows record that `secret_headers`
>    changed but never a value (the key is redacted).
> 2. `tool_id` accepts `TL-<uuid>` or the bare uuid; responses always use `TL-`. Anything else is
>    `INVALID_INPUT` (field `tool_id`); unknown, deleted, and other orgs' tools are `TOOL_NOT_FOUND`.
> 3. ToolUpdate: omitted fields keep their value; `null` clears `speech`, `body`, and `secret_headers` and is
>    `INVALID_INPUT` for every other field; `secret_headers` replaces the whole stored set. The sent fields are
>    merged over the stored definition and the result is validated as a whole (field paths as in request
>    validation). An update that changes nothing writes nothing (no audit row, `updated_at` unchanged).
> 4. Validation adds to the ToolCreate field rules: reserved system tool names (§4, case-sensitive) on `name`;
>    `response` keys are variable names (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`) and values parse as JSONPath (field
>    `response.<var>`); secret header names are RFC 7230 tokens, may not repeat a plain `headers` name
>    (case-insensitive), and values are non-empty, at most 4 096 characters, without CR/LF. A URL whose host is
>    templated passes: the runtime SSRF guard checks the rendered URL.
> 5. Names are unique per org among live tools: the specs/03 `UNIQUE (org_id, name)` is not partial, so a
>    name held only by a soft-deleted tool is freed by deleting that row for good in the same transaction.
> 6. DELETE answers 409 `CONFLICT` with `details.references` = `[{type, id, version}]` (at most 20) when a
>    published pathway version (node `tools` or `global_config.globalTools`), a published persona version
>    (`config.tools`), or any version of a live agent (`call_settings.tools`) lists the tool in either id form.
>    `?force=true` is open to every `tools:write` principal (unlike pathways, deleting a tool loses no data
>    that cannot be recreated).
> 7. `POST /v1/tools/{tool_id}/test` ships with the tool runtime (specs/13 §5 Decision P08-T02a).

## 3. Secret headers

Stored in `tools.secret_headers_enc` as envelope-encrypted JSON (`va_common.crypto.encrypt_json(org_id, value)`,
AES-256-GCM data key wrapped by the KMS provider, AAD = `tool:{tool_id}`). Decrypted only inside `ToolRunner` at
execution time and never logged; `call.tool` event payloads show secret header values as `"••••"`.

## 4. Exposure to the dialogue LLM

Each available tool becomes an OpenAI-style function:
```json
{"type": "function", "function": {"name": "<tool.name>", "description": "<tool.description>", "parameters": <tool.input_schema>}}
```
Available tools at a turn = node `tools` ∪ `global_config.globalTools` ∪ call `tools` (task mode) ∪ allowed system
tools (§6). Name collisions: call-level inline tool overrides org tool with same name; system tool names are reserved
(`end_call`, `transfer_call`, `press_button`, `send_sms`, `schedule_callback`, `lookup_knowledge`, `book_slot`,
`list_slots`) and customer tools may not use them (validation error `INVALID_INPUT`).

`tools` array in Send Call may contain: `"TL-<uuid>"` strings, `"KB-<uuid>"` strings (enables `lookup_knowledge`
over those KBs and per-turn retrieval injection per `14-knowledge-bases.md` §7), or inline tool objects with the same
fields as ToolCreate (no `secret_headers`; inline `headers` values are treated as secret in logs).

## 5. Tool execution runtime (`ToolRunner.execute`)

Inputs: tool definition, LLM arguments (JSON), engine variables, call context, `EngineIO`.

1. Parse arguments JSON; validate against `input_schema` (`jsonschema` Draft202012Validator). Invalid → return a tool
   message to the LLM: `{"error":"invalid_arguments","details":[…]}` (the LLM can retry once; a second failure in the
   same turn makes the engine respond without the tool).
2. Speech-during-tool: if `speech` set, `io.say(render(speech), mark=f"tool:{call_id}")` immediately, concurrent with
   the request. If `speech` is empty and the request is still running after **1 200 ms**, play a cached filler phrase
   from the voice's phrase cache (`09-tts-elevenlabs.md`, phrases "One moment.", "Just a second.", "Let me check.")
   chosen round-robin; at most one filler per tool call; disabled when org setting `engine.tool_fillers=false`.
3. Render request: `url` (percent-encode values), `query` (merged into URL), `headers` + decrypted secret headers,
   `body` (recursive templating with type preservation) using `input.*` plus variables (11 §8).
4. Send through `SafeHttpClient`: HTTP/1.1+2, connect timeout 2 s, total timeout `timeout_ms`, response cap 1 MB
   (truncated → `response_truncated:true`), SSRF guard (§9), header `User-Agent: VoiceAgents-Tools/1.0`,
   `X-VA-Call-Id`, `X-VA-Signature` (HMAC of body with org webhook secret, same scheme as 19 §8).
5. Retries: on connect error, timeout, or 5xx, up to `retries` with backoff 250 ms × attempt.
6. Parse response: JSON if content-type JSON or body parses; else text. Apply `response_map` via JSONPath: single
   match → value; multiple matches → list; no match → null. Merge non-null into `variables`.
7. Build the tool message for the LLM:
   `{"status": <code>, "data": <parsed response truncated to 4 000 chars as JSON>, "extracted": {…}}`.
8. Emit `call.tool` (19 §2) with request (masked), status code, excerpt ≤ 2 KB, duration, extracted.
9. Wait for the speech playback to finish (if still playing) before the LLM's follow-up response starts streaming to
   TTS (the LLM request itself starts immediately to save time; tokens are buffered).
10. Failures (timeout/connection/4xx/5xx after retries) return `{"status": <code or 0>, "error": "<kind>"}` to the
    LLM; the dialogue prompt instructs the model to apologize briefly and offer alternatives. Errors also emit
    `call.error` level warning `TOOL_FAILED`.

> Decision (2026-11-05) P08-T02a: the runner core is `va_tools.models.ToolSpec` (from a stored row with its
> decrypted secret headers, or from a Send Call inline tool; `function_def(spec)` is the §4 function),
> `va_tools.render.render_request`, `va_tools.runner.ToolRunner.run(spec, arguments, scope)` over the shared
> `va_tools.http.HttpExecutor`, and `va_tools.signing.tool_signer` (the specs/19 §8 headers with every active org
> webhook secret). Steps 2 and 9 (speech, fillers) and the events belong to the in-call executor (P08-T02b).
> 1. Arguments: blank text is `{}`; anything but a JSON object is `invalid_arguments` with the detail
>    `{"path": "", "message": "arguments are not a JSON object"}`; schema errors are `{path, message}` (path like
>    `a.b[0]`, message ≤ 300 chars), sorted by path, at most 10. A schema that is itself invalid or has an
>    unresolvable `$ref` accepts any object (stored tools are validated; this only guards legacy rows).
> 2. Templates read the arguments as `input.*` only (`{{input.date}}`; the input layer is `{"input": args}`), then
>    the usual specs/11 §8 layers. A `query` key replaces every pair of that key already in the URL; secret header
>    values are never templated and win over a plain header of the same name (case-insensitive). A template that
>    does not parse is the outcome `render_error` (tool message `{"status":0,"error":"render_error"}`).
> 3. Tool message: `data` is the parsed body itself when its compact JSON has at most 4 000 characters, else a
>    4 000-character excerpt string ending in `…`; `extracted` lists every `response` name (null = no match). Only
>    non-null values of a 2xx answer are merged into variables; failures (`http_error`, `timeout`, `connection`,
>    `blocked`, `invalid_url`, `render_error`) carry no `data` and count as `TOOL_FAILED` (`"<tool>: <kind>"`
>    plus ` (<status>)` for HTTP errors); `invalid_arguments` does not.
> 4. Shown headers (the `/test` answer and `call.tool`): secret headers, every inline tool header (§4), and every
>    secret-named header (`Authorization`, `X-Api-Key`, …) show as `••••`. `call.tool` data = `tool_name`,
>    `tool_id`, `outcome`, `request` (`url` without user info/query/fragment, `method`, masked `headers`, `body`
>    capped like `data`), `status_code`, `response_excerpt` (≤ 2 000 chars), `duration_ms`, `attempts`,
>    `truncated`, `extracted`.
> 5. `POST /v1/tools/{tool_id}/test` needs `tools:write` (it sends the stored secrets), writes no audit row and
>    stores nothing. It answers 200 for every run: the §2 body plus `error` (null for 2xx, else the outcome),
>    `details` (argument errors), and `truncated`; `request` shows the full URL and is null when nothing was sent.
>    Built-ins are those of a conversation without a call (empty numbers and `call_id`, UTC, `en`). No database
>    session is open while the request runs.

> Decision (2026-11-05) P08-T02b1: task-mode calls run custom tools through `va_tools.executor.CallToolExecutor`
> (the `va_engine.tool_executor.ToolExecutor` protocol: `definitions()`, `tool_speech`, `handles(name)`,
> `speech(name)`, `execute(call, scope) -> ToolAnswer(content, variables)`).
> 1. Tools of a call: the live stored tools of `tool_ids` (call org only, in list order; absent, deleted and
>    foreign ids are skipped, logged `media.tool_missing` with a count) loaded by `va_media.call_tools.
>    load_call_tools` in one tenant session, secret headers decrypted after it closed; then the inline tools. An
>    inline tool replaces a stored tool of the same name; reserved system tool names and later duplicates are
>    dropped (`tools.tool_skipped`). A load failure leaves only the inline tools (`media.tools_unavailable`).
>    Requests are signed with the org's active webhook secrets (step 4); when those cannot be read the requests
>    go out unsigned (`media.tool_signing_unavailable`).
> 2. Events: every run publishes `call.tool` (message `Tool <name>: <outcome>`, the P08-T02a data), invalid
>    arguments included; a run without a 2xx answer also publishes a `warning` `call.error`
>    `{"code": "TOOL_FAILED", "detail": "<tool>: <kind>[ (<status>)]"}` (message `Tool call failed`).
> 3. Speech and fillers: the executed calls of one round start as tasks at once (at most 3 in parallel). The
>    caller hears the rendered `speech` of the first executed call whose tool has one (arguments as `input.*`,
>    a template that does not parse → no line); otherwise, when a tool still runs after 1 200 ms (on the call's
>    clock), one filler of `TOOL_FILLERS` ("One moment.", "Just a second.", "Let me check."), round-robin per
>    call: at most one line per round, also for parallel calls. `engine.tool_fillers=false` silences both
>    speech and fillers. These lines are transcript records only, never model messages (an assistant message
>    may not sit between a tool call and its answers), and are part of the cached filler set (specs/09 §9).
> 4. Step 9: the follow-up request starts once every tool of the round answered and the speech or filler line
>    finished playing (the runner is parked while the line plays); a barge-in during the line cancels the
>    unfinished tools and answers them `interrupted`.
> 5. Variables: the round's non-null 2xx extractions are merged into the call's variables in call order after
>    the round; tools of a round render with the variables of its start. The runtime snapshots the engine
>    state to `call:{id}:engine` after a merge and the end routine merges its variables into `calls.variables`
>    (as for pathway calls). `send_sms` and `schedule_callback` run behind the same executor (§6 Decision
>    P08-T02b2); `lookup_knowledge` follows with P08-T10.

Max tool calls per assistant turn: 4 (parallel tool calls from the LLM run concurrently, max 3 in parallel). Max
tool calls per call: 100.

## 6. Built-in system tools

| Name | Parameters schema | Availability | Behavior |
|---|---|---|---|
| `end_call` | `{"type":"object","properties":{"reason":{"type":"string"}},"required":[]}` | always (task mode); pathway nodes only when `global_config.allowEndCallTool` (default true) | Speaks final line, then `EndCallAction(reason="agent_end_call_tool")` |
| `transfer_call` | `{"type":"object","properties":{"destination":{"type":"string","description":"Key from the transfer list or 'default'"}},"required":[]}` | when call has `transfer_phone_number` or `transfer_list` (task mode only — ignored for pathways per Bland) | Speaks final line then cold transfer to resolved number |
| `press_button` | `{"type":"object","properties":{"digits":{"type":"string","pattern":"^[0-9*#ABCDwW]{1,32}$"}},"required":["digits"]}` | voice channel unless `ignore_button_press` is irrelevant (that controls inbound DTMF) — always on voice | Sends DTMF (IVR navigation) |
| `send_sms` | `{"type":"object","properties":{"message":{"type":"string","maxLength":1600}},"required":["message"]}` | agent number SMS-capable and org has active 10DLC for US long codes or toll-free verified | Sends SMS to the caller |
| `schedule_callback` | `{"type":"object","properties":{"when":{"type":"string","description":"ISO 8601 local time"},"note":{"type":"string"}},"required":["when"]}` | org setting `engine.allow_callbacks=true` | Creates a scheduled call (same config, `start_time=when`, `parent_call_id`) via API service layer; returns `{scheduled_call_id}` |
| `lookup_knowledge` | `{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}` | KB ids in tools or nodes | Returns top-5 chunks formatted per 14 §7 |
| `list_slots`, `book_slot` | defined in 11 §3.2.7 | Scheduling node only | Scheduling webhooks |

> Decision (2026-11-02): the wire schemas and availability of these tools follow the specs/10 §7 Decision P05-T09b
> (`end_call` adds `final_message`, `transfer_call` uses `label`, `schedule_callback` uses `datetime_iso`); the
> `press_button` pattern and `lookup_knowledge` schema above are kept. Implemented in `va_engine.system_tools`.
>
> Decision (2026-11-05) P08-T02b2: in task-mode calls `send_sms` and `schedule_callback` run behind the call's
> `CallToolExecutor` as system tools (`va_tools.executor.SystemTool`, handlers in `va_media.call_system_tools`).
> They are not in the executor's definitions, have no `speech` (a slow one gets a filler), and publish no
> `call.tool`; a handler failure publishes `call.error` `TOOL_FAILED` (`<tool>: <reason>`). The TaskBrain offers an
> executor-backed system tool only when its executor runs it, so a tool the runtime cannot run is never offered.
> 1. Availability: `send_sms` needs `tools.allow_send_sms` and the agent number (`from` of an outbound call, `to`
>    of an inbound one) to be an active SMS-capable number of the org (`va_db.voicemail_sms.sms_sender`, the check
>    the voicemail SMS and the Send Call `SMS_NOT_ENABLED` rule share; 10DLC / toll-free readiness joins that
>    check with P14-T05, so every sender check gets it at once; an unreadable capability counts as not capable,
>    `media.sms_capability_unavailable`). `schedule_callback` needs `tools.allow_schedule_callback` or
>    `engine.allow_callbacks`, a database and a workflow starter.
> 2. `send_sms` sends the message (trimmed; blank → `blank_message`) from the agent number to the other party
>    through `OutboundSms` (the voicemail SMS core: one Telnyx attempt, `messages` row in the number pair's
>    conversation). Every run gets a fresh message id: providers may omit or reuse tool call ids and the brain
>    never re-runs a tool call, so two `send_sms` calls send two messages. Success publishes `call.sms_sent` (message `SMS sent`,
>    data `message_id`, `conversation_id`, `segments`, `source: "tool"`) and answers
>    `{"status":"sent","segments":n}`; a refusal answers `{"error":"sms_failed","reason":"<reason>"}`.
> 3. `schedule_callback` reads `datetime_iso` with the Send Call `start_time` rules (specs/05 §3.3: offset kept,
>    naive → the call's `timezone` with `fold=0`, after now, at most 90 days ahead; `va_domain.call_callback`).
>    An unusable time answers `{"error":"invalid_datetime","reason":"invalid_datetime|in_the_past|too_far_ahead"}`
>    with no error event, so the model may ask again. The child (`va_db.callbacks.schedule_callback`, a fresh
>    id per run, so two calls schedule two callbacks) is an ordinary scheduled outbound call to the other party from
>    the agent number (`va_db.call_children.child_call`: the dispatch columns of the parent, `parent_call_id`, no
>    credit hold; an inbound parent's numbers are swapped) with the parent's `calls.config` (`start_time` set,
>    the note in the non-config key `callback_note`, without `retry_attempt` and the maintenance message). The
>    row and its `call.scheduled` event (`source: "callback"`, `parent_call_id`) are written and routed to the
>    webhook outbox in one transaction; after the commit the media worker broadcasts the event and starts the
>    child's `CallWorkflow` (`scheduled_for`), whose dial admits it like every scheduled call (compliance,
>    limits, credit hold). Success answers `{"status":"scheduled","scheduled_call_id","scheduled_for"}` (local
>    time in the call's timezone). A failed start marks the child `failed` (`error`/`call_error`, `call.error`
>    `{"code":"dispatch_failed","detail":"workflow_start"}`) unless the workflow already moved it on, and answers
>    `{"error":"callback_failed"}`; a database or event failure creates nothing and answers the same.
> 4. Pathway calls get these tools with P08-T02c.
>
> Decision (2026-11-02): the end reason of a call ended through `end_call` is the normative specs/05 §9
> `agent_end_call` (the table's `agent_end_call_tool` is not a §9 value). In task mode the final line is the
> tool's `final_message`; without one it is `"Goodbye."` unless the reply already spoke text this turn (specs/11 §6
> Decision P05-T09c). `end_call_speech` static/prompt modes arrive with pathway nodes (phase 07).

`end_call` and `transfer_call` final-line semantics: tool parameters additionally accept (via call config
`end_call_speech` / node `endCall`) `speech` (static), `speech_mode: "static"|"prompt"`, `speech_prompt`. Static:
speak `speech` verbatim. Prompt: dialogue LLM generates a one-sentence closing using `speech_prompt` as instruction
(500 ms budget; on timeout or error fall back to `speech`, then to `"Goodbye."`). Action happens only after the line
finished playing (playback mark).

## 7. Dynamic data

Send Call `dynamic_data` (also persona/agent/inbound configs):
```json
[{
  "url": "https://crm.acme.com/contacts?phone={{to}}",
  "method": "GET",
  "headers": {"Authorization": "Bearer {{request_data.crm_token}}"},
  "query": {},
  "body": null,
  "cache": true,
  "timing": "pre_call",
  "response_data": [
    {"name": "customer_name", "data": "$.contact.name", "context": "The customer's name is {{customer_name}}."},
    {"name": "plan", "data": "$.contact.plan", "context": "Their current plan is {{plan}}."}
  ]
}]
```
- `timing`: `"pre_call"` (default; fetched by `prefetch_call_context` activity) or `"every_turn"` (re-fetched at the
  start of each user turn, concurrently with routing; 800 ms budget; stale value used on timeout).
- `cache: true` → Redis key `dyn:{org_id}:{sha256(method+rendered url+body)}` TTL 300 s (`cache_ttl_seconds` field
  overrides, 0–86 400).
- Each `response_data` item sets variable `name` from JSONPath `data`; `context` (templated after extraction) is
  appended to the system prompt under `## Context` when the variable is non-null.
- Inbound calls: pre-call fetch runs inline with 1.5 s budget (01 §2.2); remaining fetches complete asynchronously and
  their variables/context appear from the next turn.
- Emits `call.dynamic_data` with `{url (no query secrets), status_code, duration_ms, variables_set:[…], cached:bool}`.
- Failures do not fail the call; event level `warning`.

## 8. Custom code sandbox (`va_tools.sandbox.deno`)

### 8.1 Contract
Customer code (TypeScript or JavaScript) default-exports an async function:
```ts
export default async function (ctx: {
  variables: Record<string, unknown>;     // current variables (copy)
  request_data: Record<string, unknown>;
  call: { id: string; from: string; to: string; direction: string; timezone: string; language: string };
  lastUserMessage: string;
  fetch: typeof fetch;                     // only allowlisted hosts succeed
}): Promise<{ variables?: Record<string, unknown>; say?: string; route_to?: string }>
```
`route_to` (node id) overrides routing for this node.

### 8.2 Execution
1. Write to a fresh temp dir (`tempfile.mkdtemp(prefix="va-code-")`): `user.ts` (code), `harness.ts`:
   reads one JSON line from stdin, imports `./user.ts`, calls default with ctx (with `fetch` = global fetch),
   validates return is a plain object, writes one JSON line to stdout `{"ok":true,"result":…}` or
   `{"ok":false,"error":"…"}`; total output cap 256 KB.
2. Command:
   ```
   deno run --quiet --no-prompt --no-config --no-lock --no-npm --no-remote
     --allow-read=<tempdir> --allow-net=<comma-separated allowlist or none>
     --v8-flags=--max-old-space-size=128 <tempdir>/harness.ts
   ```
   `--no-remote` forbids remote imports; with empty allowlist `--allow-net` is omitted (no network).
   Environment cleared (`env={"NO_COLOR":"1","DENO_DIR":<tempdir>/.deno}`), cwd = tempdir.
3. Timeout: `timeoutMs` default 1 000, max 5 000; enforced with `asyncio.wait_for`; on timeout kill process group
   (`start_new_session=True`, `os.killpg`). Also `resource.setrlimit(RLIMIT_CPU, ceil(timeout/1000)+1)` and
   `RLIMIT_AS` 512 MB in `preexec_fn`.
4. Concurrency: semaphore `CUSTOM_CODE_MAX_CONCURRENCY` (default 8 per process).
5. Temp dir removed after execution.
6. Errors → variables unchanged; `call.error` event `CUSTOM_CODE_FAILED` with message (≤ 500 chars); engine follows
   normal routing.
7. Network allowlist entries are hostnames (optionally `host:port`); private IP literals rejected at validation.

### 8.3 Validation at publish
`deno check --no-config --no-remote user.ts` with a bundled `ctx.d.ts` type file; diagnostics mapped to
`CUSTOM_CODE_ERROR` (31). JavaScript is checked with `deno check` too (syntax only). Timeout 10 s.

## 9. SSRF guard (shared with webhooks)
`SafeHttpClient` reuses `va_common.net.check_destination_url(url, ...)` — the exact rules of
`19-events-and-webhooks.md` §8 (blocked ranges, scheme rules, `WEBHOOK_ALLOW_PRIVATE` in local/test). DNS is resolved
once and the connection pinned to the checked IP (custom `httpx` transport) to prevent DNS rebinding.

> Decision (2026-11-01): the guard is `va_common.net.check_destination_url`, which returns the vetted addresses the
> pinning client connects to; its rules are the specs/19 §8 Decision P05-T05a. The pinning client itself arrives with
> tool execution.

> Decision (2026-11-03): shared HTTP executor (P07-T07). `va_tools.http.HttpExecutor` implements
> `va_engine.ports.HttpPort` for webhook nodes, scheduling nodes and (phase 08) tools: it receives an already
> rendered `HttpCall` (rendering stays in `va_engine.templating`; `va_tools` depends on `va_engine`, never the
> reverse) and never raises. Production executors come from `open_http_executor(settings, clock=, allow_http=,
> resolver=, call_id=, signer=)` over `va_common.safe_http.safe_client` (connect-time DNS pinning, private ranges
> per `WEBHOOK_ALLOW_PRIVATE`, no redirects). Pre-flight: an unparseable URL or one without a host is
> `invalid_url`; a scheme other than `https` (plus `http` when allowed) is `blocked`; a refused connection is
> `blocked` (never retried), an unresolvable name a `connection` failure. Timeouts: connect `min(2 s,
> timeout_ms)`, every other phase `timeout_ms`. Steps 4–6 as above: `User-Agent`, `X-VA-Call-Id`, signer headers
> over the exact body bytes, JSON body for every method but `GET`; retries on connect errors, timeouts and 5xx
> with `0.25 s × attempt` sleeps on the injected clock; body capped at 1 MiB (`truncated`), parsed as JSON when it
> parses, else text; JSONPath (`va_tools.jsonpath` over jsonpath-ng's extended parser, compiled paths LRU-cached)
> applies to JSON bodies of any status: one match → value, several → list, none (or an invalid path, logged
> without the path) → null. Logs carry method, host, status, outcome, attempts and duration only.

## 10. Tests required
- CRUD, validation errors, secret masking, tenant isolation, reference-check delete.
- Rendering with `input.*`, type preservation, URL encoding.
- JSONPath extraction single/multi/no match; truncation of large responses.
- Retries/backoff on 5xx and timeout; no retry on 4xx.
- Speech-during-tool ordering and filler after 1 200 ms (freezegun/fake clock + fake IO).
- Parallel tool calls limits; per-turn and per-call caps.
- System tools: each tool's availability rules and actions; speech_mode prompt fallback chain.
- Dynamic data: pre_call, every_turn budget/stale fallback, cache hit/miss, context rendering, failure tolerance.
- Deno sandbox (requires deno in CI image): success, thrown error, timeout kill, memory exhaustion, network denied,
  allowlisted fetch to a local test server, remote import denied, oversized output, deno check failures.
- SSRF: blocked ranges and DNS rebinding pin.

## 11. Acceptance criteria
- Simulator e2e call invokes a tool with speech, the fake customer API receives the rendered body, extracted
  variables appear in `calls.variables`, and `call.tool` webhook arrives.
- Custom code node p95 execution overhead ≤ 120 ms (Deno cold start included) on CI hardware.
