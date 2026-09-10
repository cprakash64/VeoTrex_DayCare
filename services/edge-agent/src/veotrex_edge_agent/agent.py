import asyncio

import structlog

from veotrex_edge_agent.config import EdgeSettings
from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.gpu_worker.supervisor import WorkerFailure


class EdgeAgent:
    """Lifecycle shell only; camera and inference work are intentionally absent."""

    def __init__(
        self, settings: EdgeSettings, gpu_worker: GpuWorkerSupervisor | None = None
    ) -> None:
        self.settings = settings
        self.gpu_worker = gpu_worker
        self._stop = asyncio.Event()
        self._logger = structlog.get_logger()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_shutdown(self) -> None:
        self._logger.info("shutdown_requested", node_id=str(self.settings.node_id))
        self._stop.set()

    async def run(self) -> None:
        self._logger.info(
            "edge_agent_started",
            service=self.settings.service_name,
            environment=self.settings.environment,
            version=self.settings.version,
            node_id=str(self.settings.node_id),
        )
        monitor: asyncio.Task[None] | None = None
        if self.gpu_worker is not None:
            try:
                await asyncio.to_thread(self.gpu_worker.start)
                monitor = asyncio.create_task(self._monitor_gpu_worker())
            except WorkerFailure as exc:
                self._logger.error("gpu_worker_start_failed", category=str(exc))
        await self._stop.wait()
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if self.gpu_worker is not None:
            await asyncio.to_thread(self.gpu_worker.stop)
        self._logger.info("edge_agent_stopped", node_id=str(self.settings.node_id))

    async def _monitor_gpu_worker(self) -> None:
        assert self.gpu_worker is not None
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            try:
                await asyncio.to_thread(self.gpu_worker.health)
            except WorkerFailure as exc:
                self._logger.warning("gpu_worker_health_failed", category=str(exc))
                try:
                    await asyncio.to_thread(self.gpu_worker.recover)
                except WorkerFailure as recovery_exc:
                    self._logger.error("gpu_worker_recovery_failed", category=str(recovery_exc))
