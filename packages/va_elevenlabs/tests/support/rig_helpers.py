"""The observation half of a client test rig: a manual clock, a media-worker metrics registry, and an
in-memory span exporter."""

from __future__ import annotations

from typing import Any

from clock_helpers import ManualClock
from obs_helpers import memory_provider

from va_common.metrics import Service, build_metrics, new_registry

__all__ = ["Observed"]


class Observed:
    """``clock`` drives the client; ``service_metrics`` and ``provider`` are what the client records into."""

    def __init__(self) -> None:
        self.clock = ManualClock()
        self.registry = new_registry(process_collectors=False)
        self.service_metrics = build_metrics(Service.MEDIA_WORKER, self.registry)
        self.provider, self.exporter = memory_provider()

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0

    def spans(self, name: str) -> list[Any]:
        return [span for span in self.exporter.get_finished_spans() if span.name == name]
