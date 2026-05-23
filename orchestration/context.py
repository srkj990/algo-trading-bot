from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from cli.configuration import SessionConfig
from config import RuntimeConfig, get_runtime_config
from data_fetcher import get_data_service
from data_providers.service import MarketDataService
from engines.base import TradingEngine
from executor import place_order
from logger import get_logger, log_event
from trade_store import TradeStore
from reporting import export_trade_book_report
from state_store import EngineState, load_engine_state, save_engine_state

from .positions import parse_trade_day, save_runtime_state


@dataclass
class TradingContext:
    config: SessionConfig
    engine: TradingEngine
    runtime_config: RuntimeConfig
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
    session_runtime_state: dict[str, Any]
    trade_store: TradeStore
    cycle_data_cache: dict[tuple[str, str, str, str], Any] = field(default_factory=dict)
    trade_book: list[dict[str, Any]] = field(default_factory=list)
    recent_live_order_timestamps: list[float] = field(default_factory=list)
    previous_cycle_started_at: datetime | None = None


def build_trading_context(config: SessionConfig) -> TradingContext:
    saved_state: EngineState = load_engine_state(config.engine.name)
    runtime_config = get_runtime_config()
    data_service = get_data_service()
    trade_store = TradeStore(config.engine.name, config.execution_mode)
    if hasattr(config.engine, "hydrate_runtime_state"):
        config.engine.hydrate_runtime_state(saved_state)
    positions = config.engine.reconcile_startup(
        execution_mode=config.execution_mode,
        persisted_positions=saved_state["positions"],
    )
    context: TradingContext
    context = TradingContext(
        config=config,
        engine=config.engine,
        runtime_config=runtime_config,
        data_service=data_service,
        fetch_data=lambda *args, **kwargs: None,
        place_order=lambda *args, **kwargs: None,
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
        session_runtime_state=dict(saved_state.get("session_runtime_state", {})),
        trade_store=trade_store,
        trade_book=trade_store.load_trade_book(),
    )
    context.fetch_data = _build_context_fetcher(context)
    context.place_order = _build_context_order_placer(context)
    persist_runtime_state(context)
    return context


def _build_context_fetcher(context: TradingContext) -> Callable[..., Any]:
    def fetch_data(
        symbol: str,
        period: str = "1d",
        interval: str = "1m",
        provider: str | None = None,
        use_cache: bool = True,
    ) -> Any:
        resolved_provider = (provider or context.data_service.get_active_provider()).upper()
        cache_key = (resolved_provider, symbol, period, interval)
        if (
            use_cache
            and context.runtime_config.data_cache.per_cycle_enabled
            and cache_key in context.cycle_data_cache
        ):
            return context.data_service._clone_data(context.cycle_data_cache[cache_key])

        data = context.data_service.get_data(
            symbol,
            period=period,
            interval=interval,
            provider=provider,
            use_cache=use_cache,
        )
        if use_cache and context.runtime_config.data_cache.per_cycle_enabled:
            context.cycle_data_cache[cache_key] = context.data_service._clone_data(data)
        return data

    return fetch_data


def _build_context_order_placer(context: TradingContext) -> Callable[..., Any]:
    def submit_order(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("runtime_config", context.runtime_config)
        kwargs.setdefault("trade_store", context.trade_store)
        kwargs.setdefault("execution_provider", context.config.execution_provider)
        kwargs.setdefault("execution_mode", context.config.execution_mode)
        resolved_mode = str(kwargs.get("execution_mode") or context.config.execution_mode or "PAPER").upper()
        risk_cfg = context.runtime_config.risk_controls
        if resolved_mode == "LIVE" and int(risk_cfg.max_orders_per_minute or 0) > 0:
            cutoff = datetime.now().timestamp() - 60.0
            context.recent_live_order_timestamps = [
                ts for ts in context.recent_live_order_timestamps if float(ts) >= cutoff
            ]
            if len(context.recent_live_order_timestamps) >= int(risk_cfg.max_orders_per_minute):
                raise RuntimeError(
                    "Risk control blocked order: max_orders_per_minute limit reached."
                )

        reference_price = kwargs.get("price")
        if reference_price is None:
            reference_price = kwargs.get("entry_price")
        result = place_order(*args, **kwargs)

        if resolved_mode == "LIVE":
            context.recent_live_order_timestamps.append(datetime.now().timestamp())

        if (
            resolved_mode == "LIVE"
            and result is not None
            and int(result.filled_quantity or 0) > 0
            and reference_price is not None
            and float(reference_price) > 0
        ):
            actual_price = float(result.average_price or reference_price)
            slippage_pct = abs(actual_price - float(reference_price)) / float(reference_price) * 100.0
            max_slippage_pct = float(risk_cfg.abnormal_slippage_pause_pct or 0.0)
            if max_slippage_pct > 0 and slippage_pct >= max_slippage_pct:
                pause_minutes = int(risk_cfg.api_failure_pause_minutes or 0)
                if pause_minutes > 0:
                    pause_until = datetime.now().timestamp() + (pause_minutes * 60)
                    context.session_runtime_state["risk_pause_until"] = datetime.fromtimestamp(pause_until).isoformat()
                    context.session_runtime_state["risk_pause_reason"] = (
                        f"Abnormal slippage detected on {kwargs.get('order_type', 'MARKET')} order "
                        f"for {args[2] if len(args) > 2 else kwargs.get('symbol', 'UNKNOWN')}: "
                        f"{slippage_pct:.2f}% >= {max_slippage_pct:.2f}%"
                    )
                    persist_runtime_state(context)
                context.log_event(
                    f"[RISK] Abnormal slippage detected: {slippage_pct:.2f}% "
                    f"(threshold {max_slippage_pct:.2f}%)",
                    "warning",
                )

        return result

    return submit_order


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
        context.session_runtime_state,
        engine_runtime_state,
        context.save_engine_state,
    )
