"""The maintenance takeover of an inbound call (specs/16 §4 Decision P05-T12d)."""

from __future__ import annotations

import uuid

import pytest
from media_helpers import call_context

from va_media.maintenance import MAINTENANCE_END, maintenance_takeover


@pytest.mark.parametrize("message", [None, "", "   \n"])
def test_no_takeover_without_a_message(message: str | None) -> None:
    assert maintenance_takeover(call_context(uuid.uuid4(), maintenance_message=message)) is None


def test_the_message_is_spoken_at_once_and_ends_inbound_rejected() -> None:
    message = "  We are closed for maintenance, please call back tomorrow. "
    takeover = maintenance_takeover(call_context(uuid.uuid4(), maintenance_message=message))
    assert takeover is not None
    assert (takeover.reason, takeover.text, takeover.silence_ms, takeover.max_wait_s, takeover.beep) == (
        MAINTENANCE_END,
        message,
        0,
        0.0,
        None,
    )
    assert MAINTENANCE_END == "inbound_rejected"
