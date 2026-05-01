from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from engines.delivery_equity import DeliveryEquityEngine
from engines.futures_equity import FuturesEquityEngine
from engines.intraday_equity import IntradayEquityEngine
from engines.intraday_futures import IntradayFuturesEngine
from engines.intraday_options import IntradayOptionsEngine
from engines.options_equity import OptionsEquityEngine


class DeliveryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = DeliveryEquityEngine(5.0, 10.0, 4.0)

    def test_normalize_entry_signal_allows_buy_only(self) -> None:
        self.assertEqual(self.engine.normalize_entry_signal("BUY"), "BUY")
        self.assertIsNone(self.engine.normalize_entry_signal("SELL"))

    def test_get_signal_exit_reason_uses_sell_signal(self) -> None:
        reason = self.engine.get_signal_exit_reason({"side": "BUY"}, "SELL")
        self.assertEqual(reason, "SELL_SIGNAL")

    def test_set_portfolio_rules_updates_symbol_allocation(self) -> None:
        self.engine.set_portfolio_rules(0.5)
        self.assertEqual(self.engine.max_symbol_allocation, 0.5)

    def test_apply_entry_allocation_limit_caps_quantity(self) -> None:
        qty = self.engine.apply_entry_allocation_limit("SBIN.NS", 10, 100.0, {}, 1000.0)
        self.assertEqual(qty, 2)

    def test_reconcile_startup_returns_persisted_positions_in_paper_mode(self) -> None:
        persisted = {"SBIN.NS": {"side": "BUY"}}
        self.assertEqual(self.engine.reconcile_startup("PAPER", persisted), persisted)


class FuturesEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FuturesEquityEngine(5.0, 10.0, 4.0)

    def test_get_cycle_state_weekend_disables_scan(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 5, 2, 10, 0, 0))
        self.assertFalse(state["allow_scan"])

    def test_get_cycle_state_active_session_allows_scan(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 10, 0, 0))
        self.assertTrue(state["allow_scan"])

    def test_get_signal_exit_reason_returns_reversal(self) -> None:
        self.assertEqual(self.engine.get_signal_exit_reason({"side": "BUY"}, "SELL"), "REVERSAL")

    def test_apply_entry_allocation_limit_rounds_to_lot_size(self) -> None:
        with patch("engines.futures_equity.get_contract_lot_size", return_value=25):
            qty = self.engine.apply_entry_allocation_limit("NFO:TESTFUT", 80, 10.0, {}, 1000.0)
        self.assertEqual(qty, 25)

    def test_reconcile_startup_returns_persisted_positions_in_paper_mode(self) -> None:
        persisted = {"NFO:TESTFUT": {"side": "BUY"}}
        self.assertEqual(self.engine.reconcile_startup("PAPER", persisted), persisted)


class IntradayFuturesEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = IntradayFuturesEngine(5.0, 10.0, 4.0)

    def test_get_cycle_state_before_open_waits(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 9, 0, 0))
        self.assertFalse(state["allow_entries"])

    def test_get_cycle_state_entry_cutoff_disables_new_entries(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 15, 10, 0))
        self.assertFalse(state["allow_entries"])
        self.assertTrue(state["allow_scan"])

    def test_get_cycle_state_square_off_forces_exit(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 15, 20, 0))
        self.assertTrue(state["force_square_off"])

    def test_apply_entry_allocation_limit_rounds_using_lot_size(self) -> None:
        with patch("engines.intraday_futures.get_contract_lot_size", return_value=25), \
             patch("engines.futures_equity.get_contract_lot_size", return_value=25):
            qty = self.engine.apply_entry_allocation_limit("NFO:TESTFUT", 80, 10.0, {}, 1000.0)
        self.assertEqual(qty, 25)

    def test_reconcile_startup_returns_persisted_positions_in_paper_mode(self) -> None:
        persisted = {"NFO:TESTFUT": {"side": "BUY"}}
        self.assertEqual(self.engine.reconcile_startup("PAPER", persisted), persisted)


class IntradayEquityEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = IntradayEquityEngine(5.0, 10.0, 4.0)

    def test_get_cycle_state_before_open_waits(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 9, 0, 0))
        self.assertFalse(state["allow_scan"])

    def test_get_cycle_state_entry_cutoff_disables_entries(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 14, 50, 0))
        self.assertFalse(state["allow_entries"])
        self.assertTrue(state["allow_scan"])

    def test_get_cycle_state_square_off_forces_exit(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 15, 20, 0))
        self.assertTrue(state["force_square_off"])

    def test_get_cycle_state_active_session_allows_entries(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 10, 0, 0))
        self.assertTrue(state["allow_entries"])

    def test_get_signal_exit_reason_requires_confirmation_streak(self) -> None:
        position = {"side": "BUY"}
        self.assertIsNone(self.engine.get_signal_exit_reason(position, "SELL"))
        self.assertEqual(self.engine.get_signal_exit_reason(position, "SELL"), "REVERSAL")

    def test_get_signal_exit_reason_resets_streak_when_aligned(self) -> None:
        position = {"side": "BUY", "reversal_streak": 1}
        self.assertIsNone(self.engine.get_signal_exit_reason(position, "BUY"))
        self.assertEqual(position["reversal_streak"], 0)

    def test_requires_extended_intraday_history_for_auto_mode(self) -> None:
        self.assertTrue(self.engine.requires_extended_intraday_history("3"))

    def test_requires_extended_intraday_history_for_breakout(self) -> None:
        self.assertTrue(self.engine.requires_extended_intraday_history("1", strategy_name="BREAKOUT"))

    def test_requires_extended_intraday_history_returns_false_for_other_modes(self) -> None:
        self.assertFalse(self.engine.requires_extended_intraday_history("1", strategy_name="MA"))

    def test_get_vwap_bias_returns_bullish(self) -> None:
        df = pd.DataFrame(
            [{"Close": 100.0, "Volume": 10}, {"Close": 105.0, "Volume": 10}]
        )
        self.assertEqual(self.engine.get_vwap_bias(df), "BULLISH")

    def test_passes_breakout_volume_filter_without_history(self) -> None:
        passed, reason = self.engine.passes_breakout_volume_filter(None)
        self.assertTrue(passed)
        self.assertIn("No extended intraday history", reason)

    def test_apply_signal_filters_blocks_breakout_when_volume_is_weak(self) -> None:
        evaluation = {
            "details": {"BREAKOUT": {"signal": "BUY", "score": 1.0}},
            "signal": "BUY",
        }
        intraday_df = pd.DataFrame([{"Close": 105.0, "Volume": 100}], index=[pd.Timestamp("2026-04-29 09:30:00")])
        with patch.object(self.engine, "passes_breakout_volume_filter", return_value=(False, "Weak volume")), \
             patch.object(self.engine, "passes_vwap_bias_gate", return_value=True):
            filtered = self.engine.apply_signal_filters(evaluation, intraday_df, min_confirmations=1)
        self.assertEqual(filtered["signal"], "HOLD")


class OptionsEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = OptionsEquityEngine(5.0, 10.0, 4.0)

    def test_get_cycle_state_active_session_allows_scan(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 10, 0, 0))
        self.assertTrue(state["allow_scan"])

    def test_get_signal_exit_reason_returns_reversal(self) -> None:
        self.assertEqual(self.engine.get_signal_exit_reason({"side": "BUY"}, "SELL"), "REVERSAL")

    def test_apply_entry_allocation_limit_rounds_to_lot_size(self) -> None:
        with patch("engines.options_equity.get_contract_lot_size", return_value=15):
            qty = self.engine.apply_entry_allocation_limit("NFO:TESTCE", 100, 10.0, {}, 1000.0)
        self.assertEqual(qty, 15)

    def test_reconcile_startup_returns_persisted_positions_in_paper_mode(self) -> None:
        persisted = {"NFO:TESTCE": {"side": "BUY"}}
        self.assertEqual(self.engine.reconcile_startup("PAPER", persisted), persisted)


class IntradayOptionsEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = IntradayOptionsEngine(5.0, 10.0, 4.0)

    def _option_session_df(self) -> pd.DataFrame:
        rows = []
        for index in range(1, 22):
            rows.append(
                {
                    "Open": 100.0 + (index * 0.2),
                    "High": 100.9 + (index * 0.2),
                    "Low": 99.9 + (index * 0.2),
                    "Close": 100.8 + (index * 0.2),
                    "Volume": 100.0,
                }
            )
        rows[-1] = {
            "Open": 104.0,
            "High": 105.9,
            "Low": 104.0,
            "Close": 105.8,
            "Volume": 220.0,
        }
        index = pd.date_range("2026-04-29 09:20:00", periods=len(rows), freq="1min")
        return pd.DataFrame(rows, index=index)

    def _mean_reversion_session_df(self) -> pd.DataFrame:
        rows = []
        for index in range(1, 22):
            rows.append(
                {
                    "Open": 100.0 + (index * 0.1),
                    "High": 100.8 + (index * 0.1),
                    "Low": 99.8 + (index * 0.1),
                    "Close": 100.3 + (index * 0.1),
                    "Volume": 100.0,
                }
            )
        rows[-1] = {
            "Open": 102.7,
            "High": 103.0,
            "Low": 102.2,
            "Close": 102.9,
            "Volume": 110.0,
        }
        index = pd.date_range("2026-04-29 09:20:00", periods=len(rows), freq="1min")
        return pd.DataFrame(rows, index=index)

    def _volatility_session_df(self) -> pd.DataFrame:
        rows = []
        for index in range(1, 22):
            rows.append(
                {
                    "Open": 100.0 + (index * 0.1),
                    "High": 100.8 + (index * 0.1),
                    "Low": 99.9 + (index * 0.1),
                    "Close": 100.4 + (index * 0.1),
                    "Volume": 100.0,
                }
            )
        rows[-1] = {
            "Open": 102.4,
            "High": 104.1,
            "Low": 102.2,
            "Close": 103.8,
            "Volume": 150.0,
        }
        index = pd.date_range("2026-04-29 09:20:00", periods=len(rows), freq="1min")
        return pd.DataFrame(rows, index=index)

    def _append_option_candle(
        self,
        df: pd.DataFrame,
        *,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> pd.DataFrame:
        next_index = df.index[-1] + pd.Timedelta(minutes=1)
        appended = pd.DataFrame(
            [
                {
                    "Open": open_price,
                    "High": high,
                    "Low": low,
                    "Close": close,
                    "Volume": volume,
                }
            ],
            index=[next_index],
        )
        return pd.concat([df, appended])

    def test_get_cycle_state_before_open_waits(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 9, 15, 0))
        self.assertFalse(state["allow_entries"])

    def test_get_cycle_state_entry_cutoff_disables_entries(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 15, 11, 0))
        self.assertFalse(state["allow_entries"])
        self.assertTrue(state["allow_scan"])

    def test_get_cycle_state_square_off_forces_exit(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 15, 20, 0))
        self.assertTrue(state["force_square_off"])

    def test_get_cycle_state_active_session_allows_entries(self) -> None:
        state = self.engine.get_cycle_state(datetime(2026, 4, 29, 10, 0, 0))
        self.assertTrue(state["allow_entries"])

    def test_apply_entry_allocation_limit_rounds_to_lot_size(self) -> None:
        with patch("engines.intraday_options.get_contract_lot_size", return_value=50), \
             patch("engines.options_equity.get_contract_lot_size", return_value=50):
            qty = self.engine.apply_entry_allocation_limit("NFO:TESTCE", 120, 10.0, {}, 10000.0)
        self.assertEqual(qty, 100)

    def test_get_time_exit_reason_uses_max_hold_minutes(self) -> None:
        position = {"entry_time": "2026-04-29T09:00:00"}
        reason = self.engine.get_time_exit_reason(position, datetime(2026, 4, 29, 12, 30, 0))
        self.assertTrue(reason.startswith("TIME_EXIT_"))

    def test_get_time_exit_reason_uses_cutoff_time(self) -> None:
        reason = self.engine.get_time_exit_reason({}, datetime.combine(datetime(2026, 4, 29).date(), self.engine.time_exit_cutoff))
        self.assertIn("TIME_EXIT_", reason)

    def test_get_trade_frequency_key_uses_underlying(self) -> None:
        self.assertEqual(self.engine.get_trade_frequency_key("NFO:TEST", {"underlying": "NIFTY"}), "NIFTY")

    def test_get_max_trades_per_day_returns_configured_value(self) -> None:
        self.assertEqual(self.engine.get_max_trades_per_day(), self.engine.max_trades_per_underlying_per_day)

    def test_get_entry_profile_returns_expected_profile(self) -> None:
        self.assertEqual(self.engine.get_entry_profile("ATM_MOMENTUM"), "MOMENTUM")
        self.assertEqual(self.engine.get_entry_profile("ATM_VWAP_REVERSION"), "MEAN_REVERSION")
        self.assertEqual(self.engine.get_entry_profile("ATM_IV_EXPANSION"), "VOLATILITY")

    def test_build_volatility_regime_context_classifies_expansion(self) -> None:
        regime = self.engine.build_volatility_regime_context(
            self._volatility_session_df(),
            {"iv_change_15m_pct": 4.0},
        )
        self.assertEqual(regime["label"], "EXPANSION")

    def test_validate_momentum_entry_arms_setup_on_breakout_candle(self) -> None:
        passed, reason = self.engine.validate_momentum_entry(
            "BUY",
            self._option_session_df(),
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=105.8,
            option_vwap=103.0,
            strategy_name="ATM_MOMENTUM",
        )
        self.assertFalse(passed)
        self.assertIn("armed", reason)
        self.assertEqual(
            self.engine.momentum_entry_setups["UNKNOWN:ATM_MOMENTUM:BUY:BUY"]["state"],
            "awaiting_confirmation",
        )

    def test_validate_momentum_entry_confirms_after_follow_through(self) -> None:
        breakout_df = self._option_session_df()
        self.engine.validate_momentum_entry(
            "BUY",
            breakout_df,
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=105.8,
            option_vwap=103.0,
            strategy_name="ATM_MOMENTUM",
        )
        confirmation_df = self._append_option_candle(
            breakout_df,
            open_price=105.9,
            high=106.65,
            low=105.7,
            close=106.55,
            volume=260.0,
        )
        passed, reason = self.engine.validate_momentum_entry(
            "BUY",
            confirmation_df,
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=106.55,
            option_vwap=103.8,
            strategy_name="ATM_MOMENTUM",
        )
        self.assertFalse(passed)
        self.assertIn("confirmed", reason)
        self.assertEqual(
            self.engine.momentum_entry_setups["UNKNOWN:ATM_MOMENTUM:BUY:BUY"]["state"],
            "awaiting_pullback",
        )

    def test_validate_momentum_entry_passes_after_pullback_retest(self) -> None:
        breakout_df = self._option_session_df()
        self.engine.validate_momentum_entry(
            "BUY",
            breakout_df,
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=105.8,
            option_vwap=103.0,
            strategy_name="ATM_MOMENTUM",
        )
        confirmation_df = self._append_option_candle(
            breakout_df,
            open_price=105.9,
            high=106.65,
            low=105.7,
            close=106.55,
            volume=260.0,
        )
        self.engine.validate_momentum_entry(
            "BUY",
            confirmation_df,
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=106.55,
            option_vwap=103.8,
            strategy_name="ATM_MOMENTUM",
        )
        pullback_df = self._append_option_candle(
            confirmation_df,
            open_price=106.1,
            high=106.5,
            low=104.9,
            close=105.7,
            volume=280.0,
        )
        passed, reason = self.engine.validate_momentum_entry(
            "BUY",
            pullback_df,
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=105.7,
            option_vwap=105.1,
            strategy_name="ATM_MOMENTUM",
        )
        self.assertTrue(passed)
        self.assertIn("pullback entry ready", reason)
        self.assertEqual(self.engine.momentum_entry_setups, {})

    def test_validate_momentum_entry_blocks_when_volume_spike_is_missing(self) -> None:
        df = self._option_session_df()
        df.iloc[-1, df.columns.get_loc("Volume")] = 110.0
        passed, reason = self.engine.validate_momentum_entry(
            "BUY",
            df,
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=105.8,
            option_vwap=103.0,
            strategy_name="ATM_MOMENTUM",
        )
        self.assertFalse(passed)
        self.assertIn("volume", reason)

    def test_validate_momentum_entry_blocks_when_trend_alignment_fails(self) -> None:
        passed, reason = self.engine.validate_momentum_entry(
            "BUY",
            self._option_session_df(),
            {"underlying_bias": "BEARISH", "volatility_regime": "NORMAL"},
            latest_close=105.8,
            option_vwap=103.0,
            strategy_name="ATM_MOMENTUM",
        )
        self.assertFalse(passed)
        self.assertIn("trend alignment failed", reason)

    def test_validate_momentum_entry_blocks_in_sideways_regime(self) -> None:
        passed, reason = self.engine.validate_momentum_entry(
            "BUY",
            self._option_session_df(),
            {"underlying_bias": "BULLISH", "volatility_regime": "SIDEWAYS"},
            latest_close=105.8,
            option_vwap=103.0,
            strategy_name="ATM_MOMENTUM",
        )
        self.assertFalse(passed)
        self.assertIn("SIDEWAYS", reason)

    def test_validate_mean_reversion_entry_passes_for_vwap_retest(self) -> None:
        passed, reason = self.engine.validate_mean_reversion_entry(
            "BUY",
            self._mean_reversion_session_df(),
            {"underlying_bias": "BULLISH", "volatility_regime": "NORMAL"},
            latest_close=102.9,
            option_vwap=102.4,
        )
        self.assertTrue(passed)
        self.assertIn("passed", reason)

    def test_validate_mean_reversion_entry_blocks_in_expansion_regime(self) -> None:
        passed, reason = self.engine.validate_mean_reversion_entry(
            "BUY",
            self._mean_reversion_session_df(),
            {"underlying_bias": "BULLISH", "volatility_regime": "EXPANSION"},
            latest_close=102.9,
            option_vwap=102.4,
        )
        self.assertFalse(passed)
        self.assertIn("EXPANSION", reason)

    def test_validate_mean_reversion_entry_blocks_when_price_misses_vwap_retest(self) -> None:
        passed, reason = self.engine.validate_mean_reversion_entry(
            "BUY",
            self._mean_reversion_session_df(),
            {"underlying_bias": "BULLISH"},
            latest_close=102.9,
            option_vwap=101.0,
        )
        self.assertFalse(passed)
        self.assertIn("retest VWAP zone", reason)

    def test_validate_volatility_entry_passes_for_expansion_setup(self) -> None:
        passed, reason = self.engine.validate_volatility_entry(
            "BUY",
            self._volatility_session_df(),
            {
                "underlying_bias": "BULLISH",
                "iv_percentile": 35.0,
                "iv_change_15m_pct": 4.0,
                "volatility_regime": "EXPANSION",
            },
            latest_close=103.8,
            option_vwap=102.6,
        )
        self.assertTrue(passed)
        self.assertIn("passed", reason)

    def test_validate_volatility_entry_blocks_when_iv_is_not_supportive(self) -> None:
        passed, reason = self.engine.validate_volatility_entry(
            "BUY",
            self._volatility_session_df(),
            {
                "underlying_bias": "BULLISH",
                "iv_percentile": 35.0,
                "iv_change_15m_pct": -1.0,
                "volatility_regime": "EXPANSION",
            },
            latest_close=103.8,
            option_vwap=102.6,
        )
        self.assertFalse(passed)
        self.assertIn("IV percentile unavailable or short-term IV change is not supportive", reason)

    def test_validate_volatility_entry_blocks_in_sideways_regime(self) -> None:
        passed, reason = self.engine.validate_volatility_entry(
            "BUY",
            self._volatility_session_df(),
            {
                "underlying_bias": "BULLISH",
                "iv_percentile": 35.0,
                "iv_change_15m_pct": 4.0,
                "volatility_regime": "SIDEWAYS",
            },
            latest_close=103.8,
            option_vwap=102.6,
        )
        self.assertFalse(passed)
        self.assertIn("SIDEWAYS", reason)

    def test_apply_signal_filters_blocks_momentum_profile_when_validator_fails(self) -> None:
        evaluation = {
            "signal": "BUY",
            "agreement_count": 1,
            "score": 1.5,
            "strategy": "ATM_MOMENTUM",
            "details": {},
        }
        analytics = {
            "underlying": "NIFTY",
            "underlying_bias": "BULLISH",
            "option_type": "CE",
            "option_price": 120.0,
            "delta": 0.35,
            "iv": 0.2,
            "iv_percentile": 40.0,
            "days_to_expiry": 3,
        }
        with patch.object(self.engine, "get_underlying_bias", return_value={"bias": "BULLISH", "ema": 100.0, "vwap": 99.0, "close": 101.0}), \
             patch.object(self.engine, "validate_momentum_entry", return_value=(False, "Momentum validator blocked")):
            filtered = self.engine.apply_signal_filters(
                evaluation,
                self._option_session_df(),
                analytics=analytics,
            )
        self.assertEqual(filtered["signal"], "HOLD")
        self.assertEqual(filtered["score"], 0.0)
        self.assertEqual(filtered["options_filter_note"], "Momentum validator blocked")

    def test_apply_signal_filters_does_not_apply_momentum_validator_to_mean_reversion(self) -> None:
        evaluation = {
            "signal": "BUY",
            "agreement_count": 1,
            "score": 1.5,
            "strategy": "ATM_VWAP_REVERSION",
            "details": {},
        }
        analytics = {
            "underlying": "NIFTY",
            "underlying_bias": "BULLISH",
            "option_type": "CE",
            "option_price": 120.0,
            "delta": 0.35,
            "iv": 0.2,
            "iv_percentile": 40.0,
            "days_to_expiry": 3,
        }
        with patch.object(self.engine, "get_underlying_bias", return_value={"bias": "BULLISH", "ema": 100.0, "vwap": 99.0, "close": 101.0}), \
             patch.object(self.engine, "validate_momentum_entry") as validator, \
             patch.object(self.engine, "validate_mean_reversion_entry", return_value=(True, "Mean reversion validator passed")):
            filtered = self.engine.apply_signal_filters(
                evaluation,
                self._mean_reversion_session_df(),
                analytics=analytics,
            )
        validator.assert_not_called()
        self.assertEqual(filtered["signal"], "BUY")

    def test_apply_signal_filters_uses_mean_reversion_validator_for_mean_reversion_profile(self) -> None:
        evaluation = {
            "signal": "BUY",
            "agreement_count": 1,
            "score": 1.5,
            "strategy": "ATM_VWAP_REVERSION",
            "details": {},
        }
        analytics = {
            "underlying": "NIFTY",
            "underlying_bias": "BULLISH",
            "option_type": "CE",
            "option_price": 120.0,
            "delta": 0.35,
            "iv": 0.2,
            "iv_percentile": 40.0,
            "days_to_expiry": 3,
        }
        with patch.object(self.engine, "get_underlying_bias", return_value={"bias": "BULLISH", "ema": 100.0, "vwap": 99.0, "close": 101.0}), \
             patch.object(self.engine, "validate_mean_reversion_entry", return_value=(False, "Mean reversion validator blocked")):
            filtered = self.engine.apply_signal_filters(
                evaluation,
                self._mean_reversion_session_df(),
                analytics=analytics,
            )
        self.assertEqual(filtered["signal"], "HOLD")
        self.assertEqual(filtered["options_filter_note"], "Mean reversion validator blocked")

    def test_apply_signal_filters_uses_volatility_validator_for_volatility_profile(self) -> None:
        evaluation = {
            "signal": "BUY",
            "agreement_count": 1,
            "score": 1.5,
            "strategy": "ATM_IV_EXPANSION",
            "details": {},
        }
        analytics = {
            "underlying": "NIFTY",
            "underlying_bias": "BULLISH",
            "option_type": "CE",
            "option_price": 120.0,
            "delta": 0.35,
            "iv": 0.2,
            "iv_percentile": 10.0,
            "iv_change_15m_pct": 3.0,
            "days_to_expiry": 3,
        }
        with patch.object(self.engine, "get_underlying_bias", return_value={"bias": "BULLISH", "ema": 100.0, "vwap": 99.0, "close": 101.0}), \
             patch.object(self.engine, "validate_volatility_entry", return_value=(False, "Volatility validator blocked")):
            filtered = self.engine.apply_signal_filters(
                evaluation,
                self._volatility_session_df(),
                analytics=analytics,
            )
        self.assertEqual(filtered["signal"], "HOLD")
        self.assertEqual(filtered["options_filter_note"], "Volatility validator blocked")

    def test_apply_signal_filters_routes_atm_multi_to_momentum_validator(self) -> None:
        evaluation = {
            "signal": "BUY",
            "agreement_count": 1,
            "score": 1.5,
            "strategy": "ATM_MULTI",
            "selected_profile": "MOMENTUM",
            "details": {"ATM_MULTI": {"selected_profile": "MOMENTUM"}},
        }
        analytics = {
            "underlying": "NIFTY",
            "underlying_bias": "BULLISH",
            "option_type": "CE",
            "option_price": 120.0,
            "delta": 0.35,
            "iv": 0.2,
            "iv_percentile": 40.0,
            "days_to_expiry": 3,
        }
        with patch.object(self.engine, "get_underlying_bias", return_value={"bias": "BULLISH", "ema": 100.0, "vwap": 99.0, "close": 101.0}), \
             patch.object(self.engine, "validate_momentum_entry", return_value=(False, "Momentum validator blocked")) as momentum_validator, \
             patch.object(self.engine, "validate_mean_reversion_entry") as mean_reversion_validator:
            filtered = self.engine.apply_signal_filters(
                evaluation,
                self._option_session_df(),
                analytics=analytics,
            )
        momentum_validator.assert_called_once()
        mean_reversion_validator.assert_not_called()
        self.assertEqual(filtered["options_filter_note"], "Momentum validator blocked")

    def test_apply_signal_filters_routes_atm_multi_to_mean_reversion_validator(self) -> None:
        evaluation = {
            "signal": "BUY",
            "agreement_count": 1,
            "score": 1.5,
            "strategy": "ATM_MULTI",
            "selected_profile": "MEAN_REVERSION",
            "details": {"ATM_MULTI": {"selected_profile": "MEAN_REVERSION"}},
        }
        analytics = {
            "underlying": "NIFTY",
            "underlying_bias": "BULLISH",
            "option_type": "CE",
            "option_price": 120.0,
            "delta": 0.35,
            "iv": 0.2,
            "iv_percentile": 40.0,
            "days_to_expiry": 3,
        }
        with patch.object(self.engine, "get_underlying_bias", return_value={"bias": "BULLISH", "ema": 100.0, "vwap": 99.0, "close": 101.0}), \
             patch.object(self.engine, "validate_momentum_entry") as momentum_validator, \
             patch.object(self.engine, "validate_mean_reversion_entry", return_value=(False, "Mean reversion validator blocked")) as mean_reversion_validator:
            filtered = self.engine.apply_signal_filters(
                evaluation,
                self._mean_reversion_session_df(),
                analytics=analytics,
            )
        momentum_validator.assert_not_called()
        mean_reversion_validator.assert_called_once()
        self.assertEqual(filtered["options_filter_note"], "Mean reversion validator blocked")


if __name__ == "__main__":
    unittest.main()
