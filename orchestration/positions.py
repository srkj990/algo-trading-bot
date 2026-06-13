from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from brokers.base import OrderStatus
from engines.common import count_open_structures, get_deployed_capital, update_trailing_stop
from config import (
    INTRADAY_OPTIONS_THETA_EXIT_MIN_MINUTES,
    INTRADAY_OPTIONS_THETA_EXIT_RATIO,
    is_intraday_options_engine_name,
)
from models.position_adapter import (
    calculate_position_pnl,
    opposite_side,
    position_entry_price,
    position_quantity,
    position_side,
)
from reporting import summarize_by_exit_reason
from models.trade_record import TradeRecord
from trade_store import TradeStore
from transaction_costs import estimate_intraday_equity_round_trip_cost


def _coerce_positive_int(value: Any, default: int = 0) -> int:
    try:
        coerced = int(value or 0)
    except (TypeError, ValueError):
        return int(default)
    return max(0, coerced)


def _coerce_positive_float(value: Any) -> float | None:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None


def _resolve_order_fill(order_result: Any, requested_quantity: int, reference_price: float) -> dict[str, Any]:
    requested_quantity = max(0, int(requested_quantity or 0))
    filled_qty = 0
    exit_price = float(reference_price)
    if order_result is not None:
        filled_qty = _coerce_positive_int(getattr(order_result, "filled_quantity", 0))
        average_price = _coerce_positive_float(getattr(order_result, "average_price", None))
        if filled_qty <= 0 and average_price is not None:
            status = getattr(order_result, "status", None)
            if status not in {OrderStatus.REJECTED, OrderStatus.CANCELLED, "REJECTED", "CANCELLED"}:
                filled_qty = requested_quantity
        filled_qty = min(filled_qty, requested_quantity)
        if average_price is not None:
            exit_price = float(average_price)
    return {
        "filled": filled_qty > 0,
        "exit_price": exit_price,
        "filled_quantity": filled_qty,
        "requested_quantity": requested_quantity,
        "order_result": order_result,
    }


def _record_confirmed_exit_fill(
    *,
    position: dict[str, Any],
    filled_quantity: int,
    symbol: str,
    exit_price: float,
    reason: str,
    exit_time: datetime,
    trade_book: list[dict[str, Any]],
    trade_store: TradeStore | None,
    transaction_cost_model_enabled: bool,
    slippage_pct_per_side: float,
) -> None:
    closed_slice = dict(position)
    closed_slice["quantity"] = int(filled_quantity)
    record_closed_trade(
        trade_book,
        trade_store,
        symbol,
        closed_slice,
        float(exit_price),
        reason,
        exit_time,
        transaction_cost_model_enabled,
        slippage_pct_per_side,
    )


def _is_runner_action_executable(position: dict[str, Any], action: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    level_index = int(action.get("level_index", -1))
    target_key = "runner_level1_target" if level_index == 0 else "runner_level2_target"
    target_price = _coerce_positive_float(action.get("target_price") or position.get(target_key))
    latest_price = _coerce_positive_float(snapshot.get("latest_close"))
    if target_price is None or latest_price is None:
        return False
    if position_side(position) == "BUY":
        return latest_price >= target_price
    return latest_price <= target_price


def _runner_action_reference_price(position: dict[str, Any], action: dict[str, Any], fallback: float) -> float:
    level_index = int(action.get("level_index", -1))
    target_key = "runner_level1_target" if level_index == 0 else "runner_level2_target"
    target_price = _coerce_positive_float(action.get("target_price") or position.get(target_key))
    return float(target_price if target_price is not None else fallback)


def normalize_partial_exit_quantity(
    position: dict[str, Any],
    requested_exit_quantity: int,
) -> int:
    current_qty = max(0, int(position_quantity(position)))
    exit_qty = max(0, min(int(requested_exit_quantity or 0), current_qty))
    if exit_qty <= 0:
        return 0

    lot_size = max(1, int(position.get("lot_size") or 1))
    if not is_intraday_options_engine_name(position.get("engine_name")) or lot_size <= 1:
        return exit_qty

    if current_qty % lot_size != 0:
        return current_qty if exit_qty >= current_qty else 0

    exit_qty = (exit_qty // lot_size) * lot_size
    if exit_qty <= 0:
        return 0

    remaining_qty = current_qty - exit_qty
    if remaining_qty == 0:
        return exit_qty
    if remaining_qty < lot_size:
        adjusted_exit_qty = current_qty - lot_size
        return adjusted_exit_qty if adjusted_exit_qty >= lot_size else 0
    return exit_qty


def get_pair_symbols(positions: dict[str, dict[str, Any]], pair_id: str) -> list[str]:
    return [
        symbol
        for symbol, position in positions.items()
        if position.get("pair_id") == pair_id
    ]


def get_pair_position_metrics(
    positions: dict[str, dict[str, Any]],
    pair_symbols: list[str],
    symbol_snapshots: dict[str, dict[str, Any]],
) -> dict[str, float] | None:
    entry_total = 0.0
    current_total = 0.0
    total_pnl = 0.0
    for pair_symbol in pair_symbols:
        position = positions.get(pair_symbol)
        snapshot = symbol_snapshots.get(pair_symbol)
        if not position or not snapshot:
            return None
        entry_total += position_entry_price(position)
        current_total += float(snapshot["latest_close"])
        pnl, _ = calculate_position_pnl(position, float(snapshot["latest_close"]))
        total_pnl += pnl
    return {
        "entry_total_premium": entry_total,
        "current_total_premium": current_total,
        "total_pnl": total_pnl,
    }


def get_latest_exit_price(
    engine: Any,
    symbol: str,
    position: dict[str, Any],
    fetch_data: Callable[..., Any],
    log_event: Callable[..., Any],
    symbol_snapshots: dict[str, dict[str, Any]] | None = None,
) -> float:
    snapshot = (symbol_snapshots or {}).get(symbol)
    if snapshot and snapshot.get("latest_close") is not None:
        return float(snapshot["latest_close"])
    try:
        data = fetch_data(symbol, period=engine.data_period, interval=engine.data_interval)
        if not data.empty:
            return float(data.iloc[-1]["Close"])
    except Exception as exc:
        log_event(f"[REPORT] Could not fetch exit price for {symbol}: {exc}", "warning")
    return float(position.get("best_price") or position_entry_price(position))


def record_closed_trade(
    trade_book: list[dict[str, Any]],
    trade_store: TradeStore | None,
    symbol: str,
    position: dict[str, Any],
    exit_price: float,
    exit_reason: str,
    exit_time: Any,
    transaction_cost_model_enabled: bool,
    slippage_pct_per_side: float,
) -> dict[str, Any]:
    quantity = position_quantity(position)
    entry_price = position_entry_price(position)
    side = position_side(position)
    pnl, pnl_pct = calculate_position_pnl(position, float(exit_price))

    estimated_charges = 0.0
    net_pnl = pnl
    if (
        transaction_cost_model_enabled
        and position.get("engine_name") == "intraday_equity"
        and symbol.endswith(".NS")
        and ":" not in symbol
    ):
        breakdown = estimate_intraday_equity_round_trip_cost(
            entry_side=side,
            entry_price=entry_price,
            exit_price=float(exit_price),
            quantity=quantity,
            slippage_pct_per_side=float(slippage_pct_per_side or 0.0),
        )
        estimated_charges = float(breakdown.total)
        net_pnl = pnl - estimated_charges

    raw_analytics = position.get("entry_analytics") or {}
    safe_analytics: dict[str, Any] = {}
    for k, v in raw_analytics.items():
        try:
            import pandas as _pd
            if isinstance(v, _pd.Series):
                v = v.iloc[0] if len(v) else None
        except Exception:
            pass
        safe_analytics[k] = v

    trade = TradeRecord(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_time=position.get("entry_time"),
        exit_time=exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
        entry_price=entry_price,
        exit_price=float(exit_price),
        pnl=pnl,
        estimated_charges=estimated_charges,
        net_pnl=net_pnl,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
        engine_name=position.get("engine_name"),
        execution_mode=position.get("execution_mode"),
        pair_id=position.get("pair_id"),
        entry_reason=position.get("entry_reason"),
        entry_strategy=position.get("entry_strategy"),
        signal_score=float(position["signal_score"]) if position.get("signal_score") is not None else None,
        entry_analytics=safe_analytics or None,
    )
    payload = trade.to_dict()
    trade_book.append(payload)
    if trade_store is not None:
        trade_store.record_trade(trade)
    return payload


def build_exit_position_lines(position: dict[str, Any], exit_price: float, reason: str) -> list[str]:
    pnl, pnl_pct = calculate_position_pnl(position, exit_price)
    lines = [
        f"Symbol: {position['symbol']}",
        f"EntrySide: {position_side(position)}",
        f"ExitSide: {opposite_side(position)}",
        f"Qty: {position_quantity(position)}",
        f"EntryPrice: {position_entry_price(position):.2f}",
        f"Current/ExitPrice: {float(exit_price):.2f}",
        f"P&L: {pnl:+.2f} ({pnl_pct:+.2f}%)",
        f"Reason: {reason}",
    ]
    if position.get("entry_time"):
        lines.append(f"EntryTime: {format_trade_time(position['entry_time'])}")
    if position.get("stop_loss") is not None:
        lines.append(f"Stop: {float(position['stop_loss']):.2f}")
    if position.get("target") is not None:
        lines.append(f"Target: {float(position['target']):.2f}")
    if position.get("trailing_stop") is not None:
        lines.append(f"Trail: {float(position['trailing_stop']):.2f}")
    return lines


def format_trade_time(raw_value: Any) -> str:
    if not raw_value:
        return "-"
    try:
        return datetime.fromisoformat(raw_value).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(raw_value)


def get_theta_exit_reason(
    position: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime,
) -> str | None:
    if not is_intraday_options_engine_name(position.get("engine_name")):
        return None
    if position_side(position) != "BUY":
        return None
    analytics = snapshot.get("analytics") or {}
    theta = analytics.get("theta")
    premium = analytics.get("option_price") or snapshot.get("latest_close")
    if theta is None or premium in (None, 0):
        return None

    minimum_hold = int(INTRADAY_OPTIONS_THETA_EXIT_MIN_MINUTES or 0)
    entry_time_raw = position.get("entry_time")
    if minimum_hold > 0 and entry_time_raw:
        try:
            held_minutes = (now - datetime.fromisoformat(entry_time_raw)).total_seconds() / 60.0
            if held_minutes < minimum_hold:
                return None
        except ValueError:
            return None

    theta_ratio = abs(float(theta)) / max(float(premium), 0.01)
    if float(theta) < 0 and theta_ratio >= float(INTRADAY_OPTIONS_THETA_EXIT_RATIO or 0.0):
        return f"THETA_EXIT_{theta_ratio:.3f}"
    return None


def execute_partial_position_exit(
    engine: Any,
    position: dict[str, Any],
    exit_quantity: int,
    reason: str,
    exit_price: float,
    now: datetime,
    trade_book: list[dict[str, Any]],
    trade_store: TradeStore | None,
    place_order: Callable[..., Any],
    transaction_cost_model_enabled: bool,
    slippage_pct_per_side: float,
    exit_limit_price_buffer_pct: float = 0.0,
) -> dict[str, Any]:
    exit_qty = normalize_partial_exit_quantity(position, exit_quantity)
    if exit_qty <= 0:
        return {
            "filled": False,
            "exit_price": float(exit_price),
            "filled_quantity": 0,
            "requested_quantity": 0,
            "order_result": None,
        }
    exit_side = opposite_side(position)
    order_type = "MARKET"
    limit_price = None
    if exit_limit_price_buffer_pct > 0:
        order_type = "LIMIT"
        buffer = float(exit_price) * exit_limit_price_buffer_pct
        if exit_side == "SELL":
            limit_price = max(0.01, float(exit_price) - buffer)
        else:
            limit_price = float(exit_price) + buffer
    order_result = place_order(
        exit_side,
        exit_qty,
        position["symbol"],
        note=reason,
        product=(position.get("order_product") or engine.order_product),
        enforce_spread_check=False,
        enforce_margin_check=False,
        entry_price=float(exit_price),
        order_type=order_type,
        price=limit_price,
    )

    fill = _resolve_order_fill(order_result, exit_qty, float(exit_price))
    filled_qty = int(fill["filled_quantity"])
    exit_price = float(fill["exit_price"])

    if filled_qty <= 0:
        return fill

    _record_confirmed_exit_fill(
        position=position,
        filled_quantity=filled_qty,
        symbol=position["symbol"],
        exit_price=float(exit_price),
        reason=reason,
        exit_time=now,
        trade_book=trade_book,
        trade_store=trade_store,
        transaction_cost_model_enabled=transaction_cost_model_enabled,
        slippage_pct_per_side=slippage_pct_per_side,
    )
    position["quantity"] = max(0, int(position.get("quantity") or 0) - filled_qty)
    return fill


def log_trade_book_summary(
    capital: float,
    trade_book: list[dict[str, Any]],
    log_event: Callable[..., Any],
    transaction_cost_model_enabled: bool,
) -> None:
    log_event("[REPORT] CLOSED TRADES")
    from display import trade_book_table
    trade_book_table(log_event, trade_book, capital, transaction_cost_model_enabled)


def close_position_symbols(
    engine: Any,
    positions: dict[str, dict[str, Any]],
    symbols: list[str],
    reason: str,
    trade_book: list[dict[str, Any]],
    trade_store: TradeStore | None,
    place_order: Callable[..., Any],
    log_order_signal_banner: Callable[..., Any],
    fetch_data: Callable[..., Any],
    log_event: Callable[..., Any],
    transaction_cost_model_enabled: bool,
    slippage_pct_per_side: float,
    symbol_snapshots: dict[str, dict[str, Any]] | None = None,
    exit_time: datetime | None = None,
    exit_limit_price_buffer_pct: float = 0.0,
) -> bool:
    changed = False
    exit_time = exit_time or datetime.now()
    for symbol in list(symbols):
        position = positions.get(symbol)
        if not position:
            continue
        exit_price = get_latest_exit_price(engine, symbol, position, fetch_data, log_event, symbol_snapshots=symbol_snapshots)
        log_order_signal_banner("EXIT", build_exit_position_lines(position, exit_price, reason))
        exit_side = opposite_side(position)
        order_type = "MARKET"
        limit_price = None
        if exit_limit_price_buffer_pct > 0:
            order_type = "LIMIT"
            buffer = float(exit_price) * exit_limit_price_buffer_pct
            limit_price = max(0.01, float(exit_price) - buffer) if exit_side == "SELL" else float(exit_price) + buffer
        order_result = place_order(
            exit_side,
            position_quantity(position),
            symbol,
            note=reason,
            product=(position.get("order_product") or engine.order_product),
            enforce_spread_check=False,
            enforce_margin_check=False,
            entry_price=exit_price,
            order_type=order_type,
            price=limit_price,
        )
        fill = _resolve_order_fill(order_result, position_quantity(position), float(exit_price))
        if not fill["filled"]:
            log_event(f"[EXIT] {symbol} {reason} order not filled; position remains open", "warning")
            continue
        _record_confirmed_exit_fill(
            position=position,
            filled_quantity=int(fill["filled_quantity"]),
            symbol=symbol,
            exit_price=float(fill["exit_price"]),
            reason=reason,
            exit_time=exit_time,
            trade_book=trade_book,
            trade_store=trade_store,
            transaction_cost_model_enabled=transaction_cost_model_enabled,
            slippage_pct_per_side=slippage_pct_per_side,
        )
        remaining_qty = max(0, position_quantity(position) - int(fill["filled_quantity"]))
        if remaining_qty <= 0:
            del positions[symbol]
        else:
            position["quantity"] = remaining_qty
        changed = True
    return changed


def build_option_pair_candidate(
    engine: Any,
    pair_config: dict[str, Any] | None,
    symbol_snapshots: dict[str, dict[str, Any]],
    positions: dict[str, dict[str, Any]],
    log_event: Callable[..., Any],
) -> dict[str, Any] | None:
    del engine
    if not pair_config or pair_config.get("mode") != "TWO_LEG_RANGE":
        return None

    pair_id = pair_config["pair_id"]
    if any(position.get("pair_id") == pair_id for position in positions.values()):
        return None

    leg_snapshots = []
    for symbol in pair_config["symbols"]:
        snapshot = symbol_snapshots.get(symbol)
        if not snapshot:
            return None
        leg_snapshots.append(snapshot)

    if any(snapshot["signal"] != "SELL" for snapshot in leg_snapshots):
        return None

    analytics = leg_snapshots[0].get("analytics") or {}
    underlying_price = analytics.get("underlying_price")
    if underlying_price is None:
        return None

    if not (pair_config["lower_strike"] <= underlying_price <= pair_config["upper_strike"]):
        log_event(
            f"[PAIR] Underlying {underlying_price:.2f} is outside configured range "
            f"{pair_config['lower_strike']}-{pair_config['upper_strike']}, skipping pair entry"
        )
        return None

    total_premium = sum(snapshot["latest_close"] for snapshot in leg_snapshots)
    average_atr = sum(snapshot["atr"] for snapshot in leg_snapshots) / len(leg_snapshots)
    total_score = sum(snapshot["score"] for snapshot in leg_snapshots)
    return {
        "symbol": pair_id,
        "signal": pair_config.get("entry_side", "SELL"),
        "agreement_count": len(leg_snapshots),
        "score": total_score,
        "latest_close": total_premium,
        "atr": average_atr,
        "analytics": {
            "underlying": analytics.get("underlying"),
            "underlying_price": underlying_price,
        },
        "is_pair": True,
        "pair_config": pair_config,
        "legs": [
            {
                "symbol": symbol,
                "latest_close": symbol_snapshots[symbol]["latest_close"],
                "atr": symbol_snapshots[symbol]["atr"],
                "analytics": symbol_snapshots[symbol].get("analytics"),
                "score": symbol_snapshots[symbol]["score"],
            }
            for symbol in pair_config["symbols"]
        ],
    }


def parse_trade_day(raw_value: Any) -> date:
    try:
        return date.fromisoformat(raw_value)
    except (TypeError, ValueError):
        return datetime.now().date()


def save_runtime_state(
    engine_name: str,
    positions: dict[str, dict[str, Any]],
    traded_symbols_today: set[str],
    trade_counts_today: dict[str, int],
    active_trade_day: date,
    last_entry_time: float,
    regime_cache: dict[str, Any],
    session_runtime_state: dict[str, Any] | None = None,
    engine_runtime_state: dict[str, Any] | Callable[..., Any] | None = None,
    save_engine_state: Callable[..., Any] | None = None,
) -> None:
    if save_engine_state is None and callable(engine_runtime_state):
        save_engine_state = engine_runtime_state
        engine_runtime_state = {}
    if save_engine_state is None:
        raise TypeError("save_engine_state callback is required")

    save_engine_state(
        engine_name=engine_name,
        positions=positions,
        traded_symbols_today=traded_symbols_today,
        trade_counts_today=trade_counts_today,
        active_trade_day=active_trade_day,
        last_entry_time=last_entry_time,
        regime_cache=regime_cache,
        session_runtime_state=session_runtime_state or {},
        engine_runtime_state=engine_runtime_state or {},
    )


def log_ranked_candidates(candidates: list[dict[str, Any]], log_event: Callable[..., Any]) -> None:
    from display import ranked_candidates_table
    ranked_candidates_table(log_event, candidates)


def summarize_execution_stats(
    engine,
    capital,
    positions,
    trade_book,
    fetch_data,
    log_event,
    export_trade_book_report,
    transaction_cost_model_enabled,
):
    deployed_capital = get_deployed_capital(positions)
    open_count = len(positions)
    open_structure_count = count_open_structures(positions)

    from display import fmt_header
    width = 50
    border = "=" * width
    log_event(f"\n{border}")
    log_event(fmt_header("[STATS] Execution Summary"))
    log_event(border)
    log_event(f"[STATS]  {'Starting capital':<26}{capital:.2f}")
    log_event(f"[STATS]  {'Open positions':<26}{open_count}")
    log_event(f"[STATS]  {'Open structures':<26}{open_structure_count}")
    log_event(f"[STATS]  {'Deployed capital':<26}{deployed_capital:.2f}")
    log_event(f"[STATS]  {'Capital reserve':<26}{max(0.0, capital - deployed_capital):.2f}")
    log_trade_book_summary(capital, trade_book, log_event, transaction_cost_model_enabled)

    try:
        report_path = export_trade_book_report(trade_book, engine_name=engine.name)
        if report_path:
            log_event(f"[REPORT] Trade report exported: {report_path}")
    except Exception as exc:
        log_event(f"[REPORT] Failed to export trade report: {exc}", "warning")

    from display import fmt_pnl, fmt_side

    if open_count == 0:
        log_event("[STATS] No open positions to evaluate for unrealized P/L")
        log_event("=" * 50)
        return

    total_market_value = 0.0
    total_unrealized = 0.0
    long_pnl = 0.0
    short_pnl = 0.0
    long_count = 0
    short_count = 0
    long_positions = []
    short_positions = []
    for symbol, position in positions.items():
        try:
            data = fetch_data(symbol, period=engine.data_period, interval=engine.data_interval)
        except Exception as exc:
            log_event(f"[STATS] Could not fetch latest data for {symbol}: {exc}", "warning")
            continue

        if data.empty:
            log_event(f"[STATS] No latest data for {symbol}, skipping P/L calculation", "warning")
            continue

        latest_close = float(data.iloc[-1]["Close"])
        market_value = latest_close * position_quantity(position)
        total_market_value += market_value
        pnl, pnl_pct = calculate_position_pnl(position, latest_close)

        bucket = long_positions if position_side(position) == "BUY" else short_positions
        bucket.append({"symbol": symbol, "pnl": pnl, "pnl_pct": pnl_pct})
        if position_side(position) == "BUY":
            long_pnl += pnl
            long_count += 1
        else:
            short_pnl += pnl
            short_count += 1
        total_unrealized += pnl
        log_event(
            f"[STATS]  {symbol}  {fmt_side(position_side(position))}  "
            f"qty={position_quantity(position)}  "
            f"entry={position_entry_price(position):.2f}  current={latest_close:.2f}  "
            f"mktval={market_value:.2f}  {fmt_pnl(pnl, pnl_pct)}"
        )

    log_event("\n" + "-" * 50)
    log_event(f"[STATS] Long  count={long_count}  P&L {fmt_pnl(long_pnl)}")
    for pos in sorted(long_positions, key=lambda x: x["pnl"], reverse=True):
        log_event(f"[STATS]   {pos['symbol']}  {fmt_pnl(pos['pnl'], pos['pnl_pct'])}")

    log_event(f"[STATS] Short count={short_count}  P&L {fmt_pnl(short_pnl)}")
    for pos in sorted(short_positions, key=lambda x: x["pnl"], reverse=True):
        log_event(f"[STATS]   {pos['symbol']}  {fmt_pnl(pos['pnl'], pos['pnl_pct'])}")

    log_event(f"\n[STATS] Total market value  {total_market_value:.2f}")
    log_event(f"[STATS] Total unrealized    {fmt_pnl(total_unrealized)}")
    if deployed_capital > 0:
        ret_pct = (total_unrealized / deployed_capital) * 100
        log_event(f"[STATS] Return on deployed  {fmt_pnl(ret_pct)}%")
    log_event("=" * 50)


def force_square_off_positions(
    engine,
    positions,
    trade_book,
    trade_store,
    place_order,
    log_order_signal_banner,
    fetch_data,
    log_event,
    transaction_cost_model_enabled,
    slippage_pct_per_side,
    exit_limit_price_buffer_pct: float = 0.0,
):
    if not positions:
        return False

    log_event("[SQUAREOFF] Closing all open positions")
    changed = False
    exit_time = datetime.now()
    for symbol, position in list(positions.items()):
        exit_price = get_latest_exit_price(engine, symbol, position, fetch_data, log_event)
        log_order_signal_banner("FORCE SQUARE OFF", build_exit_position_lines(position, exit_price, "Intraday square-off"))
        exit_side = opposite_side(position)
        order_type = "MARKET"
        limit_price = None
        if exit_limit_price_buffer_pct > 0:
            order_type = "LIMIT"
            buffer = float(exit_price) * exit_limit_price_buffer_pct
            limit_price = max(0.01, float(exit_price) - buffer) if exit_side == "SELL" else float(exit_price) + buffer
        order_result = place_order(
            exit_side,
            position_quantity(position),
            symbol,
            note="Intraday square-off",
            product=(position.get("order_product") or engine.order_product),
            enforce_spread_check=False,
            enforce_margin_check=False,
            entry_price=exit_price,
            order_type=order_type,
            price=limit_price,
        )
        fill = _resolve_order_fill(order_result, position_quantity(position), float(exit_price))
        if not fill["filled"]:
            log_event(f"[SQUAREOFF] {symbol} order not filled; position remains open", "warning")
            continue
        _record_confirmed_exit_fill(
            position=position,
            filled_quantity=int(fill["filled_quantity"]),
            symbol=symbol,
            exit_price=float(fill["exit_price"]),
            reason="Intraday square-off",
            exit_time=exit_time,
            trade_book=trade_book,
            trade_store=trade_store,
            transaction_cost_model_enabled=transaction_cost_model_enabled,
            slippage_pct_per_side=slippage_pct_per_side,
        )
        remaining_qty = max(0, position_quantity(position) - int(fill["filled_quantity"]))
        if remaining_qty <= 0:
            del positions[symbol]
        else:
            position["quantity"] = remaining_qty
        changed = True
    return changed


def manage_open_positions(
    engine,
    positions,
    symbol_snapshots,
    now,
    trade_book,
    trade_store,
    place_order,
    log_order_signal_banner,
    fetch_data,
    log_event,
    transaction_cost_model_enabled,
    slippage_pct_per_side,
    exit_limit_price_buffer_pct: float = 0.0,
):
    state_changed = False
    processed_pair_ids = set()
    for symbol, position in list(positions.items()):
        pair_id = position.get("pair_id")
        if pair_id and pair_id in processed_pair_ids:
            continue

        if pair_id:
            pair_symbols = get_pair_symbols(positions, pair_id)
            expected_pair_symbols = position.get("pair_symbols") or []
            if expected_pair_symbols and len(pair_symbols) < len(expected_pair_symbols):
                log_event(f"[PAIR EXIT] {pair_id} leg synchronization guard triggered")
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair sync guard for {pair_id}",
                    trade_book=trade_book,
                    trade_store=trade_store,
                    place_order=place_order,
                    log_order_signal_banner=log_order_signal_banner,
                    fetch_data=fetch_data,
                    log_event=log_event,
                    transaction_cost_model_enabled=transaction_cost_model_enabled,
                    slippage_pct_per_side=slippage_pct_per_side,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

            pair_snapshots = [symbol_snapshots.get(pair_symbol) for pair_symbol in pair_symbols]
            if any(snapshot is None for snapshot in pair_snapshots):
                log_event(f"[ERROR] Missing latest data for option pair {pair_id}", "error")
                continue

            underlying_price = None
            for snapshot in pair_snapshots:
                analytics = snapshot.get("analytics") or {}
                if analytics.get("underlying_price") is not None:
                    underlying_price = analytics["underlying_price"]
                    break

            lower_strike = position.get("pair_lower_strike")
            upper_strike = position.get("pair_upper_strike")
            if (
                underlying_price is not None
                and lower_strike is not None
                and upper_strike is not None
                and not (lower_strike <= underlying_price <= upper_strike)
            ):
                log_event(f"[PAIR EXIT] {pair_id} underlying {underlying_price:.2f} breached range {lower_strike}-{upper_strike}")
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair range break {underlying_price:.2f}",
                    trade_book=trade_book,
                    trade_store=trade_store,
                    place_order=place_order,
                    log_order_signal_banner=log_order_signal_banner,
                    fetch_data=fetch_data,
                    log_event=log_event,
                    transaction_cost_model_enabled=transaction_cost_model_enabled,
                    slippage_pct_per_side=slippage_pct_per_side,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

        snapshot = symbol_snapshots.get(symbol)
        if not snapshot:
            log_event(f"[ERROR] Missing latest data for open symbol {symbol}", "error")
            continue

        trailing_updated = update_trailing_stop(position, snapshot["latest_close"], engine.trailing_percent)
        if trailing_updated:
            log_event(
                f"[TRAILING] {symbol} trailing stop updated to {position['trailing_stop']:.2f} "
                f"(best_price={position['best_price']:.2f})"
            )
            state_changed = True

        if hasattr(engine, "get_runner_partial_exit"):
            runner_adjusted_this_cycle = False
            for _ in range(3):
                action = engine.get_runner_partial_exit(position, snapshot, now)
                if not action:
                    break
                if not _is_runner_action_executable(position, action, snapshot):
                    log_event(
                        f"[RUNNER] {symbol} {action['reason']} touched but not executable at "
                        f"{float(snapshot['latest_close']):.2f}; waiting for live price confirmation",
                        "warning",
                    )
                    break
                exit_price = _runner_action_reference_price(position, action, float(snapshot["latest_close"]))
                exit_quantities = list(position.get("runner_exit_quantities") or [0, 0, 0])
                current_qty = int(position.get("quantity") or 0)
                two_lot_trailing_only_runner = (
                    is_intraday_options_engine_name(position.get("engine_name"))
                    and int(action.get("level_index", -1)) == 0
                    and int(exit_quantities[0] or 0) > 0
                    and int(exit_quantities[1] or 0) == 0
                    and current_qty == int(exit_quantities[0] or 0) + int(exit_quantities[2] or 0)
                )
                if two_lot_trailing_only_runner:
                    log_event(
                        f"[RUNNER] {symbol} {action['reason']} reached at {exit_price:.2f} | "
                        "promoting protection without scaling out the final two-lot runner"
                    )
                    if hasattr(engine, "apply_runner_partial_exit"):
                        engine.apply_runner_partial_exit(position, action, exit_price, snapshot)
                    state_changed = True
                    runner_adjusted_this_cycle = True
                    break
                log_event(
                    f"[RUNNER] {symbol} {action['reason']} triggered at {exit_price:.2f} | "
                    f"Qty={action['quantity']} | Entry={position_entry_price(position):.2f}"
                )
                partial_result = execute_partial_position_exit(
                    engine,
                    position,
                    int(action["quantity"]),
                    str(action["reason"]),
                    exit_price,
                    now,
                    trade_book,
                    trade_store,
                    place_order,
                    transaction_cost_model_enabled,
                    slippage_pct_per_side,
                    exit_limit_price_buffer_pct=exit_limit_price_buffer_pct,
                )
                if not partial_result["filled"]:
                    log_event(
                        f"[RUNNER] {symbol} {action['reason']} order not filled; "
                        "position quantity and protection unchanged",
                        "warning",
                    )
                    break
                if hasattr(engine, "apply_runner_partial_exit"):
                    engine.apply_runner_partial_exit(
                        position,
                        action,
                        float(partial_result["exit_price"]),
                        snapshot,
                    )
                state_changed = True
                runner_adjusted_this_cycle = True
                if int(position.get("quantity") or 0) <= 0:
                    del positions[symbol]
                break
            if symbol not in positions:
                continue
            if runner_adjusted_this_cycle:
                continue

        exit_reason = engine.evaluate_position_exit(position, snapshot["latest_candle"])
        if not exit_reason and hasattr(engine, "get_time_exit_reason"):
            exit_reason = engine.get_time_exit_reason(position, now)
        if not exit_reason:
            exit_reason = get_theta_exit_reason(position, snapshot, now)
        if exit_reason:
            if pair_id:
                pair_symbols = get_pair_symbols(positions, pair_id)
                log_event(f"[PAIR EXIT] {pair_id} {exit_reason} triggered by {symbol} at {snapshot['latest_close']:.2f}")
                if close_position_symbols(
                    engine,
                    positions,
                    pair_symbols,
                    reason=f"Pair exit via {symbol} {exit_reason}",
                    trade_book=trade_book,
                    trade_store=trade_store,
                    place_order=place_order,
                    log_order_signal_banner=log_order_signal_banner,
                    fetch_data=fetch_data,
                    log_event=log_event,
                    transaction_cost_model_enabled=transaction_cost_model_enabled,
                    slippage_pct_per_side=slippage_pct_per_side,
                    symbol_snapshots=symbol_snapshots,
                    exit_time=now,
                ):
                    state_changed = True
                processed_pair_ids.add(pair_id)
                continue

            exit_price = float(snapshot["latest_close"])
            log_event(
                f"[EXIT] {symbol} {exit_reason} triggered at {exit_price:.2f} | "
                f"Entry={position_entry_price(position):.2f} | Qty={position_quantity(position)}"
            )
            log_order_signal_banner("EXIT", build_exit_position_lines(position, exit_price, exit_reason))
            _exit_side = opposite_side(position)
            _order_type = "MARKET"
            _limit_price = None
            if exit_limit_price_buffer_pct > 0:
                _order_type = "LIMIT"
                _buf = float(exit_price) * exit_limit_price_buffer_pct
                _limit_price = max(0.01, float(exit_price) - _buf) if _exit_side == "SELL" else float(exit_price) + _buf
            order_result = place_order(
                _exit_side,
                position_quantity(position),
                symbol,
                note=f"Exit {position_side(position)} via {exit_reason}",
                product=(position.get("order_product") or engine.order_product),
                enforce_spread_check=False,
                enforce_margin_check=False,
                entry_price=exit_price,
                order_type=_order_type,
                price=_limit_price,
            )
            fill = _resolve_order_fill(order_result, position_quantity(position), float(exit_price))
            if not fill["filled"]:
                log_event(f"[EXIT] {symbol} {exit_reason} order not filled; position remains open", "warning")
                continue
            _record_confirmed_exit_fill(
                position=position,
                filled_quantity=int(fill["filled_quantity"]),
                symbol=symbol,
                exit_price=float(fill["exit_price"]),
                reason=exit_reason,
                exit_time=now,
                trade_book=trade_book,
                trade_store=trade_store,
                transaction_cost_model_enabled=transaction_cost_model_enabled,
                slippage_pct_per_side=slippage_pct_per_side,
            )
            remaining_qty = max(0, position_quantity(position) - int(fill["filled_quantity"]))
            if remaining_qty <= 0:
                del positions[symbol]
            else:
                position["quantity"] = remaining_qty
            state_changed = True
            continue

        signal_exit_reason = engine.get_signal_exit_reason(position, snapshot["signal"])
        if signal_exit_reason:
            exit_price = float(snapshot["latest_close"])
            log_event(
                f"[EXIT] Signal-based exit for {symbol}: {snapshot['signal']} ({signal_exit_reason}) at {exit_price:.2f} | "
                f"Entry={position_entry_price(position):.2f} | Qty={position_quantity(position)}"
            )
            log_order_signal_banner("EXIT", build_exit_position_lines(position, exit_price, signal_exit_reason))
            order_result = place_order(
                opposite_side(position),
                position_quantity(position),
                symbol,
                note=f"Close {position_side(position)} via {signal_exit_reason}",
                product=(position.get("order_product") or engine.order_product),
                enforce_spread_check=False,
                enforce_margin_check=False,
                entry_price=exit_price,
            )
            fill = _resolve_order_fill(order_result, position_quantity(position), float(exit_price))
            if not fill["filled"]:
                log_event(f"[EXIT] {symbol} {signal_exit_reason} order not filled; position remains open", "warning")
                continue
            _record_confirmed_exit_fill(
                position=position,
                filled_quantity=int(fill["filled_quantity"]),
                symbol=symbol,
                exit_price=float(fill["exit_price"]),
                reason=signal_exit_reason,
                exit_time=now,
                trade_book=trade_book,
                trade_store=trade_store,
                transaction_cost_model_enabled=transaction_cost_model_enabled,
                slippage_pct_per_side=slippage_pct_per_side,
            )
            remaining_qty = max(0, position_quantity(position) - int(fill["filled_quantity"]))
            if remaining_qty <= 0:
                del positions[symbol]
            else:
                position["quantity"] = remaining_qty
            state_changed = True

    return state_changed
