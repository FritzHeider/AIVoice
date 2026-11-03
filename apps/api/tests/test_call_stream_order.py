"""``SeqOrder``: live call events are released strictly in seq order (specs/19 §9 Decision P06-T06a)."""

from __future__ import annotations

from va_api.services.call_stream_order import SeqOrder


def test_contiguous_seqs_are_released_at_once() -> None:
    order: SeqOrder[str] = SeqOrder(0)
    assert order.offer(1, "a") == ["a"]
    assert order.offer(2, "b") == ["b"]
    assert (order.last, order.has_gap, order.lowest_pending) == (2, False, None)


def test_a_gap_holds_later_seqs_until_it_is_filled() -> None:
    order: SeqOrder[str] = SeqOrder(4)
    assert order.offer(7, "g") == []
    assert order.offer(6, "f") == []
    assert (order.has_gap, order.lowest_pending) == (True, 6)
    assert order.offer(5, "e") == ["e", "f", "g"]
    assert order.last == 7
    assert not order.has_gap


def test_duplicates_and_seqs_at_or_below_the_floor_are_dropped() -> None:
    order: SeqOrder[str] = SeqOrder(3)
    assert order.offer(3, "old") == []
    assert order.offer(2, "older") == []
    assert order.offer(5, "e") == []
    assert order.offer(5, "again") == []
    assert order.offer(4, "d") == ["d", "e"]


def test_an_item_less_offer_fills_its_place_without_output() -> None:
    order: SeqOrder[str] = SeqOrder(0)
    assert order.offer(2, "b") == []
    assert order.offer(1, None) == ["b"]


def test_advance_skips_holes_and_releases_held_items_in_order() -> None:
    order: SeqOrder[str] = SeqOrder(0)
    order.offer(3, "c")
    order.offer(5, "e")
    order.offer(9, "i")
    assert order.advance(4) == ["c", "e"]
    assert (order.last, order.lowest_pending) == (5, 9)
    assert order.advance(8) == ["i"]
    assert order.last == 9


def test_advance_below_the_floor_keeps_it() -> None:
    order: SeqOrder[str] = SeqOrder(10)
    assert order.advance(4) == []
    assert order.last == 10
