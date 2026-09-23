"""Streaming recorded-video input for local evaluation (V1-02B1A).

Frames are yielded one at a time and never accumulated: the iterator holds exactly one decoded
frame, so a ten-minute 1080p video costs the same memory as a ten-second one. That is a
requirement rather than an optimisation - this pipeline is aimed at a Jetson Orin Nano with
roughly 8 GB shared between CPU and GPU, and the existing R4D replay harness buffers every
frame to disk first, which is precisely what this must not do.

**Timestamps come from the media timeline, not from the clock.** Processing a video slower or
faster than its source frame rate must not change a single emitted timestamp, because every
temporal feature built on top of this (entry/exit reasoning, dwell time, event clips) is only
as correct as its time axis. Wall-clock timing would silently encode how busy the machine was.

The exact OpenCV semantics were measured on this repository's demo file rather than assumed,
because they are easy to get wrong by one frame: with the FFMPEG backend,
``CAP_PROP_POS_MSEC`` read *before* ``read()`` lags by one frame, while read *immediately
after* a successful ``read()`` it is the presentation timestamp of the frame just returned.
This module therefore reads it after, and still treats it as advisory: a timeline that is
absent, non-finite or goes backwards is rejected in favour of the frame rate, so a container
with a broken index degrades to correct uniform timestamps instead of emitting nonsense.

Only local filesystem paths are accepted. No URL of any kind is opened in this stage.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

# Resource bounds. A frame larger than this is refused rather than decoded: at 4 bytes per
# pixel a single 8K frame is already ~130 MB, and this runs beside a GPU worker.
MAX_FRAME_PIXELS = 8_294_400  # 3840x2160
MAX_FRAME_SIDE = 4_096
MIN_FRAME_SIDE = 16
# A frame rate outside this range means a broken or hostile container, not a real recording.
MIN_SOURCE_FPS = 0.1
MAX_SOURCE_FPS = 1_000.0
# Consecutive decode failures tolerated before the source gives up. A few corrupt frames in a
# long recording are survivable and are skipped; a wall of them is a broken file.
MAX_CONSECUTIVE_DECODE_FAILURES = 30
# Accepted containers. Deliberately a small allow-list rather than "whatever OpenCV opens".
SUPPORTED_SUFFIXES = frozenset({".mp4", ".m4v", ".mov"})


class VideoSourceError(Exception):
    """A bounded category. Never a decoder message, never a full filesystem path."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class VideoFrame:
    """One decoded frame. ``image`` is a BGR uint8 array owned by the consumer for the
    duration of one iteration only; the source does not retain it."""

    frame_index: int
    timestamp_ms: float
    width: int
    height: int
    image: NDArray[np.uint8]

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let pixel data reach a log line or a traceback.
        return (
            f"VideoFrame(index={self.frame_index}, ts={self.timestamp_ms:.1f}ms, "
            f"{self.width}x{self.height})"
        )


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    width: int
    height: int
    source_fps: float | None
    frame_count: int | None
    duration_ms: float | None


class RecordedVideoSource:
    """A bounded, streaming reader over one local recorded video file.

    Use as a context manager so the underlying capture is released on every path, including
    an exception mid-iteration:

        with RecordedVideoSource(path) as source:
            for frame in source.frames():
                ...
    """

    def __init__(self, path: Path, *, sample_every: int = 1) -> None:
        if sample_every < 1:
            raise VideoSourceError("invalid_sampling")
        self._path = Path(path)
        self._sample_every = sample_every
        self._capture: Any | None = None
        self._metadata: VideoMetadata | None = None
        self._timeline_trusted = True
        self._frames_decoded = 0
        self._frames_failed = 0

    # ------------------------------------------------------------------------------ opening
    @staticmethod
    def _open_capture(path: Path) -> Any:
        try:
            import cv2
        except ImportError:
            raise VideoSourceError("video_backend_unavailable") from None
        # A str path, never a URL: cv2.VideoCapture would happily accept "http://..." or a
        # device index, and neither is acceptable input in this stage.
        capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        if not capture.isOpened():
            capture.release()
            raise VideoSourceError("video_unreadable")
        return capture

    def _validate_path(self) -> None:
        if self._path.is_symlink():
            raise VideoSourceError("video_symlink_rejected")
        try:
            status = self._path.stat()
        except OSError:
            raise VideoSourceError("video_not_found") from None
        if not self._path.is_file():
            raise VideoSourceError("video_not_a_regular_file")
        if status.st_size == 0:
            raise VideoSourceError("video_empty")
        if self._path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise VideoSourceError("video_unsupported_container")

    def open(self) -> VideoMetadata:
        """Open and validate the file, returning what is known about it.

        Every refusal happens here, before a single frame is decoded, so a caller never has to
        unwind a half-started pipeline because the file turned out to be unusable.
        """
        if self._capture is not None:
            return self._require_metadata()
        self._validate_path()
        import cv2

        capture = self._open_capture(self._path)
        try:
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            raw_fps = float(capture.get(cv2.CAP_PROP_FPS))
            raw_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            if width < MIN_FRAME_SIDE or height < MIN_FRAME_SIDE:
                raise VideoSourceError("video_dimensions_too_small")
            if (
                width > MAX_FRAME_SIDE
                or height > MAX_FRAME_SIDE
                or width * height > MAX_FRAME_PIXELS
            ):
                raise VideoSourceError("video_dimensions_too_large")
            fps = (
                raw_fps
                if math.isfinite(raw_fps) and MIN_SOURCE_FPS <= raw_fps <= MAX_SOURCE_FPS
                else None
            )
            # A container may report no frame count at all; that is not an error, but a
            # reported count of zero from a readable file is an empty video.
            count: int | None = None
            if math.isfinite(raw_count) and raw_count > 0:
                count = int(raw_count)
            elif math.isfinite(raw_count) and raw_count == 0:
                raise VideoSourceError("video_has_no_frames")
            duration = (count / fps * 1000.0) if (count and fps) else None
        except VideoSourceError:
            capture.release()
            raise
        except Exception:
            capture.release()
            raise VideoSourceError("video_unreadable") from None
        self._capture = capture
        self._metadata = VideoMetadata(width, height, fps, count, duration)
        return self._metadata

    def _require_metadata(self) -> VideoMetadata:
        if self._metadata is None:  # pragma: no cover - open() always sets it
            raise VideoSourceError("video_not_opened")
        return self._metadata

    @property
    def metadata(self) -> VideoMetadata:
        return self._require_metadata()

    @property
    def frames_decoded(self) -> int:
        return self._frames_decoded

    @property
    def frames_failed(self) -> int:
        """Frames the decoder could not produce and which were skipped. Not the same as
        frames deliberately skipped by sampling, which were never decoded at all."""
        return self._frames_failed

    @property
    def timeline_trusted(self) -> bool:
        """False once the container's timeline was found unusable and frame-rate timestamps
        were substituted. Reported so a run's provenance is never ambiguous."""
        return self._timeline_trusted

    # ----------------------------------------------------------------------------- iteration
    def _timestamp(self, capture: Any, decoded_index: int) -> float:
        """The media timestamp of the frame just read, or a frame-rate substitute.

        ``decoded_index`` counts frames the decoder produced, including ones skipped by
        sampling, so the fallback stays on the source timeline rather than the sampled one.
        """
        import cv2

        metadata = self._require_metadata()
        fallback = (
            decoded_index / metadata.source_fps * 1000.0
            if metadata.source_fps
            else float(decoded_index)
        )
        if not self._timeline_trusted:
            return fallback
        try:
            position = float(capture.get(cv2.CAP_PROP_POS_MSEC))
        except Exception:
            self._timeline_trusted = False
            return fallback
        if not math.isfinite(position) or position < 0.0:
            self._timeline_trusted = False
            return fallback
        # A stuck timeline (every frame reporting the same instant) is worse than useless: it
        # would collapse the whole recording onto one moment. Detect it on the second frame
        # and fall back for the rest of the run rather than emitting a broken axis.
        if decoded_index > 0 and position == 0.0:
            self._timeline_trusted = False
            return fallback
        return position

    def frames(self) -> Iterator[VideoFrame]:
        """Yield frames in order. Exactly one decoded frame is live at a time."""
        if self._capture is None:
            self.open()
        capture = self._capture
        self._require_metadata()
        assert capture is not None
        decoded_index = 0
        emitted = 0
        # Failures are held pending rather than counted immediately. OpenCV reports the end of
        # the stream and an undecodable frame identically, so the two are told apart by what
        # happens next: if a later read succeeds, the run really did skip frames and they are
        # counted; if the retries simply run out, that was the end of the file and nothing was
        # dropped. Without this, every healthy video would report a phantom drop per retry.
        pending_failures = 0
        while True:
            ok, image = capture.read()
            if not ok or image is None:
                pending_failures += 1
                if pending_failures > MAX_CONSECUTIVE_DECODE_FAILURES:
                    break
                continue
            if pending_failures:
                self._frames_failed += pending_failures
                pending_failures = 0
            timestamp_ms = self._timestamp(capture, decoded_index)
            take = decoded_index % self._sample_every == 0
            decoded_index += 1
            self._frames_decoded += 1
            if not take:
                # Decoded but not processed: released immediately rather than held.
                del image
                continue
            if image.ndim != 3 or image.shape[2] != 3:
                self._frames_failed += 1
                del image
                continue
            height, width = int(image.shape[0]), int(image.shape[1])
            if width * height > MAX_FRAME_PIXELS:
                raise VideoSourceError("video_dimensions_too_large")
            yield VideoFrame(emitted, timestamp_ms, width, height, image)
            emitted += 1
        if emitted == 0:
            # Opened, but nothing usable came out of it.
            raise VideoSourceError("video_has_no_frames")

    # ------------------------------------------------------------------------------- closing
    def close(self) -> None:
        """Release the capture. Safe to call more than once and on a never-opened source."""
        capture, self._capture = self._capture, None
        if capture is not None:
            # Release must never mask the error that caused the unwind.
            with contextlib.suppress(Exception):
                capture.release()

    def __enter__(self) -> RecordedVideoSource:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
