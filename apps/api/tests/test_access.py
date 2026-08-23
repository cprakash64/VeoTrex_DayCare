from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from veotrex_api.config import Settings
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError
from veotrex_api.main import create_app

ISSUER = "https://tenant.auth0.example/"


class StubIdentityVerifier:
    def __init__(self, identities: dict[str, ExternalIdentity]) -> None:
        self.identities = identities

    async def verify(self, token: str) -> ExternalIdentity:
        try:
            return self.identities[token]
        except KeyError as exc:
            raise IdentityVerificationError("rejected") from exc


def external(subject: str, organization: str) -> ExternalIdentity:
    return ExternalIdentity(
        provider="auth0",
        issuer=ISSUER,
        subject=subject,
        external_organization_id=organization,
    )


async def test_me_authenticates_maps_tenant_and_fails_closed(settings: Settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    tenant_a, tenant_b, tenant_c = uuid4(), uuid4(), uuid4()
    actor_a, actor_b, actor_c = uuid4(), uuid4(), uuid4()
    organization_a = f"org_{uuid4().hex}"
    organization_b = f"org_{uuid4().hex}"
    organization_c = f"org_{uuid4().hex}"
    subject_a = f"auth0|{uuid4().hex}"
    subject_b = f"auth0|{uuid4().hex}"
    subject_c = f"auth0|{uuid4().hex}"
    organization_a_binding = uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:a, 'Access A', 'ACTIVE'), (:b, 'Access B', 'ACTIVE'), "
                    "(:c, 'Access C', 'ACTIVE')"
                ),
                {"a": tenant_a, "b": tenant_b, "c": tenant_c},
            )
            await connection.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) VALUES "
                    "(:actor_a, :tenant_a, 'Owner A', 'ACTIVE'), "
                    "(:actor_b, :tenant_b, 'No role B', 'ACTIVE'), "
                    "(:actor_c, :tenant_c, 'Archived C', 'ACTIVE')"
                ),
                {
                    "actor_a": actor_a,
                    "tenant_a": tenant_a,
                    "actor_b": actor_b,
                    "tenant_b": tenant_b,
                    "actor_c": actor_c,
                    "tenant_c": tenant_c,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_identity_bindings "
                    "(id, tenant_id, provider, issuer, external_organization_id) VALUES "
                    "(:binding_a, :tenant_a, 'auth0', :issuer, :org_a), "
                    "(:binding_b, :tenant_b, 'auth0', :issuer, :org_b), "
                    "(:binding_c, :tenant_c, 'auth0', :issuer, :org_c)"
                ),
                {
                    "binding_a": organization_a_binding,
                    "tenant_a": tenant_a,
                    "org_a": organization_a,
                    "binding_b": uuid4(),
                    "tenant_b": tenant_b,
                    "org_b": organization_b,
                    "binding_c": uuid4(),
                    "tenant_c": tenant_c,
                    "org_c": organization_c,
                    "issuer": ISSUER,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO actor_identities "
                    "(id, tenant_id, actor_id, provider, issuer, subject, archived_at) VALUES "
                    "(:identity_a, :tenant_a, :actor_a, 'auth0', :issuer, :subject_a, NULL), "
                    "(:identity_b, :tenant_b, :actor_b, 'auth0', :issuer, :subject_b, NULL), "
                    "(:identity_c, :tenant_c, :actor_c, 'auth0', :issuer, :subject_c, now())"
                ),
                {
                    "identity_a": uuid4(),
                    "tenant_a": tenant_a,
                    "actor_a": actor_a,
                    "subject_a": subject_a,
                    "identity_b": uuid4(),
                    "tenant_b": tenant_b,
                    "actor_b": actor_b,
                    "subject_b": subject_b,
                    "identity_c": uuid4(),
                    "tenant_c": tenant_c,
                    "actor_c": actor_c,
                    "subject_c": subject_c,
                    "issuer": ISSUER,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO role_assignments (id, tenant_id, actor_id, role) "
                    "VALUES (:id, :tenant_id, :actor_id, 'TENANT_OWNER')"
                ),
                {"id": uuid4(), "tenant_id": tenant_a, "actor_id": actor_a},
            )

        verifier = StubIdentityVerifier(
            {
                "valid-a": external(subject_a, organization_a),
                "cross-tenant": external(subject_a, organization_b),
                "no-role": external(subject_b, organization_b),
                "archived-identity": external(subject_c, organization_c),
            }
        )
        app = create_app(settings, engine, verifier, factory)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/me")).status_code == 401
            assert (
                await client.get("/v1/me", headers={"Authorization": "Bearer invalid"})
            ).status_code == 401

            response = await client.get(
                f"/v1/me?tenant_id={tenant_b}",
                headers={
                    "Authorization": "Bearer valid-a",
                    "x-tenant-id": str(tenant_b),
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["actor_id"] == str(actor_a)
            assert body["tenant_id"] == str(tenant_a)
            assert body["roles"] == [{"role": "TENANT_OWNER", "facility_id": None}]
            assert "manage:integrations" in body["permissions"]
            assert not {"issuer", "subject", "external_organization_id"} & body.keys()

            assert (
                await client.get("/v1/me", headers={"Authorization": "Bearer cross-tenant"})
            ).status_code == 403
            assert (
                await client.get("/v1/me", headers={"Authorization": "Bearer no-role"})
            ).status_code == 403
            assert (
                await client.get("/v1/me", headers={"Authorization": "Bearer archived-identity"})
            ).status_code == 403

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE tenant_identity_bindings SET archived_at = now() "
                        "WHERE id = :binding_id"
                    ),
                    {"binding_id": organization_a_binding},
                )
            assert (
                await client.get("/v1/me", headers={"Authorization": "Bearer valid-a"})
            ).status_code == 403
    finally:
        await engine.dispose()
