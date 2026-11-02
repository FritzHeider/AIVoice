"""``va_voice.pipeline.waits`` re-exports the shared clock-driven waits of ``va_common.waits``."""

from __future__ import annotations

from va_common.waits import wait_until_set, within
from va_voice.pipeline import waits


def test_the_pipeline_module_re_exports_the_shared_waits() -> None:
    assert (waits.within, waits.wait_until_set) == (within, wait_until_set)
