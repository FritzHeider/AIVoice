"""The call's opener as the phrase cache sees it (specs/07 §11 step 5, specs/09 §9; Decision P05-T10d).

``prefetch_call_context`` renders the templated ``first_sentence`` once (:func:`render_opener`), stores the
text in ``CallContext.opener_text`` and the audio in the phrase cache under the key of :func:`phrase_voice`,
:func:`phrase_format` and :func:`cache_text`; the task runner speaks ``opener_text`` verbatim and the media
Speaker looks it up with the same voice, the leg's TTS format and the pronounced text, so both sides agree on
the entry.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from va_common.logging import get_logger
from va_domain.call_context import CallContext
from va_domain.languages import lookup_language
from va_engine.builtins import call_builtins
from va_engine.templating import (
    MissingVariables,
    Template,
    TemplateScope,
    TemplateSyntaxError,
    parse_template,
    render,
)
from va_voice.audio.formats import PCM16LE_16000, ULAW_8000, AudioFormat
from va_voice.phrase_cache import PhraseVoice
from va_voice.text.pronunciation import PronunciationApplier

__all__ = ["cache_text", "parse_field", "phrase_format", "phrase_voice", "render_opener"]

_log = get_logger(__name__)


def parse_field(text: str, field_name: str) -> Template | str:
    """``text`` parsed as a template; a syntax error is logged and the raw text is used as is."""
    try:
        return parse_template(text)
    except TemplateSyntaxError as exc:
        _log.warning("template_invalid", field=field_name, position=exc.position)
        return text


def _missing_sink(call_id: UUID) -> MissingVariables:
    def report(path: str) -> None:
        _log.info("template_variable_missing", path=path, call_id=str(call_id))

    return MissingVariables(sink=report)


def render_opener(context: CallContext, *, now: datetime) -> str | None:
    """The templated, stripped ``first_sentence`` of a task-mode call at ``now``; ``None`` for other runners,
    a missing or blank ``first_sentence``, or a blank render."""
    config = context.config
    source = config.first_sentence
    if config.runner_type != "task" or source is None or not source.strip():
        return None
    template = parse_field(source, "first_sentence")
    if isinstance(template, str):
        text = template
    else:
        scope = TemplateScope(
            variables=context.variables,
            request_data=config.request_data,
            builtins=call_builtins(context, now=now),
            timezone=config.timezone,
            missing=_missing_sink(context.call_id),
        )
        text = render(template, scope)
    text = text.strip()
    return text or None


def phrase_voice(context: CallContext, *, model_id: str) -> PhraseVoice:
    """The call's voice as a phrase cache key: the same fields the TTS socket is opened with."""
    config = context.config
    mapping = lookup_language(config.language)
    return PhraseVoice(
        voice_id=config.provider_voice_id,
        model_id=model_id,
        settings=config.voice_settings,
        language_code=mapping.tts_code if mapping is not None else None,
    )


def phrase_format(context: CallContext) -> AudioFormat:
    """The TTS format of the call's leg: ``pcm_16000`` with ``hd_audio``, else ``ulaw_8000``."""
    return PCM16LE_16000 if context.config.hd_audio else ULAW_8000


def cache_text(context: CallContext, text: str) -> str:
    """``text`` after the call's pronunciation guide: what the Speaker looks up (and TTS would be sent)."""
    return PronunciationApplier(context.config.pronunciation_guide).apply(text.strip()).text
