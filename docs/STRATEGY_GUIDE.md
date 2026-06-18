# Strategy Guide

A practical decision framework for selecting engines, strategies, and risk parameters based on market conditions, capital, and risk tolerance.

---

## Table of Contents

1. [Engine Overview](#engine-overview)
2. [Strategy Reference](#strategy-reference)
3. [Engine Selection Framework](#engine-selection-framework)
4. [Per-Engine Deep Dive](#per-engine-deep-dive)
5. [Strategy Selection by Market Condition](#strategy-selection-by-market-condition)
6. [Risk Style Selection](#risk-style-selection)
7. [Adaptive Regime Behaviour](#adaptive-regime-behaviour)
8. [Key Config Knobs by Scenario](#key-config-knobs-by-scenario)
9. [Decision Workflow](#decision-workflow)
10. [Common Mistakes](#common-mistakes)
11. [SL / Target / Trailing Parameters — Formulas and Examples](#sl--target--trailing-parameters--formulas-and-examples)

---

## Engine Overview

| # | Engine | Instrument | Time Horizon | Product | Suitable For |
|---|---|---|---|---|---|
| 1 | `intraday_equity` | NSE equity stocks | Same-day (squareoff 15:15) | MIS | Active intraday traders |
| 2 | `delivery_equity` | NSE equity stocks | Multi-day (up to 5 days) | CNC | Swing / positional traders |
| 3 | `futures_equity` | NSE/NFO futures | Multi-day positional | NRML | Positional futures traders |
| 4 | `options_equity` | NSE/NFO options | Multi-day positional | NRML | Positional options traders |
| 5 | `intraday_futures` | NSE/NFO index/stock futures | Same-day (squareoff 15:15) | MIS | Leverage intraday traders |
| 6 | `intraday_options` | NSE/NFO ATM options | Same-day (squareoff 15:15) | MIS | Options intraday traders — both directions |
| 7 | `intraday_options_buyer` | NSE/NFO ATM options | Same-day (squareoff 15:15) | MIS | Options intraday buyers (long only) |
| 8 | `intraday_options_seller` | NSE/NFO ATM options | Same-day (squareoff 15:15) | MIS | Premium writers — 5 seller strategies |

---

## Strategy Reference

### Available strategies and when they work

| Strategy | Signal Logic | Works Best When | Fails When |
|---|---|---|---|
| **MA** | MA20 > MA50 → BUY; MA20 < MA50 → SELL | Strong directional trend | Sideways / choppy markets — frequent false crossovers |
| **RSI** | RSI < 30 → oversold BUY; RSI > 70 → overbought SELL | Sideways / range-bound markets | Strong trends — RSI stays extended for extended periods |
| **VWAP** | Price > VWAP → bullish; Price < VWAP → bearish | Active-volume intraday sessions with institutional flow | Choppy price oscillating around VWAP repeatedly |
| **BREAKOUT** | Close > prior 20-candle high → BUY; < low → SELL | Post-consolidation momentum expansion with volume confirmation | Fake breakouts — price breaks level and immediately reverses |
| **ORB** | Opening range (first 15 candles) high/low break | Strong gap-and-go opens; clear directional session bias | Flat opens, fake early moves that fill back into range |
| **ATM_MOMENTUM** | Strong directional signal + staged pullback entry | Trending intraday with clear VWAP alignment | Whipsaw sessions where price oscillates around VWAP |
| **ATM_ORB** | Opening range break in options | Gap-and-go days with early directional conviction | Fake ORB breaks that fill back into range |
| **ATM_BREAKOUT_EXPANSION** | 45-candle compression + breakout + volume + ATR expansion | Sustained directional moves after tight consolidation | Thin-volume breakouts, sudden reversals after entry |
| **ATM_TRAP_REVERSAL** | Failed breakout + strong reversal candle sequence | Markets that test key levels and sharply reverse | Trending markets — counter-trend setups get stopped out |
| **AUTO_ADAPTIVE** | Engine selects regime (SIDEWAYS/NORMAL/EXPANSION) and adapts levels | When you are unsure which strategy suits the day | No failure mode — worst case is SIDEWAYS conservative sizing |
| **ATM_ORB_FAILURE_SELL** | Price breaks ORB level then re-enters range; SELL premium | Failed breakout days — price probes a level and reverses | Trending days — real ORB breaks will run against the sold option |
| **ATM_VWAP_FADE_SELL** | Price stretched from VWAP in low-ADX session; SELL premium | Choppy, range-bound sessions with low ADX (< 18) | Strong directional sessions — price stays away from VWAP |
| **SHORT_THETA_AFTER_11AM** | Post-11 AM pure theta sell; low ADX + contained range | Afternoon range-bound days after event-driven morning volatility settles | Morning sessions; high-ADX trending days |
| **LOW_VOLATILITY_RANGE_SELL** | Contracting ATR below its own MA + low ADX; SELL slightly OTM | Volatility compression days before a squeeze resolves | Days with imminent catalysts — ATR expansion would blow stops |
| **EXHAUSTION_SELL** | RSI > 75 + price > 1.5 ATR above VWAP + RSI declining → SELL_CE | Overbought exhaustion with momentum stalling; expiry-day pinning | Early-session moves where momentum is still accelerating |

### Multi-Strategy Confirmation (recommended combinations)

| Combination | Use When |
|---|---|
| MA + RSI | Trend confirmation + momentum filter — reduces false crossovers |
| BREAKOUT + VWAP | Breakout only in the direction institutions favor — reduces fake breaks |
| VWAP + ORB | Both directional inputs agree — high-conviction intraday |
| MA + BREAKOUT | Strong trend continuation setups |

Set `strategy_mode = MULTI` and `min_confirmations >= 2` for any multi-strategy combination.

---

## Engine Selection Framework

### Step 1 — Choose by time horizon

- **Same-day only, no overnight risk** → `intraday_equity`, `intraday_futures`, or `intraday_options`
- **Comfortable holding 1–5 days** → `delivery_equity`, `futures_equity`, or `options_equity`

### Step 2 — Choose by instrument preference

```
Equities:       intraday_equity  (intraday)   |  delivery_equity  (swing)
Futures:        intraday_futures (intraday)   |  futures_equity   (positional)
Options:        intraday_options (intraday)   |  options_equity   (positional)
```

### Step 3 — Choose by capital and risk tolerance

| Capital Range | Risk Appetite | Recommended Engine | Rationale |
|---|---|---|---|
| < ₹50,000 | Low | `intraday_equity` (CONSERVATIVE) | No leverage, liquid stocks, capped loss |
| ₹50K–₹2L | Moderate | `intraday_equity` (BALANCED) or `delivery_equity` | Scalable without margin risk |
| ₹2L–₹5L | Moderate–High | `intraday_futures` or `delivery_equity` | Futures leverage provides capital efficiency |
| ₹5L+ | High | `intraday_options` or multi-engine | High reward potential with defined max loss (options) |
| Any | Learning/Paper | Any engine in paper mode | Zero real risk while building confidence |

### Step 4 — Check market volatility context

| Market Context | Preferred Engine | Avoid |
|---|---|---|
| High VIX (>18), strong trending day | `intraday_futures`, `intraday_options` (ATM_MOMENTUM) | `delivery_equity` (overnight gap risk increases) |
| Low VIX (<14), sideways/range | `intraday_equity` (RSI), `delivery_equity` | `intraday_options` (theta kills premium buyers) |
| Gap-and-go open (>0.5% gap) | `intraday_options` (ATM_ORB), `intraday_futures` | Delivery entry on gap day (chasing entry) |
| Post-consolidation breakout | `intraday_equity` (BREAKOUT), `intraday_futures` | RSI (fights the move) |
| Earnings / Event day | `intraday_options` (defined loss), reduce all other sizes | `intraday_futures` (gap risk with leverage) |
| Pre-expiry (Thursday) | `intraday_options` expiry caution, watch IV crush | `options_equity` positional (expiry P&L distortion) |

---

## Per-Engine Deep Dive

### intraday_equity

**What it does:** Scans NSE equity stocks each minute. Supports MA, RSI, VWAP, BREAKOUT, and ORB with volume confirmation. Auto-adaptive mode detects regime (SIDEWAYS/NORMAL/EXPANSION) and adjusts stop/target/trailing multipliers automatically.

**Session timing:**
- Market open: 09:15
- Entry cutoff: configurable (default: 30 minutes before square-off)
- Square-off: 15:15
- Sleep: 60 seconds between cycles

**Best used when:**
- You want diversified intraday equity exposure across multiple stocks
- Market shows a clear directional trend (use MA or BREAKOUT)
- You prefer equity over derivatives (no expiry pressure, no theta decay)
- Capital is below ₹2L and leverage is not needed

**Strategy selection:**
| Day Type | Recommended Strategy |
|---|---|
| Strong trending (Nifty up/down >0.5%) | MA, BREAKOUT, or AUTO_ADAPTIVE |
| Sideways, range-bound | RSI or AUTO_ADAPTIVE |
| Active volume day | VWAP |
| Strong opening momentum | ORB |
| Uncertain conditions | AUTO_ADAPTIVE (let the engine decide) |

**Risk style guidance:**
| Risk Style | risk_percent | Use When |
|---|---|---|
| CONSERVATIVE | 0.5% of capital | New to intraday, volatile market, earnings week |
| BALANCED | 1.0% of capital | Normal market conditions, tested strategy |
| AGGRESSIVE | 1.5% of capital | Strong trend day, high-conviction setup, experienced |

**Adaptive ATR levels (AUTO_ADAPTIVE):**
- SIDEWAYS: tight stop (×1.6), small target (×1.1), conservative trailing (×0.8)
- NORMAL: moderate stop (×1.4), decent target (×1.6), standard trailing (×1.0)
- EXPANSION: moderate stop (×1.4), wide target (×2.2), looser trailing (×1.15)

**Key knobs to tune:**
- `square_off_time` — push to `"15:00"` on volatile days to exit earlier
- `adaptive_target_multiplier_expansion` — raise to `2.5` on strong breakout days
- `cooldown_seconds` — lower to `120` on high-momentum days; raise to `600` in choppy sessions

---

### delivery_equity

**What it does:** Positions are held CNC (delivery) for up to `max_hold_days` (default 5 days). Uses Nifty50 trend guard to block entries against the broader market trend. Adaptive ATR levels have wider stops and targets appropriate for multi-day holds.

**Session timing:**
- No intraday square-off — positions held overnight
- Sleep: 300 seconds between cycles (slower scan is fine for positional)
- Cooldown: 0 seconds (one position per symbol, no repeat entries)

**Best used when:**
- You want to capture multi-day swing moves without leverage
- Broader market trend is clear (MA50 on Nifty is the gate)
- You have capacity to monitor positions once daily
- Risk appetite is moderate — no leverage, defined stop losses

**Strategy selection:**
| Swing Setup | Recommended Strategy |
|---|---|
| Clear multi-day trend | MA + BREAKOUT (multi-confirmation) |
| Oversold / overbought levels | RSI |
| Volume-driven momentum | BREAKOUT with volume confirmation |
| Uncertain | AUTO_ADAPTIVE (adapts to daily regime) |

**Risk style guidance:**
| Risk Style | risk_percent | Use When |
|---|---|---|
| CONSERVATIVE | 0.5% | Bear market, uncertain macro, first swing trades |
| BALANCED | 1.0% | Normal trending environment |
| AGGRESSIVE | 1.5% | Strong bull run, high-conviction swing with clear Nifty trend |

**Adaptive ATR levels:**
- Wider than intraday to accommodate multi-day moves and overnight noise
- SIDEWAYS: stop ×2.4, target ×1.6 — tight stop, modest target
- NORMAL: stop ×2.0, target ×2.6 — good risk-reward for swing
- EXPANSION: stop ×1.8, target ×3.4 — let winners run in strong trends

**Key knobs to tune:**
- `max_hold_days` — reduce to `3` if you prefer quick exits; raise to `7` for slower swing setups
- `nifty_trend_ma_window` — raise to `100` for stronger trend filter; lower to `20` for faster filter
- `adaptive_target_multiplier_expansion` — raise to `4.0` on strong bull market days

---

### intraday_futures

**What it does:** Trades NSE/NFO index futures and stock futures intraday with MIS product. Lot size is automatically aligned — position sizes are rounded to whole lots. Inherits the FuturesEquityEngine risk and signal framework.

**Session timing:**
- Market open: 09:15
- Entry cutoff: 15:05 (configurable)
- Square-off: 15:15
- Sleep: 60 seconds; Cooldown: 180 seconds
- Data: 15-day history, 3-minute candles

**Best used when:**
- You want leveraged intraday exposure to index or stock futures
- Capital is above ₹2L (lot sizes require meaningful margin)
- Market is trending strongly (leverage amplifies both gains and losses)
- VIX is elevated — futures benefit from momentum; theta is not a factor

**Strategies:** Inherits futures-equity strategy set (MA, RSI, VWAP, BREAKOUT, ORB).

**Risk style guidance:**
| Risk Style | risk_percent | Use When |
|---|---|---|
| CONSERVATIVE | 0.5% | First futures trades, uncertain day |
| BALANCED | 1.0% | Normal trending session |
| AGGRESSIVE | 1.5% | Strong trend, high VIX, conviction setup |

**Key knobs to tune:**
- `entry_cutoff` — move to `"14:45"` to stop new entries earlier on risk days
- `max_symbol_allocation` — default 35% per symbol; reduce to `0.20` to avoid over-concentration on a single future
- `cooldown_seconds` — keep at 180 minimum; futures leverage makes rapid re-entries costly

**Caution:** Futures leverage means a 1.5% risk style with a 35% allocation can result in significant notional exposure. Always verify margin requirements against your capital before live sessions.

---

### intraday_options (Engine 6 — Both Directions)

**What it does:** The most feature-rich intraday engine. Trades ATM or near-ATM options in a defined-loss / defined-reward structure. Supports seven specialized option strategies. Has regime detection (SIDEWAYS/NORMAL/EXPANSION), IV percentile filter, open interest guard, theta-decay exit, partial/runner exit, and tick-entry simulation in backtest. Fires BUY CE on bullish signals and BUY PE on bearish signals.

**Session timing:**
- Market open: 09:15
- Entry cutoff: 15:05 (configurable)
- Hard time exit: 15:10 (`intraday_options_time_exit_cutoff`)
- Square-off: 15:15
- Sleep: 10 seconds (fastest cycle of all engines — options move quickly)
- Cooldown: 180 seconds

**Best used when:**
- You want defined maximum loss (premium paid) regardless of market gaps
- VIX is above 15 — higher IV means bigger premium moves for less underlying movement
- Clear directional bias exists early in the session (ORB or momentum setup)
- Market is likely to have a sustained directional move (budget days, event days)
- Capital is above ₹3L to comfortably handle lot sizes

**Strategy selection:**

| Day Type | Recommended Strategy | Reasoning |
|---|---|---|
| Gap-and-go open (>0.5% gap) | ATM_ORB | Opening range break — clean directional bias early |
| Trending session, VWAP aligned | ATM_MOMENTUM | Staged entry on pullback — better fill price |
| Post-consolidation breakout with volume | ATM_BREAKOUT_EXPANSION | High-quality signal, less frequent but reliable |
| Key level test + sharp reversal | ATM_TRAP_REVERSAL | Counter-trend — use conservative sizing |
| Uncertain / want all setups | AUTO_ADAPTIVE | Engine selects based on detected regime |

**Risk style guidance:**
| Risk Style | risk_percent | Use When |
|---|---|---|
| CONSERVATIVE | 0.5% | Learning options flow, volatile event days |
| BALANCED | 1.0% | Normal trend day with clear setup |
| AGGRESSIVE | 1.5% | Strong conviction, high VIX, confirmed ORB or momentum |

**IV and premium filters (config.runtime.yaml → fno section):**
- `max_buy_iv_percentile: 75.0` — avoids buying expensive premium (IV crush risk)
- `min_contract_price: 8.0` — avoids illiquid sub-8 premiums
- `intraday_options_max_hold_minutes: 15` — time stop; options bleed theta each minute (override per session via UI/CLI `time_exit_minutes`)
- `intraday_options_vega_crush_block_percent: 20` — exits/blocks when IV drops sharply

**Adaptive ATR levels for options (wider than equity):**
- SIDEWAYS: stop ×2.0, target ×1.1 — tight stop, small target
- NORMAL: stop ×1.7, target ×1.7 — symmetric R:R
- EXPANSION: stop ×1.7, target ×2.3 — let winners run on strong expansion

**Lot mode:** Default `CAPITAL_BASED` — lot size scales with your declared capital. Switch to `ONE_LOT` if you want a fixed single contract.

**Entry mode:** Default `LIVE_STAGED` — closed candle signal + staged breakout confirmation for a better fill price. Switch to `LIVE_TICK_CONFIRM` for sub-1m forming-candle entry, or `LEGACY_IMMEDIATE` to bypass staged filters (not recommended).

**Key knobs to tune:**
- `intraday_options_max_hold_minutes` — reduce to `20` on choppy days to cut time risk
- `max_buy_iv_percentile` — reduce to `60` on high-VIX days to avoid buying at the top of IV
- `runner_level1_premium_target_pct` / `runner_level2_premium_target_pct` — tune runner exit levels based on your observed premium ranges

---

### intraday_options_buyer (Engine 7 — Buyer Only)

Identical to Engine 6 but `trade_direction_mode = BUY_ONLY`. All signal filter checks run normally; any SELL signal is suppressed to HOLD. Use when you want directional long-premium trades only (no writing).

**Strategies:** Same 7 buyer strategies as Engine 6.

---

### intraday_options_seller (Engine 8 — Independent Seller)

**What it does:** Five independent premium-writing strategies that look for conditions favoring option decay, not directional momentum. Generates `SELL_CE` / `SELL_PE` signals directly — no buyer logic is inherited. Positions are sized for margin (SELL side: underlying × 12%), exits are fixed-percentage (target = 30% premium decay, stop = 60% premium rise, HARD_TARGET mode — no trailing).

**Seller filter chain:**
1. ADX gate: blocks if ADX > 22 (trending market — premium can expand against writer)
2. Regime gate: blocks EXPANSION regime except for `EXHAUSTION_SELL`
3. Time gate: `SHORT_THETA_AFTER_11AM` and `LOW_VOLATILITY_RANGE_SELL` need current time ≥ 11:00
4. Delta ceiling: `|delta| ≤ 0.55` (allows ATM writes; expiry-day ATM CE/PE delta drifts 0.48–0.53)
5. IV floor: blocks if IV percentile < 10 (no worthwhile premium)

**Strategy selection:**
| Condition | Strategy |
|---|---|
| Failed ORB breakout day | ATM_ORB_FAILURE_SELL |
| Choppy, low-ADX session, price straying from VWAP | ATM_VWAP_FADE_SELL |
| Quiet afternoon session post-11 AM, range-bound | SHORT_THETA_AFTER_11AM |
| Volatility compression, ATR contracting | LOW_VOLATILITY_RANGE_SELL |
| RSI overbought/oversold exhaustion + VWAP stretch | EXHAUSTION_SELL |

**Key config params (config.runtime.yaml → fno):**
- `intraday_options_seller_max_adx: 22.0` — raise to allow entries in moderate trends
- `intraday_options_seller_max_delta: 0.55` — ceiling for written option delta
- `intraday_options_seller_target_decay_pct: 30.0` — exit when premium decays by 30%
- `intraday_options_seller_stop_pct: 60.0` — exit if premium rises by 60%
- `intraday_options_seller_min_iv_percentile: 10.0` — minimum IV for worthwhile premium

---

### futures_equity

**What it does:** Positional multi-day futures engine. Holds NRML (no intraday square-off) for swing/positional trades on index or stock futures. Uses 5-minute candles over a 3-month data window.

**Best used when:**
- You want leveraged swing exposure without expiry-driven exits
- Capital is above ₹5L (NRML margins are higher)
- Market is in a multi-day trend (bull or bear)

**Strategies:** MA, RSI, VWAP, BREAKOUT, ORB — same set as equity engines.

**Key difference from intraday_futures:** No daily square-off. Positions roll overnight — gap risk is real. Use tight stop losses (`risk_percent` of 0.5–1.0%) to manage overnight exposure.

---

### options_equity

**What it does:** Positional multi-day options engine. Holds NRML options for swing/event plays. Uses 15-minute candles over a 2-month data window.

**Best used when:**
- You want multi-day leveraged exposure with defined maximum loss
- A clear multi-day event is anticipated (budget, Fed meeting, result season)
- Capital is above ₹3L and you can monitor positions daily

**Caution:** Theta decay works against buyers every day the position is held. Multi-day options requires either strong conviction, sufficient time-to-expiry, or an IV expansion thesis. Avoid buying near-expiry options for positional holds.

---

## Strategy Selection by Market Condition

### Pre-session checklist (before 09:15 each day)

1. **Check Nifty/SGX Nifty futures** — are they gap-up or gap-down?
2. **Check VIX** — below 14 (calm), 14–18 (normal), above 18 (elevated)
3. **Check global cues** — Dow, Nasdaq overnight close
4. **Check any scheduled events** — RBI policy, budget, expiry, major results

### Condition → Engine → Strategy mapping

| Market Condition | VIX | Engine | Strategy |
|---|---|---|---|
| Strong trend day, clear direction | Any | `intraday_equity` or `intraday_futures` | MA, BREAKOUT, or VWAP |
| Gap-and-go open (>0.5% gap) | Any | `intraday_options` | ATM_ORB |
| Sideways, range-bound day | <15 | `intraday_equity` | RSI |
| Moderate trend, active volume | 14–18 | `intraday_equity` | VWAP or AUTO_ADAPTIVE |
| High momentum, VIX elevated | >18 | `intraday_options` | ATM_MOMENTUM or ATM_ORB |
| Post-consolidation breakout | Any | `intraday_equity` or `intraday_futures` | BREAKOUT |
| Failed breakout / key level trap | Any | `intraday_options` | ATM_TRAP_REVERSAL (small size) |
| Event day (budget, policy, expiry) | >15 | `intraday_options` | ATM_MOMENTUM (defined loss) |
| Multi-day bull swing | <18 | `delivery_equity` | MA + BREAKOUT (multi-confirm) |
| Multi-day bear swing | Any | `delivery_equity` | MA or BREAKOUT (SHORT) |
| Uncertain, no clear bias | Any | `intraday_equity` (AUTO_ADAPTIVE) | AUTO_ADAPTIVE |
| Learning / testing | Any | Any engine (paper mode) | Any |

---

## Risk Style Selection

### When to use each style

| Risk Style | risk_percent | intraday | positional | Use When |
|---|---|---|---|---|
| CONSERVATIVE | 0.5% capital | ₹250 risk on ₹50K | ₹500 risk on ₹1L | Learning, volatile week, uncertain market |
| BALANCED | 1.0% capital | ₹500 risk on ₹50K | ₹1K risk on ₹1L | Normal conditions, tested setup |
| AGGRESSIVE | 1.5% capital | ₹750 risk on ₹50K | ₹1.5K risk on ₹1L | Strong conviction, confirmed trend, experienced |

### Risk controls that gate entries regardless of style

These are set in `config.runtime.yaml → risk_controls` and are independent of risk style:

| Control | Default | Purpose |
|---|---|---|
| `daily_max_loss_pct` | 3% | Blocks all new entries once daily loss exceeds 3% of capital |
| `consecutive_loss_limit` | 3 | Blocks entries after 3 consecutive losses |
| `api_failure_pause_minutes` | 5 | Pauses after scan/API failures |
| `abnormal_slippage_pause_pct` | 0.5% | Pauses if a live fill slips >0.5% from expected |

On a day with multiple losses, `consecutive_loss_limit` will naturally gate you out before `daily_max_loss_pct` kicks in. Do not override these limits on losing days.

---

## Adaptive Regime Behaviour

The `intraday_equity`, `delivery_equity`, and `intraday_options` engines detect the intraday regime automatically when `AUTO_ADAPTIVE` mode is used.

### Regimes

| Regime | Market Behaviour | Stop Multiplier | Target Multiplier | Trailing Multiplier |
|---|---|---|---|---|
| **SIDEWAYS** | Low range, price oscillates around VWAP | Widest (protective) | Smallest (take quick profits) | Tightest (lock in gains fast) |
| **NORMAL** | Moderate directional movement | Moderate | Moderate | Moderate |
| **EXPANSION** | Large directional range, ATR expanding | Moderate (don't stop out early) | Largest (let winners run) | Looser (allow trend to continue) |

### How to interpret adaptive behaviour

- In **SIDEWAYS**: the engine takes small profits and uses tight trailing. If you see frequent small profits being booked, the regime is sideways. This is correct — do not loosen targets.
- In **EXPANSION**: the engine holds positions longer and targets larger moves. If a position stays open past your usual expectation, the engine is correctly riding an expansion regime.
- If the regime classification seems wrong (e.g., trending day classified as SIDEWAYS): check `intraday_options_regime_expansion_range_pct` and `intraday_options_regime_sideways_range_pct` in the config. Lowering the expansion threshold will promote more sessions to EXPANSION.

### Regime thresholds (tunable in config.runtime.yaml → fno)

| Key | Default | Effect |
|---|---|---|
| `intraday_options_regime_expansion_range_pct` | 1.1% | Lower to classify more sessions as EXPANSION |
| `intraday_options_regime_sideways_range_pct` | 0.55% | Raise to classify more sessions as SIDEWAYS |
| `intraday_options_sideways_vwap_band_pct` | 0.15% | VWAP deviation band for sideways classification |

---

## Key Config Knobs by Scenario

### Scenario: Volatile week (budget / RBI / FII flows)

```yaml
# config.runtime.yaml
risk_controls:
  daily_max_loss_pct: 0.02       # tighten from 3% to 2%
  consecutive_loss_limit: 3      # tighten from 4 to 3

engine_defaults:
  intraday_options:
    entry_cutoff: "14:45"        # stop entries earlier
    max_buy_iv_percentile: 60.0  # avoid buying expensive IV
    intraday_options_max_hold_minutes: 20  # shorter time stop
```

### Scenario: Strong trending day (Nifty up >1%)

```yaml
engine_defaults:
  intraday_equity:
    adaptive_target_multiplier_expansion: 2.5   # let winners run further
    adaptive_trailing_multiplier_expansion: 1.2  # give trend more room
  intraday_options:
    adaptive_target_multiplier_expansion: 2.8   # premium can run on strong trend
```

### Scenario: Sideways / consolidation day

```yaml
engine_defaults:
  intraday_equity:
    square_off_time: "14:30"     # exit earlier — flat afternoon common
    cooldown_seconds: 600        # reduce churn in choppy conditions
```

### Scenario: Learning / first week live

```yaml
orders:
  max_live_order_notional: 30000  # hard cap to limit exposure per order

risk_controls:
  daily_max_loss_pct: 0.015       # 1.5% daily loss cap
  consecutive_loss_limit: 2       # stop after 2 losses
```

### Scenario: Options expiry day (Thursday)

```yaml
fno:
  intraday_options_expiry_warning_days: 3       # warn/block 3 days before expiry
  intraday_options_max_hold_minutes: 15         # shorter holds on expiry day
  intraday_options_time_exit_cutoff: "14:45"   # exit well before expiry chaos
  intraday_options_vega_crush_block_percent: 15 # tighter IV crush guard
```

---

## Decision Workflow

Use this step-by-step process each morning before starting a session.

```
STEP 1 — CHECK MARKET CONTEXT
│
├── VIX > 18?
│     ├── YES → prefer intraday_options (defined loss), avoid delivery entry
│     └── NO  → all engines viable; match to strategy
│
├── Gap > 0.5%?
│     ├── YES → intraday_options ATM_ORB, or intraday_futures
│     └── NO  → check trend and strategy
│
└── Scheduled event today?
      ├── YES → intraday_options only (defined loss), conservative sizing
      └── NO  → normal session flow

STEP 2 — SELECT ENGINE
│
├── Want intraday, equity only → intraday_equity
├── Want intraday, leveraged  → intraday_futures
├── Want intraday, defined loss → intraday_options
├── Want overnight swing, equity → delivery_equity
├── Want overnight swing, leveraged → futures_equity
└── Want overnight, options play → options_equity

STEP 3 — SELECT STRATEGY
│
├── Trend day          → MA, BREAKOUT (or ATM_MOMENTUM for options)
├── Range day          → RSI (or skip options)
├── Gap-and-go         → ORB or ATM_ORB
├── Post-consolidation → BREAKOUT or ATM_BREAKOUT_EXPANSION
├── Failed breakout    → ATM_TRAP_REVERSAL (small size)
└── Uncertain          → AUTO_ADAPTIVE

STEP 4 — SELECT RISK STYLE
│
├── First week / uncertain → CONSERVATIVE (0.5%)
├── Normal conditions      → BALANCED (1.0%)
└── Strong conviction      → AGGRESSIVE (1.5%)

STEP 5 — TUNE CONFIG IF NEEDED
│
├── Volatile day  → tighten daily_max_loss_pct, lower entry_cutoff
├── Trend day     → raise adaptive_target_multiplier_expansion
├── Choppy day    → raise cooldown_seconds, lower entry_cutoff
└── Expiry day    → lower time exit cutoff, tighten IV filters

STEP 6 — RUN BACKTEST FIRST
│
├── Run backtest on recent 5–10 days with chosen engine + strategy
├── Check win rate, avg P&L, max drawdown
├── If results reasonable → switch to paper mode for 1–2 days
└── If paper mode profitable → switch to live with CONSERVATIVE risk
```

---

## Common Mistakes

| Mistake | Why It Hurts | Fix |
|---|---|---|
| Using MA strategy on a sideways day | MA generates false crossovers in ranges → repeated stop-outs | Switch to RSI or AUTO_ADAPTIVE on flat-VIX days |
| Buying options on low-VIX days | Premium is cheap but moves are small → time decay wins | On low VIX, prefer equity strategies; if options, use very small premium targets |
| Increasing `aggressive` risk after a losing streak | Revenge trading with larger size → accelerated drawdown | Respect `consecutive_loss_limit`; step down to CONSERVATIVE after 2 losses |
| Running delivery_equity on event days | Overnight gaps can gap past stop losses | Avoid fresh delivery entries 1 day before major events |
| Ignoring entry_cutoff | Late entries have less time to work and face square-off pressure | Keep `entry_cutoff` at least 30 minutes before square-off |
| Using AUTO_ADAPTIVE without backtesting | Unknown behaviour in your symbols | Run at least 10 days of backtest before live AUTO_ADAPTIVE |
| Over-allocating to a single symbol | Max loss on one bad trade is too high | Keep `max_symbol_allocation` at default (20–25%) or lower |
| Running options without checking IV percentile | Buying when IV is high → IV crush erases gains even if direction is right | Check `max_buy_iv_percentile`; lower it on high-VIX days |
| Skipping paper mode before live | Surprises in order flow, fills, timing | Always run 1–2 days in paper mode when switching engine or strategy |

---

## SL / Target / Trailing Parameters — Formulas and Examples

This section explains exactly how stop-loss, target, trailing stop, and trailing activation are calculated for each engine, with worked examples.

### Glossary

| Term | Meaning |
|---|---|
| `entry_price` | Price at which the position was filled |
| `atr` | Average True Range of the instrument at entry time |
| `base_atr` | `max(atr, entry_price × atr_floor_pct)` — ATR floored to avoid near-zero values |
| `stop_distance` | Absolute rupee distance between entry and stop loss |
| `trailing_distance` | Absolute rupee distance the trailing stop trails below best_price |
| `trailing_activation_distance` | best_price must move this far from entry before trailing stop activates |
| `best_price` | Highest price seen since entry (BUY) or lowest (SELL) |
| `trailing_active` | Flag; once True, trailing stop fires on reversal |
| `conviction` | Multiplier `1.0 + signal_score × weight` — high-scoring signals get wider targets |
| `exit_mode` | `TRAIL_ONLY` (default): target activates trailing; `HARD_TARGET`: exit at target price |

---

### 1. Intraday Equity (`intraday_equity`)

**Instrument**: NSE equity stocks — 1-minute candles, MIS, squareoff 15:15.

**Regime classification** (determines multipliers):

| Signal Score | ATR Ratio | Range Ratio | Regime |
|---|---|---|---|
| ≥ 0.035 OR range_ratio ≥ 0.006 | any | any | EXPANSION |
| ≤ 0.008 AND atr_ratio ≤ 0.003 | low | low | SIDEWAYS |
| otherwise | — | — | NORMAL |

**ATR multipliers per regime** (from `config.runtime.yaml`):

| | Stop | Target | Trailing |
|---|---|---|---|
| SIDEWAYS | 1.6× | 1.1× | 0.8× |
| NORMAL | 1.4× | 1.6× | 1.0× |
| EXPANSION | 1.4× | 2.2× | 1.15× |

**Formulas**:
```
base_atr   = max(atr, entry_price × 0.0015)
conviction = 1.0 + min(1.0, signal_score) × 0.5

stop_distance     = max(entry_price × 0.0025,  base_atr × stop_mult)
target_distance   = max(entry_price × 0.0045,  base_atr × target_mult × conviction)
trailing_distance = max(entry_price × 0.002,   base_atr × trail_mult)
trailing_distance = max(trailing_distance, volatility_distance)   [market-breadth adjusted]

For BUY:
  stop_loss    = entry_price − stop_distance
  target       = entry_price + target_distance × 1.6   [level3]
  trailing_stop = stop_loss   (starts at SL, ratchets up)

trailing_activation_distance = max(trailing_distance, level1_distance × 0.8)
  where level1_distance = target_distance × 0.5
```

**Worked example** — SBIN.NS BUY entry ₹500, ATR = ₹5, signal_score = 0.025 → NORMAL regime:
```
base_atr   = max(5, 500 × 0.0015) = max(5, 0.75) = 5.0
conviction = 1.0 + 0.025 × 0.5 = 1.0125

stop_distance     = max(500 × 0.0025, 5.0 × 1.4) = max(1.25, 7.0) = 7.0
target_distance   = max(500 × 0.0045, 5.0 × 1.6 × 1.0125) = max(2.25, 8.1) = 8.1
trailing_distance = max(500 × 0.002, 5.0 × 1.0) = max(1.0, 5.0) = 5.0

stop_loss    = 500 − 7.0  = ₹493.00
target       = 500 + 8.1 × 1.6 = ₹512.96   [level3 = target_distance × 1.6]
trailing_stop starts at ₹493.00

trailing_activation_distance = max(5.0, 4.05 × 0.8) = max(5.0, 3.24) = 5.0
→ trailing activates once best_price reaches ₹505.00 (entry + 5.0)

After activation: trailing ratchets to best_price − 5.0 on every candle close.
With TRAIL_ONLY exit_mode: when price hits ₹512.96 target, trailing continues
  (no exit). Position exits only when price drops to trailing_stop.
```

**Partial levels** (for display / runner reference):
```
level1_target = entry + target_distance × 0.5 = ₹504.05
level2_target = entry + target_distance       = ₹508.10
level3_target = entry + target_distance × 1.6 = ₹512.96
```

---

### 2. Delivery Equity (`delivery_equity`)

**Instrument**: NSE equity stocks — daily candles, CNC, hold up to 5 days.

**Regime classification**:

| Condition | Regime |
|---|---|
| score ≥ 0.035 OR swing_range_ratio ≥ 0.035 OR atr_ratio ≥ 0.025 | EXPANSION |
| score ≤ 0.010 AND range_ratio ≤ 0.015 AND atr_ratio ≤ 0.012 | SIDEWAYS |
| otherwise | NORMAL |

**ATR multipliers per regime**:

| | Stop | Target | Trailing |
|---|---|---|---|
| SIDEWAYS | 2.4× | 1.6× | 1.2× |
| NORMAL | 2.0× | 2.6× | 1.5× |
| EXPANSION | 1.8× | 3.4× | 1.8× |

**Formulas**:
```
base_atr   = max(atr, entry_price × 0.006)
conviction = 1.0 + min(1.0, signal_score) × 0.75

stop_distance     = max(entry_price × 0.018, base_atr × stop_mult)
target_distance   = max(entry_price × 0.040, base_atr × target_mult × conviction)
trailing_distance = max(entry_price × 0.012, base_atr × trail_mult)
trailing_distance = max(trailing_distance, swing_range_distance × 1.2)

For BUY:
  stop_loss    = entry_price − stop_distance
  target       = entry_price + target_distance × 1.5   [level3]
  trailing_stop = stop_loss

Special trailing activation (delivery only):
  trailing_activation_distance = max(target_distance, atr × 0.5, 0.01)
  → trailing only activates once price reaches the target level (reference only;
    with TRAIL_ONLY the target is a floor, not a hard exit)
```

**Worked example** — INFY.NS BUY entry ₹1500, ATR = ₹30, signal_score = 0.02 → NORMAL regime:
```
base_atr   = max(30, 1500 × 0.006) = max(30, 9) = 30
conviction = 1.0 + 0.02 × 0.75 = 1.015

stop_distance     = max(1500 × 0.018, 30 × 2.0) = max(27, 60) = 60.0
target_distance   = max(1500 × 0.040, 30 × 2.6 × 1.015) = max(60, 79.2) = 79.2
trailing_distance = max(1500 × 0.012, 30 × 1.5) = max(18, 45) = 45.0

stop_loss    = 1500 − 60 = ₹1440.00
target       = 1500 + 79.2 × 1.5 = ₹1618.80   [level3]
trailing_stop starts at ₹1440.00

trailing_activation_distance = max(79.2, 15, 0.01) = 79.2
→ trailing activates only once best_price reaches ₹1579.20 (entry + 79.2)

With TRAIL_ONLY: when price hits ₹1618.80, trailing continues above that level.
  At best_price ₹1650, trailing_stop = 1650 − 45 = ₹1605.00. Exit on reversal.
```

**Note**: `delivery_equity` always used trail-only internally (`include_target=False` in evaluate_exit). The `exit_mode` flag has no additional effect here.

---

### 3. Intraday Options (`intraday_options`)

**Instrument**: NSE/NFO ATM index options — 1-minute candles, MIS, squareoff 15:15.

**All distances are relative to the option premium price** (not underlying price).

**Regime classification** (from analytics volatility_regime field):

| volatility_regime | Stop | Target | Trailing |
|---|---|---|---|
| SIDEWAYS | 2.0× | 1.1× | 0.8× |
| NORMAL | 1.7× | 1.7× | 1.0× |
| EXPANSION | 1.7× | 2.3× | 1.15× |

**Risk-style scaling** (CONSERVATIVE / BALANCED / AGGRESSIVE):

The ATR multipliers above are further scaled by the risk style's `sl_percent`, `target_percent`, `trailing_percent` relative to BALANCED values. BALANCED = 10%/20%/7.5% → multiplier = 1.0×.

**Formulas**:
```
base_atr   = max(atr, entry_price × 0.015)   [premium ATR, floored at 1.5%]
conviction = 1.0 + min(1.0, signal_score) × 0.5

stop_distance     = max(entry_price × 0.05,   base_atr × stop_mult × risk_scale)
target_distance   = max(entry_price × 0.08,   base_atr × target_mult × conviction × risk_scale)
trailing_distance = max(entry_price × 0.035,  base_atr × trail_mult × risk_scale)
trailing_distance = max(trailing_distance, premium_volatility_distance)

For BUY_CE / BUY_PE:
  stop_loss    = entry_price − stop_distance
  target       = entry_price + target_distance × 1.6   [level3]
  trailing_stop = stop_loss

trailing_activation_distance = max(trailing_distance, level1_distance × 0.8)
  where level1_distance = target_distance × 0.5
```

**Worked example** — NIFTY CE BUY at premium ₹80, ATR = ₹6, signal_score = 0.04 → NORMAL, BALANCED:
```
base_atr   = max(6, 80 × 0.015) = max(6, 1.2) = 6.0
conviction = 1.0 + 0.04 × 0.5 = 1.02

stop_distance     = max(80 × 0.05, 6 × 1.7) = max(4.0, 10.2) = 10.2
target_distance   = max(80 × 0.08, 6 × 1.7 × 1.02) = max(6.4, 10.4) = 10.4
trailing_distance = max(80 × 0.035, 6 × 1.0) = max(2.8, 6.0) = 6.0

stop_loss    = 80 − 10.2 = ₹69.80
target       = 80 + 10.4 × 1.6 = ₹96.64   [level3]
trailing_stop starts at ₹69.80

level1_target = 80 + 10.4 × 0.5 = ₹85.20
level2_target = 80 + 10.4       = ₹90.40
trailing_activation_distance = max(6.0, 5.2 × 0.8) = max(6.0, 4.16) = 6.0
→ trailing activates once best_price ≥ ₹86.00 (entry + 6)

With TRAIL_ONLY and multi-lot runner exits:
  Level1 hit (₹85.20): 30% lots exit, stop moves to entry (breakeven)
  Level2 hit (₹90.40): 40% lots exit, stop tightens, target → sentinel (1e9)
    → trailing now ratchets freely above ₹90.40
  Remaining 30% exits when price drops to trailing_stop
```

**Fixed-premium runner exits** (when `runner_partial_exit_lot_threshold` exceeded):
```
runner_level1_premium_target_pct: 8.0  → level1 = entry + entry × 8% = ₹86.40
runner_level2_premium_target_pct: 15.0 → level2 = entry + entry × 15% = ₹92.00
```

---

### 4. Futures / Options Equity and Intraday Futures

**Instrument**: NRML or MIS F&O contracts.

These engines use `build_position()` from `engines/common.py` — the simpler percentage-based formula (no adaptive regime). The session risk style sets the ATR multipliers.

**Formulas**:
```
resolve_trade_targets():
  stop_distance     = atr × atr_stop_multiplier
  stop_loss (BUY)   = entry_price − stop_distance
  target    (BUY)   = entry_price + stop_distance × target_risk_reward
  trailing_distance = atr × trailing_atr_multiplier
  trailing_stop     = stop_loss (starts at SL)
  trailing_activation_distance = max(trailing_distance, stop_distance)

build_position() [percentage-based fallback]:
  stop_loss (BUY) = entry_price × (1 − sl_pct / 100)
  target    (BUY) = entry_price × (1 + target_pct / 100)
  trailing_stop   = stop_loss
```

**Risk style multipliers** (intraday engines):

| Style | ATR Stop Mult | ATR Trail Mult | Target RR | Risk % |
|---|---|---|---|---|
| CONSERVATIVE | 1.2× | 0.8× | 1.4× | 0.5% |
| BALANCED | 1.35× | 0.9× | 1.5× | 1.0% |
| AGGRESSIVE | 1.5× | 1.0× | 1.6× | 1.5% |

**Risk style multipliers** (positional engines):

| Style | ATR Stop Mult | ATR Trail Mult | Target RR | Risk % |
|---|---|---|---|---|
| CONSERVATIVE | 1.5× | 1.0× | 1.8× | 0.5% |
| BALANCED | 1.65× | 1.25× | 2.0× | 1.0% |
| AGGRESSIVE | 1.8× | 1.5× | 2.2× | 1.5% |

**Worked example** — NIFTY Futures BUY at ₹22000, ATR = ₹80, BALANCED intraday:
```
stop_distance     = 80 × 1.35 = 108
stop_loss         = 22000 − 108 = ₹21892
target            = 22000 + 108 × 1.5 = ₹22162
trailing_distance = 80 × 0.9 = 72
trailing_stop starts at ₹21892
trailing_activation_distance = max(72, 108) = 108
→ trailing activates once best_price ≥ ₹22108 (entry + 108)
```

---

### 5. How the Trailing Stop Moves (All Engines)

Once `trailing_active = True`:

```
On every candle close (and intra-candle via WebSocket ticks for intraday_options):

BUY position:
  best_price = max(best_price, current_close)
  new_candidate = best_price − trailing_distance
  trailing_stop = max(trailing_stop, new_candidate)  ← only moves UP, never down

SELL position:
  best_price = min(best_price, current_close)
  new_candidate = best_price + trailing_distance
  trailing_stop = min(trailing_stop, new_candidate)  ← only moves DOWN, never up
```

**Example** — BUY, entry ₹500, trailing_distance ₹5, trailing_active=True:
```
Close ₹510 → best=510, trailing_stop = max(prev, 510−5) = 505
Close ₹515 → best=515, trailing_stop = max(505, 510) = 510
Close ₹512 → best=515 (no update), trailing_stop = 510 (no update)
Close ₹509 → best=515, trailing_stop = 510 → LOW ≤ 510 → EXIT TRAILING_STOP
```

---

### 6. TRAIL_ONLY vs HARD_TARGET Comparison

| Scenario | HARD_TARGET | TRAIL_ONLY (default) |
|---|---|---|
| Price hits target ₹512, reverses | Exits at ₹512 | Trailing activates; trails freely |
| Price blows through ₹512 to ₹520 | Exits at ₹512 (misses ₹8) | Trails to ₹515, exits on reversal |
| Price hits target, whipsaws back | Exits at ₹512 | Exits at trailing stop (below entry possible if fast reversal) |
| Best for | Sideways range-bound plays | Strong directional / trending days |

**Config**: `execution_safety.exit_mode: "TRAIL_ONLY"` in `config.runtime.yaml`.
**Per-session override**: Exit Mode dropdown in the web UI configure form.
