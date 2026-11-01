"""Single-call calling-hours check (specs/17-batches-and-scheduling.md §7, specs/23 §3).

The destination is open when, in **every** candidate zone of the number, the local time is inside one of the
org's `compliance.calling_hours` windows and, with `compliance.state_quiet_hours`, the destination state's
rule allows it. The state comes from the NANP area code (`va_common.nanp`); non-NANP destinations have no
state rule.
"""

from __future__ import annotations

from datetime import datetime

from va_common.nanp import npa, state_for_npa
from va_compliance.calling_windows import callee_timezones, local_minute, minute_in_windows
from va_compliance.state_rules import StateRule, state_allows, state_rules
from va_domain.org import OrgSettings

__all__ = ["calling_hours_open", "destination_state_rule"]


def destination_state_rule(e164: str) -> StateRule | None:
    """The state rule of a NANP destination's area code, `None` when there is none."""
    area_code = npa(e164)
    state = None if area_code is None else state_for_npa(area_code)
    return None if state is None else state_rules().get(state)


def calling_hours_open(at_utc: datetime, e164: str, *, settings: OrgSettings) -> bool:
    """Whether calling `e164` at `at_utc` respects the org's calling hours (and state rules when enabled)."""
    compliance = settings.compliance
    rule = destination_state_rule(e164) if compliance.state_quiet_hours else None
    for zone in callee_timezones(e164, explicit=None, fallback=settings.timezone):
        weekday, minute = local_minute(at_utc, zone)
        if not minute_in_windows(weekday, minute, compliance.calling_hours):
            return False
        if rule is not None and not state_allows(rule, weekday, minute):
            return False
    return True
