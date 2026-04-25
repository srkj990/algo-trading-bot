import os
import urllib.parse as urlparse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from kiteconnect import KiteConnect

from config import (
    _load_dotenv,
    get_broker_access_token,
    get_broker_api_key,
    get_broker_api_secret,
    get_broker_config,
    get_broker_ip_mode,
    get_broker_primary_env_name,
    get_broker_redirect_uri,
    get_supported_brokers,
)
from network_utils import broker_request, configure_kite_client_network


ENV_PATH = ".env"
DEFAULT_HOST = "127.0.0.1"


def _write_env_value(key, value, path=ENV_PATH):
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as env_file:
            lines = env_file.readlines()

    updated = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}\n"
            updated = True
            break

    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as env_file:
        env_file.writelines(lines)

    os.environ[key] = value


def _prompt_broker():
    brokers = list(get_supported_brokers())
    
    print("Available brokers:")
    for index, broker in enumerate(brokers, start=1):
        print(
            f"{index}. {broker.name} "
            f"({broker.code}) - default callback port {broker.default_port}"
        )

    while True:
        raw = input(
            "Choose broker by number or code [default 1]: "
        ).strip()
        if not raw:
            return brokers[0]

        if raw.isdigit():
            position = int(raw)
            if 1 <= position <= len(brokers):
                return brokers[position - 1]

        normalized = raw.upper()
        for broker in brokers:
            if broker.code == normalized:
                return broker

        print("Invalid selection. Please choose a valid broker.")


def _prompt_port(broker):
    raw = input(
        f"{broker.name} callback port [default {broker.default_port}]: "
    ).strip()
    if not raw:
        return broker.default_port

    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("Port must be a valid number.") from exc

    if port < 1 or port > 65535:
        raise RuntimeError("Port must be between 1 and 65535.")
    return port


def _prompt_redirect_uri(broker):
    configured = get_broker_redirect_uri(broker.code, required=False)
    default_redirect = configured or f"http://{DEFAULT_HOST}:{broker.default_port}"
    raw = input(
        f"{broker.name} redirect URI [default {default_redirect}]: "
    ).strip()
    return raw or default_redirect


def _build_local_redirect_uri(broker, port):
    configured = get_broker_redirect_uri(broker.code, required=False) or ""
    if configured:
        parsed = urlparse.urlparse(configured)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or DEFAULT_HOST
        path = parsed.path or ""
        return f"{scheme}://{host}:{port}{path}"
    return f"http://{DEFAULT_HOST}:{port}"


def _parse_local_redirect(redirect_uri):
    parsed = urlparse.urlparse(redirect_uri)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(
            f"Redirect URI must point to localhost/127.0.0.1, got: {redirect_uri}"
        )
    if not parsed.port:
        raise RuntimeError(
            f"Redirect URI must include an explicit port, got: {redirect_uri}"
        )
    return parsed


def _apply_port_to_redirect_uri(redirect_uri, port):
    parsed = _parse_local_redirect(redirect_uri)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or DEFAULT_HOST
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{host}:{port}{path}{query}"


def _run_local_callback_server(redirect_uri, callback_handler):
    parsed_redirect = _parse_local_redirect(redirect_uri)

    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed_path = urlparse.urlparse(self.path)
            query = urlparse.parse_qs(parsed_path.query)

            try:
                access_token = callback_handler(query)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    (
                        "Login successful. Access token generated.\n"
                        f"{access_token[:12]}..."
                    ).encode("utf-8")
                )
            except Exception as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    f"Authentication failed: {exc}".encode("utf-8")
                )

        def log_message(self, format, *args):
            return

    server = HTTPServer(
        (parsed_redirect.hostname, parsed_redirect.port),
        RequestHandler,
    )
    print(f"Starting local callback server on {redirect_uri}")
    server.handle_request()


def _build_kite_login_url(broker, redirect_uri):
    del redirect_uri
    kite = configure_kite_client_network(
        KiteConnect(api_key=get_broker_api_key(broker.code)),
        ip_mode=get_broker_ip_mode(),
    )
    return kite.login_url()


def _build_upstox_login_url(broker, redirect_uri):
    params = {
        "response_type": "code",
        "client_id": get_broker_api_key(broker.code),
        "redirect_uri": redirect_uri,
        "state": "algo-auth",
    }
    return (
        "https://api.upstox.com/v2/login/authorization/dialog?"
        f"{urlparse.urlencode(params)}"
    )


def _exchange_kite_access_token(broker, query, redirect_uri):
    del redirect_uri
    if "request_token" not in query:
        raise RuntimeError("Missing request_token in callback")

    kite = configure_kite_client_network(
        KiteConnect(api_key=get_broker_api_key(broker.code)),
        ip_mode=get_broker_ip_mode(),
    )
    session = kite.generate_session(
        query["request_token"][0],
        api_secret=get_broker_api_secret(broker.code),
    )
    return session["access_token"]


def _exchange_upstox_access_token(broker, query, redirect_uri):
    if "code" not in query:
        raise RuntimeError("Missing authorization code in callback")

    response = broker_request(
        "POST",
        "https://api.upstox.com/v2/login/authorization/token",
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "code": query["code"][0],
            "client_id": get_broker_api_key(broker.code),
            "client_secret": get_broker_api_secret(broker.code),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
        ip_mode=get_broker_ip_mode(),
    )
    try:
        response.raise_for_status()
    except Exception as exc:
        detail = response.text.strip()
        raise RuntimeError(
            "Upstox token exchange failed. Confirm that UPSTOX_API_KEY, "
            "UPSTOX_API_SECRET, and UPSTOX_REDIRECT_URI exactly match the "
            "values configured in the Upstox app. "
            f"Response: {detail or exc}"
        ) from exc
    return response.json()["access_token"]


AUTH_HANDLERS = {
    "kite": {
        "build_login_url": _build_kite_login_url,
        "exchange_access_token": _exchange_kite_access_token,
    },
    "upstox": {
        "build_login_url": _build_upstox_login_url,
        "exchange_access_token": _exchange_upstox_access_token,
    },
}


def _get_auth_handlers(broker):
    handlers = AUTH_HANDLERS.get(broker.auth_backend)
    if handlers is None:
        raise RuntimeError(
            f"No auth handler configured for broker backend: {broker.auth_backend}"
        )
    return handlers


def refresh_broker_token(broker_code=None):
    broker = (
        get_broker_config(broker_code)
        if broker_code
        else _prompt_broker()
    )
    handlers = _get_auth_handlers(broker)
    configured_redirect_uri = _prompt_redirect_uri(broker)

    if broker.auth_backend == "kite":
        port = _prompt_port(broker)
        redirect_uri = _apply_port_to_redirect_uri(configured_redirect_uri, port)
    else:
        redirect_uri = configured_redirect_uri
        port = _parse_local_redirect(redirect_uri).port

    redirect_env_name = get_broker_primary_env_name(broker.code, "REDIRECT_URI")
    token_env_name = get_broker_primary_env_name(broker.code, "ACCESS_TOKEN")

    print(f"\nOpening {broker.name} login...")
    print(f"Using redirect URI: {redirect_uri}")
    print(
        "Broker app redirect settings must exactly match this URI "
        "for authentication to succeed."
    )

    _write_env_value(redirect_env_name, redirect_uri)

    login_url = handlers["build_login_url"](broker, redirect_uri)
    webbrowser.open(login_url)

    def handle_callback(query):
        access_token = handlers["exchange_access_token"](
            broker=broker,
            query=query,
            redirect_uri=redirect_uri,
        )
        _write_env_value(token_env_name, access_token)
        print(f"{broker.name} access token updated in .env as {token_env_name}")
        return access_token

    _run_local_callback_server(
        redirect_uri=redirect_uri,
        callback_handler=handle_callback,
    )

    return os.environ.get(token_env_name)


def main():
    _load_dotenv(ENV_PATH)
    print("Refreshing access token using common broker auth flow")
    broker = _prompt_broker()
    print(
        f"Using env keys: "
        f"{get_broker_primary_env_name(broker.code, 'API_KEY')}, "
        f"{get_broker_primary_env_name(broker.code, 'API_SECRET')}, "
        f"{get_broker_primary_env_name(broker.code, 'ACCESS_TOKEN')}"
    )
    refresh_broker_token(broker.code)
    print("\nAccess token updated successfully in .env")


if __name__ == "__main__":
    main()
