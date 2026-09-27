"""Ratio-eligible staff roster and authoritative staff check-in/out (V1-04C).

Revision ID: 0011_staff_presence
Revises: 0010_classroom_presence
Create Date: 2026-09-25

Three things, all additive except one relaxed NOT NULL:

* ``staff_ratio_eligibility`` - an operator's designation that an enrolled adult staff profile
  belongs to a facility's roster and whether they *count toward the configured classroom
  ratio*. Configured, not certified: nothing here records a licence, a qualification or a legal
  status. At most one ACTIVE assignment per (tenant, facility, staff) - a partial unique index,
  so two ambiguous concurrent designations cannot both exist. Deactivated, never deleted.
* ``staff_presence_events`` - an append-only CHECKED_IN / REFRESHED / CHECKED_OUT stream. A
  staff member's current room is their single latest event by ``sequence``; ``UNIQUE (tenant_id,
  staff_profile_id, sequence)`` makes two concurrent writers that saw the same state unable to
  both commit, so a person can never be current in two rooms. An open event carries a bounded
  lease (``valid_until``, 60 s - 4 h). The runtime role gets SELECT and INSERT only.
* ``areas.presence_source_mode`` - explicit per-classroom source precedence. Every existing row
  becomes MANUAL_AGGREGATE, exactly the V1-04B behaviour; roster staff counts apply only after an
  operator switches a classroom to ROSTER_STAFF_PLUS_MANUAL_CHILDREN.
* ``classroom_presence_snapshots.qualified_staff_count`` becomes nullable: in roster mode the
  operator reports children and visitors only, so the manual row never carries a staff number
  that could be mistaken for, or added to, the roster count.

No image, face, embedding, track or name is stored in either new table. No trigger or function
is added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_staff_presence"
down_revision: str | None = "0010_classroom_presence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ELIGIBILITY = "staff_ratio_eligibility"
EVENTS = "staff_presence_events"
MODES = "'MANUAL_AGGREGATE', 'ROSTER_STAFF_PLUS_MANUAL_CHILDREN'"
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
    op.add_column(
        "areas",
        sa.Column(
            "presence_source_mode",
            sa.String(40),
            server_default=sa.text("'MANUAL_AGGREGATE'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_areas_presence_source_mode", "areas", f"presence_source_mode IN ({MODES})"
    )
    op.alter_column(
        "classroom_presence_snapshots",
        "qualified_staff_count",
        existing_type=sa.Integer(),
        nullable=True,
    )

    op.create_table(
        ELIGIBILITY,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("staff_profile_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("counts_toward_ratio", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(500)),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivated_by_actor_id", sa.Uuid()),
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
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_staff_ratio_eligibility_status"
        ),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="ck_staff_ratio_eligibility_period",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_staff_ratio_eligibility_revision"),
        sa.CheckConstraint(
            "(status = 'INACTIVE') = (deactivated_at IS NOT NULL) "
            "AND (deactivated_at IS NULL) = (deactivated_by_actor_id IS NULL)",
            name="ck_staff_ratio_eligibility_deactivation",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_ratio_eligibility_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["staff_profile_id", "tenant_id"],
            ["staff_profiles.id", "staff_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_ratio_eligibility_profile_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_ratio_eligibility_creator_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["deactivated_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_ratio_eligibility_deactivator_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_staff_ratio_eligibility_id_tenant"),
    )
    op.create_index(
        "uq_staff_ratio_eligibility_active",
        ELIGIBILITY,
        ["tenant_id", "facility_id", "staff_profile_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_staff_ratio_eligibility_facility",
        ELIGIBILITY,
        ["tenant_id", "facility_id", "status"],
    )

    op.create_table(
        EVENTS,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("staff_profile_id", sa.Uuid(), nullable=False),
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
            name="ck_staff_presence_events_type",
        ),
        sa.CheckConstraint("source = 'STAFF_ROSTER'", name="ck_staff_presence_events_source"),
        sa.CheckConstraint("sequence >= 1", name="ck_staff_presence_events_sequence"),
        sa.CheckConstraint(
            "(event_type = 'CHECKED_OUT') = (valid_until IS NULL)",
            name="ck_staff_presence_events_lease_presence",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR (valid_until >= occurred_at + interval '60 seconds' "
            "AND valid_until <= occurred_at + interval '4 hours')",
            name="ck_staff_presence_events_lease_bounds",
        ),
        sa.CheckConstraint(
            "occurred_at <= created_at + interval '120 seconds'",
            name="ck_staff_presence_events_not_future",
        ),
        sa.CheckConstraint(
            "checked_in_at <= occurred_at "
            "AND (event_type <> 'CHECKED_IN' OR checked_in_at = occurred_at)",
            name="ck_staff_presence_events_session_start",
        ),
        sa.ForeignKeyConstraint(
            ["area_id", "facility_id", "tenant_id"],
            ["areas.id", "areas.facility_id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_presence_events_area_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["staff_profile_id", "tenant_id"],
            ["staff_profiles.id", "staff_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_presence_events_profile_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_presence_events_recorder_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_staff_presence_events_id_tenant"),
        # The concurrency guarantee: one event per position in a person's stream.
        sa.UniqueConstraint(
            "tenant_id",
            "staff_profile_id",
            "sequence",
            name="uq_staff_presence_events_staff_sequence",
        ),
    )
    op.create_index(
        "ix_staff_presence_events_classroom",
        EVENTS,
        ["tenant_id", "area_id", sa.text("occurred_at DESC"), sa.text("sequence DESC")],
    )

    _harden(ELIGIBILITY)
    _harden(EVENTS)


def downgrade() -> None:
    # A roster-mode manual report has no staff count. Restoring NOT NULL would need a number
    # that was never reported, and the append-only guard forbids editing one in; refuse rather
    # than invent data.
    bind = op.get_bind()
    missing = bind.execute(
        sa.text(
            "SELECT count(*) FROM classroom_presence_snapshots WHERE qualified_staff_count IS NULL"
        )
    ).scalar_one()
    if missing:
        raise RuntimeError(
            "downgrade refused: roster-mode presence reports have no qualified staff count"
        )
    for table in (EVENTS, ELIGIBILITY):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    op.drop_index("ix_staff_presence_events_classroom", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_staff_ratio_eligibility_facility", table_name=ELIGIBILITY)
    op.drop_index("uq_staff_ratio_eligibility_active", table_name=ELIGIBILITY)
    op.drop_table(ELIGIBILITY)
    op.alter_column(
        "classroom_presence_snapshots",
        "qualified_staff_count",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_constraint("ck_areas_presence_source_mode", "areas", type_="check")
    op.drop_column("areas", "presence_source_mode")
