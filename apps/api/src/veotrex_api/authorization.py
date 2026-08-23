from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    TENANT_OWNER = "TENANT_OWNER"
    FACILITY_ADMIN = "FACILITY_ADMIN"
    SAFETY_REVIEWER = "SAFETY_REVIEWER"
    VIEWER = "VIEWER"


class Permission(StrEnum):
    READ_OPERATIONAL = "read:operational"
    MANAGE_TENANT_CONFIGURATION = "manage:tenant-configuration"
    MANAGE_FACILITIES = "manage:facilities"
    MANAGE_INTEGRATIONS = "manage:integrations"
    MANAGE_MEMBERS = "manage:members"
    ADMINISTER_FACILITY = "administer:facility"
    CONFIGURE_FACILITY_CAMERAS = "configure:facility-cameras"
    REVIEW_SAFETY_DATA = "review:safety-data"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.TENANT_OWNER: frozenset(Permission),
    Role.FACILITY_ADMIN: frozenset(
        {
            Permission.READ_OPERATIONAL,
            Permission.ADMINISTER_FACILITY,
            Permission.CONFIGURE_FACILITY_CAMERAS,
            Permission.REVIEW_SAFETY_DATA,
        }
    ),
    Role.SAFETY_REVIEWER: frozenset({Permission.READ_OPERATIONAL, Permission.REVIEW_SAFETY_DATA}),
    Role.VIEWER: frozenset({Permission.READ_OPERATIONAL}),
}


@dataclass(frozen=True, slots=True)
class RoleGrant:
    role: Role
    facility_id: UUID | None


def permissions_for(grants: tuple[RoleGrant, ...]) -> frozenset[Permission]:
    return frozenset(permission for grant in grants for permission in ROLE_PERMISSIONS[grant.role])


def has_permission(
    grants: tuple[RoleGrant, ...], permission: Permission, facility_id: UUID | None = None
) -> bool:
    for grant in grants:
        if permission not in ROLE_PERMISSIONS[grant.role]:
            continue
        if facility_id is None or grant.facility_id is None or grant.facility_id == facility_id:
            return True
    return False
