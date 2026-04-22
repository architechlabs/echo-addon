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

    @staticmethod
    def _feature_names(player: dict[str, Any]) -> set[str]:
        raw_features = player.get("supported_features") or []
        features: set[str] = set()
        if isinstance(raw_features, list):
            for item in raw_features:
                if item is None:
                    continue
                features.add(str(item).strip().lower())
        return features

    @staticmethod
    def _player_volume_level(player: dict[str, Any]) -> float | None:
        raw_volume: int | float | None = None
        for key in ("volume_level", "volume", "current_volume"):
            value = player.get(key)
            if isinstance(value, (int, float)):
                raw_volume = value
                break
        if raw_volume is None:
            return None
        if raw_volume > 1.0:
            return max(0.0, min(1.0, float(raw_volume) / 100.0))
        return max(0.0, min(1.0, float(raw_volume)))

    @staticmethod
    def _resolve_current_item(
        queue_state: dict[str, Any],
        queue_items: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        current_item = queue_state.get("current_item")
        if isinstance(current_item, dict):
            return current_item

        current_index = queue_state.get("current_index")
        if isinstance(current_index, int) and 0 <= current_index < len(queue_items):
            item = queue_items[current_index]
            if isinstance(item, dict):
                return item

        if queue_items:
            first = queue_items[0]
            if isinstance(first, dict):
                return first

        return None

    @staticmethod
    def _elapsed_seconds(queue_state: dict[str, Any] | None) -> float:
        value = (queue_state or {}).get("elapsed_time")
        if isinstance(value, (int, float)):
            return float(value)
        return 0.0

    async def _wait_for_playback_progress(
        self,
        ma: MusicAssistantClient,
        queue_id: str,
        *,
        timeout_seconds: float,
    ) -> bool:
        initial_state = await ma.get_queue_state(queue_id)
        baseline = self._elapsed_seconds(initial_state)
        deadline = time.monotonic() + timeout_seconds
        state = initial_state

        while True:
            state_name = str((state or {}).get("state") or "").strip().lower()
            elapsed = self._elapsed_seconds(state)
            if state_name == "playing" and elapsed > baseline + 0.25:
                return True
            if elapsed > baseline + 0.75:
                return True

            if time.monotonic() >= deadline:
                return False

            await asyncio.sleep(0.8)
            state = await ma.get_queue_state(queue_id)

    async def _queue_playback_target(
        self,
        ma: MusicAssistantClient,
        queue_id: str,
    ) -> tuple[str, str]:
        queue_state = await ma.get_queue_state(queue_id)
        queue_items = await ma.get_queue_items(queue_id)
        current_item = self._resolve_current_item(queue_state, queue_items) or {}

        media_item = current_item.get("media_item") or {}
        media_uri = str(media_item.get("uri") or current_item.get("uri") or "").strip()
        query = str(current_item.get("name") or media_item.get("name") or "").strip()
        return media_uri, query

    async def _build_player_snapshot(
        self,
        ma: MusicAssistantClient,
        player: dict[str, Any],
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

        current_item = self._resolve_current_item(queue_state, queue_items)

        volume_level = self._player_volume_level(player)
        muted = player.get("volume_muted")
        if muted is None:
            muted = player.get("muted")
        is_volume_muted = bool(muted) if muted is not None else None

        features = self._feature_names(player)
        has_volume_support = (
            "volume_set" in features
            or "volume_mute" in features
            or volume_level is not None
            or is_volume_muted is not None
        )

        available = bool(player.get("available", True))
        state = str(player.get("state") or player.get("playback_state") or "unknown")
        queue_state_name = str(queue_state.get("state") or "").strip().lower()
        if queue_state_name in {"playing", "paused", "idle", "buffering"}:
            state = queue_state_name

        return ProxyPlayerSnapshot(
            addon_player_id=self.addon_player_id(player_id),
            ma_player_id=player_id,
            name=str(player.get("name") or player_id),
            available=available,
            state=state,
            powered=player.get("powered"),
            volume_level=volume_level,
            is_volume_muted=is_volume_muted,
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
                *(self._build_player_snapshot(ma, player) for player in players)
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
                await ma.play(queue_id, player_id=ma_player_id)
                started = await self._wait_for_playback_progress(
                    ma,
                    queue_id,
                    timeout_seconds=8.0,
                )
                if not started:
                    media_uri, query = await self._queue_playback_target(ma, queue_id)
                    replayed = False

                    if media_uri:
                        try:
                            await ma.play_media_uri(queue_id, media_uri, option="play")
                            replayed = True
                        except MusicAssistantError as replay_exc:
                            logger.warning(
                                "play_media fallback failed for %s: %s",
                                queue_id,
                                replay_exc,
                            )

                    if not replayed and query:
                        try:
                            replay_result = await ma.search_and_play(query, queue_id=queue_id)
                            replayed = replay_result is not None
                        except MusicAssistantError as replay_exc:
                            logger.warning(
                                "search_and_play fallback failed for %s: %s",
                                queue_id,
                                replay_exc,
                            )

                    if replayed:
                        await asyncio.sleep(0.25)
                        with contextlib.suppress(MusicAssistantError):
                            await ma.play(queue_id, player_id=ma_player_id)
                        started = await self._wait_for_playback_progress(
                            ma,
                            queue_id,
                            timeout_seconds=8.0,
                        )

                    if not started:
                        raise MusicAssistantError(
                            f"Playback did not progress within 8 seconds for queue={queue_id}"
                        )
            elif request.command == "pause":
                await ma.pause(queue_id, player_id=ma_player_id)
            elif request.command == "next":
                await ma.next_track(queue_id, player_id=ma_player_id)
            elif request.command == "previous":
                await ma.prev_track(queue_id, player_id=ma_player_id)
            elif request.command == "volume_set":
                if request.volume is None:
                    raise ValueError("volume is required for volume_set")
                await ma.set_volume(ma_player_id, request.volume)
            elif request.command == "mute":
                if request.muted is None:
                    raise ValueError("muted (bool) is required for mute")
                await ma.set_mute(ma_player_id, request.muted)
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
            logger.warning(
                "MA command failed during %s for player %s: %s",
                request.command,
                ma_player_id or request.addon_player_id or "<unknown>",
                exc,
            )
            response: dict[str, Any] = {
                "ok": request.command != "play",
                "warning": "ma_command_failed",
                "error": str(exc),
            }
            if request.command == "play":
                response["error_code"] = "playback_not_started"
            with contextlib.suppress(Exception):
                response["snapshot"] = (await self.get_snapshot()).model_dump()
            return response
        finally:
            await ma.close()

        await asyncio.sleep(0.35)

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
                    query=message.get("query"),
                    media_id=message.get("media_id"),
                    media_type=message.get("media_type"),
                    volume=message.get("volume"),
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