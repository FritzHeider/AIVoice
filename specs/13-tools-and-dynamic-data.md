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
`SafeHttpClient` reuses `va_common.net.assert_public_destination(url)` — the exact rules of
`19-events-and-webhooks.md` §8 (blocked ranges, scheme rules, `WEBHOOK_ALLOW_PRIVATE` in local/test). DNS is resolved
once and the connection pinned to the checked IP (custom `httpx` transport) to prevent DNS rebinding.

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
