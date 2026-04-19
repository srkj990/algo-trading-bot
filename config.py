import os
from dataclasses import dataclass


def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


@dataclass(frozen=True)
class BrokerConfig:
    code: str
    name: str
    env_prefix: str
    default_port: int
    auth_backend: str
    env_aliases: tuple[str, ...] = ()

    def env_names(self, suffix):
        suffix_key = suffix.upper()
        names = [f"{self.env_prefix}_{suffix_key}"]
        names.extend(f"{alias}_{suffix_key}" for alias in self.env_aliases)
        return tuple(names)


BROKERS = (
    BrokerConfig(
        code="KITE",
        name="Zerodha Kite",
        env_prefix="KITE",
        default_port=8000,
        auth_backend="kite",
        env_aliases=("ZERODHA",),
    ),
    BrokerConfig(
        code="UPSTOX",
        name="Upstox",
        env_prefix="UPSTOX",
        default_port=8001,
        auth_backend="upstox",
    ),
)

BROKER_MAP = {broker.code: broker for broker in BROKERS}


def _normalize_broker_code(broker):
    code = (broker or "").strip().upper()
    if not code:
        raise RuntimeError("Broker code is required.")
    if code not in BROKER_MAP:
        supported = ", ".join(BROKER_MAP)
        raise RuntimeError(
            f"Unsupported broker '{broker}'. Supported brokers: {supported}"
        )
    return code


def get_broker_config(broker):
    return BROKER_MAP[_normalize_broker_code(broker)]


def get_supported_brokers():
    return BROKERS


def _get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )
    return value


def _get_first_env_value(names):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def get_broker_env_names(broker, suffix):
    return get_broker_config(broker).env_names(suffix)


def get_broker_primary_env_name(broker, suffix):
    return get_broker_env_names(broker, suffix)[0]


def get_broker_env_value(broker, suffix, required=True):
    names = get_broker_env_names(broker, suffix)
    value = _get_first_env_value(names)
    if value:
        return value

    if required:
        raise RuntimeError(
            "Missing required environment variable. "
            f"Checked: {', '.join(names)}"
        )
    return None


def get_broker_api_key(broker):
    return get_broker_env_value(broker, "API_KEY")


def get_broker_api_secret(broker):
    return get_broker_env_value(broker, "API_SECRET")


def get_broker_access_token(broker):
    return get_broker_env_value(broker, "ACCESS_TOKEN")


def get_broker_redirect_uri(broker, required=False):
    return get_broker_env_value(broker, "REDIRECT_URI", required=required)


def get_api_key():
    return get_broker_api_key("KITE")


def get_api_secret():
    return get_broker_api_secret("KITE")


def get_access_token():
    return get_broker_access_token("KITE")


def get_upstox_access_token():
    return get_broker_access_token("UPSTOX")


def get_upstox_api_key():
    return get_broker_api_key("UPSTOX")


def get_upstox_api_secret():
    return get_broker_api_secret("UPSTOX")


def get_default_data_provider():
    return os.getenv("DATA_PROVIDER", "YFINANCE").upper()


def get_default_execution_provider():
    return os.getenv("EXECUTION_PROVIDER", "KITE").upper()


API_KEY = _get_first_env_value(get_broker_env_names("KITE", "API_KEY"))
ACCESS_TOKEN = _get_first_env_value(get_broker_env_names("KITE", "ACCESS_TOKEN"))
UPSTOX_ACCESS_TOKEN = _get_first_env_value(
    get_broker_env_names("UPSTOX", "ACCESS_TOKEN")
)

# Minimum candles per strategy
MIN_CANDLES = {
    "MA": 50,
    "RSI": 14,
    "BREAKOUT": 20,
    "VWAP": 1,
    "ORB": 20,
}

# Nifty 50 symbol universe
# Source note:
# This list reflects the current project configuration target universe.
NIFTY50_SYMBOLS = [
    "ADANIENT.NS",
    "ADANIPORTS.NS",
    "APOLLOHOSP.NS",
    "ASIANPAINT.NS",
    "AXISBANK.NS",
    "BAJAJ-AUTO.NS",
    "BAJFINANCE.NS",
    "BAJAJFINSV.NS",
    "BEL.NS",
    "BHARTIARTL.NS",
    "CIPLA.NS",
    "COALINDIA.NS",
    "DRREDDY.NS",
    "EICHERMOT.NS",
    "ETERNAL.NS",
    "GRASIM.NS",
    "HCLTECH.NS",
    "HDFCBANK.NS",
    "HDFCLIFE.NS",
    "HINDALCO.NS",
    "HINDUNILVR.NS",
    "ICICIBANK.NS",
    "INDIGO.NS",
    "INFY.NS",
    "ITC.NS",
    "JIOFIN.NS",
    "JSWSTEEL.NS",
    "KOTAKBANK.NS",
    "LT.NS",
    "M&M.NS",
    "MARUTI.NS",
    "MAXHEALTH.NS",
    "NESTLEIND.NS",
    "NTPC.NS",
    "ONGC.NS",
    "POWERGRID.NS",
    "RELIANCE.NS",
    "SBILIFE.NS",
    "SHRIRAMFIN.NS",
    "SBIN.NS",
    "SUNPHARMA.NS",
    "TCS.NS",
    "TATACONSUM.NS",
    "TMPV.NS",
    "TATASTEEL.NS",
    "TECHM.NS",
    "TITAN.NS",
    "TRENT.NS",
    "ULTRACEMCO.NS",
    "WIPRO.NS",
]

# Manual symbol table for quick daily selection.
# Update this table whenever you want your own watchlist shortcuts.
MANUAL_SYMBOL_TABLE = {
    "1": "RELIANCE.NS",
    "2": "INFY.NS",
    "3": "TCS.NS",
    "4": "HDFCBANK.NS",
    "5": "ICICIBANK.NS",
    "6": "SBIN.NS",
    "7": "KOTAKBANK.NS",
    "8": "ITC.NS",
    "9": "BHARTIARTL.NS",
    "10": "LT.NS",
}

# Single-symbol table for quick focused runs.
# Keep this table small so single-mode selection stays fast.
SINGLE_SYMBOL_TABLE = {
    "1": "SIEMENS.NS",
    "2": "RELIANCE.NS",
}

# Logging config
LOG_FILE = "algo.log"
LOG_LEVEL = "INFO"
