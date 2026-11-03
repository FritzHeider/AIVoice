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
