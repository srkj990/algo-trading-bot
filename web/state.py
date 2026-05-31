"""
Thread-safe shared state between the trading loop and the web server.

The trading loop (main thread or daemon thread) writes to TradingContext.
The web server thread reads from WebState to serve HTTP / WebSocket clients.

Design rules:
- WebState holds a reference to TradingContext; it never copies large dicts.
- snapshot() serialises what the browser needs; called under _lock so the
  trading loop cannot mutate mid-serialise.
- log_ring_buffer and ws_clients are written by logger.py (any thread) and
  read by the WebSocket broadcaster (web thread) — both under _lock.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestration.context import TradingContext

# ── module-level singleton ────────────────────────────────────────────────────
_lock = threading.Lock()
_context: "TradingContext | None" = None
_session_thread: threading.Thread | None = None
_stop_event = threading.Event()        # signals the trading loop to stop
_server_exit_event = threading.Event() # signals main.py to exit the process
_log_ring: deque[dict[str, Any]] = deque(maxlen=500)
_ws_clients: set[asyncio.Queue] = set()          # one Queue per connected tab
_session_status: str = "idle"                    # idle | running | stopped
_web_loop: asyncio.AbstractEventLoop | None = None  # set by server at startup

# ── warning center state ──────────────────────────────────────────────────────
_active_warnings: list[dict[str, Any]] = []      # warnings from latest cycle
_market_intel: dict[str, Any] = {}               # market intelligence summary


# ── lifecycle ─────────────────────────────────────────────────────────────────

def set_web_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _web_loop
    _web_loop = loop


def attach_context(ctx: "TradingContext") -> None:
    global _context
    with _lock:
        _context = ctx


def clear_context() -> None:
    global _context, _session_thread, _active_warnings, _market_intel
    with _lock:
        _context = None
        _session_thread = None
        _active_warnings = []
        _market_intel = {}
    try:
        from web.core.warning_engine import clear_warnings
        clear_warnings()
    except Exception:
        pass


def set_session_thread(t: threading.Thread) -> None:
    global _session_thread
    with _lock:
        _session_thread = t


def set_status(status: str) -> None:
    global _session_status
    with _lock:
        _session_status = status


def get_status() -> str:
    with _lock:
        return _session_status


def get_stop_event() -> threading.Event:
    return _stop_event


def request_stop() -> None:
    _stop_event.set()


def reset_stop_event() -> None:
    _stop_event.clear()


def get_server_exit_event() -> threading.Event:
    return _server_exit_event


def request_server_exit() -> None:
    _server_exit_event.set()


def is_session_alive() -> bool:
    with _lock:
        return _session_thread is not None and _session_thread.is_alive()


# ── log ring buffer ───────────────────────────────────────────────────────────

def push_log(line: str, level: str = "info") -> None:
    entry: dict[str, Any] = {
        "type": "log",
        "ts": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "line": line,
    }
    with _lock:
        _log_ring.append(entry)
        queues = list(_ws_clients)

    # broadcast outside the lock to avoid deadlock with async event loop
    _broadcast_nowait(entry, queues)


def get_log_history() -> list[dict[str, Any]]:
    with _lock:
        return list(_log_ring)


# ── warning center ────────────────────────────────────────────────────────────

def set_warnings(warnings: list[dict[str, Any]], market_intel: dict[str, Any]) -> None:
    global _active_warnings, _market_intel
    with _lock:
        _active_warnings = list(warnings)
        _market_intel = dict(market_intel)


def get_warnings() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with _lock:
        return list(_active_warnings), dict(_market_intel)


# ── WebSocket client registry ─────────────────────────────────────────────────

def register_ws_client(q: asyncio.Queue) -> None:
    with _lock:
        _ws_clients.add(q)


def unregister_ws_client(q: asyncio.Queue) -> None:
    with _lock:
        _ws_clients.discard(q)


def broadcast(event: dict[str, Any]) -> None:
    with _lock:
        queues = list(_ws_clients)
    _broadcast_nowait(event, queues)


def _broadcast_nowait(event: dict[str, Any], queues: list[asyncio.Queue]) -> None:
    """Put event on every client queue; drops silently when full."""
    if not queues or _web_loop is None:
        return
    for q in queues:
        try:
            _web_loop.call_soon_threadsafe(q.put_nowait, event)
        except Exception:
            pass


# ── state snapshot ────────────────────────────────────────────────────────────

def snapshot() -> dict[str, Any]:
    """
    JSON-serialisable snapshot of all live trading state.
    Called by GET /api/state and pushed over WebSocket after each cycle.
    """
    with _lock:
        ctx = _context
        status = _session_status

    if ctx is None:
        return {"status": status, "positions": {}, "trade_book": [],
                "candidates": [], "session": {}, "cycle": {}, "risk": {}}

    # positions ---------------------------------------------------------------
    positions: dict[str, Any] = {}
    for sym, pos in ctx.positions.items():
        positions[sym] = {
            "symbol": sym,
            "side": pos.get("side"),
            "quantity": pos.get("quantity"),
            "entry_price": _f(pos.get("entry_price")),
            "stop_loss": _f(pos.get("stop_loss")),
            "target": _f(pos.get("target")),
            "trailing_stop": _f(pos.get("trailing_stop")),
            "trailing_active": bool(pos.get("trailing_active")),
            "atr": _f(pos.get("atr")),
            "entry_time": str(pos.get("entry_time", "")),
            "engine_name": pos.get("engine_name"),
            "execution_mode": pos.get("execution_mode"),
            "pair_id": pos.get("pair_id"),
            # Greeks (options)
            "delta": _f((pos.get("entry_analytics") or {}).get("delta")),
            "iv": _f((pos.get("entry_analytics") or {}).get("iv")),
            "underlying_price": _f((pos.get("entry_analytics") or {}).get("underlying_price")),
            "option_type": (pos.get("entry_analytics") or {}).get("option_type"),
        }

    # trade book (today only) -------------------------------------------------
    today = ctx.active_trade_day.isoformat() if ctx.active_trade_day else ""
    trade_book: list[dict[str, Any]] = []
    for t in ctx.trade_book:
        exit_time = t.get("exit_time", "")
        if str(exit_time)[:10] != today:
            continue
        trade_book.append({
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "quantity": t.get("quantity"),
            "entry_price": _f(t.get("entry_price")),
            "exit_price": _f(t.get("exit_price")),
            "pnl": _f(t.get("pnl")),
            "net_pnl": _f(t.get("net_pnl")),
            "exit_reason": t.get("exit_reason"),
            "entry_time": str(t.get("entry_time", "")),
            "exit_time": str(exit_time),
        })

    # session summary ---------------------------------------------------------
    capital = float(ctx.config.capital or 0)
    deployed = sum(
        float(p.get("entry_price", 0)) * int(p.get("quantity", 0))
        for p in ctx.positions.values()
    )
    session_pnl = sum(float(t.get("net_pnl", t.get("pnl", 0)) or 0) for t in trade_book)
    daily_loss_pct = (-session_pnl / capital * 100) if capital > 0 and session_pnl < 0 else 0.0

    session: dict[str, Any] = {
        "engine": ctx.engine.name if ctx.engine else "",
        "execution_mode": ctx.config.execution_mode,
        "capital": capital,
        "deployed_capital": _f(deployed),
        "deployed_pct": _f(deployed / capital * 100 if capital > 0 else 0),
        "session_pnl": _f(session_pnl),
        "daily_loss_pct": _f(daily_loss_pct),
        "open_positions": len(ctx.positions),
        "trades_today": len(trade_book),
        "trade_day": today,
        "risk_style": getattr(ctx.config, "risk_style_name", ""),
        "symbols_count": len(getattr(ctx.config, "selected_symbols", []) or []),
    }

    # risk status -------------------------------------------------------------
    risk_pause = ctx.session_runtime_state.get("risk_pause_until")
    consecutive_losses = 0
    for t in reversed(trade_book):
        if float(t.get("net_pnl", t.get("pnl", 0)) or 0) < 0:
            consecutive_losses += 1
        else:
            break

    risk: dict[str, Any] = {
        "paused": bool(risk_pause),
        "pause_until": str(risk_pause or ""),
        "pause_reason": ctx.session_runtime_state.get("risk_pause_reason", ""),
        "consecutive_losses": consecutive_losses,
        "daily_loss_pct": _f(daily_loss_pct),
        "override_paused": bool(ctx.session_runtime_state.get("override_pause_entries")),
        "override_safe_mode": bool(ctx.session_runtime_state.get("override_safe_mode")),
    }

    with _lock:
        active_warnings = list(_active_warnings)
        market_intel = dict(_market_intel)

    return {
        "status": status,
        "positions": positions,
        "trade_book": trade_book,
        "session": session,
        "risk": risk,
        "warnings": active_warnings,
        "market_intel": market_intel,
        "ts": datetime.now().strftime("%H:%M:%S"),
    }


def _f(v: Any) -> float | None:
    """Safe float conversion; returns None for falsy/None values."""
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None
