"""Application settings loaded from environment variables or a .env file.

Copy .env.example to .env and fill in your values.
All variables are prefixed with ECHO_ to avoid conflicts.

Per-user configuration (MA URL, MA token, player ID) is NOT stored here.
It is collected once per user during Alexa account linking and persisted
in SQLite, keyed by an opaque access token.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Public URL ─────────────────────────────────────────────────────────
    public_url: str = "https://echo-api.gethomeassistant.com"

    # ── Alexa request validation ───────────────────────────────────────────
    alexa_validation: Literal["enforce", "log", "off"] = "enforce"
    alexa_skill_id: str = ""

    # ── OAuth2 (Alexa Account Linking) ─────────────────────────────────────
    oauth_client_id: str = "echo-bridge"
    oauth_client_secret: str = ""

    # ── Persistence ────────────────────────────────────────────────────────
    db_path: str = "/data/echo.db"

    # ── Local Music Assistant connection (auto-discovered or manual) ───────
    local_ma_url: str = ""
    local_ma_token: str = ""

    # ── Backend WebSocket bridge ───────────────────────────────────────────
    backend_ws_url: str = ""
    backend_ws_token: str = ""
    backend_instance_id: str = "echoweave-local"

    # ── Proxy player settings ─────────────────────────────────────────────
    proxy_player_prefix: str = "addon"
    proxy_status_push: bool = True
    proxy_status_poll_interval: int = 3
    proxy_player_filter: str = ""
    # When set to a MA player_id, volume commands for players without volume support
    # are forwarded to this player (e.g. "Nitish's Echo Dot" when the primary player
    # is a UPnP Echo Dot that doesn't support volume via UPnP).
    proxy_volume_player: str = ""

    # ── Worker registration ──────────────────────────────────────────────────
    worker_url: str = "https://echo-api.gethomeassistant.com"
    worker_secret: str = ""

    # ── Runtime ────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    port: int = 8000

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_prefix": "ECHO_",
    }

    @property
    def local_proxy_enabled(self) -> bool:
        return bool(self.local_ma_url.strip())

    @property
    def proxy_player_filter_values(self) -> set[str]:
        if not self.proxy_player_filter.strip():
            return set()
        return {
            value.strip()
            for value in self.proxy_player_filter.split(",")
            if value.strip()
        }

    @property
    def backend_ws_enabled(self) -> bool:
        return bool(
            self.proxy_status_push
            and self.backend_ws_url.strip()
            and self.local_proxy_enabled
        )
