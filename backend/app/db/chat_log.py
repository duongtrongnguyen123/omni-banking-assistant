"""Durable chat-history store (the "conversations" sidebar backend).

This is deliberately separate from :mod:`app.context.session_store`. That
store is *ephemeral* — TTL-bounded, capped at ~20 messages — and exists
only to carry in-flight drafts and a short context window for the NLU.

This module is the *permanent* archive: every user turn and every Omni
reply is appended here so the user can reopen any past conversation,
exactly like the conversation list in other AI chat UIs.

There is no authentication yet, so conversations are namespaced only by
``user_id`` (the demo user). Swapping in real auth later means nothing
more than passing a real user id through ``current_user``.

All functions go through the shared :func:`app.db.connection.get_connection`
SQLite handle (autocommit / WAL). IDs are uuid4 hex; timestamps are
ISO-8601 UTC strings, matching the convention used elsewhere in the DB.
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from typing import Optional

from .connection import get_connection

# Keep auto-derived titles short enough to render in a narrow sidebar.
_TITLE_MAX = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _derive_title(text: str) -> str:
    """First user message, trimmed, becomes the conversation title."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > _TITLE_MAX:
        cleaned = cleaned[: _TITLE_MAX - 1].rstrip() + "…"
    return cleaned or "Cuộc trò chuyện mới"


# ---------------------------------------------------------------------------
# Sessions (conversations)
# ---------------------------------------------------------------------------


def create_session(user_id: str, title: str = "") -> dict:
    """Create a fresh conversation and return its row as a dict."""
    conn = get_connection()
    sid = _new_id()
    now = _now()
    conn.execute(
        "INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, user_id, title, now, now),
    )
    return {
        "id": sid,
        "user_id": user_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "preview": "",
    }


def list_sessions(user_id: str, limit: int = 100) -> list[dict]:
    """All of a user's conversations, newest activity first.

    Each row carries a ``message_count`` and a ``preview`` (the last
    message text) so the sidebar can render without a second query.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT s.id, s.user_id, s.title, s.created_at, s.updated_at,
               (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id)
                   AS message_count,
               (SELECT m.content FROM chat_messages m
                    WHERE m.session_id = s.id
                    ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1)
                   AS preview
        FROM chat_sessions s
        WHERE s.user_id = ?
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def admin_list_sessions(
    *,
    user_id: Optional[str] = None,
    q: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Conversation list for operators.

    User-facing history is scoped to one caller. Admin history intentionally
    spans users but stays read-only and paginated so the dashboard can inspect
    support/audit cases without touching the normal chat surface.
    """
    conn = get_connection()
    where: list[str] = []
    params: list[object] = []
    if user_id:
        where.append("s.user_id = ?")
        params.append(user_id)
    if q:
        like = f"%{q.strip()}%"
        where.append(
            """
            (
                s.title LIKE ?
                OR EXISTS (
                    SELECT 1 FROM chat_messages mq
                    WHERE mq.session_id = s.id AND mq.content LIKE ?
                )
            )
            """
        )
        params.extend([like, like])
    if intent:
        where.append(
            """
            EXISTS (
                SELECT 1 FROM chat_messages mi
                WHERE mi.session_id = s.id AND mi.intent = ?
            )
            """
        )
        params.append(intent)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT s.id, s.user_id, s.title, s.created_at, s.updated_at,
               (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id)
                   AS message_count,
               (SELECT m.content FROM chat_messages m
                    WHERE m.session_id = s.id
                    ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1)
                   AS preview,
               (SELECT GROUP_CONCAT(DISTINCT m.intent)
                    FROM chat_messages m
                    WHERE m.session_id = s.id AND m.intent IS NOT NULL)
                   AS intents
        FROM chat_sessions s
        {where_sql}
        ORDER BY s.updated_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, max(1, min(limit, 500)), max(0, offset)),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["intents"] = [
            x for x in (item.get("intents") or "").split(",") if x
        ]
        out.append(item)
    return out


def admin_count_sessions(
    *,
    user_id: Optional[str] = None,
    q: Optional[str] = None,
    intent: Optional[str] = None,
) -> int:
    conn = get_connection()
    where: list[str] = []
    params: list[object] = []
    if user_id:
        where.append("s.user_id = ?")
        params.append(user_id)
    if q:
        like = f"%{q.strip()}%"
        where.append(
            """
            (
                s.title LIKE ?
                OR EXISTS (
                    SELECT 1 FROM chat_messages mq
                    WHERE mq.session_id = s.id AND mq.content LIKE ?
                )
            )
            """
        )
        params.extend([like, like])
    if intent:
        where.append(
            """
            EXISTS (
                SELECT 1 FROM chat_messages mi
                WHERE mi.session_id = s.id AND mi.intent = ?
            )
            """
        )
        params.append(intent)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM chat_sessions s {where_sql}",
        params,
    ).fetchone()
    return int(row["n"] if row else 0)


def get_session(session_id: str, user_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, user_id, title, created_at, updated_at "
        "FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def get_messages(session_id: str, user_id: str) -> Optional[list[dict]]:
    """Ordered messages for a conversation, or ``None`` if it doesn't
    belong to the caller (so the route can return 404)."""
    if get_session(session_id, user_id) is None:
        return None
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, role, content, intent, response_json, created_at "
        "FROM chat_messages WHERE session_id = ? "
        "ORDER BY created_at ASC, rowid ASC",
        (session_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def admin_get_session(session_id: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, user_id, title, created_at, updated_at "
        "FROM chat_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def admin_get_messages(session_id: str) -> Optional[list[dict]]:
    if admin_get_session(session_id) is None:
        return None
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, user_id, role, content, intent, response_json, created_at "
        "FROM chat_messages WHERE session_id = ? "
        "ORDER BY created_at ASC, rowid ASC",
        (session_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def _row_to_message(row) -> dict:
    item = dict(row)
    raw = item.pop("response_json", None)
    item["response"] = None
    if raw:
        try:
            item["response"] = json.loads(raw)
        except Exception:
            item["response"] = None
    return item


def latest_session_id(user_id: str) -> Optional[str]:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM chat_sessions WHERE user_id = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["id"] if row else None


def resolve_session(user_id: str, session_id: Optional[str]) -> str:
    """Return a usable session id: the requested one if it exists and is
    owned by the caller, else create a fresh conversation.

    Used by the chat route so a message is never dropped on the floor
    just because the client sent a stale / missing session id.
    """
    if session_id and get_session(session_id, user_id) is not None:
        return session_id
    return create_session(user_id)["id"]


def rename_session(session_id: str, user_id: str, title: str) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "UPDATE chat_sessions SET title = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ?",
        (_derive_title(title), _now(), session_id, user_id),
    )
    return cur.rowcount > 0


def delete_session(session_id: str, user_id: str) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    # chat_messages rows go via ON DELETE CASCADE (foreign_keys = ON).
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def append_message(
    session_id: str,
    user_id: str,
    role: str,
    content: str,
    intent: Optional[str] = None,
    response: Optional[dict] = None,
) -> dict:
    """Append one turn and bump the conversation's ``updated_at``.

    The first *user* turn in an untitled conversation also sets the
    conversation title (so the sidebar shows something meaningful
    without the user having to name it).
    """
    conn = get_connection()
    mid = _new_id()
    now = _now()
    response_json = (
        json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        if response is not None
        else None
    )
    conn.execute(
        "INSERT INTO chat_messages "
        "(id, session_id, user_id, role, content, intent, response_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, session_id, user_id, role, content, intent, response_json, now),
    )
    conn.execute(
        "UPDATE chat_sessions SET updated_at = ?, "
        "title = CASE WHEN (title IS NULL OR title = '') AND ? = 'user' "
        "             THEN ? ELSE title END "
        "WHERE id = ?",
        (now, role, _derive_title(content), session_id),
    )
    return {
        "id": mid,
        "session_id": session_id,
        "role": role,
        "content": content,
        "intent": intent,
        "response": response,
        "created_at": now,
    }
