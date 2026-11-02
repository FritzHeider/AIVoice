"""e2e_metrics: Prometheus scrape parsing and histogram quantiles between two scrapes."""

from __future__ import annotations

import math

import pytest
from e2e_metrics import histogram_quantile, parse_metrics, sample, sample_sum

BEFORE = """\
# HELP va_audio_underruns_total Outbound audio underruns
# TYPE va_audio_underruns_total counter
va_audio_underruns_total 2.0
# HELP va_media_active_calls Attached calls
# TYPE va_media_active_calls gauge
va_media_active_calls{transport="telnyx"} 3.0
va_media_active_calls{transport="widget"} 1.0
# HELP va_event_loop_lag_ms Loop lag
# TYPE va_event_loop_lag_ms histogram
va_event_loop_lag_ms_bucket{le="1.0"} 10.0
va_event_loop_lag_ms_bucket{le="5.0"} 10.0
va_event_loop_lag_ms_bucket{le="10.0"} 10.0
va_event_loop_lag_ms_bucket{le="+Inf"} 10.0
va_event_loop_lag_ms_count 10.0
va_event_loop_lag_ms_sum 4.0
"""


def _after(b1: int, b5: int, b10: int, inf: int) -> str:
    return (
        "# TYPE va_event_loop_lag_ms histogram\n"
        f'va_event_loop_lag_ms_bucket{{le="1.0"}} {b1}\n'
        f'va_event_loop_lag_ms_bucket{{le="5.0"}} {b5}\n'
        f'va_event_loop_lag_ms_bucket{{le="10.0"}} {b10}\n'
        f'va_event_loop_lag_ms_bucket{{le="+Inf"}} {inf}\n'
        f"va_event_loop_lag_ms_count {inf}\n"
        "va_event_loop_lag_ms_sum 99.0\n"
    )


def test_samples_keep_their_suffixes_and_labels() -> None:
    metrics = parse_metrics(BEFORE)
    assert sample(metrics, "va_audio_underruns_total") == 2.0
    assert sample(metrics, "va_media_active_calls", transport="telnyx") == 3.0
    assert sample(metrics, "va_event_loop_lag_ms_count") == 10.0
    assert sample(metrics, "va_missing_total") == 0.0
    assert sample(metrics, "va_media_active_calls") == 0.0
    assert sample_sum(metrics, "va_media_active_calls") == 4.0


def test_quantiles_use_only_observations_between_the_scrapes() -> None:
    before = parse_metrics(BEFORE)
    # 100 new observations: 90 in <= 1 ms, 8 in (1, 5], 1 in (5, 10], 1 above 10.
    after = parse_metrics(_after(100, 108, 109, 110))
    assert histogram_quantile(before, after, "va_event_loop_lag_ms", 0.5) == 1.0
    assert histogram_quantile(before, after, "va_event_loop_lag_ms", 0.9) == 1.0
    assert histogram_quantile(before, after, "va_event_loop_lag_ms", 0.95) == 5.0
    assert histogram_quantile(before, after, "va_event_loop_lag_ms", 0.99) == 10.0
    assert histogram_quantile(before, after, "va_event_loop_lag_ms", 1.0) == math.inf


def test_a_quantile_needs_new_observations_and_a_valid_q() -> None:
    before = parse_metrics(BEFORE)
    with pytest.raises(ValueError, match="no va_event_loop_lag_ms observations"):
        histogram_quantile(before, before, "va_event_loop_lag_ms", 0.5)
    with pytest.raises(ValueError, match="no va_missing observations"):
        histogram_quantile(before, before, "va_missing", 0.5)
    with pytest.raises(ValueError, match="q must be in"):
        histogram_quantile(before, before, "va_event_loop_lag_ms", 0.0)
