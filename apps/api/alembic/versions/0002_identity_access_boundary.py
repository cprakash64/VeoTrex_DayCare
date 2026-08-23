"""Add provider-neutral identity and tenant authorization boundary.

Revision ID: 0002_identity_access
Revises: 0001_stage0
Create Date: 2026-08-23

Downgrade is intentionally blocked: collapsing multiple external identities and scoped,
historical role assignments into the Stage 0 Actor columns would destroy authorization history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_identity_access"
down_revision: str | None = "0001_stage0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_IDENTITY_TABLES = ("actor_identities", "role_assignments")


def _enable_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.create_table(
        "tenant_identity_bindings",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("external_organization_id", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_tenant_identity_bindings_id_tenant"),
        sa.UniqueConstraint(
            "provider",
            "issuer",
            "external_organization_id",
            name="uq_tenant_identity_bindings_external_org",
        ),
    )
    op.create_table(
        "actor_identities",
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            name="fk_actor_identities_actor_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_actor_identities_id_tenant"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "issuer",
            "subject",
            name="uq_actor_identities_external_principal",
        ),
    )
    op.create_table(
        "role_assignments",
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("facility_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "role IN ('TENANT_OWNER', 'FACILITY_ADMIN', 'SAFETY_REVIEWER', 'VIEWER')",
            name="ck_role_assignments_role",
        ),
        sa.CheckConstraint(
            "role <> 'TENANT_OWNER' OR facility_id IS NULL",
            name="ck_tenant_owner_is_tenant_scoped",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            name="fk_role_assignments_actor_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            name="fk_role_assignments_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            name="fk_role_assignments_facility_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_role_assignments_id_tenant"),
    )
    op.create_index(
        "uq_role_assignments_active_tenant_scope",
        "role_assignments",
        ["tenant_id", "actor_id", "role"],
        unique=True,
        postgresql_where=sa.text("facility_id IS NULL AND archived_at IS NULL"),
    )
    op.create_index(
        "uq_role_assignments_active_facility_scope",
        "role_assignments",
        ["tenant_id", "actor_id", "role", "facility_id"],
        unique=True,
        postgresql_where=sa.text("facility_id IS NOT NULL AND archived_at IS NULL"),
    )

    # Preserve Stage 0 identity traceability. Legacy identities confer no Auth0 access.
    op.execute(
        "INSERT INTO actor_identities "
        "(id, tenant_id, actor_id, provider, issuer, subject) "
        "SELECT gen_random_uuid(), tenant_id, id, 'legacy', "
        "'urn:veotrex:stage0', external_subject FROM actors"
    )
    op.execute(
        "INSERT INTO role_assignments (id, tenant_id, actor_id, role) "
        "SELECT gen_random_uuid(), tenant_id, id, role FROM actors "
        "WHERE role IN ('TENANT_OWNER', 'FACILITY_ADMIN', 'SAFETY_REVIEWER', 'VIEWER')"
    )
    op.drop_constraint("uq_actors_external_subject", "actors", type_="unique")
    op.drop_column("actors", "external_subject")
    op.drop_column("actors", "role")

    for table in RLS_IDENTITY_TABLES:
        _enable_rls(table)

    # This is the only pre-RLS lookup. Inputs come exclusively from a verified token;
    # exact matching and active-state filters produce one authoritative tenant UUID.
    op.execute(
        """
        CREATE FUNCTION resolve_tenant_identity_binding(
            requested_provider text,
            requested_issuer text,
            requested_external_organization_id text
        ) RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT binding.tenant_id
            FROM public.tenant_identity_bindings AS binding
            JOIN public.tenants AS tenant ON tenant.id = binding.tenant_id
            WHERE binding.provider = requested_provider
              AND binding.issuer = requested_issuer
              AND binding.external_organization_id = requested_external_organization_id
              AND binding.archived_at IS NULL
              AND tenant.status = 'ACTIVE'
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION resolve_tenant_identity_binding(text, text, text) FROM PUBLIC"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0002_identity_access is irreversible: downgrade would destroy external identity "
        "and role-assignment history"
    )
