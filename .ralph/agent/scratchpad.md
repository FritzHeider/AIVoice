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

## Plan P03-T07b (Planner, 2026-11-01)

Task: FakeStripe checkout, subscriptions, invoices, lifecycle events. Spec refs: specs/30 §4.1 (+ Decision P03-T07a),
specs/24 §7 (top-up checkout `mode=payment` with `price_data`/`product_data`/`payment_intent_data.setup_future_usage`/
metadata; subscription checkout with `plans.stripe_price_id`; plan change `create_prorations`; cancel at period end;
handled events `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated`,
`customer.subscription.deleted`). Builds on the T07a pipeline (`http.stripe_endpoint`, handlers raise `StripeError`,
`routes._API`, `events.emit_event(state, type, obj, call=None|call)`, `listing.paginate`, `params.*`).

### Spec decision to write: specs/30 §4.1 `> Decision (2026-11-01): FakeStripe checkout, subscriptions, invoices (P03-T07b).`
Append it directly after the P03-T07a decision; numbered rules. Also amend the T07a decision's first sentence only if it
becomes false (it says "follow in P03-T07b" — leave it). Rules:
1. Ids (seeded `state.ids`): `prod_`+14, `price_`+24, `cs_test_`+58, `sub_`+24, `si_`+14 (subscription item),
   `in_`+24, `il_`+24 (invoice line), `seti_`+24. Objects carry `id`, `object`, `created`, `livemode: false`.
2. Products/prices: `POST /v1/products` (name required; id optional custom id, duplicate → 400 `resource_already_exists`;
   description, metadata, active default true), `GET /v1/products/{id}`. `POST /v1/prices` (currency required, 3 letters
   lowercased; unit_amount required integer ≥ 0; exactly one of product | product_data[name] (creates a product);
   recurring[interval] day|week|month|year, recurring[interval_count] 1..(day 365|week 52|month 12|year 1) default 1;
   nickname, lookup_key (unique; duplicate → 400 unless `transfer_lookup_key=true`), metadata, active), `GET /v1/prices/{id}`,
   `GET /v1/prices` (filters `product`, `active`, `type` one_time|recurring, `lookup_keys[]`; paging as T07a rule 6).
   Price object: id, object "price", active, currency, unit_amount, product, type one_time|recurring,
   recurring {interval, interval_count, usage_type "licensed"} | null, nickname, lookup_key, metadata, billing_scheme
   "per_unit", created, livemode.
3. Checkout sessions: `POST /v1/checkout/sessions`: mode required payment|subscription|setup; success_url required
   (`{CHECKOUT_SESSION_ID}` template supported); cancel_url; customer (must exist) | customer_email (not both → 400);
   client_reference_id; metadata; line_items[n]{price | price_data{currency, unit_amount, product | product_data{name,
   description?, metadata?}, recurring{interval, interval_count}?}, quantity (int ≥ 1, default 1)}; payment_intent_data
   {setup_future_usage on_session|off_session, description, metadata} (payment mode only); subscription_data{metadata,
   description} (subscription mode only); currency (setup mode); payment_method_types[]; expires_at accepted (30 min..24 h
   from now else 400); `expand` ignored. Validation: payment mode → ≥ 1 line item, all one_time prices (a recurring one →
   400 `parameter_invalid` "You must be in subscription mode to specify a recurring price"); subscription mode → ≥ 1
   recurring price, all recurring prices share interval+interval_count, one-time items allowed and billed on the first
   invoice; setup mode → no line_items (400) and needs currency or payment_method_types; all line items one currency
   (else 400). `price_data` creates an inline price (`active: false`) and product if `product_data`; `price` must exist
   and be active. Session object: id, object "checkout.session", mode, status open|complete|expired, payment_status
   unpaid|paid|no_payment_required (setup), amount_subtotal, amount_total (Σ unit_amount×quantity; null for setup),
   currency, customer, customer_email, customer_details (null until complete, then {email, name: null}),
   client_reference_id, metadata, success_url, cancel_url, url `{request base}/sim/stripe/checkout/{id}` (null once not
   open), payment_intent, subscription, setup_intent, invoice (null until complete), expires_at (default created+86 400),
   payment_method_types, created, livemode. Line items are not embedded: `GET /v1/checkout/sessions/{id}/line_items`
   lists `item` objects {id `li_`+24, object "item", price (full price object), quantity, amount_subtotal, amount_total,
   currency, description (product name)}. `GET /v1/checkout/sessions/{id}`, `POST /v1/checkout/sessions/{id}/expire`
   (open only, else 400 `checkout_session_not_open`... use code `invalid_request` plain message) → status expired, url null,
   event `checkout.session.expired`.
4. Hosted page `GET /sim/stripe/checkout/{id}` (FakeStripe port, control route, not under the API pipeline):
   no `outcome` → 200 HTML page with links `?outcome=success` / `?outcome=cancel`; `outcome=success[&card=<magic pm id>]`
   (default `pm_card_visa`; unknown magic → 400 sim_error `invalid_request`) completes it and answers 303 to success_url
   (template substituted); `outcome=cancel` expires it (event `checkout.session.expired`) and answers 303 to cancel_url
   (200 HTML when none); unknown id → 404 `not_found`; a session that is not open → 409 sim_error `conflict`; other
   outcome values → 400. A declining card (or the armed `decline` config) answers 402 HTML naming the decline code, leaves
   the session open, creates nothing, emits nothing. Completion: the customer is the session's, else a new customer with
   `customer_email` (so va_billing always gets one); a new `pm_…` from the card (T07a rule 6 card table) is created.
   - payment: pm attached to the customer only when `payment_intent_data.setup_future_usage` is set (customer default is
     NOT changed — Stripe semantics); a `succeeded` PaymentIntent (amount_total, currency, customer, payment_method,
     metadata/description/setup_future_usage from payment_intent_data, latest_charge `ch_…`); events
     `payment_intent.succeeded`, then `checkout.session.completed`.
   - subscription: pm attached; a subscription (rule 5, default_payment_method = pm, metadata from subscription_data) with
     a paid `subscription_create` invoice (recurring items + one-time items); events `customer.subscription.created`,
     `invoice.paid`, `checkout.session.completed`. Session `subscription` and `invoice` set.
   - setup: pm attached; a `succeeded` SetupIntent {id, object "setup_intent", customer, payment_method, status, usage
     "off_session", created, livemode} (`GET /v1/setup_intents/{id}`); events `setup_intent.succeeded`,
     `checkout.session.completed`.
   Completed session: status complete, payment_status paid (setup: no_payment_required), url null, customer_details.
5. Subscriptions: `POST /v1/subscriptions` (customer required; items[n]{price, quantity}; default_payment_method (must be
   attached to the customer); metadata; description; cancel_at_period_end; payment_behavior allow_incomplete (default) |
   default_incomplete | error_if_incomplete; off_session; proration_behavior accepted). Object: id, object "subscription",
   customer, status incomplete|active|past_due|canceled, items {object list, data [{id si_, object "subscription_item",
   price (full object), quantity, current_period_start, current_period_end, created}], has_more false, url},
   cancel_at_period_end, canceled_at, ended_at, cancel_at (= period end when cancel_at_period_end else null),
   default_payment_method, latest_invoice, metadata, description, billing_cycle_anchor, start_date, currency, collection_method
   "charge_automatically", created, livemode. (Current API shape: period dates live on items, not on the subscription.)
   Create invoices immediately (billing_reason subscription_create) and charges: payment method = default_payment_method
   else the customer's invoice default; none or `default_incomplete` → status incomplete, invoice open, no charge event;
   decline (card or armed config) → `error_if_incomplete`: 402 card_error, nothing stored; otherwise status incomplete,
   invoice open with attempt_count 1, events `customer.subscription.created`, `invoice.payment_failed`; success → active,
   invoice paid, events `customer.subscription.created`, `invoice.paid`. Period: start = now, end = start + interval×count
   (`periods.add_interval`, calendar months/years clamped to month end, from the anchor day).
   `GET /v1/subscriptions/{id}`, `GET /v1/subscriptions` (filters customer, status (default excludes canceled; `all`),
   price; paging).
   `POST /v1/subscriptions/{id}`: items[0]{id (must be an item of the subscription), price, quantity}, proration_behavior
   create_prorations (default) | none | always_invoice, cancel_at_period_end, metadata (merge rules as customers),
   default_payment_method, description. A price change must keep the recurring interval+count (else 400
   `parameter_invalid` — sim limitation, recorded). Proration (create_prorations/always_invoice): fraction =
   (period_end − now) / (period_end − period_start); two lines `proration: true` — credit −round_half_up(old×qty×fraction),
   charge +round_half_up(new×qty×fraction) — stored as pending lines; create_prorations bills them on the next invoice;
   always_invoice creates a `subscription_update` invoice now: net > 0 charged (paid/payment_failed as above, failure
   → past_due), net ≤ 0 → invoice paid with amount_due 0 and the credit added to `customer.balance` (negative = credit).
   No-op update (nothing changed) emits nothing. Otherwise event `customer.subscription.updated` with
   `previous_attributes` holding the old values of changed top-level fields (items → old items object, cancel_at,
   cancel_at_period_end, metadata, default_payment_method, status). Updating a canceled subscription → 400.
   `DELETE /v1/subscriptions/{id}` (params invoice_now, prorate, cancellation_details accepted): status canceled,
   canceled_at = ended_at = now, event `customer.subscription.deleted`; already canceled → 400.
6. Invoices: object id, object "invoice", customer, status draft|open|paid|void (sim uses open|paid), billing_reason
   subscription_create|subscription_cycle|subscription_update, currency, subtotal, total, amount_due, amount_paid,
   amount_remaining, attempt_count, attempted, period_start, period_end, lines {object list, data [{id il_, object
   "line_item", amount, currency, description, quantity, period{start,end}, pricing {type "price_details",
   price_details {price, product}, unit_amount_decimal}, parent {type "subscription_item_details",
   subscription_item_details {subscription, subscription_item, proration}}}], has_more false, url}, parent {type
   "subscription_details", subscription_details {subscription, metadata}} (current API shape; no top-level
   `subscription`/`payment_intent`), default_payment_method null, hosted_invoice_url null, status_transitions {paid_at,
   finalized_at}, created, livemode, next_payment_attempt. `GET /v1/invoices/{id}`, `GET /v1/invoices` (filters customer,
   subscription, status; paging), `POST /v1/invoices/{id}/pay` (optional payment_method attached to the customer; open only,
   else 400 "Invoice is already paid"): success → paid + `invoice.paid`, and a past_due/incomplete subscription becomes
   active with `customer.subscription.updated`; decline → 402 card_error, attempt_count+1, `invoice.payment_failed`.
   Invoice charges create no PaymentIntent and no `payment_intent.*` event (sim simplification, recorded).
7. Control (FakeStripe port only): `POST /sim/stripe/subscriptions/{id}/renew` body optional `{"fail": bool}` (strict,
   default false): ends the current period. cancel_at_period_end → status canceled, ended_at = period end, event
   `customer.subscription.deleted` (200 the subscription). Otherwise new period [old end, old end + interval) on every item,
   a `subscription_cycle` invoice (items + pending prorations, pending cleared), charged as rule 5 (`fail: true` forces a
   decline with `generic_decline`; no payment method also fails): paid → status active, events `invoice.paid`,
   `customer.subscription.updated` (previous_attributes items); failed → status past_due, invoice open, events
   `invoice.payment_failed`, `customer.subscription.updated` (previous_attributes items + status). Canceled/incomplete
   subscription → 409 sim_error `conflict`; unknown → 404 `not_found`. Response 200 `{"subscription", "invoice"}`
   (invoice null on cancel). The sim clock is not moved.
8. Reset drops products, prices, sessions, subscriptions, invoices, setup intents. `/sim/state` `stripe` adds
   `products`, `prices`, `checkout_sessions`, `subscriptions`, `invoices`, `setup_intents`.
9. `DELETE` requests read parameters from the query string and the form body (SDK sends them as query).

### Files (packages/va_sim/src/va_sim/stripe/)
- `periods.py` (pure): `add_interval(ts: int, interval: Literal["day","week","month","year"], count: int, *, anchor_day: int
  | None = None) -> int` (UTC; months/years clamp to month end; keep anchor day so Jan 31 → Feb 28 → Mar 31),
  `MAX_INTERVAL_COUNT: Mapping[str,int]`, `prorate(amount: int, *, start: int, end: int, now: int) -> int`
  (Decimal ROUND_HALF_UP, now clamped into [start, end]).
- `billing_objects.py` (pure JSON builders, keeps objects.py small): `product_json`, `price_json`, `checkout_session_json`,
  `checkout_item_json`, `subscription_json`, `subscription_item_json`, `invoice_json`, `invoice_line_json`,
  `setup_intent_json`.
- `catalog.py`: handlers `create_product`, `retrieve_product`, `create_price`, `retrieve_price`, `list_prices`; helpers
  `require_price(store, id, *, param)`, `inline_price(call|state, price_data, *, param_prefix)` (used by checkout),
  `parse_recurring(...)`.
- `charging.py`: shared card-charge decision extracted from `payment_intents._outcome`:
  `decide(store, card: CardSpec, *, off_session: bool, force_decline: bool = False) -> ChargeDecision`
  (`outcome`, `decline_code`, `code`, `message`); `payment_intents` uses it (behaviour unchanged, T07a tests stay green),
  and `card_error(decision, *, extra) -> StripeError` builds the 402. Also `new_id(state, prefix, length)` helper (move
  `_new_id` here; ids from `state.ids`).
- `invoicing.py`: `open_invoice(state, sub, *, reason, lines, period) -> dict`, `subscription_lines(state, sub, period)`,
  `attempt_payment(state, invoice, sub, *, call, force_decline=False, payment_method=None) -> ChargeDecision | None`
  (marks paid/open, attempt_count, status_transitions, customer balance for credits; emits `invoice.paid` /
  `invoice.payment_failed`), `subscription_payment_method(store, sub) -> str | None`.
- `subscriptions.py`: `create_subscription`, `retrieve_subscription`, `list_subscriptions`, `update_subscription`,
  `cancel_subscription`, plus `start_subscription(state, *, customer, items, default_pm, metadata, description, call,
  payment_behavior) -> (sub, invoice, decision)` shared with checkout, `renew_subscription(state, sub_id, *, fail) ->
  (sub, invoice|None)` used by control.
- `invoices.py`: `retrieve_invoice`, `list_invoices`, `pay_invoice`.
- `setup_intents.py`: `retrieve_setup_intent` (+ creation helper used by checkout).
- `checkout.py`: `create_checkout_session`, `retrieve_checkout_session`, `checkout_line_items`, `expire_checkout_session`,
  `complete_session(state, session_id, card_id) -> dict` and `expire_session(state, session, call|None)`.
- `checkout_page.py`: `checkout_page(state, request) -> Response` (rule 4 HTML / 303 / 402 / 404 / 409 / 400; escape all
  interpolated text with `html.escape`).
- Modify `routes._API` (new paths incl. `DELETE /v1/subscriptions/{sub_id}`, `/v1/invoices/{in_id}/pay`,
  `/v1/checkout/sessions/{cs_id}/expire`, `/line_items`), `control.py` (`/sim/stripe/checkout/{session_id}` GET,
  `/sim/stripe/subscriptions/{sub_id}/renew` POST with `RenewBody(_Strict)` `fail: bool = False`, allow empty body),
  `store.py` (dicts products, prices, checkout_sessions, checkout_items (session id → list), subscriptions, invoices,
  setup_intents, pending_lines (sub id → list); `lookup_keys` index; reset), `state.py` snapshot, `http.py`
  `_decoded_params` (DELETE: query pairs + body pairs), `payment_intents.py` (use `charging`), possibly `customers.py`
  (`balance` updates are just dict writes). Keep every module ≤ ~250 lines and radon ≤ B.
- specs/30 §4.1 Decision P03-T07b (rules above, adjusted to what was built); AGENTS.md P03-T07b bullet.

### Tests (packages/va_sim/tests/stripe/, extend tests/support/stripe_helpers.py with `price(client, amount, recurring=...)`,
`checkout(client, **params)`, `complete(client, session, card=None, outcome="success")` (GET page via the same ASGI client,
no redirects), `renew(client, sub_id, fail=False)`, `event_types(hooks)`)
- test_stripe_periods.py: month/year add with clamping + anchor (Jan 31→Feb 28/29→Mar 31), week/day, interval_count,
  prorate half-up and clamping.
- test_stripe_catalog.py: product create/retrieve/custom id/duplicate; price create with product / product_data /
  recurring; validation (currency, negative amount, both/neither product, bad interval, interval_count bounds); lookup_key
  uniqueness + transfer; list filters + paging; unknown 404.
- test_stripe_checkout.py: payment-mode top-up exactly as specs/24 §7 (price_data/product_data/unit_amount, quantity 1,
  payment_intent_data.setup_future_usage=off_session, metadata org_id/purpose/amount_micros) → url under request base;
  page without outcome 200 HTML; success → 303 Location success_url with `{CHECKOUT_SESSION_ID}` substituted; events
  [`payment_intent.succeeded`, `checkout.session.completed`] with metadata; PI succeeded, pm attached to customer, customer
  default unchanged; customer_email → new customer; no setup_future_usage → pm not attached; cancel → 303 cancel_url +
  `checkout.session.expired`; second visit 409; unknown 404; bad outcome 400; declining card + armed decline → 402 page,
  session still open, no events; validation errors (mode missing, recurring price in payment mode, mixed currencies,
  setup with line_items, customer+customer_email, unknown price, inactive price, mixed intervals, expires_at bounds);
  line_items listing; expire API + second expire 400; retrieve; setup mode → setup intent + events.
- test_stripe_subscriptions.py: subscription checkout → subscription active, items period dates (month), invoice paid
  `subscription_create`, events order; create via API with default pm success / no pm → incomplete / declined →
  incomplete + `invoice.payment_failed` / error_if_incomplete 402 nothing stored / default_incomplete; price change with
  create_prorations (frozen accelerated clock advanced half a period → proration lines ±half, applied on renew invoice
  total) and none (no lines) and always_invoice (immediate invoice, upgrade charged, downgrade credits customer.balance);
  interval change 400; previous_attributes contents; no-op update emits nothing; cancel_at_period_end → updated event with
  cancel_at; renew then → deleted; DELETE cancel → deleted; second cancel 400; list filters.
- test_stripe_invoices.py: retrieve/list filters/paging, unknown 404, line shapes (pricing/parent), pay open invoice
  success → paid + subscription past_due→active events; pay paid invoice 400; pay with declining pm 402 + attempt_count.
- test_stripe_renewal.py (control): renew success (period advanced from old end, invoice cycle paid, events), `fail: true`
  → past_due + payment_failed + updated(previous status), armed config decline fails too, customer without pm fails,
  renew canceled → 409, unknown 404, strict body 422, empty body ok.
- test_stripe_control.py / tests/test_state.py: snapshot keys added; reset clears new stores.
- test_stripe_sdk_billing.py (acceptance; realtime state, real socket like test_stripe_sdk.py, same pytestmark filter and
  `close_async`): SDK creates product+recurring price, top-up checkout session (`checkout.sessions.create_async` with
  price_data) → httpx GET url `?outcome=success` → 303 → `payment_intents.retrieve_async` succeeded; subscription checkout
  → complete → `subscriptions.retrieve_async` active; `subscriptions.update_async` price change with
  `proration_behavior=create_prorations`; `invoices.retrieve_async(latest_invoice)` and `invoices.list_async(subscription=…)`;
  renew via control; `subscriptions.update_async(cancel_at_period_end=True)` then `subscriptions.cancel_async` → canceled;
  every recorded webhook passes `stripe.Webhook.construct_event(body, header, "whsec_sim")` and the type sequence
  includes checkout.session.completed, customer.subscription.created/updated/deleted, invoice.paid. Verify the SDK's
  DELETE param encoding works.
- Coverage: va_sim ≥ 85 % (target ≥ 95 % per new module).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones — handlers share `require_*`/`new_id`/list helpers; don't copy the payment-intent decline block),
`uvx radon cc -s -a packages/va_sim/src/va_sim/stripe` (no C+), audit (`uvx pip-audit` on `uv export`, `corepack pnpm audit`).

### Risks
- jscpd: billing JSON builders and handlers look alike; centralise list objects (`list_json`) and id creation.
- Accelerated clock: proration tests need controlled `now`; use `state.clock.advance(s)` (accelerated) and assert exact
  amounts from the formula, not wall time.
- radon: checkout validation and update_subscription are branchy — split into small validators per concern.
- SDK: `checkout.sessions.create_async` nested list params (`line_items[0][price_data][product_data][name]`) must decode
  (form.py supports it); `subscriptions.cancel_async` sends DELETE (query params); `items[0][id]` for update.
- Don't break T07a: `payment_intents` refactor onto `charging.decide` must keep all existing tests unchanged.

## Build P03-T07b (Builder, 2026-11-01)

Implemented per plan with these adjustments (all recorded in specs/30 §4.1 Decision P03-T07b):
- Modules: `catalog`, `billing_objects`, `periods`, `charging` (decide/ChargeDecision/new_id; payment_intents refactored
  onto it, T07a tests unchanged), `invoicing` (lines as dicts, `open_invoice` applies customer balance,
  `apply_attempt` + `announce`), `subscriptions` (+ `PlannedSubscription`, `start_subscription`),
  `subscription_update`, `invoices`, `renewal`, `checkout`, `checkout_complete` (+ setup intent retrieve),
  `checkout_page`. `params` gained `currency`, `hash_list`, `string_list`, `new_metadata`, prefix on integer/boolean/
  mapping/metadata. `payment_methods.create_card_method`, `customers.new_customer_json` extracted for the page.
- Events: `customer.subscription.created` carries the final status and precedes the invoice event; always_invoice and
  renewal emit the invoice event before `customer.subscription.updated`.
- Hosted page passes 3DS (authentication cards succeed); setup mode never declines; API/renewal/pay charges are
  off-session. Missing referenced objects stay 404 (T07a convention). expires_at reported, not enforced.
- Subscription item updates require `items[n][id]` (adding/removing items unsupported, recorded).
Tests: test_stripe_{periods,catalog,checkout,subscriptions,invoices,renewal,sdk_billing}.py; control/state snapshot
keys updated. va_sim.stripe coverage 99%.

## Review P03-T07b (Reviewer, 2026-11-01)
Approved. Re-ran lint/typecheck/placeholders (pass), make test (5062 passed, 98.18%), spec refs, jscpd (0 clones),
radon (avg A, no C+). Diff matches specs/30 §4.1 Decision P03-T07b and the plan deliverables (checkout modes + hosted
page, subscription create/update/cancel with prorations, invoices retrieve/list/pay, renew control with fail →
past_due, lifecycle events); SDK acceptance test verifies every webhook with construct_event.
