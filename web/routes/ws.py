"""
WS /ws/events

Each connected browser tab gets its own asyncio.Queue.
The trading loop (via logger.py → web.state.push_log) calls
_broadcast_nowait() which enqueues events onto every tab's queue using
call_soon_threadsafe so the async event loop picks them up safely.

Event envelope (JSON):
    { "type": "log",       "ts": "HH:MM:SS", "level": "info", "line": "..." }
    { "type": "state",     "ts": "...", ...snapshot fields... }
    { "type": "ping",      "ts": "..." }
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web import state as web_state

router = APIRouter()

_PING_INTERVAL = 15       # seconds between keepalive pings
_STATE_PUSH_INTERVAL = 2  # seconds between full state pushes


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()

    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    web_state.register_ws_client(q)

    # send full log history immediately so the browser has backfill on connect
    history = web_state.get_log_history()
    for entry in history:
        try:
            await websocket.send_text(json.dumps(entry))
        except Exception:
            break

    # send current state immediately
    try:
        await websocket.send_text(json.dumps({"type": "state", **web_state.snapshot()}))
    except Exception:
        web_state.unregister_ws_client(q)
        return

    ping_task = asyncio.create_task(_ping_loop(websocket))
    state_task = asyncio.create_task(_state_push_loop(websocket))

    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=_PING_INTERVAL)
                await websocket.send_text(json.dumps(event))
            except asyncio.TimeoutError:
                # ping_task keeps the connection alive; just loop
                pass
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        ping_task.cancel()
        state_task.cancel()
        web_state.unregister_ws_client(q)


async def _ping_loop(ws: WebSocket) -> None:
    import time
    while True:
        await asyncio.sleep(_PING_INTERVAL)
        try:
            await ws.send_text(json.dumps({"type": "ping", "ts": _now()}))
        except Exception:
            return


async def _state_push_loop(ws: WebSocket) -> None:
    """Push a full state snapshot every few seconds so the browser stays fresh
    even if no log events fire (e.g. between candle closes)."""
    while True:
        await asyncio.sleep(_STATE_PUSH_INTERVAL)
        try:
            snap = web_state.snapshot()
            await ws.send_text(json.dumps({"type": "state", **snap}))
        except Exception:
            return


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")
