"""
Breakout trigger-level computation.

For every candidate that came from a closed-candle signal, this module
derives the *intra-candle* price that must be touched for us to enter
early — instead of waiting for the next candle to close.

LIVE / PAPER
------------
trigger_price is set just ABOVE the candle close (BUY) or just BELOW (SELL)
so that we only enter when price continues past the signal close, confirming
momentum rather than chasing a reversal.

Buffer = 10 % of ATR (typically 5-15 paise on NSE mid-caps).  This is small
enough not to miss the move but large enough to filter noise ticks.

BACKTEST
--------
We want to model "entered during the signal candle at the breakout point".
The fill price is max(candle_open, close − ATR*0.4) for BUY so we never
fill above the close (we can't know what the close was while the candle
is forming) and we favour a price in the lower-half of the candle's range.
"""
from __future__ import annotations


def compute_live_trigger(candidate: dict) -> float:
    """
    Live-session trigger: price must push a tiny step beyond the closed candle.

    BUY  → trigger = close + buffer   (price continuing up)
    SELL → trigger = close − buffer   (price continuing down)
    """
    close = float(candidate.get("latest_close") or 0.0)
    atr   = float(candidate.get("atr") or 0.0)
    # 10 % of ATR; fall back to 0.05 % of price when ATR is absent
    buffer = atr * 0.10 if atr > 0 else close * 0.0005
    direction = str(candidate.get("signal") or "BUY").upper()
    if direction == "BUY":
        return round(close + buffer, 2)
    return round(close - buffer, 2)


def compute_backtest_fill(candidate: dict, candle_open: float) -> float:
    """
    Backtest fill price: model "we entered during the signal candle,
    roughly at the breakout level, no later than candle close".

    For BUY:  fill = max(candle_open, close − ATR*0.4)
              → enters somewhere in the lower half of the candle range
    For SELL: fill = min(candle_open, close + ATR*0.4)
              → enters somewhere in the upper half of the candle range

    This is conservative: we never give ourselves a fill better than
    candle_open (we assume we weren't watching at the exact tick zero).
    """
    close = float(candidate.get("latest_close") or 0.0)
    atr   = float(candidate.get("atr") or 0.0)
    shift = atr * 0.40 if atr > 0 else close * 0.002   # ~0.2 % fallback
    direction = str(candidate.get("signal") or "BUY").upper()
    if direction == "BUY":
        # earlier fill = close minus shift, but not better than open
        early = close - shift
        return round(max(candle_open, early), 2)
    else:
        early = close + shift
        return round(min(candle_open, early), 2)
