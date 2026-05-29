# Zerodha Algo Trading Bot

Algorithmic trading and backtesting system for Indian markets (NSE/BSE/NFO/BFO).
Supports six trading engines across three execution modes with shared signal, risk, and position logic.

For day-to-day operator instructions, read [HOW_TO_USE.md](./HOW_TO_USE.md).

---

## Execution Modes

| Mode | Entry point | Real orders | Use for |
| --- | --- | --- | --- |
| **Backtest** | `backtesting.py` | No | Historical simulation |
| **Paper** | `main.py` | No — simulated fills | Market-hours dry run |
| **Live** | `main.py` | Yes — Kite / Upstox | Production trading |

**Paper mode now simulates fills.** `executor.place_order` returns a synthetic `OrderResult` in PAPER mode so positions are tracked in memory exactly as they would be in LIVE mode. This enables the full position lifecycle (stop-loss, trailing, target, exit) to run during paper sessions without connecting to a broker.

---

## Six Trading Engines

| # | Engine | Product | Candle cadence | Typical use |
| --- | --- | --- | --- | --- |
| 1 | `intraday_equity` | `MIS` | `1m` live / `5m` backtest default | Intraday stock trading |
| 2 | `delivery_equity` | `CNC` | `1d` | Swing and delivery positions |
| 3 | `futures_equity` | `NRML` | `5m` live / `15m` backtest | Positional index futures |
| 4 | `options_equity` | `NRML` | `15m` | Positional index options |
| 5 | `intraday_futures` | `MIS` | `3m` live / `5m` backtest | Intraday index futures |
| 6 | `intraday_options` | `MIS` | `1m` | Intraday ATM option runner |

---

## Tick-Based Entry System (`tick_entry/`)

### Problem solved

The standard polling loop fetches closed candles every 60 seconds (15 seconds for intraday options). On a large breakout candle that moves 1–3%, the entry fires at the **top of the candle close** — missing most of the move.

### How it works

After each scan cycle detects a signal, the `TickEntryManager` watches the live price for the remaining time in the cycle and fires an entry as soon as price crosses the breakout trigger level — without waiting for the next closed candle.

| Mode | Price source | Entry mechanism |
| --- | --- | --- |
| **Live** | KiteConnect WebSocket (`KiteTicker`) with REST LTP fallback | Callback fires immediately on tick crossing trigger |
| **Paper** | REST LTP poll every 3–10 seconds | Entry fires mid-cycle; fill simulated via paper `OrderResult` |
| **Backtest** | Candle OHLC check — no network | Fill price = `max(candle_open, close − ATR×0.40)` for BUY |

### Per-engine watch windows

| Engine | Watch window | Poll interval | Notes |
| --- | --- | --- | --- |
| `intraday_equity` | 52 s | 8 s | 60 s cycle |
| `intraday_options` | 11 s | 3 s | 15 s cycle — highest value here |
| `intraday_futures` | 52 s | 8 s | 60 s cycle |
| `futures_equity` | 52 s | 10 s | 60 s cycle |
| `options_equity` | 52 s | 10 s | 60 s cycle |
| `delivery_equity` | **disabled** | — | Daily candles; no intra-candle benefit |

### Trigger level

The live trigger is the last closed candle close ± (ATR × 0.10). The small buffer prevents noise entries while keeping the price threshold close to the signal level.

### Backtest tick simulation

Enable in `BacktestConfig`:

```python
config = BacktestConfig(
    engine_name="intraday_equity",
    tick_entry_enabled=True,   # default is False
    ...
)
```

When enabled, fill price = `max(candle_open, close − ATR×0.40)` for BUY — modelling "we entered during the signal candle at the breakout level, not at its close." The summary reports `tick_entry_fills: N`.

### WebSocket startup

In LIVE + KITE mode, `build_trading_context` automatically starts `KiteTickerManager`. If the WebSocket cannot connect within 15 seconds it logs a warning and falls back to REST LTP polling.

---

## Architecture

### Key design rules

- **Backtest parity**: backtesting uses the same `scan_symbols`, `should_enter_trade`, `resolve_trade_targets`, and engine position builders as live/paper. No parallel logic.
- **Shared target resolution**: stop, target, trailing distance, and activation distance all flow through `resolve_trade_targets(...)` in `engines/common.py`.
- **Paper = tracked**: paper mode creates real in-memory positions via simulated fills; exits, trailing stops, and risk guards all run normally.
- **Tick entry is additive**: the existing closed-candle scan loop runs first; tick entry monitors only fire for candidates not yet entered.

### Module map

| Path | Purpose |
| --- | --- |
| `main.py` | Thin launcher for live / paper sessions |
| `backtesting.py` | Interactive candle-replay backtester |
| `cli/configuration.py` | Interactive prompt flow; builds `SessionConfig` |
| `orchestration/context.py` | Wires engine, broker, data, logger, config into `TradingContext`; starts WebSocket ticker |
| `orchestration/session.py` | Main 60-second supervision loop; calls scan, entry, position management, tick entry |
| `orchestration/signal_workflow.py` | Per-symbol scan / signal evaluation; `get_stable_signal_data` candle gating |
| `orchestration/positions.py` | Position lifecycle helpers: exits, trailing, partial exits, square-off, trade recording |
| `engines/` | Six engine classes; each owns `get_cycle_state`, signal normalization, exit evaluation, adaptive position builders |
| `engines/common.py` | Shared helpers: `build_position`, `resolve_trade_targets`, `update_trailing_stop`, capital limits |
| `engines/base.py` | `TradingEngine` abstract base class |
| `executor.py` | `place_order`: validation, margin check, spread check, broker submission, paper fill simulation |
| `brokers/clients.py` | Concrete `KiteBrokerClient` and `UpstoxBrokerClient` implementations |
| `brokers/base.py` | `BrokerClient` ABC; `OrderResult`, `Quote`, `OrderRequest`, `OrderStatus` types |
| `tick_entry/` | Tick-based entry system: engine config, trigger levels, `TickEntryManager`, backtest simulator |
| `data_providers/` | Provider plugin system: Kite, Upstox, YFinance; shared `MarketDataService` |
| `data_providers/kite_ticker.py` | KiteConnect WebSocket manager; `arm_breakout`, `get_ltp`, singleton helpers |
| `signal_scoring.py` | Ranking scores; `evaluate_symbol_signal`, `rank_candidates` |
| `strategy.py` | All strategy signal functions |
| `indicators.py` | `compute_atr`, `compute_rsi`, `compute_vwap` |
| `risk_manager.py` | `position_size`, `atr_position_size`, `atr_stop_from_value`, `update_trailing_stop` |
| `config.py` | Runtime config model, defaults, `get_runtime_config()` |
| `config.runtime.yaml` | Local runtime overrides |
| `state/` | Runtime state persistence: positions, trade counts, regime cache |
| `state/trade_store/` | Trade and order-audit records |
| `logs/` | Session logs |
| `Results/BackTest/` | Backtest output: summary, trades CSV, equity CSV |
| `tests/unit/` | Unit test suite |

---

## Engine Reference

### 1. Intraday Equity (`intraday_equity`)

- Long and short MIS intraday entries
- Strategies: `MA`, `RSI`, `VWAP`, `BREAKOUT`, `ORB`, `AUTO_ADAPTIVE`
- VWAP bias filter; breakout volume filter; reversal exit confirmation
- Entry cutoff 30 minutes before 15:15 square-off
- Adaptive position builder: classifies `SIDEWAYS` / `NORMAL` / `EXPANSION` regime from ATR, recent range, and signal score; stores adaptive stop, target levels, trailing distance, activation distance, and range volatility distance
- Quantity sized from adaptive stop distance

#### Auto-Adaptive strategy routing

| Market context | Strategy basket | Confirmations |
| --- | --- | ---: |
| `GAP_UP`/`GAP_DOWN` + `GAP_GO` | `ORB`, `VWAP`, `BREAKOUT` | 2 |
| `GAP_UP`/`GAP_DOWN` + `GAP_FILL` | `ORB`, `VWAP` | 2 |
| Gap + `SIDEWAYS` | `VWAP`, `RSI` | 2 |
| `NO_GAP` / normal | `MA`, `RSI` | configured normal count |
| `PENDING_OPEN_RANGE` | waits; no entry | — |

### 2. Delivery Equity (`delivery_equity`)

- Long-only CNC delivery positions on daily candles
- Strategies: `MA`, `RSI`, `BREAKOUT`
- Nifty 50-DMA trend guard for new long entries
- Swing-style adaptive position builder: daily ATR, recent 3–10 daily ranges × 1.2 volatility distance, 1.8% minimum stop floor
- Target is a reference level only; delivery exits rely on stop / trailing / sell-signal / time-based logic
- Time-based exit: configurable max hold days

### 3. Futures Equity (`futures_equity`)

- Positional NRML index futures
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; startup reconciliation from broker positions
- Provider forced to Kite

### 4. Options Equity (`options_equity`)

- Positional NRML index options
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; startup reconciliation from broker positions
- Provider forced to Kite

### 5. Intraday Futures (`intraday_futures`)

- Intraday MIS index futures
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Entry cutoff at 15:05; forced square-off at 15:15
- Lot-size-aware sizing

### 6. Intraday Options (`intraday_options`)

- Intraday ATM single-option flow; optional two-leg bounded range pair in live/paper
- Strategies: `ATM_MOMENTUM`, `ATM_ORB`, `ATM_VWAP_REVERSION`, `ATM_MULTI`, `ATM_BREAKOUT_EXPANSION`, `ATM_IV_EXPANSION`, `ATM_TRAP_REVERSAL`
- Dynamic ATM strike selection and rolling for open long option flows
- Greeks, IV, delta, spread, open-interest, expiry, cost, and vega-crush filters
- Staged momentum entry: breakout confirmation + pullback re-entry sequence
- Legacy immediate entry mode also available
- Trend-adaptive runner: premium-volatility-aware trailing; three-level partial exits
- Require-closed-signal-candle flag (`require_closed_signal_candle = True`) — makes tick entry especially valuable here

---

## Strategy Reference

### Shared equity / futures strategies

| Strategy | Min candles | First actionable | Signal rule |
| --- | ---: | ---: | --- |
| `MA` | 50 | 51 | `BUY` when MA20 > MA50; `SELL` when MA20 < MA50 |
| `RSI` | 14 | 15 | `BUY` below 30; `SELL` above 70 |
| `BREAKOUT` | 20 | 22 | `BUY` above prior 20-candle high; `SELL` below prior 20-candle low |
| `VWAP` | 1 | 6 | `BUY` above cumulative VWAP; `SELL` below |
| `ORB` | 20 | 21 | `BUY` above first-15-candle high; `SELL` below first-15-candle low |

First actionable is higher than the configured minimum because `confirm_signal()` requires both the previous and current candle windows to agree.

### Intraday-options strategies

| Strategy | Min candles | Core logic |
| --- | ---: | --- |
| `ATM_MOMENTUM` | 20 | RSI14 + VWAP + 5-candle breakout; CE on bullish, PE on bearish |
| `ATM_ORB` | 16 | First 15 session candles; CE above ORB high, PE below ORB low |
| `ATM_VWAP_REVERSION` | 20 | 6-candle prior VWAP deviation; CE/PE on re-entry through VWAP |
| `ATM_MULTI` | 20 | Aligned momentum + ORB; or VWAP reversion in low-ATR regime |
| `ATM_BREAKOUT_EXPANSION` | 45 | 45-candle compression + 30-candle breakout + volume spike + ATR expansion |
| `ATM_IV_EXPANSION` | 30 | 20-candle key level + 10-candle body average + RSI; body >= 1.8× average |
| `ATM_TRAP_REVERSAL` | 24 | 20-candle support/resistance + 3 trap candles + reversal body >= 1.5× average |

---

## Shared Trading Features

- Cost-aware trade gate: entry blocked when projected net profit after charges is negative or cost-to-profit ratio exceeds limit
- Risk-style presets: `CONSERVATIVE`, `BALANCED`, `AGGRESSIVE` (engine-aware; separate intraday and positional buckets)
- Max open positions, max capital per trade, max capital deployed
- One-trade-per-symbol-per-day control
- Daily loss limit and consecutive loss limit risk controls
- Order-rate limit guard (`max_orders_per_minute`)
- Abnormal slippage detection and automatic pause
- Spread and margin pre-flight checks before live orders
- Order audit trail and trade store under `state/trade_store/`
- Per-cycle data caching to reduce redundant API calls
- Startup position reconciliation from broker for F&O and delivery engines
- Runtime state persistence under `state/` across restarts

---

## Runtime Configuration

Defaults live in `config.py`. Override in `config.runtime.yaml`.

Key sections and fields:

| Section | Notable fields |
| --- | --- |
| `execution_safety` | `min_ranked_candidate_score`, `reversal_exit_confirmation_candles`, `intraday_equity_entry_cutoff_minutes_before_squareoff` |
| `orders` | `default_entry_order_type`, `entry_limit_price_buffer_pct`, `max_live_order_notional`, `max_spread_pct`, `margin_check_enabled`, `max_orders_per_minute` |
| `session_defaults` | `exit_only_default`, `live_broker_resync_interval_seconds` |
| `risk_controls` | `daily_max_loss_pct`, `consecutive_loss_limit`, `api_failure_pause_minutes`, `abnormal_slippage_pause_pct` |
| `fno` | `intraday_options_lot_mode`, `intraday_options_entry_mode`, `intraday_options_max_entry_cost_ratio`, `intraday_options_max_spread_pct`, `intraday_options_min_open_interest`, `intraday_options_roll_trigger_pct`, `intraday_options_theta_exit_ratio` |
| `engine_defaults.intraday_equity` | `gap_threshold_percent`, `opening_range_candles`, `breakout_volume_multiplier` |
| `engine_defaults.delivery_equity` | Per-symbol allocation cap, max hold days |
| `data_cache` | `per_cycle_enabled` |
| `transaction_costs` | Cost model toggle and slippage settings |

---

## Live vs Backtest Parity

- Signal evaluation, cost gating, risk-style targets, and trailing updates share the same code in both paths
- Engine adaptive position builders (intraday equity, delivery equity, intraday options) are called in both live and backtest entry paths
- Backtest trailing updates read stored `trailing_distance` from the position — no hardcoded zero
- Intraday-equity and intraday-futures backtests enforce entry cutoff and forced square-off so MIS positions are not carried overnight
- Auto-adaptive backtests use a backtest-local regime cache; live runtime state is not mutated
- Intraday-options backtests use real option contract symbols, premium candles, and lot-sized quantities
- `tick_entry_enabled=True` in `BacktestConfig` models early intra-candle fills rather than always filling at candle close

---

## Running the project

```powershell
# Live or paper session
run_main.bat
# or
venv\Scripts\python main.py

# Interactive backtest
run_backtest.bat
# or
venv\Scripts\python backtesting.py

# Unit tests
venv\Scripts\python -m pytest
```

---

## Test Status

```
116 passed, 1 pre-existing failure
```

The 1 failing test (`IntradayOptionsEngineTests.test_get_cycle_state_before_open_waits`) is a pre-existing edge-case in `engines/intraday_options.py` boundary logic unrelated to entry, exit, or risk features.

---

## Recommended Reading Order for Developers

1. This file
2. [HOW_TO_USE.md](./HOW_TO_USE.md) — operator workflow
3. [config.runtime.yaml](./config.runtime.yaml) — active knobs
4. [orchestration/context.py](./orchestration/context.py) — dependency wiring
5. [orchestration/session.py](./orchestration/session.py) — main supervision loop
6. [tick_entry/manager.py](./tick_entry/manager.py) — tick entry system
7. The engine file for the engine you are working with
