"""HTTP client for the local Echo Bridge addon proxy API."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_TIMEOUT = ClientTimeout(total=15)


class EchoweaveProxyApiClient:
    """Communicate directly with the Echo Bridge addon on the same HA instance."""

    def __init__(self, hass: HomeAssistant, addon_url: str) -> None:
        self._hass = hass
        self._addon_url = addon_url.rstrip("/")

    async def get_players(self) -> dict[str, Any]:
        """Fetch all proxy players from the addon."""
        session = async_get_clientsession(self._hass)
        url = f"{self._addon_url}/proxy/players"
        try:
            async with session.get(url, timeout=_TIMEOUT) as response:
                response.raise_for_status()
                return await response.json()
        except ClientError as exc:
            raise RuntimeError(f"Failed to fetch proxy players: {exc}") from exc

    async def send_command(
        self,
        command: str,
        addon_player_id: str,
        *,
        volume: int | None = None,
        query: str | None = None,
        media_id: str | None = None,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        """Send a playback command to a proxy player."""
        session = async_get_clientsession(self._hass)
        url = f"{self._addon_url}/proxy/command"
        payload: dict[str, Any] = {
            "command": command,
            "addon_player_id": addon_player_id,
        }
        if volume is not None:
            payload["volume"] = volume
        if query is not None:
            payload["query"] = query
        if media_id is not None:
            payload["media_id"] = media_id
        if media_type is not None:
            payload["media_type"] = media_type
        try:
            async with session.post(url, json=payload, timeout=_TIMEOUT) as response:
                response.raise_for_status()
                return await response.json()
        except ClientError as exc:
            raise RuntimeError(f"Failed to send proxy command: {exc}") from exc

    async def health_check(self) -> dict[str, Any]:
        """Check if the addon is healthy and reachable."""
        session = async_get_clientsession(self._hass)
        url = f"{self._addon_url}/proxy/health"
        try:
            async with session.get(url, timeout=_TIMEOUT) as response:
                response.raise_for_status()
                return await response.json()
        except ClientError as exc:
            raise RuntimeError(f"Addon health check failed: {exc}") from exc
