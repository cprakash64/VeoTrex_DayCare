"""Camera portals: doorway lines for anonymous room entry / exit (V1-05A).

Revision ID: 0014_camera_portals
Revises: 0013_guardian_release
Create Date: 2026-09-26

* ``camera_portals`` - one operator-configured doorway line per row, scoped to tenant, facility,
  classroom (``area_id``) and camera: normalised endpoints (CHECK 0-1, which also refuses NaN
  and infinity), the room's side as seen on the picture (LEFT/RIGHT/ABOVE/BELOW), a dead-band
  (0-0.1), a minimum length (0.01), a restricted label unique per camera among ACTIVE rows,
  ``enabled``, ACTIVE / ARCHIVED with who/when archived, and a revision. Composite FKs keep the
  classroom inside the facility and the camera inside the tenant; the service checks that the
  camera belongs to the classroom (camera -> zone -> area). Runtime: SELECT, INSERT, UPDATE.

Configuration only: no person, identity, image or event is stored. The edge does not yet read
these rows (ADR 0029). No trigger or function is added. The downgrade refuses while any portal
exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_camera_portals"
down_revision: str | None = "0013_guardian_release"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PORTALS = "camera_portals"
RLS_USING = (
    "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
    "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        PORTALS,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
        sa.Column("x2", sa.Float(), nullable=False),
        sa.Column("y2", sa.Float(), nullable=False),
        sa.Column("inside_side", sa.String(8), nullable=False),
        sa.Column("deadband", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("archived_by_actor_id", sa.Uuid()),
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
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_camera_portals_status"),
        sa.CheckConstraint(
            "inside_side IN ('LEFT', 'RIGHT', 'ABOVE', 'BELOW')",
            name="ck_camera_portals_inside_side",
        ),
        sa.CheckConstraint(
            "x1 BETWEEN 0 AND 1 AND y1 BETWEEN 0 AND 1 AND x2 BETWEEN 0 AND 1 "
            "AND y2 BETWEEN 0 AND 1",
            name="ck_camera_portals_normalised",
        ),
        sa.CheckConstraint(
            "sqrt((x2 - x1) * (x2 - x1) + (y2 - y1) * (y2 - y1)) >= 0.01",
            name="ck_camera_portals_length",
        ),
        sa.CheckConstraint("deadband BETWEEN 0 AND 0.1", name="ck_camera_portals_deadband"),
        sa.CheckConstraint(
            "label ~ '^[A-Za-z0-9 _.()/:#-]{1,40}$' AND label = btrim(label)",
            name="ck_camera_portals_label",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_camera_portals_revision"),
        sa.CheckConstraint(
            "(status = 'ARCHIVED') = (archived_at IS NOT NULL) "
            "AND (archived_at IS NULL) = (archived_by_actor_id IS NULL)",
            name="ck_camera_portals_archive",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_camera_portals_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["area_id", "facility_id", "tenant_id"],
            ["areas.id", "areas.facility_id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_camera_portals_area_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            ondelete="RESTRICT",
            name="fk_camera_portals_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_camera_portals_creator_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_camera_portals_archiver_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_camera_portals_id_tenant"),
    )
    op.create_index(
        "uq_camera_portals_active_label",
        PORTALS,
        ["tenant_id", "camera_id", sa.text("lower(label)")],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index("ix_camera_portals_camera", PORTALS, ["tenant_id", "camera_id", "status"])
    op.create_index("ix_camera_portals_classroom", PORTALS, ["tenant_id", "area_id", "status"])
    op.execute(f'ALTER TABLE "{PORTALS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{PORTALS}" FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON "{PORTALS}" USING {RLS_USING}')
    op.execute(f'REVOKE ALL ON TABLE "{PORTALS}" FROM PUBLIC')


def downgrade() -> None:
    # Fail closed: removing the table would destroy operator camera calibration.
    if op.get_bind().execute(sa.text("SELECT count(*) FROM camera_portals")).scalar():
        raise RuntimeError("downgrade refused: camera portals exist")
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{PORTALS}"')
    for name in (
        "ix_camera_portals_classroom",
        "ix_camera_portals_camera",
        "uq_camera_portals_active_label",
    ):
        op.drop_index(name, table_name=PORTALS)
    op.drop_table(PORTALS)
