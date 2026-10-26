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


def write_app(directory: Path, body: str | None = None) -> str:
    name = f"exportapi_{uuid.uuid4().hex}"
    source = (
        body
        if body is not None
        else f"class _App:\n    def openapi(self):\n        return {SCHEMA!r}\n\n\napp = _App()\n"
    )
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


def test_writes_canonical_file_then_unchanged(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = write_app(app_dir)
    assert export(tmp_path, name) == 0
    out = tmp_path / "openapi" / "openapi.json"
    assert out.read_text(encoding="utf-8") == cod.render(SCHEMA)
    assert "wrote openapi/openapi.json" in capsys.readouterr().out
    mtime = out.stat().st_mtime_ns
    assert export(tmp_path, name) == 0
    assert "unchanged openapi/openapi.json" in capsys.readouterr().out
    assert out.stat().st_mtime_ns == mtime
    assert not list(out.parent.glob(".*.tmp"))
    assert cod.main(["--root", str(tmp_path), "--app", f"{name}:app"]) == 0


def test_rewrites_stale_file(tmp_path: Path, app_dir: Path) -> None:
    out = tmp_path / "docs" / "api.json"
    out.parent.mkdir()
    out.write_text("{}\n", encoding="utf-8")
    assert export(tmp_path, write_app(app_dir), "--out", "docs/api.json") == 0
    assert out.read_text(encoding="utf-8") == cod.render(SCHEMA)


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
    exported = (tmp_path / "openapi" / "openapi.json").read_text(encoding="utf-8")
    assert exported == (REPO / "openapi" / "openapi.json").read_text(encoding="utf-8")
