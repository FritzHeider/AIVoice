"""Edge entry points: module-level app and ``python -m va_edge.serve``."""

from __future__ import annotations

import importlib

import pytest
from service_helpers import capture_serve

from va_common.service import Bind
from va_edge import serve


def test_module_app_imports_without_io() -> None:
    main = importlib.import_module("va_edge.main")
    assert main.app.title == "VoiceAgents Telephony Edge"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [([], Bind("0.0.0.0", 8100)), (["--host", "127.0.0.1", "--port", "18100"], Bind("127.0.0.1", 18100))],  # noqa: S104
)
def test_serve_main_parses_args_and_runs(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: Bind
) -> None:
    seen = capture_serve(monkeypatch, serve)
    assert serve.main(argv) == 0
    from va_edge.main import app  # noqa: PLC0415

    assert seen == [(app, expected, {})]
