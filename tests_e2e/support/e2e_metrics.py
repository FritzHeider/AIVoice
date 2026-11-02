"""Prometheus text parsing for e2e checks that read a service's ``/metrics`` (specs/30 §6 Decision P05-T17e).

A scrape is a mapping ``(sample name, frozenset of label pairs) -> value``; sample names keep their suffixes
(``_total``, ``_bucket``, ``_count``). Histogram quantiles compare two scrapes, so they describe only what
happened between them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from prometheus_client.parser import text_string_to_metric_families

__all__ = ["Scrape", "histogram_quantile", "parse_metrics", "sample", "sample_sum"]

type Scrape = Mapping[tuple[str, frozenset[tuple[str, str]]], float]

_LE = "le"


def parse_metrics(text: str) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    """Every sample of a Prometheus text exposition."""
    samples: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
    for family in text_string_to_metric_families(text):
        for item in family.samples:
            samples[(item.name, frozenset(item.labels.items()))] = float(item.value)
    return samples


def sample(metrics: Scrape, name: str, **labels: str) -> float:
    """The value of the sample ``name`` with exactly ``labels`` (0.0 when absent)."""
    return metrics.get((name, frozenset(labels.items())), 0.0)


def sample_sum(metrics: Scrape, name: str) -> float:
    """The sum of every sample called ``name`` (across all label sets)."""
    return sum(value for (sample_name, _), value in metrics.items() if sample_name == name)


def histogram_quantile(before: Scrape, after: Scrape, name: str, q: float) -> float:
    """The upper bound of the bucket holding the ``q``-quantile of the observations made between two scrapes
    of the unlabeled histogram ``name`` (nearest rank; ``math.inf`` when it lies above the largest bound).

    Raises ``ValueError`` when nothing was observed in between or ``q`` is outside (0, 1].
    """
    if not 0 < q <= 1:
        message = f"q must be in (0, 1], got {q}"
        raise ValueError(message)
    bucket = f"{name}_bucket"
    deltas: list[tuple[float, float]] = []
    for (sample_name, labels), value in after.items():
        if sample_name != bucket:
            continue
        le = dict(labels)[_LE]
        deltas.append((float(le), value - before.get((sample_name, labels), 0.0)))
    deltas.sort()
    total = deltas[-1][1] if deltas else 0.0
    if total <= 0:
        message = f"no {name} observations between the two scrapes"
        raise ValueError(message)
    rank = math.ceil(q * total)
    for bound, cumulative in deltas:
        if cumulative >= rank:
            return bound
    return math.inf
