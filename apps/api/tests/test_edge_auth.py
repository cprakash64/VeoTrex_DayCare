"""V1-DEMO-03B: edge node machine authentication.

Unit tests pin the token format and digest. Database-backed tests run the SECURITY DEFINER
authentication function as the restricted runtime role, and drive the ``/v1/edge`` routes and a
human route through the real app, asserting that every failure is the same generic 401 and that
neither credential kind opens the other's surface.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs
from tests_edge_fixtures import FakeRing, Secrets, add_credential, seed_world, set_tenant

from veotrex_api import access
from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.edge_auth import (
    TOKEN_LENGTH,
    TOKEN_PREFIX,
    EdgeAuthenticationFailed,
    MalformedEdgeCredential,
    bearer_value,
    credential_digest,
    issue_edge_credential,
    parse_edge_token,
)
from veotrex_api.identity import ExternalIdentity
from veotrex_api.main import create_app
from veotrex_api.models import EdgeNode, EdgeNodeCredential, Facility, Tenant
from veotrex_api.ring_client import RingClient

INSUFFICIENT_PRIVILEGE = "42501"


# ---------------------------------------------------------------------------------------- unit
def test_issued_credentials_are_high_entropy_strict_and_only_stored_as_a_bound_digest() -> None:
    issued = issue_edge_credential()
    token = issued.token.get_secret_value()
    assert token.startswith(TOKEN_PREFIX) and len(token) == TOKEN_LENGTH
    _, selector, secret = token.split(".")
    assert UUID(selector) == issued.credential_id
    assert len(base64.urlsafe_b64decode(secret + "=")) == 32  # 256 bits
    assert parse_edge_token(token) == (issued.credential_id, issued.secret_sha256)
    assert len(issued.secret_sha256) == 32 and secret.encode() not in issued.secret_sha256
    # The digest is bound to its selector: the same secret under another id does not match.
    assert credential_digest(uuid4(), secret) != issued.secret_sha256
    assert issue_edge_credential().token.get_secret_value() != token
    for rendered in (repr(issued), str(issued), repr(issued.token)):
        assert secret not in rendered and token not in rendered
    assert access.EDGE_TOKEN_PREFIX == TOKEN_PREFIX


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "vte1",
        "vte2." + str(uuid4()) + "." + "A" * 43,
        "vte1." + str(uuid4()).upper() + "." + "A" * 43,
        "vte1." + uuid4().hex + "...." + "A" * 43,
        "vte1." + str(uuid4()) + "." + "A" * 42,
        "vte1." + str(uuid4()) + "." + "A" * 44,
        "vte1." + str(uuid4()) + "." + "A" * 42 + "=",
        "vte1." + str(uuid4()) + "." + "A" * 42 + "B",  # non-canonical trailing bits
        "vte1." + str(uuid4()) + "." + "A" * 42 + "+",
        " vte1." + str(uuid4()) + "." + "A" * 43,
        "vte1." + str(uuid4()) + "." + "A" * 43 + "\n",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln",
        "x" * 10_000,
        None,
        12,
    ],
)
def test_malformed_tokens_are_refused_without_echoing_them(bad: Any) -> None:
    with pytest.raises(MalformedEdgeCredential) as caught:
        parse_edge_token(bad)
    assert str(caught.value) == "malformed edge credential"


def test_the_authorization_header_is_bounded_and_strict() -> None:
    token = issue_edge_credential().token.get_secret_value()
    assert bearer_value([f"Bearer {token}"]) == token
    assert bearer_value([f"bearer {token}"]) == token
    for headers in (
        [],
        [f"Bearer {token}", f"Bearer {token}"],
        [f"Basic {token}"],
        [f"Bearer  {token}"],
        [f"Bearer {token} "],
        ["Bearer"],
        ["Bearer " + "A" * 200],
    ):
        with pytest.raises(EdgeAuthenticationFailed) as caught:
            bearer_value(headers)
        assert token not in str(caught.value)


# ------------------------------------------------------------------ SQL boundary (runtime role)
async def _authenticate_sql(factory: async_sessionmaker[AsyncSession], token: str) -> list[Any]:
    credential_id, digest = parse_edge_token(token)
    async with factory() as session, session.begin():
        return list(
            (
                await session.execute(
                    text("SELECT * FROM authenticate_edge_node_credential(:id, :digest)"),
                    {"id": credential_id, "digest": digest},
                )
            ).all()
        )


async def test_the_authentication_function_returns_only_server_resolved_identity(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine, engine = make_engine(admin_settings), make_engine(settings)
    admin_factory, factory = make_session_factory(admin_engine), make_session_factory(engine)
    try:
        world = await seed_world(admin_factory, InMemoryCredentialVault())
        token = world.token.get_secret_value()
        [row] = await _authenticate_sql(factory, token)
        assert tuple(row) == (world.tenant_id, world.node_id, world.facility_id)
        assert set(row._mapping) == {"tenant_id", "edge_node_id", "facility_id"}
        async with admin_factory() as session, session.begin():
            await set_tenant(session, world.tenant_id)
            stored = await session.get(EdgeNodeCredential, world.credential_id)
            assert stored is not None and stored.last_used_at is not None
            # Plaintext is never persisted: neither the whole token nor the secret appears.
            secret = token.rsplit(".", 1)[1]
            raw_row = (
                await session.execute(
                    text("SELECT row_to_json(c)::text FROM edge_node_credentials c WHERE id = :id"),
                    {"id": world.credential_id},
                )
            ).scalar_one()
            assert secret not in raw_row and token not in raw_row
            assert stored.secret_sha256 != secret.encode()

        other = issue_edge_credential().token.get_secret_value()
        unknown_selector = other
        same_selector_other_secret = (
            f"{TOKEN_PREFIX}{world.credential_id}.{other.rsplit('.', 1)[1]}"
        )
        assert await _authenticate_sql(factory, unknown_selector) == []
        assert await _authenticate_sql(factory, same_selector_other_secret) == []
        async with factory() as session, session.begin():
            empty = (
                await session.execute(
                    text("SELECT * FROM authenticate_edge_node_credential(:id, :digest)"),
                    {"id": world.credential_id, "digest": b"\x00" * 31},
                )
            ).all()
            assert empty == []
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_the_runtime_role_cannot_read_or_write_credentials_directly(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine, engine = make_engine(admin_settings), make_engine(settings)
    try:
        world = await seed_world(make_session_factory(admin_engine), InMemoryCredentialVault())
        async with engine.connect() as connection:
            for statement in (
                "SELECT count(*) FROM edge_node_credentials",
                "UPDATE edge_node_credentials SET status = 'ACTIVE'",
                "DELETE FROM edge_node_credentials",
                "INSERT INTO edge_node_credentials (id, tenant_id, edge_node_id, secret_sha256, "
                "status) VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
                "decode(repeat('00', 32), 'hex'), 'ACTIVE')",
                "UPDATE edge_nodes SET status = 'ONLINE'",
                "UPDATE camera_assignments SET ended_at = NULL",
                "DELETE FROM camera_assignments",
                "INSERT INTO camera_assignments (id, tenant_id, camera_id, edge_node_id) VALUES "
                "(gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid())",
            ):
                with pytest.raises(DBAPIError) as raised:
                    async with connection.begin():
                        await set_tenant_connection(connection, world.tenant_id)
                        await connection.execute(text(statement))
                assert getattr(raised.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE, (
                    statement
                )
            # Read-only access to nodes and assignments, still under RLS.
            async with connection.begin():
                assert await connection.scalar(text("SELECT count(*) FROM edge_nodes")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM camera_assignments")) == 0
                await set_tenant_connection(connection, world.tenant_id)
                assert await connection.scalar(text("SELECT count(*) FROM edge_nodes")) == 1
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def set_tenant_connection(connection: Any, tenant_id: UUID) -> None:
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


# ------------------------------------------------------------------------ HTTP authentication
class RecordingVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, token: str) -> ExternalIdentity:
        self.calls += 1
        raise AssertionError("an edge credential must never reach the human verifier")


@asynccontextmanager
async def app_client(
    settings: Settings, admin_settings: Settings
) -> AsyncIterator[tuple[AsyncClient, Any, async_sessionmaker[AsyncSession], RecordingVerifier]]:
    admin_engine, engine = make_engine(admin_settings), make_engine(settings)
    factory, admin_factory = make_session_factory(engine), make_session_factory(admin_engine)
    vault = InMemoryCredentialVault()
    ring = FakeRing()
    http = httpx.AsyncClient(transport=httpx.MockTransport(ring.handler))
    world = await seed_world(admin_factory, vault)
    verifier = RecordingVerifier()
    app = create_app(
        settings,
        engine,
        identity_verifier=verifier,
        session_factory=factory,
        credential_vault=vault,
        ring_client=RingClient(settings, Secrets(), http),
        secret_resolver=Secrets(),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.ring = ring  # type: ignore[attr-defined]
            client.app = app  # type: ignore[attr-defined]
            yield client, world, admin_factory, verifier
    finally:
        await http.aclose()
        await engine.dispose()
        await admin_engine.dispose()


async def _release_probe(client: AsyncClient, header: str | None) -> httpx.Response:
    """DELETE of an unknown lease: 401 when unauthenticated, 404 when authenticated."""
    headers = {} if header is None else {"authorization": header}
    return await client.delete("/v1/edge/whep-leases/" + "A" * 43, headers=headers)


async def test_every_authentication_failure_is_the_same_generic_401(
    settings: Settings, admin_settings: Settings
) -> None:
    async with app_client(settings, admin_settings) as (client, world, admin_factory, _):
        token = world.token.get_secret_value()
        assert (await _release_probe(client, f"Bearer {token}")).status_code == 404

        revoked_id, revoked_token = await add_credential(
            admin_factory, world.tenant_id, world.node_id
        )
        async with admin_factory() as session, session.begin():
            await set_tenant(session, world.tenant_id)
            await session.execute(
                update(EdgeNodeCredential)
                .where(EdgeNodeCredential.id == revoked_id)
                .values(status="REVOKED", revoked_at=datetime.now(UTC))
            )
        other = issue_edge_credential().token.get_secret_value()
        attempts = {
            "missing": None,
            "wrong scheme": f"Basic {token}",
            "malformed": "Bearer vte1.not-a-credential",
            "jwt shaped": "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln",
            "unknown selector": f"Bearer {other}",
            "wrong secret": f"Bearer {TOKEN_PREFIX}{world.credential_id}.{other.rsplit('.', 1)[1]}",
            "revoked": f"Bearer {revoked_token.get_secret_value()}",
            "oversized": "Bearer " + "A" * 4096,
        }
        with capture_logs() as logs:
            responses = {
                name: await _release_probe(client, value) for name, value in attempts.items()
            }
        for name, response in responses.items():
            assert response.status_code == 401, name
            assert response.json() == {"detail": "authentication required"}, name
            assert response.headers["www-authenticate"] == "Bearer", name
        rendered = repr(logs)
        assert token not in rendered and other not in rendered
        assert revoked_token.get_secret_value() not in rendered
        assert other.rsplit(".", 1)[1] not in rendered
        assert all(
            set(entry) <= {"event", "reason", "log_level", "request_id"}
            for entry in logs
            if entry["event"] == "edge_authentication_failed"
        )


async def test_a_disabled_node_or_inactive_tenant_or_facility_fails_closed(
    settings: Settings, admin_settings: Settings
) -> None:
    async with app_client(settings, admin_settings) as (client, world, admin_factory, _):
        header = f"Bearer {world.token.get_secret_value()}"
        assert (await _release_probe(client, header)).status_code == 404
        for model, key, value, restore in (
            (EdgeNode, world.node_id, "DISABLED", "ONLINE"),
            (Facility, world.facility_id, "ARCHIVED", "ACTIVE"),
            (Tenant, world.tenant_id, "ARCHIVED", "ACTIVE"),
        ):
            async with admin_factory() as session, session.begin():
                await set_tenant(session, world.tenant_id)
                await session.execute(update(model).where(model.id == key).values(status=value))
            assert (await _release_probe(client, header)).status_code == 401, model
            offer = await client.post(
                f"/v1/edge/cameras/{world.camera_id}/whep",
                content=b"v=0\r\nm=video 9 RTP 96\r\n",
                headers={"authorization": header, "content-type": "application/sdp"},
            )
            assert offer.status_code == 401
            async with admin_factory() as session, session.begin():
                await set_tenant(session, world.tenant_id)
                await session.execute(update(model).where(model.id == key).values(status=restore))
            assert (await _release_probe(client, header)).status_code == 404
        assert client.ring.requests == []  # type: ignore[attr-defined]


async def test_an_edge_credential_never_authenticates_a_human_endpoint(
    settings: Settings, admin_settings: Settings
) -> None:
    async with app_client(settings, admin_settings) as (client, world, _, verifier):
        header = {"authorization": f"Bearer {world.token.get_secret_value()}"}
        for method, path in (
            ("GET", "/v1/me"),
            ("GET", "/v1/integrations/ring/connections"),
            ("GET", "/v1/integrations/ring/devices"),
        ):
            response = await client.request(method, path, headers=header)
            assert response.status_code == 401, path
        assert verifier.calls == 0


async def test_a_human_bearer_token_never_authenticates_an_edge_endpoint(
    settings: Settings, admin_settings: Settings
) -> None:
    async with app_client(settings, admin_settings) as (client, _, _, verifier):
        response = await _release_probe(client, "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln")
        assert response.status_code == 401
        assert verifier.calls == 0


async def test_a_database_outage_fails_closed(settings: Settings, admin_settings: Settings) -> None:
    async with app_client(settings, admin_settings) as (client, world, _, _):
        from sqlalchemy.exc import OperationalError

        class Broken:
            def __call__(self) -> Any:
                raise OperationalError("SELECT", {}, Exception("synthetic outage"))

        client.app.state.edge_authenticator._factory = Broken()  # type: ignore[attr-defined]
        response = await _release_probe(client, f"Bearer {world.token.get_secret_value()}")
        assert response.status_code == 503


async def test_revocation_takes_effect_for_the_next_request(
    settings: Settings, admin_settings: Settings
) -> None:
    async with app_client(settings, admin_settings) as (client, world, admin_factory, _):
        header = f"Bearer {world.token.get_secret_value()}"
        assert (await _release_probe(client, header)).status_code == 404
        from veotrex_api.edge_credentials import revoke_credential

        assert await revoke_credential(
            admin_factory, tenant_id=world.tenant_id, credential_id=world.credential_id
        )
        assert (await _release_probe(client, header)).status_code == 401
        async with admin_factory() as session, session.begin():
            await set_tenant(session, world.tenant_id)
            row = await session.scalar(
                select(EdgeNodeCredential).where(EdgeNodeCredential.id == world.credential_id)
            )
            assert row is not None and row.status == "REVOKED" and row.revoked_at is not None
