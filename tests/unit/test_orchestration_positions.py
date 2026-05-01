from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock

import orchestration.positions as position_flow
from engines.common import build_position


class PositionWorkflowHelperTests(unittest.TestCase):
    def test_get_pair_symbols_returns_all_matching_symbols(self) -> None:
        positions = {
            "PE": {"pair_id": "PAIR-1"},
            "CE": {"pair_id": "PAIR-1"},
            "SBIN.NS": {},
        }
        self.assertEqual(position_flow.get_pair_symbols(positions, "PAIR-1"), ["PE", "CE"])

    def test_get_pair_position_metrics_returns_totals(self) -> None:
        positions = {
            "PE": build_position("PE", "SELL", 1, 100.0, sl_pct=5, target_pct=10, trailing_pct=4),
            "CE": build_position("CE", "SELL", 1, 120.0, sl_pct=5, target_pct=10, trailing_pct=4),
        }
        snapshots = {
            "PE": {"latest_close": 90.0},
            "CE": {"latest_close": 110.0},
        }
        metrics = position_flow.get_pair_position_metrics(positions, ["PE", "CE"], snapshots)
        self.assertEqual(metrics["entry_total_premium"], 220.0)
        self.assertEqual(metrics["current_total_premium"], 200.0)
        self.assertGreater(metrics["total_pnl"], 0.0)

    def test_get_pair_position_metrics_returns_none_when_snapshot_missing(self) -> None:
        positions = {"PE": build_position("PE", "SELL", 1, 100.0, sl_pct=5, target_pct=10, trailing_pct=4)}
        self.assertIsNone(position_flow.get_pair_position_metrics(positions, ["PE"], {}))

    def test_get_latest_exit_price_prefers_snapshot(self) -> None:
        price = position_flow.get_latest_exit_price(
            engine=Mock(data_period="1d", data_interval="1m"),
            symbol="SBIN.NS",
            position=build_position("SBIN.NS", "BUY", 1, 100.0, sl_pct=5, target_pct=10, trailing_pct=4),
            fetch_data=Mock(),
            log_event=Mock(),
            symbol_snapshots={"SBIN.NS": {"latest_close": 101.5}},
        )
        self.assertEqual(price, 101.5)

    def test_build_exit_position_lines_include_optional_fields(self) -> None:
        position = build_position(
            "SBIN.NS",
            "BUY",
            1,
            100.0,
            sl_pct=5,
            target_pct=10,
            trailing_pct=4,
            entry_time="2026-04-29T09:20:00",
        )
        lines = position_flow.build_exit_position_lines(position, 105.0, "TARGET")
        self.assertTrue(any("EntryTime:" in line for line in lines))
        self.assertTrue(any("Target:" in line for line in lines))

    def test_format_trade_time_returns_dash_for_empty(self) -> None:
        self.assertEqual(position_flow.format_trade_time(None), "-")

    def test_format_trade_time_returns_raw_string_for_invalid_value(self) -> None:
        self.assertEqual(position_flow.format_trade_time("not-a-date"), "not-a-date")

    def test_parse_trade_day_uses_fallback_for_invalid_input(self) -> None:
        parsed = position_flow.parse_trade_day("invalid")
        self.assertIsNotNone(parsed.year)

    def test_save_runtime_state_delegates_to_save_engine_state(self) -> None:
        saver = Mock()
        position_flow.save_runtime_state(
            "intraday_equity",
            {},
            {"SBIN.NS"},
            {"SBIN.NS": 1},
            datetime(2026, 4, 29).date(),
            100.0,
            {},
            {},
            saver,
        )
        saver.assert_called_once()

    def test_log_ranked_candidates_logs_empty_message(self) -> None:
        log_event = Mock()
        position_flow.log_ranked_candidates([], log_event)
        log_event.assert_called_once_with("[SCAN] No actionable ranked candidates")

    def test_build_option_pair_candidate_returns_candidate_for_valid_range_setup(self) -> None:
        candidate = position_flow.build_option_pair_candidate(
            engine=Mock(),
            pair_config={
                "mode": "TWO_LEG_RANGE",
                "pair_id": "PAIR-1",
                "symbols": ["PE", "CE"],
                "lower_strike": 24000,
                "upper_strike": 24600,
                "entry_side": "SELL",
            },
            symbol_snapshots={
                "PE": {"signal": "SELL", "latest_close": 100.0, "atr": 10.0, "score": 0.2, "analytics": {"underlying": "NIFTY", "underlying_price": 24300.0}},
                "CE": {"signal": "SELL", "latest_close": 120.0, "atr": 12.0, "score": 0.3, "analytics": {"underlying": "NIFTY", "underlying_price": 24300.0}},
            },
            positions={},
            log_event=Mock(),
        )
        self.assertEqual(candidate["symbol"], "PAIR-1")
        self.assertTrue(candidate["is_pair"])

    def test_build_option_pair_candidate_returns_none_when_out_of_range(self) -> None:
        candidate = position_flow.build_option_pair_candidate(
            engine=Mock(),
            pair_config={
                "mode": "TWO_LEG_RANGE",
                "pair_id": "PAIR-1",
                "symbols": ["PE", "CE"],
                "lower_strike": 24000,
                "upper_strike": 24600,
            },
            symbol_snapshots={
                "PE": {"signal": "SELL", "latest_close": 100.0, "atr": 10.0, "score": 0.2, "analytics": {"underlying_price": 24700.0}},
                "CE": {"signal": "SELL", "latest_close": 120.0, "atr": 12.0, "score": 0.3, "analytics": {"underlying_price": 24700.0}},
            },
            positions={},
            log_event=Mock(),
        )
        self.assertIsNone(candidate)

    def test_close_position_symbols_removes_positions_and_records_trade(self) -> None:
        positions = {
            "SBIN.NS": build_position("SBIN.NS", "BUY", 1, 100.0, sl_pct=5, target_pct=10, trailing_pct=4)
        }
        trade_book = []
        changed = position_flow.close_position_symbols(
            engine=Mock(data_period="1d", data_interval="1m", order_product="MIS"),
            positions=positions,
            symbols=["SBIN.NS"],
            reason="Manual close",
            trade_book=trade_book,
            trade_store=None,
            place_order=Mock(),
            log_order_signal_banner=Mock(),
            fetch_data=Mock(),
            log_event=Mock(),
            transaction_cost_model_enabled=False,
            slippage_pct_per_side=0.0,
            symbol_snapshots={"SBIN.NS": {"latest_close": 102.0}},
            exit_time=datetime(2026, 4, 29, 15, 0, 0),
        )
        self.assertTrue(changed)
        self.assertEqual(positions, {})
        self.assertEqual(len(trade_book), 1)

    def test_force_square_off_positions_returns_false_when_flat(self) -> None:
        changed = position_flow.force_square_off_positions(
            engine=Mock(),
            positions={},
            trade_book=[],
            trade_store=None,
            place_order=Mock(),
            log_order_signal_banner=Mock(),
            fetch_data=Mock(),
            log_event=Mock(),
            transaction_cost_model_enabled=False,
            slippage_pct_per_side=0.0,
        )
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
