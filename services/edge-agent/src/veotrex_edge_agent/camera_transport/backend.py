from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from veotrex_edge_agent.camera_transport.descriptor import LiveSessionLease, VideoCodec
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory
from veotrex_edge_agent.camera_transport.timing import CompressedSample, DecodedSample


@dataclass(frozen=True, slots=True)
class TransportConnected:
    """The provider answered DESCRIBE/SDP. This is not media flow."""

    generation: int
    at: float


@dataclass(frozen=True, slots=True)
class MediaNegotiated:
    generation: int
    at: float
    codec: VideoCodec
    decoder: str | None
    hardware_decoder: bool


@dataclass(frozen=True, slots=True)
class DecodedCaps:
    generation: int
    at: float
    width: int | None
    height: int | None
    framerate: float | None
    nvmm: bool


@dataclass(frozen=True, slots=True)
class MediaBatch:
    generation: int
    compressed: tuple[CompressedSample, ...]
    decoded: tuple[DecodedSample, ...]


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Worker liveness. Proves the process is alive, never that media is flowing."""

    generation: int
    at: float


@dataclass(frozen=True, slots=True)
class BackendFailed:
    generation: int
    at: float
    category: TransportErrorCategory


@dataclass(frozen=True, slots=True)
class EndOfStream:
    generation: int
    at: float


@dataclass(frozen=True, slots=True)
class BackendExited:
    generation: int
    at: float


BackendEvent = (
    TransportConnected
    | MediaNegotiated
    | DecodedCaps
    | MediaBatch
    | Heartbeat
    | BackendFailed
    | EndOfStream
    | BackendExited
)


class MediaBackendHandle(Protocol):
    """One media session (one generation). ``stop`` must release every OS resource."""

    @property
    def pid(self) -> int | None: ...

    def start(self, lease: LiveSessionLease, emit: Callable[[BackendEvent], None]) -> None: ...

    def stop(self) -> None: ...


BackendFactory = Callable[[int], MediaBackendHandle]
