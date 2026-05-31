"""
TickExitMonitor — intra-candle WebSocket exit monitoring for live/paper sessions.

For each open position, arms three breakout callbacks on the KiteTickerManager:
  - stop-loss level (fires immediately when breached)
  - trailing stop level (fires immediately when breached)
  - target level (fires immediately when hit)

Also registers a continuous per-tick callback that ratchets the trailing stop
upward in real time as price moves favorably — without waiting for candle close.

When an exit fires, the position is closed at the live tick price and recorded.
The session's candle-close manage_open_positions loop continues to work as a
fallback if WebSocket is unavailable.

Only activated when:
  - execution_mode is LIVE or PAPER
  - KiteTickerManager is connected (WebSocket available)
  - engine has tick_exit_enabled = True in EngineTickConfig (intraday_options only)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from config import TRANSACTION_COST_MODEL_ENABLED, TRANSACTION_SLIPPAGE_PCT_PER_SIDE
from engines.common import update_trailing_stop
from models.position_adapter import (
    opposite_side,
    position_entry_price,
    position_quantity,
    position_side,
)
from orchestration.context import persist_runtime_state
from orchestration.positions import record_closed_trade

logger = logging.getLogger(__name__)


class TickExitMonitor:
    """
    Intra-candle exit monitor.  One instance per session.

    Call ``refresh(positions)`` after each manage_open_positions cycle.
    Call ``shutdown()`` when the session ends.
    """

    def __init__(self, context) -> None:
        self._ctx = context
        self._lock = threading.Lock()
        # symbol -> {"token": int, "sl": float, "trail": float, "target": float}
        self._armed: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, positions: dict) -> None:
        """Re-arm exits for all open positions.  Call after each session cycle."""
        ticker = getattr(self._ctx, "_ticker_manager", None)
        if not ticker or not ticker.is_connected():
            return
        mode = str(getattr(self._ctx.config, "execution_mode", "")).upper()
        if mode not in {"LIVE", "PAPER"}:
            return
        from tick_entry.engine_config import get_engine_tick_config
        cfg = get_engine_tick_config(getattr(self._ctx.engine, "name", ""))
        if not cfg.tick_exit_enabled:
            return

        token_map: dict[str, int] = getattr(self._ctx, "_symbol_token_map", {})

        # Disarm symbols that are no longer open
        for sym in list(self._armed):
            if sym not in positions:
                self._disarm(sym, ticker)

        # Arm / refresh symbols that are open
        for sym, pos in positions.items():
            self._arm_position(sym, pos, ticker, token_map)

    def shutdown(self) -> None:
        """Disarm everything — call when session ends."""
        ticker = getattr(self._ctx, "_ticker_manager", None)
        for sym in list(self._armed):
            self._disarm(sym, ticker)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _disarm(self, symbol: str, ticker) -> None:
        info = self._armed.pop(symbol, {})
        token = info.get("token")
        if token and ticker:
            ticker.disarm_all(int(token))
            ticker.disarm_continuous(int(token))

    def _arm_position(self, symbol: str, position: dict, ticker, token_map: dict) -> None:
        token = token_map.get(symbol)
        if not token:
            return

        side = position_side(position)
        sl = float(position.get("stop_loss") or 0)
        trail = float(position.get("trailing_stop") or 0)
        target = float(position.get("target") or 0)

        # Re-arm only when levels changed (trailing stop moves on each ratchet)
        existing = self._armed.get(symbol, {})
        if (existing.get("sl") == sl and existing.get("trail") == trail
                and existing.get("target") == target):
            return

        # Disarm previous arms before re-arming
        ticker.disarm_all(int(token))
        ticker.disarm_continuous(int(token))

        # Breakout directions: for a BUY position, stop/trail fires on downward cross
        exit_dir = "SELL" if side == "BUY" else "BUY"
        entry_dir = "BUY" if side == "BUY" else "SELL"

        def make_exit_cb(reason: str):
            def cb(tok: int, ltp: float) -> None:
                self._fire_exit(symbol, ltp, reason)
            return cb

        if sl > 0:
            ticker.arm_breakout(int(token), sl, exit_dir, make_exit_cb("STOP_LOSS"))
        if trail > 0 and trail != sl:
            ticker.arm_breakout(int(token), trail, exit_dir, make_exit_cb("TRAILING_STOP"))
        if target > 0:
            ticker.arm_breakout(int(token), target, entry_dir, make_exit_cb("TARGET"))

        # Continuous callback for intra-candle trailing ratchet
        def on_tick(tok: int, ltp: float) -> None:
            self._on_tick_ratchet(symbol, ltp)

        ticker.arm_continuous(int(token), on_tick)

        self._armed[symbol] = {"token": token, "sl": sl, "trail": trail, "target": target}
        self._ctx.log_event(
            f"[TICK-EXIT] Armed {symbol} | SL={sl:.2f} TRAIL={trail:.2f} TARGET={target:.2f}"
        )

    def _on_tick_ratchet(self, symbol: str, ltp: float) -> None:
        """Per-tick callback — ratchets trailing stop upward in real time."""
        with self._lock:
            if symbol not in self._ctx.positions:
                return
            position = self._ctx.positions[symbol]
            trailing_pct = float(getattr(self._ctx.engine, "trailing_percent", 0.0))
            moved = update_trailing_stop(position, ltp, trailing_pct)
            if moved:
                ticker = getattr(self._ctx, "_ticker_manager", None)
                token_map: dict[str, int] = getattr(self._ctx, "_symbol_token_map", {})
                if ticker:
                    self._arm_position(symbol, position, ticker, token_map)

    def _fire_exit(self, symbol: str, ltp: float, exit_reason: str) -> None:
        """Called from WebSocket thread — exits the position at the live tick price."""
        with self._lock:
            if symbol not in self._ctx.positions:
                return  # already exited (double-arm guard)
            position = self._ctx.positions[symbol]

            # Staleness check: verify the exit level is still valid
            side = position_side(position)
            if exit_reason == "STOP_LOSS":
                sl = float(position.get("stop_loss") or 0)
                if (side == "BUY" and ltp > sl) or (side == "SELL" and ltp < sl):
                    return
            elif exit_reason == "TRAILING_STOP":
                if not position.get("trailing_active"):
                    return
                trail = float(position.get("trailing_stop") or 0)
                if (side == "BUY" and ltp > trail) or (side == "SELL" and ltp < trail):
                    return
            elif exit_reason == "TARGET":
                target = float(position.get("target") or 0)
                if (side == "BUY" and ltp < target) or (side == "SELL" and ltp > target):
                    return

            now = datetime.now()
            self._ctx.log_event(
                f"[TICK-EXIT] {symbol} {exit_reason} @ {ltp:.2f} "
                f"(entry={position_entry_price(position):.2f}) [intra-candle]",
                "warning",
            )

            order_result = self._ctx.place_order(
                opposite_side(position),
                position_quantity(position),
                symbol,
                note=f"TickExit {exit_reason}",
                product=self._ctx.engine.order_product,
                enforce_spread_check=False,
                enforce_margin_check=False,
                entry_price=ltp,
            )
            exit_price = ltp
            if order_result and order_result.average_price:
                exit_price = float(order_result.average_price)

            record_closed_trade(
                self._ctx.trade_book,
                self._ctx.trade_store,
                symbol,
                position,
                exit_price,
                exit_reason,
                now,
                TRANSACTION_COST_MODEL_ENABLED,
                float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            del self._ctx.positions[symbol]
            # Disarm this symbol's arms now that it's closed
            ticker = getattr(self._ctx, "_ticker_manager", None)
            self._disarm(symbol, ticker)
            persist_runtime_state(self._ctx)
