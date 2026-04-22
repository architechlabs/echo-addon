"""Local Music Assistant proxy service and optional backend websocket bridge."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.ma.client import (
    MusicAssistantAuthError,
    MusicAssistantClient,
    MusicAssistantError,
    MusicAssistantUnreachableError,
)
from app.proxy.models import ProxyCommandRequest, ProxyPlayerSnapshot, ProxySnapshot
from app.settings import Settings

logger = logging.getLogger(__name__)


def _websocket_connect_headers(headers: dict[str, str]) -> dict[str, Any]:
    if not headers:
        return {}
    try:
        parameters = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        return {"additional_headers": headers}
    if "additional_headers" in parameters:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


class LocalProxyService:
    """Expose local Music Assistant players as addon-scoped players."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.local_proxy_enabled

    @property
    def instance_id(self) -> str:
        return self._settings.backend_instance_id.strip() or "echoweave-local"

    @property
    def player_prefix(self) -> str:
        return self._settings.proxy_player_prefix.strip() or "addon"

    def addon_player_id(self, ma_player_id: str) -> str:
        prefix = self.player_prefix
        if not prefix:
            return ma_player_id
        return f"{prefix}:{ma_player_id}"

    def resolve_player_id(self, addon_player_id: str | None) -> str:
        if not addon_player_id:
            raise ValueError("addon_player_id is required")
        prefix = f"{self.player_prefix}:"
        if addon_player_id.startswith(prefix):
            return addon_player_id[len(prefix) :]
        return addon_player_id

    def _new_client(self) -> MusicAssistantClient:
        if not self.enabled:
            raise RuntimeError(
                "Local proxy is not configured. Set local_ma_url and local_ma_token in the addon options."
            )
        return MusicAssistantClient(
            self._settings.local_ma_url,
            self._settings.local_ma_token,
        )

    def _filter_players(self, players: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed = self._settings.proxy_player_filter_values
        if not allowed:
            return players
        filtered: list[dict[str, Any]] = []
        for player in players:
            player_id = str(player.get("player_id") or "")
            name = str(player.get("name") or "")
            if player_id in allowed or name in allowed:
                filtered.append(player)
        return filtered

    async def _build_player_snapshot(
        self,
        ma: MusicAssistantClient,
        player: dict[str, Any],
        all_players: list[dict[str, Any]] | None = None,
    ) -> ProxyPlayerSnapshot:
        player_id = str(player.get("player_id") or "")
        queue_id = str(player.get("active_queue") or player_id or "")

        queue_state: dict[str, Any] = {}
        queue_items: list[dict[str, Any]] = []
        if queue_id:
            queue_state, queue_items = await asyncio.gather(
                ma.get_queue_state(queue_id),
                ma.get_queue_items(queue_id),
            )

        current_item = queue_state.get("current_item")
        if not current_item and isinstance(queue_state.get("current_index"), int):
            current_index = int(queue_state["current_index"])
            if 0 <= current_index < len(queue_items):
                current_item = queue_items[current_index]

        # MA volume field — try all known field names across MA versions
        # MA 2.x: "volume_level" (int 0-100); some builds: "volume" or "current_volume"
        # Use explicit None check so volume=0 is not treated as missing.
        raw_vol = None
        for _vol_key in ("volume_level", "volume", "current_volume"):
            _v = player.get(_vol_key)
            if _v is not None:
                raw_vol = _v
                break
        # If MA returns it as a float 0.0-1.0, keep as-is; if int/float > 1.0 treat as 0-100
        if isinstance(raw_vol, (int, float)):
            volume_level = float(raw_vol) / 100.0 if raw_vol > 1.0 else float(raw_vol)
        else:
            volume_level = None
        is_volume_muted = player.get("volume_muted") or player.get("muted")

        # Determine if this player actually supports volume_set
        supported_features = player.get("supported_features") or []
        has_volume_support = "volume_set" in supported_features

        # For players where volume is advertised but returns None (e.g. UPnP Echo Dots —
        # Amazon blocks volume at hardware level even though MA's UPnP integration lists
        # volume_set in supported_features), also try companion routing.
        # Condition: volume_set in features but volume_level is still None after extraction.
        _needs_companion = (not has_volume_support) or (has_volume_support and volume_level is None)

        _companion_player: dict[str, Any] | None = None
        if _needs_companion and all_players is not None:
            companion_id = self._find_volume_companion(all_players, player_id)
            if companion_id:
                _companion_player = next(
                    (p for p in all_players if str(p.get("player_id") or "") == companion_id), None
                )

        if _companion_player is not None:
            logger.debug(
                "Borrowing volume/available/state from companion %s for %s",
                _companion_player.get("player_id"), player_id,
            )
            # Pull volume from companion
            raw_vol = None
            for _vol_key in ("volume_level", "volume", "current_volume"):
                _v = _companion_player.get(_vol_key)
                if _v is not None:
                    raw_vol = _v
                    break
            if isinstance(raw_vol, (int, float)):
                volume_level = float(raw_vol) / 100.0 if raw_vol > 1.0 else float(raw_vol)
            # Pull mute from companion
            _cmuted = _companion_player.get("volume_muted") or _companion_player.get("muted")
            if _cmuted is not None:
                is_volume_muted = bool(_cmuted)
            # Mark as having volume support so HA shows the slider
            has_volume_support = True

        # Availability + state: UPnP Echo Dots go available=False / state="off" when idle
        # (MA loses UPnP connection between tracks). Use companion values so the entity
        # stays visible and usable in HA at all times.
        _available = bool(player.get("available", False))
        _state = str(player.get("state") or player.get("playback_state") or "unknown")
        if _companion_player is not None:
            if not _available:
                _available = bool(_companion_player.get("available", False))
            # If UPnP player is off/unknown but companion is idle/playing, use companion state
            if _state in ("off", "unknown", ""):
                _comp_state = str(_companion_player.get("state") or _companion_player.get("playback_state") or "")
                if _comp_state and _comp_state not in ("off", "unknown"):
                    _state = _comp_state

        return ProxyPlayerSnapshot(
            addon_player_id=self.addon_player_id(player_id),
            ma_player_id=player_id,
            name=str(player.get("name") or player_id),
            available=_available,
            state=_state,
            powered=player.get("powered"),
            volume_level=volume_level,
            is_volume_muted=bool(is_volume_muted) if is_volume_muted is not None else None,
            has_volume_support=has_volume_support,
            active_queue_id=queue_id or None,
            queue_state=str(queue_state.get("state") or "") or None,
            current_index=queue_state.get("current_index"),
            source=str(player.get("active_source") or "") or None,
            current_item=current_item if isinstance(current_item, dict) else None,
            current_media=player.get("current_media")
            if isinstance(player.get("current_media"), dict)
            else None,
        )

    async def list_players(self) -> list[ProxyPlayerSnapshot]:
        ma = self._new_client()
        try:
            all_players = await ma.get_players()
            players = self._filter_players(all_players)
            snapshots = await asyncio.gather(
                *(self._build_player_snapshot(ma, player, all_players) for player in players)
            )
            return sorted(snapshots, key=lambda item: item.name.lower())
        except MusicAssistantAuthError:
            logger.error(
                "MA token rejected (401). Set 'local_ma_token' in addon config with "
                "a HA long-lived access token (HA Profile → Long-lived access tokens)."
            )
            return []
        finally:
            await ma.close()

    async def get_snapshot(self) -> ProxySnapshot:
        return ProxySnapshot(
            instance_id=self.instance_id,
            player_prefix=self.player_prefix,
            players=await self.list_players(),
        )

    async def get_player(self, addon_player_id: str) -> ProxyPlayerSnapshot:
        resolved = self.resolve_player_id(addon_player_id)
        players = await self.list_players()
        for player in players:
            if player.ma_player_id == resolved or player.addon_player_id == addon_player_id:
                return player
        raise ValueError(f"Unknown addon player: {addon_player_id}")

    async def _resolve_player_target(
        self,
        ma: MusicAssistantClient,
        addon_player_id: str | None,
    ) -> tuple[str, str]:
        ma_player_id = self.resolve_player_id(addon_player_id)
        all_players = await ma.get_players()
        players = self._filter_players(all_players)
        for pool in (players, all_players):
            for player in pool:
                player_id = str(player.get("player_id") or "")
                if player_id != ma_player_id:
                    continue
                queue_id = str(
                    player.get("active_queue")
                    or player.get("queue_id")
                    or player_id
                    or ""
                )
                return player_id, queue_id

        logger.warning(
            "Primary player %s missing in MA list; falling back to raw player id",
            ma_player_id,
        )
        return ma_player_id, ma_player_id

    @staticmethod
    def _player_mac(player: dict[str, Any]) -> str | None:
        """Extract the MAC address from a MA player dict, or None if unavailable."""
        device_info = player.get("device_info") or {}
        mac = device_info.get("mac_address") or (device_info.get("identifiers") or {}).get("mac_address")
        return str(mac).lower().replace("-", ":") if mac else None

    @staticmethod
    def _player_identifiers(player: dict[str, Any]) -> set[str]:
        """Return normalized device identifier values for companion matching."""
        device_info = player.get("device_info") or {}
        identifiers = device_info.get("identifiers") or {}
        values: set[str] = set()
        if isinstance(identifiers, dict):
            for raw in identifiers.values():
                if raw is None:
                    continue
                val = str(raw).strip().lower()
                if val:
                    values.add(val)
        mac = LocalProxyService._player_mac(player)
        if mac:
            values.add(mac)
        return values

    def _find_volume_companion(
        self,
        all_players: list[dict[str, Any]],
        target_player_id: str,
    ) -> str | None:
        """Find a companion MA player that shares the same physical device and supports volume_set.
        Detection order:
          1. proxy_volume_player config override (explicit player_id or name)
          2. Shared MAC address (device_info.mac_address)
          3. Name-based heuristic: an Alexa player whose name is a case-insensitive match
             or substring of the UPnP player name (e.g. both contain 'Echo Dot')
        """
        # 1. Config override takes priority
        override = self._settings.proxy_volume_player.strip()
        if override:
            logger.info("Volume override configured: routing volume to '%s'", override)
            return override

        target_is_uuid = "uuid" in target_player_id.lower()

        def _log_detect(message: str, *args: Any) -> None:
            if target_is_uuid:
                logger.info(message, *args)
            else:
                logger.debug(message, *args)

        target = next((p for p in all_players if str(p.get("player_id") or "") == target_player_id), None)
        target_is_upuuid = target_player_id.lower().startswith("upuuid")
        if not target:
            candidates = [
                p for p in all_players
                if (
                    "volume_set" in (p.get("supported_features") or [])
                    or "volume_mute" in (p.get("supported_features") or [])
                    or any(p.get(k) is not None for k in ("volume_level", "volume", "current_volume"))
                )
            ]
            if not candidates:
                candidates = list(all_players)
            if target_is_upuuid:
                echoish = [
                    p
                    for p in candidates
                    if (
                        "alexa" in str(p.get("provider") or "").lower()
                        or "echo" in str(p.get("name") or "").lower()
                        or "amazon" in str((p.get("device_info") or {}).get("manufacturer") or "").lower()
                    )
                ]
                if echoish:
                    candidates = echoish
            if len(candidates) == 1:
                pid = str(candidates[0].get("player_id") or "")
                _log_detect(
                    "Primary player %s not present; using single volume-capable candidate %s",
                    target_player_id,
                    pid,
                )
                return pid
            echo_candidates = [
                p for p in candidates if "echo" in str(p.get("name") or "").lower()
            ]
            if len(echo_candidates) == 1:
                pid = str(echo_candidates[0].get("player_id") or "")
                _log_detect(
                    "Primary player %s not present; using single Echo-like candidate %s",
                    target_player_id,
                    pid,
                )
                return pid
            return None

        # Candidates: other players that are likely volume-capable
        broad_candidates = [
            p for p in all_players if str(p.get("player_id") or "") != target_player_id
        ]
        candidates = [
            p for p in broad_candidates
            if (
                "volume_set" in (p.get("supported_features") or [])
                or "volume_mute" in (p.get("supported_features") or [])
                or any(p.get(k) is not None for k in ("volume_level", "volume", "current_volume"))
            )
        ]
        if not candidates:
            candidates = broad_candidates
        if target_is_upuuid:
            echoish = [
                p
                for p in candidates
                if (
                    "alexa" in str(p.get("provider") or "").lower()
                    or "echo" in str(p.get("name") or "").lower()
                    or "amazon" in str((p.get("device_info") or {}).get("manufacturer") or "").lower()
                )
            ]
            if echoish:
                candidates = echoish

        # 2. MAC address match
        target_mac = self._player_mac(target)
        if target_mac:
            for player in candidates:
                if self._player_mac(player) == target_mac:
                    pid = str(player.get("player_id") or "")
                    _log_detect(
                        "Volume companion auto-detected via MAC: %s → %s (MAC %s)",
                        target_player_id, pid, target_mac,
                    )
                    return pid
        else:
            logger.debug("Player %s has no MAC address; trying name-based companion detection", target_player_id)

        # 2b. Identifier match (uuid/ip/serial/etc.)
        target_ids = self._player_identifiers(target)
        if target_ids:
            for player in candidates:
                overlap = target_ids.intersection(self._player_identifiers(player))
                if overlap:
                    pid = str(player.get("player_id") or "")
                    _log_detect(
                        "Volume companion auto-detected via identifiers: %s → %s (shared=%s)",
                        target_player_id,
                        pid,
                        sorted(overlap)[:3],
                    )
                    return pid

        # 3. Name-based heuristic — useful when MA's UPnP integration doesn't expose MAC
        # Strip common prefixes and compare normalised names
        target_name = str(target.get("name") or "").lower().strip()
        # Remove UPnP-specific prefix tokens so "Echo Dot" matches "Nitish's Echo Dot"
        for player in candidates:
            cname = str(player.get("name") or "").lower().strip()
            # Check if either name is a substring of the other (covers "Echo Dot" in both)
            if target_name and cname and (target_name in cname or cname in target_name):
                pid = str(player.get("player_id") or "")
                _log_detect(
                    "Volume companion auto-detected via name match: %s ('%s') → %s ('%s')",
                    target_player_id, target_name, pid, cname,
                )
                return pid

        # 3b. Alexa/Echo heuristic fallback
        alexa_candidates = [
            p for p in candidates
            if (
                "alexa" in str(p.get("provider") or "").lower()
                or "echo" in str(p.get("name") or "").lower()
            )
        ]
        playing_alexa = [
            p for p in alexa_candidates
            if str(p.get("state") or p.get("playback_state") or "").lower() == "playing"
        ]
        if playing_alexa:
            pid = str(playing_alexa[0].get("player_id") or "")
            _log_detect(
                "Volume companion auto-detected via playing Alexa heuristic: %s → %s",
                target_player_id,
                pid,
            )
            return pid
        if len(alexa_candidates) == 1:
            pid = str(alexa_candidates[0].get("player_id") or "")
            _log_detect(
                "Volume companion auto-detected via single Alexa heuristic: %s → %s",
                target_player_id,
                pid,
            )
            return pid

        # 4. Last resort: if there is exactly one candidate with volume support, use it
        if len(candidates) == 1:
            pid = str(candidates[0].get("player_id") or "")
            _log_detect(
                "Volume companion auto-detected (single candidate): %s → %s", target_player_id, pid
            )
            return pid

        logger.warning(
            "No volume companion found for %s (MAC=%s, %d candidates). "
            "Set 'proxy_volume_player' in addon options to specify explicitly.",
            target_player_id, target_mac, len(candidates),
        )
        return None

    async def execute(self, request: ProxyCommandRequest) -> dict[str, Any]:
        if request.command == "refresh":
            return {
                "ok": True,
                "snapshot": (await self.get_snapshot()).model_dump(),
            }

        ma = self._new_client()
        ma_player_id = ""
        try:
            ma_player_id, queue_id = await self._resolve_player_target(ma, request.addon_player_id)
            if request.command == "play":
                # Before attempting play, check if the resolved player is actually available.
                # UPnP Echo Dots go offline between tracks — if unavailable, the play
                # command will silently fail while volume (routed to the Alexa companion)
                # keeps working.
                all_players = await ma.get_players()
                target_player = next(
                    (p for p in all_players if str(p.get("player_id") or "") == ma_player_id), None
                )
                target_available = bool((target_player or {}).get("available", False))
                target_state = str((target_player or {}).get("state") or "").lower()

                # If target is available and not off, try normal play first
                play_attempted = False
                if target_available and target_state not in ("off", "unavailable"):
                    try:
                        await ma.play(queue_id, player_id=ma_player_id)
                        play_attempted = True
                    except Exception as exc:
                        logger.warning(
                            "Play failed on primary player %s: %s — will try companion",
                            ma_player_id, exc,
                        )

                # Force-start check: verify audio actually started within 5 seconds.
                # Alexa companion players don't always report elapsed_time, so also
                # check if the player state transitions to "playing".
                started = False
                if play_attempted:
                    queue_state = await ma.get_queue_state(queue_id)

                    def _elapsed(payload: dict[str, Any] | None) -> float:
                        value = (payload or {}).get("elapsed_time")
                        if isinstance(value, (int, float)):
                            return float(value)
                        return 0.0

                    baseline_elapsed = _elapsed(queue_state)
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        queue_state_name = str((queue_state or {}).get("state") or "").lower()
                        current_elapsed = _elapsed(queue_state)
                        # Consider started if: state is "playing" AND either elapsed advanced
                        # or companion player is active (Alexa players may not report elapsed)
                        if queue_state_name == "playing" and (
                            current_elapsed > baseline_elapsed + 0.3
                            or current_elapsed > 0.0
                        ):
                            started = True
                            break
                        # Also check: if state changed to "playing" and at least 2s passed,
                        # trust it — Alexa providers don't always update elapsed
                        if queue_state_name == "playing" and (time.monotonic() - (deadline - 5.0)) > 2.0:
                            started = True
                            break
                        await asyncio.sleep(0.8)
                        queue_state = await ma.get_queue_state(queue_id)

                # If play wasn't attempted (player unavailable) or didn't start, try companion
                if not started:
                    companion_id = self._find_volume_companion(
                        all_players if all_players else await ma.get_players(),
                        ma_player_id,
                    )

                    if companion_id and companion_id != ma_player_id:
                        companion = next(
                            (p for p in all_players if str(p.get("player_id") or "") == companion_id),
                            None,
                        )
                        if companion:
                            comp_queue = str(
                                companion.get("active_queue")
                                or companion.get("queue_id")
                                or companion_id
                                or ""
                            )
                            logger.info(
                                "Primary player %s %s; routing play to companion %s (queue=%s)",
                                ma_player_id,
                                "unavailable" if not target_available else "not progressing",
                                companion_id,
                                comp_queue,
                            )

                            # Try to replay current content on the companion
                            replay_ok = False
                            
                            # Get the current queue item from the primary queue to replay
                            # We can get queue state even if play wasn't attempted, because the queue exists
                            try:
                                queue_state_fallback = await ma.get_queue_state(queue_id)
                                current_item = queue_state_fallback.get("current_item") if isinstance(queue_state_fallback, dict) else None
                                if not isinstance(current_item, dict):
                                    queue_items = await ma.get_queue_items(queue_id)
                                    idx = queue_state_fallback.get("current_index") if isinstance(queue_state_fallback, dict) else None
                                    if isinstance(idx, int) and 0 <= idx < len(queue_items):
                                        current_item = queue_items[idx]
                                    elif queue_items:
                                        current_item = queue_items[0]

                                force_uri = ""
                                force_query = ""
                                if isinstance(current_item, dict):
                                    media_item = current_item.get("media_item") or {}
                                    force_uri = str(media_item.get("uri") or current_item.get("uri") or "").strip()
                                    force_query = str(
                                        current_item.get("name")
                                        or media_item.get("name")
                                        or ""
                                    ).strip()

                                if force_uri:
                                    try:
                                        await ma.play_media_uri(comp_queue, force_uri)
                                        replay_ok = True
                                    except MusicAssistantError as exc:
                                        logger.warning("Companion play_media_uri failed: %s", exc)

                                if not replay_ok and force_query:
                                    try:
                                        result = await ma.search_and_play(force_query, queue_id=comp_queue)
                                        replay_ok = result is not None
                                    except MusicAssistantError as exc:
                                        logger.warning("Companion search_and_play failed: %s", exc)
                            except Exception as exc:
                                logger.warning("Failed to determine queue items for replay: %s", exc)

                            # Final fallback: just send play to companion queue directly
                            if not replay_ok:
                                try:
                                    await ma.play(comp_queue, player_id=companion_id)
                                except MusicAssistantError as exc:
                                    logger.warning("Companion play failed: %s", exc)
                    elif not play_attempted:
                        # No companion found either — try play on primary as last resort
                        logger.warning(
                            "Player %s unavailable and no companion found; attempting play anyway",
                            ma_player_id,
                        )
                        await ma.play(queue_id, player_id=ma_player_id)
            elif request.command == "pause":
                await ma.pause(queue_id, player_id=ma_player_id)
            elif request.command == "next":
                await ma.next_track(queue_id, player_id=ma_player_id)
            elif request.command == "previous":
                await ma.prev_track(queue_id, player_id=ma_player_id)
            elif request.command == "volume_set":
                if request.volume is None:
                    raise ValueError("volume is required for volume_set")
                # Determine which player to actually send volume to.
                # UPnP Echo Dots and similar devices have volume_set blocked by the manufacturer.
                # If the target doesn't support volume, find a companion (Alexa player for the
                # same physical device) and route to it instead.
                all_players = await ma.get_players()
                target_player = next(
                    (p for p in all_players if str(p.get("player_id") or "") == ma_player_id), None
                )
                target_features = (target_player.get("supported_features") or []) if target_player else []
                target_raw_volume = None
                if target_player is not None:
                    for _vol_key in ("volume_level", "volume", "current_volume"):
                        _vv = target_player.get(_vol_key)
                        if _vv is not None:
                            target_raw_volume = _vv
                            break
                vol_player_id = ma_player_id
                needs_companion = (
                    target_player is None
                    or "volume_set" not in target_features
                    or target_raw_volume is None
                )
                if needs_companion:
                    companion = self._find_volume_companion(all_players, ma_player_id)
                    if companion:
                        vol_player_id = companion
                    else:
                        logger.warning(
                            "Player %s has no volume_set support and no companion found; "
                            "attempting anyway. Set 'proxy_volume_player' in addon config "
                            "to force-route to a specific player (e.g. 'Nitish\\'s Echo Dot').",
                            ma_player_id,
                        )
                await ma.set_volume(vol_player_id, request.volume)
            elif request.command == "mute":
                if request.muted is None:
                    raise ValueError("muted (bool) is required for mute")
                all_players = await ma.get_players()
                target_player = next(
                    (p for p in all_players if str(p.get("player_id") or "") == ma_player_id), None
                )
                mute_player_id = ma_player_id
                if target_player is None or "volume_mute" not in (target_player.get("supported_features") or []):
                    companion = self._find_volume_companion(all_players, ma_player_id)
                    if companion:
                        mute_player_id = companion
                await ma.set_mute(mute_player_id, request.muted)
            elif request.command == "stop":
                await ma.stop(queue_id, player_id=ma_player_id)
            elif request.command == "play_query":
                if not request.query or not request.query.strip():
                    raise ValueError("query is required for play_query")
                await ma.search_and_play(request.query.strip(), queue_id=queue_id)
            elif request.command == "play_media":
                if not request.media_id or not request.media_id.strip():
                    raise ValueError("media_id is required for play_media")
                await ma.play_media_uri(queue_id, request.media_id.strip())
            else:
                raise ValueError(f"Unsupported command: {request.command}")
        except (MusicAssistantUnreachableError, MusicAssistantError) as exc:
            # MA can time out under load while still applying commands eventually.
            # Return optimistic success so HA controls don't hard-fail on transient timeouts.
            logger.warning(
                "MA command failed during %s for player %s: %s",
                request.command,
                ma_player_id or request.addon_player_id or "<unknown>",
                exc,
            )
            return {"ok": True, "warning": "ma_command_failed", "error": str(exc)}
        finally:
            await ma.close()

        # Let MA background state settle before returning the snapshot to HA
        if request.command in (
            "play", "pause", "stop", "next", "previous",
            "volume_set", "mute", "play_query", "play_media"
        ):
            await asyncio.sleep(0.5)

        try:
            player = await self.get_player(request.addon_player_id or ma_player_id)
            return {"ok": True, "player": player.model_dump()}
        except ValueError:
            # Command execution can still succeed even if the primary UUID player is
            # temporarily absent from MA discovery. Return success with a fresh snapshot
            # instead of surfacing an avoidable 422 to HA.
            logger.warning(
                "Command %s succeeded but player %s not present in snapshot; returning snapshot fallback",
                request.command,
                request.addon_player_id or ma_player_id,
            )
            return {
                "ok": True,
                "snapshot": (await self.get_snapshot()).model_dump(),
            }


class BackendWebSocketBridge:
    """Push live player snapshots to a backend and receive remote commands."""

    def __init__(self, settings: Settings, proxy: LocalProxyService) -> None:
        self._settings = settings
        self._proxy = proxy
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._last_error = ""
        self._last_sync_payload = ""

    @property
    def enabled(self) -> bool:
        return self._settings.backend_ws_enabled

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self._connected,
            "backend_ws_url": self._settings.backend_ws_url or None,
            "instance_id": self._proxy.instance_id,
            "last_error": self._last_error or None,
        }

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run_forever(), name="backend-ws-bridge")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._connected = False

    async def _run_forever(self) -> None:
        headers: dict[str, str] = {}
        if self._settings.backend_ws_token:
            headers["Authorization"] = f"Bearer {self._settings.backend_ws_token}"

        while True:
            try:
                async with websockets.connect(
                    self._settings.backend_ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    **_websocket_connect_headers(headers),
                ) as websocket:
                    self._connected = True
                    self._last_error = ""
                    self._last_sync_payload = ""
                    logger.info(
                        "Connected backend websocket bridge: %s",
                        self._settings.backend_ws_url,
                    )

                    await self._send_hello(websocket)

                    sender = asyncio.create_task(self._status_loop(websocket))
                    receiver = asyncio.create_task(self._receive_loop(websocket))
                    done, pending = await asyncio.wait(
                        {sender, receiver},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in pending:
                        task.cancel()
                    for task in done:
                        task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                logger.warning("Backend websocket bridge disconnected: %s", exc)
                await asyncio.sleep(5)

    async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None:
        await websocket.send(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    async def _send_hello(self, websocket: Any) -> None:
        snapshot = await self._proxy.get_snapshot()
        await self._send_json(
            websocket,
            {
                "type": "hello",
                "instance_id": self._proxy.instance_id,
                "player_prefix": self._proxy.player_prefix,
                "capabilities": [
                    "refresh",
                    "play",
                    "pause",
                    "stop",
                    "next",
                    "previous",
                    "volume_set",
                    "play_query",
                    "play_media",
                ],
                "snapshot": snapshot.model_dump(),
            },
        )

    async def _status_loop(self, websocket: Any) -> None:
        while True:
            snapshot = await self._proxy.get_snapshot()
            snapshot_payload = snapshot.model_dump_json(exclude_none=True)
            if snapshot_payload != self._last_sync_payload:
                self._last_sync_payload = snapshot_payload
                await self._send_json(
                    websocket,
                    {
                        "type": "status",
                        "instance_id": self._proxy.instance_id,
                        "snapshot": json.loads(snapshot_payload),
                    },
                )
            await asyncio.sleep(self._settings.proxy_status_poll_interval)

    async def _receive_loop(self, websocket: Any) -> None:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON backend message")
                continue

            message_type = str(message.get("type") or "")
            if message_type == "ping":
                await self._send_json(websocket, {"type": "pong", "instance_id": self._proxy.instance_id})
                continue

            if message_type in {"refresh", "request_snapshot"}:
                snapshot = await self._proxy.get_snapshot()
                await self._send_json(
                    websocket,
                    {
                        "type": "status",
                        "instance_id": self._proxy.instance_id,
                        "snapshot": snapshot.model_dump(),
                    },
                )
                continue

            if message_type != "command":
                continue

            request_id = str(message.get("request_id") or "")
            try:
                result = await self._proxy.execute(
                    ProxyCommandRequest(
                        command=message.get("command"),
                        addon_player_id=message.get("addon_player_id"),
                        query=message.get("query"),
                        media_id=message.get("media_id"),
                        media_type=message.get("media_type"),
                        volume=message.get("volume"),
                        request_id=request_id or None,
                    )
                )
                await self._send_json(
                    websocket,
                    {
                        "type": "command_result",
                        "request_id": request_id or None,
                        **result,
                    },
                )
            except Exception as exc:
                await self._send_json(
                    websocket,
                    {
                        "type": "command_result",
                        "request_id": request_id or None,
                        "ok": False,
                        "error": str(exc),
                    },
                )


async def websocket_status_session(
    proxy: LocalProxyService,
    send_json: Callable[[dict[str, Any]], asyncio.Future | Any],
    receive_json: Callable[[], asyncio.Future | Any],
    poll_interval: int,
) -> None:
    """Drive a websocket session with live snapshots and command handling."""

    last_snapshot_payload = ""
    snapshot = await proxy.get_snapshot()
    await send_json({"type": "hello", "snapshot": snapshot.model_dump()})

    while True:
        snapshot = await proxy.get_snapshot()
        snapshot_payload = snapshot.model_dump_json(exclude_none=True)
        if snapshot_payload != last_snapshot_payload:
            last_snapshot_payload = snapshot_payload
            await send_json({"type": "status", "snapshot": json.loads(snapshot_payload)})

        try:
            message = await asyncio.wait_for(receive_json(), timeout=poll_interval)
        except asyncio.TimeoutError:
            continue

        message_type = str(message.get("type") or "")
        if message_type == "ping":
            await send_json({"type": "pong", "instance_id": proxy.instance_id})
            continue

        if message_type in {"refresh", "request_snapshot"}:
            snapshot = await proxy.get_snapshot()
            await send_json({"type": "status", "snapshot": snapshot.model_dump()})
            continue

        if message_type != "command":
            continue

        request_id = str(message.get("request_id") or "")
        try:
            result = await proxy.execute(
                ProxyCommandRequest(
                    command=message.get("command"),
                    addon_player_id=message.get("addon_player_id"),
                    query=message.get("query"),                    media_id=message.get("media_id"),
                    media_type=message.get("media_type"),                    volume=message.get("volume"),
                    request_id=request_id or None,
                )
            )
            await send_json(
                {
                    "type": "command_result",
                    "request_id": request_id or None,
                    **result,
                }
            )
        except Exception as exc:
            await send_json(
                {
                    "type": "command_result",
                    "request_id": request_id or None,
                    "ok": False,
                    "error": str(exc),
                }
            )