"""``CLICKHOUSE_URL`` parsing and the client context manager."""

from __future__ import annotations

from typing import Any, cast

import pytest

from va_events.clickhouse import ClickHouseTarget, open_clickhouse, parse_clickhouse_url
from va_events.clickhouse import client as client_module


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://default:@localhost:8123/va",
            ClickHouseTarget("localhost", 8123, "default", "", False, "va"),
        ),
        ("https://ch.example.com/va", ClickHouseTarget("ch.example.com", 8443, "default", "", True, "va")),
        ("http://ch:9123", ClickHouseTarget("ch", 9123, "default", "", False, "default")),
        ("http://sink:p%40ss%2Fw@ch:8123/", ClickHouseTarget("ch", 8123, "sink", "p@ss/w", False, "default")),
        ("http://us%20er@ch/analytics", ClickHouseTarget("ch", 8123, "us er", "", False, "analytics")),
    ],
)
def test_parse_clickhouse_url(url: str, expected: ClickHouseTarget) -> None:
    assert parse_clickhouse_url(url) == expected


@pytest.mark.parametrize("url", ["clickhouse://u:hunter2@ch:9000/va", "http:///va", "hunter2"])
def test_parse_clickhouse_url_rejects_non_http_urls_without_echoing_them(url: str) -> None:
    with pytest.raises(ValueError, match="http") as raised:
        parse_clickhouse_url(url)
    assert "hunter2" not in str(raised.value)


def test_target_repr_hides_the_password() -> None:
    assert "hunter2" not in repr(parse_clickhouse_url("http://u:hunter2@ch:8123/va"))


class RecordingClient:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_open_clickhouse_connects_to_the_default_database_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []
    client = RecordingClient()

    async def fake_create(**kwargs: Any) -> RecordingClient:
        created.append(kwargs)
        return client

    monkeypatch.setattr(client_module, "create_async_client", fake_create)
    with pytest.raises(RuntimeError, match="boom"):
        async with open_clickhouse("https://u:s3cret@ch:8443/va", connect_timeout_s=2.0) as opened:
            assert opened is cast("object", client)
            assert client.closed == 0
            raise RuntimeError("boom")
    assert client.closed == 1
    assert created == [
        {
            "host": "ch",
            "port": 8443,
            "username": "u",
            "password": "s3cret",
            "secure": True,
            "database": "default",
            "connect_timeout": 2.0,
            "send_receive_timeout": 30.0,
        }
    ]
