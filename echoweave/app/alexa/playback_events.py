"""AudioPlayer lifecycle event handlers.

Alexa sends these events as the device plays audio:
  - AudioPlayer.PlaybackStarted
  - AudioPlayer.PlaybackStopped
  - AudioPlayer.PlaybackFinished
  - AudioPlayer.PlaybackNearlyFinished
  - AudioPlayer.PlaybackFailed

These are fire-and-forget from Alexa's perspective — the response body
can be empty. We use them to keep our session store in sync.
"""

from __future__ import annotations

import logging
from typing import Any

from app.alexa.session_store import PlayState, get_session_store
from app.alexa.token_mapper import decode_token
from app.alexa.validators import extract_device_id

logger = logging.getLogger(__name__)

_EMPTY_RESPONSE: dict[str, Any] = {"version": "1.0", "response": {}}


async def handle_playback_event(body: dict[str, Any]) -> dict[str, Any]:
    """Process an AudioPlayer.* event and update session state."""
    request_type = body.get("request", {}).get("type", "")
    device_id = extract_device_id(body)
    token = body.get("request", {}).get("token", "")
    offset_ms = int(body.get("request", {}).get("offsetInMilliseconds", 0))

    logger.info(
        "AudioPlayer event: type=%s device=%s token=%s offset=%d",
        request_type, device_id, token[:40] if token else "", offset_ms,
    )

    store = get_session_store()
    parts = decode_token(token) if token else None

    if request_type == "AudioPlayer.PlaybackStarted":
        updates: dict[str, Any] = {
            "play_state": PlayState.PLAYING,
            "offset_ms": offset_ms,
            "last_event": "PlaybackStarted",
        }
        if parts:
            updates["queue_id"] = parts.queue_id
            updates["current_token"] = token
        store.update(device_id, **updates)

    elif request_type == "AudioPlayer.PlaybackStopped":
        store.update(
            device_id,
            play_state=PlayState.PAUSED,
            offset_ms=offset_ms,
            last_event="PlaybackStopped",
        )

    elif request_type == "AudioPlayer.PlaybackFinished":
        store.update(
            device_id,
            play_state=PlayState.IDLE,
            offset_ms=0,
            last_event="PlaybackFinished",
        )

    elif request_type == "AudioPlayer.PlaybackNearlyFinished":
        # Could enqueue the next track here (ENQUEUE behavior) if needed.
        # For now we let the user issue the next command manually.
        logger.debug("PlaybackNearlyFinished — no action taken")

    elif request_type == "AudioPlayer.PlaybackFailed":
        error = body.get("request", {}).get("error", {})
        logger.warning(
            "Alexa PlaybackFailed: device=%s type=%s message=%s token=%s",
            device_id,
            error.get("type", "?"),
            error.get("message", "?"),
            token[:40] if token else "",
        )
        store.update(device_id, play_state=PlayState.IDLE, last_event="PlaybackFailed")

    return _EMPTY_RESPONSE
