from __future__ import annotations

import unittest
import uuid
from datetime import date

import state_store


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine_name = f"test_engine_{uuid.uuid4().hex}"
        self.state_path = state_store._state_path(self.engine_name)
        self.addCleanup(self._cleanup_state_file)

    def _cleanup_state_file(self) -> None:
        try:
            if self.state_path.exists():
                self.state_path.unlink()
        except OSError:
            pass

    def test_load_engine_state_returns_defaults_when_missing(self) -> None:
        state = state_store.load_engine_state(self.engine_name)
        self.assertEqual(state["positions"], {})
        self.assertEqual(state["traded_symbols_today"], [])
        self.assertEqual(state["trade_counts_today"], {})
        self.assertEqual(state["last_entry_time"], 0)
        self.assertEqual(state["engine_runtime_state"], {})

    def test_state_path_uses_lowercase_engine_name(self) -> None:
        path = state_store._state_path("Intraday_Equity")
        self.assertEqual(path.name, "intraday_equity_state.json")

    def test_save_engine_state_creates_state_file(self) -> None:
        state_store.save_engine_state(
            engine_name=self.engine_name,
            positions={"SBIN.NS": {"side": "BUY"}},
            traded_symbols_today={"SBIN.NS"},
            trade_counts_today={"SBIN.NS": 1},
            active_trade_day=date(2026, 4, 29),
            last_entry_time=123.4,
            regime_cache={"SBIN.NS": {"mode": "trend"}},
            engine_runtime_state={"momentum_entry_setups": {"NIFTY:ATM_MOMENTUM:CE:BUY": {"state": "awaiting_confirmation"}}},
        )
        self.assertTrue(self.state_path.exists())

    def test_save_and_load_round_trip_state(self) -> None:
        state_store.save_engine_state(
            engine_name=self.engine_name,
            positions={"SBIN.NS": {"side": "BUY", "quantity": 1}},
            traded_symbols_today={"SBIN.NS", "INFY.NS"},
            trade_counts_today={"SBIN.NS": 2},
            active_trade_day=date(2026, 4, 29),
            last_entry_time=55.0,
            regime_cache={"SBIN.NS": {"allow_entries": True}},
            engine_runtime_state={"runtime_flag": True},
        )
        loaded = state_store.load_engine_state(self.engine_name)
        self.assertEqual(sorted(loaded["traded_symbols_today"]), ["INFY.NS", "SBIN.NS"])
        self.assertEqual(loaded["trade_counts_today"]["SBIN.NS"], 2)
        self.assertEqual(loaded["positions"]["SBIN.NS"]["quantity"], 1)
        self.assertTrue(loaded["engine_runtime_state"]["runtime_flag"])

    def test_load_engine_state_fills_missing_keys(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"positions": {"SBIN.NS": {"side": "BUY"}}}', encoding="utf-8")
        loaded = state_store.load_engine_state(self.engine_name)
        self.assertIn("traded_symbols_today", loaded)
        self.assertIn("trade_counts_today", loaded)
        self.assertIn("active_trade_day", loaded)
        self.assertIn("regime_cache", loaded)
        self.assertIn("engine_runtime_state", loaded)


if __name__ == "__main__":
    unittest.main()
