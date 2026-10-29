"""An ASGI wrapper that sets the client address the app sees (httpx's ``ASGITransport`` always reports
``127.0.0.1``)."""

from __future__ import annotations

from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["PeerSwitch"]

_PEER_PORT = 50000


@dataclass
class PeerSwitch:
    """Wraps ``app``; while ``host`` is set, HTTP requests arrive from ``(host, 50000)``.

    ``host`` may be any string (``"testclient"`` imitates a peer that is not an IP address).
    """

    app: ASGIApp
    host: str | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.host is not None:
            scope = {**scope, "client": (self.host, _PEER_PORT)}
        await self.app(scope, receive, send)
