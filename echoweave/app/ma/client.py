"""Music Assistant API client.

Music Assistant exposes a JSON-RPC style API at POST /api.
Every request follows the shape:
    { "message_id": "<str>", "command": "<namespace/action>", "args": { ... } }

The response wraps the result:
    { "message_id": "<str>", "result": <data> }

This client handles:
  - Connection lifecycle (lazy init, clean close)
  - Retry on transient network errors
  - Fallback commands for MA version differences
  - Session caching for stream URL construction
  - All playback and discovery operations needed by the skill
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0
_MAX_RETRIES = 2

# ── Session-ID cache ──────────────────────────────────────────────────────────
# MA's PlayerQueue.session_id is marked serialize="omit" so it never appears
# in API responses. We extract it from player.current_media.custom_data when
# available and cache it here. When absent, we use "0" — MA accepts any value
# when a queue has not been initialised with a specific session yet.
_session_cache: dict[str, tuple[str, float]] = {}
_SESSION_CACHE_TTL = 600  # 10 minutes


def _get_cached_session_id(queue_id: str) -> Optional[str]:
    entry = _session_cache.get(queue_id)
    if not entry:
        return None
    sid, ts = entry
    if time.time() - ts > _SESSION_CACHE_TTL:
        del _session_cache[queue_id]
        return None
    return sid


def _cache_session_id(queue_id: str, session_id: str) -> None:
    _session_cache[queue_id] = (session_id, time.time())


def invalidate_session_cache(queue_id: Optional[str] = None) -> None:
    if queue_id:
        _session_cache.pop(queue_id, None)
    else:
        _session_cache.clear()


class MusicAssistantError(Exception):
    pass


class MusicAssistantUnreachableError(MusicAssistantError):
    pass


class MusicAssistantAuthError(MusicAssistantError):
    pass


class MusicAssistantClient:
    """Async client for the Music Assistant JSON-RPC API.

    Parameters
    ----------
    base_url:
        Root URL of the MA server, e.g. http://192.168.1.42:8095
    token:
        Long-lived bearer token from MA → Settings → Long-lived access tokens
    timeout:
        Default request timeout in seconds
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._token = token
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _make_headers(self) -> dict[str, str]:
        if not self._token:
            logger.warning("MA token is empty — requests will be unauthenticated")
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._make_headers(),
                timeout=self._timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Low-level ─────────────────────────────────────────────────────────────

    @property
    def _api_url(self) -> str:
        """The JSON-RPC endpoint."""
        if self._base_url.endswith("/api"):
            return self._base_url
        return f"{self._base_url}/api"

    async def _command(self, command: str, **args: Any) -> Any:
        """Execute a single MA JSON-RPC command.

        Raises MusicAssistantError on HTTP errors.
        Raises MusicAssistantUnreachableError on connectivity failures.
        Raises MusicAssistantAuthError on 401.
        """
        if not self._base_url:
            raise MusicAssistantUnreachableError("MA URL is not configured (ECHO_MA_URL)")

        client = await self._ensure_client()
        message_id = str(int(time.time() * 1000))
        payload = {"message_id": message_id, "command": command, "args": args}

        logger.info(
            "MA command: %s  args=%s",
            command,
            json.dumps(args, separators=(",", ":"), default=str),
        )

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await client.post(self._api_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                # Keep debug logs compact for high-frequency polling commands.
                if command in {"players/all", "player_queues/get", "player_queues/items"}:
                    if isinstance(data, list):
                        summary = f"list[{len(data)}]"
                    elif isinstance(data, dict):
                        summary = f"dict_keys={list(data.keys())[:8]}"
                    else:
                        summary = type(data).__name__
                    logger.debug("MA raw response: %s  summary=%s", command, summary)
                else:
                    logger.debug(
                        "MA raw response: %s  body=%s",
                        command,
                        json.dumps(data, separators=(",", ":"), default=str)[:600],
                    )
                # MA returns HTTP 200 even for errors — error is in the JSON body.
                # Detect error if: "error" key present AND non-null, regardless of "result"
                if isinstance(data, dict):
                    err_val = data.get("error")
                    if err_val is not None and err_val is not False and err_val != "":
                        logger.warning(
                            "MA RPC error: command=%s  error=%s", command, err_val
                        )
                        raise MusicAssistantError(
                            f"MA RPC error for {command}: {err_val}"
                        )
                result = data.get("result", data) if isinstance(data, dict) else data
                logger.info("MA response: %s  status=%d  result_type=%s", command, resp.status_code, type(result).__name__)
                return result
            except httpx.ConnectError as exc:
                last_exc = exc
                logger.warning(
                    "MA connect error attempt %d/%d: command=%s  %s",
                    attempt, _MAX_RETRIES, command, exc,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "MA timeout attempt %d/%d: command=%s", attempt, _MAX_RETRIES, command
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                body_text = (exc.response.text or "")[:500]
                logger.warning(
                    "MA HTTP %d: command=%s  body=%s", status, command, body_text
                )
                if status == 401:
                    raise MusicAssistantAuthError(
                        f"MA token rejected (401) for command={command}"
                    ) from exc
                raise MusicAssistantError(
                    f"MA API {status} for command={command}: {body_text}"
                ) from exc

        raise MusicAssistantUnreachableError(
            f"Could not reach MA at {self._base_url} after {_MAX_RETRIES} attempts (command={command})"
        ) from last_exc

    async def _command_fallback(self, commands: list[str], **args: Any) -> Any:
        """Try a list of command names in order; succeed on the first that works.

        Used when MA API naming has changed between versions.
        """
        last_exc: Optional[MusicAssistantError] = None
        for idx, cmd in enumerate(commands):
            try:
                result = await self._command(cmd, **args)
                if idx > 0:
                    logger.info("MA fallback succeeded on command=%s", cmd)
                return result
            except MusicAssistantError as exc:
                last_exc = exc
                err = str(exc)
                # Retry on routing errors (404/500) OR MA RPC errors (command renamed between versions)
                if idx < len(commands) - 1 and (
                    "API 404" in err or "API 500" in err or "RPC error" in err
                ):
                    logger.debug("MA command=%s failed, trying next fallback", cmd)
                    continue
                raise
        if last_exc:
            raise last_exc
        raise MusicAssistantError("No commands to try")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_server_info(self) -> dict[str, Any]:
        """Fetch MA server metadata. Useful for connectivity checks."""
        result = await self._command("server/info")
        if isinstance(result, dict):
            return result
        return {}

    async def get_players(self) -> list[dict[str, Any]]:
        """Return all players known to MA."""
        result = await self._command_fallback(["players/all", "players/get"])
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.values())
        return []

    async def get_queue_items(self, queue_id: str) -> list[dict[str, Any]]:
        """Return the items currently in a player queue."""
        try:
            result = await self._command_fallback(
                ["player_queues/items", "playerqueues/items"],
                queue_id=queue_id,
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                items = result.get("items") or result.get("result") or []
                return items if isinstance(items, list) else []
        except MusicAssistantError:
            logger.debug("Queue items unavailable for queue=%s (player may be idle)", queue_id)
        return []

    async def get_queue_state(self, queue_id: str) -> dict[str, Any]:
        """Return state for a specific player queue (includes current_index)."""
        try:
            result = await self._command_fallback(
                ["player_queues/get", "playerqueues/get"],
                queue_id=queue_id,
            )
            if isinstance(result, dict):
                return result
        except MusicAssistantError:
            logger.debug("Queue state unavailable for queue=%s (player may be idle)", queue_id)
        return {}

    async def get_current_queue_item(self, queue_id: str) -> Optional[dict[str, Any]]:
        """Return the currently-active queue item, or None if the queue is empty."""
        try:
            state = await self.get_queue_state(queue_id)
            current_index = state.get("current_index")
            if current_index is None:
                logger.debug("Queue %s has no current_index", queue_id)
                return None

            items = await self.get_queue_items(queue_id)
            if not items:
                logger.debug("Queue %s is empty", queue_id)
                return None

            try:
                idx = int(current_index)
                if 0 <= idx < len(items):
                    item = items[idx]
                    item["queue_id"] = queue_id
                    logger.info(
                        "Current queue item: queue=%s idx=%d name=%s item_id=%s",
                        queue_id, idx,
                        item.get("name", "?"),
                        item.get("queue_item_id", "?"),
                    )
                    return item
            except (ValueError, TypeError):
                pass

            return None
        except MusicAssistantError:
            logger.warning("Could not get current queue item for queue=%s", queue_id, exc_info=True)
            return None

    async def get_active_queue_id(self) -> Optional[str]:
        """Find the queue_id for the first actively-playing or available player."""
        try:
            players = await self.get_players()
        except MusicAssistantError:
            logger.warning("Could not list players for queue discovery", exc_info=True)
            return None

        # Prefer a player that is actively playing
        for player in players:
            state = str(player.get("state") or "").lower()
            if state == "playing":
                queue_id = (
                    player.get("active_queue")
                    or player.get("queue_id")
                    or player.get("player_id")
                )
                if queue_id and isinstance(queue_id, str) and queue_id.strip():
                    logger.info("Active queue discovered: %s (player=%s)", queue_id, player.get("player_id"))
                    return queue_id

        # Fall back to any available, powered player
        for player in players:
            if not player.get("available", False):
                continue
            queue_id = (
                player.get("active_queue")
                or player.get("queue_id")
                or player.get("player_id")
            )
            if queue_id and isinstance(queue_id, str) and queue_id.strip():
                logger.info("Fallback queue: %s (player=%s)", queue_id, player.get("player_id"))
                return queue_id

        logger.warning("No active queue found across all players")
        return None

    async def search_and_play(
        self,
        query: str,
        queue_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Search MA for query and enqueue the best result on queue_id.

        Returns a dict with queue_id, queue_item_id, name, artist, uri
        on success, or None if nothing was found or could be played.
        """
        normalized = re.sub(r"\s+", " ", query.strip().lower())
        normalized = re.sub(r"^(songs?|music)\s+by\s+", "", normalized).strip()
        if not normalized:
            return None

        if not queue_id:
            queue_id = await self.get_active_queue_id()
        if not queue_id:
            logger.warning("search_and_play: no queue_id available")
            return None

        # Search order: tracks first, then artists, albums, playlists
        search_types = ["track", "artist", "album", "playlist"]
        for media_type in search_types:
            items = await self._search(normalized, media_type)
            logger.info(
                "Search: query=%r type=%s results=%d", normalized, media_type, len(items)
            )
            if not items:
                continue

            top = items[0]
            uri = top.get("uri", "")
            item_id = top.get("item_id") or top.get("id") or ""

            # For artists, find their tracks instead
            if media_type == "artist":
                artist_name = top.get("name", normalized)
                tracks = await self._search(artist_name, "track")
                if not tracks:
                    continue
                top = tracks[0]
                uri = top.get("uri", "")
                item_id = top.get("item_id") or top.get("id") or ""
                media_type = "track"

            if not uri and not item_id:
                continue

            play_uri = uri or f"library://{media_type}/{item_id}"
            logger.info(
                "Enqueuing: queue=%s uri=%s name=%s", queue_id, play_uri, top.get("name", "?")
            )

            play_media_ok = False
            for option_key, option_val in [
                ("option", "play"),
                ("option", "replace"),
                ("queue_option", "play"),
            ]:
                try:
                    invalidate_session_cache(queue_id)
                    await self._command(
                        "player_queues/play_media",
                        queue_id=queue_id,
                        media=play_uri,
                        **{option_key: option_val},
                    )
                    logger.info(
                        "play_media succeeded: queue=%s %s=%s",
                        queue_id, option_key, option_val,
                    )
                    play_media_ok = True
                    break
                except MusicAssistantError as exc:
                    logger.warning(
                        "play_media %s=%s failed: %s", option_key, option_val, exc
                    )

            if not play_media_ok:
                logger.warning(
                    "play_media exhausted all options for queue=%s uri=%s",
                    queue_id, play_uri,
                )
                continue

            # Explicitly start playback — some MA versions only enqueue on play_media
            await asyncio.sleep(0.25)
            try:
                await self._command_fallback(
                    ["player_queues/play", "playerqueues/play"],
                    queue_id=queue_id,
                )
                logger.debug("Explicit play sent after play_media: queue=%s", queue_id)
            except MusicAssistantError as exc:
                logger.debug("Explicit play after play_media (non-fatal): %s", exc)

            # Give MA a moment to enqueue the item, then read back the item ID
            await asyncio.sleep(0.3)
            q_items = await self.get_queue_items(queue_id)
            matched_item_id = ""
            for q_item in reversed(q_items):
                q_uri = q_item.get("uri", "")
                q_name = str(q_item.get("name") or "").lower()
                item_name = str(top.get("name") or "").lower()
                if q_uri and (q_uri == play_uri or q_uri == uri):
                    matched_item_id = q_item.get("queue_item_id", "")
                    break
                if item_name and q_name and q_name == item_name:
                    matched_item_id = q_item.get("queue_item_id", "")
                    break

            if not matched_item_id and q_items:
                matched_item_id = q_items[-1].get("queue_item_id", "")

            if not matched_item_id:
                logger.warning(
                    "Could not identify queued item for query=%r queue=%s", normalized, queue_id
                )
                matched_item_id = item_id or normalized

            logger.info(
                "search_and_play success: query=%r name=%s queue=%s item=%s",
                normalized, top.get("name", "?"), queue_id, matched_item_id,
            )

            return {
                "queue_id": queue_id,
                "queue_item_id": matched_item_id,
                "name": top.get("name", ""),
                "artist": top.get("artist") or "",
                "uri": play_uri,
            }

        logger.warning("search_and_play: no results found for query=%r", normalized)
        return None

    async def _search(self, query: str, media_type: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search MA for media. Tries multiple payload shapes for version compatibility."""
        singular = {"tracks": "track", "artists": "artist", "albums": "album", "playlists": "playlist"}.get(
            media_type, media_type
        )

        candidates: list[dict[str, Any]] = [
            {"search_query": query, "media_types": [singular], "limit": limit},
            {"search_query": query, "media_types": [media_type], "limit": limit},
            {"search": query, "media_types": [singular], "limit": limit},
            {"query": query, "media_types": [singular], "limit": limit},
        ]

        for payload in candidates:
            try:
                result = await self._command_fallback(
                    ["music/search", "music.search"], **payload
                )
                items = self._extract_list(result, media_type)
                if items:
                    return items
            except MusicAssistantError:
                continue

        return []

    @staticmethod
    def _extract_list(data: Any, key: str) -> list[dict[str, Any]]:
        """Recursively find a list of dicts under a variety of possible keys."""
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        if not isinstance(data, dict):
            return []
        singular = {"tracks": "track", "artists": "artist", "albums": "album", "playlists": "playlist"}.get(key, key)
        for k in (key, singular, "items", "result", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return [i for i in v if isinstance(i, dict)]
        nested = data.get("result")
        if isinstance(nested, dict):
            for k in (key, singular):
                v = nested.get(k)
                if isinstance(v, list):
                    return [i for i in v if isinstance(i, dict)]
        return []

    # ── Playback controls ─────────────────────────────────────────────────────

    async def play(self, queue_id: str, *, player_id: str | None = None) -> None:
        """Resume / start the queue.

        Tries queue-level play first (player_queues/play), then falls back
        to the player-level play command (players/cmd/play). The player-level
        command is more reliable for UPnP/physical players that may be idle.
        """
        logger.info("MA play: queue=%s", queue_id)
        try:
            await self._command("player_queues/play", queue_id=queue_id)
        except MusicAssistantError as exc:
            logger.warning(
                "Queue-level play failed (%s), trying player-level: queue=%s", exc, queue_id
            )
            target_player = player_id or queue_id
            try:
                await self._command("players/cmd/play", player_id=target_player)
            except MusicAssistantError as player_exc:
                logger.warning(
                    "Player-level play failed (%s), trying current-item play_media fallback: queue=%s",
                    player_exc,
                    queue_id,
                )
                state = await self.get_queue_state(queue_id)
                current_item = state.get("current_item") if isinstance(state, dict) else None
                if not isinstance(current_item, dict):
                    items = await self.get_queue_items(queue_id)
                    idx = state.get("current_index") if isinstance(state, dict) else None
                    if isinstance(idx, int) and 0 <= idx < len(items):
                        current_item = items[idx]
                    elif items:
                        current_item = items[0]

                play_uri = ""
                if isinstance(current_item, dict):
                    media_item = current_item.get("media_item") or {}
                    play_uri = str(media_item.get("uri") or current_item.get("uri") or "").strip()

                if play_uri:
                    await self.play_media_uri(queue_id, play_uri, option="play")
                    return
                raise

    async def pause(self, queue_id: str, *, player_id: str | None = None) -> None:
        """Pause the queue."""
        logger.info("MA pause: queue=%s", queue_id)
        try:
            await self._command_fallback(
                ["player_queues/pause", "playerqueues/pause"],
                queue_id=queue_id,
            )
        except MusicAssistantError as exc:
            logger.warning(
                "Queue-level pause failed (%s), trying player-level: player=%s",
                exc,
                queue_id,
            )
            target_player = player_id or queue_id
            await self._command("players/cmd/pause", player_id=target_player)

    async def stop(self, queue_id: str, *, player_id: str | None = None) -> None:
        """Stop the queue."""
        logger.info("MA stop: queue=%s", queue_id)
        try:
            await self._command_fallback(
                ["player_queues/stop", "playerqueues/stop"],
                queue_id=queue_id,
            )
        except MusicAssistantError as exc:
            logger.warning(
                "Queue-level stop failed (%s), trying player-level: player=%s",
                exc,
                queue_id,
            )
            target_player = player_id or queue_id
            await self._command("players/cmd/stop", player_id=target_player)

    async def play_media_uri(self, queue_id: str, media: str, option: str = "play") -> None:
        """Play a media URI on a queue (library://track/123, spotify://..., etc.)."""
        logger.info("MA play_media: queue=%s media=%s option=%s", queue_id, media, option)
        invalidate_session_cache(queue_id)
        await self._command(
            "player_queues/play_media",
            queue_id=queue_id,
            media=media,
            option=option,
        )

    async def pause_player(self, player_id: str) -> None:
        """Pause a specific player by player_id."""
        logger.info("MA pause player: player=%s", player_id)
        await self._command("players/cmd/pause", player_id=player_id)

    async def next_track(self, queue_id: str, *, player_id: str | None = None) -> None:
        """Skip to the next track in the queue."""
        logger.info("MA next: queue=%s", queue_id)
        try:
            await self._command_fallback(
                ["player_queues/next", "playerqueues/next"],
                queue_id=queue_id,
            )
        except MusicAssistantError as exc:
            logger.warning(
                "Queue-level next failed (%s), trying player-level: player=%s",
                exc,
                queue_id,
            )
            target_player = player_id or queue_id
            await self._command_fallback(
                ["players/cmd/next", "players/cmd/next_track"],
                player_id=target_player,
            )

    async def prev_track(self, queue_id: str, *, player_id: str | None = None) -> None:
        """Go back to the previous track in the queue."""
        logger.info("MA previous: queue=%s", queue_id)
        try:
            await self._command_fallback(
                ["player_queues/previous", "playerqueues/previous", "player_queues/prev"],
                queue_id=queue_id,
            )
        except MusicAssistantError as exc:
            logger.warning(
                "Queue-level previous failed (%s), trying player-level: player=%s",
                exc,
                queue_id,
            )
            target_player = player_id or queue_id
            await self._command_fallback(
                ["players/cmd/previous", "players/cmd/prev", "players/cmd/previous_track"],
                player_id=target_player,
            )

    async def set_volume(self, player_id: str, volume: int) -> None:
        """Set player volume (0-100)."""
        vol = max(0, min(100, int(volume)))
        logger.info("MA volume: player=%s volume=%d", player_id, vol)
        try:
            await self._command(
                "players/cmd/volume_set",
                player_id=player_id,
                volume_level=vol,
            )
        except MusicAssistantError as exc:
            # Some MA builds use "volume" instead of "volume_level"
            logger.warning("players/cmd/volume_set failed (%s), retrying with 'volume' param", exc)
            await self._command(
                "players/cmd/volume_set",
                player_id=player_id,
                volume=vol,
            )
        # Brief pause so MA state reflects the change before any snapshot reads
        await asyncio.sleep(0.3)

    async def set_mute(self, player_id: str, muted: bool) -> None:
        """Mute or unmute a player."""
        logger.info("MA mute: player=%s muted=%s", player_id, muted)
        await self._command_fallback(
            ["players/cmd/volume_mute", "players/cmd/mute"],
            player_id=player_id,
            muted=muted,
        )

    # ── Stream URL resolution ─────────────────────────────────────────────────

    async def get_stream_url(self, queue_id: str, item_id: str) -> str:
        """Build the internal MA stream URL for a given queue item.

        MA stream path: /stream/single/{session_id}/{queue_id}/{item_id}/{player_id}.flac

        session_id is not exposed via the REST API. We extract it from
        player.current_media.custom_data when available and cache it.
        When absent, "0" is accepted by MA for fresh sessions.
        """
        if not self._base_url:
            raise MusicAssistantUnreachableError("MA URL is not configured")

        # Find the player that owns this queue
        player_id = ""
        session_id = _get_cached_session_id(queue_id) or "0"

        try:
            players = await self.get_players()
            for player in players:
                active = (
                    player.get("active_queue")
                    or player.get("queue_id")
                    or player.get("player_id")
                )
                if str(active or "") == queue_id or str(player.get("player_id") or "") == queue_id:
                    player_id = str(player.get("player_id") or "")
                    # Try to extract session_id from current_media
                    current_media = player.get("current_media")
                    if isinstance(current_media, dict):
                        custom_data = current_media.get("custom_data")
                        if isinstance(custom_data, dict):
                            sid = str(custom_data.get("session_id") or "").strip()
                            if sid and sid != "0":
                                session_id = sid
                                _cache_session_id(queue_id, sid)
                                logger.debug(
                                    "Got session_id=%s from current_media queue=%s", sid, queue_id
                                )
                    break
        except Exception:
            logger.warning("Could not resolve player for stream queue=%s", queue_id, exc_info=True)

        if not player_id:
            player_id = queue_id  # MA often uses player_id == queue_id

        url = (
            f"{self._base_url}/stream/single/{session_id}"
            f"/{queue_id}/{item_id}/{player_id}.flac"
        )
        logger.info(
            "MA stream URL: queue=%s item=%s player=%s session=%s  →  %s",
            queue_id, item_id, player_id, session_id, url,
        )
        return url
