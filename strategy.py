import pandas as pd
from indicators import compute_rsi, compute_vwap

from config import MIN_CANDLES
from logger import get_logger

logger = get_logger()

def has_enough_data(df, strategy_name):
    required = MIN_CANDLES.get(strategy_name, 1)
    available = len(df)

    logger.info(f"{strategy_name} → Required: {required}, Available: {available}")
    print(f"{strategy_name} → Required: {required}, Available: {available}")

    if available < required:
        logger.warning(f"Not enough data for {strategy_name}")
        return False

    return True

# 🔵 Moving Average
def ma_strategy(df):
    df['ma20'] = df['Close'].rolling(20).mean()
    df['ma50'] = df['Close'].rolling(50).mean()

    latest = df.iloc[-1]

    ma20 = float(latest['ma20'])
    ma50 = float(latest['ma50'])

    if pd.isna(ma20) or pd.isna(ma50):
        return "HOLD"

    if ma20 > ma50:
        return "BUY"
    elif ma20 < ma50:
        return "SELL"
    return "HOLD"


# 🟢 RSI Strategy
def rsi_strategy(df):
    df['rsi'] = compute_rsi(df['Close'])

    latest = df.iloc[-1]
    rsi = float(latest['rsi'])

    if pd.isna(rsi):
        return "HOLD"

    if rsi < 30:
        return "BUY"
    elif rsi > 70:
        return "SELL"
    return "HOLD"


# 🟡 Breakout Strategy
def breakout_strategy(df):
    latest = df.iloc[-1]

    prev_high = df['High'].rolling(20).max().iloc[-2]
    prev_low = df['Low'].rolling(20).min().iloc[-2]

    close = float(latest['Close'])

    if close > prev_high:
        return "BUY"
    elif close < prev_low:
        return "SELL"
    return "HOLD"


# 🟣 VWAP Strategy
def vwap_strategy(df):
    df['vwap'] = compute_vwap(df)

    latest = df.iloc[-1]

    close = float(latest['Close'])
    vwap = float(latest['vwap'])

    if pd.isna(vwap):
        return "HOLD"

    if close > vwap:
        return "BUY"
    elif close < vwap:
        return "SELL"
    return "HOLD"


# 🟠 ORB Strategy (Opening Range Breakout)
def orb_strategy(df):
    if len(df) < 20:
        return "HOLD"

    first_15 = df.iloc[:15]

    high = first_15['High'].max()
    low = first_15['Low'].min()

    latest = df.iloc[-1]
    close = float(latest['Close'])

    if close > high:
        return "BUY"
    elif close < low:
        return "SELL"
    return "HOLD"


def generate_signal(df, strategy_name):
    print(f"\n[STRATEGY] Using: {strategy_name}")
    logger.info(f"\n[STRATEGY] Using: {strategy_name}")

    if not has_enough_data(df, strategy_name):
        return "HOLD"

    if strategy_name == "MA":
        return confirm_signal(df, ma_strategy)

    elif strategy_name == "RSI":
        return confirm_signal(df, rsi_strategy)

    elif strategy_name == "BREAKOUT":
        return confirm_signal(df, breakout_strategy)

    elif strategy_name == "VWAP":
        return confirm_signal(df, vwap_strategy)

    elif strategy_name == "ORB":
        return confirm_signal(df, orb_strategy)

    print("[STRATEGY] Invalid strategy")
    logger.info("[STRATEGY] Invalid strategy")
    return "HOLD"

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

    logger.info(f"Confirmation signals: {signals}")
    print(f"Confirmation signals: {signals}")

    if len(signals) < 2:
        return "HOLD"

    if signals[-1] == signals[-2]:
        return signals[-1]

    return "HOLD"

def multi_strategy_signal(df, strategies, min_confirmations=2):
    logger.info(f"Multi-strategy mode: {strategies}")
    print(f"Multi-strategy mode: {strategies}")

    signals = {}

    for strat in strategies:
        if not has_enough_data(df, strat):
            signals[strat] = "HOLD"
            continue

        signals[strat] = generate_signal(df, strat)

    logger.info(f"Signals: {signals}")
    print(f"Signals: {signals}")

    buy_count = list(signals.values()).count("BUY")
    sell_count = list(signals.values()).count("SELL")

    logger.info(f"BUY: {buy_count}, SELL: {sell_count}")
    print(f"BUY: {buy_count}, SELL: {sell_count}")

    if buy_count >= min_confirmations and buy_count > sell_count:
        return "BUY"

    elif sell_count >= min_confirmations and sell_count > buy_count:
        return "SELL"

    return "HOLD"
