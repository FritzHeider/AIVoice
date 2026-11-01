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

## Plan P03-T08a (Planner, 2026-11-01)

P03-T08 was split in plan/phase-03.md: **T08a** = package + transport core + call-control/conference commands;
**T08b** = provisioning/numbers/messaging/download/balance + `TelnyxClientFactory`. T09 now depends on T08a,
T15/T16 on T08b. Spec: specs/06 §2, §2.1 (call-command + conference rows), §2.2, §3. va_telnyx must NEVER import
va_sim (phase constraint: no shared message construction); tests may.

### Spec decision to write: specs/06 §2.2 `> Decision (2026-11-01): va_telnyx transport (P03-T08a).`
1. Rate-limit categories: `call_command` (every `/calls*` and `/conferences*` POST incl. dial) 50/s burst 100,
   `number_search` 5/s burst 5, `number_order` 2/s burst 2, `messaging` 20/s burst 20 (burst = rate where the spec
   gives none). Operations outside these four (provisioning CRUD, phone-number management, order status, balance,
   download) are not limited client-side. Key `rl:telnyx:{account}:{category}` (spec's `rl:telnyx:{account}` +
   category suffix, one bucket per category). Wait loop: take → if denied sleep `retry_after_ms` on the injected
   Clock while the 2 s budget lasts, else raise `TelnyxRateLimited`. Redis errors fail open (log
   `telnyx_rate_limiter_unavailable`, proceed) — the vendor's own 429 + retries still protect us.
2. Retry: idempotent = every call command/conference command (they carry command_id) and every GET/PATCH/DELETE.
   Attempts 3 total? Spec says "3 attempts, delays 100, 400, 1200" → interpret as **up to 3 retries** (4 requests)
   with delays 100/400/1200 ms + jitter U(0,100) ms; `Retry-After` (seconds int or HTTP-date) replaces the delay
   when larger, capped at 5 s (beyond the cap → stop retrying and map the error). Retry on httpx.ConnectError /
   ConnectTimeout / PoolTimeout(no bytes sent) and on 429/500/502/503/504; ReadTimeout on idempotent calls retries too
   (command_id dedupes); non-idempotent (`POST /number_orders`, `POST /messages`, create-resource POSTs) retry only
   on ConnectError/ConnectTimeout. Record the attempts interpretation in the Decision.
3. Error mapping: 422 any error code `90018` → `CallAlreadyEnded`; 404 on a call command (path `/calls/{id}/actions/*`)
   → `CallAlreadyEnded`; `hangup` swallows `CallAlreadyEnded` (returns normally, result `already_ended`). Other
   non-2xx → `TelnyxApiError(status, errors)`; `TelnyxApiError.to_app_error()` → `AppError("CARRIER_ERROR",
   details={"telnyx_errors": [...code,title,detail...]})`; transport failure after retries → `TelnyxUnavailable`
   (subclass of `TelnyxApiError` with status 0? no — separate `TelnyxTransportError`) whose `to_app_error()` is also
   `CARRIER_ERROR` (details `{"telnyx_errors": [], "transport": "<exc class>"}`); `TelnyxRateLimited.to_app_error()`
   → `SERVICE_UNAVAILABLE`. A non-JSON error body becomes one synthetic error `{"code": "http_<status>",
   "title": reason_phrase, "detail": first 200 chars}`.
4. Metrics: client-library metrics are not in the per-service CATALOG (names are catalog-unique, the client runs in
   api/edge/worker). Add specs/27 §3.6 "Vendor client metrics (registered by the client library in any process
   that builds a client)" with `va_telnyx_requests_total` counter labels `operation`, `outcome`
   (ok/already_ended/carrier_error/rate_limited/transport_error) and `va_telnyx_request_duration_ms` histogram
   (LAT_MS) label `operation`, plus a Decision line. Check `test_metrics.parse_spec_catalog` only reads §3.1–§3.5
   (`_SECTION = ^### 3\.([1-5])`) and the count stays 56 — run it. `va_vendor_errors_total` stays media-only.
5. Tracing: one span per logical call `telnyx.<operation>` (kind CLIENT) with `telnyx.operation`,
   `telnyx.call_control_id` (when present), `telnyx.command_id`, `telnyx.status_code` (final), `telnyx.attempts`;
   error status on failure. httpx spans per attempt come from `va_common.http` instrumentation.

### Files (packages/va_telnyx, dist `va-telnyx`, module `va_telnyx`)
- `pyproject.toml` (deps: va-common, httpx[http2], pydantic, opentelemetry-api, prometheus-client, redis — mirror
  versions already in the lock); `src/va_telnyx/py.typed`; workspace wiring per AGENTS.md P00-T05 (root dev group +
  `[tool.uv.sources]`, mypy `mypy_path` += `packages/va_telnyx/src` + `packages/va_telnyx/tests/support`, `files` +=
  src/tests, pytest pythonpath += `packages/va_telnyx/tests/support`).
- `src/va_telnyx/__init__.py`: re-export `TelnyxClient`, errors, request models, `TelnyxRateLimiter`, `client_state`.
- `errors.py`: `TelnyxError(Exception)` base with `to_app_error() -> AppError`; `TelnyxErrorItem` (frozen dataclass
  code/title/detail/source: dict|None); `TelnyxApiError(status: int, errors: tuple[TelnyxErrorItem, ...],
  operation: str)`; `CallAlreadyEnded(TelnyxApiError)`; `TelnyxRateLimited(TelnyxError)` (category, account);
  `TelnyxTransportError(TelnyxError)` (operation, cause). `parse_error_body(response) -> tuple[TelnyxErrorItem,...]`.
- `client_state.py`: `ClientState` (pydantic frozen: v: Literal[1]=1, call_id: UUID, leg: Literal["primary",
  "transfer_target","warm_proxy","monitor"], org_id: UUID); `encode(state) -> str` (compact JSON, base64 std,
  raises ValueError if > 1024 bytes encoded), `decode(value: str) -> ClientState` raising `ClientStateError`
  (ValueError subclass) on bad base64/JSON/schema; `command_id(action: str, call_id: UUID|str, leg: str, *parts:
  str) -> str` = `"-".join([action, str(call_id), leg, *parts])` (deterministic; spec example
  `hangup-{call_id}-{leg_role}`).
- `rate_limit.py`: `RateCategory = Literal["call_command","number_search","number_order","messaging"]`,
  `LIMITS: Mapping[RateCategory, BucketLimit]`, `TelnyxRateLimiter(redis, *, clock: Clock, max_wait_s=2.0)` with
  `async acquire(account: str, category: RateCategory) -> None`. Needs ms precision: extend
  `va_common.token_bucket.BucketDecision` with `retry_after_ms: int` (additive; set in `take`; update its unit test).
  `NullRateLimiter` is NOT needed — tests use fakeredis (`fakeredis[lua]`).
- `retry.py`: `RetryPolicy(delays_s=(0.1,0.4,1.2), jitter_s=0.1, retry_statuses=frozenset({429,500,502,503,504}),
  max_retry_after_s=5.0)`, `retry_after_seconds(headers, now) -> float|None` (int or HTTP-date), `next_delay(attempt,
  response|None, rng, now) -> float|None`. Randomness via injectable `random.Random` (tests seed it).
- `metrics.py`: `TelnyxMetrics(registry: CollectorRegistry)` creating the two §3.6 metrics with `LAT_MS`
  (import from va_common.metrics); `observe(operation, outcome, duration_ms)`. Client takes `metrics: TelnyxMetrics |
  None = None`.
- `transport.py`: `Operation` (frozen dataclass: name, method, path, category: RateCategory|None, idempotent: bool,
  call_command: bool) and `TelnyxTransport` that owns `_send(op, *, json=None, params=None, call_control_id=None,
  command_id=None) -> dict[str, Any]` (rate limit → attempts loop with httpx → error mapping → returns parsed JSON
  `data`), metrics, span. Keep functions radon ≤ B: split `_attempt`, `_classify(response)`, `_sleep_before_retry`.
- `models.py`: request models (pydantic, `extra="forbid"`, serialize with `model_dump(mode="json", by_alias=True,
  exclude_none=True)`; `from_` aliased to `"from"`): `StreamParams` (stream_url, stream_track
  ("inbound_track"|"outbound_track"|"both_tracks"), stream_codec, stream_bidirectional_mode ("rtp"), 
  stream_bidirectional_codec ("PCMU"|"L16"), stream_bidirectional_sampling_rate (8000|16000),
  stream_bidirectional_target_legs="self", stream_auth_token), `RecordParams` (record "record-from-answer"?,
  record_channels, record_format, record_track, record_max_length), `DialRequest` (spec row fields exactly, defaults
  webhook_url_method="POST", send_silence_when_idle=True, timeout_secs, time_limit_secs,
  answering_machine_detection ("disabled"|"detect"|"detect_beep"|"detect_words"|"greeting_end"|"premium"), config
  dict, custom_headers list[{name,value}], link_to, bridge_intent, bridge_on_answer), `AnswerRequest`,
  `TransferRequest` (early_media=True, answering_machine_detection="disabled" defaults), `RecordStart` defaults
  per row, `PlaybackStart`; response models `DialResult` (call_control_id, call_leg_id, call_session_id,
  is_alive, record_type="call", extra="ignore"), `CommandResult` (result: str, extra ignore),
  `Conference` (id, name, status?, extra ignore). Read the sim's `call_models.py` only to *compare* field lists —
  where the sim rejects a spec field (extra_forbidden), fix the **sim** model to follow the Telnyx docs, not the
  client.
- `client.py`: `TelnyxClient(api_key, base_url, http, limiter, *, account="platform", metrics=None, clock=SystemClock(),
  rng=None, retry=RetryPolicy(), tracer_provider=None)`. Auth header added per request (`Authorization: Bearer`),
  `repr` never shows the key. Methods (all `async`, `command_id: str` keyword-only required, `client_state: str |
  None = None`): `dial(req: DialRequest) -> DialResult` (command_id inside the model), `answer(ccid, req)`,
  `reject(ccid, cause: Literal["CALL_REJECTED","USER_BUSY"], *, command_id)`, `hangup(ccid, *, command_id,
  client_state=None) -> bool` (False when already ended), `transfer(ccid, req)`, `bridge(ccid, other_ccid, *,
  command_id, client_state, park_after_unbridge=None)`, `send_dtmf(ccid, digits, duration_millis=250, *,
  command_id)` (validate digits regex `^[0-9A-D*#wW]+$` and 100–500 → `ValueError` before any I/O),
  `record_start(ccid, *, command_id, client_state, format="wav", channels="dual", play_beep=False, max_length=0,
  timeout_secs=0)`, `record_stop`, `streaming_start(ccid, stream: StreamParams, *, command_id, client_state)`,
  `streaming_stop(ccid, *, stream_id=None, command_id, client_state)`, `playback_start(ccid, audio_url,
  loop="infinity", overlay=False, target_legs="self", *, command_id, client_state)`, `playback_stop(ccid, *,
  command_id)` (body stop="all"), `conference_create(ccid, name, *, command_id, client_state) -> Conference`
  (beep_enabled="never", start_conference_on_create=True), `conference_join(conf_id, ccid, *,
  end_conference_on_exit: bool, command_id, client_state, start_conference_on_enter=True, hold=False, mute=False)`,
  `conference_leave(conf_id, ccid, *, command_id)`. Commands on an ended call (other than hangup) raise
  `CallAlreadyEnded` — callers ignore it (document in docstring). Paths built with `urllib.parse.quote(ccid,
  safe="")` (call_control_ids contain `:`; check Telnyx/sim ids — `v3:...` — quote with safe=":" if the sim route
  rejects encoded colons; test it).
- `README`-free; AGENTS.md bullet; specs/06 §2.2 Decision; specs/27 §3.6 + Decision.

### Tests (packages/va_telnyx/tests/, support dir `tests/support/telnyx_client_helpers.py`)
Helpers: `sim_client(state, api_key=DEFAULT_API_KEY) -> TelnyxClient` over `httpx.ASGITransport` of
`Starlette(routes=simulator_routes("telnyx", state))` (reuse `telnyx_helpers.telnyx_state(RecordingTransport)`,
`at`, `settle`, `inbound_call`, `answered_call`, `events`, `summary` from va_sim test support — already on
pythonpath), fakeredis limiter, FrozenClock; `respx_client(router)`; `client_state_for(...)`.
- `test_client_state.py`: round trip; compact JSON exact bytes; >1 KB rejected; bad base64/JSON/leg/version → error;
  `command_id` determinism.
- `test_errors.py`: parse Telnyx error body, non-JSON body, `to_app_error` → CARRIER_ERROR with
  `details.telnyx_errors`, rate-limited → SERVICE_UNAVAILABLE, transport → CARRIER_ERROR.
- `test_requests_exact.py` (respx): for every T08a method assert method, path, Authorization header, and the exact
  JSON body (full dict equality, incl. defaults: send_silence_when_idle true, target_legs "self",
  webhook_url_method "POST", early_media true, beep_enabled "never", stop "all", etc.); `from` alias; `None` fields
  omitted; send_dtmf validation raises before any request (`respx` route not called).
- `test_retry.py` (respx + FrozenClock recording sleeps + seeded rng): 503,503,200 → success after delays
  ≈ 0.1+j, 0.4+j; 429 with `Retry-After: 2` → 2 s sleep; Retry-After HTTP-date; Retry-After > cap → no retry,
  error mapped; 4 consecutive 503 → CARRIER_ERROR after 4 requests; ConnectError retried; ReadTimeout retried for a
  command; 400 not retried; non-idempotent op (use an internal Operation with idempotent=False via transport) not
  retried on 503 nor ReadTimeout but retried on ConnectError.
- `test_error_mapping.py` (respx): 422 90018 → CallAlreadyEnded; 404 on command → CallAlreadyEnded; hangup returns
  False on both; 404 on conference join → TelnyxApiError (not a call command); 401 → TelnyxApiError CARRIER_ERROR.
- `test_rate_limit.py` (fakeredis + FrozenClock whose sleep advances time): burst 100 call commands pass with no sleep;
  101st sleeps ≈ 20 ms then passes; number_search 6th waits; when wait would exceed 2 s → `TelnyxRateLimited` and no
  HTTP request made; buckets are per account and per category; Redis error → fail open + log event.
- `test_metrics_tracing.py`: InMemorySpanExporter (`obs_helpers.memory_provider()`) span name/attributes/attempts;
  metrics counter outcome labels via `new_registry(process_collectors=False)`.
- `test_contract_calls.py` (FakeTelnyx, accelerated state): dial → DialResult ids, sim summary shows connection/
  stream params; duplicate command_id dial returns same ccid; answer inbound; reject inbound USER_BUSY; hangup
  answered → True, then again → False; transfer; bridge two answered calls; send_dtmf; record_start/stop;
  streaming_start/stop (stream_url pointing nowhere → use `media_agent` fixture or a call without stream and
  assert sim accepted; follow AGENTS.md P03-T03b: any stream_url needs the media_agent fixture — prefer
  dialing without stream_url and asserting `streaming.failed`-free via `events`... simplest: use the
  `media_agent` fixture from va_sim tests/telnyx/conftest.py by importing `media_agent` support), playback
  start/stop, conference create/join/leave; every webhook from sim carries the encoded client_state that
  `client_state.decode` reads back. Command on ended call → CallAlreadyEnded.
- `test_token_bucket.py` (va_common): `retry_after_ms` reported.
- Coverage ≥ 95 % for va_telnyx.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a packages/va_telnyx/src` (no C+), audit (`uvx pip-audit` on `uv export`,
`corepack pnpm audit`).

### Risks
- Sim models may forbid some spec fields (e.g. `send_silence_when_idle`, `stream_bidirectional_target_legs`,
  `link_to`, `bridge_intent`, `park_after_unbridge`, `answering_machine_detection_config`): fix the sim per Telnyx
  docs + specs/06 (not the client), keep its tests green.
- Call control ids with `:` in URL paths — verify quoting with the sim and respx.
- FrozenClock.sleep semantics (does it advance?) — check `va_common.time.FrozenClock.sleep`; if it doesn't advance,
  use a recording clock in tests.
- Streaming contract test + media WS: follow AGENTS.md P03-T03b media_agent rule or the sim adds `streaming.failed`.
- Don't make `TelnyxClient` a god class: transport in `transport.py`, commands in `client.py` (≤ ~300 lines);
  T08b adds `provisioning.py`/`numbers.py` mixins or a second module.

## Build P03-T08a (Builder, 2026-11-01)
Built `packages/va_telnyx` per plan: errors (TelnyxApiError/CallAlreadyEnded/TelnyxRateLimited/TelnyxTransportError
+ to_app_error), client_state (encode/decode/command_id), rate_limit (per account+category buckets, 2 s budget per
acquire, Redis fail-open), retry (3 retries 100/400/1200 ms + jitter, Retry-After ≤ 5 s), metrics (§3.6), transport
(Operation + pipeline + span), models (Dial/Answer/Transfer/StreamParams/AmdConfig; `serialization_alias="from"`
because mypy runs without the pydantic plugin), client (all T08a methods). Deviations from plan: limiter `max_wait`
is per acquire call (not cumulative); `TelnyxError` base maps to CARRIER_ERROR (no abstract method — placeholder
gate); no sim model changes were needed (FakeTelnyx accepted every body). Shared `RecordingClock` moved to
`packages/va_common/tests/clock_helpers.py` (jscpd clone with events_sink). `test_metrics.parse_spec_catalog` now
resets on any `##`/`###` heading so the new §3.6 table is not parsed as §3.5. `BucketDecision.retry_after_ms` added.
Spec: specs/06 §2.2 Decision P03-T08a; specs/27 §3.6 + Decision. Coverage va_telnyx 100 %, radon avg A (max B),
jscpd 0 clones, spec refs 0, pip-audit clean, pnpm audit 3 moderate (no high).

## Review P03-T08a (Reviewer, 2026-11-01)
Approved. Re-ran make lint/typecheck/placeholders (0 findings) and make test (5179 passed, 98.24 %). Checked
client bodies against specs/06 §2.1 rows (defaults, `from` alias, target_legs only on dial/answer per Decision 5),
§2.2 retry/mapping/rate-limit semantics vs Decision, §3 client_state format, specs/27 §3.6 metrics. No findings.

## Plan P03-T08b (Planner, 2026-11-01)
Scope: rest of the specs/06 §2.1 table (provisioning, numbers, messaging, download, balance) + `TelnyxClientFactory`
(specs/06 §1). FakeTelnyx already serves every endpoint (`va_sim.telnyx.resources` apps/OVPs/messaging profiles with
filters `filter[application_name][contains]`, `filter[name][contains]`, `filter[name]` exact; `numbers.py`
available/orders/phone_numbers/messaging; `messages.py`; `/v2/balance`; recordings `/sim/recordings/{id}.wav` via
the control routes). Fix the **sim** only if it rejects a documented field; do not share code with it.

### Spec decision to write: specs/06 §2.2 `> Decision (2026-11-01): provisioning, numbers, messaging, factory (P03-T08b).`
1. Operations/categories: `search_numbers` → `number_search`; `create_number_order` → `number_order`
   (non-idempotent); `send_message` → `messaging` (non-idempotent); provisioning POSTs non-idempotent, no category;
   GET/PATCH/DELETE idempotent, no category (consistent with Decision P03-T08a 1/3).
2. List methods return every page: follow `meta.total_pages` with `page[number]`, `page[size]=250` (cap 20 pages →
   stop; Telnyx max page size 250). Name filters are server-side; the bootstrap compares names exactly itself.
3. `search_numbers` validates `limit` 1–50 and features ⊆ {voice,sms,mms} with `ValueError` before any I/O; always
   sends `filter[best_effort]=false`; unset filters omitted; features sent as repeated `filter[features][]`.
4. `get_phone_number_by_e164` returns `None` when `data` is empty. `update_phone_number` sends only the given
   fields (`ValueError` when none); `number_tags(org_id) -> ["va", f"org:{org_id}"]` builds the §2.1 tags.
5. `download(url, destination, *, max_bytes=2 GiB)` (specs/06 §11 step 2): absolute URL only (`ValueError` for a
   relative path), no auth header, streamed to `destination` (truncated on each retry attempt, deleted on final
   failure), retried like a GET; exceeding `max_bytes` raises `TelnyxDownloadTooLarge` (a `TelnyxError`,
   `CARRIER_ERROR`, details `{"telnyx_errors": [], "max_bytes": n}`) and removes the file. Returns bytes written.
6. Factory: `for_call(call)` reads `call.config["telnyx_account_id"]` (None/absent = platform);
   `for_account(None)` = platform; BYO key from the injected async resolver, cached in process 5 min per account
   (one in-flight resolve per account via a per-account `asyncio.Lock`); unknown/disabled account → resolver
   returns `None` → `UnknownTelnyxAccount(LookupError)`; `invalidate(account_id)` drops the cache entry (key
   rotation/deletion). The client's `account` label is `str(telnyx_account_id)` (rate-limit bucket per account).

### Files
- `packages/va_telnyx/src/va_telnyx/transport.py`: refactor so the pipeline is shared:
  `request(...) -> dict` (JSON, unchanged signature) and new `download(operation, url, destination, *, max_bytes)
  -> int`. Generalize `_send_with_retries(request, span, attempt)` where `attempt: Callable[[], Awaitable[
  httpx.Response]]`; for download the attempt uses `self._http.stream("GET", url, headers=...)`, on 2xx writes
  chunks via `anyio.open_file(destination, "wb")` (non-blocking; anyio is already installed via httpx — add it to
  va-telnyx deps explicitly) counting bytes, raising `TelnyxDownloadTooLarge` when over; on non-2xx reads at most
  64 KiB of the body for `_mapped_error`. Keep radon ≤ B (split helpers). Add `params` support already present.
  Also add `paged(operation, path, params, model) -> list[M]` helper (or put it in a small `pagination.py`):
  loops pages per Decision 2.
- `src/va_telnyx/errors.py`: `TelnyxDownloadTooLarge(TelnyxError)` (operation, max_bytes; `to_app_error` per
  Decision 5). Metrics outcome for it: `carrier_error` (falls under `_outcome` default).
- `src/va_telnyx/provisioning_models.py` (keep models.py < ~250 lines): requests `CallControlAppCreate`
  (fields/defaults exactly the §2.1 row; `inbound` fixed `{"channel_limit": None, "sip_subdomain_receive_settings":
  "only_my_connections"}` — note `channel_limit: null` must be SENT, so build that sub-dict explicitly, not via
  exclude_none), `OutboundVoiceProfileCreate` (`traffic_type="conversational", service_plan="global",
  concurrent_call_limit=None` sent as null, `enabled=True`, `whitelisted_destinations=("US","CA")` overridable),
  `MessagingProfileCreate` (`webhook_api_version="2"`, `whitelisted_destinations=("US","CA")`, `enabled=True`);
  responses (extra="ignore"): `CallControlApp(id, application_name, webhook_event_url, outbound_voice_profile_id`
  read from `outbound.outbound_voice_profile_id` via a model validator or nested `AppOutbound` model),
  `OutboundVoiceProfile(id, name, whitelisted_destinations)`, `MessagingProfile(id, name, webhook_url: str|None,
  whitelisted_destinations)`, `Balance(balance: Decimal, credit_limit: Decimal, available_credit: Decimal,
  currency: str)`.
- `src/va_telnyx/number_models.py`: `NumberSearch` (country_code `^[A-Z]{2}$` default "US", national_destination_code,
  locality, administrative_area, contains, features tuple[Literal["voice","sms","mms"],...]=(), phone_number_type
  Literal["local","toll_free","mobile","national"]|None, limit 1..50 = 10; `.params() -> dict[str, str|list[str]]`),
  `AvailableNumber(phone_number, features: tuple[str,...]` parsed from `[{"name":..}]`, `monthly_cost`,
  `upfront_cost` as Decimal from `cost_information`, `region_information: list[dict]`, best_effort, reservable),
  `NumberOrder(id, status Literal["pending","success","failure"], phone_numbers: tuple[OrderedNumber(id,
  phone_number, status)], connection_id, messaging_profile_id, customer_reference)`, `PhoneNumber(id,
  phone_number, status, connection_id, messaging_profile_id, tags, customer_reference, phone_number_type)`,
  `PhoneNumberMessaging(id, phone_number, messaging_profile_id)`, `MessageCreate` request (`from_` with
  `serialization_alias="from"`, `to`, `text|None`, `media_urls: tuple[str,...]|None` (≤ 10), `subject|None`,
  `messaging_profile_id`, `webhook_url`, `webhook_failover_url|None`, `use_profile_webhooks=False`; model validator:
  text or media_urls required), `Message` response (id, direction, type, from_ phone_number via validator, to
  list statuses, parts, encoding, text, messaging_profile_id).
- `src/va_telnyx/provisioning.py`: mixin `ProvisioningCommands` (attribute `_transport: TelnyxTransport`) with
  `create_call_control_app(name, webhook_url, *, outbound_voice_profile_id) -> CallControlApp`,
  `list_call_control_apps(name_contains) -> list[CallControlApp]`, `update_call_control_app(app_id, *,
  webhook_url=None, outbound_voice_profile_id=None)` (body only given fields: `webhook_event_url`,
  `outbound.outbound_voice_profile_id`; ValueError when none), `create_outbound_voice_profile(name, *,
  whitelisted_destinations=("US","CA"))`, `list_outbound_voice_profiles(name)`, `create_messaging_profile(name,
  webhook_url, *, whitelisted_destinations=("US","CA"))`, `list_messaging_profiles(name)`,
  `update_messaging_profile(profile_id, *, webhook_url)` (PATCH `/messaging_profiles/{id}` `{"webhook_url"}`),
  `get_balance() -> Balance` (GET `/balance`).
- `src/va_telnyx/numbers.py`: mixin `NumberCommands`: `search_numbers(search: NumberSearch) -> list[AvailableNumber]`,
  `create_number_order(phone_numbers: Sequence[str], *, connection_id, customer_reference, messaging_profile_id=None)
  -> NumberOrder`, `get_number_order(order_id)`, `get_phone_number_by_e164(e164) -> PhoneNumber | None`,
  `update_phone_number(number_id, *, connection_id=None, tags=None) -> PhoneNumber`,
  `update_phone_number_messaging(number_id, messaging_profile_id: str | None) -> PhoneNumberMessaging`,
  `delete_phone_number(number_id) -> PhoneNumber`, `number_tags(org_id: uuid.UUID) -> list[str]` (module fn),
  `send_message(message: MessageCreate) -> Message`, `download(url, destination: Path, *, max_bytes=2 GiB) -> int`.
  (messaging + download may live in `messaging.py` if `numbers.py` passes ~250 lines.)
- `src/va_telnyx/client.py`: `class TelnyxClient(ProvisioningCommands, NumberCommands[, MessagingCommands])`; keep
  call commands as is. Mixins type `_transport` as a class-level annotation only (no placeholder-style bodies).
- `src/va_telnyx/factory.py`: `type KeyResolver = Callable[[uuid.UUID], Awaitable[str | None]]`;
  `class UnknownTelnyxAccount(LookupError)`; `class TelnyxClientFactory` with `__init__(self, *, base_url, http,
  limiter, platform_api_key, resolve_key: KeyResolver, metrics=None, clock: Clock | None = None, retry=None,
  tracer_provider=None, key_ttl_s: float = 300.0)`; `platform` property; `async for_account(telnyx_account_id:
  uuid.UUID | None) -> TelnyxClient`; `async for_call(call: _HasConfig) -> TelnyxClient` (`Protocol` with
  `config: Mapping[str, Any]`; value str or UUID, parsed with `uuid.UUID(str(v))`; invalid → ValueError);
  `invalidate(telnyx_account_id)`. Cache entries `(client, expires_at_monotonic)`; `__repr__` hides keys. Clock
  `monotonic()` for expiry (FrozenClock/RecordingClock advance in tests).
- `src/va_telnyx/__init__.py`: export the new models, mixin-free client, factory, `UnknownTelnyxAccount`,
  `TelnyxDownloadTooLarge`, `number_tags`.
- `packages/va_telnyx/pyproject.toml`: add `anyio` dependency (same version range as lock).
- specs/06 §2.2 Decision P03-T08b (above). AGENTS.md bullet (factory, download, list paging, test helpers).

### Tests (packages/va_telnyx/tests/)
- `test_provisioning_exact.py` (respx `telnyx_api`): exact method/path/query/body (full dict equality) for every
  provisioning method incl. `inbound.channel_limit: null`, `concurrent_call_limit: null`, `webhook_event_failover_url
  ""`; update bodies with only given fields; ValueError for an empty update (no request); list paging: two pages
  (`meta.total_pages=2`) concatenated and `page[number]=2` requested; balance parsing to Decimal.
- `test_numbers_exact.py` (respx): search query string exact (repeated `filter[features][]`, `filter[best_effort]=
  false`, omitted unset filters); limit 0/51 and unknown feature → ValueError, route not called; order body exact
  (no `messaging_profile_id` when None); `get_phone_number_by_e164` → None on empty data; update/messaging/delete
  bodies and paths; `number_tags`; send_message body exact (`from` alias, `use_profile_webhooks: false`, None
  omitted); MessageCreate without text/media → ValidationError.
- `test_non_idempotent_retry.py` (respx + RecordingClock): `create_number_order` and `send_message` with 503 → no
  retry, CARRIER_ERROR, one request; ReadTimeout → one request, `TelnyxTransportError`; ConnectError then 200 →
  retried once (sleep 0.1+jitter); `create_call_control_app` 503 not retried; `get_number_order` 503,200 retried;
  rate-limit categories: search uses `rl:telnyx:platform:number_search`, order `number_order`, message
  `messaging` (inspect fakeredis keys), provisioning creates no bucket key.
- `test_download.py` (respx + tmp_path): absolute URL, no `Authorization` header, bytes written equal body, return
  value = size; 503 then 200 retried and file contains only the second body; 404 → TelnyxApiError (not
  CallAlreadyEnded) and no file left; body > max_bytes → TelnyxDownloadTooLarge, file removed, `to_app_error()`
  CARRIER_ERROR with details; relative URL → ValueError; a mid-body `httpx.ReadError` (respx side effect stream
  that raises) retried and file truncated.
- `test_factory.py` (respx two keys or a fake resolver + RecordingClock): platform for `None`/absent config; BYO
  client uses resolver key (Authorization header) and `account == str(id)`; resolver called once within 5 min,
  again after `clock.advance(301)`; concurrent `for_account` (gather 10) → one resolver call; resolver `None` →
  UnknownTelnyxAccount; `invalidate` forces re-resolve; `for_call` with str/UUID id; invalid id → ValueError;
  repr hides keys.
- `test_contract_provisioning.py` (FakeTelnyx via `sim_clients`): create OVP → create app with it → list by
  `name_contains` finds it → update webhook → GET shows it (raw client) → messaging profile create/list exact name/
  update → balance; second account via `POST /sim/telnyx/accounts` + factory BYO client sees none of the platform
  resources (per-key isolation).
- `test_contract_numbers.py` (FakeTelnyx): search US local voice+sms limit 3 → 3 numbers; order → pending; `await
  state.tasks.drain()` (no live streams) → get_number_order success; get_phone_number_by_e164 finds it; update
  connection_id+tags (`number_tags`) → reflected; messaging update → profile id; send_message from it → Message
  queued, sim `/sim/telnyx/messages` lists it; delete → status deleted, lookup now None; `fail_number_orders`
  config → order status failure. Download: dial with `record="record-from-answer"` or record_start on an answered
  call (reuse T08a contract helpers in `test_contract_calls.py` / va_sim telnyx_helpers), hang up, take
  `recording_urls.wav` from the `call.recording.saved` webhook payload, `download` it → valid RIFF/WAV header,
  size equals the sim's served body. The sim URL is absolute on the ASGI app base (`http://sim/...`) — ensure the
  client's http client resolves it through the same ASGITransport (sim_clients' `http` has no base_url; fine).
- Coverage va_telnyx stays ≥ 95 % (aim 100 %).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones — provisioning/number models are field lists similar to the sim's: wrap with `# jscpd:ignore-start/end
(reason)` as models.py already does), `uvx radon cc -s -a packages/va_telnyx/src` (no C+), audit.

### Risks
- client.py size: keep commands in mixins; `TelnyxClient` stays thin.
- respx streaming errors mid-body: use `httpx.Response(200, stream=<custom AsyncByteStream raising ReadError>)`.
- `anyio.open_file` in tests with tmp_path is fine; never `open()` in the event loop (ASYNC230 ruff rule).
- Sim `GET /phone_numbers` with `filter[phone_number]` uses `+` — httpx encodes `+` as `%2B` in params; verify the
  sim decodes it (Starlette does).
- jscpd may flag request/response models vs sim models; prefer distinct structure rather than ignores where easy.

## Build P03-T08b (Builder, 2026-11-01)
Built per plan. Transport: shared `_observed` (span + metrics + limiter) and `_exchange`; new `request_pages`
(one observation per list, page[size]=250, ≤ 20 pages), `download` (`_Download.attempt` streams with
`anyio.open_file`, error bodies ≤ 64 KiB, file removed on any failure), `parse_items`, `segment` (moved from
client.py). Mixins: `provisioning.py`, `numbers.py`, `messaging.py` (send_message + download); models in
`provisioning_models.py`, `number_models.py`, `message_models.py`; `models._Request/_Response` renamed public
`RequestModel/ResponseModel` (ResponseModel now `coerce_numbers_to_str`). `factory.py` TelnyxClientFactory +
UnknownTelnyxAccount. Deviation from plan: provisioning request bodies are literal dicts in the mixin methods
(no request models; exact §2.1 field lists, tested by full-dict equality). `TelnyxDownloadTooLarge` in errors.
anyio added to va-telnyx deps. Test helper `sim_http(state)`. Spec: specs/06 §2.2 Decision P03-T08b; AGENTS.md
bullet. Gates: make fmt/lint/typecheck/placeholders pass; make test 5240 passed (98.27 %), va_telnyx 100 %;
radon avg A (no C+); jscpd 0 clones; spec refs 0; pip-audit clean; pnpm audit 3 moderate (no high).

## Review P03-T08b (Reviewer, 2026-11-01)
Approved. Re-ran make lint/typecheck/placeholders (0 findings), make test (5240 passed, 98.27 %), jscpd 0 clones,
radon no C+, spec refs 0. Checked provisioning create bodies field-for-field against the specs/06 §2.1 rows (nulls
sent), list filters/paging, number search query, order body, PATCH/DELETE paths, send_message vs specs/21 §2 step 6,
non-idempotent retry ops, download (no auth, truncation, cleanup, 64 KiB error body), factory cache/lock/invalidate.
Note for the bootstrap task (P03-T09): specs/21 §5 step 2a says the bootstrap configures the messaging profile with
Telnyx automatic opt-out replies disabled — not part of the §2.1 create body, handle it there.

## Plan P03-T09 (Planner, 2026-11-01)
Task: `va_telnyx` webhooks and media protocol models (plan/phase-03.md). Spec: specs/06 §4.1, §4.2 (signature part
only — the stream-token HMAC is P05-T?? `/ws/telnyx`, specs/28 §6.2, NOT this task), §5 (event list), §8 (media
models), specs/21 §5 (message.received/sent/finalized). `va_telnyx` must never import `va_sim`; tests may.

### Spec decision to write: specs/06 §4.1 `> Decision (2026-11-01): webhook and media verification API (P03-T09).`
1. `verify_webhook(raw_body: bytes, headers: Mapping[str, str], public_key: str | VerifyKey, now: datetime) -> None`
   and `verify_media_ws(stream_url: str, headers, public_key, now) -> None` raise `WebhookVerificationError` with
   `reason` ∈ `missing_headers` | `malformed_timestamp` | `stale_timestamp` | `invalid_signature`; header names
   matched case-insensitively; timestamp must be ASCII digits (unix seconds); `abs(now - ts) > 300` → stale (exactly
   300 accepted; future skew rejected the same way); signature not valid base64 / not 64 bytes / wrong → invalid.
   Verification happens before any JSON parsing; the caller (edge/media) maps every reason to 401 / close 4401 and
   counts `telnyx_webhook_invalid_signature_total` (edge task, not here).
2. `public_key` str = base64 of the 32-byte Ed25519 key (`TELNYX_PUBLIC_KEY`); `load_public_key` raises
   `ValueError` on bad base64/length (config error, at startup).
3. Payload models ignore unknown fields and default every field Telnyx may omit; an unknown `event_type` parses as
   `UnknownEventPayload` (raw dict kept) — never an error. Envelope `meta` optional.
4. Media parsing: `parse_media_message` dispatches by hand on `event`; `media` → `MediaFrame(chunk, timestamp,
   payload)` slots dataclass; unknown `event` → `UnknownMediaEvent(event, raw)`; non-JSON / non-object / missing
   required field / bad base64 → `MediaProtocolError` (a `ValueError`).
(Put a one-line pointer under §8 to rule 4.)

### Files
- `packages/va_telnyx/pyproject.toml`: add `"pynacl>=1.5"` (already in the lock via root dev deps). Check mypy: if
  pynacl lacks types, add a `[[tool.mypy.overrides]] module = "nacl.*"` ignore_missing_imports in root pyproject
  (va_sim already imports nacl — reuse whatever it does).
- `src/va_telnyx/signatures.py`: constants `SIGNATURE_HEADER = "telnyx-signature-ed25519"`, `TIMESTAMP_HEADER =
  "telnyx-timestamp"`, `TOLERANCE_S = 300`; `type VerifyReason = Literal[...]`; `class WebhookVerificationError(
  Exception)` (`reason`, message without secrets); `load_public_key(b64: str) -> nacl.signing.VerifyKey`;
  `verify_webhook(...)`, `verify_media_ws(...)` both via private `_verify(signed_suffix: bytes, headers, key, now)`
  (`f"{ts}|".encode() + suffix`; webhook suffix = raw body bytes, media suffix = stream_url UTF-8). Header lookup:
  `{k.lower(): v for k, v in headers.items()}` (works for dict and Starlette Headers / websockets Headers).
  `now` must be timezone-aware (`ValueError` otherwise). `nacl.exceptions.BadSignatureError` → invalid_signature.
- `src/va_telnyx/webhooks.py`: envelope + dispatch.
  `WebhookMeta(attempt: int | None, delivered_to: str | None)`; `WebhookEvent(record_type: str = "event",
  id: str, event_type: str, occurred_at: datetime, payload: dict[str, Any] = {})`; `WebhookEnvelope(data:
  WebhookEvent, meta: WebhookMeta | None = None)`; `parse_envelope(raw_body: bytes) -> WebhookEnvelope` (orjson +
  model_validate; failures → `WebhookParseError(ValueError)`); `parse_payload(event: WebhookEvent) -> EventPayload`
  via `PAYLOAD_MODELS: Mapping[str, type[TelnyxPayload]]` (unknown → `UnknownEventPayload(event_type, raw)`);
  `VOICE_EVENT_TYPES`, `MESSAGING_EVENT_TYPES` frozensets (= keys).
- `src/va_telnyx/voice_events.py` (all `ResponseModel`-style: extra ignore, frozen, populate_by_name,
  `from_` via `validation_alias="from"`): `CallPayload` base (call_control_id: str, call_leg_id, call_session_id,
  connection_id, client_state, from_, to, direction: str | None ("incoming"/"outgoing" kept as str for tolerance),
  state, start_time: datetime | None, custom_headers: tuple[CustomHeaderValue, ...] = ()); subclasses:
  `CallInitiated`, `CallAnswered`, `CallBridged`, `CallHangup(hangup_cause, hangup_source, sip_hangup_cause,
  end_time)`, `MachineDetectionEnded(result)`, `MachineGreetingEnded(result)`, `PremiumDetectionEnded(result)`,
  `PremiumGreetingEnded(result)`, `CallScreeningDetected` (base + optional `result`), `DtmfReceived(digit)`,
  `RecordingSaved(recording_id, channels, format, recording_started_at, recording_ended_at, recording_urls:
  RecordingUrls(wav: str|None, mp3: str|None), public_recording_urls: RecordingUrls)`, `RecordingError(reason:
  str|None)`, `PlaybackStarted/PlaybackEnded(media_url, overlay: bool|None, status)`, `StreamingStarted/
  StreamingStopped(stream_url)`, `StreamingFailed(stream_id, stream_url, failure_reason)`, `ConferencePayload`
  (conference_id + call fields optional, `reason`), `CallHold`, `CallUnhold`, `DeepfakeResult`, `DeepfakeError`,
  `ConversationCreated`, `ConversationStartFailed` (base only). Every §5 row's event_type maps to one model
  (including `call.machine.premium.call_screening.detected`, `call.deepfake_detection.result|.error`,
  `call.conversation.created|.start_failed`, `call.hold|unhold`, `conference.created|participant.joined|
  participant.left|ended`, `call.recording.error`, `call.dtmf.received`). Builder: cross-check field names against
  the Telnyx webhook docs (developers.telnyx.com) and the FakeTelnyx emitters (`va_sim/telnyx/calls.py payload()`,
  `call_engine.py`, `call_media.py`) — fix the sim only if it violates the documented contract.
- `src/va_telnyx/message_events.py`: `MessageEventPayload(Message)` adds `record_type`, `errors:
  tuple[MessageErrorItem(code, title, detail), ...]`, `media: tuple[MessageMedia(url, content_type), ...]`,
  `cost: MessageCost(amount: Decimal|None, currency)|None`, `received_at/sent_at/completed_at: datetime|None`,
  `webhook_url`, `subject`; mapped for `message.received`, `message.sent`, `message.finalized`. If extending
  frozen `Message` is awkward, use composition, but keep `from_`/`to[0].status` semantics identical.
- `src/va_telnyx/media.py`: the §8 models verbatim (pydantic, extra ignore; `MediaStartInfo.from_` alias "from",
  `tags: tuple[str, ...] = ()`), `@dataclass(slots=True, frozen=True) MediaFrame(chunk: int, timestamp: int,
  payload: bytes)`, `UnknownMediaEvent(event: str, raw: dict)`, `class MediaErrorCode(IntEnum)` UNKNOWN_ERROR=100002,
  MALFORMED_FRAME=100003, INVALID_MEDIA=100004, RATE_LIMIT_REACHED=100005; `type InboundMediaMessage = MediaFrame |
  MediaConnected | MediaStart | MediaDtmf | MediaStop | MediaError | MediaMark | UnknownMediaEvent`;
  `parse_media_message(raw: str | bytes) -> InboundMediaMessage` (orjson.loads; `media` fast path reads
  `frame["media"]["chunk"|"timestamp"|"payload"]`, `int()`, `base64.b64decode(..., validate=True)`; others
  `Model.model_validate(frame)`; `pydantic.ValidationError`/KeyError/TypeError/binascii.Error → MediaProtocolError);
  outbound builders returning `bytes` (orjson): `media_message(payload: bytes)` (ValueError on empty),
  `mark_message(name: str)` (ValueError on empty), `clear_message()`; `segment_mark(turn_index, segment_index) ->
  "seg:<t>:<s>"` and `parse_segment_mark(name) -> tuple[int, int] | None`.
- `src/va_telnyx/__init__.py`: export the public names above.
- specs/06 Decision (above); AGENTS.md bullet (modules, verify API, test vector helpers).

### Tests (packages/va_telnyx/tests/)
- `test_signatures.py`: keypair generated in test (`nacl.signing.SigningKey.generate()`); valid webhook; wrong key;
  tampered body (one byte); stale ts 301 s past and 301 s future → stale; exactly 300 → ok; missing each header →
  missing_headers; non-digit ts → malformed_timestamp; non-base64 / short signature → invalid_signature; upper-case
  header names accepted; naive `now` → ValueError; `VerifyKey` instance and base64 str both accepted;
  `load_public_key` bad base64 / 31 bytes → ValueError. **Fixed test vector**: a committed constant vector built
  from a deterministic seed (`SigningKey(bytes(range(32)))`, fixed ts, fixed body, signature hex literal computed
  once and pinned — compute the hex at build time and hard-code it; verify it also passes with pynacl directly)
  so a change in the signed-string format fails. Media: valid signature over URL with query; URL with query changed
  (`call_id=2`) → invalid; signature over URL without query → invalid. Fuzz: 200 random signatures (parametrize on
  index, generate inside the test with `os.urandom`) all rejected (specs/06 §14.4).
- `test_webhook_models.py`: fixture payload per event type (table-driven, one dict per type built in the test from
  Telnyx-documented shapes) → `parse_payload` returns the expected model class with the expected fields; extra
  unknown keys ignored; missing optional keys default; unknown event type → `UnknownEventPayload` with raw;
  `parse_envelope` on invalid JSON / missing `data.id` → WebhookParseError; `meta` optional;
  `set(PAYLOAD_MODELS)` ⊇ every `event_type` in the specs/06 §5 table (parse the table from the spec file, like
  other spec-mirroring tests) ∪ {message.received, message.sent, message.finalized}.
- `test_contract_webhooks.py` (FakeTelnyx, `telnyx_helpers.telnyx_state(RecordingTransport)` from va_sim tests
  support, or the `sim_clients` helper): drive a call through dial → answer → AMD premium → playback start/stop →
  record start → streaming start (to the `media_agent` fixture) / stop → conference join/leave → hangup, plus send a
  message and inject an inbound message (`POST /sim/telnyx/messages/inbound`); for every delivered webhook:
  `verify_webhook(raw body, headers, sim public key, now=sim clock time)` passes, `parse_envelope` +
  `parse_payload` gives a non-Unknown model, `client_state` round-trips through `va_telnyx.client_state.decode`,
  recording_urls.wav is absolute. A tampered copy of one delivered body fails verification.
- `test_media_models.py`: parse every §8 inbound sample (connected/start/media/dtmf/stop/error/mark) with extra
  fields tolerated; `media` → MediaFrame with ints and decoded bytes; unknown event → UnknownMediaEvent; malformed:
  not JSON, JSON array, missing `media.payload`, non-int chunk, bad base64, missing `start.media_format` →
  MediaProtocolError; outbound builders produce exact JSON (`orjson.loads` equality) and reject empty;
  `segment_mark`/`parse_segment_mark` round trip and reject `seg:x:1`/`other`; MediaErrorCode values.
- `test_contract_media.py` (FakeTelnyx `MediaSession` → `media_agent` fixture): answered call with streaming_start
  to the agent; `verify_media_ws(agent full URL incl. query, agent handshake headers, sim public key, now)` passes
  (and Bearer header equals `stream_auth_token` when given); every frame the agent received parses via
  `parse_media_message` (connected, start with media_format PCMU/8000, media frames with increasing chunk, dtmf
  after `queue_dtmf`, stop on hangup); agent sends `media_message(160 μ-law bytes)`, `mark_message(segment_mark(1,0))`
  → sim echoes a `mark` that parses to `MediaMark` with that name; `clear_message()` accepted (no `error` frame).
  Respect AGENTS gotchas: no `state.tasks.drain()` with a live stream; assert order/counts not virtual ms.
- Coverage va_telnyx stays 100 % (≥ 95 %).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4
--silent` (media models vs va_sim media_frames may look similar — keep structures distinct; ignore markers only with a
reason), `uvx radon cc -s -a packages/va_telnyx/src` (no C+; split `parse_media_message` dispatch into a dict of
per-event parsers), audit (`uvx pip-audit` on `uv export`, `corepack pnpm audit`).

### Risks
- pydantic `datetime` parsing of Telnyx `occurred_at` with 6-digit fraction + `Z`: fine; `start_time` may be absent.
- Media fast path must not go through pydantic (spec says hand-written dispatch); keep it O(1) per frame.
- Fixed vector: generate the pinned signature with the deterministic seed during the build, then hard-code it; never
  commit a private key string other than the obviously-test `bytes(range(32))` seed (gitleaks).
- Two `telnyx_client(...)`/media streams in one test hang (AGENTS gotcha) — one stream per test.

## Build P03-T09 (Builder, 2026-11-01)
Built per plan. New modules: `signatures.py` (verify_webhook / verify_media_ws / load_public_key, reasons
missing_headers|malformed_timestamp|stale_timestamp|invalid_signature; `_verify` split into header/freshness/decode
helpers for radon A), `voice_events.py` (one model per specs/06 §5 row, 27 types), `message_events.py`
(`MessageEventPayload(Message)` + media/errors/cost/timestamps), `webhooks.py` (envelope, PAYLOAD_MODELS,
UnknownEventPayload, WebhookParseError), `media.py` (§8 models, MediaFrame fast path, UnknownMediaEvent,
MediaErrorCode, outbound builders, segment marks). pynacl added to va-telnyx deps. Spec: specs/06 §4.1 Decision
P03-T09 (4 rules) + pointer under §8; AGENTS.md bullet. Tests: test_signatures (pinned vector from seed
bytes(range(32)), 200-case fuzz), test_webhook_models (parses the §5 table, ⊆/= checks), test_media_models,
test_contract_webhooks (call lifecycle incl. premium AMD+beep, playback, conference, recording; streaming; SMS
sent/finalized/received — every FakeTelnyx webhook verifies + parses non-Unknown; tampered body rejected),
test_contract_media (handshake signature + Bearer, every frame parses, dtmf, media+mark echo, clear, stop).
Test helpers: `telnyx_client_helpers.provisioned(...)` and `sms_number(...)` (dedup with test_contract_numbers).
Deviation: none of substance; `number_order.complete` is correctly Unknown (not a voice/messaging event).
Gates: make fmt/lint/typecheck/placeholders pass; make test 5558 passed (98.30 %), va_telnyx 100 %; radon avg A,
no C+; jscpd 0 clones; spec refs 0; pip-audit no known vulns; pnpm audit 3 moderate (no high); gitleaks pass.

## Review P03-T09 (Reviewer, 2026-11-01)
Rejected (1 finding). make lint/typecheck/placeholders pass (0 findings). Models vs specs/06 §5 rows / §8 and the
Decision text checked; contract tests verify real FakeTelnyx webhooks/media frames; fixed vector + fuzz present.

1. `packages/va_telnyx/src/va_telnyx/signatures.py` `_check_fresh`: an attacker-controlled `telnyx-timestamp` made of
   many ASCII digits escapes the verifier with a non-`WebhookVerificationError` exception, so the edge would answer
   500 instead of 401 (specs/06 §4.1 Decision P03-T09 rule 1: every failure is a `WebhookVerificationError` with
   one of the four reasons). Reproduced: `'9'*400` → `OverflowError: int too large to convert to float`
   (`now.timestamp() - int(ts)`); `'9'*5000` → `ValueError: Exceeds the limit (4300 digits)`. Fix: give the digit
   string a length cap (for example ≤ 12 digits, otherwise `malformed_timestamp`), or compare in integers
   (`abs(int(now.timestamp()) - ts)`) after the cap, and record the cap in Decision rule 1. Add tests for both inputs
   (400 and 5000 digits) that expect `WebhookVerificationError` with the chosen reason, for `verify_webhook` and
   `verify_media_ws`.

## Build P03-T09 fix (Builder, 2026-11-01)
Review finding 1 fixed: `_check_fresh` rejects timestamps longer than 12 ASCII digits as `malformed_timestamp`
(`_MAX_TIMESTAMP_DIGITS`), so `int()`/float overflow can no longer escape. Decision rule 1 in specs/06 §4.1 now says
"1–12 ASCII digits". Tests: `test_oversized_timestamps_are_malformed_not_crashes` (13/400/5000 digits × verify_webhook
and verify_media_ws; 6 fail without the fix) + 12-digit timestamp → stale. Gates: fmt/lint/typecheck/placeholders
pass, make test 5565 passed (98.30 %), spec refs 0, radon avg A, jscpd 0.

## Review P03-T09 re-review (Reviewer, 2026-11-01)
Approved. Finding 1 fixed (12-digit cap → malformed_timestamp, Decision rule 1 updated, tests for 13/400/5000 digits on
both verifiers). Re-ran: lint/typecheck/placeholders pass, make test 5565 passed (98.30 %), spec refs 0.

## Plan P03-T10 (Planner, 2026-11-01)
`va_elevenlabs` TTS multi-context WebSocket client (specs/09 §1-§3, §8, §11, §12; sim contract specs/30 §3.3
Decision P03-T04 + P03-T05c rule 9). New workspace package `packages/va_elevenlabs` (dist `va-elevenlabs`, module
`va_elevenlabs`). Client builds every wire message itself from specs/09 §3 — never import `va_sim` from src (tests may).

### Spec decision to write: specs/09 §11 `> Decision (2026-11-01): TTS WebSocket client (P03-T10).`
1. Class `va_elevenlabs.tts_ws.TtsSocket` implements the §1 `StreamingTts` protocol with the §1 method names
   (`open/start_context/send/flush/cancel/audio/close`; the plan's `open_context/send_text/close_context` names map to
   these). Also an async context manager (`async with TtsSocket(...) as tts` → `open` on enter is NOT implied: enter
   only guarantees `close()` on exit). Socket voice = the `open()` voice; `start_context(id, voice)` with a different
   `provider_voice_id` → `ValueError` (caller opens a second socket, §2); a different `settings` is sent in that
   context's init only.
2. Logical vs wire ids: callers only ever see the logical `context_id`; after reconnect n the wire id is
   `{id}-r{n}` (§11) and audio for it is reported under the logical id.
3. Clause confirmation: a clause is confirmed when the cumulative count of non-whitespace alignment `chars` received
   for the context exceeds the non-whitespace length of all earlier clauses (alignment `null` confirms nothing,
   except that the context's first audio chunk always confirms clause 0). Reconnect resends exactly the unconfirmed
   clauses (each with its original `flush` flag, then a bare flush if the context had been flushed).
4. End of utterance: when every clause of a flushed context is confirmed and no audio for it has arrived for
   `tail_grace_s` (0.3 s, injected clock), the client sends `close_context`; the server `isFinal` yields
   `TtsAudio(context_id, b"", None, is_final=True)`. Audio arriving for a non-cancelled context before `isFinal` is
   forwarded. Only cancelled contexts drop audio (`va_tts_late_audio_dropped_total`). After `cancel` the `isFinal`
   still yields `is_final=True` (callers know the server freed the slot). A context's `is_final` is emitted once.
5. Max 3 contexts (`ka` + 2): `start_context` with 2 non-`ka` contexts open cancels the oldest first.
6. Keepalive: `ka` initialised at open with `{"text": " ", "context_id": "ka"}` (no voice settings); every
   `keepalive_s` (15) of injected-clock time with no non-`ka` context open, send `{"text": "", "context_id": "ka"}`.
7. Failure classification (`tts_errors.classify_close(code, reason)`, `classify_error(error)`):
   close 1008 reason `Invalid API key` → `TtsAuthError` (fatal); error frame or close reason `voice_not_found` →
   `TtsVoiceNotFoundError` (fatal, caller switches voice); other 1008 closes starting `invalid ` → `TtsProtocolError`
   (fatal); error frames `context_closed|invalid_message|max_active_conversations` → `TtsProtocolError` (fatal, our
   bug); `quota_exceeded`/`insufficient_credits`... unknown error codes → `TtsServerError` recorded
   (`va_vendor_errors_total{vendor="elevenlabs_tts", code=<error>}`) and handled by the reconnect path when the socket
   closes; inactivity close (reason starts `Have not received`), 1011, 1012, 1006/abnormal, connect errors → retry.
   A fatal error is raised from the `audio()` iterator (and from later `send`/`flush`) and stops the socket.
8. Reconnect: delays 50/100/200 ms (injected clock); a failed attempt = connect error or a new socket that closes
   before delivering any audio; 3 consecutive failed attempts → `TtsUnavailableError` (raised from `audio()`). A
   reconnect is attempted on every unexpected close (keep warm), also with no active context. Each reconnect
   increments `va_vendor_ws_reconnects_total{vendor="elevenlabs_tts"}`. No audio for a context within
   `no_audio_timeout_s` (3.0) after its first flush → reconnect path (same accounting).
9. Metrics via the media-worker catalog (`va_tts_ttfa_ms{model}` first text sent → first audio per context,
   reset on reconnect only if no audio yet; `va_tts_characters_total{model}` characters of every text frame;
   `va_vendor_ws_reconnects_total`; `va_vendor_errors_total`; new `va_tts_late_audio_dropped_total` counter, added to
   specs/27 §3.3 and `va_common.metrics` CATALOG). Spans: `elevenlabs.tts.connect` per attempt (attrs
   `elevenlabs.voice_id`, `elevenlabs.model_id`, `elevenlabs.attempt`) and `elevenlabs.tts.context` per context
   (`elevenlabs.context_id`, `elevenlabs.characters`, `elevenlabs.ttfa_ms`, `elevenlabs.reconnects`, ends at is_final
   / cancel / close; error status on fatal).
10. Timeouts: connect `open_timeout_s` 5, each send `send_timeout_s` 5 (a send timeout = socket failure → reconnect
    path), close handshake 2 s. `websockets` client with `proxy=None`, `additional_headers={"xi-api-key": key}`.

### Files
- `packages/va_elevenlabs/pyproject.toml` (deps: `websockets>=17.0`, `orjson`, `pydantic>=2.9`,
  `opentelemetry-api`, `prometheus-client`, `va-common`, `va-domain`; uv_build; `[tool.uv.sources]` workspace).
  `src/va_elevenlabs/py.typed`. Root `pyproject.toml`: dev group `"va-elevenlabs"`, sources entry, mypy_path
  `packages/va_elevenlabs/src:packages/va_elevenlabs/tests/support`, mypy files src+tests, pytest pythonpath
  `packages/va_elevenlabs/tests/support`. `uv lock`. Mirror whatever else va_telnyx registered (grep the repo for
  `va_telnyx` in Makefile/CI/coverage config).
- `src/va_elevenlabs/tts_models.py`: `type OutputFormat = Literal["ulaw_8000", "pcm_16000"]`; `TtsVoice`
  (`provider_voice_id`, `model_id = "eleven_flash_v2_5"`, `settings: va_domain.voices.VoiceSettings = VoiceSettings()`,
  `language_code: str | None = None`, frozen); `ChunkAlignment(chars: tuple[str, ...], char_start_ms: tuple[int, ...],
  char_duration_ms: tuple[int, ...])` frozen; `TtsAudio(context_id, audio: bytes, alignment: ChunkAlignment | None,
  is_final: bool = False)` frozen; `StreamingTts` Protocol (§1 verbatim); `@dataclass(frozen=True) TtsConfig(ws_base,
  api_key, zero_retention=False, inactivity_timeout=180, chunk_length_schedule=(50, 90, 120, 150), keepalive_s=15.0,
  no_audio_timeout_s=3.0, tail_grace_s=0.3, reconnect_delays_s=(0.05, 0.1, 0.2), open_timeout_s=5.0,
  send_timeout_s=5.0)` (`repr=False` on api_key).
- `src/va_elevenlabs/tts_wire.py` (pure): `socket_url(config, voice, fmt) -> str` (path `/v1/text-to-speech/{voice}/
  multi-stream-input`, query in §2 table order: model_id, output_format, sync_alignment=true, auto_mode=false,
  inactivity_timeout, apply_text_normalization=auto, language_code only when set, enable_ssml_parsing=false,
  enable_logging=false only when zero_retention; voice id URL-quoted; ws_base trailing slash tolerated);
  `init_context(wire_id, settings, schedule) -> bytes`, `keepalive_init() -> bytes`, `keepalive() -> bytes`,
  `text_message(wire_id, text, *, flush) -> bytes` (`normalize_clause(text)`: strip trailing whitespace + exactly one
  space; `flush` key only when true; empty/whitespace text → ValueError), `flush_message`, `close_context_message`,
  `close_socket_message` (orjson bytes, key order as §3); `parse_frame(raw: str | bytes) -> AudioFrame | FinalFrame |
  ErrorFrame` (slots dataclasses; `AudioFrame(wire_id, audio bytes (b64 decoded, validate=True), alignment
  ChunkAlignment|None)` from `audio`/`contextId`/`alignment{chars,charStartTimesMs,charDurationsMs}`; `FinalFrame(
  wire_id)`; `ErrorFrame(error, message, code)`; anything else / bad base64 / mismatched alignment lengths →
  `TtsProtocolError`). Audio frames with empty `audio` string are ignored by the socket (not an error).
- `src/va_elevenlabs/tts_errors.py`: `TtsError(Exception)` (`code: str`), subclasses `TtsAuthError`,
  `TtsVoiceNotFoundError`, `TtsProtocolError`, `TtsServerError`, `TtsUnavailableError`; `type Disposition =
  Literal["retry", "fatal"]`; `classify_close(code: int | None, reason: str) -> TtsError | None` (None = retry),
  `classify_error(frame: ErrorFrame) -> TtsError` + `is_fatal(err)`. Messages never contain the API key.
- `src/va_elevenlabs/tts_context.py` (pure): `Clause(text, flush, confirmed=False)`; `ContextState(logical_id,
  settings, schedule)` with `wire_id`, `rewire(n)`, `add(text, flush)`, `mark_flushed()`, `confirm(alignment|None) ->
  int` (newly confirmed), `unconfirmed() -> list[Clause]`, `complete() -> bool` (flushed and all confirmed),
  `cancelled`, `final_emitted`, `first_text_at`, `first_audio_at`, `last_audio_at`, `characters`.
- `src/va_elevenlabs/tts_metrics.py`: `TtsMetrics` Protocol (`ttfa(model, ms)`, `characters(model, n)`,
  `reconnect()`, `vendor_error(code)`, `late_dropped()`), `CatalogTtsMetrics(ServiceMetrics)` using the catalog
  names, `NullTtsMetrics`.
- `src/va_elevenlabs/tts_ws.py`: `TtsSocket(config, *, clock: Clock = SystemClock(), metrics: TtsMetrics =
  NullTtsMetrics(), tracer_provider=None, connect=websockets.asyncio.client.connect)`. Internals: one reader task +
  one timer task (keepalive / tail grace / no-audio watchdog; wakes every `min(pending deadlines)` via
  `clock.sleep`), both created in `open()`, cancelled in `close()`; a done-callback turns an unexpected task crash into
  the terminal error. Output `asyncio.Queue[TtsAudio | TtsError]`; `audio()` is an async generator that raises the
  terminal error and ends after `close()`. Send lock around every socket write. Reconnect logic in a private method
  used by both reader (on close) and watchdog. `close()` sends `close_socket`, waits ≤ 2 s for the server close,
  then closes; idempotent. Keep each method radon ≤ B (split helpers).
- `src/va_elevenlabs/__init__.py`: export public names.
- `packages/va_common/src/va_common/metrics.py`: add `_c("va_tts_late_audio_dropped_total", ...)` to the
  media-worker list; specs/27 §3.3 row `va_tts_late_audio_dropped_total | counter | —`.
- `packages/va_common/tests/clock_helpers.py`: `ManualClock(FrozenClock)` — `sleep(s)` parks a future until
  `advance()` passes its deadline (`sleep(0)` yields once); `async def run_for(seconds)` advances and yields a few
  loop turns; `sleepers` count for asserting a timer is parked (never poll with real sleeps).
- specs/09 §11 Decision (above); AGENTS.md bullet (package, modules, test helpers, gotchas found).

### Tests (packages/va_elevenlabs/tests/; support `tests/support/tts_client_helpers.py`)
Support: `RecordingTtsServer` (websockets `serve` on 127.0.0.1:0; records every connection's path/query/headers and
JSON messages into `asyncio.Queue`s; `next_message(timeout)` with a wall-clock bound; test can `send_frame`,
`close(code, reason)` the current connection, refuse further connections); `sim_tts(state)` = `tts_helpers.served`
+ a `TtsConfig(ws_base=url.replace("http","ws"), api_key=...)`; `collect(tts, until=...)` reads `audio()` with a
wall timeout. conftest re-exports `isolated_env`/`reset_logging` like va_telnyx.
- `test_tts_wire.py`: exact bytes/JSON for every §3 message (compare with the spec's literal JSON examples parsed
  from specs/09 §3 code blocks so the spec stays the oracle), clause normalization (`"Hi"`→`"Hi "`, `"Hi  \n"`→
  `"Hi "`, empty → ValueError), URL query (order, language_code present/absent, enable_logging only under zero
  retention, quoted voice id), `parse_frame` audio/alignment/null alignment/isFinal/error/garbage/bad base64/length
  mismatch/non-object.
- `test_tts_context.py`: confirmation by non-whitespace alignment counts across chunk boundaries, null alignment,
  first chunk confirms clause 0, `unconfirmed`, `rewire` ids `t7-0-r1`, `t7-0-r2`, `complete`.
- `test_tts_errors.py`: classification table (every row of Decision rule 7).
- `test_tts_protocol.py` (RecordingTtsServer + ManualClock): message sequence snapshot (ka init, context init with
  voice_settings + `generation_config.chunk_length_schedule [50,90,120,150]`, text frames, bare flush,
  close_context on cancel, close_socket on close) + `xi-api-key` header + URL query; keepalive cadence (no message
  before 15 s, one at 15 s, two at 30 s, none while a context is open, resumes after its isFinal); max contexts
  (third start sends close_context for the oldest); voice mismatch → ValueError; tail grace → close_context only
  after `tail_grace_s` of silence once all clauses confirmed; is_final emitted once; cancel then late audio frame →
  dropped (metric) and not yielded; no-audio watchdog: flushed, no audio, advance 3 s → second connection with
  `t1-0-r1` init and the resent clause; error frame `quota_exceeded` → vendor error metric then reconnect on close;
  `context_closed` error → TtsProtocolError from `audio()`; send after fatal raises; malformed frame → TtsProtocolError;
  3 failed reconnects (server refuses) → TtsUnavailableError; metrics (ttfa observed once per context, characters,
  reconnects) read from a `build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))` registry;
  spans with `memory_provider()`.
- `test_tts_contract.py` (FakeElevenLabs `realtime_state()` + `served`): open + two flushed clauses → μ-law audio
  under the logical id, alignment chars reconstruct the text, `is_final` after completion; sim
  `/sim/elevenlabs/tts/requests` shows contexts `ka` and `t1-0` with the expected text/model/output_format;
  `pcm_16000`; cancel during a `delay` fault → no audio after cancel, is_final, record `interrupted`;
  **reconnection mid-utterance**: arm `disconnect` with `after_chunks` = first chunk of clause 2 (clause 1 = 13 chars
  × 60 ms → 4 chunks, so `after_chunks=4`) → client reconnects, sim records a `t1-0-r1` context whose text is only
  clause 2, audio covers both clauses, reconnect metric 1; `error` fault → reconnect; `disconnect` fault with
  `after_chunks=0, times=4` → TtsUnavailableError; unknown voice → TtsVoiceNotFoundError; allow-list without the key
  → TtsAuthError; close() → record end_reason `close_socket`.
- Coverage va_elevenlabs ≥ 95 % (aim 100 %).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(the client's wire builders vs sim tts code must stay structurally distinct), `uvx radon cc -s -a
packages/va_elevenlabs/src` (no C+), audit (`uvx pip-audit` on `uv export`, `corepack pnpm audit`).

### Risks
- Sim `close_context` drops the in-flight tail (rule 6), so close_context only after tail grace; do not shorten.
- Accelerated sim clock + real sockets race: contract tests use `realtime_state()`; client timers use their own
  injected clock (SystemClock in contract tests, ManualClock in protocol tests).
- websockets 17 honours proxy env vars — pass `proxy=None`. `filterwarnings=error`: close every connection/server
  in tests (unclosed sockets raise ResourceWarning).
- Reconnect + cancellation races: all socket writes under one lock; a reconnect in progress must queue `send`s
  (await an `_connected` event) rather than writing to the dead socket.
- `va_tts_late_audio_dropped_total` catalog addition may be asserted by an existing metrics test parsing specs/27
  §3.3 — update both together.

## Build P03-T10 (Builder, 2026-11-01)
New package `packages/va_elevenlabs` (tts_models, tts_wire, tts_context, tts_errors, tts_metrics, tts_ws) +
tests (wire vs specs/09 §3 JSON examples, context, errors, metrics, 31 protocol tests on RecordingTtsServer +
ManualClock, 8 FakeElevenLabs contract tests incl. mid-utterance reconnect resending only clause 2 as `t1-0-r1`).
Deviations from the plan, written into specs/09 §11 Decision P03-T10:
- `flush(id)` marks end of utterance (chunker flushes every clause, so flush flags cannot); close_context follows
  after 0.3 s quiet (3 s if alignment did not cover every visible char).
- Reconnect attempt failure = cannot connect OR socket closes before audio/keepalive (bounds a close-on-connect
  loop); initial socket loss does not count; 3 consecutive failures → TtsUnavailableError.
- Frames that are objects with none of audio/isFinal/error are ignored (not fatal); ttfa not reset on reconnect.
- Contexts closed by us or complete when their socket died emit is_final locally instead of replay.
Also: `va_tts_late_audio_dropped_total` in catalog + specs/27 §3.3 (test_metrics count 56→57); ManualClock +
settle in va_common/tests/clock_helpers.py; StreamingTts Protocol wrapped in jscpd ignore (verbatim spec).
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 5665 passed 98.28 %, va_elevenlabs 97 %,
spec refs 0, jscpd 0 clones, radon avg A (no C), pip-audit no known vulns, pnpm audit 3 moderate (pre-existing).

## Review P03-T10 (Reviewer, 2026-11-01)
Approved. Re-ran: make lint/typecheck/placeholders exit 0 (0 findings), make test 5665 passed 98.28 %
(va_elevenlabs tts_ws 96 %, others 99-100 %), spec refs 0, jscpd 0 clones, radon avg A (no C), package suite 3x
stable (100 passed, ~4.4 s). Checked against specs/09 §1-§3, §8, §11: URL query order, exact message shapes,
ka init/keepalive, max 2 utterance contexts + ka, cancel drops late audio (new metric in specs/27 §3.3 +
catalog), -r{n} replay of unconfirmed clauses only, 50/100/200 ms reconnect with 3-failure limit, 3 s
watchdog. API key only in header (TtsConfig repr=False). Looked at a close()-during-reconnect leak race:
not reachable (the reader cannot be parked inside _attach with _ws unset).

## Plan P03-T11 (Planner, 2026-11-01)
Task: `va_elevenlabs` realtime STT WebSocket client (specs/08 §1-§7, §9 metering counter, §11 tests; simulator
contract = specs/30 §3.3 Decision P03-T05a). Spec beats plan wording: the class is `ElevenLabsRealtimeStt` in
`stt_realtime.py` (spec path), the plan's `SttSession` name is not used. Out of scope: `regions` (EU base URL,
needs org data_region wiring, later phase), batch STT (P03-T12), engine-side `call.error` emission (the client
yields warning/error `SttEvent`s; the media pipeline maps them later), live smoke (P03-T16).

### Spec decision to write: specs/08 §7 `> Decision (2026-11-01): realtime STT client (P03-T11).` Number the rules:
1. `ElevenLabsRealtimeStt(conn: SttConnection, *, clock, metrics, tracer_provider)` implements §1 `StreamingStt`
   (models in `stt_models`; `va_voice` does not exist yet, so the Protocol lives in va_elevenlabs like
   `StreamingTts`). `open(cfg)` connects (`xi-api-key` header, `proxy=None`, open_timeout 2 s, `ping_interval=None`,
   `max_size=2**22`, `compression=None`), waits for `session_started` and verifies the echoed `audio_format`,
   `sample_rate`, `commit_strategy == "manual"` (mismatch → `SttProtocolError`, fatal). Connect errors retry
   50/100/200 ms (then `SttUnavailableError`); handshake 401/403 → `SttAuthError`.
2. `SttConfig.include_language_detection` must equal `language_code is None` (validator; §3 derives the param);
   `sample_rate` must match `audio_format` (8000/16000).
3. Batching: `send_audio(frame)` takes whole codec frames (PCM16 byte count even, else `ValueError`), accumulates
   and sends one message once ≥ `batch_ms` (100) of audio is buffered. `commit()` / `send_audio(..., commit=True)`
   sends the buffer with `"commit": true`, or one 20 ms codec-silence frame (`0xFF`×160 / zeros×640) when empty.
   Every chunk carries `sample_rate`; `previous_text` only on the first chunk of each session.
4. Commit throttle: a commit within 250 ms of the previous one is deferred to `last + 0.25 s` (audio keeps flowing
   in normal batches meanwhile); further commits inside the window merge into the deferred one.
   `commit_throttled` from the server → resend a commit 250 ms later (same deferral path).
5. Commits outstanding are a FIFO (EOT time, replay mark). `committed_transcript` opens a 300 ms fallback window;
   `committed_transcript_with_timestamps` within it emits `SttEvent(committed)` with `words` (type `word` only,
   `start`/`end` → `start_s`/`end_s`, `logprob`) and `language_code`; when the window expires, the plain text is
   emitted with `words=[]`. A late timestamp variant after the fallback is dropped. `va_stt_final_ms` = commit()
   call → committed event. `committed_transcript_entities` re-emits the most recent committed event with equal text
   as `SttEvent(committed, entities=[...], is_update=True)` (new field `is_update: bool = False`, an extension of §1:
   the Literal kinds stay as specified); no match → logged and dropped. `edited_transcript` and
   `insufficient_audio_activity` ignored; unknown message types logged and ignored.
6. Server message actions (§4 table): `warning` → `SttEvent(warning, error_type="stt_warning", text=error)`;
   `auth_error` → `SttAuthError`; `quota_exceeded` → `SttQuotaError`; `unaccepted_terms` → `SttTermsError` (all
   fatal); `invalid_request` → reconnect once with keyterms=[] and entity_detection=[]; a second `invalid_request`
   → `SttProtocolError` fatal; `rate_limited`, `resource_exhausted`, `transcriber_error` → reconnect with
   200/400/800 ms backoff; `error`, `session_time_limit_exceeded`, any other close/network error → reconnect with
   50/100/200 ms; `queue_overflow` → batch 200 ms for the rest of the client's life; `chunk_size_exceeded` → batch
   50 ms (the server dropped that message; nothing resent); `input_error` → logged at error, counted, continue.
   Every server error message_type is counted `va_vendor_errors_total{vendor="elevenlabs_stt", code}`. Closes with
   no preceding error frame: 1008 reason `Invalid API key`/`Missing API key` → `SttAuthError`; 1008 `invalid …` →
   same as `invalid_request`; everything else reconnects. Malformed frames (not JSON, not an object, missing
   `message_type`, bad field types) → `SttProtocolError` fatal. A fatal error yields `SttEvent(error,
   error_type=<code>)`, ends `events()`, and is raised from later `send_audio`/`commit`.
7. Reactive reconnect (§7): a replay buffer keeps every audio byte sent since the last *acknowledged* commit
   (commit whose transcript arrived) plus frames produced during the gap, capped at `replay_max_s` (2.0 s, oldest
   dropped). After the new `session_started` it is sent in `batch_ms` messages, the first carrying
   `previous_text` = last 300 chars of context text (committed transcripts + `add_context(text)` from the engine);
   if commits were outstanding one commit is re-sent after the replay. Up to 3 attempts; an attempt fails when it
   cannot connect or its socket closes before `session_started`; 3 consecutive failures → `SttUnavailableError`.
   Each attempt counts `va_vendor_ws_reconnects_total{vendor="elevenlabs_stt"}`; each reconnect yields
   `SttEvent(warning, error_type="stt_reconnect")`.
8. Proactive rotation: `rotation_due` (session age ≥ `rotate_after_s`, default 1500) is read by the engine, which
   calls `rotate()` when the user is silent: open a second socket, wait for its `session_started`, switch the sender
   (first chunk carries `previous_text`), keep reading the old socket for `rotate_drain_s` (0.5) then close it.
   Events of both sockets go to the same queue. A failed rotation leaves the old session in use and yields a
   `stt_warning`.
9. Metering/observability: `audio_seconds` = caller audio accepted by `send_audio` (not replays, not silence
   commits). Spans `stt.session` (whole client life, attr `va.stt.reconnects`) and `elevenlabs.stt.connect` per
   attempt (`elevenlabs.model_id`, `elevenlabs.attempt`, `elevenlabs.rotation`).

### Files (packages/va_elevenlabs/src/va_elevenlabs/)
- `stt_models.py`: `type SttAudioFormat = Literal["ulaw_8000", "pcm_16000"]`; pydantic frozen `SttWord(text,
  start_s, end_s, logprob: float | None)`, `SttEntity(text, entity_type, start_char, end_char)`, `SttConfig`
  (§1 fields, defaults: ulaw_8000/8000, language_code None, include_language_detection True, keyterms [],
  filter_background_audio False, entity_detection [], previous_text None; validators rule 2), `SttEvent` (§1 +
  `is_update`); `StreamingStt` Protocol verbatim (jscpd ignore if needed); `@dataclass(frozen=True) SttConnection(
  ws_base, api_key (repr=False), model_id="scribe_v2_realtime", zero_retention=False, keyterms_encoding:
  Literal["repeated","json"]="repeated", keepalive_interval_ms=5000, batch_ms=100, min_commit_interval_s=0.25,
  timestamp_grace_s=0.3, reconnect_delays_s=(0.05,0.1,0.2), backoff_delays_s=(0.2,0.4,0.8), replay_max_s=2.0,
  rotate_after_s=1500.0, rotate_drain_s=0.5, open_timeout_s=2.0, send_timeout_s=5.0)`.
- `stt_wire.py` (pure, built from specs/08 §3/§4 independently of va_sim): `realtime_url(conn, cfg, *, minimal=False)`
  (path `/v1/speech-to-text/realtime`; query in §3 table order: model_id, audio_format, commit_strategy=manual,
  include_timestamps=true, include_language_detection, language_code (when set), keyterms (repeated or one
  `keyterms=<json array>`), filter_background_audio true|false, entity_detection repeated (omitted when empty),
  keepalive_interval_ms, enable_logging=false only under zero_retention, no_verbatim=false; trailing slash on
  ws_base tolerated; `urlencode(..., quote_via=quote_plus)`); `audio_chunk(audio, *, commit, sample_rate,
  previous_text=None) -> str` (key order as §4 example; previous_text key only when given); `silence_frame(fmt)`;
  `bytes_per_ms(fmt)`; `parse_message(raw) -> SessionStarted | Partial | Committed | CommittedWords | Entities |
  ServerError | Ignored` (slots dataclasses; `ServerError(message_type, error)` for every §4 error/warning type;
  `Ignored(message_type)` for edited_transcript/insufficient_audio_activity/unknown; malformed → `SttProtocolError`).
- `stt_errors.py`: `SttError(code, message)`, `SttAuthError`, `SttQuotaError`, `SttTermsError`,
  `SttProtocolError`, `SttUnavailableError`; `type SttAction = Literal["fatal", "reconnect", "backoff",
  "minimal", "warn", "shrink", "grow", "throttled", "log", "ignore"]`; `ACTIONS: Mapping[str, SttAction]` (every §4
  row, test parses the §4 table and checks each row has an entry); `fatal_error(message_type, message) -> SttError`;
  `classify_close(code, reason) -> SttError | Literal["minimal"] | None`.
- `stt_metrics.py`: `SttMetrics` Protocol (`final_ms(ms)`, `reconnect()`, `vendor_error(code)`),
  `CatalogSttMetrics(ServiceMetrics)` (`va_stt_final_ms`, `va_vendor_ws_reconnects_total`, `va_vendor_errors_total`
  with vendor `elevenlabs_stt`), `NullSttMetrics`. To keep jscpd at 0: factor the shared vendor counters into one
  small `ws_metrics.VendorCounters(metrics, vendor)` used by both `CatalogTtsMetrics` and `CatalogSttMetrics`.
- `stt_buffer.py` (pure): `AudioBatcher(fmt, batch_ms)` (`add(frame) -> bytes | None`, `drain() -> bytes`,
  `resize(batch_ms)`); `ReplayBuffer(fmt, max_s)` (`append(chunk)`, `mark() -> int` at commit, `ack(mark)` drops
  through a mark, `chunks(batch_ms) -> list[bytes]`, `dropped_ms`).
- `stt_commits.py` (pure): `CommitTracker(min_interval_s, grace_s)`: `request(now) -> float | None` (None = send
  now, else deferred deadline; merges), `sent(now, mark)`, `plain(text, now)` (opens fallback), `with_words(...)`
  → resolved commit info (eot time, mark) or None (late), `expired(now)` → fallback commits, `outstanding`,
  `next_deadline()`.
- `stt_realtime.py`: `ElevenLabsRealtimeStt` (reader task per socket + one timer task for deferred commit /
  fallback / rotation drain; send lock; `asyncio.Queue[SttEvent | None]`; `events()` async generator; `close()`
  idempotent, closes sockets with 1000 and cancels tasks). Properties `session_id`, `reconnects`,
  `audio_seconds`, `rotation_due`; methods `add_context(text)`, `rotate()`. Keep every method radon ≤ B. Shared
  connect plumbing with `tts_ws` (header, proxy=None, auth-status → auth error, retryable exception tuple) goes into
  `ws_connect.py` if it would otherwise duplicate (jscpd).
- `languages.py` (§5): `LanguageMapping(scribe_code: str | None, detect: bool, tts_code: str | None,
  tts_choices: frozenset[str] | None)`; `LANGUAGE_MAP` (every §5 row and alias), `TTS_SUPPORTED` (32 flash v2.5
  codes: en ja zh de hi fr ko pt it es id nl tr fil pl sv bg ro ar cs el fi hr ms sk da ta uk ru hu no vi);
  `resolve_language(value) -> LanguageMapping` raising `AppError("INVALID_INPUT", ..., field="language")`;
  `tts_language(mapping, detected) -> str` (babel: detected if in TTS_SUPPORTED else en; auto {en,es}; fluent
  {en,es,de,fr,pt,it}; fixed codes outside TTS_SUPPORTED → en); `LanguageLock` (`observe(code) -> str | None`:
  first two committed codes agree → locked, else latest; `locked` property).
- `keyterms.py` (§6): `Keyterm(text, boost)`; `parse_keyword(item) -> Keyterm` (`word` / `word:boost`, boost int
  1..10 default 2; ValueError on invalid); `merge_keyterms(sources: Sequence[Iterable[str]], *, cap=100) ->
  KeytermSet(terms: tuple[str,...], glossary: tuple[str,...], warnings: tuple[str,...])` (validate < 50 chars,
  ≤ 5 words, none of `<>{}[]\`; dedupe case-insensitively keeping the highest boost at the first position; stable
  sort by boost desc; cap; glossary = boost ≥ 5); `glossary_line(glossary) -> str | None`
  ("Important names and terms: a, b").
- `__init__.py`: export the new public names; package description in pyproject mentions STT.
- specs/08 §7 Decision (above); specs/08 header line already names `stt_realtime.py`. AGENTS.md bullet.

### Tests (packages/va_elevenlabs/tests/)
Support: generalise `RecordingTtsServer` into a protocol-neutral `ws_server_helpers.RecordingWsServer` (records
connections/messages, `reject_status`, an `on_message` hook) used by both the TTS helpers and a new
`stt_client_helpers.py` (`session_started(cfg_overrides)` frame builder, `auto_start=True` answers each connection
with `session_started` echoing the query, `committed(text)`/`committed_words(text, words)` builders, `rig(url,
**conn)` on `ManualClock` + media-worker registry + memory spans, `collect_events(stt, until)`). Avoid jscpd
clones between TTS and STT helpers by sharing.
- `test_stt_wire.py`: URL snapshots for option combinations (language set/auto, keyterms repeated vs json,
  entity_detection, zero_retention, filter on/off, pcm_16000, minimal), chunk JSON vs the §4 example parsed from the
  spec code block, previous_text omitted when None, parse of every server type (+ malformed cases).
- `test_stt_errors.py`: parses the specs/08 §4 table; every message_type has an `ACTIONS` entry with the rule-6
  action; close classification table.
- `test_stt_buffer.py`, `test_stt_commits.py`: batching 100/200/50 ms, silence frame sizes, replay cap/ack marks,
  throttle defer/merge, fallback expiry, late timestamp variant dropped.
- `test_stt_models.py`: validators (detection vs language_code, sample_rate vs format).
- `test_languages.py`: every §5 row parsed from the spec table maps as specified (aliases incl.); unknown →
  AppError INVALID_INPUT field language; tts_language for babel/auto/fluent; TTS_SUPPORTED has 32 entries; lock
  logic (agree → lock, disagree → latest, then third agreeing pair).
- `test_keyterms.py`: boost parsing, invalid drops with warnings, dedupe, sort, cap 100 (and 1000), glossary.
- `test_stt_protocol.py` (RecordingWsServer + ManualClock): header + URL; batching 5 frames per message;
  previous_text only on first chunk; commit with empty buffer sends one silence frame; throttle window (second
  commit after 100 ms sent at 250 ms, third merged); every §4 server message row handled as rule 6 (fatal → error
  event + iterator ends + send raises; reconnect rows → second connection, warning `stt_reconnect`, replay buffer
  resent with previous_text and outstanding commit re-sent; backoff rows wait 200 ms on the ManualClock;
  invalid_request → minimal URL then fatal on repeat; commit_throttled → resend at +250 ms; queue_overflow → 200 ms
  messages; chunk_size_exceeded → 50 ms messages; input_error continues; warning event); session_started mismatch
  fatal; committed fallback after 300 ms vs timestamp variant; entities update `is_update`; 3 failed reconnects →
  SttUnavailableError event; handshake 401 → SttAuthError; rotation (new socket, switch, old closed after 0.5 s,
  events from old socket during drain delivered); metrics (`va_stt_final_ms` observed per commit, reconnect and
  vendor error counters) and spans.
- `test_stt_contract.py` (FakeElevenLabs `realtime_state()` + `tts_helpers.served`, audio from
  `stt_helpers.caller_audio`): open + utterance + commit → committed text with words; partials arrive; pcm_16000;
  sim `/sim/elevenlabs/stt/sessions` shows keyterms/entity_detection/previous_text; entity detection → is_update
  event with `credit_card_number`; fault `session_time_limit_exceeded` mid-utterance (after_ms inside the utterance)
  → reconnect, sim shows 2 sessions, committed text equals the full utterance (no audio lost); `commit_throttled`
  fault → commit resent and transcript arrives; `auth_error` fault / wrong key → SttAuthError; `quota_exceeded` →
  error event; `disconnect` with times=4 → SttUnavailableError; rotation between two utterances → each committed on
  its own session, nothing lost.
- Coverage va_elevenlabs ≥ 95 %.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones: watch TTS/STT metrics, connect plumbing, test servers), `uvx radon cc -s -a packages/va_elevenlabs/src`
(no C+), audit (`uvx pip-audit` on `uv export`, `corepack pnpm audit`).

### Risks
- Sim commits resolve only after 100 ms voiced + 260 ms marker window, and a commit does not force an unresolved
  segment: contract tests commit well after the utterance ends (≥ 300 ms silence) — read AGENTS.md P03-T05a.
- Contract tests on the wall clock (`realtime_state()`); client timers on SystemClock there, ManualClock in protocol
  tests (use `wait_for_sleep` before `run_for` when a timer starts after socket I/O).
- Replay after reconnect must not re-send `previous_text` past the first chunk (server input_error).
- `filterwarnings=error`: close every socket/server; never leave the reader task pending at test end.
- Refactoring `RecordingTtsServer`/`CatalogTtsMetrics` must keep the 100 existing TTS tests green.

## Build P03-T11 (Builder, 2026-11-01)
Implemented per plan, with these deviations, all written into the specs/08 §7 Decision (P03-T11, rules 1-10):
- `SttAction` has no `ignore`; ignored types are `stt_wire.IGNORED_TYPES`. `test_stt_errors` checks that the §4 rows
  (21) equal data types ∪ ignored ∪ `ACTIONS`.
- Reconnect replay re-sends the collapsed outstanding commit at its mark (`ReplayBuffer.split(mark)`), not after
  the whole replay. Plain transcripts still waiting for their variant are emitted before the replay.
- `SttConnection.healthy_after_s` (1 s): a session that dies before any transcript frame and within 1 s counts as a
  failed attempt (needed so `disconnect` x4 → unavailable, like TTS `_healthy`). `context_chars=300`,
  `close_timeout_s=2.0` were added too.
- One `stt_reconnect` warning per reconnect (not per attempt); each attempt counts the reconnect metric.
- Shared plumbing to keep jscpd at 0: `ws_tasks` (run_timer/nap/crash_of/stop_tasks, now used by `tts_ws` too),
  `ws_metrics.VendorCounters` (CatalogTtsMetrics refactored onto it), tests: `ws_server_helpers.RecordingWsServer`,
  `rig_helpers.Observed`, `contract_helpers.SimClient`/`sim_served` (test_tts_contract refactored onto them).
Gates: make fmt/lint/typecheck/placeholders 0; make test 5828 passed (TOTAL 98%, va_elevenlabs stt_realtime 94%,
other new modules 100%); spec refs 0 problems; jscpd 0 clones; radon avg A (2.47), no C+; pip-audit no known vulns;
pnpm audit 3 moderate (below high, unchanged; no TS touched).

## Review P03-T11 (Reviewer, 2026-11-01)
Approved. Re-ran: make lint/typecheck/placeholders 0; make test 5828 passed (stt_realtime 94%, other new modules 100%);
check_spec_refs 0; jscpd 0 clones; radon no C+. Diff checked against specs/08 §1-§7: URL query order, chunk shape,
previous_text first-chunk-only, 100 ms batching/silence commit, 250 ms throttle merge, 300 ms timestamp fallback,
every §4 row (ACTIONS + IGNORED_TYPES), §5 table, §6 keyterms, §7 replay cap/attempts/rotation all match.
Not blocking (for P04 engine work): an answer for a commit sent on a rotated-out session that never arrives
within the 0.5 s drain stays in the outstanding FIFO. The engine only rotates during silence, which keeps that unlikely.

## Plan P03-T12 (Planner, 2026-11-01)
`va_elevenlabs` REST client `ElevenLabsRest` (specs/15 header names `va_elevenlabs.rest.ElevenLabsRest`; specs/09
names `tts_rest.py`, specs/08 names `stt_batch.py`). Layout like va_telnyx: transport + mixins.
Phrase cache (Redis, P04-T11), catalog sync / clone workflow (P09), S3 artifact + call_events (P12-T01) are OUT of
scope; this task delivers only the vendor calls and pure response helpers they need.

### Spec decision to write: specs/15 §7 `> Decision (2026-11-01): ElevenLabs REST client (P03-T12).` Numbered rules:
1. Module map: `rest_transport.ElevenLabsTransport` (auth/retry/errors/metrics/spans), `tts_rest.TtsRestCommands`,
   `voices_rest.VoiceCommands`, `stt_batch.BatchSttCommands`; `rest.ElevenLabsRest` combines the three mixins.
   `ElevenLabsRest(api_key, base_url, http, *, clock, rng, metrics, tracer_provider, retry)`; the caller owns the
   `httpx.AsyncClient` built with `va_common.http.http_client` (timeouts are per operation, rule 3). `repr` hides
   the key. Header `xi-api-key`; `Accept: application/json` except audio routes.
2. Operations (method name = metric/span `operation`): `tts_convert(voice_id, text, *, model_id, output_format,
   voice_settings=None, language_code=None, apply_text_normalization="auto") -> TtsRestAudio(audio, content_type,
   character_count, request_id)` (`POST /v1/text-to-speech/{id}?output_format=`); `tts_with_timestamps(...same) ->
   TtsTimestamped(audio: bytes, alignment: RestAlignment|None, normalized_alignment|None)` (`.../with-timestamps`,
   decodes `audio_base64`, alignment arrays equal length else `ElevenLabsProtocolError`); `list_voices(*,
   category=None, search=None, voice_type=None, page_size=100) -> list[VendorVoice]` follows `next_page_token`
   until `has_more=false` (cap 100 pages → `ElevenLabsProtocolError` if exceeded; always
   `include_total_count=false`); `get_voice(id)`; `add_voice(name, files: Sequence[VoiceFile], *, description=None,
   labels=None, remove_background_noise=False) -> AddedVoice(voice_id, requires_verification)`
   (`POST /v1/voices/add`); `edit_voice(id, name, files=None, *, description=None, labels=None,
   remove_background_noise=False)` (`POST /v1/voices/{id}/edit`; description is always sent when given — vendor
   clears an absent description, documented); `get_voice_settings(id) -> VoiceSettings` (va_domain.voices);
   `update_voice_settings(id, settings: VoiceSettings)` (`POST /v1/voices/{id}/settings/edit`, full JSON);
   `delete_voice(id, *, missing_ok=True)` (404 tolerated per specs/15 §3.4 → returns False); `batch_transcribe(
   source: AudioUpload | SourceUrl, BatchOptions) -> BatchTranscript`.
3. Timeouts and retries (per operation, `RestRetry(delays_s)` + uniform 0-100 ms jitter, `Retry-After` replaces a
   shorter delay, > cap stops): reads (`list_voices`, `get_voice`, `get_voice_settings`) timeout 10 s, retry
   0.5/1/2 s on 429/5xx/transport; `tts_*` 10 s, one retry after 0.2 s (callers fall back to the socket, specs/09
   §9); `update_voice_settings` 10 s, 0.5/1/2 s (idempotent full write); `delete_voice` 10 s, 0.5/1/2 s;
   `add_voice`/`edit_voice` 60 s, NO client retry (non-idempotent; VoiceCloneWorkflow retries 2/4/8/16/32 s,
   specs/15 §3.2); `batch_transcribe` 300 s, retries 5 s/20 s on 429/5xx/transport (specs/08 §8).
4. Errors (`rest_errors.py`): `ElevenLabsError(message, operation)` base with `to_app_error()`;
   `ElevenLabsApiError(status, code, message, detail)` parses both vendor shapes — `{"detail": {"status",
   "message"}}` (code = status) and 422 `{"detail": [{"loc","msg","type"}]}` (code `validation_error`, message =
   first `msg`) — plus non-JSON bodies (code `http_<status>`). Subclasses: `ElevenLabsAuthError` (401/403),
   `ElevenLabsNotFound` (404 / `voice_not_found`), `ElevenLabsRateLimited` (429 after retries),
   `ElevenLabsTransportError` (timeout/network after retries), `ElevenLabsProtocolError` (malformed success body).
   `to_app_error` per specs/15 §7: auth → `INTERNAL_ERROR`; 422 and 400 (`voice_limit_reached`, `invalid_sample`,
   …) → `VOICE_CLONE_FAILED` with `details.provider_message`; 404 → `VOICE_NOT_FOUND`; 429/5xx/transport →
   `UPSTREAM_TIMEOUT`. Messages never contain the key.
5. Multipart: built by httpx `files=`/`data=`; fields in this order: `name`, `description`, `labels` (compact
   JSON object), `remove_background_noise` (`true|false`), then one `files` part per sample (`VoiceFile(filename,
   data: bytes, content_type)`; `VoiceFile.from_path(path)` reads with anyio). Batch: `model_id`, `file` (or
   `cloud_storage_url` for `SourceUrl`), `use_multi_channel`, `multichannel_output_style`,
   `timestamps_granularity`, `tag_audio_events`, `diarize`, `num_speakers` (when set), `language_code` (when set),
   repeated `keyterms` (≤ 1 000, `ValueError` beyond), repeated `entity_detection`; query `enable_logging=false`
   only when `zero_retention`. `BatchOptions.for_recording(model_id, *, stereo=True, language_code, keyterms,
   entity_detection, zero_retention)` applies specs/08 §8 (stereo: multichannel `combined`, or `separate` when
   entity_detection is non-empty per the specs/08 §8 Decision; mono: diarize, num_speakers=2) — note
   `detect_speaker_roles` is NOT sent (the sim ignores unknown fields; record in the Decision that it is sent
   only for mono and test it via respx).
6. `BatchTranscript(language_code, text, words: tuple[BatchWord,...], entities, transcription_id,
   audio_duration_secs)` normalises all three response shapes; `separate` is merged by (start, channel_index,
   original order) — same as the sim's `combined`. `BatchWord(text, start, end, type, speaker_id, channel_index,
   logprob)`; spacing entries kept in `words`, dropped by `utterances()`. Pure `group_utterances(words, gap_s=0.7)
   -> tuple[Utterance(role, text, start, end, words)]`: words only (type `word`), sorted by start, role
   `user` for channel 0 / `assistant` for channel 1 (mono: `speaker_0` user, `speaker_1` assistant), new
   utterance on role change or gap > 0.7 s; text joined with single spaces.
7. Metrics (specs/27 §3.6 table rows added): `va_elevenlabs_requests_total{operation, outcome}` (outcome
   ok/client_error/auth_error/rate_limited/server_error/transport_error) and `va_elevenlabs_request_duration_ms
   {operation}` (LAT_MS, retries included), `ElevenLabsRestMetrics(registry)` one per registry. Span
   `elevenlabs.<operation>` CLIENT with `elevenlabs.operation`, `elevenlabs.voice_id` (when present),
   `http.status_code` (last), `elevenlabs.attempts`; error status when raising.

### Files
- `packages/va_common/src/va_common/http.py`: move `retry_after_seconds(value, now)` here (public); `va_telnyx.retry`
  re-exports it (keep `from va_telnyx.retry import retry_after_seconds` working; tests unchanged). Avoids a jscpd clone.
- `packages/va_elevenlabs/pyproject.toml`: add `httpx>=0.28`, `anyio>=4`; description mentions REST.
- New in `packages/va_elevenlabs/src/va_elevenlabs/`: `rest_errors.py`, `rest_metrics.py`, `rest_retry.py`
  (`RestRetry`, per-op constants), `rest_transport.py` (`RestOperation(name, method, timeout_s, retry)`,
  `ElevenLabsTransport.request_json/request_bytes/…` with one `_observed` wrapper), `rest_models.py` (pydantic:
  `VendorVoice` (voice_id, name, category, labels, description, preview_url, settings: VoiceSettings|None,
  verified_languages: list[VerifiedLanguage], samples: list[VendorSample]|None, created_at_unix) with
  `extra="ignore"`; `AddedVoice`, `VoiceFile`, `TtsRestAudio`, `RestAlignment`, `TtsTimestamped`),
  `tts_rest.py`, `voices_rest.py`, `stt_batch.py` (models `BatchOptions`, `AudioUpload`, `SourceUrl`,
  `BatchWord`, `BatchTranscript`, `Utterance`, `group_utterances`), `rest.py` (`ElevenLabsRest`).
  `__init__.py` exports. Keep radon ≤ B per function.
- specs/15 §7 Decision; specs/27 §3.6 two rows + one sentence in its Decision naming `va_elevenlabs`;
  specs/08 §8 one-line pointer to the Decision for `BatchOptions.for_recording`.
- `packages/va_telnyx/tests/test_observability.py::_spec_rows`: filter rows starting with `va_telnyx_`.
- AGENTS.md bullet (P03-T12).

### Tests (packages/va_elevenlabs/tests/, support `tests/support/rest_client_helpers.py`: `rest_rig(transport)`
→ ElevenLabsRest on ManualClock/RecordingClock + fresh registry + memory spans; `sim_rest()` async cm = ElevenLabsRest
over `httpx.ASGITransport` on the FakeElevenLabs Starlette app of `realtime_state()` (reuse
`contract_helpers.SimClient`/`sim_served` patterns), plus the raw sim client for `/sim/*`)
- `test_rest_errors.py`: both detail shapes, non-JSON, status→subclass, `to_app_error` table of specs/15 §7.
- `test_rest_transport.py` (respx): header `xi-api-key`, key not in repr/errors; retry 429/5xx/transport per op
  with delays recorded on the clock incl. jitter bound and Retry-After; add/edit never retried; batch 5/20 s;
  final error mapping; metrics outcomes + duration; spans attributes/error status; spec §3.6 rows parsed.
- `test_rest_multipart.py` (respx capture): decode the request body with `multipart` (python-multipart, already a
  dev dep via va-sim) or `email.parser.BytesParser`: field order/values for add/edit (labels JSON, booleans,
  N file parts with filename + content type), batch fields (repeated keyterms/entity_detection, cloud_storage_url
  vs file, enable_logging query only under zero retention, mono vs stereo `for_recording`, separate when entities),
  keyterms > 1000 → ValueError.
- `test_stt_batch_models.py`: parsing single / separate / combined fixtures; separate merge order; group_utterances
  (role change, 700 ms gap boundary: exactly 0.7 s stays, 0.701 splits, spacing dropped, mono speaker mapping).
- `test_rest_contract.py` (FakeElevenLabs): tts_convert ulaw_8000 length = 60 ms/char × 8 bytes, x-character-count;
  with_timestamps alignment chars equal the text; unknown voice → ElevenLabsNotFound / VOICE_NOT_FOUND;
  list_voices page_size=3 walks all 11 premade via tokens (category=premade) and search filter; add_voice with 2
  WAV samples (use `voice_helpers.wav_sample` if importable via pythonpath, else build WAV with stdlib `wave`) →
  get_voice shows 2 samples with sim-detected mime; edit_voice with new files replaces samples; settings get/update
  round trip; delete clone ok, delete again → False (404 tolerated), delete premade → ElevenLabsApiError
  `voice_not_editable`; add with invalid (short) sample → `invalid_sample` → VOICE_CLONE_FAILED; voice limit
  → voice_limit_reached; tts fault `error` once → retried and succeeds; batch: dual-channel WAV built from
  `stt_helpers.caller_audio` (caller ch0) + a REST TTS rendering (agent ch1) → `combined` transcript words with
  channel 0/1, group_utterances gives user then assistant; batch fault `rate_limited` once → retried after 5 s
  (ManualClock or RecordingClock-backed sleeps; contract can use a clock whose sleep records and returns);
  sim `/sim/elevenlabs/stt/batch` record shows keyterms/entity_detection/enable_logging.
- Coverage va_elevenlabs ≥ 95 %; new modules ~100 %.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones — watch ElevenLabsRestMetrics vs TelnyxMetrics and `_observed` vs va_telnyx transport; factor into
va_common if flagged), `uvx radon cc -s -a packages/va_elevenlabs/src` (no C+), audit (`uvx pip-audit` on
`uv export`, `corepack pnpm audit`).

### Risks
- httpx `files=` with `data=` puts data fields before files — matches rule 5 order; test it.
- httpx `data=` alone is urlencoded: batch with `SourceUrl` must still be multipart (send fields as
  `files=[(name,(None,value))]`, AGENTS.md P03-T05b).
- REST TTS costs `tts_ttfa_ms` of sim time; contract tests on `realtime_state()` (ttfa 0).
- Retry sleeps must go through the injected clock (no wall sleeps in tests).
- `filterwarnings=error`: close every httpx client/ASGI app.

## Build P03-T12 (Builder, 2026-11-01)
Implemented per plan; deviations (all written into specs/15 §7 Decision P03-T12 rules 1-7 + specs/27 §3.6 Decision):
- Transport API is `call(op, path, parse, ...)` (parse runs inside the span/metric so malformed bodies count as
  `client_error`) and `observed(op, work)` + `Exchange.send` for multi-request ops (paged `list_voices` = one
  observation; `elevenlabs.attempts` sums every request incl. pages). No `request_json`; `no_body` parser for
  edit/settings/delete.
- No `retry` ctor parameter: schedules are fixed per `RestOperation` (`rest_retry` READ/TTS/BATCH/NO_RETRY);
  Retry-After cap 30 s.
- Errors renamed `ElevenLabsNotFoundError` / `ElevenLabsRateLimitedError` (ruff N818). Unlisted 4xx →
  INTERNAL_ERROR; VOICE_CLONE_FAILED details carry provider_code + provider_message.
- `BatchOptions.for_recording(..., redact=bool)` (instead of an entity list): redact → entity_detection pii,pci +
  `separate`. `detect_speaker_roles` sent only for mono. BatchEntity keeps channel_index for separate.
- jscpd: shared `obs_helpers.vendor_metric_rows(prefix)` (telnyx test refactored onto it),
  `contract_helpers.control_records`, `rig`/`api` fixtures in va_elevenlabs tests/conftest.py.
Gates: make fmt/lint/typecheck 0; placeholders 0 findings; spec refs 0; make test 5924 passed (TOTAL 98%, all
new va_elevenlabs modules 100%); jscpd 0 clones; radon avg A (2.48), no C+; pip-audit no known vulns;
pnpm audit 3 moderate (no high; no TS touched).

## Review P03-T12 (Reviewer, 2026-11-01)
Rejected. Re-ran: make lint/typecheck/placeholders 0; make test 5924 passed (new REST modules 100%). Diff checked
against specs/15 §2-§7, specs/08 §8, specs/09 §9, specs/27 §3.6: endpoints, multipart order, retry schedules,
error mapping, metrics rows all match. One finding:
1. packages/va_elevenlabs/src/va_elevenlabs/stt_batch.py `_entities` (the `int(item.get("start_char", 0))` /
   `int(item.get("end_char", 0))` lines): a malformed entity offset escapes as a raw `TypeError` (`"start_char": null`)
   or `ValueError` (`"start_char": "x"`), verified with `parse_transcript({"text":"a","words":[],"entities":[{"start_char":None}]})`.
   That breaks the specs/15 §7 Decision P03-T12 rule 4 contract (a malformed success body is
   `ElevenLabsProtocolError` → `INTERNAL_ERROR`). The specs/27 §3.6 Decision (a malformed body counts as
   `client_error`) breaks too: `ElevenLabsTransport.observed` only catches `ElevenLabsError`, so the metric records
   outcome `ok` and the span has no error status for an operation that raised. Fix: validate offsets like
   `_number` does (int, not bool; reject otherwise with `ElevenLabsProtocolError(_OPERATION, "entities[].start_char
   is not an integer")`), and add bad-offset rows to the `test_stt_batch_models` malformed parametrize. Also make
   `entity_type`/`text` strict (don't `str()` None into "None"). Optionally harden `observed` so any non-ElevenLabs
   `Exception` from `parse` gets wrapped as a protocol error, so a future parser slip can't record `ok`.

## Build P03-T12 fix (Builder, 2026-11-01)
Finding 1 fixed: `stt_batch._entities` now validates via `_entity_text` (str required for `text`/`entity_type`) and
`_entity_offset` (int, not bool, for `start_char`/`end_char`) → `ElevenLabsProtocolError`. Hardened
`ElevenLabsTransport.call`: TypeError/ValueError/KeyError/IndexError from `parse` → `ElevenLabsProtocolError(op,
<exc name>)` (so metrics `client_error` + span error). specs/15 §7 Decision rule 4 sentence added. Tests: 7 new
malformed-entity rows in test_stt_batch_models; test_rest_transport: malformed batch entity counts client_error +
error span, parametrized parser-slip test.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, spec refs 0, make test 5936 passed (TOTAL 98%,
rest_transport/stt_batch 100%), jscpd 0 clones, radon no C+.

## Review P03-T12 re-review (Reviewer, 2026-11-01)
Approved. Re-ran lint/typecheck/placeholders 0, make test 5936 passed (98%, REST modules 100%), spec refs 0, jscpd 0.
Finding 1 fixed: `_entity_text`/`_entity_offset` raise ElevenLabsProtocolError; `transport.call` wraps parser slips.

## Plan P03-T13a (Planner, 2026-11-01)
P03-T13 split (DEC-006): T13a = core + non-streaming `complete`; T13b = streaming dialogue/speculation. Out of scope
here: `stream_dialogue`, structured calls/embeddings (T14), prompts/window (phase 05), the `PUT /v1/org/llm-provider`
route + SSRF check (API phase; the probe function is delivered here), usage_events DB writes (billing phase).

### Spec decision to write: specs/10 §1 `> Decision (2026-11-01): va_llm core (P03-T13a).` Numbered rules:
1. Package map (`packages/va_llm`, dist `va-llm`, deps: `openai>=2`, `httpx>=0.28`, `pydantic>=2.9`,
   `opentelemetry-api`, `prometheus-client`, `orjson`, `va-common`, `va-db`): `settings` (`LlmSettings`
   mixin), `models` (`ChatMessage`, `ToolDef`, `ToolCall`, `TokenUsage`, `Completion`), `providers`
   (`LlmProvider`, `platform_provider(settings)`, `ProviderRegistry`, `probe_provider`), `org_providers`
   (`OrgProviderStore`, `encrypt_org_api_key`), `policy` (`CallType`, `CallPolicy`, `POLICIES`), `breaker`,
   `errors`, `pricing`, `metering`, `metrics`, `deadline`, `gateway` (`LlmGateway`). Never import `va_sim`.
2. Clients: one `openai.AsyncOpenAI(base_url, api_key, timeout=httpx.Timeout(connect=2, read=30, write=5,
   pool=2), max_retries=0, http_client=<shared>)` per provider, cached by `(provider.id, base_url,
   sha256(api_key))` so a rotated org key gets a new client; the caller owns the shared
   `va_common.http.http_client(settings, timeout=..., http2=True, max_connections=500, max_keepalive=100)`.
   `prewarm()` sends `GET {base_url}/models` for the platform provider (errors logged `llm.prewarm_failed`, not
   raised). Every request carries `X-VA-Call-Id` when a call id is given (FakeLLM rule matching, specs/10 §12).
3. Org providers: `OrgProviderStore(database, envelope, *, clock, ttl_s=60)` reads `org_llm_providers` in
   `database.tenant_session(org_id)`, decrypts `api_key_enc` with AAD `org_llm_providers.api_key_enc:<row id>`
   (specs/28 §2 format), builds `LlmProvider(id="org:<uuid>", models=row.models, supports_tool_choice=...)`;
   cache (hits and misses) for `ttl_s` on `clock.monotonic()`; `invalidate(org_id)`. `ProviderRegistry(platform,
   org_store | None)`: `resolve(org_id) -> LlmProvider` = org provider when present else platform.
4. Probe: `probe_provider(provider, http) -> ProbeResult(supports_tool_choice, error)`: one non-streaming
   `chat.completions.create` with `max_tokens=16` (not 1: a forced tool call's arguments cannot fit in one
   token, real providers and FakeLLM truncate it), tool `va_probe` (`{"type":"object","properties":{"ok":
   {"type":"boolean"}}}`) forced by name, `temperature=0`, 10 s timeout. Tool call present → `(True, None)`;
   2xx without a tool call, or 400/422 → retry once without tools: success → `(False, None)`; any other failure
   → `(False, "<kind>: <vendor message>")` (key never included).
5. Call policies (§2.1 as data, `POLICIES: Mapping[CallType, CallPolicy]`): `CallType = Literal["dialogue",
   "router", "loop_condition", "extraction", "voicemail", "guard_rails", "analysis"]`; role model: dialogue →
   `dialogue`, analysis → `analysis`, others → `router` (override with `model=`). Timeout is the TOTAL budget of
   the call on the primary target (retries included): router/loop/voicemail 1.5 s, guard_rails 2 s,
   extraction 3 s, dialogue 2.5 s (T13a non-streaming uses the TTFT budget; T13b adds the 20 s total), analysis
   60 s PER attempt. Retries: classifiers/router/extraction → 1 immediate retry on retryable errors; analysis →
   2 retries after 2 s and 8 s (timeouts retryable too), then the fallback model; dialogue → no retry, straight
   to the fallback model. Retryable = 500/502/503/504, connect errors, and 429 whose `Retry-After` ≤ 1 s (sleep
   it first; absent header = 0); 429 with Retry-After > 1 s fails over immediately. 4xx other than 429 never
   retried. A classifier timeout is final (no retry). Exhausted → `LlmUnavailableError` (callers apply their
   documented fallback; T13a does not implement heuristics).
6. Fallback target: platform request → `(platform, platform.models["fallback"])`; org request → the platform
   provider's fallback model ("if org provider fails, platform provider"). Only dialogue and analysis fall back;
   one attempt, its own breaker; skipped when it equals the primary target.
7. Breaker (`CircuitBreaker(clock, failures=5, window_s=30, open_s=20)`, one per `(provider_id, model)` in
   `BreakerBoard`): failures = timeouts, 5xx, connect errors; any HTTP answer < 500 (2xx, 429, other 4xx)
   counts as success because the server answered; closed → open on the 5th failure within 30 s (sliding deque of monotonic times); open → half-open
   after 20 s; half-open admits exactly 1 probe (concurrent callers are rejected); probe success → closed (window
   cleared), failure → open again for 20 s. `allow() -> bool` and `record(success: bool)`; transitions logged
   `llm.breaker_transition` (provider, model, from, to) and exported as gauge `va_llm_breaker_state{provider,
   model}` 0 closed / 1 half_open / 2 open. Open primary → dialogue/analysis go to fallback, others raise
   `LlmUnavailableError(reason="breaker_open")` without a request.
8. Errors (`errors.py`): `LlmError(message, *, provider_id, model, kind)` with `kind` ∈ `timeout|http|parse`
   (the `va_llm_errors_total` label) and `to_app_error()` → `AppError("UPSTREAM_TIMEOUT")`; subclasses
   `LlmTimeoutError`, `LlmHttpError(status)` (`LlmRateLimitedError` for 429), `LlmConnectionError` (kind http),
   `LlmProtocolError` (kind parse: malformed body / no choices), `LlmUnavailableError(reason, last: LlmError |
   None)`. Map openai exceptions in one function (`APITimeoutError` → timeout, `APIConnectionError` → connection,
   `APIStatusError` → http with `status_code` and `response.headers["retry-after"]`, `APIResponseValidationError`
   → protocol). Messages never contain the API key.
9. Usage + metering: `TokenUsage(prompt_tokens, completion_tokens, estimated=False)` from the response `usage`;
   absent → offline estimate `ceil(len(text.encode())/4)` per message text (+4 per message) and per completion
   text + tool-call arguments, `estimated=True` (tiktoken comes with `window.py`, DEC-006). `pricing.MODEL_COSTS`
   built-in `{model: ModelCost(in_per_1k_micros, out_per_1k_micros)}` for the 4 specs/02 §6 models + embeddings
   model (provisional: GLM-5.3-Flash 50/200, GLM-5.3 600/2200, Kimi-K2.6 600/2500, Qwen3-Embedding-8B 20/0),
   `load_costs(settings.llm_model_costs_json)` merges the JSON over them (invalid JSON or shape → `ValueError` at
   startup); unknown model → cost 0 + log `llm.unpriced_model` once per model. `cost_micros = ceil(tokens * per_1k
   / 1000)` per direction. `UsageRecord(org_id, call_id, call_type, provider_id, model, prompt_tokens,
   completion_tokens, estimated, cost_in_micros, cost_out_micros)`; `UsageRecorder` Protocol `async
   record(UsageRecord)`; the gateway records every successful completion (including fallbacks); a recorder
   exception is logged `llm.usage_record_failed` and swallowed.
10. Metrics: va_llm takes `LlmMetrics(registry, catalog: ServiceMetrics | None = None)`: registers the new
   specs/27 §3.6 rows in `registry` — `va_llm_requests_total{call_type, model, outcome}` (outcome
   ok/timeout/rate_limited/server_error/client_error/transport_error/parse_error/breaker_open, one per attempt),
   `va_llm_request_duration_ms{call_type}` (LAT_MS, one per `complete`, retries + fallback included),
   `va_llm_breaker_state{provider, model}` gauge, `va_llm_speculation_total{outcome}` (registered now, used by
   T13b); when `catalog` (media-worker `ServiceMetrics`) is given it also feeds §3.3 `va_llm_tokens_total{model,
   direction}` and `va_llm_errors_total{model, kind}` (T13b adds `va_llm_ttft_ms`). Add a specs/27 §3.6 Decision
   sentence for va_llm like the ElevenLabs one. Span per `complete`: `llm.<call_type>` CLIENT with
   `llm.call_type`, `llm.provider`, `llm.model` (final), `llm.attempts`, `llm.fallback` (bool),
   `llm.tokens_in`/`llm.tokens_out`, `http.status_code` (last, when any); error status when raising.
11. `LlmGateway(registry: ProviderRegistry, http: httpx.AsyncClient, *, clock, metrics, recorder=None,
   costs=MODEL_COSTS, tracer_provider=None)`; `async complete(call_type, *, org_id, messages, tools=(),
   tool_choice=None, temperature=0.0, max_tokens=512, model=None, call_id=None, response_format=None) ->
   Completion(content, tool_calls, finish_reason, usage, provider_id, model, attempts, fell_back)`. Deadlines
   run on the injected clock (`deadline.within(clock, seconds, coro)` races the request task against
   `clock.sleep` and cancels the loser) so ManualClock tests are exact; `aclose()` closes nothing it does not own.

### Files
- `packages/va_llm/pyproject.toml` (+ `src/va_llm/py.typed`, `__init__.py` exports), root `pyproject.toml`:
  dev group `va-llm` + `[tool.uv.sources] va-llm = { workspace = true }`, mypy `files`
  (`packages/va_llm/src`, `packages/va_llm/tests`) and `mypy_path` (`packages/va_llm/src`,
  `packages/va_llm/tests/support`), pytest `pythonpath` += `packages/va_llm/tests/support`; `uv lock`.
- `src/va_llm/{settings,models,providers,org_providers,policy,breaker,errors,pricing,metering,metrics,deadline,
  gateway}.py` per the rules. Keep radon ≤ B per function (split `complete` into attempt/retry/fallback helpers).
- specs/10 §1 Decision (rules 1-11, condensed), §11 one-line pointer; specs/27 §3.6 four rows + Decision sentence.
- AGENTS.md bullet (P03-T13a); check `.env.example` needs no new keys (none planned).

### Tests (`packages/va_llm/tests/`, support `tests/support/llm_client_helpers.py`: `platform(base_url=...)`
provider builder, `gateway_rig(transport)` → LlmGateway on `ManualClock` + fresh registry + memory spans + a
`RecordingUsage` recorder; `sim_gateway(state)` = gateway over `httpx.ASGITransport(Starlette(routes=
simulator_routes("llm", state)))` with base_url `http://sim/v1`; respx fixture on `http://llm.test/v1`)
- `test_breaker.py`: 4 failures stay closed; 5th within 30 s opens; failures older than 30 s drop out; open
  rejects; at +20 s half-open admits 1 and rejects a concurrent 2nd; probe success closes (gauge 0), failure
  reopens (gauge 2); 429/4xx count as success; transitions logged.
- `test_policy.py`: table equals the specs/10 §2.1 rows (timeouts per call type), role-model mapping.
- `test_gateway_retries.py` (respx): per call type — router 503 then 200 → ok, 2 attempts; router 503 ×2 →
  LlmUnavailableError; 429 Retry-After 1 → slept 1.0 s on the clock then retried; 429 Retry-After 2 → no retry;
  connect error retried; 400 not retried (LlmHttpError surfaced inside LlmUnavailableError.last or raised —
  pick: raise `LlmHttpError` directly for non-retryable 4xx, documented); router timeout at 1.5 s
  (ManualClock `wait_for_sleep(1.5)`/`run_for`) → unavailable, request cancelled; analysis 500,500,500 → sleeps
  2 s and 8 s then fallback model succeeds (`fell_back`, model = fallback); dialogue 503 → fallback immediately,
  no retry; breaker open on primary → dialogue goes to fallback without a primary request, router raises
  `breaker_open` with zero requests; org provider failure → platform fallback model; `X-VA-Call-Id` header sent;
  request body (model, messages, tools, tool_choice, temperature, max_tokens, no `stream`).
- `test_usage_metering.py`: usage from response; estimate when absent (`estimated=True`, exact numbers);
  costs with defaults and JSON overrides; invalid JSON → ValueError; unknown model → 0 + one log; recorder gets
  one record per completion incl. fallback; recorder exception swallowed + logged.
- `test_metrics_spans.py`: §3.6 `va_llm_` rows parsed with `obs_helpers.vendor_metric_rows("va_llm_")` equal the
  registered metrics; outcomes per attempt; duration once; catalog tokens/errors only with a media-worker
  `ServiceMetrics`; span attributes and error status; key absent from repr/error messages/logs.
- `test_providers.py`: platform provider from `LlmSettings` defaults (spec dev values) and env; client cache by
  id + key hash (rotated key → new client, same → same); `prewarm` GET /models, failure logged not raised;
  probe → True / (2xx no tool call → False) / (400 → retry without tools → False) / auth error → error string.
- `test_org_providers_db.py` (integration, Postgres via va_db support `running_postgres` + `fresh_database` +
  `upgrade` module fixture, `tenant_rows.open_database`, Envelope with local KMS fakes from `crypto_fakes`):
  stored row decrypts to the provider; missing row → platform; cache hit within ttl (no DB read: count via a
  wrapper), refresh after ttl, `invalidate`; wrong AAD (row id swapped) → error; RLS: org B never sees A's row.
- `test_contract_fakellm.py` (FakeLLM `realtime_llm_state()` ASGI): complete returns rule content and usage;
  `X-VA-Call-Id` matched by a `match.call_id` rule; `server_error` fault once → retried ok; `rate_limit` fault
  retry_after_s=1 → retried after 1 s on the gateway clock; `timeout` fault → router unavailable after 1.5 s
  (`wait_held(state, 1)` then `clock.run_for(1.5)`); probe → supports_tool_choice True; tool calls parsed.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones; watch metrics/transport similarities with va_elevenlabs — factor into va_common if flagged),
`uvx radon cc -s -a packages/va_llm/src` (no C+), audit (`uvx pip-audit` on `uv export`, `corepack pnpm audit`).
Coverage va_llm ≥ 95 %.

### Risks
- openai SDK's own timeouts vs our clock deadlines: keep SDK timeout as a backstop (≥ policy), deadline via clock.
- ManualClock: every sleep parks (deadline + Retry-After sleepers coexist) — tests must `wait_for_sleep(s)` for the
  specific duration before `run_for`.
- Cancelling an in-flight openai request must not leak sockets (`filterwarnings=error`); close the shared http
  client and ASGI apps in fixtures.
- `test_metrics.py` counts 57 catalog rows (§3.1-§3.5): §3.6 additions don't change it; don't add va_llm rows
  to §3.3.
- respx + openai: openai sends to `{base_url}/chat/completions`; base_url must end in `/v1`.

## Build P03-T13a (Builder, 2026-11-01)
New package `packages/va_llm` (dist `va-llm`): settings, models, errors, providers, clients, org_providers, probe,
policy, breaker, deadline, pricing, metering, metrics, wire, gateway. Root pyproject: dev dep + source, mypy
files/mypy_path, pytest pythonpath; uv.lock. specs/10 §1 Decision (9 rules) + §11 metering Decision; specs/27 §3.6
four `va_llm_` rows + Decision. AGENTS.md bullet.
Deviations from the plan (all written into the specs/10 §1 Decision):
- Every exhausted call raises `LlmUnavailableError(reason exhausted|breaker_open, last)` (no direct LlmHttpError
  raise for 4xx); dialogue/analysis also fall back after a non-retryable 4xx or malformed body (other model/provider
  may accept it).
- `LlmProvider.api_key` must be non-empty (openai refuses an empty key at client construction).
- Probe uses the provider's `router` model. Clients enforce `max_concurrency` with a per-provider semaphore.
- Response bodies are parsed by `wire.parse_completion` from `with_raw_response` (SDK lenient construction lets
  malformed bodies through); span `http.status_code` is the real status.
- openai 3.42 is typed against `httpx2`; `clients.openai_client`/`sdk_timeout` are the only typing crossings.
Gates: make fmt 0, make lint 0, make typecheck 0, make placeholders 0 findings, spec refs 0, make test 6106 passed
(TOTAL 98%, va_llm 100%), jscpd 0 clones, radon no C+ (avg A 2.33), pip-audit no known vulns, pnpm audit 0.
Integration: test_org_providers_db.py ran against Docker Postgres (6 passed).

## Review P03-T13a (Reviewer, 2026-11-01)
Re-ran: make lint 0, make typecheck 0, make placeholders 0, make test 6106 passed (va_llm 100 %), spec refs 0,
jscpd 0 clones, radon no C+. Code otherwise matches the specs/10 §1/§2.1/§2.2/§11 Decisions closely. One finding:

1. `packages/va_llm/src/va_llm/gateway.py` `LlmGateway.complete` (the `provider = await self.registry.resolve(org_id)`
   line, before the span/try): a failure of the org-provider lookup escapes `complete` raw. Examples:
   `OrgProviderStore.get` raises `va_common.crypto.DecryptionError` (tested in `test_org_providers_db.py`),
   SQLAlchemy/asyncpg errors when Postgres is unavailable, or a pydantic `ValidationError` when a stored `models`
   JSON lacks a role (`org_providers.py` passes it with `type: ignore`). That breaks the documented contract (specs/10
   §1 Decision rule 6 "Every exhausted call raises `LlmUnavailableError`", AGENTS.md "raises only
   `LlmUnavailableError`") that the media worker will rely on for its §2.1 fallbacks. It also skips the §2.1 dialogue
   rule "if org provider fails, platform provider". Failures are not cached, so a DB blip makes every turn raise.
   Required: catch lookup failures in `complete` (inside the span so duration/span still record). Log
   `llm.org_provider_unavailable` (org_id, error type, never the key). Then for falls_back policies
   (dialogue/analysis) go straight to the platform fallback target (`fell_back=True`; count it like any fallback).
   For the other call types raise `LlmUnavailableError` with a new reason `"provider_unavailable"` (add it to
   `UnavailableReason` and the rule 6 text) and `last=None`. Wrap the store's errors at the `OrgProviderStore`/registry
   boundary (e.g. `OrgProviderError(LlmError)` raised from `_load` for DB/decrypt/validation failures) so the gateway
   doesn't catch bare `Exception`. Also validate `row.models` explicitly instead of the `type: ignore`.
   Tests: gateway with an org source that raises → dialogue answers from the platform fallback model, router raises
   `LlmUnavailableError(reason="provider_unavailable")` with zero requests, span error status + duration observed;
   DB test: a row with a `models` dict missing a role → `OrgProviderError`, a decrypt failure → `OrgProviderError`.

## Build P03-T13a fix (Builder, 2026-11-01)
Finding 1 fixed: `errors.OrgProviderError(provider_id, cause)` (cause = exception type name only) and reason
`provider_unavailable`. `OrgProviderStore.get` wraps `_load` failures (SQLAlchemyError, OSError, CryptoError,
ValidationError, botocore ClientError/BotoCoreError) — not cached; `models` validated with a
`TypeAdapter(dict[ModelRole, str])` (no type: ignore). `LlmGateway.complete` resolves the provider inside the span
(`_open`): logs `llm.org_provider_unavailable` (org_id, call_type, error); dialogue/analysis run one attempt on the
platform fallback (`fell_back=True`, metered); other call types raise `LlmUnavailableError("provider_unavailable",
last=None)` with zero requests; duration + span error status recorded either way. specs/10 §1 rule 6 updated;
AGENTS.md bullet extended. Tests: new `test_org_provider_failure.py` (fallback per falls-back type, fallback failure,
5 classifier types unavailable/no request/no attempt samples/span, unreachable DB → OrgProviderError not cached);
DB tests: decrypt failure → OrgProviderError(cause DecryptionError) not cached, missing role / unknown role →
ValidationError cause.
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 6118 passed (TOTAL 98%, va_llm 100% after
final branch tidy; va_llm suite re-run 182 passed), spec refs 0, jscpd 0 clones, radon max B (avg A 2.35),
pip-audit no known vulns, pnpm audit --audit-level high clean (3 moderate pre-existing).

## Review P03-T13a re-review (Reviewer, 2026-11-01)
Re-ran: make lint 0, typecheck 0, placeholders 0, make test 6118 passed (va_llm 100 %), spec refs 0, jscpd 0 clones.
Finding 1 resolved: OrgProviderStore wraps lookup failures in OrgProviderError (not cached, models validated via
TypeAdapter); complete resolves inside the span, falls back (dialogue/analysis) or raises provider_unavailable.
Approved, ticked, committed.

## Plan P03-T13b (Planner, 2026-11-01)
Task: `va_llm` streaming dialogue (specs/10 §2, §2.1 dialogue row, §8, §11). Builds on gateway.py (T13a).

### Spec decision to write: specs/10 §2 `> Decision (2026-11-01): streaming dialogue (P03-T13b).` Numbered rules:
1. Entry point is `LlmGateway.stream_dialogue(request: DialogueRequest) -> DialogueStream` (async; returns as soon
   as the producer task started, never waits for a token). `DialogueRequest` as in §2 but `provider: LlmProvider |
   None = None` (None → `registry.resolve(org_id)`, OrgProviderError → platform fallback like rule 6) and
   `model: str | None = None` (None → provider's `dialogue` model); `tools: tuple[ToolDef, ...] = ()`,
   `messages: tuple[ChatMessage, ...]` (frozen model, arbitrary types allowed for LlmProvider).
2. Body: T13a `ChatRequest.body()` plus `stream=True`, `stream_options={"include_usage": True}`,
   `tool_choice="auto"` when tools are given; `X-VA-Call-Id` when `call_id`. Sent through
   `client.chat.completions.with_streaming_response.create(...)` (via `Any`, like `_send`); the gateway parses the
   SSE lines itself (`stream_wire.py`), never the SDK's chunk objects. The provider semaphore is held for the whole
   stream.
3. SSE parsing (`va_llm/stream_wire.py`, pure): lines `data: <json>` / `data: [DONE]`; blank lines and lines
   starting `:` ignored; other fields (`event:`, `id:`) ignored. A chunk with an `error` object, a non-JSON data
   line, or a chunk that is not an object / has malformed `choices` → `LlmProtocolError`. End of body without
   `[DONE]` → `LlmConnectionError("stream ended without [DONE]")`. `StreamAssembler.feed(chunk) -> ChunkDeltas
   (content: str, first_token: bool)`: non-empty `delta.content` counts as token; `delta.tool_calls` items with an
   `index` accumulate `id`/`function.name` (first non-empty wins) and append `function.arguments`; a tool-call delta
   counts as first token; `reasoning_content`/`reasoning` and role-only/empty deltas are ignored (never first
   token); `finish_reason` kept (last non-null); `usage` (in the trailing `choices: []` chunk) parsed with the T13a
   `_usage` rules (reuse: move `_usage`/`_object`/`_text`/`_count` helpers so both wire modules share them, no
   copy). `assembler.tool_calls()` → tuple[ToolCall] ordered by index; a call without name → LlmProtocolError at
   end.
4. Deadlines on the injected clock: TTFT 2.5 s per target, measured from that target's send (fresh for the
   fallback); total 20 s from `stream_dialogue` start covering both targets (a fallback's TTFT is
   `min(2.5, remaining)`). Each read of the next line races the current deadline with `deadline.within`.
5. Before the first token, any failure of the primary (HTTP error any status incl. 429, connect, TTFT timeout,
   protocol error, end without `[DONE]`, open breaker) → one attempt on the platform fallback model (rule 6 of
   T13a: skipped when it equals the primary target, no retries on the primary). Both fail → stream fails:
   `text()` and `tool_calls()` raise `LlmUnavailableError(reason exhausted|breaker_open|provider_unavailable,
   last)`, `stream.error` holds it.
6. After the first token, a failure (disconnect, protocol error, total deadline) ends the stream: `text()` ends
   normally after the delivered text (partial text kept), `stream.error` = the `LlmError` (caller emits the error
   event), `finish_reason` None, `tool_calls()` returns the calls assembled so far that have a name. No fallback.
7. `ttft_ms` (float, injected clock, from the producing target's send) set on the first token; observed in §3.3
   `va_llm_ttft_ms{model, purpose="dialogue"}` when a catalog is given (`LlmMetrics.ttft(model, purpose, ms)`).
8. Speculative (`speculative=True`): deltas are buffered until `commit()`; `commit()` releases the buffer and
   later deltas to `text()`; `discard()` cancels the producer (closes the HTTP response), drops the buffer, and
   `text()` then ends with nothing. `va_llm_speculation_total{outcome}` counts the first of commit/discard on a
   speculative stream only; repeated calls are no-ops; `commit()` on a non-speculative stream is a no-op and
   `discard()` closes it without the metric. `text()` may be iterated once (second call → RuntimeError).
9. Cancellation: `DialogueStream` is an async context manager; `aclose()` cancels and awaits the producer. If the
   task iterating `text()` is cancelled, the generator's `finally` cancels the producer immediately (no clock
   wait), so the HTTP stream is closed before the cancellation completes (spec: within 50 ms).
10. Breaker per attempt (T13a rule 7): success at the first token or at a token-less normal end; failure verdict
   at a pre-token error by `error.breaker_failure`; cancelled before a verdict → `abandon()`. Post-token failures
   do not touch the breaker.
11. Metering: usage from the final chunk; absent (incl. discard, cancellation, mid-stream error) → `estimate_usage`
   over prompt + text received + tool calls (`estimated=True`). Recorded exactly once when the producer ends
   (completed, failed after a token, discarded, cancelled) for the target that produced tokens; discarded tokens
   are metered (§8). Nothing is metered when no target produced a token.
12. Metrics/spans: `va_llm_requests_total{call_type="dialogue"}` one per attempt (outcome `ok` when the attempt
   produced the stream's result incl. a post-token failure? → no: post-token failure counts its error outcome);
   `va_llm_request_duration_ms{call_type="dialogue"}` once per stream (start → producer end);
   `va_llm_errors_total` per failed attempt; tokens via the meter. Span `llm.dialogue` (CLIENT, started with
   `tracer.start_span` and ended by the producer) with the T13a attributes plus `llm.streaming=true`,
   `llm.speculative`, `llm.ttft_ms` (when set), `llm.turn_index` (when set), `llm.outcome`
   (`completed|failed|truncated|discarded|cancelled`); error status for failed/truncated.

### Files
- `packages/va_llm/src/va_llm/stream_wire.py` (new, pure): `parse_sse_line`, `StreamAssembler`, `ChunkDeltas`.
- `packages/va_llm/src/va_llm/wire_fields.py` (new): move `_Invalid`, `_object`, `_text`, `_count`, `_usage`,
  tool-call field readers from `wire.py` (public names `Invalid`, `read_object`, ...); `wire.py` imports them.
- `packages/va_llm/src/va_llm/targets.py` (new): move `_Target` (→ `Target`), `_outcome` (→ `attempt_outcome`),
  `_backstop` (→ `sdk_backstop`), `RETRYABLE_STATUSES` from gateway.py (gateway re-exports `RETRYABLE_STATUSES`).
- `packages/va_llm/src/va_llm/metering.py`: add `Meter(metrics, costs, recorder)` with `async record(org_id,
  call_id, call_type, provider_id, model, usage)` (moved body of `LlmGateway._meter`; both paths use it).
- `packages/va_llm/src/va_llm/metrics.py`: `ttft(model, purpose, ms)` (catalog histogram, None-safe),
  `speculated(outcome: Literal["committed","discarded"])`.
- `packages/va_llm/src/va_llm/dialogue.py` (new): `DialogueRequest`, `DialogueStream` (public surface: `text()`,
  `tool_calls()`, `ttft_ms`, `usage`, `finish_reason`, `error`, `provider_id`, `model`, `fell_back`, `attempts`,
  `commit()`, `discard()`, `aclose()`, `__aenter__/__aexit__`), internal `_Producer` running attempts and
  pushing into an `asyncio.Queue[str | _End]`. Keep radon ≤ B per function.
- `packages/va_llm/src/va_llm/gateway.py`: `stream_dialogue(request)`; share target resolution
  (`_open`-style resolve + fallback target) with dialogue.py via a small internal object (e.g.
  `_StreamContext(clients, clock, metrics, breakers, meter, tracer, platform_fallback)`); keep gateway.py < ~420 lines.
- `__init__.py`: export `DialogueRequest`, `DialogueStream`.
- specs/10 §2 Decision (rules above, condensed); specs/27 §3.6 Decision: one sentence that streams count attempts
  the same way and feed `va_llm_ttft_ms{purpose="dialogue"}`.
- AGENTS.md bullet (P03-T13b).

### Tests (packages/va_llm/tests/; helpers in `tests/support/llm_client_helpers.py`: `sse_body(chunks, done=True)`
→ bytes, `content_chunks(*tokens, usage=...)`, `tool_call_chunks(...)`, a `StreamingBody` httpx `AsyncByteStream`
that yields scripted parts and can hang (`asyncio.Event`) before part n, records `closed`/`cancelled`)
- `test_stream_wire.py`: SSE line parsing (comments, blank, `[DONE]`, non-JSON → protocol), content deltas, reasoning
  deltas ignored + not first token, role-only chunk not first token, tool-call assembly over 3 deltas by index with
  interleaved indices, first token on tool-call header, usage chunk with `choices: []`, error chunk → protocol,
  nameless call → protocol.
- `test_dialogue_stream.py` (respx + ManualClock rig):
  * text deltas in order; `ttft_ms` = clock time from send to first content (advance clock between parts by making
    the body wait on an event + `run_for`), reasoning deltas before content excluded from TTFT; usage from final
    chunk; finish_reason; tool_calls() assembled; request body has `stream`, `stream_options.include_usage`,
    `tool_choice: "auto"` only with tools, `X-VA-Call-Id`.
  * fallback before first token: 503 → fallback model answers (`fell_back`, model, attempts 2); 429 (Retry-After 0)
    → fallback (no retry); connect error → fallback; TTFT: body hangs → `wait_for_sleep(2.5)`, `run_for(2.5)` →
    fallback, first request closed; end without [DONE] before token → fallback; malformed chunk → fallback; open
    primary breaker → fallback with zero primary requests; both fail → `text()` raises LlmUnavailableError
    (reason exhausted, last set), `tool_calls()` raises; fallback == primary → no second attempt; org provider
    error → platform fallback directly.
  * after first token: body breaks (`httpx.ReadError`) → text yields the partial tokens and ends, `error` is
    LlmConnectionError, no fallback request; total deadline: tokens then hang → `wait_for_sleep(...)` remaining of
    20 s → truncated; usage estimated (`estimated=True`) and metered once.
  * cancellation: consumer task iterating `text()` cancelled while body hangs → the response stream is closed
    (StreamingBody.closed) right after `await task` with no clock advance; `aclose()`/context-manager exit same;
    breaker abandoned (half-open probe slot freed).
  * speculative: tokens not visible before `commit()` (text consumer blocked; check via `asyncio.wait(timeout=0)`-
    style settle helper, never sleeps), commit releases buffered then live tokens; discard closes the response,
    `text()` yields nothing, usage estimated and recorded, metric `discarded`; committed metric; double calls
    counted once; commit on non-speculative no-op, discard closes without metric; second `text()` → RuntimeError.
- `test_dialogue_metrics.py`: per-attempt `va_llm_requests_total` outcomes (server_error then ok on fallback),
  duration observed once, `va_llm_ttft_ms{model,purpose="dialogue"}` only with catalog, tokens in/out, errors_total;
  span `llm.dialogue` attributes (`llm.outcome`, `llm.ttft_ms`, `llm.fallback`, `llm.speculative`,
  `llm.turn_index`) and error status on failed/truncated.
- `test_dialogue_contract_fakellm.py` (`sim_gateway(realtime_llm_state())`): rule reply streamed token by token with
  usage from the final chunk (`include_usage`); tool-call rule assembled into ToolCall with valid JSON args;
  `server_error` fault → fallback model (sim request log shows 2 requests, second model = fallback); `disconnect`
  fault → fallback; header-less `timeout` fault → `wait_held(state, 1)` then gateway clock `run_for(2.5)` →
  fallback; `timeout` with `after_tokens=2` → `run_for` remaining → partial 2 tokens kept, `error` set;
  speculative discard against a held stream closes it (sim `held` drops to 0).
- Existing T13a tests stay green after the moves (targets/wire_fields/Meter refactor).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a packages/va_llm/src` (no C+), pip-audit + pnpm audit. va_llm coverage ≥ 95 %.

### Risks
- Verify that `with_streaming_response.create(stream=True)` raises `APIStatusError` on non-2xx when entered and that
  `iter_lines()` is available on the async raw response; if not, fall back to `client.chat.completions.create(...,
  stream=True)` → `AsyncStream.response` for raw lines. Either way parse lines ourselves.
- httpx errors raised while iterating the body (ReadError, RemoteProtocolError) are not wrapped by the SDK: map
  `httpx.HTTPError` → `LlmConnectionError` in the producer.
- ManualClock: each line read registers a deadline sleep; tests must `wait_for_sleep(exact remaining)` before
  `run_for`. Cancel timers promptly (`within` already does).
- Span across tasks: use `tracer.start_span` + `trace.use_span(span, end_on_exit=False)` in the producer and end it
  exactly once.
- Unawaited producer at GC → warnings are errors: `DialogueStream` must always be closed in tests (context manager)
  and the producer must never leave an exception unretrieved (store it in `error`).

## Build P03-T13b (Builder, 2026-11-01)
Implemented per plan. New: `va_llm.dialogue` (DialogueRequest, DialogueStream, DialogueDeps; producer task, END via
task done-callback so text() never hangs even if cancelled before start), `stream_wire.StreamAssembler`,
`wire_fields` (readers moved from wire.py), `targets` (Target/attempt_outcome/sdk_backstop/record_failure/
platform_fallback moved from gateway.py), `metering.Meter` (shared by complete + streams), `LlmMetrics.ttft/
speculated`. `LlmGateway.stream_dialogue(request)`. Deviations from plan: `tool_calls()` returns list (spec §2
signature); the span is started in `DialogueStream.__init__` and ended in the done-callback; cancelled/discarded
attempts are not counted in va_llm_requests_total (recorded in specs/27 Decision); `aclose()` re-raises only an
unexpected producer crash. Contract tests run FakeLLM on a real socket (`served_gateway`) because httpx
ASGITransport buffers whole bodies. Specs: specs/10 §2 Decision P03-T13b (12 rules), specs/27 §3.6 Decision.
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 6195 passed (TOTAL 98%, va_llm 100%),
spec refs 0, jscpd 0 clones, radon max B (avg A 2.46), pip-audit no known vulns, pnpm audit high clean
(3 moderate pre-existing).

## Review P03-T13b (Reviewer, 2026-11-01)
Re-ran: make lint 0, make typecheck 0, make placeholders 0 findings, make test 6195 passed (va_llm 100 %). Diff read against
specs/10 §2/§2.1/§8/§11 and the new Decisions.
Findings:
1. `packages/va_llm/src/va_llm/dialogue.py` `discard()` (≈line 200) + `_ended` (≈line 255): when a speculative
   stream's producer has already finished (the common case: the speculation completes while the caller is still
   talking, then the turn is abandoned), the `_END` marker sits in `self._held`; `discard()` sets `self._held = None`
   and `_stop()` returns at once (task done), so `_END` is dropped and nothing ever reaches `self._queue`. A consumer
   already iterating `text()` (or one that calls `text()` afterwards) blocks forever. Reproduced with a throwaway
   test: speculative stream → `await stream.tool_calls()` → `await stream.discard()` → `collect(stream)` never
   finishes (both "consumer waiting before discard" and "text() after discard" variants hang). specs/10 §2 Decision
   rule 8 requires "`discard()` ... drops the buffer, and `text()` then ends empty". Fix: on discard, after stopping
   the producer, put `_END` on the queue when the held buffer contained it (or always ensure the queue ends once
   the producer is done and the buffer is dropped); add tests for both variants (discard after the end with a waiting
   consumer and with a later `text()` call).
2. `dialogue.py` `_finish_span` (≈line 460): an unexpected producer crash ends the span with outcome `failed` but no
   error status/exception because `self.error` is None (only `LlmError`s set it). specs/27 §3.6 Decision says
   `failed` ends with error status. Record the crash exception (e.g. `task.exception()` in `_ended`) on the span and
   set ERROR status; extend `test_an_unexpected_producer_crash_reaches_the_consumer` (or a metrics test) to assert the
   span status.

## Build P03-T13b fix (Builder, 2026-11-01)
1. `discard()` now keeps the dropped held buffer; after stopping the producer it puts `_END` on the queue when the
   buffer already held it (producer finished before discard; `_ended` never runs again). Tests:
   `test_discard_after_the_end_ends_a_waiting_consumer`, `test_text_after_discarding_a_finished_stream_ends_empty`
   (shared setup `_finished_speculation`; both hung before the fix).
2. `_ended` passes `task.exception()` to `_finish_span(outcome, crash)`; a crash is recorded on the span with ERROR
   status (description = exception type). The crash test now asserts status + exception event.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 6197 passed (TOTAL 98 %, va_llm dialogue.py 100 %),
jscpd 0 clones, radon max B (avg A 2.48), spec refs 0, pip-audit no known vulns, pnpm audit high clean (3 moderate).

## Review P03-T13b re-review (Reviewer, 2026-11-01)
Re-ran lint 0, typecheck 0, placeholders 0, make test 6197 passed (va_llm 100 %), spec refs 0, jscpd 0 clones. Both
findings fixed: `discard()` re-queues `_END` when the dropped buffer held it (two new tests); `_ended` records a producer
crash on the span with ERROR status (crash test asserts it). Approved.

## Plan P03-T14a (Planner, 2026-11-01)
Split P03-T14 into T14a (structured outputs, this task) and T14b (embeddings client) in plan/phase-03.md (T16 now
depends on T14b). Rationale: each is a full review unit (T13 was split the same way).

### Spec decision to write: specs/10 §3 `> Decision (2026-11-01): structured outputs (P03-T14a).` Numbered rules:
1. API: `LlmGateway.call_structured(call_type: CallType, schema: type[M], *, org_id, messages, function_name,
   description, call_id=None, temperature=0.0, max_tokens=512) -> M` (M bound BaseModel). The §3 signature's
   `provider, model, timeout_s` come from the call type: provider via the registry, model via the policy role,
   timeout = §2.1 row (each request — the first and the repair — is one `complete` call with its own policy budget,
   retries, breaker skip, fallback, metering). `call_type="dialogue"` → `ValueError` (dialogue streams).
2. Registry: `TOOL_NAMES: frozenset[str]` = the 14 §3 names (constant). `register_structured_function(name, schema)`
   raises `ValueError` for a name outside `TOOL_NAMES` or a second registration with a different schema class (same
   class = no-op); `registered_schema(name) -> type[BaseModel] | None`. `call_structured` raises `ValueError` for an
   unregistered-in-TOOL_NAMES name, or when the name is registered with a different schema class.
3. Tool schema: `tool_schema(schema) -> dict` = `schema.model_json_schema()` with every `$ref` to `#/$defs/X`
   replaced by a deep copy of the def (siblings of `$ref` merged), `$defs` removed, and `additionalProperties: false`
   on every object schema that has `properties` (unless it already sets `additionalProperties`). A recursive model
   (a def that references itself directly or indirectly) → `ValueError("recursive schema")`. Cached per class
   (`functools.cache`).
4. Forced-tool mode (provider `supports_tool_choice`): `tools=[ToolDef(name, description, tool_schema)]`,
   `tool_choice=ForceTool(name)`. The output is the first tool call whose name is `function_name` (else the first
   tool call); no tool call → the first JSON object in `content` (rule 6), else invalid ("no tool call").
5. JSON mode (provider `supports_tool_choice=false`; on `OrgProviderError` during the mode lookup use the platform
   provider's flag): no tools; append a system message `Respond with only a JSON object that matches this JSON
   Schema: <compact JSON of tool_schema>` after the given messages; output = first JSON object in `content`.
6. First JSON object: scan `content` for `{`, `json.JSONDecoder().raw_decode` from each `{` in order; the first that
   decodes to a dict wins; none → invalid ("no JSON object in the reply").
7. Validation: arguments text → JSON (orjson; non-JSON or non-object → invalid), then
   `jsonschema.Draft202012Validator(tool_schema)` errors sorted by path, rendered `"<path>: <message>"`
   (path in `va_domain`-like dotted form `items[0].name`, `$` for root), at most 5 joined by `; ` (each message
   truncated at 200 chars); then `schema.model_validate(obj)` (a pydantic error rendered the same way).
8. Repair: on the first invalid output, one more request = original messages + `assistant` message with the raw
   previous output (arguments JSON text, or content; no `tool_calls`, so no tool reply is needed) + `user` message
   `Your previous output was invalid: <errors>. Call {function_name} again with corrected arguments.` In the same
   mode as the first request; additionally a first-request reply with no tool call in forced mode switches the repair
   to JSON mode (system instruction appended, no tools). Second invalid output → `StructuredOutputError`.
9. `StructuredOutputError(LlmError)` (`kind = "parse"`, `.function_name`, `.errors: tuple[str, ...]`, `.attempts`
   = structured requests made (2), provider_id/model of the last completion). `LlmUnavailableError` from `complete`
   propagates unchanged (callers catch `LlmError` to apply their §2.1 fallback). Both completions are metered by
   `complete`.
10. Observability: `va_llm_structured_total{function, outcome}` counter (outcome `valid|repaired|invalid`) in
    `LlmMetrics` (new §3.6 row in specs/27 + Decision sentence); log `llm.structured_invalid` (warning) with
    function, attempt, error count (never the arguments). The `llm.<call_type>` spans come from `complete`.

### Files
- `packages/va_llm/pyproject.toml`: add `jsonschema>=4.26`; root `pyproject.toml` mypy already has
  `types-jsonschema` in dev deps (verify mypy is happy; add to package deps only the runtime lib). `uv lock`.
- `packages/va_llm/src/va_llm/structured.py` (new): `TOOL_NAMES`, `register_structured_function`,
  `registered_schema`, `tool_schema`, `first_json_object`, `validate_arguments(schema, text) -> M` raising
  `_InvalidOutput(errors)` (internal), `repair_message(function_name, errors)`, `json_mode_instruction(schema)`,
  `StructuredOutput`-free; keep pure (no gateway import).
- `packages/va_llm/src/va_llm/structured_calls.py` (new) OR a method body in gateway.py delegating to a small
  `_StructuredRun` helper in `structured_calls.py` (keep gateway.py < ~420 lines, radon ≤ B):
  `async def call_structured(gateway_complete, registry, metrics, call_type, schema, ...)`. Gateway method
  `LlmGateway.call_structured(...)` is a thin wrapper.
- `errors.py`: `StructuredOutputError`. `metrics.py`: `STRUCTURED_METRIC = "va_llm_structured_total"`,
  `LlmMetrics.structured(function, outcome)`; `StructuredOutcome` Literal.
- `__init__.py`: export `StructuredOutputError`, `TOOL_NAMES`, `register_structured_function`, `tool_schema`.
- specs/10 §3 Decision; specs/27 §3.6 row `va_llm_structured_total | counter | function, outcome (valid/repaired/invalid)`
  (+ `test_metrics_spans.py` row parser must still pass: it parses `va_llm_` rows — register the counter).
- AGENTS.md bullet (P03-T14a).

### Tests (packages/va_llm/tests/)
- `test_structured_schema.py` (pure): `tool_schema` inlines nested `$defs` (model with nested model + list of
  nested + Optional nested → no `$ref`/`$defs` anywhere, `additionalProperties: false` on each object), keeps an
  explicit `additionalProperties` (dict[str, int] field), recursive model → ValueError; cached identity;
  `first_json_object` (prose around, braces in strings, first non-dict `{` skipped, nested, none → None);
  validation error rendering (path forms, 5-error cap, truncation); registry rules (unknown name, conflicting
  re-register, idempotent same class); `TOOL_NAMES` equals the 14 names in the specs/10 §3 paragraph (parse it).
- `test_structured_drift.py`: `TOOL_NAMES == va_sim.llm.heuristics.STRUCTURED_TOOL_NAMES`; AST scan of every
  `*.py` under `apps/*/src` and `packages/*/src` for calls named `call_structured` with a literal `function_name`
  keyword → must be in `TOOL_NAMES` (non-literal → fail with the file:line, so call sites use constants? no: allow
  `Name`/`Attribute` constants only if they resolve to module-level string constants — simpler: require a string
  literal; currently zero call sites, assert the scanner finds a planted literal in a tmp file via a unit test of
  the scanner function).
- `test_structured_calls.py` (respx + gateway_rig): valid forced tool call → model instance, request has `tools`
  with the inlined schema, `tool_choice` forcing the name, `temperature 0`, `max_tokens 512`, no `stream`; metric
  `valid`; invalid (schema violation: missing required) then repaired → second request contains the assistant raw
  output + exact repair user message, metric `repaired`; invalid JSON args then repaired; pydantic-only violation
  (validator beyond JSON Schema, e.g. `field_validator`) triggers repair; unrecoverable → StructuredOutputError
  with errors/attempts=2, metric `invalid`, both completions metered (recorder sees 2 records); no tool call but
  JSON object in content → accepted without repair; no tool call and no JSON → repair in JSON mode (no `tools`,
  system instruction last-before-repair? — exact message order: original messages, system instruction,
  assistant raw, user repair); provider `supports_tool_choice=False` (org provider fixture) → JSON mode first
  request (no tools, instruction appended); OrgProviderError during lookup → platform flag (forced mode) and
  `complete` falls to... (non-falling-back call type raises LlmUnavailableError("provider_unavailable")
  unchanged); LlmUnavailableError on first request propagates (router 503 twice); `call_type="dialogue"` →
  ValueError; unknown function name → ValueError before any request; conflicting registered schema → ValueError;
  tool call choice: picks the call named `function_name` when several.
- `test_structured_contract_fakellm.py` (`sim_gateway(realtime_llm_state())` or accelerated `llm_state`, ASGI ok
  since non-streaming): rule `tool_name: select_path` with object args → parsed model; rule with string malformed
  args at `call_index: 0` and valid args at `call_index: 1` → repaired (sim request log shows 2 requests, second has
  the repair message); malformed both times → StructuredOutputError; heuristic args (no rule) for each of the 14
  names using `tests/support/llm_schemas.py`-like pydantic models built per name (reuse va_sim test SPEC_CALLS?
  they live in va_sim tests support — not importable as a package path from va_llm tests unless on pythonpath;
  instead define small pydantic models in `packages/va_llm/tests/support/structured_models.py` for 3-4 names
  (select_path, score_condition, classify_answer, evaluate_guard_rails) whose JSON schemas the sim fills validly);
  JSON-mode contract: rule `system_contains: ["Respond with only a JSON object"]` responding content
  `Sure: {"...": ...}` → parsed.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a packages/va_llm/src` (no C+), `uvx pip-audit` on `uv export` + `corepack pnpm audit`.
va_llm coverage ≥ 95 % (currently 100 %).

### Risks
- `model_json_schema()` puts `$defs` at the root and may emit `$ref` with siblings (description) — merge siblings.
- Pydantic `Optional[Nested]` renders `anyOf: [{$ref}, {type: null}]` — inliner must recurse into lists/anyOf.
- `additionalProperties: false` + pydantic defaults: fields with defaults are not `required`; fine. A model with
  `extra="allow"` would conflict; keep explicit `additionalProperties` if present.
- The sim's `SchemaFill.conform` must produce valid args for inlined schemas without `$defs` (it handled
  specs/30 schemas; verify with the contract tests).
- jsonschema `best_match`/error ordering nondeterminism: sort by `(list(error.absolute_path) as str, message)`.
- Keep `gateway.py` size and radon in check by putting the run logic in `structured_calls.py`.

## Build P03-T14a (Builder, 2026-11-01)
Implemented per plan. New `va_llm.structured` (pure: TOOL_NAMES, register_structured_function/registered_schema/
check_function, tool_schema with keyword-aware $defs inlining + additionalProperties false, first_json_object,
validate_arguments → InvalidOutputError, repair_message, json_mode_instruction) and `structured_calls`
(StructuredCall, run_structured(gateway, metrics, call); LlmGateway imported only under TYPE_CHECKING).
`LlmGateway.call_structured` thin wrapper. `StructuredOutputError` in errors.py; `va_llm_structured_total` in
LlmMetrics (`structured_outputs` counter). jsonschema added to va_llm deps.
Deviations: tool_schema returns a deep copy per call (cached internally) instead of the cached object; validation
uses the cached Draft202012Validator; LlmUnavailableError adds no structured metric (recorded in specs/27 Decision).
Tests: test_structured_schema (27+), test_structured_calls (22, respx), test_structured_drift (AST scanner + sim set
equality), test_structured_contract_fakellm (9). Autouse conftest fixture resets the registry.
Specs: specs/10 §3 Decision P03-T14a (9 rules), specs/27 §3.6 row + Decision. AGENTS.md bullet.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 6260 passed (TOTAL 98 %, va_llm 100 %),
spec refs 0, jscpd 0 clones, radon max B (structured avg A 2.77), pip-audit no known vulns, pnpm audit high clean
(3 moderate pre-existing).

## Review P03-T14a (Reviewer, 2026-11-01)
Re-ran: make lint 0, make typecheck 0, make placeholders 0 findings, make test 6260 passed (TOTAL 98.39 %). Read the diff
against specs/10 §3 and the new Decision (9 rules) and the specs/27 §3.6 row/Decision: the API, registry rules, keyword-aware
`$defs` inlining with `additionalProperties: false`, forced/JSON mode choice (platform flag on OrgProviderError), first-JSON
scan, validation rendering/cap/clip, repair message order and JSON-mode switch, the error fields, the metric outcomes, and the
log fields (no output content) all match. Tests cover valid/repaired/invalid, JSON-mode paths, org-provider modes,
unavailability during both requests, the refused names and dialogue, the drift and call-site scanner, and FakeLLM contracts.
The double provider lookup (mode, then complete) hits the OrgProviderStore TTL cache. Approved.

## Plan P03-T14b (Planner, 2026-11-01)
Embeddings client for the platform provider (specs/10 §10, §11; specs/24 `kb_embedding_tokens`). Standalone class,
not part of `LlmGateway` (no call-type policy, no breaker, no org providers: §10 says "on the platform provider").

### Spec decision to write: specs/10 §10 `> Decision (2026-11-01): embeddings client (P03-T14b).` Numbered rules:
1. `va_llm.Embeddings(provider, http, *, redis, clock, metrics, model, dimensions=1024, recorder=None, costs=None,
   tracer_provider=None, concurrency=4)`; services pass `registry.platform` and `LlmSettings.embeddings_model` /
   `embeddings_dimensions` (new settings fields mirroring the existing specs/02 §6 `EMBEDDINGS_MODEL` /
   `EMBEDDINGS_DIMENSIONS` rows; defaults `Qwen/Qwen3-Embedding-8B` / 1024; no new env rows). One instance per
   process (the 4-batch semaphore is per instance). It builds its own `AsyncOpenAI` via `clients.openai_client`.
2. `embed(texts, *, org_id, document_id=None) -> list[list[float]]` (order preserved): empty list → `[]` with no
   request; an empty string → `ValueError` (vendors 400 on it). Inputs split into batches of 64, at most
   `concurrency` (4) batches in flight (asyncio.Semaphore + TaskGroup); a failed batch cancels the rest and its error
   propagates (completed batches stay metered).
3. Truncation (no tiktoken, DEC-006): an input above 8 000 estimated tokens (`metering.estimate_tokens`, 4 UTF-8
   bytes/token) is cut to its first 32 000 UTF-8 bytes on a character boundary
   (`text.encode()[:32000].decode(errors="ignore")`); logged once per batch `llm.embedding_truncated` (count only,
   never text).
4. Request: `client.embeddings.with_raw_response.create(model, input=[...], dimensions, encoding_format="float")`
   with the SDK backstop timeout; deadline 30 s per attempt raced on the injected clock (`deadline.within`). Body
   parsed by our own `parse_embeddings(body, expected=n, dimensions=d)` (new in `wire.py` or a new
   `embedding_wire.py`): `data` list of n objects with distinct `index` 0..n-1 (sorted by index), each `embedding`
   a list of numbers of length == dimensions with a non-zero norm, optional `usage.prompt_tokens` (int ≥ 0);
   anything else → `LlmProtocolError`. Vectors L2-normalised (pure Python `math.fsum`/`sqrt`, float64).
5. Retries: on 429, 500/502/503/504, connect errors, and timeouts, up to 3 retries after 1 s, 4 s, 10 s (the three
   listed waits; 4 attempts at most — record that "3 attempts" is read as 3 retries). A 429's wait is
   `max(delay, Retry-After)` capped at 30 s. Other 4xx and protocol errors are not retried. Exhaustion →
   `LlmUnavailableError("exhausted", provider_id, model, last=<final error>)` (reuse errors.py, `map_openai_error`).
6. `embed_query(text, *, org_id) -> list[float]`: Redis key `emb:q:{sha256(f"{model}|{dimensions}|{text}")}`
   (hex), value orjson list of floats, `SET ... EX 86400`. Hit with a valid vector (right length, numbers) → returned
   with no request; malformed cached value → treated as a miss and overwritten. Redis errors on GET/SET (any
   `redis.RedisError` / `OSError`) → log `llm.embedding_cache_unavailable` and continue without the cache (fail
   open). Add `emb:q:{hash}` to specs/03 Redis key table if absent (check §11 Redis keys; add row: string, 24 h).
7. Metering: each successful batch → `EmbeddingUsageRecord(org_id, document_id, purpose: "ingest"|"query", model,
   prompt_tokens, estimated, cost_micros)` to `EmbeddingUsageRecorder.record` (Protocol, new in `metering.py`);
   `cost_micros = costs.cost_micros(model, prompt_tokens, 0)[0]`; missing usage → sum of `estimate_tokens(input)`
   flagged estimated; recorder failure logged `llm.usage_record_failed` and swallowed. Billing maps it to
   `kb_embedding_tokens` (quantity = prompt_tokens) later. `embed` purpose `ingest`, `embed_query` purpose `query`.
8. Metrics (specs/27 §3.6 rows + Decision sentence): `va_llm_embedding_requests_total{model, outcome}` (outcome =
   AttemptOutcome minus breaker_open, one per attempt), `va_llm_embedding_duration_ms{purpose}` histogram LAT_MS (one
   per embed/embed_query call that sent requests), `va_llm_embedding_cache_total{result}` (hit/miss/error). Tokens
   also go to §3.3 `va_llm_tokens_total{direction="in"}` and failed attempts to `va_llm_errors_total` (catalog,
   like `complete`). Spans: one `llm.embeddings` CLIENT span per batch (attributes `llm.provider`, `llm.model`,
   `llm.inputs`, `llm.attempts`, `llm.tokens_in`, `http.status_code`; ERROR status + exception on failure).

### Files
- `packages/va_llm/src/va_llm/embeddings.py` (new): `Embeddings`, `EMBED_BATCH_SIZE=64`, `MAX_INPUT_BYTES=32_000`,
  `RETRY_DELAYS_S=(1.0, 4.0, 10.0)`, `ATTEMPT_TIMEOUT_S=30.0`, `query_cache_key(model, dims, text)`, `truncate(text)`.
  Keep radon ≤ B; split cache into `embedding_cache.py` (`QueryCache(redis, metrics)` get/put) and wire parsing into
  `embedding_wire.py` (pure: `parse_embeddings`, `l2_normalize`) if embeddings.py grows past ~250 lines.
- `metering.py`: `EmbeddingUsageRecord`, `EmbeddingUsageRecorder`, `EmbeddingPurpose` Literal.
- `metrics.py`: three new metrics + methods `embedding_attempt(model, outcome)`, `embedding_duration(purpose, ms)`,
  `embedding_cache(result)`; reuse `tokens`/`error`.
- `targets.attempt_outcome` reused for outcomes; `sdk_backstop` reused.
- `settings.py`: `embeddings_model`, `embeddings_dimensions` fields.
- `__init__.py`: export `Embeddings`, `EmbeddingUsageRecord`, `EmbeddingUsageRecorder`.
- `pyproject.toml`: add `redis>=…` (match the version constraint other packages use) if va_common does not already
  provide it transitively — declare it explicitly since va_llm imports `redis`.
- specs/10 §10 Decision; specs/27 §3.6 rows + Decision; specs/03 Redis key row if missing; AGENTS.md bullet.

### Tests (packages/va_llm/tests/; helpers in tests/support/llm_client_helpers.py: `embeddings_rig(...)` = Embeddings on
ManualClock + fakeredis + fresh registry + RecordingRecorder + memory spans, `embedding_body(n, dims, usage=)`)
- `test_embedding_wire.py` (pure): parse ok (out-of-order indexes sorted), wrong count/duplicate index/wrong length/
  non-number/zero vector/missing data/bad usage → LlmProtocolError; normalisation gives unit norm; truncate keeps
  ≤ 32 000 bytes on a char boundary (multi-byte text), short text untouched; cache key = sha256 of `model|dims|text`.
- `test_embeddings.py` (respx `llm_api`): 130 texts → 3 requests of 64/64/2 with `dimensions: 1024`,
  `encoding_format: "float"`, model, order preserved; ≤ 4 in flight (10 batches with hanging responses: exactly 4
  requests seen before releasing one); empty list → no request; empty string → ValueError; truncation sends the cut
  text; retries: 503,503,200 → success after waits 1 s, 4 s (ManualClock wait_for_sleep/run_for); 4 × 500 →
  LlmUnavailableError with waits 1/4/10; 429 Retry-After 7 → waits 7; 400 → no retry; timeout (hang 30 s) retried;
  wrong-length vector → LlmProtocolError-caused unavailability without retry; a failing batch cancels in-flight
  siblings; usage records per batch (purpose, document_id, tokens, cost from COSTS, estimated when usage absent);
  recorder failure swallowed; metrics (attempt outcomes, duration once, catalog tokens/errors); span attributes and
  error status; API key never in errors/logs.
- `test_embedding_cache.py`: miss → request + SET with TTL 86400 (fakeredis `ttl`), second call hit → no request,
  metric hit/miss; different model/dims/text → different keys; corrupt cached value → miss and overwrite; Redis
  down (a client whose commands raise ConnectionError) → still returns vectors, `error` metric, log event.
- `test_embeddings_contract_fakellm.py` (sim over ASGI, `llm_state()`/realtime): 70 texts → 1024-d unit vectors,
  deterministic (same text → identical vector across calls/batches), sim records 2 embedding requests with
  dimensions 1024; armed `rate_limit` fault (endpoint embeddings) once → retried and succeeds; `server_error` faults
  exhausting → LlmUnavailableError; usage tokens equal the sim's `prompt_tokens`.
- `test_metrics_spans.py` keeps passing (registered rows == spec rows).

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones; reuse helpers rather than copying retry code from gateway), `uvx radon cc -s -a packages/va_llm/src`,
pip-audit + `corepack pnpm audit`. va_llm coverage stays ≥ 95 % (currently 100 %).

### Risks
- respx records only completed calls: assert in-flight counts with the `Hanging` side effect's `requests`.
- ManualClock: a retry wait starts after the respx answer; `await clock.wait_for_sleep(1.0)` before `run_for`.
- TaskGroup cancellation of sibling batches must not leak hanging respx tasks (assert `cancelled`).
- jscpd may flag similarity with gateway `_attempt`; factor shared bits (`targets.attempt_outcome`, `map_openai_error`).
- fakeredis TTL check needs the real `redis.asyncio` API (`ttl`), available in fakeredis.

## Build P03-T14b (Builder, 2026-11-01)
Implemented per plan. New `va_llm.embeddings.Embeddings` (64-batches, semaphore of 4 + TaskGroup, first failure
propagates and cancels siblings, 30 s per-attempt deadline via `within`, retries 1/4/10 s on 429/5xx/connect/timeout,
429 = max(delay, Retry-After) capped 30 s, everything else → LlmUnavailableError("exhausted", last)), pure
`embedding_wire` (parse_embeddings → LlmProtocolError, l2_normalize, truncate_input 32 000 bytes, query_cache_key
`emb:q:{<hex>}` hash tag), `embedding_cache.QueryCache` (24 h, malformed = miss, Redis errors fail open). Metering:
`EmbeddingUsageRecord`/`EmbeddingUsageRecorder` in metering.py. Metrics: three new §3.6 rows. Settings: embeddings_model,
embeddings_dimensions. redis dep added to va_llm.
Deviations: truncation is logged once per call (not per batch). Spec conflict: specs/03 §11 `kbq:{sha256}` 1 h and
specs/14 §7 vs specs/10 §10 `emb:q` 24 h → followed specs/10 + plan; replaced the specs/03 row, Decisions in 03/14
(14 also notes retries 1/4/10 instead of 1/2/4/8).
Pre-existing flake fixed: va_llm hanging-first-request timeout tests hung under `-n auto --cov` (reproduced at HEAD with
`pytest packages/va_llm packages/va_sim/tests/llm -n auto --cov`, failed in 6 of 7 runs; my new files moved tests across workers and
make test hit it twice). Cause: openai SDK `asyncify(get_platform)` thread hop before a client's first request vs
ManualClock deadlines. Fix: autouse `inline_sdk_platform_lookup` in va_llm conftest; then 3/3 green on that selection.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 findings, make test 6323 passed twice (TOTAL 98 %, va_llm 100 %),
spec refs 0, jscpd 0 clones, radon avg A (max B), pip-audit no known vulns, pnpm audit high clean (3 moderate
pre-existing).

## Review P03-T14b (Reviewer, 2026-11-01)
Re-ran: make lint 0, make typecheck 0, make placeholders 0 findings, make test 6323 passed (TOTAL 98.41 %, va_llm 100 %).
Read embeddings/embedding_wire/embedding_cache against specs/10 §10 + the new 8-rule Decision, specs/27 §3.6 rows and
Decision, and the specs/03 §11 / specs/14 §6-§7 conflict resolution (kbq 1 h → emb:q 24 h; no code referenced kbq).
Batching, semaphore + TaskGroup cancellation, retry classification (429 Retry-After cap, 5xx set, timeout/connect),
own body parsing with dimension/index/zero-norm checks, normalisation, truncation on char boundary, fail-open cache,
metering (estimated flag, swallowed recorder failure), metrics and spans all match; tests cover each with ManualClock
(no wall sleeps). The conftest `asyncify` inline patch is test-only and justified. Approved.

## Plan P03-T15a (Planner, 2026-11-01)
P03-T15 was too big for one activation, so I split it in `plan/phase-03.md` into T15a (bootstrap library + script),
T15b (python.Dockerfile, compose `sim` service, `make up` writes TELNYX_PUBLIC_KEY), and T15c (Redis-backed SimState,
SIM_WORKERS). The parent P03-T15 gets ticked once all three are done. This plan covers T15a only (specs/06 §12, §13).

### Spec decision to write: specs/06 §12 `> Decision (2026-11-01): bootstrap library and script (P03-T15a).`
1. The logic lives in `va_telnyx.bootstrap` (the script now, the `BootstrapTelnyxAccount` activity later). It only
   talks to `TelnyxClient`, never touches the DB or env.
2. Names: `platform_resource_name(va_env) = "voiceagents-{va_env}"` and `org_resource_name(org_id) =
   "voiceagents-org-{org_id}"`. Webhooks: `webhook_urls(edge_public_host, telnyx_account_id=None)` gives
   `https://{host}/telnyx/voice[/{id}]` and `https://{host}/telnyx/messaging[/{id}]` (always https, as in §1 and §12,
   even when the host is local; the sim only records the URL).
3. Names must match exactly. The list filters are `contains` (apps, voice profiles) or provider-side, so results are
   filtered to `name == wanted`. When several match exactly, use the first in list order and log
   `telnyx_bootstrap.duplicate_resources` (kind, name, count, chosen id) at warning level. Nothing is deleted.
4. Order: voice profile, then app (created with that profile; if it exists, one PATCH that sends only the fields
   that differ: `webhook_event_url` and/or `outbound.outbound_voice_profile_id`), then the messaging profile (created,
   or PATCHed when `webhook_url` differs). Nothing is sent when nothing differs.
5. Result `BootstrapResult(call_control_app_id, outbound_voice_profile_id, messaging_profile_id, created: tuple[str,
   ...], updated: tuple[str, ...])`. Kinds are `outbound_voice_profile`, `call_control_app`, `messaging_profile`.
   `env_values()` returns the three `TELNYX_*_ID` keys in §12 step 4 order.
6. Script `scripts/telnyx_bootstrap.py [--env-file PATH] [--write-env] [--json]`. Settings come from the env file
   plus the environment (`VA_ENV`, `TELNYX_API_BASE`, `TELNYX_API_KEY`, `EDGE_PUBLIC_HOST`, `REDIS_URL` for the
   rate limiter, which fails open when Redis is down). The default `--env-file` is `.env`; an explicitly given
   missing file exits 2. stdout carries only the result: three `KEY=value` lines, or one JSON object with the
   three keys plus `created`/`updated`. Logs go to stderr. A Telnyx error (`TelnyxError`) exits 1 with
   `telnyx_bootstrap.failed` (error code and status only, never the key).
7. `--write-env` updates the three keys in place. Each existing `KEY=...` line keeps its trailing `  # comment`;
   a missing key is appended at the end; every other line stays byte for byte; the file is written through a temp
   file plus `os.replace`; when no value changes, the file is not written.

### Files
- `packages/va_telnyx/src/va_telnyx/bootstrap.py` (new): `RESOURCE_PREFIX = "voiceagents"`, `ResourceKind` Literal,
  `WebhookUrls` (frozen dataclass voice/messaging), `platform_resource_name(va_env: str) -> str`,
  `org_resource_name(org_id: uuid.UUID | str) -> str`, `webhook_urls(edge_public_host: str, telnyx_account_id:
  uuid.UUID | str | None = None) -> WebhookUrls`, `BootstrapResult` (frozen dataclass + `env_values() -> dict[str,
  str]`), `ENV_KEYS` tuple, `async bootstrap_account(client: TelnyxClient, *, name: str, webhooks: WebhookUrls) ->
  BootstrapResult`. Keep each step in its own small private coroutine (`_voice_profile`, `_app`, `_messaging`) plus an
  `_exact(kind, name, items, key)` helper, so radon stays at B or better. Export the names from `va_telnyx/__init__.py`.
- `scripts/telnyx_bootstrap.py` (new): `BootstrapSettings(RedisSettings)` with `service_name = "telnyx-bootstrap"`,
  `telnyx_api_base: str = "http://localhost:9100/v2"`, `telnyx_api_key: str = "sim-telnyx-key"`,
  `edge_public_host: str = "localhost:8100"` (the specs/02 §6 defaults). Also `parse_args(argv)`,
  `load_settings(env_file: Path | None) -> BootstrapSettings` (pass `_env_file` through a `dict[str, Any]` because
  mypy runs without the pydantic plugin), `async run_bootstrap(settings, *, http: httpx.AsyncClient, redis: Redis) ->
  BootstrapResult`, `render_text(result) -> str`, `render_json(result) -> str`, `update_env_file(path, values:
  Mapping[str, str]) -> bool` (True when written), and `main(argv=None) -> int`. The HTTP client comes from
  `va_common.http.http_client(settings, timeout=...)` and Redis from `va_common.redis.redis_client(settings)`; both
  are closed on exit. Logging goes to stderr via `configure_logging(settings, stream=sys.stderr, ...)`; see
  `seed_dev.py` for the pattern.
- specs/06 §12 Decision (above). `AGENTS.md`: add a Learned bullet and a Commands row
  (`uv run python scripts/telnyx_bootstrap.py --env-file .env [--write-env]`).

### Tests
- `packages/va_telnyx/tests/test_bootstrap.py`, contract tests over FakeTelnyx using `telnyx_client_helpers.sim_clients`:
  - first run: `created` lists all three kinds; the sim lists (raw GETs) show exactly one of each, named
    `voiceagents-test`; the app's webhook and profile id are as expected; the messaging webhook is as expected.
  - second run: same ids, `created == updated == ()`, still one of each.
  - a resource named `voiceagents-test-old` (contains-match only) is ignored, and a new exact one is created.
  - app webhook drift (raw PATCH to the sim) → rerun lists `updated == ("call_control_app",)` and restores the
    webhook; profile drift (app points at another voice profile) → patched; messaging webhook drift → patched.
  - two exact-name apps → the first is used, and the warning event is logged (`service_helpers.json_log_stream`).
  - pure tests: `platform_resource_name("local")`, `org_resource_name(uuid)`, `webhook_urls` with and without an
    account id, `env_values()` key order.
- `packages/va_telnyx/tests/test_bootstrap_requests.py` (respx `telnyx_api` fixture): when only the webhook differs,
  the PATCH body is exactly `{"webhook_event_url": ...}`; when only the profile differs, it is exactly
  `{"outbound": {...}}`; nothing differs → no PATCH request; a create error (422) propagates as the mapped
  `TelnyxError`.
- `scripts/tests/test_telnyx_bootstrap.py`:
  - `update_env_file`: replaces a value and keeps `  # comment`; appends missing keys; leaves other lines and
    comments untouched; returns False and does not change mtime/content when nothing changed; keeps the final newline.
  - `load_settings` reads the env file; an explicit missing file → `main` returns 2.
  - end to end: `async with running_sims(new_state()) as urls` (va_sim `sim_helpers`), write a tmp env file with
    `TELNYX_API_BASE={urls['telnyx']}/v2`, `VA_ENV=test`, and an unreachable `REDIS_URL` (the limiter fails open).
    Run `main([... "--write-env", "--json"])` in a thread (`asyncio.to_thread`, since `main` calls `asyncio.run`) twice.
    Check that stdout JSON ids are equal across runs, the second run has empty `created`, and the env file holds
    the ids. Text mode prints exactly three `KEY=value` lines. If calling `main` from a thread is awkward, test
    `run_bootstrap` directly with an `httpx.AsyncClient` on the running sim plus one sync `main` test where the
    sim runs on a background thread. Add `isolate_env` (service_helpers) so a developer `.env` never leaks in.
  - Telnyx failure (wrong API key → 401) → exit 1, and stderr does not contain the key.

### Gates
`make fmt lint typecheck placeholders test`, `uv run python scripts/check_spec_refs.py`, `npx --yes jscpd@4 --silent`
(0 clones), `uvx radon cc -s -a packages/va_telnyx/src scripts/telnyx_bootstrap.py`, `uvx pip-audit` on `uv export`
plus `corepack pnpm audit`. va_telnyx coverage stays at or above 95 %.

### Risks
- The sim's messaging-profile list filter is exact (`filter[name]`), while apps and voice profiles use `contains`.
  The code filters exactly in every case.
- `main` uses `asyncio.run`, so it cannot run inside the pytest event loop. Use `to_thread`, or test sync with the
  sim on a thread.
- `RedisSettings` may require `REDIS_URL`; give it an unreachable URL in tests (`UNREACHABLE` in service_helpers).
- Tests must not use a developer `.env`; always pass an explicit tmp `--env-file`.

## Build P03-T15a (Builder, 2026-11-01)
Implemented per plan. `va_telnyx.bootstrap` (`bootstrap_account`, `_Run` steps voice_profile → app → messaging,
`_exact` exact-name filter + `telnyx_bootstrap.duplicate_resources` warning, `BootstrapResult.env_values()`,
`platform_resource_name`/`org_resource_name`/`webhook_urls`), exported from `va_telnyx`. `scripts/telnyx_bootstrap.py`
(BootstrapSettings(RedisSettings) with `service_name: ClassVar`, `load_settings`, `run_bootstrap`, `render_text/json`,
`update_env_file` atomic via mkstemp + Path.replace keeping mode/comments/line endings, exit 2 missing env file or
invalid settings, exit 1 on TelnyxError logging type/operation/status/codes only). specs/06 §12 Decision (7 rules),
AGENTS.md Commands row + Learned bullet. Fixed a pre-existing check_spec_refs failure in plan/phase-03.md (T14b tick
line `27 §3.6` → `specs/27 §3.6`).
Deviations: when no `--env-file` is given, `.env` is used only if present (plan: default `.env`); `--write-env` then
writes `.env`. Invalid settings also exit 2.
Tests: test_bootstrap.py (9, FakeTelnyx contract), test_bootstrap_requests.py (5, respx exact PATCH bodies),
scripts/tests/test_telnyx_bootstrap.py (12; end-to-end via `asyncio.to_thread(main)` + `running_sims`).
Manual smoke: host sim on 19100, script printed 3 ids, second run created/updated empty, env file rewritten keeping
the comment.

## Review P03-T15a (Reviewer, 2026-11-01)
Re-ran: make lint 0, make typecheck 0, make placeholders 0, make test 6349 passed (TOTAL 98 %, bootstrap.py 100 %,
script 98 %), check_spec_refs 0, jscpd 0 clones, radon avg A. Read bootstrap.py and scripts/telnyx_bootstrap.py
against specs/06 §12 steps 1-4 and the 7-rule Decision: names, https webhook URLs, exact-name filtering with
duplicate warning, order voice profile → app → messaging, PATCH only drifted fields, env_values order, stdout/stderr
split, exit codes, key never logged (tested), atomic env rewrite keeping comments/mode. Defaults match .env.example.
§13 "bootstrap idempotency" covered (run twice, drift patched). Approved.
