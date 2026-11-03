# 11 — Conversational Pathways Execution Engine (`packages/va_engine`)

The conversation engine is the brain of every call, SMS conversation, and widget thread. It is transport-agnostic:
the media worker (voice), `SmsConversationWorkflow` (SMS), and the widget handler (chat/voice) all drive the same
`ConversationEngine` through the `EngineIO` interface (§10). Two runners exist: **task mode** (single free-form
prompt, §6) and **pathway mode** (versioned node graph, §4–§5). Personas and agents resolve to one of these two
runners before the engine starts (`20-memory-personas-agents.md`).

## 1. Package layout

```
packages/va_engine/src/va_engine/
├── __init__.py
├── models/
│   ├── nodes.py            # PathwayNode, PathwayEdge, NodeType, all node config models (§3)
│   ├── graph.py            # PathwayGraph (validated nodes+edges+global_config), GlobalConfig
│   ├── state.py            # EngineState, NodeFrame, TurnRecord (§9)
│   └── results.py          # TurnResult, Action union (§7)
├── templating.py           # {{var}} engine (§8)
├── validation.py           # save/publish validation (§11)
├── prompt.py               # prompt assembly (§5.7)
├── router.py               # global check, routing, loop condition scoring (§5.2–§5.5)
├── extraction.py           # variable extraction (§5.3)
├── handlers/               # one module per node type (§4)
│   ├── base.py  default.py  wait_for_response.py  webhook.py  knowledge_base.py
│   ├── transfer_call.py  end_call.py  sms.py  custom_code.py  press_button.py
│   ├── transfer_pathway.py  scheduling.py
├── task_runner.py          # task-mode runner (§6)
├── engine.py               # ConversationEngine orchestrating turns (§5)
├── speculative.py          # speculative generation coordinator (§5.8)
├── logs.py                 # pathway_logs builder (§12)
└── io.py                   # EngineIO protocol (§10)
```

## 2. Concepts

- A **graph** is the JSON stored in `pathway_versions.nodes`, `.edges`, `.global_config`.
- Exactly one node has `isStart: true`. Nodes with `isGlobal: true` are reachable from any node through an implicit
  edge labelled with the node's `globalLabel`.
- An **edge** is directed `source → target` with a natural-language `label` (the condition the router evaluates).
- The engine keeps a **current node** and a **return stack** (for global nodes and Transfer Pathway).
- Variables live in a flat-with-nesting `dict[str, JsonValue]` bag (`EngineState.variables`).

## 3. JSON schemas

### 3.1 `PathwayNode`

Field names are camelCase inside `data` exactly as Bland stores them so Bland pathway exports import unchanged.

```json
{
  "id": "node_7f3a",                      // string, 1–64 chars, [A-Za-z0-9_-]; unique in graph
  "type": "Default",                      // NodeType, see §3.3
  "position": {"x": 120.0, "y": 340.0},   // editor only
  "width": 320, "height": 180,            // editor only, optional
  "data": {
    "name": "Greeting",                   // required, 1–120 chars, unique in graph (case-insensitive)
    "isStart": false,
    "isGlobal": false,
    "globalLabel": null,                  // required when isGlobal; natural-language trigger condition
    "enableGlobalAutoReturn": true,       // global nodes: return to previous node after executing (default true)
    "prompt": "Greet the caller and ask how you can help.",   // dynamic generation instructions
    "text": null,                         // static text; when non-empty it is spoken verbatim (templated) and `prompt` is ignored
    "condition": null,                    // loop condition; stay on node until met
    "conditionThreshold": 6,              // 0–10 score at/above which the condition counts as met (default 6)
    "extractVars": [                      // variables extracted from the conversation at this node
      {"name": "first_name", "type": "string", "description": "Caller's first name", "required": false}
    ],
    "extractVarsMode": "before_response", // "before_response" | "background" (non-blocking; result usable from next turn)
    "pathwayExamples": [                  // routing fine-tuning examples
      {"conversation": [{"role":"assistant","content":"…"},{"role":"user","content":"…"}], "chosenLabel": "Wants a reservation"}
    ],
    "conditionExamples": [
      {"conversation": [{"role":"user","content":"It's Mike."}], "conditionMet": true, "explanation": "Name given"}
    ],
    "dialogueExamples": [
      {"conversation": [{"role":"user","content":"Do you take walk-ins?"}], "assistantResponse": "We do, but…"}
    ],
    "modelOptions": {
      "temperature": 0.7,                 // 0–1; null = inherit global/call
      "interruptionThreshold": null,      // ms (legacy fixed wait) null = adaptive
      "skipUserResponse": false,          // after speaking, immediately evaluate outgoing edges without waiting for user
      "block_interruptions": false,
      "resumption_speed": 2,              // 1 fast, 2 medium, 3 slow (endpointing aggressiveness, see 07-media-pipeline.md)
      "interruptibility": null,           // 0–3 per node; null = call-level `interruptibility`
      "model": null                       // dialogue model override (must be an allowed model for the org)
    },
    "enableForwarding": false,            // global nodes / any node: jump to forwardingNode after executing
    "forwardingNode": null,               // node id OR {"pathway_id": "...", "version": "production"|int, "node_id": "..."?}
    "kb": null,                           // Knowledge Base node: "KB-<uuid>" or array of them
    "kbTopK": 5,
    "tools": [],                          // array of "TL-<uuid>" / inline tool objects available to the LLM at this node
    "webhook": null,                      // Webhook node config §3.2.1
    "transfer": null,                     // Transfer Call config §3.2.2
    "transferNumber": null,               // legacy alias of transfer.number (Bland); normalized on load
    "endCall": null,                      // End Call config §3.2.3
    "sms": null,                          // SMS config §3.2.4
    "pressButton": null,                  // Press Button config §3.2.5
    "transferPathway": null,              // Transfer Pathway config §3.2.6
    "scheduling": null,                   // Scheduling config §3.2.7
    "customCode": null,                   // Custom Code config §3.2.8
    "waitForResponse": null,              // Wait for Response config §3.2.9
    "maxVisits": 8,                       // loop protection; 1–100
    "tag": {"name": "Greeting", "color": "#4F46E5"}   // shown in pathway_logs/dashboard
  }
}
```

Pydantic model `PathwayNodeData` holds every field above with the defaults shown; unknown keys are preserved in
`extra` (model_config `extra="allow"`) so round-tripping Bland exports never loses data.

#### 3.2.1 `webhook`

```json
{
  "url": "https://api.acme.com/lookup?phone={{from}}",
  "method": "POST",                         // GET|POST|PUT|PATCH|DELETE
  "headers": {"Authorization": "Bearer {{request_data.token}}"},
  "body": {"name": "{{first_name}}"},       // JSON, templated recursively (strings only)
  "timeoutMs": 10000,                       // 100–60000
  "retries": 0,                             // 0–3
  "speech": "One moment while I check that.",  // said while request runs (optional)
  "responseData": [                         // JSONPath extraction into variables
    {"name": "account_status", "data": "$.account.status", "context": "Customer account status"}
  ],
  "routeOnStatus": {"2xx": null, "4xx": "node_err", "5xx": "node_err", "timeout": "node_err"},
  "responsePathways": [                     // conditional routing on extracted values (evaluated in order)
    {"variable": "account_status", "operator": "==", "value": "active", "targetNodeId": "node_active"}
  ]
}
```

Operators: `==`, `!=`, `>`, `>=`, `<`, `<=`, `contains`, `not_contains`, `is_empty`, `is_not_empty`.

#### 3.2.2 `transfer`

```json
{
  "mode": "cold",                         // "cold" | "warm"
  "number": "+14155550100",               // E.164, SIP URI, or {{variable}}; or key of call `transfer_list`
  "speech": "Transferring you now.",      // final line before transfer
  "speechMode": "static",                 // "static" | "prompt"
  "speechPrompt": null,                   // used when speechMode=="prompt" (speech is fallback)
  "callerIdMode": "agent_number",         // "agent_number" | "original_caller"
  "warm": {                               // used when mode=="warm"
    "briefingPrompt": "Brief the human agent: the caller {{first_name}} wants {{intent}}.",
    "holdAudio": "hold_music",            // "hold_music" | "continue_conversation" | "silence"
    "targetTimeoutSecs": 30,
    "onTargetNoAnswer": {"action": "return_to_agent", "nodeId": null}  // "return_to_agent" | "end_call" | "move_to_node"
  }
}
```

#### 3.2.3 `endCall`

```json
{"speech": "Thanks for calling. Goodbye!", "speechMode": "static", "speechPrompt": null}
```

#### 3.2.4 `sms`

```json
{"to": "{{from}}", "from": null, "message": "Here is your link: {{booking_url}}", "messageMode": "static",
 "messagePrompt": null, "speech": "I just texted you the link.", "continueOnFailure": true}
```

`from` null = the call's agent number (must be SMS-capable; else org default SMS number).

#### 3.2.5 `pressButton`

```json
{"digits": "1w2#", "speech": null, "durationMillis": 250}
```
Digits allowed `0-9*#ABCDwW` (w = 0.5 s pause, W = 1 s).

#### 3.2.6 `transferPathway`

```json
{"pathwayId": "<uuid>", "version": "production", "nodeId": null, "carryVariables": true, "returnOnEnd": false}
```

`version`: `"production"`, `"staging"`, or integer. `nodeId` null = target start node.

#### 3.2.7 `scheduling`

```json
{
  "calendarProvider": "webhook",          // only "webhook" in v1: availability + booking via customer URLs
  "availabilityUrl": "https://api.acme.com/slots?date={{requested_date}}",
  "bookingUrl": "https://api.acme.com/book",
  "headers": {},
  "timezone": "{{timezone}}",
  "slotDurationMinutes": 30,
  "lookaheadDays": 14,
  "speech": "Let me check what's available.",
  "confirmationPrompt": "Confirm the booked time with the caller in their timezone.",
  "onFailureNodeId": null
}
```
Availability response contract: `{"slots":[{"start":"ISO8601","end":"ISO8601"}]}`. Booking request body:
`{"start","end","name","phone","email","notes","call_id"}`; response `{"booking_id","start","end"}` → variables
`booking_id`, `booking_start`, `booking_end`.

#### 3.2.8 `customCode`

```json
{"language": "typescript", "code": "export default async function (ctx) { return { variables: { total: ctx.variables.qty * 9.5 } }; }",
 "timeoutMs": 1000, "networkAllowlist": [], "speech": null}
```
Runtime in `13-tools-and-dynamic-data.md` §8.

#### 3.2.9 `waitForResponse`

```json
{"waitSeconds": 30, "repromptText": "Are you still there?", "maxReprompts": 2, "onTimeout": "end_call"}
```
`onTimeout`: `"end_call"` | `"move_to_node"` (with `"timeoutNodeId"`) | `"repeat"`.

### 3.2 `PathwayEdge`

```json
{
  "id": "edge_19c",
  "source": "node_7f3a",
  "target": "node_8b10",
  "type": "custom",                      // editor only
  "data": {
    "label": "Caller wants to make a reservation",   // required unless isFallback
    "description": "",                    // extra routing guidance shown to router
    "isFallback": false,                  // taken when no other edge applies (max one per source)
    "alwaysPick": false,                  // unconditional edge (skip router) — max one per source, excludes others
    "priority": 0                         // tie-break, higher first
  }
}
```

### 3.3 `NodeType` enumeration

`Default`, `Wait for Response`, `Webhook`, `Knowledge Base`, `Transfer Call`, `End Call`, `SMS`, `Custom Code`,
`Press Button`, `Transfer Pathway`, `Scheduling`, `Global`. (`Global` is a Default node with `isGlobal:true`; the
editor exposes it as a palette entry; on load `type:"Global"` normalizes to `Default` + `isGlobal:true`.)
Aliases accepted on import: `"Transfer Node"`→`Transfer Call`, `"End Node"`→`End Call`, `"Knowledge Base Node"`→
`Knowledge Base`, `"Webhook Node"`→`Webhook`.

### 3.4 `GlobalConfig` (`pathway_versions.global_config`)

```json
{
  "globalPrompt": "You are Ava, a friendly receptionist for Bella Pasta…",
  "applyGlobalPromptToAllNodes": true,
  "defaultTemperature": 0.7,
  "defaultVoice": null,                  // voice name/uuid used when call does not specify
  "defaultLanguage": null,
  "memoryEnabled": false,
  "globalTools": [],                     // tool ids available on every node
  "globalKb": null,                      // KB ids searched on every node (in addition to node kb)
  "maxTotalTurns": 200,
  "errorPhrase": "I'm sorry, I'm having trouble right now.",
  "routerModel": null,
  "dialogueModel": null,
  "noMatchBehavior": "stay"              // "stay" | "fallback_edge"
}
```

## 4. Node type runtime semantics

Every handler implements:

```python
class NodeHandler(Protocol):
    node_type: NodeType
    async def on_enter(self, ctx: TurnContext) -> list[Action]: ...      # executed when the node becomes current
    async def respond(self, ctx: TurnContext) -> list[Action]: ...       # produce the agent's reply for this turn
    def waits_for_user(self, node: PathwayNode) -> bool: ...
```

| Type | `on_enter` | `respond` | Waits for user | Text mode (SMS/widget) |
|---|---|---|---|---|
| **Default** | nothing | If `text` non-empty → `Say(render(text))`; else stream dialogue LLM with assembled prompt (§5.7) → `SayStream`. Tools listed on node are offered as functions. | yes unless `skipUserResponse` | same (text output) |
| **Wait for Response** | arm wait timer `waitSeconds` | if `prompt`/`text` present, speak it; timer expiry with no user speech → speak `repromptText` up to `maxReprompts`, then `onTimeout` | yes | wait translates to conversation inactivity; timeout ignored unless `onTimeout=end_call` → closes conversation after `waitSeconds*10` |
| **Webhook** | run extraction (if `extractVars`) **before** request; `Say(speech)` concurrently if set; execute HTTP (`13-tools` runtime) ; map `responseData`; emit `call.webhook_node`; choose route: first matching `responsePathways` → `routeOnStatus` → normal router | if node has `prompt`/`text`, respond using response vars; if neither, immediately route (acts as `skipUserResponse`) | no if no prompt/text | same, `speech` dropped |
| **Knowledge Base** | none | retrieve top `kbTopK` chunks for last user message (plus node prompt hint) via `va_kb.retrieve` (cache per call/query hash); inject `<knowledge>` block (14-knowledge-bases.md); stream LLM | yes | same |
| **Transfer Call** | — | speak final line (`speech` or composed via `speechPrompt`), then emit `TransferAction(mode, target, caller_id_mode, warm)` executed after playback mark echoes | n/a (terminal for AI unless warm returns) | SMS: send message "A team member will reach out shortly." (configurable `textModeMessage`) and set conversation `human_takeover`; widget: switch to `human_takeover` |
| **End Call** | — | speak final line, then `EndCallAction(reason="agent_end_call")` after playback | n/a | closes conversation after sending the line |
| **SMS** | render message (static or LLM `messagePrompt`), `SendSmsAction`; on failure: if `continueOnFailure` continue else route to fallback edge; speak `speech` | default respond if prompt/text present else immediate route | per prompt | in SMS channel the message is just sent as the next message |
| **Custom Code** | run sandbox with ctx (§13 §8); merge returned `variables`; `say` → `Say` | if prompt/text present respond; else immediate route | no unless prompt/text | same |
| **Press Button** | `SendDtmfAction(digits, duration)`, optional `speech` before | immediate route | no | ignored (logged warning), immediate route |
| **Transfer Pathway** | push frame (if `returnOnEnd`), load target graph version, set current node = target `nodeId` or start; if `carryVariables=false` reset to built-ins+request_data | delegated to new current node | per target | same |
| **Scheduling** | speak `speech`; GET availability; inject slots into prompt; LLM negotiates; when LLM calls built-in `book_slot(start)` function → POST booking → variables; failure → `onFailureNodeId` | LLM with scheduling functions `list_slots`, `book_slot` | yes | same |
| **Global** | push `NodeFrame(current)` on return stack | as Default | as Default | same |

After a global node finishes its response: if `enableForwarding` → jump to `forwardingNode`; else if
`enableGlobalAutoReturn` → pop return stack and set current node to the popped node **without re-speaking** it
(next user turn is evaluated against the popped node's edges); prompt context includes
`{{prevNodePrompt}}` = popped node's prompt so the LLM can resume naturally.

> Decision (2026-11-03): global nodes and return stack (P07-T05). `va_engine.navigation.Navigator(state,
> ActiveGraph(pathway_id, version, graph), loader=PathwayLoader | None)` moves the state; it emits no events and
> calls no LLM. `apply(RouteDecision)` and `finish_node()` return a `Transition` (`kind` stay|enter|global|return|
> forward, `speaks`, `visits`, `over_limit`, `pathway_changed`, `problem`).
> 1. Return frames carry `kind: "global" | "transfer_pathway"` (EngineState schema version 3, §9). Entering a global
>    node from the router pushes `NodeFrame(kind="global", pathway_id, version, node_id=current)` of the active graph.
>    The §5.2 global check runs only when the current node is not global and the graph has another global node, so
>    globals never nest directly; "nested" means a global triggered from a node reached after an earlier global.
> 2. After a node executed: `enableForwarding` with a `forwardingNode` (any node type) forwards — a node id inside
>    the active graph, or `{pathway_id, version, node_id}` loaded through the `PathwayLoader` (`node_id` null = the
>    target's start node; the state's pathway id/version switch, variables are kept, `graph_stack_versions` is
>    untouched). Forwarding out of a global node discards its global frame (no return). Otherwise a global node with
>    `enableGlobalAutoReturn` pops the top frame when it is a global frame of the active pathway and makes its node
>    current without re-speaking it and without a visit; a global node without such a frame (reached by an edge)
>    stays. Otherwise the conversation stays on the node.
> 3. Leaving a global node by an edge or the fallback edge (auto-return off) discards its global frame. A
>    `transfer_pathway` frame is never popped by the auto-return.
> 4. Entering a node (edge, fallback, global, forward) increments `visit_counts[node_id]`; `over_limit` = visits >
>    `maxVisits` (the turn orchestration applies loop protection). Returning never counts.
> 5. `{{prevNodePrompt}}` while a global node runs = the prompt (else text, else `""`) of the node in the top global
>    frame when that frame belongs to the active pathway; `None` otherwise.
> 6. The return stack holds at most 16 frames; a push beyond drops the bottom frame (`engine.return_stack_trimmed`).
> 7. Broken targets never raise: a route target or forward id not in the graph, a target pathway without the node /
>    start node, a loader failure (`PathwayUnavailableError`, logged `engine.pathway_load_failed`), or a missing
>    return target → stay on the current node with `problem` = `target_missing` | `forward_target_missing` |
>    `pathway_unavailable` | `return_target_missing`, logged `engine.navigation_failed` with the reason; the popped
>    frame of a missing return target is dropped, a failed forward keeps the global frame.
> 8. State upgrade 2 → 3: each v2 frame gets `kind = "global"` when its `pathway_id` equals the state's
>    `pathway_id` (both may be null), else `"transfer_pathway"`; a v2 frame that already carries `kind` is rejected
>    (`EngineStateError` `newer_field`). Frozen fixture `packages/va_engine/tests/fixtures/engine_state/v3.json`.

## 5. Per-turn algorithm (pathway mode)

Triggered by `engine.on_user_turn(UserTurn(text, words, started_ms, ended_ms, language))` after EOT
(`07-media-pipeline.md`). Also triggered with `UserTurn(text="")` for `skipUserResponse` chaining and wait timeouts.

### 5.1 Barge-in truncation
If the previous assistant turn was interrupted, `EngineState.history[-1].content` is replaced with
`text_spoken` (provided by the playback tracker, alignment-accurate) and `interrupted=True` is recorded. The
truncated portion is never shown to the LLM.

### 5.2 Global node check
Skipped when the current node is itself a global node being executed or when the graph has no global nodes.
One router call (`10-llm-gateway.md`, router call contract) with candidates = all global labels **plus** the current
node's outgoing labels, so the model chooses in context. If the winning candidate is a global node with score ≥ 6
and it outscores the best local edge: push `NodeFrame(current_node_id)` and jump to the global node. Global
precedence: on a tie, the global node wins (Bland: "take precedence over the condition decisions").
The combined call's result is reused by §5.5 (no second routing call) when a local edge wins.

### 5.3 Variable extraction
If the current node has `extractVars` and mode `before_response`: router-model call with forced function
`extract_variables` whose JSON schema is built from `extractVars` (types map: string→string, integer→integer,
number→number, boolean→boolean, array→array of strings). Missing values return `null`. Extracted non-null values
merge into `variables` (overwriting). Emits `call.extraction`. Mode `background`: same call scheduled as a task;
results merge when complete and are emitted then.

Extraction system prompt (verbatim):
```
You extract structured variables from a phone conversation. Return only values the caller (or the
conversation) explicitly established. If a value is not clearly present, return null. Do not guess.
Today is {{now_utc}} (timezone {{timezone}}). Variable definitions:
{% for v in vars %}- {{v.name}} ({{v.type}}{% if v.required %}, required{% endif %}): {{v.description}}
{% endfor %}
```
(Internal Jinja template — not customer templating.)

### 5.4 Loop condition
If node has `condition`: router-model call with forced function `score_condition` →
`{"score": int 0..10, "reason": str}`. System prompt (verbatim):
```
Decide how likely it is that the following condition has been satisfied by the conversation so far.
Condition: "{{condition}}"
Score from 0 (definitely not satisfied) to 10 (definitely satisfied). Consider only the conversation.
{% if examples %}Examples:
{% for e in examples %}- Conversation: {{e.conversation_text}} → satisfied: {{e.conditionMet}} ({{e.explanation}})
{% endfor %}{% endif %}
```
`met = score >= conditionThreshold`. Emits `call.loop_condition` with message `"Likely {score}/10"`. Not met →
skip §5.5 and respond on the current node (the LLM is told via prompt suffix `The following condition is not yet
satisfied; keep working toward it: "{condition}"`).

### 5.5 Routing
Candidates = outgoing edges of current node (excluding `isFallback`). Rules in order:
1. `alwaysPick` edge exists → take it, no LLM call.
2. Webhook `responsePathways`/`routeOnStatus` match → take it.
3. No candidates → stay.
4. Router call (forced function `select_path`) with numbered labels plus option `0: "None of these — stay on the
   current step"`; returns `{"choice": int, "scores": [{"index": int, "score": int 0..10}], "reason": str}`.
   Few-shot from `pathwayExamples`. System prompt (verbatim):
```
You are the routing component of a phone agent. Based on the conversation, decide which next step applies.
Current step: "{{node_name}}" — {{node_prompt_short}}
Options:
0: None of these — stay on the current step
{% for e in edges %}{{loop.index}}: {{e.label}}{% if e.description %} ({{e.description}}){% endif %}
{% endfor %}
Pick the single best option. Prefer 0 unless the caller's latest message clearly matches an option.
```
5. choice 0 or best score < 5 → fallback edge if present and `noMatchBehavior=="fallback_edge"` or the node has a
   fallback edge; else stay.
6. Ties broken by `priority` desc then edge order.

Emits `call.route` with `{from_node, to_node, label, alternatives:[{label, score}], global:false, router_ms}`.
Entering a node increments `visit_counts[node_id]`; if it exceeds `maxVisits`, the engine routes to the fallback edge
if any, else speaks `errorPhrase` and ends the call (`end_reason="loop_protection"`) — an `end_reason` value defined
in `05-calls-api.md` §9.

> Decision (2026-11-03): decision calls (P07-T04). `va_engine.router.Router.choose`, `va_engine.loop_condition.
> LoopCondition.score` and `va_engine.extraction.Extractor.extract` are pure decision components: inputs → typed
> result (no state mutation, no events; the turn orchestration applies them and emits `call.route`/
> `call.loop_condition`/`call.extraction` from their `event_data`). They talk to the gateway through
> `va_engine.decisions.StructuredPort` (`LlmGateway.call_structured`), with call types `router`, `loop_condition`,
> `extraction`.
> 1. Prompts, function names, and output schemas are specs/10 §9.1–§9.3 (`va_llm` `prompts/router.txt`,
>    `loop_condition.txt`, `extract.txt`, rendered by `va_llm.prompts.render_prompt`); the inline prompts of
>    §5.3–§5.5 here are superseded. Router choice 0 ≡ `path_id: "stay"`; global candidates are `global:{node_id}`.
>    User message: the last 8 (router) / 10 (loop condition, extraction) user/assistant history turns as
>    `Caller: …`/`Agent: …` lines, then (router only) `Known information: {compact JSON, sorted keys, keys starting
>    with "_" left out}`, then `Latest caller message: "{text}"`. `{node_purpose}` = node name + `: ` + the first 300
>    characters of the node prompt (static `text` when there is no prompt; the name alone when neither).
> 2. Router examples: a `pathwayExamples` entry maps `chosenLabel` to the first candidate edge whose label equals it
>    (case-insensitive, stripped); `x` is the example's last user message; entries matching no edge or without a
>    user message are skipped.
> 3. Rules, in order: an `alwaysPick` edge → `edge` (no LLM); a webhook-selected edge → `edge`; no candidate edges
>    and no global candidates → the fallback edge when the node has one, else `stay` (no LLM). Global candidates
>    are offered when the caller asks for them (§5.2), the node is not global, and the graph has another global
>    node. Router result: scores = {chosen: confidence} ∪ alternatives (unknown ids ignored, first occurrence
>    wins, missing = 0; `stay` is scored like any option). Outcome `global` when the best global candidate (ties:
>    graph order) scores ≥ 6 and at least as high as the best candidate edge and `stay` (global precedence);
>    else `edge` when the best candidate edge (ties: `priority` desc, then edge order) scores ≥ 5 and higher than
>    `stay` (a tie with `stay` stays); else `fallback` (every node with a fallback edge, whatever
>    `noMatchBehavior`); else `stay`. `alternatives` = every scored candidate edge and global node except the
>    winner, score desc then option order (empty when no router call was made).
> 4. Loop condition: met when `score ≥ conditionThreshold`; the model's `met` is stored as `model_met` only.
>    `conditionExamples` render as the specs/10 §9.2 `{examples_block}`. Not met → prompt suffix
>    `The following condition is not yet satisfied; keep working toward it: "{condition}"`.
> 5. Extraction: per-node schema built from `extractVars` (all required; integer/number/boolean also accept a
>    string, every type accepts null; JSON property names are the variable names). After validation strings are
>    coerced: string → stripped (empty → null); integer ← integers, integral floats, digit strings (`1,200`), and
>    spoken numbers (`twenty five`, `a hundred and five`); number ← numbers, numeric strings, spoken integers;
>    boolean ← yes/no words (`yes yeah yep yup true correct sure right` / `no nope nah false incorrect`) as the
>    first word with no word of the other side. Integers must fit a signed 64-bit integer and digit strings may
>    carry at most 18 digits before the decimal point (longer ones are rejected, never converted). Values that fail
>    coercion become null and are listed in `rejected`. `{now_local}` = `%A %Y-%m-%d %H:%M` in the zone (English weekday names; an unknown zone → UTC).
>    `background` vs `before_response` scheduling and the overwrite merge are the caller's.
> 6. Failures never raise: on `LlmUnavailableError`/`StructuredOutputError` the router applies the specs/10 §9.1
>    heuristic (exactly one candidate edge label sharing ≥ 2 content words with the caller's latest message →
>    that edge, else rule 3's fallback/stay; `degraded=True`), the loop condition returns `met=False`,
>    `score=None` (message `Condition check unavailable`), extraction returns no values; each logs
>    `engine.router_degraded` / `engine.loop_condition_degraded` / `engine.extraction_degraded` with `reason`,
>    never conversation text.
> 7. Dynamic schemas: one pydantic class per distinct option tuple / variable tuple
>    (`va_engine.decision_schemas`, LRU 256); they are not registered with `register_structured_function`. The
>    `va_llm.structured` tool-schema and validator caches are LRU 1 024 so per-graph classes stay bounded.

### 5.6 Execute
If the node changed: run `on_enter` of the new node (actions executed in order; `Say` actions queue to TTS). Then
`respond` of the current node unless `on_enter` produced a terminal action. If the node's handler says it doesn't
wait for user (or `skipUserResponse`), after the response's playback completes the engine re-enters §5.2 with an
empty user turn (chain). Chain depth limit: 10 per user turn (`end_reason="loop_protection"` beyond).

### 5.7 Prompt assembly (dialogue LLM messages)

```
system:
  [1] Global prompt (if applyGlobalPromptToAllNodes or node has no prompt)       — templated
  [2] Persona/agent identity block (from task runner/persona) if any
  [3] "## Current step: {node_name}\n{node_prompt}"                                  — templated
  [4] Loop-condition suffix (§5.4) if not met
  [5] Memory context block (20-memory-personas-agents.md §2.4) if enabled
  [6] <knowledge> block (KB node / globalKb) if any
  [7] Dialogue examples rendered as "Example exchange:\nCaller: …\nYou: …"
  [8] Variables block: "## Known information\n- first_name: Mike\n…" (excluding secrets/request_data keys prefixed "_")
  [9] Runtime facts: "Current time: {now_local} ({timezone}). Caller number: {from}. You are on a phone call;
      keep replies short (1–2 sentences), conversational, no markdown, no lists, spell numbers naturally."
      Text mode replaces the last sentence with "You are chatting by text; keep replies concise; no markdown."
 [10] Pronunciation hints are NOT added here (applied at TTS, 09-tts-elevenlabs.md)
messages: last 40 history turns (system-level events like transfers summarized as assistant notes)
tools: node tools + global tools + built-in system tools allowed at this node (13-tools §6)
```
Static prefix ([1]–[3],[7]) ordering is kept stable to maximize provider prompt caching.

### 5.8 Speculative generation
When a turn starts, in parallel:
- **A**: §5.2–§5.5 decision pipeline (router calls).
- **B**: speculative dialogue generation on the **current** node (prompt assembled as if staying), streaming tokens
  into a buffer but **not** to TTS.

When A completes: if it stays on the current node (and no extraction-dependent template variables changed) → B is
committed: buffered tokens flush to TTS immediately, stream continues. Otherwise B is cancelled (HTTP stream closed)
and a fresh generation runs for the new node. Static-text nodes skip B. Speculation disabled when the node has
`extractVars` with mode `before_response` referenced in its prompt (detected by template variable scan), or org
setting `engine.speculative=false`. Metrics: `speculation_hit` / `speculation_miss` counters.

### 5.9 Events per turn
`call.turn` (user), [`call.route`|`call.loop_condition`|`call.extraction`|`call.tool`|`call.webhook_node`]*,
`call.turn` (assistant, after playback ends or is interrupted), `call.latency` set.

## 6. Task-mode runner

Used when the call has `task` (or persona/agent resolving to a prompt). A synthetic single-node graph:
node `task_root` type Default with `prompt = task`, global prompt empty, tools = call `tools` + built-ins
(`end_call`, `transfer_call` if `transfer_phone_number`/`transfer_list` set, `press_button`, `send_sms` if
SMS-capable number, `lookup_knowledge` if KB ids in tools). `first_sentence` spoken verbatim (templated) at call
start. No routing calls; extraction only via tools. All other behavior identical (prompt assembly §5.7 [1] empty).

> Decision (2026-11-02): task-mode runner (P05-T09c). `va_engine.task_runner.TaskBrain` implements the
> `va_voice.pipeline.Brain` protocol.
> 1. Opener: `first_sentence` rendered with the templating engine (§8) and spoken as `SpeakStatic`; when blank
>    the model writes the opener (one turn without a user message). A template with a syntax error is used as
>    raw text (`template_invalid` log); each missing variable path is logged once per call.
> 2. Every request renders the specs/10 §5 Voice System Prompt (instructions = templated `task`, variables =
>    `request_data` overlaid by the call's variables, now/timezone/language from the config, counterpart = the
>    callee for outbound and the dialled agent number for inbound calls), fitted with `fit_voice_system` (the
>    tool note and tool JSON count as extra tokens), plus the tool usage note when tools are offered and the
>    history trimmed by `ConversationWindow(llm.context_budget_tokens)`. Model, temperature and `max_tokens`
>    come from the resolved config.
> 3. Tools = `system_tools(facts)` + the custom tools of the `ToolExecutor` (`va_engine.tool_executor`). Without
>    an executor `send_sms`, `schedule_callback` and `lookup_knowledge` are not offered (the phase 08 ToolRunner
>    executes them through the executor, as it does custom tools).
> 4. Tool loop per user turn (specs/10 §7): at most 3 rounds with tools; after the third, after two rounds that
>    contained an invalid call, or once the call made 100 tool calls, the next request offers no tools (tool
>    calls returned anyway are ignored). A round executes at most 4 calls (later ones are answered
>    `{"error":"too_many_tool_calls"}`); executor calls run concurrently (at most 3 at once) and every call is
>    answered in call order. `press_button` answers `{"status":"sent","digits":…}` and hands
>    `PressButton(digits)` to the action handler. The first `end_call`/`transfer_call` of a round is terminal;
>    later calls are answered `{"error":"skipped"}`. A `transfer_call` whose label does not resolve is answered as
>    invalid arguments (`label: unknown transfer label`). Each assistant message with tool calls is followed by
>    exactly one tool message per call; calls left unanswered when the reply is cut off are answered
>    `{"error":"interrupted"}`.
> 5. `end_call` sets `EngineState.ended`/`end_reason = "agent_end_call"` and ends the call after its final line
>    (`final_message`, else `"Goodbye."` unless text was already spoken this turn). `transfer_call` speaks its
>    `message` (not interruptible), then hands `TransferRequest(target, label, message)` to the action handler;
>    the turn ends. Both act only after the spoken part of the reply finished playing.
> 6. An LLM that is unavailable before the first token (both targets) makes the runner speak `error_phrase`; a
>    failure after the first token keeps the spoken text and drops the round's tool calls.
> 7. Barge-in (§5.1): the interrupted assistant message keeps only the heard text ending in `—` (`—` alone when
>    nothing was heard) and its history record is marked `interrupted`. A turn that supersedes an earlier one
>    (specs/07 §8) removes the earlier user message and everything after it. DTMF turns become
>    `[Caller pressed: …]` user messages. `on_no_input` yields nothing (the pipeline reprompts); timing uses the
>    call-level `PipelineConfig` (no node overrides).

## 7. Actions

```python
Action = Say | SayStream | SendDtmfAction | TransferAction | EndCallAction | SendSmsAction | SetVariables |
         PlayAudio | Wait | SwitchGraph | HumanTakeover
```
- `Say(text, interruptible: bool | None, mark: str)`; `SayStream(async_iter_tokens, ...)`.
- `TransferAction(mode, target_e164_or_sip, caller_id_mode, warm: WarmTransferConfig | None)`.
- `EndCallAction(reason: EndReason, ended_by: "agent"|"guard_rail"|...)`.
- `SendSmsAction(to, from_, body)`; `SendDtmfAction(digits, duration_ms)`.
Terminal actions (`TransferAction` cold, `EndCallAction`) execute only after the preceding `Say` playback mark
echoes (`EngineIO.wait_playback(mark)`), with a 15 s safety timeout.

## 8. Templating engine (`va_engine.templating`)

Customer-facing syntax `{{ expression }}` (whitespace optional). Grammar:
```
expression := path ( "|" filter )*
path       := ident ( "." ident | "[" int "]" )*
filter     := "default:" quoted | "upper" | "lower" | "title" | "json" | "phone_spoken" | "date:" quoted | "trim"
```
- Resolution order: `input.*` (tool args, only inside tool templates) → node-local vars → `variables` (extracted,
  webhook response, custom code) → `request_data` (also addressable as `request_data.x`) → built-ins.
- Built-ins: `lastUserMessage`, `prevNodePrompt`, `now_utc` (ISO 8601), `now_local` (ISO in call timezone),
  `today` (YYYY-MM-DD local), `from`, `to`, `call_id`, `voice_id`, `timezone`, `language`, `agent_number`,
  `user_number`, `batch_id`, `metadata.*`.
- Missing variable without default → empty string, and a `call.error` event (level `warning`, code
  `TEMPLATE_VAR_MISSING`, data `{var}`) emitted once per variable per call. In URLs, values are percent-encoded;
  in JSON bodies, non-string values keep their type when the whole string is a single expression
  (`"{{qty}}"` with qty=3 → `3`).
- `phone_spoken`: `+14155550100` → `"4 1 5, 5 5 5, 0 1 0 0"`. `date:"%B %d"` strftime in call tz.
- Escaping: `\{{` emits literal `{{`.
- Implementation: hand-written tokenizer + recursive resolver, no `eval`, max template length 20 000 chars,
  max 500 expressions.

> Decision (2026-11-02): templating and engine state (P05-T09a). `va_engine.templating.parse_template(text)` parses
> once and raises `TemplateSyntaxError(message, position)` (a `ValueError`; `TemplateLimitError` for > 20 000 chars or
> > 500 expressions) for an unterminated `{{`, an empty expression, a bad path, an unknown filter, or a `default:`/
> `date:` argument that is not a double- or single-quoted string (`\` escapes the next character inside quotes).
> Whitespace is allowed around every token; `\{{` renders `{{`, any other backslash and a `}}` outside an
> expression are literal; list indexes are non-negative. A path's first segment is found in the first layer that
> has the key (a stored `null` counts as found and renders `""`); the name `request_data` itself resolves to the
> whole mapping after the `request_data` keys and before built-ins; any later miss (absent key, index out of range,
> indexing a scalar) is "missing". Values render as: strings as-is, `null` → `""`, booleans `true`/`false`, numbers
> and lists/objects as compact JSON. Filters run left to right; `default:"x"` replaces a missing, `null`, or `""`
> value and clears the missing report; other filters leave a missing value missing. `json` emits the value as JSON,
> `phone_spoken` speaks each group of the national format (parsed with region US when there is no `+`;
> `+442071838750` → `0 2 0, 7 1 8 3, 8 7 5 0`; unparseable input → every digit space-joined, no digits → unchanged),
> `date:"fmt"` reads ISO 8601 datetimes (aware ones converted to the call timezone, naive ones taken as local) or
> dates and keeps the value unchanged when it cannot parse or format it; an unknown timezone falls back to UTC.
> Missing paths are reported through a per-call `MissingVariables` (sink called once per distinct canonical path,
> e.g. `a.b[0]`); the runner maps the sink to the `TEMPLATE_VAR_MISSING` warning. A runtime caller that hits a
> syntax error speaks/sends the raw text and logs `template.invalid`; save/publish validation reports it.
> `render_url` percent-encodes every expression value (`quote(s, safe="")`), never literal text; `render_json`
> templates string values and list items (never keys), and a string that is exactly one expression (no surrounding
> text or whitespace) yields the raw typed value (a filter's output is a string; missing → `""`). Built-ins come from
> `va_engine.builtins.call_builtins(context, now=, last_user_message=, prev_node_prompt=)`: `now_utc`/`now_local`
> are ISO 8601 with seconds precision, `voice_id` is the platform voice id, `agent_number` is `from_number` for
> outbound and web calls and `to_number` for inbound (`user_number` the other side), `metadata` is `config.metadata`.

> Decision (2026-11-03): templating robustness (P07-T02). Rendering never raises for any scope value: integers beyond
> 64 bits render as their digits, non-string mapping keys as their JSON text (`1` → `"1"`, `true` → `"true"`,
> `null` → `"null"`), non-finite floats as `null`, other non-JSON values as `str()`; the same applies to the `json`
> filter. `date:` keeps the value unchanged when the timezone conversion overflows (years 1 and 9999).
> `render`/`render_url` accept a parsed `Template` or template text (parsed through the 1 024-entry LRU cache
> `cached_template`, also used by `render_json` for string leaves); only parsing raises (`TemplateSyntaxError`).
> Golden cases, including every example of this section, live in
> `packages/va_engine/tests/fixtures/templating/golden.json`.

## 9. EngineState serialization

```python
class NodeFrame(BaseModel):
    kind: Literal["global", "transfer_pathway"]
    pathway_id: UUID | None
    version: int | None
    node_id: str

class TurnRecord(BaseModel):
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    node_id: str | None
    interrupted: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    at_ms: int

PendingAction = (PendingSay | PendingDtmf | PendingTransfer | PendingEndCall | PendingSms | PendingSetVariables
                 | PendingSwitchGraph | PendingHumanTakeover)   # discriminated by `kind`; each has `after_mark`

class EngineState(BaseModel):
    schema_version: Literal[3] = 3
    runner: Literal["task", "pathway"]
    pathway_id: UUID | None
    pathway_version: int | None
    current_node_id: str
    return_stack: list[NodeFrame] = []
    visit_counts: dict[str, int] = {}
    variables: dict[str, JsonValue] = {}
    history: list[TurnRecord] = []          # capped to 200; older summarized into `history_summary`
    history_summary: str = ""
    wait_reprompts: int = 0
    turn_index: int = 0
    ended: bool = False
    end_reason: str | None = None
    graph_stack_versions: dict[str, int] = {}   # pathway_id -> version for transfer-pathway frames
    pending_actions: list[PendingAction] = []   # decided, not yet executed
    wait_armed_at_ms: int | None = None         # when the current Wait for Response timer was armed
```

Serialized with `dump_engine_state()` into `conversations.engine_state` (SMS/widget) after every turn, and into Redis
`call:{id}:engine` (TTL 2 h) every turn for voice (for warm-transfer proxies and crash forensics). Final state merges
`variables` into `calls.variables`. Deserialization rejects unknown `schema_version`.

> Decision (2026-11-02): history cap (P05-T09a). Every engine model forbids unknown fields
> (`va_engine.models.load_engine_state` raises `ValidationError` for them and for an unknown `schema_version`).
> `EngineState.add_turn(record)` appends; once `history` exceeds 200 turns the oldest move into `history_summary` as
> `role: content` lines (newline-joined after the existing summary), and the summary keeps only its last 4 000
> characters. The fold is deterministic (no LLM call); an LLM summary may replace it later without a schema change.

> Decision (2026-11-03): versioned EngineState (P07-T03). Schema version 2 adds `pending_actions` (actions a runner
> decided but the executor has not run yet, in order: `say` {text, mark, interruptible}, `send_dtmf` {digits,
> duration_ms 250}, `transfer` {mode cold|warm, target, caller_id_mode}, `end_call` {reason, ended_by "agent"},
> `send_sms` {to, from, body}, `set_variables` {variables}, `switch_graph` {pathway_id, version, node_id},
> `human_takeover`; `transfer` and `end_call` are terminal) and `wait_armed_at_ms` (text channels persist the Wait for
> Response timer across turns). Every action has `after_mark`: `null` = due now, otherwise it runs once that playback
> mark echoed (§7); `EngineState.take_pending(mark)` removes and returns the due actions in order. The only writer is
> `va_engine.models.dump_engine_state` (`model_dump_json(by_alias=True)`, so the SMS sender key is `from`). The reader
> `load_engine_state` accepts JSON text or a decoded object (never mutated), upgrades older versions step by step
> (1 → 2 adds the two fields with their defaults; a v1 document that already carries a v2 field is rejected), and
> raises `EngineStateError` (`.problem`, `.version`) for invalid JSON, a non-object, or a missing, non-integer, < 1, or newer
> `schema_version`; unknown fields still raise `ValidationError`. Every released version keeps a frozen fixture
> `packages/va_engine/tests/fixtures/engine_state/vN.json` that must keep loading; a new field means a version bump,
> one upgrader, and a new fixture.

## 10. `EngineIO` protocol

```python
class EngineIO(Protocol):
    channel: Literal["voice", "sms", "widget_chat", "widget_voice"]
    async def say(self, text: str, *, mark: str, interruptible: bool | None) -> None: ...
    async def say_stream(self, tokens: AsyncIterator[str], *, mark: str, interruptible: bool | None) -> SpokenResult: ...
    async def wait_playback(self, mark: str, timeout_s: float) -> PlaybackResult: ...   # text channels return immediately
    async def execute(self, action: Action) -> ActionResult: ...
    async def emit(self, event: DomainEvent) -> None: ...
    def now_ms(self) -> int: ...
```

## 11. Validation (save draft = warnings allowed; publish = errors block)

| Rule | Code | Message | Save | Publish |
|---|---|---|---|---|
| Exactly one start node | `PATHWAY_START_NODE` | "Pathway must have exactly one start node (found {n})." | warn | error |
| Node ids unique | `PATHWAY_DUPLICATE_NODE_ID` | "Duplicate node id '{id}'." | error | error |
| Node names unique (ci) | `PATHWAY_DUPLICATE_NODE_NAME` | "Duplicate node name '{name}'." | warn | error |
| Edge endpoints exist | `PATHWAY_EDGE_DANGLING` | "Edge '{id}' references missing node '{node}'." | error | error |
| Non-fallback edge has label | `PATHWAY_EDGE_LABEL` | "Edge '{id}' needs a condition label." | warn | error |
| ≤1 fallback / alwaysPick per source; alwaysPick excludes other edges | `PATHWAY_EDGE_CONFLICT` | "Node '{name}' has conflicting fallback/always edges." | warn | error |
| All non-global nodes reachable from start (global nodes count as reachable) | `PATHWAY_UNREACHABLE` | "Node '{name}' is unreachable." | warn | warn |
| Global node has `globalLabel` | `PATHWAY_GLOBAL_LABEL` | "Global node '{name}' needs a global label." | warn | error |
| Default/KB/Wait nodes have prompt or text | `PATHWAY_NODE_EMPTY` | "Node '{name}' needs a prompt or static text." | warn | error |
| Webhook has valid https URL (http allowed in local/test) | `PATHWAY_WEBHOOK_URL` | "Webhook node '{name}' has an invalid URL." | warn | error |
| Transfer has number | `PATHWAY_TRANSFER_TARGET` | "Transfer node '{name}' needs a destination." | warn | error |
| KB ids exist in org | `PATHWAY_KB_MISSING` | "Knowledge base '{id}' not found." | warn | error |
| Tool ids exist | `PATHWAY_TOOL_MISSING` | "Tool '{id}' not found." | warn | error |
| Transfer Pathway target exists, not self-recursive without nodeId | `PATHWAY_TARGET_MISSING` | "Target pathway '{id}' not found." | warn | error |
| Custom code passes `deno check` | `CUSTOM_CODE_ERROR` | "Custom code in '{name}': {diagnostic}" | warn | error |
| Templates parse | `PATHWAY_TEMPLATE_SYNTAX` | "Invalid template in '{name}': {detail}" | error | error |
| extractVars names `^[a-z_][a-z0-9_]{0,63}$`, unique per node | `PATHWAY_VAR_NAME` | "Invalid variable name '{var}' in '{name}'." | error | error |
| Graph size ≤ 500 nodes, ≤ 2 000 edges, JSON ≤ 5 MB | `PATHWAY_TOO_LARGE` | "Pathway exceeds size limits." | error | error |

Validation result shape: `{"valid": bool, "errors": [{code, message, node_id?, edge_id?}], "warnings": [...]}`.
Publish failures return HTTP 400 `INVALID_INPUT` with these items in `errors[].details`.

> Decision (2026-11-03): node/edge schema and validation (P07-T01). Models live in `va_engine.models.nodes`
> (`PathwayNode` = a discriminated union on `type`, one class per §3.3 type, `PathwayEdge`, every §3.2.x config) and
> `va_engine.models.graph` (`GlobalConfig`, `PathwayGraph`, `load_graph`, `dump_graph`, `graph_hash`). Attributes
> are snake_case, JSON keys camelCase as Bland writes them (`modelOptions.block_interruptions`/`resumption_speed`
> and the `forwardingNode` object keep snake_case; `sms.from` is the attribute `from_`). Every model keeps unknown
> keys, and dumps write only the keys that were present (`exclude_unset`, numbers keep their JSON type), so a Bland
> export round-trips byte-identically in canonical form. Normalization happens before discrimination: the four
> import aliases become canonical types and `"Global"` becomes `"Default"` with `data.isGlobal: true`; the legacy
> `transferNumber` is kept as stored and read through `effective_transfer_number(data)` (`transfer.number` first).
> `modelOptions.temperature` defaults to `null` (inherit, §13; the §3.1 `0.7` is an example value). Bounds the spec
> leaves open: edge ids are 1–256 characters of any kind (Bland/React Flow ids such as `reactflow__edge-1-2`), Press
> Button `durationMillis` 50–5 000, warm `targetTimeoutSecs` 1–600, scheduling `slotDurationMinutes` 5–480 and
> `lookaheadDays` 1–90, Wait for Response `waitSeconds` 1–3 600 and `maxReprompts` 0–10, `kbTopK` 1–50,
> `maxTotalTurns` 1–1 000. Schema violations raise `pydantic.ValidationError` from `load_graph`; `schema_field(loc)`
> renders the `INVALID_INPUT` field (`nodes[3].data.name`, the union tag dropped). `graph_hash` removes the
> editor-only node keys `position`, `width`, `height` and hashes compact UTF-8 JSON with sorted keys (orjson).
> `va_engine.validation.validate_graph(graph, mode=, refs=GraphRefs(...), allow_http=, code_checker=)` returns
> issues in table row order, then node/edge array order; an oversized graph returns only `PATHWAY_TOO_LARGE`
> (JSON size = canonical dump). `GraphRefs` fields left `None` skip that existence check (KB, tool, pathway ids);
> `code_checker` (the `deno check` runner, phase 08) is optional, but empty custom code is always reported
> (diagnostic `code is empty`). Rule details: duplicate ids/names are reported on every repeat after the first;
> `PATHWAY_EDGE_DANGLING` once per missing endpoint; `PATHWAY_EDGE_CONFLICT` once per source node (`node_id` = the
> source); alwaysPick edges still need a label; reachability (checked only with exactly one start node) seeds the
> start and every global node and follows edges plus node-level jumps (`forwardingNode` id, webhook `routeOnStatus`
> and `responsePathways` targets, `waitForResponse.timeoutNodeId`, `scheduling.onFailureNodeId`, warm
> `onTargetNoAnswer.nodeId`); a webhook URL needs scheme `https` (or `http` with `allow_http`, local/test) and a
> literal host (no `{{` in the authority); a `forwardingNode` object is a pathway target like `transferPathway`;
> templates are checked in `prompt`, `text`, `condition`, `globalLabel`, every string of the declared
> `webhook`/`transfer`/`endCall`/`sms`/`pressButton`/`scheduling`/`waitForResponse`/`customCode` fields except
> `customCode.code`, and global `globalPrompt`/`errorPhrase` (name `global_config`, no `node_id`); the detail is
> `<json path>: <parser error>`; `PATHWAY_VAR_NAME` also covers webhook `responseData` names (unique per list).

## 12. `pathway_logs` format (deterministic)

One entry per assistant turn, ordered by turn index:
```json
{"tag": {"name": "Take Order", "color": "#16A34A"}, "node_id": "node_order", "pathway_id": "…", "pathway_version": 3,
 "role": "assistant", "text": "Great — what would you like?", "user_text": "I'd like to order",
 "decision": {"route": {"from": "node_greet", "to": "node_order", "label": "Wants to order",
   "alternatives": [{"label":"Wants hours","score":2}]}, "global": false,
   "loop_condition": null, "extracted": {"first_name": "Mike"}, "tools": [{"name":"lookup","status":200,"ms":184}]},
 "created_at": "2026-10-24T18:22:31.412Z"}
```
Keys always present (null when not applicable); keys sorted as above; times UTC with milliseconds.

## 13. Configuration precedence for node model options
`node.modelOptions` → `global_config` defaults → call config (`temperature`, `interruptibility`,
`interruption_threshold`, `block_interruptions`) → org settings → platform defaults (temperature 0.7,
interruptibility 2, resumption_speed 2).

## 14. Tests required
- Model round-trip: every Bland example pathway in `packages/va_engine/tests/fixtures/bland/*.json` (restaurant
  ordering, appointment booking, lead qualification — authored in-repo) loads and dumps losslessly.
- Validation: one test per rule in §11, both save and publish modes.
- Templating: grammar, filters, missing var events, type preservation, escaping, URL encoding, limits; hypothesis
  fuzz never raises except `TemplateSyntaxError`.
- Router: stay/route/fallback/alwaysPick/ties/global precedence/return stack; scripted fake LLM from `va_sim`.
- Loop condition threshold and prompt suffix; extraction before_response vs background.
- Every node handler: voice and text-mode behavior table rows in §4 (parametrized).
- Speculation hit/miss with cancellation verified (fake LLM records cancelled streams).
- maxVisits and chain depth protection.
- Barge-in truncation replaces history content.
- EngineState JSON round trip and schema_version rejection.
- pathway_logs snapshot (syrupy) for the restaurant fixture conversation.

## 15. Acceptance criteria
- Simulator e2e: restaurant-ordering pathway call completes with expected node sequence, variables, SMS sent, and
  End Call; pathway_logs equals snapshot.
- Router + extraction add zero critical-path latency when speculation hits (measured: assistant first token time
  equals speculative stream time ± 10 ms in simulator).
- All handlers ≥ 90 % branch coverage.
