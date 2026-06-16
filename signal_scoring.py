from config import MIN_CANDLES
from config import MIN_RANKED_CANDIDATE_SCORE
from indicators import compute_atr, compute_rsi, compute_vwap
from strategy import generate_signal_payload, get_breakout_reference_levels


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

    if strategy_name.startswith("ATM_") or strategy_name in {
        "SHORT_THETA_AFTER_11AM",
        "LOW_VOLATILITY_RANGE_SELL",
        "EXHAUSTION_SELL",
    }:
        return float(signal_payload.get("strength", 0.0)) + (normalized_atr * 0.1)

    if strategy_name == "MA" and len(df) >= MIN_CANDLES["MA"]:
        ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
        ma50 = float(df["Close"].rolling(50).mean().iloc[-1])
        if ma20 == ma20 and ma50 == ma50 and close > 0:
            score = abs(ma20 - ma50) / close

    elif strategy_name == "MA_LONG" and len(df) >= MIN_CANDLES["MA_LONG"]:
        ma50  = float(df["Close"].rolling(50).mean().iloc[-1])
        ma200 = float(df["Close"].rolling(200).mean().iloc[-1])
        if ma50 == ma50 and ma200 == ma200 and close > 0:
            score = abs(ma50 - ma200) / close

    elif strategy_name == "RSI" and len(df) >= MIN_CANDLES["RSI"]:
        rsi = float(compute_rsi(df["Close"]).iloc[-1])
        if rsi == rsi:
            score = max(0.0, (30 - rsi) / 100) if signal == "BUY" else max(0.0, (rsi - 70) / 100)

    elif strategy_name == "BREAKOUT" and len(df) >= MIN_CANDLES["BREAKOUT"]:
        prev_high, prev_low = get_breakout_reference_levels(df, lookback=20)
        if prev_high is not None and prev_low is not None and close > 0:
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
        opt_sig = signal_payload.get("option_signal") or ""
        score = get_strategy_score(strategy_name, data, signal_payload)
        _is_active = signal in {"BUY", "SELL"} or opt_sig in {"BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE"}
        return {
            "signal": signal,
            "agreement_count": 1 if _is_active else 0,
            "score": score,
            "strategy": signal_payload.get("strategy", strategy_name),
            "details": {
                strategy_name: {
                    "signal": signal,
                    "score": score,
                    "reason": signal_payload.get("reason"),
                    "option_signal": signal_payload.get("option_signal"),
                    "strength": signal_payload.get("strength", 0.0),
                    "selected_profile": signal_payload.get("selected_profile"),
                    "components": signal_payload.get("components"),
                }
            },
            "reason": signal_payload.get("reason"),
            "option_signal": signal_payload.get("option_signal"),
            "option_type": signal_payload.get("option_type"),
            "strength": signal_payload.get("strength", 0.0),
            "selected_profile": signal_payload.get("selected_profile"),
            "components": signal_payload.get("components"),
        }

    details = {}
    buy_count = 0
    sell_count = 0
    # For ATM option strategies that return BUY_CE / BUY_PE instead of BUY / SELL,
    # track CE vs PE votes separately so majority option type is surfaced.
    ce_count = 0
    pe_count = 0

    for strat in strategies:
        signal_payload = generate_signal_payload(data, strat)
        strat_signal = signal_payload["execution_signal"]
        strat_score = get_strategy_score(strat, data, signal_payload)
        opt_sig = signal_payload.get("option_signal") or ""
        details[strat] = {
            "signal": strat_signal,
            "score": strat_score,
            "reason": signal_payload.get("reason"),
            "option_signal": opt_sig,
            "strength": signal_payload.get("strength", 0.0),
            "selected_profile": signal_payload.get("selected_profile"),
            "components": signal_payload.get("components"),
        }
        if opt_sig == "BUY_CE":
            buy_count += 1
            ce_count += 1
        elif opt_sig == "BUY_PE":
            sell_count += 1
            pe_count += 1
        elif opt_sig == "SELL_CE":
            sell_count += 1
            ce_count += 1
        elif opt_sig == "SELL_PE":
            sell_count += 1
            pe_count += 1
        elif strat_signal == "BUY":
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
        or (final_signal == "BUY" and item.get("option_signal") == "BUY_CE")
        or (final_signal == "SELL" and item.get("option_signal") in {"BUY_PE", "SELL_CE", "SELL_PE"})
    )

    # Resolve option_signal: majority vote among agreeing strategies.
    # BUY → CE if more CE votes, BUY → PE if more PE votes (rare but possible with mixed strategies).
    # SELL → PE (directional short via put).
    resolved_option_signal = None
    resolved_option_type = None

    # Detect whether agreeing strategies are seller-type (SELL_CE / SELL_PE)
    sell_ce_votes = sum(
        1 for v in details.values() if v.get("option_signal") == "SELL_CE"
    )
    sell_pe_votes = sum(
        1 for v in details.values() if v.get("option_signal") == "SELL_PE"
    )
    is_seller_mode = sell_ce_votes > 0 or sell_pe_votes > 0

    if final_signal == "BUY":
        if ce_count >= pe_count and ce_count > 0:
            resolved_option_signal = "BUY_CE"
            resolved_option_type = "CE"
        elif pe_count > ce_count:
            resolved_option_signal = "BUY_PE"
            resolved_option_type = "PE"
    elif final_signal == "SELL":
        if is_seller_mode:
            # Seller strategies: majority CE/PE vote determines which contract to write
            if ce_count >= pe_count and ce_count > 0:
                resolved_option_signal = "SELL_CE"
                resolved_option_type = "CE"
            elif pe_count > ce_count:
                resolved_option_signal = "SELL_PE"
                resolved_option_type = "PE"
        else:
            # Legacy buyer-engine SELL direction → buy PE (short put via buyer engine)
            if pe_count >= ce_count and pe_count > 0:
                resolved_option_signal = "BUY_PE"
                resolved_option_type = "PE"
            elif ce_count > pe_count:
                resolved_option_signal = "BUY_CE"
                resolved_option_type = "CE"

    # Pick best-scoring agreeing strategy's metadata for selected_profile / components
    best_detail = max(
        (v for v in details.values() if v["signal"] == final_signal
         or (final_signal == "BUY" and v.get("option_signal") == "BUY_CE")
         or (final_signal == "SELL" and v.get("option_signal") in {"BUY_PE", "SELL_CE", "SELL_PE"})),
        key=lambda v: v["score"],
        default={},
    )

    return {
        "signal": final_signal,
        "agreement_count": agreement_count,
        "score": score,
        "strategy": None,
        "details": details,
        "reason": best_detail.get("reason"),
        "option_signal": resolved_option_signal,
        "option_type": resolved_option_type,
        "strength": best_detail.get("strength", 0.0),
        "selected_profile": best_detail.get("selected_profile"),
        "components": best_detail.get("components"),
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
