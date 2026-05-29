from __future__ import annotations

import unittest

import pandas as pd

from indicators import compute_vwap


class IndicatorTests(unittest.TestCase):
    def test_compute_vwap_falls_back_when_volume_is_zero(self) -> None:
        df = pd.DataFrame(
            {
                "High": [102.0, 104.0],
                "Low": [98.0, 100.0],
                "Close": [101.0, 103.0],
                "Volume": [0, 0],
            }
        )

        vwap = compute_vwap(df)

        self.assertFalse(vwap.isna().any())
        self.assertAlmostEqual(float(vwap.iloc[0]), (102.0 + 98.0 + 101.0) / 3)


if __name__ == "__main__":
    unittest.main()
