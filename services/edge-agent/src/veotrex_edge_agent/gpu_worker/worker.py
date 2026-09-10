#!/usr/bin/python3
"""Standalone system-Python TensorRT inference worker."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fd_transport import (  # type: ignore[import-not-found]
    FileDescriptorError,
    receive_packet,
    validate_sealed_memfd,
)
from runtime import (  # type: ignore[import-not-found]
    INPUT_BYTES,
    INPUT_SHAPE,
    MODEL_ID,
    RuntimeFailure,
    TensorRtRuntime,
)

PROTOCOL_VERSION = 1
WORKER_VERSION = "1.1.0"
MAX_MESSAGE_BYTES = 65_536
EXPECTED_TENSORRT_PREFIX = "10.16.2"
EXPECTED_CUDA_RUNTIME = 13_020
COMMANDS = frozenset(
    {"HELLO", "HEALTH", "SHUTDOWN", "LOAD_MODEL", "MODEL_STATUS", "INFER_TENSOR", "UNLOAD_MODEL"}
)
REQUEST_FIELDS = frozenset({"command", "payload", "protocol_version", "request_id"})


def probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "architecture": platform.machine(),
        "cuda_device_count": 0,
        "cuda_driver_version": None,
        "cuda_runtime_version": None,
        "latest_probe_monotonic": time.monotonic(),
        "platform_ok": platform.machine() == "aarch64",
        "protocol_version": PROTOCOL_VERSION,
        "python_version": platform.python_version(),
        "python_version_ok": sys.version_info[:2] == (3, 12),
        "status": "FAILED",
        "tensorrt_import_ok": False,
        "tensorrt_version": None,
        "tensorrt_version_ok": False,
        "worker_pid": os.getpid(),
        "worker_version": WORKER_VERSION,
    }
    try:
        import tensorrt as trt  # type: ignore[import-not-found]

        result.update(
            tensorrt_import_ok=True,
            tensorrt_version=trt.__version__,
            tensorrt_version_ok=trt.__version__.startswith(EXPECTED_TENSORRT_PREFIX),
        )
        cuda = ctypes.CDLL(ctypes.util.find_library("cudart") or "libcudart.so.13")
        runtime, driver, count = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        codes = (
            cuda.cudaRuntimeGetVersion(ctypes.byref(runtime)),
            cuda.cudaDriverGetVersion(ctypes.byref(driver)),
            cuda.cudaGetDeviceCount(ctypes.byref(count)),
        )
        result.update(
            cuda_runtime_version=runtime.value,
            cuda_driver_version=driver.value,
            cuda_device_count=count.value,
            cuda_probe_ok=all(code == 0 for code in codes),
        )
    except Exception:
        result["cuda_probe_ok"] = False
    gates = (
        result["platform_ok"],
        result["python_version_ok"],
        result["tensorrt_import_ok"],
        result["tensorrt_version_ok"],
        result.get("cuda_probe_ok", False),
        result["cuda_runtime_version"] == EXPECTED_CUDA_RUNTIME,
        result["cuda_device_count"] >= 1,
    )
    result["status"] = "READY" if all(gates) else "FAILED"
    return result


def response(
    request_id: str, *, result: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    return {
        "error": error,
        "ok": error is None,
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "result": result,
    }


def send(sock: socket.socket, value: dict[str, Any]) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(data) > MAX_MESSAGE_BYTES:
        data = json.dumps(
            response(value.get("request_id", "invalid"), error="message_too_large"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    sock.sendall(data)


def parse(data: bytes) -> tuple[str, str, dict[str, Any]]:
    value = json.loads(data.decode("utf-8", errors="strict"))
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise ValueError
    request_id, command, payload = value["request_id"], value["command"], value["payload"]
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise RuntimeFailure("protocol_version_mismatch")
    if command not in COMMANDS or not isinstance(payload, dict):
        raise RuntimeFailure("unknown_command")
    return request_id, command, payload


def validate_infer_payload(payload: dict[str, Any]) -> None:
    required = {
        "frame_id",
        "dtype",
        "shape",
        "layout",
        "byte_order",
        "capture_monotonic_ns",
        "deadline_monotonic_ns",
        "qualification_digest",
    }
    if (
        set(payload) != required
        or not isinstance(payload["frame_id"], str)
        or not payload["frame_id"]
    ):
        raise RuntimeFailure("invalid_tensor_metadata")
    if payload["dtype"] != "float32" or payload["shape"] != list(INPUT_SHAPE):
        raise RuntimeFailure("invalid_tensor_contract")
    if payload["layout"] != "NCHW" or payload["byte_order"] != "little":
        raise RuntimeFailure("invalid_tensor_contract")
    if payload["capture_monotonic_ns"] is not None and not isinstance(
        payload["capture_monotonic_ns"], int
    ):
        raise RuntimeFailure("invalid_tensor_metadata")
    if payload["deadline_monotonic_ns"] is not None and not isinstance(
        payload["deadline_monotonic_ns"], int
    ):
        raise RuntimeFailure("invalid_tensor_metadata")
    if not isinstance(payload["qualification_digest"], bool):
        raise RuntimeFailure("invalid_tensor_metadata")


def serve(fd: int) -> int:
    os.umask(0o077)
    sock = socket.socket(fileno=fd)
    started, startup, runtime = time.monotonic(), probe(), TensorRtRuntime()
    while True:
        descriptors: list[int] = []
        request_id = "invalid"
        try:
            data, descriptors = receive_packet(sock, MAX_MESSAGE_BYTES)
            if not data:
                runtime.unload()
                return 0
            if len(data) > MAX_MESSAGE_BYTES:
                raise RuntimeFailure("message_too_large")
            request_id, command, payload = parse(data)
            if command == "INFER_TENSOR":
                if len(descriptors) != 1:
                    raise RuntimeFailure("invalid_fd_count")
                validate_infer_payload(payload)
                validate_sealed_memfd(descriptors[0], INPUT_BYTES)
                current = runtime.infer(descriptors[0], payload)
            elif descriptors:
                raise RuntimeFailure("unexpected_fd")
            elif command == "LOAD_MODEL":
                if set(payload) != {"model_id"} or payload["model_id"] != MODEL_ID:
                    raise RuntimeFailure("unknown_model_id")
                current = runtime.load(payload["model_id"])
            elif command == "MODEL_STATUS":
                if payload:
                    raise RuntimeFailure("invalid_payload")
                current = runtime.status()
            elif command == "UNLOAD_MODEL":
                if payload:
                    raise RuntimeFailure("invalid_payload")
                current = runtime.unload()
            elif command in {"HELLO", "HEALTH"}:
                if payload:
                    raise RuntimeFailure("invalid_payload")
                current = dict(startup if command == "HELLO" else probe())
                current.update(runtime.status())
                current["worker_uptime_seconds"] = time.monotonic() - started
            else:
                if payload:
                    raise RuntimeFailure("invalid_payload")
                runtime.unload()
                send(sock, response(request_id, result={"status": "STOPPED"}))
                return 0
            send(sock, response(request_id, result=current))
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            send(sock, response(request_id, error="malformed_message"))
        except (RuntimeFailure, FileDescriptorError) as exc:
            send(sock, response(request_id, error=str(exc)[:64]))
        finally:
            for received_fd in descriptors:
                os.close(received_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    try:
        return serve(parser.parse_args().fd)
    except Exception as exc:
        print(f"gpu_worker_failed category={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
