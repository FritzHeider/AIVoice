"""`scripts/generate_npa_states.py`: the committed table equals a regeneration; `--check` detects drift."""

from __future__ import annotations

from pathlib import Path

import pytest

import generate_npa_states as gen


def test_committed_table_matches_regeneration() -> None:
    assert gen.main(["--check"]) == 0
    assert gen.OUT_FILE.read_text(encoding="utf-8") == gen.render(gen.build_table())


def test_check_detects_a_tampered_copy(tmp_path: Path) -> None:
    copy = tmp_path / "npa_states.json"
    copy.write_text(gen.OUT_FILE.read_text(encoding="utf-8").replace('"415": "CA"', '"415": "NV"'))
    assert gen.main(["--check", "--out", str(copy)]) == 1
    assert gen.main(["--check", "--out", str(tmp_path / "missing.json")]) == 1


def test_write_creates_then_leaves_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "data" / "npa_states.json"
    assert gen.main(["--out", str(out)]) == 0
    assert gen.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == gen.OUT_FILE.read_text(encoding="utf-8")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("wrote ")
    assert lines[1].startswith("unchanged ")


@pytest.mark.parametrize(
    ("description", "code"),
    [
        ("San Francisco, CA", "CA"),
        ("Ontario", "ON"),
        ("Nova Scotia/Prince Edward Island", "NS"),
        ("Washington State", "WA"),
        ("Jamaica", None),
        ("", None),
    ],
)
def test_region_code(description: str, code: str | None) -> None:
    assert gen.region_code(description) == code
