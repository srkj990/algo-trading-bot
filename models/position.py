from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PositionSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_BASED = "TIME_BASED"
    REVERSAL = "REVERSAL"
    SELL_SIGNAL = "SELL_SIGNAL"


@dataclass(slots=True)
class Position:
    symbol: str
    side: PositionSide
    quantity: int
    entry_price: float
    stop_loss: float
    target: float
    trailing_stop: float
    best_price: float | None = None
    atr: float | None = None
    stop_distance: float | None = None
    trailing_distance: float | None = None
    trailing_activation_distance: float | None = None
    trailing_active: bool = False
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.side, PositionSide):
            self.side = PositionSide(str(self.side).upper())
        self.entry_price = float(self.entry_price)
        self.stop_loss = float(self.stop_loss)
        self.target = float(self.target)
        self.trailing_stop = float(self.trailing_stop)
        self.best_price = self.entry_price if self.best_price is None else float(self.best_price)
        self.atr = None if self.atr is None else float(self.atr)
        self.stop_distance = None if self.stop_distance is None else float(self.stop_distance)
        self.trailing_distance = (
            None if self.trailing_distance is None else float(self.trailing_distance)
        )
        self.trailing_activation_distance = (
            None
            if self.trailing_activation_distance is None
            else float(self.trailing_activation_distance)
        )

        if not self.symbol:
            raise ValueError("symbol is required")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.stop_loss <= 0 or self.target <= 0 or self.trailing_stop <= 0:
            raise ValueError("price levels must be positive")

    @property
    def unrealized_pnl(self) -> float:
        if self.side == PositionSide.BUY:
            return (self.best_price - self.entry_price) * self.quantity
        return (self.entry_price - self.best_price) * self.quantity

    def update_trailing_stop(self, latest_close: float, trailing_pct: float) -> bool:
        latest_close = float(latest_close)
        trailing_pct = float(trailing_pct)
        trailing_distance = self.trailing_distance
        if trailing_pct <= 0 and trailing_distance is None:
            return False

        old_trailing = self.trailing_stop
        if self.side == PositionSide.BUY:
            self.best_price = max(self.best_price, latest_close)
            if self.trailing_activation_distance and (
                self.best_price - self.entry_price
            ) < self.trailing_activation_distance:
                return False
            if self.trailing_activation_distance:
                self.trailing_active = True
            candidate = (
                self.best_price - trailing_distance
                if trailing_distance is not None
                else self.best_price * (1 - trailing_pct / 100)
            )
            self.trailing_stop = max(self.trailing_stop, candidate)
        else:
            self.best_price = min(self.best_price, latest_close)
            if self.trailing_activation_distance and (
                self.entry_price - self.best_price
            ) < self.trailing_activation_distance:
                return False
            if self.trailing_activation_distance:
                self.trailing_active = True
            candidate = (
                self.best_price + trailing_distance
                if trailing_distance is not None
                else self.best_price * (1 + trailing_pct / 100)
            )
            self.trailing_stop = min(self.trailing_stop, candidate)

        return self.trailing_stop != old_trailing

    def evaluate_exit(
        self,
        latest_high: float,
        latest_low: float,
        include_target: bool = True,
    ) -> ExitReason | None:
        latest_high = float(latest_high)
        latest_low = float(latest_low)

        if self.side == PositionSide.BUY:
            if latest_low <= self.stop_loss:
                return ExitReason.STOP_LOSS
            if latest_low <= self.trailing_stop:
                return ExitReason.TRAILING_STOP
            if include_target and latest_high >= self.target:
                return ExitReason.TARGET
        else:
            if latest_high >= self.stop_loss:
                return ExitReason.STOP_LOSS
            if latest_high >= self.trailing_stop:
                return ExitReason.TRAILING_STOP
            if include_target and latest_low <= self.target:
                return ExitReason.TARGET

        return None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "trailing_stop": self.trailing_stop,
            "best_price": self.best_price,
            "atr": self.atr,
            "stop_distance": self.stop_distance,
            "trailing_distance": self.trailing_distance,
            "trailing_activation_distance": self.trailing_activation_distance,
            "trailing_active": self.trailing_active,
        }
        data.update(self.extra_fields)
        return data

    @classmethod
    def from_mapping(cls, raw_position: dict[str, Any]) -> Position:
        core_fields = {
            "symbol",
            "side",
            "quantity",
            "entry_price",
            "stop_loss",
            "target",
            "trailing_stop",
            "best_price",
            "atr",
            "stop_distance",
            "trailing_distance",
            "trailing_activation_distance",
            "trailing_active",
        }
        extra_fields = {
            key: value for key, value in raw_position.items() if key not in core_fields
        }
        return cls(
            symbol=str(raw_position["symbol"]),
            side=PositionSide(str(raw_position["side"]).upper()),
            quantity=int(raw_position["quantity"]),
            entry_price=float(raw_position["entry_price"]),
            stop_loss=float(raw_position["stop_loss"]),
            target=float(raw_position["target"]),
            trailing_stop=float(raw_position["trailing_stop"]),
            best_price=raw_position.get("best_price"),
            atr=raw_position.get("atr"),
            stop_distance=raw_position.get("stop_distance"),
            trailing_distance=raw_position.get("trailing_distance"),
            trailing_activation_distance=raw_position.get(
                "trailing_activation_distance"
            ),
            trailing_active=bool(raw_position.get("trailing_active", False)),
            extra_fields=extra_fields,
        )
