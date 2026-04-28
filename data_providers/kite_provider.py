from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from kiteconnect import KiteConnect

from config import get_access_token, get_api_key, get_broker_ip_mode
from network_utils import configure_kite_client_network

from .base import DataProvider


class KiteDataProvider(DataProvider):
    name = "KITE"

    def __init__(self):
        self._kite_client = None
        self._instrument_cache = {}

    def fetch(self, symbol: str, period: str = "1d", interval: str = "1m"):
        instrument_token = self._get_instrument_token(symbol)
        from_date, to_date = self._resolve_date_window(period)
        candles = self._get_client().historical_data(
            instrument_token,
            from_date,
            to_date,
            self._map_interval(interval),
        )
        if not candles:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        data = pd.DataFrame(candles)
        data["date"] = pd.to_datetime(data["date"])
        data = data.rename(
            columns={
                "date": "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )
        data = data.set_index("Date")
        return data[["Open", "High", "Low", "Close", "Volume"]]

    def _get_client(self):
        if self._kite_client is None:
            self._kite_client = configure_kite_client_network(
                KiteConnect(api_key=get_api_key()),
                ip_mode=get_broker_ip_mode(),
            )
            self._kite_client.set_access_token(get_access_token())
        return self._kite_client

    def _get_instrument_token(self, symbol: str):
        exchange, tradingsymbol = self._parse_symbol_exchange(symbol)
        cache_key = f"{exchange}:{tradingsymbol}"
        if cache_key not in self._instrument_cache:
            instruments = self._get_client().instruments(exchange)
            for item in instruments:
                key = f"{item['exchange']}:{item['tradingsymbol']}"
                self._instrument_cache[key] = item["instrument_token"]

        token = self._instrument_cache.get(cache_key)
        if token is None:
            raise RuntimeError(f"Kite instrument token not found for {symbol}")
        return token

    @staticmethod
    def _parse_symbol_exchange(symbol: str):
        if not symbol:
            raise ValueError("Symbol is required")

        if ":" in symbol:
            exchange, tradingsymbol = symbol.split(":", 1)
            return exchange.upper(), tradingsymbol.replace(".NS", "")

        return "NSE", symbol.replace(".NS", "")

    @staticmethod
    def _resolve_date_window(period: str):
        now = datetime.now()
        period_key = (period or "1d").lower()
        if period_key.endswith("d"):
            delta = timedelta(days=int(period_key[:-1]))
        elif period_key.endswith("mo"):
            delta = timedelta(days=int(period_key[:-2]) * 30)
        elif period_key.endswith("y"):
            delta = timedelta(days=int(period_key[:-1]) * 365)
        else:
            delta = timedelta(days=5)
        return now - delta, now

    @staticmethod
    def _map_interval(interval: str):
        mapping = {
            "1m": "minute",
            "3m": "3minute",
            "5m": "5minute",
            "10m": "10minute",
            "15m": "15minute",
            "30m": "30minute",
            "60m": "60minute",
            "1d": "day",
        }
        return mapping.get(interval, interval)
