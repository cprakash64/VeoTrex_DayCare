from __future__ import annotations

import time
from dataclasses import asdict
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import NDArray

from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.image_pipeline import PixelFormat, add_source_coordinates, preprocess_image


class DetectionProfile(StrEnum):
    NORMAL = "NORMAL"
    TRACKING_HIGH_RECALL = "TRACKING_HIGH_RECALL"


class ReferenceImageDetector:
    """Correctness-reference decoded-image path; future camera paths must match it."""

    def __init__(self, supervisor: GpuWorkerSupervisor) -> None:
        self._supervisor = supervisor

    def infer_decoded(
        self,
        image: NDArray[np.uint8],
        *,
        pixel_format: PixelFormat,
        frame_id: str,
        profile: DetectionProfile = DetectionProfile.NORMAL,
    ) -> dict[str, Any]:
        started = time.perf_counter_ns()
        prepared = preprocess_image(image, pixel_format=pixel_format)
        inference_started = time.perf_counter_ns()
        threshold = 0.05 if profile is DetectionProfile.TRACKING_HIGH_RECALL else 0.25
        result = self._supervisor.infer_tensor(
            prepared.tensor.tobytes(),
            frame_id=frame_id,
            candidate_score_threshold=threshold,
            nms_iou_threshold=0.45,
            qualification_candidates=False,
        )
        source_started = time.perf_counter_ns()
        result["detections"] = add_source_coordinates(result["detections"], prepared.transform)
        source_mapping_ms = (time.perf_counter_ns() - source_started) / 1e6
        result["transform"] = asdict(prepared.transform)
        result["image_timing"] = {
            "color_conversion_ms": prepared.color_conversion_ms,
            "resize_ms": prepared.resize_ms,
            "tensor_construction_ms": prepared.tensor_construction_ms,
            "decoded_frame_to_detection_ms": (time.perf_counter_ns() - started) / 1e6,
            "tensor_rpc_ms": (source_started - inference_started) / 1e6,
            "source_mapping_ms": source_mapping_ms,
        }
        return result
