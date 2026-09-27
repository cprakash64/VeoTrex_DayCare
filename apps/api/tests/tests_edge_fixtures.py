"""Synthetic edge-node world for V1-DEMO-03B tests. No network, no real credential, no media.

``seed_world`` builds, with the ADMIN session factory, one tenant with a facility, an edge node
holding a fresh machine credential, and a Ring connection whose (synthetic) OAuth tokens sit in
an in-memory vault, plus one camera with an ACTIVE assignment to that node. ``FakeRing`` is an
``httpx.MockTransport`` handler that plays Ring's OAuth token endpoint and WHEP endpoints and
records every request it receives.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.credential_vault import CredentialContext, CredentialMaterial
from veotrex_api.edge_auth import issue_edge_credential
from veotrex_api.models import (
    CameraAssignment,
    CameraProviderComponent,
    CameraProviderConnection,
    CameraProviderDevice,
    EdgeNode,
    EdgeNodeCredential,
    Facility,
)

RING_ACCESS = "synthetic-ring-access-token-A1"
RING_REFRESH = "synthetic-ring-refresh-token-R1"
RING_ACCESS_REFRESHED = "synthetic-ring-access-token-A2"
RING_REFRESH_ROTATED = "synthetic-ring-refresh-token-R2"
RING_API = "https://api.amazonvision.com"
OFFER = (
    b"v=0\r\n"
    b"o=- 1 1 IN IP4 127.0.0.1\r\n"
    b"s=-\r\n"
    b"t=0 0\r\n"
    b"m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    b"c=IN IP4 0.0.0.0\r\n"
    b"a=rtpmap:96 H264/90000\r\n"
    b"a=recvonly\r\n"
    b"a=mid:0\r\n"
)
ANSWER = (
    b"v=0\r\n"
    b"o=- 2 2 IN IP4 127.0.0.1\r\n"
    b"s=-\r\n"
    b"t=0 0\r\n"
    b"m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    b"c=IN IP4 0.0.0.0\r\n"
    b"a=rtpmap:96 H264/90000\r\n"
    b"a=sendonly\r\n"
    b"a=mid:0\r\n"
)


class Secrets:
    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr("synthetic-client-secret")


async def set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@dataclass
class EdgeWorld:
    tenant_id: UUID
    facility_id: UUID
    node_id: UUID
    credential_id: UUID
    token: SecretStr
    connection_id: UUID
    camera_id: UUID
    device_record_id: UUID
    component_record_id: UUID
    assignment_id: UUID
    device_id: str
    component_id: str | None


async def add_credential(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID, node_id: UUID
) -> tuple[UUID, SecretStr]:
    issued = issue_edge_credential()
    async with admin_factory() as session, session.begin():
        await set_tenant(session, tenant_id)
        session.add(
            EdgeNodeCredential(
                id=issued.credential_id,
                tenant_id=tenant_id,
                edge_node_id=node_id,
                secret_sha256=issued.secret_sha256,
                status="ACTIVE",
            )
        )
    return issued.credential_id, issued.token


async def add_node(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID, facility_id: UUID
) -> UUID:
    node_id = uuid4()
    async with admin_factory() as session, session.begin():
        await set_tenant(session, tenant_id)
        session.add(
            EdgeNode(
                id=node_id,
                tenant_id=tenant_id,
                facility_id=facility_id,
                name=f"Synthetic Jetson {node_id.hex[:8]}",
                architecture="aarch64",
                gpu_available=True,
                memory_mb=8192,
                software_version="0.1.0-test",
                status="ONLINE",
            )
        )
    return node_id


async def add_camera(
    admin_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    connection_id: UUID,
    *,
    device_id: str,
    component_id: str | None,
    capabilities: tuple[str, ...] = ("LIVE_VIDEO",),
    assign_to: UUID | None = None,
) -> tuple[UUID, UUID, UUID, UUID | None]:
    from veotrex_api.models import Camera

    camera_id, device_record_id, component_record_id = uuid4(), uuid4(), uuid4()
    assignment_id = uuid4() if assign_to else None
    component_key = "__single__" if component_id is None else f"provider:{component_id}"
    async with admin_factory() as session, session.begin():
        await set_tenant(session, tenant_id)
        session.add(
            CameraProviderDevice(
                id=device_record_id,
                tenant_id=tenant_id,
                provider_connection_id=connection_id,
                provider_device_id=device_id,
                display_name="Synthetic Ring device",
                inventory_state="ACTIVE",
                sync_state="HEALTHY",
            )
        )
        session.add(
            Camera(
                id=camera_id,
                tenant_id=tenant_id,
                zone_id=None,
                provider_connection_id=connection_id,
                provider_device_id=device_id,
                provider_component_id=component_id,
                provider_component_key=component_key,
                name="Synthetic camera",
                status="DISCOVERED",
            )
        )
        await session.flush()
        session.add(
            CameraProviderComponent(
                id=component_record_id,
                tenant_id=tenant_id,
                provider_device_record_id=device_record_id,
                camera_id=camera_id,
                component_key=component_key,
                provider_component_id=component_id,
                display_name="Synthetic camera",
                inventory_state="ACTIVE",
                capabilities=list(capabilities),
                capability_details={},
            )
        )
        if assign_to is not None:
            await session.flush()
            session.add(
                CameraAssignment(
                    id=assignment_id,
                    tenant_id=tenant_id,
                    camera_id=camera_id,
                    edge_node_id=assign_to,
                    assigned_at=datetime.now(UTC) - timedelta(minutes=5),
                )
            )
    return camera_id, device_record_id, component_record_id, assignment_id


async def seed_world(
    admin_factory: async_sessionmaker[AsyncSession],
    vault: Any,
    *,
    device_id: str | None = None,
    component_id: str | None = None,
    access_expires_in: timedelta = timedelta(hours=1),
) -> EdgeWorld:
    tenant_id, facility_id, actor_id = uuid4(), uuid4(), uuid4()
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Edge tenant {tenant_id.hex[:8]}"},
        )
        await set_tenant(session, tenant_id)
        await session.execute(
            text(
                "INSERT INTO actors (id, tenant_id, display_name, status) "
                "VALUES (:actor, :tenant, 'Owner', 'ACTIVE')"
            ),
            {"actor": actor_id, "tenant": tenant_id},
        )
        session.add(
            Facility(
                id=facility_id,
                tenant_id=tenant_id,
                name=f"Facility {facility_id.hex[:8]}",
                jurisdiction="US-AZ",
                timezone="America/Phoenix",
                status="ACTIVE",
            )
        )
    node_id = await add_node(admin_factory, tenant_id, facility_id)
    credential_id, token = await add_credential(admin_factory, tenant_id, node_id)

    owner_id, connection_id = uuid4(), uuid4()
    stored = await vault.store_new(
        CredentialContext("RING", "ring_pending_link", owner_id),
        CredentialMaterial(SecretStr(RING_ACCESS), SecretStr(RING_REFRESH)),
    )
    async with admin_factory() as session, session.begin():
        await set_tenant(session, tenant_id)
        session.add(
            CameraProviderConnection(
                id=connection_id,
                tenant_id=tenant_id,
                name=f"Ring {connection_id.hex[:8]}",
                provider_type="RING",
                secret_ref=stored.secret_ref,
                credential_owner_id=owner_id,
                status="ACTIVE",
                external_account_id=f"synthetic-account-{connection_id.hex}",
                integration_state="ACTIVE",
                linked_by_actor_id=actor_id,
                linked_at=datetime.now(UTC),
                access_expires_at=datetime.now(UTC) + access_expires_in,
                credential_generation=1,
            )
        )
    device = device_id or f"synthetic-device-{uuid4().hex[:12]}"
    camera_id, device_record_id, component_record_id, assignment_id = await add_camera(
        admin_factory,
        tenant_id,
        connection_id,
        device_id=device,
        component_id=component_id,
        assign_to=node_id,
    )
    assert assignment_id is not None
    return EdgeWorld(
        tenant_id,
        facility_id,
        node_id,
        credential_id,
        token,
        connection_id,
        camera_id,
        device_record_id,
        component_record_id,
        assignment_id,
        device,
        component_id,
    )


@dataclass
class Scripted:
    status: int = 201
    body: bytes = ANSWER
    content_type: str | None = "application/sdp"
    location: str | None = "default"
    raise_error: Exception | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


class FakeRing:
    """Ring OAuth + WHEP, scripted. Records method, path, headers and body of each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.whep_scripts: list[Scripted] = []
        self.delete_scripts: list[Scripted] = []
        self.refresh_calls = 0
        self.on_request: Callable[[httpx.Request], None] | None = None

    def whep_posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and "/whep/" in r.url.path]

    def whep_deletes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "DELETE"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if request.url.path == "/oauth/token":
            self.refresh_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": RING_ACCESS_REFRESHED,
                    "refresh_token": RING_REFRESH_ROTATED,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "integration",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/whep/sessions"):
            script = self.whep_scripts.pop(0) if self.whep_scripts else Scripted()
            if script.raise_error is not None:
                raise script.raise_error
            headers = dict(script.extra_headers)
            if script.content_type:
                headers["content-type"] = script.content_type
            if script.location == "default":
                headers["location"] = (
                    f"{request.url.path}/synthetic-ring-session-{len(self.requests)}"
                )
            elif script.location is not None:
                headers["location"] = script.location
            return httpx.Response(script.status, content=script.body, headers=headers)
        if request.method == "DELETE" and "/whep/sessions/" in request.url.path:
            script = self.delete_scripts.pop(0) if self.delete_scripts else Scripted(204, b"")
            if script.raise_error is not None:
                raise script.raise_error
            return httpx.Response(script.status, content=script.body)
        return httpx.Response(404)
