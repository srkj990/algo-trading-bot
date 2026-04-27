from __future__ import annotations

from brokers.clients import KiteBrokerClient, UpstoxBrokerClient


def create_broker_client(provider: str):
    normalized = (provider or "KITE").upper()
    if normalized == "KITE":
        return KiteBrokerClient()
    if normalized == "UPSTOX":
        return UpstoxBrokerClient()
    raise ValueError(f"Unsupported execution provider: {provider}")
