"""Small Home Assistant REST API client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class EntityState:
    """Subset of a Home Assistant entity state."""

    entity_id: str
    state: str
    attributes: dict[str, Any]
    last_updated: str | None
    last_changed: str | None


class CommandVerificationError(RuntimeError):
    """Raised when Home Assistant does not report a requested device state."""


class HomeAssistantClient:
    """Async REST client for Home Assistant."""

    def __init__(self, url: str, token: str, timeout: float = 20.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HomeAssistantClient":
        await self._get_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_states(self) -> dict[str, EntityState]:
        client = await self._get_client()
        response = await client.get(f"{self.url}/api/states")
        response.raise_for_status()
        states: dict[str, EntityState] = {}
        for item in response.json():
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            states[entity_id] = EntityState(
                entity_id=entity_id,
                state=str(item.get("state", "unknown")),
                attributes=item.get("attributes", {}),
                last_updated=item.get("last_updated"),
                last_changed=item.get("last_changed"),
            )
        return states

    async def get_state(self, entity_id: str) -> EntityState:
        client = await self._get_client()
        response = await client.get(f"{self.url}/api/states/{entity_id}")
        response.raise_for_status()
        item = response.json()
        return EntityState(
            entity_id=str(item.get("entity_id", entity_id)),
            state=str(item.get("state", "unknown")),
            attributes=item.get("attributes", {}),
            last_updated=item.get("last_updated"),
            last_changed=item.get("last_changed"),
        )

    async def set_state(
        self,
        entity_id: str,
        state: str | float | int | bool,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        client = await self._get_client()
        response = await client.post(
            f"{self.url}/api/states/{entity_id}",
            json={"state": state, "attributes": attributes or {}},
        )
        response.raise_for_status()

    async def set_climate_temperature(
        self, entity_id: str, temperature: float, hvac_mode: str = "heat"
    ) -> None:
        await self.call_service(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": temperature, "hvac_mode": hvac_mode},
        )

    async def set_climate_hvac_mode(self, entity_id: str, hvac_mode: str) -> None:
        await self.call_service(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode},
        )

    async def turn_on(self, entity_id: str) -> None:
        domain = entity_id.split(".", 1)[0]
        await self.call_service(domain, "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> None:
        domain = entity_id.split(".", 1)[0]
        await self.call_service(domain, "turn_off", {"entity_id": entity_id})

    async def set_switch_state_verified(
        self,
        entity_id: str,
        *,
        on: bool,
        attempts: int = 2,
        verify_timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        desired = "on" if on else "off"
        for _attempt in range(attempts):
            if on:
                await self.turn_on(entity_id)
            else:
                await self.turn_off(entity_id)
            if await self._wait_for(
                entity_id,
                lambda state: state.state == desired,
                verify_timeout_seconds,
                poll_interval_seconds,
            ):
                return
        raise CommandVerificationError(
            f"{entity_id} did not report {desired} after {attempts} attempts"
        )

    async def set_climate_temperature_verified(
        self,
        entity_id: str,
        temperature: float,
        *,
        attempts: int = 2,
        verify_timeout_seconds: float = 12.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        for _attempt in range(attempts):
            await self.set_climate_temperature(entity_id, temperature)
            if await self._wait_for(
                entity_id,
                lambda state: self._temperature_matches(state, temperature),
                verify_timeout_seconds,
                poll_interval_seconds,
            ):
                return
        raise CommandVerificationError(
            f"{entity_id} did not report target {temperature:.1f}C after {attempts} attempts"
        )

    async def set_climate_hvac_mode_verified(
        self,
        entity_id: str,
        hvac_mode: str,
        *,
        attempts: int = 2,
        verify_timeout_seconds: float = 12.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        for _attempt in range(attempts):
            await self.set_climate_hvac_mode(entity_id, hvac_mode)
            if await self._wait_for(
                entity_id,
                lambda state: state.state == hvac_mode,
                verify_timeout_seconds,
                poll_interval_seconds,
            ):
                return
        raise CommandVerificationError(
            f"{entity_id} did not report HVAC mode {hvac_mode} after {attempts} attempts"
        )

    async def _wait_for(
        self,
        entity_id: str,
        predicate: Any,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_interval_seconds)
            current = await self.get_state(entity_id)
            if predicate(current):
                return True
            if current.state in {"unavailable", "unknown"}:
                return False
        return False

    def _temperature_matches(self, state: EntityState, expected: float) -> bool:
        value = state.attributes.get("temperature")
        try:
            return value is not None and abs(float(value) - expected) <= 0.05
        except (TypeError, ValueError):
            return False

    async def get_calendar_events(
        self,
        entity_ids: list[str],
        *,
        start_date_time: str,
        end_date_time: str,
    ) -> dict[str, list[dict[str, Any]]]:
        result = await self.call_service(
            "calendar",
            "get_events",
            {
                "entity_id": entity_ids,
                "start_date_time": start_date_time,
                "end_date_time": end_date_time,
            },
            return_response=True,
        )
        return self._normalize_calendar_events(result)

    def _normalize_calendar_events(self, result: Any) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(result, dict):
            return {}
        if isinstance(result.get("service_response"), dict):
            result = result["service_response"]
        events_by_entity: dict[str, list[dict[str, Any]]] = {}
        for entity_id, payload in result.items():
            if isinstance(payload, dict):
                events = payload.get("events", [])
            elif isinstance(payload, list):
                events = payload
            else:
                events = []
            events_by_entity[str(entity_id)] = [
                item for item in events if isinstance(item, dict)
            ]
        return events_by_entity

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        return_response: bool = False,
    ) -> Any:
        client = await self._get_client()
        suffix = "?return_response" if return_response else ""
        response = await client.post(
            f"{self.url}/api/services/{domain}/{service}{suffix}",
            json=data,
        )
        response.raise_for_status()
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None
