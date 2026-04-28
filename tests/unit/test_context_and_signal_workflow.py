from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from orchestration.context import TradingContext, build_trading_context, persist_runtime_state
from orchestration.signal_workflow import (
    get_cached_regime_context,
    get_stable_signal_data,
    log_market_context,
    resolve_atm_option_contract_snapshot,
)


class ContextTests(unittest.TestCase):
    def test_build_trading_context_hydrates_runtime_state(self) -> None:
        engine = Mock()
        engine.name = "intraday_equity"
        engine.reconcile_startup.return_value = {"SBIN.NS": {"side": "BUY"}}
        engine.export_runtime_state.return_value = {"runtime_flag": True}
        config = SimpleNamespace(engine=engine, execution_mode="PAPER")
        saved_state = {
            "positions": {"SBIN.NS": {"side": "BUY"}},
            "traded_symbols_today": ["SBIN.NS"],
            "trade_counts_today": {"SBIN.NS": 2},
            "active_trade_day": "2026-04-29",
            "last_entry_time": 33.0,
            "regime_cache": {"SBIN.NS": {"mode": "trend"}},
            "engine_runtime_state": {"runtime_flag": True},
        }
        with patch("orchestration.context.load_engine_state", return_value=saved_state), \
             patch("orchestration.context.get_data_service", return_value=Mock()), \
             patch("orchestration.context.persist_runtime_state") as persist:
            context = build_trading_context(config)
        self.assertEqual(context.positions["SBIN.NS"]["side"], "BUY")
        self.assertEqual(context.trade_counts_today["SBIN.NS"], 2)
        engine.hydrate_runtime_state.assert_called_once_with(saved_state)
        persist.assert_called_once()

    def test_persist_runtime_state_delegates_to_save_runtime_state(self) -> None:
        context = SimpleNamespace(
            engine=SimpleNamespace(name="intraday_equity"),
            positions={},
            traded_symbols_today={"SBIN.NS"},
            trade_counts_today={"SBIN.NS": 1},
            active_trade_day=date(2026, 4, 29),
            last_entry_time=0.0,
            regime_cache={},
            engine_runtime_state={"runtime_flag": True},
            save_engine_state=Mock(),
        )
        with patch("orchestration.context.save_runtime_state") as saver:
            persist_runtime_state(context)
        saver.assert_called_once()


class SignalWorkflowHelperTests(unittest.TestCase):
    def test_get_cached_regime_context_returns_matching_day(self) -> None:
        regime_cache = {"SBIN.NS": {"trade_day": "2026-04-29", "context": {"mode": "trend"}}}
        self.assertEqual(
            get_cached_regime_context(regime_cache, "SBIN.NS", date(2026, 4, 29)),
            {"mode": "trend"},
        )

    def test_get_cached_regime_context_returns_none_for_stale_day(self) -> None:
        regime_cache = {"SBIN.NS": {"trade_day": "2026-04-28", "context": {"mode": "trend"}}}
        self.assertIsNone(get_cached_regime_context(regime_cache, "SBIN.NS", date(2026, 4, 29)))

    def test_get_stable_signal_data_drops_open_candle_when_engine_requires_closed_candle(self) -> None:
        engine = SimpleNamespace(require_closed_signal_candle=True)
        now = datetime(2026, 4, 29, 9, 31, 15)
        data = pd.DataFrame(
            [{"Close": 100.0}, {"Close": 101.0}],
            index=[pd.Timestamp("2026-04-29 09:30:00"), pd.Timestamp("2026-04-29 09:31:00")],
        )
        stable = get_stable_signal_data(engine, data, now)
        self.assertEqual(len(stable), 1)

    def test_log_market_context_writes_reason_when_present(self) -> None:
        log_event = Mock()
        log_market_context(
            log_event,
            "SBIN.NS",
            {
                "gap_percent": 1.2,
                "gap_type": "UP",
                "behavior": "TREND",
                "strategies": ["MA"],
                "min_confirmations": 1,
                "allow_entries": True,
                "reason": "Opening trend",
            },
        )
        self.assertEqual(log_event.call_count, 2)

    def test_resolve_atm_option_contract_snapshot_returns_enriched_contract_snapshot(self) -> None:
        option_data = pd.DataFrame(
            [{"Open": 10.0, "High": 12.0, "Low": 9.0, "Close": 11.0, "Volume": 100}],
            index=[pd.Timestamp("2026-04-29 09:30:00")],
        )
        with patch("orchestration.signal_workflow.get_atm_option_strike", return_value=24500), \
             patch("orchestration.signal_workflow.resolve_option_contract", return_value="NFO:TEST24500CE"), \
             patch("orchestration.signal_workflow.get_option_greeks_snapshot", return_value={"delta": 0.3}), \
             patch("orchestration.signal_workflow.get_atr_value", return_value=2.5):
            snapshot = resolve_atm_option_contract_snapshot(
                engine=SimpleNamespace(data_period="10d", data_interval="1m", require_closed_signal_candle=False),
                atm_option_config={"underlying": "NIFTY", "expiry": "2026-04-30", "strike_offset": 0},
                evaluation={"option_type": "CE"},
                now=datetime(2026, 4, 29, 9, 31, 0),
                fetch_data=Mock(return_value=option_data),
            )
        self.assertEqual(snapshot["symbol"], "NFO:TEST24500CE")
        self.assertEqual(snapshot["latest_close"], 11.0)


if __name__ == "__main__":
    unittest.main()
