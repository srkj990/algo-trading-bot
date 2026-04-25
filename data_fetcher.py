from datetime import datetime, timedelta
import time
from urllib.parse import quote

import pandas as pd
import yfinance as yf
from kiteconnect import KiteConnect

from config import (
    get_access_token,
    get_api_key,
    get_broker_ip_mode,
    get_default_data_provider,
    get_upstox_access_token,
)
from logger import get_logger
from network_utils import broker_request, configure_kite_client_network


logger = get_logger()
DATA_PROVIDER = get_default_data_provider()
_kite_client = None
_kite_instruments_cache = {}
_upstox_symbol_cache = {}


def set_data_provider(provider):
    global DATA_PROVIDER
    DATA_PROVIDER = (provider or "YFINANCE").upper()


def get_data_provider():
    return DATA_PROVIDER


def get_data(symbol, period="1d", interval="1m", provider=None):
    active_provider = (provider or DATA_PROVIDER or "YFINANCE").upper()
    logger.info(
        "[DATA] Provider=%s | Symbol=%s | period=%s | interval=%s",
        active_provider,
        symbol,
        period,
        interval,
    )
    print(
        f"\n[DATA] Provider={active_provider} | Fetching {symbol} "
        f"(period={period}, interval={interval})..."
    )

    fetch_started_at = time.time()
    try:
        if active_provider == "YFINANCE":
            if symbol.upper().startswith("NFO:"):
                raise ValueError(
                    "YFINANCE does not support F&O symbols. Use KITE data provider for F&O."
                )
            data = _get_data_yfinance(symbol, period, interval)
        elif active_provider == "KITE":
            data = _get_data_kite(symbol, period, interval)
        elif active_provider == "UPSTOX":
            if symbol.upper().startswith("NFO:"):
                raise ValueError(
                    "Upstox F&O data is not supported yet. Use KITE for F&O."
                )
            data = _get_data_upstox(symbol, period, interval)
        else:
            raise ValueError(f"Unsupported data provider: {active_provider}")
    except Exception as exc:
        elapsed = time.time() - fetch_started_at
        message = (
            f"[DATA ERROR] Provider={active_provider} | Symbol={symbol} | "
            f"period={period} | interval={interval} | "
            f"elapsed={elapsed:.2f}s | {type(exc).__name__}: {exc}"
        )
        print(message)
        logger.exception(message)
        raise

    elapsed = time.time() - fetch_started_at
    print(f"[DATA] {symbol} fetch completed in {elapsed:.2f}s")
    logger.info(f"[DATA] {symbol} fetch completed in {elapsed:.2f}s")
    print(f"[DATA] {symbol} rows fetched: {len(data)}")
    logger.info(f"[DATA] {symbol} rows fetched: {len(data)}")

    if not data.empty:
        print(f"[DATA] {symbol} last candle:")
        print(data.tail(1))
        logger.info(f"[DATA] {symbol} last candle:\n{data.tail(1)}")
    else:
        logger.warning(
            "[DATA WARNING] Provider=%s | Symbol=%s returned 0 rows for period=%s interval=%s",
            active_provider,
            symbol,
            period,
            interval,
        )

    return data


def _get_data_yfinance(symbol, period, interval):
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


def _get_kite_client():
    global _kite_client
    if _kite_client is None:
        _kite_client = configure_kite_client_network(
            KiteConnect(api_key=get_api_key()),
            ip_mode=get_broker_ip_mode(),
        )
        _kite_client.set_access_token(get_access_token())
    return _kite_client


def _parse_symbol_exchange(symbol):
    if not symbol:
        raise ValueError("Symbol is required")

    if ":" in symbol:
        exchange, tradingsymbol = symbol.split(":", 1)
        return exchange.upper(), tradingsymbol.replace(".NS", "")

    return "NSE", symbol.replace(".NS", "")


def _get_kite_instrument_token(symbol):
    exchange, tradingsymbol = _parse_symbol_exchange(symbol)
    cache_key = f"{exchange}:{tradingsymbol}"
    if cache_key not in _kite_instruments_cache:
        kite = _get_kite_client()
        instruments = kite.instruments(exchange)
        for item in instruments:
            key = f"{item['exchange']}:{item['tradingsymbol']}"
            _kite_instruments_cache[key] = item["instrument_token"]

    token = _kite_instruments_cache.get(cache_key)
    if token is None:
        raise RuntimeError(f"Kite instrument token not found for {symbol}")
    return token


def _get_data_kite(symbol, period, interval):
    kite = _get_kite_client()
    instrument_token = _get_kite_instrument_token(symbol)
    from_date, to_date = _resolve_date_window(period)
    candles = kite.historical_data(
        instrument_token,
        from_date,
        to_date,
        _map_kite_interval(interval),
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


def _get_upstox_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {get_upstox_access_token()}",
    }


def _resolve_upstox_instrument_key(symbol):
    tradingsymbol = symbol.replace(".NS", "")
    if tradingsymbol in _upstox_symbol_cache:
        return _upstox_symbol_cache[tradingsymbol]

    response = broker_request(
        "GET",
        "https://api.upstox.com/v2/instruments/search",
        headers=_get_upstox_headers(),
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
            _upstox_symbol_cache[tradingsymbol] = item["instrument_key"]
            return item["instrument_key"]

    raise RuntimeError(f"Upstox instrument key not found for {symbol}")


def _get_data_upstox(symbol, period, interval):
    instrument_key = _resolve_upstox_instrument_key(symbol)
    from_date, to_date = _resolve_date_window(period)
    interval_key = _map_upstox_interval(interval)
    encoded_key = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{encoded_key}/{interval_key}/{to_date.strftime('%Y-%m-%d')}/"
        f"{from_date.strftime('%Y-%m-%d')}"
    )
    response = broker_request(
        "GET",
        url,
        headers=_get_upstox_headers(),
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


def _resolve_date_window(period):
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


def _map_kite_interval(interval):
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


def _map_upstox_interval(interval):
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
