import asyncio
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from veotrex_edge_agent.agent import EdgeAgent
from veotrex_edge_agent.config import EdgeSettings
from veotrex_edge_agent.gpu_worker import WorkerFailure
from veotrex_edge_agent.main import EXIT_CONFIG, load_settings
from veotrex_edge_agent.sdnotify import SystemdNotifier


def edge_settings() -> EdgeSettings:
    return EdgeSettings(
        _env_file=None,
        node_id=UUID("00000000-0000-0000-0000-000000000001"),
        environment="test",
        version="test",
        source_commit="0123456789abcdef0123456789abcdef01234567",
        heartbeat_interval_seconds=5,
    )


class RecordingNotifier(SystemdNotifier):
    def __init__(self, watchdog_interval: float | None = None) -> None:
        super().__init__("@recording", None)
        self.sent: list[str] = []
        self._interval = watchdog_interval

    @property
    def watchdog_interval_seconds(self) -> float | None:
        return self._interval

    def notify(self, *fields: str) -> bool:
        self.sent.append("\n".join(fields))
        return True


def test_edge_configuration_requires_node_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEOTREX_EDGE_NODE_ID", raising=False)
    with pytest.raises(ValidationError):
        EdgeSettings(_env_file=None)


def test_missing_configuration_exits_ex_config_naming_fields_not_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    monkeypatch.chdir(str(tmp_path))  # no .env in the working directory
    monkeypatch.delenv("VEOTREX_EDGE_NODE_ID", raising=False)
    monkeypatch.setenv("VEOTREX_EDGE_HEARTBEAT_INTERVAL_SECONDS", "999999")
    with pytest.raises(SystemExit) as raised:
        load_settings()
    assert raised.value.code == EXIT_CONFIG == 78
    err = capsys.readouterr().err
    record = json.loads(err)
    assert record["event"] == "edge_config_invalid"
    assert record["fields"] == ["heartbeat_interval_seconds", "node_id"]
    assert "999999" not in err


async def test_agent_lifecycle_stops_gracefully() -> None:
    notifier = RecordingNotifier()
    agent = EdgeAgent(edge_settings(), notifier=notifier)
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0)
    agent.request_shutdown()
    await asyncio.wait_for(task, timeout=1)
    assert agent.stopping
    assert notifier.sent[0].startswith("READY=1\nSTATUS=ready commit=0123456789ab version=test")
    assert notifier.sent[-1].startswith("STOPPING=1\nSTATUS=")
    assert "worker=absent" in agent.status_line()
    assert not agent.degraded


async def test_gpu_worker_failure_does_not_crash_agent_but_reports_degraded() -> None:
    class FailingWorker:
        stopped = False

        def start(self) -> None:
            raise WorkerFailure("worker_timeout")

        def stop(self) -> None:
            self.stopped = True

    worker = FailingWorker()
    notifier = RecordingNotifier()
    agent = EdgeAgent(edge_settings(), worker, notifier)  # type: ignore[arg-type]
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.01)
    assert not task.done()
    # Readiness is still signalled: the process is live and configured; only the worker is down.
    assert notifier.sent[0].startswith("READY=1\nSTATUS=degraded ")
    assert "worker=FAILED" in notifier.sent[0]
    assert "last_error=gpu_worker_start_failed:worker_timeout" in notifier.sent[0]
    assert agent.degraded
    agent.request_shutdown()
    await asyncio.wait_for(task, timeout=1)
    assert worker.stopped


async def test_worker_health_failure_recovers_and_updates_status() -> None:
    class FlakyWorker:
        def __init__(self) -> None:
            self.health_calls = 0
            self.recovered = 0
            self.stopped = False

        def start(self) -> None:
            pass

        def health(self) -> dict[str, object]:
            self.health_calls += 1
            if self.health_calls == 1:
                raise WorkerFailure("worker_exited")
            return {}

        def recover(self) -> dict[str, object]:
            self.recovered += 1
            return {}

        def stop(self) -> None:
            self.stopped = True

    # model_copy skips validation: a zero heartbeat drives the monitor loop immediately.
    settings = edge_settings().model_copy(update={"heartbeat_interval_seconds": 0})
    worker = FlakyWorker()
    notifier = RecordingNotifier()
    agent = EdgeAgent(settings, worker, notifier)  # type: ignore[arg-type]
    task = asyncio.create_task(agent.run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if worker.recovered:
            break
    assert "worker=READY" in notifier.sent[0]
    assert worker.recovered == 1
    assert any("STATUS=degraded" in item and "worker=DEGRADED" in item for item in notifier.sent)
    recovered = [item for item in notifier.sent if "worker_restarts=1" in item]
    assert recovered and "worker=READY" in recovered[-1]
    assert "last_error=gpu_worker_health_failed:worker_exited" in agent.status_line()
    agent.request_shutdown()
    await asyncio.wait_for(task, timeout=1)
    assert worker.stopped


async def test_watchdog_is_pinged_from_the_event_loop() -> None:
    notifier = RecordingNotifier(watchdog_interval=0.01)
    agent = EdgeAgent(edge_settings(), notifier=notifier)
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.08)
    agent.request_shutdown()
    await asyncio.wait_for(task, timeout=1)
    assert notifier.sent.count("WATCHDOG=1") >= 3


def test_status_line_carries_no_secret_shaped_content() -> None:
    agent = EdgeAgent(edge_settings(), notifier=RecordingNotifier())
    line = agent.status_line()
    assert line.startswith("ready commit=0123456789ab version=test worker=absent")
    assert "node_id" not in line and "00000000-0000" not in line
