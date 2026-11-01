"""``va_telnyx.TelnyxClient``: typed Telnyx v2 commands (specs/06-telephony-telnyx.md §2, §2.1).

Every call command takes a deterministic ``command_id`` (:func:`va_telnyx.client_state.command_id`) so Telnyx
deduplicates retries. A command on a call that has already ended raises :class:`CallAlreadyEnded`; ``hangup``
returns ``False`` instead, every other caller ignores the error. Other failures raise :class:`TelnyxApiError`,
:class:`TelnyxTransportError`, or :class:`TelnyxRateLimited` (each has ``to_app_error()``).
"""

from __future__ import annotations

import random
import re
from typing import Any, Final, Literal

import httpx
from opentelemetry.trace import TracerProvider

from va_common.time import Clock, SystemClock
from va_telnyx.errors import CallAlreadyEnded
from va_telnyx.messaging import MessagingCommands
from va_telnyx.metrics import TelnyxMetrics
from va_telnyx.models import (
    AnswerRequest,
    CommandResult,
    Conference,
    DialRequest,
    DialResult,
    StreamParams,
    TransferRequest,
)
from va_telnyx.numbers import NumberCommands
from va_telnyx.provisioning import ProvisioningCommands
from va_telnyx.rate_limit import TelnyxRateLimiter
from va_telnyx.retry import RetryPolicy
from va_telnyx.transport import Operation, TelnyxTransport, parse_data, segment

__all__ = ["DTMF_DIGITS", "PLATFORM_ACCOUNT", "TelnyxClient"]

PLATFORM_ACCOUNT: Final = "platform"
DTMF_DIGITS: Final = re.compile(r"^[0-9A-D*#wW]+$")
_DTMF_MIN_MS: Final = 100
_DTMF_MAX_MS: Final = 500

_DIAL: Final = Operation("dial", "POST", "call_command", idempotent=True)
_CONFERENCE_CREATE: Final = Operation("conference_create", "POST", "call_command", idempotent=True)
_CONFERENCE_JOIN: Final = Operation("conference_join", "POST", "call_command", idempotent=True)
_CONFERENCE_LEAVE: Final = Operation("conference_leave", "POST", "call_command", idempotent=True)


def _command(action: str) -> Operation:
    return Operation(action, "POST", "call_command", idempotent=True, call_command=True)


_COMMANDS: Final = {
    action: _command(action)
    for action in (
        "answer",
        "reject",
        "hangup",
        "transfer",
        "bridge",
        "send_dtmf",
        "record_start",
        "record_stop",
        "streaming_start",
        "streaming_stop",
        "playback_start",
        "playback_stop",
    )
}


def _with_state(body: dict[str, Any], client_state: str | None) -> dict[str, Any]:
    if client_state is not None:
        body["client_state"] = client_state
    return body


class TelnyxClient(ProvisioningCommands, NumberCommands, MessagingCommands):
    """Telnyx REST client for one API key (the platform account or a BYO account): call control here,
    provisioning, numbers, and messaging in the mixins."""

    def __init__(  # noqa: PLR0913 - the specs/06 §2 positional four plus injectable collaborators
        self,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        limiter: TelnyxRateLimiter,
        *,
        account: str = PLATFORM_ACCOUNT,
        metrics: TelnyxMetrics | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        retry: RetryPolicy | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._transport = TelnyxTransport(
            api_key=api_key,
            base_url=base_url,
            http=http,
            limiter=limiter,
            account=account,
            clock=clock if clock is not None else SystemClock(),
            retry=retry if retry is not None else RetryPolicy(),
            rng=rng if rng is not None else random.Random(),  # noqa: S311 - retry jitter, not a secret
            metrics=metrics,
            tracer_provider=tracer_provider,
        )

    def __repr__(self) -> str:
        return f"TelnyxClient(base_url={self._transport.base_url!r}, account={self._transport.account!r})"

    @property
    def account(self) -> str:
        return self._transport.account

    async def _call_command(
        self, action: str, call_control_id: str, body: dict[str, Any], command_id: str
    ) -> CommandResult:
        operation = _COMMANDS[action]
        envelope = await self._transport.request(
            operation,
            f"/calls/{segment(call_control_id)}/actions/{action}",
            json={**body, "command_id": command_id},
            call_control_id=call_control_id,
            command_id=command_id,
        )
        return parse_data(operation, envelope, CommandResult)

    async def dial(self, request: DialRequest) -> DialResult:
        """``POST /calls``: a new outgoing leg."""
        envelope = await self._transport.request(
            _DIAL, "/calls", json=request.body(), command_id=request.command_id
        )
        return parse_data(_DIAL, envelope, DialResult)

    async def answer(self, call_control_id: str, request: AnswerRequest) -> CommandResult:
        body = request.body()
        command_id = body.pop("command_id")
        return await self._call_command("answer", call_control_id, body, command_id)

    async def reject(
        self, call_control_id: str, cause: Literal["CALL_REJECTED", "USER_BUSY"], *, command_id: str
    ) -> CommandResult:
        return await self._call_command("reject", call_control_id, {"cause": cause}, command_id)

    async def hangup(self, call_control_id: str, *, command_id: str, client_state: str | None = None) -> bool:
        """Hang up the leg; ``False`` when it had already ended (which counts as success)."""
        try:
            await self._call_command("hangup", call_control_id, _with_state({}, client_state), command_id)
        except CallAlreadyEnded:
            return False
        return True

    async def transfer(self, call_control_id: str, request: TransferRequest) -> CommandResult:
        body = request.body()
        command_id = body.pop("command_id")
        return await self._call_command("transfer", call_control_id, body, command_id)

    async def bridge(
        self,
        call_control_id: str,
        other_call_control_id: str,
        *,
        command_id: str,
        client_state: str,
        park_after_unbridge: Literal["self"] | None = None,
    ) -> CommandResult:
        body: dict[str, Any] = {"call_control_id": other_call_control_id, "client_state": client_state}
        if park_after_unbridge is not None:
            body["park_after_unbridge"] = park_after_unbridge
        return await self._call_command("bridge", call_control_id, body, command_id)

    async def send_dtmf(
        self, call_control_id: str, digits: str, duration_millis: int = 250, *, command_id: str
    ) -> CommandResult:
        """Send DTMF; ``digits`` from ``0-9 A-D * # w W`` and 100-500 ms per digit (``ValueError``
        otherwise)."""
        if not DTMF_DIGITS.fullmatch(digits):
            msg = f"invalid DTMF digits: {digits!r}"
            raise ValueError(msg)
        if not _DTMF_MIN_MS <= duration_millis <= _DTMF_MAX_MS:
            msg = f"duration_millis must be within {_DTMF_MIN_MS}-{_DTMF_MAX_MS}: {duration_millis}"
            raise ValueError(msg)
        body = {"digits": digits, "duration_millis": duration_millis}
        return await self._call_command("send_dtmf", call_control_id, body, command_id)

    async def record_start(
        self, call_control_id: str, *, command_id: str, client_state: str, play_beep: bool = False
    ) -> CommandResult:
        """Dual-channel WAV recording without length or silence limits."""
        body: dict[str, Any] = {
            "format": "wav",
            "channels": "dual",
            "play_beep": play_beep,
            "max_length": 0,
            "timeout_secs": 0,
            "client_state": client_state,
        }
        return await self._call_command("record_start", call_control_id, body, command_id)

    async def record_stop(self, call_control_id: str, *, command_id: str, client_state: str) -> CommandResult:
        return await self._call_command(
            "record_stop", call_control_id, {"client_state": client_state}, command_id
        )

    async def streaming_start(
        self, call_control_id: str, stream: StreamParams, *, command_id: str, client_state: str
    ) -> CommandResult:
        body = _with_state(stream.body(), client_state)
        return await self._call_command("streaming_start", call_control_id, body, command_id)

    async def streaming_stop(
        self, call_control_id: str, *, command_id: str, client_state: str, stream_id: str | None = None
    ) -> CommandResult:
        body: dict[str, Any] = {"client_state": client_state}
        if stream_id is not None:
            body["stream_id"] = stream_id
        return await self._call_command("streaming_stop", call_control_id, body, command_id)

    async def playback_start(  # noqa: PLR0913 - the specs/06 §2.1 playback options
        self,
        call_control_id: str,
        audio_url: str,
        *,
        command_id: str,
        client_state: str,
        loop: int | Literal["infinity"] = "infinity",
        overlay: bool = False,
        target_legs: Literal["self", "opposite", "both"] = "self",
    ) -> CommandResult:
        """Play ``audio_url`` (hold music during a warm transfer)."""
        body: dict[str, Any] = {
            "audio_url": audio_url,
            "loop": loop,
            "overlay": overlay,
            "target_legs": target_legs,
            "client_state": client_state,
        }
        return await self._call_command("playback_start", call_control_id, body, command_id)

    async def playback_stop(self, call_control_id: str, *, command_id: str) -> CommandResult:
        return await self._call_command("playback_stop", call_control_id, {"stop": "all"}, command_id)

    async def conference_create(
        self, call_control_id: str, name: str, *, command_id: str, client_state: str
    ) -> Conference:
        """A conference with ``call_control_id`` as its first participant."""
        body = {
            "call_control_id": call_control_id,
            "name": name,
            "beep_enabled": "never",
            "client_state": client_state,
            "command_id": command_id,
            "start_conference_on_create": True,
        }
        envelope = await self._transport.request(
            _CONFERENCE_CREATE,
            "/conferences",
            json=body,
            call_control_id=call_control_id,
            command_id=command_id,
        )
        return parse_data(_CONFERENCE_CREATE, envelope, Conference)

    async def conference_join(  # noqa: PLR0913 - the specs/06 §2.1 participant options
        self,
        conference_id: str,
        call_control_id: str,
        *,
        end_conference_on_exit: bool,
        command_id: str,
        client_state: str,
        start_conference_on_enter: bool = True,
        hold: bool = False,
        mute: bool = False,
    ) -> CommandResult:
        body = {
            "call_control_id": call_control_id,
            "start_conference_on_enter": start_conference_on_enter,
            "end_conference_on_exit": end_conference_on_exit,
            "hold": hold,
            "mute": mute,
            "client_state": client_state,
            "command_id": command_id,
        }
        envelope = await self._transport.request(
            _CONFERENCE_JOIN,
            f"/conferences/{segment(conference_id)}/actions/join",
            json=body,
            call_control_id=call_control_id,
            command_id=command_id,
        )
        return parse_data(_CONFERENCE_JOIN, envelope, CommandResult)

    async def conference_leave(
        self, conference_id: str, call_control_id: str, *, command_id: str
    ) -> CommandResult:
        envelope = await self._transport.request(
            _CONFERENCE_LEAVE,
            f"/conferences/{segment(conference_id)}/actions/leave",
            json={"call_control_id": call_control_id, "command_id": command_id},
            call_control_id=call_control_id,
            command_id=command_id,
        )
        return parse_data(_CONFERENCE_LEAVE, envelope, CommandResult)
