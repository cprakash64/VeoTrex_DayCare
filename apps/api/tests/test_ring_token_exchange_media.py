"""Transport compatibility for Ring's token-exchange POST.

Ring delivers the authorization code from a Java HTTP client whose request did not match the
single media type the endpoint originally required, and the middleware refused it with 415
before the route ever ran. These tests pin the accepted shapes, keep the allowlist closed, and
assert the authorization code never reaches a log record.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.main import create_app
from veotrex_api.request_media import (
    MalformedBody,
    UnsupportedMedia,
    canonical_code_payload,
    normalize_media_type,
)
from veotrex_api.ring_service import TokenReceiptState

SYNTHETIC_CODE = "synthetic-authorization-code-not-from-ring"
PATH = "/v1/integrations/ring/token-exchange"


class StubRingService:
    """Stands in for RingLinkService so the transport layer is tested without Ring or a database."""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def receive_authorization_code(self, code: SecretStr) -> TokenReceiptState:
        self.received.append(code.get_secret_value())
        return TokenReceiptState.UNCLAIMED


class Secrets:
    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr("media-test-secret")


def build(settings: Settings) -> tuple[AsyncClient, StubRingService]:
    app = create_app(
        settings,
        credential_vault=InMemoryCredentialVault(),
        secret_resolver=Secrets(),
    )
    stub = StubRingService()
    app.state.ring_service = stub
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
    return client, stub


# --------------------------------------------------------------- media type normalisation


def test_media_type_normalisation_strips_parameters_and_case() -> None:
    assert normalize_media_type("application/json") == "application/json"
    assert normalize_media_type("application/json; charset=UTF-8") == "application/json"
    assert normalize_media_type("APPLICATION/JSON") == "application/json"
    assert normalize_media_type(" application/x-www-form-urlencoded ; q=1") == (
        "application/x-www-form-urlencoded"
    )
    assert normalize_media_type(None) == ""
    assert normalize_media_type("") == ""


# ------------------------------------------------------------------- canonical payload


@pytest.mark.parametrize(
    "media_type,body",
    [
        ("application/json", json.dumps({"code": SYNTHETIC_CODE}).encode()),
        ("application/x-www-form-urlencoded", f"code={SYNTHETIC_CODE}".encode()),
        ("", json.dumps({"code": SYNTHETIC_CODE}).encode()),
        ("", f"code={SYNTHETIC_CODE}".encode()),
    ],
)
def test_every_accepted_shape_yields_the_same_canonical_payload(
    media_type: str, body: bytes
) -> None:
    assert (
        canonical_code_payload(media_type, body)
        == json.dumps({"code": SYNTHETIC_CODE}, separators=(",", ":")).encode()
    )


def test_fields_beyond_the_code_are_ignored_not_rejected() -> None:
    """An additive change on Ring's side must not break linking."""
    document = json.dumps({"code": SYNTHETIC_CODE, "state": "x", "scope": "y"}).encode()
    assert b"state" not in canonical_code_payload("application/json", document)
    form = f"code={SYNTHETIC_CODE}&state=x".encode()
    assert b"state" not in canonical_code_payload("application/x-www-form-urlencoded", form)


@pytest.mark.parametrize("media_type", ["text/plain", "application/xml", "multipart/form-data"])
def test_allowlist_stays_closed(media_type: str) -> None:
    with pytest.raises(UnsupportedMedia):
        canonical_code_payload(media_type, b"code=x")


@pytest.mark.parametrize(
    "media_type,body",
    [
        ("application/json", b"{}"),
        ("application/json", b'{"code": ""}'),
        ("application/json", b'{"code": 7}'),
        ("application/json", b"not json"),
        ("application/json", b'["code"]'),
        ("application/x-www-form-urlencoded", b"state=x"),
        ("application/x-www-form-urlencoded", b"code="),
        ("application/x-www-form-urlencoded", b"code=a&code=b"),
        ("", b"\xff\xfe\x00"),
    ],
)
def test_unusable_bodies_are_malformed(media_type: str, body: bytes) -> None:
    with pytest.raises(MalformedBody):
        canonical_code_payload(media_type, body)


def test_oversized_code_is_rejected() -> None:
    with pytest.raises(MalformedBody):
        canonical_code_payload("application/x-www-form-urlencoded", b"code=" + b"a" * 2049)


# ------------------------------------------------------------------------ endpoint behaviour


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "application/json"}, json.dumps({"code": SYNTHETIC_CODE})),
        (
            {"content-type": "application/json; charset=utf-8"},
            json.dumps({"code": SYNTHETIC_CODE}),
        ),
        (
            {"content-type": "application/x-www-form-urlencoded"},
            f"code={SYNTHETIC_CODE}",
        ),
        (
            {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
            f"code={SYNTHETIC_CODE}",
        ),
        ({}, f"code={SYNTHETIC_CODE}"),
        ({}, json.dumps({"code": SYNTHETIC_CODE})),
    ],
)
async def test_ring_transport_shapes_reach_the_service(
    settings: Settings, headers: dict[str, str], body: str
) -> None:
    client, stub = build(settings)
    async with client:
        response = await client.post(PATH, headers=headers, content=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == TokenReceiptState.UNCLAIMED.value
    assert stub.received == [SYNTHETIC_CODE]


async def test_unsupported_media_type_is_still_refused(settings: Settings) -> None:
    client, stub = build(settings)
    async with client:
        response = await client.post(
            PATH, headers={"content-type": "text/plain"}, content=f"code={SYNTHETIC_CODE}"
        )
    assert response.status_code == 415
    assert stub.received == []


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "application/json"}, "{}"),
        ({"content-type": "application/x-www-form-urlencoded"}, "code=a&code=b"),
        ({"content-type": "application/x-www-form-urlencoded"}, "state=x"),
    ],
)
async def test_unusable_bodies_never_reach_the_service(
    settings: Settings, headers: dict[str, str], body: str
) -> None:
    client, stub = build(settings)
    async with client:
        response = await client.post(PATH, headers=headers, content=body)
    assert response.status_code == 422
    assert stub.received == []


async def test_oversized_request_is_refused(settings: Settings) -> None:
    client, stub = build(settings)
    oversized = "code=" + "a" * (settings.ring_token_exchange_body_bytes + 64)
    async with client:
        response = await client.post(
            PATH,
            headers={"content-type": "application/x-www-form-urlencoded"},
            content=oversized,
        )
    assert response.status_code == 413
    assert stub.received == []


async def test_authorization_code_never_appears_in_logs(settings: Settings) -> None:
    client, _ = build(settings)
    async with client:
        with capture_logs() as entries:
            response = await client.post(
                PATH,
                headers={"content-type": "application/x-www-form-urlencoded"},
                content=f"code={SYNTHETIC_CODE}",
            )
    assert response.status_code == 200
    rendered = json.dumps(entries, default=str)
    assert SYNTHETIC_CODE not in rendered
    observed = [entry for entry in entries if entry.get("event") == "provider_request"]
    assert observed, "the transport diagnostic did not record the request"
    assert observed[-1]["media_type"] == "application/x-www-form-urlencoded"
    assert observed[-1]["status_code"] == 200


@pytest.mark.parametrize(
    "body",
    [f"code={SYNTHETIC_CODE}", json.dumps({"code": SYNTHETIC_CODE})],
)
async def test_absent_content_type_is_genuinely_absent(settings: Settings, body: str) -> None:
    """Guards the probe itself, not just the handler.

    A client that quietly supplies a default Content-Type would turn this into a test of
    form-encoding and leave the header-less path - the one Java's HttpClient actually produces -
    silently uncovered. The recorded media type is asserted to be empty so that cannot happen.
    """
    client, stub = build(settings)
    async with client:
        with capture_logs() as entries:
            response = await client.post(PATH, content=body)
    assert response.status_code == 200, response.text
    assert stub.received == [SYNTHETIC_CODE]
    observed = [entry for entry in entries if entry.get("event") == "provider_request"]
    assert observed, "the transport diagnostic did not record the request"
    assert observed[-1]["media_type"] == "", (
        "the request carried a Content-Type; this test was not exercising the absent-header path"
    )
