"""Config flow for Echoweave proxy."""

from __future__ import annotations

import logging
import os

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    ADDON_URL_MARKER,
    CONF_ADDON_URL,
    CONF_SCAN_INTERVAL,
    DEFAULT_ADDON_URL,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _read_addon_marker(hass) -> str:
    """Read the addon URL from the marker file written by the addon."""
    marker = hass.config.path(ADDON_URL_MARKER)
    if not os.path.isfile(marker):
        return ""
    try:
        with open(marker) as f:
            return f.read().strip()
    except OSError:
        return ""


class EchoweaveProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle manual configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            addon_url = user_input[CONF_ADDON_URL].rstrip("/")
            user_input[CONF_ADDON_URL] = addon_url
            from .api import EchoweaveProxyApiClient

            api = EchoweaveProxyApiClient(self.hass, addon_url)
            try:
                await api.health_check()
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(addon_url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=DEFAULT_NAME, data=user_input)

        suggested_url = _read_addon_marker(self.hass) or DEFAULT_ADDON_URL

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDON_URL, default=suggested_url): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=DEFAULT_SCAN_INTERVAL,
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_hassio(self, discovery_info: dict | None = None) -> FlowResult:
        """Handle auto-discovery from the addon via Supervisor or marker file."""
        discovery_info = discovery_info or {}
        addon_url = discovery_info.get("addon_url", "")

        if not addon_url:
            addon_url = _read_addon_marker(self.hass) or DEFAULT_ADDON_URL

        from .api import EchoweaveProxyApiClient

        api = EchoweaveProxyApiClient(self.hass, addon_url)
        try:
            await api.health_check()
        except Exception:
            _LOGGER.warning(
                "Echo Bridge addon discovered but not reachable at %s", addon_url
            )
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(addon_url)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=DEFAULT_NAME,
            data={
                CONF_ADDON_URL: addon_url,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            },
        )

    async def async_step_import(self, import_data: dict | None = None) -> FlowResult:
        """Handle auto-import triggered by addon marker file on startup."""
        import_data = import_data or {}
        addon_url = import_data.get(CONF_ADDON_URL, "")
        if not addon_url:
            return self.async_abort(reason="no_addon_url")

        await self.async_set_unique_id(addon_url)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=DEFAULT_NAME,
            data={
                CONF_ADDON_URL: addon_url,
                CONF_SCAN_INTERVAL: import_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return EchoweaveProxyOptionsFlow(config_entry)


class EchoweaveProxyOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        merged = {**self._config_entry.data, **self._config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ADDON_URL,
                    default=merged.get(CONF_ADDON_URL, DEFAULT_ADDON_URL),
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=merged.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors={})
