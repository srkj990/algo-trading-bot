def build_position(
    symbol,
    side,
    quantity,
    entry_price,
    sl_pct=None,
    target_pct=None,
    trailing_pct=None,
    stop_loss=None,
    target=None,
    trailing_stop=None,
    trailing_distance=None,
    atr=None,
    stop_distance=None,
    **extra_fields,
):
    if stop_loss is None or target is None or trailing_stop is None:
        if side == "BUY":
            stop_loss = entry_price * (1 - sl_pct / 100)
            target = entry_price * (1 + target_pct / 100)
            trailing_stop = entry_price * (1 - trailing_pct / 100)
        else:
            stop_loss = entry_price * (1 + sl_pct / 100)
            target = entry_price * (1 - target_pct / 100)
            trailing_stop = entry_price * (1 + trailing_pct / 100)

    position = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target": target,
        "trailing_stop": trailing_stop,
        "best_price": entry_price,
        "atr": atr,
        "stop_distance": stop_distance,
        "trailing_distance": trailing_distance,
    }
    position.update(extra_fields)
    return position


def merge_persisted_position_state(position, persisted_position):
    if not persisted_position:
        return position

    merged = dict(position)
    for key, value in persisted_position.items():
        if key in {"symbol", "side", "quantity", "entry_price"}:
            continue
        merged[key] = value
    return merged


def update_trailing_stop(position, latest_close, trailing_pct):
    trailing_distance = position.get("trailing_distance")
    if trailing_pct <= 0 and trailing_distance is None:
        return False

    old_trailing = position["trailing_stop"]
    if position["side"] == "BUY":
        position["best_price"] = max(position["best_price"], latest_close)
        activation_distance = position.get("trailing_activation_distance")
        if activation_distance is not None:
            try:
                activation_distance = float(activation_distance)
            except (TypeError, ValueError):
                activation_distance = None
        if activation_distance and activation_distance > 0:
            favorable_move = position["best_price"] - float(position["entry_price"])
            if favorable_move < activation_distance:
                return False
            position["trailing_active"] = True
        if trailing_distance is not None:
            candidate = position["best_price"] - trailing_distance
        else:
            candidate = position["best_price"] * (1 - trailing_pct / 100)
        position["trailing_stop"] = max(position["trailing_stop"], candidate)
    else:
        position["best_price"] = min(position["best_price"], latest_close)
        activation_distance = position.get("trailing_activation_distance")
        if activation_distance is not None:
            try:
                activation_distance = float(activation_distance)
            except (TypeError, ValueError):
                activation_distance = None
        if activation_distance and activation_distance > 0:
            favorable_move = float(position["entry_price"]) - position["best_price"]
            if favorable_move < activation_distance:
                return False
            position["trailing_active"] = True
        if trailing_distance is not None:
            candidate = position["best_price"] + trailing_distance
        else:
            candidate = position["best_price"] * (1 + trailing_pct / 100)
        position["trailing_stop"] = min(position["trailing_stop"], candidate)

    return position["trailing_stop"] != old_trailing


def evaluate_exit(position, latest_candle, include_target=True):
    high = float(latest_candle["High"])
    low = float(latest_candle["Low"])

    if position["side"] == "BUY":
        if low <= position["stop_loss"]:
            return "STOP_LOSS"
        if low <= position["trailing_stop"]:
            return "TRAILING_STOP"
        if include_target and high >= position["target"]:
            return "TARGET"
    else:
        if high >= position["stop_loss"]:
            return "STOP_LOSS"
        if high >= position["trailing_stop"]:
            return "TRAILING_STOP"
        if include_target and low <= position["target"]:
            return "TARGET"

    return None


def log_positions(positions, log_event, current_prices=None):
    if not positions:
        log_event("[POSITION] Flat")
        return

    for symbol, position in positions.items():
        current_price = current_prices.get(symbol) if current_prices else position['best_price']
        
        # Calculate P&L
        if position['side'] == 'BUY':
            pnl_abs = (current_price - position['entry_price']) * position['quantity']
        else:  # SELL
            pnl_abs = (position['entry_price'] - current_price) * position['quantity']
        
        pnl_pct = (pnl_abs / (position['entry_price'] * position['quantity'])) * 100 if position['entry_price'] > 0 else 0
        
        pnl_str = f"P&L={pnl_abs:+.2f} ({pnl_pct:+.2f}%)"
        
        log_event(
            (
                f"[POSITION] {position['symbol']} {position['side']} "
                f"Qty={position['quantity']} "
                f"Entry={position['entry_price']:.2f} "
                f"Current={current_price:.2f} "
                f"SL={position['stop_loss']:.2f} "
                f"Target={position['target']:.2f} "
                f"Trail={position['trailing_stop']:.2f} "
                f"Best={position['best_price']:.2f} "
                f"{pnl_str}"
            )
        )


def get_deployed_capital(positions):
    return sum(
        position["entry_price"] * position["quantity"]
        for position in positions.values()
    )


def get_symbol_deployed_capital(positions, symbol):
    position = positions.get(symbol)
    if not position:
        return 0.0

    return position["entry_price"] * position["quantity"]


def count_open_structures(positions):
    structure_keys = set()
    for symbol, position in positions.items():
        pair_id = position.get("pair_id")
        structure_keys.add(pair_id or symbol)
    return len(structure_keys)


def apply_capital_limits_to_quantity(
    quantity,
    entry_price,
    max_capital_per_trade,
    max_capital_deployed,
    deployed_capital,
    log_event,
):
    if quantity <= 0 or entry_price <= 0:
        return 0

    per_trade_cap_qty = int(max_capital_per_trade / entry_price)
    remaining_deployable = max(0.0, max_capital_deployed - deployed_capital)
    deployed_cap_qty = int(remaining_deployable / entry_price)
    limited_qty = min(quantity, per_trade_cap_qty, deployed_cap_qty)

    log_event(
        (
            f"[RISK] Quantity limits: Original={quantity}, "
            f"Per-trade cap={per_trade_cap_qty} "
            f"(max_capital_per_trade={max_capital_per_trade:.2f}), "
            f"Deployed cap={deployed_cap_qty} "
            f"(remaining_deployable={remaining_deployable:.2f}), "
            f"Final={limited_qty}"
        )
    )

    return limited_qty
