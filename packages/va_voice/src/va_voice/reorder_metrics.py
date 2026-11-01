"""Reorder buffer metrics (specs/07-media-pipeline.md §5.1, specs/27-observability.md §3.3).

The buffer runs in the media-worker, so it records into that service's catalog metrics.
"""

from __future__ import annotations

from va_common.metrics import ServiceMetrics

__all__ = ["CatalogReorderMetrics"]


class CatalogReorderMetrics:
    """:class:`va_voice.reorder.ReorderObserver` over ``va_audio_reorder_total`` and
    ``va_audio_gap_frames_total``.

    Duplicates are kept in :class:`va_voice.reorder.ReorderStats` only (no catalog metric).
    """

    def __init__(self, metrics: ServiceMetrics) -> None:
        self._reordered = metrics.counter("va_audio_reorder_total")
        self._gaps = metrics.counter("va_audio_gap_frames_total")

    def reordered(self, count: int) -> None:
        self._reordered.inc(count)

    def gap_filled(self, count: int) -> None:
        self._gaps.inc(count)

    def duplicate(self, count: int) -> None:
        del count
