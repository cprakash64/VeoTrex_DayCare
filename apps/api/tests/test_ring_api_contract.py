"""V1-01A-2: the exact HTTP requests VeoTrex sends to the Ring Partner API.

Pinned against the official documentation (developer.amazon.com/docs/ring/api-documentation,
re-read 2026-09-22) and Amazon's official sample (AmazonAppDev/ring-api-helloworld). Every
request is captured by a mock transport and compared field by field: method, path, query,
headers, body. Tokens are synthetic and never real; nothing here contacts Ring.
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import structlog
from pydantic import SecretStr

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.config import Settings
from veotrex_api.ring_client import (
    JSON_MEDIA_TYPE,
    RingAmbiguousResult,
    RingClient,
    RingClientError,
    safe_error_summary,
    user_agent,
)
from veotrex_api.ring_service import ACCOUNT_IDENTIFIER_MAX, partner_account_identifier

SYNTHETIC_ACCESS = "synthetic-access-token-Ab12.-_~+/="
NONCE = "yT8jdW_nu2W4gR6FI-l8hkPpt_c9EAf4DJ9CTIcuM7c"
MASKED = "u***r@partner.example.com"


class Resolver:
    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr("client-secret-test-value")


class Capture:
    def __init__(self, responder: object) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        responder = self._responder
        return responder(request) if callable(responder) else responder  # type: ignore[no-any-return,operator]


def client_with(settings: Settings, capture: Capture) -> tuple[RingClient, httpx.AsyncClient]:
    # The transport is injected but the client is built by RingClient's own constructor path
    # for headers: replicate production by constructing the AsyncClient the same way.
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(capture),
        headers={"User-Agent": user_agent(settings.app_version)},
    )
    return RingClient(settings, Resolver(), http), http


def users_me_document() -> dict[str, object]:
    return {
        "data": {
            "type": "users",
            "id": "ava1.ring.account.SYNTHETIC",
            "attributes": {
                "first_name": "Discard",
                "last_name": "Me",
                "email": "discard@example.test",
                "phone_number": "+10000000000",
            },
        }
    }


def integration_document(status: str, **attributes: str) -> dict[str, object]:
    return {
        "data": {
            "type": "app-integrations",
            "id": "ava1.ring.client.SYNTHETIC",
            "attributes": {"status": status, **attributes},
        },
        "meta": {"time": "2026-07-21T07:07:15Z"},
    }


# ------------------------------------------------------------------------------ users/me


async def test_users_me_request_matches_the_documented_contract(settings: Settings) -> None:
    capture = Capture(httpx.Response(200, json=users_me_document()))
    client, http = client_with(settings, capture)
    try:
        account_id = await client.get_account_id(SecretStr(SYNTHETIC_ACCESS))
    finally:
        await http.aclose()
    assert account_id == "ava1.ring.account.SYNTHETIC"
    (request,) = capture.requests
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "api.amazonvision.com"
    assert request.url.path == "/v1/users/me"
    assert request.url.query == b""
    assert request.content == b"", "the documented request has no body"
    # Authorization is exactly "Bearer <token>": raw text, no quoting, masking or whitespace.
    assert request.headers["authorization"] == f"Bearer {SYNTHETIC_ACCESS}"
    assert "SecretStr" not in request.headers["authorization"]
    assert "*" not in request.headers["authorization"]
    # The reference's JSON API media type on both negotiation headers, nothing vendor-specific.
    assert request.headers["accept"] == JSON_MEDIA_TYPE == "application/json"
    assert request.headers["content-type"] == JSON_MEDIA_TYPE
    assert request.headers["user-agent"].startswith("VeoTrex-ControlPlane/")
    assert "python-httpx" not in request.headers["user-agent"]
    # No other application header: the full set is the documented one plus transport defaults.
    application_headers = {
        name
        for name in request.headers
        if name not in {"host", "accept-encoding", "connection", "user-agent", "content-length"}
    }
    assert application_headers == {"authorization", "accept", "content-type"}


async def test_users_me_keeps_only_the_directed_account_id(settings: Settings) -> None:
    capture = Capture(httpx.Response(200, json=users_me_document()))
    client, http = client_with(settings, capture)
    try:
        value = await client.get_account_id(SecretStr(SYNTHETIC_ACCESS))
    finally:
        await http.aclose()
    assert value == "ava1.ring.account.SYNTHETIC"
    assert isinstance(value, str)
    for pii in ("Discard", "Me", "discard@example.test", "+10000000000"):
        assert pii not in value


@pytest.mark.parametrize("status", [400, 403, 404, 406, 415])
async def test_users_me_non_2xx_is_provider_rejected_never_success(
    settings: Settings, status: int
) -> None:
    capture = Capture(
        httpx.Response(
            status,
            json={"errors": [{"status": str(status), "title": "Not Acceptable"}]},
            headers={"x-request-id": "gw-correlation-123"},
        )
    )
    client, http = client_with(settings, capture)
    try:
        with pytest.raises(RingClientError) as caught:
            await client.get_account_id(SecretStr(SYNTHETIC_ACCESS))
    finally:
        await http.aclose()
    assert caught.value.category == "provider_rejected"
    assert caught.value.status_code == status
    assert caught.value.operation == "users_me"
    assert caught.value.error_title == "Not Acceptable"
    assert caught.value.provider_request_id == "gw-correlation-123"
    assert SYNTHETIC_ACCESS not in str(caught.value)


# ----------------------------------------------------------------------- app integrations


async def test_app_integration_post_and_patch_match_the_documented_contract(
    settings: Settings,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=integration_document("awaiting"))
        return httpx.Response(
            200,
            json=integration_document(
                "completed", account_identifier=MASKED, updated_at="2026-07-21T07:07:16Z"
            ),
        )

    capture = Capture(respond)
    client, http = client_with(settings, capture)
    try:
        await client.confirm_app_integration(SecretStr(SYNTHETIC_ACCESS), NONCE, MASKED)
        await client.complete_app_integration(SecretStr(SYNTHETIC_ACCESS), MASKED)
    finally:
        await http.aclose()
    post, patch = capture.requests
    for request in (post, patch):
        assert request.url.host == "api.amazonvision.com"
        assert request.url.path == "/v1/accounts/me/app-integrations"
        assert request.url.query == b""
        assert request.headers["authorization"] == f"Bearer {SYNTHETIC_ACCESS}"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json"
    assert post.method == "POST"
    assert json.loads(post.content) == {"account_identifier": MASKED, "nonce": NONCE}
    assert patch.method == "PATCH"
    assert json.loads(patch.content) == {"account_identifier": MASKED, "status": "completed"}


async def test_app_integration_without_identifier_sends_only_required_fields(
    settings: Settings,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=integration_document("awaiting" if request.method == "POST" else "completed"),
        )

    capture = Capture(respond)
    client, http = client_with(settings, capture)
    try:
        await client.confirm_app_integration(SecretStr(SYNTHETIC_ACCESS), NONCE)
        await client.complete_app_integration(SecretStr(SYNTHETIC_ACCESS))
    finally:
        await http.aclose()
    post, patch = capture.requests
    assert json.loads(post.content) == {"nonce": NONCE}
    assert json.loads(patch.content) == {"status": "completed"}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "account_identifier": MASKED,
            "status": "completed",
            "updated_at": "x",
        },  # flat, not JSON:API
        integration_document("awaiting"),  # wrong status for PATCH
        {"data": {"type": "users", "id": "x", "attributes": {"status": "completed"}}},
        {"data": {"type": "app-integrations", "id": "x"}},
        {},
    ],
)
async def test_app_integration_patch_requires_a_completed_json_api_envelope(
    settings: Settings, payload: dict[str, object]
) -> None:
    capture = Capture(httpx.Response(200, json=payload))
    client, http = client_with(settings, capture)
    try:
        with pytest.raises(RingClientError, match="malformed_response"):
            await client.complete_app_integration(SecretStr(SYNTHETIC_ACCESS), MASKED)
    finally:
        await http.aclose()


async def test_app_integration_failure_semantics_are_preserved(settings: Settings) -> None:
    # 4xx: definitive rejection with safe diagnostics; never retried.
    capture = Capture(
        httpx.Response(400, json={"errors": [{"status": "400", "title": "Invalid Nonce"}]})
    )
    client, http = client_with(settings, capture)
    try:
        with pytest.raises(RingClientError) as rejected:
            await client.confirm_app_integration(SecretStr(SYNTHETIC_ACCESS), NONCE, MASKED)
    finally:
        await http.aclose()
    assert rejected.value.category == "provider_rejected"
    assert rejected.value.error_title == "Invalid Nonce"
    assert len(capture.requests) == 1
    # 5xx and transport failures on a mutating call are ambiguous and never replayed.
    capture = Capture(httpx.Response(502, text="gateway"))
    client, http = client_with(settings, capture)
    try:
        with pytest.raises(RingAmbiguousResult, match="provider_unavailable"):
            await client.complete_app_integration(SecretStr(SYNTHETIC_ACCESS), MASKED)
    finally:
        await http.aclose()
    assert len(capture.requests) == 1

    def drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("dropped")

    capture = Capture(drop)
    client, http = client_with(settings, capture)
    try:
        with pytest.raises(RingAmbiguousResult, match="transport_failure"):
            await client.confirm_app_integration(SecretStr(SYNTHETIC_ACCESS), NONCE, MASKED)
    finally:
        await http.aclose()
    assert len(capture.requests) == 1


# ------------------------------------------------------------------ safe diagnostics/logging


def test_safe_error_summary_keeps_only_title_and_correlation_id() -> None:
    response = httpx.Response(
        406,
        json={
            "errors": [
                {
                    "status": "406",
                    "title": "Not Acceptable☃ " + "x" * 200,
                    "detail": "access_token=leak refresh_token=leak nonce=leak",
                }
            ],
            "access_token": "leak",
        },
        headers=[(b"x-amzn-requestid", b"abc-123\xc3\xa9"), (b"authorization", b"Bearer leak")],
    )
    title, request_id = safe_error_summary(response)
    assert title is not None and title.startswith("Not Acceptable ")
    assert len(title) <= 80 and title.isascii()
    assert request_id == "abc-123"
    assert "leak" not in (title or "") and "leak" not in (request_id or "")
    assert safe_error_summary(httpx.Response(406, text="<html>not json</html>")) == (None, None)
    assert safe_error_summary(httpx.Response(406, json={"errors": "weird"})) == (None, None)
    assert safe_error_summary(httpx.Response(406, json={"errors": [{"code": "NA"}]})) == (
        "NA",
        None,
    )


async def test_provider_failure_log_event_is_bounded_and_secret_free(settings: Settings) -> None:
    events: list[dict[str, object]] = []

    def sink(_: object, __: str, event: dict[str, object]) -> dict[str, object]:
        events.append(dict(event))
        return event

    previous = structlog.get_config()
    structlog.configure(processors=[sink, structlog.processors.KeyValueRenderer()])
    try:
        capture = Capture(
            httpx.Response(
                406,
                json={"errors": [{"status": "406", "title": "Not Acceptable"}], "tok": "leak"},
                headers={"x-request-id": "corr-1"},
            )
        )
        client, http = client_with(settings, capture)
        try:
            with pytest.raises(RingClientError):
                await client.get_account_id(SecretStr(SYNTHETIC_ACCESS))
        finally:
            await http.aclose()
    finally:
        structlog.configure(**previous)
    failures = [event for event in events if event.get("event") == "ring_provider_request_failed"]
    assert len(failures) == 1
    event = failures[0]
    assert event["operation"] == "users_me"
    assert event["category"] == "provider_rejected"
    assert event["status_code"] == 406
    assert event["error_title"] == "Not Acceptable"
    assert event["provider_request_id"] == "corr-1"
    rendered = repr(event)
    for forbidden in (SYNTHETIC_ACCESS, "leak", "Bearer", "authorization", "nonce"):
        assert forbidden not in rendered
    assert set(event) == {
        "event",
        "operation",
        "category",
        "status_code",
        "error_title",
        "provider_request_id",
    }


# ----------------------------------------------------------------- account identifier


def principal(
    display_name: str | None, actor_id: str = "0123456789abcdef0123456789abcdef"
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        issuer="https://tenant.auth0.example/",
        subject="auth0|secret-subject",
        external_organization_id="org_secret",
        actor_id=UUID(actor_id),
        tenant_id=UUID("11111111-1111-4111-8111-111111111111"),
        display_name=display_name,
        grants=(RoleGrant(Role.TENANT_OWNER, None),),
        permissions=frozenset(Permission),
    )


def test_partner_account_identifier_is_masked_deterministic_and_bounded() -> None:
    assert partner_account_identifier(principal("Chandra Pandey")) == "C***y@veotrex"
    assert partner_account_identifier(principal("Chandra Pandey")) == partner_account_identifier(
        principal("Chandra Pandey")
    )
    # Non-printable and non-ASCII characters never reach Ring; whitespace is not a character.
    assert partner_account_identifier(principal("  Zoë ☃ Q  ")) == "Z***Q@veotrex"
    # Too short or absent: a masked actor id, never the subject, organization or tenant.
    for name in (None, "", "A", "   "):
        value = partner_account_identifier(principal(name))
        assert value == "01***ef@veotrex"
    long_name = "N" * 500
    value = partner_account_identifier(principal(long_name))
    assert value == "N***N@veotrex" and len(value) <= ACCOUNT_IDENTIFIER_MAX
    for forbidden in ("secret-subject", "org_secret", "11111111", "Chandra", "Pandey"):
        assert forbidden not in partner_account_identifier(principal("Chandra Pandey"))
        assert forbidden not in value


def test_user_agent_is_a_bounded_product_token() -> None:
    assert user_agent("0.1.0-staging") == "VeoTrex-ControlPlane/0.1.0-staging"
    assert user_agent("bad (version) <x>") == "VeoTrex-ControlPlane/badversionx"
    assert user_agent("") == "VeoTrex-ControlPlane/unknown"
    assert len(user_agent("v" * 100)) <= len("VeoTrex-ControlPlane/") + 32
