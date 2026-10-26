"""Production entry point of the edge image (specs/29-infrastructure.md §1.1: ``python -m va_edge.serve``).

Runs :data:`va_edge.main.app` through the shared runner (``va_common.service.serve``), which owns
SIGTERM/SIGINT and shuts uvicorn down gracefully.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from va_common.service import parse_bind, serve

__all__ = ["DEFAULT_PORT", "main"]

DEFAULT_PORT: Final = 8100


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=DEFAULT_PORT, description="VoiceAgents telephony edge")
    from va_edge.main import app  # noqa: PLC0415 - `--help` and bad arguments exit before the app is built

    return serve(app, bind)


if __name__ == "__main__":
    raise SystemExit(main())
