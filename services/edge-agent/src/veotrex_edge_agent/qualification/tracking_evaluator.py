from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
from scipy.optimize import linear_sum_assignment  # type: ignore[import-untyped]

from veotrex_edge_agent.tracking import TrackView


@dataclass(frozen=True, slots=True)
class GroundTruthTrack:
    identity: str
    box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class SyntheticTrackingMetrics:
    matched_gt_observations: int
    missed_gt_observations: int
    false_track_observations: int
    id_switches: int
    track_fragmentations: int
    fully_retained_gt_trajectories: int
    tracker_trajectories_per_gt: dict[str, int]
    initialization_delay_frames: dict[str, int]


def _iou(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = (
        (left[2] - left[0]) * (left[3] - left[1])
        + (right[2] - right[0]) * (right[3] - right[1])
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def evaluate_synthetic_replay(
    truths: list[list[GroundTruthTrack]],
    outputs: list[list[TrackView]],
    *,
    minimum_iou: float = 0.5,
) -> SyntheticTrackingMetrics:
    if len(truths) != len(outputs):
        raise ValueError("frame_count_mismatch")
    assignments: dict[str, list[tuple[int, int]]] = defaultdict(list)
    first_gt: dict[str, int] = {}
    matched = missed = false = 0
    for frame_index, (frame_truth, frame_output) in enumerate(zip(truths, outputs, strict=True)):
        for item in frame_truth:
            first_gt.setdefault(item.identity, frame_index)
        if not frame_truth or not frame_output:
            missed += len(frame_truth)
            false += len(frame_output)
            continue
        cost = np.array(
            [
                [1 - _iou(gt.box, track.bbox_xyxy_source) for track in frame_output]
                for gt in frame_truth
            ]
        )
        rows, columns = linear_sum_assignment(cost)
        accepted = [
            (int(r), int(c))
            for r, c in zip(rows, columns, strict=True)
            if 1 - cost[r, c] >= minimum_iou
        ]
        matched += len(accepted)
        missed += len(frame_truth) - len(accepted)
        false += len(frame_output) - len(accepted)
        for row, column in accepted:
            assignments[frame_truth[row].identity].append(
                (frame_index, frame_output[column].track_id)
            )
    switches = fragments = retained = 0
    counts: dict[str, int] = {}
    delays: dict[str, int] = {}
    for identity, first_frame in first_gt.items():
        observed = assignments[identity]
        ids = [item[1] for item in observed]
        switches += sum(a != b for a, b in pairwise(ids))
        fragments += sum(b[0] > a[0] + 1 for a, b in pairwise(observed))
        counts[identity] = len(set(ids))
        delays[identity] = observed[0][0] - first_frame if observed else len(truths) - first_frame
        if observed and len(observed) == sum(
            identity == x.identity for frame in truths for x in frame
        ):
            retained += 1
    return SyntheticTrackingMetrics(
        matched, missed, false, switches, fragments, retained, counts, delays
    )
