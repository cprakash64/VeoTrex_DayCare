import http.client
import ssl
from typing import Any

import pytest
from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory as C
from veotrex_edge_agent.camera_transport.webrtc_media import (
    REQUIRED_ICE_ELEMENTS,
    WhepMediaBackend,
    probe_webrtc_runtime,
)
from veotrex_edge_agent.camera_transport.whep_client import (
    RING_WHEP_HOST,
    WhepClient,
    WhepConfig,
    _default_connection,
)
from veotrex_edge_agent.qualification.whep_fixture_server import (
    SYNTHETIC_BEARER,
    WhepFixtureServer,
    WhepScript,
)

DEVICE = "synthetic-device-0001"
BEARER = SecretStr(SYNTHETIC_BEARER)
OTHER_BEARER = SecretStr("synthetic-wrong-bearer-value")
SESSION_PATH = f"/v1/devices/{DEVICE}/media/streaming/whep/sessions/synthetic-session-7"


@pytest.fixture
def fixture_server():  # type: ignore[no-untyped-def]
    with WhepFixtureServer() as server:
        yield server


def client_for(server: WhepFixtureServer, **overrides: Any) -> WhepClient:
    config = WhepConfig(host=RING_WHEP_HOST, port=443, **overrides)
    return WhepClient(config, connection_factory=server.connection_factory())


def location_for(server: WhepFixtureServer, path: str = SESSION_PATH) -> str:
    return f"https://{RING_WHEP_HOST}:443{path}"


# --------------------------------------------------------------------- URL + header contract
def test_session_path_matches_current_official_endpoint_and_encodes_ids() -> None:
    client = WhepClient()
    assert client.session_path(DEVICE) == f"/v1/devices/{DEVICE}/media/streaming/whep/sessions"
    assert client.session_path(DEVICE, "2").endswith("/sessions?component_id=2")
    for bad in ("", "a/b", "../etc", "a b", "a?x=1", "x" * 300, 5, None):
        with pytest.raises(TransportError) as caught:
            client.session_path(bad)  # type: ignore[arg-type]
        assert caught.value.category is C.INVALID_ENDPOINT
    with pytest.raises(TransportError):
        client.session_path(DEVICE, "bad component")


def test_default_connection_requires_verified_tls() -> None:
    connection = _default_connection(RING_WHEP_HOST, 443, 5.0)
    assert isinstance(connection, http.client.HTTPSConnection)
    context = connection._context  # type: ignore[attr-defined]
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    connection.close()


def test_bearer_is_sent_only_in_the_authorization_header(fixture_server: WhepFixtureServer) -> None:
    fixture_server.script(WhepScript(location=location_for(fixture_server)))
    client = client_for(fixture_server)
    session = client.create_session(DEVICE, BEARER, "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n")
    observation = fixture_server.observations()[0]
    assert observation.method == "POST"
    assert observation.headers["authorization"] == f"Bearer {SYNTHETIC_BEARER}"
    assert observation.headers["content-type"] == "application/sdp"
    assert SYNTHETIC_BEARER not in observation.path
    assert SYNTHETIC_BEARER not in observation.query
    assert "@" not in observation.path
    assert session.status == 201
    assert session.video_codecs == ("H264",)
    assert session.teardown_supported


def test_session_and_sdp_never_appear_in_repr_or_error_text(
    fixture_server: WhepFixtureServer,
) -> None:
    fixture_server.script(WhepScript(location=location_for(fixture_server)))
    session = client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    for text in (repr(session), str(session), str(session.safe_dict())):
        assert SYNTHETIC_BEARER not in text
        assert "synthetic-session-7" not in text
        assert "m=video" not in text
    assert session.safe_dict()["session_resource"] == "present"
    error = TransportError(C.WHEP_HTTP_UNAUTHORIZED)
    assert str(error) == "WHEP_HTTP_UNAUTHORIZED"


def test_empty_or_malformed_bearer_is_rejected_before_any_request(
    fixture_server: WhepFixtureServer,
) -> None:
    client = client_for(fixture_server)
    for bad in (SecretStr(""), SecretStr("token with space"), SecretStr("x" * 9000)):
        with pytest.raises(TransportError) as caught:
            client.create_session(DEVICE, bad, "v=0\r\n")
        assert caught.value.category is C.WHEP_HTTP_UNAUTHORIZED
    assert fixture_server.observations() == []


# --------------------------------------------------------------------- HTTP status mapping
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, C.WHEP_OFFER_FAILED),
        (401, C.WHEP_HTTP_UNAUTHORIZED),
        (403, C.WHEP_HTTP_FORBIDDEN),
        (404, C.CAMERA_OFFLINE),
        (429, C.WHEP_HTTP_RATE_LIMITED),
        (500, C.WHEP_HTTP_SERVER_ERROR),
        (502, C.WHEP_HTTP_SERVER_ERROR),
        (503, C.CAMERA_OFFLINE),
        (302, C.REDIRECT_REFUSED),
        (418, C.WHEP_OFFER_FAILED),
    ],
)
def test_http_status_maps_to_safe_category(
    fixture_server: WhepFixtureServer, status: int, category: C
) -> None:
    fixture_server.script(
        WhepScript(status=status, body=b"", content_type=None, require_bearer=False)
    )
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is category


def test_redirects_are_never_followed_with_a_bearer_token(
    fixture_server: WhepFixtureServer,
) -> None:
    fixture_server.script(
        WhepScript(
            status=302,
            body=b"",
            content_type=None,
            location="https://evil.example.com/v1/x",
            require_bearer=False,
        )
    )
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is C.REDIRECT_REFUSED
    assert len(fixture_server.observations()) == 1  # no second request to the redirect target


def test_unauthorized_fixture_rejects_wrong_bearer(fixture_server: WhepFixtureServer) -> None:
    fixture_server.script(WhepScript(location=location_for(fixture_server)))
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).create_session(DEVICE, OTHER_BEARER, "v=0\r\n")
    assert caught.value.category is C.WHEP_HTTP_UNAUTHORIZED


# --------------------------------------------------------------------- SDP answer contract
def test_offer_must_be_sdp_and_bounded(fixture_server: WhepFixtureServer) -> None:
    client = client_for(fixture_server, max_sdp_bytes=2_048)
    for bad in ("", "not-sdp", "m=video 9\r\n"):
        with pytest.raises(TransportError) as caught:
            client.create_session(DEVICE, BEARER, bad)
        assert caught.value.category is C.WHEP_OFFER_FAILED
    with pytest.raises(TransportError) as caught:
        client.create_session(DEVICE, BEARER, "v=0\r\n" + "a=x\r\n" * 1_000)
    assert caught.value.category is C.WHEP_OFFER_FAILED
    assert fixture_server.observations() == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not an sdp body",
        b"o=- 0 0 IN IP4 127.0.0.1\r\nm=video 9 x 96\r\n",  # missing v=0
        b"v=0\r\ns=-\r\n",  # no media
        b"v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",  # audio only
        b"v=0\r\nm=video 9 x 96\r\nm=audio 9 x 111\r\n",  # active audio breaks video-only
        b"v=0\r\n\xff\xfe binary\r\n",
    ],
)
def test_malformed_or_unexpected_answers_are_rejected(
    fixture_server: WhepFixtureServer, body: bytes
) -> None:
    fixture_server.script(WhepScript(body=body, location=location_for(fixture_server)))
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is C.WHEP_INVALID_ANSWER


def test_rejected_audio_line_with_zero_port_is_accepted(fixture_server: WhepFixtureServer) -> None:
    body = b"v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H265/90000\r\nm=audio 0 x 111\r\n"
    fixture_server.script(WhepScript(body=body, location=location_for(fixture_server)))
    session = client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert session.video_codecs == ("H265",)


def test_oversized_answer_is_rejected(fixture_server: WhepFixtureServer) -> None:
    oversized = b"v=0\r\nm=video 9 x 96\r\n" + b"a=pad:x\r\n" * 4_000
    fixture_server.script(WhepScript(body=oversized, location=location_for(fixture_server)))
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server, max_sdp_bytes=4_096).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is C.WHEP_INVALID_ANSWER


def test_wrong_content_type_is_rejected(fixture_server: WhepFixtureServer) -> None:
    fixture_server.script(
        WhepScript(content_type="text/html", location=location_for(fixture_server))
    )
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is C.WHEP_INVALID_ANSWER


# --------------------------------------------------------------------- Location contract
def test_absolute_and_relative_session_resources_are_accepted(
    fixture_server: WhepFixtureServer,
) -> None:
    client = client_for(fixture_server)
    assert client.validate_location(SESSION_PATH) == location_for(fixture_server)
    assert client.validate_location(location_for(fixture_server)) == location_for(fixture_server)


@pytest.mark.parametrize(
    "location",
    [
        "http://api.amazonvision.com/v1/devices/d/media/streaming/whep/sessions/s",  # downgrade
        "https://evil.example.com/v1/devices/d/media/streaming/whep/sessions/s",  # cross-origin
        "https://user:pass@api.amazonvision.com/v1/devices/d/media/streaming/whep/sessions/s",
        "https://api.amazonvision.com:8443/v1/devices/d/media/streaming/whep/sessions/s",
        "/v1/devices/d/media/streaming/whep/sessions/s#frag",
        "/etc/passwd",
        "/v1/devices/d/media/streaming/whep/sessions",  # no session id
        "ftp://api.amazonvision.com/v1/devices/d/media/streaming/whep/sessions/s",
        "",
        "x" * 2_000,
    ],
)
def test_invalid_or_foreign_session_resources_are_rejected(location: str) -> None:
    with pytest.raises(TransportError) as caught:
        WhepClient().validate_location(location)
    assert caught.value.category is C.WHEP_INVALID_LOCATION


def test_invalid_location_header_fails_session_creation(fixture_server: WhepFixtureServer) -> None:
    fixture_server.script(WhepScript(location="https://evil.example.com/v1/x"))
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is C.WHEP_INVALID_LOCATION


def test_missing_location_yields_session_without_teardown(
    fixture_server: WhepFixtureServer,
) -> None:
    # Ring does not currently document the Location semantics, so absence is reported, not invented.
    fixture_server.script(WhepScript(location=None))
    session = client_for(fixture_server).create_session(DEVICE, BEARER, "v=0\r\n")
    assert session.session_url is None
    assert session.teardown_supported is False
    assert session.safe_dict()["session_resource"] == "absent"


# --------------------------------------------------------------------- teardown
def test_teardown_deletes_validated_resource_with_bearer(fixture_server: WhepFixtureServer) -> None:
    fixture_server.script(
        WhepScript(location=location_for(fixture_server)),
        WhepScript(status=204, body=b"", content_type=None),
    )
    client = client_for(fixture_server)
    session = client.create_session(DEVICE, BEARER, "v=0\r\n")
    assert session.session_url is not None
    client.delete_session(session.session_url, BEARER)
    delete = fixture_server.observations()[1]
    assert delete.method == "DELETE"
    assert delete.path == SESSION_PATH
    assert delete.headers["authorization"] == f"Bearer {SYNTHETIC_BEARER}"


def test_teardown_failure_is_reported_safely(fixture_server: WhepFixtureServer) -> None:
    fixture_server.script(WhepScript(status=500, body=b"", content_type=None, require_bearer=False))
    with pytest.raises(TransportError) as caught:
        client_for(fixture_server).delete_session(location_for(fixture_server), BEARER)
    assert caught.value.category is C.WHEP_TEARDOWN_FAILED


def test_teardown_of_already_removed_session_is_not_an_error(
    fixture_server: WhepFixtureServer,
) -> None:
    fixture_server.script(WhepScript(status=404, body=b"", content_type=None, require_bearer=False))
    client_for(fixture_server).delete_session(location_for(fixture_server), BEARER)


def test_repeated_create_and_teardown_cycles_are_bounded(fixture_server: WhepFixtureServer) -> None:
    client = client_for(fixture_server)
    for _ in range(5):
        fixture_server.script(
            WhepScript(location=location_for(fixture_server)),
            WhepScript(status=204, body=b"", content_type=None),
        )
        session = client.create_session(DEVICE, BEARER, "v=0\r\n")
        assert session.session_url is not None
        client.delete_session(session.session_url, BEARER)
    assert len(fixture_server.observations()) == 10


# --------------------------------------------------------------------- transport failures
def test_unreachable_endpoint_is_provider_unavailable() -> None:
    def refused(host: str, port: int, timeout: float) -> Any:
        return http.client.HTTPConnection("127.0.0.1", 9, timeout=0.5)

    with pytest.raises(TransportError) as caught:
        WhepClient(connection_factory=refused).create_session(DEVICE, BEARER, "v=0\r\n")
    assert caught.value.category is C.PROVIDER_UNAVAILABLE


def test_timeout_is_reported_as_session_acquire_timeout() -> None:
    class TimingOut:
        def request(self, *_: Any, **__: Any) -> None:
            raise TimeoutError

        def close(self) -> None:
            return None

    with pytest.raises(TransportError) as caught:
        WhepClient(connection_factory=lambda *_: TimingOut()).create_session(
            DEVICE, BEARER, "v=0\r\n"
        )
    assert caught.value.category is C.SESSION_ACQUIRE_TIMEOUT


def test_config_bounds_are_validated() -> None:
    for bad in (
        {"connect_timeout_seconds": 0},
        {"total_timeout_seconds": 1_000},
        {"max_sdp_bytes": 10},
        {"host": "bad host"},
        {"port": 0},
    ):
        with pytest.raises(ValueError):
            WhepConfig(**bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------- webrtc runtime gate
def test_webrtc_probe_reports_missing_ice_elements_and_package_hint() -> None:
    report = probe_webrtc_runtime(element_present=lambda name: name not in REQUIRED_ICE_ELEMENTS)
    assert report.available is False
    assert set(report.missing_elements) == set(REQUIRED_ICE_ELEMENTS)
    assert report.missing_package_hint == "gstreamer1.0-nice"
    assert "libnice" in report.detail
    healthy = probe_webrtc_runtime(element_present=lambda _name: True)
    assert healthy.available is True and healthy.missing_package_hint is None


def test_whep_media_backend_fails_closed_without_webrtc_runtime() -> None:
    backend = WhepMediaBackend(
        1, probe=lambda: probe_webrtc_runtime(element_present=lambda _: False)
    )
    with pytest.raises(TransportError) as caught:
        backend.start(None, lambda _event: None)  # type: ignore[arg-type]
    assert caught.value.category is C.WEBRTC_RUNTIME_UNAVAILABLE
    assert backend.pid is None
    backend.stop()


def test_real_host_probe_matches_installed_stack() -> None:
    # Records the actual platform state; this host lacks the libnice elements webrtcbin needs.
    report = probe_webrtc_runtime()
    assert isinstance(report.available, bool)
    if not report.available:
        assert report.missing_elements
