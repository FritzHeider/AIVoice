"""Tests for the spec-reference gate (specs/02 §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

import check_spec_refs as csr

CONV_SPEC = """\
# 02 Conventions

## 3. Make
text
### 9.1 Detail
## 4 Python
## Appendix
"""
ERRORS_SPEC = "# 31 Errors\n\n## 3. Codes\n"


def make_repo(root: Path, plan: str = "", prompt: str = "", extra_specs: tuple[str, ...] = ()) -> Path:
    specs = root / "specs"
    specs.mkdir()
    (specs / "02-conv.md").write_text(CONV_SPEC, encoding="utf-8")
    (specs / "31-errors.md").write_text(ERRORS_SPEC, encoding="utf-8")
    for name in extra_specs:
        (specs / name).write_text("# extra\n", encoding="utf-8")
    for folder, body in (("plan", plan), ("prompts", prompt)):
        (root / folder).mkdir()
        (root / folder / "phase-00.md").write_text(body, encoding="utf-8")
    return root


def problems(tmp_path: Path, plan: str, extra_specs: tuple[str, ...] = ()) -> list[tuple[int, str, str]]:
    root = make_repo(tmp_path, plan=plan, extra_specs=extra_specs)
    return [(p.line, p.kind, p.detail) for p in csr.check_repo(root)]


def kinds(tmp_path: Path, plan: str, extra_specs: tuple[str, ...] = ()) -> list[str]:
    return [kind for _line, kind, _detail in problems(tmp_path, plan, extra_specs)]


@pytest.mark.parametrize(
    "line",
    [
        "see specs/02-conv.md",
        "see specs/02",
        "specs/02-conv.md §3",
        "specs/02 § 3",
        "specs/02 §9.1",
        "specs/02 §4",
        "specs/02 §3; specs/31 §3",
        "spec §9 before any reference",
        "specs/*.md glob is not a reference",
    ],
)
def test_valid_references(tmp_path: Path, line: str) -> None:
    assert problems(tmp_path, f"# Plan\n\n{line}\n") == []


def test_missing_full_form_reports_line(tmp_path: Path) -> None:
    found = problems(tmp_path, "intro\n\n- read specs/40-nothing.md now\n")
    assert found == [(3, "missing-spec", "specs/40-nothing.md does not exist")]


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("specs/02-con_v.md §3", "02-con_v.md"),
        ("specs/02-Conv.md", "02-Conv.md"),
        ("specs/021-foo.md", "021-foo.md"),
        ("specs/2-foo.md", "2-foo.md"),
        ("specs/bogus.md", "bogus.md"),
        ("(see `specs/31-error.md`)", "31-error.md"),
    ],
)
def test_malformed_full_form_is_looked_up_literally(tmp_path: Path, line: str, name: str) -> None:
    assert problems(tmp_path, f"{line}\n") == [(1, "missing-spec", f"specs/{name} does not exist")]


@pytest.mark.parametrize("line", ["specs/021 §3", "specs/02a §3", "specs/02_x §3", "specs/02-x §3"])
def test_short_form_requires_exactly_two_digits(tmp_path: Path, line: str) -> None:
    assert problems(tmp_path, f"{line}\n") == []


def test_short_form_before_punctuation(tmp_path: Path) -> None:
    assert kinds(tmp_path, "see specs/02. Then (specs/31) and specs/02, §8\n") == ["missing-section"]


def test_missing_short_form(tmp_path: Path) -> None:
    assert problems(tmp_path, "specs/77 §1\n") == [(1, "missing-spec", "no spec file starts with specs/77-")]


def test_ambiguous_short_form(tmp_path: Path) -> None:
    found = problems(tmp_path, "specs/05 §1\n", extra_specs=("05-a.md", "05-b.md"))
    assert found == [(1, "ambiguous-spec", "specs/05 matches 05-a.md, 05-b.md")]


def test_full_form_disambiguates(tmp_path: Path) -> None:
    assert kinds(tmp_path, "specs/05-a.md\n", extra_specs=("05-a.md", "05-b.md")) == []


def test_missing_section(tmp_path: Path) -> None:
    assert problems(tmp_path, "specs/02 §8\n") == [
        (1, "missing-section", "specs/02-conv.md has no heading for §8")
    ]


def test_section_pairs_with_nearest_preceding_spec(tmp_path: Path) -> None:
    found = problems(tmp_path, "specs/02 §3 and specs/31 §9\n")
    assert found == [(1, "missing-section", "specs/31-errors.md has no heading for §9")]


def test_range_checks_each_endpoint(tmp_path: Path) -> None:
    found = problems(tmp_path, "specs/02 §3\u2013§9\n")
    assert found == [(1, "missing-section", "specs/02-conv.md has no heading for §9")]


def test_pairing_is_line_scoped(tmp_path: Path) -> None:
    assert kinds(tmp_path, "specs/31\n§9 on the next line\n") == []


def test_section_after_unresolved_spec_not_reported(tmp_path: Path) -> None:
    assert kinds(tmp_path, "specs/88-gone.md §1 §2\n") == ["missing-spec"]


def test_unresolved_spec_resets_pairing(tmp_path: Path) -> None:
    assert kinds(tmp_path, "specs/02 §3 specs/88 §9\n") == ["missing-spec"]


def test_h1_and_non_numeric_headings_ignored(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    index = csr.load_spec_index(root / "specs")
    assert index["02-conv.md"] == frozenset({"3", "9.1", "4"})
    assert index["31-errors.md"] == frozenset({"3"})


def test_heading_needs_separator_after_number(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "07-x.md").write_text("## 12abc\n## 5) odd\n## 6.\n", encoding="utf-8")
    assert csr.load_spec_index(specs)["07-x.md"] == frozenset({"6"})


def test_prompts_are_scanned_and_other_dirs_are_not(tmp_path: Path) -> None:
    root = make_repo(tmp_path, prompt="specs/99 here\n")
    (root / "docs").mkdir()
    (root / "docs" / "x.md").write_text("specs/98 here\n", encoding="utf-8")
    (root / "plan" / "notes.txt").write_text("specs/97 here\n", encoding="utf-8")
    assert [(p.path, p.kind) for p in csr.check_repo(root)] == [("prompts/phase-00.md", "missing-spec")]


def test_problem_render() -> None:
    problem = csr.Problem("plan/phase-00.md", 4, "missing-section", "specs/02-conv.md has no heading for §8")
    assert problem.render() == "plan/phase-00.md:4: missing-section: specs/02-conv.md has no heading for §8"


def test_main_reports_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, plan="ok\nspecs/02 §8\n")
    assert csr.main(["--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "plan/phase-00.md:2: missing-section: specs/02-conv.md has no heading for §8\n"
    assert "1 problem(s)" in captured.err


def test_main_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, plan="specs/02 §3\n", prompt="specs/31-errors.md §3\n")
    assert csr.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_main_without_specs_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert csr.main(["--root", str(tmp_path)]) == 2
    assert "no specs/ directory" in capsys.readouterr().err


def test_repository_refs_resolve() -> None:
    assert csr.main([]) == 0
