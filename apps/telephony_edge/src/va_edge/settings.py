"""Telephony edge settings (specs/02-repo-layout-and-conventions.md §6, variables scoped to ``edge``)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Final

from va_common.settings import DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["SIM_PUBLIC_KEY_FILE", "EdgeSettings"]

# Written by the simulator at startup (MASTER_PROMPT §9; `make up` also copies it into `.env`).
SIM_PUBLIC_KEY_FILE: Final = Path(".sim/telnyx_public_key")


class EdgeSettings(RedisSettings, DatabaseSettings, TemporalSettings):
    """Settings of the Telnyx webhook ingress."""

    service_name: ClassVar[str] = "telephony-edge"

    edge_public_host: str = "localhost:8100"
    media_public_host: str = "localhost:8200"
    telnyx_public_key: str = ""

    def webhook_public_key(self, sim_key_file: Path = SIM_PUBLIC_KEY_FILE) -> str:
        """``TELNYX_PUBLIC_KEY``; in ``local`` an empty value falls back to the simulator's key file.

        Returns ``""`` when neither is available (the edge refuses to start, see ``va_edge.main``).
        """
        key = self.telnyx_public_key.strip()
        if key or not self.is_local or not sim_key_file.is_file():
            return key
        return sim_key_file.read_text(encoding="utf-8").strip()
