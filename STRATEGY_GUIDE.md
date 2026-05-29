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

---

## Engine Overview

| Engine | Instrument | Time Horizon | Product | Suitable For |
|---|---|---|---|---|
| `intraday_equity` | NSE equity stocks | Same-day (squareoff 15:15) | MIS | Active intraday traders |
| `delivery_equity` | NSE equity stocks | Multi-day (up to 5 days) | CNC | Swing / positional traders |
| `intraday_futures` | NSE/NFO index/stock futures | Same-day (squareoff 15:15) | MIS | Leverage intraday traders |
| `intraday_options` | NSE/NFO index/stock options | Same-day (squareoff 15:15) | MIS | Options intraday traders |
| `futures_equity` | NSE/NFO futures | Multi-day positional | NRML | Positional futures traders |
| `options_equity` | NSE/NFO options | Multi-day positional | NRML | Positional options traders |

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

### intraday_options

**What it does:** The most feature-rich intraday engine. Trades ATM or near-ATM options in a defined-loss / defined-reward structure. Supports four specialized option strategies (ATM_MOMENTUM, ATM_ORB, ATM_BREAKOUT_EXPANSION, ATM_TRAP_REVERSAL). Has regime detection (SIDEWAYS/NORMAL/EXPANSION), IV percentile filter, open interest guard, theta-decay exit, partial/runner exit, and tick-entry simulation in backtest.

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
- `intraday_options_max_hold_minutes: 30` — time stop; options bleed theta each minute
- `intraday_options_vega_crush_block_percent: 20` — exits/blocks when IV drops sharply

**Adaptive ATR levels for options (wider than equity):**
- SIDEWAYS: stop ×2.0, target ×1.1 — tight stop, small target
- NORMAL: stop ×1.7, target ×1.7 — symmetric R:R
- EXPANSION: stop ×1.7, target ×2.3 — let winners run on strong expansion

**Lot mode:** Default `CAPITAL_BASED` — lot size scales with your declared capital. Switch to `FIXED` if you want to control lots manually.

**Entry mode:** Default `LEGACY_IMMEDIATE` — enters on signal candle. Switch to `STAGED` (ATM_MOMENTUM) for a pullback-confirmation entry at a better fill price.

**Key knobs to tune:**
- `intraday_options_max_hold_minutes` — reduce to `20` on choppy days to cut time risk
- `max_buy_iv_percentile` — reduce to `60` on high-VIX days to avoid buying at the top of IV
- `runner_level1_premium_target_pct` / `runner_level2_premium_target_pct` — tune runner exit levels based on your observed premium ranges

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
| `consecutive_loss_limit` | 4 | Blocks entries after 4 consecutive losses |
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
