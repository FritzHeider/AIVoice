"""Tests for the OpenAPI drift gate (specs/02 §9)."""

from __future__ import annotations

import json
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

import check_openapi_drift as cod

SCHEMA: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "VoiceAgents", "version": "1"},
    "paths": {"/healthz": {"get": {"summary": "Liveness, café"}}},
}
INTERNAL: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "VoiceAgents Internal", "version": "1"},
    "paths": {"/internal/auth/session": {"get": {"summary": "Session"}}},
}


def write_app(
    directory: Path,
    schema: dict[str, Any] | None = None,
    body: str | None = None,
    internal: dict[str, Any] | None = None,
) -> str:
    name = f"fakeapi_{uuid.uuid4().hex}"
    if body is None:
        body = textwrap.dedent(
            f"""
            import json as _json

            _RAW = {json.dumps(schema if schema is not None else SCHEMA)!r}
            _INTERNAL = {json.dumps(internal if internal is not None else INTERNAL)!r}


            class _State:
                def internal_openapi(self) -> dict:
                    return _json.loads(_INTERNAL)


            class _App:
                state = _State()

                def openapi(self) -> dict:
                    return _json.loads(_RAW)


            app = _App()
            """
        )
    (directory / f"{name}.py").write_text(body, encoding="utf-8")
    return name


@pytest.fixture
def app_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "pkgs"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(directory))
    return directory


def commit_spec(
    root: Path, schema: dict[str, Any], internal: dict[str, Any] | None = INTERNAL, name: str = "openapi.json"
) -> Path:
    path = root / "openapi" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cod.render(schema), encoding="utf-8")
    if internal is not None:
        (root / "openapi" / "internal.json").write_text(cod.render(internal), encoding="utf-8")
    return path


def run(root: Path, app: str) -> int:
    return cod.main(["--root", str(root), "--app", f"{app}:app"])


def test_render_is_canonical() -> None:
    text = cod.render({"b": {"z": 1, "a": [{"y": 2, "x": "é"}]}, "a": 0})
    expected = [
        "{",
        '  "a": 0,',
        '  "b": {',
        '    "a": [',
        "      {",
        '        "x": "é",',
        '        "y": 2',
        "      }",
        "    ],",
        '    "z": 1',
        "  }",
        "}",
    ]
    assert text == "\n".join(expected) + "\n"


def test_current_spec_passes(tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit_spec(tmp_path, SCHEMA)
    assert run(tmp_path, write_app(app_dir)) == 0
    out = capsys.readouterr().out
    assert "openapi/openapi.json is current" in out
    assert "openapi/internal.json is current" in out


def test_drift_fails_with_diff(tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit_spec(tmp_path, SCHEMA)
    changed = {**SCHEMA, "paths": {"/readyz": {"get": {"summary": "Readiness"}}}}
    assert run(tmp_path, write_app(app_dir, changed)) == 1
    captured = capsys.readouterr()
    assert "--- committed" in captured.out
    assert "+++ generated" in captured.out
    assert '+    "/readyz": {' in captured.out
    assert "openapi/openapi.json is out of date; run: make openapi" in captured.err
    assert "internal.json is out of date" not in captured.err


def test_internal_drift_alone_fails(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit_spec(tmp_path, SCHEMA)
    changed = {**INTERNAL, "paths": {"/internal/auth/signup": {"post": {"summary": "Signup"}}}}
    assert run(tmp_path, write_app(app_dir, internal=changed)) == 1
    captured = capsys.readouterr()
    assert "openapi/openapi.json is current" in captured.out
    assert '+    "/internal/auth/signup": {' in captured.out
    assert "openapi/internal.json is out of date; run: make openapi" in captured.err
    assert "openapi.json is out of date" not in captured.err


def test_missing_internal_spec_fails(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit_spec(tmp_path, SCHEMA, internal=None)
    assert run(tmp_path, write_app(app_dir)) == 1
    assert "missing committed spec openapi/internal.json" in capsys.readouterr().err


def test_app_without_internal_document_fails(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit_spec(tmp_path, SCHEMA)
    name = write_app(
        app_dir, body="class _App:\n    def openapi(self):\n        return {}\n\n\napp = _App()\n"
    )
    assert run(tmp_path, name) == 1
    assert "has no callable state.internal_openapi()" in capsys.readouterr().err


def test_app_without_spec_fails(tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, write_app(app_dir)) == 1
    assert "missing committed spec" in capsys.readouterr().err


def test_spec_without_package_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit_spec(tmp_path, SCHEMA)
    assert run(tmp_path, f"absent_{uuid.uuid4().hex}.main") == 1
    assert "package not found" in capsys.readouterr().err


def test_bootstrap_state_skips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, f"absent_{uuid.uuid4().hex}.main") == 0
    assert "skipped" in capsys.readouterr().err


def test_import_error_inside_module_fails(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit_spec(tmp_path, SCHEMA)
    name = write_app(app_dir, body=f"import missing_dep_{uuid.uuid4().hex}\n")
    assert run(tmp_path, name) == 1
    assert "ModuleNotFoundError" in capsys.readouterr().err


def test_missing_attribute_fails(tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit_spec(tmp_path, SCHEMA)
    name = write_app(app_dir, body="other = 1\n")
    assert run(tmp_path, name) == 1
    assert "AttributeError" in capsys.readouterr().err


def test_object_without_openapi_fails(
    tmp_path: Path, app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit_spec(tmp_path, SCHEMA)
    name = write_app(app_dir, body="app = 1\n")
    assert run(tmp_path, name) == 1
    assert "has no callable openapi()" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["no_colon", ":app", "module:"])
def test_bad_app_argument_exits_2(tmp_path: Path, value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cod.main(["--root", str(tmp_path), "--app", value])
    assert excinfo.value.code == 2


def test_compare_truncates_large_diff() -> None:
    expected = "".join(f"line {n}\n" for n in range(500))
    diff = cod.compare(expected, "")
    assert len(diff) == cod.DIFF_LINE_LIMIT + 1
    assert diff[-1] == "[diff truncated after 200 lines]"


def test_compare_equal_is_empty() -> None:
    assert cod.compare("a\n", "a\n") == []


def test_repository_state() -> None:
    assert cod.main([]) == 0
