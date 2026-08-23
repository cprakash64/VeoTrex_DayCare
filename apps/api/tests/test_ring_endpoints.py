from uuid import uuid4

import httpx
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.main import create_app
from veotrex_api.ring_client import RingClient


class Secrets:
    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr("endpoint-test-secret")


async def test_token_exchange_boundary_and_unauthenticated_claim(settings: Settings) -> None:
    account_id = f"ring-{uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "endpoint-access",
                    "refresh_token": "endpoint-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "integration",
                },
            )
        return httpx.Response(200, json={"data": {"type": "users", "id": account_id}})

    engine = make_engine(settings)
    factory = make_session_factory(engine)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ring_client = RingClient(settings, Secrets(), http)
    app = create_app(
        settings,
        engine,
        session_factory=factory,
        credential_vault=InMemoryCredentialVault(),
        ring_client=ring_client,
        secret_resolver=Secrets(),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/integrations/ring/token-exchange")).status_code == 405
            assert (
                await client.post(
                    "/v1/integrations/ring/token-exchange",
                    content="code=x",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
            ).status_code == 415
            assert (
                await client.post(
                    "/v1/integrations/ring/token-exchange",
                    content=b"x" * 1025,
                    headers={"content-type": "application/json"},
                )
            ).status_code == 413
            assert (
                await client.post(
                    "/v1/integrations/ring/token-exchange",
                    json={"code": "code-value", "unexpected": True},
                )
            ).status_code == 422
            accepted = await client.post(
                "/v1/integrations/ring/token-exchange", json={"code": "code-value"}
            )
            assert accepted.status_code == 200
            assert accepted.json() == {"status": "UNCLAIMED", "connection_id": None}
            assert (
                await client.post(
                    "/v1/integrations/ring/claim",
                    json={"nonce": "A" * 43, "time": 1750000000000},
                )
            ).status_code == 401
    finally:
        await http.aclose()
        await engine.dispose()
