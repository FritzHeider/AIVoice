"""``INTERNATIONAL_ALLOWED_COUNTRIES`` on the api (specs/02 §6, specs/24 §6 item 7)."""

from __future__ import annotations

import pytest
from api_helpers import make_settings

DEFAULT = "US,CA,GB,AU,IE,NZ,DE,FR,ES,IT,NL,BE,SE,NO,DK,FI,MX,BR,IN,PH,SG"


def test_default_allowed_destination_countries() -> None:
    settings = make_settings()
    assert settings.international_allowed_countries == DEFAULT
    assert settings.allowed_destination_countries() == frozenset(DEFAULT.split(","))


def test_allowed_destination_countries_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNATIONAL_ALLOWED_COUNTRIES", " us, ca ,,gb")
    assert make_settings().allowed_destination_countries() == frozenset({"US", "CA", "GB"})
