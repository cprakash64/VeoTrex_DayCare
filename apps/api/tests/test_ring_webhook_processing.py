import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.credential_vault import (
    CredentialMaterial,
    CredentialVaultError,
    InMemoryCredentialVault,
)
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.models import Camera, CameraProviderConnection, CameraProviderDevice
from veotrex_api.ring_client import RingTokenSet
from veotrex_api.ring_inventory import NormalizedComponent, NormalizedDevice
from veotrex_api.ring_inventory_service import RingInventoryService
from veotrex_api.ring_service import RingLinkService, ring_credential_context
from veotrex_api.ring_webhook import RingWebhookService

KEY = "processing-test-hmac-key"


class Secrets:
    def resolve(self, _: str) -> SecretStr:
        return SecretStr(KEY)


class Link:
    async def get_valid_access_token(self, tenant_id, connection_id, **kwargs) -> SecretStr:
        return SecretStr("access")


class Client:
    async def discover_devices(self, token: SecretStr) -> tuple[NormalizedDevice, ...]:
        return (device(),)

    async def get_device(self, token: SecretStr, provider_device_id: str) -> NormalizedDevice:
        return device()


class BlockingClient(Client):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def discover_devices(self, token: SecretStr) -> tuple[NormalizedDevice, ...]:
        self.started.set()
        await self.release.wait()
        return (device(),)


class BlockingRefreshClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def refresh(self, token: SecretStr) -> RingTokenSet:
        self.started.set()
        await self.release.wait()
        return RingTokenSet(SecretStr("rotated-access"), SecretStr("rotated-refresh"), 3600, ())


def device() -> NormalizedDevice:
    return NormalizedDevice(
        "opaque-device",
        "Ring camera",
        True,
        "US",
        "AZ",
        "a" * 64,
        "b" * 64,
        (
            NormalizedComponent(
                None, "__single__", "Ring camera", ("LIVE_VIDEO",), {}, False, False
            ),
        ),
    )


def webhook(account: str, event_type: str, timestamp: int) -> tuple[bytes, str]:
    raw = json.dumps(
        {
            "meta": {
                "version": "1.1",
                "time": "2026-08-23T12:00:00Z",
                "request_id": str(uuid4()),
                "account_id": account,
            },
            "data": {
                "id": str(uuid4()),
                "type": event_type,
                "attributes": {
                    "source": "opaque-device",
                    "source_type": "devices",
                    "timestamp": timestamp,
                },
            },
        },
        separators=(",", ":"),
    ).encode()
    return raw, "sha256=" + hmac.new(KEY.encode(), raw, hashlib.sha256).hexdigest()


async def setup(factory, vault: InMemoryCredentialVault):
    tenant_id, actor_id, connection_id, owner_id = uuid4(), uuid4(), uuid4(), uuid4()
    credential = await vault.store_new(
        ring_credential_context(owner_id),
        CredentialMaterial(SecretStr("access"), SecretStr("refresh")),
    )
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM ring_webhook_inbox"))
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Webhook', 'ACTIVE')"),
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
                secret_ref=credential.secret_ref,
                credential_owner_id=owner_id,
                status="ACTIVE",
                external_account_id=f"account-{connection_id}",
                integration_state="ACTIVE",
                linked_by_actor_id=actor_id,
                linked_at=datetime.now(UTC),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    principal = AuthenticatedPrincipal(
        "https://test.example/",
        "auth0|webhook",
        "org_webhook",
        actor_id,
        tenant_id,
        "Owner",
        (RoleGrant(Role.TENANT_OWNER, None),),
        frozenset(Permission),
    )
    return principal, connection_id, owner_id, credential.secret_ref


async def test_out_of_order_status_and_explicit_removal_reappearance(settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    vault = InMemoryCredentialVault()
    principal, connection_id, _, _ = await setup(factory, vault)
    inventory = RingInventoryService(factory, Link(), Client())  # type: ignore[arg-type]
    processor = RingWebhookService(factory, Secrets(), "test:key", vault, inventory)
    account = f"account-{connection_id}"
    try:
        first = await inventory.sync_connection(principal, connection_id)
        assert first.cameras_created == 1
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            camera_id = await session.scalar(
                select(Camera.id).where(Camera.tenant_id == principal.tenant_id)
            )

        base = int(datetime.now(UTC).timestamp() * 1000)
        for event_type, timestamp in (
            ("device_offline", base + 2000),
            ("device_online", base + 1000),
        ):
            raw, signature = webhook(account, event_type, timestamp)
            assert await processor.ingest(raw, signature)
            assert await processor.process_one()
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            assert (
                await session.scalar(
                    select(CameraProviderDevice.provider_online).where(
                        CameraProviderDevice.tenant_id == principal.tenant_id
                    )
                )
                is False
            )

        raw, signature = webhook(account, "device_removed", base + 3000)
        await processor.ingest(raw, signature)
        await processor.process_one()
        await inventory.sync_connection(principal, connection_id)
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            removed = await session.scalar(
                select(Camera).where(Camera.tenant_id == principal.tenant_id)
            )
            assert removed is not None and removed.status == "DISABLED"
        await inventory.sync_device(principal.tenant_id, connection_id, "opaque-device")
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            camera = await session.scalar(
                select(Camera).where(Camera.tenant_id == principal.tenant_id)
            )
            assert camera is not None
            assert camera.id == camera_id
            assert camera.status == "DISCOVERED"

        blocking = BlockingClient()
        racing_inventory = RingInventoryService(factory, Link(), blocking)  # type: ignore[arg-type]
        sync_task = asyncio.create_task(racing_inventory.sync_connection(principal, connection_id))
        await blocking.started.wait()
        raw, signature = webhook(account, "device_removed", base + 4000)
        await processor.ingest(raw, signature)
        await processor.process_one()
        blocking.release.set()
        await sync_task
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            raced = await session.scalar(
                select(Camera).where(Camera.tenant_id == principal.tenant_id)
            )
            assert raced is not None and raced.status == "DISABLED"
    finally:
        await engine.dispose()


async def test_app_integration_removal_revokes_local_credential(settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    vault = InMemoryCredentialVault()
    principal, connection_id, owner_id, secret_ref = await setup(factory, vault)
    inventory = RingInventoryService(factory, Link(), Client())  # type: ignore[arg-type]
    processor = RingWebhookService(factory, Secrets(), "test:key", vault, inventory)
    try:
        await inventory.sync_connection(principal, connection_id)
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            connection = await session.get(CameraProviderConnection, connection_id)
            assert connection is not None
            connection.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        refresh_client = BlockingRefreshClient()
        link_service = RingLinkService(
            settings,
            factory,
            vault,
            refresh_client,  # type: ignore[arg-type]
            Secrets(),
        )
        refresh_task = asyncio.create_task(
            link_service.get_valid_access_token(principal.tenant_id, connection_id)
        )
        await refresh_client.started.wait()
        raw, signature = webhook(f"account-{connection_id}", "app_integration_removed", 4000)
        await processor.ingest(raw, signature)
        removal_task = asyncio.create_task(processor.process_one())
        await asyncio.sleep(0)
        refresh_client.release.set()
        assert (await refresh_task).get_secret_value() == "rotated-access"
        assert await removal_task
        with pytest.raises(CredentialVaultError):
            await vault.get(secret_ref, ring_credential_context(owner_id))
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(principal.tenant_id)},
            )
            connection = await session.get(CameraProviderConnection, connection_id)
            assert connection is not None
            assert connection.operational_health == "REMOTE_REMOVED"
            assert connection.integration_state == "DISCONNECTED"
            assert connection.secret_ref is None
    finally:
        await engine.dispose()
