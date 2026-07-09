"""Combined Property Environment Manager entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from typing import Any

from property_environment_manager.bootstrap import bootstrap_legacy_data
from property_environment_manager.web import DashboardServer
from trv_regulator.config import load_settings as load_trv_settings
from trv_regulator.controller import TRVRegulator
from trv_regulator.ha.client import HomeAssistantClient as TRVHomeAssistantClient
from ventilation_manager.config import load_settings as load_ventilation_settings
from ventilation_manager.controller import VentilationController
from ventilation_manager.ha.client import (
    HomeAssistantClient as VentilationHomeAssistantClient,
)

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


class CombinedPayloadProvider:
    """Dashboard payload adapter for the independently tested controllers."""

    def __init__(
        self,
        *,
        ventilation: VentilationController | None,
        trv: TRVRegulator | None,
    ) -> None:
        self.ventilation = ventilation
        self.trv = trv

    def dashboard_payload(self) -> dict[str, Any]:
        controllers: dict[str, Any] = {}
        if self.ventilation is not None:
            controllers["ventilation"] = self.ventilation.dashboard_payload()
        if self.trv is not None:
            controllers["trv"] = self.trv.dashboard_payload()
        return {
            "app": "property_environment_manager",
            "controllers": controllers,
        }


async def _run_loop(
    *,
    name: str,
    stop: asyncio.Event,
    settings: Any,
    controller: Any,
    client_factory: Callable[[str, str], Any],
    log_decision: Callable[[Any], str],
) -> None:
    logger = logging.getLogger(f"{__name__}.{name}")
    if not settings.ha_token:
        raise RuntimeError(f"{name} Home Assistant token is required")

    logger.info(
        "Starting %s for house %s; zones=%s; active_control=%s",
        name,
        settings.house_code,
        ",".join(zone.zone_id for zone in settings.zones),
        settings.active_control,
    )

    async with client_factory(settings.ha_url, settings.ha_token) as ha:
        while not stop.is_set():
            try:
                decisions = await controller.run_once(ha)
                for decision in decisions:
                    logger.info(log_decision(decision))
            except Exception:
                logger.exception("%s loop failed", name)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.poll_interval_seconds
                )
            except TimeoutError:
                pass


async def main() -> None:
    configure_logging()
    stop = asyncio.Event()

    def request_stop() -> None:
        LOGGER.info("Stop requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    ventilation_enabled = _env_bool("PEM_VENTILATION_ENABLED", True)
    trv_enabled = _env_bool("PEM_TRV_ENABLED", True)

    for controller_name, result in bootstrap_legacy_data().items():
        LOGGER.info("Legacy %s data: %s", controller_name, result)

    ventilation = (
        VentilationController(load_ventilation_settings())
        if ventilation_enabled
        else None
    )
    trv = TRVRegulator(load_trv_settings()) if trv_enabled else None

    dashboard = DashboardServer(
        host="0.0.0.0",
        port=_env_int("PEM_WEB_PORT", 8097),
        payload_provider=CombinedPayloadProvider(ventilation=ventilation, trv=trv),
    )
    await dashboard.start()

    tasks: list[asyncio.Task[None]] = []
    if ventilation is not None:
        tasks.append(
            asyncio.create_task(
                _run_loop(
                    name="ventilation",
                    stop=stop,
                    settings=ventilation.settings,
                    controller=ventilation,
                    client_factory=VentilationHomeAssistantClient,
                    log_decision=lambda decision: (
                        f"{decision.zone_id} mode={decision.mode} "
                        f"should_run={decision.should_run} "
                        f"command={decision.command} reason={decision.reason}"
                    ),
                )
            )
        )
    if trv is not None:
        tasks.append(
            asyncio.create_task(
                _run_loop(
                    name="trv",
                    stop=stop,
                    settings=trv.settings,
                    controller=trv,
                    client_factory=TRVHomeAssistantClient,
                    log_decision=lambda decision: (
                        f"{decision.zone_id} mode={decision.mode} "
                        f"action={decision.suggested_action} "
                        f"target={decision.suggested_target_temperature_c} "
                        f"reason={decision.reason}"
                    ),
                )
            )
        )

    try:
        if not tasks:
            LOGGER.warning("No controllers enabled")
            await stop.wait()
        else:
            await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await dashboard.stop()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
