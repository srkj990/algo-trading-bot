from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engines.intraday_options import IntradayOptionsEngine
from orchestration.session import _execute_single_entry


class SessionEntrySizingTests(unittest.TestCase):
    def test_intraday_options_capital_based_qty_uses_affordable_lot_quantity(self) -> None:
        engine = IntradayOptionsEngine(sl_percent=10.0, target_percent=20.0, trailing_percent=7.5)
        context = SimpleNamespace(
            config=SimpleNamespace(
                capital=100000.0,
                risk_percent=0.01,
                atr_stop_multiplier=2.0,
                trailing_atr_multiplier=1.25,
                target_risk_reward=2.0,
                risk_style_name="BALANCED",
                max_open_positions=1,
                max_capital_per_trade=100000.0,
                max_capital_deployed=100000.0,
                one_trade_per_symbol_per_day=True,
                execution_mode="PAPER",
                atm_option_config={
                    "underlying": "NIFTY",
                    "expiry": "2026-05-19",
                    "strike_offset_mode": "ATM",
                },
                intraday_options_lot_mode="CAPITAL_BASED",
            ),
            engine=engine,
            positions={},
            traded_symbols_today=set(),
            trade_counts_today={},
            last_entry_time=0.0,
            runtime_config=SimpleNamespace(
                orders=SimpleNamespace(
                    default_entry_order_type="MARKET",
                    entry_limit_price_buffer_pct=0.0,
                ),
                fno=SimpleNamespace(
                    intraday_options_max_entry_cost_ratio=0.30,
                    intraday_options_max_spread_pct=0.02,
                    intraday_options_min_open_interest=1000,
                ),
            ),
            log_event=Mock(),
            place_order=Mock(
                return_value=SimpleNamespace(
                    filled_quantity=150,
                    average_price=100.0,
                    order_id="TEST-2",
                )
            ),
            trade_book=[],
            trade_store=None,
        )
        candidate = {
            "symbol": "NFO:TESTCE",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.8,
            "latest_close": 100.0,
            "atr": 50.0,
            "analytics": {
                "underlying": "NIFTY",
                "underlying_price": 24000.0,
                "option_type": "CE",
            },
            "trade_identity": "NIFTY",
            "asset_class": "INTRADAY_OPTIONS",
        }
        actual_targets = {
            "asset_class": "INTRADAY_OPTIONS",
            "risk_profile": "BALANCED",
            "stop_loss": 90.0,
            "target": 120.0,
            "trailing_stop": 92.0,
            "min_breakeven_price": 101.0,
            "expected_costs": 25.0,
            "expected_net_profit": 100.0,
            "cost_to_profit_ratio": 0.2,
            "cost_breakdown": {},
            "multi_level_targets": [110.0, 118.0, 128.0],
        }

        with patch("orchestration.session.get_contract_lot_size", return_value=75), \
             patch("engines.intraday_options.get_contract_lot_size", return_value=75), \
             patch("engines.options_equity.get_contract_lot_size", return_value=75), \
             patch("orchestration.session.should_enter_trade", return_value=True), \
             patch("orchestration.session.calculate_cost_aware_targets", return_value=actual_targets), \
             patch("orchestration.session.persist_runtime_state"):
            entered = _execute_single_entry(
                context,
                candidate,
                now=SimpleNamespace(isoformat=lambda: "2026-05-18T10:00:00"),
                deployed_capital=0.0,
                cycle_state={},
            )

        self.assertTrue(entered)
        self.assertEqual(context.place_order.call_args.args[1], 150)
        self.assertEqual(context.positions["NFO:TESTCE"]["quantity"], 150)

    def test_intraday_options_one_lot_mode_overrides_capital_based_qty(self) -> None:
        engine = IntradayOptionsEngine(sl_percent=10.0, target_percent=20.0, trailing_percent=7.5)
        context = SimpleNamespace(
            config=SimpleNamespace(
                capital=100000.0,
                risk_percent=0.01,
                atr_stop_multiplier=2.0,
                trailing_atr_multiplier=1.25,
                target_risk_reward=2.0,
                risk_style_name="BALANCED",
                max_open_positions=1,
                max_capital_per_trade=100000.0,
                max_capital_deployed=100000.0,
                one_trade_per_symbol_per_day=True,
                execution_mode="PAPER",
                atm_option_config={
                    "underlying": "NIFTY",
                    "expiry": "2026-05-19",
                    "strike_offset_mode": "ATM",
                },
                intraday_options_lot_mode="ONE_LOT",
            ),
            engine=engine,
            positions={},
            traded_symbols_today=set(),
            trade_counts_today={},
            last_entry_time=0.0,
            runtime_config=SimpleNamespace(
                orders=SimpleNamespace(
                    default_entry_order_type="MARKET",
                    entry_limit_price_buffer_pct=0.0,
                ),
                fno=SimpleNamespace(
                    intraday_options_max_entry_cost_ratio=0.30,
                    intraday_options_max_spread_pct=0.02,
                    intraday_options_min_open_interest=1000,
                ),
            ),
            log_event=Mock(),
            place_order=Mock(
                return_value=SimpleNamespace(
                    filled_quantity=75,
                    average_price=100.0,
                    order_id="TEST-1",
                )
            ),
            trade_book=[],
            trade_store=None,
        )
        candidate = {
            "symbol": "NFO:TESTCE",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.8,
            "latest_close": 100.0,
            "atr": 5.0,
            "analytics": {
                "underlying": "NIFTY",
                "underlying_price": 24000.0,
                "option_type": "CE",
            },
            "trade_identity": "NIFTY",
            "asset_class": "INTRADAY_OPTIONS",
        }
        actual_targets = {
            "asset_class": "INTRADAY_OPTIONS",
            "risk_profile": "BALANCED",
            "stop_loss": 90.0,
            "target": 120.0,
            "trailing_stop": 92.0,
            "min_breakeven_price": 101.0,
            "expected_costs": 25.0,
            "expected_net_profit": 100.0,
            "cost_to_profit_ratio": 0.2,
            "cost_breakdown": {},
            "multi_level_targets": [110.0, 118.0, 128.0],
        }

        with patch("orchestration.session.get_contract_lot_size", return_value=75), \
             patch("engines.intraday_options.get_contract_lot_size", return_value=75), \
             patch("engines.options_equity.get_contract_lot_size", return_value=75), \
             patch("orchestration.session.position_size", return_value=450), \
             patch("orchestration.session.apply_capital_limits_to_quantity", side_effect=lambda qty, *_args: qty), \
             patch("orchestration.session.should_enter_trade", return_value=True), \
             patch("orchestration.session.calculate_cost_aware_targets", return_value=actual_targets), \
             patch("orchestration.session.persist_runtime_state"):
            entered = _execute_single_entry(
                context,
                candidate,
                now=SimpleNamespace(isoformat=lambda: "2026-05-18T10:00:00"),
                deployed_capital=0.0,
                cycle_state={},
            )

        self.assertTrue(entered)
        self.assertEqual(context.place_order.call_args.args[1], 75)
        self.assertEqual(context.positions["NFO:TESTCE"]["quantity"], 75)

    def test_intraday_options_live_entry_blocks_wide_spread(self) -> None:
        engine = IntradayOptionsEngine(sl_percent=10.0, target_percent=20.0, trailing_percent=7.5)
        context = SimpleNamespace(
            config=SimpleNamespace(
                capital=100000.0,
                risk_percent=0.01,
                atr_stop_multiplier=2.0,
                trailing_atr_multiplier=1.25,
                target_risk_reward=2.0,
                risk_style_name="BALANCED",
                max_open_positions=1,
                max_capital_per_trade=100000.0,
                max_capital_deployed=100000.0,
                one_trade_per_symbol_per_day=True,
                execution_mode="LIVE",
                execution_provider="KITE",
                atm_option_config={
                    "underlying": "NIFTY",
                    "expiry": "2026-05-19",
                    "strike_offset_mode": "ATM",
                },
                intraday_options_lot_mode="ONE_LOT",
            ),
            engine=engine,
            positions={},
            traded_symbols_today=set(),
            trade_counts_today={},
            last_entry_time=0.0,
            runtime_config=SimpleNamespace(
                orders=SimpleNamespace(
                    default_entry_order_type="MARKET",
                    entry_limit_price_buffer_pct=0.0,
                    margin_check_enabled=True,
                    margin_buffer_pct=0.05,
                ),
                fno=SimpleNamespace(
                    intraday_options_max_entry_cost_ratio=0.30,
                    intraday_options_max_spread_pct=0.02,
                    intraday_options_min_open_interest=1000,
                ),
            ),
            log_event=Mock(),
            place_order=Mock(),
            trade_book=[],
            trade_store=None,
        )
        candidate = {
            "symbol": "NFO:TESTCE",
            "signal": "BUY",
            "agreement_count": 1,
            "score": 0.8,
            "latest_close": 100.0,
            "atr": 5.0,
            "analytics": {
                "underlying": "NIFTY",
                "underlying_price": 24000.0,
                "option_type": "CE",
            },
            "trade_identity": "NIFTY",
            "asset_class": "INTRADAY_OPTIONS",
        }

        with patch("orchestration.session.get_contract_lot_size", return_value=75), \
             patch("engines.intraday_options.get_contract_lot_size", return_value=75), \
             patch("engines.options_equity.get_contract_lot_size", return_value=75), \
             patch("orchestration.session.should_enter_trade", return_value=True), \
             patch("orchestration.session.calculate_cost_aware_targets", return_value={
                 "asset_class": "INTRADAY_OPTIONS",
                 "risk_profile": "BALANCED",
                 "stop_loss": 90.0,
                 "target": 120.0,
                 "trailing_stop": 92.0,
                 "min_breakeven_price": 101.0,
                 "expected_costs": 25.0,
                 "expected_net_profit": 100.0,
                 "cost_to_profit_ratio": 0.2,
                 "cost_breakdown": {},
                 "multi_level_targets": [110.0, 118.0, 128.0],
             }), \
             patch("orchestration.session.get_quote", return_value=SimpleNamespace(spread_pct=0.03, open_interest=5000)), \
             patch("orchestration.session.get_available_margin", return_value=500000.0), \
             patch("orchestration.session.persist_runtime_state"):
            entered = _execute_single_entry(
                context,
                candidate,
                now=SimpleNamespace(isoformat=lambda: "2026-05-18T10:00:00"),
                deployed_capital=0.0,
                cycle_state={},
            )

        self.assertFalse(entered)
        context.place_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
