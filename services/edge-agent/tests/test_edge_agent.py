import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from veotrex_edge_agent.agent import EdgeAgent
from veotrex_edge_agent.config import EdgeSettings


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
