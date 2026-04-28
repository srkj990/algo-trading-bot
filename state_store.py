from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict


STATE_DIR = Path("state")


class EngineState(TypedDict):
    positions: dict[str, dict[str, Any]]
    traded_symbols_today: list[str]
    trade_counts_today: dict[str, int]
    active_trade_day: str
    last_entry_time: float
    regime_cache: dict[str, Any]
    engine_runtime_state: dict[str, Any]


def _default_engine_state() -> EngineState:
    return {
        "positions": {},
        "traded_symbols_today": [],
        "trade_counts_today": {},
        "active_trade_day": datetime.now().date().isoformat(),
        "last_entry_time": 0,
        "regime_cache": {},
        "engine_runtime_state": {},
    }


def _state_path(engine_name: str) -> Path:
    return STATE_DIR / f"{engine_name.lower()}_state.json"


def load_engine_state(engine_name: str) -> EngineState:
    path = _state_path(engine_name)
    if not path.exists():
        return _default_engine_state()

    with open(path, encoding="utf-8") as state_file:
        data: EngineState = json.load(state_file)

    data.setdefault("positions", {})
    data.setdefault("traded_symbols_today", [])
    data.setdefault("trade_counts_today", {})
    data.setdefault("active_trade_day", datetime.now().date().isoformat())
    data.setdefault("last_entry_time", 0)
    data.setdefault("regime_cache", {})
    data.setdefault("engine_runtime_state", {})
    return data


def save_engine_state(
    engine_name: str,
    positions: dict[str, dict[str, Any]],
    traded_symbols_today: set[str] | list[str],
    trade_counts_today: dict[str, int],
    active_trade_day: Any,
    last_entry_time: float,
    regime_cache: dict[str, Any],
    engine_runtime_state: dict[str, Any] | None = None,
) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path = _state_path(engine_name)
    payload: EngineState = {
        "positions": positions,
        "traded_symbols_today": sorted(traded_symbols_today),
        "trade_counts_today": trade_counts_today,
        "active_trade_day": active_trade_day.isoformat(),
        "last_entry_time": last_entry_time,
        "regime_cache": regime_cache,
        "engine_runtime_state": engine_runtime_state or {},
    }

    with open(path, "w", encoding="utf-8") as state_file:
        json.dump(payload, state_file, indent=2, sort_keys=True)
