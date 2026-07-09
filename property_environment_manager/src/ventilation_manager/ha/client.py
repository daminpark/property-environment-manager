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
    """Raised when Home Assistant does not report the requested device state."""


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

    async def turn_on(self, entity_id: str) -> None:
        await self._call_service("switch", "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> None:
        await self._call_service("switch", "turn_off", {"entity_id": entity_id})

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
            deadline = asyncio.get_running_loop().time() + verify_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(poll_interval_seconds)
                current = await self.get_state(entity_id)
                if current.state == desired:
                    return
                if current.state in {"unavailable", "unknown"}:
                    break
        raise CommandVerificationError(
            f"{entity_id} did not report {desired} after {attempts} attempts"
        )

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        client = await self._get_client()
        response = await client.post(
            f"{self.url}/api/services/{domain}/{service}",
            json=data,
        )
        response.raise_for_status()
