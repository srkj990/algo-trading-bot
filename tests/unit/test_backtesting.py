from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from backtesting import BacktestConfig, BacktestEngine


class BacktestWorkflowTests(unittest.TestCase):
    def test_intraday_options_capital_based_backtest_uses_capital_sized_lot_quantity(self) -> None:
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
            intraday_options_lot_mode="CAPITAL_BASED",
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
        candidate = {
            "symbol": "NFO:TESTCE",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.8,
            "latest_close": 100.0,
            "atr": 50.0,
            "strategy": "ATM_BREAKOUT_EXPANSION",
            "reason": "test",
            "analytics": {"underlying_price": 24000.0, "option_type": "CE"},
        }

        with patch("backtesting.get_contract_lot_size", return_value=75), \
             patch("engines.intraday_options.get_contract_lot_size", return_value=75), \
             patch("engines.options_equity.get_contract_lot_size", return_value=75), \
             patch("backtesting.should_enter_trade", return_value=True), \
             patch("backtesting.calculate_cost_aware_targets", return_value={
                 "asset_class": "INTRADAY_OPTIONS",
                 "risk_profile": "BALANCED",
                 "stop_loss": 90.0,
                 "target": 120.0,
                 "trailing_stop": 92.0,
                 "min_breakeven_price": 101.0,
                 "expected_costs": 25.0,
                 "expected_net_profit": 100.0,
                 "cost_to_profit_ratio": 0.2,
                 "multi_level_targets": [],
             }):
            engine._enter_ranked_candidates([candidate], pd.Timestamp("2026-05-18 10:00:00+05:30"))

        self.assertEqual(engine.trades[0]["quantity"], 150)

    def test_intraday_options_backtest_keeps_trend_adaptive_exit_levels(self) -> None:
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
        candidate = {
            "symbol": "NFO:TESTCE",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.8,
            "latest_close": 100.0,
            "atr": 5.0,
            "strategy": "ATM_BREAKOUT_EXPANSION",
            "reason": "test",
            "analytics": {"underlying_price": 24000.0, "option_type": "CE"},
        }
        trend_position = {
            "symbol": "NFO:TESTCE",
            "side": "BUY",
            "quantity": 75,
            "entry_price": 100.0,
            "stop_loss": 88.0,
            "target": 135.0,
            "trailing_stop": 90.0,
            "best_price": 100.0,
            "trailing_distance": 6.0,
            "trailing_activation_distance": 8.0,
            "trailing_active": False,
        }

        with patch("backtesting.get_contract_lot_size", return_value=75), \
             patch("engines.intraday_options.get_contract_lot_size", return_value=75), \
             patch("engines.options_equity.get_contract_lot_size", return_value=75), \
             patch("backtesting.should_enter_trade", return_value=True), \
             patch("backtesting.calculate_cost_aware_targets", return_value={
                 "asset_class": "INTRADAY_OPTIONS",
                 "risk_profile": "BALANCED",
                 "stop_loss": 95.0,
                 "target": 110.0,
                 "trailing_stop": 97.0,
                 "min_breakeven_price": 101.0,
                 "expected_costs": 25.0,
                 "expected_net_profit": 100.0,
                 "cost_to_profit_ratio": 0.2,
                 "multi_level_targets": [108.0, 112.0, 118.0],
             }), \
             patch.object(engine.engine_helper, "build_trend_adaptive_position", return_value=dict(trend_position)):
            engine._enter_ranked_candidates([candidate], pd.Timestamp("2026-05-18 10:00:00+05:30"))

        position = engine.positions["NFO:TESTCE"]
        self.assertEqual(position["stop_loss"], 88.0)
        self.assertEqual(position["target"], 135.0)
        self.assertEqual(position["trailing_stop"], 90.0)

    def test_intraday_options_backtest_uses_previous_trailing_level_for_current_candle(self) -> None:
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
        engine.positions["NFO:TESTCE"] = {
            "symbol": "NFO:TESTCE",
            "side": "BUY",
            "quantity": 75,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target": 130.0,
            "trailing_stop": 95.0,
            "best_price": 100.0,
            "trailing_distance": 5.0,
            "trailing_activation_distance": 1.0,
            "trailing_active": False,
            "entry_time": "2026-05-18T10:35:00+05:30",
        }

        engine._manage_intraday_options_position(
            symbol="NFO:TESTCE",
            latest_candle={"Open": 100.0, "High": 106.0, "Low": 100.0, "Close": 106.0},
            latest_close=106.0,
            timestamp=pd.Timestamp("2026-05-18 10:36:00+05:30"),
        )

        self.assertIn("NFO:TESTCE", engine.positions)
        self.assertEqual(engine.positions["NFO:TESTCE"]["trailing_stop"], 101.0)

    def test_intraday_options_backtest_prioritizes_price_exit_over_time_exit(self) -> None:
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
        engine.positions["NFO:TESTCE"] = {
            "symbol": "NFO:TESTCE",
            "side": "BUY",
            "quantity": 75,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target": 130.0,
            "trailing_stop": 95.0,
            "best_price": 100.0,
            "trailing_distance": 5.0,
            "trailing_activation_distance": 1.0,
            "trailing_active": False,
            "entry_time": "2026-05-18T14:30:00+05:30",
        }
        engine.trades.append({"symbol": "NFO:TESTCE", "side": "BUY", "entry_price": 100.0, "quantity": 75})

        engine._manage_intraday_options_position(
            symbol="NFO:TESTCE",
            latest_candle={"Open": 94.0, "High": 100.0, "Low": 93.0, "Close": 94.0},
            latest_close=94.0,
            timestamp=pd.Timestamp("2026-05-18 15:02:00+05:30"),
        )

        self.assertEqual(engine.trades[-1]["exit_reason"], "STOP_LOSS")

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


if __name__ == "__main__":
    unittest.main()
