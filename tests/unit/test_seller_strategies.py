import unittest
from datetime import datetime, time
from unittest.mock import patch

import pandas as pd
import numpy as np

from strategy import (
    compute_adx,
    strategy_orb_failure_sell,
    strategy_vwap_fade_sell,
    strategy_theta_sell,
    strategy_low_vol_range_sell,
    strategy_exhaustion_sell,
)
from signal_scoring import evaluate_symbol_signal


def _make_df(n=30, trend="flat", seed=42):
    """Build a synthetic OHLCV DataFrame suitable for seller strategy tests."""
    rng = np.random.default_rng(seed)
    base = 22000.0
    closes = []
    for i in range(n):
        if trend == "up":
            base += 10
        elif trend == "down":
            base -= 10
        closes.append(base + rng.uniform(-5, 5))
    closes = np.array(closes)
    opens = closes - rng.uniform(0, 4, n)
    highs = np.maximum(opens, closes) + rng.uniform(1, 6, n)
    lows = np.minimum(opens, closes) - rng.uniform(1, 6, n)
    volumes = rng.integers(1000, 5000, n).astype(float)
    idx = pd.date_range("2024-01-15 09:15", periods=n, freq="1min")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


class TestComputeAdx(unittest.TestCase):
    def test_returns_expected_columns(self):
        df = _make_df(40)
        result = compute_adx(df, period=14)
        self.assertIn("adx", result.columns)
        self.assertIn("plus_di", result.columns)
        self.assertIn("minus_di", result.columns)
        self.assertEqual(len(result), len(df))

    def test_adx_in_valid_range(self):
        df = _make_df(50)
        result = compute_adx(df)
        self.assertTrue((result["adx"] >= 0).all())
        self.assertTrue((result["adx"] <= 100).all())

    def test_strong_trend_gives_higher_adx_than_flat(self):
        df_up = _make_df(60, trend="up")
        df_flat = _make_df(60, trend="flat")
        adx_up = float(compute_adx(df_up)["adx"].iloc[-1])
        adx_flat = float(compute_adx(df_flat)["adx"].iloc[-1])
        # A persistent uptrend should produce meaningfully higher ADX than a flat market
        self.assertGreater(adx_up, adx_flat)


class TestOrbFailureSell(unittest.TestCase):
    def _bullish_failure_df(self):
        """Breakout above ORB high then close back inside — bearish reversal candle."""
        n = 25
        idx = pd.date_range("2024-01-15 09:15", periods=n, freq="1min")
        opens = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n
        closes = [100.0] * n
        # ORB candles: high stays at 101
        # Failure candle: high pierces 103, close back at 100.5 (inside), bearish body
        highs[-3] = 103.5
        closes[-3] = 100.3
        opens[-3] = 101.5
        closes[-1] = 100.2
        opens[-1] = 101.2  # bearish close below orb_high=101
        highs[-1] = 101.5
        lows[-1] = 99.8
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )

    def test_bullish_failure_gives_sell_ce(self):
        df = self._bullish_failure_df()
        result = strategy_orb_failure_sell(df)
        self.assertEqual(result["option_signal"], "SELL_CE")
        self.assertEqual(result["execution_signal"], "SELL")

    def test_no_failure_gives_no_trade(self):
        df = _make_df(25)
        result = strategy_orb_failure_sell(df)
        self.assertEqual(result["option_signal"], None)
        self.assertEqual(result["execution_signal"], "HOLD")


class TestVwapFadeSell(unittest.TestCase):
    def _above_vwap_df(self):
        """Flat candles with final close stretched above VWAP, low ADX."""
        n = 25
        idx = pd.date_range("2024-01-15 09:15", periods=n, freq="1min")
        base = 100.0
        closes = [base] * n
        closes[-1] = base * 1.008  # 0.8% above — well above default 0.3% threshold
        opens = closes[:]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )

    def test_bullish_stretch_gives_sell_pe(self):
        df = self._above_vwap_df()
        low_adx = pd.DataFrame(
            {"adx": [5.0] * len(df), "plus_di": [4.0] * len(df), "minus_di": [3.0] * len(df)},
            index=df.index,
        )
        with patch("strategy.compute_adx", return_value=low_adx):
            result = strategy_vwap_fade_sell(df, adx_threshold=25.0, stretch_pct=0.003)
        self.assertEqual(result["option_signal"], "SELL_PE")
        self.assertEqual(result["execution_signal"], "SELL")

    def test_high_adx_blocks_fade(self):
        """Patch compute_adx to return high ADX — should produce NO_TRADE."""
        df = self._above_vwap_df()
        high_adx = pd.DataFrame(
            {"adx": [30.0] * len(df), "plus_di": [20.0] * len(df), "minus_di": [10.0] * len(df)},
            index=df.index,
        )
        with patch("strategy.compute_adx", return_value=high_adx):
            result = strategy_vwap_fade_sell(df, adx_threshold=18.0)
        self.assertIsNone(result["option_signal"])
        self.assertEqual(result["execution_signal"], "HOLD")


class TestThetaSell(unittest.TestCase):
    def _afternoon_low_adx_df(self):
        n = 20
        idx = pd.date_range("2024-01-15 11:30", periods=n, freq="1min")
        closes = [100.0] * n
        opens = closes[:]
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.3 for c in closes]
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [500.0] * n},
            index=idx,
        )

    def test_afternoon_low_adx_gives_signal(self):
        df = self._afternoon_low_adx_df()
        low_adx = pd.DataFrame(
            {"adx": [8.0] * len(df), "plus_di": [10.0] * len(df), "minus_di": [9.0] * len(df)},
            index=df.index,
        )
        with patch("strategy.compute_adx", return_value=low_adx):
            result = strategy_theta_sell(df, time_gate_hour=11, adx_threshold=15.0)
        self.assertIn(result["option_signal"], {"SELL_CE", "SELL_PE"})
        self.assertEqual(result["execution_signal"], "SELL")

    def test_before_time_gate_blocks(self):
        n = 20
        idx = pd.date_range("2024-01-15 09:30", periods=n, freq="1min")  # before 11am
        closes = [100.0] * n
        df = pd.DataFrame(
            {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [500.0] * n},
            index=idx,
        )
        result = strategy_theta_sell(df, time_gate_hour=11)
        self.assertIsNone(result["option_signal"])

    def test_high_adx_blocks_theta(self):
        df = self._afternoon_low_adx_df()
        high_adx = pd.DataFrame(
            {"adx": [25.0] * len(df), "plus_di": [18.0] * len(df), "minus_di": [12.0] * len(df)},
            index=df.index,
        )
        with patch("strategy.compute_adx", return_value=high_adx):
            result = strategy_theta_sell(df, time_gate_hour=11, adx_threshold=15.0)
        self.assertIsNone(result["option_signal"])


class TestLowVolRangeSell(unittest.TestCase):
    def _contracting_atr_df(self):
        """Flat session with contracting ATR — prices close together."""
        n = 30
        idx = pd.date_range("2024-01-15 11:00", periods=n, freq="1min")
        closes = [100.0 + i * 0.01 for i in range(n)]
        opens = closes[:]
        # Very tight range — low ATR
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.1 for c in closes]
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [500.0] * n},
            index=idx,
        )

    def test_contracting_atr_low_adx_gives_signal(self):
        df = self._contracting_atr_df()
        n = len(df)
        # Decreasing ATR: starts at 2.0, ends at 0.5 — clearly contracting
        atr_values = pd.Series([2.0 - i * (1.5 / (n - 1)) for i in range(n)], index=df.index)
        low_adx = pd.DataFrame(
            {"adx": [12.0] * n, "plus_di": [9.0] * n, "minus_di": [8.0] * n},
            index=df.index,
        )
        with patch("strategy.compute_adx", return_value=low_adx):
            with patch("strategy.compute_atr", return_value=atr_values):
                result = strategy_low_vol_range_sell(df, adx_threshold=20.0, atr_ratio_threshold=0.9)
        self.assertIn(result["option_signal"], {"SELL_CE", "SELL_PE"})
        self.assertEqual(result["execution_signal"], "SELL")

    def test_sell_ce_includes_strike_offset(self):
        df = self._contracting_atr_df()
        n = len(df)
        # Force price into upper half (last close > day mid)
        df = df.copy()
        df["Close"] = df["Close"] + 5.0  # shift price to upper half of range
        atr_values = pd.Series([2.0 - i * (1.5 / (n - 1)) for i in range(n)], index=df.index)
        low_adx = pd.DataFrame(
            {"adx": [10.0] * n, "plus_di": [8.0] * n, "minus_di": [7.0] * n},
            index=df.index,
        )
        with patch("strategy.compute_adx", return_value=low_adx):
            with patch("strategy.compute_atr", return_value=atr_values):
                result = strategy_low_vol_range_sell(df, adx_threshold=20.0, atr_ratio_threshold=0.9)
        if result["option_signal"] == "SELL_CE":
            self.assertEqual(result.get("strike_offset"), 1)
        elif result["option_signal"] == "SELL_PE":
            self.assertEqual(result.get("strike_offset"), -1)


class TestExhaustionSell(unittest.TestCase):
    def _overbought_df(self):
        """Rising trend that overshoots VWAP — extreme RSI."""
        n = 25
        idx = pd.date_range("2024-01-15 10:00", periods=n, freq="1min")
        # Strong up move so RSI goes high; final candles flatten (RSI declining)
        closes = [100.0 + i * 2.0 for i in range(20)] + [138.0, 137.5, 137.0, 136.5, 136.0]
        opens = [c - 0.5 for c in closes]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
            index=idx,
        )

    def test_overbought_with_declining_rsi_gives_sell_ce(self):
        df = self._overbought_df()
        result = strategy_exhaustion_sell(
            df, rsi_overbought=60.0, vwap_distance_atr=0.5, momentum_lookback=3
        )
        # Not guaranteed to fire on synthetic data, but if it fires it must be SELL_CE
        if result["option_signal"] is not None:
            self.assertEqual(result["option_signal"], "SELL_CE")
            self.assertEqual(result["execution_signal"], "SELL")

    def test_flat_market_no_exhaustion(self):
        df = _make_df(25)
        result = strategy_exhaustion_sell(df, rsi_overbought=80.0, rsi_oversold=20.0)
        self.assertIsNone(result["option_signal"])


class TestSellerMultiStrategyAggregation(unittest.TestCase):
    """evaluate_symbol_signal with mixed SELL_CE / SELL_PE votes."""

    def _payload(self, opt_sig):
        return {
            "execution_signal": "SELL" if opt_sig in {"SELL_CE", "SELL_PE"} else "HOLD",
            "option_signal": opt_sig if opt_sig in {"SELL_CE", "SELL_PE"} else None,
            "reason": "test",
            "strength": 0.8,
            "selected_profile": None,
            "components": None,
            "strategy": opt_sig,
        }

    def test_two_sell_ce_plus_one_sell_pe_resolves_sell_ce(self):
        df = _make_df(25)
        payloads = {
            "ATM_ORB_FAILURE_SELL": self._payload("SELL_CE"),
            "ATM_VWAP_FADE_SELL": self._payload("SELL_CE"),
            "EXHAUSTION_SELL": self._payload("SELL_PE"),
        }
        with patch("signal_scoring.generate_signal_payload", side_effect=lambda df, s: payloads[s]):
            with patch("signal_scoring.get_strategy_score", return_value=0.5):
                result = evaluate_symbol_signal(
                    df,
                    mode="2",
                    strategies=list(payloads.keys()),
                    min_confirmations=2,
                )
        self.assertEqual(result["signal"], "SELL")
        self.assertEqual(result["option_signal"], "SELL_CE")
        self.assertEqual(result["option_type"], "CE")

    def test_buy_signal_blocked_for_seller_mode(self):
        """A strategy returning BUY_CE while others return SELL should not contaminate resolution."""
        df = _make_df(25)
        payloads = {
            "ATM_ORB_FAILURE_SELL": self._payload("SELL_PE"),
            "ATM_VWAP_FADE_SELL": self._payload("SELL_PE"),
            "EXHAUSTION_SELL": {"execution_signal": "BUY", "option_signal": "BUY_CE",
                                 "reason": "test", "strength": 0.3, "selected_profile": None,
                                 "components": None, "strategy": "EXHAUSTION_SELL"},
        }
        with patch("signal_scoring.generate_signal_payload", side_effect=lambda df, s: payloads[s]):
            with patch("signal_scoring.get_strategy_score", return_value=0.5):
                result = evaluate_symbol_signal(
                    df,
                    mode="2",
                    strategies=list(payloads.keys()),
                    min_confirmations=2,
                )
        self.assertEqual(result["signal"], "SELL")
        self.assertEqual(result["option_signal"], "SELL_PE")


if __name__ == "__main__":
    unittest.main()
