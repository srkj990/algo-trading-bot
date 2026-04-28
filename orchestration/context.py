from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from cli.configuration import SessionConfig
from data_fetcher import get_data, get_data_service
from data_providers.service import MarketDataService
from engines.base import TradingEngine
from executor import place_order
from logger import get_logger, log_event
from reporting import export_trade_book_report
from state_store import EngineState, load_engine_state, save_engine_state

from .positions import parse_trade_day, save_runtime_state


@dataclass
class TradingContext:
    config: SessionConfig
    engine: TradingEngine
    data_service: MarketDataService
    fetch_data: Callable[..., Any]
    place_order: Callable[..., Any]
    log_event: Callable[..., Any]
    logger: Any
    save_engine_state: Callable[..., Any]
    export_trade_book_report: Callable[..., Any]
    positions: dict[str, dict[str, Any]]
    traded_symbols_today: set[str]
    trade_counts_today: dict[str, int]
    active_trade_day: date
    last_entry_time: float
    regime_cache: dict[str, Any]
    trade_book: list[dict[str, Any]] = field(default_factory=list)
    previous_cycle_started_at: datetime | None = None


def build_trading_context(config: SessionConfig) -> TradingContext:
    saved_state: EngineState = load_engine_state(config.engine.name)
    if hasattr(config.engine, "hydrate_runtime_state"):
        config.engine.hydrate_runtime_state(saved_state)
    positions = config.engine.reconcile_startup(
        execution_mode=config.execution_mode,
        persisted_positions=saved_state["positions"],
    )
    context = TradingContext(
        config=config,
        engine=config.engine,
        data_service=get_data_service(),
        fetch_data=get_data,
        place_order=place_order,
        log_event=log_event,
        logger=get_logger(),
        save_engine_state=save_engine_state,
        export_trade_book_report=export_trade_book_report,
        positions=positions,
        traded_symbols_today=set(saved_state["traded_symbols_today"]),
        trade_counts_today={
            str(key): int(value)
            for key, value in saved_state.get("trade_counts_today", {}).items()
        },
        active_trade_day=parse_trade_day(saved_state["active_trade_day"]),
        last_entry_time=float(saved_state["last_entry_time"]),
        regime_cache=saved_state["regime_cache"],
    )
    persist_runtime_state(context)
    return context


def persist_runtime_state(context: TradingContext) -> None:
    engine_runtime_state = {}
    if hasattr(context.engine, "export_runtime_state"):
        engine_runtime_state = context.engine.export_runtime_state()
    save_runtime_state(
        context.engine.name,
        context.positions,
        context.traded_symbols_today,
        context.trade_counts_today,
        context.active_trade_day,
        context.last_entry_time,
        context.regime_cache,
        engine_runtime_state,
        context.save_engine_state,
    )
