from __future__ import annotations

from typing import Any, Callable

from models import Position


def build_position(
    symbol: str,
    side: str,
    quantity: int,
    entry_price: float,
    sl_pct: float | None = None,
    target_pct: float | None = None,
    trailing_pct: float | None = None,
    stop_loss: float | None = None,
    target: float | None = None,
    trailing_stop: float | None = None,
    trailing_distance: float | None = None,
    atr: float | None = None,
    stop_distance: float | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    if stop_loss is None or target is None or trailing_stop is None:
        if side == "BUY":
            stop_loss = entry_price * (1 - sl_pct / 100)
            target = entry_price * (1 + target_pct / 100)
            trailing_stop = entry_price * (1 - trailing_pct / 100)
        else:
            stop_loss = entry_price * (1 + sl_pct / 100)
            target = entry_price * (1 - target_pct / 100)
            trailing_stop = entry_price * (1 + trailing_pct / 100)

    position = Position(
        symbol=symbol,
        side=str(side).upper(),
        quantity=quantity,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target=target,
        trailing_stop=trailing_stop,
        best_price=entry_price,
        atr=atr,
        stop_distance=stop_distance,
        trailing_distance=trailing_distance,
        trailing_activation_distance=extra_fields.get("trailing_activation_distance"),
        trailing_active=bool(extra_fields.get("trailing_active", False)),
        extra_fields=extra_fields,
    )
    return position.to_dict()


def merge_persisted_position_state(
    position: dict[str, Any],
    persisted_position: dict[str, Any] | None,
) -> dict[str, Any]:
    if not persisted_position:
        return position

    merged = dict(position)
    for key, value in persisted_position.items():
        if key in {"symbol", "side", "quantity", "entry_price"}:
            continue
        merged[key] = value
    return merged


def update_trailing_stop(position: dict[str, Any], latest_close: float, trailing_pct: float) -> bool:
    typed_position = Position.from_mapping(position)
    changed = typed_position.update_trailing_stop(latest_close, trailing_pct)
    position.update(typed_position.to_dict())
    return changed


def evaluate_exit(
    position: dict[str, Any],
    latest_candle: dict[str, Any],
    include_target: bool = True,
) -> str | None:
    typed_position = Position.from_mapping(position)
    exit_reason = typed_position.evaluate_exit(
        latest_high=latest_candle["High"],
        latest_low=latest_candle["Low"],
        include_target=include_target,
    )
    return exit_reason.value if exit_reason else None


def log_positions(
    positions: dict[str, dict[str, Any]],
    log_event: Callable[..., Any],
    current_prices: dict[str, float] | None = None,
) -> None:
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


def get_deployed_capital(positions: dict[str, dict[str, Any]]) -> float:
    return sum(
        position["entry_price"] * position["quantity"]
        for position in positions.values()
    )


def get_symbol_deployed_capital(positions: dict[str, dict[str, Any]], symbol: str) -> float:
    position = positions.get(symbol)
    if not position:
        return 0.0

    return position["entry_price"] * position["quantity"]


def count_open_structures(positions: dict[str, dict[str, Any]]) -> int:
    structure_keys = set()
    for symbol, position in positions.items():
        pair_id = position.get("pair_id")
        structure_keys.add(pair_id or symbol)
    return len(structure_keys)


def apply_capital_limits_to_quantity(
    quantity: int,
    entry_price: float,
    max_capital_per_trade: float,
    max_capital_deployed: float,
    deployed_capital: float,
    log_event: Callable[..., Any],
) -> int:
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
