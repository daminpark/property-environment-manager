"""TRV Regulator entry point."""

from __future__ import annotations

import asyncio
import logging
import signal

from trv_regulator.config import load_settings
from trv_regulator.controller import TRVRegulator
from trv_regulator.ha.client import HomeAssistantClient
from trv_regulator.web import DashboardServer


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
    regulator = TRVRegulator(settings)
    stop = asyncio.Event()

    def request_stop() -> None:
        logger.info("Stop requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    if not settings.ha_token:
        raise RuntimeError("TRV_HA_TOKEN is required")

    logger.info(
        "Starting for house %s; zones=%s; active_control=%s",
        settings.house_code,
        ",".join(zone.zone_id for zone in settings.zones),
        settings.active_control,
    )

    dashboard = DashboardServer(host="0.0.0.0", port=settings.web_port, payload_provider=regulator, title="TRV Regulator")
    await dashboard.start()

    try:
        async with HomeAssistantClient(settings.ha_url, settings.ha_token) as ha:
            while not stop.is_set():
                try:
                    decisions = await regulator.run_once(ha)
                    for decision in decisions:
                        logger.info(
                            "%s mode=%s action=%s target=%s reason=%s",
                            decision.zone_id,
                            decision.mode,
                            decision.suggested_action,
                            decision.suggested_target_temperature_c,
                            decision.reason,
                        )
                except Exception:
                    logger.exception("TRV regulator loop failed")
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
    finally:
        await dashboard.stop()


if __name__ == "__main__":
    asyncio.run(main())
