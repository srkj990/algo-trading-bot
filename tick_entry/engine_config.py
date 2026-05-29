"""
Per-engine tick-entry configuration.

timeout_seconds  – how long to watch for a trigger within one cycle.
                   Must be < engine.sleep_seconds to avoid overlapping cycles.
poll_interval    – REST LTP poll cadence (seconds) when WebSocket unavailable.
enabled          – delivery_equity uses daily candles; no intra-candle benefit.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineTickConfig:
    enabled: bool
    timeout_seconds: float   # watch window per cycle
    poll_interval: float     # seconds between REST LTP calls


# Keyed by engine.name
TICK_ENTRY_CONFIG: dict[str, EngineTickConfig] = {
    # 1m candles, 60s cycle → watch 52s, poll every 8s (≈6 LTP checks)
    "intraday_equity": EngineTickConfig(enabled=True, timeout_seconds=52, poll_interval=8),

    # 1m candles, 15s cycle → watch 11s, poll every 3s (≈3 LTP checks)
    # require_closed_signal_candle=True makes this especially valuable
    "intraday_options": EngineTickConfig(enabled=True, timeout_seconds=11, poll_interval=3),

    # 3m candles, 60s cycle → watch 52s, poll every 8s
    "intraday_futures": EngineTickConfig(enabled=True, timeout_seconds=52, poll_interval=8),

    # 5m candles, 60s cycle → watch 52s, poll every 10s
    "futures_equity": EngineTickConfig(enabled=True, timeout_seconds=52, poll_interval=10),

    # 15m candles, 60s cycle → watch 52s, poll every 10s
    "options_equity": EngineTickConfig(enabled=True, timeout_seconds=52, poll_interval=10),

    # Daily candles, 300s cycle → tick entry adds no value on daily bars
    "delivery_equity": EngineTickConfig(enabled=False, timeout_seconds=0, poll_interval=0),
}


def get_engine_tick_config(engine_name: str) -> EngineTickConfig:
    return TICK_ENTRY_CONFIG.get(engine_name, EngineTickConfig(enabled=False, timeout_seconds=0, poll_interval=0))
