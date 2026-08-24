from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import SecretStr

from veotrex_edge_agent.qualification.models import SessionRequest, SessionResult


class QualificationEnvironmentError(RuntimeError):
    """A safe, non-credential-bearing environment diagnostic."""


class StreamBackend(Protocol):
    async def run_session(
        self,
        request: SessionRequest,
        access_token: SecretStr,
        cancel: asyncio.Event,
    ) -> SessionResult: ...


class AccessTokenProvider(Protocol):
    """Injected boundary; deployed use delegates to Stage 1B get_valid_access_token()."""

    async def access_token_for(self, request: SessionRequest) -> SecretStr: ...


class CallableAccessTokenProvider:
    def __init__(self, provider: Callable[[SessionRequest], Awaitable[SecretStr]]) -> None:
        self._provider = provider

    async def access_token_for(self, request: SessionRequest) -> SecretStr:
        result = await self._provider(request)
        if not isinstance(result, SecretStr):
            raise QualificationEnvironmentError("credential provider returned an invalid type")
        return result
