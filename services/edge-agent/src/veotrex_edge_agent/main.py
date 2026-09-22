import asyncio
import json
import logging
import signal
import sys

import structlog
from pydantic import ValidationError

from veotrex_edge_agent.agent import EdgeAgent
from veotrex_edge_agent.config import EdgeSettings
from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor

# sysexits.h EX_CONFIG. The systemd unit lists it in RestartPreventExitStatus=: invalid or
# missing configuration is a fatal operator error that a restart cannot fix, so the service
# fails once, loudly, instead of consuming its start-limit budget in a loop.
EXIT_CONFIG = 78


def configure_logging(settings: EdgeSettings) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=settings.log_level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


async def run_agent(settings: EdgeSettings) -> None:
    configure_logging(settings)
    agent = EdgeAgent(settings, GpuWorkerSupervisor())
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, agent.request_shutdown)
    await agent.run()


def load_settings() -> EdgeSettings:
    """Validate configuration or exit with EX_CONFIG, naming the fields and never the values."""
    try:
        return EdgeSettings()
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        print(
            json.dumps(
                {
                    "event": "edge_config_invalid",
                    "level": "error",
                    "fields": fields,
                    "exit_code": EXIT_CONFIG,
                    "hint": "set VEOTREX_EDGE_<FIELD> in the service environment file",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(EXIT_CONFIG) from None


def main() -> None:
    asyncio.run(run_agent(load_settings()))


if __name__ == "__main__":
    main()
