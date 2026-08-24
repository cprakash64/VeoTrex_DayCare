from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.models import (
    Camera,
    CameraProviderComponent,
    CameraProviderConnection,
    CameraProviderDevice,
)
from veotrex_api.ring_client import RingClient, RingClientError
from veotrex_api.ring_inventory import NormalizedDevice
from veotrex_api.ring_service import RingLinkError, RingLinkService


class RingInventoryError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(f"Ring inventory failed: {category}")
        self.category = category


@dataclass(frozen=True, slots=True)
class SyncResult:
    connection_id: UUID
    devices_seen: int
    cameras_created: int
    synchronized_at: datetime


@dataclass(frozen=True, slots=True)
class InventoryCamera:
    camera_id: UUID
    connection_id: UUID
    display_name: str
    inventory_state: str
    provider_online: bool | None
    capabilities: tuple[str, ...]
    assigned: bool
    privacy_controls_configured: bool
    last_synchronized_at: datetime | None


class RingInventoryService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        link_service: RingLinkService,
        client: RingClient,
    ) -> None:
        self._factory = factory
        self._link = link_service
        self._client = client

    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    async def sync_connection(
        self, principal: AuthenticatedPrincipal, connection_id: UUID
    ) -> SyncResult:
        if Permission.MANAGE_INTEGRATIONS not in principal.permissions:
            raise RingInventoryError("access_denied")
        try:
            token = await self._link.get_valid_access_token(principal.tenant_id, connection_id)
            try:
                devices = await self._client.discover_devices(token)
            except RingClientError as exc:
                if exc.category != "unauthorized":
                    raise
                # A GET is safe to repeat after exactly one lifecycle-controlled refresh.
                token = await self._link.get_valid_access_token(
                    principal.tenant_id, connection_id, force_refresh=True
                )
                devices = await self._client.discover_devices(token)
        except RingLinkError as exc:
            await self._record_failure(principal.tenant_id, connection_id, exc.category)
            raise RingInventoryError("credential_unavailable") from exc
        except RingClientError as exc:
            await self._record_failure(principal.tenant_id, connection_id, exc.category)
            raise RingInventoryError(exc.category) from exc
        if len({device.provider_device_id for device in devices}) != len(devices):
            await self._record_failure(principal.tenant_id, connection_id, "duplicate_device")
            raise RingInventoryError("malformed_response")
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection)
                .where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == principal.tenant_id,
                    CameraProviderConnection.provider_type == "RING",
                )
                .with_for_update()
            )
            if connection is None or connection.integration_state != "ACTIVE":
                raise RingInventoryError("connection_unavailable")
            if connection.operational_health == "REMOTE_REMOVED":
                raise RingInventoryError("remote_removed")
            created = 0
            for device in devices:
                created += await self._reconcile_device(
                    session,
                    principal.tenant_id,
                    connection_id,
                    device,
                    now,
                    allow_removed_restore=False,
                )
            connection.last_sync_at = now
            connection.last_sync_failure_category = None
            connection.operational_health = "ACTIVE"
        return SyncResult(connection_id, len(devices), created, now)

    async def sync_device(
        self, tenant_id: UUID, connection_id: UUID, provider_device_id: str
    ) -> None:
        token = await self._link.get_valid_access_token(tenant_id, connection_id)
        device = await self._client.get_device(token, provider_device_id)
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection)
                .where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if connection is None or connection.integration_state != "ACTIVE":
                raise RingInventoryError("connection_unavailable")
            if connection.operational_health == "REMOTE_REMOVED":
                raise RingInventoryError("remote_removed")
            await self._reconcile_device(
                session,
                tenant_id,
                connection_id,
                device,
                now,
                allow_removed_restore=True,
            )
            connection.last_sync_at = now
            connection.last_sync_failure_category = None

    async def _reconcile_device(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        connection_id: UUID,
        incoming: NormalizedDevice,
        now: datetime,
        *,
        allow_removed_restore: bool,
    ) -> int:
        device = await session.scalar(
            select(CameraProviderDevice)
            .where(
                CameraProviderDevice.tenant_id == tenant_id,
                CameraProviderDevice.provider_connection_id == connection_id,
                CameraProviderDevice.provider_device_id == incoming.provider_device_id,
            )
            .with_for_update()
        )
        if device is None:
            device = CameraProviderDevice(
                id=uuid4(),
                tenant_id=tenant_id,
                provider_connection_id=connection_id,
                provider_device_id=incoming.provider_device_id,
                display_name=incoming.display_name,
            )
            session.add(device)
            await session.flush()
        elif device.inventory_state == "REMOVED" and not allow_removed_restore:
            return 0
        device.display_name = incoming.display_name
        device.inventory_state = "ACTIVE"
        device.sync_state = "HEALTHY"
        device.provider_online = incoming.provider_online
        device.status_observed_at = (
            now if incoming.provider_online is not None else device.status_observed_at
        )
        device.last_seen_at = now
        device.last_synced_at = now
        device.removed_at = None
        device.capabilities_sha256 = incoming.capabilities_sha256
        device.configuration_sha256 = incoming.configuration_sha256
        device.location_country = incoming.location_country
        device.location_region = incoming.location_region
        device.last_failure_category = None

        existing = {
            value.component_key: value
            for value in (
                await session.scalars(
                    select(CameraProviderComponent).where(
                        CameraProviderComponent.tenant_id == tenant_id,
                        CameraProviderComponent.provider_device_record_id == device.id,
                    )
                )
            ).all()
        }
        created = 0
        seen: set[str] = set()
        for incoming_component in incoming.components:
            seen.add(incoming_component.component_key)
            component = existing.get(incoming_component.component_key)
            if component is None:
                camera = Camera(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    zone_id=None,
                    provider_connection_id=connection_id,
                    provider_device_id=incoming.provider_device_id,
                    provider_component_id=incoming_component.provider_component_id,
                    provider_component_key=incoming_component.component_key,
                    name=incoming_component.display_name,
                    status="DISCOVERED",
                )
                session.add(camera)
                await session.flush()
                component = CameraProviderComponent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    provider_device_record_id=device.id,
                    camera_id=camera.id,
                    component_key=incoming_component.component_key,
                    provider_component_id=incoming_component.provider_component_id,
                    display_name=incoming_component.display_name,
                )
                session.add(component)
                existing[incoming_component.component_key] = component
                created += 1
            else:
                found_camera = await session.scalar(
                    select(Camera).where(
                        Camera.id == component.camera_id, Camera.tenant_id == tenant_id
                    )
                )
                if found_camera is None:
                    raise RingInventoryError("camera_reference_missing")
                camera = found_camera
                camera.provider_component_id = incoming_component.provider_component_id
                camera.name = incoming_component.display_name
                if camera.status == "DISABLED":
                    camera.status = "ACTIVE" if camera.zone_id is not None else "DISCOVERED"
            component.provider_component_id = incoming_component.provider_component_id
            component.display_name = incoming_component.display_name
            component.inventory_state = "ACTIVE"
            component.capabilities = list(incoming_component.capabilities)
            component.capability_details = incoming_component.capability_details
            component.privacy_zones_configured = incoming_component.privacy_zones_configured
            component.motion_zones_configured = incoming_component.motion_zones_configured
            component.removed_at = None
        for key, component in existing.items():
            if key not in seen and component.inventory_state == "ACTIVE":
                component.inventory_state = "REMOVED"
                component.removed_at = now
                removed_camera = await session.scalar(
                    select(Camera).where(
                        Camera.id == component.camera_id, Camera.tenant_id == tenant_id
                    )
                )
                if removed_camera is not None:
                    removed_camera.status = "DISABLED"
        return created

    async def list_inventory(
        self, principal: AuthenticatedPrincipal
    ) -> tuple[InventoryCamera, ...]:
        if Permission.READ_OPERATIONAL not in principal.permissions:
            raise RingInventoryError("access_denied")
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            rows = (
                await session.execute(
                    select(Camera, CameraProviderComponent, CameraProviderDevice)
                    .join(CameraProviderComponent, CameraProviderComponent.camera_id == Camera.id)
                    .join(
                        CameraProviderDevice,
                        CameraProviderDevice.id
                        == CameraProviderComponent.provider_device_record_id,
                    )
                    .where(Camera.tenant_id == principal.tenant_id)
                    .order_by(Camera.name, Camera.id)
                )
            ).all()
        return tuple(
            InventoryCamera(
                camera.id,
                camera.provider_connection_id,
                component.display_name,
                device.inventory_state,
                device.provider_online,
                tuple(component.capabilities),
                camera.zone_id is not None,
                component.privacy_zones_configured,
                device.last_synced_at,
            )
            for camera, component, device in rows
        )

    async def _record_failure(self, tenant_id: UUID, connection_id: UUID, category: str) -> None:
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection)
                .where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if connection is not None and connection.operational_health != "REMOTE_REMOVED":
                connection.operational_health = (
                    "AUTH_DEGRADED"
                    if category in {"unauthorized", "credential_unavailable"}
                    else "SYNC_DEGRADED"
                )
                connection.last_sync_failure_category = category[:128]
