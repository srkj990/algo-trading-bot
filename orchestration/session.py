from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from config import (
    COST_EDGE_BUFFER_RUPEES,
    EXPECTED_EDGE_SCORE_MULTIPLIER,
    MIN_EDGE_TO_COST_RATIO,
    TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER,
    TRANSACTION_COST_MODEL_ENABLED,
    TRANSACTION_SLIPPAGE_PCT_PER_SIDE,
    get_runtime_config,
)
from executor import calculate_cost_aware_targets
from executor import get_available_margin, get_quote
from engines.common import (
    apply_capital_limits_to_quantity,
    build_position,
    count_open_structures,
    get_deployed_capital,
    log_positions,
    resolve_trade_targets,
)
from fno_data_fetcher import (
    get_atm_option_strike,
    get_contract_lot_size,
    get_option_greeks_snapshot,
    resolve_option_contract,
)
from models.position_adapter import opposite_side, position_entry_price, position_quantity, position_side
from risk_manager import (
    atr_position_size,
    atr_stop_from_value,
    calculate_target_price,
    check_daily_loss_limit,
    position_size,
)
from transaction_costs import estimate_intraday_equity_round_trip_cost

from . import positions as position_flow
from .context import persist_runtime_state
from .signal_workflow import scan_symbols, should_enter_trade


def log_order_signal_banner(log_event, title, lines):
    from display import banner as _banner
    _banner(log_event, title, lines, prefix="ORDER", width=72)


def _resolve_entry_order_type(context) -> str:
    return str(context.runtime_config.orders.default_entry_order_type or "MARKET").upper()


def _resolve_limit_price(entry_price: float, side: str, buffer_pct: float) -> float:
    price = float(entry_price)
    buffer = price * float(buffer_pct or 0.0)
    if side == "BUY":
        return price + buffer
    return max(0.01, price - buffer)


def _validate_intraday_options_live_entry(context, symbol, quantity, entry_price, actual_targets) -> bool:
    if str(context.config.execution_mode).upper() != "LIVE":
        return True

    fno_cfg = context.runtime_config.fno
    quote = get_quote(symbol, provider=context.config.execution_provider)
    spread_pct = quote.spread_pct
    max_spread_pct = float(fno_cfg.intraday_options_max_spread_pct or 0.0)
    if max_spread_pct > 0 and spread_pct is not None and spread_pct > max_spread_pct:
        context.log_event(
            f"[FILTER] Skipping {symbol}: spread {spread_pct * 100:.2f}% exceeds "
            f"intraday-options max {max_spread_pct * 100:.2f}%"
        )
        return False

    min_open_interest = int(fno_cfg.intraday_options_min_open_interest or 0)
    open_interest = quote.open_interest
    if min_open_interest > 0 and open_interest is not None and int(open_interest) < min_open_interest:
        context.log_event(
            f"[FILTER] Skipping {symbol}: OI {int(open_interest)} below minimum "
            f"{min_open_interest}"
        )
        return False

    max_cost_ratio = float(fno_cfg.intraday_options_max_entry_cost_ratio or 0.0)
    actual_cost_ratio = float(actual_targets.get("cost_to_profit_ratio") or 0.0)
    if max_cost_ratio > 0 and actual_cost_ratio > max_cost_ratio:
        context.log_event(
            f"[FILTER] Skipping {symbol}: expected costs are {actual_cost_ratio * 100:.1f}% "
            f"of profit, above max {max_cost_ratio * 100:.1f}%"
        )
        return False

    if bool(context.runtime_config.orders.margin_check_enabled):
        available_margin = get_available_margin(
            product=context.engine.order_product,
            provider=context.config.execution_provider,
        )
        if available_margin is not None:
            required_margin = float(entry_price) * int(quantity)
            required_margin *= 1 + float(context.runtime_config.orders.margin_buffer_pct or 0.0)
            if float(available_margin) < required_margin:
                context.log_event(
                    f"[FILTER] Skipping {symbol}: available margin {float(available_margin):.2f} "
                    f"below estimated requirement {required_margin:.2f}"
                )
                return False

    return True


def _calculate_session_pnl(trade_book, trade_day):
    session_pnl = 0.0
    trade_day_text = trade_day.isoformat()
    for trade in trade_book or []:
        exit_time = trade.get("exit_time")
        if not exit_time:
            continue
        exit_day = None
        if isinstance(exit_time, datetime):
            exit_day = exit_time.date().isoformat()
        else:
            try:
                exit_day = datetime.fromisoformat(str(exit_time)).date().isoformat()
            except ValueError:
                exit_day = str(exit_time)[:10]
        if exit_day != trade_day_text:
            continue
        session_pnl += float(trade.get("net_pnl", trade.get("pnl", 0.0)) or 0.0)
    return session_pnl


def _calculate_consecutive_losses(trade_book, trade_day):
    trade_day_text = trade_day.isoformat()
    closed_today = []
    for trade in trade_book or []:
        exit_time = trade.get("exit_time")
        if not exit_time:
            continue
        exit_day = None
        if isinstance(exit_time, datetime):
            exit_day = exit_time.date().isoformat()
            sort_key = exit_time
        else:
            try:
                parsed_exit = datetime.fromisoformat(str(exit_time))
                exit_day = parsed_exit.date().isoformat()
                sort_key = parsed_exit
            except ValueError:
                exit_day = str(exit_time)[:10]
                sort_key = datetime.min
        if exit_day != trade_day_text:
            continue
        closed_today.append((sort_key, trade))

    consecutive_losses = 0
    for _, trade in sorted(closed_today, key=lambda item: item[0], reverse=True):
        net_pnl = float(trade.get("net_pnl", trade.get("pnl", 0.0)) or 0.0)
        if net_pnl < 0:
            consecutive_losses += 1
            continue
        break
    return consecutive_losses


def _set_risk_pause(context, now, pause_minutes, reason):
    if int(pause_minutes or 0) <= 0:
        return
    pause_until = now + timedelta(minutes=int(pause_minutes))
    context.session_runtime_state["risk_pause_until"] = pause_until.isoformat()
    context.session_runtime_state["risk_pause_reason"] = str(reason)
    context.log_event(
        f"[RISK] Trading paused until {pause_until.strftime('%H:%M:%S')} | {reason}",
        "warning",
    )
    persist_runtime_state(context)


def _get_risk_pause_status(context, now):
    raw_pause_until = context.session_runtime_state.get("risk_pause_until")
    if not raw_pause_until:
        return False, None
    try:
        pause_until = datetime.fromisoformat(str(raw_pause_until))
    except ValueError:
        context.session_runtime_state.pop("risk_pause_until", None)
        context.session_runtime_state.pop("risk_pause_reason", None)
        persist_runtime_state(context)
        return False, None
    if pause_until <= now:
        context.session_runtime_state.pop("risk_pause_until", None)
        context.session_runtime_state.pop("risk_pause_reason", None)
        persist_runtime_state(context)
        return False, None
    return True, context.session_runtime_state.get("risk_pause_reason")


def _apply_safe_mode_tightening(context) -> None:
    """Move each open position to breakeven stop, halve remaining target distance,
    and activate trailing immediately. Only applied once per position."""
    for symbol, pos in list(context.positions.items()):
        if pos.get("_safe_mode_tightened"):
            continue
        try:
            entry   = float(pos.get("entry_price") or 0)
            side    = str(pos.get("side") or "BUY").upper()
            current_sl = float(pos.get("stop_loss") or 0)
            current_tgt = float(pos.get("target") or 0)
            if entry <= 0:
                continue

            # move stop to breakeven (only tighten, never loosen)
            if side == "BUY":
                new_sl = max(current_sl, entry)
            else:
                new_sl = min(current_sl, entry) if current_sl > 0 else entry

            # halve remaining distance to target
            if current_tgt > 0:
                remaining = abs(current_tgt - entry)
                if side == "BUY":
                    new_tgt = entry + remaining * 0.5
                else:
                    new_tgt = entry - remaining * 0.5
            else:
                new_tgt = current_tgt

            pos["stop_loss"]      = round(new_sl, 4)
            pos["target"]         = round(new_tgt, 4)
            pos["trailing_active"] = True
            pos["_safe_mode_tightened"] = True

            context.log_event(
                f"[SAFE MODE] {symbol}: SL → {new_sl:.2f} (breakeven), "
                f"Target → {new_tgt:.2f} (halved), trailing activated",
                "warning",
            )
        except Exception as exc:
            context.log_event(f"[SAFE MODE] Tightening failed for {symbol}: {exc}", "error")


def _sync_exit_only_live_positions(context, now):
    if str(getattr(context.config, "execution_mode", "")).upper() != "LIVE":
        return False
    if not bool(getattr(context.config, "exit_only_mode", False)):
        return False
    resync_interval_seconds = int(
        getattr(context.config, "live_broker_resync_interval_seconds", 0) or 0
    )
    last_resync_at = context.session_runtime_state.get("last_live_broker_resync_at")
    if resync_interval_seconds > 0 and last_resync_at:
        try:
            last_resync_dt = datetime.fromisoformat(str(last_resync_at))
        except ValueError:
            last_resync_dt = None
        if last_resync_dt is not None:
            if (now - last_resync_dt).total_seconds() < resync_interval_seconds:
                return False

    existing_positions = dict(context.positions)
    reconciled_positions = context.engine.reconcile_startup(
        execution_mode=context.config.execution_mode,
        persisted_positions=existing_positions,
    )
    context.session_runtime_state["last_live_broker_resync_at"] = now.isoformat()
    if reconciled_positions == existing_positions:
        return False

    added = sorted(set(reconciled_positions) - set(existing_positions))
    removed = sorted(set(existing_positions) - set(reconciled_positions))
    for symbol in added:
        context.log_event(f"[RECON] Exit-only sync added live broker position: {symbol}")
    for symbol in removed:
        context.log_event(f"[RECON] Exit-only sync removed broker-flat position: {symbol}")
    context.positions = reconciled_positions
    return True


def _build_intraday_option_position_from_roll(context, current_position, symbol, qty, entry_price, analytics, now):
    extra_fields = {
        "trade_identity": current_position.get("trade_identity"),
        "dynamic_atm_roll_enabled": True,
        "strike_offset": current_position.get("strike_offset", 0),
        "strike_offset_mode": current_position.get("strike_offset_mode", "ATM"),
        "entry_underlying_price": analytics.get("underlying_price"),
        "rolled_from": current_position.get("symbol"),
        "roll_count": int(current_position.get("roll_count", 0)) + 1,
    }
    if hasattr(context.engine, "build_trend_adaptive_position"):
        option_data = context.fetch_data(
            symbol,
            period=context.engine.data_period,
            interval=context.engine.data_interval,
        )
        return context.engine.build_trend_adaptive_position(
            symbol=symbol,
            side="BUY",
            quantity=qty,
            entry_price=float(entry_price),
            atr=float(current_position.get("atr") or 0.0),
            signal_score=float(current_position.get("runner_signal_score") or 0.0),
            analytics=analytics,
            lot_size=get_contract_lot_size(symbol),
            now=now,
            entry_analytics=analytics,
            engine_name=context.engine.name,
            execution_mode=context.config.execution_mode,
            order_product=context.engine.order_product,
            premium_volatility_distance=(
                context.engine.get_premium_volatility_trailing_distance(
                    option_data,
                    analytics,
                )
                if hasattr(context.engine, "get_premium_volatility_trailing_distance")
                else 0.0
            ),
            extra_fields=extra_fields,
        )
    stop_distance = float(entry_price) * 0.10
    stop_loss_price = float(entry_price) - stop_distance
    target_distance = float(entry_price) * 0.20
    trailing_distance = float(entry_price) * 0.075
    target_price = calculate_target_price("BUY", float(entry_price), target_distance)
    trailing_stop = float(stop_loss_price)
    trailing_activation_distance = max(
        float(trailing_distance or 0.0),
        float(stop_distance) * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
    )
    return build_position(
        symbol=symbol,
        side="BUY",
        quantity=qty,
        entry_price=float(entry_price),
        stop_loss=stop_loss_price,
        target=target_price,
        trailing_stop=trailing_stop,
        trailing_distance=trailing_distance,
        trailing_activation_distance=trailing_activation_distance,
        trailing_active=False,
        atr=current_position.get("atr"),
        stop_distance=stop_distance,
        lot_size=get_contract_lot_size(symbol),
        entry_analytics=analytics,
        entry_time=now.isoformat(),
        engine_name=context.engine.name,
        execution_mode=context.config.execution_mode,
        order_product=context.engine.order_product,
        **extra_fields,
    )


def _maybe_roll_dynamic_atm_positions(context, symbol_snapshots, now) -> bool:
    cfg = context.config
    if context.engine.name != "intraday_options" or not cfg.atm_option_config:
        return False

    roll_trigger_pct = float(context.runtime_config.fno.intraday_options_roll_trigger_pct or 0.0)
    if roll_trigger_pct <= 0:
        return False

    state_changed = False
    for symbol, position in list(context.positions.items()):
        if not position.get("dynamic_atm_roll_enabled"):
            continue
        snapshot = symbol_snapshots.get(symbol)
        analytics = (snapshot or {}).get("analytics") or position.get("entry_analytics") or {}
        entry_underlying_price = float(position.get("entry_underlying_price") or analytics.get("underlying_price") or 0.0)
        current_underlying_price = float(analytics.get("underlying_price") or 0.0)
        if entry_underlying_price <= 0 or current_underlying_price <= 0:
            continue
        move_pct = abs(current_underlying_price - entry_underlying_price) / entry_underlying_price
        if move_pct < (roll_trigger_pct / 100.0):
            continue

        underlying = analytics.get("underlying")
        option_type = analytics.get("option_type")
        expiry = analytics.get("expiry") or cfg.atm_option_config.get("expiry")
        strike_offset = int(position.get("strike_offset", 0))
        if not underlying or option_type not in {"CE", "PE"} or not expiry:
            continue

        new_strike = get_atm_option_strike(underlying, expiry, option_type, strike_offset=strike_offset)
        new_symbol = resolve_option_contract(underlying, expiry, new_strike, option_type)
        if new_symbol == symbol:
            position["entry_underlying_price"] = current_underlying_price
            state_changed = True
            continue

        option_data = context.fetch_data(new_symbol, period=context.engine.data_period, interval=context.engine.data_interval)
        if option_data.empty:
            context.log_event(f"[ROLL] Skipping roll for {symbol}: no data for {new_symbol}", "warning")
            continue
        new_entry_price = float(option_data.iloc[-1]["Close"])
        new_analytics = get_option_greeks_snapshot(
            new_symbol,
            option_intraday=option_data,
            option_price=new_entry_price,
            underlying_price=current_underlying_price if current_underlying_price > 0 else None,
        )

        context.log_event(
            f"[ROLL] Rolling {symbol} -> {new_symbol} | Underlying move={move_pct * 100:.2f}% | "
            f"{entry_underlying_price:.2f}->{current_underlying_price:.2f}"
        )
        closed = position_flow.close_position_symbols(
            context.engine,
            context.positions,
            [symbol],
            reason=f"Strike roll {move_pct * 100:.2f}%",
            trade_book=context.trade_book,
            trade_store=context.trade_store,
            place_order=context.place_order,
            log_order_signal_banner=lambda title, lines: log_order_signal_banner(context.log_event, title, lines),
            fetch_data=context.fetch_data,
            log_event=context.log_event,
            transaction_cost_model_enabled=TRANSACTION_COST_MODEL_ENABLED,
            slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            symbol_snapshots=symbol_snapshots,
            exit_time=now,
        )
        if not closed:
            continue

        requested_qty = int(position_quantity(position))
        order_type = _resolve_entry_order_type(context)
        limit_price = None
        if order_type == "LIMIT":
            limit_price = _resolve_limit_price(
                new_entry_price,
                "BUY",
                context.runtime_config.orders.entry_limit_price_buffer_pct,
            )
        order_result = context.place_order(
            "BUY",
            requested_qty,
            new_symbol,
            note=f"Strike roll from {symbol}",
            product=context.engine.order_product,
            entry_price=new_entry_price,
            order_type=order_type,
            price=limit_price,
            enforce_spread_check=True,
            enforce_margin_check=True,
        )
        if order_result is None or int(order_result.filled_quantity or 0) <= 0:
            context.log_event(f"[ROLL] Roll entry for {new_symbol} did not fill", "warning")
            state_changed = True
            continue

        qty = int(order_result.filled_quantity)
        actual_entry_price = float(order_result.average_price or new_entry_price)
        context.positions[new_symbol] = _build_intraday_option_position_from_roll(
            context,
            position,
            new_symbol,
            qty,
            actual_entry_price,
            new_analytics,
            now,
        )
        state_changed = True
    return state_changed


def run_trading_session(context):
    cfg = context.config
    engine = context.engine
    _paper_override = bool(
        getattr(cfg, "paper_trading_override", False)
        or getattr(get_runtime_config().session_defaults, "paper_trading_override", False)
    )

    context.log_event("[MAIN] Trading session initialized")
    context.log_event(
        f"[MAIN] Engine={engine.name} | Mode={cfg.execution_mode} | ExitOnly={bool(getattr(cfg, 'exit_only_mode', False))} | "
        f"OpenPositions={len(context.positions)} | Sleep={int(getattr(engine, 'sleep_seconds', 60))}s"
    )
    if _paper_override:
        context.log_event(
            "[MAIN] paper_trading_override=true — weekend/market-hours checks bypassed. "
            "Cycle clock simulated as Wednesday 10:30. PAPER mode only.",
            "warning",
        )
    if str(getattr(cfg, "execution_mode", "")).upper() == "LIVE":
        context.log_event(
            f"[MAIN] Live execution provider={getattr(cfg, 'execution_provider', 'KITE')} | "
            f"Broker resync interval={int(getattr(cfg, 'live_broker_resync_interval_seconds', 0) or 0)}s"
        )
    if context.positions:
        context.log_event(f"[MAIN] Restored {len(context.positions)} persisted/open position(s) at startup")
    else:
        context.log_event("[MAIN] No persisted/open positions at startup")

    while True:
        if context.stop_fn():
            context.log_event("[MAIN] Stop requested — exiting trading loop.")
            break
        context.cycle_data_cache.clear()
        now = datetime.now()
        if context.previous_cycle_started_at is not None:
            gap_seconds = (now - context.previous_cycle_started_at).total_seconds()
            expected_cycle_seconds = max(30, int(getattr(engine, "sleep_seconds", 60)) + 30)
            if gap_seconds > expected_cycle_seconds:
                context.log_event(
                    f"[HEALTH] Cycle gap detected: {gap_seconds:.1f}s since previous cycle start (expected around {expected_cycle_seconds}s). Possible causes: internet issue, provider stall, slow API response, or a blocked fetch.",
                    "warning",
                )
        context.previous_cycle_started_at = now

        current_trade_day = now.date()
        if current_trade_day != context.active_trade_day:
            context.active_trade_day = current_trade_day
            context.traded_symbols_today.clear()
            context.trade_counts_today.clear()
            context.session_runtime_state.pop("risk_pause_until", None)
            context.session_runtime_state.pop("risk_pause_reason", None)
            context.recent_live_order_timestamps.clear()
            context.log_event("[MAIN] New day detected, reset traded symbol tracker")
            if engine.order_product == "MIS" and context.positions:
                context.log_event("[MAIN] Clearing stale intraday positions for new day", "warning")
                context.positions.clear()
            context.regime_cache = {}
            persist_runtime_state(context)

        cycle_now = _simulated_now(now) if _paper_override else now
        cycle_state = engine.get_cycle_state(cycle_now)
        if _sync_exit_only_live_positions(context, now):
            persist_runtime_state(context)
        from display import cycle_banner as _cycle_banner
        _cycle_banner(context.log_event, cycle_state["reason"], now)
        context.log_event(f"[SESSION] {cycle_state['reason']}")

        if cycle_state["force_square_off"]:
            context.log_event("[SESSION] Force square-off path active")
            if position_flow.force_square_off_positions(
                engine,
                context.positions,
                context.trade_book,
                context.trade_store,
                context.place_order,
                lambda title, lines: log_order_signal_banner(context.log_event, title, lines),
                context.fetch_data,
                context.log_event,
                TRANSACTION_COST_MODEL_ENABLED,
                float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            ):
                persist_runtime_state(context)
            log_positions(context.positions, context.log_event)
            time.sleep(engine.sleep_seconds)
            continue

        if not cycle_state["allow_scan"] and not (cycle_state["manage_positions"] and context.positions):
            context.log_event(
                f"[SESSION] Idle: {cycle_state['reason']} | OpenPositions={len(context.positions)} | "
                f"Sleeping {int(getattr(engine, 'sleep_seconds', 60))}s"
            )
            log_positions(context.positions, context.log_event)
            time.sleep(engine.sleep_seconds)
            continue

        try:
            context.log_event(
                f"[SCAN] Starting scan cycle | Symbols={len(getattr(cfg, 'selected_symbols', []) or [])} | "
                f"ManagePositions={bool(cycle_state['manage_positions'])} | AllowEntries={bool(cycle_state['allow_entries'])}"
            )
            scan_result = scan_symbols(context, now)
        except Exception as exc:
            context.log_event(f"[ERROR] Scan cycle failed: {type(exc).__name__}: {exc}", "error")
            pause_minutes = int(context.runtime_config.risk_controls.api_failure_pause_minutes or 0)
            _set_risk_pause(
                context,
                now,
                pause_minutes,
                f"API or scan failure: {type(exc).__name__}",
            )
            time.sleep(engine.sleep_seconds)
            continue
        symbol_snapshots = scan_result.symbol_snapshots
        ranked_candidates = scan_result.ranked_candidates
        context._last_symbol_snapshots = symbol_snapshots  # used by explainability emitter
        position_flow.log_ranked_candidates(ranked_candidates, context.log_event)

        # Warning Center — web-mode only, no-op in console mode
        try:
            from logger import is_web_mode
            if is_web_mode():
                from web import state as _web_state
                from web.core.warning_engine import compute_warnings
                _vix = context.session_runtime_state.get("_cached_vix")
                _warnings, _market_intel = compute_warnings(context, symbol_snapshots, now, vix=_vix)
                _web_state.set_warnings(_warnings, _market_intel)
        except Exception:
            pass

        if not symbol_snapshots and context.positions:
            context.log_event("[ERROR] No symbol data available for open positions", "error")
            _set_risk_pause(
                context,
                now,
                int(context.runtime_config.risk_controls.api_failure_pause_minutes or 0),
                "No symbol data available for open positions",
            )
        elif not symbol_snapshots:
            context.log_event("[ERROR] No symbol data available in this cycle", "error")
            _set_risk_pause(
                context,
                now,
                int(context.runtime_config.risk_controls.api_failure_pause_minutes or 0),
                "No symbol data available in this cycle",
            )
            time.sleep(engine.sleep_seconds)
            continue

        state_changed = False
        if cycle_state["manage_positions"]:
            state_changed = position_flow.manage_open_positions(
                engine,
                context.positions,
                symbol_snapshots,
                now,
                context.trade_book,
                context.trade_store,
                context.place_order,
                lambda title, lines: log_order_signal_banner(context.log_event, title, lines),
                context.fetch_data,
                context.log_event,
                TRANSACTION_COST_MODEL_ENABLED,
                float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            if state_changed:
                persist_runtime_state(context)
            if _maybe_roll_dynamic_atm_positions(context, symbol_snapshots, now):
                persist_runtime_state(context)

        # safe mode: tighten open positions once, then auto-stop when flat
        if context.session_runtime_state.get("override_safe_mode"):
            _apply_safe_mode_tightening(context)
            if not context.positions:
                context.log_event(
                    "[OVERRIDE] Safe mode: all positions closed — auto-stopping session.", "warning"
                )
                from logger import is_web_mode
                if is_web_mode():
                    from web import state as _web_state
                    _web_state.request_stop()
                break

        deployed_capital = get_deployed_capital(context.positions)
        context.log_event(f"[RISK] Current deployed capital: {deployed_capital:.2f}")
        current_time = time.time()
        cooldown_active = engine.cooldown_seconds > 0 and current_time - context.last_entry_time < engine.cooldown_seconds

        if getattr(cfg, "exit_only_mode", False):
            context.log_event("[SESSION] Exit-only mode active - new entries disabled")
        elif context.session_runtime_state.get("override_pause_entries"):
            context.log_event("[OVERRIDE] New entries paused by trader override")
        elif not cycle_state["allow_entries"]:
            context.log_event("[SESSION] New entries disabled in current window")
        elif cooldown_active:
            context.log_event("[COOLDOWN] Skipping new entries")
        else:
            paused, pause_reason = _get_risk_pause_status(context, now)
            if paused:
                context.log_event(
                    f"[RISK] New entries paused by risk controls | Reason={pause_reason}",
                    "warning",
                )
                log_positions(context.positions, context.log_event)
                time.sleep(engine.sleep_seconds)
                continue

            risk_cfg = context.runtime_config.risk_controls
            max_daily_loss_pct = float(risk_cfg.daily_max_loss_pct or 0.0)
            session_pnl = _calculate_session_pnl(context.trade_book, current_trade_day)
            if check_daily_loss_limit(session_pnl, cfg.capital, max_daily_loss_pct):
                context.log_event(
                    f"[RISK] Daily loss limit reached | SessionPnL={session_pnl:+.2f} | "
                    f"MaxLossPct={max_daily_loss_pct * 100:.2f}% | New entries blocked"
                )
                log_positions(context.positions, context.log_event)
                time.sleep(engine.sleep_seconds)
                continue
            consecutive_loss_limit = int(risk_cfg.consecutive_loss_limit or 0)
            if consecutive_loss_limit > 0:
                consecutive_losses = _calculate_consecutive_losses(
                    context.trade_book,
                    current_trade_day,
                )
                if consecutive_losses >= consecutive_loss_limit:
                    context.log_event(
                        f"[RISK] Consecutive loss limit reached | Losses={consecutive_losses} | "
                        f"Limit={consecutive_loss_limit} | New entries blocked",
                        "warning",
                    )
                    log_positions(context.positions, context.log_event)
                    time.sleep(engine.sleep_seconds)
                    continue
            planned_entries = ranked_candidates[:1] if cfg.entry_selection_mode == "TOP1" else ranked_candidates[:cfg.top_n_count]

            for candidate in planned_entries:
                if candidate.get("is_pair"):
                    try:
                        entered = _execute_pair_entry(context, candidate, now, deployed_capital)
                    except Exception as exc:
                        context.log_event(
                            f"[ERROR] Pair entry failed: {type(exc).__name__}: {exc}",
                            "error",
                        )
                        _set_risk_pause(
                            context,
                            now,
                            int(risk_cfg.api_failure_pause_minutes or 0),
                            f"Entry failure: {type(exc).__name__}",
                        )
                        break
                    if entered:
                        deployed_capital = get_deployed_capital(context.positions)
                        if cfg.entry_selection_mode == "TOP1":
                            break
                    continue

                try:
                    entered = _execute_single_entry(context, candidate, now, deployed_capital, cycle_state)
                except Exception as exc:
                    context.log_event(
                        f"[ERROR] Entry failed for {candidate.get('symbol', 'UNKNOWN')}: "
                        f"{type(exc).__name__}: {exc}",
                        "error",
                    )
                    _set_risk_pause(
                        context,
                        now,
                        int(risk_cfg.api_failure_pause_minutes or 0),
                        f"Entry failure: {type(exc).__name__}",
                    )
                    break
                if entered:
                    deployed_capital = get_deployed_capital(context.positions)
                    if cfg.entry_selection_mode == "TOP1":
                        break

        if not ranked_candidates:
            context.log_event("[MAIN] No new trade")

        log_positions(context.positions, context.log_event)

        # ------------------------------------------------------------------ #
        # TICK / LTP ENTRY: monitor live price for the rest of this cycle.    #
        # Fires an early entry when price crosses the breakout trigger level   #
        # mid-candle, instead of waiting until the next closed candle.        #
        # Works in LIVE (WebSocket + REST), PAPER (simulated fill), BACKTEST  #
        # (handled separately in BacktestEngine).                             #
        # ------------------------------------------------------------------ #
        _run_tick_entry(context, ranked_candidates, engine)

        # Interruptible sleep — wakes early when stop is requested (web mode)
        _sleep_interruptible(engine.sleep_seconds, context.stop_fn)


def _simulated_now(real_now: datetime) -> datetime:
    """
    Return a datetime that looks like a Wednesday at 10:30, preserving wall-clock
    seconds-within-the-minute so cycle timing still feels real.
    Used only when paper_trading_override=true to bypass weekend/market-hours guards.
    """
    # Find most recent Wednesday (weekday 2) on or before today
    days_back = (real_now.weekday() - 2) % 7  # 0 if already Wednesday
    sim_date = (real_now - timedelta(days=days_back)).date()
    return real_now.replace(
        year=sim_date.year, month=sim_date.month, day=sim_date.day,
        hour=10, minute=30,
    )


def _sleep_interruptible(seconds: float, stop_fn) -> None:
    """Sleep for `seconds` but wake every second to check stop_fn()."""
    end = time.time() + float(seconds)
    while time.time() < end:
        if stop_fn():
            return
        time.sleep(min(1.0, end - time.time()))


def _run_tick_entry(context: Any, ranked_candidates: list, engine: Any) -> None:
    """
    After the normal candle-close entry loop, watch live prices for
    ``ranked_candidates`` that did NOT yet result in a position.
    Fires an early entry if price crosses the breakout trigger.

    Skipped when:
    - No candidates (no signal this cycle)
    - Engine doesn't support tick entry (e.g. delivery_equity)
    - exit_only_mode is active
    - Risk / cooldown guards are already active (they would have blocked
      the normal entry loop too, so we respect them here)
    """
    if not ranked_candidates:
        return
    if getattr(context.config, "exit_only_mode", False):
        return

    from tick_entry.engine_config import get_engine_tick_config
    tick_cfg = get_engine_tick_config(engine.name)
    if not tick_cfg.enabled:
        return

    # Only watch candidates not yet positioned
    watch_list = [
        c for c in ranked_candidates
        if c.get("symbol") not in context.positions
        and not c.get("is_pair")          # pair entries have their own logic
    ]
    if not watch_list:
        return

    try:
        from tick_entry import TickEntryManager
        mgr = TickEntryManager(context)
        mgr.run(watch_list, cycle_remaining_seconds=tick_cfg.timeout_seconds)
    except Exception as exc:
        context.log_event(
            f"[TICK-ENTRY] Monitor error ({engine.name}): {type(exc).__name__}: {exc}",
            "warning",
        )


def _execute_pair_entry(context, candidate, now, deployed_capital):
    cfg = context.config
    engine = context.engine
    pair_config = candidate["pair_config"]
    pair_symbols = pair_config["symbols"]
    pair_id = pair_config["pair_id"]

    if any(pair_symbol in context.positions for pair_symbol in pair_symbols):
        context.log_event(f"[LIMIT] Pair {pair_id} already has an open leg")
        return False

    if cfg.one_trade_per_symbol_per_day and any(pair_symbol in context.traded_symbols_today for pair_symbol in pair_symbols):
        context.log_event(f"[LIMIT] Pair {pair_id} already traded today on one of its legs")
        return False

    if count_open_structures(context.positions) >= cfg.max_open_positions:
        context.log_event(f"[LIMIT] Max open position structures would be exceeded by pair {pair_id}")
        return False

    trade_key = None
    max_trades_per_day = 0
    if hasattr(engine, "get_trade_frequency_key") and hasattr(engine, "get_max_trades_per_day"):
        trade_key = engine.get_trade_frequency_key(pair_id, candidate.get("analytics"))
        max_trades_per_day = engine.get_max_trades_per_day()
        if trade_key and max_trades_per_day > 0:
            trade_count = int(context.trade_counts_today.get(trade_key, 0))
            if trade_count >= max_trades_per_day:
                context.log_event(f"[LIMIT] {trade_key} reached max intraday option trades for the day ({trade_count}/{max_trades_per_day})")
                return False

    leg_entries = []
    pair_premium = sum(leg["latest_close"] for leg in candidate["legs"])
    if pair_premium <= 0:
        context.log_event(f"[RISK] Invalid pair premium for {pair_id}", "warning")
        return False

    per_trade_cap_lots = int(cfg.max_capital_per_trade / pair_premium)
    remaining_deployable = max(0.0, cfg.max_capital_deployed - deployed_capital)
    deploy_cap_lots = int(remaining_deployable / pair_premium)
    max_pair_lots = min(per_trade_cap_lots, deploy_cap_lots)

    for leg in candidate["legs"]:
        leg_symbol = leg["symbol"]
        leg_entry_price = leg["latest_close"]
        leg_atr = leg.get("atr", 0.0)
        leg_stop_data = atr_stop_from_value(candidate["signal"], leg_entry_price, leg_atr, cfg.atr_stop_multiplier)
        if leg_stop_data["stop_distance"] <= 0:
            max_pair_lots = 0
            break

        leg_lot_size = get_contract_lot_size(leg_symbol)
        leg_sizing = atr_position_size(
            capital=cfg.capital,
            entry_price=leg_entry_price,
            atr_value=leg_atr,
            atr_multiplier=cfg.atr_stop_multiplier,
            risk_percent=cfg.risk_percent,
        )
        risk_lots = leg_sizing["quantity"] // leg_lot_size
        leg_qty_cap = engine.apply_entry_allocation_limit(
            leg_symbol,
            max(leg_lot_size, max_pair_lots * leg_lot_size),
            leg_entry_price,
            context.positions,
            cfg.capital,
        )
        allocation_lots = leg_qty_cap // leg_lot_size
        max_pair_lots = min(max_pair_lots, risk_lots, allocation_lots)
        leg_entries.append(
            {
                "symbol": leg_symbol,
                "entry_price": leg_entry_price,
                "atr": leg_atr,
                "stop_data": leg_stop_data,
                "lot_size": leg_lot_size,
                "analytics": leg.get("analytics"),
            }
        )

    if max_pair_lots <= 0:
        context.log_event(f"[RISK] Pair quantity is 0 for {pair_id} after limits", "warning")
        return False

    estimated_trade_capital = 0.0
    entered_pair_symbols = []
    pair_target_price = calculate_target_price(candidate["signal"], pair_premium, pair_premium * (cfg.target_percent / 100.0))
    pair_stop_loss_price = pair_premium * (1 - (cfg.sl_percent / 100.0)) if candidate["signal"] == "BUY" else pair_premium * (1 + (cfg.sl_percent / 100.0))
    context.log_event(
        f"[PAIR ENTRY] Executing bounded range pair {pair_id} | Underlying={candidate['analytics'].get('underlying_price', 0.0):.2f} | Range={pair_config['lower_strike']}-{pair_config['upper_strike']} | Lots={max_pair_lots} | Combined SL={pair_stop_loss_price:.2f} | Combined Target={pair_target_price:.2f}"
    )

    for leg_entry in leg_entries:
        leg_symbol = leg_entry["symbol"]
        qty = max_pair_lots * leg_entry["lot_size"]
        requested_qty = qty
        target_distance = leg_entry["stop_data"]["stop_distance"] * cfg.target_risk_reward
        trailing_distance = leg_entry["atr"] * cfg.trailing_atr_multiplier
        target_price = calculate_target_price(candidate["signal"], leg_entry["entry_price"], target_distance)
        trailing_stop = float(leg_entry["stop_data"]["stop_loss_price"])
        trailing_activation_distance = max(
            float(trailing_distance or 0.0),
            float(leg_entry["stop_data"].get("stop_distance") or 0.0) * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
        )
        leg_targets = calculate_cost_aware_targets(
            entry_price=float(leg_entry["entry_price"]),
            quantity=int(qty),
            asset_class="INTRADAY_OPTIONS",
            risk_profile=cfg.risk_style_name,
            signal_strength=float(candidate.get("score") or 0.5),
            side=candidate["signal"],
        )
        if not _validate_intraday_options_live_entry(
            context,
            leg_symbol,
            qty,
            leg_entry["entry_price"],
            leg_targets,
        ):
            continue
        try:
            log_order_signal_banner(
                context.log_event,
                "PAIR LEG ENTRY",
                [
                    f"Structure: {pair_id}",
                    f"Leg: {leg_symbol}",
                    f"Side: {candidate['signal']}",
                    f"Qty: {qty}",
                    f"Entry: {leg_entry['entry_price']:.2f}",
                    f"Stop: {leg_entry['stop_data']['stop_loss_price']:.2f}",
                    f"Target: {target_price:.2f}",
                    f"Trail: {trailing_stop:.2f}",
                ],
            )
            order_type = _resolve_entry_order_type(context)
            limit_price = None
            if order_type == "LIMIT":
                limit_price = _resolve_limit_price(
                    leg_entry["entry_price"],
                    candidate["signal"],
                    context.runtime_config.orders.entry_limit_price_buffer_pct,
                )
            order_result = context.place_order(
                candidate["signal"],
                qty,
                leg_symbol,
                note=f"Pair entry {pair_id}",
                product=engine.order_product,
                entry_price=leg_entry["entry_price"],
                order_type=order_type,
                price=limit_price,
                enforce_spread_check=True,
            )
            if order_result is None or int(order_result.filled_quantity or 0) <= 0:
                context.log_event(
                    f"[ORDER] Pair leg not filled yet | Symbol={leg_symbol} | Requested={requested_qty}",
                    "warning",
                )
                continue
            qty = int(order_result.filled_quantity)
            if qty < requested_qty:
                context.log_event(
                    f"[ORDER] Pair leg partial fill | Symbol={leg_symbol} | Filled={qty}/{requested_qty}",
                    "warning",
                )
            context.log_event(
                f"[ORDER] Pair leg accepted | Symbol={leg_symbol} | OrderId={order_result.order_id} | "
                f"Filled={qty}/{requested_qty}"
            )
        except Exception:
            if entered_pair_symbols:
                context.log_event(
                    f"[PAIR EXIT] Pair entry failed on {leg_symbol}; closing already-entered legs to avoid partial exposure",
                    "warning",
                )
                position_flow.close_position_symbols(
                    engine,
                    context.positions,
                    entered_pair_symbols,
                    reason=f"Pair sync unwind {pair_id}",
                    trade_book=context.trade_book,
                    trade_store=context.trade_store,
                    place_order=context.place_order,
                    log_order_signal_banner=lambda title, lines: log_order_signal_banner(context.log_event, title, lines),
                    fetch_data=context.fetch_data,
                    log_event=context.log_event,
                    transaction_cost_model_enabled=TRANSACTION_COST_MODEL_ENABLED,
                    slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
                    exit_time=now,
                )
            raise
        actual_entry_price = float(order_result.average_price or leg_entry["entry_price"])
        actual_stop_data = atr_stop_from_value(
            candidate["signal"],
            actual_entry_price,
            leg_entry["atr"],
            cfg.atr_stop_multiplier,
        )
        actual_target_distance = actual_stop_data["stop_distance"] * cfg.target_risk_reward
        actual_target_price = calculate_target_price(
            candidate["signal"],
            actual_entry_price,
            actual_target_distance,
        )
        actual_trailing_distance = leg_entry["atr"] * cfg.trailing_atr_multiplier
        actual_trailing_stop = float(actual_stop_data["stop_loss_price"])
        actual_trailing_activation_distance = max(
            float(actual_trailing_distance or 0.0),
            float(actual_stop_data.get("stop_distance") or 0.0)
            * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
        )
        context.positions[leg_symbol] = build_position(
            symbol=leg_symbol,
            side=candidate["signal"],
            quantity=qty,
            entry_price=actual_entry_price,
            stop_loss=actual_stop_data["stop_loss_price"],
            target=actual_target_price,
            trailing_stop=actual_trailing_stop,
            trailing_distance=actual_trailing_distance,
            trailing_activation_distance=actual_trailing_activation_distance,
            trailing_active=False,
            atr=leg_entry["atr"],
            stop_distance=actual_stop_data["stop_distance"],
            lot_size=leg_entry["lot_size"],
            entry_analytics=leg_entry["analytics"],
            pair_id=pair_id,
            pair_mode=pair_config["mode"],
            pair_underlying=pair_config["underlying"],
            pair_lower_strike=pair_config["lower_strike"],
            pair_upper_strike=pair_config["upper_strike"],
            pair_symbols=pair_symbols,
            pair_entry_total_premium=pair_premium,
            pair_stop_loss_price=pair_stop_loss_price,
            pair_target_price=pair_target_price,
            entry_time=now.isoformat(),
            engine_name=engine.name,
            execution_mode=context.config.execution_mode,
            order_product=engine.order_product,
            trade_identity=pair_config["underlying"],
        )
        entered_pair_symbols.append(leg_symbol)
        context.traded_symbols_today.add(leg_symbol)
        estimated_trade_capital += actual_entry_price * qty

    if trade_key:
        context.trade_counts_today[trade_key] = int(context.trade_counts_today.get(trade_key, 0)) + 1
    context.last_entry_time = time.time()
    context.log_event(f"[RISK] Updated deployed capital: {deployed_capital + estimated_trade_capital:.2f}")
    persist_runtime_state(context)
    return True


def _execute_single_entry(context, candidate, now, deployed_capital, cycle_state):
    del cycle_state
    cfg = context.config
    engine = context.engine
    symbol = candidate["symbol"]

    if symbol in context.positions:
        context.log_event(f"[LIMIT] {symbol} already has an open position")
        return False

    trade_identity = candidate.get("trade_identity", symbol)
    if cfg.one_trade_per_symbol_per_day and trade_identity in context.traded_symbols_today:
        context.log_event(f"[LIMIT] {trade_identity} already traded today, skipping")
        return False

    if engine.name == "intraday_options" and cfg.atm_option_config:
        same_underlying_open = any(
            ((position.get("entry_analytics") or {}).get("underlying") == trade_identity)
            for position in context.positions.values()
        )
        if same_underlying_open:
            context.log_event(f"[LIMIT] {trade_identity} already has an open ATM options position")
            return False

    if hasattr(engine, "get_trade_frequency_key") and hasattr(engine, "get_max_trades_per_day"):
        trade_key = engine.get_trade_frequency_key(symbol, candidate.get("analytics"))
        max_trades_per_day = engine.get_max_trades_per_day()
        if trade_key and max_trades_per_day > 0:
            trade_count = int(context.trade_counts_today.get(trade_key, 0))
            if trade_count >= max_trades_per_day:
                context.log_event(f"[LIMIT] {trade_key} reached max intraday option trades for the day ({trade_count}/{max_trades_per_day})")
                return False

    if count_open_structures(context.positions) >= cfg.max_open_positions:
        context.log_event("[LIMIT] Max open position structures reached")
        return False

    entry_price = candidate["latest_close"]
    atr_value = candidate.get("atr", 0.0)
    if engine.name == "intraday_options" and cfg.atm_option_config:
        if hasattr(engine, "get_trend_adaptive_level_spec"):
            level_spec = engine.get_trend_adaptive_level_spec(
                entry_price=entry_price,
                side=candidate["signal"],
                atr=atr_value,
                signal_score=float(candidate.get("score") or 0.0),
                analytics=candidate.get("analytics") or {},
                premium_volatility_distance=float(
                    candidate.get("premium_volatility_distance") or 0.0
                ),
            )
            stop_distance = float(level_spec["stop_distance"])
            stop_loss_price = float(level_spec["stop_loss_price"])
        else:
            stop_distance = entry_price * 0.10
            stop_loss_price = entry_price - stop_distance if candidate["signal"] == "BUY" else entry_price + stop_distance
        stop_data = {
            "atr": atr_value,
            "stop_distance": stop_distance,
            "stop_loss_price": stop_loss_price,
        }
        if cfg.intraday_options_lot_mode == "ONE_LOT":
            qty = get_contract_lot_size(symbol)
        else:
            remaining_deployable = max(0.0, cfg.max_capital_deployed - deployed_capital)
            available_capital = min(
                float(cfg.capital - deployed_capital),
                float(cfg.max_capital_per_trade),
                float(remaining_deployable),
            )
            qty = int(available_capital / entry_price) if entry_price > 0 else 0
    elif engine.name in {"intraday_equity", "delivery_equity"} and hasattr(engine, "get_trend_adaptive_level_spec"):
        level_spec = engine.get_trend_adaptive_level_spec(
            entry_price=entry_price,
            side=candidate["signal"],
            atr=atr_value,
            signal_score=float(candidate.get("score") or 0.0),
            volatility_distance=float(candidate.get("equity_volatility_distance") or 0.0),
        )
        stop_data = {
            "atr": atr_value,
            "stop_distance": float(level_spec["stop_distance"]),
            "stop_loss_price": float(level_spec["stop_loss_price"]),
        }
        qty = position_size(
            cfg.capital,
            entry_price,
            stop_data["stop_loss_price"],
            cfg.risk_percent,
        )
    else:
        stop_data = atr_stop_from_value(candidate["signal"], entry_price, atr_value, cfg.atr_stop_multiplier)
        if stop_data["stop_distance"] <= 0:
            context.log_event(f"[RISK] ATR unavailable for {symbol}, skipping entry", "warning")
            return False
        sizing = atr_position_size(
            capital=cfg.capital,
            entry_price=entry_price,
            atr_value=atr_value,
            atr_multiplier=cfg.atr_stop_multiplier,
            risk_percent=cfg.risk_percent,
        )
        qty = sizing["quantity"]

    qty = apply_capital_limits_to_quantity(
        qty,
        entry_price,
        cfg.max_capital_per_trade,
        cfg.max_capital_deployed,
        deployed_capital,
        context.log_event,
    )
    qty = engine.apply_entry_allocation_limit(symbol, qty, entry_price, context.positions, cfg.capital)
    if qty <= 0:
        context.log_event(f"[RISK] Quantity is 0 for {symbol} after applying risk and capital limits, skipping", "warning")
        return False

    if engine.name == "delivery_equity" and candidate["signal"] == "BUY":
        nifty_history = context.fetch_data(
            engine.nifty_trend_symbol,
            period=engine.data_period,
            interval="1d",
        )
        trend_ok, trend_reason = engine.passes_nifty_trend_guard(nifty_history, now)
        if not trend_ok:
            context.log_event(f"[FILTER] Skipping {symbol}: {trend_reason}")
            return False

    if not should_enter_trade(
        candidate,
        context,
        entry_price=float(entry_price),
        quantity=int(qty),
    ):
        return False

    estimated_trade_capital = entry_price * qty
    if engine.name == "intraday_options" and cfg.atm_option_config:
        if hasattr(engine, "get_trend_adaptive_level_spec"):
            level_spec = engine.get_trend_adaptive_level_spec(
                entry_price=entry_price,
                side=candidate["signal"],
                atr=atr_value,
                signal_score=float(candidate.get("score") or 0.0),
                analytics=candidate.get("analytics") or {},
            )
            target_price = float(level_spec["level3_target"])
            trailing_distance = float(level_spec["trailing_distance"])
            trailing_stop = float(level_spec["stop_loss_price"])
            trailing_activation_distance = float(level_spec["trailing_activation_distance"])
        else:
            target_distance = entry_price * 0.20
            trailing_distance = entry_price * 0.075
            target_price = calculate_target_price(candidate["signal"], entry_price, target_distance)
            trailing_stop = float(stop_data["stop_loss_price"])
            trailing_activation_distance = max(
                float(trailing_distance or 0.0),
                float(stop_data.get("stop_distance") or 0.0) * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
            )
    elif engine.name in {"intraday_equity", "delivery_equity"} and hasattr(engine, "get_trend_adaptive_level_spec"):
        level_spec = engine.get_trend_adaptive_level_spec(
            entry_price=entry_price,
            side=candidate["signal"],
            atr=atr_value,
            signal_score=float(candidate.get("score") or 0.0),
            volatility_distance=float(candidate.get("equity_volatility_distance") or 0.0),
        )
        target_price = float(level_spec["level3_target"])
        trailing_distance = float(level_spec["trailing_distance"])
        trailing_stop = float(level_spec["stop_loss_price"])
        trailing_activation_distance = float(level_spec["trailing_activation_distance"])
    else:
        target_distance = stop_data["stop_distance"] * cfg.target_risk_reward
        trailing_distance = atr_value * cfg.trailing_atr_multiplier
        target_price = calculate_target_price(candidate["signal"], entry_price, target_distance)
        trailing_stop = float(stop_data["stop_loss_price"])
        trailing_activation_distance = max(
            float(trailing_distance or 0.0),
            float(stop_data.get("stop_distance") or 0.0) * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
        )

    if (
        TRANSACTION_COST_MODEL_ENABLED
        and engine.name == "intraday_equity"
        and symbol.endswith(".NS")
        and ":" not in symbol
    ):
        breakdown = estimate_intraday_equity_round_trip_cost(
            entry_side=str(candidate.get("signal") or "BUY"),
            entry_price=float(entry_price),
            exit_price=float(entry_price),
            quantity=int(qty),
            slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
        )
        est_cost = float(breakdown.total)
        expected_edge_points = float(entry_price) * float(candidate.get("score") or 0.0) * float(EXPECTED_EDGE_SCORE_MULTIPLIER or 1.0)
        expected_edge_rupees = expected_edge_points * int(qty)
        required_edge = (est_cost * float(MIN_EDGE_TO_COST_RATIO or 1.0)) + float(COST_EDGE_BUFFER_RUPEES or 0.0)
        if expected_edge_rupees < required_edge:
            context.log_event(
                f"[FILTER] Skipping {symbol} due to low edge vs cost | Score={candidate.get('score', 0.0):.4f} | ExpectedEdge~{expected_edge_rupees:.2f} | EstCost~{est_cost:.2f} | Required>={required_edge:.2f}"
            )
            return False

    if engine.name == "intraday_options" and cfg.atm_option_config:
        preflight_targets = calculate_cost_aware_targets(
            entry_price=float(entry_price),
            quantity=int(qty),
            asset_class="INTRADAY_OPTIONS",
            risk_profile=cfg.risk_style_name,
            signal_strength=float(candidate.get("score") or 0.5),
            side=candidate["signal"],
        )
        if not _validate_intraday_options_live_entry(
            context,
            symbol,
            qty,
            entry_price,
            preflight_targets,
        ):
            return False

    context.log_event(
        f"[ENTRY] Executing trade on {symbol} | Signal={candidate['signal']} | Agree={candidate['agreement_count']} | "
        f"Score={candidate['score']:.4f} | ATR={atr_value:.2f} | "
        f"Stop={float(candidate.get('stop_loss', stop_data['stop_loss_price'])):.2f} | Qty={qty}"
    )
    entry_lines = [
        f"Symbol: {symbol}",
        f"Side: {candidate['signal']}",
        f"Qty: {qty}",
        f"Entry: {entry_price:.2f}",
        f"Stop: {float(candidate.get('stop_loss', stop_data['stop_loss_price'])):.2f}",
        f"Target: {float(candidate.get('target', target_price)):.2f}",
        f"Trail: {float(candidate.get('trailing_stop', trailing_stop)):.2f}",
        f"Score: {candidate['score']:.4f}",
    ]
    if candidate.get("analytics"):
        analytics = candidate["analytics"]
        entry_lines.append(f"Underlying: {analytics.get('underlying', 'N/A')} @ {analytics.get('underlying_price', 0.0):.2f}")
        entry_lines.append(
            f"OptionType: {(analytics.get('option_type') or 'N/A').upper()} | StrikeMode: {cfg.atm_option_config.get('strike_offset_mode', 'N/A') if cfg.atm_option_config else 'N/A'}"
        )
    log_order_signal_banner(context.log_event, "SINGLE ENTRY", entry_lines)
    requested_qty = qty
    order_type = _resolve_entry_order_type(context)
    limit_price = None
    if order_type == "LIMIT":
        limit_price = _resolve_limit_price(
            entry_price,
            candidate["signal"],
            context.runtime_config.orders.entry_limit_price_buffer_pct,
        )
    order_result = context.place_order(
        candidate["signal"],
        qty,
        symbol,
        note="Entry",
        product=engine.order_product,
        entry_price=entry_price,
        order_type=order_type,
        price=limit_price,
        enforce_spread_check=True,
    )
    if order_result is None or int(order_result.filled_quantity or 0) <= 0:
        context.log_event(
            f"[ORDER] Entry not filled yet | Symbol={symbol} | Requested={requested_qty}",
            "warning",
        )
        return False
    qty = int(order_result.filled_quantity)
    if qty < requested_qty:
        context.log_event(
            f"[ORDER] Partial fill on {symbol} | Filled={qty}/{requested_qty} | "
            "recalculating position using actual filled quantity",
            "warning",
        )
    actual_entry_price = float(order_result.average_price or entry_price)
    estimated_trade_capital = actual_entry_price * qty
    actual_targets = resolve_trade_targets(
        engine_name=engine.name,
        entry_price=actual_entry_price,
        quantity=qty,
        risk_style_name=cfg.risk_style_name,
        signal_strength=float(candidate.get("score") or 0.5),
        side=candidate["signal"],
        atr=float(atr_value or 0.0),
        trailing_atr_multiplier=cfg.trailing_atr_multiplier,
        engine_helper=engine,
    )
    trailing_distance = float(actual_targets["trailing_distance"])
    trailing_activation_distance = float(actual_targets["trailing_activation_distance"])
    context.log_event(
        f"[ORDER] Entry accepted | Symbol={symbol} | OrderId={order_result.order_id} | Filled={qty}/{requested_qty}"
    )
    position_extra_fields = {
        "trade_identity": trade_identity,
        "dynamic_atm_roll_enabled": bool(engine.name == "intraday_options" and cfg.atm_option_config),
        "strike_offset": candidate.get("strike_offset", 0),
        "strike_offset_mode": candidate.get("strike_offset_mode", "ATM"),
        "entry_underlying_price": (candidate.get("analytics") or {}).get("underlying_price"),
        "asset_class": actual_targets["asset_class"],
        "risk_profile": actual_targets["risk_profile"],
        "min_breakeven_price": actual_targets["min_breakeven_price"],
        "expected_costs": actual_targets["expected_costs"],
        "expected_net_profit": actual_targets["expected_net_profit"],
        "cost_to_profit_ratio": actual_targets["cost_to_profit_ratio"],
        "cost_breakdown": actual_targets["cost_breakdown"],
    }
    if engine.name == "intraday_options" and cfg.atm_option_config and hasattr(engine, "build_trend_adaptive_position"):
        context.positions[symbol] = engine.build_trend_adaptive_position(
            symbol=symbol,
            side=candidate["signal"],
            quantity=qty,
            entry_price=actual_entry_price,
            atr=float(atr_value or 0.0),
            signal_score=float(candidate.get("score") or 0.0),
            analytics=candidate.get("analytics") or {},
            lot_size=get_contract_lot_size(symbol) if ":" in symbol else 1,
            now=now,
            entry_analytics=candidate.get("analytics"),
            engine_name=engine.name,
            execution_mode=context.config.execution_mode,
            order_product=engine.order_product,
            premium_volatility_distance=float(
                candidate.get("premium_volatility_distance") or 0.0
            ),
            extra_fields=position_extra_fields,
        )
    elif engine.name in {"intraday_equity", "delivery_equity"} and hasattr(engine, "build_trend_adaptive_position"):
        context.positions[symbol] = engine.build_trend_adaptive_position(
            symbol=symbol,
            side=candidate["signal"],
            quantity=qty,
            entry_price=actual_entry_price,
            atr=float(atr_value or 0.0),
            signal_score=float(candidate.get("score") or 0.0),
            now=now,
            engine_name=engine.name,
            execution_mode=context.config.execution_mode,
            order_product=engine.order_product,
            volatility_distance=float(candidate.get("equity_volatility_distance") or 0.0),
            extra_fields=position_extra_fields,
        )
    else:
        context.positions[symbol] = build_position(
            symbol=symbol,
            side=candidate["signal"],
            quantity=qty,
            entry_price=actual_entry_price,
            stop_loss=actual_targets["stop_loss"],
            target=actual_targets["target"],
            trailing_stop=actual_targets["trailing_stop"],
            trailing_distance=trailing_distance,
            trailing_activation_distance=trailing_activation_distance,
            trailing_active=False,
            atr=atr_value,
            stop_distance=float(actual_targets["stop_distance"]),
            lot_size=get_contract_lot_size(symbol) if ":" in symbol else 1,
            entry_analytics=candidate.get("analytics"),
            entry_time=now.isoformat(),
            engine_name=engine.name,
            execution_mode=context.config.execution_mode,
            order_product=engine.order_product,
            **position_extra_fields,
        )
    context.traded_symbols_today.add(trade_identity)
    if hasattr(engine, "get_trade_frequency_key"):
        trade_key = engine.get_trade_frequency_key(symbol, candidate.get("analytics"))
        if trade_key:
            context.trade_counts_today[trade_key] = int(context.trade_counts_today.get(trade_key, 0)) + 1
    context.last_entry_time = time.time()
    context.log_event(f"[RISK] Updated deployed capital: {deployed_capital + estimated_trade_capital:.2f}")
    persist_runtime_state(context)

    # Emit "Why This Trade?" explainability event to the browser (web mode only)
    try:
        from logger import is_web_mode
        if is_web_mode():
            from web import state as _web_state
            _emit_why_trade(context, candidate, symbol, actual_entry_price, qty, now, _web_state)
    except Exception:
        pass

    return True


def _emit_why_trade(context, candidate, symbol, entry_price, qty, now, web_state):
    """Broadcast a why_this_trade WS event so the browser can render the explainability panel."""
    details = candidate.get("details") or {}
    analytics = candidate.get("analytics") or {}
    vwap_bias = candidate.get("vwap_bias") or (
        next((
            s.get("vwap_bias") for s in [context.positions.get(symbol, {})]
        ), None)
    )

    # Build reasons (green checkmarks) from signal details
    reasons_ok: list[str] = []
    reasons_warn: list[str] = []

    if candidate.get("vwap_bias") == "above" or candidate.get("signal") == "BUY":
        reasons_ok.append("Above VWAP")
    elif candidate.get("vwap_bias") == "below" and candidate.get("signal") == "SELL":
        reasons_ok.append("Below VWAP (sell bias)")

    if candidate.get("agreement_count", 0) >= 2:
        reasons_ok.append(f"{candidate['agreement_count']} strategies agree")

    score = candidate.get("score", 0.0)
    if score >= 0.6:
        reasons_ok.append(f"Strong signal score ({score:.2f})")
    elif score >= 0.4:
        reasons_ok.append(f"Moderate signal score ({score:.2f})")

    if analytics.get("delta"):
        delta = float(analytics["delta"])
        if 0.3 <= abs(delta) <= 0.7:
            reasons_ok.append(f"Delta in range ({delta:.2f})")

    # Warnings from current active warnings
    try:
        from web.core.warning_engine import get_active_ids
        active_ids = get_active_ids()
        if any("iv_expansion" in wid for wid in active_ids):
            reasons_warn.append("IV expanding (check premium cost)")
        if any("vwap_broad_weakness" in wid for wid in active_ids):
            reasons_warn.append("Broad market below VWAP")
        if any("lunch_chop_zone" in wid for wid in active_ids):
            reasons_warn.append("Lunch chop zone active")
        if any("late_otm_risk" in wid for wid in active_ids):
            reasons_warn.append("Afternoon session — theta risk elevated")
        if any("vix_elevated" in wid for wid in active_ids):
            reasons_warn.append("VIX elevated — wider ranges expected")
    except Exception:
        pass

    # Signal quality
    try:
        from web.core.warning_engine import compute_signal_quality_score
        sq = compute_signal_quality_score(
            getattr(context, "_last_symbol_snapshots", {}),
            context.session_runtime_state.get("_cached_vix"),
            now,
        )
    except Exception:
        sq = {"score": 0, "label": "N/A"}

    web_state.broadcast({
        "type": "why_this_trade",
        "symbol": symbol,
        "side": candidate.get("signal"),
        "entry_price": round(float(entry_price), 2),
        "qty": qty,
        "score": round(float(score), 4),
        "confidence_pct": min(100, round(float(score) * 100)),
        "strategy": candidate.get("strategy") or candidate.get("signal"),
        "reasons_ok": reasons_ok,
        "reasons_warn": reasons_warn,
        "signal_quality": sq,
        "analytics": {
            "underlying": analytics.get("underlying"),
            "underlying_price": analytics.get("underlying_price"),
            "option_type": analytics.get("option_type"),
            "iv": round(float(analytics["iv"]) * 100, 1) if analytics.get("iv") else None,
            "delta": round(float(analytics["delta"]), 3) if analytics.get("delta") else None,
            "dte": analytics.get("days_to_expiry"),
        },
        "ts": now.strftime("%H:%M:%S"),
    })


def handle_keyboard_interrupt(context):
    context.log_event("\n[MAIN] Bot stopped by user.")
    if len(context.positions) == 0:
        return

    context.log_event(f"\n[MAIN] You have {len(context.positions)} open position(s).")
    close_choice = input("\nClose all positions? (YES/NO) [default NO]: ").strip().upper()
    if close_choice != "YES":
        context.log_event("[MAIN] Positions remain open. Please manage them manually.")
        return

    confirm = input("Are you sure? This will close ALL positions immediately. (YES/NO): ").strip().upper()
    if confirm != "YES":
        context.log_event("[MAIN] Close cancelled. Keeping positions open.")
        return

    context.log_event("[MAIN] Closing all open positions...")
    exit_time = datetime.now()
    for symbol, position in list(context.positions.items()):
        exit_price = position_flow.get_latest_exit_price(
            context.engine,
            symbol,
            position,
            context.fetch_data,
            context.log_event,
        )
        context.log_event(
            f"[MAIN] Closing {symbol}: {position_side(position)} {position_quantity(position)} units at market"
        )
        context.place_order(
            opposite_side(position),
            position_quantity(position),
            symbol,
            note="User-initiated emergency close-out",
            product=context.engine.order_product,
            enforce_spread_check=False,
            enforce_margin_check=False,
            entry_price=exit_price,
        )
        position_flow.record_closed_trade(
            context.trade_book,
            context.trade_store,
            symbol,
            position,
            exit_price,
            "User-initiated emergency close-out",
            exit_time,
            TRANSACTION_COST_MODEL_ENABLED,
            float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
        )
        del context.positions[symbol]
    persist_runtime_state(context)
    context.log_event("[MAIN] All positions closed.")


def summarize_session(context):
    position_flow.summarize_execution_stats(
        context.engine,
        context.config.capital,
        context.positions,
        context.trade_book,
        context.fetch_data,
        context.log_event,
        context.export_trade_book_report,
        TRANSACTION_COST_MODEL_ENABLED,
    )
