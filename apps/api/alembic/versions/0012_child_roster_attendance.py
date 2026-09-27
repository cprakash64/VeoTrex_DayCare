"""Child roster and authoritative attendance check-in/out (V1-04D).

Revision ID: 0012_child_attendance
Revises: 0011_staff_presence
Create Date: 2026-09-26

* ``child_profiles`` - a facility-scoped roster entry: an opaque id, a bounded display name for
  the operator's own screens, ACTIVE / INACTIVE / ARCHIVED, and an optional identifier-shaped
  external reference (unique per facility when present) for a future attendance connector.
  Ordinary roster data, not identity evidence: there is deliberately no photo, face, embedding,
  date of birth, address, medical, guardian or camera column. Never deleted by the runtime.
* ``child_attendance_events`` - an append-only CHECKED_IN / REFRESHED / CHECKED_OUT stream,
  source ATTENDANCE. A child's current classroom is their single latest event by ``sequence``;
  ``UNIQUE (tenant_id, child_profile_id, sequence)`` stops two concurrent writers from both
  committing. Open events carry a lease of 30 min - 12 h. A composite FK to
  ``child_profiles (id, facility_id, tenant_id)`` and one to ``areas (id, facility_id,
  tenant_id)`` make a child in another facility's classroom unstorable. Runtime: SELECT, INSERT.
* ``areas.presence_source_mode`` gains ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF; existing rows keep
  their mode.
* ``classroom_presence_snapshots.child_count`` becomes nullable: in attendance mode the manual
  report carries visitors only, so it can never hold a child number beside attendance.

No trigger or function is added. The downgrade refuses while any child, attendance event,
visitor-only report or attendance-mode classroom exists, rather than destroying or inventing
data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_child_attendance"
down_revision: str | None = "0011_staff_presence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROFILES = "child_profiles"
EVENTS = "child_attendance_events"
OLD_MODES = "'MANUAL_AGGREGATE', 'ROSTER_STAFF_PLUS_MANUAL_CHILDREN'"
NEW_MODES = f"{OLD_MODES}, 'ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF'"
RLS_USING = (
    "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
    "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def _harden(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING {RLS_USING}')
    op.execute(f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC')


def upgrade() -> None:
    op.drop_constraint("ck_areas_presence_source_mode", "areas", type_="check")
    op.create_check_constraint(
        "ck_areas_presence_source_mode", "areas", f"presence_source_mode IN ({NEW_MODES})"
    )
    op.alter_column(
        "classroom_presence_snapshots", "child_count", existing_type=sa.Integer(), nullable=True
    )

    op.create_table(
        PROFILES,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("external_reference", sa.String(64)),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=False),
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
            "status IN ('ACTIVE', 'INACTIVE', 'ARCHIVED')", name="ck_child_profiles_status"
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 120 "
            "AND display_name !~ '[[:cntrl:]<>]' AND display_name = btrim(display_name)",
            name="ck_child_profiles_display_name",
        ),
        sa.CheckConstraint(
            "external_reference IS NULL "
            "OR external_reference ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$'",
            name="ck_child_profiles_external_reference",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_profiles_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_profiles_creator_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_child_profiles_id_tenant"),
        sa.UniqueConstraint(
            "id", "facility_id", "tenant_id", name="uq_child_profiles_id_facility_tenant"
        ),
    )
    op.create_index(
        "uq_child_profiles_external_reference",
        PROFILES,
        ["tenant_id", "facility_id", "external_reference"],
        unique=True,
        postgresql_where=sa.text("external_reference IS NOT NULL"),
    )
    op.create_index("ix_child_profiles_facility", PROFILES, ["tenant_id", "facility_id", "status"])

    op.create_table(
        EVENTS,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("child_profile_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by_actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint(
            "event_type IN ('CHECKED_IN', 'REFRESHED', 'CHECKED_OUT')",
            name="ck_child_attendance_events_type",
        ),
        sa.CheckConstraint("source = 'ATTENDANCE'", name="ck_child_attendance_events_source"),
        sa.CheckConstraint("sequence >= 1", name="ck_child_attendance_events_sequence"),
        sa.CheckConstraint(
            "(event_type = 'CHECKED_OUT') = (valid_until IS NULL)",
            name="ck_child_attendance_events_lease_presence",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR (valid_until >= occurred_at + interval '30 minutes' "
            "AND valid_until <= occurred_at + interval '12 hours')",
            name="ck_child_attendance_events_lease_bounds",
        ),
        sa.CheckConstraint(
            "occurred_at <= created_at + interval '120 seconds'",
            name="ck_child_attendance_events_not_future",
        ),
        sa.CheckConstraint(
            "checked_in_at <= occurred_at "
            "AND (event_type <> 'CHECKED_IN' OR checked_in_at = occurred_at)",
            name="ck_child_attendance_events_session_start",
        ),
        sa.ForeignKeyConstraint(
            ["area_id", "facility_id", "tenant_id"],
            ["areas.id", "areas.facility_id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_attendance_events_area_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["child_profile_id", "facility_id", "tenant_id"],
            ["child_profiles.id", "child_profiles.facility_id", "child_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_attendance_events_child_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_attendance_events_recorder_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_child_attendance_events_id_tenant"),
        # The concurrency guarantee: one event per position in a child's stream.
        sa.UniqueConstraint(
            "tenant_id",
            "child_profile_id",
            "sequence",
            name="uq_child_attendance_events_child_sequence",
        ),
    )
    op.create_index(
        "ix_child_attendance_events_classroom",
        EVENTS,
        ["tenant_id", "area_id", sa.text("occurred_at DESC"), sa.text("sequence DESC")],
    )

    _harden(PROFILES)
    _harden(EVENTS)


def downgrade() -> None:
    # Fail closed: removing these objects would destroy roster and attendance history, and
    # restoring the old constraints would need child numbers nobody reported.
    bind = op.get_bind()
    blockers = {
        "child profiles": "SELECT count(*) FROM child_profiles",
        "attendance events": "SELECT count(*) FROM child_attendance_events",
        "visitor-only presence reports": (
            "SELECT count(*) FROM classroom_presence_snapshots WHERE child_count IS NULL"
        ),
        "attendance-mode classrooms": (
            "SELECT count(*) FROM areas "
            "WHERE presence_source_mode = 'ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF'"
        ),
    }
    present = [name for name, query in blockers.items() if bind.execute(sa.text(query)).scalar()]
    if present:
        raise RuntimeError(f"downgrade refused: {', '.join(present)} exist")
    for table in (EVENTS, PROFILES):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    op.drop_index("ix_child_attendance_events_classroom", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_child_profiles_facility", table_name=PROFILES)
    op.drop_index("uq_child_profiles_external_reference", table_name=PROFILES)
    op.drop_table(PROFILES)
    op.alter_column(
        "classroom_presence_snapshots", "child_count", existing_type=sa.Integer(), nullable=False
    )
    op.drop_constraint("ck_areas_presence_source_mode", "areas", type_="check")
    op.create_check_constraint(
        "ck_areas_presence_source_mode", "areas", f"presence_source_mode IN ({OLD_MODES})"
    )
