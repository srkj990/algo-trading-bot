# Trading Algo Bot - How To Use

Practical guide for running backtests, paper sessions, and live sessions with the current engine set.

## Start Here

Use these entry points:

```powershell
run_main.bat
run_backtest.bat
```

Or directly:

```powershell
venv\Scripts\python main.py
venv\Scripts\python backtesting.py
```

Run tests before making live changes:

```powershell
venv\Scripts\python -m pytest
```

## What The Two Main Flows Do

### `main.py`

Use this for:

- `PAPER` sessions
- `LIVE` sessions

This flow:

- asks for engine, mode, provider, capital, symbols/contracts, risk style, limits, and strategy
- restores persisted runtime state
- manages open positions
- scans for new entries when entries are allowed
- writes logs and trade store records

### `backtesting.py`

Use this for:

- historical simulation with the same engine family
- validating live-style target and trailing behavior before running paper or live

This flow:

- asks for engine, capital, symbols/contracts, risk style, selection mode, and strategy
- downloads or resolves historical data
- runs the same signal and target logic wherever live logic exists
- exports summary, trades, and equity curve under `Results/BackTest/`

## Three Operating Modes

| Mode | Where | Real orders? | Best use |
| --- | --- | --- | --- |
| Backtest | `backtesting.py` | No | historical validation |
| Paper | `main.py` | No | market-hours behavior check |
| Live | `main.py` | Yes | production trading |

Recommended progression:

1. Backtest the engine and strategy.
2. Run paper during live market hours.
3. Move to live only after logs and exits look correct.

## Current Session Defaults You Should Know

These now come from [config.runtime.yaml](./config.runtime.yaml):

- `session_defaults.exit_only_default`
- `session_defaults.live_broker_resync_interval_seconds`

That means the session can start in exit-only mode without prompting you.

Exit-only mode means:

- existing positions are managed
- no fresh entries are placed
- in live mode, broker positions are periodically resynced

## Provider Rules

### Equity engines

You can choose:

- `YFINANCE`
- `KITE`
- `UPSTOX`

### F&O engines

These are auto-forced to:

- data provider: `KITE`
- execution provider: `KITE`

## Engine Guide

### 1. Intraday Equity

Engine id: `1`  
Engine name: `intraday_equity`

What it does:

- trades stocks intraday using `MIS`
- supports long and short entries
- squares off before market close

Live strategies:

- `MA`
- `RSI`
- `VWAP`
- `BREAKOUT`
- `ORB`
- `AUTO ADAPTIVE`

Backtest notes:

- default backtest data is `5d` / `5m`
- breakout backtests pull extra history for volume confirmation
- backtest uses the live trade gate and shared target resolution

When to use it:

- you want the simplest place to start
- you want a stock intraday engine with adaptive filtering

### 2. Delivery Equity

Engine id: `2`  
Engine name: `delivery_equity`

What it does:

- trades long-only CNC delivery positions
- uses daily data
- applies a Nifty trend guard to new long entries

Strategies:

- `MA`
- `RSI`
- `BREAKOUT`

Behavior notes:

- target is not used as a forced delivery exit
- per-symbol allocation cap is configurable during setup
- live reconciliation can restore broker holdings

### 3. Futures Equity

Engine id: `3`  
Engine name: `futures_equity`

What it does:

- trades positional index futures using `NRML`
- uses lot-aware sizing and broker reconciliation

Strategies:

- `MA`
- `RSI`
- `BREAKOUT`
- `VWAP`
- `ORB`

Behavior notes:

- contract selection is driven through the F&O prompt flow
- live provider is Kite only

### 4. Options Equity

Engine id: `4`  
Engine name: `options_equity`

What it does:

- trades positional index options using `NRML`
- uses lot-aware sizing and broker reconciliation

Strategies:

- `MA`
- `RSI`
- `BREAKOUT`
- `VWAP`
- `ORB`

Behavior notes:

- contract resolution happens during the F&O prompt flow
- intended for positional options, not the intraday ATM option runner logic

### 5. Intraday Futures

Engine id: `5`  
Engine name: `intraday_futures`

What it does:

- trades intraday index futures on `MIS`
- enforces entry cutoff and forced square-off

Strategies:

- `MA`
- `RSI`
- `BREAKOUT`
- `VWAP`
- `ORB`

Behavior notes:

- default live data cadence is `15d` / `3m`
- backtest default is `5d` / `5m`
- quantity is rounded to lot size

### 6. Intraday Options

Engine id: `6`  
Engine name: `intraday_options`

What it does:

- trades intraday ATM-style option flows on `MIS`
- can manage dynamic ATM single-option entries
- can run bounded two-leg range-pair entries in live/paper flow

Strategies:

- `ATM_MOMENTUM`
- `ATM_ORB`
- `ATM_VWAP_REVERSION`
- `ATM_MULTI`
- `ATM_BREAKOUT_EXPANSION`
- `ATM_IV_EXPANSION`
- `ATM_TRAP_REVERSAL`

Important current behavior:

- live and backtest share the same target/trailing resolution where applicable
- staged momentum mode mirrors the live-style confirmation and pullback sequence
- option entries can be filtered by expected cost ratio, spread, open interest, delta, IV percentile, and regime
- runner logic is trend-adaptive and can trail differently based on premium volatility
- two-lot runner positions now protect rather than forcing a scale-out in supervision

Config-driven defaults:

- `fno.intraday_options_lot_mode`
- `fno.intraday_options_entry_mode`

Possible lot modes:

- `ONE_LOT`
- `CAPITAL_BASED`

Possible entry modes:

- `LIVE_STAGED`
- `LEGACY_IMMEDIATE`

## What The Live Setup Prompts Ask

The `main.py` flow currently asks for:

1. engine
2. execution mode: `PAPER` or `LIVE`
3. provider choices for equity engines, or auto-forces Kite for F&O
4. capital
5. symbols or F&O contract selection
6. risk style
7. portfolio and deployment limits
8. one-trade-per-symbol-per-day
9. entry selection: `TOP 1` or `TOP N`
10. strategy mode and strategy choice

Special-case behavior:

- single-structure F&O sessions can auto-force `max_open_positions = 1`
- single-structure F&O sessions can auto-force `TOP 1`
- intraday-options live sessions read lot mode and entry mode from config

## What The Backtest Prompts Ask

The `backtesting.py` flow currently asks for:

1. engine
2. capital
3. symbol or F&O contract setup
4. risk style
5. max positions
6. capital limits
7. one-trade-per-symbol-per-day
8. entry selection mode
9. strategy mode and strategy
10. period and interval

Special-case behavior:

- intraday-options backtests support ATM single-option flow
- intraday-equity backtests support `AUTO_ADAPTIVE`
- F&O backtests can resolve underlyings, expiries, and strike modes interactively

## Risk Styles

Risk styles are engine-aware now.

Intraday engines use one preset bucket.  
Positional engines use another.

Each risk style controls:

- ATR stop multiplier
- ATR trailing multiplier
- target risk-reward
- fallback percent stop/target/trailing values
- capital risk percent

Options:

- `CONSERVATIVE`
- `BALANCED`
- `AGGRESSIVE`

## How Results Are Stored

### Backtests

Files go to `Results/BackTest/`:

- summary text
- trades CSV
- equity CSV

### Paper and Live

Files and state go to:

- `logs/` for session logs
- `state/` for runtime state
- `state/trade_store/` for trade and order-audit records

## Recommended Usage Patterns

### Safest starting point

Use:

- engine: `intraday_equity`
- mode: `PAPER`
- provider: `YFINANCE`
- risk style: `BALANCED`
- entry selection: `TOP 1`

### Swing stock workflow

Use:

- engine: `delivery_equity`
- mode: `PAPER` first, then `LIVE`
- smaller max symbol allocation

### Intraday options workflow

Use:

1. backtest with `intraday_options`
2. verify staged momentum and exits in paper
3. check `config.runtime.yaml` for lot mode and entry mode
4. go live only after logs match expected contract, stop, target, and runner behavior

## High-Value Config Knobs

Review these before live trading:

- `session_defaults.exit_only_default`
- `session_defaults.live_broker_resync_interval_seconds`
- `execution_safety.min_ranked_candidate_score`
- `execution_safety.reversal_exit_confirmation_candles`
- `execution_safety.intraday_equity_entry_cutoff_minutes_before_squareoff`
- `orders.default_entry_order_type`
- `orders.max_live_order_notional`
- `orders.margin_check_enabled`
- `fno.intraday_options_max_entry_cost_ratio`
- `fno.intraday_options_max_spread_pct`
- `fno.intraday_options_min_open_interest`
- `fno.intraday_options_roll_trigger_pct`
- `fno.intraday_options_theta_exit_ratio`

## Troubleshooting

### Session starts in exit-only mode

Check:

- `session_defaults.exit_only_default`

### F&O provider choice is ignored

That is expected. F&O sessions currently force:

- data provider `KITE`
- execution provider `KITE`

### Backtest and live target behavior look different

Check:

- the engine path you are using
- the selected risk style
- `resolve_trade_targets(...)` in [engines/common.py](./engines/common.py)

Current code is set up so shared live logic is reused in backtesting wherever available.

### Intraday options entered too aggressively

Review:

- `fno.intraday_options_entry_mode`
- `fno.intraday_options_max_entry_cost_ratio`
- `fno.intraday_options_max_spread_pct`
- `fno.intraday_options_min_open_interest`

### Tests before live release

Run:

```powershell
venv\Scripts\python -m pytest
```

Current expected result:

- `187 passed`
