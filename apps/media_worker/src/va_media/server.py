"""``/ws/telnyx``: the Telnyx media WebSocket (specs/07-media-pipeline.md §2, §3 steps 1-4 and 6; specs/07 §3
Decisions P05-T07a and P05-T07b).

Close codes go out after ``accept()`` (a refusal before it is an HTTP 403 without a code), and a refused
stream is closed without reading a message. Order: authentication (4401), a secondary leg (4421: it must
attach to the owner's session, which arrives with warm transfers), draining (4503), a call already attached
here (4409), capacity (4503), the Redis ownership lock held elsewhere (4409) or Redis unavailable (1011),
then the Telnyx ``start`` within 5 s (else 1008), then the call context with the ``calls.status`` check
(unknown call or ended call 4401, database/Redis failure 1011), the control + carrier channel subscription
(1011 when it fails), the ``carrier_state`` snapshot (1011 on a Redis error), and finally the
:class:`~va_media.session.CallSession`. Every refusal after the lock releases it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from va_common.logging import bound_context, get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_media.auth import MediaAuth, MediaAuthError
from va_media.calls import CallRegistry
from va_media.carrier_state import read_carrier_state
from va_media.channels import CallFeed, ChannelError, ChannelHub
from va_media.context import CallContextLoader, ContextRefusedError
from va_media.control import ControlHandler
from va_media.drain import DrainState
from va_media.ownership import CallOwnership
from va_media.session import CallSession, SessionBody, SessionEnder, StreamStart
from va_media.sessions import LiveSessions
from va_media.socket import StarletteSocket
from va_media.stream import StreamStartError, wait_for_start

__all__ = [
    "CLOSE_AUTH",
    "CLOSE_DUPLICATE",
    "CLOSE_MISROUTED",
    "CLOSE_POLICY",
    "CLOSE_REDIS",
    "CLOSE_UNAVAILABLE",
    "TELNYX_PATH",
    "MediaAttach",
    "telnyx_media_route",
]

TELNYX_PATH: Final = "/ws/telnyx"
CLOSE_POLICY: Final = 1008
CLOSE_REDIS: Final = 1011
CLOSE_AUTH: Final = 4401
CLOSE_DUPLICATE: Final = 4409
CLOSE_MISROUTED: Final = 4421
CLOSE_UNAVAILABLE: Final = 4503

_log = get_logger(__name__)


@dataclass(slots=True)
class MediaAttach:
    """Everything the ``/ws/telnyx`` handler needs; built by the app lifespan."""

    auth: MediaAuth
    registry: CallRegistry
    drain: DrainState
    redis: Redis
    pod_id: str
    clock: Clock
    metrics: ServiceMetrics
    body: SessionBody
    loader: CallContextLoader
    hub: ChannelHub
    sessions: LiveSessions
    control_handler: ControlHandler
    ender: SessionEnder | None = None

    async def _refuse(self, socket: StarletteSocket, code: int, reason: str, *, detail: str = "") -> None:
        self.metrics.counter("va_media_admission_rejected_total").labels(reason=reason).inc()
        _log.warning("media.ws_rejected", reason=reason, detail=detail or reason, close_code=code)
        await socket.close(code)

    async def handle(self, websocket: WebSocket) -> None:
        """Authenticate, admit, lock, wait for ``start``, then run the session."""
        await websocket.accept()
        socket = StarletteSocket(websocket)
        raw_query = bytes(websocket.scope.get("query_string", b"")).decode("latin-1")
        try:
            claims = self.auth.verify(
                str(websocket.scope["path"]), raw_query, websocket.headers, self.clock.now()
            )
        except MediaAuthError as exc:
            await self._refuse(socket, CLOSE_AUTH, "auth", detail=exc.reason)
            return
        call_id = str(claims.call_id)
        with bound_context(call_id=call_id, leg=claims.leg):
            if claims.leg != "primary":
                await self._refuse(socket, CLOSE_MISROUTED, "misrouted_leg")
            elif self.drain.draining:
                await self._refuse(socket, CLOSE_UNAVAILABLE, "draining")
            elif self.registry.contains(call_id):
                await self._refuse(socket, CLOSE_DUPLICATE, "duplicate", detail="attached_here")
            elif not self.registry.admit(call_id):
                await self._refuse(socket, CLOSE_UNAVAILABLE, "capacity")
            else:
                try:
                    await self._attach(socket, CallOwnership(self.redis, claims.call_id, self.pod_id))
                finally:
                    self.registry.release(call_id)

    async def _attach(self, socket: StarletteSocket, ownership: CallOwnership) -> None:
        try:
            acquired = await ownership.acquire()
        except (RedisError, OSError) as exc:
            await self._refuse(socket, CLOSE_REDIS, "redis", detail=type(exc).__name__)
            return
        if not acquired:
            await self._refuse(socket, CLOSE_DUPLICATE, "duplicate", detail="owned_elsewhere")
            return
        try:
            start = await wait_for_start(socket, self.clock)
        except StreamStartError as exc:
            await self._release(ownership)
            if exc.reason == "timeout":
                await self._refuse(socket, CLOSE_POLICY, "start_timeout")
            else:
                _log.warning("media.stream_start_failed", detail=exc.reason)
                await socket.close(CLOSE_POLICY)
            return
        await self._open_session(socket, ownership, start)

    async def _open_session(
        self, socket: StarletteSocket, ownership: CallOwnership, start: StreamStart
    ) -> None:
        context = await self._load_context(socket, ownership, start)
        if context is None:
            return
        try:
            feed = await self.hub.subscribe(ownership.call_id)
        except ChannelError as exc:
            await self._release(ownership)
            await self._refuse(socket, CLOSE_REDIS, "subscribe", detail=exc.reason)
            return
        try:
            await self._run_session(socket, ownership, start, context, feed)
        finally:
            await self.hub.unsubscribe(feed)

    async def _load_context(
        self, socket: StarletteSocket, ownership: CallOwnership, start: StreamStart
    ) -> CallContext | None:
        try:
            return await self.loader.load(ownership.call_id, start.call_control_id)
        except ContextRefusedError as exc:
            await self._release(ownership)
            code = CLOSE_REDIS if exc.reason in ("database", "redis") else CLOSE_AUTH
            await self._refuse(socket, code, exc.reason, detail=exc.detail)
            return None

    async def _run_session(
        self,
        socket: StarletteSocket,
        ownership: CallOwnership,
        start: StreamStart,
        context: CallContext,
        feed: CallFeed,
    ) -> None:
        try:
            carrier_state = await read_carrier_state(self.redis, ownership.call_id)
        except (RedisError, OSError) as exc:
            await self._release(ownership)
            await self._refuse(socket, CLOSE_REDIS, "redis", detail=type(exc).__name__)
            return
        session = CallSession(
            context=context,
            socket=socket,
            start=start,
            ownership=ownership,
            redis=self.redis,
            clock=self.clock,
            metrics=self.metrics,
            feed=feed,
            carrier_state=carrier_state,
            control_handler=self.control_handler,
            ender=self.ender,
        )
        self.sessions.add(session)
        try:
            await session.run(self.body)
        except Exception:  # a failed session must not take the server down; its cleanup already ran
            _log.exception("media.session_failed")
        finally:
            self.sessions.remove(session)

    async def _release(self, ownership: CallOwnership) -> None:
        try:
            await ownership.release()
        except (RedisError, OSError) as exc:
            _log.warning("media.ownership_release_failed", detail=type(exc).__name__)


async def _telnyx_endpoint(websocket: WebSocket) -> None:
    attach: MediaAttach = websocket.app.state.media_attach
    await attach.handle(websocket)


def telnyx_media_route() -> WebSocketRoute:
    """The ``/ws/telnyx`` route; it reads the :class:`MediaAttach` from ``app.state.media_attach``."""
    return WebSocketRoute(TELNYX_PATH, _telnyx_endpoint)
