"""Stream proxy router.

Alexa requires publicly-accessible HTTPS audio URLs. Music Assistant
runs on the local network. This router bridges the two by:

  GET /stream/{access_token}/{queue_id}/{item_id}

  - access_token: the user's Echo Bridge access token (issued during account
                  linking). Used to look up the user's MA URL + token from the DB.
  - queue_id / item_id: identify the specific track in the MA queue.

The handler:
  1. Looks up the user's MA config from SQLite
  2. Resolves the internal MA stream URL via MusicAssistantClient.get_stream_url()
  3. Opens an httpx streaming request to MA
  4. Forwards bytes to Alexa chunk-by-chunk (no buffering)

Range header is forwarded when present so Alexa can resume from an offset.
Content-Type is audio/flac (MA returns .flac by default).
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

_CHUNK_SIZE = 32 * 1024  # 32 KB
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


@router.get(
    "/{access_token}/{queue_id}/{item_id}",
    summary="Proxy MA audio stream to Alexa",
)
async def stream_audio(
    access_token: str,
    queue_id: str,
    item_id: str,
    request: Request,
) -> StreamingResponse:
    from app.main import get_settings
    from app.storage.db import get_user_config

    settings = get_settings()

    # ── Resolve user config from access token ─────────────────────────────
    user_config = await get_user_config(settings.db_path, access_token)
    if not user_config:
        logger.warning(
            "Stream request with unknown token: %.8s...  queue=%s item=%s",
            access_token, queue_id, item_id,
        )
        raise HTTPException(status_code=401, detail="Invalid stream token")

    # ── Resolve internal MA stream URL ────────────────────────────────────
    from app.ma.client import MusicAssistantClient

    ma = MusicAssistantClient(user_config["ma_url"], user_config["ma_token"])
    try:
        ma_url = await ma.get_stream_url(queue_id, item_id)
    except Exception as exc:
        logger.error(
            "Could not resolve stream URL: queue=%s item=%s  error=%s",
            queue_id, item_id, exc,
        )
        raise HTTPException(status_code=503, detail="Could not resolve audio stream") from exc
    finally:
        await ma.close()

    # ── Forward Range header for seek / resume support ────────────────────
    forward_headers: dict[str, str] = {
        "Authorization": f"Bearer {user_config['ma_token']}"
    }
    range_header = request.headers.get("Range")
    if range_header:
        forward_headers["Range"] = range_header
        logger.debug(
            "Forwarding Range header: %s  queue=%s item=%s", range_header, queue_id, item_id
        )

    logger.info("Streaming: queue=%s item=%s  url=%s", queue_id, item_id, ma_url)

    async def _generator() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
            async with client.stream("GET", ma_url, headers=forward_headers) as resp:
                if resp.status_code not in (200, 206):
                    logger.warning(
                        "MA stream returned %d: queue=%s item=%s",
                        resp.status_code, queue_id, item_id,
                    )
                async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                    yield chunk

    return StreamingResponse(
        _generator(),
        media_type="audio/flac",
        headers={"Accept-Ranges": "bytes"},
    )

