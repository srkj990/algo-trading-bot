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

---

## [2026-05-31] ATM_MULTI loss optimisation — config tuning

### Symptom
Backtest `backtest_intraday_options_20260530_220205` (ATM_MULTI, AGGRESSIVE, 5 days, ₹1L):
- 34 trades, 41% win rate, Net P&L = −₹9,520 (−9.5%), Max DD = 12.05%
- 26/34 exits (76%) via STOP_LOSS — all net losses, avg −₹710 each
- BUY_CE entries: 12.5% win rate (8 trades, 1 winner) on a non-trending day
- Trailing stop activating at exactly breakeven → TRAILING_STOP exits with net losses

### Root Cause
1. Stop multiplier ATR×1.7 too tight — hit by 1-candle normal volatility, not reversals
2. R:R was 1:1 (stop = target = ATR×1.7) — insufficient for 41% win rate
3. Trailing distance ATR×1.0 = activation threshold → trailing stop lands at entry (breakeven) even with the new breakeven clamp; needs to be narrower than activation to lock in profit
4. EXPANSION regime threshold (1.1%) too loose — fired CE entries on May 26 (descending day)
5. `consecutive_loss_limit=4` allowed 4-loss streaks costing ₹2,803

### Fix
Config-only changes in `config/config.runtime.yaml`:

| Parameter | Before | After |
|-----------|--------|-------|
| `adaptive_stop_multiplier_normal` | 1.7 | 2.2 |
| `adaptive_stop_multiplier_sideways` | 2.0 | 2.5 |
| `adaptive_stop_multiplier_expansion` | 1.7 | 2.0 |
| `adaptive_target_multiplier_normal` | 1.7 | 2.5 |
| `adaptive_target_multiplier_sideways` | 1.1 | 1.5 |
| `adaptive_target_multiplier_expansion` | 2.3 | 3.0 |
| `adaptive_trailing_multiplier_normal` | 1.0 | 0.7 |
| `adaptive_trailing_multiplier_sideways` | 0.8 | 0.6 |
| `adaptive_trailing_multiplier_expansion` | 1.15 | 0.8 |
| `adaptive_min_stop_pct` | 0.05 | 0.07 |
| `consecutive_loss_limit` | 4 | 3 |
| `intraday_options_max_entry_cost_ratio` | 0.30 | 0.20 |
| `intraday_options_regime_expansion_range_pct` | 1.1 | 1.4 |

Key effect: tighter trailing (ATR×0.7) vs activation (ATR×1.0) means trailing stop activates at entry+0.3×ATR profit instead of breakeven.

### Files Changed
- `config/config.runtime.yaml`

### Commit
`config: widen stops, raise targets, tighten trailing for intraday_options loss reduction` (commit c5d1fff)

---

## [2026-05-31] UI: lot mode selector + auto capital-per-trade

### Symptom
`intraday_options_lot_mode` was only settable by editing `config.runtime.yaml`. Users starting sessions from the web UI had no way to switch between ONE_LOT and CAPITAL_BASED sizing. Also, `max_capital_per_trade` field showed a static ₹50,000 default that did not update when capital or max_open_positions changed.

### Fix
- Added Lot Mode dropdown to web UI for intraday_options engine (live and backtest tabs), shown only when engine = Intraday Options
- Added `oninput="autoCapPerTrade(pfx)"` to capital and max-pos inputs — auto-computes `max_capital_per_trade = floor(capital / max_open_positions)` on each keystroke
- `cli/configuration.py` and `web/routes/config.py`: prefer `intraday_options_lot_mode` from the form payload over `runtime_config.fno` default

### Files Changed
- `web/static/index.html`
- `cli/configuration.py`
- `web/routes/config.py`

### Commit
`feat: add lot mode selector and auto capital-per-trade to UI` (commit 9e05110)

---

## [2026-05-31] Zero trades on small capital — max_symbol_allocation blocks intraday_options

### Symptom
Backtest `backtest_intraday_options_20260531_203440` with capital=₹20,000: **0 trades executed** despite valid signals.

### Root Cause
`engines/intraday_options.py:apply_entry_allocation_limit()` called `super()` which applies `max_symbol_allocation: 0.2` (20% of capital cap per symbol) **on top of** the already-capital-bounded quantity.

Execution chain for ₹20,000 capital, entry=₹210:
1. `qty = int(20000 / 210) = 95` (capital-bounded)
2. `super().apply_entry_allocation_limit()`: `max_symbol_capital = 20000 × 0.2 = ₹4,000`
3. `symbol_cap_qty = int(4000 / 210) = 19`
4. `capped = min(95, 19) = 19`
5. Lot rounding: `(19 // 75) * 75 = 0` → **trade skipped**

Blocking condition: `entry_price > capital × max_symbol_allocation / lot_size` → `entry_price > 20000 × 0.2 / 75 = ₹53.33` — triggered by every normal premium entry.

### Fix
`engines/intraday_options.py:apply_entry_allocation_limit()` — removed `super()` call. Capital is already bounded by `max_capital_per_trade` before this method is called; the `max_symbol_allocation` cap is redundant and harmful for lot-based contracts. Now only lot-size rounding is applied:
```python
lot_size = get_contract_lot_size(symbol)
return (quantity // lot_size) * lot_size
```

Minimum capital to trade = `lot_size × entry_price` (e.g. ₹75 × ₹200 = ₹15,000 for one NIFTY lot at ₹200 premium).

### Files Changed
- `engines/intraday_options.py`

### Commit
`fix: remove max_symbol_allocation cap from intraday_options apply_entry_allocation_limit` (commit 09999ae)

---

## [2026-05-31] consecutive_loss_limit mismatch — config.py default stale after yaml change

### Symptom
`config/config.runtime.yaml` was updated to `consecutive_loss_limit: 3` (from 4) as part of the loss-optimisation change, but `config.py` still had `os.getenv("RISK_CONSECUTIVE_LOSS_LIMIT", "4")` as the fallback default. Any deployment using the env-var path (no yaml file present, or yaml not loaded) would silently use 4 instead of 3.

### Root Cause
Two sources of truth for the same default: `config/config.runtime.yaml` (authoritative for running sessions) and `config.py` `_default_runtime_config_map()` (used as base before yaml merges). When the yaml was updated, the code default was not kept in sync.

### Fix
Updated `config.py` env default from `"4"` to `"3"`:
```python
"consecutive_loss_limit": int(os.getenv("RISK_CONSECUTIVE_LOSS_LIMIT", "3")),
```

### Files Changed
- `config.py`

### Commit
`fix: sync consecutive_loss_limit default in config.py from 4 to 3` (commit d7c224e)

---

## [2026-05-31] Backtest ignores consecutive_loss_limit and daily_max_loss_pct

### Symptom
May 27 backtest data shows 4 consecutive STOP_LOSS trades after an earlier TRAILING_STOP loss:

| Entry | Exit | Reason | Net P&L | Consec losses at entry |
|-------|------|--------|---------|----------------------|
| 10:18 | 10:38 | TRAILING_STOP | −₹16 | 1 |
| 10:38 | 10:40 | STOP_LOSS | −₹629 | 2 |
| 10:56 | 11:00 | STOP_LOSS | −₹674 | **3 → limit** |
| 11:07 | 11:11 | STOP_LOSS | −₹767 | should be blocked |
| 11:19 | 11:36 | STOP_LOSS | −₹632 | should be blocked |
| 14:37 | 14:38 | STOP_LOSS | −₹967 | should be blocked |

With `consecutive_loss_limit=3`, trades 4-6 should have been blocked. The live session would have stopped at trade 3. The backtest produced −₹2,366 in avoidable losses.

### Root Cause
`backtesting.py:_enter_ranked_candidates()` had no `consecutive_loss_limit` or `daily_max_loss_pct` check. The live session check in `orchestration/session.py` (line 732) was never mirrored in the backtest, causing backtest P&L to diverge from live trading behaviour.

### Fix
Added `_consecutive_losses_today()` helper and two early-return guards at the top of `_enter_ranked_candidates()` in `backtesting.py`:
```python
# Consecutive loss guard
if consec_limit > 0 and self._consecutive_losses_today(trade_day) >= consec_limit:
    return  # block all new entries for the rest of this candle cycle

# Daily max loss guard
if day_pnl < -(starting_equity * daily_max_loss_pct):
    return
```
Both guards mirror the exact logic in `session.py` so backtest and live results are now consistent.

### Files Changed
- `backtesting.py`
- `tests/unit/test_session_entry_sizing.py` (updated expected qty after max_symbol_allocation removal)

### Commit
`fix: enforce consecutive_loss_limit and daily_max_loss_pct in backtest` (commit 5bdc614)

---

## [2026-05-31] intraday_options risk style has no effect — BALANCED and AGGRESSIVE produce identical P&L

### Symptom
Backtests run with BALANCED vs AGGRESSIVE risk style for `intraday_options` engine produced identical P&L. Switching risk style had no observable effect on stop distance, target distance, or trailing behaviour.

### Root Cause
`engines/intraday_options.py:get_trend_adaptive_level_spec()` uses class-level `_adaptive_stop`, `_adaptive_target`, `_adaptive_trailing` dicts populated from `config.runtime.yaml` (`adaptive_stop_multiplier_normal`, etc.). These yaml values are identical regardless of which risk style is selected.

`build_trend_adaptive_position()` was called without a `risk_style_name` parameter from both the backtest path (`backtesting.py:920`) and the live entry path (`orchestration/session.py:1454`). The `risk_style_name` only reached `calculate_cost_aware_targets()` inside `resolve_trade_targets()`, which is used purely for cost/profit metadata — its stop/target/trailing outputs are entirely overwritten by `build_trend_adaptive_position()`.

Result: `ASSET_CLASS_RISK_PROFILES["INTRADAY_OPTIONS"]["BALANCED/AGGRESSIVE"]["sl_percent"]` values were never applied to the actual stop, target, or trailing levels.

### Fix
**`engines/intraday_options.py` — `get_trend_adaptive_level_spec()`**

Added `risk_style_name="BALANCED"` parameter. Computes per-axis scale factors relative to the BALANCED baseline using `ASSET_CLASS_RISK_PROFILES["INTRADAY_OPTIONS"]`:
```python
sl_scale = chosen["sl_percent"] / base["sl_percent"]          # AGGRESSIVE: 12/10 = 1.2
target_scale = chosen["target_percent"] / base["target_percent"]  # AGGRESSIVE: 20/15 = 1.333
trailing_scale = chosen["trailing_percent"] / base["trailing_percent"]  # AGGRESSIVE: 6/4.8 = 1.25
stop_multiplier = self._adaptive_stop[regime] * sl_scale
target_multiplier = self._adaptive_target[regime] * target_scale
trailing_multiplier = self._adaptive_trailing[regime] * trailing_scale
```

**`engines/intraday_options.py` — `build_trend_adaptive_position()`**
Added `risk_style_name="BALANCED"` parameter and passes it through to `get_trend_adaptive_level_spec()`.

**`orchestration/session.py`**
Passes `risk_style_name=cfg.risk_style_name` to `build_trend_adaptive_position()` at the live entry path.

**`backtesting.py`**
Passes `risk_style_name=self.config.risk_style_name` to `build_trend_adaptive_position()` at the backtest entry path.

### Effect
| Style | Stop (NORMAL) | Target (NORMAL) | Trailing (NORMAL) |
|-------|--------------|-----------------|-------------------|
| CONSERVATIVE | ATR × 1.76 | ATR × 2.00 | ATR × 0.583 |
| BALANCED | ATR × 2.20 | ATR × 2.50 | ATR × 0.700 |
| AGGRESSIVE | ATR × 2.64 | ATR × 3.33 | ATR × 0.875 |

CONSERVATIVE cuts losses faster (tighter stop, shorter target). AGGRESSIVE lets winners run further and accepts wider individual losses. The yaml regime-adaptive multipliers remain the calibration base; the risk style scales them proportionally.

### Files Changed
- `engines/intraday_options.py`
- `orchestration/session.py`
- `backtesting.py`

### Commit
`fix: apply risk style scaling to intraday_options stop/target/trailing levels`

---

## [2026-05-31] CONSERVATIVE risk style blocks more intraday_options entries than expected

### Symptom
Switching to CONSERVATIVE risk style produced significantly fewer trades than BALANCED or AGGRESSIVE. Risk style is supposed to affect exit levels and position sizing, not entry frequency.

### Root Cause
`orchestration/signal_workflow.py:should_enter_trade()` calls `resolve_trade_targets()` which calls `calculate_cost_aware_targets()` using `ASSET_CLASS_RISK_PROFILES["INTRADAY_OPTIONS"][risk_style_name]`. For CONSERVATIVE the profile has `target_percent=12%` vs BALANCED `15%`.

A smaller target percentage → smaller expected gross profit → the fixed transaction costs represent a larger fraction → `cost_to_profit_ratio` exceeds `intraday_options_max_entry_cost_ratio=0.20` for more trades → entry is blocked.

But the **actual** target at runtime is ATR-based (from `get_trend_adaptive_level_spec()`), not a percentage. The profitability check was using a profile percentage (12%) that has no relation to the actual exit price the trade will use (ATR × 2.0 at CONSERVATIVE scaling ≈ ₹10 on a ₹180 entry = 5.5%).

### Fix
**`orchestration/signal_workflow.py` — `should_enter_trade()`**

For `intraday_options`, after `resolve_trade_targets()` computes cost metadata, recalculate `is_profitable`, `cost_to_profit_ratio`, `expected_gross_profit`, `expected_net_profit` using the real ATR-based target from `get_trend_adaptive_level_spec()`:
```python
level_spec = context.engine.get_trend_adaptive_level_spec(
    entry_price=..., atr=..., analytics=..., risk_style_name=..., ...
)
real_target = level_spec["level3_target"]
gross = abs(real_target - entry_price) * quantity
targets["is_profitable"] = (gross - costs) > 0
targets["cost_to_profit_ratio"] = costs / gross
```

This means the entry filter now uses the same target the trade will actually aim for, so CONSERVATIVE entries are evaluated correctly against their real ATR-based profit potential — not a stale profile percentage.

### Files Changed
- `orchestration/signal_workflow.py`

### Commit
`fix: use ATR-based target in should_enter_trade profitability check for intraday_options`
