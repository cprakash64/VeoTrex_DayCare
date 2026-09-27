"""Anonymous room-transition events from edge nodes (V1-05B).

Revision ID: 0015_room_transition_events
Revises: 0014_camera_portals
Create Date: 2026-09-26

* ``room_transition_events`` - append-only. The primary key is the event id the edge generated
  and persisted before its first upload, so retries land on the same row. Tenant, facility,
  classroom (``area_id``), camera, edge node and portal are composite-FK scoped; the service
  derives them from the node's credential, its active camera assignment and the portal - never
  from the request. ``ephemeral_track_id`` + ``stream_instance_id`` are camera-session-local
  metadata, not a person. CHECKs bound the type, crossing point (NaN and infinity fail
  ``BETWEEN``), evidence, track number, stream id shape, and ``occurred_at`` against the
  server's receipt time (at most 120 s ahead, at most 7 days behind). No profile, face,
  embedding, frame or image column. Runtime: SELECT, INSERT - never UPDATE or DELETE.

Indexes serve the classroom timeline (keyset by occurred_at, id), per-camera reads and a future
retention job (by received_at). No trigger or function is added. The downgrade refuses while any
event exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_room_transition_events"
down_revision: str | None = "0014_camera_portals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENTS = "room_transition_events"
RLS_USING = (
    "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
    "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        EVENTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("edge_node_id", sa.Uuid(), nullable=False),
        sa.Column("portal_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ephemeral_track_id", sa.Integer(), nullable=False),
        sa.Column("stream_instance_id", sa.String(64), nullable=False),
        sa.Column("crossing_x", sa.Float(), nullable=False),
        sa.Column("crossing_y", sa.Float(), nullable=False),
        sa.Column("evidence_observations", sa.Integer(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('ENTERED', 'EXITED')", name="ck_room_transition_events_type"
        ),
        sa.CheckConstraint(
            "crossing_x BETWEEN 0 AND 1 AND crossing_y BETWEEN 0 AND 1",
            name="ck_room_transition_events_crossing",
        ),
        sa.CheckConstraint(
            "evidence_observations BETWEEN 1 AND 100",
            name="ck_room_transition_events_evidence",
        ),
        sa.CheckConstraint(
            "ephemeral_track_id BETWEEN 1 AND 2147483647",
            name="ck_room_transition_events_track",
        ),
        sa.CheckConstraint(
            "stream_instance_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'",
            name="ck_room_transition_events_stream",
        ),
        sa.CheckConstraint(
            "occurred_at <= received_at + interval '120 seconds'",
            name="ck_room_transition_events_not_future",
        ),
        sa.CheckConstraint(
            "occurred_at >= received_at - interval '7 days'",
            name="ck_room_transition_events_not_stale",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_room_transition_events_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["area_id", "facility_id", "tenant_id"],
            ["areas.id", "areas.facility_id", "areas.tenant_id"],
            ondelete="RESTRICT",
            name="fk_room_transition_events_area_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            ondelete="RESTRICT",
            name="fk_room_transition_events_camera_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["edge_node_id", "tenant_id"],
            ["edge_nodes.id", "edge_nodes.tenant_id"],
            ondelete="RESTRICT",
            name="fk_room_transition_events_node_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["portal_id", "tenant_id"],
            ["camera_portals.id", "camera_portals.tenant_id"],
            ondelete="RESTRICT",
            name="fk_room_transition_events_portal_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_room_transition_events_id_tenant"),
    )
    op.create_index(
        "ix_room_transition_events_classroom",
        EVENTS,
        ["tenant_id", "area_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_room_transition_events_camera",
        EVENTS,
        ["tenant_id", "camera_id", sa.text("occurred_at DESC")],
    )
    op.create_index("ix_room_transition_events_received", EVENTS, ["tenant_id", "received_at"])
    op.execute(f'ALTER TABLE "{EVENTS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{EVENTS}" FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON "{EVENTS}" USING {RLS_USING}')
    op.execute(f'REVOKE ALL ON TABLE "{EVENTS}" FROM PUBLIC')


def downgrade() -> None:
    # Fail closed: removing the table would destroy the room-transition record.
    if op.get_bind().execute(sa.text("SELECT count(*) FROM room_transition_events")).scalar():
        raise RuntimeError("downgrade refused: room transition events exist")
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{EVENTS}"')
    for name in (
        "ix_room_transition_events_received",
        "ix_room_transition_events_camera",
        "ix_room_transition_events_classroom",
    ):
        op.drop_index(name, table_name=EVENTS)
    op.drop_table(EVENTS)
