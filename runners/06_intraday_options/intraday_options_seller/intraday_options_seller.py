import os
import sys

# Add the zerodha-alago root to path so all package imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from cli.configuration import build_session_config_from_dict
from logger import finalize_session_logger, log_event, setup_session_logger
from orchestration.context import build_trading_context
from orchestration.session import (
    handle_keyboard_interrupt,
    run_trading_session,
    summarize_session,
)

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Change EXECUTION_MODE to "LIVE" when ready for real orders.
# ATM options intraday (MIS) — dynamic strike selection, Kite required.
EXECUTION_MODE = "PAPER"
CAPITAL = 100_000.0

# Seller Only (writes PE). Override via the first CLI arg if needed, e.g.
# `main.py 6`.
ENGINE_CHOICE = sys.argv[1] if len(sys.argv) > 1 else "8"

CONFIG = {
    "engine_choice": ENGINE_CHOICE,         # IntradayOptionsSellerEngine (1m MIS)
    "execution_mode": EXECUTION_MODE,
    "capital": CAPITAL,
    # data_provider and execution_provider are automatically set to KITE for F&O
    "fno_underlying": "NIFTY",
    "fno_expiry": "",                        # empty = nearest expiry auto-resolved
    "fno_structure": "SINGLE",              # SINGLE = ATM dynamic; PAIR = two-leg range
    "strike_offset_mode": "ATM",            # ATM / ATM_PLUS_1 / ATM_MINUS_1
    "strategy_name": "ATM_MULTI",           # ATM_MOMENTUM / ATM_ORB / ATM_MULTI / ATM_VWAP_REVERSION
    "intraday_options_lot_mode": "CAPITAL_BASED",   # ONE_LOT or CAPITAL_BASED
    "intraday_options_entry_mode": "LIVE_STAGED",   # LIVE_STAGED / LIVE_TICK_CONFIRM / LEGACY_IMMEDIATE
    "risk_style": "2",                      # BALANCED
    "max_open_positions": 2,
    "max_capital_per_trade": 50_000.0,
    "max_capital_deployed": 100_000.0,
}

# ── RUNNER ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup_session_logger(tag=f"iopts{ENGINE_CHOICE}")
    log_event(f"[CONFIG] engine_choice={ENGINE_CHOICE}")
    context = None
    try:
        session_config = build_session_config_from_dict(CONFIG)
        context = build_trading_context(session_config)
        run_trading_session(context)
    except KeyboardInterrupt:
        if context is not None:
            handle_keyboard_interrupt(context)
    except Exception as exc:
        log_event(f"[ERROR] {exc}", "error")
        raise
    finally:
        if context is not None:
            try:
                summarize_session(context)
            except Exception:
                pass
        finalize_session_logger()
