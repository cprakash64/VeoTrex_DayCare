"""sd_notify client: protocol shape, no-op outside systemd, bounded secret-free status."""

from __future__ import annotations

import os
import socket
from pathlib import Path

from veotrex_edge_agent.sdnotify import MAX_STATUS_CHARS, SystemdNotifier


def listener(tmp_path: Path) -> tuple[socket.socket, str]:
    path = str(tmp_path / "notify.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(2)
    return sock, path


def test_disabled_outside_systemd() -> None:
    notifier = SystemdNotifier.from_environment({})
    assert notifier.enabled is False
    assert notifier.watchdog_interval_seconds is None
    assert notifier.ready("ready") is False
    assert notifier.watchdog() is False
    assert notifier.stopping("bye") is False


def test_ready_status_watchdog_and_stopping_datagrams(tmp_path: Path) -> None:
    sock, path = listener(tmp_path)
    try:
        notifier = SystemdNotifier.from_environment(
            {"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid())}
        )
        assert notifier.enabled and notifier.watchdog_interval_seconds == 30.0
        assert notifier.ready("ready commit=abc worker=READY")
        assert sock.recv(4096) == b"READY=1\nSTATUS=ready commit=abc worker=READY"
        assert notifier.status("degraded\nline two   spaced")
        assert sock.recv(4096) == b"STATUS=degraded line two spaced"
        assert notifier.watchdog()
        assert sock.recv(4096) == b"WATCHDOG=1"
        assert notifier.stopping("stopping")
        assert sock.recv(4096) == b"STOPPING=1\nSTATUS=stopping"
    finally:
        sock.close()


def test_status_is_bounded(tmp_path: Path) -> None:
    sock, path = listener(tmp_path)
    try:
        notifier = SystemdNotifier(path)
        assert notifier.status("x" * (MAX_STATUS_CHARS + 50))
        assert sock.recv(4096) == b"STATUS=" + b"x" * MAX_STATUS_CHARS
    finally:
        sock.close()


def test_watchdog_for_another_pid_is_ignored(tmp_path: Path) -> None:
    notifier = SystemdNotifier.from_environment(
        {"NOTIFY_SOCKET": str(tmp_path / "s"), "WATCHDOG_USEC": "1000000", "WATCHDOG_PID": "1"}
    )
    assert notifier.watchdog_interval_seconds is None


def test_unreachable_socket_never_raises(tmp_path: Path) -> None:
    notifier = SystemdNotifier(str(tmp_path / "absent.sock"))
    assert notifier.enabled
    assert notifier.ready("ready") is False
