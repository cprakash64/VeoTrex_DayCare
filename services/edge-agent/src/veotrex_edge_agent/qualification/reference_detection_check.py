from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.image_pipeline import PixelFormat, preprocess_image
from veotrex_edge_agent.qualification.image_decoder import decode_image
from veotrex_edge_agent.qualification.yolox_reference import (
    compare_tensors,
    pillow_official_algorithm_reference,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    paths = sorted(args.images.glob("*.jpg"))[: args.count]
    if not paths:
        parser.error("no JPEG images found")
    supervisor = GpuWorkerSupervisor()
    comparisons: list[dict[str, Any]] = []
    supervisor.start()
    supervisor.load_model()
    try:
        for path in paths:
            decoded = decode_image(path)
            production = preprocess_image(decoded.rgb, pixel_format=PixelFormat.RGB8)
            reference, scale, width, height = pillow_official_algorithm_reference(
                decoded.rgb, pixel_format=PixelFormat.RGB8
            )
            difference = compare_tensors(production.tensor, reference)
            first = supervisor.infer_tensor(production.tensor.tobytes(), frame_id=path.stem)
            second = supervisor.infer_tensor(reference.tobytes(), frame_id=f"{path.stem}-reference")
            first_detections = first["detections"]
            second_detections = second["detections"]
            same_count = len(first_detections) == len(second_detections)
            max_box_difference = 0.0
            max_score_difference = 0.0
            if same_count:
                for left, right in zip(first_detections, second_detections, strict=True):
                    max_box_difference = max(
                        max_box_difference,
                        max(
                            abs(float(a) - float(b))
                            for a, b in zip(
                                left["bbox_xyxy_model"].values(),
                                right["bbox_xyxy_model"].values(),
                                strict=True,
                            )
                        ),
                    )
                    max_score_difference = max(
                        max_score_difference, abs(float(left["score"]) - float(right["score"]))
                    )
            comparisons.append(
                {
                    "image": path.name,
                    "scale_equal": production.transform.scale == scale,
                    "dimensions_equal": (
                        production.transform.resized_width,
                        production.transform.resized_height,
                    )
                    == (width, height),
                    "tensor_maximum_absolute_difference": difference.maximum_absolute_difference,
                    "tensor_mean_absolute_difference": difference.mean_absolute_difference,
                    "tensor_differing_values": difference.differing_values,
                    "detection_count_equal": same_count,
                    "production_detections": len(first_detections),
                    "reference_detections": len(second_detections),
                    "maximum_box_difference": max_box_difference if same_count else None,
                    "maximum_score_difference": max_score_difference if same_count else None,
                }
            )
    finally:
        supervisor.stop()
    report = {
        "images": len(comparisons),
        "all_scale_and_dimensions_equal": all(
            x["scale_equal"] and x["dimensions_equal"] for x in comparisons
        ),
        "detection_count_agreement_images": sum(x["detection_count_equal"] for x in comparisons),
        "maximum_tensor_difference": max(
            x["tensor_maximum_absolute_difference"] for x in comparisons
        ),
        "mean_of_tensor_mean_differences": float(
            np.mean([x["tensor_mean_absolute_difference"] for x in comparisons])
        ),
        "comparisons": comparisons,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
