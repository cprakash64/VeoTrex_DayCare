from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from veotrex_edge_agent.image_inference import ReferenceImageDetector
from veotrex_edge_agent.image_pipeline import (
    PADDING_VALUE,
    ImageTransform,
    PixelFormat,
    add_source_coordinates,
    inverse_box,
    preprocess_image,
)
from veotrex_edge_agent.qualification.image_decoder import ImageDecodeError, decode_image
from veotrex_edge_agent.qualification.person_evaluator import (
    GroundTruth,
    Prediction,
    coco_bbox_to_xyxy,
    density_bucket,
    match_persons,
    size_bucket,
)
from veotrex_edge_agent.qualification.yolox_reference import (
    compare_tensors,
    pillow_official_algorithm_reference,
)


@pytest.mark.parametrize(
    "height,width", [(640, 640), (240, 640), (640, 240), (17, 1000), (1000, 17), (1, 1), (333, 517)]
)
def test_shapes_scales_padding_and_determinism(height: int, width: int) -> None:
    image = np.arange(height * width * 3, dtype=np.uint8).reshape(height, width, 3)
    first = preprocess_image(image, pixel_format=PixelFormat.BGR8)
    second = preprocess_image(image, pixel_format=PixelFormat.BGR8)
    expected = min(640 / height, 640 / width)
    assert first.transform.scale == expected
    assert first.transform.resized_width == int(width * expected)
    assert first.transform.resized_height == int(height * expected)
    assert (
        first.tensor.shape == (1, 3, 640, 640)
        and first.tensor.dtype == np.float32
        and first.tensor.flags.c_contiguous
    )
    assert np.array_equal(first.tensor, second.tensor)
    rh, rw = first.transform.resized_height, first.transform.resized_width
    if rh < 640:
        assert np.all(first.tensor[0, :, rh:, :] == PADDING_VALUE)
    if rw < 640:
        assert np.all(first.tensor[0, :, :, rw:] == PADDING_VALUE)


def test_channel_contract_no_normalization_and_top_left() -> None:
    rgb = np.array([[[10, 20, 30]]], dtype=np.uint8)
    converted = preprocess_image(rgb, pixel_format=PixelFormat.RGB8)
    preserved = preprocess_image(rgb, pixel_format=PixelFormat.BGR8)
    assert converted.tensor[0, :, 0, 0].tolist() == [30.0, 20.0, 10.0]
    assert preserved.tensor[0, :, 0, 0].tolist() == [10.0, 20.0, 30.0]
    assert converted.tensor.max() == 30.0


def test_independent_official_algorithm_translation_matches_contract() -> None:
    rng = np.random.default_rng(20260910)
    image = rng.integers(0, 256, (333, 517, 3), dtype=np.uint8)
    production = preprocess_image(image, pixel_format=PixelFormat.RGB8)
    reference, scale, width, height = pillow_official_algorithm_reference(
        image, pixel_format=PixelFormat.RGB8
    )
    difference = compare_tensors(production.tensor, reference)
    assert (scale, width, height) == (
        production.transform.scale,
        production.transform.resized_width,
        production.transform.resized_height,
    )
    # Pillow and OpenCV-style bilinear rounding differ by at most one for this fixture.
    assert difference.maximum_absolute_difference <= 1.0
    assert difference.mean_absolute_difference < 0.2


def test_pixel_format_must_be_explicit_and_input_valid() -> None:
    with pytest.raises(ValueError, match="pixel_format"):
        preprocess_image(np.zeros((1, 1, 3), dtype=np.uint8), pixel_format="RGB8")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_decoded"):
        preprocess_image(np.zeros((1, 1), dtype=np.uint8), pixel_format=PixelFormat.RGB8)


def transform(width: int, height: int) -> ImageTransform:
    scale = min(640 / height, 640 / width)
    return ImageTransform(
        width,
        height,
        640,
        640,
        scale,
        int(width * scale),
        int(height * scale),
        0,
        0,
        640 - int(width * scale),
        640 - int(height * scale),
        PixelFormat.RGB8,
    )


@pytest.mark.parametrize(("width", "height"), [(640, 640), (1280, 720), (720, 1280), (517, 333)])
def test_coordinate_round_trip(width: int, height: int) -> None:
    metadata = transform(width, height)
    source = (1.25, 2.5, width - 0.75, height - 1.5)
    model = (
        source[0] * metadata.scale,
        source[1] * metadata.scale,
        source[2] * metadata.scale,
        source[3] * metadata.scale,
    )
    actual = inverse_box(model, metadata)
    assert actual == pytest.approx(source, abs=1e-9)


def test_inverse_clipping_padding_fractional_and_invalid() -> None:
    metadata = transform(1280, 720)
    assert inverse_box((-10.0, -5.0, 700.0, 640.0), metadata) == (0.0, 0.0, 1280.0, 720.0)
    assert inverse_box((0.5, 0.25, 639.5, 359.5), metadata) == pytest.approx(
        (1.0, 0.5, 1279.0, 719.0)
    )
    assert inverse_box((0, 500, 10, 600), metadata) is None
    assert inverse_box((4, 4, 4, 8), metadata) is None
    assert inverse_box((0, 0, math.nan, 1), metadata) is None


def test_source_detection_schema_preserves_model_box() -> None:
    metadata = transform(1280, 720)
    source = add_source_coordinates(
        [{"class": "PERSON", "bbox_xyxy_model": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}}], metadata
    )
    assert source[0]["bbox_xyxy_model"] == {"x1": 1, "y1": 2, "x2": 3, "y2": 4}
    assert source[0]["bbox_xyxy_source"] == {"x1": 2.0, "y1": 4.0, "x2": 6.0, "y2": 8.0}


def test_reference_detector_returns_both_coordinate_spaces() -> None:
    class FakeSupervisor:
        def infer_tensor(self, tensor: bytes, **metadata: object) -> dict[str, object]:
            assert len(tensor) == 4_915_200
            return {
                "frame_id": metadata["frame_id"],
                "detections": [
                    {
                        "class": "PERSON",
                        "bbox_xyxy_model": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
                    }
                ],
            }

    detector = ReferenceImageDetector(FakeSupervisor())  # type: ignore[arg-type]
    result = detector.infer_decoded(
        np.zeros((320, 640, 3), dtype=np.uint8),
        pixel_format=PixelFormat.RGB8,
        frame_id="fixture",
    )
    detection = result["detections"][0]  # type: ignore[index]
    assert "bbox_xyxy_model" in detection and "bbox_xyxy_source" in detection
    assert result["transform"]["version"] == "yolox-top-left-bgr-v1"  # type: ignore[index]


def save(path: Path, mode: str, size: tuple[int, int] = (4, 3)) -> None:
    Image.new(mode, size).save(path, format="PNG")


@pytest.mark.parametrize("mode", ["RGB", "L", "RGBA"])
def test_decoder_normalizes_supported_modes(tmp_path: Path, mode: str) -> None:
    path = tmp_path / f"{mode}.bin"
    save(path, mode)
    decoded = decode_image(path)
    assert (
        decoded.rgb.shape == (3, 4, 3)
        and decoded.rgb.dtype == np.uint8
        and decoded.source_mode == mode
    )


@pytest.mark.parametrize("payload", [b"", b"not-an-image", b"\x89PNG\r\n\x1a\n"])
def test_decoder_rejects_empty_invalid_and_corrupt(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "bad.jpg"
    path.write_bytes(payload)
    with pytest.raises(ImageDecodeError):
        decode_image(path)


def test_decoder_rejects_oversized_dimensions_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wide.png"
    save(path, "RGB", (101, 1))
    monkeypatch.setattr("veotrex_edge_agent.qualification.image_decoder.MAX_WIDTH", 100)
    with pytest.raises(ImageDecodeError, match="dimensions"):
        decode_image(path)


def test_matching_is_score_ordered_one_to_one_and_crowd_aware() -> None:
    truths = [
        GroundTruth((0, 0, 10, 10), 100),
        GroundTruth((20, 20, 30, 30), 100),
        GroundTruth((40, 40, 60, 60), 400, True),
    ]
    predictions = [
        Prediction((0, 0, 10, 10), 0.8),
        Prediction((0, 0, 10, 10), 0.9),
        Prediction((40, 40, 60, 60), 0.7),
        Prediction((80, 80, 90, 90), 0.6),
    ]
    result = match_persons(predictions, truths, iou_threshold=0.5)
    assert (
        result.true_positives,
        result.false_positives,
        result.false_negatives,
        result.ignored_predictions,
    ) == (1, 2, 1, 1)
    assert result.precision == pytest.approx(1 / 3) and result.recall == 0.5


def test_evaluator_boundaries() -> None:
    assert coco_bbox_to_xyxy([1, 2, 3, 4]) == (1, 2, 4, 6)
    with pytest.raises(ValueError):
        coco_bbox_to_xyxy([0, 0, 0, 1])
    assert [size_bucket(x) for x in (100, 1024, 9216)] == ["small", "medium", "large"]
    assert [density_bucket(x) for x in (0, 1, 2, 5, 10)] == ["0", "1", "2-4", "5-9", "10+"]
