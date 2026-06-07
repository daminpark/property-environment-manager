"""Ventilation Manager entry point."""

from __future__ import annotations

import asyncio
import logging
import signal

from ventilation_manager.config import load_settings
from ventilation_manager.controller import VentilationController
from ventilation_manager.ha.client import HomeAssistantClient
from ventilation_manager.web import DashboardServer


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    configure_logging()
    logger = logging.getLogger(__name__)
    settings = load_settings()
    controller = VentilationController(settings)
    stop = asyncio.Event()

    def request_stop() -> None:
        logger.info("Stop requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    if not settings.ha_token:
        raise RuntimeError("VENT_HA_TOKEN is required")

    logger.info(
        "Starting for house %s; zones=%s; active_control=%s",
        settings.house_code,
        ",".join(zone.zone_id for zone in settings.zones),
        settings.active_control,
    )

    dashboard = DashboardServer(host="0.0.0.0", port=settings.web_port, payload_provider=controller, title="Ventilation Manager")
    await dashboard.start()

    async with HomeAssistantClient(settings.ha_url, settings.ha_token) as ha:
        while not stop.is_set():
            try:
                decisions = await controller.run_once(ha)
                for decision in decisions:
                    logger.info(
                        "%s mode=%s should_run=%s command=%s reason=%s",
                        decision.zone_id,
                        decision.mode,
                        decision.should_run,
                        decision.command,
                        decision.reason,
                    )
            except Exception:
                logger.exception("Ventilation loop failed")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.poll_interval_seconds
                )
            except TimeoutError:
                pass

    await dashboard.stop()


if __name__ == "__main__":
    asyncio.run(main())
