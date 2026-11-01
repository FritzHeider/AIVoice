"""The event catalog mirrors specs/19-events-and-webhooks.md §2; stream routing follows §1 step 2."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from va_db.models import CallEventCategory
from va_events import DEFAULT_CATEGORY, STREAMS, EventType, Stream, stream_for

_SPEC = Path(__file__).resolve().parents[3] / "specs" / "19-events-and-webhooks.md"


def _spec_rows() -> dict[str, str | None]:
    text = _SPEC.read_text(encoding="utf-8")
    section = text.split("## 2. Event type catalog", 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    rows: dict[str, str | None] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0].startswith("`"):
            continue
        category = None if cells[1] in {"—", ""} else cells[1]
        for name in re.findall(r"`([a-z_]+\.[a-z_]+)`", cells[0]):
            rows[name] = category
    return rows


def test_every_spec_type_is_an_event_type_and_nothing_else() -> None:
    rows = _spec_rows()
    assert len(rows) > 50
    assert {member.value for member in EventType} == set(rows)


def test_default_categories_match_the_spec_column() -> None:
    rows = _spec_rows()
    assert {member.value: category for member, category in DEFAULT_CATEGORY.items()} == rows


def test_every_default_category_is_a_call_events_category() -> None:
    allowed = set(get_args(CallEventCategory))
    assert {c for c in DEFAULT_CATEGORY.values() if c is not None} <= allowed


@pytest.mark.parametrize(
    ("event_type", "stream"),
    [
        (EventType.CALL_TURN, "calls"),
        (EventType.CALL_COMPLETED, "calls"),
        (EventType.BATCH_IN_PROGRESS, "calls"),
        (EventType.CITATIONS_READY, "calls"),
        (EventType.EVALS_COMPLETED, "calls"),
        (EventType.SMS_RECEIVED, "sms"),
        (EventType.CONVERSATION_HUMAN_TAKEOVER, "sms"),
        (EventType.WIDGET_MESSAGE, "widget"),
        (EventType.BILLING_LOW_BALANCE, "billing"),
        (EventType.NUMBER_PURCHASED, "system"),
        (EventType.TENDLC_CAMPAIGN_STATUS, "system"),
        (EventType.VOICE_CLONE_READY, "system"),
        (EventType.KB_FAILED, "system"),
        (EventType.SYSTEM_FRAUD_SUSPECTED, "system"),
    ],
)
def test_stream_routing_by_prefix(event_type: EventType, stream: Stream) -> None:
    assert stream_for(event_type) == stream


def test_every_type_routes_to_a_known_stream() -> None:
    assert {stream_for(member) for member in EventType} == set(STREAMS)
