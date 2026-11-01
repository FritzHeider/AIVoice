"""The call policies equal the specs/10 §2.1 table (Decision P03-T13a rule 5)."""

from __future__ import annotations

import re
from pathlib import Path

from va_llm.policy import CALL_TYPES, POLICIES, CallType

SPEC = Path(__file__).resolve().parents[3] / "specs" / "10-llm-gateway.md"
# §2.1 row label -> call type
ROWS: dict[str, CallType] = {
    "Dialogue": "dialogue",
    "Router (`select_path`)": "router",
    "Loop condition": "loop_condition",
    "Extraction": "extraction",
    "Voicemail classifier": "voicemail",
    "Guard-rail classifier": "guard_rails",
    "Analysis (post-call)": "analysis",
}


def _spec_timeouts() -> dict[CallType, float]:
    section = SPEC.read_text(encoding="utf-8").split("### 2.1 ", 1)[1].split("\n### ", 1)[0]
    timeouts = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in ROWS:
            ms = re.search(r"([\d ]+) ms", cells[1])
            seconds = float(ms.group(1).replace(" ", "")) / 1000 if ms else float(cells[1].removesuffix(" s"))
            timeouts[ROWS[cells[0]]] = seconds
    return timeouts


def test_every_call_type_has_a_policy_with_the_spec_timeout() -> None:
    assert set(POLICIES) == set(CALL_TYPES) == set(ROWS.values())
    assert {name: policy.timeout_s for name, policy in POLICIES.items()} == _spec_timeouts()


def test_role_models_retries_and_fallback() -> None:
    assert POLICIES["dialogue"].role == "dialogue"
    assert POLICIES["analysis"].role == "analysis"
    classifiers: tuple[CallType, ...] = ("router", "loop_condition", "extraction", "voicemail", "guard_rails")
    for name in classifiers:
        policy = POLICIES[name]
        assert (policy.role, policy.retry_delays_s, policy.per_attempt, policy.falls_back) == (
            "router",
            (0.0,),
            False,
            False,
        )
    assert (POLICIES["dialogue"].retry_delays_s, POLICIES["dialogue"].falls_back) == ((), True)
    analysis = POLICIES["analysis"]
    assert (analysis.retry_delays_s, analysis.per_attempt, analysis.falls_back) == ((2.0, 8.0), True, True)
