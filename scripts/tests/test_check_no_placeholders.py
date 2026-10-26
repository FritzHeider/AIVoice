"""Tests for the unfinished-work detector gate (specs/02 §9.1).

Banned tokens are assembled from fragments with `j()` so this file never contains them literally; implicit
string concatenation is avoided on purpose because `ruff format` would join the pieces back together.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path, PurePosixPath

import pytest

import check_no_placeholders as cnp

FILLER = "place" + "holder"
CAP_FILLER = "Place" + "holder"
DOMAIN = "example" + ".com"
LOG_CALL = "console" + ".log"


def j(*parts: str) -> str:
    return "".join(parts)


def rules_for(path: str, source: str) -> list[tuple[int, str]]:
    findings = cnp.check_file(PurePosixPath(path), textwrap.dedent(source).encode("utf-8"))
    return [(f.line, f.rule) for f in findings]


def rule_names(path: str, source: str) -> list[str]:
    return [rule for _line, rule in rules_for(path, source)]


# --- text rules ---------------------------------------------------------------------------------------------

MARKERS = [j("TO", "DO"), j("FIX", "ME"), j("X", "XX"), j("HA", "CK"), j("T", "BD")]


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("packages/x/src/x/mod.py", 'VALUE = 1\nNOTE = "{m}: later"\n'),
        ("apps/dashboard/src/a.ts", "const a = 1;\n// {m} wire this\n"),
        ("infra/helm/values.yaml", "replicas: 1\nimage: {m}\n"),
    ],
)
def test_marker_detected(marker: str, path: str, template: str) -> None:
    assert rules_for(path, template.format(m=marker)) == [(2, "marker")]


@pytest.mark.parametrize(
    "line",
    [
        "# a quick hack around the parser",
        "# the todo list is rendered below",
        j("# XX", "XX is a mask, not a marker"),
        j("# STA", "TBD_FLAG is a longer identifier"),
    ],
)
def test_marker_lowercase_or_embedded_ignored(line: str) -> None:
    assert rule_names("packages/x/src/x/mod.py", line + "\n") == []


@pytest.mark.parametrize("word", [FILLER, CAP_FILLER, FILLER.upper()])
def test_filler_word_detected(word: str) -> None:
    assert rules_for("packages/x/src/x/mod.py", f'TEXT = "a {word} value"\n') == [(1, "filler-word")]


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("apps/dashboard/src/search.tsx", f'<input {FILLER}="Search calls" />'),
        ("apps/dashboard/src/search.tsx", f"<input {FILLER}={{label}} />"),
        ("apps/dashboard/src/search.tsx", f"<input {FILLER} = 'x' />"),
        ("apps/dashboard/src/globals.css", f"input::{FILLER} {{ color: grey; }}"),
        ("apps/dashboard/src/ui/input.tsx", f'const cls = "{FILLER}:text-muted-foreground";'),
        ("scripts/tests/test_x.py", j("import check_no_", FILLER, "s as cnp")),
    ],
)
def test_filler_word_allowances(path: str, line: str) -> None:
    assert "filler-word" not in rule_names(path, line + "\n")


def test_filler_word_allowance_does_not_hide_bare_word() -> None:
    line = f'<input {FILLER}="{FILLER} text" />\n'
    assert rule_names("apps/dashboard/src/search.tsx", line) == ["filler-word"]


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (j("raise NotImplemented", "Error"), "not-implemented"),
        (j("# Implement", " me later"), "implement-me"),
        (j('BODY = "Lorem', ' Ipsum dolor"'), "lorem-ipsum"),
        (j('KEY = "YOUR-API', '-KEY"'), "sample-api-key"),
    ],
)
def test_text_rules_detected(text: str, rule: str) -> None:
    assert rules_for("packages/x/src/x/mod.py", f"A = 1\n{text}\n") == [(2, rule)]


def test_example_domain_non_test_fails() -> None:
    source = f'URL = "https://api.{DOMAIN}/v1"\n'
    assert rules_for("packages/x/src/x/a.py", source) == [(1, "example-domain")]


@pytest.mark.parametrize(
    "path",
    [
        "packages/x/tests/test_a.py",
        "apps/dashboard/src/a.test.ts",
        "apps/dashboard/src/a.spec.tsx",
        "x/conftest.py",
    ],
)
def test_example_domain_allowed_in_tests(path: str) -> None:
    assert rule_names(path, f'URL = "https://{DOMAIN}"\n') == []


# --- Python AST rules ---------------------------------------------------------------------------------------

PY_PATH = "packages/x/src/x/mod.py"


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    pass\n",
        "def f():\n    ...\n",
        'def f():\n    """Doc."""\n    pass\n',
        "async def f():\n    ...\n",
        "@registry.register()\ndef f():\n    pass\n",
        "class A:\n    def m(self):\n        pass\n",
        "class A:\n    pass\n",
        "class A:\n    ...\n",
        "class AppError(Exception):\n    pass\n",
        "def outer():\n    def inner():\n        pass\n    return inner\n",
        "class P(Protocol):\n    def m(self):\n        def g():\n            pass\n        return g\n",
    ],
)
def test_stub_body_detected(source: str) -> None:
    assert rule_names(PY_PATH, source) == ["stub-body"]


def test_stub_body_reports_definition_line() -> None:
    assert rules_for(PY_PATH, "import os\n\n\ndef f():\n    pass\n") == [(4, "stub-body")]


@pytest.mark.parametrize(
    "source",
    [
        "class P(Protocol):\n    def m(self) -> int:\n        ...\n",
        "class P(typing.Protocol):\n    async def m(self):\n        ...\n",
        "class P(Protocol[T]):\n    def m(self) -> T:\n        ...\n",
        "class A(ABC):\n    @abstractmethod\n    def m(self):\n        ...\n",
        "class A(abc.ABC):\n    @abc.abstractmethod\n    def m(self):\n        pass\n",
        "@overload\ndef f(x: int) -> int:\n    ...\n",
        "@typing.overload\ndef f(x: str) -> str:\n    ...\n",
        'class AppError(Exception):\n    """Domain error."""\n    pass\n',
        'class Bad(errors.BaseError):\n    """Wrapped."""\n',
        'class Odd(UserWarning):\n    """A warning."""\n    ...\n',
        'class Root(BaseException):\n    """Root."""\n    pass\n',
        'class P(Protocol):\n    """Marker protocol."""\n    ...\n',
        'class Marker:\n    """Only a docstring."""\n',
        "def f():\n    return 1\n",
        'def f():\n    """Doc."""\n    return None\n',
    ],
)
def test_stub_body_allowed(source: str) -> None:
    assert rule_names(PY_PATH, source) == []


def test_stub_body_skipped_for_type_stub_files() -> None:
    assert rule_names("packages/x/src/x/mod.pyi", "def f() -> int: ...\nclass A: ...\n") == []


def test_print_call_detected_in_library_source() -> None:
    assert rules_for(PY_PATH, "x = 1\nprint(x)\n") == [(2, "print-call")]


@pytest.mark.parametrize(
    "path",
    [
        "scripts/tool.py",
        "apps/dashboard/scripts/gen.py",
        "sdks/cli/src/va_cli/app.py",
        "packages/x/src/x/__main__.py",
        "apps/a/src/a/cli.py",
        "packages/x/src/x/cli/commands.py",
    ],
)
def test_print_call_allowed(path: str) -> None:
    assert rule_names(path, "print('hello')\n") == []


def test_print_inside_string_literal_is_not_a_call() -> None:
    assert rule_names(PY_PATH, 'TEXT = "print(x)"\nlogger.info("done")\n') == []


def test_syntax_error_reported() -> None:
    assert rules_for(PY_PATH, "x = 1\ndef broken(:\n") == [(2, "syntax-error")]


def test_syntax_error_without_line_number() -> None:
    findings = cnp.check_python(PurePosixPath(PY_PATH), "x = 1\0\n")
    assert [(f.rule, f.line >= 1) for f in findings] == [("syntax-error", True)]


# --- TS/JS rules --------------------------------------------------------------------------------------------

TS_PATH = "apps/dashboard/src/a.ts"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("const f = () => {};\n", [(1, "empty-function")]),
        ("export function f() {}\n", [(1, "empty-function")]),
        ("const a = 1;\nfunction f(a: number) {\n  \n}\n", [(2, "empty-function")]),
        ("p.then(run).catch(() => {});\n", [(1, "empty-function")]),
        ("try {\n  run();\n} catch {}\n", [(3, "empty-catch")]),
        ("try {\n  run();\n} catch (e) {\n}\n", [(3, "empty-catch")]),
    ],
)
def test_ts_empty_bodies(source: str, expected: list[tuple[int, str]]) -> None:
    assert rules_for(TS_PATH, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "const f = () => {\n  return 1;\n};\n",
        "function f(a: number) {\n  return a;\n}\n",
        "const o = f({});\nconst g = () => ({});\n",
        "try {\n  run();\n} catch (e) {\n  report(e);\n}\n",
    ],
)
def test_ts_non_empty_bodies_pass(source: str) -> None:
    assert rule_names(TS_PATH, source) == []


def test_console_log_detected() -> None:
    assert rules_for(TS_PATH, f"const a = 1;\n{LOG_CALL}(a);\n") == [(2, "console-log")]


@pytest.mark.parametrize(
    ("path", "line"),
    [
        (TS_PATH, f"{LOG_CALL}('ready'); // va-allow-console   "),
        ("apps/dashboard/scripts/gen.ts", f"{LOG_CALL}('generated');"),
        (TS_PATH, "console.error('failed');"),
    ],
)
def test_console_log_allowed(path: str, line: str) -> None:
    assert rule_names(path, line + "\n") == []


def test_ts_rules_not_applied_to_python() -> None:
    assert rule_names(PY_PATH, 'JS = "() => {}"\n') == []


# --- exclusions, binaries, rendering ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "apps/dashboard/node_modules/x/index.js",
        "packages/x/.venv/lib/a.py",
        "apps/widget/dist/widget.js",
        "sdks/python/build/lib/a.py",
        "apps/dashboard/.next/server/a.js",
        "packages/x/src/x/__pycache__/a.py",
        "sdks/typescript/src/schema.d.ts",
        "apps/dashboard/pnpm-lock.yaml",
        "packages/x/uv.lock",
        "openapi/openapi.json",
    ],
)
def test_is_excluded(path: str) -> None:
    assert cnp.is_excluded(PurePosixPath(path))


def test_excluded_file_is_not_scanned() -> None:
    data = j("# TO", "DO\n").encode()
    assert cnp.check_file(PurePosixPath("apps/widget/dist/widget.js"), data) == []


def test_regular_paths_are_not_excluded() -> None:
    assert not cnp.is_excluded(PurePosixPath("apps/api/src/va_api/main.py"))


@pytest.mark.parametrize("data", [j("# TO", "DO\0\n").encode(), b"\xff\xfe" + j("TO", "DO").encode()])
def test_binary_files_skipped(data: bytes) -> None:
    assert cnp.check_file(PurePosixPath("packages/x/src/x/blob.bin"), data) == []


def test_finding_render_format_and_truncation() -> None:
    long_line = "    " + j("# TO", "DO ") + "y" * 200
    [finding] = cnp.check_file(PurePosixPath("infra/a.yaml"), long_line.encode())
    rendered = finding.render()
    assert rendered.startswith(j("infra/a.yaml:1: marker: # TO", "DO y"))
    assert len(finding.excerpt) == cnp.EXCERPT_LIMIT
    assert finding.excerpt == long_line.strip()[: cnp.EXCERPT_LIMIT]


# --- CLI ----------------------------------------------------------------------------------------------------


def test_main_reports_findings_for_explicit_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "dirty.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "packages" / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    code = cnp.main(["--root", str(tmp_path), "packages/clean.py", "packages/dirty.py"])
    out, err = capsys.readouterr()
    assert code == 1
    assert out == "packages/dirty.py:1: stub-body: def f():\n"
    assert err == "1 finding(s)\n"


def test_main_returns_zero_for_clean_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "clean.ts").write_text("export const a = 1;\n", encoding="utf-8")
    assert cnp.main(["--root", str(tmp_path), "clean.ts"]) == 0
    assert capsys.readouterr().out == ""


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=ci@voiceagents.test", "-c", "user.name=ci", "-C", str(root), *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_git_discovery(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    marker = j("# TO", "DO\n")
    git(tmp_path, "init", "-q")
    write(tmp_path, ".gitignore", "scripts/ignored.py\n")
    write(tmp_path, "scripts/tracked.py", marker)
    write(tmp_path, "scripts/gone.py", marker)
    write(tmp_path, "docs/outside.py", marker)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "scripts" / "gone.py").unlink()
    write(tmp_path, "scripts/untracked.py", marker)
    write(tmp_path, "scripts/ignored.py", marker)

    found = [p.relative_to(tmp_path).as_posix() for p in cnp.list_candidate_files(tmp_path)]
    assert found == ["scripts/tracked.py", "scripts/untracked.py"]

    assert cnp.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in out] == ["scripts/tracked.py", "scripts/untracked.py"]


def test_main_returns_two_outside_a_git_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert cnp.main(["--root", str(tmp_path)]) == 2
    assert "git ls-files failed" in capsys.readouterr().err


def test_repository_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    code = cnp.main([])
    out, _err = capsys.readouterr()
    assert code == 0, out
