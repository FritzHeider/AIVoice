"""Bland-style Send Call bodies (one per parameter group) parse with `SendCallRequest` (specs/05 §3, §10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from va_domain.calls import SendCallRequest, unknown_fields
from va_domain.phone import normalize_e164

FIXTURES = Path(__file__).parent / "fixtures" / "bland_send_call"
FILES = sorted(FIXTURES.glob("*.json"))
# Bland-only parameters a fixture carries on purpose to prove they are ignored.
EXPECTED_UNKNOWN = {"12_legacy_aliases.json": ["reduce_latency"]}
RUNNERS = {
    "02_pathway_runner.json": "pathway",
    "03_persona_settings.json": "persona",
    "04_agent_runner.json": "agent",
}


def _load(path: Path) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


def test_there_are_twelve_fixtures() -> None:
    assert len(FILES) == 12


@pytest.mark.parametrize("path", FILES, ids=[path.name for path in FILES])
def test_fixture_parses_and_passes_the_rules(path: Path) -> None:
    raw = _load(path)
    request = SendCallRequest.model_validate(raw)
    request.check_rules()
    assert unknown_fields(raw) == EXPECTED_UNKNOWN.get(path.name, [])
    assert request.runner_type == RUNNERS.get(path.name, "task")
    assert normalize_e164(request.phone_number, request.country).startswith("+1415555")


def test_legacy_fixture_folds_its_aliases() -> None:
    request = SendCallRequest.model_validate(_load(FIXTURES / "12_legacy_aliases.json"))
    fields = request.explicit_fields()
    assert fields["ivr_mode"] is True
    assert fields["dispositions"] == ["BALANCE_OBTAINED", "UNREACHABLE"]
    assert fields["dialing_strategy"].type == "local"
    assert not {"amd", "local_dialing", "geospatial_dialing", "available_tags"} & set(fields)


def test_tools_fixture_merges_knowledge_bases() -> None:
    request = SendCallRequest.model_validate(_load(FIXTURES / "08_tools_dynamic_data.json"))
    tools = request.explicit_fields()["tools"]
    assert tools[-1] == "KB-0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b07"
    assert len(tools) == 4
