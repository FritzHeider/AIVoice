"""Clock-driven waits of the pipeline; the implementation lives in :mod:`va_common.waits` (shared with the
telephony edge, which must not depend on va_voice)."""

from __future__ import annotations

from va_common.waits import wait_until_set, within

__all__ = ["wait_until_set", "within"]
