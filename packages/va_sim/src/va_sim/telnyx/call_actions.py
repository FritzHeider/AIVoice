"""FakeTelnyx call commands ``POST /v2/calls/{ccid}/actions/{action}`` (specs/06-telephony-telnyx.md §2.1;
specs/30-testing-and-evals.md §3.2, Decision 2026-10-29 P03-T02b rules 2 to 4, 7, 9 to 11).

Each action is a body model, the leg state it needs, an optional extra check, and a run step that calls the
engine. The shared wrapper in ``call_routes`` does lookup, parsing, ``command_id`` dedup, the ended-call
check, the checks, the ``client_state`` replacement, and the command log, in that order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from starlette.responses import JSONResponse

from va_sim.clock import iso_z
from va_sim.telnyx.call_models import (
    AnswerRequest,
    BridgeRequest,
    CommandFields,
    DialRequest,
    HangupRequest,
    PlaybackStartRequest,
    PlaybackStopRequest,
    RecordStartRequest,
    RecordStopRequest,
    RejectRequest,
    SendDtmfRequest,
    StreamingStartRequest,
    StreamingStopRequest,
    TransferRequest,
)
from va_sim.telnyx.calls import Call, DtmfEntry, LegOutcome, PendingMedia, outcome_of
from va_sim.telnyx.errors import call_ended, invalid, invalid_state
from va_sim.telnyx.store import Account

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["Action", "Needs", "call_actions", "pending_media", "state_error", "transfer_outcome"]

type Needs = Literal["any", "answered", "ringing_incoming"]
type Check = Callable[[Account, Call, Any], JSONResponse | None]
type Run = Callable[[Account, Call, Any], None]

_REJECT_CAUSES: Final = {"CALL_REJECTED": "call_rejected", "USER_BUSY": "user_busy"}


def _no_check(_account: Account, _call: Call, _body: Any) -> JSONResponse | None:
    return None


@dataclass(frozen=True, slots=True)
class Action:
    model: type[CommandFields]
    needs: Needs
    run: Run
    check: Check = _no_check


def state_error(needs: Needs, call: Call, action: str) -> JSONResponse | None:
    """``10015`` when ``call`` is not in the state ``action`` needs."""
    fits = {
        "any": True,
        "answered": call.answered,
        "ringing_incoming": call.direction == "incoming" and call.answered_at is None,
    }[needs]
    return None if fits else invalid_state(f"Call is not in a state that accepts {action}.")


def pending_media(body: DialRequest | AnswerRequest) -> PendingMedia:
    """Streaming and recording to start on answer, from a dial or answer body."""
    return PendingMedia(
        stream=body.stream_params() if body.stream_url else None,
        record_channels=body.record_channels if body.record is not None else None,
        record_format=body.record_format,
    )


def transfer_outcome(state: SimState, call: Call) -> LegOutcome:
    """How a leg dialled on behalf of ``call`` behaves: its scenario's ``transfer_target``."""
    scenario = state.scenarios.get(call.scenario_id or "")
    return outcome_of(scenario.transfer_target if scenario is not None else None)


def call_actions(state: SimState) -> Mapping[str, Action]:
    """The action table, closed over the shared state and call engine."""
    engine = state.call_engine

    def answer(_account: Account, call: Call, body: AnswerRequest) -> None:
        if body.webhook_url is not None:
            call.webhook_url = body.webhook_url
        call.pending = pending_media(body)
        engine.answer(call)

    def reject(_account: Account, call: Call, body: RejectRequest) -> None:
        engine.hang_up(call, _REJECT_CAUSES[body.cause], "callee")

    def hangup(_account: Account, call: Call, _body: HangupRequest) -> None:
        engine.hang_up(call, "normal_clearing" if call.answered else "originator_cancel", call.platform_side)

    def transfer(account: Account, call: Call, body: TransferRequest) -> None:
        engine.transfer(
            account,
            call,
            transfer_outcome(state, call),
            body.timeout_secs,
            from_=body.from_ or call.from_,
            to=body.to,
            webhook_url=body.webhook_url or call.webhook_url,
            client_state=body.target_leg_client_state,
            amd=body.answering_machine_detection,
            time_limit_s=float(body.time_limit_secs),
            pending=PendingMedia(record_channels="dual" if body.record is not None else None),
        )

    def check_bridge(account: Account, call: Call, body: BridgeRequest) -> JSONResponse | None:
        other = account.calls.get(body.call_control_id)
        if other is None:
            return invalid(f"Call {body.call_control_id!r} does not exist.", "/call_control_id")
        if other.ended:
            return call_ended(other.call_control_id)
        if other is call or not other.answered or call.bridged_with or other.bridged_with:
            return invalid_state("Call is not in a state that accepts bridge.")
        return None

    def bridge(account: Account, call: Call, body: BridgeRequest) -> None:
        engine.bridge(call, account.calls[body.call_control_id], park=body.park_after_unbridge == "self")

    def send_dtmf(_account: Account, call: Call, body: SendDtmfRequest) -> None:
        call.dtmf_sent.append(DtmfEntry(body.digits, body.duration_millis, iso_z(state.clock.now())))

    def check_record_start(_account: Account, call: Call, _body: RecordStartRequest) -> JSONResponse | None:
        return None if call.recording is None else invalid_state("Call is already being recorded.")

    def record_start(_account: Account, call: Call, body: RecordStartRequest) -> None:
        engine.start_recording(call, body.channels, body.format)

    def check_record_stop(_account: Account, call: Call, _body: RecordStopRequest) -> JSONResponse | None:
        return None if call.recording is not None else invalid_state("Call is not being recorded.")

    def record_stop(_account: Account, call: Call, _body: RecordStopRequest) -> None:
        engine.send(item for item in (engine.stop_recording(call),) if item is not None)

    def check_streaming_start(
        _account: Account, call: Call, body: StreamingStartRequest
    ) -> JSONResponse | None:
        if body.stream_url is None:
            return invalid("is required", "/stream_url")
        return None if call.stream is None else invalid_state("Call is already streaming.")

    def streaming_start(_account: Account, call: Call, body: StreamingStartRequest) -> None:
        engine.start_stream(call, body.stream_params())

    def check_streaming_stop(
        _account: Account, call: Call, body: StreamingStopRequest
    ) -> JSONResponse | None:
        if call.stream is None:
            return invalid_state("Call is not streaming.")
        if body.stream_id is not None and body.stream_id != call.stream.stream_id:
            return invalid(f"Stream {body.stream_id!r} does not exist on this call.", "/stream_id")
        return None

    def streaming_stop(_account: Account, call: Call, _body: StreamingStopRequest) -> None:
        engine.send(item for item in (engine.stop_stream(call),) if item is not None)

    def playback_start(_account: Account, call: Call, body: PlaybackStartRequest) -> None:
        engine.start_playback(call, body.audio_url, overlay=body.overlay)

    def playback_stop(_account: Account, call: Call, body: PlaybackStopRequest) -> None:
        engine.send(
            engine.stop_playbacks(call, "cancelled", overlay=body.overlay, current=body.stop == "current")
        )

    return {
        "answer": Action(AnswerRequest, "ringing_incoming", answer),
        "reject": Action(RejectRequest, "ringing_incoming", reject),
        "hangup": Action(HangupRequest, "any", hangup),
        "transfer": Action(TransferRequest, "answered", transfer),
        "bridge": Action(BridgeRequest, "answered", bridge, check_bridge),
        "send_dtmf": Action(SendDtmfRequest, "answered", send_dtmf),
        "record_start": Action(RecordStartRequest, "answered", record_start, check_record_start),
        "record_stop": Action(RecordStopRequest, "answered", record_stop, check_record_stop),
        "streaming_start": Action(StreamingStartRequest, "answered", streaming_start, check_streaming_start),
        "streaming_stop": Action(StreamingStopRequest, "answered", streaming_stop, check_streaming_stop),
        "playback_start": Action(PlaybackStartRequest, "answered", playback_start),
        "playback_stop": Action(PlaybackStopRequest, "answered", playback_stop),
    }
