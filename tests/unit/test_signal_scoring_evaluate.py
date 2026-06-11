import unittest
from unittest.mock import patch

import pandas as pd

from signal_scoring import evaluate_symbol_signal


class EvaluateSymbolSignalMultiStrategyTests(unittest.TestCase):
    def _df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Open": [100.0] * 5,
                "High": [101.0] * 5,
                "Low": [99.0] * 5,
                "Close": [100.0] * 5,
                "Volume": [100.0] * 5,
            }
        )

    def _payload(self, execution_signal, option_signal):
        return {
            "execution_signal": execution_signal,
            "option_signal": option_signal,
            "reason": "test",
            "strength": 1.0,
            "selected_profile": None,
            "components": None,
        }

    def test_two_buy_pe_votes_resolve_to_sell_buy_pe(self) -> None:
        payloads = {
            "ATM_TRAP_REVERSAL": self._payload("BUY", "BUY_PE"),
            "ATM_VWAP_REVERSION": self._payload("BUY", "BUY_PE"),
        }
        with patch(
            "signal_scoring.generate_signal_payload",
            side_effect=lambda data, strat: payloads[strat],
        ), patch("signal_scoring.get_strategy_score", return_value=1.0):
            result = evaluate_symbol_signal(
                self._df(),
                mode="2",
                strategies=["ATM_TRAP_REVERSAL", "ATM_VWAP_REVERSION"],
                min_confirmations=2,
            )

        self.assertEqual(result["signal"], "SELL")
        self.assertEqual(result["option_signal"], "BUY_PE")
        self.assertEqual(result["option_type"], "PE")
        self.assertEqual(result["agreement_count"], 2)

    def test_two_buy_ce_votes_resolve_to_buy_buy_ce(self) -> None:
        payloads = {
            "ATM_TRAP_REVERSAL": self._payload("BUY", "BUY_CE"),
            "ATM_VWAP_REVERSION": self._payload("BUY", "BUY_CE"),
        }
        with patch(
            "signal_scoring.generate_signal_payload",
            side_effect=lambda data, strat: payloads[strat],
        ), patch("signal_scoring.get_strategy_score", return_value=1.0):
            result = evaluate_symbol_signal(
                self._df(),
                mode="2",
                strategies=["ATM_TRAP_REVERSAL", "ATM_VWAP_REVERSION"],
                min_confirmations=2,
            )

        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["option_signal"], "BUY_CE")
        self.assertEqual(result["option_type"], "CE")
        self.assertEqual(result["agreement_count"], 2)

    def test_mixed_ce_pe_votes_do_not_force_hold(self) -> None:
        payloads = {
            "ATM_TRAP_REVERSAL": self._payload("BUY", "BUY_PE"),
            "ATM_VWAP_REVERSION": self._payload("SELL", "NO_TRADE"),
        }
        with patch(
            "signal_scoring.generate_signal_payload",
            side_effect=lambda data, strat: payloads[strat],
        ), patch("signal_scoring.get_strategy_score", return_value=1.0):
            result = evaluate_symbol_signal(
                self._df(),
                mode="2",
                strategies=["ATM_TRAP_REVERSAL", "ATM_VWAP_REVERSION"],
                min_confirmations=2,
            )

        # Both votes count toward SELL (BUY_PE -> sell_count, SELL -> sell_count)
        self.assertEqual(result["signal"], "SELL")
        self.assertEqual(result["option_signal"], "BUY_PE")
        self.assertEqual(result["agreement_count"], 2)


if __name__ == "__main__":
    unittest.main()
