"""AudioPlayer directive builders.

Alexa's AudioPlayer interface requires specific directive shapes.
These helpers build them correctly and consistently.
"""

from __future__ import annotations

from typing import Any, Literal


def play_directive(
    *,
    url: str,
    token: str,
    offset_ms: int = 0,
    behavior: Literal["REPLACE_ALL", "ENQUEUE", "REPLACE_ENQUEUED"] = "REPLACE_ALL",
    expected_previous_token: str = "",
) -> dict[str, Any]:
    """Build an AudioPlayer.Play directive.

    Parameters
    ----------
    url:
        Public HTTPS URL of the audio stream.
    token:
        Opaque string identifying this stream item. Echoed back in
        AudioPlayer events so we can correlate state.
    offset_ms:
        Resume position in milliseconds.
    behavior:
        REPLACE_ALL: replace queue and play immediately (default).
        ENQUEUE: add to end of queue.
        REPLACE_ENQUEUED: replace queued items but keep current.
    expected_previous_token:
        Required when behavior is ENQUEUE; must match the token of the
        currently-playing item.
    """
    directive: dict[str, Any] = {
        "type": "AudioPlayer.Play",
        "playBehavior": behavior,
        "audioItem": {
            "stream": {
                "url": url,
                "token": token,
                "offsetInMilliseconds": offset_ms,
            }
        },
    }
    if behavior == "ENQUEUE" and expected_previous_token:
        directive["audioItem"]["stream"]["expectedPreviousToken"] = expected_previous_token
    return directive


def stop_directive() -> dict[str, Any]:
    """Build an AudioPlayer.Stop directive (pause playback on the device)."""
    return {"type": "AudioPlayer.Stop"}


def clear_queue_directive(
    behavior: Literal["CLEAR_ALL", "CLEAR_ENQUEUED"] = "CLEAR_ALL",
) -> dict[str, Any]:
    """Build an AudioPlayer.ClearQueue directive.

    CLEAR_ALL: stop playback and clear queue.
    CLEAR_ENQUEUED: clear queued items but let current track finish.
    """
    return {"type": "AudioPlayer.ClearQueue", "clearBehavior": behavior}
