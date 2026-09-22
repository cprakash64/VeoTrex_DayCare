"""V1-01A-1: the tenant-scoped Ring connection list behind the dashboard sync control.

The key case is the one V1-01A-0 found: an ACTIVE connection with zero devices, zero components
and zero cameras must be listed so its first sync can be started from the dashboard. The list
carries lifecycle and sync state only; the credential reference, credential owner and provider
account identifier never leave the API. Both tenants run over the real runtime role and RLS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, text

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError
from veotrex_api.main import create_app
from veotrex_api.models import Camera, CameraProviderConnection, CameraProviderDevice

ISSUER = "https://tenant.auth0.example/"
FORBIDDEN_KEYS = {
    "secret_ref",
    "credential_owner_id",
    "external_account_id",
    "access_token",
    "refresh_token",
    "nonce",
    "ciphertext",
}


class StubIdentityVerifier:
    def __init__(self, identities: dict[str, ExternalIdentity]) -> None:
        self.identities = identities

    async def verify(self, token: str) -> ExternalIdentity:
        try:
            return self.identities[token]
        except KeyError as exc:
            raise IdentityVerificationError("rejected") from exc


class Secrets:
    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr("connections-test-secret")


class NoRing:
    async def aclose(self) -> None:
        return None


async def seed_tenant(  # type: ignore[no-untyped-def]
    admin_factory, label: str, organization: str, subject: str, role: str
) -> tuple[UUID, UUID]:
    tenant_id, actor_id = uuid4(), uuid4()
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Connections tenant {label}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_identity_bindings "
                "(id, tenant_id, provider, issuer, external_organization_id) "
                "VALUES (:id, :tenant_id, 'auth0', :issuer, :organization)"
            ),
            {"id": uuid4(), "tenant_id": tenant_id, "issuer": ISSUER, "organization": organization},
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
        await session.execute(
            text(
                "INSERT INTO actor_identities "
                "(id, tenant_id, actor_id, provider, issuer, subject) "
                "VALUES (:id, :tenant, :actor, 'auth0', :issuer, :subject)"
            ),
            {
                "id": uuid4(),
                "tenant": tenant_id,
                "actor": actor_id,
                "issuer": ISSUER,
                "subject": subject,
            },
        )
        await session.execute(
            text(
                "INSERT INTO role_assignments (id, tenant_id, actor_id, role) "
                "VALUES (:id, :tenant, :actor, :role)"
            ),
            {"id": uuid4(), "tenant": tenant_id, "actor": actor_id, "role": role},
        )
    return tenant_id, actor_id


async def seed_connection(  # type: ignore[no-untyped-def]
    admin_factory,
    tenant_id: UUID,
    actor_id: UUID,
    *,
    name: str,
    status: str = "ACTIVE",
    integration_state: str = "ACTIVE",
    operational_health: str = "ACTIVE",
    last_sync_at: datetime | None = None,
    failure: str | None = None,
) -> UUID:
    connection_id = uuid4()
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
        )
        session.add(
            CameraProviderConnection(
                id=connection_id,
                tenant_id=tenant_id,
                name=name,
                provider_type="RING",
                secret_ref=f"vault://postgres/{uuid4()}",
                credential_owner_id=uuid4(),
                credential_generation=1,
                status=status,
                external_account_id=f"acct-{uuid4().hex}",
                integration_state=integration_state,
                operational_health=operational_health,
                linked_by_actor_id=actor_id,
                linked_at=datetime.now(UTC),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                last_sync_at=last_sync_at,
                last_sync_failure_category=failure,
            )
        )
    return connection_id


async def test_zero_inventory_active_connection_is_listed_and_tenant_scoped(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    org_a, org_b = f"org_{uuid4().hex}", f"org_{uuid4().hex}"
    sub_a, sub_b, sub_viewer = (f"auth0|{uuid4().hex}" for _ in range(3))
    try:
        tenant_a, actor_a = await seed_tenant(admin_factory, "A", org_a, sub_a, "TENANT_OWNER")
        tenant_b, actor_b = await seed_tenant(admin_factory, "B", org_b, sub_b, "TENANT_OWNER")
        # Tenant A: the exact production-shaped case, plus a configuring and a disconnected one.
        fresh = await seed_connection(admin_factory, tenant_a, actor_a, name="Ring account fresh")
        configuring = await seed_connection(
            admin_factory,
            tenant_a,
            actor_a,
            name="Ring account setup",
            status="PENDING",
            integration_state="CONFIGURING",
            failure="provider_rejected",
        )
        gone = await seed_connection(
            admin_factory,
            tenant_a,
            actor_a,
            name="Ring account gone",
            status="DISABLED",
            integration_state="DISCONNECTED",
        )
        other = await seed_connection(admin_factory, tenant_b, actor_b, name="Ring account B")
        # A viewer in tenant A: may read the list, may not sync.
        async with admin_factory() as session, session.begin():
            viewer_id = uuid4()
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_a)}
            )
            await session.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) "
                    "VALUES (:actor, :tenant, 'Viewer', 'ACTIVE')"
                ),
                {"actor": viewer_id, "tenant": tenant_a},
            )
            await session.execute(
                text(
                    "INSERT INTO actor_identities "
                    "(id, tenant_id, actor_id, provider, issuer, subject) "
                    "VALUES (:id, :tenant, :actor, 'auth0', :issuer, :subject)"
                ),
                {
                    "id": uuid4(),
                    "tenant": tenant_a,
                    "actor": viewer_id,
                    "issuer": ISSUER,
                    "subject": sub_viewer,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO role_assignments (id, tenant_id, actor_id, role) "
                    "VALUES (:id, :tenant, :actor, 'VIEWER')"
                ),
                {"id": uuid4(), "tenant": tenant_a, "actor": viewer_id},
            )
            # Proof of the zero-inventory precondition for tenant A's fresh connection.
            for model in (CameraProviderDevice, Camera):
                assert (
                    await session.scalar(
                        select(func.count()).select_from(model).where(model.tenant_id == tenant_a)  # type: ignore[attr-defined]
                    )
                    == 0
                )

        verifier = StubIdentityVerifier(
            {
                "owner-a": ExternalIdentity("auth0", ISSUER, sub_a, org_a),
                "owner-b": ExternalIdentity("auth0", ISSUER, sub_b, org_b),
                "viewer-a": ExternalIdentity("auth0", ISSUER, sub_viewer, org_a),
            }
        )
        app = create_app(
            settings,
            engine,
            verifier,
            factory,
            credential_vault=InMemoryCredentialVault(),
            ring_client=NoRing(),  # type: ignore[arg-type]
            secret_resolver=Secrets(),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = "/v1/integrations/ring/connections"
            assert (await client.get(path)).status_code == 401
            assert (
                await client.get(path, headers={"Authorization": "Bearer nobody"})
            ).status_code == 401

            listed = await client.get(path, headers={"Authorization": "Bearer owner-a"})
            assert listed.status_code == 200, listed.text
            rows = listed.json()
            assert [row["connection_id"] for row in rows] == [
                str(fresh),
                str(gone),
                str(configuring),
            ], "deterministic by display name"
            by_id = {row["connection_id"]: row for row in rows}
            assert by_id[str(fresh)] == {
                "connection_id": str(fresh),
                "display_name": "Ring account fresh",
                "provider": "RING",
                "status": "ACTIVE",
                "integration_state": "ACTIVE",
                "operational_health": "ACTIVE",
                "last_synchronized_at": None,
                "last_sync_failure_category": None,
            }
            assert by_id[str(configuring)]["integration_state"] == "CONFIGURING"
            assert by_id[str(configuring)]["status"] == "PENDING"
            assert by_id[str(configuring)]["last_sync_failure_category"] == "provider_rejected"
            assert by_id[str(gone)]["integration_state"] == "DISCONNECTED"
            for row in rows:
                assert not FORBIDDEN_KEYS & set(row), row.keys()
            assert "acct-" not in listed.text and "vault://" not in listed.text
            assert str(other) not in listed.text and "Ring account B" not in listed.text

            # Tenant B sees only its own connection; tenant A's ids never appear.
            listed_b = await client.get(path, headers={"Authorization": "Bearer owner-b"})
            assert [row["connection_id"] for row in listed_b.json()] == [str(other)]
            assert str(fresh) not in listed_b.text

            # A viewer can read the list (read:operational) but cannot trigger a sync.
            viewer = await client.get(path, headers={"Authorization": "Bearer viewer-a"})
            assert viewer.status_code == 200 and len(viewer.json()) == 3
            assert (
                await client.post(
                    f"/v1/integrations/ring/connections/{fresh}/sync",
                    headers={"Authorization": "Bearer viewer-a"},
                )
            ).status_code == 403
            # Knowing tenant A's connection id gives tenant B nothing: same reply as unknown.
            cross = await client.post(
                f"/v1/integrations/ring/connections/{fresh}/sync",
                headers={"Authorization": "Bearer owner-b"},
            )
            unknown = await client.post(
                f"/v1/integrations/ring/connections/{uuid4()}/sync",
                headers={"Authorization": "Bearer owner-b"},
            )
            assert cross.status_code == unknown.status_code == 502
            assert cross.text == unknown.text
        async with admin_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_a)}
            )
            state = await session.scalar(
                select(CameraProviderConnection.operational_health).where(
                    CameraProviderConnection.id == fresh
                )
            )
            assert state == "ACTIVE", "listing and refused syncs must not mutate the connection"
    finally:
        await engine.dispose()
        await admin_engine.dispose()
