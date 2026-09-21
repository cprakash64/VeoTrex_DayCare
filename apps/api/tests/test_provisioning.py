from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from veotrex_api.authorization import Role
from veotrex_api.config import Settings

# Provisioning is the privileged, non-public CLI path (veotrex-provision). It runs as the
# deployment admin identity by design - never as the API runtime role - so every test here
# uses ``admin_settings``.
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.models import (
    ActorIdentity,
    AuditEvent,
    RoleAssignment,
    Tenant,
    TenantIdentityBinding,
)
from veotrex_api.provisioning import (
    TENANT_CREATED,
    BootstrapOwnerRequest,
    CreateTenantRequest,
    ProvisioningError,
    bootstrap_owner,
    change_role_assignment,
    ensure_tenant,
    revoke_role_assignment,
)


def request(tenant_id, *, issuer: str = "https://tenant.auth0.example/"):
    return BootstrapOwnerRequest(
        tenant_id=tenant_id,
        issuer=issuer,
        external_organization_id=f"org_{uuid4().hex}",
        subject=f"auth0|{uuid4().hex}",
        display_name="Initial owner",
        request_id=f"test:{uuid4()}",
    )


async def test_bootstrap_owner_is_atomic_audited_and_refuses_duplicates(
    admin_settings: Settings,
) -> None:
    engine = make_engine(admin_settings)
    factory = make_session_factory(engine)
    tenant_id = uuid4()
    bootstrap_request = request(tenant_id)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) "
                    "VALUES (:id, 'Provisioned tenant', 'ACTIVE')"
                ),
                {"id": tenant_id},
            )
        async with factory() as session, session.begin():
            actor_id = await bootstrap_owner(session, bootstrap_request)
        assert actor_id is not None

        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActorIdentity)
                    .where(ActorIdentity.tenant_id == tenant_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TenantIdentityBinding)
                    .where(TenantIdentityBinding.tenant_id == tenant_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignment)
                    .where(RoleAssignment.tenant_id == tenant_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.tenant_id == tenant_id)
                )
                == 3
            )

        with pytest.raises(ProvisioningError, match="already exists"):
            async with factory() as session, session.begin():
                await bootstrap_owner(session, bootstrap_request)
    finally:
        await engine.dispose()


async def test_bootstrap_dry_run_and_invalid_inputs_make_no_changes(
    admin_settings: Settings,
) -> None:
    engine = make_engine(admin_settings)
    factory = make_session_factory(engine)
    tenant_id = uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) "
                    "VALUES (:id, 'Dry run tenant', 'ACTIVE')"
                ),
                {"id": tenant_id},
            )
        async with factory() as session, session.begin():
            assert await bootstrap_owner(session, request(tenant_id), dry_run=True) is None

        with pytest.raises(ProvisioningError, match="HTTPS issuer"):
            async with factory() as session, session.begin():
                await bootstrap_owner(session, request(tenant_id, issuer="http://unsafe.example/"))
        with pytest.raises(ProvisioningError, match="does not exist"):
            async with factory() as session, session.begin():
                await bootstrap_owner(session, request(uuid4()))
    finally:
        await engine.dispose()


async def test_role_change_and_revocation_are_audited(admin_settings: Settings) -> None:
    engine = make_engine(admin_settings)
    factory = make_session_factory(engine)
    tenant_id = uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) "
                    "VALUES (:id, 'Role audit tenant', 'ACTIVE')"
                ),
                {"id": tenant_id},
            )
        async with factory() as session, session.begin():
            actor_id = await bootstrap_owner(session, request(tenant_id))
        assert actor_id is not None

        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            assignment = await session.scalar(
                select(RoleAssignment).where(RoleAssignment.actor_id == actor_id)
            )
            assert assignment is not None
            replacement = await change_role_assignment(
                session, assignment, Role.VIEWER, actor_id, f"test:{uuid4()}"
            )
            await revoke_role_assignment(session, replacement, actor_id, f"test:{uuid4()}")

        async with factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            assignments = (
                await session.scalars(
                    select(RoleAssignment).where(RoleAssignment.actor_id == actor_id)
                )
            ).all()
            assert len(assignments) == 2
            assert {assignment.role for assignment in assignments} == {
                Role.TENANT_OWNER.value,
                Role.VIEWER.value,
            }
            assert all(assignment.archived_at is not None for assignment in assignments)
            actions = set((await session.scalars(select(AuditEvent.action))).all())
            assert "authorization.role.changed" in actions
            assert "authorization.role.revoked" in actions
    finally:
        await engine.dispose()


def tenant_request(tenant_id, name: str):
    return CreateTenantRequest(tenant_id=tenant_id, name=name, request_id=f"test:{uuid4()}")


async def test_create_tenant_is_idempotent_and_refuses_conflicts(admin_settings: Settings) -> None:
    """The step that must precede bootstrap_owner on a deployment with no tenants yet."""
    engine = make_engine(admin_settings)
    factory = make_session_factory(engine)
    tenant_id = uuid4()
    name = f"Staging Daycare {uuid4().hex[:8]}"
    try:
        async with factory() as session, session.begin():
            assert await ensure_tenant(session, tenant_request(tenant_id, name), dry_run=True)

        # A dry run must leave the table exactly as it found it, not merely return early.
        async with factory() as session:
            assert await session.get(Tenant, tenant_id) is None
            assert (
                await session.scalar(
                    select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.target_id == tenant_id)
                )
                == 0
            )

        async with factory() as session, session.begin():
            assert await ensure_tenant(session, tenant_request(tenant_id, name)) is True

        # Re-running the identical request changes nothing and does not raise.
        async with factory() as session, session.begin():
            assert await ensure_tenant(session, tenant_request(tenant_id, name)) is False

        async with factory() as session:
            created = await session.get(Tenant, tenant_id)
            assert created is not None
            assert created.name == name
            assert created.status == "ACTIVE"
            audited = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.target_id == tenant_id, AuditEvent.action == TENANT_CREATED)
            )
            assert audited == 1

        # The same id under a different name is a mistake, not something to reconcile.
        with pytest.raises(ProvisioningError):
            async with factory() as session, session.begin():
                await ensure_tenant(session, tenant_request(tenant_id, "A Different Name"))

        for invalid in ("", "   ", "x" * 201):
            with pytest.raises(ProvisioningError):
                async with factory() as session, session.begin():
                    await ensure_tenant(session, tenant_request(uuid4(), invalid))

        # The whole point: bootstrap_owner now has an ACTIVE tenant to bind to.
        async with factory() as session, session.begin():
            actor_id = await bootstrap_owner(session, request(tenant_id))
        assert actor_id is not None

        async with factory() as session:
            owners = await session.scalar(
                select(func.count())
                .select_from(RoleAssignment)
                .where(
                    RoleAssignment.tenant_id == tenant_id,
                    RoleAssignment.role == Role.TENANT_OWNER.value,
                )
            )
            assert owners == 1
    finally:
        await engine.dispose()


async def test_create_tenant_rolls_back_completely_on_failure(admin_settings: Settings) -> None:
    """A transaction that fails after the insert must leave no tenant and no audit row.

    ensure_tenant writes the tenant and its audit event in one transaction owned by the
    caller. If a later step in that transaction fails, a partially provisioned tenant -
    present but never bound to an organization or an owner - would be invisible to the
    provisioner's own conflict checks on the next run.
    """
    engine = make_engine(admin_settings)
    factory = make_session_factory(engine)
    tenant_id = uuid4()
    name = f"Rollback Daycare {uuid4().hex[:8]}"
    try:
        with pytest.raises(RuntimeError, match="induced"):
            async with factory() as session, session.begin():
                assert await ensure_tenant(session, tenant_request(tenant_id, name)) is True
                raise RuntimeError("induced failure after the insert")

        async with factory() as session:
            assert await session.get(Tenant, tenant_id) is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.target_id == tenant_id)
                )
                == 0
            )

        # The id is still free, so provisioning can simply be retried.
        async with factory() as session, session.begin():
            assert await ensure_tenant(session, tenant_request(tenant_id, name)) is True
    finally:
        await engine.dispose()
