from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import structlog

from veotrex_edge_agent.gpu_worker.fd_transport import create_sealed_memfd
from veotrex_edge_agent.gpu_worker.metrics import GpuWorkerMetrics
from veotrex_edge_agent.gpu_worker.protocol import (
    COMMANDS,
    PROTOCOL_VERSION,
    ProtocolError,
    receive_message,
    request,
    send_message,
)
from veotrex_edge_agent.gpu_worker.runtime import INPUT_BYTES, INPUT_SHAPE, MODEL_ID

SYSTEM_PYTHON = Path("/usr/bin/python3")
EXPECTED_TENSORRT_PREFIX = "10.16.2"
EXPECTED_CUDA_RUNTIME = 13_020
_SAFE_ENV = MappingProxyType({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
_SAFE_WORKER_ERRORS = frozenset(
    {
        "malformed_message",
        "message_too_large",
        "protocol_version_mismatch",
        "unknown_command",
        "unknown_model_id",
        "invalid_fd_count",
        "unexpected_fd",
        "invalid_tensor_metadata",
        "invalid_tensor_contract",
        "invalid_tensor_size",
        "mutable_tensor_rejected",
        "request_expired",
        "model_not_ready",
        "model_load_failed",
        "artifact_unavailable",
        "artifact_hash_mismatch",
        "artifact_size_mismatch",
        "artifact_symlink_rejected",
        "manifest_contract_mismatch",
        "platform_incompatible",
        "tensorrt_version_mismatch",
        "engine_deserialize_failed",
        "engine_tensor_contract_mismatch",
        "context_create_failed",
        "cuda_device_error",
        "cuda_stream_create_failed",
        "cuda_input_allocate_failed",
        "cuda_output_allocate_failed",
        "input_address_failed",
        "output_address_failed",
        "cuda_h2d_failed",
        "cuda_d2h_failed",
        "cuda_sync_failed",
        "tensorrt_execute_failed",
        "inference_failed",
        "tensor_map_failed",
        "invalid_payload",
    }
)


class SupervisorState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    max_attempts: int = 3
    window_seconds: float = 60.0
    initial_backoff_seconds: float = 0.1
    maximum_backoff_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    startup_timeout_seconds: float = 30.0
    health_timeout_seconds: float = 2.0
    shutdown_timeout_seconds: float = 2.0
    model_timeout_seconds: float = 30.0
    inference_timeout_seconds: float = 5.0
    restart: RestartPolicy = RestartPolicy()


class WorkerFailure(RuntimeError):
    """Safe parent-side worker failure category."""


class GpuWorkerSupervisor:
    def __init__(
        self,
        config: WorkerConfig | None = None,
        *,
        worker_path: Path | None = None,
        executable: Path = SYSTEM_PYTHON,
        expected_root: Path | None = None,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self.config = config or WorkerConfig()
        self._worker_path = worker_path or Path(__file__).with_name("worker.py")
        self._executable = executable
        self._expected_root = expected_root or Path(__file__).parents[2]
        self._clock = clock
        self._sleep = sleeper
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._state = SupervisorState.STOPPED
        self._details: dict[str, Any] | None = None
        self._restart_times: list[float] = []
        self.metrics = GpuWorkerMetrics()
        self._logger = structlog.get_logger()
        self._inference_lane = threading.Lock()
        self._rpc_lock = threading.Lock()

    @property
    def state(self) -> SupervisorState:
        if (
            self._state is SupervisorState.READY
            and self._process
            and self._process.poll() is not None
        ):
            self._transition(SupervisorState.DEGRADED, "worker_exited")
        return self._state

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    @property
    def sanitized_environment(self) -> dict[str, str]:
        return dict(_SAFE_ENV)

    def _transition(self, state: SupervisorState, category: str) -> None:
        previous = self._state
        self._state = state
        self._logger.info(
            "gpu_worker_state_changed",
            previous=previous.value,
            state=state.value,
            category=category,
            worker_pid=self.pid,
            protocol_version=PROTOCOL_VERSION,
            tensorrt_version=(self._details or {}).get("tensorrt_version"),
            cuda_runtime_version=(self._details or {}).get("cuda_runtime_version"),
            cuda_device_count=(self._details or {}).get("cuda_device_count"),
        )

    def _validate_paths(self) -> tuple[Path, Path]:
        executable = self._executable.resolve(strict=True)
        worker = self._worker_path.resolve(strict=True)
        root = self._expected_root.resolve(strict=True)
        if executable != SYSTEM_PYTHON.resolve(strict=True):
            raise WorkerFailure("invalid_worker_executable")
        if not worker.is_file() or not worker.is_relative_to(root):
            raise WorkerFailure("invalid_worker_path")
        if self._worker_path.is_symlink():
            raise WorkerFailure("invalid_worker_path")
        return executable, worker

    def start(self) -> dict[str, Any]:
        if self.state is SupervisorState.READY:
            return dict(self._details or {})
        if self._process is not None:
            self.stop()
        self._transition(SupervisorState.STARTING, "start_requested")
        try:
            executable, worker = self._validate_paths()
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            parent.settimeout(self.config.startup_timeout_seconds)
            os.set_inheritable(child.fileno(), True)
            self._process = subprocess.Popen(  # noqa: S603 - executable and worker are validated
                [str(executable), "-I", str(worker), "--fd", str(child.fileno())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(child.fileno(),),
                env=self.sanitized_environment,
                start_new_session=True,
            )
            child.close()
            self._socket = parent
            details = self._call("HELLO", timeout=self.config.startup_timeout_seconds)
            self._validate_capabilities(details)
        except Exception as exc:
            self.metrics.handshake_failures_total += 1
            self._cleanup_process(force=True)
            self._transition(SupervisorState.FAILED, self._safe_category(exc))
            raise WorkerFailure(self._safe_category(exc)) from None
        self._details = details
        self._transition(SupervisorState.READY, "handshake_succeeded")
        return dict(details)

    def _validate_capabilities(self, details: dict[str, Any]) -> None:
        if details.get("protocol_version") != PROTOCOL_VERSION:
            raise WorkerFailure("protocol_version_mismatch")
        if details.get("architecture") != "aarch64":
            raise WorkerFailure("architecture_mismatch")
        if not str(details.get("tensorrt_version", "")).startswith(EXPECTED_TENSORRT_PREFIX):
            raise WorkerFailure("tensorrt_version_mismatch")
        if details.get("cuda_runtime_version") != EXPECTED_CUDA_RUNTIME:
            raise WorkerFailure("cuda_runtime_version_mismatch")
        if (
            not isinstance(details.get("cuda_device_count"), int)
            or details["cuda_device_count"] < 1
        ):
            raise WorkerFailure("cuda_device_unavailable")
        if details.get("status") != "READY":
            raise WorkerFailure("worker_unhealthy")

    def _call(
        self,
        command: str,
        *,
        timeout: float,
        payload: dict[str, Any] | None = None,
        fds: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        if command not in COMMANDS or self._socket is None:
            raise WorkerFailure("invalid_command")
        request_id = uuid4().hex
        with self._rpc_lock:
            self._socket.settimeout(timeout)
            try:
                send_message(self._socket, request(request_id, command, payload), fds)
                response = receive_message(self._socket)
            except (OSError, ProtocolError) as exc:
                self.metrics.protocol_errors_total += 1
                raise WorkerFailure(self._safe_category(exc)) from None
        expected = {"error", "ok", "protocol_version", "request_id", "result"}
        if set(response) != expected:
            raise WorkerFailure("invalid_response_envelope")
        if response.get("protocol_version") != PROTOCOL_VERSION:
            raise WorkerFailure("protocol_version_mismatch")
        if response.get("request_id") != request_id:
            raise WorkerFailure("request_id_mismatch")
        if response.get("ok") is not True or not isinstance(response.get("result"), dict):
            error = response.get("error")
            category = error if error in _SAFE_WORKER_ERRORS else "worker_error"
            raise WorkerFailure(category)
        result = response["result"]
        assert isinstance(result, dict)
        return result

    def load_model(self, model_id: str = MODEL_ID) -> dict[str, Any]:
        if self.state is not SupervisorState.READY:
            raise WorkerFailure("worker_not_ready")
        result = self._call(
            "LOAD_MODEL", timeout=self.config.model_timeout_seconds, payload={"model_id": model_id}
        )
        self._details = {**(self._details or {}), **result}
        return result

    def model_status(self) -> dict[str, Any]:
        if self.state is not SupervisorState.READY:
            raise WorkerFailure("worker_not_ready")
        return self._call("MODEL_STATUS", timeout=self.config.health_timeout_seconds)

    def unload_model(self) -> dict[str, Any]:
        if self.state is not SupervisorState.READY:
            raise WorkerFailure("worker_not_ready")
        result = self._call("UNLOAD_MODEL", timeout=self.config.model_timeout_seconds)
        self._details = {**(self._details or {}), **result}
        return result

    def infer_tensor(
        self,
        tensor: bytes | bytearray | memoryview,
        *,
        frame_id: str,
        capture_monotonic_ns: int | None = None,
        deadline_monotonic_ns: int | None = None,
        qualification_digest: bool = False,
    ) -> dict[str, Any]:
        if self.state is not SupervisorState.READY:
            raise WorkerFailure("worker_not_ready")
        if len(tensor) != INPUT_BYTES:
            raise WorkerFailure("invalid_tensor_size")
        if deadline_monotonic_ns is not None and self._clock() * 1e9 >= deadline_monotonic_ns:
            self.metrics.expired_frames_total += 1
            raise WorkerFailure("request_expired")
        if not self._inference_lane.acquire(blocking=False):
            self.metrics.backpressure_drops_total += 1
            raise WorkerFailure("worker_busy")
        self.metrics.inference_requests_total += 1
        descriptor = -1
        started = time.perf_counter_ns()
        try:
            descriptor = create_sealed_memfd(tensor)
            payload = {
                "frame_id": frame_id,
                "dtype": "float32",
                "shape": list(INPUT_SHAPE),
                "layout": "NCHW",
                "byte_order": "little",
                "capture_monotonic_ns": capture_monotonic_ns,
                "deadline_monotonic_ns": deadline_monotonic_ns,
                "qualification_digest": qualification_digest,
            }
            result = self._call(
                "INFER_TENSOR",
                timeout=self.config.inference_timeout_seconds,
                payload=payload,
                fds=(descriptor,),
            )
            self.metrics.inference_successes_total += 1
            return result
        except WorkerFailure as exc:
            self.metrics.inference_failures_total += 1
            if str(exc) == "request_expired":
                self.metrics.expired_frames_total += 1
            raise
        finally:
            self.metrics.parent_rpc_roundtrip_ms = (time.perf_counter_ns() - started) / 1e6
            if descriptor >= 0:
                os.close(descriptor)
            self._inference_lane.release()

    def health(self) -> dict[str, Any]:
        if self.state is not SupervisorState.READY:
            raise WorkerFailure("worker_not_ready")
        try:
            result = self._call("HEALTH", timeout=self.config.health_timeout_seconds)
            self._validate_capabilities(result)
        except WorkerFailure:
            self.metrics.health_failures_total += 1
            self._transition(SupervisorState.DEGRADED, "health_failed")
            raise
        self.metrics.last_successful_health_monotonic = self._clock()
        self._details = result
        return result

    def recover(self) -> dict[str, Any]:
        now = self._clock()
        window = self.config.restart.window_seconds
        self._restart_times = [value for value in self._restart_times if now - value <= window]
        if len(self._restart_times) >= self.config.restart.max_attempts:
            self._transition(SupervisorState.FAILED, "restart_circuit_open")
            raise WorkerFailure("restart_circuit_open")
        attempt = len(self._restart_times)
        delay = min(
            self.config.restart.initial_backoff_seconds * (2**attempt),
            self.config.restart.maximum_backoff_seconds,
        )
        self._restart_times.append(now)
        self.metrics.restarts_total += 1
        self.stop()
        self._sleep(delay)
        return self.start()

    def stop(self) -> None:
        if self._process is None:
            self._transition(SupervisorState.STOPPED, "already_stopped")
            return
        if self._process.poll() is None and self._socket is not None:
            try:
                self._call("SHUTDOWN", timeout=self.config.shutdown_timeout_seconds)
                self._process.wait(timeout=self.config.shutdown_timeout_seconds)
            except (WorkerFailure, subprocess.TimeoutExpired):
                self._process.terminate()
                try:
                    self._process.wait(timeout=self.config.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=self.config.shutdown_timeout_seconds)
        self._cleanup_process(force=False)
        self._transition(SupervisorState.STOPPED, "stop_complete")

    def _cleanup_process(self, *, force: bool) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._process is not None and self._process.poll() is None and force:
            self._process.kill()
            self._process.wait(timeout=self.config.shutdown_timeout_seconds)
        self._process = None

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "worker_pid": self.pid,
            "worker_uptime_seconds": (self._details or {}).get("worker_uptime_seconds"),
            "restart_count": self.metrics.restarts_total,
            "metrics": self.metrics.snapshot(
                up=self.state is SupervisorState.READY, details=self._details
            ),
        }

    @staticmethod
    def _safe_category(exc: BaseException) -> str:
        if isinstance(exc, socket.timeout | TimeoutError):
            return "worker_timeout"
        if isinstance(exc, FileNotFoundError):
            return "worker_file_missing"
        if isinstance(exc, ProtocolError):
            return str(exc)[:64]
        if isinstance(exc, WorkerFailure):
            return str(exc)[:64]
        return type(exc).__name__.lower()[:64]
