from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import func, select, text

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.models import Camera, CameraProviderConnection, CameraProviderDevice
from veotrex_api.ring_inventory import NormalizedComponent, NormalizedDevice
from veotrex_api.ring_inventory_service import RingInventoryService


class Link:
    async def get_valid_access_token(self, tenant_id, connection_id) -> SecretStr:
        return SecretStr("test-access")


class Client:
    def __init__(self, devices: tuple[NormalizedDevice, ...]) -> None:
        self.devices = devices

    async def discover_devices(self, token: SecretStr) -> tuple[NormalizedDevice, ...]:
        return self.devices


def ring_device(name: str = "Camera", *, component_count: int = 1) -> NormalizedDevice:
    components = tuple(
        NormalizedComponent(
            None if component_count == 1 else f"opaque/{index}",
            "__single__" if component_count == 1 else f"provider:opaque/{index}",
            name if component_count == 1 else f"{name} {index}",
            ("LIVE_VIDEO",),
            {"video": {"codec": "h264"}},
            True,
            False,
        )
        for index in range(component_count)
    )
    return NormalizedDevice(
        "opaque/device/id",
        name,
        True,
        "US",
        "AZ",
        "a" * 64,
        "b" * 64,
        components,
    )


async def setup_connection(factory) -> tuple[AuthenticatedPrincipal, UUID]:
    tenant_id, actor_id, connection_id = uuid4(), uuid4(), uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Inventory', 'ACTIVE')"),
            {"id": tenant_id},
        )
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
        )
        await session.execute(
            text(
                "INSERT INTO actors (id, tenant_id, display_name, status) "
                "VALUES (:actor, :tenant, 'Owner', 'ACTIVE')"
            ),
            {"actor": actor_id, "tenant": tenant_id},
        )
        session.add(
            CameraProviderConnection(
                id=connection_id,
                tenant_id=tenant_id,
                name=f"Ring {connection_id}",
                provider_type="RING",
                status="ACTIVE",
                external_account_id=f"account-{connection_id}",
                integration_state="ACTIVE",
                linked_by_actor_id=actor_id,
                linked_at=datetime.now(UTC),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    principal = AuthenticatedPrincipal(
        issuer="https://test.example/",
        subject="auth0|inventory",
        external_organization_id="org_inventory",
        actor_id=actor_id,
        tenant_id=tenant_id,
        display_name="Owner",
        grants=(RoleGrant(Role.TENANT_OWNER, None),),
        permissions=frozenset(Permission),
    )
    return principal, connection_id


async def test_reconciliation_is_idempotent_and_preserves_camera_identity(settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    client = Client((ring_device(component_count=2),))
    subject = RingInventoryService(factory, Link(), client)  # type: ignore[arg-type]
    principal, connection_id = await setup_connection(factory)
    try:
        first = await subject.sync_connection(principal, connection_id)  # type: ignore[arg-type]
        assert first.cameras_created == 2
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            original_ids = tuple(
                (
                    await session.scalars(
                        select(Camera.id).where(Camera.tenant_id == principal.tenant_id)
                    )
                ).all()
            )
        client.devices = (ring_device("Renamed", component_count=2),)
        second = await subject.sync_connection(principal, connection_id)  # type: ignore[arg-type]
        assert second.cameras_created == 0
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            assert (
                tuple(
                    (
                        await session.scalars(
                            select(Camera.id).where(Camera.tenant_id == principal.tenant_id)
                        )
                    ).all()
                )
                == original_ids
            )
            assert set(
                (
                    await session.scalars(
                        select(Camera.name).where(Camera.tenant_id == principal.tenant_id)
                    )
                ).all()
            ) == {
                "Renamed 0",
                "Renamed 1",
            }
    finally:
        await engine.dispose()


async def test_temporary_discovery_omission_does_not_remove_device(settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    client = Client((ring_device(),))
    subject = RingInventoryService(factory, Link(), client)  # type: ignore[arg-type]
    principal, connection_id = await setup_connection(factory)
    try:
        await subject.sync_connection(principal, connection_id)  # type: ignore[arg-type]
        client.devices = ()
        await subject.sync_connection(principal, connection_id)  # type: ignore[arg-type]
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Camera)
                    .where(Camera.tenant_id == principal.tenant_id)
                )
                == 1
            )
            state = await session.scalar(
                select(CameraProviderDevice.inventory_state).where(
                    CameraProviderDevice.tenant_id == principal.tenant_id
                )
            )
            assert state == "ACTIVE"
    finally:
        await engine.dispose()
