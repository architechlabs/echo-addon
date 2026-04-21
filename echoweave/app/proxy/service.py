"""Local Music Assistant proxy service and optional backend websocket bridge."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.ma.client import MusicAssistantAuthError, MusicAssistantClient
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

        # MA volume is 0-100 integer; HA expects 0.0-1.0 float
        raw_vol = player.get("volume_level")
        volume_level = float(raw_vol) / 100.0 if isinstance(raw_vol, (int, float)) else None
        is_volume_muted = player.get("volume_muted") or player.get("muted")

        return ProxyPlayerSnapshot(
            addon_player_id=self.addon_player_id(player_id),
            ma_player_id=player_id,
            name=str(player.get("name") or player_id),
            available=bool(player.get("available", False)),
            state=str(player.get("state") or player.get("playback_state") or "unknown"),
            powered=player.get("powered"),
            volume_level=volume_level,
            is_volume_muted=bool(is_volume_muted) if is_volume_muted is not None else None,
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
            players = self._filter_players(await ma.get_players())
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
        players = self._filter_players(await ma.get_players())
        for player in players:
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
        return ma_player_id, ma_player_id

    async def execute(self, request: ProxyCommandRequest) -> dict[str, Any]:
        if request.command == "refresh":
            return {
                "ok": True,
                "snapshot": (await self.get_snapshot()).model_dump(),
            }

        ma = self._new_client()
        try:
            ma_player_id, queue_id = await self._resolve_player_target(ma, request.addon_player_id)
            if request.command == "play":
                await ma.play(queue_id)
            elif request.command == "pause":
                await ma.pause(queue_id)
            elif request.command == "next":
                await ma.next_track(queue_id)
            elif request.command == "previous":
                await ma.prev_track(queue_id)
            elif request.command == "volume_set":
                if request.volume is None:
                    raise ValueError("volume is required for volume_set")
                await ma.set_volume(ma_player_id, request.volume)
            elif request.command == "mute":
                if request.muted is None:
                    raise ValueError("muted (bool) is required for mute")
                await ma.set_mute(ma_player_id, request.muted)
            elif request.command == "stop":
                await ma.stop(queue_id)
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
        finally:
            await ma.close()

        player = await self.get_player(request.addon_player_id or ma_player_id)
        return {"ok": True, "player": player.model_dump()}


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