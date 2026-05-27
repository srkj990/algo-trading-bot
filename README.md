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
- live entry orchestration and backtesting are expected to stay on that same shared target-resolution path
- engines that own adaptive position builders, such as `intraday_equity`, `delivery_equity`, and `intraday_options`, should use those builders in both live/paper and backtest paths

## Current Engine Coverage

| Engine | Product | Data cadence | Typical use | Notes |
| --- | --- | --- | --- | --- |
| `intraday_equity` | `MIS` | `1d` / `1m` live, `5m` backtest default | same-day stock trading | auto-adaptive strategy selection plus adaptive SL/target/trailing |
| `delivery_equity` | `CNC` | `6mo` / `1d` | swing and delivery positions | long-only entries plus swing-style adaptive SL/trailing |
| `futures_equity` | `NRML` | `3mo` / `5m` live, `15m` backtest default | positional index futures | lot-aware sizing |
| `options_equity` | `NRML` | `2mo` / `15m` | positional index options | lot-aware sizing |
| `intraday_futures` | `MIS` | `15d` / `3m` live, `5m` backtest default | intraday index futures | entry cutoff + forced square-off |
| `intraday_options` | `MIS` | `1d` / `1m` | intraday ATM option trading | dynamic ATM, Greeks filters, adaptive runner logic |

## Engine Features

### Intraday Equity

- Long and short intraday entries
- `MA`, `RSI`, `VWAP`, `BREAKOUT`, `ORB`
- `AUTO_ADAPTIVE` mode in live and backtest flows
- VWAP bias filter
- breakout volume filter
- reversal exits with confirmation candles
- late-day entry cutoff before square-off
- adaptive stop, target, trailing distance, and position sizing based on ATR, recent range, and signal score
- backtests enforce the same entry cutoff and intraday square-off behavior instead of carrying MIS positions overnight

#### Auto-Adaptive Intraday Equity

Auto-adaptive mode is a strategy-selection layer, not a separate technical indicator.

For each symbol and trade day, it:

- builds a market context from the current intraday session and daily history
- classifies the open as `GAP_UP`, `GAP_DOWN`, or `NO_GAP`
- classifies early behavior as `GAP_GO`, `GAP_FILL`, `SIDEWAYS`, or `PENDING_OPEN_RANGE`
- chooses the strategy basket and confirmation count from that context
- waits for the opening range before entries when the context is not ready
- caches the daily regime context so repeated scans do not recalculate it every cycle

Current routing:

- gap with continuation behavior: `ORB`, `VWAP`, `BREAKOUT`, usually with 2 confirmations
- gap with fill behavior: `ORB`, `VWAP`, usually with 2 confirmations
- gap but sideways behavior: `VWAP`, `RSI`, usually with 2 confirmations
- normal/no-gap behavior: `MA`, `RSI`, with the configured normal confirmation count

After a signal passes, the entry still goes through the same VWAP bias, breakout-volume, cost-aware, risk, capital, and one-trade-per-day gates as the other intraday-equity modes.

Adaptive levels then classify the trade as:

- `SIDEWAYS`: lower conviction, tighter target behavior, slower expansion assumptions
- `NORMAL`: balanced ATR/range behavior
- `EXPANSION`: stronger score or wider recent range, wider target and trailing behavior

The position builder stores `adaptive_levels_enabled`, `adaptive_regime`, adaptive target levels, ATR trailing distance, range volatility distance, stop distance, and trailing activation distance on the position.

### Delivery Equity

- Long-only entries
- `MA`, `RSI`, `BREAKOUT`
- Nifty trend guard for new long entries
- per-symbol allocation cap
- delivery holdings reconciliation in live mode
- time-based exit support across business days
- swing-style adaptive stop, target reference, trailing distance, and position sizing based on daily ATR, recent daily range, and signal score
- delivery target remains a reference level and does not force an exit; delivery exits still rely on stop/trailing/sell-signal/time-based logic

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

## Strategy Reference

Strategy candles use the engine's active data interval. For example, `intraday_equity` live uses `1m` candles, while its default backtest uses `5m` candles. Delivery equity uses `1d` candles.

### Legacy Equity/Futures Strategies

These strategies are shared by equity, futures, and positional option engines where supported.

| Strategy | Configured minimum | Practical first actionable | Core candle logic | Signal rule | Main tuning location |
| --- | ---: | ---: | --- | --- | --- |
| `MA` | 50 | 51 | rolling `MA20` and `MA50` on `Close` | `BUY` when `MA20 > MA50`, `SELL` when `MA20 < MA50` | `strategy.min_candles.MA` |
| `RSI` | 14 | 15 | 14-period RSI on `Close` | `BUY` below 30, `SELL` above 70 | `strategy.min_candles.RSI`, `indicators.compute_rsi()` |
| `BREAKOUT` | 20 | 22 | previous 20 completed candles, excluding latest | `BUY` above prior high, `SELL` below prior low | `strategy.min_candles.BREAKOUT`, `get_breakout_reference_levels()` |
| `VWAP` | 1 | 6 | cumulative VWAP from session/history frame | `BUY` above VWAP, `SELL` below VWAP | `strategy.min_candles.VWAP`, `indicators.compute_vwap()` |
| `ORB` | 20 | 21 | first 15 candles define opening high/low | `BUY` above opening range high, `SELL` below opening range low | `strategy.min_candles.ORB`, `engine_defaults.intraday_equity.opening_range_candles` |

The practical first actionable column includes the legacy confirmation wrapper in `strategy.confirm_signal()`, which requires the previous candle window and current candle window to agree. This is why the effective candle count can be higher than `strategy.min_candles`.

### Intraday-Equity Auto-Adaptive Inputs

| Item | Default / rule | Used for | Tuning location |
| --- | --- | --- | --- |
| Gap threshold | `1.0%` | classifies `GAP_UP`, `GAP_DOWN`, `NO_GAP` | `engine_defaults.intraday_equity.gap_threshold_percent` |
| Opening range | `15` candles | detects `GAP_GO`, `GAP_FILL`, `SIDEWAYS` | `engine_defaults.intraday_equity.opening_range_candles` |
| Breakout volume filter | same clock time across prior days; current volume >= average * `1.2` | validates `BREAKOUT` signals | `engine_defaults.intraday_equity.breakout_volume_multiplier` |
| Normal confirmations | `2` | confirmations for no-gap `MA` + `RSI` routing | `execution_safety.intraday_equity_auto_normal_min_confirmations` |
| Reversal confirmation | `2` opposite-signal candles | avoids single-candle reversal exits | `execution_safety.reversal_exit_confirmation_candles` |
| Entry cutoff | `30` minutes before 15:15 square-off | blocks fresh intraday-equity entries late in session | `execution_safety.intraday_equity_entry_cutoff_minutes_before_squareoff` |
| ATR period | `14` | scoring, sizing, adaptive levels | `indicators.compute_atr()` / `signal_scoring.get_atr_value()` |

### Intraday-Options Strategies

Intraday-options strategies emit `BUY_CE`, `BUY_PE`, or `NO_TRADE`; execution maps `BUY_CE` and `BUY_PE` to long option entries.

| Strategy | Configured minimum | Practical minimum | Core candle logic | Signal rule | Main tuning location |
| --- | ---: | ---: | --- | --- | --- |
| `ATM_MOMENTUM` | 20 | 20 | RSI14, VWAP, previous 5-candle breakout high/low | bullish above VWAP + RSI > 60 + breakout; bearish below VWAP + RSI < 40 + breakdown | `strategy.py`, `engine_defaults.intraday_options.momentum_*` |
| `ATM_ORB` | 16 | 16 | first 15 session candles | CE above opening range high, PE below opening range low | `strategy_orb(opening_range_minutes=15)` |
| `ATM_VWAP_REVERSION` | 20 | 20 | 6-candle prior VWAP deviation and latest re-entry | CE after negative deviation re-enters above VWAP; PE after positive deviation re-enters below VWAP | `strategy_vwap(deviation_threshold=0.0035, lookback=6)` |
| `ATM_MULTI` | 20 | 20 | combines momentum, ORB, VWAP reversion, ATR14 sideways check | uses aligned momentum+ORB; otherwise can use VWAP reversion in low ATR regime | `strategy_multi(sideways_atr_threshold=0.0035)` |
| `ATM_BREAKOUT_EXPANSION` | 45 | 45 | 45-candle compression, 30-candle breakout, 20-candle volume, ATR14 expansion | breakout plus compression plus volume spike plus ATR expansion | `strategy_breakout_expansion()` |
| `ATM_IV_EXPANSION` | 30 | 30 | 20-candle key level, 10-candle body average, RSI14 | breakout candle with body >= 1.8x average and RSI confirmation | `strategy_iv_expansion()` |
| `ATM_TRAP_REVERSAL` | 24 | 26 | 20-candle support/resistance, 3 trap candles, 10-candle body average | failed support/resistance break with reversal body >= 1.5x average | `strategy_trap_reversal()` |

### Indicators And Score Inputs

| Indicator / score | Default candles | Used by |
| --- | ---: | --- |
| RSI | 14 | `RSI`, `ATM_MOMENTUM`, `ATM_IV_EXPANSION` |
| ATR | 14 | ranking score, adaptive stops, trailing distance, `ATM_MULTI`, volatility regimes |
| VWAP | cumulative over provided frame/session | `VWAP`, VWAP bias gates, option filters, auto-adaptive context |
| Candidate score floor | `0.008` | `rank_candidates()` filters low-quality ranked candidates |

Strategy score is not a probability. It is a ranking/conviction number built from distance from trigger levels plus normalized ATR. Adaptive equity position builders also use it to classify `SIDEWAYS`, `NORMAL`, or `EXPANSION`.

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
- intraday-equity and delivery-equity backtests reuse their adaptive position builders when those builders are available
- intraday-equity backtests respect entry cutoff and forced square-off windows, so MIS positions are not carried overnight
- auto-adaptive backtests use a backtest-local regime cache and no-op persistence shim so live runtime state is not mutated
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

- focused backtest, signal-workflow, intraday-equity, and delivery-equity tests pass with `venv\Scripts\python -m pytest`
- latest full unit run observed `190` passing and `1` unrelated intraday-options boundary expectation still failing

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
