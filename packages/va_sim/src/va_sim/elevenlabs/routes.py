"""Everything FakeElevenLabs serves beyond the shared ``/sim/*`` control API (specs/30 §3.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import BaseRoute, WebSocketRoute
from starlette.websockets import WebSocket

from va_sim.elevenlabs.control import elevenlabs_control_routes
from va_sim.elevenlabs.tts_rest import tts_rest_routes
from va_sim.elevenlabs.tts_ws import tts_websocket

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["elevenlabs_routes"]


def elevenlabs_routes(state: SimState) -> list[BaseRoute]:
    """The TTS WebSocket, REST synthesis, and the ``/sim/elevenlabs/*`` control routes."""

    async def multi_stream_input(websocket: WebSocket) -> None:
        await tts_websocket(state, websocket)

    return [
        WebSocketRoute("/v1/text-to-speech/{voice_id}/multi-stream-input", multi_stream_input),
        *tts_rest_routes(state),
        *elevenlabs_control_routes(state),
    ]
