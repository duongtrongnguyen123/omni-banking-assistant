"""Short-term conversation memory.

Tracks the in-flight draft per user so follow-ups like
"chọn Trần Hoàng Minh" or "xác nhận" can be tied back to the right transaction.
"""

from __future__ import annotations

import threading
from typing import Optional

from ..models.schemas import ContactDraft, TransactionDraft


class ConversationMemory:
    def __init__(self) -> None:
        self.current_draft: Optional[TransactionDraft] = None
        self.current_contact_draft: Optional[ContactDraft] = None
        self.history: list[dict] = []

    def set_draft(self, draft: TransactionDraft) -> None:
        self.current_draft = draft

    def clear_draft(self) -> None:
        self.current_draft = None

    def set_contact_draft(self, draft: ContactDraft) -> None:
        self.current_contact_draft = draft

    def clear_contact_draft(self) -> None:
        self.current_contact_draft = None

    def append(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        # Keep last 30 turns only — enough for the demo, bounds memory.
        if len(self.history) > 30:
            self.history = self.history[-30:]

    def conversation_messages(self, max_turns: int = 8) -> list[dict]:
        """Recent turns as OpenAI-compatible chat messages.

        Used as conversational context for both NLU (so the model can resolve
        references like "còn tháng trước?") and response phrasing.
        """
        role_map = {"user": "user", "omni": "assistant"}
        return [
            {"role": role_map.get(h["role"], "user"), "content": h["content"]}
            for h in self.history[-max_turns:]
        ]


_lock = threading.Lock()
_sessions: dict[str, ConversationMemory] = {}


def session_for(user_id: str) -> ConversationMemory:
    with _lock:
        if user_id not in _sessions:
            _sessions[user_id] = ConversationMemory()
        return _sessions[user_id]
