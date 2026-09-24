"""Bounded live preview: the processed frame, with the tracker's boxes drawn on it.

V1-DEMO-01 served geometry only and drew boxes on a black canvas. That was a deliberate
privacy choice, and for the client demonstration it is the wrong one: a monitoring product has
to show the room. This module adds the image back, under tight bounds.

**Exactly one frame exists at a time.** ``PreviewBuffer`` holds a single encoded JPEG and
replaces it wholesale; the previous bytes become unreachable and are collected. There is no
queue, no history, no ring, and nothing is ever written to disk.

**The preview cannot slow inference and cannot be slowed by a browser.** Encoding happens on
the pipeline thread, throttled to a target rate: at 640x480 a JPEG costs ~4 ms against a
detector that costs ~113 ms, so at 8 fps it is a few percent of one frame's budget. Crucially,
the encoder runs whether or not anyone is watching, and HTTP handlers only ever read the latest
slot - so a browser that stops reading, stalls, or opens ten tabs cannot apply backpressure to
capture or inference. Slow rendering degrades the preview rate and nothing else.

**A stale frame is not shown.** If the camera stops, the last good frame would otherwise sit on
screen looking live. ``latest`` refuses to return anything older than ``max_age_seconds``, so
the dashboard falls back to an explicit placeholder instead of a convincing lie.

The overlay draws the tracker's *current confirmed* boxes and a track number. It never draws a
name, an identity, an age, a classification or a score, because none of those exist here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veotrex_edge_agent.qualification.metrics import BoundedSamples

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

# Defaults chosen for the demo: smooth enough to read as live, cheap enough to be invisible
# against the detector's cost.
DEFAULT_PREVIEW_FPS = 8.0
DEFAULT_JPEG_QUALITY = 70
# The preview is a monitor view, not a recording; downscaling a 1080p camera here saves both
# encode time and bytes on the wire without changing what an operator can see.
DEFAULT_MAX_WIDTH = 960
# Beyond this the picture is no longer "live" and must not be presented as though it were.
DEFAULT_MAX_AGE_SECONDS = 2.0
ENCODE_SAMPLE_CAPACITY = 512

BOX_COLOURS = (
    (0, 200, 0),
    (255, 128, 0),
    (0, 160, 255),
    (200, 0, 200),
    (0, 220, 220),
    (255, 80, 80),
    (140, 255, 140),
    (80, 80, 255),
)


@dataclass(frozen=True, slots=True)
class PreviewConfig:
    target_fps: float = DEFAULT_PREVIEW_FPS
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    max_width: int = DEFAULT_MAX_WIDTH
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS

    def __post_init__(self) -> None:
        if not 0.5 <= self.target_fps <= 30.0:
            raise ValueError("preview target fps must be between 0.5 and 30")
        if not 30 <= self.jpeg_quality <= 95:
            raise ValueError("preview jpeg quality must be between 30 and 95")
        if not 160 <= self.max_width <= 1920:
            raise ValueError("preview max width must be between 160 and 1920")
        if not 0.2 <= self.max_age_seconds <= 30.0:
            raise ValueError("preview max age must be between 0.2 and 30 seconds")

    @property
    def minimum_interval_seconds(self) -> float:
        return 1.0 / self.target_fps


@dataclass(frozen=True, slots=True, repr=False)
class PreviewFrame:
    """One encoded JPEG and what it depicts. Held only in memory, only one at a time."""

    jpeg: bytes
    sequence: int
    frame_index: int
    published_monotonic: float
    width: int
    height: int

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # The bytes are image data; they must never land in a log line or a traceback.
        return (
            f"PreviewFrame(#{self.sequence}, frame={self.frame_index}, "
            f"{self.width}x{self.height}, {len(self.jpeg)} bytes)"
        )


class PreviewBuffer:
    """A single-slot holder for the most recent encoded preview frame."""

    def __init__(self, *, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> None:
        self._lock = threading.Lock()
        self._frame: PreviewFrame | None = None
        self._sequence = 0
        self._max_age = max_age_seconds

    def publish(self, jpeg: bytes, *, frame_index: int, width: int, height: int) -> PreviewFrame:
        with self._lock:
            self._sequence += 1
            # Replacing the slot is what bounds memory: the previous frame's bytes become
            # unreachable here and are freed. Nothing accumulates.
            frame = PreviewFrame(
                jpeg=jpeg,
                sequence=self._sequence,
                frame_index=frame_index,
                published_monotonic=time.monotonic(),
                width=width,
                height=height,
            )
            self._frame = frame
            return frame

    def latest(self) -> PreviewFrame | None:
        """The current frame, or None when there is none or it has gone stale."""
        with self._lock:
            frame = self._frame
            if frame is None:
                return None
            if time.monotonic() - frame.published_monotonic > self._max_age:
                # Old enough that showing it would misrepresent a dead feed as a live one.
                return None
            return frame

    def clear(self) -> None:
        """Drop the frame. Called when the source stops, so nothing outlives the session."""
        with self._lock:
            self._frame = None

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def has_frame(self) -> bool:
        return self.latest() is not None


class PreviewRenderer:
    """Draws the tracker's boxes onto a processed frame and encodes it, on a throttle.

    Called from the pipeline's existing per-frame hook, so it sees exactly the frame the
    detector and tracker just processed together with that frame's confirmed boxes. Geometry
    and image therefore cannot drift apart, and no versioning or client-side redraw is needed.
    """

    def __init__(self, config: PreviewConfig | None = None) -> None:
        self.config = config or PreviewConfig()
        self.buffer = PreviewBuffer(max_age_seconds=self.config.max_age_seconds)
        self.encode_ms = BoundedSamples(ENCODE_SAMPLE_CAPACITY)
        self.previews_encoded_total = 0
        self.previews_skipped_total = 0
        self.encode_failures_total = 0
        self._last_encode_monotonic = 0.0
        self._unavailable = False

    def _due(self, now: float) -> bool:
        return (now - self._last_encode_monotonic) >= self.config.minimum_interval_seconds

    def render(
        self,
        image: NDArray[np.uint8],
        boxes: Sequence[tuple[int, tuple[float, ...]]],
        *,
        frame_index: int,
        occupancy: int,
        source_health: str,
    ) -> PreviewFrame | None:
        """Encode this frame if one is due, otherwise skip it cheaply.

        Returns the published frame, or None when the frame was throttled away or could not be
        encoded. A failure here must never propagate: the demo losing its picture is bad, the
        demo losing its tracking is worse.
        """
        now = time.monotonic()
        if not self._due(now) or self._unavailable:
            self.previews_skipped_total += 1
            return None
        try:
            import cv2
        except ImportError:  # pragma: no cover - the group is installed wherever this runs
            self._unavailable = True
            return None

        started = time.perf_counter_ns()
        try:
            canvas = self._annotate(cv2, image, boxes, occupancy, source_health)
            ok, encoded = cv2.imencode(
                ".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), self.config.jpeg_quality]
            )
            if not ok:
                self.encode_failures_total += 1
                return None
            height, width = canvas.shape[:2]
            frame = self.buffer.publish(
                encoded.tobytes(), frame_index=frame_index, width=int(width), height=int(height)
            )
        except Exception:
            # Bounded and swallowed on purpose: tracking continues without a picture.
            self.encode_failures_total += 1
            return None
        self._last_encode_monotonic = now
        self.previews_encoded_total += 1
        self.encode_ms.add((time.perf_counter_ns() - started) / 1e6)
        return frame

    def _annotate(
        self,
        cv2: Any,
        image: NDArray[np.uint8],
        boxes: Sequence[tuple[int, tuple[float, ...]]],
        occupancy: int,
        source_health: str,
    ) -> Any:
        """Boxes and track numbers on a copy of the frame.

        A copy, not the original: the array handed to the hook is the one the detector saw,
        and drawing into it would corrupt what any other consumer of that frame observes.
        """
        source_height, source_width = image.shape[:2]
        scale = 1.0
        if source_width > self.config.max_width:
            scale = self.config.max_width / float(source_width)
            canvas = cv2.resize(
                image,
                (int(source_width * scale), int(source_height * scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            canvas = image.copy()

        for track_id, box in boxes:
            colour = BOX_COLOURS[int(track_id) % len(BOX_COLOURS)]
            x1, y1, x2, y2 = (float(value) * scale for value in box)
            cv2.rectangle(canvas, (int(x1), int(y1)), (int(x2), int(y2)), colour, 2)
            label = f"Track {int(track_id)}"
            # A filled strip behind the label so it stays readable over a bright scene.
            (text_width, text_height), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            top = max(int(y1) - text_height - 6, 0)
            cv2.rectangle(
                canvas,
                (int(x1), top),
                (int(x1) + text_width + 8, top + text_height + 6),
                colour,
                -1,
            )
            cv2.putText(
                canvas,
                label,
                (int(x1) + 4, top + text_height + 1),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (16, 16, 16),
                1,
                cv2.LINE_AA,
            )

        # Status strip: a head count and the feed's own state. No identity, no classification.
        status = f"people: {occupancy}   {source_health}"
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 24), (16, 18, 22), -1)
        cv2.putText(
            canvas, status, (8, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 226, 232), 1, cv2.LINE_AA
        )
        return canvas

    def clear(self) -> None:
        self.buffer.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "previews_encoded_total": self.previews_encoded_total,
            "previews_skipped_total": self.previews_skipped_total,
            "preview_encode_failures_total": self.encode_failures_total,
            "preview_target_fps": self.config.target_fps,
            "preview_encode_ms": {
                "count": float(self.encode_ms.count),
                "p50": self.encode_ms.percentile(50),
                "p95": self.encode_ms.percentile(95),
                "max": self.encode_ms.maximum,
            },
        }
