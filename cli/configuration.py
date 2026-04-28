from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import get_broker_ip_mode, get_upstox_static_ip
from data_fetcher import set_data_provider
from engines.base import TradingEngine
from engines import (
    DeliveryEquityEngine,
    FuturesEquityEngine,
    IntradayEquityEngine,
    IntradayFuturesEngine,
    IntradayOptionsEngine,
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

RISK_STYLES = {
    "1": {
        "name": "CONSERVATIVE",
        "atr_stop_multiplier": 1.5,
        "trailing_atr_multiplier": 1.0,
        "target_risk_reward": 1.8,
        "sl_percent": 0.4,
        "target_percent": 0.8,
        "trailing_percent": 0.25,
        "risk_percent": 0.005,
    },
    "2": {
        "name": "BALANCED",
        "atr_stop_multiplier": 2.0,
        "trailing_atr_multiplier": 1.25,
        "target_risk_reward": 2.0,
        "sl_percent": 0.5,
        "target_percent": 1.0,
        "trailing_percent": 0.35,
        "risk_percent": 0.01,
    },
    "3": {
        "name": "AGGRESSIVE",
        "atr_stop_multiplier": 2.5,
        "trailing_atr_multiplier": 1.5,
        "target_risk_reward": 2.2,
        "sl_percent": 0.7,
        "target_percent": 1.4,
        "trailing_percent": 0.5,
        "risk_percent": 0.015,
    },
}

DEFAULT_CONFIRMATIONS = {
    2: 2,
    3: 2,
    4: 3,
    5: 3,
}

ENGINE_OPTIONS = {
    "1": IntradayEquityEngine,
    "2": DeliveryEquityEngine,
    "3": FuturesEquityEngine,
    "4": OptionsEquityEngine,
    "5": IntradayFuturesEngine,
    "6": IntradayOptionsEngine,
}


@dataclass
class SessionConfig:
    engine_choice: str
    engine: TradingEngine
    execution_mode: str
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


def log_help(message: str) -> None:
    log_event(f"[HELP] {message}")


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
        return cli_input.prompt_choice(
            "F&O futures universe: NIFTY 50(1), SENSEX(2), BOTH(3) [default 3]: ",
            [
                {"label": get_fno_display_name(FNO_INDEX_SYMBOLS[0]), "key": 1, "value": FNO_INDEX_SYMBOLS[0]},
                {"label": get_fno_display_name(FNO_INDEX_SYMBOLS[1]), "key": 2, "value": FNO_INDEX_SYMBOLS[1]},
                {"label": "BOTH", "key": 3, "value": "BOTH"},
            ],
            default=3,
        )

    return cli_input.prompt_choice(
        "F&O options underlying: " + ", ".join(
            f"{get_fno_display_name(symbol)}({i})"
            for i, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1)
        ) + " [default 1]: ",
        [
            {"label": get_fno_display_name(symbol), "key": i, "value": symbol}
            for i, symbol in enumerate(FNO_INDEX_SYMBOLS, start=1)
        ],
        default=1,
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
    expiry_choice = cli_input.prompt_int(
        "Choose expiry [default 1]: ",
        default=1,
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
    strike_offset_mode = cli_input.prompt_choice(
        "ATM strike mode: ATM(1), ATM + 1 STRIKE(2), ATM - 1 STRIKE(3) [default 1]: ",
        [
            {"label": "ATM", "key": 1, "value": "ATM"},
            {"label": "ATM + 1", "key": 2, "value": "ATM_PLUS_1"},
            {"label": "ATM - 1", "key": 3, "value": "ATM_MINUS_1"},
        ],
        default=1,
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

    if engine_name == "intraday_options":
        structure_mode = cli_input.prompt_choice(
            "Options structure: ATM SINGLE OPTION(1) or TWO-LEG RANGE PAIR(2)? [default 1]: ",
            [
                {"label": "ATM SINGLE OPTION", "key": 1, "value": "SINGLE"},
                {"label": "TWO-LEG RANGE PAIR", "key": 2, "value": "PAIR"},
            ],
            default=1,
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
    confirmation = cli_input.prompt_choice(
        "Continue with these F&O contracts? YES(1) or NO(2) [default 1]: ",
        [
            {"label": "YES", "key": 1, "value": "YES"},
            {"label": "NO", "key": 2, "value": "NO"},
        ],
        default=1,
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
    log_broker_network_banner()
    log_event("[SETUP] Choose trading engine - determines trading style and timeframe")
    log_event("[SETUP]   INTRADAY EQUITY: 1-minute data, MIS product, 9:15-15:30, auto square-off")
    log_event("[SETUP]   DELIVERY EQUITY: Daily data, CNC product, long-term holding")
    log_event("[SETUP]   FUTURES EQUITY: Positional index futures on NIFTY 50 and SENSEX via Kite derivatives")
    log_event("[SETUP]   OPTIONS EQUITY: Positional index options on NIFTY 50 and SENSEX with ATM strike assist")
    log_event("[SETUP]   INTRADAY FUTURES: MIS index futures with auto square-off and lot-aware sizing")
    log_event("[SETUP]   INTRADAY OPTIONS: MIS index options with Greeks/IV filters and auto square-off")
    log_help("Pick the engine first so the bot can ask only the prompts relevant to that trading style. Example: 6 for INTRADAY OPTIONS")

    engine_choice = cli_input.prompt_choice(
        "Engine: INTRADAY EQUITY(1), DELIVERY EQUITY(2), FUTURES EQUITY(3), OPTIONS EQUITY(4), INTRADAY FUTURES(5), INTRADAY OPTIONS(6)? [default 1]: ",
        [
            {"label": "INTRADAY EQUITY", "key": 1, "value": "1"},
            {"label": "DELIVERY EQUITY", "key": 2, "value": "2"},
            {"label": "FUTURES EQUITY", "key": 3, "value": "3"},
            {"label": "OPTIONS EQUITY", "key": 4, "value": "4"},
            {"label": "INTRADAY FUTURES", "key": 5, "value": "5"},
            {"label": "INTRADAY OPTIONS", "key": 6, "value": "6"},
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
    if engine_choice in {"3", "4", "5", "6"}:
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

    log_event("[SETUP] Choose risk style - affects stop-loss distance and position sizing")
    log_event("[SETUP]   CONSERVATIVE: 1.5x ATR stops, 0.5% risk per trade, safer but fewer trades")
    log_event("[SETUP]   BALANCED: 2.0x ATR stops, 1.0% risk per trade, good balance")
    log_event("[SETUP]   AGGRESSIVE: 2.5x ATR stops, 1.5% risk per trade, higher risk/reward")
    log_help("Choose how aggressive the stop-loss and position sizing should be. Example: 2 for BALANCED")

    risk_style_key = cli_input.prompt_choice(
        "Risk style: CONSERVATIVE(1), BALANCED(2), AGGRESSIVE(3)? [default 2]: ",
        [
            {"label": "CONSERVATIVE", "key": 1, "value": "1"},
            {"label": "BALANCED", "key": 2, "value": "2"},
            {"label": "AGGRESSIVE", "key": 3, "value": "3"},
        ],
        default=2,
    )
    risk_style = RISK_STYLES[risk_style_key]
    atr_stop_multiplier = risk_style["atr_stop_multiplier"]
    trailing_atr_multiplier = risk_style["trailing_atr_multiplier"]
    target_risk_reward = risk_style["target_risk_reward"]
    sl_percent = risk_style["sl_percent"]
    target_percent = risk_style["target_percent"]
    trailing_percent = risk_style["trailing_percent"]
    risk_percent = risk_style["risk_percent"]

    engine = ENGINE_OPTIONS[engine_choice](
        sl_percent=sl_percent,
        target_percent=target_percent,
        trailing_percent=trailing_percent,
    )
    if engine.name == "intraday_options":
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
        if engine.name == "intraday_options":
            log_event("[SETUP] Intraday ATM options scalping uses dynamic ATM contract selection, 10% stop-loss, 20% target, and cooldown-managed entries.")

    log_event(f"[MAIN] Engine selected: {engine.name}")
    log_event(
        f"[MAIN] Risk style selected: {risk_style['name']} | ATR stop={atr_stop_multiplier:.2f}x | ATR trail={trailing_atr_multiplier:.2f}x | Target RR={target_risk_reward:.2f}x | Capital risk={risk_percent * 100:.2f}%"
    )

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
    log_event(
        f"[MAIN] Scan configuration | Engine={engine.name} | Data provider={data_provider} | Execution provider={execution_provider} | Symbol mode={symbol_mode} | Symbols={len(selected_symbols)} | Data={engine.data_period}/{engine.data_interval} | Mode={mode} | Max positions={max_open_positions} | Max/trade={max_capital_per_trade:.2f} | Max deployed={max_capital_deployed:.2f} | One trade/day={one_trade_per_symbol_per_day} | Selection={entry_selection_mode} | Top N={top_n_count}"
    )

    return SessionConfig(
        engine_choice=engine_choice,
        engine=engine,
        execution_mode=execution_mode,
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
    )
