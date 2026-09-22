"""End-to-end tenant isolation through the HTTP API, over the real runtime role.

Two tenants each own a Ring connection with one camera. Tenant A's principal must see only its
own devices and must not be able to act on Tenant B's connection. The API's existing contract
for a resource the caller cannot see is ``404`` (not ``403``), so a cross-tenant identifier is
indistinguishable from a non-existent one and discloses nothing.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from veotrex_api.config import Settings
from veotrex_api.credential_vault import CredentialMaterial, InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError
from veotrex_api.main import create_app
from veotrex_api.models import CameraProviderConnection
from veotrex_api.ring_inventory import NormalizedComponent, NormalizedDevice
from veotrex_api.ring_service import ring_credential_context

ISSUER = "https://tenant.auth0.example/"


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
        return SecretStr("isolation-test-secret")


class RingClient:
    """Discovery returns one device whose name carries the tenant label it was seeded for."""

    def __init__(self) -> None:
        self.by_token: dict[str, str] = {}

    async def discover_devices(self, token: SecretStr) -> tuple[NormalizedDevice, ...]:
        # The token reaches here only after RingLinkService resolved the connection under the
        # caller's tenant context; an unknown token would be a test wiring error, not a case.
        label = self.by_token[token.get_secret_value()]
        return (
            NormalizedDevice(
                f"device-{label}",
                f"Camera {label}",
                True,
                "US",
                "AZ",
                "a" * 64,
                "b" * 64,
                (
                    NormalizedComponent(
                        None, "__single__", f"Camera {label}", ("LIVE_VIDEO",), {}, False, False
                    ),
                ),
            ),
        )

    async def aclose(self) -> None:
        return None


async def seed_tenant(  # type: ignore[no-untyped-def]
    admin_factory, vault: InMemoryCredentialVault, label: str, organization: str, subject: str
) -> tuple[UUID, UUID]:
    tenant_id, actor_id, connection_id, owner_id = uuid4(), uuid4(), uuid4(), uuid4()
    credential = await vault.store_new(
        ring_credential_context(owner_id),
        CredentialMaterial(SecretStr(f"token-{label}"), SecretStr(f"refresh-{label}")),
    )
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Isolation tenant {label}"},
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
                "VALUES (:id, :tenant, :actor, 'TENANT_OWNER')"
            ),
            {"id": uuid4(), "tenant": tenant_id, "actor": actor_id},
        )
        session.add(
            CameraProviderConnection(
                id=connection_id,
                tenant_id=tenant_id,
                name=f"Ring {label}",
                provider_type="RING",
                secret_ref=credential.secret_ref,
                credential_owner_id=owner_id,
                credential_generation=1,
                status="ACTIVE",
                external_account_id=f"account-{connection_id}",
                integration_state="ACTIVE",
                linked_by_actor_id=actor_id,
                linked_at=datetime.now(UTC),
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    return tenant_id, connection_id


async def test_tenant_a_cannot_read_or_act_on_tenant_b_over_http(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    organization_a, organization_b = f"org_{uuid4().hex}", f"org_{uuid4().hex}"
    subject_a, subject_b = f"auth0|{uuid4().hex}", f"auth0|{uuid4().hex}"
    vault = InMemoryCredentialVault()
    try:
        tenant_a, connection_a = await seed_tenant(
            admin_factory, vault, "A", organization_a, subject_a
        )
        tenant_b, connection_b = await seed_tenant(
            admin_factory, vault, "B", organization_b, subject_b
        )

        ring_client = RingClient()
        ring_client.by_token = {"token-A": "A", "token-B": "B"}
        verifier = StubIdentityVerifier(
            {
                "user-a": ExternalIdentity("auth0", ISSUER, subject_a, organization_a),
                "user-b": ExternalIdentity("auth0", ISSUER, subject_b, organization_b),
            }
        )
        app = create_app(
            settings,
            engine,
            verifier,
            factory,
            credential_vault=vault,
            ring_client=ring_client,  # type: ignore[arg-type]
            secret_resolver=Secrets(),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers_a = {"Authorization": "Bearer user-a"}
            headers_b = {"Authorization": "Bearer user-b"}
            for headers, connection_id in ((headers_a, connection_a), (headers_b, connection_b)):
                synced = await client.post(
                    f"/v1/integrations/ring/connections/{connection_id}/sync", headers=headers
                )
                assert synced.status_code == 200, synced.text
                assert synced.json()["cameras_created"] == 1

            devices_a = await client.get("/v1/integrations/ring/devices", headers=headers_a)
            assert devices_a.status_code == 200
            names_a = {device["display_name"] for device in devices_a.json()}
            assert names_a == {"Camera A"}
            assert all(device["connection_id"] == str(connection_a) for device in devices_a.json())
            assert "Camera B" not in devices_a.text and str(connection_b) not in devices_a.text

            devices_b = await client.get("/v1/integrations/ring/devices", headers=headers_b)
            assert {device["display_name"] for device in devices_b.json()} == {"Camera B"}

            # Cross-tenant identifiers must be indistinguishable from unknown ones. The API's
            # existing per-route contract is preserved, not redesigned here: sync resolves the
            # credential first and reports 502 "unavailable", resume reports a generic 409, and
            # disconnect reports 404. None discloses anything about tenant B's row, and the
            # cross-tenant and never-existed cases are byte-identical.
            for action, expected in (("sync", 502), ("resume", 409), ("disconnect", 404)):
                cross = await client.post(
                    f"/v1/integrations/ring/connections/{connection_b}/{action}",
                    headers=headers_a,
                )
                unknown = await client.post(
                    f"/v1/integrations/ring/connections/{uuid4()}/{action}", headers=headers_a
                )
                assert cross.status_code == expected, (action, cross.text)
                assert unknown.status_code == expected, (action, unknown.text)
                assert cross.text == unknown.text, action
                assert "Ring B" not in cross.text and "Camera B" not in cross.text
                assert str(connection_b) not in cross.text

        async with admin_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_b)}
            )
            state = await session.scalar(
                text("SELECT integration_state FROM camera_provider_connections WHERE id = :id"),
                {"id": connection_b},
            )
            assert state == "ACTIVE", "tenant A's requests must not have mutated tenant B"
    finally:
        await engine.dispose()
        await admin_engine.dispose()
