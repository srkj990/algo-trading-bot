"""
GET /api/state    — full JSON snapshot of live trading state
GET /api/logs     — last N log lines from the ring buffer
GET /api/indices  — live Nifty 50, Sensex LTP (Kite), and India VIX (yfinance)
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web import state as web_state

router = APIRouter()

# simple in-process cache so rapid page refreshes don't hammer Kite rate limits
_indices_cache: dict = {}
_indices_cache_ts: float = 0.0
_INDICES_CACHE_TTL = 8.0  # seconds

# VIX cache — longer TTL since VIX doesn't change tick-by-tick
_vix_cache: float | None = None
_vix_cache_ts: float = 0.0
_VIX_CACHE_TTL = 60.0  # seconds

# Bound on the blocking Kite/yfinance lookups below — protects the single
# uvicorn event loop from a DNS/socket hang that ignores library-level timeouts.
_INDICES_FETCH_TIMEOUT_SECONDS = 10.0


@router.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(web_state.snapshot())


@router.get("/api/warnings/history")
async def get_warnings_history() -> JSONResponse:
    """Return the warning ring-buffer history (last 200 entries)."""
    try:
        from web.core.warning_engine import get_warning_history
        return JSONResponse({"history": get_warning_history()})
    except Exception:
        return JSONResponse({"history": []})


@router.get("/api/logs")
async def get_logs(n: int = 200) -> JSONResponse:
    history = web_state.get_log_history()
    return JSONResponse({"logs": history[-n:]})


@router.get("/api/alert-history")
async def get_alert_history() -> JSONResponse:
    """Return persistent warn/error log history — never cleared during session."""
    return JSONResponse({"alerts": web_state.get_alert_history()})


@router.get("/api/status")
async def get_status() -> JSONResponse:
    return JSONResponse({
        "status": web_state.get_status(),
        "alive": web_state.is_session_alive(),
    })


@router.get("/api/indices")
async def get_indices() -> JSONResponse:
    """Return Nifty 50, Sensex, and India VIX with % change from previous close."""
    global _indices_cache, _indices_cache_ts, _vix_cache, _vix_cache_ts

    now = time.time()
    if now - _indices_cache_ts < _INDICES_CACHE_TTL and _indices_cache:
        return JSONResponse(_indices_cache)

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_indices),
            timeout=_INDICES_FETCH_TIMEOUT_SECONDS,
        )
    except Exception:
        result = {"nifty": None, "sensex": None, "source": "error"}

    # VIX — separate cache with longer TTL
    if now - _vix_cache_ts >= _VIX_CACHE_TTL:
        try:
            vix = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_vix),
                timeout=_INDICES_FETCH_TIMEOUT_SECONDS,
            )
        except Exception:
            vix = None
        _vix_cache = vix
        _vix_cache_ts = now
        # Push VIX into active session state so warning engine can pick it up
        _store_vix_in_session(vix)
    result["vix"] = _vix_cache

    _indices_cache = result
    _indices_cache_ts = now
    return JSONResponse(result)


def _fetch_vix() -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker("^INDIAVIX")
        hist = t.history(period="1d", interval="5m")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return None


def _store_vix_in_session(vix: float | None) -> None:
    """Cache VIX in TradingContext.session_runtime_state so warning_engine can read it without calling yfinance."""
    try:
        ctx = web_state._context
        if ctx is not None and vix is not None:
            ctx.session_runtime_state["_cached_vix"] = vix
    except Exception:
        pass


def _fetch_indices() -> dict:
    """Try Kite LTP first; fall back to yfinance on any error."""
    try:
        return _fetch_via_kite()
    except Exception:
        pass
    try:
        return _fetch_via_yfinance()
    except Exception:
        return {"nifty": None, "sensex": None, "source": "error"}


def _fetch_via_kite() -> dict:
    from fno_data_fetcher import _get_kite_client
    from network_utils import kite_rate_limited_call

    kite = _get_kite_client()
    symbols = ["NSE:NIFTY 50", "BSE:SENSEX"]
    raw = kite_rate_limited_call("quote", lambda: kite.quote(symbols))

    def _extract(key):
        d = raw.get(key, {})
        ltp = float(d.get("last_price") or 0)
        ohlc = d.get("ohlc") or {}
        day_open = float(ohlc.get("open") or ltp)
        # ohlc.close = previous trading day's close (standard NSE/BSE % change basis)
        prev_close = float(ohlc.get("close") or day_open)
        chg_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0.0
        return {"ltp": round(ltp, 2), "open": round(day_open, 2), "chg_pct": round(chg_pct, 2)}

    return {
        "nifty":  _extract("NSE:NIFTY 50"),
        "sensex": _extract("BSE:SENSEX"),
        "source": "kite",
    }


def _fetch_via_yfinance() -> dict:
    import yfinance as yf

    def _get(ticker_sym):
        t = yf.Ticker(ticker_sym)
        hist = t.history(period="2d", interval="1d")
        intra = t.history(period="1d", interval="1m")
        if intra.empty:
            return {"ltp": None, "open": None, "chg_pct": None}
        ltp = float(intra["Close"].iloc[-1])
        day_open = float(intra["Open"].iloc[0])
        # Use previous day's daily close as the reference (matches NSE/BSE convention)
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else day_open
        chg_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0.0
        return {"ltp": round(ltp, 2), "open": round(day_open, 2), "chg_pct": round(chg_pct, 2)}

    return {
        "nifty":  _get("^NSEI"),
        "sensex": _get("^BSESN"),
        "source": "yfinance",
    }
