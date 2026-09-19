"""Entry point local da fase 2 para observar eventos normalizados."""

from __future__ import annotations

import argparse
import asyncio
import signal

from src.ingestion.tiktok import TikTokLiveConnector
from src.logging import get_logger, setup_logging

LOGGER = get_logger(__name__)


async def run(unique_id: str) -> None:
    connector = TikTokLiveConnector(unique_id)
    loop = asyncio.get_running_loop()
    stop_task: asyncio.Task[None] | None = None

    def request_stop() -> None:
        nonlocal stop_task
        if stop_task is None or stop_task.done():
            stop_task = asyncio.create_task(connector.stop())

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop)
        except NotImplementedError:
            # Alguns ambientes Windows não suportam signal handlers no loop.
            pass

    connector.start()
    LOGGER.info("TikTok connector iniciado para @%s", connector.unique_id)
    try:
        async for event in connector.events():
            LOGGER.info(
                "evento normalizado: type=%s source_event_id=%s user=%s",
                event.event_type.value,
                event.source_event_id or "-",
                event.user.display_name,
            )
    finally:
        await connector.stop()
        LOGGER.info("TikTok connector encerrado: state=%s", connector.state.value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Observa eventos de uma TikTok LIVE")
    parser.add_argument("unique_id", help="username do creator, com ou sem @")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    setup_logging(args.log_level)
    asyncio.run(run(args.unique_id))


if __name__ == "__main__":
    main()
