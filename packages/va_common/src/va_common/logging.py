"""Structured logging for every service (specs/27-observability.md §1, specs/02 §4 Logging rule).

structlog is backed by the stdlib ``logging`` module so third-party loggers (uvicorn, httpx, temporalio) share
one pipeline: the same standard fields, trace ids, and redaction apply to every line. Call
:func:`configure_logging` once at startup, then use :func:`get_logger` and the context binding helpers.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Mapping, MutableMapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final, TextIO
from uuid import UUID

import orjson
import structlog
from opentelemetry import trace
from structlog.typing import Processor

from va_common.masking import REDACTED, is_secret_key, mask_phones_in_text
from va_common.settings import BaseServiceSettings, LogFormat
from va_common.tracing import resolve_pod

__all__ = [
    "RAW_MEDIA_KEYS",
    "TRANSCRIPT_KEYS",
    "RedactProcessor",
    "RedactionPolicy",
    "bind_call_context",
    "bind_turn",
    "bound_context",
    "clear_context",
    "component_levels",
    "configure_logging",
    "get_logger",
    "otel_trace_processor",
    "redact",
    "transcripts_allowed",
]

TRANSCRIPT_KEYS: Final = frozenset({"text", "transcript"})
RAW_MEDIA_KEYS: Final = frozenset({"audio", "payload"})
MAX_DEPTH: Final = 8

_COMPONENT_LEVEL_VAR: Final = re.compile(r"^LOG_LEVEL_(?P<component>[A-Za-z0-9_]+)$")
_LEVELS: Final = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_ACCESS_LOGGER: Final = "uvicorn.access"

# Component loggers whose level a previous configure_logging call set; reset on reconfiguration.
_configured_components: set[str] = set()


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """What the redaction step may let through."""

    mask_phones: bool = True
    allow_transcripts: bool = False

    @classmethod
    def from_settings(cls, settings: BaseServiceSettings) -> RedactionPolicy:
        """Phones unmasked only with ``LOG_LEVEL=DEBUG`` in local; transcripts per ``transcripts_allowed``."""
        return cls(
            mask_phones=not (settings.log_level == "DEBUG" and settings.is_local),
            allow_transcripts=transcripts_allowed(settings),
        )


def transcripts_allowed(settings: BaseServiceSettings) -> bool:
    """``LOG_TRANSCRIPTS`` is forced false in production."""
    return settings.log_transcripts and not settings.is_production


def redact(data: Mapping[str, Any], policy: RedactionPolicy, *, hipaa: bool = False) -> dict[str, Any]:
    """Return a redacted copy of ``data``; the input is never mutated.

    Raw-media keys are dropped, transcript keys are dropped unless the policy allows them and ``hipaa`` is
    false, secret-named keys keep their key with the value replaced by ``[REDACTED]``, bytes become
    ``<N bytes>``, and phone numbers inside strings are masked when the policy says so. Containers nested
    deeper than ``MAX_DEPTH`` are replaced by ``[REDACTED]``.
    """
    return _redact_mapping(data, policy, allow_transcripts=policy.allow_transcripts and not hipaa, depth=0)


def _redact_mapping(
    data: Mapping[str, Any], policy: RedactionPolicy, *, allow_transcripts: bool, depth: int
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key)
        if _dropped(name, allow_transcripts=allow_transcripts):
            continue
        if is_secret_key(name):
            result[key] = REDACTED
        else:
            result[key] = _redact_value(value, policy, allow_transcripts=allow_transcripts, depth=depth + 1)
    return result


def _dropped(name: str, *, allow_transcripts: bool) -> bool:
    lowered = name.lower()
    if lowered in RAW_MEDIA_KEYS:
        return True
    return lowered in TRANSCRIPT_KEYS and not allow_transcripts


def _redact_value(value: Any, policy: RedactionPolicy, *, allow_transcripts: bool, depth: int) -> Any:
    if isinstance(value, str):
        return mask_phones_in_text(value) if policy.mask_phones else value
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    if not isinstance(value, Mapping | list | tuple):
        return value
    if depth >= MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return _redact_mapping(value, policy, allow_transcripts=allow_transcripts, depth=depth)
    items = [
        _redact_value(item, policy, allow_transcripts=allow_transcripts, depth=depth + 1) for item in value
    ]
    return items if isinstance(value, list) else tuple(items)


class RedactProcessor:
    """structlog processor applying :func:`redact`; a bound ``hipaa_mode`` drops transcripts."""

    def __init__(self, policy: RedactionPolicy) -> None:
        self.policy = policy

    def __call__(
        self, _logger: object, _method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        return redact(event_dict, self.policy, hipaa=bool(event_dict.get("hipaa_mode")))


def otel_trace_processor(
    _logger: object, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Add ``trace_id`` (32 hex) and ``span_id`` (16 hex) of the current OpenTelemetry span, if any."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


class _StaticFields:
    """Add the per-process standard fields (``service``, ``env``, ``version``, ``pod``)."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        self.fields = dict(fields)

    def __call__(
        self, _logger: object, _method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for key, value in self.fields.items():
            event_dict.setdefault(key, value)
        return event_dict


class _VaHandler(logging.StreamHandler[TextIO]):
    """Root handler installed by :func:`configure_logging`; its type marks it for replacement."""


def component_levels(environ: Mapping[str, str]) -> dict[str, int]:
    """Parse ``LOG_LEVEL_<COMPONENT>=<LEVEL>`` variables into ``{"<component>": level}``.

    The component is the lower-cased suffix and names a stdlib logger
    (``LOG_LEVEL_VA_MEDIA`` -> ``va_media``).
    """
    levels: dict[str, int] = {}
    for name, raw in environ.items():
        match = _COMPONENT_LEVEL_VAR.match(name)
        if match is None:
            continue
        level = raw.strip().upper()
        if level not in _LEVELS:
            msg = f"{name} must be one of {sorted(_LEVELS)}, got {raw!r}"
            raise ValueError(msg)
        levels[match.group("component").lower()] = _LEVELS[level]
    return levels


def _serialize(obj: Any, **_: Any) -> str:
    # Lenient on purpose: a log line must never raise because of an odd value.
    return orjson.dumps(obj, default=str, option=orjson.OPT_NON_STR_KEYS).decode()


def _shared_processors(settings: BaseServiceSettings, fields: Mapping[str, str]) -> list[Processor]:
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _StaticFields(fields),
        structlog.processors.StackInfoRenderer(),
    ]
    if settings.log_format is LogFormat.JSON:
        # ConsoleRenderer formats exceptions itself and warns when they arrive pre-rendered.
        processors.append(structlog.processors.format_exc_info)
    processors += [otel_trace_processor, RedactProcessor(RedactionPolicy.from_settings(settings))]
    return processors


def _renderer(settings: BaseServiceSettings) -> Processor:
    if settings.log_format is LogFormat.JSON:
        return structlog.processors.JSONRenderer(serializer=_serialize)
    return structlog.dev.ConsoleRenderer(colors=False)


def _install_root_handler(handler: logging.Handler, level: str) -> None:
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _VaHandler)]:
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level)


def _apply_component_levels(levels: Mapping[str, int]) -> None:
    for name in _configured_components - set(levels):
        logging.getLogger(name).setLevel(logging.NOTSET)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    _configured_components.clear()
    _configured_components.update(levels)


def configure_logging(
    settings: BaseServiceSettings,
    *,
    version: str = "unknown",
    pod: str | None = None,
    stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Configure structlog and the stdlib root logger; safe to call again (replaces the handler)."""
    env = os.environ if environ is None else environ
    fields = {
        "service": settings.service_name,
        "env": settings.va_env.value,
        "version": version,
        "pod": pod or resolve_pod(env),
    }
    shared = _shared_processors(settings, fields)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = _VaHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, _renderer(settings)],
        )
    )
    _install_root_handler(handler, settings.log_level)
    _apply_component_levels(component_levels(env))
    # Access lines come from the API middleware (specs/27-observability.md §1), not uvicorn.
    logging.getLogger(_ACCESS_LOGGER).disabled = True


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a stdlib-backed structlog logger; ``name`` selects the stdlib logger (and its level)."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name, **initial_values)
    return logger


def bind_call_context(*, org_id: UUID | str, call_id: UUID | str, turn_index: int | None = None) -> None:
    """Bind ``org_id``/``call_id`` (and ``turn_index`` when given) to every line in the current context."""
    values: dict[str, Any] = {"org_id": str(org_id), "call_id": str(call_id)}
    if turn_index is not None:
        values["turn_index"] = turn_index
    structlog.contextvars.bind_contextvars(**values)


def bind_turn(turn_index: int) -> None:
    """Bind the current dialogue turn."""
    structlog.contextvars.bind_contextvars(turn_index=turn_index)


def bound_context(**values: Any) -> AbstractContextManager[None]:
    """Bind ``values`` for the duration of a ``with`` block, restoring the previous context on exit."""
    return structlog.contextvars.bound_contextvars(**values)


def clear_context() -> None:
    """Remove every context-bound field (start of a new request, call, or task)."""
    structlog.contextvars.clear_contextvars()
