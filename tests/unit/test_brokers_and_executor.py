from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brokers.base import OrderRequest, OrderResult, OrderStatus
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

    def test_place_order_in_paper_mode_returns_none(self) -> None:
        executor.set_execution_mode("PAPER")
        with patch.object(executor, "_get_broker_client") as get_client:
            result = executor.place_order("BUY", 1, "SBIN.NS")
        self.assertIsNone(result)
        get_client.assert_not_called()

    def test_place_order_in_live_mode_uses_broker_client(self) -> None:
        fake_client = Mock()
        fake_client.place_order.return_value = OrderResult("OID-1", OrderStatus.PENDING)
        executor.set_execution_mode("LIVE")
        with patch.object(executor, "_get_broker_client", return_value=fake_client):
            result = executor.place_order("BUY", 2, "SBIN.NS", note="Entry", product="CNC")
        self.assertEqual(result, "OID-1")

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
