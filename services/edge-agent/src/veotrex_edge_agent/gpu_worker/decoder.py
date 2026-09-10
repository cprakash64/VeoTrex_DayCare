from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

PERSON_CLASS_ID = 0
PERSON_CLASS = "PERSON"
MODEL_SIZE = 640
YOLOX_STRIDES = (8, 16, 32)
YOLOX_ROWS = 8_400
YOLOX_FEATURES = 85


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    candidate_score_threshold: float = 0.25
    nms_iou_threshold: float = 0.45
    pre_nms_max_candidates: int = 300
    post_nms_max_detections: int = 100

    def __post_init__(self) -> None:
        if not 0.0 <= self.candidate_score_threshold <= 1.0:
            raise ValueError("invalid_candidate_threshold")
        if not 0.0 <= self.nms_iou_threshold <= 1.0:
            raise ValueError("invalid_nms_threshold")
        if not 1 <= self.pre_nms_max_candidates <= YOLOX_ROWS:
            raise ValueError("invalid_pre_nms_limit")
        if not 1 <= self.post_nms_max_detections <= self.pre_nms_max_candidates:
            raise ValueError("invalid_post_nms_limit")


@dataclass(frozen=True, slots=True)
class Candidate:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    source_index: int


def grid_position(index: int) -> tuple[int, int, int]:
    if not 0 <= index < YOLOX_ROWS:
        raise ValueError("invalid_yolox_row")
    offset = 0
    for stride in YOLOX_STRIDES:
        side = MODEL_SIZE // stride
        count = side * side
        if index < offset + count:
            local = index - offset
            return local % side, local // side, stride
        offset += count
    raise AssertionError("unreachable")


def decode_person_candidates(
    values: Sequence[float], config: DetectionConfig | None = None
) -> tuple[list[Candidate], int]:
    config = config or DetectionConfig()
    if len(values) != YOLOX_ROWS * YOLOX_FEATURES:
        raise ValueError("invalid_yolox_output_size")
    candidates: list[Candidate] = []
    anomalies = 0
    for index in range(YOLOX_ROWS):
        offset = index * YOLOX_FEATURES
        raw_x, raw_y, raw_w, raw_h = values[offset : offset + 4]
        objectness, person_probability = values[offset + 4], values[offset + 5]
        fields = (raw_x, raw_y, raw_w, raw_h, objectness, person_probability)
        if not all(math.isfinite(value) for value in fields):
            anomalies += 1
            continue
        score = objectness * person_probability
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            anomalies += 1
            continue
        if score < config.candidate_score_threshold:
            continue
        grid_x, grid_y, stride = grid_position(index)
        try:
            width = math.exp(raw_w) * stride
            height = math.exp(raw_h) * stride
        except OverflowError:
            anomalies += 1
            continue
        center_x = (raw_x + grid_x) * stride
        center_y = (raw_y + grid_y) * stride
        decoded = (center_x, center_y, width, height)
        if not all(math.isfinite(value) for value in decoded) or width <= 0 or height <= 0:
            anomalies += 1
            continue
        x1 = max(0.0, min(float(MODEL_SIZE), center_x - width / 2))
        y1 = max(0.0, min(float(MODEL_SIZE), center_y - height / 2))
        x2 = max(0.0, min(float(MODEL_SIZE), center_x + width / 2))
        y2 = max(0.0, min(float(MODEL_SIZE), center_y + height / 2))
        if x2 <= x1 or y2 <= y1:
            anomalies += 1
            continue
        candidates.append(Candidate(x1, y1, x2, y2, score, index))
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.source_index))
    return candidates[: config.pre_nms_max_candidates], anomalies


def intersection_over_union(left: Candidate, right: Candidate) -> float:
    width = max(0.0, min(left.x2, right.x2) - max(left.x1, right.x1))
    height = max(0.0, min(left.y2, right.y2) - max(left.y1, right.y1))
    intersection = width * height
    left_area = (left.x2 - left.x1) * (left.y2 - left.y1)
    right_area = (right.x2 - right.x1) * (right.y2 - right.y1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def non_maximum_suppression(
    candidates: Sequence[Candidate], config: DetectionConfig | None = None
) -> list[Candidate]:
    config = config or DetectionConfig()
    valid = [
        candidate
        for candidate in candidates
        if all(
            math.isfinite(value)
            for value in (candidate.x1, candidate.y1, candidate.x2, candidate.y2, candidate.score)
        )
        and candidate.x2 > candidate.x1
        and candidate.y2 > candidate.y1
        and 0.0 <= candidate.score <= 1.0
    ]
    valid.sort(key=lambda candidate: (-candidate.score, candidate.source_index))
    pending = valid[: config.pre_nms_max_candidates]
    selected: list[Candidate] = []
    while pending and len(selected) < config.post_nms_max_detections:
        current = pending.pop(0)
        selected.append(current)
        pending = [
            candidate
            for candidate in pending
            if intersection_over_union(current, candidate) <= config.nms_iou_threshold
        ]
    return selected


def person_detections(
    values: Sequence[float], config: DetectionConfig | None = None
) -> tuple[list[dict[str, object]], int]:
    config = config or DetectionConfig()
    candidates, anomalies = decode_person_candidates(values, config)
    selected = non_maximum_suppression(candidates, config)
    return [
        {
            "class": PERSON_CLASS,
            "class_id": PERSON_CLASS_ID,
            "score": candidate.score,
            "bbox_xyxy_model": {
                "x1": candidate.x1,
                "y1": candidate.y1,
                "x2": candidate.x2,
                "y2": candidate.y2,
            },
        }
        for candidate in selected
    ], anomalies
