from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class Quote:
    symbol: str
    last_price: float


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


@dataclass(slots=True)
class OrderResult:
    order_id: str | None
    status: OrderStatus
    message: str | None = None


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
