from __future__ import annotations

import time
from datetime import datetime

from config import (
    COST_EDGE_BUFFER_RUPEES,
    EXPECTED_EDGE_SCORE_MULTIPLIER,
    MIN_EDGE_TO_COST_RATIO,
    TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER,
    TRANSACTION_COST_MODEL_ENABLED,
    TRANSACTION_SLIPPAGE_PCT_PER_SIDE,
)
from engines.common import (
    apply_capital_limits_to_quantity,
    build_position,
    count_open_structures,
    get_deployed_capital,
    log_positions,
)
from fno_data_fetcher import get_contract_lot_size
from models.position_adapter import opposite_side, position_entry_price, position_quantity, position_side
from risk_manager import (
    atr_position_size,
    atr_stop_from_value,
    calculate_target_price,
    position_size,
)
from transaction_costs import estimate_intraday_equity_round_trip_cost

from . import positions as position_flow
from .context import persist_runtime_state
from .signal_workflow import scan_symbols


def log_order_signal_banner(log_event, title, lines):
    border = "=" * 72
    log_event(border)
    log_event(f"[ORDER] {title}")
    for line in lines:
        log_event(f"[ORDER] {line}")
    log_event(border)


def run_trading_session(context):
    cfg = context.config
    engine = context.engine

    while True:
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
            context.log_event("[MAIN] New day detected, reset traded symbol tracker")
            if engine.order_product == "MIS" and context.positions:
                context.log_event("[MAIN] Clearing stale intraday positions for new day", "warning")
                context.positions.clear()
            context.regime_cache = {}
            persist_runtime_state(context)

        cycle_state = engine.get_cycle_state(now)
        context.log_event("\n==============================")
        context.log_event("New Cycle Started")
        context.log_event("==============================")
        context.log_event(f"[SESSION] {cycle_state['reason']}")

        if cycle_state["force_square_off"]:
            if position_flow.force_square_off_positions(
                engine,
                context.positions,
                context.trade_book,
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
            log_positions(context.positions, context.log_event)
            time.sleep(engine.sleep_seconds)
            continue

        scan_result = scan_symbols(context, now)
        symbol_snapshots = scan_result.symbol_snapshots
        ranked_candidates = scan_result.ranked_candidates
        position_flow.log_ranked_candidates(ranked_candidates, context.log_event)

        if not symbol_snapshots and context.positions:
            context.log_event("[ERROR] No symbol data available for open positions", "error")
        elif not symbol_snapshots:
            context.log_event("[ERROR] No symbol data available in this cycle", "error")
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
                context.place_order,
                lambda title, lines: log_order_signal_banner(context.log_event, title, lines),
                context.fetch_data,
                context.log_event,
                TRANSACTION_COST_MODEL_ENABLED,
                float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
            )
            if state_changed:
                persist_runtime_state(context)

        deployed_capital = get_deployed_capital(context.positions)
        context.log_event(f"[RISK] Current deployed capital: {deployed_capital:.2f}")
        current_time = time.time()
        cooldown_active = engine.cooldown_seconds > 0 and current_time - context.last_entry_time < engine.cooldown_seconds

        if not cycle_state["allow_entries"]:
            context.log_event("[SESSION] New entries disabled in current window")
        elif cooldown_active:
            context.log_event("[COOLDOWN] Skipping new entries")
        else:
            planned_entries = ranked_candidates[:1] if cfg.entry_selection_mode == "TOP1" else ranked_candidates[:cfg.top_n_count]

            for candidate in planned_entries:
                if candidate.get("is_pair"):
                    entered = _execute_pair_entry(context, candidate, now, deployed_capital)
                    if entered:
                        deployed_capital = get_deployed_capital(context.positions)
                        if cfg.entry_selection_mode == "TOP1":
                            break
                    continue

                entered = _execute_single_entry(context, candidate, now, deployed_capital, cycle_state)
                if entered:
                    deployed_capital = get_deployed_capital(context.positions)
                    if cfg.entry_selection_mode == "TOP1":
                        break

        if not ranked_candidates:
            context.log_event("[MAIN] No new trade")

        log_positions(context.positions, context.log_event)
        time.sleep(engine.sleep_seconds)


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
        target_distance = leg_entry["stop_data"]["stop_distance"] * cfg.target_risk_reward
        trailing_distance = leg_entry["atr"] * cfg.trailing_atr_multiplier
        target_price = calculate_target_price(candidate["signal"], leg_entry["entry_price"], target_distance)
        trailing_stop = float(leg_entry["stop_data"]["stop_loss_price"])
        trailing_activation_distance = max(
            float(trailing_distance or 0.0),
            float(leg_entry["stop_data"].get("stop_distance") or 0.0) * float(TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER or 0.0),
        )
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
            order_id = context.place_order(candidate["signal"], qty, leg_symbol, note=f"Pair entry {pair_id}", product=engine.order_product)
            context.log_event(f"[ORDER] Pair leg accepted | Symbol={leg_symbol} | OrderId={order_id}")
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
                    place_order=context.place_order,
                    log_order_signal_banner=lambda title, lines: log_order_signal_banner(context.log_event, title, lines),
                    fetch_data=context.fetch_data,
                    log_event=context.log_event,
                    transaction_cost_model_enabled=TRANSACTION_COST_MODEL_ENABLED,
                    slippage_pct_per_side=float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE or 0.0),
                    exit_time=now,
                )
            raise
        context.positions[leg_symbol] = build_position(
            symbol=leg_symbol,
            side=candidate["signal"],
            quantity=qty,
            entry_price=leg_entry["entry_price"],
            stop_loss=leg_entry["stop_data"]["stop_loss_price"],
            target=target_price,
            trailing_stop=trailing_stop,
            trailing_distance=trailing_distance,
            trailing_activation_distance=trailing_activation_distance,
            trailing_active=False,
            atr=leg_entry["atr"],
            stop_distance=leg_entry["stop_data"]["stop_distance"],
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
            order_product=engine.order_product,
        )
        entered_pair_symbols.append(leg_symbol)
        context.traded_symbols_today.add(leg_symbol)
        estimated_trade_capital += leg_entry["entry_price"] * qty

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
        stop_distance = entry_price * 0.10
        stop_loss_price = entry_price - stop_distance if candidate["signal"] == "BUY" else entry_price + stop_distance
        stop_data = {
            "atr": atr_value,
            "stop_distance": stop_distance,
            "stop_loss_price": stop_loss_price,
        }
        qty = position_size(
            capital=cfg.capital,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            risk_percent=cfg.risk_percent,
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

    estimated_trade_capital = entry_price * qty
    if engine.name == "intraday_options" and cfg.atm_option_config:
        target_distance = entry_price * 0.20
        trailing_distance = entry_price * 0.075
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

    context.log_event(
        f"[ENTRY] Executing trade on {symbol} | Signal={candidate['signal']} | Agree={candidate['agreement_count']} | Score={candidate['score']:.4f} | ATR={atr_value:.2f} | Stop={stop_data['stop_loss_price']:.2f} | Qty={qty}"
    )
    entry_lines = [
        f"Symbol: {symbol}",
        f"Side: {candidate['signal']}",
        f"Qty: {qty}",
        f"Entry: {entry_price:.2f}",
        f"Stop: {stop_data['stop_loss_price']:.2f}",
        f"Target: {target_price:.2f}",
        f"Trail: {trailing_stop:.2f}",
        f"Score: {candidate['score']:.4f}",
    ]
    if candidate.get("analytics"):
        analytics = candidate["analytics"]
        entry_lines.append(f"Underlying: {analytics.get('underlying', 'N/A')} @ {analytics.get('underlying_price', 0.0):.2f}")
        entry_lines.append(
            f"OptionType: {(analytics.get('option_type') or 'N/A').upper()} | StrikeMode: {cfg.atm_option_config.get('strike_offset_mode', 'N/A') if cfg.atm_option_config else 'N/A'}"
        )
    log_order_signal_banner(context.log_event, "SINGLE ENTRY", entry_lines)
    order_id = context.place_order(candidate["signal"], qty, symbol, note="Entry", product=engine.order_product)
    context.log_event(f"[ORDER] Entry accepted | Symbol={symbol} | OrderId={order_id}")
    context.positions[symbol] = build_position(
        symbol=symbol,
        side=candidate["signal"],
        quantity=qty,
        entry_price=entry_price,
        stop_loss=stop_data["stop_loss_price"],
        target=target_price,
        trailing_stop=trailing_stop,
        trailing_distance=trailing_distance,
        trailing_activation_distance=trailing_activation_distance,
        trailing_active=False,
        atr=atr_value,
        stop_distance=stop_data["stop_distance"],
        lot_size=get_contract_lot_size(symbol) if ":" in symbol else 1,
        entry_analytics=candidate.get("analytics"),
        entry_time=now.isoformat(),
        engine_name=engine.name,
        order_product=engine.order_product,
    )
    context.traded_symbols_today.add(trade_identity)
    if hasattr(engine, "get_trade_frequency_key"):
        trade_key = engine.get_trade_frequency_key(symbol, candidate.get("analytics"))
        if trade_key:
            context.trade_counts_today[trade_key] = int(context.trade_counts_today.get(trade_key, 0)) + 1
    context.last_entry_time = time.time()
    context.log_event(f"[RISK] Updated deployed capital: {deployed_capital + estimated_trade_capital:.2f}")
    persist_runtime_state(context)
    return True


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
        )
        position_flow.record_closed_trade(
            context.trade_book,
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
