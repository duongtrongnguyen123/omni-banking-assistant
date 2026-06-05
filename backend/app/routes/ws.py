"""WebSocket endpoints.

Two channels:

* ``/ws/chat`` — bidirectional: client sends a message, server returns
  an ``OmniResponse``. Same as the ``POST /api/chat`` HTTP path.
* ``/ws/events`` — server-push only: pushes notification toasts
  (transfer success, schedule fired, anomaly warning, low balance,
  recurring detected) to the client. Drains any backlog on connect so
  events that fired while the tab was closed still surface.

The events channel fails open: if it disconnects the chat stays
functional, toasts just stop appearing until the client reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..services.events import get_bus
from ..services.orchestrator import handle_message

log = logging.getLogger("omni.ws")

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


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """Push notification stream.

    The client doesn't send anything meaningful here — we only listen
    for the close frame. Keeping a read coroutine running alongside
    the push loop lets us detect disconnects immediately rather than
    blocking forever in ``send_text`` after the socket dies.
    """
    await ws.accept()
    user_id = ws.headers.get("x-user-id") or get_settings().demo_user_id
    bus = get_bus()

    async def _drain_client() -> None:
        # We don't act on client messages, but we have to consume them
        # so the WebSocket protocol layer doesn't backpressure.
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            return

    drain_task = asyncio.create_task(_drain_client())
    try:
        async for event in bus.subscribe(user_id):
            try:
                await ws.send_text(event.model_dump_json())
            except Exception as e:
                log.warning("ws/events send failed for %s: %s", user_id, e)
                break
    except asyncio.CancelledError:  # pragma: no cover
        pass
    finally:
        drain_task.cancel()
        with contextlib.suppress(Exception):
            await drain_task
