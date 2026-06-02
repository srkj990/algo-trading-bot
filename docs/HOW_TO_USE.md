# How To Use — Operator Guide

Practical guide for running backtests, paper sessions, and live sessions.

---

## Quick Start

```powershell
# Launch the web UI (recommended — no console needed)
venv\Scripts\python main.py

# Unit tests — run before any live change
venv\Scripts\python -m pytest
```

The web UI opens automatically at `http://localhost:8000`. Everything — configure, start, stop, reconfigure, backtest — is done from the browser. No console prompts.

---

## Web UI Overview

| Area | What it does |
| --- | --- |
| **Configure / Backtest tabs** | Set engine, execution mode, capital, risk style, symbols/contract, strategy |
| **Dashboard** | Live positions, trade book, P&L, candidates, log stream, market intelligence |
| **Backtest running view** | Live progress bar, running trades table, log stream during a backtest |
| **Backtest results view** | Summary tiles, equity curve, full trade table, regime analytics, failure analysis |

### Session lifecycle

1. Configure and click **Configure & Start →**
2. Dashboard appears — session is running
3. Click **■ STOP** to stop the session
4. Click **⚙ Reconfigure** to return to the configure form
5. Repeat from step 1 without restarting the process

The server process stays alive between sessions. Ctrl+C in the terminal exits the process cleanly.

---

## Three Modes

| Mode | Real orders | Best for |
| --- | --- | --- |
| **PAPER** | No — simulated fills | Dry run; full position tracking without a broker |
| **LIVE** | Yes — Kite / Upstox | Production trading |
| **Backtest** | No | Historical validation before paper |

**Always validate in this order: Backtest → Paper → Live.**

### Weekend / off-hours paper testing

Check **"Weekend / Off-hours paper test mode"** in the configure form. This bypasses market-hours checks and weekend guards so you can run paper sessions any time. Only available in PAPER mode.

### Paper mode

Paper mode creates real in-memory positions. `executor.place_order` returns a simulated fill at the entry price, so positions are tracked with stops, trailing stops, partial exits, and all risk guards active — exactly like live mode. Logs show `[PAPER] Simulated fill: BUY 50 SBIN.NS @ 512.30`.

---

## Market Ticker Bar

The top bar shows NIFTY 50, SENSEX, and India VIX in real time.

- VIX **green** = below 16 (calm)
- VIX **amber** = 16–20 (elevated)
- VIX **red** = above 20 (high risk)

Data source label shows `Kite live` or `yfinance`.

---

## Market Intelligence Panel

Shown while a session is running. Displays:

- **Regime**: detected market regime (`TREND UP`, `TREND DOWN`, `HIGH VOLATILITY`, `CHOPPY`, `RANGE BOUND`)
- **Signal Quality**: 0–100 score based on VWAP alignment, EMA, ADX, signal distribution, IV stability, VIX environment
- **VWAP breadth**: how many symbols are above/below VWAP
- **Signals**: buy/sell/hold counts across all scanned symbols
- **Avg Score**: mean signal score this cycle
- **VIX** and **ATM IV** (if F&O session)

---

## Warning Center

Warnings appear automatically when guards detect risky conditions. They are **advisory only** — the bot keeps running regardless.

| Severity | Meaning |
| --- | --- |
| **CRITICAL** | Serious risk condition (VIX > 22, IV crush, extreme breadth weakness) |
| **WARNING** | Elevated risk (VIX 18–22, IV expansion, time risk) |
| **INFO** | Informational (data staleness, liquidity caution) |

Click any warning to expand its detail. Click **History ▾** to see the last 40 warnings across the session.

Categories: `VOLATILITY`, `TIME`, `TREND`, `LIQUIDITY`, `OPTIONS_STRUCTURE`, `RISK`, `INFRA`

---

## Bot Override Controls

Shown in the dashboard while a session is running. Four controls:

### ⏸ Pause Entries
Blocks all new entries. Existing open positions continue running with their original stops, targets, and trailing logic. Session stays alive indefinitely.

**Resume** with the **▶ Resume** button.

### 🛡 Safe Exit
Blocks new entries AND immediately tightens all open positions:
- Stop moved to **breakeven** (entry price) — you cannot lose on any open trade
- Target **halved** — take profit at half the original distance
- Trailing activated immediately regardless of activation threshold

When all positions close, the session **auto-stops**. Use this when you want to exit carefully, not in a panic.

**Resume** clears both Pause Entries and Safe Exit flags.

### 🚨 Emergency Exit All
Closes all open positions immediately at market price. Records the trades. Session stays alive (you can resume and re-enter if desired). Use when you need out immediately.

### Override status badge
- **ACTIVE** (green) — normal operation
- **PAUSED** (amber) — entries paused or safe exit active
- **SAFE MODE** (amber) — safe exit active

---

## "Why This Trade?" Panel

Appears automatically when the bot enters a new position. Shows:

- Symbol, side, entry price, quantity, confidence %
- **Signal Quality score** (0–100) with label
- **Conditions Met**: reasons the entry passed all checks
- **Cautions / Risks**: warnings that were noted but did not block entry

Dismiss with the × button. A new panel appears on the next entry.

---

## P&L Display

Every trade table shows three P&L columns:

| Column | Meaning |
| --- | --- |
| **Gross P&L** | Raw profit before transaction costs |
| **Charges** | Estimated brokerage + STT + exchange fees + slippage |
| **Net P&L** | What you actually keep (Gross − Charges) |

The Session P&L card shows net P&L as the headline with a "Gross: ₹X (−₹Y charges)" sub-line.

The backtest results summary includes both **Gross P&L** and **Net P&L** tiles, plus a **Charges** tile that shows the charges as a percentage of gross P&L so you can see how much slippage drag is eating into returns.

---

## Backtest

### Running a backtest

1. Switch to the **Backtest** tab in the configure form
2. Set engine, capital, risk style, symbol/contract, strategy, period, interval
3. Click **▶ Run Backtest**
4. The running view shows a progress bar + live trades table as trades close

### Backtest results

After completion, the results view shows:

- **Summary tiles**: Total Return, Gross P&L, Charges (% of gross), Net P&L, Win Rate, Max Drawdown, Ending Equity, Tick Fills
- **Equity Curve** chart
- **Trade table** with Gross P&L, Charges, and Net P&L per trade
- **Regime Analytics**: performance broken down by time-of-day session and by exit reason
- **Failure Analysis**: max consecutive losses/wins, worst loss cluster, best win run, worst trading hour, loss concentration %, total L/W counts

Click **← New Backtest** to run another.

### Backtest output files

Results also written to `Results/BackTest/`:

- `backtest_<engine>_<timestamp>_summary.txt`
- `backtest_<engine>_<timestamp>_trades.csv` — fields include `pnl`, `estimated_charges`, `net_pnl`
- `backtest_<engine>_<timestamp>_equity.csv`

---

## Engine Guide

### 1. Intraday Equity

Engine id: `1` | Name: `intraday_equity`

- Intraday long and short stock entries on MIS; squares off before 15:15
- Strategies: `MA`, `RSI`, `VWAP`, `BREAKOUT`, `ORB`, `AUTO_ADAPTIVE`
- Adaptive stop/target/trailing from ATR, recent range, and signal score
- Backtest default: `3mo` / `5m`

**When to start here**: simplest engine; works with YFinance data; no F&O account needed.

### 2. Delivery Equity

Engine id: `2` | Name: `delivery_equity`

- Long-only delivery positions on daily candles (CNC)
- Strategies: `MA`, `RSI`, `BREAKOUT`
- Nifty 50-DMA trend guard required for new long entries
- Swing-style stop with 1.8% minimum floor

### 3. Futures Equity

Engine id: `3` | Name: `futures_equity`

- Positional NRML index futures
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; Kite only

### 4. Options Equity

Engine id: `4` | Name: `options_equity`

- Positional NRML index options
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; Kite only

### 5. Intraday Futures

Engine id: `5` | Name: `intraday_futures`

- Intraday MIS index futures; entry cutoff 15:05; square-off 15:15
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; Kite only

### 6. Intraday Options

Engine id: `6` | Name: `intraday_options`

- Intraday ATM single-option flow or two-leg bounded range pair (MIS)
- Strategies: `ATM_MOMENTUM`, `ATM_ORB`, `ATM_VWAP_REVERSION`, `ATM_MULTI`, `ATM_BREAKOUT_EXPANSION`, `ATM_IV_EXPANSION`, `ATM_TRAP_REVERSAL`
- Dynamic ATM strike resolution and rolling; staged momentum entry; three-level runner partial exits
- Greeks, IV, spread, OI, expiry, cost, and vega-crush filters

**Recommended workflow**: Backtest → Paper with staged mode → Live only after logs show correct contract, stop, target, and runner behavior.

---

## Strategy Reference

### Shared equity / futures strategies

| Strategy | Min candles | First actionable | What triggers entry |
| --- | ---: | ---: | --- |
| `MA` | 50 | 51 | MA20 crosses MA50 |
| `RSI` | 14 | 15 | RSI < 30 (BUY) or > 70 (SELL) |
| `BREAKOUT` | 20 | 22 | Close above prior 20-candle high / below low |
| `VWAP` | 1 | 6 | Close above / below cumulative VWAP |
| `ORB` | 20 | 21 | Close above / below first-15-candle range |

### Intraday-options strategies

| Strategy | Min candles | Entry condition |
| --- | ---: | --- |
| `ATM_MOMENTUM` | 20 | RSI + VWAP + 5-candle breakout alignment |
| `ATM_ORB` | 16 | Close beyond first-15-candle range |
| `ATM_VWAP_REVERSION` | 20 | Price re-enters VWAP after 6-candle deviation |
| `ATM_MULTI` | 20 | Aligned momentum + ORB, or VWAP reversion in sideways regime |
| `ATM_BREAKOUT_EXPANSION` | 45 | Compression + breakout + volume spike + ATR expansion |
| `ATM_IV_EXPANSION` | 30 | Key-level breakout + oversized candle body + RSI confirmation |
| `ATM_TRAP_REVERSAL` | 24 | Failed support/resistance break + oversized reversal body |

---

## Risk Styles

| Style | Risk per trade | Use when |
| --- | --- | --- |
| `CONSERVATIVE` | 0.5% | Learning, volatile week, after losing streak |
| `BALANCED` | 1.0% | Normal conditions, tested strategy |
| `AGGRESSIVE` | 1.5% | Strong conviction, confirmed trend |

---

## Runtime Position Resync (Manual Positions)

In LIVE sessions, the bot automatically polls the broker every `live_broker_resync_interval_seconds` (default 60 seconds) and injects any newly detected broker positions that are not already in the algo's position dict.

This means if you open a position manually in Kite while a session is running, the bot will detect it within ~60 seconds and start managing it — arming stop-loss, trailing stop, and target exits automatically.

You will see in the logs:
```
[RECON] Runtime sync detected manual broker position: NFO:NIFTY2660223300PE — added for management
```

**Notes:**
- Works in LIVE mode only (not PAPER or BACKTEST — no broker to poll)
- Only adds positions; never removes algo-managed positions mid-session
- The position is reconciled using the same engine logic as startup (`reconcile_startup`), so SL/target/trailing levels are correctly set
- Adjust the polling interval in `config.runtime.yaml → session_defaults.live_broker_resync_interval_seconds`

---

## Exit-Only Mode

When `exit_only_mode` is active (set in `config.runtime.yaml → session_defaults.exit_only_default: true` or via `cli/configuration.py`), the session manages and closes existing positions but places no new entries. Unlike the runtime Pause Entries override, exit-only mode is a config-level flag set before the session starts.

---

## High-Value Config Knobs

All in `config.runtime.yaml`. A process restart is required after changes.

| Knob | Default | Effect |
| --- | --- | --- |
| `session_defaults.exit_only_default` | false | Start in exit-only mode |
| `execution_safety.min_ranked_candidate_score` | 0.008 | Minimum signal score for entry |
| `orders.max_live_order_notional` | 0 (off) | Hard cap per order |
| `orders.margin_check_enabled` | true | Pre-flight margin check |
| `orders.exit_limit_price_buffer_pct` | 0.01 | Buffer applied to exit LIMIT prices (1% default); 0 to disable |
| `risk_controls.daily_max_loss_pct` | 0 (off) | Blocks entries after daily loss limit |
| `risk_controls.consecutive_loss_limit` | 0 (off) | Blocks after N consecutive losses |
| `fno.intraday_options_entry_mode` | `LIVE_STAGED` | `LIVE_STAGED` or `LEGACY_IMMEDIATE` |
| `fno.intraday_options_max_entry_cost_ratio` | 0.30 | Max cost/profit ratio for options entry |
| `session_defaults.live_broker_resync_interval_seconds` | 60 | How often (seconds) LIVE sessions poll broker for new manual positions |
| `execution_safety.exit_mode` | `TRAIL_ONLY` | `TRAIL_ONLY` = target activates trailing (no hard ceiling); `HARD_TARGET` = exit immediately at target price |

---

## Where Results Are Stored

| Location | Contents |
| --- | --- |
| `logs/` | Session event logs |
| `state/` | Runtime state: positions, trade counts, regime cache |
| `state/trade_store/` | Trade records and order-audit trail |
| `Results/BackTest/` | Backtest summary, trades CSV, equity CSV |

---

## Troubleshooting

### Session starts in exit-only mode

Check `session_defaults.exit_only_default` in `config.runtime.yaml`. Set to `false`.

### F&O provider choice is ignored

Expected. F&O sessions force both data and execution provider to Kite.

### No trades entered after signal fires

Check in order:
1. `execution_safety.min_ranked_candidate_score` — score too low?
2. Cost gate: look for `[SKIP] Trade not profitable after costs` in logs
3. Capital gate: `max_capital_deployed` or `max_capital_per_trade` exceeded?
4. One-trade-per-day: symbol already traded today?
5. Risk pause: `[RISK]` log lines showing a pause is active?
6. Override: is Pause Entries or Safe Exit active in the Bot Controls bar?

### Warning center shows CRITICAL but bot keeps trading

Correct behavior — warnings are advisory only. The bot never auto-stops on warnings. Use the Safe Exit or Emergency Exit buttons if you want to act on a warning.

### Safe Exit activated but session did not auto-stop

Safe Exit auto-stops only when all positions close. If there are no open positions when you activate it, it will stop immediately. If positions are open, it waits until each one exits naturally (via its tightened stop or halved target).

### Exit order rejected — "Market orders without market protection are not allowed via API"

This is a Kite API restriction for F&O instruments. Raw MARKET orders are not accepted. The bot uses LIMIT orders with a 1% buffer by default (`orders.exit_limit_price_buffer_pct = 0.01`). If you see this error after an older session config, check that `config.runtime.yaml` has `exit_limit_price_buffer_pct: 0.01` under the `orders` section and restart the session.

### Exit order rejected — "Order price is not a multiple of tick size"

NSE F&O tick size is ₹0.05. Limit prices from `LTP × (1 ± buffer)` can produce non-aligned floats. The executor now rounds all LIMIT prices to the nearest ₹0.05 automatically. If you see this error it means you are running an older version — update `executor.py`.

### Exit order rejected — "Wrong product type" or NRML position exited as MIS

This happens when a reconciled NRML position is exited with `product=MIS`. The fix stores `order_product` on the position dict at reconciliation time so exits use the correct product. Restart the session after updating `engines/intraday_options.py`.

### Manual position not picked up by bot during live session

In LIVE mode, the bot polls the broker every 60 seconds (configurable via `live_broker_resync_interval_seconds`) and automatically adds newly detected positions. If the position is still not showing after 60–90 seconds:
1. Confirm the session is in LIVE mode (not PAPER)
2. Check the log for `[RECON] Runtime sync detected manual broker position:`
3. If absent, check the engine's `reconcile_startup()` supports the instrument type (intraday_options: MIS and NRML both supported)

### Backtest charges look high

Check `transaction_costs` in `config.runtime.yaml`. The cost model estimates brokerage + STT + exchange fees. If `transaction_cost_model_enabled = false`, charges will show as zero.

### Paper positions disappear after restart

Paper positions are persisted to `state/`. If `state/` was cleared, positions are lost. Paper positions are not reconciled from a broker because no real broker order was placed.

### Tests before live release

```powershell
venv\Scripts\python -m pytest
```

Expected: tests pass with 1 pre-existing failure (an intraday-options cycle-state boundary edge case unrelated to entry, exit, or risk).
