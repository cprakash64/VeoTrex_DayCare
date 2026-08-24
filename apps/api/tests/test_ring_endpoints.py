import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import httpx
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.main import create_app
from veotrex_api.ring_client import RingClient


def signed_webhook(request_id: str) -> tuple[bytes, str]:
    raw = json.dumps(
        {
            "meta": {
                "version": "1.1",
                "time": "2026-08-23T12:00:00Z",
                "request_id": request_id,
                "account_id": "unknown-but-signed-account",
            },
            "data": {
                "id": "event-id",
                "type": "future_event",
                "attributes": {"source": "opaque/device", "source_type": "devices"},
            },
        },
        separators=(",", ":"),
    ).encode()
    digest = hmac.new(b"endpoint-test-secret", raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


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


async def test_webhook_boundary_is_bounded_signed_and_durable(settings: Settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    app = create_app(
        settings,
        engine,
        session_factory=factory,
        credential_vault=InMemoryCredentialVault(),
        ring_client=RingClient(settings, Secrets(), http),
        secret_resolver=Secrets(),
    )
    request_id = f"endpoint-{uuid4()}"
    raw, signature = signed_webhook(request_id)
    try:
        async with factory() as session, session.begin():
            await session.execute(text("DELETE FROM ring_webhook_inbox"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/providers/ring/webhooks")).status_code == 405
            assert (
                await client.post(
                    "/v1/providers/ring/webhooks",
                    content=b"x" * (settings.ring_webhook_body_bytes + 1),
                    headers={"content-type": "application/json", "x-signature": signature},
                )
            ).status_code == 413
            assert (
                await client.post(
                    "/v1/providers/ring/webhooks",
                    content=raw,
                    headers={"content-type": "text/plain", "x-signature": signature},
                )
            ).status_code == 415
            assert (
                await client.post(
                    "/v1/providers/ring/webhooks",
                    content=raw,
                    headers={"content-type": "application/json"},
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/v1/providers/ring/webhooks",
                    content=raw + b" ",
                    headers={"content-type": "application/json", "x-signature": signature},
                )
            ).status_code == 401
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/v1/providers/ring/webhooks",
                        content=raw,
                        headers={"content-type": "application/json", "x-signature": signature},
                    )
                    for _ in range(2)
                ]
            )
            assert [response.status_code for response in responses] == [200, 200]
            assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
            assert await app.state.ring_webhook_service.process_one()
            async with factory() as session:
                state = await session.scalar(
                    text("SELECT state FROM ring_webhook_inbox WHERE request_id = :request"),
                    {"request": request_id},
                )
                assert state == "FAILED_PERMANENT"
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM provider_events "
                            "WHERE provider_request_id = :request"
                        ),
                        {"request": request_id},
                    )
                    == 0
                )
    finally:
        await http.aclose()
        await engine.dispose()
