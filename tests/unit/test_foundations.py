import unittest

from brokers.base import BrokerClient, OrderRequest, OrderResult, OrderStatus, Quote
from engines.common import build_position, evaluate_exit, update_trailing_stop
from models.position import ExitReason, Position, PositionSide


class PositionModelTests(unittest.TestCase):
    def test_position_defaults_best_price_to_entry(self):
        position = Position(
            symbol="SBIN.NS",
            side=PositionSide.BUY,
            quantity=10,
            entry_price=100.0,
            stop_loss=95.0,
            target=110.0,
            trailing_stop=96.0,
        )
        self.assertEqual(position.best_price, 100.0)

    def test_position_rejects_non_positive_quantity(self):
        with self.assertRaises(ValueError):
            Position(
                symbol="SBIN.NS",
                side=PositionSide.BUY,
                quantity=0,
                entry_price=100.0,
                stop_loss=95.0,
                target=110.0,
                trailing_stop=96.0,
            )

    def test_unrealized_pnl_for_long_position_uses_best_price(self):
        position = Position(
            symbol="SBIN.NS",
            side=PositionSide.BUY,
            quantity=5,
            entry_price=100.0,
            stop_loss=95.0,
            target=110.0,
            trailing_stop=96.0,
            best_price=108.0,
        )
        self.assertEqual(position.unrealized_pnl, 40.0)

    def test_unrealized_pnl_for_short_position_uses_best_price(self):
        position = Position(
            symbol="SBIN.NS",
            side=PositionSide.SELL,
            quantity=5,
            entry_price=100.0,
            stop_loss=105.0,
            target=90.0,
            trailing_stop=104.0,
            best_price=92.0,
        )
        self.assertEqual(position.unrealized_pnl, 40.0)

    def test_evaluate_exit_returns_stop_loss_for_long(self):
        position = Position(
            symbol="SBIN.NS",
            side=PositionSide.BUY,
            quantity=5,
            entry_price=100.0,
            stop_loss=95.0,
            target=110.0,
            trailing_stop=96.0,
        )
        self.assertEqual(
            position.evaluate_exit(latest_high=101.0, latest_low=94.0),
            ExitReason.STOP_LOSS,
        )

    def test_evaluate_exit_returns_target_for_short(self):
        position = Position(
            symbol="SBIN.NS",
            side=PositionSide.SELL,
            quantity=5,
            entry_price=100.0,
            stop_loss=105.0,
            target=90.0,
            trailing_stop=104.0,
        )
        self.assertEqual(
            position.evaluate_exit(latest_high=101.0, latest_low=89.0),
            ExitReason.TARGET,
        )

    def test_update_trailing_stop_advances_for_long_position(self):
        position = Position(
            symbol="SBIN.NS",
            side=PositionSide.BUY,
            quantity=5,
            entry_price=100.0,
            stop_loss=95.0,
            target=110.0,
            trailing_stop=96.0,
            trailing_distance=3.0,
        )
        changed = position.update_trailing_stop(latest_close=106.0, trailing_pct=0.0)
        self.assertTrue(changed)
        self.assertEqual(position.best_price, 106.0)
        self.assertEqual(position.trailing_stop, 103.0)

    def test_build_position_returns_validated_legacy_dict_with_extras(self):
        built = build_position(
            symbol="SBIN.NS",
            side="BUY",
            quantity=5,
            entry_price=100.0,
            sl_pct=5.0,
            target_pct=10.0,
            trailing_pct=4.0,
            pair_id="PAIR-1",
        )
        self.assertEqual(built["side"], "BUY")
        self.assertEqual(built["stop_loss"], 95.0)
        self.assertAlmostEqual(built["target"], 110.0)
        self.assertEqual(built["pair_id"], "PAIR-1")

    def test_common_update_trailing_stop_mutates_legacy_mapping(self):
        position = build_position(
            symbol="SBIN.NS",
            side="SELL",
            quantity=5,
            entry_price=100.0,
            sl_pct=5.0,
            target_pct=10.0,
            trailing_pct=4.0,
            trailing_distance=2.0,
        )
        changed = update_trailing_stop(position, latest_close=94.0, trailing_pct=0.0)
        self.assertTrue(changed)
        self.assertEqual(position["best_price"], 94.0)
        self.assertEqual(position["trailing_stop"], 96.0)

    def test_common_evaluate_exit_returns_string_reason_for_legacy_mapping(self):
        position = build_position(
            symbol="SBIN.NS",
            side="BUY",
            quantity=5,
            entry_price=100.0,
            sl_pct=5.0,
            target_pct=10.0,
            trailing_pct=4.0,
        )
        reason = evaluate_exit(position, {"High": 111.0, "Low": 100.0}, include_target=True)
        self.assertEqual(reason, "TARGET")


class BrokerInterfaceTests(unittest.TestCase):
    def test_broker_client_cannot_be_instantiated_without_implementation(self):
        with self.assertRaises(TypeError):
            BrokerClient()

    def test_minimal_broker_implementation_matches_contract(self):
        class DummyBroker(BrokerClient):
            def place_order(self, order: OrderRequest) -> OrderResult:
                return OrderResult(order_id="OID-1", status=OrderStatus.PENDING)

            def get_positions(self):
                return []

            def get_quote(self, symbol: str) -> Quote:
                return Quote(symbol=symbol, last_price=100.0)

            def get_intraday_positions(self):
                return []

            def get_delivery_holdings(self):
                return []

            def get_nfo_positions(self):
                return []

            def cancel_order(self, order_id: str) -> bool:
                return True

        broker = DummyBroker()
        result = broker.place_order(OrderRequest(symbol="SBIN.NS", side="BUY", quantity=1))
        quote = broker.get_quote("SBIN.NS")
        self.assertEqual(result.status, OrderStatus.PENDING)
        self.assertEqual(quote.last_price, 100.0)

    def test_quote_spread_helpers(self):
        quote = Quote(symbol="SBIN.NS", last_price=100.0, bid_price=99.0, ask_price=101.0)
        self.assertEqual(quote.spread, 2.0)
        self.assertEqual(quote.spread_pct, 0.02)


if __name__ == "__main__":
    unittest.main()
