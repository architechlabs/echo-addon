"""HTTP and websocket routes for the local proxy agent."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.ma.client import MusicAssistantAuthError
from app.proxy.models import ProxyCommandRequest
from app.proxy.service import BackendWebSocketBridge, LocalProxyService, websocket_status_session

router = APIRouter(prefix="/proxy", tags=["proxy"])
logger = logging.getLogger(__name__)


def _proxy_service(request: Request) -> LocalProxyService:
    service = getattr(request.app.state, "local_proxy_service", None)
    if service is None:
        raise RuntimeError("Local proxy service is not initialised")
    return service


def _bridge(request: Request) -> BackendWebSocketBridge:
    bridge = getattr(request.app.state, "backend_ws_bridge", None)
    if bridge is None:
        raise RuntimeError("Backend websocket bridge is not initialised")
    return bridge


@router.get("/health")
async def proxy_health(request: Request) -> dict[str, Any]:
    proxy = _proxy_service(request)
    bridge = _bridge(request)
    return {
        "enabled": proxy.enabled,
        "instance_id": proxy.instance_id,
        "player_prefix": proxy.player_prefix,
        "backend": bridge.status(),
    }


@router.get("/players")
async def proxy_players(request: Request) -> dict[str, Any]:
    proxy = _proxy_service(request)
    try:
        snapshot = await proxy.get_snapshot()
    except MusicAssistantAuthError as exc:
        raise HTTPException(
            status_code=503,
            detail="Music Assistant authentication failed. Set local_ma_token in addon config.",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot.model_dump()


@router.get("/player")
async def proxy_primary_player(request: Request) -> dict[str, Any]:
    """Return the single primary proxy player selected by the addon."""
    proxy = _proxy_service(request)
    try:
        player = await proxy.get_primary_player()
    except MusicAssistantAuthError as exc:
        raise HTTPException(
            status_code=503,
            detail="Music Assistant authentication failed. Set local_ma_token in addon config.",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return player.model_dump()


@router.get("/players/{addon_player_id}")
async def proxy_player(addon_player_id: str, request: Request) -> dict[str, Any]:
    proxy = _proxy_service(request)
    try:
        player = await proxy.get_player(addon_player_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return player.model_dump()


@router.get("/debug/ma-players")
async def debug_ma_players(request: Request) -> dict[str, Any]:
    """Dump raw MA player data for debugging. Shows all fields including device_info, supported_features."""
    proxy = _proxy_service(request)
    ma = proxy._new_client()
    try:
        players = await ma.get_players()
    finally:
        await ma.close()
    return {"players": players}


@router.post("/command")
async def proxy_command(payload: ProxyCommandRequest, request: Request) -> dict[str, Any]:
    proxy = _proxy_service(request)
    try:
        return await proxy.execute(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected proxy command error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.websocket("/ws")
async def proxy_ws(websocket: WebSocket) -> None:
    proxy = getattr(websocket.app.state, "local_proxy_service", None)
    if proxy is None:
        await websocket.close(code=1011, reason="Local proxy service is not initialised")
        return

    await websocket.accept()

    async def send_json(payload: dict[str, Any]) -> None:
        await websocket.send_json(payload)

    async def receive_json() -> dict[str, Any]:
        return await websocket.receive_json()

    try:
        await websocket_status_session(
            proxy=proxy,
            send_json=send_json,
            receive_json=receive_json,
            poll_interval=max(1, int(getattr(websocket.app.state, "proxy_poll_interval", 3))),
        )
    except WebSocketDisconnect:
        return