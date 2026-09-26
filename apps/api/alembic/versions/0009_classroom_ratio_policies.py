"""Classrooms and operator-configured ratio policies (V1-04A).

Revision ID: 0009_classroom_ratio_policies
Revises: 0008_edge_node_credentials
Create Date: 2026-09-25

A classroom is the existing ``areas`` row with ``kind = 'CLASSROOM'`` - no competing location
table. This revision only adds what that reuse lacks:

* ``areas.age_band_label`` - nullable operator text ("Toddler"). Existing rows are untouched.
* ``classroom_ratio_policies`` - tenant-owned, forced Row Level Security, composite tenant-safe
  foreign keys, integer CHECKs mirroring the service's validation.

Purely additive; the downgrade removes exactly these objects. No jurisdiction value is seeded,
and the global policy-pack tables are not touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_classroom_ratio_policies"
down_revision: str | None = "0008_edge_node_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "classroom_ratio_policies"


def upgrade() -> None:
    op.add_column("areas", sa.Column("age_band_label", sa.String(64), nullable=True))

    op.create_table(
        TABLE,
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("age_band_label", sa.String(64)),
        sa.Column("max_children_per_staff", sa.Integer(), nullable=False),
        sa.Column("minimum_staff", sa.Integer(), nullable=False),
        sa.Column("maximum_group_size", sa.Integer()),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_reference", sa.String(500)),
        sa.Column("created_by_actor_id", sa.Uuid()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_classroom_ratio_policies_status"
        ),
        sa.CheckConstraint(
            "max_children_per_staff > 0", name="ck_classroom_ratio_policies_max_children"
        ),
        sa.CheckConstraint("minimum_staff >= 0", name="ck_classroom_ratio_policies_minimum_staff"),
        sa.CheckConstraint(
            "maximum_group_size IS NULL OR maximum_group_size > 0",
            name="ck_classroom_ratio_policies_group_size",
        ),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="ck_classroom_ratio_policies_period",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_classroom_ratio_policies_revision"),
        sa.ForeignKeyConstraint(
            ["area_id", "tenant_id"],
            ["areas.id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_ratio_policies_area_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_ratio_policies_creator_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_classroom_ratio_policies_id_tenant"),
    )
    op.create_index(
        "ix_classroom_ratio_policies_area",
        TABLE,
        ["tenant_id", "area_id", "status", "effective_from"],
    )
    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{TABLE}" USING '
        "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    # Least privilege: nothing for PUBLIC; the runtime role is granted by the role provisioner.
    op.execute(f'REVOKE ALL ON TABLE "{TABLE}" FROM PUBLIC')


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{TABLE}"')
    op.drop_index("ix_classroom_ratio_policies_area", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_column("areas", "age_band_label")
