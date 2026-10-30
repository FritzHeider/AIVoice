# Decision Journal

Consequential decisions taken during the build, newest at the bottom. IDs are sequential.

Template:

```
## DEC-NNN — <short title>

- **Decision:** <the question that had to be answered>
- **Chosen Option:** <what was done>
- **Confidence:** <0-100>
- **Alternatives Considered:** <options and why they lost>
- **Reasoning:** <why the chosen option wins>
- **Reversibility:** <yes/no, and what undoing it costs>
- **Timestamp:** <UTC ISO 8601>
```

---

## DEC-001 — Where the specs/02 §6 env-table parser lives

- **Decision:** P00-T02 needs to parse the specs/02 §6 environment-variable table to prove `.env.example` mirrors
  it. Should that parser be a reusable module, inline test code, or a new `make check` gate script?
- **Chosen Option:** a library module `scripts/env_spec.py` (typed, no CLI, no import side effects), imported by
  `scripts/tests/test_env_example.py` via the existing `pythonpath = ["scripts"]` pytest setting. Parity is
  enforced through `make test` only.
- **Confidence:** 75
- **Alternatives Considered:**
  - Parser inline in the test module — less reusable, and invisible to the settings tests coming in P00-T05, which
    will want the same §6 row data to check `BaseServiceSettings` field coverage.
  - A new `scripts/check_env_example.py` gate wired into `make check` — rejected: specs/02 §3 fixes the `make check`
    recipe and §9 lists the complete gate table with no env gate in it, so adding one would contradict the spec.
    Enforcing the same property as a pytest test needs no spec change.
- **Reasoning:** the §6 table is a shared contract that at least two future tasks must read programmatically, so the
  parsing logic belongs somewhere importable; keeping it a plain library (no `main`, no argv) means it costs nothing
  if a later phase does decide to wrap it in a gate.
- **Reversibility:** yes — moving the functions into the test module or into a gate script is a module-local change
  with no callers outside `scripts/`.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-002 — Scope and allowances of the unfinished-work detector gate

- **Decision:** specs/02 §9.1 leaves open which files count as "tracked", how case-insensitive the markers are, and
  whether common legitimate uses (Tailwind `placeholder:` variant, `@overload` stubs, `.pyi` files, the gate's own
  module name) must fail.
- **Chosen Option:** scan tracked + untracked-not-ignored files; markers upper-case only with word boundaries; allow
  the Tailwind `placeholder:` variant, the identifier `check_no_placeholders`, `@overload` bodies, and `.pyi` files;
  no generic suppression comment. Recorded as the `> Decision (2026-10-25)` block under specs/02 §9.1.
- **Confidence:** 75
- **Alternatives Considered:**
  - Tracked files only — rejected: Builder output is uncommitted when gates run, so the gate would miss it.
  - Case-insensitive markers — rejected: flags ordinary English ("hack", "todo list") and forces awkward prose.
  - Failing on Tailwind `placeholder:` — rejected: shadcn/ui's Input ships it; it is the utility form of the
    allowed `::placeholder` pseudo-element. Failing on the module name would make `make placeholders` unrunnable.
- **Reasoning:** keeps the gate strict on real unfinished work while not punishing framework idioms the spec
  clearly intended to allow.
- **Reversibility:** yes — each allowance is one regex alternative or predicate in `scripts/check_no_placeholders.py`.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-003 — OpenAPI drift gate behaviour before the API exists

- **Decision:** `make check` runs `scripts/check_openapi_drift.py` from P00-T04 on, but `va_api` and
  `openapi/openapi.json` only arrive in P00-T11. Should the gate fail, be left out of `make check`, or skip?
- **Chosen Option:** skip with exit 0 and a stderr notice only while BOTH the `va_api` package and the committed
  document are absent; any half-state or import failure exits 1. Recorded in the specs/02 §9 Decision block.
- **Confidence:** 80
- **Alternatives Considered:**
  - Fail until P00-T11 — rejected: `make check` would be red for six tasks, violating "keep the build green".
  - Remove the call from the `make check` recipe — rejected: specs/02 §3 fixes the recipe.
- **Reasoning:** the skip condition cannot be hit once P00-T11 lands (both artefacts exist), so the gate becomes
  strict automatically with no later edit needed.
- **Reversibility:** yes — delete the bootstrap branch in `main()` once `va_api` exists.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-004 — Default HTTP status for the two-status `KB_ERROR` code

- **Decision:** specs/31 §3 lists `KB_ERROR` as `400/500`, but `CODES` maps each code to one status.
- **Chosen Option:** `CODES["KB_ERROR"] = 400`; `500` allowed only as an explicit `http_status` override via
  `va_common.errors.ALTERNATE_STATUSES`. Recorded as the `> Decision (2026-10-25)` block in specs/31 §4.
- **Confidence:** 75
- **Alternatives Considered:**
  - Default 500 — rejected: most KB failures (bad file content, parse failures) are caused by the client upload.
  - `CODES` values as sets — rejected: specs/31 §4 fixes `CODES: dict[str, int]`.
- **Reasoning:** keeps the spec's type, makes the first-listed status the default, and still lets server-side
  failures (embedding provider down) report 500 deliberately.
- **Reversibility:** yes — change one entry in `CODES`/`ALTERNATE_STATUSES` and the spec note.
- **Timestamp:** 2026-10-25T00:00:00Z

## DEC-005 — Split P03-T02 (FakeTelnyx) into REST/webhooks and call control

- **Decision:** P03-T02 covers ~40 endpoints, the signed webhook emitter, and scenario-driven call progress; too
  large for one Builder activation with full tests.
- **Chosen Option:** P03-T02a (keys, auth/accounts, errors, provisioning/number/message REST, webhook emitter,
  supervised tasks) then P03-T02b (dial, call actions, conferences, scenario progress, AMD, inbound, call control
  API). P03-T03 and P03-T08 now depend on P03-T02b.
- **Confidence:** 85
- **Alternatives Considered:**
  - One task — rejected: P03-T01 (smaller) already filled an activation; a blocked build costs a full cycle.
  - Split by HTTP family only — rejected: call control needs the emitter and keys first, so they go in T02a.
- **Reasoning:** T02a is self-contained and testable (messages and number orders exercise the emitter end to end).
- **Reversibility:** yes — the plan ids are the only artefact.
- **Timestamp:** 2026-10-29T00:00:00Z
