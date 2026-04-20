"""Per-device playback session store.

Tracks which MA queue each Alexa device is playing from, and the last
known playback state. This is pure in-memory state — it resets on restart,
which is acceptable because Alexa's own AudioPlayer context carries the
token and offset when a session resumes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class PlayState(str, Enum):
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    IDLE = "IDLE"


@dataclass
class DeviceSession:
    device_id: str
    queue_id: str = ""
    player_id: str = ""
    current_token: str = ""
    play_state: PlayState = PlayState.IDLE
    offset_ms: int = 0
    last_event: str = ""


class SessionStore:
    """Thread-safe in-memory store of per-device playback sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}
        self._lock = threading.Lock()

    def get(self, device_id: str) -> Optional[DeviceSession]:
        with self._lock:
            return self._sessions.get(device_id)

    def update(
        self,
        device_id: str,
        *,
        queue_id: Optional[str] = None,
        player_id: Optional[str] = None,
        current_token: Optional[str] = None,
        play_state: Optional[PlayState] = None,
        offset_ms: Optional[int] = None,
        last_event: Optional[str] = None,
    ) -> DeviceSession:
        with self._lock:
            session = self._sessions.get(device_id)
            if session is None:
                session = DeviceSession(device_id=device_id)
                self._sessions[device_id] = session
            if queue_id is not None:
                session.queue_id = queue_id
            if player_id is not None:
                session.player_id = player_id
            if current_token is not None:
                session.current_token = current_token
            if play_state is not None:
                session.play_state = play_state
            if offset_ms is not None:
                session.offset_ms = offset_ms
            if last_event is not None:
                session.last_event = last_event
            logger.debug(
                "Session updated: device=%s queue=%s state=%s token=%s",
                device_id,
                session.queue_id,
                session.play_state,
                session.current_token[:30] if session.current_token else "",
            )
            return session


# Module-level singleton used by routers and intent handlers
_store = SessionStore()


def get_session_store() -> SessionStore:
    return _store
