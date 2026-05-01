from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class Quote:
    symbol: str
    last_price: float
    bid_price: float | None = None
    ask_price: float | None = None

    @property
    def spread(self) -> float | None:
        if self.bid_price is None or self.ask_price is None:
            return None
        return float(self.ask_price) - float(self.bid_price)

    @property
    def spread_pct(self) -> float | None:
        if self.last_price <= 0:
            return None
        spread = self.spread
        if spread is None:
            return None
        return spread / float(self.last_price)


@dataclass(slots=True)
class PositionSnapshot:
    symbol: str
    quantity: int
    average_price: float
    side: str


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    product: str = "MIS"
    note: str | None = None
    order_type: str = "MARKET"
    price: float | None = None
    trigger_price: float | None = None
    validity: str = "DAY"


@dataclass(slots=True)
class OrderResult:
    order_id: str | None
    status: OrderStatus
    message: str | None = None
    requested_quantity: int = 0
    filled_quantity: int = 0
    pending_quantity: int = 0
    average_price: float | None = None
    parent_order_id: str | None = None
    child_order_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class BrokerClient(ABC):
    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult:
        """Place an order through the underlying broker."""

    @abstractmethod
    def get_positions(self) -> list[PositionSnapshot]:
        """Return open positions from the broker."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for a symbol."""

    @abstractmethod
    def get_intraday_positions(self) -> list[dict]:
        """Return current intraday positions in the legacy executor shape."""

    @abstractmethod
    def get_delivery_holdings(self) -> list[dict]:
        """Return current delivery holdings in the legacy executor shape."""

    @abstractmethod
    def get_nfo_positions(self) -> list[dict]:
        """Return current F&O positions in the legacy executor shape."""

    def get_order_status(self, order_id: str) -> OrderResult | None:
        """Return the broker order status when supported."""
        raise NotImplementedError("Order-status lookup is not implemented for this broker.")

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open broker order when supported."""
        raise NotImplementedError("Order cancellation is not implemented for this broker.")

    def get_available_margin(self, product: str | None = None) -> float | None:
        """Return currently available trading margin when supported."""
        raise NotImplementedError("Margin lookup is not implemented for this broker.")
