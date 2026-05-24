import unittest

import pandas as pd

from signal_scoring import get_strategy_score
from strategy import breakout_strategy, get_breakout_reference_levels


class BreakoutStrategyTests(unittest.TestCase):
    def test_breakout_reference_levels_use_last_completed_lookback_window(self) -> None:
        df = pd.DataFrame(
            {
                "High": list(range(101, 123)),
                "Low": list(range(81, 103)),
                "Close": list(range(91, 113)),
            }
        )

        prev_high, prev_low = get_breakout_reference_levels(df, lookback=20)

        self.assertEqual(prev_high, 121.0)
        self.assertEqual(prev_low, 82.0)

    def test_breakout_strategy_returns_hold_when_insufficient_candles(self) -> None:
        df = pd.DataFrame(
            {
                "High": [101.0] * 20,
                "Low": [99.0] * 20,
                "Close": [100.0] * 20,
            }
        )

        self.assertEqual(breakout_strategy(df), "HOLD")

    def test_breakout_score_uses_last_completed_window_levels(self) -> None:
        df = pd.DataFrame(
            {
                "High": [100.0 + index for index in range(21)] + [130.0],
                "Low": [90.0 + index for index in range(21)] + [95.0],
                "Close": [95.0 + index for index in range(21)] + [129.0],
            }
        )

        score = get_strategy_score(
            "BREAKOUT",
            df,
            {"execution_signal": "BUY"},
        )

        self.assertGreater(score, 0.0)


if __name__ == "__main__":
    unittest.main()
