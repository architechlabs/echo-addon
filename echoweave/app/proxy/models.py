"""Pydantic models for the local proxy agent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProxyCommandRequest(BaseModel):
    command: Literal[
        "play",
        "pause",
        "stop",
        "next",
        "previous",
        "volume_set",
        "play_query",
        "play_media",
        "refresh",
    ]
    addon_player_id: str | None = None
    query: str | None = None
    media_id: str | None = None
    media_type: str | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    request_id: str | None = None


class ProxyPlayerSnapshot(BaseModel):
    addon_player_id: str
    ma_player_id: str
    name: str
    available: bool
    state: str
    powered: bool | None = None
    volume_level: float | None = None  # 0.0–1.0 HA scale
    active_queue_id: str | None = None
    queue_state: str | None = None
    current_index: int | None = None
    source: str | None = None
    current_item: dict[str, Any] | None = None
    current_media: dict[str, Any] | None = None


class ProxySnapshot(BaseModel):
    instance_id: str
    player_prefix: str
    players: list[ProxyPlayerSnapshot]
