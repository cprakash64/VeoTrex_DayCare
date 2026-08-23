"""Add Ring one-way account linking and credential lifecycle metadata.

Revision ID: 0003_ring_linking
Revises: 0002_identity_access
Create Date: 2026-08-23

Downgrade is intentionally blocked because it would erase credential lifecycle and
security audit state while leaving external Ring integration state unresolved.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_ring_linking"
down_revision: str | None = "0002_identity_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_connections_status", "camera_provider_connections", type_="check")
    op.alter_column("camera_provider_connections", "facility_id", nullable=True)
    op.alter_column("camera_provider_connections", "secret_ref", nullable=True)
    op.add_column("camera_provider_connections", sa.Column("external_account_id", sa.String(512)))
    # The vault credential remains context-bound to its unguessable pending-link ID
    # after tenant binding. This is metadata, never credential material.
    op.add_column("camera_provider_connections", sa.Column("credential_owner_id", sa.Uuid()))
    op.add_column(
        "camera_provider_connections",
        sa.Column("integration_state", sa.String(40), server_default="CONFIGURING", nullable=False),
    )
    op.add_column("camera_provider_connections", sa.Column("linked_by_actor_id", sa.Uuid()))
    op.add_column("camera_provider_connections", sa.Column("linked_at", sa.DateTime(timezone=True)))
    op.add_column(
        "camera_provider_connections", sa.Column("access_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "camera_provider_connections",
        sa.Column("credential_generation", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "camera_provider_connections", sa.Column("last_refresh_at", sa.DateTime(timezone=True))
    )
    op.add_column("camera_provider_connections", sa.Column("last_failure_category", sa.String(128)))
    op.add_column(
        "camera_provider_connections", sa.Column("disconnected_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "camera_provider_connections", sa.Column("archived_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_connections_status",
        "camera_provider_connections",
        "status IN ('PENDING', 'ACTIVE', 'DISABLED', 'ARCHIVED')",
    )
    op.create_check_constraint(
        "ck_connections_integration_state",
        "camera_provider_connections",
        "integration_state IN ('CONFIGURING', 'ACTIVE', 'REAUTH_REQUIRED', "
        "'REFRESH_UNCERTAIN', 'DISCONNECTED', 'ARCHIVED')",
    )
    op.create_check_constraint(
        "ck_connections_generation_positive",
        "camera_provider_connections",
        "credential_generation >= 1",
    )
    op.create_foreign_key(
        "fk_provider_connections_linked_actor_tenant",
        "camera_provider_connections",
        "actors",
        ["linked_by_actor_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_ring_active_account",
        "camera_provider_connections",
        ["provider_type", "external_account_id"],
        unique=True,
        postgresql_where=sa.text(
            "provider_type = 'RING' AND external_account_id IS NOT NULL "
            "AND integration_state NOT IN ('DISCONNECTED', 'ARCHIVED')"
        ),
    )

    op.create_table(
        "ring_pending_links",
        sa.Column("ring_account_id", sa.String(512)),
        sa.Column("credential_secret_ref", sa.String(512), nullable=False),
        sa.Column("credential_generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(40), server_default="RECEIVED", nullable=False),
        sa.Column("claim_tenant_id", sa.Uuid()),
        sa.Column("claim_actor_id", sa.Uuid()),
        sa.Column("claim_started_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_category", sa.String(128)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('RECEIVED', 'UNCLAIMED', 'CLAIMING', "
            "'RING_CONFIRMATION_UNCERTAIN', 'RING_CONFIRMED_UNBOUND', "
            "'CLAIMED', 'FAILED', 'ARCHIVED')",
            name="ck_ring_pending_links_state",
        ),
        sa.CheckConstraint(
            "state IN ('RECEIVED', 'FAILED', 'ARCHIVED') OR ring_account_id IS NOT NULL",
            name="ck_ring_pending_account_required",
        ),
        sa.CheckConstraint(
            "credential_generation >= 1", name="ck_ring_pending_generation_positive"
        ),
        sa.ForeignKeyConstraint(
            ["claim_actor_id", "claim_tenant_id"],
            ["actors.id", "actors.tenant_id"],
            name="fk_ring_pending_claim_actor_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_ring_pending_eligible_account",
        "ring_pending_links",
        ["ring_account_id"],
        unique=True,
        postgresql_where=sa.text(
            "ring_account_id IS NOT NULL AND state IN "
            "('UNCLAIMED', 'CLAIMING', 'RING_CONFIRMATION_UNCERTAIN', "
            "'RING_CONFIRMED_UNBOUND') AND archived_at IS NULL"
        ),
    )
    op.create_index(
        "ix_ring_pending_state_received",
        "ring_pending_links",
        ["state", "received_at"],
    )

    op.execute(
        """
        CREATE FUNCTION create_ring_pending_link(
            pending_id uuid, requested_secret_ref text, requested_generation integer,
            requested_expires_at timestamptz
        ) RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            INSERT INTO public.ring_pending_links
                (id, credential_secret_ref, credential_generation, access_expires_at, state)
            VALUES
                (pending_id, requested_secret_ref, requested_generation,
                 requested_expires_at, 'RECEIVED')
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION complete_ring_pending_account(pending_id uuid, requested_account_id text)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            WITH changed AS (
                UPDATE public.ring_pending_links
                SET ring_account_id = requested_account_id, state = 'UNCLAIMED',
                    last_failure_category = NULL
                WHERE id = pending_id AND state = 'RECEIVED' AND archived_at IS NULL
                RETURNING 1
            ) SELECT EXISTS(SELECT 1 FROM changed)
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION record_ring_pending_failure(pending_id uuid, failure_category text)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            WITH changed AS (
                UPDATE public.ring_pending_links
                SET last_failure_category = failure_category
                WHERE id = pending_id AND state = 'RECEIVED' AND archived_at IS NULL
                RETURNING 1
            ) SELECT EXISTS(SELECT 1 FROM changed)
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION list_ring_pending_candidates(requested_received_after timestamptz)
        RETURNS TABLE(
            id uuid, ring_account_id text, credential_secret_ref text,
            credential_generation integer, access_expires_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            SELECT link.id, link.ring_account_id, link.credential_secret_ref,
                   link.credential_generation, link.access_expires_at
            FROM public.ring_pending_links AS link
            WHERE link.state = 'UNCLAIMED' AND link.archived_at IS NULL
              AND link.access_expires_at > now()
              AND link.received_at >= requested_received_after
            ORDER BY link.received_at
            LIMIT 100
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION start_ring_pending_claim(
            pending_id uuid, requested_tenant_id uuid, requested_actor_id uuid
        ) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            WITH changed AS (
                UPDATE public.ring_pending_links
                SET state = 'CLAIMING', claim_tenant_id = requested_tenant_id,
                    claim_actor_id = requested_actor_id, claim_started_at = now()
                WHERE id = pending_id AND state = 'UNCLAIMED' AND archived_at IS NULL
                  AND access_expires_at > now()
                RETURNING 1
            ) SELECT EXISTS(SELECT 1 FROM changed)
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION transition_ring_pending_link(
            pending_id uuid, expected_state text, requested_state text,
            failure_category text DEFAULT NULL
        ) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        DECLARE affected integer;
        BEGIN
            IF (expected_state, requested_state) NOT IN (
                ('RECEIVED', 'FAILED'),
                ('RECEIVED', 'ARCHIVED'),
                ('UNCLAIMED', 'CLAIMING'),
                ('UNCLAIMED', 'ARCHIVED'),
                ('CLAIMING', 'UNCLAIMED'),
                ('CLAIMING', 'FAILED'),
                ('CLAIMING', 'RING_CONFIRMATION_UNCERTAIN'),
                ('CLAIMING', 'RING_CONFIRMED_UNBOUND'),
                ('CLAIMING', 'CLAIMED'),
                ('RING_CONFIRMATION_UNCERTAIN', 'FAILED'),
                ('RING_CONFIRMATION_UNCERTAIN', 'RING_CONFIRMED_UNBOUND'),
                ('RING_CONFIRMATION_UNCERTAIN', 'ARCHIVED'),
                ('RING_CONFIRMED_UNBOUND', 'CLAIMED'),
                ('RING_CONFIRMED_UNBOUND', 'ARCHIVED'),
                ('FAILED', 'ARCHIVED'),
                ('CLAIMED', 'ARCHIVED')
            ) THEN
                RAISE EXCEPTION 'invalid pending-link transition';
            END IF;
            UPDATE public.ring_pending_links
            SET state = requested_state,
                last_failure_category = failure_category,
                claimed_at = CASE WHEN requested_state = 'CLAIMED' THEN now() ELSE claimed_at END,
                archived_at = CASE WHEN requested_state = 'ARCHIVED' THEN now() ELSE archived_at END
            WHERE id = pending_id AND state = expected_state;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected = 1;
        END
        $function$
        """
    )
    for signature in (
        "create_ring_pending_link(uuid, text, integer, timestamptz)",
        "complete_ring_pending_account(uuid, text)",
        "record_ring_pending_failure(uuid, text)",
        "list_ring_pending_candidates(timestamptz)",
        "start_ring_pending_claim(uuid, uuid, uuid)",
        "transition_ring_pending_link(uuid, text, text, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError(
        "0003_ring_linking is irreversible: downgrade would erase credential lifecycle "
        "state without revoking the external Ring integration"
    )
