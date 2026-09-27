"""Authoritative manual classroom presence snapshots (V1-04B).

Revision ID: 0010_classroom_presence
Revises: 0009_classroom_ratio_policies
Create Date: 2026-09-25

An operator reports aggregate counts for a classroom - children, qualified staff, visitors -
with a short validity. Append-only:

* a new report is a new row; history stays auditable;
* the only permitted UPDATE is one revocation (``revoked_at`` + ``revoked_by_actor_id``, NULL ->
  NOT NULL, once), enforced by a trigger so the database - not only the API - refuses any edit of
  counts, timestamps or provenance, and any second revocation;
* the runtime role receives no DELETE (``runtime_role.TABLE_CLASSIFICATION``).

The composite foreign key to ``areas (id, facility_id, tenant_id)`` makes a classroom/facility
mismatch impossible to store. Counts, validity and future-skew bounds are CHECKs. No names,
identifiers of people, images or camera data exist in this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_classroom_presence"
down_revision: str | None = "0009_classroom_ratio_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "classroom_presence_snapshots"
GUARD = "classroom_presence_snapshot_guard"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_areas_id_facility_tenant", "areas", ["id", "facility_id", "tenant_id"]
    )
    op.create_table(
        TABLE,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column("qualified_staff_count", sa.Integer(), nullable=False),
        sa.Column("visitor_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_by_actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_actor_id", sa.Uuid()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint("source = 'MANUAL'", name="ck_classroom_presence_snapshots_source"),
        sa.CheckConstraint(
            "child_count BETWEEN 0 AND 150", name="ck_classroom_presence_snapshots_children"
        ),
        sa.CheckConstraint(
            "qualified_staff_count BETWEEN 0 AND 50", name="ck_classroom_presence_snapshots_staff"
        ),
        sa.CheckConstraint(
            "visitor_count BETWEEN 0 AND 50", name="ck_classroom_presence_snapshots_visitors"
        ),
        sa.CheckConstraint(
            "valid_until >= observed_at + interval '30 seconds' "
            "AND valid_until <= observed_at + interval '15 minutes'",
            name="ck_classroom_presence_snapshots_validity",
        ),
        sa.CheckConstraint(
            "observed_at <= created_at + interval '120 seconds'",
            name="ck_classroom_presence_snapshots_not_future",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_actor_id IS NULL)",
            name="ck_classroom_presence_snapshots_revocation_pair",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_classroom_presence_snapshots_revoked_after_created",
        ),
        sa.ForeignKeyConstraint(
            ["area_id", "facility_id", "tenant_id"],
            ["areas.id", "areas.facility_id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_presence_snapshots_area_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_presence_snapshots_submitter_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_classroom_presence_snapshots_revoker_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_classroom_presence_snapshots_id_tenant"),
    )
    op.create_index(
        "ix_classroom_presence_snapshots_latest",
        TABLE,
        [
            "tenant_id",
            "area_id",
            sa.text("observed_at DESC"),
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    )
    # Append-only in the database itself. SECURITY DEFINER with a pinned search_path because the
    # project's function contract (ADR 0018, tests/test_runtime_role.py section 11) requires it
    # of every function in ``public``; it confers nothing here - the body runs no SQL and reads
    # no table, it only compares OLD with NEW. No role (runtime included) is granted EXECUTE; a
    # trigger is invoked by PostgreSQL itself, which does not check EXECUTE for it.
    op.execute(
        f"""
        CREATE FUNCTION public.{GUARD}() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION 'presence snapshot is already revoked'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.revoked_at IS NULL OR NEW.revoked_by_actor_id IS NULL THEN
                RAISE EXCEPTION 'the only permitted change is a revocation'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.tenant_id, NEW.facility_id, NEW.area_id, NEW.child_count,
                NEW.qualified_staff_count, NEW.visitor_count, NEW.source, NEW.observed_at,
                NEW.valid_until, NEW.submitted_by_actor_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.tenant_id, OLD.facility_id, OLD.area_id, OLD.child_count,
                OLD.qualified_staff_count, OLD.visitor_count, OLD.source, OLD.observed_at,
                OLD.valid_until, OLD.submitted_by_actor_id, OLD.created_at) THEN
                RAISE EXCEPTION 'presence snapshots are append-only'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{GUARD}() FROM PUBLIC")
    op.execute(
        f'CREATE TRIGGER {GUARD} BEFORE UPDATE ON "{TABLE}" '
        f"FOR EACH ROW EXECUTE FUNCTION public.{GUARD}()"
    )
    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{TABLE}" USING '
        "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f'REVOKE ALL ON TABLE "{TABLE}" FROM PUBLIC')


def downgrade() -> None:
    op.execute(f'DROP TRIGGER IF EXISTS {GUARD} ON "{TABLE}"')
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{TABLE}"')
    op.drop_index("ix_classroom_presence_snapshots_latest", table_name=TABLE)
    op.drop_table(TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{GUARD}()")
    op.drop_constraint("uq_areas_id_facility_tenant", "areas", type_="unique")
