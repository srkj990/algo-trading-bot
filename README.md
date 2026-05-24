# Trading Algo Bot

Interactive trading and backtesting bot for Indian markets with six engines:

- `intraday_equity`
- `delivery_equity`
- `futures_equity`
- `options_equity`
- `intraday_futures`
- `intraday_options`

If you want the operator workflow instead of the project overview, read [HOW_TO_USE.md](./HOW_TO_USE.md).

## What This Repo Does

- Runs paper or live sessions from [main.py](./main.py)
- Runs interactive backtests from [backtesting.py](./backtesting.py)
- Persists runtime state under `state/`
- Writes session logs under `logs/`
- Exports backtest results under `Results/BackTest/`
- Supports broker execution through Kite and selected equity flows through Upstox

The important design rule in the current codebase is:

- backtesting should use the same entry/exit target logic as live trading wherever that logic exists in the live path
- shared stop, target, trailing-distance, and activation-distance resolution now flows through `resolve_trade_targets(...)` in [engines/common.py](./engines/common.py)

## Current Engine Coverage

| Engine | Product | Data cadence | Typical use | Notes |
| --- | --- | --- | --- | --- |
| `intraday_equity` | `MIS` | `1d` / `1m` live, `5m` backtest default | same-day stock trading | supports auto-adaptive mode |
| `delivery_equity` | `CNC` | `6mo` / `1d` | swing and delivery positions | long-only entry flow |
| `futures_equity` | `NRML` | `3mo` / `5m` live, `15m` backtest default | positional index futures | lot-aware sizing |
| `options_equity` | `NRML` | `2mo` / `15m` | positional index options | lot-aware sizing |
| `intraday_futures` | `MIS` | `15d` / `3m` live, `5m` backtest default | intraday index futures | entry cutoff + forced square-off |
| `intraday_options` | `MIS` | `1d` / `1m` | intraday ATM option trading | dynamic ATM, Greeks filters, adaptive runner logic |

## Engine Features

### Intraday Equity

- Long and short intraday entries
- `MA`, `RSI`, `VWAP`, `BREAKOUT`, `ORB`
- `AUTO ADAPTIVE` mode in live sessions
- VWAP bias filter
- breakout volume filter
- reversal exits with confirmation candles
- late-day entry cutoff before square-off

### Delivery Equity

- Long-only entries
- `MA`, `RSI`, `BREAKOUT`
- Nifty trend guard for new long entries
- per-symbol allocation cap
- delivery holdings reconciliation in live mode
- time-based exit support across business days

### Futures Equity

- Positional index futures
- `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- lot-size-aware sizing
- startup reconciliation from broker positions

### Options Equity

- Positional index options
- `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- lot-size-aware sizing
- startup reconciliation from broker positions

### Intraday Futures

- Intraday index futures on `MIS`
- entry cutoff and forced square-off window
- lot-size-aware sizing and startup sync

### Intraday Options

- Dynamic ATM single-option flow driven by underlying signal
- optional two-leg bounded range pair flow in live/paper sessions
- strategies:
  - `ATM_MOMENTUM`
  - `ATM_ORB`
  - `ATM_VWAP_REVERSION`
  - `ATM_MULTI`
  - `ATM_BREAKOUT_EXPANSION`
  - `ATM_IV_EXPANSION`
  - `ATM_TRAP_REVERSAL`
- Greeks, IV, VWAP, delta, spread, open-interest, expiry, and cost filters
- staged momentum entry mode for live-style breakout confirmation
- legacy immediate mode for raw breakout-style entry
- dynamic ATM strike rolling for open long option flows
- theta-aware exits
- trend-adaptive runner handling with premium-volatility-aware trailing distance
- two-lot runner protection behavior and larger multi-lot partial exits

## Shared Trading Features

- cost-aware trade filtering before entry
- risk-style presets: `CONSERVATIVE`, `BALANCED`, `AGGRESSIVE`
- separate intraday vs positional risk presets
- max open positions
- max capital per trade
- max capital deployed
- one-trade-per-symbol-per-day control
- paper and live trade persistence
- order audit and trade store under `state/trade_store/`
- provider caching plus per-cycle caching
- runtime safety checks for live orders

## Runtime Configuration

Runtime defaults are defined in [config.py](./config.py) and can be overridden with [config.runtime.yaml](./config.runtime.yaml).

Important config sections:

- `execution_safety`
- `transaction_costs`
- `data_cache`
- `session_defaults`
- `risk_controls`
- `orders`
- `trade_store`
- `fno`
- `engine_defaults`
- `backtest_defaults`

Useful current knobs:

- `session_defaults.exit_only_default`
- `session_defaults.live_broker_resync_interval_seconds`
- `orders.default_entry_order_type`
- `orders.entry_limit_price_buffer_pct`
- `fno.intraday_options_lot_mode`
- `fno.intraday_options_entry_mode`
- `fno.intraday_options_max_entry_cost_ratio`
- `fno.intraday_options_roll_trigger_pct`

## Live vs Backtest Parity

Recent behavior to be aware of:

- live and backtest entry target resolution share the same `resolve_trade_targets(...)` helper
- backtesting no longer keeps a separate fallback stop/target/trailing calculation for engines that already have live logic
- backtest trailing updates now read stored `trailing_distance` instead of using hardcoded zero
- intraday-options backtests reuse live-style momentum, runner, and trend-adaptive position behavior

That means:

- if you tune risk style, trailing multiplier, or cost-aware target behavior, both live and backtest flows should move together
- if an engine owns custom position behavior in the live engine class, backtesting should prefer that behavior instead of inventing a parallel version

## Repo Layout

| Path | Purpose |
| --- | --- |
| [main.py](./main.py) | starts live or paper session |
| [backtesting.py](./backtesting.py) | interactive backtest runner |
| [cli/](./cli) | prompt flow and session configuration |
| [engines/](./engines) | engine-specific trading behavior |
| [orchestration/](./orchestration) | scan loop, session control, position management |
| [executor.py](./executor.py) | order execution, safety, broker integration |
| [config.py](./config.py) | runtime config model and defaults |
| [config.runtime.yaml](./config.runtime.yaml) | local runtime overrides |
| [tests/unit/](./tests/unit) | unit tests |

## Running The Project

Windows helpers:

- `run_main.bat`
- `run_backtest.bat`

Direct commands:

```powershell
venv\Scripts\python main.py
venv\Scripts\python backtesting.py
venv\Scripts\python -m pytest
```

## Testing Status

Current verified unit-test status:

- `187` tests passing via `venv\Scripts\python -m pytest`

## Notes For Operators

- F&O engines force `KITE` as both data and execution provider
- live session behavior now reads `EXIT ONLY` and broker resync defaults from config instead of prompting each time
- intraday-options live sessions can default to `CAPITAL_BASED` or `ONE_LOT` sizing from config
- intraday-options live sessions can default to staged or legacy momentum entry mode from config

## Recommended Reading Order

1. [HOW_TO_USE.md](./HOW_TO_USE.md)
2. [config.runtime.yaml](./config.runtime.yaml)
3. [cli/configuration.py](./cli/configuration.py)
4. [orchestration/session.py](./orchestration/session.py)
5. the engine file you plan to trade
