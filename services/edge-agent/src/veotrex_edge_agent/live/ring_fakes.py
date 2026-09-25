"""Deterministic fakes for the Ring live path. TEST AND LOCAL QUALIFICATION ONLY.

Every value here is synthetic. There is no network call, no OAuth, no Ring endpoint, no
credential and no camera, which is the point: the adapter's lifecycle, backpressure, reconnect
and teardown behaviour are all properties of the code, and they should be provable on a laptop
with no Ring account while Amazon's IP-level block is still in place.

The scene is a moving block on noise - textured enough that a JPEG of it is unmistakably not a
blank canvas, and containing no person, because qualifying transport does not require one.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from veotrex_edge_agent.live.ring_media import (
    BoundedFrameSlot,
    DecodedFrame,
    RingMediaError,
    RingSessionMaterial,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

# Obviously synthetic, and obviously not a real Ring identifier shape.
FAKE_SESSION_ID = "synthetic-session-0001"
FAKE_RESOURCE_PATH = "/v1/devices/synthetic-device/media/streaming/whep/sessions/synthetic-0001"


def synthetic_scene(width: int, height: int, index: int) -> NDArray[np.uint8]:
    """A textured moving block. No person, no room, no recorded footage."""
    rng = np.random.default_rng(index)
    canvas = (rng.random((height, width, 3)) * 60 + 30).astype(np.uint8)
    left = 10 + (index * 7) % max(width - 60, 1)
    top = max(height // 4, 1)
    canvas[top : top + max(height // 2, 1), left : left + 50] = 220
    return canvas


class FakeRingSessionProvider:
    """Hands out synthetic session material and records that it was released.

    ``fail_with`` makes the negative matrix deterministic: a terminal category must end the
    source immediately, a transient one must be retried under the budget, and the test asserts
    which happened rather than how long it took.
    """

    def __init__(
        self,
        *,
        fail_with: str | None = None,
        fail_first: int = 0,
        material: RingSessionMaterial | None = None,
    ) -> None:
        self._fail_with = fail_with
        self._fail_first = fail_first
        self._material = material or RingSessionMaterial(
            session_id=FAKE_SESSION_ID,
            resource_path=FAKE_RESOURCE_PATH,
            codec="H264",
            hardware_decoder=False,
        )
        self.acquired = 0
        self.released: list[RingSessionMaterial] = []
        self.release_calls = 0

    def acquire(self) -> RingSessionMaterial:
        self.acquired += 1
        if self._fail_with and (self._fail_first == 0 or self.acquired <= self._fail_first):
            raise RingMediaError(self._fail_with)
        return self._material

    def release(self, material: RingSessionMaterial) -> None:
        self.release_calls += 1
        self.released.append(material)

    @property
    def outstanding(self) -> int:
        """Sessions acquired and not released. Must be zero once a source has closed."""
        failed = 0 if not self._fail_with else min(self.acquired, self._fail_first or self.acquired)
        return max(self.acquired - failed - self.release_calls, 0)


class FakeRingFrameReader:
    """Produces synthetic decoded frames through the same bounded slot the real reader uses."""

    def __init__(
        self,
        *,
        frame_count: int = 10,
        width: int = 320,
        height: int = 240,
        start_error: str | None = None,
        read_error_after: int | None = None,
        stall_after: int | None = None,
        discontinuity_at: int | None = None,
        pts_start_ms: float | None = 0.0,
        pts_step_ms: float = 100.0,
    ) -> None:
        self._frame_count = frame_count
        self._width = width
        self._height = height
        self._start_error = start_error
        self._read_error_after = read_error_after
        self._stall_after = stall_after
        self._discontinuity_at = discontinuity_at
        self._pts_start = pts_start_ms
        self._pts_step = pts_step_ms
        self._produced = 0
        self._eos = False
        self.slot = BoundedFrameSlot()
        self.started = 0
        self.closed = 0
        self.material: RingSessionMaterial | None = None
        self.open = False

    def start(self, material: RingSessionMaterial) -> None:
        if self._start_error:
            raise RingMediaError(self._start_error)
        self.started += 1
        self.material = material
        self.open = True
        self._produced = 0
        self._eos = False
        # Same contract as the real reader: a restarted session gets a fresh slot, because
        # close() closed the last one.
        self.slot = BoundedFrameSlot()

    def read(self, timeout_seconds: float) -> DecodedFrame | None:
        _ = timeout_seconds
        if not self.open:
            raise RingMediaError("DECODER_FAILED")
        if self._read_error_after is not None and self._produced >= self._read_error_after:
            raise RingMediaError("DECODER_FAILED")
        if self._stall_after is not None and self._produced >= self._stall_after:
            return None  # the stall the source must notice
        if self._produced >= self._frame_count:
            self._eos = True
            return None
        index = self._produced
        self._produced += 1
        pts = None if self._pts_start is None else self._pts_start + index * self._pts_step
        frame = DecodedFrame(
            image=synthetic_scene(self._width, self._height, index),
            width=self._width,
            height=self._height,
            arrival_monotonic_ns=time.monotonic_ns(),
            pts_ms=pts,
            discontinuity=index == self._discontinuity_at,
        )
        # Through the real slot, so backpressure behaviour is exercised rather than bypassed.
        self.slot.publish(frame)
        return self.slot.take(0.5)

    def close(self) -> None:
        self.closed += 1
        self.open = False
        self.slot.close()

    @property
    def eos(self) -> bool:
        return self._eos

    @property
    def stats(self) -> dict[str, Any]:
        return self.slot.stats.as_dict()


class CountingThreadReader(FakeRingFrameReader):
    """A reader that really starts a thread, so leak tests have something to leak."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, material: RingSessionMaterial) -> None:
        super().start(material)
        self._stop.clear()
        thread = threading.Thread(target=self._stop.wait, name="fake-ring-reader", daemon=True)
        self._thread = thread
        thread.start()

    def close(self) -> None:
        super().close()
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
