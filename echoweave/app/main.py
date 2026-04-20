"""FastAPI application entry point.

Startup sequence:
  1. Load settings from env / .env
  2. Configure logging
  3. Initialise the SQLite database (create tables if needed)
  4. Register all routes

There is no longer a global Music Assistant client. Each Alexa request
looks up the user's MA config from the DB (keyed by their access token)
and creates a short-lived MusicAssistantClient for that request only.
This supports an unlimited number of users, each with their own MA instance.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.logging_config import setup_logging
from app.proxy.service import BackendWebSocketBridge, LocalProxyService
from app.settings import Settings

logger = logging.getLogger(__name__)

_settings: Settings | None = None


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Settings not initialised — startup incomplete")
    return _settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings

    _settings = Settings()
    setup_logging(_settings.log_level)

    logger.info("Echo Bridge starting up")
    logger.info("Public URL: %s", _settings.public_url or "<not set>")
    logger.info("Validation: %s", _settings.alexa_validation)

    if not _settings.public_url:
        logger.error("ECHO_PUBLIC_URL is not set — stream URLs for Alexa will be broken")
    if not _settings.oauth_client_secret:
        logger.error("ECHO_OAUTH_CLIENT_SECRET is not set — account linking will fail")

    from app.storage.db import init_db

    await init_db(_settings.db_path)
    logger.info("Database ready: %s", _settings.db_path)

    app.state.local_proxy_service = LocalProxyService(_settings)
    app.state.backend_ws_bridge = BackendWebSocketBridge(_settings, app.state.local_proxy_service)
    app.state.proxy_poll_interval = _settings.proxy_status_poll_interval

    if _settings.local_proxy_enabled:
        logger.info(
            "Local proxy enabled: MA=%s instance_id=%s prefix=%s",
            _settings.local_ma_url,
            _settings.backend_instance_id,
            _settings.proxy_player_prefix,
        )
    else:
        logger.info("Local proxy disabled: local_ma_url not configured (set manually or enable auto_discover_ma)")

    if _settings.backend_ws_enabled:
        logger.info("Backend websocket sync enabled: %s", _settings.backend_ws_url)
        await app.state.backend_ws_bridge.start()
    elif _settings.backend_ws_url:
        logger.warning(
            "Backend websocket URL is set but local proxy is not fully configured; bridge will stay disabled"
        )

    # ── Push home config to worker so account linking needs no manual input ──
    if _settings.worker_secret and _settings.local_ma_url and _settings.local_ma_token:
        import asyncio
        asyncio.create_task(_register_with_worker(_settings))
    else:
        logger.info(
            "Worker registration skipped — set worker_secret (and ensure local_ma_url/local_ma_token are set) to enable auto player-picker on account linking"
        )

    yield

    await app.state.backend_ws_bridge.stop()

    logger.info("Echo Bridge shutting down")


async def _register_with_worker(settings) -> None:
    """Push MA config + player list to the Cloudflare worker.

    Called once on startup (fire-and-forget). Failures are logged but not fatal.
    This lets the worker's account-linking form show a player dropdown without
    the user ever having to type a URL or token.
    """
    import httpx
    from app.ma.client import MusicAssistantClient

    try:
        client = MusicAssistantClient(settings.local_ma_url, settings.local_ma_token)
        raw_players = await client.get_players()
        players = [
            {
                "player_id": p.get("player_id", ""),
                "name": p.get("display_name") or p.get("name") or p.get("player_id", ""),
            }
            for p in raw_players
            if p.get("player_id")
        ]
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                f"{settings.worker_url.rstrip('/')}/api/home/register",
                json={
                    "ma_url": settings.local_ma_url,
                    "ma_token": settings.local_ma_token,
                    "players": players,
                },
                headers={"X-Addon-Secret": settings.worker_secret},
            )
        if resp.status_code == 200:
            logger.info(
                "Registered with worker at %s — %d players pushed",
                settings.worker_url,
                len(players),
            )
        else:
            logger.warning(
                "Worker registration failed: HTTP %s — %s", resp.status_code, resp.text[:200]
            )
    except Exception as exc:
        logger.warning("Worker registration failed: %s", exc)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Echo Bridge",
        description="Alexa ↔ Music Assistant integration",
        version="3.0.0",
        lifespan=lifespan,
    )

    # ── Health ────────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": "echo-bridge"}

    # ── Debug: list MA players for a specific linked user ─────────────────
    @app.get("/debug/players", tags=["system"])
    async def debug_players(token: str = Query(..., description="User access token")):
        from app.ma.client import MusicAssistantClient
        from app.storage.db import get_user_config

        settings = get_settings()
        cfg = await get_user_config(settings.db_path, token)
        if not cfg:
            return JSONResponse(
                status_code=404,
                content={"error": "Token not found. Link your account first."},
            )
        ma = MusicAssistantClient(cfg["ma_url"], cfg["ma_token"])
        try:
            players = await ma.get_players()
            return {
                "ma_url": cfg["ma_url"],
                "count": len(players),
                "players": [
                    {
                        "player_id": p.get("player_id"),
                        "name": p.get("name"),
                        "state": p.get("state"),
                        "available": p.get("available"),
                        "active_queue": p.get("active_queue"),
                    }
                    for p in players
                ],
            }
        finally:
            await ma.close()

    # ── Debug: list all linked accounts (no secrets exposed) ──────────────
    @app.get("/debug/users", tags=["system"])
    async def debug_users():
        from app.storage.db import list_user_configs

        settings = get_settings()
        configs = await list_user_configs(settings.db_path)
        return {"count": len(configs), "users": configs}

    # ── Global error handler ──────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception %s %s", request.method, request.url)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    # ── Routers ───────────────────────────────────────────────────────────
    from app.alexa.router import router as alexa_router
    from app.auth.router import router as auth_router
    from app.ma.stream_proxy import router as stream_router
    from app.proxy.router import router as proxy_router

    app.include_router(auth_router)
    app.include_router(alexa_router)
    app.include_router(stream_router)
    app.include_router(proxy_router)

    return app


app = create_app()

