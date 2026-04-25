import socket
import threading
from contextlib import contextmanager

import requests
from requests.adapters import HTTPAdapter


DEFAULT_BROKER_IP_MODE = "IPV4_ONLY"
_getaddrinfo_lock = threading.RLock()


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


def create_requests_session(ip_mode=None):
    session = requests.Session()
    normalized_mode = (ip_mode or DEFAULT_BROKER_IP_MODE).strip().upper()
    if normalized_mode == "IPV4_ONLY":
        adapter = IPv4OnlyAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
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
