"""V1-00A-R1: the encrypted_credentials boundary.

The API runtime role holds no privilege on ``encrypted_credentials``. Every production path
(token receipt, claim, token retrieval, refresh/rotation, disconnect, remote removal) goes
through the four ``vault_credential_*`` SECURITY DEFINER functions, which authorize against the
pending-link state machine and the tenant-scoped connection. These tests run the REAL
``EncryptedCredentialVault`` and ``RingLinkService`` as the restricted role, with two tenants,
and prove that direct SQL, the function interface, and the service path all refuse to cross
tenants. All token values are synthetic and never printed.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests_ring_fakes import FakeRingClient, TestSecrets

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.config import Settings
from veotrex_api.credential_vault import (
    CredentialMaterial,
    CredentialVaultError,
    CredentialVersionConflict,
)
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.encrypted_vault import KEY_BYTES, EncryptedCredentialVault, VaultKeyProvider
from veotrex_api.ring_nonce import compute_ring_nonce
from veotrex_api.ring_repository import ConnectionState
from veotrex_api.ring_service import RingLinkError, RingLinkService, ring_credential_context
from veotrex_api.runtime_role import psycopg_dsn
from veotrex_api.secrets import SecretResolver

INSUFFICIENT_PRIVILEGE = "42501"


class KeyResolver:
    def __init__(self) -> None:
        self._value = base64.b64encode(os.urandom(KEY_BYTES)).decode()

    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr(self._value)


def vault_for(settings: Settings) -> tuple[EncryptedCredentialVault, object]:
    engine = make_engine(settings)
    resolver: SecretResolver = KeyResolver()
    return (
        EncryptedCredentialVault(
            make_session_factory(engine), VaultKeyProvider(resolver, "env:VEOTREX_VAULT_MASTER_KEY")
        ),
        engine,
    )


async def seed_tenant_with_claimed_link(
    admin_settings: Settings, label: str
) -> tuple[UUID, UUID, UUID]:
    """Tenant + CLAIMED pending link + connection referencing owner_id; returns
    (tenant_id, actor_id, owner_id). Credential rows are created by the vault under test."""
    tenant_id, actor_id, owner_id, connection_id = uuid4(), uuid4(), uuid4(), uuid4()
    engine = make_engine(admin_settings)
    try:
        async with engine.begin() as admin:
            await admin.execute(
                text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
                {"id": tenant_id, "name": f"Credential tenant {label}"},
            )
            await admin.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
            )
            await admin.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) "
                    "VALUES (:actor, :tenant, 'Owner', 'ACTIVE')"
                ),
                {"actor": actor_id, "tenant": tenant_id},
            )
            await admin.execute(
                text(
                    "INSERT INTO ring_pending_links (id, credential_secret_ref, "
                    "credential_generation, access_expires_at, state, ring_account_id, "
                    "claim_tenant_id, claim_actor_id, claimed_at) VALUES "
                    "(:id, :ref, 1, now() + interval '1 hour', 'CLAIMED', :account, "
                    ":tenant, :actor, now())"
                ),
                {
                    "id": owner_id,
                    "ref": f"vault://postgres/{uuid4()}",
                    "account": f"acct-{label}-{uuid4().hex}",
                    "tenant": tenant_id,
                    "actor": actor_id,
                },
            )
            await admin.execute(
                text(
                    "INSERT INTO camera_provider_connections "
                    "(id, tenant_id, name, provider_type, status, integration_state, "
                    "credential_owner_id, linked_by_actor_id, linked_at, access_expires_at, "
                    "credential_generation) VALUES (:id, :tenant, :name, 'RING', 'ACTIVE', "
                    "'ACTIVE', :owner, :actor, now(), now() + interval '1 hour', 1)"
                ),
                {
                    "id": connection_id,
                    "tenant": tenant_id,
                    "name": f"Ring {label}",
                    "owner": owner_id,
                    "actor": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, owner_id


def test_runtime_has_no_direct_access_to_encrypted_credentials(settings: Settings) -> None:
    with psycopg.connect(psycopg_dsn(settings.database_url.get_secret_value())) as connection:
        for statement in (
            "SELECT * FROM public.encrypted_credentials",
            "SELECT count(*) FROM public.encrypted_credentials",
            "INSERT INTO public.encrypted_credentials (id, provider, owner_kind, owner_id, "
            "nonce, ciphertext) VALUES (gen_random_uuid(), 'RING', 'ring_pending_link', "
            "gen_random_uuid(), '\\x000000000000000000000000', "
            "'\\x00000000000000000000000000000000')",
            "UPDATE public.encrypted_credentials SET version = version + 1",
            "DELETE FROM public.encrypted_credentials",
            "SELECT public.vault_credential_authorized"
            "('ring_pending_link', gen_random_uuid(), 'open')",
        ):
            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(statement)  # type: ignore[arg-type]
            except psycopg.Error as exc:
                assert exc.sqlstate == INSUFFICIENT_PRIVILEGE, statement
            else:
                raise AssertionError(f"not refused: {statement}")


async def test_cross_tenant_credential_is_unreachable_by_every_route(
    settings: Settings, admin_settings: Settings
) -> None:
    """Section 17: Tenant A cannot retrieve Tenant B's credential material or reference through
    direct SQL, the SECURITY DEFINER interface, or the application service path."""
    tenant_a, _, owner_a = await seed_tenant_with_claimed_link(admin_settings, "A")
    tenant_b, _, owner_b = await seed_tenant_with_claimed_link(admin_settings, "B")
    vault, engine = vault_for(settings)
    try:
        stored_a = await vault.store_new(
            ring_credential_context(owner_a, tenant_a),
            CredentialMaterial(SecretStr("synthetic-access-A"), SecretStr("synthetic-refresh-A")),
        )
        stored_b = await vault.store_new(
            ring_credential_context(owner_b, tenant_b),
            CredentialMaterial(SecretStr("synthetic-access-B"), SecretStr("synthetic-refresh-B")),
        )
        # Each tenant opens its own credential.
        opened_a = await vault.get(stored_a.secret_ref, ring_credential_context(owner_a, tenant_a))
        assert opened_a.material.access_token.get_secret_value() == "synthetic-access-A"
        opened_b = await vault.get(stored_b.secret_ref, ring_credential_context(owner_b, tenant_b))
        assert opened_b.material.access_token.get_secret_value() == "synthetic-access-B"

        # Approved interface, Tenant A context, Tenant B's reference and owner: unavailable.
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored_b.secret_ref, ring_credential_context(owner_b, tenant_a))
        # Tenant A context with its own owner id against B's reference: context mismatch,
        # which discloses nothing beyond what A already holds.
        with pytest.raises(CredentialVaultError, match="context mismatch"):
            await vault.get(stored_b.secret_ref, ring_credential_context(owner_a, tenant_a))
        # No tenant at all: unavailable.
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored_b.secret_ref, ring_credential_context(owner_b))
        # Rotation and deletion across tenants are refused the same way, and B is unchanged.
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.replace_if_version(
                stored_b.secret_ref,
                ring_credential_context(owner_b, tenant_a),
                1,
                CredentialMaterial(SecretStr("x"), SecretStr("y")),
            )
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.delete(stored_b.secret_ref, ring_credential_context(owner_b, tenant_a))
        still_b = await vault.get(stored_b.secret_ref, ring_credential_context(owner_b, tenant_b))
        assert still_b.version == 1

        # Function interface directly, as the runtime role, under Tenant A's context.
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_a)}
            )
            credential_b = UUID(stored_b.secret_ref.rsplit("/", 1)[1])
            outcome = await connection.scalar(
                text(
                    "SELECT outcome FROM vault_credential_open"
                    "(:id, 'RING', 'ring_pending_link', :owner)"
                ),
                {"id": credential_b, "owner": owner_b},
            )
            assert outcome == "unavailable"
            # Enumeration is impossible: no function takes anything but a primary key.
            with pytest.raises(DBAPIError) as raised:
                await connection.execute(text("SELECT id FROM encrypted_credentials"))
            assert getattr(raised.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


async def test_pre_tenant_lifecycle_is_bounded(
    settings: Settings, admin_settings: Settings
) -> None:
    """Token receipt stores before any tenant exists; clean-up may delete only while the link
    is still RECEIVED (or was never created). Once claimed, only the owning tenant may act."""
    vault, engine = vault_for(settings)
    admin_engine = make_engine(admin_settings)
    try:
        pending = uuid4()
        material = CredentialMaterial(SecretStr("synthetic-access"), SecretStr("synthetic-refresh"))
        stored = await vault.store_new(ring_credential_context(pending), material)
        # Orphan (no pending link yet): the failure path of token receipt may delete it.
        await vault.delete(stored.secret_ref, ring_credential_context(pending))
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored.secret_ref, ring_credential_context(pending))

        stored = await vault.store_new(ring_credential_context(pending), material)
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("SELECT create_ring_pending_link(:id, :ref, 1, now() + interval '1 hour')"),
                {"id": pending, "ref": stored.secret_ref},
            )
        # RECEIVED: nobody can open it (no tenant is entitled yet), but clean-up may delete.
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored.secret_ref, ring_credential_context(pending))
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored.secret_ref, ring_credential_context(pending, uuid4()))
        async with admin_engine.begin() as admin:
            assert await admin.scalar(
                text("SELECT complete_ring_pending_account(:id, :account)"),
                {"id": pending, "account": f"acct-{pending.hex}"},
            )
        # UNCLAIMED: not deletable by the pre-tenant path any more, not openable.
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.delete(stored.secret_ref, ring_credential_context(pending))
        tenant_id, actor_id = uuid4(), uuid4()
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Claimer', 'ACTIVE')"),
                {"id": tenant_id},
            )
            await admin.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
            )
            await admin.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) "
                    "VALUES (:actor, :tenant, 'Claimer', 'ACTIVE')"
                ),
                {"actor": actor_id, "tenant": tenant_id},
            )
            assert await admin.scalar(
                text("SELECT start_ring_pending_claim(:id, :tenant, :actor)"),
                {"id": pending, "tenant": tenant_id, "actor": actor_id},
            )
        # CLAIMING by tenant_id: exactly that tenant may open; another may not.
        opened = await vault.get(stored.secret_ref, ring_credential_context(pending, tenant_id))
        assert opened.material.refresh_token.get_secret_value() == "synthetic-refresh"
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored.secret_ref, ring_credential_context(pending, uuid4()))
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(stored.secret_ref, ring_credential_context(pending))
        # Even the claiming tenant cannot rotate or delete until a connection binds it.
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.delete(stored.secret_ref, ring_credential_context(pending, tenant_id))
    finally:
        await admin_engine.dispose()
        await engine.dispose()  # type: ignore[attr-defined]


async def test_full_ring_credential_workflow_through_the_real_vault(
    settings: Settings, admin_settings: Settings
) -> None:
    """Section 16: token receipt, account completion, claim, token retrieval, refresh/rotation
    with compare-and-swap, and disconnect/revoke - every step over the SECURITY DEFINER
    boundary as the runtime role, with the production vault implementation."""
    vault, engine = vault_for(settings)
    admin_engine = make_engine(admin_settings)
    factory = make_session_factory(engine)  # type: ignore[arg-type]
    client = FakeRingClient(f"ring-{uuid4().hex}")
    service = RingLinkService(settings, factory, vault, client, TestSecrets())  # type: ignore[arg-type]
    tenant_id, actor_id = uuid4(), uuid4()
    try:
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Workflow', 'ACTIVE')"),
                {"id": tenant_id},
            )
            await admin.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
            )
            await admin.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) "
                    "VALUES (:actor, :tenant, 'Owner', 'ACTIVE')"
                ),
                {"actor": actor_id, "tenant": tenant_id},
            )
        principal = AuthenticatedPrincipal(
            issuer="https://test.auth0.example/",
            subject=f"auth0|{actor_id.hex}",
            external_organization_id=f"org_{tenant_id.hex}",
            actor_id=actor_id,
            tenant_id=tenant_id,
            display_name="Owner",
            grants=(RoleGrant(Role.TENANT_OWNER, None),),
            permissions=frozenset(Permission),
        )
        assert await service.receive_authorization_code(SecretStr("code-workflow")) == "UNCLAIMED"
        timestamp = int(datetime.now(UTC).timestamp() * 1000)
        nonce = compute_ring_nonce(timestamp, client.account_id, "test-hmac-key")
        result = await service.claim(
            principal, timestamp_ms=timestamp, nonce=nonce, request_id="r1"
        )
        assert result.state is ConnectionState.ACTIVE
        token = await service.get_valid_access_token(tenant_id, result.connection_id)
        assert token.get_secret_value() == "access-1"
        # Force a rotation: the vault's compare-and-swap replaces version 1 with 2.
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
            )
            await admin.execute(
                text(
                    "UPDATE camera_provider_connections SET access_expires_at = "
                    ":past WHERE id = :id"
                ),
                {"past": datetime.now(UTC) - timedelta(seconds=1), "id": result.connection_id},
            )
        rotated = await service.get_valid_access_token(tenant_id, result.connection_id)
        assert rotated.get_secret_value() == "access-2" and client.refresh_calls == 1
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
            )
            row = (
                await admin.execute(
                    text(
                        "SELECT secret_ref, credential_owner_id, credential_generation "
                        "FROM camera_provider_connections WHERE id = :id"
                    ),
                    {"id": result.connection_id},
                )
            ).one()
        assert row.credential_generation == 2
        context = ring_credential_context(row.credential_owner_id, tenant_id)
        with pytest.raises(CredentialVersionConflict):
            await vault.replace_if_version(
                row.secret_ref, context, 1, CredentialMaterial(SecretStr("s"), SecretStr("t"))
            )
        # Another tenant cannot retrieve this connection's token through the service path.
        with pytest.raises(RingLinkError, match="credential_unavailable"):
            await service.get_valid_access_token(uuid4(), result.connection_id)
        # Disconnect revokes the credential; the row is gone for everyone, including the owner.
        await service.disconnect(principal, result.connection_id, "r2")
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(row.secret_ref, context)
        async with admin_engine.begin() as admin:
            assert (
                await admin.scalar(
                    text("SELECT count(*) FROM encrypted_credentials WHERE id = :id"),
                    {"id": UUID(row.secret_ref.rsplit("/", 1)[1])},
                )
                == 0
            )
    finally:
        await admin_engine.dispose()
        await engine.dispose()  # type: ignore[attr-defined]
