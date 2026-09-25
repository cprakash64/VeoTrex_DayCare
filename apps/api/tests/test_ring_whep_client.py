"""V1-DEMO-03B: server-side Ring WHEP client methods. No network: httpx.MockTransport only."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests_edge_fixtures import ANSWER, OFFER, Secrets

from veotrex_api.config import Settings
from veotrex_api.ring_client import (
    WHEP_LOCATION_DIAGNOSTIC_FIELDS,
    RingAmbiguousResult,
    RingClient,
    RingClientError,
    RingWhepSession,
    WhepLocationRejected,
    WhepLocationRejection,
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


# ------------------------------------------------ Location rejection diagnostics (hotfix)
# Distinctive values so any leak of an identifier into a log or exception is detectable.
SECRET_DEVICE = "dev-SECRETDEVICE-77"  # noqa: S105 - synthetic
SECRET_SESSION = "sess-SECRETSESSION-4242"  # noqa: S105 - synthetic
SECRET_PATH = f"/v1/devices/{SECRET_DEVICE}/media/streaming/whep/sessions/{SECRET_SESSION}"
SAFE_EVENT_KEYS = {
    "event",
    "log_level",
    "operation",
    "status_code",
    "provider_request_id",
    "reason",
} | set(WHEP_LOCATION_DIAGNOSTIC_FIELDS)


def test_valid_absolute_and_relative_locations_still_pass(settings: Settings) -> None:
    client, seen = client_with(settings, lambda _: httpx.Response(204))
    expected = f"https://api.amazonvision.com:443{SECRET_PATH}"
    for location in (
        f"https://api.amazonvision.com{SECRET_PATH}",
        f"https://api.amazonvision.com:443{SECRET_PATH}",
        f"https://API.amazonvision.com{SECRET_PATH}",
        SECRET_PATH,
    ):
        assert client.validated_whep_location(location, device_id=SECRET_DEVICE) == expected
    assert client.validated_whep_location(SECRET_PATH + "?a=1&b=2") == expected + "?a=1&b=2"
    assert seen == []


@pytest.mark.parametrize(
    ("location", "device", "reason", "expected"),
    [
        (12345, None, "invalid_type_or_length", {}),
        ("", None, "invalid_type_or_length", {}),
        ("/" + "x" * 1024, None, "invalid_type_or_length", {}),
        (SECRET_PATH + " ", None, "non_printable", {}),
        (SECRET_PATH + "\x7f", None, "non_printable", {}),
        (SECRET_PATH + "#SECRETFRAG", None, "fragment_present", {}),
        ("https://api.amazonvision.com:99999" + SECRET_PATH, None, "malformed_absolute_url", {}),
        ("https://api.amazonvision.com:x" + SECRET_PATH, None, "malformed_absolute_url", {}),
        ("http://api.amazonvision.com" + SECRET_PATH, None, "non_https", {"absolute": True}),
        ("ftp://api.amazonvision.com" + SECRET_PATH, None, "non_https", {}),
        ("https://u@api.amazonvision.com" + SECRET_PATH, None, "userinfo_present", {}),
        (
            "https://evil.example" + SECRET_PATH,
            None,
            "origin_mismatch",
            {"origin_match": False, "absolute": True},
        ),
        ("https://api.amazonvision.com:8443" + SECRET_PATH, None, "origin_mismatch", {}),
        ("https://api.amazonvision.com.evil.example" + SECRET_PATH, None, "origin_mismatch", {}),
        (
            "//evil.example" + SECRET_PATH,
            None,
            "path_shape_mismatch",
            {"absolute": False, "path_segment_count": 10, "session_tail_length": 23},
        ),
        (
            SECRET_PATH + "/../../x",
            None,
            "path_shape_mismatch",
            {"contains_slash": True, "session_tail_length": len(SECRET_SESSION) + 8},
        ),
        (
            f"/v1/devices/{SECRET_DEVICE}/media/streaming/whep/sessions/",
            None,
            "path_shape_mismatch",
            {"trailing_slash": True, "session_tail_length": 0, "path_segment_count": 8},
        ),
        (SECRET_PATH + ":1", None, "path_shape_mismatch", {"contains_colon": True}),
        (SECRET_PATH + "+1", None, "path_shape_mismatch", {"contains_plus": True}),
        (SECRET_PATH + "=1", None, "path_shape_mismatch", {"contains_equals": True}),
        (
            SECRET_PATH + "!",
            None,
            "path_shape_mismatch",
            {"contains_other_outside_current_allowlist": True, "contains_colon": False},
        ),
        (
            "https://api.amazonvision.com/v1/somewhere/else?x=1",
            None,
            "path_shape_mismatch",
            {"absolute": True, "session_tail_length": None, "query_present": True},
        ),
        (SECRET_PATH + "?x=<y>", None, "query_shape_mismatch", {"query_present": True}),
        (
            "/v1/devices/other-device/media/streaming/whep/sessions/" + SECRET_SESSION,
            SECRET_DEVICE,
            "device_scope_mismatch",
            {"absolute": False},
        ),
    ],
)
def test_every_rejection_is_unchanged_and_carries_only_a_safe_reason(
    settings: Settings, location: object, device: str | None, reason: str, expected: dict[str, Any]
) -> None:
    client, seen = client_with(settings, lambda _: httpx.Response(204))
    with pytest.raises(RingClientError) as caught:
        client.validated_whep_location(location, device_id=device)
    error = caught.value
    # Behaviour is exactly what it was: the same error type contract, category and message.
    assert error.category == "invalid_location" and error.operation == "whep_location"
    assert str(error) == "Ring operation failed: whep_location/invalid_location"
    assert not isinstance(error, RingAmbiguousResult)
    assert isinstance(error, WhepLocationRejected)
    assert error.reason == WhepLocationRejection(reason)
    assert set(error.diagnostics) <= WHEP_LOCATION_DIAGNOSTIC_FIELDS
    for value in error.diagnostics.values():
        assert value is None or isinstance(value, bool | int)
        assert not isinstance(value, int) or isinstance(value, bool) or 0 <= value <= 2048
    for key, value in expected.items():
        assert error.diagnostics[key] == value, key
    rendered = f"{error!s} {error!r} {error.diagnostics!r} {error.reason!r}"
    for secret in (SECRET_DEVICE, SECRET_SESSION, "evil", "amazonvision", "8443", "SECRETFRAG"):
        assert secret not in rendered
    assert seen == [], "validation never makes a request"


def test_path_diagnostics_are_only_emitted_for_path_shape_mismatches(settings: Settings) -> None:
    client, _ = client_with(settings, lambda _: httpx.Response(204))
    with pytest.raises(WhepLocationRejected) as caught:
        client.validated_whep_location("https://evil.example" + SECRET_PATH)
    assert caught.value.diagnostics == {"absolute": True, "origin_match": False}


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://evil.example" + SECRET_PATH, "origin_mismatch"),
        ("https://api.amazonvision.com:8443" + SECRET_PATH, "origin_mismatch"),
        ("http://api.amazonvision.com" + SECRET_PATH, "non_https"),
        ("https://u:p@api.amazonvision.com" + SECRET_PATH, "userinfo_present"),
        (SECRET_PATH + "#SECRETFRAG", "fragment_present"),
        (SECRET_PATH + "/../../x", "path_shape_mismatch"),
        (SECRET_PATH + ":443", "path_shape_mismatch"),
        (SECRET_PATH + "?x=<y>", "query_shape_mismatch"),
        (
            "/v1/devices/other/media/streaming/whep/sessions/" + SECRET_SESSION,
            "device_scope_mismatch",
        ),
        ("https://api.amazonvision.com:99999" + SECRET_PATH, "malformed_absolute_url"),
    ],
)
async def test_a_refused_location_on_a_201_logs_one_safe_event_and_is_never_contacted(
    settings: Settings, location: str, reason: str
) -> None:
    token = SecretStr("synthetic-ring-access-SECRETTOKEN")
    client, seen = client_with(
        settings,
        lambda _: httpx.Response(
            201,
            content=ANSWER,
            headers={
                "content-type": "application/sdp",
                "location": location,
                "x-amzn-requestid": "req-diag-1",
            },
        ),
    )
    with capture_logs() as logs, pytest.raises(RingClientError) as caught:
        await client.create_whep_session(token, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536)
    # Behaviour unchanged: the same error, and only the POST was ever sent.
    assert caught.value.category == "invalid_location"
    assert [request.method for request in seen] == ["POST"]
    diagnostics = [entry for entry in logs if entry["event"] == "ring_whep_location_rejected"]
    assert len(diagnostics) == 1, "exactly one diagnostic event"
    [event] = diagnostics
    assert set(event) <= SAFE_EVENT_KEYS
    assert event["operation"] == "whep_create" and event["status_code"] == 201
    assert event["reason"] == reason and event["provider_request_id"] == "req-diag-1"
    generic = [entry for entry in logs if entry["event"] == "ring_provider_request_failed"]
    assert len(generic) == 1 and generic[0]["category"] == "invalid_location"
    rendered = repr(logs) + str(caught.value) + repr(caught.value)
    for secret in (
        location,
        SECRET_DEVICE,
        SECRET_SESSION,
        token.get_secret_value(),
        "SECRETTOKEN",
        "Authorization",
        "Bearer",
        "v=0",
        "a=rtpmap",
        "m=video",
        "evil",
        "amazonvision",
        "SECRETFRAG",
    ):
        assert secret not in rendered, secret


async def test_a_valid_location_emits_no_rejection_event(settings: Settings) -> None:
    client, seen = client_with(
        settings,
        lambda _: httpx.Response(
            201,
            content=ANSWER,
            headers={"content-type": "application/sdp", "location": SECRET_PATH},
        ),
    )
    with capture_logs() as logs:
        session = await client.create_whep_session(
            TOKEN, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536
        )
    assert session.session_url == f"https://api.amazonvision.com:443{SECRET_PATH}"
    assert [entry for entry in logs if entry["event"] == "ring_whep_location_rejected"] == []
    assert [request.method for request in seen] == ["POST"]


async def test_non_2xx_responses_and_delete_never_emit_the_location_diagnostic(
    settings: Settings,
) -> None:
    client, _ = client_with(
        settings, lambda _: httpx.Response(503, headers={"location": "https://evil.example/x"})
    )
    with capture_logs() as logs, pytest.raises(RingClientError):
        await client.create_whep_session(TOKEN, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536)
    client, seen = client_with(settings, lambda _: httpx.Response(204))
    with capture_logs() as delete_logs, pytest.raises(RingClientError):
        await client.delete_whep_session(TOKEN, "https://evil.example" + SECRET_PATH)
    for entries in (logs, delete_logs):
        assert [e for e in entries if e["event"] == "ring_whep_location_rejected"] == []
    assert seen == [], "an untrusted teardown resource is never contacted"


# ------------------------------------------- query_shape_mismatch diagnostics (hotfix 2)
QUERY_NAME = "SECRETQNAME"
QUERY_VALUE = "SECRETQVALUE"
NAMED_CLASSES = {
    "+": "contains_plus",
    "/": "contains_slash",
    ":": "contains_colon",
    ";": "contains_semicolon",
    ",": "contains_comma",
    "@": "contains_at",
    "?": "contains_question_mark",
    "[": "contains_brackets",
    "]": "contains_brackets",
}
QUERY_BOOLEANS = sorted(set(NAMED_CLASSES.values()) | {"contains_other_punctuation"})


def _query_rejection(settings: Settings, query: str) -> WhepLocationRejected:
    client, seen = client_with(settings, lambda _: httpx.Response(204))
    with pytest.raises(WhepLocationRejected) as caught:
        client.validated_whep_location(
            f"https://api.amazonvision.com{SECRET_PATH}?{query}", device_id=SECRET_DEVICE
        )
    assert caught.value.reason is WhepLocationRejection.QUERY_SHAPE_MISMATCH
    assert caught.value.category == "invalid_location"
    assert seen == []
    return caught.value


def test_the_diagnostic_allowlist_is_exactly_the_unchanged_validator() -> None:
    """The regex alone decides; the diagnostic mirror must describe the same language."""
    import random
    import string

    from veotrex_api.ring_client import (
        _QUERY_ALLOWED,
        _QUERY_CURRENT_LIMIT,
        _WHEP_LOCATION_QUERY,
    )

    # Same character class as ever; only the bound moved from 256 to 512.
    assert _WHEP_LOCATION_QUERY.pattern == r"^[A-Za-z0-9._~%=&-]{0,512}$"
    assert _QUERY_CURRENT_LIMIT == 512

    def mirror(value: str) -> bool:
        return len(value) <= _QUERY_CURRENT_LIMIT and all(c in _QUERY_ALLOWED for c in value)

    printable = [chr(code) for code in range(33, 127)]
    samples = [*printable, "", "a" * 255, "a" * 256, "a" * 257, "a=1&b=2", "x" * 256 + "+"]
    samples += ["a" * 360, "a" * 511, "a" * 512, "a" * 513, "x" * 511 + "+", "x" * 512 + "+"]
    rng = random.Random(20260925)  # noqa: S311 - seeded test fuzz, not cryptography
    samples += [
        "".join(rng.choice(printable) for _ in range(rng.randint(0, 600))) for _ in range(2000)
    ]
    allowed = sorted(_QUERY_ALLOWED)
    samples += [
        "".join(rng.choice(allowed) for _ in range(rng.randint(400, 600))) for _ in range(500)
    ]
    for value in samples:
        assert bool(_WHEP_LOCATION_QUERY.fullmatch(value)) is mirror(value), repr(value)
    assert set(printable) - _QUERY_ALLOWED <= set(string.punctuation)


def _allowed_query(length: int) -> str:
    """A synthetic query of exactly ``length`` characters, all in the current allowlist and
    shaped like Ring's (key=value pairs with percent-escapes). Obviously not a real query."""
    body = f"{QUERY_NAME}=" + "%2Dv-_.~" * 200 + f"&k2={QUERY_VALUE}"
    query = (body * 4)[:length]
    assert len(query) == length and query[-1] != "%"
    return query


@pytest.mark.parametrize("length", [256, 360, 512])
def test_queries_up_to_the_new_limit_are_accepted_and_preserved_verbatim(
    settings: Settings, length: int
) -> None:
    client, seen = client_with(settings, lambda _: httpx.Response(204))
    query = _allowed_query(length)
    for location in (
        f"https://api.amazonvision.com{SECRET_PATH}?{query}",
        f"{SECRET_PATH}?{query}",
    ):
        assert len(location) <= 1024, "fits the unchanged overall Location bound"
        validated = client.validated_whep_location(location, device_id=SECRET_DEVICE)
        # Opaque: never parsed, reordered or re-encoded.
        assert validated == f"https://api.amazonvision.com:443{SECRET_PATH}?{query}"
    assert seen == []


def test_a_513_character_valid_query_is_still_rejected_as_over_length(
    settings: Settings,
) -> None:
    query = _allowed_query(513)
    rejection = _query_rejection(settings, query)
    diagnostics = rejection.diagnostics
    assert diagnostics["query_over_current_limit"] is True
    assert diagnostics["query_length"] == 513
    assert diagnostics["disallowed_char_count"] == 0
    assert diagnostics["absolute"] is True and diagnostics["query_present"] is True
    assert all(diagnostics[name] is False for name in QUERY_BOOLEANS)


def test_short_disallowed_queries_report_not_over_limit(settings: Settings) -> None:
    rejection = _query_rejection(settings, f"{QUERY_NAME}=a+b")
    assert rejection.diagnostics["query_over_current_limit"] is False
    assert rejection.diagnostics["contains_plus"] is True


async def test_a_real_create_with_a_360_character_query_succeeds_and_deletes_it_exactly(
    settings: Settings,
) -> None:
    """The production shape: 201, absolute Location, 360-character allowlisted query."""
    token = SecretStr("synthetic-ring-access-SECRETTOKEN")
    query = _allowed_query(360)
    location = f"https://api.amazonvision.com{SECRET_PATH}?{query}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            201,
            content=ANSWER,
            headers={"content-type": "application/sdp", "location": location},
        )

    client, seen = client_with(settings, handler)
    with capture_logs() as logs:
        session = await client.create_whep_session(
            token, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536
        )
        assert session.session_url is not None
        await client.delete_whep_session(token, session.session_url)
    assert [entry for entry in logs if entry["event"] == "ring_whep_location_rejected"] == []
    assert [entry for entry in logs if entry["event"] == "ring_provider_request_failed"] == []
    assert [request.method for request in seen] == ["POST", "DELETE"]
    delete = seen[1]
    # Same origin, same path, the query byte-for-byte - the resource Ring issued.
    assert delete.url.host == "api.amazonvision.com" and delete.url.scheme == "https"
    assert delete.url.raw_path == f"{SECRET_PATH}?{query}".encode()
    assert delete.headers["authorization"] == f"Bearer {token.get_secret_value()}"
    rendered = repr(logs) + repr(session)
    for secret in (query, QUERY_NAME, QUERY_VALUE, SECRET_DEVICE, SECRET_SESSION, "SECRETTOKEN"):
        assert secret not in rendered, secret


@pytest.mark.parametrize("character", sorted(NAMED_CLASSES))
def test_each_named_punctuation_class_sets_only_its_own_flag(
    settings: Settings, character: str
) -> None:
    rejection = _query_rejection(settings, f"{QUERY_NAME}={QUERY_VALUE}{character}tail")
    diagnostics = rejection.diagnostics
    expected = NAMED_CLASSES[character]
    for name in QUERY_BOOLEANS:
        assert diagnostics[name] is (name == expected), name
    assert diagnostics["disallowed_char_count"] == 1
    assert diagnostics["query_over_current_limit"] is False


def test_unknown_punctuation_only_sets_the_other_flag_and_is_never_rendered(
    settings: Settings,
) -> None:
    unknown = "!$'()*<>^`{|}\\\""
    rejection = _query_rejection(settings, f"{QUERY_NAME}={unknown}{QUERY_VALUE}")
    diagnostics = rejection.diagnostics
    assert diagnostics["contains_other_punctuation"] is True
    assert all(diagnostics[name] is False for name in set(NAMED_CLASSES.values()))
    assert diagnostics["disallowed_char_count"] == len(unknown)
    assert set(diagnostics) <= WHEP_LOCATION_DIAGNOSTIC_FIELDS
    assert all(value is None or isinstance(value, bool | int) for value in diagnostics.values())
    rendered = f"{rejection!s} {rejection!r}"
    for secret in (unknown, QUERY_NAME, QUERY_VALUE, SECRET_DEVICE, SECRET_SESSION):
        assert secret not in rendered


def test_the_disallowed_count_is_capped(settings: Settings) -> None:
    rejection = _query_rejection(settings, ";" * 600)
    assert rejection.diagnostics["disallowed_char_count"] == 256
    assert rejection.diagnostics["query_length"] == 600
    assert rejection.diagnostics["query_over_current_limit"] is True


async def test_a_real_create_logs_one_safe_query_event_without_any_query_text(
    settings: Settings,
) -> None:
    token = SecretStr("synthetic-ring-access-SECRETTOKEN")
    location = (
        f"https://api.amazonvision.com{SECRET_PATH}"
        f"?{QUERY_NAME}={QUERY_VALUE};other=SECRETQVALUE2,x:y"
    )
    client, seen = client_with(
        settings,
        lambda _: httpx.Response(
            201,
            content=ANSWER,
            headers={"content-type": "application/sdp", "location": location},
        ),
    )
    with capture_logs() as logs, pytest.raises(RingClientError) as caught:
        await client.create_whep_session(token, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536)
    assert caught.value.category == "invalid_location"
    # Unchanged since before either hotfix: create reports its own operation.
    assert str(caught.value) == "Ring operation failed: whep_create/invalid_location"
    assert [request.method for request in seen] == ["POST"], "never contacted"
    [event] = [entry for entry in logs if entry["event"] == "ring_whep_location_rejected"]
    assert set(event) <= SAFE_EVENT_KEYS
    assert event["reason"] == "query_shape_mismatch"
    assert event["contains_semicolon"] is True and event["contains_comma"] is True
    assert event["contains_colon"] is True and event["contains_other_punctuation"] is False
    assert event["disallowed_char_count"] == 3
    rendered = repr(logs) + str(caught.value) + repr(caught.value)
    for secret in (
        location,
        QUERY_NAME,
        QUERY_VALUE,
        "SECRETQVALUE2",
        "other=",
        SECRET_DEVICE,
        SECRET_SESSION,
        "SECRETTOKEN",
        "Authorization",
        "Bearer",
        "v=0",
        "a=rtpmap",
        "amazonvision",
    ):
        assert secret not in rendered, secret


# ------------------------------------------------- WHEP teardown diagnostics (hotfix 4)
FAILURE_EVENT_KEYS = {
    "event",
    "log_level",
    "operation",
    "category",
    "status_code",
    "provider_request_id",
    "error_title",
    "response_content_type_class",
    "response_body_present",
    "response_body_length",
    "response_body_truncated",
}
TEARDOWN_TOKEN = SecretStr("synthetic-ring-access-SECRETTOKEN")
OPAQUE_QUERY = (
    "X-Sig-Algorithm=SYNTH-HMAC&X-Sig-Credential=SECRETCRED%2F20260925%2Fus%2Fwhep"
    "&X-Sig-Date=20260925T000000Z&X-Sig-Expires=300&X-Sig-Token=SECRETQTOKEN~a.b-c_d"
    "&X-Sig-Signature=" + "0123456789abcdef" * 12
)
TEARDOWN_URL = f"https://api.amazonvision.com:443{SECRET_PATH}?{OPAQUE_QUERY}"
BODY_SECRETS = (
    "SECRETDETAIL",
    "SECRETMSG",
    "SECRETMETA",
    "eyJhbGciOiJSUzI1NiJ9.SECRETJWT",
    "access_token=SECRETBODYTOKEN",
    "refresh_token",
    "<html>",
)


def _teardown_client(
    settings: Settings, response: httpx.Response
) -> tuple[RingClient, list[httpx.Request]]:
    return client_with(settings, lambda _: response)


async def _delete_failure(
    settings: Settings, response: httpx.Response
) -> tuple[
    RingClientError, MutableMapping[str, Any], list[httpx.Request], list[MutableMapping[str, Any]]
]:
    client, seen = _teardown_client(settings, response)
    with capture_logs() as logs, pytest.raises(RingClientError) as caught:
        await client.delete_whep_session(TEARDOWN_TOKEN, TEARDOWN_URL)
    [event] = [entry for entry in logs if entry["event"] == "ring_provider_request_failed"]
    return caught.value, event, seen, logs


def _assert_nothing_sensitive(rendered: str) -> None:
    for secret in (
        *BODY_SECRETS,
        TEARDOWN_URL,
        OPAQUE_QUERY,
        "SECRETCRED",
        "SECRETQTOKEN",
        "X-Sig",
        SECRET_DEVICE,
        SECRET_SESSION,
        TEARDOWN_TOKEN.get_secret_value(),
        "SECRETTOKEN",
        "Authorization",
        "Bearer",
        "v=0",
        "a=rtpmap",
        "set-cookie",
        "SECRETCOOKIE",
    ):
        assert secret not in rendered, secret


JSON_ERROR = (
    b'{"errors":[{"title":"Forbidden","code":"AccessDenied","detail":"SECRETDETAIL '
    b'access_token=SECRETBODYTOKEN","meta":{"k":"SECRETMETA"}}],"message":"SECRETMSG",'
    b'"refresh_token":"eyJhbGciOiJSUzI1NiJ9.SECRETJWT"}'
)


async def test_a_delete_403_is_still_forbidden_and_reports_only_safe_fields(
    settings: Settings,
) -> None:
    error, event, seen, logs = await _delete_failure(
        settings,
        httpx.Response(
            403,
            content=JSON_ERROR,
            headers={
                "content-type": "application/json",
                "x-amzn-requestid": "req-teardown-1",
                "set-cookie": "session=SECRETCOOKIE",
                "www-authenticate": 'Bearer error="SECRETDETAIL"',
            },
        ),
    )
    # Behaviour unchanged: same category, not ambiguous, one request, no retry.
    assert error.category == "forbidden" and error.operation == "whep_delete"
    assert error.status_code == 403 and not isinstance(error, RingAmbiguousResult)
    assert str(error) == "Ring operation failed: whep_delete/forbidden"
    assert [request.method for request in seen] == ["DELETE"]
    assert set(event) <= FAILURE_EVENT_KEYS
    assert event == {
        "event": "ring_provider_request_failed",
        "log_level": "warning",
        "operation": "whep_delete",
        "category": "forbidden",
        "status_code": 403,
        "provider_request_id": "req-teardown-1",
        "error_title": "Forbidden",
        "response_content_type_class": "json",
        "response_body_present": True,
        "response_body_length": len(JSON_ERROR),
        "response_body_truncated": False,
    }
    assert error.error_title == "Forbidden"
    _assert_nothing_sensitive(repr(logs) + str(error) + repr(error))


@pytest.mark.parametrize(
    ("body", "content_type", "title", "type_class"),
    [
        (
            b'{"errors":[{"code":"AccessDenied","detail":"SECRETDETAIL"}]}',
            "application/json",
            "AccessDenied",
            "json",
        ),
        (
            b'{"message":"SECRETMSG","access_token=SECRETBODYTOKEN":1}',
            "application/json",
            None,
            "json",
        ),
        (b'{"errors":[{"title":"Forbid', "application/json", None, "json"),
        (b'{"errors": [SECRETDETAIL', "application/problem+json", None, "json"),
        (
            b"<html><body>SECRETDETAIL access_token=SECRETBODYTOKEN</body></html>",
            "text/html; charset=utf-8",
            None,
            "html",
        ),
        (b"Forbidden: SECRETMSG eyJhbGciOiJSUzI1NiJ9.SECRETJWT", "text/plain", None, "text"),
        (b"\x00\xffSECRETDETAIL", "application/octet-stream", None, "other"),
        (b"SECRETMSG", None, None, "other"),
        (b"", "application/json", None, "empty"),
        (
            b'{"errors":[{"title":"\\u0000Bad\\u007f title"}]}',
            "application/json",
            "Bad title",
            "json",
        ),
    ],
)
async def test_provider_error_bodies_yield_only_the_shared_safe_title(
    settings: Settings, body: bytes, content_type: str | None, title: str | None, type_class: str
) -> None:
    headers = {"content-type": content_type} if content_type else {}
    error, event, _, logs = await _delete_failure(
        settings, httpx.Response(403, content=body, headers=headers)
    )
    assert error.category == "forbidden"
    assert event["error_title"] == title
    assert event["response_content_type_class"] == type_class
    assert event["response_body_present"] is bool(body)
    assert event["response_body_length"] == len(body)
    assert event["response_body_truncated"] is False
    assert set(event) <= FAILURE_EVENT_KEYS
    _assert_nothing_sensitive(repr(logs) + str(error) + repr(error))


def test_the_whep_title_policy_is_the_generic_one() -> None:
    from veotrex_api.ring_client import safe_error_summary, whep_response_diagnostics

    for body in (JSON_ERROR, b'{"errors":[{"code":"X"}]}', b'{"a":1}', b"[1]", b"nope"):
        generic, _ = safe_error_summary(httpx.Response(403, content=body))
        whep = whep_response_diagnostics(httpx.Headers(), body, truncated=False)["error_title"]
        assert whep == generic


async def test_an_oversized_error_body_is_capped_and_never_parsed(settings: Settings) -> None:
    body = b'{"errors":[{"title":"SECRETDETAIL"}],"pad":"' + b"x" * 80_000 + b'"}'
    error, event, _, logs = await _delete_failure(
        settings, httpx.Response(403, content=body, headers={"content-type": "application/json"})
    )
    assert error.category == "forbidden"
    assert event["response_body_truncated"] is True
    assert event["response_body_length"] <= 65_536
    assert event["error_title"] is None, "a truncated document is never parsed"
    _assert_nothing_sensitive(repr(logs))


@pytest.mark.parametrize(
    ("status", "category", "ambiguous"),
    [
        (401, "unauthorized", False),
        (429, "rate_limited", False),
        (400, "provider_rejected", False),
        (500, "provider_unavailable", True),
        (307, "redirect_refused", False),
    ],
)
async def test_delete_categories_are_unchanged(
    settings: Settings, status: int, category: str, ambiguous: bool
) -> None:
    error, event, seen, logs = await _delete_failure(
        settings,
        httpx.Response(
            status,
            content=JSON_ERROR,
            headers={"content-type": "application/json", "location": "https://evil.example/x"},
        ),
    )
    assert error.category == category and event["category"] == category
    assert isinstance(error, RingAmbiguousResult) is ambiguous
    # A redirect is refused, never followed: exactly one request, to Ring.
    assert [(r.method, r.url.host) for r in seen] == [("DELETE", "api.amazonvision.com")]
    _assert_nothing_sensitive(repr(logs))
    assert "evil" not in repr(logs)


async def test_post_failures_carry_the_same_safe_diagnostics_and_behave_as_before(
    settings: Settings,
) -> None:
    client, seen = client_with(
        settings,
        lambda _: httpx.Response(
            403, content=JSON_ERROR, headers={"content-type": "application/json"}
        ),
    )
    with capture_logs() as logs, pytest.raises(RingClientError) as caught:
        await client.create_whep_session(
            TEARDOWN_TOKEN, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536
        )
    assert caught.value.category == "forbidden"
    assert str(caught.value) == "Ring operation failed: whep_create/forbidden"
    assert [request.method for request in seen] == ["POST"]
    [event] = [entry for entry in logs if entry["event"] == "ring_provider_request_failed"]
    assert event["operation"] == "whep_create" and event["error_title"] == "Forbidden"
    assert set(event) <= FAILURE_EVENT_KEYS
    _assert_nothing_sensitive(repr(logs) + repr(caught.value))


async def test_post_2xx_failures_keep_their_original_event_shape(settings: Settings) -> None:
    client, _ = client_with(
        settings,
        lambda _: httpx.Response(201, content=b"not sdp", headers={"location": SECRET_PATH}),
    )
    with capture_logs() as logs, pytest.raises(RingClientError):
        await client.create_whep_session(
            TEARDOWN_TOKEN, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536
        )
    failures = [e for e in logs if e["event"] == "ring_provider_request_failed"]
    assert [e["category"] for e in failures] == ["malformed_answer"]
    assert "response_body_length" not in failures[0], "only non-success responses are described"


# -------------------------------------------- DELETE request preservation (tests only)
async def test_delete_targets_the_validated_location_byte_for_byte_with_the_bearer(
    settings: Settings,
) -> None:
    location = f"https://api.amazonvision.com{SECRET_PATH}?{OPAQUE_QUERY}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            201, content=ANSWER, headers={"content-type": "application/sdp", "location": location}
        )

    client, seen = client_with(settings, handler)
    session = await client.create_whep_session(
        TEARDOWN_TOKEN, SECRET_DEVICE, None, OFFER, max_answer_bytes=65536
    )
    # Validation preserves path and query exactly: no parsing, re-ordering or re-encoding.
    assert session.session_url == TEARDOWN_URL
    await client.delete_whep_session(TEARDOWN_TOKEN, session.session_url)
    post, delete = seen
    assert delete.method == "DELETE"
    assert delete.url.scheme == "https" and delete.url.host == "api.amazonvision.com"
    assert delete.url.port in (None, 443)
    # httpx request construction sends exactly the validated path and query bytes.
    assert delete.url.raw_path == f"{SECRET_PATH}?{OPAQUE_QUERY}".encode()
    assert delete.url.query == OPAQUE_QUERY.encode()
    assert b"%2F" in delete.url.raw_path, "percent-escapes are forwarded, not decoded"
    # Teardown still authenticates exactly as Ring documents it.
    assert delete.headers["authorization"] == f"Bearer {TEARDOWN_TOKEN.get_secret_value()}"
    assert delete.headers["accept"] == "application/sdp"
    assert "content-type" not in delete.headers and delete.content == b""
    assert post.headers["authorization"] == delete.headers["authorization"]


async def test_delete_never_follows_a_redirect_with_the_bearer(settings: Settings) -> None:
    client, seen = client_with(
        settings,
        lambda request: httpx.Response(
            302 if request.url.host == "api.amazonvision.com" else 204,
            headers={"location": f"https://api.amazonvision.com{SECRET_PATH}-other"},
        ),
    )
    with pytest.raises(RingClientError) as caught:
        await client.delete_whep_session(TEARDOWN_TOKEN, TEARDOWN_URL)
    assert caught.value.category == "redirect_refused"
    assert len(seen) == 1, "a same-origin redirect is not followed either"
