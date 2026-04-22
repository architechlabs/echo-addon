"""Coordinator for Echoweave proxy data."""

from __future__ import annotations

import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EchoweaveProxyApiClient
from .const import CONF_ADDON_URL, CONF_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class EchoweaveProxyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        addon_url = entry.data[CONF_ADDON_URL]
        self.api = EchoweaveProxyApiClient(hass, addon_url)
        update_interval = datetime.timedelta(
            seconds=entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, 5),
            )
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            player = await self.api.get_player()
        except Exception as exc:
            raise UpdateFailed(f"Cannot reach Echo Bridge addon: {exc}") from exc

        if not isinstance(player, dict):
            raise UpdateFailed("Invalid response from addon: missing player payload")

        return {"player": player}

    def player_payload(self) -> dict[str, Any]:
        if not self.data:
            return {}
        player = self.data.get("player") or {}
        return player if isinstance(player, dict) else {}
