# How To Use — Operator Guide

Practical guide for running backtests, paper sessions, and live sessions.

---

## Quick Start

```powershell
# Live or paper session
run_main.bat

# Interactive backtest
run_backtest.bat

# Run tests (do this before any live change)
venv\Scripts\python -m pytest
```

Or directly:

```powershell
venv\Scripts\python main.py
venv\Scripts\python backtesting.py
```

---

## Terminal Display

Sessions and backtests print a colored layout to the terminal. Colors are automatically suppressed when stdout is not a TTY.

- **Startup**: full config summary printed once showing engine, mode, capital, risk style, strategies, and all active limits
- **Each cycle**: open positions table with green/red P&L; ranked candidates table with scores
- **Session end**: trade book table with all closed trades and net P&L per trade
- **Backtest end**: summary table with equity curve stats, win rate, and per-trade averages

Log files written to `logs/` are plain text — ANSI codes are stripped before writing so they remain readable without color support.

---

## Three Modes

| Mode | Command | Real orders | Best for |
| --- | --- | --- | --- |
| **Backtest** | `backtesting.py` | No | Historical validation before paper |
| **Paper** | `main.py` → select PAPER | No — simulated fills | Market-hours dry run with full position tracking |
| **Live** | `main.py` → select LIVE | Yes | Production trading |

**Always validate in this order: Backtest → Paper → Live.**

### What paper mode does now

Paper mode creates real in-memory positions. `executor.place_order` returns a simulated fill at the entry price, so positions are tracked with stops, trailing stops, partial exits, and all risk guards active — exactly like live mode, but with no broker connection. Logs show `[PAPER] Simulated fill: BUY 50 SBIN.NS @ 512.30`.

---

## Live / Paper Session (`main.py`)

### What it asks you

1. Engine (1–6)
2. Execution mode: `PAPER` or `LIVE`
3. Data provider and execution provider (auto-forced to Kite for F&O)
4. Capital
5. Symbols or F&O contract (underlying, expiry, structure, strike mode)
6. Risk style: `CONSERVATIVE`, `BALANCED`, `AGGRESSIVE`
7. Max open positions and capital limits
8. One-trade-per-symbol-per-day: yes/no
9. Entry selection: `TOP 1` or `TOP N`
10. Strategy mode and strategy

### Special-case rules

- F&O engines (`futures_equity`, `options_equity`, `intraday_futures`, `intraday_options`) automatically force data provider and execution provider to Kite
- Single-structure F&O sessions auto-force `max_open_positions = 1` and `TOP 1`
- `intraday_options` reads lot mode (`ONE_LOT` / `CAPITAL_BASED`) and entry mode (`LIVE_STAGED` / `LEGACY_IMMEDIATE`) from `config.runtime.yaml` instead of prompting

### Session defaults from config

These come from `config.runtime.yaml` and apply without prompting:

- `session_defaults.exit_only_default` — if `true`, session starts in exit-only mode
- `session_defaults.live_broker_resync_interval_seconds` — how often live positions sync with broker

**Exit-only mode**: existing positions are managed normally; no new entries are placed. Use it to safely wind down a session.

---

## Backtest (`backtesting.py`)

### What it asks you

1. Engine
2. Capital
3. Symbol or F&O contract setup (underlying, expiry, structure, strike mode)
4. Risk style
5. Max positions and capital limits
6. One-trade-per-symbol-per-day
7. Entry selection mode
8. Strategy mode and strategy
9. Period and interval

### Tick-entry simulation

To simulate early intra-candle entry instead of always filling at candle close, add `tick_entry_enabled=True` when building a `BacktestConfig` programmatically:

```python
from backtesting import BacktestConfig, BacktestEngine

config = BacktestConfig(
    engine_name="intraday_equity",   # or "intraday_options"
    capital=100_000,
    tick_entry_enabled=True,         # model early breakout fills
    ...
)
results = BacktestEngine(config).run(history)
print(f"Tick-entry fills: {results['tick_entry_fills']}")
```

When enabled, fill price = `max(candle_open, close − ATR×0.40)` for BUY — reflecting "we entered at the breakout level during the candle, not at the close."

| Engine | Candle used for simulation | Notes |
| --- | --- | --- |
| `intraday_equity` | Underlying 1-minute candle | Standard breakout fill |
| `intraday_futures` | Futures 3-minute candle | Standard breakout fill |
| `intraday_options` | **Option premium candle** | Captures ORB/momentum spike entry; significantly better than candle close on large-move days |
| `delivery_equity` | **Disabled** | Daily candles; no intra-candle benefit |

### Backtest output

Results go to `Results/BackTest/`:

- `backtest_<engine>_<timestamp>_summary.txt`
- `backtest_<engine>_<timestamp>_trades.csv`
- `backtest_<engine>_<timestamp>_equity.csv`

Key fields in the trades CSV: `entry_price`, `candle_close_price`, `tick_entry`, `exit_price`, `exit_reason`, `pnl`, `estimated_charges`, `net_pnl`.

---

## Tick-Based Entry (Live / Paper)

After each scan cycle detects a signal, the system watches the live price for the remaining time in that cycle. If price crosses the breakout trigger level, an entry fires immediately — without waiting for the next candle to close.

### How to know it is working

Look for log lines like:

```
[TICK-ENTRY] Armed NSE:RELIANCE BUY trigger=2541.80 (close=2539.30 atr=24.50)
[TICK-ENTRY] LTP trigger: NSE:RELIANCE @ 2542.10 (trigger=2541.80) via LTP poll
[TICK-ENTRY] Position opened: NSE:RELIANCE @ 2542.10 [PAPER]
```

For LIVE + Kite, WebSocket ticks replace LTP polling when the connection is available:

```
[TICKER] KiteConnect WebSocket ticker started
[TICK-ENTRY] Using WebSocket tick stream
[TICK-ENTRY] WS trigger: NFO:NIFTY25JUN24500CE @ 148.50
```

### Watch windows per engine

| Engine | Watch window | Cycle length |
| --- | ---: | ---: |
| `intraday_equity` | 52 s | 60 s |
| `intraday_options` | 11 s | 15 s |
| `intraday_futures` | 52 s | 60 s |
| `futures_equity` | 52 s | 60 s |
| `options_equity` | 52 s | 60 s |
| `delivery_equity` | disabled | 300 s |

### WebSocket token mapping (for developers)

Instrument tokens must be mapped before WebSocket subscriptions fire. After `build_trading_context`, populate:

```python
context._symbol_token_map = {
    "NSE:RELIANCE": 738561,
    "NSE:NIFTY 50": 256265,
}
ticker = context._ticker_manager
if ticker:
    ticker.subscribe(list(context._symbol_token_map.values()))
```

If the token map is empty, the system falls back to REST LTP polling automatically.

---

## Engine Guide

### 1. Intraday Equity

Engine id: `1` | Name: `intraday_equity`

- Intraday long and short stock entries on MIS
- Squares off before 15:15
- Strategies: `MA`, `RSI`, `VWAP`, `BREAKOUT`, `ORB`, `AUTO_ADAPTIVE`
- Adaptive stop/target/trailing from ATR, recent range, and signal score
- Backtest default: `5d` / `5m`

**When to start here**: simplest engine; works with YFinance data; no F&O account needed.

#### Auto-Adaptive routing

| Context | Strategies | Confirmations |
| --- | --- | ---: |
| Gap + continuation | `ORB`, `VWAP`, `BREAKOUT` | 2 |
| Gap + fill | `ORB`, `VWAP` | 2 |
| Gap + sideways | `VWAP`, `RSI` | 2 |
| No-gap normal | `MA`, `RSI` | configured normal count |
| Opening range not ready | waits | — |

### 2. Delivery Equity

Engine id: `2` | Name: `delivery_equity`

- Long-only delivery positions on daily candles
- Strategies: `MA`, `RSI`, `BREAKOUT`
- Nifty 50-DMA trend guard required for new long entries
- Target is a reference; exits rely on stop/trailing/sell-signal/time limit
- Swing-style adaptive stop (minimum 1.8% floor); trailing is wider/slower than intraday to handle overnight gaps

### 3. Futures Equity

Engine id: `3` | Name: `futures_equity`

- Positional NRML index futures
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; Kite only

### 4. Options Equity

Engine id: `4` | Name: `options_equity`

- Positional NRML index options
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; Kite only

### 5. Intraday Futures

Engine id: `5` | Name: `intraday_futures`

- Intraday MIS index futures; entry cutoff 15:05; square-off 15:15
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; Kite only

### 6. Intraday Options

Engine id: `6` | Name: `intraday_options`

- Intraday ATM single-option flow or two-leg bounded range pair
- Strategies: `ATM_MOMENTUM`, `ATM_ORB`, `ATM_VWAP_REVERSION`, `ATM_MULTI`, `ATM_BREAKOUT_EXPANSION`, `ATM_IV_EXPANSION`, `ATM_TRAP_REVERSAL`
- Dynamic ATM strike resolution and rolling; staged momentum entry; three-level runner partial exits
- Greeks, IV, spread, OI, expiry, cost, and vega-crush filters
- Config-driven lot mode and entry mode:

| Config key | Values |
| --- | --- |
| `fno.intraday_options_lot_mode` | `ONE_LOT`, `CAPITAL_BASED` |
| `fno.intraday_options_entry_mode` | `LIVE_STAGED`, `LEGACY_IMMEDIATE` |

**Recommended workflow**: Backtest → Paper with staged mode → Live only after logs show correct contract, stop, target, and runner behavior.

---

## Strategy and Candle Reference

Candle counts below are in the engine's active interval (live cadence).

### Shared equity / futures strategies

| Strategy | Min candles | First actionable | What triggers entry |
| --- | ---: | ---: | --- |
| `MA` | 50 | 51 | MA20 crosses MA50 |
| `RSI` | 14 | 15 | RSI < 30 (BUY) or > 70 (SELL) |
| `BREAKOUT` | 20 | 22 | Close above prior 20-candle high / below low |
| `VWAP` | 1 | 6 | Close above / below cumulative VWAP |
| `ORB` | 20 | 21 | Close above / below first-15-candle range |

"First actionable" is higher because `confirm_signal()` checks both the previous and current candle windows; both must agree.

### Intraday-options strategies

| Strategy | Min candles | Entry condition |
| --- | ---: | --- |
| `ATM_MOMENTUM` | 20 | RSI + VWAP + 5-candle breakout alignment |
| `ATM_ORB` | 16 | Close beyond first-15-candle range |
| `ATM_VWAP_REVERSION` | 20 | Price re-enters VWAP after 6-candle deviation |
| `ATM_MULTI` | 20 | Aligned momentum + ORB, or VWAP reversion in sideways regime |
| `ATM_BREAKOUT_EXPANSION` | 45 | Compression + breakout + volume spike + ATR expansion |
| `ATM_IV_EXPANSION` | 30 | Key-level breakout + oversized candle body + RSI confirmation |
| `ATM_TRAP_REVERSAL` | 24 | Failed support/resistance break + oversized reversal body |

---

## Risk Styles

Each engine uses engine-aware risk presets (intraday vs positional buckets are separate).

| Style | ATR stop | Trailing | Target R/R | Risk per trade |
| --- | --- | --- | --- | --- |
| `CONSERVATIVE` | tighter | slower | lower | smaller |
| `BALANCED` | moderate | moderate | moderate | moderate |
| `AGGRESSIVE` | wider | faster | higher | larger |

These control: ATR stop multiplier, ATR trailing multiplier, target risk-reward, fallback percent stop/target/trailing, and capital risk percent.

---

## High-Value Config Knobs

Review these before going live. All live in `config.runtime.yaml`; a process restart is required after changes.

### Session and safety

| Knob | Default | Effect |
| --- | --- | --- |
| `session_defaults.exit_only_default` | false | Start in exit-only mode |
| `session_defaults.live_broker_resync_interval_seconds` | 0 | Broker position resync interval |
| `execution_safety.min_ranked_candidate_score` | 0.008 | Minimum signal score for entry |
| `execution_safety.reversal_exit_confirmation_candles` | 2 | Opposite candles before reversal exit |
| `orders.default_entry_order_type` | `MARKET` | `MARKET` or `LIMIT` |
| `orders.max_live_order_notional` | 0 (off) | Blocks oversized individual orders |
| `orders.margin_check_enabled` | true | Pre-flight margin check |
| `orders.max_spread_pct` | 0 (off) | Blocks wide-spread entries |
| `orders.max_orders_per_minute` | 0 (off) | Rate-limits live orders |
| `risk_controls.daily_max_loss_pct` | 0 (off) | Blocks new entries after daily loss limit |
| `risk_controls.consecutive_loss_limit` | 0 (off) | Blocks after N consecutive losses |
| `fno.intraday_options_max_entry_cost_ratio` | 0.30 | Max cost/profit ratio for options entry |
| `fno.intraday_options_max_spread_pct` | 0 (off) | Spread filter for option entries |
| `fno.intraday_options_min_open_interest` | 0 (off) | OI filter for option entries |
| `fno.intraday_options_roll_trigger_pct` | 0 (off) | ATM roll trigger for live positions |
| `fno.intraday_options_theta_exit_ratio` | 0 (off) | Theta-aware exit guard |

### Per-engine timing and data (under `engine_defaults.<engine>`)

All six engines expose these; defaults shown are for `intraday_options`.

| Knob | Default | Effect |
| --- | --- | --- |
| `sleep_seconds` | 15 | Session polling interval; lower = faster scans but more API calls |
| `cooldown_seconds` | 180 | Wait after entry before next entry is considered |
| `data_period` | `"1d"` | History window fetched each scan cycle |
| `data_interval` | `"1m"` | Candle granularity fetched each scan cycle |
| `entry_cutoff` | `"15:05"` | Stop new entries after this time (intraday engines only) |
| `square_off_time` | `"15:15"` | Force-exit all positions after this time (intraday engines only) |

### Adaptive level multipliers (under `engine_defaults.<engine>`)

Control stop/target/trailing distances per volatility regime. Available for `intraday_equity`, `delivery_equity`, and `intraday_options`.

| Knob | Example | Effect |
| --- | --- | --- |
| `adaptive_stop_multiplier_expansion` | 1.7 | ATR multiple for stop in trending/expansion regime |
| `adaptive_target_multiplier_expansion` | 2.3 | ATR multiple for target in trending regime |
| `adaptive_trailing_multiplier_expansion` | 1.15 | ATR multiple for trailing stop in trending regime |
| `adaptive_min_stop_pct` | 0.05 | Hard floor for stop distance as % of entry price |
| `adaptive_min_target_pct` | 0.08 | Hard floor for target distance as % of entry price |
| `adaptive_conviction_score_weight` | 0.5 | How much signal score stretches the target (higher = bigger targets on strong signals) |
| `volatility_trailing_range_multiplier` | 1.2 | Swing range × this = volatility trailing distance (`delivery_equity` only) |

---

## Where Results Are Stored

| Location | Contents |
| --- | --- |
| `logs/` | Session event logs |
| `state/` | Runtime state: positions, trade counts, regime cache |
| `state/trade_store/` | Trade records and order-audit trail |
| `Results/BackTest/` | Backtest summary, trades CSV, equity CSV |

---

## Recommended Workflows

### Start here (safest)

- Engine: `intraday_equity`
- Mode: `PAPER`
- Provider: `YFINANCE`
- Risk style: `BALANCED`
- Strategy: `AUTO_ADAPTIVE`
- Entry selection: `TOP 1`

### Swing stock positions

- Engine: `delivery_equity`
- Mode: `PAPER` first, then `LIVE`
- Check: Nifty trend guard active; trailing wider than intraday

### Intraday options

1. Backtest with `intraday_options` + `ATM_BREAKOUT_EXPANSION` or `ATM_MOMENTUM`
2. Check `config.runtime.yaml`: set `intraday_options_entry_mode = LIVE_STAGED`
3. Paper during market hours; inspect logs for correct contract, stop, target, and runner behavior
4. Go live only after at least 5 paper sessions look correct

---

## Troubleshooting

### Session starts in exit-only mode

Check `session_defaults.exit_only_default` in `config.runtime.yaml`. Set to `false` to allow entries.

### F&O provider choice is ignored

Expected. F&O sessions force both data and execution provider to Kite. This cannot be changed in the prompt flow.

### No trades entered after signal fires

Check in order:

1. `execution_safety.min_ranked_candidate_score` — score too low?
2. Cost gate: look for `[SKIP] Trade not profitable after costs` in logs
3. Capital gate: `max_capital_deployed` or `max_capital_per_trade` exceeded?
4. One-trade-per-day: symbol already traded today?
5. Risk pause: `[RISK]` log lines showing a pause is active?
6. `session_defaults.exit_only_default` — accidentally in exit-only mode?

### Tick entry not firing

1. Check that the engine is not `delivery_equity` (tick entry disabled on daily candles)
2. Look for `[TICK-ENTRY] Armed ...` log lines — if absent, no candidates reached the tick-entry stage
3. For WebSocket: `[TICKER] WebSocket did not connect` means REST LTP fallback is being used — this is normal if credentials are unavailable or in PAPER mode
4. Check that the trigger price is realistic: `trigger = close ± (ATR × 0.10)` — if ATR is very small the trigger may be too close to the close price to observe movement in the watch window

### Backtest and live target behavior look different

Check:
- Same risk style selected?
- Same engine adaptive builder used in both paths? Check `engines/common.py` `resolve_trade_targets` and the engine's `build_trend_adaptive_position`

### Intraday backtest carries a position overnight

Intraday equity and intraday futures backtests enforce entry cutoff and force square-off. If a position is carrying overnight, check that `engine.get_cycle_state` is being called in `_process_timestamp` and that the engine name is one of the intraday engines.

### Paper positions disappear after restart

Paper positions are persisted to `state/`. If `state/` was cleared, positions are lost. Paper positions are not reconciled from a broker because no real broker order was placed.

### Intraday options entered too aggressively

Review:
- `fno.intraday_options_entry_mode` — use `LIVE_STAGED` for confirmation before entry
- `fno.intraday_options_max_entry_cost_ratio` — lower to 0.20 to require better edge
- `fno.intraday_options_max_spread_pct` — add a spread filter to avoid illiquid contracts

### Tests before live release

```powershell
venv\Scripts\python -m pytest
```

Expected result: `116 passed, 1 pre-existing failure` (the 1 failure is an intraday-options cycle-state boundary edge case unrelated to entry, exit, or risk).
