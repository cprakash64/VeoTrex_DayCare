"""Add AEAD-encrypted credential storage for the production vault.

Revision ID: 0005_encrypted_credentials
Revises: 0004_ring_inventory
Create Date: 2026-09-12

Credential records are deliberately not tenant-scoped: the existing CredentialVault contract binds
a credential to (provider, owner_kind, owner_id), and Ring one-way linking stores credentials
before any tenant is known. Isolation therefore comes from narrow grants plus AEAD associated data
that binds ciphertext to its context, not from row-level security on a tenant column that does not
exist at that point in the lifecycle.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_encrypted_credentials"
down_revision: str | None = "0004_ring_inventory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "encrypted_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("owner_kind", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version >= 1", name="ck_encrypted_credentials_version_positive"),
        sa.CheckConstraint("schema_version >= 1", name="ck_encrypted_credentials_schema_positive"),
        # AES-GCM nonce is 12 bytes; the tag is inside the ciphertext, which is bounded well above
        # the 8 KiB-per-token application limit.
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_encrypted_credentials_nonce_len"),
        sa.CheckConstraint(
            "octet_length(ciphertext) BETWEEN 16 AND 65536",
            name="ck_encrypted_credentials_ciphertext_len",
        ),
    )
    op.create_index(
        "ix_encrypted_credentials_owner",
        "encrypted_credentials",
        ["provider", "owner_kind", "owner_id"],
    )
    # Least privilege: no PUBLIC access to credential ciphertext.
    op.execute("REVOKE ALL ON TABLE encrypted_credentials FROM PUBLIC")


def downgrade() -> None:
    op.drop_index("ix_encrypted_credentials_owner", table_name="encrypted_credentials")
    op.drop_table("encrypted_credentials")
