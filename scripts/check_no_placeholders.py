"""Unfinished-work detector gate (specs/02 §9.1), run by the Makefile target of the same purpose.

Scans the git-listed files (tracked plus untracked-but-not-ignored) under the five source roots and
reports leftover work markers, filler text, stub bodies, stray debug output, and empty TypeScript functions
or catch blocks. Every finding is printed as `path:line: rule: excerpt`; the exit code is 1 when anything was
found, 2 when the file list could not be obtained, and 0 otherwise.

The banned tokens are written in this file with a character class splitting them (for example `T[O]DO`) so
the checker can scan its own source without reporting itself.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

SCAN_ROOTS: Final = ("apps", "packages", "sdks", "infra", "scripts")
EXCLUDED_DIRS: Final = frozenset(
    {"node_modules", ".venv", "dist", "build", ".next", "__pycache__", ".turbo", "coverage"}
)
LOCKFILES: Final = frozenset({"uv.lock", "pnpm-lock.yaml", "package-lock.json", "yarn.lock"})
GENERATED_OPENAPI: Final = PurePosixPath("openapi/openapi.json")
PY_SUFFIXES: Final = frozenset({".py"})
TS_SUFFIXES: Final = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"})
TEST_DIRS: Final = frozenset({"tests", "tests_e2e", "__tests__"})
EXCERPT_LIMIT: Final = 120
BINARY_SNIFF_BYTES: Final = 8192
GIT_TIMEOUT_SECONDS: Final = 30
CONSOLE_ALLOW_MARKER: Final = "// va-allow-console"

_TEST_NAME_RE: Final = re.compile(r"^(?:test_.*\.py|.*_test\.py|conftest\.py|.*\.(?:test|spec)\.[^.]+)$")

_TEXT_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("marker", re.compile(r"\b(?:T[O]DO|F[I]XME|X[X]X|H[A]CK|T[B]D)\b")),
    ("filler-word", re.compile(r"place[h]older", re.IGNORECASE)),
    ("not-implemented", re.compile(r"\bNotImplemented[E]rror\b")),
    ("implement-me", re.compile(r"implement[ ]me", re.IGNORECASE)),
    ("lorem-ipsum", re.compile(r"lorem[ ]ipsum", re.IGNORECASE)),
    ("sample-api-key", re.compile(r"your-api[-]key", re.IGNORECASE)),
    ("example-domain", re.compile(r"\bexample[.]com\b", re.IGNORECASE)),
)
_FILLER_ALLOWED_RE: Final = re.compile(
    r"place[h]older\s*=|::place[h]older|\bplace[h]older:|check_no_place[h]olders",
    re.IGNORECASE,
)

_EMPTY_CATCH_RE: Final = re.compile(r"\bcatch\s*(?:\([^)]*\))?\s*\{\s*\}")
_EMPTY_ARROW_RE: Final = re.compile(r"=>\s*\{\s*\}")
_EMPTY_PAREN_BODY_RE: Final = re.compile(r"\)\s*\{\s*\}")
_CONSOLE_LOG_RE: Final = re.compile(r"\bconsole[.]log\s*\(")

_EXCEPTION_SUFFIXES: Final = ("Error", "Exception", "Warning")


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.excerpt}"


def make_excerpt(line: str) -> str:
    return line.strip()[:EXCERPT_LIMIT]


def is_excluded(rel: PurePosixPath) -> bool:
    if any(part in EXCLUDED_DIRS for part in rel.parts):
        return True
    return rel.name == "schema.d.ts" or rel.name in LOCKFILES or rel == GENERATED_OPENAPI


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def is_test_path(rel: PurePosixPath) -> bool:
    return any(part in TEST_DIRS for part in rel.parts[:-1]) or bool(_TEST_NAME_RE.match(rel.name))


def is_cli_module(rel: PurePosixPath) -> bool:
    if rel.parts[:2] == ("sdks", "cli"):
        return True
    return rel.name in {"cli.py", "__main__.py"} or "cli" in rel.parts[:-1]


def in_scripts_dir(rel: PurePosixPath) -> bool:
    return "scripts" in rel.parts[:-1]


def _line_violates(rule: str, pattern: re.Pattern[str], line: str) -> bool:
    if rule == "filler-word":
        line = _FILLER_ALLOWED_RE.sub("", line)
    return pattern.search(line) is not None


def check_text_rules(rel: PurePosixPath, text: str) -> list[Finding]:
    rules = [
        (rule, pattern) for rule, pattern in _TEXT_RULES if rule != "example-domain" or not is_test_path(rel)
    ]
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        findings += [
            Finding(str(rel), number, rule, make_excerpt(line))
            for rule, pattern in rules
            if _line_violates(rule, pattern, line)
        ]
    return findings


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_protocol(cls: ast.ClassDef) -> bool:
    return any(_base_name(base) == "Protocol" for base in cls.bases)


def _is_exception_class(cls: ast.ClassDef) -> bool:
    names = [_base_name(base) for base in cls.bases]
    return any(name == "BaseException" or name.endswith(_EXCEPTION_SUFFIXES) for name in names)


def _has_docstring(body: list[ast.stmt]) -> bool:
    first = body[0] if body else None
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def _is_trivial_statement(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    return (
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis
    )


def _is_stub_body(body: list[ast.stmt]) -> bool:
    remaining = body[1:] if _has_docstring(body) else body
    return all(_is_trivial_statement(stmt) for stmt in remaining)


def _has_allowing_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_base_name(dec) in {"abstractmethod", "overload"} for dec in func.decorator_list)


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, rel: PurePosixPath, lines: Sequence[str]) -> None:
        self.rel = rel
        self.lines = lines
        self.check_print = not in_scripts_dir(rel) and not is_cli_module(rel)
        self.class_stack: list[ast.ClassDef] = []
        self.findings: list[Finding] = []

    def _report(self, node: ast.stmt | ast.expr, rule: str) -> None:
        line = self.lines[node.lineno - 1] if 0 < node.lineno <= len(self.lines) else ""
        self.findings.append(Finding(str(self.rel), node.lineno, rule, make_excerpt(line)))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if _is_stub_body(node.body) and not self._class_stub_allowed(node):
            self._report(node, "stub-body")
        self.class_stack.append(node)
        self.generic_visit(node)
        self.class_stack.pop()

    def _class_stub_allowed(self, node: ast.ClassDef) -> bool:
        has_doc = _has_docstring(node.body)
        if has_doc and len(node.body) == 1:
            return True
        return has_doc and (_is_exception_class(node) or _is_protocol(node))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if _is_stub_body(node.body) and not self._function_stub_allowed(node):
            self._report(node, "stub-body")
        enclosing = self.class_stack
        self.class_stack = []
        self.generic_visit(node)
        self.class_stack = enclosing

    def _function_stub_allowed(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        in_protocol = bool(self.class_stack) and _is_protocol(self.class_stack[-1])
        return in_protocol or _has_allowing_decorator(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.check_print and isinstance(node.func, ast.Name) and node.func.id == "print":
            self._report(node, "print-call")
        self.generic_visit(node)


def check_python(rel: PurePosixPath, text: str) -> list[Finding]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=str(rel))
    except SyntaxError as exc:
        number = exc.lineno or 1
        line = lines[number - 1] if 0 < number <= len(lines) else ""
        return [Finding(str(rel), number, "syntax-error", make_excerpt(line))]
    visitor = _PythonVisitor(rel, lines)
    visitor.visit(tree)
    return visitor.findings


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _empty_function_matches(text: str, catch_spans: Sequence[tuple[int, int]]) -> Iterator[re.Match[str]]:
    yield from _EMPTY_ARROW_RE.finditer(text)
    for match in _EMPTY_PAREN_BODY_RE.finditer(text):
        if not any(start <= match.start() < end for start, end in catch_spans):
            yield match


def _console_log_lines(rel: PurePosixPath, lines: Sequence[str]) -> Iterator[int]:
    if in_scripts_dir(rel):
        return
    for number, line in enumerate(lines, start=1):
        if _CONSOLE_LOG_RE.search(line) and not line.rstrip().endswith(CONSOLE_ALLOW_MARKER):
            yield number


def check_script_like(rel: PurePosixPath, text: str) -> list[Finding]:
    lines = text.splitlines()
    path = str(rel)

    def finding(line_number: int, rule: str) -> Finding:
        return Finding(path, line_number, rule, make_excerpt(lines[line_number - 1]))

    catch_matches = list(_EMPTY_CATCH_RE.finditer(text))
    catch_spans = [(m.start(), m.end()) for m in catch_matches]
    findings = [finding(_line_of(text, m.start()), "empty-catch") for m in catch_matches]
    findings += [
        finding(_line_of(text, m.start()), "empty-function")
        for m in _empty_function_matches(text, catch_spans)
    ]
    findings += [finding(number, "console-log") for number in _console_log_lines(rel, lines)]
    return findings


def check_file(rel: PurePosixPath, data: bytes) -> list[Finding]:
    if is_excluded(rel) or is_binary(data):
        return []
    text = data.decode("utf-8")
    findings = check_text_rules(rel, text)
    if rel.suffix in PY_SUFFIXES:
        findings += check_python(rel, text)
    elif rel.suffix in TS_SUFFIXES:
        findings += check_script_like(rel, text)
    return findings


def list_candidate_files(root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *SCAN_ROOTS,
        ],
        capture_output=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    names = sorted({name for name in result.stdout.decode("utf-8").split("\0") if name})
    return [root / name for name in names if (root / name).is_file()]


def scan(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        rel = PurePosixPath(path.relative_to(root).as_posix())
        findings += check_file(rel, path.read_bytes())
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail on unfinished-work markers, stubs, and debug output.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the directory above scripts/)",
    )
    parser.add_argument(
        "paths", nargs="*", help="files relative to --root to scan instead of the git listing"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root: Path = args.root.resolve()
    if args.paths:
        files = [root / name for name in args.paths]
    else:
        try:
            files = list_candidate_files(root)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"check_no_placeholders: git ls-files failed: {exc}", file=sys.stderr)
            return 2
    findings = scan(root, files)
    for item in findings:
        print(item.render())
    print(f"{len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
