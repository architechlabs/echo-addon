"""Standard Alexa response envelope builder.

All skill responses must follow the Alexa response format exactly.
Use build_response() for normal responses and build_error_response()
for failure cases.
"""

from __future__ import annotations

from typing import Any, Optional


def build_response(
    *,
    speech: str = "",
    reprompt: str = "",
    should_end_session: Optional[bool] = True,
    directives: Optional[list[dict[str, Any]]] = None,
    session_attributes: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a valid Alexa response envelope.

    Parameters
    ----------
    speech:
        Plain text spoken to the user. Omit for AudioPlayer-only responses.
    reprompt:
        Follow-up prompt if the session stays open.
    should_end_session:
        True to close the session, False to keep open, None to omit
        (required for AudioPlayer-only responses — Alexa rejects shouldEndSession
        when an AudioPlayer directive is present without a speech output).
    directives:
        List of AudioPlayer or interface directives.
    session_attributes:
        Key-value pairs persisted across turns of the same session.
    """
    response: dict[str, Any] = {}

    if speech:
        response["outputSpeech"] = {"type": "PlainText", "text": speech}

    if reprompt:
        response["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }

    if directives:
        response["directives"] = directives
        # Alexa requires shouldEndSession to be absent (not false) when an
        # AudioPlayer directive is the only content and there is no speech.
        if not speech and should_end_session is True:
            should_end_session = None

    if should_end_session is not None:
        response["shouldEndSession"] = should_end_session

    return {
        "version": "1.0",
        "sessionAttributes": session_attributes or {},
        "response": response,
    }


def build_error_response(message: str) -> dict[str, Any]:
    """Build a minimal response for error cases."""
    return build_response(speech=message, should_end_session=True)
