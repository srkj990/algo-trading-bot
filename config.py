from __future__ import annotations

import ast
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _load_dotenv(path: str = ".env") -> None:
    # Resolve relative paths against this file's directory so runners started
    # from a subdirectory (e.g. runners/06_intraday_options/) still find the
    # project-root .env.
    resolved = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    candidates = [path, resolved]
    for candidate in candidates:
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as env_file:
                for raw_line in env_file:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            return


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
    _config_dir = Path(__file__).parent
    candidates = (
        _config_dir / "config" / "config.runtime.yaml",
        _config_dir / "config" / "config.runtime.yml",
        _config_dir / "config.runtime.yaml",
        _config_dir / "config.runtime.yml",
        Path("config/config.runtime.yaml"),
        Path("config/config.runtime.yml"),
        Path("config.runtime.yaml"),
        Path("config.runtime.yml"),
    )
    for path in candidates:
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


def get_broker_request_timeout_seconds() -> float:
    raw_value = os.getenv("BROKER_REQUEST_TIMEOUT_SECONDS", "30").strip()
    try:
        timeout_seconds = float(raw_value)
    except ValueError:
        return 30.0
    return timeout_seconds if timeout_seconds > 0 else 30.0


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
    exit_mode: str = "TRAIL_ONLY"
    default_execution_mode: str = "LIVE"

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
        if self.exit_mode not in {"TRAIL_ONLY", "HARD_TARGET"}:
            raise ValueError("execution_safety.exit_mode must be TRAIL_ONLY or HARD_TARGET")
        if self.default_execution_mode not in {"LIVE", "PAPER"}:
            raise ValueError("execution_safety.default_execution_mode must be LIVE or PAPER")


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
class SessionDefaultsConfig:
    exit_only_default: bool
    live_broker_resync_interval_seconds: int
    paper_trading_override: bool = False  # bypass weekend + market-hours checks for paper testing

    def validate(self) -> None:
        if self.live_broker_resync_interval_seconds < 0:
            raise ValueError("session_defaults.live_broker_resync_interval_seconds must be >= 0")


@dataclass(frozen=True)
class RiskControlsConfig:
    daily_max_loss_pct: float
    consecutive_loss_limit: int
    api_failure_pause_minutes: int
    max_orders_per_minute: int
    abnormal_slippage_pause_pct: float

    def validate(self) -> None:
        if self.daily_max_loss_pct < 0:
            raise ValueError("risk_controls.daily_max_loss_pct must be >= 0")
        if self.consecutive_loss_limit < 0:
            raise ValueError("risk_controls.consecutive_loss_limit must be >= 0")
        if self.api_failure_pause_minutes < 0:
            raise ValueError("risk_controls.api_failure_pause_minutes must be >= 0")
        if self.max_orders_per_minute < 0:
            raise ValueError("risk_controls.max_orders_per_minute must be >= 0")
        if self.abnormal_slippage_pause_pct < 0:
            raise ValueError("risk_controls.abnormal_slippage_pause_pct must be >= 0")


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
    exit_limit_price_buffer_pct: float
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
    intraday_options_market_open_hour: int
    intraday_options_market_open_minute: int
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
    intraday_options_lot_mode: str
    intraday_options_entry_mode: str
    intraday_options_max_entry_cost_ratio: float
    intraday_options_max_spread_pct: float
    intraday_options_min_open_interest: int
    intraday_options_roll_trigger_pct: float
    intraday_options_theta_exit_ratio: float
    intraday_options_theta_exit_min_minutes: int
    intraday_options_sell_margin_pct: float
    intraday_options_seller_max_adx: float
    intraday_options_seller_adx_period: int
    intraday_options_seller_target_decay_pct: float
    intraday_options_seller_stop_pct: float
    intraday_options_seller_min_iv_percentile: float
    intraday_options_seller_max_delta: float

    def validate(self) -> None:
        if not self.underlying_details:
            raise ValueError("fno.underlying_details cannot be empty")
        if self.auto_rollover_days < 0:
            raise ValueError("fno.auto_rollover_days must be >= 0")
        if self.default_risk_free_rate < 0:
            raise ValueError("fno.default_risk_free_rate must be >= 0")
        if not 0 <= self.intraday_options_market_open_hour <= 23:
            raise ValueError(
                "fno.intraday_options_market_open_hour must be between 0 and 23"
            )
        if not 0 <= self.intraday_options_market_open_minute <= 59:
            raise ValueError(
                "fno.intraday_options_market_open_minute must be between 0 and 59"
            )
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
        if self.intraday_options_lot_mode not in {"ONE_LOT", "CAPITAL_BASED"}:
            raise ValueError(
                "fno.intraday_options_lot_mode must be ONE_LOT or CAPITAL_BASED"
            )
        if self.intraday_options_entry_mode not in {"LIVE_STAGED", "LEGACY_IMMEDIATE", "LIVE_TICK_CONFIRM"}:
            raise ValueError(
                "fno.intraday_options_entry_mode must be LIVE_STAGED, LEGACY_IMMEDIATE, or LIVE_TICK_CONFIRM"
            )
        if self.intraday_options_max_entry_cost_ratio < 0:
            raise ValueError("fno.intraday_options_max_entry_cost_ratio must be >= 0")
        if self.intraday_options_max_spread_pct < 0:
            raise ValueError("fno.intraday_options_max_spread_pct must be >= 0")
        if self.intraday_options_min_open_interest < 0:
            raise ValueError("fno.intraday_options_min_open_interest must be >= 0")
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
    session_defaults: SessionDefaultsConfig
    risk_controls: RiskControlsConfig
    orders: OrderValidationConfig
    trade_store: TradeStoreConfig
    logging: LoggingConfig
    universe: UniverseConfig
    fno: FnoConfig
    engine_defaults: dict[str, Any]
    backtest_defaults: dict[str, Any]

    def validate(self) -> None:
        self.strategy.validate()
        self.execution_safety.validate()
        self.transaction_costs.validate()
        self.data_cache.validate()
        self.session_defaults.validate()
        self.risk_controls.validate()
        self.orders.validate()
        self.trade_store.validate()
        self.logging.validate()
        self.universe.validate()
        self.fno.validate()
        if not self.engine_defaults:
            raise ValueError("engine_defaults cannot be empty")
        if not self.backtest_defaults:
            raise ValueError("backtest_defaults cannot be empty")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_runtime_config_map() -> dict[str, Any]:
    return {
        "strategy": {
            "min_candles": {
                "MA": 50,
                "MA_LONG": 200,
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
                os.getenv("TRAILING_ACTIVATION_STOP_DISTANCE_MULTIPLIER", "1.0")
            ),
            "intraday_equity_entry_cutoff_minutes_before_squareoff": int(
                os.getenv(
                    "INTRADAY_EQUITY_ENTRY_CUTOFF_MINUTES_BEFORE_SQUAREOFF",
                    "30",
                )
            ),
            "exit_mode": os.getenv("EXECUTION_EXIT_MODE", "TRAIL_ONLY"),
            "default_execution_mode": os.getenv("DEFAULT_EXECUTION_MODE", "LIVE"),
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
        "session_defaults": {
            "exit_only_default": _parse_bool(
                os.getenv("EXIT_ONLY_DEFAULT", "0"),
                default=False,
            ),
            "live_broker_resync_interval_seconds": int(
                os.getenv("LIVE_BROKER_RESYNC_INTERVAL_SECONDS", "60")
            ),
            "paper_trading_override": _parse_bool(
                os.getenv("PAPER_TRADING_OVERRIDE", "0"),
                default=False,
            ),
        },
        "risk_controls": {
            "daily_max_loss_pct": float(
                os.getenv("RISK_DAILY_MAX_LOSS_PCT", "0.03")
            ),
            "consecutive_loss_limit": int(
                os.getenv("RISK_CONSECUTIVE_LOSS_LIMIT", "3")
            ),
            "api_failure_pause_minutes": int(
                os.getenv("RISK_API_FAILURE_PAUSE_MINUTES", "5")
            ),
            "max_orders_per_minute": int(
                os.getenv("RISK_MAX_ORDERS_PER_MINUTE", "10")
            ),
            "abnormal_slippage_pause_pct": float(
                os.getenv("RISK_ABNORMAL_SLIPPAGE_PAUSE_PCT", "0.5")
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
            "exit_limit_price_buffer_pct": float(
                os.getenv("ORDER_EXIT_LIMIT_PRICE_BUFFER_PCT", "0.01")
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
            "base_dir": os.getenv("TRADE_STORE_DIR", str(Path(__file__).parent / "state" / "trade_store")),
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
                "14": "SIEMENS.NS",
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
            "intraday_options_market_open_hour": int(
                os.getenv("INTRADAY_OPTIONS_MARKET_OPEN_HOUR", "9")
            ),
            "intraday_options_market_open_minute": int(
                os.getenv("INTRADAY_OPTIONS_MARKET_OPEN_MINUTE", "15")
            ),
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
            "intraday_options_lot_mode": os.getenv(
                "INTRADAY_OPTIONS_LOT_MODE",
                "CAPITAL_BASED",
            ).upper(),
            "intraday_options_entry_mode": os.getenv(
                "INTRADAY_OPTIONS_ENTRY_MODE",
                "LIVE_STAGED",
            ).upper(),
            "intraday_options_max_entry_cost_ratio": float(
                os.getenv("INTRADAY_OPTIONS_MAX_ENTRY_COST_RATIO", "0.30")
            ),
            "intraday_options_max_spread_pct": float(
                os.getenv("INTRADAY_OPTIONS_MAX_SPREAD_PCT", "0.02")
            ),
            "intraday_options_min_open_interest": int(
                os.getenv("INTRADAY_OPTIONS_MIN_OPEN_INTEREST", "1000")
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
            "intraday_options_sell_margin_pct": float(
                os.getenv("INTRADAY_OPTIONS_SELL_MARGIN_PCT", "0.12")
            ),
            "intraday_options_seller_max_adx": float(
                os.getenv("INTRADAY_OPTIONS_SELLER_MAX_ADX", "22.0")
            ),
            "intraday_options_seller_adx_period": int(
                os.getenv("INTRADAY_OPTIONS_SELLER_ADX_PERIOD", "14")
            ),
            "intraday_options_seller_target_decay_pct": float(
                os.getenv("INTRADAY_OPTIONS_SELLER_TARGET_DECAY_PCT", "30.0")
            ),
            "intraday_options_seller_stop_pct": float(
                os.getenv("INTRADAY_OPTIONS_SELLER_STOP_PCT", "60.0")
            ),
            "intraday_options_seller_min_iv_percentile": float(
                os.getenv("INTRADAY_OPTIONS_SELLER_MIN_IV_PERCENTILE", "10.0")
            ),
            "intraday_options_seller_max_delta": float(
                os.getenv("INTRADAY_OPTIONS_SELLER_MAX_DELTA", "0.45")
            ),
        },
        "engine_defaults": {
            "delivery_equity": {
                "max_symbol_allocation": 0.25,
                "nifty_trend_symbol": "NSE:NIFTY 50",
                "nifty_trend_ma_window": 50,
                "max_hold_days": 5,
                "sleep_seconds": 300,
                "cooldown_seconds": 0,
                "data_period": "1y",
                "data_interval": "1d",
                "adaptive_stop_multiplier_sideways": 2.4,
                "adaptive_stop_multiplier_normal": 2.0,
                "adaptive_stop_multiplier_expansion": 1.8,
                "adaptive_target_multiplier_sideways": 1.6,
                "adaptive_target_multiplier_normal": 2.6,
                "adaptive_target_multiplier_expansion": 3.4,
                "adaptive_trailing_multiplier_sideways": 1.2,
                "adaptive_trailing_multiplier_normal": 1.5,
                "adaptive_trailing_multiplier_expansion": 1.8,
                "adaptive_min_stop_pct": 0.018,
                "adaptive_min_target_pct": 0.040,
                "adaptive_min_trailing_pct": 0.012,
                "adaptive_conviction_score_weight": 0.75,
                "volatility_trailing_range_multiplier": 1.2,
            },
            "intraday_equity": {
                "gap_threshold_percent": 1.0,
                "opening_range_candles": 15,
                "breakout_volume_multiplier": 1.2,
                "square_off_time": "15:15",
                "sleep_seconds": 60,
                "cooldown_seconds": 300,
                "data_period": "1d",
                "data_interval": "1m",
                "adaptive_stop_multiplier_sideways": 1.6,
                "adaptive_stop_multiplier_normal": 1.4,
                "adaptive_stop_multiplier_expansion": 1.4,
                "adaptive_target_multiplier_sideways": 1.1,
                "adaptive_target_multiplier_normal": 1.6,
                "adaptive_target_multiplier_expansion": 2.2,
                "adaptive_trailing_multiplier_sideways": 0.8,
                "adaptive_trailing_multiplier_normal": 1.0,
                "adaptive_trailing_multiplier_expansion": 1.15,
                "adaptive_min_stop_pct": 0.0025,
                "adaptive_min_target_pct": 0.0045,
                "adaptive_min_trailing_pct": 0.002,
                "adaptive_conviction_score_weight": 0.5,
            },
            "futures_equity": {
                "max_symbol_allocation": 0.25,
                "sleep_seconds": 60,
                "cooldown_seconds": 300,
                "data_period": "3mo",
                "data_interval": "5m",
            },
            "options_equity": {
                "max_symbol_allocation": 0.25,
                "sleep_seconds": 60,
                "cooldown_seconds": 300,
                "data_period": "2mo",
                "data_interval": "15m",
            },
            "intraday_futures": {
                "max_symbol_allocation": 0.35,
                "entry_cutoff": "15:05",
                "square_off_time": "15:15",
                "sleep_seconds": 60,
                "cooldown_seconds": 180,
                "data_period": "15d",
                "data_interval": "3m",
            },
            "intraday_options": {
                "max_symbol_allocation": 0.2,
                "min_contract_price": 8.0,
                "min_abs_delta": 0.2,
                "max_buy_iv_percentile": 85.0,
                "min_sell_iv_percentile": 15.0,
                "entry_cutoff": "15:05",
                "square_off_time": "15:15",
                "sleep_seconds": 5,
                "cooldown_seconds": 180,
                "data_period": "1d",
                "data_interval": "1m",
                "adaptive_stop_multiplier_sideways": 2.0,
                "adaptive_stop_multiplier_normal": 1.7,
                "adaptive_stop_multiplier_expansion": 1.7,
                "adaptive_target_multiplier_sideways": 1.1,
                "adaptive_target_multiplier_normal": 1.7,
                "adaptive_target_multiplier_expansion": 2.3,
                "adaptive_trailing_multiplier_sideways": 0.8,
                "adaptive_trailing_multiplier_normal": 1.0,
                "adaptive_trailing_multiplier_expansion": 1.15,
                "adaptive_min_stop_pct": 0.05,
                "adaptive_min_target_pct": 0.08,
                "adaptive_min_trailing_pct": 0.035,
                "adaptive_conviction_score_weight": 0.5,
                "momentum_volume_multiplier": 1.5,
                "momentum_spike_multiplier": 2.0,
                "momentum_min_body_ratio": 0.6,
                "momentum_quality_lookback": 20,
                "momentum_fast_ema_span": 9,
                "momentum_confirmation_timeout_candles": 3,
                "momentum_pullback_timeout_candles": 5,
                "momentum_pullback_band_pct": 0.0035,
                "mean_reversion_max_body_ratio": 0.55,
                "mean_reversion_spike_multiplier": 1.4,
                "mean_reversion_retest_band_pct": 0.0035,
                "mean_reversion_quality_lookback": 20,
                "volatility_min_body_ratio": 0.45,
                "volatility_range_multiplier": 1.2,
                "volatility_quality_lookback": 20,
                "runner_level_exit_fractions": [0.3, 0.4, 0.3],
                "runner_partial_exit_lot_threshold": 4,
                "runner_level1_premium_target_pct": 12.0,
                "runner_level2_premium_target_pct": 25.0,
                "runner_partial_exit_fraction": 0.15,
            },
        },
        "backtest_defaults": {
            "risk_styles": {
                "intraday": {
                    "1": {
                        "name": "CONSERVATIVE",
                        "atr_stop_multiplier": 1.2,
                        "trailing_atr_multiplier": 0.8,
                        "target_risk_reward": 1.4,
                        "risk_percent": 0.005,
                    },
                    "2": {
                        "name": "BALANCED",
                        "atr_stop_multiplier": 1.35,
                        "trailing_atr_multiplier": 0.9,
                        "target_risk_reward": 1.5,
                        "risk_percent": 0.01,
                    },
                    "3": {
                        "name": "AGGRESSIVE",
                        "atr_stop_multiplier": 1.5,
                        "trailing_atr_multiplier": 1.0,
                        "target_risk_reward": 1.6,
                        "risk_percent": 0.015,
                    },
                },
                "positional": {
                    "1": {
                        "name": "CONSERVATIVE",
                        "atr_stop_multiplier": 1.5,
                        "trailing_atr_multiplier": 1.0,
                        "target_risk_reward": 1.8,
                        "risk_percent": 0.005,
                    },
                    "2": {
                        "name": "BALANCED",
                        "atr_stop_multiplier": 1.65,
                        "trailing_atr_multiplier": 1.25,
                        "target_risk_reward": 2.0,
                        "risk_percent": 0.01,
                    },
                    "3": {
                        "name": "AGGRESSIVE",
                        "atr_stop_multiplier": 1.8,
                        "trailing_atr_multiplier": 1.5,
                        "target_risk_reward": 2.2,
                        "risk_percent": 0.015,
                    },
                },
            },
            "default_data": {
                "intraday_equity": {"period": "5d", "interval": "5m"},
                "delivery_equity": {"period": "1y", "interval": "1d"},
                "futures_equity": {"period": "2mo", "interval": "15m"},
                "options_equity": {"period": "2mo", "interval": "15m"},
                "intraday_futures": {"period": "5d", "interval": "5m"},
                "intraday_options": {"period": "1d", "interval": "1m"},
            },
            "prompt_defaults": {
                "engine_choice": 1,
                "capital": 100000,
                "symbol_mode": 3,
                "single_symbol_key": "1",
                "fno_futures_base_symbol": 3,
                "fno_options_base_symbol": 1,
                "expiry_choice": 1,
                "intraday_options_structure_mode": 1,
                "intraday_options_strike_mode": 1,
                "fno_contract_confirm": 1,
                "intraday_options_strategy": 1,
                "intraday_equity_strategy_mode": 2,
                "default_strategy_mode": 2,
                "single_strategy_key": "1",
                "risk_style": 2,
                "max_positions": 1,
                "one_trade_per_symbol_per_day": 1,
                "entry_selection_mode": 1,
                "top_n": 2,
                "intraday_options_lot_mode": 2,
                "intraday_options_entry_mode": 1,
            },
        },
    }


def _build_runtime_config() -> RuntimeConfig:
    merged = _deep_merge(_default_runtime_config_map(), _load_runtime_overrides())
    config = RuntimeConfig(
        strategy=StrategyConfig(**merged["strategy"]),
        execution_safety=ExecutionSafetyConfig(**merged["execution_safety"]),
        transaction_costs=TransactionCostConfig(**merged["transaction_costs"]),
        data_cache=DataCacheConfig(**merged["data_cache"]),
        session_defaults=SessionDefaultsConfig(**merged["session_defaults"]),
        risk_controls=RiskControlsConfig(**merged["risk_controls"]),
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
            exit_limit_price_buffer_pct=float(
                merged["orders"]["exit_limit_price_buffer_pct"]
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
        engine_defaults=merged["engine_defaults"],
        backtest_defaults=merged["backtest_defaults"],
    )
    config.validate()
    return config


RUNTIME_CONFIG = _build_runtime_config()


def get_runtime_config() -> RuntimeConfig:
    return RUNTIME_CONFIG


ENGINE_TO_ASSET_CLASS = {
    "intraday_equity": "INTRADAY_EQUITY",
    "delivery_equity": "DELIVERY_EQUITY",
    "futures_equity": "FUTURES_EQUITY",
    "options_equity": "OPTIONS_EQUITY",
    "intraday_futures": "INTRADAY_FUTURES",
    "intraday_options": "INTRADAY_OPTIONS",
}

ASSET_CLASS_RISK_PROFILES = {
    # INTRADAY_EQUITY:
    # - Used directly by cost-aware target calculations via executor.calculate_cost_aware_targets().
    # - sl_percent / target_percent / trailing_percent are also used by the engine's normal build_position path.
    # - min_breakeven_move is only used by cost-aware target calculations.
    "INTRADAY_EQUITY": {
        "CONSERVATIVE": {
            "sl_percent": 0.7,
            "target_percent": 1.2,
            "trailing_percent": 0.35,
            "min_breakeven_move": 0.35,
        },
        "BALANCED": {
            "sl_percent": 1.0,
            "target_percent": 1.8,
            "trailing_percent": 0.5,
            "min_breakeven_move": 0.55,
        },
        "AGGRESSIVE": {
            "sl_percent": 4.5,
            "target_percent": 10.0,
            "trailing_percent": 3.0,
            "min_breakeven_move": 3.5,
        },
    },
    # INTRADAY_OPTIONS:
    # - Used indirectly by cost-aware target calculations and entry profitability checks.
    # - These values are NOT the main live ATM options stop/target/trail driver.
    # - The intraday_options engine mostly derives its own ATR/regime/premium-volatility-based
    #   stop, target, and trailing levels in engines/intraday_options.py.
    # - multi_level_targets here are currently ignored by the trend-adaptive intraday_options path.
    # - min_breakeven_move here is only used by cost-aware target calculations.
    "INTRADAY_OPTIONS": {
        "CONSERVATIVE": {
            "sl_percent": 8.0,
            "target_percent": 12.0,
            "trailing_percent": 4.0,
            "min_breakeven_move": 2.5,
            "multi_level_targets": [6.0, 12.0, 18.0],
        },
        "BALANCED": {
            "sl_percent": 10.0,
            "target_percent": 15.0,
            "trailing_percent": 4.8,
            "min_breakeven_move": 3.5,
            "multi_level_targets": [8.0, 15.0, 22.0],
        },
        "AGGRESSIVE": {
            "sl_percent": 12.0,
            "target_percent": 20.0,
            "trailing_percent": 6.0,
            "min_breakeven_move": 5.0,
            "multi_level_targets": [10.0, 18.0, 28.0],
        },
    },
    # DELIVERY_EQUITY:
    # - Used directly by cost-aware target calculations.
    # - sl_percent / target_percent / trailing_percent are also used directly by the engine's
    #   build_position path for live/paper positions.
    # - min_breakeven_move is only used by cost-aware target calculations.
    "DELIVERY_EQUITY": {
        "CONSERVATIVE": {
            "sl_percent": 2.5,
            "target_percent": 5.0,
            "trailing_percent": 1.8,
            "min_breakeven_move": 1.5,
        },
        "BALANCED": {
            "sl_percent": 3.5,
            "target_percent": 7.0,
            "trailing_percent": 2.2,
            "min_breakeven_move": 2.5,
        },
        "AGGRESSIVE": {
            "sl_percent": 2.8,
            "target_percent": 4.5,
            "trailing_percent": 1.0,
            "min_breakeven_move": 0.9,
        },
    },
    # FUTURES_EQUITY:
    # - Used directly by cost-aware target calculations.
    # - sl_percent / target_percent / trailing_percent are also used directly by the engine's
    #   build_position path for normal futures positions.
    # - min_breakeven_move is only used by cost-aware target calculations.
    "FUTURES_EQUITY": {
        "CONSERVATIVE": {
            "sl_percent": 0.8,
            "target_percent": 1.4,
            "trailing_percent": 0.4,
            "min_breakeven_move": 0.3,
        },
        "BALANCED": {
            "sl_percent": 1.1,
            "target_percent": 1.8,
            "trailing_percent": 0.55,
            "min_breakeven_move": 0.45,
        },
        "AGGRESSIVE": {
            "sl_percent": 1.5,
            "target_percent": 2.4,
            "trailing_percent": 0.7,
            "min_breakeven_move": 0.65,
        },
    },
    # OPTIONS_EQUITY:
    # - Used directly by cost-aware target calculations.
    # - sl_percent / target_percent / trailing_percent are also used directly by the engine's
    #   build_position path for standard options positions.
    # - multi_level_targets and min_breakeven_move are used by cost-aware target calculations.
    "OPTIONS_EQUITY": {
        "CONSERVATIVE": {
            "sl_percent": 3.0,
            "target_percent": 5.0,
            "trailing_percent": 1.25,
            "min_breakeven_move": 1.0,
            "multi_level_targets": [2.5, 5.0, 9.0],
        },
        "BALANCED": {
            "sl_percent": 4.0,
            "target_percent": 7.0,
            "trailing_percent": 1.5,
            "min_breakeven_move": 1.5,
            "multi_level_targets": [3.0, 7.0, 12.0],
        },
        "AGGRESSIVE": {
            "sl_percent": 5.0,
            "target_percent": 9.0,
            "trailing_percent": 2.0,
            "min_breakeven_move": 2.25,
            "multi_level_targets": [4.0, 9.0, 15.0],
        },
    },
    # INTRADAY_FUTURES:
    # - Used directly by cost-aware target calculations.
    # - sl_percent / target_percent / trailing_percent are also used directly by the engine's
    #   build_position path for normal intraday futures positions.
    # - min_breakeven_move is only used by cost-aware target calculations.
    "INTRADAY_FUTURES": {
        "CONSERVATIVE": {
            "sl_percent": 0.6,
            "target_percent": 1.0,
            "trailing_percent": 0.3,
            "min_breakeven_move": 0.2,
        },
        "BALANCED": {
            "sl_percent": 0.9,
            "target_percent": 1.5,
            "trailing_percent": 0.4,
            "min_breakeven_move": 0.3,
        },
        "AGGRESSIVE": {
            "sl_percent": 1.2,
            "target_percent": 2.0,
            "trailing_percent": 0.55,
            "min_breakeven_move": 0.45,
        },
    },
}


def resolve_asset_class(engine_name: str) -> str:
    normalized_engine_name = str(engine_name or "").strip().lower()
    return ENGINE_TO_ASSET_CLASS.get(normalized_engine_name, "INTRADAY_EQUITY")


INTRADAY_ENGINE_NAMES = frozenset(
    {
        "intraday_equity",
        "intraday_futures",
        "intraday_options",
        "intraday_options_buyer",
        "intraday_options_seller",
    }
)


def is_intraday_engine_name(engine_name: str | None) -> bool:
    return str(engine_name or "").strip().lower() in INTRADAY_ENGINE_NAMES


INTRADAY_OPTIONS_ENGINE_NAMES = frozenset(
    {
        "intraday_options",
        "intraday_options_buyer",
        "intraday_options_seller",
    }
)


def is_intraday_options_engine_name(engine_name: str | None) -> bool:
    """True for the base intraday_options engine and its Buyer/Seller-only
    variants. All three share apply_signal_filters/strategy machinery and
    config (ENGINE_DEFAULTS["intraday_options"]); they differ only in
    trade_direction_mode."""
    return str(engine_name or "").strip().lower() in INTRADAY_OPTIONS_ENGINE_NAMES


def get_risk_style_presets(engine_name: str | None = None) -> dict[str, dict[str, Any]]:
    risk_styles = BACKTEST_DEFAULTS["risk_styles"]
    if "intraday" not in risk_styles or "positional" not in risk_styles:
        return risk_styles
    bucket = "intraday" if is_intraday_engine_name(engine_name) else "positional"
    return risk_styles[bucket]


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
ENGINE_DEFAULTS = RUNTIME_CONFIG.engine_defaults
BACKTEST_DEFAULTS = RUNTIME_CONFIG.backtest_defaults

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
INTRADAY_OPTIONS_MARKET_OPEN_HOUR = (
    RUNTIME_CONFIG.fno.intraday_options_market_open_hour
)
INTRADAY_OPTIONS_MARKET_OPEN_MINUTE = (
    RUNTIME_CONFIG.fno.intraday_options_market_open_minute
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
