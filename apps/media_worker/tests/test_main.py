"""``python -m va_media.main`` wiring."""

from __future__ import annotations

from functools import partial

import pytest
from service_helpers import capture_serve

from va_common.service import Bind, StopController
from va_media import main as media_main


def test_main_parses_args_and_serves_with_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, media_main)
    assert media_main.main(["--port", "18200"]) == 0
    [(app, bind, kwargs)] = seen
    assert bind == Bind("0.0.0.0", 18200)  # noqa: S104
    assert kwargs["stop"] is app.state.stop
    assert isinstance(kwargs["stop"], StopController)
    drain = kwargs["drain"]
    assert isinstance(drain, partial)
    assert drain.func is media_main.drain
    assert drain.args == (app,)
