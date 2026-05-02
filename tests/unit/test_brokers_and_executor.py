from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brokers.base import OrderRequest, OrderResult, OrderStatus, Quote
from brokers.clients import KiteBrokerClient, UpstoxBrokerClient
from brokers.factory import create_broker_client
import executor


class BrokerFactoryTests(unittest.TestCase):
    def test_factory_creates_kite_client(self) -> None:
        client = create_broker_client("KITE")
        self.assertIsInstance(client, KiteBrokerClient)

    def test_factory_creates_upstox_client(self) -> None:
        client = create_broker_client("upstox")
        self.assertIsInstance(client, UpstoxBrokerClient)

    def test_factory_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            create_broker_client("INVALID")


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        executor._broker_clients.clear()
        executor.set_execution_provider("KITE")

    def test_calculate_cost_aware_targets_for_intraday_options_returns_profitable_levels(self) -> None:
        targets = executor.calculate_cost_aware_targets(
            entry_price=100.0,
            quantity=75,
            asset_class="INTRADAY_OPTIONS",
            risk_profile="BALANCED",
            signal_strength=0.85,
            side="BUY",
        )
        self.assertLess(targets["stop_loss"], 100.0)
        self.assertGreater(targets["target"], 104.0)
        self.assertEqual(len(targets["multi_level_targets"]), 3)
        self.assertGreater(targets["expected_costs"], 0.0)
        self.assertTrue(targets["is_profitable"])

    def test_calculate_cost_aware_targets_supports_short_side(self) -> None:
        targets = executor.calculate_cost_aware_targets(
            entry_price=100.0,
            quantity=50,
            asset_class="INTRADAY_EQUITY",
            risk_profile="CONSERVATIVE",
            signal_strength=0.6,
            side="SELL",
        )
        self.assertGreater(targets["stop_loss"], 100.0)
        self.assertLess(targets["target"], 100.0)
        self.assertLess(targets["trailing_stop"], targets["stop_loss"])

    def test_place_order_in_paper_mode_returns_none(self) -> None:
        executor.set_execution_mode("PAPER")
        with patch.object(executor, "_get_broker_client") as get_client:
            result = executor.place_order("BUY", 1, "SBIN.NS")
        self.assertIsNone(result)
        get_client.assert_not_called()

    def test_place_order_in_live_mode_uses_broker_client(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.9, 100.1)
        fake_client.place_order.return_value = OrderResult(
            "OID-1",
            OrderStatus.PENDING,
            requested_quantity=2,
            pending_quantity=2,
        )
        fake_client.get_order_status.return_value = OrderResult(
            "OID-1",
            OrderStatus.FILLED,
            requested_quantity=2,
            filled_quantity=2,
            pending_quantity=0,
            average_price=100.0,
        )
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            result = executor.place_order("BUY", 2, "SBIN.NS", note="Entry", product="CNC")
        self.assertEqual(result.order_id, "OID-1")
        self.assertEqual(result.filled_quantity, 2)

    def test_place_order_passes_limit_order_details(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.95, 100.05)
        fake_client.place_order.return_value = OrderResult(
            "OID-2",
            OrderStatus.PENDING,
            requested_quantity=1,
            pending_quantity=1,
        )
        fake_client.get_order_status.return_value = OrderResult(
            "OID-2",
            OrderStatus.FILLED,
            requested_quantity=1,
            filled_quantity=1,
            pending_quantity=0,
            average_price=99.8,
        )
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            executor.place_order(
                "BUY",
                1,
                "SBIN.NS",
                order_type="LIMIT",
                price=99.8,
                entry_price=99.8,
            )
        request = fake_client.place_order.call_args.args[0]
        self.assertEqual(request.order_type, "LIMIT")
        self.assertEqual(request.price, 99.8)

    def test_place_order_retries_partial_fill(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.95, 100.05)
        fake_client.place_order.side_effect = [
            OrderResult("OID-1", OrderStatus.PENDING, requested_quantity=5, pending_quantity=5),
            OrderResult("OID-2", OrderStatus.PENDING, requested_quantity=2, pending_quantity=2),
        ]
        fake_client.get_order_status.side_effect = [
            OrderResult(
                "OID-1",
                OrderStatus.PARTIAL,
                requested_quantity=5,
                filled_quantity=3,
                pending_quantity=2,
                average_price=100.0,
            ),
            OrderResult(
                "OID-2",
                OrderStatus.FILLED,
                requested_quantity=2,
                filled_quantity=2,
                pending_quantity=0,
                average_price=100.2,
            ),
        ]
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            result = executor.place_order("BUY", 5, "SBIN.NS", entry_price=100.0)
        self.assertEqual(result.filled_quantity, 5)
        self.assertEqual(fake_client.place_order.call_count, 2)

    def test_place_order_blocks_wide_spread(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 94.0, 106.0)
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            with self.assertRaises(RuntimeError):
                executor.place_order("BUY", 1, "SBIN.NS", entry_price=100.0)

    def test_place_order_blocks_when_margin_is_insufficient(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.95, 100.05)
        fake_client.get_available_margin.return_value = 50.0
        runtime_config = replace(
            executor.get_runtime_config(),
            orders=replace(executor.get_runtime_config().orders, margin_check_enabled=True),
        )
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            with self.assertRaises(RuntimeError):
                executor.place_order(
                    "BUY",
                    1,
                    "SBIN.NS",
                    entry_price=100.0,
                    runtime_config=runtime_config,
                )

    def test_place_order_retries_rejected_order_with_smaller_limit_order(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.9, 100.1)
        fake_client.get_available_margin.return_value = None
        fake_client.place_order.side_effect = [
            OrderResult("OID-10", OrderStatus.PENDING, requested_quantity=4, pending_quantity=4),
            OrderResult("OID-11", OrderStatus.PENDING, requested_quantity=3, pending_quantity=3),
        ]
        fake_client.get_order_status.side_effect = [
            OrderResult(
                "OID-10",
                OrderStatus.REJECTED,
                requested_quantity=4,
                filled_quantity=0,
                pending_quantity=0,
                message="Price band breach",
            ),
            OrderResult(
                "OID-11",
                OrderStatus.FILLED,
                requested_quantity=3,
                filled_quantity=3,
                pending_quantity=0,
                average_price=100.1,
            ),
        ]
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            result = executor.place_order("BUY", 4, "SBIN.NS", entry_price=100.0)
        self.assertEqual(result.order_id, "OID-11")
        self.assertEqual(result.filled_quantity, 3)
        retry_request = fake_client.place_order.call_args_list[1].args[0]
        self.assertEqual(retry_request.order_type, "LIMIT")
        self.assertEqual(retry_request.quantity, 3)

    def test_place_order_requires_fill_confirmation(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.9, 100.1)
        fake_client.get_available_margin.return_value = None
        fake_client.place_order.return_value = OrderResult(
            "OID-20",
            OrderStatus.PENDING,
            requested_quantity=1,
            pending_quantity=1,
        )
        fake_client.get_order_status.return_value = OrderResult(
            "OID-20",
            OrderStatus.PENDING,
            requested_quantity=1,
            pending_quantity=1,
        )
        runtime_config = replace(
            executor.get_runtime_config(),
            orders=replace(
                executor.get_runtime_config().orders,
                reconcile_attempts=1,
                fill_confirmation_required=True,
                rejection_retry_enabled=False,
                partial_fill_retry_enabled=False,
            ),
        )
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            with self.assertRaises(RuntimeError):
                executor.place_order(
                    "BUY",
                    1,
                    "SBIN.NS",
                    entry_price=100.0,
                    runtime_config=runtime_config,
                )

    def test_place_bracket_order_marks_synthetic_mode(self) -> None:
        fake_client = Mock()
        fake_client.get_quote.return_value = Quote("SBIN.NS", 100.0, 99.95, 100.05)
        fake_client.place_order.return_value = OrderResult(
            "OID-3",
            OrderStatus.PENDING,
            requested_quantity=1,
            pending_quantity=1,
        )
        fake_client.get_order_status.return_value = OrderResult(
            "OID-3",
            OrderStatus.FILLED,
            requested_quantity=1,
            filled_quantity=1,
            pending_quantity=0,
            average_price=100.0,
        )
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            result = executor.place_bracket_order(
                "BUY",
                1,
                "SBIN.NS",
                entry_price=100.0,
                stop_loss_price=97.0,
                target_price=105.0,
            )
        self.assertTrue(result.metadata["bracket_requested"])
        self.assertEqual(result.metadata["bracket_mode"], "SYNTHETIC")

    def test_get_broker_client_caches_by_provider(self) -> None:
        with patch.object(executor, "create_broker_client", return_value=Mock()) as factory:
            first = executor._get_broker_client("KITE")
            second = executor._get_broker_client("KITE")
        self.assertIs(first, second)
        factory.assert_called_once_with("KITE")

    def test_is_upstox_static_ip_blocked_detects_known_code(self) -> None:
        self.assertTrue(executor.is_upstox_static_ip_blocked("UDAPI1154 static IP mismatch"))

    def test_is_upstox_static_ip_blocked_returns_false_for_other_errors(self) -> None:
        self.assertFalse(executor.is_upstox_static_ip_blocked("something else"))


class UpstoxClientUtilityTests(unittest.TestCase):
    def test_extract_error_detail_prefers_error_list(self) -> None:
        response = SimpleNamespace(
            json=lambda: {"errors": [{"errorCode": "UDAPI", "message": "blocked"}]},
            text="ignored",
        )
        self.assertEqual(UpstoxBrokerClient.extract_error_detail(response), "UDAPI: blocked")

    def test_extract_error_detail_uses_message_when_no_errors(self) -> None:
        response = SimpleNamespace(json=lambda: {"message": "plain message"}, text="ignored")
        self.assertEqual(UpstoxBrokerClient.extract_error_detail(response), "plain message")

    def test_extract_error_detail_falls_back_to_text(self) -> None:
        response = SimpleNamespace(json=Mock(side_effect=ValueError("bad")), text="raw text")
        self.assertEqual(UpstoxBrokerClient.extract_error_detail(response), "raw text")

    def test_extract_ip_addresses_finds_ipv4_and_ipv6_without_duplicates(self) -> None:
        text = "Use 49.205.247.48 or 2401:4900:1234:abcd::1 and 49.205.247.48 again"
        ips = UpstoxBrokerClient.extract_ip_addresses(text)
        self.assertEqual(ips, ["49.205.247.48", "2401:4900:1234:abcd::1"])

    def test_format_ip_diagnostics_includes_all_present_values(self) -> None:
        message = UpstoxBrokerClient.format_ip_diagnostics(
            "1.1.1.1",
            "2.2.2.2",
            "2001:db8::1",
            ["2.2.2.2", "3.3.3.3"],
        )
        self.assertIn("broker outbound public IPv4: 1.1.1.1", message)
        self.assertIn("configured Upstox static IP: 2.2.2.2", message)
        self.assertIn("other IP(s) mentioned by broker: 3.3.3.3", message)

    def test_format_ip_diagnostics_returns_empty_string_when_no_values(self) -> None:
        self.assertEqual(UpstoxBrokerClient.format_ip_diagnostics(None, None, None, []), "")

    def test_get_product_constant_maps_known_values(self) -> None:
        client = UpstoxBrokerClient()
        self.assertEqual(client._product_constant("MIS"), "I")
        self.assertEqual(client._product_constant("CNC"), "D")
        self.assertEqual(client._product_constant("NRML"), "D")

    def test_get_product_constant_defaults_to_intraday(self) -> None:
        client = UpstoxBrokerClient()
        self.assertEqual(client._product_constant("UNKNOWN"), "I")

    def test_kite_parse_symbol_exchange_defaults_to_nse(self) -> None:
        exchange, tradingsymbol = KiteBrokerClient._parse_symbol_exchange("SBIN.NS")
        self.assertEqual((exchange, tradingsymbol), ("NSE", "SBIN"))

    def test_kite_parse_symbol_exchange_supports_prefixed_symbols(self) -> None:
        exchange, tradingsymbol = KiteBrokerClient._parse_symbol_exchange("NFO:NIFTY24APR24500CE")
        self.assertEqual((exchange, tradingsymbol), ("NFO", "NIFTY24APR24500CE"))

    def test_kite_parse_symbol_exchange_rejects_empty_symbol(self) -> None:
        with self.assertRaises(ValueError):
            KiteBrokerClient._parse_symbol_exchange("")

    def test_kite_product_constant_uses_client_mapping(self) -> None:
        fake_client = SimpleNamespace(PRODUCT_MIS="MIS_CONST", PRODUCT_CNC="CNC_CONST", PRODUCT_NRML="NRML_CONST")
        client = KiteBrokerClient()
        with patch.object(client, "_get_client", return_value=fake_client):
            self.assertEqual(client._product_constant("CNC"), "CNC_CONST")


if __name__ == "__main__":
    unittest.main()
