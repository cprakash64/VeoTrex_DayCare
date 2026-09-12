"""Production credential vault: AES-256-GCM envelope records behind the existing contract.

Implements the `CredentialVault` protocol so `RingLinkService` and `RingWebhookService` are
unchanged. Ciphertext is bound by AEAD associated data to the record's non-secret context
(schema version, provider, owner kind, owner id, credential version), so a ciphertext copied to a
different provider, owner, or version fails to decrypt rather than silently authorising.

`cryptography` (already locked via `pyjwt[crypto]`) supplies AES-256-GCM; no cryptography is
implemented here. Python cannot guarantee memory zeroisation, so this module minimises how long
plaintext is referenced but does not claim to erase it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.credential_vault import (
    CredentialContext,
    CredentialMaterial,
    CredentialVaultError,
    CredentialVersionConflict,
    VaultCredential,
)
from veotrex_api.models import EncryptedCredential
from veotrex_api.secrets import SecretResolutionError, SecretResolver

SCHEMA_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
MAX_SECRET_BYTES = 8_192
SECRET_REF_PREFIX = "vault://postgres/"  # noqa: S105 - a reference scheme, not a secret
_FIELD_SEPARATOR = b"\x1f"


class VaultKeyUnavailable(CredentialVaultError):
    """The master key is missing or unusable; the vault fails closed."""


@dataclass(frozen=True, slots=True, repr=False)
class _Envelope:
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "_Envelope(REDACTED)"


def canonical_aad(
    context: CredentialContext, credential_id: UUID, version: int, schema_version: int
) -> bytes:
    """Deterministic associated data. Order and separator are fixed; fields cannot be confused."""
    fields = (
        str(schema_version).encode(),
        context.provider.encode(),
        context.owner_kind.encode(),
        str(context.owner_id).encode(),
        str(version).encode(),
    )
    if any(_FIELD_SEPARATOR in field for field in fields):
        raise CredentialVaultError("credential context is not encodable")
    return _FIELD_SEPARATOR.join(fields)


def _encode(material: CredentialMaterial) -> bytes:
    access = material.access_token.get_secret_value().encode()
    refresh = material.refresh_token.get_secret_value().encode()
    if not access or not refresh:
        raise CredentialVaultError("credential material is incomplete")
    if len(access) > MAX_SECRET_BYTES or len(refresh) > MAX_SECRET_BYTES:
        raise CredentialVaultError("credential material exceeds the permitted size")
    if _FIELD_SEPARATOR in access or _FIELD_SEPARATOR in refresh:
        raise CredentialVaultError("credential material is not encodable")
    return access + _FIELD_SEPARATOR + refresh


def _decode(plaintext: bytes) -> CredentialMaterial:
    access, separator, refresh = plaintext.partition(_FIELD_SEPARATOR)
    if not separator or not access or not refresh:
        raise CredentialVaultError("credential material is corrupt")
    return CredentialMaterial(SecretStr(access.decode("utf-8")), SecretStr(refresh.decode("utf-8")))


class VaultKeyProvider:
    """Resolves the AEAD master key by reference. The key never enters settings or argv."""

    def __init__(self, resolver: SecretResolver, key_ref: str) -> None:
        self._resolver = resolver
        self._key_ref = key_ref

    def key(self) -> bytes:
        try:
            raw = self._resolver.resolve(self._key_ref).get_secret_value()
        except SecretResolutionError:
            raise VaultKeyUnavailable("vault master key is unavailable") from None
        material = self._decode_key(raw)
        if len(material) != KEY_BYTES:
            raise VaultKeyUnavailable("vault master key must be 32 bytes")
        return material

    @staticmethod
    def _decode_key(raw: str) -> bytes:
        import base64
        import binascii

        candidate = raw.strip()
        try:
            if len(candidate) == KEY_BYTES * 2:
                return bytes.fromhex(candidate)
            return base64.b64decode(candidate, validate=True)
        except (ValueError, binascii.Error):
            raise VaultKeyUnavailable("vault master key is not valid hex or base64") from None

    def available(self) -> bool:
        """Readiness probe. Never returns, logs, or derives an identifier from the key."""
        try:
            self.key()
        except VaultKeyUnavailable:
            return False
        return True


class EncryptedCredentialVault:
    """PostgreSQL-backed, AEAD-encrypted implementation of the CredentialVault protocol."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        key_provider: VaultKeyProvider,
        *,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        self._factory = session_factory
        self._keys = key_provider
        self._schema_version = schema_version

    # ------------------------------------------------------------------ helpers
    def _seal(
        self, context: CredentialContext, credential_id: UUID, version: int, material: Any
    ) -> _Envelope:
        nonce = os.urandom(NONCE_BYTES)
        aad = canonical_aad(context, credential_id, version, self._schema_version)
        plaintext = _encode(material)
        try:
            ciphertext = AESGCM(self._keys.key()).encrypt(nonce, plaintext, aad)
        finally:
            del plaintext
        return _Envelope(nonce, ciphertext)

    def _open(self, record: EncryptedCredential, context: CredentialContext) -> CredentialMaterial:
        aad = canonical_aad(context, record.id, record.version, int(record.schema_version))
        try:
            plaintext = AESGCM(self._keys.key()).decrypt(
                bytes(record.nonce), bytes(record.ciphertext), aad
            )
        except InvalidTag:
            # Wrong tenant/provider/owner/version, tampered ciphertext or nonce, or wrong key.
            raise CredentialVaultError("credential could not be authenticated") from None
        try:
            return _decode(plaintext)
        finally:
            del plaintext

    @staticmethod
    def _identifier(secret_ref: str) -> UUID:
        if not secret_ref.startswith(SECRET_REF_PREFIX):
            raise CredentialVaultError("credential is unavailable")
        try:
            return UUID(secret_ref.removeprefix(SECRET_REF_PREFIX))
        except ValueError:
            raise CredentialVaultError("credential is unavailable") from None

    async def _load(
        self, session: AsyncSession, secret_ref: str, context: CredentialContext
    ) -> EncryptedCredential:
        record = await session.scalar(
            select(EncryptedCredential).where(
                EncryptedCredential.id == self._identifier(secret_ref)
            )
        )
        if record is None:
            raise CredentialVaultError("credential is unavailable")
        # Cheap non-cryptographic pre-check; AEAD remains the authority.
        if record.provider != context.provider or record.owner_kind != context.owner_kind:
            raise CredentialVaultError("credential context mismatch")
        if record.owner_id != context.owner_id:
            raise CredentialVaultError("credential context mismatch")
        return record

    # ------------------------------------------------------------------ protocol
    async def store_new(
        self, context: CredentialContext, material: CredentialMaterial
    ) -> VaultCredential:
        credential_id = uuid4()
        envelope = self._seal(context, credential_id, 1, material)
        async with self._factory() as session, session.begin():
            session.add(
                EncryptedCredential(
                    id=credential_id,
                    provider=context.provider,
                    owner_kind=context.owner_kind,
                    owner_id=context.owner_id,
                    version=1,
                    schema_version=self._schema_version,
                    nonce=envelope.nonce,
                    ciphertext=envelope.ciphertext,
                )
            )
        return VaultCredential(f"{SECRET_REF_PREFIX}{credential_id}", 1, material)

    async def get(self, secret_ref: str, context: CredentialContext) -> VaultCredential:
        async with self._factory() as session:
            record = await self._load(session, secret_ref, context)
            material = self._open(record, context)
            return VaultCredential(secret_ref, record.version, material)

    async def replace_if_version(
        self,
        secret_ref: str,
        context: CredentialContext,
        expected_version: int,
        material: CredentialMaterial,
    ) -> VaultCredential:
        """Compare-and-swap rotation. A stale writer can never overwrite a newer refresh token."""
        credential_id = self._identifier(secret_ref)
        async with self._factory() as session, session.begin():
            record = await session.scalar(
                select(EncryptedCredential)
                .where(EncryptedCredential.id == credential_id)
                .with_for_update()
            )
            if record is None:
                raise CredentialVaultError("credential is unavailable")
            if (
                record.provider != context.provider
                or record.owner_kind != context.owner_kind
                or record.owner_id != context.owner_id
            ):
                raise CredentialVaultError("credential context mismatch")
            if record.version != expected_version:
                raise CredentialVersionConflict("credential generation changed")
            next_version = record.version + 1
            envelope = self._seal(context, credential_id, next_version, material)
            record.version = next_version
            record.schema_version = self._schema_version
            record.nonce = envelope.nonce
            record.ciphertext = envelope.ciphertext
        return VaultCredential(secret_ref, next_version, material)

    async def delete(self, secret_ref: str, context: CredentialContext) -> None:
        async with self._factory() as session, session.begin():
            record = await session.scalar(
                select(EncryptedCredential).where(
                    EncryptedCredential.id == self._identifier(secret_ref)
                )
            )
            if record is None:
                return
            if (
                record.provider != context.provider
                or record.owner_kind != context.owner_kind
                or record.owner_id != context.owner_id
            ):
                raise CredentialVaultError("credential context mismatch")
            await session.execute(
                delete(EncryptedCredential).where(EncryptedCredential.id == record.id)
            )
