from __future__ import annotations

from typing import Optional

from fastapi import Header

from ..config import get_settings


def current_user(x_user_id: Optional[str] = Header(default=None)) -> str:
    """Identify the caller. Defaults to the demo user; a real deployment
    would validate JWT/OAuth2 here (per slide 5 — "Lớp bảo mật và an toàn")."""
    return x_user_id or get_settings().demo_user_id
