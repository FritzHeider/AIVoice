"""Opener pre-render during ``prefetch_call_context`` (specs/07 §11 step 5, specs/09 §9; Decision P05-T10d).

:meth:`OpenerPrerender.opener_for` renders the templated ``first_sentence`` once (stored in
``CallContext.opener_text``; ``None`` with ``wait_for_greeting``, where the opener may answer the greeting).
For a pathway call :meth:`OpenerPrerender.opener_for_call` takes the start node's static ``text`` instead
(:func:`va_engine.pathway_call.opener_text` over the graph of the call's pinned version, read through a
:class:`PathwayGraphSource`): the engine speaks the same rendered line at call time, so it plays from the
cache (specs/11 §10 Decision P07-T12b). A graph that cannot be read is logged
``call_context.pathway_opener_failed`` (reason only) and the call gets no pre-rendered opener.
:meth:`OpenerPrerender.render` then, within :data:`PRERENDER_BUDGET_S`, stores the opener in the phrase cache
for one hour and makes sure the voice's standard fillers are cached (30 days), both in the leg's TTS format
under the pronounced text. Orgs with zero retention or HIPAA mode get nothing rendered (the REST endpoint has
no ``enable_logging=false``); an org whose settings cannot be read is skipped too. Failures are logged by
entry name (never the text) and never raised: the media worker then speaks the opener through the live socket.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from va_common.logging import get_logger
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_engine.opener import cache_text, phrase_format, phrase_voice, render_opener
from va_engine.pathway_call import opener_text
from va_voice.audio.formats import AudioFormat
from va_voice.phrase_cache import (
    PER_CALL_TTL_S,
    SHARED_TTL_S,
    PhraseCache,
    PhraseUnavailableError,
    PhraseVoice,
    phrase_cache_key,
)

__all__ = [
    "PRERENDER_BUDGET_S",
    "OpenerPrerender",
    "OrgSettingsSource",
    "PathwayGraphSource",
    "PrerenderResult",
    "SkipReason",
]

PRERENDER_BUDGET_S: Final = 8.0
"""The whole pre-render (opener and fillers) gets this long; the activity's own timeout is 20 s."""

type SkipReason = Literal["org_missing", "org_settings_unavailable", "zero_retention", "timeout"]

_log = get_logger(__name__)


class OrgSettingsSource(Protocol):
    """Where org settings come from (:class:`va_db.org_settings.OrgSettingsReader` satisfies it)."""

    async def get(self, org_id: uuid.UUID) -> OrgSettings | None: ...


class PathwayGraphSource(Protocol):
    """Graphs of pinned pathway versions (:class:`va_pathways.call_graphs.CallGraphCache` satisfies it)."""

    async def get(self, org_id: uuid.UUID, pathway_id: uuid.UUID, version: int) -> ActiveGraph: ...


@dataclass(frozen=True, slots=True)
class PrerenderResult:
    """``opener``: the opener is in the cache; ``fillers``: how many standard fillers are; ``skipped``: why
    nothing (or not everything, on ``timeout``) was rendered."""

    opener: bool = False
    fillers: int = 0
    skipped: SkipReason | None = None


class OpenerPrerender:
    """Renders a call's opener and the voice's fillers into the phrase cache; one per worker process."""

    def __init__(
        self,
        *,
        cache: PhraseCache,
        org_settings: OrgSettingsSource,
        model_id: str,
        budget_s: float = PRERENDER_BUDGET_S,
        graphs: PathwayGraphSource | None = None,
    ) -> None:
        self._graphs = graphs
        self._cache = cache
        self._org_settings = org_settings
        self._model_id = model_id
        self._budget_s = budget_s

    @property
    def model_id(self) -> str:
        """The TTS model phrases are rendered with (the media worker's socket model)."""
        return self._model_id

    def opener_for(self, context: CallContext, now: datetime) -> str | None:
        """The opener text to store in the context (``None``: the media worker renders or generates it)."""
        if context.config.wait_for_greeting:
            return None
        return render_opener(context, now=now)

    async def opener_for_call(self, context: CallContext, now: datetime) -> str | None:
        """:meth:`opener_for`, or for a pathway call the start node's static line (``None`` without a graph
        source, without a pinned version, or when the graph cannot be read)."""
        config = context.config
        if config.runner_type != "pathway":
            return self.opener_for(context, now)
        if self._graphs is None or config.pathway_id is None or config.pathway_version is None:
            return None
        try:
            active = await self._graphs.get(context.org_id, config.pathway_id, config.pathway_version)
        except PathwayUnavailableError as exc:
            _log.warning("call_context.pathway_opener_failed", reason=exc.reason)
            return None
        except (SQLAlchemyError, OSError) as exc:
            _log.warning("call_context.pathway_opener_failed", reason=type(exc).__name__)
            return None
        return opener_text(active, context, now=now)

    async def render(self, context: CallContext) -> PrerenderResult:
        """Cache ``context.opener_text`` (per call) and the standard fillers (shared); never raises."""
        skipped = await self._skip_reason(context.org_id)
        if skipped is not None:
            _log.info("call_context.prerender_skipped", reason=skipped)
            return PrerenderResult(skipped=skipped)
        voice = phrase_voice(context, model_id=self._model_id)
        fmt = phrase_format(context)
        text = None if context.opener_text is None else cache_text(context, context.opener_text)
        try:
            async with asyncio.timeout(self._budget_s):
                opener, fillers = await asyncio.gather(
                    self._opener(voice, text, fmt), self._cache.prefetch(voice, fmt, ttl_s=SHARED_TTL_S)
                )
        except TimeoutError:
            _log.warning("call_context.prerender_failed", error="TimeoutError", budget_s=self._budget_s)
            return PrerenderResult(skipped="timeout")
        cached = sum(phrase is not None for phrase in fillers.values())
        _log.info("call_context.prerendered", opener=opener, fillers=cached)
        return PrerenderResult(opener=opener, fillers=cached)

    async def _skip_reason(self, org_id: uuid.UUID) -> SkipReason | None:
        try:
            org = await self._org_settings.get(org_id)
        except (
            SQLAlchemyError,
            RedisError,
            OSError,
            ValueError,
        ) as exc:  # ValueError: stored settings invalid
            _log.warning("call_context.org_settings_unavailable", error=type(exc).__name__)
            return "org_settings_unavailable"
        if org is None:
            return "org_missing"
        if org.privacy.zero_retention or org.hipaa_mode:
            return "zero_retention"
        return None

    async def _opener(self, voice: PhraseVoice, text: str | None, fmt: AudioFormat) -> bool:
        if text is None:
            return False
        try:
            await self._cache.get_or_render(voice, text, fmt, ttl_s=PER_CALL_TTL_S)
        except PhraseUnavailableError as exc:
            _log.warning(
                "call_context.prerender_failed",
                entry=phrase_cache_key(voice, text, fmt),
                error=type(exc.__cause__).__name__,
            )
            return False
        return True
