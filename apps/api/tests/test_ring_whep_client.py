"""V1-DEMO-03B: server-side Ring WHEP client methods. No network: httpx.MockTransport only."""

from __future__ import annotations

from collections.abc import Callable
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

    assert _WHEP_LOCATION_QUERY.pattern == r"^[A-Za-z0-9._~%=&-]{0,256}$"

    def mirror(value: str) -> bool:
        return len(value) <= _QUERY_CURRENT_LIMIT and all(c in _QUERY_ALLOWED for c in value)

    printable = [chr(code) for code in range(33, 127)]
    samples = [*printable, "", "a" * 255, "a" * 256, "a" * 257, "a=1&b=2", "x" * 256 + "+"]
    rng = random.Random(20260925)  # noqa: S311 - seeded test fuzz, not cryptography
    samples += [
        "".join(rng.choice(printable) for _ in range(rng.randint(0, 300))) for _ in range(2000)
    ]
    for value in samples:
        assert bool(_WHEP_LOCATION_QUERY.fullmatch(value)) is mirror(value), repr(value)
    assert set(printable) - _QUERY_ALLOWED <= set(string.punctuation)


def test_an_over_limit_but_otherwise_valid_query_is_reported_as_length(
    settings: Settings,
) -> None:
    query = f"{QUERY_NAME}=" + "v" * 300
    rejection = _query_rejection(settings, query)
    diagnostics = rejection.diagnostics
    assert diagnostics["query_over_current_limit"] is True
    assert diagnostics["query_length"] == len(query)
    assert diagnostics["disallowed_char_count"] == 0
    assert diagnostics["absolute"] is True and diagnostics["query_present"] is True
    assert all(diagnostics[name] is False for name in QUERY_BOOLEANS)
    # A query at the limit is accepted, exactly as before.
    client, _ = client_with(settings, lambda _: httpx.Response(204))
    at_limit = "a=" + "v" * 254
    assert client.validated_whep_location(f"{SECRET_PATH}?{at_limit}").endswith(at_limit)


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
