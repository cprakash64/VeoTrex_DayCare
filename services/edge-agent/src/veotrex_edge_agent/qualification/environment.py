from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

GST_PLUGINS = (
    "rtspsrc",
    "rtph264depay",
    "h264parse",
    "rtph265depay",
    "h265parse",
    "nvv4l2decoder",
    "avdec_h264",
    "avdec_h265",
)


def _read_first(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value[:512]
    return None


def _run_safe(command: list[str], timeout: float = 3.0) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - executable is selected from a fixed allowlist
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr).strip()
    return output[:4096] if output else None


def inspect_environment() -> dict[str, Any]:
    gst = shutil.which("gst-inspect-1.0")
    gst_launch = shutil.which("gst-launch-1.0")
    plugins = {name: bool(gst and _run_safe([gst, name], timeout=2.0)) for name in GST_PLUGINS}
    interfaces: list[str] = []
    try:
        interfaces = sorted(name for _, name in __import__("socket").if_nameindex())
    except OSError:
        pass
    l4t = _read_first(("/etc/nv_tegra_release",))
    model = _read_first(("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"))
    timedatectl = shutil.which("timedatectl")
    deepstream = sorted(Path("/opt/nvidia/deepstream").glob("deepstream-*"))
    disk = shutil.disk_usage("/")
    return {
        "architecture": platform.machine(),
        "platform": platform.platform(),
        "os": platform.system(),
        "os_release": platform.release(),
        "kernel": platform.version(),
        "jetson_model": model,
        "l4t_version": l4t,
        "memory_bytes": (
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if hasattr(os, "sysconf")
            else None
        ),
        "storage_total_bytes": disk.total,
        "storage_free_bytes": disk.free,
        "gstreamer_version": _run_safe([gst_launch, "--version"]) if gst_launch else None,
        "gstreamer_plugins": plugins,
        "tls_backend_hint": bool(plugins["rtspsrc"]),
        "deepstream_installed": bool(deepstream),
        "deepstream_paths": [str(path) for path in deepstream],
        "network_interfaces": interfaces,
        "time_synchronization": (
            _run_safe([timedatectl, "show", "--property=NTPSynchronized", "--value"])
            if timedatectl
            else None
        ),
    }


def environment_json() -> str:
    return json.dumps(inspect_environment(), indent=2, sort_keys=True)
