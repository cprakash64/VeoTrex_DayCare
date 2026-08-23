from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class StreamHealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class CameraCapability(StrEnum):
    LIVE_VIDEO = "LIVE_VIDEO"
    RECEIVE_AUDIO = "RECEIVE_AUDIO"
    SEND_AUDIO = "SEND_AUDIO"
    SNAPSHOT = "SNAPSHOT"
    HISTORICAL_CLIP = "HISTORICAL_CLIP"
    MOTION_EVENTS = "MOTION_EVENTS"


@dataclass(frozen=True)
class CapabilityLimit:
    capability: CameraCapability
    name: str
    value: str | int | bool


@dataclass(frozen=True)
class CameraCapabilities:
    supported: frozenset[CameraCapability]
    limitations: tuple[CapabilityLimit, ...] = ()

    def supports(self, capability: CameraCapability) -> bool:
        return capability in self.supported


@dataclass(frozen=True)
class DiscoveredCamera:
    opaque_device_id: str
    display_name: str
    capabilities: CameraCapabilities
    manufacturer: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class StreamRequest:
    opaque_device_id: str
    preferred_transport: str | None = None


@dataclass(frozen=True)
class StreamHandle:
    stream_id: str
    endpoint: str
    transport: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class StreamHealth:
    status: StreamHealthStatus
    checked_at: datetime
    detail_code: str | None = None


class CameraProvider(Protocol):
    """Provider boundary. Implementations own credentials and provider-specific protocols."""

    async def discover_devices(self) -> list[DiscoveredCamera]: ...

    async def inspect_capabilities(self, opaque_device_id: str) -> CameraCapabilities: ...

    def open_stream(self, request: StreamRequest) -> AbstractAsyncContextManager[StreamHandle]: ...

    async def stream_health(self, handle: StreamHandle) -> StreamHealth: ...

    async def reconnect(self, handle: StreamHandle) -> StreamHandle: ...

    async def retrieve_recording(
        self, opaque_device_id: str, start: datetime, end: datetime
    ) -> bytes: ...

    async def request_snapshot(self, opaque_device_id: str) -> bytes: ...
