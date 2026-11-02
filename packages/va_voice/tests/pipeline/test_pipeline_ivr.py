"""IVR dead end: :class:`IvrProgress` and the ``ivr`` task (specs/05 §9, specs/07 §11, P05-T10b)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from pipeline_fakes import ScriptedBrain
from pipeline_harness import Harness, harness

from va_voice.pipeline import BrainAction, BrainOutput, IvrProgress, PipelineConfig, SpeakStatic

DEAD_END_S = 20.0
MENU = "Press one for sales, two for support."


def deadline(progress: IvrProgress) -> float | None:
    return progress.deadline_ms


def test_progress_starts_with_the_opener() -> None:
    progress = IvrProgress(1000)
    assert deadline(progress) is None
    assert not progress.due(10**9)
    # A turn before the start is remembered but opens no window.
    assert progress.on_turn("Welcome", 5)
    assert deadline(progress) is None
    progress.start(100)
    assert deadline(progress) == 1100
    assert not progress.due(1099.9)
    assert progress.due(1100)


def test_new_text_restarts_the_window_and_repeated_text_does_not() -> None:
    progress = IvrProgress(1000)
    progress.start(0)
    assert progress.on_turn("Press 1 for sales.", 400)
    assert progress.deadline_ms == 1400
    # Normalisation: case and punctuation do not make a repeated menu new.
    assert not progress.on_turn("press 1 for SALES", 900)
    assert not progress.on_turn("   ", 950)
    assert progress.deadline_ms == 1400
    assert progress.on_turn("Please hold.", 1200)
    assert progress.deadline_ms == 2200


def test_an_action_restarts_the_window() -> None:
    progress = IvrProgress(1000)
    progress.on_action(50)
    assert deadline(progress) is None
    progress.start(0)
    progress.on_action(700)
    assert deadline(progress) == 1700


def test_the_dead_end_must_be_positive() -> None:
    with pytest.raises(ValueError, match="dead_end_ms must be > 0"):
        IvrProgress(0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ivr_dead_end_s": 0}, "ivr_dead_end_s must be > 0"),
        ({"greeting_timeout_ms": 999}, "greeting_timeout_ms must be within 1000..30000"),
        ({"greeting_timeout_ms": 30_001}, "greeting_timeout_ms must be within 1000..30000"),
    ],
)
def test_config_bounds(overrides: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PipelineConfig(**overrides)  # type: ignore[arg-type]


def ivr_config(*, ivr_mode: bool = True) -> PipelineConfig:
    return PipelineConfig(
        ivr_mode=ivr_mode, ivr_dead_end_s=DEAD_END_S, no_input_timeout_ms=30_000, max_reprompts=100
    )


async def speak_every(h: Harness, gap_s: float) -> None:
    while True:
        await h.caller.say(600)
        await h.clock.sleep(gap_s)


@contextlib.asynccontextmanager
async def menus(h: Harness, gap_s: float = 4.0) -> AsyncIterator[None]:
    task = asyncio.get_running_loop().create_task(speak_every(h, gap_s))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_repeating_menu_ends_the_call_as_a_dead_end() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hello.")])
    async with harness(config=ivr_config(), brain=brain, utterances=[MENU] * 20) as h:
        ended: list[float] = []
        h.task.add_done_callback(lambda _: ended.append(h.now_ms()))
        async with menus(h):
            result = await h.result()
    assert result.end_reason == "ivr_dead_end"
    turns = h.brain.user_turns
    assert len(turns) >= 3
    assert {turn.text for turn in turns} == {MENU}
    # The window restarted when the menu was first heard (delivered shortly after its end) and never again.
    first_heard = turns[0].end_ms
    assert first_heard + DEAD_END_S * 1000 <= ended[0] <= first_heard + DEAD_END_S * 1000 + 2000


async def test_new_prompts_keep_the_call_alive() -> None:
    texts = [f"Menu option number {n}" for n in range(30)]
    async with harness(config=ivr_config(), brain=ScriptedBrain(), utterances=texts) as h:
        async with menus(h):
            await h.until(lambda: h.now_ms() >= 3 * DEAD_END_S * 1000, timeout_s=4 * DEAD_END_S)
        assert not h.task.done()


def pressing() -> list[BrainOutput]:
    return [BrainAction("press_button")]


async def test_button_presses_are_progress() -> None:
    brain = ScriptedBrain(replies=[pressing() for _ in range(30)])
    async with harness(config=ivr_config(), brain=brain, utterances=[MENU] * 30) as h:
        async with menus(h):
            await h.until(lambda: h.now_ms() >= 3 * DEAD_END_S * 1000, timeout_s=4 * DEAD_END_S)
        assert not h.task.done()


async def test_without_ivr_mode_there_is_no_dead_end() -> None:
    async with harness(config=ivr_config(ivr_mode=False), brain=ScriptedBrain(), utterances=[MENU] * 30) as h:
        async with menus(h):
            await h.until(lambda: h.now_ms() >= 3 * DEAD_END_S * 1000, timeout_s=4 * DEAD_END_S)
        assert not h.task.done()
        assert len(h.brain.user_turns) >= 5


async def test_silence_after_the_opener_is_a_dead_end() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hello.")])
    async with harness(config=ivr_config(), brain=brain) as h:
        ended: list[float] = []
        h.task.add_done_callback(lambda _: ended.append(h.now_ms()))
        result = await h.result()
    assert result.end_reason == "ivr_dead_end"
    assert DEAD_END_S * 1000 <= ended[0] <= DEAD_END_S * 1000 + 40
