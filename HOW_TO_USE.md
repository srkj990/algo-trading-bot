# Trading Algo Bot — How to Use

> A practical guide for running backtests, paper trades, and live sessions across all six engines.  
> Read top-to-bottom the first time. After that, jump straight to the engine you want.

---

## Table of Contents

1. [First-Time Setup](#1-first-time-setup)
2. [Understanding the Three Modes](#2-understanding-the-three-modes)
3. [Engine Quick Reference](#3-engine-quick-reference)
4. [Engine 1 — Intraday Equity](#4-engine-1--intraday-equity)
5. [Engine 2 — Delivery Equity](#5-engine-2--delivery-equity)
6. [Engine 3 — Positional Futures](#6-engine-3--positional-futures)
7. [Engine 4 — Positional Options](#7-engine-4--positional-options)
8. [Engine 5 — Intraday Futures](#8-engine-5--intraday-futures)
9. [Engine 6 — Intraday Options](#9-engine-6--intraday-options)
10. [Reading Your Results](#10-reading-your-results)
11. [Key Config Knobs](#11-key-config-knobs)
12. [Recommended Progression](#12-recommended-progression)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. First-Time Setup

### Install dependencies

```bash
# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac/Linux

# Install runtime dependencies
pip install -r requirements.txt

# Install developer tools (optional but recommended)
pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pre_commit install
```

### Create your `.env` file

Copy the example below into a file named `.env` in the project root. Fill in only the credentials you will actually use.

```env
# ── Kite (required for all F&O engines) ──────────────────────────
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_api_secret
KITE_ACCESS_TOKEN=your_kite_access_token

# ── Upstox (optional – equity only for now) ──────────────────────
UPSTOX_API_KEY=your_upstox_api_key
UPSTOX_API_SECRET=your_upstox_api_secret
UPSTOX_ACCESS_TOKEN=your_upstox_access_token
UPSTOX_REDIRECT_URI=http://127.0.0.1:8001

# ── Defaults ─────────────────────────────────────────────────────
DATA_PROVIDER=YFINANCE          # YFINANCE | KITE | UPSTOX
EXECUTION_PROVIDER=KITE         # KITE | UPSTOX
LOG_LEVEL=INFO
```

> **No Kite credentials yet?** You can still run backtests and paper trades for all equity engines using `YFINANCE` as the data provider. F&O engines always require Kite.

### Verify the setup

```bash
python -m py_compile main.py backtesting.py
python -m unittest discover -s tests\unit -p "test_*.py"
```

All 100 unit tests should pass before you run anything live.

---

## 2. Understanding the Three Modes

| Mode | How to start | Orders placed? | What it teaches you |
|---|---|---|---|
| **Backtest** | `run_backtest.bat` | No | Whether your strategy had an edge historically |
| **Paper** | `python main.py` → choose `PAPER` | No | Whether your entry/exit logic behaves correctly in real market hours |
| **Live** | `python main.py` → choose `LIVE` | Yes — real money | Production trading |

**Always go Backtest → Paper → Live.** Never skip paper.

### What each mode saves

```
state/          ← open positions, trade-day tracking, regime cache (Paper & Live)
logs/           ← timestamped session logs (all modes)
Results/        ← end-of-session Excel trade report (Paper & Live)
Results/BackTest/ ← summary.txt, trades.csv, equity.csv (Backtest only)
state/trade_store/ ← JSONL order-audit and slippage records (Live only)
```

---

## 3. Engine Quick Reference

| Engine | Best for | Data needed | Broker needed |
|---|---|---|---|
| `intraday_equity` | Same-day stock scalps | YFinance or Kite | Optional (paper via YFinance) |
| `delivery_equity` | Multi-day stock positions | YFinance or Kite | Optional |
| `futures_equity` | Positional NIFTY/SENSEX futures | Kite | Kite |
| `options_equity` | Positional NIFTY/SENSEX options | Kite | Kite |
| `intraday_futures` | Intraday NIFTY/SENSEX futures | Kite | Kite |
| `intraday_options` | Intraday ATM option scalps | Kite | Kite |

---

## 4. Engine 1 — Intraday Equity

**What it does:** Scans a basket of stocks during market hours using 1-minute candles. Enters long or short positions intraday and auto squares off before the MIS window closes. Good for learning the bot before touching F&O.

**Strategies available:** `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`, or `ADAPTIVE` (multi-strategy auto-selection).

### Backtest example — RELIANCE with VWAP strategy

```bash
run_backtest.bat
```

```
[HELP] Choose engine (e.g. intraday_equity)
> intraday_equity

[HELP] Choose strategy (MA | RSI | BREAKOUT | VWAP | ORB | ADAPTIVE)
> VWAP

[HELP] Enter symbol (e.g. RELIANCE)
> RELIANCE

[HELP] Backtest period (e.g. 6mo, 1y, 2y)
> 6mo

[HELP] Backtest interval (e.g. 1d, 1h, 1m)
> 1d

[HELP] Starting capital (e.g. 100000)
> 100000
```

**Output written to:** `Results/BackTest/intraday_equity_RELIANCE_VWAP_summary.txt`

Sample summary you will see:

```
Ending Equity   : ₹1,12,430
Total Return    : 12.43%
Closed Trades   : 47
Win Rate        : 57.4%
Max Drawdown    : -6.2%
Est. Charges    : ₹3,210
Est. Net P&L    : ₹9,220
```

### Paper trade example — top 3 stocks, ADAPTIVE mode

```bash
python main.py
```

```
Choose engine: intraday_equity
Choose execution mode: PAPER
Choose data provider: YFINANCE
Enter capital: 200000
Enter symbols (comma-separated): RELIANCE,INFY,HDFCBANK
Choose risk style (CONSERVATIVE | BALANCED | AGGRESSIVE): BALANCED
Max open positions: 3
Max capital per position: 60000
Entry selection mode (TOP1 | TOP3 | ALL): TOP 1
Strategy mode: ADAPTIVE
One trade per symbol per day? YES
```

The bot now runs. Every 60 seconds it scans all three symbols, ranks candidates by signal score, and takes the top-ranked setup if it clears the minimum score threshold. You will see output like:

```
[SCAN] RELIANCE  | Score: 0.031 | Signal: BUY  | VWAP bias: BULLISH
[SCAN] INFY      | Score: 0.009 | Signal: NONE | Below threshold
[SCAN] HDFCBANK  | Score: 0.011 | Signal: SELL | VWAP bias: BEARISH
[ENTRY] RELIANCE BUY  50 shares @ ₹2,874 | SL ₹2,831 | Target ₹2,960
[EXIT]  RELIANCE SELL 50 shares @ ₹2,941 | P&L +₹3,350 | Reason: TARGET
```

### Live example — single stock, BREAKOUT strategy

```bash
python main.py
```

```
Choose engine: intraday_equity
Choose execution mode: LIVE
Choose data provider: KITE
Enter capital: 500000
Enter symbols: TCS
Choose risk style: BALANCED
Max open positions: 1
Entry selection mode: TOP 1
Strategy mode: BREAKOUT
```

The bot places real MIS orders through Kite. Every order goes through pre-flight margin validation and spread checks before submission. Fill confirmation is polled until the order reaches a confirmed state.

---

## 5. Engine 2 — Delivery Equity

**What it does:** Long-only delivery positions held for multiple days. Uses daily candles and CNC product type. Suitable for swing trading a watchlist of stocks with per-symbol capital allocation.

**Strategies available:** `MA`, `RSI`, `BREAKOUT`.

### Backtest example — NIFTY 50 basket, MA strategy, 1 year

```bash
run_backtest.bat
```

```
Choose engine: delivery_equity
Choose strategy: MA
Enter symbols: RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK
Backtest period: 1y
Backtest interval: 1d
Starting capital: 1000000
```

**Output:** `Results/BackTest/delivery_equity_MA_summary.txt`

```
Ending Equity   : ₹11,42,000
Total Return    : 14.2%
Closed Trades   : 23
Win Rate        : 65.2%
Max Drawdown    : -8.7%
Est. Charges    : ₹4,100
Est. Net P&L    : ₹1,38,000
```

### Paper trade example — 5-stock basket

```bash
python main.py
```

```
Choose engine: delivery_equity
Choose execution mode: PAPER
Choose data provider: YFINANCE
Enter capital: 500000
Enter symbols: RELIANCE,TCS,WIPRO,BAJFINANCE,AXISBANK
Choose risk style: CONSERVATIVE
Max open positions: 5
Max capital per position: 80000
Strategy mode: MA
```

The bot runs once per day (daily candle resolution). Positions are held overnight and reconciled with your broker holdings on restart.

### Live example

```bash
python main.py
```

```
Choose engine: delivery_equity
Choose execution mode: LIVE
Choose data provider: KITE
Enter symbols: HDFCBANK,ICICIBANK,KOTAKBANK
Choose risk style: BALANCED
Max open positions: 3
Max capital per position: 150000
Strategy mode: RSI
```

Positions are placed as CNC (delivery) orders. The bot reconciles with your actual Kite holdings on every startup, so a crash or restart does not orphan positions.

---

## 6. Engine 3 — Positional Futures

**What it does:** Trades NIFTY 50 or SENSEX index futures as positional (NRML) positions. Resolves the live front-month contract automatically, rounds quantities to lot size, and reconciles with broker F&O positions.

> **Requires Kite** for both data and execution. No YFinance option.

### Backtest example — NIFTY futures, RSI strategy

```bash
run_backtest.bat
```

```
Choose engine: futures_equity
Choose strategy: RSI
Choose underlying (NIFTY | SENSEX | BOTH): NIFTY
Choose expiry (e.g. 2025-01-30): 2025-01-30
Backtest period: 3mo
Backtest interval: 1d
Starting capital: 500000
```

**Output:** `Results/BackTest/futures_equity_NIFTY_RSI_summary.txt`

```
Ending Equity   : ₹5,38,000
Total Return    : 7.6%
Closed Trades   : 11
Win Rate        : 63.6%
Max Drawdown    : -4.1%
Est. Charges    : ₹8,200
Est. Net P&L    : ₹30,800
```

> **F&O backtest note:** This backtest uses the underlying index as a price proxy. It is not a full contract-premium backtest with expiry roll and margin decay modeling. Treat P&L numbers as directional signal validation, not exact live performance estimates.

### Paper trade example — NIFTY futures

```bash
python main.py
```

```
Choose engine: futures_equity
Choose execution mode: PAPER
  (Data provider auto-set to KITE for F&O)
Enter capital: 500000
Choose underlying: NIFTY
Choose expiry: 2025-01-30
  → Resolved: NIFTY25JAN26FUT | Lot size: 75
Choose risk style: BALANCED
Max open positions: 1   (auto-set for single contract)
Strategy mode: MA
```

Console output during session:

```
[SCAN]  NIFTY  | Score: 0.044 | MA Signal: BUY | Trend: BULLISH
[ENTRY] NIFTY25JAN26FUT BUY 1 lot (75 units) @ ₹23,850
        SL: ₹23,620 | Target: ₹24,310 | ATR stop active
[EXIT]  NIFTY25JAN26FUT SELL 75 units @ ₹24,190 | P&L +₹25,500 | Reason: TARGET
```

### Live example

```bash
python main.py
```

```
Choose engine: futures_equity
Choose execution mode: LIVE
Enter capital: 500000
Choose underlying: SENSEX
Choose expiry: 2025-01-30
Choose risk style: BALANCED
Strategy mode: BREAKOUT
```

---

## 7. Engine 4 — Positional Options

**What it does:** Trades NIFTY or SENSEX index options as positional (NRML) multi-day positions. You manually choose the expiry, option type (CE/PE), and strike. The bot then monitors the position with stop, target, and trailing-stop logic.

> **Requires Kite.**

### Backtest example — NIFTY 24500 CE, MA strategy

```bash
run_backtest.bat
```

```
Choose engine: options_equity
Choose strategy: MA
Choose underlying: NIFTY
Option type (CE | PE): CE
Strike: 24500
Choose expiry: 2025-01-30
Backtest period: 1mo
Backtest interval: 1d
Starting capital: 200000
```

### Paper trade example

```bash
python main.py
```

```
Choose engine: options_equity
Choose execution mode: PAPER
Enter capital: 200000
Choose underlying: NIFTY
Option type: CE
Strike: 24500
Choose expiry: 2025-01-30
  → Resolved: NIFTY25JAN24500CE | Lot size: 75 | Premium: ₹142
Choose risk style: BALANCED
Max open positions: 1
Strategy mode: RSI
```

Console output:

```
[SCAN]  NIFTY  | RSI: 64 | Signal: BUY
[ENTRY] NIFTY25JAN24500CE BUY 1 lot (75 units) @ ₹142 | SL ₹128 | Target ₹178
[EXIT]  NIFTY25JAN24500CE SELL 75 @ ₹171 | P&L +₹2,175 | Reason: TRAILING_STOP
```

### Live example

```bash
python main.py
```

```
Choose engine: options_equity
Choose execution mode: LIVE
Choose underlying: SENSEX
Option type: PE
Strike: 79000
Choose expiry: 2025-01-30
Choose risk style: CONSERVATIVE
Strategy mode: VWAP
```

---

## 8. Engine 5 — Intraday Futures

**What it does:** Same as positional futures but uses MIS product type for intraday-only positions. Has a hard entry cutoff and forces square-off before MIS auto-square time. Best for traders who want leveraged index exposure without overnight risk.

> **Requires Kite.**

### Backtest example — NIFTY intraday futures, VWAP strategy

```bash
run_backtest.bat
```

```
Choose engine: intraday_futures
Choose strategy: VWAP
Choose underlying: NIFTY
Choose expiry: 2025-01-30
Backtest period: 1mo
Backtest interval: 1m
Starting capital: 300000
```

> The backtest uses underlying 1-minute candles as a proxy. Actual futures premium tracking is not modeled.

### Paper trade example

```bash
python main.py
```

```
Choose engine: intraday_futures
Choose execution mode: PAPER
Enter capital: 300000
Choose underlying: NIFTY
Choose expiry: 2025-01-30
  → Resolved: NIFTY25JAN26FUT | Lot size: 75 | Product: MIS
Choose risk style: BALANCED
Strategy mode: VWAP
```

Console output:

```
[09:22] [SCAN]  NIFTY | VWAP: 23,820 | Price: 23,860 | Signal: BUY
[09:22] [ENTRY] NIFTY25JAN26FUT BUY 75 units @ ₹23,862 | SL ₹23,730
[11:45] [EXIT]  NIFTY25JAN26FUT SELL 75 @ ₹24,010 | P&L +₹11,100 | Reason: TARGET
[15:10] [INFO]  MIS square-off window approaching — no new entries allowed
```

### Live example

```bash
python main.py
```

```
Choose engine: intraday_futures
Choose execution mode: LIVE
Choose underlying: SENSEX
Choose expiry: 2025-01-30
Choose risk style: AGGRESSIVE
Strategy mode: ORB
```

The bot blocks new entries 30 minutes before the MIS square-off window and automatically exits any open position before the broker's auto-square time.

---

## 9. Engine 6 — Intraday Options

**What it does:** The most feature-rich engine. Monitors the underlying index in real time using 1-minute candles (checked every 15 seconds). When the chosen strategy fires, it automatically resolves the live ATM (or near-ATM) option contract and places the trade. Includes strike rolling, theta exit, vega-crush blocking, spread filtering, and margin pre-checks.

> **Requires Kite.** This engine has two distinct structures — choose based on your market view.

### Structure A — ATM Single Option (directional scalp)

You pick the underlying, expiry, and strike mode. The bot resolves whether to buy CE or PE based on the signal.

**When to use:** Trending or breakout sessions where you want a single directional bet.

#### Backtest example — NIFTY ATM, Momentum strategy

```bash
run_backtest.bat
```

```
Choose engine: intraday_options
Choose structure: ATM SINGLE OPTION
Choose strategy: ATM_MOMENTUM
Choose underlying: NIFTY
Choose expiry: 2025-01-30
Choose strike mode (ATM | ATM+1 | ATM-1): ATM
Backtest period: 1mo
Backtest interval: 1m
Starting capital: 200000
```

> The backtest uses the underlying spot index as a price proxy. True contract-premium decay and roll are not modeled.

**Output:** `Results/BackTest/intraday_options_NIFTY_ATM_MOMENTUM_summary.txt`

```
Ending Equity   : ₹2,24,000
Total Return    : 12.0%
Closed Trades   : 18
Win Rate        : 55.6%
Max Drawdown    : -9.3%
Est. Charges    : ₹6,400
Est. Net P&L    : ₹17,600
```

#### Paper trade example — NIFTY ATM, Multi-strategy

```bash
python main.py
```

```
Choose engine: intraday_options
Choose execution mode: PAPER
  (Data + execution auto-set to KITE)
Enter capital: 200000
Choose underlying: NIFTY
Choose structure: ATM SINGLE OPTION
Choose expiry: 2025-01-30
Choose strike mode: ATM
  → Strike confirmed: ATM (resolves live at entry time)
Choose strategy: ATM_MULTI
```

Console output during session:

```
[09:31] [SCAN]  NIFTY spot: 24,388 | VWAP: 24,351 | RSI: 62 | IV%: 31
[09:31] [SIGNAL] ATM_MULTI → MOMENTUM profile → BUY_CE
[09:31] [FILTER] Delta: 0.51 ✓ | IV%: 31 ✓ | Spread: 0.8% ✓ | Margin: OK ✓
[09:31] [ENTRY]  NIFTY25JAN24400CE BUY 1 lot (75 units) @ ₹98
                 SL: ₹88 (-10%) | Target: ₹118 (+20%) | Trailing: 7.5%
[10:14] [ROLL]   Underlying moved +1.8% → rolling to NIFTY25JAN24550CE
[10:14] [EXIT]   NIFTY25JAN24400CE SELL 75 @ ₹52 | P&L -₹3,450 (roll exit)
[10:14] [ENTRY]  NIFTY25JAN24550CE BUY 1 lot (75 units) @ ₹85
[11:02] [EXIT]   NIFTY25JAN24550CE SELL 75 @ ₹119 | P&L +₹2,550 | Reason: TARGET
[14:55] [THETA]  Premium ₹42 | Theta ratio exceeded threshold → forced exit
[14:55] [EXIT]   (any open position) | Reason: THETA_EXIT
[15:10] [INFO]   Intraday cutoff reached — no new entries
[15:25] [SQUAREOFF] All MIS positions closed before broker window
```

#### Live example — NIFTY ATM+1, ORB strategy

```bash
python main.py
```

```
Choose engine: intraday_options
Choose execution mode: LIVE
Enter capital: 200000
Choose underlying: NIFTY
Choose structure: ATM SINGLE OPTION
Choose expiry: 2025-01-30
Choose strike mode: ATM + 1 STRIKE
Choose strategy: ATM_ORB
```

What happens live:

1. Bot waits for the first 15-minute candle to close to establish the opening range.
2. When price breaks the range high, signal fires `BUY_CE`. Bot resolves the live `ATM + 1` call strike.
3. Pre-flight checks: spread, margin, delta, IV percentile, vega-crush guard all run.
4. If all pass → limit order placed via Kite, polled until fill confirmed.
5. Fill-based SL and target are set. Position is tracked with 7.5% trailing stop.
6. If underlying moves more than `INTRADAY_OPTIONS_ROLL_TRIGGER_PCT` — position rolls to a fresh ATM strike.
7. If theta decay crosses `INTRADAY_OPTIONS_THETA_EXIT_RATIO` — position is force-exited.
8. All positions squared off before MIS window regardless of P&L.

---

### Structure B — Two-Leg Range Pair (range-bound sessions)

You pick the expiry, a lower PE strike, and an upper CE strike. The bot enters both legs and manages them as a single unit. Exits if the underlying breaks the range.

**When to use:** Sideways, low-volatility sessions. You are short both wings and collecting premium while the index stays inside your band.

#### Paper trade example — NIFTY two-leg range pair

```bash
python main.py
```

```
Choose engine: intraday_options
Choose execution mode: PAPER
Enter capital: 300000
Choose underlying: NIFTY
Choose structure: TWO-LEG RANGE PAIR
Choose expiry: 2025-01-30
Choose lower PE strike: 24100
Choose upper CE strike: 24700
  → NIFTY25JAN24100PE | Premium ₹38 | Delta -0.18
  → NIFTY25JAN24700CE | Premium ₹41 | Delta  0.16
  → Combined premium: ₹79 | Range width: 600 pts
  → Confirm? YES
```

Console output:

```
[09:32] [ENTRY] NIFTY25JAN24100PE SELL 75 @ ₹38
[09:32] [ENTRY] NIFTY25JAN24700CE SELL 75 @ ₹41
        Combined credit: ₹5,925 | Pair SL: combined debit > ₹119 (2× credit)
[11:15] [SCAN]  NIFTY: 24,440 | Inside range 24,100–24,700 ✓
[14:30] [SCAN]  NIFTY: 24,680 | Approaching upper bound — caution
[14:45] [EXIT]  Range break above 24,700 → unwind both legs
        NIFTY25JAN24100PE BUY 75 @ ₹12 | Profit: ₹1,950
        NIFTY25JAN24700CE BUY 75 @ ₹68 | Loss : -₹2,025
        Net pair P&L: -₹75 | Reason: RANGE_BREAK
```

#### Live example — SENSEX two-leg range pair

```bash
python main.py
```

```
Choose engine: intraday_options
Choose execution mode: LIVE
Enter capital: 300000
Choose underlying: SENSEX
Choose structure: TWO-LEG RANGE PAIR
Choose expiry: 2025-01-30
Choose lower PE strike: 79000
Choose upper CE strike: 81000
```

Both legs are submitted together. If either leg partially fills, the other leg is unwound automatically to avoid orphan exposure.

---

## 10. Reading Your Results

### Backtest output files

Located in `Results/BackTest/`:

| File | Contents |
|---|---|
| `*_summary.txt` | Equity, return, win rate, drawdown, net P&L after charges |
| `*_trades.csv` | Every trade with gross P&L, charges, net P&L per trade |
| `*_equity.csv` | Equity curve over time |

### Live/paper session report

Located in `Results/`:

| Sheet | Contents |
|---|---|
| `Trades` | Symbol, side, quantity, entry/exit time, gross P&L, charges, net P&L |
| `ExitReasonSummary` | Grouped counts and totals by STOP_LOSS, TARGET, TRAILING_STOP, REVERSAL, THETA_EXIT, RANGE_BREAK |

### Order audit trail (live only)

Located in `state/trade_store/`:

- JSONL files with pre-flight, submission, fill-confirmation, and slippage records for every order.
- Use these to verify actual fill price vs expected price and measure real slippage.

### Reading exit reasons

| Exit reason | What happened |
|---|---|
| `TARGET` | Price hit your profit target. Good. |
| `STOP_LOSS` | Price hit your stop. Worked as intended. |
| `TRAILING_STOP` | Trailing stop triggered after the move. Locked in partial profit. |
| `REVERSAL` | Signal reversed. Exit before full stop. |
| `THETA_EXIT` | Premium decayed too fast relative to remaining time. Options-specific. |
| `RANGE_BREAK` | Underlying broke outside the two-leg pair band. |
| `MAX_HOLD` | Position held too long — max-hold-time exit triggered. |
| `SQUAREOFF` | MIS intraday square-off window forced exit. |

---

## 11. Key Config Knobs

All of these can be set in `.env` or `config.runtime.yaml` without changing code.

### General trading behaviour

```env
MIN_RANKED_CANDIDATE_SCORE=0.008       # Minimum signal score to enter (raise to trade less)
REVERSAL_EXIT_CONFIRMATION_CANDLES=2  # Candles needed to confirm a reversal exit
TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER=0.5  # When trailing kicks in
TRANSACTION_COST_MODEL_ENABLED=1      # Include realistic cost estimates (keep ON)
```

### Intraday equity safety

```env
INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF=30  # Stop entries 30m before MIS close
INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS=2            # Require MA + RSI to agree
MIN_EDGE_TO_COST_RATIO=1.2                                # Skip trades where edge < 1.2× cost
```

### Intraday options controls

```env
INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING=3    # Daily trade cap per index
INTRADAY_OPTIONS_MAX_HOLD_MINUTES=90            # Force-exit after 90 minutes
INTRADAY_OPTIONS_TIME_EXIT_CUTOFF=14:45         # No new entries after this time
INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT=5     # Block if 15m IV change < 5%
INTRADAY_OPTIONS_MIN_RANGE_PCT=0.3              # Block if session range < 0.3%
INTRADAY_OPTIONS_ROLL_TRIGGER_PCT=1.5           # Roll strike if underlying moves 1.5%
INTRADAY_OPTIONS_THETA_EXIT_RATIO=0.4           # Exit if theta > 40% of remaining premium
INTRADAY_OPTIONS_THETA_EXIT_MIN_MINUTES=60      # Theta exit only active after 60 minutes
INTRADAY_OPTIONS_IV_EXPANSION_MAX_IV_PERCENTILE=40  # IV Expansion: enter only below 40th percentile
INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT=0.2    # Sideways blocker band width
INTRADAY_OPTIONS_SIDEWAYS_LOOKBACK_CANDLES=8   # Candles to check for sideways condition
```

### Risk styles at a glance

| Style | Stop | Target | Trailing | Best for |
|---|---|---|---|---|
| `CONSERVATIVE` | Tight | Moderate | Early | Capital preservation, new users |
| `BALANCED` | Moderate | Moderate | Standard | General use — start here |
| `AGGRESSIVE` | Wide | Wide | Late | High conviction, experienced users |

---

## 12. Recommended Progression

Follow this order when you first use each engine. Do not skip steps.

```
Week 1 — Backtest
  └── intraday_equity   → VWAP, 6mo, 3 stocks
  └── delivery_equity   → MA, 1y, 5 stocks

Week 2 — Paper (equity)
  └── intraday_equity   → PAPER, BALANCED, 1 week of real market hours
  └── Check Results/ — compare fills to expected, exit reasons to signals

Week 3 — Backtest (F&O)
  └── intraday_futures  → VWAP, NIFTY, 1mo
  └── intraday_options  → ATM_MOMENTUM, ATM, 1mo

Week 4 — Paper (F&O)
  └── intraday_options  → PAPER, ATM_MULTI, 1 week
  └── Monitor: actual signal firing rate, roll events, theta exits

Week 5+ — Live (small size first)
  └── Start with 1 lot, CONSERVATIVE risk style
  └── Check state/trade_store/ slippage records after every session
  └── Only scale up when paper vs live results match closely
```

---

## 13. Troubleshooting

### Bot exits immediately at startup

```
RuntimeConfig validation error: ...
```

A config value is invalid (e.g., a negative TTL, zero lot size). Check your `.env` and `config.runtime.yaml` values against the key knobs listed above.

### F&O engine says "KITE credentials missing"

All F&O engines require Kite. Make sure `KITE_API_KEY`, `KITE_API_SECRET`, and `KITE_ACCESS_TOKEN` are set in `.env`. The access token must be refreshed daily — use `auto_auth.py` or `run_auto_auth.bat` to automate this.

### Order rejected — "Insufficient margin"

The bot's margin pre-check is blocking the entry. Either reduce position size, free up margin in your Kite account, or reduce `Max open positions`.

### Kite `invalid access token` error mid-session

The Kite access token expired (tokens last 24 hours). Run `run_auto_auth.bat` to refresh, then restart the bot. Open positions in `state/` are preserved.

### Paper and live results look very different

Check `state/trade_store/` slippage audit records. Common causes: wide bid-ask spreads on the option you chose (filter with `INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT`), low OI on the strike, or the backtest using a proxy price rather than actual option premium.

### Position stuck open after restart

The bot persists positions in `state/`. On restart it reconciles with broker positions. If a position is genuinely orphaned, close it manually in Kite and then delete the relevant entry from `state/positions.json`.

### Running unit tests to verify a code change

```bash
venv\Scripts\python.exe -m unittest discover -s tests\unit -p "test_*.py"
```

All 100 tests should pass. If any fail after a code change, do not run live until the failure is resolved.

---

*Document reflects the codebase as of the commit: "Add resilient live order handling and intraday options roll/theta controls."*
