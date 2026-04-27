from __future__ import annotations

from typing import Any

from .position import Position


def coerce_position(raw_position: Position | dict[str, Any]) -> Position:
    if isinstance(raw_position, Position):
        return raw_position
    return Position.from_mapping(raw_position)


def position_side(raw_position: Position | dict[str, Any]) -> str:
    return coerce_position(raw_position).side.value


def opposite_side(raw_position: Position | dict[str, Any]) -> str:
    return "SELL" if position_side(raw_position) == "BUY" else "BUY"


def position_quantity(raw_position: Position | dict[str, Any]) -> int:
    return int(coerce_position(raw_position).quantity)


def position_entry_price(raw_position: Position | dict[str, Any]) -> float:
    return float(coerce_position(raw_position).entry_price)


def calculate_position_pnl(
    raw_position: Position | dict[str, Any],
    exit_price: float,
) -> tuple[float, float]:
    position = coerce_position(raw_position)
    quantity = int(position.quantity)
    entry_price = float(position.entry_price)
    exit_price = float(exit_price)
    if position.side.value == "BUY":
        pnl = (exit_price - entry_price) * quantity
    else:
        pnl = (entry_price - exit_price) * quantity

    deployed = entry_price * quantity
    pnl_pct = (pnl / deployed) * 100 if deployed > 0 else 0.0
    return pnl, pnl_pct


def signed_position_value(
    raw_position: Position | dict[str, Any],
    market_price: float,
) -> float:
    position = coerce_position(raw_position)
    sign = 1 if position.side.value == "BUY" else -1
    return float(market_price) * int(position.quantity) * sign
