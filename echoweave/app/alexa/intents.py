"""Alexa intent handlers.

Each handler receives the full Alexa request body, a MusicAssistantClient,
and the application Settings, then returns an Alexa response dict.

Supported intents
-----------------
PlayIntent           — play what's currently queued in MA
PlayAudio            — search MA and play the result
AMAZON.PauseIntent   — pause on both Alexa and MA
AMAZON.ResumeIntent  — resume from saved position
AMAZON.StopIntent    — stop and clear
AMAZON.CancelIntent  — same as stop
AMAZON.NextIntent    — next track
AMAZON.PreviousIntent— previous track
VolumeIntent         — set volume (1-100)
AMAZON.HelpIntent    — usage hint
"""

from __future__ import annotations

import logging
from typing import Any

from app.alexa.directives import clear_queue_directive, play_directive, stop_directive
from app.alexa.response_builder import build_response
from app.alexa.session_store import PlayState, get_session_store
from app.alexa.token_mapper import decode_token, encode_token
from app.alexa.validators import extract_device_id

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _extract_query(body: dict[str, Any]) -> str:
    """Pull a free-text search query from the intent slots."""
    slots = body.get("request", {}).get("intent", {}).get("slots", {})
    for name in ("query", "searchQuery", "SearchQuery", "musicQuery", "phrase"):
        val = slots.get(name, {}).get("value", "")
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_volume(body: dict[str, Any]) -> int | None:
    """Extract a volume level (1-100) from intent slots."""
    slots = body.get("request", {}).get("intent", {}).get("slots", {})
    for name in ("volume", "Volume", "volumeLevel"):
        raw = slots.get(name, {}).get("value", "")
        if raw:
            try:
                vol = int(float(raw))
                return max(1, min(100, vol))
            except (ValueError, TypeError):
                pass
    return None


def _extract_player_name(body: dict[str, Any]) -> str:
    """Extract a player/room name from intent slots."""
    slots = body.get("request", {}).get("intent", {}).get("slots", {})
    for name in ("player", "room", "speaker", "device"):
        val = slots.get(name, {}).get("value", "")
        if val and isinstance(val, str) and val.strip():
            return val.strip().lower()
    return ""


def _get_audio_context(body: dict[str, Any]) -> dict[str, Any]:
    return body.get("context", {}).get("AudioPlayer", {})


def _queue_id_from_session(body: dict[str, Any]) -> str:
    """Try to recover queue_id from device session or AudioPlayer token."""
    store = get_session_store()
    device_id = extract_device_id(body)
    session = store.get(device_id)
    if session and session.queue_id:
        return session.queue_id
    # Fall back to the token in the AudioPlayer context
    token = _get_audio_context(body).get("token", "")
    if token:
        parts = decode_token(token)
        if parts:
            return parts.queue_id
    return ""


def _build_public_stream_url(
    access_token: str, queue_id: str, item_id: str, public_url: str
) -> str:
    """Construct the public HTTPS URL that Alexa will fetch for audio.

    The access_token is embedded in the path so the stream proxy can look up
    the user's MA URL/token without any Alexa context headers.
    """
    base = public_url.rstrip("/")
    return f"{base}/stream/{access_token}/{queue_id}/{item_id}"


# ─── Dispatch ────────────────────────────────────────────────────────────────


async def handle_intent(
    body: dict[str, Any],
    ma_client: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Route an IntentRequest to the matching handler."""
    intent_name = body.get("request", {}).get("intent", {}).get("name", "")
    device_id = extract_device_id(body)
    logger.info("Intent: %s  device: %s", intent_name, device_id)

    handler = _INTENT_MAP.get(intent_name, _handle_unknown)
    return await handler(body, ma_client, settings, user_config)


# ─── Individual handlers ──────────────────────────────────────────────────────


async def _handle_play(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """PlayIntent — resume / start the current MA queue on the selected player."""
    device_id = extract_device_id(body)
    store = get_session_store()

    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")

    try:
        if not queue_id:
            queue_id = await ma.get_active_queue_id()

        if not queue_id:
            logger.warning("PlayIntent: no queue_id available device=%s", device_id)
            return build_response(
                speech="I couldn't find a Music Assistant player. "
                       "Make sure a player is set up in Music Assistant.",
            )

        item = await ma.get_current_queue_item(queue_id)
        if item is None:
            logger.warning("PlayIntent: queue empty queue_id=%s device=%s", queue_id, device_id)
            return build_response(
                speech="The Music Assistant queue is empty. "
                       "Try saying: play jazz, or play something by Radiohead.",
            )

        await ma.play(queue_id)

        name = item.get("name", "")
        speech = f"Playing {name} on Music Assistant." if name else "Playing on Music Assistant."

        logger.info(
            "PlayIntent: queue=%s item=%s name=%s device=%s",
            queue_id, item.get("queue_item_id", "?"), name, device_id,
        )

        store.update(
            device_id,
            queue_id=queue_id,
            play_state=PlayState.PLAYING,
            last_event="PlayIntent",
        )

        return build_response(speech=speech)

    except Exception:
        logger.exception("PlayIntent failed device=%s", device_id)
        return build_response(speech="Sorry, I couldn't start playback right now.")


async def _handle_play_audio(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """PlayAudio — search MA for the query and play the top result on the MA player."""
    device_id = extract_device_id(body)
    query = _extract_query(body)
    store = get_session_store()

    if not query:
        return await _handle_play(body, ma, settings, user_config)

    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")
    if not queue_id:
        queue_id = await ma.get_active_queue_id()

    if not queue_id:
        return build_response(
            speech="I couldn't find a Music Assistant player to play on. "
                   "Please re-link your account and select a player."
        )

    logger.info("PlayAudio: query=%r queue=%s device=%s", query, queue_id, device_id)

    try:
        result = await ma.search_and_play(query=query, queue_id=queue_id)
        if result is None:
            logger.warning("PlayAudio: no results for query=%r", query)
            return build_response(
                speech=f"I couldn't find anything matching '{query}' in Music Assistant.",
            )

        name = result.get("name", "")
        artist = result.get("artist", "")
        if name and artist:
            speech = f"Playing {name} by {artist} on Music Assistant."
        elif name:
            speech = f"Playing {name} on Music Assistant."
        else:
            speech = f"Playing {query} on Music Assistant."

        logger.info(
            "PlayAudio: playing queue=%s name=%s artist=%s device=%s",
            result["queue_id"], name, artist, device_id,
        )

        store.update(
            device_id,
            queue_id=result["queue_id"],
            play_state=PlayState.PLAYING,
            last_event="PlayAudio",
        )

        return build_response(speech=speech)

    except Exception:
        logger.exception("PlayAudio failed query=%r device=%s", query, device_id)
        return build_response(speech="Sorry, I couldn't find or play that right now.")


async def _handle_pause(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """PauseIntent — pause on Alexa and sync the pause to MA."""
    device_id = extract_device_id(body)
    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")
    store = get_session_store()

    logger.info("PauseIntent: queue=%s device=%s", queue_id, device_id)

    try:
        if queue_id:
            await ma.pause(queue_id)
            logger.info("PauseIntent: MA paused queue=%s", queue_id)
        else:
            # No known queue — try to pause any active player
            players = await ma.get_players()
            for p in players:
                pid = p.get("player_id", "")
                state = str(p.get("state", "")).lower()
                if pid and state == "playing":
                    await ma.pause_player(pid)
                    logger.info("PauseIntent: paused player=%s (no queue_id)", pid)
                    break
    except Exception:
        logger.warning("PauseIntent: failed to sync pause to MA", exc_info=True)

    store.update(device_id, play_state=PlayState.PAUSED, last_event="PauseIntent")
    return build_response(directives=[stop_directive()])


async def _handle_resume(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """ResumeIntent — resume playback on the MA player."""
    device_id = extract_device_id(body)
    store = get_session_store()

    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")
    if not queue_id:
        queue_id = await ma.get_active_queue_id()

    logger.info("ResumeIntent: queue=%s device=%s", queue_id, device_id)

    if not queue_id:
        return build_response(speech="There's nothing to resume. Say play to start music.")

    try:
        await ma.play(queue_id)
        logger.info("ResumeIntent: MA play called queue=%s", queue_id)

        store.update(
            device_id,
            queue_id=queue_id,
            play_state=PlayState.PLAYING,
            last_event="ResumeIntent",
        )

        return build_response(speech="Resuming Music Assistant.")

    except Exception:
        logger.exception("ResumeIntent failed device=%s", device_id)
        return build_response(speech="Sorry, I couldn't resume right now.")


async def _handle_stop(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """StopIntent / CancelIntent — stop playback on Alexa and MA."""
    device_id = extract_device_id(body)
    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")
    store = get_session_store()

    logger.info("StopIntent: queue=%s device=%s", queue_id, device_id)

    try:
        if queue_id:
            await ma.pause(queue_id)
    except Exception:
        logger.warning("StopIntent: failed to sync stop to MA", exc_info=True)

    store.update(device_id, play_state=PlayState.STOPPED, last_event="StopIntent")
    return build_response(directives=[stop_directive()])


async def _handle_next(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """NextIntent — skip to the next track."""
    device_id = extract_device_id(body)
    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")
    store = get_session_store()

    if not queue_id:
        queue_id = await ma.get_active_queue_id()

    logger.info("NextIntent: queue=%s device=%s", queue_id, device_id)

    if not queue_id:
        return build_response(speech="I don't know which player to skip on.")

    try:
        await ma.next_track(queue_id)
        logger.info("NextIntent: MA next sent queue=%s", queue_id)

        import asyncio
        await asyncio.sleep(0.4)
        item = await ma.get_current_queue_item(queue_id)
        name = item.get("name", "") if item else ""
        speech = f"Playing {name}." if name else "Playing next track."

        logger.info("NextIntent: next track=%s device=%s", name or "?", device_id)

        store.update(
            device_id,
            queue_id=queue_id,
            play_state=PlayState.PLAYING,
            last_event="NextIntent",
        )

        return build_response(speech=speech)

    except Exception:
        logger.exception("NextIntent failed device=%s", device_id)
        return build_response(speech="Sorry, I couldn't skip to the next track.")


async def _handle_previous(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """PreviousIntent — go back to the previous track."""
    device_id = extract_device_id(body)
    queue_id = _queue_id_from_session(body) or user_config.get("player_id", "")
    store = get_session_store()

    if not queue_id:
        queue_id = await ma.get_active_queue_id()

    logger.info("PreviousIntent: queue=%s device=%s", queue_id, device_id)

    if not queue_id:
        return build_response(speech="I don't know which player to go back on.")

    try:
        await ma.prev_track(queue_id)
        logger.info("PreviousIntent: MA previous sent queue=%s", queue_id)

        import asyncio
        await asyncio.sleep(0.4)
        item = await ma.get_current_queue_item(queue_id)
        name = item.get("name", "") if item else ""
        speech = f"Playing {name}." if name else "Playing previous track."

        logger.info("PreviousIntent: prev track=%s device=%s", name or "?", device_id)

        store.update(
            device_id,
            queue_id=queue_id,
            play_state=PlayState.PLAYING,
            last_event="PreviousIntent",
        )

        return build_response(speech=speech)

    except Exception:
        logger.exception("PreviousIntent failed device=%s", device_id)
        return build_response(speech="Sorry, I couldn't go to the previous track.")


async def _handle_volume(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """VolumeIntent — set volume on the MA player (1-100)."""
    device_id = extract_device_id(body)
    volume = _extract_volume(body)

    if volume is None:
        return build_response(
            speech="Please say a volume between 1 and 100. For example: set volume to 50.",
        )

    # Resolve player_id from session → user config → MA auto-select
    session = get_session_store().get(device_id)
    player_id = (session.player_id if session else "") or user_config.get("player_id", "")

    if not player_id:
        # Find the first available player
        try:
            players = await ma.get_players()
            for p in players:
                pid = p.get("player_id", "")
                if pid and p.get("available", False):
                    player_id = pid
                    break
        except Exception:
            logger.warning("VolumeIntent: could not list players", exc_info=True)

    if not player_id:
        return build_response(speech="I couldn't find a Music Assistant player to set the volume on.")

    logger.info("VolumeIntent: player=%s volume=%d device=%s", player_id, volume, device_id)

    try:
        await ma.set_volume(player_id, volume)
        return build_response(speech=f"Volume set to {volume}.")
    except Exception:
        logger.exception("VolumeIntent failed player=%s", player_id)
        return build_response(speech="Sorry, I couldn't change the volume.")


async def _handle_help(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    return build_response(
        speech=(
            "You can say: play, pause, resume, stop, next, previous, "
            "or play followed by an artist, album, or song name. "
            "You can also say set volume to a number between 1 and 100."
        ),
        should_end_session=False,
    )


async def _handle_unknown(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    intent = body.get("request", {}).get("intent", {}).get("name", "unknown")
    logger.warning("Unhandled intent: %s", intent)
    return build_response(
        speech="Sorry, I don't know how to do that. Try saying play, pause, or next.",
    )


# ─── PlaybackController (physical button events on Alexa devices) ─────────────


async def handle_playback_controller(
    body: dict[str, Any],
    ma: Any,
    settings: Any,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Handle PlaybackController.* button events from Alexa devices.

    These fire when the user presses physical buttons (play/pause/next/prev)
    on an Echo device. No session is attached; responses contain only directives.
    """
    request_type = body.get("request", {}).get("type", "")
    device_id = extract_device_id(body)
    logger.info("PlaybackController: type=%s device=%s", request_type, device_id)

    if request_type == "PlaybackController.PlayCommandIssued":
        return await _handle_resume(body, ma, settings, user_config)
    elif request_type == "PlaybackController.PauseCommandIssued":
        return await _handle_pause(body, ma, settings, user_config)
    elif request_type == "PlaybackController.NextCommandIssued":
        return await _handle_next(body, ma, settings, user_config)
    elif request_type == "PlaybackController.PreviousCommandIssued":
        return await _handle_previous(body, ma, settings, user_config)

    logger.warning("Unhandled PlaybackController type: %s", request_type)
    return {"version": "1.0", "response": {}}


# ─── Intent dispatch map ──────────────────────────────────────────────────────

_INTENT_MAP = {
    "PlayIntent": _handle_play,
    "PlayAudio": _handle_play_audio,
    "AMAZON.PauseIntent": _handle_pause,
    "AMAZON.ResumeIntent": _handle_resume,
    "AMAZON.StopIntent": _handle_stop,
    "AMAZON.CancelIntent": _handle_stop,
    "AMAZON.NextIntent": _handle_next,
    "AMAZON.PreviousIntent": _handle_previous,
    "VolumeIntent": _handle_volume,
    "AMAZON.HelpIntent": _handle_help,
}
