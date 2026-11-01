# 10 — LLM Gateway

Package: `packages/va_llm`. Modules: `providers.py` (registry, clients), `dialogue.py` (streaming), `structured.py`
(forced tool calls), `prompts/` (verbatim templates as `.txt` files loaded at import), `window.py` (token budget),
`embeddings.py`, `breaker.py`, `metering.py`.

## 1. Providers

```python
class LlmProvider(BaseModel):
    id: str                       # "platform" or "org:<uuid>"
    base_url: str
    api_key: SecretStr
    models: dict[Literal["dialogue","router","analysis","fallback"], str]
    max_concurrency: int = 500
    supports_tool_choice: bool = True
    supports_parallel_tool_calls: bool = False
```

- Platform provider: `base_url=LLM_DEFAULT_BASE_URL` (production `https://api.telnyx.com/v2/ai/openai`),
  `api_key=LLM_DEFAULT_API_KEY` (the Telnyx API key), models from env `LLM_DIALOGUE_MODEL` (`zai-org/GLM-5.3-Flash`),
  `LLM_ROUTER_MODEL` (`zai-org/GLM-5.3-Flash`), `LLM_ANALYSIS_MODEL` (`zai-org/GLM-5.3`), `LLM_FALLBACK_MODEL`
  (`moonshotai/Kimi-K2.6`).
- Org provider (BYO, enterprise feature): stored in table `org_llm_providers` (shared-contract addition), configured
  via `PUT /v1/org/llm-provider` `{base_url, api_key, models:{dialogue,router,analysis,fallback}}` and
  `DELETE /v1/org/llm-provider`. On save the gateway runs a probe (`chat.completions.create` with
  `max_tokens=1` and a forced tool call) and stores `supports_tool_choice`. `base_url` is SSRF-checked like webhooks.
- Client: `openai.AsyncOpenAI(base_url=..., api_key=..., timeout=httpx.Timeout(connect=2, read=30, write=5, pool=2), max_retries=0, http_client=<shared httpx.AsyncClient(http2=True, limits=Limits(max_connections=500, max_keepalive_connections=100))>)`.
  Clients are cached per provider id; connections are pre-warmed at media-worker startup with one `GET {base_url}/models`.
- Model override per call: `SendCallConfig.dialogue_model` / `router_model` (from `model` param or agent/persona
  settings) selects the model string within the chosen provider.

> Decision (2026-11-01): va_llm core (P03-T13a). Streaming dialogue (`dialogue.py`) follows in P03-T13b,
> structured calls and embeddings in P03-T14; the module list above gains `settings`, `models`, `clients`,
> `org_providers`, `probe`, `policy`, `errors`, `pricing`, `metrics`, `deadline`, `wire`, and `gateway`.
> 1. `va_llm.LlmSettings` holds the specs/02 §6 `LLM_*` rows; `platform_provider(settings)` builds the provider
>    `platform`. `LlmProvider` requires a non-empty `api_key` and a model for each of the four roles.
> 2. `LlmClients` caches one `AsyncOpenAI` per provider id (rebuilt when the base URL or the key's sha256 changes,
>    so a rotated org key gets a new client) on the caller's shared httpx client (`max_retries=0`, the timeout
>    above), bounds concurrent requests per provider with `max_concurrency`, and `prewarm` lists `/models`
>    (failure logged `llm.prewarm_failed`). Every request with a call id sends `X-VA-Call-Id`.
> 3. `OrgProviderStore(database, envelope, clock=, ttl_s=60)` reads `org_llm_providers` in the org's
>    `tenant_session`, decrypts `api_key_enc` with AAD `org_llm_providers.api_key_enc:<row id>`
>    (`encrypt_org_api_key` seals it for writers), and caches hits and misses for `ttl_s` (`invalidate(org_id)`
>    after a change). `ProviderRegistry.resolve(org_id)` returns the org provider, else the platform provider.
> 4. `probe_provider(provider, http)` sends one non-streaming completion on the provider's `router` model with a
>    forced `va_probe` call, `temperature=0`, and `max_tokens=16` (a forced call's arguments cannot fit in one
>    token), 10 s timeout. A tool call → `supports_tool_choice=true`; a 2xx without one, or a 400/422, is retried
>    once without tools (success → `false` with no error); any other failure → `false` with
>    `"<kind>: <message>"` (the key is masked).
> 5. `va_llm.policy.POLICIES` is the §2.1 table: call types `dialogue`, `router`, `loop_condition`,
>    `extraction`, `voicemail`, `guard_rails`, `analysis`; role model `dialogue`/`analysis`/`router` (others),
>    overridable per call with `model=`. The timeout is the total budget of the attempts on a target (waits
>    included), except analysis: 60 s per attempt. Retries: the classifiers and extraction 1 immediate retry,
>    analysis 2 retries after 2 s and 8 s (timeouts retried too), dialogue none. Retryable: 500/502/503/504,
>    connect errors, and 429 with `Retry-After` ≤ 1 s (absent = 0; the wait is `max(policy delay, Retry-After)`
>    and must fit in the remaining budget). A 429 above 1 s, other 4xx, and malformed bodies are never retried;
>    a classifier timeout is final (its budget is spent).
> 6. Dialogue and analysis fall back once, with a fresh timeout, to the platform provider's `fallback` model
>    (also for an org provider) after any failure of the primary target, including a skipped open breaker and a
>    non-retryable 4xx (another model or provider may accept the request); no fallback when it is the primary
>    target. When the org's provider cannot be loaded (`OrgProviderStore` wraps database, KMS/decryption, and
>    invalid-row failures in `OrgProviderError`; they are logged `llm.org_provider_unavailable` and not cached),
>    dialogue and analysis go straight to that fallback (`fell_back=True`) and the other call types raise
>    `LlmUnavailableError(reason="provider_unavailable", last=None)` without a request. Every exhausted call
>    raises `LlmUnavailableError(reason="exhausted"|"breaker_open"|"provider_unavailable", last=...)`, whose
>    `last` is the final request error; callers apply their §2.1 fallback.
> 7. Breakers per `(provider_id, model)` follow §2.2 on the injected clock; failures are timeouts, 5xx, and
>    connect errors, while any other HTTP answer (2xx, 429, 4xx, malformed 2xx) is a success. A cancelled
>    half-open probe frees the probe slot. An open primary breaker skips to the fallback (dialogue/analysis) or
>    raises `LlmUnavailableError("breaker_open")` without a request.
> 8. Errors: `LlmTimeoutError` (kind `timeout`), `LlmConnectionError`, `LlmHttpError(status)` and
>    `LlmRateLimitedError(retry_after_s)` (kind `http`), `LlmProtocolError` (kind `parse`); `to_app_error()` is
>    `UPSTREAM_TIMEOUT`. The gateway parses the raw body itself (the SDK constructs malformed bodies leniently).
>    openai ≥ 3 is typed against `httpx2` but accepts the platform's httpx clients at runtime; `va_llm.clients`
>    is the only place that crosses that boundary.
> 9. `LlmGateway(registry, http, clock=, metrics=, recorder=, costs=, tracer_provider=)`.
>    `complete(call_type, *, org_id, messages, tools, tool_choice, temperature=0.0, max_tokens=512, model,
>    call_id, response_format) -> Completion(content, tool_calls, finish_reason, usage, provider_id, model,
>    attempts, fell_back)` (non-streaming). Deadlines race the request against `clock.sleep` (tests with a manual
>    clock are exact); the SDK timeout stays as a backstop that is never shorter. Metrics and spans: specs/27 §3.6
>    Decision (P03-T13a). Metering: §11 Decision.

## 2. Streaming dialogue (`dialogue.py`)

```python
async def stream_dialogue(req: DialogueRequest) -> DialogueStream
class DialogueRequest(BaseModel):
    provider: LlmProvider; model: str; messages: list[ChatMessage]; tools: list[ToolDef] = []
    temperature: float = 0.7; max_tokens: int = 250; speculative: bool = False
    call_id: UUID | None = None; org_id: UUID; turn_index: int | None = None
class DialogueStream:
    def text(self) -> AsyncIterator[str]                 # content deltas (after commit when speculative)
    async def tool_calls(self) -> list[ToolCall]          # resolved after stream end
    ttft_ms: float | None; usage: TokenUsage | None; finish_reason: str | None
    def commit(self) -> None                              # speculative: release buffered + future tokens
    async def discard(self) -> None                       # speculative: cancel request, drop tokens
```

- Calls `client.chat.completions.create(model, messages, tools?, tool_choice="auto" if tools, temperature,
  max_tokens, stream=True, stream_options={"include_usage": True})`.
- `ttft_ms` = time from request send to first non-empty `delta.content` (or first `delta.tool_calls` when the reply
  starts with a tool call). Deltas carrying `reasoning_content` are ignored (reasoning models) and do not count as
  first token.
- Tool-call deltas are accumulated by `index` into `ToolCall(id, name, arguments_json)`.
- Speculative mode: tokens are buffered until `commit()`; `discard()` cancels the HTTP stream (`await response.close()`).
- Cancellation (barge-in): cancelling the consumer task closes the stream within 50 ms.

> Decision (2026-11-01): streaming dialogue (P03-T13b). Modules `dialogue`, `stream_wire`, `wire_fields`, and
> `targets` join the package.
> 1. `LlmGateway.stream_dialogue(DialogueRequest) -> DialogueStream` returns before any token (a producer task does
>    the work). `DialogueRequest` is frozen with `org_id`, `messages`, `tools`, `provider: LlmProvider | None = None`
>    (None → `registry.resolve(org_id)`; an `OrgProviderError` goes straight to the platform fallback as in §1
>    Decision rule 6), `model: str | None = None` (None → the provider's `dialogue` model), `temperature=0.7`,
>    `max_tokens=250`, `speculative`, `call_id`, `turn_index`. `tool_calls()` returns `list[ToolCall]`.
> 2. The request is the §1 body plus `stream=true`, `stream_options={"include_usage": true}`, and `tool_choice="auto"`
>    when tools are given (`X-VA-Call-Id` with a call id), sent with the SDK's `with_streaming_response`; the
>    gateway parses the raw SSE lines itself. The provider's concurrency slot is held for the whole stream.
> 3. SSE: `data: <json>` chunks, `data: [DONE]` ends; blank lines, `:` comments, and other fields are ignored. A chunk
>    with an `error` member, a non-JSON or non-object chunk, or malformed `choices`/`delta`/`tool_calls`/`usage` is an
>    `LlmProtocolError`; a body that ends without `[DONE]` is an `LlmConnectionError`, a body read error too
>    (`LlmTimeoutError` for an httpx read timeout). A token is a non-empty `delta.content` or any `delta.tool_calls`
>    item; reasoning and role-only deltas are not. Tool calls accumulate by `index` (first non-empty `id`/`name`
>    wins, `arguments` concatenate); a finished stream with a nameless call is a protocol error.
> 4. Deadlines on the injected clock: TTFT 2.5 s per target from that target's attempt start (the provider slot wait
>    included), total 20 s from `stream_dialogue` covering both targets (a fallback's TTFT is `min(2.5, remaining)`).
>    Each line read races the current deadline.
> 5. Before the first token any failure of the primary (any HTTP status incl. 429, connect, TTFT deadline, protocol
>    error, missing `[DONE]`, open breaker) gets one attempt on the platform `fallback` model (none when it is the
>    primary target); no retries. When both fail, `text()` and `tool_calls()` raise
>    `LlmUnavailableError(reason="exhausted"|"breaker_open", last)` (the reason of the last target) and `error`
>    holds it.
> 6. After the first token a failure (read error, protocol error, total deadline) ends the stream without fallback:
>    `text()` ends normally after the delivered deltas, `error` holds the `LlmError`, `finish_reason` stays None, and
>    `tool_calls()` returns the named calls assembled so far.
> 7. `ttft_ms` (injected clock, from the producing attempt's start) is set at the first token and observed in
>    specs/27 §3.3 `va_llm_ttft_ms{model, purpose="dialogue"}` when the process passes its catalog.
> 8. Speculative streams buffer deltas (and the end) until `commit()`; `discard()` cancels the producer, drops the
>    buffer, and `text()` then ends empty. `va_llm_speculation_total{outcome}` counts the first commit/discard of a
>    speculative stream; repeats are no-ops; `commit()` on a plain stream does nothing, `discard()` closes it
>    without the metric. `text()` may be iterated once (`RuntimeError` otherwise).
> 9. `DialogueStream` is an async context manager; `aclose()`, `discard()`, and cancelling (or abandoning) the
>    consumer of `text()` cancel the producer and wait until the HTTP response is closed, with no clock wait.
>    `aclose()` re-raises only an unexpected producer crash.
> 10. Breakers (§2.2) per attempt: success at the first token or a token-less normal end; a pre-token failure records
>    `success = not breaker_failure`; a cancelled attempt without a verdict frees the probe slot. Post-token failures
>    leave the breaker alone.
> 11. Metering (§11): exactly once when the producer ends, for the attempt that produced a token or completed
>    without one; usage from the final chunk, else estimated over the prompt, the received text, and the tool calls
>    (`estimated=True`; discarded and cancelled streams included). Nothing is metered when no attempt produced.
> 12. Metrics and spans: specs/27 §3.6 Decision (P03-T13b).

### 2.1 Timeouts and fallback

| Call type | Timeout | On timeout / error before first token | After first token |
|---|---|---|---|
| Dialogue | TTFT 2 500 ms; total 20 s | retry once on `fallback` model (same provider; if org provider fails, platform provider) | stream ends; partial text kept; error event |
| Router (`select_path`) | 1 500 ms | heuristic fallback (§9.1 step 5) | — |
| Loop condition | 1 500 ms | treat as not met (stay) | — |
| Extraction | 3 000 ms | variables unchanged; warning event | — |
| Voicemail classifier | 1 500 ms | `unknown` | — |
| Guard-rail classifier | 2 000 ms | not triggered; warning event | — |
| Analysis (post-call) | 60 s | retry ×2 with backoff 2 s, 8 s; then fallback model | — |

HTTP retry policy inside a call type (before any token): `429` (honor `Retry-After` ≤ 1 s, else fail over), `500`,
`502`, `503`, `504`, connect errors → 1 immediate retry for router/extraction/classifiers; dialogue goes straight to
fallback to protect latency.

### 2.2 Circuit breaker (`breaker.py`)

Per `(provider_id, model)`: closed → open after 5 failures (timeouts/5xx/connect) within 30 s; open for 20 s (requests
skip directly to fallback); half-open allows 1 probe; success closes. State is per process; transitions logged and
exported as `va_llm_breaker_state{provider,model}`.

## 3. Structured outputs (`structured.py`)

Every forced function used anywhere in the platform is registered once in `va_llm.structured.TOOL_NAMES`
(a `frozenset[str]`) together with its schema via `register_structured_function(name, schema)` at import time of the
owning module. Registered names: `select_path`, `score_condition`, `extract_variables`, `classify_answer`,
`evaluate_guard_rails`, `grade`, `summarize`, `classify_disposition`, `extract_analysis_schema`, `answer_questions`,
`record_sentiment`, `record_analysis`, `extract_citations`, `update_memory`. A test asserts that every
`call_structured(..., function_name=...)` call site uses a registered name; the FakeLLM rules engine imports the set so it
can never drift.

```python
async def call_structured(provider, model, messages, schema: type[BaseModel], *, function_name: str,
                          description: str, timeout_s: float, temperature: float = 0.0) -> BaseModel
```

1. Build tool `{"type":"function","function":{"name": function_name, "description": description, "parameters": schema.model_json_schema()}}`
   (with `$defs` inlined and `additionalProperties: false`).
2. Request with `tools=[tool]`, `tool_choice={"type":"function","function":{"name":function_name}}`,
   `temperature`, `max_tokens=512`, non-streaming.
3. Parse `message.tool_calls[0].function.arguments`; validate with `jsonschema` then Pydantic.
4. If the provider has `supports_tool_choice=false` or the reply has no tool call: fall back to JSON mode — append
   system instruction `Respond with only a JSON object that matches this JSON Schema: <schema>` and parse the first
   `{…}` block in `message.content`.
5. On validation failure: one repair retry adding a user message
   `Your previous output was invalid: <validation errors>. Call {function_name} again with corrected arguments.`
6. Second failure → raise `StructuredOutputError` (callers apply their fallback).

> Decision (2026-11-01): structured outputs (P03-T14a). Modules `structured` (pure) and `structured_calls`.
> 1. API: `LlmGateway.call_structured(call_type, schema, *, org_id, messages, function_name, description,
>    call_id=None, temperature=0.0, max_tokens=512) -> schema instance`. The provider, model, and timeout of the
>    signature above come from the call type: provider via the registry, model via the §2.1 policy role, and each
>    request (the first and the repair) is one `complete` call with its own policy budget, retries, breaker skip,
>    fallback, metering, and span. `call_type="dialogue"` raises `ValueError` (dialogue streams).
> 2. `TOOL_NAMES` is the constant set of the 14 names above. `register_structured_function(name, schema)` raises
>    `ValueError` for a name outside it or a second registration with another class (the same class is a no-op);
>    `registered_schema(name)` reads it. `call_structured` raises `ValueError` before any request for a name outside
>    `TOOL_NAMES` or registered with another class. FakeLLM never imports `va_llm` (specs/30 §3.4): it keeps its
>    own copy, and `packages/va_llm/tests/test_structured_drift.py` asserts both sets are equal and that every
>    `call_structured(function_name=...)`/`register_structured_function(...)` call site in `apps/*/src` and
>    `packages/*/src` uses a string literal from `TOOL_NAMES`.
> 3. Tool schema (`tool_schema(schema)`): `model_json_schema()` with every `#/$defs/X` reference replaced by a copy
>    of the definition (sibling keys such as `description` merged over it), `$defs` removed, and
>    `additionalProperties: false` on every object schema with `properties` that does not set it (only schema
>    positions are rewritten; `enum`, `default`, ... values are data). A self-referencing model → `ValueError`.
> 4. Forced mode (provider `supports_tool_choice`): the output is the arguments of the first tool call named
>    `function_name` (else the first tool call); a reply without tool calls falls back to the first JSON object of
>    `content` (rule 6), and when there is none the output is invalid.
> 5. JSON mode (provider `supports_tool_choice=false`; when the org's provider cannot be loaded, the platform
>    provider's flag decides and `complete` applies the §1 rules): no tools, the system instruction (compact JSON of
>    the tool schema) is appended after the given messages, and the output is the first JSON object of `content`.
> 6. First JSON object: from each `{` in order, the first prefix that decodes to a JSON object.
> 7. Validation: the output text must be a JSON object, then Draft 2020-12 `jsonschema` against the tool schema,
>    then `schema.model_validate`. Errors render as `<path>: <message>` (`items[0].name`, `$` for the root),
>    jsonschema errors sorted, at most 5, each message clipped to 200 characters.
> 8. Repair: original messages (+ the JSON-mode instruction) + an `assistant` message with the raw invalid output
>    + the user message above, in the same mode as the first request, except that a forced request answered
>    without a tool call is repaired in JSON mode. A second invalid output raises `StructuredOutputError`
>    (`kind="parse"`, `function_name`, `errors`, `attempts=2`, target of the last completion).
>    `LlmUnavailableError` from either request propagates unchanged.
> 9. Observability: `va_llm_structured_total{function, outcome}` (specs/27 §3.6) and a `llm.structured_invalid`
>    warning per invalid output (function, call type, attempt, error count; never the output itself).

## 4. Prompt assembly for voice dialogue

Message list order:
1. `system`: rendered **Voice System Prompt** (§5).
2. `system` (optional): tool usage notes when tools exist: `You can use tools. Before using a tool that takes time, the caller will hear "{speech}". After a tool returns, tell the caller the result in plain spoken language.`
3. Conversation window (§6): alternating `user`/`assistant` messages; tool calls and results as `assistant`
   (`tool_calls`) + `tool` messages; DTMF turns as `user` with text `"[Caller pressed: 1 2 3]"`; injected context as
   `system` messages at their position: `[Context update] {text}`.
4. For pathway Default nodes with `dialogueExamples`: inserted before the window as alternating example
   `user`/`assistant` pairs preceded by `system`: `Examples of how to respond at this step:`.

Templating of `{{var}}` in prompts happens before assembly (`11` §6).

## 5. Voice System Prompt (verbatim, `prompts/voice_system.txt`)

```
You are {agent_name}, speaking with a person on a live phone call{company_clause}.
Current date and time: {now_local} ({timezone}), {weekday}.
{direction_line}

How to speak:
- Everything you write is converted to speech. Write only the words that should be said aloud.
- Never use markdown, lists, headings, emojis, or symbols such as *, #, or /.
- Keep each reply short: usually one or two sentences and under 40 words, unless the caller asks for more detail.
- Ask at most one question at a time, then stop and let the caller answer.
- Say numbers the way people say them aloud: phone numbers digit by digit in groups, prices like "twenty-four dollars and fifty cents", times like "three thirty in the afternoon", dates like "Tuesday, March fourth".
- Do not read web addresses or email addresses character by character unless the caller asks.
- If you did not understand the caller, politely ask them to repeat.
- If the caller interrupts you, your earlier reply ends where it is marked with "—". Do not repeat what you already said unless asked.
- If asked whether you are an AI or a real person, answer truthfully.
- Respond in {language_name}.

Your instructions:
{instructions}
{known_information_section}{memory_section}{knowledge_section}{glossary_section}
```

Placeholder rendering rules (all produced by `va_llm.prompts.render_voice_system`):
- `agent_name`: persona/agent `agent_name` setting, else `"the assistant"` with the first line reading
  `You are the assistant, speaking…`.
- `company_clause`: `" on behalf of {company}"` when `company` set, else empty.
- `direction_line`: outbound → `You placed this call to {to_display}.`; inbound → `The caller called {company_or_number}.`;
  web → `The person is talking to you through a website.`
- `instructions`: task prompt, or `global_prompt` + blank line + node prompt (pathways), trimmed.
- `known_information_section`: when variables exist: `\nKnown information:\n` + lines `- {name}: {value}` (values
  JSON-encoded if not strings; excludes keys starting with `_` and built-ins `call_id`, `voice_id`).
- `memory_section`: `\nWhat you remember about this caller:\n{memory_context}` (`20` §2).
- `knowledge_section`: `\nReference information (use it only if relevant; do not mention that you were given it):\n`
  + chunks `[{i}] {content}` (top-k from `14` §6).
- `glossary_section`: `\nImportant names and terms: {comma-separated terms}` (`08` §6).

## 6. Conversation window (`window.py`)

- Token counting: `tiktoken.get_encoding("cl100k_base")` as an approximation for all models (+ 4 tokens per message).
- Budget: `context_budget_tokens` = org setting `llm.context_budget_tokens` (default 6 000, max 32 000) for the
  window; system prompt and examples are not trimmed but are measured — if system + examples exceed 60 % of the
  model's configured `max_context` (default 128 000), KB chunks are reduced first (drop lowest-ranked), then memory
  context truncated to 800 tokens.

> Decision (2026-10-29): the org setting's accepted range is 1 000–32 000 (specs/04 §7 now uses this spec's maximum).

- Trimming: drop the oldest complete turns (user+assistant pairs and their tool messages together) until within
  budget; always keep the opener assistant message; insert `system` `"(Earlier parts of the conversation are omitted.)"`
  at the cut.
- Interrupted assistant messages contain only `text_spoken` + `"—"`.

## 7. In-call tool definitions

Generated per turn from: custom tools available at the node/call (`13` §2), knowledge bases (no tool; retrieval is
automatic), and built-ins for the task runner:

| Name | Parameters (JSON Schema) | Available when |
|---|---|---|
| `transfer_call` | `{"type":"object","properties":{"label":{"type":"string","enum":[<transfer_list keys>]},"message":{"type":"string","description":"Short line to say before transferring"}},"required":["label"]}` (no `label` param and enum when only `transfer_phone_number`) | task runner with transfer target(s) |
| `end_call` | `{"type":"object","properties":{"reason":{"type":"string"},"final_message":{"type":"string"}},"required":["reason"]}` | always for task runner |
| `press_button` | `{"type":"object","properties":{"digits":{"type":"string","pattern":"^[0-9*#]{1,32}$"}},"required":["digits"]}` | `ivr_mode` or org setting |
| `send_sms` | `{"type":"object","properties":{"message":{"type":"string","maxLength":1600}},"required":["message"]}` | `from` number has SMS capability and org setting `tools.allow_send_sms` |
| `schedule_callback` | `{"type":"object","properties":{"datetime_iso":{"type":"string"},"note":{"type":"string"}},"required":["datetime_iso"]}` | org setting `tools.allow_schedule_callback` (creates a scheduled call with same config) |

Custom tool function definition: `name` = tool `name`, `description` = tool `description`, `parameters` = tool
`input_schema` (validated to be an object schema at tool creation).

Tool loop: max 3 tool rounds per user turn; each round's results appended as `tool` messages; the final round
streams speech. Unknown tool name or invalid arguments → `tool` message `{"error":"invalid_arguments","detail":…}` and
the model gets one more round.

## 8. Speculative generation contract

The engine may start `stream_dialogue(speculative=True)` for the current node in parallel with routing (`11` §7).
- If routing keeps the node (`stay` or self) and no variable extraction changed variables referenced by the node prompt,
  the engine calls `commit()` → buffered tokens flow to TTS immediately.
- Otherwise `discard()` and a new request is made with the new node's prompt.
- Metrics: `va_llm_speculation_total{outcome="committed|discarded"}`. Discarded tokens are still metered.

## 9. Classifier prompts (verbatim, `prompts/*.txt`)

### 9.1 Router — `prompts/router.txt`, function `select_path`

System:
```
You are the routing component of a phone conversation flow. Decide which path the conversation takes next, using only the conversation so far, the known information, and the caller's latest message.

Current step: "{node_name}"
What the current step is for: {node_purpose}

Paths from the current step:
{paths}
- id: stay | condition: None of the paths above apply yet. Remain on the current step.

Paths available from any step:
{global_paths}

{examples_block}Rules:
- Choose a path only when its condition is clearly met by what the caller said or by the known information.
- If more than one path applies, choose the most specific one.
- Choose "stay" when the caller has not yet given what the current step needs.
- Give every path you considered a confidence from 0 to 10.
Call select_path exactly once.
```
User: the last 8 messages of the window rendered as `Caller: …` / `Agent: …` lines, then
`Known information: {json of variables}` and `Latest caller message: "{text}"`.

`{paths}` lines: `- id: {edge_id} | condition: {edge label}{" | description: " + edge.description if set}`.
`{global_paths}` lines: `- id: global:{node_id} | condition: {global node label}`; `None` when empty.
`{examples_block}`: when `pathwayExamples` exist: `Examples:\n` + lines `Caller said: "{x}" → path {edge_id}` + blank line.
`{node_purpose}`: node `name` + first 300 chars of the node prompt.

Schema:
```json
{"type":"object","additionalProperties":false,"required":["path_id","confidence","alternatives","reason"],
 "properties":{"path_id":{"type":"string","enum":["<edge ids>","stay","<global ids>"]},
  "confidence":{"type":"integer","minimum":0,"maximum":10},
  "alternatives":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["path_id","confidence"],
     "properties":{"path_id":{"type":"string"},"confidence":{"type":"integer","minimum":0,"maximum":10}}}},
  "reason":{"type":"string","maxLength":200}}}
```
Step 5 heuristic fallback (on timeout/StructuredOutputError): if exactly one outgoing edge label shares ≥ 2
content words with the user text (stop-words removed) choose it, else `stay`.

### 9.2 Loop condition — `prompts/loop_condition.txt`, function `score_condition`

```
You check whether a step of a phone conversation is complete.

Completion condition: {condition}

Read the conversation and decide how likely it is that the condition has been fully satisfied, from 0 (certainly not) to 10 (certainly yes). If anything the condition requires is still missing, say what is missing.
Call score_condition exactly once.
```
User: last 10 messages + latest caller message. Schema `{"score": integer 0–10, "met": boolean, "missing": string ≤ 200}`.
Met when `score ≥ 7` (logged as `Likely {score}/10`), regardless of the `met` field (stored for diagnostics).

### 9.3 Variable extraction — `prompts/extract.txt`, function `extract_variables`

```
Extract the following information from the phone conversation. Use only what the caller or the agent explicitly stated. If a value was not stated, return null for it. Do not guess.

Variables:
{variable_lines}

Current date and time for resolving relative dates: {now_local} ({timezone}).
Call extract_variables exactly once.
```
`variable_lines`: `- {name} ({type}): {description}`. Schema: object with each variable as property, type
`string|integer|boolean` + `null`, all required (nullable). Integer parsing accepts spoken numbers; booleans from
yes/no semantics.

### 9.4 Voicemail / IVR classifier — `prompts/answer_classifier.txt`, function `classify_answer`

```
You hear the first words spoken after an outbound phone call was answered. Decide who or what answered.

- human: a live person speaking naturally (for example "Hello?", "This is Dana", "Who is this?").
- voicemail: a recorded greeting or mailbox message (for example "You've reached…", "leave a message after the tone", "the person you are calling is not available", "the mailbox is full").
- ivr: an automated menu or system (for example "Press 1 for…", "Please say or enter…", "Your call is important to us").
- unknown: not enough information yet.

Transcript so far: "{transcript}"
Seconds since answer: {seconds}
Call classify_answer exactly once.
```
Schema `{"label": "human"|"voicemail"|"ivr"|"unknown", "confidence": number 0–1}`. Acted on when
`confidence ≥ 0.8` and label ≠ `unknown`.

### 9.5 Guard-rail classifier — `prompts/guard_rails.txt`, function `evaluate_guard_rails`

```
You monitor a live phone conversation for specific situations. For each rule below, decide whether the caller's latest message, in the context of the conversation, triggers it.

Rules:
{rule_lines}

Be conservative: mark a rule as triggered only when the conversation clearly matches it.
Call evaluate_guard_rails exactly once.
```
`rule_lines`: `- {name}: {description}`; `tcpa:opt_out` is rendered as
`- tcpa_opt_out: The caller asks to stop being called, to be removed from the list, to not be contacted again, or says "do not call".`
Schema `{"results":[{"name": string, "triggered": boolean, "confidence": number 0–1, "evidence": string ≤ 200}]}`;
a rule fires when `triggered && confidence ≥ 0.75`.

## 10. Embeddings (`embeddings.py`)

- `client.embeddings.create(model=EMBEDDINGS_MODEL, input=[...], dimensions=EMBEDDINGS_DIMENSIONS)` on the platform
  provider (Telnyx Inference `/embeddings`), batches of 64 inputs, each input truncated to 8 000 tokens (cl100k).
- Concurrency: 4 batches in flight per worker process; retries on 429/5xx (3 attempts, 1 s/4 s/10 s).
- Returned vectors validated `len == 1024`, L2-normalized before storage (pgvector cosine).
- Query embeddings are cached in Redis `emb:q:{sha256(model|dims|text)}` for 24 h.
- Metering: `kb_embedding_tokens` usage (quantity = `usage.prompt_tokens`).

## 11. Metering

Every completion records `llm_tokens_in` and `llm_tokens_out` usage events (price 0 for calls — included in
per-minute; priced for `/analyze` and Norm-style builders per plan), cost from env
`LLM_COST_MICROS_PER_1K_IN`/`LLM_COST_MICROS_PER_1K_OUT` per model map in `va_llm.pricing.MODEL_COSTS` (configurable
JSON env `LLM_MODEL_COSTS_JSON`). Usage comes from the final stream chunk (`include_usage`); if absent, estimated
with tiktoken.

> Decision (2026-11-01): metering in P03-T13a. A missing `usage` is estimated offline as `ceil(utf8_bytes / 4)` per
> text (message contents, tool-call names and arguments) plus 4 per prompt message and flagged
> `TokenUsage.estimated` (tiktoken arrives with `window.py`, DEC-006). `va_llm.pricing.MODEL_COSTS` holds
> provisional built-in prices in micro-dollars per 1 000 tokens (`zai-org/GLM-5.3-Flash` 50/200, `zai-org/GLM-5.3`
> 600/2200, `moonshotai/Kimi-K2.6` 600/2500, `Qwen/Qwen3-Embedding-8B` 20/0); `load_costs(LLM_MODEL_COSTS_JSON)`
> merges overrides over them and raises `ValueError` at startup for invalid JSON or entries; an unknown model costs
> 0 and is logged `llm.unpriced_model` once. Costs round up per direction. Every successful completion (fallbacks
> included) goes to the injected `UsageRecorder` as a `UsageRecord(org_id, call_id, call_type, provider_id, model,
> prompt_tokens, completion_tokens, estimated, cost_in_micros, cost_out_micros)`; a recorder failure is logged
> `llm.usage_record_failed` and never fails the completion (the billing phase writes `usage_events`).

## 12. Simulator (`va_sim.llm`)

OpenAI-compatible server (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`) driven by scenario scripts:
- Dialogue: replies from a scenario queue keyed by `call_id` header `X-VA-Call-Id` (the gateway sends it on every
  request) or by matching regex rules on the last user message; streams tokens at `SIM_LLM_TOKENS_PER_S` (default 80)
  after `SIM_LLM_TTFT_MS` (default 150).
- Forced tool calls: returns arguments from scenario rules or schema-valid defaults (`stay`, score 8, nulls).
- Embeddings: deterministic hash-based unit vectors (1024-d).
- Fault injection: timeouts, 429, malformed tool args (to exercise repair retry).

## 13. Tests required

- Provider registry and per-org override; probe on save; SSRF refusal.
- Streaming: TTFT measurement excludes reasoning deltas; tool-call delta assembly; cancellation closes stream.
- Fallback matrix (§2.1) per call type; breaker transitions with fake clock.
- Structured: forced tool path, JSON-mode fallback, repair retry, final failure.
- Prompt rendering snapshots for every placeholder combination; router/loop/extract/classifier prompt snapshots.
- Window trimming keeps opener and tool pairs; interrupted message formatting.
- Speculative commit/discard semantics.
- Embeddings batching, truncation, normalization, caching.
- Live smoke: `test_telnyx_inference_stream` (TTFT recorded, text non-empty), `test_telnyx_inference_tool_choice`
  (forced tool call honored by `zai-org/GLM-5.3-Flash`), `test_telnyx_embeddings_1024`.

## 14. Acceptance criteria

1. Dialogue `llm_ttft_ms` p50 ≤ 300 ms against Telnyx Inference from us-east (live suite).
2. Router decisions never block speech beyond the speculative design (route_ms off the critical path when the node stays).
3. All classifier calls return schema-valid objects or apply their documented fallback (no exceptions escape).
