# Trading Algo Bot

This repository is an interactive algo-trading bot for Indian markets with support for:

- intraday equity
- delivery equity
- positional index futures
- positional index options
- intraday index futures
- intraday index options

If you want an operator guide instead of an architecture walkthrough, read [HOW_TO_USE.md](./HOW_TO_USE.md).

The bot runs from `main.py`, persists runtime state under `state/`, writes session logs under `logs/`, and can place real orders through supported brokers when `LIVE` mode is enabled.

Recent UX/runtime improvements now also include:

- broker API IPv4 pinning support for static-IP-sensitive Upstox order flow
- `[HELP]` guidance lines before interactive CLI inputs in both `main.py` and `backtesting.py`
- engine-aware setup prompts that skip irrelevant questions where the choice is fixed
- interactive backtesting exports under `Results/BackTest/`
- asset-class aware stop-loss, target, and trailing-stop presets keyed off the selected engine and risk style
- cost-aware pre-trade filtering that can skip setups whose net edge is too small after estimated charges
- widened `INTRADAY_OPTIONS` presets so option stops, trails, and targets better reflect premium volatility and multi-stage exits

Recent architecture improvements now also include:

- structured runtime config sections with validation plus optional `config.runtime.yaml` overrides
- typed `Position` foundation with validation, exit evaluation, and trailing-stop logic
- `TradingEngine` and `BrokerClient` abstract base layers
- broker client factory for `KITE` and `UPSTOX`
- extracted `cli/` helpers for reusable interactive prompts
- extracted `orchestration/` helpers for position lifecycle management
- extracted `cli/configuration.py` for runtime setup/configuration flow
- extracted `orchestration/session.py` and `orchestration/signal_workflow.py` for runtime supervision and scan logic
- provider-based `data_providers/` plugins behind a shared market-data service
- dependency-wired trading context for engine, broker, data, logger, and persisted runtime state
- market-data caching at both the shared provider layer and the per-cycle trading-context layer
- structured trade and order-audit persistence under `state/trade_store/`
- live-order pre-flight validation plus broker order-status reconciliation
- limit-order support, broker fill-confirmation polling, rejection retries, partial-fill retries, margin checks, slippage audits, and spread-aware entry checks in the execution layer
- synthetic bracket-order entry support for strategies that want to stage broker-managed exits later
- quality-tooling config for `mypy`, `ruff`, and `pre-commit`
- lazy engine package loading so foundational tests do not require broker SDK imports

## Current Engine Coverage

### 1. Intraday Equity
- Engine: `intraday_equity`
- Product: `MIS`
- Data: `1d` / `1m`
- Features:
  - adaptive intraday regime selection
  - VWAP bias filter
  - breakout volume confirmation
  - intraday auto square-off

### 2. Delivery Equity
- Engine: `delivery_equity`
- Product: `CNC`
- Data: daily
- Features:
  - long-only delivery positions
  - per-symbol allocation limit
  - broker holding reconciliation

### 3. Positional Futures
- Engine: `futures_equity`
- Product: `NRML`
- Universe: NIFTY 50 and SENSEX index futures via Kite
- Features:
  - F&O contract resolution by expiry
  - lot-size aware quantity rounding
  - broker F&O position reconciliation

### 4. Positional Options
- Engine: `options_equity`
- Product: `NRML`
- Universe: NIFTY 50 and SENSEX index options via Kite
- Features:
  - option contract resolution by expiry, type, and strike
  - lot-size aware quantity rounding
  - broker F&O position reconciliation

### 5. Intraday Futures
- Engine: `intraday_futures`
- Product: `MIS`
- Universe: NIFTY 50 and SENSEX index futures via Kite
- Features:
  - intraday entry cutoff
  - forced MIS square-off window
  - lot-size aware sizing and startup sync for MIS futures only

### 6. Intraday Options
- Engine: `intraday_options`
- Product: `MIS`
- Universe: NIFTY 50 and SENSEX index options via Kite
- Features:
  - dynamic ATM single-option scalping flow driven by underlying price action
  - selectable strike mode:
    - `ATM`
    - `ATM + 1 STRIKE`
    - `ATM - 1 STRIKE`
  - intraday entry cutoff
  - forced MIS square-off window
  - faster supervision loop with closed-candle confirmation for safer entries
  - Greeks and IV snapshot on every scan
  - premium, delta, IV-percentile, and VWAP-band filters before entry
  - vega-crush blocker based on 15-minute IV change
  - expiry warning when time-to-expiry is very low
  - configurable per-underlying daily trade cap
  - selectable `ATM SINGLE OPTION` or `TWO-LEG RANGE PAIR` entry flow
  - bounded-market two-leg short pair with linked exits on range break or leg stop
  - dynamic ATM strike rolling when the underlying moves beyond the configured threshold
  - theta-aware explicit exit guard for long single-leg intraday options
  - lot-size aware sizing and startup sync for MIS options only

## Trading Features

- ATR-based stop, trailing stop, and target placement
- risk-style presets: `CONSERVATIVE`, `BALANCED`, `AGGRESSIVE`
- single-strategy, multi-strategy, and adaptive intraday mode
- candidate ranking by signal agreement, score, and ATR
- configurable max open positions and deployment caps
- one-trade-per-symbol-per-day control
- persisted positions, traded symbols, trade-day tracking, and regime cache
- persistent trade-book and order-audit JSONL records under `state/trade_store/`

## Runtime Config

Runtime defaults now live behind a structured `RuntimeConfig` in [config.py](./config.py). Existing constant aliases still work, but the main source of truth is now grouped into validated sections such as:

- `strategy`
- `execution_safety`
- `transaction_costs`
- `data_cache`
- `orders`
- `trade_store`
- `logging`
- `universe`
- `fno`

Optional file-based overrides can be supplied with [config.runtime.yaml.example](./config.runtime.yaml.example) copied to `config.runtime.yaml`.

Current behavior:

- startup validates the runtime config before the session begins
- invalid values such as negative cache TTLs or zero min quantities fail fast
- YAML overrides are merged on top of the built-in defaults
- the old constant imports remain available for compatibility while newer code reads structured sections

## Caching And Audit Safety

The runtime now includes a couple of operational safety upgrades that matter during live or frequent intraday scans:

- repeated candle requests are cached inside `MarketDataService` for a short TTL
- the trading context also keeps a per-cycle cache so repeated fetches for the same symbol and timeframe are reused inside one scan loop
- live orders run through a pre-flight validation step before broker submission
- every order can emit structured audit records for pre-flight, submission, paper skip, and reconciliation stages
- closed trades are persisted as structured records instead of existing only in in-memory session summaries

### Intraday Equity Default Safety Bias

The intraday equity flow now defaults to a more selective profile so weak `1m` noise does not turn into frequent low-edge trades.

Current default behavior:

- `TOP 1` is the default entry-selection mode
- one-trade-per-symbol-per-day remains default `YES`
- ranked candidates must clear a minimum score threshold before they are even eligible
- auto-adaptive normal-session intraday equity now requires both `MA` and `RSI` to agree
- reversal exits now require confirmation instead of closing on the first opposite candle
- trailing stop logic activates later so trades get some room to develop
- fresh intraday equity entries stop earlier before the MIS square-off window
- intraday equity entries can be rejected when estimated edge is too small relative to estimated costs

These defaults are meant to reduce overtrading, cut down on whipsaw exits, and make paper results more realistic relative to live trading costs.

## Strategies

The active signal framework currently uses:

- `MA`
- `RSI`
- `BREAKOUT`
- `VWAP`
- `ORB`

For intraday options, the signal layer now supports:

- `ATM_MOMENTUM`
- `ATM_ORB`
- `ATM_VWAP_REVERSION`
- `ATM_MULTI`
- `ATM_BREAKOUT_EXPANSION`
- `ATM_IV_EXPANSION`
- `ATM_TRAP_REVERSAL`

These use the underlying for signal generation, then dynamically resolve the live ATM `CE` or `PE` contract before entry. Option analytics filters are still applied before the trade is allowed through.

## Greeks and IV Support

`option_analytics.py` now provides reusable Black-Scholes utilities for:

- theoretical option price
- implied volatility
- delta
- gamma
- theta
- vega
- rho
- time-to-expiry conversion

`fno_data_fetcher.py` now exposes:

- contract metadata lookup
- contract underlying inference
- lot size lookup
- latest contract price lookup
- option Greeks/IV snapshot generation
- 15-minute IV change estimate for vega-crush checks
- option intraday VWAP snapshot support
- approximate IV rank / IV percentile from recent option history

Notes:

- Greeks/IV support currently applies to option contracts only.
- IV rank / percentile is an approximation built from recent premium history and current underlying price.
- F&O market data and execution require `KITE` in this repo.

## Providers

### Data providers

- `YFINANCE`
- `KITE`
- `UPSTOX`

### Execution providers

- `KITE`
- `UPSTOX`

Important:

- equity flows can use `YFINANCE`, `KITE`, or `UPSTOX`
- F&O flows currently force `KITE` for both data and execution
- Upstox F&O execution/data is not implemented yet

### Broker Network Mode

The runtime now supports a broker network mode to keep broker API calls on IPv4 when needed.

Environment settings:

```env
UPSTOX_STATIC_IP=49.205.247.48
BROKER_IP_MODE=IPV4_ONLY
```

Current behavior:

- when `BROKER_IP_MODE=IPV4_ONLY`, broker API requests prefer IPv4 and avoid temporary IPv6 routes
- this is especially useful for Upstox apps/accounts with static-IP restrictions on order APIs
- startup now prints a small network banner showing the active broker IP mode and configured Upstox static IP
- live Upstox order diagnostics now print:
  - detected broker outbound IPv4
  - general laptop IPv6 when present
  - configured static IP

## Runtime Flow

Run:

```powershell
python main.py
```

The bot will prompt for:

1. engine
2. execution mode
3. provider selection if relevant for that engine
4. capital
5. symbol or F&O contract selection
6. risk style
7. open-position and capital limits
8. entry selection mode if relevant
9. strategy mode or strategy selection

The CLI now also prints `[HELP]` lines before important prompts, including a short explanation and an example input.

Prompt behavior is now engine-aware:

- F&O engines skip provider questions that would be auto-overridden to `KITE`
- single-structure modes auto-set `Max open positions=1`
- single-structure modes auto-set `TOP 1` entry selection
- intraday options asks its own strategy prompt directly instead of the generic MA/RSI/BREAKOUT strategy-mode block

Under the hood, the runtime now has a cleaner split:

- `main.py` is now a thin launcher
- `cli/interactive_input.py` owns reusable prompt helpers
- `cli/configuration.py` owns the interactive runtime setup/configuration flow
- `orchestration/context.py` wires engine, data, execution, logging, and persisted state into a trading context
- `orchestration/signal_workflow.py` owns per-symbol scan and signal-evaluation flow
- `orchestration/session.py` owns the runtime supervision loop, entry orchestration, and session shutdown handling
- `orchestration/positions.py` owns reusable trade/position lifecycle helpers
- `executor.py` routes broker calls through concrete broker clients created by a small factory
- `data_fetcher.py` is now a compatibility facade over the provider-backed market-data service

### F&O Contract UX

For futures:

- choose NIFTY, SENSEX, or both
- choose expiry

For options:

- choose underlying
- choose structure:
  - `ATM SINGLE OPTION`
  - `TWO-LEG RANGE PAIR`

For ATM single-option flow:

- choose expiry
- choose strike mode:
  - `ATM`
  - `ATM + 1 STRIKE`
  - `ATM - 1 STRIKE`
- the bot scans the underlying spot symbol, not a fixed option contract
- choose one intraday options strategy:
  - `Momentum`
  - `ORB`
  - `VWAP Reversion`
  - `Multi-strategy`
  - `Breakout Expansion`
  - `IV Expansion`
  - `Trap Reversal`
- when a valid signal appears, the bot resolves the chosen strike mode automatically
- `BUY_CE` means it buys the selected call strike
- `BUY_PE` means it buys the selected put strike
- single-leg ATM entries currently use:
  - `10%` stop-loss
  - `20%` target
  - `7.5%` trailing stop behavior

This flow is designed for fast intraday scalps where you want the system to choose the live ATM contract instead of manually locking one strike before the session starts.

### Strike Mode Explanation

The strike-mode choice is numerical around the live ATM strike.

Example if NIFTY spot is around `24400` and strikes are `24350`, `24400`, `24450`:

- `ATM`
  - bullish signal -> buy `24400 CE`
  - bearish signal -> buy `24400 PE`

- `ATM + 1 STRIKE`
  - bullish signal -> buy `24450 CE`
  - bearish signal -> buy `24450 PE`

- `ATM - 1 STRIKE`
  - bullish signal -> buy `24350 CE`
  - bearish signal -> buy `24350 PE`

How to think about it:

- use `ATM` when you want the cleanest and most neutral default
- use `ATM + 1 STRIKE` when you want one strike above the live ATM level
- use `ATM - 1 STRIKE` when you want one strike below the live ATM level

Because this is numerical:

- for calls, `ATM + 1 STRIKE` is usually a slightly higher strike call
- for puts, `ATM + 1 STRIKE` is also a slightly higher strike put

So choose based on strike placement, not just on whether you are bullish or bearish.

For two-leg range pair flow:

- choose expiry
- choose lower `PE` strike
- choose upper `CE` strike
- review the resolved contracts, lot size, premium, Greeks, and range width
- the bot treats this as a bounded-range short pair when the underlying stays inside the selected strike band

The CLI now also logs the resolved lot size for every selected F&O contract.

## How To Use Intraday ATM Options

Recommended use pattern:

1. Start `main.py`
2. Choose `KITE` data and execution for F&O
3. Choose engine `INTRADAY OPTIONS`
4. Select `ATM SINGLE OPTION`
5. Choose underlying, expiry, and strike mode
6. Choose one of the available intraday ATM strategies
7. Let the bot monitor the underlying and resolve the ATM option only when the setup is valid

This is best when:

- you want short intraday option trades instead of holding contracts all day
- you do not want to manually pick CE/PE/strike before the move starts
- you want one consistent workflow for trend, breakout, and mean-reversion sessions

### Market Flow Guide

Use these strategies based on what the market is doing, not just personal preference.

#### 1. ATM Momentum Scalping

What it looks for:

- price above VWAP
- RSI above `60`
- breakout above the recent high window
- result: `BUY_CE`

Or:

- price below VWAP
- RSI below `40`
- breakdown below the recent low window
- result: `BUY_PE`

Best market flow:

- directional trending sessions
- clean post-open continuation
- strong one-sided moves after consolidation
- index staying on one side of VWAP

Use it when:

- the market is clearly pushing higher or lower
- candles are expanding in the breakout direction
- you want to catch continuation, not reversal

Avoid it when:

- the market is choppy around VWAP
- repeated false breakouts are happening
- the first move has already exhausted and price is stretching too far from VWAP

#### 2. ATM ORB

What it looks for:

- break above the first 15-minute range high -> `BUY_CE`
- break below the first 15-minute range low -> `BUY_PE`

Best market flow:

- strong opening drive days
- gap-and-go sessions
- early trend days where the market shows clear intent in the first 15 minutes

Use it when:

- the first 15-minute range is respected
- the breakout is clean and decisive
- the market is transitioning from opening balance to trend

Avoid it when:

- the open is noisy and both sides of the opening range are getting tested
- the market starts rotating sideways after 9:30
- there is no conviction after the opening range is formed

#### 3. ATM VWAP Reversion

What it looks for:

- price stretches away from VWAP by more than the configured deviation
- then re-enters toward VWAP
- bullish re-entry -> `BUY_CE`
- bearish re-entry -> `BUY_PE`

Best market flow:

- sideways or rotational sessions
- failed directional pushes
- mean-reversion days where the market keeps snapping back toward fair value

Use it when:

- the market is not sustaining breakouts
- price is overextended and then starts reverting
- ATR is relatively low and the session feels balanced rather than trending

Avoid it when:

- the market is in a clean trend
- price keeps walking away from VWAP without reversion
- macro/news momentum is driving a directional move

#### 4. ATM Multi-Strategy

What it does:

- prefers Momentum + ORB alignment for stronger trend signals
- if the market is sideways by ATR logic, it allows VWAP Reversion to lead
- if signals conflict, it returns `NO_TRADE`

Best market flow:

- uncertain sessions where you want the bot to self-filter harder
- mixed conditions across the day
- traders who prefer fewer but cleaner entries

Use it when:

- you want confirmation before taking premium risk
- you are okay missing some trades to avoid conflicting setups
- you want one default mode for unknown market conditions

Avoid it when:

- you want maximum trade frequency
- you already know the session is strongly trending and want direct Momentum or ORB behavior

#### 5. ATM Breakout Expansion

What it looks for:

- a compressed range over the recent session window
- break above recent range high -> `BUY_CE`
- break below recent range low -> `BUY_PE`
- a volume spike on the underlying
- ATR expanding versus the recent baseline

Best market flow:

- post-compression expansion days
- late-morning or afternoon breakout sessions
- volatility expansion after a quiet opening phase

#### 6. ATM IV Expansion

What it looks for:

- a momentum candle at a key level on the underlying
- breakout above resistance -> `BUY_CE`
- breakdown below support -> `BUY_PE`
- low IV percentile on the option contract, so the bot is entering before a potential IV expansion

Best market flow:

- compressed premium conditions before a directional move
- setups where price is coiling near an important breakout level
- sessions where directional price expansion may also reprice implied volatility

#### 7. ATM Trap Reversal

What it looks for:

- failed support break and recovery -> `BUY_CE`
- failed resistance break and rejection -> `BUY_PE`
- strong reversal candle after the failed move

Best market flow:

- false-break sessions
- stop-hunt style moves that quickly reverse
- choppy mornings that transition into cleaner reversal legs

### Practical Strategy Selection Cheat Sheet

- Use `ATM_MOMENTUM` when the market is trending and staying away from VWAP.
- Use `ATM_ORB` when the edge is mostly in the first breakout after market open.
- Use `ATM_VWAP_REVERSION` when the market is balanced, choppy, and repeatedly reverting.
- Use `ATM_MULTI` when you want the safest default because it blocks conflicting signals.
- Use `ATM_BREAKOUT_EXPANSION` when the market compresses first and then expands with volume and ATR support.
- Use `ATM_IV_EXPANSION` when you want low-IV directional entries near key breakout levels.
- Use `ATM_TRAP_REVERSAL` when false breaks and fast recoveries are the main edge.

### How The ATM Flow Actually Trades

- The signal is generated from the underlying, not from a fixed option chart you selected manually.
- Once the strategy says `BUY_CE` or `BUY_PE`, the bot resolves the selected strike mode for the chosen expiry:
  - nearest ATM
  - one strike above ATM
  - one strike below ATM
- It then fetches the tradable option contract, applies options filters, sizes the trade, and places the order through the existing F&O executor path.
- Open positions are still managed with your normal runtime loop, stop, target, trailing logic, and square-off window.

This matters because the ATM contract can change as the underlying moves. The system is built to choose the current ATM contract at entry time instead of forcing you to guess the right strike before the move begins.

### Scan Speed And User Safety

Intraday options still use `1m` candles for signal generation in the current codebase.

What changed to make it safer for the user:

- the runtime supervision loop now wakes up every `15` seconds instead of once every `60` seconds
- entries are evaluated from the last fully closed `1m` candle, not from a still-forming candle
- this helps avoid false entries caused by intraminute spikes that vanish before the candle closes
- open-position supervision happens more frequently, so the engine is not sleeping for a full minute between checks

What this does not mean:

- this is still not tick-level execution logic
- a very fast spike can still move option premium aggressively inside one minute

What it does mean:

- the engine is safer than before because it does not enter on incomplete candle noise
- the console and log file show much clearer order banners for entries and exits

### Intraday Options Capability Matrix

Current intraday options engine capabilities:

- dynamic ATM contract resolution from the underlying at entry time
- selectable `ATM_MOMENTUM`, `ATM_ORB`, `ATM_VWAP_REVERSION`, `ATM_MULTI`, `ATM_BREAKOUT_EXPANSION`, `ATM_IV_EXPANSION`, and `ATM_TRAP_REVERSAL`
- Greeks snapshot at entry scan time using Kite instruments plus Black-Scholes calculations
- IV percentile / IV rank approximation from recent option history
- IV change over the last 15 minutes for vega-crush protection
- option premium floor, delta floor, IV-percentile gate, VWAP-band filter, and underlying bias filter
- profile-based entry validation after the raw strategy signal:
  - `MOMENTUM`: trend alignment, breakout arming, follow-through confirmation, and pullback-entry gating
  - `MEAN_REVERSION`: VWAP retest plus controlled candle/range checks
  - `VOLATILITY`: trend alignment, range expansion, supportive IV behavior, and volatility-regime support
  - `ATM_MULTI` now routes dynamically into `MOMENTUM` or `MEAN_REVERSION` validation based on the profile it selected at signal time
- a substitute volatility-regime context is computed from realized session range, recent VWAP deviation, and 15-minute IV change
- advanced confirmation rules use that regime context:
  - momentum entries are blocked in sideways conditions
  - mean-reversion entries are blocked during expansion conditions
  - volatility entries require a non-sideways regime in addition to IV/range checks
- momentum entry setups are persisted in engine runtime state so armed/confirmed setups survive normal state saves during the session
- per-underlying trade cap, cooldown, max-hold time exit, and intraday cutoff / square-off window
- bounded two-leg short range pair support

Support status for the requested strategy ideas:

1. `Breakout + Expansion Strategy (Kill Theta Decay)`

- Status: `YES`
- Already covered by:
  - `ATM_BREAKOUT_EXPANSION` for compression -> breakout -> volume spike -> ATR expansion
  - `ATM_ORB` for opening-range breakout
  - `ATM_MOMENTUM` for directional breakout with RSI and VWAP alignment
- Feasibility with Kite: `HIGH`
  - Kite minute candles are enough for range-compression, breakout, ATR-expansion, and underlying volume-spike logic.
  - This can be implemented without any new broker feature, only extra signal logic on top of existing spot/index candles.

2. `IV Expansion Strategy (Reverse IV Crush Game)`

- Status: `YES`
- Already covered by:
  - `ATM_IV_EXPANSION`
  - IV percentile calculation
  - IV change over 15 minutes
  - option-price and underlying-price analytics at scan time
- Current entry model:
  - low-IV percentile gating is now applied at filter time while the directional trigger still comes from underlying price action at a key level
  - explicit “enter before IV spike” entry model
- Feasibility with Kite: `MEDIUM-HIGH`
  - Feasible if we continue using our own IV calculations from Kite price data.
  - The limitation is that this repo does not ingest full option-chain snapshots or exchange-native IV surfaces, so the signal would still use an approximate IV percentile instead of an institutional-grade volatility surface.

3. `Seller Trap Detection (Best Edge)`

- Status: `YES`
- Already covered by:
  - `ATM_TRAP_REVERSAL` for failed breakdown / failed breakout detection
- Feasibility with Kite: `HIGH`
  - Fully feasible from underlying minute candles alone.
  - This is a price-action strategy and does not require extra broker-side data beyond what Kite already provides.

4. `Avoid Sideways Markets (Most Important Filter)`

- Status: `YES`
- Already covered by:
  - `ATM_MULTI` explicitly prefers reversion only in sideways ATR conditions and now routes that choice into the mean-reversion validator
  - `INTRADAY_OPTIONS_MIN_RANGE_PCT` blocks low-volatility sessions
  - VWAP-band filter and underlying bias filter reduce entries in noisy, directionless conditions
  - a dedicated sideways blocker now rejects entries when recent prices stay trapped in a narrow VWAP band for multiple candles
  - the substitute volatility-regime classifier adds a session-level expansion/normal/sideways tag used by profile validators
- Current note:
  - current sideways filter is good, but not yet a dedicated “no trade when price is trapped in a narrow VWAP band for N candles” rule
- Feasibility with Kite: `HIGH`
  - We already have most of the inputs and can make this stricter very easily.

5. `Momentum Scalping (Speed Advantage)`

- Status: `YES`
- Already covered by:
  - `ATM_MOMENTUM`
  - `ATM_ORB` for early breakout continuation
  - intraday options uses `1m` candles and a `15s` supervision loop
  - time exits, target/stop/trailing logic, premium filters, and cooldown
- Current note:
  - momentum-profile entries now move through a three-step workflow: breakout arm -> follow-through confirmation -> pullback entry near EMA9/VWAP
  - current target logic is still fixed for ATM single-option flow instead of a true “5-15% fast scalp profile”
- Feasibility with Kite: `HIGH`
  - Existing engine structure already supports this well; adding a faster scalp preset would be the next clean refinement.

6. `Event-Based Strategy (Exploit Seller Risk)`

- Status: `NO`
- Already covered by:
  - nothing event-aware
- Missing pieces:
  - event calendar ingestion
  - pre-event compression detection
  - event-time execution rules
  - straddle-buy structure for intraday options
- Feasibility with Kite: `MEDIUM`
  - The market-data side is feasible, but Kite does not give you an economic/news event calendar.
  - This would require an external event source plus new multi-leg execution support for straddles.
  - Directional breakout-on-event is feasible; event-timed straddle automation is more involved.

7. `Smart Execution Layer (Critical for Bot)`

- Status: `PARTIAL`
- Already covered by:
  - cooldowns
  - per-underlying trade caps
  - lot-size aware sizing
  - dynamic ATM contract resolution
  - stop/target/trailing management
  - time-based exits and square-off behavior
  - pair synchronization for bounded two-leg range mode
- Missing pieces:
  - retry / failover logic for quote resolution and order placement
  - deeper slippage-aware order management for options beyond current limit/spread controls
  - full OCO lifecycle management for synthetic bracket exits
- Feasibility with Kite: `MEDIUM-HIGH`
  - Many of these are feasible with Kite order APIs and quote data.
  - The main limitations are now around full multi-leg/OCO execution-state management rather than basic quote/depth access.

## Files to Know

- [main.py](./main.py): thin runtime launcher
- [config.py](./config.py): broker env loading, symbol tables, F&O defaults
- [data_providers](./data_providers): provider plugins plus shared market-data service
- [data_fetcher.py](./data_fetcher.py): compatibility facade over the market-data service
- [fno_data_fetcher.py](./fno_data_fetcher.py): F&O contract discovery, metadata, analytics
- [option_analytics.py](./option_analytics.py): Black-Scholes, IV, Greeks
- [executor.py](./executor.py): broker-facing execution entry points backed by broker clients
- [executor_fno.py](./executor_fno.py): F&O-specific position helpers
- [engines](./engines): trading-engine implementations
- [engines/base.py](./engines/base.py): common `TradingEngine` abstract interface
- [brokers](./brokers): broker interfaces, concrete clients, and factory
- [models](./models): typed domain models such as `Position`
- [cli](./cli): prompt helpers and runtime setup/configuration flow
- [orchestration](./orchestration): context wiring, scan/session workflows, and position lifecycle helpers
- [state_store.py](./state_store.py): persistent runtime state

## Architecture Notes

Current refactoring status:

- Phase 1 foundations are in place:
  - typed `Position` model
  - `TradingEngine` ABC
  - `BrokerClient` ABC
  - unit coverage for the core foundations
- Phase 2 is now in place:
  - `backtesting.py` now uses typed position adapters for key P&L/equity paths
  - `executor.py` now delegates to concrete broker clients through a factory
  - `main.py` is now reduced to a thin launcher
  - runtime setup now lives in `cli/configuration.py`
  - session orchestration now lives in `orchestration/session.py`
  - signal scanning/evaluation now lives in `orchestration/signal_workflow.py`
  - market-data providers now live behind a shared plugin-style service in `data_providers/`
  - trading dependencies and persisted runtime state now hydrate through `orchestration/context.py`
- Phase 3 quality baseline is now in place:
  - broader type hints were added across refactored runtime seams
  - `pyproject.toml` now configures `mypy` and `ruff`
  - `.pre-commit-config.yaml` now wires formatting, lint, and type-check hooks
  - `requirements-dev.txt` now lists the developer tooling dependencies
  - the unit suite now covers broker/executor seams, persistence, orchestration helpers, signal helpers, and engine workflow behavior
  - automated coverage is now at `100` unit tests in this workspace

## Developer Quality

Developer tooling files now included:

- `pyproject.toml`
- `.pre-commit-config.yaml`
- `requirements-dev.txt`

Suggested local setup:

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pre_commit install
```

Configured quality gates:

- `ruff` for linting and formatting
- `mypy` with stricter checking targeted first at refactored modules
- `pre-commit` hooks for formatting, linting, type checking, and basic file hygiene

## Environment

Secrets/defaults are loaded from `.env`.

Example:

```env
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_api_secret
KITE_ACCESS_TOKEN=your_kite_access_token

UPSTOX_API_KEY=your_upstox_api_key
UPSTOX_API_SECRET=your_upstox_api_secret
UPSTOX_ACCESS_TOKEN=your_upstox_access_token
UPSTOX_REDIRECT_URI=http://127.0.0.1:8001

DATA_PROVIDER=YFINANCE
EXECUTION_PROVIDER=KITE
LOG_LEVEL=INFO
UPSTOX_STATIC_IP=49.205.247.48
BROKER_IP_MODE=IPV4_ONLY
```

Additional environment-backed controls for the stricter intraday equity defaults:

```env
MIN_RANKED_CANDIDATE_SCORE=0.008
INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS=2
REVERSAL_EXIT_CONFIRMATION_CANDLES=2
TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER=0.5
INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF=30
TRANSACTION_COST_MODEL_ENABLED=1
TRANSACTION_SLIPPAGE_PCT_PER_SIDE=0.0002
EXPECTED_EDGE_SCORE_MULTIPLIER=1.0
MIN_EDGE_TO_COST_RATIO=1.2
COST_EDGE_BUFFER_RUPEES=5.0
```

## State and Logging

- engine state is stored in `state/`
- session logs are written to `logs/`
- open positions persist with stop/target/trailing state
- position dicts are still persisted in legacy-compatible form, but are now validated through typed adapters in the refactored paths
- daily trade counts now persist for engines that enforce intraday frequency caps
- F&O positions can now also persist extra contract metadata such as lot size and entry analytics
- end-of-run trade reports are exported to `Results/`

## Backtesting

Run:

```powershell
run_backtest.bat
```

The backtest runner is now interactive and engine-aware, similar to the live CLI.

Current backtesting behavior:

- prints `[HELP]` before major inputs
- shows example input values
- prints available strategies before `Choose strategy:`
- prints valid Yahoo Finance periods before `Backtest period`
- prints valid Yahoo Finance intervals before `Backtest interval`
- exports result files automatically under `Results/BackTest/`

Backtest exports:

- `..._summary.txt`
- `..._trades.csv`
- `..._equity.csv`

Backtest summary now includes:

- ending equity
- total return
- closed trades
- win rate
- max drawdown
- estimated transaction charges
- estimated net P&L

Trade CSV now includes:

- gross P&L
- estimated charges
- estimated net P&L

Current transaction-cost coverage in backtesting:

- `intraday_equity`
- `delivery_equity`
- `futures_equity`
- `intraday_futures`
- `options_equity`
- `intraday_options`

### Current F&O Backtesting Limitation

There is now a basic F&O backtesting entry flow, but it is still simplified.

Current behavior:

- `intraday_options` backtesting in `ATM SINGLE OPTION` mode now uses the underlying only for signal generation, then resolves a real option contract and uses option premium candles for entry, exit, and sizing
- `intraday_options` `TWO-LEG RANGE PAIR` premium backtesting is not implemented yet
- this is still not a full options simulator with expiry decay modeling, historical Greeks, or dynamic strike-roll behavior matching live execution

### End-of-Run Trade Report

At the end of a session, the bot now generates a spreadsheet-style trade report in `Results/`.

Report contents:

- `Trades` sheet with symbol, side, quantity, entry/exit timestamps, gross P&L, estimated charges, and estimated net P&L
- `ExitReasonSummary` sheet with grouped counts and gross/net totals by exit reason such as `STOP_LOSS`, `TRAILING_STOP`, `TARGET`, and `REVERSAL`

The exporter prefers `.xlsx`. If the environment is missing spreadsheet libraries, the repo still has a built-in fallback writer so Excel output is still generated without extra installation in normal use.

## Intraday Options Controls

These environment-backed controls now affect `intraday_options`:

- `INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING`
- `INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS`
- `INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT`
- `INTRADAY_OPTIONS_MIN_RANGE_PCT`
- `INTRADAY_OPTIONS_MIN_SIGNAL_SCORE`
- `INTRADAY_OPTIONS_MAX_HOLD_MINUTES`
- `INTRADAY_OPTIONS_TIME_EXIT_CUTOFF`
- `INTRADAY_OPTIONS_IV_EXPANSION_MAX_IV_PERCENTILE`
- `INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT`
- `INTRADAY_OPTIONS_SIDEWAYS_LOOKBACK_CANDLES`
- `INTRADAY_OPTIONS_ROLL_TRIGGER_PCT`
- `INTRADAY_OPTIONS_THETA_EXIT_RATIO`
- `INTRADAY_OPTIONS_THETA_EXIT_MIN_MINUTES`

Current behavior:

- `BUY` entries are blocked unless option price is above session VWAP
- `SELL` entries are blocked unless option price is below session VWAP
- entries are blocked if 15-minute IV change is below the configured vega-crush threshold
- entries are blocked when the intraday range-percent volatility proxy is too low
- ATM single-leg `BUY_CE` entries are allowed only when the underlying is bullish on VWAP plus EMA
- ATM single-leg `BUY_PE` entries are allowed only when the underlying is bearish on VWAP plus EMA
- low-score signals are skipped even if they are directionally valid
- live entries can now be blocked when the best bid/ask spread is wider than the configured threshold
- live entries can now be blocked when available broker margin is below the estimated requirement
- broker-submitted orders are polled until a fill/reject/cancel state is confirmed instead of trusting the initial response alone
- live entries can now be sent as `MARKET` or `LIMIT` orders based on runtime config
- rejected live entries can retry with a reduced quantity and a repriced limit order
- partially filled live entries now retry the remaining quantity and size positions from the actual filled quantity
- broker-confirmed fills now emit slippage audit rows under `state/trade_store/`
- low-DTE contracts are warned about, but not force-blocked
- trade count limits are enforced per underlying, not per individual strike
- in two-leg range mode, both legs are entered together and both legs are exited together on range break or paired stop conditions
- bounded two-leg pairs now also have combined premium-based stop/target handling and combined P&L logging
- intraday options positions can be exited by max-hold time or cutoff time, not just price-based exits
- if a pair entry/execution becomes partial, the remaining live leg is unwound to avoid orphan exposure
- the CLI shows a confirmation summary for selected F&O contracts before the run continues
- open-position limits treat a bounded two-leg pair as one strategy structure instead of two separate slots
- live startup reconciliation now preserves persisted pair metadata so paired exits still work after a restart
- single-leg ATM entries are deduplicated at the underlying level so the bot does not keep stacking fresh ATM contracts for the same underlying in one session
- single-leg ATM entries now support `ATM`, `ATM + 1 STRIKE`, and `ATM - 1 STRIKE`
- open single-leg ATM positions can now roll to a refreshed strike when the underlying has moved far enough from the original ATM anchor
- long single-leg ATM positions can now exit explicitly when theta decay becomes too aggressive for the remaining premium
- intraday options supervision now runs every `15` seconds while signal entries still wait for closed `1m` candles
- order logs now print clearer entry and exit banners so live actions stand out in both console and log file
- bracket-order entry support is now exposed as a synthetic execution helper; it records stop-loss and target intent even though current Kite docs expose `regular` and `co` varieties rather than native `BO`
- single-leg entries now compute stop, target, trailing stop, breakeven, and expected net profit from asset-class-specific risk profiles plus estimated round-trip costs
- cost-aware entry gating rejects trades when estimated costs make the setup net-unprofitable or consume more than `35%` of projected profit
- `INTRADAY_OPTIONS` now uses wider risk presets:
  - `CONSERVATIVE`: `8%` SL, `12%` target, `4%` trail, `[6%, 12%, 18%]` staged targets
  - `BALANCED`: `10%` SL, `15%` target, `4.8%` trail, `[8%, 15%, 22%]` staged targets
  - `AGGRESSIVE`: `12%` SL, `20%` target, `6%` trail, `[10%, 18%, 28%]` staged targets
- `ATM_BREAKOUT_EXPANSION` looks for compression, breakout, volume spike, and ATR expansion on the underlying before buying the ATM option
- `ATM_IV_EXPANSION` looks for low-IV percentile plus a momentum candle at a key level before buying the ATM option
- `ATM_TRAP_REVERSAL` looks for failed support/resistance breaks and reversal recovery before buying the ATM option
- the sideways blocker suppresses entries when recent price action remains stuck inside a narrow VWAP band

## Known Gaps

- no rich broker-native margin calculator yet for complex option-selling structures
- no general basket/multi-leg options strategy engine yet beyond the bounded two-leg range pair
- no open-interest / option-chain analytics yet
- no reliable PCR/OI filter yet because there is no option-chain ingestion layer
- F&O backtesting is currently proxy-based for some flows rather than full contract-premium modeling
- Upstox F&O support is still missing
- IV percentile/rank is approximate, not a full volatility surface model

## Suggested Next Upgrades For User Experience

- add presets for common options workflows such as ATM scalp, 1-step OTM momentum, and expiry-day mode
- show margin and notional exposure per trade before confirming `LIVE` entries
- support option-chain browsing instead of manual single-contract selection
- add OI/PCR ingestion so directional filters are based on actual chain liquidity data
- add strike auto-refresh and auto-rollover for weekly expiry transitions
- add multi-leg strategy templates such as debit spreads, credit spreads, and straddles
- add a small dashboard or TUI view for live P&L, Greeks drift, and square-off countdown
- add open-interest, put-call ratio, and event-volatility filters for options
- deepen F&O backtesting with true option-premium candles, decay, rollover, and richer lot/margin modeling
- extend synthetic bracket support into managed OCO cancellation and broker-side child-order reconciliation

## Suggested Next Refactoring Steps

- continue migrating remaining dict-heavy position flows to typed helpers first, then to direct model usage

## Verification

The latest refactoring changes were checked with:

```powershell
venv\Scripts\python.exe -m unittest discover -s tests\unit -p "test_*.py"
venv\Scripts\python.exe -m py_compile main.py backtesting.py executor.py state_store.py trade_store.py data_fetcher.py cli\configuration.py orchestration\context.py orchestration\signal_workflow.py orchestration\session.py config.py
```
