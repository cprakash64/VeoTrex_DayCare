import pickle
from uuid import UUID

import pytest
from pydantic import SecretStr

from veotrex_edge_agent.camera_transport import media_worker
from veotrex_edge_agent.camera_transport.descriptor import (
    GENERIC_RTSP_ENDPOINT_POLICY,
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    RING_ENDPOINT_POLICY,
    CredentialMode,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    SessionCredential,
    TransportProtocol,
    redact_text,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import (
    TransportError,
    TransportErrorCategory,
    safe_category,
)
from veotrex_edge_agent.qualification.gstreamer import ring_rtsps_url

CAMERA = UUID(int=0x5A)
SECRET = "synthetic-secret-value-for-tests"  # noqa: S105 - obviously synthetic


def _descriptor(**overrides: object) -> LiveSessionDescriptor:
    values: dict[str, object] = {
        "provider": ProviderKind.RTSP,
        "logical_camera_id": CAMERA,
        "generation": 1,
        "endpoint": validate_endpoint(
            "rtsp://192.0.2.20:554/stream1", GENERIC_RTSP_ENDPOINT_POLICY
        ),
        "created_monotonic": 100.0,
        "expires_monotonic": 160.0,
        "renew_after_monotonic": 155.0,
    }
    values.update(overrides)
    return LiveSessionDescriptor(**values)  # type: ignore[arg-type]


def _rejected(raw: object, policy=GENERIC_RTSP_ENDPOINT_POLICY) -> TransportError:  # type: ignore[no-untyped-def]
    with pytest.raises(TransportError) as caught:
        validate_endpoint(raw, policy)
    assert caught.value.category is TransportErrorCategory.INVALID_ENDPOINT
    if isinstance(raw, str) and raw:
        assert raw not in str(caught.value)
    return caught.value


def test_valid_session_descriptor_is_immutable_and_safely_represented() -> None:
    descriptor = _descriptor()
    assert descriptor.transport_protocol is TransportProtocol.RTSP
    assert descriptor.endpoint.uri == "rtsp://192.0.2.20:554/stream1"
    assert "192.0.2" not in repr(descriptor)
    assert "stream1" not in repr(descriptor)
    assert "stream1" not in str(descriptor.safe_dict())
    assert descriptor.safe_dict()["endpoint"] == "rtsp://private:554/REDACTED"
    with pytest.raises(AttributeError):
        descriptor.generation = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "ftp://192.0.2.10/video",
        "data:video/mp4;base64,AAAA",
        "javascript:alert(1)",
        "http://192.0.2.10/stream",
        "https://192.0.2.10/stream",
        "udp://192.0.2.10:5000",
        "srt://192.0.2.10:9000",
    ],
)
def test_unexpected_schemes_are_rejected(raw: str) -> None:
    _rejected(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "rtsp://",
        "rtsp:///no-host",
        "rtsp://192.0.2.10:99999/x",
        "rtsp://192.0.2.10:0/x",
        "rtsp://192.0.2.10:abc/x",
        "rtsp://[::1/x",
        "rtsp://192.0.2.10/x#fragment",
        "rtsp://bad_host_name/x",
        "rtsp://-leading.example/x",
        "rtsp://192.0.2.10/" + "a" * 3000,
        12345,
        None,
    ],
)
def test_malformed_endpoints_are_rejected(raw: object) -> None:
    _rejected(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "/dev/video0",
        "./recording.mp4",
        "/home/user/video.h264",
        "rtsp://192.0.2.10/../../etc/passwd",
        "rtsp://192.0.2.10/a/%2e%2e/%2e%2e/b",
        "file:/etc/shadow",
    ],
)
def test_arbitrary_filesystem_endpoints_are_rejected(raw: str) -> None:
    _rejected(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "rtsp://192.0.2.10/stream ! filesink location=/tmp/leak.h264",
        "rtsp://192.0.2.10/stream!filesink",
        "videotestsrc ! fakesink",
        "rtsp://192.0.2.10/stream\n! filesink location=x",
        "rtsp://192.0.2.10/stream\tlatency=0",
        "rtsp://192.0.2.10/strеam",  # noqa: RUF001 - deliberate Cyrillic confusable
        "rtsp://192.0.2.10/stream;rm -rf /",
        "rtsp://192.0.2.10/$(id)",
        "rtsp://192.0.2.10/`id`",
    ],
)
def test_arbitrary_pipeline_injection_is_rejected(raw: str) -> None:
    _rejected(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "rtsp://user:pass@192.0.2.10/stream",
        "rtsp://token@192.0.2.10/stream",
        "rtsp://:secret@192.0.2.10/stream",
    ],
)
def test_credentials_inside_urls_are_rejected(raw: str) -> None:
    _rejected(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "rtsp://169.254.169.254/latest/meta-data",
        "rtsp://127.0.0.1/stream",
        "rtsp://localhost/stream",
        "rtsp://0.0.0.0/stream",
        "rtsp://224.0.0.1/stream",
        "rtsp://[::1]/stream",
        "rtsp://[fe80::1]/stream",
        "rtsp://camera.localhost/stream",
    ],
)
def test_ssrf_sensitive_targets_are_rejected_for_generic_cameras(raw: str) -> None:
    _rejected(raw)


def test_ring_policy_accepts_only_the_documented_rtsps_endpoint() -> None:
    endpoint = validate_endpoint(
        ring_rtsps_url("ava1.device/opaque", "lens 1"), RING_ENDPOINT_POLICY
    )
    assert endpoint.protocol is TransportProtocol.RTSPS
    assert endpoint.port == 322
    assert endpoint.safe_repr() == "rtsps://video.rtsp.amazonvision.com:322/REDACTED"
    assert "opaque" not in repr(endpoint)
    for raw in (
        "rtsp://video.rtsp.amazonvision.com:322/v1/devices/a/stream",
        "rtsps://video.rtsp.amazonvision.com:554/v1/devices/a/stream",
        "rtsps://evil.example.com:322/v1/devices/a/stream",
        "rtsps://video.rtsp.amazonvision.com:322/v1/admin",
        "rtsps://video.rtsp.amazonvision.com:322/v1/devices/a/stream?redirect=x",
    ):
        _rejected(raw, RING_ENDPOINT_POLICY)


def test_local_fixture_policy_is_loopback_only() -> None:
    assert validate_endpoint("rtsp://127.0.0.1:8554/s", LOCAL_FIXTURE_ENDPOINT_POLICY).port == 8554
    _rejected("rtsp://192.0.2.10:8554/s", LOCAL_FIXTURE_ENDPOINT_POLICY)
    _rejected("rtsp://localhost:8554/s", LOCAL_FIXTURE_ENDPOINT_POLICY)
    _rejected("rtsps://127.0.0.1:8554/s", LOCAL_FIXTURE_ENDPOINT_POLICY)


def test_secret_redaction_in_credential_lease_and_text() -> None:
    credential = SessionCredential(CredentialMode.RTSP_USER_PASSWORD, "x", SecretStr(SECRET))
    lease = LiveSessionLease(_descriptor(), credential)
    for text in (repr(credential), str(credential), repr(lease), str(lease), f"{lease}"):
        assert SECRET not in text
    with pytest.raises(TypeError):
        pickle.dumps(credential)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    raw = (
        f"failed rtsp://user:{SECRET}@192.0.2.30:554/cam/stream?token={SECRET} "
        f"Authorization: Bearer {SECRET} password={SECRET} user-pw={SECRET}"
    )
    scrubbed = redact_text(raw)
    assert SECRET not in scrubbed
    assert "/cam/stream" not in scrubbed
    assert "rtsp://REDACTED@" in scrubbed or "rtsp://192.0.2.30:554/REDACTED" in scrubbed


def test_credential_modes_require_material_and_reject_separator_usernames() -> None:
    with pytest.raises(TransportError):
        SessionCredential(CredentialMode.RTSP_USER_PASSWORD, "x", SecretStr(""))
    with pytest.raises(TransportError):
        SessionCredential(CredentialMode.RTSP_USER_PASSWORD, "a:b", SecretStr(SECRET))


def test_descriptor_expiry_and_remaining_are_monotonic() -> None:
    descriptor = _descriptor()
    assert not descriptor.expired(154.0)
    assert descriptor.expiry_remaining(150.0) == 10.0
    assert descriptor.expired(160.0)
    assert descriptor.expiry_remaining(170.0) == 0.0
    with pytest.raises(ValueError, match="renewal"):
        _descriptor(renew_after_monotonic=161.0)
    with pytest.raises(ValueError, match="lifetime"):
        _descriptor(expires_monotonic=99.0, renew_after_monotonic=None)
    with pytest.raises(ValueError, match="renewal requires"):
        _descriptor(expires_monotonic=None)


@pytest.mark.parametrize("generation", [0, -1, True, 1.5])
def test_session_generation_must_be_a_positive_integer(generation: object) -> None:
    with pytest.raises(ValueError, match="generation"):
        _descriptor(generation=generation)


def test_capability_metadata_is_bounded() -> None:
    with pytest.raises(ValueError, match="capability"):
        _descriptor(capabilities=frozenset({"token=abc"}))


def test_unknown_categories_never_echo_untrusted_text() -> None:
    assert safe_category("AUTHORIZATION_FAILED") is TransportErrorCategory.AUTHORIZATION_FAILED
    assert safe_category(f"rtsp://x:{SECRET}@h") is TransportErrorCategory.INTERNAL_TRANSPORT_ERROR
    assert str(TransportError(TransportErrorCategory.CAMERA_OFFLINE)) == "CAMERA_OFFLINE"


def _start(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "type": "START",
        "protocol_version": 1,
        "generation": 3,
        "location": "rtsp://127.0.0.1:8554/stream",
        "user_id": "fixture",
        "password": SECRET,
        "latency_ms": 200,
        "tcp_timeout_ms": 10_000,
        "decoder": "nvidia",
    }
    message.update(overrides)
    return message


def test_worker_revalidates_start_message() -> None:
    assert media_worker.validate_start(_start())["generation"] == 3
    for bad in (
        _start(location="rtsp://u:p@127.0.0.1/x"),
        _start(location="file:///etc/passwd"),
        _start(location="rtsp://127.0.0.1/x ! filesink"),
        _start(decoder="ffmpeg"),
        _start(latency_ms=True),
        _start(generation=0),
        _start(protocol_version=2),
        {**_start(), "pipeline": "videotestsrc ! fakesink"},
    ):
        with pytest.raises(ValueError):
            media_worker.validate_start(bad)


def test_worker_same_origin_rule_blocks_redirect_and_cross_origin_control() -> None:
    origin = media_worker.origin_of("rtsp://127.0.0.1:8554/stream")
    assert media_worker.origin_of("rtsp://127.0.0.1:8554/stream/trackID=0") == origin
    assert media_worker.origin_of("rtsp://127.0.0.1:9999/stream") != origin
    assert media_worker.origin_of("rtsps://127.0.0.1:8554/stream") != origin
    assert media_worker.origin_of("rtsp://evil@127.0.0.1:8554/stream") is None
