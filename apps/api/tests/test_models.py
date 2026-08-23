from sqlalchemy import ForeignKeyConstraint

from veotrex_api.models import TENANT_OWNED_TABLES, Area, AuditEvent, Camera


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


def test_all_expected_customer_tables_are_marked_for_rls() -> None:
    assert set(TENANT_OWNED_TABLES) == {
        "facilities",
        "areas",
        "zones",
        "camera_provider_connections",
        "cameras",
        "edge_nodes",
        "camera_assignments",
        "actors",
        "audit_events",
    }
