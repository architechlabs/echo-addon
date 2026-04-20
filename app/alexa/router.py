"""Alexa skill webhook — the single endpoint Amazon calls for all events.

Every request from the Alexa service arrives here as a POST.
The handler:
  1. Reads the raw body (needed for signature verification)
  2. Validates JSON structure
  3. Checks timestamp freshness
  4. Verifies RSA-SHA1 signature
  5. Optionally validates the Alexa Skill ID
  6. Extracts the user's access token (context.System.user.accessToken)
     — issued during account linking, stored in our SQLite DB
  7. Looks up the user's MA config (URL + token + player ID) from the DB
  8. Creates a per-request MusicAssistantClient for that user
  9. Routes to the appropriate handler
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.alexa.intents import handle_intent, handle_playback_controller
from app.alexa.playback_events import handle_playback_event
from app.alexa.response_builder import build_error_response, build_response
from app.alexa.validators import (
    extract_device_id,
    extract_skill_id,
    extract_user_id,
    validate_alexa_request,
    verify_alexa_signature,
    verify_alexa_timestamp,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alexa", tags=["alexa"])


@router.post("", summary="Alexa skill webhook")
@router.post("/", include_in_schema=False)
async def alexa_webhook(request: Request) -> JSONResponse:
    # ── 1. Read raw bytes (needed for signature verification) ─────────────
    raw_body = await request.body()

    # ── 2. Parse JSON ─────────────────────────────────────────────────────
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("Failed to parse Alexa request body as JSON")
        return JSONResponse(content=build_error_response("Invalid request body"), status_code=400)

    request_type = body.get("request", {}).get("type", "")
    intent_name = body.get("request", {}).get("intent", {}).get("name", "")
    device_id = extract_device_id(body)
    user_id = extract_user_id(body)

    logger.info(
        "Alexa request: type=%s intent=%s device=%s user=%s",
        request_type, intent_name or "<none>", device_id, user_id,
    )

    # ── 3. Structure validation ───────────────────────────────────────────
    err = validate_alexa_request(body)
    if err:
        logger.warning("Structure validation failed: %s  device=%s", err, device_id)
        return JSONResponse(content=build_error_response(err), status_code=400)

    # ── 4. Timestamp check ────────────────────────────────────────────────
    if not verify_alexa_timestamp(body):
        logger.warning("Timestamp check failed device=%s", device_id)
        return JSONResponse(
            content=build_error_response("Request timestamp too old"), status_code=400
        )

    # ── 5. Signature verification ─────────────────────────────────────────
    from app.main import get_settings

    settings = get_settings()
    validation_mode = settings.alexa_validation

    if validation_mode != "off":
        sig_ok = await verify_alexa_signature(request, raw_body)
        if not sig_ok:
            if validation_mode == "enforce":
                logger.warning("Signature failed — rejecting  device=%s", device_id)
                return JSONResponse(
                    content=build_error_response("Invalid request signature"), status_code=400
                )
            logger.warning("Signature failed — allowing (log mode)  device=%s", device_id)

    # ── 6. Skill ID check (optional) ──────────────────────────────────────
    if settings.alexa_skill_id:
        incoming = extract_skill_id(body)
        if incoming and incoming != settings.alexa_skill_id:
            logger.warning(
                "Skill ID mismatch: got=%s expected=%s device=%s",
                incoming, settings.alexa_skill_id, device_id,
            )
            return JSONResponse(
                content=build_error_response("Unauthorized skill"), status_code=403
            )

    # ── 7. Resolve per-user MA config via access token ────────────────────
    access_token: str = (
        body.get("context", {})
        .get("System", {})
        .get("user", {})
        .get("accessToken", "")
        or ""
    )

    if not access_token:
        logger.warning("No access token in request — user has not linked account  device=%s", device_id)
        return JSONResponse(
            content=build_response(
                speech=(
                    "Your account is not linked. "
                    "Please open the Alexa app, go to Echo Bridge skill settings, "
                    "and tap Link Account to connect your Music Assistant."
                ),
                should_end_session=True,
            )
        )

    from app.storage.db import get_user_config

    user_config = await get_user_config(settings.db_path, access_token)

    if not user_config:
        logger.warning(
            "Access token not found in DB — user needs to re-link  device=%s token=%.8s...",
            device_id, access_token,
        )
        return JSONResponse(
            content=build_response(
                speech=(
                    "Your account link has expired or was revoked. "
                    "Please open the Alexa app and re-link Echo Bridge."
                ),
                should_end_session=True,
            )
        )

    logger.info(
        "User config loaded: ma_url=%s player_id=%s  device=%s",
        user_config["ma_url"], user_config.get("player_id", ""), device_id,
    )

    # ── 8. Create per-request MA client ───────────────────────────────────
    from app.ma.client import MusicAssistantClient

    ma = MusicAssistantClient(user_config["ma_url"], user_config["ma_token"])

    # ── 9. Route by request type ──────────────────────────────────────────
    try:
        if request_type == "LaunchRequest":
            response = build_response(
                speech=(
                    "Music Assistant is ready. "
                    "Say play to start music, or give me a song, artist, or album name."
                ),
                reprompt="Try saying: play jazz, or play Bohemian Rhapsody.",
                should_end_session=False,
            )
            logger.info("LaunchRequest handled device=%s", device_id)
            return JSONResponse(content=response)

        elif request_type == "IntentRequest":
            result = await handle_intent(body, ma, settings, user_config)
            logger.info("IntentRequest handled intent=%s device=%s", intent_name, device_id)
            return JSONResponse(content=result)

        elif request_type.startswith("AudioPlayer."):
            result = await handle_playback_event(body)
            logger.info("AudioPlayer event: type=%s device=%s", request_type, device_id)
            return JSONResponse(content=result)

        elif request_type.startswith("PlaybackController."):
            result = await handle_playback_controller(body, ma, settings, user_config)
            logger.info("PlaybackController: type=%s device=%s", request_type, device_id)
            return JSONResponse(content=result)

        elif request_type == "SessionEndedRequest":
            reason = body.get("request", {}).get("reason", "USER_INITIATED")
            logger.info("SessionEnded reason=%s device=%s", reason, device_id)
            return JSONResponse(content={"version": "1.0", "response": {}})

        else:
            logger.warning("Unknown Alexa request type: %s  device=%s", request_type, device_id)
            return JSONResponse(
                content=build_error_response(f"Unknown request type: {request_type}"),
                status_code=400,
            )

    except Exception:
        logger.exception(
            "Error handling Alexa request type=%s intent=%s device=%s",
            request_type, intent_name, device_id,
        )
        return JSONResponse(
            content=build_response(
                speech="Something went wrong on our end. Please try again in a moment.",
                should_end_session=True,
            )
        )

    finally:
        await ma.close()

