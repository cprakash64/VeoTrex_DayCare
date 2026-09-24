"""Provider-neutral live frame contract (V1-DEMO-01).

Everything downstream - detection, tracking, occupancy, the dashboard - consumes ``LiveFrame``
and never learns which source produced it. That is the whole point: a local USB camera today
and Ring WHEP once Amazon clears the upstream block must be interchangeable without touching a
line of detection, tracking or business logic.

One thing deliberately *does* travel with every frame: ``SourceKind``. Recorded imagery must
never be able to present itself as a live camera further up the stack, so provenance is carried
explicitly rather than inferred from which object happened to construct the frame.

**Time.** Two clocks, for two different jobs, and conflating them is the bug this contract
exists to prevent:

``monotonic_ns`` / ``timestamp_ms``
    Arrival on a monotonic clock, measured from the start of the session. This is what the
    tracker uses for temporal continuity. It cannot jump backwards when NTP steps the system
    clock, and it keeps increasing across a camera reconnect.

``capture_timestamp_ms``
    What the device or container said, when it says anything at all. Advisory, frequently
    absent on UVC, and never used for continuity.

A recorded file has a trustworthy media timeline and no meaningful arrival time; a live camera
is the reverse. Both fit here, and the pipeline reads ``timestamp_ms`` in both cases.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

# Resource bounds shared by every source. A frame larger than this is refused rather than
# decoded: this runs beside a TensorRT worker on a Jetson with ~8 GB shared between CPU and GPU.
MAX_FRAME_PIXELS = 8_294_400  # 3840x2160
MAX_FRAME_SIDE = 4_096
MIN_FRAME_SIDE = 16


class SourceKind(StrEnum):
    """What the imagery actually is. Never inferred, never defaulted.

    ``LIVE_RING_WHEP`` is declared now and produced by nothing: it is the slot the Ring source
    will occupy, and having it here means adding Ring changes no consumer.
    """

    LIVE_LOCAL_CAMERA = "LIVE_LOCAL_CAMERA"
    LIVE_RING_WHEP = "LIVE_RING_WHEP"
    SYNTHETIC_TEST = "SYNTHETIC_TEST"

    @property
    def is_live(self) -> bool:
        return self in {SourceKind.LIVE_LOCAL_CAMERA, SourceKind.LIVE_RING_WHEP}


class SourceHealth(StrEnum):
    """What the dashboard shows about the feed itself, distinct from what is in the picture."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class LiveSourceError(Exception):
    """A bounded category. Never a device path, never a driver message, never pixels."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class LiveFrame:
    """One decoded frame and its provenance.

    ``image`` is a BGR uint8 array owned by the consumer for one iteration only; a source must
    not retain it, and a consumer must not assume it survives the next iteration.

    ``frame_index``, ``timestamp_ms``, ``width``, ``height`` and ``image`` are exactly the
    attributes the B1A tracking pipeline reads, so a ``LiveFrame`` feeds it unchanged.
    """

    kind: SourceKind
    source_id: str
    frame_index: int
    # Monotonic milliseconds since the session began. Strictly increasing, reconnect-safe.
    timestamp_ms: float
    monotonic_ns: int
    width: int
    height: int
    image: NDArray[np.uint8]
    # What the device claimed, when it claimed anything. Advisory only.
    capture_timestamp_ms: float | None = None
    # True when the feed was interrupted before this frame - a reconnect, or a gap long enough
    # that motion between the previous frame and this one cannot be assumed continuous.
    discontinuity: bool = False

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Pixels must never reach a log line or a traceback.
        return (
            f"LiveFrame({self.kind}, {self.source_id}, #{self.frame_index}, "
            f"{self.timestamp_ms:.1f}ms, {self.width}x{self.height}"
            f"{', DISCONTINUITY' if self.discontinuity else ''})"
        )


@dataclass(frozen=True, slots=True)
class SourceDescription:
    """Safe metadata about a source. Everything here is printable and shareable."""

    kind: SourceKind
    source_id: str
    width: int
    height: int
    nominal_fps: float | None


class LiveVideoSource(Protocol):
    """A source of live frames.

    Implementations own whatever device, socket or process backs them, and must release it in
    ``close`` on every path - including an exception raised mid-iteration by the consumer.
    ``frames`` yields until the source stops or ``close`` is called; it must never block
    forever on a dead device.
    """

    @property
    def kind(self) -> SourceKind: ...

    @property
    def source_id(self) -> str: ...

    @property
    def health(self) -> SourceHealth: ...

    def describe(self) -> SourceDescription: ...

    def frames(self) -> Iterator[LiveFrame]: ...

    def close(self) -> None: ...


def validate_geometry(width: int, height: int) -> None:
    """Shared bounds check, applied by every source before a frame is published."""
    if width < MIN_FRAME_SIDE or height < MIN_FRAME_SIDE:
        raise LiveSourceError("frame_dimensions_too_small")
    if width > MAX_FRAME_SIDE or height > MAX_FRAME_SIDE or width * height > MAX_FRAME_PIXELS:
        raise LiveSourceError("frame_dimensions_too_large")
