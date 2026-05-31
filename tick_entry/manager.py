"""
TickEntryManager — unified tick/LTP entry across LIVE, PAPER and BACKTEST.

Usage (session loop):
    from tick_entry import TickEntryManager
    mgr = TickEntryManager(context)
    entered = mgr.run(ranked_candidates, cycle_remaining_seconds=52)

Usage (backtest loop — no network):
    from tick_entry import TickEntryManager
    entered = TickEntryManager.backtest_enter(
        candidates, current_candle, engine_helper, config, cash, positions, ...
    )
"""
from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from logger import log_event as _global_log
from tick_entry.engine_config import get_engine_tick_config
from tick_entry.levels import compute_live_trigger


# ---------------------------------------------------------------------------
# Public entry point for LIVE / PAPER session loop
# ---------------------------------------------------------------------------

class TickEntryManager:
    """
    One instance per session.  Call ``run()`` after each scan cycle.
    """

    def __init__(self, context: Any) -> None:
        self._ctx = context
        self._log = getattr(context, "log_event", _global_log)
        self._engine_name: str = getattr(context.engine, "name", "")
        self._cfg = get_engine_tick_config(self._engine_name)

    # ------------------------------------------------------------------
    # Main entry point: LIVE / PAPER
    # ------------------------------------------------------------------

    def run(
        self,
        candidates: list[dict],
        cycle_remaining_seconds: float | None = None,
    ) -> bool:
        """
        Monitor live prices for each candidate and fire an early entry
        when the trigger level is crossed.

        Returns True if at least one entry was fired.
        """
        if not self._cfg.enabled or not candidates:
            return False

        timeout = cycle_remaining_seconds if cycle_remaining_seconds is not None else self._cfg.timeout_seconds
        if timeout <= 0:
            return False

        mode = str(getattr(self._ctx.config, "execution_mode", "PAPER")).upper()

        # Delivery-equity daily bars – skip entirely
        if not self._cfg.enabled:
            return False

        arms = self._build_arms(candidates)
        if not arms:
            return False

        self._log(
            f"[TICK-ENTRY] Watching {len(arms)} candidate(s) for {timeout:.0f}s "
            f"| engine={self._engine_name} | mode={mode}"
        )

        return self._poll_and_fire(arms, timeout, mode)

    # ------------------------------------------------------------------
    # Arm building
    # ------------------------------------------------------------------

    def _build_arms(self, candidates: list[dict]) -> list[dict]:
        arms = []
        for cand in candidates:
            sym = cand.get("signal_symbol") or cand.get("symbol", "")
            direction = str(cand.get("signal") or "BUY").upper()
            if direction not in {"BUY", "SELL"}:
                continue
            if sym in self._ctx.positions:
                continue     # already in trade
            trigger = compute_live_trigger(cand)
            if trigger <= 0:
                continue
            arms.append({
                "symbol": sym,
                "direction": direction,
                "trigger": trigger,
                "candidate": deepcopy(cand),
                "fired": False,
            })
            self._log(
                f"[TICK-ENTRY] Armed {sym} {direction} "
                f"trigger={trigger:.2f} (close={cand.get('latest_close', 0):.2f} "
                f"atr={cand.get('atr', 0):.2f})"
            )
        return arms

    # ------------------------------------------------------------------
    # Polling loop (WebSocket-aware)
    # ------------------------------------------------------------------

    def _poll_and_fire(self, arms: list[dict], timeout: float, mode: str) -> bool:
        ticker = getattr(self._ctx, "_ticker_manager", None)
        use_ws = ticker and getattr(ticker, "is_connected", lambda: False)()
        token_map: dict[str, int] = getattr(self._ctx, "_symbol_token_map", {})
        entry_lock = threading.Lock()
        entered_flag = [False]

        if use_ws:
            self._log("[TICK-ENTRY] Using WebSocket tick stream")
            self._arm_websocket(arms, ticker, token_map, entry_lock, entered_flag, mode)
            time.sleep(min(timeout, self._cfg.timeout_seconds))
            # Disarm any remaining
            for arm in arms:
                if not arm["fired"]:
                    token = token_map.get(arm["symbol"])
                    if token:
                        ticker.disarm_all(int(token))
        else:
            self._log("[TICK-ENTRY] Using REST LTP polling")
            self._poll_ltp(arms, entry_lock, entered_flag, timeout, mode)

        return entered_flag[0]

    def _arm_websocket(
        self,
        arms: list[dict],
        ticker: Any,
        token_map: dict[str, int],
        lock: threading.Lock,
        entered_flag: list[bool],
        mode: str,
    ) -> None:
        for arm in arms:
            token = self._ensure_ws_subscription(arm["symbol"], ticker, token_map)
            if not token:
                continue

            def make_cb(a=arm):
                def cb(tok: int, ltp: float) -> None:
                    with lock:
                        if a["fired"] or self._ctx.positions.get(a["symbol"]):
                            return
                        a["fired"] = True
                    self._log(f"[TICK-ENTRY] WS trigger: {a['symbol']} @ {ltp:.2f}")
                    ok = self._fire(a["candidate"], ltp, mode)
                    if ok:
                        entered_flag[0] = True
                return cb

            ticker.arm_breakout(int(token), arm["trigger"], arm["direction"], make_cb())

    def _ensure_ws_subscription(
        self,
        symbol: str,
        ticker: Any,
        token_map: dict[str, int],
    ) -> int | None:
        token = token_map.get(symbol)
        if token:
            return int(token)

        try:
            provider = self._ctx.data_service.get_provider("KITE")
            resolver = getattr(provider, "get_instrument_token", None)
            if resolver is None:
                return None
            token = int(resolver(symbol))
            token_map[symbol] = token
            ticker.subscribe([token])
            self._log(f"[TICK-ENTRY] Subscribed {symbol} token={token}")
            return token
        except Exception as exc:
            self._log(f"[TICK-ENTRY] WebSocket subscribe skipped for {symbol}: {exc}", "warning")
            return None

    def _poll_ltp(
        self,
        arms: list[dict],
        lock: threading.Lock,
        entered_flag: list[bool],
        timeout: float,
        mode: str,
    ) -> None:
        deadline = time.monotonic() + timeout
        poll_interval = self._cfg.poll_interval

        while time.monotonic() < deadline:
            remaining_arms = [a for a in arms if not a["fired"]]
            if not remaining_arms:
                break

            for arm in remaining_arms:
                ltp = self._get_ltp(arm["symbol"])
                if not ltp:
                    continue

                direction = arm["direction"]
                crossed = (direction == "BUY" and ltp >= arm["trigger"]) or (
                    direction == "SELL" and ltp <= arm["trigger"]
                )
                if crossed:
                    with lock:
                        if arm["fired"] or self._ctx.positions.get(arm["symbol"]):
                            continue
                        arm["fired"] = True
                    self._log(
                        f"[TICK-ENTRY] LTP trigger: {arm['symbol']} @ {ltp:.2f} "
                        f"(trigger={arm['trigger']:.2f})"
                    )
                    ok = self._fire(arm["candidate"], ltp, mode)
                    if ok:
                        entered_flag[0] = True

            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # LTP fetch (WebSocket cache → REST fallback)
    # ------------------------------------------------------------------

    def _get_ltp(self, symbol: str) -> float | None:
        # 1. WebSocket cache
        ticker = getattr(self._ctx, "_ticker_manager", None)
        if ticker and getattr(ticker, "is_connected", lambda: False)():
            token_map = getattr(self._ctx, "_symbol_token_map", {})
            token = self._ensure_ws_subscription(symbol, ticker, token_map)
            if token:
                ltp = ticker.get_ltp(int(token))
                if ltp:
                    return ltp
        # 2. REST quote
        try:
            from executor import get_quote
            provider = getattr(self._ctx.config, "data_provider", None)
            q = get_quote(symbol, provider=provider)
            return float(q.last_price) if q and q.last_price else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Fire entry
    # ------------------------------------------------------------------

    def _fire(self, candidate: dict, live_price: float, mode: str) -> bool:
        """
        Patch the candidate with the live price and call _execute_single_entry.
        For PAPER mode executor.place_order returns a synthetic OrderResult
        (see executor.py paper_fill patch), so positions are tracked correctly.
        """
        from orchestration.signal_workflow import should_enter_trade
        from orchestration.session import (
            _execute_single_entry,
            get_deployed_capital,
            persist_runtime_state,
        )

        symbol = candidate.get("symbol", "")
        if symbol in self._ctx.positions:
            self._log(f"[TICK-ENTRY] {symbol} already positioned — skip")
            return False

        patched = deepcopy(candidate)
        patched["latest_close"] = live_price

        # Re-validate profitability at the live price
        probe = deepcopy(patched)
        if not should_enter_trade(probe, self._ctx, entry_price=live_price):
            self._log(f"[TICK-ENTRY] {symbol} not profitable at {live_price:.2f} — skip")
            return False

        deployed = get_deployed_capital(self._ctx.positions)
        fake_cycle_state = {"manage_positions": False, "allow_entries": True}
        now = datetime.now()
        try:
            ok = _execute_single_entry(self._ctx, patched, now, deployed, fake_cycle_state)
        except Exception as exc:
            self._log(f"[TICK-ENTRY] Entry error for {symbol}: {type(exc).__name__}: {exc}", "error")
            return False

        if ok:
            persist_runtime_state(self._ctx)
            self._log(f"[TICK-ENTRY] Position opened: {symbol} @ {live_price:.2f} [{mode}]")
        return ok


# ---------------------------------------------------------------------------
# Backtest helper (no network, no context — called directly from BacktestEngine)
# ---------------------------------------------------------------------------

def backtest_tick_fill(
    candidate: dict,
    candle_open: float,
    candle_high: float,
    candle_low: float,
) -> float | None:
    """
    Given the OHLC of the signal candle, return the tick-simulated fill price
    (i.e. the price we *would* have entered at, earlier in the candle).

    Returns None if the trigger was NOT touched in this candle — meaning we
    should NOT enter at all on this candle (wait for next bar's open instead,
    which is the standard fallback).

    Logic
    -----
    BUY : trigger = close - ATR*0.40
          high >= trigger → touched; fill = max(candle_open, trigger)
    SELL: trigger = close + ATR*0.40
          low  <= trigger → touched; fill = min(candle_open, trigger)
    """
    from tick_entry.levels import compute_backtest_fill

    direction = str(candidate.get("signal") or "BUY").upper()
    close = float(candidate.get("latest_close") or 0.0)
    atr   = float(candidate.get("atr") or 0.0)
    shift = atr * 0.40 if atr > 0 else close * 0.002

    if direction == "BUY":
        trigger = close - shift
        if candle_high < trigger:
            return None           # price never touched the trigger inside this candle
        fill = compute_backtest_fill(candidate, candle_open)
        return fill
    else:
        trigger = close + shift
        if candle_low > trigger:
            return None
        fill = compute_backtest_fill(candidate, candle_open)
        return fill
