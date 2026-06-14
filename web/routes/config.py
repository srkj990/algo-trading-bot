"""
Configuration, session-control, and backtest routes.

GET  /api/symbols          — symbol tables for the equity form
GET  /api/fno-data         — live expiries / ATM strike for an underlying
                             (falls back to synthetic expiries for backtest mode)
POST /api/configure        — validate form payload and store SessionConfig
POST /api/start            — build TradingContext and start trading loop
POST /api/stop             — graceful stop signal + clear context for reconfigure
POST /api/backtest         — build BacktestConfig and run backtest (streams progress)
GET  /api/backtest/results — latest backtest result
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web import state as web_state

router = APIRouter()

# module-level pending config (set by /api/configure, consumed by /api/start)
_pending_config: Any = None
_pending_config_lock = threading.Lock()

# latest backtest result
_backtest_result: dict | None = None
_backtest_lock = threading.Lock()
_backtest_thread: threading.Thread | None = None
_backtest_cancel = threading.Event()


def _export_session_report(context: Any) -> None:
    """Export session Excel report from TradeStore JSONL files after session ends."""
    try:
        from logger import log_event
        ts = getattr(context, "trade_store", None)
        if ts is None or not ts.is_enabled():
            return
        from session_report import export_session_excel
        from config import get_runtime_config
        rc = get_runtime_config()
        base_dir = str(getattr(rc.trade_store, "base_dir", "trade_store"))
        path = export_session_excel(
            engine_name=ts.engine_name,
            trade_day=ts.trade_day,
            base_dir=base_dir,
        )
        if path:
            log_event(f"[Report] Session Excel saved: {path}", "info")
    except Exception as exc:
        try:
            from logger import log_event
            log_event(f"[Report] Excel export failed (non-fatal): {exc}", "warning")
        except Exception:
            pass


# ── symbol / FNO reference data ───────────────────────────────────────────────

@router.get("/api/runtime-defaults")
async def get_runtime_defaults() -> JSONResponse:
    """Return runtime config values used to pre-populate all form fields in the web UI."""
    from config import get_runtime_config
    from backtesting import BACKTEST_DEFAULT_DATA
    rc = get_runtime_config()
    pd = (rc.backtest_defaults or {}).get("prompt_defaults", {})
    # Engine number → [period, interval] keyed by the engine-choice int (1–6)
    _engine_names = {
        "1": "intraday_equity",
        "2": "delivery_equity",
        "3": "futures_equity",
        "4": "options_equity",
        "5": "intraday_futures",
        "6": "intraday_options",
        "7": "intraday_options",  # Buyer Only — shares period/interval defaults with Both
        "8": "intraday_options",  # Seller Only — shares period/interval defaults with Both
    }
    engine_period_interval = {
        k: list(BACKTEST_DEFAULT_DATA.get(v, ["1d", "1m"]))
        for k, v in _engine_names.items()
    }
    return JSONResponse({
        # Risk controls
        "daily_max_loss_pct": rc.risk_controls.daily_max_loss_pct,
        "consecutive_loss_limit": rc.risk_controls.consecutive_loss_limit,
        # intraday_options FNO settings
        "intraday_options_max_trades_per_underlying": rc.fno.intraday_options_max_trades_per_underlying,
        "intraday_options_max_hold_minutes": rc.fno.intraday_options_max_hold_minutes,
        "intraday_options_lot_mode": rc.fno.intraday_options_lot_mode,
        "intraday_options_entry_mode": rc.fno.intraday_options_entry_mode,
        # Execution
        "exit_mode": rc.execution_safety.exit_mode,
        "default_execution_mode": rc.execution_safety.default_execution_mode,
        # Backtest/live form defaults (from backtest_prompt_defaults in yaml)
        "risk_style": pd.get("risk_style", 3),
        "one_trade_per_symbol_per_day": pd.get("one_trade_per_symbol_per_day", 2) == 1,
        "intraday_options_strategy_mode": pd.get("intraday_options_strategy_mode", 1),
        "partial_exit_enabled": pd.get("partial_exit_enabled", 1) == 1,
        "capital": pd.get("capital", 100000),
        "max_positions": pd.get("max_positions", 1),
        "engine_choice": pd.get("engine_choice", 6),
        # Period/interval defaults per engine — driven by backtest_defaults.default_data in yaml
        "engine_period_interval": engine_period_interval,
    })


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


def _synthetic_expiries(instrument_type: str) -> list[str]:
    """Generate next 4–6 weekly/monthly expiry dates as fallback when Kite is unavailable."""
    today = date.today()
    expiries: list[str] = []
    # weekly options expire on Thursday; monthly futures expire last Thursday
    # walk forward and collect the next 6 Thursdays
    d = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    for _ in range(6):
        expiries.append(d.isoformat())
        d += timedelta(days=7)
    return expiries


_FNO_DATA_TIMEOUT_SECONDS = 10.0


def _fetch_fno_payload(underlying: str, instrument_type: str) -> dict[str, Any]:
    """Blocking Kite lookup — run off the event loop via an executor so a slow
    or unreachable broker connection can't freeze the whole web server."""
    from fno_data_fetcher import (
        get_atm_option_strike,
        get_available_expiries,
        get_available_option_strikes,
    )
    expiries = get_available_expiries(underlying, instrument_type=instrument_type)
    if not expiries:
        return {
            "expiries": [],
            "error": f"No expiries found for {underlying} ({instrument_type}). "
                     "Ensure Kite credentials are configured and market data is available.",
        }
    result: dict[str, Any] = {"expiries": expiries, "source": "kite"}
    if instrument_type == "OPT":
        nearest = expiries[0]
        try:
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
        except Exception as strike_exc:
            result["strike_error"] = str(strike_exc)
    return result


@router.get("/api/fno-data")
async def get_fno_data(
    underlying: str = "NIFTY",
    instrument_type: str = "OPT",
    mode: str = "live",        # "live" | "backtest" (kept for compat; both use Kite now)
) -> JSONResponse:
    """
    Return available expiries and ATM strike for a given F&O underlying.
    Always fetches real expiries from Kite. Falls back to synthetic Thursday
    dates only if Kite is unavailable or unreachable, so the form remains usable.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_fno_payload, underlying, instrument_type),
            timeout=_FNO_DATA_TIMEOUT_SECONDS,
        )
        return JSONResponse(result)
    except Exception as exc:
        # Fall back to synthetic Thursday expiries so the form remains usable
        # even when Kite credentials are missing, the market is closed, or the
        # broker connection is slow/unreachable (e.g. DNS hangs that ignore
        # the request-level timeout).
        if isinstance(exc, asyncio.TimeoutError):
            error_text = (
                f"Timed out after {_FNO_DATA_TIMEOUT_SECONDS:.0f}s waiting for Kite "
                f"instrument data for {underlying} ({instrument_type})."
            )
        else:
            error_text = str(exc)
        synthetic = _synthetic_expiries(instrument_type)
        return JSONResponse({
            "expiries": synthetic,
            "source": "synthetic",
            "error": error_text,
            "hint": "Could not reach Kite — showing estimated expiry dates. "
                    "Verify credentials for live trading.",
        }, status_code=200)


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

    # Wire the stop event into the trading loop so STOP button works immediately
    context.stop_fn = web_state.get_stop_event().is_set

    def _loop():
        from orchestration.session import run_trading_session
        try:
            run_trading_session(context)
        except Exception as exc:
            import traceback
            from logger import log_event
            log_event(f"[WEB] Trading loop error: {exc}\n{traceback.format_exc()}", "error")
        finally:
            _export_session_report(context)
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


@router.post("/api/reset")
async def reset_session() -> JSONResponse:
    """
    Clear context and pending config so the user can reconfigure cleanly.
    Call this after the session has fully stopped.
    """
    global _pending_config
    if web_state.is_session_alive():
        return JSONResponse({"error": "Session still running. Stop it first."}, status_code=409)
    web_state.clear_context()
    with _pending_config_lock:
        _pending_config = None
    web_state.set_status("idle")
    web_state.reset_stop_event()
    return JSONResponse({"ok": True, "status": "idle"})


# ── backtest ──────────────────────────────────────────────────────────────────

@router.post("/api/backtest")
async def run_backtest(payload: dict) -> JSONResponse:
    """
    Build a BacktestConfig from the form payload and run the backtest in a
    daemon thread.  Progress is streamed over WebSocket as log events.
    Returns immediately; poll GET /api/backtest/results or listen on WS.
    """
    global _backtest_thread

    if _backtest_thread is not None and _backtest_thread.is_alive():
        return JSONResponse({"error": "A backtest is already running."}, status_code=409)

    _backtest_cancel.clear()

    try:
        cfg = _build_backtest_config(payload)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Backtest configuration error: {exc}"}, status_code=500)

    def _run():
        global _backtest_result
        from logger import log_event
        from backtesting import BacktestEngine, export_backtest_results
        try:
            log_event(f"[BACKTEST] Starting {cfg.engine_name} | {cfg.period} / {cfg.interval} | capital={cfg.capital:,.0f}")
            log_event(f"[BACKTEST] Universe: {', '.join(str(s) for s in cfg.universe)}")
            log_event(f"[BACKTEST] Strategy: {cfg.strategy_mode} — {', '.join(cfg.strategies)}")
            def _on_trade(trade):
                web_state.broadcast({"type": "backtest_trade", "trade": trade})

            engine = BacktestEngine(cfg, log_fn=log_event, cancel_event=_backtest_cancel, trade_fn=_on_trade)
            summary = engine.run()
            paths = export_backtest_results(summary)
            result = _summarise(summary, paths)
            with _backtest_lock:
                _backtest_result = result
            web_state.broadcast({"type": "backtest_done", "result": result})
            log_event(f"[BACKTEST] Done — return {result['total_return_pct']:+.2f}% | "
                      f"{result['closed_trades']} trades | win rate {result['win_rate_pct']:.1f}%")
        except Exception as exc:
            log_event(f"[BACKTEST] Error: {exc}", "error")
            web_state.broadcast({"type": "backtest_error", "error": str(exc)})

    _backtest_thread = threading.Thread(target=_run, name="backtest-loop", daemon=True)
    _backtest_thread.start()

    return JSONResponse({
        "ok": True,
        "engine": cfg.engine_name,
        "period": cfg.period,
        "interval": cfg.interval,
        "capital": cfg.capital,
    })


@router.get("/api/backtest/results")
async def get_backtest_results() -> JSONResponse:
    with _backtest_lock:
        if _backtest_result is None:
            running = _backtest_thread is not None and _backtest_thread.is_alive()
            return JSONResponse({"status": "running" if running else "idle"})
        return JSONResponse({"status": "done", "result": _backtest_result})


@router.post("/api/backtest/cancel")
async def cancel_backtest() -> JSONResponse:
    _backtest_cancel.set()
    return JSONResponse({"ok": True})


@router.get("/api/session-trades")
async def get_session_trades() -> JSONResponse:
    """Return today's closed trades from the JSONL trade store, enriched with entry metadata."""
    import json
    from datetime import date as _date
    from pathlib import Path

    ctx = web_state.get_context()
    today = _date.today().isoformat()
    engine_name = "intraday_options"
    if ctx and ctx.engine:
        engine_name = ctx.engine.name

    # locate trade store directory
    base_dir = Path("runners")
    candidate_dirs = sorted(base_dir.glob(f"*/{engine_name}"), key=lambda p: p.name) if base_dir.exists() else []
    if not candidate_dirs:
        candidate_dirs = [Path(".")]

    trades: list[dict] = []
    for runner_dir in candidate_dirs:
        trades_file = runner_dir / "state" / "trade_store" / f"{engine_name}_{today}_trades.jsonl"
        if not trades_file.exists():
            continue
        try:
            with open(trades_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
        except Exception:
            pass

    # also include in-memory trades for running session
    if ctx and ctx.trade_book:
        seen_ids = {t.get("trade_id") for t in trades}
        for t in ctx.trade_book:
            if str(t.get("exit_time", ""))[:10] == today and t.get("trade_id") not in seen_ids:
                trades.append(t)

    return JSONResponse({"trades": trades, "trade_day": today, "engine": engine_name})


# ── bot override controls ──────────────────────────────────────────────────────

@router.post("/api/override/pause")
async def override_pause() -> JSONResponse:
    """Pause new entries — existing positions still managed."""
    ctx = web_state._context
    if ctx is None:
        return JSONResponse({"error": "No active session."}, status_code=400)
    ctx.session_runtime_state["override_pause_entries"] = True
    from logger import log_event
    log_event("[OVERRIDE] New entries PAUSED by trader. Existing positions still managed.", "warning")
    web_state.broadcast({"type": "override", "action": "pause", "ts": _ts()})
    return JSONResponse({"ok": True, "action": "pause"})


@router.post("/api/override/resume")
async def override_resume() -> JSONResponse:
    """Resume new entries after a pause."""
    ctx = web_state._context
    if ctx is None:
        return JSONResponse({"error": "No active session."}, status_code=400)
    ctx.session_runtime_state.pop("override_pause_entries", None)
    ctx.session_runtime_state.pop("override_safe_mode", None)
    from logger import log_event
    log_event("[OVERRIDE] New entries RESUMED by trader.", "info")
    web_state.broadcast({"type": "override", "action": "resume", "ts": _ts()})
    return JSONResponse({"ok": True, "action": "resume"})


@router.post("/api/override/safe_mode")
async def override_safe_mode() -> JSONResponse:
    """Safe mode: pause entries + move all open positions to breakeven stop,
    halve remaining target distance, activate trailing immediately.
    When all positions close, the session auto-stops."""
    ctx = web_state._context
    if ctx is None:
        return JSONResponse({"error": "No active session."}, status_code=400)
    ctx.session_runtime_state["override_safe_mode"] = True
    ctx.session_runtime_state["override_pause_entries"] = True
    # clear the per-position tightening flag so the next cycle re-applies
    for pos in ctx.positions.values():
        pos.pop("_safe_mode_tightened", None)
    from logger import log_event
    n = len(ctx.positions)
    log_event(
        f"[OVERRIDE] SAFE MODE activated — entries paused, {n} position(s) moved to "
        "breakeven stop + halved target + trailing activated. Session auto-stops when flat.",
        "warning",
    )
    web_state.broadcast({"type": "override", "action": "safe_mode", "ts": _ts()})
    return JSONResponse({"ok": True, "action": "safe_mode", "positions_affected": n})


@router.post("/api/override/emergency_exit")
async def override_emergency_exit() -> JSONResponse:
    """Close all open positions immediately (market order)."""
    ctx = web_state._context
    if ctx is None:
        return JSONResponse({"error": "No active session."}, status_code=400)
    if not ctx.positions:
        return JSONResponse({"ok": True, "action": "emergency_exit", "closed": 0})

    from logger import log_event
    from datetime import datetime
    from orchestration import positions as position_flow
    from config import TRANSACTION_COST_MODEL_ENABLED, TRANSACTION_SLIPPAGE_PCT_PER_SIDE
    from models.position_adapter import opposite_side, position_quantity

    log_event("[OVERRIDE] EMERGENCY EXIT — closing all positions NOW.", "error")
    exit_time = datetime.now()
    closed = 0
    for symbol, position in list(ctx.positions.items()):
        try:
            exit_price = position_flow.get_latest_exit_price(
                ctx.engine, symbol, position, ctx.fetch_data, ctx.log_event,
            )
            ctx.place_order(
                opposite_side(position),
                position_quantity(position),
                symbol,
                note="Emergency exit override",
                product=ctx.engine.order_product,
                enforce_spread_check=False,
                enforce_margin_check=False,
                entry_price=exit_price,
            )
            position_flow.record_closed_trade(
                ctx.trade_book, ctx.trade_store, symbol, position, exit_price,
                "Emergency exit override", exit_time,
                TRANSACTION_COST_MODEL_ENABLED,
                float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            del ctx.positions[symbol]
            closed += 1
        except Exception as exc:
            log_event(f"[OVERRIDE] Emergency exit failed for {symbol}: {exc}", "error")

    from orchestration.context import persist_runtime_state
    persist_runtime_state(ctx)
    web_state.broadcast({"type": "override", "action": "emergency_exit", "closed": closed, "ts": _ts()})
    log_event(f"[OVERRIDE] Emergency exit complete — {closed} position(s) closed.", "warning")
    return JSONResponse({"ok": True, "action": "emergency_exit", "closed": closed})


@router.get("/api/override/status")
async def override_status() -> JSONResponse:
    ctx = web_state._context
    if ctx is None:
        return JSONResponse({"paused": False, "safe_mode": False})
    return JSONResponse({
        "paused": bool(ctx.session_runtime_state.get("override_pause_entries")),
        "safe_mode": bool(ctx.session_runtime_state.get("override_safe_mode")),
    })


def _ts():
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


# ── backtest helpers ──────────────────────────────────────────────────────────

def _build_backtest_config(data: dict):
    """
    Build a BacktestConfig from a plain dict submitted by the browser form.
    Mirrors the logic of prompt_backtest_config() without any input() calls.
    """
    from backtesting import (
        BACKTEST_DEFAULT_DATA,
        BACKTEST_FNO_SYMBOLS,
        BACKTEST_PROMPT_DEFAULTS,
        BacktestConfig,
        ENGINE_OPTIONS,
        VALID_BACKTEST_INTERVALS,
        VALID_BACKTEST_PERIODS,
        build_fno_backtest_universe,
    )
    from config import (
        FNO_INDEX_SYMBOLS,
        NIFTY50_SYMBOLS,
        MANUAL_SYMBOL_TABLE,
        SINGLE_SYMBOL_TABLE,
        get_risk_style_presets,
        get_runtime_config,
        is_intraday_options_engine_name,
        INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS,
    )
    from fno_data_fetcher import get_fno_spot_quote_symbol

    runtime_config = get_runtime_config()

    # ── engine ────────────────────────────────────────────────────────────────
    engine_choice = str(data.get("engine_choice", "1"))
    if engine_choice not in ENGINE_OPTIONS:
        raise ValueError(f"Invalid engine choice: {engine_choice}")
    engine_cls = ENGINE_OPTIONS[engine_choice]
    engine_name = engine_cls.name
    is_fno = "futures" in engine_name or "options" in engine_name

    # ── capital ───────────────────────────────────────────────────────────────
    try:
        capital = float(data["capital"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("capital must be a positive number")
    if capital <= 0:
        raise ValueError("capital must be greater than 0")

    # ── period / interval ─────────────────────────────────────────────────────
    default_period, default_interval = BACKTEST_DEFAULT_DATA.get(
        engine_name,
        (getattr(engine_cls, "data_period", "6mo"), getattr(engine_cls, "data_interval", "1d")),
    )
    period = str(data.get("period", default_period)).strip() or default_period
    interval = str(data.get("interval", default_interval)).strip() or default_interval
    if period not in VALID_BACKTEST_PERIODS:
        raise ValueError(f"Invalid period '{period}'. Valid: {', '.join(VALID_BACKTEST_PERIODS)}")
    if interval not in VALID_BACKTEST_INTERVALS:
        raise ValueError(f"Invalid interval '{interval}'. Valid: {', '.join(VALID_BACKTEST_INTERVALS)}")

    # ── risk style ────────────────────────────────────────────────────────────
    risk_style_key = str(data.get("risk_style", "2"))
    risk_styles = get_risk_style_presets(engine_name)
    if risk_style_key not in risk_styles:
        risk_style_key = "2"
    risk_style = risk_styles[risk_style_key]

    # ── universe / contract selection ─────────────────────────────────────────
    option_backtest_settings: dict | None = None
    summary_lines: list[str] = []

    if is_fno:
        fno_underlying = str(data.get("fno_underlying", "NIFTY")).upper()
        if fno_underlying == "BOTH":
            universe = build_fno_backtest_universe(engine_name, "BOTH")
            summary_lines.append("F&O universe: NIFTY 50 + SENSEX")
        else:
            if fno_underlying not in BACKTEST_FNO_SYMBOLS:
                raise ValueError(f"Unsupported FNO underlying for backtesting: {fno_underlying}. "
                                  f"Supported: {', '.join(BACKTEST_FNO_SYMBOLS)}")
            universe = build_fno_backtest_universe(engine_name, fno_underlying)
            summary_lines.append(f"F&O underlying: {fno_underlying}")

        if is_intraday_options_engine_name(engine_name) and fno_underlying != "BOTH":
            fno_expiry = str(data.get("fno_expiry", ""))
            structure_mode = str(data.get("fno_structure", "SINGLE")).upper()
            if structure_mode == "PAIR":
                lower_strike = int(data.get("lower_pe_strike", 0))
                upper_strike = int(data.get("upper_ce_strike", 0))
                if lower_strike <= 0 or upper_strike <= 0 or lower_strike >= upper_strike:
                    raise ValueError("lower_pe_strike must be below upper_ce_strike and both positive")
                option_backtest_settings = {
                    "base_symbol": fno_underlying,
                    "expiry": fno_expiry,
                    "structure_mode": "PAIR",
                    "lower_strike": lower_strike,
                    "upper_strike": upper_strike,
                    "underlying_symbol": _safe_spot_symbol(fno_underlying),
                }
                summary_lines.append(f"Structure: PAIR {lower_strike}–{upper_strike}")
            else:
                strike_mode = str(data.get("strike_offset_mode", "ATM")).upper()
                option_backtest_settings = {
                    "base_symbol": fno_underlying,
                    "expiry": fno_expiry,
                    "structure_mode": "SINGLE",
                    "strike_mode": strike_mode,
                    "underlying_symbol": _safe_spot_symbol(fno_underlying),
                }
                summary_lines.append(f"Structure: ATM SINGLE | Strike mode: {strike_mode}")
    else:
        symbol_mode = str(data.get("symbol_mode", "NIFTY50")).upper()
        if symbol_mode == "NIFTY50":
            universe = tuple(NIFTY50_SYMBOLS)
            summary_lines.append("Universe: NIFTY50 (50 stocks)")
        elif symbol_mode == "SINGLE":
            sym = str(data.get("symbol", "")).strip().upper()
            if not sym:
                raise ValueError("symbol is required for SINGLE mode")
            # resolve from table if it's a key, otherwise use as-is
            resolved = SINGLE_SYMBOL_TABLE.get(sym, sym)
            # add .NS suffix for yfinance if missing
            if not resolved.endswith(".NS") and ":" not in resolved:
                resolved = resolved + ".NS"
            universe = (resolved,)
            summary_lines.append(f"Universe: {resolved}")
        elif symbol_mode == "MANUAL_MULTI":
            raw = str(data.get("symbols", "")).strip()
            syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
            if not syms:
                raise ValueError("symbols must not be empty for MANUAL_MULTI mode")
            resolved_syms = []
            for s in syms:
                r = MANUAL_SYMBOL_TABLE.get(s, s)
                if not r.endswith(".NS") and ":" not in r:
                    r = r + ".NS"
                resolved_syms.append(r)
            universe = tuple(dict.fromkeys(resolved_syms))  # deduplicate, preserve order
            summary_lines.append(f"Universe: {', '.join(universe)}")
        else:
            raise ValueError(f"Invalid symbol_mode: {symbol_mode}")

    # ── strategy ──────────────────────────────────────────────────────────────
    strategy_mode_raw = str(data.get("strategy_mode", "2"))
    if is_intraday_options_engine_name(engine_name):
        if strategy_mode_raw == "2":
            strategy_mode = "MULTI"
            strategy_name = None
            raw_strats = data.get("strategies", [])
            if isinstance(raw_strats, str):
                raw_strats = [s.strip() for s in raw_strats.split(",") if s.strip()]
            valid_iopts = set(engine_cls.supported_strategies.values())
            strategies = tuple(s.upper() for s in raw_strats if s.upper() in valid_iopts)
            if not strategies:
                strategies = ("ATM_MULTI", "ATM_BREAKOUT_EXPANSION")
            min_confirmations = max(1, int(data.get("min_confirmations", 1)))
        else:
            strategy_mode = "SINGLE"
            strategy_name = str(data.get("strategy_name", "ATM_MOMENTUM")).upper()
            valid_iopts = set(engine_cls.supported_strategies.values())
            if strategy_name not in valid_iopts:
                strategy_name = "ATM_MOMENTUM"
            strategies = (strategy_name,)
            min_confirmations = 1
    elif strategy_mode_raw == "3":
        strategy_mode = "AUTO_ADAPTIVE"
        strategy_name = None
        strategies = ("MA", "RSI")
        min_confirmations = max(2, int(INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS))
    elif strategy_mode_raw == "1":
        strategy_mode = "SINGLE"
        strategy_name = str(data.get("strategy_name", next(iter(engine_cls.supported_strategies.values()))))
        strategies = (strategy_name,)
        min_confirmations = 1
    else:
        strategy_mode = "MULTI"
        strategy_name = None
        raw_strats = data.get("strategies", [])
        if isinstance(raw_strats, str):
            raw_strats = [s.strip() for s in raw_strats.split(",") if s.strip()]
        strategies = tuple(raw_strats) if raw_strats else tuple(engine_cls.supported_strategies.values())
        min_confirmations = max(1, int(data.get("min_confirmations", 1)))

    # ── position limits ───────────────────────────────────────────────────────
    max_positions = max(1, int(data.get("max_open_positions", 1)))
    max_capital_per_trade = float(data.get("max_capital_per_trade", capital / max_positions))
    max_capital_deployed = float(data.get("max_capital_deployed", capital))
    one_trade_per_symbol_per_day = bool(data.get("one_trade_per_symbol_per_day", True))
    top_n = max(1, int(data.get("top_n", 1)))

    _lot_mode_from_form = str(data.get("intraday_options_lot_mode", "")).strip().upper()
    intraday_options_lot_mode = (
        _lot_mode_from_form
        if _lot_mode_from_form in {"ONE_LOT", "CAPITAL_BASED"}
        else str(runtime_config.fno.intraday_options_lot_mode or "CAPITAL_BASED").upper()
    )
    _entry_mode_from_form = str(data.get("intraday_options_entry_mode", "")).strip().upper()
    intraday_options_entry_mode = (
        _entry_mode_from_form
        if _entry_mode_from_form in {"LIVE_STAGED", "LEGACY_IMMEDIATE", "LIVE_TICK_CONFIRM"}
        else str(runtime_config.fno.intraday_options_entry_mode or "LIVE_STAGED").upper()
    )
    _max_trades_raw = data.get("max_trades_per_underlying")
    bt_max_trades = max(1, min(10, int(_max_trades_raw))) if _max_trades_raw is not None and str(_max_trades_raw).strip() else None
    _time_exit_raw = data.get("time_exit_minutes")
    bt_time_exit_minutes = max(0, min(120, int(_time_exit_raw))) if _time_exit_raw is not None and str(_time_exit_raw).strip() else None
    _forming_raw = data.get("forming_tick_enabled")
    bt_forming_tick_enabled = bool(_forming_raw) if _forming_raw is not None else None
    _confirm_raw = data.get("forming_tick_confirm_ticks")
    bt_forming_tick_confirm_ticks = max(1, min(5, int(_confirm_raw))) if _confirm_raw is not None and str(_confirm_raw).strip() else None
    _partial_raw = data.get("partial_exit_enabled")
    bt_partial_exit_enabled = None if (bool(_partial_raw) if _partial_raw is not None else True) else False
    _exit_mode_raw = str(data.get("exit_mode", "")).strip().upper()
    bt_exit_mode = (
        _exit_mode_raw
        if _exit_mode_raw in {"TRAIL_ONLY", "HARD_TARGET"}
        else str(runtime_config.execution_safety.exit_mode or "TRAIL_ONLY").upper()
    )
    _daily_max_loss_raw = data.get("daily_max_loss_pct")
    bt_daily_max_loss_pct = (
        max(0.0, min(1.0, float(_daily_max_loss_raw)))
        if _daily_max_loss_raw is not None and str(_daily_max_loss_raw).strip()
        else None
    )

    summary_lines.extend([
        f"Engine={engine_name}",
        f"Risk style={risk_style['name']} | risk%={risk_style['risk_percent']} "
        f"| ATR stop x={risk_style['atr_stop_multiplier']} | Trailing ATR x={risk_style['trailing_atr_multiplier']} "
        f"| Target R:R={risk_style['target_risk_reward']}",
        f"Strategy mode={strategy_mode} | Strategies={', '.join(strategies)} | Min confirmations={min_confirmations}",
        f"Period={period} | Interval={interval}",
        f"Position limits: max_positions={max_positions} | max_capital_per_trade={max_capital_per_trade:.2f} "
        f"| max_capital_deployed={max_capital_deployed:.2f} | top_n={top_n} "
        f"| one_trade_per_symbol_per_day={one_trade_per_symbol_per_day}",
    ])

    if is_intraday_options_engine_name(engine_name):
        summary_lines.append(
            f"Exit mode={bt_exit_mode} | Lot mode={intraday_options_lot_mode} | Entry mode={intraday_options_entry_mode}"
        )
        summary_lines.append(
            "Session overrides: "
            f"max_trades_per_underlying={bt_max_trades if bt_max_trades is not None else 'default'} "
            f"| time_exit_minutes={bt_time_exit_minutes if bt_time_exit_minutes is not None else 'default'} "
            f"| forming_tick_enabled={bt_forming_tick_enabled if bt_forming_tick_enabled is not None else 'default'} "
            f"| forming_tick_confirm_ticks={bt_forming_tick_confirm_ticks if bt_forming_tick_confirm_ticks is not None else 'default'} "
            f"| partial_exit_enabled={bt_partial_exit_enabled if bt_partial_exit_enabled is not None else 'default'} "
            f"| daily_max_loss_pct={bt_daily_max_loss_pct if bt_daily_max_loss_pct is not None else 'default'}"
        )

    return BacktestConfig(
        engine_name=engine_name,
        capital=capital,
        period=period,
        interval=interval,
        strategy_mode=strategy_mode,
        strategy_name=strategy_name,
        strategies=tuple(strategies),
        min_confirmations=min_confirmations,
        risk_percent=risk_style["risk_percent"],
        atr_stop_multiplier=risk_style["atr_stop_multiplier"],
        trailing_atr_multiplier=risk_style["trailing_atr_multiplier"],
        target_risk_reward=risk_style["target_risk_reward"],
        risk_style_name=risk_style["name"],
        top_n=top_n,
        max_positions=max_positions,
        max_capital_per_trade=max_capital_per_trade,
        max_capital_deployed=max_capital_deployed,
        universe=universe,
        one_trade_per_symbol_per_day=one_trade_per_symbol_per_day,
        intraday_options_lot_mode=intraday_options_lot_mode,
        intraday_options_entry_mode=intraday_options_entry_mode,
        summary_lines=summary_lines,
        option_backtest_settings=option_backtest_settings,
        max_trades_per_underlying=bt_max_trades,
        time_exit_minutes=bt_time_exit_minutes,
        forming_tick_enabled=bt_forming_tick_enabled,
        forming_tick_confirm_ticks=bt_forming_tick_confirm_ticks,
        partial_exit_enabled=bt_partial_exit_enabled,
        exit_mode=bt_exit_mode,
        daily_max_loss_pct=bt_daily_max_loss_pct,
    )


_SPOT_SYMBOL_FALLBACK = {
    "SENSEX": "BSE:SENSEX",
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}


def _safe_spot_symbol(base_symbol: str) -> str:
    try:
        from fno_data_fetcher import get_fno_spot_quote_symbol
        return get_fno_spot_quote_symbol(base_symbol)
    except Exception:
        return _SPOT_SYMBOL_FALLBACK.get(base_symbol.upper(), f"NSE:{base_symbol}")


def _summarise(summary: dict, paths: tuple) -> dict:
    """Convert BacktestEngine._build_summary() result to a JSON-safe dict."""
    import math

    cfg = summary["config"]
    eq_df = summary["equity_curve"]
    trades_df = summary["trades"]

    # equity curve — downsample to at most 300 points for the chart
    equity_points: list[float] = []
    if not eq_df.empty:
        step = max(1, len(eq_df) // 300)
        equity_points = [round(float(v), 2) for v in eq_df["equity"].iloc[::step]]

    def _safe(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if math.isnan(f) or math.isinf(f) else round(f, 4)
        except (TypeError, ValueError):
            return None

    def _safe_str(v):
        if v is None:
            return ""
        if isinstance(v, float) and math.isnan(v):
            return ""
        return str(v)

    def _safe_int(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if math.isnan(f) else int(f)
        except (TypeError, ValueError):
            return None

    # per-trade list (closed only)
    trades_list: list[dict] = []
    if not trades_df.empty:
        closed = trades_df.dropna(subset=["exit_time"])
        for _, row in closed.iterrows():
            gross = float(row.get("pnl") or 0)
            charges = float(row.get("estimated_charges") or 0)
            net = float(row.get("net_pnl") or (gross - charges))
            breakdown = row.get("charges_breakdown")
            charges_breakdown = breakdown if isinstance(breakdown, dict) else {}
            trades_list.append({
                "symbol": str(row.get("symbol", "")),
                "side": str(row.get("side", "")),
                "entry_time": str(row.get("entry_time", ""))[:19],
                "exit_time": str(row.get("exit_time", ""))[:19],
                "entry_price": _ff(row.get("entry_price")),
                "exit_price": _ff(row.get("exit_price")),
                "quantity": int(row.get("quantity") or 0),
                "pnl": round(gross, 2),
                "net_pnl": round(net, 2),
                "charges": round(charges, 2),
                "charges_breakdown": {
                    "brokerage": _safe(charges_breakdown.get("brokerage")) or 0.0,
                    "stt": _safe(charges_breakdown.get("stt")) or 0.0,
                    "exchange_txn": _safe(charges_breakdown.get("exchange_txn")) or 0.0,
                    "sebi": _safe(charges_breakdown.get("sebi")) or 0.0,
                    "stamp": _safe(charges_breakdown.get("stamp")) or 0.0,
                    "gst": _safe(charges_breakdown.get("gst")) or 0.0,
                    "slippage": _safe(charges_breakdown.get("slippage")) or 0.0,
                },
                "exit_reason": str(row.get("exit_reason", "")),
                "entry_reason": _safe_str(row.get("entry_reason")),
                "strategy": str(row.get("strategy") or ""),
                "score": _safe(row.get("score")),
                "atr": _safe(row.get("atr")),
                "hold_minutes": _safe_int(row.get("hold_minutes")),
                "partial_exit_count": _safe_int(row.get("partial_exit_count")) or 0,
                "partial_exit_events": row.get("partial_exit_events") if isinstance(row.get("partial_exit_events"), list) else [],
                "underlying": _safe_str(row.get("underlying_symbol")),
                "option_type": _safe_str(row.get("option_type")),
                "underlying_close_at_entry": _safe(row.get("underlying_close_at_entry")),
            })

    # ── regime analytics (B.13) ────────────────────────────────────────────────
    regime_analytics = _compute_regime_analytics(trades_df, cfg.capital)

    # ── failure analysis (B.15) ────────────────────────────────────────────────
    failure_analysis = _compute_failure_analysis(trades_df)

    return {
        "engine": cfg.engine_name,
        "period": cfg.period,
        "interval": cfg.interval,
        "capital": cfg.capital,
        "ending_equity": _safe(summary.get("ending_equity")),
        "total_return_pct": _safe(summary.get("total_return_percent")),
        "closed_trades": int(summary.get("closed_trades") or 0),
        "win_rate_pct": _safe(summary.get("win_rate_percent")),
        "max_drawdown_pct": _safe(summary.get("max_drawdown_percent")),
        "total_charges": _safe(summary.get("total_estimated_charges")),
        "charges_summary": {
            key: _safe(val) or 0.0
            for key, val in (summary.get("charges_summary") or {}).items()
        },
        "total_gross_pnl": _safe(summary.get("total_gross_pnl")),
        "total_net_pnl": _safe(summary.get("total_net_pnl")),
        "tick_entry_fills": int(summary.get("tick_entry_fills") or 0),
        "equity_curve": equity_points,
        "trades": trades_list,
        "regime_analytics": regime_analytics,
        "failure_analysis": failure_analysis,
        "files": {
            "summary": str(paths[0]),
            "trades": str(paths[1]),
            "equity": str(paths[2]),
        },
    }


def _compute_regime_analytics(trades_df, capital: float) -> list[dict]:
    """Break strategy performance by time-of-day and exit-reason 'regime' proxies."""
    import math
    if trades_df is None or (hasattr(trades_df, "empty") and trades_df.empty):
        return []

    closed = trades_df.dropna(subset=["exit_time"]) if not trades_df.empty else trades_df
    if closed.empty:
        return []

    def _pnl(row):
        return float(row.get("net_pnl") or row.get("pnl") or 0)

    def _hour(row):
        try:
            et = str(row.get("entry_time", ""))
            return int(et[11:13]) if len(et) >= 13 else None
        except Exception:
            return None

    def _session(row):
        h = _hour(row)
        if h is None:
            return "UNKNOWN"
        if h < 10:
            return "MORNING (09:15-10:00)"
        if h < 11:
            return "MID MORNING (10:00-11:00)"
        if h < 12:
            return "PRE LUNCH (11:00-12:00)"
        if h < 13:
            return "LUNCH (12:00-13:00)"
        if h < 14:
            return "POST LUNCH (13:00-14:00)"
        return "AFTERNOON (14:00+)"

    # Group by session
    buckets: dict[str, list] = {}
    for _, row in closed.iterrows():
        sess = _session(row)
        buckets.setdefault(sess, []).append(row)

    session_order = [
        "MORNING (09:15-10:00)", "MID MORNING (10:00-11:00)",
        "PRE LUNCH (11:00-12:00)", "LUNCH (12:00-13:00)",
        "POST LUNCH (13:00-14:00)", "AFTERNOON (14:00+)",
    ]
    result = []
    for sess in session_order:
        rows = buckets.get(sess, [])
        if not rows:
            continue
        pnls = [_pnl(r) for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        net = sum(pnls)
        result.append({
            "session": sess,
            "trades": len(rows),
            "wins": wins,
            "win_rate": round(wins / len(rows) * 100, 1) if rows else 0.0,
            "net_pnl": round(net, 2),
            "avg_pnl": round(net / len(rows), 2) if rows else 0.0,
            "return_pct": round(net / capital * 100, 2) if capital > 0 else 0.0,
        })

    # Also by exit reason
    reason_buckets: dict[str, list] = {}
    for _, row in closed.iterrows():
        reason = str(row.get("exit_reason", "unknown") or "unknown")[:30]
        reason_buckets.setdefault(reason, []).append(row)

    by_reason = []
    for reason, rows in sorted(reason_buckets.items(), key=lambda x: -len(x[1])):
        pnls = [_pnl(r) for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        net = sum(pnls)
        by_reason.append({
            "exit_reason": reason,
            "trades": len(rows),
            "win_rate": round(wins / len(rows) * 100, 1),
            "net_pnl": round(net, 2),
        })

    return {"by_session": result, "by_exit_reason": by_reason}


def _compute_failure_analysis(trades_df) -> dict:
    """Compute loss cluster, time-based failure, and streak analysis."""
    if trades_df is None or (hasattr(trades_df, "empty") and trades_df.empty):
        return {}

    closed = trades_df.dropna(subset=["exit_time"]) if not trades_df.empty else trades_df
    if closed.empty:
        return {}

    def _pnl(row):
        return float(row.get("net_pnl") or row.get("pnl") or 0)

    pnls = [_pnl(row) for _, row in closed.iterrows()]

    # Max consecutive losses
    max_consec_losses = 0
    cur_consec = 0
    for p in pnls:
        if p < 0:
            cur_consec += 1
            max_consec_losses = max(max_consec_losses, cur_consec)
        else:
            cur_consec = 0

    # Max consecutive wins
    max_consec_wins = 0
    cur_consec = 0
    for p in pnls:
        if p > 0:
            cur_consec += 1
            max_consec_wins = max(max_consec_wins, cur_consec)
        else:
            cur_consec = 0

    # Worst loss cluster: biggest N consecutive losses by cumulative loss
    worst_cluster_pnl = 0.0
    worst_cluster_len = 0
    best_run_pnl = 0.0
    best_run_len = 0
    cur_loss = 0.0
    cur_loss_len = 0
    cur_win = 0.0
    cur_win_len = 0
    for p in pnls:
        if p < 0:
            cur_loss += p
            cur_loss_len += 1
            cur_win = 0.0
            cur_win_len = 0
            if cur_loss < worst_cluster_pnl:
                worst_cluster_pnl = cur_loss
                worst_cluster_len = cur_loss_len
        else:
            cur_win += p
            cur_win_len += 1
            cur_loss = 0.0
            cur_loss_len = 0
            if cur_win > best_run_pnl:
                best_run_pnl = cur_win
                best_run_len = cur_win_len

    # Worst hour (most losses)
    hour_losses: dict[int, float] = {}
    hour_counts: dict[int, int] = {}
    for _, row in closed.iterrows():
        p = _pnl(row)
        if p >= 0:
            continue
        try:
            et = str(row.get("entry_time", ""))
            h = int(et[11:13]) if len(et) >= 13 else -1
        except Exception:
            h = -1
        if h >= 0:
            hour_losses[h] = hour_losses.get(h, 0.0) + p
            hour_counts[h] = hour_counts.get(h, 0) + 1

    worst_hour = None
    if hour_losses:
        worst_h = min(hour_losses, key=lambda h: hour_losses[h])
        worst_hour = {
            "hour": f"{worst_h:02d}:00",
            "losses": hour_counts[worst_h],
            "total_loss": round(hour_losses[worst_h], 2),
        }

    # Loss concentration: what % of total loss comes from worst 20% of trades
    loss_pnls = sorted([p for p in pnls if p < 0])
    top20_count = max(1, len(loss_pnls) // 5)
    total_loss = sum(loss_pnls)
    top20_loss = sum(loss_pnls[:top20_count]) if loss_pnls else 0.0
    loss_concentration_pct = round(top20_loss / total_loss * 100, 1) if total_loss < 0 else 0.0

    return {
        "max_consecutive_losses": max_consec_losses,
        "max_consecutive_wins": max_consec_wins,
        "worst_loss_cluster_pnl": round(worst_cluster_pnl, 2),
        "worst_loss_cluster_len": worst_cluster_len,
        "best_win_run_pnl": round(best_run_pnl, 2),
        "best_win_run_len": best_run_len,
        "worst_hour": worst_hour,
        "loss_concentration_pct": loss_concentration_pct,
        "total_losses": len(loss_pnls),
        "total_wins": len([p for p in pnls if p > 0]),
    }


def _ff(v) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None
