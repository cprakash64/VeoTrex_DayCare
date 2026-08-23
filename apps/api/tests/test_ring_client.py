import json

import httpx
import pytest
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.ring_client import RingAmbiguousResult, RingClient, RingClientError


class Resolver:
    def resolve(self, secret_ref: str) -> SecretStr:
        assert secret_ref == "env:RING_CLIENT_SECRET"  # noqa: S105
        return SecretStr("client-secret-test-value")


def token_payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "access_token": "access-test-value",
        "refresh_token": "refresh-test-value",
        "token_type": "Bearer",
        "expires_in": 14400,
        "scope": "read write",
    }
    value.update(overrides)
    return value


async def test_code_exchange_and_users_me_minimize_data(settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json=token_payload())
        return httpx.Response(
            200,
            json={
                "data": {
                    "type": "users",
                    "id": "stable-account-id",
                    "attributes": {"email": "discard@example.test", "phone": "discard"},
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingClient(settings, Resolver(), http)
    tokens = await client.exchange_authorization_code(SecretStr("one-time-code"))
    assert tokens.expires_in == 14400
    assert tokens.scopes == ("read", "write")
    assert await client.get_account_id(tokens.access_token) == "stable-account-id"
    body = requests[0].content.decode()
    assert "grant_type=authorization_code" in body
    assert "one-time-code" in body
    await http.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        token_payload(access_token=None),
        token_payload(refresh_token=None),
        token_payload(expires_in=0),
        token_payload(expires_in="14400"),
        token_payload(token_type="Basic"),  # noqa: S106
        token_payload(scope={"bad": "shape"}),
    ],
)
async def test_malformed_token_responses_fail_closed(
    settings: Settings, payload: dict[str, object]
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingClientError, match="malformed_token_response") as caught:
        await client.exchange_authorization_code(SecretStr("never-in-error"))
    assert "never-in-error" not in str(caught.value)
    assert "access-test-value" not in str(caught.value)
    await http.aclose()


@pytest.mark.parametrize("status", [400, 403, 404, 429])
async def test_ring_4xx_are_structured_and_secret_free(settings: Settings, status: int) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=json.dumps({"secret": "not-logged"}))
        )
    )
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingClientError) as caught:
        await client.exchange_authorization_code(SecretStr("code-never-logged"))
    assert caught.value.status_code == status
    assert "code-never-logged" not in str(caught.value)
    assert "not-logged" not in str(caught.value)
    await http.aclose()


async def test_mutating_timeout_and_5xx_are_ambiguous_without_retry(settings: Settings) -> None:
    calls = 0

    def timeout(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("unsafe detail")

    http = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingAmbiguousResult, match="transport_failure"):
        await client.refresh(SecretStr("rotating-refresh-token"))
    assert calls == 1
    await http.aclose()

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "x"}))
    )
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingAmbiguousResult, match="provider_unavailable"):
        await client.confirm_app_integration(SecretStr("access"), "A" * 43)
    await http.aclose()


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": {"type": "users"}}, {"data": {"type": "other", "id": "x"}}],
)
async def test_users_me_rejects_malformed_json_api(
    settings: Settings, payload: dict[str, object]
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    client = RingClient(settings, Resolver(), http)
    with pytest.raises(RingClientError, match="malformed_response"):
        await client.get_account_id(SecretStr("access"))
    await http.aclose()
