"""SQLite persistence for per-user Music Assistant configurations.

Schema
------
user_configs  — permanent per-user records, keyed by access_token (UUID).
auth_codes    — short-lived one-time codes issued during OAuth2 account-linking.
                Expire after 10 minutes and are deleted on first use.

Every user's MA URL, token, and optional player ID are stored here.
The access_token is opaque to Alexa — it is issued during account linking
and included in every subsequent Alexa request body under
context.System.user.accessToken.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS user_configs (
    token       TEXT PRIMARY KEY,
    ma_url      TEXT NOT NULL,
    ma_token    TEXT NOT NULL,
    player_id   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code         TEXT PRIMARY KEY,
    ma_url       TEXT NOT NULL,
    ma_token     TEXT NOT NULL,
    player_id    TEXT NOT NULL DEFAULT '',
    redirect_uri TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT '',
    expires_at   TEXT NOT NULL
);
"""


async def init_db(path: str) -> None:
    """Create tables if they do not exist."""
    import os
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_CREATE_TABLES)
        await db.commit()


async def create_auth_code(
    path: str,
    ma_url: str,
    ma_token: str,
    player_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Generate a short-lived auth code and persist the associated config."""
    code = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO auth_codes
               (code, ma_url, ma_token, player_id, redirect_uri, state, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (code, ma_url, ma_token, player_id, redirect_uri, state, expires_at),
        )
        await db.commit()
    return code


async def exchange_code_for_token(path: str, code: str) -> Optional[dict[str, Any]]:
    """Exchange a one-time auth code for a permanent access token.

    Returns {"access_token": str, "token_type": "bearer"} or None on failure.
    The code is deleted from the DB regardless of outcome (one-time use).
    """
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM auth_codes WHERE code = ?", (code,)) as cur:
            row = await cur.fetchone()

        # Always delete the code — one-time use regardless of outcome
        await db.execute("DELETE FROM auth_codes WHERE code = ?", (code,))

        if not row:
            await db.commit()
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            await db.commit()
            return None

        token = str(uuid.uuid4())
        await db.execute(
            """INSERT OR REPLACE INTO user_configs (token, ma_url, ma_token, player_id)
               VALUES (?, ?, ?, ?)""",
            (token, row["ma_url"], row["ma_token"], row["player_id"]),
        )
        await db.commit()

    return {"access_token": token, "token_type": "bearer"}


async def get_user_config(path: str, token: str) -> Optional[dict[str, Any]]:
    """Look up a user's MA config by access token. Returns None if not found."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_configs WHERE token = ?", (token,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return dict(row)


async def list_user_configs(path: str) -> list[dict[str, Any]]:
    """Return all user configs (tokens, ma_url, player_id — never ma_token)."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT token, ma_url, player_id, created_at, updated_at FROM user_configs"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
