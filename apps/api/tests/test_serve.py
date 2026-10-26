"""``python -m va_api.serve`` (specs/29-infrastructure.md §1.1) runs the module-level app on port 8000."""

from __future__ import annotations

import pytest
from service_helpers import capture_serve

from va_api import serve
from va_api.main import app
from va_common.service import Bind


def test_serve_main_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, serve)
    assert serve.main([]) == 0
    assert seen == [(app, Bind("0.0.0.0", 8000), {})]  # noqa: S104


def test_serve_main_rejects_bad_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, serve)
    with pytest.raises(SystemExit):
        serve.main(["--port", "http"])
    assert seen == []
