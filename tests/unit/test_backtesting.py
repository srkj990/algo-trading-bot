from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from backtesting import BacktestConfig, BacktestEngine


class BacktestWorkflowTests(unittest.TestCase):
    def test_intraday_options_engine_uses_legacy_raw_entry_mode_when_configured(self) -> None:
        config = BacktestConfig(
            engine_name="intraday_options",
            capital=100000.0,
            period="1d",
            interval="1m",
            strategy_mode="SINGLE",
            strategy_name="ATM_BREAKOUT_EXPANSION",
            strategies=("ATM_BREAKOUT_EXPANSION",),
            min_confirmations=1,
            risk_percent=0.01,
            atr_stop_multiplier=2.0,
            trailing_atr_multiplier=1.25,
            target_risk_reward=2.0,
            risk_style_name="BALANCED",
            top_n=1,
            max_positions=1,
            max_capital_per_trade=100000.0,
            max_capital_deployed=100000.0,
            universe=("NSE:NIFTY 50",),
            intraday_options_entry_mode="LEGACY_IMMEDIATE",
            option_backtest_settings={
                "base_symbol": "NIFTY",
                "expiry": "2026-05-19",
                "structure_mode": "SINGLE",
                "strike_mode": "ATM",
                "underlying_symbol": "NSE:NIFTY 50",
                "signal_symbols": ("NSE:NIFTY 50",),
            },
        )
        engine = BacktestEngine(config)
        self.assertEqual(engine.engine_helper.momentum_entry_mode, "LEGACY_RAW")

    def test_process_timestamp_uses_live_scan_workflow(self) -> None:
        config = BacktestConfig(
            engine_name="intraday_options",
            capital=100000.0,
            period="1d",
            interval="1m",
            strategy_mode="SINGLE",
            strategy_name="ATM_BREAKOUT_EXPANSION",
            strategies=("ATM_BREAKOUT_EXPANSION",),
            min_confirmations=1,
            risk_percent=0.01,
            atr_stop_multiplier=2.0,
            trailing_atr_multiplier=1.25,
            target_risk_reward=2.0,
            risk_style_name="BALANCED",
            top_n=1,
            max_positions=1,
            max_capital_per_trade=100000.0,
            max_capital_deployed=100000.0,
            universe=("NSE:NIFTY 50",),
            option_backtest_settings={
                "base_symbol": "NIFTY",
                "expiry": "2026-05-19",
                "structure_mode": "SINGLE",
                "strike_mode": "ATM",
                "underlying_symbol": "NSE:NIFTY 50",
                "signal_symbols": ("NSE:NIFTY 50",),
            },
        )
        engine = BacktestEngine(config)
        history = {
            "NSE:NIFTY 50": pd.DataFrame(
                [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 0}],
                index=[pd.Timestamp("2026-05-18 10:00:00+05:30")],
            )
        }
        ranked_candidates = [{"symbol": "NFO:TESTCE", "signal": "BUY", "agreement_count": 1, "score": 0.8}]
        scan_result = SimpleNamespace(ranked_candidates=ranked_candidates, symbol_snapshots={})

        with patch("backtesting.scan_symbols", return_value=scan_result) as mock_scan, \
             patch.object(engine, "_enter_ranked_candidates") as mock_enter, \
             patch.object(engine, "_mark_equity"):
            engine._process_timestamp(history, pd.Timestamp("2026-05-18 10:00:00+05:30"))

        mock_scan.assert_called_once()
        mock_enter.assert_called_once_with(ranked_candidates, pd.Timestamp("2026-05-18 10:00:00+05:30"))

    def test_enter_ranked_candidates_uses_live_trade_gate(self) -> None:
        config = BacktestConfig(
            engine_name="intraday_equity",
            capital=100000.0,
            period="5d",
            interval="5m",
            strategy_mode="SINGLE",
            strategy_name="MA",
            strategies=("MA",),
            min_confirmations=1,
            risk_percent=0.01,
            atr_stop_multiplier=2.0,
            trailing_atr_multiplier=1.25,
            target_risk_reward=2.0,
            risk_style_name="BALANCED",
            top_n=1,
            max_positions=1,
            max_capital_per_trade=100000.0,
            max_capital_deployed=100000.0,
            universe=("SBIN.NS",),
        )
        engine = BacktestEngine(config)
        candidate = {
            "symbol": "SBIN.NS",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.7,
            "latest_close": 100.0,
            "atr": 2.0,
            "strategy": "MA",
            "reason": "test",
        }

        with patch("backtesting.should_enter_trade", return_value=False) as mock_gate:
            engine._enter_ranked_candidates([candidate], pd.Timestamp("2026-05-18 10:00:00+05:30"))

        mock_gate.assert_called_once()
        self.assertFalse(engine.positions)
        self.assertEqual(engine.trades, [])

    def test_enter_ranked_candidates_skips_underpriced_intraday_option_contracts(self) -> None:
        config = BacktestConfig(
            engine_name="intraday_options",
            capital=100000.0,
            period="1d",
            interval="1m",
            strategy_mode="SINGLE",
            strategy_name="ATM_BREAKOUT_EXPANSION",
            strategies=("ATM_BREAKOUT_EXPANSION",),
            min_confirmations=1,
            risk_percent=0.01,
            atr_stop_multiplier=2.0,
            trailing_atr_multiplier=1.25,
            target_risk_reward=2.0,
            risk_style_name="BALANCED",
            top_n=1,
            max_positions=1,
            max_capital_per_trade=100000.0,
            max_capital_deployed=100000.0,
            universe=("BSE:SENSEX",),
            option_backtest_settings={
                "base_symbol": "SENSEX",
                "expiry": "2026-05-21",
                "structure_mode": "SINGLE",
                "strike_mode": "ATM",
                "underlying_symbol": "BSE:SENSEX",
                "signal_symbols": ("BSE:SENSEX",),
            },
        )
        engine = BacktestEngine(config)
        candidate = {
            "symbol": "BFO:SENSEX2652175100PE",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.7,
            "latest_close": 0.05,
            "atr": 20.0,
            "strategy": "ATM_BREAKOUT_EXPANSION",
            "reason": "test",
            "analytics": {"option_price": 0.05},
        }

        with patch("backtesting.get_contract_lot_size", return_value=20), \
             patch("backtesting.should_enter_trade", return_value=True) as mock_gate:
            engine._enter_ranked_candidates([candidate], pd.Timestamp("2026-05-21 10:00:00+05:30"))

        mock_gate.assert_not_called()
        self.assertFalse(engine.positions)
        self.assertEqual(engine.trades, [])


if __name__ == "__main__":
    unittest.main()
