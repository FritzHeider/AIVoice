"""TTS socket metrics (specs/09 §12, specs/27-observability.md §3.3; Decision P03-T10 rule 9).

The socket runs in the media-worker, so it records into that service's catalog metrics.
"""

from __future__ import annotations

from typing import Final, Protocol

from va_common.metrics import ServiceMetrics

__all__ = ["VENDOR", "CatalogTtsMetrics", "NullTtsMetrics", "TtsMetrics"]

VENDOR: Final = "elevenlabs_tts"


class TtsMetrics(Protocol):
    def ttfa(self, model: str, ms: float) -> None: ...

    def characters(self, model: str, count: int) -> None: ...

    def reconnect(self) -> None: ...

    def vendor_error(self, code: str) -> None: ...

    def late_dropped(self) -> None: ...


class CatalogTtsMetrics:
    """``va_tts_ttfa_ms``, ``va_tts_characters_total``, ``va_vendor_ws_reconnects_total``,
    ``va_vendor_errors_total``, ``va_tts_late_audio_dropped_total`` of a media-worker
    :class:`ServiceMetrics`."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._ttfa = metrics.histogram("va_tts_ttfa_ms")
        self._characters = metrics.counter("va_tts_characters_total")
        self._reconnects = metrics.counter("va_vendor_ws_reconnects_total")
        self._errors = metrics.counter("va_vendor_errors_total")
        self._late = metrics.counter("va_tts_late_audio_dropped_total")

    def ttfa(self, model: str, ms: float) -> None:
        self._ttfa.labels(model=model).observe(ms)

    def characters(self, model: str, count: int) -> None:
        self._characters.labels(model=model).inc(count)

    def reconnect(self) -> None:
        self._reconnects.labels(vendor=VENDOR).inc()

    def vendor_error(self, code: str) -> None:
        self._errors.labels(vendor=VENDOR, code=code).inc()

    def late_dropped(self) -> None:
        self._late.inc()


class NullTtsMetrics:
    """Records nothing (tools and tests that do not read metrics)."""

    def ttfa(self, model: str, ms: float) -> None:
        del model, ms

    def characters(self, model: str, count: int) -> None:
        del model, count

    def reconnect(self) -> None:
        return

    def vendor_error(self, code: str) -> None:
        del code

    def late_dropped(self) -> None:
        return
