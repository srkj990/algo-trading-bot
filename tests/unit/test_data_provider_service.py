import unittest

from data_providers.service import MarketDataService


class DummyFrame:
    empty = False

    def __len__(self):
        return 1

    def tail(self, count):
        return f"tail({count})"


class DummyProvider:
    def __init__(self, name):
        self.name = name

    def fetch(self, symbol, period="1d", interval="1m"):
        return DummyFrame()


class MarketDataServiceTests(unittest.TestCase):
    def test_service_uses_active_provider_and_fetches_data(self):
        service = MarketDataService(
            providers={
                "YFINANCE": DummyProvider("YFINANCE"),
                "KITE": DummyProvider("KITE"),
            },
            active_provider="KITE",
        )
        data = service.get_data("SBIN.NS")
        self.assertFalse(data.empty)
        self.assertEqual(service.get_active_provider(), "KITE")

    def test_service_rejects_unknown_provider(self):
        service = MarketDataService(providers={"YFINANCE": DummyProvider("YFINANCE")})
        with self.assertRaises(ValueError):
            service.get_provider("MISSING")


if __name__ == "__main__":
    unittest.main()
