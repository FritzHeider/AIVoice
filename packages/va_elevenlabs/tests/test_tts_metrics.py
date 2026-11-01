"""TTS metrics adapters: the catalog one records media-worker metrics; the null one records nothing."""

from __future__ import annotations

from va_common.metrics import Service, build_metrics, new_registry
from va_elevenlabs import CatalogTtsMetrics, NullTtsMetrics, TtsMetrics


def exercise(metrics: TtsMetrics) -> None:
    metrics.ttfa("eleven_flash_v2_5", 120.0)
    metrics.characters("eleven_flash_v2_5", 12)
    metrics.reconnect()
    metrics.vendor_error("quota_exceeded")
    metrics.late_dropped()


def test_catalog_metrics_record_every_series() -> None:
    registry = new_registry(process_collectors=False)
    exercise(CatalogTtsMetrics(build_metrics(Service.MEDIA_WORKER, registry)))
    model = {"model": "eleven_flash_v2_5"}
    assert registry.get_sample_value("va_tts_ttfa_ms_sum", model) == 120.0
    assert registry.get_sample_value("va_tts_characters_total", model) == 12
    assert registry.get_sample_value("va_vendor_ws_reconnects_total", {"vendor": "elevenlabs_tts"}) == 1
    errors = {"vendor": "elevenlabs_tts", "code": "quota_exceeded"}
    assert registry.get_sample_value("va_vendor_errors_total", errors) == 1
    assert registry.get_sample_value("va_tts_late_audio_dropped_total") == 1


def test_null_metrics_accept_every_call() -> None:
    exercise(NullTtsMetrics())
