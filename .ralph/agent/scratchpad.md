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
