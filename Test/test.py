from pathlib import Path
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import yfinance as yf
from kiteconnect import KiteConnect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    get_access_token,
    get_api_key,
    get_broker_ip_mode,
    get_upstox_access_token,
    get_upstox_static_ip,
)
from data_fetcher import get_data, set_data_provider
from executor import is_upstox_static_ip_blocked, place_order, set_execution_mode, set_execution_provider
from network_utils import broker_request, configure_kite_client_network


def build_kite_client():
    kite = configure_kite_client_network(
        KiteConnect(api_key=get_api_key()),
        ip_mode=get_broker_ip_mode(),
    )
    kite.set_access_token(get_access_token())
    return kite


def print_result(label, success, detail):
    status = "[OK]" if success else "[FAIL]"
    print(f"{status} {label}: {detail}")


def mask_token(token):
    if not token:
        return "<empty>"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def prompt_yes_no(message):
    try:
        return input(message).strip().upper() == "YES"
    except EOFError:
        return False


def print_data_result(symbol, data):
    if data.empty:
        print_result(symbol, False, "No candles returned")
        return
    print_result(symbol, True, f"{data.iloc[-1]['Close']:.2f} ({len(data)} candles)")


def fetch_data_quietly(symbol, period, interval):
    buffer = StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        return get_data(symbol, period=period, interval=interval)


def get_broker_public_ipv4():
    response = broker_request(
        "GET",
        "https://api.ipify.org",
        timeout=10,
        ip_mode=get_broker_ip_mode(),
    )
    return response.text.strip()


def get_general_public_ipv6():
    response = broker_request(
        "GET",
        "https://api64.ipify.org",
        timeout=10,
        ip_mode="AUTO",
    )
    candidate_ip = response.text.strip()
    if ":" in candidate_ip:
        return candidate_ip
    return None


def get_upstox_profile():
    response = broker_request(
        "GET",
        "https://api.upstox.com/v2/user/profile",
        headers={
            "Accept": "application/json",
            "Api-Version": "2.0",
            "Authorization": f"Bearer {get_upstox_access_token().strip()}",
        },
        timeout=30,
        ip_mode=get_broker_ip_mode(),
    )
    response.raise_for_status()
    return response.json()


def main():
    yf_cache_dir = PROJECT_ROOT / "state" / "yfinance_cache"
    yf_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(yf_cache_dir))

    token = get_upstox_access_token().strip()
    print("TOKEN:", mask_token(token))
    print("LENGTH:", len(token))
    print("BROKER IP MODE:", get_broker_ip_mode())

    broker_public_ipv4 = None
    general_public_ipv6 = None
    try:
        broker_public_ipv4 = get_broker_public_ipv4()
        print("BROKER PUBLIC IPv4:", broker_public_ipv4)
    except Exception as exc:
        print_result("Broker IPv4 lookup", False, exc)
        print("Continuing without broker IPv4 check.")

    try:
        general_public_ipv6 = get_general_public_ipv6()
        if general_public_ipv6:
            print("GENERAL PUBLIC IPv6:", general_public_ipv6)
    except Exception:
        general_public_ipv6 = None

    configured_static_ip = (get_upstox_static_ip() or "").strip()
    if configured_static_ip:
        print("CONFIGURED UPSTOX STATIC IP:", configured_static_ip)

    print("=== BROKER PROFILE TEST ===")

    zerodha_profile_ok = False
    try:
        print("Zerodha:", build_kite_client().profile())
        zerodha_profile_ok = True
    except Exception as exc:
        print_result("Zerodha profile", False, exc)

    upstox_profile_ok = False
    try:
        print("Upstox:", get_upstox_profile())
        upstox_profile_ok = True
    except Exception as exc:
        print_result("Upstox profile", False, exc)
        print(
            "Hint: run `venv\\Scripts\\python.exe .\\auto_auth.py`, choose UPSTOX, "
            "and refresh UPSTOX_ACCESS_TOKEN in .env."
        )

    print("\n=== NEW SYMBOLS DATA TEST (IRB, JPPOWER, RPOWER) ===")

    new_symbols = ["IRB.NS", "JPPOWER.NS", "RPOWER.NS"]

    print("\n-- YFINANCE --")
    set_data_provider("YFINANCE")
    for symbol in new_symbols:
        try:
            data = fetch_data_quietly(symbol, period="5d", interval="1d")
            print_data_result(symbol, data)
        except Exception as exc:
            print_result(symbol, False, exc)

    print("\n-- UPSTOX --")
    set_data_provider("UPSTOX")
    for symbol in new_symbols:
        try:
            data = fetch_data_quietly(symbol, period="5d", interval="1d")
            print_data_result(symbol, data)
        except Exception as exc:
            print_result(symbol, False, exc)

    print("\n=== UPSTOX LIVE ORDER TEST (1 qty RPOWER) ===")
    if prompt_yes_no("Place a REAL Upstox order now? Type YES to continue [default NO]: "):
        set_execution_mode("LIVE")
        set_execution_provider("UPSTOX")
        try:
            order_id = place_order(
                "BUY",
                1,
                "RPOWER.NS",
                note="Test-1qty-Upstox",
                product="MIS",
            )
            print_result("UPSTOX LIVE BUY 1 RPOWER.NS", True, f"Order ID {order_id}")
        except Exception as exc:
            if is_upstox_static_ip_blocked(exc):
                print_result("Upstox order", False, str(exc))
                print(
                    "Action needed: keep BROKER_IP_MODE=IPV4_ONLY so broker APIs use the "
                    f"stable IPv4 {configured_static_ip or broker_public_ipv4}, or update "
                    "the Upstox static IP setting to match the actual outbound broker IP."
                )
            else:
                print_result("Upstox order", False, exc)
    else:
        print("Skipped live Upstox order test.")

    print("\n=== KITE LIVE ORDER TEST (1 qty RPOWER) ===")
    if prompt_yes_no("Place a REAL Kite order now? Type YES to continue [default NO]: "):
        set_execution_mode("LIVE")
        set_execution_provider("KITE")
        try:
            kite_test = build_kite_client()
            order_id = kite_test.place_order(
                variety=kite_test.VARIETY_AMO,
                exchange=kite_test.EXCHANGE_NSE,
                tradingsymbol="RPOWER",
                transaction_type=kite_test.TRANSACTION_TYPE_BUY,
                quantity=1,
                product=kite_test.PRODUCT_MIS,
                order_type=kite_test.ORDER_TYPE_MARKET,
                validity="DAY",
                market_protection=2,
                tag="Test-1qty-Kite",
            )
            print_result(
                "KITE AMO MARKET BUY 1 RPOWER.NS",
                True,
                f"Order ID {order_id} with market protection 2%",
            )
            print("Cancel it manually in Kite if you do not want it to remain queued.")
        except Exception as exc:
            print_result("Kite order placement", False, str(exc))
    else:
        print("Skipped live Kite order test.")

    if not zerodha_profile_ok:
        print(
            "\nZerodha profile auth or network access is failing, so Kite data/order checks may "
            "also fail until auth/network access is fixed."
        )

    if not upstox_profile_ok:
        print(
            "\nUpstox profile auth is failing, so Upstox data/order checks may also fail "
            "until the access token is refreshed or broker IP access is aligned."
        )

    print("Test complete!")


if __name__ == "__main__":
    main()
