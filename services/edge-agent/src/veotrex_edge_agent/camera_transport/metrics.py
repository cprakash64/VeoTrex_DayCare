from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory

COUNTERS = (
    "camera_transport_sessions_total",
    "camera_transport_connect_failures_total",
    "camera_transport_reconnects_total",
    "camera_transport_stalls_total",
    "camera_transport_renewals_total",
    "camera_transport_renewal_failures_total",
    "camera_transport_decoder_errors_total",
    "camera_transport_media_buffers_total",
    "camera_transport_decoded_buffers_total",
    "camera_transport_timestamp_discontinuities_total",
    "camera_transport_stale_generation_buffers_total",
)
GAUGES = ("camera_transport_up", "camera_media_flowing", "camera_decoder_up")
_TIMING_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_GAP_BUCKETS = (0.01, 0.034, 0.067, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
HISTOGRAMS: dict[str, tuple[float, ...]] = {
    "camera_session_acquire_seconds": _TIMING_BUCKETS,
    "camera_time_to_first_media_seconds": _TIMING_BUCKETS,
    "camera_time_to_first_decoded_buffer_seconds": _TIMING_BUCKETS,
    "camera_inter_buffer_gap_seconds": _GAP_BUCKETS,
}
METRIC_PREFIX = "veotrex_"


class Histogram:
    __slots__ = ("bounds", "count", "counts", "total")

    def __init__(self, bounds: tuple[float, ...]) -> None:
        if not bounds or list(bounds) != sorted(set(bounds)):
            raise ValueError("histogram bounds must be strictly increasing")
        self.bounds = bounds
        self.counts = [0] * (len(bounds) + 1)
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            return
        index = next((i for i, bound in enumerate(self.bounds) if value <= bound), len(self.bounds))
        self.counts[index] += 1
        self.count += 1
        self.total += value


class TransportMetrics:
    """Per-camera, low-cardinality metrics.

    The only label values are the logical camera UUID (bounded by site inventory) and, for
    failures, the fixed taxonomy. Tokens, URLs, session references, provider device IDs, and
    account IDs cannot be labels because no API accepts free-form label values.
    """

    def __init__(self, camera_id: UUID) -> None:
        if not isinstance(camera_id, UUID):
            raise TypeError("metrics label must be a logical camera UUID")
        self.camera_id = camera_id
        self.counters = dict.fromkeys(COUNTERS, 0)
        self.gauges = dict.fromkeys(GAUGES, 0)
        self.failures: dict[TransportErrorCategory, int] = {}
        self.histograms = {name: Histogram(bounds) for name, bounds in HISTOGRAMS.items()}

    def inc(self, name: str, amount: int = 1) -> None:
        if name not in self.counters:
            raise KeyError("unknown counter")
        if amount < 0:
            raise ValueError("counters only increase")
        self.counters[name] += amount

    def set_gauge(self, name: str, value: bool) -> None:
        if name not in self.gauges:
            raise KeyError("unknown gauge")
        self.gauges[name] = int(value)

    def observe(self, name: str, value: float) -> None:
        self.histograms[name].observe(value)

    def failure(self, category: TransportErrorCategory) -> None:
        category = TransportErrorCategory(category)
        self.failures[category] = self.failures.get(category, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "camera_id": str(self.camera_id),
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "failures_by_category": {key.value: value for key, value in self.failures.items()},
            "histograms": {
                name: {"count": item.count, "sum": round(item.total, 6)}
                for name, item in self.histograms.items()
            },
        }


def label_sets(metrics: Iterable[TransportMetrics]) -> set[tuple[tuple[str, str], ...]]:
    """All distinct label sets that exposition would emit (used by cardinality tests)."""
    result: set[tuple[tuple[str, str], ...]] = set()
    for item in metrics:
        camera = ("camera_id", str(item.camera_id))
        result.add((camera,))
        for category in item.failures:
            result.add((camera, ("category", category.value)))
    return result


def render_prometheus(metrics: Iterable[TransportMetrics]) -> str:
    lines: list[str] = []
    for item in metrics:
        camera = f'camera_id="{item.camera_id}"'
        for name, value in item.counters.items():
            lines.append(f"{METRIC_PREFIX}{name}{{{camera}}} {value}")
        for name, value in item.gauges.items():
            lines.append(f"{METRIC_PREFIX}{name}{{{camera}}} {value}")
        for category, value in sorted(item.failures.items()):
            labels = f'{camera},category="{category.value}"'
            lines.append(f"{METRIC_PREFIX}camera_transport_failures_total{{{labels}}} {value}")
        for name, histogram in item.histograms.items():
            cumulative = 0
            for bound, count in zip(histogram.bounds, histogram.counts, strict=False):
                cumulative += count
                lines.append(f'{METRIC_PREFIX}{name}_bucket{{{camera},le="{bound}"}} {cumulative}')
            lines.append(f'{METRIC_PREFIX}{name}_bucket{{{camera},le="+Inf"}} {histogram.count}')
            lines.append(f"{METRIC_PREFIX}{name}_sum{{{camera}}} {histogram.total}")
            lines.append(f"{METRIC_PREFIX}{name}_count{{{camera}}} {histogram.count}")
    return "\n".join(lines) + ("\n" if lines else "")
