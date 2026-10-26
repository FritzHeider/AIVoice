from __future__ import annotations

import time
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_common.errors import AppError
from va_common.ids import PublicPrefix, from_public, is_public, new_id, parse_public, to_public

SAMPLE = uuid.UUID("0190f1b2-3c4d-7e5f-8a6b-9c0d1e2f3a4b")


def _uuid7_millis(value: uuid.UUID) -> int:
    return value.int >> 80


def test_new_id_is_uuid7_with_current_timestamp() -> None:
    before = int(time.time() * 1000)
    value = new_id()
    after = int(time.time() * 1000)
    assert isinstance(value, uuid.UUID)
    assert value.version == 7
    assert before <= _uuid7_millis(value) <= after


def test_new_id_is_unique() -> None:
    assert len({new_id() for _ in range(1000)}) == 1000


def test_to_public_is_canonical_lower_case() -> None:
    assert to_public(PublicPrefix.TOOL, SAMPLE) == "TL-0190f1b2-3c4d-7e5f-8a6b-9c0d1e2f3a4b"
    assert to_public(PublicPrefix.KNOWLEDGE_BASE, SAMPLE).startswith("KB-")
    assert to_public(PublicPrefix.PROMPT, SAMPLE).startswith("PT-")


@given(st.uuids(), st.sampled_from(PublicPrefix))
def test_public_id_round_trip(value: uuid.UUID, prefix: PublicPrefix) -> None:
    public = to_public(prefix, value)
    assert from_public(public, prefix) == value
    assert parse_public(public) == (prefix, value)
    assert is_public(public, prefix)


def test_upper_case_uuid_part_is_accepted() -> None:
    assert from_public("KB-" + str(SAMPLE).upper(), PublicPrefix.KNOWLEDGE_BASE) == SAMPLE


@pytest.mark.parametrize(
    "value",
    [
        f"KB-{SAMPLE}",
        f"tl-{SAMPLE}",
        f"TL{SAMPLE}",
        f"TL-{SAMPLE.hex}",
        "TL-not-a-uuid",
        f"TL-{{{SAMPLE}}}",
        f"TL-{SAMPLE} ",
        f"XX-{SAMPLE}",
        "",
    ],
)
def test_from_public_rejects_malformed(value: str) -> None:
    with pytest.raises(AppError) as info:
        from_public(value, PublicPrefix.TOOL)
    assert info.value.code == "INVALID_INPUT"
    assert info.value.http_status == 400
    assert info.value.message.startswith("Invalid TL- id: ")
    assert not is_public(value, PublicPrefix.TOOL)


def test_error_message_truncates_long_input() -> None:
    with pytest.raises(AppError) as info:
        from_public("TL-" + "x" * 5000, PublicPrefix.TOOL)
    assert len(info.value.message) < 200
    assert "…" in info.value.message


def test_parse_public_rejects_unknown_prefix() -> None:
    with pytest.raises(AppError) as info:
        parse_public(f"XX-{SAMPLE}")
    assert info.value.code == "INVALID_INPUT"
    assert info.value.message.startswith("Invalid public id: ")
