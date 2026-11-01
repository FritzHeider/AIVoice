"""STT metrics adapters: the catalog one records media-worker metrics; the null one records nothing."""

from __future__ import annotations

from va_common.metrics import Service, build_metrics, new_registry
from va_elevenlabs import CatalogSttMetrics, NullSttMetrics, SttMetrics


def exercise(metrics: SttMetrics) -> None:
    metrics.final_ms(80.0)
    metrics.reconnect()
    metrics.vendor_error("rate_limited")


def test_catalog_metrics_record_every_series() -> None:
    registry = new_registry(process_collectors=False)
    exercise(CatalogSttMetrics(build_metrics(Service.MEDIA_WORKER, registry)))
    assert registry.get_sample_value("va_stt_final_ms_sum") == 80.0
    assert registry.get_sample_value("va_vendor_ws_reconnects_total", {"vendor": "elevenlabs_stt"}) == 1
    errors = {"vendor": "elevenlabs_stt", "code": "rate_limited"}
    assert registry.get_sample_value("va_vendor_errors_total", errors) == 1


def test_null_metrics_accept_every_call() -> None:
    exercise(NullSttMetrics())
