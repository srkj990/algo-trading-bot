import json
from datetime import datetime
from pathlib import Path


STATE_DIR = Path("state")


def _state_path(engine_name):
    return STATE_DIR / f"{engine_name.lower()}_state.json"


def load_engine_state(engine_name):
    path = _state_path(engine_name)
    if not path.exists():
        return {
            "positions": {},
            "traded_symbols_today": [],
            "active_trade_day": datetime.now().date().isoformat(),
            "last_entry_time": 0,
            "regime_cache": {},
        }

    with open(path, encoding="utf-8") as state_file:
        data = json.load(state_file)

    data.setdefault("positions", {})
    data.setdefault("traded_symbols_today", [])
    data.setdefault("active_trade_day", datetime.now().date().isoformat())
    data.setdefault("last_entry_time", 0)
    data.setdefault("regime_cache", {})
    return data


def save_engine_state(
    engine_name,
    positions,
    traded_symbols_today,
    active_trade_day,
    last_entry_time,
    regime_cache,
):
    STATE_DIR.mkdir(exist_ok=True)
    path = _state_path(engine_name)
    payload = {
        "positions": positions,
        "traded_symbols_today": sorted(traded_symbols_today),
        "active_trade_day": active_trade_day.isoformat(),
        "last_entry_time": last_entry_time,
        "regime_cache": regime_cache,
    }

    with open(path, "w", encoding="utf-8") as state_file:
        json.dump(payload, state_file, indent=2, sort_keys=True)
