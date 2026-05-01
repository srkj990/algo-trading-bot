from __future__ import annotations

import time
from typing import Any

from brokers.base import BrokerClient, OrderRequest, OrderResult, OrderStatus
from brokers.clients import UpstoxBrokerClient
from brokers.factory import create_broker_client
from config import (
    RuntimeConfig,
    get_broker_ip_mode,
    get_default_execution_provider,
    get_runtime_config,
)
from logger import log_event
from models.trade_record import OrderAuditRecord
from trade_store import TradeStore


EXECUTION_MODE = "PAPER"
EXECUTION_PROVIDER = get_default_execution_provider()
_broker_clients: dict[str, BrokerClient] = {}


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
    entry_price: float | None,
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
    if entry_price is not None and float(entry_price) <= 0:
        raise ValueError("Entry price must be positive when provided")
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
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }:
            return latest
        if attempt < runtime_config.orders.reconcile_attempts - 1:
            time.sleep(runtime_config.orders.reconcile_delay_seconds)
    return latest


def place_order(
    signal: str,
    quantity: int,
    symbol: str,
    note: str | None = None,
    product: str = "MIS",
    entry_price: float | None = None,
    runtime_config: RuntimeConfig | None = None,
    trade_store: TradeStore | None = None,
    execution_provider: str | None = None,
    execution_mode: str | None = None,
) -> str | None:
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
        entry_price,
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
        )
        return None

    client = _get_broker_client(resolved_provider)
    order_result = client.place_order(
        OrderRequest(
            symbol=symbol,
            side=signal,
            quantity=quantity,
            product=product,
            note=note,
        )
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
    )
    reconciled = _reconcile_order_status(client, order_result, runtime_config)
    if reconciled.order_id != order_result.order_id or reconciled.status != order_result.status:
        log_event(
            f"[EXECUTION] Order reconciliation | OrderId={reconciled.order_id} | Status={reconciled.status.value}"
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
    )
    if reconciled.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
        raise RuntimeError(
            f"Live order failed with status {reconciled.status.value}: {reconciled.message or 'No broker message'}"
        )
    return reconciled.order_id


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
