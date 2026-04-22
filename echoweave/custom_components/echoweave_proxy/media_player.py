"""Media player platform for Echoweave proxy players."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.media_player import MediaPlayerEntity, MediaType
from homeassistant.components.media_player.const import MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACTIVE_QUEUE_ID,
    ATTR_ADDON_PLAYER_ID,
    ATTR_MA_PLAYER_ID,
    ATTR_QUEUE_STATE,
    ATTR_SOURCE,
    DOMAIN,
)
from .coordinator import EchoweaveProxyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EchoweaveProxyCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _build_entities() -> list[EchoweaveProxyPlayerEntity]:
        new_entities: list[EchoweaveProxyPlayerEntity] = []
        for player in coordinator.player_payloads():
            addon_player_id = str(player.get("addon_player_id") or "")
            if not addon_player_id or addon_player_id in known:
                continue
            known.add(addon_player_id)
            new_entities.append(EchoweaveProxyPlayerEntity(coordinator, entry, addon_player_id))
        return new_entities

    entities = _build_entities()
    if entities:
        async_add_entities(entities)

    def _handle_coordinator_update() -> None:
        entities = _build_entities()
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


_STATE_MAP = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "idle": MediaPlayerState.IDLE,
    "off": MediaPlayerState.OFF,
    "standby": MediaPlayerState.STANDBY,
    "buffering": MediaPlayerState.BUFFERING,
}


class EchoweaveProxyPlayerEntity(CoordinatorEntity[EchoweaveProxyCoordinator], MediaPlayerEntity):
    _attr_has_entity_name = True
    _BASE_FEATURES = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.PLAY_MEDIA
    )
    _VOLUME_FEATURES = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        # Proxy entities always expose volume controls for a stable HA UX.
        # The proxy service handles unsupported commands gracefully per player.
        return self._BASE_FEATURES | self._VOLUME_FEATURES

    def __init__(
        self,
        coordinator: EchoweaveProxyCoordinator,
        entry: ConfigEntry,
        addon_player_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._addon_player_id = addon_player_id
        self._attr_unique_id = f"echoweave_{addon_player_id.replace(':', '_')}"
        # Optimistic state overrides — cleared on next coordinator refresh
        self._optimistic_volume: float | None = None
        self._optimistic_state: str | None = None
        self._optimistic_state_expires: float | None = None
        # Sticky payload cache so the entity remains controllable even if MA temporarily
        # drops this player from discovery (common with some Echo/UPnP integrations).
        self._last_player_payload: dict[str, Any] = {
            "addon_player_id": addon_player_id,
            "name": addon_player_id,
            "available": True,
            "state": "idle",
            "has_volume_support": True,
        }

    def _live_player(self) -> dict[str, Any]:
        for player in self.coordinator.player_payloads():
            if str(player.get("addon_player_id") or "") == self._addon_player_id:
                return player
        return {}

    @property
    def _player(self) -> dict[str, Any]:
        live = self._live_player()
        if live:
            self._last_player_payload = dict(live)
            return live
        return self._last_player_payload

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coordinator refreshed — selectively clear optimistic overrides then re-render.

        Only wipe _optimistic_volume once MA confirms a real (non-None) volume.
        For players that permanently report volume_level=None (e.g. UPnP Echo Dots),
        the slider stays at the last user-set value rather than snapping back to 0.
        """
        real_vol = self._player.get("volume_level")
        if real_vol is not None and self._optimistic_volume is not None:
            self._optimistic_volume = None
        if self._optimistic_state is not None:
            now = time.monotonic()
            real_state = str(self._player.get("state") or "").lower()
            expiry = self._optimistic_state_expires or 0.0
            if real_state in {"playing", "paused", "idle", "off", "standby", "buffering"} and real_state == self._optimistic_state:
                self._optimistic_state = None
                self._optimistic_state_expires = None
            elif expiry and now >= expiry:
                self._optimistic_state = None
                self._optimistic_state_expires = None
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        # Mark unavailable only when coordinator polling fails (addon unreachable).
        # Do not mirror volatile per-player availability from MA for this proxy entity.
        return bool(self.coordinator.last_update_success)

    @property
    def name(self) -> str | None:
        player = self._player
        return str(player.get("name") or self._addon_player_id)

    @property
    def state(self) -> MediaPlayerState | None:
        if self._optimistic_state:
            return _STATE_MAP.get(self._optimistic_state, MediaPlayerState.IDLE)
        state = str(self._player.get("state") or "").lower()
        if state in {"off", "standby", "unknown", "unavailable"} and self.available:
            # Keep controls visible for proxy entities even when MA reports transient off/standby.
            return MediaPlayerState.IDLE
        return _STATE_MAP.get(state, MediaPlayerState.IDLE if self.available else None)

    @property
    def volume_level(self) -> float | None:
        # Use optimistic value immediately after volume command (before coordinator refresh)
        if self._optimistic_volume is not None:
            return self._optimistic_volume
        # volume_level is pre-converted to 0.0-1.0 in the proxy snapshot
        vol = self._player.get("volume_level")
        if isinstance(vol, (int, float)):
            return max(0.0, min(1.0, float(vol)))
        return None

    @property
    def is_volume_muted(self) -> bool | None:
        muted = self._player.get("is_volume_muted")
        if isinstance(muted, bool):
            return muted
        return None

    @property
    def media_content_type(self) -> str | None:
        if self._player.get("current_item"):
            return MediaType.MUSIC
        return None

    @property
    def media_title(self) -> str | None:
        current_item = self._player.get("current_item") or {}
        current_media = self._player.get("current_media") or {}
        # MA 2.x: queue item wraps the real track in 'media_item'
        media_item = current_item.get("media_item") or {}
        return (
            current_item.get("name")
            or media_item.get("name")
            or current_media.get("title")
            or current_media.get("name")
        )

    @property
    def media_artist(self) -> str | None:
        current_item = self._player.get("current_item") or {}
        # MA queue items use 'artists' (list), not 'artist'
        artists = current_item.get("artists") or current_item.get("artist")
        if isinstance(artists, list) and artists:
            first = artists[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("name") or first.get("item_id") or ""
        if isinstance(artists, str):
            return artists
        if isinstance(artists, dict):
            return artists.get("name")
        # Fallback: check media_item inside queue item (MA 2.x structure)
        media_item = current_item.get("media_item") or {}
        mi_artists = media_item.get("artists") or []
        if isinstance(mi_artists, list) and mi_artists:
            first = mi_artists[0]
            if isinstance(first, dict):
                return first.get("name") or ""
        current_media = self._player.get("current_media") or {}
        return current_media.get("artist")

    @property
    def media_album_name(self) -> str | None:
        current_item = self._player.get("current_item") or {}
        media_item = current_item.get("media_item") or {}
        for src in (current_item, media_item):
            album = src.get("album")
            if isinstance(album, str) and album:
                return album
            if isinstance(album, dict):
                return album.get("name")
        return None

    @property
    def media_image_url(self) -> str | None:
        current_item = self._player.get("current_item") or {}
        current_media = self._player.get("current_media") or {}
        media_item = current_item.get("media_item") or {}
        for src in (current_item, media_item, current_media):
            image = src.get("image") or src.get("image_url") or src.get("thumb")
            # MA 2.x: image is a dict with 'path' key
            if isinstance(image, dict):
                image = image.get("path") or image.get("url") or ""
            if isinstance(image, str) and image.startswith("http"):
                return image
            metadata = src.get("metadata") or src.get("media_metadata")
            if isinstance(metadata, dict):
                img = metadata.get("image") or metadata.get("image_url")
                if isinstance(img, dict):
                    img = img.get("path") or img.get("url") or ""
                if isinstance(img, str) and img.startswith("http"):
                    return img
        return None

    @property
    def media_duration(self) -> float | None:
        current_item = self._player.get("current_item") or {}
        duration = current_item.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            return float(duration)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        player = self._player
        return {
            ATTR_ADDON_PLAYER_ID: player.get("addon_player_id"),
            ATTR_MA_PLAYER_ID: player.get("ma_player_id"),
            ATTR_ACTIVE_QUEUE_ID: player.get("active_queue_id"),
            ATTR_QUEUE_STATE: player.get("queue_state"),
            ATTR_SOURCE: player.get("source"),
        }

    @property
    def device_info(self) -> DeviceInfo:
        player = self._player
        return DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            manufacturer="ArchitechLabs",
            model="Echoweave Proxy Player",
            name=str(player.get("name") or self._addon_player_id),
        )

    # ── Playback commands ─────────────────────────────────────────────────

    async def _send(
        self,
        command: str,
        *,
        volume: int | None = None,
        muted: bool | None = None,
        media_id: str | None = None,
        media_type: str | None = None,
        query: str | None = None,
    ) -> None:
        await self.coordinator.api.send_command(
            command,
            self._addon_player_id,
            volume=volume,
            muted=muted,
            media_id=media_id,
            media_type=media_type,
            query=query,
        )
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        self._optimistic_state = "playing"
        self._optimistic_state_expires = time.monotonic() + 12.0
        self.async_write_ha_state()
        await self._send("play")

    async def async_media_pause(self) -> None:
        self._optimistic_state = "paused"
        self._optimistic_state_expires = time.monotonic() + 6.0
        self.async_write_ha_state()
        await self._send("pause")

    async def async_media_stop(self) -> None:
        self._optimistic_state = "idle"
        self._optimistic_state_expires = time.monotonic() + 6.0
        self.async_write_ha_state()
        await self._send("stop")

    async def async_media_next_track(self) -> None:
        await self._send("next")

    async def async_media_previous_track(self) -> None:
        await self._send("previous")

    async def async_set_volume_level(self, volume: float) -> None:
        clamped = max(0.0, min(1.0, volume))
        # Optimistically update slider so HA UI doesn't reset while command is in-flight
        self._optimistic_volume = clamped
        self.async_write_ha_state()
        await self._send("volume_set", volume=round(clamped * 100))

    async def async_mute_volume(self, mute: bool) -> None:
        await self._send("mute", muted=mute)

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media by URI or search query.

        Music Assistant and the HA UI both call this method.
        If media_id looks like a URI (contains ://), send as play_media.
        Otherwise, treat as a search query via play_query.
        """
        if "://" in media_id:
            await self._send("play_media", media_id=media_id, media_type=media_type)
        else:
            await self._send("play_query", query=media_id)
