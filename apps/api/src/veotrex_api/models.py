from datetime import date, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
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
        # Target of the presence snapshot FK (V1-04B): a snapshot's classroom and facility must
        # agree, enforced by the database rather than trusted from the API.
        UniqueConstraint("id", "facility_id", "tenant_id", name="uq_areas_id_facility_tenant"),
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
    # A classroom is an Area of kind CLASSROOM (V1-04A); no separate classroom table exists.
    kind: Mapped[str] = mapped_column(String(50), nullable=False, default="ROOM")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    # Operator-supplied configuration text ("Toddler", "Pre-K"). Never inferred from imagery,
    # never a child's age, never a name.
    age_band_label: Mapped[str | None] = mapped_column(String(64))


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
        CheckConstraint(
            "status IN ('PENDING', 'ACTIVE', 'DISABLED', 'ARCHIVED')",
            name="ck_connections_status",
        ),
        CheckConstraint(
            "integration_state IN ('CONFIGURING', 'ACTIVE', 'REAUTH_REQUIRED', "
            "'REFRESH_UNCERTAIN', 'DISCONNECTED', 'ARCHIVED')",
            name="ck_connections_integration_state",
        ),
        CheckConstraint("credential_generation >= 1", name="ck_connections_generation_positive"),
        CheckConstraint(
            "operational_health IN ('ACTIVE', 'AUTH_DEGRADED', 'REAUTH_REQUIRED', "
            "'REMOTE_REMOVED', 'SYNC_DEGRADED')",
            name="ck_connections_operational_health",
        ),
        ForeignKeyConstraint(
            ["linked_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_connections_linked_actor_tenant",
        ),
        Index(
            "uq_ring_active_account",
            "provider_type",
            "external_account_id",
            unique=True,
            postgresql_where=text(
                "provider_type = 'RING' AND external_account_id IS NOT NULL "
                "AND integration_state NOT IN ('DISCONNECTED', 'ARCHIVED')"
            ),
        ),
    )

    facility_id: Mapped[UUID | None] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(512))
    credential_owner_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    external_account_id: Mapped[str | None] = mapped_column(String(512))
    integration_state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="CONFIGURING"
    )
    linked_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_category: Mapped[str | None] = mapped_column(String(128))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operational_health: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_failure_category: Mapped[str | None] = mapped_column(String(128))
    remote_removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EncryptedCredential(Base, IdMixin, TimestampMixin):
    """AEAD-sealed provider credential.

    Not tenant-scoped by design: the CredentialVault contract binds a credential to
    (provider, owner_kind, owner_id), and Ring one-way linking creates credentials before any
    tenant exists. Only ciphertext and non-secret context are stored; no column ever holds a
    plaintext token.
    """

    __tablename__ = "encrypted_credentials"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_encrypted_credentials_version_positive"),
        CheckConstraint("schema_version >= 1", name="ck_encrypted_credentials_schema_positive"),
        CheckConstraint("octet_length(nonce) = 12", name="ck_encrypted_credentials_nonce_len"),
        CheckConstraint(
            "octet_length(ciphertext) BETWEEN 16 AND 65536",
            name="ck_encrypted_credentials_ciphertext_len",
        ),
        Index("ix_encrypted_credentials_owner", "provider", "owner_kind", "owner_id"),
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class RingPendingLink(Base, IdMixin):
    __tablename__ = "ring_pending_links"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RECEIVED', 'UNCLAIMED', 'CLAIMING', "
            "'RING_CONFIRMATION_UNCERTAIN', 'RING_CONFIRMED_UNBOUND', "
            "'CLAIMED', 'FAILED', 'ARCHIVED')",
            name="ck_ring_pending_links_state",
        ),
        CheckConstraint(
            "state IN ('RECEIVED', 'FAILED', 'ARCHIVED') OR ring_account_id IS NOT NULL",
            name="ck_ring_pending_account_required",
        ),
        CheckConstraint("credential_generation >= 1", name="ck_ring_pending_generation_positive"),
        ForeignKeyConstraint(
            ["claim_actor_id", "claim_tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_ring_pending_claim_actor_tenant",
        ),
        Index(
            "uq_ring_pending_eligible_account",
            "ring_account_id",
            unique=True,
            postgresql_where=text(
                "ring_account_id IS NOT NULL AND state IN "
                "('UNCLAIMED', 'CLAIMING', 'RING_CONFIRMATION_UNCERTAIN', "
                "'RING_CONFIRMED_UNBOUND') AND archived_at IS NULL"
            ),
        ),
        Index("ix_ring_pending_state_received", "state", "received_at"),
    )

    ring_account_id: Mapped[str | None] = mapped_column(String(512))
    credential_secret_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    credential_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="RECEIVED")
    claim_tenant_id: Mapped[UUID | None] = mapped_column(Uuid)
    claim_actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    claim_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_category: Mapped[str | None] = mapped_column(String(128))


class Camera(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "cameras"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_cameras_id_tenant"),
        UniqueConstraint(
            "tenant_id",
            "provider_connection_id",
            "provider_device_id",
            "provider_component_key",
            name="uq_cameras_provider_component",
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
        CheckConstraint(
            "status IN ('DISCOVERED', 'ACTIVE', 'DISABLED', 'ARCHIVED')",
            name="ck_cameras_status",
        ),
    )

    zone_id: Mapped[UUID | None] = mapped_column(Uuid)
    provider_connection_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_device_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider_component_id: Mapped[str | None] = mapped_column(String(512))
    provider_component_key: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DISCOVERED")


class CameraProviderDevice(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "camera_provider_devices"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_provider_devices_id_tenant"),
        UniqueConstraint(
            "tenant_id",
            "provider_connection_id",
            "provider_device_id",
            name="uq_provider_devices_identity",
        ),
        ForeignKeyConstraint(
            ["provider_connection_id", "tenant_id"],
            ["camera_provider_connections.id", "camera_provider_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_devices_connection_tenant",
        ),
        CheckConstraint(
            "inventory_state IN ('ACTIVE', 'STALE', 'REMOVED', 'ARCHIVED')",
            name="ck_provider_devices_inventory_state",
        ),
        CheckConstraint(
            "sync_state IN ('HEALTHY', 'DEGRADED')",
            name="ck_provider_devices_sync_state",
        ),
        Index(
            "ix_provider_devices_connection_state",
            "tenant_id",
            "provider_connection_id",
            "inventory_state",
        ),
    )

    provider_connection_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_device_id: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    inventory_state: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    sync_state: Mapped[str] = mapped_column(String(20), nullable=False, default="HEALTHY")
    provider_online: Mapped[bool | None] = mapped_column()
    status_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capabilities_sha256: Mapped[str | None] = mapped_column(String(64))
    configuration_sha256: Mapped[str | None] = mapped_column(String(64))
    location_country: Mapped[str | None] = mapped_column(String(2))
    location_region: Mapped[str | None] = mapped_column(String(64))
    last_failure_category: Mapped[str | None] = mapped_column(String(128))


class CameraProviderComponent(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "camera_provider_components"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_provider_components_id_tenant"),
        UniqueConstraint(
            "tenant_id",
            "provider_device_record_id",
            "component_key",
            name="uq_provider_components_identity",
        ),
        UniqueConstraint("tenant_id", "camera_id", name="uq_provider_components_camera"),
        ForeignKeyConstraint(
            ["provider_device_record_id", "tenant_id"],
            ["camera_provider_devices.id", "camera_provider_devices.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_components_device_tenant",
        ),
        ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_components_camera_tenant",
        ),
        CheckConstraint(
            "inventory_state IN ('ACTIVE', 'REMOVED', 'ARCHIVED')",
            name="ck_provider_components_inventory_state",
        ),
    )

    provider_device_record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    camera_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    component_key: Mapped[str] = mapped_column(String(512), nullable=False)
    provider_component_id: Mapped[str | None] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    inventory_state: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    capability_details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    privacy_zones_configured: Mapped[bool] = mapped_column(nullable=False, default=False)
    motion_zones_configured: Mapped[bool] = mapped_column(nullable=False, default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderEvent(Base, IdMixin, TenantOwnedMixin):
    __tablename__ = "provider_events"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_provider_events_id_tenant"),
        UniqueConstraint("provider", "provider_request_id", name="uq_provider_events_request"),
        ForeignKeyConstraint(
            ["provider_connection_id", "tenant_id"],
            ["camera_provider_connections.id", "camera_provider_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_events_connection_tenant",
        ),
        ForeignKeyConstraint(
            ["provider_device_record_id", "tenant_id"],
            ["camera_provider_devices.id", "camera_provider_devices.tenant_id"],
            ondelete="RESTRICT",
            name="fk_provider_events_device_tenant",
        ),
        Index("ix_provider_events_tenant_occurred", "tenant_id", "provider_occurred_at"),
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_connection_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    provider_device_record_id: Mapped[UUID | None] = mapped_column(Uuid)
    provider_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_sub_type: Mapped[str | None] = mapped_column(String(128))
    component_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    provider_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RingWebhookInbox(Base, IdMixin):
    __tablename__ = "ring_webhook_inbox"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_ring_webhook_request_id"),
        CheckConstraint(
            "state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', "
            "'FAILED_RETRYABLE', 'FAILED_PERMANENT')",
            name="ck_ring_webhook_state",
        ),
        CheckConstraint("attempts >= 0 AND attempts <= 10", name="ck_ring_webhook_attempts"),
        Index("ix_ring_webhook_ready", "state", "next_attempt_at", "received_at"),
    )

    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    envelope_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ring_account_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(512))
    source_type: Mapped[str | None] = mapped_column(String(64))
    event_timestamp_ms: Mapped[int | None] = mapped_column(BigInteger)
    sub_type: Mapped[str | None] = mapped_column(String(128))
    component_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    related_device_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_category: Mapped[str | None] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class EdgeNodeCredential(Base, IdMixin, TenantOwnedMixin):
    """Machine credential of one EdgeNode (V1-DEMO-03B).

    ``id`` is the public selector carried inside the token; ``secret_sha256`` is a
    domain-separated SHA-256 of the 256-bit random secret, bound to that selector. The
    plaintext is never stored. The runtime role has no privilege on this table: it
    authenticates only through ``authenticate_edge_node_credential`` (migration 0008).
    """

    __tablename__ = "edge_node_credentials"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_edge_node_credentials_id_tenant"),
        ForeignKeyConstraint(
            ["edge_node_id", "tenant_id"],
            ["edge_nodes.id", "edge_nodes.tenant_id"],
            ondelete="RESTRICT",
            name="fk_edge_node_credentials_node_tenant",
        ),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_edge_node_credentials_status"),
        CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) "
            "OR (status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_edge_node_credentials_revocation",
        ),
        CheckConstraint(
            "octet_length(secret_sha256) = 32", name="ck_edge_node_credentials_digest_len"
        ),
        Index("ix_edge_node_credentials_node", "tenant_id", "edge_node_id", "status"),
    )

    edge_node_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    secret_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class ClassroomRatioPolicy(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    """An operator-configured staff-to-child ratio for one classroom (V1-04A).

    Configured policy, not certified law: the numbers are what the operator entered, with the
    operator's own source/reference text. No jurisdictional value is stored or implied here.
    Overlapping ACTIVE periods for one classroom are refused by the service under a per-
    classroom advisory lock; the pure resolver is deterministic even if one ever exists.
    """

    __tablename__ = "classroom_ratio_policies"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_classroom_ratio_policies_id_tenant"),
        ForeignKeyConstraint(
            ["area_id", "tenant_id"],
            ["areas.id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_ratio_policies_area_tenant",
        ),
        ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_ratio_policies_creator_tenant",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_classroom_ratio_policies_status"
        ),
        CheckConstraint(
            "max_children_per_staff > 0", name="ck_classroom_ratio_policies_max_children"
        ),
        CheckConstraint("minimum_staff >= 0", name="ck_classroom_ratio_policies_minimum_staff"),
        CheckConstraint(
            "maximum_group_size IS NULL OR maximum_group_size > 0",
            name="ck_classroom_ratio_policies_group_size",
        ),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="ck_classroom_ratio_policies_period",
        ),
        CheckConstraint("revision >= 1", name="ck_classroom_ratio_policies_revision"),
        Index(
            "ix_classroom_ratio_policies_area",
            "tenant_id",
            "area_id",
            "status",
            "effective_from",
        ),
    )

    area_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    age_band_label: Mapped[str | None] = mapped_column(String(64))
    max_children_per_staff: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_staff: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_group_size: Mapped[int | None] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_reference: Mapped[str | None] = mapped_column(String(500))
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid)


class ClassroomPresenceSnapshot(Base, IdMixin, TenantOwnedMixin):
    """One operator-reported aggregate head count for a classroom (V1-04B). Append-only.

    Counts and provenance only: no names, no child or staff identifiers, no images, no faces,
    no tracks, no boxes. A new report is a new row; a report is never edited. The only change a
    row may ever undergo is a single revocation (``revoked_at`` + ``revoked_by_actor_id``), and
    a database trigger (migration 0010) refuses every other UPDATE. The runtime role cannot
    DELETE.
    """

    __tablename__ = "classroom_presence_snapshots"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_classroom_presence_snapshots_id_tenant"),
        ForeignKeyConstraint(
            ["area_id", "facility_id", "tenant_id"],
            ["areas.id", "areas.facility_id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_presence_snapshots_area_facility_tenant",
        ),
        ForeignKeyConstraint(
            ["submitted_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_presence_snapshots_submitter_tenant",
        ),
        ForeignKeyConstraint(
            ["revoked_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_presence_snapshots_revoker_tenant",
        ),
        CheckConstraint("source = 'MANUAL'", name="ck_classroom_presence_snapshots_source"),
        CheckConstraint(
            "child_count BETWEEN 0 AND 150", name="ck_classroom_presence_snapshots_children"
        ),
        CheckConstraint(
            "qualified_staff_count BETWEEN 0 AND 50", name="ck_classroom_presence_snapshots_staff"
        ),
        CheckConstraint(
            "visitor_count BETWEEN 0 AND 50", name="ck_classroom_presence_snapshots_visitors"
        ),
        CheckConstraint(
            "valid_until >= observed_at + interval '30 seconds' "
            "AND valid_until <= observed_at + interval '15 minutes'",
            name="ck_classroom_presence_snapshots_validity",
        ),
        CheckConstraint(
            "observed_at <= created_at + interval '120 seconds'",
            name="ck_classroom_presence_snapshots_not_future",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_actor_id IS NULL)",
            name="ck_classroom_presence_snapshots_revocation_pair",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_classroom_presence_snapshots_revoked_after_created",
        ),
        Index(
            "ix_classroom_presence_snapshots_latest",
            "tenant_id",
            "area_id",
            text("observed_at DESC"),
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    facility_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    area_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    child_count: Mapped[int] = mapped_column(Integer, nullable=False)
    qualified_staff_count: Mapped[int] = mapped_column(Integer, nullable=False)
    visitor_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="MANUAL")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_by_actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid)


TENANT_OWNED_TABLES = (
    "facilities",
    "areas",
    "zones",
    "camera_provider_connections",
    "camera_provider_devices",
    "camera_provider_components",
    "cameras",
    "provider_events",
    "edge_nodes",
    "camera_assignments",
    "actors",
    "tenant_identity_bindings",
    "actor_identities",
    "role_assignments",
    "audit_events",
    # V1-04A
    "classroom_ratio_policies",
    # V1-04B
    "classroom_presence_snapshots",
)

# The organization-to-Tenant binding is the pre-context root of trust. Runtime roles
# receive no direct table privileges and use only the exact-match security-definer
# function. Applying tenant RLS before the Tenant is known would be circular.
RLS_TENANT_TABLES = tuple(
    table for table in TENANT_OWNED_TABLES if table != "tenant_identity_bindings"
)


# --------------------------------------------------------------------- staff enrollment (V1-02A)
# Monitored ADULT staff only. These rows are the people VeoTrex may later recognise in video.
# They are deliberately separate from Actor/ActorIdentity (authenticated dashboard users) and
# there is no child counterpart by design: the platform does not build child face identities.


class StaffProfile(Base, IdMixin, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "staff_profiles"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_staff_profiles_id_tenant"),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE', 'DELETED')", name="ck_staff_profiles_status"
        ),
        CheckConstraint(
            "enrollment_state IN ('EMPTY', 'COLLECTING', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_staff_profiles_enrollment_state",
        ),
        ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_profiles_creator_tenant",
        ),
        Index("ix_staff_profiles_tenant_status", "tenant_id", "status"),
    )

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    # Backend-authoritative readiness; never inferred by the UI from image counts.
    enrollment_state: Mapped[str] = mapped_column(String(20), nullable=False, default="EMPTY")
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StaffEnrollmentImage(Base, IdMixin, TenantOwnedMixin):
    """One accepted enrollment photo. Bytes live in the private media store under an opaque
    server-generated key; the row holds validation metadata only."""

    __tablename__ = "staff_enrollment_images"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_staff_enrollment_images_id_tenant"),
        CheckConstraint(
            "status IN ('ACCEPTED', 'DELETED')", name="ck_staff_enrollment_images_status"
        ),
        ForeignKeyConstraint(
            ["staff_profile_id", "tenant_id"],
            ["staff_profiles.id", "staff_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_enrollment_images_profile_tenant",
        ),
        Index(
            "uq_staff_enrollment_images_accepted_content",
            "staff_profile_id",
            "content_sha256",
            unique=True,
            postgresql_where=text("status = 'ACCEPTED'"),
        ),
        Index("ix_staff_enrollment_images_profile", "tenant_id", "staff_profile_id"),
    )

    staff_profile_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    media_key: Mapped[str | None] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, default="image/jpeg")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    face_size_px: Mapped[int | None] = mapped_column(Integer)
    quality: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACCEPTED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StaffFaceTemplate(Base, IdMixin, TenantOwnedMixin):
    """A face template derived from one accepted image. Sensitive biometric material: never
    serialised by a dashboard route, never logged; leaves the API only through the
    admin-only recognition package for the edge."""

    __tablename__ = "staff_face_templates"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_staff_face_templates_id_tenant"),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_staff_face_templates_status"),
        CheckConstraint("dimensions >= 1", name="ck_staff_face_templates_dimensions"),
        ForeignKeyConstraint(
            ["staff_profile_id", "tenant_id"],
            ["staff_profiles.id", "staff_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_face_templates_profile_tenant",
        ),
        ForeignKeyConstraint(
            ["enrollment_image_id", "tenant_id"],
            ["staff_enrollment_images.id", "staff_enrollment_images.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_face_templates_image_tenant",
        ),
        Index("ix_staff_face_templates_profile", "tenant_id", "staff_profile_id", "status"),
    )

    staff_profile_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    enrollment_image_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    dtype: Mapped[str] = mapped_column(String(16), nullable=False)
    template: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    quality: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
