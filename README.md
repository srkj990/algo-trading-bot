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
  - intraday entry cutoff
  - forced MIS square-off window
  - Greeks and IV snapshot on every scan
  - premium, delta, IV-percentile, and VWAP-band filters before entry
  - vega-crush blocker based on 15-minute IV change
  - expiry warning when time-to-expiry is very low
  - configurable per-underlying daily trade cap
  - selectable `SINGLE OPTION` or `TWO-LEG RANGE PAIR` entry flow
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

For intraday options, these price-action signals are now complemented by option analytics filters instead of replacing them.

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
  - `SINGLE OPTION`
  - `TWO-LEG RANGE PAIR`

For single-option flow:

- choose expiry
- choose `CE` or `PE`
- choose strike mode:
  - `ATM`
  - `OTM offset`
  - `ITM offset`
  - `MANUAL`

For two-leg range pair flow:

- choose expiry
- choose lower `PE` strike
- choose upper `CE` strike
- review the resolved contracts, lot size, premium, Greeks, and range width
- the bot treats this as a bounded-range short pair when the underlying stays inside the selected strike band

The CLI now also logs the resolved lot size for every selected F&O contract.

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
- single-leg CE entries are allowed only when the underlying is bullish on VWAP plus EMA
- single-leg PE entries are allowed only when the underlying is bearish on VWAP plus EMA
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
