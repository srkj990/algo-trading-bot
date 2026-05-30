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


@router.get("/api/fno-data")
async def get_fno_data(
    underlying: str = "NIFTY",
    instrument_type: str = "OPT",
    mode: str = "live",        # "live" | "backtest" (kept for compat; both use Kite now)
) -> JSONResponse:
    """
    Return available expiries and ATM strike for a given F&O underlying.
    Always fetches real expiries from Kite. Falls back to synthetic Thursday
    dates only if Kite is unavailable, so the form remains usable.
    """
    try:
        from fno_data_fetcher import (
            get_atm_option_strike,
            get_available_expiries,
            get_available_option_strikes,
        )
        expiries = get_available_expiries(underlying, instrument_type=instrument_type)
        if not expiries:
            return JSONResponse({
                "expiries": [],
                "error": f"No expiries found for {underlying} ({instrument_type}). "
                         "Ensure Kite credentials are configured and market data is available.",
            })
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
        return JSONResponse(result)
    except Exception as exc:
        # Fall back to synthetic Thursday expiries so the form remains usable
        # even when Kite credentials are missing or the market is closed.
        synthetic = _synthetic_expiries(instrument_type)
        return JSONResponse({
            "expiries": synthetic,
            "source": "synthetic",
            "error": str(exc),
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
            engine = BacktestEngine(cfg)
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

        if engine_name == "intraday_options" and fno_underlying != "BOTH":
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
    if engine_name == "intraday_options":
        strategy_mode = "SINGLE"
        strategy_name = str(data.get("strategy_name", "ATM_MOMENTUM"))
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
        min_confirmations = max(1, int(data.get("min_confirmations", min(2, len(strategies)))))

    # ── position limits ───────────────────────────────────────────────────────
    max_positions = max(1, int(data.get("max_open_positions", 1)))
    max_capital_per_trade = float(data.get("max_capital_per_trade", capital / max_positions))
    max_capital_deployed = float(data.get("max_capital_deployed", capital))
    one_trade_per_symbol_per_day = bool(data.get("one_trade_per_symbol_per_day", True))
    top_n = max(1, int(data.get("top_n", 1)))

    intraday_options_lot_mode = str(runtime_config.fno.intraday_options_lot_mode or "CAPITAL_BASED").upper()
    intraday_options_entry_mode = str(runtime_config.fno.intraday_options_entry_mode or "LIVE_STAGED").upper()

    summary_lines.extend([
        f"Engine={engine_name}",
        f"Risk style={risk_style['name']}",
        f"Strategy mode={strategy_mode} | Strategies={', '.join(strategies)}",
        f"Period={period} | Interval={interval}",
    ])

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
    )


def _safe_spot_symbol(base_symbol: str) -> str:
    try:
        from fno_data_fetcher import get_fno_spot_quote_symbol
        return get_fno_spot_quote_symbol(base_symbol)
    except Exception:
        return f"NSE:{base_symbol}"


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

    # per-trade list (closed only)
    trades_list: list[dict] = []
    if not trades_df.empty:
        closed = trades_df.dropna(subset=["exit_time"])
        for _, row in closed.iterrows():
            pnl = float(row.get("net_pnl") or row.get("pnl") or 0)
            trades_list.append({
                "symbol": str(row.get("symbol", "")),
                "side": str(row.get("side", "")),
                "entry_time": str(row.get("entry_time", ""))[:19],
                "exit_time": str(row.get("exit_time", ""))[:19],
                "entry_price": _ff(row.get("entry_price")),
                "exit_price": _ff(row.get("exit_price")),
                "quantity": int(row.get("quantity") or 0),
                "pnl": round(pnl, 2),
                "exit_reason": str(row.get("exit_reason", "")),
                "strategy": str(row.get("strategy") or ""),
            })

    def _safe(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if math.isnan(f) or math.isinf(f) else round(f, 4)
        except (TypeError, ValueError):
            return None

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
        "total_net_pnl": _safe(summary.get("total_net_pnl")),
        "tick_entry_fills": int(summary.get("tick_entry_fills") or 0),
        "equity_curve": equity_points,
        "trades": trades_list,
        "files": {
            "summary": str(paths[0]),
            "trades": str(paths[1]),
            "equity": str(paths[2]),
        },
    }


def _ff(v) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None
