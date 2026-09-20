"""Optional, Ring-API-only egress proxy.

A deployment can be on a network path that the Ring API rejects while the identical request
succeeds from elsewhere. ``ring_api_proxy_url`` lets Ring API (control-plane) requests leave
through a proxy without moving any other traffic - Ring OAuth token exchange included - off its
existing direct path, and without a process-wide ``HTTPS_PROXY`` that would also capture Auth0
JWKS fetches.

Nothing here reaches the real Ring API or any external host: the one test that exercises a real
socket connects to a loopback stub that answers nothing.
"""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.ring_client import RingAmbiguousResult, RingClient, RingClientError

BASE_SETTINGS = {
    "environment": "test",
    "database_url": "postgresql+psycopg://u:p@localhost:5432/db",
    "app_version": "0.0.0-test",
    "_env_file": None,
}
PROXY = "socks5://172.20.0.1:1081"


class Resolver:
    def resolve(self, _: str) -> SecretStr:
        return SecretStr("proxy-test-only")


def proxied_settings(**overrides: object) -> Settings:
    return Settings(**BASE_SETTINGS, ring_api_proxy_url=PROXY, **overrides)  # type: ignore[arg-type]


def token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "proxy-test-access",
            "refresh_token": "proxy-test-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "read",
        },
    )


def recording_client(
    log: list[httpx.Request], response: httpx.Response | None = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return response if response is not None else httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def split_client(
    settings: Settings,
    direct_log: list[httpx.Request],
    api_log: list[httpx.Request],
    api_body: dict,
) -> tuple[RingClient, httpx.AsyncClient, httpx.AsyncClient]:
    """A Ring client whose two transports are separately observable.

    ``api_http_client`` stands in for the proxied client: production builds that client from
    ``ring_api_proxy_url``, and the routing under test is which of the two a call reaches.
    """
    direct = recording_client(direct_log, token_response())
    api = recording_client(api_log, httpx.Response(200, json=api_body))
    return RingClient(settings, Resolver(), direct, api_http_client=api), direct, api


# A. Unset proxy: nothing changes.


async def test_unset_proxy_leaves_one_client_serving_every_ring_host(settings: Settings) -> None:
    assert settings.ring_api_proxy_url is None
    client = RingClient(settings, Resolver())
    try:
        assert client._api_http is client._http
        assert client._owns_api_client is False
        for url in (settings.ring_oauth_token_url, f"{settings.ring_api_base_url}/v1/users/me"):
            assert client._client_for(url) is client._http
    finally:
        await client.aclose()


async def test_unset_proxy_still_serves_oauth_and_api_from_an_injected_client(
    settings: Settings,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(200, json={"data": {"type": "users", "id": "unchanged-account"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingClient(settings, Resolver(), http)
    tokens = await client.exchange_authorization_code(SecretStr("one-time-code"))
    assert await client.get_account_id(tokens.access_token) == "unchanged-account"
    assert [request.url.host for request in seen] == ["oauth.ring.com", "api.amazonvision.com"]
    await http.aclose()


async def test_an_injected_client_is_never_re_routed_even_when_a_proxy_is_configured() -> None:
    """The injected transport is the caller's explicit choice, so it serves every request."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = RingClient(proxied_settings(), Resolver(), http)
    assert client._api_http is http
    assert client._client_for("https://api.amazonvision.com/v1/users/me") is http
    await http.aclose()


# B. Configured proxy: a separate Ring API client carrying the proxy.


async def test_configured_proxy_builds_a_separate_ring_api_client() -> None:
    client = RingClient(proxied_settings(), Resolver())
    try:
        assert client._api_http is not client._http
        assert client._owns_api_client is True
        assert client._client_for("https://api.amazonvision.com/v1/users/me") is client._api_http
        assert client._client_for("https://oauth.ring.com/oauth/token") is client._http
    finally:
        await client.aclose()


async def test_the_ring_api_client_carries_the_configured_proxy_and_the_direct_one_does_not() -> (
    None
):
    # HTTPX internals: the public surface exposes no accessor for a resolved proxy transport.
    # The behavioural proof is the loopback SOCKS test below; this pins the wiring directly.
    client = RingClient(proxied_settings(), Resolver())
    try:
        api_pool = client._api_http._transport_for_url(
            httpx.URL("https://api.amazonvision.com/v1/users/me")
        )._pool
        direct_pool = client._http._transport_for_url(
            httpx.URL("https://oauth.ring.com/oauth/token")
        )._pool
        assert type(api_pool).__name__ == "AsyncSOCKSProxy"
        assert type(direct_pool).__name__ == "AsyncConnectionPool"
    finally:
        await client.aclose()


async def test_the_ring_api_client_keeps_the_configured_ring_timeouts() -> None:
    settings = proxied_settings()
    client = RingClient(settings, Resolver())
    try:
        timeout = client._api_http.timeout
        assert timeout.connect == settings.ring_connect_timeout_seconds
        assert timeout.read == settings.ring_read_timeout_seconds
        assert timeout.write == settings.ring_write_timeout_seconds
    finally:
        await client.aclose()


async def test_closing_the_ring_client_closes_the_proxied_client_too() -> None:
    client = RingClient(proxied_settings(), Resolver())
    api_http = client._api_http
    await client.aclose()
    assert api_http.is_closed
    assert client._http.is_closed


# C. /v1/users/me.


async def test_users_me_uses_the_proxied_ring_api_client(settings: Settings) -> None:
    direct_log: list[httpx.Request] = []
    api_log: list[httpx.Request] = []
    client, direct, api = split_client(
        settings, direct_log, api_log, {"data": {"type": "users", "id": "proxied-account"}}
    )
    try:
        assert await client.get_account_id(SecretStr("access")) == "proxied-account"
        assert [request.url.path for request in api_log] == ["/v1/users/me"]
        assert direct_log == []
    finally:
        await direct.aclose()
        await api.aclose()


# D. Ring device inventory.


async def test_device_inventory_uses_the_proxied_ring_api_client(settings: Settings) -> None:
    direct_log: list[httpx.Request] = []
    api_log: list[httpx.Request] = []
    client, direct, api = split_client(settings, direct_log, api_log, {"data": []})
    try:
        assert await client.discover_devices(SecretStr("access")) == ()
        assert [request.url.path for request in api_log] == ["/v1/devices"]
        assert direct_log == []
    finally:
        await direct.aclose()
        await api.aclose()


async def test_app_integration_calls_use_the_proxied_ring_api_client(settings: Settings) -> None:
    direct_log: list[httpx.Request] = []
    api_log: list[httpx.Request] = []
    client, direct, api = split_client(
        settings,
        direct_log,
        api_log,
        {"data": {"type": "app-integrations", "attributes": {"status": "awaiting"}}},
    )
    try:
        await client.confirm_app_integration(SecretStr("access"), "A" * 43)
        assert [request.url.path for request in api_log] == [
            "/v1/accounts/me/app-integrations",
        ]
        assert direct_log == []
    finally:
        await direct.aclose()
        await api.aclose()


# E. OAuth token exchange never inherits the Ring API proxy.


async def test_token_exchange_and_refresh_stay_on_the_direct_client(settings: Settings) -> None:
    direct_log: list[httpx.Request] = []
    api_log: list[httpx.Request] = []
    client, direct, api = split_client(settings, direct_log, api_log, {})
    try:
        await client.exchange_authorization_code(SecretStr("one-time-code"))
        await client.refresh(SecretStr("refresh-token"))
        assert [request.url.host for request in direct_log] == ["oauth.ring.com"] * 2
        assert api_log == []
    finally:
        await direct.aclose()
        await api.aclose()


async def test_only_ring_api_traffic_reaches_the_proxy_and_oauth_does_not() -> None:
    """End-to-end over a real socket: a loopback stub standing in for the SOCKS proxy.

    The stub reads the SOCKS5 client greeting and hangs up, so the Ring API request fails as a
    transport failure - what is under test is which destination each request dialled, not the
    response. The OAuth token URL points at a closed loopback port, so a token exchange that
    had inherited the proxy would show up as a second connection to the stub.
    """
    greetings: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            greetings.append(await reader.read(3))
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = Settings(
        **BASE_SETTINGS,  # type: ignore[arg-type]
        ring_api_proxy_url=f"socks5://127.0.0.1:{port}",
        # A closed loopback port: a direct token exchange refuses at once and touches no network.
        ring_oauth_token_url="https://127.0.0.1:1/oauth/token",  # noqa: S106 - a URL
    )
    client = RingClient(settings, Resolver())
    try:
        with pytest.raises(RingClientError, match="transport_failure"):
            await client.get_account_id(SecretStr("access-never-sent"))
        assert len(greetings) == 1
        assert greetings[0][:1] == b"\x05"  # SOCKS5 version byte

        with pytest.raises(RingAmbiguousResult, match="transport_failure"):
            await client.exchange_authorization_code(SecretStr("one-time-code"))
        assert len(greetings) == 1  # unchanged: OAuth did not dial the proxy
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


# G. Invalid proxy configuration fails at settings validation.


@pytest.mark.parametrize(
    "invalid",
    [
        "not-a-url",
        "ftp://172.20.0.1:1081",
        "socks4://172.20.0.1:1081",
        "socks5://",
        "socks5://172.20.0.1:99999",
        1081,
    ],
)
def test_invalid_proxy_configuration_is_refused_at_startup(invalid: object) -> None:
    with pytest.raises(ValueError) as caught:
        Settings(**BASE_SETTINGS, ring_api_proxy_url=invalid)  # type: ignore[arg-type]
    assert "Ring API proxy URL" in str(caught.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_proxy_value_means_unset(blank: str) -> None:
    assert Settings(**BASE_SETTINGS, ring_api_proxy_url=blank).ring_api_proxy_url is None  # type: ignore[arg-type]


def test_the_proxy_url_is_kept_out_of_the_settings_repr() -> None:
    assert PROXY not in repr(proxied_settings())
