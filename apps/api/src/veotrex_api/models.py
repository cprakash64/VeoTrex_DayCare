from datetime import date, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[object, object]] = {dict[str, Any]: JSON}


class IdMixin:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TenantOwnedMixin:
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )


class Tenant(Base, IdMixin, TimestampMixin):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_tenants_status"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class Facility(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "facilities"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_facilities_id_tenant"),
        UniqueConstraint("tenant_id", "name", name="uq_facilities_tenant_name"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_facilities_status"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(16), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class Area(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "areas"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_areas_id_tenant"),
        UniqueConstraint("tenant_id", "facility_id", "name", name="uq_areas_facility_name"),
        ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_areas_facility_tenant",
        ),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_areas_status"),
    )

    facility_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, default="ROOM")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class Zone(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "zones"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_zones_id_tenant"),
        UniqueConstraint("tenant_id", "area_id", "name", name="uq_zones_area_name"),
        ForeignKeyConstraint(
            ["area_id", "tenant_id"],
            ["areas.id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_zones_area_tenant",
        ),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_zones_status"),
    )

    area_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class CameraProviderConnection(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "camera_provider_connections"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_provider_connections_id_tenant"),
        UniqueConstraint("tenant_id", "provider_type", "name", name="uq_provider_connections_name"),
        ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_connections_facility_tenant",
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_connections_status"),
    )

    facility_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class Camera(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "cameras"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_cameras_id_tenant"),
        UniqueConstraint(
            "tenant_id",
            "provider_connection_id",
            "provider_device_id",
            name="uq_cameras_provider_device",
        ),
        ForeignKeyConstraint(
            ["zone_id", "tenant_id"],
            ["zones.id", "zones.tenant_id"],
            ondelete="RESTRICT",
            name="fk_cameras_zone_tenant",
        ),
        ForeignKeyConstraint(
            ["provider_connection_id", "tenant_id"],
            ["camera_provider_connections.id", "camera_provider_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_cameras_connection_tenant",
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED', 'ARCHIVED')", name="ck_cameras_status"),
    )

    zone_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_connection_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_device_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class EdgeNode(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "edge_nodes"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_edge_nodes_id_tenant"),
        ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_edge_nodes_facility_tenant",
        ),
        CheckConstraint(
            "status IN ('PROVISIONING', 'ONLINE', 'OFFLINE', 'DISABLED')",
            name="ck_edge_nodes_status",
        ),
        CheckConstraint("memory_mb > 0", name="ck_edge_nodes_memory_positive"),
    )

    facility_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    gpu_available: Mapped[bool] = mapped_column(nullable=False, default=False)
    accelerator_type: Mapped[str | None] = mapped_column(String(64))
    memory_mb: Mapped[int] = mapped_column(nullable=False)
    software_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PROVISIONING")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CameraAssignment(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "camera_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            ondelete="RESTRICT",
            name="fk_assignments_camera_tenant",
        ),
        ForeignKeyConstraint(
            ["edge_node_id", "tenant_id"],
            ["edge_nodes.id", "edge_nodes.tenant_id"],
            ondelete="RESTRICT",
            name="fk_assignments_edge_tenant",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at > assigned_at", name="ck_assignments_time_order"
        ),
        Index(
            "uq_camera_assignments_active_camera",
            "camera_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    edge_node_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Actor(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "actors"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_actors_id_tenant"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_actors_status"),
    )

    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")


class TenantIdentityBinding(Base, IdMixin, TenantOwnedMixin):
    __tablename__ = "tenant_identity_bindings"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_tenant_identity_bindings_id_tenant"),
        UniqueConstraint(
            "provider",
            "issuer",
            "external_organization_id",
            name="uq_tenant_identity_bindings_external_org",
        ),
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    external_organization_id: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActorIdentity(Base, IdMixin, TenantOwnedMixin):
    __tablename__ = "actor_identities"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_actor_identities_id_tenant"),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "issuer",
            "subject",
            name="uq_actor_identities_external_principal",
        ),
        ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_actor_identities_actor_tenant",
        ),
    )

    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoleAssignment(Base, IdMixin, TenantOwnedMixin):
    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_role_assignments_id_tenant"),
        ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_actor_tenant",
        ),
        ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_facility_tenant",
        ),
        ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_creator_tenant",
        ),
        CheckConstraint(
            "role IN ('TENANT_OWNER', 'FACILITY_ADMIN', 'SAFETY_REVIEWER', 'VIEWER')",
            name="ck_role_assignments_role",
        ),
        CheckConstraint(
            "role <> 'TENANT_OWNER' OR facility_id IS NULL",
            name="ck_tenant_owner_is_tenant_scoped",
        ),
        Index(
            "uq_role_assignments_active_tenant_scope",
            "tenant_id",
            "actor_id",
            "role",
            unique=True,
            postgresql_where=text("facility_id IS NULL AND archived_at IS NULL"),
        ),
        Index(
            "uq_role_assignments_active_facility_scope",
            "tenant_id",
            "actor_id",
            "role",
            "facility_id",
            unique=True,
            postgresql_where=text("facility_id IS NOT NULL AND archived_at IS NULL"),
        ),
    )

    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    facility_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base, IdMixin, TenantOwnedMixin):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_audit_events_actor_tenant",
        ),
        Index("ix_audit_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_events_target", "tenant_id", "target_type", "target_id"),
    )

    actor_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class JurisdictionPolicy(Base, IdMixin, TimestampMixin):
    __tablename__ = "jurisdiction_policies"
    __table_args__ = (UniqueConstraint("jurisdiction", name="uq_policies_jurisdiction"),)

    jurisdiction: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class PolicyVersion(Base, IdMixin):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
        CheckConstraint(
            "status IN ('DRAFT', 'ENABLED', 'DISABLED')", name="ck_policy_versions_status"
        ),
        CheckConstraint(
            "disabled_at IS NULL OR disabled_at >= effective_date", name="ck_policy_versions_dates"
        ),
    )

    policy_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("jurisdiction_policies.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    disabled_at: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    source_references: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


TENANT_OWNED_TABLES = (
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
)

# The organization-to-Tenant binding is the pre-context root of trust. Runtime roles
# receive no direct table privileges and use only the exact-match security-definer
# function. Applying tenant RLS before the Tenant is known would be circular.
RLS_TENANT_TABLES = tuple(
    table for table in TENANT_OWNED_TABLES if table != "tenant_identity_bindings"
)
