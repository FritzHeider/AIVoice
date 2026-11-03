"""Engine actions (specs/11 §7).

:class:`TransferRequest` and :class:`PressButton` are the task-mode runner's telephony actions (Decision
P05-T09c): they subclass :class:`va_voice.pipeline.BrainAction`, so the pipeline passes them through unchanged
to the media worker's action handler.

The pathway node handlers (specs/11 §4, Decision P07-T06) return the :data:`Action` union: :class:`Say` /
:class:`SayStream` (speech), :class:`Wait` (arm a Wait for Response timer), and the terminal
:class:`TransferAction`, :class:`EndCallAction` and :class:`HumanTakeover`, which run once the line named by
``after_mark`` finished playing (``None`` = at once). The P07-T07 handlers add the non-terminal
:class:`SendDtmfAction` (Press Button) and :class:`SwitchGraph` (Transfer Pathway, applied by
:meth:`va_engine.navigation.Navigator.switch_graph`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Final, Literal

from va_engine.models.nodes import PathwayVersionRef, WarmTransfer
from va_voice.pipeline.models import BrainAction

__all__ = [
    "TERMINAL_ACTIONS",
    "Action",
    "EndCallAction",
    "HumanTakeover",
    "PressButton",
    "Say",
    "SayStream",
    "SendDtmfAction",
    "SwitchGraph",
    "TransferAction",
    "TransferRequest",
    "Wait",
    "is_terminal",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferRequest(BrainAction):
    """Transfer the caller to ``target`` (E.164); ``label`` is the transfer list key the model chose."""

    kind: str = "transfer_call"
    target: str
    label: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PressButton(BrainAction):
    """Send ``digits`` as DTMF (``w``/``W`` are pauses)."""

    kind: str = "press_button"
    digits: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Say:
    """Speak (or send) ``text``; ``mark`` names its playback, ``interruptible`` ``None`` = the call level."""

    text: str
    mark: str
    interruptible: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SayStream:
    """Speak the dialogue tokens as they stream in (same ``mark``/``interruptible`` rules as :class:`Say`)."""

    tokens: AsyncIterator[str] = field(compare=False)
    mark: str
    interruptible: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Wait:
    """Arm the Wait for Response timer: no user input within ``seconds`` is a wait timeout."""

    seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferAction:
    """Transfer the call to ``target`` (E.164 or ``sip:``/``sips:`` URI) once ``after_mark`` played."""

    mode: Literal["cold", "warm"]
    target: str
    caller_id_mode: Literal["agent_number", "original_caller"]
    warm: WarmTransfer | None = None
    after_mark: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EndCallAction:
    """End the call (close the conversation on text channels) once ``after_mark`` played."""

    reason: str
    ended_by: str = "agent"
    after_mark: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanTakeover:
    """Hand a text conversation to a human operator once ``after_mark`` was delivered."""

    after_mark: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SendDtmfAction:
    """Send ``digits`` as DTMF (``w`` = 0.5 s pause, ``W`` = 1 s), each tone ``duration_ms`` long, once
    ``after_mark`` played."""

    digits: str
    duration_ms: int
    after_mark: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SwitchGraph:
    """Continue in another pathway (Transfer Pathway node).

    ``node_id`` ``None`` = the target's start node; ``carry_variables`` keeps the variables; ``return_on_end``
    makes the conversation come back to the Transfer Pathway node instead of ending at an End Call node.
    """

    pathway_id: str
    version: PathwayVersionRef
    node_id: str | None = None
    carry_variables: bool = True
    return_on_end: bool = False


type Action = (
    Say | SayStream | Wait | SendDtmfAction | SwitchGraph | TransferAction | EndCallAction | HumanTakeover
)

TERMINAL_ACTIONS: Final = (TransferAction, EndCallAction, HumanTakeover)


def is_terminal(action: Action) -> bool:
    """Whether ``action`` ends the AI's part of the conversation."""
    return isinstance(action, TERMINAL_ACTIONS)
