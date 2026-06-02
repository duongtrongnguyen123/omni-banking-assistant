"""WebSocket chat endpoint — slide 5 calls out Socket.IO; FastAPI's native
WebSocket is API-compatible enough for the demo and removes a dependency."""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..services.orchestrator import handle_message

router = APIRouter(tags=["ws"])


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    await ws.accept()
    user_id = ws.headers.get("x-user-id") or get_settings().demo_user_id
    try:
        while True:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw)
                message = payload.get("message", "")
            except json.JSONDecodeError:
                message = raw
            if not message:
                continue
            resp = handle_message(user_id, message)
            await ws.send_text(resp.model_dump_json())
    except WebSocketDisconnect:
        return
