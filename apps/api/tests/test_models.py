from sqlalchemy import ForeignKeyConstraint

from veotrex_api.models import (
    RLS_TENANT_TABLES,
    TENANT_OWNED_TABLES,
    ActorIdentity,
    Area,
    AuditEvent,
    Camera,
    RoleAssignment,
)


def composite_foreign_keys(model: type[object]) -> list[ForeignKeyConstraint]:
    return [
        constraint
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, ForeignKeyConstraint) and len(constraint.columns) == 2
    ]


def test_nested_domain_models_enforce_same_tenant_relationships() -> None:
    assert composite_foreign_keys(Area)
    assert len(composite_foreign_keys(Camera)) == 2


def test_audit_actor_relationship_is_restrictive_and_tenant_scoped() -> None:
    constraints = composite_foreign_keys(AuditEvent)
    assert len(constraints) == 1
    assert constraints[0].ondelete == "RESTRICT"


def test_identity_and_roles_have_composite_tenant_foreign_keys() -> None:
    assert len(composite_foreign_keys(ActorIdentity)) == 1
    assert len(composite_foreign_keys(RoleAssignment)) == 3


def test_all_expected_customer_tables_are_tenant_owned() -> None:
    assert set(TENANT_OWNED_TABLES) == {
        "facilities",
        "areas",
        "zones",
        "camera_provider_connections",
        "cameras",
        "edge_nodes",
        "camera_assignments",
        "actors",
        "tenant_identity_bindings",
        "actor_identities",
        "role_assignments",
        "audit_events",
    }


def test_pre_context_binding_is_the_only_tenant_owned_table_outside_rls() -> None:
    assert set(TENANT_OWNED_TABLES) - set(RLS_TENANT_TABLES) == {"tenant_identity_bindings"}
