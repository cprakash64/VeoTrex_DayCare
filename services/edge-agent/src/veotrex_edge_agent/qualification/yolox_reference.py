from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from veotrex_edge_agent.image_pipeline import MODEL_HEIGHT, MODEL_WIDTH, PADDING_VALUE, PixelFormat


@dataclass(frozen=True, slots=True)
class ReferenceComparison:
    maximum_absolute_difference: float
    mean_absolute_difference: float
    differing_values: int


def pillow_official_algorithm_reference(
    image: NDArray[np.uint8], *, pixel_format: PixelFormat
) -> tuple[NDArray[np.float32], float, int, int]:
    """Independent qualification translation of the official YOLOX steps using Pillow."""
    source_height, source_width, _ = image.shape
    scale = min(MODEL_HEIGHT / source_height, MODEL_WIDTH / source_width)
    resized_width, resized_height = int(source_width * scale), int(source_height * scale)
    bgr = image[..., ::-1] if pixel_format is PixelFormat.RGB8 else image
    resized = np.asarray(
        Image.fromarray(bgr).resize((resized_width, resized_height), Image.Resampling.BILINEAR),
        dtype=np.uint8,
    )
    canvas = np.full((MODEL_HEIGHT, MODEL_WIDTH, 3), PADDING_VALUE, dtype=np.uint8)
    canvas[:resized_height, :resized_width] = resized
    return (
        np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32),
        scale,
        resized_width,
        resized_height,
    )


def compare_tensors(
    production: NDArray[np.float32], reference: NDArray[np.float32]
) -> ReferenceComparison:
    difference = np.abs(production - reference)
    return ReferenceComparison(
        maximum_absolute_difference=float(difference.max()),
        mean_absolute_difference=float(difference.mean()),
        differing_values=int(np.count_nonzero(difference)),
    )
