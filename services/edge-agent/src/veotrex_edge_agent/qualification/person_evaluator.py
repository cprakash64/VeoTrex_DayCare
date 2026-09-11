from __future__ import annotations

from dataclasses import dataclass

Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class GroundTruth:
    box: Box
    area: float
    iscrowd: bool = False


@dataclass(frozen=True, slots=True)
class Prediction:
    box: Box
    score: float


@dataclass(frozen=True, slots=True)
class MatchResult:
    true_positives: int
    false_positives: int
    false_negatives: int
    ignored_predictions: int
    matched_ground_truth_indices: tuple[int, ...]

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        denominator = self.precision + self.recall
        return 2 * self.precision * self.recall / denominator if denominator else 0.0


def intersection_over_union(left: Box, right: Box) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = width * height
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def intersection_over_prediction(prediction: Box, region: Box) -> float:
    width = max(0.0, min(prediction[2], region[2]) - max(prediction[0], region[0]))
    height = max(0.0, min(prediction[3], region[3]) - max(prediction[1], region[1]))
    intersection = width * height
    area = max(0.0, prediction[2] - prediction[0]) * max(0.0, prediction[3] - prediction[1])
    return intersection / area if area else 0.0


def match_persons(
    predictions: list[Prediction],
    ground_truths: list[GroundTruth],
    *,
    iou_threshold: float,
    crowd_overlap_threshold: float = 0.5,
) -> MatchResult:
    ordinary = [(index, truth) for index, truth in enumerate(ground_truths) if not truth.iscrowd]
    crowds = [truth for truth in ground_truths if truth.iscrowd]
    matched: set[int] = set()
    true_positives = false_positives = ignored = 0
    ordered = sorted(enumerate(predictions), key=lambda item: (-item[1].score, item[0]))
    for _, prediction in ordered:
        options = [
            (intersection_over_union(prediction.box, truth.box), index)
            for index, truth in ordinary
            if index not in matched
        ]
        best_iou, best_index = max(options, default=(0.0, -1), key=lambda item: (item[0], -item[1]))
        if best_iou >= iou_threshold:
            matched.add(best_index)
            true_positives += 1
        elif any(
            intersection_over_prediction(prediction.box, crowd.box) >= crowd_overlap_threshold
            for crowd in crowds
        ):
            ignored += 1
        else:
            false_positives += 1
    return MatchResult(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=len(ordinary) - len(matched),
        ignored_predictions=ignored,
        matched_ground_truth_indices=tuple(sorted(matched)),
    )


def coco_bbox_to_xyxy(value: list[float]) -> Box:
    if len(value) != 4:
        raise ValueError("invalid_coco_bbox")
    x, y, width, height = map(float, value)
    if width <= 0 or height <= 0:
        raise ValueError("invalid_coco_bbox")
    return x, y, x + width, y + height


def size_bucket(area: float) -> str:
    if area < 32**2:
        return "small"
    if area < 96**2:
        return "medium"
    return "large"


def density_bucket(count: int) -> str:
    if count == 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    if count <= 9:
        return "5-9"
    return "10+"
