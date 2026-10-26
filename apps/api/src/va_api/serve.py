"""Production entry point of the api image (specs/29-infrastructure.md §1.1: ``python -m va_api.serve``).

Runs :data:`va_api.main.app` through the shared runner (``va_common.service.serve``), which owns
SIGTERM/SIGINT and shuts uvicorn down gracefully.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from va_common.service import parse_bind, serve

__all__ = ["DEFAULT_PORT", "main"]

DEFAULT_PORT: Final = 8000


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=DEFAULT_PORT, description="VoiceAgents API")
    from va_api.main import app  # noqa: PLC0415 - `--help` and bad arguments exit before the app is built

    return serve(app, bind)


if __name__ == "__main__":
    raise SystemExit(main())
