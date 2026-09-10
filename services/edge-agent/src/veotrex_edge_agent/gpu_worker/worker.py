#!/usr/bin/python3
"""Standalone system-Python TensorRT control worker; standard library only."""

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
from typing import Any

PROTOCOL_VERSION = 1
WORKER_VERSION = "1.0.0"
MAX_MESSAGE_BYTES = 65_536
EXPECTED_TENSORRT_PREFIX = "10.16.2"
EXPECTED_CUDA_RUNTIME = 13_020
COMMANDS = frozenset({"HELLO", "HEALTH", "SHUTDOWN"})
REQUEST_FIELDS = frozenset({"command", "payload", "protocol_version", "request_id"})


def probe() -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "architecture": platform.machine(),
        "cuda_device_count": 0,
        "cuda_driver_version": None,
        "cuda_runtime_version": None,
        "latest_probe_monotonic": started,
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
        import tensorrt as trt  # type: ignore[import-not-found]  # apt system binding

        result["tensorrt_import_ok"] = True
        result["tensorrt_version"] = trt.__version__
        result["tensorrt_version_ok"] = trt.__version__.startswith(EXPECTED_TENSORRT_PREFIX)
        cuda = ctypes.CDLL(ctypes.util.find_library("cudart") or "libcudart.so.13")
        runtime, driver, count = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        runtime_rc = cuda.cudaRuntimeGetVersion(ctypes.byref(runtime))
        driver_rc = cuda.cudaDriverGetVersion(ctypes.byref(driver))
        count_rc = cuda.cudaGetDeviceCount(ctypes.byref(count))
        result.update(
            cuda_runtime_version=runtime.value,
            cuda_driver_version=driver.value,
            cuda_device_count=count.value,
            cuda_probe_ok=runtime_rc == driver_rc == count_rc == 0,
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
    if len(data) <= MAX_MESSAGE_BYTES:
        sock.sendall(data)


def serve(fd: int) -> int:
    os.umask(0o077)
    sock = socket.socket(fileno=fd)
    started = time.monotonic()
    startup = probe()
    while True:
        data = sock.recv(MAX_MESSAGE_BYTES + 1)
        if not data:
            return 0
        if len(data) > MAX_MESSAGE_BYTES:
            send(sock, response("invalid", error="message_too_large"))
            continue
        try:
            value = json.loads(data.decode("utf-8", errors="strict"))
            if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
                raise ValueError
            request_id = value["request_id"]
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                raise ValueError
            if value["protocol_version"] != PROTOCOL_VERSION:
                send(sock, response(request_id, error="protocol_version_mismatch"))
                continue
            command = value["command"]
            if command not in COMMANDS or value["payload"] != {}:
                send(sock, response(request_id, error="unknown_command"))
                continue
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            send(sock, response("invalid", error="malformed_message"))
            continue
        if command == "HELLO":
            current = dict(startup)
        elif command == "HEALTH":
            current = probe()
        else:
            send(sock, response(request_id, result={"status": "STOPPED"}))
            return 0
        current["worker_uptime_seconds"] = time.monotonic() - started
        send(sock, response(request_id, result=current))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    arguments = parser.parse_args()
    try:
        return serve(arguments.fd)
    except Exception as exc:
        print(f"gpu_worker_failed category={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
