# Scratchpad — Phase 00

(Earlier entries for P00-T01/T02 were cleared; their evidence lives in plan/phase-00.md ticks and git history.)

## Plan P00-T03 — Placeholder detector gate

Spec: specs/02 §9.1 (authoritative behaviour list). Deliverables: `scripts/check_no_placeholders.py`,
`scripts/tests/test_check_no_placeholders.py`. Acceptance: `make placeholders` exits 0 on the repo; every rule has
at least one failing-fixture test (and a passing/allowed case where the spec defines an allowance).

### Self-reference problem (critical design constraint)
The checker scans `scripts/`, i.e. its own source and its own tests. Neither file may contain a literal banned token.
- In the checker, write regexes with a character class splitting the token: `T[O]DO`, `F[I]XME`, `X[X]X`, `H[A]CK`,
  `T[B]D`, `place[h]older`, `NotImplemented[E]rror`, `implement[ ]me`, `lorem[ ]ipsum`, `your-api[-]key`,
  `example[.]com`, `console[.]log`. No docstring/comment/rule-id may contain the plain words (rule ids are lowercase
  and must avoid the word "placeholder" because that rule is case-insensitive).
- In tests, build fixture text from fragments with a helper `def j(*parts: str) -> str: return "".join(parts)`
  (e.g. `j("TO", "DO")`). Do NOT use implicit string concatenation (`"TO" "DO"`): ruff format joins it into a literal.
- The required file/module name `check_no_placeholders` (and `make placeholders`) contains the word; the
  filler-word rule therefore exempts the exact identifier `check_no_placeholders` (see allowances below).

### File discovery
- `list_candidate_files(root: Path) -> list[Path]`: run
  `git -C <root> ls-files -z --cached --others --exclude-standard -- apps packages sdks infra scripts`
  (subprocess with `timeout=30`, `check=True`); keep only paths that exist as regular files (deleted-but-cached paths
  are skipped). Untracked-but-not-ignored files are included on purpose so the gate sees Builder work before commit.
  If git fails → print `check_no_placeholders: git ls-files failed: ...` to stderr and exit 2.
- `is_excluded(rel: PurePosixPath) -> bool`: any path part in
  `{"node_modules", ".venv", "dist", "build", ".next", "__pycache__", ".turbo", "coverage"}`; name `schema.d.ts`;
  rel == `openapi/openapi.json`; lockfiles `{"uv.lock", "pnpm-lock.yaml", "package-lock.json", "yarn.lock"}`.
- `is_binary(data: bytes) -> bool`: NUL byte in the first 8192 bytes, or UTF-8 decode failure.

### Module layout (`scripts/check_no_placeholders.py`, typed, mypy strict, low CC — keep each function ≤ B)
```python
SCAN_ROOTS: Final = ("apps", "packages", "sdks", "infra", "scripts")
PY_SUFFIXES = {".py"}            # .pyi skipped for stub-body rule (stubs are legitimately `...`)
TS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}

@dataclass(frozen=True, slots=True)
class Finding:
    path: str; line: int; rule: str; excerpt: str
    def render(self) -> str: return f"{self.path}:{self.line}: {self.rule}: {self.excerpt}"

def is_test_path(rel: PurePosixPath) -> bool   # parts contain "tests"/"tests_e2e"/"__tests__", or name matches
                                               # test_*.py, *_test.py, conftest.py, *.test.*, *.spec.*
def is_cli_module(rel: PurePosixPath) -> bool  # under sdks/cli/, or name in {"cli.py","__main__.py"}, or a "cli" dir part
def check_text_rules(rel, text) -> list[Finding]     # line-based regex table, all file types
def check_python(rel, text) -> list[Finding]         # ast: stub bodies, print calls
def check_script_like(rel, text) -> list[Finding]    # TS/JS: empty fn, empty catch, console.log
def check_file(rel: PurePosixPath, data: bytes) -> list[Finding]   # binary/exclusion + dispatch
def scan(root: Path, files: Iterable[Path]) -> list[Finding]       # sorted by (path, line, rule)
def main(argv: Sequence[str] | None = None) -> int
```
CLI: `argparse` with `--root PATH` (default: repo root = `Path(__file__).resolve().parent.parent`) and optional
positional `paths` (relative to root; when given, scan exactly those instead of git listing — used by tests and for
ad-hoc runs). Print each `Finding.render()` to stdout; final summary line to stderr
(`N finding(s)`); return 1 if any finding, else 0. `if __name__ == "__main__": raise SystemExit(main())`.

### Rules (rule id → match)
Line-based (excerpt = stripped line truncated to 120 chars; one finding per rule per line):
| id | regex / condition |
|---|---|
| `marker` | `\b(?:T[O]DO|F[I]XME|X[X]X|H[A]CK|T[B]D)\b` — case-SENSITIVE (lowercase "hack"/"todo" in prose are not markers) |
| `filler-word` | `place[h]older` case-insensitive, after removing allowed spans: `place[h]older\s*=` (HTML/JSX attr, incl. `={`), `::place[h]older` (CSS), `\bplace[h]older:` (Tailwind variant, see Decision), and the identifier `check_no_place[h]olders` |
| `not-implemented` | `\bNotImplemented[E]rror\b` |
| `implement-me` | `implement[ ]me` case-insensitive |
| `lorem-ipsum` | `lorem[ ]ipsum` case-insensitive |
| `sample-api-key` | `your-api[-]key` case-insensitive |
| `example-domain` | `\bexample[.]com\b` case-insensitive, skipped when `is_test_path(rel)` |

Python (`.py` only), via `ast.parse` (SyntaxError → finding `syntax-error` at `exc.lineno or 1`):
| id | condition |
|---|---|
| `stub-body` | FunctionDef/AsyncFunctionDef whose body, after dropping a leading docstring, is empty or only `pass` / `Expr(Constant(Ellipsis))`. Allowed when: enclosing class is a Protocol (any base whose name or attr is `Protocol`, incl. `Protocol[T]` subscripts and `typing.Protocol`); decorated with `abstractmethod` (Name or `abc.abstractmethod` Attribute); decorated with `overload` / `typing.overload` (Decision). |
| `stub-body` | ClassDef whose body (dropping docstring) is only pass/`...`: allowed only if it HAS a docstring and is an exception class (a base name/attr ending in `Error`, `Exception`, or `Warning`, or equal to `BaseException`) or a Protocol. Class with docstring only (no pass) is fine. |
| `print-call` | `Call(func=Name("print"))` when not under `scripts/` and not `is_cli_module(rel)` |
Use an `ast.NodeVisitor` subclass that tracks the class stack (to know Protocol context for methods).

TS/JS (`TS_SUFFIXES`), regex on the full text with `re.MULTILINE`, line number = `text.count("\n", 0, m.start()) + 1`:
| id | regex |
|---|---|
| `empty-catch` | `\bcatch\s*(?:\([^)]*\))?\s*\{\s*\}` (always a failure) |
| `empty-function` | `=>\s*\{\s*\}` and `\)\s*\{\s*\}` — skip a `\)\s*\{\s*\}` match that is the tail of an `empty-catch` match (check whether its start lies inside a catch match span) |
| `console-log` | `\bconsole[.]log\s*\(` unless the file is under a `scripts/` dir part or the line ends with `// va-allow-console` (rstrip) |
Strip nothing from TS (no comment parser); keep it regex-simple as spec says.

### Tests (`scripts/tests/test_check_no_placeholders.py`, `import check_no_placeholders as cnp`)
Use `j()` fragment helper everywhere a banned token appears. Parametrize where possible (keeps jscpd clean).
1. `test_marker_detected[...]` — each of the 5 markers in a .py comment-free string literal, a .ts file, a .yaml file → rule `marker`, correct line.
2. `test_marker_lowercase_ignored` — prose "hack" / "todo list" → no finding; `XXXX` → no finding (word boundary).
3. `test_filler_word_detected` (case-insensitive, `Place…` in a .py) and allowances: JSX `placeholder="Search"`,
   `placeholder={label}`, CSS `input::placeholder {`, Tailwind `placeholder:text-muted`, `import check_no_placeholders` → none;
   a line with an allowed attr AND a bare word still fails.
4. `test_text_rules_detected[...]` parametrized: not-implemented, implement-me, lorem-ipsum (mixed case), sample-api-key.
5. `test_example_domain_non_test_fails` (`packages/x/src/x/a.py`) and `test_example_domain_allowed_in_tests`
   (`packages/x/tests/test_a.py`, `apps/d/src/a.test.ts`).
6. Python stub-body: function `pass`, function `...`, function docstring+pass, async def, method in normal class,
   bare empty-ish class `class A: pass`, exception class WITHOUT docstring → fail. Allowed: Protocol method,
   `typing.Protocol` method, generic `Protocol[T]` method, `@abstractmethod`, `@abc.abstractmethod`, `@overload`,
   exception class with docstring+pass, class with only docstring, real function body → no finding.
   `.pyi` file with `...` bodies → no stub finding.
7. `test_print_call` — fails in `packages/x/src/x/mod.py`; allowed in `scripts/tool.py`, `sdks/cli/src/va_cli/app.py`,
   `packages/x/src/x/__main__.py`, `apps/a/src/a/cli.py`; the string `"print("` inside a literal does not fail (AST-based).
8. `test_syntax_error_reported`.
9. TS: `() => {}`, `function f() {}`, multi-line `{\n  \n}`, `.catch(() => {})`, `catch {}` and `catch (e) {\n}` →
   `empty-catch` exactly once (not also `empty-function`); non-empty bodies and `({})` object literal pass.
10. console.log: fails in `apps/dashboard/src/a.ts`; allowed with trailing `// va-allow-console`; allowed in
    `apps/dashboard/scripts/gen.ts`.
11. Exclusions: `is_excluded` true for node_modules/.venv/dist/build/.next, `schema.d.ts`, lockfiles,
    `openapi/openapi.json`; binary data (NUL byte) and invalid UTF-8 → `check_file` returns [].
12. `test_finding_render_format` — `path:line: rule: excerpt`, excerpt truncated to 120 chars.
13. `main` with explicit paths in `tmp_path`: exit 1 + stdout lines for a dirty file; exit 0 for clean files.
14. git discovery: `tmp_path` repo (`git init -q`, set user.email/name via `-c`), tracked + untracked-not-ignored file
    under `scripts/` both found; gitignored file and a file outside SCAN_ROOTS not found; a tracked-then-deleted file
    skipped. `main(["--root", str(non_git_dir)])` → returns 2.
15. `test_repository_is_clean` — `cnp.main([])` on the real repo returns 0 (this is the acceptance criterion as a test).
Coverage target ≥ 95 % on the new module (`--cov-fail-under=85` global).

### Also update
- specs/02 §9.1: append `> Decision (2026-10-25):` block recording: (a) files = git tracked + untracked-not-ignored
  under the 5 roots; (b) markers case-sensitive uppercase with word boundaries, other phrases case-insensitive;
  (c) extra allowances: Tailwind `placeholder:` variant (shadcn/ui Input uses it; it is the utility form of
  `::placeholder`) and the literal identifier `check_no_placeholders` (spec-mandated file name);
  (d) `@overload` stubs and `.pyi` files allowed for `...` bodies; (e) "CLI modules" = `sdks/cli/**`, any `cli`
  package dir, `cli.py`, `__main__.py`; (f) test paths definition; (g) no generic inline suppression exists — only
  `// va-allow-console`.
- AGENTS.md "Learned during the build": how to write fixtures with banned tokens (fragment join, no implicit concat),
  and the gate's file discovery (untracked files are scanned).
- `.ralph/agent/decisions.md`: DEC-002 for the allowances above.
- plan tick is the Reviewer's job.

### Gates
`make fmt lint typecheck placeholders test` (test-ts unaffected but run `make test-ts` for evidence); plus audit
(`uvx pip-audit` on `uv export`, `corepack pnpm audit`), `uvx radon cc -s -a scripts`, `npx --yes jscpd@4`.
Emit build.done as a plain string per the memory.

### Risks
- radon CC: the visitor and rule dispatch can grow; keep table-driven, split predicates.
- jscpd may flag repetitive parametrized fixtures; prefer `pytest.mark.parametrize` tables over copy-pasted tests.
- `filterwarnings = ["error"]`: `ast.parse` on fixture code with invalid escapes raises SyntaxWarning → error;
  keep fixture sources free of invalid escapes (use raw strings where backslashes appear).
- `test_repository_is_clean` depends on git being present (it is; P00-T01 toolchain check requires it).

## Build P00-T03 — done (Builder)

Files: `scripts/check_no_placeholders.py` (new), `scripts/tests/test_check_no_placeholders.py` (new, 111 tests),
specs/02 §9.1 `> Decision (2026-10-25)` block (a–h), AGENTS.md (2 learned bullets), decisions.md DEC-002.
Deviation from plan: none of substance. `.pyi` files are simply not AST-checked (check_python only runs on `.py`);
print/console "scripts" exemption = any `scripts` directory component (covers top-level scripts/ and
apps/*/scripts/). Decorators that are calls (`@registry.register()`) do not allow stub bodies (tested).
Gate evidence (this iteration): make fmt 0; make lint 0; make typecheck 0 (mypy: 6 files ok); make placeholders 0
("0 finding(s)"); make test 0 (176 passed, total cov 99.81 %, checker module 100 %); make test-ts 0 (no TS projects
yet); pip-audit + pnpm audit: no known vulnerabilities; radon cc avg A (3.03), checker max B (7, check_text_rules);
jscpd: 0 clones.

## Plan P00-T04 — Spec reference and OpenAPI drift gates

Spec: specs/02 §3 (`make check` = gates + `check_openapi_drift.py` + `check_spec_refs.py`), §9 (pass = exit 0),
plan/phase-00.md P00-T04 deliverables. Makefile `check` target already calls both scripts (no Makefile change).
Acceptance: both scripts exit 0 on the repo and exit 1 on crafted fixtures. Same self-reference care as T03:
no banned tokens in new files (use `j()` fragments in tests if one is ever needed; none should be).

### A. `scripts/check_spec_refs.py` (typed, mypy strict, every function CC ≤ B)
Prototype run (Planner, this iteration) with the rules below found 0 problems on current plan/ + prompts/.

```python
SOURCE_GLOBS: Final = ("plan/*.md", "prompts/*.md")
SPEC_RE  = re.compile(r"specs/(?P<num>\d{2})(?:-(?P<slug>[a-z0-9-]+)\.md)?")   # full or short form
SECT_RE  = re.compile(r"§\s?(?P<sec>\d+(?:\.\d+)*)")
HEAD_RE  = re.compile(r"^#{2,6} (?P<sec>\d+(?:\.\d+)*)[. ]", re.MULTILINE)

@dataclass(frozen=True, slots=True)
class Problem:
    path: str; line: int; kind: str; detail: str
    def render(self) -> str: return f"{self.path}:{self.line}: {self.kind}: {self.detail}"

def load_spec_index(specs_dir: Path) -> dict[str, frozenset[str]]   # filename -> section numbers from HEAD_RE
def resolve_spec(match: re.Match[str], index) -> tuple[str | None, Problem-kind | None]
        # full form: "<num>-<slug>.md" must be a key -> else kind "missing-spec"
        # short form "specs/NN" (no slug): exactly one key startswith "NN-" -> else "ambiguous-spec"/"missing-spec"
def check_line(rel: str, lineno: int, line: str, index) -> list[Problem]
def check_text(rel: str, text: str, index) -> list[Problem]
def check_repo(root: Path) -> list[Problem]     # sorted(glob) over SOURCE_GLOBS
def main(argv: Sequence[str] | None = None) -> int   # --root (default repo root = parent of scripts/)
```
Pairing rule (line-scoped): tokenize the line into spec refs and § refs ordered by position; each § ref pairs with the
nearest preceding *successfully resolved* spec ref on the same line. A § ref with no preceding spec ref on that line
is unpaired and ignored (e.g. "spec §9.1" or "every model in §3–§9" in prose). A § ref after an unresolved spec ref
is ignored (the spec problem is already reported). Ranges `§3–§9` are just two § tokens, each checked.
Section resolution: `sec` must be in the file's heading set; HEAD_RE matches `## N.`, `## N `, and deeper levels
(`### 9.1 ...`) so dotted refs resolve to subsection headings. Problem kinds: `missing-spec`, `ambiguous-spec`,
`missing-section` (detail: `specs/<file> has no heading for §N`).
Output: each problem to stdout; summary `check_spec_refs: N problem(s)` to stderr; exit 1 if any, else 0.
Missing `specs/` dir → stderr message, exit 2.

### B. `scripts/check_openapi_drift.py`
```python
DEFAULT_APP: Final = "va_api.main:app"
DEFAULT_SPEC: Final = Path("openapi/openapi.json")
class AppLike(Protocol):
    def openapi(self) -> dict[str, Any]: ...          # Protocol method: allowed by placeholder gate
def render(schema: Mapping[str, Any]) -> str:        # json.dumps(sort_keys=True, indent=2, ensure_ascii=False) + "\n"
def load_app(target: str) -> AppLike                 # "module:attr" via importlib; ValueError on bad format
def compare(expected: str, actual: str) -> list[str] # unified diff lines (difflib, fromfile=committed, tofile=generated), capped at 200 lines
def main(argv=None) -> int                           # --root, --app (default DEFAULT_APP), --spec (default DEFAULT_SPEC rel to root)
```
Behaviour / exit codes:
- 0 when committed file == `render(app.openapi())`. Print `check_openapi_drift: openapi/openapi.json is current`.
- 1 on drift: print the diff to stdout + hint `run: make openapi` to stderr.
- 1 when exactly one of {app module importable, committed spec file} exists (app without spec → "missing committed
  spec"; spec without app → "cannot import va_api.main"). Import errors *inside* an existing module (not
  ModuleNotFoundError for the top-level package itself) propagate as failure 1 with the error text.
- **Bootstrap state**: top-level package of `--app` not found (`importlib.util.find_spec("va_api") is None`) AND spec
  file absent → print `skipped: va_api not present yet (lands in P00-T11)` to stderr, exit 0. This is the current repo
  state; P00-T11 adds both and from then on the gate is strict.
- `render` is the single serializer; P00-T11's `scripts/export_openapi.py` must import and use
  `check_openapi_drift.render` so export and check can never disagree (note this in the spec Decision).
Insert root (`--root`) on `sys.path` is NOT needed: va_api will be a uv workspace member installed in the venv.
Tests pass `--app` pointing at a fixture module written into `tmp_path` and add that dir via `monkeypatch.syspath_prepend`.

### C. Tests
`scripts/tests/test_check_spec_refs.py` (`import check_spec_refs as csr`), fixture repo built in `tmp_path`
(`specs/02-conv.md` with `## 3. Make`, `### 9.1 Detail`, `## 4 Python`; `specs/31-errors.md` with `## 3. Codes`;
`plan/phase-00.md`, `prompts/phase-00.md`):
1. full-form ref to existing spec ok; to missing file → `missing-spec` with correct line number.
2. short form `specs/02` resolves; `specs/77` → missing-spec; two files `05-a.md`,`05-b.md` + `specs/05` → ambiguous-spec.
3. `§3`, `§ 3` (space), `§9.1` dotted subsection, `## 4 Python` (no dot) all resolve; `§8` → missing-section.
4. pairing: `specs/02 §3; specs/31 §3` each against its own file; `specs/31 … §9` fails against 31 not 02;
   `§9` before any spec ref → ignored; `§3–§9` with spec 02 lacking 9 → one missing-section for 9 only.
5. § after an unresolved spec ref is not reported (only the missing-spec problem).
6. files outside plan/ and prompts/ (e.g. `docs/x.md`) are not scanned.
7. `Problem.render` format; `load_spec_index` ignores `# Title` (h1) and non-numeric headings.
8. `main(["--root", tmp])`: exit 1 + stdout lines on broken fixture; 0 on clean; 2 when specs/ missing.
9. `test_repository_refs_resolve`: `csr.main([])` on the real repo == 0 (acceptance as a test).

`scripts/tests/test_check_openapi_drift.py` (`import check_openapi_drift as cod`), fixture app = module file
`fakeapi_<uuid-hex>.py` in tmp_path (unique name per test to avoid sys.modules caching; or `monkeypatch.delitem(sys.modules, …, raising=False)`)
defining `class _App: def openapi(self): return {...}` and `app = _App()`:
1. `render` sorts keys at every depth, 2-space indent, trailing newline, non-ASCII preserved.
2. matching committed file → 0; drifted (changed path/summary) → 1 and stdout contains `---`/`+++` diff lines.
3. app present, spec missing → 1; spec present, package missing → 1; both absent → 0 with "skipped" on stderr.
4. module present but raises ImportError of another module inside → 1 (not treated as bootstrap skip).
5. bad `--app` format (`no_colon`) → exit 2 via argparse error or explicit message; missing attribute → 1.
6. diff truncation: very large drift caps output at 200 lines + `... diff truncated` marker — NOTE: the marker text
   must not contain banned tokens; use `[diff truncated after 200 lines]`.
7. `test_repository_state` : `cod.main([])` on the real repo == 0 (currently the bootstrap skip).
Coverage ≥ 95 % on both modules.

### D. Docs
- specs/02 §9: append `> Decision (2026-10-25):` block: (a) spec-ref pairing is line-scoped, nearest preceding
  resolved spec ref; unpaired § ignored; short form `specs/NN` must match exactly one file; section = any heading level
  ≥2 starting with the number followed by `.` or space; scanned files = plan/*.md + prompts/*.md. (b) OpenAPI drift:
  canonical rendering `json.dumps(sort_keys=True, indent=2, ensure_ascii=False)+"\n"` shared with export via
  `check_openapi_drift.render`; bootstrap skip only while both va_api and openapi.json are absent; any half-state fails.
- AGENTS.md "Learned during the build": spec-ref pairing rule (write `specs/NN §N` on one line) and the drift
  bootstrap skip ending at P00-T11.
- `.ralph/agent/decisions.md`: DEC-003 (next sequential id) for the bootstrap skip.

### Gates
`make fmt lint typecheck placeholders test test-ts`, then `uv run python scripts/check_spec_refs.py` and
`uv run python scripts/check_openapi_drift.py` (both exit 0) — i.e. full `make check`. Plus `uvx pip-audit` on
`uv export`, `corepack pnpm audit`, `uvx radon cc -s -a scripts`, `npx --yes jscpd@4`. Emit build.done as plain string.

### Risks
- `filterwarnings=error`: importing fixture modules must not emit warnings.
- sys.modules caching between tests → unique module names per test.
- `find_spec("va_api.main")` raises ModuleNotFoundError when parent missing — use `find_spec(top_level)` first.
- jscpd: the two new test files share fixture-building shapes; keep helpers small and parametrize.

## Build P00-T04 — done (Builder)

Files: `scripts/check_spec_refs.py`, `scripts/check_openapi_drift.py` (new), `scripts/tests/test_check_spec_refs.py`
(24 tests), `scripts/tests/test_check_openapi_drift.py` (18 tests), specs/02 §9 `> Decision (2026-10-25)` block
(placed above §9.1), AGENTS.md (2 bullets), decisions.md DEC-003.
Deviations: spec-ref resolution returns a small `Resolution` dataclass instead of a tuple; drift gate catches any
exception from import/openapi() and reports it as exit 1 (message includes the exception type); app/spec half-state
checks are in `_presence_problem`. An en dash in a test fixture is written as `–` (ruff RUF001).
Gate evidence: make fmt 0; lint 0; typecheck 0 (10 files); placeholders 0 (0 findings); test 0 (218 passed, total
cov 99 %, both new modules 100 %); test-ts 0; make check 0 (drift: bootstrap skip; spec refs: 0 problems);
pip-audit + pnpm audit: no known vulnerabilities; radon avg A (2.86 over scripts), max B (7, resolve_spec);
jscpd: 0 clones.

## Review P00-T04

Gates re-run by Reviewer: make lint/typecheck/placeholders/test all 0 (218 passed, 99.87 % cov); make check 0
(drift: bootstrap skip; spec refs: 0 problems). Drift gate, docs, DEC-003 and AGENTS.md bullets look correct.
Rejected for one deliverable gap in the spec-ref gate:

1. `scripts/check_spec_refs.py:26` (`SPEC_RE`) — the plan deliverable is "every `specs/<file>.md` path mentioned in
   `plan/*.md` and `prompts/*.md` exists", but only paths matching `specs/\d{2}-[a-z0-9-]+\.md` are checked as full
   paths; anything else either falls back to the short form or is not matched at all, so misspelled paths pass
   silently. Reproduced with `check_line` against the real `specs/` index — all of these return `[]`:
   - `specs/02-repo_layout.md §3` (underscore → slug group fails → short form `specs/02` resolves to 02 and §3 passes)
   - `specs/02-Repo-Layout.md` (uppercase → same short-form fallback)
   - `specs/021-foo.md` (resolves as `specs/02` → wrong file)
   - `specs/2-foo.md`, `specs/bogus.md` (no match at all → unchecked)
   Required: any token of the form `specs/<name>.md` (excluding globs containing `*`) must be looked up literally in
   the index and reported as `missing-spec` when absent; the short form must only match `specs/NN` NOT followed by a
   further digit, letter, `_` or `-` (e.g. `specs/(?P<num>\d{2})(?![\w-])`). Add each of the five lines above to the
   tests as failing cases (keep the existing `specs/*.md glob is not a reference` passing case), and update the
   specs/02 §9 Decision (a) text if the matching rule wording changes.

## Build P00-T04 (rework after review) — done (Builder)

Fixed review finding 1: `SPEC_RE` is now `specs/(?:(?P<file>[^\s*`'"()<>\[\]|]+?\.md)|(?P<num>\d{2})(?![\w-]))`.
Any `specs/<name>.md` token (no `*`) is looked up literally (missing → `missing-spec`); short form only for exactly two
digits not followed by `[\w-]`. All five reviewer lines now report `missing-spec` (verified against real specs/ index).
Tests: +11 (parametrized malformed full forms incl. the 5 reviewer cases + backtick/paren-wrapped; short form with
trailing digit/letter/_/- is not a reference; short form before `.`/`)`/`,` still resolves). Glob case still passes.
specs/02 §9 Decision (a) wording updated; module docstring updated.
Gates: fmt/lint/typecheck/placeholders 0; test 0 (229 passed, 99.87 %, check_spec_refs 100 %); test-ts 0; make check 0
(spec refs 0 problems); pip-audit + pnpm audit clean; radon avg A (2.85), max B (7 resolve_spec); jscpd 0 clones.

## Review P00-T04 (rework) — approved

Reviewer re-ran: make lint 0, typecheck 0, placeholders 0 (0 findings), test 0 (229 passed, 99.87 %), make check 0.
Verified all five previously-failing lines now report `missing-spec` against the real specs/ index; short form
`specs/02 §3.`, glob `specs/*.md`, backtick-wrapped full form and `#anchor` suffix still resolve correctly.
Spec Decision (a) wording matches implementation. Ticked and committed.

## Plan P00-T05 — `va_common` core: settings, clock, ids, json, errors, money

Spec: specs/02 §4 (time, IDs, money, errors, settings), specs/31 §3 + §4 (CODES parity), specs/01 §4 (env selection),
specs/02 §6 rows `VA_ENV`, `LOG_LEVEL`, `LOG_TRANSCRIPTS`, `LOG_FORMAT`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_TRACES_SAMPLER_ARG`, `SENTRY_DSN` (the "all" rows). No `packages/` dir exists yet — this task creates the first
workspace member. Checked: ruff 0.17 A005 (stdlib-module shadowing) does NOT flag `va_common/time.py` or
`va_common/json.py` (non-strict mode only checks top-level modules), so use the spec names `va_common.time`, `va_common.json`.

### Package scaffold
- `packages/va_common/pyproject.toml`: `name = "va-common"`, `version = "0.1.0"`, `requires-python = ">=3.12,<3.13"`,
  build backend `uv_build` (or hatchling; whichever `uv init --lib` emits with uv 0.12), src layout, deps added with
  `uv add --package va-common pydantic pydantic-settings orjson uuid-utils` (pydantic>=2.9 per specs/02 §2).
- `packages/va_common/src/va_common/__init__.py` (docstring only + `__all__ = []` not needed — keep a one-line docstring),
  `py.typed` (empty marker; the placeholder gate ignores empty marker files? verify; if flagged, it's non-code so fine).
- Root `pyproject.toml`: add `"va-common"` to `[dependency-groups] dev` with `[tool.uv.sources] va-common = { workspace = true }`
  so plain `uv sync`/`uv run` installs it; mypy `files += ["packages/va_common/src", "packages/va_common/tests"]`,
  `mypy_path = "scripts:packages/va_common/src"`. Run `uv lock` and commit `uv.lock`.

### Modules (all mypy strict, every function radon CC ≤ B)
1. `va_common/time.py`
   ```python
   def utcnow() -> datetime                                  # datetime.now(UTC)
   def ensure_utc(dt: datetime) -> datetime                  # ValueError on naive; converts aware -> UTC
   class Clock(Protocol):
       def now(self) -> datetime: ...                        # aware UTC
       def monotonic(self) -> float: ...                     # seconds
       async def sleep(self, seconds: float) -> None: ...
   class SystemClock:  now()=utcnow(), monotonic()=time.monotonic(), sleep=asyncio.sleep
   class FrozenClock:
       def __init__(self, start: datetime | None = None) -> None   # default 2026-01-01T00:00:00Z; naive -> ValueError
       def advance(self, delta: float | timedelta) -> None          # negative -> ValueError; moves now AND monotonic
       def set(self, dt: datetime) -> None                         # jump wall clock only (monotonic unchanged); naive -> ValueError
       async def sleep(self, seconds: float) -> None               # advance(seconds) then await asyncio.sleep(0)
   ```
   (Protocol method bodies `...` are allowed by the placeholder gate — specs/02 §9.1 Decision.)
2. `va_common/ids.py`
   ```python
   class PublicPrefix(StrEnum): TOOL = "TL"; KNOWLEDGE_BASE = "KB"; PROMPT = "PT"
   def new_id() -> uuid.UUID                       # stdlib UUID from uuid_utils.uuid7() (version 7, time-ordered)
   def to_public(prefix: PublicPrefix, value: uuid.UUID) -> str     # f"{prefix}-{value}" lowercase canonical
   def parse_public(value: str) -> tuple[PublicPrefix, uuid.UUID]   # any known prefix
   def from_public(value: str, prefix: PublicPrefix) -> uuid.UUID   # wrong/missing prefix or bad uuid ->
         AppError("INVALID_INPUT", f"Invalid {prefix.value}- id: {value!r}")  (message must not exceed 200 chars: truncate value)
   def is_public(value: str, prefix: PublicPrefix) -> bool
   ```
   Prefix match is case-sensitive (`TL-`); UUID part accepts any case and hyphenated 36-char form only (no braces/urn).
3. `va_common/json.py` (orjson)
   ```python
   def dumps(obj: object, *, sort_keys: bool = False) -> bytes     # default handler: Decimal -> str, pydantic BaseModel ->
                                                                   # model_dump(mode="json"), set/frozenset -> sorted list; else TypeError
   def dumps_str(obj: object, *, sort_keys: bool = False) -> str
   def loads(data: bytes | bytearray | memoryview | str) -> Any
   ```
   Naive datetimes are rejected by default handler path? orjson serialises naive datetimes natively — pass
   `OPT_PASSTHROUGH_DATETIME` and handle datetime in `default`: aware -> `isoformat()` in UTC, naive -> TypeError
   (enforces the "never naive" rule at the boundary). UUID/enum/dataclass stay native.
4. `va_common/errors.py`
   ```python
   CODES: Final[Mapping[str, int]]                   # all 63 rows of specs/31 §3, in table order (MappingProxyType)
   ALTERNATE_STATUSES: Final[Mapping[str, frozenset[int]]] = {"KB_ERROR": frozenset({500})}
   class AppError(Exception):
       def __init__(self, code: str, message: str, http_status: int | None = None, *,
                    field: str | None = None, details: Mapping[str, Any] | None = None) -> None
           # unknown code -> ValueError; http_status None -> CODES[code]; status not in {CODES[code]} | ALTERNATE_STATUSES.get(code)
           # -> ValueError. Attributes: code, message, http_status, field, details (dict copy, default {}).
       def to_error_item(self) -> dict[str, Any]     # {"error","message"} + "field" if set + "details" if non-empty
       def __repr__/__str__                           # "CODE: message" (str), no details leak in str
   def status_for(code: str) -> int                  # CODES lookup, ValueError on unknown
   ```
   Decision: `KB_ERROR` row is `400/500` → `CODES["KB_ERROR"] = 400` (first listed: client-caused processing errors are
   the Bland default) and 500 is permitted only as an explicit `http_status` override.
5. `va_common/money.py`
   ```python
   MICROS_PER_DOLLAR: Final = 1_000_000
   def dollars_to_micros(value: Decimal | int | str | float) -> int   # Decimal(str(value)) for float; quantize to 1e-6
         # ROUND_HALF_EVEN; bool, NaN, ±inf, unparsable str -> ValueError/TypeError (bool -> TypeError)
   def micros_to_dollars(micros: int) -> Decimal                       # exact, quantized to 6 places
   def micros_to_dollars_str(micros: int) -> str                       # fixed 6 decimals: 1_500_000 -> "1.500000", -90_000 -> "-0.090000"
   ```
   bool input to micros_* -> TypeError.
6. `va_common/settings.py`
   ```python
   class Environment(StrEnum): LOCAL="local"; TEST="test"; STAGING="staging"; PRODUCTION="production"
   class LogFormat(StrEnum): CONSOLE="console"; JSON="json"
   class BaseServiceSettings(BaseSettings):
       model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True,
                                         case_sensitive=False)
       service_name: ClassVar[str] = "va"           # subclasses override
       va_env: Environment = Environment.LOCAL       # VA_ENV
       log_level: str = "INFO"                       # validated/upper-cased against DEBUG/INFO/WARNING/ERROR/CRITICAL
       log_transcripts: bool = False                 # forcing false in production belongs to P00-T06 (do not implement here)
       log_format: LogFormat | None = None           # resolved by model_validator: console in local, json elsewhere
       otel_exporter_otlp_endpoint: str = ""         # empty = tracing exporter disabled (wired in P00-T08)
       otel_traces_sampler_arg: float = Field(0.1, ge=0.0, le=1.0)
       sentry_dsn: str = ""
       @property is_production -> bool; @property is_local -> bool
   ```
   Field names map 1:1 to the spec §6 variable names (lower-case). Keep `log_format` typed as `LogFormat` after
   validation (use a `mode="before"` validator on the raw value + `model_validator(mode="after")` via `object.__setattr__`
   only if needed for frozen; simplest: field `log_format: LogFormat` with `default_factory` impossible because it
   depends on va_env → use `model_validator(mode="before")` on the input dict to fill `log_format` from `va_env`).

### Tests (`packages/va_common/tests/`, `conftest.py` with an autouse fixture that `monkeypatch.delenv`s the settings
vars and `chdir`s to `tmp_path` so a developer `.env` never leaks in)
- `test_time.py`: utcnow aware UTC; ensure_utc converts +02:00 and rejects naive; SystemClock now/monotonic monotone,
  `await sleep(0)`; FrozenClock default start, advance(float/timedelta) moves now+monotonic, negative advance and naive
  start/set raise, set() leaves monotonic, `await sleep(5)` advances exactly 5 s without real waiting; both classes satisfy
  `Clock` (assign to a `Clock`-typed variable — checked by mypy).
- `test_ids.py`: new_id version 7, unique over 1000, monotonically non-decreasing by time (sorted ids == generation order
  within the same ms is not guaranteed → assert `.version == 7` and timestamp bits close to now);
  hypothesis `@given(st.uuids(), st.sampled_from(PublicPrefix))`: `from_public(to_public(p,u),p)==u` and
  `parse_public` round-trip; upper-case uuid accepted; wrong prefix / lowercase prefix / missing dash / bad uuid /
  braces → AppError INVALID_INPUT 400 with truncated value in message; is_public true/false.
- `test_json.py`: dumps returns bytes; UUID/datetime(UTC)/Decimal/BaseModel/set/enum serialise; naive datetime and
  unknown object → TypeError; sort_keys orders nested keys; dumps_str/loads round-trip; loads accepts bytes/str/memoryview.
- `test_errors.py`: **parity** — parse `specs/31-error-catalog.md` §3 (repo root = `Path(__file__).resolve().parents[3]`;
  rows matching ``^\| `([A-Z_]+)` \| ([0-9/]+) \|`` between `## 3.` and `## 3a.`) and assert `set(CODES)==set(table)`,
  `CODES[c]==first status`, and `{CODES[c]} | ALTERNATE_STATUSES.get(c, ∅) == all statuses in the cell`; table has 63
  rows and CODES has no extras. AppError defaults status, accepts KB_ERROR 500, rejects KB_ERROR 404 and unknown code,
  to_error_item omits empty field/details, details dict is copied (mutating input doesn't change error), str format;
  status_for.
- `test_money.py`: examples (0, 1, "0.09"→90_000, Decimal("1.2345675") half-even → 1_234_568, 0.1 float → 100_000,
  negatives, "1e-6"); invalid inputs (True, "abc", NaN, inf); hypothesis `st.integers(-10**15, 10**15)`:
  `dollars_to_micros(micros_to_dollars_str(m)) == m` and `dollars_to_micros(micros_to_dollars(m)) == m`;
  `@given(st.decimals(places=6, allow_nan=False, allow_infinity=False, min/max ±1e9))`: exact round-trip.
- `test_settings.py`: defaults (`_env_file=None`); env vars override (VA_ENV=production → is_production, log_format json);
  local default console; explicit LOG_FORMAT wins; invalid VA_ENV / LOG_LEVEL / sampler 1.5 → ValidationError;
  LOG_LEVEL lower-case normalised; `.env` file in tmp cwd is read; settings are frozen (assignment raises);
  subclass with extra field + `service_name` override works and ignores unrelated env vars.
Coverage target 100 % on va_common (global gate 85 %).

### Docs
- specs/31 §4: append `> Decision (2026-10-25):` — KB_ERROR default 400, 500 as explicit override via
  `ALTERNATE_STATUSES`; parity test treats a multi-status cell as {default} ∪ alternates.
- specs/02 §4: append `> Decision (2026-10-25):` — `va_common.time.Clock` API (now/monotonic/sleep, FrozenClock for
  tests); `va_common.json` rejects naive datetimes; money string = fixed 6 decimals, ROUND_HALF_EVEN on input; public
  ids: case-sensitive prefix, canonical lower-case UUID, parse errors → `INVALID_INPUT`.
- AGENTS.md learned bullets: (1) adding a workspace package = member dir + root dev-group entry with workspace source +
  mypy files/mypy_path; (2) va_common tests isolate env via conftest; use `_env_file=None` when constructing settings in tests.
- decisions.md: DEC-004 (KB_ERROR status choice).

### Gates
`make fmt lint typecheck placeholders test test-ts` and full `make check`; `uvx pip-audit` on `uv export`,
`corepack pnpm audit`, `uvx radon cc -s -a scripts packages`, `npx --yes jscpd@4`. Emit build.done as plain string.

### Risks
- `filterwarnings = ["error"]`: pydantic-settings may warn if `env_file` missing? (it does not); hypothesis
  HealthCheck warnings → keep strategies cheap.
- frozen BaseSettings + derived `log_format` → use before-validator, not post-init mutation.
- pytest importlib mode + per-package `tests/` without `__init__.py`: fine; conftest in packages/va_common/tests only.
- `uuid_utils.uuid7()` returns `uuid_utils.UUID`, not stdlib — convert (`uuid.UUID(int=x.int)`) or use `uuid_utils.compat.uuid7`.

## Build P00-T05 — done (Builder)

Created `packages/va_common` (uv_build, src layout, py.typed; deps pydantic>=2.9, pydantic-settings, orjson,
uuid-utils) with `time`, `ids`, `json`, `errors`, `money`, `settings`; root pyproject wires it into the dev group
(workspace source) and mypy files/mypy_path; uv.lock updated.
Deviations from the plan, all following the spec over the plan:
- specs/31 §3 has **62** rows, not 63; the parity test asserts 62, table order, and `{default} ∪ alternates`.
- `BaseServiceSettings` defaults follow the specs/02 §6 table: `log_transcripts=True`,
  `otel_exporter_otlp_endpoint="http://localhost:4317"` (plan said False / "").
- `log_format` field has a typed default (CONSOLE) and is resolved from `va_env` in a before-validator when
  unset/empty, so mypy (no pydantic plugin) does not require it as a constructor arg.
- `new_id()` uses `uuid_utils.compat.uuid7` (returns stdlib `uuid.UUID`). Error echo of bad ids capped at 64 chars.
Docs: specs/31 §4 Decision (KB_ERROR), specs/02 §4 Decision (va_common API), DEC-004, 3 AGENTS.md bullets.
Gates: make fmt 0; lint 0; typecheck 0 (24 files); placeholders 0 (0 findings); test 0 (378 passed, total 99.90 %,
va_common 100 %); make check 0 (drift bootstrap skip; spec refs 0 problems); pip-audit (`uv export --all-packages
--no-emit-workspace` + `--no-deps`) no known vulnerabilities; pnpm audit clean; radon avg A (2.69), max B (10,
pre-existing scripts/tests/test_env_example.py); jscpd 0 clones. No TypeScript changed.

## Review P00-T05

Re-ran gates: make lint 0, typecheck 0 (24 files), placeholders 0 (0 findings), test 0 (378 passed, 99.90 %,
va_common 100 %). CODES parity test, settings defaults vs specs/02 §6, money/ids/time all checked and correct.
Rejected for 2 findings:

1. `packages/va_common/src/va_common/json.py:20` — `OPT_PASSTHROUGH_DATETIME` passes `datetime.date` and
   `datetime.time` to `default` too (not only `datetime`), and `_default` has no branch for them, so
   `dumps(date(2026, 1, 2))` and `dumps(time(1, 2))` raise `TypeError: Type is not JSON serializable: date`
   (verified). Dates are ordinary payload values (billing periods, usage days, Call Object dates) and must serialize.
   Required: after the `datetime` branch, serialize `date` as `isoformat()` (`"2026-01-02"`); for `time`, serialize
   aware times via `isoformat()` and reject naive `time` the same way as naive datetimes (or serialize as
   `isoformat()` and document it — pick one and record it in the specs/02 §4 Decision bullet for `va_common.json`).
   Add tests for `date`, aware/naive `time`, and a `date` nested in a dict/model.
2. `packages/va_common/src/va_common/errors.py:58-66` — specs/02 §4 states the contract as
   `AppError(code, message, http_status, details)`, but `details` is keyword-only here, so the documented positional
   call `AppError("KB_ERROR", "m", 500, {"code": "x"})` raises `TypeError`. Required: make the signature
   `(code, message, http_status=None, details=None, *, field=None)` (spec order; `field` keyword-only since it is not
   in the §4 signature) and add a test calling it positionally with `details`.

## Build P00-T05 (rework after review) — done (Builder)

1. `va_common.json`: `date` -> `isoformat()` (`"2026-01-02"`); aware `time` -> `isoformat()` keeping its own offset
   (no date to convert to UTC); naive `time` -> `TypeError` like naive datetimes. `datetime` branch stays first
   (datetime subclasses date). Tests: bare date, date nested in dict + pydantic model, aware times (UTC and -05:00),
   naive time rejected. specs/02 §4 Decision bullet updated.
2. `AppError` signature now `(code, message, http_status=None, details=None, *, field=None)`; test calls
   `AppError("KB_ERROR", "m", 500, {"code": "x"})` positionally. Decision bullet added to specs/02 §4. No non-test callers.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 (0 findings), test 0 (383 passed, 99 % total, va_common json/errors
100 %), make check 0; pip-audit + pnpm audit clean; radon avg A (2.69); jscpd 0 clones.

## Review P00-T05 (rework) — approved

Re-ran: lint 0, typecheck 0, placeholders 0 (0 findings), test 0 (383 passed, 99.90 %). Verified `dumps` handles
date / aware time and rejects naive time; `AppError("KB_ERROR","m",500,{"code":"x"})` works positionally; tests
cover both; specs/02 §4 Decision updated. Ticked P00-T05 and committed.

## Plan P00-T06 — `va_common` logging and masking

Spec refs: specs/27-observability.md §1 (processors, standard fields, binding, redaction, per-component levels),
specs/27-observability.md §8 (Sentry reuses the log redaction → expose a pure function), specs/02 §4 Logging rule,
specs/02 §6 rows `LOG_LEVEL`, `LOG_TRANSCRIPTS`, `LOG_FORMAT`, `LOG_LEVEL_<COMPONENT>`, `POD_ID`.
Out of scope: tracing/metrics setup, `va_common.observability.init` (P00-T08 — it will call `configure_logging`),
FastAPI request-id middleware (P00-T11), `va_compliance.redaction` (transcript PII, later phase).

### Dependencies
`uv add --package va-common structlog opentelemetry-api` (otel-api only: `otel_trace_processor` reads the current span;
the SDK/exporter arrive in P00-T08). Update the package `description` to mention logging/masking. `uv lock`.

### `packages/va_common/src/va_common/masking.py` (pure, no structlog import)
```python
REDACTED: Final = "[REDACTED]"
def mask_phone(number: str) -> str
    # E.164 "+<7..15 digits>": >= 11 digits -> "+" + first 4 digits + "***" + last 4  (+14155550123 -> +1415***0123)
    # 7..10 digits -> "+" + first 2 + "***" + last 2. Anything not matching ^\+[1-9]\d{6,14}$ -> returned unchanged.
def mask_phones_in_text(text: str) -> str
    # re.sub of (?<![\d+])\+[1-9]\d{6,14}(?!\d) via mask_phone; handles numbers embedded in prose/URLs.
def is_secret_key(name: str) -> bool
    # lower-case, split on [_\-.\s]+ ; True when any segment in
    # {"key","secret","token","password","passwd","authorization","cookie","pepper","dsn","credential","credentials"}
    # and the LAST segment is not in {"prefix","id","ids","count","version","type","name","length"}.
    # => api_key, xi-api-key, client_secret, access_token, Authorization, sentry_dsn, api_key_pepper -> True;
    #    api_key_prefix (access-log field, spec §1), key_id, pepper_version, prompt_tokens, completion_tokens,
    #    turn_index, monkey -> False ("tokens" plural is a count, not a secret).
```

### `packages/va_common/src/va_common/logging.py`
```python
TRANSCRIPT_KEYS: Final = frozenset({"text", "transcript"})
RAW_MEDIA_KEYS: Final = frozenset({"audio", "payload"})
@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    mask_phones: bool = True
    allow_transcripts: bool = False
    @classmethod
    def from_settings(cls, settings: BaseServiceSettings) -> RedactionPolicy
        # mask_phones = not (log_level == "DEBUG" and is_local); allow_transcripts = transcripts_allowed(settings)
def transcripts_allowed(settings: BaseServiceSettings) -> bool   # log_transcripts and not is_production (forced false)
def redact(data: Mapping[str, Any], policy: RedactionPolicy, *, hipaa: bool = False) -> dict[str, Any]
    # recursive (dict / list / tuple values, depth cap 8 -> deeper containers replaced by REDACTED):
    #  - RAW_MEDIA_KEYS: key dropped; TRANSCRIPT_KEYS: dropped unless policy.allow_transcripts and not hipaa
    #  - is_secret_key(key): value -> REDACTED
    #  - bytes/bytearray/memoryview values -> f"<{n} bytes>" (raw audio never reaches output)
    #  - str values -> mask_phones_in_text when policy.mask_phones; other values unchanged
    # Pure function: P00-T08 Sentry before_send reuses it.
class RedactProcessor:
    def __init__(self, policy: RedactionPolicy) -> None
    def __call__(self, logger: object, method_name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]
        # hipaa = bool(event_dict.get("hipaa_mode")) (bound by org context); returns redact(...) result.
        # The "event" message string is phone-masked too.
def otel_trace_processor(logger, method_name, event_dict) -> event_dict
    # opentelemetry.trace.get_current_span().get_span_context(); if is_valid: trace_id = f"{id:032x}", span_id = f"{id:016x}"
def component_levels(environ: Mapping[str, str]) -> dict[str, int]
    # LOG_LEVEL_<COMPONENT>=<LEVEL> -> {"<component lower-case>": logging level int}; bare "LOG_LEVEL" ignored;
    # invalid level -> ValueError naming the variable. (LOG_LEVEL_VA_MEDIA=DEBUG -> logger "va_media".)
def configure_logging(settings: BaseServiceSettings, *, version: str = "unknown", pod: str | None = None,
                      stream: TextIO | None = None, environ: Mapping[str, str] | None = None) -> None
    # stdlib-backed structlog so uvicorn/httpx/temporal stdlib logs share the pipeline:
    # shared chain = [structlog.contextvars.merge_contextvars, structlog.stdlib.add_log_level (key "level"),
    #   structlog.stdlib.add_logger_name, TimeStamper(fmt="iso", utc=True, key="ts"), _static_fields(service=
    #   settings.service_name, env=settings.va_env.value, version, pod), StackInfoRenderer(),
    #   otel_trace_processor, RedactProcessor(RedactionPolicy.from_settings(settings))]
    # JSON: + format_exc_info, JSONRenderer(serializer=_serialize) where _serialize = orjson.dumps(..., default=str).decode()
    #   (lenient: logging must never raise on an odd value — do NOT use va_common.json which rejects naive datetimes)
    # CONSOLE: ConsoleRenderer(colors=False) and NO format_exc_info (structlog warns otherwise; pytest filterwarnings=error)
    # structlog.configure(processors=[*shared, ProcessorFormatter.wrap_for_formatter], logger_factory=LoggerFactory(),
    #   wrapper_class=structlog.stdlib.BoundLogger, cache_logger_on_first_use=True)
    # Root logger: remove the handler a previous configure_logging call installed (idempotent; marker attribute),
    #   add StreamHandler(stream or sys.stdout) with ProcessorFormatter(foreign_pre_chain=shared, processors=
    #   [ProcessorFormatter.remove_processors_meta, renderer]); root level = settings.log_level; apply
    #   component_levels(environ if not None else os.environ); logging.getLogger("uvicorn.access").disabled = True
    #   (spec §1 access logs come from middleware). pod default: os.environ.get("POD_ID") or socket.gethostname().
def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger
def bind_call_context(*, org_id: UUID | str, call_id: UUID | str, turn_index: int | None = None) -> None
    # structlog.contextvars.bind_contextvars with str() ids; turn_index only if not None
def bind_turn(turn_index: int) -> None
def bound_context(**values: Any) -> AbstractContextManager[None]   # wraps structlog.contextvars.bound_contextvars
def clear_context() -> None                                         # clear_contextvars
```
Keep every function radon CC ≤ B (split `redact` into `_redact_value` / `_redact_item` helpers).

### Tests
`packages/va_common/tests/conftest.py`: add `"POD_ID"` to cleared vars; add autouse fixture that after each test calls
`structlog.reset_defaults()`, `structlog.contextvars.clear_contextvars()` and removes the handler installed by
`configure_logging` from the root logger (restore root level) so tests do not leak config into each other.
Helper in tests: `configure(settings, stream=io.StringIO(), environ={})` then `json.loads` each line.
- `test_masking.py`: mask_phone examples (+14155550123 → +1415***0123; +442071838750 → +4420***8750; 8-digit → +12***78;
  non-E.164 "4155550123", "+0123…", "+1-415…" unchanged); mask_phones_in_text in prose with two numbers, number in
  URL query, digit runs longer than 15 untouched, `+` preceded by digit untouched; hypothesis
  `st.from_regex(r"\+[1-9]\d{10,14}", fullmatch=True)`: masked keeps first 5/last 4 chars, contains "***", and the
  middle digits never appear; is_secret_key parametrized true/false lists above (case-insensitive, `-` separators).
- `test_logging.py`:
  - JSON line has ts (ISO, UTC "Z"/+00:00), level, event, logger, service (subclass `service_name="api"`), env,
    version, pod (explicit and POD_ID env fallback, and hostname fallback).
  - bind_call_context / bind_turn fields appear; bound_context removes them on exit; clear_context; UUID ids rendered as str.
  - phone masked in event message, top-level field and nested dict/list; NOT masked when LOG_LEVEL=DEBUG and
    VA_ENV=local; still masked when DEBUG in staging.
  - secrets: authorization, api_key, xi-api-key, nested `headers={"Authorization": ...}` → "[REDACTED]";
    api_key_prefix and prompt_tokens kept.
  - audio/payload keys dropped; bytes value rendered as "<N bytes>".
  - transcripts: `text` kept in local with LOG_TRANSCRIPTS=true; dropped when LOG_TRANSCRIPTS=false; dropped in
    production even with LOG_TRANSCRIPTS=true (transcripts_allowed False); dropped when `hipaa_mode=True` bound.
  - redact() pure: input mapping not mutated; depth cap.
  - otel: inside `trace.use_span(NonRecordingSpan(SpanContext(trace_id=..., span_id=..., is_remote=False)))`
    trace_id/span_id rendered as 32/16 hex; absent with no span.
  - component_levels: LOG_LEVEL_VA_MEDIA=debug → {"va_media": 10}; ignores LOG_LEVEL and unrelated vars; invalid → ValueError;
    configure_logging applies it (a `va_media` stdlib logger emits DEBUG while root is INFO; root INFO filters debug).
  - foreign stdlib logger (`logging.getLogger("thirdparty").warning("x +14155550123")`) rendered as JSON and masked.
  - exception: `log.exception` in JSON includes `exception` traceback text; console format renders a non-JSON line
    without warnings.
  - configure_logging twice → exactly one handler (no duplicate lines); uvicorn.access disabled.
Coverage 100 % on masking.py/logging.py.

### Docs
- specs/27-observability.md §1: `> Decision (2026-10-25):` — secret-key rule (segment match + exempt suffixes, value
  replaced by `[REDACTED]` rather than dropped so the key's presence stays visible; raw-media keys and gated transcript
  keys dropped); phone mask shape for 7–10-digit numbers; transcripts allowed iff LOG_TRANSCRIPTS and VA_ENV ≠
  production (and never with bound `hipaa_mode`); `LOG_LEVEL_<COMPONENT>` names a stdlib/structlog logger (lower-cased
  suffix); `version` passed by the service (default "unknown"), `pod` = POD_ID or hostname; logs stdlib-backed.
- AGENTS.md learned bullet: use `va_common.logging.get_logger`; tests reset structlog via conftest; console renderer
  omits format_exc_info.

### Gates
`make fmt lint typecheck placeholders test` + `make check`; pip-audit on `uv export --all-packages --no-emit-workspace`,
`corepack pnpm audit`, `uvx radon cc -s -a scripts packages`, `npx --yes jscpd@4`. build.done as plain string.

### Risks
- `cache_logger_on_first_use=True` + test reconfiguration: always call get_logger after configure in tests.
- mypy strict with structlog types (`EventDict` = MutableMapping[str, Any]; processor typing via `structlog.typing.Processor`).
- module named `logging.py` inside va_common: use `import logging` (absolute → stdlib) — fine as with json.py.
- ProcessorFormatter foreign records: `event` comes from record message; ensure redaction runs in foreign_pre_chain.

## Build P00-T06 — done (Builder)

Added `va_common.masking` (mask_phone, mask_phones_in_text, is_secret_key, REDACTED) and `va_common.logging`
(RedactionPolicy, transcripts_allowed, redact, RedactProcessor, otel_trace_processor, component_levels,
configure_logging, get_logger, bind_call_context, bind_turn, bound_context, clear_context) as planned; deps structlog
26.1 + opentelemetry-api 1.45 (uv.lock updated). conftest: POD_ID cleared + `reset_logging` autouse fixture.
Implementation notes beyond the plan:
- `structlog.stdlib.filter_by_level` is first in the structlog chain only (not foreign_pre_chain, where logger is None).
- In JSON format `format_exc_info` runs in the shared chain BEFORE redaction, so phone numbers in tracebacks are masked.
- Reconfiguring resets component loggers set by the previous call to NOTSET (module-level set).
- Root handler identified by a private `_VaHandler` subclass instead of a marker attribute.
- `pod` falls back through the `environ` argument (os.environ when None) then hostname.
Docs: specs/27 §1 `> Decision (2026-10-25)`; AGENTS.md bullet.
Gates: fmt 0; lint 0; typecheck 0 (28 files); placeholders 0 (0 findings); test 0 (471 passed, total 99 %,
logging.py/masking.py 100 %); make check 0; pip-audit + pnpm audit no known vulnerabilities; radon avg A (2.71),
new code max B (9, _redact_value); jscpd 0 clones. No TypeScript changed.

## Review P00-T06

Re-ran gates: make lint 0, typecheck 0 (28 files), placeholders 0 (0 findings), test 0 (471 passed, 99.92 %,
logging.py/masking.py 100 %). Pipeline, binding helpers, transcript/hipaa gating, phone masking, component levels,
handler idempotency and the specs/27 §1 Decision all check out. Rejected for 1 finding:

1. `packages/va_common/src/va_common/masking.py:57-68` (`is_secret_key`) — camelCase and concatenated keys are not
   recognised as secrets because segmentation only splits on `_ - . whitespace`. Verified: `apiKey`, `accessToken`,
   `clientSecret`, `secretKey`, `webhookSecret`, `apikey` all return False, so their values are logged in clear.
   The plan/phase-00.md P00-T06 deliverable requires redaction of keys matching `*key*`, `*secret*`, `*token*`
   (substring), and MASTER_PROMPT §4.6 forbids secrets in logs; JSON from SDKs/vendors/dashboard commonly uses
   camelCase. Required: also split camelCase boundaries (e.g. `re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)` before
   lower-casing) AND treat a segment as secret when it *contains* one of the secret words as a suffix/word
   (`apikey`, `secretkey`, `accesstoken` -> True) while keeping the exempt-last-segment rule and the plural-`tokens`
   exception (`prompt_tokens`, `completion_tokens`, `api_key_prefix`, `key_id`, `turn_index`, `monkey` stay False —
   pick a rule that keeps `monkey`/`turkey` False, e.g. segment ends with a secret word and either equals it or the
   prefix is in a small set like `api`, `access`, `refresh`, `client`, `secret`, `private`, `session`, `auth`,
   `webhook`, `x`, `xi`, or just check `segment.endswith(("key","secret","token","password"))` and exempt a short
   allowlist). Add parametrized tests for `apiKey`, `accessToken`, `clientSecret`, `secretKey`, `webhookSecret`,
   `apikey` -> True and `promptTokens`, `apiKeyPrefix`, `keyId`, `monkey` -> False, plus one log-output test with a
   camelCase secret nested in a dict. Update the specs/27 §1 Decision text describing the rule.

## Build P00-T06 (rework after review) — done (Builder)

`is_secret_key` now splits camelCase/acronym boundaries (`apiKey`, `APIKey`, `XApiKey`) before lower-casing, and a
segment is secret when it is a secret word, ends with `secret`/`password`/`passwd`/`credential(s)` (`dbpassword`), or
is `key`/`token` glued to a qualifier in {api, access, refresh, client, secret, private, session, auth, bearer,
webhook, signing, id, x, xi} (`apikey`, `accesstoken`, `refreshtoken`); exempt-last-segment rule unchanged, so
`apiKeyPrefix`, `keyId`, `promptTokens`, `monkey`, `turkey`, `hotkey`, `tokenizer` stay False. Tests: parametrized
true/false additions + `test_camel_case_secrets_redacted_in_nested_payload` (log output). specs/27 §1 Decision updated.
Gates: fmt 0, lint 0, typecheck 0, placeholders 0 (0 findings), test 0 (490 passed, 99 %, masking.py 100 %),
make check 0; pip-audit + pnpm audit no known vulnerabilities; radon avg A (2.72), new max B (6); jscpd 0 clones.

## Review P00-T06 (rework) — approved

Re-ran: lint 0, typecheck 0, placeholders 0, test 0 (490 passed, 99.92 %, logging.py/masking.py 100 %). Probed
`is_secret_key` on ~40 keys: apiKey, accessToken, clientSecret, secretKey, webhookSecret, apikey, APIKey, XApiKey,
idToken, secretAccessKey -> True; promptTokens, apiKeyPrefix, keyId, monkey, turkey, hotkey, tokenizer, tokenCount
-> False. Tests and specs/27 §1 Decision cover the rule. Ticked P00-T06 and committed.

## Plan P00-T07 — `va_common.crypto` envelope encryption

Spec refs: specs/28-security.md §3 (algorithm, blob format, data keys + cache, KEK providers, API, rotation),
specs/28 §2 (LOCAL_MASTER_KEY local/test only), specs/02 §6 rows `KMS_PROVIDER`, `LOCAL_MASTER_KEY`, `AWS_KMS_KEY_ID`,
specs/03 `org_data_keys`. Blob format: specs/28 §3 binary layout is normative — the plan/phase-00.md shorthand
`v1|key_id|wrapped_dek|nonce|ciphertext` is the same fields; record that in the Decision.
Out of scope: DB-backed `org_data_keys` store + org-creation hook (phase 01), `ReencryptWorkflow` + admin rotate
endpoint (later phases; they call `Envelope.rewrap`), S3 stream-chunked recording encryption (recordings phase).

### Dependencies
`uv add --package va-common cryptography aioboto3`; dev group: `uv add --dev "types-aioboto3[kms]"` (so mypy strict
types `aioboto3.Session().client("kms")`; if the stubs fight, add a `[[tool.mypy.overrides]] module=["aioboto3.*"]
ignore_missing_imports=true` — only for aioboto3). Package description += "crypto". `uv lock`.

### Package `packages/va_common/src/va_common/crypto/` (all mypy strict, every function radon CC ≤ B)
`__init__.py` re-exports the public names below (`__all__`).

`blob.py`
```python
BLOB_VERSION: Final = 0x01; NONCE_SIZE: Final = 12; TAG_SIZE: Final = 16; DATA_KEY_SIZE: Final = 32
class CryptoError(Exception)                 # base: misconfiguration, missing active key
class DecryptionError(CryptoError)           # malformed blob, wrong aad/org/key, tampering — message never includes data
@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    kek_id: str; encrypted_data_key: bytes; nonce: bytes; ciphertext: bytes   # ciphertext includes the 16-byte GCM tag
    def to_bytes(self) -> bytes
        # 0x01 | len(kek_id utf8) (1 byte, 1..255 else CryptoError) | kek_id | len(edk) (2 bytes BE, 1..65535) | edk
        # | nonce(12) | ciphertext+tag
    @classmethod
    def from_bytes(cls, data: bytes) -> EncryptedBlob
        # DecryptionError on: empty, version != 0x01, kek_id_len 0, truncated anywhere, non-utf8 kek_id, edk_len 0,
        # ciphertext shorter than TAG_SIZE. Use memoryview/offset helper `_take(data, offset, n)` to keep CC low.
def column_aad(table: str, column: str, row_id: UUID | str) -> str      # f"{table}.{column}:{row_id}"
```

`kms.py`
```python
NIL_ORG_ID: Final = UUID(int=0)              # platform-level secrets (platform_encrypt)
LOCAL_KEK_ID: Final = "local:v1"
@dataclass(frozen=True, slots=True)
class WrappedDataKey: kek_id: str; encrypted_data_key: bytes
@dataclass(frozen=True, slots=True)
class DataKey:
    kek_id: str; plaintext: bytes = field(repr=False); encrypted_data_key: bytes
    @property wrapped -> WrappedDataKey
class KmsProvider(Protocol):
    async def generate_data_key(self, org_id: UUID) -> DataKey
    async def decrypt_data_key(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes
class LocalKmsProvider:
    def __init__(self, master_key: bytes, *, kek_id: str = LOCAL_KEK_ID) -> None   # len != 32 -> CryptoError
    @classmethod from_hex(cls, value: str) -> LocalKmsProvider                      # 64 hex chars else CryptoError (no value in msg)
    generate_data_key: dk = os.urandom(32); nonce = os.urandom(12);
        encrypted = nonce + AESGCM(kek).encrypt(nonce, dk, _org_context(org_id))   # _org_context = f"org_id={org_id}".encode()
        (org binding mirrors the AWS encryption context)
    decrypt_data_key: kek_id mismatch -> DecryptionError; len < 12+16 -> DecryptionError; InvalidTag -> DecryptionError;
        result length != 32 -> DecryptionError
class KmsClient(Protocol):   # subset of the aiobotocore KMS client, boto3 keyword signatures
    async def generate_data_key(self, *, KeyId: str, KeySpec: str, EncryptionContext: Mapping[str, str]) -> Mapping[str, Any]
    async def decrypt(self, *, CiphertextBlob: bytes, KeyId: str, EncryptionContext: Mapping[str, str]) -> Mapping[str, Any]
class AwsKmsProvider:
    def __init__(self, client: KmsClient, key_id: str) -> None                     # empty key_id -> CryptoError
    generate_data_key: resp = await client.generate_data_key(KeyId=key_id, KeySpec="AES_256",
        EncryptionContext={"org_id": str(org_id)}); DataKey(kek_id=resp["KeyId"] (ARN), plaintext=resp["Plaintext"],
        encrypted_data_key=resp["CiphertextBlob"]); plaintext len != 32 -> CryptoError
    decrypt_data_key: client.decrypt(CiphertextBlob=..., KeyId=wrapped.kek_id, EncryptionContext={"org_id": ...});
        botocore.exceptions.ClientError with Error.Code in {"InvalidCiphertextException","IncorrectKeyException"}
        -> DecryptionError (from None); other ClientErrors propagate; plaintext len != 32 -> DecryptionError
@asynccontextmanager
async def aws_kms_provider(key_id: str, *, region_name: str | None = None,
                           session: aioboto3.Session | None = None) -> AsyncIterator[AwsKmsProvider]
    # session.client("kms", region_name=region_name, config=botocore.config.Config(connect_timeout=5, read_timeout=10,
    #   retries={"max_attempts": 3, "mode": "standard"})) — every network call has a timeout (MASTER_PROMPT §4.7)
@asynccontextmanager
async def open_kms_provider(settings: CryptoSettings) -> AsyncIterator[KmsProvider]
    # local -> LocalKmsProvider.from_hex(settings.local_master_key.get_secret_value()); aws -> aws_kms_provider(...)
```

`cache.py`
```python
class DataKeyCache:   # LRU + TTL for unwrapped data keys (specs/28 §3: 10 000 entries, 10 min)
    def __init__(self, *, max_entries: int = 10_000, ttl_seconds: float = 600.0, clock: Clock | None = None)  # SystemClock default; uses clock.monotonic()
    def get(self, org_id: UUID, wrapped: WrappedDataKey) -> bytes | None   # expired -> evicted, None; hit -> move_to_end
    def put(self, org_id: UUID, wrapped: WrappedDataKey, plaintext: bytes) -> None   # evict oldest beyond max_entries
    def clear(self) -> None; def __len__(self) -> int
    # key = (org_id, kek_id, encrypted_data_key); OrderedDict
```

`envelope.py`
```python
class DataKeyStore(Protocol):            # phase 01 implements it over org_data_keys (status='active')
    async def active_data_key(self, org_id: UUID) -> WrappedDataKey | None
class Envelope:
    def __init__(self, provider: KmsProvider, store: DataKeyStore, *, cache: DataKeyCache | None = None) -> None
    async def create_data_key(self, org_id: UUID) -> WrappedDataKey     # generate via provider, cache plaintext, return wrapped for the caller to persist
    async def encrypt(self, org_id: UUID, plaintext: bytes, aad: str) -> bytes
        # active key from store (None -> CryptoError "no active data key"); unwrap (cache, else provider + cache.put);
        # nonce = os.urandom(12); AESGCM(dk).encrypt(nonce, plaintext, aad.encode()); EncryptedBlob(...).to_bytes()
    async def decrypt(self, org_id: UUID, blob: bytes, aad: str) -> bytes
        # EncryptedBlob.from_bytes; unwrap the EMBEDDED key (never a store lookup); InvalidTag -> DecryptionError
    async def encrypt_str(...)-> bytes / decrypt_str(...) -> str      # utf-8; invalid utf-8 after decrypt -> DecryptionError
    async def platform_encrypt(self, plaintext: bytes, aad: str) -> bytes / platform_decrypt(...)   # org_id = NIL_ORG_ID
    async def rewrap(self, org_id: UUID, blob: bytes, aad: str) -> bytes
        # key-rotation helper for ReencryptWorkflow: decrypt with the embedded key; if the embedded
        # (kek_id, encrypted_data_key) already equals the active key return blob unchanged; else encrypt under active key
```
`va_common/settings.py`: add
```python
class KmsProviderName(StrEnum): LOCAL = "local"; AWS = "aws"
class CryptoSettings(BaseServiceSettings):   # api, worker, media Settings extend it (specs/02 §6 scope "api, worker, media")
    kms_provider: KmsProviderName = KmsProviderName.LOCAL
    local_master_key: SecretStr = SecretStr("")
    aws_kms_key_id: str = ""
    model_validator(after): local + production -> ValueError ("KMS_PROVIDER=local is refused when VA_ENV=production");
        local requires local_master_key to be 64 hex chars (ValueError naming LOCAL_MASTER_KEY, never the value);
        aws requires non-empty aws_kms_key_id
```
Export `CryptoSettings`, `KmsProviderName` in settings `__all__`.

### Tests (`packages/va_common/tests/`)
Test-only helpers in `tests/crypto_fakes.py` (NOT in src): `InMemoryDataKeyStore` (dict org_id -> WrappedDataKey,
`activate(org_id, wrapped)`) and `FakeKmsClient` implementing the boto3 keyword signatures with an in-memory AESGCM
master key; `KeyId` returned as an ARN `arn:aws:kms:us-east-1:111122223333:key/<uuid>`; raises
`botocore.exceptions.ClientError({"Error": {"Code": "InvalidCiphertextException", ...}}, "Decrypt")` when the encryption
context or ciphertext does not match; records calls (to assert KeyId/KeySpec/EncryptionContext). Fixture constants are
generated with `os.urandom`/`secrets` at test time, never hard-coded key material.
- `test_crypto_blob.py`: to_bytes/from_bytes round trip with exact byte layout asserted (version byte, kek len, BE
  edk len); hypothesis round trip over kek_id (1..255 utf-8 bytes) / edk / ciphertext; every malformed case above ->
  DecryptionError (parametrized); kek_id > 255 bytes and edk > 65535 -> CryptoError on encode; column_aad format.
- `test_crypto_kms.py`: Local: wrap/unwrap round trip; wrong org -> DecryptionError; wrong master key; tampered
  wrapped key; foreign kek_id; bad master key length / non-hex / wrong-length hex -> CryptoError; DataKey repr
  hides plaintext. AWS (FakeKmsClient): generate passes KeyId, KeySpec="AES_256", EncryptionContext {"org_id": str};
  kek_id = returned ARN; decrypt passes KeyId=wrapped.kek_id and same context; InvalidCiphertextException ->
  DecryptionError; other ClientError (e.g. "AccessDeniedException") propagates; bad plaintext length paths; empty
  key_id -> CryptoError. `aws_kms_provider` with a fake session object whose `client()` returns an async context manager
  yielding FakeKmsClient — assert service name "kms", region and Config timeouts/retries. `open_kms_provider` for
  local and aws settings.
- `test_crypto_cache.py`: hit/miss, TTL expiry with `va_common.time.FrozenClock.advance` (no sleeps), LRU eviction order at
  max_entries, get refreshes recency, clear/len, keys distinct per org.
- `test_crypto_envelope.py` (both providers via parametrized fixture): encrypt/decrypt round trip incl. empty
  plaintext and hypothesis bytes; blob parses and embeds active kek_id/edk; two encryptions differ (random nonce);
  wrong aad (row swap: `column_aad("tools","secret_headers_enc",row_a)` vs row_b) -> DecryptionError; wrong org ->
  DecryptionError; flipped byte in ciphertext / tag / nonce / edk -> DecryptionError; truncated blob -> DecryptionError;
  no active key -> CryptoError; decrypt after rotation (old blob still decrypts via embedded key while store points to
  new key); rewrap moves to the new key and still decrypts, unchanged when already current (identity bytes);
  encrypt_str/decrypt_str unicode; invalid utf-8 -> DecryptionError; platform_encrypt/decrypt use NIL org (blob not
  decryptable as another org); cache: second decrypt does not call the provider (counting wrapper), create_data_key
  primes the cache.
- `test_settings.py` additions: CryptoSettings defaults require LOCAL_MASTER_KEY (missing -> ValidationError naming the
  var, not leaking value); valid local; production+local refused; aws without key id refused; aws valid; conftest
  clears `KMS_PROVIDER`, `LOCAL_MASTER_KEY`, `AWS_KMS_KEY_ID` if not already.
Coverage 100 % on the crypto package; async tests via pytest-asyncio (asyncio_mode = "auto" already set).

### Docs
- specs/28 §3 `> Decision (2026-10-25):` — (a) API is async (`await envelope.encrypt(...)`) because AWS KMS unwrap is
  network I/O; it lives on an `Envelope(provider, store, cache)` object; (b) binary blob layout above is the plan's
  `v1|key_id|wrapped_dek|nonce|ciphertext`; ciphertext carries the GCM tag at its end; (c) local wrapped key =
  `nonce(12) | AES-256-GCM(KEK, data key, aad="org_id=<uuid>") + tag`, mirroring the AWS encryption context;
  (d) `DataKeyStore.active_data_key` protocol, implemented over `org_data_keys` in phase 01; decryption uses only the
  embedded wrapped key; cache keyed by (org_id, kek_id, encrypted_data_key); (e) `rewrap` is the ReencryptWorkflow
  primitive and is a no-op on blobs already under the active key; (f) failures raise `DecryptionError`
  (never carrying key/plaintext material), surfaced by the API as INTERNAL_ERROR; (g) local-in-production refusal and
  key-format checks enforced by `CryptoSettings` validation at startup; AWS client timeouts connect 5 s / read 10 s,
  3 standard retries.
- AGENTS.md learned bullet: services that store tenant secrets extend `CryptoSettings` and use
  `open_kms_provider` + `Envelope`; test fakes live in `packages/va_common/tests/crypto_fakes.py`.

### Gates
`make fmt lint typecheck placeholders test` + `make check`; pip-audit on `uv export --all-packages
--no-emit-workspace`, `corepack pnpm audit`, `uvx radon cc -s -a scripts packages`, `npx --yes jscpd@4`.
build.done as plain string (memory mem-1792920384-602e).

### Risks
- Placeholder checker / secret-scan false positives on test key material — generate keys at runtime.
- `types-aioboto3` stub version must match aioboto3; aiobotocore pins botocore — check pip-audit after lock.
- Logging redaction: never log DataKey or plaintext; `field(repr=False)` + DecryptionError messages are generic.
- mypy on `Mapping[str, Any]` response indexing: validate types (`isinstance(..., bytes)`) and raise CryptoError otherwise
  (also gives coverage targets; keep helpers small for CC).

## Build P00-T07 — done (Builder)

`va_common.crypto` package (blob, kms, cache, envelope, `__init__` re-exports) + `CryptoSettings`/`KmsProviderName`
in settings, per plan. Deviations: AWS client config is `aiobotocore.config.AioConfig` (stubs reject botocore
`Config`); boto3-named params carry `# noqa: N803`; `DataKeyCache` rejects `max_entries < 1` / `ttl_seconds <= 0`;
`EncryptedBlob.to_bytes` also rejects nonce != 12 bytes / ciphertext shorter than the tag. Test fakes in
`packages/va_common/tests/crypto_fakes.py` (`InMemoryDataKeyStore`, `FakeKmsClient`, `CountingProvider`); the tests
dir was added to pytest `pythonpath` and mypy `mypy_path` so tests can import it. Deps: cryptography, aioboto3
(va-common); types-aioboto3[kms] (dev). Docs: specs/28 §3 Decision (a)-(g), AGENTS.md bullets, package description.
Gates: fmt 0, lint 0, typecheck 0 (38 files), placeholders 0 findings, test 0 (614 passed, 99.94 %, crypto/* and
settings.py 100 %), make check 0; pip-audit + pnpm audit no known vulnerabilities; radon avg A (2.57), all crypto
functions A; jscpd 0 clones.

## Review P00-T07 — approved

Re-ran: lint 0, typecheck 0 (38 files), placeholders 0, test 0 (614 passed, 99.94 %, crypto/* 100 %). Checked blob
layout byte-for-byte against specs/28 §3, local wrap org binding, AWS encryption context/KeyId/KeySpec, ClientError
mapping (decrypt errors -> DecryptionError, others propagate), cache key includes org_id (wrong-org cannot hit the
cache), rewrap identity on current key, CryptoSettings production refusal, env vars match specs/02 §6. No key literals
in tests; no sleeps. Note for later: `open_kms_provider` relies on the AWS default region chain (no region setting in
specs/02 §6). Ticked P00-T07 and committed.

## Plan P00-T08 — `va_common` infrastructure helpers (redis, http, tracing, metrics, observability init)

Spec refs: specs/02 §2 (core deps: `redis>=5.1`, `httpx[http2]`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`,
`opentelemetry-instrumentation-httpx`, `prometheus-client`, `sentry-sdk`), specs/02 §4 (Container holds clients),
specs/02 §6 (`REDIS_URL` scope "all backend", `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_SAMPLER_ARG`, `SENTRY_DSN`,
`POD_ID`), specs/27 §2 (TracerProvider/Resource/sampler/force-sample/propagation), specs/27 §3 (metric tables, `LAT_MS`,
per-service `/metrics`), specs/27 §8 (Sentry), specs/27 header (`va_common.observability.init(service_name, settings)`),
MASTER_PROMPT §4.7 (every network call has a timeout).
Out of scope: FastAPI/asyncpg instrumentation and request metrics middleware (P00-T11 / phase 01), `/healthz` `/readyz`
endpoints (P00-T11/T12 — they will use `redis_ping` from here), Temporal TracingInterceptor (worker phase).
Docker is NOT available on this machine right now: the testcontainers test must skip cleanly, coverage comes from unit tests.

### Dependencies
`uv add --package va-common "redis>=5.1" "httpx[http2]" opentelemetry-sdk opentelemetry-exporter-otlp
opentelemetry-instrumentation-httpx opentelemetry-instrumentation-redis prometheus-client sentry-sdk`;
dev: `uv add --dev fakeredis` (async fake for unit tests). Package description += "redis, http, tracing, metrics".
`uv lock`; pip-audit afterwards (grpcio comes with the OTLP gRPC exporter).

### `settings.py`
```python
class RedisSettings(BaseServiceSettings):      # services with REDIS_URL ("all backend" scope) extend it
    redis_url: str = "redis://localhost:6379/0"
```
Add to `__all__`. (Settings classes compose by multiple inheritance later: `class Settings(CryptoSettings, RedisSettings)`.)
conftest `SETTINGS_ENV_VARS` += `REDIS_URL`.

### `va_common/redis.py`
```python
REDIS_SOCKET_TIMEOUT_S: Final = 5.0; REDIS_CONNECT_TIMEOUT_S: Final = 2.0; REDIS_HEALTH_CHECK_INTERVAL_S: Final = 30
def redis_client(settings: RedisSettings, *, url: str | None = None, decode_responses: bool = False,
                 max_connections: int = 50, tracer_provider: TracerProvider | None = None) -> redis.asyncio.Redis
    # Redis.from_url(url or settings.redis_url, socket_timeout, socket_connect_timeout, health_check_interval,
    #   retry_on_timeout=True, max_connections, decode_responses, client_name=settings.service_name)
    # empty url -> CryptoError? no: ValueError("REDIS_URL is empty"). `url=` override exists for SIM_STATE_REDIS_URL.
    # OTel: RedisInstrumentor().instrument_client(client, tracer_provider=...) when tracer_provider given
    #   (verify instrument_client exists in the installed opentelemetry-instrumentation-redis; if not, call
    #   RedisInstrumentor().instrument(tracer_provider=...) once from setup_tracing instead and record that).
async def redis_ping(client: Redis, *, timeout: float = 1.0) -> bool
    # asyncio.timeout(timeout): await client.ping(); False on TimeoutError / redis.exceptions.RedisError / OSError
    # (specs/27 §9 "all within 1 s"); never raises.
```

### `va_common/http.py`
```python
DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0
def http_client(settings: BaseServiceSettings, *, timeout: float | httpx.Timeout, http2: bool = False,
                base_url: str = "", headers: Mapping[str, str] | None = None, max_connections: int = 100,
                max_keepalive: int = 20, transport: httpx.AsyncBaseTransport | None = None,
                tracer_provider: TracerProvider | None = None, version: str = "unknown") -> httpx.AsyncClient
    # `timeout` is required keyword (no default — callers must choose); float -> httpx.Timeout(timeout,
    #   connect=min(timeout, DEFAULT_CONNECT_TIMEOUT_S)); timeout <= 0 -> ValueError.
    # headers: {"User-Agent": f"voiceagents-{settings.service_name}/{version}", **headers}; follow_redirects=False;
    # limits httpx.Limits(max_connections, max_keepalive_connections); http2 flag passed through (h2 installed).
    # HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracer_provider) always (global provider when None).
```

### `va_common/tracing.py`
```python
FORCE_SAMPLE_ATTRIBUTE: Final = "va.force_sample"
class ForceSampleSampler(Sampler):   # root sampler: attribute va.force_sample is True -> RECORD_AND_SAMPLE, else delegate
    def __init__(self, delegate: Sampler) -> None; should_sample(...); get_description() -> f"ForceSample{{{delegate}}}"
def build_sampler(ratio: float) -> Sampler   # ParentBased(root=ForceSampleSampler(TraceIdRatioBased(ratio)))
def build_resource(service_name: str, settings: BaseServiceSettings, *, version: str, pod: str) -> Resource
    # service.name, service.version, deployment.environment (= va_env value), k8s.pod.name
def resolve_pod(environ: Mapping[str, str] | None = None) -> str   # POD_ID or socket.gethostname(); refactor
    # logging.configure_logging to use it (one place for the rule)
def setup_tracing(service_name: str, settings: BaseServiceSettings, *, version: str = "unknown",
                  span_processor: SpanProcessor | None = None, set_global: bool = True,
                  environ: Mapping[str, str] | None = None) -> TracerProvider
    # span_processor default: BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint,
    #   insecure=endpoint.startswith("http://"), timeout=10)) — gRPC exporter (collector 4317).
    # set_global: trace.set_tracer_provider(provider) and propagate.set_global_textmap(CompositePropagator(
    #   [TraceContextTextMapPropagator(), W3CBaggagePropagator()])). Tests pass set_global=False (OTel global can be set once).
def force_sampled_attributes(**attrs: AttributeValue) -> dict[str, AttributeValue]  # {**attrs, "va.force_sample": True}
    # helper for the call root span (specs/27 §2 "calls are always sampled")
def shutdown_tracing(provider: TracerProvider) -> None   # force_flush + shutdown
```

### `va_common/metrics.py`
```python
LAT_MS: Final[tuple[float, ...]] = (5,10,25,50,75,100,150,200,300,400,500,650,800,1000,1300,1600,2000,3000,5000,10000)
class MetricKind(StrEnum): COUNTER="counter"; GAUGE="gauge"; HISTOGRAM="histogram"
class Service(StrEnum): API="api"; TELEPHONY_EDGE="telephony_edge"; MEDIA_WORKER="media_worker"; WORKER="worker";
    WEBHOOK_DISPATCHER="webhook_dispatcher"; EVENTS_SINK="events_sink"
@dataclass(frozen=True, slots=True) class MetricSpec: name: str; kind: MetricKind; labels: tuple[str, ...] = (); doc: str
CATALOG: Final[Mapping[Service, tuple[MetricSpec, ...]]]   # every row of specs/27 §3.1-§3.5, exact names/labels;
    # §3.5 split: va_webhook_* -> WEBHOOK_DISPATCHER, va_events_sink_* -> EVENTS_SINK. Combined rows
    # ("a / b") become two specs; "(ended only)" -> va_media_calls_started_total has no labels,
    # va_media_calls_ended_total has end_reason. All histograms use LAT_MS. Counter names keep "_total"
    # (prometheus_client strips/re-adds it; exposition name is identical).
def new_registry(*, process_collectors: bool = True) -> CollectorRegistry   # fresh registry (+Process/Platform/GC collectors)
class ServiceMetrics:
    def __init__(self, service: Service, registry: CollectorRegistry) -> None   # registers every CATALOG[service] metric
    registry: CollectorRegistry
    def counter(self, name: str) -> Counter; def gauge(self, name: str) -> Gauge; def histogram(self, name: str) -> Histogram
        # unknown name or wrong kind -> KeyError/TypeError with the name in the message
def build_metrics(service: Service, registry: CollectorRegistry | None = None) -> ServiceMetrics
def metrics_app(registry: CollectorRegistry) -> ASGIApp   # prometheus_client.make_asgi_app(registry); typed via a Protocol/alias
def render_latest(registry: CollectorRegistry) -> tuple[bytes, str]   # generate_latest + CONTENT_TYPE_LATEST (for FastAPI route)
```

### `va_common/observability.py`
```python
@dataclass(slots=True) class Observability: tracer_provider: TracerProvider; metrics: ServiceMetrics | None; sentry_enabled: bool
    def shutdown(self) -> None   # shutdown_tracing + sentry_sdk.flush(timeout=2) when enabled
def init(service_name: str, settings: BaseServiceSettings, *, version: str = "unknown", service: Service | None = None,
         span_processor=None, set_global=True, environ=None, stream=None) -> Observability
    # configure_logging(settings, version=..., stream/environ) + setup_tracing + build_metrics(service) when given
    # + init_sentry(settings, version)
def init_sentry(settings: BaseServiceSettings, *, version: str) -> bool
    # empty SENTRY_DSN -> False, no init. Else sentry_sdk.init(dsn, environment=va_env, release=version,
    #   traces_sample_rate=0.0, send_default_pii=False, integrations=[AsyncioIntegration(), HttpxIntegration()],
    #   before_send=sentry_before_send(policy)) -> True. FastAPI integration auto-enables when fastapi is installed.
def sentry_before_send(policy: RedactionPolicy) -> Callable[[Event, Hint], Event | None]
    # applies va_common.logging.redact to event["extra"], ["contexts"], ["request"] (headers/data), breadcrumbs[].data;
    # drops ["user"]["email"]/["ip_address"] (never user email, specs/27 §8)
```
If `observability.py` makes the task too large, Sentry stays in this file anyway (it is ~40 lines); keep functions CC ≤ B.

### Tests (`packages/va_common/tests/`)
- `test_redis.py`: client built from settings carries timeouts/health interval/retry/client_name (inspect
  `client.connection_pool.connection_kwargs`), `url=` override, empty url -> ValueError; `redis_ping` True against
  fakeredis (`fakeredis.FakeAsyncRedis`), False on ConnectionError (fake client raising), False on timeout (client whose
  ping awaits an `asyncio.Event` never set, timeout=0.01 — no sleeps); tracing instrumented client emits a span to
  `InMemorySpanExporter` for a fakeredis command (if instrument_client works with FakeAsyncRedis; otherwise assert
  the instrumentor was applied).
  `test_redis_integration.py` (`@pytest.mark.integration`): testcontainers `RedisContainer("redis:7.2-alpine")` →
  `redis_client(url=...)` set/get + `redis_ping` True; fixture skips (`pytest.skip("docker unavailable")`) when the Docker
  daemon cannot be reached (check via `docker.from_env().ping()` in try/except).
- `test_http.py` (respx): required timeout applied (connect capped at 5 s), zero/negative -> ValueError,
  User-Agent header, custom headers merge, base_url, no redirect following, http2 flag, limits; request through
  respx produces an httpx client span in `InMemorySpanExporter` with `http.request.method`/`http.method` attribute and
  a `traceparent` header on the outgoing request when inside a parent span.
- `test_tracing.py`: resource attributes (service.name/version, deployment.environment, k8s.pod.name from POD_ID and
  hostname fallback); sampler: ratio 0 drops a normal root span, keeps one with `force_sampled_attributes`, child of a
  sampled parent is sampled under ratio 0 (ParentBased); ratio 1 samples all; description string; default processor
  is BatchSpanProcessor with OTLP exporter endpoint from settings (inspect, no network: shut down immediately);
  `set_global=True` sets the global textmap to tracecontext+baggage (use a fixture that saves/restores
  `propagate.get_global_textmap()`; for the tracer provider global, patch `trace.set_tracer_provider` via
  monkeypatch and assert it was called with the provider); shutdown flushes in-memory exporter.
- `test_metrics.py`: parses every table under specs/27 §3.1–§3.5 (path resolved from repo root via
  `Path(__file__).parents[3] / "specs/27-observability.md"`, since conftest chdirs to tmp_path) — names (split " / "),
  kind, labels (backticked tokens, "—" none, "(X only)" scoping) — and asserts exact CATALOG parity both directions;
  each `build_metrics(service)` registers all metrics (exposition contains each name with `# TYPE`); histogram buckets
  == LAT_MS; accessor wrong kind/unknown name errors; two services get independent registries; `metrics_app` served
  via httpx `ASGITransport` returns 200 `text/plain; version=0.0.4` with observed values; `render_latest` content type.
- `test_observability.py`: `init` configures logging (StringIO stream, JSON line contains service), tracer provider
  with in-memory processor, metrics for given service, `sentry_enabled` False without DSN; with DSN
  (`https://public@example.invalid/1` built at runtime) `sentry_sdk.init` called with the asserted kwargs (monkeypatch
  `sentry_sdk.init` to capture — no network); `sentry_before_send` redacts `extra.api_key`, request header
  `authorization`, phone numbers in breadcrumbs, drops user email/ip; `shutdown` calls flush only when enabled.
Coverage 100 % on the new modules. No sleeps; `asyncio.timeout` with an Event for timeouts.

### Docs
- specs/27 §3 `> Decision (2026-10-25):` CATALOG lives in `va_common.metrics`, keyed by service; §3.5 split by prefix;
  each service uses its own `CollectorRegistry` (built by `build_metrics`), plus process/platform/GC collectors;
  `/metrics` via `metrics_app(registry)` (mounted on the same port for api/edge/media, dedicated port for worker types —
  P00-T12).
- specs/27 §2 `> Decision (2026-10-25):` force-sample = root sampler checks span attribute `va.force_sample=True`
  (`force_sampled_attributes`) under `ParentBased`; gRPC OTLP exporter (insecure for `http://` endpoints, 10 s timeout);
  `service.version` passed by the service; `k8s.pod.name` = `POD_ID` or hostname (same rule as logs); httpx and redis
  clients are instrumented per client by the factories; global provider/propagator set by `setup_tracing`.
- specs/27 §8 Decision: `observability.init` performs logging + tracing + metrics + Sentry; Sentry before_send reuses
  `redact()` and strips user email/ip.
- specs/02 §6 nothing new (no new env vars). AGENTS.md learned bullets: use `redis_client`/`http_client` factories (never
  raw `httpx.AsyncClient`), `http_client` requires `timeout=`; tests use `setup_tracing(..., span_processor=
  SimpleSpanProcessor(InMemorySpanExporter()), set_global=False)` and `build_metrics(service)` (fresh registry, never
  the prometheus global REGISTRY); integration tests skip without Docker.

### Gates
`make fmt lint typecheck placeholders test` + `make check`; `uvx pip-audit` on `uv export --all-packages
--no-emit-workspace`, `corepack pnpm audit`, `uvx radon cc -s -a scripts packages`, `npx --yes jscpd@4`. build.done
as a plain string (mem-1792920384-602e).

### Risks
- `filterwarnings = error`: OTel emits warnings on overriding the global tracer provider and on deprecated semconv;
  sentry/grpc may emit DeprecationWarnings on import — keep tests on `set_global=False`; add narrowly scoped
  `filterwarnings` entries only for third-party warnings that cannot be avoided, with a comment naming the source.
- prometheus_client `Counter("x_total")` registers as `x`; the exposition still shows `x_total` — test on exposition text.
- Missing stubs: `prometheus_client` and `fakeredis` ship types; OTel ships `py.typed`; if `sentry_sdk` typing of
  `Event`/`Hint` is awkward, use `sentry_sdk.types` (`Event`, `Hint`).
- xdist + random parametrize ban (mem-1792981166-ce01) — no random values in parametrize.
- Instrumenting the same httpx client twice / uninstrument leaks between tests: build a new client per test and close it.

## Build P00-T08 — done (Builder)

Implemented per plan: `RedisSettings` (+ conftest `REDIS_URL`), `va_common.redis` (`redis_client`, `redis_ping`),
`va_common.http` (`http_client` with required `timeout`, `build_timeout`, `user_agent`), `va_common.tracing`
(`ForceSampleSampler`, `build_sampler`, `build_resource`, `resolve_pod` — logging now uses it, `setup_tracing`,
`force_sampled_attributes`, `shutdown_tracing`), `va_common.metrics` (56-metric `CATALOG`, `ServiceMetrics`,
`build_metrics`, `new_registry`, `metrics_app`, `render_latest`), `va_common.observability` (`init`, `init_sentry`,
`sentry_before_send` which also tags bound org_id/call_id per specs/27 §8).
Deviations from plan, all deliberate:
- `redis_ping(client, *, timeout_s=1.0)`: ruff ASYNC109 forbids an async `timeout` parameter.
- `RedisInstrumentor().instrument_client` exists in 0.66b1, so it is used per client.
- prometheus_client 0.26 exposes `# TYPE x_total counter` (full name); tests assert on that.
- Integration test probes the Docker socket with a raw socket: `docker.from_env()` leaked a socket
  (ResourceWarning -> session error under filterwarnings=error). Docker absent here -> it skips.
Docs: specs/27 §2, §3, §8 Decision notes; AGENTS.md three bullets.
Gates: make check exit 0 (678 passed, 1 skipped; total coverage 99%, new modules 100%), pip-audit no known vulns,
pnpm audit clean, radon avg A (2.60), no C+ blocks, jscpd 0 clones.

## Review P00-T08 — rejected (1 finding)

Gates re-run by Reviewer: `make lint typecheck placeholders` exit 0; `make test` 678 passed, 1 skipped, coverage
99.95 %. Metrics CATALOG, sampler/resource/exporter, redis/http factories, AGENTS.md bullets all match specs/27.

1. `packages/va_common/src/va_common/observability.py:136-146` (`sentry_before_send`) — specs/27 §8 says
   `before_send` "applies the same redaction as logs", and §1 log redaction masks phone numbers in **any** string
   field (logs run `format_exc_info` before `redact_processor`, so exception text is masked in logs). The hook only
   redacts `extra`, `contexts`, `request`, and breadcrumbs. Verified leak (reviewer ran it):
   `{'message': 'dial +14155550123', 'logentry': {'message': 'dial %s', 'params': ['+14155550123']},
   'exception': {'values': [{'value': 'invalid number +14155550123'}]}}` comes back unchanged. Also
   `exception.values[].stacktrace.frames[].vars` (sentry-sdk captures local variables by default) and
   `threads.values[].stacktrace.frames[].vars` go out unredacted. Sentry's built-in EventScrubber drops some
   secret-named keys but does not mask phone numbers or transcripts.
   Fix: also mask `message`, `logentry.message`/`logentry.formatted`/`logentry.params`, `exception.values[].value`,
   and run `redact()` on each frame's `vars` (exception and threads stacktraces). Keep functions at CC <= B. The
   simplest safe option is to redact every top-level section except the structural keys Sentry needs (`event_id`,
   `timestamp`, `level`, `platform`, `sdk`, `modules`, ...), but make sure `exception.values[].type`/`module`/
   frame `filename`/`function`/`lineno` survive. Add tests for message, logentry params, exception value, and frame
   vars (`api_key` -> `[REDACTED]`, phone masked, `transcript` dropped). Update the specs/27 §8 Decision note to list
   the redacted fields.

## Build P00-T08 (rework after review) — done (Builder)

Finding 1 fixed: `sentry_before_send` now also redacts `logentry` (message/formatted/params via `redact`), masks
phones in `message` and every `exception.values[].value`, and runs `redact()` on each frame's `vars` in
`exception.values[].stacktrace.frames` and `threads.values[].stacktrace.frames` (api_key -> [REDACTED], phone masked,
transcript dropped). Exception `type`/`module` and frame `filename`/`function`/`lineno` untouched. Small helpers
(`_redact_message`, `_redact_frames`, `_redact_stack_entry`, `_stack_values`, `_redact_stacks`), all CC A.
4 new tests (message/logentry/exception value, frame vars in exception + threads, odd shapes, mask_phones=False).
specs/27 §8 Decision note lists the redacted fields.
Gates: make fmt/lint/typecheck/placeholders/test exit 0 (682 passed, 1 skipped, coverage 99.95 %, observability.py
100 %); make check exit 0; pip-audit + pnpm audit no vulns; radon avg A (2.62); jscpd 0 clones.

## Review P00-T08 (rework) — approved

Gates re-run: make lint/typecheck/placeholders exit 0; make test 682 passed, 1 skipped, coverage 99.95 %.
Re-ran the prior leak payload: message, logentry message/formatted/params, exception value, and frame vars
(exception + threads) are now masked/redacted; api_key/token -> [REDACTED], transcript dropped, user email/ip
removed; type/module/filename/function/lineno intact. specs/27 §8 Decision lists the fields.

## Plan P00-T09 — docker-compose infrastructure profile

Spec refs: specs/29 §2 (compose YAML is normative), specs/02 §2 (image pins), AGENTS.md Ports, specs/02 §6 / `.env.example`
(credentials, buckets, endpoints). Later tasks own the rest of the compose file: sim service = P03 (phase-03 telnyx
bootstrap task), Dockerfiles = P20-T01, apps/e2e profiles = P20-T02, dashboards + alert rules = P19-T02. So this task
writes ONLY infra + observability services; no `build:` entries, no `x-py-env` anchor (nothing uses it yet; P20-T02 adds it).

### Environment
Docker daemon is NOT running (OrbStack installed: `/usr/local/bin/orb`). Builder: try `orb start` (then
`docker info`) to get a daemon for the live `make up` evidence. `docker compose` CLI v5.5.0 is present, so
`docker compose config` works without a daemon. If the daemon cannot be started after a genuine attempt, the static
tests + `docker compose config --quiet` are the evidence and the scratchpad must say `make up` was not run (never claim it).

### Decisions (record in specs/29 §2 as `> Decision (2026-10-25):` under the YAML block)
1. `otel-collector` runs in profiles `[infra, apps, e2e, observability]`: every service exports OTLP to 4317
   (`.env.example` OTEL_EXPORTER_OTLP_ENDPOINT, MASTER_PROMPT §9 lists the collector under `make up`). prometheus,
   grafana, jaeger stay `[observability]` only. The collector exports traces to jaeger only when that profile runs:
   collector config exports traces via `otlp/jaeger` exporter (`jaeger:4317`, tls insecure) — NO: an unreachable
   exporter just logs retries; to keep infra-only logs clean use the `debug` exporter (verbosity basic) for traces
   and a `prometheus` exporter on `0.0.0.0:8889` for metrics, plus `otlp/jaeger`. Choose: traces -> [otlp/jaeger],
   with `sending_queue`/`retry_on_failure` defaults (failures only warn). Metrics -> [prometheus] (:8889, scraped by
   Prometheus). Extensions: `health_check` on `0.0.0.0:13133`. Receivers: otlp grpc `0.0.0.0:4317`, http `0.0.0.0:4318`.
   Processors: `memory_limiter` (check_interval 1s, limit_percentage 80, spike_limit_percentage 20) then `batch`.
2. `make up` with no PROFILE must start the `infra` profile (today every service has a profile, so the current Makefile
   starts nothing). Makefile: `PROFILE ?= infra`, `up: scripts/dev_up.sh $(PROFILE)`, `down: scripts/dev_down.sh`.
3. `make down` stops all profiles: `docker compose --profile '*' down --remove-orphans` (keeps volumes; `dev_down.sh -v`
   / `--volumes` also removes named volumes).
4. Prometheus alert rules file mount is deferred to P19-T02 (spec mounts `infra/grafana/alerts.yaml`, plan P19 names
   `infra/prometheus/alerts.yml`; P19 resolves which). Mounting a missing file makes Docker create a directory, so
   do not mount it now.
5. Healthchecks added beyond spec (plan requires them): mailpit (`["CMD", "/mailpit", "readyz"]`), temporal-ui
   (`["CMD-SHELL", "wget -qO- http://localhost:8080/ >/dev/null || exit 1"]` — verify wget exists in image; if not,
   omit and note), prometheus (`wget -qO- http://localhost:9090/-/ready`), grafana (`wget -qO- http://localhost:3000/api/health`),
   jaeger (`wget -qO- http://localhost:14269/` admin port; verify), otel-collector: the contrib image is distroless
   (no shell/wget) — no container healthcheck; readiness is the `health_check` extension on 13133 published as
   `13133:13133` so dev_up.sh can curl it. Document which services lack a healthcheck and why.
6. Temporal healthcheck: spec uses `tctl`; `temporalio/auto-setup:1.25` ships both `tctl` and `temporal`. Keep spec form
   if it works on a live daemon; otherwise use `["CMD", "temporal", "operator", "cluster", "health", "--address",
   "temporal:7233"]` and record a Decision. Add `start_period: 30s` (schema setup is slow).
7. postgres-init `01-roles.sql` per spec text after the YAML: roles `va_app`, `va_system` (LOGIN PASSWORD 'va') and
   database `temporal`. Risk: auto-setup also creates `temporal` + `temporal_visibility`; verify auto-setup tolerates
   an existing `temporal` DB (its create step ignores "already exists" in 1.25 — confirm on live run). If it fails,
   drop the CREATE DATABASE and record a Decision. Make role creation idempotent with `DO $$ ... IF NOT EXISTS ... $$`.
   File lives at `infra/docker/postgres-init/01-roles.sql` (placeholder gate scans infra/; keep it clean).
8. `minio-init` is one-shot. `docker compose up --wait` can fail on an exited container on some compose versions.
   Test on the live daemon; if `--wait` errors on `minio-init exited (0)`, `dev_up.sh` runs
   `docker compose --profile P up -d --wait --wait-timeout 180 <long-running services>` and then
   `docker compose --profile P run --rm minio-init`. Make `mc mb` idempotent: `mc mb --ignore-existing`.
   Add `restart: "no"`.

### Files
- `docker-compose.yml`: `name: voiceagents`; services postgres, redis, clickhouse, temporal, temporal-ui, minio,
  minio-init, mailpit (profiles `[infra, apps, e2e]` exactly as spec; temporal-ui `[infra, apps]`), otel-collector
  (decision 1, ports 4317, 4318, 13133, 8889), jaeger, prometheus, grafana (`[observability]`). Images/ports/env exactly
  as specs/29 §2. Named volumes: `pgdata`, `miniodata` per spec, plus `redisdata` (`/data`, AOF is on),
  `chdata` (`/var/lib/clickhouse`), `prometheusdata`, `grafanadata` — record in the Decision. Grafana env:
  `GF_SECURITY_ADMIN_USER=admin`, `GF_SECURITY_ADMIN_PASSWORD=admin`, `GF_AUTH_ANONYMOUS_ENABLED=true`,
  `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer` (local only). Every long-running service `restart: unless-stopped`.
- `infra/otel/collector.yaml` (decision 1).
- `infra/prometheus/prometheus.yml`: `global: {scrape_interval: 15s, evaluation_interval: 15s}`; jobs:
  `otel-collector` (`otel-collector:8889`), `prometheus` (`localhost:9090`), and host-run services via
  `host.docker.internal` (api :8000, telephony-edge :8100, media-worker :8200, `metrics_path: /metrics`). Add
  `extra_hosts: ["host.docker.internal:host-gateway"]` on prometheus (Linux). Worker-type metrics ports are decided
  in P00-T12; leave a comment saying P00-T12/P20-T02 add them? NO (comments that promise future work could read as
  placeholders) — simply scrape what exists now.
- `infra/grafana/provisioning/datasources/prometheus.yaml` (Prometheus `http://prometheus:9090`, default, uid
  `prometheus`) and `infra/grafana/provisioning/dashboards/voiceagents.yaml` (file provider, path
  `/var/lib/grafana/dashboards`, folder VoiceAgents) + `infra/grafana/dashboards/` containing one real dashboard
  `platform-overview.json` is P19 scope — instead keep the dashboards dir present via `.gitkeep`? Not allowed? A
  `.gitkeep` is fine (not a placeholder token). Simpler: do not provision the dashboards provider yet; mount only
  `./infra/grafana/provisioning`; drop the dashboards volume until P19-T02 (Decision 4 extended).
- `scripts/dev_up.sh` (bash, `set -euo pipefail`): arg/`PROFILE` env default `infra`; checks `docker info` reachable
  (clear error otherwise, exit 1); creates `.env` from `.env.example` if missing (compose `env_file` users later
  need it; harmless now); `docker compose --profile "$profile" up -d --wait --wait-timeout 180`; then for profiles
  including the collector, poll `http://localhost:13133/` up to 30 × 1 s via curl (`curl -fsS --retry 30
  --retry-delay 1 --retry-all-errors`, no sleep loop); prints `docker compose ps`.
- `scripts/dev_down.sh`: `docker compose --profile '*' down --remove-orphans` plus `-v` pass-through when called with
  `-v|--volumes`; unknown arg -> usage + exit 2.
- `Makefile`: decisions 2–3.
- `pyproject.toml` dev group: `pyyaml`, `types-pyyaml`; `uv lock`.
- AGENTS.md: Ports line add OTel HTTP 4318, collector health 13133, collector metrics 8889, Jaeger 16686
  (observability profile); learned bullets: `make up` = infra profile; observability stack via
  `make up PROFILE=observability` (note: that profile alone does NOT start postgres etc.; use
  `COMPOSE_PROFILES=infra,observability docker compose up -d --wait` or make dev_up.sh accept a comma list:
  implement `dev_up.sh infra,observability` by splitting on commas into repeated `--profile` flags — do this);
  `make down` stops all profiles, `scripts/dev_down.sh -v` wipes volumes; `docker compose config` works without a daemon.

### Tests — `scripts/tests/test_compose.py` (pyyaml, repo root via `Path(__file__).resolve().parents[2]`)
- `test_image_pins_match_toolchain_table`: parse specs/02 §2 table rows whose value has an image in backticks
  (`pgvector/pgvector:pg16`, `redis:7.2-alpine`, `clickhouse/...:24.8`, `temporalio/auto-setup:1.25`,
  `temporalio/ui:2.31`, `minio/minio:RELEASE...`) and assert each equals the compose service image.
- `test_images_match_spec_compose_block`: extract the ```yaml block under specs/29 §2, yaml-load it, and for every
  service present in BOTH files assert identical `image`, `ports`, `profiles` (except otel-collector: assert spec
  profiles ⊂ ours and `infra` added) and `environment`. Also assert every infra/observability service in the spec
  block that has no `build:` exists in ours (catches drift both ways).
- `test_infra_profile_services`: set of services with `infra` in profiles == {postgres, redis, clickhouse, temporal,
  temporal-ui, minio, minio-init, mailpit, otel-collector}.
- `test_ports_match_agents_md`: parse the AGENTS.md "## Ports (local)" paragraph into name->port for the infra names
  (Postgres 5432, Redis 6379, ClickHouse HTTP 8123 + 9009:9000, Temporal 7233, Temporal UI 8233, MinIO 9000/9001,
  Mailpit 1025/8025, OTel collector 4317, Prometheus 9090, Grafana 3001) and assert each host port is published by
  the right service; and no two services publish the same host port.
- `test_long_running_services_have_healthchecks`: every service except minio-init and otel-collector (documented
  exceptions) defines `healthcheck.test`; depends_on conditions reference existing services.
- `test_named_volumes_declared`: every `name:/path` volume source without `./` is in top-level `volumes`.
- `test_credentials_match_env_example`: parse `.env.example` (reuse `env_spec` parser if it exposes one; else simple
  `KEY=value  # comment` split) — DATABASE_URL user/pass/db == postgres env; S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY ==
  MINIO_ROOT_*; minio-init entrypoint creates exactly S3_BUCKET_RECORDINGS/KB/VOICES; CLICKHOUSE_URL db == CLICKHOUSE_DB;
  host ports in REDIS_URL/TEMPORAL_HOST/SMTP_URL/OTEL_EXPORTER_OTLP_ENDPOINT/S3_ENDPOINT_URL are published.
- `test_collector_config`: otlp grpc endpoint `0.0.0.0:4317`, http `0.0.0.0:4318`, health_check `0.0.0.0:13133`,
  every pipeline's receivers/processors/exporters are defined; memory_limiter is first processor.
- `test_prometheus_config`: scrapes `otel-collector:8889`; every job has static targets; file referenced in compose.
- `test_compose_mounts_exist`: every `./` bind source exists in the repo.
- `test_postgres_init_sql`: creates va_app, va_system, temporal DB (per spec text).
- `test_scripts_executable_and_strict`: dev_up.sh/dev_down.sh have exec bit, `#!/usr/bin/env bash`, `set -euo pipefail`.
- `test_makefile_up_down_use_scripts`: Makefile `up` invokes `scripts/dev_up.sh`, `PROFILE ?= infra`, `down` invokes
  `scripts/dev_down.sh`.
- `test_docker_compose_config_valid` (`@pytest.mark.integration`? no — it needs only the CLI): skip if
  `shutil.which("docker")` is None; run `docker compose --profile '*' config --quiet` via subprocess with timeout=30
  and assert rc 0 (no daemon needed). Also `docker compose --profile infra config --services` == infra set.
- `test_dev_down_rejects_unknown_arg`: run `bash scripts/dev_down.sh --bogus` -> exit 2 (it must parse args before
  calling docker). And `dev_up.sh` exits 1 with a message when `docker info` fails: run with `PATH` pointing at a tmp
  dir containing a fake `docker` script that exits 1 (+ real bash path) — no daemon touched.

### Gates
`make fmt lint typecheck placeholders test`, `make check`; live: `orb start` then `make up`, `docker compose ps`
(all healthy), `curl -fsS localhost:13133/`, `make up PROFILE=infra,observability` then `make down`. pip-audit on
`uv export`, pnpm audit, radon, jscpd; build.done plain string (mem-1792920384-602e).

### Risks
- Live verification depends on OrbStack starting; image pulls take minutes (use timeout 600000).
- `filterwarnings=error` + subprocess: use `subprocess.run(..., check=False, capture_output=True, timeout=...)`.
- Temporal auto-setup vs pre-created `temporal` DB, `tctl` presence, compose `--wait` with one-shot job — see
  decisions 6–8; fix based on live behaviour and record what was observed.
- ClickHouse 24.8 with `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1` and empty default password allows network access only
  when `CLICKHOUSE_SKIP_USER_SETUP=1` in newer images; 24.8 is fine but verify `curl localhost:8123/ping` and a query
  `SELECT 1` from the host.
- The placeholder gate scans `infra/` and `scripts/` — no banned tokens in YAML comments.

## Build P00-T09 — done (Builder)

Files: `docker-compose.yml` (infra + observability services, no build services), `infra/docker/postgres-init/01-roles.sql`
(idempotent roles va_app/va_system + `temporal` DB via `\gexec`), `infra/otel/collector.yaml`, `infra/prometheus/prometheus.yml`,
`infra/grafana/provisioning/datasources/prometheus.yaml`, `scripts/dev_up.sh` (comma-separated profiles, daemon check,
`.env` bootstrap, `up --wait` on long-running services then `run --rm` for ONE_SHOT_SERVICES, collector health poll),
`scripts/dev_down.sh` (`--profile '*' down --remove-orphans`, `-v`), Makefile (`PROFILE ?= infra`, up/down -> scripts),
pyproject dev group `pyyaml`/`types-pyyaml`, `scripts/tests/test_compose.py` (32 tests), AGENTS.md Ports + 4 learned bullets.

Live findings (OrbStack daemon started with `orb start`, Docker 29.8.2):
- `minio/minio` and `minio/mc` no longer exist on Docker Hub (hub API "object not found"; Quay 401). Switched to
  `bitnamilegacy/minio:2024.10.13` (binary reports RELEASE.2024-10-13T13-34-11Z, same commit) + `bitnamilegacy/minio-client:2024.10.8`.
  Updated specs/02 §2 table + specs/29 §2 YAML block with Decision notes.
- `temporalio/ui:2.31` tag does not exist -> `2.31.2` (spec updated).
- `mc mb -p` IS `--ignore-existing`; combining errors -> kept spec form.
- `docker compose up --wait` exits 1 on `minio-init exited (0)` -> dev_up.sh splits one-shot jobs (decision 8 fallback).
- auto-setup 1.25 tolerates the pre-created `temporal` DB; spec `tctl` healthcheck works.
Evidence: `make up` rc 0, all 8 long-running infra services healthy, buckets created, collector `/` "Server available",
ClickHouse `SELECT 1` -> 1, roles va_app/va_system + DBs temporal/temporal_visibility present, redis PONG, mailpit/minio 200.
`make up PROFILE=infra,observability` rc 0: jaeger/prometheus/grafana healthy, Prometheus targets otel-collector+prometheus up
(host api/edge/media down: not running), Grafana datasource provisioned, OTLP/HTTP span to :4318 visible in Jaeger services.
`make down` rc 0, 0 containers left, volumes kept.
Gates: make fmt 0, lint 0, typecheck 0 (51 files), placeholders 0 findings, test 714 passed 1 skipped cov 99.95 %,
make check 0 (spec refs 0 problems); pip-audit + pnpm audit no vulns; radon avg A (2.70), no C+ blocks; jscpd 0 clones.

## Review P00-T09 — rejected (1 finding)

Gates re-run: make lint 0, typecheck 0, placeholders 0 findings, test 714 passed 1 skipped (cov 99.95 %).
Compose/spec/Decision review otherwise clean (images, ports, env, profiles, volumes, collector + prometheus config,
roles SQL, Makefile, AGENTS.md all consistent with specs/29 §2 + Decisions).

1. `scripts/dev_up.sh:73` — `for job in "${jobs[@]}"` crashes with `jobs[@]: unbound variable` (exit 1) under
   `set -u` on bash < 4.4 whenever the selected profiles contain no one-shot service. Reproduced with a fake `docker`
   on PATH: `PATH=<fake>:$PATH /bin/bash scripts/dev_up.sh observability` -> rc 1 after `up --wait` (stock macOS
   bash 3.2; `#!/usr/bin/env bash` resolves to it on any Mac without Homebrew bash, which is how `make up` invokes
   it). AGENTS.md documents `PROFILE=observability` as a usage, and the phase exit gate is a clean clone. Fix: guard
   empty arrays (`if [[ ${#jobs[@]} -gt 0 ]]; then ...` or `${jobs[@]+"${jobs[@]}"}`; same care for any other
   possibly-empty array expansion, e.g. `profile_flags` when PROFILES is `,`). Add a test in
   `scripts/tests/test_compose.py` that runs dev_up.sh with a fake `docker` (info ok, `config --services` prints
   only long-running services) and asserts rc 0 and that `run --rm` is not invoked; run it with `bash` and, when
   present, `/bin/bash`, so macOS dev machines catch bash-3.2 regressions.

## Build P00-T09 (rework after review) — done (Builder)

Finding 1 fixed in `scripts/dev_up.sh`: one-shot job loop guarded by `${#jobs[@]} -gt 0`; an empty profile list
(e.g. `,`) now exits 2 with "no profile given" before any array expansion (`profile_flags` can no longer be empty);
comment explains the bash 3.2 `set -u` empty-array rule. `services` was already guarded (exit 2 when empty).
Tests added in `scripts/tests/test_compose.py` (fake `docker` + `curl` on PATH, parametrized over `bash` on PATH and
`/bin/bash`): observability profile without one-shot jobs -> rc 0 and no `run --rm`; `infra,observability` -> `up --wait`
lists only long-running services and the next call is `run --rm minio-init`; `,` -> rc 2 and only `docker info` called.
Verified the old script fails 3 of these on /bin/bash 3.2 (and the `,` case on Homebrew bash too).
Gates: make fmt 0, lint 0, typecheck 0, placeholders 0, test 720 passed 1 skipped cov 99.95 %, make check 0;
pip-audit no known vulns, pnpm audit no vulns, radon avg A (2.69), jscpd 0 clones.

## Review P00-T09 (rework) — approved

Gates re-run: make lint 0, typecheck 0, placeholders 0 findings, test 720 passed 1 skipped (cov 99.95 %).
Finding 1 resolved: `jobs` loop guarded, empty profile list exits 2 before expansion; new fake-docker tests pass on
both PATH bash and /bin/bash 3.2 (9 dev_up tests).

## Plan P00-T10 — ML model and audio asset scripts

Spec refs: specs/07 §5.3 (Silero VAD v5 ONNX, tag `v5.1.2`, path `src/silero_vad/data/silero_vad.onnx`, sha256 in
`models/MANIFEST.json` recorded on first fetch and verified later), specs/07 §10.2 (background tracks), specs/02 §1
layout (`models/`, `assets/audio/` committed, procedural, no third-party audio), specs/06 §10.2 (hold music playback).
`.gitignore` already ignores `/models/*.onnx` and keeps `models/MANIFEST.json` + `assets/audio/` tracked.
Makefile `setup` already calls `scripts/fetch_models.sh` and `uv run python scripts/generate_audio_assets.py`.

### Decisions (record as `> Decision (2026-10-25):` in the named spec, same commit)
1. specs/07 §5.3: the fetch logic lives in `scripts/fetch_models.py` (typed, tested); `scripts/fetch_models.sh` is the
   spec-named entry point and only `exec uv run --no-sync python scripts/fetch_models.py "$@"` (with
   `set -euo pipefail`, cd to repo root via `dirname "$0"`). Manifest format below.
2. specs/07 §10.2: assets are stored at their playback level so the mixer applies unit gain: `static` −40 dBFS RMS,
   `office`/`cafe`/`restaurant` −22 dBFS RMS, `hold_music` −18 dBFS RMS (peak-limited to −1 dBFS). Master synthesis
   at 16 kHz; the `.ulaw` (8 kHz G.711 μ-law, raw, no header) is derived by FFT-domain low-pass at 3 400 Hz (static
   keeps its 300 Hz high-pass) + 2:1 decimation; `.pcm16k` is raw little-endian int16 mono 16 kHz. Randomness comes
   only from `numpy.random.PCG64(seed).random_raw()` (bit-generator streams are version-stable, unlike `Generator`
   methods); float conversion is ours (`(raw >> 11) * 2**-53`). Fixed seeds per track (e.g. crc32 of the track name
   XOR a global seed constant `20261025`).
3. specs/06 §10.2 step 1 + specs/07 §10.2: hold music is a procedural asset `assets/audio/hold_music.{ulaw,pcm16k,wav}`
   (60 s loop). The playback URL becomes `https://{PUBLIC_API_BASE}/static/hold/hold_music.wav` (Telnyx
   `playback_start` accepts WAV or MP3; we have no MP3 encoder and add none). The WAV is 16 kHz 16-bit PCM mono,
   written with stdlib `wave` (deterministic header). Serving it is the API's job in a later phase; only the spec line
   changes now.

### A. `scripts/fetch_models.py` (mypy strict, every function radon CC ≤ B)
- Constants: `REPO_ROOT = Path(__file__).resolve().parents[1]`, `MANIFEST_PATH = REPO_ROOT / "models/MANIFEST.json"`,
  `SILERO = ModelSpec(name="silero_vad", path="models/silero_vad.onnx",
  url="https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx",
  source="snakers4/silero-vad", tag="v5.1.2", license="MIT")`; `MODELS: tuple[ModelSpec, ...] = (SILERO,)`.
- `@dataclass(frozen=True) class ModelSpec` (name, path, url, source, tag, license).
- `@dataclass(frozen=True) class ManifestEntry` (name, path, url, source, tag, license, sha256, bytes).
- Manifest JSON (sorted keys, indent 2, trailing newline):
  `{"version": 1, "models": [{"name","path","url","source","tag","license","sha256","bytes"}]}`.
- `load_manifest(path) -> dict[str, ManifestEntry]` (missing file -> `{}`; malformed -> `ManifestError`).
- `write_manifest(path, entries)` atomic (tmp + `os.replace`).
- `sha256_file(path) -> str` (chunked 1 MiB).
- `Fetcher = Callable[[str], bytes]`; `http_fetch(url) -> bytes` via `urllib.request.urlopen(url, timeout=60)`
  (only `https://` allowed; `S310` noqa justified by the scheme check; max 50 MB guard).
- `verify(spec, entry, root) -> VerifyResult` enum: `OK`, `MISSING`, `MISMATCH`, `UNRECORDED`.
- `fetch_one(spec, entry|None, root, fetch) -> ManifestEntry`: if file exists and matches the recorded sha -> return
  entry (no download). Else download; if an entry exists and the digest differs -> raise `ChecksumError` and do NOT
  write the file; write atomically to `models/` (`mkdir(parents=True, exist_ok=True)`); return new/same entry.
  Also reject when the recorded `url`/`tag` differs from `spec` (manifest pinned to another release) -> `ManifestError`
  unless `--update` is given (then re-record).
- `main(argv: Sequence[str] | None = None, *, root: Path = REPO_ROOT, fetch: Fetcher = http_fetch) -> int`
  argparse: `--check` (verify only, never download: exit 0 all OK, exit 1 and print `models/silero_vad.onnx:
  sha256 mismatch (expected X, got Y)` / `missing` / `not recorded in manifest`), `--update` (accept new digest,
  re-record). Default mode: fetch missing/mismatched files, record first digests, exit 1 on `ChecksumError`/
  `ManifestError`/`urllib.error.URLError`/`TimeoutError` with a one-line message on stderr. Prints one status line per
  model (`ok`, `downloaded`, `recorded`).
- Builder runs `scripts/fetch_models.sh` for real once, commits `models/MANIFEST.json` with the real sha256 + byte
  count; sanity-check the downloaded file starts with ONNX protobuf bytes and is ~2 MB (v5 is ≈ 2.2 MB).

### B. `scripts/generate_audio_assets.py` (mypy strict, CC ≤ B; numpy only)
- `pyproject.toml` dev group: add `numpy>=2.3` (scripts run in the root env); `uv lock`. Do not add scipy/soxr.
- Constants: `SR = 16_000`, `SR_ULAW = 8_000`, `LOOP_S = 60.0`, `XFADE_S = 0.5`, `SEED = 20261025`,
  `OUT_DIR = REPO_ROOT / "assets/audio"`, `TRACKS = ("static", "office", "cafe", "restaurant", "hold_music")`,
  `TARGET_DBFS = {"static": -40.0, "office": -22.0, "cafe": -22.0, "restaurant": -22.0, "hold_music": -18.0}`.
- `class Rng` wrapping `np.random.PCG64(seed)`: `uniform(n) -> NDArray[float64]`, `normal(n)` (Box–Muller from
  two uniforms), `poisson_times(rate_hz, duration_s, jitter) -> NDArray` (exponential inter-arrival via −ln(u)/rate).
- DSP helpers (all vectorized, no Python per-sample loops): `bandpass_fft(x, lo, hi, sr)` (rfft, zero bins outside,
  irfft n=len), `pink(n, rng)` (white -> spectrum × 1/sqrt(f), f≥1 Hz), `brown(n, rng)` (spectrum × 1/f, then
  high-pass 20 Hz), `envelope(n, rng, f_lo=3, f_hi=6)` (sum of 3 sinusoids with random freq/phase, mapped to
  0.2…1.0), `murmur(n, rng)` = bandpass(white, 200, 2000) × envelope, `clicks(n, rng, rate 2–6/s, 5–20 ms bursts)`
  (white bursts × exp decay placed by slicing at Poisson onsets), `clinks(n, rng, 0.2–1/s)` (decaying sinusoids
  2–5 kHz, decay 40–120 ms), `rumble(n, rng)` = bandpass(brown, 20, 120).
- Recipes per specs/07 §10.2: `static` = bandpass(pink, 300, 3400); `office` = brown + clicks + 0.5·murmur;
  `cafe` = murmur + clinks; `restaurant` = 1.5·murmur + clinks + rumble; `hold_music` = I–vi–IV–V (C, A m, F, G)
  at 72 BPM, 2-bar chords, soft additive tones (fundamental + 2 harmonics at −9/−15 dB) with ADSR per note plus an
  arpeggio voice in eighth notes; all frequencies < 3 400 Hz so the 8 kHz version is intact.
- `make_loop(x_long, sr)`: synthesize `LOOP_S + XFADE_S` seconds, equal-power cross-fade the last 0.5 s into the
  first 0.5 s, return exactly `LOOP_S * sr` samples (seam continuity).
- `level(x, target_dbfs)`: scale to target RMS; soft peak limit at −1 dBFS (scale down if peak > 0.89; recompute so
  RMS stays ≥ target − 0.5 dB, which holds for all recipes — assert in tests).
- `to_pcm16(x) -> bytes` (round half-even via `np.rint`, clip, `<i2`), `decimate_to_8k(x)` (FFT low-pass 3 400 Hz,
  take `x[::2]`), `ulaw_encode(pcm: NDArray[int16]) -> bytes` (G.711 μ-law, bias 0x84, clip 32635, segment via
  vectorized bit-length; bytes inverted ^0xFF). Do NOT use `audioop` (deprecated -> DeprecationWarning ->
  `filterwarnings=error`). Also `ulaw_decode` (for tests and as the inverse reference).
- `write_wav(path, pcm16k_bytes)` via `wave` (nchannels 1, sampwidth 2, 16 kHz) — hold_music only.
- `render(track) -> dict[str, bytes]` mapping filename -> bytes (`{track}.ulaw`, `{track}.pcm16k`, plus
  `hold_music.wav`).
- `main(argv=None, *, out_dir=OUT_DIR) -> int`: argparse `--check` (render in memory and compare with files on
  disk; exit 1 listing missing/different files), `--out DIR`. Default writes atomically only when content differs and
  prints `wrote`/`unchanged` per file. Generation for all tracks must take < 10 s (FFT on ~1 M samples is fast).
- Run it and commit the 11 files under `assets/audio/` (sizes: `.ulaw` 480 000 B, `.pcm16k` 1 920 000 B, wav
  1 920 044 B → ≈ 12 MB total; acceptable, specs/02 says assets are committed).

### C. Tests
`scripts/tests/test_fetch_models.py` (no network: inject a fake `fetch` returning fixture bytes made at test time):
- `test_first_fetch_records_sha_and_bytes`: empty root -> main() 0, file written, manifest entry sha == hashlib of
  bytes, url/tag/license from `SILERO`.
- `test_second_run_does_not_download` (fake fetch raises if called) -> 0, prints ok.
- `test_check_fails_on_corrupted_model` (flip a byte) -> `main(["--check"])` == 1, stderr mentions `mismatch` and the
  path. (acceptance)
- `test_check_fails_when_missing` / `test_check_fails_when_unrecorded` -> 1.
- `test_corrupted_file_is_redownloaded_when_digest_matches_manifest` -> 0, file restored.
- `test_download_mismatching_manifest_is_rejected_and_not_written` -> 1, existing file untouched / absent.
- `test_manifest_pinned_to_other_tag_is_rejected` -> 1; `--update` re-records -> 0.
- `test_network_error_exits_1` (fetch raises `urllib.error.URLError`).
- `test_http_fetch_rejects_non_https` (`ValueError` for `file://`/`http://`), and a respx-free unit of the size
  guard with a monkeypatched `urlopen` returning an object with `read(n)`.
- `test_committed_manifest_matches_spec`: repo `models/MANIFEST.json` has exactly one entry, url contains
  `v5.1.2/src/silero_vad/data/silero_vad.onnx`, sha256 is 64 hex, bytes > 1 MB; and that url/tag match specs/07 §5.3
  text (parse the tag and path from the spec).
- `test_shell_wrapper`: `scripts/fetch_models.sh` executable, `#!/usr/bin/env bash`, `set -euo pipefail`, execs
  `scripts/fetch_models.py`.
- `test_model_check_if_present`: if `models/silero_vad.onnx` exists locally, `main(["--check"])` == 0 (skip otherwise).

`scripts/tests/test_generate_audio_assets.py`:
- `test_committed_assets_are_byte_identical_to_regeneration` (acceptance): `main(["--check"])` == 0 against the repo
  `assets/audio/`; plus render twice in-process -> equal bytes.
- `test_check_detects_modified_asset`: write to tmp out dir, alter one byte -> `--check` 1; missing file -> 1.
- `test_write_is_idempotent`: second run reports `unchanged`, mtimes unchanged.
- `test_sizes_and_formats` (parametrize over tracks): `.ulaw` == 480 000 B, `.pcm16k` == 1 920 000 B; WAV header
  1 ch / 2 bytes / 16 000 Hz / 960 000 frames.
- `test_levels` (parametrize): RMS of pcm16k within ±0.5 dB of `TARGET_DBFS`, peak ≤ −1 dBFS; μ-law decoded RMS
  within ±1 dB of target (static uses ±1 dB too).
- `test_static_is_band_limited`: >99 % of spectral energy in 300–3 400 Hz.
- `test_ulaw_band_limit`: 8 kHz files have <1 % energy above 3 450 Hz (decimation alias check).
- `test_loop_seam_is_continuous`: |x[-1] − x[0]| and the RMS of the 20 ms around the seam (tail+head concatenated)
  within 3 dB of the track RMS (no click/gap).
- `test_ulaw_codec_known_values`: G.711 table spot checks (0 -> 0xFF, 32767 -> 0x80, −32768 -> 0x00, 1 -> 0xFF,
  −1 -> 0x7F) and decode(encode(x)) error within μ-law segment step for a ramp over the full int16 range.
- `test_clicks_and_clinks_rates`: count onsets produced by the event generators for a fixed seed lie within the
  spec ranges (office clicks 2–6 /s mean, cafe clinks 0.2–1 /s).
- `test_rng_is_stable`: first 4 raw uniforms for seed 1 equal hard-coded constants (detects a numpy bit-generator
  change before the asset drift test does).
- `test_hold_music_band_limited_and_tonal`: spectral peaks at the chord fundamentals (C4 261.63 Hz etc.) and no
  energy above 3 400 Hz.

### D. Docs
- specs/07 §5.3 + §10.2 Decisions (1, 2, 3 above); specs/06 §10.2 step 1 URL → `hold_music.wav` with Decision 3.
- AGENTS.md learned bullets: `scripts/fetch_models.sh --check` verifies, `--update` re-pins; `.onnx` not committed,
  manifest is; `generate_audio_assets.py --check` is the asset drift check and assets must be regenerated + committed
  when the generator changes; use `PCG64.random_raw`, never `Generator` methods, for anything committed; no `audioop`.

### Gates
`make fmt lint typecheck placeholders test`, `make check`; `scripts/fetch_models.sh` (real download), then
`scripts/fetch_models.sh --check` (0), corrupt a copy -> 1; `uv run python scripts/generate_audio_assets.py` twice
(second run all `unchanged`), `--check` 0; `shasum` of assets equal across both runs. pip-audit on `uv export`, pnpm
audit, radon cc (no C+), jscpd. build.done plain string per mem-1792920384-602e.

### Risks
- numpy `Generator` stream changes across versions -> avoided via raw PCG64 output + own conversion; `test_rng_is_stable`.
- Cross-platform float ULP differences (arm64 Mac vs x86 CI) could flip an int16/μ-law rounding on a rare sample and
  break the byte-identity test in CI. Keep math simple (FFT + elementwise), round once at the end. If CI ever shows
  drift, the fallback is to compare sha256 per platform; do not pre-emptively weaken the test.
- 12 MB of committed binaries; fine per specs/02, but tests that render all tracks must stay < 10 s total — render once
  per session with a module-scoped fixture (`functools.cache` on `render`), xdist-safe (pure function).
- `uv run --no-sync` in the wrapper requires `uv sync` first (Makefile setup order already does that).
- GitHub raw URL availability; tests never hit the network.

## Build P00-T10 — done (Builder)

Files: `scripts/fetch_models.py` + `scripts/fetch_models.sh` (exec wrapper), `models/MANIFEST.json` (real fetch:
silero v5.1.2, 2 327 524 B, sha256 2623a295…788f; `.onnx` ignored), `scripts/generate_audio_assets.py` + 11 files in
`assets/audio/`, tests `scripts/tests/test_fetch_models.py` (30) and `test_generate_audio_assets.py` (41); numpy>=2.3
dev dep; Decisions in specs/07 §5.3, §10.2 and specs/06 §10.2 (hold_music.wav); AGENTS.md bullets.
Deviations from plan (recorded in specs/07 §10.2 Decision): hold music at 64 BPM (not 72) so 60 s = exactly two
I–vi–IV–V progressions, rendered circularly (no cross-fade); `.ulaw` is levelled separately from `.pcm16k` so both
hit the target RMS; limiter is tanh soft-clip (office hits it once, RMS -22.13 dB). Seam test = seam jump ≤ 99.9th
percentile of sample steps (+ 20 ms RMS check on stationary `static`), since sparse clicks make windowed RMS noisy.
Gates: make fmt 0, lint 0, typecheck 0 (55 files), placeholders 0 findings, make test 791 passed 1 skipped cov 99 %
(fetch_models 100 %, generate 99 %), make check 0. Real `fetch_models.sh --check` 0; corrupted byte -> 1 with mismatch
message; regeneration twice -> all 11 `unchanged`, shasum identical. pip-audit no known vulns, pnpm audit no vulns,
radon avg A (2.63, no C+), jscpd 0 clones.

## Review P00-T10 — approved

Re-ran: make lint 0, typecheck 0, placeholders 0 findings, make test 791 passed 1 skipped (cov 99.92 %), make check 0;
`generate_audio_assets.py --check` 0, `fetch_models.sh --check` 0, `--check --update` rejected (argparse exit 2).
Checked μ-law codec (bias 0x84 / clip 32635, decode precedence), make_loop seam math, hold-music timing (8 chords of
7.5 s = 60 s), spec Decisions in 07 §5.3/§10.2 and 06 §10.2 (no remaining hold_music.mp3 refs), specs/02 layout.

## Plan P00-T11 — API skeleton with envelopes and OpenAPI export

Spec refs: specs/31 §1 (envelope matrix, `request_id`, validation → 400), specs/31 §4 (install_handlers, x-envelope),
specs/01 §1 (api: `uvicorn va_api.main:app`, port 8000), specs/26 §1 (OpenAPI rules: info, servers, operationId regex,
tags order, x-envelope, x-scope, examples, ErrorLegacy/ErrorEnvelope, ApiKeyAuth), specs/27 §9 (api readyz = Postgres
`SELECT 1`, Redis PING, Temporal connected, all within 1 s; body shape), specs/27 §1 (middleware binds `request_id`),
specs/28 §5 (CORS + `X-Content-Type-Options`/`Referrer-Policy` on all responses), specs/04 §2.4 (public allowlist
includes `GET /v1/health`, `GET /openapi.json`), specs/02 §6 (api env vars), specs/02 §9 Decision (b) (drift gate).

### Package `apps/api` (workspace member, `uv_build`, src layout, `py.typed`)
- `apps/api/pyproject.toml`: name `va-api`, version `0.1.0`; deps via `uv add --package va-api`: `va-common`
  (workspace), `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2.0.35`, `asyncpg`, `temporalio>=1.8`,
  `pydantic>=2.9`, `pydantic-settings`. Root: dev group `va-api` + `[tool.uv.sources] va-api = { workspace = true }`,
  dev dep `openapi-spec-validator`; mypy `mypy_path` += `apps/api/src:apps/api/tests`, `files` += `apps/api/src`,
  `apps/api/tests`; pytest `pythonpath` += `apps/api/tests` (for the test-route helper module). `uv lock`.
- `va_api/__init__.py`: `__version__ = "0.1.0"` (test: equals `importlib.metadata.version("va-api")`).
- `va_api/settings.py`: `class ApiSettings(RedisSettings)`, `service_name = "api"`, fields (specs/02 §6 defaults):
  `database_url`, `database_pool_size: int = 10`, `temporal_host = "localhost:7233"`, `temporal_namespace = "default"`,
  `public_api_base_url = "http://localhost:8000"`, `dashboard_base_url = "http://localhost:3000"`,
  `openapi_public_server_url = "http://localhost:8000"`. No secrets yet (phase 02).
- `va_api/envelopes.py`:
  - `class Envelope(StrEnum)`: `LEGACY = "legacy"`, `ENVELOPE = "envelope"`.
  - `FAMILIES: Final[tuple[tuple[str, Envelope], ...]]` = every pattern of the specs/31 §1 table verbatim
    (`/v1/calls*` … `/v2/batches*` legacy; `/v1/knowledge*` … `/v1/audit_logs*`, `/internal/*` envelope; `/v1/me`
    exact).
  - `classify_path(path: str) -> Envelope`: pattern ending `*` = raw prefix match (`/numbers/*` and `/internal/*` also
    match the bare `/numbers` / `/internal`); pattern without `*` = exact match; **longest matching pattern wins**
    (`/v1/orgs/x` → legacy via `/v1/orgs*` over `/v1/org*`; `/v1/memory` envelope; `/v1/me` envelope); no match →
    `LEGACY` (Bland default).
  - `class EnvelopeRoute(APIRoute)` with class attr `envelope: ClassVar[Envelope]`; `__init__(self, path, endpoint,
    **kwargs)` sets `kwargs["openapi_extra"] = {**(kwargs.get("openapi_extra") or {}), "x-envelope": self.envelope}`
    (route-supplied keys such as `x-scope` kept; an explicit conflicting `x-envelope` raises `ValueError`).
    Subclasses `LegacyRoute`, `EnvelopedRoute`.
  - `legacy_router(**kwargs) -> APIRouter` / `envelope_router(**kwargs) -> APIRouter` → `APIRouter(route_class=...)`.
  - `route_envelope(scope: Scope) -> Envelope`: `scope.get("route")` (FastAPI `APIRoute.matches` puts the matched
    route in the child scope; Builder confirms with a test) → its `openapi_extra["x-envelope"]` when present, else
    `classify_path(scope["path"])` (404/405/unmatched paths).
- `va_api/errors.py`:
  - Pydantic models (OpenAPI components, also used by tests to validate bodies): `ErrorItem{error: str, message: str,
    field: str | None = None, details: dict[str, Any] | None = None}` (examples via `json_schema_extra`),
    `ErrorLegacy{status: Literal["error"], message: str, errors: list[ErrorItem], request_id: str}`,
    `ErrorEnvelope{data: None, errors: list[ErrorItem], request_id: str}`.
  - `error_body(envelope, items: Sequence[Mapping[str, Any]], request_id) -> dict[str, Any]`: legacy →
    `{"status":"error","message": items[0]["message"], "errors": items, "request_id": rid}`; envelope →
    `{"data": None, "errors": items, "request_id": rid}` (items omit absent `field`/`details`).
  - `error_response(request, status, items, headers=None) -> JSONResponse` (envelope via `route_envelope`, request id
    via `request_id_of(request)`; also sets `X-Request-Id` + security headers itself so the 500 path, which runs in
    Starlette's ServerErrorMiddleware outside our middleware, still carries them).
  - `validation_items(exc: RequestValidationError) -> list[dict]`: one item per error, `error="INVALID_INPUT"`,
    `field` = dotted `loc` without the leading `body`/`query`/`path`/`header`/`cookie` segment (ints kept as
    `items.0.name`); omitted when empty or when `type == "json_invalid"`; `message` = pydantic `msg` with a leading
    `"Value error, "` stripped.
  - `HTTP_STATUS_CODES: Mapping[int, str]` for Starlette `HTTPException`: 400 INVALID_INPUT, 401 AUTH_FAILURE,
    403 FORBIDDEN, 404 NOT_FOUND, 409 CONFLICT, 413 KB_FILE_TOO_LARGE is wrong → use INVALID_INPUT, 429 RATE_LIMITED,
    503 SERVICE_UNAVAILABLE, 504 UPSTREAM_TIMEOUT; other 4xx → INVALID_INPUT, other 5xx → INTERNAL_ERROR. Status and
    exc headers (`Allow`, `Retry-After`) preserved; 404 message "Not found.", 405 "Method not allowed.", otherwise
    `exc.detail` when it is a string.
  - `install_handlers(app: FastAPI) -> None`: `AppError` → `exc.http_status`, `[exc.to_error_item()]`;
    `RequestValidationError` → 400; `StarletteHTTPException` → mapping above; `Exception` → 500
    `INTERNAL_ERROR`/"Internal server error" (logs `api.unhandled_exception` with `exc_info`, `request_id`, method,
    route; body never contains exception text).
- `va_api/middleware.py` (pure ASGI, no `BaseHTTPMiddleware`):
  - `REQUEST_ID_HEADER = "x-request-id"`; valid incoming = `^[A-Za-z0-9._:-]{1,128}$` (echoed), else generated
    `"req_" + new_id().hex` (UUIDv7 via `va_common.ids.new_id`). `request_id_of(request) -> str` reads
    `request.state.request_id` (generates and stores if absent).
  - `RequestContextMiddleware(app)`: http/websocket scopes only; stores id in `scope["state"]["request_id"]`, runs the
    app inside `structlog.contextvars.bound_contextvars(request_id=...)`, appends `x-request-id`,
    `x-content-type-options: nosniff`, `referrer-policy: no-referrer` to `http.response.start` (no duplicates).
  - `MetricsMiddleware(app, metrics: ServiceMetrics)`: `va_http_requests_total{route,method,status}` and
    `va_http_request_duration_ms{route,method}` (monotonic); route label = matched `APIRoute.path_format` from
    `scope["route"]`, else `"unmatched"`; status 500 recorded when the app raises before `http.response.start`.
  - Order in `create_app` (outer → inner): RequestContext → Metrics → CORS → router (add in reverse).
- CORS (specs/28 §5): `CORSMiddleware(allow_origins=[settings.dashboard_base_url], allow_credentials=True,
  allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"], allow_headers=["Authorization","Content-Type",
  "Idempotency-Key","X-CSRF-Token","X-VA-Org-Id","Last-Event-ID"], expose_headers=["X-Request-Id","Retry-After"])`.
- `va_common/health.py` (framework-free, reused by every service in P00-T12):
  `CheckFn = Callable[[], Awaitable[None]]` (raises on failure); `class CheckStatus(StrEnum)` ok/error/timeout;
  `@dataclass(frozen=True) CheckResult(name, status, detail: str | None)`;
  `@dataclass(frozen=True) ReadinessReport(results, version)` with `ok`, `failing -> tuple[str, ...]` (sorted),
  `http_status` (200/503), `body()` → ok: `{"status":"ok","checks":{name: "ok"},"version": v}`; failing:
  `{"status":"unavailable","checks":{...},"failing":[...],"version": v}`;
  `async def run_checks(checks: Mapping[str, CheckFn], *, timeout_s: float = 1.0, version: str) -> ReadinessReport`
  (concurrent in `asyncio.TaskGroup`, each under `asyncio.timeout`; `TimeoutError` → timeout; `Exception` → error with
  `detail = type(exc).__name__`, logged as `health.check_failed` with name/detail; `CancelledError` propagates);
  `liveness_body(version) -> {"status":"ok","version": v}`. Details are logged, never returned.
- `va_api/container.py`:
  - `class TemporalConnector` (`host`, `namespace`; `async client() -> temporalio.client.Client` lazily connects under
    an `asyncio.Lock` and caches; failed connect leaves cache empty; `connect` callable injectable, default
    `Client.connect`); `async check(timeout_s)` → `check_health(timeout=timedelta(seconds=timeout_s))`, raises
    `ConnectionError` when False.
  - `@dataclass(slots=True) class Container(settings, engine: AsyncEngine, redis: Redis, temporal: TemporalConnector)`
    with `readiness_checks() -> dict[str, CheckFn]` = postgres/redis/temporal from `va_api.health`.
  - `@asynccontextmanager async def build_container(settings) -> AsyncIterator[Container]`: `create_async_engine(url,
    pool_size=settings.database_pool_size, pool_pre_ping=True)`, `va_common.redis.redis_client(settings)`, connector;
    no network at startup; exit: `engine.dispose()`, `redis.aclose()`.
  - `ContainerFactory = Callable[[ApiSettings], AbstractAsyncContextManager[ReadinessSource]]` where
    `ReadinessSource` is a Protocol with `readiness_checks()` (tests pass fakes).
- `va_api/health.py`: `async postgres_check(engine)` (`SELECT 1` via `engine.connect()`), `redis_check(client)`
  (`redis_ping(client, timeout_s=1.0)`, False → `ConnectionError`), `temporal_check(connector)`.
- `va_api/routers/health.py`: `public_router = legacy_router(tags=["Health"])` with
  `GET /v1/health` (`operation_id="health_get"`, `response_model=HealthStatus` = `{status: Literal["ok"]}` with example,
  `openapi_extra={"x-scope": "public"}`) → `{"status":"ok"}`; `probe_router = APIRouter(include_in_schema=False)` with
  `GET /healthz` (`liveness_body(__version__)`), `GET /readyz` (`run_checks(app.state.container.readiness_checks(),
  timeout_s=app.state.readiness_timeout_s, version=__version__)` → JSONResponse(body, status)), `GET /metrics`
  (`render_latest(app.state.metrics.registry)`).
- `va_api/openapi.py`: `TAGS` = specs/26 §1 tag list in order + `"Health"` appended (as `openapi_tags` dicts);
  `build_openapi(app, settings) -> dict` (installed as `app.openapi`, cached on `app.openapi_schema`):
  `get_openapi(title="VoiceAgents API", version=__version__, openapi_version="3.1.0", routes=app.routes, tags=TAGS,
  contact={"name": "VoiceAgents Support", "url": "https://voiceagents.ai/support", "email": "support@voiceagents.ai"},
  license_info={"name": "Proprietary"}, servers=servers(settings))`; then: add `ErrorItem`/`ErrorLegacy`/
  `ErrorEnvelope` component schemas (`model_json_schema(ref_template="#/components/schemas/{model}")`, hoist `$defs`)
  and `securitySchemes.ApiKeyAuth = {"type":"http","scheme":"bearer"}`; for every operation replace a `"422"`
  response with `"400"` and add `"default"`, both `{"description": ..., "content": {"application/json": {"schema":
  {"$ref": "#/components/schemas/ErrorLegacy|ErrorEnvelope"}}}}` by the operation's `x-envelope`; drop
  `HTTPValidationError`/`ValidationError` components when unreferenced.
  `servers(settings)`: `[{"url": OPENAPI_PUBLIC_SERVER_URL, "description": "Production"}, {"url":
  "http://localhost:8000", "description": "Local"}]`, collapsed to the single Local entry when equal (dev default).
- `va_api/main.py`: `create_app(settings: ApiSettings | None = None, *, container_factory: ContainerFactory =
  build_container, init_observability: bool = True, readiness_timeout_s: float = 1.0) -> FastAPI`:
  `FastAPI(title=..., version=__version__, lifespan=...)`; `app.state.settings/metrics/readiness_timeout_s`
  (`build_metrics(Service.API)` — per-app registry), middleware, `install_handlers`, include `public_router`,
  `probe_router`, `app.openapi = partial(build_openapi, app, settings)`. Lifespan: when `init_observability`,
  `obs = observability.init("api", settings, version=__version__)` (no `service=`; metrics are per app) and
  `obs.shutdown()` on exit; `async with container_factory(settings) as container: app.state.container = container;
  yield`. Module level: `app = create_app()` (no I/O at import; drift gate + uvicorn import this).
- `scripts/export_openapi.py`: `main(argv=None) -> int` with `--root` (default repo root), `--app` (default
  `check_openapi_drift.DEFAULT_APP`, validated by `check_openapi_drift.app_target`), `--out` (default
  `openapi/openapi.json`); text = `check_openapi_drift.render(check_openapi_drift.load_app(app).openapi())`; atomic
  write (tmp + `os.replace`, `mkdir -p`) only when changed; prints `wrote <path>` / `unchanged <path>`; exit 1 with a
  one-line stderr message when loading fails. Then run `make openapi` and commit `openapi/openapi.json`.

### Tests
`apps/api/tests/conftest.py`: clear api env vars + chdir tmp (copy pattern from va_common conftest incl.
`reset_logging`), `settings` fixture (`ApiSettings(**{"_env_file": None})` via typed kwargs dict), `FakeChecks`
container factory (dict of name → async callable, records calls), `make_client(app)` helper: `LifespanManager` +
`httpx.AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url="http://test")`.
`apps/api/tests/sample_routes.py`: test-only legacy router (`/v1/calls/_t/...`) and envelope router
(`/v1/knowledge/_t/...`): raise AppError (with field/details), body model `{phone_number: str (pattern),
voicemail: {action: Literal["hangup","leave_message"]}}`, query `limit: int = Query(25, ge=1, le=100)`, a
`field_validator` raising `ValueError("must be E.164")`, raise `HTTPException(409)`/`(429, headers Retry-After)`,
raise `RuntimeError("secret detail")`.
- `test_errors.py`: `test_app_error_legacy_shape`, `test_app_error_envelope_shape_with_details`,
  `test_legacy_message_equals_first_error`, `test_validation_400_legacy_one_item_per_field` (2 bad fields → 2 items,
  fields `phone_number`, `voicemail.action`), `test_validation_400_envelope`, `test_validation_query_field`,
  `test_invalid_json_body_has_no_field`, `test_value_error_prefix_stripped`, `test_unknown_path_uses_family`
  (parametrize `/v1/knowledge/x` envelope, `/v1/calls/x/y` legacy, `/nope` legacy),
  `test_method_not_allowed_keeps_allow_header`, `test_http_exception_mapping` (parametrize 401/403/404/409/429 with
  Retry-After/503/418/502), `test_unhandled_exception_is_500_without_leak` (no "secret detail"/"Traceback" in body,
  X-Request-Id + nosniff headers present, log line has `request_id` and the exception), `test_bodies_validate_against
  _models` (ErrorLegacy/ErrorEnvelope `model_validate`), `test_kb_error_alternate_status_rendered` (500 override).
- `test_envelopes.py`: `test_classify_every_spec_family` (parse specs/31 §1 table rows, assert each pattern → its
  family; sample paths derived from each pattern), `test_longest_match_wins` (`/v1/orgs/1` legacy, `/v1/org` envelope,
  `/v1/me` envelope, `/v1/memory/x` envelope, `/v1/meetings` legacy fallback), `test_route_class_sets_x_envelope`
  (keeps `x-scope`, conflicting value raises), `test_scope_route_is_set_by_fastapi`,
  `test_app_routes_match_family` (every APIRoute in `create_app()` under `/v1`, `/v2`, `/numbers`, `/internal` has
  `x-envelope` == `classify_path(path)`).
- `test_request_context.py`: `test_generated_id_format` (`^req_[0-9a-f]{32}$`, UUID version 7),
  `test_valid_incoming_id_echoed`, `test_invalid_incoming_id_replaced` (parametrize 129 chars, space, `<`),
  `test_ids_unique_per_request`, `test_error_body_request_id_matches_header`, `test_request_id_bound_in_logs_and_cleared`,
  `test_security_headers_on_all_responses` (200, 404, 500, CORS preflight), `test_no_duplicate_headers`.
- `test_cors.py`: `test_preflight_from_dashboard_allowed` (origin echoed, credentials, methods, the six headers),
  `test_other_origin_not_allowed`, `test_simple_request_exposes_request_id`.
- `test_health.py`: `test_healthz_does_not_touch_dependencies`, `test_v1_health_body_exact`,
  `test_readyz_all_ok` (200, checks postgres/redis/temporal ok, version), `test_readyz_redis_down_503`
  (`failing == ["redis"]`, detail not in body), `test_readyz_timeout` (readiness_timeout_s 0.05, slow check →
  "timeout"), `test_readyz_checks_run_concurrently`, `test_metrics_counts_requests` (route `/v1/health`, status 200;
  unknown path → `unmatched`), `test_probe_routes_not_in_schema`.
- `test_checks.py`: `test_postgres_check_unreachable_raises` (`127.0.0.1:1`), `test_redis_check_ok_with_fakeredis`,
  `test_redis_check_unreachable_raises`, `test_temporal_connector_caches_client` (injected connect counting),
  `test_temporal_connector_retries_after_failed_connect`, `test_temporal_check_unhealthy_raises`,
  `test_build_container_opens_and_closes` (no network; engine disposed, redis closed),
  `test_readyz_real_container_reports_unreachable` (all three pointed at port 1 → 503, statuses error|timeout).
- `test_lifespan.py`: `test_observability_init_and_shutdown_called` (monkeypatch `va_api.main.observability.init`),
  `test_container_closed_on_shutdown`, `test_module_app_imports_without_io`, `test_version_matches_metadata`.
- `test_openapi.py`: `test_openapi_31_and_info` (title, version, license Proprietary, contact),
  `test_servers_default_single_local`, `test_servers_production_then_local`, `test_tags_order_matches_spec`
  (parse specs/26 §1 tags line + Health), `test_operations_contract` (operationId regex `^[a-z]+(_[a-z0-9]+)+$` +
  unique, tag, x-envelope, x-scope, 2xx example), `test_no_422_and_400_refs_envelope_component` (app + sample routes),
  `test_error_components_and_security_scheme`, `test_committed_document_is_current`
  (`render(create_app(default settings).openapi())` == `openapi/openapi.json`), `test_committed_document_is_valid_31`
  (`openapi_spec_validator.validate`).
- `packages/va_common/tests/test_health.py`: ok/error/timeout/mixed reports, body shapes, `failing` sorted, detail is
  the exception type name and logged, cancellation propagates, empty checks → ok.
- `scripts/tests/test_export_openapi.py`: writes canonical file for a fake app module (reuse the drift test's
  `app_dir` pattern), second run `unchanged`, drift check passes on the exported file, load failure → 1, bad `--app`
  → 2, default app export into tmp root == committed file.
- Existing `scripts/tests/test_check_openapi_drift.py::test_repository_state` now exercises the real app (must stay 0).

### Docs (Decisions dated 2026-10-25, same commit)
- specs/31 §4: route classes (`LegacyRoute`/`EnvelopedRoute` via `legacy_router`/`envelope_router`) set x-envelope
  instead of dependencies (dependencies do not run for path-unmatched errors); unmatched paths classified from the §1
  patterns (longest match, `*` prefix, default legacy); HTTPException → code mapping incl. 405 as INVALID_INPUT with
  status 405; validation `field` rules (leading location dropped, omitted for invalid JSON, `Value error, ` stripped);
  request id = echoed `[A-Za-z0-9._:-]{1,128}` or `req_<uuidv7 hex>` (matches the specs/05 example).
- specs/27 §1 + §9: request id format (supersedes "uuid4"); readiness body (`unavailable`, `failing`, statuses
  ok/error/timeout, details logged only); `va_common.health.run_checks` is the shared implementation.
- specs/26 §1: servers collapse to one Local entry when `OPENAPI_PUBLIC_SERVER_URL` equals the local URL (specs/02 §6
  default wins over the `https://api.voiceagents.ai` text here); `Health` tag appended; `GET /v1/health` is
  `health_get` with `x-scope: public`; `/healthz`, `/readyz`, `/metrics` excluded from the schema; FastAPI 422 → 400
  plus `default` error response referencing ErrorLegacy/ErrorEnvelope.
- AGENTS.md: `make openapi` after any route/model change (drift gate); new routers come from
  `legacy_router`/`envelope_router` only; API test client recipe (LifespanManager + ASGITransport
  `raise_app_exceptions=False`); `create_app(init_observability=False, container_factory=...)` in tests; 500 handler
  runs outside middleware so it sets headers itself; apps/api added to mypy/pytest paths.

### Gates
`make fmt lint typecheck placeholders test`, `make openapi` (second run `unchanged`), `uv run python
scripts/check_openapi_drift.py` (0), `make check`; smoke: `uv run uvicorn va_api.main:app --port 8000` with `make up`
infra → `/healthz` 200, `/readyz` 200 (Temporal up) and 503 after `docker compose stop redis` (restart after);
pip-audit on `uv export`, pnpm audit, `uvx radon cc -s -a` (no C+), jscpd. build.done plain string (mem-1792920384-602e).

### Risks
- Starlette `ServerErrorMiddleware` re-raises after sending the 500 → test transport needs
  `raise_app_exceptions=False`; pytest `filterwarnings=error` may surface warnings from it — assert none.
- `scope["route"]` is set by FastAPI's `APIRoute.matches`; if a FastAPI version stops doing so, the envelope falls back
  to path classification (still correct for spec families) — covered by `test_scope_route_is_set_by_fastapi`.
- Developer `.env` with a non-default `OPENAPI_PUBLIC_SERVER_URL` makes the module-level drift gate fail locally; tests
  build the app from explicit default settings. Document in AGENTS.md.
- temporalio connect to a closed port may retry internally; readiness timeout bounds it (test accepts error|timeout).
- asyncpg/SQLAlchemy engine must not connect at import or startup (readyz is the only probe); `pool_pre_ping` only.
- 1 s budget per check: run checks concurrently so total readyz ≤ ~1 s.

## Build P00-T11 — done (Builder)

Implemented per plan, with these deviations (all recorded in specs/AGENTS.md):
- FastAPI 0.162 routing: an `include_router(prefix=...)` prefix is NOT in `scope["route"].path_format`, so routers
  carry their own prefix (`legacy_router(prefix=...)`) and `create_app(routers=ROUTERS)` includes them without one.
  `app.state.routers` lists them (tests walk them for `test_app_routes_match_family`). `routers=` is also the test seam
  for the sample routers.
- Request id stored in `scope["va.request_id"]`, not `scope["state"]` (asgi-lifespan shares one state dict across
  requests — caught by `test_ids_unique_per_request`).
- MetricsMiddleware also logs the specs/27 §1 access line (`http.request`: method, path template, status, duration_ms).
- Test helpers in `apps/api/tests/support/` (pytest pythonpath + mypy_path); `apps/api/tests` is in mypy `files` but
  NOT `mypy_path` (otherwise duplicate module `conftest`/`test_health` with va_common in one mypy run).
- `email-validator` added to va-api (FastAPI validates the OpenAPI contact email; without it a warning is logged).
- `openapi.py` `servers()` strips a trailing slash before comparing to the Local URL.

Evidence (this iteration): make fmt ok; make lint "All checks passed!"; make typecheck "no issues found in 81 source
files"; make placeholders "0 finding(s)"; make test 954 passed, 1 skipped, TOTAL 99% (va_api files 98-100%);
make check exit 0 (drift "openapi/openapi.json is current", spec refs 0 problems); make openapi twice → wrote, then
unchanged; pip-audit "No known vulnerabilities found"; pnpm audit clean; radon average A (2.08), no C+; jscpd 0 clones.
Smoke with `make up` + `uvicorn va_api.main:app`: /healthz 200 with headers; /readyz 200; `docker compose stop redis`
→ 503 `failing:["redis"]`; restart → 200; /v1/calls/nope legacy 404, /v1/knowledge/nope envelope 404.
Infra left running.

## Review P00-T11 — approved
Re-ran: make lint (pass), make typecheck (81 files, no issues), make placeholders (0), make test (954 passed, 1 skipped,
99.81 %), check_openapi_drift (current), check_spec_refs (0), openapi_spec_validator on openapi/openapi.json (valid).
Reviewed envelopes/errors/middleware/main/container/health/openapi against specs/31 §1/§3/§4, specs/27 §9, specs/02 §6
env vars, specs/26 §1. Codes used by HTTP mapping all exist in CODES with matching status. Tests use events, no sleeps.

## Plan P00-T12 — Service skeletons with health and graceful shutdown

Goal: six runnable services (`telephony_edge`, `media_worker`, `worker`, `webhook_dispatcher`, `events_sink`, `sim`)
on one shared runtime in `va_common`, so each app is a few small files (jscpd must stay at 0 clones). Readiness per
specs/27 §9 table; `/healthz` `/readyz` `/metrics` on the service port (edge 8100, media 8200, sim 9100-9400) or on the
metrics port (worker 9300, dispatcher 9400, sink 9500); SIGTERM/SIGINT → graceful stop.

### Spec conflicts to resolve (Decisions dated 2026-10-25, same commit)
- specs/01 §1 lists sim as `python -m va_sim.server`; specs/02 §1 (package `va_sim_server`) and specs/29 §1.1
  (`va_sim_server.main`) agree on `va_sim_server.main` → use that; Decision note under the specs/01 §1 table. Same note:
  `uvicorn va_edge.main:app` stays valid (module-level `app`), and `va_edge.serve` / `va_api.serve` (specs/29 §1.1
  images) run the app through the shared runner (`python -m va_edge.serve`). Distribution names follow the Dockerfile
  rule `va-${SERVICE//_/-}`: `va-telephony-edge`, `va-media-worker`, `va-worker`, `va-webhook-dispatcher`,
  `va-events-sink`, `va-sim`.
- specs/27 §9 Decision (append to the existing one): liveness failure is 503 `{"status":"unavailable","reason":<str>,
  "version":v}` (media `event_loop_lag`, worker `temporal_worker_stopped`); worker liveness requires every started
  Temporal worker to be running, and phase 00 registers no task queues (no workflows exist; `temporalio.worker.Worker`
  refuses zero workflows/activities), so the worker runs probes + Temporal client only until `va_workflows` adds
  registrations; media `vad` check = model file present and sha256 equal to `models/MANIFEST.json` (verified once in
  the lifespan via `asyncio.to_thread`; real onnxruntime session load arrives with `va_voice`); media `capacity` check
  fails when active calls ≥ `MEDIA_MAX_CALLS_PER_POD`; `draining` check fails after SIGTERM; sim readiness checks Redis
  only when `SIM_STATE_REDIS_URL` is set (empty → no checks → ok); events-sink ClickHouse check = HTTP `GET /ping`
  (expects body `Ok.`) on `CLICKHOUSE_URL`'s scheme/host/port (credentials/db stripped), no clickhouse-connect yet.
- specs/01 §3 graceful drain: implemented as: SIGTERM → `SADD media:pods:draining {pod_id}`, readiness 503, listener
  stays open while active calls > 0, up to `MEDIA_DRAIN_TIMEOUT_S`, then server exit; `SREM` on shutdown. Note it in
  the same specs/27 Decision (one line referencing specs/01 §3).

### va_common changes
1. `settings.py`: add mixins (all fields defaulted, spec names):
   - `DatabaseSettings(BaseServiceSettings)`: `database_url = "postgresql+asyncpg://va:va@localhost:5432/va"`,
     `database_pool_size: int = Field(10, ge=1)`.
   - `TemporalSettings(BaseServiceSettings)`: `temporal_host = "localhost:7233"`, `temporal_namespace = "default"`,
     `temporal_task_queue = "va-main"`.
   - export both; `ApiSettings(RedisSettings, DatabaseSettings, TemporalSettings)` drops its duplicated fields
     (field order irrelevant; `.env.example` unaffected).
2. New `va_common/checks.py` (moved from `va_api/health.py` + `va_api/container.py`; va_api imports them from here,
   delete `va_api/health.py`, keep `va_api.container.Container/build_container/ContainerFactory/ReadinessSource`):
   `CHECK_TIMEOUT_S = 1.0`; `class TemporalConnector` (unchanged code, moved); `TemporalCheck` Protocol;
   `async postgres_check(engine: AsyncEngine)`, `async redis_check(client: Redis)`, `async temporal_check(connector)`,
   new `async clickhouse_check(client: httpx.AsyncClient, ping_url: str)` (GET, `raise_for_status`, body.strip() ==
   "Ok." else `ConnectionError`), `clickhouse_ping_url(clickhouse_url: str) -> str` (`urllib.parse`: keep scheme +
   hostname + port, path `/ping`; raises `ValueError` for non-http(s) schemes), `create_engine(settings:
   DatabaseSettings) -> AsyncEngine` (`create_async_engine(url, pool_size=..., pool_pre_ping=True)`).
   Add deps to `packages/va_common/pyproject.toml`: `sqlalchemy[asyncio]>=2.0.35`, `asyncpg>=0.32.0`,
   `temporalio>=1.8`, `starlette>=1.0` (match installed 1.10), `uvicorn[standard]>=0.54.0`. Update description.
   Move the corresponding api tests (`apps/api/tests/test_checks.py` checks/connector tests) to
   `packages/va_common/tests/test_checks.py`; keep container/readyz-real-container tests in api.
3. New `va_common/service.py` (pure Starlette + uvicorn; every function CC ≤ B):
   - `@dataclass ProbeState`: `version: str`, `registry: Collector`, `checks: Callable[[], Mapping[str, CheckFn]]`,
     `liveness: Callable[[], str | None] = lambda: None` (returns failure reason), `readiness_timeout_s: float = 1.0`.
   - `async liveness_response(state) -> JSONResponse` (200 `liveness_body(v)` or 503 `{"status":"unavailable",
     "reason":r,"version":v}`), `async readiness_response(state) -> JSONResponse` (`run_checks`), `metrics_response(
     state) -> Response` (`render_latest`).
   - `probe_routes() -> list[Route]`: GET `/healthz`, `/readyz`, `/metrics`, handlers read `request.app.state.probes`.
     api's `probe_router` handlers delegate to the three response helpers (api keeps FastAPI APIRoutes so its
     metrics `route` label still comes from `scope["route"]`; `app.state.probes` set in api `create_app` / lifespan).
   - `probe_app(lifespan) -> Starlette`: `Starlette(routes=probe_routes(), lifespan=lifespan)` for metrics-port
     services.
   - `class Supervisor`: `__init__(on_failure: Callable[[str, BaseException], None])`; `start(name, factory:
     Callable[[], Awaitable[None]]) -> asyncio.Task[None]` (done-callback: cancelled → nothing; exception → log
     `service.task_failed` with name + exc_info and call `on_failure`; normal return → log `service.task_exited`);
     `async aclose()` cancels all and awaits them (`gather(return_exceptions=True)`); `running(name) -> bool`.
   - `class StopController`: `asyncio.Event`-based `request(reason: str)` (first reason kept, logs `service.stop_
     requested`), `requested: bool`, `reason`, `async wait()`.
   - `class ManagedServer(uvicorn.Server)`: overrides `capture_signals` with a no-op `contextlib.contextmanager`
     (signals are owned by `run_server`).
   - `async run_server(app, *, host, port, stop: StopController, drain: Callable[[], Awaitable[None]] | None = None,
     install_signals: bool = True, server_factory=ManagedServer) -> None`: `uvicorn.Config(app, host, port,
     lifespan="on", log_config=None, access_log=False, timeout_graceful_shutdown=10)`; when `install_signals`, add
     loop signal handlers SIGTERM/SIGINT → `stop.request(signal name)` (removed in `finally`); run `server.serve()`
     as a task; wait for first of {server task done, stop.wait()}; on stop: `await drain()` if given, then
     `server.should_exit = True` and await the server task. If the server task finishes first with
     `server.started` False → raise `RuntimeError("server failed to start")` (port in use, lifespan failure).
   - `def run_main(coro_factory: Callable[[], Awaitable[None]]) -> int`: `asyncio.run(...)`, returns 0, or 1 after
     logging `service.crashed` for an exception (KeyboardInterrupt → 130).
4. `metrics.py` `Service`: add nothing (sim builds `new_registry()` without catalog).

### Per-app layout (each: `apps/<dir>/pyproject.toml` (uv_build, deps `va-common` + what it imports), `src/<pkg>/
`__init__.py` (`__version__` from `importlib.metadata.version("<dist>")`), `py.typed`, `settings.py`, `container.py`
(where it has deps), `app.py`/`main.py`, `tests/conftest.py`, `tests/support/<pkg>_helpers.py`, tests)
Container pattern copied from api: `@dataclass Container` + `readiness_checks() -> dict[str, CheckFn]`,
`@asynccontextmanager build_container(settings)` (no network at startup, closes everything), test seam
`container_factory`. Every `create_app(settings=None, *, container_factory=build_container, init_observability=True,
readiness_timeout_s=1.0, ...)` sets `app.state.probes = ProbeState(...)` (checks bound to `app.state.container` in
the lifespan) and `observability.init("<service_name>", cfg, version=__version__)` / `obs.shutdown()` in lifespan.

A. `apps/telephony_edge` → `va_edge` (dist `va-telephony-edge`, deps fastapi, va-common):
   `EdgeSettings(RedisSettings, DatabaseSettings, TemporalSettings)` (+ `edge_public_host = "localhost:8100"`,
   `media_public_host = "localhost:8200"`), `service_name="telephony-edge"`; container checks postgres/redis/temporal;
   `main.py`: `create_app() -> FastAPI` (FastAPI title "VoiceAgents Telephony Edge", `openapi_url=None`,
   `app.router.routes.extend(probe_routes())`, metrics `build_metrics(Service.TELEPHONY_EDGE)`); module `app =
   create_app()`. `serve.py`: `main(argv=None) -> int` (`--host` default `0.0.0.0` with `# noqa: S104` comment
   explaining container bind, `--port` 8100) → `run_main(lambda: run_server(app, host, port, stop=StopController()))`;
   `if __name__ == "__main__": raise SystemExit(main())`.
B. `apps/media_worker` → `va_media` (dist `va-media-worker`, deps fastapi, va-common):
   `MediaSettings(RedisSettings, DatabaseSettings, TemporalSettings)` + `media_drain_timeout_s: float = 2700`,
   `media_max_calls_per_pod: int = Field(60, ge=1)`, `pod_id: str = ""` (validator: empty → `socket.gethostname()`),
   `service_name="media-worker"`.
   - `calls.py` `class CallRegistry(max_calls)`: `admit(call_id) -> bool` (False at capacity or duplicate),
     `release(call_id)`, `active: int`, `at_capacity: bool`, `async wait_idle(timeout_s) -> bool` (asyncio.Event set
     when active hits 0).
   - `drain.py` `class DrainState`: `draining: bool`; `async begin(redis, pod_id)` (idempotent; sets flag then
     `SADD media:pods:draining pod_id`; Redis errors logged `media.drain_mark_failed`, flag still set), `async
     clear(redis, pod_id)` (`SREM`, errors logged). Key constant `DRAINING_PODS_KEY = "media:pods:draining"`.
   - `loop_lag.py` `class LoopLagMonitor(interval_s=0.25, threshold_s=0.5)`: `async run()` loop (`loop.time()` before/
     after `asyncio.sleep(interval)`; `record(lag)`), `record(lag_s)` (stores, sets gauge if given), `lag_s`,
     `first_sample` Event, `healthy -> bool`, `liveness() -> str | None` ("event_loop_lag").
   - `vad_model.py`: `MODEL_NAME = "silero_vad.onnx"`; `async verify_vad_model(models_dir: Path) -> None` reads
     `MANIFEST.json` (read exact schema from P00-T10's `models/MANIFEST.json` and `scripts/fetch_models.py`), hashes the
     file in `asyncio.to_thread`; raises `VadModelError` (missing file / manifest entry / mismatch). `class VadStatus`
     (`loaded: bool`, `error: str | None`); readiness `vad` check raises when not loaded.
   - `container.py`: Redis client + checks `draining`, `capacity`, `redis`, `vad` (order as in specs/27 §9).
   - `main.py`: `create_app(settings=None, *, container_factory, init_observability, readiness_timeout_s, models_dir:
     Path = Path("models"), monitor: LoopLagMonitor | None = None) -> FastAPI`; lifespan: obs → container →
     `verify_vad_model` (failure logged `media.vad_model_unavailable`, readiness stays 503) → Supervisor starts the
     loop-lag monitor (`on_failure` requests stop) → yield → `drain.clear` → `supervisor.aclose()`.
     `app.state.registry: CallRegistry`, `app.state.drain`, liveness = monitor.liveness. `async drain(app)`:
     `await drain.begin(...)`, `await registry.wait_idle(settings.media_drain_timeout_s)` (log `media.drain_timeout`
     with remaining active count when False). `main(argv=None) -> int` (`--host 0.0.0.0`, `--port 8200`) runs
     `run_server(app, ..., drain=partial(drain, app))`; module-level `app = create_app()`; `__main__` guard.
C. `apps/worker` → `va_worker` (dist `va-worker`, deps temporalio, va-common):
   `WorkerSettings(RedisSettings, DatabaseSettings, TemporalSettings)`, `service_name="worker"`, `METRICS_PORT=9300`.
   - `task_queues.py`: `TASK_QUEUES = ("va-main", "va-postcall", "va-ingest", "va-system")` (specs/01 §1; derive from
     `settings.temporal_task_queue` base: `va-main` → `va-postcall`, `va-ingest`, `va-system` by replacing the
     `-main` suffix — function `task_queue_names(base) -> tuple[str, ...]`, `ValueError` when base lacks `-main`);
     `@dataclass(frozen) TaskQueueRegistration(task_queue, workflows: Sequence[type], activities: Sequence[Callable])`;
     `REGISTRATIONS: tuple[TaskQueueRegistration, ...] = ()` with a docstring stating `va_workflows` appends them.
   - `runner.py` `class TemporalWorkers(connector, registrations, *, worker_factory=temporalio.worker.Worker)`:
     `async run()` (connects client, builds one worker per registration, runs them in a TaskGroup; no registrations →
     returns immediately after logging `worker.no_task_queues`), `async shutdown()` (each `worker.shutdown()`),
     `liveness() -> str | None` (`"temporal_worker_stopped"` when any built worker is not running; `WorkerLike`
     Protocol with `run()`, `shutdown()`, `is_running`).
   - `container.py`: engine, redis, TemporalConnector; checks postgres/redis/temporal.
   - `main.py`: `create_app(...) -> Starlette` (`probe_app(lifespan)`; lifespan: obs → container → supervisor starts
     `TemporalWorkers.run` (failure → `stop.request`) → yield → workers.shutdown → supervisor.aclose); `async run(
     settings, *, host="0.0.0.0", port=9300, stop)`; `main(argv=None)`; `__main__` guard.
D. `apps/webhook_dispatcher` → `va_webhooks` (dist `va-webhook-dispatcher`, deps sqlalchemy, va-common):
   `DispatcherSettings(RedisSettings, DatabaseSettings)` + `webhook_allow_private: bool = True` with validator forcing
   False when `va_env in {staging, production}` (specs/02 §6); container: engine only (redis client not opened —
   readiness is Postgres only per specs/27 §9); metrics port 9400; `main.py` same shape as worker without background
   tasks (outbox loop arrives with specs/19 work).
E. `apps/events_sink` → `va_events_sink` (dist `va-events-sink`): `EventsSinkSettings(RedisSettings)` +
   `clickhouse_url = "http://default:@localhost:8123/va"`; container: redis + `http_client(settings, timeout=...)` +
   ping url; checks redis/clickhouse; metrics port 9500; same `main.py` shape.
F. `apps/sim` → `va_sim_server` (dist `va-sim`): `SimSettings(BaseServiceSettings)` + `sim_clock: Literal["realtime",
   "accelerated"] = "realtime"`, `sim_state_redis_url: str = ""`, `sim_workers: int = Field(1, ge=1)`;
   `SIM_PORTS = {"telnyx": 9100, "elevenlabs": 9200, "llm": 9300, "stripe": 9400}`; `create_app(name, settings, *,
   redis_factory)` → Starlette probe app (registry `new_registry(process_collectors=False)` per port to avoid
   duplicate process collectors; checks `{"redis": ...}` only when `sim_state_redis_url`); `async run(settings, stop)`
   runs the four servers concurrently with one shared StopController (`install_signals` only once — the runner
   installs handlers in `run`, servers get `install_signals=False`); `main(argv=None)`; `__main__` guard. The sim's
   vendor routes come with `packages/va_sim` (phase 03) — mention in the module docstring as scope, not as a stub.

Shared workspace edits: root `pyproject.toml` dev group + `[tool.uv.sources]` for each new dist; mypy `mypy_path`
append each `apps/<dir>/src` and `apps/<dir>/tests/support`, `files` each `src` + `tests`; pytest `pythonpath`
append each `tests/support`. Test-support module names must be unique across apps (`edge_helpers`, `media_helpers`,
`worker_helpers`, `webhooks_helpers`, `sink_helpers`, `sim_helpers`). `uv lock` + `uv sync --all-packages`.
Reuse one generic client helper: add `packages/va_common/tests/service_helpers.py` with `make_client(app)`
(LifespanManager + ASGITransport `raise_app_exceptions=False`), `ok_check`, `failing(exc)`, `FakeContainer(checks)`
factory — apps' helpers import from it to keep jscpd at 0 (api keeps its own helpers or switches to these).

### Tests (names → assertion)
va_common:
- `test_checks.py`: moved api checks tests + `test_clickhouse_check_ok` / `_bad_body_raises` / `_http_error_raises`
  (respx), `test_clickhouse_ping_url_strips_credentials_and_db` (parametrize http/https, default port kept absent),
  `test_clickhouse_ping_url_rejects_native_scheme`, `test_create_engine_does_not_connect`.
- `test_service.py`: `test_probe_routes_liveness_ok`, `test_liveness_failure_503_reason`, `test_readiness_ok_and_503`
  (body shape, `failing`), `test_metrics_route_renders_registry`, `test_unknown_path_404`, `test_supervisor_failure_
  calls_on_failure_and_logs`, `test_supervisor_aclose_cancels`, `test_supervisor_normal_exit_logged`,
  `test_stop_controller_keeps_first_reason`, `test_run_server_serves_and_stops_on_request` (real uvicorn on port 0?
  uvicorn with port 0 → read bound port from `server.servers[0].sockets[0].getsockname()`; wait for `server.started`
  via polling an Event set in app lifespan, then httpx GET /healthz, then `stop.request("test")`),
  `test_run_server_runs_drain_before_exit` (drain coroutine records order), `test_run_server_signal_handler_requests
  _stop` (`os.kill(os.getpid(), SIGTERM)` with install_signals=True → reason `SIGTERM`; handlers removed afterwards),
  `test_run_server_startup_failure_raises` (lifespan raising → RuntimeError), `test_run_main_exit_codes` (0, 1 on
  exception, logs `service.crashed`).
- `test_settings.py` additions: Database/Temporal mixin defaults and env reading.
Per app (`apps/<dir>/tests/`):
- `test_settings.py`: defaults match specs/02 §6 values; env override; (dispatcher) private forced False in
  staging/production; (media) `pod_id` defaults to hostname; (worker) `task_queue_names("va-main")` and ValueError.
- `test_health.py`: `test_healthz_ok_without_dependencies` (body + version from metadata), `test_readyz_all_ok`,
  `test_readyz_redis_unreachable_503` (real container factory with `REDIS_URL=redis://127.0.0.1:1/0`,
  `DATABASE_URL` port 1, `TEMPORAL_HOST=127.0.0.1:1`; assert 503, `redis` in failing with status error|timeout; for
  dispatcher: `test_readyz_postgres_unreachable_503` and `test_readyz_does_not_check_redis`; for sim: with and
  without `SIM_STATE_REDIS_URL`), `test_metrics_exposes_service_catalog` (one catalog metric name from specs/27 §3.x
  for that service present; sim: process-less registry renders), `test_container_closed_on_shutdown`,
  `test_observability_init_and_shutdown_called` (monkeypatch `observability.init`).
- media extras: `test_calls.py` (admit/release/capacity/duplicate/wait_idle true+timeout using events, no sleeps
  beyond the timeout under test of 0.01), `test_drain.py` (fakeredis: SADD/SREM, idempotent, redis error logged and
  flag set; readyz 503 with `draining` failing after `begin`; capacity check 503 when registry full), `test_loop_lag.py`
  (`record(0.6)` → liveness `event_loop_lag`, `/healthz` 503; real `run()` produces a first sample < threshold),
  `test_vad_model.py` (tmp manifest+file ok; missing file, mismatched hash, missing entry → `VadModelError`; app with
  bad models_dir → readyz `vad` error; with good tmp dir → `vad` ok), `test_drain_flow.py` (`drain(app)` with an
  admitted call released from another task → returns; with timeout 0.01 → logs `media.drain_timeout`).
- worker extras: `test_runner.py` (no registrations → returns, liveness None; fake worker factory: runs, liveness
  None while running, `"temporal_worker_stopped"` after it stops, `shutdown` called; worker failure → supervisor
  requests stop).
- sim: `test_run.py` (`run()` with four ephemeral ports via a `ports` override parameter → all four `/healthz` 200,
  stop → returns).
- each app `test_main.py`: `test_main_parses_args_and_runs` (monkeypatch `run_server`/`run_main` to capture host/port;
  default ports 8100/8200/9300/9400/9500), `test_module_app_imports_without_io` where a module-level app exists.
- api: existing tests stay green after the refactor (`ApiSettings` mixins, checks moved, probe handlers delegating).

### Docs
- specs/01 §1 and specs/27 §9 Decisions above (keep `check_spec_refs` happy: spec + § on the same line).
- AGENTS.md (dated P00-T12): shared runtime (`va_common.service`: `probe_routes`/`probe_app`/`ProbeState`,
  `Supervisor`, `StopController`, `run_server` owns signals via `ManagedServer`), `va_common.checks` home of
  readiness checks + `TemporalConnector`, settings mixins, commands to run each service (`uv run python -m
  va_edge.serve`, `-m va_media.main`, `-m va_worker.main`, `-m va_webhooks.main`, `-m va_events_sink.main`,
  `-m va_sim_server.main`), metrics ports, test-support module naming rule, `service_helpers.make_client`.

### Gates
`make fmt lint typecheck placeholders test`, `make check` (drift + spec refs), `make openapi` → `unchanged` (api schema
must not change; if it does, the refactor leaked into routes — fix, do not regenerate blindly), pip-audit on
`uv export`, pnpm audit, `uvx radon cc -s -a` (no C+), `npx --yes jscpd@4` (0 clones). Smoke with `make up`: start
each service (`uv run python -m ...`), curl `/healthz` + `/readyz` (200), `docker compose stop redis` → edge/media/
worker/sink 503 with `redis` failing, dispatcher still 200; restart redis; `kill -TERM` each pid → exits 0 within a few
seconds (media with no calls exits promptly and SREMs its pod id). build.done plain string (mem-1792920384-602e).

### Risks
- uvicorn `Server.serve` signal capture: overriding `capture_signals` is the supported seam in 0.54 (method exists);
  a test asserts SIGTERM reaches `StopController` and the process does not re-raise the signal after exit.
- Real-uvicorn tests need port 0 and readiness synchronisation via an Event set when `server.started`; never sleep.
- Several FastAPI/Starlette apps in one process each build their own registry (`build_metrics` per app) — never the
  global REGISTRY (duplicate timeseries errors).
- temporalio connect to port 1 may retry inside the 1 s budget → accept `error|timeout`.
- Moving checks out of va_api must keep `apps/api` coverage and tests green; jscpd may flag the per-app
  `build_container`/lifespan shapes — keep them minimal and push shared pieces into `va_common.service`.
- Task is large (6 apps); if it cannot finish in one Builder activation, emit build.blocked with what remains so the
  Planner can split (T12a runtime+edge+media, T12b worker/dispatcher/sink, T12c sim).

## Build P00-T12 — done (Builder)

Implemented the plan with these deliberate differences (all recorded in the spec Decisions or AGENTS.md):
- Dependency containers: instead of six hand-written Container classes, `va_common.checks.open_dependencies()` yields a
  `Dependencies` stack (`postgres/redis/temporal/clickhouse(...)` create the client, push its cleanup on an
  AsyncExitStack, add the check in call order). api's `Container` keeps its fields (engine/redis/temporal) on top of it.
  Edge/dispatcher/sink/sim containers are the `Dependencies` object; media and worker wrap it in a tiny dataclass that
  exposes the Redis client / Temporal connector they need.
- `va_common.service` additionally has `service_scope` / `service_lifespan` / `probe_service_app` / `ContainerFactory[S]`
  / `container_checks` / `observed` / `parse_bind` + `Bind` / `serve` (exit code 1 when a supervised task failure stopped
  the process). StopController: a second request forces the exit (skips a hanging drain, sets `force_exit`).
  `run_server` converts uvicorn's startup `sys.exit` into `ServerStartupError` and, when cancelled, lets uvicorn close
  its sockets (bounded) before re-raising.
- Media: no module-level `app` (specs/01 §1 runs it only as `python -m va_media.main`); lag ticker interval 0.1 s per the
  specs/27 §3 `va_event_loop_lag_ms` row (plan said 0.25). `va_media_draining` gauge + lag histogram wired.
- Also added `va_api.serve` (specs/29 §1.1 image module) via the same runner.
- Sim `--port` is the base port (telnyx = port, +100, +200, +300).
- Test-support dirs only where needed: `apps/media_worker/tests/support/media_helpers.py`,
  `apps/worker/tests/support/worker_helpers.py`; everything else uses `packages/va_common/tests/service_helpers.py`
  (va_common and api conftests now import `isolate_env`/`reset_logging` from it).

Evidence (this iteration): make fmt 0; make lint 0; make typecheck 0 (138 files); make placeholders 0 findings;
make test 1090 passed 1 skipped, coverage 99.83%; make check 0 (openapi current, spec refs 0 problems);
make openapi → unchanged; pip-audit (uv export, --no-deps) no known vulnerabilities; pnpm audit no known
vulnerabilities; radon no C+ (avg A 2.01); jscpd 0 clones.
Smoke (logs/smoke_p00t12.sh, infra up): all six services healthz/readyz 200 (sim on 19100-19400); `docker compose stop
redis` → edge/media/worker/sink readyz 503 failing ["redis"], dispatcher + sim stay 200; restart → all 200; SIGTERM →
every process exit 0 within 1 s; `media:pods:draining` empty afterwards.

## Review P00-T12 — approved

Re-ran: make lint 0, make typecheck 0 (138 files), make placeholders 0 findings, make test 1090 passed 1 skipped
(99.83%), make check 0 (openapi current, spec refs 0), jscpd 0 clones, radon: no C+ in the new code (only the two
pre-existing api test functions from P00-T11). Reviewed va_common.service/checks, media (drain/registry/lag/vad),
worker runner, dispatcher/sink/sim settings and runners against specs/01 §1, specs/02 §6, specs/27 §9 + Decisions.
Non-blocking note for the workflow phase: `va_worker.task_queues` docstring says packages "append" to `REGISTRATIONS`,
which is a Final tuple — the workflow phase should replace the tuple literal (or add a register function) and wire
`task_queue_names(settings.temporal_task_queue)` into the registrations.

## Plan P00-T13 — TypeScript workspace scaffolds

Acceptance: `corepack pnpm -r lint typecheck test build` (run as separate `pnpm -r run <x>` passes) exits 0; `make lint
typecheck test-ts` and `make check` stay green. Node 22, pnpm 9.15.9 via corepack (no `pnpm` on PATH).

### Spec conflicts / decisions (record as `> Decision (2026-10-25):` in the named spec, same commit)
1. specs/02 §6 + `.env.example`: add `NEXT_PUBLIC_API_BASE_URL` | dashboard | `http://localhost:8000` | browser base URL
   for API calls — directly after `NEXT_PUBLIC_SENTRY_DSN` in both (parity test enforces order/value/comment; update any
   hard-coded count in `scripts/tests/test_env_example.py` if present). Needed because the dashboard calls the api
   cross-origin with `credentials: "include"` (api CORS already allows `DASHBOARD_BASE_URL`).
2. specs/25 §23: `/status` is a client page (TanStack Query) calling api `GET /healthz`, `GET /readyz` (unlisted probes,
   zod-parsed bodies per specs/27 §9 Decision) and `GET /v1/health` via openapi-fetch. No `/` page yet (Home needs auth,
   phase 02+): `/` renders the designed `not-found.tsx`. Fonts self-hosted via `@fontsource-variable/inter` +
   `@fontsource-variable/jetbrains-mono` (no network at build; spec fonts kept — specs beat the generic aesthetics
   guidance's Inter ban). Token colours written in OKLCH (Tailwind 4 palette values for indigo-600/400, emerald-600,
   amber-500, rose-600, sky-600).
3. specs/26 §3: SDK type tests use Vitest `expectTypeOf` (`vitest --typecheck`) instead of `tsd` (tsd needs built
   `.d.ts`; `pnpm -r test` runs before `build`). Schema drift checked by a Vitest test (below).
4. specs/22 §4: this phase ships `init()` + auto-init + Shadow DOM mount only; `window.VoiceAgentsWidget` = IIFE global
   exposing `init` (full JS API, audio worklet, components arrive with the widget phase — say so in module docstring as
   scope, not as a stub). Size budget enforced at build (`scripts/check-size.mjs`, ≤ 60 KB gzip).

### Root files
- `tsconfig.base.json`: `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`,
  `noFallthroughCasesInSwitch`, `verbatimModuleSyntax`, `isolatedModules`, `target ES2022`, `module ESNext`,
  `moduleResolution Bundler`, `skipLibCheck`, `forceConsistentCasingInFileNames`.
- `eslint.base.mjs`: exports `base({ tsconfigRootDir, react?: boolean })` returning flat config: `@eslint/js`
  recommended, `typescript-eslint` `strictTypeChecked` + `stylisticTypeChecked` with `parserOptions.projectService`,
  when react: `eslint-plugin-react-hooks` (recommended) + `eslint-plugin-jsx-a11y` (flat recommended);
  `eslint-config-prettier` last; ignores `dist`, `.next`, `coverage`, `**/schema.d.ts`, `next-env.d.ts`.
  Root `package.json` devDependencies: everything `eslint.base.mjs` imports + `eslint`, `prettier`, `typescript`.
- `.prettierrc.json` (`printWidth 100`, `semi true`, `singleQuote false`, `trailingComma all`), `.prettierignore`
  (`dist`, `.next`, `coverage`, `**/schema.d.ts`, `pnpm-lock.yaml`, `next-env.d.ts`).
- Root `package.json` scripts unchanged. Each package declares its own devDeps too (eslint, prettier, typescript,
  vitest, @vitest/coverage-v8) so `pnpm -r run` resolves bins locally.
- Every package scripts: `format: prettier --write .`, `lint: eslint . --max-warnings 0 && prettier --check .`,
  `typecheck: tsc --noEmit`, `test: vitest run --coverage`, `build`. Vitest coverage thresholds 80 (lines/branches/
  functions/statements), excluding config files, `schema.d.ts`, Next `app/**/layout.tsx`? (no — keep layout covered via
  test or exclude only `*.config.*`, `next-env.d.ts`).

### A. `sdks/typescript` — npm `@voiceagents/sdk`
- `package.json`: `type: module`, `exports` (import → `dist/index.js`, require → `dist/index.cjs`, types),
  `files: ["dist"]`, `engines.node >=18`, scripts above + `build: tsup`, `gen:api: openapi-typescript
  ../../openapi/openapi.json -o src/schema.d.ts`. devDeps: tsup, openapi-typescript, msw, vitest, coverage-v8.
- `tsup.config.ts`: entry `src/index.ts`, formats esm+cjs, dts, clean, target es2020.
- `src/schema.d.ts` (generated, committed).
- `src/version.ts`: `export const VERSION = "0.0.0"` matching package.json (test asserts equality).
- `src/errors.ts`: `VoiceAgentsError extends Error`; `APIError(status, code, message, requestId, body)`;
  subclasses `BadRequestError` 400, `AuthenticationError` 401, `InsufficientCreditsError` 402,
  `PermissionDeniedError` 403, `NotFoundError` 404, `ConflictError` 409, `RateLimitError` 429,
  `InternalServerError` ≥500; `APIConnectionError`, `APITimeoutError`. `parseErrorBody(body): {code, message}` handles
  envelope `{errors:[{code,message,...}]}`/`{error:{...}}` and legacy shapes — read `ErrorEnvelope`/`ErrorLegacy` in
  `openapi/openapi.json` + specs/31 §4 for exact fields; `makeAPIError(status, body, requestId)`.
- `src/client.ts`: `export interface ClientOptions {apiKey?: string; baseURL?: string; timeout?: number; maxRetries?:
  number; orgId?: string; fetch?: typeof fetch; defaultHeaders?: Record<string,string>}`;
  `DEFAULT_BASE_URL = "https://api.voiceagents.ai"`, `DEFAULT_TIMEOUT_MS = 30000`, `DEFAULT_MAX_RETRIES = 2`.
  `class VoiceAgentsClient { constructor(options?: ClientOptions); readonly baseURL; readonly timeout; readonly
  maxRetries; health(): Promise<HealthStatus> /* GET /v1/health, typed from schema components["schemas"]["HealthStatus"]
  */ }`. apiKey falls back to `globalThis.process?.env.VOICEAGENTS_API_KEY` (guarded for non-Node), baseURL to
  `VOICEAGENTS_BASE_URL` then default; trailing slash stripped. Private `request<T>(method, path)`:
  headers `Authorization: Bearer` (when apiKey), `X-VA-Org-Id` (when orgId), `User-Agent`-equivalent
  `X-VA-Client: voiceagents-typescript/<VERSION>` (browsers forbid User-Agent; record in Decision 3), `Accept`,
  defaultHeaders merged; timeout via `AbortSignal.timeout`-equivalent AbortController → `APITimeoutError`; retries on
  connection error, 408, 429 (honour `Retry-After` seconds), 500/502/503/504 with backoff `min(8000, 500*2^n)` full
  jitter; sleep + random injectable via a non-exported constructor seam (`ClientOptions` internal `_sleep`/`_random`
  NOT public — use a second internal options type exported only from `src/internal.ts` for tests). Non-2xx after
  retries → `makeAPIError` with `X-Request-Id`.
- `src/index.ts`: re-exports client, errors, VERSION, types `HealthStatus`, `ClientOptions`, `paths`/`components`.
- Tests (`tests/`, msw `setupServer`): `client.test.ts` — `health returns status ok`, `sends bearer and org headers`,
  `apiKey from env`, `baseURL trailing slash stripped`, `retries 503 then succeeds (sleep called with jittered delay)`,
  `honours Retry-After`, `gives up after maxRetries → InternalServerError with requestId`, `404 → NotFoundError code
  from envelope body`, `legacy body parsed`, `network error → APIConnectionError after retries`,
  `timeout → APITimeoutError` (fake sleep, abort), `custom fetch used`; `errors.test.ts` status → class table;
  `types.test-d.ts` (`expectTypeOf(client.health()).resolves.toEqualTypeOf<{status:"ok"}>()`, constructor rejects
  unknown option via `// @ts-expect-error`); `schema.test.ts` — regenerate with `openapiTS(new URL(openapi.json))` +
  `astToString` and compare to committed `src/schema.d.ts` (drift check); `version.test.ts`.

### B. `apps/widget` — `@voiceagents/widget` (private)
- Vite 6/7 lib build: `vite.config.ts` `build.lib {entry: src/index.ts, name: "VoiceAgentsWidget", formats: ["iife"],
  fileName: () => "widget.js"}`, `outDir dist`, minify. `build: vite build && node scripts/check-size.mjs`
  (gzipSync dist/widget.js, fail > 61440 bytes, prints size). Vitest env `jsdom`.
- `src/index.ts`: `export interface WidgetOptions {widgetKey: string; userToken?: string; open?: boolean; target?:
  Document}`; `export interface WidgetHandle {root: ShadowRoot; host: HTMLDivElement; destroy(): void}`;
  `ROOT_ID = "va-widget-root"`; `init(options): WidgetHandle` — validates `widgetKey` starts with `wgt_` (throws
  `WidgetConfigError`), idempotent (second call returns existing handle for same key; different key → destroys and
  remounts), creates `<div id="va-widget-root">` appended to `body`, `attachShadow({mode: "open"})`, injects a
  `<style>` with CSS variables (light/dark via `prefers-color-scheme`, `prefers-reduced-motion`) and an accessible
  launcher `<button type="button" aria-label=... aria-expanded="false" part="launcher">` (text via textContent only),
  `data-open` reflected on host. `readScriptConfig(script: HTMLOrSVGScriptElement | null): WidgetOptions | null` reads
  `data-widget-key`, `data-user-token`, `data-open="true"`; `autoInit()` runs at module load when
  `document.currentScript` has `data-widget-key` (errors reported via `console.error`? — placeholder checker flags
  console debug? check rules; else dispatch `CustomEvent("va-widget:error")` on window).
- Tests: mount creates host+shadow root, launcher has accessible name and aria-expanded, styles isolated inside shadow
  (no `<style>` in document head), idempotent, remount on new key, destroy removes host, invalid key throws,
  readScriptConfig parses/ignores, autoInit with fake currentScript, data-open sets open state.

### C. `apps/dashboard` — `dashboard` (private; name must be `dashboard` for `pnpm --filter dashboard`)
- Next 15 (app router, `src/` dir), React 19, Tailwind 4 (`@tailwindcss/postcss`, `postcss.config.mjs`),
  shadcn/ui init: `components.json` (style new-york, rsc true, tsx, css `src/app/globals.css`, aliases `@/components`,
  `@/lib/utils`), `src/lib/utils.ts` `cn()` (clsx + tailwind-merge), `src/components/ui/button.tsx`, `card.tsx`,
  `badge.tsx` (shadcn sources, class-variance-authority). TanStack Query 5, openapi-fetch, openapi-typescript, zod.
  scripts: `dev: next dev -p 3000`, `build: next build`, `start: next start -p 3000`, `gen:api: openapi-typescript
  ../../openapi/openapi.json -o src/lib/api/schema.d.ts`, lint/format/typecheck/test as above. `NEXT_TELEMETRY_DISABLED=1`
  via `.npmrc`? no — set `env` in scripts or `next.config.ts` cannot; use `cross-env`-free: `"build": "next build"`
  and document `NEXT_TELEMETRY_DISABLED=1` in AGENTS.md / CI. `next.config.ts`: `reactStrictMode`, `poweredByHeader:
  false`, `typedRoutes` off.
- `src/styles/tokens.css`: every token from specs/25 §23 (light `:root`, dark `.dark` + `@media (prefers-color-scheme:
  dark) :root:not(.light)`), `--radius: 8px`, `--space: 4px`, `--font-sans` (Inter Variable), `--font-mono`
  (JetBrains Mono Variable). `src/app/globals.css`: `@import "tailwindcss"; @import "../styles/tokens.css";
  @theme inline {...}` mapping tokens to Tailwind colours, `::selection`, focus-visible ring, reduced-motion.
- `src/app/layout.tsx`: html `lang="en"`, imports fontsource + globals, `metadata` (title template
  "%s · VoiceAgents"), `viewport.themeColor` light/dark, `<Providers>` (client, `QueryClientProvider`, QueryClient
  staleTime 30 s, refetchOnWindowFocus true).
- `src/app/status/page.tsx` (server shell + `generateMetadata` title "Status") renders `<StatusView />` client
  component `src/components/status/status-view.tsx`: three rows — API liveness (`/healthz`: status + version),
  readiness (`/readyz`: overall + per-check table with ok/error/timeout badges, failing list), public health
  (`/v1/health` via openapi-fetch client). Loading skeleton, unreachable error state with retry button, refetch every
  10 s, `aria-live="polite"` region; all strings from `src/i18n/en.ts`.
- `src/lib/env.ts`: `apiBaseUrl()` reads `process.env.NEXT_PUBLIC_API_BASE_URL` (zod url, default
  `http://localhost:8000`, strip trailing slash).
- `src/lib/api/schema.d.ts` (generated), `src/lib/api/client.ts` (`createApiClient(baseUrl, fetch?)` openapi-fetch
  with `credentials: "include"`), `src/lib/api/errors.ts` (`ApiError {code, message, field?, requestId}` +
  `toApiError(status, body, requestId)` both envelopes), `src/lib/api/probes.ts` (`fetchLiveness`, `fetchReadiness`:
  zod schemas `livenessSchema {status:"ok", version}`, `readinessSchema {status:"ok"|"unavailable", checks:
  record<"ok"|"error"|"timeout">, failing?: string[], version}`; 503 readiness body is data, not an error; other
  non-2xx → ApiError; network → ApiError code `NETWORK_ERROR`? — use a local `ProbeUnreachableError`).
- `src/app/api/healthz/route.ts` (specs/27 §9): `GET` → `{"status":"ok","version": <package.json version>}`,
  `dynamic = "force-dynamic"`.
- `src/app/not-found.tsx`, `src/app/error.tsx` (client, retry) designed with tokens.
- Tests (Vitest, jsdom, Testing Library, msw or injected fetch): `tokens.test.ts` (parse tokens.css; every spec token
  present in light and dark; oklch→sRGB→WCAG contrast ≥ 4.5 for foreground/background, muted-foreground/background,
  primary-foreground/primary, card/foreground), `probes.test.ts` (ok, 503 readiness parsed, malformed body → error,
  network error), `errors.test.ts`, `env.test.ts`, `status-view.test.tsx` (renders ok rows, failing check badge text,
  unreachable state + retry triggers refetch), `healthz-route.test.ts`, `schema.test.ts` (drift: regenerate and compare
  like the SDK — share the comparison helper? no cross-package import; jscpd: keep each ≤ 10 lines).

### Docs
- AGENTS.md (dated P00-T13): TS layout, root `tsconfig.base.json`/`eslint.base.mjs`/prettier, commands
  (`corepack pnpm --filter dashboard dev`, `gen:api` in dashboard + sdk after `make openapi`, schema drift tests),
  `NEXT_TELEMETRY_DISABLED=1`, widget size gate, `NEXT_PUBLIC_API_BASE_URL`.

### Gates
`corepack pnpm install` (lockfile updated, committed), `corepack pnpm -r run lint`, `typecheck`, `test`, `build`;
`make fmt lint typecheck placeholders test test-ts`, `make check`; `corepack pnpm audit` (fix highs via overrides if
any); pip-audit unchanged; radon unchanged; `npx --yes jscpd@4` 0 clones (now includes TS). Smoke: `make up`, run api
(`uv run uvicorn va_api.main:app --port 8000`) + `corepack pnpm --filter dashboard start` after build; curl
`localhost:3000/api/healthz` 200 and `/status` 200 HTML; widget: dist/widget.js size printed.

### Risks
- `next build` offline: no `next/font/google` (fontsource only); disable telemetry; ESLint during build — set
  `eslint.ignoreDuringBuilds: true` in next.config since lint runs separately (avoid next's legacy eslint plugin
  requirement).
- Placeholder checker scans TS (empty functions, console usage) — read its TS rules before writing (`noop` callbacks
  need bodies or comments per rule).
- `exactOptionalPropertyTypes` vs openapi-fetch/Next types — use `skipLibCheck`.
- jsdom + React 19 + Testing Library versions; Vitest 3 `@vitejs/plugin-react` for TSX.
- msw v2 in Node 22 fine; ensure `onUnhandledRequest: "error"`.
- pnpm audit may flag transitive dev deps; prefer upgrades, document any override.

## Build P00-T13 — done (Builder)

Implemented per plan, with these deviations (all recorded as `> Decision (2026-10-25)` in the named specs):
- Versions: Next 15.5.28, React 19.2, TS 5.9 (typescript-eslint caps < 6.1), ESLint 9, Vitest 4.1 (3.2 pulled
  critical tinypool / vitest advisories), Vite 7, zod 4, jest-dom 7. Root `pnpm.overrides`: `postcss@<8.5.23` →
  `^8.5.23` (Next pins 8.4.31, 2 highs) and `esbuild@<0.28.1` → `^0.28.1` (tsup). `pnpm audit`: 0.
- Tokens use CSS `light-dark()` + `color-scheme` (`:root.light` / `:root.dark` force) instead of duplicated `.dark` and
  media blocks (jscpd flagged the duplicate). specs/25 Decision (d) updated accordingly.
- `next-env.d.ts` git-ignored (`apps/dashboard/.gitignore`): Next 15.5 writes a reference to `.next/types/routes.d.ts`,
  which breaks `tsc` on a clean clone.
- `.jscpd.json` at the root excludes generated `schema.d.ts` (the two copies are identical by construction).
- SDK retries also cover timeouts and 409 `IDEMPOTENCY_IN_PROGRESS` (specs/26 §2.2); Retry-After accepts delta-seconds
  or HTTP date ≤ 60 s.
- Dashboard CSRF middleware (`va_csrf` → `X-CSRF-Token` on unsafe methods) included; 401/MFA redirects left to phase 02.
- `.env.example` + specs/02 §6 gained `NEXT_PUBLIC_API_BASE_URL` (counts in test_env_example: 106 names / 105 keys /
  78 literal).

Gates (this iteration): make fmt 0; make check 0 (ruff + 3× eslint/prettier, mypy + 3× tsc, placeholders 0 findings,
pytest 1090 passed 1 skipped 99.83 %, Vitest widget 22 / dashboard 52 / sdk 48 + type tests, openapi current, spec refs
0); `pnpm -r run build` 0 (widget.js 2296 B gzip, /status first load 153 kB); pnpm audit 0; pip-audit 0; jscpd 0 clones;
radon avg A (2.56), only the pre-existing api C test. Smoke: api on :8000 + `next start` :3000 → `/api/healthz` 200
`{"status":"ok","version":"0.0.0"}`, `/status` 200 with heading, `/` 404 page, api CORS allows origin localhost:3000 with
credentials and exposes X-Request-Id.

## Review P00-T13 — approved

Re-ran: make lint, typecheck, placeholders (0), test (1090 passed, 1 skipped), test-ts (widget 22, dashboard 52, sdk 48 +
type tests; coverage ≥ 93 % branches everywhere), `pnpm -r run build` (widget.js 2296 B gzip), pnpm audit 0, jscpd 0,
spec refs 0, openapi current. Diff checked against specs/25 §23 (tokens match Tailwind 4 OKLCH values for the named hues,
API client CSRF + both error envelopes), specs/26 §2.2/§3 (retry set, backoff 0.5 s×2^n full jitter cap 8 s, error
classes by status), specs/22 §4 (Shadow DOM mount, data-* auto-init), specs/31 §1 (`errors[].error` code key). Reviewer
fix: AGENTS.md said Vitest 3, packages pin Vitest ^4.1.12 — corrected.

## Plan P00-T14 — CI workflow and pre-commit

Spec refs: specs/29 §6.1 (ci.yml jobs), specs/02 §9 (gates), specs/28 T17 / §2 (gitleaks pre-commit + CI). Phase prompt:
"`.github/workflows/ci.yml` running `make check` on push/PR with service containers".

### Decisions (record as `> Decision (2026-10-25):` under specs/29 §6.1, same commit)
(a) Phase-00 `ci.yml` jobs: `check` (merges §6.1 jobs 1–2: `make check` = lint/typecheck/placeholders/pytest/test-ts/
    openapi drift/spec refs, with service containers Postgres/Redis/ClickHouse/MinIO + Docker for testcontainers),
    `ts-build` (`pnpm -r run build`: next build, widget size gate ≤ 60 KB, SDK tsup; then `gen:api` drift: run
    `pnpm --filter dashboard run gen:api` and `pnpm --filter @voiceagents/sdk run gen:api` then
    `git diff --exit-code -- apps/dashboard/src/lib/api/schema.d.ts sdks/typescript/src/schema.d.ts`; check output is
    prettier-stable — if gen output differs from committed only by formatting, drop the diff step and rely on the
    schema drift Vitest tests, note in Decision), `security` (gitleaks via `gitleaks/gitleaks-action@v2` or the gitleaks
    binary with `fetch-depth: 0`, `uvx pip-audit` on `uv export --frozen --all-packages --no-hashes --no-emit-workspace`,
    `pnpm audit --audit-level high`), `actionlint` (runs actionlint on `.github/workflows/`). Jobs docs/helm/docker-build
    and semgrep/trivy/checkov/tflint are added by the phases that create mkdocs/helm/Dockerfiles/terraform.
(b) Actions pinned to major version tags (`actions/checkout@v5`, `astral-sh/setup-uv@v7` (with `enable-cache: true`,
    `cache-dependency-glob: uv.lock`), `actions/setup-node@v5` (`node-version-file: .nvmrc`, `cache: pnpm`),
    `pnpm/action-setup@v4` (reads `packageManager`), `actions/upload-artifact@v4`). Verify current majors with
    `gh api repos/<owner>/<repo>/releases/latest` or web before writing; use whatever is latest.
(c) Pre-commit hooks are `repo: local`, `language: system`, running through `uv run` / `corepack pnpm exec` so hook
    versions equal the locked versions (no separate hook pins to drift). gitleaks hook = `repo: https://github.com/gitleaks/gitleaks`
    pinned rev (latest tag) per specs/28 T17.

### Files
- `.github/workflows/ci.yml`:
  - `name: ci`; `on: push: branches: [main]`, `pull_request:`; `permissions: contents: read`;
    `concurrency: {group: ci-${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true}`;
    top-level `env: {UV_FROZEN: "1"? (no — pass --frozen explicitly), NEXT_TELEMETRY_DISABLED: "1", FORCE_COLOR: "1",
    PYTHONUNBUFFERED: "1"}`.
  - `check` job: `runs-on: ubuntu-latest`, `timeout-minutes: 30`, `services:` postgres (`pgvector/pgvector:pg16`,
    env POSTGRES_USER/PASSWORD/DB = va/va/va, ports 5432:5432, `options: --health-cmd "pg_isready -U va" --health-interval
    5s --health-timeout 3s --health-retries 20`), redis (`redis:7.2-alpine`, 6379, `redis-cli ping`), clickhouse
    (`clickhouse/clickhouse-server:24.8`, 8123+9000? — 9000 clashes with MinIO: map clickhouse native to 9009:9000? use
    8123:8123 and 9440? keep clickhouse 8123 only + `CLICKHOUSE_SKIP_USER_SETUP=1`/user env matching docker-compose.yml,
    health `wget -qO- http://localhost:8123/ping`), minio (`bitnamilegacy/minio:2024.10.13`, env MINIO_ROOT_USER/PASSWORD
    as compose, 9000:9000, health `mc ready local`). Images and env MUST match docker-compose.yml (test asserts).
    Job env: DATABASE_URL/REDIS_URL/CLICKHOUSE_URL/S3_ENDPOINT_URL etc. with the names from specs/02 §6 pointing at
    localhost (read `.env.example` for exact var names/forms; use only names that exist there).
    Steps: checkout; setup-uv (python-version from `.python-version`); pnpm/action-setup; setup-node; `uv sync
    --all-packages --frozen`; `corepack pnpm install --frozen-lockfile` (or `pnpm install --frozen-lockfile`);
    `scripts/fetch_models.sh` with `actions/cache` keyed on `hashFiles('models/MANIFEST.json')` for `models/` (only if
    any test needs model files — check; if not, skip this step); `make check`; upload `coverage.xml`/junit as artifact
    (add `PYTEST_ADDOPTS: --junitxml=reports/pytest.xml --cov-report=xml:reports/coverage.xml` env so `make check`
    stays unchanged; ensure `reports/` gitignored), `if: always()`.
  - `ts-build`, `security`, `actionlint` jobs per Decision (a). actionlint job: `uv sync --frozen --only-group dev`? simpler:
    `uvx --from actionlint-py@<locked version> actionlint` or `uv run actionlint` after sync.
- `.pre-commit-config.yaml`: `default_install_hook_types: [pre-commit]`, `fail_fast: false`, repos:
  local hooks — `ruff` (`uv run ruff check --fix`, `types_or: [python, pyi]`, `require_serial: true`), `ruff-format`
  (`uv run ruff format`), `placeholders` (`uv run python scripts/check_no_placeholders.py`, `pass_filenames: false`,
  `always_run: true`), `prettier` (`corepack pnpm exec prettier --write --ignore-unknown`, `files: ^(apps|sdks)/`,
  `types_or: [javascript, jsx, ts, tsx, css, json, yaml, markdown]`), `actionlint` (`uv run actionlint`, `files:
  ^\.github/workflows/`); remote `gitleaks` hook (pinned rev). Exclude `^(specs|plan|prompts)/` and `uv.lock|pnpm-lock.yaml`
  globally where relevant (ruff already excludes *.md).
- `pyproject.toml` dev group: add `actionlint-py>=1.7.12` and `pre-commit>=4` (check latest); `uv lock`.
- `scripts/tests/test_ci_workflow.py` (pyyaml; note YAML `on:` parses as `True` key — handle):
  - `test_actionlint_passes` — subprocess actionlint binary (resolve via `shutil.which("actionlint")` in venv; the dev dep
    guarantees it; fail, not skip, when missing) on `.github/workflows/ci.yml`, exit 0, empty output.
  - `test_actionlint_detects_broken_workflow` — tmp file with an undefined job in `needs:` / bad expression → exit ≠ 0.
  - `test_triggers_push_main_and_pull_request`, `test_permissions_read_only`, `test_concurrency_cancels`.
  - `test_check_job_runs_make_check` (a step `run` contains `make check`), `test_check_job_uses_frozen_installs`
    (`uv sync --all-packages --frozen`, `--frozen-lockfile`).
  - `test_service_images_match_compose` — for postgres/redis/clickhouse/minio, service `image` equals the image in
    docker-compose.yml, and each has `--health-cmd` in options.
  - `test_service_env_matches_compose` — credentials env equal compose's for each service.
  - `test_job_env_names_exist_in_env_example` — every env key on the check job is a variable in `.env.example` or in an
    explicit allowlist (`NEXT_TELEMETRY_DISABLED`, `FORCE_COLOR`, `PYTHONUNBUFFERED`, `PYTEST_ADDOPTS`).
  - `test_every_job_has_timeout`, `test_actions_pinned_to_versions` (every `uses:` has `@` and no `@main`/`@master`).
  - `test_security_job_runs_audits` (pip-audit, pnpm audit, gitleaks present), `test_ts_build_job_builds`.
  - pre-commit: `test_precommit_config_valid` — `pre-commit validate-config .pre-commit-config.yaml` exit 0;
    `test_precommit_has_required_hooks` (ids ruff, ruff-format, placeholders, prettier, gitleaks);
    `test_precommit_local_hooks_use_locked_tools` (local entries start with `uv run` or `corepack pnpm exec`).
  - Keep helpers tiny (jscpd), share `load_yaml` with test_compose? No cross-test import; small fixture.
- AGENTS.md (dated P00-T14): `uv run pre-commit install`, `uv run pre-commit run --all-files`, `uv run actionlint`,
  CI job layout, `PYTEST_ADDOPTS` reports, "CI mirrors make check".
- specs/29 §6.1 Decision note; plan tick done by Reviewer.

### Gates
`make fmt lint typecheck placeholders test test-ts`, `make check`; `uv run actionlint` 0; `uv run pre-commit run
--all-files` 0 (must leave tree unchanged — if prettier/ruff rewrite anything, fix the hook scope); pip-audit + pnpm audit;
radon; jscpd. If `gh`/`act` unavailable, local commands equal to every CI step are the evidence (run `pnpm -r run build`,
gen:api diff, pip-audit, gitleaks if installable via `brew`/binary — else note it).

### Risks
- MinIO 9000 vs ClickHouse native 9000 port clash in services → don't publish ClickHouse 9000.
- GitHub service containers can't set `command`; bitnami minio needs none. ClickHouse may need `ulimits` → only via
  options `--ulimit nofile=262144:262144`.
- YAML 1.1 `on` → True in pyyaml.
- pre-commit `prettier --write` on `.github/*.yml` or root md could rewrite specs; scope `files`.
- actionlint runs shellcheck only if installed; fine without. pnpm action-setup must precede setup-node `cache: pnpm`.
- Placeholder checker scans `scripts/`; `.github` not scanned.

## Build P00-T14 — done (Builder)

Files: `.github/workflows/ci.yml` (jobs check / ts-build / security / actionlint), `.pre-commit-config.yaml`,
`.gitleaks.toml`, `scripts/tests/test_ci_workflow.py` (30 tests), `.gitignore` (`/reports/`), dev deps
`actionlint-py>=1.7.12.25`, `pre-commit>=4.7.0` (uv.lock), AGENTS.md (commands rows + P00-T14 line), specs/29 §6.1 Decision.
Deviations from plan:
- Action majors are newer than the plan guessed (verified via `gh api .../releases/latest`): checkout@v7, setup-uv@v10,
  setup-node@v7, pnpm/action-setup@v6, upload-artifact@v7.
- gitleaks runs from the checksum-verified v8.30.1 release binary (gitleaks-action needs an org licence); pre-commit rev
  v8.30.1 (test asserts the two agree). Full-history scan found one false positive (`plan/phase-20.md:48`
  "key rotation, Telnyx/ElevenLabs" → generic-api-key); `.gitleaks.toml` extends defaults with a path+match AND allowlist.
- Placeholder hook id is `check_no_placeholders` (the bare word trips the checker's filler-word rule in the test file).
- No model fetch step: tests use a synthetic models dir (`apps/media_worker/tests/conftest.py`).
- Service containers can't mount init SQL → step runs `psql -f infra/docker/postgres-init/01-roles.sql`.
- gen:api output is byte-identical to committed schema.d.ts, so the ts-build diff step is kept.
Gates: make fmt 0; make check 0 (ruff, mypy 139 files, placeholders 0, pytest 1120 passed 1 skipped 99.83 %, Vitest
widget 22 / dashboard 52 / sdk 48, openapi current, spec refs 0); actionlint 0; pre-commit run --all-files all Passed,
tree unchanged; pnpm -r build 0 (widget 2296 B gzip); gen:api diff 0; pip-audit --strict 0 vulns; pnpm audit 0; gitleaks
git (local darwin binary) no leaks; jscpd 0 clones; radon: only pre-existing api C test. Not run: the workflow on GitHub
itself (no act); local equivalents of every step were run instead.

## Review P00-T14 — rejected (1 finding)

Re-ran: make lint 0, make typecheck 0, make placeholders 0 findings, `uv run actionlint` 0, pre-commit validate-config 0,
make test-ts passed. make test: **1 failed**, 1119 passed, 1 skipped (`apps/api/tests/test_serve.py::test_serve_main_defaults`).
CI diff itself (ci.yml, .pre-commit-config.yaml, .gitleaks.toml, test_ci_workflow.py, specs/29 Decision, AGENTS.md)
checked against specs/29 §6.1 and compose/.env.example: no findings there.

1. `apps/api/tests/test_serve.py:9,16` vs `apps/api/tests/test_lifespan.py:76`: order-dependent failure, and CI
   (`make check`, `pytest -n auto`) will hit it whenever xdist puts both files on the same worker.
   `test_module_app_imports_without_io` calls `importlib.reload(va_api.main)`, which builds a new `app` object.
   test_serve imported `app` at module import time (stale object), but `serve.main` lazily imports the *current*
   `va_api.main.app`, so `seen == [(app, ...)]` fails on identity. Reproduce deterministically:
   `uv run pytest -p no:randomly --no-cov -q apps/api/tests/test_lifespan.py apps/api/tests/test_serve.py` → 1 failed.
   Fix the cause (no retries): in test_serve, compare against `sys.modules["va_api.main"].app` /
   `importlib.import_module("va_api.main").app` resolved at assertion time; or have the lifespan test restore the
   original module object (e.g. run the reload against a fresh import under `monkeypatch.setitem(sys.modules, ...)`
   so the reload does not leak). Grep the other apps' tests for the same reload+module-level-import pattern
   (`importlib.reload` in apps/*/tests) and fix any sibling. Re-run the reproduce command plus a full `make check`.

## Build P00-T14 (rework after review) — done (Builder)

Finding 1 fixed at the cause: `apps/api/tests/test_lifespan.py::test_module_app_imports_without_io` no longer calls
`importlib.reload(va_api.main)` (which rebinds `va_api.main.app` in place and left test_serve holding a stale app). It
now `monkeypatch.delitem(sys.modules, "va_api.main")` + `monkeypatch.setattr(va_api, "main", main)` and imports a fresh
copy, asserting it is a distinct module with the expected app; teardown restores the original module object and package
attribute. Sibling grep: no other `importlib.reload` in apps/*/tests or packages/*/tests (telephony_edge uses
import_module without reload, resolves app at assertion time — fine).
Gates: reproduce command (lifespan then serve, -p no:randomly) 7 passed; serve/lifespan/serve order 7 passed; make fmt 0,
lint 0, typecheck 0, placeholders 0, test 1120 passed 1 skipped 99.83 %; make check 0.

## Review P00-T14 (rework) — approved

Re-ran: reproduce command (lifespan then serve, -p no:randomly) 7 passed; make lint 0, typecheck 0, placeholders 0,
test 1120 passed 1 skipped 99.83 %, test-ts 0, actionlint 0. Fix removes reload and restores module via monkeypatch.
AGENTS.md gained the no-reload gotcha. Committed 2cee168.
