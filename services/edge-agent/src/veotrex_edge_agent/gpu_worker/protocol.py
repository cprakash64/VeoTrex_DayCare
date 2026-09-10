from __future__ import annotations

import json
import socket
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
COMMANDS = frozenset({"HELLO", "HEALTH", "SHUTDOWN"})


class ProtocolError(RuntimeError):
    """Sanitized local protocol failure."""


def encode_message(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large")
    return data


def send_message(sock: socket.socket, value: dict[str, Any]) -> None:
    sock.sendall(encode_message(value))


def receive_message(sock: socket.socket) -> dict[str, Any]:
    data = sock.recv(MAX_MESSAGE_BYTES + 1)
    if not data:
        raise ProtocolError("worker_connection_closed")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large")
    try:
        value = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise ProtocolError("malformed_message") from None
    if not isinstance(value, dict):
        raise ProtocolError("invalid_envelope")
    return value


def request(request_id: str, command: str) -> dict[str, Any]:
    return {
        "command": command,
        "payload": {},
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
    }
