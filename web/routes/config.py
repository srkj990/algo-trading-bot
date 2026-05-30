"""
Configuration and session-control routes.

GET  /api/symbols          — symbol tables for the equity form
GET  /api/fno-data         — live expiries / ATM strike for an underlying
POST /api/configure        — validate form payload and store SessionConfig
POST /api/start            — build TradingContext and start trading loop
POST /api/stop             — graceful stop signal
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web import state as web_state

router = APIRouter()

# module-level pending config (set by /api/configure, consumed by /api/start)
_pending_config: Any = None
_pending_config_lock = threading.Lock()


# ── symbol / FNO reference data ───────────────────────────────────────────────

@router.get("/api/symbols")
async def get_symbols() -> JSONResponse:
    """Return equity symbol tables for the configure form."""
    from config import MANUAL_SYMBOL_TABLE, NIFTY50_SYMBOLS, SINGLE_SYMBOL_TABLE
    return JSONResponse({
        "single": dict(SINGLE_SYMBOL_TABLE),
        "manual": dict(MANUAL_SYMBOL_TABLE),
        "nifty50": list(NIFTY50_SYMBOLS),
        "fno_underlyings": _fno_underlyings(),
    })


def _fno_underlyings() -> list[dict]:
    try:
        from config import FNO_INDEX_SYMBOLS
        from fno_data_fetcher import get_fno_display_name
        return [{"value": s, "label": get_fno_display_name(s)} for s in FNO_INDEX_SYMBOLS]
    except Exception:
        return [{"value": "NIFTY", "label": "NIFTY 50"}, {"value": "SENSEX", "label": "SENSEX"}]


@router.get("/api/fno-data")
async def get_fno_data(underlying: str = "NIFTY", instrument_type: str = "OPT") -> JSONResponse:
    """Return available expiries and ATM strike for a given F&O underlying."""
    try:
        from fno_data_fetcher import (
            get_atm_option_strike,
            get_available_expiries,
            get_available_option_strikes,
        )
        expiries = get_available_expiries(underlying, instrument_type=instrument_type)
        result: dict[str, Any] = {"expiries": expiries}
        if expiries and instrument_type == "OPT":
            nearest = expiries[0]
            atm_ce = get_atm_option_strike(underlying, nearest, "CE")
            atm_pe = get_atm_option_strike(underlying, nearest, "PE")
            ce_strikes = get_available_option_strikes(underlying, nearest, "CE")
            pe_strikes = get_available_option_strikes(underlying, nearest, "PE")
            result.update({
                "nearest_expiry": nearest,
                "atm_strike": atm_ce,
                "atm_ce": atm_ce,
                "atm_pe": atm_pe,
                "ce_strikes": ce_strikes[:20],
                "pe_strikes": pe_strikes[:20],
            })
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── configure ─────────────────────────────────────────────────────────────────

@router.post("/api/configure")
async def configure(payload: dict) -> JSONResponse:
    """
    Validate the form payload and build a SessionConfig.
    Stores it for /api/start to consume.
    Returns the config summary on success or validation errors on failure.
    """
    global _pending_config
    if web_state.is_session_alive():
        return JSONResponse({"error": "A session is already running. Stop it first."}, status_code=409)

    try:
        from cli.configuration import build_session_config_from_dict
        cfg = build_session_config_from_dict(payload)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Configuration error: {exc}"}, status_code=500)

    with _pending_config_lock:
        _pending_config = cfg

    return JSONResponse({
        "ok": True,
        "engine": cfg.engine.name,
        "execution_mode": cfg.execution_mode,
        "capital": cfg.capital,
        "risk_style": cfg.risk_style_name,
        "symbols": len(cfg.selected_symbols),
        "strategy": cfg.strategy_name or (", ".join(cfg.strategies) if cfg.strategies else "AUTO"),
    })


# ── start / stop ──────────────────────────────────────────────────────────────

@router.post("/api/start")
async def start_session() -> JSONResponse:
    """Build TradingContext from pending config and start the trading loop."""
    global _pending_config
    if web_state.is_session_alive():
        return JSONResponse({"error": "Session already running."}, status_code=409)

    with _pending_config_lock:
        cfg = _pending_config

    if cfg is None:
        return JSONResponse({"error": "No configuration found. POST /api/configure first."}, status_code=400)

    try:
        from orchestration.context import build_trading_context
        context = build_trading_context(cfg)
    except Exception as exc:
        return JSONResponse({"error": f"Failed to initialise trading context: {exc}"}, status_code=500)

    web_state.attach_context(context)
    web_state.reset_stop_event()
    web_state.set_status("running")

    def _loop():
        from orchestration.session import run_trading_session
        try:
            run_trading_session(context)
        except Exception as exc:
            from logger import log_event
            log_event(f"[WEB] Trading loop error: {exc}", "error")
        finally:
            web_state.set_status("stopped")
            web_state.broadcast({"type": "status", "status": "stopped"})

    t = threading.Thread(target=_loop, name="trading-loop", daemon=True)
    web_state.set_session_thread(t)
    t.start()

    return JSONResponse({"ok": True, "status": "running", "engine": cfg.engine.name})


@router.post("/api/stop")
async def stop_session() -> JSONResponse:
    """Signal the trading loop to stop after the current cycle."""
    web_state.request_stop()
    web_state.set_status("stopping")
    web_state.broadcast({"type": "status", "status": "stopping"})
    return JSONResponse({"ok": True, "status": "stopping"})
