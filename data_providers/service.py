from __future__ import annotations

import time
from typing import Any

from config import get_default_data_provider
from logger import get_logger

class MarketDataService:
    def __init__(
        self,
        providers: dict[str, Any],
        active_provider: str | None = None,
        logger: Any = None,
    ) -> None:
        self.providers = {
            str(name).upper(): provider
            for name, provider in providers.items()
        }
        self.logger = logger or get_logger()
        self.active_provider = (active_provider or get_default_data_provider() or "YFINANCE").upper()

    def set_active_provider(self, provider: str) -> None:
        self.active_provider = (provider or "YFINANCE").upper()

    def get_active_provider(self) -> str:
        return self.active_provider

    def get_provider(self, provider: str | None = None) -> Any:
        resolved = (provider or self.active_provider or "YFINANCE").upper()
        selected = self.providers.get(resolved)
        if selected is None:
            raise ValueError(f"Unsupported data provider: {resolved}")
        return selected

    def get_data(
        self,
        symbol: str,
        period: str = "1d",
        interval: str = "1m",
        provider: str | None = None,
    ) -> Any:
        provider_instance = self.get_provider(provider)
        active_provider = provider_instance.name
        self.logger.info(
            "[DATA] Provider=%s | Symbol=%s | period=%s | interval=%s",
            active_provider,
            symbol,
            period,
            interval,
        )
        print(
            f"\n[DATA] Provider={active_provider} | Fetching {symbol} "
            f"(period={period}, interval={interval})..."
        )

        fetch_started_at = time.time()
        try:
            data = provider_instance.fetch(symbol, period=period, interval=interval)
        except Exception as exc:
            elapsed = time.time() - fetch_started_at
            message = (
                f"[DATA ERROR] Provider={active_provider} | Symbol={symbol} | "
                f"period={period} | interval={interval} | "
                f"elapsed={elapsed:.2f}s | {type(exc).__name__}: {exc}"
            )
            print(message)
            self.logger.exception(message)
            raise

        elapsed = time.time() - fetch_started_at
        print(f"[DATA] {symbol} fetch completed in {elapsed:.2f}s")
        self.logger.info("[DATA] %s fetch completed in %.2fs", symbol, elapsed)
        print(f"[DATA] {symbol} rows fetched: {len(data)}")
        self.logger.info("[DATA] %s rows fetched: %s", symbol, len(data))

        if not data.empty:
            print(f"[DATA] {symbol} last candle:")
            print(data.tail(1))
            self.logger.info("[DATA] %s last candle:\n%s", symbol, data.tail(1))
        else:
            self.logger.warning(
                "[DATA WARNING] Provider=%s | Symbol=%s returned 0 rows for period=%s interval=%s",
                active_provider,
                symbol,
                period,
                interval,
            )

        return data


def build_default_market_data_service() -> MarketDataService:
    from .kite_provider import KiteDataProvider
    from .upstox_provider import UpstoxDataProvider
    from .yfinance_provider import YFinanceDataProvider

    providers = {
        "YFINANCE": YFinanceDataProvider(),
        "KITE": KiteDataProvider(),
        "UPSTOX": UpstoxDataProvider(),
    }
    return MarketDataService(providers=providers)
