"""Credential vault SECURITY DEFINER boundary and tenant self-isolation (V1-00A-R1).

Revision ID: 0006_vault_boundary
Revises: 0005_encrypted_credentials
Create Date: 2026-09-21

``encrypted_credentials`` is global by design (Ring one-way linking stores a credential before
any tenant is known), so the API runtime role must never hold table privileges on it: with the
vault master key in the same process, direct SELECT would let a compromised API decrypt every
tenant's Ring tokens in one query. The runtime instead executes four narrow functions, each of
which addresses exactly one credential by primary key, requires the caller's context
(provider, owner kind, owner id) to match the row, and authorizes the operation against the
existing pending-link state machine and the tenant-scoped connection that references the
credential once it is claimed:

* ``open``    - the pending link is CLAIMING by the caller's tenant (the claim step), or a
                connection of the caller's tenant references the credential.
* ``replace`` - only through a connection of the caller's tenant (token refresh).
* ``delete``  - through a connection of the caller's tenant (disconnect, remote removal), or
                while the link is still RECEIVED (pre-tenant clean-up), or when no pending link
                references the credential at all (orphan from a failed link creation).
* ``create``  - inserts a brand-new version-1 row; nothing to authorize against yet.

The caller's tenant is ``app.tenant_id``, the same transaction-local setting Row Level Security
uses everywhere else. No dynamic SQL; typed parameters only; ``search_path`` pinned. Lookups are
by primary key, so no function can enumerate.

``tenants`` gains ``FORCE ROW LEVEL SECURITY`` with a self-only policy: a tenant-scoped request
reads only its own row. Identity bootstrap is unaffected: ``resolve_tenant_identity_binding`` is
SECURITY DEFINER and provisioning runs as the admin identity.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_vault_boundary"
down_revision: str | None = "0005_encrypted_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_SETTING = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

FUNCTION_SIGNATURES = (
    "vault_credential_authorized(text, uuid, text)",
    "vault_credential_create(uuid, text, text, uuid, integer, bytea, bytea)",
    "vault_credential_open(uuid, text, text, uuid)",
    "vault_credential_replace(uuid, text, text, uuid, integer, integer, bytea, bytea)",
    "vault_credential_delete(uuid, text, text, uuid)",
)


def upgrade() -> None:
    # Private authorization predicate. Not granted to the runtime: it is called only from the
    # SECURITY DEFINER functions below, which run as the owner. Built by substituting a module
    # constant into a fixed template; no caller input is ever interpolated.
    op.execute(AUTHORIZED_FUNCTION_SQL.replace("{TENANT}", TENANT_SETTING))
    _upgrade_rest()


AUTHORIZED_FUNCTION_SQL = """
        CREATE FUNCTION vault_credential_authorized(
            requested_owner_kind text, requested_owner_id uuid, requested_operation text
        ) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
            SELECT requested_owner_kind = 'ring_pending_link'
              AND requested_operation IN ('open', 'replace', 'delete')
              AND (
                -- Claim step: the link is being claimed by the caller's tenant.
                (requested_operation = 'open' AND EXISTS (
                    SELECT 1 FROM public.ring_pending_links AS link
                    WHERE link.id = requested_owner_id
                      AND link.archived_at IS NULL
                      AND link.state = 'CLAIMING'
                      AND link.claim_tenant_id IS NOT NULL
                      AND link.claim_tenant_id = {TENANT}
                ))
                -- Claimed: a connection owned by the caller's tenant references it.
                OR EXISTS (
                    SELECT 1 FROM public.camera_provider_connections AS connection
                    WHERE connection.credential_owner_id = requested_owner_id
                      AND connection.tenant_id = {TENANT}
                )
                -- Pre-tenant clean-up: the link never left RECEIVED, or was never created.
                OR (requested_operation = 'delete' AND NOT EXISTS (
                    SELECT 1 FROM public.ring_pending_links AS link
                    WHERE link.id = requested_owner_id
                      AND NOT (link.state = 'RECEIVED' AND link.archived_at IS NULL)
                ))
              )
        $function$
"""


def _upgrade_rest() -> None:
    op.execute(
        """
        CREATE FUNCTION vault_credential_create(
            credential_id uuid, requested_provider text, requested_owner_kind text,
            requested_owner_id uuid, requested_schema_version integer,
            requested_nonce bytea, requested_ciphertext bytea
        ) RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        BEGIN
            IF requested_provider <> 'RING' OR requested_owner_kind <> 'ring_pending_link' THEN
                RAISE EXCEPTION 'unsupported credential context';
            END IF;
            INSERT INTO public.encrypted_credentials
                (id, provider, owner_kind, owner_id, version, schema_version, nonce, ciphertext)
            VALUES
                (credential_id, requested_provider, requested_owner_kind, requested_owner_id,
                 1, requested_schema_version, requested_nonce, requested_ciphertext);
            RETURN 1;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION vault_credential_open(
            credential_id uuid, requested_provider text, requested_owner_kind text,
            requested_owner_id uuid
        ) RETURNS TABLE(
            outcome text, version integer, schema_version integer, nonce bytea, ciphertext bytea
        ) LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        DECLARE record public.encrypted_credentials%ROWTYPE;
        BEGIN
            SELECT * INTO record FROM public.encrypted_credentials AS c
            WHERE c.id = credential_id;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'unavailable'::text, NULL::integer, NULL::integer,
                                    NULL::bytea, NULL::bytea;
                RETURN;
            END IF;
            IF record.provider <> requested_provider
               OR record.owner_kind <> requested_owner_kind
               OR record.owner_id <> requested_owner_id THEN
                RETURN QUERY SELECT 'context_mismatch'::text, NULL::integer, NULL::integer,
                                    NULL::bytea, NULL::bytea;
                RETURN;
            END IF;
            IF NOT public.vault_credential_authorized(
                requested_owner_kind, requested_owner_id, 'open'
            ) THEN
                RETURN QUERY SELECT 'unavailable'::text, NULL::integer, NULL::integer,
                                    NULL::bytea, NULL::bytea;
                RETURN;
            END IF;
            RETURN QUERY SELECT 'ok'::text, record.version, record.schema_version,
                                record.nonce, record.ciphertext;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION vault_credential_replace(
            credential_id uuid, requested_provider text, requested_owner_kind text,
            requested_owner_id uuid, expected_version integer,
            requested_schema_version integer, requested_nonce bytea, requested_ciphertext bytea
        ) RETURNS TABLE(outcome text, version integer)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        DECLARE record public.encrypted_credentials%ROWTYPE;
        BEGIN
            SELECT * INTO record FROM public.encrypted_credentials AS c
            WHERE c.id = credential_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'unavailable'::text, NULL::integer;
                RETURN;
            END IF;
            IF record.provider <> requested_provider
               OR record.owner_kind <> requested_owner_kind
               OR record.owner_id <> requested_owner_id THEN
                RETURN QUERY SELECT 'context_mismatch'::text, NULL::integer;
                RETURN;
            END IF;
            IF NOT public.vault_credential_authorized(
                requested_owner_kind, requested_owner_id, 'replace'
            ) THEN
                RETURN QUERY SELECT 'unavailable'::text, NULL::integer;
                RETURN;
            END IF;
            IF record.version <> expected_version THEN
                RETURN QUERY SELECT 'version_conflict'::text, record.version;
                RETURN;
            END IF;
            UPDATE public.encrypted_credentials AS c
            SET version = record.version + 1,
                schema_version = requested_schema_version,
                nonce = requested_nonce,
                ciphertext = requested_ciphertext,
                updated_at = now()
            WHERE c.id = credential_id;
            RETURN QUERY SELECT 'ok'::text, record.version + 1;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION vault_credential_delete(
            credential_id uuid, requested_provider text, requested_owner_kind text,
            requested_owner_id uuid
        ) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        DECLARE record public.encrypted_credentials%ROWTYPE;
        BEGIN
            SELECT * INTO record FROM public.encrypted_credentials AS c
            WHERE c.id = credential_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'absent';
            END IF;
            IF record.provider <> requested_provider
               OR record.owner_kind <> requested_owner_kind
               OR record.owner_id <> requested_owner_id THEN
                RETURN 'context_mismatch';
            END IF;
            IF NOT public.vault_credential_authorized(
                requested_owner_kind, requested_owner_id, 'delete'
            ) THEN
                RETURN 'unavailable';
            END IF;
            DELETE FROM public.encrypted_credentials AS c WHERE c.id = credential_id;
            RETURN 'deleted';
        END
        $function$
        """
    )
    for signature in FUNCTION_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")

    # A tenant-scoped request may read only its own tenant row. Provisioning runs as the admin
    # identity and the identity resolver is SECURITY DEFINER, so bootstrap is unaffected.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_self ON tenants USING (id = {TENANT}) "
        "WITH CHECK (id = {TENANT})".replace("{TENANT}", TENANT_SETTING)
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_self ON tenants")
    op.execute("ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
    for signature in reversed(FUNCTION_SIGNATURES):
        op.execute(f"DROP FUNCTION {signature}")
