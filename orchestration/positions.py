from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from engines.common import count_open_structures, get_deployed_capital, update_trailing_stop
from models.position_adapter import (
    calculate_position_pnl,
    opposite_side,
    position_entry_price,
    position_quantity,
    position_side,
)
from reporting import summarize_by_exit_reason
from transaction_costs import estimate_intraday_equity_round_trip_cost


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
    symbol: str,
    position: dict[str, Any],
    exit_price: float,
    exit_reason: str,
    exit_time: Any,
    transaction_cost_model_enabled: bool,
    slippage_pct_per_side: float,
) -> None:
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

    trade_book.append(
        {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_time": position.get("entry_time"),
            "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
            "entry_price": entry_price,
            "exit_price": float(exit_price),
            "pnl": pnl,
            "estimated_charges": estimated_charges,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "pair_id": position.get("pair_id"),
        }
    )


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


def log_trade_book_summary(
    capital: float,
    trade_book: list[dict[str, Any]],
    log_event: Callable[..., Any],
    transaction_cost_model_enabled: bool,
) -> None:
    log_event("[REPORT] CLOSED TRADES")
    if not trade_book:
        log_event("[REPORT]   No closed trades recorded in this session")
        return

    sorted_trades = sorted(trade_book, key=lambda item: item.get("exit_time") or "")
    total_realized = sum(trade["pnl"] for trade in sorted_trades)
    total_estimated_charges = sum(float(trade.get("estimated_charges") or 0.0) for trade in sorted_trades)
    total_net = sum(float(trade.get("net_pnl") or trade["pnl"]) for trade in sorted_trades)
    wins = sum(1 for trade in sorted_trades if trade["pnl"] > 0)
    losses = sum(1 for trade in sorted_trades if trade["pnl"] < 0)
    flats = len(sorted_trades) - wins - losses
    traded_value = sum(trade["entry_price"] * trade["quantity"] for trade in sorted_trades)
    best_trade = max(sorted_trades, key=lambda item: item["pnl"])
    worst_trade = min(sorted_trades, key=lambda item: item["pnl"])

    log_event(f"[REPORT]   Closed={len(sorted_trades)} | Wins={wins} | Losses={losses} | Flat={flats}")
    log_event(f"[REPORT]   Traded value: {traded_value:.2f}")
    log_event(f"[REPORT]   Realized P&L: {total_realized:+.2f}")
    if transaction_cost_model_enabled:
        log_event(f"[REPORT]   Est. charges: {total_estimated_charges:.2f}")
        log_event(f"[REPORT]   Est. net P&L: {total_net:+.2f}")
    log_event(f"[REPORT]   Return on starting capital: {(total_realized / capital) * 100:+.2f}%")
    log_event(f"[REPORT]   Best trade: {best_trade['symbol']} {best_trade['pnl']:+.2f}")
    log_event(f"[REPORT]   Worst trade: {worst_trade['symbol']} {worst_trade['pnl']:+.2f}")
    log_event("[REPORT]   # | Symbol | Side | Qty | EntryTime | ExitTime | Entry | Exit | P&L | ExitReason")
    for index, trade in enumerate(sorted_trades, start=1):
        pair_suffix = f" [{trade['pair_id']}]" if trade.get("pair_id") else ""
        log_event(
            f"[REPORT]   {index:>2} | {trade['symbol']}{pair_suffix} | {trade['side']:<4} | "
            f"{trade['quantity']:>3} | {format_trade_time(trade['entry_time'])} | "
            f"{format_trade_time(trade['exit_time'])} | {trade['entry_price']:.2f} | "
            f"{trade['exit_price']:.2f} | {trade['pnl']:+.2f} ({trade['pnl_pct']:+.2f}%) | {trade['exit_reason']}"
        )

    summary_rows = summarize_by_exit_reason(sorted_trades)
    log_event("[REPORT] Exit reason summary (gross/net):")
    for row in summary_rows:
        log_event(
            f"[REPORT]   {row['exit_reason']}: Trades={row['trades']} | Gross={row['gross_pnl']:+.2f} | Net={row['net_pnl']:+.2f}"
        )


def close_position_symbols(
    engine: Any,
    positions: dict[str, dict[str, Any]],
    symbols: list[str],
    reason: str,
    trade_book: list[dict[str, Any]],
    place_order: Callable[..., Any],
    log_order_signal_banner: Callable[..., Any],
    fetch_data: Callable[..., Any],
    log_event: Callable[..., Any],
    transaction_cost_model_enabled: bool,
    slippage_pct_per_side: float,
    symbol_snapshots: dict[str, dict[str, Any]] | None = None,
    exit_time: datetime | None = None,
) -> bool:
    changed = False
    exit_time = exit_time or datetime.now()
    for symbol in list(symbols):
        position = positions.get(symbol)
        if not position:
            continue
        exit_price = get_latest_exit_price(engine, symbol, position, fetch_data, log_event, symbol_snapshots=symbol_snapshots)
        log_order_signal_banner("EXIT", build_exit_position_lines(position, exit_price, reason))
        place_order(opposite_side(position), position_quantity(position), symbol, note=reason, product=engine.order_product)
        record_closed_trade(
            trade_book,
            symbol,
            position,
            exit_price,
            reason,
            exit_time,
            transaction_cost_model_enabled,
            slippage_pct_per_side,
        )
        del positions[symbol]
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
    engine_runtime_state: dict[str, Any],
    save_engine_state: Callable[..., Any],
) -> None:
    save_engine_state(
        engine_name=engine_name,
        positions=positions,
        traded_symbols_today=traded_symbols_today,
        trade_counts_today=trade_counts_today,
        active_trade_day=active_trade_day,
        last_entry_time=last_entry_time,
        regime_cache=regime_cache,
        engine_runtime_state=engine_runtime_state,
    )


def log_ranked_candidates(candidates: list[dict[str, Any]], log_event: Callable[..., Any]) -> None:
    if not candidates:
        log_event("[SCAN] No actionable ranked candidates")
        return

    log_event("[SCAN] Ranked candidates:")
    for index, candidate in enumerate(candidates, start=1):
        analytics = candidate.get("analytics") or {}
        greeks_text = ""
        if candidate.get("is_pair"):
            pair_data = candidate.get("pair_config") or {}
            greeks_text = (
                f" | Pair={pair_data.get('lower_strike')}-{pair_data.get('upper_strike')}"
                f" | Underlying={analytics.get('underlying_price', 0.0):.2f}"
            )
        elif analytics:
            greeks_text = (
                f" | Delta={analytics.get('delta', 0.0):.3f}"
                f" | IV={analytics.get('iv', 0.0):.3f}"
            )
        log_event(
            f"[SCAN] Rank {index} | {candidate['symbol']} | Signal={candidate['signal']} | "
            f"Agree={candidate['agreement_count']} | Score={candidate['score']:.4f} | "
            f"ATR={candidate['atr']:.2f} | Last close={candidate['latest_close']:.2f}{greeks_text}"
        )


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

    log_event("\n" + "=" * 50)
    log_event("[STATS] Execution summary:")
    log_event("=" * 50)
    log_event(f"[STATS] Starting capital: {capital:.2f}")
    log_event(f"[STATS] Open positions: {open_count}")
    log_event(f"[STATS] Open structures: {open_structure_count}")
    log_event(f"[STATS] Deployed capital (entry exposure): {deployed_capital:.2f}")
    log_event(f"[STATS] Capital reserve estimate: {max(0.0, capital - deployed_capital):.2f}")
    log_trade_book_summary(capital, trade_book, log_event, transaction_cost_model_enabled)

    try:
        report_path = export_trade_book_report(trade_book, engine_name=engine.name)
        if report_path:
            log_event(f"[REPORT] Trade report exported: {report_path}")
    except Exception as exc:
        log_event(f"[REPORT] Failed to export trade report: {exc}", "warning")

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
        bucket.append(
            {
                "symbol": symbol,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
        if position_side(position) == "BUY":
            long_pnl += pnl
            long_count += 1
        else:
            short_pnl += pnl
            short_count += 1
        total_unrealized += pnl
        log_event(
            f"[STATS] {symbol} {position_side(position)} qty={position_quantity(position)} "
            f"entry={position_entry_price(position):.2f} current={latest_close:.2f} "
            f"market_value={market_value:.2f} pnl={pnl:+.2f} ({pnl_pct:+.2f}%)"
        )

    log_event("\n" + "-" * 50)
    log_event("[STATS] LONG POSITIONS SUMMARY:")
    log_event(f"[STATS]   Count: {long_count}")
    log_event("[STATS]   Stocks:")
    if long_positions:
        for pos in sorted(long_positions, key=lambda x: x["pnl"], reverse=True):
            log_event(f"[STATS]     {pos['symbol']}: {pos['pnl']:+.2f} ({pos['pnl_pct']:+.2f}%)")
    else:
        log_event("[STATS]     None")
    log_event(f"[STATS]   Total P&L: {long_pnl:+.2f}")

    log_event("\n[STATS] SHORT POSITIONS SUMMARY:")
    log_event(f"[STATS]   Count: {short_count}")
    log_event("[STATS]   Stocks:")
    if short_positions:
        for pos in sorted(short_positions, key=lambda x: x["pnl"], reverse=True):
            log_event(f"[STATS]     {pos['symbol']}: {pos['pnl']:+.2f} ({pos['pnl_pct']:+.2f}%)")
    else:
        log_event("[STATS]     None")
    log_event(f"[STATS]   Total P&L: {short_pnl:+.2f}")

    log_event("\n[STATS] OVERALL RESULTS:")
    log_event(f"[STATS]   Total market value of open positions: {total_market_value:.2f}")
    log_event(f"[STATS]   Total unrealized P&L: {total_unrealized:+.2f}")
    if deployed_capital > 0:
        log_event(f"[STATS]   Return %: {(total_unrealized / deployed_capital) * 100:+.2f}%")
    log_event("=" * 50)


def force_square_off_positions(
    engine,
    positions,
    trade_book,
    place_order,
    log_order_signal_banner,
    fetch_data,
    log_event,
    transaction_cost_model_enabled,
    slippage_pct_per_side,
):
    if not positions:
        return False

    log_event("[SQUAREOFF] Closing all open positions")
    changed = False
    exit_time = datetime.now()
    for symbol, position in list(positions.items()):
        exit_price = get_latest_exit_price(engine, symbol, position, fetch_data, log_event)
        log_order_signal_banner("FORCE SQUARE OFF", build_exit_position_lines(position, exit_price, "Intraday square-off"))
        place_order(
            opposite_side(position),
            position_quantity(position),
            symbol,
            note="Intraday square-off",
            product=engine.order_product,
        )
        record_closed_trade(
            trade_book,
            symbol,
            position,
            exit_price,
            "Intraday square-off",
            exit_time,
            transaction_cost_model_enabled,
            slippage_pct_per_side,
        )
        del positions[symbol]
        changed = True
    return changed


def manage_open_positions(
    engine,
    positions,
    symbol_snapshots,
    now,
    trade_book,
    place_order,
    log_order_signal_banner,
    fetch_data,
    log_event,
    transaction_cost_model_enabled,
    slippage_pct_per_side,
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

        exit_reason = engine.evaluate_position_exit(position, snapshot["latest_candle"])
        if not exit_reason and hasattr(engine, "get_time_exit_reason"):
            exit_reason = engine.get_time_exit_reason(position, now)
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
            place_order(
                opposite_side(position),
                position_quantity(position),
                symbol,
                note=f"Exit {position_side(position)} via {exit_reason}",
                product=engine.order_product,
            )
            record_closed_trade(
                trade_book,
                symbol,
                position,
                exit_price,
                exit_reason,
                now,
                transaction_cost_model_enabled,
                slippage_pct_per_side,
            )
            del positions[symbol]
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
            place_order(
                opposite_side(position),
                position_quantity(position),
                symbol,
                note=f"Close {position_side(position)} via {signal_exit_reason}",
                product=engine.order_product,
            )
            record_closed_trade(
                trade_book,
                symbol,
                position,
                exit_price,
                signal_exit_reason,
                now,
                transaction_cost_model_enabled,
                slippage_pct_per_side,
            )
            del positions[symbol]
            state_changed = True

    return state_changed
