from __future__ import annotations

from typing import Optional

from fastapi import Header

from ..config import get_settings


def current_user(x_user_id: Optional[str] = Header(default=None)) -> str:
    """Identify the caller. Defaults to the demo user; a real deployment
    would validate JWT/OAuth2 here (per slide 5 — "Lớp bảo mật và an toàn")."""
    return x_user_id or get_settings().demo_user_id


def current_session(
    x_chat_session_id: Optional[str] = Header(default=None),
) -> Optional[str]:
    """Which durable conversation this request belongs to.

    The chat route echoes the active conversation back via the
    ``X-Chat-Session-Id`` response header; the frontend replays it on the
    draft-action endpoints (confirm / cancel / select) so those turns land
    in the same conversation archive that feeds the NLU context window.
    ``None`` when absent (older clients / WebSocket / scripts) — handlers
    then fall back to the ephemeral, user-scoped session."""
    return x_chat_session_id
