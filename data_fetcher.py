from __future__ import annotations

from typing import Any

from data_providers import build_default_market_data_service
from data_providers.service import MarketDataService


_market_data_service: MarketDataService = build_default_market_data_service()


def get_data_service() -> MarketDataService:
    return _market_data_service


def set_data_provider(provider: str) -> None:
    _market_data_service.set_active_provider(provider)


def get_data_provider() -> str:
    return _market_data_service.get_active_provider()


def get_data(
    symbol: str,
    period: str = "1d",
    interval: str = "1m",
    provider: str | None = None,
    use_cache: bool = True,
) -> Any:
    return _market_data_service.get_data(
        symbol,
        period=period,
        interval=interval,
        provider=provider,
        use_cache=use_cache,
    )
