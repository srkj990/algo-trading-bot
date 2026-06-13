# Bugs and Fixes

Track record of bugs found and fixes applied for audit and future reference.

---

## [2026-06-09] `truth value of a Series is ambiguous` crashes in live session loop

### Symptom
Live session crashed repeatedly with:
```
[WEB] Trading loop error: The truth value of a Series is ambiguous.
Use a.empty, a.bool(), a.item(), a.any() or a.all().
```
Traceback eventually pinned to `engines/intraday_options.py:388 get_runner_partial_exit`.
Multiple mid-session crashes meant the open position was not managed — it persisted into a PAPER-mode reconnect where the stop loss was eventually hit.

### Root Causes

**1. `get_runner_partial_exit` — `latest_candle or {}`**  
`snapshot["latest_candle"]` is `stable_option_data.iloc[-1]` — a pandas `Series`. Using `series or {}` triggers `Series.__bool__()` → `ValueError`.

**2. `warning_engine._check_blocked_signals` — `if analytics.get("iv"):`**  
`analytics["iv"]` was a pandas Series in some paths. Boolean test → same error.

**3. `warning_engine._build_market_intel` — `if analytics.get("iv"):`**  
Same issue — direct boolean test on a potentially-Series IV value.

**4. `orchestration/session.py _emit_why_trade` — `if analytics.get("delta"):`, `if analytics.get("iv") else None`**  
Direct boolean/ternary on Series values in the WS broadcast dict builder.

**5. `backtesting.py _on_trade` — `_analytics.get("iv") or 0`, `if _analytics.get("iv") else None`**  
Same pattern in the backtest WS broadcast path.

### Fixes

**`engines/intraday_options.py:get_runner_partial_exit`**
```python
_snap = snapshot if snapshot is not None else {}
latest_candle = _snap.get("latest_candle")
try:
    trigger_price = float(latest_candle["High"]) if latest_candle is not None else 0.0
except (KeyError, TypeError, ValueError):
    trigger_price = 0.0
```

**`orchestration/session.py`** — added `_safe_float`, `_safe_round`, `_safe_scalar` helpers; used them in all analytics dict construction inside `_emit_why_trade`.

**`backtesting.py`** — added `_scalar`, `_safe_iv`, `_safe_price` helpers; used them in `_on_trade` broadcast dict.

**`web/core/warning_engine.py`** — all `analytics.get()` boolean uses replaced with `isinstance(str)` guards and `try/except (TypeError, ValueError)` around all numeric field accesses.

### Files Changed
- `engines/intraday_options.py` — `get_runner_partial_exit()`
- `orchestration/session.py` — `_emit_why_trade()`, new helpers
- `backtesting.py` — `_on_trade()`, new helpers
- `web/core/warning_engine.py` — `_check_blocked_signals()`, `_build_market_intel()`

---

## [2026-06-09] `reporting is not a package` import error on session Excel export

### Symptom
```
[Report] Excel export failed (non-fatal): No module named 'reporting.session_report'; 'reporting' is not a package
ImportError: cannot import name 'summarize_by_exit_reason' from 'reporting'
```
Session Excel export failed; a subsequent fix attempt also broke the main `reporting.py` import used by `orchestration/positions.py`.

### Root Cause
The initial implementation placed `session_report.py` inside a new `reporting/` directory. Python resolves `import reporting` as the package (directory) rather than the existing module (`reporting.py` at project root). This shadowed `reporting.py` entirely — both the new export and the existing `summarize_by_exit_reason` import broke.

### Fix
Moved the Excel export module to root as `session_report.py` (uniquely named, no conflict). Changed `web/routes/config.py` import to `from session_report import export_session_excel`. Deleted `reporting/` directory.

### Files Changed
- `session_report.py` (new, at project root)
- `web/routes/config.py` — `_export_session_report()` import path

---

## [2026-06-09] PAPER mode orders not appearing in Kite broker

### Symptom
Trades appeared in the algo's trade book (with P&L) but no orders were visible in the Kite Order Book.

### Root Cause
Not a bug. The session was configured in **PAPER mode** (`Execution mode: PAPER` in session log). In paper mode, `executor.py` simulates fills locally (logs `[PAPER] Simulated fill: BUY 195 NFO:NIFTY2660923200CE @ 46.75`) and assigns a fake order ID (`PAPER-xxxxxxxxx`). No Kite API call is made.

### Resolution
Select **LIVE** execution mode in the web UI Configure tab before starting the session. Only LIVE mode sends real orders to Kite.

---

## [2026-06-04] ATM_ORB fires spurious signal at 09:15 using prior-day data

### Symptom
Backtest showed an ATM_ORB trade entry at 09:15 — the very first candle of the day. ORB is designed to wait 15 candles before triggering (earliest valid signal ≈09:30 on 1m data).

### Root Cause
`IntradayOptionsEngine.require_closed_signal_candle = True`. In `get_stable_signal_data`, at timestamp 09:15 the condition `latest_naive >= current_minute` is True → `data.iloc[:-1]` strips the 09:15 candle. The stripped slice ends at **yesterday's 15:29**. `_get_session_df(df)` then filters to the last date in that slice → returns **all 375 prior-day candles**. `strategy_orb` sees `len(session_df) = 375 > 15`, computes the opening range from yesterday's first 15 candles, and fires BUY_CE/BUY_PE using yesterday's last close vs yesterday's ORB range — labelled at today's 09:15 timestamp.

### Fix
**`orchestration/signal_workflow.py:get_stable_signal_data()`** — after stripping the forming candle, check if the strip crossed a day boundary:
```python
stripped = data.iloc[:-1]
if not stripped.empty and stripped.index[-1].date() != latest_naive.date():
    return data  # today's first candle remains; strategy min-candle checks block entry
return stripped
```

### Files Changed
- `orchestration/signal_workflow.py` — `get_stable_signal_data()`

---

## [2026-06-04] Multi-strategy mode always HOLDs for intraday_options (BUY_CE/BUY_PE not counted)

### Symptom
Selecting Multi-strategy mode for `intraday_options` produced zero trades — every signal returned HOLD regardless of how many strategies agreed.

### Root Cause
`evaluate_symbol_signal` mode `"2"` counted `strat_signal == "BUY"` and `strat_signal == "SELL"` for agreement. ATM option strategies return `execution_signal = "BUY_CE"` or `"BUY_PE"`, never plain `"BUY"` or `"SELL"`. So `buy_count` and `sell_count` were always 0 → `final_signal = "HOLD"`.

Additionally, when a valid multi-strategy `signal="BUY"` was produced but `option_signal=None` (CE/PE votes tied), `dynamic_atm_scan=True` meant the code later tried to access `contract_snapshot["strike"]` — which was never assigned → `UnboundLocalError`.

### Fix
**`signal_scoring.py:evaluate_symbol_signal()`** — count `BUY_CE` as a buy vote and `BUY_PE` as a sell vote. Track `ce_count` and `pe_count` separately to resolve the majority option type. Surface resolved `option_signal`/`option_type` in the multi-strategy result.

**`orchestration/signal_workflow.py:scan_symbols()`** — initialize `contract_snapshot = None` before the try block. Guard all `contract_snapshot[]` accesses with `contract_snapshot is not None`. Block entry when `dynamic_atm_scan=True` but `option_signal` is not BUY_CE/BUY_PE (CE/PE tie → reset signal to HOLD).

### Files Changed
- `signal_scoring.py` — `evaluate_symbol_signal()` multi-strategy aggregator
- `orchestration/signal_workflow.py` — `contract_snapshot` init + guards

---

## [2026-06-11] Multi-strategy SELL/BUY_PE votes silently discarded as "tied or missing" + late/chasing entry overhaul

### Symptom
Replaying `runners/06_intraday_options/logs/algo_20260611_090725_to_20260611_154331.log` for SENSEX
(mode "2", `ATM_TRAP_REVERSAL` + `ATM_VWAP_REVERSION`) showed repeated cycles where both strategies
agreed `BUY_PE`, but `evaluate_symbol_signal` returned `final_signal="HOLD"` with the reason
"Multi-strategy CE/PE votes tied or missing" (`orchestration/signal_workflow.py`). This stalled the
scanner for long stretches (09:55-10:07, then a 71-minute gap), contributing directly to late entries.
Separately, when a signal *did* fire, entries were taken after the move had already matured (delta
0.54-0.56, RANGE_BOUND regime, "above VWAP + score > 1.1" only).

### Root Causes

**1. Regression of the [2026-06-04 fix](#2026-06-04-multi-strategy-mode-always-holds-for-intraday_options-buy_ce-buy_pe-not-counted) above**
`OPTION_SIGNAL_TO_EXECUTION` maps both `BUY_CE` and `BUY_PE` to `execution_signal="BUY"`
(`strategy.py:9-13`). The 06-04 fix's `if strat_signal == "BUY" or opt_sig == "BUY_CE":` branch
matched first for `BUY_PE` strategies too (since `execution_signal=="BUY"` regardless), so the
`elif opt_sig == "BUY_PE"` branch was dead code. `sell_count`/`pe_count` could never be
incremented, so `final_signal` could never be `"SELL"` — a 2/2 `BUY_PE` agreement produced
`final_signal="BUY"` with `ce_count==pe_count==0`, hitting the "tied or missing" guard.

**2. Mode-"2" multi-strategy evaluations bypassed all entry-quality validators**
`evaluate_symbol_signal` mode "2" returns `"strategy": None`. `resolve_entry_profile()` fell back
to `get_entry_profile(None)` → `None`, so `apply_signal_filters` never ran
`validate_momentum_entry`/`validate_mean_reversion_entry` (freshness, breakout-distance, volume,
trend-alignment checks) — the trade was gated only by the generic VWAP-side filter and
`min_signal_score`, matching the "VWAP + score-only" entry symptom.

### Fixes

**A. `signal_scoring.py:evaluate_symbol_signal()`** — classify each strategy's vote by
`option_signal` first (`BUY_CE` → buy/CE vote, `BUY_PE` → sell/PE vote), falling back to
`execution_signal` only when `option_signal` is absent:
```python
if opt_sig == "BUY_CE":
    buy_count += 1
    ce_count += 1
elif opt_sig == "BUY_PE":
    sell_count += 1
    pe_count += 1
elif strat_signal == "BUY":
    buy_count += 1
elif strat_signal == "SELL":
    sell_count += 1
```

**B. `engines/intraday_options.py:resolve_entry_profile()`** — when `strategy_name is None` (mode
"2"), inspect `evaluation["details"]` for strategies whose `option_signal` matches the resolved
`option_signal`, map each to its `entry_profiles` value, and prefer `MOMENTUM` >
`MEAN_REVERSION` > `VOLATILITY` if any agreeing strategy maps to that profile. This routes
mode-"2" signals through the existing `validate_momentum_entry`/`validate_mean_reversion_entry`
chain.

**C. New entry-quality filters** (all gated by `entry_quality_filters_enabled`, default `true`,
to allow A/B comparison by toggling to `false`):
- **C1 — Signal freshness**: `max_signal_age_candles` (default 2). Momentum reuses the existing
  `momentum_entry_setups` staging state; mean-reversion gets a new parallel
  `mean_reversion_entry_setups` state machine tracking VWAP-retest age. Stale signals rejected
  with `rejection_code="stale_breakout"`.
- **C2 — Breakout/VWAP distance filter**: `max_breakout_distance_pct` (default **1.5%**, measured
  against the **option premium** — momentum vs `prev_high`/`prev_low`, mean-reversion vs
  `option_vwap`). Rejects extended/chasing entries with `rejection_code="extended_from_level"`.
  (Note: 0.4% was considered first but rejected almost all entries since option premiums move
  >0.4% per candle routinely; 1.5% chosen as the working default.)
- **C4 — Delta ceiling**: `max_abs_delta` (default 0.52) in `apply_signal_filters`, alongside the
  existing `min_abs_delta` floor. Rejects already-mature moves with
  `rejection_code="delta_too_high"`.
- **C5 — Volume confirmation for mean-reversion**: `volume_confirmation_multiplier` (default 1.5x
  avg volume). Rejects with `rejection_code="weak_breakout"`.
- **C6 — Range-bound protection**: `sideways_score_multiplier` (default 1.5x) raises
  `min_signal_score` when `volatility_regime == "SIDEWAYS"`. Rejects with
  `rejection_code="range_bound_rejection"`.
- **C8 — Composite entry-quality score**: new `compute_entry_quality_score()` (0-100, weighted:
  fresh breakout 25, VWAP alignment 20, volume expansion 20, RSI confirmation 10, breakout/ORB
  confirmation 15, trend alignment 10). `min_entry_quality_score` (default 70.0). Rejects with
  `rejection_code="low_entry_quality"`.
- **C9 — Diagnostic rejection codes**: `filtered["rejection_code"]` parsed from the lowercase
  prefix of `profile_reason` (e.g. `"stale_breakout: ..."` → `"stale_breakout"`), plus a new
  `filtered["entry_quality"]` summary dict (delta, regime, distance/age thresholds, rejection
  code) via `_build_entry_quality_summary()`.

**D. Dashboard plumbing** — `orchestration/signal_workflow.py` now surfaces `entry_quality` and
`rejection_code` in `symbol_snapshots`. `web/core/warning_engine.py:_check_blocked_signals()`
appends "Reason code", "Delta", and "Regime" to the blocked-signal warning detail.

**E. Backtest A/B support** — `entry_quality_filters_enabled: false` in
`config/config.runtime.yaml` reproduces pre-overhaul filter behaviour for comparison runs.
`backtesting.py._build_summary()` now also reports `profit_factor` (gross profit / gross loss).

### New config knobs (`config/config.runtime.yaml` → `engine_defaults.intraday_options`)
- `max_breakout_distance_pct: 1.5`
- `max_signal_age_candles: 2`
- `volume_confirmation_multiplier: 1.5`
- `sideways_score_multiplier: 1.5`
- `min_entry_quality_score: 70.0`
- `entry_quality_filters_enabled: true`
- `max_abs_delta: 0.52` (added alongside the existing `min_abs_delta`)

### Files Changed
- `signal_scoring.py`
- `engines/intraday_options.py`
- `config/config.runtime.yaml`
- `orchestration/signal_workflow.py`
- `web/core/warning_engine.py`
- `backtesting.py`
- `tests/unit/test_engine_workflows.py`
- `tests/unit/test_signal_scoring_evaluate.py` (new)

---

## [2026-06-04] Runner web servers serve stale local static/index.html instead of web/static/

### Symptom
All UI edits to `web/static/index.html` were invisible after restart. The strategy mode dropdown, multi-strategy checkboxes, and CE/PE display fixes never appeared despite being committed.

### Root Cause
Each runner had `runners/XX/static/index.html` (~2431 lines, untracked). Each runner's `main_*_web.py` explicitly set `_STATIC_DIR = Path(__file__).parent / "static"` pointing to the local stale copy instead of `web/static/index.html` (2710 lines, authoritative).

### Fix
1. All 6 `runners/XX/main_*_web.py` changed to: `_STATIC_DIR = Path(__file__).parent.parent.parent / "web" / "static"`
2. All 6 `runners/XX/static/` directories deleted from disk
3. `.gitignore` entry `runners/*/static/` added
4. `web/server.py` now raises `RuntimeError` if `_STATIC_DIR` doesn't exist (silent fallback removed)

### Files Changed
- All 6 `runners/XX/main_*_web.py`
- `.gitignore`
- `web/server.py`

---

## [2026-06-03] All runners use hardcoded default values instead of config.runtime.yaml

### Symptom
Every runner `.bat` starts with `cd /d "%~dp0"`, changing the CWD to the runner's subdirectory (e.g. `runners/06_intraday_options/`). All `Path("relative/path")` references in the codebase resolved relative to that subdirectory rather than the project root, meaning:
- `config/config.runtime.yaml` was never found → all runtime config values fell back to hardcoded defaults (e.g. `daily_max_loss_pct = 0.03` instead of the yaml value)
- `state/kite_cache` instrument files were written to the runner subdirectory instead of project root
- `state/` engine state (positions, trade counts) was scattered across runner subdirectories
- `state/trade_store` trade records landed in runner subdirectories

Symptom observed: after setting `daily_max_loss_pct: 0.15` in yaml, backtests still showed `Daily max loss exceeded (3.0%)` because the yaml was never loaded.

### Root Cause
`config.py:_load_runtime_overrides()` used `Path("config/config.runtime.yaml")` — a CWD-relative path. With CWD = `runners/06.../`, the file doesn't exist, so `_load_runtime_overrides()` returns `{}` and `RUNTIME_CONFIG` uses all hardcoded env-var defaults.

### Fix
All path references changed to `Path(__file__).parent`-relative (anchored to the `.py` file location, which is always the project root):
- `config.py:_load_runtime_overrides()` — candidates list now starts with `Path(__file__).parent / "config" / "config.runtime.yaml"` (CWD fallbacks retained for compatibility)
- `network_utils.py:_KITE_CACHE_DIR` — `Path(__file__).parent / "state" / "kite_cache"`
- `state_store.py:STATE_DIR` — `Path(__file__).parent / "state"`
- `config.py trade_store.base_dir` default — `Path(__file__).parent / "state" / "trade_store"`

Note: `logger.py:LOG_DIR = Path("logs")` is intentionally CWD-relative so each runner writes logs to its own `logs/` subdirectory.

### Files Changed
- `config.py` — `_load_runtime_overrides()` + `trade_store.base_dir` default
- `network_utils.py` — `_KITE_CACHE_DIR`
- `state_store.py` — `STATE_DIR`

---

## [2026-06-03] BSE:SENSEX backtest fails — instrument token not found

### Symptom
`[DATA ERROR] RuntimeError: Kite instrument token not found for NSE:SENSEX` (and previously `BSE:SENSEX`). SENSEX backtest produced no data and failed immediately.

### Root Cause
SENSEX (BSE index token `265`) and NIFTY 50 (NSE index token `256265`) do not appear in the `instruments()` API response for their respective exchanges — they are index tokens that must be hardcoded. Additionally, `_safe_spot_symbol()` in `web/routes/config.py` had a fallback `f"NSE:{base_symbol}"` that produced `NSE:SENSEX` (wrong) for SENSEX, which also has no token.

### Fix
1. `data_providers/kite_provider.py` — Pre-seed `_instrument_cache` with `_KITE_INDEX_TOKENS = {"NSE:NIFTY 50": 256265, "BSE:SENSEX": 265, "NSE:SENSEX": 265}` (alias so either prefix works).
2. `web/routes/config.py:_safe_spot_symbol()` — Replace generic `NSE:` prefix fallback with exchange-correct map: `{"SENSEX": "BSE:SENSEX", "NIFTY": "NSE:NIFTY 50", ...}`.

### Files Changed
- `data_providers/kite_provider.py` — `_KITE_INDEX_TOKENS` + pre-seeded `_instrument_cache`
- `web/routes/config.py` — `_safe_spot_symbol()` fallback map

---

## [2026-06-03] daily_max_loss_pct blocks all entries after first options SL

### Symptom
In SENSEX backtest, after the first trade hit stop-loss (PE entry at ₹320.95, 300+ qty due to CAPITAL_BASED sizing → ₹11.7k loss), every subsequent signal was blocked: `[RISK] Daily max loss exceeded (3.0%)`. The 73700 CE trade that went on to gain +154% was never placed.

### Root Cause
Two compounding issues:
1. `daily_max_loss_pct` was 3% (₹3,000 on ₹100k) — far too tight for CAPITAL_BASED lot sizing where one stop-loss can be 10%+
2. The yaml value was never loaded (see CWD path bug above), so even after editing the yaml to `0.15`, backtests still used `0.03`

### Fix
- `config/config.runtime.yaml` — raised `daily_max_loss_pct` from `0.03` → `0.15`
- `config.py` path fix (see above) — yaml now loads correctly for all runners

---

## [2026-06-03] CE/PE display shows full contract name in backtest in-progress trade table

### Symptom
The live/running trade table during backtest (the streaming `btr-trades-body` table) showed raw Kite symbol `BFO:SENSEX2660473700CE` instead of the readable `CE 73700`. The final results table and live dashboard already used `shortSym()` but `appendBtTrade()` did not.

Additionally, the `shortSym()` regex used `\d{6}` for the expiry portion but Kite weekly expiries are 5 digits (`YYMD` format: `26604` = year 26, month 6, day 04), causing `3700` to be extracted instead of `73700`.

### Fix
- `web/static/index.html:appendBtTrade()` — added `shortSym(t.symbol)` call
- `web/static/index.html:shortSym()` — fixed regex to `\d{5}` (weekly) with `\d{7}` fallback (monthly), correctly extracting `73700`, `73500` etc.

---

## [2026-06-03] Partial and forced-exit orders use MARKET type, reject NRML product, ignore tick rounding

### Symptom
Partial exits (runner level exits), forced square-offs, and candle-close managed exits all used `MARKET` order type and hardcoded `engine.order_product` (MIS) regardless of the actual position product. For F&O NRML positions this caused "Wrong product type" rejections. On NSE F&O, MARKET orders also rejected with "Market orders without market protection not allowed via API".

Additionally, limit prices computed as `ltp × buffer` produced non-₹0.05-aligned floats, causing "Order price is not a multiple of tick size" rejections.

### Fix
**`orchestration/positions.py`** — All three position-exit paths (`execute_partial_position_exit`, `close_position_symbols`, `force_square_off_positions`, `manage_open_positions`) now:
- Use LIMIT order with `exit_limit_price_buffer_pct` buffer when that config is > 0
- Use `position.get("order_product") or engine.order_product` for product (respects NRML)

**`executor.py`** — LIMIT price rounding to nearest ₹0.05 applied at order submission (not just in the tick-exit path).

### Files Changed
- `orchestration/positions.py` — `exit_limit_price_buffer_pct` parameter added to all exit functions
- `executor.py` — LIMIT price auto-rounded to nearest ₹0.05 tick

---

## [2026-06-03] MA_LONG strategy added to delivery_equity (MA50 vs MA200 crossover)

### Description
New strategy `MA_LONG` added for `delivery_equity`. Uses MA50 / MA200 crossover — appropriate for multi-week delivery positions where short MA20/MA50 crossover is too noisy. The existing `MA` strategy (MA20/MA50) remains available.

### Changes
- `strategy.py` — `ma_long_strategy()` function; wired in `_evaluate_legacy_signal()`
- `signal_scoring.py` — score function for `MA_LONG`
- `engines/delivery_equity.py` — `supported_strategies` updated to include `MA_LONG` as strategy `"2"` (existing strategies renumbered)

---

## [2026-06-03] FormingCandlePreview — sub-1m entry via forming candle + tick LTP

### Description
New module `tick_entry/forming_candle.py`. Synthesises a partial forming candle from live WebSocket LTP and appends it to closed-candle history, then runs the engine's signal scanner with `require_closed_signal_candle=False`. Returns `FORMING_TICK` candidates if the LTP has crossed the ORB/momentum threshold and held for at least `confirm_ticks` consecutive ticks.

Activation: `intraday_options` engine, `forming_tick_enabled=True`, KiteTickerManager connected. LIVE and PAPER only — not used in backtest.

### Files Changed
- `tick_entry/forming_candle.py` — new module (FormingCandlePreview class)

---

## [2026-06-03] Trail-Only Exit — target activates trailing instead of closing position

### Symptom
In all engines, when price hit the fixed `target` level the position was closed immediately. Any further move beyond the target was lost. For strong trending moves, this cut off significant upside — position closed exactly at target while price continued to run.

Example: entry 100, target 110, trailing distance 4. Price runs to 115. Old behavior: exits at 110, misses ₹5 further move. New behavior: trails from 115 with trailing stop at 111, exits at 111 on reversal.

### Root Cause (design limitation, not a bug)
The original `evaluate_exit()` in `models/position.py` returned `TARGET` whenever `latest_high >= target`. There was no mechanism to convert a target hit into a trailing-stop activation.

### Fix
**`models/position.py`** — `update_trailing_stop()` gains `exit_mode: str = "TRAIL_ONLY"` parameter. When `exit_mode == "TRAIL_ONLY"` and `latest_close >= target` (BUY) or `<= target` (SELL), the method:
1. Sets `trailing_active = True`
2. Replaces `target` with a sentinel (`1e9` for BUY, `0.01` for SELL) — unreachable by any real price
3. Bypasses the `trailing_activation_distance` guard if trailing was already activated by target-cross

`evaluate_exit()` is unchanged — the sentinel values `1e9` / `0.01` are never reached so TARGET exit never fires.

**`engines/common.py`** — module-level `update_trailing_stop()` wrapper passes `exit_mode=position.get("exit_mode", "TRAIL_ONLY")` to the typed model.

**`tick_exit/monitor.py`** — `_arm_position()` skips arming a TARGET WebSocket breakout callback when `target >= 1e8` (sentinel guard).

**`engines/intraday_options.py`** — `apply_runner_partial_exit()` at level 2: instead of setting `target = runner_level3_target`, now sets sentinel (`1e9` BUY / `0.01` SELL`) and forces `trailing_active = True`. `runner_level3_target` is preserved in the position dict for logging.

**`config.py`** + **`config/config.runtime.yaml`** — New `execution_safety.exit_mode` config field. Default: `"TRAIL_ONLY"`. Change to `"HARD_TARGET"` to restore old immediate-exit behavior.

**`cli/configuration.py`** + **`web/static/index.html`** + **`web/routes/config.py`** — Exit Mode selector added to both live and backtest configure forms. Default pre-populated from runtime config.

**`backtesting.py`** + **`orchestration/session.py`** — `exit_mode` stored in `position["exit_mode"]` at entry so it survives restarts and is accessible from any exit path.

### Files Changed
- `models/position.py` — `update_trailing_stop()`: exit_mode parameter + target-cross sentinel logic
- `engines/common.py` — pass exit_mode through wrapper
- `tick_exit/monitor.py` — sentinel guard on TARGET arm
- `engines/intraday_options.py` — runner level2 uses sentinel instead of hard level3 target
- `config.py` — `ExecutionSafetyConfig.exit_mode` field + validation
- `config/config.runtime.yaml` — `execution_safety.exit_mode: "TRAIL_ONLY"`
- `cli/configuration.py` — `exit_mode` in `SessionConfig` + validation + `build_session_config_from_dict`
- `backtesting.py` — `BacktestConfig.exit_mode` + wired into all position builders
- `orchestration/session.py` — `exit_mode` in `position_extra_fields` at entry
- `web/static/index.html` — Exit Mode dropdowns on both Live and Backtest tabs
- `web/routes/config.py` — parse `exit_mode` from backtest form payload
- `tests/unit/test_foundations.py` — 6 new tests covering trail-only and hard-target behavior
- `tests/unit/test_engine_workflows.py` — updated runner level2 assertion to expect sentinel

---

## [2026-06-01] Backtest produces 0 trades after entry mode default changed to LIVE_STAGED

### Symptom
After changing `intraday_options_entry_mode` default from `LEGACY_IMMEDIATE` to `LIVE_STAGED` in `config.runtime.yaml`, backtest produced 0 trades across an entire 5d/1m ATM_MULTI run. Log showed `Trades=0 | WinRate=0% | P&L=₹0` at every checkpoint from candle 1 through 750.

### Root Cause
`backtesting.py:_build_engine_helper()` mapped any non-`LEGACY_IMMEDIATE` entry mode to `momentum_entry_mode = "STAGED"` on the engine instance. The `STAGED` momentum entry pipeline requires 3 consecutive candles (breakout → confirmation → pullback) to complete, plus live WebSocket tick-based staging signals. In backtest, the **pullback detection** (`pullback_ready`) requires price to pull back near EMA/VWAP after the breakout — a market condition that rarely triggers in NIFTY 1m data within the 3-candle window. Combined with the `SIDEWAYS` regime blocker and range filters, the STAGED path effectively never produced a valid entry.

### Fix
**`backtesting.py:_build_engine_helper()`** — Always set `engine.momentum_entry_mode = "LEGACY_RAW"` in backtest regardless of the session's `intraday_options_entry_mode` config. The `LEGACY_RAW` path bypasses staged confirmation filters (delta/IV/VWAP checks still run) and directly evaluates breakout signals on closed candles — the correct behaviour for backtesting where there is no live tick data.

The `intraday_options_entry_mode` setting still controls live session behavior (LIVE_STAGED → STAGED pipeline, LEGACY_IMMEDIATE → LEGACY_RAW) but is now ignored for backtest engine setup.

### Files Changed
- `backtesting.py`: `_build_engine_helper()` — removed conditional `"STAGED"` branch, always uses `"LEGACY_RAW"`

---

## [2026-06-01] Backtest overtrading + timing improvements (Phase 1–5)

### Symptom
Backtest results showed TRAILING_STOP exits firing within 1–3 candles of entry with near-zero gross P&L, resulting in net losses after transaction charges (~₹29–52 per trade).

Example trades (ATM_MOMENTUM, May 27 2026):
| Symbol | Entry | Exit | Gross P&L | Net P&L |
|--------|-------|------|-----------|---------|
| 23900CE | 179.45 | 179.65 | +13 | **-20** |
| 23900CE | 186.6 | 184.8 | -117 | **-152** |
| 23950CE | 184.15 | 184.35 | +13 | **-21** |
| 23950PE | 152.9 | 152.55 | -23 | **-51** |
| 23900PE | 140.0 | 140.30 | +39 | **-14** |

### Root Cause

**Bug 1 — `models/position.py:update_trailing_stop()`**

The trailing activation distance was set to `max(trailing_distance, level1_distance * 0.8)`. With typical NIFTY options ATR (~4–7), `trailing_distance = ATR * 1.0` and `level1_distance = ATR * 0.85`, so `trailing_activation_distance = ATR * 1.0`. When trailing activates at `best_price = entry + ATR`, the computed candidate is `(entry + ATR) - ATR = entry` — exactly breakeven. Any slippage or small adverse tick → TRAILING_STOP fires at a net loss.

The trailing stop candidate was never clamped to be above the entry price, so activation placed the trailing stop at or below breakeven.

**Bug 2 — `tick_exit/monitor.py:_fire_exit()`**

The TRAILING_STOP staleness check did not verify `trailing_active`. A WebSocket breakout arm that fired before `trailing_active=True` was confirmed could bypass the activation guard.

### Fix

**`models/position.py` — `update_trailing_stop()`**
Added breakeven clamp after computing the trailing stop candidate:
```python
# BUY side:
candidate = max(candidate, self.entry_price)   # never below breakeven
# SELL side:
candidate = min(candidate, self.entry_price)   # never above breakeven
```
This guarantees the trailing stop never activates at a level that produces a loss exit, regardless of ATR multiplier config. As price continues to move favorably beyond activation, the trailing stop ratchets above entry to lock in real profit.

**`tick_exit/monitor.py` — `_fire_exit()`**
Added `trailing_active` guard before the TRAILING_STOP exit path:
```python
if not position.get("trailing_active"):
    return  # trailing not yet activated — reject stale arm
```

### Files Changed
- `models/position.py`
- `tick_exit/monitor.py`

### Commit
`fix: clamp trailing stop to breakeven on activation; guard tick-exit trailing_active`

---

## [2026-05-31] Backtest crashes on Sunday — `get_underlying_bias()` live API call

### Symptom
Running a 5-day backtest on Sunday would crash with `RuntimeError: No underlying data for NIFTY`. Zero trades would be produced despite valid pre-fetched historical data.

### Root Cause
`engines/intraday_options.py:get_underlying_bias()` made a live Kite API call (`get_data(period="2d", interval="1m", provider="KITE")`) when `underlying_df=None`. On Sunday, markets are closed and Kite returns 0 rows for this query, causing the RuntimeError.

Additionally, `apply_signal_filters()` called `get_underlying_bias()` unconditionally when `prefetched_underlying_df=None` (non-ATM-scan path), even during backtests.

### Fix

**`engines/intraday_options.py` — `get_underlying_bias()`**
Removed the live API call. Now returns `{"bias": "NEUTRAL", ...}` immediately when `underlying_df` is None or empty, instead of fetching live data:
```python
def get_underlying_bias(self, underlying, underlying_df=None):
    if underlying_df is None or underlying_df.empty:
        return {"bias": "NEUTRAL", "close": 0.0, "vwap": 0.0, "ema": 0.0}
```

**`engines/intraday_options.py` — `apply_signal_filters()`**
Added `prefetched_underlying_df is not None` guard before calling `get_underlying_bias()`, so the underlying bias filter is skipped entirely when no data is available (rather than applying a blocking NEUTRAL that would reject all entries):
```python
if analytics and not analytics.get("skip_underlying_bias") and prefetched_underlying_df is not None:
```

### Files Changed
- `engines/intraday_options.py`

### Commit
`fix: get_underlying_bias returns NEUTRAL when no data available; skip bias filter when prefetched_underlying_df is None`

---

## [2026-05-31] Intra-candle trailing stop ratchet — backtest used candle Close instead of High/Low

### Symptom
In backtests, if a candle had a high spike followed by a crash (e.g. Open=125, High=155, Low=80, Close=82), the trailing stop was only ratcheted using the Close price (82), not the High (155). This meant the trailing stop never captured the spike, and the backtest exit price was worse than what live trading would produce.

### Root Cause
`backtesting.py:_manage_intraday_options_position()` called `update_trailing_stop(position, float(latest_close), ...)`, only ratcheting with the candle close. The candle High was never used.

### Fix
Changed to ratchet the trailing stop with candle **High** (BUY) or **Low** (SELL) before evaluating exits, simulating intra-candle price movement:
```python
if position_side(position) == "BUY":
    update_trailing_stop(position, float(latest_candle.get("High", latest_close)), trailing_distance)
else:
    update_trailing_stop(position, float(latest_candle.get("Low", latest_close)), trailing_distance)
```

### Files Changed
- `backtesting.py`

### Commit
`feat: intra-candle WebSocket exit monitoring + trailing ratchet` (commit b597ad5)

---

## [2026-05-31] ATM_MULTI loss optimisation — config tuning

### Symptom
Backtest `backtest_intraday_options_20260530_220205` (ATM_MULTI, AGGRESSIVE, 5 days, ₹1L):
- 34 trades, 41% win rate, Net P&L = −₹9,520 (−9.5%), Max DD = 12.05%
- 26/34 exits (76%) via STOP_LOSS — all net losses, avg −₹710 each
- BUY_CE entries: 12.5% win rate (8 trades, 1 winner) on a non-trending day
- Trailing stop activating at exactly breakeven → TRAILING_STOP exits with net losses

### Root Cause
1. Stop multiplier ATR×1.7 too tight — hit by 1-candle normal volatility, not reversals
2. R:R was 1:1 (stop = target = ATR×1.7) — insufficient for 41% win rate
3. Trailing distance ATR×1.0 = activation threshold → trailing stop lands at entry (breakeven) even with the new breakeven clamp; needs to be narrower than activation to lock in profit
4. EXPANSION regime threshold (1.1%) too loose — fired CE entries on May 26 (descending day)
5. `consecutive_loss_limit=4` allowed 4-loss streaks costing ₹2,803

### Fix
Config-only changes in `config/config.runtime.yaml`:

| Parameter | Before | After |
|-----------|--------|-------|
| `adaptive_stop_multiplier_normal` | 1.7 | 2.2 |
| `adaptive_stop_multiplier_sideways` | 2.0 | 2.5 |
| `adaptive_stop_multiplier_expansion` | 1.7 | 2.0 |
| `adaptive_target_multiplier_normal` | 1.7 | 2.5 |
| `adaptive_target_multiplier_sideways` | 1.1 | 1.5 |
| `adaptive_target_multiplier_expansion` | 2.3 | 3.0 |
| `adaptive_trailing_multiplier_normal` | 1.0 | 0.7 |
| `adaptive_trailing_multiplier_sideways` | 0.8 | 0.6 |
| `adaptive_trailing_multiplier_expansion` | 1.15 | 0.8 |
| `adaptive_min_stop_pct` | 0.05 | 0.07 |
| `consecutive_loss_limit` | 4 | 3 |
| `intraday_options_max_entry_cost_ratio` | 0.30 | 0.20 |
| `intraday_options_regime_expansion_range_pct` | 1.1 | 1.4 |

Key effect: tighter trailing (ATR×0.7) vs activation (ATR×1.0) means trailing stop activates at entry+0.3×ATR profit instead of breakeven.

### Files Changed
- `config/config.runtime.yaml`

### Commit
`config: widen stops, raise targets, tighten trailing for intraday_options loss reduction` (commit c5d1fff)

---

## [2026-06-02] Runtime broker resync misses manually opened positions

### Symptom
User opens a position manually in Kite (e.g. NIFTY 2nd Jun 23300 PE, qty=130, NRML, avg ₹29.65) while the bot is already running a live session. Bot never detects or manages the position — it is invisible to the algo until the session is restarted.

```
# Log shows only algo-entered positions; manually opened position absent:
[SESSION] Open positions: {}
[RECON] Loaded 0 live intraday options positions from broker
```

### Root Cause
`orchestration/session.py:_sync_exit_only_live_positions()` already had periodic broker resync logic (calls `engine.reconcile_startup()` every `live_broker_resync_interval_seconds`) but was gated by:
```python
if not bool(getattr(context.config, "exit_only_mode", False)):
    return False   # ← blocked all normal LIVE sessions
```
Normal sessions (non-exit-only) never ran the resync loop.

### Fix
**`orchestration/session.py` — `_sync_exit_only_live_positions()`**

Removed the `exit_only_mode` gate. The function now runs in both exit-only and normal LIVE sessions, but behaves differently:

- **Exit-only mode**: mirrors broker state exactly — adds new and removes broker-flat positions (unchanged)
- **Normal mode**: only injects newly detected manual positions; never removes algo-managed positions to avoid disturbing positions mid-fill or intentionally held by the algo

```python
exit_only = bool(getattr(context.config, "exit_only_mode", False))
# ... resync_interval throttle check ...
added = sorted(set(reconciled_positions) - set(existing_positions))
removed = sorted(set(existing_positions) - set(reconciled_positions))

if exit_only:
    # mirror broker state exactly
    context.positions = reconciled_positions
else:
    # normal mode: only add newly detected manual positions
    for symbol in added:
        context.positions[symbol] = reconciled_positions[symbol]
        context.log_event(
            f"[RECON] Runtime sync detected manual broker position: {symbol} — added for management"
        )
```

After the fix, a manually opened NRML position is detected within `live_broker_resync_interval_seconds` (default 60s) and added to the algo's position tracking. The bot then manages its SL, trailing stop, and target exactly like an algo-entered position.

### Input Example
1. Bot session running in LIVE mode (intraday_options engine)
2. User manually opens NIFTY2660223300PE in Kite: qty=130, NRML, avg ₹29.65
3. Within 60 seconds: `[RECON] Runtime sync detected manual broker position: NFO:NIFTY2660223300PE — added for management`
4. Bot immediately arms tick-exit SL/target breakout callbacks and manages the position

### Files Changed
- `orchestration/session.py`: `_sync_exit_only_live_positions()` — removed `exit_only_mode` gate; split add-only vs mirror behavior
- `config/config.runtime.yaml`: updated comment on `live_broker_resync_interval_seconds` to reflect it now covers both normal and exit-only sessions

---

## [2026-06-02] Exit orders use MARKET type — rejected by Kite API for F&O

### Symptom
All exit orders (STOP_LOSS, TARGET, TRAILING_STOP, TICK_EXIT) were placed as `order_type=MARKET`. For F&O instruments, Kite rejects raw MARKET orders via the API:

```
[WEB] Trading loop error: Market orders without market protection are not allowed via API.
```

### Root Cause
`orchestration/positions.py` and `tick_exit/monitor.py` always passed `order_type="MARKET"` and `price=None` to `place_order()`. Kite API requires either `LIMIT` with a price, or market protection (not supported via the standard API path).

### Fix
Added `exit_limit_price_buffer_pct` config field (default `0.01` = 1%). All exit call sites now build a LIMIT order when this is set:

```python
if exit_limit_price_buffer_pct > 0:
    _order_type = "LIMIT"
    _buf = float(exit_price) * exit_limit_price_buffer_pct
    # SELL exit: price slightly below LTP to get filled quickly
    _limit_price = max(0.01, float(exit_price) - _buf) if _exit_side == "SELL" else float(exit_price) + _buf
```

For a SELL exit at LTP ₹50 with 1% buffer: limit price = ₹49.50 (filled at market depth immediately). For a BUY exit: ₹50.50.

### Input Example
- Position: NIFTY 23350 CE, BUY, qty=75
- LTP at SL breach: ₹39.05
- Old: `place_order(SELL, 75, order_type="MARKET")` → Kite rejects
- New: `place_order(SELL, 75, order_type="LIMIT", price=38.66)` → accepted and filled

### Files Changed
- `config.py`: added `exit_limit_price_buffer_pct: float` to `OrderValidationConfig`; default `0.01` from env `ORDER_EXIT_LIMIT_PRICE_BUFFER_PCT`
- `config/config.runtime.yaml`: added `exit_limit_price_buffer_pct: 0.01`
- `orchestration/positions.py`: all 5 exit `place_order` calls updated
- `orchestration/session.py`: passes `exit_limit_price_buffer_pct` to `manage_open_positions` and `force_square_off_positions`
- `tick_exit/monitor.py`: exit order in `_fire_exit()` updated

---

## [2026-06-02] Exit orders use engine product (MIS) instead of position product (NRML)

### Symptom
Manually reconciled NRML options positions were being exited with `product="MIS"`. Kite rejects MIS exit orders for NRML positions:

```
[EXECUTION] Note: Exit BUY via TRAILING_STOP
it places mis order and got rejected, because my order is nrml
```

### Root Cause
All exit `place_order` calls in `orchestration/positions.py` and `tick_exit/monitor.py` used `product=engine.order_product` (hardcoded to `"MIS"` for `intraday_options`). The `position["order_product"]` field, which stores the broker's actual product (`NRML` for reconciled positions), was ignored.

### Fix
All exit call sites changed from `product=engine.order_product` to `product=(position.get("order_product") or engine.order_product)`. The `order_product` field is stored in the position dict by `build_trend_adaptive_position` and also by `_build_reconciled_live_position` (which now reads it from the broker response).

```python
# Before:
product=engine.order_product,          # always "MIS"

# After:
product=(position.get("order_product") or engine.order_product),  # "NRML" when reconciled
```

`engines/intraday_options.py:_build_reconciled_live_position()` also updated to store the broker's actual product:
```python
order_product=(item.get("product") or self.order_product).upper(),
```

### Input Example
- Broker position: NIFTY 23300 PE, NRML, reconciled at startup
- Old: exit placed as `SELL 130 MIS` → rejected (position is NRML)
- New: position dict has `order_product="NRML"`; exit placed as `SELL 130 NRML` → accepted

### Files Changed
- `orchestration/positions.py`: `product=` in all 5 exit paths changed to `(position.get("order_product") or engine.order_product)` (replace_all)
- `tick_exit/monitor.py`: `_fire_exit()` exit `place_order` updated
- `engines/intraday_options.py`: `_build_reconciled_live_position()` updated to store broker product

---

## [2026-06-02] Exit limit price not tick-aligned — Kite rejects "price not multiple of tick size"

### Symptom
LIMIT exit orders were placed with prices like ₹43.31 or ₹38.66. NSE F&O tick size is ₹0.05. Kite rejects prices not aligned to the tick grid:

```
Order price is not a multiple of tick size
```

Screenshot showed: LIMIT, MIS, price ₹43.31 (not a multiple of 0.05).

### Root Cause
Exit limit prices were computed as `LTP × (1 ± buffer_pct)` which produces arbitrary floats. No tick-size rounding was applied anywhere in the order placement path.

### Fix
**`executor.py`** — added tick rounding for all LIMIT orders before submission:

```python
if normalized_order_type == "LIMIT" and resolved_price is not None:
    tick = 0.05
    resolved_price = round(round(float(resolved_price) / tick) * tick, 2)
```

This applies to both entry and exit LIMIT orders, for all instruments.

### Input Example
- LTP at SL breach: ₹43.75; buffer 1%; raw limit price = ₹43.75 × 0.99 = ₹43.3125
- Old: ₹43.31 → rejected
- New: round(43.3125 / 0.05) × 0.05 = round(866.25) × 0.05 = 866 × 0.05 = ₹43.30 → accepted

### Files Changed
- `executor.py`: tick-size rounding added for LIMIT orders

---

## [2026-06-02] Intraday options reconciliation misses NRML positions at startup

### Symptom
User has a manually opened NRML intraday option position. After restarting the session, the bot logs:

```
[RECON] Loaded 0 live intraday options positions from broker
```

The NRML position is invisible despite being live on the broker.

### Root Cause
`engines/intraday_options.py:reconcile_startup()` called `get_options_positions(product="MIS")` — filtering to MIS only. NRML positions are returned by the broker but were silently discarded.

### Fix
Changed to `get_options_positions(product=None)` so all open options positions (MIS and NRML) are included in startup reconciliation.

### Input Example
- Broker: NIFTY 2nd Jun 23300 PE, qty=130, product=NRML
- Old: `get_options_positions(product="MIS")` → position not returned → 0 loaded
- New: `get_options_positions(product=None)` → position included → reconciled and managed

### Files Changed
- `engines/intraday_options.py`: `reconcile_startup()` — `product="MIS"` → `product=None`

---

## [2026-06-02] Delivery equity backtest produces 0 trades — midnight timestamp blocks market hours check

### Symptom
5-year delivery equity backtest with yfinance 1d data: 0 trades across 496 candles. Log showed `allow_scan=False` for every candle from the very first one.

### Root Cause
yfinance daily candles have `time(0, 0)` (midnight) as their timestamp. `engines/delivery_equity.py:get_cycle_state()` had a market-hours guard:

```python
if current_time < self.market_open or current_time >= self.market_close:
    return {"allow_scan": False, ...}
```

`time(0, 0)` is before `market_open` (09:15), so every daily candle was blocked as "market closed".

### Fix
Added a `is_daily_bar` detection: any candle with `time(0, 0)` is treated as a valid daily bar and skips the market-hours guard entirely.

```python
is_daily_bar = current_time == time(0, 0)
if not is_daily_bar and (current_time < self.market_open or current_time >= self.market_close):
    return {"allow_scan": False, "reason": "Market closed for delivery execution"}
```

### Input Example
- yfinance 1d bar: `2024-05-15 00:00:00` → `current_time = time(0, 0)`
- Old: `time(0,0) < time(9,15)` → `allow_scan=False` → 0 trades across full 5-year backtest
- New: `is_daily_bar=True` → guard skipped → scan proceeds → trades generated

### Files Changed
- `engines/delivery_equity.py`: `get_cycle_state()` — added `is_daily_bar` check

---

## [2026-06-02] Intraday options runner — `.env` not found when runner sets CWD to subfolder

### Symptom
Running `runners/06_intraday_options/main.py` (which sets `CWD` to its own folder via `cd /d "%~dp0"`) causes a crash at startup:

```
Missing required environment variable. Checked: KITE_API_KEY, ZERODHA_API_KEY
```

`.env` exists in the project root but the runner's CWD is `runners/06_intraday_options/`.

### Root Cause
`config.py:_load_dotenv()` opened `.env` relative to the process CWD (not relative to `config.py`'s location). When the runner changes CWD to its own subfolder, `".env"` resolves to `runners/06_intraday_options/.env` which does not exist.

### Fix
`_load_dotenv()` now also tries to find `.env` relative to `config.py`'s own directory (`__file__`), regardless of the process CWD:

```python
def _load_dotenv(path: str = ".env") -> None:
    resolved = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    candidates = [path, resolved]
    for candidate in candidates:
        if os.path.exists(candidate):
            # load env vars ...
            return
```

CWD-relative path is tried first (for backward compatibility). If not found, falls back to `config.py`-directory-relative path.

### Input Example
- CWD: `C:\WorkSpace_Siva\Zerodha\zerodha-alago\runners\06_intraday_options\`
- `config.py` location: `C:\WorkSpace_Siva\Zerodha\zerodha-alago\config.py`
- Old: looks for `runners/06_intraday_options/.env` → not found → crash
- New: fallback looks for `C:\WorkSpace_Siva\Zerodha\zerodha-alago\.env` → found → loads

### Files Changed
- `config.py`: `_load_dotenv()` — try CWD path first, then `__file__`-relative path

---

## [2026-06-02] Public IP not shown after Kite auth — must manually check for IP allowlist

### Symptom
After successfully generating a Kite access token via `auto_auth.py`, the user did not know their public IP to add to the Kite developer console's Allowed IPs list. They had to manually visit `https://api.ipify.org` or run PowerShell to find it.

### Fix
Added `_print_public_ip()` called automatically after a successful token refresh:

```python
def _print_public_ip():
    public_ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode()
    print(f"\nYour public IP : {public_ip}")
    print("Add this IP to Kite developer console → App Settings → Allowed IPs if not already added.")
```

### Files Changed
- `auto_auth.py`: added `_print_public_ip()` function; called from `main()` after successful auth

---

## [2026-06-01] UI: lot mode selector + auto capital-per-trade

### Symptom
`intraday_options_lot_mode` was only settable by editing `config.runtime.yaml`. Users starting sessions from the web UI had no way to switch between ONE_LOT and CAPITAL_BASED sizing. Also, `max_capital_per_trade` field showed a static ₹50,000 default that did not update when capital or max_open_positions changed.

### Fix
- Added Lot Mode dropdown to web UI for intraday_options engine (live and backtest tabs), shown only when engine = Intraday Options
- Added `oninput="autoCapPerTrade(pfx)"` to capital and max-pos inputs — auto-computes `max_capital_per_trade = floor(capital / max_open_positions)` on each keystroke
- `cli/configuration.py` and `web/routes/config.py`: prefer `intraday_options_lot_mode` from the form payload over `runtime_config.fno` default

### Files Changed
- `web/static/index.html`
- `cli/configuration.py`
- `web/routes/config.py`

### Commit
`feat: add lot mode selector and auto capital-per-trade to UI` (commit 9e05110)

---

## [2026-05-31] Zero trades on small capital — max_symbol_allocation blocks intraday_options

### Symptom
Backtest `backtest_intraday_options_20260531_203440` with capital=₹20,000: **0 trades executed** despite valid signals.

### Root Cause
`engines/intraday_options.py:apply_entry_allocation_limit()` called `super()` which applies `max_symbol_allocation: 0.2` (20% of capital cap per symbol) **on top of** the already-capital-bounded quantity.

Execution chain for ₹20,000 capital, entry=₹210:
1. `qty = int(20000 / 210) = 95` (capital-bounded)
2. `super().apply_entry_allocation_limit()`: `max_symbol_capital = 20000 × 0.2 = ₹4,000`
3. `symbol_cap_qty = int(4000 / 210) = 19`
4. `capped = min(95, 19) = 19`
5. Lot rounding: `(19 // 75) * 75 = 0` → **trade skipped**

Blocking condition: `entry_price > capital × max_symbol_allocation / lot_size` → `entry_price > 20000 × 0.2 / 75 = ₹53.33` — triggered by every normal premium entry.

### Fix
`engines/intraday_options.py:apply_entry_allocation_limit()` — removed `super()` call. Capital is already bounded by `max_capital_per_trade` before this method is called; the `max_symbol_allocation` cap is redundant and harmful for lot-based contracts. Now only lot-size rounding is applied:
```python
lot_size = get_contract_lot_size(symbol)
return (quantity // lot_size) * lot_size
```

Minimum capital to trade = `lot_size × entry_price` (e.g. ₹75 × ₹200 = ₹15,000 for one NIFTY lot at ₹200 premium).

### Files Changed
- `engines/intraday_options.py`

### Commit
`fix: remove max_symbol_allocation cap from intraday_options apply_entry_allocation_limit` (commit 09999ae)

---

## [2026-05-31] consecutive_loss_limit mismatch — config.py default stale after yaml change

### Symptom
`config/config.runtime.yaml` was updated to `consecutive_loss_limit: 3` (from 4) as part of the loss-optimisation change, but `config.py` still had `os.getenv("RISK_CONSECUTIVE_LOSS_LIMIT", "4")` as the fallback default. Any deployment using the env-var path (no yaml file present, or yaml not loaded) would silently use 4 instead of 3.

### Root Cause
Two sources of truth for the same default: `config/config.runtime.yaml` (authoritative for running sessions) and `config.py` `_default_runtime_config_map()` (used as base before yaml merges). When the yaml was updated, the code default was not kept in sync.

### Fix
Updated `config.py` env default from `"4"` to `"3"`:
```python
"consecutive_loss_limit": int(os.getenv("RISK_CONSECUTIVE_LOSS_LIMIT", "3")),
```

### Files Changed
- `config.py`

### Commit
`fix: sync consecutive_loss_limit default in config.py from 4 to 3` (commit d7c224e)

---

## [2026-05-31] Backtest ignores consecutive_loss_limit and daily_max_loss_pct

### Symptom
May 27 backtest data shows 4 consecutive STOP_LOSS trades after an earlier TRAILING_STOP loss:

| Entry | Exit | Reason | Net P&L | Consec losses at entry |
|-------|------|--------|---------|----------------------|
| 10:18 | 10:38 | TRAILING_STOP | −₹16 | 1 |
| 10:38 | 10:40 | STOP_LOSS | −₹629 | 2 |
| 10:56 | 11:00 | STOP_LOSS | −₹674 | **3 → limit** |
| 11:07 | 11:11 | STOP_LOSS | −₹767 | should be blocked |
| 11:19 | 11:36 | STOP_LOSS | −₹632 | should be blocked |
| 14:37 | 14:38 | STOP_LOSS | −₹967 | should be blocked |

With `consecutive_loss_limit=3`, trades 4-6 should have been blocked. The live session would have stopped at trade 3. The backtest produced −₹2,366 in avoidable losses.

### Root Cause
`backtesting.py:_enter_ranked_candidates()` had no `consecutive_loss_limit` or `daily_max_loss_pct` check. The live session check in `orchestration/session.py` (line 732) was never mirrored in the backtest, causing backtest P&L to diverge from live trading behaviour.

### Fix
Added `_consecutive_losses_today()` helper and two early-return guards at the top of `_enter_ranked_candidates()` in `backtesting.py`:
```python
# Consecutive loss guard
if consec_limit > 0 and self._consecutive_losses_today(trade_day) >= consec_limit:
    return  # block all new entries for the rest of this candle cycle

# Daily max loss guard
if day_pnl < -(starting_equity * daily_max_loss_pct):
    return
```
Both guards mirror the exact logic in `session.py` so backtest and live results are now consistent.

### Files Changed
- `backtesting.py`
- `tests/unit/test_session_entry_sizing.py` (updated expected qty after max_symbol_allocation removal)

### Commit
`fix: enforce consecutive_loss_limit and daily_max_loss_pct in backtest` (commit 5bdc614)

---

## [2026-05-31] intraday_options risk style has no effect — BALANCED and AGGRESSIVE produce identical P&L

### Symptom
Backtests run with BALANCED vs AGGRESSIVE risk style for `intraday_options` engine produced identical P&L. Switching risk style had no observable effect on stop distance, target distance, or trailing behaviour.

### Root Cause
`engines/intraday_options.py:get_trend_adaptive_level_spec()` uses class-level `_adaptive_stop`, `_adaptive_target`, `_adaptive_trailing` dicts populated from `config.runtime.yaml` (`adaptive_stop_multiplier_normal`, etc.). These yaml values are identical regardless of which risk style is selected.

`build_trend_adaptive_position()` was called without a `risk_style_name` parameter from both the backtest path (`backtesting.py:920`) and the live entry path (`orchestration/session.py:1454`). The `risk_style_name` only reached `calculate_cost_aware_targets()` inside `resolve_trade_targets()`, which is used purely for cost/profit metadata — its stop/target/trailing outputs are entirely overwritten by `build_trend_adaptive_position()`.

Result: `ASSET_CLASS_RISK_PROFILES["INTRADAY_OPTIONS"]["BALANCED/AGGRESSIVE"]["sl_percent"]` values were never applied to the actual stop, target, or trailing levels.

### Fix
**`engines/intraday_options.py` — `get_trend_adaptive_level_spec()`**

Added `risk_style_name="BALANCED"` parameter. Computes per-axis scale factors relative to the BALANCED baseline using `ASSET_CLASS_RISK_PROFILES["INTRADAY_OPTIONS"]`:
```python
sl_scale = chosen["sl_percent"] / base["sl_percent"]          # AGGRESSIVE: 12/10 = 1.2
target_scale = chosen["target_percent"] / base["target_percent"]  # AGGRESSIVE: 20/15 = 1.333
trailing_scale = chosen["trailing_percent"] / base["trailing_percent"]  # AGGRESSIVE: 6/4.8 = 1.25
stop_multiplier = self._adaptive_stop[regime] * sl_scale
target_multiplier = self._adaptive_target[regime] * target_scale
trailing_multiplier = self._adaptive_trailing[regime] * trailing_scale
```

**`engines/intraday_options.py` — `build_trend_adaptive_position()`**
Added `risk_style_name="BALANCED"` parameter and passes it through to `get_trend_adaptive_level_spec()`.

**`orchestration/session.py`**
Passes `risk_style_name=cfg.risk_style_name` to `build_trend_adaptive_position()` at the live entry path.

**`backtesting.py`**
Passes `risk_style_name=self.config.risk_style_name` to `build_trend_adaptive_position()` at the backtest entry path.

### Effect
| Style | Stop (NORMAL) | Target (NORMAL) | Trailing (NORMAL) |
|-------|--------------|-----------------|-------------------|
| CONSERVATIVE | ATR × 1.76 | ATR × 2.00 | ATR × 0.583 |
| BALANCED | ATR × 2.20 | ATR × 2.50 | ATR × 0.700 |
| AGGRESSIVE | ATR × 2.64 | ATR × 3.33 | ATR × 0.875 |

CONSERVATIVE cuts losses faster (tighter stop, shorter target). AGGRESSIVE lets winners run further and accepts wider individual losses. The yaml regime-adaptive multipliers remain the calibration base; the risk style scales them proportionally.

### Files Changed
- `engines/intraday_options.py`
- `orchestration/session.py`
- `backtesting.py`

### Commit
`fix: apply risk style scaling to intraday_options stop/target/trailing levels`

---

## [2026-05-31] CONSERVATIVE risk style blocks more intraday_options entries than expected

### Symptom
Switching to CONSERVATIVE risk style produced significantly fewer trades than BALANCED or AGGRESSIVE. Risk style is supposed to affect exit levels and position sizing, not entry frequency.

### Root Cause
`orchestration/signal_workflow.py:should_enter_trade()` calls `resolve_trade_targets()` which calls `calculate_cost_aware_targets()` using `ASSET_CLASS_RISK_PROFILES["INTRADAY_OPTIONS"][risk_style_name]`. For CONSERVATIVE the profile has `target_percent=12%` vs BALANCED `15%`.

A smaller target percentage → smaller expected gross profit → the fixed transaction costs represent a larger fraction → `cost_to_profit_ratio` exceeds `intraday_options_max_entry_cost_ratio=0.20` for more trades → entry is blocked.

But the **actual** target at runtime is ATR-based (from `get_trend_adaptive_level_spec()`), not a percentage. The profitability check was using a profile percentage (12%) that has no relation to the actual exit price the trade will use (ATR × 2.0 at CONSERVATIVE scaling ≈ ₹10 on a ₹180 entry = 5.5%).

### Fix
**`orchestration/signal_workflow.py` — `should_enter_trade()`**

For `intraday_options`, after `resolve_trade_targets()` computes cost metadata, recalculate `is_profitable`, `cost_to_profit_ratio`, `expected_gross_profit`, `expected_net_profit` using the real ATR-based target from `get_trend_adaptive_level_spec()`:
```python
level_spec = context.engine.get_trend_adaptive_level_spec(
    entry_price=..., atr=..., analytics=..., risk_style_name=..., ...
)
real_target = level_spec["level3_target"]
gross = abs(real_target - entry_price) * quantity
targets["is_profitable"] = (gross - costs) > 0
targets["cost_to_profit_ratio"] = costs / gross
```

This means the entry filter now uses the same target the trade will actually aim for, so CONSERVATIVE entries are evaluated correctly against their real ATR-based profit potential — not a stale profile percentage.

### Files Changed
- `orchestration/signal_workflow.py`

### Commit
`fix: use ATR-based target in should_enter_trade profitability check for intraday_options`

---

## [2026-05-31] Small capital (≤₹35k) hits daily_max_loss_pct after first trade — entries blocked all day

### Symptom
Backtests with capital ≤ ₹20,000 produced only 2–4 trades over 5 days regardless of risk style. ₹1,00,000 capital with identical settings produced 27 trades.

### Root Cause
`daily_max_loss_pct = 3%` (₹600 for ₹20k capital). A single AGGRESSIVE stop-loss on 1 NIFTY lot ≈ ATR×2.64 × 75 units ≈ ₹975, which exceeds the ₹600 daily cap. After the first losing trade, all entries for the remaining day are blocked by the risk guard. This repeats each day → effectively 0–1 trades per day.

The 3% cap is calibrated for larger accounts (₹1L+) where ₹3,000 can absorb 2–3 losses before blocking. On small capital the absolute rupee cap becomes smaller than a single lot's stop-loss.

### Fix
Added `resolve_daily_max_loss_pct(capital, configured_pct)` in `risk_manager.py`. When `capital ≤ ₹35,000`, the effective daily loss pct is raised to `max(configured_pct, 10%)`:

```python
_SMALL_CAPITAL_THRESHOLD = 35_000.0
_SMALL_CAPITAL_MIN_DAILY_LOSS_PCT = 0.10

def resolve_daily_max_loss_pct(capital, configured_pct):
    if capital <= _SMALL_CAPITAL_THRESHOLD:
        return max(pct, _SMALL_CAPITAL_MIN_DAILY_LOSS_PCT)
    return pct
```

Both `backtesting.py` and `orchestration/session.py` now call `resolve_daily_max_loss_pct()` instead of using the raw config value directly.

Effect on ₹20k capital: daily loss budget = `max(3%, 10%)` = 10% = ₹2,000 → absorbs 2 AGGRESSIVE lot losses before blocking (consistent with consecutive_loss_limit=3 being the binding constraint instead).

### Files Changed
- `risk_manager.py`
- `backtesting.py`
- `orchestration/session.py`

### Commit
`fix: raise daily_max_loss_pct floor to 10% for capital <= 35k`

---

## [2026-06-01] Backtest overtrade + late entry/exit — full overhaul

### Symptom
`backtest_intraday_options_20260601_003515`: ATM_MULTI, BALANCED, 1m, 1 month.
- 125 trades despite `intraday_options_max_trades_per_underlying: 2` in config
- Net P&L = −₹24,756 with 56% win rate — system bled money through overtrading and charges
- TIME_EXIT_30M exits: 26 trades, 69% losses, total loss −₹7,080

### Root Causes

**1. Backtest never enforced daily trade cap per underlying**
`backtesting.py._enter_ranked_candidates()` never called `get_trade_frequency_key()` / `get_max_trades_per_day()` that the live session uses (`session.py:1158–1165`). Result: 17–18 trades/day instead of max 2.

**2. TIME_EXIT hardcoded at 30m (then raised to 60m in yaml)**
`engines/intraday_options.py.max_hold_minutes` was set from yaml `intraday_options_max_hold_minutes: 30` with no way to override per session.

**3. Entry mode default was LEGACY_IMMEDIATE**
`config.runtime.yaml fno.intraday_options_entry_mode: "LEGACY_IMMEDIATE"` bypassed staged option filters — changed to `LIVE_STAGED`.

**4. No sub-1m forming-candle entry mechanism**
Signal was evaluated only on fully-closed 1m candles. Moves that happened within a 1m bar were missed.

### Fixes

#### Phase 1: Backtest correctness
- `backtesting.py._enter_ranked_candidates()`: Added daily cap check using engine's `get_trade_frequency_key()` + `get_max_trades_per_day()`. After a successful entry, increments `trade_counts_by_day[trade_day][freq_key]`.
- `backtesting.py._build_engine_helper()`: Applies `config.time_exit_minutes` and `config.max_trades_per_underlying` as instance overrides on the engine.
- `backtesting.py.export_backtest_results()`: Added extended diagnostics — avg win/loss, profit factor, charges %, tick entry count, trades/day stats, P&L by exit reason.
- `backtesting.py._exit_position()`: Added `hold_minutes` field to trade records.

#### Phase 2: FormingCandlePreview (sub-1m live entry)
- New file: `tick_entry/forming_candle.py` — `FormingCandlePreview` class.
  - Synthesises partial candle from last closed 1m close + current WebSocket LTP
  - Runs signal scanner with `require_closed_signal_candle=False`
  - Requires LTP to hold above/below threshold for `forming_tick_confirm_ticks` consecutive ticks
  - Returns candidates marked `signal_source="FORMING_TICK"`
- `orchestration/session.py`: Instantiates `FormingCandlePreview` when `entry_mode=LIVE_TICK_CONFIRM` and merges forming candidates into ranked list each cycle.

#### Phase 3: TIME_EXIT configurable
- `config/config.runtime.yaml`: Changed `intraday_options_max_hold_minutes: 30` → `15`.
- `backtesting.py`/`session.py`: Accept `time_exit_minutes` from `BacktestConfig`/`SessionConfig` and override `engine.max_hold_minutes` at start.

#### Phase 4: Entry mode cleanup
- Added `LIVE_TICK_CONFIRM` as valid entry mode across `config.py`, `cli/configuration.py`, validation, and engine setup.
- Default `intraday_options_entry_mode` changed from `LEGACY_IMMEDIATE` → `LIVE_STAGED`.

#### Phase 5: UI controls
- `web/static/index.html`: Added selectable controls (live + backtest tabs) for:
  - Entry Mode (LIVE_STAGED / LIVE_TICK_CONFIRM / LEGACY_IMMEDIATE)
  - Max Trades per Underlying per Day (1–10)
  - Time Exit Window (10/15/20/30/disabled)
  - Forming-Candle Entry toggle + Confirm Ticks
- `cli/configuration.py`: Console prompts for same fields in both live and backtest CLI flows.
- `BacktestConfig` / `SessionConfig`: New fields `max_trades_per_underlying`, `time_exit_minutes`, `forming_tick_enabled`, `forming_tick_confirm_ticks`.
- `web/routes/config.py`: Parses new fields from backtest form payload.

### Files Changed
- `backtesting.py`
- `engines/intraday_options.py` (via `max_hold_minutes`/`max_trades_per_underlying_per_day` instance override)
- `config/config.runtime.yaml`
- `config.py` (validation allows LIVE_TICK_CONFIRM)
- `tick_entry/forming_candle.py` (new)
- `orchestration/session.py`
- `cli/configuration.py`
- `web/routes/config.py`
- `web/static/index.html`
- `tests/unit/test_config_and_trade_store.py` (updated valid entry modes)

---

## [2026-06-12] Intraday Options engine split: Buyer Only / Seller Only / Buy+Sell Both

### Symptom
A live trade log (`runners/06_intraday_options/logs/...sensex...`) showed a closed
`PE 73600 SELL 214.50→165.05 +1,743` trade — confirming the engine already opens
**genuine short/written option positions** when the multi-strategy aggregator (see the
[2026-06-11 fix](#2026-06-11-multi-strategy-sellbuy_pe-votes-silently-discarded-as-tied-or-missing--latechasing-entry-overhaul)
above) resolves `final_signal="SELL"` (`option_signal="BUY_PE"`,
`transaction_type="SELL"`, sell-to-open, requires margin). Prior to this change there was
no way to opt out of the seller side — the single `intraday_options` engine mixed
long-CE-buy and short-PE-write trades unconditionally, which many retail accounts
cannot margin for.

### Change
Split the single `IntradayOptionsEngine` into three separately-selectable engines that
share 100% of `apply_signal_filters`, strategies, lot sizing, and exit logic, differing
only in a new `trade_direction_mode` class attribute checked as the **first** gate in
`apply_signal_filters`:

| Engine choice | `name` | `trade_direction_mode` | Behavior |
|---|---|---|---|
| 6 — Buy + Sell Both | `intraday_options` (alias `IntradayOptionsBothEngine`) | `BUY_SELL_BOTH` | Unrestricted — unchanged from prior releases (current default). |
| 7 — Buyer Only | `intraday_options_buyer` | `BUY_ONLY` | Long CE only; `SELL`-resolved scans forced to `HOLD`. |
| 8 — Seller Only | `intraday_options_seller` | `SELL_ONLY` | Short PE (option-writing) only, requires margin; `BUY`-resolved scans forced to `HOLD`. |

See [`Docs/INTRADAY_OPTIONS_GUIDE.md` Section 1.1](INTRADAY_OPTIONS_GUIDE.md#11-engine-variants--buyer--seller--both)
for the full signal-model explanation and known limitations (no distinct "long PE"
signal exists yet — Section 17).

### Files Changed
- `engines/intraday_options.py` — `trade_direction_mode` class attribute + gate at the
  top of `apply_signal_filters`; new `IntradayOptionsBuyerEngine`,
  `IntradayOptionsSellerEngine` subclasses; `IntradayOptionsBothEngine` alias.
- `engines/__init__.py` — export new classes.
- `config.py` — `INTRADAY_OPTIONS_ENGINE_NAMES` frozenset +
  `is_intraday_options_engine_name()` helper; `INTRADAY_ENGINE_NAMES` updated so the new
  engines get the "intraday" risk-style bucket.
- `cli/configuration.py` — `ENGINE_OPTIONS["7"]`/`["8"]`, engine-choice prompt labels,
  all `engine.name == "intraday_options"` checks broadened via
  `INTRADAY_OPTIONS_ENGINE_NAMES`.
- `cli/interactive_input.py` — `prompt_strategy_configuration` broadened.
- `backtesting.py` — `ENGINE_OPTIONS["7"]`/`ENGINE_OPTIONS["8"]`, engine-class mapping in
  `_build_engine_helper`, all `engine_name == "intraday_options"` checks broadened via
  `is_intraday_options_engine_name()`.
- `orchestration/session.py`, `orchestration/positions.py`,
  `orchestration/signal_workflow.py` — all `engine.name`/`position.get("engine_name")`
  equality checks against `"intraday_options"` broadened via
  `is_intraday_options_engine_name()`.
- `tick_entry/forming_candle.py` — activation check broadened.
- `web/routes/config.py` — `/api/runtime-defaults` engine→period/interval map extended
  for choices 7/8; `/api/backtest` checks broadened.
- `web/static/index.html` — engine picker dropdowns (live + backtest) add options 7/8;
  `IS_FNO`/`IS_IOPTS` JS helpers and `_enginePeriodInterval` fallback extended.
- `tests/unit/test_engine_workflows.py` — new `IntradayOptionsDirectionModeTests`
  asserting Buyer holds on SELL, Seller holds on BUY, Both passes both through
  unchanged, and default `trade_direction_mode` values.

---

## [2026-06-13] Nifty/Sensex expiry dropdown stuck in "loading" forever

### Symptom
On the intraday-options web dashboard, the expiry dropdown spun forever
("Unable to fetch nifty/sensex expiry"). Once Kite indices started showing
live data (ticker bar working, VIX populated), the expiry field still fell
back to a synthetic date with `⚠ Strike info unavailable: Timed out after 10s
waiting for Kite instrument data for NIFTY (OPT)`.

### Root Causes
Three layered bugs, all stemming from blocking calls inside `async def`
FastAPI routes — on a single-threaded uvicorn event loop, one blocking call
freezes **every** endpoint, not just the caller:

1. **`web/routes/config.py` `/api/fno-data`** — called `kite.instruments()`
   and friends synchronously inside the route coroutine. A slow/hanging Kite
   call froze the whole server (`curl` returned `HTTP_CODE:000` — TCP
   connected, zero response — after 20s).
2. **`web/routes/session.py` `/api/indices`** — same pattern for the
   Nifty/Sensex/VIX quote fetch, which runs on every page load and every 10s
   poll, so it re-froze the loop continuously.
3. **`network_utils.py` `_KITE_MIN_INTERVAL_SECONDS["instruments"] = 86400.0`**
   — a module-global, operation-keyed rate limiter that called
   `time.sleep(~86399)` on any second "instruments" fetch in the same process
   (e.g. a different exchange). `get_cached_kite_instruments()` already
   provides daily file-based caching per exchange
   (`state/kite_cache/instruments_{EXCHANGE}_{YYYYMMDD}.json`), making this
   in-memory throttle redundant and actively harmful.

### Fixes
- `/api/fno-data` and `/api/indices` now run their blocking Kite/yfinance
  calls via `loop.run_in_executor(None, fn, ...)` wrapped in
  `asyncio.wait_for(..., timeout=10.0)`, with a synthetic-data/error fallback
  on `asyncio.TimeoutError` so the route always returns within 10s and the
  event loop never blocks.
- Removed the `"instruments"` entry from `_KITE_MIN_INTERVAL_SECONDS` in
  `network_utils.py` — instrument lists are already cached daily per exchange
  via `get_cached_kite_instruments()`.

### Files Changed
- `web/routes/config.py` — `_FNO_DATA_TIMEOUT_SECONDS`, `_fetch_fno_payload()`
  helper, executor+timeout wrapping in `get_fno_data`.
- `web/routes/session.py` — `_INDICES_FETCH_TIMEOUT_SECONDS`, executor+timeout
  wrapping for `_fetch_indices`/`_fetch_vix` in `get_indices`.
- `network_utils.py` — removed the 24h `"instruments"` rate-limit entry.

---

## [2026-06-13] Backtest "TRADES (N)" panel: overlapping cards + no entry-reason visibility

### Symptom
During a running backtest, the live "TRADES (N)" card list (`#btr-trades-body`)
showed overlapping/garbled rows once the list grew beyond the panel's
`max-height`. Separately, the entry signal/reason for each trade was only
visible after expanding a card (running view) and not shown at all in the
final results table (`#bt-trades-body`).

### Root Cause
`#btr-trades-body` (and `#why-trades-body`) are `display:flex;
flex-direction:column` containers with `max-height` + `overflow-y:auto`. Flex
children default to `flex-shrink:1`, so once total card height exceeded
`max-height`, the browser shrank/squished the cards to fit instead of letting
the container scroll — producing the overlapping layout.

### Fixes
- Added `#btr-trades-body > div, #why-trades-body > div{flex-shrink:0}` so
  cards keep their natural height and the container scrolls instead of
  squishing.
- `appendBtTrade()` now renders `t.entry_reason` as an always-visible,
  truncated line under the card header (with a `title` tooltip for the full
  text), in addition to the existing "Signal" line inside the
  expand/collapse detail panel.
- `renderBacktestResults()`'s final trades table gained a new "Entry Reason"
  column (`<td>` showing `t.entry_reason`, truncated with a `title` tooltip),
  matching the pattern already used by the live "Trade Book Today" table.

### Files Changed
- `web/static/index.html` — `<style>` flex-shrink fix; `appendBtTrade()` card
  template; `renderBacktestResults()` header/row template (`colspan` 11→12).

### Follow-up: final results table replaced with the same expandable cards as the running view
The new "Entry Reason" column in the final results table showed `—` for every
row, because `t.entry_reason` was empty for those trades while the richer
analytics (strategy, score, ATR, IV, stop loss, hold time, partial exits) were
only available via the running view's expandable cards. Per user request, the
final "Trades (N)" table now reuses the exact same card rendering as the
running view, so both "during running" and "after running" show identical,
complete per-trade details.

- Extracted `appendBtTrade()`'s card markup into a shared
  `buildTradeCardHtml(t, uid)` function.
- `#bt-trades-body` container changed from a `<table>` to the same
  `display:flex;flex-direction:column;overflow-y:auto` card list as
  `#btr-trades-body`, with `flex-shrink:0` on each card.
- `renderBacktestResults()` now renders one card per trade via
  `buildTradeCardHtml()`.

### Follow-up 2: cards in the final results view still showed almost nothing
Even with the card layout, the final results cards only showed "Charges",
"Exit reason", and "Gross" — the same fields the old table had. The cause was
upstream: `web/routes/config.py::_summarise()` built `trades_list` from
`trades_df` using only `symbol/side/entry_time/exit_time/entry_price/
exit_price/quantity/pnl/net_pnl/charges/exit_reason/strategy` — none of the
richer per-trade fields (`entry_reason`, `score`, `atr`, `hold_minutes`,
`partial_exit_count`, `partial_exit_events`, `underlying_symbol`,
`option_type`, `underlying_close_at_entry`) that `_on_trade()` already streams
during a *running* backtest (`backtesting.py:1290-1304`) and that
`buildTradeCardHtml()` expects.

- `_summarise()` now also includes `entry_reason`, `score`, `atr`,
  `hold_minutes`, `partial_exit_count`, `partial_exit_events`, `underlying`
  (from `underlying_symbol`), `option_type`, and `underlying_close_at_entry`
  in each `trades_list` entry, using new `_safe_str`/`_safe_int` helpers
  (alongside the existing `_safe`/`_ff`) to handle pandas `NaN`/`None` safely.
- `iv` and `stop_loss` are not persisted on closed trades in `trades_df` and
  remain absent from both the running and final views — a pre-existing gap,
  not introduced by this change.

### Files Changed (Follow-up 2)
- `web/routes/config.py` — `_summarise()`: added `_safe_str`/`_safe_int`
  helpers (moved `_safe` earlier in the function) and extended `trades_list`
  entries with the additional fields above.
