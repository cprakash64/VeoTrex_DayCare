import asyncio
import logging
import signal
import sys

import structlog

from veotrex_edge_agent.agent import EdgeAgent
from veotrex_edge_agent.config import EdgeSettings


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
    agent = EdgeAgent(settings)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, agent.request_shutdown)
    await agent.run()


def main() -> None:
    asyncio.run(run_agent(EdgeSettings()))


if __name__ == "__main__":
    main()
