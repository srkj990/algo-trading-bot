# Zerodha Algo Bot

This repository contains an equity trading bot with separate live execution engines for:

- `intraday_equity`
- `delivery_equity`

Market data can be sourced with `yfinance`, Zerodha Kite, or Upstox. Live orders can be routed through Zerodha Kite or Upstox when `LIVE` mode is enabled, and state is persisted under `state/`.

## Key Features

The bot includes:

- **ATR-based risk management**: Dynamic stop-loss calculation and position sizing using Average True Range
- **Signal scoring system**: Ranked candidates based on strategy signal quality and ATR normalization
- **Multi-strategy support**: Combine multiple strategies with configurable confirmation thresholds
- **Three risk profiles**: Conservative, Balanced, and Aggressive with different ATR multipliers and risk percentages
- **Flexible data providers**: `YFINANCE` (free/no auth), `KITE` (Zerodha), `UPSTOX` (Upstox APIs)
- **Flexible execution providers**: `KITE` (Zerodha), `UPSTOX` (Upstox APIs)
- **Configurable trading modes**: Paper trading (simulation) or Live execution
- **State persistence**: Positions saved to disk and reloaded on restart
- **Comprehensive logging**: Session-based event logging and audit trail

## Live Trading Flow

The main runtime entry point is `main.py`.

When started, the bot asks for:

1. data provider
2. execution mode: `PAPER` or `LIVE`
3. execution provider
4. engine: `intraday_equity` or `delivery_equity`
5. capital
6. symbol mode
7. risk style
8. max open positions / deployment caps
9. entry selection mode
10. strategy mode

## Provider Support

### Data providers

- `YFINANCE`: public Yahoo Finance candles, no broker auth required
- `KITE`: historical candles fetched through Zerodha Kite Connect
- `UPSTOX`: historical candles fetched through Upstox APIs

### Execution providers

- `KITE`: order placement plus position/holding sync through Zerodha
- `UPSTOX`: order placement plus position/holding sync through Upstox

You can mix providers. For example:

- `YFINANCE` data + `KITE` execution
- `YFINANCE` data + `UPSTOX` execution
- `KITE` data + `KITE` execution
- `UPSTOX` data + `UPSTOX` execution

### Symbol Selection

Supported modes:

- `SINGLE`
- `MANUAL_MULTI`
- `NIFTY50`

The default prompt now points to `NIFTY50 UNIVERSE`.

### Entry Selection

Candidates are ranked and the bot can enter:

- `TOP 1`
- `TOP N`

The default prompt now points to `TOP N`.

## Signal Scoring

Signals are still generated from the existing strategy set:

- `MA`
- `RSI`
- `BREAKOUT`
- `VWAP`
- `ORB`

Each actionable strategy signal now receives a numeric score. The score is built from the strategy-specific edge plus a small ATR normalization component. In multi-strategy mode:

- agreement count decides whether a symbol is actionable
- summed score decides relative quality
- ATR is used as an additional ranking tie-breaker

Ranked candidates are logged before any order is placed.

## Risk Styles

Three predefined risk profiles are available in [main.py](main.py):

### 1. CONSERVATIVE
- ATR stop multiplier: 1.5x
- Trailing ATR multiplier: 1.0x
- Target risk-reward ratio: 1.8:1
- Risk per trade: 0.5%

### 2. BALANCED (Default)
- ATR stop multiplier: 2.0x
- Trailing ATR multiplier: 1.25x
- Target risk-reward ratio: 2.0:1
- Risk per trade: 1.0%

### 3. AGGRESSIVE
- ATR stop multiplier: 2.5x
- Trailing ATR multiplier: 1.5x
- Target risk-reward ratio: 2.2:1
- Risk per trade: 1.5%

Each profile determines stop-loss distance (ATR × multiplier), position size, and trailing stop behavior.

## Engines

### Intraday Equity (`intraday_equity`)

**Data**: 1-day candles with 1-minute updates  
**Product**: MIS (Margin Intraday Square-off)  
**Trading Window**: 9:15 AM - 3:15 PM (force square-off at 3:15 PM)  
**Supported Strategies**: MA, RSI, VWAP, BREAKOUT, ORB  
**Entry Type**: Long & Short (bidirectional)  
**Key Features**:
- VWAP bias gate for entry filtering
- Breakout volume filter (1.2x volume multiplier)
- Adaptive gap/opening-range strategy switching
- Opening Range Breakout (ORB) from first 15-minute candle
- Entry cutoff at 3:10 PM to ensure timely exits

### Delivery Equity (`delivery_equity`)

**Data**: 6-month history with daily candles  
**Product**: CNC (Cash and Carry)  
**Trading Window**: 9:15 AM - 3:30 PM  
**Supported Strategies**: MA, RSI, BREAKOUT  
**Entry Type**: Long-only (no short selling)  
**Key Features**:
- Per-symbol allocation control (max 25% of capital per symbol)
- Longer-term position holding capability
- Exit on stop-loss, trailing stop, or sell signal

### Position Management

Both engines manage positions with:
- **Stop-Loss Exit**: Triggered when price hits stop-loss level
- **Trailing Stop Exit**: Locks in profits as price moves favorably
- **Target Exit**: Closes position at calculated profit target (when enabled)
- **Reversal Exit**: Closes existing position and enters opposite direction on new signal
- **Deployment Caps**: Control maximum open positions and capital allocation

## Running the Bot

### Live Trading

```powershell
python main.py
```

The bot will interactively prompt for:
1. Data provider (YFINANCE, KITE, UPSTOX)
2. Execution mode (PAPER or LIVE)
3. Execution provider (KITE or UPSTOX)
4. Trading engine (intraday_equity or delivery_equity)
5. Capital amount
6. Symbol mode (SINGLE, MANUAL_MULTI, NIFTY50)
7. Risk style (CONSERVATIVE, BALANCED, AGGRESSIVE)
8. Max open positions and deployment caps
9. Entry selection (TOP 1 or TOP N)
10. Strategy mode (SINGLE or MULTI with min confirmations)

### Backtesting

```powershell
python backtesting.py --capital 1000000 --period 1y --top-n 5 --max-positions 5
```

Full options:
- `--capital` - Starting capital (default: 1,000,000)
- `--period` - Data period (default: 1y; options: 1mo, 3mo, 6mo, 1y, 2y, 5y)
- `--top-n` - Number of top candidates to enter
- `--max-positions` - Max concurrent open positions
- `--risk-percent` - Risk per trade as % of capital
- `--atr-stop-multiplier` - ATR multiple for stop-loss
- `--trailing-atr-multiplier` - ATR multiple for trailing stop
- `--target-risk-reward` - Target risk-reward ratio
- `--strategies` - Comma-separated list (MA,RSI,BREAKOUT)
- `--min-confirmations` - Min strategy confirmations for entry

### Token Refresh

```powershell
python auto_auth.py
```

Updates both Zerodha Kite and Upstox access tokens in `.env` file.

## State Management & Logging

### Position State
- Persisted to `state/` directory (e.g., `intraday_equity_state.json`)
- Automatically loaded on bot restart
- Tracks:
  - Open positions with entry price, stop-loss, target, trailing stop
  - Today's traded symbols (to prevent duplicate entries)
  - Active trading day date
  - Last entry timestamp

### Logging
- Session logs stored in `logs/` directory
- Filename format: `algo_YYYYMMDD_HHMMSS_running.log`
- Configurable log level via `LOG_LEVEL` in [config.py](config.py)
- Includes detailed event traces for debugging and auditing

## Configuration

### Environment Variables

Secrets and defaults are loaded from `.env`:

```env
# Zerodha Kite
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_api_secret
KITE_ACCESS_TOKEN=your_kite_access_token

# Upstox
UPSTOX_API_KEY=your_upstox_api_key
UPSTOX_API_SECRET=your_upstox_api_secret
UPSTOX_ACCESS_TOKEN=your_upstox_access_token
UPSTOX_REDIRECT_URI=http://127.0.0.1:8001

# Defaults
DATA_PROVIDER=YFINANCE
EXECUTION_PROVIDER=KITE
LOG_LEVEL=INFO
```

### Symbol Lists

Tradable symbol lists defined in [config.py](config.py):
- `SINGLE_SYMBOL_TABLE` - Single stock for testing
- `MANUAL_SYMBOL_TABLE` - Custom multi-stock portfolio
- `NIFTY50_SYMBOLS` - NIFTY 50 index constituents (default scan universe)

### Broker Authentication

**Zerodha Kite Setup**:
1. Create app on [Zerodha Developer Console](https://kite.trade)
2. Get API key and API secret
3. Run `python auto_auth.py` to get and store access token

**Upstox Setup**:
1. Register app on [Upstox Developer Portal](https://upstox.com/developer/api)
2. Get API key, API secret, and set redirect URI to `http://127.0.0.1:8001` (or your port)
3. Run `python auto_auth.py` to get and store access token

## Safety Features

### Position Filtering (Critical Safety Feature)

**By default, the bot ONLY manages positions that are in your configured symbol tables.**

When running in LIVE mode, the bot will:
- ✅ **Load and manage** positions that exist in `NIFTY50_SYMBOLS`, `MANUAL_SYMBOL_TABLE`, or `SINGLE_SYMBOL_TABLE`
- ❌ **Skip and ignore** positions like `GOLDIETF.NS`, `ERNIN.NS`, or any other stocks not in your configured universe

**Configuration:**
```python
# In config.py
ONLY_MANAGE_CONFIGURED_SYMBOLS = True  # Default: True (safe)
```

**Set to `False` ONLY if you want the bot to manage ALL positions in your broker account.**

### Execution Mode Safety

- **PAPER Mode**: Simulates trading without touching real positions
- **LIVE Mode**: Only affects positions in configured symbol tables (when filtering is enabled)
- **Default to PAPER**: Bot starts in PAPER mode by default

### Broker Integration Safety

- **Position Sync**: Only loads positions from configured symbols
- **Order Placement**: Only places orders for symbols in your universe
- **State Persistence**: Saves position state locally for recovery

## File Structure

### Core Runtime Files
- [main.py](main.py) - Interactive trading bot entry point with configuration prompts
- [executor.py](executor.py) - Order placement and position/holding retrieval via Kite or Upstox
- [data_fetcher.py](data_fetcher.py) - Market data fetching from YFinance, Kite, or Upstox
- [strategy.py](strategy.py) - Strategy signal generation (MA, RSI, BREAKOUT, VWAP, ORB)

### Analysis & Risk
- [signal_scoring.py](signal_scoring.py) - Scores and ranks candidate signals with ATR normalization
- [risk_manager.py](risk_manager.py) - Position sizing, stop-loss, target, and trailing stop calculations
- [indicators.py](indicators.py) - Technical indicators (RSI, VWAP, ATR)

### Trading Engines
- [engines/intraday_equity.py](engines/intraday_equity.py) - Intraday trading engine (1-minute, MIS product, 9:15-15:30)
- [engines/delivery_equity.py](engines/delivery_equity.py) - Delivery trading engine (daily candles, CNC product, long-only)
- [engines/common.py](engines/common.py) - Shared utilities for position management and exit evaluation

### State & Configuration
- [config.py](config.py) - Broker configuration, symbol lists, environment loading
- [state_store.py](state_store.py) - Position state persistence (state/ directory)
- [logger.py](logger.py) - Session-based event logging (logs/ directory)
- [auto_auth.py](auto_auth.py) - Broker token refresh for Kite and Upstox

### Testing & Analysis
- [backtesting.py](backtesting.py) - Standalone daily-bar backtester for NIFTY50 universe



- **Safety First**: By default, the bot only manages positions in your configured symbol tables (`NIFTY50_SYMBOLS`, `MANUAL_SYMBOL_TABLE`, `SINGLE_SYMBOL_TABLE`). Stocks like `GOLDIETF`, `ERNIN`, etc. are automatically ignored unless you set `ONLY_MANAGE_CONFIGURED_SYMBOLS = False` in `config.py`.
- YFinance provider does not require broker authentication but needs internet access
- State files are JSON format and can be manually edited if needed
- Existing positions without ATR fields will work but new ATR-specific trailing logic only applies to new positions
- All timestamps use market time (IST/UTC+5:30)
- Position exits are checked at every new candle update
- The bot respects market holidays (weekends are automatically skipped)
