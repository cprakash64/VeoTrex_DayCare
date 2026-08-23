import asyncio
from uuid import uuid4

import pytest
from pydantic import SecretStr

from veotrex_api.credential_vault import (
    CredentialContext,
    CredentialMaterial,
    CredentialVaultError,
    CredentialVersionConflict,
    InMemoryCredentialVault,
)


async def test_vault_context_binding_cas_and_secure_delete() -> None:
    vault = InMemoryCredentialVault()
    context = CredentialContext("RING", "ring_pending_link", uuid4())
    other = CredentialContext("RING", "ring_pending_link", uuid4())
    first = await vault.store_new(
        context, CredentialMaterial(SecretStr("access-1"), SecretStr("refresh-1"))
    )
    assert first.version == 1
    with pytest.raises(CredentialVaultError, match="context mismatch"):
        await vault.get(first.secret_ref, other)

    async def rotate(suffix: str):
        return await vault.replace_if_version(
            first.secret_ref,
            context,
            1,
            CredentialMaterial(SecretStr(f"access-{suffix}"), SecretStr(f"refresh-{suffix}")),
        )

    results = await asyncio.gather(rotate("a"), rotate("b"), return_exceptions=True)
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, CredentialVersionConflict) for value in results) == 1
    assert (await vault.get(first.secret_ref, context)).version == 2
    await vault.delete(first.secret_ref, context)
    with pytest.raises(CredentialVaultError, match="unavailable"):
        await vault.get(first.secret_ref, context)
