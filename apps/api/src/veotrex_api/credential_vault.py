import asyncio
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import SecretStr


class CredentialVaultError(Exception):
    pass


class CredentialVersionConflict(CredentialVaultError):
    pass


@dataclass(frozen=True, slots=True)
class CredentialContext:
    provider: str
    owner_kind: str
    owner_id: UUID
    # The tenant on whose behalf the operation runs, when one is known. It is the RLS context the
    # production vault presents to PostgreSQL so the credential functions can authorize the
    # caller; it is deliberately excluded from equality, because the binding of a credential is
    # (provider, owner_kind, owner_id) and pre-tenant operations carry no tenant at all.
    tenant_id: UUID | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    access_token: SecretStr
    refresh_token: SecretStr


@dataclass(frozen=True, slots=True)
class VaultCredential:
    secret_ref: str
    version: int
    material: CredentialMaterial


class CredentialVault(Protocol):
    async def store_new(
        self, context: CredentialContext, material: CredentialMaterial
    ) -> VaultCredential: ...

    async def get(self, secret_ref: str, context: CredentialContext) -> VaultCredential: ...

    async def replace_if_version(
        self,
        secret_ref: str,
        context: CredentialContext,
        expected_version: int,
        material: CredentialMaterial,
    ) -> VaultCredential: ...

    async def delete(self, secret_ref: str, context: CredentialContext) -> None: ...


class InMemoryCredentialVault:
    """Isolated test/local adapter; intentionally non-persistent and not production capable."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[CredentialContext, int, CredentialMaterial]] = {}
        self._lock = asyncio.Lock()

    async def store_new(
        self, context: CredentialContext, material: CredentialMaterial
    ) -> VaultCredential:
        async with self._lock:
            secret_ref = f"vault://memory/{uuid4()}"
            self._entries[secret_ref] = (context, 1, material)
            return VaultCredential(secret_ref, 1, material)

    async def get(self, secret_ref: str, context: CredentialContext) -> VaultCredential:
        async with self._lock:
            try:
                stored_context, version, material = self._entries[secret_ref]
            except KeyError as exc:
                raise CredentialVaultError("credential is unavailable") from exc
            if stored_context != context:
                raise CredentialVaultError("credential context mismatch")
            return VaultCredential(secret_ref, version, material)

    async def replace_if_version(
        self,
        secret_ref: str,
        context: CredentialContext,
        expected_version: int,
        material: CredentialMaterial,
    ) -> VaultCredential:
        async with self._lock:
            try:
                stored_context, version, _ = self._entries[secret_ref]
            except KeyError as exc:
                raise CredentialVaultError("credential is unavailable") from exc
            if stored_context != context:
                raise CredentialVaultError("credential context mismatch")
            if version != expected_version:
                raise CredentialVersionConflict("credential generation changed")
            replacement = (context, version + 1, material)
            self._entries[secret_ref] = replacement
            return VaultCredential(secret_ref, version + 1, material)

    async def delete(self, secret_ref: str, context: CredentialContext) -> None:
        async with self._lock:
            entry = self._entries.get(secret_ref)
            if entry is None:
                return
            if entry[0] != context:
                raise CredentialVaultError("credential context mismatch")
            del self._entries[secret_ref]


class UnavailableCredentialVault:
    """Production fail-closed adapter until a managed secret backend is configured."""

    @staticmethod
    def _unavailable() -> CredentialVaultError:
        return CredentialVaultError("managed credential vault is not configured")

    async def store_new(
        self, context: CredentialContext, material: CredentialMaterial
    ) -> VaultCredential:
        raise self._unavailable()

    async def get(self, secret_ref: str, context: CredentialContext) -> VaultCredential:
        raise self._unavailable()

    async def replace_if_version(
        self,
        secret_ref: str,
        context: CredentialContext,
        expected_version: int,
        material: CredentialMaterial,
    ) -> VaultCredential:
        raise self._unavailable()

    async def delete(self, secret_ref: str, context: CredentialContext) -> None:
        raise self._unavailable()
