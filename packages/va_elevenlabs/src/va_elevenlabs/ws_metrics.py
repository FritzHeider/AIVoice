"""Vendor WebSocket counters shared by the TTS and STT clients (specs/27-observability.md §3.3)."""

from __future__ import annotations

from va_common.metrics import ServiceMetrics

__all__ = ["VendorCounters"]


class VendorCounters:
    """``va_vendor_ws_reconnects_total`` and ``va_vendor_errors_total`` for one ``vendor`` label."""

    def __init__(self, metrics: ServiceMetrics, vendor: str) -> None:
        self._vendor = vendor
        self._reconnects = metrics.counter("va_vendor_ws_reconnects_total")
        self._errors = metrics.counter("va_vendor_errors_total")

    def reconnect(self) -> None:
        self._reconnects.labels(vendor=self._vendor).inc()

    def vendor_error(self, code: str) -> None:
        self._errors.labels(vendor=self._vendor, code=code).inc()
