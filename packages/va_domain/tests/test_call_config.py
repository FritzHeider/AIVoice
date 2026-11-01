"""`SendCallConfig` matches the specs/05 §4.4 block field for field, and round-trips through JSON."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from va_domain.calls import config, parts
from va_domain.calls.config import SendCallConfig

SPEC = Path(__file__).resolve().parents[3] / "specs" / "05-calls-api.md"
_CLASS = re.compile(r"^class (\w+)\(BaseModel\):$")
_FIELD = re.compile(r"^(\w+): ([^=]+?)(?: = (.+))?$")
_MISSING = object()


def _spec_block() -> str:
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("### 4.4") :]
    return section[
        section.index("```python") + len("```python") : section.index("```\n", section.index("```python"))
    ]


def _spec_classes() -> dict[str, list[tuple[str, str, object]]]:
    """Class name -> [(field, type text, default literal or _MISSING)] in spec order."""
    classes: dict[str, list[tuple[str, str, object]]] = {}
    current: list[tuple[str, str, object]] = []
    for raw in _spec_block().splitlines():
        line = raw.split("#", 1)[0].strip()
        header = _CLASS.match(line)
        if header:
            current = classes.setdefault(header.group(1), [])
            continue
        for statement in filter(None, (part.strip() for part in line.split(";"))):
            match = _FIELD.match(statement)
            if match:
                current.append((match.group(1), match.group(2).strip(), _literal(match.group(3))))
    return classes


def _literal(text: str | None) -> object:
    if text is None:
        return _MISSING
    try:
        return ast.literal_eval(text)
    except ValueError:
        return text  # a constructor call such as DialingStrategy()


SPEC_CLASSES = _spec_classes()
MODELS: dict[str, type[BaseModel]] = {
    name: getattr(config, name, None) or getattr(parts, name) for name in SPEC_CLASSES
}


def test_spec_block_lists_every_model() -> None:
    assert set(SPEC_CLASSES) == {
        "PronunciationRule",
        "VoicemailConfig",
        "RetryConfig",
        "DialingStrategy",
        "WarmTransferConfig",
        "GuardRailAction",
        "GuardRail",
        "PostCallEvals",
        "SendCallConfig",
    }
    assert len(SPEC_CLASSES["SendCallConfig"]) == 72


@pytest.mark.parametrize("name", sorted(SPEC_CLASSES))
def test_field_names_match_in_order(name: str) -> None:
    assert list(MODELS[name].model_fields) == [field for field, _, _ in SPEC_CLASSES[name]]


@pytest.mark.parametrize("name", sorted(SPEC_CLASSES))
def test_required_fields_and_defaults_match(name: str) -> None:
    model = MODELS[name]
    for field, _, default in SPEC_CLASSES[name]:
        info = model.model_fields[field]
        if default is _MISSING:
            assert info.is_required(), field
            continue
        assert not info.is_required(), field
        actual = info.get_default(call_default_factory=True)
        if isinstance(default, str) and default.endswith("()"):
            assert actual == MODELS[default[:-2]](), field
        else:
            assert actual == default, field


def _resolved(**overrides: Any) -> SendCallConfig:
    base: dict[str, Any] = {
        "runner_type": "task",
        "task": "Confirm the booking.",
        "phone_number": "+14155550100",
        "voice_id": "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b7a",
        "provider_voice_id": "XrExE9yKIg1WjnnlVkGX",
        "voice_settings": {"speed": 1.1},
        "dialogue_model": "zai-org/GLM-5.3-Flash",
        "router_model": "zai-org/GLM-5.3-Flash",
        "from_number": "+14155550101",
        "phone_number_id": None,
        "dispositions": ["INTERESTED", "NOT_INTERESTED"],
    }
    return SendCallConfig.model_validate({**base, **overrides})


def test_json_round_trip_keeps_every_value() -> None:
    cfg = _resolved(
        voicemail={
            "action": "leave_message_and_sms",
            "message": "m",
            "sms": {"message": "s", "from": "+14155550102"},
        },
        retry={"wait": 600, "on_no_answer": True},
        dialing_strategy={"type": "custom_pooling", "pool_id": "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b7b"},
        guard_rails=[{"type": "custom", "name": "n", "description": "d", "action": {"type": "end_call"}}],
        inline_tools=[{"name": "lookup", "description": "d", "url": "https://api.acme.test/{{id}}"}],
        dynamic_data=[
            {"url": "https://crm.acme.test", "response_data": [{"name": "plan", "data": "$.plan"}]}
        ],
        start_time="2026-11-20T17:30:00Z",
        request_data={"name": "Jordan", "n": [1, 2]},
    )
    dumped = cfg.model_dump(mode="json", by_alias=True)
    assert dumped["voicemail"]["sms"] == {"message": "s", "from": "+14155550102"}
    assert SendCallConfig.model_validate(dumped) == cfg


def test_defaults_of_the_minimal_config() -> None:
    cfg = _resolved()
    assert cfg.voice_id == UUID("0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b7a")
    assert cfg.voicemail.action == "hangup"
    assert cfg.voicemail.detect_timeout_ms == 12000
    assert cfg.warm_transfer.timeout_s == 45
    assert cfg.error_phrase.startswith("I'm sorry, I'm having trouble on my end.")


def test_unknown_config_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="extra"):
        _resolved(surprise=True)
