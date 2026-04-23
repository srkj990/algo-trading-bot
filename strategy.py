import pandas as pd

from config import MIN_CANDLES
from indicators import compute_atr, compute_rsi, compute_vwap
from logger import get_logger

logger = get_logger()

OPTION_SIGNAL_TO_EXECUTION = {
    "BUY_CE": "BUY",
    "BUY_PE": "BUY",
    "NO_TRADE": "HOLD",
}
OPTION_STRATEGIES = {
    "ATM_MOMENTUM",
    "ATM_ORB",
    "ATM_VWAP_REVERSION",
    "ATM_MULTI",
    "ATM_BREAKOUT_EXPANSION",
    "ATM_IV_EXPANSION",
    "ATM_TRAP_REVERSAL",
}
OPTION_STRATEGY_MIN_CANDLES = {
    "ATM_MOMENTUM": 20,
    "ATM_ORB": 16,
    "ATM_VWAP_REVERSION": 20,
    "ATM_MULTI": 20,
    "ATM_BREAKOUT_EXPANSION": 45,
    "ATM_IV_EXPANSION": 30,
    "ATM_TRAP_REVERSAL": 24,
}


def _clip_strength(value):
    return max(0.0, min(1.0, float(value)))


def _build_signal_payload(signal, strength, reason, strategy_name, **extra):
    option_type = None
    if signal == "BUY_CE":
        option_type = "CE"
    elif signal == "BUY_PE":
        option_type = "PE"
    payload = {
        "signal": signal,
        "strength": _clip_strength(strength),
        "reason": str(reason),
        "strategy": strategy_name,
        "execution_signal": OPTION_SIGNAL_TO_EXECUTION.get(signal, signal),
        "option_type": option_type,
        "option_signal": signal if signal in {"BUY_CE", "BUY_PE"} else None,
    }
    payload.update(extra)
    return payload


def _is_option_strategy(strategy_name):
    return strategy_name in OPTION_STRATEGIES


def get_required_candles(strategy_name):
    if _is_option_strategy(strategy_name):
        return OPTION_STRATEGY_MIN_CANDLES[strategy_name]
    return MIN_CANDLES.get(strategy_name, 1)


def has_enough_data(df, strategy_name):
    required = get_required_candles(strategy_name)
    available = len(df)

    logger.info("%s -> Required: %s, Available: %s", strategy_name, required, available)
    print(f"{strategy_name} -> Required: {required}, Available: {available}")

    if available < required:
        logger.warning("Not enough data for %s", strategy_name)
        return False

    return True


def _get_session_df(df):
    if df.empty:
        return df
    trade_date = df.index[-1].date()
    return df.loc[df.index.date == trade_date].copy()


def _no_trade(reason, strategy_name, **extra):
    return _build_signal_payload(
        signal="NO_TRADE",
        strength=0.0,
        reason=reason,
        strategy_name=strategy_name,
        **extra,
    )


def _legacy_payload(signal, strategy_name, reason):
    return {
        "signal": signal,
        "strength": 1.0 if signal in {"BUY", "SELL"} else 0.0,
        "reason": reason,
        "strategy": strategy_name,
        "execution_signal": signal,
        "option_type": None,
        "option_signal": None,
    }


def ma_strategy(df):
    df = df.copy()
    df["ma20"] = df["Close"].rolling(20).mean()
    df["ma50"] = df["Close"].rolling(50).mean()

    latest = df.iloc[-1]
    ma20 = float(latest["ma20"])
    ma50 = float(latest["ma50"])

    if pd.isna(ma20) or pd.isna(ma50):
        return "HOLD"

    if ma20 > ma50:
        return "BUY"
    if ma20 < ma50:
        return "SELL"
    return "HOLD"


def rsi_strategy(df):
    df = df.copy()
    df["rsi"] = compute_rsi(df["Close"])

    latest = df.iloc[-1]
    rsi = float(latest["rsi"])

    if pd.isna(rsi):
        return "HOLD"

    if rsi < 30:
        return "BUY"
    if rsi > 70:
        return "SELL"
    return "HOLD"


def breakout_strategy(df):
    latest = df.iloc[-1]
    prev_high = df["High"].rolling(20).max().iloc[-2]
    prev_low = df["Low"].rolling(20).min().iloc[-2]
    close = float(latest["Close"])

    if close > prev_high:
        return "BUY"
    if close < prev_low:
        return "SELL"
    return "HOLD"


def vwap_strategy(df):
    df = df.copy()
    df["vwap"] = compute_vwap(df)

    latest = df.iloc[-1]
    close = float(latest["Close"])
    vwap = float(latest["vwap"])

    if pd.isna(vwap):
        return "HOLD"

    if close > vwap:
        return "BUY"
    if close < vwap:
        return "SELL"
    return "HOLD"


def orb_strategy(df):
    if len(df) < 20:
        return "HOLD"

    first_15 = df.iloc[:15]
    high = first_15["High"].max()
    low = first_15["Low"].min()
    latest = df.iloc[-1]
    close = float(latest["Close"])

    if close > high:
        return "BUY"
    if close < low:
        return "SELL"
    return "HOLD"


def strategy_momentum(
    df,
    breakout_lookback=5,
    rsi_period=14,
    bullish_rsi=60,
    bearish_rsi=40,
):
    strategy_name = "ATM_MOMENTUM"
    session_df = _get_session_df(df)
    minimum = max(rsi_period + 1, breakout_lookback + 2)
    if len(session_df) < minimum:
        return _no_trade(
            f"Need at least {minimum} session candles for momentum setup",
            strategy_name,
        )

    enriched = session_df.copy()
    enriched["rsi"] = compute_rsi(enriched["Close"], period=rsi_period)
    enriched["vwap"] = compute_vwap(enriched)

    latest = enriched.iloc[-1]
    prior_window = enriched.iloc[-(breakout_lookback + 1):-1]
    breakout_high = float(prior_window["High"].max())
    breakout_low = float(prior_window["Low"].min())
    close = float(latest["Close"])
    vwap = float(latest["vwap"])
    rsi = float(latest["rsi"])

    if pd.isna(vwap) or pd.isna(rsi):
        return _no_trade("RSI or VWAP not available yet", strategy_name)

    if close > vwap and rsi > bullish_rsi and close > breakout_high:
        strength = (
            ((rsi - bullish_rsi) / 40.0)
            + ((close - vwap) / max(abs(vwap), 1.0))
            + ((close - breakout_high) / max(abs(close), 1.0))
        )
        return _build_signal_payload(
            signal="BUY_CE",
            strength=strength,
            reason=(
                f"Momentum bullish: close {close:.2f} above VWAP {vwap:.2f}, "
                f"RSI {rsi:.1f}, breakout above {breakout_high:.2f}"
            ),
            strategy_name=strategy_name,
        )

    if close < vwap and rsi < bearish_rsi and close < breakout_low:
        strength = (
            ((bearish_rsi - rsi) / 40.0)
            + ((vwap - close) / max(abs(vwap), 1.0))
            + ((breakout_low - close) / max(abs(close), 1.0))
        )
        return _build_signal_payload(
            signal="BUY_PE",
            strength=strength,
            reason=(
                f"Momentum bearish: close {close:.2f} below VWAP {vwap:.2f}, "
                f"RSI {rsi:.1f}, breakdown below {breakout_low:.2f}"
            ),
            strategy_name=strategy_name,
        )

    return _no_trade(
        (
            f"Momentum conditions unmet: close={close:.2f}, VWAP={vwap:.2f}, "
            f"RSI={rsi:.1f}, breakout_high={breakout_high:.2f}, "
            f"breakout_low={breakout_low:.2f}"
        ),
        strategy_name,
    )


def strategy_orb(df, opening_range_minutes=15):
    strategy_name = "ATM_ORB"
    session_df = _get_session_df(df)
    if len(session_df) <= opening_range_minutes:
        return _no_trade(
            f"Need more than {opening_range_minutes} candles to confirm ORB breakout",
            strategy_name,
        )

    opening_range = session_df.iloc[:opening_range_minutes]
    latest = session_df.iloc[-1]
    orb_high = float(opening_range["High"].max())
    orb_low = float(opening_range["Low"].min())
    close = float(latest["Close"])

    if close > orb_high:
        strength = (close - orb_high) / max(abs(close), 1.0)
        return _build_signal_payload(
            signal="BUY_CE",
            strength=strength,
            reason=f"ORB upside breakout above {orb_high:.2f} with close {close:.2f}",
            strategy_name=strategy_name,
        )

    if close < orb_low:
        strength = (orb_low - close) / max(abs(close), 1.0)
        return _build_signal_payload(
            signal="BUY_PE",
            strength=strength,
            reason=f"ORB downside breakout below {orb_low:.2f} with close {close:.2f}",
            strategy_name=strategy_name,
        )

    return _no_trade(
        f"Price {close:.2f} remains inside opening range {orb_low:.2f}-{orb_high:.2f}",
        strategy_name,
    )


def strategy_vwap(df, deviation_threshold=0.0035, lookback=6):
    strategy_name = "ATM_VWAP_REVERSION"
    session_df = _get_session_df(df)
    minimum = max(lookback + 2, 8)
    if len(session_df) < minimum:
        return _no_trade(
            f"Need at least {minimum} session candles for VWAP reversion",
            strategy_name,
        )

    enriched = session_df.copy()
    enriched["vwap"] = compute_vwap(enriched)
    enriched["deviation"] = (
        (enriched["Close"] - enriched["vwap"]) / enriched["vwap"].replace(0, pd.NA)
    ).fillna(0.0)

    latest = enriched.iloc[-1]
    previous = enriched.iloc[-2]
    prior_deviation = enriched["deviation"].iloc[-(lookback + 1):-1]
    max_positive = float(prior_deviation.max())
    min_negative = float(prior_deviation.min())
    close = float(latest["Close"])
    vwap = float(latest["vwap"])

    bullish_reentry = (
        min_negative <= -abs(deviation_threshold)
        and float(previous["Close"]) <= float(previous["vwap"])
        and close >= vwap
    )
    bearish_reentry = (
        max_positive >= abs(deviation_threshold)
        and float(previous["Close"]) >= float(previous["vwap"])
        and close <= vwap
    )

    if bullish_reentry:
        strength = abs(min_negative) / max(abs(deviation_threshold), 1e-6)
        return _build_signal_payload(
            signal="BUY_CE",
            strength=strength,
            reason=(
                f"VWAP reversion long: prior deviation {min_negative:.4f}, "
                f"price re-entered above VWAP {vwap:.2f}"
            ),
            strategy_name=strategy_name,
        )

    if bearish_reentry:
        strength = abs(max_positive) / max(abs(deviation_threshold), 1e-6)
        return _build_signal_payload(
            signal="BUY_PE",
            strength=strength,
            reason=(
                f"VWAP reversion short: prior deviation {max_positive:.4f}, "
                f"price re-entered below VWAP {vwap:.2f}"
            ),
            strategy_name=strategy_name,
        )

    return _no_trade(
        (
            f"No VWAP reversion setup: latest close={close:.2f}, vwap={vwap:.2f}, "
            f"max_positive_dev={max_positive:.4f}, min_negative_dev={min_negative:.4f}"
        ),
        strategy_name,
    )


def strategy_multi(df, sideways_atr_threshold=0.0035):
    strategy_name = "ATM_MULTI"
    momentum = strategy_momentum(df)
    orb = strategy_orb(df)
    vwap_reversion = strategy_vwap(df)

    actionable = [
        item for item in (momentum, orb)
        if item["signal"] in {"BUY_CE", "BUY_PE"}
    ]
    if len(actionable) == 2:
        if actionable[0]["signal"] == actionable[1]["signal"]:
            strength = (actionable[0]["strength"] + actionable[1]["strength"]) / 2.0
            return _build_signal_payload(
                signal=actionable[0]["signal"],
                strength=max(0.75, strength),
                reason=(
                    f"Momentum and ORB aligned: {actionable[0]['reason']} | "
                    f"{actionable[1]['reason']}"
                ),
                strategy_name=strategy_name,
                components={
                    "momentum": momentum,
                    "orb": orb,
                    "vwap": vwap_reversion,
                },
            )
        return _no_trade(
            "Momentum and ORB conflict, so multi-strategy is standing aside",
            strategy_name,
            components={
                "momentum": momentum,
                "orb": orb,
                "vwap": vwap_reversion,
            },
        )

    close = float(df.iloc[-1]["Close"]) if not df.empty else 0.0
    atr_series = compute_atr(df)
    atr_value = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
    atr_ratio = (atr_value / close) if close > 0 and atr_value == atr_value else 0.0
    is_sideways = atr_ratio <= sideways_atr_threshold

    if is_sideways and vwap_reversion["signal"] in {"BUY_CE", "BUY_PE"}:
        return _build_signal_payload(
            signal=vwap_reversion["signal"],
            strength=max(0.6, vwap_reversion["strength"]),
            reason=(
                f"Sideways regime detected (ATR ratio {atr_ratio:.4f}); "
                f"using VWAP reversion: {vwap_reversion['reason']}"
            ),
            strategy_name=strategy_name,
            components={
                "momentum": momentum,
                "orb": orb,
                "vwap": vwap_reversion,
            },
        )

    return _no_trade(
        (
            f"Multi-strategy found no aligned momentum/ORB setup and "
            f"sideways ATR ratio is {atr_ratio:.4f}"
        ),
        strategy_name,
        components={
            "momentum": momentum,
            "orb": orb,
            "vwap": vwap_reversion,
        },
    )


def strategy_breakout_expansion(
    df,
    compression_lookback=45,
    breakout_window=30,
    volume_window=20,
    compression_range_pct=0.0045,
    volume_spike_multiplier=1.8,
    atr_period=14,
    atr_expansion_multiplier=1.1,
):
    strategy_name = "ATM_BREAKOUT_EXPANSION"
    session_df = _get_session_df(df)
    minimum = max(compression_lookback, breakout_window + 2, volume_window + 2, atr_period + 5)
    if len(session_df) < minimum:
        return _no_trade(
            f"Need at least {minimum} session candles for breakout expansion",
            strategy_name,
        )

    enriched = session_df.copy()
    enriched["atr"] = compute_atr(enriched, period=atr_period)
    latest = enriched.iloc[-1]
    prior_window = enriched.iloc[-(breakout_window + 1):-1]
    compression_window = enriched.iloc[-compression_lookback:-1]
    volume_reference = enriched["Volume"].iloc[-(volume_window + 1):-1]
    prior_atr_window = enriched["atr"].iloc[-6:-1]

    close = float(latest["Close"])
    high = float(prior_window["High"].max())
    low = float(prior_window["Low"].min())
    compression_range = (
        (float(compression_window["High"].max()) - float(compression_window["Low"].min()))
        / max(abs(close), 1.0)
    )
    latest_volume = float(latest["Volume"])
    average_volume = float(volume_reference.mean()) if not volume_reference.empty else 0.0
    current_atr = float(latest["atr"]) if latest["atr"] == latest["atr"] else 0.0
    prior_atr = float(prior_atr_window.mean()) if not prior_atr_window.empty else 0.0

    compression_ok = compression_range <= compression_range_pct
    volume_available = latest_volume > 0 and average_volume > 0
    volume_spike = (
        latest_volume >= (average_volume * volume_spike_multiplier)
        if volume_available
        else True
    )
    atr_expanding = current_atr > 0 and prior_atr > 0 and current_atr >= (prior_atr * atr_expansion_multiplier)

    if close > high and compression_ok and volume_spike and atr_expanding:
        strength = (
            ((close - high) / max(abs(close), 1.0))
            + (latest_volume / max(average_volume, 1.0) / 10.0)
            + (current_atr / max(prior_atr, 1e-6) / 10.0)
        )
        return _build_signal_payload(
            signal="BUY_CE",
            strength=strength,
            reason=(
                f"Breakout expansion long: close {close:.2f} above {high:.2f}, "
                f"compression {compression_range:.4f}, volume spike {latest_volume:.0f}/{average_volume:.0f}, "
                f"ATR {current_atr:.4f}>{prior_atr:.4f}"
            ),
            strategy_name=strategy_name,
        )

    if close < low and compression_ok and volume_spike and atr_expanding:
        strength = (
            ((low - close) / max(abs(close), 1.0))
            + (latest_volume / max(average_volume, 1.0) / 10.0)
            + (current_atr / max(prior_atr, 1e-6) / 10.0)
        )
        return _build_signal_payload(
            signal="BUY_PE",
            strength=strength,
            reason=(
                f"Breakout expansion short: close {close:.2f} below {low:.2f}, "
                f"compression {compression_range:.4f}, volume spike {latest_volume:.0f}/{average_volume:.0f}, "
                f"ATR {current_atr:.4f}>{prior_atr:.4f}"
            ),
            strategy_name=strategy_name,
        )

    return _no_trade(
        (
            f"No breakout expansion setup: close={close:.2f}, high={high:.2f}, low={low:.2f}, "
            f"compression={compression_range:.4f}, volume_available={volume_available}, "
            f"vol_spike={volume_spike}, atr_expanding={atr_expanding}"
        ),
        strategy_name,
    )


def strategy_iv_expansion(
    df,
    key_level_window=20,
    body_lookback=10,
    body_expansion_multiplier=1.8,
    rsi_period=14,
):
    strategy_name = "ATM_IV_EXPANSION"
    session_df = _get_session_df(df)
    minimum = max(key_level_window + 2, body_lookback + 2, rsi_period + 2)
    if len(session_df) < minimum:
        return _no_trade(
            f"Need at least {minimum} session candles for IV expansion setup",
            strategy_name,
        )

    enriched = session_df.copy()
    enriched["rsi"] = compute_rsi(enriched["Close"], period=rsi_period)
    latest = enriched.iloc[-1]
    prior = enriched.iloc[-(key_level_window + 1):-1]
    body_reference = (enriched["Close"] - enriched["Open"]).abs().iloc[-(body_lookback + 1):-1]

    recent_high = float(prior["High"].max())
    recent_low = float(prior["Low"].min())
    close = float(latest["Close"])
    open_price = float(latest["Open"])
    candle_body = abs(close - open_price)
    avg_body = float(body_reference.mean()) if not body_reference.empty else 0.0
    rsi = float(latest["rsi"]) if latest["rsi"] == latest["rsi"] else 50.0

    bullish_momentum = close > recent_high and candle_body >= (avg_body * body_expansion_multiplier) and rsi >= 55
    bearish_momentum = close < recent_low and candle_body >= (avg_body * body_expansion_multiplier) and rsi <= 45

    if bullish_momentum:
        strength = (
            ((close - recent_high) / max(abs(close), 1.0))
            + (candle_body / max(avg_body, 1e-6) / 10.0)
            + max(0.0, (rsi - 55) / 100.0)
        )
        return _build_signal_payload(
            signal="BUY_CE",
            strength=strength,
            reason=(
                f"IV expansion bullish trigger near key level {recent_high:.2f}: "
                f"close {close:.2f}, body {candle_body:.2f}, RSI {rsi:.1f}"
            ),
            strategy_name=strategy_name,
        )

    if bearish_momentum:
        strength = (
            ((recent_low - close) / max(abs(close), 1.0))
            + (candle_body / max(avg_body, 1e-6) / 10.0)
            + max(0.0, (45 - rsi) / 100.0)
        )
        return _build_signal_payload(
            signal="BUY_PE",
            strength=strength,
            reason=(
                f"IV expansion bearish trigger near key level {recent_low:.2f}: "
                f"close {close:.2f}, body {candle_body:.2f}, RSI {rsi:.1f}"
            ),
            strategy_name=strategy_name,
        )

    return _no_trade(
        (
            f"No IV expansion trigger: close={close:.2f}, high={recent_high:.2f}, low={recent_low:.2f}, "
            f"body={candle_body:.2f}, avg_body={avg_body:.2f}, RSI={rsi:.1f}"
        ),
        strategy_name,
    )


def strategy_trap_reversal(
    df,
    support_window=20,
    trap_confirmation_candles=3,
    reversal_body_multiplier=1.5,
):
    strategy_name = "ATM_TRAP_REVERSAL"
    session_df = _get_session_df(df)
    minimum = max(support_window + trap_confirmation_candles + 3, 24)
    if len(session_df) < minimum:
        return _no_trade(
            f"Need at least {minimum} session candles for trap reversal",
            strategy_name,
        )

    enriched = session_df.copy()
    latest = enriched.iloc[-1]
    recent = enriched.iloc[-(trap_confirmation_candles + 1):-1]
    support_base = enriched.iloc[-(support_window + trap_confirmation_candles + 1):-(trap_confirmation_candles + 1)]
    support = float(support_base["Low"].min())
    resistance = float(support_base["High"].max())
    avg_body = float((support_base["Close"] - support_base["Open"]).abs().tail(10).mean())
    latest_body = abs(float(latest["Close"]) - float(latest["Open"]))

    seller_trap = (
        float(recent["Low"].min()) < support
        and float(latest["Close"]) > support
        and float(latest["Close"]) > float(latest["Open"])
        and latest_body >= (avg_body * reversal_body_multiplier)
    )
    buyer_trap = (
        float(recent["High"].max()) > resistance
        and float(latest["Close"]) < resistance
        and float(latest["Close"]) < float(latest["Open"])
        and latest_body >= (avg_body * reversal_body_multiplier)
    )

    if seller_trap:
        strength = (
            ((float(latest["Close"]) - support) / max(abs(float(latest["Close"])), 1.0))
            + (latest_body / max(avg_body, 1e-6) / 10.0)
        )
        return _build_signal_payload(
            signal="BUY_CE",
            strength=strength,
            reason=(
                f"Seller trap detected: support {support:.2f} broke and failed within "
                f"{trap_confirmation_candles} candles; close recovered to {float(latest['Close']):.2f}"
            ),
            strategy_name=strategy_name,
        )

    if buyer_trap:
        strength = (
            ((resistance - float(latest["Close"])) / max(abs(float(latest["Close"])), 1.0))
            + (latest_body / max(avg_body, 1e-6) / 10.0)
        )
        return _build_signal_payload(
            signal="BUY_PE",
            strength=strength,
            reason=(
                f"Buyer trap detected: resistance {resistance:.2f} broke and failed within "
                f"{trap_confirmation_candles} candles; close fell back to {float(latest['Close']):.2f}"
            ),
            strategy_name=strategy_name,
        )

    return _no_trade(
        (
            f"No trap reversal setup: support={support:.2f}, resistance={resistance:.2f}, "
            f"latest_close={float(latest['Close']):.2f}, latest_body={latest_body:.2f}"
        ),
        strategy_name,
    )


def _evaluate_legacy_signal(df, strategy_name):
    if strategy_name == "MA":
        signal = confirm_signal(df, ma_strategy)
    elif strategy_name == "RSI":
        signal = confirm_signal(df, rsi_strategy)
    elif strategy_name == "BREAKOUT":
        signal = confirm_signal(df, breakout_strategy)
    elif strategy_name == "VWAP":
        signal = confirm_signal(df, vwap_strategy)
    elif strategy_name == "ORB":
        signal = confirm_signal(df, orb_strategy)
    else:
        logger.info("[STRATEGY] Invalid strategy")
        print("[STRATEGY] Invalid strategy")
        signal = "HOLD"
    return _legacy_payload(signal, strategy_name, f"Legacy strategy result: {signal}")


def generate_signal_payload(df, strategy_name):
    print(f"\n[STRATEGY] Using: {strategy_name}")
    logger.info("\n[STRATEGY] Using: %s", strategy_name)

    if not has_enough_data(df, strategy_name):
        if _is_option_strategy(strategy_name):
            return _no_trade("Not enough data", strategy_name)
        return _legacy_payload("HOLD", strategy_name, "Not enough data")

    if strategy_name == "ATM_MOMENTUM":
        return strategy_momentum(df)
    if strategy_name == "ATM_ORB":
        return strategy_orb(df)
    if strategy_name == "ATM_VWAP_REVERSION":
        return strategy_vwap(df)
    if strategy_name == "ATM_MULTI":
        return strategy_multi(df)
    if strategy_name == "ATM_BREAKOUT_EXPANSION":
        return strategy_breakout_expansion(df)
    if strategy_name == "ATM_IV_EXPANSION":
        return strategy_iv_expansion(df)
    if strategy_name == "ATM_TRAP_REVERSAL":
        return strategy_trap_reversal(df)

    return _evaluate_legacy_signal(df, strategy_name)


def generate_signal(df, strategy_name):
    return generate_signal_payload(df, strategy_name)["execution_signal"]


def get_signal(df, strategy_type):
    return generate_signal_payload(df, strategy_type)


def confirm_signal(df, strategy_func):
    signals = []

    confirmation_windows = [3, 2, 1]
    for candles_to_trim in confirmation_windows:
        if candles_to_trim == 1:
            sub_df = df
        else:
            sub_df = df.iloc[:-candles_to_trim + 1]

        if len(sub_df) < 5:
            continue

        signal = strategy_func(sub_df)
        signals.append(signal)

    logger.info("Confirmation signals: %s", signals)
    print(f"Confirmation signals: {signals}")

    if len(signals) < 2:
        return "HOLD"

    if signals[-1] == signals[-2]:
        return signals[-1]

    return "HOLD"


def multi_strategy_signal(df, strategies, min_confirmations=2):
    logger.info("Multi-strategy mode: %s", strategies)
    print(f"Multi-strategy mode: {strategies}")

    signals = {}

    for strat in strategies:
        if not has_enough_data(df, strat):
            signals[strat] = "HOLD"
            continue

        signals[strat] = generate_signal(df, strat)

    logger.info("Signals: %s", signals)
    print(f"Signals: {signals}")

    buy_count = list(signals.values()).count("BUY")
    sell_count = list(signals.values()).count("SELL")

    logger.info("BUY: %s, SELL: %s", buy_count, sell_count)
    print(f"BUY: {buy_count}, SELL: {sell_count}")

    if buy_count >= min_confirmations and buy_count > sell_count:
        return "BUY"
    if sell_count >= min_confirmations and sell_count > buy_count:
        return "SELL"

    return "HOLD"
