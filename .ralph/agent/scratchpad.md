# Scratchpad — Phase 03

## Plan P03-T06b (Planner, 2026-11-01)

Context: the previous loop run died ("too many consecutive failures": Claude Code lingered after each turn and Ralph
counted SIGTERMs; ralph.yml now has `max_consecutive_failures: 50`). The P03-T06b implementation is already in the
working tree, UNCOMMITTED and UNREVIEWED. Planner check (2026-11-01): `pytest packages/va_sim/tests/llm
tests/test_state.py` 504 passed; ruff clean; mypy va_sim clean (168 files); placeholders 0; radon: no C+ blocks in
va_sim/llm. Do NOT rewrite it. Builder job = verify against the acceptance criteria, close gaps, run full gates.

Existing files (keep):
- src/va_sim/llm/heuristics.py (`STRUCTURED_TOOL_NAMES`, `structured_arguments`, in-call classifiers),
  heuristics_post.py (post-call: disposition/summary/analysis/citations/grade), heuristics_base.py (`Heuristic`),
  schema_fill.py (`SchemaFill.conform` -> always schema-valid), text.py (text helpers),
  embeddings.py (pure: blake2b signed buckets, L2 norm, float|base64, dims), embed_route.py (`POST /v1/embeddings`),
  gate.py (shared auth/model/fault answers); modified chat.py/control.py/params.py/reply.py/routes.py/store.py/state.py
- tests: test_llm_heuristics.py, test_llm_schema_fill.py, test_llm_text.py, test_llm_embeddings.py,
  support/llm_schemas.py (`SPEC_CALLS` for all 14 names, `forced_body`), test_llm_openai_sdk.py
  (`test_forced_structured_calls_return_schema_valid_arguments`, `test_embeddings_through_the_sdk`)
- specs/30 §3.4 Decision P03-T06b already written; plan P03-T14 drift-test note; AGENTS.md P03-T06b bullet;
  pyproject dev deps jsonschema + types-jsonschema (uv.lock updated).

Builder checklist:
1. Confirm `STRUCTURED_TOOL_NAMES` == the specs/10 §3 registered-name list exactly (every name, character for
   character); add a test parsing specs/10 §3 if one does not exist (check test_llm_heuristics.py first).
2. Confirm SDK acceptance test covers EVERY name in `SPEC_CALLS` and validates with
   `jsonschema.Draft202012Validator`; confirm embeddings tests assert determinism, unit norm, 1024 default,
   `dimensions` override, similar > dissimilar cosine, base64 decode == float vector, usage tokens.
3. Check specs/30 §3.4 decision text matches the code (dims default, bucket hashing, encoding formats, fault
   `endpoint` field, `/sim/state` llm `embeddings` count).
4. Run gates: `make fmt lint typecheck placeholders test` (record counts + coverage), `uv run python
   scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent` (0 clones expected), `uvx radon cc -s -a
   packages/va_sim/src/va_sim/llm`, audit (`uvx pip-audit` on `uv export` — new dep jsonschema).
   Known flake: apps/api pytest-timeout setup errors under load (fix:api-test-timeout-load); rerun module alone.
5. Tick nothing (Reviewer ticks). Emit build.done with the backpressure strings.

Risks: jscpd clones between heuristics/heuristics_post; schema_fill.py at 472 lines (fine unless radon C).

## Build P03-T06b (Builder, 2026-11-01)

Verified WIP against checklist, no code changes needed: specs/10 §3 name parse test exists
(test_llm_heuristics: 14 names == STRUCTURED_TOOL_NAMES == SPEC_CALLS); SDK test validates every SPEC_CALLS entry with
Draft202012Validator; embeddings tests cover determinism/unit-norm/1024/similarity/encodings/inputs/faults.
Gates: make fmt 0 (no diff), lint 0, typecheck 0, placeholders 0 findings, check_spec_refs 0, make test 0
(4873 passed, TOTAL coverage 98%), pip-audit strict: no known vulns, pnpm audit: 3 moderate (< high), jscpd 0 clones,
radon va_sim/llm avg A (3.26), no C+ blocks.

## Review P03-T06b (Reviewer, 2026-11-01)

Approved. Re-ran: make lint/typecheck/placeholders 0; make test 4873 passed, 98.02% coverage. Diff checked against
specs/30 §3.4 + Decision P03-T06b and plan acceptance (SDK forced call per name schema-valid; embeddings
deterministic/unit/1024/similarity). Reviewer fix in place: module docstrings cited stale Decision P03-T06b rule
numbers (decision has rules 1-6); corrected in heuristics*, schema_fill, text, embeddings, embed_route, gate, control.

## Plan P03-T07a (Planner, 2026-11-01)

P03-T07 was too big for one Builder pass, so I split it in plan/phase-03.md: T07a = core (this plan), T07b = checkout,
subscriptions, invoices, lifecycle events. plan/phase-15.md P15-T03-ish dependency now points at P03-T07b.
Spec: specs/30 §4.1, specs/24 §7. The official SDK is `stripe` 16.1.0 (checked: `StripeClient(api_key, *,
base_addresses=..., http_client=..., max_network_retries=...)`, `stripe.HTTPXClient` exists,
`stripe.Webhook.construct_event(payload, sig_header, secret, tolerance=300)`).

### Spec decision to write: specs/30 §4.1 `> Decision (2026-11-01): FakeStripe core (P03-T07a).` Number the rules:
1. Auth: `Authorization: Bearer <key>` or Basic (`<key>:` base64, how curl `-u` sends it). Accepted keys start with
   `sk_test_sim` (default) plus control-added ones. Missing/unknown → 401 `{"error":{"type":"invalid_request_error",
   "message":"Invalid API Key provided: sk_test_****<last4>"}}` (missing key: "You did not provide an API key...").
   Resources are global to the sim (no per-key isolation; Stripe accounts are not modelled).
2. Requests: POST bodies are `application/x-www-form-urlencoded` with Stripe bracket notation
   (`metadata[org_id]=…`, `line_items[0][price_data][currency]=usd`, `expand[]=…`); GET query uses the same.
   Values stay strings except the params a handler declares integer/boolean (`amount`, `confirm`, `off_session`,
   `limit`): parse "true"/"false" and base-10 ints; invalid → 400 `parameter_invalid_integer`/`parameter_invalid_boolean`
   with `param`. Unknown params → 400 `parameter_unknown` "Received unknown parameter: x" (Stripe does this; it keeps
   our provider honest). `expand` is accepted and ignored except the fields T07a can expand (none required; accept
   and ignore is fine — document it).
3. Responses: JSON objects with `id`, `object`, `created` (sim clock unix s), `livemode: false`, `metadata` ({} default).
   Every response has `Request-Id: req_<alnum14>` and echoes the request's `Stripe-Version` header when sent. Events
   carry `api_version` = the `Stripe-Version` of the request that caused them, else constant
   `va_sim.stripe.objects.DEFAULT_API_VERSION = "2025-10-29.clover"` (the sim never imports `stripe`).
   Errors: `{"error":{"type","code"?,"decline_code"?,"message","param"?,"doc_url"?,"payment_intent"?,"request_log_url"?}}`;
   404 unknown object → `resource_missing` "No such customer: 'cus_x'" param `id`; unknown URL → 404
   invalid_request_error "Unrecognized request URL (POST: /v1/x)...".
4. Idempotency: POST with `Idempotency-Key` stores `(key, method+path+sorted decoded params)` → (status, body) per API
   key; a replay returns the stored status/body with header `Idempotent-Replayed: true` and creates nothing, emits
   nothing; same key with different params → 400 `{"error":{"type":"idempotency_error","message":"Keys for idempotent
   requests can only be used with the same parameters they were first used with..."}}`. Errors are stored too
   (Stripe stores 4xx except 401/429). Cleared by reset.
5. Ids from `state.ids` (seeded): `cus_`+alnum(14), `pm_`+alnum(24), `pi_`+alnum(24), `ch_`+alnum(24),
   `bps_`+alnum(24), `evt_`+alnum(24), `req_`+alnum(14), client secret `{pi_id}_secret_`+alnum(25).
6. Payment methods: magic test ids `pm_card_visa`, `pm_card_mastercard` (succeed), `pm_card_chargeDeclined`
   (decline `card_declined`/`generic_decline`), `pm_card_chargeDeclinedInsufficientFunds` (`card_declined`/
   `insufficient_funds`), `pm_card_authenticationRequired` (off-session → `authentication_required`).
   `POST /v1/payment_methods/{magic}/attach {customer}` creates a NEW `pm_…` (type card, `card{brand,last4,exp_month,
   exp_year,funding}` from a table, `customer`), behaviour remembered on the stored pm. Attaching a stored pm to
   another customer → 400. `GET /v1/payment_methods/{id}`, `GET /v1/customers/{id}/payment_methods` (list object
   `{"object":"list","data":[...],"has_more":false,"url"}`; `limit` 1..100 default 10, newest first).
7. Customers: `POST /v1/customers` (email, name, description, phone, metadata, payment_method (attach a magic id),
   invoice_settings[default_payment_method]), `GET`/`POST /v1/customers/{id}` (update; metadata merges key-wise,
   empty string value deletes a key — Stripe semantics), default pm must belong to the customer else 400
   `resource_missing`. Object fields: id, object "customer", created, email, name, description, phone, metadata,
   invoice_settings {default_payment_method, custom_fields: null, footer: null}, balance 0, currency null,
   delinquent false, livemode false.
8. Payment intents: `POST /v1/payment_intents` (amount int ≥ 50 else `amount_too_small`; currency required, lowercase
   3 letters; customer; payment_method; confirm; off_session (only valid with confirm=true, else 400
   `parameter_invalid`… keep simple: Stripe accepts off_session only with confirm); description; metadata;
   setup_future_usage; payment_method_types[]; automatic_payment_methods[enabled]). confirm=false → status
   `requires_payment_method` (no pm) / `requires_confirmation` (pm). confirm=true: pm = given pm else the customer's
   `invoice_settings.default_payment_method`; none → 400 `parameter_missing` param `payment_method`. Outcome order:
   config `decline` (= specs `SimState.stripe_decline`) → card_error code `card_declined` decline_code from config
   (default `generic_decline`); else pm behaviour; success → status `succeeded`, `amount_received` = amount,
   `latest_charge` = new `ch_…`, emit `payment_intent.succeeded`. Failure → HTTP 402 card_error body with
   `payment_intent` (status `requires_payment_method`, `last_payment_error` = the error object), the PI is stored
   (retrievable), emit `payment_intent.payment_failed`. `authentication_required` only when off_session=true; an
   on-session confirm with that pm → status `requires_action` (`next_action {type:"use_stripe_sdk"}`), 200, no event.
   `GET /v1/payment_intents/{id}`. Object: id, object "payment_intent", amount, amount_received, currency, customer,
   payment_method, status, client_secret, confirmation_method "automatic", capture_method "automatic", description,
   metadata, latest_charge, last_payment_error, setup_future_usage, created, livemode.
9. Billing portal: `POST /v1/billing_portal/sessions` (customer required + must exist, return_url optional) →
   object "billing_portal.session" {id, customer, return_url, url `{request base}/sim/stripe/portal/{id}`, created,
   livemode, configuration "bpc_sim", locale null}. `GET /sim/stripe/portal/{id}` → 200 text/html page naming the
   customer with a link to return_url (404 sim_error when unknown). URLs use the request's base URL (scheme+host as
   the client reached the sim: `http://sim:9400` in compose).
10. Events: object "event" {id, object, api_version, created, data{object, previous_attributes?}, livemode false,
   pending_webhooks 1, request{id: req id, idempotency_key}, type}. `data.object` is a deep copy at emit time. Stored
   (log 10 000) and served `GET /v1/events/{id}` and `GET /v1/events` (limit, `type` filter, newest first).
   Delivery: POST JSON to config `webhook_url` (default `SIM_STRIPE_WEBHOOK_URL`) with headers `Content-Type:
   application/json; charset=utf-8`, `Stripe-Signature: t=<sim-clock unix s>,v1=<hex HMAC-SHA256(secret,
   f"{t}.{body}")>`; secret = config `webhook_secret` default `whsec_sim` (bytes of the full string, as Stripe).
   First attempt inline (ordering), failure retried after 1 s and 3 s on the sim clock (same event, re-signed), then
   dropped; delivery log `GET /sim/stripe/webhooks`. `webhook_url: null` (PATCH) disables delivery (events still logged).
11. Control (`/sim/stripe/*`, FakeStripe port only): `GET`/`PATCH /sim/stripe/config` {`decline`: bool,
   `decline_code`: str, `webhook_url`: str|null, `webhook_secret`: str} (strict, 422 invalid_request on unknown keys);
   `POST /sim/stripe/api-keys {"api_key"}` (must start with `sk_`) 204; `GET /sim/stripe/events` (`{"events":[...]}`
   oldest first); `POST /sim/stripe/events {"type": str, "object": {...JSON object...}}` → emits a signed event with
   that data object (for va_billing tests of `charge.refunded`, `charge.dispute.created`, ...) → 201 the event;
   `GET /sim/stripe/webhooks` (`{"deliveries":[...]}`). Reset restores config to settings defaults and drops everything.
   `/sim/state` gains `"stripe": {"customers", "payment_methods", "payment_intents", "events", "webhook_deliveries"}`.

### Files (create under packages/va_sim/src/va_sim/stripe/; keep `__init__.py` import-free like telnyx)
- `form.py` (pure): `decode_form(pairs: Sequence[tuple[str,str]]) -> dict[str, FormValue]` (nested dicts; `a[0][b]`
  and `a[]` → lists; conflicting shapes → `FormError(param)`), `FormValue = str | list[FormValue] | dict[str, FormValue]`.
- `params.py` (pure): typed readers `string(p, name)`, `integer(p, name)`, `boolean(p, name)`, `mapping(p, name)`,
  `metadata(p)`, `check_known(p, allowed: frozenset[str])` raising `StripeParamError(code, message, param)`.
- `errors.py`: `stripe_error(status, type_, message, *, code=None, param=None, decline_code=None, extra=None)`,
  `resource_missing(kind, id)`, `unrecognized_url(method, path)`, `auth_error(...)`, `card_error(...)`.
- `objects.py` (pure builders): `customer_json`, `payment_method_json`, `payment_intent_json`, `portal_json`,
  `event_json`, `list_json(items, url)`; test card table `TEST_CARDS: Mapping[str, TestCard]`.
- `signing.py` (pure): `signature_header(secret: str, timestamp: int, payload: bytes) -> str`.
- `store.py`: `StripeConfig` (dataclass: decline, decline_code, webhook_url, webhook_secret), `StripeStore(defaults)`
  with dicts customers/payment_methods/payment_intents/portal_sessions, `events: deque`, idempotency cache,
  `keys: set[str]`, `reset()`.
- `webhooks.py`: `StripeWebhookEmitter` (same retry semantics as telnyx). To avoid jscpd clones, extract the generic
  post+log+retry loop from `va_sim/telnyx/webhooks.py` into `va_sim/delivery.py` (`SignedDelivery`/`deliver(...)`
  with a headers callback and `WebhookDelivery` record) and have both emitters use it; telnyx tests must pass
  UNCHANGED (keep `va_sim.telnyx.webhooks.WebhookDelivery`/`RETRY_DELAYS_S` names importable via re-export).
- `http.py`: `stripe_endpoint(state, handler)` wrapper = auth → form decode → idempotency → handler → Request-Id /
  Stripe-Version headers; StripeParamError → 400.
- `customers.py`, `payment_methods.py`, `payment_intents.py`, `portal.py`, `events.py` (handlers);
  `control.py` (sim routes), `routes.py` `stripe_routes(state) -> list[BaseRoute]` incl. a catch-all
  `Route("/v1/{path:path}")` 404 unrecognized URL LAST.
- Modify: `va_sim/server.py` (`elif sim == "stripe": routes.extend(stripe_routes(state))`; `build_state` passes
  `stripe_defaults=settings.stripe_defaults()`), `va_sim/settings.py` (`StripeDefaults(webhook_url)` +
  `SimSettings.stripe_defaults()`), `va_sim/state.py` (`self.stripe = StripeStore(...)`, `stripe_webhooks` emitter
  sharing `webhook_transport`, reset, snapshot, docstrings), `apps/sim` only if needed (it mounts simulator_routes).
- Root `pyproject.toml` dev group: `stripe>=16.1,<17` (+ `uv lock`). Stripe ships types (py.typed); if mypy
  complains add nothing—fix call sites.
- specs/30 §4.1 Decision (rules above), AGENTS.md P03-T07a bullet.

### Tests (packages/va_sim/tests/stripe/, helpers tests/support/stripe_helpers.py)
- helpers: `stripe_state()` (accelerated, `webhook_transport=RecordingTransport` from telnyx_helpers or a local
  recorder), `stripe_client(state)` (httpx ASGITransport over `Starlette(routes=simulator_routes("stripe", state))`,
  auth header), `form(**nested)` encoder written independently (do not reuse va_sim.stripe.form), `verify(body, header,
  secret)` independent HMAC check, `realtime` + `running_sims(state, ports={"stripe": 0})` for SDK tests.
- test_stripe_form.py: nested dicts, indexed lists, `[]` lists, conflicts → FormError, empty values.
- test_stripe_auth.py: bearer ok, basic ok, missing/unknown 401 body, added key accepted, Request-Id present.
- test_stripe_customers.py: create/retrieve/update, metadata merge + delete-by-empty, unknown param 400, unknown id
  404 resource_missing, default pm must be attached.
- test_stripe_payment_methods.py: magic attach creates new pm with card fields, list per customer newest-first/limit,
  attach to second customer 400, unknown magic id 404.
- test_stripe_payment_intents.py: confirm=false statuses; off-session success → succeeded + latest_charge + one
  `payment_intent.succeeded` webhook; config decline → 402 card_error card_declined with payment_intent and
  `payment_intent.payment_failed`; insufficient_funds pm decline_code; authentication_required off-session 402 vs
  on-session requires_action; missing pm 400 parameter_missing; amount_too_small; default pm fallback; retrieve.
- test_stripe_idempotency.py: replay same body + header + no second event; mismatch 400 idempotency_error; per-key
  scoping; error responses replayed.
- test_stripe_webhooks.py: signature header verifies (independent HMAC), timestamp = sim clock, retry 1 s/3 s on 500
  then drop (accelerated clock), webhook_url null disables, custom secret, fire arbitrary event via control.
- test_stripe_control.py: config GET/PATCH/422, api-keys validation, events list, webhooks list, reset clears and
  restores config, `/sim/state` stripe counts (also update tests/test_state.py expected snapshot if it asserts keys).
- test_stripe_portal.py: create (customer required/unknown), url uses request base, portal page 200/404.
- test_stripe_sdk.py (acceptance, realtime state, real socket, webhook receiver = a recording Starlette app served
  with `batch_helpers.served_app` or in-process transport): `StripeClient("sk_test_sim", base_addresses={"api": url},
  http_client=stripe.HTTPXClient(), max_network_retries=0)`; `await client.v1.customers.create_async(...)`,
  `payment_methods.attach_async`, `customers.update_async(invoice_settings default pm)`,
  `payment_intents.create_async(confirm=True, off_session=True)` → succeeded; arm decline via control → raises
  `stripe.CardError` with `code == "card_declined"`; `stripe.Webhook.construct_event(body, header, "whsec_sim")` for
  every recorded webhook (realtime clock so the 300 s tolerance holds); `events.retrieve_async`. If the sync client is
  simpler under pytest-asyncio, run it with `asyncio.to_thread`.
- Coverage: va_sim stays ≥ 85 % (target ≥ 95 % for new modules).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a packages/va_sim/src/va_sim/stripe` (no C+), audit (`uvx pip-audit` on `uv export`,
new dep stripe), coverage from make test.

### Risks
- stripe SDK 16 sends `Stripe-Version` of the newest API and may send extra params (e.g. none by default); if the
  SDK adds headers like `X-Stripe-Client-User-Agent`, ignore them. Unknown-param strictness must not reject params
  the SDK adds on its own — verify in the SDK test.
- HTTPXClient async usage under pytest-asyncio: create the client inside the test loop; close via
  `await http_client.close_async()` if exposed, else let ResourceWarning not fire (filterwarnings=error!).
- jscpd: webhook emitter refactor (see Files). Telnyx webhook tests must stay green.
- The SDK may refuse plain http base? (it does not; base_addresses accepts http).

## Build P03-T07a (Builder, 2026-11-01)

Implemented `va_sim.stripe` per plan (form, params, errors, objects, signing, store, webhooks, http, listing,
customers, payment_methods, payment_intents, portal, events, control, routes) + `va_sim/delivery.py`
(`SignedDelivery`, shared post/retry/log; telnyx `WebhookEmitter` now delegates, names re-exported, telnyx tests
unchanged and green). State/server/settings wired (`StripeDefaults`, `SimState.stripe`, `stripe_webhooks`, reset,
snapshot). specs/30 §4.1 Decision P03-T07a written (11 rules), AGENTS.md bullet, dev dep `stripe>=16.1,<17`.
Deviations from the plan, all recorded in the decision:
- DEFAULT_API_VERSION = `2026-10-28.endive` (what stripe 16.1 pins), not clover.
- No `customer.*` events in T07a (only payment_intent.* and control-fired).
- Concurrent replay of an in-flight key → 409 `idempotency_key_in_use`; crash releases the key.
- `ending_before` → 400 (only `starting_after` paging).
- A magic id given as both customer `payment_method` and default pm resolves to the created pm (real Stripe allows it).
- SDK test ignores exactly httpx's `data=<bytes>` DeprecationWarning raised from stripe's HTTPXClient.
Existing tests updated: test_control scoped-routes (stripe config now 200 on stripe, 404 elsewhere), test_state
snapshot, new test_server stripe webhook target test.
Gates: fmt ok; lint exit 0; typecheck exit 0 (605 files); placeholders 0 findings; make test 4965 passed, coverage
98.10% (va_sim.stripe 95-100% per module); spec refs 0 problems; jscpd 0 clones; radon stripe avg A (2.79), no C+
in new code (scenario._locate C is pre-existing); pip-audit no known vulns; pnpm audit 3 moderate (< high, pre-existing).

## Review P03-T07a (Reviewer, 2026-11-01)

Approved. Re-ran: lint 0, typecheck 0, placeholders 0, make test 4965 passed (coverage 98.10%), spec refs 0 problems,
jscpd 0 clones, radon no C+ in stripe/ or delivery.py. Diff checked against specs/30 §4.1 + Decision P03-T07a and
specs/02 §6 (sk_test_sim, whsec_sim, SIM_STRIPE_WEBHOOK_URL). Telnyx emitter refactor onto va_sim.delivery keeps
names re-exported; telnyx tests unchanged. Non-blocking notes: the first webhook attempt runs inline inside the
API request, as FakeTelnyx does (a slow target delays the Stripe response by up to 5 s); P03-T07b follows.
