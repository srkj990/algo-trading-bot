from datetime import date, datetime

from kiteconnect import KiteConnect

from config import (
    FNO_INDEX_SYMBOLS,
    FNO_GREEKS_HISTORY_PERIOD,
    FNO_UNDERLYING_DETAILS,
    FNO_DEFAULT_RISK_FREE_RATE,
    get_access_token,
    get_api_key,
    get_default_data_provider,
)
from data_fetcher import get_data
from indicators import compute_vwap
from option_analytics import calculate_greeks, implied_volatility, years_to_expiry


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


def _parse_symbol_exchange(symbol):
    if not symbol or ":" not in symbol:
        raise ValueError(f"Expected exchange-prefixed symbol, got: {symbol}")
    exchange, tradingsymbol = symbol.split(":", 1)
    return exchange.upper(), tradingsymbol


def get_contract_metadata(symbol):
    exchange, tradingsymbol = _parse_symbol_exchange(symbol)
    kite = _get_kite_client()
    for item in kite.instruments(exchange):
        if (item.get("tradingsymbol") or "").upper() != tradingsymbol.upper():
            continue
        return {
            "exchange": exchange,
            "tradingsymbol": item["tradingsymbol"],
            "instrument_type": (item.get("instrument_type") or "").upper(),
            "strike": float(item.get("strike") or 0.0),
            "expiry": _format_expiry(item.get("expiry")) if item.get("expiry") else None,
            "lot_size": int(item.get("lot_size") or 1),
            "tick_size": float(item.get("tick_size") or 0.05),
            "name": item.get("name"),
            "segment": item.get("segment"),
        }
    raise RuntimeError(f"Contract metadata not found for {symbol}")


def get_contract_underlying_base(symbol):
    metadata = get_contract_metadata(symbol)
    name = (metadata.get("name") or "").upper()
    if name in FNO_UNDERLYING_DETAILS:
        return name

    tradingsymbol = (metadata.get("tradingsymbol") or "").upper()
    for base_symbol in FNO_INDEX_SYMBOLS:
        if tradingsymbol.startswith(base_symbol):
            return base_symbol

    raise RuntimeError(f"Could not infer underlying for {symbol}")


def get_contract_lot_size(symbol):
    return get_contract_metadata(symbol).get("lot_size", 1)


def get_contract_last_price(symbol):
    quote = _get_kite_client().ltp([symbol]).get(symbol, {})
    return float(quote.get("last_price") or 0.0)


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


def get_option_greeks_snapshot(
    symbol,
    risk_free_rate=None,
    iv_history_period=None,
):
    metadata = get_contract_metadata(symbol)
    option_type = metadata["instrument_type"]
    if option_type not in {"CE", "PE"}:
        raise ValueError(f"Greeks are supported only for option contracts, got {symbol}")

    underlying = get_contract_underlying_base(symbol)
    option_price = get_contract_last_price(symbol)
    underlying_price = get_underlying_spot_price(underlying)
    time_to_expiry = years_to_expiry(metadata["expiry"])
    active_risk_free_rate = (
        float(risk_free_rate)
        if risk_free_rate is not None
        else float(FNO_DEFAULT_RISK_FREE_RATE)
    )

    implied_vol = implied_volatility(
        option_price=option_price,
        spot=underlying_price,
        strike=metadata["strike"],
        time_to_expiry=time_to_expiry,
        risk_free_rate=active_risk_free_rate,
        option_type=option_type,
    )
    greeks = calculate_greeks(
        spot=underlying_price,
        strike=metadata["strike"],
        time_to_expiry=time_to_expiry,
        risk_free_rate=active_risk_free_rate,
        volatility=implied_vol,
        option_type=option_type,
    )

    history_period = iv_history_period or FNO_GREEKS_HISTORY_PERIOD
    iv_rank = None
    iv_percentile = None
    try:
        history = get_options_data(
            symbol,
            period=history_period,
            interval="1d",
            provider="KITE",
        )
        iv_history = []
        for _, row in history.tail(60).iterrows():
            close_price = float(row["Close"] or 0.0)
            iv_value = implied_volatility(
                option_price=close_price,
                spot=underlying_price,
                strike=metadata["strike"],
                time_to_expiry=time_to_expiry,
                risk_free_rate=active_risk_free_rate,
                option_type=option_type,
            )
            if iv_value > 0:
                iv_history.append(iv_value)
        if iv_history:
            iv_low = min(iv_history)
            iv_high = max(iv_history)
            if iv_high > iv_low:
                iv_rank = ((implied_vol - iv_low) / (iv_high - iv_low)) * 100.0
            percentile_hits = sum(1 for item in iv_history if item <= implied_vol)
            iv_percentile = (percentile_hits / len(iv_history)) * 100.0
    except Exception:
        iv_rank = None
        iv_percentile = None

    snapshot = {
        "symbol": symbol,
        "underlying": underlying,
        "underlying_price": underlying_price,
        "option_price": option_price,
        "strike": metadata["strike"],
        "option_type": option_type,
        "expiry": metadata["expiry"],
        "days_to_expiry": max(int(round(time_to_expiry * 365)), 0),
        "time_to_expiry_years": time_to_expiry,
        "lot_size": metadata["lot_size"],
        "iv": implied_vol,
        "iv_rank": iv_rank,
        "iv_percentile": iv_percentile,
        "risk_free_rate": active_risk_free_rate,
    }

    try:
        option_intraday = get_options_data(
            symbol,
            period="2d",
            interval="1m",
            provider="KITE",
        )
        underlying_intraday = get_data(
            get_fno_spot_quote_symbol(underlying),
            period="2d",
            interval="1m",
            provider="KITE",
        )
        if len(option_intraday) >= 16 and len(underlying_intraday) >= 16:
            option_prev_close = float(option_intraday["Close"].iloc[-16])
            underlying_prev_close = float(underlying_intraday["Close"].iloc[-16])
            previous_iv = implied_volatility(
                option_price=option_prev_close,
                spot=underlying_prev_close,
                strike=metadata["strike"],
                time_to_expiry=time_to_expiry,
                risk_free_rate=active_risk_free_rate,
                option_type=option_type,
            )
            if previous_iv > 0:
                snapshot["iv_15m_ago"] = previous_iv
                snapshot["iv_change_15m_pct"] = (
                    (implied_vol - previous_iv) / previous_iv
                ) * 100.0
        if not option_intraday.empty:
            snapshot["option_vwap"] = float(compute_vwap(option_intraday).iloc[-1])
    except Exception:
        pass

    snapshot.update(greeks)
    return snapshot


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
