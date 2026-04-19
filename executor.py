import requests
from kiteconnect import KiteConnect

from config import (
    get_access_token,
    get_api_key,
    get_default_execution_provider,
    get_upstox_access_token,
)
from logger import log_event


EXECUTION_MODE = "PAPER"
EXECUTION_PROVIDER = get_default_execution_provider()
_kite_client = None
_kite_instruments_cache = {}
_upstox_symbol_cache = {}


def set_execution_mode(mode):
    global EXECUTION_MODE
    EXECUTION_MODE = mode.upper()


def set_execution_provider(provider):
    global EXECUTION_PROVIDER
    EXECUTION_PROVIDER = (provider or "KITE").upper()


def get_execution_provider():
    return EXECUTION_PROVIDER


def _get_kite_client():
    global _kite_client
    if _kite_client is None:
        _kite_client = KiteConnect(api_key=get_api_key())
        _kite_client.set_access_token(get_access_token())
    return _kite_client


def _get_kite_instrument(symbol):
    tradingsymbol = symbol.replace(".NS", "")
    cache_key = f"NSE:{tradingsymbol}"
    if cache_key not in _kite_instruments_cache:
        kite = _get_kite_client()
        instruments = kite.instruments("NSE")
        for item in instruments:
            key = f"{item['exchange']}:{item['tradingsymbol']}"
            _kite_instruments_cache[key] = item
    instrument = _kite_instruments_cache.get(cache_key)
    if instrument is None:
        raise RuntimeError(f"Kite instrument metadata not found for {symbol}")
    return instrument


def _upstox_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {get_upstox_access_token()}",
    }


def _get_upstox_instrument_key(symbol):
    tradingsymbol = symbol.replace(".NS", "")
    if tradingsymbol in _upstox_symbol_cache:
        return _upstox_symbol_cache[tradingsymbol]

    response = requests.get(
        "https://api.upstox.com/v2/instruments/search",
        headers=_upstox_headers(),
        params={
            "query": tradingsymbol,
            "exchanges": "NSE",
            "segments": "EQ",
            "page_number": 1,
            "records": 10,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    for item in payload.get("data", []):
        if item.get("exchange") == "NSE" and item.get("trading_symbol") == tradingsymbol:
            _upstox_symbol_cache[tradingsymbol] = item["instrument_key"]
            return item["instrument_key"]

    raise RuntimeError(f"Upstox instrument key not found for {symbol}")


def _kite_product_constant(product):
    kite = _get_kite_client()
    normalized = (product or "MIS").upper()
    mapping = {
        "MIS": kite.PRODUCT_MIS,
        "CNC": kite.PRODUCT_CNC,
        "NRML": kite.PRODUCT_NRML,
    }
    return mapping.get(normalized, kite.PRODUCT_MIS)


def _upstox_product_constant(product):
    normalized = (product or "MIS").upper()
    mapping = {
        "MIS": "I",
        "CNC": "D",
        "NRML": "D",
    }
    return mapping.get(normalized, "I")


def place_order(signal, quantity, symbol, note=None, product="MIS"):
    log_event("\n[EXECUTION] Preparing order...")
    log_event(f"[EXECUTION] Provider: {EXECUTION_PROVIDER}")
    log_event(f"[EXECUTION] Symbol: {symbol.replace('.NS', '')}")
    log_event(f"[EXECUTION] Signal: {signal}")
    log_event(f"[EXECUTION] Quantity: {quantity}")
    log_event(f"[EXECUTION] Mode: {EXECUTION_MODE}")
    log_event(f"[EXECUTION] Product: {(product or 'MIS').upper()}")

    if note:
        log_event(f"[EXECUTION] Note: {note}")

    if EXECUTION_MODE != "LIVE":
        log_event("Order NOT placed (paper mode)")
        return None

    if EXECUTION_PROVIDER == "KITE":
        return _place_order_kite(signal, quantity, symbol, product)
    if EXECUTION_PROVIDER == "UPSTOX":
        return _place_order_upstox(signal, quantity, symbol, product, note)

    raise ValueError(f"Unsupported execution provider: {EXECUTION_PROVIDER}")


def _place_order_kite(signal, quantity, symbol, product):
    kite = _get_kite_client()
    tradingsymbol = symbol.replace(".NS", "")
    transaction_type = (
        kite.TRANSACTION_TYPE_BUY if signal == "BUY" else kite.TRANSACTION_TYPE_SELL
    )
    return kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol=tradingsymbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=_kite_product_constant(product),
        order_type=kite.ORDER_TYPE_MARKET,
    )


def _place_order_upstox(signal, quantity, symbol, product, note):
    payload = {
        "quantity": quantity,
        "product": _upstox_product_constant(product),
        "validity": "DAY",
        "price": 0,
        "tag": (note or "algo")[:40],
        "instrument_token": _get_upstox_instrument_key(symbol),
        "order_type": "MARKET",
        "transaction_type": signal,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }
    response = requests.post(
        "https://api-hft.upstox.com/v2/order/place",
        headers=_upstox_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("data", {}).get("order_id")


def get_intraday_positions():
    if EXECUTION_PROVIDER == "KITE":
        return _get_intraday_positions_kite()
    if EXECUTION_PROVIDER == "UPSTOX":
        return _get_intraday_positions_upstox()
    raise ValueError(f"Unsupported execution provider: {EXECUTION_PROVIDER}")


def get_delivery_holdings():
    if EXECUTION_PROVIDER == "KITE":
        return _get_delivery_holdings_kite()
    if EXECUTION_PROVIDER == "UPSTOX":
        return _get_delivery_holdings_upstox()
    raise ValueError(f"Unsupported execution provider: {EXECUTION_PROVIDER}")


def _get_intraday_positions_kite():
    kite = _get_kite_client()
    response = kite.positions()
    return [
        item
        for item in response.get("net", [])
        if (item.get("product") or "").upper() == "MIS"
    ]


def _get_delivery_holdings_kite():
    kite = _get_kite_client()
    return kite.holdings()


def _get_intraday_positions_upstox():
    response = requests.get(
        "https://api.upstox.com/v2/portfolio/short-term-positions",
        headers=_upstox_headers(),
        timeout=30,
    )
    response.raise_for_status()
    positions = []
    for item in response.json().get("data", []):
        if (item.get("product") or "").upper() != "I":
            continue
        positions.append(
            {
                "tradingsymbol": item.get("trading_symbol"),
                "quantity": int(item.get("quantity") or item.get("net_quantity") or 0),
                "average_price": float(item.get("average_price") or item.get("buy_price") or 0),
                "product": "MIS",
            }
        )
    return positions


def _get_delivery_holdings_upstox():
    response = requests.get(
        "https://api.upstox.com/v2/portfolio/long-term-holdings",
        headers=_upstox_headers(),
        timeout=30,
    )
    response.raise_for_status()
    holdings = []
    for item in response.json().get("data", []):
        holdings.append(
            {
                "tradingsymbol": item.get("trading_symbol"),
                "quantity": int(item.get("quantity") or 0),
                "t1_quantity": int(item.get("t1_quantity") or 0),
                "average_price": float(item.get("average_price") or 0),
                "last_price": float(item.get("last_price") or 0),
            }
        )
    return holdings
