"""Staff (adult teacher) enrollment: profiles, enrollment images, face templates (V1-02A).

Revision ID: 0007_staff_enrollment
Revises: 0006_vault_boundary
Create Date: 2026-09-22

Monitored staff are a separate domain from authenticated dashboard users (actors), and there
is deliberately no child counterpart. All three tables are tenant-owned with forced Row Level
Security and composite tenant-safe foreign keys. Image bytes are not stored here; the row keeps
an opaque media key and validation metadata. Templates are bytea and carry their model
identity, version and format so a later model change invalidates them explicitly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_staff_enrollment"
down_revision: str | None = "0006_vault_boundary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("staff_profiles", "staff_enrollment_images", "staff_face_templates")


def _enable_tenant_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" USING '
        "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.create_table(
        "staff_profiles",
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("enrollment_state", sa.String(20), nullable=False),
        sa.Column("created_by_actor_id", sa.Uuid()),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
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
            "status IN ('ACTIVE', 'INACTIVE', 'DELETED')", name="ck_staff_profiles_status"
        ),
        sa.CheckConstraint(
            "enrollment_state IN ('EMPTY', 'COLLECTING', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_staff_profiles_enrollment_state",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id", "tenant_id"],
            ["actors.id", "actors.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_profiles_creator_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_staff_profiles_id_tenant"),
    )
    op.create_index("ix_staff_profiles_tenant_status", "staff_profiles", ["tenant_id", "status"])

    op.create_table(
        "staff_enrollment_images",
        sa.Column("staff_profile_id", sa.Uuid(), nullable=False),
        sa.Column("media_key", sa.String(64)),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("face_size_px", sa.Integer()),
        sa.Column("quality", sa.Integer()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'DELETED')", name="ck_staff_enrollment_images_status"
        ),
        sa.ForeignKeyConstraint(
            ["staff_profile_id", "tenant_id"],
            ["staff_profiles.id", "staff_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_enrollment_images_profile_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_staff_enrollment_images_id_tenant"),
    )
    op.create_index(
        "uq_staff_enrollment_images_accepted_content",
        "staff_enrollment_images",
        ["staff_profile_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("status = 'ACCEPTED'"),
    )
    op.create_index(
        "ix_staff_enrollment_images_profile",
        "staff_enrollment_images",
        ["tenant_id", "staff_profile_id"],
    )

    op.create_table(
        "staff_face_templates",
        sa.Column("staff_profile_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_image_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("dtype", sa.String(16), nullable=False),
        sa.Column("template", sa.LargeBinary(), nullable=False),
        sa.Column("quality", sa.Integer()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name="ck_staff_face_templates_status"
        ),
        sa.CheckConstraint("dimensions >= 1", name="ck_staff_face_templates_dimensions"),
        sa.ForeignKeyConstraint(
            ["staff_profile_id", "tenant_id"],
            ["staff_profiles.id", "staff_profiles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_face_templates_profile_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["enrollment_image_id", "tenant_id"],
            ["staff_enrollment_images.id", "staff_enrollment_images.tenant_id"],
            ondelete="RESTRICT",
            name="fk_staff_face_templates_image_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_staff_face_templates_id_tenant"),
    )
    op.create_index(
        "ix_staff_face_templates_profile",
        "staff_face_templates",
        ["tenant_id", "staff_profile_id", "status"],
    )
    for table in TABLES:
        _enable_tenant_rls(table)
    # Least privilege: nothing for PUBLIC; the runtime role is granted by the role provisioner.
    for table in TABLES:
        op.execute(f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    op.drop_index("ix_staff_face_templates_profile", table_name="staff_face_templates")
    op.drop_table("staff_face_templates")
    op.drop_index("ix_staff_enrollment_images_profile", table_name="staff_enrollment_images")
    op.drop_index(
        "uq_staff_enrollment_images_accepted_content", table_name="staff_enrollment_images"
    )
    op.drop_table("staff_enrollment_images")
    op.drop_index("ix_staff_profiles_tenant_status", table_name="staff_profiles")
    op.drop_table("staff_profiles")
