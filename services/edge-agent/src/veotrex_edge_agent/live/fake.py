"""A deterministic live source with no hardware behind it (V1-DEMO-01).

Every test in this package runs against this, and so does ``--source synthetic`` on the CLI.
That matters for two reasons: CI has no camera, and this Jetson currently has none either, so
without a synthetic source the live path would be unverifiable rather than merely unqualified.

It is declared ``SourceKind.SYNTHETIC_TEST``, so imagery that never came from a camera cannot
reach the dashboard claiming to be live. The dashboard reads that field and says so.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import numpy as np
from numpy.typing import NDArray

from veotrex_edge_agent.live.source import (
    LiveFrame,
    LiveSourceError,
    SourceDescription,
    SourceHealth,
    SourceKind,
)


class FakeLiveSource:
    """Emits a fixed number of frames at a chosen cadence.

    ``fail_after`` makes the source raise mid-iteration, which is how the tests prove that a
    capture failure still releases the device and still stops the pipeline cleanly.
    """

    kind = SourceKind.SYNTHETIC_TEST

    def __init__(
        self,
        *,
        frame_count: int = 10,
        width: int = 320,
        height: int = 240,
        fps: float = 30.0,
        source_id: str = "synthetic-0",
        fail_after: int | None = None,
        interval_seconds: float = 0.0,
        painter: object | None = None,
    ) -> None:
        self._frame_count = frame_count
        self._width = width
        self._height = height
        self._fps = fps
        self._source_id = source_id
        self._fail_after = fail_after
        self._interval = interval_seconds
        self._painter = painter
        self._health = SourceHealth.STARTING
        self.closed = False
        self.frames_emitted = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def health(self) -> SourceHealth:
        return self._health

    def describe(self) -> SourceDescription:
        return SourceDescription(self.kind, self._source_id, self._width, self._height, self._fps)

    def _image(self, index: int) -> NDArray[np.uint8]:
        canvas = np.zeros((self._height, self._width, 3), dtype=np.uint8)
        painter = self._painter
        if callable(painter):
            painter(canvas, index)
        return canvas

    def frames(self) -> Iterator[LiveFrame]:
        self._health = SourceHealth.RUNNING
        try:
            for index in range(self._frame_count):
                if self.closed:
                    break
                if self._fail_after is not None and index >= self._fail_after:
                    self._health = SourceHealth.FAILED
                    raise LiveSourceError("camera_unavailable")
                if self._interval:
                    time.sleep(self._interval)
                now = time.monotonic_ns()
                self.frames_emitted += 1
                yield LiveFrame(
                    kind=self.kind,
                    source_id=self._source_id,
                    frame_index=index,
                    # Nominal cadence rather than real elapsed time, so a test's expected
                    # timestamps do not depend on how busy the machine was.
                    timestamp_ms=index / self._fps * 1000.0,
                    monotonic_ns=now,
                    width=self._width,
                    height=self._height,
                    image=self._image(index),
                    capture_timestamp_ms=None,
                    discontinuity=False,
                )
        finally:
            if self._health is SourceHealth.RUNNING:
                self._health = SourceHealth.STOPPED

    def close(self) -> None:
        self.closed = True
        if self._health is not SourceHealth.FAILED:
            self._health = SourceHealth.STOPPED
