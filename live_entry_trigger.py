"""
Tick-based / LTP-poll entry trigger for intraday strategies.

Problem solved
--------------
The polling loop fetches 1-minute candles every 60 seconds.  When a large
breakout candle forms, entry only happens at the NEXT candle's open — missing
the entire move.  This module watches the live price (via KiteConnect WebSocket
ticks or REST LTP polling) and fires an entry as soon as the price crosses the
breakout level, without waiting for the candle to close.

How it fits into the session loop
----------------------------------
1. After the normal scan cycle produces `ranked_candidates`, call
   `arm_tick_entries(context, candidates, remaining_seconds)`.
2. For each unpositioned candidate that has a computable breakout level, an arm
   is registered.
3. The function blocks for `remaining_seconds` (up to 55 s), polling or
   receiving ticks.  The first candidate whose live price crosses its trigger
   level fires an entry immediately via `context.place_order`.
4. If no candidate triggers before the timeout the session loop continues
   normally into its next 60-second cycle.

Breakout-level computation
--------------------------
The trigger price is computed conservatively as `latest_close ± (ATR * 0.10)`
so that we don't re-enter a signal we already evaluated (we need the price to
push a little further from the current candle close before committing).

For ORB / BREAKOUT strategies the signal already fired on a closed candle, so
`latest_close` *is* the breakout level.  The tiny ATR buffer prevents noise
entries.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from logger import log_event as _global_log


@dataclass
class TickEntryArm:
    symbol: str
    direction: str            # "BUY" or "SELL"
    trigger_price: float      # live price that activates entry
    candidate: dict[str, Any]
    fired: bool = field(default=False, init=False)


def compute_trigger_price(candidate: dict[str, Any]) -> float:
    """
    Derive an intra-candle trigger level from the signal candidate.

    For a BUY: slightly above the last candle close so we only enter on
    continued upward momentum.
    For a SELL: slightly below.

    The buffer is 10% of ATR (very small — typically 5-15 paise on equities)
    so as not to miss the move while still filtering micro-noise.
    """
    close = float(candidate.get("latest_close") or 0.0)
    atr = float(candidate.get("atr") or 0.0)
    buffer = atr * 0.10 if atr > 0 else close * 0.0005  # fallback 0.05%
    direction = str(candidate.get("signal") or "BUY").upper()
    if direction == "BUY":
        return round(close + buffer, 2)
    return round(close - buffer, 2)


def _get_live_ltp(context: Any, symbol: str) -> float | None:
    """
    Read LTP from WebSocket cache first; fall back to REST quote if unavailable.
    """
    # Try WebSocket ticker
    ticker = getattr(context, "_ticker_manager", None)
    if ticker and ticker.is_connected():
        token = getattr(context, "_symbol_token_map", {}).get(symbol)
        if token:
            ltp = ticker.get_ltp(int(token))
            if ltp:
                return ltp

    # Fallback: REST quote (rate-limited)
    try:
        from executor import get_quote
        quote = get_quote(symbol, provider=getattr(context.config, "data_provider", None))
        return float(quote.last_price) if quote and quote.last_price else None
    except Exception:
        return None


def arm_tick_entries(
    context: Any,
    candidates: list[dict[str, Any]],
    remaining_seconds: float = 50.0,
    poll_interval: float = 8.0,
) -> bool:
    """
    Monitor live prices for `candidates` and fire an entry if any crosses its
    trigger level before `remaining_seconds` elapses.

    Returns True if at least one entry was fired.
    """
    if not candidates or remaining_seconds <= 0:
        return False

    now_fn: Callable[[], datetime] = datetime.now
    log = getattr(context, "log_event", _global_log)
    entered = False
    entry_lock = threading.Lock()

    arms: list[TickEntryArm] = []
    for cand in candidates:
        sym = cand.get("symbol", "")
        direction = str(cand.get("signal") or "BUY").upper()
        if direction not in {"BUY", "SELL"}:
            continue
        trigger = compute_trigger_price(cand)
        if trigger <= 0:
            continue
        arms.append(TickEntryArm(symbol=sym, direction=direction, trigger_price=trigger, candidate=cand))
        log(f"[TICK-ENTRY] Armed {sym} {direction} trigger={trigger:.2f} (candle_close={cand.get('latest_close', 0):.2f})")

    if not arms:
        return False

    ticker = getattr(context, "_ticker_manager", None)
    use_websocket = ticker and ticker.is_connected()

    if use_websocket:
        _arm_via_websocket(context, arms, entry_lock, now_fn, log)
        time.sleep(min(remaining_seconds, 55.0))
        # disarm any remaining
        for arm in arms:
            if not arm.fired:
                token = getattr(context, "_symbol_token_map", {}).get(arm.symbol)
                if token:
                    ticker.disarm_all(int(token))
        entered = any(a.fired for a in arms)
    else:
        entered = _arm_via_ltp_poll(context, arms, entry_lock, now_fn, log, remaining_seconds, poll_interval)

    return entered


# ---------------------------------------------------------------------------
# WebSocket path
# ---------------------------------------------------------------------------

def _arm_via_websocket(
    context: Any,
    arms: list[TickEntryArm],
    entry_lock: threading.Lock,
    now_fn: Callable,
    log: Callable,
) -> None:
    ticker = context._ticker_manager
    token_map: dict[str, int] = getattr(context, "_symbol_token_map", {})

    def make_callback(arm: TickEntryArm):
        def callback(token: int, ltp: float) -> None:
            with entry_lock:
                if arm.fired:
                    return
                arm.fired = True
            log(f"[TICK-ENTRY] TRIGGERED {arm.symbol} @ {ltp:.2f} (trigger={arm.trigger_price:.2f}) via WebSocket")
            _fire_entry(context, arm.candidate, ltp, now_fn(), log)
        return callback

    for arm in arms:
        token = token_map.get(arm.symbol)
        if not token:
            continue
        ticker.arm_breakout(int(token), arm.trigger_price, arm.direction, make_callback(arm))


# ---------------------------------------------------------------------------
# LTP polling path (fallback)
# ---------------------------------------------------------------------------

def _arm_via_ltp_poll(
    context: Any,
    arms: list[TickEntryArm],
    entry_lock: threading.Lock,
    now_fn: Callable,
    log: Callable,
    max_wait: float,
    poll_interval: float,
) -> bool:
    deadline = time.monotonic() + max_wait
    entered = False

    while time.monotonic() < deadline:
        remaining_arms = [a for a in arms if not a.fired]
        if not remaining_arms:
            break

        for arm in remaining_arms:
            ltp = _get_live_ltp(context, arm.symbol)
            if not ltp:
                continue
            direction = arm.direction
            crossed = (direction == "BUY" and ltp >= arm.trigger_price) or (
                direction == "SELL" and ltp <= arm.trigger_price
            )
            if crossed:
                with entry_lock:
                    if arm.fired:
                        continue
                    arm.fired = True
                log(f"[TICK-ENTRY] TRIGGERED {arm.symbol} @ {ltp:.2f} (trigger={arm.trigger_price:.2f}) via LTP poll")
                _fire_entry(context, arm.candidate, ltp, now_fn(), log)
                entered = True

        time.sleep(poll_interval)

    return entered


# ---------------------------------------------------------------------------
# Entry execution
# ---------------------------------------------------------------------------

def _fire_entry(
    context: Any,
    candidate: dict[str, Any],
    live_price: float,
    now: datetime,
    log: Callable,
) -> None:
    """
    Place the entry order using the live (tick) price instead of the candle close.
    Reuses the same guard-rail checks as the normal entry path.
    """
    from orchestration.signal_workflow import should_enter_trade
    from orchestration.session import (
        _execute_single_entry,
        get_deployed_capital,
        persist_runtime_state,
    )

    symbol = candidate.get("symbol", "")

    # Skip if already in a position (race-condition guard)
    if symbol in context.positions:
        log(f"[TICK-ENTRY] {symbol} already in positions — skipping tick entry")
        return

    # Override entry price with the live tick price
    patched = dict(candidate)
    patched["latest_close"] = live_price

    # Re-validate profitability at the live price
    from copy import deepcopy
    signal_copy = deepcopy(patched)
    if not should_enter_trade(signal_copy, context, entry_price=live_price):
        log(f"[TICK-ENTRY] {symbol} not profitable at live price {live_price:.2f} — skipping")
        return

    deployed_capital = get_deployed_capital(context.positions)
    fake_cycle_state: dict = {"manage_positions": False, "allow_entries": True}
    try:
        entered = _execute_single_entry(context, patched, now, deployed_capital, fake_cycle_state)
        if entered:
            persist_runtime_state(context)
            log(f"[TICK-ENTRY] Entry placed for {symbol} @ live price {live_price:.2f}")
        else:
            log(f"[TICK-ENTRY] Entry attempt for {symbol} returned False (limit/guard hit)")
    except Exception as exc:
        log(f"[TICK-ENTRY] Entry error for {symbol}: {type(exc).__name__}: {exc}", "error")
