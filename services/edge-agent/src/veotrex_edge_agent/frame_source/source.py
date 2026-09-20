"""Provider-neutral frame source contract.

A frame source hands decoded RGB frames to the detector. It knows nothing about detection,
tracking or occupancy, and nothing downstream needs to know which source produced a frame -
except for one thing that must never be lost: whether the imagery is live or recorded. That
travels with every frame as ``SourceKind`` so a recorded clip can never present itself as a
live camera further up the stack.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageFile, UnidentifiedImageError

MAX_WIDTH = 7_680
MAX_HEIGHT = 4_320
MAX_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False


class SourceKind(StrEnum):
    """What the imagery actually is. Never inferred, never defaulted."""

    RECORDED_DEMO = "RECORDED_DEMO"
    LIVE_RING = "LIVE_RING"


class SourceHealth(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ENDED = "ENDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class FrameSourceError(RuntimeError):
    """A safe frame-source failure category. Never carries a path or a payload."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class SourceFrame:
    """One decoded frame plus the provenance the UI is required to show."""

    kind: SourceKind
    stream_instance_id: str
    sequence: int
    # Position inside the source medium. For a looping clip this restarts at every loop.
    media_timestamp_seconds: float
    # Strictly increasing across loops. The tracker needs monotonic time: feeding it a
    # timestamp that jumps backwards at a loop boundary would look like a discontinuity.
    monotonic_timestamp_seconds: float
    loop_index: int
    width: int
    height: int
    rgb: NDArray[np.uint8]
    encoded_jpeg: bytes


@dataclass(frozen=True, slots=True)
class SourceStatus:
    kind: SourceKind
    health: SourceHealth
    frames_emitted: int
    loops_completed: int
    decode_failures: int
    last_error_category: str | None


class FrameSource(Protocol):
    """Minimum contract the monitoring pipeline depends on."""

    @property
    def kind(self) -> SourceKind: ...

    def status(self) -> SourceStatus: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


def decode_jpeg(payload: bytes) -> NDArray[np.uint8]:
    """Bounded JPEG -> RGB decode.

    Deliberately separate from the qualification image decoder: that one reads a path and
    belongs to the offline harness, and runtime code must not depend on the harness. The
    safety bounds are the same in spirit - refuse anything whose decoded size is not sane
    rather than letting a malformed frame allocate freely.
    """
    if not payload:
        raise FrameSourceError("empty_frame")
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width > MAX_WIDTH or height > MAX_HEIGHT:
                raise FrameSourceError("invalid_frame_dimensions")
            if width * height > MAX_PIXELS:
                raise FrameSourceError("frame_too_large")
            converted = image if image.mode == "RGB" else image.convert("RGB")
            return np.asarray(converted, dtype=np.uint8).copy()
    except FrameSourceError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
        raise FrameSourceError("malformed_frame") from None
