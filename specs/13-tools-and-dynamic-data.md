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

> Decision (2026-11-05) P08-T02c1: tools on pathway nodes (`va_engine.engine_node_tools`, shared round helpers in
> `va_engine.tool_tasks`).
> 1. Reply nodes = `Default` and `Knowledge Base` nodes (global nodes included) whose reply streams from the
>    dialogue model (no static `text`). They offer the allowed system tools (runner `pathway`: `end_call` only when
>    `global_config.allowEndCallTool`, default true; never `transfer_call`; `press_button` on voice; `send_sms`/
>    `schedule_callback`/`lookup_knowledge` only when the call's executor runs them), then the custom tools of the
>    node's `tools` followed by `global_config.globalTools` that the executor resolves (`PathwayToolExecutor.
>    definitions_for(refs)`, superseded by Decision P08-FIX-17 item 2: a string = stored tool id, a mapping =
>    inline tool by `name`; ref order, a tool named
>    twice once; unresolved refs are skipped and logged `engine.tool_unresolved` with a count; custom tools with a
>    reserved or already offered name are dropped). Other node types offer no custom or system tool; a Scheduling
>    node offers exactly `list_slots`/`book_slot`. Call-level `tools` apply to task mode only.
> 2. Without an executor (text chat, unit tests, simulations) no custom tools are offered; without system tool
>    facts (`ConversationFacts.system_tools` `None`) no system tools: text runs are unchanged.
> 3. Each reply pass at a node allows at most 3 tool rounds, 2 rounds with an invalid call, 4 calls per round and
>    100 tool calls per call (task-mode caps); then the node replies once more without tools. Tool answers are
>    `tool` history records (context updates on later requests, like Scheduling; call ids and arguments are not
>    persisted). A round cut by a barge-in cancels the unfinished tools and answers them
>    `{"error":"interrupted"}`; what the other unanswered calls read is Decision P08-FIX-20. Speech and fillers
>    follow §5 Decision P08-T02b1 item 3; the line is spoken as a `Say` and recorded as an assistant history
>    line. Tool variables merge into the conversation variables after the round.
> 4. `end_call` speaks `final_message`, else `Goodbye.` unless a reply of the pass already produced text, sets the
>    state ended with `end_reason` `agent_end_call` (as task mode; the §6 table's `agent_end_call_tool` is not
>    used), and yields `EndCallAction(reason="agent_end_call", ended_by="agent", after_mark=<the line's mark>)`.
>    `press_button` yields `SendDtmfAction(digits, duration_ms=250)` after the round's answers were recorded
>    (order superseded by Decision P08-FIX-19: right after that call's answer).
> 5. A reply the model could not give (unavailable: the turn ends after the `errorPhrase`, exactly as on a node
>    without tools, specs/11 §5.9 Decision P07-T08b) and a reply cut off mid-stream (stream error after the first
>    token; its tool calls may be incomplete; logged `engine.llm_stream_failed`) are followed by no tool round and
>    no further request in that pass, as in task mode. This also holds for Scheduling nodes.

> Decision (2026-11-05) P08-FIX-16: a pathway conversation that ended always ends the call, and tool rounds never
> rely on the model's call ids.
> 1. Final lines are not interruptible: the `end_call` tool's line (`final_message` or `Goodbye.`) and the
>    loop-protection `errorPhrase` are `Say(interruptible=False)`, like the End Call node's line and task mode's
>    final line. The `errorPhrase` spoken after an unavailable model reply ends no call and stays interruptible.
> 2. Owed end: once the state is ended (`end_call` tool, End Call node, loop protection), the engine owes its
>    consumer the `EndCallAction`. When the reply was closed before that action was yielded (a caller turn ended
>    before the final line's first audio, so the pipeline cancelled the reply and delivers a merged turn,
>    specs/07 §8), the next `handle_user_turn` decides nothing, records nothing (the superseded turn and the line
>    stay in the history as they are, no model request is made) and yields the final line again (same mark,
>    `interruptible=False`) followed by the `EndCallAction`. The debt is cleared when the `EndCallAction` is
>    yielded: the consumer ends the call on receipt. A reply closed again on the re-spoken line leaves the end
>    owed; turns after a delivered end yield nothing (`engine.turn_after_end` logs `owed`).
> 3. Tool rounds (pathway reply nodes and task mode, `va_engine.tool_tasks`) key tool tasks, answers, variable
>    merges and `interrupted` answers by a call's position in the round, never by its call id: providers omit or
>    repeat ids, and two calls with the same (or an empty) id each run, are cancelled together on a barge-in, and
>    get their own `tool` answer. `start_tools` returns one task per executed call in call order;
>    `round_contents(plan, answers)` pairs every planned call with its content. The id stays what is sent back
>    to the model as `tool_call_id`.

> Decision (2026-11-05) P08-FIX-17: a tool that ran stays on record when its turn is superseded, and a pathway
> node runs exactly the tools its own refs name.
> 1. Superseded turn (specs/07 §8 pre-audio merge: a caller turn that ends before the reply's first audio cancels
>    the reply and is delivered merged, with `supersedes`): the tools of the superseded turn already ran (see
>    Decision P08-FIX-19 for answers that report an action), and
>    their side effects, merged variables and `call.tool` events are never withdrawn, so their answers stay.
>    Pathways: the turn's `tool` history records stay, in order, ahead of the merged caller turn (they reach the
>    next request as context updates); its user and assistant records go as before. Task mode: its completed
>    tool rounds (the assistant `tool_calls` message without its text, which nobody heard, followed by one `tool`
>    answer per call) are re-appended behind the merged caller message, and their `tool` history records stay; a
>    round lacking an answer for every call is dropped. `{"error":"interrupted"}` answers (tools cancelled when
>    the reply was closed) are kept like any other answer. A turn superseded again carries the same rounds on,
>    once. A task-mode `transfer_call` whose reply was closed on its `message` (before the message's first
>    audio) never reached the action handler: its answer is rewritten from `{"status":"transferring"}` to
>    `{"error":"interrupted"}` (message and history record), so the carried round does not tell the model that
>    the call is being transferred. An `end_call` or `transfer_call` cut earlier in its round is answered
>    `interrupted` like every action that was not handed over (Decision P08-FIX-20).
> 2. Per-node resolution: `PathwayToolExecutor.for_refs(refs) -> ToolExecutor` replaces `definitions_for`. The
>    returned node executor's `definitions()` are the tools the node's `tools` followed by `globalTools` refs
>    name, in ref order (a string = that stored tool, a mapping = that inline tool); unresolved refs are
>    skipped, and of several refs with one tool name the first wins (node tools before global tools). The node
>    offers, speaks for (`speech`) and executes exactly those definitions plus the call's executor-backed system
>    tools, so the definition shown to the model is the one that runs, and different tools of one name on
>    different nodes (inline or stored) never mix. `engine.tool_unresolved` counts the refs that produced no
>    definition (unresolved, or shadowed by an earlier ref of the same name). There is no specs/11 §11 validation
>    rule for repeated names. Node executors share the call's runner, events and system tool runs (one drain at
>    call end): binding for P08-T02c2. `PathwayToolExecutor` itself keeps only `tool_speech` (the tool usage
>    note) and `handles(name)` (which executor-backed system tools the call runs).

> Decision (2026-11-05) P08-FIX-19: an answer that reports an action (`{"status":"sent",…}`,
> `{"status":"transferring"}`, `{"status":"ending"}`) is on record only for an action that was handed to the
> consumer of the reply.
> 1. A pathway tool round (`va_engine.engine_node_tools.run_node_round`) answers its calls in call order and
>    yields each `press_button`'s `SendDtmfAction` right after that call's answer, before the next call is
>    answered (the task-mode order of specs/11 §6 Decision P05-T09c item 4). A reply closed at a press leaves that
>    press `sent`: an action the reply yielded was received and runs to completion (specs/07 §8 Decision P04-T12b,
>    actions are never cancelled by a reply cancel). Every later call of the round that reports an action is
>    answered `{"error":"interrupted"}`, an `end_call` included: it neither ends the conversation nor is owed
>    (Decision P08-FIX-16 owes the end of a conversation that ended; this one did not), and the next caller turn
>    is handled as usual. The executor calls of the round have finished by then and keep their answers (Decision
>    P08-FIX-20).
>    The round's `end_call` takes effect only once every call was answered and every press handed over. These
>    records stay when the turn is superseded (Decision P08-FIX-17 item 1).
> 2. In Decision P08-FIX-17 item 1 "ran" means: tools the executor ran, and actions handed to the consumer
>    (`press_button`; a task-mode `transfer_call` after its message). A task-mode `transfer_call` closed on its
>    message and then repeated by the model in the merged turn is requested as usual: the carried answer reads
>    `interrupted`, the new call is answered `transferring` and yields its `TransferRequest`.

> Decision (2026-11-05) P08-FIX-20: in a tool round that is cut short (pathway reply nodes and task mode alike;
> the cut may land on the speech or filler line, while the tools are awaited, or at a `press_button`) a call is
> answered `{"error":"interrupted"}` only when what it stands for did not happen:
> 1. an executor call whose run was still unfinished and was cancelled at the cut (a system tool run behind the
>    executor continues detached and reads `{"status":"in_progress"}` instead, §6 Decision P08-FIX-21);
> 2. a `press_button`, `end_call` or `transfer_call` that was not handed over (Decision P08-FIX-19).
>
> Every other call not answered yet gets the answer that is true for it. An executor call whose run finished
> keeps its real answer (blank content reads `tool_failed`), and the variables it extracted are merged at the
> cut, in call order, when the round had not merged them yet: its request went out and its `call.tool` event
> exists, so reporting it `interrupted` would make the model run it again (a second booking) once the round is
> carried by a superseding turn (Decision P08-FIX-17 item 1). A call answered without running
> (`invalid_arguments`, `too_many_tool_calls`, `skipped`) keeps that answer. A cut round never ends or transfers
> the call. Both runners settle a round through `va_engine.tool_tasks.settle_round` (`finished_answers`,
> `cut_contents`). This narrows the "every later call" / "unanswered calls" wording of Decision P08-FIX-19
> item 1, Decision P08-T02c1 item 3 and §5 Decision P08-T02b1 item 4.

> Decision (2026-11-05) P08-T02c2: pathway calls in the media worker get node tools and system tools.
> 1. The call's `PathwayToolExecutor` is `va_tools.pathway_executor.PathwayCallExecutor`. `for_refs(refs)`
>    returns a `CallToolExecutor` over the tools the refs resolve to, in ref order: a string ref is a stored
>    tool id (`TL-<uuid>` or a bare uuid, `tool_ref_id`; the Tools API parses its path ids with the same
>    function) and resolves when the call loaded that tool; a mapping ref is validated as a Send Call inline tool
>    (`va_domain.calls.tools.InlineTool`: no `secret_headers`, its `headers` are masked like secrets) and one
>    that does not validate resolves to nothing (logged `tools.inline_tool_invalid` once per call with the error
>    count only). So does a mapping ref that cannot be written as JSON (an integer beyond 64 bits passes graph
>    validation): it could not be sent either, and it is logged once per call for all such refs (`errors=1`).
>    Tools with a reserved name are dropped; of several refs with one tool name the first wins
>    (`tools.tool_skipped`). Every node executor shares the call's `va_tools.executor.SystemRuns`: the
>    executor-backed system tools run on any reply node, a run one node left behind is `detached` for every
>    other node's executor (§6 Decision P08-FIX-21), and all runs are drained once.
> 2. Loading (`va_media.call_tools.PathwayCallTools`): the stored tools named by the node `tools` and the
>    `globalTools` of the call's pinned graph are loaded when the call starts, before any vendor socket opens,
>    and those of every graph the call switches to (Transfer Pathway node, forwarding to a pathway) right after
>    that graph is loaded. Rows are read in one tenant session of the call's org (ids that are absent, deleted
>    or another org's resolve to nothing) and their secret headers are decrypted after the session closed. An
>    id whose lookup ran is not looked up again in the call. A lookup that could not run (database, KMS;
>    logged `media.tools_unavailable`), that ran out of time, or that was cancelled with the turn that
>    switched graphs learned nothing about its ids: the graph keeps its inline tools and the next load of that
>    graph looks again (every switch or return to the graph is such a load, so the retries are bounded by the
>    graph loads of the call). Requests are signed as in §5 Decision P08-T02b1 item 1; the org's signing
>    secrets are read when the first graph with tool refs is seen, and until that read worked requests go out
>    unsigned. A read that failed (`media.tool_signing_unavailable`) or ran out of time is repeated at the
>    next load of a graph with tool refs; an org that is gone is not asked for again. Deadline: the load at
>    call start has none of its own (the wait task mode has too, before any audio); a load at a graph switch
>    runs inside the caller's turn and gets 2 s on the call clock
>    (`va_media.call_tools.SWITCH_LOAD_TIMEOUT_S`) for the tools and the signing secrets together. Past it
>    the switch goes on with what was finished: a tool lookup cut off is logged `media.tools_unavailable`
>    (`error_type` `TimeoutError`) and skips the signing read, a signing read cut off is logged
>    `media.tool_signing_unavailable`.
> 3. System tools: the facts are the task-mode ones (`facts_for_call`) with runner `pathway`, so reply nodes
>    offer `end_call` (per `allowEndCallTool`), `press_button`, and `send_sms`/`schedule_callback` under the
>    task-mode availability rules (§6 Decisions P08-T02b2 and P08-FIX-13), never `transfer_call`. Fillers follow
>    the org's `engine.tool_fillers`. A media worker without a call tool source offers `end_call` and
>    `press_button` only.
> 4. Events: tool runs are reported as in task mode (§5 Decision P08-T02b1 item 2). The `call.tool` data of a
>    pathway call also carries `turn_index` and `node_id` (the engine turn and current node when the run is
>    reported), which lists the run in that turn's `pathway_logs[].decision.tools` (specs/11 §12 rule 3).
> 5. The call's tool speech (quoted by the tool usage note) is the first non-blank `speech` among the tools the
>    pinned graph's refs resolve to, in node order then `globalTools`; while the call has none, the refs of a
>    graph loaded later are searched the same way, and once found it stays. The engine reads it before every
>    reply pass (`PathwayEngine._prepare`), so the note quotes a later graph's speech from that graph's first
>    reply on; until the call has one the note quotes the first filler phrase.
> 6. At call end the system tool runs a barge-in left running are drained (20 s, §6 Decision P08-FIX-13 item 2)
>    before the brain closes and the engine snapshot is flushed.

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
>    unfinished tools and answers them `interrupted`. Tools that finished before the cut keep their answers
>    (§4 Decision P08-FIX-20); a system tool whose run goes on reads `in_progress` (§6 Decision P08-FIX-21).
> 5. Variables: the round's non-null 2xx extractions are merged into the call's variables in call order after
>    the round; tools of a round render with the variables of its start. The runtime snapshots the engine
>    state to `call:{id}:engine` after a merge and the end routine merges its variables into `calls.variables`
>    (as for pathway calls). The finished tools of a round that was cut short merge their variables at the cut
>    (§4 Decision P08-FIX-20). `send_sms` and `schedule_callback` run behind the same executor (§6 Decision
>    P08-T02b2); `lookup_knowledge` follows with P08-T10.
> 6. A turn superseded before any audio keeps its completed tool rounds: specs/13 §4 Decision P08-FIX-17 item 1.

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
> 4. Pathway calls get these tools on their reply nodes: §4 Decision P08-T02c2 item 3.
>
> Decision (2026-11-05) P08-FIX-13: hardening of the P08-T02b2 system tools.
> 1. Both tools need the other party's number to be E.164 (`va_media.call_sms.dialable`, the edge's inbound
>    rule): for an anonymous or withheld caller the runtime builds neither (`media.system_tools_skipped`, reason
>    `no_callable_number`), so the model is not offered them; a handler reached anyway answers
>    `{"error":"sms_failed"|"callback_failed","reason":"no_callable_number"}`.
> 2. A `send_sms` / `schedule_callback` run is a side effect the caller asked for (the tool says so:
>    `SystemTool.detaches`, Decision P08-FIX-22), so it runs to completion when a barge-in
>    interrupts the turn: the executor owns each run as a task awaited through `asyncio.shield`. The model is
>    told the run is in progress (Decision P08-FIX-21); the run's outcome (failure event included) is still
>    reported, and at call end
>    `CallToolExecutor.aclose` waits up to 20 s for unfinished runs (before the snapshot and the call's events
>    are flushed), then cancels the rest (`tools.system_tools_abandoned`).
> 3. Per-call caps: `send_sms` sends at most 3 messages per call (sent or in flight; a message Telnyx did not
>    accept does not count); a call has at most 1 pending callback (children in status `scheduled`, counted under
>    the parent row lock, `va_db.callbacks.CallbackLimitError`; a canceled/failed/dialed callback frees the
>    slot). A run past a cap answers `{"error":"limit_reached"}` with no error event.
> 4. A failed callback start first terminates the child's workflow id (a timed-out start may have reached
>    Temporal and would otherwise sleep until the due time), then marks the child `failed` as in P08-T02b2 item 3,
>    retrying the write once. When both writes fail the row stays `scheduled` without a workflow and is logged at
>    error level `media.callback_orphaned` with the child call id (no sweep starts such rows); the tool answers
>    `{"error":"callback_failed"}`.
>
> Decision (2026-11-05) P08-FIX-14: item 4 of P08-FIX-13 is reordered, and the drain gets a live session.
> 1. A failed callback start first records the failure (guarded `scheduled` → `failed` with the child's
>    `call.error` `dispatch_failed`, one retry), then acts on the outcome: `failed` or unrecorded (both writes
>    failed, `media.callback_orphaned`) → terminate the child's workflow id (`CallbackStarter.abandon`, 5 s,
>    returns whether a workflow was terminated, never raises) and answer `{"error":"callback_failed"}`; the
>    child is no longer `scheduled` but not ended (its workflow, reached by a timed-out start, already moved it
>    on) → the child belongs to that workflow: nothing is terminated and the tool answers `scheduled`; the child
>    already ended → answer `{"error":"callback_failed"}` without terminating. Terminating before the write could
>    leave a moved-on child without its workflow; a failed child is skipped at dial even if a late start lands.
>    Start 10 s + abandon 5 s + two writes stay inside the 20 s drain of item 2.
> 2. `CallSession` (specs/07 §3) ends a session in two stages: the body (and the end-request and control
>    tasks) is cancelled and awaited first while the ownership renewer and the heartbeat keep running, so the
>    body's teardown (the system-tool drain, the snapshot flush) never lets the heartbeat or the lock expire;
>    then the renewer and the heartbeat stop and the end routine runs. A lock lost during the teardown is not
>    released, and the session then ends `worker_lost` (Decision P08-FIX-18).
>
> Decision (2026-11-05) P08-FIX-15: the keepalive of item 2 of P08-FIX-14 is bounded. The renewer and the
> heartbeat keep running during the body's teardown for at most `TEARDOWN_KEEPALIVE_S` = the 20 s system-tool
> drain + 5 s (25 s) on the session clock. Past it the session logs `media.body_teardown_overran`, stops both
> keepers (the heartbeat key expires after its 10 s TTL, so the workflow's heartbeat watch ends the call
> `worker_crash`, and the lock is no longer renewed) and keeps waiting for the body before the end routine and
> the cleanup (which then run with `worker_lost`, Decision P08-FIX-18): a wedged teardown can no longer pass
> for a live call. The heartbeat also stops as soon as the renewer reports the lock lost (during the race or
> the teardown): the pod that lost the call stops vouching for it.
>
> Decision (2026-11-05) P08-FIX-18: a session that stopped vouching for its call does not end it.
> 1. When the body's teardown is over, a session whose renewer lost the lock (during the race or the
>    teardown), or whose teardown overran `TEARDOWN_KEEPALIVE_S` (both keepers stopped, Decision P08-FIX-15),
>    ends with `end_reason='worker_lost'`, `ended_by='error'` (`OWNERSHIP_LOST`) whatever decided the race: the
>    end routine neither records nor hangs up (specs/07 §18 Decision P05-T11b step 1) and signals
>    `worker_lost`; `CallWorkflow` ends the call (on that signal, or on its heartbeat watch: `worker_crash`).
>    One party ends the call, not two.
> 2. A `transferred` end that decided the race stays: its routine neither records nor hangs up anyway, and
>    `worker_lost` would make the workflow hang up on the bridged caller and target. A lock lost, or an
>    overrun, before the body returned `transferred` (it returns after its exit stack unwound) still ends
>    `worker_lost`, as for every call the pod still answers for.
> 3. A session that failed (the body or a control handler raised) still raises and closes 1011; its end
>    routine is given `worker_lost` instead of the failure, for the same reason as item 1.
> 4. The lock: not released after a loss (it is not ours), released by compare-and-delete after an overrun.
>
> Decision (2026-11-05) P08-FIX-21: a system tool run that goes on after its round was cut (Decision P08-FIX-13
> item 2) is answered `{"status":"in_progress"}`, not `{"error":"interrupted"}`. The message will normally be
> sent or the callback scheduled, and `interrupted` would make the model of the superseding or next turn do it
> again (a second text to the customer). This is the rule of specs/13 §4 Decision P08-FIX-20 ("`interrupted`
> only for what did not happen") applied to a run whose continuation is known at the cut.
> 1. The executor reports such calls: `va_engine.tool_executor.ToolExecutor.detached(call) -> bool`, by object
>    identity (call ids may be empty or repeated, and equal calls are different runs). `CallToolExecutor` notes
>    a call (of a tool that detaches, Decision P08-FIX-22) when the wait for its executor-owned run is cancelled
>    while the run itself is not (still running, or just finished). A run cancelled by `aclose` is not detached,
>    and neither is a custom tool: its request is cancelled with the wait. Both still read `interrupted`.
> 2. `va_engine.tool_tasks.cut_contents` / `settle_round(..., executor=)` ask the executor that ran the round
>    for every executor call cancelled at the cut (`va_engine.task_tools.IN_PROGRESS`); a call whose run
>    finished keeps its real answer as before.
> 3. The cancellation of the wait always propagates: the executor never turns a cancelled wait into an answer.
>    A runner cancelled while it awaits a tool task forwards the cancel to that task, and a tool task that then
>    ended with a result would swallow the runner's own cancellation (the reply would run on).
> 4. The run's outcome is reported as before (`call.sms_sent` / `call.scheduled`, a failure as `call.error`
>    `TOOL_FAILED`); the model is not told it later. The per-call caps of Decision P08-FIX-13 item 3 still
>    bound a model that sends again anyway.
>
> Applies to task-mode calls now and to pathway calls once their executor is wired (P08-T02c2).
>
> Decision (2026-11-05) P08-FIX-22: whether a run goes on after its round was cut is a property of the tool,
> not of being a system tool: `va_tools.executor.SystemTool.detaches` (every system tool declares it).
> 1. `send_sms` and `schedule_callback` declare `detaches = True` (a side effect the caller asked for):
>    Decision P08-FIX-13 item 2 and Decision P08-FIX-21 apply to them unchanged.
> 2. A system tool without a side effect declares `detaches = False` (the `lookup_knowledge` of P08-T10): its
>    run happens in the task that waits for it and is cancelled with it; the executor neither owns nor drains
>    it (`aclose`), it is never `detached`, and a cut round answers it `{"error":"interrupted"}`.
>    `in_progress` would be wrong there: that answer is never followed by the result, so the model would
>    neither have the knowledge nor look again. A failure outcome or a crash is reported like for every system
>    tool (`call.error` `TOOL_FAILED`).
> 3. Not changed, on purpose: an `in_progress` answer is not replaced by the run's outcome later (Decision
>    P08-FIX-21 item 4). It would matter only when a barge-in lands inside a run that then fails; the failure
>    is on the call (`call.error` `TOOL_FAILED`), and rewriting the answer needs a link from the call to a
>    history record that survives superseded turns and the persisted engine state in both runners.
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

> Decision (2026-11-05) P08-T03a: the dynamic data core and the outbound pre-call fetch. `every_turn` (Decision
> P08-T03b below, which amends the "Outbound pre-call" paragraph: every entry is fetched there, whatever its
> `timing`) and the inbound budget (P08-T03c) build on it.
>
> **One fetch** (`va_tools.dynamic_data.DynamicDataFetcher.fetch(spec, index, scope, timeout_ms=)`, never raises):
> 1. The request is the spec rendered like a tool request (`va_tools.render.render_request`, the specs/13 §5
>    Decision P08-T02a rules: URL expressions percent-encoded, `query` merged into the URL, headers as text, a
>    lone body expression keeps its value's type). A template that does not parse is the outcome `render_error`
>    and nothing is sent.
> 2. The cache is used only with `cache: true`, `cache_ttl_seconds` > 0 and a Redis client. The key is
>    `dyn:{org_id}:{sha256 hex}` over `"<METHOD>\n<rendered url with the merged query>\n<body>"`, the body as
>    compact JSON with sorted keys (empty for no body); headers are not part of it, the org is. The value is
>    `{"status": int, "body": <parsed JSON document>}`. A stored value of that shape whose body is a JSON
>    document (rule 4) is a hit (no request, outcome `ok`, `cached: true`, the JSONPaths applied to the stored
>    body). Any other stored value is a miss, logged `tools.dynamic_data_cache_invalid`: one that does not
>    parse into that shape, and (amended 2026-11-06) one whose body is text, a bare string, nothing or `null`,
>    which only a build before this amendment could have stored. A Redis failure is a miss too.
> 3. On a miss the request goes through the shared HTTP executor (specs/13 §9 Decision 2026-11-03) with
>    `User-Agent` and `X-VA-Call-Id` and **without a signature**: step 4 of §5 signs tool requests only, and a
>    dynamic data endpoint authenticates the platform by its own header, as in the example above. Only a
>    *readable* 2xx answer (rule 4: a whole JSON document, so never a truncated one) whose compact JSON is at
>    most 262 144 bytes is stored (`SET … EX cache_ttl_seconds`); a failed write is logged and ignored.
>    Failures are never cached, and (amended 2026-11-06) neither is a 2xx answer that is not readable (a
>    proxy's 200 HTML page, an empty 200/204, a bare JSON string): it says nothing about the data, so like a
>    500 it must not keep the endpoint from being asked again, by the next turn and by every other call of
>    the org with the same rendered request, for up to `cache_ttl_seconds`.
> 4. Variables are set only for the outcome `ok` (a 2xx answer): each `response_data` item sets `name` to the
>    JSONPath value of `data`. The JSONPaths are read only from a *readable* answer, a whole JSON document
>    (`DynamicDataResult.readable`; amended 2026-11-06 with Decision P08-T03b item 3): an empty body, a body
>    that is not JSON (a bare JSON string or `null` included, which the executor cannot tell from text and
>    nothing) and a body cut at the executor's size cap are `ok`, not readable, and set nothing, also when the
>    cut body happens to parse. A null or missing value sets nothing, and neither does a value the stores behind
>    a call cannot hold (each is logged by name, never with the value, and skipped; the fetch stays `ok` with its
>    other variables, and a skipped variable gets no context line): a value whose arrays and objects nest more
>    than 32 deep (a scalar is 0, `[1]` is 1; the context JSON, the engine state and the event dumps stop at
>    about 254 levels and wrap a variable in levels of their own, so the limit keeps a wide margin), a value
>    with U+0000 in any string or object key (Postgres `jsonb` refuses it, which would lose the events and the
>    `calls.variables` merge of every fetch of the call), and a value whose compact JSON exceeds 16 384 bytes.
>    The rules apply alike to a fetched and a cached answer. Of two items with one name the later one counts.
> 5. `context` is rendered for each variable that was set and has a non-blank `context`, with this fetch's
>    variables in front of the scope's, stripped and cut to 2 000 characters; a blank result or a template that
>    does not parse gives no line.
> 6. Every outcome but `ok` (`http_error`, `timeout`, `connection`, `blocked`, `invalid_url`, `render_error`, and
>    `error` for anything else that raised) sets nothing and is a `warning`.
>
> **Several fetches** (`fetch_all`) run concurrently, each rendered in the SAME scope (a fetch never sees another
> fetch's variables), within one budget on the injected clock; a fetch not finished by then is cancelled and
> reported as `timeout`. Results are merged in `dynamic_data` order (`merge_results`): a later fetch wins a
> variable name, and its context line (or its having none) replaces the earlier line of that name.
>
> **Outbound pre-call** (`va_workflows.activities.dynamic_data.PreCallDynamicData`, inside
> `prefetch_call_context`): the `timing: "pre_call"` specs are rendered with the call's variables,
> `request_data` and built-ins, each request gets 5 s (connect ≤ 2 s) and no retries, the phase gets 6 s (inside
> the activity's 20 s). The merged variables and context lines go into `CallContext.variables` /
> `context_sections` BEFORE the opener is rendered, so the first sentence, the prompts and "Known information"
> see them. After the context is saved, one transaction writes a `call.dynamic_data` event per fetch and merges
> the variables into `calls.variables` (a task call without tools has no engine-state snapshot that would), then
> the events are broadcast. Recording after the save means an activity retry caused by a failed save has
> recorded nothing; a recording failure is logged (`call_context.dynamic_data_record_failed`) and swallowed,
> because dynamic data never fails a call. An attempt that fails after it recorded (the activity timing out
> during the opener pre-render) fetches and records again on the retry, so these events are at-least-once.
> A context the media worker rebuilds from the `calls` row (Redis miss,
> specs/07 §3) carries no dynamic data. The worker shares one guarded tool HTTP client between these fetches and
> the pathway test runs.
>
> **Event** `call.dynamic_data`: message `Dynamic data: {n} variables set` (`1 variable set`; plus ` (cached)`) or
> `Dynamic data failed: {outcome}` (plus ` ({status})` when a response arrived); data `{url, method, status_code,
> duration_ms, variables_set, cached, outcome, timing, index}` where `url` has no user info, query or fragment
> (empty when it could not be rendered) and `index` is the spec's position in `dynamic_data`. Logs
> (`tools.dynamic_data`) carry the index, timing, outcome, status, cache flag, duration and variable count only.
>
> **Prompt**: the context lines are the `## Context` block of the Voice System Prompt (specs/10 §5 Decision
> P08-T03a) in task and pathway calls.

> Decision (2026-11-05) P08-T03b: per-turn dynamic data (`timing: "every_turn"`) in calls.
>
> 1. **First value.** The pre-call phase (`PreCallDynamicData.fetch`) fetches EVERY `dynamic_data` entry,
>    whatever its `timing` (this amends "the `timing: "pre_call"` specs" of Decision P08-T03a): the opener, the
>    first prompt and the stale fallback need a value. The variables of all entries merge as in P08-T03a.
>    Lines: `CallContext.context_sections` holds the merged lines of the `pre_call` entries only; the lines of
>    each `every_turn` entry go to the new `CallContext.turn_sections[index]` (`dict[int, list[str]]`, the
>    index being the entry's position in `dynamic_data`; an entry that produced no line has no key;
>    `CallContext.turn_lines()` lists them in index order). The context version stays 1. Amended 2026-11-06:
>    a worker writes the field into EVERY context (`"turn_sections": {}` when there is none) and a rolling
>    update does not swap worker and media worker at the same instant, so a new top-level field is additive
>    only for a reader that skips keys it does not know. From this build on `CallContext` does
>    (`extra="ignore"` on the top-level model only; `config`, `memory` and every nested model still refuse an
>    unknown key, and a change of meaning bumps `version`): a later field is additive for readers of this
>    build or newer. No build older than this one is deployed, so `turn_sections` itself needs no such
>    reader; a reader that refused it would rebuild the context from the `calls` row and lose the pre-call
>    variables, the context lines and `opener_text`.
> 2. **Refresh.** At the start of each user turn (speech or keypad; not the opener, not a `wait_for_greeting`
>    greeting, not a no-input or node wait timeout, not a chained pass or a tool round, and not a turn refused
>    by `maxTotalTurns`) every `every_turn` entry is fetched again, all concurrently and in ONE scope (the
>    call's current variables, `request_data`, built-ins), each request with `timeout_ms` 800
>    (`va_tools.turn_data.TURN_TIMEOUT_MS`) and no retries, the whole refresh within 0.8 s of the call clock
>    (`TURN_BUDGET_S`). The request, cache and extraction rules are P08-T03a's (`cache: true` usually means a
>    Redis hit; the media worker uses the same Redis as the worker, so `dyn:` entries are shared); requests are
>    not signed.
> 3. **Result.** A fetch that succeeded with a readable answer (P08-T03a rule 4: a whole JSON document): its
>    variables are set over the call's variables (a later entry wins a name) and its lines replace that
>    fetch's earlier lines wholesale (a variable the answer no longer has keeps its value, as "a null sets
>    nothing" in P08-T03a, and loses its line). A failed or timed-out fetch changes nothing: the earlier value
>    and lines stay (the stale value). Neither does a 2xx answer that is not readable (an empty body, a body
>    that is not JSON, a body cut at the size cap): it says nothing about the data, so a proxy's 200 error
>    page or one oversized answer must not erase context a 500 would have kept; its event is still `ok` with
>    `variables_set: []` (and it is not cached, P08-T03a rule 3, so the next turn asks the endpoint again).
>    Variables are never unset. The
>    `## Context` block is the pre-call lines, then the turn lines in `dynamic_data` order; two entries of
>    different timing that set one name both keep their line, the variable holds the later writer's value.
>    **A name two `every_turn` entries share** (added 2026-11-06): "changes nothing" includes the names of
>    the entry that did not count. Each entry holds the names its last counted fetch that was put in effect
>    in this call set; in a turn in which it does not count (failed, timed out, not readable), an EARLIER
>    entry that sets one of those names does not get it, so the variable keeps the later entry's last value
>    instead of flipping to the earlier entry's and back (the earlier entry's own line still follows its
>    answer, as any two entries sharing a name each keep their line). A later entry that counts wins the
>    name as before; an entry that counted with an answer that no longer has a name no longer holds it; an
>    entry that has not counted yet in this call holds nothing, because the pre-call phase records lines,
>    not names (so on the first turns of a call, and always in a call whose context was rebuilt or that is
>    inbound, a shared name follows the latest entry that counted in the turn).
> 4. **Where the turn waits.** Pathway calls: the refresh runs beside the turn's decision calls and the
>    speculative reply; the node is executed after both finished (so after at most `max(decision, 0.8 s)`);
>    the decision itself sees the earlier values. The refreshed variables are merged once both finished and
>    BEFORE the turn's extraction is applied (extraction and tool variables of the turn win a shared name). A
>    refresh that changed a variable value or a turn line discards the speculative reply (`prompt_changed`);
>    an unchanged one keeps it. Task calls have no routing step: the turn's first request waits for the
>    refresh (at most 0.8 s; a cache hit costs one Redis GET).
> 5. **Cut.** The variables, the lines and the events of a refresh are ONE commit: fetching changes nothing
>    (`TurnData.refresh` returns a pending `TurnRefresh`), and the turn puts it in effect in a single
>    synchronous step (`commit_refresh`: `apply()` replaces the lines and reports the fetches, then the
>    variables are merged) once everything it awaits beside the refresh has finished: in a pathway call after
>    the decision calls, in a task call right after the refresh. A turn cut before that step (a barge-in
>    merge, a superseded turn), whether its refresh is still running (the fetches are cancelled) or already
>    finished while the decision calls run (the common case: a cache hit takes milliseconds, a decision
>    hundreds), and a turn whose decision fails, leaves no trace: nothing is merged, no line changes, no
>    event is written; the next turn refreshes again. Only the `dyn:` cache entry of a fetch that completed
>    stays (it is the endpoint's answer, not call state). A port that raises, in the refresh or in `apply()`,
>    is logged (`engine.turn_data_failed`) and the reply goes on with the earlier values.
> 6. **Events and state.** One `call.dynamic_data` per fetch per turn (message, data and level as in
>    P08-T03a, `timing: "every_turn"`), published by the media worker through the call's `TurnRecorder` in
>    `dynamic_data` order. The refreshed variables are part of the engine state snapshot
>    (`call:{id}:engine`); a task call with `every_turn` entries gets the snapshot (and the end routine's
>    merge into `calls.variables`) even without tools. The turn lines are not part of `EngineState` (a voice
>    call never resumes from a snapshot).
> 7. **Not yet.** Inbound calls have no pre-call fetch until P08-T03c: their `every_turn` entries get their
>    first value at the first user turn, as does a context the media worker rebuilt from the `calls` row.
>
> Code: the port `va_engine.turn_data.TurnData` (`sections()`, `refresh(scope) -> TurnRefresh` with
> `variables` and `apply()`; `refresh_turn` never raises, `commit_refresh(variables, refresh)`) used by
> `TaskBrain(turn_data=)`, `PathwayEngine` (`EnginePorts.turn_data`) and `PathwayPrompt(turn_data=)`; its
> implementation `va_tools.turn_data.TurnDynamicData` (`refresh` → `FetchedTurn`; the held names are
> updated only by `commit`, never by a refresh) over the
> `DynamicDataFetcher`; the media worker's `va_media.call_dynamic_data.TurnDataSource` (the shared tool HTTP
> client, Redis, clock) builds one per call that has `every_turn` entries.

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
