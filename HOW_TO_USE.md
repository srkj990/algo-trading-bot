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
- should not keep a separate stop/target/trailing path when the live engine already owns that behavior

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
- `AUTO_ADAPTIVE`

Strategy modes:

- `Single(1)`: run one chosen strategy, such as only `MA` or only `BREAKOUT`
- `Multi(2)`: run a chosen basket of strategies and require confirmation agreement
- `Auto Adaptive(3)`: let the engine choose the strategy basket from the current day context

Backtest notes:

- default backtest data is `5d` / `5m`
- breakout backtests pull extra history for volume confirmation
- backtest uses the live trade gate and shared target resolution
- auto-adaptive backtests use the same regime-selection flow as live/paper
- backtests now respect entry cutoff and forced square-off, so intraday positions should not carry overnight

When to use it:

- you want the simplest place to start
- you want a stock intraday engine with adaptive filtering

#### What Auto-Adaptive Intraday Equity Does

Auto-adaptive mode is best understood as a context-aware controller.

It does not replace `MA`, `RSI`, `VWAP`, `BREAKOUT`, or `ORB`. Instead, it chooses which of those strategies to trust for the current market context.

Flow:

1. Builds a daily market context for each symbol.
2. Compares today's open with the previous daily close.
3. Labels the day as `GAP_UP`, `GAP_DOWN`, or `NO_GAP`.
4. Reads the opening range, VWAP, latest close, and volume.
5. Labels opening behavior as `GAP_GO`, `GAP_FILL`, `SIDEWAYS`, or `PENDING_OPEN_RANGE`.
6. Selects a strategy basket and confirmation count.
7. Applies normal signal filters, including VWAP bias and breakout volume checks.
8. Builds the position with adaptive stop, target, trailing, and quantity sizing.

Current strategy routing:

| Context | Strategy basket | Confirmation behavior |
| --- | --- | --- |
| Gap plus continuation | `ORB`, `VWAP`, `BREAKOUT` | typically 2 confirmations |
| Gap plus fill behavior | `ORB`, `VWAP` | typically 2 confirmations |
| Gap but sideways | `VWAP`, `RSI` | typically 2 confirmations |
| No-gap / normal day | `MA`, `RSI` | configured normal confirmation count |
| Opening range not ready | waits; no fresh entry | entries blocked until enough candles exist |

Adaptive trade levels:

- `SIDEWAYS`: muted score and low ATR/range; more conservative target behavior
- `NORMAL`: balanced ATR/range behavior
- `EXPANSION`: stronger score or wider recent range; wider target/trailing behavior

The adaptive position builder stores:

- `adaptive_levels_enabled`
- `adaptive_regime`
- `adaptive_signal_score`
- `adaptive_level1_target`
- `adaptive_level2_target`
- `adaptive_level3_target`
- `stop_distance`
- `trailing_distance`
- `trailing_activation_distance`
- `range_volatility_distance`
- `atr_trailing_distance`

Quantity is sized from the adaptive stop distance, so a wider stop normally reduces quantity and a tighter stop can increase it within capital limits.

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
- delivery now has a separate swing-style adaptive position builder
- adaptive delivery levels use daily ATR, recent daily range, and signal score
- delivery adaptive trailing is deliberately wider/slower than intraday trailing to handle multi-day gap behavior

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
- intraday-equity backtests respect live-like entry cutoff and square-off windows
- auto-adaptive backtests keep a backtest-local regime cache and do not persist live runtime state
- F&O backtests can resolve underlyings, expiries, and strike modes interactively

## Strategy And Candle Reference

Use this section when you want to tune a strategy or understand why no trade was taken yet.

The candle counts below are counted in the engine's active interval:

- `intraday_equity`: live `1m`, default backtest `5m`
- `delivery_equity`: `1d`
- `futures_equity`: live `5m`, default backtest `15m`
- `options_equity`: `15m`
- `intraday_futures`: live `3m`, default backtest `5m`
- `intraday_options`: `1m`

### Shared Equity/Futures Strategy Candles

| Strategy | Configured minimum | First actionable in practice | What it reads | Buy / sell condition |
| --- | ---: | ---: | --- | --- |
| `MA` | 50 | 51 | `Close` rolling `MA20` and `MA50` | buy if `MA20 > MA50`; sell if `MA20 < MA50` |
| `RSI` | 14 | 15 | 14-candle RSI on `Close` | buy below 30; sell above 70 |
| `BREAKOUT` | 20 | 22 | previous 20 completed candles' high/low | buy above prior high; sell below prior low |
| `VWAP` | 1 | 6 | cumulative VWAP over provided frame | buy above VWAP; sell below VWAP |
| `ORB` | 20 | 21 | first 15 candles' high/low | buy above opening range high; sell below opening range low |

Why "first actionable" is higher:

- legacy strategies use `confirm_signal()`
- `confirm_signal()` evaluates both the previous candle window and current candle window
- trade direction must match in both windows

Where to tune:

- configured minimum candles: `strategy.min_candles` in [config.runtime.yaml](./config.runtime.yaml)
- RSI/ATR/VWAP calculation code: [indicators.py](./indicators.py)
- strategy rules: [strategy.py](./strategy.py)

### Intraday Equity Auto-Adaptive Details

Auto-adaptive intraday equity uses normal strategies, but chooses the basket automatically.

| Input | Default | Meaning | Update here |
| --- | ---: | --- | --- |
| Gap threshold | `1.0%` | classifies gap up/down vs no gap | `engine_defaults.intraday_equity.gap_threshold_percent` |
| Opening range | `15` candles | classifies open behavior and ORB range | `engine_defaults.intraday_equity.opening_range_candles` |
| Breakout volume multiplier | `1.2` | requires current volume >= matched prior average * multiplier | `engine_defaults.intraday_equity.breakout_volume_multiplier` |
| Normal confirmations | `2` | confirmations for normal no-gap routing | `execution_safety.intraday_equity_auto_normal_min_confirmations` |
| Reversal exit confirmations | `2` | opposite-signal candles before reversal exit | `execution_safety.reversal_exit_confirmation_candles` |
| Entry cutoff | `30` minutes before square-off | blocks new intraday-equity entries late day | `execution_safety.intraday_equity_entry_cutoff_minutes_before_squareoff` |

Auto-adaptive routing:

| Market context | Strategy basket | Confirmation count |
| --- | --- | ---: |
| `GAP_UP` / `GAP_DOWN` plus `GAP_GO` | `ORB`, `VWAP`, `BREAKOUT` | 2 |
| `GAP_UP` / `GAP_DOWN` plus `GAP_FILL` | `ORB`, `VWAP` | 2 |
| Gap but `SIDEWAYS` | `VWAP`, `RSI` | 2 |
| `NO_GAP` / normal | `MA`, `RSI` | configured normal confirmations |
| `PENDING_OPEN_RANGE` | waits; no fresh entry | n/a |

Adaptive position sizing:

- stop distance comes from ATR, recent candle range, and signal score
- quantity is calculated from risk percent and adaptive stop price
- wider stop means lower quantity; tighter stop can mean higher quantity within capital caps

### Delivery Equity Adaptive Details

Delivery equity uses daily candles and long-only entries.

| Item | Default / rule | Meaning |
| --- | --- | --- |
| Nifty trend guard | 50 daily closes | new long entries require Nifty close above 50DMA |
| Max hold | 5 business days | time-based delivery exit |
| Swing volatility distance | recent 3-10 daily ranges * `1.2` | delivery trailing distance input |
| Adaptive base ATR floor | max ATR14 or `0.6%` of entry | prevents unrealistically tight swing stops |
| Minimum stop distance | `1.8%` of entry | swing stop floor |
| Delivery target | reference only | target does not force delivery exit |

Update delivery defaults in:

- `engine_defaults.delivery_equity` in [config.runtime.yaml](./config.runtime.yaml)
- adaptive logic in [engines/delivery_equity.py](./engines/delivery_equity.py)

### Intraday Options Strategy Candles

| Strategy | Configured minimum | Practical minimum | What it reads | Entry signal |
| --- | ---: | ---: | --- | --- |
| `ATM_MOMENTUM` | 20 | 20 | RSI14, VWAP, previous 5-candle breakout | CE on bullish momentum; PE on bearish momentum |
| `ATM_ORB` | 16 | 16 | first 15 session candles | CE above ORB high; PE below ORB low |
| `ATM_VWAP_REVERSION` | 20 | 20 | 6-candle prior VWAP deviation | CE/PE when price re-enters VWAP |
| `ATM_MULTI` | 20 | 20 | momentum + ORB + VWAP reversion + ATR14 | aligned momentum/ORB or sideways VWAP reversion |
| `ATM_BREAKOUT_EXPANSION` | 45 | 45 | 45 compression, 30 breakout, 20 volume, ATR14 | breakout with compression, volume spike, ATR expansion |
| `ATM_IV_EXPANSION` | 30 | 30 | 20 key level, 10 body average, RSI14 | strong body breakout with RSI confirmation |
| `ATM_TRAP_REVERSAL` | 24 | 26 | 20 support/resistance, 3 trap candles, 10 body average | failed break and reversal body confirmation |

Intraday options quality filters also use:

- momentum quality lookback: `20`
- momentum fast EMA: `9`
- momentum confirmation timeout: `3` candles
- momentum pullback timeout: `5` candles
- mean-reversion quality lookback: `20`
- volatility quality lookback: `20`
- sideways regime lookback: `8` candles

Update these in:

- `engine_defaults.intraday_options` in [config.runtime.yaml](./config.runtime.yaml)
- `fno.intraday_options_*` in [config.runtime.yaml](./config.runtime.yaml)
- strategy rule functions in [strategy.py](./strategy.py)

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
- whether the engine has its own adaptive builder, such as intraday equity, delivery equity, or intraday options

Current code is set up so shared live logic is reused in backtesting wherever available.

### Auto-adaptive intraday equity crashes during backtest

This should now be fixed.

The backtest signal context now includes the runtime fields expected by the live scan workflow:

- `active_trade_day`
- `regime_cache`
- `session_runtime_state`
- `trade_counts_today`
- no-op `save_engine_state`

If it crashes again, check that your local changes still include the backtest context fields in [backtesting.py](./backtesting.py).

### Intraday backtest carries a position overnight

This should now be fixed for intraday equity.

Backtests now call the engine cycle state, block new entries during the entry cutoff, and force square-off in the square-off window.

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

- focused backtest and signal-workflow suites pass
- latest full unit run observed `190 passed` and `1` unrelated intraday-options boundary expectation still failing
