import socket
import threading
import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from config import get_broker_request_timeout_seconds


DEFAULT_BROKER_IP_MODE = "IPV4_ONLY"
_getaddrinfo_lock = threading.RLock()
_kite_rate_lock = threading.RLock()
_kite_last_call_at = {}
_KITE_MIN_INTERVAL_SECONDS = {
    "historical_data": 0.4,
    "quote": 0.75,
    "instruments": 86400.0,
    "orders": 0.1,
}
_KITE_CACHE_DIR = Path(__file__).parent / "state" / "kite_cache"


@contextmanager
def broker_network_context(ip_mode=None):
    normalized_mode = (ip_mode or DEFAULT_BROKER_IP_MODE).strip().upper()
    if normalized_mode != "IPV4_ONLY":
        yield
        return

    with _getaddrinfo_lock:
        original_getaddrinfo = socket.getaddrinfo

        def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return original_getaddrinfo(
                host,
                port,
                socket.AF_INET,
                type,
                proto,
                flags,
            )

        socket.getaddrinfo = _ipv4_only_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


class IPv4OnlyAdapter(HTTPAdapter):
    def send(self, request, *args, **kwargs):
        with broker_network_context("IPV4_ONLY"):
            return super().send(request, *args, **kwargs)


def create_requests_session(ip_mode=None, timeout_seconds=None):
    session = requests.Session()
    normalized_mode = (ip_mode or DEFAULT_BROKER_IP_MODE).strip().upper()
    if normalized_mode == "IPV4_ONLY":
        adapter = IPv4OnlyAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    resolved_timeout = timeout_seconds
    if resolved_timeout is None:
        resolved_timeout = get_broker_request_timeout_seconds()
    resolved_timeout = float(resolved_timeout)
    original_request = session.request

    def _request_with_default_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", resolved_timeout)
        return original_request(method, url, **kwargs)

    session.request = _request_with_default_timeout
    return session


def broker_request(method, url, session=None, ip_mode=None, **kwargs):
    active_session = session or create_requests_session(ip_mode=ip_mode)
    return active_session.request(method=method, url=url, **kwargs)


def configure_kite_client_network(kite_client, ip_mode=None):
    kite_client.reqsession = create_requests_session(ip_mode=ip_mode)
    return kite_client


def run_in_broker_network(callable_obj, *args, ip_mode=None, **kwargs):
    with broker_network_context(ip_mode=ip_mode):
        return callable_obj(*args, **kwargs)


def kite_rate_limited_call(operation, callable_obj, *args, **kwargs):
    minimum_interval = float(_KITE_MIN_INTERVAL_SECONDS.get(str(operation), 0.0) or 0.0)
    if minimum_interval > 0:
        with _kite_rate_lock:
            now = time.monotonic()
            last_called = float(_kite_last_call_at.get(operation, 0.0) or 0.0)
            wait_seconds = max(0.0, minimum_interval - (now - last_called))
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _kite_last_call_at[operation] = time.monotonic()
    return callable_obj(*args, **kwargs)


def get_cached_kite_instruments(exchange, fetch_callable):
    normalized_exchange = str(exchange or "").upper()
    if not normalized_exchange:
        raise ValueError("Exchange is required for instrument cache")

    cache_day = datetime.now().strftime("%Y%m%d")
    cache_dir = _KITE_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"instruments_{normalized_exchange}_{cache_day}.json"

    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, list) else []

    try:
        instruments = kite_rate_limited_call("instruments", fetch_callable)
    except Exception:
        fallback_candidates = sorted(
            cache_dir.glob(f"instruments_{normalized_exchange}_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for fallback_path in fallback_candidates:
            if fallback_path == cache_path:
                continue
            with fallback_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list) and payload:
                return payload
        raise

    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(instruments, handle, default=str)
    return instruments
