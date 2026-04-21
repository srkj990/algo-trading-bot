# Zerodha Algo Bot

This repository is an interactive algo-trading bot for Indian markets with support for:

- intraday equity
- delivery equity
- positional index futures
- positional index options
- intraday index futures
- intraday index options

The bot runs from `main.py`, persists runtime state under `state/`, writes session logs under `logs/`, and can place real orders through supported brokers when `LIVE` mode is enabled.

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
  - lot-size aware sizing and startup sync for MIS options only

## Trading Features

- ATR-based stop, trailing stop, and target placement
- risk-style presets: `CONSERVATIVE`, `BALANCED`, `AGGRESSIVE`
- single-strategy, multi-strategy, and adaptive intraday mode
- candidate ranking by signal agreement, score, and ATR
- configurable max open positions and deployment caps
- one-trade-per-symbol-per-day control
- persisted positions, traded symbols, trade-day tracking, and regime cache

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

## Runtime Flow

Run:

```powershell
python main.py
```

The bot will prompt for:

1. data provider
2. execution mode
3. execution provider
4. engine
5. capital
6. symbol or F&O contract selection
7. risk style
8. open-position and capital limits
9. entry selection mode
10. strategy mode

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
6. Choose one of the four intraday ATM strategies
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

### Practical Strategy Selection Cheat Sheet

- Use `ATM_MOMENTUM` when the market is trending and staying away from VWAP.
- Use `ATM_ORB` when the edge is mostly in the first breakout after market open.
- Use `ATM_VWAP_REVERSION` when the market is balanced, choppy, and repeatedly reverting.
- Use `ATM_MULTI` when you want the safest default because it blocks conflicting signals.

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

## Files to Know

- [main.py](./main.py): interactive runtime loop and trade orchestration
- [config.py](./config.py): broker env loading, symbol tables, F&O defaults
- [fno_data_fetcher.py](./fno_data_fetcher.py): F&O contract discovery, metadata, analytics
- [option_analytics.py](./option_analytics.py): Black-Scholes, IV, Greeks
- [executor.py](./executor.py): broker order placement and position sync
- [executor_fno.py](./executor_fno.py): F&O-specific position helpers
- [engines](./engines): trading-engine implementations
- [state_store.py](./state_store.py): persistent runtime state

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
```

## State and Logging

- engine state is stored in `state/`
- session logs are written to `logs/`
- open positions persist with stop/target/trailing state
- daily trade counts now persist for engines that enforce intraday frequency caps
- F&O positions can now also persist extra contract metadata such as lot size and entry analytics

## Intraday Options Controls

These environment-backed controls now affect `intraday_options`:

- `INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING`
- `INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS`
- `INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT`
- `INTRADAY_OPTIONS_MIN_RANGE_PCT`
- `INTRADAY_OPTIONS_MIN_SIGNAL_SCORE`
- `INTRADAY_OPTIONS_MAX_HOLD_MINUTES`
- `INTRADAY_OPTIONS_TIME_EXIT_CUTOFF`

Current behavior:

- `BUY` entries are blocked unless option price is above session VWAP
- `SELL` entries are blocked unless option price is below session VWAP
- entries are blocked if 15-minute IV change is below the configured vega-crush threshold
- entries are blocked when the intraday range-percent volatility proxy is too low
- ATM single-leg `BUY_CE` entries are allowed only when the underlying is bullish on VWAP plus EMA
- ATM single-leg `BUY_PE` entries are allowed only when the underlying is bearish on VWAP plus EMA
- low-score signals are skipped even if they are directionally valid
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
- intraday options supervision now runs every `15` seconds while signal entries still wait for closed `1m` candles
- order logs now print clearer entry and exit banners so live actions stand out in both console and log file

## Known Gaps

- no margin-aware options selling model yet
- no general basket/multi-leg options strategy engine yet beyond the bounded two-leg range pair
- no open-interest / option-chain analytics yet
- no reliable PCR/OI filter yet because there is no option-chain ingestion layer
- no dynamic ATM strike rolling yet for live open positions
- no F&O backtesting engine yet
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
- add order-status polling, rejection summaries, and broker-side execution reconciliation after every live order
- add open-interest, put-call ratio, and event-volatility filters for options
- add F&O backtesting with lot sizing, expiry, and decay modeling

## Verification

The latest code changes were syntax-checked with:

```powershell
python -m compileall main.py fno_data_fetcher.py option_analytics.py engines executor_fno.py
```
