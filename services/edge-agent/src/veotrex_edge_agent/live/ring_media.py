"""The boundary between a Ring WHEP media session and the live pipeline (V1-DEMO-02).

Three things are kept apart on purpose, because collapsing them is how a live-video path ends up
holding a credential it does not need:

    A. account and device authorization   already exists, outside this module, untouched here
    B. WHEP media-session acquisition     ``RingLiveSessionProvider``
    C. decoded frame delivery             ``RingFrameReader``

``RingWhepSource`` consumes B and C and never sees A. The provider hands back
``RingSessionMaterial``, which deliberately carries **no token**: an access token lives in the
existing credential boundary (``AccessTokenProvider`` and ``RingWhepSessionProvider`` in
``camera_transport``), the provider uses it to create and to delete the session, and the source
only ever holds an opaque session id and the resource path it must ask the provider to release.
So a source object captured in a traceback, a log line or a metric cannot leak a credential,
because it never had one.

Nothing here imports GStreamer, WebRTC or HTTP. That is what makes the whole adapter testable
with no Ring account, no network and no camera: the real reader and a fake reader satisfy the
same protocol, and every test below the transport uses the fake.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

# A reader that has produced nothing for this long has stalled, whatever the socket says.
DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS = 20.0
DEFAULT_STALL_TIMEOUT_SECONDS = 10.0
# The decoded slot holds exactly one frame. See BoundedFrameSlot for why it is not a queue.
FRAME_SLOT_CAPACITY = 1


class RingMediaError(RuntimeError):
    """A bounded category, never a URL, a token, an SDP body or a driver message."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class RingSessionMaterial:
    """What a WHEP session is, to something that only wants frames from it.

    An opaque id and the resource the provider must delete. No bearer token, no SDP, no
    endpoint with a query string in it - the repr below exists so that none of this can reach a
    log line by accident even as these fields grow.
    """

    session_id: str
    resource_path: str
    # Ring does not document a WHEP session lifetime, so there is no expiry field to populate.
    # Inventing one would mean scheduling a renewal on a deadline nobody has published.
    codec: str | None = None
    hardware_decoder: bool | None = None

    def __post_init__(self) -> None:
        if not self.session_id or not self.resource_path:
            raise RingMediaError("invalid_session_material")

    def __repr__(self) -> str:
        # Length, not content: enough to debug a truncation, never enough to replay a session.
        return (
            f"RingSessionMaterial(session_id=<{len(self.session_id)} chars>, "
            f"resource=<{len(self.resource_path)} chars>, codec={self.codec})"
        )

    def as_dict(self) -> dict[str, Any]:
        """Safe to log and to put in a metric. The identifiers themselves are not included."""
        return {"codec": self.codec, "hardware_decoder": self.hardware_decoder}


@dataclass(frozen=True, slots=True, repr=False)
class DecodedFrame:
    """One decoded picture as it leaves the media subsystem, before it becomes a LiveFrame.

    ``pts_ms`` is the media timestamp where the decoder supplied one and ``None`` where it did
    not. None is not zero: a frame with an unknown presentation time must not be presented as
    one that arrived at the start of the stream.
    """

    image: NDArray[np.uint8]
    width: int
    height: int
    arrival_monotonic_ns: int
    pts_ms: float | None = None
    discontinuity: bool = False

    def __repr__(self) -> str:
        # The array is picture data and must never be rendered into a traceback.
        return (
            f"DecodedFrame({self.width}x{self.height}, pts={self.pts_ms}, "
            f"discontinuity={self.discontinuity})"
        )


@runtime_checkable
class RingLiveSessionProvider(Protocol):
    """Acquires and releases one WHEP media session. Owns the credential; the source does not."""

    def acquire(self) -> RingSessionMaterial:
        """Negotiate a session, or raise ``RingMediaError`` with a bounded category."""
        ...

    def release(self, material: RingSessionMaterial) -> None:
        """Tear the session down. Must be safe to call twice and must not raise on a dead one."""
        ...


@runtime_checkable
class RingFrameReader(Protocol):
    """Turns an acquired session into decoded frames. The only part that touches media."""

    def start(self, material: RingSessionMaterial) -> None: ...

    def read(self, timeout_seconds: float) -> DecodedFrame | None:
        """The newest decoded frame, or None if none arrived within the timeout."""
        ...

    def close(self) -> None:
        """Release decoder, sink, threads and buffers. Safe on a reader that never started."""
        ...

    @property
    def eos(self) -> bool:
        """True once the stream ended cleanly.

        This is what separates "the feed is over" from "the feed has gone quiet". Both look
        like ``read`` returning None, and treating them alike would either retry a finished
        stream forever or accept a stalled one as a normal ending.
        """
        ...

    @property
    def stats(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class FrameSlotStats:
    received_total: int = 0
    dropped_total: int = 0
    delivered_total: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "frames_received_total": self.received_total,
            "frames_dropped_total": self.dropped_total,
            "frames_delivered_total": self.delivered_total,
        }


class BoundedFrameSlot:
    """A single-slot newest-frame-wins handoff between the media thread and the pipeline.

    The same choice V1-DEMO-01 made for the USB camera, for the same reason: a network camera
    delivers faster than the detector consumes, and those two rates cannot both be honoured. A
    queue would accumulate and show the room as it was a minute ago; for safety monitoring a
    current view with gaps beats a complete view that is behind. An overwritten frame is counted,
    because a dropped frame you can see in a metric is very different from latency you cannot.

    Bounded by construction rather than by policy: there is one slot, so there is nothing to
    grow. The media side never blocks on the consumer, which is what stops a slow detector from
    applying backpressure all the way to the network socket.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: DecodedFrame | None = None
        self._arrived = threading.Event()
        self._closed = False
        self.stats = FrameSlotStats()

    def publish(self, frame: DecodedFrame) -> None:
        with self._lock:
            if self._closed:
                return
            self.stats.received_total += 1
            if self._frame is not None:
                # The waiting frame was never consumed. Replacing it is the policy; counting it
                # is how an operator learns the detector is behind the feed.
                self.stats.dropped_total += 1
            self._frame = frame
            self._arrived.set()

    def take(self, timeout_seconds: float) -> DecodedFrame | None:
        """The current frame, removed from the slot, or None if none arrived in time."""
        if not self._arrived.wait(timeout_seconds):
            return None
        with self._lock:
            frame, self._frame = self._frame, None
            self._arrived.clear()
            if frame is not None:
                self.stats.delivered_total += 1
            return frame

    def close(self) -> None:
        """Drop whatever is held and wake anyone waiting. No frame outlives the session."""
        with self._lock:
            self._closed = True
            self._frame = None
        self._arrived.set()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def depth(self) -> int:
        with self._lock:
            return 0 if self._frame is None else 1


@dataclass(slots=True)
class RingMediaMetrics:
    """Counters the dashboard and the logs may see. No identifier from the session appears."""

    sessions_started_total: int = 0
    sessions_completed_total: int = 0
    session_failures_total: int = 0
    reconnect_count: int = 0
    frames_received_total: int = 0
    frames_dropped_total: int = 0
    decode_latency_ms: list[float] = field(default_factory=list)
    frame_age_ms: list[float] = field(default_factory=list)
    stream_state: str = "STOPPED"
    last_failure_category: str | None = None

    def observe_decode(self, milliseconds: float) -> None:
        self._add(self.decode_latency_ms, milliseconds)

    def observe_age(self, milliseconds: float) -> None:
        self._add(self.frame_age_ms, milliseconds)

    @staticmethod
    def _add(target: list[float], value: float, capacity: int = 512) -> None:
        # Bounded on purpose: an unbounded sample list is an unbounded memory leak on a feed
        # that runs for a day.
        target.append(value)
        if len(target) > capacity:
            del target[: len(target) - capacity]

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float | None:
        if not samples:
            return None
        ordered = sorted(samples)
        index = min(len(ordered) - 1, int(round((percentile / 100.0) * (len(ordered) - 1))))
        return round(ordered[index], 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ring_whep_sessions_started_total": self.sessions_started_total,
            "ring_whep_sessions_completed_total": self.sessions_completed_total,
            "ring_whep_session_failures_total": self.session_failures_total,
            "ring_whep_reconnect_count": self.reconnect_count,
            "ring_frames_received_total": self.frames_received_total,
            "ring_frames_dropped_total": self.frames_dropped_total,
            "ring_decode_latency_ms": {
                "p50": self._percentile(self.decode_latency_ms, 50),
                "p95": self._percentile(self.decode_latency_ms, 95),
            },
            "ring_frame_age_ms": {
                "p50": self._percentile(self.frame_age_ms, 50),
                "p95": self._percentile(self.frame_age_ms, 95),
            },
            "ring_stream_state": self.stream_state,
            # A bounded category such as WHEP_HTTP_UNAUTHORIZED. Never a message, URL or body.
            "ring_last_failure_category": self.last_failure_category,
        }
