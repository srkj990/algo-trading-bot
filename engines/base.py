from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class TradingEngine(ABC):
    name: str
    data_period: str
    data_interval: str
    order_product: str
    supported_strategies: dict[str, str]

    @abstractmethod
    def get_cycle_state(self, now: datetime) -> dict[str, Any]:
        """Describe whether this engine can scan, enter, and manage positions."""

    @abstractmethod
    def normalize_entry_signal(self, signal: str | None) -> str | None:
        """Map a raw strategy signal to a tradable entry signal."""

    @abstractmethod
    def evaluate_position_exit(
        self,
        position: dict[str, Any],
        latest_candle: dict[str, Any],
    ) -> str | None:
        """Return an exit reason when the current candle triggers one."""

    @abstractmethod
    def get_signal_exit_reason(
        self,
        position: dict[str, Any],
        signal: str | None,
    ) -> str | None:
        """Return an exit reason caused by a fresh strategy signal."""

    @abstractmethod
    def apply_entry_allocation_limit(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        positions: dict[str, dict[str, Any]],
        capital: float,
    ) -> int:
        """Return a quantity after engine-specific allocation caps are applied."""

    @abstractmethod
    def reconcile_startup(
        self,
        execution_mode: str,
        persisted_positions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Merge broker state with persisted state at process startup."""
