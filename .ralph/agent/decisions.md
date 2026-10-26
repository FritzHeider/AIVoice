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
