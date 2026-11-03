# Phase 07 scratchpad

## Plan P07-T01 — Node and edge schemas with validation (specs/11 §3, §11; specs/12 §1 graph hash)

Note: templating (P07-T02) and EngineState (P07-T03) already exist from P05-T09a (`va_engine.templating`,
`va_engine.models.state`). Their planner passes will mostly verify acceptance gaps; T01 only builds schemas/validation.

### Files to create
1. `packages/va_engine/src/va_engine/models/nodes.py`
   - `NodeType` = `Literal["Default","Wait for Response","Webhook","Knowledge Base","Transfer Call","End Call","SMS",
     "Custom Code","Press Button","Transfer Pathway","Scheduling"]` (`Global` is input-only) + `NODE_TYPES` tuple and
     `NODE_TYPE_ALIASES = {"Transfer Node": "Transfer Call", "End Node": "End Call", "Knowledge Base Node":
     "Knowledge Base", "Webhook Node": "Webhook"}`.
   - Shared base config `_Camel = ConfigDict(alias_generator=pydantic.alias_generators.to_camel, validate_by_name=True,
     validate_by_alias=True, serialize_by_alias=True, extra="allow")`: snake_case attributes, camelCase JSON. Fields whose
     JSON name is snake_case (`modelOptions.block_interruptions`, `resumption_speed`, `forwardingNode.pathway_id/
     node_id`) get explicit `Field(alias=...)` / the generator is identity for them.
     extra="allow" everywhere (lossless Bland round trip, unknown keys kept in `model_extra`).
   - Config models with every field + default from §3.1/§3.2.x: `ExtractVar(name, type Literal["string","integer",
     "number","boolean"] default "string", description="", required=False)`, `PathwayExample`, `ConditionExample`,
     `DialogueExample`, `ConversationMessage(role, content)`, `ModelOptions`, `NodeTag(name, color)`, `ForwardingTarget
     (pathway_id, version: Literal["production","staging"]|int, node_id|None)`, `WebhookResponseData(name, data,
     context="")`, `ResponsePathway(variable, operator: WebhookOperator literal of the 10 ops, value: JsonValue=None,
     target_node_id)`, `RouteOnStatus` (keys `2xx`,`4xx`,`5xx`,`timeout` via explicit aliases → attrs s2xx etc.,
     all `str|None`), `WebhookConfig` (method literal GET..DELETE default POST, headers dict[str,str], body JsonValue,
     timeout_ms 100..60000 default 10000, retries 0..3 default 0, speech, response_data, route_on_status,
     response_pathways), `WarmTransfer`/`OnTargetNoAnswer`, `TransferConfig` (mode cold|warm default cold, number,
     speech, speech_mode static|prompt, speech_prompt, caller_id_mode, warm), `EndCallConfig`, `SmsConfig`
     (`from` → attr `from_` alias "from"), `PressButtonConfig` (digits regex `^[0-9*#ABCDwW]+$`, duration_millis 250),
     `TransferPathwayConfig` (pathway_id str, version production|staging|int default production, node_id, carry_variables
     True, return_on_end False), `SchedulingConfig` (calendar_provider Literal["webhook"], urls, headers, timezone,
     slot_duration_minutes 30, lookahead_days 14, speech, confirmation_prompt, on_failure_node_id),
     `CustomCodeConfig` (language Literal["typescript","javascript"] default typescript, code, timeout_ms 1000,
     network_allowlist [], speech), `WaitForResponseConfig` (wait_seconds 30, reprompt_text, max_reprompts 2,
     on_timeout end_call|move_to_node|repeat, timeout_node_id).
   - `PathwayNodeData`: every §3.1 field with defaults (name 1–120 chars; condition_threshold 0..10 default 6;
     extract_vars_mode before_response|background; max_visits 1..100 default 8; kb `str | list[str] | None`;
     kb_top_k 5; tools `list[str | dict[str, JsonValue]]`; forwarding_node `str | ForwardingTarget | None`;
     transfer_number legacy alias kept; on load, `model_validator(mode="after")` fills `transfer =
     TransferConfig(number=transfer_number)` when transfer is None and transfer_number non-empty — NOT serialized
     unless set? → avoid: do the alias normalization in `effective_transfer_number(data)` helper instead, so the stored
     JSON stays unchanged (lossless). Record as Decision.)
   - `Position(x: float, y: float)`, `PathwayNode(id: constr ^[A-Za-z0-9_-]{1,64}$, type: NodeType, position:
     Position|None=None, width/height: float|None, data: PathwayNodeData)`; a `model_validator(mode="before")`
     normalizes: alias types → canonical; `"Global"` → `"Default"` and `data.isGlobal = True`.
     Spec asks for a "discriminated union": implement `AnyPathwayNode = Annotated[DefaultNode | WaitForResponseNode |
     ... , Field(discriminator="type")]` where each subclass of `PathwayNode` pins `type: Literal[...]`
     (12 subclasses incl. KnowledgeBaseNode/CustomCodeNode since import must accept them). Parse via
     `NODE_ADAPTER = TypeAdapter(AnyPathwayNode)` after normalization (normalization function `normalize_node_raw(raw)`
     applied in `parse_node` before the adapter, since a before-validator on a union member can't change the tag).
   - `EdgeData(label: str = "", description="", is_fallback=False, always_pick=False, priority: int = 0)`,
     `PathwayEdge(id ^[A-Za-z0-9_-]{1,64}$ — Decision: same id rule as nodes, source, target, type: str|None=None,
     data: EdgeData = EdgeData())`.
   - Functions: `parse_node(raw) -> AnyPathwayNode`, `parse_edge(raw) -> PathwayEdge`, `dump_node(node) -> dict`,
     `dump_edge(edge) -> dict` = `model_dump(mode="json", by_alias=True, exclude_unset=True)` (exclude_unset keeps
     exports lossless; normalized fields set in the before-validator count as set).
2. `packages/va_engine/src/va_engine/models/graph.py`
   - `GlobalConfig` (§3.4, every field + defaults, extra allow; `max_total_turns` 1..1000 default 200,
     `no_match_behavior` stay|fallback_edge, `global_kb: str | list[str] | None`, `global_tools: list[str|dict]`).
   - `PathwayGraph(nodes: list[AnyPathwayNode], edges: list[PathwayEdge], global_config: GlobalConfig)`: helpers
     `node(id) -> AnyPathwayNode | None`, `start_nodes()`, `global_nodes()`, `out_edges(node_id)` (list order).
     Do NOT reject duplicates here (validation reports them); index by first occurrence.
   - `load_graph(nodes: Sequence[Any], edges: Sequence[Any], global_config: Mapping[str, Any] | None) -> PathwayGraph`
     (raises pydantic `ValidationError`; errors carry `loc` like `("nodes", 3, "data", "name")` → API maps with
     `va_domain.org.field_path` to INVALID_INPUT in T09) and `dump_graph(graph) -> GraphJson` TypedDict
     `{nodes, edges, global_config}`.
   - `canonical_json(value: JsonValue) -> bytes` = orjson `OPT_SORT_KEYS` (compact, UTF-8) and
     `graph_hash(nodes, edges, global_config) -> str` (hex sha256 of canonical_json of `{"nodes": ..., "edges": ...,
     "global_config": ...}` with node keys `position`, `width`, `height` removed — all three are editor layout;
     Decision). Operates on JSON values (what is stored), plus `PathwayGraph.hash()` = graph_hash(*dump_graph).
3. `packages/va_engine/src/va_engine/validation.py`
   - `Rule` StrEnum/Literal of the 17 codes + `CUSTOM_CODE_ERROR`; `RULES: Final[dict[code, (save_severity,
     publish_severity)]]` straight from §11 table; `Mode = Literal["save","publish"]`.
   - `@dataclass(frozen=True) GraphIssue(code, message, severity: Literal["error","warning"], node_id: str|None=None,
     edge_id: str|None=None)` with `as_item()` → `{code, message, node_id?, edge_id?}` (omit None).
   - `@dataclass(frozen=True) GraphRefs(pathway_id: str|None=None, kb_ids: frozenset[str]|None=None, tool_ids:
     frozenset[str]|None=None, pathway_ids: frozenset[str]|None=None)`; a `None` set = that existence check is skipped
     (T09 passes org data). `code_checker: Callable[[CustomCodeConfig], str | None] | None = None` parameter (phase 08
     supplies `deno check`; None skips CUSTOM_CODE_ERROR) — Decision.
   - `validate_graph(graph: PathwayGraph, *, mode: Mode, refs: GraphRefs = GraphRefs(), allow_http: bool = False,
     code_checker=None) -> list[GraphIssue]` and `validation_report(issues) -> ValidationReport` TypedDict
     `{"valid": not errors, "errors": [...], "warnings": [...]}`.
   - Exact messages from §11 (format strings in one `MESSAGES` dict). Rule specifics:
     * TOO_LARGE: >500 nodes or >2000 edges or `len(canonical_json(dump_graph))` > 5_000_000 → return ONLY that issue.
     * START_NODE: count `is_start` ≠ 1 → `{n}` = count.
     * DUPLICATE_NODE_ID: once per duplicated id (second+ occurrence, node_id = id). DUPLICATE_NODE_NAME:
       `casefold()`-compare, reported once per duplicate occurrence with that node's name.
     * EDGE_DANGLING: per edge, source then target each missing → one issue each (edge_id, node = missing id).
     * EDGE_LABEL: non-fallback edge with blank (strip) label.
     * EDGE_CONFLICT: per source node (array order) with >1 fallback, >1 alwaysPick, or an alwaysPick edge plus any
       other outgoing edge → one issue, node_id = source.
     * UNREACHABLE: only when exactly one start node; BFS seeds = start + all global nodes; successors = edges +
       node-level jumps (`forwardingNode` str, webhook `routeOnStatus` values + `responsePathways[].targetNodeId`,
       `waitForResponse.timeoutNodeId`, `scheduling.onFailureNodeId`, `transfer.warm.onTargetNoAnswer.nodeId`)
       (Decision); each unreached non-global node → issue.
     * GLOBAL_LABEL: `is_global` with blank `global_label`.
     * NODE_EMPTY: type Default/Knowledge Base/Wait for Response with blank prompt and text.
     * WEBHOOK_URL: Webhook node with missing config/url, or `urlsplit(url)` scheme not `https` (or `http` when
       allow_http), empty hostname, or a `{{` in scheme/netloc (host must be literal) — Decision.
     * TRANSFER_TARGET: Transfer Call node whose `transfer.number` and `transferNumber` are both blank.
     * KB_MISSING (per id: node `kb`, global `globalKb`), TOOL_MISSING (string ids in node `tools`, global
       `globalTools`; inline tool dicts skipped), TARGET_MISSING (`transferPathway` missing/blank pathwayId, id not in
       refs.pathway_ids, or == refs.pathway_id with nodeId None) — message `{id}`.
     * CUSTOM_CODE_ERROR: Custom Code node, `code_checker(cfg)` returns diagnostic.
     * TEMPLATE_SYNTAX: `parse_template` every templated string: node `prompt`, `text`, `condition`, `globalLabel`,
       every string leaf of `webhook`, `transfer`, `endCall`, `sms`, `pressButton.speech`, `scheduling`,
       `waitForResponse.repromptText`, `customCode.speech` (exclude `customCode.code`), edge label/description?
       (no — router text, not templated), global `globalPrompt`, `errorPhrase`. detail = `"{json path}: {error}"`
       (e.g. `webhook.headers.Authorization: ...`); one issue per bad field; global-config issues use name
       `global_config`, node_id None.
     * VAR_NAME: extractVars name not matching `^[a-z_][a-z0-9_]{0,63}$` or repeated within the node; also
       webhook `responseData[].name` (Decision — they write variables too).
   - Ordering (Decision, deterministic): issues in §11 table row order; within a rule, node/edge array order.
     severity = RULES[code][mode].
4. `packages/va_engine/src/va_engine/models/__init__.py`: re-export new public names.
5. Spec: add `> Decision (2026-11-03): node/edge schema and validation (P07-T01)` under specs/11 §11 (and a short one
   under §3.1) covering: snake attrs/camel JSON, extra keys kept, exclude_unset dump, type normalization, transferNumber
   kept + `effective_transfer_number`, edge id rule, refs None = skipped, code_checker, reachability jumps, webhook URL
   literal host, response var names, issue ordering, TOO_LARGE short-circuit, graph_hash excludes position/width/height.
6. AGENTS.md: one dated bullet (module names, `exclude_unset` dump rule, refs/ code_checker seams, fixtures dir).

### Fixtures (`packages/va_engine/tests/fixtures/bland/`)
- `restaurant_ordering.json`, `appointment_booking.json`, `lead_qualification.json`: `{nodes, edges, global_config}`
  authored in-repo, Bland camelCase, covering every node type incl. Knowledge Base/Custom Code, global node,
  webhook with responseData/responsePathways/routeOnStatus, SMS, End Call, Transfer Call, pressButton, scheduling,
  waitForResponse, transferPathway, unknown Bland editor keys (`selected`, `dragging`, `data.active`) to prove
  preservation. All must validate publish-clean with refs that contain their KB/tool/pathway ids and a stub
  code_checker returning None.
- `legacy_import.json`: types `"Global"`, `"Transfer Node"`, `"End Node"`, `"Webhook Node"`, `"Knowledge Base Node"`,
  `transferNumber` legacy field.

### Tests (packages/va_engine/tests/)
- `test_nodes.py`: defaults of PathwayNodeData equal §3.1 values; each §3.2.x example JSON parses into its config
  model; alias normalization (4 aliases parametrized) and Global → Default+isGlobal; unknown keys preserved at node,
  data, and config levels; id/name/maxVisits/conditionThreshold/timeoutMs/retries/digits bounds rejected with
  ValidationError locs; invalid NodeType rejected; edge defaults; `effective_transfer_number`.
- `test_graph.py`: fixture round trip `dump_graph(load_graph(x)) == x` for the 3 canonical fixtures (parametrized);
  legacy fixture normalizes and its dump re-loads to an equal graph (idempotent); `graph_hash` stable under key order
  and position/width/height changes, changes when a prompt changes; known-value hash for a tiny graph computed
  independently with hashlib+json.dumps(sort_keys, separators) to pin canonical form; helper methods.
- `test_validation.py`: each rule parametrized over modes asserting code, exact message, severity per §11 table,
  node_id/edge_id; clean fixtures → no issues in publish; refs None skips KB/tool/target checks; self-recursive
  transferPathway; http URL allowed only with allow_http; template detail path; ordering test on a graph with several
  issues; TOO_LARGE short-circuits (501 nodes; 2001 edges; >5 MB prompt text — build via model_construct to stay fast);
  `validation_report` shape (`valid` false only with errors; warnings-only graph valid).
  A test that the code list in RULES equals the spec §11 table (parse `specs/11-pathways-engine.md` table rows with a
  regex: code, message, save, publish columns) — keeps messages/severities drift-checked.
- Coverage target ≥ 90 % on new modules.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_engine -q`, then full `make check` (background +
Monitor heartbeat per AGENTS.md). jscpd: config models are repetitive—use shared helpers / base config to avoid clones.

### Risks
- pydantic `alias_generator` + `extra="allow"`: extras dump under their own key (fine). to_camel("s2xx")—use explicit
  aliases for RouteOnStatus. `from` in SmsConfig needs alias "from".
- `exclude_unset` + before-validator normalization: set fields modified in the raw dict count as set — fine.
- Discriminated union with subclasses inheriting a `type` field: each subclass redeclares `type: Literal[...]`.
- mypy w/o pydantic plugin: construct models in tests via `Model.model_validate({...})`.

## Build P07-T01 (done)
- Files: va_engine/models/{nodes,graph}.py, va_engine/validation.py, models/__init__.py re-exports; tests test_nodes/test_graph/
  test_validation + support/graph_helpers.py; fixtures tests/fixtures/bland/{restaurant_ordering,appointment_booking,
  lead_qualification,legacy_import}.json; specs/11 §11 Decision P07-T01; AGENTS.md bullet.
- Deviations from plan: modelOptions.temperature default null (precedence §13); alwaysPick edges still need labels
  (spec rule literal); forwardingNode object counts as a TARGET_MISSING check; empty custom code always reported.
- Gates: make fmt/lint/typecheck/placeholders pass; make check exit 0 (11386 passed, 7 skipped); new modules 100% cov;
  radon avg 2.78; jscpd 0 clones; pip-audit clean; pnpm audit 3 moderate (no high). No e2e (no call flow touched).

## Review P07-T01 (approved)
- Re-ran: make lint/typecheck/placeholders pass; make test exit 0 (11386 passed, 7 skipped, total cov 99%); radon avg 2.78; jscpd 0 clones on va_engine.
- Checked models against specs/11 §3.1–§3.4 and validation against the §11 table (codes, messages, severities drift-tested by parsing the spec). Deviations (temperature null default, transferNumber kept as stored, open bounds) are recorded in the §11 Decision. graph_hash matches specs/12 §1 (sorted keys, layout excluded).

## Plan P07-T02 — Templating engine (specs/11 §8)

Context: `va_engine.templating` (parser, scope layers, filters, render/render_url/render_json, MissingVariables) and
`va_engine.builtins` already exist from P05-T09a (spec §8 Decision P05-T09a) with unit + small hypothesis tests.
This task closes the remaining gaps: robustness (fuzz found real crashes), the spec deliverable signature
`render(template, scope)` taking text, golden tests from the spec examples, and a grammar-driven fuzz suite.

Verified crashes on HEAD (uv run python):
- `{{b}}` with b = 2**70 → `TypeError: Integer exceeds 64-bit range` (orjson); same for `|json`.
- `{{d}}` with d = {1: 2} → `TypeError: Dict key must be str`.
- `{{t|date:"%Y"}}` with t = "0001-01-01T00:00:00+00:00", tz America/New_York → `OverflowError` (astimezone);
  "9999-12-31T23:59:59-05:00" in Asia/Tokyo too.

### Changes — `packages/va_engine/src/va_engine/templating.py`
1. `_json_text(value: object) -> str` (used by `stringify` and the `json` filter):
   `orjson.dumps(value, default=str, option=orjson.OPT_NON_STR_KEYS).decode()`; on `orjson.JSONEncodeError` (big ints,
   etc.) fall back to `json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)` where
   `_jsonable` recurses: Mapping → `{_key(k): _jsonable(v)}` (`_key`: str as-is, bool → "true"/"false", None → "null",
   else `str(k)`, matching orjson OPT_NON_STR_KEYS), list/tuple → list, float non-finite → None (orjson's behaviour),
   str/int/bool/None/float as-is, anything else → `str(value)`. Never raises.
2. `_format_date`: wrap the `astimezone` and `strftime` in one try catching `(ValueError, OverflowError)` → return
   `stringify(value)` (value unchanged, as the Decision already says).
3. `@lru_cache(maxsize=1024) def cached_template(text: str) -> Template` = parse_template (exceptions are not cached;
   Template is frozen so sharing is safe). `render(template: Template | str, scope)` and `render_url(...)` accept text
   (parsed via `cached_template`, raising `TemplateSyntaxError`); `render_text` stays (delegates to `render`).
   `render_json` string leaves use `cached_template` too. Add `cached_template` to `__all__`.
4. Docstring of the module mentions both.

### Spec — specs/11 §8: append `> Decision (2026-11-03): templating robustness (P07-T02).`
Rendering never raises for any value: integers beyond 64 bits render as their digits, non-string mapping keys as
their JSON text (`1` → `"1"`, `true` → `"true"`), non-finite floats as `null`, other non-JSON values as `str()`;
`date:` keeps the value unchanged when the timezone conversion overflows (years 1/9999). `render`/`render_url`
accept a parsed `Template` or text (parsed through a 1 024-entry LRU cache, `cached_template`); only parsing raises
(`TemplateSyntaxError`). Golden cases live in `packages/va_engine/tests/fixtures/templating/golden.json`.

### Tests
- `packages/va_engine/tests/fixtures/templating/golden.json`: list of cases `{name, template (str or JSON value for
  mode json), mode: "text"|"url"|"json", scope: {variables, request_data, builtins, node_vars?, input?, timezone?},
  expected? , error?: {message, position}, missing?: [paths]}`. Must include every spec §8 example: `"{{qty}}"`
  qty=3 → 3 (json mode), `+14155550100|phone_spoken` → `4 1 5, 5 5 5, 0 1 0 0`, `+442071838750` →
  `0 2 0, 7 1 8 3, 8 7 5 0`, `date:"%B %d"` in call tz (aware UTC ISO converted to America/Los_Angeles crossing a
  day boundary), `\{{` literal, `request_data.x` and bare request_data key, `input.x` precedence over variables,
  node_vars over variables, `default:"..."` on missing/null/"", built-ins (`lastUserMessage`, `metadata.k`),
  URL encoding (`a b&c` → `a%20b%26c`, literal text kept), whitespace variants, filters chain
  (`trim|upper`), `json` filter on an object, error cases (unterminated, empty, unknown filter, bad index,
  unterminated string) with exact message/position, big int / int-key object / year-1 date robustness cases.
  ~30 cases.
- `packages/va_engine/tests/test_templating_golden.py`: parametrized over the file (ids = name) rendering with a
  fresh `MissingVariables` and asserting expected/missing/error; `test_spec_phone_examples_are_golden` regex-parses
  `specs/11-pathways-engine.md` §8 for `` `+NNN` → `"..."` `` and `` `+NNN` → `...` `` phone examples and asserts each
  renders as written (spec drift check, like test_validation parses §11); `test_render_accepts_text_and_caches`
  (`render("{{a}}", scope)`, `cached_template.cache_info().hits` increments; syntax error from text raises
  `TemplateSyntaxError`).
- `packages/va_engine/tests/test_templating_props.py` (extend): grammar strategy `templates()` building valid sources
  from segments (literal text without `{`/`\`, escaped `\{{`, expressions with random whitespace, paths of idents and
  `[n]`, filters incl. `default:`/`date:` with quoted args containing quotes/backslashes escaped properly);
  `_HOSTILE_VALUES` = recursive JSON-ish values + `st.integers()` unbounded (big ints), dicts with int/bool keys,
  floats incl. nan/inf, extreme ISO strings (`st.datetimes(timezones=st.timezones()|none).map(isoformat)` incl. year 1
  / 9999), arbitrary text; timezones from `["UTC","America/New_York","Asia/Tokyo","Pacific/Kiritimati","Nope/Zone"]`.
  Properties: `test_grammar_templates_always_parse` (never raises), `test_rendering_never_raises` (render, render_url,
  render_json of `{"k": [source]}` with hostile scope → str / JSON-ish, no exception), `test_arbitrary_text_raises_only_
  syntax_errors` (st.text incl. `{`,`}`,`|`,`\` heavy alphabet; any exception other than TemplateSyntaxError fails),
  `test_missing_paths_reported_once` (render twice → sink count == distinct missing). Use `@settings(max_examples=200,
  deadline=None)` only where needed.
- Unit additions in `test_templating.py`: `test_stringify_never_raises` parametrized (2**70 → digits, {1: 2} →
  `{"1":2}`, {True: 1}, nan → `null`, object() → str), `test_json_filter_big_int`, `test_date_filter_overflow_keeps_value`.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_engine -q` (coverage of templating 100 %), then
`make test` in background with Monitor heartbeat (PATH needs /home/ralph/.local/bin). radon/jscpd/pip-audit for the
build.done payload. No e2e (no call flow touched). AGENTS.md: one dated bullet (cached_template/render(text), golden
file location, never-raise rendering).

### Risks
- `OPT_NON_STR_KEYS` changes output for non-str keys only; existing tests unaffected.
- Hypothesis on big structures can be slow → keep max_size small; avoid `deadline` flakes with `deadline=None`.
- lru_cache on a module function: tests asserting cache hits must call `cached_template.cache_clear()` first.

## Build P07-T02 (done)
- templating.py: `_json_text` (orjson OPT_NON_STR_KEYS, stdlib fallback via `_jsonable`), date overflow guarded,
  `cached_template` LRU, `render`/`render_url` accept text, render_json uses cache. Spec §8 Decision P07-T02.
- Tests: golden.json (33 cases) + test_templating_golden.py (spec phone/json drift checks, cache), grammar fuzz +
  hostile values in test_templating_props.py, unit never-raise/overflow cases in test_templating.py.
- Gates so far: fmt/lint/typecheck/placeholders pass; va_engine 384 passed; templating 100% cov; radon avg 3.23;
  jscpd 0 clones; pip-audit clean.
- make test exit 0 (11442 passed, 7 skipped, total cov 99%). No e2e (no call flow touched).

## Review P07-T02 (approved)
- Re-ran: make lint/typecheck/placeholders pass; va_engine 384 passed; make test exit 0 (11442 passed, 7 skipped, 99% cov); radon avg 3.23; jscpd 0 clones.
- Checked against specs/11 §8 + plan acceptance (golden from spec examples, fuzz never raises). Decision P07-T02 recorded in §8.

## Plan P07-T03 — EngineState and versioned serialization (specs/11 §9)

Context: `va_engine.models.state` already has `EngineState` v1 (P05-T09a: NodeFrame, TurnRecord, history cap,
`load_engine_state` = strict `model_validate_json`). Missing per the plan deliverables: pending actions, a versioned
serialization with upgraders, and a committed v1 fixture proving backward compatibility.

### Changes — `packages/va_engine/src/va_engine/models/state.py`
1. `CURRENT_SCHEMA_VERSION: Final = 2`; `EngineState.schema_version: Literal[2] = 2`.
2. New v2 fields on `EngineState` (all defaulted, so v1 data upgrades by adding them):
   - `pending_actions: list[PendingAction] = []` — actions decided but not yet executed (terminal actions waiting
     for a playback mark, queued SMS/DTMF, a pending graph switch); runners append, the executor removes on success.
   - `wait_armed_at_ms: int | None = None` — when the current Wait for Response timer was armed (text channels
     persist it across turns: inactivity close after `waitSeconds*10`, §4).
3. `PendingAction` = `Annotated[Union[...], Field(discriminator="kind")]` of strict (`extra="forbid"`, frozen)
   models, each with `after_mark: str | None = None` (execute after that playback mark echoes, §7):
   - `PendingSay(kind="say", text: str, mark: str, interruptible: bool | None = None)`
   - `PendingDtmf(kind="send_dtmf", digits: str (min_length 1), duration_ms: int = 250 (ge 50, le 1000? use ge=1))`
   - `PendingTransfer(kind="transfer", mode: Literal["cold","warm"]="cold", target: str, caller_id_mode:
     Literal["agent_number","original_caller"]="agent_number")`
   - `PendingEndCall(kind="end_call", reason: str, ended_by: str = "agent")`
   - `PendingSms(kind="send_sms", to: str, from_: str = Field(serialization_alias="from",
     validation_alias=AliasChoices("from","from_")), body: str)` (populate_by_name)
   - `PendingSetVariables(kind="set_variables", variables: dict[str, JsonValue])`
   - `PendingSwitchGraph(kind="switch_graph", pathway_id: UUID, version: int | None, node_id: str | None)`
   - `PendingHumanTakeover(kind="human_takeover")`
   Add `TERMINAL_ACTION_KINDS = frozenset({"transfer", "end_call"})` and `EngineState.take_pending(mark: str | None)
   -> list[PendingAction]` that removes and returns, in order, the actions whose `after_mark` is `None` or == mark
   (pure helper the executor uses; keeps order of the rest).
4. Versioned serialization:
   - `class EngineStateError(ValueError)` with `.version: object` (message `unsupported engine state schema_version
     {v!r}` / `engine state is not a JSON object` / `engine state has no schema_version`).
   - `dump_engine_state(state) -> str` = `state.model_dump_json(by_alias=True)` (canonical form; field order = model
     order). Docstring: the only writer for `conversations.engine_state` / Redis `call:{id}:engine`.
   - `load_engine_state(data: str | bytes | Mapping[str, object]) -> EngineState`: str/bytes → `orjson.loads`
     (orjson.JSONDecodeError → EngineStateError); must be a dict; read `schema_version` (missing → error; `bool`
     is not an int; not in `1..CURRENT` → error); apply `_UPGRADERS[v]` for v in range(version, CURRENT) on a
     shallow copy (never mutates the caller's mapping); then `EngineState.model_validate(raw)` (unknown fields
     still raise ValidationError).
   - `_upgrade_v1(raw) -> dict`: copy, `schema_version=2`, `pending_actions=[]`, `wait_armed_at_ms=None`
     (setdefault-free: v1 forbids these keys, so a v1 doc containing them raises at validation — assign only).
   Docstring: adding a field = bump version + one upgrader + a frozen `engine_state_vN.json` fixture.
5. `models/__init__.py` and `va_engine/__init__.py` (if it re-exports) export: `CURRENT_SCHEMA_VERSION`,
   `EngineStateError`, `dump_engine_state`, `PendingAction` + each Pending* class, `TERMINAL_ACTION_KINDS`.
6. `task_runner.py`: no behaviour change (constructs EngineState with defaults). Builder greps any callers that
   serialize state (`model_dump_json` on EngineState) and switches them to `dump_engine_state`.

### Spec — specs/11 §9
Update the code block: `schema_version: Literal[2] = 2`, add `pending_actions: list[PendingAction] = []` and
`wait_armed_at_ms: int | None = None`, and a short `PendingAction` union listing. Append
`> Decision (2026-11-03): versioned EngineState (P07-T03).` — v2 fields and meaning; `after_mark` semantics; writer
`dump_engine_state` (by_alias, `from` key); reader upgrades older versions step by step (1→2 adds the two fields
with defaults), rejects versions < 1, > current, missing or non-integer with `EngineStateError`; unknown fields still
rejected; every released version keeps a frozen fixture `packages/va_engine/tests/fixtures/engine_state/vN.json` that
must keep loading forever.

### Tests — `packages/va_engine/tests/test_state.py` (extend; update the v1-specific asserts)
- Fixtures: `packages/va_engine/tests/fixtures/engine_state/v1.json` (hand-written realistic v1 pathway state:
  return_stack with 2 frames, visit_counts, nested variables incl. null/bool/list, history with user/assistant/
  tool/system and an interrupted turn, summary, graph_stack_versions, wait_reprompts, ended false) and
  `v2.json` (same + every pending action kind incl. `send_sms` with key `from`, `after_mark` set on end_call/
  transfer, `wait_armed_at_ms`), written in the exact `dump_engine_state` form (single line, no trailing newline,
  or compare parsed JSON — builder: compare `orjson.loads` equality AND `dump_engine_state(load(v2)) == text.strip()`
  so the canonical form is pinned).
- `test_v1_fixture_upgrades`: load v1 → schema_version 2, pending_actions [], wait_armed_at_ms None, every v1 field
  equal to the fixture's values; dump → re-load equal.
- `test_v2_fixture_is_canonical` (pinned dump), `test_round_trip_every_pending_kind` (parametrized over kinds;
  str, bytes, and dict inputs), `test_sms_from_alias` (dump key `from`, accepts `from` and `from_`).
- `test_load_does_not_mutate_input` (dict input unchanged after v1 upgrade).
- `test_rejects_bad_versions` parametrized: 0, 3, -1, "1", True, None, missing → EngineStateError with `.version`.
- `test_rejects_non_object` ("[]", "1", b"null", invalid JSON "{") → EngineStateError.
- `test_v1_with_v2_fields_rejected` (v1 doc containing `pending_actions` → ValidationError, upgrader overwrites? —
  decide: the upgrader assigns defaults, so a v1 doc with those keys silently loses them; instead upgrader raises
  EngineStateError if they are present? Choose: assign only when absent is wrong for v1 strictness → builder
  implements `_upgrade_v1` that raises EngineStateError("v1 engine state carries v2 field ...") when present;
  test asserts that.)
- `test_unknown_fields_rejected` keeps working for state/turn/pending action.
- `test_take_pending`: mixed after_mark list; take(None) returns unmarked in order; take("m1") returns unmarked +
  m1, leaves m2; order preserved.
- hypothesis `test_round_trip_property`: strategy building EngineState with random variables (JSON values,
  bounded), history, pending actions → `load_engine_state(dump_engine_state(s)) == s`; `@settings(max_examples=100,
  deadline=None)`.
- Existing `test_defaults` updated to schema_version 2 and the new defaults; existing schema-version test replaced
  by the parametrized one (version 3 rejected).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_engine -q` (state.py 100 % cov), then `make test`
in background + Monitor heartbeat (PATH += /home/ralph/.local/bin). radon, jscpd, pip-audit for build.done. No e2e
(no call flow touched). AGENTS.md: one dated bullet (dump/load_engine_state, upgrader rule, frozen fixtures dir).

### Risks
- mypy without pydantic plugin: construct `PendingSms` via `model_validate({...})` or `from_=` (validation_alias
  AliasChoices + populate_by_name accepts `from_` kwarg at runtime; mypy sees the field name `from_` — fine).
- Discriminated union with frozen models inside a mutable list: fine. JsonValue in hypothesis: keep ints bounded
  (orjson 64-bit) — use `st.integers(-2**53, 2**53)`, finite floats.
- Any other code asserting `schema_version == 1` (only test_state today) — grep apps/ and tests_e2e too.

## Build P07-T03 (in progress → done when make test passes)
- state.py: v2 (pending_actions discriminated union of 8 frozen Pending* models with after_mark, wait_armed_at_ms),
  take_pending, EngineStateError(problem, version, field) (problem literal avoids TRY003), dump_engine_state,
  load_engine_state(str|bytes|Mapping) with _UPGRADERS {1: _upgrade_v1} (v1 doc carrying v2 field rejected).
- Fixtures engine_state/v1.json + v2.json (prettier-formatted; canonical test compares orjson re-dump), excluded from
  jscpd (frozen copies). Spec §9 code block + Decision P07-T03. AGENTS.md bullet.
- Gates: fmt/lint/typecheck/placeholders/spec_refs pass; va_engine 431 passed, state.py 100%; radon avg 2.70;
  jscpd 0; pip-audit clean.
- make test exit 0 (11489 passed, 7 skipped, 99.05% cov). No e2e (no call flow touched).

## Review P07-T03 (approved)
- Re-ran: make lint/typecheck/placeholders pass, check_spec_refs 0 problems; make test exit 0 (11489 passed, 7 skipped, 99% cov).
- Diff checked against specs/11 §9 and plan deliverables (pending actions, versioned serialization, v1 back-compat fixture). Decision P07-T03 recorded.

## Plan P07-T04 — Decision calls: routing, extraction, loop conditions (specs/11 §5.2–§5.5, specs/10 §3 + §9.1–§9.3)

Scope: the three router-model decision components only (pure inputs → typed result). No orchestration, no event
emission, no state mutation (T05 globals/return stack, T08 turn orchestration/speculation/events use these results).

### Spec conflicts to resolve (one Decision block under specs/11 §5.5, cross-ref note under specs/10 §9.2)
`> Decision (2026-11-03): decision calls (P07-T04).`
1. Prompts, function names and output schemas are specs/10 §9.1–§9.3 (packaged `va_llm/prompts/router.txt`,
   `loop_condition.txt`, `extract.txt`); the inline prompts of §5.3–§5.5 here are superseded (they describe the
   same calls). Router `choice 0` ≡ `path_id == "stay"`; global candidates are `global:{node_id}`.
2. Loop threshold = node `conditionThreshold` (default 6); specs/10 §9.2 "≥ 7" applies only as FakeLLM's own
   `met` flag. The model's `met` field is stored for diagnostics, never decides.
3. `conditionExamples` render as `{examples_block}` in loop_condition.txt before the last line:
   `Examples:\n` + `- Conversation: {caller/agent lines joined " / "} → satisfied: {true|false} ({explanation})` +
   blank line; empty when none. specs/10 §9.2 block updated to show the placeholder.
4. Router examples (`pathwayExamples`): `chosenLabel` maps to the first outgoing edge whose label equals it
   (case-insensitive, stripped); `x` = last user message of the example conversation; examples whose label matches
   no edge, or with no user message, are skipped. `{node_purpose}` = name + first 300 chars of node `prompt`
   (or `text` when no prompt).
5. Router result interpretation (§5.5 steps 4–6 + §5.2): scores = {chosen: confidence} ∪ alternatives (unknown
   ids ignored, first occurrence wins, missing = 0). Local winner = highest-scored local edge (ties: chosen id
   first, then `priority` desc, then edge order) if its score ≥ 5 else none. Global winner = highest-scored global
   id with score ≥ 6 and ≥ best local score (tie → global). Outcome kinds: `global` | `edge` | `fallback` (no
   local winner, node has a fallback edge — every fallback edge counts, `noMatchBehavior` irrelevant when a fallback
   edge exists, per §5.5 rule 5) | `stay`.
6. Failure fallbacks (never raise for LLM problems): router on `LlmUnavailableError`/`StructuredOutputError` →
   specs/10 §9.1 heuristic (exactly one local edge label sharing ≥ 2 content words with the user text, stop-words
   removed → that edge, else stay/fallback rule) with `degraded=True`; loop condition → `met=False`, `score=None`,
   `degraded=True`; extraction → no values, `degraded=True`. Each logs a warning (`engine.router_degraded`,
   `engine.loop_condition_degraded`, `engine.extraction_degraded`, with `reason`), never transcript text.
7. Extraction typed coercion: the per-node schema allows `T | string | null` for integer/number/boolean and
   `string | null` for string (all required); after validation `coerce_value(type, value)`: string → stripped,
   "" → null; integer ← int, integral float, digit strings ("1,200" ok), spoken numbers ("twenty five",
   "a hundred and five", "two thousand"), non-integral → rejected; number ← int/float, numeric strings, spoken
   integers; boolean ← bool, yes/no words (yes, yeah, yep, true, correct, sure / no, nope, false, nah, not). Values
   that fail → null + name in `rejected`. Merge rule (non-null overwrites) stays with the caller.
8. Dynamic schemas: one pydantic model class per distinct option set / variable set, built by
   `va_engine.decision_schemas` under `functools.lru_cache(maxsize=256)`; the names are NOT registered with
   `register_structured_function` (dynamic classes per graph). va_llm `_tool_schema`/`_validator` move from
   `functools.cache` to `lru_cache(maxsize=1024)` so per-graph classes cannot grow memory unboundedly.

### Files
- `packages/va_llm/src/va_llm/prompts/router.txt`, `loop_condition.txt`, `extract.txt` — verbatim specs/10 §9.1–9.3
  System blocks with `{placeholders}` (`{examples_block}` added to loop_condition per Decision 3). Drift test like
  `voice_system.txt` (parse the fenced block after the `### 9.N` heading; extend the existing voice_system drift test
  in va_llm tests, e.g. `test_prompts.py`, parametrized over the three names). Rendering with `str.format_map`-style
  safe substitution: use a small `render_prompt(name, **values)` in `va_llm.prompts` that replaces only `{known}`
  keys (values may contain braces — do NOT use str.format on user text; replace placeholders in one regex pass).
- `packages/va_llm/src/va_llm/structured.py`: `_tool_schema`/`_validator` → `functools.lru_cache(maxsize=1024)`.
- `packages/va_engine/src/va_engine/decision_schemas.py`:
  `route_schema(option_ids: tuple[str, ...]) -> type[BaseModel]` (fields `path_id: Literal[*ids]`, `confidence:
  int ge0 le10`, `alternatives: list[RouteAlternative]` (path_id str, confidence 0–10), `reason: str max 200`) —
  build with `pydantic.create_model`, `Literal.__getitem__(ids)`; `condition_schema()` static `ConditionScore`
  (score 0–10, met bool, missing str ≤ 200); `extraction_schema(vars: tuple[tuple[str, str], ...])` (name, type).
  Model docstrings are sent to the model: none (or instruction-like). Var names that are not valid python
  identifiers: use `create_model` with field aliases (`Field(alias=name)`, internal `f0..fn`) and dump `by_alias`;
  make sure `model_json_schema(by_alias=True)` (default) lists the real names; reject duplicate names (validation
  already does for graphs; dedupe keep first).
- `packages/va_engine/src/va_engine/conversation_text.py` (pure): `transcript_lines(history: Sequence[TurnRecord],
  limit: int) -> str` (`Caller: …`/`Agent: …` for user/assistant, skip tool/system; interrupted assistant turns as
  stored), `example_lines(...)`, `content_words(text) -> frozenset[str]` (lowercase word tokens via
  `va_voice.text_norm.tokens`, minus a fixed English stop-word frozenset), `known_information(variables)` (compact
  orjson, keys starting with `_` excluded, `OPT_SORT_KEYS`).
- `packages/va_engine/src/va_engine/router.py`:
  ```python
  @dataclass(frozen=True, slots=True)
  class DecisionInputs:  # shared by all three
      org_id: uuid.UUID; call_id: uuid.UUID | None
      history: Sequence[TurnRecord]; latest: str; variables: Mapping[str, JsonValue]
  @dataclass(frozen=True, slots=True)
  class RouteCandidate: path_id: str; label: str; edge: PathwayEdge | None; global_node_id: str | None
  @dataclass(frozen=True, slots=True)
  class RouteDecision:
      kind: Literal["edge", "global", "fallback", "stay"]; edge: PathwayEdge | None; global_node_id: str | None
      label: str | None; alternatives: tuple[ScoredOption, ...]  # (label, score) sorted score desc then order
      reason: str; router_ms: int; degraded: bool; llm_called: bool
      def event_data(self, from_node: str) -> dict[str, JsonValue]  # specs/19 call.route data (+ to_node, global)
  class Router:
      def __init__(self, gateway: StructuredPort, *, clock: Clock) -> None
      async def choose(self, graph: PathwayGraph, node: PathwayNode, inputs: DecisionInputs, *,
                       include_globals: bool, webhook_edge: PathwayEdge | None = None) -> RouteDecision
  ```
  `StructuredPort` = Protocol with `call_structured` matching `LlmGateway.call_structured` (va_engine.ports or in
  router.py; LlmGateway satisfies it structurally). Order in `choose`: alwaysPick edge → `edge`, no LLM; webhook
  edge given → `edge`; no local candidates and (no globals or not include_globals) → stay (or fallback when a
  fallback edge exists? — no: §5.5 rule 3 "no candidates → stay"); else one `call_structured("router",
  route_schema(ids), function_name="select_path", description="Choose the next path of the conversation.")` with
  messages [system(router.txt rendered), user(last 8 transcript lines + `Known information: …` + `Latest caller
  message: "…"`)]; globals included only when `include_globals` and the node is not itself global and the graph
  has globals; a global equal to the current node is excluded. `router_ms` from `clock.monotonic()` around the call.
  Also `heuristic_route(edges, text)` pure function (Decision 6).
- `packages/va_engine/src/va_engine/loop_condition.py`: `ConditionResult(score: int | None, met: bool, missing: str,
  model_met: bool | None, threshold: int, degraded: bool, ms: int)` with `message` property `"Likely {score}/10"`
  (`None` score → `"Condition check unavailable"`) and `event_data()` (`score`, `met`); `NOT_MET_SUFFIX` template
  `The following condition is not yet satisfied; keep working toward it: "{condition}"` + `not_met_suffix(cond)`;
  `class LoopCondition(gateway, *, clock)` → `async def score(node, inputs) -> ConditionResult | None` (None when
  the node has no condition; messages system(loop_condition.txt) + user(last 10 transcript lines + latest)).
- `packages/va_engine/src/va_engine/extraction.py`: `ExtractionResult(values: dict[str, JsonValue] (non-null only),
  nulls: tuple[str,...], rejected: tuple[str,...], degraded: bool, ms: int)`, `event_data()` = `{"variables":
  values}`; `coerce_value(type, raw) -> JsonValue | _Rejected`; `class Extractor(gateway, *, clock)` →
  `async def extract(node, inputs, *, now: datetime, timezone: str) -> ExtractionResult | None` (None when no
  extractVars). `{now_local}` = `now.astimezone(ZoneInfo(tz))` formatted `%A %Y-%m-%d %H:%M` with fixed English day
  names (locale-free table, see AGENTS.md calendar gotcha); bad tz → UTC. Messages system(extract.txt) + user (last
  10 transcript lines + latest). `background` vs `before_response` scheduling is the caller's (T08); docstring says so.
- `packages/va_engine/src/va_engine/spoken_numbers.py`: `parse_spoken_integer(text) -> int | None` (zero–nineteen,
  tens, hundred, thousand, million, billion; "a"/"and"/hyphens; digit strings with commas; negative "minus").
- Export the public names from `va_engine/__init__.py` only if that file already re-exports (check; else leave).

### Tests (packages/va_engine/tests/)
Unit with a fake `StructuredPort` (records schema/messages/function_name, returns scripted instances or raises):
- `test_router.py`: alwaysPick skips LLM; webhook edge; no candidates → stay without LLM; chosen edge ≥5;
  chosen <5 → stay / fallback edge; `stay` with fallback edge → fallback; ties (alternative equal confidence: chosen
  id wins; two alternatives equal and chosen=stay below 5 irrelevant; equal-score non-chosen → priority desc then
  order); global ≥6 beats local; global tie with local → global; global 5 → ignored; global excluded when
  include_globals=False / node is global / self; enum of schema = edge ids + stay + globals in order; prompt render
  snapshot (exact text for a 2-edge + 1-global node with examples and description) — compare against an inline
  expected string; examples mapping (unknown label skipped); user message has last 8 lines + known info without
  `_secret`; degraded on LlmUnavailableError and StructuredOutputError → heuristic edge (2 shared words) and stay
  (0 or 2 matching edges); alternatives sorted, unknown ids dropped; `event_data` shape; router_ms from ManualClock.
- `test_loop_condition.py`: threshold default 6 (score 6 met, 5 not), node threshold 8; `model_met` ignored;
  examples block rendered/absent; degraded → met False; message strings; suffix text; no condition → None.
- `test_extraction.py`: parametrized `coerce_value` table (spoken numbers, "1,200", 3.0 → 3, 3.5 integer rejected,
  "yeah" → True, "nope" → False, "maybe" rejected, "" → null); schema: every var required + nullable, aliases for
  non-identifier names ("first name", "1st"), cache returns same class for same vars; result splits values/nulls/
  rejected; degraded; prompt lines `- name (type): description`; now_local formatting incl. invalid tz → UTC.
- `test_spoken_numbers.py`: table incl. edge cases + hypothesis: `parse_spoken_integer(str(n)) == n`.
- `test_decision_schemas.py`: tool_schema(route_schema(...)) validates FakeLLM-like outputs; lru caching identity.
- FakeLLM-driven integration `test_decisions_sim.py` (using `task_helpers.served()` Sim + real `LlmGateway`):
  routing by heuristic (caller text sharing label words → edge), `Sim.rules` scripting a malformed `select_path`
  argument (path_id not in enum / confidence 11) first then valid → repaired result (assert 2 requests, second has the
  repair user message); malformed twice → degraded heuristic; `score_condition` via rule returning score 7 vs
  threshold 8; extraction via sim heuristic of an integer var from "I need four tables" or a rule returning
  `"four"` → coerced 4; rule with fault `timeout`/500 → degraded results. Check how FakeLLM rules script tool-call
  arguments (llm rules docs in specs/30 §3.4 / `va_sim/llm/rules.py`) and malformed args (`raw_arguments`?) — use
  whatever the rules engine supports; if it cannot emit invalid JSON, emit schema-invalid values.
- va_llm: prompt drift test for the three new prompt files; `render_prompt` leaves braces in values untouched;
  structured cache is bounded (`_tool_schema.cache_info().maxsize == 1024`).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_engine packages/va_llm -q` (new modules ≥ 95 %,
handlers N/A), `uv run python scripts/check_spec_refs.py`, then `make test` in background with Monitor heartbeat
(PATH += /home/ralph/.local/bin; truncate log first). radon (<10 avg), jscpd 0, pip-audit for build.done. No e2e
(no call flow wired yet). AGENTS.md: one dated bullet (decision modules, prompts, StructuredPort, lru caches).

### Risks
- FakeLLM `select_path` heuristic returns `stay` with confidence 5 when nothing matches: fine (stay).
- mypy with `create_model` + dynamic Literal: type the factory as `type[BaseModel]` and read fields via
  `model_dump(by_alias=True)`, not attribute access.
- jsonschema `enum` of path ids must keep spec order: edges (graph order), `stay`, globals.
- `structured_drift` test requires literal `function_name=` at call sites: keep literals in router/loop/extraction.
- Don't let the Router mutate EngineState (pure result); T05/T08 apply it.

## Build P07-T04 (done)
- New: va_engine `decisions` (StructuredPort, DecisionInputs, text helpers), `decision_schemas` (LRU dynamic route/extraction
  classes, static ConditionScore), `router` (Router.choose, interpret_route, heuristic_route), `loop_condition`,
  `extraction` (coerce_value), `spoken_numbers`. va_llm: prompts router/loop_condition/extract.txt + `render_prompt`;
  structured caches LRU 1024 (`SCHEMA_CACHE_SIZE`).
- Specs: specs/10 §9.2 block gains `{examples_block}` + Decision; specs/11 §5.5 Decision P07-T04 (rules 1-7). Deviations from
  plan: winner rule scores `stay` like an option (edge must beat stay; global must tie-or-beat edge AND stay); ties among edges
  = priority then edge order (not chosen-first); no candidates + fallback edge → fallback without LLM; module for the port is
  `va_engine.decisions` (not conversation_text); extraction `number` returns int for integral values.
- Gates so far: lint/typecheck/placeholders pass, spec_refs 0, va_engine+va_llm 1021 passed (new modules 99-100 %),
  radon avg A 2.91 (new 3.51), jscpd 0, pip-audit clean (pnpm 3 moderate, none high).
- make test exit 0 (11639 passed, 7 skipped, 99.06% cov). No e2e (no call flow wired yet).

## Review P07-T04 (rejected, 1 finding)
- Re-ran: make lint/typecheck/placeholders pass, check_spec_refs 0 problems. Design reviewed against specs/11 §5.2-§5.5,
  specs/10 §9.1-§9.3, specs/19 §2 event data: router/loop/extraction rules, prompts, schemas, event_data shapes OK.
1. `packages/va_engine/src/va_engine/extraction.py` `_number_text` (int(digits)) and
   `packages/va_engine/src/va_engine/spoken_numbers.py` `_digit_value` (`int(text.replace(",", ""))`) raise
   `ValueError: Exceeds the limit (4300 digits) for integer string conversion` for a model-returned digit string longer
   than 4 300 digits: `coerce_value("integer", "1"*5000)`, `coerce_value("number", "1"*5000)` and
   `parse_spoken_integer("9"*5000)` all raise (verified). The extraction string fields are unbounded, so
   `Extractor.extract` raises out of a turn, contradicting the Decision P07-T04 rule 5/6 contract ("values that fail
   coercion become null and are listed in rejected"; "failures never raise"). Fix: cap the accepted digit length (e.g.
   reject strings with more than ~30 digits, or catch ValueError → REJECTED / None) in both places, and add regression
   tests (coerce_value integer/number + parse_spoken_integer with a 5 000-digit string → REJECTED / None; a hypothesis
   property that coerce_value never raises for arbitrary text would also cover it).

## Build P07-T04 rework (finding 1)
- `spoken_numbers.MAX_DIGITS = 18`: `_digit_value` returns None for longer digit runs (no `int()` on them).
- `extraction._number_text` rejects digit strings with > 18 integer digits before converting; `_number` rejects ints
  outside signed 64-bit (also integral floats like 1e300). Spec specs/11 §5.5 Decision rule 5 states the bound.
- Tests: 5 000-digit / comma / spoken-scale cases in both test files, the 20-digit case now REJECTED, hypothesis
  `test_coerce_value_never_raises` over text/ints/floats for every type.
- Build P07-T04 rework gates: lint/typecheck/placeholders pass, spec_refs 0, make test exit 0 (11654 passed, 7 skipped, 99.06%), radon avg A 3.90 for touched modules.

## Review P07-T04 rework (approved)
- Finding 1 fixed: 5 000-digit strings → REJECTED/None, huge spoken scales → None, 1e300 → REJECTED (verified by hand).
- make lint/typecheck/placeholders pass, check_spec_refs 0, make test exit 0 (11654 passed, 7 skipped, 99.06%).

## Plan P07-T05 — Global nodes and return stack (specs/11 §2, §3.1, §4 "Global" row + paragraph, §5.2, §9)

Scope: pure navigation over `EngineState` (no events, no LLM — the router already scores globals, P07-T04).
T08 (turn orchestration) will call this; T07's Transfer Pathway reuses the pathway switch + loader port.

### Spec decisions (append `> Decision (2026-11-03): global nodes and return stack (P07-T05).` under the §4
"After a global node finishes…" paragraph in specs/11, and update the §9 `NodeFrame` block with `kind`)
1. Frames carry `kind: "global" | "transfer_pathway"`. Entering a global node from the router (`RouteDecision.kind ==
   "global"`) pushes `NodeFrame(kind="global", pathway_id=state.pathway_id, version=state.pathway_version,
   node_id=current)`. The global check (§5.2) runs only when the current node is not global and the graph has a
   global node other than the current one (same rule as `Router._global_candidates`) — so globals never nest
   directly; "nested" = a global triggered from a node reached after an earlier global (forward/return/edge).
2. After a node executed (`finish_node`): `enableForwarding` with a `forwardingNode` (ANY node type, §3.1) →
   forward: a string id jumps inside the current graph; an object `{pathway_id, version, node_id}` switches graph via
   the `PathwayLoader` port (current pathway/version replaced, `node_id` null → target start node, variables kept,
   `graph_stack_versions` untouched). Forwarding out of a global node discards that node's global frame (no return).
   Else, for a global node with `enableGlobalAutoReturn` (default true) → pop the top frame (must be kind global;
   otherwise no return) and make the popped node current WITHOUT re-speaking and without a visit increment.
   Else stay on the node.
3. Leaving a global node by an edge/fallback route (auto-return off) discards its global frame (top frame, kind
   global). Leaving it by a new global is impossible (rule 1).
4. Entering a node (edge, fallback, global, forward) increments `visit_counts[node_id]` and reports
   `over_limit = visits > node.maxVisits` (T08 applies loop protection). Returning never counts.
5. `prevNodePrompt` while a global node executes = the top global frame's node `prompt` (else `text`, else "") when
   the frame belongs to the current pathway; `None` otherwise (T08 puts it into the template scope / §5.7).
6. Stack depth cap `MAX_RETURN_STACK = 16`: pushing beyond drops the bottom frame (log `engine.return_stack_trimmed`).
7. Broken targets never raise out of a turn: a forward id not in the graph, a frame node missing after a pop, or a
   loader failure (`PathwayUnavailableError`) → stay on the current node (`Transition.kind="stay"`, `problem` =
   `forward_target_missing` | `return_target_missing` | `pathway_unavailable`), logged `engine.navigation_failed`
   with the reason, frame still dropped for a missing return target.

### State schema v3 (`packages/va_engine/src/va_engine/models/state.py`)
- `NodeFrame.kind: Literal["global", "transfer_pathway"]` (required). `CURRENT_SCHEMA_VERSION = 3`,
  `schema_version: Literal[3] = 3`. Upgrader `_upgrade_v2`: each `return_stack` frame gets `kind` =
  `"transfer_pathway"` when its `pathway_id` differs from the state's `pathway_id`, else `"global"`; a v2 frame that
  already has `kind` → `EngineStateError("newer_field", version=2, field="return_stack.kind")`. `_UPGRADERS = {1:
  _upgrade_v1, 2: _upgrade_v2}` (chain 1→2→3; v1 frames get kinds too through the chain). Don't mutate input.
- New frozen fixture `packages/va_engine/tests/fixtures/engine_state/v3.json` (copy v2.json content + kinds, include
  one frame of each kind); v1/v2 fixtures stay untouched. Update `test_state.py`: current version 3, v3 fixture
  canonical test, v2 fixture upgrades (frames kinds derived), v2 doc with `kind` rejected, existing v1 tests keep
  passing (adjust asserts expecting `== 2`). Update spec §9 block (`schema_version: Literal[3] = 3`, NodeFrame kind)
  and AGENTS-style note in the Decision.
- grep other users of `NodeFrame(`/`schema_version` (task_runner, media, tests) and update constructors.

### New module `packages/va_engine/src/va_engine/navigation.py`
```python
MAX_RETURN_STACK: Final = 16
class PathwayUnavailableError(Exception): reason: str
@dataclass(frozen=True, slots=True)
class ActiveGraph: pathway_id: UUID | None; version: int | None; graph: PathwayGraph
class PathwayLoader(Protocol):
    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph  # raises PathwayUnavailableError
TransitionKind = Literal["stay", "enter", "global", "return", "forward"]
@dataclass(frozen=True, slots=True)
class Transition:
    kind: TransitionKind; from_node: str; to_node: str
    pathway_changed: bool = False; visits: int = 0; max_visits: int | None = None
    problem: str | None = None
    @property over_limit -> bool   # max_visits is not None and visits > max_visits
    @property speaks -> bool       # kind in {"enter", "global", "forward"} (return/stay: no re-speak)
class Navigator:
    def __init__(self, state: EngineState, active: ActiveGraph, *, loader: PathwayLoader | None = None)
    state, active, graph (property), node (current PathwayNode; raises LookupError only if state is corrupt —
      constructor validates current node exists, else ValueError)
    def global_check_enabled(self) -> bool
    def apply(self, decision: RouteDecision) -> Transition      # stay/edge/fallback/global (rules 1, 3, 4)
    async def finish_node(self) -> Transition                  # rule 2 (+5 problems → rule 7)
    def prev_node_prompt(self) -> str | None                   # rule 5
    def enter(self, node_id: str) -> Transition                # used by apply/forward; T07/T08 reuse
```
`apply` checks `decision.from_node == state.current_node_id` (ValueError otherwise — programming error).
Pathway switch helper `_switch(active: ActiveGraph, node_id: str | None)` sets `state.pathway_id/pathway_version/
current_node_id`; target start node = `graph.start_nodes()[0]` (none → problem `forward_target_missing`).
Logging via `va_common.logging.get_logger`; never log conversation text.

### Tests
- `packages/va_engine/tests/support/navigation_helpers.py`: `MemoryLoader(graphs: dict[(pathway_id, version_ref)
  → ActiveGraph], fail=set())` recording `loads`; `nav_state(current, pathway_id=P1, version=1)`;
  `global_graph()` builder (start "greet" → "order" → "confirm"; globals "hours" (auto-return), "human"
  (auto-return false, edge → "goodbye"), "menu" (enableForwarding → "order"), "other_pathway" (forwardingNode
  object to pathway P2)); `route(node, kind, to)` making `RouteDecision`s.
- `packages/va_engine/tests/test_navigation.py` (unit): global entry pushes kind-global frame with pathway/version;
  visits/over_limit; global check disabled on a global node / graph with only the current global / no globals;
  auto-return pops and `speaks` False, visit count unchanged; auto-return off → stays, then edge route drops frame;
  fallback route from global drops frame; forwarding by id from global drops frame and enters target (speaks);
  forwarding from a non-global node; forwarding to pathway object loads via loader (version ref passed through,
  node_id None → start), state pathway fields switched, variables kept; loader failure / missing forward id /
  missing pop target → stay with `problem` and no raise; top frame of kind transfer_pathway is never popped by
  auto-return; stack cap 16 drops bottom; `prev_node_prompt` prompt/text/""/None; `apply` with mismatched
  from_node raises; Navigator with unknown current node raises ValueError.
- `packages/va_engine/tests/test_global_scenarios.py` (acceptance "nested global triggers"): drive Router (real,
  `decision_helpers.ScriptedPort` scripted `select_path` outputs) + Navigator over multi-turn scenarios:
  (a) greet → "what are your hours?" → global hours (≥6 beats local) → auto-return to greet (stack empty), next turn
  routes greet's edge; (b) nested: greet → global `menu` forwards to order (frame dropped) → from order a global
  `hours` triggers → returns to order (not greet), stack depth trace asserted each step; (c) global tie with
  local edge → global wins; global score 5 → local edge; (d) global `human` without auto-return, then edge route
  away clears the stack, then a second global from the new node returns there; (e) forward to another pathway then
  a global of the NEW graph triggers and returns inside it (frame pathway = P2). Assert router requests offered
  `global:` options only when `global_check_enabled()`. Round-trip the state through `dump_engine_state`/
  `load_engine_state` between turns (proves frames serialize).
- Coverage of navigation.py ≥ 95 %.

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_engine -q`, `uv run python
scripts/check_spec_refs.py`, then `make test` in background (PATH=/home/ralph/.local/bin:$PATH; truncate
logs/make_test.log first; Monitor heartbeat). radon avg < 10, jscpd 0, pip-audit for build.done. No e2e.
AGENTS.md: one dated bullet (navigation module, NodeFrame.kind v3, loader port, fixture v3).

### Risks
- Other code constructing `NodeFrame` without `kind` or asserting schema_version 2 (grep repo, incl. apps/ and
  fixtures used by media/api) — fix all.
- Spec §9 code block may be drift-checked by a test; update block and test consistently.
- Keep Navigator free of router imports beyond `RouteDecision` type (no cycle: router doesn't import navigation).

## Build P07-T05 (done)
- New `va_engine.navigation` (Navigator/ActiveGraph/PathwayLoader/PathwayUnavailableError/Transition, MAX_RETURN_STACK 16);
  EngineState v3 (`NodeFrame.kind` required, `_upgrade_v2` derives kind by pathway id match, case-insensitive text);
  fixture v3.json; spec §4 Decision P07-T05 (8 rules) + §9 block updated; AGENTS.md bullet.
- Deviation from plan: extra problem `target_missing` (route/enter target not in graph, drafts can have them);
  failed forward keeps the global frame; loader failures also log `engine.pathway_load_failed` with reason.
- Tests: test_navigation.py (unit, 100 % navigation.py), test_global_scenarios.py (real Router + ScriptedPort, state
  round-trip per turn), test_state.py v3 updates.
- Gates: fmt/lint/typecheck/placeholders pass, spec_refs 0, va_engine 647 passed, radon avg A 2.5, jscpd 0,
  pip-audit clean, pnpm 3 moderate (none high).
- make test exit 0 (11712 passed, 7 skipped, 99.06% cov). No e2e (navigation not wired into a call flow yet).

## Review P07-T05 (approved)
- Re-ran: make lint/typecheck/placeholders pass, check_spec_refs 0, make test exit 0 (11712 passed, 7 skipped, 99.06 %,
  navigation.py 100 %). Diff checked against specs/11 §3.1/§4/§5.2/§9 and the P07-T05 Decision: frame push/pop, forward
  frame discard, transfer_pathway frames never auto-popped, visit counting, v2→v3 upgrader (no input mutation).

## Plan P07-T06 — Node handlers: Default, Wait for Response, End Call, Transfer Call (cold), Global (specs/11 §4, §7)

Scope: pure handlers producing engine actions; no routing/orchestration (T08), no prompt assembly (T08 implements the
`DialoguePort` with §5.7 + speculation). Global = Default handler (navigator already pushes the frame, P07-T05).

### Spec decision — append `> Decision (2026-11-03): node handlers (P07-T06).` under the P07-T05 Decision in specs/11 §4
1. Handlers implement `on_enter(ctx)`, `respond(ctx)` → `HandlerResult(actions: tuple[Action, ...], problem: str|None)`
   (not a bare list: the problem string reports a broken config without raising) and `waits_for_user(node)`.
   `HandlerResult.terminal` = any `TransferAction`/`EndCallAction`/`HumanTakeover`.
2. Speech: static `text` → `Say(rendered)`; otherwise prompt → `SayStream(ctx.dialogue.stream(ctx))` (the turn
   orchestration's dialogue generator, §5.7/§5.8). Templates with a syntax error are spoken raw (`engine.template_invalid`).
   `modelOptions.block_interruptions` → `interruptible=False`, else `None` (call level).
3. Final lines (End Call, Transfer): config `speechMode: "prompt"` with a non-blank `speechPrompt` composes one line through
   `LinePort.compose` (dialogue call type); failure/blank → the rendered `speech` (`engine.final_line_fallback`, reason).
   `static` → rendered `speech`. No config speech → node `text`, else node `prompt` composed (failure → no line). No line
   → the terminal action is due at once (`after_mark=None`). Final lines are not interruptible.
4. End Call: `Say(line)` + `EndCallAction(reason="agent_end_call", ended_by="agent", after_mark=line mark)`; sets
   `state.ended`/`end_reason`. Text channels identical (the line is the last message, the action closes the conversation).
5. Transfer Call (voice): target = rendered `effective_transfer_number`; a key of the call `transfer_list` resolves to
   its number; must be E.164 (`^\+[1-9]\d{6,14}$`) or a `sip:`/`sips:` URI. Valid → `Say(line)` + `TransferAction(mode,
   target, caller_id_mode, warm, after_mark)` (warm config passed through; phase 12 executes warm). Invalid/missing →
   `Say(errorPhrase)`, problem `transfer_target_invalid`, no terminal action (logged without the number). `sms` channel:
   `Say(data.transfer.textModeMessage or "A team member will reach out shortly.")` + `HumanTakeover`; widget channels:
   `HumanTakeover` only.
6. Wait for Response (`waitForResponse` absent = defaults): `on_enter` resets `wait_reprompts`, sets `wait_armed_at_ms`
   and returns `Wait(seconds)`; text channels arm `waitSeconds*10` only for `onTimeout="end_call"` (else nothing armed).
   `respond` speaks text/prompt when present, else nothing. `timeout(ctx)` → `WaitTimeout(result, move_to)`: voice
   with `repromptText` and reprompts < `maxReprompts` → `Say(reprompt)` + re-armed `Wait`; then `onTimeout`:
   `end_call` → `EndCallAction("no_input_timeout", ended_by="system")`; `move_to_node` → `move_to=timeoutNodeId`
   (missing/unknown id → end_call, `engine.wait_timeout_target_missing`); `repeat` → reprompts reset, `respond` again +
   `Wait`. Text channels: timeout → end_call only. `disarm(state)` clears the timer when the user speaks.
7. Waits for user: Default/Global unless `skipUserResponse`; Wait for Response always; End Call/Transfer never.

### Files
- `packages/va_engine/src/va_engine/models/results.py`: add frozen dataclasses (kw_only, slots) `Say(text, mark,
  interruptible: bool|None=None)`, `SayStream(tokens: AsyncIterator[str], mark, interruptible=None)`, `Wait(seconds:
  float)`, `TransferAction(mode, target, caller_id_mode, warm: WarmTransfer|None, after_mark: str|None)`,
  `EndCallAction(reason, ended_by="agent", after_mark=None)`, `HumanTakeover(after_mark=None)`; `type Action = Say |
  SayStream | Wait | TransferAction | EndCallAction | HumanTakeover` (T07 extends); `TERMINAL_ACTIONS`. Keep existing
  TransferRequest/PressButton. Export from `va_engine.models`.
- `packages/va_engine/src/va_engine/handlers/__init__.py` (re-exports), `base.py` (`Channel` Literal voice|sms|widget_chat|
  widget_voice + `is_text(channel)`; `DialoguePort` Protocol `stream(ctx) -> AsyncIterator[str]`; `LinePort` Protocol
  `async compose(ctx, instruction: str) -> str` raising `LineUnavailableError`; `MarkSequence` (`next()` →
  `f"t{turn_index}.{n}"`); `TurnContext` dataclass (state, graph, node, channel, scope_factory/`render(text)`,
  dialogue, lines, transfer_list, now_ms callable, marks, logger fields); `HandlerResult`; `NodeHandler` Protocol;
  helpers `render_text`, `speak_node(ctx)` (rule 2), `final_line(ctx, speech, mode, prompt)` (rule 3)),
  `default.py` (`DefaultHandler`), `wait_for_response.py` (`WaitForResponseHandler`, `WaitTimeout`, `disarm`),
  `end_call.py`, `transfer_call.py` (`resolve_transfer_target(ctx) -> str|None`, `DEFAULT_TEXT_MODE_MESSAGE`),
  `registry.py` (`handler_for(node) -> NodeHandler`, raises `UnsupportedNodeError(node_type)` for types without a
  handler yet; `HANDLERS` mapping by NodeType).
- `packages/va_engine/src/va_engine/final_lines.py`: `GatewayLinePort(gateway: CompletionPort, *, org_id, call_id,
  model=None, temperature=0.7, history_turns=10)`; `CompletionPort` Protocol mirroring `LlmGateway.complete`. Messages:
  system = rendered global prompt (if any) + `"\n\n## Your next line\nWrite only the line you say now: one or two short
  sentences, no markdown. Instruction: {instruction}"`; then last 10 user/assistant history turns; `max_tokens=120`.
  `LlmUnavailableError` or blank `content` → `LineUnavailableError(reason)`. Temperature: node modelOptions → global
  defaultTemperature → 0.7.
- specs/11 §4 Decision as above. AGENTS.md: one dated bullet (handlers package, ports, HandlerResult, final lines).

### Tests (packages/va_engine/tests/)
- `support/handler_helpers.py`: `FakeDialogue(tokens=("Hi ", "there"))` (records ctx node ids), `FakeLines(reply=str|
  Exception)` (records instructions), `handler_ctx(node_dict, channel="voice", variables=, transfer_list=, graph=,
  global_config=, now_ms=1000)` built on `graph_helpers`, `drain(stream)`.
- `test_handler_default.py`: static text rendered (`{{first_name}}`), template syntax error spoken raw, prompt → SayStream
  whose tokens come from the dialogue port, block_interruptions → interruptible False, waits_for_user vs
  skipUserResponse, on_enter empty, Global node (`type: "Global"` raw) uses the same handler and behaves identically;
  parametrize voice/sms/widget_chat (§4 text mode = same).
- `test_handler_end_call.py`: static speech; speechMode prompt → composed line + instruction rendered; prompt fallback to
  speech on `LineUnavailableError` and on blank compose; no endCall config → node text, then node prompt composed;
  nothing → EndCallAction with after_mark None; mark linkage; state.ended/end_reason; Say not interruptible; text
  channels same actions.
- `test_handler_transfer.py`: E.164 number, legacy `transferNumber`, `{{var}}` number, transfer_list key, SIP URI,
  invalid/missing → errorPhrase + problem and no terminal; speech prompt mode + fallback; caller_id_mode + warm pass
  through; sms → textModeMessage default/custom + HumanTakeover; widget_chat/widget_voice → HumanTakeover only;
  logs never contain the number (json_log_stream).
- `test_handler_wait.py`: on_enter arms (Wait seconds, wait_armed_at_ms, reprompts reset); text-channel arming rules;
  respond with text/prompt/none; timeout reprompt sequence up to maxReprompts then each onTimeout (end_call reason
  no_input_timeout/system, move_to_node, missing target → end_call, repeat resets); no repromptText → straight to
  onTimeout; text timeout end_call; disarm.
- `test_handler_registry.py`: each T06 type maps to its handler; unsupported types raise UnsupportedNodeError.
- `test_final_lines.py`: GatewayLinePort with a recording fake gateway (system text, history window, temperature
  precedence, max_tokens, blank/unavailable → LineUnavailableError) + one FakeLLM contract via `task_helpers.served()`.
- Coverage: every new module ≥ 95 % (handlers ≥ 90 % branch, spec §15).

### Gates
`make fmt lint typecheck placeholders`, `uv run pytest packages/va_engine -q`, `uv run python scripts/check_spec_refs.py`,
`make test` in background (PATH=/home/ralph/.local/bin:$PATH; truncate logs/make_test.log; Monitor heartbeat), radon avg
< 10, jscpd 0, pip-audit. No e2e (handlers not wired into calls yet).

### Risks
- `TransferRequest`/`PressButton` (pipeline BrainActions) vs new engine actions: keep both; T12 maps engine actions.
- AsyncIterator inside a frozen dataclass: fine (no hashing); don't make Say/SayStream comparable by tokens in tests.
- `TransferConfig` has no `textModeMessage` field: read it from `model_extra` (extra=allow), don't add a field (keeps
  Bland round trip exact) — or add an optional field with alias if validation/dump tests stay green; prefer model_extra.
- Don't log transfer targets or conversation text.

## Build P07-T06 (done when make test passes)
- New `va_engine.handlers` (base: TurnContext/HandlerResult/ports/MarkSequence/final_line/speak_node; default,
  end_call, transfer_call, wait_for_response, registry), `va_engine.final_lines.GatewayLinePort`, §7 actions in
  `models/results.py` (exported from `va_engine.models`). Spec: specs/11 §4 Decision P07-T06 (7 rules). AGENTS.md bullet.
- Deviations from plan: E.164 targets starting with `+` have separators removed; blank rendered instruction also
  falls back (reason `blank_instruction`); line port model precedence node → dialogueModel → constructor model.
- Tests: test_handler_{default,end_call,transfer,wait,registry}.py, test_final_lines.py (incl. FakeLLM contract);
  all new modules 100 % (branch). va_engine 757 passed. radon avg A 2.64, jscpd 0, pip-audit clean, pnpm 3 moderate.
- make test exit 0 (11821 passed, 7 skipped, 99 % total). No e2e (handlers not wired into calls yet).

## Review P07-T06 (approved)
- Re-ran: lint/typecheck/placeholders pass, check_spec_refs 0, make test exit 0 (11821 passed, 7 skipped, 99.07 %;
  handlers/* and final_lines.py 100 %). Checked against specs/11 §3.2.2/§3.2.3/§3.2.9, §4 rows, §7 and the
  P07-T06 Decision; end reasons `agent_end_call`/`no_input_timeout` match specs/05 §9.
- Non-blocking notes for T08: the §4 `NodeHandler` protocol lists `node_type`; handlers are keyed by the registry
  instead. `HandlerResult.terminal` counts a warm `TransferAction` as terminal (§4 says warm may return): revisit in
  phase 12.
