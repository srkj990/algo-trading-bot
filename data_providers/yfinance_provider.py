from __future__ import annotations

import yfinance as yf

from .base import DataProvider


class YFinanceDataProvider(DataProvider):
    name = "YFINANCE"

    def fetch(self, symbol: str, period: str = "1d", interval: str = "1m"):
        if symbol.upper().startswith("NFO:"):
            raise ValueError(
                "YFINANCE does not support F&O symbols. Use KITE data provider for F&O."
            )

        data = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            timeout=20,
        )

        if hasattr(data.columns, "levels"):
            data.columns = [col[0] for col in data.columns]

        return data
