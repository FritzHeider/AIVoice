# Phase 10 — Phone Numbers, Inbound Configuration, Pools, BYO Telnyx

- [ ] P10-T01 Number search
  - Spec refs: specs/16-phone-numbers.md (GET /v1/numbers/available); specs/06-telephony-telnyx.md (number search)
  - Depends on: P03-T08
  - Deliverables: search proxy with filters (country, area code, contains, features, type, limit), pricing annotation
  - Acceptance: tests against FakeTelnyx incl. `NO_NUMBERS_FOUND`.

- [ ] P10-T02 Purchase and release
  - Spec refs: specs/16-phone-numbers.md (POST /numbers/purchase, release, number orders); specs/06-telephony-telnyx.md (number orders, phone number update/delete)
  - Depends on: P10-T01
  - Deliverables: purchase endpoint (Bland-compatible body/response), `NumberOrderWorkflow` (order, poll/await webhook, assign connection + messaging profile, persist), release flow, events `number.purchased`/`number.released`
  - Acceptance: Temporal tests incl. `NUMBER_UNAVAILABLE` race.

- [ ] P10-T03 Inbound configuration API and labels
  - Spec refs: specs/16-phone-numbers.md §4 and inbound endpoints
  - Depends on: P10-T02, P05-T12
  - Deliverables: `GET /v1/inbound`, `GET/POST /v1/inbound/{phone_number}` with every field validated (reusing Send Call validators), `POST /inbound/update_label`, maintenance window, fallback number
  - Acceptance: e2e purchase_and_inbound passes.

- [ ] P10-T04 Number pools and local presence
  - Spec refs: specs/16-phone-numbers.md (number pools, dialing_strategy local presence)
  - Depends on: P10-T02
  - Deliverables: `/v1/number_pools` CRUD + membership, selection strategies (round_robin, least_recently_used, random, local_match) with per-number daily caps, `dialing_strategy` resolution in Send Call (enterprise feature gate for custom pooling)
  - Acceptance: deterministic selection tests; concurrency-safe counters.

- [ ] P10-T05 BYO Telnyx accounts
  - Spec refs: specs/16-phone-numbers.md §7; specs/06-telephony-telnyx.md (BYO Telnyx account handling, bootstrap)
  - Depends on: P10-T02, P00-T07
  - Deliverables: `POST /v1/accounts` (validate key, bootstrap resources in customer account, encrypt, return `encrypted_key`), `POST /v1/accounts/delete`, `POST /v1/inbound/insert`, `POST /v1/inbound/delete`, Send Call `encrypted_key` header/body handling selecting credentials, webhook verification with the account's public key
  - Acceptance: e2e byo_telnyx_outbound passes; invalid key → `INVALID_ENCRYPTED_KEY`.

- [ ] P10-T06 Monthly number billing
  - Spec refs: specs/16-phone-numbers.md (monthly number billing); specs/24-billing-and-limits.md (phone_number_month meter)
  - Depends on: P10-T02
  - Deliverables: monthly Temporal schedule posting usage per active platform number (BYO numbers excluded), proration on purchase month
  - Acceptance: time-skipping tests across month boundaries.

## Phase exit gate
`make check && make e2e` pass.
