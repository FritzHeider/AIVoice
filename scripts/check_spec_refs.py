"""Spec-reference gate (specs/02 §9), run by `make check`.

Scans `plan/*.md` and `prompts/*.md` for references to specification files and sections:

- `specs/<name>.md` (full form, any name without a `*` glob) must name an existing file in `specs/`, looked up
  literally;
- `specs/NN` (short form, two digits not followed by a letter, digit, `_` or `-`) must match exactly one file
  whose name starts with `NN-`;
- a `§N` or `§N.M` reference is paired with the nearest preceding successfully resolved spec reference on the
  same line and must match a heading (level 2 or deeper) in that file that starts with the number followed by
  `.` or a space. A `§` reference with no spec reference before it on its line is prose and is not checked.

Every problem is printed as `path:line: kind: detail`; the exit code is 1 when anything was found, 2 when the
`specs/` directory is missing, and 0 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SOURCE_GLOBS: Final = ("plan/*.md", "prompts/*.md")
SPEC_RE: Final = re.compile(r"specs/(?:(?P<file>[^\s*`'\"()<>\[\]|]+?\.md)|(?P<num>\d{2})(?![\w-]))")
SECT_RE: Final = re.compile(r"§\s?(?P<sec>\d+(?:\.\d+)*)")
HEAD_RE: Final = re.compile(r"^#{2,6} (?P<sec>\d+(?:\.\d+)*)[. ]", re.MULTILINE)

SpecIndex = Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class Problem:
    path: str
    line: int
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Resolution:
    filename: str | None
    kind: str
    detail: str


def load_spec_index(specs_dir: Path) -> dict[str, frozenset[str]]:
    index: dict[str, frozenset[str]] = {}
    for path in sorted(specs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        index[path.name] = frozenset(m.group("sec") for m in HEAD_RE.finditer(text))
    return index


def resolve_spec(match: re.Match[str], index: SpecIndex) -> Resolution:
    name = match.group("file")
    if name is not None:
        if name in index:
            return Resolution(name, "", "")
        return Resolution(None, "missing-spec", f"specs/{name} does not exist")
    num = match.group("num")
    candidates = sorted(name for name in index if name.startswith(f"{num}-"))
    if len(candidates) == 1:
        return Resolution(candidates[0], "", "")
    if not candidates:
        return Resolution(None, "missing-spec", f"no spec file starts with specs/{num}-")
    return Resolution(None, "ambiguous-spec", f"specs/{num} matches {', '.join(candidates)}")


def _tokens(line: str) -> list[re.Match[str]]:
    matches = [*SPEC_RE.finditer(line), *SECT_RE.finditer(line)]
    return sorted(matches, key=lambda m: m.start())


def check_line(rel: str, lineno: int, line: str, index: SpecIndex) -> list[Problem]:
    problems: list[Problem] = []
    current: str | None = None
    for token in _tokens(line):
        if token.re is SPEC_RE:
            resolution = resolve_spec(token, index)
            current = resolution.filename
            if current is None:
                problems.append(Problem(rel, lineno, resolution.kind, resolution.detail))
            continue
        section = token.group("sec")
        if current is not None and section not in index[current]:
            detail = f"specs/{current} has no heading for §{section}"
            problems.append(Problem(rel, lineno, "missing-section", detail))
    return problems


def check_text(rel: str, text: str, index: SpecIndex) -> list[Problem]:
    problems: list[Problem] = []
    for number, line in enumerate(text.splitlines(), start=1):
        problems += check_line(rel, number, line, index)
    return problems


def check_repo(root: Path) -> list[Problem]:
    index = load_spec_index(root / "specs")
    problems: list[Problem] = []
    for pattern in SOURCE_GLOBS:
        for path in sorted(root.glob(pattern)):
            rel = path.relative_to(root).as_posix()
            problems += check_text(rel, path.read_text(encoding="utf-8"), index)
    return problems


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail on spec file or section references that do not resolve."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the directory above scripts/)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root: Path = args.root.resolve()
    if not (root / "specs").is_dir():
        print(f"check_spec_refs: no specs/ directory under {root}", file=sys.stderr)
        return 2
    problems = check_repo(root)
    for problem in problems:
        print(problem.render())
    print(f"check_spec_refs: {len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
