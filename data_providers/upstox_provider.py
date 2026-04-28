from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote

import pandas as pd

from config import get_broker_ip_mode, get_upstox_access_token
from network_utils import broker_request

from .base import DataProvider


class UpstoxDataProvider(DataProvider):
    name = "UPSTOX"

    def __init__(self):
        self._symbol_cache = {}

    def fetch(self, symbol: str, period: str = "1d", interval: str = "1m"):
        if symbol.upper().startswith("NFO:"):
            raise ValueError("Upstox F&O data is not supported yet. Use KITE for F&O.")

        instrument_key = self._resolve_instrument_key(symbol)
        from_date, to_date = self._resolve_date_window(period)
        interval_key = self._map_interval(interval)
        encoded_key = quote(instrument_key, safe="")
        url = (
            f"https://api.upstox.com/v2/historical-candle/"
            f"{encoded_key}/{interval_key}/{to_date.strftime('%Y-%m-%d')}/"
            f"{from_date.strftime('%Y-%m-%d')}"
        )
        response = broker_request(
            "GET",
            url,
            headers=self._headers(),
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        candles = response.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        data = pd.DataFrame(
            candles,
            columns=[
                "Date",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
                "OpenInterest",
            ],
        )
        data["Date"] = pd.to_datetime(data["Date"])
        data = data.set_index("Date").sort_index()
        return data[["Open", "High", "Low", "Close", "Volume"]]

    def _resolve_instrument_key(self, symbol: str):
        tradingsymbol = symbol.replace(".NS", "")
        if tradingsymbol in self._symbol_cache:
            return self._symbol_cache[tradingsymbol]

        response = broker_request(
            "GET",
            "https://api.upstox.com/v2/instruments/search",
            headers=self._headers(),
            params={
                "query": tradingsymbol,
                "exchanges": "NSE",
                "segments": "EQ",
                "page_number": 1,
                "records": 10,
            },
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("data", []):
            if item.get("exchange") == "NSE" and item.get("trading_symbol") == tradingsymbol:
                self._symbol_cache[tradingsymbol] = item["instrument_key"]
                return item["instrument_key"]

        raise RuntimeError(f"Upstox instrument key not found for {symbol}")

    @staticmethod
    def _headers():
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {get_upstox_access_token()}",
        }

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
            "1m": "1minute",
            "3m": "3minute",
            "5m": "5minute",
            "10m": "10minute",
            "15m": "15minute",
            "30m": "30minute",
            "1d": "day",
            "1w": "week",
            "1mo": "month",
        }
        mapped = mapping.get(interval)
        if mapped is None:
            raise ValueError(f"Unsupported Upstox interval: {interval}")
        return mapped
