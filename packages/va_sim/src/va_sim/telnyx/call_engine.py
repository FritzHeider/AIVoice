"""FakeTelnyx call engine (specs/30-testing-and-evals.md §3.2, Decision 2026-10-29 P03-T02b).

The only place that mutates calls and conferences: scenario-driven progress of outgoing legs (ring →
answered | busy | no_answer | rejected | failed), answer, hang-up (with the stream, playback, conference,
and recording side effects in a fixed order), bridges, transfers, and conference membership. Handlers
validate commands and then call one engine method; every webhook goes through :class:`CallEvents` so
per-session order holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from va_sim.clock import iso_z
from va_sim.telnyx.call_events import Outgoing
from va_sim.telnyx.call_media import CallMedia
from va_sim.telnyx.calls import (
    CONFERENCE_LIFETIME,
    Call,
    Conference,
    LegOutcome,
    PendingMedia,
    TransferEntry,
)
from va_sim.telnyx.store import Account

__all__ = ["NON_ANSWER", "SIP_CAUSES", "CallEngine", "HangupSpec"]

# SIP status reported as ``sip_hangup_cause`` for each ``hangup_cause``.
SIP_CAUSES: Final[Mapping[str, str]] = {
    "normal_clearing": "200",
    "originator_cancel": "487",
    "timeout": "487",
    "time_limit": "200",
    "user_busy": "486",
    "not_found": "404",
    "no_answer": "480",
    "call_rejected": "603",
    "unspecified": "503",
}


@dataclass(frozen=True, slots=True)
class HangupSpec:
    """How an unanswered outgoing leg ends; ``delay_s`` None means at the dial's ``timeout_secs``."""

    cause: str
    source: str
    delay_s: float | None


NON_ANSWER: Final[Mapping[str, HangupSpec]] = {
    "busy": HangupSpec("user_busy", "callee", 0.5),
    "rejected": HangupSpec("call_rejected", "callee", 0.5),
    "failed": HangupSpec("unspecified", "unknown", 0.5),
    "no_answer": HangupSpec("timeout", "unknown", None),
}


class CallEngine(CallMedia):
    """Creates call legs and drives their lifecycle."""

    def new_call(self, account: Account, *, call_session_id: str | None = None, **fields: Any) -> Call:
        """A new leg on ``account``; ``fields`` are the :class:`Call` fields other than ids and times."""
        ids = self._state.ids
        call = Call(
            call_control_id=f"v3:{ids.token(24)}",
            call_leg_id=ids.uuid(),
            call_session_id=call_session_id or ids.uuid(),
            created_at=self._state.clock.now(),
            created_ms=self._state.clock.virtual_ms(),
            **fields,
        )
        account.calls[call.call_control_id] = call
        if call.bridge_on_answer is not None:
            call.bridge_on_answer.waiting_legs.append(call)
        return call

    # Progress.

    def start_outgoing(self, call: Call, timeout_s: float) -> None:
        """``call.initiated`` now, then the scenario outcome on the sim clock."""
        self.emit(call, "call.initiated")
        outcome = call.outcome
        if outcome.result == "answered" and outcome.ring_s <= timeout_s:
            self.later(call, outcome.ring_s, lambda: self.answer(call), name="answer")
            return
        spec = NON_ANSWER["no_answer" if outcome.result == "answered" else outcome.result]
        delay = timeout_s if spec.delay_s is None else spec.delay_s
        self.later(call, delay, lambda: self.hang_up(call, spec.cause, spec.source), name="progress")

    def start_incoming(self, call: Call, first_command_timeout_s: float | None) -> None:
        """``call.initiated`` for an inbound leg; without any command in time the leg times out."""
        self.emit(call, "call.initiated")
        if first_command_timeout_s is None:
            return

        def expire() -> None:
            if not call.commands and not call.ended:
                self.hang_up(call, "timeout", "unknown")

        self.later(call, first_command_timeout_s, expire, name="first-command-timeout")

    def answer(self, call: Call) -> None:
        """Answer: ``call.answered``, pending stream and recording, AMD, time limit, bridge on answer."""
        call.state = "answered"
        call.answered_at = self._state.clock.now()
        call.answered_ms = self._state.clock.virtual_ms()
        self.emit(call, "call.answered")
        peer, call.bridge_on_answer = call.bridge_on_answer, None
        if peer is not None and peer.ended:
            self.hang_up(call, "normal_clearing", call.platform_side)
            return
        pending, call.pending = call.pending, PendingMedia()
        if pending.stream is not None:
            self.start_stream(call, pending.stream)
        if pending.record_channels is not None:
            self.start_recording(call, pending.record_channels, pending.record_format)
        if call.direction == "outgoing":
            self.schedule_amd(call)
        self.later(call, call.time_limit_s, lambda: self.hang_up(call, "time_limit", "unknown"), name="limit")
        if peer is not None and peer.answered and peer.bridged_with is None:
            self.bridge(peer, call, park=False)

    def hang_up(self, call: Call, cause: str, source: str) -> None:
        """End the leg: streaming.stopped, playback.ended, participant.left, call.hangup, recording.saved."""
        if call.ended:
            return
        self.cancel_timers(call)
        items: list[Outgoing] = []
        stopped = self.stop_stream(call)
        if stopped is not None:
            items.append(stopped)
        items.extend(self.stop_playbacks(call, "call_hangup", overlay=None, current=False))
        items.extend(self.leave_conference(call))
        call.state = "hangup"
        call.ended_at = self._state.clock.now()
        call.ended_ms = self._state.clock.virtual_ms()
        call.hangup_cause, call.hangup_source = cause, source
        items.append(
            self.event(
                call,
                "call.hangup",
                call.payload(
                    hangup_cause=cause,
                    hangup_source=source,
                    sip_hangup_cause=SIP_CAUSES.get(cause, "unknown"),
                    end_time=iso_z(call.ended_at),
                ),
            )
        )
        saved = self.stop_recording(call)
        if saved is not None:
            items.append(saved)
        self.send(items)
        self._unbridge(call)
        self._cancel_waiting_legs(call)

    def _unbridge(self, call: Call) -> None:
        peer, call.bridged_with = call.bridged_with, None
        if peer is None:
            return
        peer.bridged_with = None
        if peer.park_after_unbridge:
            peer.state = "parked"
            peer.park_after_unbridge = False
        else:
            self.hang_up(peer, "normal_clearing", peer.remote_side)

    def _cancel_waiting_legs(self, call: Call) -> None:
        """Hang up the still-ringing legs (transfer, ``link_to`` + ``bridge_on_answer``) that would bridge to
        ``call`` on answer: the platform could never control them otherwise."""
        waiting, call.waiting_legs = call.waiting_legs, []
        for leg in waiting:
            if leg.bridge_on_answer is call and not leg.ended:
                self.hang_up(leg, "originator_cancel", "caller")

    # Bridges and transfers.

    def bridge(self, a: Call, b: Call, *, park: bool) -> None:
        """Bridge two answered legs: ``call.bridged`` on ``a`` then ``b``."""
        a.bridged_with, b.bridged_with = b, a
        a.state = b.state = "bridged"
        a.park_after_unbridge = park
        self.send([self.event(a, "call.bridged"), self.event(b, "call.bridged")])

    def transfer(self, account: Account, a: Call, outcome: LegOutcome, timeout_s: float, **leg: Any) -> Call:
        """Dial leg B for answered leg ``a``; B bridges to ``a`` when it answers."""
        b = self.new_call(
            account,
            call_session_id=a.call_session_id,
            direction="outgoing",
            connection_id=a.connection_id,
            base_url=a.base_url,
            state="bridging",
            outcome=outcome,
            scenario_id=a.scenario_id,
            bridge_on_answer=a,
            **leg,
        )
        a.transfers.append(TransferEntry(b.call_control_id, b.to, b.from_, iso_z(self._state.clock.now())))
        self.start_outgoing(b, timeout_s)
        return b

    # Conferences.

    def create_conference(self, account: Account, call: Call, name: str) -> Conference:
        """A conference with ``call`` as its first participant."""
        now = self._state.clock.now()
        conference = Conference(
            {
                "id": self._state.ids.uuid(),
                "name": name,
                "record_type": "conference",
                "status": "in_progress",
                "connection_id": call.connection_id,
                "created_at": iso_z(now),
                "expires_at": iso_z(now + CONFERENCE_LIFETIME),
                "region": "sim",
            }
        )
        account.conferences[conference.conference_id] = conference
        self.send([self._conference_event(call, conference, "conference.created")])
        self.join_conference(call, conference, end_on_exit=False)
        return conference

    def join_conference(self, call: Call, conference: Conference, *, end_on_exit: bool) -> None:
        conference.participants[call] = end_on_exit
        call.conference = conference
        self.send([self._conference_event(call, conference, "conference.participant.joined")])

    def leave_conference(self, call: Call) -> list[Outgoing]:
        """Remove ``call`` from its conference; ends the conference when it was the last or the host."""
        conference, call.conference = call.conference, None
        if conference is None:
            return []
        end_on_exit = conference.participants.pop(call)
        items = [self._conference_event(call, conference, "conference.participant.left")]
        if conference.participants and not end_on_exit:
            return items
        conference.record["status"] = "completed"
        reason = "host_left" if end_on_exit else "all_left"
        items.append(self._conference_event(call, conference, "conference.ended", reason=reason))
        for other in tuple(conference.participants):
            del conference.participants[other]
            other.conference = None
            items.append(self._conference_event(other, conference, "conference.participant.left"))
        return items

    def _conference_event(
        self, call: Call, conference: Conference, event_type: str, **extra: object
    ) -> Outgoing:
        payload = call.participant(conference, **extra)
        return self.event(call, event_type, payload, conference_id=conference.conference_id)
