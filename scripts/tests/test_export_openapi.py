"""Tests for `make openapi` (scripts/export_openapi.py, specs/26 §1)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

import check_openapi_drift as cod
import export_openapi as eo

REPO = Path(__file__).resolve().parents[2]
SCHEMA: dict[str, Any] = {"openapi": "3.1.0", "info": {"title": "T", "version": "1"}, "paths": {"/é": {}}}
INTERNAL: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "I", "version": "1"},
    "paths": {"/internal/x": {}},
}
APP_SOURCE = f"""
class _State:
    def internal_openapi(self):
        return {INTERNAL!r}


class _App:
    state = _State()

    def openapi(self):
        return {SCHEMA!r}


app = _App()
"""


def write_app(directory: Path, body: str | None = None) -> str:
    name = f"exportapi_{uuid.uuid4().hex}"
    source = body if body is not None else APP_SOURCE
    (directory / f"{name}.py").write_text(source, encoding="utf-8")
    return name


@pytest.fixture
def app_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "pkgs"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(directory))
    return directory


def export(root: Path, app: str, *extra: str) -> int:
    return eo.main(["--root", str(root), "--app", f"{app}:app", *extra])


def test_writes_canonical_files_then_unchanged(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = write_app(app_dir)
    files = {tmp_path / "openapi" / "openapi.json": SCHEMA, tmp_path / "openapi" / "internal.json": INTERNAL}
    assert export(tmp_path, name) == 0
    assert {path: path.read_text(encoding="utf-8") for path in files} == {
        path: cod.render(schema) for path, schema in files.items()
    }
    assert capsys.readouterr().out.splitlines() == [
        "wrote openapi/openapi.json",
        "wrote openapi/internal.json",
    ]
    mtimes = [path.stat().st_mtime_ns for path in files]
    assert export(tmp_path, name) == 0
    assert capsys.readouterr().out.splitlines() == [
        "unchanged openapi/openapi.json",
        "unchanged openapi/internal.json",
    ]
    assert [path.stat().st_mtime_ns for path in files] == mtimes
    assert not list((tmp_path / "openapi").glob(".*.tmp"))
    assert cod.main(["--root", str(tmp_path), "--app", f"{name}:app"]) == 0


def test_rewrites_stale_file(tmp_path: Path, app_dir: Path) -> None:
    out = tmp_path / "docs" / "api.json"
    out.parent.mkdir()
    out.write_text("{}\n", encoding="utf-8")
    assert (
        export(tmp_path, write_app(app_dir), "--out", "docs/api.json", "--internal-out", "docs/int.json") == 0
    )
    assert out.read_text(encoding="utf-8") == cod.render(SCHEMA)
    assert (out.parent / "int.json").read_text(encoding="utf-8") == cod.render(INTERNAL)


def test_load_failure_exits_1(tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    name = write_app(app_dir, body="app = 1\n")
    assert export(tmp_path, name) == 1
    assert "export_openapi: cannot load" in capsys.readouterr().err
    assert not (tmp_path / "openapi").exists()


def test_bad_app_argument_exits_2(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        eo.main(["--root", str(tmp_path), "--app", "no_colon"])
    assert excinfo.value.code == 2


def test_default_app_export_matches_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAPI_PUBLIC_SERVER_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert eo.main(["--root", str(tmp_path)]) == 0
    for name in ("openapi.json", "internal.json"):
        exported = (tmp_path / "openapi" / name).read_text(encoding="utf-8")
        assert exported == (REPO / "openapi" / name).read_text(encoding="utf-8")
