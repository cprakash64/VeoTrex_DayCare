"""Add tenant-safe Ring inventory and signed webhook inbox.

Revision ID: 0004_ring_inventory
Revises: 0003_ring_linking
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ring_inventory"
down_revision: str | None = "0003_ring_linking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_tenant_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" USING '
        "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.add_column(
        "camera_provider_connections",
        sa.Column("operational_health", sa.String(32), server_default="ACTIVE", nullable=False),
    )
    op.add_column(
        "camera_provider_connections", sa.Column("last_sync_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "camera_provider_connections", sa.Column("last_sync_failure_category", sa.String(128))
    )
    op.add_column(
        "camera_provider_connections", sa.Column("remote_removed_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_connections_operational_health",
        "camera_provider_connections",
        "operational_health IN ('ACTIVE', 'AUTH_DEGRADED', 'REAUTH_REQUIRED', "
        "'REMOTE_REMOVED', 'SYNC_DEGRADED')",
    )

    op.drop_constraint("uq_cameras_provider_device", "cameras", type_="unique")
    op.drop_constraint("ck_cameras_status", "cameras", type_="check")
    op.alter_column("cameras", "zone_id", nullable=True)
    op.add_column("cameras", sa.Column("provider_component_id", sa.String(512)))
    op.add_column(
        "cameras",
        sa.Column("provider_component_key", sa.String(512), server_default="__single__"),
    )
    op.execute("UPDATE cameras SET provider_component_key = '__single__'")
    op.alter_column("cameras", "provider_component_key", nullable=False)
    op.alter_column("cameras", "provider_component_key", server_default=None)
    op.create_unique_constraint(
        "uq_cameras_provider_component",
        "cameras",
        ["tenant_id", "provider_connection_id", "provider_device_id", "provider_component_key"],
    )
    op.create_check_constraint(
        "ck_cameras_status",
        "cameras",
        "status IN ('DISCOVERED', 'ACTIVE', 'DISABLED', 'ARCHIVED')",
    )

    op.create_table(
        "camera_provider_devices",
        sa.Column("provider_connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider_device_id", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("inventory_state", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column("sync_state", sa.String(20), server_default="HEALTHY", nullable=False),
        sa.Column("provider_online", sa.Boolean()),
        sa.Column("status_observed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("capabilities_sha256", sa.String(64)),
        sa.Column("configuration_sha256", sa.String(64)),
        sa.Column("location_country", sa.String(2)),
        sa.Column("location_region", sa.String(64)),
        sa.Column("last_failure_category", sa.String(128)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "inventory_state IN ('ACTIVE', 'STALE', 'REMOVED', 'ARCHIVED')",
            name="ck_provider_devices_inventory_state",
        ),
        sa.CheckConstraint(
            "sync_state IN ('HEALTHY', 'DEGRADED')", name="ck_provider_devices_sync_state"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_connection_id", "tenant_id"],
            ["camera_provider_connections.id", "camera_provider_connections.tenant_id"],
            name="fk_provider_devices_connection_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_provider_devices_id_tenant"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_connection_id",
            "provider_device_id",
            name="uq_provider_devices_identity",
        ),
    )
    op.create_index(
        "ix_provider_devices_connection_state",
        "camera_provider_devices",
        ["tenant_id", "provider_connection_id", "inventory_state"],
    )

    op.create_table(
        "camera_provider_components",
        sa.Column("provider_device_record_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("component_key", sa.String(512), nullable=False),
        sa.Column("provider_component_id", sa.String(512)),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("inventory_state", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("capability_details", sa.JSON(), nullable=False),
        sa.Column(
            "privacy_zones_configured", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "motion_zones_configured", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "inventory_state IN ('ACTIVE', 'REMOVED', 'ARCHIVED')",
            name="ck_provider_components_inventory_state",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_device_record_id", "tenant_id"],
            ["camera_provider_devices.id", "camera_provider_devices.tenant_id"],
            name="fk_provider_components_device_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            name="fk_provider_components_camera_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_provider_components_id_tenant"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_device_record_id",
            "component_key",
            name="uq_provider_components_identity",
        ),
        sa.UniqueConstraint("tenant_id", "camera_id", name="uq_provider_components_camera"),
    )

    op.create_table(
        "provider_events",
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider_device_record_id", sa.Uuid()),
        sa.Column("provider_request_id", sa.String(128), nullable=False),
        sa.Column("provider_event_id", sa.String(512), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("provider_sub_type", sa.String(128)),
        sa.Column("component_ids", sa.JSON(), nullable=False),
        sa.Column("provider_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_connection_id", "tenant_id"],
            ["camera_provider_connections.id", "camera_provider_connections.tenant_id"],
            name="fk_provider_events_connection_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_device_record_id", "tenant_id"],
            ["camera_provider_devices.id", "camera_provider_devices.tenant_id"],
            name="fk_provider_events_device_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_provider_events_id_tenant"),
        sa.UniqueConstraint("provider", "provider_request_id", name="uq_provider_events_request"),
    )
    op.create_index(
        "ix_provider_events_tenant_occurred",
        "provider_events",
        ["tenant_id", "provider_occurred_at"],
    )

    op.create_table(
        "ring_webhook_inbox",
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("envelope_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ring_account_id", sa.String(512), nullable=False),
        sa.Column("event_id", sa.String(512), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(512)),
        sa.Column("source_type", sa.String(64)),
        sa.Column("event_timestamp_ms", sa.BigInteger()),
        sa.Column("sub_type", sa.String(128)),
        sa.Column("component_ids", sa.JSON(), nullable=False),
        sa.Column("related_device_ids", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), server_default="RECEIVED", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_category", sa.String(128)),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', "
            "'FAILED_RETRYABLE', 'FAILED_PERMANENT')",
            name="ck_ring_webhook_state",
        ),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 10", name="ck_ring_webhook_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_ring_webhook_request_id"),
    )
    op.create_index(
        "ix_ring_webhook_ready",
        "ring_webhook_inbox",
        ["state", "next_attempt_at", "received_at"],
    )

    for table in ("camera_provider_devices", "camera_provider_components", "provider_events"):
        _enable_tenant_rls(table)

    op.execute("""
        CREATE FUNCTION ingest_ring_webhook(
            inbox_id uuid, requested_request_id text, requested_version text,
            requested_envelope_time timestamptz, requested_account_id text,
            requested_event_id text, requested_event_type text,
            requested_source_id text, requested_source_type text,
            requested_timestamp_ms bigint, requested_sub_type text,
            requested_component_ids json, requested_device_ids json
        ) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            WITH inserted AS (
                INSERT INTO public.ring_webhook_inbox
                    (id, request_id, version, envelope_time, ring_account_id, event_id,
                     event_type, source_id, source_type, event_timestamp_ms, sub_type,
                     component_ids, related_device_ids)
                VALUES (inbox_id, requested_request_id, requested_version,
                        requested_envelope_time, requested_account_id, requested_event_id,
                        requested_event_type, requested_source_id, requested_source_type,
                        requested_timestamp_ms, requested_sub_type,
                        requested_component_ids, requested_device_ids)
                ON CONFLICT (request_id) DO NOTHING RETURNING 1
            ) SELECT EXISTS(SELECT 1 FROM inserted)
        $function$
    """)
    op.execute("""
        CREATE FUNCTION claim_next_ring_webhook()
        RETURNS TABLE(
            id uuid, request_id text, version text, envelope_time timestamptz,
            ring_account_id text, event_id text, event_type text, source_id text,
            source_type text, event_timestamp_ms bigint, sub_type text,
            component_ids json, related_device_ids json, attempts integer
        ) LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            WITH candidate AS (
                SELECT inbox.id FROM public.ring_webhook_inbox AS inbox
                WHERE inbox.state IN ('RECEIVED', 'FAILED_RETRYABLE')
                  AND (inbox.next_attempt_at IS NULL OR inbox.next_attempt_at <= now())
                  AND inbox.attempts < 5
                ORDER BY inbox.received_at
                FOR UPDATE SKIP LOCKED LIMIT 1
            ), claimed AS (
                UPDATE public.ring_webhook_inbox AS inbox
                SET state = 'PROCESSING', attempts = inbox.attempts + 1
                FROM candidate WHERE inbox.id = candidate.id
                RETURNING inbox.*
            )
            SELECT claimed.id, claimed.request_id, claimed.version,
                   claimed.envelope_time, claimed.ring_account_id, claimed.event_id,
                   claimed.event_type, claimed.source_id, claimed.source_type,
                   claimed.event_timestamp_ms, claimed.sub_type,
                   claimed.component_ids, claimed.related_device_ids,
                   claimed.attempts FROM claimed
        $function$
    """)
    op.execute("""
        CREATE FUNCTION finish_ring_webhook(
            inbox_id uuid, requested_state text, failure_category text DEFAULT NULL,
            retry_at timestamptz DEFAULT NULL
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        DECLARE affected integer;
        BEGIN
            IF requested_state NOT IN ('PROCESSED', 'FAILED_RETRYABLE', 'FAILED_PERMANENT') THEN
                RAISE EXCEPTION 'invalid webhook completion state';
            END IF;
            UPDATE public.ring_webhook_inbox SET state = requested_state,
                last_failure_category = failure_category, next_attempt_at = retry_at,
                processed_at = CASE WHEN requested_state IN ('PROCESSED', 'FAILED_PERMANENT')
                                    THEN now() ELSE NULL END
            WHERE id = inbox_id AND state = 'PROCESSING';
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected = 1;
        END
        $function$
    """)
    op.execute("""
        CREATE FUNCTION resolve_ring_webhook_connection(requested_account_id text)
        RETURNS TABLE(connection_id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            SELECT connection.id, connection.tenant_id
            FROM public.camera_provider_connections AS connection
            WHERE connection.provider_type = 'RING'
              AND connection.external_account_id = requested_account_id
            ORDER BY
              (connection.integration_state NOT IN ('DISCONNECTED', 'ARCHIVED')) DESC,
              connection.created_at DESC
            LIMIT 1
        $function$
    """)
    for signature in (
        "ingest_ring_webhook(uuid, text, text, timestamptz, text, text, text, "
        "text, text, bigint, text, json, json)",
        "claim_next_ring_webhook()",
        "finish_ring_webhook(uuid, text, text, timestamptz)",
        "resolve_ring_webhook_connection(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError(
        "0004_ring_inventory is irreversible: inventory and durable webhook state "
        "cannot be erased safely"
    )
