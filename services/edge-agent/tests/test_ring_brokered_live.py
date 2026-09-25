"""V1-DEMO-03C: brokered WHEP wired into the Ring live source - everything but real media.

Three layers, none of which needs Ring, a network beyond loopback, or a real credential:

* the session provider against the loopback ``WhepFixtureServer`` acting as the VeoTrex broker;
* ``RingWhepSource`` driven through that provider, proving lifecycle, DELETE and bounded retry;
* ``GstFrameReader``'s WebRTC control protocol against a scripted stand-in worker process, which
  is how EOS, decode errors, worker death, a silent stream and teardown are made deterministic.

The real GStreamer/WebRTC chain is exercised separately in ``test_ring_webrtc_e2e.py``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from veotrex_edge_agent.camera_transport.broker_whep import (
    BrokeredWhepExchange,
    BrokeredWhepSessionProvider,
    BrokerWhepClient,
    ControlPlaneEndpoint,
)
from veotrex_edge_agent.camera_transport.reconnect import ReconnectPolicy
from veotrex_edge_agent.camera_transport.webrtc_media import WebRtcRuntimeReport
from veotrex_edge_agent.live import ring_gst
from veotrex_edge_agent.live.cli import build_brokered_ring_source
from veotrex_edge_agent.live.ring import RingWhepSource, is_terminal
from veotrex_edge_agent.live.ring_broker import BrokeredRingSessionProvider
from veotrex_edge_agent.live.ring_fakes import FakeRingFrameReader
from veotrex_edge_agent.live.ring_gst import GstFrameReader
from veotrex_edge_agent.live.ring_media import RingMediaError, RingSessionMaterial
from veotrex_edge_agent.live.source import LiveSourceError, SourceHealth
from veotrex_edge_agent.qualification.cli import parser as edge_parser
from veotrex_edge_agent.qualification.transport_qualification import child_pids
from veotrex_edge_agent.qualification.whep_fixture_server import (
    SYNTHETIC_ANSWER,
    Observation,
    WhepFixtureServer,
    WhepScript,
)

SYSTEM_PYTHON = Path("/usr/bin/python3")
CAMERA = UUID("0c7e5a31-44f2-4d0b-9a8e-6f1b2c3d4e5f")
TOKEN = "vte1.9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a." + "S" * 42 + "w"  # obviously synthetic
LEASE_PATH = "/v1/edge/whep-leases/" + "L" * 43
OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
AVAILABLE = WebRtcRuntimeReport(True, (), None, "present")
UNAVAILABLE = WebRtcRuntimeReport(False, ("nicesrc",), "gstreamer1.0-nice", "absent")
FAST = ReconnectPolicy(initial_delay_seconds=0.001, maximum_delay_seconds=0.002, max_attempts=3)
FRAME_WORKER = (
    Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent/camera_transport/frame_worker.py"
)


# ------------------------------------------------------------------------------ fixtures
@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "edge.credential"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(TOKEN + "\n")
    return path


class Broker:
    """The loopback fixture server playing the VeoTrex WHEP broker, with a switchable answer."""

    def __init__(self, server: WhepFixtureServer) -> None:
        self.server = server
        self.post: WhepScript = WhepScript(location=LEASE_PATH, require_bearer=False)
        server.state.responder = self.respond

    def respond(self, observation: Observation) -> WhepScript:
        if observation.headers.get("authorization") != f"Bearer {TOKEN}":
            return WhepScript(status=401, body=b"", content_type=None, require_bearer=False)
        if observation.method == "DELETE":
            return WhepScript(status=204, body=b"", content_type=None, require_bearer=False)
        return self.post

    def posts(self) -> list[Observation]:
        return [o for o in self.server.observations() if o.method == "POST"]

    def deletes(self) -> list[Observation]:
        return [o for o in self.server.observations() if o.method == "DELETE"]


@pytest.fixture
def broker() -> Iterator[Broker]:
    with WhepFixtureServer() as server:
        yield Broker(server)


def provider_for(
    broker: Broker,
    credential_file: Path,
    *,
    runtime: WebRtcRuntimeReport = AVAILABLE,
    reads: list[object] | None = None,
    **client_kwargs: Any,
) -> BrokeredRingSessionProvider:
    endpoint = ControlPlaneEndpoint("127.0.0.1", 443, local=True)
    client = BrokerWhepClient(
        endpoint, connection_factory=broker.server.connection_factory(), **client_kwargs
    )

    def reader(path: Any) -> Any:
        from veotrex_edge_agent.camera_transport.broker_whep import read_edge_credential

        if reads is not None:
            reads.append(path)
        return read_edge_credential(path)

    sessions = BrokeredWhepSessionProvider(
        CAMERA, endpoint, credential_file, runtime_probe=lambda: runtime, credential_reader=reader
    )
    return BrokeredRingSessionProvider(CAMERA, sessions, BrokeredWhepExchange(client))


# --------------------------------------------------------------------- session provider
def test_material_is_opaque_and_negotiation_goes_only_to_the_broker(
    broker: Broker, credential_file: Path
) -> None:
    provider = provider_for(broker, credential_file)
    material = provider.acquire()
    for rendered in (repr(material), str(material), repr(provider), str(material.as_dict())):
        assert TOKEN not in rendered and "L" * 43 not in rendered
    assert material.resource_path == f"/v1/edge/cameras/{CAMERA}/whep"
    assert material.negotiate is not None
    assert material.negotiate(OFFER) == SYNTHETIC_ANSWER
    [post] = broker.posts()
    assert post.path == f"/v1/edge/cameras/{CAMERA}/whep"
    assert post.headers["authorization"] == f"Bearer {TOKEN}"
    assert post.body == OFFER.encode()
    with pytest.raises(RingMediaError, match="invalid_session_material"):
        material.negotiate(OFFER)  # one offer per session, never re-POSTed
    assert len(broker.posts()) == 1
    provider.release(material)
    provider.release(material)
    [delete] = broker.deletes()
    assert delete.path == LEASE_PATH
    assert provider.open_sessions == 0
    with pytest.raises(RingMediaError, match="invalid_session_material"):
        material.negotiate(OFFER)  # a released session cannot be revived


def test_an_unusable_runtime_fails_before_the_credential_is_read(
    broker: Broker, credential_file: Path
) -> None:
    reads: list[object] = []
    provider = provider_for(broker, credential_file, runtime=UNAVAILABLE, reads=reads)
    with pytest.raises(RingMediaError) as caught:
        provider.acquire()
    assert caught.value.category == "WEBRTC_RUNTIME_UNAVAILABLE"
    assert is_terminal(caught.value.category)
    assert reads == [] and broker.server.observations() == []


def test_a_missing_credential_file_fails_closed(broker: Broker, tmp_path: Path) -> None:
    provider = provider_for(broker, tmp_path / "absent")
    with pytest.raises(RingMediaError) as caught:
        provider.acquire()
    assert caught.value.category == "PROVIDER_NOT_CONFIGURED" and is_terminal(
        "PROVIDER_NOT_CONFIGURED"
    )


@pytest.mark.parametrize(
    ("script", "category", "terminal"),
    [
        (WhepScript(status=401, body=b"", require_bearer=False), "WHEP_HTTP_UNAUTHORIZED", True),
        (WhepScript(status=404, body=b"", require_bearer=False), "AUTHORIZATION_FAILED", True),
        (WhepScript(status=403, body=b"", require_bearer=False), "WHEP_HTTP_FORBIDDEN", True),
        (WhepScript(status=503, body=b"", require_bearer=False), "CAMERA_OFFLINE", False),
        (WhepScript(status=429, body=b"", require_bearer=False), "WHEP_HTTP_RATE_LIMITED", False),
        (
            WhepScript(body=b"<html>not sdp</html>", location=LEASE_PATH, require_bearer=False),
            "WHEP_INVALID_ANSWER",
            False,
        ),
        (WhepScript(location=None, require_bearer=False), "WHEP_INVALID_LOCATION", True),
        (
            WhepScript(
                location="/v1/devices/d/media/streaming/whep/sessions/s", require_bearer=False
            ),
            "WHEP_INVALID_LOCATION",
            True,
        ),
        (
            WhepScript(location="/v1/edge/whep-leases/short", require_bearer=False),
            "WHEP_INVALID_LOCATION",
            True,
        ),
    ],
)
def test_broker_failures_map_to_bounded_live_categories(
    broker: Broker, credential_file: Path, script: WhepScript, category: str, terminal: bool
) -> None:
    broker.post = script
    provider = provider_for(broker, credential_file)
    material = provider.acquire()
    assert material.negotiate is not None
    with pytest.raises(RingMediaError) as caught:
        material.negotiate(OFFER)
    assert caught.value.category == category
    assert is_terminal(caught.value.category) is terminal
    assert TOKEN not in str(caught.value)
    provider.release(material)
    assert broker.deletes() == [], "no lease was created, so nothing is deleted"


def test_broker_authentication_failure_with_a_wrong_credential(
    broker: Broker, tmp_path: Path
) -> None:
    wrong = tmp_path / "wrong.credential"
    descriptor = os.open(wrong, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(TOKEN[:-1] + "A\n")
    provider = provider_for(broker, wrong)
    material = provider.acquire()
    assert material.negotiate is not None
    with pytest.raises(RingMediaError) as caught:
        material.negotiate(OFFER)
    assert caught.value.category == "WHEP_HTTP_UNAUTHORIZED"


def test_a_broker_that_never_answers_times_out_boundedly(
    broker: Broker, credential_file: Path
) -> None:
    broker.post = WhepScript(location=LEASE_PATH, delay_seconds=3.0, require_bearer=False)
    provider = provider_for(
        broker, credential_file, connect_timeout_seconds=0.5, total_timeout_seconds=1.0
    )
    material = provider.acquire()
    assert material.negotiate is not None
    started = time.monotonic()
    with pytest.raises(RingMediaError) as caught:
        material.negotiate(OFFER)
    assert time.monotonic() - started < 2.5
    assert caught.value.category == "SESSION_ACQUIRE_TIMEOUT"
    assert not is_terminal(caught.value.category)


def test_the_provider_bounds_unreleased_sessions(broker: Broker, credential_file: Path) -> None:
    provider = provider_for(broker, credential_file)
    materials = [provider.acquire() for _ in range(4)]
    with pytest.raises(RingMediaError, match="session_limit_reached"):
        provider.acquire()
    provider.close()
    assert provider.open_sessions == 0
    assert all(m.negotiate is not None for m in materials)


# ----------------------------------------------------------------- RingWhepSource wiring
class NegotiatingReader(FakeRingFrameReader):
    """A fake frame reader that negotiates exactly like the real one does in ``start``."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sessions: list[str] = []

    def start(self, material: RingSessionMaterial) -> None:
        assert material.negotiate is not None
        answer = material.negotiate(OFFER)
        assert answer.startswith("v=0")
        self.sessions.append(material.session_id)
        super().start(material)


def source_for(
    broker: Broker, credential_file: Path, reader: NegotiatingReader
) -> tuple[RingWhepSource, BrokeredRingSessionProvider]:
    provider = provider_for(broker, credential_file)
    source = RingWhepSource(
        str(CAMERA),
        provider,
        reader,
        reconnect_policy=FAST,
        first_frame_timeout_seconds=0.5,
        stall_timeout_seconds=0.5,
    )
    return source, provider


def test_ring_source_streams_through_the_broker_and_deletes_the_lease_on_close(
    broker: Broker, credential_file: Path
) -> None:
    reader = NegotiatingReader(frame_count=1000)
    source, provider = source_for(broker, credential_file, reader)
    frames = []
    for frame in source.frames():
        frames.append(frame)
        if len(frames) == 5:
            break
    source.close()
    assert [f.frame_index for f in frames] == [0, 1, 2, 3, 4]
    assert len(broker.posts()) == 1 and len(broker.deletes()) == 1
    assert provider.open_sessions == 0 and source.health is SourceHealth.STOPPED


def test_a_rejected_edge_credential_ends_the_source_after_one_attempt(
    broker: Broker, credential_file: Path
) -> None:
    broker.post = WhepScript(status=401, body=b"", require_bearer=False)
    source, provider = source_for(broker, credential_file, NegotiatingReader())
    assert list(source.frames()) == []
    assert source.health is SourceHealth.FAILED
    assert source.failure_category == "WHEP_HTTP_UNAUTHORIZED"
    assert len(broker.posts()) == 1, "a terminal refusal is never retried"
    assert provider.open_sessions == 0


def test_a_camera_the_broker_will_not_authorize_fails_fast(
    broker: Broker, credential_file: Path
) -> None:
    broker.post = WhepScript(status=404, body=b"", require_bearer=False)
    source, _ = source_for(broker, credential_file, NegotiatingReader())
    assert list(source.frames()) == []
    assert source.failure_category == "AUTHORIZATION_FAILED"
    assert len(broker.posts()) == 1


def test_transient_broker_failures_are_retried_boundedly(
    broker: Broker, credential_file: Path
) -> None:
    broker.post = WhepScript(status=503, body=b"", require_bearer=False)
    source, provider = source_for(broker, credential_file, NegotiatingReader())
    started = time.monotonic()
    assert list(source.frames()) == []
    assert time.monotonic() - started < 10
    assert source.health is SourceHealth.FAILED
    assert len(broker.posts()) <= FAST.max_attempts + 1
    assert broker.deletes() == [] and provider.open_sessions == 0


def test_every_reconnect_negotiates_a_fresh_session_and_releases_the_old_one(
    broker: Broker, credential_file: Path
) -> None:
    reader = NegotiatingReader(frame_count=1000, stall_after=3)
    source, provider = source_for(broker, credential_file, reader)
    frames = list(source.frames())
    assert frames, "each session delivered frames before stalling"
    assert source.failure_category == "MEDIA_STALLED"
    posts, deletes = broker.posts(), broker.deletes()
    assert len(posts) == len(deletes) == len(reader.sessions) <= FAST.max_attempts + 1
    assert len(set(reader.sessions)) == len(reader.sessions), "no session is ever reused"
    assert provider.open_sessions == 0
    assert source.reconnect_count <= FAST.max_attempts


# --------------------------------------------------------------------------------- CLI
def test_the_cli_builds_the_real_path_only_from_complete_safe_configuration(
    credential_file: Path, tmp_path: Path
) -> None:
    good = {
        "control_plane_url": "https://control.example.com",
        "credential_file": str(credential_file),
        "environment": "production",
    }
    source = build_brokered_ring_source(str(CAMERA), **good)
    assert isinstance(source, RingWhepSource)
    reader = source._reader
    assert isinstance(reader, GstFrameReader) and reader._source == "webrtc"
    assert reader._process is None, "building the source opens no media"
    loose = tmp_path / "loose.credential"
    loose.write_text(TOKEN + "\n")
    loose.chmod(0o644)
    for camera, overrides, expected in (
        ("front-door", {}, "ring_camera_id_must_be_a_veotrex_camera_uuid"),
        ("synthetic-device-0001", {}, "ring_camera_id_must_be_a_veotrex_camera_uuid"),
        (str(CAMERA), {"control_plane_url": None}, "control_plane_url_not_configured"),
        (
            str(CAMERA),
            {"control_plane_url": "http://control.example.com"},
            "control_plane_url_invalid",
        ),
        (
            str(CAMERA),
            {"control_plane_url": "https://api.amazonvision.com/v1/devices/x"},
            "control_plane_url_invalid",
        ),
        (str(CAMERA), {"credential_file": None}, "credential_file_not_configured"),
        (str(CAMERA), {"credential_file": TOKEN}, "credential_file_path_not_absolute"),
        (str(CAMERA), {"credential_file": str(tmp_path / "x")}, "credential_file_missing"),
        (str(CAMERA), {"credential_file": str(loose)}, "credential_file_permissions_too_open"),
    ):
        with pytest.raises(LiveSourceError) as caught:
            build_brokered_ring_source(camera, **{**good, **overrides})
        assert caught.value.category == expected
        assert TOKEN not in str(caught.value)


def test_the_cli_accepts_no_credential_token_or_provider_identity() -> None:
    common = ["live-demo", "--source", "ring", "--ring-camera", str(CAMERA)]
    for forbidden in (
        ["--credential", TOKEN],
        ["--token", TOKEN],
        ["--ring-token", "x"],
        ["--access-token", "x"],
        ["--provider-device-id", "x"],
        ["--whep-url", "https://api.amazonvision.com/x"],
    ):
        with pytest.raises(SystemExit):
            edge_parser().parse_args(common + forbidden)
    parsed = edge_parser().parse_args(
        [*common, "--control-plane-url", "https://c.example.com", "--credential-file", "/x"]
    )
    assert parsed.control_plane_url == "https://c.example.com" and parsed.credential_file == "/x"


def test_the_cli_refuses_ring_without_configuration_before_opening_media(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from veotrex_edge_agent.live.cli import run_demo_cli

    monkeypatch.delenv("VEOTREX_EDGE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("VEOTREX_EDGE_CREDENTIAL_FILE", raising=False)
    arguments = edge_parser().parse_args(
        ["live-demo", "--source", "ring", "--ring-camera", str(CAMERA), "--headless"]
    )
    assert run_demo_cli(arguments) == 2
    err = capsys.readouterr().err
    assert "control_plane_url_not_configured" in err
    assert "--credential-file" in err and TOKEN not in err


# ----------------------------------------------------- GstFrameReader WebRTC protocol
FAKE_WORKER = r"""
import array, importlib.util, json, os, socket, sys, time
SCENARIO = __SCENARIO__
EXPECTED_ANSWER = __ANSWER__
FRAME_HELPER = __HELPER__
fd = int(sys.argv[sys.argv.index("--fd") + 1])
sock = socket.socket(fileno=fd)

def send(value, fds=None):
    data = json.dumps(value).encode()
    if fds:
        sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
    else:
        sock.send(data)

def receive(timeout):
    sock.settimeout(timeout)
    try:
        data = sock.recv(65537)
    except (socket.timeout, BlockingIOError):
        return None
    if not data:
        sys.exit(0)
    message = json.loads(data)
    if message.get("type") == "STOP":
        sys.exit(0)
    return message

def wait_for(kind):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        message = receive(0.2)
        if message is not None and message.get("type") == kind:
            return message
    sys.exit(9)

def idle():
    while True:
        receive(0.2)
        send({"type": "HEARTBEAT", "generation": 1})

env_ok = set(os.environ) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
plugins = {"webrtcbin": True, "nicesrc": SCENARIO != "runtime_missing", "nicesink": True,
           "nvv4l2decoder": True}
send({"type": "HELLO", "protocol_version": 1, "worker_pid": os.getpid(), "plugins": plugins})
start = wait_for("START")
keys = {"type", "protocol_version", "generation", "codec", "decoder", "stun_server",
        "gather_timeout_seconds", "egress"}
if not env_ok or set(start) != keys or start["egress"] != "frames" or start["codec"] != "H264":
    send({"type": "FAILED", "generation": 1, "category": "INTERNAL_TRANSPORT_ERROR"})
    idle()
if SCENARIO == "no_offer":
    idle()
if SCENARIO == "ice_failed":
    send({"type": "FAILED", "generation": 1, "category": "WEBRTC_ICE_FAILED"})
    idle()
send({"type": "OFFER", "generation": 1, "sdp": "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n",
      "candidates": 1})
answer = wait_for("ANSWER")
if answer.get("sdp") != EXPECTED_ANSWER:
    send({"type": "FAILED", "generation": 1, "category": "WHEP_INVALID_ANSWER"})
    idle()
if SCENARIO == "eos":
    send({"type": "EOS", "generation": 1})
elif SCENARIO == "decode_error":
    send({"type": "FAILED", "generation": 1, "category": "DECODER_FAILED"})
elif SCENARIO == "leaky_category":
    send({"type": "FAILED", "generation": 1, "category": "/dev/video0: secret detail"})
elif SCENARIO == "stopped":
    send({"type": "STOPPED", "generation": 1})
elif SCENARIO == "exit":
    sys.exit(1)
elif SCENARIO == "frames":
    spec = importlib.util.spec_from_file_location("helper", FRAME_HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    for index in range(6):
        payload = bytes([(index * 40) % 256]) * (64 * 48 * 3)
        frame_fd = helper.create_sealed_frame_memfd(memoryview(payload))
        send({"type": "FRAME", "sequence": index + 1, "format": "BGR", "width": 64,
              "height": 48, "bytes": 64 * 48 * 3, "pts_ns": index * 66_000_000,
              "arrival_ns": time.monotonic_ns(), "discontinuity": index == 0}, [frame_fd])
        os.close(frame_fd)
        receive(0.02)
idle()
"""

pytestmark_worker = pytest.mark.skipif(
    not SYSTEM_PYTHON.exists(), reason="system Python is unavailable"
)


def fake_worker(tmp_path: Path, scenario: str, answer: str = SYNTHETIC_ANSWER) -> Path:
    script = tmp_path / f"fake_webrtc_worker_{scenario}.py"
    script.write_text(
        FAKE_WORKER.replace("__SCENARIO__", repr(scenario))
        .replace("__ANSWER__", repr(answer))
        .replace("__HELPER__", repr(str(FRAME_WORKER)))
    )
    return script


def negotiating_material(answer: str = SYNTHETIC_ANSWER) -> tuple[RingSessionMaterial, list[str]]:
    offers: list[str] = []

    def negotiate(offer: str) -> str:
        offers.append(offer)
        return answer

    return (
        RingSessionMaterial(session_id="s1", resource_path="/v1/edge/x", negotiate=negotiate),
        offers,
    )


def _no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not child_pids(os.getpid()):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def reader_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    created: list[GstFrameReader] = []

    def build(scenario: str, **kwargs: Any) -> GstFrameReader:
        monkeypatch.setattr(ring_gst, "WEBRTC_WORKER_SCRIPT", fake_worker(tmp_path, scenario))
        reader = GstFrameReader(source="webrtc", decoder="nvidia", **kwargs)
        created.append(reader)
        return reader

    yield build
    for reader in created:
        reader.close()


@pytestmark_worker
def test_reader_negotiates_through_the_material_and_reads_sealed_frames(reader_for: Any) -> None:
    fds_before = len(os.listdir("/proc/self/fd"))
    threads_before = threading.active_count()
    reader = reader_for("frames")
    material, offers = negotiating_material()
    reader.start(material)
    assert offers and offers[0].startswith("v=0")
    frames = []
    deadline = time.monotonic() + 10
    while len(frames) < 3 and time.monotonic() < deadline:
        frame = reader.read(2.0)
        if frame is not None:
            frames.append(frame)
    assert len(frames) >= 3
    for frame in frames:
        assert (frame.width, frame.height) == (64, 48)
        assert frame.image.shape == (48, 64, 3) and frame.image.nbytes == 64 * 48 * 3
    assert frames[0].discontinuity is True and frames[1].discontinuity is False
    assert frames[0].pts_ms == 0.0
    reader.close()
    assert _no_children(), "no worker survives close()"
    assert threading.active_count() == threads_before, "the reader never starts a thread"
    assert len(os.listdir("/proc/self/fd")) == fds_before
    with pytest.raises(RingMediaError, match="TRANSPORT_DISCONNECTED"):
        reader.read(0.1)  # a closed reader stays closed until start() is called again


@pytestmark_worker
@pytest.mark.parametrize(
    ("scenario", "category"),
    [
        ("eos", None),
        ("decode_error", "DECODER_FAILED"),
        ("leaky_category", "INTERNAL_TRANSPORT_ERROR"),
        ("stopped", "TRANSPORT_DISCONNECTED"),
        ("exit", "WORKER_EXITED"),
    ],
)
def test_worker_outcomes_after_negotiation_are_bounded_categories(
    reader_for: Any, scenario: str, category: str | None
) -> None:
    reader = reader_for(scenario)
    reader.start(negotiating_material()[0])
    if category is None:
        deadline = time.monotonic() + 5
        while not reader.eos and time.monotonic() < deadline:
            assert reader.read(0.5) is None
        assert reader.eos, "EOS ends the stream cleanly rather than as a stall"
    else:
        with pytest.raises(RingMediaError) as caught:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                reader.read(0.5)
        assert caught.value.category == category
        assert "secret" not in str(caught.value) and "/dev" not in str(caught.value)
    reader.close()
    assert _no_children()


@pytestmark_worker
@pytest.mark.parametrize(
    ("scenario", "category"),
    [
        ("runtime_missing", "WEBRTC_RUNTIME_UNAVAILABLE"),
        ("ice_failed", "WEBRTC_ICE_FAILED"),
        ("no_offer", "SESSION_ACQUIRE_TIMEOUT"),
    ],
)
def test_start_failures_leave_nothing_running(
    reader_for: Any, monkeypatch: pytest.MonkeyPatch, scenario: str, category: str
) -> None:
    monkeypatch.setattr(ring_gst, "OFFER_TIMEOUT_SECONDS", 1.0)
    reader = reader_for(scenario)
    material, offers = negotiating_material()
    started = time.monotonic()
    with pytest.raises(RingMediaError) as caught:
        reader.start(material)
    assert time.monotonic() - started < 8
    assert caught.value.category == category
    assert offers == [], "no offer reached the broker"
    assert reader._process is None and _no_children()


@pytestmark_worker
def test_negotiation_failures_propagate_their_category_and_reap_the_worker(
    reader_for: Any,
) -> None:
    reader = reader_for("frames")

    def refuse(_offer: str) -> str:
        raise RingMediaError("WHEP_HTTP_UNAUTHORIZED")

    with pytest.raises(RingMediaError, match="WHEP_HTTP_UNAUTHORIZED"):
        reader.start(RingSessionMaterial("s", "/p", negotiate=refuse))
    assert _no_children()

    def explode(_offer: str) -> str:
        raise RuntimeError("https://secret.example/lease?token=abc")

    reader = reader_for("frames")
    with pytest.raises(RingMediaError) as caught:
        reader.start(RingSessionMaterial("s", "/p", negotiate=explode))
    assert caught.value.category == "WHEP_OFFER_FAILED" and "secret" not in str(caught.value)
    reader = reader_for("frames")
    with pytest.raises(RingMediaError, match="WHEP_INVALID_ANSWER"):
        reader.start(negotiating_material(answer="not an sdp")[0])
    with pytest.raises(RingMediaError, match="ring_session_material_missing"):
        reader_for("frames").start(RingSessionMaterial("s", "/p"))
    assert _no_children()


@pytestmark_worker
def test_the_answer_reaches_the_worker_unchanged_and_nothing_else_does(reader_for: Any) -> None:
    # The fake worker FAILs if the START shape, the environment or the answer differ.
    reader = reader_for("silent", stun_server=None)
    reader.start(negotiating_material()[0])
    assert reader.read(0.5) is None, "first frame never arrives: a timeout, not an error"
    reader.close()


@pytestmark_worker
def test_close_while_read_is_blocked_returns_promptly(reader_for: Any) -> None:
    reader = reader_for("silent")
    reader.start(negotiating_material()[0])
    outcome: dict[str, Any] = {}

    def blocked() -> None:
        started = time.monotonic()
        try:
            reader.read(30.0)
        except RingMediaError as exc:
            outcome["category"] = exc.category
        outcome["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=blocked)
    thread.start()
    time.sleep(0.3)
    reader.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["category"] == "TRANSPORT_DISCONNECTED" and outcome["seconds"] < 5
    assert _no_children()


@pytestmark_worker
def test_repeated_start_and_stop_leaks_nothing_and_never_double_starts(reader_for: Any) -> None:
    reader = reader_for("frames")
    fds_before = len(os.listdir("/proc/self/fd"))
    for _ in range(4):
        reader.start(negotiating_material()[0])
        with pytest.raises(RingMediaError, match="reader_already_started"):
            reader.start(negotiating_material()[0])
        assert reader.read(5.0) is not None
        reader.close()
        assert _no_children()
    assert len(os.listdir("/proc/self/fd")) == fds_before


@pytestmark_worker
def test_reconnect_creates_a_fresh_worker_each_time(reader_for: Any) -> None:
    reader = reader_for("frames")
    pids = []
    for _ in range(3):
        reader.start(negotiating_material()[0])
        assert reader._process is not None
        pids.append(reader._process.pid)
        reader.close()
    assert len(set(pids)) == 3
    assert _no_children()


def test_the_real_worker_script_is_the_repository_webrtc_worker() -> None:
    assert ring_gst.WEBRTC_WORKER_SCRIPT.name == "webrtc_worker.py"
    assert ring_gst.WEBRTC_WORKER_SCRIPT.parent.name == "camera_transport"
    assert ring_gst.WEBRTC_WORKER_SCRIPT.is_file()
    with pytest.raises(ValueError):
        GstFrameReader(source="webrtc", decoder="none")
    with pytest.raises(ValueError):
        GstFrameReader(source="rtsp")
    assert sys.version_info >= (3, 11)
