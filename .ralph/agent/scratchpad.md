# Ralph scratchpad — VoiceAgents, phase 00

Thinking journal for the current objective. Append new entries at the bottom.

---

## 2026-10-25 — Planner orientation

`plan/phase-00.md` state: P00-T01 ticked. First unticked task with all dependencies satisfied is
**P00-T02 `.env.example` and settings contract** (depends on P00-T01 ✓). Previous event was `work.start`, not
`build.blocked`, so no task splitting is needed.

Runtime task list was empty (`ralph tools task ready` → "No ready tasks"), and this scratchpad had been deleted
from the worktree, so both are being recreated from the plan file.

`BaseServiceSettings` itself belongs to **P00-T05**, not here. P00-T02 is only the committed env contract plus the
test that keeps it locked to specs/02 §6.

---

## Plan P00-T02 — `.env.example` and settings contract

### Spec reading (done)

- `specs/02-repo-layout-and-conventions.md` §1 lists `.env.example` at the repository root (committed).
- §6 is the normative table: 104 rows, lines 186–289 of the spec, 4 cells per row
  (`Variable | Used by | Dev value | Meaning`). Measured composition:
  - 105 variable names (one row, `` `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` ``, declares two names and two
    dev values `` `minio` / `minio12345` ``);
  - 1 of those names is a template, `LOG_LEVEL_<COMPONENT>` (dev value `unset`) → **104 literal keys** must appear
    in `.env.example`;
  - dev-value cells fall into exactly three classes with no per-variable exceptions needed:
    **76 literal** (cell starts with a backtick → required value is the first backticked span, e.g.
    `` `localhost:8100` (tunnel host for live tests) `` → `localhost:8100`; `` `console` in local, `json` elsewhere ``
    → `console`), **16 empty** (cell is `empty` or starts with `empty `, e.g. `empty (host-only cookie)`), and
    **12 free/descriptive** (`64 random hex chars`, `64 hex chars`, `hostname`, `generated at startup`,
    `created by bootstrap`, `created by `scripts/telnyx_bootstrap.py``, `created by `scripts/seed_dev.py` / bootstrap`,
    `sim public key printed by `make up``, `unset`).
  - The "starts with a backtick" test is what keeps `sim public key printed by `make up`` and
    `created by `scripts/telnyx_bootstrap.py`` out of the literal class even though they contain backticks.
- §7: `.env` is gitignored, `.env.example` is committed — `.gitignore` already has `.env`, `.env.*`,
  `!.env.example`, so no `.gitignore` change is needed.
- §9: the gate table does **not** contain an env gate, and §3 fixes the `make check` recipe. So parity is enforced
  through `make test` (pytest) only — do **not** add a new command to `make check` or a new Makefile target.

### Mirror contract (resolves the §6 ambiguity; must be written into the spec in the same commit)

`.env.example` mirrors §6 under these rules, which the test enforces:

1. One `NAME=value  # comment` line per spec variable, in **spec table order**, with a non-empty trailing comment.
2. Literal-class rows carry the spec's literal value verbatim. Values containing spaces are double-quoted
   (`EMAIL_FROM="VoiceAgents <no-reply@voiceagents.local>"`); the parser strips one layer of surrounding quotes
   before comparing.
3. Empty-class rows are present with an empty value (`AWS_KMS_KEY_ID=`).
4. Free-class rows are present with a usable local value; the test asserts presence only, not the value. Secret-shaped
   free rows (`SECRET_KEY`, `STREAM_TOKEN_SECRET`, `LOCAL_MASTER_KEY`, `API_KEY_PEPPER`) get 64 hex characters that are
   visibly non-random dev fillers (repeated `deadbeef`/`cafebabe`/`feedface`-style patterns, one distinct pattern per
   variable) and a trailing comment telling the reader to regenerate with `openssl rand -hex 32`. No real entropy and
   no real credential ever lands in the file.
5. Templated names (any name containing `<`) are **not** assignments; they appear only as a commented example line
   whose text contains the templated name, e.g.
   `# LOG_LEVEL_VA_MEDIA=DEBUG  # per-logger override, LOG_LEVEL_<COMPONENT>; unset by default`.
6. Standalone comment lines are free (section headers like `# --- Telephony (Telnyx) ---` and the file preamble).
   Blank lines are free.
7. No duplicate keys; no key absent from §6.

Add this to `specs/02-repo-layout-and-conventions.md` §6 as a single
`> Decision (2026-10-25): …` block directly under the table, in the same commit (master prompt rule 1005/§8).

### Files

**Create `scripts/env_spec.py`** — typed, no CLI, no side effects at import. Imported by the test
(`pythonpath = ["scripts"]`). Public interface:

```python
SPEC_PATH: Final[Path]          # repo-root-relative specs/02-repo-layout-and-conventions.md
ENV_EXAMPLE_PATH: Final[Path]   # repo-root-relative .env.example
SECTION_HEADING: Final[str]     # "## 6. Environment variables (complete list; mirror in `.env.example` with dev values)"

class DevValueKind(StrEnum):      # LITERAL | EMPTY | FREE
    ...

@dataclass(frozen=True, slots=True)
class SpecVar:
    name: str                     # e.g. "S3_ACCESS_KEY_ID"
    used_by: str
    dev_cell: str                 # raw Dev value cell
    kind: DevValueKind
    literal: str | None           # set iff kind is LITERAL
    meaning: str
    templated: bool               # "<" in name

@dataclass(frozen=True, slots=True)
class EnvAssignment:
    name: str
    value: str                    # one layer of surrounding quotes stripped
    comment: str                  # trailing comment text without the leading "#", stripped
    line_no: int

def repo_root() -> Path: ...                              # parents of __file__, no cwd dependence
def parse_spec_table(markdown: str) -> list[SpecVar]: ...  # skips the |---| separator row; splits "A / B" names and values
def parse_env_example(text: str) -> list[EnvAssignment]: ...
def commented_lines(text: str) -> list[str]: ...           # standalone "#" lines, for the templated-name check
def split_trailing_comment(rhs: str) -> tuple[str, str]: ...  # quote-aware: quoted value first, then "#"
```

Parsing details to get right:
- table slice = lines after the §6 heading starting with `|`, drop the header row and the `|---|` separator row,
  stop at the first non-`|` line; assert exactly 4 cells per row.
- name cell: strip `` ` ``, split on `/`, strip each; a 2-name row also splits its dev cell on `/` positionally.
- literal extraction: `re.match(r"`([^`]*)`", cell)`.
- `split_trailing_comment`: if the value starts with `"` or `'`, consume through the matching closing quote, then the
  remainder must be blank or start with `#`; otherwise split on the first `#`.

**Create `.env.example`** — 104 assignments in spec order, grouped by section-header comments, preamble explaining
`cp .env.example .env`, that `.env` is gitignored, and that `make up` writes the simulator Telnyx public key to
`.sim/telnyx_public_key`. Every assignment line gets a trailing comment: use the spec Meaning cell when it is
non-empty, otherwise a short factual note (and append the parenthetical tail of the dev cell, e.g. "tunnel host for
live tests", where the spec has one).

**Create `scripts/tests/test_env_example.py`** — tests (names and assertions):
- `test_spec_table_shape` — 104 rows parsed, 105 names, names unique, exactly one templated name
  (`LOG_LEVEL_<COMPONENT>`), class counts 76/16/12.
- `test_s3_credential_row_splits_into_two_vars` — `S3_ACCESS_KEY_ID` → `minio`, `S3_SECRET_ACCESS_KEY` → `minio12345`.
- `test_literal_extraction_ignores_trailing_prose` — table-driven over `EDGE_PUBLIC_HOST` (`localhost:8100`),
  `LOG_FORMAT` (`console`), `WEBHOOK_ALLOW_PRIVATE` (`true`), `LLM_MODEL_COSTS_JSON` (`{}`).
- `test_descriptive_dev_cells_are_free` — `TELNYX_PUBLIC_KEY` and `TELNYX_CALL_CONTROL_APP_ID` are `FREE` despite
  containing backticks.
- `test_env_example_has_no_duplicate_keys`.
- `test_env_example_covers_every_spec_variable` — set difference both ways; failure message lists missing and extra
  names (this is the acceptance criterion in both directions).
- `test_env_example_order_matches_spec_order` — separate assertion so ordering drift is distinguishable from
  presence drift.
- `test_env_example_literal_values_match_spec`.
- `test_env_example_empty_valued_variables_are_empty`.
- `test_every_assignment_has_a_trailing_comment`.
- `test_templated_variables_appear_only_as_commented_examples` — `LOG_LEVEL_<COMPONENT>` is not an assignment and is
  mentioned in a standalone comment line.
- `test_secret_shaped_values_are_64_hex_and_distinct` — the four secret-shaped free vars are `[0-9a-f]{64}`, mutually
  distinct, and their comments mention `openssl rand -hex 32`.
- `test_parity_detects_variable_added_to_spec_only` / `test_parity_detects_variable_added_to_env_only` — synthetic
  mini spec/env strings through the parsers, proving the check fails in each direction.
- `test_split_trailing_comment_handles_quoted_values` — `'"a b"  # c'` → `("a b", "c")`, plain, and no-comment cases.
- `test_email_from_is_quoted_in_the_file` — raw-text assertion that the quoting convention is actually used.

### Also in this commit

- `specs/02-repo-layout-and-conventions.md` §6: the `> Decision (2026-10-25)` mirror-contract block above.
- `AGENTS.md` "Learned during the build": one dated bullet — `.env.example` mirrors specs/02 §6 one-to-one in table
  order and `scripts/tests/test_env_example.py` fails on drift in either direction; add new variables to the spec
  table and the example file together.
- `.ralph/agent/decisions.md`: create with the header template from the `ralph-tools` skill if absent, then
  **DEC-001** — put the §6 table parser in `scripts/env_spec.py` rather than inline in the test. Confidence 75.
  Alternatives: parser inside the test module (less reusable, invisible to future settings tests); a new
  `scripts/check_env_example.py` gate (rejected — §3/§9 fix the `make check` recipe and list no env gate, so adding
  one would contradict the spec). Reversible: yes, module-local move.
- Tick `- [x] P00-T02 … — <evidence>` only in the Reviewer pass, not by the Builder.

### Gates to run (Builder)

`make fmt` → `make lint` → `make typecheck` → `make test`, plus `make test-ts` (unchanged TS, should stay green).
`make placeholders` and the openapi/spec-ref gates do not exist yet (P00-T03/P00-T04) — report them as not-yet-present
rather than passing. Backpressure tokens for `build.done` must be obtained for real per AGENTS.md
(`uvx pip-audit` over `uv export`, `corepack pnpm audit`, `uvx radon cc -s -a`, `npx --yes jscpd@4`) and the payload
emitted as a plain string, never `--json`.

### Risks

- **Coverage gate.** `make test` runs `--cov-fail-under=85` over `scripts`, `packages`, `apps`, `sdks`.
  `scripts/env_spec.py` must be fully exercised by the new test, including the quote-aware comment splitter's
  no-comment and single-quote branches, or the gate fails for the whole repo.
- **mypy strict** covers `scripts` (including `scripts/tests`): annotate every test function `-> None` and type the
  parser helpers precisely; `files`/`mypy_path` in the root `pyproject.toml` already include `scripts`, so no config
  change is needed.
- **`filterwarnings = ["error"]`** is active; keep the parser free of deprecated stdlib calls.
- **Secret-shaped example values.** Keep them obviously synthetic (repeated hex words) so no reviewer or scanner
  mistakes `.env.example` for a leaked credential.
- **Spec edit vs. ruff.** The root ruff config excludes `*.md`, so adding the Decision block cannot be reformatted —
  but keep the block inside 110 columns anyway to match the surrounding file.

---

## 2026-10-25 — Builder P00-T02, implementation notes

Everything in the plan was written, with two corrections to the plan's measurements and one environment problem that
stopped the gate run.

### Correction 1 — dev-value class counts are 77/16/12, not 76/16/12

Measured with the real parser against `specs/02-repo-layout-and-conventions.md`:

```
names 105
Counter({'literal': 77, 'empty': 16, 'free': 12})
unique 105
templated ['LOG_LEVEL_<COMPONENT>']
literal-keys 104
```

The plan's 76 does not add up (76+16+12 = 104 ≠ 105 names). `LOG_LEVEL_<COMPONENT>` has dev value `unset`, so it is
already counted in the 12 FREE rows; the remaining split is 77 LITERAL + 16 EMPTY. The 12 FREE names are
`SECRET_KEY`, `STREAM_TOKEN_SECRET`, `LOCAL_MASTER_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_CALL_CONTROL_APP_ID`,
`TELNYX_MESSAGING_PROFILE_ID`, `TELNYX_OUTBOUND_VOICE_PROFILE_ID`, `POD_ID`, `PLATFORM_POOL_ORG_ID`,
`SIM_ED25519_PRIVATE_KEY`, `API_KEY_PEPPER`, `LOG_LEVEL_<COMPONENT>`. `test_spec_table_shape` asserts 77/16/12.

### Correction 2 — the table has 104 rows declaring 105 names

Same thing stated the other way: 104 literal keys go into `.env.example`, because the 105th name is the template.
Both numbers are asserted separately so a future row addition fails loudly on whichever is wrong.

### Decisions made while writing the files

- Non-secret FREE variables whose value is produced by the local stack (`TELNYX_PUBLIC_KEY`,
  `TELNYX_CALL_CONTROL_APP_ID`, `TELNYX_MESSAGING_PROFILE_ID`, `TELNYX_OUTBOUND_VOICE_PROFILE_ID`,
  `PLATFORM_POOL_ORG_ID`, `SIM_ED25519_PRIVATE_KEY`, `POD_ID`) are assigned empty with a comment naming what writes
  them (`.sim/telnyx_public_key` from `make up`, `scripts/telnyx_bootstrap.py`, `scripts/seed_dev.py`). Inventing a
  fake id would be mock data in a committed config file; the test only asserts presence for the FREE class.
- The four secret-shaped FREE variables get distinct repeated-hex-word fillers (`deadbeef…`, `cafebabe…`,
  `feedface…`, `0badc0de…`), each 64 hex chars, each commented with `openssl rand -hex 32`.
- `.env.example` comments avoid every token the P00-T03 placeholder checker will reject (no `TODO`/`TBD`/`XXX`, no
  `placeholder`, no `example.com`, no `your-api-key`) — the file is at the repository root and so is outside the
  checker's scan roots, but the test module in `scripts/` is inside them.
- Added `test_empty_dev_cells_with_trailing_prose_are_empty` (`SESSION_COOKIE_DOMAIN`),
  `test_free_valued_variables_are_all_defined`, `test_parity_detects_changed_literal_value`, and
  `test_preamble_documents_how_to_create_a_local_env` beyond the planned list; every planned test exists.

### Files written

- `scripts/env_spec.py` — `EnvSpecError`, `DevValueKind`, `SpecVar`, `EnvAssignment`, `repo_root`,
  `parse_spec_table`, `classify_dev_value`, `split_trailing_comment`, `parse_env_example`, `commented_lines`,
  `load_spec_variables`, `load_env_example`. `classify_dev_value` is public (it was private in the plan) so the
  three-class rule can be unit-tested directly.
- `.env.example` — 104 assignments in spec order under 24 section headers, with the documented preamble.
- `scripts/tests/test_env_example.py` — 30 test cases including both parity directions and the parser error paths
  (unterminated quote, text after a quoted value, non-assignment line, missing heading, prose instead of a table,
  no data rows, wrong cell count, name/value count mismatch, empty name, unterminated backtick).
- `specs/02-repo-layout-and-conventions.md` §6 — the `> Decision (2026-10-25)` mirror contract under the table.
- `AGENTS.md` — the P00-T02 parity bullet and the `.venv/.DS_Store` gotcha.
- `.ralph/agent/decisions.md` — created with the journal template plus DEC-001 (parser as a library module rather
  than a new `make check` gate, since specs/02 §3/§9 fix the gate list).

## Blocker P00-T02

**What.** The gates were never run. The host filesystem filled up mid-iteration and the shell became unusable: every
command now fails before it executes with

```
ENOSPC: no space left on device, open '/private/tmp/claude-501/.../tasks/<id>.output'
```

because the tool harness cannot create the output file it captures stdout into. `echo ok` fails the same way, so this
is not specific to `uv`, `pytest`, or this repository.

**Why it matters here.** Two consequences:

1. No gate evidence exists. `make fmt`, `make lint`, `make typecheck`, and `make test` were not run against the new
   files, so the ruff formatting of `scripts/env_spec.py` and `scripts/tests/test_env_example.py` is unverified, and
   so is `mypy --strict` over both. Two selected rules were pre-empted by hand because they could not be checked:
   `TRY003` (every `raise EnvSpecError(...)` now passes a `msg` local instead of an inline f-string) and `PLR2004`
   (the `2` and `-1` literals became `_HEADER_ROWS` and `end < 0`). What remains unverified is mostly
   `ruff format` whitespace and line wrapping in both files, which `make fmt` fixes mechanically.
   `make placeholders` and the openapi/spec-ref gates do not exist yet (P00-T03/P00-T04), so they are not-yet-present
   rather than passing, as planned.
2. `ralph emit` also runs through the shell, so this iteration cannot publish its own event. That is why this blocker
   is recorded in the scratchpad only.

**What is known good.** Before the disk filled, the parser was run against the committed spec with the system CPython
3.12 and produced the counts quoted above, which is how corrections 1 and 2 were found. That exercised
`parse_spec_table`, `_table_rows`, `_cells`, `classify_dev_value`, and `load_spec_variables` on the real 104-row
table. `parse_env_example` and the test module have never been executed.

**Earlier environment damage, already repaired.** The same disk pressure had left `.venv` without a `bin/` directory,
and `uv sync` could not rebuild it because a Finder `.DS_Store` kept `.venv/lib` non-empty
(`failed to remove directory '.venv/lib': Directory not empty (os error 66)`). Fixed with
`find .venv -name .DS_Store -delete && rm -rf .venv && uv sync --all-packages`, which exited 0. The gotcha is now in
`AGENTS.md`.

**Proposal.** Free space on the boot volume, then re-run the Builder on P00-T02. No code needs to be rewritten first:
the next activation only has to run `make fmt` → `make lint` → `make typecheck` → `make test`, fix whatever ruff and
mypy report in the two new Python files, and confirm `test_env_example.py` passes — including that the coverage gate
still clears 85% now that `scripts/env_spec.py` is in the coverage source set. Do not re-derive the spec counts; they
are measured above. Two runtime-state items could not be recorded either, because `ralph tools` is also shell-bound:
a `fix` memory for the `.venv/.DS_Store` failure (text is in `AGENTS.md`) and a task for this blocker.

## Review P00-T02 — approved (2026-10-25)

Blocker resolved: disk freed (15 GiB available). Gates run on the committed P00-T02 files with no code changes needed:
`make fmt` (4 files unchanged), `make lint` pass, `make typecheck` pass (mypy, 4 files), `make test` 65 passed,
coverage 99.64% (`scripts/env_spec.py` 99%, one partial branch 89->99). radon avg A (3.29), jscpd 0 clones,
pip-audit and pnpm audit clean. `make placeholders` is not present until P00-T03, as planned. Acceptance verified by
`test_parity_detects_variable_added_to_spec_only` and `test_parity_detects_variable_added_to_env_only`.
Next: P00-T03 placeholder detector gate.
