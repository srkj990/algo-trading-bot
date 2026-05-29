import pandas as pd


def compute_rsi(series, period=14):
    delta = series.diff()

    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()

    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def compute_vwap(df):
   volume = df["Volume"].fillna(0)
   price = (
        (df["High"] + df["Low"] + df["Close"]) / 3
        if {"High", "Low", "Close"}.issubset(df.columns)
        else df["Close"]
    )
   cumulative_volume = volume.cumsum()
   vwap = (price * volume).cumsum() / cumulative_volume.replace(0, pd.NA)
   fallback = price.expanding(min_periods=1).mean()
   return vwap.fillna(fallback)


def compute_atr(df, period=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.rolling(period).mean()
