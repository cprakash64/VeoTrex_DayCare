from __future__ import annotations

import statistics
import time
from collections import defaultdict
from dataclasses import asdict
from itertools import pairwise
from typing import Any

from veotrex_edge_agent.tracking import PersonDetection, PersonTracker, TrackingConfig


def _percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean": statistics.mean(values),
        "p10": _percentile(values, 0.10),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def analyze_cache(cache: dict[str, Any]) -> dict[str, Any]:
    frames = cache["frames"]
    durations: dict[int, list[float]] = defaultdict(list)
    confirmed_counts: list[int] = []
    detection_counts: list[int] = []
    tracking_times: list[float] = []
    low_recoveries = lost = recovered = removed = tentative_observations = 0
    for frame in frames:
        tracking = frame["tracking"]
        confirmed = tracking["confirmed_tracks"]
        tentative = tracking["tentative_tracks"]
        confirmed_counts.append(len(confirmed))
        detection_counts.append(len(frame["detections"]))
        tracking_times.append(float(tracking["tracking_update_ms"]))
        tentative_observations += len(tentative)
        lost += len(tracking["lost_track_ids"])
        recovered += len(tracking["recovered_track_ids"])
        removed += len(tracking["removed_track_ids"])
        low_recoveries += sum(track["was_low_score_recovery"] for track in confirmed)
        for track in confirmed:
            durations[int(track["track_id"])].append(float(frame["timestamp"]))
    track_durations = [max(values) - min(values) for values in durations.values()]
    changes = [b - a for a, b in pairwise(confirmed_counts)]
    raw_changes = [b - a for a, b in pairwise(detection_counts)]
    total_seconds = max((float(x["timestamp"]) for x in frames), default=0.0)
    return {
        "frames": len(frames),
        "duration_seconds": total_seconds,
        "tracks_created_or_observed": len(durations),
        "tracks_per_minute": len(durations) / (total_seconds / 60) if total_seconds else 0,
        "track_duration_seconds": summarize(track_durations) if track_durations else {},
        "extremely_short_track_fraction": (
            sum(value < 0.5 for value in track_durations) / len(track_durations)
            if track_durations
            else 0
        ),
        "tentative_observations": tentative_observations,
        "low_score_recovery_observations": low_recoveries,
        "lost_transitions": lost,
        "recovered_transitions": recovered,
        "removed_transitions": removed,
        "raw_count_standard_deviation": statistics.pstdev(detection_counts),
        "confirmed_count_standard_deviation": statistics.pstdev(confirmed_counts),
        "raw_one_frame_spikes": sum(abs(value) > 1 for value in raw_changes),
        "confirmed_one_frame_spikes": sum(abs(value) > 1 for value in changes),
        "confirmed_one_frame_drops": sum(value < -1 for value in changes),
        "tracking_update_ms": summarize(tracking_times),
        "wall_seconds": cache["wall_seconds"],
        "real_time_factor": total_seconds / cache["wall_seconds"] if cache["wall_seconds"] else 0,
        "worker_restart_count": cache["worker_restart_count"],
    }


def replay_cached_detections(
    cache: dict[str, Any],
    *,
    cadence_fps: int,
    source_fps: int,
    source_width: int,
    source_height: int,
    config: TrackingConfig | None = None,
) -> dict[str, Any]:
    if cadence_fps < 1 or cadence_fps > source_fps:
        raise ValueError("invalid_replay_cadence")
    tracker = PersonTracker(config)
    frames: list[dict[str, Any]] = []
    last_slot = -1
    started = time.perf_counter()
    for source in cache["frames"]:
        timestamp = float(source["timestamp"])
        slot = int(timestamp * cadence_fps + 1e-9)
        if slot == last_slot:
            continue
        last_slot = slot
        detections = [
            PersonDetection(tuple(item["bbox_xyxy_source"]), float(item["score"]))
            for item in source["detections"]
        ]
        tracked = tracker.update(
            "cadence-replay",
            len(frames),
            timestamp,
            detections,
            source_width=source_width,
            source_height=source_height,
        )
        frames.append(
            {
                "sequence": len(frames),
                "timestamp": timestamp,
                "detections": [asdict(item) for item in detections],
                "tracking": asdict(tracked),
            }
        )
    return {
        "frames": frames,
        "wall_seconds": time.perf_counter() - started,
        "worker_restart_count": 0,
    }
