from __future__ import annotations

import asyncio
import os
import platform
import resource
import selectors
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ResourceSample:
    monotonic_seconds: float
    system_cpu_percent: float | None
    process_cpu_percent: float | None
    load_1m: float | None
    process_rss_bytes: int | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    network_received_bytes: int | None
    network_transmitted_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    jetson: dict[str, str | float | int | bool | None]
    metric_availability: dict[str, bool]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _linux_memory() -> tuple[int | None, int | None]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        return values.get("MemTotal"), values.get("MemAvailable")
    except (OSError, ValueError):
        return None, None


def _linux_network() -> tuple[int | None, int | None]:
    try:
        received = transmitted = 0
        for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
            _, values = line.split(":", 1)
            fields = values.split()
            received += int(fields[0])
            transmitted += int(fields[8])
        return received, transmitted
    except (OSError, ValueError, IndexError):
        return None, None


def _linux_cpu_times() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (OSError, ValueError, IndexError):
        return None


def parse_tegrastats(line: str) -> dict[str, str | float | int | bool | None]:
    safe: dict[str, str | float | int | bool | None] = {"available": True}
    tokens = line.split()
    for index, field in enumerate(tokens):
        next_value = tokens[index + 1] if index + 1 < len(tokens) else None
        if field == "RAM" and next_value:
            safe["memory"] = next_value
        elif field == "GR3D_FREQ" and next_value:
            safe["gpu"] = next_value
        elif field in {"POM_5V_IN", "VDD_IN"} and next_value:
            safe["power"] = next_value
        elif field == "CPU" and next_value:
            safe["cpu_frequencies_utilization"] = next_value
        elif field in {"NVDEC", "NVENC", "NVJPG", "VIC"} and next_value:
            safe[field.lower()] = next_value
        elif "@" in field and field.endswith("C"):
            name, value = field.split("@", 1)
            safe[f"temperature_{name.lower()}"] = value
    safe["throttling_observed"] = "throttle" in line.lower()
    return safe


def read_tegrastats_once(
    timeout_seconds: float = 2.0,
) -> dict[str, str | float | int | bool | None]:
    executable = shutil.which("tegrastats")
    if executable is None:
        return {"available": False}
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - path is resolved by shutil.which
            [executable, "--interval", "1000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        if process.stdout is None:
            return {"available": True, "sample_available": False}
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout_seconds)
            line = process.stdout.readline().strip() if ready else ""
    except OSError:
        return {"available": True, "sample_available": False}
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.5)
    return parse_tegrastats(line) if line else {"available": True, "sample_available": False}


class ResourceCollector:
    def __init__(self) -> None:
        self._last_wall: float | None = None
        self._last_cpu: float | None = None
        self._last_system_cpu: tuple[int, int] | None = None

    def sample(self) -> ResourceSample:
        sampled_at = time.monotonic()
        process_cpu = time.process_time()
        cpu_percent: float | None = None
        if self._last_wall is not None and self._last_cpu is not None:
            wall_delta = sampled_at - self._last_wall
            if wall_delta > 0:
                cpu_percent = (process_cpu - self._last_cpu) / wall_delta * 100
        self._last_wall, self._last_cpu = sampled_at, process_cpu
        system_cpu = _linux_cpu_times()
        system_cpu_percent: float | None = None
        if system_cpu is not None and self._last_system_cpu is not None:
            total_delta = system_cpu[0] - self._last_system_cpu[0]
            idle_delta = system_cpu[1] - self._last_system_cpu[1]
            if total_delta > 0:
                system_cpu_percent = (total_delta - idle_delta) / total_delta * 100
        self._last_system_cpu = system_cpu
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = None
        rss_bytes = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if platform.system() != "Darwin":
            rss_bytes *= 1024
        memory_total, memory_available = _linux_memory()
        if memory_total is None and hasattr(os, "sysconf"):
            try:
                memory_total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                memory_available = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
            except (OSError, ValueError):
                pass
        received, transmitted = _linux_network()
        disk_total: int | None
        disk_free: int | None
        try:
            disk = shutil.disk_usage("/")
            disk_total, disk_free = disk.total, disk.free
        except OSError:
            disk_total = disk_free = None
        jetson = read_tegrastats_once()
        return ResourceSample(
            monotonic_seconds=sampled_at,
            system_cpu_percent=system_cpu_percent,
            process_cpu_percent=cpu_percent,
            load_1m=load,
            process_rss_bytes=rss_bytes,
            memory_total_bytes=memory_total,
            memory_available_bytes=memory_available,
            network_received_bytes=received,
            network_transmitted_bytes=transmitted,
            disk_total_bytes=disk_total,
            disk_free_bytes=disk_free,
            jetson=jetson,
            metric_availability={
                "process_cpu": cpu_percent is not None,
                "system_cpu": system_cpu_percent is not None,
                "process_rss": rss_bytes is not None,
                "system_memory": memory_total is not None and memory_available is not None,
                "system_network": received is not None and transmitted is not None,
                "disk": disk_total is not None and disk_free is not None,
                "jetson": bool(jetson.get("available")),
            },
        )

    async def monitor(
        self,
        stop: asyncio.Event,
        destination: list[dict[str, object]],
        interval_seconds: float = 2.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("resource interval must be positive")
        while not stop.is_set():
            destination.append(self.sample().as_dict())
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
