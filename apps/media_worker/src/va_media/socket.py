"""The media WebSocket as the session sees it: receive text/binary messages, send text, close once.

:class:`MediaSocket` is the narrow surface a call session needs; :class:`StarletteSocket` adapts an accepted
Starlette ``WebSocket`` to it (tests substitute in-memory fakes).
"""

from __future__ import annotations

import contextlib
from typing import Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

__all__ = ["MediaSocket", "SocketClosedError", "StarletteSocket"]


class SocketClosedError(Exception):
    """The peer closed the media WebSocket (``code`` = its close code)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"media socket closed by the peer ({code})")
        self.code = code


class MediaSocket(Protocol):
    async def receive(self) -> str | bytes:
        """The next message; raises :class:`SocketClosedError` once the peer has closed."""
        ...

    async def send_text(self, data: str) -> None:
        """Send one text message; raises :class:`SocketClosedError` once the socket is no longer open."""
        ...

    async def close(self, code: int) -> None:
        """Close with ``code`` unless already closed by either side; never raises."""
        ...


class StarletteSocket:
    """An accepted Starlette ``WebSocket`` as a :class:`MediaSocket`."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self.close_code: int | None = None

    @property
    def open(self) -> bool:
        return (
            self._ws.client_state is WebSocketState.CONNECTED
            and self._ws.application_state is WebSocketState.CONNECTED
        )

    async def receive(self) -> str | bytes:
        if self._ws.client_state is not WebSocketState.CONNECTED:
            raise SocketClosedError(self.close_code or 1006)
        message = await self._ws.receive()
        if message["type"] == "websocket.disconnect":
            self.close_code = int(message.get("code", 1005))
            raise SocketClosedError(self.close_code)
        text = message.get("text")
        if text is not None:
            return str(text)
        return bytes(message.get("bytes") or b"")

    async def send_text(self, data: str) -> None:
        if not self.open:
            raise SocketClosedError(self.close_code or 1006)
        # A peer that vanished mid-send surfaces as WebSocketDisconnect (Starlette wraps the server's
        # OSError), RuntimeError (a send after the close message), or a bare OSError.
        try:
            await self._ws.send_text(data)
        except WebSocketDisconnect as exc:
            self.close_code = self.close_code or exc.code
            raise SocketClosedError(self.close_code) from exc
        except (RuntimeError, OSError) as exc:
            raise SocketClosedError(self.close_code or 1006) from exc

    async def close(self, code: int) -> None:
        if not self.open:
            return
        # The peer may vanish between the state check and the send (Starlette turns the server's OSError into
        # WebSocketDisconnect); the socket is closed either way.
        with contextlib.suppress(WebSocketDisconnect):
            await self._ws.close(code)
        self.close_code = code
