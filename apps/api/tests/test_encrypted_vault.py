"""Production AEAD credential vault: context binding, tampering, CAS rotation, redaction.

Database-backed cases run only against the isolated test cluster named by
``VEOTREX_TEST_DATABASE_URL``; they skip when it is not configured and never fall back to the
development database. They are never faked with SQLite either, because the rotation semantics
depend on PostgreSQL row locking. All token values here are obviously synthetic.
"""

import asyncio
import base64
import os
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from veotrex_api.credential_vault import (
    CredentialContext,
    CredentialMaterial,
    CredentialVaultError,
    CredentialVersionConflict,
)
from veotrex_api.encrypted_vault import (
    KEY_BYTES,
    SECRET_REF_PREFIX,
    EncryptedCredentialVault,
    VaultKeyProvider,
    VaultKeyUnavailable,
    canonical_aad,
)
from veotrex_api.secrets import SecretResolutionError, SecretResolver

SYNTHETIC_ACCESS = "synthetic-access-token-not-real"
SYNTHETIC_REFRESH = "synthetic-refresh-token-not-real"
CONTEXT = CredentialContext("RING", "ring_pending_link", UUID(int=0xA11CE))


class StubResolver:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def resolve(self, secret_ref: str) -> SecretStr:
        if self._value is None:
            raise SecretResolutionError("referenced secret is unavailable")
        return SecretStr(self._value)


def key_provider(raw: str | None = None) -> VaultKeyProvider:
    material = raw if raw is not None else base64.b64encode(os.urandom(KEY_BYTES)).decode()
    resolver: SecretResolver = StubResolver(material)
    return VaultKeyProvider(resolver, "env:VEOTREX_VAULT_MASTER_KEY")


def material(suffix: str = "1") -> CredentialMaterial:
    return CredentialMaterial(
        SecretStr(f"{SYNTHETIC_ACCESS}-{suffix}"), SecretStr(f"{SYNTHETIC_REFRESH}-{suffix}")
    )


# --------------------------------------------------------------------- key handling
def test_master_key_must_be_present_and_well_formed() -> None:
    assert key_provider().available() is True
    assert key_provider(base64.b64encode(os.urandom(KEY_BYTES)).decode()).available() is True
    assert VaultKeyProvider(StubResolver(None), "env:X").available() is False
    with pytest.raises(VaultKeyUnavailable, match="unavailable"):
        VaultKeyProvider(StubResolver(None), "env:X").key()
    for bad in ("", "not-base64!!", base64.b64encode(os.urandom(16)).decode(), "ab" * 8):
        with pytest.raises(VaultKeyUnavailable):
            key_provider(bad).key()
    # A 64-character hex key is accepted as an alternative encoding.
    assert len(key_provider(os.urandom(KEY_BYTES).hex()).key()) == KEY_BYTES


def test_key_provider_never_exposes_the_key_in_diagnostics() -> None:
    secret = base64.b64encode(b"K" * KEY_BYTES).decode()
    provider = key_provider(secret)
    rendered = repr(provider) + str(provider) + repr(provider.available())
    assert secret not in rendered
    assert "K" * KEY_BYTES not in rendered


# --------------------------------------------------------------------- associated data
def test_canonical_aad_binds_every_context_field() -> None:
    identifier = uuid4()
    base = canonical_aad(CONTEXT, identifier, 1, 1)
    assert base != canonical_aad(
        CredentialContext("OTHER", "ring_pending_link", CONTEXT.owner_id), identifier, 1, 1
    )
    assert base != canonical_aad(
        CredentialContext("RING", "other_kind", CONTEXT.owner_id), identifier, 1, 1
    )
    assert base != canonical_aad(
        CredentialContext("RING", "ring_pending_link", uuid4()), identifier, 1, 1
    )
    assert base != canonical_aad(CONTEXT, identifier, 2, 1)
    assert base != canonical_aad(CONTEXT, identifier, 1, 2)
    # Deterministic: same inputs always produce identical associated data.
    assert base == canonical_aad(CONTEXT, identifier, 1, 1)


def test_context_fields_containing_the_separator_are_rejected() -> None:
    hostile = CredentialContext("RING\x1fOTHER", "ring_pending_link", CONTEXT.owner_id)
    with pytest.raises(CredentialVaultError, match="not encodable"):
        canonical_aad(hostile, uuid4(), 1, 1)


# --------------------------------------------------------------------- database-backed
# conftest.py has already resolved and validated the isolated test target: VEOTREX_DATABASE_URL
# is the restricted runtime role the vault runs as in production, and VEOTREX_TEST_DATABASE_URL
# the cluster admin used only to make sure the schema exists. Re-resolving here would compare
# the value against itself and be refused, so the validated values are consumed directly.
DATABASE_URL = os.environ["VEOTREX_DATABASE_URL"]
ADMIN_DATABASE_URL = os.environ["VEOTREX_TEST_DATABASE_URL"]


def _database_available() -> bool:
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(DATABASE_URL.replace("postgresql+psycopg://", "postgresql://"))
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 5432), 1.0):
            return True
    except OSError:
        return False


pytestmark_db = pytest.mark.skipif(
    not _database_available(), reason="isolated PostgreSQL test cluster is not configured"
)


@pytest.fixture
async def vault_factory():  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from veotrex_api.models import Base, EncryptedCredential

    admin_engine = create_async_engine(ADMIN_DATABASE_URL)
    try:
        async with admin_engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all, tables=[EncryptedCredential.__table__]
            )
    finally:
        await admin_engine.dispose()
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytestmark_db
async def test_round_trip_stores_only_ciphertext(vault_factory) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from veotrex_api.models import EncryptedCredential

    vault = EncryptedCredentialVault(vault_factory, key_provider())
    stored = await vault.store_new(CONTEXT, material())
    assert stored.secret_ref.startswith(SECRET_REF_PREFIX) and stored.version == 1
    async with vault_factory() as session:
        record = await session.scalar(
            select(EncryptedCredential).where(
                EncryptedCredential.id == UUID(stored.secret_ref.removeprefix(SECRET_REF_PREFIX))
            )
        )
    assert record is not None
    blob = bytes(record.ciphertext) + bytes(record.nonce)
    assert SYNTHETIC_ACCESS.encode() not in blob
    assert SYNTHETIC_REFRESH.encode() not in blob
    opened = await vault.get(stored.secret_ref, CONTEXT)
    assert opened.material.access_token.get_secret_value() == f"{SYNTHETIC_ACCESS}-1"
    assert opened.material.refresh_token.get_secret_value() == f"{SYNTHETIC_REFRESH}-1"
    await vault.delete(stored.secret_ref, CONTEXT)
    with pytest.raises(CredentialVaultError, match="unavailable"):
        await vault.get(stored.secret_ref, CONTEXT)


@pytestmark_db
async def test_wrong_context_and_tampering_are_rejected(vault_factory) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from veotrex_api.models import EncryptedCredential

    vault = EncryptedCredentialVault(vault_factory, key_provider())
    stored = await vault.store_new(CONTEXT, material())
    identifier = UUID(stored.secret_ref.removeprefix(SECRET_REF_PREFIX))
    for wrong in (
        CredentialContext("OTHER", CONTEXT.owner_kind, CONTEXT.owner_id),
        CredentialContext(CONTEXT.provider, "other_kind", CONTEXT.owner_id),
        CredentialContext(CONTEXT.provider, CONTEXT.owner_kind, uuid4()),
    ):
        with pytest.raises(CredentialVaultError, match="context mismatch"):
            await vault.get(stored.secret_ref, wrong)
    # Tamper with ciphertext, then nonce, then the bound version: each must fail authentication.
    for mutate in ("ciphertext", "nonce", "version"):
        async with vault_factory() as session, session.begin():
            record = await session.scalar(
                select(EncryptedCredential).where(EncryptedCredential.id == identifier)
            )
            assert record is not None
            original = (bytes(record.ciphertext), bytes(record.nonce), record.version)
            if mutate == "ciphertext":
                record.ciphertext = bytes(record.ciphertext[:-1]) + bytes(
                    [record.ciphertext[-1] ^ 1]
                )
            elif mutate == "nonce":
                record.nonce = bytes([record.nonce[0] ^ 1]) + bytes(record.nonce[1:])
            else:
                record.version = record.version + 5
        with pytest.raises(CredentialVaultError, match="authenticated|context mismatch"):
            await vault.get(stored.secret_ref, CONTEXT)
        async with vault_factory() as session, session.begin():
            record = await session.scalar(
                select(EncryptedCredential).where(EncryptedCredential.id == identifier)
            )
            assert record is not None
            record.ciphertext, record.nonce, record.version = original
    # A different master key cannot open the record either.
    other_vault = EncryptedCredentialVault(vault_factory, key_provider())
    with pytest.raises(CredentialVaultError, match="authenticated"):
        await other_vault.get(stored.secret_ref, CONTEXT)
    await vault.delete(stored.secret_ref, CONTEXT)


@pytestmark_db
async def test_rotation_is_compare_and_swap_and_rejects_stale_writers(vault_factory) -> None:  # type: ignore[no-untyped-def]
    vault = EncryptedCredentialVault(vault_factory, key_provider())
    stored = await vault.store_new(CONTEXT, material("5"))
    rotated = await vault.replace_if_version(stored.secret_ref, CONTEXT, 1, material("6"))
    assert rotated.version == 2
    # A worker still holding version 1 must not overwrite the newer refresh token.
    with pytest.raises(CredentialVersionConflict, match="generation changed"):
        await vault.replace_if_version(stored.secret_ref, CONTEXT, 1, material("stale"))
    current = await vault.get(stored.secret_ref, CONTEXT)
    assert current.version == 2
    assert current.material.refresh_token.get_secret_value() == f"{SYNTHETIC_REFRESH}-6"

    async def rotate(suffix: str):  # type: ignore[no-untyped-def]
        return await vault.replace_if_version(stored.secret_ref, CONTEXT, 2, material(suffix))

    results = await asyncio.gather(rotate("a"), rotate("b"), return_exceptions=True)
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, CredentialVersionConflict) for value in results) == 1
    assert (await vault.get(stored.secret_ref, CONTEXT)).version == 3
    with pytest.raises(CredentialVaultError, match="context mismatch"):
        await vault.replace_if_version(
            stored.secret_ref,
            CredentialContext(CONTEXT.provider, CONTEXT.owner_kind, uuid4()),
            3,
            material("x"),
        )
    await vault.delete(stored.secret_ref, CONTEXT)


@pytestmark_db
async def test_unknown_reference_and_delete_semantics(vault_factory) -> None:  # type: ignore[no-untyped-def]
    vault = EncryptedCredentialVault(vault_factory, key_provider())
    for bogus in ("not-a-ref", f"{SECRET_REF_PREFIX}not-a-uuid", "vault://memory/" + str(uuid4())):
        with pytest.raises(CredentialVaultError, match="unavailable"):
            await vault.get(bogus, CONTEXT)
    # Deleting an absent credential is a no-op, not an error.
    await vault.delete(f"{SECRET_REF_PREFIX}{uuid4()}", CONTEXT)


@pytestmark_db
async def test_vault_fails_closed_without_a_key(vault_factory) -> None:  # type: ignore[no-untyped-def]
    vault = EncryptedCredentialVault(vault_factory, VaultKeyProvider(StubResolver(None), "env:X"))
    with pytest.raises(VaultKeyUnavailable):
        await vault.store_new(CONTEXT, material())
