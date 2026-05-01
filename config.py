from __future__ import annotations

import ast
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _load_dotenv(path: str = ".env") -> None:
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


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped[0] in {'"', "'"} and stripped[-1] == stripped[0]:
        return stripped[1:-1]
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return stripped


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        line = raw_line.split("#", 1)[0].rstrip()
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if content.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"Invalid YAML list structure in {path}: {raw_line}")
            parent.append(_parse_scalar(content[2:]))
            continue

        key, _, raw_value = content.partition(":")
        key = key.strip()
        value_text = raw_value.strip()
        if not key:
            raise ValueError(f"Invalid YAML key in {path}: {raw_line}")

        if value_text:
            value = _parse_scalar(value_text)
            if isinstance(parent, dict):
                parent[key] = value
            else:
                raise ValueError(f"Invalid YAML mapping in {path}: {raw_line}")
            continue

        next_container: dict[str, Any] | list[Any]
        next_container = {}
        if isinstance(parent, dict):
            parent[key] = next_container
        else:
            raise ValueError(f"Invalid YAML nesting in {path}: {raw_line}")
        stack.append((indent, next_container))

    return root


def _normalize_yaml_lists(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized[key] = _normalize_yaml_lists(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_yaml_lists(item) for item in value]
    return value


def _load_runtime_overrides() -> dict[str, Any]:
    for file_name in ("config.runtime.yaml", "config.runtime.yml"):
        path = Path(file_name)
        if path.exists():
            return _normalize_yaml_lists(_load_simple_yaml(path))
    return {}


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_first_env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class BrokerConfig:
    code: str
    name: str
    env_prefix: str
    default_port: int
    auth_backend: str
    env_aliases: tuple[str, ...] = ()

    def env_names(self, suffix: str) -> tuple[str, ...]:
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


def _normalize_broker_code(broker: str) -> str:
    code = (broker or "").strip().upper()
    if not code:
        raise RuntimeError("Broker code is required.")
    if code not in BROKER_MAP:
        supported = ", ".join(BROKER_MAP)
        raise RuntimeError(
            f"Unsupported broker '{broker}'. Supported brokers: {supported}"
        )
    return code


def get_broker_config(broker: str) -> BrokerConfig:
    return BROKER_MAP[_normalize_broker_code(broker)]


def get_supported_brokers() -> tuple[BrokerConfig, ...]:
    return BROKERS


def get_broker_env_names(broker: str, suffix: str) -> tuple[str, ...]:
    return get_broker_config(broker).env_names(suffix)


def get_broker_primary_env_name(broker: str, suffix: str) -> str:
    return get_broker_env_names(broker, suffix)[0]


def get_broker_env_value(broker: str, suffix: str, required: bool = True) -> str | None:
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


def get_broker_api_key(broker: str) -> str:
    return get_broker_env_value(broker, "API_KEY") or ""


def get_broker_api_secret(broker: str) -> str:
    return get_broker_env_value(broker, "API_SECRET") or ""


def get_broker_access_token(broker: str) -> str:
    return get_broker_env_value(broker, "ACCESS_TOKEN") or ""


def get_broker_redirect_uri(broker: str, required: bool = False) -> str | None:
    return get_broker_env_value(broker, "REDIRECT_URI", required=required)


def get_api_key() -> str:
    return get_broker_api_key("KITE")


def get_api_secret() -> str:
    return get_broker_api_secret("KITE")


def get_access_token() -> str:
    return get_broker_access_token("KITE")


def get_upstox_access_token() -> str:
    return get_broker_access_token("UPSTOX")


def get_upstox_api_key() -> str:
    return get_broker_api_key("UPSTOX")


def get_upstox_api_secret() -> str:
    return get_broker_api_secret("UPSTOX")


def get_upstox_static_ip() -> str | None:
    return os.getenv("UPSTOX_STATIC_IP")


def get_broker_ip_mode() -> str:
    return os.getenv("BROKER_IP_MODE", "IPV4_ONLY").upper()


def get_default_data_provider() -> str:
    return os.getenv("DATA_PROVIDER", "YFINANCE").upper()


def get_default_execution_provider() -> str:
    return os.getenv("EXECUTION_PROVIDER", "KITE").upper()


@dataclass(frozen=True)
class StrategyConfig:
    min_candles: dict[str, int]

    def validate(self) -> None:
        if not self.min_candles:
            raise ValueError("strategy.min_candles cannot be empty")
        for name, value in self.min_candles.items():
            if int(value) < 1:
                raise ValueError(f"strategy.min_candles[{name}] must be >= 1")


@dataclass(frozen=True)
class ExecutionSafetyConfig:
    min_ranked_candidate_score: float
    intraday_equity_auto_normal_min_confirmations: int
    reversal_exit_confirmation_candles: int
    trailing_activation_stop_distance_multiplier: float
    intraday_equity_entry_cutoff_minutes_before_squareoff: int

    def validate(self) -> None:
        if self.min_ranked_candidate_score < 0:
            raise ValueError("execution_safety.min_ranked_candidate_score must be >= 0")
        if self.intraday_equity_auto_normal_min_confirmations < 1:
            raise ValueError(
                "execution_safety.intraday_equity_auto_normal_min_confirmations must be >= 1"
            )
        if self.reversal_exit_confirmation_candles < 1:
            raise ValueError(
                "execution_safety.reversal_exit_confirmation_candles must be >= 1"
            )
        if self.trailing_activation_stop_distance_multiplier < 0:
            raise ValueError(
                "execution_safety.trailing_activation_stop_distance_multiplier must be >= 0"
            )
        if self.intraday_equity_entry_cutoff_minutes_before_squareoff < 0:
            raise ValueError(
                "execution_safety.intraday_equity_entry_cutoff_minutes_before_squareoff must be >= 0"
            )


@dataclass(frozen=True)
class TransactionCostConfig:
    enabled: bool
    slippage_pct_per_side: float
    expected_edge_score_multiplier: float
    min_edge_to_cost_ratio: float
    cost_edge_buffer_rupees: float

    def validate(self) -> None:
        if self.slippage_pct_per_side < 0:
            raise ValueError("transaction_costs.slippage_pct_per_side must be >= 0")
        if self.expected_edge_score_multiplier < 0:
            raise ValueError(
                "transaction_costs.expected_edge_score_multiplier must be >= 0"
            )
        if self.min_edge_to_cost_ratio < 0:
            raise ValueError("transaction_costs.min_edge_to_cost_ratio must be >= 0")
        if self.cost_edge_buffer_rupees < 0:
            raise ValueError("transaction_costs.cost_edge_buffer_rupees must be >= 0")


@dataclass(frozen=True)
class DataCacheConfig:
    enabled: bool
    ttl_seconds: int
    max_entries: int
    per_cycle_enabled: bool

    def validate(self) -> None:
        if self.ttl_seconds < 0:
            raise ValueError("data_cache.ttl_seconds must be >= 0")
        if self.max_entries < 1:
            raise ValueError("data_cache.max_entries must be >= 1")


@dataclass(frozen=True)
class OrderValidationConfig:
    enabled: bool
    allowed_products: tuple[str, ...]
    allowed_order_types: tuple[str, ...]
    min_quantity: int
    max_live_order_notional: float
    reconcile_attempts: int
    reconcile_delay_seconds: float
    fill_confirmation_required: bool
    default_entry_order_type: str
    entry_limit_price_buffer_pct: float
    max_spread_pct: float
    margin_check_enabled: bool
    margin_buffer_pct: float
    partial_fill_retry_enabled: bool
    partial_fill_retry_attempts: int
    rejection_retry_enabled: bool
    rejection_retry_attempts: int
    rejection_retry_reduce_quantity_pct: float
    rejection_retry_price_buffer_pct: float

    def validate(self) -> None:
        if self.min_quantity < 1:
            raise ValueError("orders.min_quantity must be >= 1")
        if self.max_live_order_notional < 0:
            raise ValueError("orders.max_live_order_notional must be >= 0")
        if self.reconcile_attempts < 1:
            raise ValueError("orders.reconcile_attempts must be >= 1")
        if self.reconcile_delay_seconds < 0:
            raise ValueError("orders.reconcile_delay_seconds must be >= 0")
        if self.margin_buffer_pct < 0:
            raise ValueError("orders.margin_buffer_pct must be >= 0")
        if not self.allowed_products:
            raise ValueError("orders.allowed_products cannot be empty")
        if not self.allowed_order_types:
            raise ValueError("orders.allowed_order_types cannot be empty")
        if self.default_entry_order_type not in self.allowed_order_types:
            raise ValueError("orders.default_entry_order_type must be allowed")
        if self.entry_limit_price_buffer_pct < 0:
            raise ValueError("orders.entry_limit_price_buffer_pct must be >= 0")
        if self.max_spread_pct < 0:
            raise ValueError("orders.max_spread_pct must be >= 0")
        if self.partial_fill_retry_attempts < 0:
            raise ValueError("orders.partial_fill_retry_attempts must be >= 0")
        if self.rejection_retry_attempts < 0:
            raise ValueError("orders.rejection_retry_attempts must be >= 0")
        if not 0 <= self.rejection_retry_reduce_quantity_pct < 1:
            raise ValueError("orders.rejection_retry_reduce_quantity_pct must be between 0 and 1")
        if self.rejection_retry_price_buffer_pct < 0:
            raise ValueError("orders.rejection_retry_price_buffer_pct must be >= 0")


@dataclass(frozen=True)
class TradeStoreConfig:
    enabled: bool
    base_dir: str
    include_paper_trades: bool

    def validate(self) -> None:
        if not self.base_dir.strip():
            raise ValueError("trade_store.base_dir cannot be blank")


@dataclass(frozen=True)
class LoggingConfig:
    file_name: str
    level: str

    def validate(self) -> None:
        if not self.file_name.strip():
            raise ValueError("logging.file_name cannot be blank")
        if not self.level.strip():
            raise ValueError("logging.level cannot be blank")


@dataclass(frozen=True)
class UniverseConfig:
    nifty50_symbols: list[str]
    manual_symbol_table: dict[str, str]
    single_symbol_table: dict[str, str]
    only_manage_configured_symbols: bool

    def validate(self) -> None:
        if not self.nifty50_symbols:
            raise ValueError("universe.nifty50_symbols cannot be empty")
        if not self.manual_symbol_table:
            raise ValueError("universe.manual_symbol_table cannot be empty")
        if not self.single_symbol_table:
            raise ValueError("universe.single_symbol_table cannot be empty")


@dataclass(frozen=True)
class FnoConfig:
    underlying_details: dict[str, dict[str, str]]
    auto_rollover_days: int
    default_risk_free_rate: float
    greeks_history_period: str
    intraday_options_max_trades_per_underlying: int
    intraday_options_expiry_warning_days: int
    intraday_options_vega_crush_block_percent: float
    intraday_options_min_range_pct: float
    intraday_options_min_signal_score: float
    intraday_options_max_hold_minutes: int
    intraday_options_time_exit_cutoff: str
    intraday_options_iv_expansion_max_iv_percentile: float
    intraday_options_sideways_vwap_band_pct: float
    intraday_options_sideways_lookback_candles: int
    intraday_options_regime_expansion_range_pct: float
    intraday_options_regime_sideways_range_pct: float
    intraday_options_regime_sideways_vwap_dev_pct: float
    intraday_options_regime_expansion_iv_change_pct: float
    intraday_options_roll_trigger_pct: float
    intraday_options_theta_exit_ratio: float
    intraday_options_theta_exit_min_minutes: int

    def validate(self) -> None:
        if not self.underlying_details:
            raise ValueError("fno.underlying_details cannot be empty")
        if self.auto_rollover_days < 0:
            raise ValueError("fno.auto_rollover_days must be >= 0")
        if self.default_risk_free_rate < 0:
            raise ValueError("fno.default_risk_free_rate must be >= 0")
        if self.intraday_options_max_trades_per_underlying < 1:
            raise ValueError(
                "fno.intraday_options_max_trades_per_underlying must be >= 1"
            )
        if self.intraday_options_expiry_warning_days < 0:
            raise ValueError(
                "fno.intraday_options_expiry_warning_days must be >= 0"
            )
        if self.intraday_options_min_range_pct < 0:
            raise ValueError("fno.intraday_options_min_range_pct must be >= 0")
        if self.intraday_options_min_signal_score < 0:
            raise ValueError("fno.intraday_options_min_signal_score must be >= 0")
        if self.intraday_options_max_hold_minutes < 0:
            raise ValueError("fno.intraday_options_max_hold_minutes must be >= 0")
        if self.intraday_options_roll_trigger_pct < 0:
            raise ValueError("fno.intraday_options_roll_trigger_pct must be >= 0")
        if self.intraday_options_theta_exit_ratio < 0:
            raise ValueError("fno.intraday_options_theta_exit_ratio must be >= 0")
        if self.intraday_options_theta_exit_min_minutes < 0:
            raise ValueError("fno.intraday_options_theta_exit_min_minutes must be >= 0")


@dataclass(frozen=True)
class RuntimeConfig:
    strategy: StrategyConfig
    execution_safety: ExecutionSafetyConfig
    transaction_costs: TransactionCostConfig
    data_cache: DataCacheConfig
    orders: OrderValidationConfig
    trade_store: TradeStoreConfig
    logging: LoggingConfig
    universe: UniverseConfig
    fno: FnoConfig

    def validate(self) -> None:
        self.strategy.validate()
        self.execution_safety.validate()
        self.transaction_costs.validate()
        self.data_cache.validate()
        self.orders.validate()
        self.trade_store.validate()
        self.logging.validate()
        self.universe.validate()
        self.fno.validate()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_runtime_config_map() -> dict[str, Any]:
    return {
        "strategy": {
            "min_candles": {
                "MA": 50,
                "RSI": 14,
                "BREAKOUT": 20,
                "VWAP": 1,
                "ORB": 20,
                "DELTA": 1,
                "IV": 1,
            }
        },
        "execution_safety": {
            "min_ranked_candidate_score": float(
                os.getenv("MIN_RANKED_CANDIDATE_SCORE", "0.008")
            ),
            "intraday_equity_auto_normal_min_confirmations": int(
                os.getenv("INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS", "2")
            ),
            "reversal_exit_confirmation_candles": int(
                os.getenv("REVERSAL_EXIT_CONFIRMATION_CANDLES", "2")
            ),
            "trailing_activation_stop_distance_multiplier": float(
                os.getenv("TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER", "0.5")
            ),
            "intraday_equity_entry_cutoff_minutes_before_squareoff": int(
                os.getenv(
                    "INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF",
                    "30",
                )
            ),
        },
        "transaction_costs": {
            "enabled": _parse_bool(
                os.getenv("TRANSACTION_COST_MODEL_ENABLED", "1"),
                default=True,
            ),
            "slippage_pct_per_side": float(
                os.getenv("TRANSACTION_SLIPPAGE_PCT_PER_SIDE", "0.0002")
            ),
            "expected_edge_score_multiplier": float(
                os.getenv("EXPECTED_EDGE_SCORE_MULTIPLIER", "1.0")
            ),
            "min_edge_to_cost_ratio": float(
                os.getenv("MIN_EDGE_TO_COST_RATIO", "1.2")
            ),
            "cost_edge_buffer_rupees": float(
                os.getenv("COST_EDGE_BUFFER_RUPEES", "5.0")
            ),
        },
        "data_cache": {
            "enabled": _parse_bool(os.getenv("DATA_CACHE_ENABLED", "1"), default=True),
            "ttl_seconds": int(os.getenv("DATA_CACHE_TTL_SECONDS", "20")),
            "max_entries": int(os.getenv("DATA_CACHE_MAX_ENTRIES", "512")),
            "per_cycle_enabled": _parse_bool(
                os.getenv("DATA_CACHE_PER_CYCLE_ENABLED", "1"),
                default=True,
            ),
        },
        "orders": {
            "enabled": _parse_bool(
                os.getenv("ORDER_VALIDATION_ENABLED", "1"),
                default=True,
            ),
            "allowed_products": ("MIS", "CNC", "NRML"),
            "allowed_order_types": ("MARKET", "LIMIT", "SL", "SL-M"),
            "min_quantity": int(os.getenv("ORDER_MIN_QUANTITY", "1")),
            "max_live_order_notional": float(
                os.getenv("ORDER_MAX_LIVE_ORDER_NOTIONAL", "0")
            ),
            "reconcile_attempts": int(os.getenv("ORDER_RECONCILE_ATTEMPTS", "3")),
            "reconcile_delay_seconds": float(
                os.getenv("ORDER_RECONCILE_DELAY_SECONDS", "1.5")
            ),
            "fill_confirmation_required": _parse_bool(
                os.getenv("ORDER_FILL_CONFIRMATION_REQUIRED", "1"),
                default=True,
            ),
            "default_entry_order_type": os.getenv(
                "ORDER_DEFAULT_ENTRY_ORDER_TYPE", "MARKET"
            ).upper(),
            "entry_limit_price_buffer_pct": float(
                os.getenv("ORDER_ENTRY_LIMIT_PRICE_BUFFER_PCT", "0")
            ),
            "max_spread_pct": float(os.getenv("ORDER_MAX_SPREAD_PCT", "0.05")),
            "margin_check_enabled": _parse_bool(
                os.getenv("ORDER_MARGIN_CHECK_ENABLED", "1"),
                default=True,
            ),
            "margin_buffer_pct": float(os.getenv("ORDER_MARGIN_BUFFER_PCT", "0.05")),
            "partial_fill_retry_enabled": _parse_bool(
                os.getenv("ORDER_PARTIAL_FILL_RETRY_ENABLED", "1"),
                default=True,
            ),
            "partial_fill_retry_attempts": int(
                os.getenv("ORDER_PARTIAL_FILL_RETRY_ATTEMPTS", "1")
            ),
            "rejection_retry_enabled": _parse_bool(
                os.getenv("ORDER_REJECTION_RETRY_ENABLED", "1"),
                default=True,
            ),
            "rejection_retry_attempts": int(
                os.getenv("ORDER_REJECTION_RETRY_ATTEMPTS", "2")
            ),
            "rejection_retry_reduce_quantity_pct": float(
                os.getenv("ORDER_REJECTION_RETRY_REDUCE_QUANTITY_PCT", "0.25")
            ),
            "rejection_retry_price_buffer_pct": float(
                os.getenv("ORDER_REJECTION_RETRY_PRICE_BUFFER_PCT", "0.002")
            ),
        },
        "trade_store": {
            "enabled": _parse_bool(os.getenv("TRADE_STORE_ENABLED", "1"), default=True),
            "base_dir": os.getenv("TRADE_STORE_DIR", "state/trade_store"),
            "include_paper_trades": _parse_bool(
                os.getenv("TRADE_STORE_INCLUDE_PAPER", "1"),
                default=True,
            ),
        },
        "logging": {
            "file_name": "algo.log",
            "level": os.getenv("LOG_LEVEL", "INFO").upper(),
        },
        "universe": {
            "nifty50_symbols": [
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
            ],
            "manual_symbol_table": {
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
                "11": "IRB.NS",
                "12": "JPPOWER.NS",
                "13": "RPOWER.NS",
            },
            "single_symbol_table": {
                "1": "HPCL.NS",
                "2": "IOC.NS",
                "3": "SAIL.NS",
                "4": "JINDALSTEL.NS",
                "5": "AARTI.NS",
                "6": "CUMMINSIND.NS",
                "7": "WABCOINDIA.NS",
                "8": "PNBHOUSING.NS",
                "9": "IDFCBANK.NS",
                "10": "MOIL.NS",
                "11": "RPOWER.NS",
                "12": "JPPOWER.NS",
                "13": "IRB.NS",
            },
            "only_manage_configured_symbols": True,
        },
        "fno": {
            "underlying_details": {
                "NIFTY": {
                    "display_name": "NIFTY 50",
                    "derivatives_exchange": "NFO",
                    "spot_quote_symbol": "NSE:NIFTY 50",
                },
                "SENSEX": {
                    "display_name": "SENSEX",
                    "derivatives_exchange": "BFO",
                    "spot_quote_symbol": "BSE:SENSEX",
                },
            },
            "auto_rollover_days": int(os.getenv("FNO_AUTO_ROLLOVER_DAYS", "1")),
            "default_risk_free_rate": float(
                os.getenv("FNO_DEFAULT_RISK_FREE_RATE", "0.06")
            ),
            "greeks_history_period": os.getenv("FNO_GREEKS_HISTORY_PERIOD", "1mo"),
            "intraday_options_max_trades_per_underlying": int(
                os.getenv("INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING", "4")
            ),
            "intraday_options_expiry_warning_days": int(
                os.getenv("INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS", "2")
            ),
            "intraday_options_vega_crush_block_percent": float(
                os.getenv("INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT", "20")
            ),
            "intraday_options_min_range_pct": float(
                os.getenv("INTRADAY_OPTIONS_MIN_RANGE_PCT", "0.35")
            ),
            "intraday_options_min_signal_score": float(
                os.getenv("INTRADAY_OPTIONS_MIN_SIGNAL_SCORE", "0.03")
            ),
            "intraday_options_max_hold_minutes": int(
                os.getenv("INTRADAY_OPTIONS_MAX_HOLD_MINUTES", "60")
            ),
            "intraday_options_time_exit_cutoff": os.getenv(
                "INTRADAY_OPTIONS_TIME_EXIT_CUTOFF",
                "14:45",
            ),
            "intraday_options_iv_expansion_max_iv_percentile": float(
                os.getenv("INTRADAY_OPTIONS_IV_EXPANSION_MAX_IV_PERCENTILE", "20")
            ),
            "intraday_options_sideways_vwap_band_pct": float(
                os.getenv("INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT", "0.0015")
            ),
            "intraday_options_sideways_lookback_candles": int(
                os.getenv("INTRADAY_OPTIONS_SIDEWAYS_LOOKBACK_CANDLES", "8")
            ),
            "intraday_options_regime_expansion_range_pct": float(
                os.getenv("INTRADAY_OPTIONS_REGIME_EXPANSION_RANGE_PCT", "1.10")
            ),
            "intraday_options_regime_sideways_range_pct": float(
                os.getenv("INTRADAY_OPTIONS_REGIME_SIDEWAYS_RANGE_PCT", "0.55")
            ),
            "intraday_options_regime_sideways_vwap_dev_pct": float(
                os.getenv("INTRADAY_OPTIONS_REGIME_SIDEWAYS_VWAP_DEV_PCT", "0.0025")
            ),
            "intraday_options_regime_expansion_iv_change_pct": float(
                os.getenv("INTRADAY_OPTIONS_REGIME_EXPANSION_IV_CHANGE_PCT", "2.0")
            ),
            "intraday_options_roll_trigger_pct": float(
                os.getenv("INTRADAY_OPTIONS_ROLL_TRIGGER_PCT", "2.0")
            ),
            "intraday_options_theta_exit_ratio": float(
                os.getenv("INTRADAY_OPTIONS_THETA_EXIT_RATIO", "0.08")
            ),
            "intraday_options_theta_exit_min_minutes": int(
                os.getenv("INTRADAY_OPTIONS_THETA_EXIT_MIN_MINUTES", "10")
            ),
        },
    }


def _build_runtime_config() -> RuntimeConfig:
    merged = _deep_merge(_default_runtime_config_map(), _load_runtime_overrides())
    config = RuntimeConfig(
        strategy=StrategyConfig(**merged["strategy"]),
        execution_safety=ExecutionSafetyConfig(**merged["execution_safety"]),
        transaction_costs=TransactionCostConfig(**merged["transaction_costs"]),
        data_cache=DataCacheConfig(**merged["data_cache"]),
        orders=OrderValidationConfig(
            allowed_products=tuple(merged["orders"]["allowed_products"]),
            allowed_order_types=tuple(merged["orders"]["allowed_order_types"]),
            enabled=bool(merged["orders"]["enabled"]),
            min_quantity=int(merged["orders"]["min_quantity"]),
            max_live_order_notional=float(merged["orders"]["max_live_order_notional"]),
            reconcile_attempts=int(merged["orders"]["reconcile_attempts"]),
            reconcile_delay_seconds=float(merged["orders"]["reconcile_delay_seconds"]),
            fill_confirmation_required=bool(
                merged["orders"]["fill_confirmation_required"]
            ),
            default_entry_order_type=str(
                merged["orders"]["default_entry_order_type"]
            ).upper(),
            entry_limit_price_buffer_pct=float(
                merged["orders"]["entry_limit_price_buffer_pct"]
            ),
            max_spread_pct=float(merged["orders"]["max_spread_pct"]),
            margin_check_enabled=bool(merged["orders"]["margin_check_enabled"]),
            margin_buffer_pct=float(merged["orders"]["margin_buffer_pct"]),
            partial_fill_retry_enabled=bool(
                merged["orders"]["partial_fill_retry_enabled"]
            ),
            partial_fill_retry_attempts=int(
                merged["orders"]["partial_fill_retry_attempts"]
            ),
            rejection_retry_enabled=bool(
                merged["orders"]["rejection_retry_enabled"]
            ),
            rejection_retry_attempts=int(
                merged["orders"]["rejection_retry_attempts"]
            ),
            rejection_retry_reduce_quantity_pct=float(
                merged["orders"]["rejection_retry_reduce_quantity_pct"]
            ),
            rejection_retry_price_buffer_pct=float(
                merged["orders"]["rejection_retry_price_buffer_pct"]
            ),
        ),
        trade_store=TradeStoreConfig(**merged["trade_store"]),
        logging=LoggingConfig(**merged["logging"]),
        universe=UniverseConfig(**merged["universe"]),
        fno=FnoConfig(**merged["fno"]),
    )
    config.validate()
    return config


RUNTIME_CONFIG = _build_runtime_config()


def get_runtime_config() -> RuntimeConfig:
    return RUNTIME_CONFIG


API_KEY = _get_first_env_value(get_broker_env_names("KITE", "API_KEY"))
ACCESS_TOKEN = _get_first_env_value(get_broker_env_names("KITE", "ACCESS_TOKEN"))
UPSTOX_ACCESS_TOKEN = _get_first_env_value(
    get_broker_env_names("UPSTOX", "ACCESS_TOKEN")
)

MIN_CANDLES = RUNTIME_CONFIG.strategy.min_candles

MIN_RANKED_CANDIDATE_SCORE = (
    RUNTIME_CONFIG.execution_safety.min_ranked_candidate_score
)
INTRADAY_EQUITY_AUTO_NORMAL_MIN_CONFIRMATIONS = (
    RUNTIME_CONFIG.execution_safety.intraday_equity_auto_normal_min_confirmations
)
REVERSAL_EXIT_CONFIRMATION_CANDLES = (
    RUNTIME_CONFIG.execution_safety.reversal_exit_confirmation_candles
)
TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER = (
    RUNTIME_CONFIG.execution_safety.trailing_activation_stop_distance_multiplier
)
INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF = (
    RUNTIME_CONFIG.execution_safety.intraday_equity_entry_cutoff_minutes_before_squareoff
)

TRANSACTION_COST_MODEL_ENABLED = RUNTIME_CONFIG.transaction_costs.enabled
TRANSACTION_SLIPPAGE_PCT_PER_SIDE = (
    RUNTIME_CONFIG.transaction_costs.slippage_pct_per_side
)
EXPECTED_EDGE_SCORE_MULTIPLIER = (
    RUNTIME_CONFIG.transaction_costs.expected_edge_score_multiplier
)
MIN_EDGE_TO_COST_RATIO = RUNTIME_CONFIG.transaction_costs.min_edge_to_cost_ratio
COST_EDGE_BUFFER_RUPEES = RUNTIME_CONFIG.transaction_costs.cost_edge_buffer_rupees

NIFTY50_SYMBOLS = RUNTIME_CONFIG.universe.nifty50_symbols
MANUAL_SYMBOL_TABLE = RUNTIME_CONFIG.universe.manual_symbol_table
SINGLE_SYMBOL_TABLE = RUNTIME_CONFIG.universe.single_symbol_table
ONLY_MANAGE_CONFIGURED_SYMBOLS = (
    RUNTIME_CONFIG.universe.only_manage_configured_symbols
)

LOG_FILE = RUNTIME_CONFIG.logging.file_name
LOG_LEVEL = RUNTIME_CONFIG.logging.level

FNO_UNDERLYING_DETAILS = RUNTIME_CONFIG.fno.underlying_details
FNO_INDEX_SYMBOLS = list(FNO_UNDERLYING_DETAILS)
FNO_AUTO_ROLLOVER_DAYS = RUNTIME_CONFIG.fno.auto_rollover_days
FNO_DEFAULT_RISK_FREE_RATE = RUNTIME_CONFIG.fno.default_risk_free_rate
FNO_GREEKS_HISTORY_PERIOD = RUNTIME_CONFIG.fno.greeks_history_period
INTRADAY_OPTIONS_MAX_TRADES_PER_UNDERLYING = (
    RUNTIME_CONFIG.fno.intraday_options_max_trades_per_underlying
)
INTRADAY_OPTIONS_EXPIRY_WARNING_DAYS = (
    RUNTIME_CONFIG.fno.intraday_options_expiry_warning_days
)
INTRADAY_OPTIONS_VEGA_CRUSH_BLOCK_PERCENT = (
    RUNTIME_CONFIG.fno.intraday_options_vega_crush_block_percent
)
INTRADAY_OPTIONS_MIN_RANGE_PCT = RUNTIME_CONFIG.fno.intraday_options_min_range_pct
INTRADAY_OPTIONS_MIN_SIGNAL_SCORE = (
    RUNTIME_CONFIG.fno.intraday_options_min_signal_score
)
INTRADAY_OPTIONS_MAX_HOLD_MINUTES = (
    RUNTIME_CONFIG.fno.intraday_options_max_hold_minutes
)
INTRADAY_OPTIONS_TIME_EXIT_CUTOFF = (
    RUNTIME_CONFIG.fno.intraday_options_time_exit_cutoff
)
INTRADAY_OPTIONS_IV_EXPANSION_MAX_IV_PERCENTILE = (
    RUNTIME_CONFIG.fno.intraday_options_iv_expansion_max_iv_percentile
)
INTRADAY_OPTIONS_SIDEWAYS_VWAP_BAND_PCT = (
    RUNTIME_CONFIG.fno.intraday_options_sideways_vwap_band_pct
)
INTRADAY_OPTIONS_SIDEWAYS_LOOKBACK_CANDLES = (
    RUNTIME_CONFIG.fno.intraday_options_sideways_lookback_candles
)
INTRADAY_OPTIONS_REGIME_EXPANSION_RANGE_PCT = (
    RUNTIME_CONFIG.fno.intraday_options_regime_expansion_range_pct
)
INTRADAY_OPTIONS_REGIME_SIDEWAYS_RANGE_PCT = (
    RUNTIME_CONFIG.fno.intraday_options_regime_sideways_range_pct
)
INTRADAY_OPTIONS_REGIME_SIDEWAYS_VWAP_DEV_PCT = (
    RUNTIME_CONFIG.fno.intraday_options_regime_sideways_vwap_dev_pct
)
INTRADAY_OPTIONS_REGIME_EXPANSION_IV_CHANGE_PCT = (
    RUNTIME_CONFIG.fno.intraday_options_regime_expansion_iv_change_pct
)
INTRADAY_OPTIONS_ROLL_TRIGGER_PCT = (
    RUNTIME_CONFIG.fno.intraday_options_roll_trigger_pct
)
INTRADAY_OPTIONS_THETA_EXIT_RATIO = (
    RUNTIME_CONFIG.fno.intraday_options_theta_exit_ratio
)
INTRADAY_OPTIONS_THETA_EXIT_MIN_MINUTES = (
    RUNTIME_CONFIG.fno.intraday_options_theta_exit_min_minutes
)
