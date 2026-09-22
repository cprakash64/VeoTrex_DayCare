"""Minimal sd_notify(3) client for systemd ``Type=notify`` supervision (V1-00B).

The agent tells systemd when it is ready, keeps a one-line operator-visible status current
(``systemctl status`` shows it as ``Status:``), pings the watchdog from its event loop, and
announces shutdown. No dependency on the ``systemd`` Python package: the protocol is a datagram
of ``KEY=VALUE`` lines to the socket named by ``NOTIFY_SOCKET``. Outside systemd every call is a
no-op, so ``veotrex-edge-agent`` behaves identically when run by hand.

Nothing here ever carries a secret: the status line is composed from state names, counts, the
source commit and the package version.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping

MAX_STATUS_CHARS = 200


class SystemdNotifier:
    def __init__(self, socket_path: str | None, watchdog_usec: int | None = None) -> None:
        self._path = socket_path or None
        self._watchdog_usec = watchdog_usec if watchdog_usec and watchdog_usec > 0 else None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> SystemdNotifier:
        env = os.environ if environ is None else environ
        path = env.get("NOTIFY_SOCKET") or None
        watchdog: int | None = None
        raw = env.get("WATCHDOG_USEC")
        if raw and raw.isdigit():
            # WATCHDOG_PID, when present, names the process expected to ping; ignore a value
            # inherited from a parent that was the intended target.
            owner = env.get("WATCHDOG_PID")
            if not owner or owner == str(os.getpid()):
                watchdog = int(raw)
        return cls(path, watchdog)

    @property
    def enabled(self) -> bool:
        return self._path is not None

    @property
    def watchdog_interval_seconds(self) -> float | None:
        """Half the systemd watchdog period, the interval at which ``WATCHDOG=1`` is due."""
        if self._watchdog_usec is None or not self.enabled:
            return None
        return self._watchdog_usec / 2 / 1_000_000

    def notify(self, *fields: str) -> bool:
        """Send one datagram. Returns False when not under systemd or the send failed."""
        if self._path is None or not fields:
            return False
        address = self._path
        if address.startswith("@"):
            address = "\0" + address[1:]
        payload = "\n".join(fields).encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(address)
                sock.sendall(payload)
        except OSError:
            return False
        return True

    @staticmethod
    def _status_line(text: str) -> str:
        single = " ".join(text.split())
        return f"STATUS={single[:MAX_STATUS_CHARS]}"

    def ready(self, status: str) -> bool:
        return self.notify("READY=1", self._status_line(status))

    def status(self, status: str) -> bool:
        return self.notify(self._status_line(status))

    def watchdog(self) -> bool:
        return self.notify("WATCHDOG=1")

    def stopping(self, status: str) -> bool:
        return self.notify("STOPPING=1", self._status_line(status))
