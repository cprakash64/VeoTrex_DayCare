"""Guardian contacts, child associations and authorized child release (V1-04E).

Revision ID: 0013_guardian_release
Revises: 0012_child_attendance
Create Date: 2026-09-26

* ``guardian_contacts`` - a facility-scoped roster entry for an adult: an opaque id, a bounded
  display name for the operator's own screens, ACTIVE / INACTIVE / ARCHIVED, and an optional
  identifier-shaped external reference (unique per facility when present). Not a legal status and
  not identity evidence: there is deliberately no photo, face, embedding, voice, identity-document,
  date-of-birth, address, phone or email column. Never deleted by the runtime.
* ``child_guardian_links`` - many-to-many child <-> contact. An operator-typed relationship label
  (no meaning to VeoTrex), a separate ``pickup_authorized`` flag, a half-open effective period,
  ACTIVE / INACTIVE with who/when deactivated, and a revision. One ACTIVE link per pair (partial
  unique index). Composite FKs make a link to another facility's child or contact unstorable.
  Runtime: SELECT, INSERT, UPDATE.
* ``child_release_events`` - append-only: one row per authorized release. A composite FK names
  the exact CHECKED_OUT attendance event it records (same child, classroom, facility and time; a
  CHECK pins the type), one release per event, and a composite FK names the exact link of this
  child and this contact that authorized it. The operator's verification method is one of three
  bounded statements. No name, label, image or camera column. Runtime: SELECT, INSERT.
* ``child_attendance_events`` gains a unique key over (id, tenant, facility, classroom, child,
  type, occurred_at) purely as the release FK's target; no row changes.

No trigger or function is added. The downgrade refuses while any contact, link or release exists,
rather than destroying family or pickup history.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0013_guardian_release"
down_revision: str | None = "0012_child_attendance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTACTS = "guardian_contacts"
LINKS = "child_guardian_links"
RELEASES = "child_release_events"
RELEASE_TARGET = "uq_child_attendance_events_release_target"
RLS_USING = (
    "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
    "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)
NAME_CHECK = (
    "char_length({column}) BETWEEN 1 AND {maximum} "
    "AND {column} !~ '[[:cntrl:]<>]' AND {column} = btrim({column})"
)


def _harden(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING {RLS_USING}')
    op.execute(f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC')


def _tenant_columns() -> list[sa.Column[Any]]:
    return [
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
    ]


def _updated_at() -> sa.Column[Any]:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_unique_constraint(
        RELEASE_TARGET,
        "child_attendance_events",
        [
            "id",
            "tenant_id",
            "facility_id",
            "area_id",
            "child_profile_id",
            "event_type",
            "occurred_at",
        ],
    )

    op.create_table(
        CONTACTS,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("external_reference", sa.String(64)),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=False),
        *_tenant_columns(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE', 'ARCHIVED')", name="ck_guardian_contacts_status"
        ),
        sa.CheckConstraint(
            NAME_CHECK.format(column="display_name", maximum=120),
            name="ck_guardian_contacts_display_name",
        ),
        sa.CheckConstraint(
            "external_reference IS NULL "
            "OR external_reference ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$'",
            name="ck_guardian_contacts_external_reference",
        ),
        sa.ForeignKeyConstraint(
            ["facility_id", "tenant_id"],
            ["facilities.id", "facilities.tenant_id"],
            ondelete="RESTRICT",
            name="fk_guardian_contacts_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_guardian_contacts_creator_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_guardian_contacts_id_tenant"),
        sa.UniqueConstraint(
            "id", "facility_id", "tenant_id", name="uq_guardian_contacts_id_facility_tenant"
        ),
    )
    op.create_index(
        "uq_guardian_contacts_external_reference",
        CONTACTS,
        ["tenant_id", "facility_id", "external_reference"],
        unique=True,
        postgresql_where=sa.text("external_reference IS NOT NULL"),
    )
    op.create_index(
        "ix_guardian_contacts_facility", CONTACTS, ["tenant_id", "facility_id", "status"]
    )

    op.create_table(
        LINKS,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("child_profile_id", sa.Uuid(), nullable=False),
        sa.Column("guardian_contact_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_label", sa.String(64), nullable=False),
        sa.Column("pickup_authorized", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("note", sa.String(200)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivated_by_actor_id", sa.Uuid()),
        *_tenant_columns(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_child_guardian_links_status"
        ),
        sa.CheckConstraint(
            NAME_CHECK.format(column="relationship_label", maximum=64),
            name="ck_child_guardian_links_relationship_label",
        ),
        sa.CheckConstraint(
            "note IS NULL OR (char_length(note) BETWEEN 1 AND 200 AND note !~ '[[:cntrl:]<>]')",
            name="ck_child_guardian_links_note",
        ),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="ck_child_guardian_links_period",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_child_guardian_links_revision"),
        sa.CheckConstraint(
            "(status = 'INACTIVE') = (deactivated_at IS NOT NULL) "
            "AND (deactivated_at IS NULL) = (deactivated_by_actor_id IS NULL)",
            name="ck_child_guardian_links_deactivation",
        ),
        sa.ForeignKeyConstraint(
            ["child_profile_id", "facility_id", "tenant_id"],
            ["child_profiles.id", "child_profiles.facility_id", "child_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_guardian_links_child_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["guardian_contact_id", "facility_id", "tenant_id"],
            [
                "guardian_contacts.id",
                "guardian_contacts.facility_id",
                "guardian_contacts.tenant_id",
            ],
            ondelete="RESTRICT",
            name="fk_child_guardian_links_contact_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_guardian_links_creator_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["deactivated_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_guardian_links_deactivator_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_child_guardian_links_id_tenant"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "facility_id",
            "child_profile_id",
            "guardian_contact_id",
            name="uq_child_guardian_links_release_target",
        ),
    )
    op.create_index(
        "uq_child_guardian_links_active",
        LINKS,
        ["tenant_id", "child_profile_id", "guardian_contact_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_child_guardian_links_child", LINKS, ["tenant_id", "child_profile_id", "status"]
    )
    op.create_index(
        "ix_child_guardian_links_contact", LINKS, ["tenant_id", "guardian_contact_id", "status"]
    )

    op.create_table(
        RELEASES,
        sa.Column("facility_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("child_profile_id", sa.Uuid(), nullable=False),
        sa.Column("guardian_contact_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_link_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_link_revision", sa.Integer(), nullable=False),
        sa.Column("verification_method", sa.String(32), nullable=False),
        sa.Column("attendance_event_id", sa.Uuid(), nullable=False),
        sa.Column("attendance_event_type", sa.String(20), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by_actor_id", sa.Uuid(), nullable=False),
        *_tenant_columns(),
        sa.CheckConstraint(
            "attendance_event_type = 'CHECKED_OUT'",
            name="ck_child_release_events_checkout_type",
        ),
        sa.CheckConstraint(
            "verification_method IN ('KNOWN_TO_STAFF', 'OPERATOR_CONFIRMED', 'PHOTO_ID_CHECKED')",
            name="ck_child_release_events_verification_method",
        ),
        sa.CheckConstraint(
            "authorization_link_revision >= 1", name="ck_child_release_events_link_revision"
        ),
        sa.CheckConstraint(
            "released_at <= created_at + interval '120 seconds'",
            name="ck_child_release_events_not_future",
        ),
        sa.ForeignKeyConstraint(
            [
                "attendance_event_id",
                "tenant_id",
                "facility_id",
                "area_id",
                "child_profile_id",
                "attendance_event_type",
                "released_at",
            ],
            [
                "child_attendance_events.id",
                "child_attendance_events.tenant_id",
                "child_attendance_events.facility_id",
                "child_attendance_events.area_id",
                "child_attendance_events.child_profile_id",
                "child_attendance_events.event_type",
                "child_attendance_events.occurred_at",
            ],
            ondelete="RESTRICT",
            name="fk_child_release_events_checkout",
        ),
        sa.ForeignKeyConstraint(
            [
                "authorization_link_id",
                "tenant_id",
                "facility_id",
                "child_profile_id",
                "guardian_contact_id",
            ],
            [
                "child_guardian_links.id",
                "child_guardian_links.tenant_id",
                "child_guardian_links.facility_id",
                "child_guardian_links.child_profile_id",
                "child_guardian_links.guardian_contact_id",
            ],
            ondelete="RESTRICT",
            name="fk_child_release_events_link",
        ),
        sa.ForeignKeyConstraint(
            ["guardian_contact_id", "facility_id", "tenant_id"],
            [
                "guardian_contacts.id",
                "guardian_contacts.facility_id",
                "guardian_contacts.tenant_id",
            ],
            ondelete="RESTRICT",
            name="fk_child_release_events_contact_facility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_child_release_events_recorder_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_child_release_events_id_tenant"),
        sa.UniqueConstraint(
            "tenant_id", "attendance_event_id", name="uq_child_release_events_attendance_event"
        ),
    )
    for name, column in (
        ("ix_child_release_events_child", "child_profile_id"),
        ("ix_child_release_events_classroom", "area_id"),
        ("ix_child_release_events_contact", "guardian_contact_id"),
    ):
        op.create_index(name, RELEASES, ["tenant_id", column, sa.text("released_at DESC")])

    for table in (CONTACTS, LINKS, RELEASES):
        _harden(table)


def downgrade() -> None:
    # Fail closed: removing these tables would destroy family contacts and pickup history.
    bind = op.get_bind()
    blockers = {
        "guardian contacts": "SELECT count(*) FROM guardian_contacts",
        "child guardian links": "SELECT count(*) FROM child_guardian_links",
        "child release events": "SELECT count(*) FROM child_release_events",
    }
    present = [name for name, query in blockers.items() if bind.execute(sa.text(query)).scalar()]
    if present:
        raise RuntimeError(f"downgrade refused: {', '.join(present)} exist")
    for table in (RELEASES, LINKS, CONTACTS):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    for name in (
        "ix_child_release_events_contact",
        "ix_child_release_events_classroom",
        "ix_child_release_events_child",
    ):
        op.drop_index(name, table_name=RELEASES)
    op.drop_table(RELEASES)
    for name in (
        "ix_child_guardian_links_contact",
        "ix_child_guardian_links_child",
        "uq_child_guardian_links_active",
    ):
        op.drop_index(name, table_name=LINKS)
    op.drop_table(LINKS)
    op.drop_index("ix_guardian_contacts_facility", table_name=CONTACTS)
    op.drop_index("uq_guardian_contacts_external_reference", table_name=CONTACTS)
    op.drop_table(CONTACTS)
    op.drop_constraint(RELEASE_TARGET, "child_attendance_events", type_="unique")
