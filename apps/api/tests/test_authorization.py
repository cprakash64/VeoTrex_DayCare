from uuid import uuid4

import pytest
from fastapi import HTTPException

from veotrex_api.access import (
    AuthenticatedPrincipal,
    AuthenticationFailureLogLimiter,
    PrincipalContext,
    require_permission,
)
from veotrex_api.authorization import Permission, Role, RoleGrant, has_permission, permissions_for


def test_role_permission_mapping_is_explicit_and_least_privilege() -> None:
    owner = (RoleGrant(Role.TENANT_OWNER, None),)
    reviewer = (RoleGrant(Role.SAFETY_REVIEWER, None),)
    viewer = (RoleGrant(Role.VIEWER, None),)

    assert Permission.MANAGE_INTEGRATIONS in permissions_for(owner)
    assert has_permission(reviewer, Permission.REVIEW_SAFETY_DATA)
    assert not has_permission(reviewer, Permission.MANAGE_INTEGRATIONS)
    assert permissions_for(viewer) == frozenset({Permission.READ_OPERATIONAL})


def test_facility_scoped_permission_does_not_cross_facilities() -> None:
    assigned_facility = uuid4()
    other_facility = uuid4()
    grants = (RoleGrant(Role.FACILITY_ADMIN, assigned_facility),)

    assert has_permission(grants, Permission.ADMINISTER_FACILITY, assigned_facility)
    assert not has_permission(grants, Permission.ADMINISTER_FACILITY, other_facility)


def test_http_permission_dependency_cannot_bypass_role_mapping() -> None:
    actor_id, tenant_id = uuid4(), uuid4()
    grants = (RoleGrant(Role.VIEWER, None),)
    principal = AuthenticatedPrincipal(
        issuer="https://idp.example/",
        subject="subject",
        external_organization_id="organization",
        actor_id=actor_id,
        tenant_id=tenant_id,
        display_name=None,
        grants=grants,
        permissions=permissions_for(grants),
    )
    context = PrincipalContext(principal=principal, session=None)  # type: ignore[arg-type]

    assert require_permission(Permission.READ_OPERATIONAL)(context) is context
    with pytest.raises(HTTPException) as denied:
        require_permission(Permission.MANAGE_INTEGRATIONS)(context)
    assert denied.value.status_code == 403


def test_authentication_failure_logging_is_bounded() -> None:
    limiter = AuthenticationFailureLogLimiter(limit=2, window_seconds=60)
    assert limiter.allow()
    assert limiter.allow()
    assert not limiter.allow()
