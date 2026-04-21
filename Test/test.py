from pathlib import Path
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import requests
import yfinance as yf

from kiteconnect import KiteConnect
from upstox_client import ApiClient, Configuration
from upstox_client.api import user_api
from upstox_client.rest import ApiException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import get_access_token, get_api_key, get_upstox_access_token
from data_fetcher import get_data, set_data_provider
from executor import (
    is_upstox_static_ip_blocked,
    place_order,
    set_execution_mode,
    set_execution_provider,
)


class _NoopPool:
    def close(self):
        return None

    def join(self):
        return None


def _safe_api_client_del(self):
    pool = getattr(self, "pool", None)
    if pool is None:
        return
    try:
        pool.close()
        pool.join()
    except Exception:
        return


ApiClient.__del__ = _safe_api_client_del


def build_upstox_api_client():
    token = get_upstox_access_token().strip()
    config = Configuration()
    config.access_token = token
    config.api_key["Authorization"] = token
    config.api_key_prefix["Authorization"] = "Bearer"
    api_client = ApiClient(config)
    if not hasattr(api_client, "pool"):
        api_client.pool = _NoopPool()
    return api_client


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
    print_result(
        symbol,
        True,
        f"{data.iloc[-1]['Close']:.2f} ({len(data)} candles)",
    )


def fetch_data_quietly(symbol, period, interval):
    buffer = StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        return get_data(symbol, period=period, interval=interval)


def main():
    yf_cache_dir = PROJECT_ROOT / "state" / "yfinance_cache"
    yf_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(yf_cache_dir))

    token = get_upstox_access_token().strip()
    print("TOKEN:", mask_token(token))
    print("LENGTH:", len(token))

    try:
        print("PUBLIC IP:", requests.get("https://api.ipify.org", timeout=10).text)
    except Exception as exc:
        print_result("Public IP lookup", False, exc)
        print("Continuing without public IP check.")

    print("=== BROKER PROFILE TEST ===")

    zerodha_profile_ok = False
    try:
        kite = KiteConnect(api_key=get_api_key())
        kite.set_access_token(get_access_token())
        print("Zerodha:", kite.profile())
        zerodha_profile_ok = True
    except Exception as exc:
        print_result("Zerodha profile", False, exc)

    upstox_profile_ok = False
    try:
        api_client = build_upstox_api_client()
        upstox = user_api.UserApi(api_client)
        print("Upstox:", upstox.get_profile(api_version="2.0"))
        upstox_profile_ok = True
    except ApiException as exc:
        print_result("Upstox profile", False, getattr(exc, "body", str(exc)))
        print(
            "Hint: run `py .\\auto_auth.py`, choose UPSTOX, and refresh "
            "UPSTOX_ACCESS_TOKEN in .env."
        )
    except Exception as exc:
        print_result("Upstox profile", False, exc)

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
                print_result(
                    "Upstox order",
                    False,
                    "Order APIs are blocked by Upstox static IP restrictions for this app/account.",
                )
                print(
                    "Action needed: configure a static public IP in your Upstox app settings "
                    "or use an app/account without static-IP enforcement for order placement."
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
            kite_test = KiteConnect(api_key=get_api_key())
            kite_test.set_access_token(get_access_token())
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
            "also fail until access/IP allowlisting is fixed."
        )

    if not upstox_profile_ok:
        print(
            "\nUpstox profile auth is failing, so Upstox data/order checks may also fail "
            "until the access token is refreshed."
        )

    print("Test complete!")


if __name__ == "__main__":
    main()
