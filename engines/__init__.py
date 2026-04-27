from importlib import import_module

from .base import TradingEngine

__all__ = [
    "TradingEngine",
    "DeliveryEquityEngine",
    "FuturesEquityEngine",
    "IntradayFuturesEngine",
    "IntradayOptionsEngine",
    "IntradayEquityEngine",
    "OptionsEquityEngine",
]

_ENGINE_IMPORTS = {
    "DeliveryEquityEngine": ".delivery_equity",
    "FuturesEquityEngine": ".futures_equity",
    "IntradayFuturesEngine": ".intraday_futures",
    "IntradayOptionsEngine": ".intraday_options",
    "IntradayEquityEngine": ".intraday_equity",
    "OptionsEquityEngine": ".options_equity",
}


def __getattr__(name: str):
    module_name = _ENGINE_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'engines' has no attribute {name!r}")
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
