# 12 — Pathways API

Router module: `apps/api/src/va_api/routers/pathways.py` (+ `pathway_tests.py`, `pathway_chat.py`). Envelope:
**legacy** (`31-error-catalog.md` §1). All endpoints require an API key or dashboard session; role permissions in §10.
Graph JSON shapes are defined in `11-pathways-engine.md` §3.

## 1. Versioning model

- `pathways` row is stable (`id` never changes).
- Each pathway has exactly one **draft** version row (`status='draft'`, number = `pathways.draft_version`). Editing
  (`POST /v1/pathway/{id}`) mutates the draft in place.
- **Publishing** freezes a version (`status='published'`, `published_at`), pins it to an environment
  (`production_version` or `staging_version`) and creates a **new draft** copied from it with number
  `latest_version + 1`.
- `POST /v1/pathway/{id}/version` saves the current draft explicitly: the draft row is frozen with
  `status='archived'` (meaning "saved, never published") and a new draft numbered `latest_version + 1` is created as
  a copy of it. Saved versions are viewable and can be published later (publishing changes their status to
  `published`).
- Calls resolve versions: `pathway_version` omitted/`0`/`"production"` → `production_version`; `"staging"` →
  `staging_version`; integer → that version if it exists (any status), else if org setting
  `pathway_version_fallback=true` (default true) → production, else `PATHWAY_VERSION_NOT_FOUND`. If the pathway has no
  production version, the draft is used and a `call.error` warning `PATHWAY_UNPUBLISHED` is emitted.
- Graph hash: `sha256(canonical_json({nodes, edges, global_config}))` with sorted keys, positions excluded.

## 2. Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/pathway/create` | Create pathway (+ draft v1) |
| GET | `/v1/pathway` | List pathways |
| GET | `/v1/pathway/{pathway_id}` | Get pathway with draft (or `?version=`) graph |
| POST | `/v1/pathway/{pathway_id}` | Update draft (name, description, nodes, edges, global_config, folder_id) |
| DELETE | `/v1/pathway/{pathway_id}` | Soft delete |
| POST | `/v1/pathway/{pathway_id}/validate` | Run validation on draft or supplied graph |
| GET | `/v1/pathway/{pathway_id}/versions` | List versions |
| GET | `/v1/pathway/{pathway_id}/version/{version_number}` | Get one version |
| POST | `/v1/pathway/{pathway_id}/version` | Save current draft as a version, start new draft |
| POST | `/v1/pathway/{pathway_id}/publish` | Publish a version to production or staging |
| POST | `/v1/pathway/{pathway_id}/promote` | Copy staging pin to production |
| POST | `/v1/pathway/{pathway_id}/revert` | Replace draft with a copy of a given version |
| POST | `/v1/pathway/{pathway_id}/duplicate` | Copy pathway (draft of chosen version) to a new pathway |
| GET | `/v1/pathway/{pathway_id}/export` | Export JSON (Bland-compatible) |
| POST | `/v1/pathway/import` | Import JSON (Bland export or ours) |
| GET/POST/PATCH/DELETE | `/v1/pathway/folders[/{folder_id}]` | Folders CRUD |
| POST | `/v1/pathway/folders/{folder_id}/move` | Move pathways into folder |
| POST | `/v1/pathway/chat/create` | Create test chat session |
| POST | `/v1/pathway/chat/{chat_id}` | Send message to test chat |
| GET | `/v1/pathway/chat/{chat_id}` | Get chat transcript + logs |
| DELETE | `/v1/pathway/chat/{chat_id}` | Delete chat session |
| GET/POST | `/v1/pathway/{pathway_id}/tests` | List / create unit tests |
| GET/PATCH/DELETE | `/v1/pathway/{pathway_id}/tests/{test_id}` | Unit test CRUD |
| POST | `/v1/pathway/{pathway_id}/tests/from_call` | Create unit test from a historical call turn |
| POST | `/v1/pathway/{pathway_id}/tests/run` | Run unit tests (all or ids) on a version |
| POST | `/v1/pathway/{pathway_id}/simulations` | Start simulation run (LLM-as-caller) |
| GET | `/v1/pathway/{pathway_id}/runs` | List test/simulation runs |
| GET | `/v1/pathway/{pathway_id}/runs/{run_id}` | Run detail |
| GET/PUT | `/v1/pathway/{pathway_id}/regression` | Regression suite settings |

## 3. Create / list / get / update / delete

### POST `/v1/pathway/create`
Request:
```json
{"name": "Bella Pasta Orders", "description": "Takes phone orders", "nodes": [], "edges": [],
 "global_config": {"globalPrompt": "You are Ava…"}, "folder_id": null}
```
`nodes`/`edges` optional; when empty a single start node `{"id":"start","type":"Default","data":{"name":"Start","isStart":true,"prompt":"Greet the caller."}}` is created.
Response 200:
```json
{"status": "success", "pathway_id": "0193c5d2-…", "version_number": 1}
```

### GET `/v1/pathway`
Query: `folder_id?`, `search?` (name ILIKE), `limit` (1–100, default 50), `cursor?`.
Response:
```json
{"status":"success","pathways":[{"pathway_id":"…","name":"…","description":"…","folder_id":null,
 "production_version":3,"staging_version":4,"draft_version":5,"latest_version":5,
 "node_count":12,"created_at":"…","updated_at":"…"}],"next_cursor":null}
```

### GET `/v1/pathway/{pathway_id}`
Query: `version?` (int | `production` | `staging` | `draft`, default draft).
Response:
```json
{"status":"success","pathway_id":"…","name":"…","description":"…","folder_id":null,
 "version_number":5,"version_status":"draft","production_version":3,"staging_version":4,
 "nodes":[…],"edges":[…],"global_config":{…},"graph_hash":"…","created_at":"…","updated_at":"…"}
```

### POST `/v1/pathway/{pathway_id}` (update draft)
Request (all optional): `name`, `description`, `folder_id`, `nodes`, `edges`, `global_config`, and
`expected_graph_hash` (optimistic concurrency: if supplied and different from the stored draft hash → 409
`CONFLICT` with `details.current_graph_hash`). `nodes`/`edges` replace the full arrays. Validation runs in save
mode; errors (not warnings) reject with 400. Response:
```json
{"status":"success","pathway_id":"…","version_number":5,"graph_hash":"…","validation":{"valid":false,"errors":[],"warnings":[…]}}
```

### DELETE `/v1/pathway/{pathway_id}`
Fails with 409 `CONFLICT` if referenced by an active (non-deleted) phone number inbound config, agent version
pinned in an env, persona production version, or widget — `details.references: [{type, id, name}]` — unless
`?force=true` (owner/admin only), which leaves references dangling (calls using them fail with `NOT_FOUND`).
Response `{"status":"success","message":"Pathway deleted."}`.

> Decision (2026-11-03): pathways API core (P07-T09a). Code: `va_api.routers.pathways`, `va_api.services.pathways`,
> `va_db.repositories.PathwayRepo`, `va_api.auth.pathway_roles`.
> 1. Permissions: reads need `pathways:read`, every write `pathways:write`. specs/04 §3.2 gives analysts
>    `pathways:write` (tests, chat) but §10 denies them graph edits, so create/update/delete/validate (and later
>    import/duplicate/folders/publish) also refuse session roles whose specs/04 §3.2 "Pathways / folders" cell is not
>    `W` (403 `FORBIDDEN` "Your role cannot edit pathways."); API keys pass. The §10 `pathways:publish` key scope is
>    not a specs/04 §2.3 scope: publish/promote/revert use `pathways:write` plus the `pathways.publish` permission.
>    `?force=true` needs an owner/admin session or a `*` API key (else 403 "Force delete requires an owner or
>    admin."), checked before the pathway is loaded.
> 2. Create: `name` 1-200 characters after trimming, `description` ≤ 2 000; `nodes` and `edges` both empty or
>    absent → the default start node above. `folder_id` must name an org folder (404 `NOT_FOUND`, field
>    `folder_id`). Graph JSON is parsed with the specs/11 §3 schema: a schema problem is one 400 `INVALID_INPUT`
>    whose `field` is the graph path (`nodes[3].data.name`). The parsed graph is validated in save mode; every
>    error-severity issue becomes one `errors[]` item `{error: INVALID_INPUT, message, details: {code, node_id?,
>    edge_id?}}` (HTTP 400, all items). The stored graph is the canonical dump (layout keys kept, unset fields
>    dropped) and `graph_hash` is computed from it.
> 3. Reference checks use the org's live knowledge bases and tools (public `KB-`/`TL-` form or the bare uuid) and
>    live pathways; `http`/private webhook URLs pass only in `VA_ENV` `local`/`test`.
> 4. List: `limit` 1-100 (default 50), cursor per specs/31 §2 Decision (`created_at DESC, id DESC`), `search` is a
>    case-insensitive substring with `%`, `_`, `\` matched literally; `node_count` counts the draft's nodes.
> 5. Get: `version` is a positive integer, `draft` (default), `production`, or `staging`; anything else is 400
>    `INVALID_INPUT` field `version`; an unset pin or unknown number is 404 `PATHWAY_VERSION_NOT_FOUND`.
> 6. Update: the pathway row is locked (`SELECT … FOR UPDATE`) before the hash comparison, so two edits with the same
>    `expected_graph_hash` serialise and the second gets 409 (`field` `expected_graph_hash`,
>    `details.current_graph_hash`). Graph parts not sent keep their stored value; `folder_id: null` moves to the
>    root while an absent `folder_id` keeps it. The stored draft is always revalidated in save mode, and the
>    response `validation` is the full save-mode report. `pathway.update` records only changed
>    `name`/`description`/`folder_id`/`graph_hash`; a no-op writes no audit row.
> 7. Validate: body optional (`nodes`, `edges`, `global_config`, `mode` `save`|`publish`, default `publish`);
>    parts not sent come from the draft. Issues are returned with 200, never raised; schema problems are still 400.
>    The route is audit-exempt (read-only).
> 8. References: phone numbers in `pending`/`active` whose `inbound_config.pathway_id` is the pathway; live agents
>    whose `env_pins` pin a semver of an `agent_versions` row with this `pathway_id`; live personas whose production
>    version `config.pathway_id` is the pathway; live widgets with this `pathway_id`. Items are
>    `{type: phone_number|agent|persona|widget, id, name}` (`name` = E.164 for numbers), in that type order.
>    `pathway.delete` records `{force, references: <count>}`.

## 4. Versions

### GET `/v1/pathway/{id}/versions`
```json
{"status":"success","versions":[{"version_number":5,"name":null,"status":"draft","is_production":false,
 "is_staging":false,"graph_hash":"…","published_at":null,"published_by":null,"created_at":"…"}]}
```
Ordered by version_number desc.

### POST `/v1/pathway/{id}/version`
Request `{"name": "v4 – new greeting"}`. Freezes draft as saved version (status `archived` meaning "saved,
unpublished"), creates new draft `latest_version+1`. Response `{"status":"success","saved_version":4,"draft_version":5}`.

### POST `/v1/pathway/{id}/publish`
Request:
```json
{"version_number": 4, "environment": "production", "run_regression": true}
```
`version_number` omitted = current draft. Steps:
1. Load version; run validation in publish mode → 400 on errors.
2. If regression enabled (request flag or suite setting `run_on_publish`) → run unit tests synchronously when
   ≤ 20 tests (timeout 120 s) else start an async run; if `block_publish_on_failure=true` and synchronous run fails
   → 409 `CONFLICT` with `details.run_id`; async case publishes only when the run passes (response status
   `"pending_regression"` with `run_id`; publish completes in the worker).
3. In one transaction: mark version `published`, set `published_at/by`, set `production_version` or
   `staging_version`; if the published version was the draft, create new draft `latest_version+1` copied from it.
4. Audit `pathway.publish`; invalidate Redis cache `pathway:{id}:v:{env}`.
Response: `{"status":"success","published_version":4,"environment":"production","draft_version":5}`.

### POST `/v1/pathway/{id}/promote`
Requires `staging_version` set. Sets `production_version = staging_version`. Response
`{"status":"success","production_version":4}`.

### POST `/v1/pathway/{id}/revert`
Request `{"version_number": 2}` → draft content replaced with v2 content (draft number unchanged).

### POST `/v1/pathway/{id}/duplicate`
Request `{"name": "Copy of …", "version": "production"}` → new pathway with draft v1. Response like create.

> Decision (2026-11-03): pathway versions (P07-T09b).
> 1. Permissions (supersedes P07-T09a item 1 for publishing): the versions list and `GET
>    /v1/pathway/{id}/version/{version_number}` need `pathways:read`; saving a version needs `pathways:write` and an
>    editing role; publish, promote, and revert need `pathways:write` and, for session principals, the
>    `pathways.publish` permission (specs/04 §3.2 "Pathways: publish / promote production"; otherwise 403
>    `FORBIDDEN` "Your role cannot publish pathways."). API keys pass on `pathways:write` (specs/04 §2.3 lists
>    publish/promote under it); the §10 `pathways:publish` name is not a key scope.
> 2. Invariant: exactly one `draft` row per pathway and `draft_version == latest_version` after every operation;
>    every write takes the pathway row lock first, so concurrent saves serialise.
> 3. Save version: body `{name?}` (1-200 characters, trimmed). The draft becomes `archived` with the name, and a new
>    draft `latest_version + 1` copies its graph (name NULL).
> 4. Publish: an unknown `version_number` is 404 `PATHWAY_VERSION_NOT_FOUND` (field `version_number`); publish-mode
>    errors reject with one `INVALID_INPUT` item each (as draft writes do). `published_at`/`published_by` are set on
>    the first publish of a version only (a later publish to the other environment moves only the pin);
>    `published_by` is the session user or the key's creating user (NULL for keys without one). Publishing the draft
>    opens a new draft; publishing a saved version opens none. `run_regression` and the suite setting
>    `run_on_publish` are accepted; regression gating (step 2, `pending_regression`) is wired with the unit tests in
>    P07-T11, until then no tests run. Every publish writes a `pathway.publish` audit row (before/after
>    `{environment, version}`).
> 5. Promote: no body; without `staging_version` 409 `CONFLICT` "The pathway has no staging version." A promote
>    that does not move production succeeds without an audit row or cache invalidation.
> 6. Revert: body `{version_number}` required; unknown → 404 `PATHWAY_VERSION_NOT_FOUND` (field `version_number`).
>    The draft keeps its number and name, takes a copy of the version's graph, and is never rejected for
>    validation issues (stored versions passed save mode); the response is
>    `{"status":"success","draft_version":d,"graph_hash":h,"validation":{...}}` with the save-mode report.
>    Reverting to an identical graph (or the draft itself) writes no audit row.
> 7. `GET /v1/pathway/{id}/version/{version_number}` returns the same body as `GET /v1/pathway/{id}?version=n`.
> 8. Cache: `va_db.pathway_cache` builds `pathway:{id}:v:{env}` (`production` | `staging`). After the transaction
>    commits, publish deletes its environment's key and a moving promote deletes `production`; Redis failures are
>    logged (`pathway_cache_unavailable`) and never fail the request. Only pinned versions may be cached: a draft
>    fallback (no production pin, §1) is never written to these keys.
> 9. Audit action names: `pathway.version_save` (the §11 `pathway.version.save`, renamed to the two-part form of
>    the specs/04 §13 list) and `pathway.revert`.

## 5. Import / export

- Export: `GET /v1/pathway/{id}/export?version=` →
  `{"format":"voiceagents.pathway.v1","name","description","nodes","edges","global_config","exported_at"}`.
- Import: `POST /v1/pathway/import` body either that format or a Bland export (`{"name","description","nodes","edges"}`
  with Bland node types). Node type aliases and `transferNumber` normalization per `11-pathways-engine.md` §3.3.
  Tool/KB references that don't exist in the importing org are kept and reported in
  `warnings` (`PATHWAY_TOOL_MISSING`/`PATHWAY_KB_MISSING`). Response like create plus `validation`.

## 6. Folders

`pathway_folders` CRUD. POST body `{name, parent_id?}`; max depth 5; deleting a folder moves its pathways to the
parent (or root). Move: `{"pathway_ids":[…]}`.

> Decision (2026-11-03): pathway folders, duplicate, import/export (P07-T09c).
> 1. Routes: the folder routes (`/v1/pathway/folders…`) and the import/export/duplicate routes live on routers that
>    `va_api.main.ROUTERS` lists before the pathways router, so the literal `folders` and `import` segments are never
>    captured by the uuid-typed `/{pathway_id}` routes. Export and the folder reads need `pathways:read`; import,
>    duplicate, and folder writes need `pathways:write` and an editing role (§10).
> 2. Duplicate: body `{name?, version?}`; `name` is 1-200 characters (trimmed), default `"Copy of " + source name` cut
>    to 200 characters; `version` is a positive integer or `draft` (default) / `production` / `staging`; an unset pin
>    or unknown number is 404 `PATHWAY_VERSION_NOT_FOUND` (field `version`). The copy keeps the description and the
>    folder and gets draft v1 holding the chosen version's graph (save-mode errors still reject). Response like
>    create. Audit `pathway.duplicate` on the new pathway (after: the create summary plus `source_pathway_id`,
>    `source_version`).
> 3. Export: `GET /v1/pathway/{id}/export?version=` (default draft; same selector and errors as `GET`). `exported_at`
>    is the request time (ISO-8601 UTC, `Z`). No audit row.
> 4. Import: body `{format?, name?, description?, nodes, edges, global_config?, folder_id?}`; other keys
>    (`exported_at`, Bland fields) are ignored and Bland's `globalConfig` is accepted for `global_config`. A present
>    `format` must be `voiceagents.pathway.v1` (else 400 `INVALID_INPUT`, field `format`); absent = a Bland export.
>    `name` defaults to `Imported pathway`. Save-mode errors reject (one item each); missing tools/KBs stay in the
>    graph. Response `{status, pathway_id, version_number, validation, warnings}`, `warnings` = the validation
>    warnings with code `PATHWAY_TOOL_MISSING`/`PATHWAY_KB_MISSING`. Audit `pathway.import` (after: the create
>    summary plus `format`: `voiceagents.pathway.v1` | `bland`). An exported graph imports with the same
>    `graph_hash`.
> 5. Folders: depth = the number of folders on the path from the root (a root folder has depth 1), at most 5. Every
>    structural write (create with a parent, a parent change, delete) first locks all folder rows of the org
>    (`SELECT … FOR UPDATE`, id order) and checks the tree, so concurrent moves cannot build a cycle or exceed the
>    depth. Errors: unknown folder in the path → 404 `NOT_FOUND` "Folder not found."; unknown `parent_id` → 404
>    `NOT_FOUND` (field `parent_id`); too deep → 400 `INVALID_INPUT` "Folders nest at most 5 levels deep." (field
>    `parent_id`); a move into itself or a sub-folder → 400 `INVALID_INPUT` "A folder cannot move into itself or one
>    of its sub-folders." (field `parent_id`).
>    - `GET /v1/pathway/folders` → `{status, folders}` ordered by name, id (no pagination); a folder is
>      `{folder_id, name, parent_id, created_at, updated_at}`. `GET /folders/{id}` → `{status, folder}`.
>    - `POST /folders` `{name, parent_id?}` and `PATCH /folders/{id}` `{name?, parent_id?}` (`parent_id: null` = root;
>      only sent fields apply; a no-op writes no audit row) → `{status, folder}`.
>    - `DELETE /folders/{id}`: its pathways (soft-deleted ones included) and direct sub-folders move to its parent
>      (or the root), then the row is deleted, so the DB cascade never removes nested folders →
>      `{status, message: "Folder deleted.", moved_pathways, moved_folders}`.
>    - `POST /folders/{id}/move` `{pathway_ids}` (at most 100, unique) sets the folder of those live pathways; an id
>      that is not a live pathway of the org → 404 `NOT_FOUND` "Pathway not found." (field `pathway_ids[i]`) and
>      nothing moves. → `{status, moved}` (rows whose folder changed); an empty list is a no-op.
> 6. Audit action names (two-part form of the specs/04 §13 list): `pathway.import`, `pathway.duplicate`,
>    `pathway_folder.create`, `pathway_folder.update`, `pathway_folder.delete`, `pathway_folder.move` (the §11
>    `pathway.folder.*`).

## 7. Test chat (text simulation of a pathway)

### POST `/v1/pathway/chat/create`
```json
{"pathway_id":"…","version":"draft","request_data":{"first_name":"Mike"},"start_node_id":null,
 "simulate_from":"+14155550123","simulate_to":"+14155550100"}
```
Creates an ephemeral engine session stored in Redis `pathway_chat:{chat_id}` (EngineState JSON + config, TTL 24 h).
Tool/webhook/custom code nodes execute for real (they are customer endpoints); SMS, transfer, end call, press
button are **simulated** (recorded as actions, not executed). Response:
```json
{"status":"success","chat_id":"…","assistant_messages":["Hi! Thanks for calling Bella Pasta…"],"current_node_id":"start"}
```
(Initial assistant message is the start node's opening response; empty if start node waits for user first.)

### POST `/v1/pathway/chat/{chat_id}`
Request `{"message": "I'd like to order a pizza"}`. Response:
```json
{"status":"success","assistant_messages":["Great! What size?"],"current_node_id":"node_order",
 "variables":{"first_name":"Mike"},"actions":[],"ended":false,
 "logs":[{"type":"route","from":"start","to":"node_order","label":"Wants to order","alternatives":[{"label":"Hours","score":1}]},
         {"type":"extraction","variables":{}},{"type":"loop_condition","score":null}]}
```
`logs` items use the same structures as `pathway_logs.decision` (11 §12) plus `tool` entries with request and
response excerpts (secret headers masked).

Branching: `POST /v1/pathway/chat/{chat_id}` with `{"message": "...", "branch_from_turn": 3}` forks state at turn 3
into a new chat id (returned as `chat_id` in response) — the "conversation branching" feature.

Voice test calls from the dashboard use the widget voice transport (`22-web-widget.md`) with an internal
widget-less session (`POST /internal/pathway/{id}/voice_test` returns a WS token) — no PSTN cost.

## 8. Unit tests

Model `pathway_unit_tests` (03). Create body:
```json
{"name":"Routes to order","node_id":"start","history":[{"role":"assistant","content":"Hi, Bella Pasta."}],
 "user_message":"Can I get a large pepperoni?",
 "expected":{"route_to_node_id":"node_order","variables":{"pizza_size":"large"},
             "grading_goal":"The agent should acknowledge the order and ask about toppings or delivery."},
 "simulated_variations":5}
```
`POST /tests/from_call` body `{"call_id","turn_index","name"}` builds history from `call_turns` before that user
turn and node from the turn's `node_id`.

Run (`POST /tests/run` `{"version":"draft","test_ids":null}`) creates `pathway_test_runs(kind='unit')`, executed by
`PathwayTestWorkflow` (queue `va-postcall`) in parallel (concurrency 8). Per test:
1. Build EngineState at `node_id` with history; run one engine turn with `user_message` (tools executed for real
   unless test sets `mock_tools: {"TL-…": {"status":200,"body":{…}}}` in `expected`).
2. Assertions: route (`route_to_node_id` equals resulting current node), variables (subset equality, case-insensitive
   for strings), grading goal (analysis model, forced function `grade` → `{"pass": bool, "rationale": str}`; prompt:
   `Given the conversation and the agent's reply, does the reply satisfy this goal? Goal: "{goal}". Be strict.`).
3. Variations: analysis model generates N paraphrases of `user_message` (prompt: `Rewrite the caller message in
   {n} different natural ways a real caller might say it on the phone. Keep the meaning identical. Return JSON
   list of strings.`); each variation repeats steps 1–2. Test passes only when all pass.
Result item:
```json
{"test_id":"…","name":"…","passed":false,"cases":[{"input":"…","passed":true,"route":"node_order",
 "variables":{…},"reply":"…","grading":{"pass":true,"rationale":"…"}}],"duration_ms":4120}
```

## 9. Simulations and regression

`POST /v1/pathway/{id}/simulations`:
```json
{"version":"draft","scenarios":[{"name":"Impatient caller","persona":"You are an impatient customer who wants a large pizza delivered fast and interrupts often.",
  "goal":"Order placed with address captured","max_turns":20,"request_data":{}}],"runs_per_scenario":3}
```
Each run: caller LLM (analysis model, system prompt = scenario persona + `Reply only with what you would say on the
phone. Say "[HANGUP]" when you would hang up.`) converses with the engine in text mode up to `max_turns`; then the
grader checks `goal` against the transcript + final variables. Results stored in `pathway_test_runs(kind='simulation')`
with full transcripts.

Regression settings (`GET/PUT /regression`), stored in `pathways` draft `global_config.regression`:
`{"run_on_publish": true, "block_publish_on_failure": true, "include_simulations": false}`.

## 10. Permissions

| Action | owner | admin | developer | analyst | billing | viewer |
|---|---|---|---|---|---|---|
| list/get/export/versions/runs | ✓ | ✓ | ✓ | ✓ | – | ✓ |
| create/update/import/duplicate/folders/tests/chat/simulations | ✓ | ✓ | ✓ | – | – | – |
| publish/promote/revert | ✓ | ✓ | ✓ | – | – | – |
| delete / force delete | ✓ | ✓ | delete only | – | – | – |
API keys: scope `pathways:read`, `pathways:write`, `pathways:publish`, or `*`.

## 11. Audit log entries
`pathway.create`, `pathway.update` (changes: name/description + `graph_hash` before/after, not full graph),
`pathway.delete`, `pathway.version_save` (Decision P07-T09b item 9), `pathway.publish` (version, environment), `pathway.promote`,
`pathway.revert`, `pathway.import`, `pathway.duplicate`, `pathway.folder.*` (named `pathway_folder.*`, Decision
P07-T09c item 6), `pathway.test.*`.

## 12. Tests required
- CRUD happy paths and tenant isolation (other org → 404) for every endpoint.
- Version lifecycle: create → edit → save version → publish production → publish staging → promote → revert; draft
  numbering invariants (exactly one draft always).
- Optimistic concurrency conflict.
- Call version resolution matrix (omitted/0/production/staging/int existing/int missing with fallback on/off/no prod).
- Delete reference checks and force.
- Import of Bland fixture with aliases; export → import round trip equality (graph hash).
- Test chat: tools executed, transfer/sms simulated, branching.
- Unit tests: route/variables/grading/variations with scripted fake LLM; from_call construction.
- Publish blocked by failing regression; async regression completes publish.
- Permissions matrix parametrized.

## 13. Acceptance criteria
- Dashboard editor can complete create→edit→test chat→publish entirely through these endpoints.
- Regression gating demonstrably blocks a broken publish in an e2e test.
