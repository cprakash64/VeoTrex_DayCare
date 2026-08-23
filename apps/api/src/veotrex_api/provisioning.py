from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from veotrex_api.authorization import Role
from veotrex_api.identity import AUTH0_PROVIDER
from veotrex_api.models import (
    Actor,
    ActorIdentity,
    AuditEvent,
    RoleAssignment,
    Tenant,
    TenantIdentityBinding,
)

TENANT_BINDING_CREATED = "identity.tenant_binding.created"
ACTOR_BINDING_CREATED = "identity.actor_binding.created"
ROLE_ASSIGNMENT_CREATED = "authorization.role.created"
ROLE_ASSIGNMENT_CHANGED = "authorization.role.changed"
ROLE_ASSIGNMENT_REVOKED = "authorization.role.revoked"


class ProvisioningError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapOwnerRequest:
    tenant_id: UUID
    issuer: str
    external_organization_id: str
    subject: str
    display_name: str | None
    request_id: str


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID,
    request_id: str,
    metadata: dict[str, str] | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            request_id=request_id,
            metadata_=metadata or {},
        )
    )


async def bootstrap_owner(
    session: AsyncSession, request: BootstrapOwnerRequest, *, dry_run: bool = False
) -> UUID | None:
    tenant = await session.get(Tenant, request.tenant_id)
    if tenant is None or tenant.status != "ACTIVE":
        raise ProvisioningError("tenant does not exist or is not active")
    if not request.issuer.startswith("https://") or not request.issuer.endswith("/"):
        raise ProvisioningError("issuer must be an exact HTTPS issuer ending in '/'")

    # This CLI requires a deployment-admin database credential. These global checks
    # intentionally run before tenant RLS context and refuse all ambiguous bindings.
    organization_exists = await session.scalar(
        select(TenantIdentityBinding.id).where(
            TenantIdentityBinding.provider == AUTH0_PROVIDER,
            TenantIdentityBinding.issuer == request.issuer,
            TenantIdentityBinding.external_organization_id == request.external_organization_id,
        )
    )
    subject_exists = await session.scalar(
        select(ActorIdentity.id).where(
            ActorIdentity.provider == AUTH0_PROVIDER,
            ActorIdentity.issuer == request.issuer,
            ActorIdentity.subject == request.subject,
            ActorIdentity.tenant_id == request.tenant_id,
        )
    )
    if organization_exists is not None or subject_exists is not None:
        raise ProvisioningError("organization or subject binding already exists; no changes made")
    if dry_run:
        return None

    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(request.tenant_id)},
    )
    actor_id = uuid4()
    actor = Actor(
        id=actor_id,
        tenant_id=request.tenant_id,
        display_name=request.display_name,
        status="ACTIVE",
    )
    tenant_binding = TenantIdentityBinding(
        tenant_id=request.tenant_id,
        provider=AUTH0_PROVIDER,
        issuer=request.issuer,
        external_organization_id=request.external_organization_id,
    )
    actor_identity = ActorIdentity(
        tenant_id=request.tenant_id,
        actor_id=actor_id,
        provider=AUTH0_PROVIDER,
        issuer=request.issuer,
        subject=request.subject,
    )
    assignment = RoleAssignment(
        tenant_id=request.tenant_id,
        actor_id=actor_id,
        role=Role.TENANT_OWNER.value,
        created_by_actor_id=actor_id,
    )
    session.add_all([actor, tenant_binding, actor_identity, assignment])
    await session.flush()
    await _audit(
        session,
        tenant_id=request.tenant_id,
        actor_id=actor_id,
        action=TENANT_BINDING_CREATED,
        target_type="tenant_identity_binding",
        target_id=tenant_binding.id,
        request_id=request.request_id,
        metadata={"provider": AUTH0_PROVIDER},
    )
    await _audit(
        session,
        tenant_id=request.tenant_id,
        actor_id=actor_id,
        action=ACTOR_BINDING_CREATED,
        target_type="actor_identity",
        target_id=actor_identity.id,
        request_id=request.request_id,
        metadata={"provider": AUTH0_PROVIDER},
    )
    await _audit(
        session,
        tenant_id=request.tenant_id,
        actor_id=actor_id,
        action=ROLE_ASSIGNMENT_CREATED,
        target_type="role_assignment",
        target_id=assignment.id,
        request_id=request.request_id,
        metadata={"role": Role.TENANT_OWNER.value},
    )
    return actor_id


async def change_role_assignment(
    session: AsyncSession,
    assignment: RoleAssignment,
    new_role: Role,
    administrator_actor_id: UUID,
    request_id: str,
) -> RoleAssignment:
    old_role = assignment.role
    assignment.archived_at = datetime.now(UTC)
    replacement = RoleAssignment(
        tenant_id=assignment.tenant_id,
        actor_id=assignment.actor_id,
        role=new_role.value,
        facility_id=assignment.facility_id,
        created_by_actor_id=administrator_actor_id,
    )
    session.add(replacement)
    await session.flush()
    await _audit(
        session,
        tenant_id=assignment.tenant_id,
        actor_id=administrator_actor_id,
        action=ROLE_ASSIGNMENT_CHANGED,
        target_type="role_assignment",
        target_id=replacement.id,
        request_id=request_id,
        metadata={
            "old_role": old_role,
            "new_role": new_role.value,
            "previous_assignment_id": str(assignment.id),
        },
    )
    return replacement


async def revoke_role_assignment(
    session: AsyncSession,
    assignment: RoleAssignment,
    administrator_actor_id: UUID,
    request_id: str,
) -> None:
    await session.execute(
        update(RoleAssignment)
        .where(
            RoleAssignment.id == assignment.id,
            RoleAssignment.tenant_id == assignment.tenant_id,
            RoleAssignment.archived_at.is_(None),
        )
        .values(archived_at=text("now()"))
    )
    await _audit(
        session,
        tenant_id=assignment.tenant_id,
        actor_id=administrator_actor_id,
        action=ROLE_ASSIGNMENT_REVOKED,
        target_type="role_assignment",
        target_id=assignment.id,
        request_id=request_id,
        metadata={"role": assignment.role},
    )
