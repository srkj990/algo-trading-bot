# Bugs and Fixes

Track record of bugs found and fixes applied for audit and future reference.

---

## [2026-05-31] TRAILING_STOP exits producing net losses

### Symptom
Backtest results showed TRAILING_STOP exits firing within 1–3 candles of entry with near-zero gross P&L, resulting in net losses after transaction charges (~₹29–52 per trade).

Example trades (ATM_MOMENTUM, May 27 2026):
| Symbol | Entry | Exit | Gross P&L | Net P&L |
|--------|-------|------|-----------|---------|
| 23900CE | 179.45 | 179.65 | +13 | **-20** |
| 23900CE | 186.6 | 184.8 | -117 | **-152** |
| 23950CE | 184.15 | 184.35 | +13 | **-21** |
| 23950PE | 152.9 | 152.55 | -23 | **-51** |
| 23900PE | 140.0 | 140.30 | +39 | **-14** |

### Root Cause

**Bug 1 — `models/position.py:update_trailing_stop()`**

The trailing activation distance was set to `max(trailing_distance, level1_distance * 0.8)`. With typical NIFTY options ATR (~4–7), `trailing_distance = ATR * 1.0` and `level1_distance = ATR * 0.85`, so `trailing_activation_distance = ATR * 1.0`. When trailing activates at `best_price = entry + ATR`, the computed candidate is `(entry + ATR) - ATR = entry` — exactly breakeven. Any slippage or small adverse tick → TRAILING_STOP fires at a net loss.

The trailing stop candidate was never clamped to be above the entry price, so activation placed the trailing stop at or below breakeven.

**Bug 2 — `tick_exit/monitor.py:_fire_exit()`**

The TRAILING_STOP staleness check did not verify `trailing_active`. A WebSocket breakout arm that fired before `trailing_active=True` was confirmed could bypass the activation guard.

### Fix

**`models/position.py` — `update_trailing_stop()`**
Added breakeven clamp after computing the trailing stop candidate:
```python
# BUY side:
candidate = max(candidate, self.entry_price)   # never below breakeven
# SELL side:
candidate = min(candidate, self.entry_price)   # never above breakeven
```
This guarantees the trailing stop never activates at a level that produces a loss exit, regardless of ATR multiplier config. As price continues to move favorably beyond activation, the trailing stop ratchets above entry to lock in real profit.

**`tick_exit/monitor.py` — `_fire_exit()`**
Added `trailing_active` guard before the TRAILING_STOP exit path:
```python
if not position.get("trailing_active"):
    return  # trailing not yet activated — reject stale arm
```

### Files Changed
- `models/position.py`
- `tick_exit/monitor.py`

### Commit
`fix: clamp trailing stop to breakeven on activation; guard tick-exit trailing_active`

---

## [2026-05-31] Backtest crashes on Sunday — `get_underlying_bias()` live API call

### Symptom
Running a 5-day backtest on Sunday would crash with `RuntimeError: No underlying data for NIFTY`. Zero trades would be produced despite valid pre-fetched historical data.

### Root Cause
`engines/intraday_options.py:get_underlying_bias()` made a live Kite API call (`get_data(period="2d", interval="1m", provider="KITE")`) when `underlying_df=None`. On Sunday, markets are closed and Kite returns 0 rows for this query, causing the RuntimeError.

Additionally, `apply_signal_filters()` called `get_underlying_bias()` unconditionally when `prefetched_underlying_df=None` (non-ATM-scan path), even during backtests.

### Fix

**`engines/intraday_options.py` — `get_underlying_bias()`**
Removed the live API call. Now returns `{"bias": "NEUTRAL", ...}` immediately when `underlying_df` is None or empty, instead of fetching live data:
```python
def get_underlying_bias(self, underlying, underlying_df=None):
    if underlying_df is None or underlying_df.empty:
        return {"bias": "NEUTRAL", "close": 0.0, "vwap": 0.0, "ema": 0.0}
```

**`engines/intraday_options.py` — `apply_signal_filters()`**
Added `prefetched_underlying_df is not None` guard before calling `get_underlying_bias()`, so the underlying bias filter is skipped entirely when no data is available (rather than applying a blocking NEUTRAL that would reject all entries):
```python
if analytics and not analytics.get("skip_underlying_bias") and prefetched_underlying_df is not None:
```

### Files Changed
- `engines/intraday_options.py`

### Commit
`fix: get_underlying_bias returns NEUTRAL when no data available; skip bias filter when prefetched_underlying_df is None`

---

## [2026-05-31] Intra-candle trailing stop ratchet — backtest used candle Close instead of High/Low

### Symptom
In backtests, if a candle had a high spike followed by a crash (e.g. Open=125, High=155, Low=80, Close=82), the trailing stop was only ratcheted using the Close price (82), not the High (155). This meant the trailing stop never captured the spike, and the backtest exit price was worse than what live trading would produce.

### Root Cause
`backtesting.py:_manage_intraday_options_position()` called `update_trailing_stop(position, float(latest_close), ...)`, only ratcheting with the candle close. The candle High was never used.

### Fix
Changed to ratchet the trailing stop with candle **High** (BUY) or **Low** (SELL) before evaluating exits, simulating intra-candle price movement:
```python
if position_side(position) == "BUY":
    update_trailing_stop(position, float(latest_candle.get("High", latest_close)), trailing_distance)
else:
    update_trailing_stop(position, float(latest_candle.get("Low", latest_close)), trailing_distance)
```

### Files Changed
- `backtesting.py`

### Commit
`feat: intra-candle WebSocket exit monitoring + trailing ratchet` (commit b597ad5)
