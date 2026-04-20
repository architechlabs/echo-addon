"""Pydantic models for Music Assistant API responses.

These models are intentionally permissive (extra fields allowed) so
that new MA versions do not break existing parsing.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class MAServerInfo(BaseModel):
    server_id: str = ""
    server_version: str = ""
    schema_version: int = 0
    model_config = {"extra": "allow"}


class MAQueueItem(BaseModel):
    queue_item_id: str = Field("", alias="queue_item_id")
    queue_id: str = ""
    name: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    uri: str = ""
    image_url: str = ""
    model_config = {"extra": "allow", "populate_by_name": True}


class MAPlayer(BaseModel):
    player_id: str = ""
    name: str = ""
    available: bool = False
    powered: bool = False
    state: str = ""
    active_queue: str = ""
    volume_level: Optional[int] = None
    model_config = {"extra": "allow"}
