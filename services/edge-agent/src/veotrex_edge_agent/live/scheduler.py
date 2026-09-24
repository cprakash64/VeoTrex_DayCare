"""Bounded-latency scheduling between capture and inference (V1-DEMO-01).

A camera delivers ~30 fps. The YOLOX-S TensorRT detector measured ~6-7 fps end to end in B1A.
Those two rates cannot both be honoured, and the choice of which to sacrifice *is* the design:

**Newest-frame-wins.** Capture runs in its own thread and publishes into a single-slot buffer.
When inference is busy, an arriving frame overwrites the one waiting and the overwritten frame
is counted as dropped. The detector therefore always works on the most recent view of the room,
and never on a backlog.

The alternative - a FIFO queue that buffers every frame - is worse for this product in a way
that gets worse over time. At 30 fps in and 7 fps out, a queue accumulates ~23 frames a second;
after a minute the dashboard would be showing the room as it was a minute ago. **For safety
monitoring, a current view with gaps beats a complete view that is minutes behind**, and a
dropped frame is visible in a counter while accumulated latency is invisible until someone
notices the demo is wrong.

Memory follows from the same choice: at most one frame waits, one is being processed, and one
is being captured. Nothing grows with session length, so a camera left running all afternoon
costs what it costs in the first second.

``capacity`` is configurable above 1 for a future source whose frames are individually
expensive to reacquire, but the default is 1 and the demo uses the default.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from veotrex_edge_agent.live.source import LiveFrame, LiveSourceError, SourceHealth

if TYPE_CHECKING:  # pragma: no cover - typing only
    from veotrex_edge_agent.live.source import LiveVideoSource

# How long a consumer waits for a frame before checking whether it should still be running.
# Short enough that shutdown is prompt, long enough not to spin a core.
POLL_INTERVAL_SECONDS = 0.05
# How long ``stop`` waits for the capture thread to notice and unwind.
JOIN_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class SchedulerMetrics:
    frames_captured_total: int = 0
    frames_delivered_total: int = 0
    frames_dropped_total: int = 0
    capture_seconds: float = 0.0

    @property
    def capture_fps(self) -> float:
        if self.capture_seconds <= 0:
            return 0.0
        return self.frames_captured_total / self.capture_seconds

    def snapshot(self) -> dict[str, float | int]:
        return {
            "frames_captured_total": self.frames_captured_total,
            "frames_delivered_total": self.frames_delivered_total,
            "frames_dropped_total": self.frames_dropped_total,
            "camera_capture_fps": round(self.capture_fps, 3),
        }


class BackpressureScheduler:
    """Runs a source on a capture thread and hands the newest frame to the consumer.

    Used as a context manager so the thread and the device are released on every path:

        with BackpressureScheduler(source) as scheduler:
            for frame in scheduler.frames():
                ...
    """

    def __init__(self, source: LiveVideoSource, *, capacity: int = 1) -> None:
        if capacity < 1:
            raise LiveSourceError("invalid_scheduler_capacity")
        self._source = source
        self._buffer: deque[LiveFrame] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None
        self.metrics = SchedulerMetrics()
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------------ status
    @property
    def failure(self) -> str | None:
        """The bounded category that stopped capture, or None."""
        return self._failure

    @property
    def health(self) -> SourceHealth:
        return self._source.health

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._capture_loop, name="veotrex-live-capture", daemon=True
        )
        self._thread = thread
        thread.start()

    def _capture_loop(self) -> None:
        started = time.monotonic()
        try:
            for frame in self._source.frames():
                if self._stop.is_set():
                    break
                with self._lock:
                    # maxlen makes this the drop: appending to a full deque discards the
                    # oldest, which at capacity 1 is the frame inference never got to.
                    if len(self._buffer) == self._buffer.maxlen:
                        self.metrics.frames_dropped_total += 1
                    self._buffer.append(frame)
                    self.metrics.frames_captured_total += 1
                self._arrived.set()
        except LiveSourceError as exc:
            self._failure = exc.category
            self._logger.warning("live_capture_failed", category=exc.category)
        except Exception:
            # A driver can raise anything; the category stays bounded either way.
            self._failure = "capture_error"
            self._logger.warning("live_capture_failed", category="capture_error")
        finally:
            self.metrics.capture_seconds = time.monotonic() - started
            self._stop.set()
            # Wake a consumer blocked waiting for a frame that will never arrive.
            self._arrived.set()

    # ---------------------------------------------------------------------------- consumer
    def frames(self, *, max_frames: int | None = None) -> Iterator[LiveFrame]:
        """Yield the newest available frame, oldest-first within the buffer.

        Ends when capture stops and the buffer drains, or after ``max_frames``.
        """
        self.start()
        delivered = 0
        while True:
            if max_frames is not None and delivered >= max_frames:
                return
            frame: LiveFrame | None = None
            with self._lock:
                if self._buffer:
                    frame = self._buffer.popleft()
            if frame is not None:
                delivered += 1
                self.metrics.frames_delivered_total += 1
                yield frame
                continue
            if self._stop.is_set():
                # Capture has finished and the buffer is empty.
                return
            self._arrived.clear()
            self._arrived.wait(POLL_INTERVAL_SECONDS)

    # ----------------------------------------------------------------------------- closing
    def stop(self) -> None:
        """Stop capture and release the source. Idempotent, and safe before ``start``."""
        self._stop.set()
        self._arrived.set()
        # Closing the source is what actually unblocks a capture thread sitting in a blocking
        # device read; the stop flag alone would only be noticed between frames.
        try:
            self._source.close()
        except Exception:  # pragma: no cover - close must not mask the caller's error
            self._logger.warning("live_source_close_failed")
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():  # pragma: no cover - a wedged driver
                self._logger.warning("live_capture_thread_did_not_exit")
        with self._lock:
            self._buffer.clear()

    def __enter__(self) -> BackpressureScheduler:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
