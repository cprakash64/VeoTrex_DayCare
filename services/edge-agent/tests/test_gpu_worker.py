from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from veotrex_edge_agent.gpu_worker.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    encode_message,
    receive_message,
    request,
)
from veotrex_edge_agent.gpu_worker.runtime import INPUT_BYTES
from veotrex_edge_agent.gpu_worker.supervisor import (
    GpuWorkerSupervisor,
    RestartPolicy,
    SupervisorState,
    WorkerConfig,
    WorkerFailure,
)

FAKE = r"""#!/usr/bin/python3
import argparse, json, os, socket, time
p=argparse.ArgumentParser(); p.add_argument("--fd", type=int, required=True); a=p.parse_args()
s=socket.socket(fileno=a.fd)
mode=__file__.rsplit("/",1)[-1].split(".",1)[0]
started=time.monotonic()
if mode == "crash_start": os._exit(7)
def result():
 return {
  "protocol_version":1, "worker_version":"fake", "python_version":"3.12.3",
  "architecture":"aarch64", "tensorrt_version":"10.16.2.10",
  "cuda_runtime_version":13020, "cuda_driver_version":13020,
  "cuda_device_count":1, "worker_pid":os.getpid(),
  "worker_uptime_seconds":time.monotonic()-started, "status":"READY",
  "secret_present":any(
   "SECRET" in k or "TOKEN" in k or "DATABASE" in k for k in os.environ
  ),
 }
while True:
 d=s.recv(65537)
 if mode == "startup_timeout": time.sleep(1); continue
 if mode == "malformed": s.send(b"{"); continue
 if mode == "oversized": s.send(b"x"*65537); continue
 v=json.loads(d); rid=v["request_id"]
 if mode == "wrong_id": rid="stale"
 if mode == "version_mismatch":
  r=result(); r["protocol_version"]=2
 else: r=result()
 if v["command"] == "HEALTH" and mode == "health_timeout": time.sleep(1); continue
 if v["command"] == "HEALTH" and mode == "crash_health": os._exit(8)
 if v["command"] == "SHUTDOWN" and mode == "shutdown_timeout": time.sleep(1); continue
 if mode == "error_secret":
  out={"protocol_version":1,"request_id":rid,"ok":False,"result":None,"error":"sensitive-test-marker"}
 else:
  out={"protocol_version":1,"request_id":rid,"ok":True,"result":r,"error":None}
 s.send(json.dumps(out,separators=(",",":"),sort_keys=True).encode())
 if v["command"] == "SHUTDOWN": break
"""


def fake(tmp_path: Path, mode: str = "valid") -> Path:
    path = tmp_path / f"{mode}.py"
    path.write_text(FAKE)
    return path


def supervisor(tmp_path: Path, mode: str = "valid", **kwargs: object) -> GpuWorkerSupervisor:
    config = WorkerConfig(
        startup_timeout_seconds=float(kwargs.get("startup", 0.15)),
        health_timeout_seconds=float(kwargs.get("health", 0.15)),
        shutdown_timeout_seconds=float(kwargs.get("shutdown", 0.15)),
        restart=kwargs.get("restart", RestartPolicy(max_attempts=2, initial_backoff_seconds=0)),  # type: ignore[arg-type]
    )
    return GpuWorkerSupervisor(config, worker_path=fake(tmp_path, mode), expected_root=tmp_path)


def test_valid_hello_handshake(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    hello = subject.start()
    assert hello["status"] == "READY" and subject.state is SupervisorState.READY
    subject.stop()


def test_valid_health_response_and_correlation(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    assert subject.health()["cuda_device_count"] == 1
    assert subject.metrics.last_successful_health_monotonic is not None
    subject.stop()


def test_graceful_shutdown_has_no_child(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    pid = subject.pid
    subject.stop()
    assert subject.state is SupervisorState.STOPPED and subject.pid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # type: ignore[arg-type]


def test_protocol_version_mismatch(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, "version_mismatch")
    with pytest.raises(WorkerFailure, match="protocol_version_mismatch"):
        subject.start()


def test_unknown_command_is_rejected_locally(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    with pytest.raises(WorkerFailure, match="invalid_command"):
        subject._call("NOPE", timeout=0.1)


def test_malformed_response_is_safe(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, "malformed")
    with pytest.raises(WorkerFailure, match="malformed_message"):
        subject.start()


def test_oversized_response_is_rejected(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, "oversized")
    with pytest.raises(WorkerFailure, match="message_too_large"):
        subject.start()


def test_startup_timeout(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailure, match="worker_timeout"):
        supervisor(tmp_path, "startup_timeout").start()


def test_health_timeout_transitions_degraded(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, "health_timeout")
    subject.start()
    with pytest.raises(WorkerFailure):
        subject.health()
    assert subject.state is SupervisorState.DEGRADED
    subject.stop()


def test_worker_crash_during_startup(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailure):
        supervisor(tmp_path, "crash_start").start()


def test_worker_crash_after_ready(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, "crash_health")
    subject.start()
    with pytest.raises(WorkerFailure):
        subject.health()
    assert subject.state is SupervisorState.DEGRADED
    subject.stop()


def test_bounded_restart_and_circuit(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, restart=RestartPolicy(max_attempts=2, initial_backoff_seconds=0))
    subject.start()
    subject.recover()
    subject.recover()
    with pytest.raises(WorkerFailure, match="restart_circuit_open"):
        subject.recover()
    assert subject.metrics.restarts_total == 2 and subject.state is SupervisorState.FAILED


def test_successful_recovery(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    first = subject.pid
    subject.recover()
    assert subject.state is SupervisorState.READY and subject.pid != first
    subject.stop()


def test_worker_executable_missing(tmp_path: Path) -> None:
    subject = GpuWorkerSupervisor(
        worker_path=fake(tmp_path), expected_root=tmp_path, executable=tmp_path / "missing"
    )
    with pytest.raises(WorkerFailure, match="worker_file_missing"):
        subject.start()


def test_worker_path_validation(tmp_path: Path) -> None:
    outside = fake(tmp_path, "valid")
    subject = GpuWorkerSupervisor(worker_path=outside, expected_root=tmp_path / "inside")
    with pytest.raises(WorkerFailure):
        subject.start()


def test_worker_symlink_rejected(tmp_path: Path) -> None:
    target = fake(tmp_path, "valid")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    with pytest.raises(WorkerFailure):
        GpuWorkerSupervisor(worker_path=link, expected_root=tmp_path).start()


def test_environment_secret_stripping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEOTREX_DATABASE_URL", "test-only-value")
    subject = supervisor(tmp_path)
    assert subject.start()["secret_present"] is False
    assert "VEOTREX_DATABASE_URL" not in subject.sanitized_environment
    subject.stop()


def test_wrong_request_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailure, match="request_id_mismatch"):
        supervisor(tmp_path, "wrong_id").start()


def test_forced_termination_after_shutdown_timeout(tmp_path: Path) -> None:
    subject = supervisor(tmp_path, "shutdown_timeout")
    subject.start()
    subject.stop()
    assert subject.pid is None


def test_repeated_start_stop_is_idempotent(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    subject.start()
    subject.stop()
    subject.stop()
    assert subject.state is SupervisorState.STOPPED


def test_health_cannot_report_ready_before_handshake(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailure, match="worker_not_ready"):
        supervisor(tmp_path).health()


def test_status_and_metrics_are_bounded(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    status = subject.status()
    assert status["metrics"]["veotrex_gpu_worker_up"] == 1
    assert "request_id" not in json.dumps(status)
    subject.stop()


def test_deterministic_json_and_message_limit() -> None:
    assert encode_message({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    with pytest.raises(ProtocolError, match="message_too_large"):
        encode_message({"x": "a" * MAX_MESSAGE_BYTES})


def test_receive_rejects_invalid_utf8() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    right.send(b"\xff")
    with pytest.raises(ProtocolError, match="malformed_message"):
        receive_message(left)
    left.close()
    right.close()


def test_request_envelope_is_exact() -> None:
    assert request("id", "HELLO") == {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "id",
        "command": "HELLO",
        "payload": {},
    }


def test_state_transition_sequence(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    assert subject.state is SupervisorState.STOPPED
    subject.start()
    assert subject.state is SupervisorState.READY
    subject.stop()
    assert subject.state is SupervisorState.STOPPED


def test_worker_error_text_is_redacted(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailure, match="^worker_error$") as captured:
        supervisor(tmp_path, "error_secret").start()
    assert "sensitive-test-marker" not in str(captured.value)


def test_inference_backpressure_is_immediate_and_bounded(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    subject._inference_lane.acquire()
    try:
        with pytest.raises(WorkerFailure, match="worker_busy"):
            subject.infer_tensor(bytes(INPUT_BYTES), frame_id="busy")
        assert subject.metrics.backpressure_drops_total == 1
    finally:
        subject._inference_lane.release()
        subject.stop()


def test_expired_inference_is_rejected_before_fd_creation(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    with pytest.raises(WorkerFailure, match="request_expired"):
        subject.infer_tensor(bytes(INPUT_BYTES), frame_id="expired", deadline_monotonic_ns=1)
    assert subject.metrics.expired_frames_total == 1
    subject.stop()


def test_wrong_tensor_size_is_rejected_before_transport(tmp_path: Path) -> None:
    subject = supervisor(tmp_path)
    subject.start()
    with pytest.raises(WorkerFailure, match="invalid_tensor_size"):
        subject.infer_tensor(b"too-short", frame_id="bad")
    subject.stop()
