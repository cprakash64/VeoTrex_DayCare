"""V1-DEMO-03B: server-side Ring WHEP client methods. No network: httpx.MockTransport only."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests_edge_fixtures import ANSWER, OFFER, Secrets

from veotrex_api.config import Settings
from veotrex_api.ring_client import (
    RingAmbiguousResult,
    RingClient,
    RingClientError,
    RingWhepSession,
    validate_sdp_answer,
)

TOKEN = SecretStr("synthetic-ring-access-for-client-tests")
DEVICE = "synthetic-device-0001"
SESSION = f"/v1/devices/{DEVICE}/media/streaming/whep/sessions/synthetic-7"


def client_with(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response]
) -> tuple[RingClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return RingClient(settings, Secrets(), http), seen


async def test_create_sends_exactly_the_documented_request_once(settings: Settings) -> None:
    client, seen = client_with(
        settings,
        lambda _: httpx.Response(
            201, content=ANSWER, headers={"content-type": "application/sdp", "location": SESSION}
        ),
    )
    session = await client.create_whep_session(TOKEN, DEVICE, "3", OFFER, max_answer_bytes=65536)
    assert isinstance(session, RingWhepSession)
    assert session.answer_sdp == ANSWER.decode()
    assert session.session_url == f"https://api.amazonvision.com:443{SESSION}"
    assert "synthetic-7" not in repr(session) and "v=0" not in repr(session)
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://api.amazonvision.com/v1/devices/{DEVICE}/media/streaming/whep/sessions"
        "?component_id=3"
    )
    assert request.headers["authorization"] == f"Bearer {TOKEN.get_secret_value()}"
    assert request.headers["content-type"] == "application/sdp"
    assert request.headers["accept"] == "application/sdp"
    assert request.content == OFFER


@pytest.mark.parametrize("device", ["", "a/b", "../x", "a b", "a?b", "x" * 257])
async def test_unsafe_provider_identities_are_refused_before_any_request(
    settings: Settings, device: str
) -> None:
    client, seen = client_with(settings, lambda _: httpx.Response(201))
    with pytest.raises(RingClientError) as caught:
        await client.create_whep_session(TOKEN, device, None, OFFER, max_answer_bytes=65536)
    assert caught.value.category == "unsupported_provider_identity"
    with pytest.raises(RingClientError):
        await client.create_whep_session(
            TOKEN, DEVICE, "bad component", OFFER, max_answer_bytes=65536
        )
    assert seen == []


@pytest.mark.parametrize(
    ("status", "category", "ambiguous"),
    [
        (401, "unauthorized", False),
        (403, "forbidden", False),
        (404, "not_found", False),
        (429, "rate_limited", False),
        (400, "provider_rejected", False),
        (302, "redirect_refused", False),
        (500, "provider_unavailable", True),
        (503, "provider_unavailable", True),
        (204, None, None),
    ],
)
async def test_status_codes_map_to_bounded_categories(
    settings: Settings, status: int, category: str | None, ambiguous: bool | None
) -> None:
    client, seen = client_with(
        settings,
        lambda _: httpx.Response(
            status,
            content=b"secret-looking provider body",
            headers={"location": "https://elsewhere.example/x", "x-amzn-requestid": "req-1"},
        ),
    )
    with capture_logs() as logs, pytest.raises(RingClientError) as caught:
        await client.create_whep_session(TOKEN, DEVICE, None, OFFER, max_answer_bytes=65536)
    error = caught.value
    if category is not None:
        assert error.category == category
        assert isinstance(error, RingAmbiguousResult) is ambiguous
    assert len(seen) == 1, "the client itself never retries"
    for text_value in (str(error), repr(error), repr(logs)):
        assert TOKEN.get_secret_value() not in text_value
        assert "secret-looking" not in text_value and "elsewhere" not in text_value
    assert any(entry.get("provider_request_id") == "req-1" for entry in logs)


async def test_transport_failure_on_create_is_ambiguous(settings: Settings) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic")

    client, seen = client_with(settings, fail)
    with pytest.raises(RingAmbiguousResult) as caught:
        await client.create_whep_session(TOKEN, DEVICE, None, OFFER, max_answer_bytes=65536)
    assert caught.value.category == "transport_failure" and len(seen) == 1


async def test_an_oversized_answer_is_abandoned_and_the_session_released(
    settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            201, content=b"v=0\r\n" + b"a=x\r\n" * 5000, headers={"location": SESSION}
        )

    client, seen = client_with(settings, handler)
    with pytest.raises(RingClientError) as caught:
        await client.create_whep_session(TOKEN, DEVICE, None, OFFER, max_answer_bytes=4096)
    assert caught.value.category == "response_too_large"
    assert [request.method for request in seen] == ["POST", "DELETE"]
    assert seen[1].url.path == SESSION


async def test_a_missing_location_is_accepted_without_a_teardown_resource(
    settings: Settings,
) -> None:
    client, _ = client_with(
        settings,
        lambda _: httpx.Response(200, content=ANSWER, headers={"content-type": "application/sdp"}),
    )
    session = await client.create_whep_session(TOKEN, DEVICE, None, OFFER, max_answer_bytes=65536)
    assert session.session_url is None


@pytest.mark.parametrize(
    "location",
    [
        "https://api.amazonvision.com:8443" + SESSION,
        "https://evil.example" + SESSION,
        "http://api.amazonvision.com" + SESSION,
        "https://x@api.amazonvision.com" + SESSION,
        SESSION + "#frag",
        SESSION + "/../../x",
        "/v1/devices/other/media/streaming/whep/sessions/s",
        "/v1/devices/" + DEVICE + "/media/streaming/whep/sessions/",
        "//evil.example" + SESSION,
        "x" * 2000,
    ],
)
async def test_an_untrusted_location_is_refused_and_never_contacted(
    settings: Settings, location: str
) -> None:
    client, seen = client_with(
        settings, lambda _: httpx.Response(201, content=ANSWER, headers={"location": location})
    )
    with pytest.raises(RingClientError) as caught:
        await client.create_whep_session(TOKEN, DEVICE, None, OFFER, max_answer_bytes=65536)
    assert caught.value.category == "invalid_location"
    assert [request.method for request in seen] == ["POST"]


async def test_delete_accepts_2xx_and_404_and_revalidates_the_resource(settings: Settings) -> None:
    statuses = iter([204, 404, 200])
    client, seen = client_with(settings, lambda _: httpx.Response(next(statuses)))
    url = f"https://api.amazonvision.com:443{SESSION}"
    for _ in range(3):
        await client.delete_whep_session(TOKEN, url)
    assert all(request.method == "DELETE" for request in seen)
    assert all(
        request.headers["authorization"] == f"Bearer {TOKEN.get_secret_value()}" for request in seen
    )
    with pytest.raises(RingClientError):
        await client.delete_whep_session(TOKEN, "https://evil.example" + SESSION)
    assert len(seen) == 3


async def test_delete_failures_are_categorized_and_not_ambiguous(settings: Settings) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic")

    client, _ = client_with(settings, fail)
    with pytest.raises(RingClientError) as caught:
        await client.delete_whep_session(TOKEN, SESSION)
    assert caught.value.category == "transport_failure"
    assert not isinstance(caught.value, RingAmbiguousResult)
    client, _ = client_with(settings, lambda _: httpx.Response(401))
    with pytest.raises(RingClientError) as caught:
        await client.delete_whep_session(TOKEN, SESSION)
    assert caught.value.category == "unauthorized"


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"", "application/sdp"),
        (ANSWER, "text/plain"),
        (b"\xff\xfe", "application/sdp"),
        (b"m=video 9 RTP 96\r\n", "application/sdp"),
        (b"v=0\r\nm=audio 9 RTP/AVP 0\r\n", "application/sdp"),
        (b"v=0\r\nm=video 9 RTP 96\r\nm=application 9 DTLS/SCTP 5000\r\n", None),
        (b"v=0\r\nm=video 9 RTP 96\r\nBAD LINE\r\n", None),
    ],
)
def test_answer_validation_rejects_malformed_sdp(body: bytes, content_type: str | None) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        validate_sdp_answer(body, content_type, 65536)


def test_answer_validation_accepts_video_and_rejected_other_media() -> None:
    assert validate_sdp_answer(ANSWER, "application/sdp; charset=utf-8", 65536)
    assert validate_sdp_answer(
        b"v=0\r\nm=video 9 RTP 96\r\nm=audio 0 RTP/AVP 0\r\n", None, 65536
    ).startswith("v=0")
