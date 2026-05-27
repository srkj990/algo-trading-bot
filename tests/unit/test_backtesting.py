from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from backtesting import BacktestConfig, BacktestEngine
from orchestration.context import persist_runtime_state


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
        mock_enter.assert_called_once_with(
            ranked_candidates,
            pd.Timestamp("2026-05-18 10:00:00+05:30"),
            history,
        )

    def test_backtest_signal_context_supports_runtime_persistence_contract(self) -> None:
        config = BacktestConfig(
            engine_name="intraday_equity",
            capital=100000.0,
            period="5d",
            interval="5m",
            strategy_mode="AUTO_ADAPTIVE",
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
        timestamp = pd.Timestamp("2026-05-18 10:00:00+05:30")
        history = {
            "SBIN.NS": pd.DataFrame(
                [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 0}],
                index=[timestamp],
            )
        }
        context = engine._build_signal_context(history, timestamp)
        context.regime_cache["SBIN.NS"] = {
            "trade_day": timestamp.date().isoformat(),
            "context": {"allow_entries": True},
        }

        persist_runtime_state(context)

        self.assertIs(context.regime_cache, engine.regime_cache)
        self.assertEqual(context.active_trade_day, timestamp.date())

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

    def test_delivery_equity_skips_entry_when_nifty_is_below_20dma(self) -> None:
        config = BacktestConfig(
            engine_name="delivery_equity",
            capital=100000.0,
            period="6mo",
            interval="1d",
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
        index_history = pd.DataFrame(
            {"Close": list(range(100, 150)) + [120]},
            index=pd.date_range("2026-03-01", periods=51, freq="D"),
        )
        history = {"SBIN.NS": pd.DataFrame(), engine.engine_helper.nifty_trend_symbol: index_history}

        with patch("backtesting.should_enter_trade", return_value=True) as mock_gate:
            engine._enter_ranked_candidates([candidate], pd.Timestamp("2026-05-21"), history)

        mock_gate.assert_not_called()
        self.assertFalse(engine.positions)

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

    def test_enter_ranked_candidates_uses_cost_aware_targets_for_runtime_parity(self) -> None:
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
        actual_targets = {
            "stop_loss": 99.2,
            "target": 101.5,
            "trailing_stop": 99.65,
            "multi_level_targets": [],
            "cost_to_profit_ratio": 0.1,
            "expected_costs": 5.0,
            "expected_net_profit": 10.0,
            "min_breakeven_price": 100.2,
            "asset_class": "INTRADAY_EQUITY",
            "risk_profile": "BALANCED",
        }

        actual_targets.update(
            {
                "trailing_distance": 2.5,
                "trailing_activation_distance": 2.5,
                "stop_distance": 0.8,
            }
        )

        with patch("backtesting.should_enter_trade", return_value=True), \
             patch("backtesting.resolve_trade_targets", return_value=actual_targets) as mock_targets:
            engine._enter_ranked_candidates([candidate], pd.Timestamp("2026-05-18 10:00:00+05:30"))

        mock_targets.assert_called_once_with(
            engine_name="intraday_equity",
            entry_price=100.0,
            quantity=357,
            risk_style_name="BALANCED",
            signal_strength=0.7,
            side="BUY",
            atr=2.0,
            trailing_atr_multiplier=1.25,
            engine_helper=engine.engine_helper,
        )
        position = engine.positions["SBIN.NS"]
        self.assertTrue(position["adaptive_levels_enabled"])
        self.assertAlmostEqual(position["stop_loss"], 97.2)
        self.assertAlmostEqual(position["target"], 109.504)
        self.assertAlmostEqual(position["trailing_stop"], 97.2)
        self.assertEqual(position["expected_costs"], 5.0)
        self.assertEqual(position["cost_to_profit_ratio"], 0.1)

    def test_process_timestamp_uses_position_trailing_distance_in_backtest_loop(self) -> None:
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
        engine.positions["SBIN.NS"] = {
            "symbol": "SBIN.NS",
            "side": "BUY",
            "quantity": 10,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "trailing_stop": 97.0,
            "trailing_distance": 3.5,
            "best_price": 100.0,
            "trailing_active": False,
        }
        history = {
            "SBIN.NS": pd.DataFrame(
                [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 0}],
                index=[pd.Timestamp("2026-05-18 10:00:00+05:30")],
            )
        }

        with patch("backtesting.update_trailing_stop") as mock_trail, \
             patch.object(engine.engine_helper, "evaluate_position_exit", return_value=None), \
             patch("backtesting.scan_symbols", return_value=SimpleNamespace(ranked_candidates=[], symbol_snapshots={})), \
             patch.object(engine, "_mark_equity"):
            engine._process_timestamp(history, pd.Timestamp("2026-05-18 10:00:00+05:30"))

        mock_trail.assert_called_once_with(engine.positions["SBIN.NS"], 100.5, 3.5)

    def test_intraday_backtest_square_off_closes_position_without_scanning(self) -> None:
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
        engine.cash = 99000.0
        engine.positions["SBIN.NS"] = {
            "symbol": "SBIN.NS",
            "side": "BUY",
            "quantity": 10,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "trailing_stop": 97.0,
            "trailing_distance": 3.5,
            "best_price": 100.0,
            "trailing_active": False,
        }
        engine.trades.append(
            {
                "symbol": "SBIN.NS",
                "side": "BUY",
                "entry_time": pd.Timestamp("2026-05-18 10:00:00+05:30"),
                "entry_price": 100.0,
                "quantity": 10,
            }
        )
        history = {
            "SBIN.NS": pd.DataFrame(
                [{"Open": 101.0, "High": 102.0, "Low": 99.0, "Close": 101.5, "Volume": 0}],
                index=[pd.Timestamp("2026-05-18 15:15:00+05:30")],
            )
        }

        with patch("backtesting.scan_symbols") as mock_scan:
            engine._process_timestamp(history, pd.Timestamp("2026-05-18 15:15:00+05:30"))

        mock_scan.assert_not_called()
        self.assertFalse(engine.positions)
        self.assertEqual(engine.trades[0]["exit_reason"], "INTRADAY_SQUARE_OFF")
        self.assertEqual(engine.trades[0]["exit_time"], pd.Timestamp("2026-05-18 15:15:00+05:30"))
        self.assertEqual(engine.trades[0]["exit_price"], 101.5)

    def test_intraday_backtest_entry_cutoff_scans_but_does_not_enter(self) -> None:
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
        history = {
            "SBIN.NS": pd.DataFrame(
                [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 0}],
                index=[pd.Timestamp("2026-05-18 14:50:00+05:30")],
            )
        }
        scan_result = SimpleNamespace(
            ranked_candidates=[
                {
                    "symbol": "SBIN.NS",
                    "signal": "BUY",
                    "agreement_count": 1,
                    "score": 0.7,
                    "latest_close": 100.5,
                    "atr": 2.0,
                }
            ],
            symbol_snapshots={},
        )

        with patch("backtesting.scan_symbols", return_value=scan_result) as mock_scan, \
             patch.object(engine, "_enter_ranked_candidates") as mock_enter:
            engine._process_timestamp(history, pd.Timestamp("2026-05-18 14:50:00+05:30"))

        mock_scan.assert_called_once()
        mock_enter.assert_not_called()
        self.assertFalse(engine.positions)


if __name__ == "__main__":
    unittest.main()
