"""`OrgSettings` defaults follow the specs/04 §7 tables plus the additions pinned by its P02-T12a Decision."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from va_domain.org import SETTINGS_GROUPS, OrgSettings

SPEC_04 = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"

# Settings other specs read (Decision "OrgSettings fields (P02-T12a)" item 2, and item 1b for `redaction`).
ADDITIONS: dict[str, Any] = {
    "compliance.dnc_inherit_from_parent": True,
    "compliance.state_quiet_hours": True,
    "compliance.max_attempts_per_number_per_day": 0,
    "compliance.recording_disclosure_policy": "auto",
    "compliance.ai_disclosure_policy": "always",
    "compliance.require_consent_attestation": True,
    "redaction.redact_llm_context": False,
    "analysis.sentiment": True,
    "delete_carrier_recordings": True,
    "cnam_display_name": None,
    "batch_concurrency_share": 1.0,
    "sms.smart_encoding": True,
}

_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_OPTIONAL_KEY = re.compile(r"(\w+)\?:")


def _section() -> str:
    text = SPEC_04.read_text(encoding="utf-8")
    start = text.index("## 7. OrgSettings model")
    return text[start : text.index("\n## 8. ", start)]


def _rows() -> list[list[str]]:
    return [
        [cell.strip() for cell in _UNESCAPED_PIPE.split(line.strip().strip("|"))]
        for line in _section().splitlines()
        if line.startswith("| `")
    ]


def _default(cell: str) -> Any:
    """The value of a Default cell: its leading backticked token, a spaced integer, or a JSON literal."""
    if cell.startswith("`"):
        token = cell[1 : cell.index("`", 1)]
        try:
            return json.loads(token)
        except json.JSONDecodeError:
            return token
    if re.fullmatch(r"\d[\d ]*", cell):
        return int(cell.replace(" ", ""))
    return json.loads(cell.split(" ", 1)[0])


def _spec_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for name_cell, type_cell, default_cell, *_ in _rows():
        path = name_cell.strip("`")
        if path in SETTINGS_GROUPS:
            # The `llm` row lists the optional override keys of its group; each defaults to null.
            defaults.update({f"{path}.{key}": None for key in _OPTIONAL_KEY.findall(type_cell)})
        else:
            defaults[path] = _default(default_cell)
    return defaults


def _model_defaults() -> dict[str, Any]:
    dumped = OrgSettings().model_dump(mode="json")
    flat: dict[str, Any] = {}
    for name, value in dumped.items():
        if name in SETTINGS_GROUPS:
            flat.update({f"{name}.{key}": sub for key, sub in value.items()})
        else:
            flat[name] = value
    return flat


SPEC_DEFAULTS = _spec_defaults()
MODEL_DEFAULTS = _model_defaults()


def test_the_table_parser_reads_both_tables() -> None:
    assert SPEC_DEFAULTS["timezone"] == "America/Los_Angeles"
    assert SPEC_DEFAULTS["low_balance_threshold_micros"] == 5_000_000
    assert SPEC_DEFAULTS["llm.context_budget_tokens"] == 6000
    assert SPEC_DEFAULTS["llm.base_url"] is None
    assert SPEC_DEFAULTS["default_voice"] == "Karen"
    assert SPEC_DEFAULTS["experiments.sticky_by_contact"] is False


def test_additions_are_not_in_the_tables() -> None:
    assert set(ADDITIONS).isdisjoint(SPEC_DEFAULTS)


def test_every_model_path_is_specified_and_every_specified_path_exists() -> None:
    assert set(MODEL_DEFAULTS) == set(SPEC_DEFAULTS) | set(ADDITIONS)


@pytest.mark.parametrize("path", sorted(SPEC_DEFAULTS))
def test_model_default_matches_the_table(path: str) -> None:
    assert MODEL_DEFAULTS[path] == SPEC_DEFAULTS[path]


@pytest.mark.parametrize("path", sorted(ADDITIONS))
def test_model_default_matches_the_pinned_addition(path: str) -> None:
    assert MODEL_DEFAULTS[path] == ADDITIONS[path]
    assert f"`{path}`" in _section()


def test_the_top_level_calling_hours_row_was_replaced() -> None:
    names = [row[0] for row in _rows()]
    assert "`calling_hours`" not in names
    assert "`compliance.calling_hours`" in names


def test_groups_are_exactly_the_sub_model_fields() -> None:
    sub_models = {
        name
        for name, field in OrgSettings.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)
    }
    assert sub_models == SETTINGS_GROUPS
    assert all(f"`{name}: " in _section() for name in SETTINGS_GROUPS)


def test_every_field_is_documented() -> None:
    models: list[type[BaseModel]] = [OrgSettings]
    models += [OrgSettings.model_fields[name].annotation for name in sorted(SETTINGS_GROUPS)]  # type: ignore[misc]
    missing = [
        f"{model.__name__}.{name}"
        for model in models
        for name, field in model.model_fields.items()
        if not field.description
    ]
    assert missing == []
