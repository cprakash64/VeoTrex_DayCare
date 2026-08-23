import asyncio

import structlog

from veotrex_edge_agent.config import EdgeSettings


class EdgeAgent:
    """Lifecycle shell only; camera and inference work are intentionally absent."""

    def __init__(self, settings: EdgeSettings) -> None:
        self.settings = settings
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
        await self._stop.wait()
        self._logger.info("edge_agent_stopped", node_id=str(self.settings.node_id))
