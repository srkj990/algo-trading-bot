from __future__ import annotations

from abc import ABC, abstractmethod


class DataProvider(ABC):
    name: str

    @abstractmethod
    def fetch(self, symbol: str, period: str = "1d", interval: str = "1m"):
        """Return OHLCV market data for a symbol."""
