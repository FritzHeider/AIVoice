
## Plan P00-T01 — Root workspace and toolchain configuration

Context (2026-10-25, Planner): repo contains only specs/, prompts/, plan/, scripts/ralph/*.sh, README/AGENTS/MASTER_PROMPT/ralph.yml.
No Python or TS code exists yet. Host: uv 0.12.17, python3.12 (homebrew), node v22.23.4 (nvm), **pnpm NOT on PATH**, corepack 0.36
present (without `packageManager` it fetches pnpm 12 — so `packageManager` pin is mandatory). Latest pnpm 9.x = 9.15.9.

### Files to create
1. `/Users/drop/AIVOICE/.python-version` → `3.12`
2. `/Users/drop/AIVOICE/.nvmrc` → `22`
3. `/Users/drop/AIVOICE/.editorconfig` → root=true; `[*]` utf-8, lf, insert_final_newline, trim_trailing_whitespace,
   indent_style=space, indent_size=2; `[*.py]` indent_size=4; `[Makefile]` indent_style=tab; `[*.md]` trim_trailing_whitespace=false.
4. `/Users/drop/AIVOICE/.gitignore` → Python (`.venv/`, `__pycache__/`, `*.py[cod]`, `.mypy_cache/`, `.ruff_cache/`,
   `.pytest_cache/`, `.coverage`, `.coverage.*`, `coverage.xml`, `htmlcov/`, `*.egg-info/`, `.hypothesis/`), Node (`node_modules/`,
   `.next/`, `out/`, `dist/`, `build/`, `*.tsbuildinfo`, `coverage/`, `playwright-report/`, `test-results/`), env
   (`.env`, `.env.*` + `!.env.example`), local runtime (`.sim/`, `logs/`, `.DS_Store`), Ralph runtime:
   `.ralph/*` then `!.ralph/agent/` then `.ralph/agent/*.lock` (keeps scratchpad/decisions/memories/tasks, drops locks,
   events, diagnostics, history). `models/*.onnx` is NOT ignored here (T10 decides; manifest committed).
5. `/Users/drop/AIVOICE/pyproject.toml` (uv workspace root, dev tools only):
   - `[project] name="voiceagents-workspace" version="0.0.0" requires-python=">=3.12,<3.13" dependencies=[]`
     + `[tool.uv] package = false` (virtual root; avoids uv "no requires-python" warnings with dependency-groups).
   - `[tool.uv.workspace] members = ["packages/*", "apps/*", "sdks/python", "sdks/cli"]`,
     `exclude = ["apps/dashboard", "apps/widget", "apps/docs"]` (Node/MkDocs dirs must never be uv members).
   - `[dependency-groups] dev = [...]` exactly specs/02 §2: pytest, pytest-asyncio, pytest-cov, pytest-xdist, pytest-timeout,
     respx, hypothesis, freezegun, "testcontainers[postgres,redis,clickhouse,minio]", mypy, ruff, types-python-dateutil,
     asgi-lifespan, syrupy. Use lower bounds only (`>=` current versions); uv.lock pins exactly.
   - `[tool.ruff]` line-length=110, target-version="py312", `extend-exclude = ["node_modules", ".venv", "dist", "build", ".next"]`.
     `[tool.ruff.lint] select = ["E","F","W","I","N","UP","B","A","C4","DTZ","T20","RET","SIM","TID","ARG","PTH","ERA","PL","TRY","RUF","ASYNC","S"]`.
     `[tool.ruff.lint.per-file-ignores]`: `"**/tests/**" = ["S101","PLR2004","ARG001"]`, `"scripts/**" = ["T20","S603","S607"]`,
     `"apps/*/src/*/api/**" = ["PLR0913"]` and `"apps/*/src/*/routers/**" = ["PLR0913"]` (FastAPI endpoint exemption per §4).
     `[tool.ruff.lint.isort] known-first-party = ["va_*"]` is not valid glob → omit; rely on src detection:
     `[tool.ruff] src = ["scripts", "packages/*/src", "apps/*/src", "sdks/python/src", "sdks/cli/src"]`.
   - `[tool.mypy] python_version="3.12" strict=true warn_unreachable=true explicit_package_bases=true
     mypy_path="scripts" files=["scripts"] exclude=['(^|/)node_modules/', '(^|/)\.venv/', '(^|/)scripts/ralph/']`.
     (Later tasks append `packages/*/src`, `apps/*/src` to `mypy_path`/`files` as they create them; note this in AGENTS.md.)
   - `[tool.pytest.ini_options] asyncio_mode="auto" asyncio_default_fixture_loop_scope="function"
     addopts=["--import-mode=importlib","--strict-markers","--strict-config"] testpaths=["scripts/tests","packages","apps","sdks"]
     pythonpath=["scripts"] timeout=60 markers=["integration: needs docker/testcontainers","e2e: simulator end-to-end","live: real vendors, LIVE_TESTS=1"]
     filterwarnings=["error"]` — verify missing testpaths entries don't error; if pytest complains, list only existing ones and note it.
   - `[tool.coverage.run] branch=true source=["scripts","packages","apps","sdks"] omit=["*/tests/*","scripts/ralph/*","*/node_modules/*"]`
     `[tool.coverage.report] show_missing=true skip_covered=false exclude_also=["if TYPE_CHECKING:","if __name__ == .__main__.:"]`.
     If non-existent source dirs produce coverage warnings that `filterwarnings=error` or CI treat as failures, trim to existing dirs.
6. `/Users/drop/AIVOICE/scripts/check_toolchain.py` — real preflight for specs/02 §2 (gives mypy/pytest/coverage real
   code on an otherwise empty tree; `make setup` runs it first). Public API:
   ```python
   Runner = Callable[[Sequence[str]], str | None]   # returns stdout+stderr text, None if binary missing
   @dataclass(frozen=True) class Requirement: name: str; command: tuple[str, ...]; minimum: tuple[int, ...]; below: tuple[int, ...] | None; required: bool = True
   @dataclass(frozen=True) class CheckResult: name: str; ok: bool; found: str | None; message: str
   REQUIREMENTS: tuple[Requirement, ...]  # python (sys.version_info, not subprocess) >=3.12,<3.13; uv >=0.5; node >=22,<23;
                                          # pnpm >=9,<10 (command: ("corepack","pnpm","--version") fallback when "pnpm" missing);
                                          # docker >=27 (required=False: needed for `make up`, not setup); docker compose >=2 (required=False)
   def parse_version(text: str) -> tuple[int, ...] | None  # first r"v?(\d+)\.(\d+)(?:\.(\d+))?" match
   def evaluate(req: Requirement, output: str | None) -> CheckResult
   def subprocess_runner(cmd: Sequence[str]) -> str | None  # subprocess.run(list(cmd), capture_output=True, text=True, timeout=15, check=False); FileNotFoundError/TimeoutExpired -> None
   def run_checks(runner: Runner = subprocess_runner, python_version: tuple[int, int, int] = sys.version_info[:3]) -> list[CheckResult]
   def main(argv: Sequence[str] | None = None) -> int  # prints "ok   uv 0.12.17" / "FAIL node: found 20.1.0, need >=22,<23"; optional failures print "warn"; exit 1 iff a required check fails
   ```
   pnpm logic: try `("pnpm","--version")`; if None, try `("corepack","pnpm","--version")` (corepack honors package.json `packageManager`).
7. `/Users/drop/AIVOICE/scripts/tests/test_check_toolchain.py` (pytest, no subprocess — inject fake runner):
   - `test_parse_version_variants` parametrized: "uv 0.12.17 (Homebrew 2026-09-18 aarch64-apple-darwin)"→(0,12,17); "v22.23.4"→(22,23,4);
     "9.15.9"→(9,15,9); "Docker version 27.3.1, build ce12230"→(27,3,1); "Docker Compose version v2.29.7"→(2,29,7); "garbage"→None.
   - `test_evaluate_ok_and_bounds`: node 22.x ok; 23.0.0 fails (upper bound); 20.x fails (lower bound) with message containing ">=22".
   - `test_missing_binary_required_fails` / `test_missing_binary_optional_warns` (docker missing → ok=True? No: ok=False but main exit stays 0).
   - `test_pnpm_falls_back_to_corepack`: runner returns None for ("pnpm",...) and "9.15.9" for ("corepack","pnpm","--version") → ok.
   - `test_python_version_check` with python_version=(3,11,9) fails, (3,12,7) ok.
   - `test_main_exit_codes` (monkeypatch module runner/version or pass via run_checks) + `capsys` asserts output lines.
   Coverage of check_toolchain.py must be ≥ 85 % (aim ~100 %).
8. `/Users/drop/AIVOICE/package.json`: `{"name":"voiceagents","private":true,"packageManager":"pnpm@9.15.9",
   "engines":{"node":">=22 <23","pnpm":">=9 <10"},"scripts":{"format":"pnpm -r --if-present run format","lint":"pnpm -r --if-present run lint",
   "typecheck":"pnpm -r --if-present run typecheck","test":"pnpm -r --if-present run test","build":"pnpm -r --if-present run build"}}`
9. `/Users/drop/AIVOICE/pnpm-workspace.yaml`: `packages: ["apps/dashboard", "apps/widget", "sdks/typescript"]`.
10. `pnpm-lock.yaml` and `uv.lock` generated by `corepack pnpm install` / `uv sync --all-packages` and committed.
11. `/Users/drop/AIVOICE/Makefile` — `.PHONY` for all; `SHELL := /bin/bash`, `.SHELLFLAGS := -eu -o pipefail -c`;
    `PNPM ?= $(shell command -v pnpm >/dev/null 2>&1 && echo pnpm || echo corepack pnpm)`; `PROFILE ?=`; `.DEFAULT_GOAL := help`
    with a `help` target (grep `## ` comments). Every target from specs/02 §3, commands verbatim except `pnpm`→`$(PNPM)` and
    `pnpm -r X` → `$(PNPM) -r --if-present run X` (so an empty pnpm workspace passes):
    - `setup`: `uv run --no-project python scripts/check_toolchain.py` (or `python3.12 scripts/check_toolchain.py` before venv exists — pick
      whichever works pre-sync; `uv run --no-project --python 3.12` is preferred), `uv sync --all-packages`, `$(PNPM) install`,
      `scripts/fetch_models.sh`, `uv run python scripts/generate_audio_assets.py`.
    - `up`: `docker compose up -d --wait $(if $(PROFILE),--profile $(PROFILE),)` (+ `scripts/dev_up.sh` once T09 exists — for now call
      docker compose directly; T09 swaps to dev_up.sh); `down`: `docker compose --profile apps down`.
    - `migrate`, `seed`, `fmt`, `lint`, `typecheck`, `test`, `test-ts`, `e2e`, `e2e-ui` (`$(PNPM) --filter dashboard exec playwright test`),
      `openapi` (`uv run python scripts/export_openapi.py` — script created in T11), `placeholders`, `load` (`scripts/load/run.sh`),
      `check: lint typecheck placeholders test test-ts` then `uv run python scripts/check_openapi_drift.py` and
      `uv run python scripts/check_spec_refs.py`.
    Targets whose scripts arrive in later tasks (placeholders T03, spec-refs/openapi-drift T04, openapi T11, fetch_models/audio T10,
    migrate P01, seed later, load P20) reference the real final paths and fail loudly until then — they are NOT stubbed or guarded.

### Spec decision to record (same commit)
Append to specs/02 §3 a note: `> Decision (2026-10-25): Make invokes pnpm as "$(PNPM) -r --if-present run <script>" (PNPM falls back to
"corepack pnpm" when pnpm is not on PATH; package.json pins packageManager pnpm@9.15.9) so recursive runs succeed while workspace
packages are still being added. scripts/check_toolchain.py is the `make setup` preflight for the §2 versions.`

### AGENTS.md updates
- Commands table: `Toolchain preflight | uv run --no-project python scripts/check_toolchain.py`.
- Learned: "pnpm is provided via corepack (`corepack enable` or Makefile `PNPM` fallback); without package.json `packageManager`, corepack
  fetches the latest major (12.x) — never remove the pin." / "mypy `files` + `mypy_path` in root pyproject list only existing dirs; every
  new package/app must append its `src` (and tests) there." / "pytest uses --import-mode=importlib; scripts are importable via pythonpath=scripts."

### Gates to run (Builder)
`uv sync --all-packages` (exit 0), `corepack pnpm install` (exit 0, lockfile written), `make lint`, `make typecheck`, `make fmt`,
`make test` (scripts/tests passes, coverage ≥85 %), `make test-ts` (no packages → exit 0), `make help` lists every §3 target.
**Expected and acceptable for P00-T01**: `make placeholders`, `make check`, `make openapi`, `make setup` (fetch_models step) fail with
"No such file" because their scripts are deliverables of T03/T04/T10/T11. Reviewer: verify these fail only for that reason.
Also verify `make -n <target>` for every target in specs/02 §3 prints the expected command (target existence acceptance).

### Risks
- uv may reject glob members matching nothing or a `[project]` root with `package=false` + workspace — if so, drop `[project]` and use a
  pure virtual root, document in AGENTS.md.
- pytest `--strict-config` + nonexistent testpaths, `filterwarnings=error` with coverage "no data" warnings — trim config, not tests.
- ruff `S` on subprocess in scripts — covered by per-file ignore S603/S607; `PLR2004` magic numbers in check_toolchain: use named constants.
- `git add -A` by Reviewer: ensure .gitignore excludes `.ralph` runtime files, `logs/`, `.venv`, `node_modules` before committing.

## Build P00-T01 (Builder, 2026-10-25)
Implemented per plan. Deviations / findings:
- ruff 0.17 formats Python code fences inside `*.md` → first `make fmt` rewrote 15 spec files. Reverted with
  `git checkout -- specs` and added `"*.md"` to `[tool.ruff] extend-exclude` (recorded in specs/02 §3 Decision + AGENTS.md).
- `subprocess_runner` returns None on non-zero exit too (not only FileNotFoundError/Timeout), so a broken binary counts as missing.
- `Requirement` gained `fallback_command` (pnpm → `corepack pnpm --version`); `main()` accepts injectable runner/python_version.
- `make up` puts `--profile` before `up` (a global compose flag): `docker compose [--profile X] up -d --wait`.
- Host has docker 29.8.1 / compose 5.5.0: they pass `>=27` / `>=2` (no upper bound, optional checks).
Gates (logs/p00t01-*.log): fmt=0 lint=0 typecheck=0 test=0 (22 passed, 100% cov) test-ts=0 (no TS projects yet).
placeholders=2 → expected: `scripts/check_no_placeholders.py` doesn't exist yet (P00-T03). `make -n` succeeds for all 16 §3 targets.
uv sync --all-packages ok; corepack pnpm install ok (pnpm-lock.yaml written); preflight prints all ok.

## Plan P00-T01 — re-plan after build.blocked (Planner, 2026-10-25)
Blocker analysis: NOT a code/spec blocker. The Ralph runtime rejected the Builder's `build.done` because the payload lacked
the backpressure evidence keys it requires: `tests`, `lint`, `typecheck`, `audit`, `coverage`, `complexity`, `duplication`
(`performance`, `specs` optional). The implementation from "## Build P00-T01" stays as-is (no task split needed).

Builder action for this activation (no redesign; keep all existing files):
1. Re-run gates fresh, writing logs to `logs/p00t01-*.log`: `make fmt`, `make lint`, `make typecheck`, `make test`, `make test-ts`.
   `make placeholders` still fails only because scripts/check_no_placeholders.py is a P00-T03 deliverable — report it as
   `"placeholders":"n/a (gate script lands in P00-T03)"`, do not fake a pass.
2. Produce the extra evidence for real:
   - audit: `uv export --frozen --all-packages --no-hashes --format requirements-txt > logs/p00t01-req.txt && uvx pip-audit -r logs/p00t01-req.txt --strict`
     and `corepack pnpm audit` (empty workspace → no deps). If pip-audit finds a vuln in a dev tool, bump the lower bound in pyproject, `uv lock`, re-run.
     If the network is unavailable, report `audit: pass (offline: lockfile-only, ...)` only if truly verified otherwise; never claim unverified.
   - complexity: `uvx radon cc -s -a scripts/check_toolchain.py` → report average grade/score, e.g. `"complexity":"A (2.1 avg, radon cc)"`.
     Anything worse than B on a function → refactor.
   - duplication: `npx --yes jscpd@4 --min-lines 5 --reporters console scripts Makefile pyproject.toml` (or `uvx pylint --disable=all --enable=duplicate-code scripts`) → `"duplication":"pass (0 clones)"`.
   - coverage: from `make test` (was 100 % on check_toolchain.py; threshold 85 %).
3. Emit with ALL required keys, e.g.:
   ralph emit "build.done" --json '{"task":"P00-T01","tests":"pass (22 passed)","lint":"pass","typecheck":"pass","audit":"pass (...)","coverage":"pass (100%)","complexity":"A (...)","duplication":"pass (0 clones)","placeholders":"n/a (P00-T03)","specs":"pass"}'
Future Builders: always include these keys in build.done (recorded in AGENTS.md Learned + memory).

## Build P00-T01 — evidence re-run (Builder, 2026-10-25)
No code changes. Fresh gates (logs/p00t01-*.log): fmt=0 lint=0 typecheck=0 test=0 (22 passed, 100% cov on check_toolchain.py)
test-ts=0 (no TS projects). placeholders=2 only because scripts/check_no_placeholders.py is a P00-T03 deliverable.
audit: uvx pip-audit -r (uv export) --strict → "No known vulnerabilities found"; corepack pnpm audit → none.
complexity: radon cc avg A (3.4); worst main B(7), evaluate B(6). duplication: jscpd 0 clones.

## Plan P00-T01 — re-plan after 2nd build.blocked (Planner, 2026-10-25)
Root cause (from .ralph/diagnostics log + ralph binary strings): the 2nd build.done WAS rejected with
`backpressure checks failed tests=false lint=false typecheck=false audit=false coverage=false duplication=false`
even though every key was present. Ralph's backpressure parser does plain substring matching on the payload TEXT
for tokens `tests: pass`, `lint: pass`, `typecheck: pass`, `audit: pass`, `coverage: pass`, `duplication: pass`,
`complexity: <number>`, optional `specs: pass` / `performance:`. A `--json` object payload serializes as
`"tests":"pass ..."` (no `: ` + space form) so nothing matches. Implementation is unchanged and correct.

Builder action (no code changes):
1. Re-run fresh: make fmt, make lint, make typecheck, make test, make test-ts; audit (uvx pip-audit on uv export --strict,
   corepack pnpm audit); uvx radon cc -s -a scripts/check_toolchain.py; jscpd duplication. Logs to logs/p00t01-*.log.
2. Emit a PLAIN STRING payload (NOT --json), exact token form, e.g.:
   ralph emit "build.done" "P00-T01 tests: pass, lint: pass, typecheck: pass, audit: pass, coverage: pass, complexity: 3.4, duplication: pass, specs: pass (22 passed, 100% cov; radon avg A; jscpd 0 clones; make placeholders n/a until P00-T03 creates scripts/check_no_placeholders.py)"
   Only include a token as `pass` if that gate actually passed in this activation.

## Build P00-T01 — evidence re-run #3 (Builder, 2026-10-25)
No code changes. Fresh: fmt=0 lint=0 typecheck=0 test=0 (22 passed, 100% cov) test-ts=0. placeholders=2 only because
scripts/check_no_placeholders.py is a P00-T03 deliverable. pip-audit --strict + pnpm audit: no known vulns.
radon avg A (3.4). jscpd 0 clones. Emitting build.done as plain-text token payload.

## Review P00-T01 (Reviewer, 2026-10-25) — APPROVED
Re-ran: lint=0 typecheck=0 fmt=0 test=0 (22 passed, 100% cov) test-ts=0; placeholders=2 only because
scripts/check_no_placeholders.py lands in P00-T03 (expected per plan). uv sync --frozen and pnpm install --frozen-lockfile ok.
Diff matches specs/02 §2 (dev group, versions) and §3 (all targets; pnpm deviation recorded as Decision). Tests assert real behaviour.
Reviewer change: added PROMPT.md (generated by run_phase.sh) to .gitignore.
