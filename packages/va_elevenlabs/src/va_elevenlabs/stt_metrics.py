"""Realtime STT metrics (specs/08 §12, specs/27-observability.md §3.3; Decision P03-T11 rule 9).

The client runs in the media-worker, so it records into that service's catalog metrics.
"""

from __future__ import annotations

from typing import Final, Protocol

from va_common.metrics import ServiceMetrics
from va_elevenlabs.ws_metrics import VendorCounters

__all__ = ["VENDOR", "CatalogSttMetrics", "NullSttMetrics", "SttMetrics"]

VENDOR: Final = "elevenlabs_stt"


class SttMetrics(Protocol):
    def final_ms(self, ms: float) -> None: ...

    def reconnect(self) -> None: ...

    def vendor_error(self, code: str) -> None: ...


class CatalogSttMetrics:
    """``va_stt_final_ms``, ``va_vendor_ws_reconnects_total``, ``va_vendor_errors_total`` of a media-worker
    :class:`ServiceMetrics` (vendor ``elevenlabs_stt``)."""

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._final = metrics.histogram("va_stt_final_ms")
        self._vendor = VendorCounters(metrics, VENDOR)

    def final_ms(self, ms: float) -> None:
        self._final.observe(ms)

    def reconnect(self) -> None:
        self._vendor.reconnect()

    def vendor_error(self, code: str) -> None:
        self._vendor.vendor_error(code)


class NullSttMetrics:
    """Records nothing (tools and tests that do not read metrics)."""

    def final_ms(self, ms: float) -> None:
        del ms

    def reconnect(self) -> None:
        return

    def vendor_error(self, code: str) -> None:
        del code
