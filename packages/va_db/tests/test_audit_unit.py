"""``va_db.audit`` without a database: redaction, the action list, IP parsing (specs/04 §13)."""

from __future__ import annotations

import enum
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any, Final, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.audit import AUDIT_ACTIONS, REDACTED, AuditActor, parse_ip, record, redact

SPEC: Final = Path(__file__).resolve().parents[3] / "specs" / "04-auth-tenancy-rbac.md"
API_KEY: Final = "va_live_" + "Ab3" * 10 + "Cd"


class Colour(enum.Enum):
    RED = "red"


def spec_actions() -> set[str]:
    """The backticked names of the §13 "Actions logged (complete list)" bullet."""
    section = SPEC.read_text(encoding="utf-8").split("## 13. Audit logging", 1)[1].split("\n## ", 1)[0]
    bullet = section.split("Actions logged (complete list):", 1)[1].split("\n- ", 1)[0]
    return set(re.findall(r"`([a-z_]+\.[a-z_]+)`", bullet))


def test_actions_mirror_the_spec_list() -> None:
    assert spec_actions() == AUDIT_ACTIONS
    assert {"api_key.update", "invitation.accept"} <= AUDIT_ACTIONS


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "Secret",
        "TOKEN",
        "api_key",
        "client_secret",
        "Authorization",
        "secret_headers",
        "secret_hash",
        "key",
        "client_secret_enc",
        "Payload_ENC",
    ],
)
def test_secret_names_lose_their_whole_value(name: str) -> None:
    assert redact({name: {"nested": 1}, "kept": "x"}) == {name: REDACTED, "kept": "x"}


def test_redaction_reaches_nested_dicts_and_lists_without_mutating() -> None:
    value: dict[str, Any] = {
        "headers": [{"authorization": "Basic abc"}, {"accept": "json"}],
        "config": {"webhook": {"token": "t-1", "url": "https://hooks.acme-dental.test"}},
    }
    assert redact(value) == {
        "headers": [{"authorization": REDACTED}, {"accept": "json"}],
        "config": {"webhook": {"token": REDACTED, "url": "https://hooks.acme-dental.test"}},
    }
    assert value["config"]["webhook"]["token"] == "t-1"  # noqa: S105 - the unredacted test value


def test_strings_holding_an_api_key_are_redacted_anywhere() -> None:
    assert redact({"note": f"Bearer {API_KEY}", "names": ["ok", API_KEY]}) == {
        "note": REDACTED,
        "names": ["ok", REDACTED],
    }
    assert redact({"prefix": API_KEY[:12]}) == {"prefix": API_KEY[:12]}


def test_values_become_json_safe() -> None:
    row_id = uuid.UUID("0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e0f")
    moment = datetime(2026, 10, 29, 14, 3, 11, tzinfo=UTC)
    assert redact(
        {
            "id": row_id,
            "at": moment,
            "day": date(2026, 10, 29),
            "amount": Decimal("1.50"),
            "ip": IPv4Address("203.0.113.24"),
            "colour": Colour.RED,
            "tags": ("a", "b"),
            "flags": [True, None, 3, 1.5],
        }
    ) == {
        "id": str(row_id),
        "at": "2026-10-29T14:03:11+00:00",
        "day": "2026-10-29",
        "amount": "1.50",
        "ip": "203.0.113.24",
        "colour": "red",
        "tags": ["a", "b"],
        "flags": [True, None, 3, 1.5],
    }
    assert redact(None) is None


def test_unsupported_values_fail_loudly() -> None:
    with pytest.raises(TypeError, match="cannot hold bytes"):
        redact({"blob": b"\x00"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("203.0.113.24", IPv4Address("203.0.113.24")),
        ("2001:db8::1", IPv6Address("2001:db8::1")),
        ("testclient", None),
        (None, None),
    ],
)
def test_parse_ip(value: str | None, expected: object) -> None:
    assert parse_ip(value) == expected


async def test_record_rejects_an_unknown_action_before_touching_the_session() -> None:
    actor = AuditActor(org_id=uuid.uuid4(), actor_type="system", actor_id=None)
    with pytest.raises(ValueError, match=r"unknown audit action: 'org\.renamed'"):
        await record(cast(AsyncSession, object()), actor, "org.renamed", "org", None)
