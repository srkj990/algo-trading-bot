"""
GET /api/state  — full JSON snapshot of live trading state
GET /api/logs   — last N log lines from the ring buffer
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web import state as web_state

router = APIRouter()


@router.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(web_state.snapshot())


@router.get("/api/logs")
async def get_logs(n: int = 200) -> JSONResponse:
    history = web_state.get_log_history()
    return JSONResponse({"logs": history[-n:]})


@router.get("/api/status")
async def get_status() -> JSONResponse:
    return JSONResponse({
        "status": web_state.get_status(),
        "alive": web_state.is_session_alive(),
    })
