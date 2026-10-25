## Phase 10 — Phone Numbers, Inbound Configuration, Number Pools, and Bring-Your-Own-Telnyx

### Goal
Complete the number lifecycle: search Telnyx inventory, purchase (`/numbers/purchase`), configure inbound agents
(`/v1/inbound/{phone_number}` with every field), labels, release, number pools with selection strategies and
local-presence dialing, monthly number billing, and BYO Telnyx accounts (`/v1/accounts` → `encrypted_key`) including
bootstrapping Call Control in the customer's Telnyx account and routing calls through it.

### Read first
- `specs/16-phone-numbers.md` — entire file (§4 InboundConfig, §7 BYO Telnyx).
- `specs/06-telephony-telnyx.md` — number search/order/update/delete operations, bootstrap, BYO handling.
- `specs/24-billing-and-limits.md` — `phone_number_month` meter.
- `specs/31-error-catalog.md` — `NO_NUMBERS_FOUND`, `NUMBER_UNAVAILABLE`, `INVALID_ENCRYPTED_KEY`, `CARRIER_CONNECTION_ERROR`.

### Phase exit gate
`make check && make e2e` pass including new flow `purchase_and_inbound` (buy a simulator number, configure a pathway,
receive an inbound call that runs it) and `byo_telnyx_outbound` (register a BYO account in the simulator and place a
call with `encrypted_key`).
