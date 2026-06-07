"""Small Home Assistant REST API client."""

from __future__ import annotations

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
