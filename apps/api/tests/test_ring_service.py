import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from tests_ring_fakes import FakeRingClient, TestSecrets

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.config import Settings
from veotrex_api.credential_vault import (
    CredentialContext,
    CredentialMaterial,
    CredentialVaultError,
    InMemoryCredentialVault,
)
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.models import AuditEvent, CameraProviderConnection, RingPendingLink
from veotrex_api.ring_client import RingAmbiguousResult, RingClientError
from veotrex_api.ring_nonce import compute_ring_nonce
from veotrex_api.ring_repository import ConnectionState
from veotrex_api.ring_service import RingLinkError, RingLinkService, TokenReceiptState


async def create_actor(factory, *, role: Role = Role.TENANT_OWNER) -> AuthenticatedPrincipal:
    """Seed a tenant and owner. ``factory`` must be the ADMIN session factory."""
    tenant_id, actor_id = uuid4(), uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Safe tenant {tenant_id.hex[:8]}"},
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
                "INSERT INTO role_assignments (id, tenant_id, actor_id, role) "
                "VALUES (:id, :tenant, :actor, :role)"
            ),
            {"id": uuid4(), "tenant": tenant_id, "actor": actor_id, "role": role.value},
        )
    grants = (RoleGrant(role, None),)
    permissions = (
        frozenset(Permission)
        if role is Role.TENANT_OWNER
        else frozenset({Permission.READ_OPERATIONAL})
    )
    return AuthenticatedPrincipal(
        issuer="https://test.auth0.example/",
        subject=f"auth0|{actor_id.hex}",
        external_organization_id=f"org_{tenant_id.hex}",
        actor_id=actor_id,
        tenant_id=tenant_id,
        display_name="Owner",
        grants=grants,
        permissions=permissions,
    )


def service(settings: Settings, factory, vault, client: FakeRingClient) -> RingLinkService:
    return RingLinkService(settings, factory, vault, client, TestSecrets())  # type: ignore[arg-type]


async def test_successful_claim_is_atomic_audited_and_replay_safe(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    vault = InMemoryCredentialVault()
    client = FakeRingClient(f"ring-{uuid4().hex}")
    subject = service(settings, factory, vault, client)
    principal = await create_actor(make_session_factory(admin_engine))
    try:
        assert await subject.receive_authorization_code(SecretStr("code-success")) == "UNCLAIMED"
        timestamp = int(datetime.now(UTC).timestamp() * 1000)
        nonce = compute_ring_nonce(timestamp, client.account_id, "test-hmac-key")
        preview = await subject.link_context(principal, timestamp, nonce)
        assert preview.eligible
        assert preview.tenant_name.startswith("Safe tenant")

        result = await subject.claim(
            principal, timestamp_ms=timestamp, nonce=nonce, request_id="claim-request"
        )
        assert result.state is ConnectionState.ACTIVE
        assert client.confirm_calls == client.complete_calls == 1
        # V1-01A-2: both App Integrations calls carry the masked partner identifier derived from
        # the signed-in principal ("Owner" -> "O***r@veotrex"), never a raw name or subject.
        assert client.account_identifiers == ["O***r@veotrex", "O***r@veotrex"]
        with pytest.raises(RingLinkError, match="invalid_or_expired_link"):
            await subject.claim(principal, timestamp_ms=timestamp, nonce=nonce, request_id="replay")

        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(principal.tenant_id)},
            )
            connection = await session.get(CameraProviderConnection, result.connection_id)
            assert connection is not None
            assert connection.integration_state == "ACTIVE"
            assert connection.secret_ref and connection.secret_ref.startswith("vault://")
            assert connection.external_account_id == client.account_id
            audits = (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.target_id == result.connection_id)
                )
            ).all()
            assert [event.action for event in audits] == [
                "integration.ring.claimed",
                "integration.ring.activated",
            ]
            serialized = repr([event.metadata_ for event in audits])
            assert "access-" not in serialized and "refresh-" not in serialized
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_permission_patch_recovery_and_cross_tenant_conflict(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    vault = InMemoryCredentialVault()
    account_id = f"ring-{uuid4().hex}"
    client = FakeRingClient(account_id)
    subject = service(settings, factory, vault, client)
    owner = await create_actor(admin_factory)
    viewer = await create_actor(admin_factory, role=Role.VIEWER)
    try:
        await subject.receive_authorization_code(SecretStr("code-first"))
        timestamp = int(datetime.now(UTC).timestamp() * 1000)
        nonce = compute_ring_nonce(timestamp, account_id, "test-hmac-key")
        with pytest.raises(RingLinkError, match="access_denied"):
            await subject.claim(viewer, timestamp_ms=timestamp, nonce=nonce, request_id="viewer")

        client.complete_error = RingClientError("patch", "provider_rejected", 400)
        result = await subject.claim(
            owner, timestamp_ms=timestamp, nonce=nonce, request_id="partial"
        )
        assert result.state is ConnectionState.CONFIGURING
        client.complete_error = None
        resumed = await subject.resume_completion(owner, result.connection_id, "resume")
        assert resumed.state is ConnectionState.ACTIVE

        other_owner = await create_actor(admin_factory)
        await subject.receive_authorization_code(SecretStr("code-second"))
        second_time = int(datetime.now(UTC).timestamp() * 1000)
        second_nonce = compute_ring_nonce(second_time, account_id, "test-hmac-key")
        with pytest.raises(RingLinkError, match="connection_conflict"):
            await subject.claim(
                other_owner,
                timestamp_ms=second_time,
                nonce=second_nonce,
                request_id="cross-tenant",
            )
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_users_me_failure_is_safely_retained_without_personal_data(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    vault = InMemoryCredentialVault()
    client = FakeRingClient("unused")
    client.users_error = RingClientError("users_me", "provider_unavailable", 503)
    subject = service(settings, factory, vault, client)
    try:
        state = await subject.receive_authorization_code(SecretStr("code-users-failure"))
        assert state is TokenReceiptState.ACCOUNT_LOOKUP_PENDING
        # ring_pending_links is reachable only through SECURITY DEFINER functions; inspecting
        # the raw row is an admin-only view.
        async with make_session_factory(admin_engine)() as session:
            pending = await session.scalar(
                select(RingPendingLink).where(
                    RingPendingLink.last_failure_category == "provider_unavailable"
                )
            )
            assert pending is not None
            assert pending.state == "RECEIVED"
            assert pending.ring_account_id is None
            assert "access-1" not in repr(pending.__dict__)
            assert "refresh-1" not in repr(pending.__dict__)
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_refresh_rotation_is_single_writer_and_ambiguous_state_commits(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    vault = InMemoryCredentialVault()
    client = FakeRingClient(f"ring-{uuid4().hex}")
    subject = service(settings, factory, vault, client)
    principal = await create_actor(make_session_factory(admin_engine))
    owner_id, connection_id = uuid4(), uuid4()
    context = CredentialContext("RING", "ring_pending_link", owner_id)
    credential = await vault.store_new(
        context, CredentialMaterial(SecretStr("access-1"), SecretStr("refresh-1"))
    )
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(principal.tenant_id)},
            )
            session.add(
                CameraProviderConnection(
                    id=connection_id,
                    tenant_id=principal.tenant_id,
                    name=f"Ring refresh {connection_id.hex[:8]}",
                    provider_type="RING",
                    secret_ref=credential.secret_ref,
                    credential_owner_id=owner_id,
                    status="ACTIVE",
                    external_account_id=client.account_id,
                    integration_state="ACTIVE",
                    linked_by_actor_id=principal.actor_id,
                    linked_at=datetime.now(UTC),
                    access_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    credential_generation=1,
                )
            )
        values = await asyncio.gather(
            subject.get_valid_access_token(principal.tenant_id, connection_id),
            subject.get_valid_access_token(principal.tenant_id, connection_id),
        )
        assert client.refresh_calls == 1
        assert {value.get_secret_value() for value in values} == {"access-2"}
        assert (await vault.get(credential.secret_ref, context)).version == 2
        assert await subject.due_for_proactive_refresh(principal.tenant_id) == ()

        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(principal.tenant_id)},
            )
            connection = await session.get(CameraProviderConnection, connection_id)
            assert connection is not None
            connection.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        client.refresh_error = RingAmbiguousResult("refresh", "transport_failure")
        with pytest.raises(RingLinkError, match="refresh_uncertain"):
            await subject.get_valid_access_token(principal.tenant_id, connection_id)
        assert client.refresh_calls == 2
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(principal.tenant_id)},
            )
            state = await session.scalar(
                select(CameraProviderConnection.integration_state).where(
                    CameraProviderConnection.id == connection_id
                )
            )
            assert state == "REFRESH_UNCERTAIN"
        await subject.disconnect(principal, connection_id, "disconnect")
        with pytest.raises(CredentialVaultError):
            await vault.get(credential.secret_ref, context)
        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(principal.tenant_id)},
            )
            disconnected = await session.get(CameraProviderConnection, connection_id)
            assert disconnected is not None
            assert disconnected.integration_state == "DISCONNECTED"
            assert disconnected.status == "DISABLED"
            assert disconnected.secret_ref is None
    finally:
        await engine.dispose()
        await admin_engine.dispose()
