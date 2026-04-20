"""Encode and decode the opaque token carried in AudioPlayer stream items.

Format:  ma:<queue_id>:<queue_item_id>

This token is sent to Alexa with each stream URL and echoed back in
AudioPlayer lifecycle events, allowing us to correlate events back to
specific Music Assistant queue items without storing state per-device.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_PREFIX = "ma:"
_SEP = ":"


@dataclass(frozen=True)
class TokenParts:
    queue_id: str
    queue_item_id: str


def encode_token(queue_id: str, queue_item_id: str) -> str:
    """Return a token string for the given queue and item identifiers."""
    return f"{_PREFIX}{queue_id}{_SEP}{queue_item_id}"


def decode_token(token: str) -> Optional[TokenParts]:
    """Parse a token string back into its parts.

    Returns None for any token that is not in the expected format.
    This happens when Alexa echoes a token from a different skill or an
    old session — treat it gracefully rather than crashing.
    """
    if not token or not token.startswith(_PREFIX):
        return None
    rest = token[len(_PREFIX):]
    # The queue_item_id may itself contain colons; split on first colon only
    parts = rest.split(_SEP, 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        logger.debug("Could not parse token: %s", token)
        return None
    return TokenParts(queue_id=parts[0], queue_item_id=parts[1])
