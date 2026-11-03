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

## Plan P07-T07 — Node handlers: Webhook, SMS, Press Button, Transfer Pathway, Scheduling (specs/11 §3.2.1/§3.2.4–§3.2.7, §4, §7; specs/13 §5 steps 3–6, §9)

Scope: the five handlers + the shared HTTP executor/JSONPath core in a new `packages/va_tools`. No turn orchestration
(T08: it applies `route_to`/`route_fallback`, `SwitchGraph`, events, chaining), no tool CRUD/LLM function exposure
(phase 08), no media wiring (T12). Engine stays transport-agnostic: every side effect goes through ports.

### Dependency direction (avoid a cycle)
specs/13 §1 has `va_tools.render` using `va_engine.templating`, so **va_tools depends on va_engine, never the
reverse**. Port protocols + their plain dataclasses live in va_engine (`va_engine/ports.py`); va_tools implements
them. JSONPath runs inside the executor (va_tools), the engine only passes `(name, path)` pairs.

### New `packages/va_engine/src/va_engine/ports.py`
```python
@dataclass(frozen=True, slots=True, kw_only=True)
class HttpCall:            # a fully rendered request
    method: Literal["GET","POST","PUT","PATCH","DELETE"]; url: str; headers: Mapping[str, str]
    body: JsonValue | None; timeout_ms: int; retries: int
    extract: tuple[tuple[str, str], ...] = ()          # (variable name, JSONPath)
type HttpOutcomeKind = Literal["ok", "http_error", "timeout", "connection", "blocked", "invalid_url"]
@dataclass(frozen=True, slots=True, kw_only=True)
class HttpOutcome:
    kind: HttpOutcomeKind; status: int | None; body: JsonValue | str | None; truncated: bool
    extracted: dict[str, JsonValue]; attempts: int; duration_ms: int
    def status_class(self) -> Literal["2xx","3xx","4xx","5xx","timeout"] | None   # connection/blocked/invalid_url → "timeout"
class HttpPort(Protocol):  async def execute(self, call: HttpCall) -> HttpOutcome   # never raises
@dataclass(frozen=True, slots=True, kw_only=True)
class SmsRequest: to: str; from_: str | None; body: str; node_id: str
@dataclass(frozen=True, slots=True, kw_only=True)
class SmsOutcome: sent: bool; message_id: str | None = None; error: str | None = None
class SmsPort(Protocol):   async def send(self, request: SmsRequest) -> SmsOutcome    # never raises
class ExtractionPort(Protocol): async def extract(self, ctx: TurnContext) -> ExtractionResult | None  # T08 adapts Extractor
class ActionSink(Protocol): def queue(self, action: Action) -> None   # non-blocking: speech that must start NOW
```
`TurnContext` (handlers/base.py) gains optional fields `http: HttpPort | None = None`, `sms: SmsPort | None = None`,
`extractor: ExtractionPort | None = None`, `sink: ActionSink | None = None`, `now: Callable[[], datetime] | None`
(scheduling lookahead; default derive from `now_ms`). A missing port = problem (`http_unavailable`/`sms_unavailable`),
never a raise. `HandlerResult` gains `route_to: str | None = None`, `route_fallback: bool = False`,
`reports: tuple[NodeReport, ...] = ()` where `NodeReport = WebhookReport | SmsReport | ExtractionReport`, each a
frozen dataclass with `event_type` (`call.webhook_node` / `call.sms_sent` / `call.extraction`) and `event_data()`
(T08 emits them in order). Keep the defaults so T06 handlers/tests stay untouched.

### New actions (`models/results.py`, exported from `va_engine.models`; extend `Action` union)
`SendDtmfAction(digits, duration_ms, after_mark: str | None)`, `SwitchGraph(pathway_id: str, version:
PathwayVersionRef, node_id: str | None, carry_variables: bool, return_on_end: bool)`. Not terminal. No
`SendSmsAction`: the SMS node needs the send outcome for `continueOnFailure`, so it calls `SmsPort` directly (Decision).

### Handlers (`packages/va_engine/src/va_engine/handlers/`)
- `conditions.py` — `evaluate(operator, actual: JsonValue, expected: JsonValue) -> bool` (pure). `==`/`!=`: numeric
  compare when both sides coerce to numbers (bool not numeric), else `stringify` compare case-insensitive after strip;
  `>,>=,<,<=` numeric only (non-numeric → False); `contains`/`not_contains`: list → any element `==`, str →
  case-insensitive substring, dict → key; None → contains False; `is_empty`: None, "", whitespace, [], {} ;
  `is_not_empty` = not. Missing variable = None.
- `webhook.py` `WebhookHandler`:
  - `on_enter`: (1) if `node.data.extract_vars` and `ctx.extractor` → `await extract(ctx)`, merge `values` into
    `state.variables`, add `ExtractionReport` — **before** the request (ordering test). (2) voice/widget_voice with
    non-blank `webhook.speech` → `ctx.sink.queue(Say(render(speech), mark, interruptible))` before awaiting HTTP (text
    channels drop speech; no sink → speech goes first in result actions, logged `engine.webhook_speech_late`).
    (3) render: `render_url(url)`, headers values `render`, body `render_json` (type-preserving); empty/blank url →
    problem `webhook_url_missing`, report kind `invalid_url`, no request. (4) `await ctx.http.execute(HttpCall(...,
    extract=tuple((r.name, r.data) for r in response_data)))`. (5) merge non-null `extracted` into variables.
    (6) route: first `responsePathways` item whose `evaluate(...)` holds and whose target exists → `route_to`; else
    `routeOnStatus[status_class]` (existing target); a target id not in the graph is skipped
    (`engine.webhook_route_missing`). (7) `WebhookReport` data: `{node_id, method, url` (scheme+host+path, query
    dropped), `status_code, outcome, attempts, duration_ms, variables_set: [names], route_to}` — no bodies/headers.
  - `respond`: `speak_node(ctx, prompt_required=True)` when text/prompt present, else empty (immediate route).
  - `waits_for_user`: (has text or prompt) and not skipUserResponse.
- `sms.py` `SmsHandler`:
  - `on_enter`: body = `messageMode: "prompt"` + non-blank `messagePrompt` → `ctx.lines.compose` (fallback to rendered
    `message`, `engine.sms_message_fallback`); blank body → problem `sms_message_missing` (+ fallback routing unless
    continueOnFailure). Channel `sms`: `Say(body)` only (the message is the next message, no port). Other channels:
    `to` rendered (default `{{from}}`) normalized to E.164 (reuse transfer_call normalization helper; invalid →
    failure `invalid_to`, number never logged), `from_` rendered or None, `await ctx.sms.send(...)`. Success → `Say(
    render(speech))` if speech set, `SmsReport(node_id, sent=True, message_id)`. Failure → report `sent=False,
    error`, problem `sms_failed`; `continueOnFailure=False` → `route_fallback=True`; no speech on failure (it would
    claim a text was sent).
  - `respond`/`waits_for_user`: as Webhook.
- `press_button.py` `PressButtonHandler`: voice/widget_voice → optional `Say(speech)` then
  `SendDtmfAction(digits, durationMillis, after_mark=speech mark|None)`; text channels → `engine.press_button_ignored`
  (warning), no actions. `respond` empty; `waits_for_user` False.
- `transfer_pathway.py` `TransferPathwayHandler`: `on_enter` → `SwitchGraph(...)` (blank pathwayId → problem
  `transfer_pathway_missing`, no action); `respond` empty; `waits_for_user` False (delegated to the target node).
- `scheduling.py` `SchedulingHandler`:
  - `on_enter`: speech queued like Webhook (voice); GET rendered `availabilityUrl` (headers rendered, timeout 10 s,
    retries 1) via HttpPort; parse `{"slots":[{start,end}]}` (ISO8601, tz-aware; naive → node timezone rendered, else
    ctx.timezone); keep slots with `now <= start < now + lookaheadDays`, sorted, max 20; store in
    `state.variables["available_slots"]` as `[{start,end}]` ISO strings. Failure / invalid body / zero slots →
    `route_to = onFailureNodeId` (existing) else problem `scheduling_unavailable`; WebhookReport for the GET.
  - `respond`: `speak_node(ctx, prompt_required=False)` (dialogue port offers the functions in T08).
  - `SCHEDULING_FUNCTIONS`: OpenAI function defs `list_slots` (no params) and `book_slot` (`start` string required)
    for T08's dialogue port; `async book_slot(ctx, start: str) -> BookingOutcome(ok, tool_message: dict, route_to)`:
    start must equal an offered slot start (compare as instants) else `{"error":"slot_unavailable"}`; POST
    `bookingUrl` body `{start, end, name, phone, email, notes, call_id}` (name = `name` var or `first_name`+`last_name`,
    phone = builtin `user_number`, email/notes vars or null, call_id builtin); 2xx with `booking_id` → variables
    `booking_id`, `booking_start`, `booking_end`; failure → route_to onFailureNodeId. `list_slots(ctx)` → tool message.
  - `waits_for_user` True.
- `registry.py`: map Webhook, SMS, Press Button, Transfer Pathway, Scheduling; only Knowledge Base / Custom Code stay
  `UnsupportedNodeError`.

### Navigator (`navigation.py`)
- `TransitionKind` += `"switch"`; `TransitionProblem` += nothing new (reuse `pathway_unavailable`,
  `forward_target_missing`).
- `async switch_graph(action: SwitchGraph) -> Transition`: load via loader (none/failure → stay + problem); target
  node (`node_id` or start) must exist; `return_on_end` → push `NodeFrame(kind="transfer_pathway", pathway_id,
  version, node_id=current)` of the source; `carry_variables=False` → `state.variables = {}` (request_data and
  built-ins remain in the template scope); switch active/state pathway fields; `enter(target)` (visit counted),
  `pathway_changed=True`.
- `pathway_return_pending() -> bool` (top frame kind transfer_pathway) and `async return_from_pathway() -> Transition`:
  pop, reload frame pathway/version, current = frame node (the Transfer Pathway node, kind "return", no visit, no
  on_enter: T08 then routes from its edges); load failure / missing node → problem `return_target_missing`. Rule:
  T08 calls it instead of executing an End Call node while a transfer_pathway frame is on top.

### New package `packages/va_tools` (dist `va-tools`; deps va-common, va-engine, httpx, jsonpath-ng)
Register per AGENTS (root dev group + `[tool.uv.sources]`, mypy `mypy_path`/`files`, src layout, `py.typed`,
uv_build). jsonpath-ng has no types → commented `[[tool.mypy.overrides]] module = ["jsonpath_ng.*"]
ignore_missing_imports`. `uv add` it to va_tools; run pip-audit.
- `jsonpath.py`: `compile_path(path) -> JsonPath` (`jsonpath_ng.ext.parse`, `functools.lru_cache(512)`, raises
  `InvalidJsonPathError`), `extract(document, path) -> JsonValue` (one match → value, several → list, none → None;
  invalid path → None + `tools.jsonpath_invalid` log), `extract_all(document, pairs) -> dict`.
- `http.py`: `HttpExecutor(client: httpx.AsyncClient, *, clock: Clock, call_id: str | None = None, signer:
  Callable[[bytes], Mapping[str,str]] | None = None, max_bytes=1_048_576)` implementing `HttpPort`; factory
  `open_http_executor(settings, *, clock, resolver=system_resolver, call_id=, signer=)` → async context manager over
  `va_common.safe_http.safe_client(settings, timeout=..., allow_private=settings.webhook_allow_private, resolver=)`
  (connect 2 s; per-request total timeout `timeout_ms`). Pre-flight `check_destination_url` for scheme (unsafe →
  `blocked`; unparseable → `invalid_url`); connect-time `UnsafeDestinationError` → `blocked` (no retry). Headers:
  rendered + `User-Agent: VoiceAgents-Tools/1.0`, `X-VA-Call-Id`, signer headers over the exact body bytes (JSON body
  via `va_common.json.dumps`, `Content-Type: application/json`; GET/DELETE with body None send none). Retries on
  connect error, timeout, 5xx: up to `retries`, sleep `0.25 × attempt` s on the injected clock; never on 4xx/blocked.
  Body read streamed with the 1 MB cap (`truncated`); parse JSON when content-type JSON or it parses, else text.
  Apply `extract` with jsonpath (only on JSON bodies; text → all None). Log `tools.http_request` (method, host,
  status, attempts, ms; never URL query/headers/body).
- Tests (`packages/va_tools/tests/`): `test_jsonpath.py` (single/multi/no match, filters `$.items[?(@.x>1)].y`, invalid
  path, cache), `test_http.py` with respx (`respx` patches httpcore → use a plain `httpx.AsyncClient` with respx for
  rendering/retry/truncation/parse: retries on 5xx & timeout with recorded backoff sleeps 0.25/0.5, no retry on 4xx,
  headers incl. UA/call id/signature verified with `va_events.webhook_signing`, 1 MB cap → truncated, text body,
  extraction), `test_http_ssrf.py` with the real-socket receiver (`packages/va_events/tests/support/webhook_receiver.py`
  — add that dir to va_tools pytest/mypy paths or copy nothing: import via pythonpath) through `open_http_executor`:
  fake resolver → 127.0.0.1 with `webhook_allow_private=True` succeeds; `False` → `blocked`; `http://10.0.0.1`
  literal blocked; DNS answer containing a private address blocked (rebinding pin). Use `ManualClock`/`RecordingClock`.
  Coverage ≥ 95 %.

### Spec
- specs/11 §4: append `> Decision (2026-11-03): webhook, SMS, press button, transfer pathway and scheduling handlers
  (P07-T07).` with the rules above (ports, HandlerResult route_to/route_fallback/reports, operator semantics, status
  class mapping incl. connection/blocked → `timeout` key, extraction-before-request, speech via ActionSink, SMS no
  speech on failure + no SendSmsAction, SwitchGraph + returnOnEnd return-instead-of-End-Call, carryVariables=false
  clears variables, scheduling slot filtering/storage `available_slots`/functions/booking body field sources,
  webhook event data fields).
- specs/13 §5/§9: Decision that `va_tools.http.HttpExecutor` is the shared executor (steps 3–6 subset; executor gets
  rendered requests; dependency direction va_tools → va_engine; JSONPath via jsonpath-ng ext).
- specs/02 package list if it enumerates packages (check; add va_tools row only if absent).

### Tests in va_engine (`packages/va_engine/tests/`)
- `support/handler_helpers.py`: extend `handler_ctx(..., http=, sms=, extractor=, sink=)`; add `FakeHttp(outcomes |
  callable)` recording `calls` (+ shared `order` log), `FakeSms(outcome)`, `FakeExtractor(values)` writing into the
  same `order` log, `RecordingSink`.
- `test_conditions.py`: every operator × types table (parametrized, incl. missing, bool vs number, list contains).
- `test_handler_webhook.py`: extraction runs before the request and its values are visible in the rendered body;
  speech queued to the sink before execute (order log); text channels drop speech; url/headers/body rendering with
  type preservation; extracted vars merged (null not merged); responsePathways first match wins / in order / missing
  target skipped; routeOnStatus 2xx/4xx/5xx/timeout and connection→timeout; no match → normal routing (route_to None);
  report data without query string; missing http port / blank url problems; respond/waits_for_user with and without
  prompt/text; logs never contain the URL query (json_log_stream).
- `test_handler_sms.py`: static/prompt message (+ fallback), to default `{{from}}` normalized, invalid to → failure,
  success speech, failure with/without continueOnFailure (route_fallback), sms channel → Say only + no port call,
  missing port problem.
- `test_handler_press_button.py`: voice speech + DTMF after mark, no speech → after_mark None, text channels ignored
  (warning logged), widget_voice sends.
- `test_handler_transfer_pathway.py` + navigator tests in `test_navigation.py`: SwitchGraph emitted; navigator swaps
  graph (state pathway id/version, current = target node or start), **variables preserved** with carry, cleared
  without; return_on_end pushes a transfer_pathway frame, `pathway_return_pending`, `return_from_pathway` restores
  source graph + node; loader failure / missing node problems; frame not popped by global auto-return (existing rule).
- `test_handler_scheduling.py`: availability GET rendered, slots filtered/sorted/capped and stored, failure →
  onFailureNodeId, book_slot success sets booking vars and POST body fields, unknown slot rejected without HTTP,
  booking failure route, list_slots message, function schemas valid (jsonschema).
- `test_handler_registry.py`: update mapping table (KB/Custom Code still unsupported).
- One integration test wiring a real `va_tools.HttpExecutor` (respx) into the Webhook handler
  (`packages/va_tools/tests/test_webhook_node.py`) — proves port compatibility + JSONPath extraction end to end.
- All new handler modules ≥ 90 % branch (aim 100 %).

### Gates
`PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
packages/va_tools -q`; `uv run python scripts/check_spec_refs.py`; `make test` in background (truncate
logs/make_test.log, Monitor heartbeat); radon avg < 10; `npx --yes jscpd@4 --silent` 0 clones; `uvx pip-audit` on
`uv export` (new jsonpath-ng/ply). No e2e (handlers not wired into calls until T12).
AGENTS.md: one dated bullet (va_tools package + HttpExecutor/jsonpath, va_engine.ports, HandlerResult new fields,
SwitchGraph/return_from_pathway, fakes).

### Risks
- respx + `safe_client`: the guarded backend may bypass respx; keep SSRF tests on real sockets, respx tests on plain clients.
- jsonpath-ng `ext` parser is slow to build: lru_cache compiled paths; parse is pure CPU, small inputs — fine inline.
- Adding fields to `TurnContext`/`HandlerResult` must keep defaults (T06 tests unchanged).
- `Action` union growth: check any exhaustive matches (grep `is_terminal`, `match action`).
- va_tools must not be imported by va_engine (add an import test like `test_model_imports` style or rely on review).
- Never log phone numbers, URLs with query strings, bodies or headers.

## Build P07-T07 (done when make test passes)
- va_engine: `ports.py`, handlers `webhook`/`sms`/`press_button`/`transfer_pathway`/`scheduling`/`conditions`/`reports`,
  `TurnContext` ports + `clock_now`, `HandlerResult.route_to/route_fallback/reports`, actions `SendDtmfAction`,
  `SwitchGraph`, `Navigator.switch_graph/pathway_return_pending/return_from_pathway` (`_load`/`_activate` shared with
  pathway forwarding). Registry now maps all but KB/Custom Code.
- New `packages/va_tools` (`http.HttpExecutor`, `open_http_executor`, `jsonpath`); jsonpath-ng 1.15 ships py.typed
  (no mypy override). Deviations from plan: responsePathways + value merge only on 2xx; DNS failure (`resolution`)
  → `connection` (retried) not `blocked`; jsonpath evaluation catches any exception (re.error seen); reports carry
  `purpose`; SMS report only when sent through the port (sms channel = Say only).
- Spec Decisions: specs/11 §4 P07-T07, specs/13 §9 P07-T07. AGENTS.md bullet added.
- Coverage of new modules 100 %; ruff/mypy/placeholders/jscpd (0 clones)/radon (avg 3.04)/pip-audit clean.

## Review P07-T07 (approved)
Re-ran lint, typecheck, placeholders, spec refs, jscpd (0 clones), make test (12028 passed, 7 skipped, 99.08 %).
Read webhook/sms/scheduling/press_button/transfer_pathway handlers, conditions, reports, ports, navigator switch/return,
va_tools http/jsonpath against specs/11 §3.2.1-§3.2.7 + Decisions. No findings.

## Plan P07-T08 split (planner, 2026-11-03)
P07-T08 was too large for one Builder pass (prompt assembly + gateway port + speculation + engine loop + events +
logs). Split in plan/phase-07.md into T08a (prompt + dialogue port + speculation coordinator, pure/unit-testable),
T08b (`PathwayEngine` orchestration, chaining, loop protection, events, background extraction, scheduling tool
rounds, transfer-pathway return), T08c (`va_engine.logs` + `call.route` summary data with `turn_index`/`tag_color`
+ Call Object `decision` projection to the §12 keys + syrupy snapshot). Design notes carried to T08b/c:
- Engine is transport-agnostic: `PathwayEngine` methods are async generators of §7 actions; the consumer resumes the
  generator only after the yielded speech played (same contract as TaskBrain), so chaining = continue after yield.
  T12 adapts it to `va_voice.pipeline.Brain`. Events go to an `EngineEvents.emit(event_type, data)` port.
- `call.route` once per assistant reply (T08c) carrying §12 decision keys + flat §5.5 keys + `turn_index`/`tag_color`;
  the Call Object projects `decision` to `route, global, loop_condition, extracted, tools` (va_db change in T08c).
- `prevNodePrompt` reaches templates through `TurnContext.builtins` (T08b builds the ctx).

## Plan P07-T08a — Pathway dialogue prompt assembly + speculative generation coordinator
Spec refs: specs/11 §5.7, §5.8, §13; specs/10 §2 (DialogueStream commit/discard), §4, §5, §6, §8.

### Spec decision — append `> Decision (2026-11-03): pathway dialogue prompt and speculation (P07-T08a).` after §5.8
Rules (implement exactly):
1. The pathway reply uses the specs/10 §4/§5 assembly (same as task mode): system = `render_voice_system` with
   `instructions` = §5.7 [1] + [3] + [4] joined by one blank line (supersedes §5.7's list; [2] persona block arrives
   phase 13, [6] knowledge phase 08, [9] runtime facts are already in the Voice System Prompt, [10] none):
   [1] rendered `globalPrompt` when non-blank and (`applyGlobalPromptToAllNodes` or the node has no non-blank
   prompt); [3] `## Current step: {node name}` + newline + rendered node prompt (else rendered `text`; header alone
   when neither); [4] loop-condition suffix when given. Known information = `request_data` then `variables`
   (variables win) via `voice_prompt_inputs`-like mapping (keys `_`-prefixed and `call_id`/`voice_id` excluded by
   render_voice_system already). Memory context passes through. Text channels use the same prompt in phase 07
   (phase 14 adds the text-mode system prompt).
2. Examples ([7]): node `dialogueExamples` → for each example the last `user` message of its `conversation` paired
   with `assistantResponse` (examples without a user message or with a blank response are skipped) →
   `va_llm.prompts.example_messages`.
3. Window: `EngineState.history` → ChatMessages: `user` → user, `assistant` → assistant (content as stored, so a
   barge-in truncated reply already ends with `—`), `tool` → `system` `[Context update] Tool {tool_name} returned:
   {content}` via `context_update` (tool_call/arguments are not persisted), `system` → `context_update(content)`;
   `history_summary` non-blank → one leading `context_update("Earlier conversation: …")`. Then
   `ConversationWindow(budget, counter).fit(messages)`; budget = settings `context_budget_tokens` (default 6 000).
   Extra in-turn messages (tool rounds of T08b) are appended after the history by the caller (`extra=`).
4. Model/temperature precedence (§13): model = node `modelOptions.model` → `global_config.dialogueModel` →
   `settings.dialogue_model` (call) → None (provider default); temperature = node `modelOptions.temperature` →
   `global_config.defaultTemperature` when the graph set it explicitly (`"default_temperature" in
   global_config.model_fields_set`) → call `settings.temperature` when set → 0.7. `max_tokens` = settings (250).
5. Speculation eligibility (`speculation_allowed`): org `engine.speculative` true, node handler streams a dialogue
   reply at this node (Default/Global, Wait for Response with non-blank prompt, Webhook/SMS with a non-blank
   prompt, Scheduling), the node has no non-blank static `text`, and no `before_response` extract var is referenced
   by the node prompt or the (applied) global prompt (`referenced_variables`: first path segment of every template
   expression; unparseable template → treat as referencing everything → not allowed). Background-mode extract vars
   never block speculation.
6. `Speculation.resolve(stays=, variables=, prompt_changed=)`: commit when `stays`, not `prompt_changed`, and
   none of the referenced variables changed value (snapshot taken at start vs. variables after extraction); else
   `await discard()`. Discard reasons: `route_changed`, `variables_changed`, `prompt_changed` (a loop condition not
   met stays on the node but adds the [4] suffix the speculative request lacked). Logged `engine.speculation`
   (node_id, outcome `committed|discarded`, reason). Metrics stay the gateway's `va_llm_speculation_total`.
7. `GatewayDialogue.stream(ctx)` opens a non-speculative stream on first iteration; a pre-token
   `LlmUnavailableError` ends the iterator empty and records `unavailable` (the engine speaks `errorPhrase`, T08b);
   the opened `DialogueStream` is exposed (`last`) so T08b reads `tool_calls()`; closing the iterator closes the
   stream (`aclose`).

### Files
- `packages/va_engine/src/va_engine/prompt.py` (new):
  - `@dataclass(frozen=True, slots=True, kw_only=True) class DialogueSettings`: `dialogue_model: str | None = None`,
    `temperature: float | None = None`, `max_tokens: int = 250`, `context_budget_tokens: int = 6_000`,
    `speculative: bool = True`, `org_id: uuid.UUID`, `call_id: uuid.UUID | None = None`, `direction:
    Literal["inbound","outbound","web"]`, `counterpart: str = ""`, `language_name: str = "English"`,
    `memory_context: str | None = None`. Classmethod `for_call(context: CallContext, org: OrgSettings, *,
    memory_context=None)` mapping CallContext/OrgSettings (`org.engine.speculative`, `org.llm.context_budget_tokens`
    — verify field path in va_domain.org) — mirrors `voice_inputs.voice_prompt_inputs`.
  - `node_instructions(ctx: TurnContext, *, condition_suffix: str | None = None) -> str` (rule 1).
  - `node_examples(node) -> tuple[ChatMessage, ...]` (rule 2).
  - `history_messages(state: EngineState) -> list[ChatMessage]` (rule 3).
  - `dialogue_model(ctx, settings) -> str | None`, `dialogue_temperature(ctx, settings) -> float` (rule 4).
  - `class PathwayPrompt(settings, *, counter: TokenCounter, clock: Clock)` with `request(ctx, *, condition_suffix=
    None, tools: tuple[ToolDef, ...] = (), extra: Sequence[ChatMessage] = (), speculative=False) -> DialogueRequest`
    (system via `voice_prompt_inputs`-equivalent `VoicePromptInputs(now_local=clock.now().astimezone(zone_named(ctx.
    timezone)), ...)`, `fit_voice_system(inputs, counter=, extra_tokens=examples+tools tokens)`, `tool_usage_note()`
    when tools, `assemble_messages(system, tool_note=, examples=, window=fit(history + extra))`, `turn_index=
    state.turn_index`).
- `packages/va_engine/src/va_engine/dialogue.py` (new): `DialogueSource` protocol (re-use `task_runner.
  DialogueSource` — move it here and re-export from task_runner to avoid duplication); `class GatewayDialogue`
  implements `handlers.DialoguePort`: `__init__(source, prompt: PathwayPrompt)`, `condition_suffix: str | None`
  attribute (set by T08b per turn), `tools: tuple[ToolDef, ...]`, `extra: list[ChatMessage]`, `async open(ctx, *,
  speculative: bool) -> DialogueStream`, `stream(ctx) -> AsyncIterator[str]` (rule 7), properties `last:
  DialogueStream | None`, `unavailable: LlmUnavailableError | None`, `text: str` (deltas of the last stream).
- `packages/va_engine/src/va_engine/speculative.py` (new): `referenced_variables(template_texts: Iterable[str]) ->
  frozenset[str] | None` (None = unparseable), `speculation_allowed(node, graph, *, enabled) -> bool` (rule 5),
  `class Speculation` (`start(dialogue: GatewayDialogue, ctx)` → opens `speculative=True` stream as a task-free
  call (stream_dialogue returns before tokens), snapshot of referenced vars; `async resolve(*, stays: bool,
  variables: Mapping, prompt_changed: bool = False) -> DialogueStream | None`; `outcome` property; `aclose()`
  discards when unresolved), `class CommittedDialogue(base: GatewayDialogue, stream: DialogueStream)` — DialoguePort
  whose first `stream(ctx)` yields the committed stream's text (recording text/unavailable like GatewayDialogue,
  sets `base.last`), later calls delegate to `base.stream`.
- `va_engine/__init__.py` exports only if the package already re-exports modules (check; keep import-light).
- specs/11 Decision block (above). AGENTS.md dated bullet.

### Tests (packages/va_engine/tests/)
- `support/dialogue_helpers.py`: `FakeStream` (DialogueStream double: scripted deltas, `tool_calls` list,
  `fail_before_token` raising `LlmUnavailableError` from `text()`, records `committed`, `discarded`, `closed`,
  `iterated`; speculative streams yield nothing until `commit()`), `FakeSource` (records `requests`, returns
  scripted `FakeStream`s in order), `settings(**over)`, `prompt_rig(clock=ManualClock/FrozenClock)` using
  `va_llm.tokens.default_counter()` (load once at module scope via fixture).
- `test_prompt.py`: instructions with/without global prompt (apply flag true/false × node prompt present/absent),
  templating of global + node prompt (variables, request_data, built-ins), `text`-only node uses text, header-only
  node, loop suffix last; examples mapping & skips; history mapping (interrupted assistant kept, tool → context
  update, system → context update, summary first); window trimming keeps last turn (small budget); model and
  temperature precedence table (parametrized); request fields (org_id, call_id, turn_index, speculative, tools +
  tool note); known information excludes `_secret`; a full request snapshot for the restaurant fixture's start node
  compared to an inline expected message list (deterministic: frozen clock, fixed timezone).
- `test_dialogue.py`: stream yields deltas and records text/last; pre-token unavailable → empty + `unavailable`;
  closing the iterator early closes the stream; `open(speculative=True)` sets the request flag; one FakeLLM contract
  test via `task_helpers.served()` (real `LlmGateway`): stream text equals the FakeLLM rule reply and the request
  body carries the system prompt with `## Current step:`.
- `test_speculative.py`: `referenced_variables` (paths, filters, unparseable → None); eligibility table (static
  text node, Wait without prompt, Webhook with/without prompt, before_response var referenced in node prompt /
  global prompt / not referenced, background var referenced, disabled setting, End Call/Transfer/Press Button →
  False); resolve stays + unchanged → committed, consumer of `CommittedDialogue.stream` gets the buffered tokens;
  route change → discarded and no token ever yielded; variable referenced changed → discarded; unreferenced change →
  committed; prompt_changed → discarded; `aclose()` unresolved → discarded; resolve twice → second is a no-op;
  second `stream()` call on CommittedDialogue opens a fresh request; log event `engine.speculation`.
  FakeLLM contract: speculative stream over served gateway commit → tokens arrive; discard → `text()` ends empty.
- Coverage of new modules 100 % (branch).

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
-q`; `uv run python scripts/check_spec_refs.py`; `make test` in background (truncate logs/make_test.log; Monitor
heartbeat); `uvx radon cc -s -a packages/va_engine/src` avg < 10; `npx --yes jscpd@4 --silent` 0 clones; audit
unchanged deps (`uvx pip-audit` on `uv export`, `corepack pnpm audit`). No e2e (not wired into calls until T12).

### Risks
- Rule 4 temperature: `GlobalConfig.default_temperature` always has 0.7; use `model_fields_set` to know if the graph
  set it — check CamelModel keeps `model_fields_set` after `load_graph` (exclude_unset dumps rely on it, so yes).
- Moving `DialogueSource` must keep `va_engine.task_runner.DialogueSource` importable (tests import it).
- Speculative `DialogueStream.text()` may be iterated once; `CommittedDialogue` must iterate it only once.
- Do not import va_tools from va_engine; keep va_engine's modules free of va_db.
- jscpd: `prompt.py` request building resembles `task_runner._request`; factor a shared helper if a clone appears.

## Build P07-T08a (done when make test passes)
- New `va_engine.prompt` (DialogueSettings.for_call, PathwayPrompt.system/request, node_instructions,
  instruction_templates, global_prompt_applies, node_examples, history_messages, dialogue_model/temperature),
  `va_engine.dialogue` (DialogueSource moved here, task_runner re-exports; GatewayDialogue with adopt/aclose/last/
  unavailable/text), `va_engine.speculative` (referenced_variables, speculation_allowed, Speculation.start/resolve/
  aclose). `voice_inputs.counterpart_of` made public.
- Deviations from plan: no separate `CommittedDialogue` class — `Speculation.resolve` commits and calls
  `GatewayDialogue.adopt(stream)`, the next `stream()` relays it. History summary sits before the window (never
  trimmed). `GatewayDialogue.tool_speech` knob added for the tool note.
- Spec Decision specs/11 §5.8 P07-T08a (replaces speculation_hit/miss with va_llm_speculation_total, 40-turn
  window with ConversationWindow). AGENTS.md bullet added.
- Tests: test_prompt.py, test_dialogue.py, test_speculative.py (FakeLLM contract tests for stream + commit/discard);
  new modules 100 % / 100 % / 99-100 % branch.
- make test: 12089 passed, 7 skipped, coverage 99.09 %; lint/typecheck/placeholders/spec refs pass; jscpd 0 clones; radon avg 2.84; pip-audit clean.

## Review P07-T08a (approved)
Re-ran lint, typecheck, placeholders, spec refs (pass), jscpd 0 clones, radon avg 2.84, make test (12089 passed,
7 skipped, 99.09 %). Read prompt/dialogue/speculative against specs/11 §5.7/§5.8/§13 + Decision P07-T08a and
va_llm DialogueStream commit/discard/aclose semantics (metrics counted by the gateway). No findings.

## Plan P07-T08b — `PathwayEngine` turn orchestration, chaining, loop protection, events
Spec refs: specs/11 §4 (+ Decisions P07-T05/T06/T07), §5.1–§5.6, §5.8 (+ Decision P07-T08a), §5.9, §7; specs/19 §2.
Depends on T08a (prompt/dialogue/speculative). No DB, no va_tools import, no va_db import in va_engine.

### Spec decision — append `> Decision (2026-11-03): pathway turn orchestration (P07-T08b).` under §5.9
1. Contract: `va_engine.engine.PathwayEngine` methods `start(greeting=None)`, `handle_user_turn(turn)`,
   `on_wait_timeout()` are async generators of §7 actions (`Say`, `SayStream`, `Wait`, `SendDtmfAction`,
   `TransferAction`, `EndCallAction`, `HumanTakeover`; `SwitchGraph` is applied internally, never yielded). The
   consumer resumes the generator only after the yielded speech finished playing (text: was delivered), so
   chaining and terminal actions need no mark wait inside the engine (`after_mark` stays informational). A
   barge-in = the consumer closes the generator (`aclose`) and calls `on_interrupted(spoken_text)`. `UserTurn` is
   `va_voice.pipeline.models.UserTurn` (DTMF turns become `[Caller pressed: …]` via `va_llm.prompts.dtmf_message`
   content). `aclose()` cancels background work, closes an open speculation and the dialogue port.
2. Turn pipeline (one decision pass): with the current node N, run concurrently (TaskGroup) — §5.3
   `before_response` extraction, §5.4 loop condition, §5.5 router (`include_globals=navigator.
   global_check_enabled()`, the §5.2 combined call) — and start the §5.8 speculation at N when
   `speculation_allowed(N, graph, enabled=settings.speculative)`. Running them in parallel keeps them off each
   other's critical path; the router therefore sees the variables from before this turn's extraction. Apply in
   order: merge non-null extracted values (overwrite); a `global` router outcome wins over everything (§5.2
   precedence); else a condition that is not met → stay with the [4] suffix (router result discarded); else apply
   the router decision through `Navigator.apply`. Resolve the speculation with `stays` = (transition kind stay),
   `prompt_changed` = suffix added, variables after the merge. Events emitted in this order: `call.extraction`
   (only when values/nulls/rejected exist or degraded — use `ExtractionResult.event_data()` + `node_id`),
   `call.loop_condition` (when a condition was scored), `call.route` (when the decision was applied and either moved
   or the router LLM was called; `RouteDecision.event_data()`). `background` extraction runs as a supervised task
   (engine task set; one per node pass), merges + emits `call.extraction` when it completes, is cancelled by
   `aclose`; it never blocks speculation or the reply.
3. Execute (§5.6), loop per pass: entering a node → loop protection check (rule 5); End Call node while
   `navigator.pathway_return_pending()` → `return_from_pathway()` and continue with a decision pass from the
   restored node (chain step, empty latest); else `on_enter` (Wait for Response also via on_enter) → yield its
   actions; `SwitchGraph` → `navigator.switch_graph` and the new node counts as entered (the Transfer Pathway node
   itself never responds); a handler `route_to` / `route_fallback` is applied right after the handler call that
   produced it (`Navigator.enter(route_to)`; fallback = the node's fallback edge target, none → stay), emitted as
   `call.route` (label = the edge from N to the target if one exists, else null; `router_ms` 0, `alternatives`
   []) and the node's `respond` is skipped; terminal actions end the pass. Otherwise `respond` → yield. After the
   node executed: `navigator.finish_node()` (forward = entered, executes the new node; return = no re-speak).
   Handler reports are emitted as their `event_type` with `event_data()` in order as soon as the handler returns.
   A handler problem is logged `engine.handler_problem` (node_id, problem) and never raises.
4. Chaining: after a pass whose final node does not wait for the user (`handler.waits_for_user(node)` false or
   `skipUserResponse`) and no terminal action, run another decision pass with an empty latest message (no user
   record). At most 10 chained passes per user turn (`CHAIN_DEPTH_LIMIT`); the 11th → loop protection end.
5. Loop protection: entering a node whose visit count exceeds `maxVisits` → the node's fallback edge target when
   it has one (entered without speaking at the over-limit node; a second over-limit in the same pass ends), else
   `Say(errorPhrase)` + `EndCallAction(reason="loop_protection", ended_by="system")`, `state.ended`/`end_reason`.
   `global_config.maxTotalTurns`: a user turn whose `turn_index` exceeds it ends the same way before any decision.
   Logged `engine.loop_protection` (node_id, cause `max_visits|chain_depth|max_total_turns`).
6. Speech + history: every yielded `Say` appends an assistant `TurnRecord(node_id=N)` before the yield; a
   `SayStream` is recorded after the consumer resumed (`dialogue.text`), or with the partial text when the
   generator is closed while it was out (finally block). An LLM unavailable before the first token (`dialogue.
   unavailable`) → `Say(errorPhrase)` (nothing recorded for the empty stream). `on_interrupted(text)` replaces the
   last spoken record's content with `text` (or `—` when blank; already ends with `—`) and sets `interrupted=True`
   (§5.1). A user turn with `supersedes` removes the superseded user record and everything after it (TaskBrain
   rule). Wait for Response timer: a user turn calls `handlers.disarm(state)`.
7. Handler speech queued on the `ActionSink` (webhook/scheduling `speech`) is yielded while the handler call is
   still running: the engine runs `on_enter` as a task with an `asyncio.Queue` sink and yields queued actions as
   they arrive, then the result's actions; closing the generator cancels the task.
8. Scheduling: a Scheduling node's `respond` runs with `dialogue.tools` = `SCHEDULING_FUNCTIONS` as `ToolDef`s;
   after the stream, `dialogue.last.tool_calls()` are executed (`list_slots(ctx)`, `book_slot(ctx, start)` with
   arguments parsed from `arguments_json`; invalid JSON / missing `start` → tool message `{"error":
   "invalid_arguments"}`), the assistant tool-call message and one tool message per call are appended to
   `dialogue.extra` (and tool `TurnRecord`s with `tool_name` to history), then the node responds again; at most 3
   tool rounds per pass (the 4th request offers no tools). A booking `route_to` routes after the round (rule 3)
   and booking reports are emitted. Other nodes offer no tools in phase 07 (custom node tools: phase 08).
   `dialogue.extra`, `tools`, `condition_suffix` are reset before every `respond`.
9. Node types without a phase-07 handler (`UnsupportedNodeError`: Knowledge Base, Custom Code) degrade: Knowledge
   Base responds like Default (no knowledge block) and waits for the user; Custom Code runs nothing, speaks its
   `text`/`prompt` when present (`speak_node(prompt_required=True)`), and waits only when it spoke and has no
   `skipUserResponse`. Logged `engine.node_degraded` (node_id, type). Phase 08 replaces both.
10. `start(greeting)`: a greeting is recorded as the first user turn (turn_index 1, no decision pass); the current
    node (state's, normally the start node — visit counted by the engine's constructor helper `EngineState`
    factory `new_pathway_state(graph, pathway_id, version)` when the state is fresh) executes as entered (§5.6),
    then chains per rule 4. `on_wait_timeout()` runs `WaitForResponseHandler.timeout(ctx)` at a Wait for Response
    node (else yields nothing): its actions, then `move_to` → enter + execute.
11. Events go through `EngineEvents.emit(event_type: str, data: dict[str, JsonValue]) -> None` (sync; the media
    adapter publishes). Turn index / tag colour enrichment is P07-T08c.

### Files
- `packages/va_engine/src/va_engine/engine_events.py`: `EngineEvents` Protocol; `report_event(report) ->
  tuple[str, dict]`; `route_event_data(decision)`; `handler_route_data(from_node, to_node, label)`.
- `packages/va_engine/src/va_engine/engine_decide.py`: `TurnDecision` dataclass (`route: RouteDecision | None`,
  `condition: ConditionResult | None`, `extraction: ExtractionResult | None`, `suffix: str | None`); `Decider(router:
  Router, condition: LoopCondition, extractor: Extractor, clock: Clock)` with `async decide(graph, node, inputs,
  *, include_globals, now, timezone) -> TurnDecision` (TaskGroup over the three; rule 2) and `background(...)`.
  `DecisionInputs` from state: `org_id`, `call_id`, `latest`, `history` = records before latest (user/assistant).
- `packages/va_engine/src/va_engine/engine_tools.py`: `scheduling_tools() -> tuple[ToolDef, ...]` (from
  `SCHEDULING_FUNCTIONS`), `async run_scheduling_calls(ctx, calls) -> SchedulingRound(messages, records, route_to,
  reports)`; `MAX_SCHEDULING_ROUNDS = 3`.
- `packages/va_engine/src/va_engine/engine.py`: `EnginePorts` (frozen, kw_only: `decider: Decider`, `dialogue:
  GatewayDialogue`, `lines: LinePort`, `events: EngineEvents`, `loader: PathwayLoader | None`, `http`, `sms`),
  `ConversationFacts` (frozen, kw_only: `org_id`, `call_id`, `channel: Channel`, `request_data`, `builtins`,
  `timezone`, `transfer_list`, `speculative: bool`), `CHAIN_DEPTH_LIMIT = 10`, `LOOP_PROTECTION = "loop_protection"`,
  `new_pathway_state(active: ActiveGraph) -> EngineState` (runner pathway, start node current, visit 1),
  `class PathwayEngine(state, active, *, ports, facts, clock)` with `state`, `navigator` properties and the rule
  1 methods. `ExtractionPort` for handlers (webhook extractVars) = small adapter over `Decider`'s Extractor
  (`_ExtractionAdapter.extract(ctx)`). Keep functions ≤ radon B; split helpers rather than one huge method.
- `specs/11` Decision block above; AGENTS.md dated bullet (engine contract + test helpers).

### Tests (`packages/va_engine/tests/`)
- `support/engine_helpers.py`: `KeyedPort` (StructuredPort keyed by `function_name` → FIFO of replies/exceptions,
  records requests; needed because decisions run concurrently), `route_reply(path_id, confidence, alternatives=)`,
  `condition_reply(score)`, `extract_reply(**values)`, `RecordingEvents` (`.events` list of (type, data)),
  `engine_rig(fixture_name|graph, channel="voice", speculative=True, http=, sms=, loader=)` → `Rig(engine, port,
  source: FakeSource, events, lines: FakeLines)`, `drive(agen, interrupt_after_tokens=None) -> list[Action]`
  (consumes SayStream tokens; returns actions with streams replaced by their text tuple), `user(text, index)`.
- `test_engine.py`: restaurant fixture multi-turn: start speaks greet stream; "Hi, I'm Mike and I want to order"
  → extraction first_name merged, route greet→take_order (`call.extraction`, `call.route` order), reply streamed
  at take_order; loop condition not met → stays, request carries the suffix, router result ignored, no
  `call.route`; condition met → route to `price` (Custom Code degraded, chains) → `confirm` static text rendered
  with variables → waits; global "what are your hours?" → global wins on tie, return stack pushed, auto-return no
  re-speak; `wait` timeout path via `on_wait_timeout`; End Call → line + EndCallAction, `state.ended`.
- `test_engine_speculation.py`: hit (stay, no suffix) → only one dialogue request, speculative flag true, stream
  committed and its tokens yielded; miss on route change → discarded (FakeStream.discarded) and a fresh request
  at the new node; miss on variable change referenced by prompt; miss on unmet condition (prompt_changed);
  speculation disabled → no speculative request.
- `test_engine_protection.py`: maxVisits exceeded → fallback edge taken / else errorPhrase + EndCallAction
  loop_protection; chain depth: a cycle of skipUserResponse nodes ends after 10 chained passes; maxTotalTurns.
- `test_engine_interrupt.py`: closing the generator mid-SayStream keeps the partial then `on_interrupted("Hel—")`
  replaces content + interrupted=True; blank → `—`; supersedes removes records; unavailable LLM → errorPhrase.
- `test_engine_nodes.py`: webhook node with speech: queued Say yielded before the HTTP finishes (FakeHttp gated by
  an Event), responsePathways route_to skips respond, `call.webhook_node` event; SMS failure → fallback;
  Transfer Pathway switch + End Call return (`MemoryLoader.with_other()`); Scheduling tool round book_slot →
  booking variables, tool records, second stream; book failure route_to onFailureNodeId; press button DTMF
  yielded; background extraction emitted after completion and cancelled by `aclose`.
- Coverage of new modules ≥ 95 % branch (target 100 %).

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
-q`; `uv run python scripts/check_spec_refs.py`; `make test` in background (truncate `logs/make_test.log` first,
Monitor heartbeat); `uvx radon cc -s -a packages/va_engine/src` avg < 10; `npx --yes jscpd@4 --silent` 0 clones;
`uvx pip-audit` on `uv export` + `corepack pnpm audit` (deps unchanged). No e2e (wired into calls in T12).

### Risks
- Concurrency + generator: never `yield` inside a TaskGroup/`async with` that would leak on aclose — gather decisions
  first, then yield. Sink relay task must be cancelled in `finally`.
- Speculation stream must always be resolved or closed (also on early generator close) → `finally: await spec.aclose()`.
- ScriptedPort is FIFO across function names → use KeyedPort for engine tests.
- jscpd: tool-round code resembles task_runner `_run_tools`; reuse `task_tools` helpers where they fit.
- Keep `PathwayEngine` small: radon complexity per method < 10; split into engine_decide/engine_tools/engine_events.

## Build P07-T08b (done when make test passes)
- New: `va_engine.engine` (PathwayEngine, EnginePorts, ConversationFacts, new_pathway_state, CHAIN_DEPTH_LIMIT,
  LOOP_PROTECTION), `engine_decide` (Decider, TurnDecision.held/suffix), `engine_events` (EngineEvents + data helpers),
  `engine_tools` (scheduling_tools, run_scheduling_calls, MAX_SCHEDULING_ROUNDS). Spec Decision specs/11 §5.9
  P07-T08b; AGENTS.md bullet.
- Deviations from plan: (a) speculation flag read from `GatewayDialogue.prompt.settings.speculative` (single source,
  DialogueSettings.for_call already maps org engine.speculative) — not in ConversationFacts; (b) scheduling tool
  answers are recorded only as `tool` history turns (shown as context updates by PathwayPrompt), not also as
  `dialogue.extra` messages — both would duplicate them in the next request; (c) streamed replies are wrapped to
  record exactly what the consumer received (GatewayDialogue.text is stale when a stream was never read);
  (d) public `settle_background()` added; (e) loop condition event data adds node_id + message.
- Tests: test_engine.py (restaurant flow), test_engine_turns.py (speculation, protection, interruptions),
  test_engine_nodes.py (webhook relay/route, SMS fallback, pathway switch+return, scheduling rounds, DTMF,
  degraded nodes, background extraction); helpers tests/support/engine_helpers.py. 53 tests.
- Gates: make fmt/lint/typecheck/placeholders pass; check_spec_refs 0; make test 12140 passed, 7 skipped, total
  coverage 99 % (engine.py 97 %, engine_decide/events 100 %, engine_tools 99 %); jscpd 0 clones; radon avg 2.86
  (no engine method above B); pip-audit clean; pnpm audit --audit-level high clean (3 moderate, pre-existing).
  No e2e: the engine is not wired into calls yet (P07-T12).

## Review P07-T08b (rejected, 1 finding)
Gates re-run: make lint / typecheck / placeholders pass, check_spec_refs 0. make test was started and stopped once
the finding below was confirmed. Engine flow, decision order, speculation resolution, relay sink, loop protection,
scheduling rounds and event data all match specs/11 §5 + Decision P07-T08b and specs/19 §2.
1. packages/va_engine/src/va_engine/engine.py:786-805 (`_accept` / `_drop_turn`): the superseded turn is located by
   an absolute `len(state.history)` index stored in `_turn_starts`. `EngineState.add_turn` folds the oldest records
   into `history_summary` once history passes HISTORY_CAP (200), which shifts every index left, so
   `del history[position:]` then cuts at the wrong place. Repro (checked): start, add 205 filler records, user turn 1
   "first", then turn 2 with `supersedes=1` → `("user", "first")` stays in the history (the stale index points past
   it). Rule 6 of the Decision says a superseding turn "removes the superseded user record and everything after it".
   Fix like `TaskBrain._drop_turn` (task_runner.py:478): remember the number of records added since the engine
   started (a counter bumped in `_record`) and delete the last `added_now - added_then` records (bounded by
   len(history)), and add a test that supersedes a turn after the history reached HISTORY_CAP. While there, drop
   `_turn_starts` entries for earlier turns so the dict does not grow for the whole call.

## Build P07-T08b rework (finding 1)
- engine.py: `_record` bumps `_records_added`; `_accept` keeps only the new turn's mark (`{turn.index:
  records_added}`; the pipeline supersedes only the previous turn, barge_in.py), `_drop_turn` deletes the last
  `records_added - mark` records (bounded by len(history)) and rewinds the counter — survives HISTORY_CAP folding.
- Tests (test_engine_turns.py): supersede after HISTORY_CAP+5 filler records drops "first"/"Reply one."; an older
  (non-latest) turn cannot be superseded. Both fail on the old index logic (checked by temporarily restoring it).

## Review P07-T08b rework (approved)
Finding 1 fixed: `_drop_turn` counts records added since the superseded turn's mark; tests cover HISTORY_CAP folding
and non-latest supersedes. Gates re-run: lint/typecheck/placeholders pass; make test 12142 passed, 7 skipped,
coverage 99.07 %.

## Plan P07-T08c — Deterministic `pathway_logs` (specs/11 §12, §14; specs/19 §4 Call Object)

### Facts that shape the design
- `call_turns` is UNIQUE (call_id, turn_index, role): a call has at most ONE assistant row per turn index, so
  pathway_logs granularity is per turn index (chained speech in one turn is one entry), both engine-side and in DB.
- The Call Object projection (`va_db.call_object._pathway_logs`, P05-T16a Decision in specs/19 §4) today takes the
  last category-`route` event per `turn_index` as `decision` — that mixes `call.loop_condition` (also category
  route) and does not produce the §12 `decision` shape (`route`, `global`, `loop_condition`, `extracted`, `tools`).
- Engine events today carry no `turn_index`/`tag_color` (Decision P07-T08b rule 9 deferred enrichment; T08c owns it).
- `PathwayLog` model already exists in `va_domain.call_object.models`; syrupy ≥ 6.1 is a root dev dep, unused so far.

### Spec decision — append `> Decision (2026-11-03): deterministic pathway_logs (P07-T08c).` under specs/11 §12
1. One entry per turn index that has an assistant turn with a node (task calls: none), ordered by turn index.
   `text` = the assistant text of that turn (engine: the turn's assistant records joined with one space, after
   barge-in truncation); `node_id` = the last node that spoke in the turn; `tag.name` = that node's `name`;
   `user_text` = the user text of the same turn index (null for turn 0 / no user turn).
2. Every engine event carries `turn_index` (the engine turn it belongs to; a background extraction keeps the turn
   index of the pass that started it). `call.route` also carries `tag_color` = the `to_node`'s `tag.color` (null
   when untagged / unknown).
3. `decision` is always an object built from that turn's events in emission (seq) order:
   `route` = last `call.route` → `{from, to, label, alternatives}` (null when none); `global` = that route's
   `global` (false without route); `loop_condition` = last `call.loop_condition` → `{score, met}` (null when none);
   `extracted` = merge (later wins) of the turn's `call.extraction` `variables` (null when no extraction event);
   `tools` = `call.webhook_node` + `call.tool` events in order → `{name, status, ms}` (name = `tool_name`, else
   `node_name`, else `node_id`; status = `status_code`; ms = `duration_ms`), `[]` when none.
4. `tag.color` = `tag_color` of the turn's last `call.route`; without one, carried over from the latest earlier
   turn's route (the node stayed current); null before the first route.
5. Serialization: keys in §12 order (model field order), `created_at` UTC `YYYY-MM-DDTHH:MM:SS.mmmZ`;
   `va_engine.logs.dump_pathway_logs(entries) -> bytes` = orjson of the dumped models (no sort, no indent... use
   `OPT_INDENT_2` for snapshot readability is fine as long as it is fixed) — byte-stable.
6. specs/19 §4 P05-T16a Decision: append one sentence "`decision`/`tag.color` are built per specs/11 §12
   Decision P07-T08c (supersedes the `call.route`-data rule above)." Keep same line `specs/11 §12` for spec refs.

### Files
- `packages/va_domain/src/va_domain/call_object/pathway_logs.py` (new, pure, shared by va_db and va_engine so the two
  are identical by construction): frozen dataclasses `LogTurn(turn_index, role, text, node_id, node_name,
  created_at)` and `LogEvent(turn_index, event_type, data: Mapping[str, Any])`; constants `ROUTE_EVENT`,
  `CONDITION_EVENT`, `EXTRACTION_EVENT`, `TOOL_EVENTS = ("call.webhook_node", "call.tool")`, `LOG_EVENT_TYPES`
  (all five); `pathway_decision(events: Sequence[LogEvent]) -> dict[str, Any]`;
  `build_pathway_logs(turns, events, *, pathway_id, pathway_version) -> list[PathwayLog]` (rules 1, 3, 4; events
  without an int `turn_index` are ignored; bool is not int). Export from `va_domain.call_object.__init__`.
- `packages/va_domain/src/va_domain/call_object/models.py`: `PathwayLog.created_at` serialized with milliseconds +
  `Z` (`PlainSerializer(..., return_type=str, when_used="json")` or field_serializer); keep JSON schema
  `format: date-time` (check `make openapi` drift; regenerate `openapi/openapi.json` + both TS `gen:api` if the
  schema changes). `decision` stays `dict[str, Any] | None` but description updated to the §12 shape.
- `packages/va_db/src/va_db/call_object.py`: `_details` loads `CallEvent.call_id, event_type, data` where
  `event_type IN LOG_EVENT_TYPES` ordered by `(call_id, seq)` into `found.log_events[call_id]: list[LogEvent]`
  (replaces `routes` + `_ROUTE_META_KEYS`); `_pathway_logs` maps `CallTurn` rows → `LogTurn` and calls
  `build_pathway_logs`. Rows with NULL event_type (pre-0007) are skipped.
- `packages/va_engine/src/va_engine/logs.py` (new): `PathwayLogBook` — `on_record(turn_index, record: TurnRecord)`
  (keeps the record object: `on_interrupted` mutates `content` in place, so truncation shows up),
  `on_event(turn_index, event_type, data)`, `drop_from(turn_index)` (supersede), `entries(*, pathway_id,
  pathway_version, graph_names: Callable[[str], str | None]) -> list[PathwayLog]` (maps to LogTurn/LogEvent:
  per turn index one user LogTurn (last user record) and one assistant LogTurn (assistant records joined " ",
  node = last assistant record's node_id, name from the active/any graph, created_at = first assistant record's
  time from the engine clock captured at on_record) then `build_pathway_logs`); `dump_pathway_logs(entries) ->
  bytes`. Tool records (`role="tool"`) are ignored.
- `packages/va_engine/src/va_engine/engine.py`: `PathwayEngine(..., logbook: PathwayLogBook | None = None)`
  (default: a fresh book, exposed as `engine.logbook`); `_emit(event_type, data, *, turn_index=None)` adds
  `turn_index` (default `state.turn_index`) to every event and `tag_color` to `call.route` (to_node lookup in the
  current graph, incl. after `switch_graph`); background extraction passes the captured turn index; `_record`
  calls `logbook.on_record(state.turn_index, record)` with `clock.now()`; `_drop_turn` calls `drop_from(index)`.
  Node names: the book receives `node_name` at on_record time (engine knows the node) to avoid graph lookups later
  (preferred: `on_record(turn_index, record, node_name=..., at=clock.now())`).
- `packages/va_engine/src/va_engine/engine_events.py`: `with_turn(data, turn_index)` / `route_tag_color(graph,
  node_id)` helpers if it keeps engine.py small.
- `packages/va_engine/src/va_engine/handlers/reports.py`: `WebhookReport.node_name` → `event_data()["node_name"]`
  (handlers/webhook.py passes `ctx.node.data.name`); update handler tests' expected event data.
- AGENTS.md dated bullet; specs/11 §12 Decision; specs/19 §4 one-line pointer.

### Tests
- `packages/va_domain/tests/test_pathway_logs.py`: decision shape (all keys present, nulls/[]), last route wins,
  loop_condition without route, extraction merge later-wins, tools mapping name fallbacks, tag colour carry-over,
  events without int turn_index / bool turn_index ignored, user_text pairing, turns without node skipped, created_at
  JSON `...412Z` (ms truncation, non-UTC input converted).
- `packages/va_engine/tests/test_logs.py`: (a) restaurant fixture conversation (reuse test_engine `at_take_order`
  style: start → order → loop condition unmet → met → chained confirm → global hours → end call, plus one webhook
  node event via a small graph or fixture node) → `dump_pathway_logs(...)` compared with syrupy
  `snapshot` (commit `packages/va_engine/tests/__snapshots__/test_logs.ambr`; create with
  `uv run pytest packages/va_engine/tests/test_logs.py --snapshot-update`); (b) byte-stability: run the same
  scripted conversation in two fresh rigs → identical bytes; (c) every event in the run has `turn_index`, every
  `call.route` has `tag_color`; (d) barge-in truncation shows the truncated text in the entry; (e) supersede drops
  the turn's entry and events; (f) background extraction completing after the next turn started is attributed to
  its own turn.
- Existing tests to update: `packages/va_engine/tests/test_engine*.py` expected event dicts (now carry
  `turn_index`, route `tag_color`), handler webhook tests (`node_name`), `packages/va_db/tests/test_call_object_db.py`
  and `packages/va_domain/tests/test_call_object.py` pathway_logs expectations (new decision shape, ms timestamps),
  `packages/va_events/tests/test_webhook_payloads.py` / `test_webhook_router_db.py` if they build pathway_logs.
  Add a va_db DB test: turn with route + loop_condition + extraction + webhook_node events → §12 decision; a
  pre-0007 row (event_type NULL) ignored.

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; `uv run pytest packages/va_engine
packages/va_domain -q`; `uv run pytest packages/va_db/tests/test_call_object_db.py -q`; `make openapi` +
`git diff --exit-code openapi/` (regenerate TS types if changed); `uv run python scripts/check_spec_refs.py`;
`make test` in background (truncate `logs/make_test.log` first, Monitor heartbeat); radon avg; jscpd 0 clones;
pip-audit + pnpm audit. No e2e (not wired into calls until P07-T12).

### Risks
- syrupy + xdist: unused-snapshot detection can misreport under `-n auto`; if `make test` fails with "unused
  snapshots", run the snapshot module serially to confirm and add `--snapshot-warn-unused` to pytest addopts
  (document in AGENTS.md). Snapshot must not contain uuids/timestamps that vary: FrozenClock + fixed PATHWAY_ID.
- Do not bump EngineState schema: the logbook lives beside the engine (T12 persists DB rows, T10 test chat uses the
  book directly); nothing new in `EngineState`.
- Pydantic datetime serializer must not break `CallObject` JSON schema / OpenAPI examples; check drift.
- Keep engine.py methods ≤ radon B; jscpd: va_db and va_engine both map to LogTurn — keep the mappers tiny.

## Build P07-T08c (done)
- New: `va_domain.call_object.pathway_logs` (LogTurn/LogEvent, `build_pathway_logs`, `pathway_decision`,
  `LOG_EVENT_TYPES`, `event_turn_index`), `va_engine.logs` (PathwayLogBook, `dump_pathway_logs`),
  `PathwayEngine(logbook=)` + `.logbook` / `.pathway_logs()`; `_emit` stamps `turn_index` on every event and
  `tag_color` on routes (`engine_events.route_tag_color`); background extraction keeps its starting turn index;
  supersede drops the turn from the book. `WebhookReport.node_name`. `PathwayLog.created_at` → `...mmmZ`.
  va_db Call Object projection now uses the builder (events by `event_type`, NULL rows skipped).
- Deviations: `text` joins stripped non-blank parts (FakeSource tokens end in spaces); tag.name = node name
  (the hours node's tag name is "Global" but §12 shows node names).
- Spec: specs/11 §12 Decision P07-T08c; specs/19 §4 P05-T16a pointer updated. openapi + both schema.d.ts
  regenerated (description-only). AGENTS.md bullet.
- Tests: va_domain test_pathway_logs (100 %), va_engine test_logs (2 syrupy snapshots in
  tests/__snapshots__/test_logs.ambr), updated engine/handler expectations, va_db call object DB test.
- Gates: lint/typecheck/placeholders pass; check_spec_refs 0; make test 12158 passed, 7 skipped, 99.03 %
  (pathway_decision was split for radon after the run started — re-ran its tests: 16 passed, 100 % cov);
  radon avg A 3.27 (max in new code B 8); jscpd 0 clones; pip-audit clean; pnpm audit 3 moderate (pre-existing).

## Review P07-T08c (approved)
Gates re-run: make lint/typecheck/placeholders pass; check_spec_refs 0; openapi drift current; jscpd 0 clones; radon
avg A 3.09; make test 12158 passed, 7 skipped, 99 % total (pathway_logs.py/logs.py/engine_events.py 100 %, engine.py
97 %). The §12 key order, decision shape, and ms timestamps match the spec; the engine and the DB share one builder.
Minor (not blocking): the `event_type is None` skip in va_db/call_object.py:179 can never run, because the SQL
`IN` filter already excludes NULL rows.

## Plan split P07-T09 (planner, 2026-11-03)
P07-T08 parent ticked (a–c done). P07-T09 is too large for one iteration: split into T09a (core CRUD + validate +
delete references), T09b (versions/publish/promote/revert; regression gating stays in T11), T09c (folders,
duplicate, import/export). Regression settings `GET/PUT /regression`, tests, runs, simulations → T11; chat → T10.

## Plan P07-T09a — Pathways API core (specs/12 §1–§3, §10–§12; specs/11 §11; specs/31 §1/§3a)

### Spec decision — append `> Decision (2026-11-03): pathways API core (P07-T09a).` under specs/12 §3
1. Permissions: reads need `pathways:read`; every write `pathways:write`. specs/04 §3.2 gives analysts
   `pathways:write` (for tests/chat) but §10 here denies them graph edits, so create/update/delete/validate
   (and later import/duplicate/folders/version/revert) also pass `va_api.auth.pathway_roles.require_pathway_editor`
   (session principals with role `analyst`/`viewer`/`billing` → 403 `FORBIDDEN` "Your role cannot edit pathways.";
   API keys pass). The §10 "`pathways:publish`" API-key scope is not a §2.3 scope: publish/promote/revert (T09b)
   use `pathways:write` + `require_permission("pathways.publish")` (a `*` key holds it). `?force=true` delete needs a
   session role owner/admin, or an API key with `*`; else 403 FORBIDDEN "Force delete requires an owner or admin.".
2. Create: name 1–200 chars (trimmed non-empty), description ≤ 2 000; `nodes`/`edges` both empty or absent → the
   §3 default start node, edges []. Graph parsed with `va_engine.models.load_graph` (schema errors → 400
   `INVALID_INPUT` with `field` from `schema_field(loc)`, e.g. `nodes[3].data.name`), then validated in **save**
   mode: each error-severity issue becomes one `errors[]` item `{error: INVALID_INPUT, message: <issue message>,
   details: {code, node_id?, edge_id?}}` (all items, HTTP 400). Stored JSON = `dump_graph(graph)`;
   `graph_hash(dumped)`. Pathway row `draft_version=latest_version=1`; version row v1 `status='draft'`.
   `folder_id` must name an org folder (else 404 `NOT_FOUND` field `folder_id`).
3. Validation refs for an org: `GraphRefs(pathway_id=str(id) | None, kb_ids={to_public("KB", id)} ∪ {str(id)} of
   non-deleted KBs, tool_ids={to_public("TL", id)} ∪ {str(id)} of tools, pathway_ids=str(id) of non-deleted
   pathways)`; `allow_http = VA_ENV in {local, test}`; no `code_checker` (phase 08).
4. List: `limit` 1–100 default 50, `cursor` per specs/31 §2 Decision (created_at DESC, id DESC),
   `folder_id` filter, `search` = name ILIKE `%term%` with `%`/`_`/`\` escaped; soft-deleted rows hidden;
   `node_count` = `jsonb_array_length(nodes)` of the draft version (one join, no N+1). `next_cursor` null at end.
5. Get: `version` = draft (default) | production | staging | positive int. production/staging unset → 404
   `PATHWAY_VERSION_NOT_FOUND`; unknown int → same. Response per §3 plus `version_status`.
6. Update draft: body all optional + `expected_graph_hash`; locks the pathway row (`SELECT … FOR UPDATE`) then the
   draft. Hash mismatch → 409 `CONFLICT` "The draft changed since it was loaded." `details.current_graph_hash`.
   `nodes`/`edges`/`global_config` each replace the stored value when present (absent ones keep stored). Save-mode
   validation errors → 400 as in 2. Response `validation` = the full `validation_report` (save mode).
   `folder_id: null` moves to root (use `model_fields_set` to distinguish absent from null).
7. Validate: body optional `{nodes, edges, global_config, mode: "save"|"publish"(default "publish")}`; no graph in
   body → validate the draft. Never 400 for issues: returns `{"status":"success","validation":{...}}`; schema
   errors still 400.
8. Delete: references (only non-deleted owners) = phone numbers whose `inbound_config->>'pathway_id' = id` and
   status in (pending, active); agents whose `env_pins` semver points to an `agent_versions` row with this
   `pathway_id`; personas whose production version `config->>'pathway_id' = id`; widgets with `pathway_id = id`.
   Each `{type: phone_number|agent|persona|widget, id, name}` (`name` = e164 / name). Non-empty and not force → 409
   `CONFLICT` "Pathway is in use." `details.references`. Soft delete sets `deleted_at`; a deleted pathway reads as 404
   everywhere.
9. Audit: `pathway.create` (after `{name, description, folder_id, graph_hash}`), `pathway.update` (before/after
   only of changed `name`/`description`/`folder_id` plus `graph_hash`; no row when nothing changed),
   `pathway.delete` (`{force, references: n}`). Validate is `audit_exempt("read-only check")`.

### Files
- `apps/api/pyproject.toml`: add `va-engine` dependency + `[tool.uv.sources] va-engine = { workspace = true }`;
  `uv lock`. (T10 test chat needs the engine in the api anyway.)
- `packages/va_db/src/va_db/repositories/pathways.py` (new; export from `va_db.repositories.__init__`):
  `PathwayRepo(session, org_id, clock=)`: `create(name, description, folder_id, graph_json, graph_hash) ->
  tuple[Pathway, PathwayVersion]`, `get(pathway_id, *, lock=False) -> Pathway` (NotFoundError for absent/deleted/
  other org), `version(pathway: Pathway, number: int) -> PathwayVersion | None`, `draft(pathway, *, lock=False)`,
  `list(limit=, cursor=, folder_id=, search=) -> Page[PathwayListRow]` (dataclass with pathway + node_count),
  `folder_exists(folder_id) -> bool`, `references(pathway_id) -> list[PathwayReference]`, `soft_delete(pathway)`,
  `validation_refs(pathway_id) -> (kb_ids, tool_ids, pathway_ids)` sets. Every query filters `org_id`.
- `apps/api/src/va_api/auth/pathway_roles.py` (new): `require_pathway_editor` dependency, `can_force_delete(ctx)`.
- `apps/api/src/va_api/services/pathways.py` (new): `parse_graph(nodes, edges, global_config) -> PathwayGraph`
  (ValidationError → AppError INVALID_INPUT field via `schema_field`; multiple schema errors → first one is fine),
  `validation_errors(issues) -> AppErrors`, `graph_refs(repo, pathway_id) -> GraphRefs`, `allow_http(settings)`,
  `create_pathway`, `update_draft`, `delete_pathway`, `validate_pathway` — each takes the open tenant session.
- `apps/api/src/va_api/errors.py` (or va_common.errors): `AppErrors(items: Sequence[AppError])` exception
  rendered by the AppError handler path with all items (status = first item's) — reuse `error_response`; check
  `test_errors` parity tests.
- `apps/api/src/va_api/routers/pathways.py` + `pathways_schemas.py` (new): `pathways_router =
  legacy_router(prefix="/v1/pathway", tags=["Pathways"])` (tag must exist in `va_api.openapi.TAG_NAMES`; add if
  missing), operation ids `pathways_create`, `pathways_list`, `pathways_get`, `pathways_update`, `pathways_delete`,
  `pathways_validate`; literal `/create` declared before `/{pathway_id}`; every op `x-scope`, examples, audited/
  audit_exempt markers; register in `va_api.main.ROUTERS`. Responses are legacy bodies (`status: "success"` +
  fields), models with `examples`.
- `apps/api/tests/support/cross_tenant_factories.py`: factory `pathway_id` (seed a pathway + draft via
  PathwayRepo in the other org); register create/update/validate request bodies if the model example is not enough.
- specs/12 §3 Decision; AGENTS.md dated bullet; `make openapi` + both TS `gen:api` (commit `openapi/*.json`,
  `schema.d.ts`).

### Tests (apps/api/tests/)
- `test_pathways_api.py` (session/API-key via `session_helpers`, module Postgres): create default start node →
  get returns it with graph_hash = `graph_hash(dump)`; create with Bland fixture graph (`packages/va_engine/tests/
  fixtures/bland/*.json`, copy one into `apps/api/tests/fixtures/pathways/` or load by path) → warnings in
  update response; schema error → 400 field `nodes[0].data...`; save-mode error (duplicate node id) → 400 with
  details.code `PATHWAY_DUPLICATE_NODE_ID`; list paging (limit 2, cursor), search escaping (`%` literal),
  folder filter, node_count; get `?version=production` unset → 404 PATHWAY_VERSION_NOT_FOUND, `?version=abc` → 400;
  update replaces nodes only, keeps global_config; `expected_graph_hash` mismatch 409 with current hash; folder_id
  null vs absent; validate draft (publish mode errors returned with 200) and supplied graph; delete referenced by
  phone number/widget/agent pin/persona → 409 references; force by owner ok / developer 403; deleted → 404 on get/
  update/list hides; other org → 404 for every `{pathway_id}` route.
- `test_pathways_permissions.py`: parametrized roles × (read, write, force delete) per §10 (analyst/viewer read ok,
  write 403; billing read 403 via scope).
- Audit rows via `session_helpers.audit_rows` (create/update/delete, none for no-op update).
- va_db `packages/va_db/tests/test_pathway_repo_db.py`: list ordering/cursor, references query each type,
  other-org isolation.
- Cross-tenant suite + audit markers + openapi contract tests pass automatically once routes exist.

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; targeted pytest of new tests;
`make openapi` + gen:api ×2 and `make test-ts`; `check_spec_refs`; `make test` in background with Monitor
heartbeat (truncate `logs/make_test.log` after the old wrapper exits); radon avg; jscpd 0 clones; pip-audit +
pnpm audit. No e2e (no call flow).

### Risks
- Adding va-engine to the api pulls va_voice/onnxruntime into the api image — acceptable; check the Dockerfile
  test still passes and mypy `files` unchanged.
- Cross-tenant suite needs request bodies for create/update/validate examples that pass validation.
- Concurrency: two updates with the same expected hash must serialise on the row lock (one 409) — add a test.

## Build P07-T09a (done)
- New: `va_db.repositories.PathwayRepo` (+ `PathwayGraphJson`, `PathwayListRow`, `PathwayReference`, `PathwayRefIds`,
  `escape_like`; method `page` instead of `list`, mypy rejects a `list` method used in annotations), `va_api.services.pathways`
  (`GraphInput`, `DraftUpdate.column_values`, `check_graph` in a worker thread, create/update/validate/delete/load_version,
  `parse_version_selector`), `va_api.auth.pathway_roles` (editor guard from the §3.2 "Pathways / folders" cell, force-delete
  check), legacy `routers/pathways.py` + `pathways_schemas.py` (in `main.ROUTERS`), `va_common.errors.MultiAppError`
  (N818 forbids the plan's `AppErrors` name; `AppError.error_items()` used by the api handler). api depends on va-engine.
- Spec: specs/12 §3 Decision P07-T09a. AGENTS.md bullet. openapi + both schema.d.ts regenerated.
- Tests: apps/api test_pathways_api.py (15), test_pathways_permissions.py (10), va_db test_pathway_repo_db.py (9),
  va_common test_errors MultiAppError (3); cross-tenant factory `pathway_id`.
- Gates: lint/typecheck/placeholders pass; check_spec_refs 0; openapi drift clean; test-ts pass (22/52/48);
  make test 12195 passed, 7 skipped, 99.07 %; after the run update_draft was split for radon (C13 → B7) and the
  unused `GraphInput.sent` removed, then pathway api/repo/cross-tenant tests re-run: 44 passed, services/pathways.py 100 %.
  radon avg A 2.92; jscpd 0 clones; pip-audit clean; pnpm audit 3 moderate (pre-existing, none high).

## Review P07-T09a (approved)
Gates re-run: lint/typecheck/placeholders pass; check_spec_refs 0; openapi drift current; jscpd 0 clones; radon avg A
2.63 (new modules); make test 12195 passed, 7 skipped, 99.07 %. Diff matches specs/12 §3/§10/§11 and the
P07-T09a Decision (editor guard, force delete owner/admin or `*` key, MultiAppError items, row-locked hash check).

## Plan P07-T09b — Pathway versions: list/get, save version, publish, promote, revert (specs/12 §1, §4, §10, §11, §12)

### Spec decision — append `> Decision (2026-11-03): pathway versions (P07-T09b).` after the specs/12 §4 revert/duplicate text (before §5)
1. Permissions (supersedes P07-T09a item 1 for API keys): list/get versions need `pathways:read`. Save version =
   `pathways:write` + `require_pathway_editor`. Publish/promote/revert = `pathways:write` + new
   `va_api.auth.pathway_roles.require_pathway_publisher`: session principals must hold the `pathways.publish`
   permission (specs/04 §3.2 row "Pathways: publish / promote production": owner/admin/developer) else 403
   `FORBIDDEN` "Your role cannot publish pathways."; API keys pass on `pathways:write` (specs/04 §2.3 lists
   publish/promote under `pathways:write`; the §10 `pathways:publish` name is not a scope). Declare
   `x-permission: pathways.publish` in openapi_extra on those three.
2. Invariant: exactly one `status='draft'` row per pathway and `draft_version == latest_version` after every
   operation. Every mutating operation takes the pathway row lock (`PathwayRepo.get(lock=True)`) first.
3. Save version: body `{name?: str 1-200 trimmed | null}`; draft row → `status='archived'`, `name` set; a new
   draft `latest_version+1` copies nodes/edges/global_config/graph_hash (name NULL); pathway
   `draft_version = latest_version = n+1`. Response `{"status":"success","saved_version":n,"draft_version":n+1}`.
   Audit `pathway.version.save` after `{version_number, name, graph_hash}`.
4. Publish: body `{version_number?: int ≥ 1, environment: "production"|"staging" = "production",
   run_regression?: bool}`. Missing number = the draft; unknown number → 404 `PATHWAY_VERSION_NOT_FOUND` field
   `version_number`. Publish-mode validation (refs as in T09a, `pathway_id` = this pathway) → 400 with one
   `INVALID_INPUT` item per error (same `validation_errors`). `run_regression` / suite `run_on_publish` are
   accepted but regression gating (and `pending_regression`) is wired in P07-T11; T09b never runs tests.
   Version → `published`; `published_at`/`published_by` set on the FIRST publish only (a later publish of the same
   version to another environment moves only the pin); `published_by` = `creating_user_id(ctx, session)`.
   Pin `production_version` or `staging_version`. If it was the draft → new draft `latest+1` copied from it.
   Response `{"status":"success","published_version":v,"environment":env,"draft_version":d}`. Audit
   `pathway.publish` before `{environment, version: previous pin}` after `{environment, version: v}`.
5. Promote: no body. `staging_version` NULL → 409 `CONFLICT` "The pathway has no staging version." (no field).
   Sets `production_version = staging_version`; audit `pathway.promote` before/after `{production_version}` only
   when it changed (always 200). Response `{"status":"success","production_version":v}`.
6. Revert: body `{version_number: int ≥ 1}` required; unknown → 404 `PATHWAY_VERSION_NOT_FOUND` field
   `version_number`. Draft graph columns replaced with a copy of that version's (number unchanged, name
   unchanged); never rejected for validation issues (stored versions passed save mode once), the response carries
   the save-mode report: `{"status":"success","draft_version":d,"graph_hash":h,"validation":{...}}`. Reverting to
   the draft itself or to an identical graph is a no-op without audit. Audit `pathway.revert` before
   `{graph_hash}` after `{graph_hash, version_number}`.
7. Versions list: all versions, `version_number DESC`, item `{version_number, name, status, is_production,
   is_staging, graph_hash, published_at, published_by, created_at}` (no pagination, as in the spec). Get one:
   `GET /v1/pathway/{id}/version/{version_number}` returns the same body as `GET /v1/pathway/{id}?version=n`
   (`pathway_detail`), 404 `PATHWAY_VERSION_NOT_FOUND` when absent.
8. Cache: `pathway:{<id>}:v:{env}` — WAIT: specs/03 key is literally `pathway:{id}:v:{env}`; build it with a new
   `va_db.pathway_cache.pathway_cache_key(pathway_id, environment)` → `f"pathway:{pathway_id}:v:{environment}"`
   (environment `production`|`staging`), and `invalidate_pathway_cache(redis, pathway_id, environments) ->
   list[str]` (DEL per key; `RedisError`/`OSError` logged `pathway_cache_unavailable`, never raised; returns
   failed envs). Called after the transaction commits: publish → its env; promote → `production`. Only pinned
   versions are cached; a draft fallback (no production pin, specs/12 §1) must never be cached (note for P07-T12).
9. specs/04 §13 audit list: add `pathway.version.save` and `pathway.revert` (after `pathway.promote`); add both to
   `va_db.audit.AUDIT_ACTIONS` (test_audit_unit compares).

### Files
- `packages/va_db/src/va_db/repositories/pathways.py`: add `versions(pathway) -> list[PathwayVersion]` (desc),
  `fork_draft(pathway, source: PathwayVersion) -> PathwayVersion` (insert copy as `latest+1` draft, bump
  `pathway.draft_version`/`latest_version`, flush, refresh), `freeze(version, *, status: "archived"|"published",
  name: str | None = None, published_by: uuid | None = None)` (sets published_at from clock only when first
  publish), `set_pin(pathway, environment, number)`, and `copy_graph(source) -> PathwayGraphJson` helper (or a
  `PathwayGraphJson.of(version)` classmethod) reused by revert via existing `update_draft(..., graph=)`. Keep
  every query org-filtered. Order of writes in save/publish: freeze the old draft first, flush, then insert the new
  draft (no unique clash: numbers differ).
- `packages/va_db/src/va_db/pathway_cache.py` (new): `PATHWAY_CACHE_ENVIRONMENTS`, `pathway_cache_key`,
  `invalidate_pathway_cache`.
- `packages/va_db/src/va_db/audit.py`: two actions.
- `apps/api/src/va_api/auth/pathway_roles.py`: `NOT_PUBLISHER_MESSAGE`, `is_pathway_publisher(ctx)`,
  `require_pathway_publisher`.
- `apps/api/src/va_api/services/pathway_versions.py` (new; keep services/pathways.py < ~400 lines): `list_versions`,
  `get_version` (reuse `load_version`), `save_version(session, ctx, pathway_id, name) -> (saved, draft)`,
  `publish_version(session, ctx, pathway_id, PublishRequest, allow_http) -> PublishOutcome(published, environment,
  draft_version)`, `promote(session, ctx, pathway_id) -> (pathway, changed: bool)`, `revert(session, ctx,
  pathway_id, number, allow_http) -> (pathway, draft, report)`. Reuse `check_graph`, `validation_errors`/
  `_reject_errors` (make public `reject_errors` in services.pathways), `VERSION_NOT_FOUND_MESSAGE`,
  `GraphInput.over`-style construction from a stored version (add `GraphInput.of(version)` classmethod).
- `apps/api/src/va_api/routers/pathway_versions.py` (new; routes attach to `pathways_router`, side-effect import in
  `va_api/main.py` with `# noqa: F401`, like auth_password) + `routers/pathway_versions_schemas.py` (request/response
  models with `examples`; `VersionItem`, `PathwayVersions`, `VersionSave`, `VersionSaved`, `PublishBody`,
  `Published`, `Promoted`, `RevertBody`, `Reverted`). Operation ids `pathways_versions_list`,
  `pathways_version_get`, `pathways_version_save`, `pathways_publish`, `pathways_promote`, `pathways_revert`;
  audited markers `pathway.version.save|publish|promote|revert`. Path `version_number` `Annotated[int, Path(ge=1)]`.
  Cache invalidation after the `tenant_session` block via `redis_of(request)`.
- `apps/api/tests/support/cross_tenant_factories.py`: `register_resource_factory("version_number", <returns "1">,
  path_prefix="/v1/pathway/")`; `register_owner_status("pathways_promote", 409)` (factory pathway has no staging);
  if the factory's one-node graph fails publish-mode validation, give the factory a publishable graph instead
  (don't weaken). Revert/publish owner controls must be 2xx.
- specs/12 §4 Decision, specs/04 §13 list; AGENTS.md dated bullet; `make openapi` + both `gen:api`.

### Tests
- `apps/api/tests/test_pathway_versions_api.py` (module Postgres via existing pathways test helpers; reuse the `Api`
  helper from test_pathways_api — move it to `tests/support/pathway_helpers.py` if needed to avoid jscpd clones):
  lifecycle create → update → save ("v1 greeting") → publish production (draft) → update → publish staging → promote
  → revert to 1; after EVERY step assert exactly one draft row, `draft_version == latest_version`, statuses and pins
  (`versions` list order/flags); publish of archived v1 → status published, no new draft; second publish of same
  version keeps `published_at`; publish validation error (publish-mode-only issue, e.g. unreachable node or a
  rule that is warning in save but error in publish — pick from va_engine.validation RULES) → 400 items, nothing
  changed; unknown version_number → 404 for publish/revert/get; promote without staging → 409; revert no-op writes
  no audit; audit rows for all four actions with expected before/after; cache: pre-set
  `pathway:{id}:v:production` / `:staging` in the container redis, publish staging deletes only staging, promote
  deletes production; Redis failure (monkeypatch broken redis / patch DEL) → publish still 200 + log
  `pathway_cache_unavailable`; concurrent save-version ×2 (asyncio.gather on two clients) → numbers distinct,
  one draft.
- `apps/api/tests/test_pathways_permissions.py`: extend parametrization — versions list/get for analyst/viewer ok,
  billing 403; save version analyst 403; publish/promote/revert developer ok, analyst/viewer 403; API key with only
  `pathways:write` can publish; key with only `pathways:read` cannot.
- `packages/va_db/tests/test_pathway_repo_db.py`: fork_draft/freeze/set_pin/versions + other-org isolation;
  `packages/va_db/tests/test_pathway_cache.py` (fakeredis: key format, invalidate, failure returns envs).
- Cross-tenant, audit markers, openapi contract suites pass.

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; targeted pytest; `make openapi`
+ `corepack pnpm --filter dashboard gen:api` + `corepack pnpm --filter @voiceagents/sdk gen:api` + `make test-ts`;
`uv run python scripts/check_spec_refs.py`; `make test` in background (truncate `logs/make_test.log` first,
Monitor heartbeat); radon avg; `npx --yes jscpd@4 --silent` 0 clones; pip-audit + pnpm audit. No e2e.

### Risks
- jscpd: new router/service mirror T09a patterns — share helpers instead of copying (version-not-found raise,
  lock+load).
- `published_by` FK to users: API key without creator → NULL.
- FastAPI `Path(ge=1)` violations render as 400 INVALID_INPUT via the existing handler — assert it.
- Promote owner control status registration; publish owner control needs a publish-valid factory graph.

## Build P07-T09b (done)
- va_db: `PathwayRepo.versions/freeze/fork_draft/set_pin`, `PathwayGraphJson.of`, `PinEnvironment`; new
  `va_db.pathway_cache` (key + invalidate, logs `pathway_cache_unavailable`); audit actions `pathway.version_save`
  (two-part: the §13 parser regex only reads `a.b`), `pathway.revert` (specs/04 §13 + AUDIT_ACTIONS).
- api: `services/pathway_versions.py`, `routers/pathway_versions.py` (+ schemas; side-effect import in main),
  `pathway_roles.require_pathway_publisher`; `services.pathways.reject_errors` public, `GraphInput.of`.
- Tests: test_pathway_versions_api.py (11), permissions extended (version role matrix ×6, key scopes ×2, helper),
  va_db repo + cache tests; `tests/support/pathway_helpers.py` (Api moved, `star_api`, `seed_pathway`).
  Cross-tenant: `version_number` factory with new `marker=False` option (value "1" leaked everywhere as marker),
  promote owner status 409, revert request version 1.
- Spec: specs/12 §4 Decision P07-T09b (+ §11 name note). openapi + both schema.d.ts regenerated.
- Gates: lint/typecheck/placeholders pass, check_spec_refs 0, test-ts 22/52/48, radon avg A 1.85, jscpd 0 clones,
  pip-audit clean, pnpm audit 3 moderate (pre-existing).
- make test: 12219 passed, 7 skipped, 99.08 % (tests refactored for jscpd afterwards; those 45 re-run, pass).

## Review P07-T09b (approved)
Gates re-run: lint/typecheck/placeholders pass; check_spec_refs 0; openapi drift current; jscpd 0 clones; make test
12219 passed, 7 skipped, 99.08 %. Diff matches specs/12 §1/§4 and Decision P07-T09b (row lock, one draft invariant,
publish-mode validation, first-publish published_at, promote 409, revert save-mode report, cache invalidation after
commit, audit actions in specs/04 §13 + AUDIT_ACTIONS).

## Plan P07-T09c — Pathway folders, duplicate, import/export (specs/12 §4 duplicate, §5, §6, §10, §11, §12)

### Spec decision — append `> Decision (2026-11-03): pathway folders, duplicate, import/export (P07-T09c).` after specs/12 §6 (before §7)
1. Route order: `/v1/pathway/folders…` and `POST /v1/pathway/import` would be captured by the uuid-typed
   `/{pathway_id}` routes, so they live on routers listed in `main.ROUTERS` BEFORE `pathways_router`:
   `routers/pathway_folders.py` `pathway_folders_router` (prefix `/v1/pathway/folders`) and
   `routers/pathway_io.py` `pathway_io_router` (prefix `/v1/pathway`: `POST /import`, `GET /{pathway_id}/export`,
   `POST /{pathway_id}/duplicate`). Permissions: export + folder list/get = `pathways:read`; everything else
   `pathways:write` + `require_pathway_editor`.
2. Duplicate: body `{name?: 1-200 trimmed, version?: int ≥1 | "draft"|"production"|"staging" = "draft"}`
   (selector parsed with `parse_version_selector`, field `version`; unset pin/unknown number → 404
   `PATHWAY_VERSION_NOT_FOUND` field `version`). Name default `"Copy of " + source name` cut to 200 chars. The copy
   keeps description and folder, gets draft v1 with the chosen version's graph (re-checked in save mode; references
   to the source pathway are kept). Response like create. Audit `pathway.duplicate` on the NEW pathway, after
   = create summary + `{source_pathway_id, source_version}`.
3. Export: `GET /v1/pathway/{id}/export?version=` (default draft; same selector + errors as GET) →
   `{"format":"voiceagents.pathway.v1","name","description","nodes","edges","global_config","exported_at"}`
   (`exported_at` = request clock, ISO-8601 UTC with `Z`). No audit (read).
4. Import: body `{format?, name?, description?, nodes, edges, global_config?, folder_id?}`; extra keys ignored
   (`exported_at`, Bland fields). `format`, when present, must be `voiceagents.pathway.v1` (else 400
   `INVALID_INPUT` field `format`); absent = Bland export. Bland's `globalConfig` key is accepted as an alias of
   `global_config` (validation_alias AliasChoices). Name default `"Imported pathway"`. Node aliases and
   `transferNumber` normalisation come from `load_graph` (specs/11 §3.3). Save-mode validation: errors reject (one
   item each), missing tools/KBs stay in the graph. Response `{status, pathway_id, version_number, validation,
   warnings}` where `warnings` = the validation warnings with code `PATHWAY_TOOL_MISSING`/`PATHWAY_KB_MISSING`.
   Audit `pathway.import` (create summary + `{format: "voiceagents.pathway.v1"|"bland"}`). Round trip: an export
   imported again has the same `graph_hash`.
5. Folders: depth = number of folders on the path from the root (a root folder has depth 1), max 5. Every
   structural write (create with parent, PATCH parent, delete) locks all folder rows of the org
   (`SELECT id … FOR UPDATE ORDER BY id`) and checks the in-memory tree, so concurrent moves cannot build a cycle
   or exceed the depth. Errors: unknown folder (path) → 404 `NOT_FOUND` "Folder not found."; unknown `parent_id`
   → 404 `NOT_FOUND` field `parent_id`; too deep → 400 `INVALID_INPUT` "Folders nest at most 5 levels deep."
   field `parent_id`; moving into itself/a descendant → 400 `INVALID_INPUT` "A folder cannot move into itself or
   one of its sub-folders." field `parent_id`.
   - `GET /v1/pathway/folders` → `{status, folders:[FolderView]}` ordered by name, id (no pagination).
     `FolderView = {folder_id, name, parent_id, created_at, updated_at}`. `GET /folders/{id}` → `{status, folder}`.
   - `POST /folders` `{name, parent_id?}` → `{status, folder}`; `PATCH /folders/{id}` `{name?, parent_id?}`
     (`parent_id: null` = root; only sent fields apply; no-op writes no audit) → `{status, folder}`.
   - `DELETE /folders/{id}`: its pathways (every row with that folder_id, soft-deleted included) and its direct
     sub-folders move to its parent (or root), then the row is deleted (so the DB cascade never deletes nested
     folders). → `{status, message:"Folder deleted.", moved_pathways: n, moved_folders: m}`.
   - `POST /folders/{id}/move` `{pathway_ids: [uuid], max 100, unique}` → sets `folder_id` on those live pathways;
     any id not a live pathway of the org → 404 `NOT_FOUND` "Pathway not found." field `pathway_ids[i]`, nothing
     changes. Empty list = no-op 200. → `{status, moved: n}` (moved = rows whose folder changed).
6. Audit actions (two-part form required by the specs/04 §13 parser): `pathway.import`, `pathway.duplicate`,
   `pathway_folder.create`, `pathway_folder.update`, `pathway_folder.delete`, `pathway_folder.move` (the §11
   `pathway.folder.*`). Add them to specs/04 §13 after `pathway.revert` and to `va_db.audit.AUDIT_ACTIONS`;
   update the specs/12 §11 line with a note.

### Files
- `packages/va_db/src/va_db/repositories/pathway_folders.py` (new; export from `va_db.repositories.__init__`):
  - pure `FolderTree(parents: Mapping[uuid, uuid|None])`: `depth(id)`, `height(id)` (levels of the subtree incl.
    itself), `is_within(candidate, root)` (candidate == root or a descendant), `children(id)`.
    `MAX_FOLDER_DEPTH: Final = 5`.
  - `PathwayFolderRepo(session, org_id, clock=)`: `list_folders()`, `get(folder_id)` (NotFoundError → AppError
    NOT_FOUND "Folder not found."), `lock_tree() -> FolderTree` (FOR UPDATE), `create(name, parent_id)`,
    `update(folder, values)`, `delete(folder) -> (moved_pathways, moved_folders)` (two UPDATEs then DELETE, all
    org-filtered), `move_pathways(folder_id, ids) -> int` (UPDATE … WHERE id IN ids AND org_id AND deleted_at IS
    NULL AND folder_id IS DISTINCT FROM :f), `live_pathway_ids(ids) -> set`.
  - `PathwayRepo.folder_exists` stays (or delegates).
- `apps/api/src/va_api/services/pathway_folders.py` (new): `create_folder`, `update_folder`, `delete_folder`,
  `move_pathways`, messages constants; audits; depth/cycle checks via FolderTree.
- `apps/api/src/va_api/services/pathway_io.py` (new): `EXPORT_FORMAT = "voiceagents.pathway.v1"`,
  `export_pathway(session, ctx, pathway_id, selector, now) -> dict`, `import_pathway(session, ctx, body-values,
  allow_http) -> (pathway, draft, report)`, `duplicate_pathway(...)`, `missing_reference_warnings(report)`.
  `services.pathways.create_pathway` gains keyword `action: str = "pathway.create"` and `audit_extra: Mapping |
  None = None` and returns the CheckedGraph report too (or add `create_checked` returning `(pathway, draft,
  report)`; keep the existing signature for the create route).
- `apps/api/src/va_api/routers/pathway_folders.py` + `pathway_folders_schemas.py`; `routers/pathway_io.py` +
  `pathway_io_schemas.py` (reuse `Name`/`Description`/`GraphNodes`/`GraphEdges`/`ValidationView`/`validation_view`
  from `pathways_schemas`; every model has an `examples` entry; operation ids `pathways_folders_list`,
  `pathways_folders_get`, `pathways_folders_create`, `pathways_folders_update`, `pathways_folders_delete`,
  `pathways_folders_move`, `pathways_import`, `pathways_export`, `pathways_duplicate`; audited markers).
- `apps/api/src/va_api/main.py`: add both routers to `ROUTERS` before `pathways_router`.
- `packages/va_db/src/va_db/audit.py` + specs/04 §13 + specs/12 §11/§6 Decision; AGENTS.md bullet.
- Cross-tenant (`apps/api/tests/support/cross_tenant_factories.py`): `folder_id` factory (root folder via
  `PathwayFolderRepo.create`); `register_request("pathways_folders_move", json={"pathway_ids": []})`;
  `pathways_duplicate` json `{"name": "Copy"}` (default draft); import example must be a valid graph.
- `make openapi` + both `gen:api`.

### Tests
- `packages/va_db/tests/test_pathway_folders_unit.py`: FolderTree depth/height/is_within (chains, siblings, unknown).
- `packages/va_db/tests/test_pathway_folder_repo_db.py`: create/list/update, delete re-parents pathways (incl.
  soft-deleted) and sub-folders, move_pathways counts only changes, org isolation (other org folder invisible,
  other org pathway ids not moved).
- `apps/api/tests/test_pathway_folders_api.py`: create root/child; depth 5 ok, 6th → 400 field parent_id; PATCH
  into own descendant → 400; PATCH moving a 3-level subtree under depth-3 folder → 400 (height check); parent_id
  null → root; delete moves pathways + sub-folders to parent; move 404 for unknown/deleted/other-org id with field
  `pathway_ids[1]` and nothing moved; list filter `GET /v1/pathway?folder_id=` reflects moves; audit rows for each
  action, none for no-op PATCH/empty move; concurrent PATCHes A→under B and B→under A (gather) → exactly one 400.
- `apps/api/tests/test_pathway_io_api.py`: import each Bland fixture (`graph_helpers.fixture`) — legacy_import
  aliases normalised (`Transfer Node` → `Transfer Call` in GET body), missing KB → `warnings` item
  `PATHWAY_KB_MISSING`; `globalConfig` alias; bad `format` → 400 field format; save-mode error (dangling edge) →
  400 items, no pathway created; export → import round trip equal `graph_hash` + nodes/edges/global_config for all
  fixtures; export `?version=production` without pin → 404; export body keys exact + `exported_at` = container
  clock; duplicate default draft, `version: 1`, `"production"` after publish, name default `Copy of …` (and a
  200-char source name truncated), unknown version 404; audit rows `pathway.import`/`pathway.duplicate`.
- `apps/api/tests/test_pathways_permissions.py`: extend the role matrix (export/folder list → analyst/viewer ok,
  billing 403; import/duplicate/folder writes → analyst 403, developer ok).
- Cross-tenant, audit-marker, openapi contract suites pass; `test_audit_unit` list mirror.

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; targeted pytest; `make openapi`
+ `corepack pnpm --filter dashboard gen:api` + `corepack pnpm --filter @voiceagents/sdk gen:api` + `make test-ts`;
`uv run python scripts/check_spec_refs.py`; `make test` in background (truncate `logs/make_test.log` first, Monitor
heartbeat); `uvx radon cc -s -a`; `npx --yes jscpd@4 --silent` 0 clones; pip-audit + pnpm audit. No e2e.

### Risks
- Route order: verify `GET /v1/pathway/folders` and `POST /v1/pathway/import` are not answered by the
  `{pathway_id}` routes (test it explicitly).
- jscpd: folder/io schemas mirror pathways_schemas — import the shared aliases, don't copy.
- Export → import hash equality relies on `dump_graph(load_graph(dump))` being idempotent; if a fixture breaks it,
  that is a real engine bug — fix it in va_engine, don't weaken the test.
- `services/pathways.py` is 372 lines: put new logic in new modules.

## Build P07-T09c (in progress → done when make test passes)
- va_db: `repositories/pathway_folders.py` (`FolderTree`, `PathwayFolderRepo` lock_tree/create/update/delete/move,
  `MAX_FOLDER_DEPTH`), audit actions (`pathway.import|duplicate`, `pathway_folder.create|update|delete|move`) in
  specs/04 §13 + AUDIT_ACTIONS.
- api: `services/pathway_folders.py`, `services/pathway_io.py`, `services.pathways.create_checked` (+`PathwayCreation`),
  routers `pathway_folders(.py|_schemas)`, `pathway_io(.py|_schemas)` before `pathways_router` in ROUTERS; shared
  router aliases (`EDITOR`, `READ_SCOPE`, `WRITE_SCOPE`, `ReadPathways`, `WritePathways`, `allow_http`) now public in
  `routers/pathways.py`.
- Tests: va_db unit + repo db, api folders (9), io (12), permissions matrix +6; cross-tenant `folder_id` factory, move
  + duplicate registered requests; `tests/support/pathway_rows.GRAPH` (va_db) shared.
- Spec: specs/12 §6 Decision P07-T09c, §11 note. openapi + both schema.d.ts regenerated. AGENTS.md bullet.
- Gates so far: lint/typecheck/placeholders pass, spec refs 0, jscpd 0, radon avg A 2.92, test-ts 22/52/48,
  pip-audit clean, pnpm audit 3 moderate (pre-existing); targeted pytest 142 passed.
- make test: 12255 passed, 7 skipped, coverage 99.08 %, exit 0 (the jscpd router/test refactor landed mid-run; the
  142 affected tests were re-run afterwards and pass).

## Review P07-T09c (approved)
Gates re-run: lint/typecheck/placeholders pass; check_spec_refs 0; openapi drift current; jscpd 0 clones; make test
12255 passed, 7 skipped, 99.08 %. Diff matches specs/12 §4/§5/§6 and Decision P07-T09c (route order, locked folder
tree, depth/cycle errors, delete re-parenting, move 404 field paths, import format/alias/warnings, export round trip,
duplicate selector + copy name, audit actions in specs/04 §13 + AUDIT_ACTIONS).

## Plan P07-T10 — Pathway test chat (text simulation; specs/12 §7, §10; specs/03 Redis `pathway_chat:{chat_id}`)

Planner note: ticked the P07-T09 parent (a–c all ticked, ac367d2).

### Facts that shape the design
- The api has no LLM gateway, no HTTP executor, no PathwayLoader. The media runtime builds its gateway in
  `va_media.runtime.open_runtime` (LlmGateway(ProviderRegistry(platform_provider(settings), OrgProviderStore(database,
  envelope, clock=)), http_client(settings, timeout=LLM_HTTP_TIMEOUT), clock=, metrics=LlmMetrics(registry, catalog),
  costs=CostTable(load_costs(...)))`). `LlmMetrics(registry, None)` works without the media catalog counters.
- `/v1/pathway/chat/create` and `/v1/pathway/chat/{chat_id}` would be captured by the uuid-typed `POST
  /v1/pathway/{pathway_id}` → new router listed in `main.ROUTERS` BEFORE `pathways_router` (like folders/io).
- Engine channels: `widget_chat` turns transfers into `HumanTakeover` and ignores press button, which would hide what
  §7 says must be shown ("transfer, end call, press button simulated, recorded as actions"). The chat therefore runs
  the engine on channel `voice` (it simulates the call); every non-speech action is recorded, never executed.

### Spec decision — append `> Decision (2026-11-03): pathway test chat (P07-T10).` at the end of specs/12 §7
1. Routes on `routers/pathway_chat.py` `pathway_chat_router` (prefix `/v1/pathway/chat`, legacy envelope, tag
   Pathways, before `pathways_router` in ROUTERS): `POST /create` (op `pathways_chat_create`), `POST /{chat_id}`
   (op `pathways_chat_send`, chat_id = uuid path param), `DELETE /{chat_id}` (op `pathways_chat_reset`; the "reset"
   deliverable: deletes the session → `{status, message: "Chat deleted."}`; unknown → 404). All three
   `require_scope("pathways:write")` + `require_pathway_editor` (analysts 403); `audit_exempt("ephemeral test chat;
   nothing persists beyond Redis")`.
2. Create body: `pathway_id` (uuid, required), `version` (selector as GET: int ≥1 | draft | production | staging,
   default `draft`; errors as `load_version`), `request_data` (object ≤ 64 KiB JSON, default {}), `start_node_id`
   (str|null; must be a node of the graph else 400 INVALID_INPUT field `start_node_id`; the state starts there
   instead of the start node), `simulate_from`/`simulate_to` (E.164 via `va_domain.phone.normalize_e164`, default
   `+15555550100`/`+15555550199`; invalid → 400 field name). Unknown/deleted/other-org pathway → 404 like GET.
   The chat runs the graph of the resolved version (frozen into the session: later draft edits do not change a
   running chat). Engine facts: org, `call_id` = chat id, channel `voice`, request_data, builtins below, timezone
   UTC, empty transfer_list. Builtins = the CallContext-free subset of `va_engine.builtins` computed for an inbound
   call from `simulate_from` to `simulate_to`: `now_utc`, `now_local`, `today`, `from`, `to`, `call_id`,
   `timezone`, `language` (`en-US`), `agent_number` = to, `user_number` = from, `batch_id` null, `metadata` {}.
   Refactor `call_builtins` so both go through one pure `builtin_values(...)` (no duplication).
3. Session = one Redis string `pathway_chat:{chat_id}` (specs/03), JSON `{org_id, pathway_id, version, graph
   (dumped), request_data, simulate_from, simulate_to, created_at, state (dump_engine_state), snapshots: {"<turn
   index>": state}}` with TTL 24 h refreshed on every write. A snapshot is stored after create (turn 0) and after
   every message (key = `state.turn_index` after the turn); only the newest 50 are kept. A chat of another org reads
   as absent → 404 `NOT_FOUND` "Chat not found." (no field). Concurrent sends to one chat: a lock
   `pathway_chat:{chat_id}:lock` `SET NX EX 120`; held → 409 `CONFLICT` "This chat is answering another message.";
   released in `finally`. Redis unavailable → 503 `SERVICE_UNAVAILABLE`.
4. Send body `{message: str 1..4000 (stripped, non-blank), branch_from_turn: int ≥0 | null}`. Ended chat (state
   `ended`) → 409 `CONFLICT` "This chat has ended." unless branching. Branching: the snapshot of that turn (absent →
   400 INVALID_INPUT field `branch_from_turn` "That turn is not available to branch from.") becomes the state of a
   NEW chat id (fresh 24 h key, snapshots copied up to and including that turn); the message runs there; the
   response carries the new `chat_id`; the original chat is untouched. Without branching the response's `chat_id`
   is the same id (always present). The user turn = `UserTurn(index=state.turn_index+1, text, start_ms=end_ms=ms
   since created_at)`.
5. Running a turn: build `PathwayEngine(state, ActiveGraph, ports, facts, clock)` per request; run `start()` (create)
   or `handle_user_turn()`; consume every action in order: `Say.text` / the full `SayStream` text (stripped,
   non-blank) → one `assistant_messages` item each; other actions → `actions` items: `{"type":"wait","seconds"}`,
   `{"type":"transfer","mode","target","caller_id_mode"}`, `{"type":"end_call","reason","ended_by"}`,
   `{"type":"human_takeover"}`, `{"type":"press_button","digits","duration_ms"}`, `{"type":"sms","to","from","body",
   "node_id"}` (from the simulated SmsPort, which returns `sent=True, message_id="sim-<n>"`). Transfer/end call/
   takeover end the chat (the engine sets `ended`). Then `settle_background()`, `aclose()`, persist. A turn is
   capped at 60 s (`va_common.waits.within` on the clock); over → return what was produced with `ended=false` and log
   `pathway_chat.turn_timeout` (state persisted as the engine left it).
6. Responses: create `{status, chat_id, assistant_messages, current_node_id, variables, actions, ended, logs}`
   (superset of the spec example); send `{status, chat_id, assistant_messages, current_node_id, variables, actions,
   ended, logs}`. `variables` = `state.variables`. `logs` = the turn's engine events in emission order mapped:
   `call.route` → `{type:"route", from, to, label, alternatives, global}`; `call.extraction` →
   `{type:"extraction", node_id, variables}`; `call.loop_condition` → `{type:"loop_condition", node_id, score, met}`;
   `call.webhook_node` → `{type:"webhook", node_id, node_name, method, url (redacted), status, ms, outcome}`;
   `call.sms_sent` → `{type:"sms", node_id, sent}`; every request through the chat's HttpPort adds `{type:"tool",
   method, url (redacted_url), request_headers (secret names → "***" via `va_common.masking.is_secret_key`),
   request_body (JSON excerpt ≤ 2 000 chars), status, response_body (excerpt ≤ 2 000 chars), ms, outcome}` right
   before the event of the node that made it; other event types are skipped.
7. Webhook/tool nodes execute for real through `va_tools.http.HttpExecutor` (the container's `http_executor`, SSRF
   guard + `allow_http` = `insecure_urls_allowed(va_env)`); Transfer Pathway / forwarding load other pathways of
   the org through `va_api.services.pathway_loader.DbPathwayLoader(database, org_id)` (production/staging pins or a number;
   missing → `PathwayUnavailableError`). LLM: dialogue/decisions/final lines over the container's `llm_gateway`
   with `DialogueSettings(org_id, direction="inbound", call_id=chat id, counterpart=simulate_from,
   context_budget_tokens=org.llm.context_budget_tokens, speculative=org.engine.speculative)` (org from
   `org_settings` reader). Test-chat LLM usage is not metered (no recorder): it is a builder tool like validate.

### Files
- `apps/api/src/va_api/services/pathway_loader.py` (new; va_db does not depend on va_engine — checked):
  `DbPathwayLoader(database, org_id)` implementing `va_engine.navigation.PathwayLoader` (tenant_session;
  `PathwayRepo.get` + pin/number → `ActiveGraph(uuid, number, load_graph(...))`; NotFound/absent pin/bad uuid →
  `PathwayUnavailableError(reason)`). P07-T12 may move it to a shared package.
- `packages/va_engine/src/va_engine/builtins.py`: `builtin_values(*, now, zone, from_number, to_number, call_id,
  voice_id, timezone, language, inbound, batch_id, metadata)` shared by `call_builtins` and the chat.
- `apps/api/src/va_api/settings.py`: mix in `va_llm.settings.LlmSettings` (drop the duplicated
  `llm_dialogue_model`/`llm_router_model` fields); specs/02 §6 scope of `LLM_MODEL_COSTS_JSON` gains `api`.
- `apps/api/src/va_api/container.py`: `llm_gateway: LlmGateway` (platform + OrgProviderStore over the container's
  envelope; http_client timeout = va_media's LLM_HTTP_TIMEOUT value, closed on shutdown; LlmMetrics on the probe
  registry when available else a private `CollectorRegistry`), `http_executor: HttpExecutor` (enter
  `open_http_executor(settings, clock=, allow_http=insecure_urls_allowed(va_env), resolver=host_resolver)` on the
  container's exit stack), `token_counter` = `va_llm.tokens.default_counter()`. Do NOT prewarm (not a readiness
  check). apps/api pyproject gains va-llm, va-tools.
- `apps/api/src/va_api/services/pathway_chat.py` (new): `ChatDeps(redis, database, gateway, http, org_settings,
  clock, counter)`, `ChatSession` model (pydantic, the Redis document), `ChatStore` (`load(org_id, chat_id)`,
  `save`, `lock(chat_id)` async CM, keys `chat_key`/`lock_key`), `create_chat(deps, ctx, body, allow_http)` →
  `ChatTurn`, `send_message(deps, ctx, chat_id, body)` → `ChatTurn`, `delete_chat(...)`.
- `apps/api/src/va_api/services/pathway_chat_run.py` (new, pure-ish): `ChatRecorder` (EngineEvents sink + HttpPort
  wrapper + simulated SmsPort collecting `logs`/`actions`), `run_actions(agen, recorder) -> list[str]` (assistant
  messages), `log_item(event_type, data)`, `excerpt(value, limit=2000)`, `masked_headers(headers)`,
  `chat_builtins(...)`.
- `apps/api/src/va_api/routers/pathway_chat.py` + `pathway_chat_schemas.py` (reuse `parse_version_selector`,
  `ReadPathways/WritePathways/EDITOR/WRITE_SCOPE` from `routers/pathways.py`; every model has an `examples` entry).
- `apps/api/src/va_api/main.py`: router before `pathways_router`.
- specs/12 §7 Decision, specs/02 §6 scope tweak, AGENTS.md bullet, `make openapi` + both `gen:api`.
- Cross-tenant (`cross_tenant_factories.py`): `chat_id` factory = a chat created through `ChatStore.save` for the
  owner org (no LLM needed: the factory writes the document directly); `register_request("pathways_chat_create",
  json={"pathway_id": <owner pathway>})` needs the owner's pathway → if the registry cannot express that, mark the
  owner control with `register_owner_status(op, 404)`? No — use the existing `pathway_id` factory mechanism
  (check how `pathways_duplicate` gets its id) and send `pathway_id` in the body; for the send op register json
  `{"message": "hi"}` and an owner status that does not need the LLM: the factory chat is `ended` → owner control
  409 (`register_owner_status("pathways_chat_send", 409)`); reset → 200.

### Tests (apps/api/tests/)
- `support/chat_helpers.py`: `chat_container(identity)` → `SessionContainer` whose `llm_gateway` is a real
  LlmGateway over FakeLLM on a socket (`running_sims(state, ports={"llm": 0})`, SystemClock, platform provider =
  sim URL/key, models = sim defaults) and `http_executor` over `host_resolver`; `rules(state, yaml)` loads FakeLLM
  rules (route_decision / extraction / dialogue replies) — reuse `packages/va_engine/tests/support/task_helpers`
  style; restaurant fixture via `graph_helpers.fixture("restaurant")` (check name) seeded with `pathway_helpers`.
- `test_pathway_chat_api.py`:
  - create on the restaurant fixture: opener in `assistant_messages`, `current_node_id` = start, `logs` [], key
    `pathway_chat:{id}` exists with TTL ≤ 86 400.
  - multi-turn: scripted FakeLLM route decisions reproduce the expected route sequence (`logs` route items
    from/to/label match), extraction variables appear in `variables` and an `extraction` log item.
  - webhook node executes for real against `webhook_receiver.served_receiver` (request received, `tool` log item
    with masked `Authorization` header, status, response excerpt) and its `webhook` log item.
  - transfer node → `actions` [{type: transfer …}], `ended` true, a further send → 409 "This chat has ended.".
  - end call → `end_call` action; SMS node → `sms` action, no Telnyx request (simulated).
  - branching: three turns, `branch_from_turn: 1` → new chat_id, its state history = snapshot 1 + the new message;
    original chat unchanged (next send continues from its own turn 3); unknown turn → 400 field.
  - start_node_id override; bad start node → 400 field; invalid simulate_from → 400 field; version production
    without pin → 404 PATHWAY_VERSION_NOT_FOUND; other-org pathway → 404; other-org chat → 404; delete → 404 after.
  - concurrent send while the lock is held (pre-set the lock key) → 409.
  - route-order test: `POST /v1/pathway/chat/create` is not answered by `/{pathway_id}`.
- `test_pathway_chat_run.py` (unit): log_item mapping for every event type, excerpt truncation, header masking,
  action mapping, simulated SMS ids, snapshot pruning to 50.
- `test_pathways_permissions.py`: chat create/send/delete → owner/admin/developer ok, analyst/viewer/billing 403;
  key with only `pathways:read` → 403.
- api loader test (`test_pathway_loader.py`, DB): production/staging/number/missing/other-org.
- va_engine `test_builtins.py` unchanged results for `call_builtins` after the refactor.
- api container test (`test_container.py`): gateway + executor clients closed on shutdown (it asserts close order
  of every httpx client — update it).
- Cross-tenant, audit-marker, openapi contract suites pass.

### Gates
`export PATH=/home/ralph/.local/bin:$PATH`; `make fmt lint typecheck placeholders`; targeted pytest; `make openapi`
+ `corepack pnpm --filter dashboard gen:api` + `corepack pnpm --filter @voiceagents/sdk gen:api` + `make test-ts`;
`uv run python scripts/check_spec_refs.py`; `make test` in background (truncate `logs/make_test.log` first, Monitor
heartbeat); `uvx radon cc -s -a`; `npx --yes jscpd@4 --silent` 0 clones; pip-audit + pnpm audit; env-example test
(settings mixin adds `LLM_*` to api: keep `.env.example`/specs/02 §6 in step). No e2e (no call flow).

### Risks
- FakeLLM scripting of concurrent decision calls: route via rules matched on function name (`route_decision`) and
  `call_index`; keep the dialogue replies deterministic with `respond: {content: ...}`.
- Speculation consumes a dialogue reply whenever it starts: disable via org setting `engine.speculative=false` in
  tests that count dialogue requests, or script enough replies.
- `test_container` asserts every httpx client's close order — update for the gateway and executor clients.
- Redis document size: snapshots capped at 50; history itself capped by the engine.
- Settings mixin may change field order/env-example drift tests: run `scripts/tests/test_env_example.py`.

## Build P07-T10 (in progress → done when make test passes)
- New: services/pathway_chat.py (ChatStore, create/send/delete, _Conversation), services/pathway_chat_run.py
  (ChatRecorder, log_item/action_item/excerpt/masked_headers), services/pathway_loader.py (DbPathwayLoader),
  routers/pathway_chat.py + pathway_chat_schemas.py (router before pathways_router).
- Changed: ApiSettings mixes LlmSettings (dup llm_* fields dropped); container llm_gateway + tools_http;
  va_tools.http.tools_client (shared by open_http_executor); va_engine.builtins.builtin_values (call_builtins delegates).
- Default simulate numbers = spec example +14155550123/+14155550100 (555 area code is not a valid number).
- Analysts refused per specs/12 §10 (chat is in the editor row).
- Tests: test_pathway_chat_api (12, FakeLLM real gateway, webhook receiver, timeout cap via monkeypatched
  TURN_TIMEOUT_S), test_pathway_chat_run (9 unit), test_pathway_loader, chat role matrix + read-only key in
  test_pathways_permissions, cross-tenant chat_id factory (ended chat, owner send 409), test_container close order.
  chat_helpers.WallClock (FrozenClock type over SystemClock: SessionContainer.clock is typed FrozenClock).
- Spec: specs/12 §7 Decision P07-T10; specs/02 LLM_MODEL_COSTS_JSON scope + api. openapi + both schema.d.ts.
- Gates: lint/typecheck/placeholders pass, spec refs 0, jscpd 0, radon avg A 2.92, test-ts 22/52/48, pip-audit
  clean, pnpm audit 3 moderate (pre-existing); targeted pytest 178 passed.
- Rework after empty review.rejected (inactivity timeout of the previous builder, not a finding): previous full
  run had 1 xdist worker crash in unrelated `test_accept_db.py::test_recording_and_hd_audio_reach_the_answer`
  (passes alone, 20 passed); re-ran gates: lint/typecheck/placeholders/specs pass, radon A 2.92, jscpd 0,
  pip-audit clean, pnpm 3 moderate; full make test re-run in background.
- The repeated crash was an OOM kill (dmesg via privileged container): `test_recording_and_hd_audio_reach_the_answer`
  grew a worker by ~440 MB+ (FakeTelnyx 4 h virtual recording). Fixed in the edge routing rig
  (`ring(..., time_limit_s=30)`); file RSS delta gone. AGENTS.md bullet added.
- Final make test: 12284 passed, 7 skipped, coverage 99.08%, exit 0.

## Review P07-T10 (approved)
- Re-ran: lint/typecheck/placeholders exit 0, make test 12284 passed 7 skipped (99 %), test-ts 22/52/48, spec refs 0.
- Diff matches specs/12 §7 + Decision; reviewer fixed specs/02 §6 scope of LLM_ANALYSIS_MODEL/LLM_FALLBACK_MODEL
  (api now reads them through LlmSettings).
