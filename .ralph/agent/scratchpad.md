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
