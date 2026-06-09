# Intraday Options Engine — Complete Feature Reference

> **Engine:** `intraday_options` (Engine 6)  
> **Data:** 1-day history, 1-minute candles  
> **Instruments:** NSE NIFTY (NFO) and BSE SENSEX (BFO) ATM options  
> **Order product:** MIS (intraday)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Session Lifecycle](#2-session-lifecycle)
3. [Underlying & Contract Selection](#3-underlying--contract-selection)
4. [Lot Sizing Modes](#4-lot-sizing-modes)
5. [Volatility Regime Detection](#5-volatility-regime-detection)
6. [Signal Filters (Entry Guards)](#6-signal-filters-entry-guards)
7. [Strategies](#7-strategies)
8. [Entry Modes](#8-entry-modes)
9. [Exit System — Stop Loss, Target, Trailing](#9-exit-system--stop-loss-target-trailing)
10. [Partial Exits (Runner System)](#10-partial-exits-runner-system)
11. [Partial Exits ON vs OFF — Trailing Interaction](#11-partial-exits-on-vs-off--trailing-interaction)
12. [Risk Controls](#12-risk-controls)
13. [Multi-Strategy Mode](#13-multi-strategy-mode)
14. [All Configurable Parameters](#14-all-configurable-parameters)
15. [Backtest Performance Summary](#15-backtest-performance-summary)
16. [Web UI — Live Session Features](#16-web-ui--live-session-features)

---

## 1. Overview

The Intraday Options Engine buys ATM (At-The-Money) CE or PE options on NIFTY or SENSEX intraday, based on directional signals from one or more strategies. It is a momentum/breakout-first engine — all trades are MIS (closed same day), premium-only (no delta-hedging), and sized by available capital.

**Signal flow:**

```
Market data (1m candles)
    → Strategy signal (BUY_CE / BUY_PE / HOLD)
    → Signal filters (regime, delta, IV, score)
    → Entry mode validation (staged / tick-confirm / immediate)
    → Position built with adaptive stop + target + trailing
    → Runner partial exits (if enabled, ≥2 lots)
    → Final exit: trailing stop / time exit / hard stop / square-off
```

---

## 2. Session Lifecycle

| Time Window | Manage Positions | Allow Entries | Notes |
|-------------|-----------------|---------------|-------|
| Weekends | No | No | No trading |
| Before 09:15 | No | No | |
| **09:15 – 15:05** | **Yes** | **Yes** | Full trading window |
| 15:05 – 15:10 | Yes | No | Entry cutoff |
| 15:10 – 15:15 | Yes | No | Time exit cutoff — all positions get time-exit reason |
| **15:15 – 15:30** | **Yes** | **No** | Force square-off all open positions |
| After 15:30 | No | No | |

**Cooldown:** 180 seconds after each entry before next entry is considered.  
**Sleep interval:** 5 seconds between scan cycles.

---

## 3. Underlying & Contract Selection

### Supported underlyings

| Underlying | Exchange | Derivatives Exchange | Lot Size |
|------------|----------|---------------------|----------|
| NIFTY | NSE | NFO | 65 |
| SENSEX | BSE | BFO | 20 |

### ATM strike resolution

At each entry cycle, the engine fetches the live spot price of the underlying and rounds to the nearest strike. The strike can be offset:

| Strike Mode | Offset | Example (NIFTY spot 22,400) |
|-------------|--------|------------------------------|
| ATM | 0 | 22,400 CE or 22,400 PE |
| ATM+1 | +1 strike | 22,450 CE |
| ATM-1 | -1 strike | 22,350 CE |

### Dynamic roll

If the underlying moves more than **2%** (`intraday_options_roll_trigger_pct`) from the entry underlying price, the position is rolled to the new ATM strike automatically (live sessions only).

### Expiry warning

If the selected contract has ≤ **2 days** to expiry (`intraday_options_expiry_warning_days`), a warning is logged but entry is still allowed.

---

## 4. Lot Sizing Modes

### CAPITAL_BASED (default)

```
available_capital = min(cash, max_capital_per_trade, remaining_deployable)
raw_qty = int(available_capital / option_premium)
qty = (raw_qty // lot_size) * lot_size   ← rounded DOWN to nearest lot
```

**Example:** Capital ₹1,00,000 | Premium ₹214 | NIFTY lot size 65

```
raw_qty = int(100000 / 214) = 467
qty = (467 // 65) * 65 = 455   → 7 lots × 65 = 455 units
Cost = 455 × ₹214 = ₹97,370
```

### ONE_LOT

Always enters exactly 1 lot regardless of capital:
```
qty = lot_size   (65 for NIFTY, 20 for SENSEX)
```

---

## 5. Volatility Regime Detection

The engine classifies the current market regime every cycle. This drives which strategies fire, adaptive stop/target multipliers, and entry validation.

### Inputs

```
session_range_pct = (session_high - session_low) / session_open × 100
recent_vwap_deviation = mean(|close - VWAP| / VWAP) over last 8 candles
iv_change_15m_pct = IV change over the last 15 minutes
```

### Classification rules

```
EXPANSION  if  session_range_pct >= 1.4%  OR  iv_change_15m_pct >= 2.0%
SIDEWAYS   if  session_range_pct <= 0.55%  AND  recent_vwap_deviation <= 0.25%
NORMAL     otherwise
```

**Minimum range gate:** If `session_range_pct < 0.35%`, the engine blocks all entries entirely (market too flat).

### Effect on adaptive multipliers

| Regime | Stop multiplier | Target multiplier | Trailing multiplier |
|--------|----------------|-------------------|---------------------|
| SIDEWAYS | 2.5× ATR | 1.5× ATR | 0.6× ATR |
| NORMAL | 2.2× ATR | 2.5× ATR | 0.7× ATR |
| EXPANSION | 2.0× ATR | 3.0× ATR | 0.8× ATR |

In EXPANSION, stops are tighter (less room for noise) and targets are wider (ride the trend). In SIDEWAYS, stops are wider (avoid chop whipsaws) and targets are closer (mean-reversion exits).

---

## 6. Signal Filters (Entry Guards)

Every signal passes through a filter chain before entry is allowed. If any filter fails, the entry is blocked with a reason logged.

### Filter chain (in order)

| # | Filter | Condition to BLOCK | Config key |
|---|--------|--------------------|-----------|
| 1 | Minimum range | `session_range_pct < 0.35%` | `intraday_options_min_range_pct` |
| 2 | Sideways block | Range ≤ 0.35% AND VWAP dev ≤ 0.15% | `intraday_options_sideways_vwap_band_pct` |
| 3 | VWAP alignment | BUY: close ≤ option_VWAP; SELL: close ≥ option_VWAP | — |
| 4 | Underlying bias | CE blocked if underlying bias is not BULLISH | — |
| 5 | Min premium | `option_price < ₹8` | `min_contract_price` |
| 6 | Vega crush | `iv_change_15m_pct ≤ −20%` | `intraday_options_vega_crush_block_percent` |
| 7 | Delta floor | `abs(delta) < 0.2` | `min_abs_delta` |
| 8 | IV percentile (buy) | `iv_percentile > 75%` for buys | `max_buy_iv_percentile` |
| 9 | IV percentile (IV_EXP) | `iv_percentile > 20%` for ATM_IV_EXPANSION | `intraday_options_iv_expansion_max_iv_percentile` |
| 10 | Signal score | `score < 0.03` | `intraday_options_min_signal_score` |
| 11 | Cost/profit ratio | `expected_costs / expected_gross_profit > 0.20` | `intraday_options_max_entry_cost_ratio` |
| 12 | Live spread | `(ask - bid) / mid > 1.5%` | `intraday_options_max_spread_pct` |
| 13 | Open interest | `OI < 10,000` | `intraday_options_min_open_interest` |

### Underlying bias calculation

```
bias = BULLISH   if  close > VWAP  AND  close > EMA(21)
bias = BEARISH   if  close < VWAP  AND  close < EMA(21)
bias = NEUTRAL   otherwise
```

CE (call) entries are blocked unless bias = BULLISH.  
PE (put) entries are blocked unless bias = BEARISH.

---

## 7. Strategies

### Minimum candle requirements

| Strategy | Min candles needed | Earliest possible entry |
|----------|--------------------|------------------------|
| ATM_MOMENTUM | 20 | ~09:35 |
| ATM_ORB | 16 | ~09:31 (after ORB forms) |
| ATM_VWAP_REVERSION | 20 | ~09:35 |
| ATM_MULTI | 20 | ~09:35 |
| ATM_BREAKOUT_EXPANSION | 45 | ~10:00 |
| ATM_IV_EXPANSION | 30 | ~09:45 |
| ATM_TRAP_REVERSAL | 24 | ~09:39 |

---

### ATM_MOMENTUM

**What it does:** Buys calls/puts when price breaks above/below a recent high/low with RSI confirmation and VWAP alignment.

**Signal (BUY_CE):**
```
close > VWAP
AND RSI(14) > 60
AND close > max(High, last 5 candles)   ← breakout_high
```

**Signal (BUY_PE):**
```
close < VWAP
AND RSI(14) < 40
AND close < min(Low, last 5 candles)    ← breakout_low
```

**Strength score:**
```
rsi_component     = (RSI - 60) / 40          [normalized 0–1]
vwap_component    = |close - VWAP| / VWAP / 0.01
breakout_component = |close - level| / close / 0.01
strength = clip(rsi_component + vwap_component + breakout_component, 0, 1)
```

**Example:** NIFTY 09:42 | Close=22,450 | VWAP=22,420 | RSI=67 | 5m high=22,445
```
close > VWAP ✓ (22450 > 22420)
RSI > 60 ✓ (67 > 60)
close > breakout_high ✓ (22450 > 22445)
→ BUY_CE signal, buy 22,450 CE
```

**Best regime:** NORMAL and EXPANSION. Avoid in SIDEWAYS (will generate false breakouts).

---

### ATM_ORB (Opening Range Breakout)

**What it does:** Defines the first 15 minutes as the "opening range" and trades breakouts above/below it.

**Opening range:**
```
orb_high = max(High, candles 1–15)   [09:15 to 09:29]
orb_low  = min(Low,  candles 1–15)
```

**Signal (BUY_CE):** `close > orb_high`  
**Signal (BUY_PE):** `close < orb_low`

**Strength score:**
```
strength = |close - level| / close
```

**Example:** SENSEX 09:32 | orb_high=73,800 | orb_low=73,600 | Current close=73,850
```
close > orb_high ✓ (73850 > 73800)
→ BUY_CE signal, buy 73,800 CE
```

**Important:** The day-boundary guard prevents this strategy from using the prior day's ORB range at 09:15 (a bug that existed earlier and was fixed).

**Best regime:** All regimes — ORB is robust because it derives its levels from today's actual opening price action.

---

### ATM_VWAP_REVERSION

**What it does:** Fades VWAP extensions — waits for price to push far from VWAP, then catches the reversion back through VWAP.

**Signal (BUY_CE):** Price extended below VWAP, now crossing back above
```
max(|(close - VWAP)/VWAP|, last 6 candles) >= 0.35%   ← extended enough
AND previous_close <= previous_VWAP                     ← was below VWAP
AND latest_close >= latest_VWAP                         ← now crossed above
```

**Signal (BUY_PE):** Price extended above VWAP, now crossing back below
```
max(|(close - VWAP)/VWAP|, last 6 candles) >= 0.35%
AND previous_close >= previous_VWAP
AND latest_close <= latest_VWAP
```

**Strength score:**
```
strength = extreme_deviation / 0.0035
strength = clip(strength, 0, 1)
```

**Example:** NIFTY 10:15 | VWAP=22,400
- 10:10 close=22,345 (deviation = −0.25%)
- 10:11 close=22,348
- 10:15 close=22,405 (crossed above VWAP)
- max deviation last 6 candles = 0.25% ≥ 0.35%? No → no signal
- If deviation was 22,320 (0.36%) → BUY_CE signal

**Best regime:** SIDEWAYS only. **Blocked in EXPANSION** (can't fade aggressive trends).

---

### ATM_MULTI

**What it does:** Hybrid — combines Momentum + ORB for trending conditions, VWAP Reversion for sideways conditions.

**Decision tree:**
```
1. Compute momentum_signal, orb_signal, vwap_signal independently

2. IF momentum_signal == orb_signal (both agree on direction):
       signal = that direction
       profile = MOMENTUM
       strength = max(0.75, (momentum_strength + orb_strength) / 2)

3. ELIF atr_ratio <= 0.0035 (sideways) AND vwap_signal != HOLD:
       signal = vwap_signal
       profile = MEAN_REVERSION
       strength = max(0.6, vwap_strength)

4. ELSE:
       signal = HOLD
```

**Where:**
```
atr_ratio = ATR(14) / close
sideways threshold = 0.0035
```

**Example (trending):** Momentum says BUY_CE, ORB says BUY_CE → enter CE with high confidence.  
**Example (sideways):** ATR/close = 0.002 < 0.0035, VWAP reversion signals BUY_CE → enter CE as mean-reversion.

**Best regime:** All — the internal decision adapts automatically.

---

### ATM_BREAKOUT_EXPANSION

**What it does:** Catches large breakouts from a compressed consolidation zone, requiring simultaneous volume spike and ATR expansion.

**Entry requires ALL of:**
```
close > max(High, last 30 candles)    [or < min(Low) for PE]
AND compression_range_pct <= 0.45%   [price was compressed]
AND volume >= avg_volume(20) × 1.8   [volume spike confirms breakout]
AND ATR(14) >= avg_ATR(last 5) × 1.1 [volatility expanding]
```

**Where:**
```
compression_range_pct = (max(High,45) - min(Low,45)) / close
```

**Strength score:**
```
breakout_component = |close - level| / close / 0.01
volume_component   = (volume / avg_volume - 1.0) / 0.8
atr_component      = (ATR / avg_ATR - 1.0) / 0.1
strength = clip(sum, 0, 1)
```

**Example:** SENSEX 10:05 | 30-period high=73,950 | Close=74,010
- Volume=85,000 vs avg=42,000 → ratio 2.02 ≥ 1.8 ✓
- 45-period range = 0.38% ≤ 0.45% ✓
- ATR=48 vs avg_ATR=43 → ratio 1.12 ≥ 1.1 ✓
- close > 30p high ✓
→ BUY_CE signal

**Requires 45 candles minimum** → earliest entry ~10:00. Frequent false signals in choppy days.

**Best regime:** EXPANSION only. Underperforms heavily in SIDEWAYS and NORMAL.

---

### ATM_IV_EXPANSION

**What it does:** Enters on strong directional candles breaking recent highs/lows, with RSI confirming momentum. Designed for IV expansion events (news, data releases).

**Signal (BUY_CE):**
```
close > max(High, last 20 candles)           ← new 20-period high
AND |close - open| >= avg(|close-open|, 10 candles) × 1.8  ← big body candle
AND RSI(14) >= 55
```

**Signal (BUY_PE):**
```
close < min(Low, last 20 candles)
AND |close - open| >= avg_body × 1.8
AND RSI(14) <= 45
```

**Blocked by `iv_expansion_max_iv_percentile = 20%`:** Entry blocked if `iv_percentile > 20`. This prevents buying options when IV is already elevated (expensive).

**Strength score:**
```
breakout_component = |close - level| / close / 0.01
body_component     = (candle_body / avg_body - 1.0) / 0.8
rsi_component      = |RSI - threshold| / 45.0
strength = clip(sum, 0, 1)
```

**Example:** SENSEX 10:30 | 20p high=73,900 | Close=74,050
- Candle body = |74,050 − 73,920| = 130 vs avg_body=60 → 130/60=2.17 ≥ 1.8 ✓
- RSI=62 ≥ 55 ✓
- iv_percentile=12% ≤ 20% ✓
→ BUY_CE signal

**Best use:** Days with clear directional momentum (budget day, RBI policy, global cues). Worst use: choppy days with no catalyst.

---

### ATM_TRAP_REVERSAL

**What it does:** Catches fake breakouts — price breaks a key level (support/resistance) but immediately reverses back. Known as a "bull trap" or "bear trap".

**BUY_CE (Seller trap — support break failure):**
```
Prior 3 candles: min(Low) < support_level    ← fake breakdown below support
AND latest_close > support_level              ← closed back above
AND latest_close > latest_open                ← bullish candle
AND |close - open| >= avg_body(10) × 1.5     ← strong reversal candle
```

**BUY_PE (Buyer trap — resistance break failure):**
```
Prior 3 candles: max(High) > resistance_level ← fake breakout above resistance
AND latest_close < resistance_level            ← closed back below
AND latest_close < latest_open                 ← bearish candle
AND |close - open| >= avg_body(10) × 1.5
```

**Where:**
```
support_level    = min(Low, last 20 candles)   [prior to trap window]
resistance_level = max(High, last 20 candles)
trap_confirmation_candles = 3
```

**Strength score:**
```
recovery_component = |close - level| / close / 0.01
body_component     = (latest_body / avg_body - 1.0) / 0.5
strength = clip(sum, 0, 1)
```

**Example:** NIFTY 11:20 | 20p low=22,250 | Recent candles touched 22,230 (broke support) but close=22,290 > support, bullish candle with big body → BUY_CE signal.

**Best use:** When a key level is clearly defined and the first break fails with a strong reversal candle.

---

## 8. Entry Modes

### LIVE_TICK_CONFIRM (recommended)

Sub-1-minute entry using live LTP ticks. The engine watches the forming candle and validates entry using a state machine:

**State machine:**

```
INITIAL
  → Conditions: strong candle, volume spike, trend aligned, breakout detected
  → On trigger: store breakout_level, → AWAITING_CONFIRMATION

AWAITING_CONFIRMATION (timeout: 3 candles)
  → Condition: close > breakout_level (BUY) or < breakout_level (SELL)
  → On confirmed: → AWAITING_PULLBACK
  → On timeout: clear setup, block entry

AWAITING_PULLBACK (timeout: 5 candles)
  → Pullback band = max(VWAP, EMA9) × 0.35%
  → BUY: low touches VWAP/EMA band AND close > EMA9 AND close > VWAP
  → On pullback: ENTRY ACCEPTED
  → On timeout: clear setup, block entry
```

Entry validation thresholds:
```
volume_spike      = volume >= avg_volume × 1.5
no_candle_spike   = range <= avg_range × 1.6   (reject spike-then-crash candles)
strong_body       = body_ratio >= 0.6
pullback_band     = 0.35% of EMA/VWAP
```

### LIVE_STAGED

Waits for a closed candle signal then uses staged validation (same INITIAL/CONFIRM flow but on closed candles, no sub-1m ticks).

### LEGACY_IMMEDIATE

Enters directly on the closed candle signal without any staged validation. Fastest but least filtered.

---

## 9. Exit System — Stop Loss, Target, Trailing

### Adaptive level calculation

All exit levels are computed from ATR at entry time, adjusted by regime and risk style.

```
base_atr = max(ATR(14), entry_price × 0.015)
conviction = 1.0 + signal_score × 0.5

stop_distance     = max(entry_price × 7%, base_atr × stop_mult)
target_distance   = max(entry_price × 8%, base_atr × target_mult × conviction)
trailing_distance = max(entry_price × 3.5%, base_atr × trailing_mult)
```

**Risk style scaling (applied on top of regime multipliers):**

| Risk Style | SL scale | Target scale | Trail scale |
|------------|----------|-------------|-------------|
| CONSERVATIVE | 0.8× | 0.875× | 0.8× |
| BALANCED | 1.0× | 1.0× | 1.0× |
| AGGRESSIVE | 1.2× | 1.125× | 1.2× |

**Numeric example:** Entry ₹200 | ATR=18 | NORMAL regime | AGGRESSIVE risk

```
base_atr = max(18, 200×0.015) = max(18, 3) = 18
conviction = 1 + 0.8×0.5 = 1.4   (score=0.8)
stop_distance  = max(200×0.07, 18×2.2×1.2) = max(14, 47.5) = 47.5
  stop_loss    = 200 - 47.5 = ₹152.50
target_distance = max(200×0.08, 18×2.5×1.125×1.4) = max(16, 70.9) = 70.9
  target       = 200 + 70.9 = ₹270.90
trailing_distance = max(200×0.035, 18×0.7×1.2) = max(7, 15.1) = 15.1
```

### Trailing activation gate

Trailing stop **does not move** until:
```
best_price - entry_price >= trailing_activation_distance
```

Where:
```
trailing_activation_distance = max(trailing_distance, level1_distance × 0.8)
level1_distance = target_distance × 0.5
```

In the example above:
```
level1_distance = 70.9 × 0.5 = 35.45
trailing_activation_distance = max(15.1, 35.45×0.8) = max(15.1, 28.4) = 28.4
```

The trailing stop will not ratchet until price is ≥ ₹228.40 (₹200 + ₹28.40).

### Exit modes

| Mode | Behaviour |
|------|-----------|
| `TRAIL_ONLY` | When price hits the hard target, `trailing_active=True` is set and the target sentinel is removed. Position runs indefinitely with a trailing stop. |
| `HARD_TARGET` | Position exits immediately when price hits the hard target. |

### Exit priority order

```
1. STOP_LOSS        if close <= stop_loss (BUY) or >= stop_loss (SELL)
2. TRAILING_STOP    if trailing_active AND close <= trailing_stop (BUY)
3. TARGET           if include_target AND close >= target
4. TIME_EXIT        if hold_time >= max_hold_minutes (30 min default)
5. TIME_CUTOFF      if now >= 15:10
6. SQUARE_OFF       if now >= 15:15
```

Note: `include_target = not runner_trailing_only`. For intraday_options, `runner_trailing_only = True`, so TARGET exit is suppressed — the position must exit via trailing stop or time exit once it passes the target level.

---

## 10. Partial Exits (Runner System)

When a position has ≥ 2 lots, the engine automatically scales out in stages, "running" the remainder.

### Lot plan calculation

**For 2 lots (exactly at threshold):**
```
fractions = [0.30, 0.40, 0.30]
level1_qty = max(1, round(2 × 0.30)) × lot_size = 1 lot
level2_qty = max(1, round(2 × 0.40)) × lot_size = 1 lot  (if remaining ≥ 2)
runner_qty = 2 - level1 - level2 lots remaining
```

**For > 2 lots (large position mode, uses fixed premium targets):**
```
level1_lots = max(1, int(total_lots × 0.2))
level2_lots = max(1, int(total_lots × 0.2))
runner_lots = total_lots - level1_lots - level2_lots
```

**Example: 7 lots (455 qty, lot_size=65)**
```
total_lots = 7
level1_lots = max(1, int(7 × 0.2)) = max(1, 1) = 1 lot = 65 qty
level2_lots = max(1, int(7 × 0.2)) = 1 lot = 65 qty
runner_lots = 7 - 1 - 1 = 5 lots = 325 qty
```

### Exit targets

**Large position (> 2 lots):** Fixed premium % gain targets
```
level1_target = entry_price × (1 + 8.0%)    [runner_level1_premium_target_pct]
level2_target = entry_price × (1 + 15.0%)   [runner_level2_premium_target_pct]
```

**Small position (= 2 lots):** ATR-based targets
```
level1_target = entry_price + target_distance × 0.5
level2_target = entry_price + target_distance × 1.0
```

**Example: Entry ₹200, large position:**
```
level1_target = ₹200 × 1.08 = ₹216  → exit 1 lot at ₹216
level2_target = ₹200 × 1.15 = ₹230  → exit 1 lot at ₹230
Remainder (5 lots) run with trailing stop
```

### Stop-loss ratchet after partial exits

**After Level1 fires:**
```
stop_loss    = max(current_stop_loss, entry_price)   ← moved to breakeven
trailing_stop = max(trailing_stop, entry_price)
trailing_active = unchanged (still False if not already activated)
```

**After Level2 fires:**
```
protected_level = max(level1_target, exit_price - trailing_distance)
stop_loss    = max(current_stop_loss, protected_level)
trailing_stop = max(trailing_stop, protected_level)
target       = 1e9   ← sentinel, hard target removed
trailing_active = True   ← trailing now fully active on remainder
```

---

## 11. Partial Exits ON vs OFF — Trailing Interaction

This is the most important behavioural difference between the two modes.

### The trailing activation gate (key mechanism)

Trailing stop only ratchets when `(best_price - entry_price) >= trailing_activation_distance`. Until this threshold is crossed, the trailing stop is frozen at its initial value.

Additionally, for the trailing exit to actually **fire**, `trailing_active` must be `True` OR `trailing_activation_distance` must be `None`.

```python
# models/position.py
if (trailing_active or trailing_activation_distance is None) and close <= trailing_stop:
    return TRAILING_STOP exit
```

### Partial Exits OFF — how trailing works

```
Entry ₹200 | trailing_activation_distance ₹28

Candle +1: price ₹215 → profit ₹15 < ₹28 → trailing still frozen
Candle +2: price ₹229 → profit ₹29 ≥ ₹28 → trailing_active = True (auto-set)
Candle +3: price ₹235 → trailing ratchets to ₹235 - ₹15 = ₹220
Candle +4: price crashes to ₹218 ≤ ₹220 → TRAILING_STOP exit at ₹218
                                              Profit = ₹18/unit ✓
```

### Partial Exits ON — how trailing is gated

```
Entry ₹200 | level1_target ₹216 | level2_target ₹230

Candle +1: price ₹217 → HIGH ≥ ₹216 → LEVEL1 fires
           → 1 lot exits at ₹217
           → SL and trailing_stop ratcheted to ₹200 (breakeven)
           → trailing_active = False  ← still NOT set

Candle +2: price ₹225 → trailing is updated (profit > activation threshold)
           BUT trailing_active is still False
           → trailing_stop is ratcheted to ₹210 in memory
           BUT the exit check: trailing_active=False → trailing exit SKIPPED

Candle +3: price crashes to ₹205 → stop_loss check: 205 > 200 → no SL
           trailing exit check: trailing_active=False → skipped
           → position continues

Candle +4: price crashes to ₹198 ≤ ₹200 (stop_loss) → STOP_LOSS exit
           Profit on runner = ₹0 (breakeven, minus fees)
```

**If Level2 had also fired at ₹230, `trailing_active=True` would have been set and the trailing would have protected gains from that point.**

### Side-by-side comparison

| Scenario | Partial Exits OFF | Partial Exits ON |
|----------|------------------|-----------------|
| Trade reaches +8% then reverses | Trailing exit at ~+5–6% | Level1 exits 1 lot at +8%, runner exits at breakeven SL |
| Trade reaches +15% then reverses | Trailing exit at ~+10–12% | Level1+2 both exit, runner exits with trailing from +15% |
| Trade reaches +25% | Trailing follows all the way up | Level1+2 exits; runner runs with trailing from +15% |
| Trade stops out at −20% | Full loss on all lots | Full loss on all lots (same) |
| Trade reverses after +6% (before activation) | No trailing — SL at entry−28% | No trailing — SL at original stop |

### When to use each

**Use Partial Exits OFF when:**
- Capital is small (1–2 lots anyway)
- You prefer clean exits with trailing stop capturing 70–80% of the move
- Strategy has high win rate but small per-trade gains
- Market is fast-moving (trailing catches more)

**Use Partial Exits ON when:**
- Capital allows 3+ lots
- You want guaranteed lock-in of partial gains at +8% and +15%
- You expect strong trend days where the runner can go much further
- You accept more breakeven exits in exchange for occasional large runners

---

## 12. Risk Controls

### Daily loss cap

```
daily_max_loss_pct = 15%   (config: risk_controls.daily_max_loss_pct)
```

If realized losses exceed 15% of starting capital (₹15,000 on ₹1,00,000), **all new entries are blocked** for the rest of the day. Existing positions continue to be managed.

### Consecutive loss limit

```
consecutive_loss_limit = 3
```

After 3 consecutive losing trades, new entries are blocked until the next session.

### Max trades per underlying per day

```
max_trades_per_underlying: 5   (per underlying — NIFTY and SENSEX tracked separately)
```

### Theta decay exit

If option's theta causes daily time-decay loss to exceed 8% of current premium (`intraday_options_theta_exit_ratio`) and position has been held at least 10 minutes, exit is triggered.

### Max hold time

```
max_hold_minutes: 30
```

Position is exited after 30 minutes regardless of P&L (configurable per session).

### Hard time cutoff

All positions are exited if open at 15:10 (`intraday_options_time_exit_cutoff`). Square-off forced at 15:15.

---

## 13. Multi-Strategy Mode

When multiple strategies are selected, signals are aggregated using a voting system.

### Voting logic

For intraday_options strategies, votes are `BUY_CE` or `BUY_PE` (not generic BUY/SELL):

```
ce_count = count of BUY_CE votes
pe_count = count of BUY_PE votes

if ce_count >= min_confirmations AND ce_count > pe_count:
    option_signal = BUY_CE
elif pe_count >= min_confirmations AND pe_count > ce_count:
    option_signal = BUY_PE
elif ce_count == pe_count (tie):
    option_signal = HOLD   ← entry blocked
else:
    option_signal = HOLD
```

### Min confirmations

`min_confirmations = 1`: Any single strategy fires → enter  
`min_confirmations = 2`: At least 2 strategies must agree → more filtered  
`min_confirmations = 3`: High conviction only → fewer but cleaner entries

**Example (3 strategies selected: ATM_MULTI, ATM_IV_EXPANSION, ATM_ORB):**
- ATM_MULTI → BUY_CE
- ATM_IV_EXPANSION → BUY_CE  
- ATM_ORB → HOLD

With `min_confirmations=2`: ce_count=2 ≥ 2 → **BUY_CE confirmed**  
With `min_confirmations=3`: ce_count=2 < 3 → **HOLD**

---

## 14. All Configurable Parameters

All parameters are in `config/config.runtime.yaml`.

### FNO section

| Parameter | Default | Description |
|-----------|---------|-------------|
| `intraday_options_max_trades_per_underlying` | 5 | Max entries per underlying per day |
| `intraday_options_max_hold_minutes` | 30 | Time exit window in minutes |
| `intraday_options_time_exit_cutoff` | "15:10" | Hard time exit — all positions exit at this time |
| `intraday_options_min_signal_score` | 0.03 | Minimum signal quality score |
| `intraday_options_min_range_pct` | 0.35 | Min underlying range % to allow entries |
| `intraday_options_vega_crush_block_percent` | 20 | Block entry if IV drops >20% in 15m |
| `intraday_options_iv_expansion_max_iv_percentile` | 20 | Max IV%ile for ATM_IV_EXPANSION entries |
| `intraday_options_sideways_vwap_band_pct` | 0.0015 | VWAP deviation for sideways detection |
| `intraday_options_sideways_lookback_candles` | 8 | Lookback for sideways regime check |
| `intraday_options_regime_expansion_range_pct` | 1.4 | Range% threshold for EXPANSION regime |
| `intraday_options_regime_sideways_range_pct` | 0.55 | Range% threshold for SIDEWAYS regime |
| `intraday_options_regime_sideways_vwap_dev_pct` | 0.0025 | VWAP dev threshold for SIDEWAYS regime |
| `intraday_options_regime_expansion_iv_change_pct` | 2.0 | IV change % for EXPANSION regime |
| `intraday_options_lot_mode` | "CAPITAL_BASED" | CAPITAL_BASED or ONE_LOT |
| `intraday_options_entry_mode` | "LIVE_TICK_CONFIRM" | LIVE_TICK_CONFIRM / LIVE_STAGED / LEGACY_IMMEDIATE |
| `intraday_options_max_entry_cost_ratio` | 0.20 | Block if costs > 20% of expected profit |
| `intraday_options_max_spread_pct` | 0.015 | Block if bid-ask spread > 1.5% |
| `intraday_options_min_open_interest` | 10000 | Min OI for entry |
| `intraday_options_roll_trigger_pct` | 2.0 | Underlying move % to trigger ATM roll |
| `intraday_options_theta_exit_ratio` | 0.08 | Theta decay exit threshold |
| `intraday_options_theta_exit_min_minutes` | 10 | Min hold before theta exit can trigger |

### Engine defaults section (intraday_options)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_contract_price` | 8.0 | Block entry if premium < ₹8 |
| `min_abs_delta` | 0.2 | Block entry if abs(delta) < 0.2 |
| `max_buy_iv_percentile` | 75.0 | Block buy if IV%ile > 75 |
| `entry_cutoff` | "15:05" | No new entries after this time |
| `square_off_time` | "15:15" | Force exit all positions |
| `sleep_seconds` | 5 | Scan cycle interval |
| `cooldown_seconds` | 180 | Post-entry cooldown |
| `adaptive_stop_multiplier_sideways/normal/expansion` | 2.5/2.2/2.0 | ATR multiplier for stop |
| `adaptive_target_multiplier_sideways/normal/expansion` | 1.5/2.5/3.0 | ATR multiplier for target |
| `adaptive_trailing_multiplier_sideways/normal/expansion` | 0.6/0.7/0.8 | ATR multiplier for trailing |
| `adaptive_min_stop_pct` | 0.07 | Minimum stop = 7% of entry price |
| `adaptive_min_target_pct` | 0.08 | Minimum target = 8% of entry price |
| `adaptive_min_trailing_pct` | 0.035 | Minimum trail = 3.5% of entry price |
| `momentum_volume_multiplier` | 1.5 | Volume spike threshold for staged entry |
| `momentum_confirmation_timeout_candles` | 3 | Candles to wait for breakout confirmation |
| `momentum_pullback_timeout_candles` | 5 | Candles to wait for pullback |
| `momentum_pullback_band_pct` | 0.0035 | Pullback proximity band (0.35% of EMA/VWAP) |
| `runner_level1_premium_target_pct` | 8.0 | Level1 partial exit at +8% |
| `runner_level2_premium_target_pct` | 15.0 | Level2 partial exit at +15% |
| `runner_partial_exit_fraction` | 0.2 | 20% of lots at each partial exit |
| `runner_partial_exit_lot_threshold` | 2 | Min lots for large-position runner mode |

---

## 15. Backtest Performance Summary

Based on 52 backtests (1-day SENSEX/NIFTY, 1m candles, ₹1,00,000 capital).

| Strategy | Underlying | Runs | Win Rate | Net P&L | Avg/Trade | Consistency |
|----------|-----------|------|----------|---------|-----------|-------------|
| **ATM_IV_EXPANSION** | SENSEX | 6 | **77%** | **+₹1,64,436** | **+₹4,216** | **6/6 profitable** |
| MULTI (various) | NIFTY | 4 | 86% | +₹83,016 | +₹2,965 | 4/4 profitable |
| MULTI (various) | SENSEX | 11 | 63% | +₹1,07,781 | +₹1,476 | 7/11 profitable |
| ATM_BREAKOUT_EXPANSION | NIFTY | 2 | 71% | −₹568 | −₹81 | 1/2 profitable |
| ATM_MULTI | SENSEX | 1 | 33% | −₹21,521 | −₹3,587 | 0/1 profitable |
| ATM_MOMENTUM | SENSEX | 3 | 18% | −₹74,817 | −₹6,802 | 0/3 profitable |
| ATM_BREAKOUT_EXPANSION | SENSEX | 21 | 50% | −₹96,851 | −₹2,201 | 4/21 profitable |

**Key finding:** `ATM_IV_EXPANSION` on SENSEX is the only strategy with 100% run consistency across all tested sessions. `ATM_BREAKOUT_EXPANSION` on SENSEX has the worst real-world performance despite an 80%+ win rate in the 3–4 best runs — the 17 losing runs overwhelm the winners.

---

## 16. Web UI — Live Session Features

### 16.1 Trade Book (Today)

The Dashboard tab shows a **Trade Book** table that updates in real-time as trades close. It matches the Backtest Results trade table in detail.

**Columns:**

| Column | Description |
|--------|-------------|
| Symbol | Short symbol name (hover for full) |
| Side | BUY (green badge) or SELL (red badge) |
| Qty | Filled quantity (lots × lot size) |
| Entry Time | Time the entry fill was confirmed (HH:MM:SS) |
| Exit Time | Time the exit fill was confirmed (HH:MM:SS) |
| Entry ₹ | Entry fill price |
| Exit ₹ | Exit fill price |
| Gross P&L | Raw P&L before charges |
| Charges | Estimated brokerage + STT (paper = ₹0) |
| Net P&L | Gross minus charges (green = profit, red = loss) |
| Exit Reason | STOP_LOSS / TARGET / TRAIL / TIME_EXIT / FORCE_SQUAREOFF |
| Entry Reason | Strategy name · signal score · IV · Delta (collapsed inline) |

**Click any row** to expand an inline detail panel showing:
- Full strategy name and signal score
- Underlying name and spot price at entry
- Option type (CE/PE), IV%, Delta, DTE
- Full entry reason text (e.g. "Breakout expansion long: close 23190 above 23184, compression 0.0035...")

**Persist after session stops:** When a session stops, the table automatically reloads from the JSONL trade store via `/api/session-trades`. A manual **↻ reload** button is also available.

### 16.2 Why These Trades? Accordion

For each new entry, a collapsible card appears showing:
- Symbol, Side, Entry Price, Qty
- Strategy name and Signal Quality score
- Conditions Met (green ✓): VWAP, agreement count, momentum, etc.
- Cautions / Risks (amber ⚠): expiry warning, IV regime, VWAP deviation
- Analytics: Underlying, Spot, Option type, IV%, Delta, DTE

Cards are stacked newest-first. Clicking any card header expands/collapses it. The panel can be dismissed with the × button and reappears on the next trade.

### 16.3 Warning Center — Blocked Signal Warnings

When the options filter blocks a potential entry, a **[OPTIONS]** warning card appears in the Warning Center (orange severity). This makes it visible without reading logs.

| Warning field | Content |
|--------------|---------|
| Title | `Entry blocked: <short symbol>` |
| Detail | Filter reason · Underlying · Spot ₹ · Option type · Bias |
| ID | `options_blocked:<symbol>` (deduplicates each cycle) |

The warning clears automatically the next cycle if the signal is no longer blocked or if a live signal fires.

**Example detail (pipe-separated, rendered as bullets in UI):**
```
Underlying bias filter blocked CE: BEARISH | Underlying: NIFTY | Spot: ₹23,190.15 | Option type: CE | Underlying bias: BEARISH
```

Other filters that produce this warning:
- `VWAP band filter blocked BUY/SELL` — premium not above/below VWAP
- `Volatility proxy blocked trade`
- `Sideways market detected`
- `Premium below minimum`
- `Vega crush alert`
- `Delta below minimum`

### 16.4 Session Excel Export

When a session stops (normally or due to error), the system automatically exports an Excel workbook to:
```
runners/06_intraday_options/Results/SessionReports/
    intraday_options_YYYY-MM-DD_session_report_HHMMSS.xlsx
```

**Sheets:**

| Sheet | Contents |
|-------|---------|
| Trades | One row per closed trade: symbol, side, qty, entry/exit time & price, gross P&L, charges, net P&L, exit reason, execution mode |
| Orders | Full order audit trail: stage (pre_flight / spread_check / margin_check / submitted / reconciled / slippage), status, price, timestamp, note |
| OrderStagesSummary | Count and pass/fail per order stage — useful for diagnosing repeated slippage or margin failures |
| ExitReasonSummary | Count, wins, win rate, total/avg net P&L grouped by exit reason |

**Requirements:** `openpyxl` must be installed (`pip install openpyxl`). If missing, the export is skipped non-fatally.

### 16.5 Execution Mode

| Mode | Description |
|------|-------------|
| **PAPER** | Simulates fills locally — no Kite API calls. Trade book shows fake order IDs (`PAPER-xxxxx`). Charges = ₹0. |
| **LIVE** | Sends real orders to Kite. Real order IDs returned. Exchange charges apply. |

**To trade live:** Set Execution Mode = **LIVE** in the Configure tab before clicking Start. Verify in the session log: `Execution mode: LIVE` and `[EXECUTION] Provider: KITE`.
