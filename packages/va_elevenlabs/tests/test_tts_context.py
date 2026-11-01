"""Context bookkeeping: clause confirmation, rewiring, completion (Decision P03-T10 rules 2-4)."""

from __future__ import annotations

from va_domain.voices import VoiceSettings
from va_elevenlabs.tts_context import ContextState
from va_elevenlabs.tts_models import ChunkAlignment


def chunk(text: str) -> ChunkAlignment:
    return ChunkAlignment(tuple(text), tuple(range(len(text))), (1,) * len(text))


def ctx(*clauses: str, flush: bool = True) -> ContextState:
    state = ContextState("t7-0", VoiceSettings(), created_at=0.0)
    for clause in clauses:
        state.add(clause, flush=flush)
    return state


def confirmed(state: ContextState) -> list[bool]:
    return [clause.confirmed for clause in state.clauses]


def test_wire_id_starts_as_logical_id_and_rewires_per_reconnect() -> None:
    state = ctx("Hi there. ")
    assert state.wire_id == "t7-0"
    state.rewire(1)
    assert state.wire_id == "t7-0-r1"
    state.rewire(3)
    assert (state.wire_id, state.reconnects) == ("t7-0-r3", 2)


def test_first_chunk_confirms_first_clause_even_without_alignment() -> None:
    state = ctx("Hello there. ", "How are you? ")
    assert state.confirm(None) == 1
    assert confirmed(state) == [True, False]
    assert state.confirm(None) == 0


def test_confirmation_counts_visible_characters_across_chunks() -> None:
    state = ctx("Hello there. ", "How are you? ")
    state.confirm(chunk("Hel"))
    state.confirm(chunk("lo th"))
    assert confirmed(state) == [True, False]
    state.confirm(chunk("ere. "))
    assert confirmed(state) == [True, False]
    assert not state.all_aligned()
    assert state.confirm(chunk(" H")) == 1
    assert confirmed(state) == [True, True]
    state.confirm(chunk("ow are you?"))
    assert state.all_aligned()


def test_one_chunk_can_confirm_several_clauses() -> None:
    state = ctx("A b. ", "C d. ", "E f. ")
    assert state.confirm(chunk("A b. C d. E")) == 3
    assert state.unconfirmed() == []


def test_unconfirmed_and_awaiting_audio() -> None:
    state = ctx("One two. ", flush=False)
    state.add("Three four. ", flush=False)
    assert not state.awaiting_audio()
    assert state.needs_bare_flush()
    state.mark_flushed()
    assert state.awaiting_audio()
    state.confirm(chunk("One"))
    assert [c.text for c in state.unconfirmed()] == ["Three four. "]
    assert state.awaiting_audio()


def test_rewire_restarts_count_at_first_unconfirmed_clause() -> None:
    state = ctx("Hello there. ", "How are you? ", "Bye now. ")
    state.confirm(chunk("Hello"))
    state.rewire(1)
    # Only "How are you? " and "Bye now. " are resent; the new socket aligns them from the start.
    assert state.confirm(None) == 1
    assert confirmed(state) == [True, True, False]
    state.confirm(chunk("How are you?"))
    assert confirmed(state) == [True, True, False]
    state.confirm(chunk("B"))
    assert confirmed(state) == [True, True, True]


def test_rewire_with_everything_confirmed_confirms_the_next_clause_on_any_chunk() -> None:
    state = ctx("Hello there. ")
    state.confirm(chunk("Hello there."))
    state.rewire(1)
    state.add("More. ", flush=True)
    assert state.confirm(None) == 1


def test_complete_and_quiet_since() -> None:
    state = ctx("Hi. ")
    assert state.quiet_since() is None
    assert not state.complete()
    state.confirm(chunk("Hi."))
    state.last_audio_at = 2.0
    assert not state.complete()
    state.ended = True
    state.ended_at = 1.5
    assert state.complete()
    assert state.quiet_since() == 2.0
    state.ended_at = 2.5
    assert state.quiet_since() == 2.5


def test_ended_without_clauses_is_complete() -> None:
    state = ctx()
    state.ended = True
    assert state.complete()
    assert not state.needs_bare_flush()
    assert state.all_aligned()
