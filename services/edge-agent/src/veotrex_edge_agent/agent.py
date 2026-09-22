import asyncio
import time

import structlog

from veotrex_edge_agent.config import EdgeSettings
from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.gpu_worker.supervisor import WorkerFailure
from veotrex_edge_agent.sdnotify import SystemdNotifier


class EdgeAgent:
    """Lifecycle shell only; camera and inference work are intentionally absent.

    Health model (V1-00B):

    * liveness  - the process runs and its event loop answers the systemd watchdog;
    * readiness - configuration validated and the startup sequence completed
                  (``READY=1``); it never depends on any network or provider;
    * degraded  - alive and ready, but the local GPU worker is not READY; the status
                  line names the last safe failure category. The agent keeps running and
                  keeps trying to recover the worker within its bounded restart policy.
    """

    def __init__(
        self,
        settings: EdgeSettings,
        gpu_worker: GpuWorkerSupervisor | None = None,
        notifier: SystemdNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.gpu_worker = gpu_worker
        self._notifier = notifier or SystemdNotifier.from_environment()
        self._stop = asyncio.Event()
        self._logger = structlog.get_logger()
        self._started_monotonic = time.monotonic()
        self._worker_state = "absent" if gpu_worker is None else "STOPPED"
        self._worker_restarts = 0
        self._last_error = "none"

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def degraded(self) -> bool:
        return self.gpu_worker is not None and self._worker_state != "READY"

    def status_line(self) -> str:
        """Bounded, secret-free operator status: shown by ``systemctl status`` and logged."""
        return (
            f"{'degraded' if self.degraded else 'ready'}"
            f" commit={self.settings.source_commit[:12]}"
            f" version={self.settings.version}"
            f" worker={self._worker_state}"
            f" worker_restarts={self._worker_restarts}"
            f" uptime_s={int(time.monotonic() - self._started_monotonic)}"
            f" last_error={self._last_error}"
        )

    def request_shutdown(self) -> None:
        self._logger.info("shutdown_requested", node_id=str(self.settings.node_id))
        self._stop.set()

    async def run(self) -> None:
        self._logger.info(
            "edge_agent_started",
            service=self.settings.service_name,
            environment=self.settings.environment,
            version=self.settings.version,
            source_commit=self.settings.source_commit,
            node_id=str(self.settings.node_id),
            supervised=self._notifier.enabled,
        )
        monitor: asyncio.Task[None] | None = None
        watchdog: asyncio.Task[None] | None = None
        if self.gpu_worker is not None:
            try:
                await asyncio.to_thread(self.gpu_worker.start)
                self._worker_state = "READY"
                monitor = asyncio.create_task(self._monitor_gpu_worker())
            except WorkerFailure as exc:
                self._worker_state = "FAILED"
                self._last_error = f"gpu_worker_start_failed:{exc}"
                self._logger.error("gpu_worker_start_failed", category=str(exc))
        # Readiness is local: configuration is valid and startup completed. A missing GPU
        # worker is reported as degraded, not as "not ready", so an offline or GPU-less node
        # is still a live, supervisable service rather than a dead one.
        self._notifier.ready(self.status_line())
        self._logger.info("edge_agent_ready", status=self.status_line())
        interval = self._notifier.watchdog_interval_seconds
        if interval is not None:
            watchdog = asyncio.create_task(self._ping_watchdog(interval))
        await self._stop.wait()
        self._notifier.stopping(self.status_line())
        for task in (monitor, watchdog):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.gpu_worker is not None:
            await asyncio.to_thread(self.gpu_worker.stop)
            self._worker_state = "STOPPED"
        self._logger.info("edge_agent_stopped", node_id=str(self.settings.node_id))

    async def _ping_watchdog(self, interval: float) -> None:
        """Liveness proof from inside the event loop; systemd kills us if it stops arriving."""
        while True:
            self._notifier.watchdog()
            await asyncio.sleep(interval)

    async def _monitor_gpu_worker(self) -> None:
        assert self.gpu_worker is not None
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            try:
                await asyncio.to_thread(self.gpu_worker.health)
            except WorkerFailure as exc:
                self._worker_state = "DEGRADED"
                self._last_error = f"gpu_worker_health_failed:{exc}"
                self._logger.warning("gpu_worker_health_failed", category=str(exc))
                self._notifier.status(self.status_line())
                try:
                    await asyncio.to_thread(self.gpu_worker.recover)
                except WorkerFailure as recovery_exc:
                    self._worker_state = "FAILED"
                    self._last_error = f"gpu_worker_recovery_failed:{recovery_exc}"
                    self._logger.error("gpu_worker_recovery_failed", category=str(recovery_exc))
                else:
                    self._worker_state = "READY"
                    self._worker_restarts += 1
                    self._logger.info("gpu_worker_recovered", restarts=self._worker_restarts)
                self._notifier.status(self.status_line())
