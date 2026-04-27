from brokers.base import OrderRequest
from brokers.clients import UpstoxBrokerClient
from brokers.factory import create_broker_client
from config import get_broker_ip_mode, get_default_execution_provider
from logger import log_event


EXECUTION_MODE = "PAPER"
EXECUTION_PROVIDER = get_default_execution_provider()
_broker_clients = {}


def set_execution_mode(mode):
    global EXECUTION_MODE
    EXECUTION_MODE = mode.upper()


def set_execution_provider(provider):
    global EXECUTION_PROVIDER
    EXECUTION_PROVIDER = (provider or "KITE").upper()


def get_execution_provider():
    return EXECUTION_PROVIDER


def _get_broker_client(provider=None):
    resolved_provider = (provider or EXECUTION_PROVIDER or "KITE").upper()
    client = _broker_clients.get(resolved_provider)
    if client is None:
        client = create_broker_client(resolved_provider)
        _broker_clients[resolved_provider] = client
    return client


def place_order(signal, quantity, symbol, note=None, product="MIS", entry_price=None):
    log_event("\n[EXECUTION] Preparing order...")
    log_event(f"[EXECUTION] Provider: {EXECUTION_PROVIDER}")
    log_event(f"[EXECUTION] Symbol: {symbol.replace('.NS', '')}")
    log_event(f"[EXECUTION] Signal: {signal}")
    log_event(f"[EXECUTION] Quantity: {quantity}")
    log_event(f"[EXECUTION] Mode: {EXECUTION_MODE}")
    log_event(f"[EXECUTION] Product: {(product or 'MIS').upper()}")
    log_event(f"[EXECUTION] Broker IP Mode: {get_broker_ip_mode()}")

    if entry_price:
        log_event(f"[EXECUTION] Entry Price: {entry_price:.2f}")
        log_event(f"[EXECUTION] Entry Value: {entry_price * quantity:.2f}")

    if note:
        log_event(f"[EXECUTION] Note: {note}")

    if EXECUTION_MODE != "LIVE":
        log_event("Order NOT placed (paper mode)")
        return None

    client = _get_broker_client()
    order_result = client.place_order(
        OrderRequest(
            symbol=symbol,
            side=signal,
            quantity=quantity,
            product=product,
            note=note,
        )
    )
    return order_result.order_id


def _extract_upstox_error_detail(response):
    return UpstoxBrokerClient.extract_error_detail(response)


def _collect_upstox_ip_diagnostics():
    return UpstoxBrokerClient()._collect_ip_diagnostics()


def _extract_ip_addresses(text):
    return UpstoxBrokerClient.extract_ip_addresses(text)


def _format_upstox_ip_diagnostics(
    broker_public_ipv4,
    configured_static_ip,
    general_public_ipv6,
    hinted_ips,
):
    return UpstoxBrokerClient.format_ip_diagnostics(
        broker_public_ipv4,
        configured_static_ip,
        general_public_ipv6,
        hinted_ips,
    )


def is_upstox_static_ip_blocked(error):
    message = str(error or "")
    return "UDAPI1154" in message and "static IP" in message


def get_intraday_positions():
    return _get_broker_client().get_intraday_positions()


def get_delivery_holdings():
    return _get_broker_client().get_delivery_holdings()


def get_nfo_positions():
    return _get_broker_client().get_nfo_positions()
