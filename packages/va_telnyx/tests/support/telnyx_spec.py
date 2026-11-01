"""Parsers of specs/06-telephony-telnyx.md tables shared by the va_telnyx and telephony-edge tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

__all__ = ["SPEC_06", "spec_voice_event_types"]

SPEC_06: Final = Path(__file__).resolve().parents[4] / "specs" / "06-telephony-telnyx.md"


def spec_voice_event_types() -> set[str]:
    """Every ``event_type`` in the first column of the specs/06 §5 table (``/ `.suffix``` abbreviations
    expanded)."""
    text = SPEC_06.read_text(encoding="utf-8")
    section = text.split("## 5. Voice webhook", 1)[1].split("### 5.1", 1)[0]
    found: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        previous = ""
        for name in re.findall(r"`([^`]+)`", first_cell):
            full = previous.rsplit(".", 1)[0] + name if name.startswith(".") else name
            found.add(full)
            previous = full
    return found
