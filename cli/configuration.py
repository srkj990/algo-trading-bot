from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import (
    get_broker_ip_mode,
    get_risk_style_presets,
    get_runtime_config,
    get_upstox_static_ip,
)
from data_fetcher import set_data_provider
from engines.base import TradingEngine
from engines import (
    DeliveryEquityEngine,
    FuturesEquityEngine,
    IntradayEquityEngine,
    IntradayFuturesEngine,
    IntradayOptionsEngine,
    IntradayOptionsBuyerEngine,
    IntradayOptionsSellerEngine,
    OptionsEquityEngine,
)
from executor import set_execution_mode, set_execution_provider
from fno_data_fetcher import (
    FNO_INDEX_SYMBOLS,
    get_atm_option_strike,
    get_available_expiries,
    get_available_option_strikes,
    get_contract_lot_size,
    get_fno_display_name,
    get_fno_spot_quote_symbol,
    get_option_greeks_snapshot,
    resolve_futures_contract,
    resolve_option_contract,
)
from logger import log_event

from . import interactive_input as cli_input

DEFAULT_CONFIRMATIONS = {
    2: 2,
    3: 2,
    4: 3,
    5: 3,
}
ENGINE_FALLBACK_LEVELS = {
    "sl_percent": 0.5,
    "target_percent": 1.0,
    "trailing_percent": 0.35,
}

ENGINE_OPTIONS = {
    "1": IntradayEquityEngine,
    "2": DeliveryEquityEngine,
    "3": FuturesEquityEngine,
    "4": OptionsEquityEngine,
    "5": IntradayFuturesEngine,
    "6": IntradayOptionsEngine,        # Buy + Sell both (unrestricted, current default)
    "7": IntradayOptionsBuyerEngine,   # Buyer only (long CE)
    "8": IntradayOptionsSellerEngine,  # Seller only (writes PE, requires margin)
}

# Engine names that share the intraday_options config sub-flow (lot mode,
# entry mode, structure mode, strategy selection). All three subclass
# IntradayOptionsEngine and inherit its apply_signal_filters/strategy machinery,
# differing only in trade_direction_mode.
INTRADAY_OPTIONS_ENGINE_NAMES = {
    IntradayOptionsEngine.name,
    IntradayOptionsBuyerEngine.name,
    IntradayOptionsSellerEngine.name,
}


def _runtime_prompt_default(key: str, fallback: Any) -> Any:
    runtime_config = get_runtime_config()
    prompt_defaults = runtime_config.backtest_defaults.get("prompt_defaults", {})
    return prompt_defaults.get(key, fallback)


@dataclass
class SessionConfig:
    engine_choice: str
    engine: TradingEngine
    execution_mode: str
    exit_only_mode: bool
    live_broker_resync_interval_seconds: int
    data_provider: str
    execution_provider: str
    capital: float
    selected_symbols: list[str]
    symbol_mode: str
    option_pair_config: dict[str, Any] | None
    atm_option_config: dict[str, Any] | None
    risk_style_name: str
    atr_stop_multiplier: float
    trailing_atr_multiplier: float
    target_risk_reward: float
    sl_percent: float
    target_percent: float
    trailing_percent: float
    risk_percent: float
    max_open_positions: int
    max_capital_per_trade: float
    max_capital_deployed: float
    one_trade_per_symbol_per_day: bool
    entry_selection_mode: str
    top_n_count: int
    mode: str
    strategy_name: str | None
    strategies: list[str] | None
    min_confirmations: int | None
    intraday_options_lot_mode: str | None
    intraday_options_entry_mode: str | None
    # New session-level overrides for intraday_options tuning
    max_trades_per_underlying: int | None = None      # daily cap per underlying (overrides yaml)
    time_exit_minutes: int | None = None              # max hold minutes (overrides yaml)
    forming_tick_enabled: bool | None = None          # enable sub-1m forming-candle entry
    forming_tick_confirm_ticks: int | None = None     # ticks required to confirm forming-candle signal
    partial_exit_enabled: bool | None = None          # None = auto (lot-threshold), False = force off
    paper_trading_override: bool = False
    exit_mode: str = "TRAIL_ONLY"
    daily_max_loss_pct: float | None = None  # overrides risk_controls.daily_max_loss_pct (None = use yaml)


def validate_session_config(config: SessionConfig) -> SessionConfig:
    runtime_config = get_runtime_config()
    if config.capital <= 0:
        raise ValueError("Capital must be greater than 0")
    if config.max_open_positions < 1:
        raise ValueError("Max open positions must be at least 1")
    if config.max_capital_per_trade <= 0:
        raise ValueError("Max capital per trade must be greater than 0")
    if config.max_capital_per_trade > config.max_capital_deployed:
        raise ValueError("Max capital per trade cannot exceed max capital deployed")
    if config.max_capital_deployed > config.capital:
        raise ValueError("Max capital deployed cannot exceed total capital")
    if config.entry_selection_mode == "TOPN" and (
        config.top_n_count < 1 or config.top_n_count > config.max_open_positions
    ):
        raise ValueError("TOP N count must be between 1 and max open positions")
    if config.mode == "1" and not config.strategy_name:
        raise ValueError("Single-strategy mode requires a strategy name")
    if config.mode == "2":
        if not config.strategies:
            raise ValueError("Multi-strategy mode requires at least one strategy")
        if not config.min_confirmations or config.min_confirmations < 1:
            raise ValueError("Multi-strategy mode requires a valid confirmation count")
    if config.execution_mode == "LIVE" and runtime_config.orders.enabled:
        if config.execution_provider not in {"KITE", "UPSTOX"}:
            raise ValueError("Live execution provider must be KITE or UPSTOX")
    if config.live_broker_resync_interval_seconds < 0:
        raise ValueError("Live broker resync interval must be >= 0")
    if config.intraday_options_lot_mode not in {None, "ONE_LOT", "CAPITAL_BASED"}:
        raise ValueError("Intraday options lot mode must be ONE_LOT or CAPITAL_BASED")
    if config.intraday_options_entry_mode not in {
        None, "LIVE_STAGED", "LEGACY_IMMEDIATE", "LIVE_TICK_CONFIRM"
    }:
        raise ValueError(
            "Intraday options entry mode must be LIVE_STAGED, LEGACY_IMMEDIATE, or LIVE_TICK_CONFIRM"
        )
    if config.max_trades_per_underlying is not None and not (1 <= config.max_trades_per_underlying <= 10):
        raise ValueError("max_trades_per_underlying must be between 1 and 10")
    if config.time_exit_minutes is not None and not (0 <= config.time_exit_minutes <= 120):
        raise ValueError("time_exit_minutes must be between 0 and 120")
    if config.daily_max_loss_pct is not None and not (0 <= config.daily_max_loss_pct <= 1):
        raise ValueError("daily_max_loss_pct must be between 0 and 1")
    if config.forming_tick_confirm_ticks is not None and not (1 <= config.forming_tick_confirm_ticks <= 5):
        raise ValueError("forming_tick_confirm_ticks must be between 1 and 5")
    if config.exit_mode not in {"TRAIL_ONLY", "HARD_TARGET"}:
        raise ValueError("exit_mode must be TRAIL_ONLY or HARD_TARGET")
    return config


def log_help(message: str) -> None:
    log_event(f"[HELP] {message}")


def log_session_config_summary(config: "SessionConfig") -> None:
    """Print a clean aligned table of all key session parameters."""
    from display import config_summary as _config_summary

    engine_labels = {
        "1": "Intraday Equity (MIS, 1-min)",
        "2": "Delivery Equity (CNC, daily)",
        "3": "Futures Equity (FNO futures)",
        "4": "Options Equity (FNO options)",
        "5": "Intraday Futures (MIS, F&O)",
        "6": "Intraday Options - Buy & Sell Both (MIS, ATM)",
        "7": "Intraday Options - Buyer Only (long CE)",
        "8": "Intraday Options - Independent Seller (premium decay strategies)",
    }
    rows: list[tuple[str, str]] = [
        ("Engine",            engine_labels.get(config.engine_choice, config.engine_choice)),
        ("Execution mode",    config.execution_mode),
        ("Data provider",     config.data_provider),
        ("Execution provider",config.execution_provider),
        ("Capital",           f"{config.capital:,.2f}"),
        ("Symbol mode",       config.symbol_mode),
        ("Symbols",           str(len(config.selected_symbols))),
        ("Risk style",        config.risk_style_name),
        ("ATR stop mult",     f"{config.atr_stop_multiplier:.2f}x"),
        ("ATR trail mult",    f"{config.trailing_atr_multiplier:.2f}x"),
        ("Target RR",         f"{config.target_risk_reward:.2f}x"),
        ("Capital risk %",    f"{config.risk_percent * 100:.2f}%"),
        ("Max positions",     str(config.max_open_positions)),
        ("Max/trade",         f"{config.max_capital_per_trade:,.2f}"),
        ("Max deployed",      f"{config.max_capital_deployed:,.2f}"),
        ("One trade/day",     "Yes" if config.one_trade_per_symbol_per_day else "No"),
        ("Entry selection",   config.entry_selection_mode + (f" (N={config.top_n_count})" if config.entry_selection_mode == "TOPN" else "")),
        ("Strategy mode",     config.mode),
    ]
    if config.strategy_name:
        rows.append(("Strategy", config.strategy_name))
    if config.strategies:
        rows.append(("Strategies", ", ".join(config.strategies)))
    if config.min_confirmations:
        rows.append(("Min confirmations", str(config.min_confirmations)))
    if config.exit_only_mode:
        rows.append(("Exit-only mode", "ACTIVE — no new entries"))
    rows.append(("Exit mode", config.exit_mode))
    if config.intraday_options_lot_mode:
        rows.append(("Options lot mode", config.intraday_options_lot_mode))
    if config.intraday_options_entry_mode:
        rows.append(("Options entry mode", config.intraday_options_entry_mode))
    if config.max_trades_per_underlying is not None:
        rows.append(("Max trades/day", str(config.max_trades_per_underlying)))
    if config.time_exit_minutes is not None:
        rows.append(("Time exit (min)", str(config.time_exit_minutes) if config.time_exit_minutes > 0 else "Disabled"))
    if config.forming_tick_enabled is not None:
        rows.append(("Forming-tick entry", "ON" if config.forming_tick_enabled else "OFF"))
        if config.forming_tick_enabled and config.forming_tick_confirm_ticks is not None:
            rows.append(("Confirm ticks", str(config.forming_tick_confirm_ticks)))
    if config.partial_exit_enabled is not None:
        rows.append(("Partial exits", "ON" if config.partial_exit_enabled else "OFF"))
    _config_summary(log_event, rows, title="Session Configuration")


def log_broker_network_banner() -> None:
    broker_ip_mode = get_broker_ip_mode()
    configured_static_ip = (get_upstox_static_ip() or "").strip()

    log_event("[NETWORK] Broker API network mode is active")
    log_event(f"[NETWORK]   BROKER_IP_MODE: {broker_ip_mode}")
    if broker_ip_mode == "IPV4_ONLY":
        log_event("[NETWORK]   Broker APIs will prefer IPv4 and avoid temporary IPv6 routes")

    if configured_static_ip:
        log_event(f"[NETWORK]   Configured Upstox static IP: {configured_static_ip}")
    else:
        log_event("[NETWORK]   Configured Upstox static IP: not set", "warning")


def prompt_fno_base_symbols(engine_name: str) -> str:
    log_event("[SETUP] F&O underlying selection - choose your derivatives universe")
    for index, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1):
        log_event(f"[SETUP]   {get_fno_display_name(symbol)} ({index})")
    log_help("Choose the F&O underlying universe for this run. Example: 1 for NIFTY 50")

    if "futures" in engine_name:
        default_choice = int(_runtime_prompt_default("fno_futures_base_symbol", 3))
        return cli_input.prompt_choice(
            f"F&O futures universe: NIFTY 50(1), SENSEX(2), BOTH(3) [default {default_choice}]: ",
            [
                {"label": get_fno_display_name(FNO_INDEX_SYMBOLS[0]), "key": 1, "value": FNO_INDEX_SYMBOLS[0]},
                {"label": get_fno_display_name(FNO_INDEX_SYMBOLS[1]), "key": 2, "value": FNO_INDEX_SYMBOLS[1]},
                {"label": "BOTH", "key": 3, "value": "BOTH"},
            ],
            default=default_choice,
        )

    default_choice = int(_runtime_prompt_default("fno_options_base_symbol", 1))
    return cli_input.prompt_choice(
        "F&O options underlying: " + ", ".join(
            f"{get_fno_display_name(symbol)}({i})"
            for i, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1)
        ) + f" [default {default_choice}]: ",
        [
            {"label": get_fno_display_name(symbol), "key": i, "value": symbol}
            for i, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1)
        ],
        default=default_choice,
    )


def prompt_fno_expiry_selection(base_symbol: str, instrument_type: str) -> str:
    expiries = get_available_expiries(base_symbol, instrument_type=instrument_type)
    if not expiries:
        raise RuntimeError(
            f"No active {instrument_type} expiries found for {get_fno_display_name(base_symbol)}."
        )

    log_event(f"[SETUP] Available expiries for {get_fno_display_name(base_symbol)}:")
    for idx, expiry in enumerate(expiries, start=1):
        log_event(f"[SETUP]   {idx}. {expiry}")

    log_event("[SETUP] Choose expiry or press Enter to use the nearest available expiry")
    log_help("Choose the expiry number from the list above. Example: 1")
    expiry_default = int(_runtime_prompt_default("expiry_choice", 1))
    expiry_choice = cli_input.prompt_int(
        f"Choose expiry [default {expiry_default}]: ",
        default=expiry_default,
        minimum=1,
        maximum=len(expiries),
    )
    return expiries[expiry_choice - 1]


def prompt_option_strike_value(base_symbol: str, expiry: str, option_type: str, label: str) -> int:
    strikes = get_available_option_strikes(base_symbol, expiry, option_type)
    if not strikes:
        raise RuntimeError(
            f"No {option_type} strikes found for {get_fno_display_name(base_symbol)} {expiry}."
        )

    default_strike = get_atm_option_strike(base_symbol, expiry, option_type)
    log_event(
        f"[SETUP] {label} strikes for {get_fno_display_name(base_symbol)} {option_type}:"
    )
    strikes_to_show = strikes[:8] if len(strikes) <= 8 else strikes[:5] + ["..."] + strikes[-3:]
    log_event(f"[SETUP]   {strikes_to_show}")
    log_event(f"[SETUP] ATM reference strike: {default_strike}")
    log_help(f"Enter an available {option_type} strike price for {label.lower()} selection. Example: {default_strike}")

    while True:
        raw = input(f"Enter {label} strike [default {default_strike}]: ").strip()
        if not raw:
            strike = default_strike
        else:
            try:
                strike = int(raw)
            except ValueError:
                log_event("[INPUT] Enter a valid strike price.", "warning")
                continue

        if strike in strikes:
            return strike

        log_event(
            f"[INPUT] Strike {strike} is not available for {get_fno_display_name(base_symbol)} {expiry} {option_type}.",
            "warning",
        )


def prompt_fno_option_contract_selection(base_symbol: str) -> tuple[list[str], str]:
    expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="OPT")
    log_help("Choose the option type to trade for this positional options setup. Example: 1 for CE")
    option_type = cli_input.prompt_choice(
        "Option type: CE(1) or PE(2)? [default 1]: ",
        [
            {"label": "CE", "key": 1, "value": "CE"},
            {"label": "PE", "key": 2, "value": "PE"},
        ],
        default=1,
    )
    strikes = get_available_option_strikes(base_symbol, expiry, option_type)
    if not strikes:
        raise RuntimeError(
            f"No {option_type} strikes found for {get_fno_display_name(base_symbol)} {expiry}."
        )

    default_strike = get_atm_option_strike(base_symbol, expiry, option_type)
    log_event(f"[SETUP] Available strikes for {get_fno_display_name(base_symbol)}:")
    strikes_to_show = strikes[:8] if len(strikes) <= 8 else strikes[:5] + ["..."] + strikes[-3:]
    log_event(f"[SETUP]   {strikes_to_show}")
    log_event(f"[SETUP] ATM reference strike: {default_strike}")
    log_help("Choose how the strike should be selected relative to ATM. Example: 1 for ATM")

    strike_mode = cli_input.prompt_choice(
        "Strike selection: ATM(1), OTM offset(2), ITM offset(3), MANUAL(4)? [default 1]: ",
        [
            {"label": "ATM", "key": 1, "value": "ATM"},
            {"label": "OTM OFFSET", "key": 2, "value": "OTM"},
            {"label": "ITM OFFSET", "key": 3, "value": "ITM"},
            {"label": "MANUAL", "key": 4, "value": "MANUAL"},
        ],
        default=1,
    )

    if strike_mode in {"OTM", "ITM"}:
        atm_index = strikes.index(default_strike)
        step_count = cli_input.prompt_int(
            "Number of strike steps from ATM [default 1]: ",
            default=1,
            minimum=1,
        )
        direction = 1 if strike_mode == "OTM" else -1
        if option_type == "PE":
            direction *= -1
        selected_index = max(0, min(len(strikes) - 1, atm_index + (direction * step_count)))
        strike = strikes[selected_index]
        log_event(f"[MAIN] Selected {strike_mode} strike {strike} from ATM {default_strike}")
    elif strike_mode == "ATM":
        strike = default_strike
        log_event(f"[MAIN] Selected ATM strike {strike}")
    else:
        log_event("[SETUP] Press Enter to use the ATM-like default strike")
        strike = default_strike
        while True:
            raw = input(f"Enter strike price [default {default_strike}]: ").strip()
            if not raw:
                strike = default_strike
            else:
                try:
                    strike = int(raw)
                except ValueError:
                    log_event("[INPUT] Enter a valid strike price.", "warning")
                    continue

            if strike in strikes:
                break

            log_event(
                f"[INPUT] Strike {strike} is not available for {get_fno_display_name(base_symbol)} {expiry} {option_type}.",
                "warning",
            )

    contract = resolve_option_contract(base_symbol, expiry, strike, option_type)
    lot_size = get_contract_lot_size(contract)
    log_event(
        f"[MAIN] Resolved F&O option contract for {get_fno_display_name(base_symbol)}: {contract} | Lot size={lot_size}"
    )
    return [contract], "FNO"


def prompt_intraday_atm_option_selection(base_symbol: str) -> tuple[list[str], str, dict[str, Any]]:
    expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="OPT")
    log_help("Choose how far the dynamic ATM selection should move from the current ATM strike. Example: 1 for ATM")
    default_strike_mode = int(_runtime_prompt_default("intraday_options_strike_mode", 1))
    strike_offset_mode = cli_input.prompt_choice(
        f"ATM strike mode: ATM(1), ATM + 1 STRIKE(2), ATM - 1 STRIKE(3) [default {default_strike_mode}]: ",
        [
            {"label": "ATM", "key": 1, "value": "ATM"},
            {"label": "ATM + 1", "key": 2, "value": "ATM_PLUS_1"},
            {"label": "ATM - 1", "key": 3, "value": "ATM_MINUS_1"},
        ],
        default=default_strike_mode,
    )
    strike_offset = {
        "ATM": 0,
        "ATM_PLUS_1": 1,
        "ATM_MINUS_1": -1,
    }[strike_offset_mode]
    scan_symbol = get_fno_spot_quote_symbol(base_symbol)
    log_event(
        f"[MAIN] Intraday ATM options mode selected for {get_fno_display_name(base_symbol)} | Expiry={expiry} | Underlying scan symbol={scan_symbol} | Strike mode={strike_offset_mode.replace('_', ' ')}"
    )
    atm_option_config = {
        "mode": "ATM_DYNAMIC",
        "underlying": base_symbol,
        "expiry": expiry,
        "scan_symbol": scan_symbol,
        "strike_offset_mode": strike_offset_mode,
        "strike_offset": strike_offset,
    }
    return [scan_symbol], "FNO_ATM", atm_option_config


def prompt_fno_option_pair_selection(base_symbol: str) -> tuple[list[str], str, dict[str, Any]]:
    expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="OPT")
    log_help("First choose the lower PE strike for the bounded range pair. Example: 24000")
    lower_pe_strike = prompt_option_strike_value(base_symbol, expiry, "PE", label="Lower PE")
    log_help("Then choose the upper CE strike for the bounded range pair. Example: 24600")
    upper_ce_strike = prompt_option_strike_value(base_symbol, expiry, "CE", label="Upper CE")
    if lower_pe_strike >= upper_ce_strike:
        raise RuntimeError("For a bounded-range pair, the PE strike must be below the CE strike.")

    pe_contract = resolve_option_contract(base_symbol, expiry, lower_pe_strike, "PE")
    ce_contract = resolve_option_contract(base_symbol, expiry, upper_ce_strike, "CE")
    pe_lot_size = get_contract_lot_size(pe_contract)
    ce_lot_size = get_contract_lot_size(ce_contract)
    if pe_lot_size != ce_lot_size:
        raise RuntimeError(f"Mismatched lot sizes for pair contracts: {pe_lot_size} vs {ce_lot_size}")

    pair_id = f"PAIR:{base_symbol}:{expiry}:{lower_pe_strike}:{upper_ce_strike}"
    log_event(f"[MAIN] Resolved two-leg range pair: {pe_contract} + {ce_contract} | Lot size={pe_lot_size}")
    pair_config = {
        "mode": "TWO_LEG_RANGE",
        "pair_id": pair_id,
        "underlying": base_symbol,
        "expiry": expiry,
        "lower_strike": lower_pe_strike,
        "upper_strike": upper_ce_strike,
        "pe_symbol": pe_contract,
        "ce_symbol": ce_contract,
        "symbols": [pe_contract, ce_contract],
        "entry_side": "SELL",
    }
    return [pe_contract, ce_contract], "FNO_PAIR", pair_config


def prompt_fno_contract_selection(
    engine_name: str,
) -> tuple[list[str], str, dict[str, Any] | None, dict[str, Any] | None]:
    selection = prompt_fno_base_symbols(engine_name)
    if "futures" in engine_name:
        base_symbols = list(FNO_INDEX_SYMBOLS) if selection == "BOTH" else [selection]
        contracts = []
        for base_symbol in base_symbols:
            expiry = prompt_fno_expiry_selection(base_symbol, instrument_type="FUT")
            contract = resolve_futures_contract(base_symbol, expiry)
            lot_size = get_contract_lot_size(contract)
            contracts.append(contract)
            log_event(
                f"[MAIN] Resolved F&O futures contract for {get_fno_display_name(base_symbol)}: {contract} | Lot size={lot_size}"
            )
        return contracts, "FNO", None, None

    if engine_name in INTRADAY_OPTIONS_ENGINE_NAMES:
        default_structure_mode = int(_runtime_prompt_default("intraday_options_structure_mode", 1))
        structure_mode = cli_input.prompt_choice(
            f"Options structure: ATM SINGLE OPTION(1) or TWO-LEG RANGE PAIR(2)? [default {default_structure_mode}]: ",
            [
                {"label": "ATM SINGLE OPTION", "key": 1, "value": "SINGLE"},
                {"label": "TWO-LEG RANGE PAIR", "key": 2, "value": "PAIR"},
            ],
            default=default_structure_mode,
        )
        if structure_mode == "PAIR":
            symbols, symbol_mode, pair_config = prompt_fno_option_pair_selection(selection)
            return symbols, symbol_mode, pair_config, None
        symbols, symbol_mode, atm_option_config = prompt_intraday_atm_option_selection(selection)
        return symbols, symbol_mode, None, atm_option_config

    symbols, symbol_mode = prompt_fno_option_contract_selection(selection)
    return symbols, symbol_mode, None, None


def log_selected_fno_contract_summary(
    engine_name: str,
    selected_symbols: list[str],
    option_pair_config: dict[str, Any] | None = None,
    atm_option_config: dict[str, Any] | None = None,
) -> None:
    if not selected_symbols:
        return

    log_event("[SETUP] Selected F&O contract summary:")
    for symbol in selected_symbols:
        if atm_option_config and symbol == atm_option_config["scan_symbol"]:
            line = (
                f"[SETUP]   ATM SCALP | Underlying={atm_option_config['underlying']} "
                f"| Expiry={atm_option_config['expiry']} | Scan={symbol}"
            )
        else:
            lot_size = get_contract_lot_size(symbol)
            line = f"[SETUP]   {symbol} | Lot size={lot_size}"
        if "options" in engine_name:
            try:
                if not (atm_option_config and symbol == atm_option_config["scan_symbol"]):
                    analytics = get_option_greeks_snapshot(symbol)
                    line += (
                        f" | Premium={analytics['option_price']:.2f}"
                        f" | Underlying={analytics['underlying_price']:.2f}"
                        f" | Delta={analytics['delta']:.3f}"
                        f" | IV={analytics['iv']:.4f}"
                        f" | DTE={analytics.get('days_to_expiry', 'N/A')}"
                    )
            except Exception as exc:
                line += f" | Greeks unavailable ({exc})"
        log_event(line)

    if option_pair_config:
        width = option_pair_config["upper_strike"] - option_pair_config["lower_strike"]
        log_event(
            f"[SETUP]   Structure=TWO_LEG_RANGE | Underlying={option_pair_config['underlying']} | Expiry={option_pair_config['expiry']} | Range={option_pair_config['lower_strike']}-{option_pair_config['upper_strike']} | Width={width}"
        )

    if atm_option_config:
        log_event(
            f"[SETUP]   Structure=ATM_DYNAMIC | Underlying={atm_option_config['underlying']} | Expiry={atm_option_config['expiry']} | Strike mode={atm_option_config['strike_offset_mode'].replace('_', ' ')} | Contracts resolved live from underlying movement"
        )


def confirm_selected_fno_contracts(
    engine_name: str,
    selected_symbols: list[str],
    option_pair_config: dict[str, Any] | None = None,
    atm_option_config: dict[str, Any] | None = None,
) -> None:
    log_selected_fno_contract_summary(
        engine_name,
        selected_symbols,
        option_pair_config,
        atm_option_config,
    )
    log_help("Confirm the resolved F&O structure before the bot proceeds. Example: 1 for YES")
    default_confirmation = int(_runtime_prompt_default("fno_contract_confirm", 1))
    confirmation = cli_input.prompt_choice(
        f"Continue with these F&O contracts? YES(1) or NO(2) [default {default_confirmation}]: ",
        [
            {"label": "YES", "key": 1, "value": "YES"},
            {"label": "NO", "key": 2, "value": "NO"},
        ],
        default=default_confirmation,
    )
    if confirmation != "YES":
        raise SystemExit("[MAIN] F&O contract selection cancelled by user.")


def should_auto_select_top1(
    symbol_mode: str,
    selected_symbols: list[str],
    option_pair_config: dict[str, Any] | None = None,
    atm_option_config: dict[str, Any] | None = None,
) -> bool:
    if atm_option_config or option_pair_config:
        return True
    return symbol_mode == "SINGLE" or len(selected_symbols) == 1


def collect_session_configuration() -> SessionConfig:
    runtime_config = get_runtime_config()
    log_broker_network_banner()
    log_event("[SETUP] Choose trading engine - determines trading style and timeframe")
    log_event("[SETUP]   INTRADAY EQUITY: 1-minute data, MIS product, 9:15-15:30, auto square-off")
    log_event("[SETUP]   DELIVERY EQUITY: Daily data, CNC product, long-term holding")
    log_event("[SETUP]   FUTURES EQUITY: Positional index futures on NIFTY 50 and SENSEX via Kite derivatives")
    log_event("[SETUP]   OPTIONS EQUITY: Positional index options on NIFTY 50 and SENSEX with ATM strike assist")
    log_event("[SETUP]   INTRADAY FUTURES: MIS index futures with auto square-off and lot-aware sizing")
    log_event("[SETUP]   INTRADAY OPTIONS: MIS index options with Greeks/IV filters and auto square-off")
    log_event("[SETUP]     - BUY & SELL BOTH (6): no direction restriction (current default)")
    log_event("[SETUP]     - BUYER ONLY (7): long CE only, defined risk, holds on SELL signals")
    log_event("[SETUP]     - INDEPENDENT SELLER (8): dedicated premium-decay strategies (ORB failure, VWAP fade, theta, exhaustion)")
    log_help("Pick the engine first so the bot can ask only the prompts relevant to that trading style. Example: 6 for INTRADAY OPTIONS")

    engine_choice = cli_input.prompt_choice(
        "Engine: INTRADAY EQUITY(1), DELIVERY EQUITY(2), FUTURES EQUITY(3), OPTIONS EQUITY(4), INTRADAY FUTURES(5), "
        "INTRADAY OPTIONS BUY+SELL(6), INTRADAY OPTIONS BUYER ONLY(7), INTRADAY OPTIONS SELLER ONLY(8)? [default 1]: ",
        [
            {"label": "INTRADAY EQUITY", "key": 1, "value": "1"},
            {"label": "DELIVERY EQUITY", "key": 2, "value": "2"},
            {"label": "FUTURES EQUITY", "key": 3, "value": "3"},
            {"label": "OPTIONS EQUITY", "key": 4, "value": "4"},
            {"label": "INTRADAY FUTURES", "key": 5, "value": "5"},
            {"label": "INTRADAY OPTIONS BUY+SELL BOTH", "key": 6, "value": "6"},
            {"label": "INTRADAY OPTIONS BUYER ONLY", "key": 7, "value": "7"},
            {"label": "INTRADAY OPTIONS SELLER ONLY", "key": 8, "value": "8"},
        ],
        default=1,
    )
    selected_engine_name = ENGINE_OPTIONS[engine_choice].name
    is_fno_engine = "futures" in selected_engine_name or "options" in selected_engine_name

    log_event("[SETUP] Choose execution mode - CRITICAL SAFETY SETTING")
    log_event("[SETUP]   PAPER: Simulates trading, NO real orders placed")
    log_event("[SETUP]   LIVE: Places REAL orders with your broker - USE WITH CAUTION")
    log_help("Choose PAPER for simulation or LIVE for real broker orders. Example: 1 for PAPER")
    execution_mode = cli_input.prompt_choice(
        "Execution mode: PAPER(1) or LIVE(9)? [default 9]: ",
        [
            {"label": "PAPER", "key": 1, "value": "PAPER"},
            {"label": "LIVE", "key": 9, "value": "LIVE"},
        ],
        default=9,
    )
    set_execution_mode(execution_mode)
    log_event(f"[MAIN] Execution mode selected: {execution_mode}")

    exit_only_mode = bool(runtime_config.session_defaults.exit_only_default)
    live_broker_resync_interval_seconds = int(
        runtime_config.session_defaults.live_broker_resync_interval_seconds
    )
    log_event(
        "[MAIN] Session behavior from config: "
        + ("EXIT ONLY - no new entries will be placed" if exit_only_mode else "NORMAL")
    )
    if execution_mode == "LIVE":
        log_event(
            f"[MAIN] Live broker resync interval from config: {live_broker_resync_interval_seconds}s"
        )

    if is_fno_engine:
        data_provider = "KITE"
        execution_provider = "KITE"
        set_data_provider(data_provider)
        set_execution_provider(execution_provider)
        log_event("[MAIN] F&O engine detected - data provider auto-set to KITE")
        log_event("[MAIN] F&O engine detected - execution provider auto-set to KITE")
    else:
        log_event("[SETUP] Choose your data provider - this determines where market data comes from")
        log_event("[SETUP]   YFINANCE: Free, no authentication needed, good for testing")
        log_event("[SETUP]   KITE: Live data from Zerodha, requires API credentials")
        log_event("[SETUP]   UPSTOX: Live data from Upstox, requires API credentials")
        log_help("Choose the market data source for signal generation. Example: 1 for YFINANCE")

        data_provider = cli_input.prompt_choice(
            "Data provider: YFINANCE(1), KITE(2), UPSTOX(3)? [default 1]: ",
            [
                {"label": "YFINANCE", "key": 1, "value": "YFINANCE"},
                {"label": "KITE", "key": 2, "value": "KITE"},
                {"label": "UPSTOX", "key": 3, "value": "UPSTOX"},
            ],
            default=1,
        )
        set_data_provider(data_provider)
        log_event(f"[MAIN] Data provider selected: {data_provider}")

        log_event("[SETUP] Choose your broker for order execution")
        log_event("[SETUP]   KITE: Zerodha's trading platform")
        log_event("[SETUP]   UPSTOX: Upstox trading platform")
        log_help("Choose which broker should receive live or paper order flow. Example: 1 for KITE")

        execution_provider = cli_input.prompt_choice(
            "Execution provider: KITE(1) or UPSTOX(2)? [default 1]: ",
            [
                {"label": "KITE", "key": 1, "value": "KITE"},
                {"label": "UPSTOX", "key": 2, "value": "UPSTOX"},
            ],
            default=1,
        )
        set_execution_provider(execution_provider)
        log_event(f"[MAIN] Execution provider selected: {execution_provider}")

    log_event("[SETUP] Enter your trading capital - this is the maximum amount the bot can risk")
    log_event("[SETUP]   For PAPER mode: Use any amount for simulation")
    log_event("[SETUP]   For LIVE mode: Use amount you're comfortable losing")
    log_help("Enter the total capital allocation for this run. Example: 10000")
    capital = cli_input.prompt_float("Enter capital for strategy: ", minimum=1)

    option_pair_config = None
    atm_option_config = None
    if engine_choice in {"3", "4", "5", "6", "7", "8"}:
        selected_symbols, symbol_mode, option_pair_config, atm_option_config = prompt_fno_contract_selection(
            ENGINE_OPTIONS[engine_choice].name
        )
        confirm_selected_fno_contracts(
            ENGINE_OPTIONS[engine_choice].name,
            selected_symbols,
            option_pair_config=option_pair_config,
            atm_option_config=atm_option_config,
        )
    else:
        selected_symbols, symbol_mode = cli_input.prompt_symbol_selection()

    risk_styles = get_risk_style_presets(ENGINE_OPTIONS[engine_choice].name)
    log_event("[SETUP] Choose risk style - affects ATR/risk sizing and target selection")
    log_event("[SETUP]   CONSERVATIVE: tight stops, 0.5% risk per trade, lower reward expectations")
    log_event("[SETUP]   BALANCED: moderate stops, 1.0% risk per trade, general-purpose default")
    log_event("[SETUP]   AGGRESSIVE: wider stops, 1.5% risk per trade, higher reward expectations")
    log_help("Choose how aggressive the ATR/risk sizing should be. Example: 2 for BALANCED")

    risk_style_key = cli_input.prompt_choice(
        "Risk style: CONSERVATIVE(1), BALANCED(2), AGGRESSIVE(3)? [default 2]: ",
        [
            {"label": "CONSERVATIVE", "key": 1, "value": "1"},
            {"label": "BALANCED", "key": 2, "value": "2"},
            {"label": "AGGRESSIVE", "key": 3, "value": "3"},
        ],
        default=2,
    )
    risk_style = risk_styles[risk_style_key]
    atr_stop_multiplier = risk_style["atr_stop_multiplier"]
    trailing_atr_multiplier = risk_style["trailing_atr_multiplier"]
    target_risk_reward = risk_style["target_risk_reward"]
    sl_percent = float(risk_style.get("sl_percent", ENGINE_FALLBACK_LEVELS["sl_percent"]))
    target_percent = float(
        risk_style.get("target_percent", ENGINE_FALLBACK_LEVELS["target_percent"])
    )
    trailing_percent = float(
        risk_style.get("trailing_percent", ENGINE_FALLBACK_LEVELS["trailing_percent"])
    )
    risk_percent = risk_style["risk_percent"]

    engine = ENGINE_OPTIONS[engine_choice](
        sl_percent=sl_percent,
        target_percent=target_percent,
        trailing_percent=trailing_percent,
    )
    if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES:
        engine.sl_percent = 10.0
        engine.target_percent = 20.0
        engine.trailing_percent = 7.5

    if engine.name == "delivery_equity":
        log_event("[SETUP] Delivery equity settings - for long-term CNC positions")
        log_event("[SETUP]   Max portfolio allocation per symbol: Maximum % of capital per stock")
        log_event("[SETUP]   Example: 25% means no single stock can exceed 25% of your capital")
        log_help("Set the largest percent of capital allowed in one delivery position. Example: 25")
        max_symbol_allocation = cli_input.prompt_float(
            "Max portfolio allocation per delivery symbol % [default 25]: ",
            default=25,
            minimum=1,
            maximum=100,
        )
        engine.set_portfolio_rules(max_symbol_allocation / 100)
        log_event(f"[MAIN] Delivery portfolio rules | Max symbol allocation={max_symbol_allocation:.2f}%")

    if "futures" in engine.name or "options" in engine.name:
        log_event("[SETUP] F&O engines use Kite derivatives contracts and live broker position sync.")
        log_event("[SETUP] Supported F&O underlyings in this build: NIFTY 50 and SENSEX.")
        if "intraday" in engine.name:
            log_event("[SETUP] Intraday F&O will use MIS product, lot-based quantity rounding, and auto square-off near market close.")
        if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES:
            log_event("[SETUP] Intraday ATM options scalping uses dynamic ATM contract selection, 10% stop-loss, 20% target, and cooldown-managed entries.")
            if engine.name == IntradayOptionsBuyerEngine.name:
                log_event("[SETUP] Buyer-only engine: opens long CE positions only; SELL/short-PE signals are held.")
            elif engine.name == IntradayOptionsSellerEngine.name:
                log_event("[SETUP] Seller-only engine: writes/shorts PE positions only (requires margin); BUY/long-CE signals are held.")

    log_event(f"[MAIN] Engine selected: {engine.name}")
    log_event(
        f"[MAIN] Risk style selected: {risk_style['name']} | ATR stop={atr_stop_multiplier:.2f}x | ATR trail={trailing_atr_multiplier:.2f}x | Target RR={target_risk_reward:.2f}x | Capital risk={risk_percent * 100:.2f}%"
    )

    intraday_options_lot_mode = None
    intraday_options_entry_mode = None
    max_trades_per_underlying = None
    time_exit_minutes = None
    forming_tick_enabled = None
    forming_tick_confirm_ticks = None
    partial_exit_enabled = None
    if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES and atm_option_config:
        intraday_options_lot_mode = str(
            runtime_config.fno.intraday_options_lot_mode or "CAPITAL_BASED"
        ).upper()
        log_event(
            "[MAIN] Intraday options lot sizing from config: "
            + ("1 lot default" if intraday_options_lot_mode == "ONE_LOT" else "capital based")
        )
        _entry_default_key = _prompt_default_int("intraday_options_entry_mode", 1)
        intraday_options_entry_mode = cli_input.prompt_choice(
            f"Entry mode: Live Tick Confirm(1) / Live Staged(2) / Legacy Immediate(3)? [default {_entry_default_key}]: ",
            [
                {"label": "LIVE_TICK_CONFIRM", "key": 1, "value": "LIVE_TICK_CONFIRM"},
                {"label": "LIVE_STAGED", "key": 2, "value": "LIVE_STAGED"},
                {"label": "LEGACY_IMMEDIATE", "key": 3, "value": "LEGACY_IMMEDIATE"},
            ],
            default=_entry_default_key,
        )
        log_event(f"[MAIN] Intraday options entry mode: {intraday_options_entry_mode}")
        _max_trades_default = _prompt_default_int("max_trades_per_underlying", 2)
        max_trades_per_underlying = cli_input.prompt_int(
            f"Max trades per underlying per day [default {_max_trades_default}]: ",
            default=_max_trades_default,
            minimum=1,
        )
        _time_exit_default = _prompt_default_int("time_exit_minutes", 15)
        time_exit_minutes = cli_input.prompt_int(
            f"Time exit window in minutes (0=disabled) [default {_time_exit_default}]: ",
            default=_time_exit_default,
            minimum=0,
        )
        _forming_default = _prompt_default_int("forming_tick_enabled", 1)
        _forming_choice = cli_input.prompt_choice(
            f"Enable forming-candle entry? YES(1) / NO(2) [default {_forming_default}]: ",
            [
                {"label": "YES", "key": 1, "value": "YES"},
                {"label": "NO", "key": 2, "value": "NO"},
            ],
            default=_forming_default,
        )
        forming_tick_enabled = _forming_choice == "YES"
        if forming_tick_enabled:
            _confirm_default = _prompt_default_int("forming_tick_confirm_ticks", 2)
            forming_tick_confirm_ticks = cli_input.prompt_int(
                f"Confirm ticks required for forming-candle signal [default {_confirm_default}]: ",
                default=_confirm_default,
                minimum=1,
            )
        _partial_default = int(_runtime_prompt_default("partial_exit_enabled", 1))
        _partial_choice = cli_input.prompt_choice(
            f"Enable partial exits for ≥3 lots? YES(1) / NO(2) [default {_partial_default}]: ",
            [{"label": "YES", "key": 1, "value": "YES"}, {"label": "NO", "key": 2, "value": "NO"}],
            default=_partial_default,
        )
        partial_exit_enabled = None if _partial_choice == "YES" else False
    auto_single_selection_mode = should_auto_select_top1(
        symbol_mode,
        selected_symbols,
        option_pair_config=option_pair_config,
        atm_option_config=atm_option_config,
    )
    if auto_single_selection_mode:
        max_open_positions = 1
        log_event("[MAIN] Single-structure mode detected - max open positions auto-set to 1")
    else:
        log_event("[SETUP] Position limits - control how many concurrent trades")
        log_event("[SETUP]   Max open positions: How many stocks can be traded simultaneously")
        log_event("[SETUP]   Higher = more diversification, but more capital needed")
        log_help("Set how many separate positions may stay open at once. Example: 3")
        max_open_positions = cli_input.prompt_int(
            "Max open positions [default 1]: ",
            default=1,
            minimum=1,
        )

    log_event("[SETUP] Capital limits per trade - controls individual position size")
    log_event("[SETUP]   Max capital per trade: Maximum amount to risk on any single stock")
    log_event("[SETUP]   Lower = more conservative, higher = larger positions")
    log_help("Set the largest capital allocation allowed for one trade. Example: 10000")
    default_max_capital_per_trade = capital / max_open_positions
    max_capital_per_trade = cli_input.prompt_float(
        f"Max capital per trade [default {default_max_capital_per_trade:.2f}]: ",
        default=default_max_capital_per_trade,
        minimum=1,
        maximum=capital,
    )

    log_event("[SETUP] Total capital deployment - overall portfolio exposure")
    log_event("[SETUP]   Max capital deployed: Total amount that can be invested across all positions")
    log_event("[SETUP]   Usually set to your total capital amount")
    log_help("Set the maximum combined capital allowed across all open trades. Example: 25000")
    max_capital_deployed = cli_input.prompt_float(
        f"Max capital deployed [default {capital:.2f}]: ",
        default=capital,
        minimum=1,
        maximum=capital,
    )

    log_event("[SETUP] Trading frequency - controls how often to trade each stock")
    log_event("[SETUP]   One trade per symbol per day: YES = only 1 trade per stock daily")
    log_event("[SETUP]   One trade per symbol per day: NO = can trade same stock multiple times")
    log_help("Choose whether the same symbol can be re-entered again on the same day. Example: 1 for YES")
    one_trade_per_symbol_per_day = cli_input.prompt_choice(
        "One trade per symbol per day? YES(1) or NO(2) [default 1]: ",
        [
            {"label": "YES", "key": 1, "value": "YES"},
            {"label": "NO", "key": 2, "value": "NO"},
        ],
        default=1,
    ) == "YES"

    if auto_single_selection_mode:
        entry_selection_mode = "TOP1"
        top_n_count = 1
        log_event("[MAIN] Single-structure mode detected - entry selection auto-set to TOP 1")
    else:
        log_event("[SETUP] Entry selection - how many top-ranked candidates to trade")
        log_event("[SETUP]   TOP 1: Only enter the highest-ranked signal")
        log_event("[SETUP]   TOP N: Enter the top N highest-ranked signals")
        log_help("Choose whether to enter only the top-ranked signal or several ranked candidates. Example: 2 for TOP N")
        entry_selection_mode = cli_input.prompt_choice(
            "Entry selection: TOP 1(1) or TOP N(2)? [default 1]: ",
            [
                {"label": "TOP 1", "key": 1, "value": "TOP1"},
                {"label": "TOP N", "key": 2, "value": "TOPN"},
            ],
            default=1,
        )
        top_n_count = 1
        if entry_selection_mode == "TOPN":
            default_top_n = min(5, max_open_positions)
            log_help(f"Enter how many ranked candidates to enter each cycle. Example: {default_top_n}")
            top_n_count = cli_input.prompt_int(
                f"Enter N for TOP N entries [default {default_top_n}]: ",
                default=default_top_n,
                minimum=1,
                maximum=max_open_positions,
            )

    mode, strategy_name, strategies, min_confirmations = cli_input.prompt_strategy_configuration(
        engine,
        DEFAULT_CONFIRMATIONS,
    )
    if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES:
        engine.momentum_entry_mode = (
            "LEGACY_RAW"
            if intraday_options_entry_mode == "LEGACY_IMMEDIATE"
            else "STAGED"
        )
    log_event(
        f"[MAIN] Scan configuration | Engine={engine.name} | Data provider={data_provider} | Execution provider={execution_provider} | Symbol mode={symbol_mode} | Symbols={len(selected_symbols)} | Data={engine.data_period}/{engine.data_interval} | Mode={mode} | Max positions={max_open_positions} | Max/trade={max_capital_per_trade:.2f} | Max deployed={max_capital_deployed:.2f} | One trade/day={one_trade_per_symbol_per_day} | Selection={entry_selection_mode} | Top N={top_n_count}"
    )

    session_config = validate_session_config(
        SessionConfig(
        engine_choice=engine_choice,
        engine=engine,
        execution_mode=execution_mode,
        exit_only_mode=exit_only_mode,
        live_broker_resync_interval_seconds=live_broker_resync_interval_seconds,
        data_provider=data_provider,
        execution_provider=execution_provider,
        capital=capital,
        selected_symbols=selected_symbols,
        symbol_mode=symbol_mode,
        option_pair_config=option_pair_config,
        atm_option_config=atm_option_config,
        risk_style_name=risk_style["name"],
        atr_stop_multiplier=atr_stop_multiplier,
        trailing_atr_multiplier=trailing_atr_multiplier,
        target_risk_reward=target_risk_reward,
        sl_percent=sl_percent,
        target_percent=target_percent,
        trailing_percent=trailing_percent,
        risk_percent=risk_percent,
        max_open_positions=max_open_positions,
        max_capital_per_trade=max_capital_per_trade,
        max_capital_deployed=max_capital_deployed,
        one_trade_per_symbol_per_day=one_trade_per_symbol_per_day,
        entry_selection_mode=entry_selection_mode,
        top_n_count=top_n_count,
        mode=mode,
        strategy_name=strategy_name,
        strategies=strategies,
        min_confirmations=min_confirmations,
        intraday_options_lot_mode=intraday_options_lot_mode,
        intraday_options_entry_mode=intraday_options_entry_mode,
        max_trades_per_underlying=max_trades_per_underlying,
        time_exit_minutes=time_exit_minutes,
        forming_tick_enabled=forming_tick_enabled,
        forming_tick_confirm_ticks=forming_tick_confirm_ticks,
        partial_exit_enabled=partial_exit_enabled,
    )
    )
    log_session_config_summary(session_config)
    return session_config


# ---------------------------------------------------------------------------
# Web-mode entry point — no input() calls; takes a plain dict from the form
# ---------------------------------------------------------------------------

def build_session_config_from_dict(data: dict) -> SessionConfig:
    """
    Build a SessionConfig from a JSON dict submitted by the browser form.
    Raises ValueError with a user-readable message on invalid input.
    All decisions mirror collect_session_configuration() exactly.
    """
    runtime_config = get_runtime_config()

    # ── engine ──────────────────────────────────────────────────────────────
    engine_choice = str(data.get("engine_choice", "1"))
    if engine_choice not in ENGINE_OPTIONS:
        raise ValueError(f"Invalid engine choice: {engine_choice}")
    engine_cls = ENGINE_OPTIONS[engine_choice]
    selected_engine_name = engine_cls.name
    is_fno_engine = "futures" in selected_engine_name or "options" in selected_engine_name

    # ── execution mode ───────────────────────────────────────────────────────
    execution_mode = str(data.get("execution_mode", "PAPER")).upper()
    if execution_mode not in {"PAPER", "LIVE"}:
        raise ValueError("execution_mode must be PAPER or LIVE")
    from executor import set_execution_mode
    set_execution_mode(execution_mode)

    exit_only_mode = bool(runtime_config.session_defaults.exit_only_default)
    live_broker_resync_interval_seconds = int(
        runtime_config.session_defaults.live_broker_resync_interval_seconds
    )
    _exit_mode_raw = str(data.get("exit_mode", "")).strip().upper()
    exit_mode = (
        _exit_mode_raw
        if _exit_mode_raw in {"TRAIL_ONLY", "HARD_TARGET"}
        else str(runtime_config.execution_safety.exit_mode or "TRAIL_ONLY").upper()
    )

    # ── risk controls ─────────────────────────────────────────────────────────
    daily_max_loss_pct: float | None = None
    _daily_max_loss_raw = data.get("daily_max_loss_pct")
    if _daily_max_loss_raw is not None and str(_daily_max_loss_raw).strip():
        daily_max_loss_pct = max(0.0, min(1.0, float(_daily_max_loss_raw)))

    # ── data / execution providers ────────────────────────────────────────────
    from data_fetcher import set_data_provider
    from executor import set_execution_provider
    if is_fno_engine:
        data_provider = "KITE"
        execution_provider = "KITE"
    else:
        data_provider = str(data.get("data_provider", "YFINANCE")).upper()
        if data_provider not in {"YFINANCE", "KITE", "UPSTOX"}:
            raise ValueError("data_provider must be YFINANCE, KITE, or UPSTOX")
        execution_provider = str(data.get("execution_provider", "KITE")).upper()
        if execution_provider not in {"KITE", "UPSTOX"}:
            raise ValueError("execution_provider must be KITE or UPSTOX")
    set_data_provider(data_provider)
    set_execution_provider(execution_provider)

    # ── capital ──────────────────────────────────────────────────────────────
    try:
        capital = float(data["capital"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("capital must be a positive number")
    if capital <= 0:
        raise ValueError("capital must be greater than 0")

    # ── symbol / F&O contract selection ─────────────────────────────────────
    option_pair_config = None
    atm_option_config = None

    if engine_choice in {"3", "4", "5", "6", "7", "8"}:
        fno_underlying = str(data.get("fno_underlying", "NIFTY"))
        fno_expiry = str(data.get("fno_expiry", ""))
        # Engine 6/7/8 ATM (non-PAIR) doesn't need the expiry to build selected_symbols —
        # it only uses it at signal time. Defer to session start so we don't require
        # a live Kite API key just to build the config (e.g. programmatic paper runners).
        _defer_expiry = engine_choice in {"6", "7", "8"} and str(data.get("fno_structure", "SINGLE")).upper() != "PAIR"
        if not fno_expiry and not _defer_expiry:
            available = get_available_expiries(
                fno_underlying,
                instrument_type="FUT" if "futures" in selected_engine_name else "OPT",
            )
            if not available:
                raise ValueError(f"No expiries found for {fno_underlying}")
            fno_expiry = available[0]

        if "futures" in selected_engine_name:
            from fno_data_fetcher import resolve_futures_contract
            if fno_underlying == "BOTH":
                contracts = []
                for sym in list(FNO_INDEX_SYMBOLS):
                    exp = get_available_expiries(sym, instrument_type="FUT")
                    if not exp:
                        raise ValueError(f"No futures expiries for {sym}")
                    contracts.append(resolve_futures_contract(sym, exp[0]))
                selected_symbols = contracts
            else:
                contract = resolve_futures_contract(fno_underlying, fno_expiry)
                selected_symbols = [contract]
            symbol_mode = "FNO"

        elif engine_choice in {"6", "7", "8"}:
            fno_structure = str(data.get("fno_structure", "SINGLE")).upper() if engine_choice == "6" else "SINGLE"
            if fno_structure == "PAIR":
                lower_pe = int(data.get("lower_pe_strike", 0))
                upper_ce = int(data.get("upper_ce_strike", 0))
                if lower_pe <= 0 or upper_ce <= 0 or lower_pe >= upper_ce:
                    raise ValueError("lower_pe_strike must be below upper_ce_strike and both must be positive")
                pe_contract = resolve_option_contract(fno_underlying, fno_expiry, lower_pe, "PE")
                ce_contract = resolve_option_contract(fno_underlying, fno_expiry, upper_ce, "CE")
                pe_lot = get_contract_lot_size(pe_contract)
                ce_lot = get_contract_lot_size(ce_contract)
                if pe_lot != ce_lot:
                    raise ValueError(f"Mismatched lot sizes: {pe_lot} vs {ce_lot}")
                pair_id = f"PAIR:{fno_underlying}:{fno_expiry}:{lower_pe}:{upper_ce}"
                option_pair_config = {
                    "mode": "TWO_LEG_RANGE",
                    "pair_id": pair_id,
                    "underlying": fno_underlying,
                    "expiry": fno_expiry,
                    "lower_strike": lower_pe,
                    "upper_strike": upper_ce,
                    "pe_symbol": pe_contract,
                    "ce_symbol": ce_contract,
                    "symbols": [pe_contract, ce_contract],
                    "entry_side": "SELL",
                }
                selected_symbols = [pe_contract, ce_contract]
                symbol_mode = "FNO_PAIR"
            else:
                strike_offset_mode = str(data.get("strike_offset_mode", "ATM")).upper()
                strike_offset = {"ATM": 0, "ATM_PLUS_1": 1, "ATM_MINUS_1": -1}.get(strike_offset_mode, 0)
                scan_symbol = get_fno_spot_quote_symbol(fno_underlying)
                atm_option_config = {
                    "mode": "ATM_DYNAMIC",
                    "underlying": fno_underlying,
                    "expiry": fno_expiry,
                    "scan_symbol": scan_symbol,
                    "strike_offset_mode": strike_offset_mode,
                    "strike_offset": strike_offset,
                }
                selected_symbols = [scan_symbol]
                symbol_mode = "FNO_ATM"

        else:  # options_equity (engine 4)
            option_type = str(data.get("option_type", "CE")).upper()
            strike = int(data.get("strike", 0))
            if strike <= 0:
                strike = get_atm_option_strike(fno_underlying, fno_expiry, option_type)
            contract = resolve_option_contract(fno_underlying, fno_expiry, strike, option_type)
            selected_symbols = [contract]
            symbol_mode = "FNO"

    else:
        # equity engines — symbol selection from form
        symbol_mode_raw = str(data.get("symbol_mode", "NIFTY50")).upper()
        if symbol_mode_raw == "SINGLE":
            sym_raw = str(data.get("symbol", "")).strip().upper()
            if not sym_raw:
                raise ValueError("symbol is required for SINGLE mode")
            if not sym_raw.endswith(".NS"):
                sym_raw += ".NS"
            selected_symbols = [sym_raw]
            symbol_mode = "SINGLE"
        elif symbol_mode_raw == "MANUAL_MULTI":
            raw_list = data.get("symbols", [])
            if isinstance(raw_list, str):
                raw_list = [s.strip() for s in raw_list.split(",") if s.strip()]
            syms = []
            seen: set = set()
            for s in raw_list:
                s = s.strip().upper()
                if not s:
                    continue
                if not s.endswith(".NS"):
                    s += ".NS"
                if s not in seen:
                    syms.append(s)
                    seen.add(s)
            if not syms:
                raise ValueError("At least one symbol required for MANUAL_MULTI mode")
            selected_symbols = syms
            symbol_mode = "MANUAL_MULTI"
        else:
            from config import NIFTY50_SYMBOLS
            selected_symbols = list(NIFTY50_SYMBOLS)
            symbol_mode = "NIFTY50"

    # ── risk style ────────────────────────────────────────────────────────────
    risk_styles = get_risk_style_presets(engine_cls.name)
    risk_style_key = str(data.get("risk_style", "2"))
    if risk_style_key not in risk_styles:
        raise ValueError(f"risk_style must be one of {list(risk_styles.keys())}")
    risk_style = risk_styles[risk_style_key]
    atr_stop_multiplier = risk_style["atr_stop_multiplier"]
    trailing_atr_multiplier = risk_style["trailing_atr_multiplier"]
    target_risk_reward = risk_style["target_risk_reward"]
    sl_percent = float(risk_style.get("sl_percent", ENGINE_FALLBACK_LEVELS["sl_percent"]))
    target_percent = float(risk_style.get("target_percent", ENGINE_FALLBACK_LEVELS["target_percent"]))
    trailing_percent = float(risk_style.get("trailing_percent", ENGINE_FALLBACK_LEVELS["trailing_percent"]))
    risk_percent = risk_style["risk_percent"]

    # ── engine instance ───────────────────────────────────────────────────────
    engine = engine_cls(sl_percent=sl_percent, target_percent=target_percent, trailing_percent=trailing_percent)
    if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES:
        engine.sl_percent = 10.0
        engine.target_percent = 20.0
        engine.trailing_percent = 7.5
    if engine.name == "delivery_equity":
        max_sym_alloc = float(data.get("max_symbol_allocation_pct", 25)) / 100
        engine.set_portfolio_rules(max_sym_alloc)

    # ── position limits ───────────────────────────────────────────────────────
    auto_single = should_auto_select_top1(symbol_mode, selected_symbols, option_pair_config, atm_option_config)
    if auto_single:
        max_open_positions = 1
    else:
        max_open_positions = max(1, int(data.get("max_open_positions", 1)))

    default_per_trade = capital / max_open_positions
    max_capital_per_trade = float(data.get("max_capital_per_trade", default_per_trade))
    max_capital_deployed = float(data.get("max_capital_deployed", capital))
    if max_capital_per_trade <= 0 or max_capital_per_trade > capital:
        raise ValueError("max_capital_per_trade must be between 0 and capital")
    if max_capital_deployed <= 0 or max_capital_deployed > capital:
        raise ValueError("max_capital_deployed must be between 0 and capital")

    one_trade_per_symbol_per_day = bool(data.get("one_trade_per_symbol_per_day", True))

    # ── entry selection ───────────────────────────────────────────────────────
    if auto_single:
        entry_selection_mode = "TOP1"
        top_n_count = 1
    else:
        entry_selection_mode = str(data.get("entry_selection_mode", "TOP1")).upper()
        if entry_selection_mode not in {"TOP1", "TOPN"}:
            raise ValueError("entry_selection_mode must be TOP1 or TOPN")
        top_n_count = int(data.get("top_n_count", 1)) if entry_selection_mode == "TOPN" else 1

    # ── strategy ──────────────────────────────────────────────────────────────
    if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES:
        mode = str(data.get("strategy_mode", "1"))
        if mode not in {"1", "2"}:
            mode = "1"
        valid_iopts = set(engine.supported_strategies.values())
        default_iopts = next(iter(engine.supported_strategies.values()))
        if mode == "1":
            strategy_name_raw = str(data.get("strategy_name", default_iopts)).upper()
            strategy_name: str | None = strategy_name_raw if strategy_name_raw in valid_iopts else default_iopts
            strategies: list[str] | None = None
            min_confirmations: int | None = None
        else:
            raw_strats = data.get("strategies", [])
            if isinstance(raw_strats, str):
                raw_strats = [s.strip() for s in raw_strats.split(",") if s.strip()]
            strategies = [s.upper() for s in raw_strats if s.upper() in valid_iopts]
            if not strategies:
                strategies = list(engine.supported_strategies.values())
            strategy_name = None
            _mc_raw = data.get("min_confirmations")
            min_confirmations = max(1, min(len(strategies), int(_mc_raw))) if _mc_raw is not None else 1
        # momentum_entry_mode set after entry mode is resolved below
    elif engine.name == "intraday_equity" and str(data.get("strategy_mode", "2")) == "3":
        mode = "3"
        strategy_name = None
        strategies = None
        min_confirmations = None
    else:
        mode = str(data.get("strategy_mode", "2"))
        if mode not in {"1", "2"}:
            raise ValueError("strategy_mode must be 1 (SINGLE) or 2 (MULTI)")
        if mode == "1":
            strategy_name = str(data.get("strategy_name", "")).upper()
            if strategy_name not in engine.supported_strategies.values():
                raise ValueError(
                    f"strategy_name must be one of {list(engine.supported_strategies.values())}"
                )
            strategies = None
            min_confirmations = None
        else:
            raw_strats = data.get("strategies", [])
            if isinstance(raw_strats, str):
                raw_strats = [s.strip() for s in raw_strats.split(",") if s.strip()]
            valid = set(engine.supported_strategies.values())
            strategies = [s.upper() for s in raw_strats if s.upper() in valid]
            if not strategies:
                strategies = list(engine.supported_strategies.values())[:2]
            strategy_name = None
            min_confirmations = DEFAULT_CONFIRMATIONS.get(len(strategies), len(strategies))

    # ── intraday options config ───────────────────────────────────────────────
    intraday_options_lot_mode: str | None = None
    intraday_options_entry_mode: str | None = None
    max_trades_per_underlying: int | None = None
    time_exit_minutes: int | None = None
    forming_tick_enabled: bool | None = None
    forming_tick_confirm_ticks: int | None = None
    partial_exit_enabled_live: bool | None = None
    if engine.name in INTRADAY_OPTIONS_ENGINE_NAMES and atm_option_config:
        _lot_mode_from_form = str(data.get("intraday_options_lot_mode", "")).strip().upper()
        intraday_options_lot_mode = (
            _lot_mode_from_form
            if _lot_mode_from_form in {"ONE_LOT", "CAPITAL_BASED"}
            else str(runtime_config.fno.intraday_options_lot_mode or "CAPITAL_BASED").upper()
        )
        _entry_mode_from_form = str(data.get("intraday_options_entry_mode", "")).strip().upper()
        intraday_options_entry_mode = (
            _entry_mode_from_form
            if _entry_mode_from_form in {"LIVE_STAGED", "LEGACY_IMMEDIATE", "LIVE_TICK_CONFIRM"}
            else str(runtime_config.fno.intraday_options_entry_mode or "LIVE_STAGED").upper()
        )
        engine.momentum_entry_mode = (
            "LEGACY_RAW" if intraday_options_entry_mode == "LEGACY_IMMEDIATE" else "STAGED"
        )
        _max_trades_raw = data.get("max_trades_per_underlying")
        if _max_trades_raw is not None and str(_max_trades_raw).strip():
            max_trades_per_underlying = max(1, min(10, int(_max_trades_raw)))
        _time_exit_raw = data.get("time_exit_minutes")
        if _time_exit_raw is not None and str(_time_exit_raw).strip():
            time_exit_minutes = max(0, min(120, int(_time_exit_raw)))
        _forming_raw = data.get("forming_tick_enabled")
        if _forming_raw is not None:
            forming_tick_enabled = bool(_forming_raw)
            _confirm_raw = data.get("forming_tick_confirm_ticks")
            if _confirm_raw is not None and str(_confirm_raw).strip():
                forming_tick_confirm_ticks = max(1, min(5, int(_confirm_raw)))
        _partial_raw = data.get("partial_exit_enabled")
        if _partial_raw is not None:
            partial_exit_enabled_live = None if bool(_partial_raw) else False

    session_config = validate_session_config(SessionConfig(
        engine_choice=engine_choice,
        engine=engine,
        execution_mode=execution_mode,
        exit_only_mode=exit_only_mode,
        live_broker_resync_interval_seconds=live_broker_resync_interval_seconds,
        data_provider=data_provider,
        execution_provider=execution_provider,
        capital=capital,
        selected_symbols=selected_symbols,
        symbol_mode=symbol_mode,
        option_pair_config=option_pair_config,
        atm_option_config=atm_option_config,
        risk_style_name=risk_style["name"],
        atr_stop_multiplier=atr_stop_multiplier,
        trailing_atr_multiplier=trailing_atr_multiplier,
        target_risk_reward=target_risk_reward,
        sl_percent=sl_percent,
        target_percent=target_percent,
        trailing_percent=trailing_percent,
        risk_percent=risk_percent,
        max_open_positions=max_open_positions,
        max_capital_per_trade=max_capital_per_trade,
        max_capital_deployed=max_capital_deployed,
        one_trade_per_symbol_per_day=one_trade_per_symbol_per_day,
        entry_selection_mode=entry_selection_mode,
        top_n_count=top_n_count,
        mode=mode,
        strategy_name=strategy_name,
        strategies=strategies,
        min_confirmations=min_confirmations,
        intraday_options_lot_mode=intraday_options_lot_mode,
        intraday_options_entry_mode=intraday_options_entry_mode,
        max_trades_per_underlying=max_trades_per_underlying,
        time_exit_minutes=time_exit_minutes,
        forming_tick_enabled=forming_tick_enabled,
        forming_tick_confirm_ticks=forming_tick_confirm_ticks,
        partial_exit_enabled=partial_exit_enabled_live,
        paper_trading_override=bool(data.get("paper_trading_override", False)),
        exit_mode=exit_mode,
        daily_max_loss_pct=daily_max_loss_pct,
    ))
    log_session_config_summary(session_config)
    return session_config
