from __future__ import annotations

import unittest

import pandas as pd

from strategy import _get_session_df, confirm_signal


class StrategyHelperTests(unittest.TestCase):
    def test_get_session_df_filters_by_ist_trade_day_for_utc_data(self) -> None:
        df = pd.DataFrame(
            [
                {"Close": 100.0},
                {"Close": 101.0},
                {"Close": 102.0},
            ],
            index=[
                pd.Timestamp("2026-05-22 18:00:00+00:00"),
                pd.Timestamp("2026-05-23 03:45:00+00:00"),
                pd.Timestamp("2026-05-23 09:30:00+00:00"),
            ],
        )
        session_df = _get_session_df(df)
        self.assertEqual(len(session_df), 2)
        self.assertTrue(all(ts.date().isoformat() == "2026-05-23" for ts in session_df.index))

    def test_confirm_signal_compares_previous_and_current_windows(self) -> None:
        df = pd.DataFrame({"Close": [1, 2, 3, 4, 5, 6]})

        def strategy_func(window):
            return "BUY" if len(window) >= 5 else "HOLD"

        self.assertEqual(confirm_signal(df, strategy_func), "BUY")

    def test_confirm_signal_returns_hold_when_previous_and_current_disagree(self) -> None:
        df = pd.DataFrame({"Close": [1, 2, 3, 4, 5, 6]})

        def strategy_func(window):
            return "BUY" if len(window) == 6 else "SELL"

        self.assertEqual(confirm_signal(df, strategy_func), "HOLD")


if __name__ == "__main__":
    unittest.main()
