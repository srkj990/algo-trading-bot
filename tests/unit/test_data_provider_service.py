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
        self.calls = 0

    def fetch(self, symbol, period="1d", interval="1m"):
        self.calls += 1
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

    def test_service_reuses_cache_for_same_request(self):
        provider = DummyProvider("KITE")
        service = MarketDataService(providers={"KITE": provider}, active_provider="KITE")
        service.get_data("SBIN.NS")
        service.get_data("SBIN.NS")
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
