from config import MIN_CANDLES
from indicators import compute_atr, compute_rsi, compute_vwap
from strategy import generate_signal


def get_strategy_score(strategy_name, df, signal):
    if signal not in {"BUY", "SELL"} or df.empty:
        return 0.0

    latest = df.iloc[-1]
    close = float(latest["Close"])
    atr_series = compute_atr(df)
    atr_value = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
    normalized_atr = (atr_value / close) if close > 0 and atr_value == atr_value else 0.0
    score = 0.0

    if strategy_name == "MA" and len(df) >= MIN_CANDLES["MA"]:
        ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
        ma50 = float(df["Close"].rolling(50).mean().iloc[-1])
        if ma20 == ma20 and ma50 == ma50 and close > 0:
            score = abs(ma20 - ma50) / close

    elif strategy_name == "RSI" and len(df) >= MIN_CANDLES["RSI"]:
        rsi = float(compute_rsi(df["Close"]).iloc[-1])
        if rsi == rsi:
            score = max(0.0, (30 - rsi) / 100) if signal == "BUY" else max(0.0, (rsi - 70) / 100)

    elif strategy_name == "BREAKOUT" and len(df) >= MIN_CANDLES["BREAKOUT"]:
        prev_high = float(df["High"].rolling(20).max().iloc[-2])
        prev_low = float(df["Low"].rolling(20).min().iloc[-2])
        if close > 0:
            score = max(0.0, (close - prev_high) / close) if signal == "BUY" else max(0.0, (prev_low - close) / close)

    elif strategy_name == "VWAP":
        vwap = float(compute_vwap(df).iloc[-1])
        if vwap == vwap and close > 0:
            score = abs(close - vwap) / close

    elif strategy_name == "ORB" and len(df) >= MIN_CANDLES["ORB"]:
        first_15 = df.iloc[:15]
        orb_high = float(first_15["High"].max())
        orb_low = float(first_15["Low"].min())
        if close > 0:
            score = max(0.0, (close - orb_high) / close) if signal == "BUY" else max(0.0, (orb_low - close) / close)

    return score + (normalized_atr * 0.25)


def get_atr_value(df, period=14):
    atr = compute_atr(df, period=period)
    if atr.empty:
        return 0.0
    value = float(atr.iloc[-1])
    return 0.0 if value != value else value


def evaluate_symbol_signal(
    data,
    mode,
    strategy_name=None,
    strategies=None,
    min_confirmations=None,
):
    if mode == "1":
        signal = generate_signal(data, strategy_name)
        score = get_strategy_score(strategy_name, data, signal)
        return {
            "signal": signal,
            "agreement_count": 1 if signal in {"BUY", "SELL"} else 0,
            "score": score,
            "details": {strategy_name: {"signal": signal, "score": score}},
        }

    details = {}
    buy_count = 0
    sell_count = 0

    for strat in strategies:
        strat_signal = generate_signal(data, strat)
        strat_score = get_strategy_score(strat, data, strat_signal)
        details[strat] = {
            "signal": strat_signal,
            "score": strat_score,
        }
        if strat_signal == "BUY":
            buy_count += 1
        elif strat_signal == "SELL":
            sell_count += 1

    if buy_count >= min_confirmations and buy_count > sell_count:
        final_signal = "BUY"
        agreement_count = buy_count
    elif sell_count >= min_confirmations and sell_count > buy_count:
        final_signal = "SELL"
        agreement_count = sell_count
    else:
        final_signal = "HOLD"
        agreement_count = max(buy_count, sell_count)

    score = sum(
        item["score"]
        for item in details.values()
        if item["signal"] == final_signal
    )

    return {
        "signal": final_signal,
        "agreement_count": agreement_count,
        "score": score,
        "details": details,
    }


def rank_candidates(candidates):
    return sorted(
        candidates,
        key=lambda item: (
            item["agreement_count"],
            item["score"],
            item.get("atr", 0.0),
            item["symbol"],
        ),
        reverse=True,
    )
