"""Local UVC/USB camera source for the Jetson (V1-DEMO-01).

Capture goes through OpenCV's V4L2 backend, which this build has (``v4l/v4l2: YES``); the
GStreamer backend is compiled out of the PyPI wheel, so a ``gst-launch`` pipeline string handed
to ``VideoCapture`` would silently fail to open. That was verified on this Jetson rather than
assumed - picking the wrong backend here fails at demo time, not at import time.

Only local V4L2 device nodes are opened. There is no URL, no network camera and no device
string taken from a request in this stage: ``open_camera`` accepts an index or a ``/dev/videoN``
path and nothing else.

Reconnect is bounded by the repository's existing ``ReconnectPolicy``/``ReconnectBudget`` (the
camera-transport circuit breaker), so a camera that has been unplugged produces a bounded
number of attempts and then a clean stop rather than an infinite retry loop behind a demo.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from veotrex_edge_agent.camera_transport.reconnect import ReconnectBudget, ReconnectPolicy
from veotrex_edge_agent.live.source import (
    LiveFrame,
    LiveSourceError,
    SourceDescription,
    SourceHealth,
    SourceKind,
    validate_geometry,
)

DEVICE_PATTERN = re.compile(r"^/dev/video(\d+)$")
DEVICE_ROOT = Path("/dev")
# A frame this old when it reaches the consumer is not worth detecting on for a live demo.
DEFAULT_REQUESTED_WIDTH = 1280
DEFAULT_REQUESTED_HEIGHT = 720
DEFAULT_REQUESTED_FPS = 30.0
# Consecutive empty reads tolerated before the source treats the device as gone.
MAX_CONSECUTIVE_READ_FAILURES = 30
# A gap longer than this marks the next frame discontinuous, so the tracker is told that
# motion across it cannot be assumed continuous.
DISCONTINUITY_GAP_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class CameraCandidate:
    """Safe, shareable metadata about one device node. No frame is retained to produce it."""

    index: int
    device: str
    width: int | None
    height: int | None
    fps: float | None
    usable: bool
    detail: str


def _device_index(device: str | int) -> int:
    if isinstance(device, int):
        if device < 0:
            raise LiveSourceError("invalid_camera_index")
        return device
    matched = DEVICE_PATTERN.match(str(device))
    if matched is None:
        # Deliberately narrow: no URL, no arbitrary GStreamer string, no file path.
        raise LiveSourceError("invalid_camera_device")
    return int(matched.group(1))


def list_device_nodes() -> list[int]:
    """V4L2 device indices present on this machine, ascending.

    Every even node is not necessarily a capture device - many UVC cameras expose a metadata
    node alongside the video node - so this lists candidates and ``probe_camera`` decides which
    can actually deliver frames.
    """
    indices: list[int] = []
    try:
        entries = sorted(DEVICE_ROOT.glob("video*"))
    except OSError:  # pragma: no cover - /dev is always readable in practice
        return []
    for entry in entries:
        matched = DEVICE_PATTERN.match(str(entry))
        if matched is not None:
            indices.append(int(matched.group(1)))
    return indices


def probe_camera(index: int, *, timeout_seconds: float = 3.0) -> CameraCandidate:
    """Report what a device node can do, without keeping any imagery.

    The capture is opened, its declared geometry is read, and it is released immediately. No
    frame is stored, written or returned - discovery must be safe to run in front of a client.
    """
    device = f"/dev/video{index}"
    try:
        import cv2
    except ImportError:
        return CameraCandidate(index, device, None, None, None, False, "opencv_unavailable")

    capture: Any | None = None
    started = time.monotonic()
    try:
        capture = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not capture.isOpened():
            return CameraCandidate(index, device, None, None, None, False, "cannot_open")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        raw_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        fps = raw_fps if 0.0 < raw_fps < 1000.0 else None
        if width <= 0 or height <= 0:
            return CameraCandidate(index, device, None, None, fps, False, "no_geometry")
        if time.monotonic() - started > timeout_seconds:  # pragma: no cover - slow device
            return CameraCandidate(index, device, width, height, fps, False, "probe_timeout")
        return CameraCandidate(index, device, width, height, fps, True, "ok")
    except Exception:
        return CameraCandidate(index, device, None, None, None, False, "probe_failed")
    finally:
        if capture is not None:
            with contextlib.suppress(Exception):
                capture.release()


def discover_cameras() -> list[CameraCandidate]:
    """Every V4L2 node on this machine with what it reports it can do."""
    return [probe_camera(index) for index in list_device_nodes()]


class LocalCameraSource:
    """A ``LiveVideoSource`` over one local V4L2 capture device.

    The device is not opened at construction: ``frames`` opens it, so a source can be built,
    inspected and closed on a machine with no camera - which is how the tests run and how the
    demo behaves when the camera is unplugged before it starts.
    """

    kind = SourceKind.LIVE_LOCAL_CAMERA

    def __init__(
        self,
        device: str | int = 0,
        *,
        width: int = DEFAULT_REQUESTED_WIDTH,
        height: int = DEFAULT_REQUESTED_HEIGHT,
        fps: float = DEFAULT_REQUESTED_FPS,
        reconnect_policy: ReconnectPolicy | None = None,
    ) -> None:
        self._index = _device_index(device)
        self._requested = (width, height, fps)
        self._capture: Any | None = None
        self._health = SourceHealth.STARTING
        self._stopping = False
        self._frames_captured = 0
        self._reconnects = 0
        self._geometry: tuple[int, int] = (0, 0)
        self._nominal_fps: float | None = None
        self._policy = reconnect_policy or ReconnectPolicy()
        self._budget = ReconnectBudget(self._policy)
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------- description
    @property
    def source_id(self) -> str:
        # Index rather than a full device path: this string reaches the dashboard and logs.
        return f"local-camera-{self._index}"

    @property
    def health(self) -> SourceHealth:
        return self._health

    @property
    def frames_captured(self) -> int:
        return self._frames_captured

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    def describe(self) -> SourceDescription:
        width, height = self._geometry
        return SourceDescription(self.kind, self.source_id, width, height, self._nominal_fps)

    # ------------------------------------------------------------------------------ device
    def _open(self) -> Any:
        try:
            import cv2
        except ImportError:
            raise LiveSourceError("video_backend_unavailable") from None
        width, height, fps = self._requested
        capture = cv2.VideoCapture(self._index, cv2.CAP_V4L2)
        if not capture.isOpened():
            with contextlib.suppress(Exception):
                capture.release()
            raise LiveSourceError("camera_unavailable")
        # Requests, not guarantees: a UVC camera may substitute its nearest supported mode,
        # so the accepted geometry is read back rather than assumed.
        with contextlib.suppress(Exception):
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            capture.set(cv2.CAP_PROP_FPS, fps)
            # A one-frame driver buffer keeps latency bounded at the source: without it the
            # driver hands back progressively staler frames once the consumer falls behind.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        accepted_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        accepted_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        raw_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        try:
            validate_geometry(accepted_width, accepted_height)
        except LiveSourceError:
            with contextlib.suppress(Exception):
                capture.release()
            raise
        self._geometry = (accepted_width, accepted_height)
        self._nominal_fps = raw_fps if 0.0 < raw_fps < 1000.0 else None
        return capture

    def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            with contextlib.suppress(Exception):
                capture.release()

    # --------------------------------------------------------------------------- iteration
    def frames(self) -> Iterator[LiveFrame]:
        """Yield frames until the device stops or ``close`` is called.

        A read failure is retried within the bounded budget; exhausting it ends the iterator
        rather than raising, so a demo whose camera is unplugged stops cleanly and the
        dashboard shows FAILED instead of a traceback.
        """
        session_start = time.monotonic_ns()
        index = 0
        previous_monotonic: int | None = None
        pending_discontinuity = False
        self._stopping = False
        try:
            self._capture = self._open()
        except LiveSourceError:
            self._health = SourceHealth.FAILED
            raise
        self._health = SourceHealth.RUNNING
        failures = 0
        while not self._stopping:
            capture = self._capture
            if capture is None:  # pragma: no cover - close() raced the loop
                break
            ok, image = capture.read()
            if not ok or image is None:
                failures += 1
                if failures <= MAX_CONSECUTIVE_READ_FAILURES:
                    continue
                if not self._reconnect():
                    break
                failures = 0
                pending_discontinuity = True
                continue
            failures = 0
            if image.ndim != 3 or image.shape[2] != 3:
                continue
            height, width = int(image.shape[0]), int(image.shape[1])
            try:
                validate_geometry(width, height)
            except LiveSourceError:
                self._health = SourceHealth.FAILED
                break
            now = time.monotonic_ns()
            if (
                previous_monotonic is not None
                and (now - previous_monotonic) > DISCONTINUITY_GAP_SECONDS * 1e9
            ):
                pending_discontinuity = True
            frame = LiveFrame(
                kind=self.kind,
                source_id=self.source_id,
                frame_index=index,
                timestamp_ms=(now - session_start) / 1e6,
                monotonic_ns=now,
                width=width,
                height=height,
                image=image,
                capture_timestamp_ms=self._capture_timestamp(capture),
                discontinuity=pending_discontinuity,
            )
            pending_discontinuity = False
            previous_monotonic = now
            index += 1
            self._frames_captured += 1
            yield frame
        if self._health is SourceHealth.RUNNING:
            self._health = SourceHealth.STOPPED

    @staticmethod
    def _capture_timestamp(capture: Any) -> float | None:
        """What the driver says, if anything. Advisory; never used for continuity."""
        try:
            import cv2

            value = float(capture.get(cv2.CAP_PROP_POS_MSEC))
        except Exception:
            return None
        return value if value > 0.0 else None

    def _reconnect(self) -> bool:
        """Re-open the device within the bounded budget. False means give up."""
        delay = self._budget.next_delay(time.monotonic())
        if delay is None:
            self._health = SourceHealth.FAILED
            self._logger.warning("live_camera_reconnect_budget_exhausted", source=self.source_id)
            return False
        self._health = SourceHealth.RECONNECTING
        self._release()
        if delay > 0:
            time.sleep(min(delay, self._policy.maximum_delay_seconds))
        try:
            self._capture = self._open()
        except LiveSourceError:
            return True if self._budget.next_delay(time.monotonic()) is not None else False
        self._reconnects += 1
        self._health = SourceHealth.RUNNING
        self._logger.info("live_camera_reconnected", source=self.source_id, count=self._reconnects)
        return True

    # ----------------------------------------------------------------------------- closing
    def close(self) -> None:
        """Stop iteration and release the device. Idempotent and safe before ``frames``."""
        self._stopping = True
        self._release()
        if self._health is not SourceHealth.FAILED:
            self._health = SourceHealth.STOPPED

    def __enter__(self) -> LocalCameraSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
