"""The person-detection boundary (V1-02B1A).

Before this stage the only detector was ``ReferenceImageDetector``, a concrete class wired
directly to the TensorRT GPU worker. That is fine for a qualification harness and wrong for a
pipeline: it makes every consumer depend on CUDA, on a TensorRT engine and on a Jetson, so the
tracking logic could not be tested or reasoned about without all three.

``PersonDetector`` is that boundary. Two implementations ship here:

``FakePersonDetector``
    Deterministic, dependency-free, and driven by a script the test supplies. It is how the
    whole pipeline - tracking, lifecycle, output, metrics - is exercised in CI with no model,
    no GPU and no video file.

``BoundingBoxValidator``
    Not a detector, but the rule every detector's output passes through. Applied by the
    pipeline rather than trusted from the implementation, because a detector is the one place
    where a model's arithmetic meets the rest of the system: a NaN, an inverted box or a
    coordinate a few pixels outside the frame must be resolved here and never reach the
    tracker, which would turn it into a Kalman state and a track id.

The real TensorRT detector lives in ``yolox.py`` so that importing this module never pulls in
CUDA, and so a machine with no GPU can still run everything except the real detector.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from veotrex_edge_agent.recorded.model import Box, DetectedPerson

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

# A frame yielding more boxes than this is a detector malfunction or an adversarial input;
# the tracker has its own bound, and this one stops the list before it ever gets there.
MAX_DETECTIONS_PER_FRAME = 300
# Boxes thinner than this in either dimension cannot be a person at any useful distance and
# break IoU arithmetic by having near-zero area.
MIN_BOX_SIDE_PX = 2.0


class DetectorError(Exception):
    """A bounded category. Never model internals, never pixels."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class PersonDetector(Protocol):
    """Finds people in one frame. Implementations must return *only* people.

    ``detect`` receives a BGR uint8 frame and must not retain it, mutate it, or write any part
    of it anywhere. It returns boxes in that frame's own pixel coordinates.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def detect(
        self, image: NDArray[np.uint8], *, frame_index: int, timestamp_ms: float
    ) -> list[DetectedPerson]: ...


@dataclass(frozen=True, slots=True)
class BoundingBoxValidator:
    """Clamps what is recoverable and rejects what is not.

    The distinction matters. A box overhanging the frame edge by a few pixels is ordinary -
    a person standing half out of shot - and clamping keeps that detection. A box that is
    inverted, non-finite or degenerate is not a misplaced person, it is a broken number, and
    silently repairing it would invent a detection that the model never made.
    """

    width: int
    height: int

    def clamp(self, box: Box) -> Box | None:
        x1, y1, x2, y2 = (float(value) for value in box)
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
            return None
        if x2 <= x1 or y2 <= y1:
            # Inverted or zero-area: a reordering would fabricate a plausible box from a
            # meaningless one, so it is dropped instead.
            return None
        clamped = (
            max(0.0, min(x1, float(self.width))),
            max(0.0, min(y1, float(self.height))),
            max(0.0, min(x2, float(self.width))),
            max(0.0, min(y2, float(self.height))),
        )
        if clamped[2] - clamped[0] < MIN_BOX_SIDE_PX or clamped[3] - clamped[1] < MIN_BOX_SIDE_PX:
            # Entirely, or almost entirely, outside the frame once clamped.
            return None
        return clamped

    def validate(self, detections: Sequence[DetectedPerson]) -> tuple[list[DetectedPerson], int]:
        """Return the usable detections and how many were rejected."""
        accepted: list[DetectedPerson] = []
        rejected = 0
        for detection in detections[:MAX_DETECTIONS_PER_FRAME]:
            confidence = float(detection.confidence)
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                rejected += 1
                continue
            if detection.label != "person":
                rejected += 1
                continue
            box = self.clamp(detection.bbox_xyxy)
            if box is None:
                rejected += 1
                continue
            accepted.append(
                DetectedPerson(
                    box,
                    confidence,
                    detection.frame_index,
                    detection.timestamp_ms,
                )
            )
        rejected += max(0, len(detections) - MAX_DETECTIONS_PER_FRAME)
        return accepted, rejected


class FakePersonDetector:
    """A detector that returns exactly what a test told it to.

    ``script`` maps a frame index to the boxes for that frame; a frame with no entry yields
    nothing, which is how a missed detection is expressed. Boxes are given as
    ``(x1, y1, x2, y2, confidence)`` so a test reads as a picture of what is happening rather
    than as object construction.
    """

    model_id = "fake-person-detector"
    model_version = "1"

    def __init__(self, script: dict[int, Sequence[tuple[float, float, float, float, float]]]):
        self._script = dict(script)
        self.frames_seen = 0

    def detect(
        self, image: NDArray[np.uint8], *, frame_index: int, timestamp_ms: float
    ) -> list[DetectedPerson]:
        self.frames_seen += 1
        return [
            DetectedPerson((x1, y1, x2, y2), score, frame_index, timestamp_ms)
            for x1, y1, x2, y2, score in self._script.get(frame_index, ())
        ]
