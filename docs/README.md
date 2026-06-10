# Algo Trading Bot

Algorithmic trading and backtesting system for Indian markets (NSE/BSE/NFO/BFO).
Supports six trading engines across three execution modes with shared signal, risk, and position logic.

For day-to-day operator instructions, read [HOW_TO_USE.md](./HOW_TO_USE.md).

The system is operated entirely through a browser-based web UI — no console interaction required. Launch with `venv\Scripts\python main.py`, open `http://localhost:8000`, and configure/start/stop/reconfigure from there.

---

## Execution Modes

| Mode | Entry point | Real orders | Use for |
| --- | --- | --- | --- |
| **Backtest** | Web UI → Backtest tab | No | Historical simulation |
| **Paper** | Web UI → Live tab → PAPER | No — simulated fills | Market-hours dry run |
| **Live** | Web UI → Live tab → LIVE | Yes — Kite / Upstox | Production trading |

**Paper mode simulates fills.** `executor.place_order` returns a synthetic `OrderResult` in PAPER mode so positions are tracked in memory exactly as they would be in LIVE mode. This enables the full position lifecycle (stop-loss, trailing, target, exit) to run during paper sessions without connecting to a broker.

**Weekend / off-hours paper testing** is supported by checking the "Weekend / Off-hours paper test mode" checkbox in the configure form — this bypasses market-hours and weekend guards (PAPER only).

---

## Six Trading Engines

| # | Engine | Product | Candle cadence | Typical use |
| --- | --- | --- | --- | --- |
| 1 | `intraday_equity` | `MIS` | `1m` live / `5m` backtest default | Intraday stock trading |
| 2 | `delivery_equity` | `CNC` | `1d` | Swing and delivery positions |
| 3 | `futures_equity` | `NRML` | `5m` live / `15m` backtest | Positional index futures |
| 4 | `options_equity` | `NRML` | `15m` | Positional index options |
| 5 | `intraday_futures` | `MIS` | `3m` live / `5m` backtest | Intraday index futures |
| 6 | `intraday_options` | `MIS` | `1m` | Intraday ATM option runner |

---

## Tick-Based Entry System (`tick_entry/`)

### Problem solved

The standard polling loop fetches closed candles every 60 seconds (15 seconds for intraday options). On a large breakout candle that moves 1–3%, the entry fires at the **top of the candle close** — missing most of the move.

### How it works

After each scan cycle detects a signal, the `TickEntryManager` watches the live price for the remaining time in the cycle and fires an entry as soon as price crosses the breakout trigger level — without waiting for the next closed candle.

| Mode | Price source | Entry mechanism |
| --- | --- | --- |
| **Live** | KiteConnect WebSocket (`KiteTicker`) with REST LTP fallback | Callback fires immediately on tick crossing trigger |
| **Paper** | REST LTP poll every 3–10 seconds | Entry fires mid-cycle; fill simulated via paper `OrderResult` |
| **Backtest** | Candle OHLC check — no network | Fill price = `max(candle_open, close − ATR×0.40)` for BUY |

### Per-engine watch windows

| Engine | Watch window | Poll interval | Notes |
| --- | --- | --- | --- |
| `intraday_equity` | 52 s | 8 s | 60 s cycle |
| `intraday_options` | 11 s | 3 s | 15 s cycle — highest value here |
| `intraday_futures` | 52 s | 8 s | 60 s cycle |
| `futures_equity` | 52 s | 10 s | 60 s cycle |
| `options_equity` | 52 s | 10 s | 60 s cycle |
| `delivery_equity` | **disabled** | — | Daily candles; no intra-candle benefit |

### Trigger level

The live trigger is the last closed candle close ± (ATR × 0.10). The small buffer prevents noise entries while keeping the price threshold close to the signal level.

### Backtest tick simulation

Enable in `BacktestConfig`:

```python
config = BacktestConfig(
    engine_name="intraday_equity",   # or "intraday_options"
    tick_entry_enabled=True,         # default is False
    ...
)
```

When enabled, fill price = `max(candle_open, close − ATR×0.40)` for BUY — modelling "we entered during the signal candle at the breakout level, not at its close." The summary reports `tick_entry_fills: N`.

For `intraday_options`, the simulation uses the **option premium candle's OHLC** (not the underlying's) so the fill reflects the intra-candle option price at the ORB or momentum breakout — significantly better than the candle close on large spike days. Not available for `delivery_equity` (daily candles; no intra-candle benefit).

### WebSocket startup

In LIVE + KITE mode, `build_trading_context` automatically starts `KiteTickerManager`. If the WebSocket cannot connect within 15 seconds it logs a warning and falls back to REST LTP polling.

---

## Tick-Based Exit System (`tick_exit/`)

### Problem solved

Without intra-candle exit monitoring, a position can spike to a profitable level and crash back within one 1-minute candle. The candle-close check sees only the low close price and exits at a loss — capturing none of the intra-candle spike. Similarly, the trailing stop only ratchets at each candle close, so the spike profit is never locked in.

### How it works

`TickExitMonitor` (one instance per session) arms three WebSocket breakout callbacks per open position after each `manage_open_positions` cycle, plus a continuous per-tick callback for trailing ratcheting:

| Arm | Direction | Fires on |
| --- | --- | --- |
| Stop-loss | Against position | Price breaches SL level intra-candle |
| Trailing stop | Against position | Price breaches trailing stop level intra-candle |
| Target | With position | Price reaches target level intra-candle |
| Continuous (per-tick) | — | Every tick — ratchets trailing stop upward in real time |

When a breakout callback fires, `_fire_exit()` acquires a lock, validates `trailing_active` for TRAILING_STOP arms, places the exit order at the live tick price, records the closed trade, and persists state — all without waiting for candle close.

The continuous callback (`_on_tick_ratchet`) calls `update_trailing_stop()` on every tick. If the stop moves, it immediately re-arms the WebSocket at the new level. This means during a spike from 122 → 155 within one candle, the trailing stop ratchets in real time (e.g. 95 → 123) and fires on the way back down — exit at ~123 instead of the candle close at 80.

### Guards and fallbacks

- Only active when `execution_mode` is `LIVE` or `PAPER` and WebSocket is connected
- Only active for engines where `tick_exit_enabled=True` in `tick_entry/engine_config.py` (currently: `intraday_options` only)
- Candle-close exits in `manage_open_positions` remain the fallback if WebSocket is unavailable
- `_fire_exit()` checks `trailing_active` before firing TRAILING_STOP — rejects stale arms that predate activation
- Trailing stop is clamped to minimum `entry_price` on activation — TRAILING_STOP can never fire below breakeven (see [Trailing Stop Breakeven Clamp](#trailing-stop-breakeven-clamp))

### Backtest equivalent

In backtests, the intra-candle trailing ratchet is simulated by updating the trailing stop with candle **High** (BUY) or **Low** (SELL) before evaluating exits — modelling the favorable intra-candle price move before the crash.

---

## Trailing Stop Breakeven Clamp (`models/position.py`)

### Problem solved

The trailing stop activation distance is set to `max(trailing_distance, level1_distance × 0.8)`. With typical NIFTY option ATR of 4–7, `trailing_distance = ATR × 1.0` and `level1_distance = ATR × 0.85`, so at activation the computed trailing stop candidate equals exactly `entry_price`. Any slippage or small adverse tick after activation fires TRAILING_STOP at a net loss after charges.

### Fix

`Position.update_trailing_stop()` clamps the trailing stop candidate to at least `entry_price` (BUY) / at most `entry_price` (SELL) on every update. This guarantees the trailing stop never activates below breakeven regardless of ATR multiplier config:

- At activation (`best_price = entry + trailing_distance`): trailing stop clamped to `entry_price` — breakeven, not a loss
- As price moves further: `best_price − trailing_distance > entry_price`, clamp no longer binds — trailing stop locks in real profit

To lock in meaningful profit at activation (not just breakeven), reduce `adaptive_trailing_multiplier_normal` from `1.0` to `0.6–0.7` in `config/config.runtime.yaml` so `trailing_distance < level1_distance`.

---

## Architecture

### Key design rules

- **Backtest parity**: backtesting uses the same `scan_symbols`, `should_enter_trade`, `resolve_trade_targets`, and engine position builders as live/paper. No parallel logic.
- **Shared target resolution**: stop, target, trailing distance, and activation distance all flow through `resolve_trade_targets(...)` in `engines/common.py`.
- **Paper = tracked**: paper mode creates real in-memory positions via simulated fills; exits, trailing stops, and risk guards all run normally.
- **Tick entry is additive**: the existing closed-candle scan loop runs first; tick entry monitors only fire for candidates not yet entered.

### Module map

| Path | Purpose |
| --- | --- |
| `main.py` | Process entry point; starts FastAPI/uvicorn web server in a daemon thread; waits on `_server_exit_event` (set by Ctrl+C) |
| `backtesting.py` | Candle-replay backtester; called from web UI or directly |
| `cli/configuration.py` | `SessionConfig` builder; used by web routes to translate form payloads |
| `display.py` | Terminal display helpers: ANSI tables, banners; ANSI stripped before log-file writes |
| `logger.py` | `log_event`, `enable_web_mode`, `is_web_mode`; routes logs to both file and web ring buffer |
| `orchestration/context.py` | Wires engine, broker, data, logger, config into `TradingContext`; starts WebSocket ticker |
| `orchestration/session.py` | Main 60-second supervision loop: scan, entry, position management, tick entry, safe-mode tightening, warning engine call |
| `orchestration/signal_workflow.py` | Per-symbol scan / signal evaluation; `get_stable_signal_data` candle gating |
| `orchestration/positions.py` | Position lifecycle: exits, trailing, partial exits, square-off, trade recording |
| `engines/` | Six engine classes; each owns `get_cycle_state`, signal normalization, exit evaluation, adaptive position builders |
| `engines/common.py` | `build_position`, `resolve_trade_targets`, `update_trailing_stop`, capital limits |
| `engines/base.py` | `TradingEngine` abstract base class |
| `executor.py` | `place_order`: validation, margin check, spread check, broker submission, paper fill simulation |
| `brokers/clients.py` | `KiteBrokerClient`, `UpstoxBrokerClient` |
| `brokers/base.py` | `BrokerClient` ABC; `OrderResult`, `Quote`, `OrderRequest`, `OrderStatus` |
| `tick_entry/` | Tick-based entry: engine config, trigger levels, `TickEntryManager`, backtest simulator |
| `tick_exit/` | Tick-based exit: `TickExitMonitor` — arms SL/trail/target WebSocket breakouts, per-tick trailing ratchet, intra-candle exits for LIVE/PAPER |
| `data_providers/` | Provider plugin system: Kite, Upstox, YFinance; `MarketDataService` |
| `data_providers/kite_ticker.py` | KiteConnect WebSocket manager; `arm_breakout`, `get_ltp`, singleton helpers |
| `signal_scoring.py` | `evaluate_symbol_signal`, `rank_candidates` |
| `strategy.py` | All strategy signal functions |
| `indicators.py` | `compute_atr`, `compute_rsi`, `compute_vwap` |
| `risk_manager.py` | `position_size`, `atr_position_size`, `atr_stop_from_value`, `update_trailing_stop` |
| `config.py` | Runtime config model, defaults, `get_runtime_config()` |
| `config/config.runtime.yaml` | Local runtime overrides |
| **Web layer** | |
| `web/server.py` | FastAPI app; mounts routes; starts uvicorn in daemon thread; `_lifespan` shutdown hook |
| `web/state.py` | Thread-safe shared state singleton: `_context`, `_log_ring`, `_ws_clients`, `_active_warnings`, `_market_intel`; `snapshot()` serialises all live state for the browser; two threading events: `_stop_event` (session) and `_server_exit_event` (process) |
| `web/routes/config.py` | `POST /api/configure`, `/api/start`, `/api/stop`, `/api/reset`, `/api/backtest`, `/api/backtest/cancel`; backtest `_summarise` with regime analytics + failure analysis; 5 override endpoints |
| `web/routes/session.py` | `GET /api/status`, `/api/state`, `/api/indices` (NIFTY/SENSEX/VIX), `/api/fno-data`, `/api/warnings/history` |
| `web/core/warning_engine.py` | Advisory warning engine: 8 guard functions (VIX, IV, VWAP breadth, trend, time, risk, options, data freshness); `compute_signal_quality_score` (0–100); `_build_market_intel`; 200-entry warning ring buffer |
| `web/static/index.html` | Single-page web UI: configure form, dashboard, backtest running + results views; WebSocket event handler; all JS inline |
| `state/` | Runtime state persistence: positions, trade counts, regime cache |
| `state/trade_store/` | Trade records and order-audit trail |
| `logs/` | Session logs |
| `Results/BackTest/` | Backtest output: summary, trades CSV, equity CSV |
| `tests/unit/` | Unit test suite |

---

## Web UI & Intelligence Layer

### Server lifecycle (`main.py` + `web/server.py`)

`main.py` starts FastAPI/uvicorn in a **daemon thread**. The main thread blocks on `_server_exit_event` (set only by Ctrl+C). Session stop/reconfigure do not exit the process — the server stays alive between sessions.

Two threading events in `web/state.py`:

| Event | Set by | Purpose |
| --- | --- | --- |
| `_stop_event` | `POST /api/stop`, Ctrl+C | Signals the trading loop to exit |
| `_server_exit_event` | Ctrl+C only | Signals main thread to exit the process |

### Thread-safe state (`web/state.py`)

`TradingContext` is held in a module-level singleton behind `threading.Lock()`. `snapshot()` serialises all live state (positions, trade book, session, risk, warnings, market_intel) under the lock. WebSocket clients each get an `asyncio.Queue`; log pushes and broadcasts use `call_soon_threadsafe` to cross the thread boundary safely.

### Warning engine (`web/core/warning_engine.py`)

Called every scan cycle from `orchestration/session.py` when in web mode. All guards are **advisory only** — they never block trading.

| Guard | Triggers on |
| --- | --- |
| VIX | > 22 critical, 18–22 warning, < 12 info |
| IV expansion | > 25%/15m expansion, < −20%/15m crush, > 85th percentile |
| VWAP breadth | ≥ 70% symbols below VWAP |
| Trend | Choppy ADX + mixed signals |
| Time | Entry cutoff < 15 min, lunch zone, opening 15 min, late OTM risk |
| Risk | Consecutive losses, daily loss approaching limit |
| Options | Late expiry, high cost ratio, spread warning |
| Data freshness | Stale symbol snapshots |

Signal quality score (0–100) combines: VWAP alignment (20 pts), EMA alignment (15 pts), ADX strength (15 pts), signal score distribution (20 pts), IV stability (15 pts), VIX environment (15 pts).

### Session configuration inputs

The live and backtest configuration forms expose:
- **Capital** — total session capital; changing it auto-updates Max Capital / Trade to `capital / max_open_positions`
- **Max Open Positions** — same auto-update trigger
- **Max Capital / Trade** — editable override; auto-populated as `floor(capital / max_positions)` on every capital or max-pos change
- **Lot Mode** (intraday_options engine only) — Capital Based (qty = floor(max_capital_per_trade / entry_price)) or One Lot (fixed single contract)

### Bot override controls (`web/routes/config.py`)

Five REST endpoints + runtime state flags in `session_runtime_state`:

| Endpoint | Flag set | Effect |
| --- | --- | --- |
| `POST /api/override/pause` | `override_pause_entries = True` | Blocks new entries; existing positions unchanged |
| `POST /api/override/resume` | both flags cleared | Normal operation |
| `POST /api/override/safe_mode` | both flags + position tightening | Moves stops to breakeven, halves targets, activates trailing; auto-stops when flat |
| `POST /api/override/emergency_exit` | closes all positions + pause | Immediate market-price exit of all positions |
| `GET /api/override/status` | — | Returns `{paused, safe_mode}` |

Safe mode tightening is applied once per position in `orchestration/session.py → _apply_safe_mode_tightening()` using the `_safe_mode_tightened` per-position flag to avoid re-applying on subsequent cycles.

### Trade explainability (`why_this_trade` WS event)

After each successful entry in `orchestration/session.py`, a `why_this_trade` WebSocket event is broadcast containing: symbol, side, entry_price, qty, confidence_pct, strategy, reasons_ok (conditions met), reasons_warn (cautions noted), signal_quality (score + label + breakdown), and analytics snapshot.

### P&L fields

Every closed trade carries three P&L values:

| Field | Meaning |
| --- | --- |
| `pnl` | Gross P&L before transaction costs |
| `estimated_charges` | Brokerage + STT + exchange fees + slippage model |
| `net_pnl` | `pnl − estimated_charges` |

All trade tables in the UI (live, backtest running, backtest results) show all three. Backtest summary tiles include `total_gross_pnl`, `total_charges`, and `total_net_pnl`.

### Backtest analytics (`web/routes/config.py → _summarise`)

`_compute_regime_analytics(trades_df, capital)` — groups closed trades into 6 time-of-day sessions (Opening Rush, Pre-Noon, Lunch, Post-Lunch, Afternoon, Closing) and by exit reason. Returns win%, net P&L, avg P&L per bucket.

`_compute_failure_analysis(trades_df)` — computes: max consecutive losses/wins, worst loss cluster (P&L + length), best win run (P&L + length), worst hour, loss concentration % (% of total losses in the worst 20% of losing trades).

---

## Terminal Display (`display.py`)

All terminal output — live sessions, paper mode, and backtest — is routed through `display.py`. ANSI color codes are applied at print time and automatically stripped before any log-file write, so log files stay clean while the terminal shows a colored layout.

| Function | Used by |
| --- | --- |
| `config_summary` | Printed once at session startup showing all active parameters |
| `positions_table` | Every scan cycle; shows open positions with colored P&L |
| `ranked_candidates_table` | After each scan; shows scored candidates |
| `trade_book_table` | Session summary; shows closed trades |
| `backtest_summary` | End of each backtest run |
| `banner` / `cycle_banner` | Order signals and cycle-state transitions |

Colors are suppressed automatically when stdout is not a TTY (e.g., redirected to a file or CI environment).

---

## Engine Reference

### 1. Intraday Equity (`intraday_equity`)

- Long and short MIS intraday entries
- Strategies: `MA`, `RSI`, `VWAP`, `BREAKOUT`, `ORB`, `AUTO_ADAPTIVE`
- VWAP bias filter; breakout volume filter; reversal exit confirmation
- Entry cutoff 30 minutes before 15:15 square-off
- Adaptive position builder: classifies `SIDEWAYS` / `NORMAL` / `EXPANSION` regime from ATR, recent range, and signal score; stores adaptive stop, target levels, trailing distance, activation distance, and range volatility distance
- Quantity sized from adaptive stop distance

#### Auto-Adaptive strategy routing

| Market context | Strategy basket | Confirmations |
| --- | --- | ---: |
| `GAP_UP`/`GAP_DOWN` + `GAP_GO` | `ORB`, `VWAP`, `BREAKOUT` | 2 |
| `GAP_UP`/`GAP_DOWN` + `GAP_FILL` | `ORB`, `VWAP` | 2 |
| Gap + `SIDEWAYS` | `VWAP`, `RSI` | 2 |
| `NO_GAP` / normal | `MA`, `RSI` | configured normal count |
| `PENDING_OPEN_RANGE` | waits; no entry | — |

### 2. Delivery Equity (`delivery_equity`)

- Long-only CNC delivery positions on daily candles
- Strategies: `MA`, `RSI`, `BREAKOUT`
- Nifty 50-DMA trend guard for new long entries
- Swing-style adaptive position builder: daily ATR, recent 3–10 daily ranges × 1.2 volatility distance, 1.8% minimum stop floor
- Target is a reference level only; delivery exits rely on stop / trailing / sell-signal / time-based logic
- Time-based exit: configurable max hold days

### 3. Futures Equity (`futures_equity`)

- Positional NRML index futures
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; startup reconciliation from broker positions
- Provider forced to Kite

### 4. Options Equity (`options_equity`)

- Positional NRML index options
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Lot-size-aware sizing; startup reconciliation from broker positions
- Provider forced to Kite

### 5. Intraday Futures (`intraday_futures`)

- Intraday MIS index futures
- Strategies: `MA`, `RSI`, `BREAKOUT`, `VWAP`, `ORB`
- Entry cutoff at 15:05; forced square-off at 15:15
- Lot-size-aware sizing

### 6. Intraday Options (`intraday_options`)

- Intraday ATM single-option flow; optional two-leg bounded range pair in live/paper
- Strategies: `ATM_MOMENTUM`, `ATM_ORB`, `ATM_VWAP_REVERSION`, `ATM_MULTI`, `ATM_BREAKOUT_EXPANSION`, `ATM_IV_EXPANSION`, `ATM_TRAP_REVERSAL`
- Dynamic ATM strike selection and rolling for open long option flows
- Greeks, IV, delta, spread, open-interest, expiry, cost, and vega-crush filters
- **Entry modes** (selectable in UI and CLI):
  - `LIVE_STAGED` (default) — closed candle signal + staged breakout confirmation
  - `LIVE_TICK_CONFIRM` — closed candle signal + forming-candle preview (sub-1m) + live LTP entry
  - `LEGACY_IMMEDIATE` — legacy bypass of staged filters (not recommended for new setups)
- **FormingCandlePreview** (LIVE_TICK_CONFIRM only): synthesises partial candle from WebSocket LTP + previous close; evaluates signal before 1m bar closes; requires `forming_tick_confirm_ticks` consecutive ticks above/below threshold to confirm
- **Per-session UI overrides** (available in both web and console):
  - `max_trades_per_underlying` — daily cap per underlying (default 2; range 1–10)
  - `time_exit_minutes` — auto-exit after N minutes (default 15; 0 = disabled)
  - `forming_tick_enabled` — toggle forming-candle entry
  - `forming_tick_confirm_ticks` — confirmation ticks required (default 2; range 1–5)
- Trend-adaptive runner: premium-volatility-aware trailing; three-level partial exits
- Require-closed-signal-candle flag (`require_closed_signal_candle = True`) — makes tick entry especially valuable here

---

## Strategy Reference

### Shared equity / futures strategies

| Strategy | Min candles | First actionable | Signal rule |
| --- | ---: | ---: | --- |
| `MA` | 50 | 51 | `BUY` when MA20 > MA50; `SELL` when MA20 < MA50 |
| `RSI` | 14 | 15 | `BUY` below 30; `SELL` above 70 |
| `BREAKOUT` | 20 | 22 | `BUY` above prior 20-candle high; `SELL` below prior 20-candle low |
| `VWAP` | 1 | 6 | `BUY` above cumulative VWAP; `SELL` below |
| `ORB` | 20 | 21 | `BUY` above first-15-candle high; `SELL` below first-15-candle low |

First actionable is higher than the configured minimum because `confirm_signal()` requires both the previous and current candle windows to agree.

### Intraday-options strategies

| Strategy | Min candles | Core logic |
| --- | ---: | --- |
| `ATM_MOMENTUM` | 20 | RSI14 + VWAP + 5-candle breakout; CE on bullish, PE on bearish |
| `ATM_ORB` | 16 | First 15 session candles; CE above ORB high, PE below ORB low |
| `ATM_VWAP_REVERSION` | 20 | 6-candle prior VWAP deviation; CE/PE on re-entry through VWAP |
| `ATM_MULTI` | 20 | Aligned momentum + ORB; or VWAP reversion in low-ATR regime |
| `ATM_BREAKOUT_EXPANSION` | 45 | 45-candle compression + 30-candle breakout + volume spike + ATR expansion |
| `ATM_IV_EXPANSION` | 30 | 20-candle key level + 10-candle body average + RSI; body >= 1.8× average |
| `ATM_TRAP_REVERSAL` | 24 | 20-candle support/resistance + 3 trap candles + reversal body >= 1.5× average |

---

## Shared Trading Features

- Cost-aware trade gate: entry blocked when projected net profit after charges is negative or cost-to-profit ratio exceeds limit
- Risk-style presets: `CONSERVATIVE`, `BALANCED`, `AGGRESSIVE` (engine-aware; separate intraday and positional buckets)
- Max open positions, max capital per trade, max capital deployed
- One-trade-per-symbol-per-day control
- Daily loss limit and consecutive loss limit risk controls
- Order-rate limit guard (`max_orders_per_minute`)
- Abnormal slippage detection and automatic pause
- Spread and margin pre-flight checks before live orders
- Order audit trail and trade store under `state/trade_store/`
- Per-cycle data caching to reduce redundant API calls
- Startup position reconciliation from broker for F&O and delivery engines
- **Runtime position resync**: in LIVE sessions, `_sync_exit_only_live_positions` polls the broker every `live_broker_resync_interval_seconds` (default 60s) and automatically injects newly detected manual positions into the algo's position dict for management
- Runtime state persistence under `state/` across restarts

---

## Runtime Configuration

Defaults live in `config.py`. Override in `config.runtime.yaml`.

Key sections and fields:

| Section | Notable fields |
| --- | --- |
| `execution_safety` | `min_ranked_candidate_score`, `reversal_exit_confirmation_candles`, `intraday_equity_entry_cutoff_minutes_before_squareoff` |
| `orders` | `default_entry_order_type`, `entry_limit_price_buffer_pct`, `exit_limit_price_buffer_pct` (default 0.01 — LIMIT buffer for all exit orders; set 0 to disable), `max_live_order_notional`, `max_spread_pct`, `margin_check_enabled`, `max_orders_per_minute` |
| `session_defaults` | `exit_only_default`, `live_broker_resync_interval_seconds` (applies to both exit-only and normal LIVE sessions) |
| `risk_controls` | `daily_max_loss_pct` (overridable per session/backtest from the web UI — "Daily Max Loss %" field, blank = use this default), `consecutive_loss_limit`, `api_failure_pause_minutes`, `abnormal_slippage_pause_pct` |
| `fno` | `intraday_options_lot_mode`, `intraday_options_entry_mode` (`LIVE_STAGED`\|`LEGACY_IMMEDIATE`\|`LIVE_TICK_CONFIRM`), `intraday_options_max_hold_minutes`, `intraday_options_max_trades_per_underlying`, `intraday_options_max_entry_cost_ratio`, `intraday_options_max_spread_pct`, `intraday_options_min_open_interest`, `intraday_options_roll_trigger_pct`, `intraday_options_theta_exit_ratio` |
| `engine_defaults.<engine>` | `sleep_seconds`, `cooldown_seconds`, `data_period`, `data_interval` — all engines |
| `engine_defaults.intraday_equity` | `square_off_time`, `gap_threshold_percent`, `opening_range_candles`, `breakout_volume_multiplier`, adaptive level multipliers |
| `engine_defaults.delivery_equity` | `max_symbol_allocation`, `max_hold_days`, `nifty_trend_ma_window`, `volatility_trailing_range_multiplier`, adaptive level multipliers |
| `engine_defaults.intraday_futures` | `entry_cutoff`, `square_off_time`, `max_symbol_allocation` |
| `engine_defaults.intraday_options` | `entry_cutoff`, `square_off_time`, adaptive level multipliers, all strategy filter thresholds, runner exit config |
| `data_cache` | `per_cycle_enabled` |
| `transaction_costs` | Cost model toggle and slippage settings |

All `engine_defaults` values can be changed in `config.runtime.yaml` without touching Python code. Adaptive level multipliers (`adaptive_stop_multiplier_*`, `adaptive_target_multiplier_*`, `adaptive_trailing_multiplier_*`) are read at engine class-load time; a process restart is required after changing them.

---

## Live vs Backtest Parity

- Signal evaluation, cost gating, risk-style targets, and trailing updates share the same code in both paths
- Engine adaptive position builders (intraday equity, delivery equity, intraday options) are called in both live and backtest entry paths
- Backtest trailing updates read stored `trailing_distance` from the position — no hardcoded zero
- Intraday-equity and intraday-futures backtests enforce entry cutoff and forced square-off so MIS positions are not carried overnight
- Auto-adaptive backtests use a backtest-local regime cache; live runtime state is not mutated
- Intraday-options backtests use real option contract symbols, premium candles, and lot-sized quantities
- All exit orders use `LIMIT` type with `exit_limit_price_buffer_pct` (default 1%) buffer — Kite API does not support raw `MARKET` exits for F&O; limit prices are tick-aligned to ₹0.05 in `executor.py`
- Exit orders use the product stored in `position["order_product"]` (set at entry or reconciliation), not the engine's default — NRML positions exit as NRML, MIS positions exit as MIS
- `tick_entry_enabled=True` in `BacktestConfig` models early intra-candle fills rather than always filling at candle close; for `intraday_options` the simulation uses the option premium candle OHLC directly
- Backtest trailing stop ratchet uses candle **High** (BUY) / **Low** (SELL) before exit evaluation — simulates the intra-candle favorable move before a spike-and-crash, matching live tick-exit behaviour
- Backtests run correctly on weekends: `get_underlying_bias()` returns NEUTRAL when no live underlying data is available instead of crashing; the underlying bias filter is skipped rather than blocking all entries
- `intraday_options_lot_mode` (ONE_LOT / CAPITAL_BASED) is now user-selectable from the web UI in both live and backtest tabs; defaults to runtime_config if not set in the form

---

## Running the project

```powershell
# Start the web UI (all-in-one: live, paper, backtest)
venv\Scripts\python main.py
# then open http://localhost:8000 in your browser

# Unit tests
venv\Scripts\python -m pytest
```

`backtesting.py` can still be run directly for scripted/headless backtest workflows, but the web UI is the recommended interface.

---

## Test Status

```
tests passed, 1 pre-existing failure
```

The 1 failing test (`IntradayOptionsEngineTests.test_get_cycle_state_before_open_waits`) is a pre-existing edge-case in `engines/intraday_options.py` boundary logic unrelated to entry, exit, or risk features.

---

## Recommended Reading Order for Developers

1. This file
2. [HOW_TO_USE.md](./HOW_TO_USE.md) — operator workflow
3. [config/config.runtime.yaml](../config/config.runtime.yaml) — active knobs
4. [orchestration/context.py](./orchestration/context.py) — dependency wiring
5. [orchestration/session.py](./orchestration/session.py) — main supervision loop
6. [tick_entry/manager.py](./tick_entry/manager.py) — tick entry system
7. [tick_exit/monitor.py](./tick_exit/monitor.py) — tick exit system
8. [models/position.py](./models/position.py) — position lifecycle, trailing stop logic
9. The engine file for the engine you are working with
