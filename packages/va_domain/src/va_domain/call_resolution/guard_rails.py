"""Guard rails forced by org compliance policy (specs/23-guardrails-and-compliance.md §4, §5;
specs/04 §7 `ai_disclosure_required` / `recording_disclosure_required`)."""

from __future__ import annotations

from typing import Final

from va_domain.calls import GuardRail
from va_domain.org import OrgSettings

__all__ = ["with_forced_rails"]

_AI: Final = "tcpa:ai_disclosure"
_RECORDING: Final = "tcpa:recording_disclosure"


def _ai_disclosure_due(org: OrgSettings, ai_disclosure_state: bool) -> bool:
    policy = org.compliance.ai_disclosure_policy
    return org.ai_disclosure_required or policy == "always" or (policy == "auto" and ai_disclosure_state)


def _recording_disclosure_due(org: OrgSettings, record: bool, all_party_consent: bool) -> bool:
    if not record:
        return False
    policy = org.compliance.recording_disclosure_policy
    return org.recording_disclosure_required or policy == "always" or (policy == "auto" and all_party_consent)


def with_forced_rails(
    rails: list[GuardRail],
    org: OrgSettings,
    *,
    record: bool,
    all_party_consent: bool,
    ai_disclosure_state: bool,
) -> list[GuardRail]:
    """`rails` plus the disclosure rails the policy requires and no layer configured (inject mode)."""
    present = {rail.type for rail in rails}
    forced = list(rails)
    if _AI not in present and _ai_disclosure_due(org, ai_disclosure_state):
        forced.append(GuardRail(type=_AI))
    if _RECORDING not in present and _recording_disclosure_due(org, record, all_party_consent):
        forced.append(GuardRail(type=_RECORDING))
    return forced
