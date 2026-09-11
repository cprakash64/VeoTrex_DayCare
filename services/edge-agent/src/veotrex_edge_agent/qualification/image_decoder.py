from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageFile, UnidentifiedImageError

MAX_ENCODED_BYTES = 25 * 1024 * 1024
MAX_WIDTH = 12_000
MAX_HEIGHT = 12_000
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False


class ImageDecodeError(ValueError):
    """Bounded qualification image decoding failure."""


@dataclass(frozen=True, slots=True)
class DecodedImage:
    rgb: NDArray[np.uint8]
    encoded_bytes: int
    source_mode: str


def decode_image(path: Path) -> DecodedImage:
    try:
        size = path.stat().st_size
        if size < 1 or size > MAX_ENCODED_BYTES:
            raise ImageDecodeError("invalid_encoded_size")
        encoded = path.read_bytes()
        with Image.open(io.BytesIO(encoded)) as image:
            image.verify()
        with Image.open(io.BytesIO(encoded)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width > MAX_WIDTH or height > MAX_HEIGHT:
                raise ImageDecodeError("invalid_image_dimensions")
            if width * height > MAX_PIXELS:
                raise ImageDecodeError("decoded_image_too_large")
            mode = image.mode
            if mode in {"1", "L", "I", "I;16", "F"}:
                converted = image.convert("L").convert("RGB")
            elif mode == "RGBA":
                background = Image.new("RGBA", image.size, (0, 0, 0, 255))
                converted = Image.alpha_composite(background, image).convert("RGB")
            elif mode == "RGB":
                converted = image
            else:
                raise ImageDecodeError("unsupported_image_mode")
            rgb = np.asarray(converted, dtype=np.uint8).copy()
    except ImageDecodeError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
        raise ImageDecodeError("malformed_image") from None
    return DecodedImage(rgb=rgb, encoded_bytes=size, source_mode=mode)
