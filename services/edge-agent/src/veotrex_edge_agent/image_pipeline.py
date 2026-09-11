from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import numpy as np
from numpy.typing import NDArray

MODEL_WIDTH = 640
MODEL_HEIGHT = 640
PADDING_VALUE = 114
TRANSFORM_VERSION = "yolox-top-left-bgr-v1"


class PixelFormat(StrEnum):
    RGB8 = "RGB8"
    BGR8 = "BGR8"


@dataclass(frozen=True, slots=True)
class ImageTransform:
    source_width: int
    source_height: int
    model_width: int
    model_height: int
    scale: float
    resized_width: int
    resized_height: int
    pad_left: int
    pad_top: int
    pad_right: int
    pad_bottom: int
    source_pixel_format: PixelFormat
    model_pixel_format: PixelFormat = PixelFormat.BGR8
    version: str = TRANSFORM_VERSION


@dataclass(frozen=True, slots=True)
class PreprocessedImage:
    tensor: NDArray[np.float32]
    transform: ImageTransform
    color_conversion_ms: float
    resize_ms: float
    tensor_construction_ms: float


def _bilinear_resize(image: NDArray[np.uint8], width: int, height: int) -> NDArray[np.uint8]:
    """Resize with OpenCV-compatible half-pixel bilinear coordinate placement."""
    source_height, source_width, _ = image.shape
    if (width, height) == (source_width, source_height):
        return image.copy()
    x = (np.arange(width, dtype=np.float64) + 0.5) * source_width / width - 0.5
    y = (np.arange(height, dtype=np.float64) + 0.5) * source_height / height - 0.5
    x = np.clip(x, 0.0, source_width - 1.0)
    y = np.clip(y, 0.0, source_height - 1.0)
    x0, y0 = np.floor(x).astype(np.intp), np.floor(y).astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, source_width - 1), np.minimum(y0 + 1, source_height - 1)
    wx, wy = x - x0, y - y0
    top = image[y0[:, None], x0] * (1.0 - wx)[None, :, None]
    top += image[y0[:, None], x1] * wx[None, :, None]
    bottom = image[y1[:, None], x0] * (1.0 - wx)[None, :, None]
    bottom += image[y1[:, None], x1] * wx[None, :, None]
    resized = top * (1.0 - wy)[:, None, None] + bottom * wy[:, None, None]
    return cast(NDArray[np.uint8], np.clip(np.rint(resized), 0, 255).astype(np.uint8))


def preprocess_image(image: NDArray[np.uint8], *, pixel_format: PixelFormat) -> PreprocessedImage:
    if not isinstance(pixel_format, PixelFormat):
        raise ValueError("pixel_format_must_be_explicit")
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("invalid_decoded_image")
    source_height, source_width, _ = image.shape
    if source_width < 1 or source_height < 1:
        raise ValueError("invalid_decoded_image")
    scale = min(MODEL_HEIGHT / source_height, MODEL_WIDTH / source_width)
    # int() intentionally truncates positive dimensions toward zero, matching official YOLOX.
    resized_width = int(source_width * scale)
    resized_height = int(source_height * scale)
    stage = time.perf_counter_ns()
    bgr = image[..., ::-1] if pixel_format is PixelFormat.RGB8 else image
    color_conversion_ms = (time.perf_counter_ns() - stage) / 1e6
    stage = time.perf_counter_ns()
    resized = _bilinear_resize(bgr, resized_width, resized_height)
    resize_ms = (time.perf_counter_ns() - stage) / 1e6
    stage = time.perf_counter_ns()
    canvas = np.full((MODEL_HEIGHT, MODEL_WIDTH, 3), PADDING_VALUE, dtype=np.uint8)
    canvas[:resized_height, :resized_width] = resized
    tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32)
    tensor_construction_ms = (time.perf_counter_ns() - stage) / 1e6
    transform = ImageTransform(
        source_width=source_width,
        source_height=source_height,
        model_width=MODEL_WIDTH,
        model_height=MODEL_HEIGHT,
        scale=scale,
        resized_width=resized_width,
        resized_height=resized_height,
        pad_left=0,
        pad_top=0,
        pad_right=MODEL_WIDTH - resized_width,
        pad_bottom=MODEL_HEIGHT - resized_height,
        source_pixel_format=pixel_format,
    )
    return PreprocessedImage(
        tensor=tensor,
        transform=transform,
        color_conversion_ms=color_conversion_ms,
        resize_ms=resize_ms,
        tensor_construction_ms=tensor_construction_ms,
    )


def inverse_box(
    box: tuple[float, float, float, float], transform: ImageTransform
) -> tuple[float, float, float, float] | None:
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        return None
    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1 or transform.scale <= 0:
        return None
    source = (
        max(0.0, min(float(transform.source_width), x1 / transform.scale)),
        max(0.0, min(float(transform.source_height), y1 / transform.scale)),
        max(0.0, min(float(transform.source_width), x2 / transform.scale)),
        max(0.0, min(float(transform.source_height), y2 / transform.scale)),
    )
    return source if source[2] > source[0] and source[3] > source[1] else None


def add_source_coordinates(
    detections: list[dict[str, object]], transform: ImageTransform
) -> list[dict[str, object]]:
    mapped: list[dict[str, object]] = []
    for detection in detections:
        model = detection.get("bbox_xyxy_model")
        if not isinstance(model, dict):
            continue
        try:
            box = tuple(float(model[key]) for key in ("x1", "y1", "x2", "y2"))
        except (KeyError, TypeError, ValueError):
            continue
        source = inverse_box(box, transform)  # type: ignore[arg-type]
        if source is None:
            continue
        mapped.append(
            {
                **detection,
                "bbox_xyxy_source": dict(zip(("x1", "y1", "x2", "y2"), source, strict=True)),
                "source_width": transform.source_width,
                "source_height": transform.source_height,
                "transform_version": transform.version,
            }
        )
    return mapped
