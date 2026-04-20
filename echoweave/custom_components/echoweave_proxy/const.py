"""Constants for the Echoweave proxy integration."""

from __future__ import annotations

DOMAIN = "echoweave_proxy"
PLATFORMS = ["media_player"]

CONF_ADDON_URL = "addon_url"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_NAME = "Echoweave Proxy"
DEFAULT_ADDON_URL = "http://local-echo-bridge:8000"
DEFAULT_SCAN_INTERVAL = 5

ATTR_ADDON_PLAYER_ID = "addon_player_id"
ATTR_MA_PLAYER_ID = "ma_player_id"
ATTR_ACTIVE_QUEUE_ID = "active_queue_id"
ATTR_QUEUE_STATE = "queue_state"
ATTR_SOURCE = "source"

ADDON_URL_MARKER = ".echoweave_addon_url"
