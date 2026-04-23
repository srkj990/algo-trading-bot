from config import MIN_CANDLES
from config import MIN_RANKED_CANDIDATE_SCORE
from indicators import compute_atr, compute_rsi, compute_vwap
from strategy import generate_signal_payload


def get_strategy_score(strategy_name, df, signal_payload):
    signal = signal_payload["execution_signal"]
    if signal not in {"BUY", "SELL"} or df.empty:
        return 0.0

    latest = df.iloc[-1]
    close = float(latest["Close"])
    atr_series = compute_atr(df)
    atr_value = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
    normalized_atr = (atr_value / close) if close > 0 and atr_value == atr_value else 0.0
    score = 0.0

    if strategy_name.startswith("ATM_"):
        return float(signal_payload.get("strength", 0.0)) + (normalized_atr * 0.1)

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
        signal_payload = generate_signal_payload(data, strategy_name)
        signal = signal_payload["execution_signal"]
        score = get_strategy_score(strategy_name, data, signal_payload)
        return {
            "signal": signal,
            "agreement_count": 1 if signal in {"BUY", "SELL"} else 0,
            "score": score,
            "strategy": signal_payload.get("strategy", strategy_name),
            "details": {
                strategy_name: {
                    "signal": signal,
                    "score": score,
                    "reason": signal_payload.get("reason"),
                    "option_signal": signal_payload.get("option_signal"),
                    "strength": signal_payload.get("strength", 0.0),
                }
            },
            "reason": signal_payload.get("reason"),
            "option_signal": signal_payload.get("option_signal"),
            "option_type": signal_payload.get("option_type"),
            "strength": signal_payload.get("strength", 0.0),
        }

    details = {}
    buy_count = 0
    sell_count = 0

    for strat in strategies:
        signal_payload = generate_signal_payload(data, strat)
        strat_signal = signal_payload["execution_signal"]
        strat_score = get_strategy_score(strat, data, signal_payload)
        details[strat] = {
            "signal": strat_signal,
            "score": strat_score,
            "reason": signal_payload.get("reason"),
            "option_signal": signal_payload.get("option_signal"),
            "strength": signal_payload.get("strength", 0.0),
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
        "strategy": None,
        "details": details,
        "reason": None,
        "option_signal": None,
        "option_type": None,
        "strength": 0.0,
    }


def rank_candidates(candidates, min_score=None):
    """
    Rank candidates by agreement_count, score, ATR, symbol (descending).

    Also applies a minimum-score filter (default via config) to prevent
    weak 1-minute signals from being tradable just because they are "top" by rank.
    """

    threshold = MIN_RANKED_CANDIDATE_SCORE if min_score is None else float(min_score)
    filtered = candidates
    if threshold and threshold > 0:
        filtered = [
            item
            for item in candidates
            if float(item.get("score") or 0.0) >= threshold
        ]

    return sorted(
        filtered,
        key=lambda item: (
            item["agreement_count"],
            item["score"],
            item.get("atr", 0.0),
            item["symbol"],
        ),
        reverse=True,
    )
