from __future__ import annotations

import math
import time
from typing import Any

from brokers.base import BrokerClient, OrderRequest, OrderResult, OrderStatus
from brokers.clients import UpstoxBrokerClient
from brokers.factory import create_broker_client
from config import (
    ASSET_CLASS_RISK_PROFILES,
    RuntimeConfig,
    TRANSACTION_SLIPPAGE_PCT_PER_SIDE,
    get_broker_ip_mode,
    get_default_execution_provider,
    get_runtime_config,
)
from logger import log_event
from models.trade_record import OrderAuditRecord
from trade_store import TradeStore
from transaction_costs import (
    estimate_delivery_equity_round_trip_cost,
    estimate_futures_round_trip_cost,
    estimate_intraday_equity_round_trip_cost,
    estimate_options_round_trip_cost,
)


EXECUTION_MODE = "PAPER"
EXECUTION_PROVIDER = get_default_execution_provider()
_broker_clients: dict[str, BrokerClient] = {}


def _resolve_cost_model(asset_class: str) -> Any:
    normalized_asset_class = str(asset_class or "").upper()
    if normalized_asset_class == "INTRADAY_OPTIONS":
        return estimate_options_round_trip_cost
    if normalized_asset_class == "OPTIONS_EQUITY":
        return estimate_options_round_trip_cost
    if normalized_asset_class == "DELIVERY_EQUITY":
        return estimate_delivery_equity_round_trip_cost
    if normalized_asset_class in {"FUTURES_EQUITY", "INTRADAY_FUTURES"}:
        return estimate_futures_round_trip_cost
    return estimate_intraday_equity_round_trip_cost


def calculate_cost_aware_targets(
    entry_price: float,
    quantity: int,
    asset_class: str,
    risk_profile: str,
    signal_strength: float = 1.0,
    side: str = "BUY",
    slippage_pct_per_side: float | None = None,
) -> dict[str, Any]:
    config = ASSET_CLASS_RISK_PROFILES[str(asset_class).upper()][str(risk_profile).upper()]
    normalized_side = str(side or "BUY").upper()
    clamped_strength = min(1.0, max(0.0, float(signal_strength or 0.0)))
    sl_pct = float(config["sl_percent"])
    target_pct = float(config["target_percent"])
    trailing_pct = float(config["trailing_percent"])
    min_breakeven_move = float(config.get("min_breakeven_move", 0.0))
    target_levels = [float(level) for level in config.get("multi_level_targets", [])]
    cost_model = _resolve_cost_model(asset_class)
    effective_slippage = (
        float(TRANSACTION_SLIPPAGE_PCT_PER_SIDE)
        if slippage_pct_per_side is None
        else float(slippage_pct_per_side)
    )

    direction = 1.0 if normalized_side == "BUY" else -1.0
    stop_loss = float(entry_price) * (1.0 - (direction * sl_pct / 100.0))
    initial_target = float(entry_price) * (1.0 + (direction * target_pct / 100.0))
    initial_costs = cost_model(
        entry_side=normalized_side,
        entry_price=float(entry_price),
        exit_price=float(initial_target),
        quantity=int(quantity),
        slippage_pct_per_side=effective_slippage,
    )

    gross_profit_per_unit = abs(float(initial_target) - float(entry_price))
    gross_profit_total = gross_profit_per_unit * int(quantity)
    net_profit = gross_profit_total - float(initial_costs.total)

    adjusted_target_pct = target_pct
    if net_profit < 0:
        breakeven_pct = max(
            min_breakeven_move,
            ((float(initial_costs.total) / max(int(quantity), 1)) / float(entry_price)) * 100.0,
        )
        adjusted_target_pct = max(target_pct, breakeven_pct * 1.5)

    strength_multiplier = 1.0
    if clamped_strength < 0.5:
        strength_multiplier = 0.8
    elif clamped_strength > 0.8:
        strength_multiplier = 1.2
    adjusted_target_pct *= strength_multiplier
    adjusted_target = float(entry_price) * (1.0 + (direction * adjusted_target_pct / 100.0))

    actual_costs = cost_model(
        entry_side=normalized_side,
        entry_price=float(entry_price),
        exit_price=float(adjusted_target),
        quantity=int(quantity),
        slippage_pct_per_side=effective_slippage,
    )

    expected_profit = abs(float(adjusted_target) - float(entry_price)) * int(quantity)
    net_profit_actual = expected_profit - float(actual_costs.total)
    trailing_stop = float(entry_price) * (1.0 - (direction * trailing_pct / 100.0))
    min_breakeven_price = float(entry_price) * (1.0 + (direction * min_breakeven_move / 100.0))

    multi_level_target_prices = [
        float(entry_price) * (1.0 + (direction * level_pct / 100.0))
        for level_pct in target_levels
    ]

    return {
        "entry_price": float(entry_price),
        "side": normalized_side,
        "asset_class": str(asset_class).upper(),
        "risk_profile": str(risk_profile).upper(),
        "signal_strength": clamped_strength,
        "stop_loss": stop_loss,
        "target": adjusted_target,
        "trailing_stop": trailing_stop,
        "min_breakeven_price": min_breakeven_price,
        "expected_gross_profit": expected_profit,
        "expected_costs": float(actual_costs.total),
        "expected_net_profit": net_profit_actual,
        "cost_to_profit_ratio": (
            float(actual_costs.total) / expected_profit if expected_profit > 0 else 0.0
        ),
        "is_profitable": net_profit_actual > 0,
        "multi_level_targets": multi_level_target_prices,
        "cost_breakdown": {
            "brokerage": float(actual_costs.brokerage),
            "stt": float(actual_costs.stt),
            "exchange": float(actual_costs.exchange_txn),
            "sebi": float(actual_costs.sebi),
            "stamp": float(actual_costs.stamp),
            "gst": float(actual_costs.gst),
            "slippage": float(actual_costs.slippage),
        },
    }


def set_execution_mode(mode: str) -> None:
    global EXECUTION_MODE
    EXECUTION_MODE = mode.upper()


def set_execution_provider(provider: str | None) -> None:
    global EXECUTION_PROVIDER
    EXECUTION_PROVIDER = (provider or "KITE").upper()


def get_execution_provider() -> str:
    return EXECUTION_PROVIDER


def _get_broker_client(provider: str | None = None) -> BrokerClient:
    resolved_provider = (provider or EXECUTION_PROVIDER or "KITE").upper()
    client = _broker_clients.get(resolved_provider)
    if client is None:
        client = create_broker_client(resolved_provider)
        _broker_clients[resolved_provider] = client
    return client


def _normalize_execution_mode(mode: str | None) -> str:
    return (mode or EXECUTION_MODE or "PAPER").upper()


def _normalize_execution_provider(provider: str | None) -> str:
    return (provider or EXECUTION_PROVIDER or "KITE").upper()


def _validate_order_request(
    signal: str,
    quantity: int,
    symbol: str,
    product: str,
    order_type: str,
    entry_price: float | None,
    trigger_price: float | None,
    runtime_config: RuntimeConfig,
    execution_mode: str,
) -> None:
    if not runtime_config.orders.enabled:
        return
    if signal not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported order side: {signal}")
    if int(quantity) < runtime_config.orders.min_quantity:
        raise ValueError(
            f"Quantity must be at least {runtime_config.orders.min_quantity}, got {quantity}"
        )
    if not str(symbol or "").strip():
        raise ValueError("Symbol is required for order placement")
    normalized_product = (product or "MIS").upper()
    if normalized_product not in runtime_config.orders.allowed_products:
        raise ValueError(
            f"Unsupported product '{normalized_product}'. Allowed products: "
            f"{', '.join(runtime_config.orders.allowed_products)}"
        )
    normalized_order_type = (order_type or "MARKET").upper()
    if normalized_order_type not in runtime_config.orders.allowed_order_types:
        raise ValueError(
            f"Unsupported order type '{normalized_order_type}'. Allowed order types: "
            f"{', '.join(runtime_config.orders.allowed_order_types)}"
        )
    if entry_price is not None and float(entry_price) <= 0:
        raise ValueError("Entry price must be positive when provided")
    if normalized_order_type == "LIMIT" and entry_price is None:
        raise ValueError("Limit orders require a price")
    if normalized_order_type in {"SL", "SL-M"} and trigger_price is None:
        raise ValueError("Stop-loss orders require a trigger price")
    if (
        execution_mode == "LIVE"
        and entry_price is not None
        and runtime_config.orders.max_live_order_notional > 0
        and (float(entry_price) * int(quantity)) > runtime_config.orders.max_live_order_notional
    ):
        raise ValueError(
            "Live order notional exceeds configured limit "
            f"{runtime_config.orders.max_live_order_notional:.2f}"
        )


def _record_order_audit(
    trade_store: TradeStore | None,
    *,
    stage: str,
    signal: str,
    quantity: int,
    symbol: str,
    product: str,
    execution_mode: str,
    provider: str,
    status: str,
    note: str | None = None,
    order_id: str | None = None,
    entry_price: float | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if trade_store is None:
        return
    trade_store.record_order_audit(
        OrderAuditRecord(
            stage=stage,
            symbol=symbol,
            side=signal,
            quantity=int(quantity),
            product=(product or "MIS").upper(),
            execution_mode=execution_mode,
            provider=provider,
            status=status,
            message=message,
            order_id=order_id,
            entry_price=entry_price,
            note=note,
            metadata=metadata or {},
        )
    )


def _reconcile_order_status(
    client: BrokerClient,
    order_result: OrderResult,
    runtime_config: RuntimeConfig,
) -> OrderResult:
    if not order_result.order_id:
        return order_result

    latest = order_result
    for attempt in range(runtime_config.orders.reconcile_attempts):
        try:
            status = client.get_order_status(order_result.order_id)
        except NotImplementedError:
            return latest
        if isinstance(status, OrderResult):
            latest = status
        elif status is not None:
            return latest
        if latest.status in {
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }:
            return latest
        if attempt < runtime_config.orders.reconcile_attempts - 1:
            time.sleep(runtime_config.orders.reconcile_delay_seconds)
    return latest


def _ensure_fill_confirmation(
    reconciled: OrderResult,
    runtime_config: RuntimeConfig,
) -> None:
    if not runtime_config.orders.fill_confirmation_required:
        return
    if reconciled.status == OrderStatus.PENDING:
        raise RuntimeError(
            "Broker did not confirm the order fill state within the configured polling window."
        )


def _build_retry_request(order: OrderRequest, remaining_quantity: int) -> OrderRequest:
    return OrderRequest(
        symbol=order.symbol,
        side=order.side,
        quantity=int(remaining_quantity),
        product=order.product,
        note=order.note,
        order_type=order.order_type,
        price=order.price,
        trigger_price=order.trigger_price,
        validity=order.validity,
    )


def _append_order_lineage(previous: OrderResult, current: OrderResult) -> OrderResult:
    child_ids = tuple(
        item
        for item in (
            *previous.child_order_ids,
            previous.order_id,
            *current.child_order_ids,
        )
        if item and item != current.order_id
    )
    return OrderResult(
        order_id=current.order_id,
        status=current.status,
        message=current.message,
        requested_quantity=current.requested_quantity,
        filled_quantity=current.filled_quantity,
        pending_quantity=current.pending_quantity,
        average_price=current.average_price,
        parent_order_id=current.parent_order_id,
        child_order_ids=child_ids,
        metadata={
            **previous.metadata,
            **current.metadata,
            "previous_order_id": previous.order_id,
        },
    )


def _merge_order_results(primary: OrderResult, secondary: OrderResult) -> OrderResult:
    child_ids = tuple(
        item
        for item in (
            *primary.child_order_ids,
            primary.order_id,
            *secondary.child_order_ids,
            secondary.order_id,
        )
        if item
    )
    final_status = secondary.status if secondary.status != OrderStatus.PENDING else primary.status
    total_requested = max(
        int(primary.requested_quantity or 0),
        int(primary.filled_quantity or 0) + int(primary.pending_quantity or 0),
    )
    total_filled = int(primary.filled_quantity or 0) + int(secondary.filled_quantity or 0)
    pending_quantity = max(0, total_requested - total_filled)
    average_price = primary.average_price
    if total_filled > 0:
        weighted_total = (
            (float(primary.average_price or 0) * int(primary.filled_quantity or 0))
            + (float(secondary.average_price or 0) * int(secondary.filled_quantity or 0))
        )
        average_price = weighted_total / total_filled if weighted_total else None
    return OrderResult(
        order_id=secondary.order_id or primary.order_id,
        status=final_status if pending_quantity == 0 else OrderStatus.PARTIAL,
        message=secondary.message or primary.message,
        requested_quantity=total_requested,
        filled_quantity=total_filled,
        pending_quantity=pending_quantity,
        average_price=average_price,
        child_order_ids=child_ids,
        metadata={
            "retry_attempted": True,
            "primary_order_id": primary.order_id,
            "secondary_order_id": secondary.order_id,
        },
    )


def _retry_partial_fill(
    client: BrokerClient,
    base_request: OrderRequest,
    order_result: OrderResult,
    runtime_config: RuntimeConfig,
    trade_store: TradeStore | None,
    resolved_mode: str,
    resolved_provider: str,
    entry_price: float | None,
) -> OrderResult:
    latest = order_result
    if (
        not runtime_config.orders.partial_fill_retry_enabled
        or latest.filled_quantity >= latest.requested_quantity
    ):
        return latest

    remaining_quantity = max(0, int(latest.requested_quantity or 0) - int(latest.filled_quantity or 0))
    attempts = int(runtime_config.orders.partial_fill_retry_attempts or 0)
    while remaining_quantity > 0 and attempts > 0:
        retry_request = _build_retry_request(base_request, remaining_quantity)
        retry_result = client.place_order(retry_request)
        _record_order_audit(
            trade_store,
            stage="partial_fill_retry_submitted",
            signal=retry_request.side,
            quantity=retry_request.quantity,
            symbol=retry_request.symbol,
            product=retry_request.product,
            execution_mode=resolved_mode,
            provider=resolved_provider,
            status=retry_result.status.value,
            note=retry_request.note,
            order_id=retry_result.order_id,
            entry_price=entry_price,
            metadata={"remaining_quantity": remaining_quantity},
        )
        reconciled_retry = _reconcile_order_status(client, retry_result, runtime_config)
        latest = _merge_order_results(latest, reconciled_retry)
        remaining_quantity = max(
            0,
            int(latest.requested_quantity or 0) - int(latest.filled_quantity or 0),
        )
        attempts -= 1
    return latest


def _safe_quote_price(client: BrokerClient, order: OrderRequest) -> float | None:
    try:
        quote = client.get_quote(order.symbol)
    except Exception:
        return None
    if order.side == "BUY":
        for candidate in (quote.ask_price, quote.last_price, quote.bid_price):
            if candidate and float(candidate) > 0:
                return float(candidate)
    for candidate in (quote.bid_price, quote.last_price, quote.ask_price):
        if candidate and float(candidate) > 0:
            return float(candidate)
    return None


def _round_retry_quantity(symbol: str, quantity: int) -> int:
    lot_size = 1
    if ":" in str(symbol):
        try:
            from fno_data_fetcher import get_contract_lot_size

            lot_size = max(1, int(get_contract_lot_size(symbol)))
        except Exception:
            lot_size = 1
    if lot_size <= 1:
        return max(0, int(quantity))
    return max(0, (int(quantity) // lot_size) * lot_size)


def _estimate_required_margin(
    order: OrderRequest,
    reference_price: float | None,
    runtime_config: RuntimeConfig,
) -> float | None:
    if reference_price is None or float(reference_price) <= 0:
        return None
    required = float(reference_price) * int(order.quantity)
    return required * (1 + float(runtime_config.orders.margin_buffer_pct or 0.0))


def _check_margin_availability(
    client: BrokerClient,
    order: OrderRequest,
    runtime_config: RuntimeConfig,
    reference_price: float | None,
) -> tuple[bool, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "margin_check_enabled": runtime_config.orders.margin_check_enabled,
        "reference_price": reference_price,
    }
    if not runtime_config.orders.margin_check_enabled:
        return True, metadata
    try:
        available = client.get_available_margin(order.product)
    except NotImplementedError:
        metadata["margin_supported"] = False
        return True, metadata
    try:
        available = None if available is None else float(available)
    except (TypeError, ValueError):
        available = None
    metadata["margin_supported"] = True
    metadata["available_margin"] = available
    required = _estimate_required_margin(order, reference_price, runtime_config)
    metadata["required_margin_estimate"] = required
    if available is None or required is None:
        return True, metadata
    return available >= float(required), metadata


def _is_margin_rejection(message: str | None) -> bool:
    text = str(message or "").lower()
    return any(
        token in text
        for token in ("margin", "fund", "insufficient", "rms", "available cash", "not enough balance")
    )


def _build_rejection_retry_request(
    client: BrokerClient,
    order: OrderRequest,
    rejected: OrderResult,
    runtime_config: RuntimeConfig,
    reference_price: float | None,
) -> OrderRequest | None:
    next_quantity = int(order.quantity)
    rejection_message = rejected.message

    try:
        available_margin = client.get_available_margin(order.product)
    except NotImplementedError:
        available_margin = None

    if _is_margin_rejection(rejection_message) and available_margin and reference_price:
        affordable_qty = int(
            math.floor(
                float(available_margin)
                / max(
                    float(reference_price) * (1 + float(runtime_config.orders.margin_buffer_pct or 0.0)),
                    0.01,
                )
            )
        )
        next_quantity = min(next_quantity, affordable_qty)
    else:
        next_quantity = int(
            math.floor(
                int(order.quantity)
                * (1 - float(runtime_config.orders.rejection_retry_reduce_quantity_pct or 0.0))
            )
        )

    next_quantity = _round_retry_quantity(order.symbol, next_quantity)
    if next_quantity <= 0 or next_quantity == int(order.quantity):
        return None

    quote_anchor = _safe_quote_price(client, order)
    price_anchor = quote_anchor or order.price or reference_price
    if price_anchor is None or float(price_anchor) <= 0:
        return None

    price_buffer = float(runtime_config.orders.rejection_retry_price_buffer_pct or 0.0)
    if order.side == "BUY":
        retry_price = float(price_anchor) * (1 + price_buffer)
    else:
        retry_price = max(0.01, float(price_anchor) * (1 - price_buffer))

    return OrderRequest(
        symbol=order.symbol,
        side=order.side,
        quantity=next_quantity,
        product=order.product,
        note=order.note,
        order_type="LIMIT",
        price=retry_price,
        trigger_price=order.trigger_price,
        validity=order.validity,
    )


def _retry_rejected_order(
    client: BrokerClient,
    base_request: OrderRequest,
    order_result: OrderResult,
    runtime_config: RuntimeConfig,
    trade_store: TradeStore | None,
    resolved_mode: str,
    resolved_provider: str,
    reference_price: float | None,
) -> OrderResult:
    latest = order_result
    if not runtime_config.orders.rejection_retry_enabled:
        return latest

    attempts = int(runtime_config.orders.rejection_retry_attempts or 0)
    current_request = base_request
    while latest.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED} and attempts > 0:
        retry_request = _build_rejection_retry_request(
            client,
            current_request,
            latest,
            runtime_config,
            reference_price,
        )
        if retry_request is None:
            break
        _record_order_audit(
            trade_store,
            stage="rejection_retry_planned",
            signal=retry_request.side,
            quantity=retry_request.quantity,
            symbol=retry_request.symbol,
            product=retry_request.product,
            execution_mode=resolved_mode,
            provider=resolved_provider,
            status=latest.status.value,
            note=retry_request.note,
            order_id=latest.order_id,
            entry_price=reference_price,
            message=latest.message,
            metadata={
                "original_order_id": latest.order_id,
                "retry_order_type": retry_request.order_type,
                "retry_price": retry_request.price,
                "retry_quantity": retry_request.quantity,
            },
        )
        retry_result = client.place_order(retry_request)
        _record_order_audit(
            trade_store,
            stage="rejection_retry_submitted",
            signal=retry_request.side,
            quantity=retry_request.quantity,
            symbol=retry_request.symbol,
            product=retry_request.product,
            execution_mode=resolved_mode,
            provider=resolved_provider,
            status=retry_result.status.value,
            note=retry_request.note,
            order_id=retry_result.order_id,
            entry_price=reference_price,
            message=retry_result.message,
            metadata={
                "order_type": retry_request.order_type,
                "price": retry_request.price,
            },
        )
        reconciled_retry = _reconcile_order_status(client, retry_result, runtime_config)
        latest = _append_order_lineage(latest, reconciled_retry)
        current_request = retry_request
        attempts -= 1
    return latest


def _enforce_spread_check(
    client: BrokerClient,
    symbol: str,
    runtime_config: RuntimeConfig,
) -> tuple[bool, dict[str, Any]]:
    max_spread_pct = float(runtime_config.orders.max_spread_pct or 0.0)
    if max_spread_pct <= 0:
        return True, {}
    quote = client.get_quote(symbol)
    spread_pct = quote.spread_pct
    metadata = {
        "last_price": quote.last_price,
        "bid_price": quote.bid_price,
        "ask_price": quote.ask_price,
        "spread": quote.spread,
        "spread_pct": spread_pct,
    }
    if spread_pct is None:
        return True, metadata
    return spread_pct <= max_spread_pct, metadata


def _record_slippage_audit(
    trade_store: TradeStore | None,
    *,
    signal: str,
    quantity: int,
    symbol: str,
    product: str,
    execution_mode: str,
    provider: str,
    note: str | None,
    order_id: str | None,
    reference_price: float | None,
    order_result: OrderResult,
) -> None:
    if reference_price is None or order_result.average_price is None or int(order_result.filled_quantity or 0) <= 0:
        return
    expected = float(reference_price)
    actual = float(order_result.average_price)
    signed_slippage = (actual - expected) if signal == "BUY" else (expected - actual)
    slippage_pct = signed_slippage / expected if expected else None
    _record_order_audit(
        trade_store,
        stage="slippage",
        signal=signal,
        quantity=quantity,
        symbol=symbol,
        product=product,
        execution_mode=execution_mode,
        provider=provider,
        status=order_result.status.value,
        note=note,
        order_id=order_id,
        entry_price=reference_price,
        metadata={
            "expected_price": expected,
            "actual_price": actual,
            "signed_slippage": signed_slippage,
            "slippage_pct": slippage_pct,
            "filled_quantity": int(order_result.filled_quantity or 0),
        },
    )


def place_order(
    signal: str,
    quantity: int,
    symbol: str,
    note: str | None = None,
    product: str = "MIS",
    entry_price: float | None = None,
    order_type: str = "MARKET",
    price: float | None = None,
    trigger_price: float | None = None,
    validity: str = "DAY",
    runtime_config: RuntimeConfig | None = None,
    trade_store: TradeStore | None = None,
    execution_provider: str | None = None,
    execution_mode: str | None = None,
    enforce_spread_check: bool = True,
    enforce_margin_check: bool = True,
) -> OrderResult | None:
    runtime_config = runtime_config or get_runtime_config()
    resolved_provider = _normalize_execution_provider(execution_provider)
    resolved_mode = _normalize_execution_mode(execution_mode)
    log_event("\n[EXECUTION] Preparing order...")
    log_event(f"[EXECUTION] Provider: {resolved_provider}")
    log_event(f"[EXECUTION] Symbol: {symbol.replace('.NS', '')}")
    log_event(f"[EXECUTION] Signal: {signal}")
    log_event(f"[EXECUTION] Quantity: {quantity}")
    log_event(f"[EXECUTION] Mode: {resolved_mode}")
    log_event(f"[EXECUTION] Product: {(product or 'MIS').upper()}")
    log_event(f"[EXECUTION] Order Type: {(order_type or 'MARKET').upper()}")
    log_event(f"[EXECUTION] Broker IP Mode: {get_broker_ip_mode()}")

    if entry_price:
        log_event(f"[EXECUTION] Entry Price: {entry_price:.2f}")
        log_event(f"[EXECUTION] Entry Value: {entry_price * quantity:.2f}")

    if note:
        log_event(f"[EXECUTION] Note: {note}")

    _validate_order_request(
        signal,
        int(quantity),
        symbol,
        product,
        order_type,
        price if price is not None else entry_price,
        trigger_price,
        runtime_config,
        resolved_mode,
    )
    _record_order_audit(
        trade_store,
        stage="pre_flight",
        signal=signal,
        quantity=quantity,
        symbol=symbol,
        product=product,
        execution_mode=resolved_mode,
        provider=resolved_provider,
        status="PASSED",
        note=note,
        entry_price=entry_price,
        metadata={
            "order_type": (order_type or "MARKET").upper(),
            "price": price,
            "trigger_price": trigger_price,
            "validity": validity,
        },
    )

    if resolved_mode != "LIVE":
        log_event("Order NOT placed (paper mode)")
        _record_order_audit(
            trade_store,
            stage="paper_skip",
            signal=signal,
            quantity=quantity,
            symbol=symbol,
            product=product,
            execution_mode=resolved_mode,
            provider=resolved_provider,
            status="SKIPPED",
            note=note,
            entry_price=entry_price,
            message="Order skipped because execution mode is PAPER",
            metadata={"order_type": (order_type or "MARKET").upper(), "price": price},
        )
        return None

    client = _get_broker_client(resolved_provider)
    if enforce_spread_check:
        spread_ok, spread_metadata = _enforce_spread_check(client, symbol, runtime_config)
        _record_order_audit(
            trade_store,
            stage="spread_check",
            signal=signal,
            quantity=quantity,
            symbol=symbol,
            product=product,
            execution_mode=resolved_mode,
            provider=resolved_provider,
            status="PASSED" if spread_ok else "BLOCKED",
            note=note,
            entry_price=entry_price,
            metadata=spread_metadata,
        )
        if not spread_ok:
            spread_pct = float(spread_metadata.get("spread_pct") or 0.0) * 100.0
            raise RuntimeError(
                f"Spread check blocked {symbol}: spread {spread_pct:.2f}% exceeds allowed "
                f"{runtime_config.orders.max_spread_pct * 100:.2f}%"
            )

    normalized_order_type = (order_type or "MARKET").upper()
    resolved_price = price
    if normalized_order_type == "LIMIT" and resolved_price is None:
        resolved_price = entry_price

    request = OrderRequest(
        symbol=symbol,
        side=signal,
        quantity=quantity,
        product=product,
        note=note,
        order_type=normalized_order_type,
        price=resolved_price,
        trigger_price=trigger_price,
        validity=validity,
    )
    if enforce_margin_check:
        margin_ok, margin_metadata = _check_margin_availability(
            client,
            request,
            runtime_config,
            resolved_price if resolved_price is not None else entry_price,
        )
        _record_order_audit(
            trade_store,
            stage="margin_check",
            signal=signal,
            quantity=quantity,
            symbol=symbol,
            product=product,
            execution_mode=resolved_mode,
            provider=resolved_provider,
            status="PASSED" if margin_ok else "BLOCKED",
            note=note,
            entry_price=entry_price,
            metadata=margin_metadata,
        )
        if not margin_ok:
            raise RuntimeError(
                "Margin check blocked order: available margin is below the estimated requirement."
            )
    order_result = client.place_order(
        request
    )
    _record_order_audit(
        trade_store,
        stage="submitted",
        signal=signal,
        quantity=quantity,
        symbol=symbol,
        product=product,
        execution_mode=resolved_mode,
        provider=resolved_provider,
        status=order_result.status.value,
        note=note,
        order_id=order_result.order_id,
        entry_price=entry_price,
        message=order_result.message,
        metadata={
            "order_type": normalized_order_type,
            "price": resolved_price,
            "trigger_price": trigger_price,
        },
    )
    reconciled = _reconcile_order_status(client, order_result, runtime_config)
    _ensure_fill_confirmation(reconciled, runtime_config)
    reconciled = _retry_rejected_order(
        client,
        request,
        reconciled,
        runtime_config,
        trade_store,
        resolved_mode,
        resolved_provider,
        resolved_price if resolved_price is not None else entry_price,
    )
    _ensure_fill_confirmation(reconciled, runtime_config)
    reconciled = _retry_partial_fill(
        client,
        request,
        reconciled,
        runtime_config,
        trade_store,
        resolved_mode,
        resolved_provider,
        entry_price,
    )
    _ensure_fill_confirmation(reconciled, runtime_config)
    if reconciled.order_id != order_result.order_id or reconciled.status != order_result.status:
        log_event(
            f"[EXECUTION] Order reconciliation | OrderId={reconciled.order_id} | "
            f"Status={reconciled.status.value} | Filled={reconciled.filled_quantity}/{reconciled.requested_quantity}"
        )
    _record_order_audit(
        trade_store,
        stage="reconciled",
        signal=signal,
        quantity=quantity,
        symbol=symbol,
        product=product,
        execution_mode=resolved_mode,
        provider=resolved_provider,
        status=reconciled.status.value,
        note=note,
        order_id=reconciled.order_id,
        entry_price=entry_price,
        message=reconciled.message,
        metadata={
            "filled_quantity": reconciled.filled_quantity,
            "requested_quantity": reconciled.requested_quantity,
            "pending_quantity": reconciled.pending_quantity,
            "average_price": reconciled.average_price,
            "child_order_ids": list(reconciled.child_order_ids),
        },
    )
    _record_slippage_audit(
        trade_store,
        signal=signal,
        quantity=quantity,
        symbol=symbol,
        product=product,
        execution_mode=resolved_mode,
        provider=resolved_provider,
        note=note,
        order_id=reconciled.order_id,
        reference_price=resolved_price if resolved_price is not None else entry_price,
        order_result=reconciled,
    )
    if reconciled.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
        raise RuntimeError(
            f"Live order failed with status {reconciled.status.value}: {reconciled.message or 'No broker message'}"
        )
    return reconciled


def place_bracket_order(
    signal: str,
    quantity: int,
    symbol: str,
    entry_price: float,
    stop_loss_price: float,
    target_price: float,
    note: str | None = None,
    product: str = "MIS",
    runtime_config: RuntimeConfig | None = None,
    trade_store: TradeStore | None = None,
    execution_provider: str | None = None,
    execution_mode: str | None = None,
) -> OrderResult | None:
    runtime_config = runtime_config or get_runtime_config()
    if stop_loss_price <= 0 or target_price <= 0:
        raise ValueError("Bracket orders require positive stop-loss and target prices")
    if abs(float(entry_price) - float(stop_loss_price)) <= 0:
        raise ValueError("Bracket orders require entry and stop-loss prices to differ")

    # Kite Connect docs currently expose regular and cover orders, not BO.
    # We therefore place the entry order and return bracket intent metadata for the caller.
    entry_result = place_order(
        signal,
        quantity,
        symbol,
        note=note or "Bracket entry",
        product=product,
        entry_price=entry_price,
        order_type="LIMIT",
        price=entry_price,
        runtime_config=runtime_config,
        trade_store=trade_store,
        execution_provider=execution_provider,
        execution_mode=execution_mode,
        enforce_spread_check=True,
    )
    if entry_result is None:
        return None
    entry_result.metadata.update(
        {
            "bracket_requested": True,
            "bracket_mode": "SYNTHETIC",
            "stop_loss_price": stop_loss_price,
            "target_price": target_price,
            "note": "Native Kite BO is not wired because current docs expose regular/co varieties; this records synthetic bracket intent.",
        }
    )
    return entry_result


def _extract_upstox_error_detail(response: Any) -> str:
    return UpstoxBrokerClient.extract_error_detail(response)


def _collect_upstox_ip_diagnostics() -> dict[str, str | None]:
    return UpstoxBrokerClient()._collect_ip_diagnostics()


def _extract_ip_addresses(text: str) -> list[str]:
    return UpstoxBrokerClient.extract_ip_addresses(text)


def _format_upstox_ip_diagnostics(
    broker_public_ipv4,
    configured_static_ip,
    general_public_ipv6,
    hinted_ips,
) -> str:
    return UpstoxBrokerClient.format_ip_diagnostics(
        broker_public_ipv4,
        configured_static_ip,
        general_public_ipv6,
        hinted_ips,
    )


def is_upstox_static_ip_blocked(error: Any) -> bool:
    message = str(error or "")
    return "UDAPI1154" in message and "static IP" in message


def get_intraday_positions() -> list[dict[str, Any]]:
    return _get_broker_client().get_intraday_positions()


def get_delivery_holdings() -> list[dict[str, Any]]:
    return _get_broker_client().get_delivery_holdings()


def get_nfo_positions() -> list[dict[str, Any]]:
    return _get_broker_client().get_nfo_positions()


def get_quote(symbol: str, provider: str | None = None):
    return _get_broker_client(provider).get_quote(symbol)


def get_available_margin(product: str | None = None, provider: str | None = None) -> float | None:
    return _get_broker_client(provider).get_available_margin(product)
