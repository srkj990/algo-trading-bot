from datetime import date, datetime

from kiteconnect import KiteConnect

from config import (
    FNO_INDEX_SYMBOLS,
    FNO_UNDERLYING_DETAILS,
    get_access_token,
    get_api_key,
    get_default_data_provider,
)
from data_fetcher import get_data


def _get_kite_client():
    kite = KiteConnect(api_key=get_api_key())
    kite.set_access_token(get_access_token())
    return kite


def _get_fno_metadata(base_symbol):
    normalized = (base_symbol or "").strip().upper()
    metadata = FNO_UNDERLYING_DETAILS.get(normalized)
    if metadata is None:
        raise ValueError(
            f"Unsupported F&O base symbol: {base_symbol}. "
            f"Supported symbols: {', '.join(FNO_INDEX_SYMBOLS)}"
        )
    return normalized, metadata


def get_fno_display_name(base_symbol):
    _, metadata = _get_fno_metadata(base_symbol)
    return metadata["display_name"]


def get_fno_derivatives_exchange(base_symbol):
    _, metadata = _get_fno_metadata(base_symbol)
    return metadata["derivatives_exchange"]


def get_fno_spot_quote_symbol(base_symbol):
    _, metadata = _get_fno_metadata(base_symbol)
    return metadata["spot_quote_symbol"]


def _get_kite_instruments_for_base(base_symbol):
    normalized, metadata = _get_fno_metadata(base_symbol)
    kite = _get_kite_client()
    instruments = kite.instruments(metadata["derivatives_exchange"])
    filtered = []
    for item in instruments:
        tradingsymbol = (item.get("tradingsymbol") or "").upper()
        if not tradingsymbol.startswith(normalized):
            continue
        filtered.append(item)
    return filtered


def _normalize_expiry(expiry):
    if isinstance(expiry, datetime):
        return expiry.date()
    if isinstance(expiry, date):
        return expiry
    if isinstance(expiry, str):
        value = expiry.strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d%b%Y", "%d%b%y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    raise ValueError(f"Unsupported expiry format: {expiry}")


def _format_expiry(expiry):
    return _normalize_expiry(expiry).isoformat()


def _is_matching_instrument_type(item, instrument_type):
    actual = (item.get("instrument_type") or "").upper()
    requested = (instrument_type or "").upper()
    if not requested:
        return actual in {"FUT", "CE", "PE"}
    if requested == "OPT":
        return actual in {"CE", "PE"}
    return actual == requested


def get_available_expiries(base_symbol, instrument_type=None):
    expiries = set()
    for item in _get_kite_instruments_for_base(base_symbol):
        if not _is_matching_instrument_type(item, instrument_type):
            continue
        expiry = item.get("expiry")
        if not expiry:
            continue
        expiries.add(_format_expiry(expiry))
    return sorted(expiries)


def get_available_option_strikes(base_symbol, expiry, option_type=None):
    target_expiry = _format_expiry(expiry)
    requested_type = (option_type or "").upper()
    strikes = set()
    for item in _get_kite_instruments_for_base(base_symbol):
        instrument_type = (item.get("instrument_type") or "").upper()
        if instrument_type not in {"CE", "PE"}:
            continue
        if requested_type and instrument_type != requested_type:
            continue
        if _format_expiry(item.get("expiry")) != target_expiry:
            continue

        strike = item.get("strike")
        if strike is None:
            continue
        strikes.add(int(float(strike)))

    return sorted(strikes)


def get_underlying_spot_price(base_symbol):
    quote_symbol = get_fno_spot_quote_symbol(base_symbol)
    quote = _get_kite_client().ltp([quote_symbol]).get(quote_symbol, {})
    last_price = float(quote.get("last_price") or 0)
    if last_price <= 0:
        raise RuntimeError(
            f"Could not fetch spot price for {get_fno_display_name(base_symbol)} "
            f"using quote symbol {quote_symbol}."
        )
    return last_price


def get_atm_option_strike(base_symbol, expiry, option_type=None):
    del option_type
    strikes = get_available_option_strikes(base_symbol, expiry)
    if not strikes:
        raise RuntimeError(
            f"No strikes found for {get_fno_display_name(base_symbol)} {expiry}."
        )

    spot_price = get_underlying_spot_price(base_symbol)
    return min(strikes, key=lambda strike: abs(strike - spot_price))


def resolve_futures_contract(base_symbol, expiry):
    target_expiry = _format_expiry(expiry)
    exchange = get_fno_derivatives_exchange(base_symbol)
    for item in _get_kite_instruments_for_base(base_symbol):
        if (item.get("instrument_type") or "").upper() != "FUT":
            continue
        if _format_expiry(item.get("expiry")) != target_expiry:
            continue
        return f"{exchange}:{item['tradingsymbol']}"

    raise RuntimeError(
        f"Futures contract not found for {get_fno_display_name(base_symbol)} "
        f"expiry {target_expiry}."
    )


def resolve_nearest_futures_contract(base_symbol):
    expiries = get_available_expiries(base_symbol, instrument_type="FUT")
    if not expiries:
        raise RuntimeError(
            f"No active futures expiries found for {get_fno_display_name(base_symbol)}."
        )
    return resolve_futures_contract(base_symbol, expiries[0])


def resolve_option_contract(base_symbol, expiry, strike, option_type):
    target_expiry = _format_expiry(expiry)
    exchange = get_fno_derivatives_exchange(base_symbol)
    requested_type = (option_type or "").upper()
    requested_strike = int(strike)

    for item in _get_kite_instruments_for_base(base_symbol):
        instrument_type = (item.get("instrument_type") or "").upper()
        if instrument_type != requested_type:
            continue
        if _format_expiry(item.get("expiry")) != target_expiry:
            continue
        if int(float(item.get("strike") or 0)) != requested_strike:
            continue
        return f"{exchange}:{item['tradingsymbol']}"

    raise RuntimeError(
        f"Option contract not found for {get_fno_display_name(base_symbol)} "
        f"expiry {target_expiry}, strike {requested_strike}, type {requested_type}."
    )


def get_futures_data(base_symbol, period="3mo", interval="5m", provider=None):
    active_provider = (provider or get_default_data_provider() or "KITE").upper()
    if active_provider != "KITE":
        raise ValueError(
            "F&O data is currently supported only via KITE provider. "
            "Set data provider to KITE."
        )

    symbol = resolve_nearest_futures_contract(base_symbol)
    return get_data(symbol, period=period, interval=interval, provider="KITE")


def get_options_data(symbol, period="2mo", interval="15m", provider=None):
    active_provider = (provider or get_default_data_provider() or "KITE").upper()
    if active_provider != "KITE":
        raise ValueError(
            "F&O data is currently supported only via KITE provider. "
            "Set data provider to KITE."
        )

    return get_data(symbol, period=period, interval=interval, provider="KITE")
