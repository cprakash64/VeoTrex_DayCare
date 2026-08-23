from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from veotrex_api.authorization import Role
from veotrex_api.config import Settings
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.models import ActorIdentity, AuditEvent, RoleAssignment, TenantIdentityBinding
from veotrex_api.provisioning import (
    BootstrapOwnerRequest,
    ProvisioningError,
    bootstrap_owner,
    change_role_assignment,
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
    settings: Settings,
) -> None:
    engine = make_engine(settings)
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


async def test_bootstrap_dry_run_and_invalid_inputs_make_no_changes(settings: Settings) -> None:
    engine = make_engine(settings)
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


async def test_role_change_and_revocation_are_audited(settings: Settings) -> None:
    engine = make_engine(settings)
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
