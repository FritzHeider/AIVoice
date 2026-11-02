"""Answering machine detection facts shared by the dial and the media worker (specs/06-telephony-telnyx.md §6
step 5, §5 AMD rows; specs/07-media-pipeline.md §11 step 2, §14; specs/07 §14 Decision P05-T10c).

- :func:`amd_mode`: the ``answering_machine_detection`` the dial requests (``premium`` or ``disabled``); the
  media worker derives the same value from the call's config to know whether a verdict will come.
- :func:`classifier_enabled`: whether the media worker runs the ``classify_answer`` LLM classifier
  (``voicemail.sensitive``) on the callee's first words.
- :func:`amd_verdict`: a Telnyx detection result as a verdict the media worker acts on.

Whenever a voicemail action matters (``action != "ignore"`` or ``sensitive``), :func:`amd_mode` is ``premium``
unless ``ivr_mode`` or ``answered_by_enabled=false``; both of those also switch the classifier off, so the
"engine detector when AMD is disabled" case of specs/07 §14 is unreachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from va_domain.calls import SendCallConfig

__all__ = [
    "AMD_DETECTION_EVENTS",
    "AmdMode",
    "AmdVerdict",
    "amd_mode",
    "amd_verdict",
    "classifier_enabled",
    "verdict_of_answered_by",
]

type AmdMode = Literal["premium", "disabled"]
type AmdVerdict = Literal["human", "machine", "fax", "unknown"]

PREMIUM_DETECTION_EVENT: Final = "call.machine.premium.detection.ended"
STANDARD_DETECTION_EVENT: Final = "call.machine.detection.ended"
AMD_DETECTION_EVENTS: Final = frozenset({PREMIUM_DETECTION_EVENT, STANDARD_DETECTION_EVENT})
"""Telnyx event types that carry an AMD verdict."""

_PREMIUM_VERDICTS: Final[Mapping[str, AmdVerdict]] = {
    "human_residence": "human",
    "human_business": "human",
    "machine": "machine",
    "fax_detected": "fax",
}
# The edge stores a standard result normalized (``human`` → ``human_residence``), so both spellings map.
_STANDARD_VERDICTS: Final[Mapping[str, AmdVerdict]] = {
    "human": "human",
    "human_residence": "human",
    "machine": "machine",
}
_ANSWERED_BY_VERDICTS: Final[Mapping[str, AmdVerdict]] = {
    "human": "human",
    "voicemail": "machine",
    "fax": "fax",
}


def amd_mode(config: SendCallConfig) -> AmdMode:
    """``premium`` unless IVR mode, ``answered_by_enabled=false``, or a non-sensitive ``ignore`` voicemail."""
    if config.ivr_mode or not config.answered_by_enabled:
        return "disabled"
    if config.voicemail.action == "ignore" and not config.voicemail.sensitive:
        return "disabled"
    return "premium"


def classifier_enabled(config: SendCallConfig, direction: str) -> bool:
    """Whether the ``classify_answer`` classifier runs: outbound, ``voicemail.sensitive``, not ``ivr_mode``,
    and ``answered_by_enabled``."""
    return (
        direction == "outbound"
        and config.voicemail.sensitive
        and not config.ivr_mode
        and config.answered_by_enabled
    )


def amd_verdict(event_type: str, result: str | None) -> AmdVerdict | None:
    """The verdict of a detection event's ``result`` (``unknown`` for ``silence``, ``not_sure`` or anything
    unexpected); ``None`` for event types that carry no verdict."""
    if event_type == PREMIUM_DETECTION_EVENT:
        return _PREMIUM_VERDICTS.get(result or "", "unknown")
    if event_type == STANDARD_DETECTION_EVENT:
        return _STANDARD_VERDICTS.get(result or "", "unknown")
    return None


def verdict_of_answered_by(answered_by: str) -> AmdVerdict:
    """The verdict behind a stored ``answered_by`` (``voicemail`` → ``machine``; others → ``unknown``)."""
    return _ANSWERED_BY_VERDICTS.get(answered_by, "unknown")
