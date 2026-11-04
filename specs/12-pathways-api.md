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

> Decision (2026-11-04): asynchronous regression on publish (P07-T11e2). Supersedes P07-T09b item 4's "no tests
> run"; P07-T11e3 adds the synchronous path (≤ 20 unit tests, no simulations, 409) in front of this one.
> 1. Regression runs when the request sets `run_regression: true` or the draft's suite settings (§9) have
>    `run_on_publish`; an explicit `false` never skips a suite that opted in. The suite settings are always read
>    from the current draft's `global_config.regression`, whichever version is published.
> 2. Step 1 validation comes first (400 before any run). The suite is every unit test of the pathway, oldest first,
>    plus, with `include_simulations`, one conversation per scenario (`simulation_id` `"<i>-1"`). More than 500
>    tests → 400 `INVALID_INPUT` field `run_regression` "The regression suite holds more than 500 unit tests.". An
>    empty suite publishes at once without a run.
> 3. `block_publish_on_failure: true`: one transaction inserts a `pathway_test_runs` row `kind: "regression"`,
>    `status: "queued"`, `version_number` = the version to publish, and `publish` = `{block: true, environment,
>    version_number, graph_hash, outcome: "pending"}`; after the commit `PathwayTestWorkflow` starts with the
>    publish intent (environment, version, the validated `graph_hash`, `published_by`, and the audit actor; the
>    actor travels only in the workflow input, never in the row). Response 200 `{"status":"pending_regression",
>    "published_version":null,"environment","version_number","draft_version","run_id"}`; nothing is published or
>    invalidated. A failed start marks the run `error` and answers 503 `SERVICE_UNAVAILABLE` (nothing published).
> 4. `block_publish_on_failure: false`: the version is published now (step 3) and the regression run (its
>    `version_number` = the published version, `publish` = `{block: false, ..., outcome: "published",
>    published_version, draft_version}`) is inserted in the same transaction; the workflow starts without a
>    publish intent and only reports. A failed start marks the run `error`, is logged, and the response is still
>    200 `success` (the publish committed).
> 5. Every publish response carries `status` (`success` | `pending_regression`), `published_version` (null while
>    pending), `environment`, `version_number` (published, or to publish), `draft_version`, and `run_id` (the
>    regression run, null without one). Run reads (`GET .../runs`, `.../runs/{run_id}`) add `publish` (null for
>    runs without one).
> 6. After `finish_pathway_test_run` returned a final status, a blocking run executes activity
>    `settle_regression_publish` (bookkeeping timeout and retries). In one transaction it locks the run row; a
>    `publish.outcome` other than `pending` is returned unchanged (a retry never publishes twice), a run still
>    queued/running stays `pending`. Otherwise: status other than `passed` → `blocked`; pathway gone or
>    soft-deleted, or the version missing → `version_unavailable`; the version's `graph_hash` differs from the
>    validated one (the draft was edited during the run) → `stale`; else step 3 runs with the requester as
>    `published_by` and audit actor → `published` (+ `published_version`, `draft_version`). The outcome and
>    `settled_at` are written into `publish` in the same transaction; after a publish the worker deletes
>    `pathway:{id}:v:{env}`. No re-validation in the worker (the graph is hash-identical to the validated one).
>    A run that never began, whose finish found the row gone, or whose settle failed after its retries (workflow
>    result `publish: "unsettled"`) keeps `outcome: "pending"`. Concurrent pending publishes settle
>    independently; the last one to commit holds the pin.
> 7. Storage: `pathway_test_runs.publish jsonb` (migration 0008, appended last; NULL for runs of other kinds).

> Decision (2026-11-04): synchronous regression gate (P07-T11e3). Refines step 2 and Decision P07-T11e2.
> 1. The synchronous path applies only to a blocking suite (`block_publish_on_failure: true`) with no simulation
>    jobs and 1-20 unit tests. Blocking suites with simulations or more than 20 tests keep the P07-T11e2 async path
>    (`pending_regression`); a non-blocking suite keeps publish-now plus an async reporting run (running it in the
>    request would only delay a publish that happens anyway).
> 2. The publish transaction (pathway row lock, publish-mode validation first) inserts the `regression` run with
>    `publish` = `{block: true, ..., outcome: "pending"}` and moves it to `running` (`started_at` set) in the same
>    transaction. No workflow starts.
> 3. After the commit the api runs the tests in the request, at most 8 at once, oldest first, each through the same
>    runner as the worker (`va_pathways.runs.run_stored_test`; no database session is held while a test runs) and
>    stores each result item in the run row. An exception from one test becomes an `error` item `runner_error`
>    (logged; no retries in the request). The whole suite is capped at 120 s of the injected clock; unfinished
>    tests are cancelled and stored as `error` items `regression_timeout`. Then the run is finished (results in
>    test order, status per §8).
> 4. The publish settles exactly as in the worker (`va_db.pathway_publish.settle_regression_publish`, same intent):
>    `published` answers 200 `{"status":"success","published_version","environment","version_number",
>    "draft_version","run_id"}` and the environment's cache key is deleted. Any other outcome answers 409
>    `CONFLICT` with `details` `{"run_id","outcome"}` and nothing is published or invalidated; messages: `blocked`
>    "The regression run did not pass; the version was not published.", `stale` (draft edited during the run)
>    "The pathway changed during the regression run; publish again.", `version_unavailable` (pathway or version
>    gone; also a run row that disappeared) "The pathway version is no longer available.".
> 5. A request cancelled mid-run (client disconnect) still closes the run in a shielded step: unfinished tests
>    are cancelled, the run is finished (missing items count as errors) and its publish settles `blocked`; a run
>    of this path is never left `running` with `outcome: "pending"`.
> 6. Idempotency is unchanged (specs/04 §2.1): the 200 and the 409 are both stored and replayed for the same
>    `Idempotency-Key` (publishing again after a fix needs a new key). The claim lock is 60 s, shorter than the
>    120 s cap: a retry with the same key after 60 s while the first request still runs starts a second run;
>    both settle independently under the pathway row lock (the last to publish holds the pin, as in P07-T11e2).

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

> Decision (2026-11-03): pathway test chat (P07-T10).
> 1. Routes live on their own router (prefix `/v1/pathway/chat`, legacy bodies, tag Pathways) listed before the
>    pathways router: `POST /create` (`pathways_chat_create`), `POST /{chat_id}` (`pathways_chat_send`), and
>    `DELETE /{chat_id}` (`pathways_chat_reset`: deletes the chat, answers `{status, message: "Chat deleted."}`;
>    a new chat is the reset). All three need `pathways:write` plus an editing role for session principals
>    (analysts and viewers get 403) and are audit-exempt: nothing persists beyond Redis.
> 2. Create body: `pathway_id` (required), `version` (number, `draft` default, `production`, `staging`; errors as
>    `GET /v1/pathway/{id}`), `request_data` (object, at most 64 KiB of JSON), `start_node_id` (must be a node of
>    the graph, else 400 `INVALID_INPUT` field `start_node_id`; the chat starts there with one visit counted),
>    `simulate_from`/`simulate_to` (any parseable number, stored as E.164; defaults `+14155550123` and
>    `+14155550100`; invalid → 400 `INVALID_INPUT` naming the field). An absent or other-org pathway is 404
>    `NOT_FOUND`. The chat freezes the graph of the resolved version; later draft edits do not change it.
> 3. Session = Redis string `pathway_chat:{chat_id}` (specs/03): `{org_id, pathway_id, version, graph, request_data,
>    simulate_from, simulate_to, created_at, state, snapshots}` with TTL 24 h refreshed on every write.
>    `snapshots` maps a turn index to the engine state after that turn (0 = after the opening); only the newest 50
>    are kept. A chat of another org, an expired one, or an unknown id is 404 `NOT_FOUND` "Chat not found.".
>    Each send holds `pathway_chat:{chat_id}:lock` (`SET NX EX 120`, released by compare-and-delete); while it is
>    held another send is 409 `CONFLICT` "This chat is answering another message.". Redis failures are 503
>    `SERVICE_UNAVAILABLE`.
> 4. Send body `{message: 1..4000 chars, stripped, non-blank; branch_from_turn: int ≥ 0 | null}`. A chat whose state
>    has ended is 409 `CONFLICT` "This chat has ended.". Branching copies the snapshot of that turn (absent → 400
>    `INVALID_INPUT` field `branch_from_turn`) and the snapshots up to it into a new chat id with a fresh TTL; the
>    message runs there, the response's `chat_id` is the new id, and the original chat is unchanged. Without
>    branching `chat_id` is the chat's own id. The user turn's index is the state's `turn_index + 1`, its time the
>    milliseconds since the chat was created.
> 5. Every request builds a `PathwayEngine` on the stored state on channel `voice` (the chat simulates a call, so
>    transfers and press button are surfaced instead of the text-channel degradations) and runs it to the end,
>    capped at 60 s; over the cap the response carries what was produced, `ended` false, the state as the engine
>    left it, and the api logs `pathway_chat.turn_timeout`. Speech (`Say`, the full text of a `SayStream`) becomes
>    `assistant_messages`; other actions become `actions`: `wait {seconds}`, `transfer {mode, target,
>    caller_id_mode}`, `end_call {reason, ended_by}`, `human_takeover`, `press_button {digits, duration_ms}`,
>    `switch_pathway {pathway_id, version, node_id}`, and `sms {node_id, to, from, body, message_id}` from the
>    simulated SMS port (never sent; `message_id` `sim-<n>`, reported as sent). Transfer, end call and human
>    takeover end the chat.
> 6. Both responses are `{status, chat_id, assistant_messages, current_node_id, variables, actions, ended, logs}`
>    (`variables` = the engine state's variables). `logs` are the turn's events in emission order: `route {from,
>    to, label, alternatives, global}`, `extraction {node_id, variables}`, `loop_condition {node_id, score, met}`,
>    `webhook {node_id, node_name, method, url, status, ms, outcome}` (URL without credentials or query), `sms
>    {node_id, sent}`, and one `tool {method, url, request_headers, request_body, status, response_body, ms,
>    outcome}` per HTTP request, before the event of the node that made it; secret-named headers
>    (`va_common.masking.is_secret_key`: `Authorization`, `*-Key`, `Cookie`, …) read `***`, bodies are JSON
>    excerpts of at most 2 000 characters (`…` marks a cut). Other event types are not shown.
> 7. Webhook and scheduling nodes call the customer's endpoint for real through `va_tools.http.HttpExecutor` over
>    the api's guarded tools client (`X-VA-Call-Id` = the chat id; `http`/private URLs only in `local`/`test`).
>    Forwarding and Transfer Pathway nodes load other pathways of the org (`production`/`staging` pins or a
>    number). Decisions, replies and final lines use the api's LLM gateway (platform provider `LLM_*` plus org
>    provider overrides) with the org's `llm.context_budget_tokens` and `engine.speculative`; built-in variables
>    are those of an inbound call from `simulate_from` to `simulate_to` in UTC (`call_id` = the chat id, language
>    `en-US`, no voice). Test-chat LLM usage is not metered (a builder tool, like validation), and its gateway
>    metrics are not exported by the api.

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

> Decision (2026-11-03): unit-test case runner (P07-T11a).
> 1. Execution lives in `packages/va_pathways` (shared by the api and the worker): `va_pathways.unit_tests.run_unit_test`
>    over `va_pathways.conversation.TextConversation` (the test chat's engine run, also used by §7). One case = an
>    EngineState at `node_id` (`visit_counts {node_id: 1}`, `turn_index` = number of `user` history messages) whose
>    `history` holds the test's messages on that node (`at_ms` 0, 1, 2, …), `request_data` = optional
>    `expected.request_data` (object, default `{}`), then ONE `handle_user_turn` with the case input, consumed like
>    the test chat (SMS, transfer, end call, press button simulated; webhooks real unless mocked), capped at 60 s.
> 2. `expected.mock_tools` keys match the request source: the Webhook node id in this phase (`TL-…` tool ids from
>    phase 08 use the same `HttpCall.source_id`). Value `{status (100-599, default 200), body (any JSON, default
>    null)}`; 2xx → `ok`, else `http_error`; the request's JSONPath `extract` reads the mocked body; the `tool` log
>    item carries `mocked: true`. Unmocked requests go to the real executor.
> 3. Assertions: `route` holds when `route_to_node_id` is absent or equals `current_node_id` after the turn;
>    `variables` holds when every expected key exists and matches (strings equal after strip + casefold, objects
>    recursively by the same subset rule, lists item by item, other values by equality, a boolean never equals a
>    number); `grading` only with `grading_goal`: analysis model, forced `grade` → `{"pass", "rationale"}`,
>    temperature 0, a `Conversation:` message (history, case input, reply as `Caller:`/`Agent:` lines) followed by
>    the prompt above with the goal substituted literally.
> 4. Nothing raises. A case that cannot be judged fails with `error`: `invalid_history` (a role other than
>    `user`/`assistant`), `unknown_node` (`node_id` not in the version), `engine_error`, `turn_timeout`,
>    `grading_unavailable` (route and variables are still reported). The result adds `status`: `failed` when any
>    judged assertion did not hold, else `error` when any case (or the variations call) errored, else `passed`;
>    `passed` = `status == "passed"`.
> 5. Variations: one analysis completion with the prompt above (`{n}` substituted) as the system message and the
>    caller message as the user message; the first JSON array in the answer is read; only non-blank strings count,
>    stripped and deduplicated case-insensitively, the original excluded, the first n kept. Fewer than n →
>    `variations_short: <missing>` on the result; an unavailable model → no variation cases and
>    `error: "variations_unavailable"` on the result. The original message is always case 0; cases run
>    sequentially, tests concurrently (the run caps at 8).
> 6. Case items add `actions` (simulated actions, chat shape) and `error` when set; `grading` is `null` without a
>    goal. Result items add `status`, plus `error` and `variations_short` when set.

> Decision (2026-11-04): unit test CRUD and runs read API (P07-T11b).
> 1. Permissions: reads (`GET /tests`, `/tests/{test_id}`, `/runs`, `/runs/{run_id}`) need `pathways:read`; test
>    writes (create, patch, delete, `from_call`) need `pathways:write` with no editing-role guard: specs/04 §3.2 row
>    "Pathway tests / simulations" gives analysts access (and is authoritative over the "tests" in §10's second
>    row). Billing has no access.
> 2. Audit actions `pathway_test.create` (also `from_call`, with `source_call_id`), `pathway_test.update`,
>    `pathway_test.delete` (the §11 `pathway.test.*`, two-part like `pathway_folder.*`). Rows carry a summary
>    (`name`, `node_id`, `history_messages`, `expected_keys`, `simulated_variations`, `source_call_id`), never the
>    history, caller message, or `expected` values (mocked tool bodies may hold secrets); updates add `fields`
>    (the changed columns). A patch that changes nothing writes no row.
> 3. Every route first loads the live pathway (404 "Pathway not found."); a test or run of another pathway of the
>    same org is 404 ("Pathway test not found." / "Pathway test run not found.") like another org's.
> 4. Body rules: `name` 1-200 trimmed; `node_id` 1-200 (not checked against any version: runs report
>    `unknown_node`); `history` at most 200 items `{role: user|assistant, content ≤ 4 000}`; `user_message` 1-4 000
>    after trimming; `expected` keys `route_to_node_id` (≤ 200), `variables`, `grading_goal` (≤ 2 000),
>    `request_data`, `mock_tools` (Decision P07-T11a items 1-2), unknown keys rejected, stored exactly as sent;
>    `simulated_variations` 0-20, default 5. Violations are 400 `INVALID_INPUT` with the field path.
> 5. PATCH sets only sent, non-null fields (`expected` replaces the whole object). DELETE removes the row (the table
>    has no `deleted_at`) and answers `{"status":"success","message":"Test deleted."}`.
> 6. Lists are newest first with `limit` (1-100, default 50) and `cursor` (`next_cursor`); runs also filter by
>    `kind` (`unit`|`simulation`|`regression`). Run list items omit `results` and carry `summary {total, passed,
>    failed, error}` counted from `results[*].status`; the run detail adds `results`.
> 7. `from_call` body `{call_id, turn_index (≥ 0), name, expected?, simulated_variations?}`: an unknown call is 404
>    `CALL_NOT_FOUND`; a call that did not run this pathway is 400 `INVALID_INPUT` field `call_id` ("The call did
>    not run this pathway."). The tested turn is the `user` row with that `turn_index` (indices are shared by both
>    roles), else 400 field `turn_index` ("The call has no caller turn with this index."). `history` = every
>    earlier turn as `{role, content}` (assistant content = `text_spoken` when recorded: what the caller heard);
>    `node_id` = the turn's `node_id`, else the latest earlier turn's, else 400 field `turn_index` ("The turn has no
>    pathway node."); `expected` = the body's, else `{"route_to_node_id": <node of the first later assistant turn
>    with one>}`, else `{}`; `source_call_id` = the call.

> Decision (2026-11-04): unit test runs (P07-T11c).
> 1. Body `{"version": "draft"|"production"|"staging"|int ≥ 1 (default "draft"), "test_ids": [uuid] | null}`
>    (other keys rejected). The version resolves at request time to a number like every version selector (unset
>    pin / missing number → 404 `PATHWAY_VERSION_NOT_FOUND` field `version`); the run stores `version_number` and
>    every test loads that number when it starts.
> 2. `test_ids` null → every test of the pathway, oldest first (`created_at, id`); a list → those tests in order,
>    duplicates dropped, at most 500 (pydantic `max_length`). An id that is not a test of this pathway → 404
>    "Pathway test not found.". No tests (empty list, or a pathway without tests) → 400 `INVALID_INPUT` field
>    `test_ids` "There are no unit tests to run."; more than 500 stored tests without ids → 400 field `test_ids`
>    "Select at most 500 tests.". Permission `pathways:write` with no editing-role guard (analysts run tests, like
>    Decision P07-T11b item 1); no audit row (the run row is the record). `Idempotency-Key` replays the same run.
> 3. The request inserts `pathway_test_runs(kind='unit', status='queued', results=[])`, commits, then starts
>    `PathwayTestWorkflow` id `pathway-test-run-<run_id>` on the post-call queue (`REJECT_DUPLICATE`; an existing
>    workflow counts as started). A failed start sets the run `error` with `finished_at` and answers 503
>    `SERVICE_UNAVAILABLE` "Test runs are unavailable; try again."; success answers 200
>    `{"status":"success","run":<run detail>}` (status `queued`).
> 4. Workflow: `begin_pathway_test_run` (queued → running + `started_at`; a run that is gone or finished ends the
>    workflow), one `run_pathway_unit_test` activity per test with at most 8 in flight, then
>    `finish_pathway_test_run`. Each test activity writes its result item into `results` itself (row locked
>    `FOR UPDATE`, an item of the same `test_id` replaced, so a retry never duplicates) and returns only the
>    status. The finish orders `results` by the run's test order and sets `status` (`failed` when any item
>    failed, else `error` when any item errored or a test has no item, else `passed`) and `finished_at`.
> 5. Per-test problems are items, never workflow failures: `{"test_id", "name", "passed": false, "status":
>    "error", "cases": [], "duration_ms": 0, "error": …}` with `test_not_found` (deleted since the run started;
>    `name` null), `version_unavailable` (pathway deleted or version no longer loads), `invalid_test` (the stored
>    row no longer validates), or `runner_error` (the activity failed after its retries; recorded by
>    `record_pathway_test_error`, which keeps an item an earlier attempt already stored). Timeouts: test activity
>    30 min start-to-close, 3 attempts (5 s initial, ×2); bookkeeping activities 30 s, 5 attempts.
> 6. The worker builds the text-run dependencies like the api test chat: the `LLM_*` platform gateway with org
>    providers (`va_llm.platform.platform_gateway`; `va_llm_*` metrics on the worker's probe registry) and the
>    guarded tool HTTP client (`WEBHOOK_ALLOW_PRIVATE`); `http://` webhook URLs run only in `local`/`test`.

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

> Decision (2026-11-04): simulations (P07-T11d).
> 1. Body `{"version": "draft"|"production"|"staging"|int ≥ 1 (default "draft"), "scenarios": [1-20 items],
>    "runs_per_scenario": 1-10 (default 1)}`, other keys rejected. Scenario `{name 1-200, persona 1-4 000, goal
>    1-2 000 (all three trimmed), max_turns 1-50 (default 20), request_data object (default {})}`, other keys
>    rejected; violations are 400 `INVALID_INPUT` with the field path (`scenarios[0].max_turns`). The version
>    resolves at request time like unit runs (Decision P07-T11c item 1). Permission `pathways:write` with no
>    editing-role guard (analysts run simulations, specs/04 §3.2), no audit row, `Idempotency-Key` replays. The
>    request inserts `pathway_test_runs(kind='simulation', status='queued')`, commits, and starts
>    `PathwayTestWorkflow` exactly like a unit run (same id, queue, failure handling: run `error` + 503); answer
>    200 `{"status":"success","run":<run detail>}`.
> 2. Scenario i × run r (both 1-based) is one conversation with `simulation_id` `"<i>-<r>"`, in scenario order
>    then run order; the workflow input carries `kind: "simulation"` and the jobs (at most 200: the 4 000-char
>    personas stay far below Temporal's 2 MB payload limit). Each conversation is one `run_pathway_simulation`
>    activity (60 min start-to-close, the unit-test retry policy), at most 8 in flight, storing its own item
>    (replaced by `simulation_id`); a failure after the retries stores a `runner_error` item via
>    `record_pathway_simulation_error`. The finish orders results by job order and sets the status as for
>    unit runs.
> 3. One conversation: a fresh engine state at the start node, `request_data` = the scenario's, the simulated
>    caller/number of the test chat, and a new conversation id used as the call id of every LLM call of that
>    conversation (the caller's included). The agent speaks first (`start`); then the caller model answers
>    (call type `analysis`, temperature 0.7, max_tokens 256, system = persona + blank line + the suffix above,
>    agent lines as `user` messages — `(silence)` for an agent turn that said nothing — and its own lines as
>    `assistant`). A reply containing `[HANGUP]` (case-sensitive) ends the conversation (`ended_by: "hangup"`):
>    the text before it, trimmed, is kept as the caller's last transcript line when non-blank and is never sent
>    to the engine. Any other reply is one `handle_user_turn` (capped at 60 s like a unit-test case). The
>    conversation also ends after the agent answered caller line `max_turns` (`max_turns`) or when the agent
>    ended it (End Call, transfer: `agent_ended`).
> 4. Grading (only when the conversation ran without error): one forced `grade` call (analysis model), messages
>    = the transcript (`Caller:`/`Agent:` lines), `Final variables: <JSON, sorted keys>`, then `Given the full
>    conversation and the final variables, did the conversation achieve this goal? Goal: "{goal}". Be strict.`
>    with the goal substituted literally. Verdict pass → `passed`, fail → `failed`. Errors give `status:
>    "error"`: `engine_error`, `turn_timeout`, `caller_unavailable`, `caller_invalid` (a blank reply without
>    `[HANGUP]`) end the conversation with `ended_by: "error"`; `grading_unavailable` keeps the real `ended_by`;
>    `version_unavailable` (the version no longer loads or has no start node) and `runner_error` are items of
>    conversations that never ran.
> 5. Result item: `{"simulation_id", "scenario" (name), "run", "goal", "passed", "status", "ended_by", "turns"
>    (caller lines sent to the engine), "transcript": [{"role": "assistant"|"user", "content"}] (agent =
>    assistant, caller = user, blank agent turns omitted), "variables" (final), "grading": {pass,
>    rationale}|null, "actions" (simulated actions, chat shape), "duration_ms", "error"?}`; items of
>    conversations that never ran have `turns` 0, empty `transcript`/`variables`/`actions`, `grading` null,
>    `duration_ms` 0. The run summary counts `status` as for unit runs.

Regression settings (`GET/PUT /regression`), stored in `pathways` draft `global_config.regression`:
`{"run_on_publish": true, "block_publish_on_failure": true, "include_simulations": false}`.

> Decision (2026-11-04): regression settings (P07-T11e1).
> 1. Settings `{run_on_publish: bool (default false), block_publish_on_failure: bool (default true),
>    include_simulations: bool (default false), scenarios: [0-20 scenarios of the Decision P07-T11d item 1 shape]}`,
>    other keys rejected (400 `INVALID_INPUT` with the field path, e.g. `scenarios[0].max_turns`). The defaults make
>    an existing pathway's publish run nothing until the suite opts in (the example above is the opted-in shape).
>    With `include_simulations` the suite plays each scenario once (P07-T11e2).
> 2. Stored under the draft's `global_config.regression`: save, fork, duplicate, and export copy the settings with
>    the graph; a draft update that sends `global_config` replaces the whole object, so clients keep the key. The
>    key is part of `graph_hash`. Reads are lenient: a missing or invalid stored value reads as the defaults.
> 3. `GET /v1/pathway/{id}/regression` (`pathways_regression_get`, `pathways:read`) answers
>    `{"status":"success","regression":{all four keys}}`. `PUT` (`pathways_regression_put`, `pathways:write` plus an
>    editing role for session principals: it edits the draft) takes the full settings (omitted keys take the
>    defaults) and answers the same body plus `draft_version` and `graph_hash`. It takes the pathway row lock;
>    storing the value already stored writes nothing. A change writes audit `pathway.regression_update` with
>    before/after `{run_on_publish, block_publish_on_failure, include_simulations, scenarios: <count>,
>    graph_hash}`. An absent or other-org pathway is 404 `NOT_FOUND`.
> 4. §4 step 3 (mark `published`, pin, fork a draft when the published version was the draft, the
>    `pathway.publish` audit row) is one shared routine, `va_db.pathway_publish.apply_publish`, used by the api and
>    by the worker when an asynchronous regression run completes a publish (P07-T11e2). Validation and the
>    resolved-graph cache invalidation stay with the caller.

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
`pathway.revert`, `pathway.import`, `pathway.duplicate`, `pathway.regression_update` (Decision P07-T11e1),
`pathway.folder.*` (named `pathway_folder.*`, Decision
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
