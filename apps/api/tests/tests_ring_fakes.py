"""Shared Ring fakes for service-level tests (no network, synthetic tokens only)."""

import asyncio

from pydantic import SecretStr

from veotrex_api.ring_client import RingClientError, RingTokenSet


class TestSecrets:
    def resolve(self, secret_ref: str) -> SecretStr:
        if "HMAC" in secret_ref:
            return SecretStr("test-hmac-key")
        return SecretStr("test-client-secret")


class FakeRingClient:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.refresh_calls = 0
        self.confirm_calls = 0
        self.complete_calls = 0
        self.account_identifiers: list[str | None] = []
        self.users_error: RingClientError | None = None
        self.confirm_error: RingClientError | None = None
        self.complete_error: RingClientError | None = None
        self.refresh_error: RingClientError | None = None

    @staticmethod
    def tokens(generation: int = 1) -> RingTokenSet:
        return RingTokenSet(
            SecretStr(f"access-{generation}"),
            SecretStr(f"refresh-{generation}"),
            3600,
            ("integration",),
        )

    async def exchange_authorization_code(self, code: SecretStr) -> RingTokenSet:
        assert code.get_secret_value().startswith("code-")
        return self.tokens()

    async def get_account_id(self, access_token: SecretStr) -> str:
        if self.users_error:
            raise self.users_error
        return self.account_id

    async def confirm_app_integration(
        self, access_token: SecretStr, nonce: str, account_identifier: str | None = None
    ) -> None:
        self.confirm_calls += 1
        self.account_identifiers.append(account_identifier)
        if self.confirm_error:
            raise self.confirm_error

    async def complete_app_integration(
        self, access_token: SecretStr, account_identifier: str | None = None
    ) -> None:
        self.complete_calls += 1
        self.account_identifiers.append(account_identifier)
        if self.complete_error:
            raise self.complete_error

    async def refresh(self, refresh_token: SecretStr) -> RingTokenSet:
        self.refresh_calls += 1
        await asyncio.sleep(0.05)
        if self.refresh_error:
            raise self.refresh_error
        return self.tokens(2)
