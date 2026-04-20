"""Echoweave proxy integration.

Auto-configures itself when the Echo Bridge addon writes a marker file
at /config/.echoweave_addon_url with the addon's internal URL.
"""

from __future__ import annotations

import logging
import os

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant

from .const import ADDON_URL_MARKER, CONF_ADDON_URL, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, PLATFORMS
from .coordinator import EchoweaveProxyCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Bootstrap: auto-create config entry if the addon marker file exists."""
    hass.data.setdefault(DOMAIN, {})

    if hass.config_entries.async_entries(DOMAIN):
        return True

    marker = hass.config.path(ADDON_URL_MARKER)
    if not os.path.isfile(marker):
        return True

    try:
        with open(marker) as fh:
            addon_url = fh.read().strip()
    except OSError:
        return True

    if not addon_url:
        return True

    _LOGGER.info(
        "Echo Bridge addon detected at %s — auto-configuring integration", addon_url
    )
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={CONF_ADDON_URL: addon_url, CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = EchoweaveProxyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
