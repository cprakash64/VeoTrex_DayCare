from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from veotrex_edge_agent.qualification.metrics import BoundedSamples


@dataclass(frozen=True, slots=True)
class CompressedSample:
    arrival: float
    pts_ns: int | None
    dts_ns: int | None
    size: int


@dataclass(frozen=True, slots=True)
class DecodedSample:
    arrival: float
    pts_ns: int | None


class TimestampSource(StrEnum):
    MEDIA_PTS = "MEDIA_PTS"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class MediaTimestamp:
    """Explicit media timing contract for future consumers (tracker sampling).

    ``stream_instance`` equals the session generation: PTS values are only comparable within one
    instance, and ``discontinuity`` marks the first buffer of a new instance or a large reversal.
    Arrival time is monotonic and is never silently substituted for a missing PTS; consumers see
    ``source=MISSING`` and must choose an explicit policy.
    """

    stream_instance: int
    sequence: int
    pts_ns: int | None
    arrival_monotonic: float
    source: TimestampSource
    discontinuity: bool


class _Clock:
    __slots__ = ("duplicates", "last", "missing", "regressions")

    def __init__(self) -> None:
        self.last: int | None = None
        self.duplicates = 0
        self.regressions = 0
        self.missing = 0

    def observe(self, value: int | None) -> int | None:
        """Return the reversal magnitude (ns) if the timestamp went backwards."""
        if value is None:
            self.missing += 1
            return None
        reversal: int | None = None
        if self.last is not None:
            if value == self.last:
                self.duplicates += 1
            elif value < self.last:
                self.regressions += 1
                reversal = self.last - value
        self.last = value
        return reversal


class MediaTimeline:
    """Per-camera timing bookkeeping with stale-generation rejection. Bounded memory."""

    def __init__(
        self, *, max_reversal_ns: int = 500_000_000, gap_sample_capacity: int = 65_536
    ) -> None:
        if max_reversal_ns <= 0:
            raise ValueError("max reversal must be positive")
        self._max_reversal_ns = max_reversal_ns
        self.instance: int | None = None
        self.instances_started = 0
        self._compressed_pts = _Clock()
        self._compressed_dts = _Clock()
        self._decoded_pts = _Clock()
        self._pending_discontinuity = False
        self._last_decoded_arrival: float | None = None
        self._recent: deque[float] = deque(maxlen=512)
        self.decoded_gaps = BoundedSamples(gap_sample_capacity)
        self.sequence = 0
        self.compressed_buffers = 0
        self.compressed_bytes = 0
        self.decoded_buffers = 0
        self.stale_rejected = 0
        self.large_reversals = 0
        self.discontinuities = 0
        self.arrival_regressions = 0
        self.last_timestamp: MediaTimestamp | None = None
        # Totals across instances for the qualification report.
        self._totals = {"pts_dup": 0, "pts_reg": 0, "pts_missing": 0, "dts_dup": 0, "dts_reg": 0}
        self._decoded_totals = {"dup": 0, "reg": 0, "missing": 0}

    def begin_instance(self, generation: int) -> None:
        if self.instance is not None and generation <= self.instance:
            raise ValueError("stream instances must increase monotonically")
        self._fold_totals()
        self.instance = generation
        self.instances_started += 1
        self._compressed_pts = _Clock()
        self._compressed_dts = _Clock()
        self._decoded_pts = _Clock()
        self._last_decoded_arrival = None
        self._recent.clear()
        self.sequence = 0
        self._pending_discontinuity = True
        if self.instances_started > 1:
            self.discontinuities += 1

    def _fold_totals(self) -> None:
        self._totals["pts_dup"] += self._compressed_pts.duplicates
        self._totals["pts_reg"] += self._compressed_pts.regressions
        self._totals["pts_missing"] += self._compressed_pts.missing
        self._totals["dts_dup"] += self._compressed_dts.duplicates
        self._totals["dts_reg"] += self._compressed_dts.regressions
        self._decoded_totals["dup"] += self._decoded_pts.duplicates
        self._decoded_totals["reg"] += self._decoded_pts.regressions
        self._decoded_totals["missing"] += self._decoded_pts.missing

    def accept_compressed(self, generation: int, sample: CompressedSample) -> bool:
        if generation != self.instance:
            self.stale_rejected += 1
            return False
        self.compressed_buffers += 1
        self.compressed_bytes += max(0, sample.size)
        self._compressed_pts.observe(sample.pts_ns)
        self._compressed_dts.observe(sample.dts_ns)
        return True

    def accept_decoded(self, generation: int, sample: DecodedSample) -> MediaTimestamp | None:
        if generation != self.instance:
            self.stale_rejected += 1
            return None
        discontinuity = self._pending_discontinuity
        self._pending_discontinuity = False
        reversal = self._decoded_pts.observe(sample.pts_ns)
        if reversal is not None and reversal > self._max_reversal_ns:
            self.large_reversals += 1
            self.discontinuities += 1
            discontinuity = True
        if self._last_decoded_arrival is not None:
            gap = sample.arrival - self._last_decoded_arrival
            if gap < 0:
                self.arrival_regressions += 1
            else:
                self.decoded_gaps.add(gap)
        self._last_decoded_arrival = sample.arrival
        self._recent.append(sample.arrival)
        self.decoded_buffers += 1
        self.sequence += 1
        timestamp = MediaTimestamp(
            stream_instance=generation,
            sequence=self.sequence,
            pts_ns=sample.pts_ns,
            arrival_monotonic=sample.arrival,
            source=TimestampSource.MISSING if sample.pts_ns is None else TimestampSource.MEDIA_PTS,
            discontinuity=discontinuity,
        )
        self.last_timestamp = timestamp
        return timestamp

    def recent_rate(self, now: float, window_seconds: float = 5.0) -> float | None:
        recent = [arrival for arrival in self._recent if now - arrival <= window_seconds]
        if len(recent) < 2:
            return None
        span = recent[-1] - recent[0]
        return (len(recent) - 1) / span if span > 0 else None

    def summary(self) -> dict[str, Any]:
        compressed = {
            "pts_duplicates": self._totals["pts_dup"] + self._compressed_pts.duplicates,
            "pts_regressions": self._totals["pts_reg"] + self._compressed_pts.regressions,
            "pts_missing": self._totals["pts_missing"] + self._compressed_pts.missing,
            "dts_duplicates": self._totals["dts_dup"] + self._compressed_dts.duplicates,
            "dts_regressions": self._totals["dts_reg"] + self._compressed_dts.regressions,
        }
        gaps = self.decoded_gaps

        def ms(value: float | None) -> float | None:
            return None if value is None else round(value * 1000, 3)

        return {
            "stream_instances": self.instances_started,
            "compressed_buffers": self.compressed_buffers,
            "compressed_bytes": self.compressed_bytes,
            "decoded_buffers": self.decoded_buffers,
            "compressed_timestamps": compressed,
            "decoded_pts_duplicates": self._decoded_totals["dup"] + self._decoded_pts.duplicates,
            "decoded_pts_regressions": self._decoded_totals["reg"] + self._decoded_pts.regressions,
            "decoded_pts_missing": self._decoded_totals["missing"] + self._decoded_pts.missing,
            "large_reversals": self.large_reversals,
            "discontinuities": self.discontinuities,
            "arrival_regressions": self.arrival_regressions,
            "stale_generation_rejected": self.stale_rejected,
            "inter_buffer_gap_samples": gaps.count,
            "inter_buffer_gap_retained": gaps.retained_count,
            "p50_gap_ms": ms(gaps.percentile(50)),
            "p95_gap_ms": ms(gaps.percentile(95)),
            "p99_gap_ms": ms(gaps.percentile(99)),
            "max_gap_ms": ms(gaps.maximum),
        }
