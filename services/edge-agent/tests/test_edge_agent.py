import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from veotrex_edge_agent.agent import EdgeAgent
from veotrex_edge_agent.config import EdgeSettings
from veotrex_edge_agent.gpu_worker import WorkerFailure


def edge_settings() -> EdgeSettings:
    return EdgeSettings(
        _env_file=None,
        node_id=UUID("00000000-0000-0000-0000-000000000001"),
        environment="test",
        version="test",
        heartbeat_interval_seconds=5,
    )


def test_edge_configuration_requires_node_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEOTREX_EDGE_NODE_ID", raising=False)
    with pytest.raises(ValidationError):
        EdgeSettings(_env_file=None)


async def test_agent_lifecycle_stops_gracefully() -> None:
    agent = EdgeAgent(edge_settings())
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0)
    agent.request_shutdown()
    await asyncio.wait_for(task, timeout=1)
    assert agent.stopping


async def test_gpu_worker_failure_does_not_crash_agent() -> None:
    class FailingWorker:
        stopped = False

        def start(self) -> None:
            raise WorkerFailure("worker_timeout")

        def stop(self) -> None:
            self.stopped = True

    worker = FailingWorker()
    agent = EdgeAgent(edge_settings(), worker)  # type: ignore[arg-type]
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.01)
    assert not task.done()
    agent.request_shutdown()
    await asyncio.wait_for(task, timeout=1)
    assert worker.stopped
