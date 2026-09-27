"""Edge node machine credentials and their authentication boundary (V1-DEMO-03B).

Revision ID: 0008_edge_node_credentials
Revises: 0007_staff_enrollment
Create Date: 2026-09-24

An EdgeNode authenticates to the control plane with a dedicated machine credential, never with
a human Auth0 identity. ``edge_node_credentials`` is tenant-owned with forced Row Level
Security like every other tenant table, and it stores only a domain-separated SHA-256 digest of
a 256-bit random secret; the plaintext is written once to an operator file by the admin tool
and never persisted.

The API does not know the tenant before the machine has authenticated, so it cannot set
``app.tenant_id`` first and read the table under RLS. Rather than granting the runtime role
SELECT on credential rows, it executes one narrow SECURITY DEFINER function that addresses a
single credential by primary key (the token's public selector), compares the presented digest,
and returns only the server-resolved (tenant_id, edge_node_id, facility_id) when the credential
is ACTIVE, the node exists and is not DISABLED, and its tenant and facility are ACTIVE. Every
other outcome returns zero rows, so a caller cannot tell an unknown selector from a wrong
secret, a revoked credential or a disabled node. No dynamic SQL; typed parameters only;
``search_path`` pinned; EXECUTE revoked from PUBLIC (the runtime grant is applied by
``veotrex-db-runtime-role``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_edge_node_credentials"
down_revision: str | None = "0007_staff_enrollment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "edge_node_credentials"
FUNCTION_SIGNATURE = "authenticate_edge_node_credential(uuid, bytea)"

AUTHENTICATE_FUNCTION_SQL = """
        CREATE FUNCTION authenticate_edge_node_credential(
            requested_credential_id uuid, presented_secret_sha256 bytea
        ) RETURNS TABLE(tenant_id uuid, edge_node_id uuid, facility_id uuid)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        DECLARE
            credential public.edge_node_credentials%ROWTYPE;
            node public.edge_nodes%ROWTYPE;
        BEGIN
            IF requested_credential_id IS NULL OR presented_secret_sha256 IS NULL
               OR octet_length(presented_secret_sha256) <> 32 THEN
                RETURN;
            END IF;
            -- One row by primary key: the function can never enumerate credentials.
            SELECT * INTO credential FROM public.edge_node_credentials AS c
            WHERE c.id = requested_credential_id
              AND c.status = 'ACTIVE'
              AND c.revoked_at IS NULL;
            -- Digest of a 256-bit random secret: a timing difference here could reveal digest
            -- bytes at best, never a preimage, so a plain comparison is sufficient.
            IF NOT FOUND OR credential.secret_sha256 <> presented_secret_sha256 THEN
                RETURN;
            END IF;
            SELECT * INTO node FROM public.edge_nodes AS n
            WHERE n.id = credential.edge_node_id
              AND n.tenant_id = credential.tenant_id
              AND n.status <> 'DISABLED';
            IF NOT FOUND THEN
                RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.tenants AS t
                WHERE t.id = credential.tenant_id AND t.status = 'ACTIVE'
            ) OR NOT EXISTS (
                SELECT 1 FROM public.facilities AS f
                WHERE f.id = node.facility_id
                  AND f.tenant_id = node.tenant_id
                  AND f.status = 'ACTIVE'
            ) THEN
                RETURN;
            END IF;
            -- Coarse last-use mark: at most one write per credential per minute.
            UPDATE public.edge_node_credentials AS c
            SET last_used_at = now()
            WHERE c.id = credential.id
              AND (c.last_used_at IS NULL OR c.last_used_at < now() - interval '60 seconds');
            RETURN QUERY SELECT credential.tenant_id, node.id, node.facility_id;
        END
        $function$
"""


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("edge_node_id", sa.Uuid(), nullable=False),
        sa.Column("secret_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name="ck_edge_node_credentials_status"
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) "
            "OR (status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_edge_node_credentials_revocation",
        ),
        sa.CheckConstraint(
            "octet_length(secret_sha256) = 32", name="ck_edge_node_credentials_digest_len"
        ),
        sa.ForeignKeyConstraint(
            ["edge_node_id", "tenant_id"],
            ["edge_nodes.id", "edge_nodes.tenant_id"],
            ondelete="RESTRICT",
            name="fk_edge_node_credentials_node_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_edge_node_credentials_id_tenant"),
    )
    op.create_index("ix_edge_node_credentials_node", TABLE, ["tenant_id", "edge_node_id", "status"])
    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{TABLE}" USING '
        "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    # Least privilege: nothing for PUBLIC. The runtime role receives no table privilege at all;
    # it only gets EXECUTE on the function below, from the role provisioner.
    op.execute(f'REVOKE ALL ON TABLE "{TABLE}" FROM PUBLIC')
    op.execute(AUTHENTICATE_FUNCTION_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION_SIGNATURE} FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION_SIGNATURE}")
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{TABLE}"')
    op.drop_index("ix_edge_node_credentials_node", table_name=TABLE)
    op.drop_table(TABLE)
