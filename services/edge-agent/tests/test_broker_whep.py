"""V1-DEMO-03B: the edge's existing WHEP stack, pointed at the VeoTrex broker.

No Ring, no network beyond the loopback ``WhepFixtureServer``, no real credential. What these
tests pin down is the boundary: the credential comes from a protected file and goes only to the
configured control plane, the request names a VeoTrex camera UUID, the Location accepted is an
opaque VeoTrex lease, teardown goes back to the control plane, and no Ring bearer or Ring
session URL is anywhere on this path.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.broker_whep import (
    BrokeredWhepExchange,
    BrokeredWhepSessionProvider,
    BrokerWhepClient,
    ControlPlaneEndpoint,
    EdgeCredentialError,
    broker_from_settings,
    read_edge_credential,
)
from veotrex_edge_agent.camera_transport.descriptor import CredentialMode, ProviderKind
from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory as C
from veotrex_edge_agent.camera_transport.webrtc_backend import AnswerExchange, WebRtcMediaBackend
from veotrex_edge_agent.camera_transport.webrtc_media import WebRtcRuntimeReport
from veotrex_edge_agent.camera_transport.whep_client import RING_WHEP_HOST
from veotrex_edge_agent.camera_transport.whep_provider import RingWhepSessionProvider
from veotrex_edge_agent.config import EdgeSettings
from veotrex_edge_agent.qualification.models import CameraTarget
from veotrex_edge_agent.qualification.whep_fixture_server import (
    SYNTHETIC_ANSWER,
    WhepFixtureServer,
    WhepScript,
)

CAMERA = UUID("5e1f0c2a-7d3b-4a6e-9c1f-2b8d4e6a0c11")
TOKEN = "vte1.3f2b1a09-8c7d-4e6f-a5b4-c3d2e1f0a9b8." + "S" * 42 + "w"  # obviously synthetic
LEASE_ID = "L" * 43
LEASE_PATH = f"/v1/edge/whep-leases/{LEASE_ID}"
OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
AVAILABLE = WebRtcRuntimeReport(True, (), None, "present")
UNAVAILABLE = WebRtcRuntimeReport(False, ("nicesrc",), "gstreamer1.0-nice", "absent")


@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "edge.credential"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(TOKEN + "\n")
    return path


@pytest.fixture
def server():  # type: ignore[no-untyped-def]
    with WhepFixtureServer() as fixture:
        fixture.state.default = WhepScript(location=LEASE_PATH, require_bearer=False)
        yield fixture


def endpoint(host: str = "127.0.0.1", port: int = 443) -> ControlPlaneEndpoint:
    return ControlPlaneEndpoint(host, port, local=True)


def client_for(server: WhepFixtureServer, **kwargs: Any) -> BrokerWhepClient:
    return BrokerWhepClient(endpoint(**kwargs), connection_factory=server.connection_factory())


# --------------------------------------------------------------------------- credential file
def test_the_credential_is_read_from_a_protected_file(credential_file: Path) -> None:
    value = read_edge_credential(credential_file)
    assert isinstance(value, SecretStr) and value.get_secret_value() == TOKEN
    assert TOKEN not in repr(value) and TOKEN not in str(value)


def _refusal(path: Any) -> str:
    with pytest.raises(EdgeCredentialError) as caught:
        read_edge_credential(path)
    assert TOKEN not in str(caught.value) and "S" * 20 not in str(caught.value)
    return caught.value.reason


def test_unsafe_credential_files_are_refused_without_echoing_content(
    tmp_path: Path, credential_file: Path
) -> None:
    assert _refusal("") == "credential_file_not_configured"
    assert _refusal("relative/edge.credential") == "credential_file_path_not_absolute"
    assert _refusal(tmp_path / "missing") == "credential_file_missing"
    link = tmp_path / "link"
    link.symlink_to(credential_file)
    assert _refusal(link) == "credential_file_is_symlink"
    assert _refusal(tmp_path) == "credential_file_not_regular"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    assert _refusal(fifo) == "credential_file_not_regular"

    def write(name: str, content: bytes, mode: int = 0o600) -> Path:
        path = tmp_path / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    assert _refusal(write("open", (TOKEN + "\n").encode(), 0o640)) == (
        "credential_file_permissions_too_open"
    )
    assert _refusal(write("world", (TOKEN + "\n").encode(), 0o604)) == (
        "credential_file_permissions_too_open"
    )
    assert _refusal(write("empty", b"")) == "credential_file_empty"
    assert _refusal(write("newline", b"\n")) == "credential_file_empty"
    assert _refusal(write("big", b"v" * 4096)) == "credential_file_too_large"
    assert _refusal(write("two", (TOKEN + "\n" + TOKEN).encode())) == "credential_malformed"
    assert _refusal(write("spaced", (" " + TOKEN).encode())) == "credential_malformed"
    assert _refusal(write("binary", b"\xff" * 10)) == "credential_malformed"
    assert _refusal(write("ring", b"ring-oauth-access-token-shaped-value")) == (
        "credential_malformed"
    )
    assert read_edge_credential(write("crlf", (TOKEN + "\r\n").encode())).get_secret_value() == (
        TOKEN
    )


# ------------------------------------------------------------------------ control plane URL
@pytest.mark.parametrize(
    "url",
    [
        "http://control.example.com",
        "https://user:pw@control.example.com",
        "https://control.example.com/api",
        "https://control.example.com?x=1",
        "https://control.example.com#x",
        "https://control example.com",
        "ftp://control.example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://[::1]",
        "https://control.example.com:99999",
        "",
    ],
)
def test_production_control_plane_urls_must_be_public_https_origins(url: str) -> None:
    with pytest.raises(ValueError):
        ControlPlaneEndpoint.parse(url, environment="production")


def test_valid_control_plane_origins() -> None:
    parsed = ControlPlaneEndpoint.parse("https://Control.Example.com/", environment="production")
    assert (parsed.host, parsed.port, parsed.local) == ("control.example.com", 443, False)
    assert (
        parsed.offer_uri(CAMERA) == f"https://control.example.com:443/v1/edge/cameras/{CAMERA}/whep"
    )
    local = ControlPlaneEndpoint.parse("https://127.0.0.1:8443", environment="test")
    assert (local.host, local.port, local.local) == ("127.0.0.1", 8443, True)
    with pytest.raises(ValueError):
        ControlPlaneEndpoint.parse("http://127.0.0.1:8000", environment="local")


def test_edge_settings_validate_the_broker_configuration(tmp_path: Path) -> None:
    node = str(uuid4())
    configured = EdgeSettings(
        node_id=node,
        environment="production",
        control_plane_url="https://control.example.com",
        credential_file="/etc/veotrex-edge/credential",
        _env_file=None,  # type: ignore[call-arg]
    )
    resolved, path = broker_from_settings(configured)
    assert resolved.host == "control.example.com" and path == Path("/etc/veotrex-edge/credential")
    assert "/etc/veotrex-edge/credential" not in repr(configured)
    for bad in (
        {"control_plane_url": "http://control.example.com"},
        {"control_plane_url": "https://127.0.0.1"},
        {"credential_file": "relative/credential"},
    ):
        with pytest.raises(ValueError):
            EdgeSettings(node_id=node, environment="production", _env_file=None, **bad)  # type: ignore[call-arg]
    unconfigured = EdgeSettings(node_id=node, _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(TransportError) as caught:
        broker_from_settings(unconfigured)
    assert caught.value.category is C.PROVIDER_NOT_CONFIGURED


# ------------------------------------------------------------------------------ WHEP client
def test_the_existing_client_posts_to_the_control_plane_with_the_edge_credential(
    server: WhepFixtureServer,
) -> None:
    client = client_for(server)
    session = client.create_session(str(CAMERA), SecretStr(TOKEN), OFFER)
    assert session.answer_sdp == SYNTHETIC_ANSWER
    assert session.session_url == f"https://127.0.0.1:443{LEASE_PATH}"
    [post] = server.observations()
    assert post.method == "POST"
    assert post.path == f"/v1/edge/cameras/{CAMERA}/whep" and post.query == ""
    assert post.headers["authorization"] == f"Bearer {TOKEN}"
    assert post.headers["content-type"] == "application/sdp"
    assert post.headers["accept"] == "application/sdp"
    assert post.body == OFFER.encode()
    assert "amazonvision" not in str(post.headers)
    assert TOKEN not in repr(session) and TOKEN not in repr(client.__dict__.get("endpoint"))


def test_the_camera_is_named_only_by_its_veotrex_uuid(server: WhepFixtureServer) -> None:
    client = client_for(server)
    for bad in ("synthetic-device-0001", str(CAMERA).upper(), CAMERA.hex, "../x", ""):
        with pytest.raises(TransportError) as caught:
            client.create_session(bad, SecretStr(TOKEN), OFFER)
        assert caught.value.category is C.INVALID_ENDPOINT
    with pytest.raises(TransportError):
        client.session_path(str(CAMERA), "1")
    assert server.observations() == []


@pytest.mark.parametrize(
    "location",
    [
        f"https://127.0.0.1:443{LEASE_PATH}",
        LEASE_PATH,
    ],
)
def test_an_opaque_veotrex_lease_is_accepted(server: WhepFixtureServer, location: str) -> None:
    server.state.default = WhepScript(location=location, require_bearer=False)
    session = client_for(server).create_session(str(CAMERA), SecretStr(TOKEN), OFFER)
    assert session.session_url == f"https://127.0.0.1:443{LEASE_PATH}"


@pytest.mark.parametrize(
    "location",
    [
        "/v1/devices/d/media/streaming/whep/sessions/s",
        f"https://{RING_WHEP_HOST}:443/v1/devices/d/media/streaming/whep/sessions/s",
        f"https://evil.example{LEASE_PATH}",
        f"https://127.0.0.1:8443{LEASE_PATH}",
        f"http://127.0.0.1:443{LEASE_PATH}",
        f"{LEASE_PATH}?x=1",
        "/v1/edge/whep-leases/short",
        f"{LEASE_PATH}/extra",
    ],
)
def test_a_ring_or_foreign_location_is_refused(server: WhepFixtureServer, location: str) -> None:
    server.state.default = WhepScript(location=location, require_bearer=False)
    with pytest.raises(TransportError) as caught:
        client_for(server).create_session(str(CAMERA), SecretStr(TOKEN), OFFER)
    assert caught.value.category is C.WHEP_INVALID_LOCATION


def test_teardown_goes_back_to_the_control_plane(server: WhepFixtureServer) -> None:
    client = client_for(server)
    session = client.create_session(str(CAMERA), SecretStr(TOKEN), OFFER)
    assert session.session_url is not None
    server.script(WhepScript(status=204, body=b"", content_type=None, require_bearer=False))
    client.delete_session(session.session_url, SecretStr(TOKEN))
    delete = server.observations()[-1]
    assert delete.method == "DELETE" and delete.path == LEASE_PATH
    assert delete.headers["authorization"] == f"Bearer {TOKEN}"
    server.script(WhepScript(status=404, body=b"", content_type=None, require_bearer=False))
    client.delete_session(session.session_url, SecretStr(TOKEN))  # already gone is fine


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, C.WHEP_HTTP_UNAUTHORIZED),
        (403, C.WHEP_HTTP_FORBIDDEN),
        (404, C.CAMERA_OFFLINE),
        (429, C.WHEP_HTTP_RATE_LIMITED),
        (502, C.WHEP_HTTP_SERVER_ERROR),
        (503, C.CAMERA_OFFLINE),
        (307, C.REDIRECT_REFUSED),
    ],
)
def test_broker_statuses_use_the_existing_taxonomy(
    server: WhepFixtureServer, status: int, category: C
) -> None:
    server.state.default = WhepScript(status=status, body=b"", require_bearer=False)
    with pytest.raises(TransportError) as caught:
        client_for(server).create_session(str(CAMERA), SecretStr(TOKEN), OFFER)
    assert caught.value.category is category
    assert TOKEN not in str(caught.value)


# ------------------------------------------------------------------ provider + exchange
async def test_the_provider_leases_the_broker_endpoint_with_the_edge_credential(
    credential_file: Path,
) -> None:
    provider = BrokeredWhepSessionProvider(
        CAMERA, endpoint(), credential_file, runtime_probe=lambda: AVAILABLE
    )
    lease = await provider.acquire(CAMERA, 1, 0.0)
    assert provider.kind is ProviderKind.RING
    assert lease.credential.mode is CredentialMode.BEARER
    assert lease.credential.secret.get_secret_value() == TOKEN
    assert lease.descriptor.endpoint.host == "127.0.0.1"
    assert lease.descriptor.endpoint.path == f"/v1/edge/cameras/{CAMERA}/whep"
    assert "BROKERED" in lease.descriptor.capabilities
    for rendered in (repr(lease), repr(provider), str(lease.descriptor.safe_dict())):
        assert TOKEN not in rendered and "amazonvision" not in rendered


async def test_the_provider_fails_closed_in_the_safe_order(credential_file: Path) -> None:
    reads: list[object] = []

    def reader(path: Any) -> SecretStr:
        reads.append(path)
        return read_edge_credential(path)

    unavailable = BrokeredWhepSessionProvider(
        CAMERA,
        endpoint(),
        credential_file,
        runtime_probe=lambda: UNAVAILABLE,
        credential_reader=reader,
    )
    with pytest.raises(TransportError) as caught:
        await unavailable.acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.WEBRTC_RUNTIME_UNAVAILABLE
    assert reads == [], "no credential is touched on a host that cannot play WHEP media"

    missing = BrokeredWhepSessionProvider(
        CAMERA, endpoint(), credential_file.parent / "absent", runtime_probe=lambda: AVAILABLE
    )
    with pytest.raises(TransportError) as caught:
        await missing.acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.PROVIDER_NOT_CONFIGURED

    ok = BrokeredWhepSessionProvider(
        CAMERA, endpoint(), credential_file, runtime_probe=lambda: AVAILABLE
    )
    with pytest.raises(TransportError) as caught:
        await ok.acquire(uuid4(), 1, 0.0)
    assert caught.value.category is C.INTERNAL_TRANSPORT_ERROR


async def test_the_exchange_negotiates_through_the_broker_and_releases_the_lease(
    server: WhepFixtureServer, credential_file: Path
) -> None:
    provider = BrokeredWhepSessionProvider(
        CAMERA, endpoint(), credential_file, runtime_probe=lambda: AVAILABLE
    )
    exchange = BrokeredWhepExchange(client_for(server))
    as_answer_exchange: AnswerExchange = exchange  # the WebRTC backend's own contract
    backend = WebRtcMediaBackend(3, as_answer_exchange)
    assert backend.generation == 3
    lease = await provider.acquire(CAMERA, 3, 0.0)
    answer = exchange(OFFER, lease)
    assert answer == SYNTHETIC_ANSWER and exchange.held == 1
    server.script(WhepScript(status=204, body=b"", content_type=None, require_bearer=False))
    assert exchange.release(3) is True
    assert exchange.release(3) is False, "idempotent"
    methods = [(o.method, o.path) for o in server.observations()]
    assert methods == [("POST", f"/v1/edge/cameras/{CAMERA}/whep"), ("DELETE", LEASE_PATH)]
    for observation in server.observations():
        assert observation.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in repr(exchange)


async def test_the_exchange_never_sends_the_edge_credential_anywhere_else(
    server: WhepFixtureServer, credential_file: Path
) -> None:
    exchange = BrokeredWhepExchange(client_for(server))
    other_origin = BrokeredWhepSessionProvider(
        CAMERA,
        ControlPlaneEndpoint("10.1.2.3", 443, local=True),
        credential_file,
        runtime_probe=lambda: AVAILABLE,
    )
    with pytest.raises(TransportError) as caught:
        exchange(OFFER, await other_origin.acquire(CAMERA, 1, 0.0))
    assert caught.value.category is C.INVALID_ENDPOINT

    class Tokens:
        async def access_token_for(self, request: Any) -> SecretStr:
            return SecretStr("synthetic-ring-bearer-not-a-real-token")

    ring_lease = await RingWhepSessionProvider(
        CameraTarget(CAMERA, "camera", "synthetic-device-0001"),
        Tokens(),  # type: ignore[arg-type]
        runtime_probe=lambda: AVAILABLE,
    ).acquire(CAMERA, 1, 0.0)
    with pytest.raises(TransportError):
        exchange(OFFER, ring_lease)
    assert server.observations() == []


async def test_a_missing_broker_lease_is_a_contract_violation(
    server: WhepFixtureServer, credential_file: Path
) -> None:
    server.state.default = WhepScript(location=None, require_bearer=False)
    provider = BrokeredWhepSessionProvider(
        CAMERA, endpoint(), credential_file, runtime_probe=lambda: AVAILABLE
    )
    with pytest.raises(TransportError) as caught:
        BrokeredWhepExchange(client_for(server))(OFFER, await provider.acquire(CAMERA, 1, 0.0))
    assert caught.value.category is C.WHEP_INVALID_LOCATION


def test_the_broker_path_cannot_reach_a_ring_token_provider() -> None:
    """Structural, not conventional: the module imports no token provider and calls none."""
    source = Path(__file__).resolve().parents[1] / (
        "src/veotrex_edge_agent/camera_transport/broker_whep.py"
    )
    tree = ast.parse(source.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "AccessTokenProvider" not in imported and "RING_WHEP_HOST" not in imported
    assert not any(module and "qualification" in module for module in modules)
    assert "access_token_for" not in called
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any(re.search(r"amazonvision", value) for value in strings[1:])
