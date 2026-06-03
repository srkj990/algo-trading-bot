import os
import sys

# Add the zerodha-alago root to path so all package imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

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
# Requires a live Kite session — data and execution are forced to KITE.
EXECUTION_MODE = "PAPER"
CAPITAL = 100_000.0

CONFIG = {
    "engine_choice": "3",           # FuturesEquityEngine (5m NRML)
    "execution_mode": EXECUTION_MODE,
    "capital": CAPITAL,
    # data_provider and execution_provider are automatically set to KITE for F&O
    "fno_underlying": "NIFTY",      # or "BANKNIFTY" / "BOTH"
    "fno_expiry": "",               # empty = nearest expiry auto-resolved
    "risk_style": "2",              # BALANCED
    "strategy_mode": "2",           # MULTI-strategy voting
    "strategies": ["MA", "VWAP"],
    "min_confirmations": 2,
    "max_open_positions": 2,
    "max_capital_per_trade": 50_000.0,
    "max_capital_deployed": 100_000.0,
}

# ── RUNNER ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup_session_logger()
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
