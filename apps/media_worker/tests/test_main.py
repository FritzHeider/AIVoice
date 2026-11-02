"""``python -m va_media.main`` wiring and the production container."""

from __future__ import annotations

from functools import partial

import pytest
from media_helpers import FakeMediaContainer
from service_helpers import capture_serve, settings_of

from va_common.service import Bind, StopController
from va_common.time import SystemClock
from va_db.session import Database
from va_media import main as media_main
from va_media.context import DbCallContextLoader
from va_media.settings import MediaSettings


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


async def test_container_builds_redis_and_database_and_disposes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed: list[bool] = []
    original = Database.dispose

    async def record_dispose(self: Database) -> None:
        disposed.append(True)
        await original(self)

    monkeypatch.setattr(Database, "dispose", record_dispose)
    async with media_main.build_container(settings_of(MediaSettings)) as container:
        assert isinstance(container.database, Database)
        assert set(container.readiness_checks()) == {"redis"}
        loader = media_main.db_context_loader(container, SystemClock())
        assert isinstance(loader, DbCallContextLoader)
        assert disposed == []
    assert disposed == [True]


def test_db_context_loader_needs_a_database() -> None:
    with pytest.raises(TypeError, match="no database"):
        media_main.db_context_loader(FakeMediaContainer(), SystemClock())
